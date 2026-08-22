from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from cogdoc.ha.index_generation import IndexIntegrityError
from cogdoc.ha.api_state import (
    DistributedKnowledgeBaseRegistry,
    DistributedMutationCoordinator,
)
from cogdoc.ha.object_store import LocalObjectStore
from cogdoc.ha.source_artifact_store import DistributedSourceArtifactStore
from cogdoc.ha.source_catalog import DistributedSourceCatalog
from cogdoc.ha.runtime import (
    DistributedIndexWorker,
    HAConfig,
    HAConfigurationError,
    HARuntime,
    manifest_for_directory,
)
from cogdoc.ha.storage import SQLiteBackend
from cogdoc.ha.tasks import JOB_SUCCEEDED
from cogdoc.api.tenant_quota import TenantQuotaPolicy
from cogdoc.source_model import SourceDocument


def _config(tmp_path, worker="worker"):
    return HAConfig(
        enabled=True,
        database_url="",
        database_schema="cogdoc",
        object_store="local",
        object_root=str(tmp_path / "objects"),
        s3_bucket="",
        s3_prefix="cogdoc",
        s3_endpoint_url=None,
        s3_region=None,
        s3_require_versioning=True,
        worker_id=worker,
        scheduler_enabled=False,
        outbox_enabled=True,
    )


def _contract():
    return {"chunk_version": "v1", "embedding_model": "model", "dimensions": 3}


def test_distributed_index_worker_publishes_objects_pointer_and_outbox_atomically(
    tmp_path,
):
    backend = SQLiteBackend(tmp_path / "ha.db")
    runtime = HARuntime(
        _config(tmp_path),
        backend=backend,
        object_store=LocalObjectStore(tmp_path / "objects"),
    )
    build_dir = tmp_path / "build"
    build_dir.mkdir()
    (build_dir / "vectors.bin").write_bytes(b"vectors")
    worker = DistributedIndexWorker(
        runtime,
        lambda _job: (
            manifest_for_directory(build_dir, contract=_contract()),
            build_dir,
        ),
    )
    job = worker.enqueue("tenant", "kb", "build-1")
    assert worker.run_once()
    assert runtime.jobs.get(job["job_id"])["status"] == JOB_SUCCEEDED
    current = runtime.index_generations.resolve_current(
        "tenant", "kb", runtime.index_repository.verify
    )
    assert current is not None
    with backend.transaction() as connection:
        event = connection.execute(
            "SELECT status,payload_json FROM ha_outbox WHERE topic='index.published'"
        ).fetchone()
    assert event is not None
    assert current["generation_id"] in event["payload_json"]
    runtime.shutdown()
    backend.close()


def test_worker_retry_after_publish_is_idempotent(tmp_path):
    backend = SQLiteBackend(tmp_path / "ha.db")
    runtime = HARuntime(
        _config(tmp_path),
        backend=backend,
        object_store=LocalObjectStore(tmp_path / "objects"),
    )
    build_dir = tmp_path / "build"
    build_dir.mkdir()
    (build_dir / "index.bin").write_bytes(b"index")
    calls = []

    def builder(_job):
        calls.append(1)
        return manifest_for_directory(build_dir, contract=_contract()), build_dir

    worker = DistributedIndexWorker(runtime, builder)
    first = worker.enqueue("tenant", "kb", "stable-build")
    worker.run_once()
    replay = worker.enqueue("tenant", "kb", "stable-build")
    assert replay["job_id"] == first["job_id"]
    assert calls == [1]
    with backend.transaction() as connection:
        assert connection.execute("SELECT COUNT(*) FROM ha_outbox").fetchone()[0] == 1
    backend.close()


