import sqlite3

import pytest

from cogdoc.api.resource_access import (
    AccessMode,
    ResourceAccessConflictError,
    ResourceAccessStore,
)
from cogdoc.api.tenancy import Permission, Principal, Role
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


@pytest.mark.parametrize(
    ("first_permission", "second_permission"),
    [("write", "read"), ("read", "write")],
)
def test_resolved_aliases_merge_to_least_privilege_independent_of_order(
    tmp_path, first_permission, second_permission
):
    resolver = Resolver(
        {
            "alice-primary@example.com": ("alice", "membership-a"),
            "alice-alias@example.com": ("alice", "membership-a"),
        }
    )
    access, state, sync = _setup(tmp_path, resolver)

    result = sync.apply(
        tenant_id="tenant",
        kb_id="kb",
        document_id="doc",
        source="private.pdf",
        owner_id="owner",
        managed_by="connector:c1",
        snapshot=ExternalAclSnapshot(
            grants=(
                ExternalGrant("alice-primary@example.com", first_permission),
                ExternalGrant("alice-alias@example.com", second_permission),
            ),
            complete=True,
        ),
    )

    assert result["status"] == "current"
    assert result["resolved_count"] == 1
    grants = access.list_grants("tenant", "kb", document_id="doc")
    assert [(grant["subject_id"], grant["role"]) for grant in grants] == [
        ("alice", "viewer")
    ]
    state.close()
    access.close()


def test_resolved_alias_membership_conflict_quarantines_and_revokes_old_grant(
    tmp_path,
):
    resolver = Resolver({"alice-primary@example.com": ("alice", "membership-a")})
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
            grants=(ExternalGrant("alice-primary@example.com"),),
            complete=True,
        ),
    )
    assert access.list_grants("tenant", "kb", document_id="doc")

    resolver.mapping["alice-alias@example.com"] = ("alice", "membership-b")
    result = sync.apply(
        **scope,
        snapshot=ExternalAclSnapshot(
            grants=(
                ExternalGrant("alice-primary@example.com", "write"),
                ExternalGrant("alice-alias@example.com", "read"),
            ),
            workspace_visible=True,
            complete=True,
        ),
    )

    assert result["status"] == "quarantined"
    assert result["policy"] == "private"
    assert result["removed"] == 1
    assert result["unresolved_count"] == 2
    assert access.list_grants("tenant", "kb", document_id="doc") == []
    assert access.get_document_policy("tenant", "kb", "doc")["policy"] == "private"
    state.close()
    access.close()


def test_revoked_resolved_identity_does_not_rollback_other_upstream_revocations(
    tmp_path,
):
    resolver = Resolver(
        {
            "alice@example.com": ("alice", "membership-a"),
            "bob@example.com": ("bob", "membership-b"),
        }
    )
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
            grants=(
                ExternalGrant("alice@example.com", "read"),
                ExternalGrant("bob@example.com", "read"),
            ),
            complete=True,
            provider_version="v1",
        ),
    )
    access.revoke_all_subject_grants("tenant", "alice", membership_id="membership-a")

    result = sync.apply(
        **scope,
        snapshot=ExternalAclSnapshot(
            grants=(ExternalGrant("alice@example.com", "read"),),
            complete=True,
            provider_version="v2",
        ),
    )
    assert result["resolved_count"] == 0
    assert result["unresolved_count"] == 1
    assert result["removed"] == 1
    assert access.list_grants("tenant", "kb", document_id="doc") == []
    assert access.authorize_query(_principal("bob"), "kb").mode is AccessMode.DENY
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


def test_acl_checkpoint_failure_cannot_leave_stale_managed_access(
    tmp_path, monkeypatch
):
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

    def fail_record(**_kwargs):
        raise RuntimeError("checkpoint unavailable")

    monkeypatch.setattr(state, "record", fail_record)
    try:
        sync.apply(**scope, snapshot=ExternalAclSnapshot(complete=False))
    except RuntimeError as exc:
        assert str(exc) == "checkpoint unavailable"
    else:  # pragma: no cover - defensive assertion
        raise AssertionError("checkpoint failure was not propagated")

    # Policy and provider-managed grants advance in the same access-store
    # transaction, before the non-authoritative checkpoint is attempted.
    assert access.get_document_policy("tenant", "kb", "doc")["policy"] == "private"
    assert access.authorize_query(_principal("alice"), "kb").mode is AccessMode.DENY
    access.close()
    state.close()


