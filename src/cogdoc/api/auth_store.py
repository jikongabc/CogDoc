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
import math
import secrets
import sqlite3
from threading import RLock
import time
import unicodedata
from typing import Any, Callable, Iterator, Mapping

from cogdoc.api.tenancy import Principal, Role


AUTH_SCHEMA_VERSION = "1"
MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 256
SESSION_TOUCH_INTERVAL_SECONDS = 60.0
_ROLES = frozenset(role.value for role in Role)
_ASSIGNABLE_ROLES = _ROLES - {Role.OWNER.value}


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
            """
        )
        row = self._conn.execute(
            "SELECT value FROM auth_schema_meta WHERE key='schema_version'"
        ).fetchone()
        if row is None:
            self._conn.execute(
                "INSERT INTO auth_schema_meta(key, value) VALUES('schema_version', ?)",
                (AUTH_SCHEMA_VERSION,),
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

    def _issue_session_locked(
        self, user_id: str, workspace_id: str, now: float
    ) -> tuple[dict[str, Any], str]:
        if self._membership_row(workspace_id, user_id) is None:
            raise AuthAuthorizationError("user is not a workspace member")
        session_id = _new_id("ses")
        token = _new_token("cgs")
        expires_at = now + self.session_ttl_seconds
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
                "SELECT user_id,password_hash,personal_workspace_id,failed_login_count,"
                "locked_until FROM auth_users WHERE email=?",
                (clean_email,),
            ).fetchone()
            encoded = row[1] if row is not None else self._dummy_password_hash
            valid = _verify_password(candidate_password, encoded)
            if row is None:
                raise AuthAuthenticationError("invalid email or password")
            user_id, _, personal_workspace_id, failed_count, locked_until = row
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
            target = requested or active_workspace_id or row[7]
            membership = self._membership_row(target, user_id)
            if membership is None:
                # A deleted/removed active workspace may fall back only to the
                # user's personal workspace, never to an arbitrary membership.
                target = row[7]
                membership = self._membership_row(target, user_id)
            if membership is None or (requested is not None and target != requested):
                raise AuthAuthorizationError("user is not a workspace member")
            # Session authentication is a read-heavy path. Persist activity at a
            # bounded cadence (or immediately on workspace switch) so concurrent
            # RAG reads do not serialize on a SQLite write for every request.
            if (
                target != active_workspace_id
                or now - float(row[4]) >= SESSION_TOUCH_INTERVAL_SECONDS
            ):
                with self._transaction():
                    changed = self._conn.execute(
                        "UPDATE auth_sessions SET active_workspace_id=?,last_seen_at=? "
                        "WHERE session_id=? AND revoked_at IS NULL AND expires_at>?",
                        (target, now, session_id, now),
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
                "expires_at": _iso(row[5]),
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
    "normalize_email",
]