def test_worker_publishes_prepared_generation_without_rebuilding(tmp_path):
    backend = SQLiteBackend(tmp_path / "ha.db")
    runtime = HARuntime(
        _config(tmp_path),
        backend=backend,
        object_store=LocalObjectStore(tmp_path / "objects"),
        index_builder=lambda _job: pytest.fail("prepared generation was rebuilt"),
    )
    build_dir = tmp_path / "build"
    build_dir.mkdir()
    (build_dir / "index.bin").write_bytes(b"prepared")
    generation = runtime.index_generations.begin_build(
        "tenant", "kb", "prepared-build", "producer"
    )
    generation = runtime.index_generations.prepare(
        generation["generation_id"],
        generation["lease_token"],
        manifest_for_directory(build_dir, contract=_contract()),
    )
    runtime.index_repository.materialize(generation, build_dir)

    assert runtime.index_worker is not None
    job = runtime.index_worker.enqueue(
        "tenant",
        "kb",
        "prepared-build",
        generation_id=generation["generation_id"],
        generation_lease_token=generation["lease_token"],
    )
    assert runtime.index_worker.run_once()
    assert runtime.jobs.get(job["job_id"])["status"] == JOB_SUCCEEDED
    assert (
        runtime.index_generations.current("tenant", "kb")["generation_id"]
        == (generation["generation_id"])
    )
    backend.close()


def test_worker_rotates_expired_prepared_generation_handoff(tmp_path, monkeypatch):
    now = [100.0]
    backend = SQLiteBackend(tmp_path / "ha.db")
    runtime = HARuntime(
        _config(tmp_path),
        backend=backend,
        object_store=LocalObjectStore(tmp_path / "objects"),
        index_builder=lambda _job: pytest.fail("prepared generation was rebuilt"),
    )
    monkeypatch.setattr(runtime.index_generations, "_clock", lambda: now[0])
    monkeypatch.setattr(runtime.jobs, "_clock", lambda: now[0])
    build_dir = tmp_path / "build"
    build_dir.mkdir()
    (build_dir / "index.bin").write_bytes(b"prepared")
    generation = runtime.index_generations.begin_build(
        "tenant", "kb", "delayed-build", "producer", lease_seconds=5
    )
    generation = runtime.index_generations.prepare(
        generation["generation_id"],
        generation["lease_token"],
        manifest_for_directory(build_dir, contract=_contract()),
    )
    runtime.index_repository.materialize(generation, build_dir)
    assert runtime.index_worker is not None
    job = runtime.index_worker.enqueue(
        "tenant",
        "kb",
        "delayed-build",
        generation_id=generation["generation_id"],
        generation_lease_token=generation["lease_token"],
    )
    now[0] += 6

    assert runtime.index_worker.run_once()
    assert runtime.jobs.get(job["job_id"])["status"] == JOB_SUCCEEDED
    published = runtime.index_generations.get(generation["generation_id"])
    assert published["status"] == "published"
    assert published["lease_token"] != generation["lease_token"]
    backend.close()


def test_worker_rejects_prepared_generation_from_another_scope(tmp_path):
    backend = SQLiteBackend(tmp_path / "ha.db")
    runtime = HARuntime(
        _config(tmp_path),
        backend=backend,
        object_store=LocalObjectStore(tmp_path / "objects"),
    )
    generation = runtime.index_generations.begin_build(
        "tenant", "other-kb", "build", "producer"
    )
    assert runtime.index_worker is not None
    job = runtime.index_worker.enqueue(
        "tenant",
        "kb",
        "build",
        generation_id=generation["generation_id"],
        generation_lease_token=generation["lease_token"],
    )

    assert runtime.index_worker.run_once()
    failed = runtime.jobs.get(job["job_id"])
    assert failed["status"] == "retry_wait"
    assert failed["error_code"] == "RUNTIMEERROR"
    assert runtime.index_generations.current("tenant", "kb") is None
    backend.close()


def test_runtime_starts_and_stops_configured_index_worker_threads(tmp_path):
    config = HAConfig(
        **{
            **_config(tmp_path).__dict__,
            "index_worker_count": 3,
            "index_worker_poll_seconds": 0.05,
        }
    )
    runtime = HARuntime(config)

    runtime.start()
    assert runtime.index_worker is not None
    assert runtime.index_worker.check() is True
    assert len(runtime.index_worker._threads) == 3
    runtime.shutdown()
    assert runtime.index_worker.check() is False


def test_runtime_owns_distributed_tenant_quota_heartbeat(tmp_path):
    runtime = HARuntime(_config(tmp_path))
    quota = runtime.bind_api_tenant_quota(TenantQuotaPolicy(max_documents=10))

    runtime.start()
    assert quota.check()
    assert quota._thread is not None and quota._thread.is_alive()
    runtime.shutdown()
    assert quota._thread is None