def test_external_acl_mapping_rejects_ambiguous_or_unbounded_payloads():
    with pytest.raises(ValueError, match="flags must be booleans"):
        ExternalAclSnapshot.from_mapping({"complete": "yes", "grants": []})
    with pytest.raises(ValueError, match="must contain objects"):
        ExternalAclSnapshot.from_mapping({"complete": True, "grants": ["alice"]})
    with pytest.raises(ValueError, match="grant count exceeds"):
        ExternalAclSnapshot.from_mapping({"complete": True, "grants": [{}] * 4_097})
    with pytest.raises(ValueError, match="exceeds the byte limit"):
        ExternalAclSnapshot.from_mapping(
            {"complete": True, "grants": [], "padding": "x" * (256 * 1024)}
        )


def test_external_acl_state_can_delete_one_connectors_exact_documents(tmp_path):
    access, state, sync = _setup(tmp_path, Resolver({}))
    base = dict(
        tenant_id="tenant",
        kb_id="kb",
        owner_id="owner",
        snapshot=ExternalAclSnapshot(complete=True),
    )
    sync.apply(document_id="one", source="one.pdf", managed_by="connector:c1", **base)
    sync.apply(document_id="two", source="two.pdf", managed_by="connector:c1", **base)
    sync.apply(
        document_id="other", source="other.pdf", managed_by="connector:c2", **base
    )

    assert state.managed_document_ids("tenant", "kb", "connector:c1") == ("one", "two")
    assert (
        state.delete_managed("tenant", "kb", "connector:c1", document_ids=("one",)) == 1
    )
    assert state.get("tenant", "kb", "one", "connector:c1") is None
    assert state.get("tenant", "kb", "two", "connector:c1") is not None
    assert state.delete_managed("tenant", "kb", "connector:c1") == 1
    assert state.get("tenant", "kb", "other", "connector:c2") is not None
    access.close()
    state.close()


def test_document_quarantine_revokes_workspace_and_manual_access_atomically(tmp_path):
    access, state, _sync = _setup(tmp_path, Resolver({}))
    access.set_document_policy(
        "tenant", "kb", "doc", "retiring.pdf", "owner", "workspace"
    )
    access.grant_subject("tenant", "kb", "alice", "viewer", document_id="doc")
    epoch = access.acl_epoch("tenant", "kb")

    assert access.quarantine_document_access("tenant", "kb", "doc") is True
    assert access.get_document_policy("tenant", "kb", "doc")["policy"] == "private"
    assert access.acl_epoch("tenant", "kb") == epoch + 1
    assert access.authorize_query(_principal("alice"), "kb").mode is AccessMode.DENY
    assert access.quarantine_document_access("tenant", "kb", "missing") is False
    access.close()
    state.close()


def test_document_quarantine_batch_has_one_epoch_and_rolls_back_as_a_unit(tmp_path):
    access, state, _sync = _setup(tmp_path, Resolver({}))
    for document in ("one", "two"):
        access.set_document_policy(
            "tenant", "kb", document, f"{document}.pdf", "owner", "workspace"
        )
        access.grant_subject("tenant", "kb", "alice", "viewer", document_id=document)
    epoch = access.acl_epoch("tenant", "kb")

    assert (
        access.quarantine_documents_access("tenant", "kb", ("one", "two", "one")) == 2
    )
    assert access.acl_epoch("tenant", "kb") == epoch + 1
    assert all(
        access.get_document_policy("tenant", "kb", document)["policy"] == "private"
        for document in ("one", "two")
    )

    for document in ("one", "two"):
        access.set_document_policy(
            "tenant", "kb", document, f"{document}.pdf", "owner", "workspace"
        )
        access.grant_subject("tenant", "kb", "alice", "viewer", document_id=document)
    access._conn.execute(
        "CREATE TRIGGER fail_second_quarantine BEFORE UPDATE OF policy "
        "ON resource_access_document_policies WHEN NEW.document_id='two' "
        "BEGIN SELECT RAISE(ABORT,'injected quarantine failure'); END"
    )
    with pytest.raises(sqlite3.IntegrityError, match="injected quarantine failure"):
        access.quarantine_documents_access("tenant", "kb", ("one", "two"))
    assert all(
        access.get_document_policy("tenant", "kb", document)["policy"] == "workspace"
        for document in ("one", "two")
    )
    assert access.authorize_query(_principal("alice"), "kb").mode is AccessMode.SUBSET
    access.close()
    state.close()


