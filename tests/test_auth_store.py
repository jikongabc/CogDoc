import hashlib
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

import cogdoc.api.auth_store as auth_module
from cogdoc.api.auth_store import (
    AuthAuthenticationError,
    AuthAuthorizationError,
    AuthConflictError,
    AuthInviteError,
    AuthLockedError,
    AuthNotFoundError,
    AuthStore,
    AuthStoreError,
    AuthValidationError,
)
from cogdoc.api.tenancy import Role


PASSWORD = "correct horse battery"
NEW_PASSWORD = "new correct horse battery"


class Clock:
    def __init__(self, value=1_800_000_000.0):
        self.value = value

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def store(tmp_path, clock):
    result = AuthStore(
        str(tmp_path / "state.db"),
        scrypt_n=1 << 10,
        session_ttl_seconds=100,
        invite_ttl_seconds=50,
        max_failed_logins=3,
        lockout_seconds=20,
        clock=clock,
    )
    yield result
    result.close()


def register(store, email="alice@example.com", name="Alice"):
    return store.register(email, PASSWORD, name)


def test_register_is_atomic_normalizes_email_and_never_returns_secrets(store):
    result = register(store, "  AℒICE@EXAMPLE.COM  ")

    assert result["user"]["email"] == "alice@example.com"
    assert result["user"]["user_id"].startswith("usr_")
    assert result["workspace"]["workspace_id"].startswith("wsp_")
    assert result["workspace"]["role"] == "owner"
    assert result["session"]["session_id"].startswith("ses_")
    assert set(result["user"]).isdisjoint(
        {"password", "password_hash", "failed_login_count", "locked_until"}
    )

    row = store._conn.execute(
        "SELECT password_hash FROM auth_users WHERE user_id=?",
        (result["user"]["user_id"],),
    ).fetchone()
    assert row[0].startswith("scrypt$v=1$n=1024$r=8$p=1$dklen=32$")
    assert PASSWORD not in row[0]
    assert store._conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert store._conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    with pytest.raises(AuthConflictError):
        register(store, "alice@example.com")
    assert store._conn.execute("SELECT COUNT(*) FROM auth_users").fetchone()[0] == 1
    assert (
        store._conn.execute("SELECT COUNT(*) FROM auth_workspaces").fetchone()[0] == 1
    )
    assert (
        store._conn.execute("SELECT COUNT(*) FROM auth_memberships").fetchone()[0] == 1
    )


def test_custom_workspace_roles_can_be_created_assigned_and_are_usage_protected(store):
    owner = register(store)
    member_user = register(store, "member@example.com", "Member")["user"]
    workspace_id = owner["workspace"]["workspace_id"]
    owner_id = owner["user"]["user_id"]
    member = store.add_member(workspace_id, member_user["user_id"], "viewer", owner_id)

    role = store.create_workspace_role(
        workspace_id,
        "Finance",
        "viewer",
        owner_id,
        "Finance documents",
    )
    assert role["system"] is False
    assert role["base_role"] == "viewer"

    assigned = store.update_member_role(
        workspace_id,
        member["member_id"],
        None,
        owner_id,
        role_id=role["role_id"],
    )
    assert assigned["role"] == "viewer"
    assert assigned["role_id"] == role["role_id"]
    assert assigned["role_name"] == "Finance"

    with pytest.raises(AuthConflictError):
        store.delete_workspace_role(workspace_id, role["role_id"], owner_id)

    restored = store.update_member_role(
        workspace_id,
        member["member_id"],
        None,
        owner_id,
        role_id="viewer",
    )
    assert restored["role_id"] == "viewer"
    assert restored["role_name"] == "Member"
    assert store.delete_workspace_role(workspace_id, role["role_id"], owner_id)

def test_password_contract_and_unknown_user_performs_dummy_verify(store, monkeypatch):
    with pytest.raises(AuthValidationError):
        store.register("short@example.com", "too-short", "Short")

    seen = []
    original = auth_module._verify_password

    def capture(password, encoded):
        seen.append(encoded)
        return original(password, encoded)

    monkeypatch.setattr(auth_module, "_verify_password", capture)
    with pytest.raises(AuthAuthenticationError):
        store.login("missing@example.com", "plausible password")
    assert seen == [store._dummy_password_hash]


