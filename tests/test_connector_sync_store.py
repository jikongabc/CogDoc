from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace

import pytest

from cogdoc.connectors.base import StaleSyncLease, SyncCancelled
from cogdoc.connectors.sync_store import ConnectorSyncStore


class Clock:
    def __init__(self):
        self.value = 100.0

    def __call__(self):
        return self.value


def _job(store):
    return store.create(
        tenant_id="tenant",
        kb_id="kb",
        connection_id="connection",
        connector_type="git",
    )


def _counters():
    return {
        "pages_processed": 1,
        "documents_seen": 2,
        "documents_fetched": 2,
        "deleted_seen": 0,
        "bytes_fetched": 10,
    }


def test_lease_rotation_rejects_late_worker_and_persists_checkpoint(tmp_path):
    clock = Clock()
    store = ConnectorSyncStore(str(tmp_path / "state.db"), clock=clock)
    job = _job(store)
    _, old_token = store.acquire(job["job_id"], lease_seconds=10)
    clock.value += 11
    _, new_token = store.acquire(job["job_id"], lease_seconds=10)
    with pytest.raises(StaleSyncLease):
        store.complete(job["job_id"], old_token, cursor="old", counters=_counters())
    store.prepare_commit(job["job_id"], new_token)
    completed = store.complete(
        job["job_id"], new_token, cursor="done", counters=_counters()
    )
    assert completed["status"] == "succeeded" and completed["attempt"] == 2
    assert store.checkpoint_for("tenant", "kb", "connection")["cursor"] == "done"
    store.close()


def test_pending_cancel_is_immediately_terminal(tmp_path):
    store = ConnectorSyncStore(str(tmp_path / "state.db"))
    job = _job(store)
    cancelled = store.request_cancel(job["job_id"])
    assert cancelled["status"] == "cancelled" and cancelled["finished_at"] is not None
    with pytest.raises(StaleSyncLease):
        store.acquire(job["job_id"], lease_seconds=10)
    store.close()


def test_expired_worker_cannot_fail_or_cancel_before_lease_rotation(tmp_path):
    clock = Clock()
    store = ConnectorSyncStore(str(tmp_path / "state.db"), clock=clock)
    job = _job(store)
    _, token = store.acquire(job["job_id"], lease_seconds=10)
    clock.value += 11
    with pytest.raises(StaleSyncLease):
        store.fail(
            job["job_id"],
            token,
            error_code="LATE",
            error_message="late",
            retryable=False,
        )
    with pytest.raises(StaleSyncLease):
        store.mark_cancelled(job["job_id"], token)
    store.close()


def test_prepare_commit_closes_cancellation_boundary(tmp_path):
    store = ConnectorSyncStore(str(tmp_path / "state.db"))
    job = _job(store)
    _, token = store.acquire(job["job_id"], lease_seconds=10)
    prepared = store.prepare_commit(job["job_id"], token)
    assert prepared["status"] == "committing"
    assert store.request_cancel(job["job_id"])["status"] == "committing"
    assert (
        store.complete(job["job_id"], token, cursor="done", counters=_counters())[
            "status"
        ]
        == "succeeded"
    )
    store.close()


def test_retry_restarts_from_successful_cursor_and_zero_counters(tmp_path):
    store = ConnectorSyncStore(str(tmp_path / "state.db"))
    job = store.create(
        tenant_id="tenant",
        kb_id="kb",
        connection_id="connection",
        connector_type="git",
        resume_cursor="base",
    )
    _, token = store.acquire(job["job_id"], lease_seconds=10)
    store.checkpoint(
        job["job_id"],
        token,
        cursor="partial",
        counters=_counters(),
        lease_seconds=10,
    )
    store.fail(
        job["job_id"],
        token,
        error_code="RETRY",
        error_message="retry",
        retryable=True,
    )
    restarted, _ = store.acquire(job["job_id"], lease_seconds=10)
    assert restarted["cursor"] == "base"
    assert restarted["pages_processed"] == 0
    assert restarted["bytes_fetched"] == 0
    store.close()