def test_retirement_fence_blocks_reauthorization_until_index_cleanup_finishes(
    tmp_path,
):
    access, state, _sync = _setup(tmp_path, Resolver({}))
    access.set_kb_policy("tenant", "kb", "owner", "workspace")
    for document in ("one", "two"):
        access.set_document_policy(
            "tenant", "kb", document, f"{document}.pdf", "owner", "workspace"
        )
    assert (
        access.begin_document_retirement("tenant", "kb", "connector:c1", ("one",)) == 1
    )
    assert access.retiring_document_ids("tenant", "kb", "connector:c1") == ("one",)

    decision = access.authorize_query(_principal("alice"), "kb")
    assert decision.mode is AccessMode.SUBSET
    assert decision.allowed_document_ids == ("two",)
    for principal in (
        Principal("tenant", "tenant-owner", Role.OWNER, "key-tenant-owner"),
        Principal("tenant", "tenant-admin", Role.ADMIN, "key-tenant-admin"),
        _principal("owner"),
    ):
        privileged = access.authorize_query(principal, "kb")
        assert privileged.mode is AccessMode.SUBSET
        assert privileged.allowed_document_ids == ("two",)
    with pytest.raises(ResourceAccessConflictError, match="retiring"):
        access.set_document_policy(
            "tenant", "kb", "one", "one.pdf", "owner", "workspace"
        )
    with pytest.raises(ResourceAccessConflictError, match="retiring"):
        access.grant_subject("tenant", "kb", "alice", "viewer", document_id="one")
    with pytest.raises(ResourceAccessConflictError, match="retiring"):
        access.apply_managed_document_access(
            "tenant",
            "kb",
            "one",
            "one.pdf",
            "owner",
            "workspace",
            "connector:c1",
            (),
        )

    assert (
        access.finish_document_retirement("tenant", "kb", "connector:c1", ("one",)) == 1
    )
    assert access.get_document_policy("tenant", "kb", "one") is None
    assert access.retiring_document_ids("tenant", "kb", "connector:c1") == ()
    access.set_document_policy("tenant", "kb", "one", "one.pdf", "owner", "private")
    access.close()
    state.close()


def test_only_document_retirement_preserves_privileged_cleanup_authority(tmp_path):
    access, state, _sync = _setup(tmp_path, Resolver({}))
    access.set_kb_policy("tenant", "kb", "owner", "workspace")
    access.set_document_policy("tenant", "kb", "only", "only.pdf", "owner", "workspace")
    access.begin_document_retirement("tenant", "kb", "connector:c1", ("only",))

    for principal in (
        Principal("tenant", "tenant-owner", Role.OWNER, "key-tenant-owner"),
        Principal("tenant", "tenant-admin", Role.ADMIN, "key-tenant-admin"),
        Principal("tenant", "owner", Role.ADMIN, "key-kb-owner"),
    ):
        assert access.authorize_query(principal, "kb").mode is AccessMode.DENY
        manage = access.authorize_query(
            principal,
            "kb",
            permission=Permission.MANAGE_ACCESS,
        )
        assert manage.mode is AccessMode.ALL

    assert (
        access.finish_document_retirement("tenant", "kb", "connector:c1", ("only",))
        == 1
    )
    access.close()
    state.close()