def test_tokens_are_256_bit_and_database_contains_only_sha256(store):
    registered = register(store)
    token = registered["access_token"]
    digest = hashlib.sha256(token.encode()).hexdigest()
    stored = store._conn.execute(
        "SELECT token_hash FROM auth_sessions WHERE session_id=?",
        (registered["session"]["session_id"],),
    ).fetchone()[0]

    assert len(token.removeprefix("cgs_")) == 43
    assert stored == digest
    assert token not in "\n".join(
        str(item) for row in store._conn.iterdump() for item in row.splitlines()
    )


def test_login_failure_counter_locks_then_unlocks_on_timer(store, clock):
    registered = register(store)
    for _ in range(2):
        with pytest.raises(AuthAuthenticationError, match="invalid"):
            store.login("alice@example.com", "incorrect but long password")
    with pytest.raises(AuthLockedError):
        store.login("alice@example.com", "incorrect but long password")
    with pytest.raises(AuthLockedError):
        store.login("alice@example.com", PASSWORD)

    clock.advance(21)
    logged_in = store.login("alice@example.com", PASSWORD)
    assert logged_in["user"] == registered["user"] | {
        "updated_at": logged_in["user"]["updated_at"]
    }
    failures = store._conn.execute(
        "SELECT failed_login_count,locked_until FROM auth_users WHERE user_id=?",
        (registered["user"]["user_id"],),
    ).fetchone()
    assert failures == (0, None)


def test_auth_context_uses_active_or_requested_member_workspace(store):
    alice = register(store)
    bob = register(store, "bob@example.com", "Bob")
    team = store.create_workspace(alice["user"]["user_id"], "Research Team")
    store.add_member(
        team["workspace_id"],
        bob["user"]["user_id"],
        "reviewer",
        alice["user"]["user_id"],
    )

    personal = store.authenticate_session(bob["access_token"])
    assert personal.workspace_id == bob["workspace"]["workspace_id"]
    assert personal.principal.role is Role.OWNER

    switched = store.authenticate_session(bob["access_token"], team["workspace_id"])
    team_membership = store.membership(team["workspace_id"], bob["user"]["user_id"])
    assert team_membership is not None
    assert switched.workspace_id == team["workspace_id"]
    assert switched.principal.tenant_id == team["workspace_id"]
    assert switched.principal.subject_id == bob["user"]["user_id"]
    assert switched.principal.role is Role.REVIEWER
    assert switched.principal.membership_id == team_membership["member_id"]
    assert (
        store.authenticate_session(bob["access_token"]).workspace_id
        == team["workspace_id"]
    )

    foreign = store.create_workspace(alice["user"]["user_id"], "Foreign")
    with pytest.raises(AuthAuthorizationError):
        store.authenticate_session(bob["access_token"], foreign["workspace_id"])


def test_session_activity_writes_are_throttled_but_membership_is_rechecked(
    store, clock
):
    owner = register(store)
    member = register(store, "member@example.com", "Member")
    workspace = store.create_workspace(owner["user"]["user_id"], "Shared")
    store.add_member(
        workspace["workspace_id"],
        member["user"]["user_id"],
        "viewer",
        owner["user"]["user_id"],
    )
    store.authenticate_session(member["access_token"], workspace["workspace_id"])
    session_id = member["session"]["session_id"]
    initial = store._conn.execute(
        "SELECT last_seen_at FROM auth_sessions WHERE session_id=?", (session_id,)
    ).fetchone()[0]

    clock.advance(30)
    first = store.authenticate_session(member["access_token"])
    unchanged = store._conn.execute(
        "SELECT last_seen_at FROM auth_sessions WHERE session_id=?", (session_id,)
    ).fetchone()[0]
    assert unchanged == initial
    assert first.principal.role is Role.VIEWER

    membership = store.membership(workspace["workspace_id"], member["user"]["user_id"])
    assert membership is not None
    store.update_member_role(
        workspace["workspace_id"],
        membership["member_id"],
        "editor",
        owner["user"]["user_id"],
    )
    assert (
        store.authenticate_session(member["access_token"]).principal.role is Role.EDITOR
    )
    assert (
        store._conn.execute(
            "SELECT last_seen_at FROM auth_sessions WHERE session_id=?", (session_id,)
        ).fetchone()[0]
        == initial
    )

    clock.advance(31)
    touched = store.authenticate_session(member["access_token"])
    persisted = store._conn.execute(
        "SELECT last_seen_at FROM auth_sessions WHERE session_id=?", (session_id,)
    ).fetchone()[0]
    assert persisted == clock.value
    assert touched.session["last_seen_at"] != first.session["last_seen_at"]