def test_dead_letter_replay_is_new_linked_job_and_original_is_immutable(tmp_path):
    store = ConnectorSyncStore(str(tmp_path / "state.db"))
    original = _job(store)
    _, token = store.acquire(original["job_id"], lease_seconds=10)
    dead_letter = store.fail(
        original["job_id"],
        token,
        error_code="THROTTLED",
        error_message="retry budget exhausted",
        retryable=False,
        dead_letter=True,
    )

    replay = store.replay_dead_letter(original["job_id"])

    assert dead_letter["status"] == "dead_letter"
    assert replay["job_id"] != original["job_id"]
    assert replay["status"] == "pending"
    assert replay["replay_of"] == original["job_id"]
    assert store.get(original["job_id"]) == dead_letter
    with pytest.raises(ValueError, match="only a dead-letter"):
        store.replay_dead_letter(replay["job_id"])
    store.close()


def test_schedule_and_health_snapshot_survive_restart(tmp_path):
    clock = Clock()
    path = str(tmp_path / "state.db")
    store = ConnectorSyncStore(path, clock=clock)
    scheduled = store.ensure_schedule(
        tenant_id="tenant",
        kb_id="kb",
        connection_id="connection",
        schedule_seconds=300,
    )
    assert scheduled["next_run_at"] == 400.0
    job = _job(store)
    running, token = store.acquire(job["job_id"], lease_seconds=10)
    store.record_health(running["job_id"], duration_seconds=0.5)
    failed = store.fail(
        job["job_id"],
        token,
        error_code="AUTHENTICATIONFAILED",
        error_message="authentication failed",
        retryable=False,
    )
    store.record_health(failed["job_id"], duration_seconds=1.5)
    store.close()

    reopened = ConnectorSyncStore(path, clock=clock)
    health = reopened.health_snapshot("tenant", "kb", "connection")
    assert health["next_run_at"] == 400.0
    assert health["health_status"] == "failed"
    assert health["last_error_code"] == "AUTHENTICATIONFAILED"
    assert health["last_duration_seconds"] == 1.5
    assert health["consecutive_failures"] == 1
    assert health["backlog"] == 0
    reopened.close()


def test_create_if_idle_deduplicates_active_connection_jobs(tmp_path):
    store = ConnectorSyncStore(str(tmp_path / "state.db"))
    first = store.create_if_idle(
        tenant_id="tenant",
        kb_id="kb",
        connection_id="connection",
        connector_type="git",
    )
    second = store.create_if_idle(
        tenant_id="tenant",
        kb_id="kb",
        connection_id="connection",
        connector_type="git",
    )
    assert second["job_id"] == first["job_id"]
    assert store.backlog_size("tenant", "kb", connection_id="connection") == 1
    store.close()


def test_job_sequence_orders_equal_timestamp_jobs_by_creation(tmp_path, monkeypatch):
    clock = Clock()
    identifiers = iter(("f" * 32, "0" * 32))
    monkeypatch.setattr(
        "cogdoc.connectors.sync_store.uuid4",
        lambda: SimpleNamespace(hex=next(identifiers)),
    )
    store = ConnectorSyncStore(str(tmp_path / "state.db"), clock=clock)
    older = _job(store)
    newer = _job(store)

    assert older["job_sequence"] == 1
    assert newer["job_sequence"] == 2
    assert [row["job_id"] for row in store.list_jobs("tenant", "kb")] == [
        newer["job_id"],
        older["job_id"],
    ]
    store.record_health(newer["job_id"], duration_seconds=0)
    assert (
        store.health_snapshot("tenant", "kb", "connection")["last_job_id"]
        == (newer["job_id"])
    )
    store.close()


