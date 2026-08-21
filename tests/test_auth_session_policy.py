import pytest

from cogdoc.api.auth_store import (
    AuthAuthenticationError,
    AuthAuthorizationError,
    AuthConflictError,
    AuthNotFoundError,
    AuthStore,
)


PASSWORD = "correct horse battery"


class Clock:
    def __init__(self, value=1_900_000_000.0):
        self.value = value

    def __call__(self):
        return self.value


@pytest.fixture
def identity(tmp_path):
    clock = Clock()
    store = AuthStore(str(tmp_path / "state.db"), scrypt_n=1 << 10, clock=clock)
    owner = store.register("owner@example.com", PASSWORD, "Owner")
    yield store, owner, clock
    store.close()


def test_workspace_session_policy_is_manager_only_and_revision_safe(identity):
    store, owner, _clock = identity
    workspace_id = owner["workspace"]["workspace_id"]
    owner_id = owner["user"]["user_id"]
    viewer = store.register("viewer@example.com", PASSWORD, "Viewer")
    store.add_member(workspace_id, viewer["user"]["user_id"], "viewer", owner_id)

    default = store.get_workspace_session_policy(
        workspace_id=workspace_id, actor_user_id=owner_id
    )
    assert default == {
        "workspace_id": workspace_id,
        "idle_timeout_minutes": None,
        "absolute_timeout_hours": None,
        "max_active_sessions": None,
        "revision": 0,
        "created_at": None,
        "updated_at": None,
    }
    with pytest.raises(AuthAuthorizationError):
        store.get_workspace_session_policy(
            workspace_id=workspace_id,
            actor_user_id=viewer["user"]["user_id"],
        )

    created = store.set_workspace_session_policy(
        workspace_id=workspace_id,
        idle_timeout_minutes=30,
        absolute_timeout_hours=24,
        max_active_sessions=3,
        expected_revision=0,
        actor_user_id=owner_id,
    )
    assert created["revision"] == 1
    with pytest.raises(AuthConflictError, match="revision"):
        store.set_workspace_session_policy(
            workspace_id=workspace_id,
            idle_timeout_minutes=None,
            absolute_timeout_hours=None,
            max_active_sessions=None,
            expected_revision=0,
            actor_user_id=owner_id,
        )


def test_idle_timeout_revokes_session_on_first_use(identity):
    store, owner, clock = identity
    workspace_id = owner["workspace"]["workspace_id"]
    store.set_workspace_session_policy(
        workspace_id=workspace_id,
        idle_timeout_minutes=5,
        absolute_timeout_hours=None,
        max_active_sessions=None,
        expected_revision=0,
        actor_user_id=owner["user"]["user_id"],
    )
    token = owner["access_token"]
    clock.value += 5 * 60

    assert not store.session_is_active(
        session_id=owner["session"]["session_id"],
        user_id=owner["user"]["user_id"],
        workspace_id=workspace_id,
    )
    with pytest.raises(AuthAuthenticationError, match="no longer active"):
        store.link_oidc_identity_from_session(
            session_id=owner["session"]["session_id"],
            user_id=owner["user"]["user_id"],
            issuer="https://id.example.com",
            subject="owner-subject",
            email=owner["user"]["email"],
            email_verified=True,
        )
    with pytest.raises(AuthAuthenticationError, match="expired"):
        store.authenticate_session(token, workspace_id=workspace_id)
    assert store.list_sessions(owner["user"]["user_id"]) == []