def test_session_absolute_expiry_and_restart(tmp_path, clock):
    path = tmp_path / "state.db"
    first = AuthStore(str(path), scrypt_n=1 << 10, session_ttl_seconds=10, clock=clock)
    registered = register(first)
    first.close()

    second = AuthStore(str(path), scrypt_n=1 << 10, session_ttl_seconds=10, clock=clock)
    context = second.authenticate_session(registered["access_token"])
    assert context.user_id == registered["user"]["user_id"]
    clock.advance(11)
    with pytest.raises(AuthAuthenticationError, match="expired"):
        second.authenticate_session(registered["access_token"])
    second.close()


def test_session_listing_single_and_all_logout(store):
    registered = register(store)
    second = store.login("alice@example.com", PASSWORD)
    third = store.login("alice@example.com", PASSWORD)
    user_id = registered["user"]["user_id"]

    rows = store.list_sessions(user_id, second["access_token"])
    assert len(rows) == 3
    assert [row["session_id"] for row in rows if row["current"]] == [
        second["session"]["session_id"]
    ]
    assert store.revoke_session(user_id, third["session"]["session_id"])
    with pytest.raises(AuthAuthenticationError):
        store.authenticate_session(third["access_token"])

    assert store.logout_all(user_id, except_token=second["access_token"]) == 1
    assert store.authenticate_session(second["access_token"]).user_id == user_id
    with pytest.raises(AuthAuthenticationError):
        store.authenticate_session(registered["access_token"])
    assert store.logout(second["access_token"])
    assert store.logout(second["access_token"]) is False


def test_password_change_revokes_other_sessions_and_preserves_current(store):
    registered = register(store)
    current = store.login("alice@example.com", PASSWORD)
    user_id = registered["user"]["user_id"]

    assert (
        store.change_password(
            user_id,
            PASSWORD,
            NEW_PASSWORD,
            current_token=current["access_token"],
        )
        == 1
    )
    assert store.authenticate_session(current["access_token"]).user_id == user_id
    with pytest.raises(AuthAuthenticationError):
        store.authenticate_session(registered["access_token"])
    with pytest.raises(AuthAuthenticationError):
        store.login("alice@example.com", PASSWORD)
    assert store.login("alice@example.com", NEW_PASSWORD)["user"]["user_id"] == user_id


def test_password_change_supports_unicode_passwords(store):
    current_password = "当前密码是一条安全短语123"
    new_password = "新密码也是一条安全短语456"
    registered = store.register("unicode@example.com", current_password, "Unicode User")
    current = store.login("unicode@example.com", current_password)
    user_id = registered["user"]["user_id"]

    assert (
        store.change_password(
            user_id,
            current_password,
            new_password,
            current_token=current["access_token"],
        )
        == 1
    )
    with pytest.raises(AuthAuthenticationError):
        store.login("unicode@example.com", current_password)
    assert (
        store.login("unicode@example.com", new_password)["user"]["user_id"] == user_id
    )


def test_workspace_crud_requires_owner_and_empty_non_personal_store(store):
    alice = register(store)
    owner_id = alice["user"]["user_id"]
    team = store.create_workspace(owner_id, "  Team   One  ")
    assert team["name"] == "Team One"
    renamed = store.rename_workspace(
        team["workspace_id"], "Team Two", owner_id, expected_revision=0
    )
    assert renamed["revision"] == 1
    with pytest.raises(AuthConflictError, match="revision"):
        store.rename_workspace(
            team["workspace_id"], "Stale", owner_id, expected_revision=0
        )
    assert store.delete_workspace(team["workspace_id"], owner_id)
    with pytest.raises(AuthNotFoundError):
        store.get_workspace(team["workspace_id"])
    with pytest.raises(AuthConflictError, match="personal"):
        store.delete_workspace(alice["workspace"]["workspace_id"], owner_id)


