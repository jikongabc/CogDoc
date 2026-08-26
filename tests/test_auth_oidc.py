import sqlite3

import pytest

from cogdoc.api.auth_store import (
    AuthAuthenticationError,
    AuthAuthorizationError,
    AuthConflictError,
    AuthStore,
)


PASSWORD = "correct horse battery"
ISSUER = "https://id.example.com"


class Clock:
    def __init__(self, value=1_900_000_000.0):
        self.value = value

    def __call__(self):
        return self.value


@pytest.fixture
def store(tmp_path):
    result = AuthStore(str(tmp_path / "state.db"), scrypt_n=1 << 10, clock=Clock())
    yield result
    result.close()


def test_workspace_oidc_policy_jit_provisions_and_reuses_subject(store):
    owner = store.register("owner@example.com", PASSWORD, "Owner")
    workspace_id = owner["workspace"]["workspace_id"]
    policy = store.set_oidc_policy(
        workspace_id=workspace_id,
        issuer=ISSUER,
        allowed_domains=["example.com"],
        default_role="viewer",
        enabled=True,
        actor_user_id=owner["user"]["user_id"],
    )
    assert policy["revision"] == 0

    first = store.login_oidc(
        issuer=ISSUER,
        subject="bob-subject",
        email="Bob@Example.com",
        display_name="Bob",
        email_verified=True,
        workspace_id=workspace_id,
        jit_provisioning_enabled=True,
    )
    assert first["user"]["email"] == "bob@example.com"
    assert first["workspace"]["role"] == "viewer"
    assert store.session_is_active(
        session_id=first["session"]["session_id"],
        user_id=first["user"]["user_id"],
    )

    second = store.login_oidc(
        issuer=ISSUER,
        subject="bob-subject",
        email="bob@example.com",
        display_name="Ignored Rename",
        email_verified=True,
        workspace_id=workspace_id,
    )
    assert second["user"]["user_id"] == first["user"]["user_id"]
    assert len(store.list_oidc_identities(user_id=first["user"]["user_id"])) == 1


def test_oidc_jit_requires_verified_email_matching_policy(store):
    owner = store.register("owner@example.com", PASSWORD, "Owner")
    workspace_id = owner["workspace"]["workspace_id"]
    store.set_oidc_policy(
        workspace_id=workspace_id,
        issuer=ISSUER,
        allowed_domains=["example.com"],
        default_role="viewer",
        enabled=True,
        actor_user_id=owner["user"]["user_id"],
    )
    with pytest.raises(AuthAuthenticationError, match="verified"):
        store.login_oidc(
            issuer=ISSUER,
            subject="subject",
            email="bob@example.com",
            display_name="Bob",
            email_verified=False,
            workspace_id=workspace_id,
            jit_provisioning_enabled=True,
        )
    with pytest.raises(AuthAuthorizationError, match="not admitted"):
        store.login_oidc(
            issuer=ISSUER,
            subject="subject",
            email="bob@other.example",
            display_name="Bob",
            email_verified=True,
            workspace_id=workspace_id,
            jit_provisioning_enabled=True,
        )


def test_existing_email_requires_explicit_link_by_default(store):
    registered = store.register("alice@example.com", PASSWORD, "Alice")
    with pytest.raises(AuthConflictError, match="explicit linking"):
        store.login_oidc(
            issuer=ISSUER,
            subject="alice-subject",
            email="alice@example.com",
            display_name="Alice",
            email_verified=True,
            jit_provisioning_enabled=True,
        )

    identity = store.link_oidc_identity(
        user_id=registered["user"]["user_id"],
        issuer=ISSUER,
        subject="alice-subject",
        email="alice@example.com",
        email_verified=True,
    )
    result = store.login_oidc(
        issuer=ISSUER,
        subject="alice-subject",
        email="alice@example.com",
        display_name="Alice",
        email_verified=True,
    )
    assert result["user"]["user_id"] == registered["user"]["user_id"]
    assert store.unlink_oidc_identity(
        identity_id=identity["identity_id"],
        user_id=registered["user"]["user_id"],
    )


