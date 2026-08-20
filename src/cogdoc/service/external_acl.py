from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Iterable
from dataclasses import dataclass
from threading import RLock
from typing import Any, Mapping, Protocol

from cogdoc.api.persistence import connect_sqlite
from cogdoc.api.resource_access import AccessPolicy, ResourceAccessStore
from cogdoc.api.tenancy import Role
from cogdoc.connectors.base import MAX_CONNECTOR_ACL_BYTES, MAX_CONNECTOR_ACL_GRANTS


@dataclass(frozen=True)
class ExternalGrant:
    external_subject: str
    permission: str = "read"
    subject_type: str = "user"

    def __post_init__(self) -> None:
        if not isinstance(self.external_subject, str):
            raise TypeError("external_subject must be a string")
        subject = self.external_subject.strip().casefold()
        if (
            not subject
            or len(subject.encode("utf-8")) > 320
            or any(
                ord(character) < 32 or ord(character) == 127 for character in subject
            )
        ):
            raise ValueError("external_subject is invalid")
        if not isinstance(self.permission, str):
            raise TypeError("external permission must be a string")
        if self.permission not in {"read", "review", "write"}:
            raise ValueError("external permission is unsupported")
        if not isinstance(self.subject_type, str):
            raise TypeError("external subject_type must be a string")
        if self.subject_type not in {"user", "group"}:
            raise ValueError("external subject_type is unsupported")
        object.__setattr__(self, "external_subject", subject)