def test_absolute_timeout_caps_new_and_existing_session_expiry(identity):
    store, owner, clock = identity
    workspace_id = owner["workspace"]["workspace_id"]
    original_expiry = store._conn.execute(
        "SELECT expires_at FROM auth_sessions WHERE session_id=?",
        (owner["session"]["session_id"],),
    ).fetchone()[0]
    policy = store.set_workspace_session_policy(
        workspace_id=workspace_id,
        idle_timeout_minutes=None,
        absolute_timeout_hours=1,
        max_active_sessions=None,
        expected_revision=0,
        actor_user_id=owner["user"]["user_id"],
    )
    assert policy["absolute_timeout_hours"] == 1
    shortened = store._conn.execute(
        "SELECT expires_at FROM auth_sessions WHERE session_id=?",
        (owner["session"]["session_id"],),
    ).fetchone()[0]
    assert shortened == clock.value + 3600
    assert shortened < original_expiry

    clock.value += 1
    login = store.login("owner@example.com", PASSWORD, workspace_id)
    row = store._conn.execute(
        "SELECT created_at,expires_at FROM auth_sessions WHERE session_id=?",
        (login["session"]["session_id"],),
    ).fetchone()
    assert row[1] == row[0] + 3600


def test_concurrent_session_limit_is_per_user_and_keeps_newest(identity):
    store, owner, clock = identity
    workspace_id = owner["workspace"]["workspace_id"]
    owner_id = owner["user"]["user_id"]
    second = store.register("second@example.com", PASSWORD, "Second")
    store.add_member(workspace_id, second["user"]["user_id"], "viewer", owner_id)
    store.set_workspace_session_policy(
        workspace_id=workspace_id,
        idle_timeout_minutes=None,
        absolute_timeout_hours=None,
        max_active_sessions=2,
        expected_revision=0,
        actor_user_id=owner_id,
    )

    tokens = [owner["access_token"]]
    for _ in range(2):
        clock.value += 1
        tokens.append(
            store.login("owner@example.com", PASSWORD, workspace_id)["access_token"]
        )
    clock.value += 1
    second_token = store.login("second@example.com", PASSWORD, workspace_id)[
        "access_token"
    ]

    with pytest.raises(AuthAuthenticationError):
        store.authenticate_session(tokens[0], workspace_id=workspace_id)
    store.authenticate_session(tokens[1], workspace_id=workspace_id)
    store.authenticate_session(tokens[2], workspace_id=workspace_id)
    store.authenticate_session(second_token, workspace_id=workspace_id)
    active = store._conn.execute(
        "SELECT user_id,COUNT(*) FROM auth_sessions WHERE active_workspace_id=? "
        "AND revoked_at IS NULL AND expires_at>? GROUP BY user_id",
        (workspace_id, clock.value),
    ).fetchall()
    assert sorted(int(row[1]) for row in active) == [1, 2]


def test_policy_tightening_revokes_existing_overflow_in_same_transaction(identity):
    store, owner, clock = identity
    workspace_id = owner["workspace"]["workspace_id"]
    owner_id = owner["user"]["user_id"]
    tokens = [owner["access_token"]]
    for _ in range(2):
        clock.value += 1
        tokens.append(
            store.login("owner@example.com", PASSWORD, workspace_id)["access_token"]
        )

    store.set_workspace_session_policy(
        workspace_id=workspace_id,
        idle_timeout_minutes=None,
        absolute_timeout_hours=None,
        max_active_sessions=1,
        expected_revision=0,
        actor_user_id=owner_id,
    )

    with pytest.raises(AuthAuthenticationError):
        store.authenticate_session(tokens[0], workspace_id=workspace_id)
    with pytest.raises(AuthAuthenticationError):
        store.authenticate_session(tokens[1], workspace_id=workspace_id)
    assert (
        store.authenticate_session(tokens[2], workspace_id=workspace_id).user_id
        == owner_id
    )


