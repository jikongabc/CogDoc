import pytest

from cogdoc.connectors.base import StaleSyncLease
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
