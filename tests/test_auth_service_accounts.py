import sqlite3

import pytest

from cogdoc.api.auth_store import (
    AuthAuthenticationError,
    AuthAuthorizationError,
    AuthConflictError,
    AuthStore,
    AuthValidationError,
)
from cogdoc.api.tenancy import Permission


PASSWORD = "correct horse battery"


def test_service_token_is_one_time_scoped_and_uses_live_account_role(tmp_path):
    now = [1_800_000_000.0]
    database = tmp_path / "state.db"
    store = AuthStore(str(database), scrypt_n=1 << 10, clock=lambda: now[0])
    owner = store.register("owner@example.com", PASSWORD, "Owner")
    workspace_id = owner["workspace"]["workspace_id"]
    account = store.create_service_account(
        workspace_id=workspace_id,
        name="Ingest Bot",
        description="Production ingestion",
        role="viewer",
        actor_user_id=owner["user"]["user_id"],
    )
    created = store.create_service_token(
        workspace_id=workspace_id,
        service_account_id=account["service_account_id"],
        label="primary",
        ttl_seconds=86400,
        actor_user_id=owner["user"]["user_id"],
    )
    raw = created.pop("token")
    assert raw.startswith("cog_svc_")
    assert raw not in str(
        store.list_service_tokens(
            workspace_id=workspace_id,
            service_account_id=account["service_account_id"],
            actor_user_id=owner["user"]["user_id"],
        )
    )

    context = store.authenticate_service_token(raw, workspace_id)
    assert (
        context.principal.subject_id
        == f"service-account:{account['service_account_id']}"
    )
    assert context.principal.role.value == "viewer"
    assert context.principal.permissions == {Permission.READ, Permission.QUERY}
    updated = store.update_service_account(
        workspace_id=workspace_id,
        service_account_id=account["service_account_id"],
        name=account["name"],
        description=account["description"],
        role="editor",
        active=True,
        expected_revision=account["revision"],
        actor_user_id=owner["user"]["user_id"],
    )
    assert (
        store.authenticate_service_token(raw, workspace_id).principal.role.value
        == "editor"
    )
    assert store.authenticate_service_token(
        raw, workspace_id
    ).principal.permissions == {
        Permission.READ,
        Permission.QUERY,
    }
    with pytest.raises(AuthConflictError, match="version"):
        store.update_service_account(
            workspace_id=workspace_id,
            service_account_id=account["service_account_id"],
            name="stale",
            description="",
            role="viewer",
            active=True,
            expected_revision=account["revision"],
            actor_user_id=owner["user"]["user_id"],
        )

    disabled = store.update_service_account(
        workspace_id=workspace_id,
        service_account_id=account["service_account_id"],
        name=updated["name"],
        description=updated["description"],
        role=updated["role"],
        active=False,
        expected_revision=updated["revision"],
        actor_user_id=owner["user"]["user_id"],
    )
    with pytest.raises(AuthAuthenticationError):
        store.authenticate_service_token(raw)
    store.update_service_account(
        workspace_id=workspace_id,
        service_account_id=account["service_account_id"],
        name=disabled["name"],
        description=disabled["description"],
        role=disabled["role"],
        active=True,
        expected_revision=disabled["revision"],
        actor_user_id=owner["user"]["user_id"],
    )
    with pytest.raises(AuthAuthenticationError):
        store.authenticate_service_token(raw)
    store.close()

    raw_bytes = database.read_bytes()
    assert raw.encode() not in raw_bytes
    with sqlite3.connect(database) as connection:
        token_hash, hint = connection.execute(
            "SELECT token_hash,secret_hint FROM auth_service_tokens"
        ).fetchone()
    assert len(token_hash) == 64
    assert hint.endswith(raw[-4:])


def test_service_token_explicit_permissions_are_role_bounded_and_live_intersected(
    tmp_path,
):
    store = AuthStore(str(tmp_path / "state.db"), scrypt_n=1 << 10)
    owner = store.register("owner@example.com", PASSWORD, "Owner")
    workspace_id = owner["workspace"]["workspace_id"]
    account = store.create_service_account(
        workspace_id=workspace_id,
        name="Publisher",
        role="reviewer",
        actor_user_id=owner["user"]["user_id"],
    )
    scoped = store.create_service_token(
        workspace_id=workspace_id,
        service_account_id=account["service_account_id"],
        label="publish-only",
        ttl_seconds=3600,
        permissions=["read", "publish"],
        actor_user_id=owner["user"]["user_id"],
    )
    principal = store.authenticate_service_token(scoped["token"]).principal
    assert principal.permissions == {Permission.READ, Permission.PUBLISH}
    assert scoped["permissions"] == ["publish", "read"]
    updated = store.update_service_account(
        workspace_id=workspace_id,
        service_account_id=account["service_account_id"],
        name=account["name"],
        description=account["description"],
        role="viewer",
        active=True,
        expected_revision=account["revision"],
        actor_user_id=owner["user"]["user_id"],
    )
    assert updated["role"] == "viewer"
    assert store.authenticate_service_token(scoped["token"]).principal.permissions == {
        Permission.READ
    }
    with pytest.raises(AuthValidationError, match="permissions"):
        store.create_service_token(
            workspace_id=workspace_id,
            service_account_id=account["service_account_id"],
            label="too broad",
            ttl_seconds=3600,
            permissions=["delete"],
            actor_user_id=owner["user"]["user_id"],
        )
    store.close()


