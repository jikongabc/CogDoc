import asyncio
import time
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from cogdoc.api.app import create_app
from cogdoc.api.ingest import IndexJobManager, KnowledgeBaseRegistry
from cogdoc.connectors.connection_store import ConnectionStore
from cogdoc.connectors.sync_store import ConnectorSyncStore
from cogdoc.service.source_catalog import SourceCatalog


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_connection_api_runs_local_sync_and_exposes_bounded_status(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("cogdoc.api.app.configure_logging", lambda: None)
    db = str(tmp_path / "connector.db")
    source_root = tmp_path / "provider"
    source_root.mkdir()
    (source_root / "guide.md").write_text(
        "# Guide\n\nEnough source content for a connector integration test.",
        encoding="utf-8",
    )

    def source_dir_for(kb_id):
        return str(tmp_path / "kb" / kb_id / "sources")

    registry = KnowledgeBaseRegistry(
        registry_path=str(tmp_path / "registry.json"), source_dir_for=source_dir_for
    )
    jobs = IndexJobManager(
        ingest_fn=lambda *_: SimpleNamespace(
            document_count=1, chunk_count=1, ocr_summary={}
        ),
        source_dir_for=source_dir_for,
        kb_exists=registry.exists,
    )
    connections = ConnectionStore(db)
    sync_jobs = ConnectorSyncStore(db)
    catalog = SourceCatalog(db)
    app = create_app(
        kb_registry=registry,
        index_jobs=jobs,
        connection_store=connections,
        connector_sync_store=sync_jobs,
        source_catalog=catalog,
    )

    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            assert (
                await client.post("/v1/knowledge-bases", json={"kb_id": "docs"})
            ).status_code == 201
            created = await client.post(
                "/v1/knowledge-bases/docs/connections",
                json={
                    "connector_type": "local-directory",
                    "name": "Local docs",
                    "config": {"root": str(source_root)},
                    "secret_env": {},
                    "workspace_visible": True,
                },
            )
            assert created.status_code == 201
            connection = created.json()
            assert "secret_env" not in connection and connection["secret_fields"] == []

            started = await client.post(
                f"/v1/knowledge-bases/docs/connections/{connection['connection_id']}/sync"
            )
            assert started.status_code == 202
            job_id = started.json()["job_id"]
            deadline = time.monotonic() + 5
            status = "pending"
            while time.monotonic() < deadline and status in {
                "pending",
                "running",
                "committing",
                "retry_wait",
            }:
                await asyncio.sleep(0.03)
                response = await client.get(
                    f"/v1/knowledge-bases/docs/sync-jobs/{job_id}"
                )
                status = response.json()["status"]
            assert status == "succeeded", response.json()
            assert "lease_token" not in response.json()
            listed = await client.get("/v1/knowledge-bases/docs/sync-jobs")
            assert listed.json()["jobs"][0]["documents_fetched"] == 1

    assert len(catalog.list_sources("default", "docs")) == 1
    materialized = list(
        (tmp_path / "kb" / "docs" / "sources" / ".connections").rglob("*.md")
    )
    assert len(materialized) == 1
    connections.close()
    sync_jobs.close()
    catalog.close()
