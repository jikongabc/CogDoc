from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from cogdoc.ha.index_generation import IndexIntegrityError
from cogdoc.ha.object_store import LocalObjectStore
from cogdoc.ha.runtime import (
    DistributedIndexWorker,
    HAConfig,
    HAConfigurationError,
    HARuntime,
    manifest_for_directory,
)
from cogdoc.ha.storage import SQLiteBackend
from cogdoc.ha.tasks import JOB_SUCCEEDED


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
