from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import json

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient

from cogdoc.api.resource_access import ResourceAccessStore
from cogdoc.api.routes.access import router
from cogdoc.api.tenancy import Principal, Role


_PHYSICAL_A = "t-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
_PHYSICAL_B = "t-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"


pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


class _Registry:
    def __init__(self) -> None:
        self.rows = {
            ("tenant-a", "alpha"): {
                "tenant_id": "tenant-a",
                "kb_id": "alpha",
                "storage_id": _PHYSICAL_A,
                "owner_id": "resource-owner-a",
                "created_at": "2026-01-01T00:00:00+00:00",
            },
            ("tenant-b", "bravo"): {
                "tenant_id": "tenant-b",
                "kb_id": "bravo",
                "storage_id": _PHYSICAL_B,
                "owner_id": "resource-owner-b",
                "created_at": "2026-01-01T00:00:00+00:00",
            },
        }

    def resolve(self, kb_id: str, tenant_id: str):
        return self.rows.get((tenant_id, kb_id))


def _principal(
    *,
    tenant_id: str = "tenant-a",
    subject_id: str = "manager",
    role: Role = Role.ADMIN,
    session: bool = False,
) -> Principal:
    fingerprint = (
        f"session:{subject_id}" if session else f"api-key-fingerprint:{subject_id}"
    )
    return Principal(
        tenant_id=tenant_id,
        subject_id=subject_id,
        role=role,
        key_fingerprint=fingerprint,
    )


def _app(tmp_path, *, principal: Principal | None = None, store=...):
    app = FastAPI()
    app.include_router(router)
    app.state.principal = principal or _principal()
    app.state.kb_registry = _Registry()
    if store is ...:
        store = ResourceAccessStore(tmp_path / "resource-access.db")
    if store is not None:
        app.state.resource_access_store = store

    @app.middleware("http")
    async def inject_principal(request: Request, call_next):
        request.state.principal = request.app.state.principal
        return await call_next(request)

    return app, store


@asynccontextmanager
async def _client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            yield client