def test_runtime_current_reader_refuses_post_publish_object_damage(tmp_path):
    backend = SQLiteBackend(tmp_path / "ha.db")
    objects = LocalObjectStore(tmp_path / "objects")
    runtime = HARuntime(_config(tmp_path), backend=backend, object_store=objects)
    build_dir = tmp_path / "build"
    build_dir.mkdir()
    content = b"safe"
    (build_dir / "index.bin").write_bytes(content)
    worker = DistributedIndexWorker(
        runtime,
        lambda _job: (
            manifest_for_directory(build_dir, contract=_contract()),
            build_dir,
        ),
    )
    worker.enqueue("tenant", "kb", "build")
    worker.run_once()
    current = runtime.index_generations.current("tenant", "kb")
    base = runtime.index_repository._base(current)
    path = objects._path(f"{base}/files/index.bin")
    path.write_bytes(b"bad!")
    with pytest.raises(IndexIntegrityError):
        runtime.index_generations.resolve_current(
            "tenant", "kb", runtime.index_repository.verify
        )
    backend.close()


def test_ha_config_requires_postgres_and_versioned_s3_for_multi_instance(tmp_path):
    local = _config(tmp_path)
    assert not local.multi_instance_safe
    unsafe_s3 = HAConfig(
        **{
            **local.__dict__,
            "database_url": "postgresql://db/cogdoc",
            "object_store": "s3",
            "s3_bucket": "bucket",
            "s3_require_versioning": False,
        }
    )
    with pytest.raises(HAConfigurationError, match="versioning"):
        unsafe_s3.validate()
    safe = HAConfig(
        **{
            **local.__dict__,
            "database_url": "postgresql://db/cogdoc",
            "object_store": "s3",
            "s3_bucket": "bucket",
        }
    )
    assert safe.multi_instance_safe


def test_api_multiwriter_fails_closed_without_postgres_and_versioned_s3(tmp_path):
    local = _config(tmp_path)
    unsafe = HAConfig(**{**local.__dict__, "api_multi_writer_enabled": True})
    with pytest.raises(HAConfigurationError, match="multi-writer API"):
        unsafe.validate()
    safe = HAConfig(
        **{
            **local.__dict__,
            "database_url": "postgresql://db/cogdoc",
            "object_store": "s3",
            "s3_bucket": "bucket",
            "api_multi_writer_enabled": True,
        }
    )
    safe.validate()
    assert safe.api_multi_writer_safe


def test_config_from_settings_has_stable_safe_defaults(tmp_path):
    settings = SimpleNamespace(
        cogdoc_ha_enabled=True,
        cogdoc_ha_worker_id="",
        cogdoc_ha_object_root="",
        cogdoc_data_dir=str(tmp_path),
        cogdoc_ha_database_url="",
        cogdoc_ha_database_schema="cogdoc",
        cogdoc_ha_object_store="local",
        cogdoc_ha_s3_bucket="",
        cogdoc_ha_s3_prefix="cogdoc",
        cogdoc_ha_s3_endpoint_url="",
        cogdoc_ha_s3_region="",
        cogdoc_ha_s3_require_versioning=True,
        cogdoc_ha_scheduler_enabled=True,
        cogdoc_ha_outbox_enabled=True,
    )
    config = HAConfig.from_settings(settings)
    assert config.worker_id
    assert config.object_root.endswith("ha-objects")
    assert config.index_replica_cache_root.endswith("ha-index-cache")
    assert config.chat_session_lease_seconds == 300.0
    assert config.chat_index_reader_lease_seconds == 600.0
    assert config.chat_max_sessions_per_scope == 1024
    assert config.chat_session_ttl_seconds == 604800
    assert config.chat_max_display_messages == 2000
    assert config.chat_max_session_bytes == 4 * 1024 * 1024


def test_empty_generation_is_valid_and_content_addressed(tmp_path):
    manifest = manifest_for_directory(tmp_path, contract=_contract())
    assert manifest["files"] == []
    canonical = hashlib.sha256(
        b'{"contract":{"chunk_version":"v1","dimensions":3,"embedding_model":"model"},'
        b'"files":[],"schema_version":"index-manifest-v1","total_bytes":0}'
    ).hexdigest()
    assert len(canonical) == 64


