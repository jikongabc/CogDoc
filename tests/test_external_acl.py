from cogdoc.api.resource_access import AccessMode, ResourceAccessStore
from cogdoc.api.tenancy import Principal, Role
from cogdoc.service.external_acl import (
    ExternalAclSnapshot,
    ExternalAclSynchronizer,
    ExternalAclSyncStore,
    ExternalGrant,
)


class Resolver:
    def __init__(self, mapping, *, fail=False):
        self.mapping = mapping
        self.fail = fail

    def resolve(self, tenant_id, grant):
        if self.fail:
            raise RuntimeError("identity backend unavailable")
        return self.mapping.get(grant.external_subject)


def _principal(subject):
    return Principal(
        tenant_id="tenant",
        subject_id=subject,
        role=Role.VIEWER,
        key_fingerprint=f"key-{subject}",
    )


def _setup(tmp_path, resolver):
    db = str(tmp_path / "state.db")
    access = ResourceAccessStore(db)
    access.set_kb_policy("tenant", "kb", "owner", "private")
    state = ExternalAclSyncStore(db)
    sync = ExternalAclSynchronizer(access, resolver, state)
    return access, state, sync


def test_external_acl_maps_known_users_and_ignores_unknown_fail_closed(tmp_path):
    access, state, sync = _setup(
        tmp_path, Resolver({"alice@example.com": ("alice", None)})
    )
    result = sync.apply(
        tenant_id="tenant",
        kb_id="kb",
        document_id="doc",
        source="private.pdf",
        owner_id="owner",
        managed_by="connector:c1",
        snapshot=ExternalAclSnapshot(
            grants=(
                ExternalGrant("alice@example.com", "read"),
                ExternalGrant("missing@example.com", "read"),
            ),
            complete=True,
            provider_version="v1",
        ),
    )
    assert result["status"] == "current" and result["unresolved_count"] == 1
    assert access.authorize_query(_principal("alice"), "kb").mode is AccessMode.SUBSET
    assert access.authorize_query(_principal("missing"), "kb").mode is AccessMode.DENY
    state.close()
    access.close()


def test_acl_failure_quarantines_and_revokes_previous_managed_grants(tmp_path):
    resolver = Resolver({"alice@example.com": ("alice", None)})
    access, state, sync = _setup(tmp_path, resolver)
    scope = dict(
        tenant_id="tenant",
        kb_id="kb",
        document_id="doc",
        source="private.pdf",
        owner_id="owner",
        managed_by="connector:c1",
    )
    sync.apply(
        **scope,
        snapshot=ExternalAclSnapshot(
            grants=(ExternalGrant("alice@example.com"),), complete=True
        ),
    )
    quarantined = sync.apply(**scope, snapshot=ExternalAclSnapshot(complete=False))
    assert quarantined["status"] == "quarantined" and quarantined["removed"] == 1
    assert access.authorize_query(_principal("alice"), "kb").mode is AccessMode.DENY
    state.close()
    access.close()


def test_manual_grant_is_preserved_across_provider_revocation(tmp_path):
    access, state, sync = _setup(
        tmp_path, Resolver({"alice@example.com": ("alice", None)})
    )
    access.set_document_policy("tenant", "kb", "doc", "private.pdf", "owner", "private")
    access.grant_subject("tenant", "kb", "alice", "viewer", document_id="doc")
    result = sync.apply(
        tenant_id="tenant",
        kb_id="kb",
        document_id="doc",
        source="private.pdf",
        owner_id="owner",
        managed_by="connector:c1",
        snapshot=ExternalAclSnapshot(complete=True),
    )
    assert result["manual_preserved"] == 0
    assert access.authorize_query(_principal("alice"), "kb").mode is AccessMode.SUBSET
    state.close()
    access.close()


def test_manual_grant_takes_ownership_from_provider_and_survives_resync(tmp_path):
    access, state, sync = _setup(
        tmp_path, Resolver({"alice@example.com": ("alice", None)})
    )
    scope = dict(
        tenant_id="tenant",
        kb_id="kb",
        document_id="doc",
        source="private.pdf",
        owner_id="owner",
        managed_by="connector:c1",
    )
    provider_grant = ExternalAclSnapshot(
        grants=(ExternalGrant("alice@example.com", "read"),), complete=True
    )
    sync.apply(**scope, snapshot=provider_grant)

    # An explicit local grant clears managed_by and becomes authoritative.
    access.grant_subject("tenant", "kb", "alice", "editor", document_id="doc")
    refreshed = sync.apply(**scope, snapshot=provider_grant)
    revoked = sync.apply(**scope, snapshot=ExternalAclSnapshot(complete=True))

    assert refreshed["manual_preserved"] == 1
    assert revoked["removed"] == 0
    assert access.authorize_query(_principal("alice"), "kb").mode is AccessMode.SUBSET
    state.close()
    access.close()


def test_identity_backend_failure_removes_stale_allowlist(tmp_path):
    resolver = Resolver({"alice@example.com": ("alice", None)})
    access, state, sync = _setup(tmp_path, resolver)
    scope = dict(
        tenant_id="tenant",
        kb_id="kb",
        document_id="doc",
        source="private.pdf",
        owner_id="owner",
        managed_by="connector:c1",
    )
    snapshot = ExternalAclSnapshot(
        grants=(ExternalGrant("alice@example.com"),), complete=True
    )
    sync.apply(**scope, snapshot=snapshot)
    resolver.fail = True
    result = sync.apply(**scope, snapshot=snapshot)
    assert result["status"] == "quarantined"
    assert access.authorize_query(_principal("alice"), "kb").mode is AccessMode.DENY
    state.close()
    access.close()


def test_identity_failure_cannot_leave_workspace_visibility_enabled(tmp_path):
    resolver = Resolver({}, fail=True)
    access, state, sync = _setup(tmp_path, resolver)
    result = sync.apply(
        tenant_id="tenant",
        kb_id="kb",
        document_id="doc",
        source="private.pdf",
        owner_id="owner",
        managed_by="connector:c1",
        snapshot=ExternalAclSnapshot(
            grants=(ExternalGrant("alice@example.com"),),
            workspace_visible=True,
            complete=True,
        ),
    )
    assert result["status"] == "quarantined"
    assert result["policy"] == "private"
    assert result["unresolved_count"] == 1
    assert access.authorize_query(_principal("alice"), "kb").mode is AccessMode.DENY
    state.close()
    access.close()
