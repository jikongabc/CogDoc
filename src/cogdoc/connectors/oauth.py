from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import sqlite3
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from threading import RLock
from typing import Any, Protocol
from urllib.parse import urlencode, urlsplit
from uuid import uuid4

from cogdoc.api.persistence import connect_sqlite
from cogdoc.ha.dbapi_compat import BackendDBAPIConnection
from cogdoc.ha.storage import DatabaseBackend
from cogdoc.connectors.credential_store import (
    CredentialRevisionConflict,
    CredentialVault,
)
from cogdoc.connectors.http_transport import HttpResponse, HttpTransport


OAUTH_TIMEOUT_SECONDS = 15.0
MAX_OAUTH_RESPONSE_BYTES = 64 * 1024
DEFAULT_OAUTH_SESSION_TTL_SECONDS = 600
NOTION_API_VERSION = "2026-03-11"

_PROVIDER = re.compile(r"[a-z][a-z0-9_-]{0,63}")
_MICROSOFT_TENANT = re.compile(
    r"(?:common|organizations|consumers|[0-9a-fA-F-]{36}|[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?)"
)
_PKCE_VERIFIER = re.compile(r"[A-Za-z0-9._~-]{43,128}")


class OAuthError(RuntimeError):
    """Base class for fail-closed OAuth control-plane errors."""


class OAuthStateMismatch(OAuthError):
    """The callback state is absent, unknown, or outside the caller scope."""


class OAuthSessionExpired(OAuthError):
    """The callback arrived after the short authorization window."""


class OAuthReplayError(OAuthError):
    """A callback state has already been consumed or cancelled."""


class OAuthProviderError(OAuthError):
    """A provider rejected or returned an invalid token response."""


class OAuthTransport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: bytes | None = None,
    ) -> HttpResponse: ...


def _required(value: object, field: str, *, limit: int = 512) -> str:
    text = str(value or "").strip()
    if not text or len(text) > limit or any(ord(char) < 32 for char in text):
        raise ValueError(f"{field} is invalid")
    return text


def _scope(
    tenant_id: object,
    kb_id: object,
    connection_id: object | None,
    user_id: object,
) -> tuple[str, str, str | None, str]:
    connection = (
        _required(connection_id, "connection_id", limit=160)
        if connection_id is not None
        else None
    )
    return (
        _required(tenant_id, "tenant_id", limit=160),
        _required(kb_id, "kb_id", limit=160),
        connection,
        _required(user_id, "user_id", limit=160),
    )


def _provider(value: object) -> str:
    provider = _required(value, "provider", limit=64).casefold()
    if not _PROVIDER.fullmatch(provider):
        raise ValueError("provider is invalid")
    return provider


def _redirect_uri(value: object) -> str:
    uri = _required(value, "redirect_uri", limit=2048)
    parts = urlsplit(uri)
    host = str(parts.hostname or "").casefold()
    loopback = parts.scheme == "http" and host in {"localhost", "127.0.0.1", "::1"}
    if (
        (parts.scheme != "https" and not loopback)
        or not host
        or parts.username
        or parts.password
        or parts.fragment
    ):
        raise ValueError("redirect_uri must be HTTPS or an HTTP loopback URL")
    return uri


