from __future__ import annotations

import builtins
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from threading import RLock
from typing import Any, Iterable, Iterator, Mapping, Sequence

from cogdoc.api.tenancy import Permission, Principal, ROLE_PERMISSIONS, Role
from cogdoc.ha.dbapi_compat import BackendDBAPIConnection
from cogdoc.ha.storage import DatabaseBackend


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


@dataclass(frozen=True, slots=True)
class DocumentUploadAccessMutation:
    """Compare-and-swap token for provisional upload ACL changes.

    Upload routes publish a document policy before the asynchronous index job
    runs so its commit-time authorization check can see the source.  If the
    job aborts before publishing an index generation, this token restores only
    the state written by that upload.  The expected policy fingerprint and
    role set prevent a delayed rollback from overwriting a concurrent ACL edit.
    """

    tenant_id: str
    kb_id: str
    document_id: str
    source: str
    policy_created: bool
    policy_fingerprint: tuple[str, str | None, str, str, str]
    previous_role_ids: tuple[str, ...]
    expected_role_ids: tuple[str, ...]

    @property
    def changed(self) -> bool:
        return self.policy_created or self.previous_role_ids != self.expected_role_ids


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
        db_path: str | os.PathLike[str] | None,
        *,
        legacy_workspace_default: bool = False,
        busy_timeout_ms: int = 5000,
        backend: DatabaseBackend | None = None,
    ) -> None:
        if (db_path is None) == (backend is None):
            raise ValueError("configure exactly one resource access backend")
        normalized_path = ""
        if db_path is not None:
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
        self._backend = backend
        self._legacy_workspace_default = legacy_workspace_default
        self._lock = RLock()
        self._conn: Any
        if backend is not None:
            self._conn = BackendDBAPIConnection(backend)
        else:
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

    @property
    def backend(self) -> DatabaseBackend | None:
        return self._backend

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
            CREATE TABLE IF NOT EXISTS resource_access_document_role_grants (
                tenant_id TEXT NOT NULL,
                kb_id TEXT NOT NULL,
                document_id TEXT NOT NULL,
                role_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (tenant_id, kb_id, document_id, role_id)
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
            CREATE TABLE IF NOT EXISTS resource_access_subject_locks (
                tenant_id TEXT NOT NULL,
                subject_id TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (tenant_id, subject_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS resource_access_retiring_documents (
                tenant_id TEXT NOT NULL,
                kb_id TEXT NOT NULL,
                document_id TEXT NOT NULL,
                managed_by TEXT NOT NULL,
                started_at TEXT NOT NULL,
                PRIMARY KEY (tenant_id, kb_id, document_id)
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
            """
            CREATE INDEX IF NOT EXISTS idx_resource_access_document_roles
            ON resource_access_document_role_grants
                (tenant_id, kb_id, role_id, document_id)
            """,
        )
        with self._lock:
            for statement in statements:
                self._conn.execute(statement)
            # v1 databases predate membership-incarnation-bound creator access.
            # NULL is intentionally fail-closed for human sessions while keeping
            # local/static-key deployments source compatible.
            for table in (() if self._backend is not None else (
                "resource_access_kb_policies",
                "resource_access_document_policies",
            )):
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
            grant_columns = (
                {"managed_by"}
                if self._backend is not None
                else {
                    str(row[1])
                    for row in self._conn.execute(
                        "PRAGMA table_info(resource_access_subject_grants)"
                    )
                }
            )
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
        if self._membership_revoked_locked(tenant_id, subject_id, membership_id):
            raise ResourceAccessConflictError("membership incarnation was revoked")

    def _lock_subjects_locked(
        self, tenant_id: str, subject_ids: Iterable[str]
    ) -> None:
        """Serialize membership tombstones with every grant publication."""

        for subject_id in sorted(set(subject_ids)):
            self._conn.execute(
                "INSERT INTO resource_access_subject_locks "
                "(tenant_id,subject_id,updated_at) VALUES (?,?,?) "
                "ON CONFLICT(tenant_id,subject_id) DO NOTHING",
                (tenant_id, subject_id, _now_iso()),
            )
            self._conn.execute(
                "SELECT 1 FROM resource_access_subject_locks "
                "WHERE tenant_id=? AND subject_id=?"
                + (
                    " FOR UPDATE"
                    if self._backend is not None
                    and self._backend.kind == "postgres"
                    else ""
                ),
                (tenant_id, subject_id),
            ).fetchone()

    def _membership_revoked_locked(
        self,
        tenant_id: str,
        subject_id: str,
        membership_id: object,
    ) -> bool:
        if membership_id is None:
            return False
        row = self._conn.execute(
            "SELECT 1 FROM resource_access_membership_tombstones "
            "WHERE tenant_id=? AND subject_id=? AND membership_id=?",
            (tenant_id, subject_id, str(membership_id)),
        ).fetchone()
        return row is not None

    def _reject_retiring_document_locked(
        self,
        tenant_id: str,
        kb_id: str,
        document_id: str,
    ) -> None:
        row = self._conn.execute(
            "SELECT 1 FROM resource_access_retiring_documents "
            "WHERE tenant_id=? AND kb_id=? AND document_id=?",
            (tenant_id, kb_id, document_id),
        ).fetchone()
        if row is not None:
            raise ResourceAccessConflictError("document access is retiring")

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
            self._lock_subjects_locked(tenant_id, (owner_id,))
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
                self._reject_retiring_document_locked(tenant_id, kb_id, document_id)
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
                self._lock_subjects_locked(tenant_id, (effective_owner,))
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

    def prepare_document_upload_access(
        self,
        tenant_id: str,
        kb_id: str,
        document_id: str,
        source: str,
        owner_id: str,
        *,
        owner_membership_id: str | None = None,
        role_ids: Iterable[str] | None = None,
    ) -> DocumentUploadAccessMutation:
        """Atomically stage one upload's policy and optional role allowlist."""

        return self.prepare_document_upload_access_batch(
            tenant_id,
            kb_id,
            ((document_id, source),),
            owner_id,
            owner_membership_id=owner_membership_id,
            role_ids=role_ids,
        )[0]

    def prepare_document_upload_access_batch(
        self,
        tenant_id: str,
        kb_id: str,
        documents: Sequence[tuple[str, str]],
        owner_id: str,
        *,
        owner_membership_id: str | None = None,
        role_ids: Iterable[str] | None = None,
    ) -> tuple[DocumentUploadAccessMutation, ...]:
        """Atomically stage document ACLs for an asynchronous upload job.

        Existing policies keep their owner and visibility; only the optional
        role allowlist is replaced.  New policies inherit the KB visibility.
        The returned immutable tokens can later be passed to
        :meth:`rollback_document_upload_access_batch` if indexing aborts.
        """

        tenant = _identity(tenant_id, field="tenant_id")
        knowledge_base = _identity(kb_id, field="kb_id")
        creator = _identity(owner_id, field="owner_id")
        membership = (
            _identity(owner_membership_id, field="owner_membership_id")
            if owner_membership_id is not None
            else None
        )
        if isinstance(documents, (str, bytes)) or not documents:
            raise ValueError("documents must contain at least one document")
        normalized_documents: list[tuple[str, str]] = []
        seen_document_ids: set[str] = set()
        seen_sources: set[str] = set()
        for item in documents:
            if not isinstance(item, (tuple, list)) or len(item) != 2:
                raise TypeError("documents must contain (document_id, source) pairs")
            document = _identity(item[0], field="document_id")
            normalized_source = _source(item[1])
            if document in seen_document_ids or normalized_source in seen_sources:
                raise ValueError("documents must not contain duplicate identities")
            seen_document_ids.add(document)
            seen_sources.add(normalized_source)
            normalized_documents.append((document, normalized_source))

        desired_roles: tuple[str, ...] | None
        if role_ids is None:
            desired_roles = None
        else:
            if isinstance(role_ids, (str, bytes)):
                raise TypeError("role_ids must be an iterable of identifiers")
            desired_roles = tuple(
                sorted({_identity(role_id, field="role_id") for role_id in role_ids})
            )

        now = _now_iso()
        mutations: list[DocumentUploadAccessMutation] = []
        changed = False
        try:
            with self._write_transaction():
                kb_row = self._conn.execute(
                    "SELECT owner_id FROM resource_access_kb_policies "
                    "WHERE tenant_id=? AND kb_id=?",
                    (tenant, knowledge_base),
                ).fetchone()
                if kb_row is None and not self._legacy_workspace_default:
                    raise ResourceAccessNotFoundError(
                        f"knowledge-base policy does not exist: {tenant}/{knowledge_base}"
                    )
                self._lock_subjects_locked(tenant, (creator,))
                self._reject_revoked_membership_locked(tenant, creator, membership)

                for document, normalized_source in normalized_documents:
                    self._reject_retiring_document_locked(
                        tenant, knowledge_base, document
                    )
                    row = self._conn.execute(
                        "SELECT source, owner_id, owner_membership_id, policy, "
                        "created_at, updated_at "
                        "FROM resource_access_document_policies "
                        "WHERE tenant_id=? AND kb_id=? AND document_id=?",
                        (tenant, knowledge_base, document),
                    ).fetchone()
                    policy_created = row is None
                    if row is not None and str(row["source"]) != normalized_source:
                        raise ResourceAccessConflictError(
                            "document identity is already bound to another source: "
                            f"{document}"
                        )
                    if row is None:
                        self._conn.execute(
                            "INSERT INTO resource_access_document_policies "
                            "(tenant_id, kb_id, document_id, source, owner_id, "
                            "owner_membership_id, policy, created_at, updated_at) "
                            "VALUES (?, ?, ?, ?, ?, ?, 'inherit', ?, ?)",
                            (
                                tenant,
                                knowledge_base,
                                document,
                                normalized_source,
                                creator,
                                membership,
                                now,
                                now,
                            ),
                        )
                        row = self._conn.execute(
                            "SELECT source, owner_id, owner_membership_id, policy, "
                            "created_at, updated_at "
                            "FROM resource_access_document_policies "
                            "WHERE tenant_id=? AND kb_id=? AND document_id=?",
                            (tenant, knowledge_base, document),
                        ).fetchone()
                        if row is None:
                            raise ResourceAccessError(
                                "document upload policy was not persisted"
                            )

                    previous_roles = tuple(
                        str(role_row["role_id"])
                        for role_row in self._conn.execute(
                            "SELECT role_id "
                            "FROM resource_access_document_role_grants "
                            "WHERE tenant_id=? AND kb_id=? AND document_id=? "
                            "ORDER BY role_id",
                            (tenant, knowledge_base, document),
                        ).fetchall()
                    )
                    expected_roles = (
                        previous_roles if desired_roles is None else desired_roles
                    )
                    if previous_roles != expected_roles:
                        self._conn.execute(
                            "DELETE FROM resource_access_document_role_grants "
                            "WHERE tenant_id=? AND kb_id=? AND document_id=?",
                            (tenant, knowledge_base, document),
                        )
                        for role_id in expected_roles:
                            self._conn.execute(
                                "INSERT INTO resource_access_document_role_grants "
                                "(tenant_id,kb_id,document_id,role_id,created_at) "
                                "VALUES (?,?,?,?,?)",
                                (tenant, knowledge_base, document, role_id, now),
                            )

                    fingerprint = (
                        str(row["owner_id"]),
                        (
                            str(row["owner_membership_id"])
                            if row["owner_membership_id"] is not None
                            else None
                        ),
                        str(row["policy"]),
                        str(row["created_at"]),
                        str(row["updated_at"]),
                    )
                    mutation = DocumentUploadAccessMutation(
                        tenant_id=tenant,
                        kb_id=knowledge_base,
                        document_id=document,
                        source=normalized_source,
                        policy_created=policy_created,
                        policy_fingerprint=fingerprint,
                        previous_role_ids=previous_roles,
                        expected_role_ids=expected_roles,
                    )
                    mutations.append(mutation)
                    changed = changed or mutation.changed

                if changed:
                    self._bump_epoch_locked(tenant, knowledge_base)
        except sqlite3.IntegrityError as exc:
            if "UNIQUE constraint failed" in str(exc):
                raise ResourceAccessConflictError(
                    "source is already bound to another document"
                ) from exc
            raise
        return tuple(mutations)

    def rollback_document_upload_access(
        self, mutation: DocumentUploadAccessMutation
    ) -> bool:
        """Restore one provisional upload ACL if it was not concurrently edited."""

        return self.rollback_document_upload_access_batch((mutation,))

    def rollback_document_upload_access_batch(
        self, mutations: Sequence[DocumentUploadAccessMutation]
    ) -> bool:
        """Compare-and-swap rollback for an aborted upload ACL publication.

        ``False`` means a policy, role allowlist, grant, or retirement fence was
        changed after staging.  In that case nothing is rolled back, preserving
        the newer authority instead of clobbering it.
        """

        if isinstance(mutations, (str, bytes)):
            raise TypeError("mutations must contain upload ACL tokens")
        tokens = tuple(mutations)
        if not tokens:
            return True
        if any(not isinstance(token, DocumentUploadAccessMutation) for token in tokens):
            raise TypeError("mutations must contain upload ACL tokens")
        tenant = tokens[0].tenant_id
        knowledge_base = tokens[0].kb_id
        if any(
            token.tenant_id != tenant or token.kb_id != knowledge_base
            for token in tokens
        ):
            raise ValueError("upload ACL tokens must share one tenant and knowledge base")
        changed_tokens = tuple(token for token in tokens if token.changed)
        if not changed_tokens:
            return True

        with self._write_transaction():
            # Validate the complete batch before touching any row.  A mixed
            # rollback would be worse than retaining provisional ACL state.
            for token in changed_tokens:
                row = self._conn.execute(
                    "SELECT source, owner_id, owner_membership_id, policy, "
                    "created_at, updated_at "
                    "FROM resource_access_document_policies "
                    "WHERE tenant_id=? AND kb_id=? AND document_id=?",
                    (tenant, knowledge_base, token.document_id),
                ).fetchone()
                if row is None or str(row["source"]) != token.source:
                    return False
                fingerprint = (
                    str(row["owner_id"]),
                    (
                        str(row["owner_membership_id"])
                        if row["owner_membership_id"] is not None
                        else None
                    ),
                    str(row["policy"]),
                    str(row["created_at"]),
                    str(row["updated_at"]),
                )
                if fingerprint != token.policy_fingerprint:
                    return False
                current_roles = tuple(
                    str(role_row["role_id"])
                    for role_row in self._conn.execute(
                        "SELECT role_id "
                        "FROM resource_access_document_role_grants "
                        "WHERE tenant_id=? AND kb_id=? AND document_id=? "
                        "ORDER BY role_id",
                        (tenant, knowledge_base, token.document_id),
                    ).fetchall()
                )
                if current_roles != token.expected_role_ids:
                    return False
                if token.policy_created:
                    grant = self._conn.execute(
                        "SELECT 1 FROM resource_access_subject_grants "
                        "WHERE tenant_id=? AND kb_id=? AND document_key=? LIMIT 1",
                        (tenant, knowledge_base, token.document_id),
                    ).fetchone()
                    retiring = self._conn.execute(
                        "SELECT 1 FROM resource_access_retiring_documents "
                        "WHERE tenant_id=? AND kb_id=? AND document_id=? LIMIT 1",
                        (tenant, knowledge_base, token.document_id),
                    ).fetchone()
                    if grant is not None or retiring is not None:
                        return False

            now = _now_iso()
            for token in changed_tokens:
                self._conn.execute(
                    "DELETE FROM resource_access_document_role_grants "
                    "WHERE tenant_id=? AND kb_id=? AND document_id=?",
                    (tenant, knowledge_base, token.document_id),
                )
                if token.policy_created:
                    self._conn.execute(
                        "DELETE FROM resource_access_document_policies "
                        "WHERE tenant_id=? AND kb_id=? AND document_id=?",
                        (tenant, knowledge_base, token.document_id),
                    )
                else:
                    for role_id in token.previous_role_ids:
                        self._conn.execute(
                            "INSERT INTO resource_access_document_role_grants "
                            "(tenant_id,kb_id,document_id,role_id,created_at) "
                            "VALUES (?,?,?,?,?)",
                            (tenant, knowledge_base, token.document_id, role_id, now),
                        )
            self._bump_epoch_locked(tenant, knowledge_base)
        return True

    def apply_managed_document_access(
        self,
        tenant_id: str,
        kb_id: str,
        document_id: str,
        source: str,
        owner_id: str,
        policy: AccessPolicy | str,
        managed_by: str,
        grants: Sequence[tuple[str, Role | str, str | None]],
        *,
        owner_membership_id: str | None = None,
    ) -> dict[str, Any]:
        """Atomically publish a document policy and one provider's grants.

        Provider ACL refreshes must never expose a mixed generation such as a
        newly-private document with stale provider-managed grants.  Keeping the
        policy change and managed-grant replacement in one SQLite transaction
        makes the authorization edge advance as a single unit.  Explicit local
        grants are deliberately preserved.
        """

        tenant_id = _identity(tenant_id, field="tenant_id")
        kb_id = _identity(kb_id, field="kb_id")
        document_id = _identity(document_id, field="document_id")
        source = _source(source)
        owner_id = _identity(owner_id, field="owner_id")
        normalized_policy = _policy(policy, document=True)
        if normalized_policy is AccessPolicy.INHERIT:
            raise ValueError("externally managed document policy cannot inherit")
        managed_by = _identity(managed_by, field="managed_by")
        if owner_membership_id is not None:
            owner_membership_id = _identity(
                owner_membership_id, field="owner_membership_id"
            )

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
        revoked_ignored = 0
        changed = False
        try:
            with self._write_transaction():
                self._lock_subjects_locked(
                    tenant_id, (*desired.keys(), owner_id)
                )
                self._reject_retiring_document_locked(tenant_id, kb_id, document_id)
                kb_row = self._conn.execute(
                    "SELECT 1 FROM resource_access_kb_policies "
                    "WHERE tenant_id=? AND kb_id=?",
                    (tenant_id, kb_id),
                ).fetchone()
                if kb_row is None and not self._legacy_workspace_default:
                    raise ResourceAccessNotFoundError(
                        f"knowledge-base policy does not exist: {tenant_id}/{kb_id}"
                    )
                self._reject_revoked_membership_locked(
                    tenant_id, owner_id, owner_membership_id
                )
                # Identity resolution can race with membership revocation.
                # Omit those stale provider subjects inside this same ACL
                # transaction so other upstream removals still commit instead
                # of rolling back to a broader previous allowlist.
                for subject, (_role_value, membership_id) in tuple(desired.items()):
                    if self._membership_revoked_locked(
                        tenant_id, subject, membership_id
                    ):
                        desired.pop(subject)
                        revoked_ignored += 1
                document = self._conn.execute(
                    "SELECT source,owner_id,owner_membership_id,policy,created_at "
                    "FROM resource_access_document_policies "
                    "WHERE tenant_id=? AND kb_id=? AND document_id=?",
                    (tenant_id, kb_id, document_id),
                ).fetchone()
                document_changed = (
                    document is None
                    or str(document["source"]) != source
                    or str(document["owner_id"]) != owner_id
                    or document["owner_membership_id"] != owner_membership_id
                    or str(document["policy"]) != normalized_policy.value
                )
                if document_changed:
                    created_at = (
                        str(document["created_at"]) if document is not None else now
                    )
                    self._conn.execute(
                        "INSERT INTO resource_access_document_policies "
                        "(tenant_id,kb_id,document_id,source,owner_id,"
                        "owner_membership_id,policy,created_at,updated_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?) "
                        "ON CONFLICT(tenant_id,kb_id,document_id) DO UPDATE SET "
                        "source=excluded.source,owner_id=excluded.owner_id,"
                        "owner_membership_id=excluded.owner_membership_id,"
                        "policy=excluded.policy,updated_at=excluded.updated_at",
                        (
                            tenant_id,
                            kb_id,
                            document_id,
                            source,
                            owner_id,
                            owner_membership_id,
                            normalized_policy.value,
                            created_at,
                            now,
                        ),
                    )
                    changed = True

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
                            "DELETE FROM resource_access_subject_grants "
                            "WHERE tenant_id=? AND kb_id=? AND document_key=? "
                            "AND subject_id=? AND managed_by=?",
                            (tenant_id, kb_id, document_id, subject, managed_by),
                        )
                        removed += 1
                        changed = True
                for subject, (role, membership_id) in desired.items():
                    row = existing.get(subject)
                    if row is not None and str(row["managed_by"] or "") != managed_by:
                        manual_preserved += 1
                        continue
                    if row is None:
                        self._conn.execute(
                            "INSERT INTO resource_access_subject_grants "
                            "(tenant_id,kb_id,document_key,subject_id,role,managed_by,"
                            "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
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
        except sqlite3.IntegrityError as exc:
            if "UNIQUE constraint failed" in str(exc):
                raise ResourceAccessConflictError(
                    f"source is already bound to another document: {source}"
                ) from exc
            raise
        return {
            "managed_by": managed_by,
            "applied": applied,
            "removed": removed,
            "manual_preserved": manual_preserved,
            "revoked_ignored": revoked_ignored,
            "acl_epoch": epoch,
            "policy": normalized_policy.value,
        }

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
            self._reject_retiring_document_locked(tenant_id, kb_id, document_id)
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
            self._conn.execute(
                "DELETE FROM resource_access_document_role_grants "
                "WHERE tenant_id=? AND kb_id=? AND document_id=?",
                (tenant_id, kb_id, document_id),
            )
            self._bump_epoch_locked(tenant_id, kb_id)
            return True

    def quarantine_document_access(
        self, tenant_id: str, kb_id: str, document_id: str
    ) -> bool:
        """Atomically make a retiring document private and revoke all grants.

        Connection teardown removes source files before a potentially slow
        index rebuild. Keeping the policy row as a private deny boundary until
        that rebuild succeeds prevents an old active index from falling back
        to workspace visibility after a partial cleanup.
        """

        return bool(
            self.quarantine_documents_access(
                tenant_id,
                kb_id,
                (document_id,),
            )
        )

    def quarantine_documents_access(
        self,
        tenant_id: str,
        kb_id: str,
        document_ids: Iterable[str],
    ) -> int:
        """Quarantine a connector's retiring documents in one transaction."""

        tenant = _identity(tenant_id, field="tenant_id")
        knowledge_base = _identity(kb_id, field="kb_id")
        if isinstance(document_ids, (str, bytes)):
            raise TypeError("document_ids must be an iterable of identifiers")
        documents = tuple(
            dict.fromkeys(
                _identity(document_id, field="document_id")
                for document_id in document_ids
            )
        )
        if not documents:
            return 0
        found = 0
        changed = False
        now = _now_iso()
        with self._write_transaction():
            for document in documents:
                row = self._conn.execute(
                    "SELECT policy FROM resource_access_document_policies "
                    "WHERE tenant_id=? AND kb_id=? AND document_id=?",
                    (tenant, knowledge_base, document),
                ).fetchone()
                if row is None:
                    continue
                found += 1
                removed = self._conn.execute(
                    "DELETE FROM resource_access_subject_grants "
                    "WHERE tenant_id=? AND kb_id=? AND document_key=?",
                    (tenant, knowledge_base, document),
                ).rowcount
                policy_changed = str(row["policy"]) != AccessPolicy.PRIVATE.value
                if policy_changed:
                    self._conn.execute(
                        "UPDATE resource_access_document_policies "
                        "SET policy=?,updated_at=? "
                        "WHERE tenant_id=? AND kb_id=? AND document_id=?",
                        (
                            AccessPolicy.PRIVATE.value,
                            now,
                            tenant,
                            knowledge_base,
                            document,
                        ),
                    )
                changed = changed or policy_changed or bool(removed)
            if changed:
                self._bump_epoch_locked(tenant, knowledge_base)
        return found

    def begin_document_retirement(
        self,
        tenant_id: str,
        kb_id: str,
        managed_by: str,
        document_ids: Iterable[str],
    ) -> int:
        """Fence and quarantine retiring documents in one durable commit."""

        tenant = _identity(tenant_id, field="tenant_id")
        knowledge_base = _identity(kb_id, field="kb_id")
        manager = _identity(managed_by, field="managed_by")
        if isinstance(document_ids, (str, bytes)):
            raise TypeError("document_ids must be an iterable of identifiers")
        documents = tuple(
            dict.fromkeys(
                _identity(document_id, field="document_id")
                for document_id in document_ids
            )
        )
        if not documents:
            return 0
        changed = False
        now = _now_iso()
        with self._write_transaction():
            for document in documents:
                existing_fence = self._conn.execute(
                    "SELECT managed_by FROM resource_access_retiring_documents "
                    "WHERE tenant_id=? AND kb_id=? AND document_id=?",
                    (tenant, knowledge_base, document),
                ).fetchone()
                if (
                    existing_fence is not None
                    and str(existing_fence["managed_by"]) != manager
                ):
                    raise ResourceAccessConflictError(
                        "document is retiring under another manager"
                    )
                if existing_fence is None:
                    self._conn.execute(
                        "INSERT INTO resource_access_retiring_documents "
                        "(tenant_id,kb_id,document_id,managed_by,started_at) "
                        "VALUES (?,?,?,?,?)",
                        (tenant, knowledge_base, document, manager, now),
                    )
                    changed = True
                row = self._conn.execute(
                    "SELECT policy FROM resource_access_document_policies "
                    "WHERE tenant_id=? AND kb_id=? AND document_id=?",
                    (tenant, knowledge_base, document),
                ).fetchone()
                if row is None:
                    continue
                removed = self._conn.execute(
                    "DELETE FROM resource_access_subject_grants "
                    "WHERE tenant_id=? AND kb_id=? AND document_key=?",
                    (tenant, knowledge_base, document),
                ).rowcount
                if str(row["policy"]) != AccessPolicy.PRIVATE.value:
                    self._conn.execute(
                        "UPDATE resource_access_document_policies SET policy=?,"
                        "updated_at=? WHERE tenant_id=? AND kb_id=? AND document_id=?",
                        (
                            AccessPolicy.PRIVATE.value,
                            now,
                            tenant,
                            knowledge_base,
                            document,
                        ),
                    )
                    changed = True
                changed = changed or bool(removed)
            if changed:
                self._bump_epoch_locked(tenant, knowledge_base)
        return len(documents)

    def finish_document_retirement(
        self,
        tenant_id: str,
        kb_id: str,
        managed_by: str,
        document_ids: Iterable[str],
    ) -> int:
        """Remove fenced ACL state only after the index no longer contains it."""

        tenant = _identity(tenant_id, field="tenant_id")
        knowledge_base = _identity(kb_id, field="kb_id")
        manager = _identity(managed_by, field="managed_by")
        if isinstance(document_ids, (str, bytes)):
            raise TypeError("document_ids must be an iterable of identifiers")
        documents = tuple(
            dict.fromkeys(
                _identity(document_id, field="document_id")
                for document_id in document_ids
            )
        )
        removed_fences = 0
        changed = False
        with self._write_transaction():
            for document in documents:
                fence = self._conn.execute(
                    "SELECT managed_by FROM resource_access_retiring_documents "
                    "WHERE tenant_id=? AND kb_id=? AND document_id=?",
                    (tenant, knowledge_base, document),
                ).fetchone()
                if fence is None:
                    continue
                if str(fence["managed_by"]) != manager:
                    raise ResourceAccessConflictError(
                        "document is retiring under another manager"
                    )
                changed = (
                    bool(
                        self._conn.execute(
                            "DELETE FROM resource_access_subject_grants "
                            "WHERE tenant_id=? AND kb_id=? AND document_key=?",
                            (tenant, knowledge_base, document),
                        ).rowcount
                    )
                    or changed
                )
                changed = (
                    bool(
                        self._conn.execute(
                            "DELETE FROM resource_access_document_role_grants "
                            "WHERE tenant_id=? AND kb_id=? AND document_id=?",
                            (tenant, knowledge_base, document),
                        ).rowcount
                    )
                    or changed
                )
                changed = (
                    bool(
                        self._conn.execute(
                            "DELETE FROM resource_access_document_policies "
                            "WHERE tenant_id=? AND kb_id=? AND document_id=?",
                            (tenant, knowledge_base, document),
                        ).rowcount
                    )
                    or changed
                )
                self._conn.execute(
                    "DELETE FROM resource_access_retiring_documents "
                    "WHERE tenant_id=? AND kb_id=? AND document_id=? AND managed_by=?",
                    (tenant, knowledge_base, document, manager),
                )
                removed_fences += 1
                changed = True
            if changed:
                self._bump_epoch_locked(tenant, knowledge_base)
        return removed_fences

    def retiring_document_ids(
        self,
        tenant_id: str,
        kb_id: str,
        managed_by: str,
    ) -> tuple[str, ...]:
        tenant = _identity(tenant_id, field="tenant_id")
        knowledge_base = _identity(kb_id, field="kb_id")
        manager = _identity(managed_by, field="managed_by")
        with self._lock:
            rows = self._conn.execute(
                "SELECT document_id FROM resource_access_retiring_documents "
                "WHERE tenant_id=? AND kb_id=? AND managed_by=? ORDER BY document_id",
                (tenant, knowledge_base, manager),
            ).fetchall()
        return tuple(str(row[0]) for row in rows)

    # -- Subject grant CRUD -------------------------------------------------

    def replace_document_roles(
        self,
        tenant_id: str,
        kb_id: str,
        document_id: str,
        role_ids: Iterable[str],
    ) -> dict[str, Any]:
        """Replace a document's role allowlist atomically.

        An empty allowlist preserves the legacy ACL behavior. Once at least one
        role is present, non-privileged readers must match one of those roles in
        addition to satisfying the existing knowledge-base/document policy.
        """

        tenant = _identity(tenant_id, field="tenant_id")
        knowledge_base = _identity(kb_id, field="kb_id")
        document = _identity(document_id, field="document_id")
        if isinstance(role_ids, (str, bytes)):
            raise TypeError("role_ids must be an iterable of identifiers")
        desired = tuple(
            sorted(
                {
                    _identity(role_id, field="role_id")
                    for role_id in role_ids
                }
            )
        )
        now = _now_iso()
        with self._write_transaction():
            self._reject_retiring_document_locked(tenant, knowledge_base, document)
            exists = self._conn.execute(
                "SELECT 1 FROM resource_access_document_policies "
                "WHERE tenant_id=? AND kb_id=? AND document_id=?",
                (tenant, knowledge_base, document),
            ).fetchone()
            if exists is None:
                raise ResourceAccessNotFoundError(
                    f"document policy does not exist: {document}"
                )
            current = {
                str(row["role_id"])
                for row in self._conn.execute(
                    "SELECT role_id FROM resource_access_document_role_grants "
                    "WHERE tenant_id=? AND kb_id=? AND document_id=?",
                    (tenant, knowledge_base, document),
                ).fetchall()
            }
            wanted = set(desired)
            changed = current != wanted
            if changed:
                self._conn.execute(
                    "DELETE FROM resource_access_document_role_grants "
                    "WHERE tenant_id=? AND kb_id=? AND document_id=?",
                    (tenant, knowledge_base, document),
                )
                for role_id in desired:
                    self._conn.execute(
                        "INSERT INTO resource_access_document_role_grants "
                        "(tenant_id,kb_id,document_id,role_id,created_at) "
                        "VALUES (?,?,?,?,?)",
                        (tenant, knowledge_base, document, role_id, now),
                    )
                epoch = self._bump_epoch_locked(tenant, knowledge_base)
            else:
                epoch = self._epoch_locked(tenant, knowledge_base)
        return {
            "tenant_id": tenant,
            "kb_id": knowledge_base,
            "document_id": document,
            "role_ids": list(desired),
            "acl_epoch": epoch,
        }

    def replace_kb_roles(
        self, tenant_id: str, kb_id: str, role_ids: Iterable[str]
    ) -> dict[str, Any]:
        tenant = _identity(tenant_id, field="tenant_id")
        knowledge_base = _identity(kb_id, field="kb_id")
        if isinstance(role_ids, (str, bytes)):
            raise TypeError("role_ids must be an iterable of identifiers")
        desired = tuple(
            sorted({_identity(role_id, field="role_id") for role_id in role_ids})
        )
        now = _now_iso()
        with self._write_transaction():
            exists = self._conn.execute(
                "SELECT 1 FROM resource_access_kb_policies "
                "WHERE tenant_id=? AND kb_id=?",
                (tenant, knowledge_base),
            ).fetchone()
            if exists is None:
                raise ResourceAccessNotFoundError(
                    f"knowledge-base policy does not exist: {tenant}/{knowledge_base}"
                )
            current = {
                str(row["role_id"])
                for row in self._conn.execute(
                    "SELECT role_id FROM resource_access_document_role_grants "
                    "WHERE tenant_id=? AND kb_id=? AND document_id=?",
                    (tenant, knowledge_base, _KB_GRANT_DOCUMENT_KEY),
                ).fetchall()
            }
            wanted = set(desired)
            if current != wanted:
                self._conn.execute(
                    "DELETE FROM resource_access_document_role_grants "
                    "WHERE tenant_id=? AND kb_id=? AND document_id=?",
                    (tenant, knowledge_base, _KB_GRANT_DOCUMENT_KEY),
                )
                for role_id in desired:
                    self._conn.execute(
                        "INSERT INTO resource_access_document_role_grants "
                        "(tenant_id,kb_id,document_id,role_id,created_at) "
                        "VALUES (?,?,?,?,?)",
                        (
                            tenant,
                            knowledge_base,
                            _KB_GRANT_DOCUMENT_KEY,
                            role_id,
                            now,
                        ),
                    )
                epoch = self._bump_epoch_locked(tenant, knowledge_base)
            else:
                epoch = self._epoch_locked(tenant, knowledge_base)
        return {
            "tenant_id": tenant,
            "kb_id": knowledge_base,
            "role_ids": list(desired),
            "acl_epoch": epoch,
        }

    def list_kb_roles(self, tenant_id: str, kb_id: str) -> builtins.list[str]:
        tenant = _identity(tenant_id, field="tenant_id")
        knowledge_base = _identity(kb_id, field="kb_id")
        with self._lock:
            rows = self._conn.execute(
                "SELECT role_id FROM resource_access_document_role_grants "
                "WHERE tenant_id=? AND kb_id=? AND document_id=? ORDER BY role_id",
                (tenant, knowledge_base, _KB_GRANT_DOCUMENT_KEY),
            ).fetchall()
        return [str(row["role_id"]) for row in rows]

    def role_usage_count(self, tenant_id: str, role_id: str) -> int:
        tenant = _identity(tenant_id, field="tenant_id")
        role = _identity(role_id, field="role_id")
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM resource_access_document_role_grants "
                "WHERE tenant_id=? AND role_id=?",
                (tenant, role),
            ).fetchone()
        return int(row[0]) if row is not None else 0

    def list_document_roles(
        self, tenant_id: str, kb_id: str, document_id: str
    ) -> builtins.list[str]:
        tenant = _identity(tenant_id, field="tenant_id")
        knowledge_base = _identity(kb_id, field="kb_id")
        document = _identity(document_id, field="document_id")
        with self._lock:
            rows = self._conn.execute(
                "SELECT role_id FROM resource_access_document_role_grants "
                "WHERE tenant_id=? AND kb_id=? AND document_id=? ORDER BY role_id",
                (tenant, knowledge_base, document),
            ).fetchall()
        return [str(row["role_id"]) for row in rows]

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
            self._lock_subjects_locked(tenant_id, (subject_id,))
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
                self._reject_retiring_document_locked(tenant_id, kb_id, document_key)
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
            self._lock_subjects_locked(tenant_id, desired.keys())
            self._reject_retiring_document_locked(tenant_id, kb_id, document_id)
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
            self._lock_subjects_locked(tenant_id, (subject_id,))
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
            kb_row, documents, grants, role_grants, retirements, epoch, membership_revoked = (
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

            # A retirement fence removes content from every read/query/review
            # projection while the old index generation may still contain it.
            # It must not revoke the control-plane authority needed to finish
            # that same cleanup: otherwise an admin deleting the only document
            # would deny its own phase-two MANAGE_ACCESS revalidation forever.
            content_permissions = {
                Permission.READ,
                Permission.QUERY,
                Permission.WRITE,
                Permission.REVIEW,
                Permission.PUBLISH,
            }
            visible_retirements = (
                retirements
                if requested_permission in content_permissions
                else frozenset()
            )

            # Tenant owner/admin roles bypass resource visibility, never their own
            # role permissions (checked above), and never a missing policy unless
            # the constructor explicitly enabled legacy workspace behavior.
            privileged_bypass = (
                principal.role in {Role.OWNER, Role.ADMIN}
                and principal.effective_access_role_id
                in {Role.OWNER.value, Role.ADMIN.value}
            )
            kb_owner_bypass = self._owner_matches(
                principal, kb_owner, kb_row["owner_membership_id"] if kb_row else None
            )
            if privileged_bypass and not visible_retirements:
                return self._all(
                    requested_tenant,
                    kb_id,
                    requested_permission,
                    epoch=epoch,
                    reason=f"{principal.role.value}_bypass",
                )
            if kb_owner_bypass and not visible_retirements:
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
            roles_by_document: dict[str, set[str]] = {}
            for row in role_grants:
                roles_by_document.setdefault(str(row["document_id"]), set()).add(
                    str(row["role_id"])
                )
            required_kb_roles = roles_by_document.get(_KB_GRANT_DOCUMENT_KEY)
            kb_role_allows = (
                not required_kb_roles
                or principal.effective_access_role_id in required_kb_roles
            )
            kb_grant_allows = self._grant_allows(
                grants_by_document.get(_KB_GRANT_DOCUMENT_KEY),
                principal,
                requested_permission,
            )
            kb_allows = (
                privileged_bypass
                or kb_owner_bypass
                or (
                    kb_role_allows
                    and (
                        kb_policy is AccessPolicy.WORKSPACE
                        or kb_grant_allows
                    )
                )
            )

            if not documents:
                if kb_allows and not visible_retirements:
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
                if document_id in visible_retirements:
                    continue
                source = _source(row["source"])
                document_owner = _identity(row["owner_id"], field="owner_id")
                document_policy = _policy(row["policy"], document=True)
                document_grant_allows = self._grant_allows(
                    grants_by_document.get(document_id),
                    principal,
                    requested_permission,
                )
                if privileged_bypass or kb_owner_bypass:
                    document_allows = True
                elif self._owner_matches(
                    principal, document_owner, row["owner_membership_id"]
                ):
                    document_allows = True
                elif document_policy is AccessPolicy.WORKSPACE:
                    document_allows = True
                elif document_policy is AccessPolicy.PRIVATE:
                    document_allows = document_grant_allows
                else:
                    document_allows = kb_allows or document_grant_allows
                required_roles = roles_by_document.get(document_id)
                if (
                    document_allows
                    and not kb_role_allows
                    and not privileged_bypass
                    and not kb_owner_bypass
                    and not self._owner_matches(
                        principal, document_owner, row["owner_membership_id"]
                    )
                ):
                    document_allows = False
                if (
                    document_allows
                    and required_roles
                    and not privileged_bypass
                    and not kb_owner_bypass
                    and not self._owner_matches(
                        principal, document_owner, row["owner_membership_id"]
                    )
                ):
                    document_allows = (
                        principal.effective_access_role_id in required_roles
                    )
                if document_allows:
                    allowed.append((document_id, source))

            if kb_allows and not visible_retirements and len(allowed) == len(documents):
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
                "resource_access_document_role_grants",
                "resource_access_document_policies",
                "resource_access_retiring_documents",
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
                "WHERE tenant_id=? UNION SELECT kb_id FROM "
                "resource_access_document_role_grants WHERE tenant_id=? "
                "UNION SELECT kb_id FROM "
                "resource_access_retiring_documents WHERE tenant_id=? ORDER BY kb_id",
                (tenant_id, tenant_id, tenant_id, tenant_id, tenant_id),
            ).fetchall()
            kb_ids = [str(row["kb_id"]) for row in rows]
            self._conn.execute(
                "DELETE FROM resource_access_membership_tombstones WHERE tenant_id=?",
                (tenant_id,),
            )
            self._conn.execute(
                "DELETE FROM resource_access_subject_locks WHERE tenant_id=?",
                (tenant_id,),
            )
            if not kb_ids:
                return 0
            for table in (
                "resource_access_subject_grants",
                "resource_access_document_role_grants",
                "resource_access_document_policies",
                "resource_access_retiring_documents",
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
        """Run a cheap, fail-closed ACL-store readiness probe.

        Each security-critical table is referenced so a missing or unreadable
        ACL schema cannot be mistaken for an empty, permissive store.
        """

        with self._lock:
            try:
                for table in (
                    "resource_access_kb_policies",
                    "resource_access_document_policies",
                    "resource_access_subject_grants",
                    "resource_access_document_role_grants",
                    "resource_access_acl_epochs",
                    "resource_access_membership_tombstones",
                    "resource_access_subject_locks",
                    "resource_access_retiring_documents",
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
        Sequence[sqlite3.Row],
        frozenset[str],
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
                role_grants = self._conn.execute(
                    "SELECT document_id,role_id FROM "
                    "resource_access_document_role_grants "
                    "WHERE tenant_id=? AND kb_id=?",
                    (tenant_id, kb_id),
                ).fetchall()
                retirements = frozenset(
                    str(row["document_id"])
                    for row in self._conn.execute(
                        "SELECT document_id FROM resource_access_retiring_documents "
                        "WHERE tenant_id=? AND kb_id=?",
                        (tenant_id, kb_id),
                    ).fetchall()
                )
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
        return (
            kb_row,
            documents,
            grants,
            role_grants,
            retirements,
            epoch,
            membership_revoked,
        )

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
    "DocumentUploadAccessMutation",
    "QueryAccessMode",
    "QueryAuthorization",
    "ResourceAccessConflictError",
    "ResourceAccessDecision",
    "ResourceAccessError",
    "ResourceAccessNotFoundError",
    "ResourceAccessStore",
    "ResourcePolicy",
]