def test_switching_workspace_enforces_target_concurrency_limit(identity):
    store, owner, clock = identity
    owner_id = owner["user"]["user_id"]
    target = store.create_workspace(owner_id, "Target")["workspace_id"]
    store.set_workspace_session_policy(
        workspace_id=target,
        idle_timeout_minutes=None,
        absolute_timeout_hours=None,
        max_active_sessions=1,
        expected_revision=0,
        actor_user_id=owner_id,
    )
    first = owner["access_token"]
    clock.value += 1
    second = store.login("owner@example.com", PASSWORD)["access_token"]

    store.authenticate_session(first, workspace_id=target)
    clock.value += 1
    store.authenticate_session(second, workspace_id=target)
    with pytest.raises(AuthAuthenticationError):
        store.authenticate_session(first, workspace_id=target)
    assert (
        store.authenticate_session(second, workspace_id=target).workspace_id == target
    )


def test_workspace_session_inventory_is_paginated_scoped_and_secret_free(identity):
    store, owner, clock = identity
    workspace_id = owner["workspace"]["workspace_id"]
    owner_id = owner["user"]["user_id"]
    admin = store.register("admin@example.com", PASSWORD, "Admin")
    viewer = store.register("inventory-viewer@example.com", PASSWORD, "Viewer")
    store.add_member(workspace_id, admin["user"]["user_id"], "admin", owner_id)
    store.add_member(workspace_id, viewer["user"]["user_id"], "viewer", owner_id)
    clock.value += 1
    admin_login = store.login("admin@example.com", PASSWORD, workspace_id)
    clock.value += 1
    viewer_login = store.login("inventory-viewer@example.com", PASSWORD, workspace_id)

    first = store.list_workspace_sessions(
        workspace_id=workspace_id,
        actor_user_id=admin["user"]["user_id"],
        limit=2,
    )
    assert first["total"] == 3
    assert len(first["sessions"]) == 2
    assert first["next_before_session_id"] is not None
    second = store.list_workspace_sessions(
        workspace_id=workspace_id,
        actor_user_id=admin["user"]["user_id"],
        limit=2,
        before_session_id=first["next_before_session_id"],
    )
    assert len(second["sessions"]) == 1
    serialized = repr([*first["sessions"], *second["sessions"]])
    for secret in (
        owner["access_token"],
        admin_login["access_token"],
        viewer_login["access_token"],
        "token_hash",
    ):
        assert secret not in serialized

    with pytest.raises(AuthNotFoundError):
        store.list_workspace_sessions(
            workspace_id=workspace_id,
            actor_user_id=admin["user"]["user_id"],
            before_session_id=viewer["session"]["session_id"],
        )


def test_admin_cannot_revoke_owner_session_but_owner_can_revoke_any(identity):
    store, owner, clock = identity
    workspace_id = owner["workspace"]["workspace_id"]
    owner_id = owner["user"]["user_id"]
    admin = store.register("revoke-admin@example.com", PASSWORD, "Admin")
    viewer = store.register("revoke-viewer@example.com", PASSWORD, "Viewer")
    store.add_member(workspace_id, admin["user"]["user_id"], "admin", owner_id)
    store.add_member(workspace_id, viewer["user"]["user_id"], "viewer", owner_id)
    clock.value += 1
    viewer_login = store.login("revoke-viewer@example.com", PASSWORD, workspace_id)

    with pytest.raises(AuthAuthorizationError, match="owner"):
        store.revoke_workspace_session(
            workspace_id=workspace_id,
            session_id=owner["session"]["session_id"],
            actor_user_id=admin["user"]["user_id"],
        )
    assert store.revoke_workspace_session(
        workspace_id=workspace_id,
        session_id=viewer_login["session"]["session_id"],
        actor_user_id=admin["user"]["user_id"],
    )
    with pytest.raises(AuthAuthenticationError):
        store.authenticate_session(
            viewer_login["access_token"], workspace_id=workspace_id
        )

    inactive = store.list_workspace_sessions(
        workspace_id=workspace_id,
        actor_user_id=owner_id,
        include_inactive=True,
    )
    revoked = next(
        item
        for item in inactive["sessions"]
        if item["session_id"] == viewer_login["session"]["session_id"]
    )
    assert revoked["status"] == "revoked"
    assert revoked["revoked_at"] is not None
