import json
import secrets

from httpx import ASGITransport, AsyncClient
import pytest

from cogdoc.api.app import create_app
from cogdoc.api.audit import AuditStore
from cogdoc.api.auth_store import AuthNotFoundError, AuthStore
from cogdoc.api.resource_access import ResourceAccessStore
from cogdoc.api.scim import (
    SCIMAccess,
    SCIM_GROUP_SCHEMA,
    SCIM_PATCH_SCHEMA,
    SCIM_USER_SCHEMA,
    parse_scim_access_registry,
)
from cogdoc.api.session_store import SessionStore


PASSWORD = "correct horse battery"
ISSUER = "https://id.example.com"


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def scim_app(tmp_path):
    auth = AuthStore(str(tmp_path / "state.db"), scrypt_n=1 << 10)
    access = ResourceAccessStore(tmp_path / "access.db")
    owner = auth.register("owner@example.com", PASSWORD, "Owner")
    other = auth.create_workspace(owner["user"]["user_id"], "Other")
    token_one, token_two = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    registry = parse_scim_access_registry(
        (
            '[{"token":"%s","workspace_id":"%s","label":"Primary"},'
            '{"token":"%s","workspace_id":"%s","label":"Other"}]'
        )
        % (
            token_one,
            owner["workspace"]["workspace_id"],
            token_two,
            other["workspace_id"],
        ),
        issuer=ISSUER,
        group_role_map='{"CogDoc Admins":"admin"}',
    )
    audit = AuditStore(tmp_path / "audit.jsonl")
    app = create_app(
        session_store=SessionStore(),
        auth_store=auth,
        resource_access_store=access,
        audit_store=audit,
        scim_access_registry=registry,
    )
    yield app, auth, audit, owner, token_one, token_two
    auth.close()
    access.close()


def _headers(token):
    return {"Authorization": f"Bearer {token}"}


def test_create_app_rejects_inconsistent_scim_policies_for_one_workspace(tmp_path):
    auth = AuthStore(str(tmp_path / "state.db"), scrypt_n=1 << 10)
    access_store = ResourceAccessStore(tmp_path / "access.db")
    try:
        first = SCIMAccess("one", "workspace", ISSUER, "one", "viewer", {})
        second = SCIMAccess("two", "workspace", ISSUER, "two", "editor", {})
        with pytest.raises(ValueError, match="one provisioning policy"):
            create_app(
                session_store=SessionStore(),
                auth_store=auth,
                resource_access_store=access_store,
                scim_access_registry={"one": first, "two": second},
            )
    finally:
        auth.close()
        access_store.close()


@pytest.mark.anyio
async def test_startup_reconciles_removed_scim_role_mapping(tmp_path):
    auth = AuthStore(str(tmp_path / "state.db"), scrypt_n=1 << 10)
    access_store = ResourceAccessStore(tmp_path / "access.db")
    owner = auth.register("owner@example.com", PASSWORD, "Owner")
    workspace_id = owner["workspace"]["workspace_id"]
    user = auth.create_scim_user(
        workspace_id=workspace_id,
        issuer=ISSUER,
        external_id="startup-user",
        user_name="startup@example.com",
        display_name="Startup",
        active=True,
        base_role="editor",
    )
    auth.create_scim_group(
        workspace_id=workspace_id,
        external_id="startup-group",
        display_name="Old Admins",
        mapped_role="admin",
        member_ids=[user["id"]],
    )
    token = secrets.token_urlsafe(32)
    registry = parse_scim_access_registry(
        json.dumps(
            [{"token": token, "workspace_id": workspace_id, "label": "Current"}]
        ),
        issuer=ISSUER,
        default_role="viewer",
        group_role_map="{}",
    )
    app = create_app(
        session_store=SessionStore(),
        auth_store=auth,
        resource_access_store=access_store,
        scim_access_registry=registry,
    )
    try:
        assert (
            auth.get_workspace(workspace_id, user_id=user["user_id"])["role"] == "admin"
        )
        async with app.router.lifespan_context(app):
            assert (
                auth.get_workspace(workspace_id, user_id=user["user_id"])["role"]
                == "viewer"
            )
    finally:
        auth.close()
        access_store.close()