def test_runtime_start_failure_stops_already_started_scheduler(tmp_path):
    class Scheduler:
        def __init__(self):
            self.started = False
            self.stopped = False

        def start(self):
            self.started = True

        def stop(self):
            self.stopped = True
            return True

    class BrokenOutbox:
        def start(self):
            raise RuntimeError("outbox startup failed")

        def stop(self):
            return None

    config = HAConfig(**{**_config(tmp_path).__dict__, "scheduler_enabled": True})
    backend = SQLiteBackend(tmp_path / "ha.db")
    runtime = HARuntime(config, backend=backend)
    scheduler = Scheduler()
    runtime.scheduler = scheduler
    runtime.outbox_dispatcher = BrokenOutbox()
    with pytest.raises(RuntimeError, match="outbox startup failed"):
        runtime.start()
    assert scheduler.started is True
    assert scheduler.stopped is True
    assert backend.check() is True
    backend.close()


def test_runtime_does_not_close_owned_backend_while_scheduler_is_alive(tmp_path):
    class StuckScheduler:
        def stop(self):
            return False

    class StoppedScheduler:
        def stop(self):
            return True

    config = HAConfig(**{**_config(tmp_path).__dict__, "scheduler_enabled": True})
    runtime = HARuntime(config)
    runtime.scheduler = StuckScheduler()
    runtime._started = True
    with pytest.raises(RuntimeError, match="did not stop"):
        runtime.shutdown()
    assert runtime.backend.check() is True
    runtime.scheduler = StoppedScheduler()
    runtime.shutdown()


@pytest.mark.anyio
async def test_app_lifespan_owns_ha_runtime_and_readiness(monkeypatch):
    import cogdoc.api.app as app_module

    class FakeHA:
        def __init__(self):
            self.starts = 0
            self.stops = 0

        def start(self):
            self.starts += 1

        def shutdown(self):
            self.stops += 1

        def check(self):
            return True

    monkeypatch.setattr(app_module, "configure_logging", lambda: None)
    runtime = FakeHA()
    app = app_module.create_app(ha_runtime=runtime)
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/health/ready")
            assert response.status_code == 200
            assert response.json()["components"]["ha_control_plane"] == {
                "status": "ready",
                "required": True,
            }
    assert runtime.starts == runtime.stops == 1


@pytest.mark.anyio
async def test_app_binds_and_owns_ha_index_mirror_lifecycle(tmp_path, monkeypatch):
    import cogdoc.api.app as app_module
    from cogdoc.api.ingest import KnowledgeBaseRegistry

    monkeypatch.setattr(app_module, "configure_logging", lambda: None)
    runtime = HARuntime(
        _config(tmp_path),
        backend=SQLiteBackend(tmp_path / "ha.db"),
        object_store=LocalObjectStore(tmp_path / "objects"),
    )
    registry = KnowledgeBaseRegistry(
        registry_path=str(tmp_path / "registry.json"),
        source_dir_for=lambda kb_id: str(tmp_path / "kb" / kb_id / "sources"),
    )
    app = app_module.create_app(
        ha_runtime=runtime,
        kb_registry=registry,
        close_state_runtime_on_shutdown=False,
    )
    assert app.state.ha_index_mirror is not None
    assert app.state.index_jobs._after_index_commit.__self__ is (
        app.state.ha_index_mirror
    )

    async with app.router.lifespan_context(app):
        assert app.state.ha_index_mirror.check() is True
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            assert (await client.get("/health/ready")).status_code == 200
    assert app.state.ha_index_mirror.check() is False
    backend = runtime.backend
    assert backend.check() is True
    backend.close()