@dataclass(frozen=True)
class ExternalAclSnapshot:
    grants: tuple[ExternalGrant, ...] = ()
    workspace_visible: bool = False
    complete: bool = True
    provider_version: str | None = None

    def __post_init__(self) -> None:
        if type(self.workspace_visible) is not bool or type(self.complete) is not bool:
            raise TypeError("ACL completeness and visibility must be booleans")
        grants = tuple(self.grants)
        if any(not isinstance(grant, ExternalGrant) for grant in grants):
            raise TypeError("external ACL grants must be ExternalGrant values")
        object.__setattr__(self, "grants", grants)
        if len(grants) > MAX_CONNECTOR_ACL_GRANTS:
            raise ValueError("external ACL grant count exceeds the limit")
        if self.provider_version is not None:
            if not isinstance(self.provider_version, str):
                raise TypeError("provider_version must be a string")
            if len(self.provider_version.encode("utf-8")) > 1024 or any(
                ord(character) < 32 or ord(character) == 127
                for character in self.provider_version
            ):
                raise ValueError("provider_version is invalid")
        keys = [(grant.subject_type, grant.external_subject) for grant in grants]
        if len(keys) != len(set(keys)):
            raise ValueError("external ACL contains duplicate subjects")

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any] | None) -> ExternalAclSnapshot:
        if payload is None:
            return cls(complete=False)
        if not isinstance(payload, Mapping):
            raise ValueError("external ACL must be a mapping")
        try:
            encoded = json.dumps(
                dict(payload),
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        except (RecursionError, TypeError, ValueError) as exc:
            raise ValueError("external ACL must be a finite JSON object") from exc
        if len(encoded) > MAX_CONNECTOR_ACL_BYTES:
            raise ValueError("external ACL exceeds the byte limit")
        raw_grants = payload.get("grants", [])
        if not isinstance(raw_grants, list):
            raise ValueError("external ACL grants must be a list")
        if len(raw_grants) > MAX_CONNECTOR_ACL_GRANTS:
            raise ValueError("external ACL grant count exceeds the limit")
        if any(not isinstance(row, Mapping) for row in raw_grants):
            raise ValueError("external ACL grants must contain objects")
        workspace_visible = payload.get("workspace_visible", False)
        complete = payload.get("complete", False)
        if type(workspace_visible) is not bool or type(complete) is not bool:
            raise ValueError("external ACL flags must be booleans")
        return cls(
            grants=tuple(
                ExternalGrant(
                    row.get("external_subject") or row.get("email") or "",
                    row.get("permission") or "read",
                    row.get("subject_type") or "user",
                )
                for row in raw_grants
            ),
            workspace_visible=workspace_visible,
            complete=complete,
            provider_version=payload.get("provider_version"),
        )

    def fingerprint(self) -> str:
        payload = {
            "complete": self.complete,
            "workspace_visible": self.workspace_visible,
            "provider_version": self.provider_version,
            "grants": sorted(
                (grant.subject_type, grant.external_subject, grant.permission)
                for grant in self.grants
            ),
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


class IdentityResolver(Protocol):
    def resolve(
        self, tenant_id: str, grant: ExternalGrant
    ) -> tuple[str, str | None] | None: ...


class WorkspaceIdentityResolver:
    """Resolve external user email to a current workspace membership."""

    def __init__(self, auth_store: Any) -> None:
        self.auth_store = auth_store

    def resolve(
        self, tenant_id: str, grant: ExternalGrant
    ) -> tuple[str, str | None] | None:
        if grant.subject_type != "user":
            return None
        user = self.auth_store.lookup_user(grant.external_subject)
        if not user:
            return None
        subject_id = str(user.get("user_id") or "")
        membership = self.auth_store.membership(tenant_id, subject_id)
        if not membership:
            return None
        membership_id = str(
            membership.get("member_id") or membership.get("membership_id") or ""
        )
        if not subject_id or not membership_id:
            return None
        return subject_id, membership_id


class ExternalAclSyncStore:
    def __init__(self, db_path: str):
        self._lock = RLock()
        self._conn = connect_sqlite(db_path)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS external_acl_sync_state ("
            "tenant_id TEXT NOT NULL,kb_id TEXT NOT NULL,document_id TEXT NOT NULL,"
            "managed_by TEXT NOT NULL,status TEXT NOT NULL,acl_fingerprint TEXT NOT NULL,"
            "provider_version TEXT,resolved_count INTEGER NOT NULL,unresolved_count INTEGER NOT NULL,"
            "updated_at REAL NOT NULL,PRIMARY KEY(tenant_id,kb_id,document_id,managed_by))"
        )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def record(
        self,
        *,
        tenant_id: str,
        kb_id: str,
        document_id: str,
        managed_by: str,
        status: str,
        snapshot: ExternalAclSnapshot,
        resolved_count: int,
        unresolved_count: int,
    ) -> dict[str, Any]:
        if status not in {"current", "quarantined"}:
            raise ValueError("external ACL status is invalid")
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO external_acl_sync_state "
                "(tenant_id,kb_id,document_id,managed_by,status,acl_fingerprint,provider_version,"
                "resolved_count,unresolved_count,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(tenant_id,kb_id,document_id,managed_by) DO UPDATE SET "
                "status=excluded.status,acl_fingerprint=excluded.acl_fingerprint,"
                "provider_version=excluded.provider_version,resolved_count=excluded.resolved_count,"
                "unresolved_count=excluded.unresolved_count,updated_at=excluded.updated_at",
                (
                    tenant_id,
                    kb_id,
                    document_id,
                    managed_by,
                    status,
                    snapshot.fingerprint(),
                    snapshot.provider_version,
                    resolved_count,
                    unresolved_count,
                    now,
                ),
            )
        return self.get(tenant_id, kb_id, document_id, managed_by) or {}

    def get(
        self, tenant_id: str, kb_id: str, document_id: str, managed_by: str
    ) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT status,acl_fingerprint,provider_version,resolved_count,unresolved_count,updated_at "
                "FROM external_acl_sync_state WHERE tenant_id=? AND kb_id=? AND document_id=? AND managed_by=?",
                (tenant_id, kb_id, document_id, managed_by),
            ).fetchone()
        if row is None:
            return None
        return {
            "status": row[0],
            "acl_fingerprint": row[1],
            "provider_version": row[2],
            "resolved_count": row[3],
            "unresolved_count": row[4],
            "updated_at": row[5],
        }

    def delete_scope(self, tenant_id: str, kb_id: str) -> int:
        """Remove provider ACL checkpoints for a deleted KB incarnation."""

        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM external_acl_sync_state WHERE tenant_id=? AND kb_id=?",
                (str(tenant_id), str(kb_id)),
            )
        return int(cursor.rowcount)

    def managed_document_ids(
        self,
        tenant_id: str,
        kb_id: str,
        managed_by: str,
    ) -> tuple[str, ...]:
        """Return every document ever checkpointed by one connector."""

        tenant = str(tenant_id).strip()
        knowledge_base = str(kb_id).strip()
        manager = str(managed_by).strip()
        if not tenant or not knowledge_base or not manager:
            raise ValueError("ACL checkpoint scope must not be empty")
        with self._lock:
            rows = self._conn.execute(
                "SELECT document_id FROM external_acl_sync_state "
                "WHERE tenant_id=? AND kb_id=? AND managed_by=? "
                "ORDER BY document_id",
                (tenant, knowledge_base, manager),
            ).fetchall()
        return tuple(str(row[0]) for row in rows)

    def delete_managed(
        self,
        tenant_id: str,
        kb_id: str,
        managed_by: str,
        document_ids: Iterable[str] | None = None,
    ) -> int:
        """Delete one connector's ACL checkpoints, optionally for exact docs."""

        tenant = str(tenant_id).strip()
        knowledge_base = str(kb_id).strip()
        manager = str(managed_by).strip()
        if not tenant or not knowledge_base or not manager:
            raise ValueError("ACL checkpoint scope must not be empty")
        documents = (
            None
            if document_ids is None
            else tuple(
                dict.fromkeys(
                    str(document_id).strip()
                    for document_id in document_ids
                    if str(document_id).strip()
                )
            )
        )
        if documents == ():
            return 0
        clauses = "tenant_id=? AND kb_id=? AND managed_by=?"
        parameters: list[object] = [tenant, knowledge_base, manager]
        if documents is not None:
            clauses += " AND document_id IN (" + ",".join("?" for _ in documents) + ")"
            parameters.extend(documents)
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM external_acl_sync_state WHERE " + clauses,
                tuple(parameters),
            )
        return int(cursor.rowcount)


