from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from cogdoc.api.auth_store import (
    AuthAuthorizationError,
    AuthConflictError,
    AuthNotFoundError,
    AuthStore,
    AuthStoreError,
)


PASSWORD = "correct horse battery"
ISSUER = "https://id.example.com"


@pytest.fixture
def directory(tmp_path):
    store = AuthStore(str(tmp_path / "state.db"), scrypt_n=1 << 10)
    owner = store.register("owner@example.com", PASSWORD, "Owner")
    yield store, owner
    store.close()


def test_scim_user_preprovision_links_oidc_and_deactivation_revokes_access(directory):
    store, owner = directory
    workspace_id = owner["workspace"]["workspace_id"]
    provisioned = store.create_scim_user(
        workspace_id=workspace_id,
        issuer=ISSUER,
        external_id="entra-user-1",
        user_name="Alice@Example.com",
        display_name="Alice",
        active=True,
        base_role="viewer",
    )

    login = store.login_oidc(
        issuer=ISSUER,
        subject="subject-alice",
        email="alice@example.com",
        display_name="Alice",
        email_verified=True,
        workspace_id=workspace_id,
        jit_provisioning_enabled=False,
        allow_verified_email_link=False,
    )
    assert login["workspace"]["workspace_id"] == workspace_id
    assert login["workspace"]["role"] == "viewer"

    disabled = store.update_scim_user(
        workspace_id=workspace_id,
        scim_user_id=provisioned["id"],
        external_id=provisioned["external_id"],
        user_name=provisioned["user_name"],
        display_name=provisioned["display_name"],
        active=False,
        base_role=provisioned["base_role"],
        expected_revision=provisioned["revision"],
    )
    assert disabled["active"] is False
    with pytest.raises(AuthStoreError):
        store.authenticate_session(login["access_token"], workspace_id=workspace_id)
    with pytest.raises(AuthAuthorizationError, match="inactive"):
        store.login_oidc(
            issuer=ISSUER,
            subject="subject-alice",
            email="alice@example.com",
            display_name="Alice",
            email_verified=True,
            workspace_id=workspace_id,
        )


def test_scim_role_remains_authoritative_over_oidc_group_mapping(directory):
    store, owner = directory
    workspace_id = owner["workspace"]["workspace_id"]
    store.set_oidc_policy(
        workspace_id=workspace_id,
        issuer=ISSUER,
        allowed_domains=["example.com"],
        default_role="viewer",
        enabled=True,
        group_role_map={"oidc-admins": "admin"},
        require_mapped_group=True,
        actor_user_id=owner["user"]["user_id"],
    )
    store.create_scim_user(
        workspace_id=workspace_id,
        issuer=ISSUER,
        external_id="directory-oidc-priority",
        user_name="priority@example.com",
        display_name="Priority",
        active=True,
        base_role="reviewer",
    )

    login = store.login_oidc(
        issuer=ISSUER,
        subject="priority-subject",
        email="priority@example.com",
        display_name="Priority",
        email_verified=True,
        group_claims={"groups": ["oidc-admins"]},
        workspace_id=workspace_id,
    )
    assert login["workspace"]["role"] == "reviewer"
    assert store._conn.execute(
        "SELECT COUNT(*) FROM auth_oidc_managed_memberships WHERE workspace_id=?",
        (workspace_id,),
    ).fetchone() == (0,)


def test_scim_group_role_is_highest_active_mapping_and_reverts_atomically(directory):
    store, owner = directory
    workspace_id = owner["workspace"]["workspace_id"]
    user = store.create_scim_user(
        workspace_id=workspace_id,
        issuer=ISSUER,
        external_id=None,
        user_name="bob@example.com",
        display_name="Bob",
        active=True,
        base_role="viewer",
    )
    group = store.create_scim_group(
        workspace_id=workspace_id,
        external_id="group-admin",
        display_name="CogDoc Admins",
        mapped_role="admin",
        member_ids=[user["id"]],
    )
    assert group["members"] == [user["id"]]
    assert store.get_workspace(workspace_id, user_id=user["user_id"])["role"] == "admin"

    replaced = store.update_scim_group(
        workspace_id=workspace_id,
        scim_group_id=group["id"],
        external_id=group["external_id"],
        display_name=group["display_name"],
        mapped_role=group["mapped_role"],
        member_ids=[],
        expected_revision=group["revision"],
    )
    assert replaced["members"] == []
    assert (
        store.get_workspace(workspace_id, user_id=user["user_id"])["role"] == "viewer"
    )


