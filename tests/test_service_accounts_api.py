from httpx import ASGITransport, AsyncClient
import pytest

from cogdoc.api.app import create_app
from cogdoc.api.audit import AuditStore
from cogdoc.api.auth_store import AuthStore
from cogdoc.api.resource_access import ResourceAccessStore
from cogdoc.api.session_store import SessionStore


PASSWORD = "correct horse battery"


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _bearer(token: str, workspace_id: str | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {token}"}
    if workspace_id is not None:
        headers["X-CogDoc-Workspace"] = workspace_id
    return headers


@pytest.mark.anyio
async def test_service_account_api_is_one_time_live_scoped_and_audited(tmp_path):
    auth = AuthStore(str(tmp_path / "state.db"), scrypt_n=1 << 10)
    access = ResourceAccessStore(tmp_path / "access.db")
    audit = AuditStore(tmp_path / "audit.jsonl")
    owner = auth.register("owner@example.com", PASSWORD, "Owner")
    viewer_identity = auth.register("viewer@example.com", PASSWORD, "Viewer")
    workspace_id = owner["workspace"]["workspace_id"]
    other_workspace = viewer_identity["workspace"]["workspace_id"]
    auth.add_member(
        workspace_id,
        viewer_identity["user"]["user_id"],
        "viewer",
        owner["user"]["user_id"],
    )
    app = create_app(
        session_store=SessionStore(),
        auth_store=auth,
        resource_access_store=access,
        audit_store=audit,
    )
    owner_headers = _bearer(owner["access_token"], workspace_id)
    viewer_headers = _bearer(viewer_identity["access_token"], workspace_id)
    try:
        async with app.router.lifespan_context(app):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="https://api.example.com"
            ) as client:
                viewer_denied = await client.post(
                    f"/v1/workspaces/{workspace_id}/service-accounts",
                    headers=viewer_headers,
                    json={"name": "Denied", "role": "viewer"},
                )
                default_policy = await client.get(
                    f"/v1/workspaces/{workspace_id}/service-account-policy",
                    headers=owner_headers,
                )
                viewer_policy_denied = await client.put(
                    f"/v1/workspaces/{workspace_id}/service-account-policy",
                    headers=viewer_headers,
                    json={
                        "max_accounts": 50,
                        "max_tokens_per_account": 5,
                        "max_token_ttl_days": 90,
                        "allow_non_expiring": False,
                        "allowed_permissions": ["read", "query"],
                        "expected_revision": 0,
                    },
                )
                policy_updated = await client.put(
                    f"/v1/workspaces/{workspace_id}/service-account-policy",
                    headers=owner_headers,
                    json={
                        "max_accounts": 50,
                        "max_tokens_per_account": 5,
                        "max_token_ttl_days": 90,
                        "allow_non_expiring": False,
                        "allowed_permissions": [
                            "read",
                            "query",
                            "write",
                            "review",
                            "publish",
                        ],
                        "expected_revision": 0,
                    },
                )
                created = await client.post(
                    f"/v1/workspaces/{workspace_id}/service-accounts",
                    headers=owner_headers,
                    json={
                        "name": "Automation",
                        "description": "CI reader",
                        "role": "viewer",
                    },
                )
                account = created.json()["service_account"]
                token_created = await client.post(
                    f"/v1/workspaces/{workspace_id}/service-accounts/"
                    f"{account['service_account_id']}/tokens",
                    headers=owner_headers,
                    json={"label": "CI", "expires_in_days": 30},
                )
                raw_token = token_created.json()["token"]
                token_metadata = token_created.json()["service_token"]
                listed = await client.get(
                    f"/v1/workspaces/{workspace_id}/service-accounts/"
                    f"{account['service_account_id']}/tokens",
                    headers=owner_headers,
                )
                tenant = await client.get(
                    "/v1/tenant", headers=_bearer(raw_token, workspace_id)
                )
                self_management_denied = await client.get(
                    f"/v1/workspaces/{workspace_id}/service-accounts",
                    headers=_bearer(raw_token, workspace_id),
                )
                cross_workspace = await client.get(
                    "/v1/tenant", headers=_bearer(raw_token, other_workspace)
                )
                updated = await client.patch(
                    f"/v1/workspaces/{workspace_id}/service-accounts/"
                    f"{account['service_account_id']}",
                    headers=owner_headers,
                    json={
                        "name": account["name"],
                        "description": account["description"],
                        "role": "editor",
                        "active": True,
                        "expected_revision": account["revision"],
                    },
                )
                live_role = await client.get(
                    "/v1/tenant", headers=_bearer(raw_token, workspace_id)
                )
                scoped_account_response = await client.post(
                    f"/v1/workspaces/{workspace_id}/service-accounts",
                    headers=owner_headers,
                    json={"name": "Read Scope", "role": "editor"},
                )
                scoped_account = scoped_account_response.json()["service_account"]
                scoped_token_response = await client.post(
                    f"/v1/workspaces/{workspace_id}/service-accounts/"
                    f"{scoped_account['service_account_id']}/tokens",
                    headers=owner_headers,
                    json={
                        "label": "read only",
                        "expires_in_days": 30,
                        "permissions": ["read", "query"],
                    },
                )
                scoped_token = scoped_token_response.json()["token"]
                scoped_write = await client.post(
                    "/v1/knowledge-bases",
                    headers=_bearer(scoped_token, workspace_id),
                    json={"kb_id": "must-not-create"},
                )
                revoked = await client.delete(
                    f"/v1/workspaces/{workspace_id}/service-accounts/"
                    f"{account['service_account_id']}/tokens/{token_metadata['token_id']}",
                    headers=owner_headers,
                    params={"expected_revision": token_metadata["revision"]},
                )
                rejected = await client.get(
                    "/v1/tenant", headers=_bearer(raw_token, workspace_id)
                )

        assert viewer_denied.status_code == 403
        assert default_policy.json()["policy"]["revision"] == 0
        assert viewer_policy_denied.status_code == 403
        assert policy_updated.status_code == 200
        assert policy_updated.json()["policy"]["allow_non_expiring"] is False
        assert created.status_code == 201
        assert token_created.status_code == 201
        assert token_created.headers["cache-control"] == "no-store"
        assert raw_token.startswith("cog_svc_")
        assert raw_token not in listed.text
        assert tenant.status_code == 200
        assert tenant.json()["role"] == "viewer"
        assert self_management_denied.status_code == 403
        assert cross_workspace.status_code == 404
        assert updated.status_code == 200
        assert live_role.json()["role"] == "editor"
        assert scoped_token_response.json()["service_token"]["permissions"] == [
            "query",
            "read",
        ]
        assert scoped_write.status_code == 403
        assert revoked.status_code == 204
        assert rejected.status_code == 401
        assert raw_token not in (tmp_path / "audit.jsonl").read_text("utf-8")
    finally:
        auth.close()
        access.close()
