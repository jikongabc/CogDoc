from __future__ import annotations

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest
from types import SimpleNamespace

from cogdoc.api.access_control import AccessControlMiddleware, TokenBucketRateLimiter
from cogdoc.api.auth_store import AuthNotFoundError, AuthStore, AuthStoreError
from cogdoc.api.resource_access import AccessMode, ResourceAccessStore
from cogdoc.api.routes.auth import router as auth_router
from cogdoc.api.routes.documents import _live_session_authorization_guard
from cogdoc.api.tenant_scope import KnowledgeBaseScope
from cogdoc.api.tenancy import Permission, Principal, Role


PASSWORD = "correct horse battery"
NEW_PASSWORD = "a newer correct horse battery"


class _AllowAll:
    def allow(self, _identity: str) -> bool:
        return True


class _DenyAll:
    def allow(self, _identity: str) -> bool:
        return False


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def auth_app(tmp_path):
    store = AuthStore(str(tmp_path / "auth.db"), scrypt_n=1 << 10)
    access_store = ResourceAccessStore(tmp_path / "resource-access.db")
    app = FastAPI()
    app.state.auth_store = store
    app.state.resource_access_store = access_store
    app.state.auth_public_rate_limiter = _AllowAll()
    app.include_router(auth_router)
    yield app, store
    access_store.close()
    store.close()


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _access_token(payload: dict) -> str:
    return payload["access_token"]


def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")


class _CountingAuthStore(AuthStore):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.authentication_calls = 0

    def authenticate_session(self, token: str, workspace_id: str | None = None):
        self.authentication_calls += 1
        return super().authenticate_session(token, workspace_id)


@pytest.mark.anyio
async def test_register_login_contract_is_strict_safe_and_non_enumerating(auth_app):
    app, _store = auth_app
    async with _client(app) as client:
        malformed = await client.post(
            "/v1/auth/register",
            json={
                "email": 42,
                "password": PASSWORD,
                "display_name": "Alice",
                "unexpected": True,
            },
        )
        created = await client.post(
            "/v1/auth/register",
            json={
                "email": "  ALICE@EXAMPLE.COM ",
                "password": PASSWORD,
                "display_name": "  Alice   Example ",
            },
        )
        duplicate = await client.post(
            "/v1/auth/register",
            json={
                "email": "alice@example.com",
                "password": PASSWORD,
                "display_name": "Someone Else",
            },
        )
        unknown = await client.post(
            "/v1/auth/login",
            json={"email": "missing@example.com", "password": "wrong password"},
        )
        wrong = await client.post(
            "/v1/auth/login",
            json={"email": "alice@example.com", "password": "wrong password"},
        )

    assert malformed.status_code == 422
    assert created.status_code == 201
    body = created.json()
    assert body["schema_version"] == "v1"
    assert body["user"]["email"] == "alice@example.com"
    assert body["user"]["display_name"] == "Alice Example"
    assert body["workspace"]["role"] == "owner"
    assert body["token_type"] == "bearer"
    serialized = repr(body)
    assert PASSWORD not in serialized
    assert "password_hash" not in serialized
    assert "key_fingerprint" not in serialized
    assert duplicate.status_code == 409
    assert duplicate.json()["error_code"] == "AUTH_CONFLICT"
    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json() == wrong.json()
    assert unknown.json()["error_code"] == "UNAUTHORIZED"