def test_oidc_only_user_cannot_remove_only_authentication_method(store):
    result = store.login_oidc(
        issuer=ISSUER,
        subject="only-subject",
        email="only@example.com",
        display_name="Only",
        email_verified=True,
        jit_provisioning_enabled=True,
    )
    identity = store.list_oidc_identities(user_id=result["user"]["user_id"])[0]
    with pytest.raises(AuthConflictError, match="only authentication"):
        store.unlink_oidc_identity(
            identity_id=identity["identity_id"],
            user_id=result["user"]["user_id"],
        )
    with pytest.raises(AuthAuthenticationError):
        store.login("only@example.com", "some plausible password")


def test_auth_schema_v1_is_migrated_in_place(tmp_path):
    path = str(tmp_path / "state.db")
    first = AuthStore(path, scrypt_n=1 << 10)
    first._conn.execute(
        "UPDATE auth_schema_meta SET value='1' WHERE key='schema_version'"
    )
    first.close()

    reopened = AuthStore(path, scrypt_n=1 << 10)
    assert reopened.check()
    assert reopened._conn.execute(
        "SELECT value FROM auth_schema_meta WHERE key='schema_version'"
    ).fetchone() == ("9",)
    for table in (
        "auth_oidc_identities",
        "auth_workspace_oidc_policies",
        "auth_oidc_managed_memberships",
        "auth_password_capabilities",
        "auth_service_accounts",
        "auth_service_tokens",
        "auth_service_account_policies",
        "auth_workspace_session_policies",
    ):
        assert reopened._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
    reopened.close()


def test_auth_schema_v6_adds_group_policy_columns_with_safe_defaults(tmp_path):
    path = str(tmp_path / "legacy-v6.db")
    legacy = sqlite3.connect(path)
    legacy.executescript(
        """
        CREATE TABLE auth_schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO auth_schema_meta(key,value) VALUES('schema_version','6');
        CREATE TABLE auth_workspace_oidc_policies (
            workspace_id TEXT PRIMARY KEY,
            issuer TEXT NOT NULL,
            allowed_domains_json TEXT NOT NULL,
            default_role TEXT NOT NULL,
            enabled INTEGER NOT NULL,
            revision INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );
        """
    )
    legacy.close()

    migrated = AuthStore(path, scrypt_n=1 << 10)
    assert migrated.check()
    columns = {
        row[1]
        for row in migrated._conn.execute(
            "PRAGMA table_info(auth_workspace_oidc_policies)"
        )
    }
    assert {"group_claim", "group_role_map_json", "require_mapped_group"} <= columns
    assert migrated._conn.execute(
        "SELECT value FROM auth_schema_meta WHERE key='schema_version'"
    ).fetchone() == ("9",)
    migrated.close()


def test_oidc_group_mapping_controls_jit_role_and_reconciles_managed_member(store):
    owner = store.register("owner@example.com", PASSWORD, "Owner")
    workspace_id = owner["workspace"]["workspace_id"]
    actor = owner["user"]["user_id"]
    policy = store.set_oidc_policy(
        workspace_id=workspace_id,
        issuer=ISSUER,
        allowed_domains=["example.com"],
        default_role="viewer",
        enabled=True,
        group_claim="team_groups",
        group_role_map={"CogDoc Editors": "editor", "COGDOC ADMINS": "admin"},
        require_mapped_group=True,
        actor_user_id=actor,
    )
    assert policy["group_role_map"] == {
        "cogdoc admins": "admin",
        "cogdoc editors": "editor",
    }

    first = store.login_oidc(
        issuer=ISSUER,
        subject="group-user",
        email="group-user@example.com",
        display_name="Group User",
        email_verified=True,
        group_claims={"team_groups": ["COGDOC EDITORS", "cogdoc admins"]},
        workspace_id=workspace_id,
        jit_provisioning_enabled=True,
    )
    assert first["workspace"]["role"] == "admin"

    second = store.login_oidc(
        issuer=ISSUER,
        subject="group-user",
        email="group-user@example.com",
        display_name="Group User",
        email_verified=True,
        group_claims={"team_groups": ["CogDoc Editors"]},
        workspace_id=workspace_id,
    )
    assert second["workspace"]["role"] == "editor"


