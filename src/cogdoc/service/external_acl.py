from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from threading import RLock
from typing import Any, Mapping, Protocol

from cogdoc.api.persistence import connect_sqlite
from cogdoc.api.resource_access import AccessPolicy, ResourceAccessStore
from cogdoc.api.tenancy import Role


@dataclass(frozen=True)
class ExternalGrant:
    external_subject: str
    permission: str = "read"
    subject_type: str = "user"

    def __post_init__(self) -> None:
        subject = str(self.external_subject or "").strip().casefold()
        if not subject or len(subject) > 320:
            raise ValueError("external_subject is invalid")
        if self.permission not in {"read", "review", "write"}:
            raise ValueError("external permission is unsupported")
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
        keys = [(grant.subject_type, grant.external_subject) for grant in self.grants]
        if len(keys) != len(set(keys)):
            raise ValueError("external ACL contains duplicate subjects")

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any] | None) -> ExternalAclSnapshot:
        if payload is None:
            return cls(complete=False)
        raw_grants = payload.get("grants") or []
        if not isinstance(raw_grants, list):
            raise ValueError("external ACL grants must be a list")
        return cls(
            grants=tuple(
                ExternalGrant(
                    str(row.get("external_subject") or row.get("email") or ""),
                    str(row.get("permission") or "read"),
                    str(row.get("subject_type") or "user"),
                )
                for row in raw_grants
                if isinstance(row, Mapping)
            ),
            workspace_visible=payload.get("workspace_visible") is True,
            complete=payload.get("complete") is True,
            provider_version=(
                str(payload.get("provider_version"))
                if payload.get("provider_version") is not None
                else None
            ),
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


class ExternalAclSynchronizer:
    _ROLES = {"read": Role.VIEWER, "review": Role.REVIEWER, "write": Role.EDITOR}

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
                for grant in snapshot.grants:
                    identity = self.identity_resolver.resolve(tenant_id, grant)
                    if identity is None:
                        unresolved += 1
                        continue
                    resolved.append(
                        (identity[0], self._ROLES[grant.permission], identity[1])
                    )
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
        self.access_store.set_document_policy(
            tenant_id,
            kb_id,
            document_id,
            source,
            owner_id,
            policy,
            owner_membership_id=owner_membership_id,
        )
        grant_result = self.access_store.replace_managed_document_grants(
            tenant_id,
            kb_id,
            document_id,
            managed_by,
            resolved if snapshot.complete else [],
        )
        status = "current" if snapshot.complete else "quarantined"
        state = self.state_store.record(
            tenant_id=tenant_id,
            kb_id=kb_id,
            document_id=document_id,
            managed_by=managed_by,
            status=status,
            snapshot=snapshot,
            resolved_count=len(resolved),
            unresolved_count=unresolved,
        )
        return {**state, **grant_result, "policy": policy.value}
