from __future__ import annotations

from httpx import ASGITransport, AsyncClient
import pytest

from cogdoc.api.app import create_app
from cogdoc.api.audit import AuditStore
from cogdoc.api.ingest import IndexJobManager, KnowledgeBaseRegistry
from cogdoc.api.tenant_quota import TenantQuotaManager, TenantQuotaPolicy


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _app(tmp_path, monkeypatch, *, max_kbs=0):
    import cogdoc.api.app as app_module

    monkeypatch.setattr(app_module, "configure_logging", lambda: None)
    monkeypatch.setenv("COGDOC_TENANT_MAX_KNOWLEDGE_BASES", str(max_kbs))

    def source_dir_for(storage_id):
        return str(tmp_path / "kb" / storage_id / "sources")

    registry = KnowledgeBaseRegistry(
        registry_path=str(tmp_path / "registry.json"),
        source_dir_for=source_dir_for,
    )
    jobs = IndexJobManager(
        ingest_fn=lambda kb, source: None,
        source_dir_for=source_dir_for,
        kb_exists=registry.exists,
    )
    app = create_app(
        kb_registry=registry,
        index_jobs=jobs,
        audit_store=AuditStore(tmp_path / "audit.jsonl"),
        api_principals={
            "a-owner": {
                "tenant_id": "tenant-a",
                "subject_id": "alice",
                "role": "owner",
            },
            "a-viewer": {
                "tenant_id": "tenant-a",
                "subject_id": "amy",
                "role": "viewer",
            },
            "b-owner": {
                "tenant_id": "tenant-b",
                "subject_id": "bob",
                "role": "owner",
            },
        },
        derived_knowledge_index_clearer=lambda _storage_id: None,
    )
    if max_kbs:
        app.state.tenant_quota = TenantQuotaManager(
            registry, TenantQuotaPolicy(max_knowledge_bases=max_kbs)
        )
    return app, registry


def _headers(key):
    return {"X-API-Key": key}


@pytest.mark.anyio
async def test_same_kb_slug_is_tenant_isolated_end_to_end(tmp_path, monkeypatch):
    app, registry = _app(tmp_path, monkeypatch)
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            first = await client.post(
                "/v1/knowledge-bases",
                json={"kb_id": "shared"},
                headers=_headers("a-owner"),
            )
            second = await client.post(
                "/v1/knowledge-bases",
                json={"kb_id": "shared"},
                headers=_headers("b-owner"),
            )
            a_list = await client.get(
                "/v1/knowledge-bases", headers=_headers("a-owner")
            )
            b_list = await client.get(
                "/v1/knowledge-bases", headers=_headers("b-owner")
            )
            await client.delete(
                "/v1/knowledge-bases/shared", headers=_headers("a-owner")
            )
            a_missing = await client.get(
                "/v1/knowledge-bases/shared", headers=_headers("a-owner")
            )
            b_still_there = await client.get(
                "/v1/knowledge-bases/shared", headers=_headers("b-owner")
            )

    assert first.status_code == second.status_code == 201
    assert first.json()["tenant_id"] == "tenant-a"
    assert second.json()["tenant_id"] == "tenant-b"
    assert [row["kb_id"] for row in a_list.json()] == ["shared"]
    assert [row["kb_id"] for row in b_list.json()] == ["shared"]
    assert a_missing.status_code == 404
    assert b_still_there.status_code == 200
    assert registry.resolve("shared", "tenant-a") is None
    assert registry.resolve("shared", "tenant-b") is not None


@pytest.mark.anyio
async def test_role_quota_tenant_metadata_and_audit_are_enforced(tmp_path, monkeypatch):
    app, _ = _app(tmp_path, monkeypatch, max_kbs=1)
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            viewer_write = await client.post(
                "/v1/knowledge-bases",
                json={"kb_id": "denied"},
                headers=_headers("a-viewer"),
            )
            created = await client.post(
                "/v1/knowledge-bases",
                json={"kb_id": "one"},
                headers=_headers("a-owner"),
            )
            over_quota = await client.post(
                "/v1/knowledge-bases",
                json={"kb_id": "two"},
                headers=_headers("a-owner"),
            )
            tenant = await client.get("/v1/tenant", headers=_headers("a-owner"))
            viewer_audit = await client.get(
                "/v1/audit-events", headers=_headers("a-viewer")
            )
            audit = await client.get(
                "/v1/audit-events", headers=_headers("a-owner")
            )
            tenant_b_audit = await client.get(
                "/v1/audit-events", headers=_headers("b-owner")
            )

    assert viewer_write.status_code == 403
    assert created.status_code == 201
    assert over_quota.status_code == 409
    assert over_quota.json()["error_code"] == "TENANT_QUOTA_EXCEEDED"
    assert tenant.json()["tenant_id"] == "tenant-a"
    assert tenant.json()["role"] == "owner"
    assert tenant.json()["quota"]["usage"]["knowledge_bases"] == 1
    assert viewer_audit.status_code == 403
    assert audit.status_code == 200
    assert all(event["tenant"] == "tenant-a" for event in audit.json()["events"])
    assert tenant_b_audit.json()["events"] == []


@pytest.mark.anyio
async def test_corrupt_audit_log_rejects_mutation_before_state_change(
    tmp_path, monkeypatch
):
    app, registry = _app(tmp_path, monkeypatch)
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            first = await client.get("/v1/tenant", headers=_headers("a-owner"))
            assert first.status_code == 200
            audit_path = app.state.audit_store.path
            payload = audit_path.read_bytes()
            audit_path.write_bytes(payload.replace(b'"outcome":"allowed"', b'"outcome":"altered"', 1))

            rejected = await client.post(
                "/v1/knowledge-bases",
                json={"kb_id": "must-not-exist"},
                headers=_headers("a-owner"),
            )

    assert rejected.status_code == 503
    assert registry.resolve("must-not-exist", "tenant-a") is None
