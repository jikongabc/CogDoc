"""Enterprise OpenID Connect login with durable one-shot browser handoff.

The browser-facing callback never places a CogDoc bearer session in a URL.
Instead, the verified login result is encrypted at rest and exchanged through a
short-lived, one-shot handoff code.  Authorization state, nonce, and PKCE are
also persisted so a process restart does not turn an in-flight login into a
replay or scope-confusion bypass.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass, field as dataclass_field
import hashlib
import json
import math
import secrets
import sqlite3
from threading import RLock
import time
from typing import Any, Callable, Literal, Mapping, Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from cogdoc.connectors.base import ConnectorError
from cogdoc.connectors.http_transport import HttpTransport


OIDC_FLOW_SCHEMA_VERSION = "1"
OIDC_MAX_RESPONSE_BYTES = 1_000_000


class OIDCError(RuntimeError):
    """Base class for OIDC failures safe for bounded route translation."""


class OIDCConfigurationError(OIDCError, ValueError):
    """OIDC server-owned configuration is invalid."""


class OIDCProtocolError(OIDCError):
    """The provider returned an invalid or unverifiable response."""


class OIDCFlowError(OIDCError):
    """An authorization state or handoff code is invalid, expired, or replayed."""


class OIDCTransportError(OIDCError):
    """The provider could not be reached within the bounded transport contract."""


class OIDCCallbackError(OIDCError):
    """A consumed callback failed and must redirect without becoming replayable."""

    def __init__(self, redirect_url: str) -> None:
        super().__init__("OIDC callback failed")
        self.redirect_url = redirect_url


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str, *, field: str) -> bytes:
    if type(value) is not str or not value or len(value) > OIDC_MAX_RESPONSE_BYTES:
        raise OIDCProtocolError(f"invalid {field}")
    try:
        return base64.b64decode(
            value + "=" * (-len(value) % 4), altchars=b"-_", validate=True
        )
    except (ValueError, binascii.Error) as exc:
        raise OIDCProtocolError(f"invalid {field}") from exc


def decode_flow_key(value: str | bytes) -> bytes:
    """Decode one exact 256-bit AES key from a URL-safe base64 value."""

    if isinstance(value, bytes):
        key = bytes(value)
    elif type(value) is str:
        try:
            key = base64.b64decode(
                value + "=" * (-len(value) % 4), altchars=b"-_", validate=True
            )
        except (ValueError, binascii.Error) as exc:
            raise OIDCConfigurationError("OIDC flow key is invalid") from exc
    else:
        raise OIDCConfigurationError("OIDC flow key is invalid")
    if len(key) != 32:
        raise OIDCConfigurationError("OIDC flow key must contain exactly 32 bytes")
    return key


def _clean_identifier(value: str, *, field: str, maximum: int = 512) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or len(value) > maximum
        or any(ord(character) < 33 or ord(character) == 127 for character in value)
    ):
        raise OIDCConfigurationError(f"invalid {field}")
    return value


def _normalized_https_url(value: str, *, field: str, allow_query: bool = False) -> str:
    clean = _clean_identifier(value, field=field, maximum=2048)
    parts = urlsplit(clean)
    if (
        parts.scheme.lower() != "https"
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.fragment
        or (parts.query and not allow_query)
    ):
        raise OIDCConfigurationError(f"{field} must be an absolute HTTPS URL")
    try:
        port = parts.port
    except ValueError as exc:
        raise OIDCConfigurationError(f"invalid {field} port") from exc
    assert parts.hostname is not None
    host = parts.hostname.lower().rstrip(".")
    netloc = host if port in (None, 443) else f"{host}:{port}"
    path = parts.path or "/"
    return urlunsplit(("https", netloc, path, parts.query, ""))


@dataclass(frozen=True, slots=True)
class OIDCProviderConfig:
    issuer: str
    client_id: str
    redirect_uri: str
    client_secret: str | None = None
    display_name: str = "Enterprise SSO"
    scopes: tuple[str, ...] = ("openid", "email", "profile")
    allowed_endpoint_hosts: tuple[str, ...] = ()
    allowed_return_urls: tuple[str, ...] = ()
    timeout_seconds: float = 15.0
    clock_skew_seconds: float = 60.0

    def validated(self) -> "OIDCProviderConfig":
        issuer = _normalized_https_url(self.issuer, field="issuer").rstrip("/")
        if urlsplit(issuer).port not in (None, 443):
            raise OIDCConfigurationError("OIDC issuer must use the default HTTPS port")
        redirect_uri = _normalized_https_url(
            self.redirect_uri, field="redirect_uri", allow_query=True
        )
        client_id = _clean_identifier(self.client_id, field="client_id", maximum=512)
        if self.client_secret is not None:
            _clean_identifier(self.client_secret, field="client_secret", maximum=4096)
        display_name = " ".join(str(self.display_name).split())
        if not display_name or len(display_name) > 120:
            raise OIDCConfigurationError("invalid OIDC display name")
        scopes = tuple(dict.fromkeys(str(item).strip() for item in self.scopes))
        if "openid" not in scopes or any(
            not item
            or len(item) > 128
            or any(character.isspace() for character in item)
            for item in scopes
        ):
            raise OIDCConfigurationError("OIDC scopes must include openid")
        issuer_host = urlsplit(issuer).hostname or ""
        allowed_hosts = tuple(
            dict.fromkeys(
                host.strip().lower().rstrip(".")
                for host in self.allowed_endpoint_hosts
                if host.strip()
            )
        )
        if any(
            not host or ":" in host or "/" in host or len(host) > 253
            for host in allowed_hosts
        ):
            raise OIDCConfigurationError("invalid OIDC endpoint host allowlist")
        if issuer_host not in allowed_hosts:
            allowed_hosts = (issuer_host, *allowed_hosts)
        returns = tuple(
            dict.fromkeys(
                _normalized_https_url(
                    item, field="allowed_return_url", allow_query=True
                )
                for item in self.allowed_return_urls
            )
        )
        if not returns:
            raise OIDCConfigurationError("OIDC requires at least one return URL")
        for field, value, lower, upper in (
            ("timeout_seconds", self.timeout_seconds, 1.0, 60.0),
            ("clock_skew_seconds", self.clock_skew_seconds, 0.0, 300.0),
        ):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise OIDCConfigurationError(f"invalid {field}")
            number = float(value)
            if not math.isfinite(number) or not lower <= number <= upper:
                raise OIDCConfigurationError(f"invalid {field}")
        return OIDCProviderConfig(
            issuer=issuer,
            client_id=client_id,
            redirect_uri=redirect_uri,
            client_secret=self.client_secret,
            display_name=display_name,
            scopes=scopes,
            allowed_endpoint_hosts=allowed_hosts,
            allowed_return_urls=returns,
            timeout_seconds=float(self.timeout_seconds),
            clock_skew_seconds=float(self.clock_skew_seconds),
        )

    def validate_return_url(self, value: str) -> str:
        candidate = _normalized_https_url(value, field="return_url", allow_query=True)
        if candidate not in self.allowed_return_urls:
            raise OIDCConfigurationError("return URL is not allowlisted")
        return candidate


class OIDCTransport(Protocol):
    def get_json(self, url: str) -> Mapping[str, Any]: ...

    def post_form(self, url: str, data: Mapping[str, str]) -> Mapping[str, Any]: ...


class HttpxOIDCTransport:
    """Bounded, DNS-pinned HTTPS transport for server-owned OIDC endpoints."""

    def __init__(self, *, allowed_hosts: tuple[str, ...], timeout_seconds: float):
        self._allowed_hosts = frozenset(allowed_hosts)
        self._transport = HttpTransport(
            allowed_hosts=set(allowed_hosts),
            timeout_seconds=timeout_seconds,
            max_response_bytes=OIDC_MAX_RESPONSE_BYTES,
            max_redirects=0,
        )

    def _url(self, value: str) -> str:
        url = _normalized_https_url(value, field="OIDC endpoint", allow_query=True)
        parts = urlsplit(url)
        if (parts.hostname or "").lower().rstrip(
            "."
        ) not in self._allowed_hosts or parts.port not in (None, 443):
            raise OIDCProtocolError("OIDC endpoint host is not allowlisted")
        return url

    @staticmethod
    def _payload(response: Any) -> Mapping[str, Any]:
        if response.status < 200 or response.status >= 300:
            raise OIDCTransportError("OIDC provider returned an unsuccessful response")
        try:
            payload = response.json()
        except (ConnectorError, UnicodeError, ValueError) as exc:
            raise OIDCProtocolError("OIDC provider returned invalid JSON") from exc
        if not isinstance(payload, Mapping):
            raise OIDCProtocolError("OIDC provider response must be an object")
        return dict(payload)

    def get_json(self, url: str) -> Mapping[str, Any]:
        try:
            response = self._transport.request(
                "GET", self._url(url), headers={"Accept": "application/json"}
            )
        except ConnectorError as exc:
            raise OIDCTransportError("OIDC provider request failed") from exc
        return self._payload(response)

    def post_form(self, url: str, data: Mapping[str, str]) -> Mapping[str, Any]:
        try:
            response = self._transport.request(
                "POST",
                self._url(url),
                body=urlencode(dict(data)).encode("ascii"),
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
        except ConnectorError as exc:
            raise OIDCTransportError("OIDC provider request failed") from exc
        return self._payload(response)

    def close(self) -> None:
        return None


@dataclass(frozen=True, slots=True)
class OIDCFlow:
    flow_id: str
    intent: Literal["login", "link"]
    state: str
    nonce: str
    code_verifier: str
    return_url: str
    workspace_id: str | None
    user_id: str | None
    session_id: str | None
    expires_at: float


class OIDCFlowStore:
    """SQLite-backed encrypted OIDC state and browser-result handoff store."""

    def __init__(
        self,
        db_path: str,
        key: str | bytes,
        *,
        clock: Callable[[], float] = time.time,
        flow_ttl_seconds: float = 600.0,
        result_ttl_seconds: float = 60.0,
        busy_timeout_ms: int = 5000,
    ) -> None:
        self._aes = AESGCM(decode_flow_key(key))
        self._clock = clock
        self.flow_ttl_seconds = self._duration(
            flow_ttl_seconds, "flow_ttl_seconds", 30.0, 1800.0
        )
        self.result_ttl_seconds = self._duration(
            result_ttl_seconds, "result_ttl_seconds", 10.0, 300.0
        )
        self._lock = RLock()
        self._closed = False
        self._conn = sqlite3.connect(
            db_path, check_same_thread=False, isolation_level=None
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute(f"PRAGMA busy_timeout={max(0, int(busy_timeout_ms))}")
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS auth_oidc_flow_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS auth_oidc_flows (
                flow_id TEXT PRIMARY KEY,
                state_hash TEXT NOT NULL UNIQUE CHECK(length(state_hash)=64),
                intent TEXT NOT NULL CHECK(intent IN ('login','link')),
                encrypted_context BLOB NOT NULL,
                return_url TEXT NOT NULL,
                workspace_id TEXT,
                user_id TEXT,
                session_id TEXT,
                created_at REAL NOT NULL,
                expires_at REAL NOT NULL,
                consumed_at REAL,
                result_code_hash TEXT UNIQUE,
                encrypted_result BLOB,
                result_expires_at REAL,
                result_consumed_at REAL,
                CHECK((intent='login' AND user_id IS NULL AND session_id IS NULL)
                   OR (intent='link' AND user_id IS NOT NULL AND session_id IS NOT NULL))
            );
            CREATE INDEX IF NOT EXISTS idx_auth_oidc_flows_expiry
                ON auth_oidc_flows(expires_at, result_expires_at);
            """
        )
        row = self._conn.execute(
            "SELECT value FROM auth_oidc_flow_meta WHERE key='schema_version'"
        ).fetchone()
        if row is None:
            self._conn.execute(
                "INSERT INTO auth_oidc_flow_meta(key,value) VALUES('schema_version',?)",
                (OIDC_FLOW_SCHEMA_VERSION,),
            )
        elif row[0] != OIDC_FLOW_SCHEMA_VERSION:
            self._conn.close()
            self._closed = True
            raise OIDCConfigurationError("unsupported OIDC flow schema version")

    @staticmethod
    def _duration(value: float, field: str, lower: float, upper: float) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise OIDCConfigurationError(f"invalid {field}")
        result = float(value)
        if not math.isfinite(result) or not lower <= result <= upper:
            raise OIDCConfigurationError(f"invalid {field}")
        return result

    def _now(self) -> float:
        value = float(self._clock())
        if not math.isfinite(value):
            raise OIDCFlowError("OIDC clock returned a non-finite value")
        return value

    def _ensure_open(self) -> None:
        if self._closed:
            raise OIDCFlowError("OIDC flow store is closed")

    @staticmethod
    def _digest(value: str) -> str:
        if type(value) is not str or not value or value != value.strip():
            raise OIDCFlowError("invalid OIDC one-shot value")
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def _encrypt(self, flow_id: str, kind: str, payload: Mapping[str, Any]) -> bytes:
        nonce = secrets.token_bytes(12)
        plaintext = json.dumps(
            dict(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        if len(plaintext) > OIDC_MAX_RESPONSE_BYTES:
            raise OIDCFlowError("OIDC flow payload exceeds the size limit")
        aad = f"cogdoc-oidc:{OIDC_FLOW_SCHEMA_VERSION}:{kind}:{flow_id}".encode()
        return nonce + self._aes.encrypt(nonce, plaintext, aad)

    def _decrypt(self, flow_id: str, kind: str, ciphertext: bytes) -> dict[str, Any]:
        if not isinstance(ciphertext, bytes) or len(ciphertext) < 29:
            raise OIDCFlowError("OIDC flow payload is invalid")
        aad = f"cogdoc-oidc:{OIDC_FLOW_SCHEMA_VERSION}:{kind}:{flow_id}".encode()
        try:
            plaintext = self._aes.decrypt(ciphertext[:12], ciphertext[12:], aad)
            payload = json.loads(plaintext)
        except Exception as exc:
            raise OIDCFlowError("OIDC flow payload could not be authenticated") from exc
        if not isinstance(payload, dict):
            raise OIDCFlowError("OIDC flow payload is invalid")
        return payload

    def create(
        self,
        *,
        intent: Literal["login", "link"],
        return_url: str,
        workspace_id: str | None = None,
        user_id: str | None = None,
        session_id: str | None = None,
    ) -> OIDCFlow:
        if intent not in {"login", "link"}:
            raise OIDCFlowError("invalid OIDC flow intent")
        if intent == "link" and (not user_id or not session_id):
            raise OIDCFlowError("link flow requires a live user session")
        if intent == "login" and (user_id is not None or session_id is not None):
            raise OIDCFlowError("login flow cannot bind a user session")
        flow_id = f"odf_{secrets.token_urlsafe(18)}"
        state = f"ods_{secrets.token_urlsafe(32)}"
        nonce = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        now = self._now()
        expires_at = now + self.flow_ttl_seconds
        encrypted = self._encrypt(
            flow_id, "context", {"nonce": nonce, "code_verifier": verifier}
        )
        with self._lock:
            self._ensure_open()
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    "INSERT INTO auth_oidc_flows(flow_id,state_hash,intent,encrypted_context,"
                    "return_url,workspace_id,user_id,session_id,created_at,expires_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        flow_id,
                        self._digest(state),
                        intent,
                        encrypted,
                        return_url,
                        workspace_id,
                        user_id,
                        session_id,
                        now,
                        expires_at,
                    ),
                )
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise
        return OIDCFlow(
            flow_id,
            intent,
            state,
            nonce,
            verifier,
            return_url,
            workspace_id,
            user_id,
            session_id,
            expires_at,
        )

    def consume_state(self, state: str) -> OIDCFlow:
        digest, now = self._digest(state), self._now()
        with self._lock:
            self._ensure_open()
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT flow_id,intent,encrypted_context,return_url,workspace_id,user_id,"
                    "session_id,expires_at,consumed_at FROM auth_oidc_flows WHERE state_hash=?",
                    (digest,),
                ).fetchone()
                if row is None or row[8] is not None or float(row[7]) <= now:
                    raise OIDCFlowError(
                        "OIDC state is invalid, expired, or already used"
                    )
                changed = self._conn.execute(
                    "UPDATE auth_oidc_flows SET consumed_at=? "
                    "WHERE flow_id=? AND consumed_at IS NULL AND expires_at>?",
                    (now, row[0], now),
                ).rowcount
                if changed != 1:
                    raise OIDCFlowError("OIDC state changed during consumption")
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise
        context = self._decrypt(str(row[0]), "context", bytes(row[2]))
        nonce, verifier = context.get("nonce"), context.get("code_verifier")
        if not isinstance(nonce, str) or not isinstance(verifier, str):
            raise OIDCFlowError("OIDC flow context is invalid")
        return OIDCFlow(
            str(row[0]),
            str(row[1]),  # type: ignore[arg-type]
            state,
            nonce,
            verifier,
            str(row[3]),
            None if row[4] is None else str(row[4]),
            None if row[5] is None else str(row[5]),
            None if row[6] is None else str(row[6]),
            float(row[7]),
        )

    def store_result(self, flow_id: str, result: Mapping[str, Any]) -> str:
        code = f"odc_{secrets.token_urlsafe(32)}"
        now = self._now()
        encrypted = self._encrypt(flow_id, "result", result)
        with self._lock:
            self._ensure_open()
            changed = self._conn.execute(
                "UPDATE auth_oidc_flows SET result_code_hash=?,encrypted_result=?,"
                "result_expires_at=? WHERE flow_id=? AND consumed_at IS NOT NULL "
                "AND result_code_hash IS NULL",
                (self._digest(code), encrypted, now + self.result_ttl_seconds, flow_id),
            ).rowcount
            if changed != 1:
                raise OIDCFlowError("OIDC flow cannot accept a result")
        return code

    def exchange_result(self, code: str) -> dict[str, Any]:
        digest, now = self._digest(code), self._now()
        with self._lock:
            self._ensure_open()
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT flow_id,encrypted_result,result_expires_at,result_consumed_at "
                    "FROM auth_oidc_flows WHERE result_code_hash=?",
                    (digest,),
                ).fetchone()
                if (
                    row is None
                    or row[1] is None
                    or row[2] is None
                    or float(row[2]) <= now
                    or row[3] is not None
                ):
                    raise OIDCFlowError(
                        "OIDC handoff code is invalid, expired, or already used"
                    )
                changed = self._conn.execute(
                    "UPDATE auth_oidc_flows SET result_consumed_at=? "
                    "WHERE flow_id=? AND result_consumed_at IS NULL AND result_expires_at>?",
                    (now, row[0], now),
                ).rowcount
                if changed != 1:
                    raise OIDCFlowError("OIDC handoff changed during consumption")
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise
        return self._decrypt(str(row[0]), "result", bytes(row[1]))

    def purge_expired(self, *, limit: int = 1000) -> int:
        if type(limit) is not int or not 1 <= limit <= 10_000:
            raise OIDCFlowError("invalid purge limit")
        now = self._now()
        with self._lock:
            self._ensure_open()
            rows = self._conn.execute(
                "SELECT flow_id FROM auth_oidc_flows WHERE "
                "(result_expires_at IS NOT NULL AND result_expires_at<=?) "
                "OR (result_expires_at IS NULL AND expires_at<=?) "
                "ORDER BY COALESCE(result_expires_at,expires_at),flow_id LIMIT ?",
                (now, now, limit),
            ).fetchall()
            if not rows:
                return 0
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                removed = 0
                for (flow_id,) in rows:
                    removed += self._conn.execute(
                        "DELETE FROM auth_oidc_flows WHERE flow_id=? AND "
                        "((result_expires_at IS NOT NULL AND result_expires_at<=?) "
                        "OR (result_expires_at IS NULL AND expires_at<=?))",
                        (flow_id, now, now),
                    ).rowcount
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise
            return removed

    def check(self) -> bool:
        with self._lock:
            self._ensure_open()
            try:
                row = self._conn.execute(
                    "SELECT value FROM auth_oidc_flow_meta WHERE key='schema_version'"
                ).fetchone()
                if row is None or row[0] != OIDC_FLOW_SCHEMA_VERSION:
                    raise OIDCFlowError("OIDC flow schema is unavailable")
                self._conn.execute("SELECT 1 FROM auth_oidc_flows LIMIT 1").fetchone()
            except sqlite3.Error as exc:
                raise OIDCFlowError("OIDC flow readiness check failed") from exc
            return True

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._conn.close()
                self._closed = True