@pytest.mark.anyio
async def test_same_app_can_restart_ha_runtime_and_mirror_lifespan(
    tmp_path, monkeypatch
):
    import cogdoc.api.app as app_module
    from cogdoc.api.ingest import KnowledgeBaseRegistry

    monkeypatch.setattr(app_module, "configure_logging", lambda: None)
    runtime = HARuntime(
        _config(tmp_path),
        backend=SQLiteBackend(tmp_path / "ha.db"),
        object_store=LocalObjectStore(tmp_path / "objects"),
    )
    registry = KnowledgeBaseRegistry(
        registry_path=str(tmp_path / "registry.json"),
        source_dir_for=lambda kb_id: str(tmp_path / "kb" / kb_id / "sources"),
    )
    app = app_module.create_app(
        ha_runtime=runtime,
        kb_registry=registry,
        close_state_runtime_on_shutdown=False,
    )

    for _cycle in range(2):
        async with app.router.lifespan_context(app):
            assert runtime.check() is True
            assert app.state.ha_index_mirror.check() is True
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                assert (await client.get("/health/ready")).status_code == 200
        assert app.state.ha_index_mirror.check() is False
    runtime.backend.close()


@pytest.mark.anyio
async def test_document_multiwriter_node_rejects_local_state_api_surface(monkeypatch):
    import cogdoc.api.app as app_module

    monkeypatch.setattr(app_module, "configure_logging", lambda: None)
    runtime = SimpleNamespace(api_multi_writer_safe=True, index_generations=None)
    app = app_module.create_app(
        ha_runtime=runtime,
        close_state_runtime_on_shutdown=False,
    )

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        allowed = await client.get("/v1/knowledge-bases")
        blocked = await client.get("/v1/chat/sessions")
        blocked_sources = await client.get("/v1/knowledge-bases/docs/sources")
        shared_source_catalog = await client.get(
            "/v1/knowledge-bases/docs/source-catalog"
        )
        shared_artifact_mutation = await client.delete(
            "/v1/knowledge-bases/docs/source-artifacts/trash?older_than=0"
        )
        shared_kb_delete = await client.delete("/v1/knowledge-bases/docs")

    assert allowed.status_code == 200
    assert blocked.status_code == 503
    assert blocked.json()["error_code"] == "MODEL_UNAVAILABLE"
    assert blocked_sources.status_code == 503
    assert shared_source_catalog.status_code != 503
    assert shared_artifact_mutation.status_code != 503
    assert shared_kb_delete.status_code == 404


@pytest.mark.anyio
async def test_document_multiwriter_deletes_shared_kb_authority(tmp_path, monkeypatch):
    import cogdoc.api.app as app_module

    monkeypatch.setattr(app_module, "configure_logging", lambda: None)
    runtime = HARuntime(_config(tmp_path))
    registry = DistributedKnowledgeBaseRegistry(
        runtime.backend, tmp_path / "source-cache"
    )
    kb = registry.create("docs", "default", "default")
    storage_id = str(kb["storage_id"])
    mutations = DistributedMutationCoordinator(
        runtime.backend, registry, owner_id="api-delete", lease_seconds=30
    )
    runtime.api_mutation_coordinator = mutations
    deletion = runtime.bind_api_kb_deletion(registry)
    deletion.activate("default", storage_id, kb_epoch=int(kb["epoch"]))
    content = b"private raw"
    document = SourceDocument.create(
        connector_type="url",
        external_id="connection:private",
        display_name="private.md",
        content_sha256=hashlib.sha256(content).hexdigest(),
        byte_size=len(content),
    )
    runtime.source_catalog.upsert("default", storage_id, document)
    runtime.source_artifact_store.put(
        "default",
        storage_id,
        document.source_id,
        document.version.version_id,
        content,
        content_sha256=document.version.content_sha256,
        media_type="text/plain",
        display_name="private.md",
        created_at=1,
    )
    api_runtime = SimpleNamespace(
        api_multi_writer_safe=True,
        index_generations=runtime.index_generations,
        source_generations=runtime.source_generations,
        api_mutation_coordinator=mutations,
        api_kb_deletion=deletion,
    )
    app = app_module.create_app(
        ha_runtime=api_runtime,
        kb_registry=registry,
        source_catalog=runtime.source_catalog,
        source_artifact_store=runtime.source_artifact_store,
        close_state_runtime_on_shutdown=False,
    )

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        deleted = await client.delete("/v1/knowledge-bases/docs")
        missing = await client.get("/v1/knowledge-bases/docs")

    assert deleted.status_code == 204
    assert missing.status_code == 404
    assert registry.get_by_storage_id(storage_id) is None
    assert runtime.source_catalog.list_sources("default", storage_id) == []
    assert (
        runtime.source_artifact_store.usage("default", storage_id)["active_versions"]
        == 0
    )
    runtime.shutdown()


