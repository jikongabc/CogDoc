from __future__ import annotations

import asyncio
import sqlite3

from httpx import ASGITransport, AsyncClient
import pytest

from cogdoc.api.app import create_app
from cogdoc.api.audit import AuditStore
from cogdoc.api.audit_exports import AuditExportManager, AuditExportStore


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_audit_export_api_is_admin_only_and_tenant_scoped(tmp_path, monkeypatch):
    import cogdoc.api.app as app_module

    monkeypatch.setattr(app_module, "configure_logging", lambda: None)
    audit = AuditStore(tmp_path / "audit.jsonl")
    export_store = AuditExportStore(tmp_path / "state.db", tmp_path / "exports")
    manager = AuditExportManager(export_store, audit)
    app = create_app(
        audit_store=audit,
        audit_export_manager=manager,
        api_principals={
            "owner-a": {"tenant_id": "a", "subject_id": "alice", "role": "owner"},
            "viewer-a": {"tenant_id": "a", "subject_id": "amy", "role": "viewer"},
            "owner-b": {"tenant_id": "b", "subject_id": "bob", "role": "owner"},
        },
    )
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            denied = await client.post(
                "/v1/audit-events/exports",
                json={},
                headers={"X-API-Key": "viewer-a"},
            )
            created = await client.post(
                "/v1/audit-events/exports",
                json={},
                headers={"X-API-Key": "owner-a"},
            )
            assert denied.status_code == 403
            assert created.status_code == 202
            job_id = created.json()["job_id"]
            for _ in range(100):
                detail = await client.get(
                    f"/v1/audit-events/exports/{job_id}",
                    headers={"X-API-Key": "owner-a"},
                )
                if detail.json()["status"] == "succeeded":
                    break
                await asyncio.sleep(0.01)
            assert detail.json()["status"] == "succeeded"
            hidden = await client.get(
                f"/v1/audit-events/exports/{job_id}",
                headers={"X-API-Key": "owner-b"},
            )
            assert hidden.status_code == 404
            content = await client.get(
                f"/v1/audit-events/exports/{job_id}/content",
                headers={"X-API-Key": "owner-a"},
            )
            assert content.status_code == 200
            assert content.headers["cache-control"] == "no-store"
            assert b'"tenant":"a"' in content.content
            assert b'"tenant":"b"' not in content.content
    export_store.close()


@pytest.mark.anyio
async def test_owned_audit_export_store_closes_with_application(tmp_path, monkeypatch):
    import cogdoc.api.app as app_module

    monkeypatch.setattr(app_module, "configure_logging", lambda: None)
    audit = AuditStore(tmp_path / "audit.jsonl")
    export_store = AuditExportStore(tmp_path / "state.db", tmp_path / "exports")
    manager = AuditExportManager(export_store, audit)
    app = create_app(audit_store=audit, audit_export_manager=manager)
    app.state.close_audit_export_store_on_shutdown = True

    async with app.router.lifespan_context(app):
        assert export_store.check()

    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        export_store.check()
    manager.reopen()
    try:
        assert export_store.check()
    finally:
        manager.shutdown()
        export_store.close()