def test_owner_and_admin_member_management_boundaries(store):
    owner = register(store)
    admin = register(store, "admin@example.com", "Admin")
    viewer = register(store, "viewer@example.com", "Viewer")
    workspace_id = owner["workspace"]["workspace_id"]
    owner_id = owner["user"]["user_id"]
    admin_id = admin["user"]["user_id"]
    viewer_id = viewer["user"]["user_id"]
    store.add_member(workspace_id, admin_id, "admin", owner_id)
    store.add_member(workspace_id, viewer_id, "viewer", admin_id)
    members = {member["user_id"]: member for member in store.list_members(workspace_id)}

    # Public routes address membership resources by member_id.  Direct store
    # callers using user_id remain compatible as well.
    updated = store.update_member_role(
        workspace_id, members[viewer_id]["member_id"], "editor", admin_id
    )
    assert updated["role"] == "editor"
    with pytest.raises(AuthValidationError, match="owner"):
        store.update_member_role(workspace_id, viewer_id, "owner", admin_id)
    with pytest.raises(AuthAuthorizationError, match="owner"):
        store.update_member_role(
            workspace_id, members[owner_id]["member_id"], "viewer", owner_id
        )
    with pytest.raises(AuthAuthorizationError, match="owner"):
        store.remove_member(workspace_id, owner_id, admin_id)
    assert store.remove_member(workspace_id, members[viewer_id]["member_id"], admin_id)


def test_workspace_with_other_members_cannot_be_deleted(store):
    owner = register(store)
    member = register(store, "member@example.com", "Member")
    team = store.create_workspace(owner["user"]["user_id"], "Team")
    store.add_member(
        team["workspace_id"],
        member["user"]["user_id"],
        "viewer",
        owner["user"]["user_id"],
    )
    with pytest.raises(AuthConflictError, match="other members"):
        store.delete_workspace(team["workspace_id"], owner["user"]["user_id"])


def test_invite_is_email_bound_one_time_and_never_lists_token(store):
    owner = register(store)
    member = register(store, "member@example.com", "Member")
    workspace_id = owner["workspace"]["workspace_id"]
    created = store.create_invite(
        workspace_id, " MEMBER@EXAMPLE.COM ", "reviewer", owner["user"]["user_id"]
    )
    token = created["invite_token"]
    digest = hashlib.sha256(token.encode()).hexdigest()
    persisted = store._conn.execute(
        "SELECT token_hash FROM auth_invites WHERE invite_id=?",
        (created["invite"]["invite_id"],),
    ).fetchone()[0]
    assert persisted == digest
    assert token not in repr(store.list_invites(workspace_id, owner["user"]["user_id"]))

    with pytest.raises(AuthAuthorizationError, match="email"):
        store.accept_invite(token, owner["user"]["user_id"])
    accepted = store.accept_invite(token, member["user"]["user_id"])
    assert accepted["member"]["role"] == "reviewer"
    assert accepted["workspace"]["workspace_id"] == workspace_id
    assert "access_token" not in accepted
    with pytest.raises(AuthInviteError, match="consumed"):
        store.accept_invite(token, member["user"]["user_id"])


def test_expired_and_revoked_invites_fail_closed(store, clock):
    owner = register(store)
    member = register(store, "member@example.com", "Member")
    workspace_id = owner["workspace"]["workspace_id"]
    owner_id = owner["user"]["user_id"]

    expired = store.create_invite(
        workspace_id, member["user"]["email"], "viewer", owner_id, ttl_seconds=2
    )
    clock.advance(3)
    assert store.list_invites(workspace_id, owner_id)[0]["status"] == "expired"
    with pytest.raises(AuthInviteError, match="expired"):
        store.accept_invite(expired["invite_token"], member["user"]["user_id"])

    fresh = store.create_invite(
        workspace_id, member["user"]["email"], "viewer", owner_id
    )
    assert store.revoke_invite(workspace_id, fresh["invite"]["invite_id"], owner_id)
    with pytest.raises(AuthInviteError, match="revoked"):
        store.accept_invite(fresh["invite_token"], member["user"]["user_id"])