@pytest.mark.anyio
async def test_me_session_revocation_and_password_change(auth_app):
    app, store = auth_app
    first = store.register("alice@example.com", PASSWORD, "Alice")
    first_token = first["access_token"]
    async with _client(app) as client:
        logged_in = await client.post(
            "/v1/auth/login",
            json={"email": "alice@example.com", "password": PASSWORD},
        )
        second_token = _access_token(logged_in.json())
        me = await client.get("/v1/auth/me", headers=_headers(second_token))
        sessions = await client.get("/v1/auth/sessions", headers=_headers(second_token))
        old_session_id = next(
            item["session_id"]
            for item in sessions.json()["sessions"]
            if not item["current"]
        )
        revoked = await client.delete(
            f"/v1/auth/sessions/{old_session_id}", headers=_headers(second_token)
        )
        missing = await client.delete(
            "/v1/auth/sessions/ses_not-owned", headers=_headers(second_token)
        )
        changed = await client.post(
            "/v1/auth/change-password",
            headers=_headers(second_token),
            json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
        )
        old_after = await client.get("/v1/auth/me", headers=_headers(first_token))
        current_after = await client.get("/v1/auth/me", headers=_headers(second_token))
        logged_out = await client.post(
            "/v1/auth/logout", headers=_headers(second_token)
        )
        after_logout = await client.get("/v1/auth/me", headers=_headers(second_token))

    assert logged_in.status_code == 200
    assert me.status_code == 200
    assert me.json()["user"]["email"] == "alice@example.com"
    assert len(sessions.json()["sessions"]) == 2
    assert sum(item["current"] for item in sessions.json()["sessions"]) == 1
    assert "access_token" not in repr(sessions.json())
    assert revoked.status_code == 204
    assert missing.status_code == 404
    assert missing.json()["error_code"] == "SESSION_NOT_FOUND"
    assert changed.status_code == 204
    assert old_after.status_code == 401
    assert current_after.status_code == 200
    assert logged_out.status_code == 204
    assert after_logout.status_code == 401


@pytest.mark.anyio
async def test_logout_all_revokes_every_session(auth_app):
    app, store = auth_app
    first = store.register("alice@example.com", PASSWORD, "Alice")
    second = store.login("alice@example.com", PASSWORD)
    async with _client(app) as client:
        response = await client.post(
            "/v1/auth/logout-all", headers=_headers(second["access_token"])
        )
        first_after = await client.get(
            "/v1/auth/me", headers=_headers(first["access_token"])
        )
        second_after = await client.get(
            "/v1/auth/me", headers=_headers(second["access_token"])
        )

    assert response.status_code == 204
    assert first_after.status_code == second_after.status_code == 401


@pytest.mark.anyio
async def test_auth_routes_reuse_middleware_context_but_revalidate_workspace(tmp_path):
    store = _CountingAuthStore(str(tmp_path / "counting.db"), scrypt_n=1 << 10)
    registered = store.register("alice@example.com", PASSWORD, "Alice")
    second_workspace = store.create_workspace(
        registered["user"]["user_id"], "Second Workspace"
    )
    app = FastAPI()
    app.state.auth_store = store
    app.state.auth_public_rate_limiter = _AllowAll()
    app.include_router(auth_router)
    app.add_middleware(
        AccessControlMiddleware,
        api_keys=set(),
        principals=None,
        rate_limiter=TokenBucketRateLimiter(capacity=0, refill_per_second=0.0),
        auth_store=store,
    )
    try:
        store.authentication_calls = 0
        async with _client(app) as client:
            me = await client.get(
                "/v1/auth/me", headers=_headers(registered["access_token"])
            )
        assert me.status_code == 200
        assert store.authentication_calls == 1

        store.authentication_calls = 0
        async with _client(app) as client:
            switched = await client.post(
                f"/v1/workspaces/{second_workspace['workspace_id']}/switch",
                headers=_headers(registered["access_token"]),
            )
        assert switched.status_code == 200
        assert (
            switched.json()["workspace"]["workspace_id"]
            == second_workspace["workspace_id"]
        )
        # One middleware authentication plus one explicit target-workspace check.
        assert store.authentication_calls == 2
    finally:
        store.close()


