from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from cogdoc.api.app import create_app
from cogdoc.api.ingest import KnowledgeBaseRegistry


@pytest.fixture
def anyio_backend():
    return "asyncio"


class _MigrationManager:
    def __init__(self):
        self.runner = SimpleNamespace(plan=self.plan)
        self.records = []

    def plan(self, records):
        return {
            "schema_version": "v1",
            "total": len(records),
            "needs_migration": len(records),
            "items": [
                {
                    "kb_id": row["kb_id"],
                    "storage_id": row["storage_id"],
                    "needs_migration": True,
                }
                for row in records
            ],
        }

    def submit(self, records, *, include_current=False):
        self.records = records
        return {
            "schema_version": "v1",
            "run_id": "a" * 32,
            "status": "queued",
            "authorized_storage_ids": [row["storage_id"] for row in records],
            "items": [],
        }

    def get(self, run_id):
        return {
            "schema_version": "v1",
            "run_id": run_id,
            "status": "completed",
            "authorized_storage_ids": ["kb"],
            "items": [{"kb_id": "kb", "storage_id": "kb", "status": "succeeded"}],
        }


@pytest.mark.anyio
async def test_index_migration_routes_scan_submit_and_hide_physical_scope(tmp_path):
    registry = KnowledgeBaseRegistry(
        registry_path=str(tmp_path / "registry.json"),
        source_dir_for=lambda storage_id: str(tmp_path / storage_id / "source"),
    )
    app = create_app(
        api_keys={"normal"},
        eval_review_api_keys={"review"},
        kb_registry=registry,
    )
    original = app.state.index_migration_manager
    manager = _MigrationManager()
    app.state.index_migration_manager = manager
    app.state.kb_registry.create("kb")
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            unauthorized = await client.get(
                "/v1/index-migrations/scan", headers={"X-API-Key": "normal"}
            )
            assert unauthorized.status_code == 403

            scan = await client.get(
                "/v1/index-migrations/scan", headers={"X-API-Key": "review"}
            )
            assert scan.status_code == 200
            assert scan.json()["needs_migration"] == 1
            assert "storage_id" not in scan.text

            started = await client.post(
                "/v1/index-migrations",
                headers={"X-API-Key": "review"},
                json={"kb_ids": ["kb"]},
            )
            assert started.status_code == 202
            assert started.json()["status"] == "queued"
            assert "authorized_storage_ids" not in started.json()

            status = await client.get(
                f"/v1/index-migrations/{'a' * 32}",
                headers={"X-API-Key": "review"},
            )
            assert status.status_code == 200
            assert status.json()["items"][0]["kb_id"] == "kb"
    finally:
        original.shutdown(wait=True)
        app.state.index_jobs.shutdown(wait=True)
        app.state.state_runtime.close()
