from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import sqlite3
from threading import Event

import pytest

from cogdoc.api.resource_access import (
    AccessMode,
    AccessPolicy,
    QueryAuthorization,
    ResourceAccessConflictError,
    ResourceAccessNotFoundError,
    ResourceAccessStore,
)
from cogdoc.api.tenancy import Permission, Principal, Role


def _principal(
    tenant_id: str = "tenant-a",
    subject_id: str = "alice",
    role: Role = Role.VIEWER,
    *,
    membership_id: str | None = None,
    session: bool = False,
) -> Principal:
    return Principal(
        tenant_id=tenant_id,
        subject_id=subject_id,
        role=role,
        key_fingerprint=(
            f"session:{tenant_id}-{subject_id}"
            if session
            else f"fingerprint-{tenant_id}-{subject_id}"
        ),
        membership_id=membership_id,
    )


def _store(tmp_path, name: str = "access.db", **kwargs) -> ResourceAccessStore:
    return ResourceAccessStore(tmp_path / name, **kwargs)


def test_missing_policy_is_fail_closed_and_legacy_default_is_explicit(tmp_path):
    principal = _principal()
    strict = _store(tmp_path, "strict.db")
    decision = strict.authorize_query(principal, "kb")
    assert decision.mode is AccessMode.DENY
    assert decision.allowed_document_ids == ()
    assert decision.allowed_sources == ()
    assert decision.reason == "policy_missing"

    legacy = _store(tmp_path, "legacy.db", legacy_workspace_default=True)
    legacy_decision = legacy.authorize_query(principal, "kb")
    assert legacy_decision.mode is AccessMode.ALL
    assert legacy_decision.allowed_sources == ()


def test_workspace_private_and_inherited_document_policies_form_exact_subset(tmp_path):
    store = _store(tmp_path)
    store.set_kb_policy("tenant-a", "kb", "owner", AccessPolicy.WORKSPACE)
    store.set_document_policy(
        "tenant-a", "kb", "public", "public.pdf", policy=AccessPolicy.INHERIT
    )
    store.set_document_policy(
        "tenant-a", "kb", "secret", "secret.pdf", policy=AccessPolicy.PRIVATE
    )

    decision = store.allowed_sources(_principal(), "kb")
    assert decision.mode is AccessMode.SUBSET
    assert decision.allowed_document_ids == ("public",)
    assert decision.allowed_sources == ("public.pdf",)
    assert decision.allows_source("public.pdf")
    assert not decision.allows_source("secret.pdf")

    store.grant_subject("tenant-a", "kb", "alice", Role.VIEWER, document_id="secret")
    assert store.authorize_query(_principal(), "kb").mode is AccessMode.ALL


def test_private_kb_can_expose_only_explicit_workspace_or_granted_documents(tmp_path):
    store = _store(tmp_path)
    store.set_kb_policy("tenant-a", "kb", "owner", AccessPolicy.PRIVATE)
    store.set_document_policy(
        "tenant-a", "kb", "workspace", "team.pdf", policy=AccessPolicy.WORKSPACE
    )
    store.set_document_policy(
        "tenant-a", "kb", "inherited", "internal.pdf", policy=AccessPolicy.INHERIT
    )
    store.set_document_policy(
        "tenant-a", "kb", "private", "private.pdf", policy=AccessPolicy.PRIVATE
    )

    decision = store.authorize_query(_principal(), "kb")
    assert decision.mode is AccessMode.SUBSET
    assert decision.allowed_document_ids == ("workspace",)
    assert decision.allowed_sources == ("team.pdf",)

    store.grant_subject("tenant-a", "kb", "alice", Role.VIEWER)
    inherited = store.authorize_query(_principal(), "kb")
    assert inherited.mode is AccessMode.SUBSET
    assert inherited.allowed_document_ids == ("inherited", "workspace")

    store.grant_subject("tenant-a", "kb", "alice", Role.VIEWER, document_id="private")
    assert store.authorize_query(_principal(), "kb").mode is AccessMode.ALL