@dataclass(frozen=True, slots=True)
class OIDCClaims:
    issuer: str
    subject: str
    email: str
    email_verified: bool
    display_name: str
    string_list_claims: Mapping[str, tuple[str, ...]] = dataclass_field(
        default_factory=dict
    )


class OIDCClient:
    """OIDC Authorization Code + S256 PKCE client with strict ID-token checks."""

    def __init__(
        self,
        config: OIDCProviderConfig,
        *,
        transport: OIDCTransport | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.config = config.validated()
        self._transport = transport or HttpxOIDCTransport(
            allowed_hosts=self.config.allowed_endpoint_hosts,
            timeout_seconds=self.config.timeout_seconds,
        )
        self._clock = clock
        self._lock = RLock()
        self._discovery: dict[str, Any] | None = None
        self._jwks: dict[str, Any] | None = None

    def _endpoint(self, discovery: Mapping[str, Any], name: str) -> str:
        value = discovery.get(name)
        if not isinstance(value, str):
            raise OIDCProtocolError(f"OIDC discovery is missing {name}")
        url = _normalized_https_url(value, field=name, allow_query=True)
        parts = urlsplit(url)
        if (parts.hostname or "").lower().rstrip(".") not in frozenset(
            self.config.allowed_endpoint_hosts
        ) or parts.port not in (None, 443):
            raise OIDCProtocolError(f"OIDC {name} host is not allowlisted")
        return url

    def discovery(self) -> dict[str, Any]:
        with self._lock:
            if self._discovery is not None:
                return dict(self._discovery)
            url = f"{self.config.issuer}/.well-known/openid-configuration"
            payload = dict(self._transport.get_json(url))
            if payload.get("issuer") != self.config.issuer:
                raise OIDCProtocolError("OIDC discovery issuer mismatch")
            for name in (
                "authorization_endpoint",
                "token_endpoint",
                "jwks_uri",
            ):
                self._endpoint(payload, name)
            algorithms = payload.get("id_token_signing_alg_values_supported")
            if algorithms is not None and (
                not isinstance(algorithms, list) or "RS256" not in algorithms
            ):
                raise OIDCProtocolError("OIDC provider does not support RS256")
            self._discovery = payload
            return dict(payload)

    def authorization_url(self, flow: OIDCFlow) -> str:
        discovery = self.discovery()
        endpoint = self._endpoint(discovery, "authorization_endpoint")
        challenge = _b64url_encode(hashlib.sha256(flow.code_verifier.encode()).digest())
        query = urlencode(
            {
                "response_type": "code",
                "client_id": self.config.client_id,
                "redirect_uri": self.config.redirect_uri,
                "scope": " ".join(self.config.scopes),
                "state": flow.state,
                "nonce": flow.nonce,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
        return f"{endpoint}{'&' if '?' in endpoint else '?'}{query}"

    def _jwks_payload(self, *, refresh: bool = False) -> dict[str, Any]:
        with self._lock:
            if self._jwks is not None and not refresh:
                return dict(self._jwks)
            discovery = self.discovery()
            payload = dict(
                self._transport.get_json(self._endpoint(discovery, "jwks_uri"))
            )
            keys = payload.get("keys")
            if not isinstance(keys, list) or not keys or len(keys) > 100:
                raise OIDCProtocolError("OIDC JWKS is invalid")
            self._jwks = payload
            return dict(payload)

    @staticmethod
    def _jwt_parts(token: str) -> tuple[dict[str, Any], dict[str, Any], bytes, bytes]:
        if type(token) is not str or len(token) > 64_000:
            raise OIDCProtocolError("OIDC ID token is invalid")
        parts = token.split(".")
        if len(parts) != 3:
            raise OIDCProtocolError("OIDC ID token must be a compact JWS")
        try:
            header = json.loads(_b64url_decode(parts[0], field="JWT header"))
            claims = json.loads(_b64url_decode(parts[1], field="JWT claims"))
        except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
            raise OIDCProtocolError("OIDC ID token JSON is invalid") from exc
        if not isinstance(header, dict) or not isinstance(claims, dict):
            raise OIDCProtocolError("OIDC ID token payload is invalid")
        return (
            header,
            claims,
            f"{parts[0]}.{parts[1]}".encode("ascii"),
            _b64url_decode(parts[2], field="JWT signature"),
        )

    @staticmethod
    def _rsa_key(jwk: Mapping[str, Any]):
        if (
            jwk.get("kty") != "RSA"
            or jwk.get("use", "sig") != "sig"
            or jwk.get("alg", "RS256") != "RS256"
        ):
            raise OIDCProtocolError("OIDC signing key is not an RSA signature key")
        raw_exponent, raw_modulus = jwk.get("e"), jwk.get("n")
        if not isinstance(raw_exponent, str) or not isinstance(raw_modulus, str):
            raise OIDCProtocolError("OIDC RSA signing key is invalid")
        exponent = int.from_bytes(_b64url_decode(raw_exponent, field="JWK e"), "big")
        modulus = int.from_bytes(_b64url_decode(raw_modulus, field="JWK n"), "big")
        if exponent < 3 or exponent % 2 == 0 or modulus.bit_length() < 2048:
            raise OIDCProtocolError("OIDC RSA signing key is too weak")
        try:
            return rsa.RSAPublicNumbers(exponent, modulus).public_key()
        except ValueError as exc:
            raise OIDCProtocolError("OIDC RSA signing key is invalid") from exc

    def _select_key(self, kid: str, *, refresh: bool = False) -> Mapping[str, Any]:
        payload = self._jwks_payload(refresh=refresh)
        matches = [
            item
            for item in payload.get("keys", [])
            if isinstance(item, Mapping) and item.get("kid") == kid
        ]
        if len(matches) != 1:
            if not refresh:
                return self._select_key(kid, refresh=True)
            raise OIDCProtocolError("OIDC signing key is unavailable or ambiguous")
        return matches[0]

    def verify_id_token(self, token: str, *, nonce: str) -> OIDCClaims:
        header, claims, signing_input, signature = self._jwt_parts(token)
        if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
            raise OIDCProtocolError("OIDC ID token uses an unsupported algorithm")
        kid = str(header["kid"])
        key = self._rsa_key(self._select_key(kid))
        try:
            key.verify(signature, signing_input, padding.PKCS1v15(), hashes.SHA256())
        except InvalidSignature:
            # Providers may rotate key material while retaining a stable kid.
            # Refetch exactly once before treating the token as invalid.
            key = self._rsa_key(self._select_key(kid, refresh=True))
            try:
                key.verify(
                    signature, signing_input, padding.PKCS1v15(), hashes.SHA256()
                )
            except InvalidSignature as exc:
                raise OIDCProtocolError("OIDC ID token signature is invalid") from exc
        except (TypeError, ValueError) as exc:
            raise OIDCProtocolError("OIDC ID token signature is invalid") from exc
        now = float(self._clock())
        if not math.isfinite(now):
            raise OIDCProtocolError("OIDC clock returned a non-finite value")
        if claims.get("iss") != self.config.issuer:
            raise OIDCProtocolError("OIDC ID token issuer mismatch")
        audience = claims.get("aud")
        audiences = [audience] if isinstance(audience, str) else audience
        if (
            not isinstance(audiences, list)
            or not audiences
            or any(not isinstance(item, str) for item in audiences)
            or self.config.client_id not in audiences
        ):
            raise OIDCProtocolError("OIDC ID token audience mismatch")
        if len(audiences) > 1 and claims.get("azp") != self.config.client_id:
            raise OIDCProtocolError("OIDC ID token authorized party mismatch")
        skew = self.config.clock_skew_seconds
        for field in ("exp", "iat"):
            value = claims.get(field)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise OIDCProtocolError(f"OIDC ID token is missing {field}")
            if not math.isfinite(float(value)):
                raise OIDCProtocolError(f"OIDC ID token has invalid {field}")
        if float(claims["exp"]) <= now - skew:
            raise OIDCProtocolError("OIDC ID token has expired")
        if float(claims["iat"]) > now + skew:
            raise OIDCProtocolError("OIDC ID token was issued in the future")
        not_before = claims.get("nbf")
        if not_before is not None:
            if (
                isinstance(not_before, bool)
                or not isinstance(not_before, (int, float))
                or not math.isfinite(float(not_before))
                or float(not_before) > now + skew
            ):
                raise OIDCProtocolError("OIDC ID token is not active yet")
        if claims.get("nonce") != nonce:
            raise OIDCProtocolError("OIDC ID token nonce mismatch")
        subject = claims.get("sub")
        email = claims.get("email")
        if not isinstance(subject, str) or not subject or len(subject) > 512:
            raise OIDCProtocolError("OIDC ID token subject is invalid")
        if not isinstance(email, str) or not email or len(email) > 320:
            raise OIDCProtocolError("OIDC ID token email is invalid")
        if claims.get("email_verified") is not True:
            raise OIDCProtocolError("OIDC email must be explicitly verified")
        display_name = claims.get("name") or claims.get("preferred_username") or email
        if not isinstance(display_name, str):
            display_name = email
        display_name = " ".join(display_name.split())[:120] or email
        string_list_claims: dict[str, tuple[str, ...]] = {}
        total_claim_bytes = 0
        for raw_name, raw_value in claims.items():
            if (
                not isinstance(raw_name, str)
                or not isinstance(raw_value, list)
                or not all(isinstance(item, str) for item in raw_value)
            ):
                continue
            if len(raw_name) > 128 or len(raw_value) > 200:
                raise OIDCProtocolError("OIDC string-list claim exceeds its limit")
            encoded = json.dumps(
                raw_value, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
            total_claim_bytes += len(raw_name.encode("utf-8")) + len(encoded)
            if total_claim_bytes > 32 * 1024:
                raise OIDCProtocolError("OIDC string-list claims are too large")
            string_list_claims[raw_name] = tuple(raw_value)
        return OIDCClaims(
            issuer=self.config.issuer,
            subject=subject,
            email=email,
            email_verified=True,
            display_name=display_name,
            string_list_claims=string_list_claims,
        )

    def exchange_code(self, code: str, *, code_verifier: str, nonce: str) -> OIDCClaims:
        clean_code = _clean_identifier(code, field="authorization code", maximum=8192)
        discovery = self.discovery()
        payload = {
            "grant_type": "authorization_code",
            "code": clean_code,
            "redirect_uri": self.config.redirect_uri,
            "client_id": self.config.client_id,
            "code_verifier": code_verifier,
        }
        if self.config.client_secret is not None:
            payload["client_secret"] = self.config.client_secret
        response = self._transport.post_form(
            self._endpoint(discovery, "token_endpoint"), payload
        )
        token = response.get("id_token")
        if not isinstance(token, str):
            raise OIDCProtocolError("OIDC token response is missing id_token")
        return self.verify_id_token(token, nonce=nonce)

    def close(self) -> None:
        close = getattr(self._transport, "close", None)
        if callable(close):
            close()


def append_query(url: str, **values: str) -> str:
    """Append bounded callback status to an already allowlisted return URL."""

    parts = urlsplit(url)
    query = parse_qsl(parts.query, keep_blank_values=True)
    query.extend((key, value) for key, value in values.items())
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


class OIDCManager:
    """Coordinates OIDC protocol state with the durable CogDoc AuthStore."""

    def __init__(
        self,
        client: OIDCClient,
        flow_store: OIDCFlowStore,
        auth_store: Any,
        *,
        jit_provisioning_enabled: bool = False,
        allow_verified_email_link: bool = False,
    ) -> None:
        self.client = client
        self.flow_store = flow_store
        self.auth_store = auth_store
        self.jit_provisioning_enabled = bool(jit_provisioning_enabled)
        self.allow_verified_email_link = bool(allow_verified_email_link)

    @property
    def enabled(self) -> bool:
        return True

    @property
    def display_name(self) -> str:
        return self.client.config.display_name

    def begin_login(
        self, *, return_url: str, workspace_id: str | None = None
    ) -> dict[str, Any]:
        validated_return = self.client.config.validate_return_url(return_url)
        flow = self.flow_store.create(
            intent="login", return_url=validated_return, workspace_id=workspace_id
        )
        return {
            "flow_id": flow.flow_id,
            "authorization_url": self.client.authorization_url(flow),
            "expires_at": flow.expires_at,
        }

    def begin_link(
        self,
        *,
        return_url: str,
        user_id: str,
        session_id: str,
        workspace_id: str | None = None,
    ) -> dict[str, Any]:
        validated_return = self.client.config.validate_return_url(return_url)
        flow = self.flow_store.create(
            intent="link",
            return_url=validated_return,
            workspace_id=workspace_id,
            user_id=user_id,
            session_id=session_id,
        )
        return {
            "flow_id": flow.flow_id,
            "authorization_url": self.client.authorization_url(flow),
            "expires_at": flow.expires_at,
        }

    def complete_callback(self, *, state: str, code: str) -> str:
        flow = self.flow_store.consume_state(state)
        try:
            claims = self.client.exchange_code(
                code, code_verifier=flow.code_verifier, nonce=flow.nonce
            )
            if flow.intent == "login":
                result = self.auth_store.login_oidc(
                    issuer=claims.issuer,
                    subject=claims.subject,
                    email=claims.email,
                    display_name=claims.display_name,
                    email_verified=claims.email_verified,
                    group_claims=claims.string_list_claims,
                    workspace_id=flow.workspace_id,
                    jit_provisioning_enabled=self.jit_provisioning_enabled,
                    allow_verified_email_link=self.allow_verified_email_link,
                )
                payload = {"kind": "login", "session": result}
            else:
                identity = self.auth_store.link_oidc_identity_from_session(
                    session_id=str(flow.session_id),
                    user_id=str(flow.user_id),
                    issuer=claims.issuer,
                    subject=claims.subject,
                    email=claims.email,
                    email_verified=claims.email_verified,
                )
                payload = {"kind": "link", "identity": identity}
            handoff = self.flow_store.store_result(flow.flow_id, payload)
        except Exception as exc:
            raise OIDCCallbackError(
                append_query(flow.return_url, oidc_error="authorization_failed")
            ) from exc
        return append_query(flow.return_url, oidc_code=handoff)

    def exchange_handoff(self, code: str) -> dict[str, Any]:
        return self.flow_store.exchange_result(code)

    def callback_error(self, *, state: str) -> str:
        flow = self.flow_store.consume_state(state)
        return append_query(flow.return_url, oidc_error="authorization_failed")

    def close(self) -> None:
        self.client.close()
