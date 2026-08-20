import asyncio
import time
from threading import Event, Lock
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from cogdoc.api.app import create_app
from cogdoc.api.ingest import IndexJobManager, KnowledgeBaseRegistry
from cogdoc.api.resource_access import ResourceAccessStore
from cogdoc.api.routes.connections import _owned_connection
from cogdoc.connectors.connection_store import ConnectionStore
from cogdoc.connectors.sync_store import ConnectorSyncStore
from cogdoc.service.source_artifact_store import SourceArtifactStore
from cogdoc.service.source_catalog import SourceCatalog
from cogdoc.tools.chunk_identity import build_document_id


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_owned_connection_requires_both_tenant_and_storage_scope():
    row = {"connection_id": "c1", "tenant_id": "tenant-b", "kb_id": "shared"}
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                connection_store=SimpleNamespace(get=lambda _connection_id: row)
            )
        )
    )
    scope = SimpleNamespace(tenant_id="tenant-a", storage_id="shared")

    assert _owned_connection(request, scope, "c1") is None


@pytest.mark.anyio
async def test_slow_connection_cleanup_does_not_hold_global_reference_lock(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("cogdoc.api.app.configure_logging", lambda: None)
    database = str(tmp_path / "connector.db")

    def source_dir_for(storage_id):
        return str(tmp_path / "kb" / storage_id / "sources")

    registry = KnowledgeBaseRegistry(
        registry_path=str(tmp_path / "registry.json"), source_dir_for=source_dir_for
    )
    jobs = IndexJobManager(
        ingest_fn=lambda *_: SimpleNamespace(
            document_count=0, chunk_count=0, ocr_summary={}
        ),
        source_dir_for=source_dir_for,
        kb_exists=registry.exists,
    )
    connections = ConnectionStore(database)
    sync_jobs = ConnectorSyncStore(database)
    catalog = SourceCatalog(database)
    access = ResourceAccessStore(tmp_path / "access.db")
    app = create_app(
        kb_registry=registry,
        index_jobs=jobs,
        connection_store=connections,
        connector_sync_store=sync_jobs,
        source_catalog=catalog,
        resource_access_store=access,
        close_state_runtime_on_shutdown=False,
        offload_workers=1,
        connector_cleanup_workers=1,
    )

    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            for kb_id in ("docs", "other"):
                assert (
                    await client.post("/v1/knowledge-bases", json={"kb_id": kb_id})
                ).status_code == 201
            created = []
            for kb_id in ("docs", "other"):
                response = await client.post(
                    f"/v1/knowledge-bases/{kb_id}/connections",
                    json={
                        "connector_type": "local-directory",
                        "name": f"{kb_id} connection",
                        "config": {"root": str(tmp_path)},
                        "secret_env": {},
                    },
                )
                assert response.status_code == 201, response.text
                created.append(response.json()["connection_id"])

            index_waiting = Event()
            release_index = Event()
            original_submit = jobs.submit
            original_get = jobs.get

            def submit_index(kb_id):
                if kb_id == "docs":
                    return {"job_id": "slow-delete-index"}
                return original_submit(kb_id)

            def read_index(job_id):
                if job_id == "slow-delete-index":
                    index_waiting.set()
                    return {
                        "status": "succeeded" if release_index.is_set() else "running"
                    }
                return original_get(job_id)

            monkeypatch.setattr(jobs, "submit", submit_index)
            monkeypatch.setattr(jobs, "get", read_index)
            deletion = asyncio.create_task(
                client.delete(f"/v1/knowledge-bases/docs/connections/{created[0]}")
            )
            try:
                deadline = time.monotonic() + 2
                while not index_waiting.is_set() and time.monotonic() < deadline:
                    await asyncio.sleep(0.01)
                assert index_waiting.is_set()

                unrelated = await asyncio.wait_for(
                    client.patch(
                        f"/v1/knowledge-bases/other/connections/{created[1]}",
                        json={"enabled": False},
                    ),
                    timeout=1,
                )
                assert unrelated.status_code == 200, unrelated.text
                fenced = await asyncio.wait_for(
                    client.patch(
                        f"/v1/knowledge-bases/docs/connections/{created[0]}",
                        json={"enabled": True},
                    ),
                    timeout=1,
                )
                assert fenced.status_code == 409, fenced.text
            finally:
                release_index.set()

            deleted = await asyncio.wait_for(deletion, timeout=3)
            assert deleted.status_code == 204, deleted.text

    connections.close()
    sync_jobs.close()
    catalog.close()
    access.close()


@pytest.mark.anyio
async def test_concurrent_connection_deletes_cannot_orphan_retirement_fence(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("cogdoc.api.app.configure_logging", lambda: None)
    database = str(tmp_path / "connector.db")

    def source_dir_for(storage_id):
        return str(tmp_path / "kb" / storage_id / "sources")

    registry = KnowledgeBaseRegistry(
        registry_path=str(tmp_path / "registry.json"), source_dir_for=source_dir_for
    )
    jobs = IndexJobManager(
        ingest_fn=lambda *_: SimpleNamespace(
            document_count=0, chunk_count=0, ocr_summary={}
        ),
        source_dir_for=source_dir_for,
        kb_exists=registry.exists,
    )
    connections = ConnectionStore(database)
    sync_jobs = ConnectorSyncStore(database)
    catalog = SourceCatalog(database)
    access = ResourceAccessStore(tmp_path / "access.db")
    app = create_app(
        kb_registry=registry,
        index_jobs=jobs,
        connection_store=connections,
        connector_sync_store=sync_jobs,
        source_catalog=catalog,
        resource_access_store=access,
        close_state_runtime_on_shutdown=False,
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
                    "name": "Concurrent cleanup",
                    "config": {"root": str(tmp_path)},
                    "secret_env": {},
                },
            )
            connection_id = created.json()["connection_id"]
            managed_by = f"connector:{connection_id}"
            retirement_id = "doc-concurrent-delete-race"
            original_cleanup = app.state.connector_connection_cleanup
            ordinal_lock = Lock()
            ordinal = 0
            first_cleaned = Event()
            second_fenced = Event()
            release_second = Event()
            cleanup_errors = []

            def orchestrated_cleanup(*args, **kwargs):
                nonlocal ordinal
                with ordinal_lock:
                    ordinal += 1
                    current = ordinal
                if current == 1:
                    result = original_cleanup(*args, **kwargs)
                    first_cleaned.set()
                    if not second_fenced.wait(timeout=3):
                        raise AssertionError("second delete did not install retirement")
                    return result
                if not first_cleaned.wait(timeout=3):
                    raise AssertionError("first delete did not finish cleanup")
                try:
                    app.state.resource_access_store.begin_document_retirement(
                        "default",
                        "docs",
                        managed_by,
                        (retirement_id,),
                    )
                except Exception as exc:
                    cleanup_errors.append(exc)
                    raise
                second_fenced.set()
                if not release_second.wait(timeout=3):
                    raise AssertionError("second delete was not released")
                app.state.resource_access_store.finish_document_retirement(
                    "default",
                    "docs",
                    managed_by,
                    (retirement_id,),
                )
                return {}

            app.state.connector_connection_cleanup = orchestrated_cleanup
            first = asyncio.create_task(
                client.delete(f"/v1/knowledge-bases/docs/connections/{connection_id}")
            )
            deadline = time.monotonic() + 3
            while not first_cleaned.is_set() and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
            assert first_cleaned.is_set()
            second = asyncio.create_task(
                client.delete(f"/v1/knowledge-bases/docs/connections/{connection_id}")
            )
            deadline = time.monotonic() + 3
            while not second_fenced.is_set() and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
            assert second_fenced.is_set(), (
                repr(cleanup_errors)
                if cleanup_errors
                else second.result().text
                if second.done()
                else "second delete is blocked"
            )

            first_result = await asyncio.wait_for(first, timeout=2)
            assert first_result.status_code == 500, first_result.text
            assert connections.get(connection_id) is not None
            assert app.state.resource_access_store.retiring_document_ids(
                "default", "docs", managed_by
            ) == (retirement_id,)

            release_second.set()
            second_result = await asyncio.wait_for(second, timeout=3)
            assert second_result.status_code == 204, second_result.text
            assert connections.get(connection_id) is None
            assert (
                app.state.resource_access_store.retiring_document_ids(
                    "default", "docs", managed_by
                )
                == ()
            )

    connections.close()
    sync_jobs.close()
    catalog.close()
    access.close()


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
        close_state_runtime_on_shutdown=False,
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
            health = await client.get(
                f"/v1/knowledge-bases/docs/connections/{connection['connection_id']}/health"
            )
            assert health.status_code == 200
            assert health.json()["health_status"] == "healthy"
            assert health.json()["last_success_at"] is not None
            assert health.json()["backlog"] == 0
            health_list = await client.get("/v1/knowledge-bases/docs/connection-health")
            assert (
                health_list.json()["connections"][0]["connection_id"]
                == (connection["connection_id"])
            )

            dead = sync_jobs.create(
                tenant_id="default",
                kb_id="docs",
                connection_id=connection["connection_id"],
                connector_type="local-directory",
            )
            _, lease = sync_jobs.acquire(dead["job_id"], lease_seconds=60)
            dead = sync_jobs.fail(
                dead["job_id"],
                lease,
                error_code="RATE_LIMIT_EXHAUSTED",
                error_message="bounded provider failure",
                retryable=False,
                dead_letter=True,
            )
            sync_jobs.record_health(dead["job_id"], duration_seconds=2.0)
            replay = await client.post(
                f"/v1/knowledge-bases/docs/sync-jobs/{dead['job_id']}/replay"
            )
            assert replay.status_code == 202, replay.text
            assert replay.json()["replay_of"] == dead["job_id"]
            assert (await _wait_for_sync_job(client, "docs", replay.json()["job_id"]))[
                "status"
            ] == "succeeded"

    assert len(catalog.list_sources("default", "docs")) == 1
    materialized = list(
        (tmp_path / "kb" / "docs" / "sources" / ".connections").rglob("*.md")
    )
    assert len(materialized) == 1
    connections.close()
    sync_jobs.close()
    catalog.close()


async def _wait_for_sync_job(client, kb_id, job_id):
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
async def test_connection_routes_work_after_reentering_same_app_lifespan(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("cogdoc.api.app.configure_logging", lambda: None)
    db = str(tmp_path / "connector.db")

    def source_dir_for(kb_id):
        return str(tmp_path / "kb" / kb_id / "sources")

    registry = KnowledgeBaseRegistry(
        registry_path=str(tmp_path / "registry.json"), source_dir_for=source_dir_for
    )
    jobs = IndexJobManager(
        ingest_fn=lambda *_: SimpleNamespace(
            document_count=0, chunk_count=0, ocr_summary={}
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
        close_state_runtime_on_shutdown=False,
    )

    for attempt in range(2):
        async with app.router.lifespan_context(app):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://testserver"
            ) as client:
                if attempt == 0:
                    assert (
                        await client.post("/v1/knowledge-bases", json={"kb_id": "docs"})
                    ).status_code == 201
                response = await client.get("/v1/knowledge-bases/docs/connections")
                assert response.status_code == 200, response.text

    connections.close()
    sync_jobs.close()
    catalog.close()


@pytest.mark.anyio
async def test_connection_delete_keeps_disabled_retry_handle_until_index_cleanup(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("cogdoc.api.app.configure_logging", lambda: None)
    database = str(tmp_path / "connector.db")
    provider = tmp_path / "provider"
    provider.mkdir()
    (provider / "guide.md").write_text(
        "# Guide\n\nMaterialized content removed by connection teardown.",
        encoding="utf-8",
    )

    def source_dir_for(storage_id):
        return str(tmp_path / "kb" / storage_id / "sources")

    registry = KnowledgeBaseRegistry(
        registry_path=str(tmp_path / "registry.json"), source_dir_for=source_dir_for
    )
    jobs = IndexJobManager(
        ingest_fn=lambda *_: SimpleNamespace(
            document_count=0, chunk_count=0, ocr_summary={}
        ),
        source_dir_for=source_dir_for,
        kb_exists=registry.exists,
    )
    connections = ConnectionStore(database)
    sync_jobs = ConnectorSyncStore(database)
    catalog = SourceCatalog(database)
    artifacts = SourceArtifactStore(tmp_path / "artifacts")
    access = ResourceAccessStore(tmp_path / "access.db")
    app = create_app(
        kb_registry=registry,
        index_jobs=jobs,
        connection_store=connections,
        connector_sync_store=sync_jobs,
        source_catalog=catalog,
        source_artifact_store=artifacts,
        resource_access_store=access,
        close_state_runtime_on_shutdown=False,
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
                    "config": {"root": str(provider)},
                    "secret_env": {},
                },
            )
            connection_id = created.json()["connection_id"]
            started = await client.post(
                f"/v1/knowledge-bases/docs/connections/{connection_id}/sync"
            )
            sync_job_id = started.json()["job_id"]
            completed = await _wait_for_sync_job(client, "docs", sync_job_id)
            assert completed["status"] == "succeeded"
            active = catalog.list_sources("default", "docs")
            assert len(active) == 1
            source_id = active[0]["source_id"]
            version_id = active[0]["version_id"]
            document_id = build_document_id(active[0]["display_name"])
            assert access.get_document_policy("default", "docs", document_id)

            from cogdoc.api.routes import documents as document_routes

            original_authorization_guard = (
                document_routes._live_session_authorization_guard
            )

            def revoked_authorization(*_args, **_kwargs):
                def deny():
                    raise PermissionError("membership was revoked")

                return deny

            monkeypatch.setattr(
                document_routes,
                "_live_session_authorization_guard",
                revoked_authorization,
            )
            revoked = await client.delete(
                f"/v1/knowledge-bases/docs/connections/{connection_id}"
            )
            assert revoked.status_code == 404, revoked.text
            assert connections.get(connection_id)["enabled"] is True
            assert connections.get(connection_id)["deleting"] is False
            assert catalog.list_sources("default", "docs")
            assert list((tmp_path / "kb" / "docs" / "sources").glob("*.md"))
            monkeypatch.setattr(
                document_routes,
                "_live_session_authorization_guard",
                original_authorization_guard,
            )

            original_submit = jobs.submit
            original_get = jobs.get
            monkeypatch.setattr(
                jobs, "submit", lambda _kb_id: {"job_id": "failed-cleanup-index"}
            )
            monkeypatch.setattr(jobs, "get", lambda _job_id: {"status": "failed"})
            failed = await client.delete(
                f"/v1/knowledge-bases/docs/connections/{connection_id}"
            )
            assert failed.status_code == 500, failed.text
            retained = connections.get(connection_id)
            assert retained is not None and retained["enabled"] is False
            assert retained["deleting"] is True
            assert retained["delete_index_job_id"] == "failed-cleanup-index"
            managed_by = f"connector:{connection_id}"
            assert access.retiring_document_ids("default", "docs", managed_by)
            policy = access.get_document_policy("default", "docs", document_id)
            assert policy is not None and policy["policy"] == "private"
            assert access.list_grants("default", "docs", document_id=document_id) == []
            assert catalog.list_sources("default", "docs") == []
            tombstoned = catalog.get("default", "docs", source_id, include_deleted=True)
            assert tombstoned is not None and tombstoned["deleted_at"] is not None
            assert artifacts.read("default", "docs", source_id, version_id)
            assert not list((tmp_path / "kb" / "docs" / "sources").glob("*.md"))

            monkeypatch.setattr(jobs, "submit", original_submit)
            monkeypatch.setattr(jobs, "get", original_get)
            retried = await client.delete(
                f"/v1/knowledge-bases/docs/connections/{connection_id}"
            )
            assert retried.status_code == 204, retried.text
            assert connections.get(connection_id) is None
            assert access.retiring_document_ids("default", "docs", managed_by) == ()
            assert access.get_document_policy("default", "docs", document_id) is None
            assert sync_jobs.checkpoint_for("default", "docs", connection_id) is None
            assert sync_jobs.retire_connection("default", "docs", connection_id) == {
                "checkpoints": 0,
                "health": 0,
            }
            assert sync_jobs.get(sync_job_id) is not None
            deleted_catalog = await asyncio.wait_for(
                client.get(
                    "/v1/knowledge-bases/docs/source-catalog?include_deleted=true"
                ),
                timeout=5,
            )
            assert deleted_catalog.status_code == 200, deleted_catalog.text
            assert deleted_catalog.json()["sources"][0]["source_id"] == source_id
            versions = await asyncio.wait_for(
                client.get(
                    f"/v1/knowledge-bases/docs/source-catalog/{source_id}/versions"
                ),
                timeout=5,
            )
            assert versions.status_code == 200, versions.text
            assert versions.json()["versions"][0]["artifact_available"] is True
            downloaded = await asyncio.wait_for(
                client.get(
                    f"/v1/knowledge-bases/docs/source-catalog/{source_id}/versions/"
                    f"{version_id}/content"
                ),
                timeout=5,
            )
            assert downloaded.status_code == 200, downloaded.text
            assert downloaded.content == (
                b"# Guide\n\nMaterialized content removed by connection teardown."
            )

            blocked = await client.post(
                "/v1/knowledge-bases/docs/connections",
                json={
                    "connector_type": "local-directory",
                    "name": "Commit in progress",
                    "config": {"root": str(provider)},
                    "secret_env": {},
                },
            )
            blocked_id = blocked.json()["connection_id"]
            blocked_row = connections.get(blocked_id)
            committing = sync_jobs.create(
                tenant_id="default",
                kb_id="docs",
                connection_id=blocked_id,
                connector_type="local-directory",
                connection_revision=int(blocked_row["revision"]),
            )
            _, lease = sync_jobs.acquire(committing["job_id"], lease_seconds=60)
            sync_jobs.prepare_commit(committing["job_id"], lease)

            conflict = await client.delete(
                f"/v1/knowledge-bases/docs/connections/{blocked_id}"
            )
            assert conflict.status_code == 409, conflict.text
            assert connections.get(blocked_id)["enabled"] is False
            assert connections.get(blocked_id)["deleting"] is True
            assert sync_jobs.get(committing["job_id"])["status"] == "committing"

    connections.close()
    sync_jobs.close()
    catalog.close()
    access.close()