@pytest.mark.anyio
async def test_workspace_routes_support_one_argument_session_provider(
    auth_app, monkeypatch
):
    app, store = auth_app
    owner = store.register("legacy-owner@example.com", PASSWORD, "Legacy Owner")
    outsider = store.register(
        "legacy-outsider@example.com", PASSWORD, "Legacy Outsider"
    )
    original_authenticate = store.authenticate_session

    def legacy_authenticate(_token: str):
        return original_authenticate(_token)

    monkeypatch.setattr(store, "authenticate_session", legacy_authenticate)
    async with _client(app) as client:
        active = await client.get(
            f"/v1/workspaces/{owner['workspace']['workspace_id']}",
            headers=_headers(owner["access_token"]),
        )
        foreign = await client.get(
            f"/v1/workspaces/{outsider['workspace']['workspace_id']}",
            headers=_headers(owner["access_token"]),
        )
        invalid = await client.get(
            f"/v1/workspaces/{owner['workspace']['workspace_id']}",
            headers=_headers("invalid-session-token"),
        )

    assert active.status_code == 200
    assert (
        active.json()["workspace"]["workspace_id"] == owner["workspace"]["workspace_id"]
    )
    assert foreign.status_code == 404
    assert foreign.json()["error_code"] == "WORKSPACE_NOT_FOUND"
    assert invalid.status_code == 401
    assert invalid.json()["error_code"] == "UNAUTHORIZED"


@pytest.mark.anyio
async def test_auth_route_maps_store_failure_to_service_unavailable(
    auth_app, monkeypatch
):
    app, store = auth_app

    def fail_authentication(*_args, **_kwargs):
        raise AuthStoreError("database unavailable")

    monkeypatch.setattr(store, "authenticate_session", fail_authentication)
    async with _client(app) as client:
        response = await client.get(
            "/v1/auth/me", headers=_headers("well-formed-but-unresolvable-token")
        )

    assert response.status_code == 503
    assert response.json()["error_code"] == "INTERNAL_ERROR"


@pytest.mark.anyio
async def test_workspace_role_checks_and_cross_workspace_opacity(auth_app):
    app, store = auth_app
    owner = store.register("owner@example.com", PASSWORD, "Owner")
    admin = store.register("admin@example.com", PASSWORD, "Admin")
    viewer = store.register("viewer@example.com", PASSWORD, "Viewer")
    outsider = store.register("outsider@example.com", PASSWORD, "Outsider")
    owner_token = owner["access_token"]

    async with _client(app) as client:
        created = await client.post(
            "/v1/workspaces",
            headers=_headers(owner_token),
            json={"name": "Product Team"},
        )
        workspace_id = created.json()["workspace"]["workspace_id"]
        store.add_member(
            workspace_id,
            admin["user"]["user_id"],
            "admin",
            owner["user"]["user_id"],
        )
        store.add_member(
            workspace_id,
            viewer["user"]["user_id"],
            "viewer",
            owner["user"]["user_id"],
        )

        outsider_get = await client.get(
            f"/v1/workspaces/{workspace_id}",
            headers=_headers(outsider["access_token"]),
        )
        missing_get = await client.get(
            "/v1/workspaces/wsp_definitely-missing",
            headers=_headers(outsider["access_token"]),
        )
        viewer_get = await client.get(
            f"/v1/workspaces/{workspace_id}",
            headers=_headers(viewer["access_token"]),
        )
        viewer_members = await client.get(
            f"/v1/workspaces/{workspace_id}/members",
            headers=_headers(viewer["access_token"]),
        )
        admin_members = await client.get(
            f"/v1/workspaces/{workspace_id}/members",
            headers=_headers(admin["access_token"]),
        )
        admin_rename = await client.patch(
            f"/v1/workspaces/{workspace_id}",
            headers=_headers(admin["access_token"]),
            json={"name": "Forbidden Rename", "expected_revision": 0},
        )
        owner_rename = await client.patch(
            f"/v1/workspaces/{workspace_id}",
            headers=_headers(owner_token),
            json={"name": "Renamed Team", "expected_revision": 0},
        )

    assert created.status_code == 201
    assert outsider_get.status_code == missing_get.status_code == 404
    assert outsider_get.json() == missing_get.json()
    assert viewer_get.status_code == 200
    assert viewer_get.json()["workspace"]["role"] == "viewer"
    assert viewer_members.status_code == 403
    assert viewer_members.json()["error_code"] == "FORBIDDEN"
    assert admin_members.status_code == 200
    assert len(admin_members.json()["members"]) == 3
    assert admin_rename.status_code == 403
    assert owner_rename.status_code == 200
    assert owner_rename.json()["workspace"]["revision"] == 1