def test_scim_versions_filters_soft_delete_and_workspace_scope(directory):
    store, owner = directory
    workspace_id = owner["workspace"]["workspace_id"]
    other = store.create_workspace(owner["user"]["user_id"], "Other")
    user = store.create_scim_user(
        workspace_id=workspace_id,
        issuer=ISSUER,
        external_id="directory-123",
        user_name="case@example.com",
        display_name="Case",
        active=True,
        base_role="reviewer",
    )

    total, rows = store.list_scim_users(
        workspace_id=workspace_id,
        filter_field="externalId",
        filter_value="directory-123",
    )
    assert total == 1
    assert [row["id"] for row in rows] == [user["id"]]
    with pytest.raises(AuthNotFoundError):
        store.get_scim_user(workspace_id=other["workspace_id"], scim_user_id=user["id"])
    with pytest.raises(AuthConflictError, match="version"):
        store.update_scim_user(
            workspace_id=workspace_id,
            scim_user_id=user["id"],
            external_id=user["external_id"],
            user_name=user["user_name"],
            display_name=user["display_name"],
            active=True,
            base_role=user["base_role"],
            expected_revision=99,
        )

    assert store.delete_scim_user(workspace_id=workspace_id, scim_user_id=user["id"])
    assert store.list_scim_users(workspace_id=workspace_id) == (0, [])
    with pytest.raises(AuthNotFoundError):
        store.get_scim_user(workspace_id=workspace_id, scim_user_id=user["id"])


def test_scim_policy_reconciliation_downgrades_persisted_memberships(directory):
    store, owner = directory
    workspace_id = owner["workspace"]["workspace_id"]
    user = store.create_scim_user(
        workspace_id=workspace_id,
        issuer=ISSUER,
        external_id="directory-policy-user",
        user_name="policy@example.com",
        display_name="Policy User",
        active=True,
        base_role="editor",
    )
    group = store.create_scim_group(
        workspace_id=workspace_id,
        external_id="directory-policy-group",
        display_name="CogDoc Admins",
        mapped_role="admin",
        member_ids=[user["id"]],
    )
    assert store.get_workspace(workspace_id, user_id=user["user_id"])["role"] == "admin"

    changed = store.reconcile_scim_policy(
        workspace_id=workspace_id,
        default_role="viewer",
        group_role_map={},
    )

    assert changed == 2
    assert (
        store.get_workspace(workspace_id, user_id=user["user_id"])["role"] == "viewer"
    )
    assert (
        store.get_scim_user(workspace_id=workspace_id, scim_user_id=user["id"])[
            "base_role"
        ]
        == "viewer"
    )
    assert (
        store.get_scim_group(workspace_id=workspace_id, scim_group_id=group["id"])[
            "mapped_role"
        ]
        is None
    )
    assert (
        store.reconcile_scim_policy(
            workspace_id=workspace_id,
            default_role="viewer",
            group_role_map={},
        )
        == 0
    )


def test_scim_global_account_disable_requires_every_provision_to_be_inactive(directory):
    store, owner = directory
    first_workspace = owner["workspace"]["workspace_id"]
    second_workspace = store.create_workspace(owner["user"]["user_id"], "Second")[
        "workspace_id"
    ]
    first = store.create_scim_user(
        workspace_id=first_workspace,
        issuer=ISSUER,
        external_id="directory-multi-1",
        user_name="multi@example.com",
        display_name="Multi Workspace",
        active=True,
        base_role="viewer",
    )
    second = store.create_scim_user(
        workspace_id=second_workspace,
        issuer=ISSUER,
        external_id="directory-multi-2",
        user_name="multi@example.com",
        display_name="Multi Workspace",
        active=True,
        base_role="reviewer",
    )
    store.update_scim_user(
        workspace_id=first_workspace,
        scim_user_id=first["id"],
        external_id=first["external_id"],
        user_name=first["user_name"],
        display_name=first["display_name"],
        active=False,
        base_role=first["base_role"],
        expected_revision=first["revision"],
    )

    with pytest.raises(AuthAuthorizationError, match="inactive"):
        store.login_oidc(
            issuer=ISSUER,
            subject="multi-subject",
            email="multi@example.com",
            display_name="Multi Workspace",
            email_verified=True,
            workspace_id=first_workspace,
        )
    active_login = store.login_oidc(
        issuer=ISSUER,
        subject="multi-subject",
        email="multi@example.com",
        display_name="Multi Workspace",
        email_verified=True,
        workspace_id=second_workspace,
    )
    assert active_login["workspace"]["role"] == "reviewer"

    store.update_scim_user(
        workspace_id=second_workspace,
        scim_user_id=second["id"],
        external_id=second["external_id"],
        user_name=second["user_name"],
        display_name=second["display_name"],
        active=False,
        base_role=second["base_role"],
        expected_revision=second["revision"],
    )
    with pytest.raises(AuthStoreError):
        store.authenticate_session(active_login["access_token"])
    with pytest.raises(AuthAuthorizationError, match="inactive"):
        store.login_oidc(
            issuer=ISSUER,
            subject="multi-subject",
            email="multi@example.com",
            display_name="Multi Workspace",
            email_verified=True,
        )