def test_job_sequence_is_concurrent_persistent_and_never_reused(tmp_path):
    path = str(tmp_path / "state.db")
    first = ConnectorSyncStore(path, clock=lambda: 100.0)
    second = ConnectorSyncStore(path, clock=lambda: 100.0)
    barrier = Barrier(2)

    def create_many(store):
        barrier.wait()
        return [_job(store) for _ in range(8)]

    with ThreadPoolExecutor(max_workers=2) as executor:
        batches = [
            executor.submit(create_many, first),
            executor.submit(create_many, second),
        ]
        jobs = [job for batch in batches for job in batch.result()]
    sequences = sorted(job["job_sequence"] for job in jobs)
    assert sequences == list(range(1, 17))

    for job in jobs:
        first.request_cancel(job["job_id"])
    assert first.delete_scope("tenant", "kb")["jobs"] == 16
    second.close()
    first.close()

    reopened = ConnectorSyncStore(path, clock=lambda: 100.0)
    assert _job(reopened)["job_sequence"] == 17
    reopened.close()


def test_prepare_commit_and_cancel_have_one_atomic_winner(tmp_path):
    path = str(tmp_path / "state.db")
    preparing = ConnectorSyncStore(path)
    cancelling = ConnectorSyncStore(path)
    job = _job(preparing)
    _, token = preparing.acquire(job["job_id"], lease_seconds=10)
    barrier = Barrier(2)

    def prepare():
        barrier.wait()
        try:
            return preparing.prepare_commit(job["job_id"], token)["status"]
        except SyncCancelled:
            return "cancel-won"

    def cancel():
        barrier.wait()
        return cancelling.request_cancel(job["job_id"])["status"]

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = {executor.submit(prepare), executor.submit(cancel)}
        results = {future.result() for future in outcomes}
    persisted = preparing.get(job["job_id"])
    if persisted["status"] == "committing":
        assert results == {"committing"}
        assert persisted["cancel_requested"] is False
    else:
        assert results == {"cancel-won", "running"}
        assert persisted["status"] == "running"
        assert persisted["cancel_requested"] is True
    cancelling.close()
    preparing.close()


def test_late_health_projection_cannot_overwrite_newer_job(tmp_path):
    clock = Clock()
    store = ConnectorSyncStore(str(tmp_path / "state.db"), clock=clock)
    old_job = _job(store)
    _, old_token = store.acquire(old_job["job_id"], lease_seconds=10)
    store.prepare_commit(old_job["job_id"], old_token)
    store.complete(old_job["job_id"], old_token, cursor="old", counters=_counters())

    clock.value += 1
    new_job = _job(store)
    _, new_token = store.acquire(new_job["job_id"], lease_seconds=10)
    store.fail(
        new_job["job_id"],
        new_token,
        error_code="NEW_FAILURE",
        error_message="newer failure",
        retryable=False,
    )
    store.record_health(new_job["job_id"], duration_seconds=2)
    store.record_health(old_job["job_id"], duration_seconds=3)

    health = store.health_snapshot("tenant", "kb", "connection")
    assert health["last_job_id"] == new_job["job_id"]
    assert health["health_status"] == "failed"
    assert health["last_error_code"] == "NEW_FAILURE"
    assert health["consecutive_failures"] == 1
    assert health["last_success_at"] == 100.0
    assert health["last_failure_at"] == 101.0
    assert health["last_duration_seconds"] == 2
    store.close()


def test_out_of_order_health_rebuilds_full_consecutive_failure_history(tmp_path):
    clock = Clock()
    store = ConnectorSyncStore(str(tmp_path / "state.db"), clock=clock)
    succeeded = _job(store)
    _, token = store.acquire(succeeded["job_id"], lease_seconds=10)
    store.prepare_commit(succeeded["job_id"], token)
    store.complete(succeeded["job_id"], token, cursor="ok", counters=_counters())
    store.record_health(succeeded["job_id"], duration_seconds=1)

    failures = []
    for index in range(2):
        clock.value += 1
        failed = _job(store)
        _, token = store.acquire(failed["job_id"], lease_seconds=10)
        store.fail(
            failed["job_id"],
            token,
            error_code=f"FAILURE_{index}",
            error_message="failure",
            retryable=False,
        )
        failures.append(failed)
    store.record_health(failures[1]["job_id"], duration_seconds=3)
    store.record_health(failures[0]["job_id"], duration_seconds=2)

    health = store.health_snapshot("tenant", "kb", "connection")
    assert health["last_job_id"] == failures[1]["job_id"]
    assert health["last_error_code"] == "FAILURE_1"
    assert health["consecutive_failures"] == 2
    assert health["last_success_at"] == 100.0
    assert health["last_failure_at"] == 102.0
    assert health["last_duration_seconds"] == 3
    store.close()