@pytest.mark.anyio
async def test_workspace_list_switch_and_delete_lifecycle(auth_app):
    app, store = auth_app
    owner = store.register("owner@example.com", PASSWORD, "Owner")
    token = owner["access_token"]
    async with _client(app) as client:
        created = await client.post(
            "/v1/workspaces", headers=_headers(token), json={"name": "Temporary"}
        )
        workspace_id = created.json()["workspace"]["workspace_id"]
        listed = await client.get("/v1/workspaces", headers=_headers(token))
        got = await client.get(
            f"/v1/workspaces/{workspace_id}", headers=_headers(token)
        )
        switched = await client.post(
            f"/v1/workspaces/{workspace_id}/switch", headers=_headers(token)
        )
        deleted = await client.delete(
            f"/v1/workspaces/{workspace_id}", headers=_headers(token)
        )
        after = await client.get(
            f"/v1/workspaces/{workspace_id}", headers=_headers(token)
        )

    assert created.status_code == 201
    assert {row["workspace_id"] for row in listed.json()["workspaces"]} == {
        owner["workspace"]["workspace_id"],
        workspace_id,
    }
    assert got.status_code == 200
    assert switched.status_code == 200
    assert switched.json()["access_token"] == token
    assert switched.json()["workspace"]["workspace_id"] == workspace_id
    assert deleted.status_code == 204
    assert after.status_code == 404


@pytest.mark.anyio
async def test_member_and_invite_lifecycle_rechecks_route_authority(auth_app):
    app, store = auth_app
    owner = store.register("owner@example.com", PASSWORD, "Owner")
    invited = store.register("member@example.com", PASSWORD, "Member")
    workspace_id = owner["workspace"]["workspace_id"]

    async with _client(app) as client:
        created = await client.post(
            f"/v1/workspaces/{workspace_id}/invites",
            headers=_headers(owner["access_token"]),
            json={"email": " MEMBER@EXAMPLE.COM ", "role": "reviewer"},
        )
        invite_token = created.json()["invite_token"]
        listed = await client.get(
            f"/v1/workspaces/{workspace_id}/invites",
            headers=_headers(owner["access_token"]),
        )
        accepted = await client.post(
            "/v1/auth/invitations/accept",
            headers=_headers(invited["access_token"]),
            json={"token": invite_token},
        )
        members = await client.get(
            f"/v1/workspaces/{workspace_id}/members",
            headers=_headers(owner["access_token"]),
        )
        member = next(
            row
            for row in members.json()["members"]
            if row["user_id"] == invited["user"]["user_id"]
        )
        updated = await client.patch(
            f"/v1/workspaces/{workspace_id}/members/{member['member_id']}",
            headers=_headers(owner["access_token"]),
            json={"role": "editor", "expected_revision": 0},
        )
        removed = await client.delete(
            f"/v1/workspaces/{workspace_id}/members/{member['member_id']}",
            headers=_headers(owner["access_token"]),
        )
        removed_access = await client.get(
            f"/v1/workspaces/{workspace_id}",
            headers=_headers(invited["access_token"]),
        )
        revocable = await client.post(
            f"/v1/workspaces/{workspace_id}/invites",
            headers=_headers(owner["access_token"]),
            json={"email": "member@example.com", "role": "viewer"},
        )
        revoked = await client.delete(
            f"/v1/workspaces/{workspace_id}/invites/"
            f"{revocable.json()['invite']['invite_id']}",
            headers=_headers(owner["access_token"]),
        )
        revoked_accept = await client.post(
            "/v1/auth/invitations/accept",
            headers=_headers(invited["access_token"]),
            json={"token": revocable.json()["invite_token"]},
        )

    assert created.status_code == 201
    assert created.json()["invite"]["email"] == "member@example.com"
    assert invite_token not in repr(listed.json())
    assert accepted.status_code == 200
    assert accepted.json()["workspace"]["role"] == "reviewer"
    assert _access_token(accepted.json()) == invited["access_token"]
    assert updated.status_code == 200
    assert updated.json()["member"]["role"] == "editor"
    assert removed.status_code == 204
    assert removed_access.status_code == 404
    assert revoked.status_code == 204
    assert revoked_accept.status_code == 400
    assert revoked_accept.json()["error_code"] == "INVITE_INVALID"