def test_grant_role_is_a_cap_intersected_with_principal_role(tmp_path):
    store = _store(tmp_path)
    store.set_kb_policy("tenant-a", "kb", "owner", AccessPolicy.PRIVATE)
    editor = _principal(role=Role.EDITOR)

    store.grant_subject("tenant-a", "kb", "alice", Role.VIEWER)
    assert store.authorize_query(editor, "kb").mode is AccessMode.ALL
    write_denied = store.authorize_query(editor, "kb", permission=Permission.WRITE)
    assert write_denied.mode is AccessMode.DENY

    store.grant_subject("tenant-a", "kb", "alice", Role.EDITOR)
    assert (
        store.authorize_query(editor, "kb", permission=Permission.WRITE).mode
        is AccessMode.ALL
    )

    viewer = _principal(role=Role.VIEWER)
    store.grant_subject("tenant-a", "kb", "alice", Role.ADMIN)
    assert (
        store.authorize_query(viewer, "kb", permission=Permission.WRITE).mode
        is AccessMode.DENY
    )


def test_owner_admin_bypass_visibility_but_not_tenant_role_permissions(tmp_path):
    store = _store(tmp_path)
    store.set_kb_policy("tenant-a", "kb", "resource-owner", AccessPolicy.PRIVATE)
    store.set_document_policy(
        "tenant-a",
        "kb",
        "doc",
        "secret.pdf",
        owner_id="document-owner",
        policy=AccessPolicy.PRIVATE,
    )

    assert (
        store.authorize_query(_principal(role=Role.ADMIN), "kb").mode is AccessMode.ALL
    )
    assert (
        store.authorize_query(
            _principal(role=Role.ADMIN), "kb", permission=Permission.MANAGE_TENANT
        ).mode
        is AccessMode.DENY
    )
    assert (
        store.authorize_query(_principal(subject_id="resource-owner"), "kb").mode
        is AccessMode.ALL
    )
    document_owner = store.authorize_query(
        _principal(subject_id="document-owner"), "kb"
    )
    assert document_owner.mode is AccessMode.SUBSET
    assert document_owner.allowed_document_ids == ("doc",)


def test_session_creator_bypass_is_bound_to_exact_membership_incarnation(tmp_path):
    store = _store(tmp_path)
    old_membership = "mem-alice-old"
    new_membership = "mem-alice-new"
    old_principal = _principal(
        subject_id="alice",
        role=Role.EDITOR,
        membership_id=old_membership,
        session=True,
    )
    new_principal = _principal(
        subject_id="alice",
        role=Role.EDITOR,
        membership_id=new_membership,
        session=True,
    )

    store.set_kb_policy(
        "tenant-a",
        "creator-kb",
        "alice",
        "private",
        owner_membership_id=old_membership,
    )
    store.set_document_policy(
        "tenant-a",
        "creator-kb",
        "creator-doc",
        "creator.pdf",
        owner_id="alice",
        policy="private",
        owner_membership_id=old_membership,
    )
    store.set_kb_policy(
        "tenant-a",
        "document-kb",
        "workspace-owner",
        "private",
        owner_membership_id="mem-workspace-owner",
    )
    store.set_document_policy(
        "tenant-a",
        "document-kb",
        "owned-doc",
        "owned.pdf",
        owner_id="alice",
        policy="private",
        owner_membership_id=old_membership,
    )

    assert store.authorize_query(old_principal, "creator-kb").mode is AccessMode.ALL
    document_access = store.authorize_query(old_principal, "document-kb")
    assert document_access.mode is AccessMode.SUBSET
    assert document_access.allowed_document_ids == ("owned-doc",)

    revoked_epochs = store.revoke_all_subject_grants(
        "tenant-a", "alice", membership_id=old_membership
    )
    assert set(revoked_epochs) == {"creator-kb", "document-kb"}
    assert (
        store.revoke_all_subject_grants(
            "tenant-a", "alice", membership_id=old_membership
        )
        == {}
    )
    assert store.authorize_query(old_principal, "creator-kb").reason == (
        "membership_revoked"
    )
    assert store.authorize_query(new_principal, "creator-kb").mode is AccessMode.DENY
    assert store.authorize_query(new_principal, "document-kb").mode is AccessMode.DENY


def test_legacy_null_owner_membership_is_fail_closed_for_sessions(tmp_path):
    store = _store(tmp_path)
    store.set_kb_policy("tenant-a", "kb", "alice", "private")
    session_principal = _principal(
        membership_id="mem-alice-current",
        session=True,
    )

    assert store.authorize_query(session_principal, "kb").mode is AccessMode.DENY
    # Local/static-key principals have no membership lifecycle and preserve the
    # source-compatible owner behavior for legacy deployments.
    assert store.authorize_query(_principal(), "kb").mode is AccessMode.ALL