async def _configure_kb(client: AsyncClient, policy: str = "private") -> dict:
    response = await client.patch(
        "/v1/knowledge-bases/alpha/access", json={"policy": policy}
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _configure_document(
    client: AsyncClient,
    document_id: str = "doc-1",
    source: str = "report.pdf",
    policy: str = "private",
) -> dict:
    response = await client.patch(
        f"/v1/knowledge-bases/alpha/documents/{document_id}/access",
        json={"policy": policy, "source": source},
    )
    assert response.status_code == 200, response.text
    return response.json()


async def test_kb_policy_get_update_and_response_never_exposes_storage_id(tmp_path):
    app, store = _app(tmp_path)
    async with _client(app) as client:
        missing = await client.get("/v1/knowledge-bases/alpha/access")
        assert missing.status_code == 200
        assert missing.json() == {
            "schema_version": "v1",
            "kb_id": "alpha",
            "configured": False,
            "owner_id": "resource-owner-a",
            "policy": None,
            "acl_epoch": 0,
            "created_at": "",
            "updated_at": "",
        }

        payload = await _configure_kb(client, "workspace")
        assert payload["kb_id"] == "alpha"
        assert payload["policy"] == "workspace"
        assert payload["configured"] is True
        assert _PHYSICAL_A not in json.dumps(payload)

        fetched = await client.get("/v1/knowledge-bases/alpha/access")
        assert fetched.status_code == 200
        assert fetched.json() == payload

    persisted = store.get_kb_policy("tenant-a", _PHYSICAL_A)
    assert persisted["policy"] == "workspace"
    assert persisted["owner_id"] == "resource-owner-a"


async def test_manage_access_permission_is_enforced_inside_router(tmp_path):
    app, store = _app(tmp_path, principal=_principal(role=Role.VIEWER))
    async with _client(app) as client:
        response = await client.patch(
            "/v1/knowledge-bases/alpha/access", json={"policy": "workspace"}
        )
    assert response.status_code == 403
    assert response.json()["error_code"] == "FORBIDDEN"
    assert store.get_kb_policy("tenant-a", _PHYSICAL_A) is None


async def test_cross_tenant_slug_and_physical_id_are_hidden_as_generic_404(tmp_path):
    app, _store = _app(tmp_path)
    async with _client(app) as client:
        for identifier in ("bravo", _PHYSICAL_B):
            response = await client.get(f"/v1/knowledge-bases/{identifier}/access")
            assert response.status_code == 404
            serialized = json.dumps(response.json(), ensure_ascii=False)
            assert "bravo" not in serialized
            assert _PHYSICAL_B not in serialized


async def test_document_policy_create_query_update_and_source_conflict(tmp_path):
    app, store = _app(tmp_path)
    async with _client(app) as client:
        await _configure_kb(client, "workspace")

        incomplete = await client.patch(
            "/v1/knowledge-bases/alpha/documents/doc-1/access",
            json={"policy": "private"},
        )
        assert incomplete.status_code == 422
        assert incomplete.json()["error_code"] == "BAD_REQUEST"

        created = await _configure_document(client)
        assert created["kb_id"] == "alpha"
        assert created["document_id"] == "doc-1"
        assert created["source"] == "report.pdf"
        assert created["policy"] == "private"
        assert _PHYSICAL_A not in json.dumps(created)

        updated = await client.patch(
            "/v1/knowledge-bases/alpha/documents/doc-1/access",
            json={"policy": "inherit"},
        )
        assert updated.status_code == 200
        assert updated.json()["source"] == "report.pdf"
        assert updated.json()["policy"] == "inherit"

        fetched = await client.get("/v1/knowledge-bases/alpha/documents/doc-1/access")
        assert fetched.status_code == 200
        assert fetched.json() == updated.json()

        conflict = await client.patch(
            "/v1/knowledge-bases/alpha/documents/doc-2/access",
            json={"policy": "private", "source": "report.pdf"},
        )
        assert conflict.status_code == 409
        assert conflict.json()["error_code"] == "BAD_REQUEST"

        absent = await client.get("/v1/knowledge-bases/alpha/documents/missing/access")
        assert absent.status_code == 404

    assert (
        store.get_document_policy("tenant-a", _PHYSICAL_A, "doc-1")["policy"]
        == "inherit"
    )


class _MembershipStore:
    def __init__(self, members: set[str], *, fail: bool = False) -> None:
        self.members = members
        self.fail = fail
        self.calls: list[tuple[str, str]] = []

    def membership(self, workspace_id: str, user_id: str):
        self.calls.append((workspace_id, user_id))
        if self.fail:
            raise RuntimeError("directory unavailable")
        if user_id not in self.members:
            return None
        return {
            "workspace_id": workspace_id,
            "user_id": user_id,
            "member_id": f"mem-{user_id}",
            "status": "active",
        }


class _GetMemberStore:
    def __init__(self, members: set[str]) -> None:
        self.members = members

    def get_member(self, workspace_id: str, member_id: str):
        if member_id not in self.members:
            raise KeyError(member_id)
        return {"workspace_id": workspace_id, "member_id": member_id}


@pytest.mark.parametrize(
    "auth_store",
    [_MembershipStore({"bob"}), _GetMemberStore({"bob"})],
)
async def test_session_grants_require_membership_and_kb_document_lists_are_separate(
    tmp_path, auth_store
):
    app, _store = _app(tmp_path, principal=_principal(session=True))
    app.state.auth_store = auth_store
    async with _client(app) as client:
        await _configure_kb(client)
        await _configure_document(client)

        kb_grant = await client.post(
            "/v1/knowledge-bases/alpha/access/grants",
            json={"subject_id": "bob", "role": "viewer"},
        )
        assert kb_grant.status_code == 200
        assert kb_grant.json()["document_id"] is None
        assert kb_grant.json()["kb_id"] == "alpha"

        document_grant = await client.post(
            "/v1/knowledge-bases/alpha/documents/doc-1/access/grants",
            json={"subject_id": "bob", "role": "reviewer"},
        )
        assert document_grant.status_code == 200
        assert document_grant.json()["document_id"] == "doc-1"

        kb_list = await client.get("/v1/knowledge-bases/alpha/access/grants")
        assert kb_list.status_code == 200
        assert [row["document_id"] for row in kb_list.json()["grants"]] == [None]

        document_list = await client.get(
            "/v1/knowledge-bases/alpha/documents/doc-1/access/grants"
        )
        assert document_list.status_code == 200
        assert [row["document_id"] for row in document_list.json()["grants"]] == [
            "doc-1"
        ]
        assert _PHYSICAL_A not in json.dumps(document_list.json())

        revoked = await client.delete(
            "/v1/knowledge-bases/alpha/documents/doc-1/access/grants/bob"
        )
        assert revoked.status_code == 204
        revoked_again = await client.delete(
            "/v1/knowledge-bases/alpha/documents/doc-1/access/grants/bob"
        )
        assert revoked_again.status_code == 404


async def test_session_cannot_grant_nonmember_and_directory_error_is_503(tmp_path):
    membership = _MembershipStore(set())
    app, store = _app(tmp_path, principal=_principal(session=True))
    app.state.auth_store = membership
    async with _client(app) as client:
        await _configure_kb(client)
        denied = await client.post(
            "/v1/knowledge-bases/alpha/access/grants",
            json={"subject_id": "outsider", "role": "viewer"},
        )
        assert denied.status_code == 422
        assert denied.json()["error_code"] == "BAD_REQUEST"
        assert store.list_grants("tenant-a", _PHYSICAL_A) == []

        membership.fail = True
        unavailable = await client.post(
            "/v1/knowledge-bases/alpha/access/grants",
            json={"subject_id": "outsider", "role": "viewer"},
        )
        assert unavailable.status_code == 503
        assert unavailable.json()["error_code"] == "INTERNAL_ERROR"


async def test_api_key_principal_keeps_legacy_grant_behavior_without_membership(
    tmp_path,
):
    membership = _MembershipStore(set(), fail=True)
    app, _store = _app(tmp_path, principal=_principal(session=False))
    async with _client(app) as client:
        await _configure_kb(client)
        granted = await client.post(
            "/v1/knowledge-bases/alpha/access/grants",
            json={"subject_id": "external-service", "role": "viewer"},
        )
    assert granted.status_code == 200
    assert membership.calls == []


async def test_service_api_key_uses_membership_incarnation_when_directory_exists(
    tmp_path,
):
    membership = _MembershipStore({"bob"})
    app, store = _app(tmp_path, principal=_principal(session=False))
    app.state.auth_store = membership
    async with _client(app) as client:
        await _configure_kb(client)
        granted = await client.post(
            "/v1/knowledge-bases/alpha/access/grants",
            json={"subject_id": "bob", "role": "viewer"},
        )
        denied = await client.post(
            "/v1/knowledge-bases/alpha/access/grants",
            json={"subject_id": "outsider", "role": "viewer"},
        )

    assert granted.status_code == 200
    assert denied.status_code == 422
    store.revoke_all_subject_grants("tenant-a", "bob", membership_id="mem-bob")
    assert store.is_membership_revoked("tenant-a", "bob", "mem-bob")


async def test_revoked_membership_incarnation_rejects_delayed_http_grant(tmp_path):
    membership = _MembershipStore({"bob"})
    app, store = _app(tmp_path, principal=_principal(session=True))
    app.state.auth_store = membership
    async with _client(app) as client:
        await _configure_kb(client)
        store.revoke_all_subject_grants("tenant-a", "bob", membership_id="mem-bob")
        delayed = await client.post(
            "/v1/knowledge-bases/alpha/access/grants",
            json={"subject_id": "bob", "role": "viewer"},
        )

    assert delayed.status_code == 409
    assert delayed.json()["error_code"] == "AUTH_CONFLICT"
    assert store.list_grants("tenant-a", _PHYSICAL_A, subject_id="bob") == []


async def test_document_grant_requires_registered_document_policy(tmp_path):
    app, _store = _app(tmp_path)
    async with _client(app) as client:
        await _configure_kb(client)
        response = await client.post(
            "/v1/knowledge-bases/alpha/documents/unknown/access/grants",
            json={"subject_id": "bob", "role": "viewer"},
        )
        listed = await client.get(
            "/v1/knowledge-bases/alpha/documents/unknown/access/grants"
        )
        revoked = await client.delete(
            "/v1/knowledge-bases/alpha/documents/unknown/access/grants/bob"
        )
    assert response.status_code == 404
    assert response.json()["error_code"] == "DOCUMENT_NOT_FOUND"
    assert listed.status_code == 404
    assert listed.json()["error_code"] == "DOCUMENT_NOT_FOUND"
    assert revoked.status_code == 404
    assert revoked.json()["error_code"] == "DOCUMENT_NOT_FOUND"


class _FailingStore:
    def get_kb_policy(self, *args):
        raise RuntimeError("database unavailable")


class _WrongScopeStore:
    def get_kb_policy(self, *args):
        return {
            "tenant_id": "tenant-b",
            "kb_id": _PHYSICAL_B,
            "owner_id": "owner-b",
            "policy": "workspace",
            "acl_epoch": 1,
        }

    def acl_epoch(self, *args):
        return 1


@pytest.mark.parametrize("store", [None, _FailingStore(), _WrongScopeStore()])
async def test_missing_failing_or_cross_scope_store_fails_closed(tmp_path, store):
    app, _store = _app(tmp_path, store=store)
    async with _client(app) as client:
        response = await client.get("/v1/knowledge-bases/alpha/access")
    assert response.status_code == 503
    assert response.json()["error_code"] == "INTERNAL_ERROR"
    assert _PHYSICAL_A not in json.dumps(response.json())
    assert _PHYSICAL_B not in json.dumps(response.json())


@pytest.mark.parametrize(
    "path,payload",
    [
        ("/v1/knowledge-bases/alpha/access", {"policy": "public"}),
        (
            "/v1/knowledge-bases/alpha/access",
            {"policy": "private", "unexpected": True},
        ),
        (
            "/v1/knowledge-bases/alpha/access/grants",
            {"subject_id": " ", "role": "viewer"},
        ),
        (
            "/v1/knowledge-bases/alpha/access/grants",
            {"subject_id": "bob", "role": "superuser"},
        ),
        (
            "/v1/knowledge-bases/alpha/documents/doc/access",
            {"policy": "inherit", "source": " report.pdf"},
        ),
    ],
)
async def test_request_models_forbid_unknown_or_noncanonical_values(
    tmp_path, path, payload
):
    app, _store = _app(tmp_path)
    async with _client(app) as client:
        if "/grants" not in path:
            response = await client.patch(path, json=payload)
        else:
            response = await client.post(path, json=payload)
    assert response.status_code == 422