class ExternalAclSynchronizer:
    _ROLES = {"read": Role.VIEWER, "review": Role.REVIEWER, "write": Role.EDITOR}
    _ROLE_RANK = {Role.VIEWER: 0, Role.REVIEWER: 1, Role.EDITOR: 2}

    def __init__(
        self,
        access_store: ResourceAccessStore,
        identity_resolver: IdentityResolver,
        state_store: ExternalAclSyncStore,
    ) -> None:
        self.access_store = access_store
        self.identity_resolver = identity_resolver
        self.state_store = state_store

    def apply(
        self,
        *,
        tenant_id: str,
        kb_id: str,
        document_id: str,
        source: str,
        owner_id: str,
        managed_by: str,
        snapshot: ExternalAclSnapshot,
        owner_membership_id: str | None = None,
    ) -> dict[str, Any]:
        original_grant_count = len(snapshot.grants)
        resolved: list[tuple[str, Role, str | None]] = []
        unresolved = 0
        if snapshot.complete:
            try:
                resolved_by_subject: dict[str, tuple[Role, str | None]] = {}
                for grant in snapshot.grants:
                    identity = self.identity_resolver.resolve(tenant_id, grant)
                    if identity is None:
                        unresolved += 1
                        continue
                    if not isinstance(identity, tuple) or len(identity) != 2:
                        raise ValueError("resolved identity is invalid")
                    subject_id, membership_id = identity
                    self._validate_resolved_identity(subject_id, "subject_id")
                    if membership_id is not None:
                        self._validate_resolved_identity(membership_id, "membership_id")
                    role = self._ROLES[grant.permission]
                    existing = resolved_by_subject.get(subject_id)
                    if existing is None:
                        resolved_by_subject[subject_id] = (role, membership_id)
                        continue
                    if existing[1] != membership_id:
                        raise ValueError("resolved identity membership is ambiguous")
                    if self._ROLE_RANK[role] < self._ROLE_RANK[existing[0]]:
                        resolved_by_subject[subject_id] = (role, membership_id)
                resolved = [
                    (subject_id, role, membership_id)
                    for subject_id, (role, membership_id) in resolved_by_subject.items()
                ]
            except Exception:
                # Identity backend failures revoke the provider-managed view;
                # they never preserve a potentially stale allowlist.
                snapshot = ExternalAclSnapshot(
                    complete=False, provider_version=snapshot.provider_version
                )
                resolved = []
                unresolved = original_grant_count
        else:
            unresolved = len(snapshot.grants)
        # Compute and persist visibility only after identity resolution.  A
        # resolver outage must not leave a previously selected workspace policy
        # in place, even when the provider snapshot claimed broad visibility.
        policy = (
            AccessPolicy.WORKSPACE
            if snapshot.complete and snapshot.workspace_visible
            else AccessPolicy.PRIVATE
        )
        grant_result = self.access_store.apply_managed_document_access(
            tenant_id,
            kb_id,
            document_id,
            source,
            owner_id,
            policy,
            managed_by,
            resolved if snapshot.complete else [],
            owner_membership_id=owner_membership_id,
        )
        revoked_ignored = int(grant_result.get("revoked_ignored", 0))
        if revoked_ignored:
            unresolved += revoked_ignored
        resolved_count = max(0, len(resolved) - revoked_ignored)
        status = "current" if snapshot.complete else "quarantined"
        state = self.state_store.record(
            tenant_id=tenant_id,
            kb_id=kb_id,
            document_id=document_id,
            managed_by=managed_by,
            status=status,
            snapshot=snapshot,
            resolved_count=resolved_count,
            unresolved_count=unresolved,
        )
        return {**state, **grant_result, "policy": policy.value}

    @staticmethod
    def _validate_resolved_identity(value: object, field: str) -> None:
        if (
            not isinstance(value, str)
            or not value
            or value != value.strip()
            or len(value) > 160
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
        ):
            raise ValueError(f"resolved {field} is invalid")