@pytest.mark.anyio
async def test_member_removal_revokes_all_grants_before_reinvite(auth_app):
    app, store = auth_app
    access_store = app.state.resource_access_store
    owner = store.register("owner@example.com", PASSWORD, "Owner")
    member = store.register("member@example.com", PASSWORD, "Member")
    workspace_id = owner["workspace"]["workspace_id"]
    owner_id = owner["user"]["user_id"]
    member_id = member["user"]["user_id"]
    owner_membership = store.membership(workspace_id, owner_id)
    assert owner_membership is not None
    store.add_member(workspace_id, member_id, "editor", owner_id)
    membership = store.membership(workspace_id, member_id)
    assert membership is not None

    for kb_id in ("storage-a", "storage-b"):
        access_store.set_kb_policy(workspace_id, kb_id, owner_id, "private")
        access_store.set_document_policy(
            workspace_id,
            kb_id,
            "doc",
            f"{kb_id}.pdf",
            policy="private",
        )
    access_store.grant_subject(workspace_id, "storage-a", member_id, Role.VIEWER)
    access_store.grant_subject(
        workspace_id, "storage-a", member_id, Role.VIEWER, document_id="doc"
    )
    access_store.grant_subject(
        workspace_id, "storage-b", member_id, Role.VIEWER, document_id="doc"
    )
    access_store.set_kb_policy(
        workspace_id,
        "member-created-kb",
        member_id,
        "private",
        owner_membership_id=membership["member_id"],
    )
    access_store.set_document_policy(
        workspace_id,
        "member-created-kb",
        "member-created-doc",
        "created.pdf",
        owner_id=member_id,
        policy="private",
        owner_membership_id=membership["member_id"],
    )
    access_store.set_kb_policy(
        workspace_id,
        "member-document-kb",
        owner_id,
        "private",
        owner_membership_id=owner_membership["member_id"],
    )
    access_store.set_document_policy(
        workspace_id,
        "member-document-kb",
        "member-owned-doc",
        "owned.pdf",
        owner_id=member_id,
        policy="private",
        owner_membership_id=membership["member_id"],
    )
    old_principal = Principal(
        tenant_id=workspace_id,
        subject_id=member_id,
        role=Role.EDITOR,
        key_fingerprint=f"session:{member_id}",
        membership_id=membership["member_id"],
    )
    queued_upload_guard = _live_session_authorization_guard(
        SimpleNamespace(
            state=SimpleNamespace(principal=old_principal),
            app=SimpleNamespace(state=app.state),
        ),
        KnowledgeBaseScope(
            tenant_id=workspace_id,
            external_id="member-created",
            storage_id="member-created-kb",
            owner_id=member_id,
        ),
        permission=Permission.WRITE,
        source="created.pdf",
    )
    assert queued_upload_guard is not None
    queued_upload_guard()
    assert (
        access_store.authorize_query(old_principal, "member-created-kb").mode
        is AccessMode.ALL
    )
    assert (
        access_store.authorize_query(old_principal, "member-document-kb").mode
        is AccessMode.SUBSET
    )
    epochs = {
        kb_id: access_store.acl_epoch(workspace_id, kb_id)
        for kb_id in (
            "storage-a",
            "storage-b",
            "member-created-kb",
            "member-document-kb",
        )
    }

    async with _client(app) as client:
        removed = await client.delete(
            f"/v1/workspaces/{workspace_id}/members/{membership['member_id']}",
            headers=_headers(owner["access_token"]),
        )
        invite = await client.post(
            f"/v1/workspaces/{workspace_id}/invites",
            headers=_headers(owner["access_token"]),
            json={"email": member["user"]["email"], "role": "editor"},
        )
        rejoined = await client.post(
            "/v1/auth/invitations/accept",
            headers=_headers(member["access_token"]),
            json={"token": invite.json()["invite_token"]},
        )

    assert removed.status_code == 204
    assert invite.status_code == 201
    assert rejoined.status_code == 200
    new_membership = store.membership(workspace_id, member_id)
    assert new_membership is not None
    assert new_membership["member_id"] != membership["member_id"]
    with pytest.raises(PermissionError, match="membership incarnation changed"):
        queued_upload_guard()
    assert access_store.is_membership_revoked(
        workspace_id, member_id, membership["member_id"]
    )
    for kb_id in (
        "storage-a",
        "storage-b",
        "member-created-kb",
        "member-document-kb",
    ):
        assert access_store.list_grants(workspace_id, kb_id, subject_id=member_id) == []
        assert access_store.acl_epoch(workspace_id, kb_id) == epochs[kb_id] + 1
    principal = Principal(
        tenant_id=workspace_id,
        subject_id=member_id,
        role=Role.EDITOR,
        key_fingerprint=f"session:{member_id}",
        membership_id=new_membership["member_id"],
    )
    assert access_store.authorize_query(principal, "storage-a").mode is AccessMode.DENY
    assert (
        access_store.authorize_query(principal, "member-created-kb").mode
        is AccessMode.DENY
    )
    assert (
        access_store.authorize_query(principal, "member-document-kb").mode
        is AccessMode.DENY
    )