def test_deleting_scim_user_removes_membership_and_bumps_group_version(directory):
    store, owner = directory
    workspace_id = owner["workspace"]["workspace_id"]
    user = store.create_scim_user(
        workspace_id=workspace_id,
        issuer=ISSUER,
        external_id="directory-deleted",
        user_name="deleted@example.com",
        display_name="Deleted",
        active=True,
        base_role="viewer",
    )
    group = store.create_scim_group(
        workspace_id=workspace_id,
        external_id="directory-group",
        display_name="Readers",
        mapped_role="reviewer",
        member_ids=[user["id"]],
    )

    assert store.delete_scim_user(
        workspace_id=workspace_id,
        scim_user_id=user["id"],
        expected_revision=user["revision"] + 1,
    )
    refreshed = store.get_scim_group(
        workspace_id=workspace_id, scim_group_id=group["id"]
    )
    assert refreshed["members"] == []
    assert refreshed["revision"] == group["revision"] + 1
    with pytest.raises(AuthNotFoundError):
        store.get_workspace(workspace_id, user_id=user["user_id"])


def test_workspace_scim_rename_cannot_mutate_global_identity(directory):
    store, owner = directory
    first_workspace = owner["workspace"]["workspace_id"]
    second_workspace = store.create_workspace(owner["user"]["user_id"], "Second")[
        "workspace_id"
    ]
    first = store.create_scim_user(
        workspace_id=first_workspace,
        issuer=ISSUER,
        external_id="rename-first",
        user_name="original@example.com",
        display_name="Original",
        active=True,
        base_role="viewer",
    )
    second = store.create_scim_user(
        workspace_id=second_workspace,
        issuer=ISSUER,
        external_id="rename-second",
        user_name="original@example.com",
        display_name="Original",
        active=True,
        base_role="viewer",
    )
    assert first["user_id"] == second["user_id"]

    renamed = store.update_scim_user(
        workspace_id=first_workspace,
        scim_user_id=first["id"],
        external_id=first["external_id"],
        user_name="renamed@example.com",
        display_name="Renamed by First Directory",
        active=True,
        base_role=first["base_role"],
        expected_revision=first["revision"],
    )

    global_user = store.get_user(user_id=first["user_id"])
    untouched = store.get_scim_user(
        workspace_id=second_workspace, scim_user_id=second["id"]
    )
    assert global_user["email"] == "original@example.com"
    assert global_user["display_name"] == "Original"
    assert untouched["user_name"] == "original@example.com"
    assert renamed["user_name"] == "renamed@example.com"
    with pytest.raises(AuthConflictError, match="managed by SCIM"):
        store.register(
            "renamed@example.com",
            PASSWORD,
            "Conflicting Local Account",
        )
    linked = store.login_oidc(
        issuer=ISSUER,
        subject="renamed-subject",
        email="renamed@example.com",
        display_name="Renamed by Provider",
        email_verified=True,
        workspace_id=first_workspace,
        jit_provisioning_enabled=False,
        allow_verified_email_link=False,
    )
    assert linked["user"]["user_id"] == first["user_id"]


def test_scim_revision_cas_is_atomic_across_store_instances(tmp_path):
    database = tmp_path / "state.db"
    first_store = AuthStore(str(database), scrypt_n=1 << 10)
    owner = first_store.register("owner@example.com", PASSWORD, "Owner")
    workspace_id = owner["workspace"]["workspace_id"]
    user = first_store.create_scim_user(
        workspace_id=workspace_id,
        issuer=ISSUER,
        external_id="concurrent-user",
        user_name="concurrent@example.com",
        display_name="Concurrent",
        active=True,
        base_role="viewer",
    )
    second_store = AuthStore(str(database), scrypt_n=1 << 10)
    barrier = threading.Barrier(2)

    def update(store, display_name):
        barrier.wait(timeout=2)
        try:
            row = store.update_scim_user(
                workspace_id=workspace_id,
                scim_user_id=user["id"],
                external_id=user["external_id"],
                user_name=user["user_name"],
                display_name=display_name,
                active=True,
                base_role=user["base_role"],
                expected_revision=user["revision"],
            )
            return "success", row["display_name"]
        except AuthConflictError:
            return "conflict", display_name

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(
                executor.map(
                    lambda args: update(*args),
                    ((first_store, "First"), (second_store, "Second")),
                )
            )
        assert sorted(result[0] for result in results) == ["conflict", "success"]
        final = first_store.get_scim_user(
            workspace_id=workspace_id, scim_user_id=user["id"]
        )
        assert final["revision"] == user["revision"] + 1
        assert final["display_name"] in {"First", "Second"}
    finally:
        second_store.close()
        first_store.close()
