"""Durable local identities, workspaces, memberships, sessions, and invites.

Secrets have deliberately narrow lifetimes: password hashes are versioned
scrypt envelopes, while session and invitation tokens are returned once and
only their SHA-256 digests are persisted.
"""

from __future__ import annotations

import base64
import binascii
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import hmac
import json
import math
import secrets
import sqlite3
from threading import RLock
import time
import unicodedata
from typing import Any, Callable, Iterator, Mapping, Sequence

from cogdoc.api.tenancy import Permission, Principal, ROLE_PERMISSIONS, Role


AUTH_SCHEMA_VERSION = "8"
MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 256
SESSION_TOUCH_INTERVAL_SECONDS = 60.0
_ROLES = frozenset(role.value for role in Role)
_ASSIGNABLE_ROLES = _ROLES - {Role.OWNER.value}
_SCIM_ROLE_RANK = {"viewer": 0, "reviewer": 1, "editor": 2, "admin": 3}
MAX_OIDC_GROUP_MAPPINGS = 100
MAX_OIDC_GROUPS_PER_CLAIM = 200
MAX_OIDC_GROUP_CLAIMS_BYTES = 32 * 1024
_OIDC_RESERVED_GROUP_CLAIMS = frozenset(
    {
        "iss",
        "sub",
        "aud",
        "azp",
        "exp",
        "iat",
        "nbf",
        "nonce",
        "email",
        "email_verified",
        "name",
        "preferred_username",
    }
)


class AuthStoreError(RuntimeError):
    """Base class for identity-store failures."""


class AuthValidationError(AuthStoreError, ValueError):
    """Input did not satisfy the store's strict contract."""


class AuthAuthenticationError(AuthStoreError):
    """Credentials or an opaque token could not be authenticated."""


class AuthLockedError(AuthAuthenticationError):
    """Password login is temporarily locked after repeated failures."""


class AuthAuthorizationError(AuthStoreError, PermissionError):
    """The acting user cannot perform the requested workspace operation."""


class AuthNotFoundError(AuthStoreError, LookupError):
    """The requested safe identity record does not exist."""


class AuthConflictError(AuthStoreError):
    """A uniqueness, revision, or lifecycle invariant was violated."""


class AuthInviteError(AuthStoreError):
    """An invitation is invalid, expired, revoked, or already consumed."""


@dataclass(frozen=True, slots=True)
class ScryptParams:
    """Version-one password hashing parameters.

    The production defaults use roughly 128 MiB for scrypt's ROMix at N=2**17.
    """

    n: int = 1 << 17
    r: int = 8
    p: int = 1
    salt_bytes: int = 16
    dklen: int = 32

    def validate(self) -> "ScryptParams":
        if type(self.n) is not int or self.n < 1 << 10 or self.n > 1 << 20:
            raise AuthValidationError("scrypt n must be between 2**10 and 2**20")
        if self.n & (self.n - 1):
            raise AuthValidationError("scrypt n must be a power of two")
        if type(self.r) is not int or not 1 <= self.r <= 32:
            raise AuthValidationError("scrypt r must be between 1 and 32")
        if type(self.p) is not int or not 1 <= self.p <= 16:
            raise AuthValidationError("scrypt p must be between 1 and 16")
        if type(self.salt_bytes) is not int or not 16 <= self.salt_bytes <= 64:
            raise AuthValidationError("scrypt salt must be between 16 and 64 bytes")
        if self.dklen != 32:
            raise AuthValidationError("scrypt dklen must be 32 bytes")
        return self


@dataclass(frozen=True, slots=True)
class AuthContext:
    """Safe authenticated state for one user in one target workspace."""

    user: dict[str, Any]
    workspace: dict[str, Any]
    session: dict[str, Any]
    principal: Principal

    @property
    def user_id(self) -> str:
        return str(self.user["user_id"])

    @property
    def workspace_id(self) -> str:
        return str(self.workspace["workspace_id"])


@dataclass(frozen=True, slots=True)
class ServiceAccountAuthContext:
    """Authenticated non-human principal with no user session capabilities."""

    service_account: dict[str, Any]
    token: dict[str, Any]
    workspace: dict[str, Any]
    principal: Principal

    @property
    def workspace_id(self) -> str:
        return str(self.workspace["workspace_id"])


def normalize_email(email: str) -> str:
    if type(email) is not str:
        raise AuthValidationError("email must be a string")
    normalized = unicodedata.normalize("NFKC", email).strip().casefold()
    if (
        not normalized
        or len(normalized) > 320
        or normalized.count("@") != 1
        or any(character.isspace() for character in normalized)
        or any(ord(character) < 32 or ord(character) == 127 for character in normalized)
    ):
        raise AuthValidationError("invalid email address")
    local, domain = normalized.split("@", 1)
    if not local or not domain or domain.startswith(".") or domain.endswith("."):
        raise AuthValidationError("invalid email address")
    return normalized


def _clean_text(value: str, *, field: str, maximum: int = 120) -> str:
    if type(value) is not str:
        raise AuthValidationError(f"{field} must be a string")
    normalized = unicodedata.normalize("NFKC", " ".join(value.split()))
    if not normalized or len(normalized) > maximum:
        raise AuthValidationError(f"{field} must contain 1 to {maximum} characters")
    if any(ord(character) < 32 or ord(character) == 127 for character in normalized):
        raise AuthValidationError(f"{field} must not contain control characters")
    return normalized


def _clean_id(value: str, *, field: str) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or len(value) > 160
    ):
        raise AuthValidationError(f"invalid {field}")
    if any(ord(character) < 33 or ord(character) == 127 for character in value):
        raise AuthValidationError(f"invalid {field}")
    return value


def _clean_password(password: str, *, enforce_minimum: bool = True) -> str:
    if type(password) is not str:
        raise AuthValidationError("password must be a string")
    minimum = MIN_PASSWORD_LENGTH if enforce_minimum else 1
    if not minimum <= len(password) <= MAX_PASSWORD_LENGTH:
        raise AuthValidationError(
            f"password must contain {minimum} to {MAX_PASSWORD_LENGTH} characters"
        )
    if password.isspace() or "\x00" in password:
        raise AuthValidationError("password is invalid")
    return password


def _clean_role(role: Role | str, *, allow_owner: bool = False) -> Role:
    try:
        normalized = role if isinstance(role, Role) else Role(role)
    except (TypeError, ValueError) as exc:
        raise AuthValidationError("invalid workspace role") from exc
    if normalized.value not in (_ROLES if allow_owner else _ASSIGNABLE_ROLES):
        raise AuthValidationError("owner role requires an ownership transfer")
    return normalized


def _urlsafe_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _urlsafe_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.b64decode(value + padding, altchars=b"-_", validate=True)


def _scrypt_maxmem(params: ScryptParams) -> int:
    # OpenSSL rejects the operation unless maxmem is strictly above its working
    # allocation. Keep the bound derived from already validated parameters.
    return min((1 << 31) - 1, max(32 << 20, 256 * params.n * params.r * params.p))


def _hash_password(password: str, params: ScryptParams) -> str:
    clean = _clean_password(password)
    salt = secrets.token_bytes(params.salt_bytes)
    derived = hashlib.scrypt(
        clean.encode("utf-8"),
        salt=salt,
        n=params.n,
        r=params.r,
        p=params.p,
        dklen=params.dklen,
        maxmem=_scrypt_maxmem(params),
    )
    return (
        f"scrypt$v=1$n={params.n}$r={params.r}$p={params.p}$dklen={params.dklen}"
        f"${_urlsafe_encode(salt)}${_urlsafe_encode(derived)}"
    )


def _parse_password_hash(encoded: str) -> tuple[ScryptParams, bytes, bytes]:
    try:
        algorithm, version, raw_n, raw_r, raw_p, raw_dklen, raw_salt, raw_digest = (
            encoded.split("$")
        )
        if algorithm != "scrypt" or version != "v=1":
            raise ValueError
        params = ScryptParams(
            n=int(raw_n.removeprefix("n=")),
            r=int(raw_r.removeprefix("r=")),
            p=int(raw_p.removeprefix("p=")),
            dklen=int(raw_dklen.removeprefix("dklen=")),
            salt_bytes=16,
        ).validate()
        salt = _urlsafe_decode(raw_salt)
        digest = _urlsafe_decode(raw_digest)
        if not 16 <= len(salt) <= 64 or len(digest) != params.dklen:
            raise ValueError
        return params, salt, digest
    except (AttributeError, TypeError, ValueError, binascii.Error) as exc:
        raise AuthStoreError("stored password hash is invalid") from exc


def _verify_password(password: str, encoded: str) -> bool:
    params, salt, expected = _parse_password_hash(encoded)
    try:
        candidate = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=params.n,
            r=params.r,
            p=params.p,
            dklen=params.dklen,
            maxmem=_scrypt_maxmem(params),
        )
    except (AttributeError, UnicodeError, ValueError):
        return False
    return hmac.compare_digest(candidate, expected)


def _token_hash(token: str) -> str:
    if type(token) is not str or not token or token != token.strip():
        raise AuthAuthenticationError("invalid authentication token")
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(18)}"


def _new_token(prefix: str) -> str:
    # token_urlsafe(32) carries exactly 256 random bits before encoding.
    return f"{prefix}_{secrets.token_urlsafe(32)}"