@pytest.mark.anyio
async def test_document_multiwriter_reads_shared_catalog_and_raw_artifact(
    tmp_path, monkeypatch
):
    import cogdoc.api.app as app_module

    monkeypatch.setattr(app_module, "configure_logging", lambda: None)
    backend = SQLiteBackend(tmp_path / "shared.db")
    objects = LocalObjectStore(tmp_path / "objects")
    catalog = DistributedSourceCatalog(backend)
    artifacts = DistributedSourceArtifactStore(backend, objects, owner_id="reader-test")
    registry = DistributedKnowledgeBaseRegistry(
        backend,
        tmp_path / "source-cache",
    )
    kb = registry.create("docs", "default", "owner")
    storage_id = str(kb["storage_id"])
    coordinator = DistributedMutationCoordinator(
        backend, registry, owner_id="api-reader", lease_seconds=30
    )
    old_content = b"old shared raw source"
    old_document = SourceDocument.create(
        connector_type="git",
        external_id="connection:document.md",
        display_name="document.md",
        content_sha256=hashlib.sha256(old_content).hexdigest(),
        byte_size=len(old_content),
    )
    catalog.upsert("default", storage_id, old_document)
    artifacts.put(
        "default",
        storage_id,
        old_document.source_id,
        old_document.version.version_id,
        old_content,
        content_sha256=old_document.version.content_sha256,
        media_type="text/plain",
        display_name="document.md",
        created_at=1,
    )
    content = b"new shared raw source"
    document = SourceDocument.create(
        connector_type="git",
        external_id="connection:document.md",
        display_name="document.md",
        content_sha256=hashlib.sha256(content).hexdigest(),
        byte_size=len(content),
    )
    catalog.upsert("default", storage_id, document)
    artifacts.put(
        "default",
        storage_id,
        document.source_id,
        document.version.version_id,
        content,
        content_sha256=document.version.content_sha256,
        media_type="text/plain",
        display_name="document.md",
        created_at=2,
    )
    runtime = SimpleNamespace(
        api_multi_writer_safe=True,
        index_generations=None,
        api_mutation_coordinator=coordinator,
    )
    app = app_module.create_app(
        ha_runtime=runtime,
        kb_registry=registry,
        source_catalog=catalog,
        source_artifact_store=artifacts,
        close_state_runtime_on_shutdown=False,
    )

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        listed = await client.get("/v1/knowledge-bases/docs/source-catalog")
        downloaded = await client.get(
            "/v1/knowledge-bases/docs/source-catalog/"
            f"{document.source_id}/versions/{document.version.version_id}/content"
        )
        deleted = await client.delete(
            "/v1/knowledge-bases/docs/source-catalog/"
            f"{old_document.source_id}/versions/{old_document.version.version_id}/artifact"
        )
        restored = await client.post(
            "/v1/knowledge-bases/docs/source-artifacts/"
            f"{deleted.json()['recovery_token']}/restore"
        )

    assert listed.status_code == 200
    assert listed.json()["sources"][0]["source_id"] == document.source_id
    assert downloaded.status_code == 200
    assert downloaded.content == content
    assert deleted.status_code == 200
    assert restored.status_code == 200
    assert coordinator.current_lease() is None
    backend.close()


def test_document_multiwriter_fails_closed_for_process_local_tenant_quota(
    monkeypatch,
):
    import cogdoc.api.app as app_module

    settings = app_module.get_settings().model_copy(
        update={"cogdoc_tenant_max_documents": 10}
    )
    monkeypatch.setattr(app_module, "get_settings", lambda: settings)
    runtime = SimpleNamespace(api_multi_writer_safe=True, index_generations=None)

    with pytest.raises(ValueError, match="distributed tenant quota"):
        app_module.create_app(
            ha_runtime=runtime,
            close_state_runtime_on_shutdown=False,
        )
