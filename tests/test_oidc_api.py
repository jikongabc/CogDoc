from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest

from cogdoc.api.access_control import AccessControlMiddleware, TokenBucketRateLimiter
from cogdoc.api.app import create_app
from cogdoc.api.auth_store import AuthStore
from cogdoc.api.oidc import (
    OIDCClaims,
    OIDCFlowStore,
    OIDCManager,
    OIDCProviderConfig,
)
from cogdoc.api.routes.auth import router as auth_router
from cogdoc.api.routes.oidc import router as oidc_router
from cogdoc.api.resource_access import ResourceAccessStore


PASSWORD = "correct horse battery"
ISSUER = "https://id.example.com"
RETURN_URL = "https://app.example.com/login"


class FakeClient:
    def __init__(self):
        self.config = OIDCProviderConfig(
            issuer=ISSUER,
            client_id="client-1",
            redirect_uri="https://api.example.com/v1/auth/oidc/callback",
            allowed_return_urls=(RETURN_URL,),
        ).validated()
        self.subject = "subject-1"
        self.email = "alice@example.com"
        self.display_name = "Alice"
        self.groups = ()
        self.exchanges = 0

    def authorization_url(self, flow):
        return f"{ISSUER}/authorize?state={flow.state}"

    def exchange_code(self, code, *, code_verifier, nonce):
        assert code == "provider-code"
        assert code_verifier and nonce
        self.exchanges += 1
        return OIDCClaims(
            issuer=ISSUER,
            subject=self.subject,
            email=self.email,
            email_verified=True,
            display_name=self.display_name,
            string_list_claims={"groups": tuple(self.groups)},
        )

    def close(self):
        return None


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def oidc_app(tmp_path):
    store = AuthStore(str(tmp_path / "state.db"), scrypt_n=1 << 10)
    flow_store = OIDCFlowStore(str(tmp_path / "state.db"), bytes(range(32)))
    client = FakeClient()
    manager = OIDCManager(
        client,
        flow_store,
        store,
        jit_provisioning_enabled=True,
    )
    executor = ThreadPoolExecutor(max_workers=2)
    app = FastAPI()
    app.state.auth_store = store
    app.state.oidc_manager = manager
    app.state.offload_executor = executor
    app.state.auth_public_rate_limiter = SimpleNamespace(allow=lambda _key: True)
    app.include_router(auth_router)
    app.include_router(oidc_router)
    app.add_middleware(
        AccessControlMiddleware,
        api_keys=set(),
        principals=None,
        rate_limiter=TokenBucketRateLimiter(capacity=0, refill_per_second=0.0),
        auth_store=store,
    )
    yield app, store, flow_store, client
    executor.shutdown(wait=True)
    flow_store.close()
    store.close()