@pytest.mark.anyio
async def test_acl_cleanup_failure_keeps_member_and_grants_retryable(
    auth_app, monkeypatch
):
    app, store = auth_app
    access_store = app.state.resource_access_store
    owner = store.register("owner@example.com", PASSWORD, "Owner")
    member = store.register("member@example.com", PASSWORD, "Member")
    workspace_id = owner["workspace"]["workspace_id"]
    owner_id = owner["user"]["user_id"]
    member_id = member["user"]["user_id"]
    membership = store.add_member(workspace_id, member_id, "viewer", owner_id)
    access_store.set_kb_policy(workspace_id, "storage-a", owner_id, "private")
    access_store.grant_subject(workspace_id, "storage-a", member_id, Role.VIEWER)
    epoch = access_store.acl_epoch(workspace_id, "storage-a")

    def fail_cleanup(
        tenant_id: str, subject_id: str, *, membership_id: str
    ) -> dict[str, int]:
        raise RuntimeError(f"ACL offline for {tenant_id}/{subject_id}/{membership_id}")

    monkeypatch.setattr(access_store, "revoke_all_subject_grants", fail_cleanup)
    async with _client(app) as client:
        failed = await client.delete(
            f"/v1/workspaces/{workspace_id}/members/{membership['member_id']}",
            headers=_headers(owner["access_token"]),
        )

    assert failed.status_code == 503
    assert failed.json()["error_code"] == "INTERNAL_ERROR"
    assert store.membership(workspace_id, member_id) is not None
    assert not access_store.is_membership_revoked(
        workspace_id, member_id, membership["member_id"]
    )
    assert (
        len(access_store.list_grants(workspace_id, "storage-a", subject_id=member_id))
        == 1
    )
    assert access_store.acl_epoch(workspace_id, "storage-a") == epoch