@pytest.mark.anyio
async def test_startup_rejects_scim_token_for_missing_workspace(tmp_path):
    auth = AuthStore(str(tmp_path / "state.db"), scrypt_n=1 << 10)
    access_store = ResourceAccessStore(tmp_path / "access.db")
    registry = parse_scim_access_registry(
        json.dumps(
            [{"token": "m" * 32, "workspace_id": "wsp_missing", "label": "Bad"}]
        ),
        issuer=ISSUER,
    )
    app = create_app(
        session_store=SessionStore(),
        auth_store=auth,
        resource_access_store=access_store,
        scim_access_registry=registry,
    )
    try:
        with pytest.raises(AuthNotFoundError, match="workspace not found"):
            async with app.router.lifespan_context(app):
                pass
        assert app.state.lifecycle_status == "stopped"
    finally:
        auth.close()
        access_store.close()


@pytest.mark.anyio
async def test_scim_admin_status_is_authorized_and_never_exposes_token_material(
    scim_app,
):
    app, auth, _audit, owner, token, _other_token = scim_app
    workspace_id = owner["workspace"]["workspace_id"]
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="https://api.example.com"
        ) as client:
            created = await client.post(
                "/scim/v2/Users",
                headers=_headers(token),
                json={
                    "schemas": [SCIM_USER_SCHEMA],
                    "externalId": "status-viewer",
                    "userName": "status-viewer@example.com",
                    "displayName": "Status Viewer",
                    "active": True,
                },
            )
            assert created.status_code == 201
            viewer = auth.login_oidc(
                issuer=ISSUER,
                subject="status-viewer-subject",
                email="status-viewer@example.com",
                display_name="Status Viewer",
                email_verified=True,
                workspace_id=workspace_id,
            )
            denied = await client.get(
                f"/v1/workspaces/{workspace_id}/scim-status",
                headers={"Authorization": f"Bearer {viewer['access_token']}"},
            )
            status = await client.get(
                f"/v1/workspaces/{workspace_id}/scim-status",
                headers={"Authorization": f"Bearer {owner['access_token']}"},
            )

    assert denied.status_code == 403
    assert status.status_code == 200
    payload = status.json()["status"]
    assert payload["enabled"] is True
    assert payload["token_labels"] == ["Primary"]
    assert payload["default_role"] == "viewer"
    assert payload["group_role_map"] == {"cogdoc admins": "admin"}
    assert payload["active_users"] == 1
    assert token not in status.text
    assert "fingerprint" not in status.text.casefold()