def test_health_projection_sql_is_history_independent(tmp_path, monkeypatch):
    store = ConnectorSyncStore(str(tmp_path / "state.db"), clock=lambda: 100.0)
    jobs = [_job(store) for _ in range(256)]
    statements = []
    monkeypatch.setattr(store, "health_snapshot", lambda *_scope: {})
    store._conn.set_trace_callback(statements.append)
    store.record_health(jobs[-1]["job_id"], duration_seconds=0)
    store._conn.set_trace_callback(None)

    ledger_reads = [
        statement
        for statement in statements
        if statement.lstrip().upper().startswith("SELECT")
        and "connector_sync_jobs" in statement
    ]
    assert len(ledger_reads) == 1
    assert "WHERE job_id=" in ledger_reads[0]
    assert all(
        "MAX(" not in statement and "SUM(" not in statement for statement in statements
    )
    health = ConnectorSyncStore.health_snapshot(store, "tenant", "kb", "connection")
    assert health["last_job_id"] == jobs[-1]["job_id"]
    assert health["consecutive_failures"] == 0
    store.close()


def test_same_job_failure_counts_once_and_success_resets_streak(tmp_path):
    clock = Clock()
    store = ConnectorSyncStore(str(tmp_path / "state.db"), clock=clock)
    job = _job(store)

    _, token = store.acquire(job["job_id"], lease_seconds=10)
    store.fail(
        job["job_id"],
        token,
        error_code="FIRST",
        error_message="first",
        retryable=True,
    )
    first_failure_at = store.health_snapshot("tenant", "kb", "connection")[
        "last_failure_at"
    ]
    clock.value += 1
    _, token = store.acquire(job["job_id"], lease_seconds=10)
    store.fail(
        job["job_id"],
        token,
        error_code="SECOND",
        error_message="second",
        retryable=True,
    )
    health = store.health_snapshot("tenant", "kb", "connection")
    assert health["consecutive_failures"] == 1
    assert health["last_failure_at"] > first_failure_at

    clock.value += 1
    _, token = store.acquire(job["job_id"], lease_seconds=10)
    store.prepare_commit(job["job_id"], token)
    store.complete(job["job_id"], token, cursor="done", counters=_counters())
    health = store.health_snapshot("tenant", "kb", "connection")
    assert health["health_status"] == "healthy"
    assert health["consecutive_failures"] == 0
    assert health["last_failure_at"] == 101.0
    store.close()


def test_commit_recovery_wait_projects_retry_health_and_counts_job_once(tmp_path):
    clock = Clock()
    store = ConnectorSyncStore(str(tmp_path / "state.db"), clock=clock)
    job = _job(store)
    _, token = store.acquire(job["job_id"], lease_seconds=10)
    store.prepare_commit(job["job_id"], token)
    store.fail(
        job["job_id"],
        token,
        error_code="COMMIT_RETRY",
        error_message="retry commit",
        retryable=True,
        retry_delay_seconds=1,
        preserve_committing=True,
    )
    health = store.health_snapshot("tenant", "kb", "connection")
    assert health["health_status"] == "retrying"
    assert health["last_job_status"] == "committing"
    assert health["last_error_code"] == "COMMIT_RETRY"
    assert health["last_failure_at"] == 100.0
    assert health["consecutive_failures"] == 1

    clock.value += 1
    _, token = store.acquire(job["job_id"], lease_seconds=10)
    store.fail(
        job["job_id"],
        token,
        error_code="COMMIT_RETRY_AGAIN",
        error_message="retry commit again",
        retryable=True,
        retry_delay_seconds=1,
        preserve_committing=True,
    )
    assert (
        store.health_snapshot("tenant", "kb", "connection")["consecutive_failures"] == 1
    )
    store.close()