@pytest.mark.anyio
async def test_user_id_delete_retry_never_removes_a_reinvited_membership(
    auth_app, monkeypatch
):
    app, store = auth_app
    access_store = app.state.resource_access_store
    owner = store.register("owner@example.com", PASSWORD, "Owner")
    member = store.register("member@example.com", PASSWORD, "Member")
    workspace_id = owner["workspace"]["workspace_id"]
    owner_id = owner["user"]["user_id"]
    member_id = member["user"]["user_id"]
    old_membership = store.add_member(workspace_id, member_id, "viewer", owner_id)
    access_store.set_kb_policy(workspace_id, "storage-a", owner_id, "private")
    original_cleanup = access_store.revoke_all_subject_grants
    replacement: dict[str, str] = {}

    def replace_membership_after_cleanup(
        tenant_id: str, subject_id: str, *, membership_id: str
    ) -> dict[str, int]:
        result = original_cleanup(tenant_id, subject_id, membership_id=membership_id)
        assert store.remove_member(workspace_id, membership_id, owner_id)
        replacement.update(
            store.add_member(workspace_id, subject_id, "viewer", owner_id)
        )
        access_store.grant_subject(
            workspace_id,
            "storage-a",
            subject_id,
            Role.VIEWER,
            membership_id=replacement["member_id"],
        )
        return result

    monkeypatch.setattr(
        access_store, "revoke_all_subject_grants", replace_membership_after_cleanup
    )
    async with _client(app) as client:
        raced = await client.delete(
            f"/v1/workspaces/{workspace_id}/members/{member_id}",
            headers=_headers(owner["access_token"]),
        )

    assert raced.status_code == 404
    current = store.membership(workspace_id, member_id)
    assert current is not None
    assert current["member_id"] == replacement["member_id"]
    assert current["member_id"] != old_membership["member_id"]
    assert not access_store.is_membership_revoked(
        workspace_id, member_id, current["member_id"]
    )
    assert (
        len(access_store.list_grants(workspace_id, "storage-a", subject_id=member_id))
        == 1
    )


@pytest.mark.anyio
async def test_anonymous_invite_accept_is_atomic_and_opaque(auth_app):
    app, store = auth_app
    owner = store.register("owner@example.com", PASSWORD, "Owner")
    invite = store.create_invite(
        owner["workspace"]["workspace_id"],
        "new@example.com",
        "viewer",
        owner["user"]["user_id"],
    )
    payload = {
        "token": invite["invite_token"],
        "email": "NEW@EXAMPLE.COM",
        "password": "brand new secure password",
        "display_name": "New User",
    }

    async with _client(app) as client:
        partial = await client.post(
            "/v1/auth/invitations/accept",
            json={"token": invite["invite_token"], "email": "new@example.com"},
        )
        accepted = await client.post("/v1/auth/invitations/accept", json=payload)
        consumed = await client.post("/v1/auth/invitations/accept", json=payload)
        invalid = await client.post(
            "/v1/auth/invitations/accept",
            json={**payload, "token": "not-a-real-token"},
        )

    assert partial.status_code == 422
    assert accepted.status_code == 200
    assert accepted.json()["user"]["email"] == "new@example.com"
    assert accepted.json()["workspace"]["role"] == "viewer"
    token = _access_token(accepted.json())
    assert (
        store.authenticate_session(token).workspace_id
        == owner["workspace"]["workspace_id"]
    )
    assert consumed.status_code == invalid.status_code == 400
    assert consumed.json() == invalid.json()
    assert consumed.json()["error_code"] == "INVITE_INVALID"


@pytest.mark.anyio
async def test_public_auth_routes_apply_their_own_limiter(tmp_path):
    store = AuthStore(str(tmp_path / "auth.db"), scrypt_n=1 << 10)
    app = FastAPI()
    app.state.auth_store = store
    app.state.auth_public_rate_limiter = _DenyAll()
    app.include_router(auth_router)
    try:
        async with _client(app) as client:
            response = await client.post(
                "/v1/auth/register",
                json={
                    "email": "blocked@example.com",
                    "password": PASSWORD,
                    "display_name": "Blocked",
                },
            )
        assert response.status_code == 429
        assert response.json()["error_code"] == "REQUEST_THROTTLED"
        assert response.headers["retry-after"] == "60"
        with pytest.raises(AuthNotFoundError):
            store.get_user(email="blocked@example.com")
    finally:
        store.close()