def test_existing_acl_database_migrates_owner_membership_columns_fail_closed(tmp_path):
    path = tmp_path / "legacy-access.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE resource_access_kb_policies (
            tenant_id TEXT NOT NULL,
            kb_id TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            policy TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (tenant_id, kb_id)
        );
        CREATE TABLE resource_access_document_policies (
            tenant_id TEXT NOT NULL,
            kb_id TEXT NOT NULL,
            document_id TEXT NOT NULL,
            source TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            policy TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (tenant_id, kb_id, document_id),
            UNIQUE (tenant_id, kb_id, source)
        );
        INSERT INTO resource_access_kb_policies VALUES
            ('tenant-a', 'kb', 'alice', 'private', 'old', 'old');
        INSERT INTO resource_access_document_policies VALUES
            ('tenant-a', 'kb', 'doc', 'old.pdf', 'alice', 'private', 'old', 'old');
        """
    )
    connection.close()

    store = ResourceAccessStore(path)
    kb_policy = store.get_kb_policy("tenant-a", "kb")
    document_policy = store.get_document_policy("tenant-a", "kb", "doc")
    assert kb_policy is not None and kb_policy["owner_membership_id"] is None
    assert document_policy is not None
    assert document_policy["owner_membership_id"] is None
    migrated_session = _principal(
        membership_id="mem-alice-current",
        session=True,
    )
    assert store.authorize_query(migrated_session, "kb").mode is AccessMode.DENY


def test_tenant_is_part_of_every_identity_and_cross_tenant_query_denies(tmp_path):
    store = _store(tmp_path)
    store.set_kb_policy("tenant-a", "same-slug", "alice", AccessPolicy.WORKSPACE)
    store.set_kb_policy("tenant-b", "same-slug", "bob", AccessPolicy.PRIVATE)

    alice = _principal("tenant-a", "alice")
    bob = _principal("tenant-b", "bob")
    assert store.authorize_query(alice, "same-slug").mode is AccessMode.ALL
    assert store.authorize_query(bob, "same-slug").mode is AccessMode.ALL
    mismatch = store.authorize_query(alice, "same-slug", tenant_id="tenant-b")
    assert mismatch.mode is AccessMode.DENY
    assert mismatch.reason == "tenant_mismatch"
    assert store.list_kb_policies("tenant-a")[0]["owner_id"] == "alice"
    assert store.list_kb_policies("tenant-b")[0]["owner_id"] == "bob"


def test_acl_epoch_changes_atomically_only_when_state_changes(tmp_path):
    store = _store(tmp_path)
    assert store.acl_epoch("tenant-a", "kb") == 0
    assert store.set_kb_policy("tenant-a", "kb", "owner", "private")["acl_epoch"] == 1
    assert store.set_kb_policy("tenant-a", "kb", "owner", "private")["acl_epoch"] == 1
    store.set_document_policy("tenant-a", "kb", "d1", "one.pdf")
    assert store.acl_epoch("tenant-a", "kb") == 2
    store.grant_subject("tenant-a", "kb", "alice", Role.VIEWER)
    assert store.acl_epoch("tenant-a", "kb") == 3
    assert store.grant_subject("tenant-a", "kb", "alice", Role.VIEWER)["acl_epoch"] == 3
    assert store.revoke_subject("tenant-a", "kb", "alice")
    assert store.acl_epoch("tenant-a", "kb") == 4
    assert not store.revoke_subject("tenant-a", "kb", "alice")
    assert store.delete_document_policy("tenant-a", "kb", "d1")
    assert store.acl_epoch("tenant-a", "kb") == 5
    assert store.clear_kb("tenant-a", "kb")
    assert store.acl_epoch("tenant-a", "kb") == 6
    assert not store.clear_kb("tenant-a", "kb")
    assert store.is_epoch_current("tenant-a", "kb", 6)


def test_revoke_all_subject_grants_is_tenant_scoped_and_bumps_each_kb_once(tmp_path):
    store = _store(tmp_path)
    for tenant_id in ("tenant-a", "tenant-b"):
        for kb_id in ("kb-a", "kb-b"):
            store.set_kb_policy(tenant_id, kb_id, "owner", "private")
            store.set_document_policy(
                tenant_id,
                kb_id,
                "doc",
                f"{tenant_id}-{kb_id}.pdf",
                policy="private",
            )
    store.grant_subject("tenant-a", "kb-a", "alice", Role.VIEWER)
    store.grant_subject("tenant-a", "kb-a", "alice", Role.VIEWER, document_id="doc")
    store.grant_subject("tenant-a", "kb-b", "alice", Role.VIEWER, document_id="doc")
    store.grant_subject("tenant-a", "kb-a", "bob", Role.VIEWER)
    store.grant_subject("tenant-b", "kb-a", "alice", Role.VIEWER)
    before = {kb_id: store.acl_epoch("tenant-a", kb_id) for kb_id in ("kb-a", "kb-b")}
    tenant_b_epoch = store.acl_epoch("tenant-b", "kb-a")

    result = store.revoke_all_subject_grants(
        "tenant-a", "alice", membership_id="mem-alice-old"
    )

    assert result == {kb_id: before[kb_id] + 1 for kb_id in ("kb-a", "kb-b")}
    assert store.is_membership_revoked("tenant-a", "alice", "mem-alice-old")
    assert store.list_grants("tenant-a", "kb-a", subject_id="alice") == []
    assert store.list_grants("tenant-a", "kb-b", subject_id="alice") == []
    assert len(store.list_grants("tenant-a", "kb-a", subject_id="bob")) == 1
    assert len(store.list_grants("tenant-b", "kb-a", subject_id="alice")) == 1
    assert store.acl_epoch("tenant-b", "kb-a") == tenant_b_epoch

    assert (
        store.revoke_all_subject_grants(
            "tenant-a", "alice", membership_id="mem-alice-old"
        )
        == {}
    )
    assert {
        kb_id: store.acl_epoch("tenant-a", kb_id) for kb_id in ("kb-a", "kb-b")
    } == result


def test_revoke_all_subject_grants_rolls_back_delete_and_epochs_on_failure(
    tmp_path, monkeypatch
):
    store = _store(tmp_path)
    for kb_id in ("kb-a", "kb-b"):
        store.set_kb_policy("tenant-a", kb_id, "owner", "private")
        store.grant_subject("tenant-a", kb_id, "alice", Role.VIEWER)
    epochs = {kb_id: store.acl_epoch("tenant-a", kb_id) for kb_id in ("kb-a", "kb-b")}
    original_bump = store._bump_epoch_locked

    def fail_on_second_kb(tenant_id: str, kb_id: str) -> int:
        if kb_id == "kb-b":
            raise RuntimeError("simulated epoch failure")
        return original_bump(tenant_id, kb_id)

    monkeypatch.setattr(store, "_bump_epoch_locked", fail_on_second_kb)
    with pytest.raises(RuntimeError, match="simulated epoch failure"):
        store.revoke_all_subject_grants(
            "tenant-a", "alice", membership_id="mem-alice-old"
        )

    assert not store.is_membership_revoked("tenant-a", "alice", "mem-alice-old")
    assert {
        kb_id: store.acl_epoch("tenant-a", kb_id) for kb_id in ("kb-a", "kb-b")
    } == epochs
    assert all(
        len(store.list_grants("tenant-a", kb_id, subject_id="alice")) == 1
        for kb_id in ("kb-a", "kb-b")
    )


def test_revoked_membership_tombstone_blocks_a_delayed_grant_after_reinvite(tmp_path):
    store = _store(tmp_path)
    store.set_kb_policy("tenant-a", "kb", "owner", "private")
    old_membership_id = "mem-alice-old"
    new_membership_id = "mem-alice-new"
    store.grant_subject(
        "tenant-a",
        "kb",
        "alice",
        Role.VIEWER,
        membership_id=old_membership_id,
    )
    membership_checked = Event()
    continue_old_request = Event()

    def delayed_old_grant() -> dict:
        # This models routes/access having already observed the old membership
        # before the concurrent member-removal request reaches the ACL store.
        membership_checked.set()
        assert continue_old_request.wait(timeout=5)
        return store.grant_subject(
            "tenant-a",
            "kb",
            "alice",
            Role.ADMIN,
            membership_id=old_membership_id,
        )

    with ThreadPoolExecutor(max_workers=1) as executor:
        delayed = executor.submit(delayed_old_grant)
        assert membership_checked.wait(timeout=5)
        store.revoke_all_subject_grants(
            "tenant-a", "alice", membership_id=old_membership_id
        )
        with pytest.raises(
            ResourceAccessConflictError, match="membership incarnation is required"
        ):
            store.grant_subject("tenant-a", "kb", "alice", Role.VIEWER)
        # Re-invitation creates a new membership ID. A fresh grant is valid and
        # must not be overwritten by the delayed request from the old incarnation.
        store.grant_subject(
            "tenant-a",
            "kb",
            "alice",
            Role.VIEWER,
            membership_id=new_membership_id,
        )
        continue_old_request.set()
        with pytest.raises(ResourceAccessConflictError, match="membership incarnation"):
            delayed.result(timeout=5)

    grants = store.list_grants("tenant-a", "kb", subject_id="alice")
    assert len(grants) == 1
    assert grants[0]["role"] == Role.VIEWER.value


def test_revoked_membership_tombstone_blocks_delayed_owner_policy_writes(tmp_path):
    store = _store(tmp_path)
    old_membership_id = "mem-alice-old"
    new_membership_id = "mem-alice-new"
    store.set_kb_policy(
        "tenant-a",
        "parent-kb",
        "workspace-owner",
        "private",
        owner_membership_id="mem-workspace-owner",
    )
    membership_checked = Event()
    continue_old_requests = Event()

    def delayed_kb_policy() -> dict:
        membership_checked.set()
        assert continue_old_requests.wait(timeout=5)
        return store.set_kb_policy(
            "tenant-a",
            "late-kb",
            "alice",
            "private",
            owner_membership_id=old_membership_id,
        )

    def delayed_document_policy() -> dict:
        membership_checked.set()
        assert continue_old_requests.wait(timeout=5)
        return store.set_document_policy(
            "tenant-a",
            "parent-kb",
            "late-doc",
            "late.pdf",
            owner_id="alice",
            policy="private",
            owner_membership_id=old_membership_id,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        delayed_kb = executor.submit(delayed_kb_policy)
        delayed_document = executor.submit(delayed_document_policy)
        assert membership_checked.wait(timeout=5)
        store.revoke_all_subject_grants(
            "tenant-a", "alice", membership_id=old_membership_id
        )
        continue_old_requests.set()
        for delayed in (delayed_kb, delayed_document):
            with pytest.raises(
                ResourceAccessConflictError, match="membership incarnation"
            ):
                delayed.result(timeout=5)

    assert store.get_kb_policy("tenant-a", "late-kb") is None
    assert store.get_document_policy("tenant-a", "parent-kb", "late-doc") is None
    # A newly accepted membership is a distinct authority and may create fresh
    # resources without clearing the old incarnation's tombstone.
    created = store.set_kb_policy(
        "tenant-a",
        "fresh-kb",
        "alice",
        "private",
        owner_membership_id=new_membership_id,
    )
    assert created["owner_membership_id"] == new_membership_id


def test_policies_grants_and_epoch_survive_restart(tmp_path):
    path = tmp_path / "persistent.db"
    first = ResourceAccessStore(path)
    first.set_kb_policy("tenant-a", "kb", "owner", "private")
    first.set_document_policy("tenant-a", "kb", "doc", "report.pdf", policy="private")
    first.grant_subject("tenant-a", "kb", "alice", Role.VIEWER, document_id="doc")
    first.revoke_all_subject_grants(
        "tenant-a", "departed", membership_id="mem-departed-old"
    )
    epoch = first.acl_epoch("tenant-a", "kb")
    first.close()

    second = ResourceAccessStore(path)
    assert second.acl_epoch("tenant-a", "kb") == epoch
    assert second.get_kb_policy("tenant-a", "kb")["policy"] == "private"
    assert (
        second.get_document_by_source("tenant-a", "kb", "report.pdf")["document_id"]
        == "doc"
    )
    assert second.list_grants("tenant-a", "kb")[0]["subject_id"] == "alice"
    assert second.is_membership_revoked("tenant-a", "departed", "mem-departed-old")
    with pytest.raises(ResourceAccessConflictError, match="membership incarnation"):
        second.grant_subject(
            "tenant-a",
            "kb",
            "departed",
            Role.VIEWER,
            membership_id="mem-departed-old",
        )
    decision = second.authorize_query(_principal(), "kb")
    assert decision.mode is AccessMode.SUBSET
    assert decision.allowed_sources == ("report.pdf",)


def test_document_source_collision_rolls_back_without_epoch_change(tmp_path):
    store = _store(tmp_path)
    store.set_kb_policy("tenant-a", "kb", "owner", "workspace")
    store.set_document_policy("tenant-a", "kb", "one", "same.pdf")
    epoch = store.acl_epoch("tenant-a", "kb")

    with pytest.raises(ResourceAccessConflictError):
        store.set_document_policy("tenant-a", "kb", "two", "same.pdf")

    assert store.acl_epoch("tenant-a", "kb") == epoch
    assert store.get_document_policy("tenant-a", "kb", "two") is None


def test_parent_existence_and_strict_input_validation(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(ResourceAccessNotFoundError):
        store.set_document_policy("tenant-a", "missing", "doc", "doc.pdf")
    with pytest.raises(ResourceAccessNotFoundError):
        store.grant_subject("tenant-a", "missing", "alice", Role.VIEWER)
    with pytest.raises((TypeError, ValueError)):
        store.set_kb_policy(" tenant-a", "kb", "owner", "workspace")
    with pytest.raises(ValueError):
        store.set_kb_policy("tenant-a", "kb", "owner", "inherit")
    with pytest.raises(ValueError):
        store.set_kb_policy("tenant-a", "kb", "owner", "public")
    with pytest.raises(TypeError):
        store.authorize_query(object(), "kb")  # type: ignore[arg-type]


def test_empty_subset_cannot_be_constructed_or_confused_with_all(tmp_path):
    store = _store(tmp_path)
    store.set_kb_policy("tenant-a", "kb", "owner", "private")
    store.set_document_policy(
        "tenant-a", "kb", "secret", "secret.pdf", policy="private"
    )
    decision = store.authorize_query(_principal(), "kb")
    assert decision.mode is AccessMode.DENY
    assert not decision.allows_source("anything.pdf")

    with pytest.raises(ValueError, match="non-empty allowlist"):
        QueryAuthorization(
            tenant_id="tenant-a",
            kb_id="kb",
            permission=Permission.QUERY,
            mode=AccessMode.SUBSET,
            acl_epoch=1,
        )


def test_concurrent_mutations_are_serialized_and_epochs_are_not_lost(tmp_path):
    store = _store(tmp_path)
    store.set_kb_policy("tenant-a", "kb", "owner", "private")

    def add(index: int) -> None:
        document_id = f"doc-{index:02d}"
        subject_id = f"subject-{index:02d}"
        store.set_document_policy(
            "tenant-a",
            "kb",
            document_id,
            f"source-{index:02d}.pdf",
            policy="private",
        )
        store.grant_subject(
            "tenant-a", "kb", subject_id, Role.VIEWER, document_id=document_id
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(add, range(24)))

    assert len(store.list_document_policies("tenant-a", "kb")) == 24
    assert len(store.list_grants("tenant-a", "kb")) == 24
    assert store.acl_epoch("tenant-a", "kb") == 49


def test_store_read_failure_is_deny_even_with_legacy_workspace_enabled(tmp_path):
    store = _store(tmp_path, legacy_workspace_default=True)
    store.close()
    decision = store.authorize_query(_principal(), "kb")
    assert decision.mode is AccessMode.DENY
    assert decision.reason == "store_unavailable"


def test_cleanup_is_tenant_scoped_and_preserves_epoch_tombstones(tmp_path):
    store = _store(tmp_path)
    for tenant in ("tenant-a", "tenant-b"):
        store.set_kb_policy(tenant, "kb", f"owner-{tenant}", "workspace")
        store.set_document_policy(tenant, "kb", "doc", f"{tenant}.pdf")
    tenant_a_epoch = store.acl_epoch("tenant-a", "kb")
    tenant_b_epoch = store.acl_epoch("tenant-b", "kb")

    assert store.clear_tenant("tenant-a") == 1
    assert store.list_kb_policies("tenant-a") == []
    assert store.list_document_policies("tenant-a", "kb") == []
    assert store.acl_epoch("tenant-a", "kb") == tenant_a_epoch + 1
    assert store.get_kb_policy("tenant-b", "kb") is not None
    assert store.acl_epoch("tenant-b", "kb") == tenant_b_epoch