def test_service_token_v4_schema_migrates_with_legacy_role_scope(tmp_path):
    database = tmp_path / "state.db"
    store = AuthStore(str(database), scrypt_n=1 << 10)
    store.close()
    with sqlite3.connect(database) as connection:
        connection.execute(
            "ALTER TABLE auth_service_tokens DROP COLUMN permissions_json"
        )
        connection.execute(
            "UPDATE auth_schema_meta SET value='4' WHERE key='schema_version'"
        )
    reopened = AuthStore(str(database), scrypt_n=1 << 10)
    try:
        columns = {
            str(row[1])
            for row in reopened._conn.execute(
                "PRAGMA table_info(auth_service_tokens)"
            ).fetchall()
        }
        assert "permissions_json" in columns
        assert reopened.check()
    finally:
        reopened.close()


def test_workspace_service_policy_limits_issuance_and_live_permissions(tmp_path):
    store = AuthStore(str(tmp_path / "state.db"), scrypt_n=1 << 10)
    owner = store.register("owner@example.com", PASSWORD, "Owner")
    workspace_id = owner["workspace"]["workspace_id"]
    user_id = owner["user"]["user_id"]
    default = store.get_service_account_policy(
        workspace_id=workspace_id, actor_user_id=user_id
    )
    assert default["revision"] == 0
    policy = store.set_service_account_policy(
        workspace_id=workspace_id,
        max_accounts=1,
        max_tokens_per_account=1,
        max_token_ttl_days=7,
        allow_non_expiring=False,
        allowed_permissions=["read", "query", "write"],
        expected_revision=0,
        actor_user_id=user_id,
    )
    account = store.create_service_account(
        workspace_id=workspace_id,
        name="Scoped",
        role="editor",
        actor_user_id=user_id,
    )
    with pytest.raises(AuthConflictError, match="too many"):
        store.create_service_account(
            workspace_id=workspace_id,
            name="Second",
            role="viewer",
            actor_user_id=user_id,
        )
    with pytest.raises(AuthValidationError, match="non-expiring"):
        store.create_service_token(
            workspace_id=workspace_id,
            service_account_id=account["service_account_id"],
            label="forever",
            ttl_seconds=None,
            actor_user_id=user_id,
        )
    token = store.create_service_token(
        workspace_id=workspace_id,
        service_account_id=account["service_account_id"],
        label="weekly",
        ttl_seconds=7 * 86400,
        actor_user_id=user_id,
    )
    assert token["permissions"] == ["query", "read", "write"]
    tightened = store.set_service_account_policy(
        workspace_id=workspace_id,
        max_accounts=1,
        max_tokens_per_account=1,
        max_token_ttl_days=7,
        allow_non_expiring=False,
        allowed_permissions=["read"],
        expected_revision=policy["revision"],
        actor_user_id=user_id,
    )
    assert tightened["revision"] == 2
    assert store.authenticate_service_token(token["token"]).principal.permissions == {
        Permission.READ
    }
    with pytest.raises(AuthConflictError, match="too many"):
        store.create_service_token(
            workspace_id=workspace_id,
            service_account_id=account["service_account_id"],
            label="second",
            ttl_seconds=86400,
            permissions=["read"],
            actor_user_id=user_id,
        )
    store.close()


def test_service_token_expiry_cross_workspace_and_revision_safe_revoke(tmp_path):
    now = [1_800_000_000.0]
    store = AuthStore(
        str(tmp_path / "state.db"), scrypt_n=1 << 10, clock=lambda: now[0]
    )
    owner = store.register("owner@example.com", PASSWORD, "Owner")
    workspace_id = owner["workspace"]["workspace_id"]
    other = store.create_workspace(owner["user"]["user_id"], "Other")
    account = store.create_service_account(
        workspace_id=workspace_id,
        name="Automation",
        role="reviewer",
        actor_user_id=owner["user"]["user_id"],
    )
    created = store.create_service_token(
        workspace_id=workspace_id,
        service_account_id=account["service_account_id"],
        label="short lived",
        ttl_seconds=60,
        actor_user_id=owner["user"]["user_id"],
    )
    raw = created["token"]
    with pytest.raises(AuthAuthorizationError, match="outside target workspace"):
        store.authenticate_service_token(raw, other["workspace_id"])
    with pytest.raises(AuthConflictError, match="version"):
        store.revoke_service_token(
            workspace_id=workspace_id,
            service_account_id=account["service_account_id"],
            token_id=created["token_id"],
            expected_revision=99,
            actor_user_id=owner["user"]["user_id"],
        )
    assert store.revoke_service_token(
        workspace_id=workspace_id,
        service_account_id=account["service_account_id"],
        token_id=created["token_id"],
        expected_revision=created["revision"],
        actor_user_id=owner["user"]["user_id"],
    )
    with pytest.raises(AuthAuthenticationError):
        store.authenticate_service_token(raw)

    expiring = store.create_service_token(
        workspace_id=workspace_id,
        service_account_id=account["service_account_id"],
        label="expires",
        ttl_seconds=30,
        actor_user_id=owner["user"]["user_id"],
    )
    now[0] += 31
    with pytest.raises(AuthAuthenticationError):
        store.authenticate_service_token(expiring["token"])
    store.close()