def test_late_old_duration_updates_ledger_without_overwriting_latest(tmp_path):
    clock = Clock()
    store = ConnectorSyncStore(str(tmp_path / "state.db"), clock=clock)
    old = _job(store)
    _, token = store.acquire(old["job_id"], lease_seconds=10)
    store.prepare_commit(old["job_id"], token)
    store.complete(old["job_id"], token, cursor="old", counters=_counters())
    store.record_health(old["job_id"], duration_seconds=1)

    clock.value += 1
    latest = _job(store)
    _, token = store.acquire(latest["job_id"], lease_seconds=10)
    store.fail(
        latest["job_id"],
        token,
        error_code="LATEST",
        error_message="latest",
        retryable=False,
    )
    store.record_health(latest["job_id"], duration_seconds=2)
    store.record_health(old["job_id"], duration_seconds=99)

    assert store.get(old["job_id"])["health_duration_seconds"] == 99
    health = store.health_snapshot("tenant", "kb", "connection")
    assert health["last_job_id"] == latest["job_id"]
    assert health["last_duration_seconds"] == 2
    store.close()


def test_newer_pending_job_is_not_regressed_by_old_terminal_projection(tmp_path):
    store = ConnectorSyncStore(str(tmp_path / "state.db"), clock=lambda: 100.0)
    old = _job(store)
    _, token = store.acquire(old["job_id"], lease_seconds=10)
    store.prepare_commit(old["job_id"], token)
    latest = _job(store)

    store.complete(old["job_id"], token, cursor="old", counters=_counters())
    store.record_health(old["job_id"], duration_seconds=8)

    health = store.health_snapshot("tenant", "kb", "connection")
    assert health["last_job_id"] == latest["job_id"]
    assert health["health_status"] == "queued"
    assert health["last_duration_seconds"] is None
    store.close()


def test_terminal_transition_projects_health_before_record_health(tmp_path):
    store = ConnectorSyncStore(str(tmp_path / "state.db"), clock=lambda: 100.0)
    job = _job(store)
    _, token = store.acquire(job["job_id"], lease_seconds=10)
    store.fail(
        job["job_id"],
        token,
        error_code="CRASH_WINDOW",
        error_message="failed before observer projection",
        retryable=False,
    )

    health = store.health_snapshot("tenant", "kb", "connection")
    assert health["health_status"] == "failed"
    assert health["last_error_code"] == "CRASH_WINDOW"
    assert health["last_duration_seconds"] == 0
    assert health["consecutive_failures"] == 1
    store.close()


def test_terminal_crash_fallback_uses_current_attempt_wall_duration(tmp_path):
    clock = Clock()
    store = ConnectorSyncStore(str(tmp_path / "state.db"), clock=clock)
    job = _job(store)
    _, token = store.acquire(job["job_id"], lease_seconds=20)
    clock.value += 7
    store.fail(
        job["job_id"],
        token,
        error_code="CRASH_WINDOW",
        error_message="failed",
        retryable=True,
    )
    assert (
        store.health_snapshot("tenant", "kb", "connection")["last_duration_seconds"]
        == 7
    )

    clock.value += 1
    _, token = store.acquire(job["job_id"], lease_seconds=20)
    clock.value += 3
    store.prepare_commit(job["job_id"], token)
    store.complete(job["job_id"], token, cursor=None, counters=_counters())
    health = store.health_snapshot("tenant", "kb", "connection")
    assert health["last_duration_seconds"] == 3
    assert health["consecutive_failures"] == 0
    store.close()


@pytest.mark.parametrize("duration", [float("nan"), float("inf"), float("-inf")])
def test_record_health_rejects_non_finite_duration(tmp_path, duration):
    store = ConnectorSyncStore(str(tmp_path / "state.db"))
    job = _job(store)
    with pytest.raises(ValueError, match="finite"):
        store.record_health(job["job_id"], duration_seconds=duration)
    store.close()


