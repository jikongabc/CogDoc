from __future__ import annotations

import threading

import pytest

from cogdoc.ha.storage import SQLiteBackend
from cogdoc.ha.tasks import (
    JOB_CANCELLED,
    JOB_DEAD_LETTER,
    JOB_RETRY_WAIT,
    JOB_SUCCEEDED,
    JobConflict,
    LeaseJobStore,
    StaleJobLease,
)


class Clock:
    def __init__(self, value=1000.0):
        self.value = value

    def __call__(self):
        return self.value


@pytest.fixture
def jobs(tmp_path):
    clock = Clock()
    backend = SQLiteBackend(tmp_path / "ha.db")
    store = LeaseJobStore(backend, clock=clock)
    yield store, clock
    backend.close()


def test_idempotent_enqueue_reuses_exact_payload_and_rejects_conflict(jobs):
    store, _clock = jobs
    first = store.enqueue("sync", "tenant-a", {"source": 1}, idempotency_key="once")
    replay = store.enqueue("sync", "tenant-a", {"source": 1}, idempotency_key="once")
    assert replay["job_id"] == first["job_id"]
    with pytest.raises(JobConflict, match="different payload"):
        store.enqueue("sync", "tenant-a", {"source": 2}, idempotency_key="once")


def test_only_one_worker_claims_and_stale_token_cannot_commit(jobs):
    store, _clock = jobs
    job = store.enqueue("sync", "tenant-a", {"source": 1})
    barrier = threading.Barrier(8)
    claimed = []

    def worker(index):
        barrier.wait()
        result = store.claim("sync", f"worker-{index}")
        if result is not None:
            claimed.append(result)

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(claimed) == 1
    active = claimed[0]
    with pytest.raises(StaleJobLease):
        store.complete(job["job_id"], "wrong-token", {"ok": True})
    completed = store.complete(job["job_id"], active["lease_token"], {"ok": True})
    assert completed["status"] == JOB_SUCCEEDED
    assert completed["result"] == {"ok": True}


def test_retry_dead_letter_and_expired_lease_recovery(jobs):
    store, clock = jobs
    job = store.enqueue("sync", "tenant-a", {}, max_attempts=3)
    first = store.claim("sync", "worker", lease_seconds=5)
    assert first is not None
    retry = store.fail(
        job["job_id"],
        first["lease_token"],
        "UPSTREAM",
        retryable=True,
        retry_delay_seconds=10,
    )
    assert retry["status"] == JOB_RETRY_WAIT
    assert store.claim("sync", "worker") is None
    clock.value += 10
    second = store.claim("sync", "worker", lease_seconds=5)
    assert second is not None
    clock.value += 6
    assert store.reap_expired() == 1
    third = store.claim("sync", "worker")
    assert third is not None
    dead = store.fail(job["job_id"], third["lease_token"], "UPSTREAM", retryable=True)
    assert dead["status"] == JOB_DEAD_LETTER


def test_cancel_queued_and_running_jobs(jobs):
    store, _clock = jobs
    queued = store.enqueue("sync", "tenant-a", {"n": 1})
    assert store.request_cancel(queued["job_id"])["status"] == JOB_CANCELLED
    running = store.enqueue("sync", "tenant-a", {"n": 2})
    lease = store.claim("sync", "worker")
    assert lease is not None and lease["job_id"] == running["job_id"]
    requested = store.request_cancel(running["job_id"])
    assert requested is not None and requested["cancel_requested"]
    with pytest.raises(StaleJobLease, match="cancelled"):
        store.heartbeat(running["job_id"], lease["lease_token"])
    finished = store.complete(running["job_id"], lease["lease_token"], {})
    assert finished["status"] == JOB_CANCELLED


def test_priority_tenant_filter_and_payload_limit(jobs):
    store, _clock = jobs
    low = store.enqueue("sync", "tenant-a", {"n": 1}, priority=-1)
    high = store.enqueue("sync", "tenant-b", {"n": 2}, priority=10)
    assert store.claim("sync", "worker")["job_id"] == high["job_id"]
    assert [row["job_id"] for row in store.list_jobs(tenant_id="tenant-a")] == [
        low["job_id"]
    ]
    with pytest.raises(ValueError, match="1 MiB"):
        store.enqueue("sync", "tenant-a", {"huge": "x" * (1024 * 1024)})


def test_expired_final_attempt_is_dead_lettered_without_extra_claim(jobs):
    store, clock = jobs
    job = store.enqueue("index", "tenant-a", {}, max_attempts=1)
    lease = store.claim("index", "worker", lease_seconds=5)
    assert lease is not None
    clock.value += 6
    assert store.reap_expired() == 1
    current = store.get(job["job_id"])
    assert current is not None
    assert current["status"] == JOB_DEAD_LETTER
    assert current["error_code"] == "LEASE_EXPIRED"
    assert store.claim("index", "another-worker") is None


def test_dead_letter_replay_is_explicit_idempotent_and_lineage_preserving(jobs):
    store, clock = jobs
    original = store.enqueue("index", "tenant-a", {"kb": "docs"}, max_attempts=1)
    lease = store.claim("index", "worker", lease_seconds=5)
    assert lease is not None
    clock.value += 6
    assert store.reap_expired() == 1

    replay = store.replay_dead_letter(original["job_id"], replay_key="operator-1")
    repeated = store.replay_dead_letter(original["job_id"], replay_key="operator-1")

    assert replay["job_id"] == repeated["job_id"]
    assert replay["replay_of"] == original["job_id"]
    assert replay["payload"] == original["payload"]
    assert replay["attempt"] == 0
    with pytest.raises(JobConflict, match="only dead-letter"):
        store.replay_dead_letter(replay["job_id"], replay_key="invalid")


def test_terminal_prune_keeps_permanent_idempotency_tombstone(jobs):
    store, clock = jobs
    original = store.enqueue(
        "sync", "tenant-a", {"source": 1}, idempotency_key="permanent"
    )
    lease = store.claim("sync", "worker")
    assert lease is not None
    store.complete(original["job_id"], lease["lease_token"], {"ok": True})
    clock.value += 100

    assert store.prune_terminal(before=clock.value - 1) == 1
    assert store.get(original["job_id"]) is None
    replay = store.enqueue(
        "sync", "tenant-a", {"source": 1}, idempotency_key="permanent"
    )
    assert replay == {
        "job_id": original["job_id"],
        "queue_name": "sync",
        "tenant_id": "tenant-a",
        "payload": {"source": 1},
        "status": JOB_SUCCEEDED,
        "idempotency_key": "permanent",
        "result": None,
        "replay_of": None,
        "pruned": True,
    }
    with pytest.raises(JobConflict, match="different payload"):
        store.enqueue("sync", "tenant-a", {"source": 2}, idempotency_key="permanent")