@pytest.mark.anyio
async def test_scim_user_group_lifecycle_is_scoped_versioned_and_audited(scim_app):
    app, auth, audit, owner, token, other_token = scim_app
    workspace_id = owner["workspace"]["workspace_id"]
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="https://api.example.com"
        ) as client:
            unauthenticated = await client.get("/scim/v2/Users")
            config = await client.get(
                "/scim/v2/ServiceProviderConfig", headers=_headers(token)
            )
            resource_type = await client.get(
                "/scim/v2/ResourceTypes/User", headers=_headers(token)
            )
            user_schema = await client.get(
                f"/scim/v2/Schemas/{SCIM_USER_SCHEMA}", headers=_headers(token)
            )
            created = await client.post(
                "/scim/v2/Users",
                headers=_headers(token),
                json={
                    "schemas": [SCIM_USER_SCHEMA],
                    "externalId": "entra-alice",
                    "userName": "alice@example.com",
                    "displayName": "Alice",
                    "active": True,
                },
            )
            user_id = created.json()["id"]
            filtered = await client.get(
                "/scim/v2/Users",
                headers=_headers(token),
                params={"filter": 'userName eq "alice@example.com"'},
            )
            cross_workspace = await client.get(
                f"/scim/v2/Users/{user_id}", headers=_headers(other_token)
            )
            group = await client.post(
                "/scim/v2/Groups",
                headers=_headers(token),
                json={
                    "schemas": [SCIM_GROUP_SCHEMA],
                    "externalId": "entra-admins",
                    "displayName": "CogDoc Admins",
                    "members": [{"value": user_id}],
                },
            )
            scim_row = auth.get_scim_user(
                workspace_id=workspace_id, scim_user_id=user_id
            )
            assert (
                auth.get_workspace(workspace_id, user_id=scim_row["user_id"])["role"]
                == "admin"
            )
            removed_member = await client.patch(
                f"/scim/v2/Groups/{group.json()['id']}",
                headers={**_headers(token), "If-Match": group.headers["etag"]},
                json={
                    "schemas": [SCIM_PATCH_SCHEMA],
                    "Operations": [
                        {
                            "op": "Remove",
                            "path": f'members[value eq "{user_id}"]',
                        }
                    ],
                },
            )
            assert (
                auth.get_workspace(workspace_id, user_id=scim_row["user_id"])["role"]
                == "viewer"
            )
            restored_member = await client.patch(
                f"/scim/v2/Groups/{group.json()['id']}",
                headers={
                    **_headers(token),
                    "If-Match": removed_member.headers["etag"],
                },
                json={
                    "schemas": [SCIM_PATCH_SCHEMA],
                    "Operations": [
                        {
                            "op": "Add",
                            "path": "members",
                            "value": [{"value": user_id}],
                        }
                    ],
                },
            )
            assert restored_member.status_code == 200
            refreshed = await client.get(
                f"/scim/v2/Users/{user_id}", headers=_headers(token)
            )
            stale = await client.put(
                f"/scim/v2/Users/{user_id}",
                headers={**_headers(token), "If-Match": 'W/"99"'},
                json={
                    "schemas": [SCIM_USER_SCHEMA],
                    "userName": "alice@example.com",
                    "displayName": "Alice",
                    "active": True,
                },
            )
            disabled = await client.patch(
                f"/scim/v2/Users/{user_id}",
                headers={**_headers(token), "If-Match": refreshed.headers["etag"]},
                json={
                    "schemas": [SCIM_PATCH_SCHEMA],
                    "Operations": [{"op": "Replace", "path": "active", "value": False}],
                },
            )
            normal_api = await client.get("/v1/tenant", headers=_headers(token))

    assert unauthenticated.status_code == 401
    assert unauthenticated.headers["content-type"].startswith("application/scim+json")
    assert config.status_code == 200
    assert config.json()["patch"]["supported"] is True
    assert resource_type.json()["endpoint"] == "/Users"
    assert any(
        attribute["name"] == "userName" and attribute["required"] is True
        for attribute in user_schema.json()["attributes"]
    )
    assert created.status_code == 201
    assert created.headers["etag"] == 'W/"1"'
    assert created.json()["meta"]["location"].endswith(f"/scim/v2/Users/{user_id}")
    assert filtered.json()["totalResults"] == 1
    assert cross_workspace.status_code == 404
    assert group.status_code == 201
    assert stale.status_code == 412
    assert disabled.status_code == 200
    assert disabled.json()["active"] is False
    assert normal_api.status_code == 401
    events = audit.list(workspace_id, limit=100)
    assert any(
        event["path"] == "/http" and event["principal"] == "scim:Primary"
        for event in events
    )
    assert token not in str(events)


@pytest.mark.anyio
async def test_scim_filter_patch_and_body_fail_closed(scim_app):
    app, _auth, _audit, _owner, token, _other_token = scim_app
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="https://api.example.com"
        ) as client:
            bad_filter = await client.get(
                "/scim/v2/Users",
                headers=_headers(token),
                params={"filter": 'name co "alice"'},
            )
            bad_pagination = await client.get(
                "/scim/v2/Users",
                headers=_headers(token),
                params={"startIndex": "nope", "count": "201"},
            )
            bad_schema = await client.post(
                "/scim/v2/Users",
                headers=_headers(token),
                json={"schemas": [], "userName": "alice@example.com"},
            )
            oversized = await client.post(
                "/scim/v2/Users",
                headers=_headers(token),
                content=b"x" * 1_000_001,
            )

    assert bad_filter.status_code == 400
    assert bad_filter.json()["scimType"] == "invalidFilter"
    assert bad_schema.status_code == 400
    assert bad_schema.json()["scimType"] == "invalidValue"
    assert bad_pagination.status_code == 400
    assert bad_pagination.headers["content-type"].startswith("application/scim+json")
    assert bad_pagination.json()["scimType"] == "invalidValue"
    assert oversized.status_code == 413