def test_oidc_group_requirement_fails_closed_without_persisting_identity(store):
    owner = store.register("owner@example.com", PASSWORD, "Owner")
    workspace_id = owner["workspace"]["workspace_id"]
    store.set_oidc_policy(
        workspace_id=workspace_id,
        issuer=ISSUER,
        allowed_domains=["example.com"],
        default_role="viewer",
        enabled=True,
        group_role_map={"approved": "viewer"},
        require_mapped_group=True,
        actor_user_id=owner["user"]["user_id"],
    )

    with pytest.raises(AuthAuthorizationError, match="mapped workspace group"):
        store.login_oidc(
            issuer=ISSUER,
            subject="unmapped-user",
            email="unmapped@example.com",
            display_name="Unmapped",
            email_verified=True,
            group_claims={"groups": ["other"]},
            workspace_id=workspace_id,
            jit_provisioning_enabled=True,
        )
    assert store.lookup_user("unmapped@example.com") is None


def test_manual_role_change_releases_oidc_role_authority(store):
    owner = store.register("owner@example.com", PASSWORD, "Owner")
    workspace_id = owner["workspace"]["workspace_id"]
    actor = owner["user"]["user_id"]
    store.set_oidc_policy(
        workspace_id=workspace_id,
        issuer=ISSUER,
        allowed_domains=["example.com"],
        default_role="viewer",
        enabled=True,
        group_role_map={"editors": "editor"},
        actor_user_id=actor,
    )
    login = store.login_oidc(
        issuer=ISSUER,
        subject="manual-user",
        email="manual@example.com",
        display_name="Manual",
        email_verified=True,
        group_claims={"groups": ["editors"]},
        workspace_id=workspace_id,
        jit_provisioning_enabled=True,
    )
    member = next(
        item
        for item in store.list_members(workspace_id)
        if item["user_id"] == login["user"]["user_id"]
    )
    store.update_member_role(workspace_id, member["member_id"], "reviewer", actor)

    relogin = store.login_oidc(
        issuer=ISSUER,
        subject="manual-user",
        email="manual@example.com",
        display_name="Manual",
        email_verified=True,
        group_claims={"groups": ["editors"]},
        workspace_id=workspace_id,
    )
    assert relogin["workspace"]["role"] == "reviewer"


def test_oidc_policy_revision_is_optimistic(store):
    owner = store.register("owner@example.com", PASSWORD, "Owner")
    workspace_id = owner["workspace"]["workspace_id"]
    actor = owner["user"]["user_id"]
    store.set_oidc_policy(
        workspace_id=workspace_id,
        issuer=ISSUER,
        allowed_domains=["example.com"],
        default_role="viewer",
        enabled=True,
        actor_user_id=actor,
    )
    updated = store.set_oidc_policy(
        workspace_id=workspace_id,
        issuer=ISSUER,
        allowed_domains=["example.com", "subsidiary.example"],
        default_role="editor",
        enabled=True,
        actor_user_id=actor,
        expected_revision=0,
    )
    assert updated["revision"] == 1
    with pytest.raises(AuthConflictError, match="revision"):
        store.set_oidc_policy(
            workspace_id=workspace_id,
            issuer=ISSUER,
            allowed_domains=["example.com"],
            default_role="viewer",
            enabled=False,
            actor_user_id=actor,
            expected_revision=0,
        )


def test_oidc_tables_contain_no_provider_tokens(store):
    # The identity layer persists only stable identity claims. OAuth access or
    # refresh tokens are never accepted by this API or represented in schema.
    columns = {
        row[1] for row in store._conn.execute("PRAGMA table_info(auth_oidc_identities)")
    }
    assert columns.isdisjoint({"access_token", "refresh_token", "id_token"})
    assert isinstance(store._conn, sqlite3.Connection)
