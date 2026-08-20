import asyncio
import hashlib
from threading import Event
import time
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from cogdoc.api.app import create_app
from cogdoc.api.ingest import IndexJobManager, KnowledgeBaseRegistry
from cogdoc.api.offload import run_sync
from cogdoc.connectors.connection_store import ConnectionStore
from cogdoc.connectors.sync_store import ConnectorSyncStore
from cogdoc.service.source_artifact_store import SourceArtifactStore
from cogdoc.service.source_catalog import SourceCatalog
from cogdoc.service.source_model import SourceDocument


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def _wait_for_sync(client, kb_id, job_id):
    deadline = time.monotonic() + 5
    response = None
    while time.monotonic() < deadline:
        response = await client.get(f"/v1/knowledge-bases/{kb_id}/sync-jobs/{job_id}")
        if response.json()["status"] not in {
            "pending",
            "running",
            "committing",
            "retry_wait",
        }:
            return response.json()
        await asyncio.sleep(0.03)
    raise AssertionError(response.text if response is not None else "sync timed out")


@pytest.mark.anyio
async def test_source_catalog_versions_diff_download_and_recoverable_delete(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("cogdoc.api.app.configure_logging", lambda: None)
    db_path = str(tmp_path / "state.db")
    provider = tmp_path / "provider"
    provider.mkdir()
    document = provider / "guide.md"
    document.write_text("# Guide\n\nfirst line\n", encoding="utf-8")

    def source_dir_for(kb_id):
        return str(tmp_path / "kb" / kb_id / "sources")

    registry = KnowledgeBaseRegistry(
        registry_path=str(tmp_path / "registry.json"),
        source_dir_for=source_dir_for,
    )
    index_jobs = IndexJobManager(
        ingest_fn=lambda *_: SimpleNamespace(
            document_count=1, chunk_count=1, ocr_summary={}
        ),
        source_dir_for=source_dir_for,
        kb_exists=registry.exists,
    )
    app = create_app(
        kb_registry=registry,
        index_jobs=index_jobs,
        connection_store=ConnectionStore(db_path),
        connector_sync_store=ConnectorSyncStore(db_path),
        source_catalog=SourceCatalog(db_path),
        source_artifact_store=SourceArtifactStore(tmp_path / "artifacts"),
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
                    "name": "Local handbook",
                    "config": {"root": str(provider)},
                },
            )
            connection_id = created.json()["connection_id"]

            first = await client.post(
                f"/v1/knowledge-bases/docs/connections/{connection_id}/sync"
            )
            assert (await _wait_for_sync(client, "docs", first.json()["job_id"]))[
                "status"
            ] == "succeeded"
            catalog = await client.get("/v1/knowledge-bases/docs/source-catalog")
            assert catalog.status_code == 200, catalog.text
            source = catalog.json()["sources"][0]
            source_id = source["source_id"]
            first_version = source["version_id"]
            assert source["connection_id"] == connection_id
            assert source["health_status"] == "healthy"
            assert source["document_id"].startswith("doc-")

            document.write_text(
                "# Guide\n\nfirst line\nsecond line\n", encoding="utf-8"
            )
            second = await client.post(
                f"/v1/knowledge-bases/docs/connections/{connection_id}/sync"
            )
            assert (await _wait_for_sync(client, "docs", second.json()["job_id"]))[
                "status"
            ] == "succeeded"
            versions = await client.get(
                f"/v1/knowledge-bases/docs/source-catalog/{source_id}/versions"
            )
            assert versions.status_code == 200, versions.text
            rows = versions.json()["versions"]
            assert len(rows) == 2
            assert all(row["artifact_available"] for row in rows)
            current = next(row for row in rows if row["is_current"])
            current_version = current["version_id"]
            assert current_version != first_version

            diff = await client.get(
                f"/v1/knowledge-bases/docs/source-catalog/{source_id}/diff",
                params={
                    "from_version_id": first_version,
                    "to_version_id": current_version,
                },
            )
            assert diff.status_code == 200, diff.text
            assert diff.json()["kind"] == "text"
            assert diff.json()["added_lines"] == 1
            assert "+second line" in diff.json()["diff"]
            assert "tenant_id" not in diff.json()["from_version"]
            assert "kb_id" not in diff.json()["from_version"]
            assert "tenant_id" not in diff.json()["to_version"]
            assert "kb_id" not in diff.json()["to_version"]

            download = await client.get(
                f"/v1/knowledge-bases/docs/source-catalog/{source_id}/versions/"
                f"{first_version}/content"
            )
            assert download.status_code == 200
            assert download.content == b"# Guide\n\nfirst line\n"
            assert download.headers["x-cogdoc-content-sha256"]

            deletion = await client.delete(
                f"/v1/knowledge-bases/docs/source-catalog/{source_id}/versions/"
                f"{first_version}/artifact"
            )
            assert deletion.status_code == 200, deletion.text
            recovery_token = deletion.json()["recovery_token"]
            assert (
                await client.get(
                    f"/v1/knowledge-bases/docs/source-catalog/{source_id}/versions/"
                    f"{first_version}/content"
                )
            ).status_code == 404
            restored = await client.post(
                f"/v1/knowledge-bases/docs/source-artifacts/{recovery_token}/restore"
            )
            assert restored.status_code == 200
            assert restored.json()["version_id"] == first_version
            assert (
                await client.delete(
                    f"/v1/knowledge-bases/docs/source-catalog/{source_id}/versions/"
                    f"{current_version}/artifact"
                )
            ).status_code == 409
            usage = await client.get("/v1/knowledge-bases/docs/source-artifacts/usage")
            assert usage.status_code == 200
            assert usage.json()["active_versions"] == 2

            # The artifact store has its own content-addressed integrity
            # anchor, while delivery additionally requires agreement with the
            # durable source catalog. A catalog-only mutation must therefore
            # fail closed for both content and diffs.
            app.state.source_catalog._conn.execute(
                "UPDATE source_catalog_versions SET content_sha256=? "
                "WHERE source_id=? AND version_id=?",
                ("0" * 64, source_id, current_version),
            )
            mismatched_download = await client.get(
                f"/v1/knowledge-bases/docs/source-catalog/{source_id}/versions/"
                f"{current_version}/content"
            )
            assert mismatched_download.status_code == 503
            mismatched_diff = await client.get(
                f"/v1/knowledge-bases/docs/source-catalog/{source_id}/diff",
                params={
                    "from_version_id": first_version,
                    "to_version_id": current_version,
                },
            )
            assert mismatched_diff.status_code == 503