def test_anonymous_invite_accept_verifies_existing_account_and_issues_session(store):
    owner = register(store)
    member = register(store, "member@example.com", "Member")
    created = store.create_invite(
        owner["workspace"]["workspace_id"],
        member["user"]["email"],
        "editor",
        owner["user"]["user_id"],
    )
    with pytest.raises(AuthAuthorizationError, match="email"):
        store.accept_invite(
            created["invite_token"],
            email="wrong@example.com",
            password=PASSWORD,
        )
    with pytest.raises(AuthAuthenticationError):
        store.accept_invite(
            created["invite_token"],
            email="member@example.com",
            password="incorrect but long password",
        )

    accepted = store.accept_invite(
        created["invite_token"],
        email=" MEMBER@EXAMPLE.COM ",
        password=PASSWORD,
    )
    assert accepted["member"]["role"] == "editor"
    assert accepted["user"]["user_id"] == member["user"]["user_id"]
    context = store.authenticate_session(accepted["access_token"])
    assert context.workspace_id == owner["workspace"]["workspace_id"]
    assert context.principal.role is Role.EDITOR


def test_anonymous_invite_accept_atomically_registers_personal_workspace(store):
    owner = register(store)
    created = store.create_invite(
        owner["workspace"]["workspace_id"],
        "new@example.com",
        "viewer",
        owner["user"]["user_id"],
    )
    accepted = store.accept_invite(
        created["invite_token"],
        email="NEW@example.com",
        password="brand new secure password",
        display_name="New User",
    )

    assert accepted["user"]["email"] == "new@example.com"
    assert accepted["member"]["role"] == "viewer"
    workspaces = store.list_workspaces(accepted["user"]["user_id"])
    assert {workspace["role"] for workspace in workspaces} == {"owner", "viewer"}
    assert (
        store.authenticate_session(accepted["access_token"]).workspace_id
        == owner["workspace"]["workspace_id"]
    )


def test_concurrent_same_email_registration_has_exactly_one_commit(store):
    def attempt(index):
        try:
            return register(store, "Concurrent@Example.com", f"User {index}")
        except AuthConflictError:
            return None

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(attempt, range(8)))

    assert sum(result is not None for result in results) == 1
    assert (
        store._conn.execute(
            "SELECT COUNT(*) FROM auth_users WHERE email='concurrent@example.com'"
        ).fetchone()[0]
        == 1
    )
    assert (
        store._conn.execute(
            "SELECT COUNT(*) FROM auth_memberships m JOIN auth_users u ON u.user_id=m.user_id "
            "WHERE u.email='concurrent@example.com'"
        ).fetchone()[0]
        == 1
    )


def test_concurrent_invite_acceptance_creates_one_membership(store):
    owner = register(store)
    member = register(store, "member@example.com", "Member")
    created = store.create_invite(
        owner["workspace"]["workspace_id"],
        member["user"]["email"],
        "viewer",
        owner["user"]["user_id"],
    )

    def accept(_):
        try:
            return store.accept_invite(
                created["invite_token"], member["user"]["user_id"]
            )
        except AuthInviteError:
            return None

    with ThreadPoolExecutor(max_workers=6) as executor:
        results = list(executor.map(accept, range(6)))
    assert sum(result is not None for result in results) == 1
    assert (
        sum(
            row["user_id"] == member["user"]["user_id"]
            for row in store.list_members(owner["workspace"]["workspace_id"])
        )
        == 1
    )


def test_close_is_idempotent_and_operations_fail(store):
    register(store)
    store.close()
    store.close()
    with pytest.raises(AuthStoreError, match="closed"):
        store.list_workspaces("usr_any")


def test_foreign_keys_reject_orphan_rows(store):
    with pytest.raises(sqlite3.IntegrityError):
        store._conn.execute(
            "INSERT INTO auth_memberships(member_id,workspace_id,user_id,role,revision,"
            "joined_at,updated_at) VALUES('mem_x','wsp_missing','usr_missing','viewer',0,1,1)"
        )
