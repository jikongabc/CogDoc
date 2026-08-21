from __future__ import annotations

import hashlib
import threading

import pytest

from cogdoc.ha.index_generation import (
    GEN_PREPARED,
    GEN_PUBLISHED,
    IndexGenerationStore,
    IndexIntegrityError,
    StaleIndexFence,
)
from cogdoc.ha.object_store import LocalObjectStore, ObjectIndexRepository
from cogdoc.ha.outbox import OutboxStore
from cogdoc.ha.scheduler import DistributedScheduler, SCHEDULE_INTERVAL, ScheduleStore
from cogdoc.ha.storage import SQLiteBackend
from cogdoc.ha.tasks import LeaseJobStore


class Clock:
    def __init__(self):
        self.value = 1000.0

    def __call__(self):
        return self.value


def _manifest(content: bytes):
    return {
        "schema_version": "index-manifest-v1",
        "contract": {
            "chunk_version": "v1",
            "embedding_model": "model",
            "dimensions": 3,
        },
        "files": [
            {
                "path": "index.bin",
                "sha256": hashlib.sha256(content).hexdigest(),
                "byte_size": len(content),
            }
        ],
    }


def _source(tmp_path, name, content):
    path = tmp_path / name
    path.mkdir()
    (path / "index.bin").write_bytes(content)
    return path


def test_expired_worker_can_upload_but_cannot_publish_after_lease_takeover(tmp_path):
    clock = Clock()
    path = tmp_path / "authority.db"
    backend_a = SQLiteBackend(path)
    backend_b = SQLiteBackend(path)
    authority_a = IndexGenerationStore(backend_a, clock=clock)
    authority_b = IndexGenerationStore(backend_b, clock=clock)
    repository = ObjectIndexRepository(LocalObjectStore(tmp_path / "objects"))
    content = b"complete generation"
    source = _source(tmp_path, "source", content)
    first = authority_a.begin_build(
        "tenant", "kb", "stable-build", "worker-a", lease_seconds=5
    )
    prepared = authority_a.prepare(
        first["generation_id"], first["lease_token"], _manifest(content)
    )
    repository.materialize(prepared, source)
    clock.value += 6
    takeover = authority_b.begin_build(
        "tenant", "kb", "stable-build", "worker-b", lease_seconds=5
    )
    assert takeover["lease_token"] != prepared["lease_token"]
    with pytest.raises(StaleIndexFence):
        authority_a.publish(
            prepared["generation_id"], prepared["lease_token"], repository.verify
        )
    published = authority_b.publish(
        takeover["generation_id"], takeover["lease_token"], repository.verify
    )
    assert published["status"] == GEN_PUBLISHED
    assert authority_a.resolve_current("tenant", "kb", repository.verify) == published
    backend_a.close()
    backend_b.close()


def test_process_loss_after_marker_before_pointer_is_resumable(tmp_path):
    clock = Clock()
    database = tmp_path / "authority.db"
    objects = LocalObjectStore(tmp_path / "objects")
    repository = ObjectIndexRepository(objects)
    backend = SQLiteBackend(database)
    authority = IndexGenerationStore(backend, clock=clock)
    source = _source(tmp_path, "source", b"durable")
    generation = authority.begin_build(
        "tenant", "kb", "crash-window", "before-crash", lease_seconds=5
    )
    prepared = authority.prepare(
        generation["generation_id"], generation["lease_token"], _manifest(b"durable")
    )
    repository.materialize(prepared, source)
    backend.close()
    # DB current was never switched; object marker survives process loss.
    backend = SQLiteBackend(database)
    recovered = IndexGenerationStore(backend, clock=clock)
    assert recovered.current("tenant", "kb") is None
    clock.value += 6
    resumed = recovered.begin_build(
        "tenant", "kb", "crash-window", "after-crash", lease_seconds=5
    )
    assert resumed["status"] == GEN_PREPARED
    recovered.publish(
        resumed["generation_id"], resumed["lease_token"], repository.verify
    )
    assert recovered.resolve_current("tenant", "kb", repository.verify) is not None
    backend.close()


def test_partial_object_upload_without_marker_never_publishes(tmp_path):
    backend = SQLiteBackend(tmp_path / "authority.db")
    authority = IndexGenerationStore(backend)
    objects = LocalObjectStore(tmp_path / "objects")
    repository = ObjectIndexRepository(objects)
    content = b"partial"
    source = _source(tmp_path, "source", content)
    generation = authority.begin_build("tenant", "kb", "partial", "worker")
    prepared = authority.prepare(
        generation["generation_id"], generation["lease_token"], _manifest(content)
    )
    base = repository._base(prepared)
    objects.put_file(
        f"{base}/files/index.bin",
        source / "index.bin",
        sha256=hashlib.sha256(content).hexdigest(),
    )
    with pytest.raises(IndexIntegrityError, match="marker"):
        authority.publish(
            prepared["generation_id"], prepared["lease_token"], repository.verify
        )
    assert authority.current("tenant", "kb") is None
    backend.close()


def test_outbox_failure_rolls_back_index_pointer_and_event(tmp_path):
    backend = SQLiteBackend(tmp_path / "authority.db")
    authority = IndexGenerationStore(backend)
    outbox = OutboxStore(backend)
    repository = ObjectIndexRepository(LocalObjectStore(tmp_path / "objects"))
    source = _source(tmp_path, "source", b"safe")
    generation = authority.begin_build("tenant", "kb", "build", "worker")
    prepared = authority.prepare(
        generation["generation_id"], generation["lease_token"], _manifest(b"safe")
    )
    repository.materialize(prepared, source)

    def fail_after_append(connection, candidate):
        outbox.append(
            connection,
            tenant_id="tenant",
            topic="index.published",
            aggregate_type="kb",
            aggregate_id="kb",
            aggregate_revision=int(candidate["fencing_token"]),
            payload={"generation": candidate["generation_id"]},
            idempotency_key="event",
        )
        raise RuntimeError("simulated process failure")

    with pytest.raises(RuntimeError, match="simulated"):
        authority.publish(
            prepared["generation_id"],
            prepared["lease_token"],
            repository.verify,
            on_publish=fail_after_append,
        )
    assert authority.current("tenant", "kb") is None
    with backend.transaction() as connection:
        assert connection.execute("SELECT COUNT(*) FROM ha_outbox").fetchone()[0] == 0
    backend.close()


def test_many_scheduler_instances_materialize_one_fire_and_one_job(tmp_path):
    database = tmp_path / "scheduler.db"
    clock = Clock()
    setup_backend = SQLiteBackend(database)
    setup_jobs = LeaseJobStore(setup_backend, clock=clock)
    setup = ScheduleStore(setup_backend, clock=clock)
    setup.create(
        "tenant",
        "queue",
        {"work": 1},
        schedule_type=SCHEDULE_INTERVAL,
        schedule_spec="60",
    )
    clock.value += 60
    backends = [SQLiteBackend(database) for _ in range(8)]
    schedulers = [
        DistributedScheduler(
            ScheduleStore(backend, clock=clock), LeaseJobStore(backend, clock=clock)
        )
        for backend in backends
    ]
    barrier = threading.Barrier(len(schedulers))

    def run(scheduler):
        barrier.wait()
        scheduler.run_once()

    threads = [
        threading.Thread(target=run, args=(scheduler,)) for scheduler in schedulers
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(setup_jobs.list_jobs()) == 1
    assert setup.pending_fires() == []
    for backend in backends:
        backend.close()
    setup_backend.close()
