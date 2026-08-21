from __future__ import annotations

import hashlib

import pytest

from cogdoc.ha.index_generation import IndexGenerationStore
from cogdoc.ha.maintenance import HAMaintenance
from cogdoc.ha.object_store import LocalObjectStore, ObjectIndexRepository
from cogdoc.ha.outbox import OutboxStore
from cogdoc.ha.scheduler import DistributedScheduler, SCHEDULE_ONCE, ScheduleStore
from cogdoc.ha.storage import SQLiteBackend
from cogdoc.ha.tasks import JOB_DEAD_LETTER, LeaseJobStore


class Clock:
    def __init__(self, value: float = 1000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


def _manifest(content: bytes) -> dict:
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


def _publish(
    authority,
    repository,
    tmp_path,
    build_id: str,
    content: bytes,
    *,
    kb_id: str = "kb",
):
    source = tmp_path / build_id
    source.mkdir()
    (source / "index.bin").write_bytes(content)
    generation = authority.begin_build("tenant", kb_id, build_id, "worker")
    prepared = authority.prepare(
        generation["generation_id"], generation["lease_token"], _manifest(content)
    )
    repository.materialize(prepared, source)
    return authority.publish(
        prepared["generation_id"], prepared["lease_token"], repository.verify
    )


def test_maintenance_reaps_prunes_scrubs_and_never_collects_current(tmp_path):
    clock = Clock()
    backend = SQLiteBackend(tmp_path / "ha.db")
    jobs = LeaseJobStore(backend, clock=clock)
    schedules = ScheduleStore(backend, clock=clock)
    outbox = OutboxStore(backend, clock=clock)
    authority = IndexGenerationStore(backend, clock=clock)
    repository = ObjectIndexRepository(LocalObjectStore(tmp_path / "objects"))

    expired = jobs.enqueue("work", "tenant", {}, max_attempts=1)
    claimed = jobs.claim("work", "worker", lease_seconds=1)
    assert claimed is not None and claimed["job_id"] == expired["job_id"]

    with backend.transaction(write=True) as connection:
        event = outbox.append(
            connection,
            tenant_id="tenant",
            topic="test",
            aggregate_type="kb",
            aggregate_id="kb",
            aggregate_revision=1,
            payload={},
            idempotency_key="event",
        )
    delivery = outbox.claim("worker", lease_seconds=60)
    assert delivery is not None
    outbox.delivered(event["event_id"], delivery["lease_token"])

    schedules.create(
        "tenant",
        "work",
        {},
        schedule_type=SCHEDULE_ONCE,
        schedule_spec=str(clock.value),
    )
    assert DistributedScheduler(schedules, jobs).run_once() == (1, 1)
    first = _publish(authority, repository, tmp_path, "first", b"old")
    second = _publish(authority, repository, tmp_path, "second", b"current")

    clock.value += 120
    maintenance = HAMaintenance(
        jobs,
        schedules,
        outbox,
        authority,
        repository,
        retention_seconds=60,
        interval_seconds=1,
        scrub_interval_seconds=10,
        clock=clock,
        monotonic=clock,
    )
    result = maintenance.run_once()

    assert jobs.get(expired["job_id"])["status"] == JOB_DEAD_LETTER
    assert result == {
        "jobs_reaped": 1,
        "jobs_pruned": 0,
        "outbox_pruned": 1,
        "fires_pruned": 1,
        "generations_removed": 1,
        "generations_scrubbed": 1,
    }
    assert authority.get(first["generation_id"]) is None
    assert (
        authority.resolve_current("tenant", "kb", repository.verify)["generation_id"]
        == second["generation_id"]
    )
    snapshot = maintenance.snapshot()
    assert snapshot.runs == 1
    assert snapshot.failures == 0
    backend.close()


def test_maintenance_thread_reports_failure_and_stops(tmp_path):
    backend = SQLiteBackend(tmp_path / "ha.db")
    jobs = LeaseJobStore(backend)
    schedules = ScheduleStore(backend)
    outbox = OutboxStore(backend)
    authority = IndexGenerationStore(backend)
    repository = ObjectIndexRepository(LocalObjectStore(tmp_path / "objects"))
    maintenance = HAMaintenance(
        jobs,
        schedules,
        outbox,
        authority,
        repository,
        interval_seconds=1,
        scrub_interval_seconds=10,
    )
    maintenance.start()
    maintenance.wake()
    assert maintenance.stop()
    assert not maintenance.snapshot().running
    backend.close()


def test_scrub_failure_does_not_starve_later_current_generations(tmp_path):
    backend = SQLiteBackend(tmp_path / "ha.db")
    jobs = LeaseJobStore(backend)
    schedules = ScheduleStore(backend)
    outbox = OutboxStore(backend)
    authority = IndexGenerationStore(backend)
    repository = ObjectIndexRepository(LocalObjectStore(tmp_path / "objects"))
    first = _publish(authority, repository, tmp_path, "first", b"broken", kb_id="a-kb")
    second = _publish(
        authority, repository, tmp_path, "second", b"healthy", kb_id="z-kb"
    )
    original_verify = repository.verify
    checked: list[str] = []

    def verify(generation):
        checked.append(generation["generation_id"])
        if generation["generation_id"] == first["generation_id"]:
            raise RuntimeError("corrupt first generation")
        original_verify(generation)

    repository.verify = verify
    maintenance = HAMaintenance(
        jobs,
        schedules,
        outbox,
        authority,
        repository,
        interval_seconds=1,
        scrub_interval_seconds=10,
    )

    with pytest.raises(RuntimeError, match="1 current generation"):
        maintenance.run_once()

    assert checked == [first["generation_id"], second["generation_id"]]
    assert maintenance._scrub_after is None
    assert maintenance.snapshot().generations_scrubbed == 1
    backend.close()


def test_one_maintenance_stage_failure_does_not_skip_later_stages(tmp_path):
    backend = SQLiteBackend(tmp_path / "ha.db")
    jobs = LeaseJobStore(backend)
    schedules = ScheduleStore(backend)
    outbox = OutboxStore(backend)
    authority = IndexGenerationStore(backend)
    repository = ObjectIndexRepository(LocalObjectStore(tmp_path / "objects"))
    current = _publish(
        authority, repository, tmp_path, "current", b"healthy", kb_id="kb"
    )
    scrubbed: list[str] = []
    original_verify = repository.verify

    def verify(generation):
        scrubbed.append(generation["generation_id"])
        original_verify(generation)

    repository.verify = verify

    def fail_reap(*, limit):
        assert limit == 100
        raise RuntimeError("database timeout")

    jobs.reap_expired = fail_reap
    maintenance = HAMaintenance(
        jobs,
        schedules,
        outbox,
        authority,
        repository,
        interval_seconds=1,
        scrub_interval_seconds=10,
    )

    with pytest.raises(RuntimeError, match="jobs_reaped"):
        maintenance.run_once()

    assert scrubbed == [current["generation_id"]]
    assert maintenance.snapshot().generations_scrubbed == 1
    backend.close()