def _iso(timestamp: float) -> str:
    from datetime import datetime, timezone

    return (
        datetime.fromtimestamp(timestamp, timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


class AuthStore:
    """Thread-safe SQLite identity store intended for CogDoc's single process."""

    def __init__(
        self,
        db_path: str,
        *,
        scrypt_params: ScryptParams | Mapping[str, int] | None = None,
        scrypt_n: int | None = None,
        scrypt_r: int | None = None,
        scrypt_p: int | None = None,
        session_ttl_seconds: float = 30 * 24 * 60 * 60,
        invite_ttl_seconds: float = 7 * 24 * 60 * 60,
        max_failed_logins: int = 5,
        lockout_seconds: float = 15 * 60,
        clock: Callable[[], float] = time.time,
        busy_timeout_ms: int = 5000,
    ):
        if isinstance(scrypt_params, Mapping):
            params = ScryptParams(**dict(scrypt_params))
        elif isinstance(scrypt_params, ScryptParams):
            params = scrypt_params
        elif scrypt_params is None:
            params = ScryptParams()
        else:
            raise AuthValidationError("invalid scrypt parameters")
        if any(value is not None for value in (scrypt_n, scrypt_r, scrypt_p)):
            params = ScryptParams(
                n=params.n if scrypt_n is None else scrypt_n,
                r=params.r if scrypt_r is None else scrypt_r,
                p=params.p if scrypt_p is None else scrypt_p,
                salt_bytes=params.salt_bytes,
                dklen=params.dklen,
            )
        self.scrypt_params = params.validate()
        self.session_ttl_seconds = self._positive_duration(
            session_ttl_seconds, "session_ttl_seconds"
        )
        self.invite_ttl_seconds = self._positive_duration(
            invite_ttl_seconds, "invite_ttl_seconds"
        )
        if type(max_failed_logins) is not int or not 1 <= max_failed_logins <= 100:
            raise AuthValidationError("max_failed_logins must be between 1 and 100")
        self.max_failed_logins = max_failed_logins
        self.lockout_seconds = self._positive_duration(
            lockout_seconds, "lockout_seconds"
        )
        if not callable(clock):
            raise AuthValidationError("clock must be callable")
        self._clock = clock
        self._lock = RLock()
        self._closed = False
        self._conn = sqlite3.connect(
            db_path, check_same_thread=False, isolation_level=None
        )
        try:
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=FULL")
            self._conn.execute(f"PRAGMA busy_timeout={max(0, int(busy_timeout_ms))}")
            self._create_schema()
            # Unknown emails must pay the same KDF cost as known users.
            self._dummy_password_hash = _hash_password(
                secrets.token_urlsafe(24), self.scrypt_params
            )
        except Exception:
            self._conn.close()
            self._closed = True
            raise

    @staticmethod
    def _positive_duration(value: float, field: str) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise AuthValidationError(f"{field} must be numeric")
        result = float(value)
        if not math.isfinite(result) or result <= 0 or result > 10 * 365 * 86400:
            raise AuthValidationError(f"invalid {field}")
        return result

    def _create_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS auth_schema_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS auth_users (
                user_id TEXT PRIMARY KEY,
                email TEXT NOT NULL UNIQUE,
                display_name TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                personal_workspace_id TEXT NOT NULL,
                failed_login_count INTEGER NOT NULL DEFAULT 0 CHECK(failed_login_count >= 0),
                locked_until REAL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS auth_workspaces (
                workspace_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                personal_owner_user_id TEXT,
                revision INTEGER NOT NULL DEFAULT 0 CHECK(revision >= 0),
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                FOREIGN KEY(personal_owner_user_id) REFERENCES auth_users(user_id)
            );
            CREATE TABLE IF NOT EXISTS auth_memberships (
                member_id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('owner','admin','editor','reviewer','viewer')),
                revision INTEGER NOT NULL DEFAULT 0 CHECK(revision >= 0),
                joined_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                UNIQUE(workspace_id, user_id),
                FOREIGN KEY(workspace_id) REFERENCES auth_workspaces(workspace_id) ON DELETE CASCADE,
                FOREIGN KEY(user_id) REFERENCES auth_users(user_id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_auth_memberships_user
                ON auth_memberships(user_id, workspace_id);
            CREATE TABLE IF NOT EXISTS auth_sessions (
                session_id TEXT PRIMARY KEY,
                token_hash TEXT NOT NULL UNIQUE CHECK(length(token_hash) = 64),
                user_id TEXT NOT NULL,
                active_workspace_id TEXT,
                created_at REAL NOT NULL,
                last_seen_at REAL NOT NULL,
                expires_at REAL NOT NULL,
                revoked_at REAL,
                FOREIGN KEY(user_id) REFERENCES auth_users(user_id) ON DELETE CASCADE,
                FOREIGN KEY(active_workspace_id) REFERENCES auth_workspaces(workspace_id) ON DELETE SET NULL
            );
            CREATE INDEX IF NOT EXISTS idx_auth_sessions_user
                ON auth_sessions(user_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_auth_sessions_workspace_activity
                ON auth_sessions(active_workspace_id, created_at DESC, session_id DESC);
            CREATE TABLE IF NOT EXISTS auth_invites (
                invite_id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL,
                email TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('admin','editor','reviewer','viewer')),
                token_hash TEXT NOT NULL UNIQUE CHECK(length(token_hash) = 64),
                created_by TEXT NOT NULL,
                created_at REAL NOT NULL,
                expires_at REAL NOT NULL,
                accepted_at REAL,
                accepted_by TEXT,
                revoked_at REAL,
                FOREIGN KEY(workspace_id) REFERENCES auth_workspaces(workspace_id) ON DELETE CASCADE,
                FOREIGN KEY(created_by) REFERENCES auth_users(user_id),
                FOREIGN KEY(accepted_by) REFERENCES auth_users(user_id)
            );
            CREATE INDEX IF NOT EXISTS idx_auth_invites_workspace
                ON auth_invites(workspace_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_auth_invites_email
                ON auth_invites(email, created_at DESC);
            CREATE TABLE IF NOT EXISTS auth_password_capabilities (
                user_id TEXT PRIMARY KEY,
                enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
                updated_at REAL NOT NULL,
                FOREIGN KEY(user_id) REFERENCES auth_users(user_id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS auth_oidc_identities (
                identity_id TEXT PRIMARY KEY,
                issuer TEXT NOT NULL,
                subject TEXT NOT NULL,
                user_id TEXT NOT NULL,
                email_at_link TEXT NOT NULL,
                created_at REAL NOT NULL,
                last_login_at REAL NOT NULL,
                UNIQUE(issuer, subject),
                UNIQUE(user_id, issuer),
                FOREIGN KEY(user_id) REFERENCES auth_users(user_id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_auth_oidc_identities_user
                ON auth_oidc_identities(user_id, created_at);
            CREATE TABLE IF NOT EXISTS auth_workspace_oidc_policies (
                workspace_id TEXT PRIMARY KEY,
                issuer TEXT NOT NULL,
                allowed_domains_json TEXT NOT NULL,
                default_role TEXT NOT NULL
                    CHECK(default_role IN ('admin','editor','reviewer','viewer')),
                enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
                group_claim TEXT NOT NULL DEFAULT 'groups',
                group_role_map_json TEXT NOT NULL DEFAULT '{}',
                require_mapped_group INTEGER NOT NULL DEFAULT 0
                    CHECK(require_mapped_group IN (0,1)),
                revision INTEGER NOT NULL DEFAULT 0 CHECK(revision >= 0),
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                FOREIGN KEY(workspace_id) REFERENCES auth_workspaces(workspace_id)
                    ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS auth_oidc_managed_memberships (
                member_id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                issuer TEXT NOT NULL,
                subject TEXT NOT NULL,
                policy_revision INTEGER NOT NULL CHECK(policy_revision >= 0),
                updated_at REAL NOT NULL,
                UNIQUE(workspace_id, user_id),
                FOREIGN KEY(member_id) REFERENCES auth_memberships(member_id)
                    ON DELETE CASCADE,
                FOREIGN KEY(workspace_id) REFERENCES auth_workspaces(workspace_id)
                    ON DELETE CASCADE,
                FOREIGN KEY(user_id) REFERENCES auth_users(user_id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS auth_scim_users (
                scim_user_id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL,
                external_id TEXT,
                user_name TEXT NOT NULL,
                display_name TEXT NOT NULL,
                user_id TEXT NOT NULL,
                member_id TEXT,
                issuer TEXT NOT NULL,
                active INTEGER NOT NULL CHECK(active IN (0,1)),
                base_role TEXT NOT NULL
                    CHECK(base_role IN ('admin','editor','reviewer','viewer')),
                revision INTEGER NOT NULL DEFAULT 1 CHECK(revision >= 1),
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                deleted_at REAL,
                FOREIGN KEY(workspace_id) REFERENCES auth_workspaces(workspace_id)
                    ON DELETE CASCADE,
                FOREIGN KEY(user_id) REFERENCES auth_users(user_id) ON DELETE CASCADE,
                FOREIGN KEY(member_id) REFERENCES auth_memberships(member_id)
                    ON DELETE SET NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_auth_scim_users_external_active
                ON auth_scim_users(workspace_id, external_id)
                WHERE external_id IS NOT NULL AND deleted_at IS NULL;
            CREATE UNIQUE INDEX IF NOT EXISTS idx_auth_scim_users_name_active
                ON auth_scim_users(workspace_id, user_name)
                WHERE deleted_at IS NULL;
            CREATE INDEX IF NOT EXISTS idx_auth_scim_users_user
                ON auth_scim_users(user_id, workspace_id, updated_at DESC);
            CREATE TABLE IF NOT EXISTS auth_scim_groups (
                scim_group_id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL,
                external_id TEXT,
                display_name TEXT NOT NULL,
                mapped_role TEXT CHECK(mapped_role IN ('admin','editor','reviewer','viewer')),
                revision INTEGER NOT NULL DEFAULT 1 CHECK(revision >= 1),
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                deleted_at REAL,
                FOREIGN KEY(workspace_id) REFERENCES auth_workspaces(workspace_id)
                    ON DELETE CASCADE
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_auth_scim_groups_external_active
                ON auth_scim_groups(workspace_id, external_id)
                WHERE external_id IS NOT NULL AND deleted_at IS NULL;
            CREATE UNIQUE INDEX IF NOT EXISTS idx_auth_scim_groups_name_active
                ON auth_scim_groups(workspace_id, display_name)
                WHERE deleted_at IS NULL;
            CREATE TABLE IF NOT EXISTS auth_scim_group_members (
                scim_group_id TEXT NOT NULL,
                scim_user_id TEXT NOT NULL,
                created_at REAL NOT NULL,
                PRIMARY KEY(scim_group_id, scim_user_id),
                FOREIGN KEY(scim_group_id) REFERENCES auth_scim_groups(scim_group_id)
                    ON DELETE CASCADE,
                FOREIGN KEY(scim_user_id) REFERENCES auth_scim_users(scim_user_id)
                    ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS auth_service_accounts (
                service_account_id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL,
                name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                role TEXT NOT NULL
                    CHECK(role IN ('admin','editor','reviewer','viewer')),
                active INTEGER NOT NULL CHECK(active IN (0,1)),
                revision INTEGER NOT NULL DEFAULT 1 CHECK(revision >= 1),
                created_by TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                deleted_at REAL,
                FOREIGN KEY(workspace_id) REFERENCES auth_workspaces(workspace_id)
                    ON DELETE CASCADE,
                FOREIGN KEY(created_by) REFERENCES auth_users(user_id)
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_auth_service_accounts_name_active
                ON auth_service_accounts(workspace_id, name)
                WHERE deleted_at IS NULL;
            CREATE INDEX IF NOT EXISTS idx_auth_service_accounts_workspace
                ON auth_service_accounts(workspace_id, created_at DESC);
            CREATE TABLE IF NOT EXISTS auth_service_tokens (
                token_id TEXT PRIMARY KEY,
                service_account_id TEXT NOT NULL,
                token_hash TEXT NOT NULL UNIQUE CHECK(length(token_hash) = 64),
                label TEXT NOT NULL,
                secret_hint TEXT NOT NULL,
                revision INTEGER NOT NULL DEFAULT 1 CHECK(revision >= 1),
                created_at REAL NOT NULL,
                expires_at REAL,
                last_used_at REAL,
                revoked_at REAL,
                permissions_json TEXT,
                FOREIGN KEY(service_account_id)
                    REFERENCES auth_service_accounts(service_account_id)
                    ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_auth_service_tokens_account
                ON auth_service_tokens(service_account_id, created_at DESC);
            CREATE TABLE IF NOT EXISTS auth_service_account_policies (
                workspace_id TEXT PRIMARY KEY,
                max_accounts INTEGER NOT NULL CHECK(max_accounts BETWEEN 1 AND 500),
                max_tokens_per_account INTEGER NOT NULL
                    CHECK(max_tokens_per_account BETWEEN 1 AND 50),
                max_token_ttl_days INTEGER NOT NULL
                    CHECK(max_token_ttl_days BETWEEN 1 AND 365),
                allow_non_expiring INTEGER NOT NULL CHECK(allow_non_expiring IN (0,1)),
                allowed_permissions_json TEXT NOT NULL,
                revision INTEGER NOT NULL DEFAULT 1 CHECK(revision >= 1),
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                FOREIGN KEY(workspace_id) REFERENCES auth_workspaces(workspace_id)
                    ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS auth_workspace_session_policies (
                workspace_id TEXT PRIMARY KEY,
                idle_timeout_minutes INTEGER
                    CHECK(idle_timeout_minutes IS NULL OR
                          idle_timeout_minutes BETWEEN 5 AND 43200),
                absolute_timeout_hours INTEGER
                    CHECK(absolute_timeout_hours IS NULL OR
                          absolute_timeout_hours BETWEEN 1 AND 8760),
                max_active_sessions INTEGER
                    CHECK(max_active_sessions IS NULL OR
                          max_active_sessions BETWEEN 1 AND 50),
                revision INTEGER NOT NULL DEFAULT 1 CHECK(revision >= 1),
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                FOREIGN KEY(workspace_id) REFERENCES auth_workspaces(workspace_id)
                    ON DELETE CASCADE
            );
            """
        )
        token_columns = {
            str(item[1])
            for item in self._conn.execute("PRAGMA table_info(auth_service_tokens)")
        }
        if "permissions_json" not in token_columns:
            try:
                self._conn.execute(
                    "ALTER TABLE auth_service_tokens ADD COLUMN permissions_json TEXT"
                )
            except sqlite3.OperationalError as exc:
                if "duplicate column name" not in str(exc).casefold():
                    raise
        oidc_policy_columns = {
            str(item[1])
            for item in self._conn.execute(
                "PRAGMA table_info(auth_workspace_oidc_policies)"
            )
        }
        for column, definition in (
            ("group_claim", "TEXT NOT NULL DEFAULT 'groups'"),
            ("group_role_map_json", "TEXT NOT NULL DEFAULT '{}'"),
            (
                "require_mapped_group",
                "INTEGER NOT NULL DEFAULT 0 CHECK(require_mapped_group IN (0,1))",
            ),
        ):
            if column in oidc_policy_columns:
                continue
            try:
                self._conn.execute(
                    f"ALTER TABLE auth_workspace_oidc_policies "
                    f"ADD COLUMN {column} {definition}"
                )
            except sqlite3.OperationalError as exc:
                if "duplicate column name" not in str(exc).casefold():
                    raise
        row = self._conn.execute(
            "SELECT value FROM auth_schema_meta WHERE key='schema_version'"
        ).fetchone()
        if row is None:
            self._conn.execute(
                "INSERT INTO auth_schema_meta(key, value) VALUES('schema_version', ?)",
                (AUTH_SCHEMA_VERSION,),
            )
        elif row[0] in {"1", "2", "3", "4", "5", "6", "7"}:
            self._conn.execute(
                "UPDATE auth_schema_meta SET value=? WHERE key='schema_version' AND value=?",
                (AUTH_SCHEMA_VERSION, row[0]),
            )
        elif row[0] != AUTH_SCHEMA_VERSION:
            raise AuthStoreError("unsupported authentication schema version")

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        self._ensure_open()
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._conn.rollback()
            raise
        else:
            self._conn.commit()

    def _ensure_open(self) -> None:
        if self._closed:
            raise AuthStoreError("authentication store is closed")

    def _now(self) -> float:
        now = float(self._clock())
        if not math.isfinite(now):
            raise AuthStoreError("clock returned a non-finite timestamp")
        return now

    @staticmethod
    def _user(row: sqlite3.Row | tuple[Any, ...]) -> dict[str, Any]:
        return {
            "user_id": row[0],
            "email": row[1],
            "display_name": row[2],
            "created_at": _iso(row[3]),
            "updated_at": _iso(row[4]),
        }

    @staticmethod
    def _workspace(row: sqlite3.Row | tuple[Any, ...]) -> dict[str, Any]:
        result = {
            "workspace_id": row[0],
            "name": row[1],
            "created_at": _iso(row[2]),
            "updated_at": _iso(row[3]),
            "revision": row[4],
        }
        if len(row) > 5 and row[5] is not None:
            result["role"] = row[5]
        return result

    @staticmethod
    def _membership(row: sqlite3.Row | tuple[Any, ...]) -> dict[str, Any]:
        return {
            "member_id": row[0],
            "user_id": row[1],
            "email": row[2],
            "display_name": row[3],
            "role": row[4],
            "joined_at": _iso(row[5]),
            "updated_at": _iso(row[6]),
            "revision": row[7],
        }

    @staticmethod
    def _session(
        row: sqlite3.Row | tuple[Any, ...], *, current: bool = False
    ) -> dict[str, Any]:
        return {
            "session_id": row[0],
            "created_at": _iso(row[1]),
            "last_seen_at": _iso(row[2]),
            "expires_at": _iso(row[3]),
            "current": current,
        }

    @staticmethod
    def _workspace_session(
        row: sqlite3.Row | tuple[Any, ...], *, now: float
    ) -> dict[str, Any]:
        revoked_at = None if row[8] is None else float(row[8])
        expires_at = float(row[7])
        status = "revoked" if revoked_at is not None else "active"
        if status == "active" and expires_at <= now:
            status = "expired"
        return {
            "session_id": str(row[0]),
            "user_id": str(row[1]),
            "email": str(row[2]),
            "display_name": str(row[3]),
            "role": None if row[4] is None else str(row[4]),
            "created_at": _iso(float(row[5])),
            "last_seen_at": _iso(float(row[6])),
            "expires_at": _iso(expires_at),
            "revoked_at": None if revoked_at is None else _iso(revoked_at),
            "status": status,
        }

    def _invite(self, row: sqlite3.Row | tuple[Any, ...], now: float) -> dict[str, Any]:
        status = "pending"
        if row[8] is not None:
            status = "accepted"
        elif row[10] is not None:
            status = "revoked"
        elif row[7] <= now:
            status = "expired"
        return {
            "invite_id": row[0],
            "workspace_id": row[1],
            "email": row[2],
            "role": row[3],
            "created_by": row[4],
            "created_at": _iso(row[6]),
            "expires_at": _iso(row[7]),
            "status": status,
        }

    @staticmethod
    def _oidc_identity(row: sqlite3.Row | tuple[Any, ...]) -> dict[str, Any]:
        return {
            "identity_id": str(row[0]),
            "issuer": str(row[1]),
            "subject": str(row[2]),
            "email_at_link": str(row[3]),
            "created_at": _iso(float(row[4])),
            "last_login_at": _iso(float(row[5])),
        }

    @classmethod
    def _oidc_policy(cls, row: sqlite3.Row | tuple[Any, ...]) -> dict[str, Any]:
        try:
            domains = json.loads(str(row[2]))
            group_role_map = json.loads(str(row[9]))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise AuthStoreError("stored OIDC workspace policy is invalid") from exc
        if not isinstance(domains, list) or any(
            not isinstance(item, str) for item in domains
        ):
            raise AuthStoreError("stored OIDC workspace policy is invalid")
        try:
            group_claim = cls._clean_oidc_group_claim(str(row[8]))
            group_map_valid = (
                isinstance(group_role_map, dict)
                and len(group_role_map) <= MAX_OIDC_GROUP_MAPPINGS
                and all(
                    isinstance(group, str)
                    and isinstance(role, str)
                    and role in _ASSIGNABLE_ROLES
                    and cls._clean_oidc_group(group) == group
                    for group, role in group_role_map.items()
                )
            )
        except AuthValidationError as exc:
            raise AuthStoreError("stored OIDC workspace policy is invalid") from exc
        if not group_map_valid:
            raise AuthStoreError("stored OIDC workspace policy is invalid")
        return {
            "workspace_id": str(row[0]),
            "issuer": str(row[1]),
            "allowed_domains": list(domains),
            "default_role": str(row[3]),
            "enabled": bool(row[4]),
            "revision": int(row[5]),
            "created_at": _iso(float(row[6])),
            "updated_at": _iso(float(row[7])),
            "group_claim": group_claim,
            "group_role_map": dict(group_role_map),
            "require_mapped_group": bool(row[10]),
        }

    @staticmethod
    def _clean_oidc_value(value: str, *, field: str, maximum: int = 2048) -> str:
        if (
            type(value) is not str
            or not value
            or value != value.strip()
            or len(value) > maximum
            or any(ord(character) < 33 or ord(character) == 127 for character in value)
        ):
            raise AuthValidationError(f"invalid {field}")
        return value

    @staticmethod
    def _email_domain(email: str) -> str:
        return normalize_email(email).rsplit("@", 1)[1]

    @staticmethod
    def _clean_domains(
        domains: Iterator[str] | list[str] | tuple[str, ...],
    ) -> tuple[str, ...]:
        result: list[str] = []
        for raw in domains:
            if type(raw) is not str:
                raise AuthValidationError("OIDC domains must be strings")
            domain = raw.strip().casefold().rstrip(".")
            if (
                not domain
                or len(domain) > 253
                or "@" in domain
                or "/" in domain
                or ":" in domain
                or domain.startswith(".")
                or any(character.isspace() for character in domain)
            ):
                raise AuthValidationError("invalid OIDC email domain")
            if domain not in result:
                result.append(domain)
        if not result or len(result) > 100:
            raise AuthValidationError("OIDC policy requires 1 to 100 domains")
        return tuple(sorted(result))

    @staticmethod
    def _clean_oidc_group(value: str) -> str:
        if type(value) is not str:
            raise AuthValidationError("OIDC group names must be strings")
        normalized = unicodedata.normalize("NFKC", " ".join(value.split())).casefold()
        if (
            not normalized
            or len(normalized) > 256
            or any(
                ord(character) < 32 or ord(character) == 127 for character in normalized
            )
        ):
            raise AuthValidationError("invalid OIDC group name")
        return normalized

    @classmethod
    def _clean_oidc_group_claim(cls, value: str) -> str:
        claim = cls._clean_oidc_value(value, field="OIDC group claim", maximum=128)
        if claim in _OIDC_RESERVED_GROUP_CLAIMS:
            raise AuthValidationError("OIDC group claim is reserved")
        return claim

    @classmethod
    def _clean_oidc_group_role_map(
        cls, mapping: Mapping[str, Role | str] | None
    ) -> dict[str, str]:
        if mapping is None:
            return {}
        if not isinstance(mapping, Mapping) or len(mapping) > MAX_OIDC_GROUP_MAPPINGS:
            raise AuthValidationError(
                f"OIDC group role map supports at most {MAX_OIDC_GROUP_MAPPINGS} groups"
            )
        result: dict[str, str] = {}
        for raw_group, raw_role in mapping.items():
            group = cls._clean_oidc_group(raw_group)
            role = _clean_role(raw_role).value
            existing = result.get(group)
            if existing is not None and existing != role:
                raise AuthValidationError(
                    "OIDC group role map contains conflicting groups"
                )
            result[group] = role
        return dict(sorted(result.items()))

    @classmethod
    def _clean_oidc_group_claims(
        cls, claims: Mapping[str, Sequence[str]] | None
    ) -> dict[str, tuple[str, ...]]:
        if claims is None:
            return {}
        if not isinstance(claims, Mapping) or len(claims) > 32:
            raise AuthValidationError("OIDC group claims are invalid")
        result: dict[str, tuple[str, ...]] = {}
        for raw_name, raw_values in claims.items():
            name = cls._clean_oidc_value(raw_name, field="OIDC claim", maximum=128)
            if isinstance(raw_values, (str, bytes)) or not isinstance(
                raw_values, Sequence
            ):
                raise AuthValidationError("OIDC group claim must be a string list")
            if len(raw_values) > MAX_OIDC_GROUPS_PER_CLAIM:
                raise AuthValidationError("OIDC group claim contains too many groups")
            groups = tuple(
                sorted({cls._clean_oidc_group(value) for value in raw_values})
            )
            result[name] = groups
        encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        if len(encoded) > MAX_OIDC_GROUP_CLAIMS_BYTES:
            raise AuthValidationError("OIDC group claims are too large")
        return result

    @staticmethod
    def _oidc_role_for_policy(
        policy: Mapping[str, Any], group_claims: Mapping[str, Sequence[str]]
    ) -> tuple[str, tuple[str, ...]]:
        claim_name = str(policy["group_claim"])
        asserted = tuple(group_claims.get(claim_name, ()))
        role_map = policy["group_role_map"]
        matched = tuple(group for group in asserted if group in role_map)
        if matched:
            role = max(
                (str(role_map[group]) for group in matched),
                key=lambda item: _SCIM_ROLE_RANK[item],
            )
            return role, matched
        if bool(policy["require_mapped_group"]):
            raise AuthAuthorizationError("OIDC identity has no mapped workspace group")
        return str(policy["default_role"]), ()

    def _password_enabled_locked(self, user_id: str) -> bool:
        row = self._conn.execute(
            "SELECT enabled FROM auth_password_capabilities WHERE user_id=?",
            (user_id,),
        ).fetchone()
        return row is None or bool(row[0])

    def _scim_account_enabled_locked(self, user_id: str) -> bool:
        row = self._conn.execute(
            "SELECT COUNT(*),COALESCE(SUM(CASE WHEN active=1 AND deleted_at IS NULL "
            "THEN 1 ELSE 0 END),0) FROM auth_scim_users WHERE user_id=?",
            (user_id,),
        ).fetchone()
        return row is None or int(row[0]) == 0 or int(row[1]) > 0

    def _get_user_row(self, user_id: str) -> tuple[Any, ...]:
        row = self._conn.execute(
            "SELECT user_id,email,display_name,created_at,updated_at,password_hash,"
            "personal_workspace_id,failed_login_count,locked_until "
            "FROM auth_users WHERE user_id=?",
            (user_id,),
        ).fetchone()
        if row is None:
            raise AuthNotFoundError("user not found")
        return row

    def _membership_row(
        self, workspace_id: str, user_id: str
    ) -> tuple[Any, ...] | None:
        return self._conn.execute(
            "SELECT member_id,role,revision FROM auth_memberships "
            "WHERE workspace_id=? AND user_id=?",
            (workspace_id, user_id),
        ).fetchone()

    def _membership_target_row(
        self, workspace_id: str, member_or_user_id: str
    ) -> tuple[Any, ...] | None:
        """Resolve route-facing membership IDs while retaining user-ID callers."""

        return self._conn.execute(
            "SELECT member_id,user_id,role,revision FROM auth_memberships "
            "WHERE workspace_id=? AND (member_id=? OR user_id=?)",
            (workspace_id, member_or_user_id, member_or_user_id),
        ).fetchone()

    def _require_manager(self, workspace_id: str, actor_user_id: str) -> Role:
        membership = self._membership_row(workspace_id, actor_user_id)
        if membership is None:
            raise AuthAuthorizationError("workspace membership required")
        role = Role(membership[1])
        if role not in {Role.OWNER, Role.ADMIN}:
            raise AuthAuthorizationError("workspace owner or admin required")
        return role

    def _session_policy_locked(self, workspace_id: str) -> dict[str, Any]:
        row = self._conn.execute(
            "SELECT idle_timeout_minutes,absolute_timeout_hours,max_active_sessions,"
            "revision,created_at,updated_at FROM auth_workspace_session_policies "
            "WHERE workspace_id=?",
            (workspace_id,),
        ).fetchone()
        if row is None:
            return {
                "workspace_id": workspace_id,
                "idle_timeout_minutes": None,
                "absolute_timeout_hours": None,
                "max_active_sessions": None,
                "revision": 0,
                "created_at": None,
                "updated_at": None,
            }
        return {
            "workspace_id": workspace_id,
            "idle_timeout_minutes": None if row[0] is None else int(row[0]),
            "absolute_timeout_hours": None if row[1] is None else int(row[1]),
            "max_active_sessions": None if row[2] is None else int(row[2]),
            "revision": int(row[3]),
            "created_at": _iso(float(row[4])),
            "updated_at": _iso(float(row[5])),
        }

    @staticmethod
    def _session_policy_expired(
        policy: Mapping[str, Any], *, created_at: float, last_seen_at: float, now: float
    ) -> bool:
        absolute = policy["absolute_timeout_hours"]
        idle = policy["idle_timeout_minutes"]
        return bool(
            (absolute is not None and created_at + int(absolute) * 3600 <= now)
            or (idle is not None and last_seen_at + int(idle) * 60 <= now)
        )

    def _enforce_session_policy_locked(
        self,
        workspace_id: str,
        *,
        now: float,
        joining_session_id: str | None = None,
        joining_user_id: str | None = None,
    ) -> int:
        """Revoke policy-expired/overflow sessions for one active workspace.

        ``joining_session_id`` is excluded from the existing set and reserves
        one slot before an already-authenticated session switches workspace.
        """

        policy = self._session_policy_locked(workspace_id)
        rows = self._conn.execute(
            "SELECT session_id,user_id,created_at,last_seen_at,expires_at FROM auth_sessions "
            "WHERE active_workspace_id=? AND revoked_at IS NULL AND expires_at>? "
            "ORDER BY created_at DESC,session_id DESC",
            (workspace_id, now),
        ).fetchall()
        revoke: set[str] = {
            str(row[0])
            for row in rows
            if str(row[0]) != joining_session_id
            and self._session_policy_expired(
                policy,
                created_at=float(row[2]),
                last_seen_at=float(row[3]),
                now=now,
            )
        }
        maximum = policy["max_active_sessions"]
        if maximum is not None:
            eligible_by_user: dict[str, list[str]] = {}
            for row in rows:
                session = str(row[0])
                if session == joining_session_id or session in revoke:
                    continue
                eligible_by_user.setdefault(str(row[1]), []).append(session)
            for user_id, eligible in eligible_by_user.items():
                available = max(
                    0,
                    int(maximum)
                    - (
                        1
                        if joining_user_id is not None and user_id == joining_user_id
                        else 0
                    ),
                )
                revoke.update(eligible[available:])
        for session_id in sorted(revoke):
            self._conn.execute(
                "UPDATE auth_sessions SET revoked_at=? WHERE session_id=? "
                "AND revoked_at IS NULL",
                (now, session_id),
            )
        return len(revoke)

    def _issue_session_locked(
        self, user_id: str, workspace_id: str, now: float
    ) -> tuple[dict[str, Any], str]:
        if self._membership_row(workspace_id, user_id) is None:
            raise AuthAuthorizationError("user is not a workspace member")
        session_id = _new_id("ses")
        token = _new_token("cgs")
        policy = self._session_policy_locked(workspace_id)
        self._enforce_session_policy_locked(
            workspace_id,
            now=now,
            joining_session_id=session_id,
            joining_user_id=user_id,
        )
        expires_at = now + self.session_ttl_seconds
        absolute = policy["absolute_timeout_hours"]
        if absolute is not None:
            expires_at = min(expires_at, now + int(absolute) * 3600)
        self._conn.execute(
            "INSERT INTO auth_sessions(session_id,token_hash,user_id,active_workspace_id,"
            "created_at,last_seen_at,expires_at,revoked_at) VALUES(?,?,?,?,?,?,?,NULL)",
            (
                session_id,
                _token_hash(token),
                user_id,
                workspace_id,
                now,
                now,
                expires_at,
            ),
        )
        return (
            {
                "session_id": session_id,
                "created_at": _iso(now),
                "last_seen_at": _iso(now),
                "expires_at": _iso(expires_at),
                "current": True,
            },
            token,
        )

    def register(
        self,
        email: str,
        password: str,
        display_name: str,
        workspace_name: str | None = None,
    ) -> dict[str, Any]:
        clean_email = normalize_email(email)
        clean_password = _clean_password(password)
        clean_display = _clean_text(display_name, field="display_name")
        clean_workspace = _clean_text(
            workspace_name or f"{clean_display} Workspace", field="workspace_name"
        )
        encoded_password = _hash_password(clean_password, self.scrypt_params)
        now = self._now()
        user_id = _new_id("usr")
        workspace_id = _new_id("wsp")
        member_id = _new_id("mem")
        with self._lock:
            try:
                with self._transaction():
                    if (
                        self._conn.execute(
                            "SELECT 1 FROM auth_scim_users WHERE user_name=? "
                            "AND active=1 AND deleted_at IS NULL LIMIT 1",
                            (clean_email,),
                        ).fetchone()
                        is not None
                    ):
                        raise AuthConflictError("email is managed by SCIM")
                    self._conn.execute(
                        "INSERT INTO auth_users(user_id,email,display_name,password_hash,"
                        "personal_workspace_id,failed_login_count,locked_until,created_at,updated_at) "
                        "VALUES(?,?,?,?,?,0,NULL,?,?)",
                        (
                            user_id,
                            clean_email,
                            clean_display,
                            encoded_password,
                            workspace_id,
                            now,
                            now,
                        ),
                    )
                    self._conn.execute(
                        "INSERT INTO auth_workspaces(workspace_id,name,personal_owner_user_id,"
                        "revision,created_at,updated_at) VALUES(?,?,?,0,?,?)",
                        (workspace_id, clean_workspace, user_id, now, now),
                    )
                    self._conn.execute(
                        "INSERT INTO auth_memberships(member_id,workspace_id,user_id,role,"
                        "revision,joined_at,updated_at) VALUES(?,?,?,'owner',0,?,?)",
                        (member_id, workspace_id, user_id, now, now),
                    )
                    session, token = self._issue_session_locked(
                        user_id, workspace_id, now
                    )
            except sqlite3.IntegrityError as exc:
                raise AuthConflictError("email is already registered") from exc
        user = self.get_user(user_id=user_id)
        workspace = self.get_workspace(workspace_id, user_id=user_id)
        return {
            "user": user,
            "workspace": workspace,
            "session": session,
            "access_token": token,
            "expires_at": session["expires_at"],
        }

    register_user = register

    def login(
        self, email: str, password: str, workspace_id: str | None = None
    ) -> dict[str, Any]:
        clean_email = normalize_email(email)
        candidate_password = _clean_password(password, enforce_minimum=False)
        requested_workspace = (
            _clean_id(workspace_id, field="workspace_id")
            if workspace_id is not None
            else None
        )
        now = self._now()
        with self._lock:
            self._ensure_open()
            row = self._conn.execute(
                "SELECT u.user_id,u.password_hash,u.personal_workspace_id,"
                "u.failed_login_count,u.locked_until,COALESCE(p.enabled,1) "
                "FROM auth_users u LEFT JOIN auth_password_capabilities p "
                "ON p.user_id=u.user_id WHERE u.email=?",
                (clean_email,),
            ).fetchone()
            password_enabled = row is not None and bool(row[5])
            encoded = (
                row[1]
                if row is not None and password_enabled
                else self._dummy_password_hash
            )
            valid = _verify_password(candidate_password, encoded) and password_enabled
            if row is None:
                raise AuthAuthenticationError("invalid email or password")
            user_id, _, personal_workspace_id, failed_count, locked_until = row[:5]
            locked = locked_until is not None and locked_until > now
            if locked:
                raise AuthLockedError("login is temporarily locked")
            if not valid:
                with self._transaction():
                    # An expired lock starts a fresh failure window.
                    prior = 0 if locked_until is not None else int(failed_count)
                    failures = prior + 1
                    new_locked_until = (
                        now + self.lockout_seconds
                        if failures >= self.max_failed_logins
                        else None
                    )
                    self._conn.execute(
                        "UPDATE auth_users SET failed_login_count=?,locked_until=?,updated_at=? "
                        "WHERE user_id=?",
                        (failures, new_locked_until, now, user_id),
                    )
                if new_locked_until is not None:
                    raise AuthLockedError("login is temporarily locked")
                raise AuthAuthenticationError("invalid email or password")
            if not self._scim_account_enabled_locked(str(user_id)):
                raise AuthAuthorizationError("SCIM-managed account is inactive")
            with self._transaction():
                target = requested_workspace or personal_workspace_id
                if self._membership_row(target, user_id) is None:
                    raise AuthAuthorizationError("user is not a workspace member")
                self._conn.execute(
                    "UPDATE auth_users SET failed_login_count=0,locked_until=NULL,updated_at=? "
                    "WHERE user_id=?",
                    (now, user_id),
                )
                session, token = self._issue_session_locked(user_id, target, now)
        user = self.get_user(user_id=user_id)
        workspace = self.get_workspace(target, user_id=user_id)
        return {
            "user": user,
            "workspace": workspace,
            "session": session,
            "access_token": token,
            "expires_at": session["expires_at"],
        }

    def authenticate_session(
        self, token: str, workspace_id: str | None = None
    ) -> AuthContext:
        digest = _token_hash(token)
        requested = (
            _clean_id(workspace_id, field="workspace_id")
            if workspace_id is not None
            else None
        )
        now = self._now()
        with self._lock:
            self._ensure_open()
            row = self._conn.execute(
                "SELECT s.session_id,s.user_id,s.active_workspace_id,s.created_at,"
                "s.last_seen_at,s.expires_at,s.revoked_at,u.personal_workspace_id "
                "FROM auth_sessions s JOIN auth_users u ON u.user_id=s.user_id "
                "WHERE s.token_hash=?",
                (digest,),
            ).fetchone()
            if row is None or row[6] is not None or row[5] <= now:
                raise AuthAuthenticationError("session is invalid or expired")
            session_id, user_id, active_workspace_id = row[:3]
            if not self._scim_account_enabled_locked(str(user_id)):
                raise AuthAuthenticationError("session is invalid or expired")
            target = requested or active_workspace_id or row[7]
            membership = self._membership_row(target, user_id)
            if membership is None:
                # A deleted/removed active workspace may fall back only to the
                # user's personal workspace, never to an arbitrary membership.
                target = row[7]
                membership = self._membership_row(target, user_id)
            if membership is None or (requested is not None and target != requested):
                raise AuthAuthorizationError("user is not a workspace member")
            policy = self._session_policy_locked(str(target))
            if self._session_policy_expired(
                policy,
                created_at=float(row[3]),
                last_seen_at=float(row[4]),
                now=now,
            ):
                with self._transaction():
                    self._conn.execute(
                        "UPDATE auth_sessions SET revoked_at=? WHERE session_id=? "
                        "AND revoked_at IS NULL",
                        (now, session_id),
                    )
                raise AuthAuthenticationError("session is invalid or expired")
            # Session authentication is a read-heavy path. Persist activity at a
            # bounded cadence (or immediately on workspace switch) so concurrent
            # RAG reads do not serialize on a SQLite write for every request.
            if (
                target != active_workspace_id
                or now - float(row[4]) >= SESSION_TOUCH_INTERVAL_SECONDS
            ):
                with self._transaction():
                    if target != active_workspace_id:
                        self._enforce_session_policy_locked(
                            str(target),
                            now=now,
                            joining_session_id=str(session_id),
                            joining_user_id=str(user_id),
                        )
                    effective_expires_at = float(row[5])
                    absolute = policy["absolute_timeout_hours"]
                    if absolute is not None:
                        effective_expires_at = min(
                            effective_expires_at,
                            float(row[3]) + int(absolute) * 3600,
                        )
                    changed = self._conn.execute(
                        "UPDATE auth_sessions SET active_workspace_id=?,last_seen_at=?,"
                        "expires_at=? "
                        "WHERE session_id=? AND revoked_at IS NULL AND expires_at>?",
                        (target, now, effective_expires_at, session_id, now),
                    ).rowcount
                    if changed != 1:
                        raise AuthAuthenticationError(
                            "session changed during authentication"
                        )
            user_row = self._get_user_row(user_id)
            workspace_row = self._conn.execute(
                "SELECT workspace_id,name,created_at,updated_at,revision FROM auth_workspaces "
                "WHERE workspace_id=?",
                (target,),
            ).fetchone()
            if workspace_row is None:
                raise AuthAuthenticationError("session workspace is unavailable")
            user = self._user(user_row[:5])
            workspace = self._workspace((*workspace_row, membership[1]))
            session = {
                "session_id": session_id,
                "created_at": _iso(row[3]),
                "last_seen_at": _iso(
                    now
                    if target != active_workspace_id
                    or now - float(row[4]) >= SESSION_TOUCH_INTERVAL_SECONDS
                    else float(row[4])
                ),
                "expires_at": _iso(
                    min(
                        float(row[5]),
                        (
                            float(row[3]) + int(policy["absolute_timeout_hours"]) * 3600
                            if policy["absolute_timeout_hours"] is not None
                            else float(row[5])
                        ),
                    )
                ),
                "current": True,
            }
            principal = Principal.for_user_session(
                tenant_id=target,
                subject_id=user_id,
                role=Role(membership[1]),
                session_id=session_id,
                membership_id=str(membership[0]),
            )
            return AuthContext(user, workspace, session, principal)

    def get_user(
        self, user_id: str | None = None, *, email: str | None = None
    ) -> dict[str, Any]:
        if (user_id is None) == (email is None):
            raise AuthValidationError("provide exactly one of user_id or email")
        with self._lock:
            self._ensure_open()
            if user_id is not None:
                identifier = _clean_id(user_id, field="user_id")
                row = self._conn.execute(
                    "SELECT user_id,email,display_name,created_at,updated_at "
                    "FROM auth_users WHERE user_id=?",
                    (identifier,),
                ).fetchone()
            else:
                assert email is not None
                row = self._conn.execute(
                    "SELECT user_id,email,display_name,created_at,updated_at "
                    "FROM auth_users WHERE email=?",
                    (normalize_email(email),),
                ).fetchone()
            if row is None:
                raise AuthNotFoundError("user not found")
            return self._user(row)

    def lookup_user(self, email: str) -> dict[str, Any] | None:
        try:
            return self.get_user(email=email)
        except AuthNotFoundError:
            return None

    def _matching_oidc_policies_locked(
        self, issuer: str, email_domain: str
    ) -> list[tuple[Any, ...]]:
        rows = self._conn.execute(
            "SELECT workspace_id,issuer,allowed_domains_json,default_role,enabled,"
            "revision,created_at,updated_at,group_claim,group_role_map_json,"
            "require_mapped_group FROM auth_workspace_oidc_policies "
            "WHERE issuer=? AND enabled=1 ORDER BY workspace_id",
            (issuer,),
        ).fetchall()
        matches: list[tuple[Any, ...]] = []
        for row in rows:
            policy = self._oidc_policy(row)
            if email_domain in policy["allowed_domains"]:
                matches.append(row)
        return matches

    def _create_oidc_user_locked(
        self, *, email: str, display_name: str, now: float
    ) -> tuple[str, str]:
        user_id, workspace_id, member_id = (
            _new_id("usr"),
            _new_id("wsp"),
            _new_id("mem"),
        )
        # The random password is never returned and password capability is
        # disabled in the same transaction.  Password login still performs the
        # dummy KDF, so an OIDC-only email is not a cheap account oracle.
        unreachable_password = _hash_password(
            secrets.token_urlsafe(48), self.scrypt_params
        )
        workspace_name = _clean_text(
            f"{display_name} Workspace", field="workspace_name"
        )
        self._conn.execute(
            "INSERT INTO auth_users(user_id,email,display_name,password_hash,"
            "personal_workspace_id,failed_login_count,locked_until,created_at,updated_at) "
            "VALUES(?,?,?,?,?,0,NULL,?,?)",
            (
                user_id,
                email,
                display_name,
                unreachable_password,
                workspace_id,
                now,
                now,
            ),
        )
        self._conn.execute(
            "INSERT INTO auth_password_capabilities(user_id,enabled,updated_at) "
            "VALUES(?,0,?)",
            (user_id, now),
        )
        self._conn.execute(
            "INSERT INTO auth_workspaces(workspace_id,name,personal_owner_user_id,"
            "revision,created_at,updated_at) VALUES(?,?,?,0,?,?)",
            (workspace_id, workspace_name, user_id, now, now),
        )
        self._conn.execute(
            "INSERT INTO auth_memberships(member_id,workspace_id,user_id,role,revision,"
            "joined_at,updated_at) VALUES(?,?,?,'owner',0,?,?)",
            (member_id, workspace_id, user_id, now, now),
        )
        return user_id, workspace_id

    def login_oidc(
        self,
        *,
        issuer: str,
        subject: str,
        email: str,
        display_name: str,
        email_verified: bool,
        group_claims: Mapping[str, Sequence[str]] | None = None,
        workspace_id: str | None = None,
        jit_provisioning_enabled: bool = False,
        allow_verified_email_link: bool = False,
    ) -> dict[str, Any]:
        """Authenticate a verified OIDC subject and issue a normal CogDoc session."""

        clean_issuer = self._clean_oidc_value(issuer, field="issuer")
        clean_subject = self._clean_oidc_value(
            subject, field="OIDC subject", maximum=512
        )
        clean_email = normalize_email(email)
        clean_display = _clean_text(display_name, field="display_name")
        clean_group_claims = self._clean_oidc_group_claims(group_claims)
        requested_workspace = (
            _clean_id(workspace_id, field="workspace_id")
            if workspace_id is not None
            else None
        )
        if email_verified is not True:
            raise AuthAuthenticationError("OIDC email is not verified")
        now = self._now()
        with self._lock:
            try:
                with self._transaction():
                    identity = self._conn.execute(
                        "SELECT identity_id,user_id,email_at_link FROM auth_oidc_identities "
                        "WHERE issuer=? AND subject=?",
                        (clean_issuer, clean_subject),
                    ).fetchone()
                    if identity is None:
                        scim_users = self._conn.execute(
                            "SELECT DISTINCT u.user_id,u.personal_workspace_id "
                            "FROM auth_scim_users s JOIN auth_users u "
                            "ON u.user_id=s.user_id WHERE s.issuer=? AND s.user_name=? "
                            "AND s.active=1 AND s.deleted_at IS NULL",
                            (clean_issuer, clean_email),
                        ).fetchall()
                        if len(scim_users) > 1:
                            raise AuthConflictError(
                                "SCIM directory identity is ambiguous"
                            )
                        user_row = scim_users[0] if scim_users else None
                        if user_row is None:
                            user_row = self._conn.execute(
                                "SELECT user_id,personal_workspace_id FROM auth_users WHERE email=?",
                                (clean_email,),
                            ).fetchone()
                        if user_row is not None:
                            scim_link = self._conn.execute(
                                "SELECT 1 FROM auth_scim_users WHERE user_id=? AND issuer=? "
                                "AND active=1 AND deleted_at IS NULL LIMIT 1",
                                (user_row[0], clean_issuer),
                            ).fetchone()
                            if not allow_verified_email_link and scim_link is None:
                                raise AuthConflictError(
                                    "verified email belongs to an account that requires explicit linking"
                                )
                            user_id, personal_workspace_id = (
                                str(user_row[0]),
                                str(user_row[1]),
                            )
                        else:
                            if not jit_provisioning_enabled:
                                raise AuthAuthorizationError(
                                    "OIDC just-in-time provisioning is disabled"
                                )
                            user_id, personal_workspace_id = (
                                self._create_oidc_user_locked(
                                    email=clean_email,
                                    display_name=clean_display,
                                    now=now,
                                )
                            )
                        identity_id = _new_id("odi")
                        self._conn.execute(
                            "INSERT INTO auth_oidc_identities(identity_id,issuer,subject,user_id,"
                            "email_at_link,created_at,last_login_at) VALUES(?,?,?,?,?,?,?)",
                            (
                                identity_id,
                                clean_issuer,
                                clean_subject,
                                user_id,
                                clean_email,
                                now,
                                now,
                            ),
                        )
                    else:
                        user_id = str(identity[1])
                        user_row = self._conn.execute(
                            "SELECT personal_workspace_id FROM auth_users WHERE user_id=?",
                            (user_id,),
                        ).fetchone()
                        if user_row is None:
                            raise AuthStoreError(
                                "OIDC identity references a missing user"
                            )
                        personal_workspace_id = str(user_row[0])
                        self._conn.execute(
                            "UPDATE auth_oidc_identities SET last_login_at=? "
                            "WHERE identity_id=?",
                            (now, identity[0]),
                        )

                    if not self._scim_account_enabled_locked(user_id):
                        raise AuthAuthorizationError("SCIM-managed account is inactive")

                    domain = self._email_domain(clean_email)
                    policies = self._matching_oidc_policies_locked(clean_issuer, domain)
                    target = requested_workspace
                    scim_targets = [
                        str(row[0])
                        for row in self._conn.execute(
                            "SELECT workspace_id FROM auth_scim_users WHERE user_id=? "
                            "AND issuer=? AND active=1 AND deleted_at IS NULL "
                            "ORDER BY workspace_id",
                            (user_id, clean_issuer),
                        ).fetchall()
                    ]
                    if target is None and scim_targets:
                        if len(scim_targets) != 1:
                            raise AuthConflictError(
                                "OIDC identity is provisioned into multiple workspaces"
                            )
                        target = scim_targets[0]
                    elif target is None and policies:
                        if len(policies) != 1:
                            raise AuthConflictError(
                                "OIDC identity matches multiple workspace policies"
                            )
                        target = str(policies[0][0])
                    target = target or personal_workspace_id
                    membership = self._membership_row(target, user_id)
                    scim_state = self._conn.execute(
                        "SELECT scim_user_id,active,deleted_at FROM auth_scim_users "
                        "WHERE workspace_id=? AND user_id=? AND issuer=? "
                        "ORDER BY updated_at DESC LIMIT 1",
                        (target, user_id, clean_issuer),
                    ).fetchone()
                    if scim_state is not None:
                        if not bool(scim_state[1]) or scim_state[2] is not None:
                            raise AuthAuthorizationError(
                                "SCIM-provisioned access is inactive"
                            )
                        self._sync_scim_membership_locked(str(scim_state[0]), now)
                        membership = self._membership_row(target, user_id)

                    policy_row = next(
                        (row for row in policies if str(row[0]) == target), None
                    )
                    managed = self._conn.execute(
                        "SELECT member_id FROM auth_oidc_managed_memberships "
                        "WHERE workspace_id=? AND user_id=? AND issuer=? AND subject=?",
                        (target, user_id, clean_issuer, clean_subject),
                    ).fetchone()
                    if membership is None and policy_row is None:
                        raise AuthAuthorizationError(
                            "OIDC identity is not admitted to the requested workspace"
                        )
                    if membership is None:
                        assert policy_row is not None
                        policy = self._oidc_policy(policy_row)
                        role, _matched_groups = self._oidc_role_for_policy(
                            policy, clean_group_claims
                        )
                        member_id = _new_id("mem")
                        self._conn.execute(
                            "INSERT INTO auth_memberships(member_id,workspace_id,user_id,role,"
                            "revision,joined_at,updated_at) VALUES(?,?,?,?,0,?,?)",
                            (member_id, target, user_id, role, now, now),
                        )
                        self._conn.execute(
                            "INSERT INTO auth_oidc_managed_memberships(member_id,workspace_id,"
                            "user_id,issuer,subject,policy_revision,updated_at) "
                            "VALUES(?,?,?,?,?,?,?)",
                            (
                                member_id,
                                target,
                                user_id,
                                clean_issuer,
                                clean_subject,
                                int(policy["revision"]),
                                now,
                            ),
                        )
                        membership = self._membership_row(target, user_id)
                    elif managed is not None and scim_state is None:
                        if policy_row is None:
                            raise AuthAuthorizationError(
                                "OIDC identity is not admitted to the requested workspace"
                            )
                        policy = self._oidc_policy(policy_row)
                        role, _matched_groups = self._oidc_role_for_policy(
                            policy, clean_group_claims
                        )
                        if str(membership[1]) == Role.OWNER.value:
                            raise AuthConflictError(
                                "OIDC group mapping cannot manage a workspace owner"
                            )
                        self._conn.execute(
                            "UPDATE auth_memberships SET role=?,revision=revision+1,updated_at=? "
                            "WHERE member_id=? AND role<>?",
                            (role, now, membership[0], role),
                        )
                        self._conn.execute(
                            "UPDATE auth_oidc_managed_memberships SET policy_revision=?,"
                            "updated_at=? WHERE member_id=?",
                            (
                                int(policy["revision"]),
                                now,
                                membership[0],
                            ),
                        )
                    session, token = self._issue_session_locked(user_id, target, now)
            except sqlite3.IntegrityError as exc:
                raise AuthConflictError("OIDC identity changed concurrently") from exc
        user = self.get_user(user_id=user_id)
        workspace = self.get_workspace(target, user_id=user_id)
        return {
            "user": user,
            "workspace": workspace,
            "session": session,
            "access_token": token,
            "expires_at": session["expires_at"],
        }

    def _session_is_active_locked(
        self,
        *,
        session_id: str,
        user_id: str,
        now: float,
        workspace_id: str | None = None,
    ) -> bool:
        row = self._conn.execute(
            "SELECT active_workspace_id,created_at,last_seen_at,expires_at,revoked_at "
            "FROM auth_sessions WHERE session_id=? AND user_id=?",
            (session_id, user_id),
        ).fetchone()
        if row is None or row[4] is not None or float(row[3]) <= now:
            return False
        active_workspace = None if row[0] is None else str(row[0])
        target_workspace = workspace_id or active_workspace
        if (
            target_workspace is None
            or active_workspace != target_workspace
            or self._membership_row(target_workspace, user_id) is None
            or not self._scim_account_enabled_locked(user_id)
        ):
            return False
        policy = self._session_policy_locked(target_workspace)
        return not self._session_policy_expired(
            policy,
            created_at=float(row[1]),
            last_seen_at=float(row[2]),
            now=now,
        )

    def session_is_active(
        self,
        *,
        session_id: str,
        user_id: str,
        workspace_id: str | None = None,
    ) -> bool:
        clean_session = _clean_id(session_id, field="session_id")
        clean_user = _clean_id(user_id, field="user_id")
        clean_workspace = (
            None
            if workspace_id is None
            else _clean_id(workspace_id, field="workspace_id")
        )
        now = self._now()
        with self._lock:
            self._ensure_open()
            return self._session_is_active_locked(
                session_id=clean_session,
                user_id=clean_user,
                workspace_id=clean_workspace,
                now=now,
            )

    def link_oidc_identity(
        self,
        *,
        user_id: str,
        issuer: str,
        subject: str,
        email: str,
        email_verified: bool,
    ) -> dict[str, Any]:
        clean_user = _clean_id(user_id, field="user_id")
        clean_issuer = self._clean_oidc_value(issuer, field="issuer")
        clean_subject = self._clean_oidc_value(
            subject, field="OIDC subject", maximum=512
        )
        clean_email = normalize_email(email)
        if email_verified is not True:
            raise AuthAuthenticationError("OIDC email is not verified")
        now, identity_id = self._now(), _new_id("odi")
        with self._lock:
            try:
                with self._transaction():
                    self._get_user_row(clean_user)
                    self._conn.execute(
                        "INSERT INTO auth_oidc_identities(identity_id,issuer,subject,user_id,"
                        "email_at_link,created_at,last_login_at) VALUES(?,?,?,?,?,?,?)",
                        (
                            identity_id,
                            clean_issuer,
                            clean_subject,
                            clean_user,
                            clean_email,
                            now,
                            now,
                        ),
                    )
            except sqlite3.IntegrityError as exc:
                raise AuthConflictError("OIDC identity is already linked") from exc
        return self.get_oidc_identity(identity_id=identity_id, user_id=clean_user)

    def link_oidc_identity_from_session(
        self,
        *,
        session_id: str,
        user_id: str,
        issuer: str,
        subject: str,
        email: str,
        email_verified: bool,
    ) -> dict[str, Any]:
        """Link only while the frozen initiating session is live in this transaction."""

        clean_session = _clean_id(session_id, field="session_id")
        clean_user = _clean_id(user_id, field="user_id")
        clean_issuer = self._clean_oidc_value(issuer, field="issuer")
        clean_subject = self._clean_oidc_value(
            subject, field="OIDC subject", maximum=512
        )
        clean_email = normalize_email(email)
        if email_verified is not True:
            raise AuthAuthenticationError("OIDC email is not verified")
        now, identity_id = self._now(), _new_id("odi")
        with self._lock:
            try:
                with self._transaction():
                    if not self._session_is_active_locked(
                        session_id=clean_session,
                        user_id=clean_user,
                        now=now,
                    ):
                        raise AuthAuthenticationError(
                            "linking session is no longer active"
                        )
                    self._conn.execute(
                        "INSERT INTO auth_oidc_identities(identity_id,issuer,subject,user_id,"
                        "email_at_link,created_at,last_login_at) VALUES(?,?,?,?,?,?,?)",
                        (
                            identity_id,
                            clean_issuer,
                            clean_subject,
                            clean_user,
                            clean_email,
                            now,
                            now,
                        ),
                    )
            except sqlite3.IntegrityError as exc:
                raise AuthConflictError("OIDC identity is already linked") from exc
        return self.get_oidc_identity(identity_id=identity_id, user_id=clean_user)

    def get_oidc_identity(self, *, identity_id: str, user_id: str) -> dict[str, Any]:
        clean_identity = _clean_id(identity_id, field="identity_id")
        clean_user = _clean_id(user_id, field="user_id")
        with self._lock:
            self._ensure_open()
            row = self._conn.execute(
                "SELECT identity_id,issuer,subject,email_at_link,created_at,last_login_at "
                "FROM auth_oidc_identities WHERE identity_id=? AND user_id=?",
                (clean_identity, clean_user),
            ).fetchone()
            if row is None:
                raise AuthNotFoundError("OIDC identity not found")
            return self._oidc_identity(row)

    def list_oidc_identities(self, *, user_id: str) -> list[dict[str, Any]]:
        clean_user = _clean_id(user_id, field="user_id")
        with self._lock:
            self._ensure_open()
            rows = self._conn.execute(
                "SELECT identity_id,issuer,subject,email_at_link,created_at,last_login_at "
                "FROM auth_oidc_identities WHERE user_id=? ORDER BY created_at,identity_id",
                (clean_user,),
            ).fetchall()
            return [self._oidc_identity(row) for row in rows]

    def unlink_oidc_identity(self, *, identity_id: str, user_id: str) -> bool:
        clean_identity = _clean_id(identity_id, field="identity_id")
        clean_user = _clean_id(user_id, field="user_id")
        with self._lock:
            self._ensure_open()
            row = self._conn.execute(
                "SELECT 1 FROM auth_oidc_identities WHERE identity_id=? AND user_id=?",
                (clean_identity, clean_user),
            ).fetchone()
            if row is None:
                raise AuthNotFoundError("OIDC identity not found")
            identity_count = int(
                self._conn.execute(
                    "SELECT COUNT(*) FROM auth_oidc_identities WHERE user_id=?",
                    (clean_user,),
                ).fetchone()[0]
            )
            if identity_count <= 1 and not self._password_enabled_locked(clean_user):
                raise AuthConflictError("cannot remove the only authentication method")
            return (
                self._conn.execute(
                    "DELETE FROM auth_oidc_identities WHERE identity_id=? AND user_id=?",
                    (clean_identity, clean_user),
                ).rowcount
                == 1
            )

    def get_oidc_policy(
        self, *, workspace_id: str, actor_user_id: str
    ) -> dict[str, Any] | None:
        clean_workspace = _clean_id(workspace_id, field="workspace_id")
        clean_actor = _clean_id(actor_user_id, field="user_id")
        with self._lock:
            self._ensure_open()
            self._require_manager(clean_workspace, clean_actor)
            row = self._conn.execute(
                "SELECT workspace_id,issuer,allowed_domains_json,default_role,enabled,"
                "revision,created_at,updated_at,group_claim,group_role_map_json,"
                "require_mapped_group FROM auth_workspace_oidc_policies "
                "WHERE workspace_id=?",
                (clean_workspace,),
            ).fetchone()
            return None if row is None else self._oidc_policy(row)

    def set_oidc_policy(
        self,
        *,
        workspace_id: str,
        issuer: str,
        allowed_domains: list[str] | tuple[str, ...],
        default_role: Role | str,
        enabled: bool,
        actor_user_id: str,
        group_claim: str = "groups",
        group_role_map: Mapping[str, Role | str] | None = None,
        require_mapped_group: bool = False,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        clean_workspace = _clean_id(workspace_id, field="workspace_id")
        clean_actor = _clean_id(actor_user_id, field="user_id")
        clean_issuer = self._clean_oidc_value(issuer, field="issuer")
        domains = self._clean_domains(allowed_domains)
        role = _clean_role(default_role)
        clean_group_claim = self._clean_oidc_group_claim(group_claim)
        clean_group_role_map = self._clean_oidc_group_role_map(group_role_map)
        if type(enabled) is not bool:
            raise AuthValidationError("OIDC policy enabled must be boolean")
        if type(require_mapped_group) is not bool:
            raise AuthValidationError("require_mapped_group must be boolean")
        if require_mapped_group and not clean_group_role_map:
            raise AuthValidationError(
                "require_mapped_group requires at least one group role mapping"
            )
        if expected_revision is not None and (
            type(expected_revision) is not int or expected_revision < 0
        ):
            raise AuthValidationError("invalid expected_revision")
        now = self._now()
        encoded_domains = json.dumps(domains, ensure_ascii=True, separators=(",", ":"))
        encoded_group_role_map = json.dumps(
            clean_group_role_map, ensure_ascii=False, separators=(",", ":")
        )
        with self._lock, self._transaction():
            self._require_manager(clean_workspace, clean_actor)
            row = self._conn.execute(
                "SELECT revision FROM auth_workspace_oidc_policies WHERE workspace_id=?",
                (clean_workspace,),
            ).fetchone()
            if row is None:
                if expected_revision not in (None, 0):
                    raise AuthConflictError("OIDC policy revision conflict")
                self._conn.execute(
                    "INSERT INTO auth_workspace_oidc_policies(workspace_id,issuer,"
                    "allowed_domains_json,default_role,enabled,group_claim,"
                    "group_role_map_json,require_mapped_group,revision,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,0,?,?)",
                    (
                        clean_workspace,
                        clean_issuer,
                        encoded_domains,
                        role.value,
                        int(enabled),
                        clean_group_claim,
                        encoded_group_role_map,
                        int(require_mapped_group),
                        now,
                        now,
                    ),
                )
            else:
                revision = int(row[0])
                if expected_revision is not None and revision != expected_revision:
                    raise AuthConflictError("OIDC policy revision conflict")
                changed = self._conn.execute(
                    "UPDATE auth_workspace_oidc_policies SET issuer=?,"
                    "allowed_domains_json=?,default_role=?,enabled=?,group_claim=?,"
                    "group_role_map_json=?,require_mapped_group=?,revision=revision+1,"
                    "updated_at=? WHERE workspace_id=? AND revision=?",
                    (
                        clean_issuer,
                        encoded_domains,
                        role.value,
                        int(enabled),
                        clean_group_claim,
                        encoded_group_role_map,
                        int(require_mapped_group),
                        now,
                        clean_workspace,
                        revision,
                    ),
                ).rowcount
                if changed != 1:
                    raise AuthConflictError("OIDC policy changed concurrently")
        policy = self.get_oidc_policy(
            workspace_id=clean_workspace, actor_user_id=clean_actor
        )
        if policy is None:
            raise AuthStoreError("OIDC policy disappeared after update")
        return policy

    @staticmethod
    def _clean_scim_external_id(value: str | None) -> str | None:
        if value is None:
            return None
        if (
            type(value) is not str
            or not value
            or value != value.strip()
            or len(value) > 512
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
        ):
            raise AuthValidationError("invalid SCIM externalId")
        return value

    @staticmethod
    def _scim_user(row: sqlite3.Row | tuple[Any, ...]) -> dict[str, Any]:
        return {
            "id": str(row[0]),
            "workspace_id": str(row[1]),
            "external_id": None if row[2] is None else str(row[2]),
            "user_name": str(row[3]),
            "display_name": str(row[4]),
            "user_id": str(row[5]),
            "active": bool(row[6]),
            "base_role": str(row[7]),
            "revision": int(row[8]),
            "created_at": _iso(float(row[9])),
            "updated_at": _iso(float(row[10])),
        }

    @staticmethod
    def _scim_group(row: sqlite3.Row | tuple[Any, ...]) -> dict[str, Any]:
        members = json.loads(str(row[9])) if row[9] is not None else []
        if not isinstance(members, list) or any(
            not isinstance(item, str) for item in members
        ):
            raise AuthStoreError("stored SCIM group members are invalid")
        return {
            "id": str(row[0]),
            "workspace_id": str(row[1]),
            "external_id": None if row[2] is None else str(row[2]),
            "display_name": str(row[3]),
            "mapped_role": None if row[4] is None else str(row[4]),
            "revision": int(row[5]),
            "created_at": _iso(float(row[6])),
            "updated_at": _iso(float(row[7])),
            "members": members,
        }

    def _scim_user_row_locked(self, workspace_id: str, scim_user_id: str):
        row = self._conn.execute(
            "SELECT scim_user_id,workspace_id,external_id,user_name,display_name,user_id,"
            "active,base_role,revision,created_at,updated_at FROM auth_scim_users "
            "WHERE workspace_id=? AND scim_user_id=? AND deleted_at IS NULL",
            (workspace_id, scim_user_id),
        ).fetchone()
        if row is None:
            raise AuthNotFoundError("SCIM user not found")
        return row

    def _scim_group_row_locked(self, workspace_id: str, scim_group_id: str):
        row = self._conn.execute(
            "SELECT g.scim_group_id,g.workspace_id,g.external_id,g.display_name,"
            "g.mapped_role,g.revision,g.created_at,g.updated_at,g.deleted_at,"
            "COALESCE(json_group_array(m.scim_user_id) FILTER "
            "(WHERE m.scim_user_id IS NOT NULL AND u.deleted_at IS NULL),'[]') "
            "FROM auth_scim_groups g LEFT JOIN auth_scim_group_members m "
            "ON m.scim_group_id=g.scim_group_id LEFT JOIN auth_scim_users u "
            "ON u.scim_user_id=m.scim_user_id WHERE g.workspace_id=? "
            "AND g.scim_group_id=? AND g.deleted_at IS NULL GROUP BY g.scim_group_id",
            (workspace_id, scim_group_id),
        ).fetchone()
        if row is None:
            raise AuthNotFoundError("SCIM group not found")
        return row

    def _sync_scim_membership_locked(self, scim_user_id: str, now: float) -> None:
        row = self._conn.execute(
            "SELECT workspace_id,user_id,member_id,active,base_role,deleted_at "
            "FROM auth_scim_users WHERE scim_user_id=?",
            (scim_user_id,),
        ).fetchone()
        if row is None:
            return
        workspace_id, user_id, member_id = str(row[0]), str(row[1]), row[2]
        active = bool(row[3]) and row[5] is None
        if not active:
            if member_id is None:
                existing = self._conn.execute(
                    "SELECT member_id,role FROM auth_memberships "
                    "WHERE workspace_id=? AND user_id=?",
                    (workspace_id, user_id),
                ).fetchone()
                if existing is not None:
                    if str(existing[1]) == Role.OWNER.value:
                        raise AuthConflictError("SCIM cannot manage a workspace owner")
                    member_id = str(existing[0])
            if member_id is not None:
                self._conn.execute(
                    "DELETE FROM auth_memberships WHERE member_id=? AND workspace_id=?",
                    (member_id, workspace_id),
                )
            self._conn.execute(
                "UPDATE auth_scim_users SET member_id=NULL WHERE scim_user_id=?",
                (scim_user_id,),
            )
            self._conn.execute(
                "UPDATE auth_sessions SET revoked_at=? WHERE user_id=? "
                "AND active_workspace_id=? AND revoked_at IS NULL",
                (now, user_id, workspace_id),
            )
            if not self._scim_account_enabled_locked(user_id):
                self._conn.execute(
                    "UPDATE auth_sessions SET revoked_at=? WHERE user_id=? "
                    "AND revoked_at IS NULL",
                    (now, user_id),
                )
            return
        roles = [str(row[4])]
        roles.extend(
            str(item[0])
            for item in self._conn.execute(
                "SELECT g.mapped_role FROM auth_scim_group_members m "
                "JOIN auth_scim_groups g ON g.scim_group_id=m.scim_group_id "
                "WHERE m.scim_user_id=? AND g.deleted_at IS NULL "
                "AND g.mapped_role IS NOT NULL",
                (scim_user_id,),
            ).fetchall()
        )
        role = max(roles, key=lambda item: _SCIM_ROLE_RANK[item])
        existing = self._conn.execute(
            "SELECT member_id,role FROM auth_memberships WHERE workspace_id=? AND user_id=?",
            (workspace_id, user_id),
        ).fetchone()
        if existing is not None and str(existing[1]) == Role.OWNER.value:
            raise AuthConflictError("SCIM cannot manage a workspace owner")
        if existing is None:
            member_id = _new_id("mem")
            self._conn.execute(
                "INSERT INTO auth_memberships(member_id,workspace_id,user_id,role,revision,"
                "joined_at,updated_at) VALUES(?,?,?,?,0,?,?)",
                (member_id, workspace_id, user_id, role, now, now),
            )
        else:
            member_id = str(existing[0])
            self._conn.execute(
                "UPDATE auth_memberships SET role=?,revision=revision+1,updated_at=? "
                "WHERE member_id=? AND role<>?",
                (role, now, member_id, role),
            )
        self._conn.execute(
            "UPDATE auth_scim_users SET member_id=? WHERE scim_user_id=?",
            (member_id, scim_user_id),
        )
        # Once SCIM manages this membership, OIDC group claims must never
        # overwrite the directory-authoritative role on later logins.
        self._conn.execute(
            "DELETE FROM auth_oidc_managed_memberships "
            "WHERE workspace_id=? AND user_id=?",
            (workspace_id, user_id),
        )

    def create_scim_user(
        self,
        *,
        workspace_id: str,
        issuer: str,
        external_id: str | None,
        user_name: str,
        display_name: str,
        active: bool,
        base_role: Role | str,
    ) -> dict[str, Any]:
        workspace = _clean_id(workspace_id, field="workspace_id")
        clean_issuer = self._clean_oidc_value(issuer, field="issuer")
        external = self._clean_scim_external_id(external_id)
        email = normalize_email(user_name)
        display = _clean_text(display_name or email, field="display_name")
        role = _clean_role(base_role)
        if type(active) is not bool:
            raise AuthValidationError("SCIM active must be boolean")
        now, scim_user_id = self._now(), _new_id("scu")
        with self._lock:
            try:
                with self._transaction():
                    if (
                        self._conn.execute(
                            "SELECT 1 FROM auth_workspaces WHERE workspace_id=?",
                            (workspace,),
                        ).fetchone()
                        is None
                    ):
                        raise AuthNotFoundError("workspace not found")
                    directory_users = self._conn.execute(
                        "SELECT DISTINCT user_id FROM auth_scim_users WHERE issuer=? "
                        "AND user_name=? AND active=1 AND deleted_at IS NULL",
                        (clean_issuer, email),
                    ).fetchall()
                    if len(directory_users) > 1:
                        raise AuthConflictError("SCIM directory identity is ambiguous")
                    user = self._conn.execute(
                        "SELECT user_id FROM auth_users WHERE email=?", (email,)
                    ).fetchone()
                    if directory_users:
                        directory_user_id = str(directory_users[0][0])
                        if user is not None and str(user[0]) != directory_user_id:
                            raise AuthConflictError(
                                "SCIM userName belongs to another account"
                            )
                        user_id = directory_user_id
                    elif user is None:
                        user_id, _ = self._create_oidc_user_locked(
                            email=email, display_name=display, now=now
                        )
                    else:
                        user_id = str(user[0])
                    self._conn.execute(
                        "INSERT INTO auth_scim_users(scim_user_id,workspace_id,external_id,"
                        "user_name,display_name,user_id,issuer,active,base_role,revision,"
                        "created_at,updated_at,deleted_at) VALUES(?,?,?,?,?,?,?,?,?,1,?,?,NULL)",
                        (
                            scim_user_id,
                            workspace,
                            external,
                            email,
                            display,
                            user_id,
                            clean_issuer,
                            int(active),
                            role.value,
                            now,
                            now,
                        ),
                    )
                    self._sync_scim_membership_locked(scim_user_id, now)
            except sqlite3.IntegrityError as exc:
                raise AuthConflictError("SCIM user already exists") from exc
        return self.get_scim_user(workspace_id=workspace, scim_user_id=scim_user_id)

    def get_scim_user(self, *, workspace_id: str, scim_user_id: str) -> dict[str, Any]:
        workspace = _clean_id(workspace_id, field="workspace_id")
        identifier = _clean_id(scim_user_id, field="scim_user_id")
        with self._lock:
            self._ensure_open()
            return self._scim_user(self._scim_user_row_locked(workspace, identifier))

    def list_scim_users(
        self,
        *,
        workspace_id: str,
        filter_field: str | None = None,
        filter_value: str | None = None,
        start_index: int = 1,
        count: int = 100,
    ) -> tuple[int, list[dict[str, Any]]]:
        workspace = _clean_id(workspace_id, field="workspace_id")
        if (
            type(start_index) is not int
            or start_index < 1
            or type(count) is not int
            or not 0 <= count <= 200
        ):
            raise AuthValidationError("invalid SCIM pagination")
        clauses, params = ["workspace_id=?", "deleted_at IS NULL"], [workspace]
        columns = {
            "id": "scim_user_id",
            "externalId": "external_id",
            "userName": "user_name",
        }
        if filter_field is not None:
            column = columns.get(filter_field)
            if column is None or filter_value is None:
                raise AuthValidationError("unsupported SCIM user filter")
            value = (
                normalize_email(filter_value)
                if filter_field == "userName"
                else filter_value
            )
            clauses.append(f"{column}=?")
            params.append(value)
        where = " AND ".join(clauses)
        with self._lock:
            self._ensure_open()
            total = int(
                self._conn.execute(
                    f"SELECT COUNT(*) FROM auth_scim_users WHERE {where}", params
                ).fetchone()[0]
            )
            rows = self._conn.execute(
                "SELECT scim_user_id,workspace_id,external_id,user_name,display_name,user_id,"
                f"active,base_role,revision,created_at,updated_at FROM auth_scim_users WHERE {where} "
                "ORDER BY scim_user_id LIMIT ? OFFSET ?",
                (*params, count, start_index - 1),
            ).fetchall()
            return total, [self._scim_user(row) for row in rows]

    def update_scim_user(
        self,
        *,
        workspace_id: str,
        scim_user_id: str,
        external_id: str | None,
        user_name: str,
        display_name: str,
        active: bool,
        base_role: Role | str,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        workspace = _clean_id(workspace_id, field="workspace_id")
        identifier = _clean_id(scim_user_id, field="scim_user_id")
        external = self._clean_scim_external_id(external_id)
        email = normalize_email(user_name)
        display = _clean_text(display_name or email, field="display_name")
        role = _clean_role(base_role)
        if type(active) is not bool:
            raise AuthValidationError("SCIM active must be boolean")
        now = self._now()
        with self._lock:
            try:
                with self._transaction():
                    row = self._scim_user_row_locked(workspace, identifier)
                    revision, user_id = int(row[8]), str(row[5])
                    if expected_revision is not None and expected_revision != revision:
                        raise AuthConflictError("SCIM user version conflict")
                    owner = self._conn.execute(
                        "SELECT user_id FROM auth_users WHERE email=? AND user_id<>?",
                        (email, user_id),
                    ).fetchone()
                    if owner is not None:
                        raise AuthConflictError(
                            "SCIM userName belongs to another account"
                        )
                    issuer_row = self._conn.execute(
                        "SELECT issuer FROM auth_scim_users WHERE scim_user_id=?",
                        (identifier,),
                    ).fetchone()
                    if issuer_row is None:
                        raise AuthNotFoundError("SCIM user not found")
                    directory_owner = self._conn.execute(
                        "SELECT user_id FROM auth_scim_users WHERE issuer=? "
                        "AND user_name=? AND scim_user_id<>? AND active=1 "
                        "AND deleted_at IS NULL LIMIT 1",
                        (issuer_row[0], email, identifier),
                    ).fetchone()
                    if (
                        directory_owner is not None
                        and str(directory_owner[0]) != user_id
                    ):
                        raise AuthConflictError(
                            "SCIM userName belongs to another directory account"
                        )
                    changed = self._conn.execute(
                        "UPDATE auth_scim_users SET external_id=?,user_name=?,display_name=?,"
                        "active=?,base_role=?,revision=revision+1,updated_at=? "
                        "WHERE workspace_id=? AND scim_user_id=? AND revision=? "
                        "AND deleted_at IS NULL",
                        (
                            external,
                            email,
                            display,
                            int(active),
                            role.value,
                            now,
                            workspace,
                            identifier,
                            revision,
                        ),
                    ).rowcount
                    if changed != 1:
                        raise AuthConflictError("SCIM user changed concurrently")
                    self._sync_scim_membership_locked(identifier, now)
            except sqlite3.IntegrityError as exc:
                raise AuthConflictError("SCIM user already exists") from exc
        return self.get_scim_user(workspace_id=workspace, scim_user_id=identifier)

    def delete_scim_user(
        self,
        *,
        workspace_id: str,
        scim_user_id: str,
        expected_revision: int | None = None,
    ) -> bool:
        workspace = _clean_id(workspace_id, field="workspace_id")
        identifier = _clean_id(scim_user_id, field="scim_user_id")
        now = self._now()
        with self._lock, self._transaction():
            row = self._scim_user_row_locked(workspace, identifier)
            if expected_revision is not None and int(row[8]) != expected_revision:
                raise AuthConflictError("SCIM user version conflict")
            changed = self._conn.execute(
                "UPDATE auth_scim_users SET active=0,deleted_at=?,revision=revision+1,"
                "updated_at=? WHERE scim_user_id=? AND revision=? AND deleted_at IS NULL",
                (now, now, identifier, int(row[8])),
            ).rowcount
            group_ids = [
                str(item[0])
                for item in self._conn.execute(
                    "SELECT scim_group_id FROM auth_scim_group_members "
                    "WHERE scim_user_id=?",
                    (identifier,),
                ).fetchall()
            ]
            self._conn.execute(
                "DELETE FROM auth_scim_group_members WHERE scim_user_id=?",
                (identifier,),
            )
            for group_id in group_ids:
                self._conn.execute(
                    "UPDATE auth_scim_groups SET revision=revision+1,updated_at=? "
                    "WHERE scim_group_id=? AND deleted_at IS NULL",
                    (now, group_id),
                )
            self._sync_scim_membership_locked(identifier, now)
            return changed == 1

    def create_scim_group(
        self,
        *,
        workspace_id: str,
        external_id: str | None,
        display_name: str,
        mapped_role: Role | str | None,
        member_ids: list[str] | tuple[str, ...] = (),
    ) -> dict[str, Any]:
        workspace = _clean_id(workspace_id, field="workspace_id")
        external = self._clean_scim_external_id(external_id)
        display = _clean_text(display_name, field="display_name")
        role = None if mapped_role is None else _clean_role(mapped_role).value
        members = tuple(
            dict.fromkeys(_clean_id(item, field="scim_user_id") for item in member_ids)
        )
        if len(members) > 10_000:
            raise AuthValidationError("SCIM group has too many members")
        now, group_id = self._now(), _new_id("scg")
        with self._lock:
            try:
                with self._transaction():
                    if (
                        self._conn.execute(
                            "SELECT 1 FROM auth_workspaces WHERE workspace_id=?",
                            (workspace,),
                        ).fetchone()
                        is None
                    ):
                        raise AuthNotFoundError("workspace not found")
                    self._conn.execute(
                        "INSERT INTO auth_scim_groups(scim_group_id,workspace_id,external_id,"
                        "display_name,mapped_role,revision,created_at,updated_at,deleted_at) "
                        "VALUES(?,?,?,?,?,1,?,?,NULL)",
                        (group_id, workspace, external, display, role, now, now),
                    )
                    for member in members:
                        self._scim_user_row_locked(workspace, member)
                        self._conn.execute(
                            "INSERT INTO auth_scim_group_members(scim_group_id,scim_user_id,"
                            "created_at) VALUES(?,?,?)",
                            (group_id, member, now),
                        )
                    for member in members:
                        self._sync_scim_membership_locked(member, now)
                        self._conn.execute(
                            "UPDATE auth_scim_users SET revision=revision+1,updated_at=? "
                            "WHERE scim_user_id=?",
                            (now, member),
                        )
            except sqlite3.IntegrityError as exc:
                raise AuthConflictError("SCIM group already exists") from exc
        return self.get_scim_group(workspace_id=workspace, scim_group_id=group_id)

    def get_scim_group(
        self, *, workspace_id: str, scim_group_id: str
    ) -> dict[str, Any]:
        workspace = _clean_id(workspace_id, field="workspace_id")
        identifier = _clean_id(scim_group_id, field="scim_group_id")
        with self._lock:
            self._ensure_open()
            return self._scim_group(self._scim_group_row_locked(workspace, identifier))

    def list_scim_groups(
        self,
        *,
        workspace_id: str,
        filter_field: str | None = None,
        filter_value: str | None = None,
        start_index: int = 1,
        count: int = 100,
    ) -> tuple[int, list[dict[str, Any]]]:
        workspace = _clean_id(workspace_id, field="workspace_id")
        if (
            type(start_index) is not int
            or start_index < 1
            or type(count) is not int
            or not 0 <= count <= 200
        ):
            raise AuthValidationError("invalid SCIM pagination")
        clauses, params = ["g.workspace_id=?", "g.deleted_at IS NULL"], [workspace]
        columns = {
            "id": "g.scim_group_id",
            "externalId": "g.external_id",
            "displayName": "g.display_name",
        }
        if filter_field is not None:
            column = columns.get(filter_field)
            if column is None or filter_value is None:
                raise AuthValidationError("unsupported SCIM group filter")
            clauses.append(f"{column}=?")
            params.append(filter_value)
        where = " AND ".join(clauses)
        with self._lock:
            self._ensure_open()
            total = int(
                self._conn.execute(
                    f"SELECT COUNT(*) FROM auth_scim_groups g WHERE {where}", params
                ).fetchone()[0]
            )
            ids = self._conn.execute(
                f"SELECT g.scim_group_id FROM auth_scim_groups g WHERE {where} "
                "ORDER BY g.scim_group_id LIMIT ? OFFSET ?",
                (*params, count, start_index - 1),
            ).fetchall()
            rows = [
                self._scim_group(self._scim_group_row_locked(workspace, str(row[0])))
                for row in ids
            ]
            return total, rows

    def update_scim_group(
        self,
        *,
        workspace_id: str,
        scim_group_id: str,
        external_id: str | None,
        display_name: str,
        mapped_role: Role | str | None,
        member_ids: list[str] | tuple[str, ...],
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        workspace = _clean_id(workspace_id, field="workspace_id")
        identifier = _clean_id(scim_group_id, field="scim_group_id")
        external = self._clean_scim_external_id(external_id)
        display = _clean_text(display_name, field="display_name")
        role = None if mapped_role is None else _clean_role(mapped_role).value
        members = tuple(
            dict.fromkeys(_clean_id(item, field="scim_user_id") for item in member_ids)
        )
        if len(members) > 10_000:
            raise AuthValidationError("SCIM group has too many members")
        now = self._now()
        with self._lock:
            try:
                with self._transaction():
                    current = self._scim_group_row_locked(workspace, identifier)
                    revision = int(current[5])
                    if expected_revision is not None and expected_revision != revision:
                        raise AuthConflictError("SCIM group version conflict")
                    previous = {
                        str(row[0])
                        for row in self._conn.execute(
                            "SELECT scim_user_id FROM auth_scim_group_members "
                            "WHERE scim_group_id=?",
                            (identifier,),
                        ).fetchall()
                    }
                    for member in members:
                        self._scim_user_row_locked(workspace, member)
                    changed = self._conn.execute(
                        "UPDATE auth_scim_groups SET external_id=?,display_name=?,mapped_role=?,"
                        "revision=revision+1,updated_at=? WHERE workspace_id=? "
                        "AND scim_group_id=? AND revision=? AND deleted_at IS NULL",
                        (external, display, role, now, workspace, identifier, revision),
                    ).rowcount
                    if changed != 1:
                        raise AuthConflictError("SCIM group changed concurrently")
                    self._conn.execute(
                        "DELETE FROM auth_scim_group_members WHERE scim_group_id=?",
                        (identifier,),
                    )
                    for member in members:
                        self._conn.execute(
                            "INSERT INTO auth_scim_group_members(scim_group_id,scim_user_id,"
                            "created_at) VALUES(?,?,?)",
                            (identifier, member, now),
                        )
                    for member in previous | set(members):
                        self._sync_scim_membership_locked(member, now)
                        self._conn.execute(
                            "UPDATE auth_scim_users SET revision=revision+1,updated_at=? "
                            "WHERE scim_user_id=? AND deleted_at IS NULL",
                            (now, member),
                        )
            except sqlite3.IntegrityError as exc:
                raise AuthConflictError("SCIM group already exists") from exc
        return self.get_scim_group(workspace_id=workspace, scim_group_id=identifier)

    def delete_scim_group(
        self,
        *,
        workspace_id: str,
        scim_group_id: str,
        expected_revision: int | None = None,
    ) -> bool:
        workspace = _clean_id(workspace_id, field="workspace_id")
        identifier = _clean_id(scim_group_id, field="scim_group_id")
        now = self._now()
        with self._lock, self._transaction():
            group = self._scim_group_row_locked(workspace, identifier)
            if expected_revision is not None and int(group[5]) != expected_revision:
                raise AuthConflictError("SCIM group version conflict")
            members = [
                str(row[0])
                for row in self._conn.execute(
                    "SELECT scim_user_id FROM auth_scim_group_members "
                    "WHERE scim_group_id=?",
                    (identifier,),
                ).fetchall()
            ]
            self._conn.execute(
                "DELETE FROM auth_scim_group_members WHERE scim_group_id=?",
                (identifier,),
            )
            changed = self._conn.execute(
                "UPDATE auth_scim_groups SET deleted_at=?,revision=revision+1,updated_at=? "
                "WHERE scim_group_id=? AND revision=? AND deleted_at IS NULL",
                (now, now, identifier, int(group[5])),
            ).rowcount
            for member in members:
                self._sync_scim_membership_locked(member, now)
                self._conn.execute(
                    "UPDATE auth_scim_users SET revision=revision+1,updated_at=? "
                    "WHERE scim_user_id=? AND deleted_at IS NULL",
                    (now, member),
                )
            return changed == 1

    def get_scim_summary(
        self, *, workspace_id: str, actor_user_id: str
    ) -> dict[str, Any]:
        workspace = _clean_id(workspace_id, field="workspace_id")
        actor = _clean_id(actor_user_id, field="user_id")
        with self._lock:
            self._ensure_open()
            self._require_manager(workspace, actor)
            users = self._conn.execute(
                "SELECT COUNT(*),"
                "COALESCE(SUM(CASE WHEN active=1 AND deleted_at IS NULL THEN 1 ELSE 0 END),0),"
                "COALESCE(SUM(CASE WHEN active=0 AND deleted_at IS NULL THEN 1 ELSE 0 END),0),"
                "COALESCE(SUM(CASE WHEN deleted_at IS NOT NULL THEN 1 ELSE 0 END),0),"
                "MAX(updated_at) FROM auth_scim_users WHERE workspace_id=?",
                (workspace,),
            ).fetchone()
            groups = self._conn.execute(
                "SELECT COUNT(*),MAX(updated_at) FROM auth_scim_groups "
                "WHERE workspace_id=? AND deleted_at IS NULL",
                (workspace,),
            ).fetchone()
            memberships = int(
                self._conn.execute(
                    "SELECT COUNT(*) FROM auth_scim_group_members m "
                    "JOIN auth_scim_groups g ON g.scim_group_id=m.scim_group_id "
                    "JOIN auth_scim_users u ON u.scim_user_id=m.scim_user_id "
                    "WHERE g.workspace_id=? AND g.deleted_at IS NULL "
                    "AND u.deleted_at IS NULL",
                    (workspace,),
                ).fetchone()[0]
            )
            timestamps = [
                float(value) for value in (users[4], groups[1]) if value is not None
            ]
            return {
                "active_users": int(users[1]),
                "inactive_users": int(users[2]),
                "deleted_users": int(users[3]),
                "groups": int(groups[0]),
                "group_memberships": memberships,
                "last_updated_at": _iso(max(timestamps)) if timestamps else None,
            }

    def reconcile_scim_policy(
        self,
        *,
        workspace_id: str,
        default_role: Role | str,
        group_role_map: Mapping[str, str],
    ) -> int:
        workspace = _clean_id(workspace_id, field="workspace_id")
        base_role = _clean_role(default_role).value
        clean_map: dict[str, str] = {}
        for name, role in group_role_map.items():
            if not isinstance(name, str) or not name:
                raise AuthValidationError("invalid SCIM group role map")
            clean_map[name.casefold()] = _clean_role(role).value
        now = self._now()
        with self._lock, self._transaction():
            if (
                self._conn.execute(
                    "SELECT 1 FROM auth_workspaces WHERE workspace_id=?", (workspace,)
                ).fetchone()
                is None
            ):
                raise AuthNotFoundError("workspace not found")
            affected = {
                str(row[0])
                for row in self._conn.execute(
                    "SELECT scim_user_id FROM auth_scim_users WHERE workspace_id=? "
                    "AND deleted_at IS NULL AND base_role<>?",
                    (workspace, base_role),
                ).fetchall()
            }
            changed = self._conn.execute(
                "UPDATE auth_scim_users SET base_role=?,revision=revision+1,updated_at=? "
                "WHERE workspace_id=? AND deleted_at IS NULL AND base_role<>?",
                (base_role, now, workspace, base_role),
            ).rowcount
            groups = self._conn.execute(
                "SELECT scim_group_id,display_name,mapped_role FROM auth_scim_groups "
                "WHERE workspace_id=? AND deleted_at IS NULL",
                (workspace,),
            ).fetchall()
            for group_id, display_name, current_role in groups:
                desired = clean_map.get(str(display_name).casefold())
                if desired == current_role:
                    continue
                affected.update(
                    str(row[0])
                    for row in self._conn.execute(
                        "SELECT scim_user_id FROM auth_scim_group_members "
                        "WHERE scim_group_id=?",
                        (group_id,),
                    ).fetchall()
                )
                self._conn.execute(
                    "UPDATE auth_scim_groups SET mapped_role=?,revision=revision+1,"
                    "updated_at=? WHERE scim_group_id=?",
                    (desired, now, group_id),
                )
                changed += 1
            for scim_user_id in affected:
                self._sync_scim_membership_locked(scim_user_id, now)
            return int(changed)

    @staticmethod
    def _service_account(row: sqlite3.Row | tuple[Any, ...]) -> dict[str, Any]:
        return {
            "service_account_id": str(row[0]),
            "workspace_id": str(row[1]),
            "name": str(row[2]),
            "description": str(row[3]),
            "role": str(row[4]),
            "active": bool(row[5]),
            "revision": int(row[6]),
            "created_by": str(row[7]),
            "created_at": _iso(float(row[8])),
            "updated_at": _iso(float(row[9])),
        }

    @staticmethod
    def _service_permissions(value: object) -> list[str] | None:
        if value is None:
            return None
        try:
            decoded = json.loads(str(value))
            if not isinstance(decoded, list) or not decoded:
                raise ValueError
            permissions = frozenset(Permission(item) for item in decoded)
            if (
                len(permissions) != len(decoded)
                or Permission.MANAGE_TENANT in permissions
            ):
                raise ValueError
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise AuthStoreError("service token permission scope is invalid") from exc
        return sorted(item.value for item in permissions)

    @staticmethod
    def _service_token(
        row: sqlite3.Row | tuple[Any, ...], *, now: float
    ) -> dict[str, Any]:
        revoked_at = None if row[7] is None else float(row[7])
        expires_at = None if row[5] is None else float(row[5])
        status = "revoked" if revoked_at is not None else "active"
        if status == "active" and expires_at is not None and expires_at <= now:
            status = "expired"
        return {
            "token_id": str(row[0]),
            "service_account_id": str(row[1]),
            "label": str(row[2]),
            "secret_hint": str(row[3]),
            "revision": int(row[4]),
            "expires_at": None if expires_at is None else _iso(expires_at),
            "last_used_at": None if row[6] is None else _iso(float(row[6])),
            "revoked_at": None if revoked_at is None else _iso(revoked_at),
            "created_at": _iso(float(row[8])),
            "status": status,
            "permissions": AuthStore._service_permissions(
                None if len(row) < 10 else row[9]
            ),
        }

    def _service_account_row_locked(
        self, workspace_id: str, service_account_id: str
    ) -> tuple[Any, ...]:
        row = self._conn.execute(
            "SELECT service_account_id,workspace_id,name,description,role,active,"
            "revision,created_by,created_at,updated_at FROM auth_service_accounts "
            "WHERE workspace_id=? AND service_account_id=? AND deleted_at IS NULL",
            (workspace_id, service_account_id),
        ).fetchone()
        if row is None:
            raise AuthNotFoundError("service account not found")
        return row

    def get_workspace_session_policy(
        self, *, workspace_id: str, actor_user_id: str
    ) -> dict[str, Any]:
        workspace = _clean_id(workspace_id, field="workspace_id")
        actor = _clean_id(actor_user_id, field="user_id")
        with self._lock:
            self._ensure_open()
            self._require_manager(workspace, actor)
            return self._session_policy_locked(workspace)

    def set_workspace_session_policy(
        self,
        *,
        workspace_id: str,
        idle_timeout_minutes: int | None,
        absolute_timeout_hours: int | None,
        max_active_sessions: int | None,
        expected_revision: int,
        actor_user_id: str,
    ) -> dict[str, Any]:
        workspace = _clean_id(workspace_id, field="workspace_id")
        actor = _clean_id(actor_user_id, field="user_id")
        for value, lower, upper, field in (
            (idle_timeout_minutes, 5, 43_200, "idle_timeout_minutes"),
            (absolute_timeout_hours, 1, 8_760, "absolute_timeout_hours"),
            (max_active_sessions, 1, 50, "max_active_sessions"),
        ):
            if value is not None and (
                type(value) is not int or not lower <= value <= upper
            ):
                raise AuthValidationError(
                    f"{field} must be null or between {lower} and {upper}"
                )
        if type(expected_revision) is not int or expected_revision < 0:
            raise AuthValidationError("invalid workspace session policy revision")
        now = self._now()
        with self._lock, self._transaction():
            self._require_manager(workspace, actor)
            current = self._session_policy_locked(workspace)
            if int(current["revision"]) != expected_revision:
                raise AuthConflictError("workspace session policy revision conflict")
            if expected_revision == 0:
                self._conn.execute(
                    "INSERT INTO auth_workspace_session_policies(workspace_id,"
                    "idle_timeout_minutes,absolute_timeout_hours,max_active_sessions,"
                    "revision,created_at,updated_at) VALUES(?,?,?,?,1,?,?)",
                    (
                        workspace,
                        idle_timeout_minutes,
                        absolute_timeout_hours,
                        max_active_sessions,
                        now,
                        now,
                    ),
                )
            else:
                changed = self._conn.execute(
                    "UPDATE auth_workspace_session_policies SET idle_timeout_minutes=?,"
                    "absolute_timeout_hours=?,max_active_sessions=?,revision=revision+1,"
                    "updated_at=? WHERE workspace_id=? AND revision=?",
                    (
                        idle_timeout_minutes,
                        absolute_timeout_hours,
                        max_active_sessions,
                        now,
                        workspace,
                        expected_revision,
                    ),
                ).rowcount
                if changed != 1:
                    raise AuthConflictError(
                        "workspace session policy revision conflict"
                    )
            if absolute_timeout_hours is not None:
                self._conn.execute(
                    "UPDATE auth_sessions SET expires_at=MIN(expires_at,created_at+?) "
                    "WHERE active_workspace_id=? AND revoked_at IS NULL",
                    (absolute_timeout_hours * 3600, workspace),
                )
            self._enforce_session_policy_locked(workspace, now=now)
            return self._session_policy_locked(workspace)

    def _service_account_policy_locked(self, workspace_id: str) -> dict[str, Any]:
        row = self._conn.execute(
            "SELECT max_accounts,max_tokens_per_account,max_token_ttl_days,"
            "allow_non_expiring,allowed_permissions_json,revision,created_at,updated_at "
            "FROM auth_service_account_policies WHERE workspace_id=?",
            (workspace_id,),
        ).fetchone()
        if row is None:
            permissions = sorted(item.value for item in ROLE_PERMISSIONS[Role.ADMIN])
            return {
                "workspace_id": workspace_id,
                "max_accounts": 100,
                "max_tokens_per_account": 10,
                "max_token_ttl_days": 365,
                "allow_non_expiring": True,
                "allowed_permissions": permissions,
                "revision": 0,
                "created_at": None,
                "updated_at": None,
            }
        parsed_permissions = self._service_permissions(row[4])
        if parsed_permissions is None:
            raise AuthStoreError("service account policy permissions are invalid")
        return {
            "workspace_id": workspace_id,
            "max_accounts": int(row[0]),
            "max_tokens_per_account": int(row[1]),
            "max_token_ttl_days": int(row[2]),
            "allow_non_expiring": bool(row[3]),
            "allowed_permissions": parsed_permissions,
            "revision": int(row[5]),
            "created_at": _iso(float(row[6])),
            "updated_at": _iso(float(row[7])),
        }

    def get_service_account_policy(
        self, *, workspace_id: str, actor_user_id: str
    ) -> dict[str, Any]:
        workspace = _clean_id(workspace_id, field="workspace_id")
        actor = _clean_id(actor_user_id, field="user_id")
        with self._lock:
            self._ensure_open()
            self._require_manager(workspace, actor)
            return self._service_account_policy_locked(workspace)

    def set_service_account_policy(
        self,
        *,
        workspace_id: str,
        max_accounts: int,
        max_tokens_per_account: int,
        max_token_ttl_days: int,
        allow_non_expiring: bool,
        allowed_permissions: Sequence[str],
        expected_revision: int,
        actor_user_id: str,
    ) -> dict[str, Any]:
        workspace = _clean_id(workspace_id, field="workspace_id")
        actor = _clean_id(actor_user_id, field="user_id")
        if type(max_accounts) is not int or not 1 <= max_accounts <= 500:
            raise AuthValidationError("max_accounts must be between 1 and 500")
        if (
            type(max_tokens_per_account) is not int
            or not 1 <= max_tokens_per_account <= 50
        ):
            raise AuthValidationError("max_tokens_per_account must be between 1 and 50")
        if type(max_token_ttl_days) is not int or not 1 <= max_token_ttl_days <= 365:
            raise AuthValidationError("max_token_ttl_days must be between 1 and 365")
        if type(allow_non_expiring) is not bool:
            raise AuthValidationError("allow_non_expiring must be boolean")
        if type(expected_revision) is not int or expected_revision < 0:
            raise AuthValidationError("invalid service account policy revision")
        try:
            permissions = frozenset(Permission(item) for item in allowed_permissions)
        except (TypeError, ValueError) as exc:
            raise AuthValidationError(
                "invalid service account policy permission"
            ) from exc
        if (
            not permissions
            or Permission.MANAGE_TENANT in permissions
            or not permissions <= ROLE_PERMISSIONS[Role.ADMIN]
        ):
            raise AuthValidationError("policy permissions must be an admin-role subset")
        encoded = json.dumps(
            sorted(item.value for item in permissions), separators=(",", ":")
        )
        now = self._now()
        with self._lock, self._transaction():
            self._require_manager(workspace, actor)
            current = self._service_account_policy_locked(workspace)
            if int(current["revision"]) != expected_revision:
                raise AuthConflictError("service account policy revision conflict")
            if expected_revision == 0:
                self._conn.execute(
                    "INSERT INTO auth_service_account_policies(workspace_id,max_accounts,"
                    "max_tokens_per_account,max_token_ttl_days,allow_non_expiring,"
                    "allowed_permissions_json,revision,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,1,?,?)",
                    (
                        workspace,
                        max_accounts,
                        max_tokens_per_account,
                        max_token_ttl_days,
                        int(allow_non_expiring),
                        encoded,
                        now,
                        now,
                    ),
                )
            else:
                changed = self._conn.execute(
                    "UPDATE auth_service_account_policies SET max_accounts=?,"
                    "max_tokens_per_account=?,max_token_ttl_days=?,allow_non_expiring=?,"
                    "allowed_permissions_json=?,revision=revision+1,updated_at=? "
                    "WHERE workspace_id=? AND revision=?",
                    (
                        max_accounts,
                        max_tokens_per_account,
                        max_token_ttl_days,
                        int(allow_non_expiring),
                        encoded,
                        now,
                        workspace,
                        expected_revision,
                    ),
                ).rowcount
                if changed != 1:
                    raise AuthConflictError("service account policy revision conflict")
            return self._service_account_policy_locked(workspace)

    def create_service_account(
        self,
        *,
        workspace_id: str,
        name: str,
        description: str = "",
        role: Role | str = Role.VIEWER,
        actor_user_id: str,
    ) -> dict[str, Any]:
        workspace = _clean_id(workspace_id, field="workspace_id")
        actor = _clean_id(actor_user_id, field="user_id")
        clean_name = _clean_text(name, field="service_account_name")
        clean_description = (
            ""
            if not description
            else _clean_text(description, field="description", maximum=500)
        )
        clean_role = _clean_role(role).value
        now, identifier = self._now(), _new_id("svc")
        with self._lock:
            try:
                with self._transaction():
                    self._require_manager(workspace, actor)
                    policy = self._service_account_policy_locked(workspace)
                    count = int(
                        self._conn.execute(
                            "SELECT COUNT(*) FROM auth_service_accounts "
                            "WHERE workspace_id=? AND deleted_at IS NULL",
                            (workspace,),
                        ).fetchone()[0]
                    )
                    if count >= int(policy["max_accounts"]):
                        raise AuthConflictError(
                            "workspace has too many service accounts"
                        )
                    self._conn.execute(
                        "INSERT INTO auth_service_accounts(service_account_id,workspace_id,"
                        "name,description,role,active,revision,created_by,created_at,updated_at,"
                        "deleted_at) VALUES(?,?,?,?,?,1,1,?,?,?,NULL)",
                        (
                            identifier,
                            workspace,
                            clean_name,
                            clean_description,
                            clean_role,
                            actor,
                            now,
                            now,
                        ),
                    )
            except sqlite3.IntegrityError as exc:
                raise AuthConflictError("service account name already exists") from exc
        return self.get_service_account(
            workspace_id=workspace,
            service_account_id=identifier,
            actor_user_id=actor,
        )

    def get_service_account(
        self,
        *,
        workspace_id: str,
        service_account_id: str,
        actor_user_id: str,
    ) -> dict[str, Any]:
        workspace = _clean_id(workspace_id, field="workspace_id")
        identifier = _clean_id(service_account_id, field="service_account_id")
        actor = _clean_id(actor_user_id, field="user_id")
        with self._lock:
            self._ensure_open()
            self._require_manager(workspace, actor)
            return self._service_account(
                self._service_account_row_locked(workspace, identifier)
            )

    def list_service_accounts(
        self, *, workspace_id: str, actor_user_id: str
    ) -> list[dict[str, Any]]:
        workspace = _clean_id(workspace_id, field="workspace_id")
        actor = _clean_id(actor_user_id, field="user_id")
        with self._lock:
            self._ensure_open()
            self._require_manager(workspace, actor)
            rows = self._conn.execute(
                "SELECT service_account_id,workspace_id,name,description,role,active,"
                "revision,created_by,created_at,updated_at FROM auth_service_accounts "
                "WHERE workspace_id=? AND deleted_at IS NULL "
                "ORDER BY name,service_account_id",
                (workspace,),
            ).fetchall()
            return [self._service_account(row) for row in rows]

    def update_service_account(
        self,
        *,
        workspace_id: str,
        service_account_id: str,
        name: str,
        description: str,
        role: Role | str,
        active: bool,
        expected_revision: int,
        actor_user_id: str,
    ) -> dict[str, Any]:
        workspace = _clean_id(workspace_id, field="workspace_id")
        identifier = _clean_id(service_account_id, field="service_account_id")
        actor = _clean_id(actor_user_id, field="user_id")
        clean_name = _clean_text(name, field="service_account_name")
        clean_description = (
            ""
            if not description
            else _clean_text(description, field="description", maximum=500)
        )
        clean_role = _clean_role(role).value
        if (
            type(active) is not bool
            or type(expected_revision) is not int
            or expected_revision < 1
        ):
            raise AuthValidationError(
                "invalid service account revision or active state"
            )
        now = self._now()
        with self._lock:
            try:
                with self._transaction():
                    self._require_manager(workspace, actor)
                    self._service_account_row_locked(workspace, identifier)
                    changed = self._conn.execute(
                        "UPDATE auth_service_accounts SET name=?,description=?,role=?,active=?,"
                        "revision=revision+1,updated_at=? WHERE workspace_id=? "
                        "AND service_account_id=? AND revision=? AND deleted_at IS NULL",
                        (
                            clean_name,
                            clean_description,
                            clean_role,
                            int(active),
                            now,
                            workspace,
                            identifier,
                            expected_revision,
                        ),
                    ).rowcount
                    if changed != 1:
                        raise AuthConflictError("service account version conflict")
                    if not active:
                        self._conn.execute(
                            "UPDATE auth_service_tokens SET revoked_at=?,"
                            "revision=revision+1 WHERE service_account_id=? "
                            "AND revoked_at IS NULL",
                            (now, identifier),
                        )
            except sqlite3.IntegrityError as exc:
                raise AuthConflictError("service account name already exists") from exc
        return self.get_service_account(
            workspace_id=workspace,
            service_account_id=identifier,
            actor_user_id=actor,
        )

    def delete_service_account(
        self,
        *,
        workspace_id: str,
        service_account_id: str,
        expected_revision: int,
        actor_user_id: str,
    ) -> bool:
        workspace = _clean_id(workspace_id, field="workspace_id")
        identifier = _clean_id(service_account_id, field="service_account_id")
        actor = _clean_id(actor_user_id, field="user_id")
        if type(expected_revision) is not int or expected_revision < 1:
            raise AuthValidationError("invalid service account revision")
        now = self._now()
        with self._lock, self._transaction():
            self._require_manager(workspace, actor)
            self._service_account_row_locked(workspace, identifier)
            changed = self._conn.execute(
                "UPDATE auth_service_accounts SET active=0,deleted_at=?,"
                "revision=revision+1,updated_at=? WHERE workspace_id=? "
                "AND service_account_id=? AND revision=? AND deleted_at IS NULL",
                (now, now, workspace, identifier, expected_revision),
            ).rowcount
            if changed != 1:
                raise AuthConflictError("service account version conflict")
            self._conn.execute(
                "UPDATE auth_service_tokens SET revoked_at=?,revision=revision+1 "
                "WHERE service_account_id=? AND revoked_at IS NULL",
                (now, identifier),
            )
            return True

    def create_service_token(
        self,
        *,
        workspace_id: str,
        service_account_id: str,
        label: str,
        ttl_seconds: float | None,
        actor_user_id: str,
        permissions: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        workspace = _clean_id(workspace_id, field="workspace_id")
        identifier = _clean_id(service_account_id, field="service_account_id")
        actor = _clean_id(actor_user_id, field="user_id")
        clean_label = _clean_text(label, field="token_label")
        ttl = (
            None
            if ttl_seconds is None
            else self._positive_duration(ttl_seconds, "ttl_seconds")
        )
        now = self._now()
        raw_token, token_id = _new_token("cog_svc"), _new_id("svt")
        digest = _token_hash(raw_token)
        expires_at = None if ttl is None else now + ttl
        with self._lock, self._transaction():
            self._require_manager(workspace, actor)
            account = self._service_account_row_locked(workspace, identifier)
            policy = self._service_account_policy_locked(workspace)
            if not bool(account[5]):
                raise AuthConflictError("service account is disabled")
            if ttl is None and not bool(policy["allow_non_expiring"]):
                raise AuthValidationError("non-expiring service tokens are disabled")
            if ttl is not None and ttl > int(policy["max_token_ttl_days"]) * 86400:
                raise AuthValidationError("service token TTL exceeds workspace policy")
            active_count = int(
                self._conn.execute(
                    "SELECT COUNT(*) FROM auth_service_tokens WHERE service_account_id=? "
                    "AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at>?)",
                    (identifier, now),
                ).fetchone()[0]
            )
            if active_count >= int(policy["max_tokens_per_account"]):
                raise AuthConflictError("service account has too many active tokens")
            role_permissions = ROLE_PERMISSIONS[Role(str(account[4]))]
            policy_permissions = frozenset(
                Permission(item) for item in policy["allowed_permissions"]
            )
            if permissions is None:
                token_permissions = role_permissions & policy_permissions
            else:
                try:
                    token_permissions = frozenset(
                        Permission(item) for item in permissions
                    )
                except (TypeError, ValueError) as exc:
                    raise AuthValidationError(
                        "invalid service token permission"
                    ) from exc
                if not token_permissions or not token_permissions <= role_permissions:
                    raise AuthValidationError(
                        "service token permissions must be a non-empty role subset"
                    )
            if not token_permissions:
                raise AuthValidationError(
                    "service token has no permission allowed by workspace policy"
                )
            if not token_permissions <= policy_permissions:
                raise AuthValidationError(
                    "service token permissions exceed workspace policy"
                )
            self._conn.execute(
                "INSERT INTO auth_service_tokens(token_id,service_account_id,token_hash,"
                "label,secret_hint,revision,created_at,expires_at,last_used_at,revoked_at,permissions_json) "
                "VALUES(?,?,?,?,?,1,?,?,NULL,NULL,?)",
                (
                    token_id,
                    identifier,
                    digest,
                    clean_label,
                    f"cog_svc_…{raw_token[-4:]}",
                    now,
                    expires_at,
                    json.dumps(
                        sorted(item.value for item in token_permissions),
                        separators=(",", ":"),
                    ),
                ),
            )
            row = self._conn.execute(
                "SELECT token_id,service_account_id,label,secret_hint,revision,expires_at,"
                "last_used_at,revoked_at,created_at,permissions_json FROM auth_service_tokens "
                "WHERE token_id=?",
                (token_id,),
            ).fetchone()
            if row is None:
                raise AuthStoreError("service token creation failed")
            metadata = self._service_token(row, now=now)
        return {**metadata, "token": raw_token}

    def list_service_tokens(
        self,
        *,
        workspace_id: str,
        service_account_id: str,
        actor_user_id: str,
    ) -> list[dict[str, Any]]:
        workspace = _clean_id(workspace_id, field="workspace_id")
        identifier = _clean_id(service_account_id, field="service_account_id")
        actor = _clean_id(actor_user_id, field="user_id")
        now = self._now()
        with self._lock:
            self._ensure_open()
            self._require_manager(workspace, actor)
            self._service_account_row_locked(workspace, identifier)
            rows = self._conn.execute(
                "SELECT token_id,service_account_id,label,secret_hint,revision,expires_at,"
                "last_used_at,revoked_at,created_at,permissions_json FROM auth_service_tokens "
                "WHERE service_account_id=? ORDER BY created_at DESC,token_id DESC",
                (identifier,),
            ).fetchall()
            return [self._service_token(row, now=now) for row in rows]

    def revoke_service_token(
        self,
        *,
        workspace_id: str,
        service_account_id: str,
        token_id: str,
        expected_revision: int,
        actor_user_id: str,
    ) -> bool:
        workspace = _clean_id(workspace_id, field="workspace_id")
        account_id = _clean_id(service_account_id, field="service_account_id")
        identifier = _clean_id(token_id, field="token_id")
        actor = _clean_id(actor_user_id, field="user_id")
        if type(expected_revision) is not int or expected_revision < 1:
            raise AuthValidationError("invalid service token revision")
        now = self._now()
        with self._lock, self._transaction():
            self._require_manager(workspace, actor)
            self._service_account_row_locked(workspace, account_id)
            row = self._conn.execute(
                "SELECT revision,revoked_at FROM auth_service_tokens "
                "WHERE token_id=? AND service_account_id=?",
                (identifier, account_id),
            ).fetchone()
            if row is None:
                raise AuthNotFoundError("service token not found")
            if int(row[0]) != expected_revision:
                raise AuthConflictError("service token version conflict")
            if row[1] is not None:
                return False
            changed = self._conn.execute(
                "UPDATE auth_service_tokens SET revoked_at=?,revision=revision+1 "
                "WHERE token_id=? AND service_account_id=? AND revision=? "
                "AND revoked_at IS NULL",
                (now, identifier, account_id, expected_revision),
            ).rowcount
            if changed != 1:
                raise AuthConflictError("service token version conflict")
            return True

    def authenticate_service_token(
        self, token: str, workspace_id: str | None = None
    ) -> ServiceAccountAuthContext:
        if (
            type(token) is not str
            or not token.startswith("cog_svc_")
            or len(token) > 256
        ):
            raise AuthAuthenticationError("invalid service token")
        target = (
            None
            if workspace_id is None
            else _clean_id(workspace_id, field="workspace_id")
        )
        digest, now = _token_hash(token), self._now()
        with self._lock:
            self._ensure_open()
            row = self._conn.execute(
                "SELECT a.service_account_id,a.workspace_id,a.name,a.description,a.role,"
                "a.active,a.revision,a.created_by,a.created_at,a.updated_at,"
                "t.token_id,t.label,t.secret_hint,t.revision,t.expires_at,t.last_used_at,"
                "t.revoked_at,t.created_at,w.name,w.created_at,w.updated_at,w.revision "
                ",t.permissions_json "
                "FROM auth_service_tokens t JOIN auth_service_accounts a "
                "ON a.service_account_id=t.service_account_id JOIN auth_workspaces w "
                "ON w.workspace_id=a.workspace_id WHERE t.token_hash=? "
                "AND t.revoked_at IS NULL AND a.active=1 AND a.deleted_at IS NULL "
                "AND (t.expires_at IS NULL OR t.expires_at>?)",
                (digest, now),
            ).fetchone()
            if row is None:
                raise AuthAuthenticationError("invalid or expired service token")
            account_workspace = str(row[1])
            if target is not None and target != account_workspace:
                raise AuthAuthorizationError(
                    "service token is outside target workspace"
                )
            if row[15] is None or now - float(row[15]) >= 300:
                self._conn.execute(
                    "UPDATE auth_service_tokens SET last_used_at=? WHERE token_id=? "
                    "AND (last_used_at IS NULL OR last_used_at<=?)",
                    (now, row[10], now - 300),
                )
            account = self._service_account(row[:10])
            token_metadata = self._service_token(
                (
                    row[10],
                    row[0],
                    row[11],
                    row[12],
                    row[13],
                    row[14],
                    now,
                    row[16],
                    row[17],
                    row[22],
                ),
                now=now,
            )
            workspace = {
                "workspace_id": account_workspace,
                "name": str(row[18]),
                "created_at": _iso(float(row[19])),
                "updated_at": _iso(float(row[20])),
                "revision": int(row[21]),
                "role": str(row[4]),
            }
            stored_permissions = self._service_permissions(row[22])
            policy_permissions = frozenset(
                Permission(item)
                for item in self._service_account_policy_locked(account_workspace)[
                    "allowed_permissions"
                ]
            )
            token_scope = (
                ROLE_PERMISSIONS[Role(str(row[4]))]
                if stored_permissions is None
                else frozenset(Permission(item) for item in stored_permissions)
            )
            principal = Principal(
                tenant_id=account_workspace,
                subject_id=f"service-account:{row[0]}",
                role=Role(str(row[4])),
                key_fingerprint=f"service-token:{row[10]}",
                permission_scope=token_scope & policy_permissions,
            )
            return ServiceAccountAuthContext(
                service_account=account,
                token=token_metadata,
                workspace=workspace,
                principal=principal,
            )

    def get_workspace(
        self, workspace_id: str, user_id: str | None = None
    ) -> dict[str, Any]:
        workspace_id = _clean_id(workspace_id, field="workspace_id")
        with self._lock:
            self._ensure_open()
            if user_id is None:
                row = self._conn.execute(
                    "SELECT workspace_id,name,created_at,updated_at,revision "
                    "FROM auth_workspaces WHERE workspace_id=?",
                    (workspace_id,),
                ).fetchone()
            else:
                user_id = _clean_id(user_id, field="user_id")
                row = self._conn.execute(
                    "SELECT w.workspace_id,w.name,w.created_at,w.updated_at,w.revision,m.role "
                    "FROM auth_workspaces w JOIN auth_memberships m "
                    "ON m.workspace_id=w.workspace_id WHERE w.workspace_id=? AND m.user_id=?",
                    (workspace_id, user_id),
                ).fetchone()
            if row is None:
                raise AuthNotFoundError("workspace not found")
            return self._workspace(row)

    def list_workspaces(self, user_id: str) -> list[dict[str, Any]]:
        user_id = _clean_id(user_id, field="user_id")
        with self._lock:
            self._ensure_open()
            rows = self._conn.execute(
                "SELECT w.workspace_id,w.name,w.created_at,w.updated_at,w.revision,m.role "
                "FROM auth_workspaces w JOIN auth_memberships m ON m.workspace_id=w.workspace_id "
                "WHERE m.user_id=? ORDER BY w.created_at,w.workspace_id",
                (user_id,),
            ).fetchall()
            return [self._workspace(row) for row in rows]

    def create_workspace(self, owner_user_id: str, name: str) -> dict[str, Any]:
        owner_user_id = _clean_id(owner_user_id, field="user_id")
        clean_name = _clean_text(name, field="workspace_name")
        workspace_id, member_id, now = _new_id("wsp"), _new_id("mem"), self._now()
        with self._lock, self._transaction():
            self._get_user_row(owner_user_id)
            self._conn.execute(
                "INSERT INTO auth_workspaces(workspace_id,name,personal_owner_user_id,revision,"
                "created_at,updated_at) VALUES(?,?,NULL,0,?,?)",
                (workspace_id, clean_name, now, now),
            )
            self._conn.execute(
                "INSERT INTO auth_memberships(member_id,workspace_id,user_id,role,revision,"
                "joined_at,updated_at) VALUES(?,?,?,'owner',0,?,?)",
                (member_id, workspace_id, owner_user_id, now, now),
            )
        return self.get_workspace(workspace_id, user_id=owner_user_id)

    def rename_workspace(
        self,
        workspace_id: str,
        name: str,
        actor_user_id: str,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        workspace_id = _clean_id(workspace_id, field="workspace_id")
        actor_user_id = _clean_id(actor_user_id, field="user_id")
        clean_name = _clean_text(name, field="workspace_name")
        if expected_revision is not None and (
            type(expected_revision) is not int or expected_revision < 0
        ):
            raise AuthValidationError("invalid expected_revision")
        now = self._now()
        with self._lock, self._transaction():
            self._require_manager(workspace_id, actor_user_id)
            row = self._conn.execute(
                "SELECT revision FROM auth_workspaces WHERE workspace_id=?",
                (workspace_id,),
            ).fetchone()
            if row is None:
                raise AuthNotFoundError("workspace not found")
            if expected_revision is not None and row[0] != expected_revision:
                raise AuthConflictError("workspace revision conflict")
            self._conn.execute(
                "UPDATE auth_workspaces SET name=?,revision=revision+1,updated_at=? "
                "WHERE workspace_id=?",
                (clean_name, now, workspace_id),
            )
        return self.get_workspace(workspace_id, user_id=actor_user_id)

    def delete_workspace(self, workspace_id: str, actor_user_id: str) -> bool:
        workspace_id = _clean_id(workspace_id, field="workspace_id")
        actor_user_id = _clean_id(actor_user_id, field="user_id")
        with self._lock, self._transaction():
            role = self._require_manager(workspace_id, actor_user_id)
            if role is not Role.OWNER:
                raise AuthAuthorizationError("only the workspace owner may delete it")
            row = self._conn.execute(
                "SELECT personal_owner_user_id FROM auth_workspaces WHERE workspace_id=?",
                (workspace_id,),
            ).fetchone()
            if row is None:
                raise AuthNotFoundError("workspace not found")
            if row[0] is not None:
                raise AuthConflictError("personal workspace cannot be deleted")
            member_count = self._conn.execute(
                "SELECT COUNT(*) FROM auth_memberships WHERE workspace_id=?",
                (workspace_id,),
            ).fetchone()[0]
            if member_count != 1:
                raise AuthConflictError("workspace must have no other members")
            self._conn.execute(
                "DELETE FROM auth_workspaces WHERE workspace_id=?", (workspace_id,)
            )
            return True

    def list_members(
        self, workspace_id: str, actor_user_id: str | None = None
    ) -> list[dict[str, Any]]:
        workspace_id = _clean_id(workspace_id, field="workspace_id")
        with self._lock:
            self._ensure_open()
            if (
                actor_user_id is not None
                and self._membership_row(
                    workspace_id, _clean_id(actor_user_id, field="user_id")
                )
                is None
            ):
                raise AuthAuthorizationError("workspace membership required")
            rows = self._conn.execute(
                "SELECT m.member_id,u.user_id,u.email,u.display_name,m.role,m.joined_at,"
                "m.updated_at,m.revision FROM auth_memberships m JOIN auth_users u "
                "ON u.user_id=m.user_id WHERE m.workspace_id=? "
                "ORDER BY m.joined_at,m.member_id",
                (workspace_id,),
            ).fetchall()
            return [self._membership(row) for row in rows]

    def membership(self, workspace_id: str, user_id: str) -> dict[str, Any] | None:
        """Return one active workspace membership for authorization services."""

        workspace_id = _clean_id(workspace_id, field="workspace_id")
        user_id = _clean_id(user_id, field="user_id")
        with self._lock:
            self._ensure_open()
            row = self._conn.execute(
                "SELECT m.member_id,u.user_id,u.email,u.display_name,m.role,"
                "m.joined_at,m.updated_at,m.revision FROM auth_memberships m "
                "JOIN auth_users u ON u.user_id=m.user_id "
                "WHERE m.workspace_id=? AND m.user_id=?",
                (workspace_id, user_id),
            ).fetchone()
        if row is None:
            return None
        return {"workspace_id": workspace_id, **self._membership(row)}

    get_member = membership

    def add_member(
        self,
        workspace_id: str,
        user_id: str,
        role: Role | str,
        actor_user_id: str,
    ) -> dict[str, Any]:
        workspace_id = _clean_id(workspace_id, field="workspace_id")
        user_id = _clean_id(user_id, field="user_id")
        actor_user_id = _clean_id(actor_user_id, field="user_id")
        clean_role = _clean_role(role)
        member_id, now = _new_id("mem"), self._now()
        with self._lock:
            try:
                with self._transaction():
                    self._require_manager(workspace_id, actor_user_id)
                    self._get_user_row(user_id)
                    self._conn.execute(
                        "INSERT INTO auth_memberships(member_id,workspace_id,user_id,role,"
                        "revision,joined_at,updated_at) VALUES(?,?,?,?,0,?,?)",
                        (member_id, workspace_id, user_id, clean_role.value, now, now),
                    )
            except sqlite3.IntegrityError as exc:
                raise AuthConflictError("user is already a workspace member") from exc
        return next(
            member
            for member in self.list_members(workspace_id)
            if member["user_id"] == user_id
        )

    def update_member_role(
        self,
        workspace_id: str,
        member_user_id: str,
        role: Role | str,
        actor_user_id: str,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        workspace_id = _clean_id(workspace_id, field="workspace_id")
        member_user_id = _clean_id(member_user_id, field="member_id")
        actor_user_id = _clean_id(actor_user_id, field="user_id")
        clean_role = _clean_role(role)
        if expected_revision is not None and (
            type(expected_revision) is not int or expected_revision < 0
        ):
            raise AuthValidationError("invalid expected_revision")
        now = self._now()
        with self._lock, self._transaction():
            actor_role = self._require_manager(workspace_id, actor_user_id)
            target = self._membership_target_row(workspace_id, member_user_id)
            if target is None:
                raise AuthNotFoundError("workspace member not found")
            target_member_id, target_user_id, target_role, target_revision = target
            if target_role == Role.OWNER.value:
                raise AuthAuthorizationError("workspace owner cannot be demoted")
            if actor_role is Role.ADMIN and clean_role is Role.OWNER:
                raise AuthAuthorizationError("admin cannot grant owner")
            if expected_revision is not None and target_revision != expected_revision:
                raise AuthConflictError("membership revision conflict")
            self._conn.execute(
                "UPDATE auth_memberships SET role=?,revision=revision+1,updated_at=? "
                "WHERE member_id=?",
                (clean_role.value, now, target_member_id),
            )
            # An explicit administrator change hands role authority back to
            # CogDoc. Future OIDC logins authenticate but do not rewrite it.
            self._conn.execute(
                "DELETE FROM auth_oidc_managed_memberships WHERE member_id=?",
                (target_member_id,),
            )
        return next(
            member
            for member in self.list_members(workspace_id)
            if member["user_id"] == target_user_id
        )

    def remove_member(
        self, workspace_id: str, member_user_id: str, actor_user_id: str
    ) -> bool:
        workspace_id = _clean_id(workspace_id, field="workspace_id")
        member_user_id = _clean_id(member_user_id, field="member_id")
        actor_user_id = _clean_id(actor_user_id, field="user_id")
        with self._lock, self._transaction():
            actor_role = self._require_manager(workspace_id, actor_user_id)
            target = self._membership_target_row(workspace_id, member_user_id)
            if target is None:
                raise AuthNotFoundError("workspace member not found")
            target_member_id, target_user_id, target_role, _ = target
            if target_role == Role.OWNER.value:
                raise AuthAuthorizationError("last workspace owner cannot be removed")
            if actor_role is Role.ADMIN and target_role == Role.OWNER.value:
                raise AuthAuthorizationError("admin cannot manage owner")
            self._conn.execute(
                "DELETE FROM auth_memberships WHERE member_id=?",
                (target_member_id,),
            )
            self._conn.execute(
                "UPDATE auth_sessions SET active_workspace_id=NULL "
                "WHERE user_id=? AND active_workspace_id=?",
                (target_user_id, workspace_id),
            )
            return True

    def create_invite(
        self,
        workspace_id: str,
        email: str,
        role: Role | str,
        actor_user_id: str,
        ttl_seconds: float | None = None,
    ) -> dict[str, Any]:
        workspace_id = _clean_id(workspace_id, field="workspace_id")
        actor_user_id = _clean_id(actor_user_id, field="user_id")
        clean_email = normalize_email(email)
        clean_role = _clean_role(role)
        duration = (
            self.invite_ttl_seconds
            if ttl_seconds is None
            else self._positive_duration(ttl_seconds, "ttl_seconds")
        )
        now, invite_id, token = self._now(), _new_id("inv"), _new_token("cgi")
        with self._lock, self._transaction():
            self._require_manager(workspace_id, actor_user_id)
            existing = self._conn.execute(
                "SELECT 1 FROM auth_memberships m JOIN auth_users u ON u.user_id=m.user_id "
                "WHERE m.workspace_id=? AND u.email=?",
                (workspace_id, clean_email),
            ).fetchone()
            if existing is not None:
                raise AuthConflictError("email already belongs to a workspace member")
            self._conn.execute(
                "INSERT INTO auth_invites(invite_id,workspace_id,email,role,token_hash,"
                "created_by,created_at,expires_at,accepted_at,accepted_by,revoked_at) "
                "VALUES(?,?,?,?,?,?,?,?,NULL,NULL,NULL)",
                (
                    invite_id,
                    workspace_id,
                    clean_email,
                    clean_role.value,
                    _token_hash(token),
                    actor_user_id,
                    now,
                    now + duration,
                ),
            )
            row = self._conn.execute(
                "SELECT invite_id,workspace_id,email,role,created_by,token_hash,created_at,"
                "expires_at,accepted_at,accepted_by,revoked_at FROM auth_invites "
                "WHERE invite_id=?",
                (invite_id,),
            ).fetchone()
        return {"invite": self._invite(row, now), "invite_token": token}

    def list_invites(
        self, workspace_id: str, actor_user_id: str
    ) -> list[dict[str, Any]]:
        workspace_id = _clean_id(workspace_id, field="workspace_id")
        actor_user_id = _clean_id(actor_user_id, field="user_id")
        now = self._now()
        with self._lock:
            self._ensure_open()
            self._require_manager(workspace_id, actor_user_id)
            rows = self._conn.execute(
                "SELECT invite_id,workspace_id,email,role,created_by,token_hash,created_at,"
                "expires_at,accepted_at,accepted_by,revoked_at FROM auth_invites "
                "WHERE workspace_id=? ORDER BY created_at DESC,invite_id DESC",
                (workspace_id,),
            ).fetchall()
            return [self._invite(row, now) for row in rows]

    def revoke_invite(
        self, workspace_id: str, invite_id: str, actor_user_id: str
    ) -> bool:
        workspace_id = _clean_id(workspace_id, field="workspace_id")
        invite_id = _clean_id(invite_id, field="invite_id")
        actor_user_id = _clean_id(actor_user_id, field="user_id")
        now = self._now()
        with self._lock, self._transaction():
            self._require_manager(workspace_id, actor_user_id)
            row = self._conn.execute(
                "SELECT accepted_at,revoked_at,expires_at FROM auth_invites "
                "WHERE workspace_id=? AND invite_id=?",
                (workspace_id, invite_id),
            ).fetchone()
            if row is None:
                raise AuthNotFoundError("invitation not found")
            if row[0] is not None or row[1] is not None or row[2] <= now:
                raise AuthInviteError("invitation is no longer pending")
            self._conn.execute(
                "UPDATE auth_invites SET revoked_at=? WHERE invite_id=?",
                (now, invite_id),
            )
            return True

    def accept_invite(
        self,
        token: str,
        user_id: str | None = None,
        *,
        email: str | None = None,
        password: str | None = None,
        display_name: str | None = None,
    ) -> dict[str, Any]:
        digest = _token_hash(token)
        if user_id is not None:
            user_id = _clean_id(user_id, field="user_id")
        clean_email = normalize_email(email) if email is not None else None
        if user_id is not None and (password is not None or display_name is not None):
            raise AuthValidationError(
                "logged-in invitation acceptance must not include account credentials"
            )
        if user_id is None and (clean_email is None or password is None):
            raise AuthValidationError(
                "anonymous invitation acceptance requires email and password"
            )
        candidate_password = (
            _clean_password(password, enforce_minimum=False)
            if password is not None
            else None
        )
        clean_display = (
            _clean_text(display_name, field="display_name")
            if display_name is not None
            else None
        )
        now, member_id = self._now(), _new_id("mem")
        encoded_new_password: str | None = None
        new_account: tuple[str, str, str] | None = None
        existing_password_hash: str | None = None
        with self._lock:
            self._ensure_open()
            invite = self._conn.execute(
                "SELECT invite_id,workspace_id,email,role,accepted_at,revoked_at,expires_at "
                "FROM auth_invites WHERE token_hash=?",
                (digest,),
            ).fetchone()
            if invite is None:
                raise AuthInviteError("invalid invitation")
            if invite[4] is not None or invite[5] is not None:
                raise AuthInviteError("invitation was already consumed or revoked")
            if invite[6] <= now:
                raise AuthInviteError("invitation has expired")
            if clean_email is not None and clean_email != invite[2]:
                raise AuthAuthorizationError("invitation email does not match")
            if user_id is None:
                user = self._conn.execute(
                    "SELECT user_id,email,password_hash,failed_login_count,locked_until "
                    "FROM auth_users WHERE email=?",
                    (invite[2],),
                ).fetchone()
            else:
                user = self._conn.execute(
                    "SELECT user_id,email FROM auth_users WHERE user_id=?", (user_id,)
                ).fetchone()
            if user is not None and user[1] != invite[2]:
                raise AuthAuthorizationError("invitation is bound to another email")
            if user_id is not None and user is None:
                raise AuthNotFoundError("invited user does not exist")

            if user_id is None and user is None:
                # A new password is checked against the registration contract,
                # not the looser login-input contract used above.
                assert password is not None
                _clean_password(password)
                if clean_display is None:
                    raise AuthValidationError(
                        "display_name is required when invitation creates an account"
                    )
                new_user_id = _new_id("usr")
                personal_workspace_id = _new_id("wsp")
                personal_member_id = _new_id("mem")
                new_account = (
                    new_user_id,
                    personal_workspace_id,
                    personal_member_id,
                )
                encoded_new_password = _hash_password(password, self.scrypt_params)
                accepted_user_id = new_user_id
            else:
                accepted_user_id = str(user[0])

            if user_id is None and user is not None:
                assert candidate_password is not None
                existing_password_hash = str(user[2])
                valid_password = _verify_password(
                    candidate_password, existing_password_hash
                )
                locked_until = user[4]
                if locked_until is not None and locked_until > now:
                    raise AuthLockedError("login is temporarily locked")
                if not valid_password:
                    with self._transaction():
                        # Invitation login shares the ordinary password lockout
                        # counters rather than creating a second brute-force path.
                        fresh = self._conn.execute(
                            "SELECT failed_login_count,locked_until FROM auth_users "
                            "WHERE user_id=?",
                            (accepted_user_id,),
                        ).fetchone()
                        prior = 0 if fresh[1] is not None else int(fresh[0])
                        failures = prior + 1
                        new_locked_until = (
                            now + self.lockout_seconds
                            if failures >= self.max_failed_logins
                            else None
                        )
                        self._conn.execute(
                            "UPDATE auth_users SET failed_login_count=?,locked_until=?,"
                            "updated_at=? WHERE user_id=?",
                            (failures, new_locked_until, now, accepted_user_id),
                        )
                    if new_locked_until is not None:
                        raise AuthLockedError("login is temporarily locked")
                    raise AuthAuthenticationError("invalid email or password")

            try:
                with self._transaction():
                    # Re-check the opaque capability inside the write transaction;
                    # this is the authoritative one-time-consumption boundary even
                    # when two AuthStore instances share the same database.
                    current_invite = self._conn.execute(
                        "SELECT accepted_at,revoked_at,expires_at FROM auth_invites "
                        "WHERE invite_id=? AND token_hash=?",
                        (invite[0], digest),
                    ).fetchone()
                    if (
                        current_invite is None
                        or current_invite[0] is not None
                        or current_invite[1] is not None
                        or current_invite[2] <= now
                    ):
                        raise AuthInviteError("invitation is no longer pending")

                    if new_account is not None:
                        new_user_id, personal_workspace_id, personal_member_id = (
                            new_account
                        )
                        self._conn.execute(
                            "INSERT INTO auth_users(user_id,email,display_name,password_hash,"
                            "personal_workspace_id,failed_login_count,locked_until,created_at,"
                            "updated_at) VALUES(?,?,?,?,?,0,NULL,?,?)",
                            (
                                new_user_id,
                                invite[2],
                                clean_display,
                                encoded_new_password,
                                personal_workspace_id,
                                now,
                                now,
                            ),
                        )
                        self._conn.execute(
                            "INSERT INTO auth_workspaces(workspace_id,name,"
                            "personal_owner_user_id,revision,created_at,updated_at) "
                            "VALUES(?,?,?,0,?,?)",
                            (
                                personal_workspace_id,
                                f"{clean_display} Workspace",
                                new_user_id,
                                now,
                                now,
                            ),
                        )
                        self._conn.execute(
                            "INSERT INTO auth_memberships(member_id,workspace_id,user_id,role,"
                            "revision,joined_at,updated_at) VALUES(?,?,?,'owner',0,?,?)",
                            (
                                personal_member_id,
                                personal_workspace_id,
                                new_user_id,
                                now,
                                now,
                            ),
                        )
                    elif existing_password_hash is not None:
                        current_user = self._conn.execute(
                            "SELECT password_hash,locked_until FROM auth_users WHERE user_id=?",
                            (accepted_user_id,),
                        ).fetchone()
                        if (
                            current_user is None
                            or not hmac.compare_digest(
                                current_user[0], existing_password_hash
                            )
                            or (current_user[1] is not None and current_user[1] > now)
                        ):
                            raise AuthAuthenticationError(
                                "account credentials changed during acceptance"
                            )
                        self._conn.execute(
                            "UPDATE auth_users SET failed_login_count=0,locked_until=NULL,"
                            "updated_at=? WHERE user_id=?",
                            (now, accepted_user_id),
                        )

                    if self._membership_row(invite[1], accepted_user_id) is not None:
                        raise AuthConflictError("user is already a workspace member")
                    self._conn.execute(
                        "INSERT INTO auth_memberships(member_id,workspace_id,user_id,role,"
                        "revision,joined_at,updated_at) VALUES(?,?,?,?,0,?,?)",
                        (
                            member_id,
                            invite[1],
                            accepted_user_id,
                            invite[3],
                            now,
                            now,
                        ),
                    )
                    changed = self._conn.execute(
                        "UPDATE auth_invites SET accepted_at=?,accepted_by=? WHERE invite_id=? "
                        "AND accepted_at IS NULL AND revoked_at IS NULL AND expires_at>?",
                        (now, accepted_user_id, invite[0], now),
                    ).rowcount
                    if changed != 1:
                        raise AuthInviteError("invitation is no longer pending")
                    if user_id is None:
                        session, access_token = self._issue_session_locked(
                            accepted_user_id, invite[1], now
                        )
                    else:
                        session, access_token = None, None
            except sqlite3.IntegrityError as exc:
                raise AuthConflictError(
                    "account or workspace membership already exists"
                ) from exc
            workspace_id = invite[1]
        member = next(
            member
            for member in self.list_members(workspace_id)
            if member["user_id"] == accepted_user_id
        )
        result: dict[str, Any] = {
            "member": member,
            "user": self.get_user(user_id=accepted_user_id),
            "workspace": self.get_workspace(workspace_id, user_id=accepted_user_id),
        }
        if access_token is not None and session is not None:
            result.update(
                {
                    "session": session,
                    "access_token": access_token,
                    "expires_at": session["expires_at"],
                }
            )
        return result

    def list_workspace_sessions(
        self,
        *,
        workspace_id: str,
        actor_user_id: str,
        limit: int = 50,
        before_session_id: str | None = None,
        include_inactive: bool = False,
    ) -> dict[str, Any]:
        workspace = _clean_id(workspace_id, field="workspace_id")
        actor = _clean_id(actor_user_id, field="user_id")
        before = (
            None
            if before_session_id is None
            else _clean_id(before_session_id, field="session_id")
        )
        if type(limit) is not int or not 1 <= limit <= 100:
            raise AuthValidationError("session page limit must be between 1 and 100")
        if type(include_inactive) is not bool:
            raise AuthValidationError("include_inactive must be boolean")
        now = self._now()
        with self._lock, self._transaction():
            self._require_manager(workspace, actor)
            self._enforce_session_policy_locked(workspace, now=now)
            active_clause = (
                ""
                if include_inactive
                else " AND s.revoked_at IS NULL AND s.expires_at>?"
            )
            base_params: list[Any] = [workspace]
            if not include_inactive:
                base_params.append(now)
            cursor_clause = ""
            cursor_params: list[Any] = []
            if before is not None:
                cursor = self._conn.execute(
                    "SELECT created_at FROM auth_sessions s WHERE s.session_id=? "
                    "AND s.active_workspace_id=?",
                    (before, workspace),
                ).fetchone()
                if cursor is None:
                    raise AuthNotFoundError("workspace session cursor not found")
                cursor_clause = (
                    " AND (s.created_at<? OR (s.created_at=? AND s.session_id<?))"
                )
                cursor_params = [float(cursor[0]), float(cursor[0]), before]
            total = int(
                self._conn.execute(
                    "SELECT COUNT(*) FROM auth_sessions s WHERE s.active_workspace_id=?"
                    + active_clause,
                    base_params,
                ).fetchone()[0]
            )
            rows = self._conn.execute(
                "SELECT s.session_id,s.user_id,u.email,u.display_name,m.role,"
                "s.created_at,s.last_seen_at,s.expires_at,s.revoked_at "
                "FROM auth_sessions s JOIN auth_users u "
                "ON u.user_id=s.user_id LEFT JOIN auth_memberships m "
                "ON m.workspace_id=s.active_workspace_id AND m.user_id=s.user_id "
                "WHERE s.active_workspace_id=?"
                + active_clause
                + cursor_clause
                + " ORDER BY s.created_at DESC,s.session_id DESC LIMIT ?",
                [*base_params, *cursor_params, limit + 1],
            ).fetchall()
            has_more = len(rows) > limit
            page = rows[:limit]
            return {
                "workspace_id": workspace,
                "total": total,
                "sessions": [self._workspace_session(row, now=now) for row in page],
                "next_before_session_id": (
                    str(page[-1][0]) if has_more and page else None
                ),
            }

    def revoke_workspace_session(
        self,
        *,
        workspace_id: str,
        session_id: str,
        actor_user_id: str,
    ) -> bool:
        workspace = _clean_id(workspace_id, field="workspace_id")
        session = _clean_id(session_id, field="session_id")
        actor = _clean_id(actor_user_id, field="user_id")
        now = self._now()
        with self._lock, self._transaction():
            actor_role = self._require_manager(workspace, actor)
            row = self._conn.execute(
                "SELECT s.user_id,m.role FROM auth_sessions s "
                "LEFT JOIN auth_memberships m ON m.workspace_id=s.active_workspace_id "
                "AND m.user_id=s.user_id WHERE s.session_id=? "
                "AND s.active_workspace_id=?",
                (session, workspace),
            ).fetchone()
            if row is None:
                raise AuthNotFoundError("workspace session not found")
            if actor_role is Role.ADMIN and row[1] == Role.OWNER.value:
                raise AuthAuthorizationError("admin cannot revoke an owner session")
            self._conn.execute(
                "UPDATE auth_sessions SET revoked_at=? WHERE session_id=? "
                "AND revoked_at IS NULL",
                (now, session),
            )
            return True

    def list_sessions(
        self,
        user_id: str,
        current_token: str | None = None,
        *,
        include_revoked: bool = False,
    ) -> list[dict[str, Any]]:
        user_id = _clean_id(user_id, field="user_id")
        current_digest = (
            _token_hash(current_token) if current_token is not None else None
        )
        now = self._now()
        with self._lock:
            self._ensure_open()
            sql = (
                "SELECT session_id,created_at,last_seen_at,expires_at,token_hash "
                "FROM auth_sessions WHERE user_id=?"
            )
            params: list[Any] = [user_id]
            if not include_revoked:
                sql += " AND revoked_at IS NULL AND expires_at>?"
                params.append(now)
            sql += " ORDER BY created_at DESC,session_id DESC"
            rows = self._conn.execute(sql, params).fetchall()
            return [
                self._session(
                    row[:4], current=hmac.compare_digest(row[4], current_digest)
                )
                if current_digest is not None
                else self._session(row[:4])
                for row in rows
            ]

    def logout(self, token: str) -> bool:
        digest, now = _token_hash(token), self._now()
        with self._lock, self._transaction():
            changed = self._conn.execute(
                "UPDATE auth_sessions SET revoked_at=? WHERE token_hash=? AND revoked_at IS NULL",
                (now, digest),
            ).rowcount
            return changed == 1

    def revoke_session(self, user_id: str, session_id: str) -> bool:
        user_id = _clean_id(user_id, field="user_id")
        session_id = _clean_id(session_id, field="session_id")
        now = self._now()
        with self._lock, self._transaction():
            changed = self._conn.execute(
                "UPDATE auth_sessions SET revoked_at=? WHERE user_id=? AND session_id=? "
                "AND revoked_at IS NULL",
                (now, user_id, session_id),
            ).rowcount
            if changed == 0:
                exists = self._conn.execute(
                    "SELECT 1 FROM auth_sessions WHERE user_id=? AND session_id=?",
                    (user_id, session_id),
                ).fetchone()
                if exists is None:
                    raise AuthNotFoundError("session not found")
            return changed == 1

    delete_session = revoke_session

    def logout_all(self, user_id: str, except_token: str | None = None) -> int:
        user_id = _clean_id(user_id, field="user_id")
        except_digest = _token_hash(except_token) if except_token is not None else None
        now = self._now()
        with self._lock, self._transaction():
            if except_digest is None:
                cursor = self._conn.execute(
                    "UPDATE auth_sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL",
                    (now, user_id),
                )
            else:
                cursor = self._conn.execute(
                    "UPDATE auth_sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL "
                    "AND token_hash<>?",
                    (now, user_id, except_digest),
                )
            return cursor.rowcount

    revoke_all_sessions = logout_all

    def change_password(
        self,
        user_id: str,
        current_password: str,
        new_password: str,
        current_token: str | None = None,
    ) -> int:
        user_id = _clean_id(user_id, field="user_id")
        current_password = _clean_password(current_password, enforce_minimum=False)
        new_password = _clean_password(new_password)
        if hmac.compare_digest(
            current_password.encode("utf-8"), new_password.encode("utf-8")
        ):
            raise AuthValidationError("new password must differ from current password")
        new_hash = _hash_password(new_password, self.scrypt_params)
        except_digest = (
            _token_hash(current_token) if current_token is not None else None
        )
        now = self._now()
        with self._lock:
            self._ensure_open()
            row = self._conn.execute(
                "SELECT password_hash FROM auth_users WHERE user_id=?", (user_id,)
            ).fetchone()
            if row is None:
                _verify_password(current_password, self._dummy_password_hash)
                raise AuthAuthenticationError("current password is invalid")
            valid = _verify_password(current_password, row[0])
            if not valid:
                raise AuthAuthenticationError("current password is invalid")
            with self._transaction():
                changed = self._conn.execute(
                    "UPDATE auth_users SET password_hash=?,failed_login_count=0,locked_until=NULL,"
                    "updated_at=? WHERE user_id=? AND password_hash=?",
                    (new_hash, now, user_id, row[0]),
                ).rowcount
                if changed != 1:
                    raise AuthConflictError("password changed concurrently")
                if except_digest is None:
                    cursor = self._conn.execute(
                        "UPDATE auth_sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL",
                        (now, user_id),
                    )
                else:
                    current = self._conn.execute(
                        "SELECT 1 FROM auth_sessions WHERE user_id=? AND token_hash=? "
                        "AND revoked_at IS NULL AND expires_at>?",
                        (user_id, except_digest, now),
                    ).fetchone()
                    if current is None:
                        raise AuthAuthenticationError("current session is invalid")
                    cursor = self._conn.execute(
                        "UPDATE auth_sessions SET revoked_at=? WHERE user_id=? "
                        "AND revoked_at IS NULL AND token_hash<>?",
                        (now, user_id, except_digest),
                    )
                return cursor.rowcount

    def check(self) -> bool:
        """Run a cheap, fail-closed SQLite readiness probe.

        Referencing every identity table makes a partially initialized or
        damaged schema unavailable without scanning user data.  The schema
        marker additionally prevents a process with unsupported migrations
        from advertising readiness.
        """

        with self._lock:
            self._ensure_open()
            try:
                row = self._conn.execute(
                    "SELECT value FROM auth_schema_meta WHERE key='schema_version'"
                ).fetchone()
                if row is None or row[0] != AUTH_SCHEMA_VERSION:
                    raise AuthStoreError("authentication schema version is unavailable")
                for table in (
                    "auth_users",
                    "auth_workspaces",
                    "auth_memberships",
                    "auth_sessions",
                    "auth_invites",
                    "auth_password_capabilities",
                    "auth_oidc_identities",
                    "auth_workspace_oidc_policies",
                    "auth_oidc_managed_memberships",
                    "auth_scim_users",
                    "auth_scim_groups",
                    "auth_scim_group_members",
                    "auth_service_accounts",
                    "auth_service_tokens",
                    "auth_service_account_policies",
                    "auth_workspace_session_policies",
                ):
                    self._conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
            except sqlite3.Error as exc:
                raise AuthStoreError(
                    "authentication store readiness check failed"
                ) from exc
            return True

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._conn.close()

    def __enter__(self) -> "AuthStore":
        self._ensure_open()
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


__all__ = [
    "AUTH_SCHEMA_VERSION",
    "AuthAuthenticationError",
    "AuthAuthorizationError",
    "AuthConflictError",
    "AuthContext",
    "AuthInviteError",
    "AuthLockedError",
    "AuthNotFoundError",
    "AuthStore",
    "AuthStoreError",
    "AuthValidationError",
    "MAX_PASSWORD_LENGTH",
    "MIN_PASSWORD_LENGTH",
    "ScryptParams",
    "ServiceAccountAuthContext",
    "normalize_email",
]
