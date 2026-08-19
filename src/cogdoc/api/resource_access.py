from __future__ import annotations

import builtins
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from threading import RLock
from typing import Any, Iterator, Mapping, Sequence

from cogdoc.api.tenancy import Permission, Principal, ROLE_PERMISSIONS, Role


class ResourceAccessError(RuntimeError):
    """Base class for resource ACL persistence errors."""


class ResourceAccessNotFoundError(ResourceAccessError, KeyError):
    """A parent resource required by an ACL mutation does not exist."""


class ResourceAccessConflictError(ResourceAccessError):
    """An ACL mutation conflicts with an existing resource identity."""


class AccessPolicy(str, Enum):
    """Visibility attached to a knowledge base or document.

    Knowledge bases accept ``workspace`` and ``private``. Documents additionally
    accept ``inherit`` so their effective visibility can follow their parent KB.
    """

    WORKSPACE = "workspace"
    PRIVATE = "private"
    INHERIT = "inherit"


class AccessMode(str, Enum):
    """Unambiguous query boundary returned by :class:`ResourceAccessStore`."""

    ALL = "all"
    SUBSET = "subset"
    DENY = "deny"


# Descriptive aliases keep callers from growing another vocabulary for the same
# persisted contract.
ResourcePolicy = AccessPolicy
QueryAccessMode = AccessMode


@dataclass(frozen=True, slots=True)
class QueryAuthorization:
    """One immutable authorization snapshot for a knowledge-base query.

    ``ALL`` and ``DENY`` deliberately carry no allowlist. Only ``SUBSET`` has an
    exact non-empty list. Consumers must branch on ``mode``; an empty tuple is
    therefore never overloaded to mean the whole knowledge base.
    """

    tenant_id: str
    kb_id: str
    permission: Permission
    mode: AccessMode
    acl_epoch: int
    allowed_document_ids: tuple[str, ...] = ()
    allowed_sources: tuple[str, ...] = ()
    reason: str = ""

    def __post_init__(self) -> None:
        if self.acl_epoch < 0:
            raise ValueError("acl_epoch must be non-negative")
        if self.mode is AccessMode.SUBSET:
            if not self.allowed_document_ids or not self.allowed_sources:
                raise ValueError("SUBSET authorization requires a non-empty allowlist")
            if len(self.allowed_document_ids) != len(self.allowed_sources):
                raise ValueError("document/source allowlists must have equal length")
        elif self.allowed_document_ids or self.allowed_sources:
            raise ValueError("only SUBSET authorization may carry an allowlist")

    @property
    def is_allowed(self) -> bool:
        return self.mode is not AccessMode.DENY

    @property
    def requires_filter(self) -> bool:
        return self.mode is AccessMode.SUBSET

    def allows_document_id(self, document_id: object) -> bool:
        if self.mode is AccessMode.ALL:
            return True
        if self.mode is AccessMode.DENY:
            return False
        return str(document_id or "") in self.allowed_document_ids

    def allows_source(self, source: object) -> bool:
        if self.mode is AccessMode.ALL:
            return True
        if self.mode is AccessMode.DENY:
            return False
        return str(source or "") in self.allowed_sources


# Alias used by authorization-oriented callers.
ResourceAccessDecision = QueryAuthorization


_IDENTITY_MAX_LENGTH = 160
_SOURCE_MAX_LENGTH = 1024
_KB_GRANT_DOCUMENT_KEY = ""
_UNSET = object()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_text(value: object, *, field: str, max_length: int) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string")
    if not value or value != value.strip():
        raise ValueError(f"{field} must be a non-empty canonical string")
    if len(value) > max_length:
        raise ValueError(f"{field} is too long")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"{field} must not contain control characters")
    return value


def _identity(value: object, *, field: str) -> str:
    return _canonical_text(value, field=field, max_length=_IDENTITY_MAX_LENGTH)


def _source(value: object) -> str:
    return _canonical_text(value, field="source", max_length=_SOURCE_MAX_LENGTH)


def _policy(value: AccessPolicy | str, *, document: bool) -> AccessPolicy:
    try:
        policy = value if isinstance(value, AccessPolicy) else AccessPolicy(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"unsupported access policy: {value!r}") from exc
    if not document and policy is AccessPolicy.INHERIT:
        raise ValueError("knowledge-base policy cannot inherit")
    return policy


def _role(value: Role | str) -> Role:
    try:
        return value if isinstance(value, Role) else Role(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"unsupported grant role: {value!r}") from exc


def _permission(value: Permission | str) -> Permission:
    try:
        return value if isinstance(value, Permission) else Permission(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"unsupported permission: {value!r}") from exc