def test_prune_terminal_jobs_is_bounded_cleanup_safe_and_retains_replay_lineage(
    tmp_path,
):
    store = ConnectorSyncStore(str(tmp_path / "state.db"), clock=lambda: 100.0)
    parent = _job(store)
    _, token = store.acquire(parent["job_id"], lease_seconds=10)
    store.fail(
        parent["job_id"],
        token,
        error_code="DEAD",
        error_message="dead",
        retryable=False,
        dead_letter=True,
    )
    child = store.replay_dead_letter(parent["job_id"])
    _, token = store.acquire(child["job_id"], lease_seconds=10)
    store.fail(
        child["job_id"],
        token,
        error_code="FAILED",
        error_message="failed",
        retryable=False,
    )
    unrelated = _job(store)
    _, token = store.acquire(unrelated["job_id"], lease_seconds=10)
    store.fail(
        unrelated["job_id"],
        token,
        error_code="FAILED",
        error_message="failed",
        retryable=False,
    )
    active = _job(store)
    cleanup_pending = _job(store)
    _, token = store.acquire(cleanup_pending["job_id"], lease_seconds=10)
    store.prepare_commit(cleanup_pending["job_id"], token)
    store.complete(cleanup_pending["job_id"], token, cursor=None, counters=_counters())

    assert store.prune_terminal_jobs(older_than=200, limit=1) == 1
    assert store.get(parent["job_id"]) is not None
    assert store.get(active["job_id"])["status"] == "pending"
    assert store.get(cleanup_pending["job_id"])["cleanup_pending"] is True
    assert store.prune_terminal_jobs(older_than=200, limit=10) == 2
    assert store.get(parent["job_id"]) is None
    assert store.get(cleanup_pending["job_id"]) is not None
    store.mark_cleanup_complete(cleanup_pending["job_id"])
    assert store.prune_terminal_jobs(older_than=200, limit=10) == 0
    assert store.get(cleanup_pending["job_id"]) is not None
    assert store.get(active["job_id"]) is not None
    store.close()


def test_retire_connection_requires_quiescence_and_keeps_terminal_audit_jobs(
    tmp_path,
):
    store = ConnectorSyncStore(str(tmp_path / "state.db"), clock=lambda: 100.0)
    active = _job(store)
    with pytest.raises(ValueError, match="active sync jobs"):
        store.retire_connection("tenant", "kb", "connection")

    store.request_cancel(active["job_id"])
    succeeded = _job(store)
    _, token = store.acquire(succeeded["job_id"], lease_seconds=10)
    store.prepare_commit(succeeded["job_id"], token)
    store.complete(succeeded["job_id"], token, cursor="done", counters=_counters())
    prefixed_connection = store.create(
        tenant_id="tenant",
        kb_id="kb",
        connection_id="connection-other",
        connector_type="git",
    )

    assert store.connection_job_ids("tenant", "kb", "connection") == (
        active["job_id"],
        succeeded["job_id"],
    )
    assert store.connection_job_ids("tenant", "kb", "connection-other") == (
        prefixed_connection["job_id"],
    )

    assert store.checkpoint_for("tenant", "kb", "connection") is not None
    assert (
        store.health_snapshot("tenant", "kb", "connection")["last_job_id"]
        == (succeeded["job_id"])
    )
    assert store.retire_connection("tenant", "kb", "connection") == {
        "checkpoints": 1,
        "health": 1,
    }
    assert store.checkpoint_for("tenant", "kb", "connection") is None
    assert store.health_snapshot("tenant", "kb", "connection")["last_job_id"] is None
    assert store.get(active["job_id"])["status"] == "cancelled"
    assert store.get(succeeded["job_id"])["status"] == "succeeded"
    store.close()


def test_prune_preserves_live_health_and_checkpoint_target_until_retired(tmp_path):
    store = ConnectorSyncStore(str(tmp_path / "state.db"), clock=lambda: 100.0)
    job = _job(store)
    _, token = store.acquire(job["job_id"], lease_seconds=10)
    store.prepare_commit(job["job_id"], token)
    store.complete(job["job_id"], token, cursor="done", counters=_counters())
    store.mark_cleanup_complete(job["job_id"])

    assert store.prune_terminal_jobs(older_than=200, limit=10) == 0
    assert store.get(job["job_id"]) is not None
    store.retire_connection("tenant", "kb", "connection")
    assert store.prune_terminal_jobs(older_than=200, limit=10) == 1
    assert store.get(job["job_id"]) is None
    store.close()