@pytest.mark.anyio
async def test_artifact_hashing_uses_an_isolated_reopenable_executor(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("cogdoc.api.app.configure_logging", lambda: None)
    db_path = str(tmp_path / "state.db")

    def source_dir_for(kb_id):
        return str(tmp_path / "kb" / kb_id / "sources")

    registry = KnowledgeBaseRegistry(
        registry_path=str(tmp_path / "registry.json"),
        source_dir_for=source_dir_for,
    )
    index_jobs = IndexJobManager(
        ingest_fn=lambda *_: SimpleNamespace(
            document_count=0, chunk_count=0, ocr_summary={}
        ),
        source_dir_for=source_dir_for,
        kb_exists=registry.exists,
    )
    catalog = SourceCatalog(db_path)
    artifacts = SourceArtifactStore(tmp_path / "artifacts")
    app = create_app(
        kb_registry=registry,
        index_jobs=index_jobs,
        connection_store=ConnectionStore(db_path),
        connector_sync_store=ConnectorSyncStore(db_path),
        source_catalog=catalog,
        source_artifact_store=artifacts,
        offload_workers=1,
        artifact_io_workers=1,
        close_state_runtime_on_shutdown=False,
    )

    first_artifact_executor = app.state.source_artifact_executor
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            created = await client.post(
                "/v1/knowledge-bases", json={"kb_id": "docs"}
            )
            assert created.status_code == 201, created.text
            storage_id = str(registry.resolve("docs", "default")["storage_id"])
            content = b"isolated-artifact-io" * 10_000
            digest = hashlib.sha256(content).hexdigest()
            document = SourceDocument.create(
                connector_type="git",
                external_id="isolated/readme.md",
                display_name="readme.md",
                content_sha256=digest,
                byte_size=len(content),
            )
            catalog.upsert("default", storage_id, document)
            artifacts.put(
                "default",
                storage_id,
                document.source_id,
                document.version.version_id,
                content,
                content_sha256=digest,
                media_type="text/markdown",
                display_name="readme.md",
                created_at=document.version.fetched_at,
            )

            verification_started = Event()
            allow_verification = Event()
            original_open_verified = artifacts.open_verified

            def blocked_open_verified(*args, **kwargs):
                verification_started.set()
                if not allow_verification.wait(timeout=5):
                    raise TimeoutError("test did not release artifact verification")
                return original_open_verified(*args, **kwargs)

            monkeypatch.setattr(artifacts, "open_verified", blocked_open_verified)
            download = asyncio.create_task(
                client.get(
                    "/v1/knowledge-bases/docs/source-catalog/"
                    f"{document.source_id}/versions/"
                    f"{document.version.version_id}/content"
                )
            )
            # Poll without occupying the sole shared offload worker: the
            # download must use it briefly for the catalog lookup before it
            # reaches the isolated artifact executor. A bare
            # ``asyncio.to_thread`` is intentionally avoided because this
            # environment can lose its completion wakeup.
            event_deadline = asyncio.get_running_loop().time() + 2
            while not verification_started.is_set():
                assert asyncio.get_running_loop().time() < event_deadline
                await asyncio.sleep(0.01)
            try:
                # The sole shared offload worker remains available while the
                # sole artifact worker is occupied by a full-file hash.
                result = await asyncio.wait_for(
                    run_sync(app.state.offload_executor, lambda: "control-ready"),
                    timeout=1,
                )
                assert result == "control-ready"
            finally:
                allow_verification.set()
            response = await download
            assert response.status_code == 200, response.text
            assert response.content == content

    assert app.state.source_artifact_executor_shutdown is True
    async with app.router.lifespan_context(app):
        assert app.state.source_artifact_executor is not first_artifact_executor
        assert (
            await run_sync(app.state.source_artifact_executor, lambda: "reopened")
            == "reopened"
        )