def _scopes(values: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    clean: list[str] = []
    for value in values:
        scope = _required(value, "scope", limit=256)
        if scope not in clean:
            clean.append(scope)
    if len(clean) > 128:
        raise ValueError("too many OAuth scopes")
    return tuple(clean)


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _state_hash(state: str) -> bytes:
    return hashlib.sha256(state.encode("ascii")).digest()


@dataclass(frozen=True, repr=False)
class OAuthAuthorizationSession:
    session_id: str
    provider: str
    state: str
    code_challenge: str
    code_challenge_method: str
    redirect_uri: str
    expires_at: float


@dataclass(frozen=True, repr=False)
class ConsumedOAuthSession:
    session_id: str
    provider: str
    tenant_id: str
    kb_id: str
    connection_id: str | None
    user_id: str
    redirect_uri: str
    code_verifier: str
    kb_epoch: int
    created_at: float
    consumed_at: float
    membership_id: str | None = None
    principal_fingerprint: str | None = None
    connection_revision: int | None = None


@dataclass(frozen=True, repr=False)
class _OAuthVerifierRecord:
    session_id: str
    credential_id: str
    tenant_id: str
    kb_id: str
    connection_id: str | None
    user_id: str


@dataclass(frozen=True, repr=False)
class OAuthAuthorizationStart:
    session_id: str
    provider: str
    authorization_url: str
    expires_at: float


@dataclass(frozen=True, repr=False)
class OAuthTokens:
    access_token: str
    refresh_token: str | None
    token_type: str
    expires_in: int | None
    scopes: tuple[str, ...]
    provider_metadata: Mapping[str, str]

    def secret_values(self, *, now: float | None = None) -> dict[str, str]:
        # Connector implementations consume the normalized `token` field,
        # independent of the provider's OAuth response spelling.
        values = {"token": self.access_token}
        if self.refresh_token:
            values["refresh_token"] = self.refresh_token
        if self.expires_in is not None:
            current = time.time() if now is None else now
            values["access_token_expires_at"] = str(current + self.expires_in)
        return values


class OAuthSessionStore:
    """One-shot OAuth state with only a digest persisted durably.

    The PKCE verifier is a short-lived encrypted vault credential. Consuming a
    state commits the one-shot marker before releasing the verifier, so crashes
    fail closed instead of permitting callback replay.
    """

    def __init__(
        self,
        db_path: str | None,
        credential_vault: CredentialVault,
        *,
        backend: DatabaseBackend | None = None,
        clock: Callable[[], float] = time.time,
        epoch_reader: Callable[[str], int] | None = None,
    ) -> None:
        if (db_path is None) == (backend is None):
            raise ValueError("exactly one of db_path or backend is required")
        self._vault = credential_vault
        self._clock = clock
        self._epoch_reader = epoch_reader or (lambda _kb_id: 0)
        self._lock = RLock()
        self._distributed = backend is not None
        self._conn: Any = (
            BackendDBAPIConnection(backend)
            if backend is not None
            else connect_sqlite(str(db_path))
        )
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS connector_oauth_sessions (
                session_id TEXT PRIMARY KEY,
                state_hash BLOB NOT NULL UNIQUE,
                verifier_credential_id TEXT NOT NULL,
                provider TEXT NOT NULL,
                tenant_id TEXT NOT NULL,
                kb_id TEXT NOT NULL,
                connection_id TEXT,
                user_id TEXT NOT NULL,
                redirect_uri TEXT NOT NULL,
                created_at REAL NOT NULL,
                expires_at REAL NOT NULL,
                consumed_at REAL,
                cancelled_at REAL,
                kb_epoch INTEGER NOT NULL DEFAULT 0,
                membership_id TEXT,
                principal_fingerprint TEXT,
                connection_revision INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_connector_oauth_sessions_expiry
                ON connector_oauth_sessions(expires_at,consumed_at,cancelled_at);
            """
        )
        if not self._distributed:
            self._ensure_column("kb_epoch", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column("membership_id", "TEXT")
            self._ensure_column("principal_fingerprint", "TEXT")
            self._ensure_column("connection_revision", "INTEGER")
        # Reconcile both ordinary expired sessions and an encrypted verifier
        # orphaned by a crash between vault creation and session insertion.
        self.purge_expired(limit=100)

    def _ensure_column(self, name: str, definition: str) -> None:
        with self._lock:
            columns = {
                str(row[1])
                for row in self._conn.execute(
                    "PRAGMA table_info(connector_oauth_sessions)"
                ).fetchall()
            }
            if name in columns:
                return
            try:
                self._conn.execute(
                    f"ALTER TABLE connector_oauth_sessions ADD COLUMN {name} {definition}"
                )
            except sqlite3.OperationalError:
                refreshed = {
                    str(row[1])
                    for row in self._conn.execute(
                        "PRAGMA table_info(connector_oauth_sessions)"
                    ).fetchall()
                }
                if name not in refreshed:
                    raise

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def check(self) -> bool:
        """Fail readiness when the one-shot authorization state is unavailable."""

        with self._lock:
            self._conn.execute(
                "SELECT 1 FROM connector_oauth_sessions LIMIT 1"
            ).fetchone()
        return True

    @property
    def credential_vault(self) -> CredentialVault:
        return self._vault

    def bind_epoch_reader(self, reader: Callable[[str], int]) -> None:
        if not callable(reader):
            raise TypeError("epoch_reader must be callable")
        self._epoch_reader = reader

    def _epoch(self, kb_id: str) -> int:
        value = self._epoch_reader(kb_id)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise OAuthSessionExpired("knowledge-base incarnation is unavailable")
        return value

    def epoch_is_current(self, kb_id: str, expected: int) -> bool:
        try:
            return self._epoch(kb_id) == expected
        except Exception:
            return False

    def create(
        self,
        *,
        provider: str,
        tenant_id: str,
        kb_id: str,
        connection_id: str | None,
        user_id: str,
        membership_id: str | None = None,
        principal_fingerprint: str | None = None,
        connection_revision: int | None = None,
        redirect_uri: str,
        ttl_seconds: int = DEFAULT_OAUTH_SESSION_TTL_SECONDS,
    ) -> OAuthAuthorizationSession:
        # Keep each request's maintenance work bounded. Failures remain visible
        # to the caller rather than silently allowing verifier rows to grow
        # without limit.
        self.purge_expired(limit=100)
        clean_provider = _provider(provider)
        tenant, kb, connection, user = _scope(tenant_id, kb_id, connection_id, user_id)
        membership = (
            _required(membership_id, "membership_id", limit=160)
            if membership_id is not None
            else None
        )
        fingerprint = (
            _required(principal_fingerprint, "principal_fingerprint", limit=160)
            if principal_fingerprint is not None
            else None
        )
        if connection_revision is not None and (
            isinstance(connection_revision, bool) or connection_revision < 1
        ):
            raise ValueError("connection_revision must be a positive integer")
        redirect = _redirect_uri(redirect_uri)
        if isinstance(ttl_seconds, bool) or not 30 <= ttl_seconds <= 1800:
            raise ValueError("OAuth session TTL must be between 30 and 1800 seconds")
        now = self._clock()
        kb_epoch = self._epoch(kb)
        expires_at = now + ttl_seconds
        session_id = f"oauth-{uuid4().hex}"
        state = _base64url(secrets.token_bytes(32))
        verifier = _base64url(secrets.token_bytes(64))
        challenge = _base64url(hashlib.sha256(verifier.encode("ascii")).digest())
        verifier_credential = self._vault.create(
            tenant_id=tenant,
            kb_id=kb,
            connection_id=connection,
            provider=clean_provider,
            credential_kind="oauth-session",
            label="OAuth authorization session",
            secret_values={"code_verifier": verifier},
            actor_id=user,
            expires_at=expires_at,
        )
        credential_id = str(verifier_credential["credential_id"])
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT INTO connector_oauth_sessions "
                    "(session_id,state_hash,verifier_credential_id,provider,tenant_id,kb_id,connection_id,"
                    "user_id,redirect_uri,created_at,expires_at,consumed_at,cancelled_at,kb_epoch,"
                    "membership_id,principal_fingerprint,connection_revision) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        session_id,
                        _state_hash(state),
                        credential_id,
                        clean_provider,
                        tenant,
                        kb,
                        connection,
                        user,
                        redirect,
                        now,
                        expires_at,
                        None,
                        None,
                        kb_epoch,
                        membership,
                        fingerprint,
                        connection_revision,
                    ),
                )
        except Exception:
            self._cleanup_verifier(
                _OAuthVerifierRecord(
                    session_id=session_id,
                    credential_id=credential_id,
                    tenant_id=tenant,
                    kb_id=kb,
                    connection_id=connection,
                    user_id=user,
                ),
                delete_session=False,
            )
            raise
        return OAuthAuthorizationSession(
            session_id=session_id,
            provider=clean_provider,
            state=state,
            code_challenge=challenge,
            code_challenge_method="S256",
            redirect_uri=redirect,
            expires_at=expires_at,
        )

    def consume(
        self,
        state: str,
        *,
        provider: str,
        tenant_id: str,
        kb_id: str,
        connection_id: str | None,
        user_id: str,
    ) -> ConsumedOAuthSession:
        clean_provider = _provider(provider)
        bound_scope = _scope(tenant_id, kb_id, connection_id, user_id)
        return self._consume_state(state, clean_provider, bound_scope=bound_scope)

    def consume_callback(self, state: str, *, provider: str) -> ConsumedOAuthSession:
        """Consume a public callback using only its unguessable state binding.

        Tenant, knowledge-base, connection, and initiating user are restored
        exclusively from the server-side session row; callback query parameters
        can therefore never override the authorization boundary.
        """

        return self._consume_state(state, _provider(provider), bound_scope=None)

    def _consume_state(
        self,
        state: str,
        clean_provider: str,
        *,
        bound_scope: tuple[str, str, str | None, str] | None,
    ) -> ConsumedOAuthSession:
        raw_state = _required(state, "state", limit=512)
        try:
            state_digest = _state_hash(raw_state)
        except UnicodeEncodeError as exc:
            raise OAuthStateMismatch("OAuth callback state is invalid") from exc
        now = self._clock()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                select = (
                    "SELECT session_id,verifier_credential_id,provider,tenant_id,kb_id,connection_id,"
                    "user_id,redirect_uri,created_at,expires_at,consumed_at,cancelled_at,kb_epoch,"
                    "membership_id,principal_fingerprint,connection_revision "
                    "FROM connector_oauth_sessions WHERE state_hash=? AND provider=?"
                )
                params: tuple[object, ...]
                if bound_scope is None:
                    params = (state_digest, clean_provider)
                else:
                    select += " AND tenant_id=? AND kb_id=? AND connection_id IS ? AND user_id=?"
                    params = (state_digest, clean_provider, *bound_scope)
                row = self._conn.execute(select, params).fetchone()
                if row is None:
                    raise OAuthStateMismatch("OAuth callback state is invalid")
                if row[10] is not None or row[11] is not None:
                    raise OAuthReplayError("OAuth callback state was already consumed")
                if float(row[9]) <= now:
                    raise OAuthSessionExpired("OAuth authorization session expired")
                if not self.epoch_is_current(str(row[4]), int(row[12])):
                    raise OAuthSessionExpired(
                        "OAuth authorization belongs to an older KB incarnation"
                    )
                updated = self._conn.execute(
                    "UPDATE connector_oauth_sessions SET consumed_at=? WHERE session_id=? "
                    "AND consumed_at IS NULL AND cancelled_at IS NULL AND expires_at>?",
                    (now, row[0], now),
                ).rowcount
                if updated != 1:
                    raise OAuthReplayError("OAuth callback state was already consumed")
                self._conn.execute("COMMIT")
            except Exception:
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")
                raise

        tenant = str(row[3])
        kb = str(row[4])
        connection = str(row[5]) if row[5] is not None else None
        user = str(row[6])
        # Do not hold the session DB write lock while the vault records use and
        # deletion; production commonly stores both tables in the same DB file.
        verifier_values = self._vault.get_for_use(
            str(row[1]),
            tenant_id=tenant,
            kb_id=kb,
            connection_id=connection,
            actor_id=user,
        )
        verifier = verifier_values.get("code_verifier", "")
        # Cleanup is retryable maintenance after the state has become one-shot.
        # A transient delete/audit failure must not strand a valid callback
        # after its verifier was already recovered successfully.
        self._cleanup_verifier(
            _OAuthVerifierRecord(
                session_id=str(row[0]),
                credential_id=str(row[1]),
                tenant_id=tenant,
                kb_id=kb,
                connection_id=connection,
                user_id=user,
            ),
            delete_session=False,
        )
        if not _PKCE_VERIFIER.fullmatch(verifier):
            raise OAuthError("OAuth PKCE verifier is invalid")
        return ConsumedOAuthSession(
            session_id=str(row[0]),
            provider=clean_provider,
            tenant_id=tenant,
            kb_id=kb,
            connection_id=connection,
            user_id=user,
            membership_id=(str(row[13]) if row[13] is not None else None),
            principal_fingerprint=(str(row[14]) if row[14] is not None else None),
            connection_revision=(int(row[15]) if row[15] is not None else None),
            redirect_uri=str(row[7]),
            code_verifier=verifier,
            kb_epoch=int(row[12]),
            created_at=float(row[8]),
            consumed_at=now,
        )

    def cancel(
        self,
        session_id: str,
        *,
        tenant_id: str,
        kb_id: str,
        connection_id: str | None,
        user_id: str,
    ) -> bool:
        tenant, kb, connection, user = _scope(tenant_id, kb_id, connection_id, user_id)
        now = self._clock()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT verifier_credential_id FROM connector_oauth_sessions WHERE session_id=? "
                    "AND tenant_id=? AND kb_id=? AND connection_id IS ? AND user_id=? AND consumed_at IS NULL "
                    "AND cancelled_at IS NULL",
                    (session_id, tenant, kb, connection, user),
                ).fetchone()
                if row is None:
                    self._conn.execute("COMMIT")
                    return False
                updated = self._conn.execute(
                    "UPDATE connector_oauth_sessions SET cancelled_at=? WHERE session_id=? "
                    "AND consumed_at IS NULL AND cancelled_at IS NULL",
                    (now, session_id),
                ).rowcount
                self._conn.execute("COMMIT")
            except Exception:
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")
                raise
        if updated == 1:
            self._cleanup_verifier(
                _OAuthVerifierRecord(
                    session_id=session_id,
                    credential_id=str(row[0]),
                    tenant_id=tenant,
                    kb_id=kb,
                    connection_id=connection,
                    user_id=user,
                ),
                delete_session=False,
            )
        return updated == 1

    def cancel_callback(self, state: str, *, provider: str) -> bool:
        """One-shot cancel a public callback using only its state binding."""

        raw_state = _required(state, "state", limit=512)
        try:
            state_digest = _state_hash(raw_state)
        except UnicodeEncodeError as exc:
            raise OAuthStateMismatch("OAuth callback state is invalid") from exc
        clean_provider = _provider(provider)
        now = self._clock()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT session_id,verifier_credential_id,tenant_id,kb_id,connection_id,user_id,"
                    "consumed_at,cancelled_at FROM connector_oauth_sessions "
                    "WHERE state_hash=? AND provider=?",
                    (state_digest, clean_provider),
                ).fetchone()
                if row is None:
                    raise OAuthStateMismatch("OAuth callback state is invalid")
                if row[6] is not None or row[7] is not None:
                    raise OAuthReplayError("OAuth callback state was already consumed")
                updated = self._conn.execute(
                    "UPDATE connector_oauth_sessions SET cancelled_at=? WHERE session_id=? "
                    "AND consumed_at IS NULL AND cancelled_at IS NULL",
                    (now, row[0]),
                ).rowcount
                if updated != 1:
                    raise OAuthReplayError("OAuth callback state was already consumed")
                self._conn.execute("COMMIT")
            except Exception:
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")
                raise
        self._cleanup_verifier(
            _OAuthVerifierRecord(
                session_id=str(row[0]),
                credential_id=str(row[1]),
                tenant_id=str(row[2]),
                kb_id=str(row[3]),
                connection_id=str(row[4]) if row[4] is not None else None,
                user_id=str(row[5]),
            ),
            delete_session=False,
        )
        return True

    def purge_expired(self, *, limit: int = 200) -> int:
        """Remove a bounded batch of terminal/expired sessions and verifiers.

        Session rows are first made terminal in their own short transaction.
        Vault work happens only after that transaction commits because the
        vault commonly uses a second SQLite connection to the same database.
        A failed vault cleanup leaves the terminal session as a retry marker.
        """

        if isinstance(limit, bool) or not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        now = self._clock()
        records: list[_OAuthVerifierRecord] = []
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                rows = self._conn.execute(
                    "SELECT session_id,verifier_credential_id,tenant_id,kb_id,connection_id,user_id,"
                    "expires_at,consumed_at,cancelled_at FROM connector_oauth_sessions "
                    "WHERE consumed_at IS NOT NULL OR cancelled_at IS NOT NULL OR expires_at<=? "
                    "ORDER BY expires_at,session_id LIMIT ?",
                    (now, limit),
                ).fetchall()
                for row in rows:
                    if row[7] is None and row[8] is None:
                        self._conn.execute(
                            "UPDATE connector_oauth_sessions SET cancelled_at=? WHERE session_id=? "
                            "AND consumed_at IS NULL AND cancelled_at IS NULL AND expires_at<=?",
                            (now, row[0], now),
                        )
                    records.append(
                        _OAuthVerifierRecord(
                            session_id=str(row[0]),
                            credential_id=str(row[1]),
                            tenant_id=str(row[2]),
                            kb_id=str(row[3]),
                            connection_id=(str(row[4]) if row[4] is not None else None),
                            user_id=str(row[5]),
                        )
                    )
                self._conn.execute("COMMIT")
            except Exception:
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")
                raise

        removed = 0
        for record in records:
            if self._cleanup_verifier(record, delete_session=True):
                removed += 1
        self._purge_orphaned_verifiers(limit=limit)
        return removed

    def _purge_orphaned_verifiers(self, *, limit: int) -> int:
        """Delete expired vault verifiers with no durable session reference."""

        removed = 0
        candidates = self._vault.expired_internal_credentials(
            expires_at=self._clock(), limit=limit
        )
        for metadata in candidates:
            credential_id = str(metadata["credential_id"])
            with self._lock:
                referenced = self._conn.execute(
                    "SELECT 1 FROM connector_oauth_sessions "
                    "WHERE verifier_credential_id=? LIMIT 1",
                    (credential_id,),
                ).fetchone()
            if referenced is not None:
                continue
            try:
                deleted = self._vault.delete(
                    credential_id,
                    tenant_id=str(metadata["tenant_id"]),
                    kb_id=str(metadata["kb_id"]),
                    connection_id=(
                        str(metadata["connection_id"])
                        if metadata.get("connection_id") is not None
                        else None
                    ),
                    actor_id="oauth-session-reconciler",
                    expected_revision=int(metadata["revision"]),
                )
            except (KeyError, CredentialRevisionConflict):
                continue
            if deleted:
                removed += 1
        return removed

    def internal_credential_ids(self, tenant_id: str, kb_id: str) -> set[str]:
        """Return verifier IDs that must never appear in public audit APIs."""

        tenant, kb, _, _ = _scope(tenant_id, kb_id, None, "oauth-internal")
        with self._lock:
            rows = self._conn.execute(
                "SELECT verifier_credential_id FROM connector_oauth_sessions "
                "WHERE tenant_id=? AND kb_id=?",
                (tenant, kb),
            ).fetchall()
        return {str(row[0]) for row in rows}

    def delete_scope(self, tenant_id: str, kb_id: str) -> int:
        """Invalidate all callback states for a deleting KB incarnation."""

        tenant, kb, _, _ = _scope(tenant_id, kb_id, None, "oauth-cleanup")
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                rows = self._conn.execute(
                    "SELECT session_id,verifier_credential_id,connection_id,user_id "
                    "FROM connector_oauth_sessions WHERE tenant_id=? AND kb_id=?",
                    (tenant, kb),
                ).fetchall()
                removed = self._conn.execute(
                    "DELETE FROM connector_oauth_sessions WHERE tenant_id=? AND kb_id=?",
                    (tenant, kb),
                ).rowcount
                self._conn.execute("COMMIT")
            except Exception:
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")
                raise
        for row in rows:
            self._cleanup_verifier(
                _OAuthVerifierRecord(
                    session_id=str(row[0]),
                    credential_id=str(row[1]),
                    tenant_id=tenant,
                    kb_id=kb,
                    connection_id=(str(row[2]) if row[2] is not None else None),
                    user_id=str(row[3]),
                ),
                delete_session=False,
            )
        return int(removed)

    def _delete_internal_audit(
        self, credential_id: str, *, tenant_id: str, kb_id: str
    ) -> None:
        self._vault.purge_internal_audit_events(
            credential_id,
            tenant_id=tenant_id,
            kb_id=kb_id,
        )

    def _cleanup_verifier(
        self,
        record: _OAuthVerifierRecord,
        *,
        delete_session: bool,
    ) -> bool:
        try:
            self._vault.delete(
                record.credential_id,
                tenant_id=record.tenant_id,
                kb_id=record.kb_id,
                connection_id=record.connection_id,
                actor_id=record.user_id,
            )
            self._delete_internal_audit(
                record.credential_id,
                tenant_id=record.tenant_id,
                kb_id=record.kb_id,
            )
        except Exception:
            return False
        if not delete_session:
            return True
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    "DELETE FROM connector_oauth_sessions WHERE session_id=? "
                    "AND (consumed_at IS NOT NULL OR cancelled_at IS NOT NULL OR expires_at<=?)",
                    (record.session_id, self._clock()),
                )
                self._conn.execute("COMMIT")
            except Exception:
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")
                return False
        return True


class OAuthProviderAdapter:
    provider: str
    authorization_endpoint: str
    token_endpoint: str
    supports_pkce = False

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str | None,
        redirect_uri: str,
        scopes: tuple[str, ...] | list[str],
        transport: OAuthTransport | None,
        allowed_host: str,
    ) -> None:
        self.client_id = _required(client_id, "client_id", limit=512)
        self.client_secret = (
            _required(client_secret, "client_secret", limit=4096)
            if client_secret is not None
            else None
        )
        self.redirect_uri = _redirect_uri(redirect_uri)
        self.scopes = _scopes(scopes)
        self.transport = transport or HttpTransport(
            allowed_hosts={allowed_host},
            timeout_seconds=OAUTH_TIMEOUT_SECONDS,
            max_response_bytes=MAX_OAUTH_RESPONSE_BYTES,
        )

    def authorization_url(self, session: OAuthAuthorizationSession) -> str:
        self._validate_session(session)
        values = self._authorization_parameters(session)
        if self.supports_pkce:
            values["code_challenge"] = session.code_challenge
            values["code_challenge_method"] = session.code_challenge_method
        return self.authorization_endpoint + "?" + urlencode(values)

    def exchange_code(self, code: str, code_verifier: str) -> OAuthTokens:
        raise NotImplementedError

    def refresh(self, refresh_token: str) -> OAuthTokens:
        raise NotImplementedError

    def _authorization_parameters(
        self, session: OAuthAuthorizationSession
    ) -> dict[str, str]:
        raise NotImplementedError

    def _validate_session(self, session: OAuthAuthorizationSession) -> None:
        if (
            session.provider != self.provider
            or session.redirect_uri != self.redirect_uri
        ):
            raise ValueError("OAuth session does not match the provider adapter")
        if session.code_challenge_method != "S256" or not session.code_challenge:
            raise ValueError("OAuth session requires an S256 PKCE challenge")

    def _token_request(
        self,
        *,
        body: bytes,
        content_type: str,
        headers: Mapping[str, str] | None = None,
    ) -> OAuthTokens:
        if len(body) > MAX_OAUTH_RESPONSE_BYTES:
            raise ValueError("OAuth token request exceeds the byte limit")
        request_headers = {
            "Accept": "application/json",
            "Content-Type": content_type,
            **dict(headers or {}),
        }
        try:
            response = self.transport.request(
                "POST",
                self.token_endpoint,
                headers=request_headers,
                body=body,
            )
        except Exception as exc:
            if isinstance(exc, OAuthError):
                raise
            raise OAuthProviderError("OAuth provider token request failed") from exc
        if response.status < 200 or response.status >= 300:
            raise OAuthProviderError(
                f"OAuth provider token request failed with HTTP {response.status}"
            )
        if len(response.body) > MAX_OAUTH_RESPONSE_BYTES:
            raise OAuthProviderError("OAuth provider response exceeds the byte limit")
        try:
            payload = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise OAuthProviderError("OAuth provider returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise OAuthProviderError("OAuth provider token response must be an object")
        return self._tokens(payload)

    def _tokens(self, payload: Mapping[str, Any]) -> OAuthTokens:
        access_token = payload.get("access_token")
        refresh_token = payload.get("refresh_token")
        token_type = payload.get("token_type", "Bearer")
        if (
            not isinstance(access_token, str)
            or not access_token
            or len(access_token) > 65_536
        ):
            raise OAuthProviderError(
                "OAuth provider response has no valid access token"
            )
        if refresh_token is not None and (
            not isinstance(refresh_token, str)
            or not refresh_token
            or len(refresh_token) > 65_536
        ):
            raise OAuthProviderError("OAuth provider returned an invalid refresh token")
        if not isinstance(token_type, str) or not token_type or len(token_type) > 64:
            raise OAuthProviderError("OAuth provider returned an invalid token type")
        expires_in = payload.get("expires_in")
        if expires_in is not None and (
            isinstance(expires_in, bool)
            or not isinstance(expires_in, int)
            or expires_in <= 0
            or expires_in > 31_536_000
        ):
            raise OAuthProviderError(
                "OAuth provider returned an invalid token lifetime"
            )
        raw_scope = payload.get("scope")
        if raw_scope is not None and not isinstance(raw_scope, str):
            raise OAuthProviderError("OAuth provider returned invalid scopes")
        granted_scopes = tuple(raw_scope.split()) if raw_scope else self.scopes
        metadata: dict[str, str] = {}
        for field in ("workspace_id", "workspace_name", "bot_id"):
            value = payload.get(field)
            if isinstance(value, str) and value and len(value) <= 512:
                metadata[field] = value
        return OAuthTokens(
            access_token=access_token,
            refresh_token=refresh_token,
            token_type=token_type,
            expires_in=expires_in,
            scopes=_scopes(list(granted_scopes)),
            provider_metadata=metadata,
        )


class NotionOAuthAdapter(OAuthProviderAdapter):
    """Notion public-connection OAuth as documented for the REST API."""

    provider = "notion"
    authorization_endpoint = "https://api.notion.com/v1/oauth/authorize"
    token_endpoint = "https://api.notion.com/v1/oauth/token"

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        transport: OAuthTransport | None = None,
    ) -> None:
        super().__init__(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri,
            scopes=(),
            transport=transport,
            allowed_host="api.notion.com",
        )

    def _authorization_parameters(
        self, session: OAuthAuthorizationSession
    ) -> dict[str, str]:
        return {
            "owner": "user",
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "state": session.state,
        }

    def exchange_code(self, code: str, code_verifier: str) -> OAuthTokens:
        del code_verifier  # Notion's public-connection flow does not document PKCE.
        body = json.dumps(
            {
                "grant_type": "authorization_code",
                "code": _required(code, "authorization code", limit=8192),
                "redirect_uri": self.redirect_uri,
            },
            separators=(",", ":"),
        ).encode("utf-8")
        return self._notion_request(body)

    def refresh(self, refresh_token: str) -> OAuthTokens:
        body = json.dumps(
            {
                "grant_type": "refresh_token",
                "refresh_token": _required(
                    refresh_token, "refresh_token", limit=65_536
                ),
            },
            separators=(",", ":"),
        ).encode("utf-8")
        return self._notion_request(body)

    def _notion_request(self, body: bytes) -> OAuthTokens:
        if self.client_secret is None:  # pragma: no cover - constructor requires it
            raise OAuthError("Notion OAuth requires a client secret")
        basic = base64.b64encode(
            f"{self.client_id}:{self.client_secret}".encode("utf-8")
        ).decode("ascii")
        return self._token_request(
            body=body,
            content_type="application/json",
            headers={
                "Authorization": f"Basic {basic}",
                "Notion-Version": NOTION_API_VERSION,
            },
        )


class AtlassianOAuthAdapter(OAuthProviderAdapter):
    """Atlassian Cloud OAuth 2.0 (3LO) adapter."""

    provider = "atlassian"
    authorization_endpoint = "https://auth.atlassian.com/authorize"
    token_endpoint = "https://auth.atlassian.com/oauth/token"
    accessible_resources_endpoint = (
        "https://api.atlassian.com/oauth/token/accessible-resources"
    )

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        scopes: tuple[str, ...] | list[str],
        transport: OAuthTransport | None = None,
    ) -> None:
        clean_scopes = _scopes(scopes)
        if not clean_scopes:
            raise ValueError("Atlassian OAuth requires at least one scope")
        # Atlassian's token and resource-discovery calls deliberately use two
        # fixed hosts.  A default transport restricted to only the token host
        # made every real (non-injected) flow fail at accessible-resources.
        resolved_transport = transport or HttpTransport(
            allowed_hosts={"auth.atlassian.com", "api.atlassian.com"},
            timeout_seconds=OAUTH_TIMEOUT_SECONDS,
            max_response_bytes=MAX_OAUTH_RESPONSE_BYTES,
        )
        super().__init__(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri,
            scopes=clean_scopes,
            transport=resolved_transport,
            allowed_host="auth.atlassian.com",
        )

    def _authorization_parameters(
        self, session: OAuthAuthorizationSession
    ) -> dict[str, str]:
        return {
            "client_id": self.client_id,
            "audience": "api.atlassian.com",
            "scope": " ".join(self.scopes),
            "redirect_uri": self.redirect_uri,
            "state": session.state,
            "response_type": "code",
            "prompt": "consent",
        }

    def exchange_code(self, code: str, code_verifier: str) -> OAuthTokens:
        del code_verifier  # Atlassian's current 3LO guide does not document PKCE.
        return self._atlassian_request(
            {
                "grant_type": "authorization_code",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "code": _required(code, "authorization code", limit=8192),
                "redirect_uri": self.redirect_uri,
            }
        )

    def refresh(self, refresh_token: str) -> OAuthTokens:
        return self._atlassian_request(
            {
                "grant_type": "refresh_token",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "refresh_token": _required(
                    refresh_token, "refresh_token", limit=65_536
                ),
            }
        )

    def _atlassian_request(self, values: Mapping[str, Any]) -> OAuthTokens:
        body = json.dumps(values, separators=(",", ":")).encode("utf-8")
        tokens = self._token_request(body=body, content_type="application/json")
        try:
            response = self.transport.request(
                "GET",
                self.accessible_resources_endpoint,
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {tokens.access_token}",
                },
            )
        except Exception as exc:
            raise OAuthProviderError(
                "Atlassian accessible-resources request failed"
            ) from exc
        if response.status < 200 or response.status >= 300:
            raise OAuthProviderError(
                "Atlassian accessible-resources request was rejected"
            )
        if len(response.body) > MAX_OAUTH_RESPONSE_BYTES:
            raise OAuthProviderError(
                "Atlassian accessible-resources response exceeds the byte limit"
            )
        try:
            payload = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise OAuthProviderError(
                "Atlassian accessible-resources returned invalid JSON"
            ) from exc
        if not isinstance(payload, list) or len(payload) > 100:
            raise OAuthProviderError(
                "Atlassian accessible-resources response is invalid"
            )
        resources: list[dict[str, Any]] = []
        for item in payload:
            if not isinstance(item, dict):
                raise OAuthProviderError("Atlassian resource entry is invalid")
            cloud_id = item.get("id")
            site_url = item.get("url")
            scopes = item.get("scopes")
            if (
                not isinstance(cloud_id, str)
                or not cloud_id
                or len(cloud_id) > 160
                or not isinstance(site_url, str)
                or len(site_url) > 2048
                or not isinstance(scopes, list)
                or any(not isinstance(scope, str) for scope in scopes)
            ):
                raise OAuthProviderError("Atlassian resource entry is invalid")
            parts = urlsplit(site_url)
            host = str(parts.hostname or "").casefold()
            if (
                parts.scheme != "https"
                or not host.endswith(".atlassian.net")
                or parts.username is not None
                or parts.password is not None
                or parts.query
                or parts.fragment
            ):
                raise OAuthProviderError("Atlassian resource URL is invalid")
            if not any("confluence" in scope.casefold() for scope in scopes):
                continue
            resources.append(
                {
                    "cloud_id": cloud_id,
                    "site_url": site_url.rstrip("/"),
                    "scopes": list(dict.fromkeys(scopes)),
                }
            )
        if not resources:
            raise OAuthProviderError("Atlassian token has no Confluence resource")
        metadata = dict(tokens.provider_metadata)
        metadata["accessible_resources"] = json.dumps(
            resources, sort_keys=True, separators=(",", ":")
        )
        return OAuthTokens(
            access_token=tokens.access_token,
            refresh_token=tokens.refresh_token,
            token_type=tokens.token_type,
            expires_in=tokens.expires_in,
            scopes=tokens.scopes,
            provider_metadata=metadata,
        )


class MicrosoftOAuthAdapter(OAuthProviderAdapter):
    """Microsoft identity platform v2 authorization-code flow with PKCE."""

    provider = "microsoft"
    supports_pkce = True

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str | None,
        redirect_uri: str,
        scopes: tuple[str, ...] | list[str],
        tenant: str = "organizations",
        transport: OAuthTransport | None = None,
    ) -> None:
        clean_tenant = _required(tenant, "Microsoft tenant", limit=253)
        if (
            not _MICROSOFT_TENANT.fullmatch(clean_tenant)
            or ".." in clean_tenant
            or clean_tenant.startswith(".")
        ):
            raise ValueError("Microsoft tenant is invalid")
        clean_scopes = _scopes(scopes)
        if not clean_scopes:
            raise ValueError("Microsoft OAuth requires at least one scope")
        self.tenant = clean_tenant
        self.authorization_endpoint = (
            f"https://login.microsoftonline.com/{clean_tenant}/oauth2/v2.0/authorize"
        )
        self.token_endpoint = (
            f"https://login.microsoftonline.com/{clean_tenant}/oauth2/v2.0/token"
        )
        super().__init__(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri,
            scopes=clean_scopes,
            transport=transport,
            allowed_host="login.microsoftonline.com",
        )

    def _authorization_parameters(
        self, session: OAuthAuthorizationSession
    ) -> dict[str, str]:
        return {
            "client_id": self.client_id,
            "response_type": "code",
            "redirect_uri": self.redirect_uri,
            "response_mode": "query",
            "scope": " ".join(self.scopes),
            "state": session.state,
        }

    def exchange_code(self, code: str, code_verifier: str) -> OAuthTokens:
        verifier = _required(code_verifier, "code_verifier", limit=128)
        if not _PKCE_VERIFIER.fullmatch(verifier):
            raise ValueError("code_verifier is invalid")
        values = {
            "client_id": self.client_id,
            "scope": " ".join(self.scopes),
            "code": _required(code, "authorization code", limit=8192),
            "redirect_uri": self.redirect_uri,
            "grant_type": "authorization_code",
            "code_verifier": verifier,
        }
        if self.client_secret is not None:
            values["client_secret"] = self.client_secret
        return self._microsoft_request(values)

    def refresh(self, refresh_token: str) -> OAuthTokens:
        values = {
            "client_id": self.client_id,
            "scope": " ".join(self.scopes),
            "refresh_token": _required(refresh_token, "refresh_token", limit=65_536),
            "grant_type": "refresh_token",
        }
        if self.client_secret is not None:
            values["client_secret"] = self.client_secret
        return self._microsoft_request(values)

    def _microsoft_request(self, values: Mapping[str, str]) -> OAuthTokens:
        body = urlencode(values).encode("ascii")
        return self._token_request(
            body=body, content_type="application/x-www-form-urlencoded"
        )


class OAuthCoordinator:
    """Coordinates one-shot state, provider exchange, and encrypted token storage."""

    def __init__(
        self,
        session_store: OAuthSessionStore,
        credential_vault: CredentialVault,
        adapters: Mapping[str, OAuthProviderAdapter],
        *,
        clock: Callable[[], float] = time.time,
        connection_reader: Callable[[str], Mapping[str, Any] | None] | None = None,
        authorization_checker: Callable[[ConsumedOAuthSession], bool] | None = None,
    ) -> None:
        if session_store.credential_vault is not credential_vault:
            raise ValueError("OAuth session store and coordinator must share a vault")
        self._sessions = session_store
        self._vault = credential_vault
        self._clock = clock
        self._connection_reader = connection_reader
        self._authorization_checker = authorization_checker
        self._adapters = {_provider(key): value for key, value in adapters.items()}
        if any(key != adapter.provider for key, adapter in self._adapters.items()):
            raise ValueError("OAuth adapter mapping key does not match provider")

    @property
    def credential_vault(self) -> CredentialVault:
        return self._vault

    @property
    def session_store(self) -> OAuthSessionStore:
        return self._sessions

    @property
    def redirect_uris(self) -> dict[str, str]:
        return {
            provider: adapter.redirect_uri
            for provider, adapter in self._adapters.items()
        }

    def bind_authorization_checker(
        self, checker: Callable[[ConsumedOAuthSession], bool]
    ) -> None:
        if not callable(checker):
            raise TypeError("authorization checker must be callable")
        self._authorization_checker = checker

    def begin(
        self,
        *,
        provider: str,
        tenant_id: str,
        kb_id: str,
        connection_id: str | None,
        user_id: str,
        membership_id: str | None = None,
        principal_fingerprint: str | None = None,
        connection_revision: int | None = None,
        ttl_seconds: int = DEFAULT_OAUTH_SESSION_TTL_SECONDS,
    ) -> OAuthAuthorizationStart:
        adapter = self._adapter(provider)
        session = self._sessions.create(
            provider=adapter.provider,
            tenant_id=tenant_id,
            kb_id=kb_id,
            connection_id=connection_id,
            user_id=user_id,
            membership_id=membership_id,
            principal_fingerprint=principal_fingerprint,
            connection_revision=connection_revision,
            redirect_uri=adapter.redirect_uri,
            ttl_seconds=ttl_seconds,
        )
        return OAuthAuthorizationStart(
            session_id=session.session_id,
            provider=adapter.provider,
            authorization_url=adapter.authorization_url(session),
            expires_at=session.expires_at,
        )

    def complete(
        self,
        *,
        provider: str,
        state: str,
        code: str,
        tenant_id: str,
        kb_id: str,
        connection_id: str | None,
        user_id: str,
        label: str,
    ) -> dict[str, Any]:
        adapter = self._adapter(provider)
        session = self._sessions.consume(
            state,
            provider=adapter.provider,
            tenant_id=tenant_id,
            kb_id=kb_id,
            connection_id=connection_id,
            user_id=user_id,
        )
        return self._exchange_and_store(adapter, session, code=code, label=label)

    def complete_callback(
        self,
        *,
        provider: str,
        state: str,
        code: str,
        label: str = "OAuth credential",
        defer_activation: bool = False,
    ) -> dict[str, Any]:
        """Complete a public callback from server-side session bindings only."""

        if type(defer_activation) is not bool:
            raise TypeError("defer_activation must be a boolean")

        adapter = self._adapter(provider)
        session = self._sessions.consume_callback(
            state,
            provider=adapter.provider,
        )
        return self._exchange_and_store(
            adapter,
            session,
            code=code,
            label=label,
            defer_activation=defer_activation,
        )

    def cancel_callback(self, *, provider: str, state: str) -> bool:
        adapter = self._adapter(provider)
        return self._sessions.cancel_callback(state, provider=adapter.provider)

    def _exchange_and_store(
        self,
        adapter: OAuthProviderAdapter,
        session: ConsumedOAuthSession,
        *,
        code: str,
        label: str,
        defer_activation: bool = False,
    ) -> dict[str, Any]:
        if not self._authority_is_current(session):
            raise OAuthSessionExpired("OAuth authorization is no longer valid")
        tokens = adapter.exchange_code(
            _required(code, "authorization code", limit=8192),
            session.code_verifier,
        )
        if not self._authority_is_current(session):
            raise OAuthSessionExpired("OAuth authority changed during token exchange")
        subject = next(
            (
                tokens.provider_metadata[key]
                for key in ("workspace_id", "bot_id")
                if key in tokens.provider_metadata
            ),
            None,
        )
        secret_values = tokens.secret_values(now=self._clock())
        if adapter.provider == "atlassian":
            resource = self._select_atlassian_resource(
                tokens,
                tenant_id=session.tenant_id,
                kb_id=session.kb_id,
                connection_id=session.connection_id,
            )
            secret_values.update(resource)
            subject = resource["cloud_id"]
        metadata = self._vault.create(
            tenant_id=session.tenant_id,
            kb_id=session.kb_id,
            connection_id=session.connection_id,
            provider=adapter.provider,
            credential_kind="oauth",
            label=label,
            subject=subject,
            scopes=list(tokens.scopes),
            secret_values=secret_values,
            actor_id=session.user_id,
            # A provider token is never public or decryptable until the API
            # callback has revalidated live authority and, when applicable,
            # bound it to the frozen connection revision.
            pending_activation=True,
        )
        if not self._authority_is_current(session):
            self._discard_pending_credential(metadata, session)
            raise OAuthSessionExpired(
                "OAuth authority changed while storing credentials"
            )
        if not defer_activation:
            metadata = self._vault.activate(
                str(metadata["credential_id"]),
                tenant_id=session.tenant_id,
                kb_id=session.kb_id,
                connection_id=session.connection_id,
                actor_id=session.user_id,
                expected_revision=int(metadata["revision"]),
            )
        # The epoch is transient callback evidence, not public credential
        # metadata and not part of the encrypted-vault schema.
        return {
            **metadata,
            "_kb_epoch": session.kb_epoch,
            "_membership_id": session.membership_id,
            "_principal_fingerprint": session.principal_fingerprint,
            "_connection_revision": session.connection_revision,
        }

    def _discard_pending_credential(
        self,
        metadata: Mapping[str, Any],
        session: ConsumedOAuthSession,
    ) -> None:
        """Best-effort deletion with a durable fail-closed fallback."""

        credential_id = str(metadata["credential_id"])
        revision = int(metadata["revision"])
        try:
            self._vault.delete(
                credential_id,
                tenant_id=session.tenant_id,
                kb_id=session.kb_id,
                connection_id=session.connection_id,
                actor_id=session.user_id,
                expected_revision=revision,
            )
            return
        except Exception:
            # The row was created pending, so it is already unusable. Persist
            # an explicit quarantine when the destructive cleanup path itself
            # fails; bounded maintenance removes it later.
            try:
                self._vault.quarantine(
                    credential_id,
                    tenant_id=session.tenant_id,
                    kb_id=session.kb_id,
                    connection_id=session.connection_id,
                    actor_id=session.user_id,
                    expected_revision=revision,
                )
            except Exception:
                pass

    def _authority_is_current(self, session: ConsumedOAuthSession) -> bool:
        if not self._sessions.epoch_is_current(session.kb_id, session.kb_epoch):
            return False
        checker = self._authorization_checker
        if checker is None:
            return True
        try:
            return checker(session) is True
        except Exception:
            return False

    def refresh_credential(
        self,
        credential_id: str,
        *,
        tenant_id: str,
        kb_id: str,
        connection_id: str | None,
        user_id: str,
        expected_revision: int | None = None,
        kb_epoch: int | None = None,
        authority_checker: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        def require_authority() -> None:
            if authority_checker is None:
                return
            try:
                authorized = authority_checker() is True
            except Exception:
                authorized = False
            if not authorized:
                raise OAuthSessionExpired("OAuth credential refresh authority changed")

        if kb_epoch is not None and not self._sessions.epoch_is_current(
            kb_id, kb_epoch
        ):
            raise OAuthSessionExpired(
                "knowledge base changed before OAuth credential refresh"
            )
        metadata = self._vault.get_metadata(
            credential_id, tenant_id=tenant_id, kb_id=kb_id
        )
        if (
            metadata is None
            or metadata["connection_id"] != connection_id
            or metadata["credential_kind"] != "oauth"
        ):
            raise KeyError(credential_id)
        metadata_revision = int(metadata["revision"])
        if expected_revision is not None:
            if isinstance(expected_revision, bool) or expected_revision < 1:
                raise ValueError("expected_revision must be a positive integer")
            if expected_revision != metadata_revision:
                raise CredentialRevisionConflict("credential revision has changed")
        require_authority()
        adapter = self._adapter(str(metadata["provider"]))
        current = self._vault.get_for_use(
            credential_id,
            tenant_id=tenant_id,
            kb_id=kb_id,
            connection_id=connection_id,
            actor_id=user_id,
        )
        refresh_token = current.get("refresh_token")
        if not refresh_token:
            raise OAuthError("OAuth credential cannot be refreshed")
        tokens = adapter.refresh(refresh_token)
        if kb_epoch is not None and not self._sessions.epoch_is_current(
            kb_id, kb_epoch
        ):
            raise OAuthSessionExpired(
                "knowledge base changed during OAuth credential refresh"
            )
        next_values = tokens.secret_values(now=self._clock())
        if adapter.provider == "atlassian":
            next_values.update(
                self._select_atlassian_resource(
                    tokens,
                    tenant_id=tenant_id,
                    kb_id=kb_id,
                    connection_id=connection_id,
                    expected_site_url=current.get("site_url"),
                    expected_cloud_id=current.get("cloud_id"),
                )
            )
        if "refresh_token" not in next_values:
            next_values["refresh_token"] = refresh_token
        # The provider call intentionally runs without an application-wide
        # reference lock. Revalidate the initiating principal, KB incarnation
        # and bound connection immediately before the optimistic vault CAS.
        require_authority()
        rotated = self._vault.rotate(
            credential_id,
            tenant_id=tenant_id,
            kb_id=kb_id,
            connection_id=connection_id,
            actor_id=user_id,
            secret_values=next_values,
            # Even callers that omit a revision participate in optimistic
            # concurrency. The provider exchange may take seconds, so rotating
            # unconditionally here would let a slower refresh overwrite a
            # newer token response.
            expected_revision=metadata_revision,
        )
        if kb_epoch is not None and not self._sessions.epoch_is_current(
            kb_id, kb_epoch
        ):
            raise OAuthSessionExpired(
                "knowledge base changed while storing refreshed credentials"
            )
        return rotated

    def _select_atlassian_resource(
        self,
        tokens: OAuthTokens,
        *,
        tenant_id: str,
        kb_id: str,
        connection_id: str | None,
        expected_site_url: str | None = None,
        expected_cloud_id: str | None = None,
    ) -> dict[str, str]:
        raw_resources = tokens.provider_metadata.get("accessible_resources")
        try:
            resources = json.loads(str(raw_resources or ""))
        except json.JSONDecodeError as exc:
            raise OAuthProviderError(
                "Atlassian accessible resources are unavailable"
            ) from exc
        if not isinstance(resources, list):
            raise OAuthProviderError("Atlassian accessible resources are invalid")
        expected_site = str(expected_site_url or "").rstrip("/") or None
        if expected_site is None and connection_id is not None:
            if self._connection_reader is None:
                raise OAuthProviderError(
                    "Atlassian connection binding cannot be verified"
                )
            connection = self._connection_reader(connection_id)
            if (
                connection is None
                or connection.get("tenant_id") != tenant_id
                or connection.get("kb_id") != kb_id
                or connection.get("connector_type") != "confluence"
                or not isinstance(connection.get("config"), Mapping)
            ):
                raise OAuthProviderError("Atlassian connection binding is invalid")
            expected_site = str(connection["config"].get("base_url") or "").rstrip("/")
        candidates = [
            resource
            for resource in resources
            if isinstance(resource, dict)
            and isinstance(resource.get("cloud_id"), str)
            and isinstance(resource.get("site_url"), str)
            and (
                expected_site is None
                or resource["site_url"].rstrip("/") == expected_site
            )
            and (expected_cloud_id is None or resource["cloud_id"] == expected_cloud_id)
        ]
        if len(candidates) != 1:
            raise OAuthProviderError(
                "Atlassian authorization must resolve exactly one configured site"
            )
        return {
            "cloud_id": str(candidates[0]["cloud_id"]),
            "site_url": str(candidates[0]["site_url"]).rstrip("/"),
        }

    def _adapter(self, provider: str) -> OAuthProviderAdapter:
        clean_provider = _provider(provider)
        try:
            return self._adapters[clean_provider]
        except KeyError as exc:
            raise ValueError("OAuth provider is not configured") from exc