class ResourceAccessStore:
    """Thread-safe, tenant-scoped SQLite ACL store.

    Raw ``kb_id`` values are opaque to this layer. HTTP callers should pass the
    already-resolved physical storage ID so ACL state follows the actual index
    namespace and cannot alias another tenant's logical slug.
    """

    def __init__(
        self,
        db_path: str | os.PathLike[str],
        *,
        legacy_workspace_default: bool = False,
        busy_timeout_ms: int = 5000,
    ) -> None:
        try:
            normalized_path = os.fspath(db_path)
        except TypeError as exc:
            raise TypeError("db_path must be path-like") from exc
        if not isinstance(normalized_path, str) or not normalized_path:
            raise ValueError("db_path must not be empty")
        if type(legacy_workspace_default) is not bool:
            raise TypeError("legacy_workspace_default must be a boolean")
        if type(busy_timeout_ms) is not int or busy_timeout_ms < 0:
            raise ValueError("busy_timeout_ms must be a non-negative integer")

        directory = os.path.dirname(normalized_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._db_path = normalized_path
        self._legacy_workspace_default = legacy_workspace_default
        self._lock = RLock()
        self._conn = sqlite3.connect(
            normalized_path,
            check_same_thread=False,
            isolation_level=None,
            timeout=busy_timeout_ms / 1000.0,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._initialize_schema()

    @property
    def legacy_workspace_default(self) -> bool:
        return self._legacy_workspace_default

    def _initialize_schema(self) -> None:
        statements = (
            """
            CREATE TABLE IF NOT EXISTS resource_access_kb_policies (
                tenant_id TEXT NOT NULL,
                kb_id TEXT NOT NULL,
                owner_id TEXT NOT NULL,
                owner_membership_id TEXT,
                policy TEXT NOT NULL CHECK (policy IN ('workspace', 'private')),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (tenant_id, kb_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS resource_access_document_policies (
                tenant_id TEXT NOT NULL,
                kb_id TEXT NOT NULL,
                document_id TEXT NOT NULL,
                source TEXT NOT NULL,
                owner_id TEXT NOT NULL,
                owner_membership_id TEXT,
                policy TEXT NOT NULL
                    CHECK (policy IN ('workspace', 'private', 'inherit')),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (tenant_id, kb_id, document_id),
                UNIQUE (tenant_id, kb_id, source)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS resource_access_subject_grants (
                tenant_id TEXT NOT NULL,
                kb_id TEXT NOT NULL,
                document_key TEXT NOT NULL,
                subject_id TEXT NOT NULL,
                role TEXT NOT NULL
                    CHECK (role IN ('owner', 'admin', 'editor', 'reviewer', 'viewer')),
                managed_by TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (tenant_id, kb_id, document_key, subject_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS resource_access_acl_epochs (
                tenant_id TEXT NOT NULL,
                kb_id TEXT NOT NULL,
                epoch INTEGER NOT NULL CHECK (epoch >= 0),
                updated_at TEXT NOT NULL,
                PRIMARY KEY (tenant_id, kb_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS resource_access_membership_tombstones (
                tenant_id TEXT NOT NULL,
                subject_id TEXT NOT NULL,
                membership_id TEXT NOT NULL,
                revoked_at TEXT NOT NULL,
                PRIMARY KEY (tenant_id, subject_id, membership_id)
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_resource_access_documents_tenant_kb
            ON resource_access_document_policies (tenant_id, kb_id, document_id)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_resource_access_grants_subject
            ON resource_access_subject_grants
                (tenant_id, kb_id, subject_id, document_key)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_resource_access_grants_tenant_subject
            ON resource_access_subject_grants (tenant_id, subject_id, kb_id)
            """,
        )
        with self._lock:
            for statement in statements:
                self._conn.execute(statement)
            # v1 databases predate membership-incarnation-bound creator access.
            # NULL is intentionally fail-closed for human sessions while keeping
            # local/static-key deployments source compatible.
            for table in (
                "resource_access_kb_policies",
                "resource_access_document_policies",
            ):
                columns = {
                    str(row[1])
                    for row in self._conn.execute(f"PRAGMA table_info({table})")
                }
                if "owner_membership_id" not in columns:
                    try:
                        self._conn.execute(
                            f"ALTER TABLE {table} ADD COLUMN owner_membership_id TEXT"
                        )
                    except sqlite3.OperationalError:
                        # Another process may have completed the same additive
                        # migration while this connection waited on SQLite's
                        # schema lock. Suppress only that proven-safe race.
                        refreshed = {
                            str(row[1])
                            for row in self._conn.execute(f"PRAGMA table_info({table})")
                        }
                        if "owner_membership_id" not in refreshed:
                            raise
            grant_columns = {
                str(row[1])
                for row in self._conn.execute(
                    "PRAGMA table_info(resource_access_subject_grants)"
                )
            }
            if "managed_by" not in grant_columns:
                self._conn.execute(
                    "ALTER TABLE resource_access_subject_grants "
                    "ADD COLUMN managed_by TEXT NOT NULL DEFAULT ''"
                )

    @contextmanager
    def _write_transaction(self) -> Iterator[None]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def _bump_epoch_locked(self, tenant_id: str, kb_id: str) -> int:
        now = _now_iso()
        self._conn.execute(
            "INSERT INTO resource_access_acl_epochs "
            "(tenant_id, kb_id, epoch, updated_at) VALUES (?, ?, 1, ?) "
            "ON CONFLICT(tenant_id, kb_id) DO UPDATE SET "
            "epoch=resource_access_acl_epochs.epoch + 1, updated_at=excluded.updated_at",
            (tenant_id, kb_id, now),
        )
        row = self._conn.execute(
            "SELECT epoch FROM resource_access_acl_epochs "
            "WHERE tenant_id=? AND kb_id=?",
            (tenant_id, kb_id),
        ).fetchone()
        if row is None:
            raise ResourceAccessError("ACL epoch update was not persisted")
        return int(row["epoch"])

    def _reject_revoked_membership_locked(
        self,
        tenant_id: str,
        subject_id: str,
        membership_id: object,
    ) -> None:
        if membership_id is None:
            return
        revoked = self._conn.execute(
            "SELECT 1 FROM resource_access_membership_tombstones "
            "WHERE tenant_id=? AND subject_id=? AND membership_id=?",
            (tenant_id, subject_id, str(membership_id)),
        ).fetchone()
        if revoked is not None:
            raise ResourceAccessConflictError("membership incarnation was revoked")

    def acl_epoch(self, tenant_id: str, kb_id: str) -> int:
        tenant_id = _identity(tenant_id, field="tenant_id")
        kb_id = _identity(kb_id, field="kb_id")
        with self._lock:
            row = self._conn.execute(
                "SELECT epoch FROM resource_access_acl_epochs "
                "WHERE tenant_id=? AND kb_id=?",
                (tenant_id, kb_id),
            ).fetchone()
        return int(row["epoch"]) if row is not None else 0

    def is_epoch_current(self, tenant_id: str, kb_id: str, epoch: int) -> bool:
        if type(epoch) is not int or epoch < 0:
            raise ValueError("epoch must be a non-negative integer")
        try:
            return self.acl_epoch(tenant_id, kb_id) == epoch
        except sqlite3.Error:
            return False

    # -- Knowledge-base policy CRUD -----------------------------------------

    def set_kb_policy(
        self,
        tenant_id: str,
        kb_id: str,
        owner_id: str,
        policy: AccessPolicy | str,
        *,
        owner_membership_id: str | None | object = _UNSET,
    ) -> dict[str, Any]:
        tenant_id = _identity(tenant_id, field="tenant_id")
        kb_id = _identity(kb_id, field="kb_id")
        owner_id = _identity(owner_id, field="owner_id")
        if owner_membership_id is not _UNSET and owner_membership_id is not None:
            owner_membership_id = _identity(
                owner_membership_id, field="owner_membership_id"
            )
        normalized_policy = _policy(policy, document=False)
        now = _now_iso()
        with self._write_transaction():
            existing = self._conn.execute(
                "SELECT owner_id, owner_membership_id, policy, created_at "
                "FROM resource_access_kb_policies WHERE tenant_id=? AND kb_id=?",
                (tenant_id, kb_id),
            ).fetchone()
            effective_owner_membership_id = (
                existing["owner_membership_id"]
                if owner_membership_id is _UNSET and existing is not None
                else None
                if owner_membership_id is _UNSET
                else owner_membership_id
            )
            self._reject_revoked_membership_locked(
                tenant_id, owner_id, effective_owner_membership_id
            )
            changed = (
                existing is None
                or existing["owner_id"] != owner_id
                or existing["owner_membership_id"] != effective_owner_membership_id
                or existing["policy"] != normalized_policy.value
            )
            if changed:
                created_at = (
                    str(existing["created_at"]) if existing is not None else now
                )
                self._conn.execute(
                    "INSERT INTO resource_access_kb_policies "
                    "(tenant_id, kb_id, owner_id, owner_membership_id, policy, "
                    "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(tenant_id, kb_id) DO UPDATE SET "
                    "owner_id=excluded.owner_id, "
                    "owner_membership_id=excluded.owner_membership_id, "
                    "policy=excluded.policy, "
                    "updated_at=excluded.updated_at",
                    (
                        tenant_id,
                        kb_id,
                        owner_id,
                        effective_owner_membership_id,
                        normalized_policy.value,
                        created_at,
                        now,
                    ),
                )
                epoch = self._bump_epoch_locked(tenant_id, kb_id)
            else:
                epoch = self._epoch_locked(tenant_id, kb_id)
        record = self.get_kb_policy(tenant_id, kb_id)
        if record is None:
            raise ResourceAccessError("knowledge-base policy write was not persisted")
        record["acl_epoch"] = epoch
        return record

    # "put" is an explicit upsert alias useful to persistence-oriented callers.
    put_kb_policy = set_kb_policy

    def get_kb_policy(self, tenant_id: str, kb_id: str) -> dict[str, Any] | None:
        tenant_id = _identity(tenant_id, field="tenant_id")
        kb_id = _identity(kb_id, field="kb_id")
        with self._lock:
            row = self._conn.execute(
                "SELECT tenant_id, kb_id, owner_id, owner_membership_id, policy, "
                "created_at, updated_at "
                "FROM resource_access_kb_policies WHERE tenant_id=? AND kb_id=?",
                (tenant_id, kb_id),
            ).fetchone()
            epoch = self._epoch_locked(tenant_id, kb_id)
        return self._kb_record(row, epoch) if row is not None else None

    def list_kb_policies(self, tenant_id: str) -> list[dict[str, Any]]:
        tenant_id = _identity(tenant_id, field="tenant_id")
        with self._lock:
            rows = self._conn.execute(
                "SELECT p.tenant_id, p.kb_id, p.owner_id, p.owner_membership_id, "
                "p.policy, "
                "p.created_at, p.updated_at, COALESCE(e.epoch, 0) AS acl_epoch "
                "FROM resource_access_kb_policies AS p "
                "LEFT JOIN resource_access_acl_epochs AS e "
                "ON e.tenant_id=p.tenant_id AND e.kb_id=p.kb_id "
                "WHERE p.tenant_id=? ORDER BY p.kb_id",
                (tenant_id,),
            ).fetchall()
        return [self._kb_record(row, int(row["acl_epoch"])) for row in rows]

    # Short list alias mirrors the store's root resource.
    list = list_kb_policies

    def delete_kb_policy(self, tenant_id: str, kb_id: str) -> bool:
        return self.clear_kb(tenant_id, kb_id)

    # -- Document policy CRUD -----------------------------------------------

    def set_document_policy(
        self,
        tenant_id: str,
        kb_id: str,
        document_id: str,
        source: str,
        owner_id: str | None = None,
        policy: AccessPolicy | str = AccessPolicy.INHERIT,
        *,
        owner_membership_id: str | None | object = _UNSET,
    ) -> dict[str, Any]:
        tenant_id = _identity(tenant_id, field="tenant_id")
        kb_id = _identity(kb_id, field="kb_id")
        document_id = _identity(document_id, field="document_id")
        source = _source(source)
        normalized_policy = _policy(policy, document=True)
        if owner_id is not None:
            owner_id = _identity(owner_id, field="owner_id")
        if owner_membership_id is not _UNSET and owner_membership_id is not None:
            owner_membership_id = _identity(
                owner_membership_id, field="owner_membership_id"
            )
        now = _now_iso()
        try:
            with self._write_transaction():
                kb_row = self._conn.execute(
                    "SELECT owner_id, owner_membership_id "
                    "FROM resource_access_kb_policies "
                    "WHERE tenant_id=? AND kb_id=?",
                    (tenant_id, kb_id),
                ).fetchone()
                if kb_row is None and not self._legacy_workspace_default:
                    raise ResourceAccessNotFoundError(
                        f"knowledge-base policy does not exist: {tenant_id}/{kb_id}"
                    )
                effective_owner = owner_id or (
                    str(kb_row["owner_id"]) if kb_row is not None else ""
                )
                if not effective_owner:
                    raise ValueError(
                        "owner_id is required for a legacy document without a KB policy"
                    )
                existing = self._conn.execute(
                    "SELECT source, owner_id, owner_membership_id, policy, created_at "
                    "FROM resource_access_document_policies "
                    "WHERE tenant_id=? AND kb_id=? AND document_id=?",
                    (tenant_id, kb_id, document_id),
                ).fetchone()
                effective_owner_membership_id = (
                    existing["owner_membership_id"]
                    if owner_membership_id is _UNSET and existing is not None
                    else (
                        kb_row["owner_membership_id"]
                        if owner_membership_id is _UNSET
                        and kb_row is not None
                        and effective_owner == str(kb_row["owner_id"])
                        else None
                    )
                    if owner_membership_id is _UNSET
                    else owner_membership_id
                )
                self._reject_revoked_membership_locked(
                    tenant_id, effective_owner, effective_owner_membership_id
                )
                changed = (
                    existing is None
                    or existing["source"] != source
                    or existing["owner_id"] != effective_owner
                    or existing["owner_membership_id"] != effective_owner_membership_id
                    or existing["policy"] != normalized_policy.value
                )
                if changed:
                    created_at = (
                        str(existing["created_at"]) if existing is not None else now
                    )
                    self._conn.execute(
                        "INSERT INTO resource_access_document_policies "
                        "(tenant_id, kb_id, document_id, source, owner_id, "
                        "owner_membership_id, policy, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT(tenant_id, kb_id, document_id) DO UPDATE SET "
                        "source=excluded.source, owner_id=excluded.owner_id, "
                        "owner_membership_id=excluded.owner_membership_id, "
                        "policy=excluded.policy, updated_at=excluded.updated_at",
                        (
                            tenant_id,
                            kb_id,
                            document_id,
                            source,
                            effective_owner,
                            effective_owner_membership_id,
                            normalized_policy.value,
                            created_at,
                            now,
                        ),
                    )
                    epoch = self._bump_epoch_locked(tenant_id, kb_id)
                else:
                    epoch = self._epoch_locked(tenant_id, kb_id)
        except sqlite3.IntegrityError as exc:
            if "UNIQUE constraint failed" in str(exc):
                raise ResourceAccessConflictError(
                    f"source is already bound to another document: {source}"
                ) from exc
            raise
        record = self.get_document_policy(tenant_id, kb_id, document_id)
        if record is None:
            raise ResourceAccessError("document policy write was not persisted")
        record["acl_epoch"] = epoch
        return record

    put_document_policy = set_document_policy
    register_document = set_document_policy

    def get_document_policy(
        self, tenant_id: str, kb_id: str, document_id: str
    ) -> dict[str, Any] | None:
        tenant_id = _identity(tenant_id, field="tenant_id")
        kb_id = _identity(kb_id, field="kb_id")
        document_id = _identity(document_id, field="document_id")
        with self._lock:
            row = self._conn.execute(
                "SELECT tenant_id, kb_id, document_id, source, owner_id, "
                "owner_membership_id, policy, created_at, updated_at "
                "FROM resource_access_document_policies "
                "WHERE tenant_id=? AND kb_id=? AND document_id=?",
                (tenant_id, kb_id, document_id),
            ).fetchone()
            epoch = self._epoch_locked(tenant_id, kb_id)
        return self._document_record(row, epoch) if row is not None else None

    def get_document_by_source(
        self, tenant_id: str, kb_id: str, source: str
    ) -> dict[str, Any] | None:
        tenant_id = _identity(tenant_id, field="tenant_id")
        kb_id = _identity(kb_id, field="kb_id")
        source = _source(source)
        with self._lock:
            row = self._conn.execute(
                "SELECT tenant_id, kb_id, document_id, source, owner_id, "
                "owner_membership_id, policy, created_at, updated_at "
                "FROM resource_access_document_policies "
                "WHERE tenant_id=? AND kb_id=? AND source=?",
                (tenant_id, kb_id, source),
            ).fetchone()
            epoch = self._epoch_locked(tenant_id, kb_id)
        return self._document_record(row, epoch) if row is not None else None

    def list_document_policies(
        self, tenant_id: str, kb_id: str
    ) -> builtins.list[dict[str, Any]]:
        tenant_id = _identity(tenant_id, field="tenant_id")
        kb_id = _identity(kb_id, field="kb_id")
        with self._lock:
            rows = self._conn.execute(
                "SELECT tenant_id, kb_id, document_id, source, owner_id, "
                "owner_membership_id, policy, created_at, updated_at "
                "FROM resource_access_document_policies "
                "WHERE tenant_id=? AND kb_id=? ORDER BY document_id",
                (tenant_id, kb_id),
            ).fetchall()
            epoch = self._epoch_locked(tenant_id, kb_id)
        return [self._document_record(row, epoch) for row in rows]

    list_documents = list_document_policies

    def delete_document_policy(
        self, tenant_id: str, kb_id: str, document_id: str
    ) -> bool:
        tenant_id = _identity(tenant_id, field="tenant_id")
        kb_id = _identity(kb_id, field="kb_id")
        document_id = _identity(document_id, field="document_id")
        with self._write_transaction():
            deleted = self._conn.execute(
                "DELETE FROM resource_access_document_policies "
                "WHERE tenant_id=? AND kb_id=? AND document_id=?",
                (tenant_id, kb_id, document_id),
            ).rowcount
            if not deleted:
                return False
            self._conn.execute(
                "DELETE FROM resource_access_subject_grants "
                "WHERE tenant_id=? AND kb_id=? AND document_key=?",
                (tenant_id, kb_id, document_id),
            )
            self._bump_epoch_locked(tenant_id, kb_id)
            return True

    # -- Subject grant CRUD -------------------------------------------------

    def grant_subject(
        self,
        tenant_id: str,
        kb_id: str,
        subject_id: str,
        role: Role | str,
        document_id: str | None = None,
        *,
        membership_id: str | None = None,
    ) -> dict[str, Any]:
        tenant_id = _identity(tenant_id, field="tenant_id")
        kb_id = _identity(kb_id, field="kb_id")
        subject_id = _identity(subject_id, field="subject_id")
        if membership_id is not None:
            membership_id = _identity(membership_id, field="membership_id")
        normalized_role = _role(role)
        document_key = (
            _KB_GRANT_DOCUMENT_KEY
            if document_id is None
            else _identity(document_id, field="document_id")
        )
        now = _now_iso()
        with self._write_transaction():
            if membership_id is None:
                revoked = self._conn.execute(
                    "SELECT 1 FROM resource_access_membership_tombstones "
                    "WHERE tenant_id=? AND subject_id=? LIMIT 1",
                    (tenant_id, subject_id),
                ).fetchone()
                if revoked is not None:
                    raise ResourceAccessConflictError(
                        "membership incarnation is required for a revoked subject"
                    )
            else:
                revoked = self._conn.execute(
                    "SELECT 1 FROM resource_access_membership_tombstones "
                    "WHERE tenant_id=? AND subject_id=? AND membership_id=?",
                    (tenant_id, subject_id, membership_id),
                ).fetchone()
                if revoked is not None:
                    raise ResourceAccessConflictError(
                        "membership incarnation was revoked"
                    )
            kb_exists = self._conn.execute(
                "SELECT 1 FROM resource_access_kb_policies "
                "WHERE tenant_id=? AND kb_id=?",
                (tenant_id, kb_id),
            ).fetchone()
            if kb_exists is None and not self._legacy_workspace_default:
                raise ResourceAccessNotFoundError(
                    f"knowledge-base policy does not exist: {tenant_id}/{kb_id}"
                )
            if document_key:
                document_exists = self._conn.execute(
                    "SELECT 1 FROM resource_access_document_policies "
                    "WHERE tenant_id=? AND kb_id=? AND document_id=?",
                    (tenant_id, kb_id, document_key),
                ).fetchone()
                if document_exists is None:
                    raise ResourceAccessNotFoundError(
                        f"document policy does not exist: {document_key}"
                    )
            existing = self._conn.execute(
                "SELECT role, managed_by, created_at, updated_at "
                "FROM resource_access_subject_grants "
                "WHERE tenant_id=? AND kb_id=? AND document_key=? AND subject_id=?",
                (tenant_id, kb_id, document_key, subject_id),
            ).fetchone()
            changed = (
                existing is None
                or existing["role"] != normalized_role.value
                or str(existing["managed_by"] or "") != ""
            )
            if changed:
                created_at = (
                    str(existing["created_at"]) if existing is not None else now
                )
                updated_at = now
                self._conn.execute(
                    "INSERT INTO resource_access_subject_grants "
                    "(tenant_id, kb_id, document_key, subject_id, role, managed_by, "
                    "created_at, updated_at) VALUES (?, ?, ?, ?, ?, '', ?, ?) "
                    "ON CONFLICT(tenant_id, kb_id, document_key, subject_id) "
                    "DO UPDATE SET role=excluded.role, managed_by='', "
                    "updated_at=excluded.updated_at",
                    (
                        tenant_id,
                        kb_id,
                        document_key,
                        subject_id,
                        normalized_role.value,
                        created_at,
                        now,
                    ),
                )
                epoch = self._bump_epoch_locked(tenant_id, kb_id)
            else:
                created_at = str(existing["created_at"])
                updated_at = str(existing["updated_at"])
                epoch = self._epoch_locked(tenant_id, kb_id)
        return {
            "tenant_id": tenant_id,
            "kb_id": kb_id,
            "document_id": document_key or None,
            "subject_id": subject_id,
            "role": normalized_role.value,
            "created_at": created_at,
            "updated_at": updated_at,
            "acl_epoch": epoch,
        }

    set_subject_grant = grant_subject

    def replace_managed_document_grants(
        self,
        tenant_id: str,
        kb_id: str,
        document_id: str,
        managed_by: str,
        grants: Sequence[tuple[str, Role | str, str | None]],
    ) -> dict[str, Any]:
        """Atomically replace one provider's grants without deleting manual grants."""

        tenant_id = _identity(tenant_id, field="tenant_id")
        kb_id = _identity(kb_id, field="kb_id")
        document_id = _identity(document_id, field="document_id")
        managed_by = _identity(managed_by, field="managed_by")
        desired: dict[str, tuple[Role, str | None]] = {}
        for subject_id, role, membership_id in grants:
            subject = _identity(subject_id, field="subject_id")
            normalized_role = _role(role)
            if normalized_role not in {Role.VIEWER, Role.REVIEWER, Role.EDITOR}:
                raise ValueError("externally managed grants are capped at editor")
            membership = (
                _identity(membership_id, field="membership_id")
                if membership_id is not None
                else None
            )
            previous = desired.get(subject)
            if previous is not None and previous != (normalized_role, membership):
                raise ValueError("duplicate managed subject has conflicting grants")
            desired[subject] = (normalized_role, membership)

        now = _now_iso()
        applied = 0
        removed = 0
        manual_preserved = 0
        changed = False
        with self._write_transaction():
            document = self._conn.execute(
                "SELECT 1 FROM resource_access_document_policies "
                "WHERE tenant_id=? AND kb_id=? AND document_id=?",
                (tenant_id, kb_id, document_id),
            ).fetchone()
            if document is None:
                raise ResourceAccessNotFoundError(
                    f"document policy does not exist: {document_id}"
                )
            existing_rows = self._conn.execute(
                "SELECT subject_id,role,managed_by,created_at FROM "
                "resource_access_subject_grants WHERE tenant_id=? AND kb_id=? "
                "AND document_key=?",
                (tenant_id, kb_id, document_id),
            ).fetchall()
            existing = {str(row["subject_id"]): row for row in existing_rows}
            for subject, row in existing.items():
                if (
                    str(row["managed_by"] or "") == managed_by
                    and subject not in desired
                ):
                    self._conn.execute(
                        "DELETE FROM resource_access_subject_grants WHERE tenant_id=? "
                        "AND kb_id=? AND document_key=? AND subject_id=? AND managed_by=?",
                        (tenant_id, kb_id, document_id, subject, managed_by),
                    )
                    removed += 1
                    changed = True
            for subject, (role, membership_id) in desired.items():
                self._reject_revoked_membership_locked(
                    tenant_id, subject, membership_id
                )
                row = existing.get(subject)
                if row is not None and str(row["managed_by"] or "") != managed_by:
                    manual_preserved += 1
                    continue
                if row is None:
                    self._conn.execute(
                        "INSERT INTO resource_access_subject_grants "
                        "(tenant_id,kb_id,document_key,subject_id,role,managed_by,created_at,updated_at) "
                        "VALUES (?,?,?,?,?,?,?,?)",
                        (
                            tenant_id,
                            kb_id,
                            document_id,
                            subject,
                            role.value,
                            managed_by,
                            now,
                            now,
                        ),
                    )
                    changed = True
                elif str(row["role"]) != role.value:
                    self._conn.execute(
                        "UPDATE resource_access_subject_grants SET role=?,updated_at=? "
                        "WHERE tenant_id=? AND kb_id=? AND document_key=? "
                        "AND subject_id=? AND managed_by=?",
                        (
                            role.value,
                            now,
                            tenant_id,
                            kb_id,
                            document_id,
                            subject,
                            managed_by,
                        ),
                    )
                    changed = True
                applied += 1
            epoch = (
                self._bump_epoch_locked(tenant_id, kb_id)
                if changed
                else self._epoch_locked(tenant_id, kb_id)
            )
        return {
            "managed_by": managed_by,
            "applied": applied,
            "removed": removed,
            "manual_preserved": manual_preserved,
            "acl_epoch": epoch,
        }

    def revoke_subject(
        self,
        tenant_id: str,
        kb_id: str,
        subject_id: str,
        document_id: str | None = None,
    ) -> bool:
        tenant_id = _identity(tenant_id, field="tenant_id")
        kb_id = _identity(kb_id, field="kb_id")
        subject_id = _identity(subject_id, field="subject_id")
        document_key = (
            _KB_GRANT_DOCUMENT_KEY
            if document_id is None
            else _identity(document_id, field="document_id")
        )
        with self._write_transaction():
            deleted = self._conn.execute(
                "DELETE FROM resource_access_subject_grants "
                "WHERE tenant_id=? AND kb_id=? AND document_key=? AND subject_id=?",
                (tenant_id, kb_id, document_key, subject_id),
            ).rowcount
            if deleted:
                self._bump_epoch_locked(tenant_id, kb_id)
            return bool(deleted)

    delete_subject_grant = revoke_subject

    def revoke_all_subject_grants(
        self,
        tenant_id: str,
        subject_id: str,
        *,
        membership_id: str | None = None,
    ) -> dict[str, int]:
        """Revoke every grant for one tenant subject in one transaction.

        The returned mapping contains the new epoch for each affected knowledge
        base.  A knowledge base is bumped exactly once even when the subject had
        both a KB grant and several document grants.  Repeating the operation is
        therefore idempotent and does not invalidate caches again.

        This operation deliberately spans the whole tenant.  When a durable
        ``membership_id`` is supplied, its revocation tombstone is committed in
        the same transaction as the deletes.  A delayed grant carrying that old
        membership incarnation is then rejected even after the user is invited
        back with a new membership ID.
        """

        tenant_id = _identity(tenant_id, field="tenant_id")
        subject_id = _identity(subject_id, field="subject_id")
        if membership_id is not None:
            membership_id = _identity(membership_id, field="membership_id")
        with self._write_transaction():
            tombstone_created = False
            if membership_id is not None:
                tombstone_created = bool(
                    self._conn.execute(
                        "INSERT INTO resource_access_membership_tombstones "
                        "(tenant_id,subject_id,membership_id,revoked_at) "
                        "VALUES (?,?,?,?) ON CONFLICT DO NOTHING",
                        (tenant_id, subject_id, membership_id, _now_iso()),
                    ).rowcount
                )
            # Creator access is itself an authorization edge. Include owned KBs
            # and documents so revocation invalidates every affected snapshot,
            # even when the member never had an explicit grant.
            rows = self._conn.execute(
                "SELECT kb_id FROM resource_access_subject_grants "
                "WHERE tenant_id=? AND subject_id=? "
                "UNION SELECT kb_id FROM resource_access_kb_policies "
                "WHERE tenant_id=? AND owner_id=? AND owner_membership_id=? AND ? "
                "UNION SELECT kb_id FROM resource_access_document_policies "
                "WHERE tenant_id=? AND owner_id=? AND owner_membership_id=? AND ? "
                "ORDER BY kb_id",
                (
                    tenant_id,
                    subject_id,
                    tenant_id,
                    subject_id,
                    membership_id,
                    tombstone_created,
                    tenant_id,
                    subject_id,
                    membership_id,
                    tombstone_created,
                ),
            ).fetchall()
            affected_kb_ids = [str(row["kb_id"]) for row in rows]
            if not affected_kb_ids:
                return {}
            self._conn.execute(
                "DELETE FROM resource_access_subject_grants "
                "WHERE tenant_id=? AND subject_id=?",
                (tenant_id, subject_id),
            )
            return {
                kb_id: self._bump_epoch_locked(tenant_id, kb_id)
                for kb_id in affected_kb_ids
            }

    # Short alias for callers that organize operations around the subject.
    revoke_subject_all = revoke_all_subject_grants

    def is_membership_revoked(
        self,
        tenant_id: str,
        subject_id: str,
        membership_id: str,
    ) -> bool:
        tenant_id = _identity(tenant_id, field="tenant_id")
        subject_id = _identity(subject_id, field="subject_id")
        membership_id = _identity(membership_id, field="membership_id")
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM resource_access_membership_tombstones "
                "WHERE tenant_id=? AND subject_id=? AND membership_id=?",
                (tenant_id, subject_id, membership_id),
            ).fetchone()
        return row is not None

    def list_grants(
        self,
        tenant_id: str,
        kb_id: str,
        *,
        document_id: str | None = None,
        subject_id: str | None = None,
    ) -> builtins.list[dict[str, Any]]:
        tenant_id = _identity(tenant_id, field="tenant_id")
        kb_id = _identity(kb_id, field="kb_id")
        clauses = ["tenant_id=?", "kb_id=?"]
        params: list[Any] = [tenant_id, kb_id]
        if document_id is not None:
            clauses.append("document_key=?")
            params.append(_identity(document_id, field="document_id"))
        if subject_id is not None:
            clauses.append("subject_id=?")
            params.append(_identity(subject_id, field="subject_id"))
        with self._lock:
            rows = self._conn.execute(
                "SELECT tenant_id, kb_id, document_key, subject_id, role, "
                "created_at, updated_at FROM resource_access_subject_grants WHERE "
                + " AND ".join(clauses)
                + " ORDER BY document_key, subject_id",
                params,
            ).fetchall()
            epoch = self._epoch_locked(tenant_id, kb_id)
        return [self._grant_record(row, epoch) for row in rows]

    # -- Query authorization ------------------------------------------------

    def authorize_query(
        self,
        principal: Principal,
        kb_id: str,
        *,
        tenant_id: str | None = None,
        permission: Permission | str = Permission.QUERY,
    ) -> QueryAuthorization:
        if not isinstance(principal, Principal):
            raise TypeError("principal must be a Principal")
        kb_id = _identity(kb_id, field="kb_id")
        requested_tenant = (
            principal.tenant_id
            if tenant_id is None
            else _identity(tenant_id, field="tenant_id")
        )
        requested_permission = _permission(permission)
        if requested_tenant != principal.tenant_id:
            return self._deny(
                requested_tenant,
                kb_id,
                requested_permission,
                epoch=0,
                reason="tenant_mismatch",
            )
        if not principal.allows(requested_permission):
            return self._deny(
                requested_tenant,
                kb_id,
                requested_permission,
                epoch=0,
                reason="principal_permission_denied",
            )

        try:
            kb_row, documents, grants, epoch, membership_revoked = (
                self._authorization_snapshot(
                    requested_tenant,
                    kb_id,
                    principal.subject_id,
                    principal.membership_id,
                )
            )
            if membership_revoked:
                return self._deny(
                    requested_tenant,
                    kb_id,
                    requested_permission,
                    epoch=epoch,
                    reason="membership_revoked",
                )
            if kb_row is None:
                if not self._legacy_workspace_default:
                    return self._deny(
                        requested_tenant,
                        kb_id,
                        requested_permission,
                        epoch=epoch,
                        reason="policy_missing",
                    )
                kb_owner = ""
                kb_policy = AccessPolicy.WORKSPACE
            else:
                kb_owner = _identity(kb_row["owner_id"], field="owner_id")
                kb_policy = _policy(kb_row["policy"], document=False)

            # Tenant owner/admin roles bypass resource visibility, never their own
            # role permissions (checked above), and never a missing policy unless
            # the constructor explicitly enabled legacy workspace behavior.
            if principal.role in {Role.OWNER, Role.ADMIN}:
                return self._all(
                    requested_tenant,
                    kb_id,
                    requested_permission,
                    epoch=epoch,
                    reason=f"{principal.role.value}_bypass",
                )
            if self._owner_matches(
                principal, kb_owner, kb_row["owner_membership_id"] if kb_row else None
            ):
                return self._all(
                    requested_tenant,
                    kb_id,
                    requested_permission,
                    epoch=epoch,
                    reason="kb_owner_bypass",
                )

            grants_by_document = {
                str(row["document_key"]): _role(row["role"]) for row in grants
            }
            kb_grant_allows = self._grant_allows(
                grants_by_document.get(_KB_GRANT_DOCUMENT_KEY),
                principal,
                requested_permission,
            )
            kb_allows = kb_policy is AccessPolicy.WORKSPACE or kb_grant_allows

            if not documents:
                if kb_allows:
                    return self._all(
                        requested_tenant,
                        kb_id,
                        requested_permission,
                        epoch=epoch,
                        reason="workspace_or_kb_grant",
                    )
                return self._deny(
                    requested_tenant,
                    kb_id,
                    requested_permission,
                    epoch=epoch,
                    reason="private",
                )

            allowed: list[tuple[str, str]] = []
            for row in documents:
                document_id = _identity(row["document_id"], field="document_id")
                source = _source(row["source"])
                document_owner = _identity(row["owner_id"], field="owner_id")
                document_policy = _policy(row["policy"], document=True)
                document_grant_allows = self._grant_allows(
                    grants_by_document.get(document_id),
                    principal,
                    requested_permission,
                )
                if self._owner_matches(
                    principal, document_owner, row["owner_membership_id"]
                ):
                    document_allows = True
                elif document_policy is AccessPolicy.WORKSPACE:
                    document_allows = True
                elif document_policy is AccessPolicy.PRIVATE:
                    document_allows = document_grant_allows
                else:
                    document_allows = kb_allows or document_grant_allows
                if document_allows:
                    allowed.append((document_id, source))

            if kb_allows and len(allowed) == len(documents):
                return self._all(
                    requested_tenant,
                    kb_id,
                    requested_permission,
                    epoch=epoch,
                    reason="all_documents_allowed",
                )
            if not allowed:
                return self._deny(
                    requested_tenant,
                    kb_id,
                    requested_permission,
                    epoch=epoch,
                    reason="no_documents_allowed",
                )
            allowed.sort(key=lambda item: item[0])
            return QueryAuthorization(
                tenant_id=requested_tenant,
                kb_id=kb_id,
                permission=requested_permission,
                mode=AccessMode.SUBSET,
                acl_epoch=epoch,
                allowed_document_ids=tuple(item[0] for item in allowed),
                allowed_sources=tuple(item[1] for item in allowed),
                reason="document_subset",
            )
        except (sqlite3.Error, ResourceAccessError, TypeError, ValueError):
            # Reads are security decisions: malformed/corrupt/unavailable state is
            # a denial, never a legacy or workspace fallback.
            return self._deny(
                requested_tenant,
                kb_id,
                requested_permission,
                epoch=0,
                reason="store_unavailable",
            )

    query_access = authorize_query
    resolve_query_access = authorize_query

    def allowed_sources(
        self,
        principal: Principal,
        kb_id: str,
        *,
        tenant_id: str | None = None,
        permission: Permission | str = Permission.QUERY,
    ) -> QueryAuthorization:
        """Return the explicit ALL/SUBSET/DENY source authorization snapshot."""

        return self.authorize_query(
            principal,
            kb_id,
            tenant_id=tenant_id,
            permission=permission,
        )

    # -- Cleanup / lifecycle ------------------------------------------------

    def clear_kb(self, tenant_id: str, kb_id: str) -> bool:
        tenant_id = _identity(tenant_id, field="tenant_id")
        kb_id = _identity(kb_id, field="kb_id")
        with self._write_transaction():
            changed = False
            for table in (
                "resource_access_subject_grants",
                "resource_access_document_policies",
                "resource_access_kb_policies",
            ):
                deleted = self._conn.execute(
                    f"DELETE FROM {table} WHERE tenant_id=? AND kb_id=?",
                    (tenant_id, kb_id),
                ).rowcount
                changed = bool(deleted) or changed
            if changed:
                self._bump_epoch_locked(tenant_id, kb_id)
            return changed

    cleanup_kb = clear_kb

    def clear_tenant(self, tenant_id: str) -> int:
        tenant_id = _identity(tenant_id, field="tenant_id")
        with self._write_transaction():
            rows = self._conn.execute(
                "SELECT kb_id FROM resource_access_kb_policies WHERE tenant_id=? "
                "UNION SELECT kb_id FROM resource_access_document_policies "
                "WHERE tenant_id=? "
                "UNION SELECT kb_id FROM resource_access_subject_grants "
                "WHERE tenant_id=? ORDER BY kb_id",
                (tenant_id, tenant_id, tenant_id),
            ).fetchall()
            kb_ids = [str(row["kb_id"]) for row in rows]
            self._conn.execute(
                "DELETE FROM resource_access_membership_tombstones WHERE tenant_id=?",
                (tenant_id,),
            )
            if not kb_ids:
                return 0
            for table in (
                "resource_access_subject_grants",
                "resource_access_document_policies",
                "resource_access_kb_policies",
            ):
                self._conn.execute(
                    f"DELETE FROM {table} WHERE tenant_id=?", (tenant_id,)
                )
            for kb_id in kb_ids:
                self._bump_epoch_locked(tenant_id, kb_id)
            return len(kb_ids)

    cleanup_tenant = clear_tenant

    def check(self) -> bool:
        """Run a cheap, fail-closed SQLite readiness probe.

        Each security-critical table is referenced so a missing or unreadable
        ACL schema cannot be mistaken for an empty, permissive store.
        """

        with self._lock:
            try:
                for table in (
                    "resource_access_kb_policies",
                    "resource_access_document_policies",
                    "resource_access_subject_grants",
                    "resource_access_acl_epochs",
                    "resource_access_membership_tombstones",
                ):
                    self._conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
            except sqlite3.Error as exc:
                raise ResourceAccessError(
                    "resource access store readiness check failed"
                ) from exc
            return True

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> ResourceAccessStore:
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    # -- Internal serialization / snapshots --------------------------------

    def _epoch_locked(self, tenant_id: str, kb_id: str) -> int:
        row = self._conn.execute(
            "SELECT epoch FROM resource_access_acl_epochs "
            "WHERE tenant_id=? AND kb_id=?",
            (tenant_id, kb_id),
        ).fetchone()
        return int(row["epoch"]) if row is not None else 0

    def _authorization_snapshot(
        self,
        tenant_id: str,
        kb_id: str,
        subject_id: str,
        membership_id: str | None,
    ) -> tuple[
        sqlite3.Row | None,
        Sequence[sqlite3.Row],
        Sequence[sqlite3.Row],
        int,
        bool,
    ]:
        # A deferred read transaction gives all four reads one WAL snapshot even
        # when another process mutates ACL state concurrently.
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                kb_row = self._conn.execute(
                    "SELECT owner_id, owner_membership_id, policy "
                    "FROM resource_access_kb_policies "
                    "WHERE tenant_id=? AND kb_id=?",
                    (tenant_id, kb_id),
                ).fetchone()
                documents = self._conn.execute(
                    "SELECT document_id, source, owner_id, owner_membership_id, policy "
                    "FROM resource_access_document_policies "
                    "WHERE tenant_id=? AND kb_id=? ORDER BY document_id",
                    (tenant_id, kb_id),
                ).fetchall()
                grants = self._conn.execute(
                    "SELECT document_key, role FROM resource_access_subject_grants "
                    "WHERE tenant_id=? AND kb_id=? AND subject_id=?",
                    (tenant_id, kb_id, subject_id),
                ).fetchall()
                epoch = self._epoch_locked(tenant_id, kb_id)
                membership_revoked = False
                if membership_id is not None:
                    membership_revoked = (
                        self._conn.execute(
                            "SELECT 1 FROM resource_access_membership_tombstones "
                            "WHERE tenant_id=? AND subject_id=? AND membership_id=?",
                            (tenant_id, subject_id, membership_id),
                        ).fetchone()
                        is not None
                    )
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
        return kb_row, documents, grants, epoch, membership_revoked

    @staticmethod
    def _owner_matches(
        principal: Principal,
        owner_id: str,
        owner_membership_id: object,
    ) -> bool:
        if not owner_id or principal.subject_id != owner_id:
            return False
        if not principal.key_fingerprint.startswith("session:"):
            return True
        return bool(
            principal.membership_id
            and owner_membership_id
            and principal.membership_id == str(owner_membership_id)
        )

    @staticmethod
    def _grant_allows(
        grant_role: Role | None,
        principal: Principal,
        permission: Permission,
    ) -> bool:
        if grant_role is None:
            return False
        # Both sides must grant the capability. An ACL grant can reduce a tenant
        # role but can never elevate it.
        return (
            permission in ROLE_PERMISSIONS[grant_role]
            and permission in principal.permissions
        )

    @staticmethod
    def _all(
        tenant_id: str,
        kb_id: str,
        permission: Permission,
        *,
        epoch: int,
        reason: str,
    ) -> QueryAuthorization:
        return QueryAuthorization(
            tenant_id=tenant_id,
            kb_id=kb_id,
            permission=permission,
            mode=AccessMode.ALL,
            acl_epoch=epoch,
            reason=reason,
        )

    @staticmethod
    def _deny(
        tenant_id: str,
        kb_id: str,
        permission: Permission,
        *,
        epoch: int,
        reason: str,
    ) -> QueryAuthorization:
        return QueryAuthorization(
            tenant_id=tenant_id,
            kb_id=kb_id,
            permission=permission,
            mode=AccessMode.DENY,
            acl_epoch=epoch,
            reason=reason,
        )

    @staticmethod
    def _kb_record(row: Mapping[str, Any], epoch: int) -> dict[str, Any]:
        return {
            "tenant_id": str(row["tenant_id"]),
            "kb_id": str(row["kb_id"]),
            "owner_id": str(row["owner_id"]),
            "owner_membership_id": (
                str(row["owner_membership_id"])
                if row["owner_membership_id"] is not None
                else None
            ),
            "policy": str(row["policy"]),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
            "acl_epoch": epoch,
        }

    @staticmethod
    def _document_record(row: Mapping[str, Any], epoch: int) -> dict[str, Any]:
        return {
            "tenant_id": str(row["tenant_id"]),
            "kb_id": str(row["kb_id"]),
            "document_id": str(row["document_id"]),
            "source": str(row["source"]),
            "owner_id": str(row["owner_id"]),
            "owner_membership_id": (
                str(row["owner_membership_id"])
                if row["owner_membership_id"] is not None
                else None
            ),
            "policy": str(row["policy"]),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
            "acl_epoch": epoch,
        }

    @staticmethod
    def _grant_record(row: Mapping[str, Any], epoch: int) -> dict[str, Any]:
        document_key = str(row["document_key"])
        return {
            "tenant_id": str(row["tenant_id"]),
            "kb_id": str(row["kb_id"]),
            "document_id": document_key or None,
            "subject_id": str(row["subject_id"]),
            "role": str(row["role"]),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
            "acl_epoch": epoch,
        }


__all__ = [
    "AccessMode",
    "AccessPolicy",
    "QueryAccessMode",
    "QueryAuthorization",
    "ResourceAccessConflictError",
    "ResourceAccessDecision",
    "ResourceAccessError",
    "ResourceAccessNotFoundError",
    "ResourceAccessStore",
    "ResourcePolicy",
]