def _client(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.anyio
async def test_oidc_login_callback_handoff_is_one_shot_and_scoped(oidc_app):
    app, store, flow_store, provider = oidc_app
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

    async with _client(app) as client:
        start = await client.post(
            "/v1/auth/oidc/authorize",
            json={"return_url": RETURN_URL, "workspace_id": workspace_id},
        )
        assert start.status_code == 200, start.text
        state = parse_qs(urlsplit(start.json()["authorization_url"]).query)["state"][0]
        callback = await client.get(
            "/v1/auth/oidc/callback",
            params={"state": state, "code": "provider-code"},
            follow_redirects=False,
        )
        handoff = parse_qs(urlsplit(callback.headers["location"]).query)["oidc_code"][0]
        exchanged = await client.post("/v1/auth/oidc/exchange", json={"code": handoff})
        token = exchanged.json()["session"]["access_token"]
        me = await client.get("/v1/auth/me", headers=_bearer(token))
        replay_state = await client.get(
            "/v1/auth/oidc/callback",
            params={"state": state, "code": "provider-code"},
        )
        replay_handoff = await client.post(
            "/v1/auth/oidc/exchange", json={"code": handoff}
        )

    assert start.status_code == 200
    assert callback.status_code == 303
    assert exchanged.status_code == 200
    assert exchanged.json()["kind"] == "login"
    assert exchanged.json()["session"]["workspace"]["workspace_id"] == workspace_id
    assert exchanged.json()["session"]["workspace"]["role"] == "viewer"
    assert me.status_code == 200
    assert me.json()["user"]["email"] == "alice@example.com"
    assert replay_state.status_code == 400
    assert replay_handoff.status_code == 400
    assert provider.exchanges == 1
    database_dump = "\n".join(flow_store._conn.iterdump())
    auth_dump = "\n".join(store._conn.iterdump())
    for secret in (token, state, handoff, "provider-code"):
        assert secret not in database_dump
        assert secret not in auth_dump


@pytest.mark.anyio
async def test_oidc_callback_applies_verified_group_mapping(oidc_app):
    app, store, _flow_store, provider = oidc_app
    owner = store.register("owner@example.com", PASSWORD, "Owner")
    workspace_id = owner["workspace"]["workspace_id"]
    store.set_oidc_policy(
        workspace_id=workspace_id,
        issuer=ISSUER,
        allowed_domains=["example.com"],
        default_role="viewer",
        enabled=True,
        group_role_map={"cogdoc-admins": "admin"},
        require_mapped_group=True,
        actor_user_id=owner["user"]["user_id"],
    )
    provider.subject = "group-callback-user"
    provider.email = "group-callback@example.com"
    provider.groups = ("CogDoc-Admins",)

    async with _client(app) as client:
        start = await client.post(
            "/v1/auth/oidc/authorize",
            json={"return_url": RETURN_URL, "workspace_id": workspace_id},
        )
        state = parse_qs(urlsplit(start.json()["authorization_url"]).query)["state"][0]
        callback = await client.get(
            "/v1/auth/oidc/callback",
            params={"state": state, "code": "provider-code"},
            follow_redirects=False,
        )
        handoff = parse_qs(urlsplit(callback.headers["location"]).query)["oidc_code"][0]
        exchanged = await client.post("/v1/auth/oidc/exchange", json={"code": handoff})

    assert callback.status_code == 303
    assert exchanged.status_code == 200
    assert exchanged.json()["session"]["workspace"]["role"] == "admin"


@pytest.mark.anyio
async def test_oidc_provider_error_consumes_state_without_exchange(oidc_app):
    app, _store, _flow_store, provider = oidc_app
    async with _client(app) as client:
        start = await client.post(
            "/v1/auth/oidc/authorize", json={"return_url": RETURN_URL}
        )
        assert start.status_code == 200, start.text
        state = parse_qs(urlsplit(start.json()["authorization_url"]).query)["state"][0]
        callback = await client.get(
            "/v1/auth/oidc/callback",
            params={"state": state, "error": "access_denied"},
            follow_redirects=False,
        )
        replay = await client.get(
            "/v1/auth/oidc/callback",
            params={"state": state, "code": "provider-code"},
        )
    assert callback.status_code == 303
    assert parse_qs(urlsplit(callback.headers["location"]).query) == {
        "oidc_error": ["authorization_failed"]
    }
    assert replay.status_code == 400
    assert provider.exchanges == 0


@pytest.mark.anyio
async def test_authenticated_user_explicitly_links_and_unlinks_oidc(oidc_app):
    app, store, _flow_store, provider = oidc_app
    registered = store.register("password@example.com", PASSWORD, "Password User")
    provider.email = "different@example.com"
    token = registered["access_token"]

    async with _client(app) as client:
        unauthorized = await client.post(
            "/v1/auth/oidc/link/authorize", json={"return_url": RETURN_URL}
        )
        start = await client.post(
            "/v1/auth/oidc/link/authorize",
            headers=_bearer(token),
            json={"return_url": RETURN_URL},
        )
        assert start.status_code == 200, start.text
        state = parse_qs(urlsplit(start.json()["authorization_url"]).query)["state"][0]
        callback = await client.get(
            "/v1/auth/oidc/callback",
            params={"state": state, "code": "provider-code"},
            follow_redirects=False,
        )
        handoff = parse_qs(urlsplit(callback.headers["location"]).query)["oidc_code"][0]
        exchanged = await client.post("/v1/auth/oidc/exchange", json={"code": handoff})
        identities = await client.get(
            "/v1/auth/oidc/identities", headers=_bearer(token)
        )
        identity_id = identities.json()["identities"][0]["identity_id"]
        removed = await client.delete(
            f"/v1/auth/oidc/identities/{identity_id}", headers=_bearer(token)
        )

    assert unauthorized.status_code == 401
    assert start.status_code == 200
    assert exchanged.status_code == 200
    assert exchanged.json()["kind"] == "link"
    assert exchanged.json()["identity"]["email_at_link"] == "different@example.com"
    assert identities.status_code == 200
    assert removed.status_code == 204


@pytest.mark.anyio
async def test_link_callback_revalidates_frozen_session_atomically(oidc_app):
    app, store, _flow_store, provider = oidc_app
    registered = store.register("password@example.com", PASSWORD, "Password User")
    token = registered["access_token"]
    session_id = registered["session"]["session_id"]
    user_id = registered["user"]["user_id"]
    original_exchange = provider.exchange_code

    def revoke_during_exchange(code, *, code_verifier, nonce):
        claims = original_exchange(code, code_verifier=code_verifier, nonce=nonce)
        assert store.revoke_session(user_id, session_id)
        return claims

    provider.exchange_code = revoke_during_exchange
    async with _client(app) as client:
        start = await client.post(
            "/v1/auth/oidc/link/authorize",
            headers=_bearer(token),
            json={"return_url": RETURN_URL},
        )
        state = parse_qs(urlsplit(start.json()["authorization_url"]).query)["state"][0]
        callback = await client.get(
            "/v1/auth/oidc/callback",
            params={"state": state, "code": "provider-code"},
            follow_redirects=False,
        )

    assert callback.status_code == 303
    assert parse_qs(urlsplit(callback.headers["location"]).query) == {
        "oidc_error": ["authorization_failed"]
    }
    assert store.list_oidc_identities(user_id=user_id) == []


def test_create_app_rejects_oidc_manager_with_split_auth_store(tmp_path):
    app_store = AuthStore(str(tmp_path / "app.db"), scrypt_n=1 << 10)
    foreign_store = AuthStore(str(tmp_path / "foreign.db"), scrypt_n=1 << 10)
    access_store = ResourceAccessStore(tmp_path / "access.db")
    try:
        with pytest.raises(ValueError, match="share the app AuthStore"):
            create_app(
                auth_store=app_store,
                resource_access_store=access_store,
                oidc_manager=SimpleNamespace(auth_store=foreign_store),
            )
    finally:
        app_store.close()
        foreign_store.close()
        access_store.close()


@pytest.mark.anyio
async def test_workspace_oidc_policy_requires_manage_access_and_revision(oidc_app):
    app, store, _flow_store, _provider = oidc_app
    owner = store.register("owner@example.com", PASSWORD, "Owner")
    viewer = store.login_oidc(
        issuer=ISSUER,
        subject="viewer-subject",
        email="viewer@example.com",
        display_name="Viewer",
        email_verified=True,
        jit_provisioning_enabled=True,
    )
    workspace_id = owner["workspace"]["workspace_id"]
    store.add_member(
        workspace_id,
        viewer["user"]["user_id"],
        "viewer",
        owner["user"]["user_id"],
    )

    async with _client(app) as client:
        denied = await client.put(
            f"/v1/workspaces/{workspace_id}/oidc-policy",
            headers=_bearer(viewer["access_token"]),
            json={"allowed_domains": ["example.com"]},
        )
        created = await client.put(
            f"/v1/workspaces/{workspace_id}/oidc-policy",
            headers=_bearer(owner["access_token"]),
            json={
                "allowed_domains": ["EXAMPLE.COM", "subsidiary.example"],
                "default_role": "editor",
                "enabled": True,
                "group_claim": "groups",
                "group_role_map": {
                    "CogDoc Editors": "editor",
                    "CogDoc Admins": "admin",
                },
                "require_mapped_group": True,
            },
        )
        owner_mapping = await client.put(
            f"/v1/workspaces/{workspace_id}/oidc-policy",
            headers=_bearer(owner["access_token"]),
            json={
                "allowed_domains": ["example.com"],
                "group_role_map": {"owners": "owner"},
            },
        )
        stale = await client.put(
            f"/v1/workspaces/{workspace_id}/oidc-policy",
            headers=_bearer(owner["access_token"]),
            json={
                "allowed_domains": ["example.com"],
                "default_role": "viewer",
                "enabled": False,
                "expected_revision": 99,
            },
        )
        fetched = await client.get(
            f"/v1/workspaces/{workspace_id}/oidc-policy",
            headers=_bearer(owner["access_token"]),
        )
        scim_status = await client.get(
            f"/v1/workspaces/{workspace_id}/scim-status",
            headers=_bearer(owner["access_token"]),
        )

    assert denied.status_code == 403
    assert created.status_code == 200
    assert created.json()["policy"]["issuer"] == ISSUER
    assert created.json()["policy"]["allowed_domains"] == [
        "example.com",
        "subsidiary.example",
    ]
    assert created.json()["policy"]["group_role_map"] == {
        "cogdoc admins": "admin",
        "cogdoc editors": "editor",
    }
    assert created.json()["policy"]["require_mapped_group"] is True
    assert owner_mapping.status_code == 422
    assert stale.status_code == 409
    assert fetched.json() == created.json()
    assert scim_status.status_code == 200
    assert scim_status.json()["status"]["enabled"] is False
