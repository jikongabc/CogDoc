import time
import threading
from concurrent.futures import Future
from types import SimpleNamespace

import pytest

from cogdoc.api.app import create_app
from cogdoc.connectors.base import SyncCancelled
from cogdoc.connectors.connection_store import ConnectionStore
from cogdoc.connectors.manager import SyncManager
from cogdoc.connectors.sync_runtime import ConnectorSyncRuntime, SyncLimits
from cogdoc.connectors.sync_store import ConnectorSyncStore


class Clock:
    def __init__(self):
        self.value = 100.0

    def __call__(self):
        return self.value


@pytest.mark.parametrize(
    "mismatch",
    ["connection_store", "sync_store", "runtime_store"],
)
def test_create_app_rejects_sync_manager_with_mismatched_store_identity(
    tmp_path, mismatch
):
    connection_store = ConnectionStore(str(tmp_path / "connections.db"))
    sync_store = ConnectorSyncStore(str(tmp_path / "sync.db"))
    other_connection_store = ConnectionStore(str(tmp_path / "other-connections.db"))
    other_sync_store = ConnectorSyncStore(str(tmp_path / "other-sync.db"))
    manager = SimpleNamespace(
        connection_store=(
            other_connection_store
            if mismatch == "connection_store"
            else connection_store
        ),
        sync_store=other_sync_store if mismatch == "sync_store" else sync_store,
        runtime=SimpleNamespace(
            store=(other_sync_store if mismatch == "runtime_store" else sync_store)
        ),
    )
    try:
        with pytest.raises(ValueError, match="must share the app connector stores"):
            create_app(
                connection_store=connection_store,
                connector_sync_store=sync_store,
                sync_manager=manager,
            )
    finally:
        other_sync_store.close()
        other_connection_store.close()
        sync_store.close()
        connection_store.close()


def test_create_app_rejects_injected_manager_without_control_plane_binding(tmp_path):
    connection_store = ConnectionStore(str(tmp_path / "connections.db"))
    sync_store = ConnectorSyncStore(str(tmp_path / "sync.db"))
    manager = SimpleNamespace(
        connection_store=connection_store,
        sync_store=sync_store,
        runtime=SimpleNamespace(store=sync_store),
    )
    try:
        with pytest.raises(ValueError, match="trusted control-plane"):
            create_app(
                connection_store=connection_store,
                connector_sync_store=sync_store,
                sync_manager=manager,
            )
    finally:
        sync_store.close()
        connection_store.close()


def _connection(store, tmp_path, *, scheduled=False):
    config = {"root": str(tmp_path)}
    if scheduled:
        config["schedule_seconds"] = 300
    return store.create(
        tenant_id="tenant",
        kb_id="kb",
        connector_type="local-directory",
        name="source",
        config=config,
        secret_env={},
        owner_id="owner",
    )


def _credential_connection(store):
    return store.create(
        tenant_id="tenant",
        kb_id="kb",
        connector_type="notion",
        name="notion",
        config={},
        secret_env={},
        credential_id="cred-1",
        credential_fields={"token"},
        owner_id="owner",
    )


def _wait_terminal(sync_store, job_id):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        job = sync_store.get(job_id)
        if job and job["status"] in {
            "succeeded",
            "failed",
            "dead_letter",
            "cancelled",
        }:
            return job
        time.sleep(0.01)
    raise AssertionError("sync job did not reach a terminal state")


def test_sink_startup_failure_dead_letters_and_manager_replays_new_job(tmp_path):
    connection_store = ConnectionStore(str(tmp_path / "state.db"))
    sync_store = ConnectorSyncStore(str(tmp_path / "state.db"))
    connection = _connection(connection_store, tmp_path, scheduled=True)
    runtime = ConnectorSyncRuntime(
        sync_store, limits=SyncLimits(max_attempts=2, retry_base_seconds=0)
    )
    manager = SyncManager(
        connection_store,
        sync_store,
        runtime,
        sink_builder=lambda connection: (_ for _ in ()).throw(
            RuntimeError("injected sink crash")
        ),
    )

    original = manager.submit(connection["connection_id"])
    dead_letter = _wait_terminal(sync_store, original["job_id"])
    connection_store.set_enabled(connection["connection_id"], True)
    replay = manager.replay(original["job_id"])
    replayed_terminal = _wait_terminal(sync_store, replay["job_id"])

    assert dead_letter["status"] == "dead_letter"
    assert dead_letter["attempt"] == 2
    assert original["connection_revision"] == 1
    assert replay["connection_revision"] == 2
    assert replay["replay_of"] == original["job_id"]
    assert replayed_terminal["status"] == "dead_letter"
    assert replayed_terminal["attempt"] == 2
    assert sync_store.get(original["job_id"]) == dead_letter
    assert manager.health(connection["connection_id"])["next_run_at"] is None
    manager.shutdown()
    sync_store.close()
    connection_store.close()


def test_recover_persists_schedule_and_keeps_one_scheduler_thread(tmp_path):
    clock = Clock()
    path = str(tmp_path / "state.db")
    connection_store = ConnectionStore(path)
    sync_store = ConnectorSyncStore(path, clock=clock)
    connection = _connection(connection_store, tmp_path, scheduled=True)
    manager = SyncManager(
        connection_store,
        sync_store,
        ConnectorSyncRuntime(sync_store),
        sink_builder=lambda connection: None,
        clock=clock,
    )

    assert manager.recover() == 0
    first_health = manager.health(connection["connection_id"])
    assert manager.recover() == 0
    second_health = manager.health(connection["connection_id"])

    assert first_health["next_run_at"] == 400.0
    assert second_health["next_run_at"] == 400.0
    assert len(manager._connection_schedules) == 1
    assert manager._scheduler_thread.is_alive()
    manager.shutdown()

    restarted = SyncManager(
        connection_store,
        sync_store,
        ConnectorSyncRuntime(sync_store),
        sink_builder=lambda connection: None,
        clock=clock,
    )
    restarted.recover()
    assert restarted.health(connection["connection_id"])["next_run_at"] == 400.0
    assert len(restarted._connection_schedules) == 1
    assert restarted._scheduler_thread.is_alive()
    restarted.shutdown()
    sync_store.close()
    connection_store.close()


def test_disable_cancels_precommit_work_and_reenable_restores_schedule(tmp_path):
    connection_store = ConnectionStore(str(tmp_path / "state.db"))
    sync_store = ConnectorSyncStore(str(tmp_path / "state.db"))
    connection = _connection(connection_store, tmp_path, scheduled=True)
    manager = SyncManager(
        connection_store,
        sync_store,
        ConnectorSyncRuntime(sync_store),
        sink_builder=lambda connection: None,
    )
    pending = sync_store.create(
        tenant_id="tenant",
        kb_id="kb",
        connection_id=connection["connection_id"],
        connector_type="local-directory",
    )

    disabled = manager.set_connection_enabled(connection["connection_id"], False)
    assert disabled["enabled"] is False
    assert sync_store.get(pending["job_id"])["status"] == "cancelled"
    health = sync_store.health_snapshot("tenant", "kb", connection["connection_id"])
    assert health["schedule_seconds"] is None
    assert health["next_run_at"] is None

    enabled = manager.set_connection_enabled(connection["connection_id"], True)
    assert enabled["enabled"] is True
    health = sync_store.health_snapshot("tenant", "kb", connection["connection_id"])
    assert health["schedule_seconds"] == 300
    assert health["next_run_at"] is not None
    manager.shutdown()
    sync_store.close()
    connection_store.close()


def test_connection_mutation_rejects_ambiguous_committing_job(tmp_path):
    connection_store = ConnectionStore(str(tmp_path / "state.db"))
    sync_store = ConnectorSyncStore(str(tmp_path / "state.db"))
    connection = _connection(connection_store, tmp_path)
    manager = SyncManager(
        connection_store,
        sync_store,
        ConnectorSyncRuntime(sync_store),
        sink_builder=lambda connection: None,
    )
    job = sync_store.create(
        tenant_id="tenant",
        kb_id="kb",
        connection_id=connection["connection_id"],
        connector_type="local-directory",
    )
    _, token = sync_store.acquire(job["job_id"], lease_seconds=60)
    sync_store.prepare_commit(job["job_id"], token)

    with pytest.raises(ValueError, match="commit in progress"):
        manager.set_connection_enabled(connection["connection_id"], False)
    with pytest.raises(ValueError, match="commit in progress"):
        manager.delete_connection(connection["connection_id"])
    assert connection_store.get(connection["connection_id"])["enabled"] is True
    assert sync_store.get(job["job_id"])["status"] == "committing"
    manager.shutdown()
    sync_store.close()
    connection_store.close()


def test_prepare_connection_delete_fences_and_drains_running_worker(tmp_path):
    connection_store = ConnectionStore(str(tmp_path / "state.db"))
    sync_store = ConnectorSyncStore(str(tmp_path / "state.db"))
    connection = _connection(connection_store, tmp_path, scheduled=True)
    manager = SyncManager(
        connection_store,
        sync_store,
        ConnectorSyncRuntime(sync_store),
        sink_builder=lambda connection: None,
    )
    job = sync_store.create(
        tenant_id="tenant",
        kb_id="kb",
        connection_id=connection["connection_id"],
        connector_type="local-directory",
    )
    _, token = sync_store.acquire(job["job_id"], lease_seconds=60)

    def cooperative_worker():
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            current = sync_store.get(job["job_id"])
            if current is not None and current["cancel_requested"]:
                sync_store.mark_cancelled(job["job_id"], token)
                return
            time.sleep(0.005)
        raise AssertionError("worker did not observe the deletion fence")

    worker = threading.Thread(target=cooperative_worker)
    worker.start()
    prepared = manager.prepare_connection_delete(
        "tenant", "kb", connection["connection_id"], timeout_seconds=1
    )
    worker.join(timeout=1)

    assert not worker.is_alive()
    assert prepared["connection"]["enabled"] is False
    assert prepared["connection"]["deleting"] is True
    with pytest.raises(ValueError, match="deletion is in progress"):
        manager.set_connection_enabled(connection["connection_id"], True)
    assert prepared["cancelled"] == 1
    assert prepared["remaining"] == 0
    assert sync_store.get(job["job_id"])["status"] == "cancelled"
    assert (
        sync_store.connection_activity("tenant", "kb", connection["connection_id"])[
            "total"
        ]
        == 0
    )
    health = sync_store.health_snapshot("tenant", "kb", connection["connection_id"])
    assert health["schedule_seconds"] is None
    assert health["next_run_at"] is None
    manager.shutdown()
    sync_store.close()
    connection_store.close()


def test_prepare_connection_delete_fences_but_never_cancels_committing_job(
    tmp_path,
):
    connection_store = ConnectionStore(str(tmp_path / "state.db"))
    sync_store = ConnectorSyncStore(str(tmp_path / "state.db"))
    connection = _connection(connection_store, tmp_path, scheduled=True)
    manager = SyncManager(
        connection_store,
        sync_store,
        ConnectorSyncRuntime(sync_store),
        sink_builder=lambda connection: None,
    )
    sync_store.ensure_schedule(
        tenant_id="tenant",
        kb_id="kb",
        connection_id=connection["connection_id"],
        schedule_seconds=300,
    )
    job = sync_store.create(
        tenant_id="tenant",
        kb_id="kb",
        connection_id=connection["connection_id"],
        connector_type="local-directory",
    )
    _, token = sync_store.acquire(job["job_id"], lease_seconds=60)
    sync_store.prepare_commit(job["job_id"], token)

    with pytest.raises(ValueError, match="commit in progress"):
        manager.prepare_connection_delete(
            "tenant", "kb", connection["connection_id"], timeout_seconds=1
        )

    retained = connection_store.get(connection["connection_id"])
    assert retained["enabled"] is False
    assert retained["deleting"] is True
    assert sync_store.get(job["job_id"])["status"] == "committing"
    health = sync_store.health_snapshot("tenant", "kb", connection["connection_id"])
    assert health["schedule_seconds"] is None
    assert health["next_run_at"] is None
    manager.shutdown()
    sync_store.close()
    connection_store.close()


def test_manual_cancel_of_retry_wait_restores_periodic_schedule(tmp_path):
    clock = Clock()
    connection_store = ConnectionStore(str(tmp_path / "state.db"))
    sync_store = ConnectorSyncStore(str(tmp_path / "state.db"), clock=clock)
    connection = _connection(connection_store, tmp_path, scheduled=True)
    manager = SyncManager(
        connection_store,
        sync_store,
        ConnectorSyncRuntime(sync_store),
        sink_builder=lambda connection: None,
        clock=clock,
    )
    job = sync_store.create(
        tenant_id="tenant",
        kb_id="kb",
        connection_id=connection["connection_id"],
        connector_type="local-directory",
    )
    _, token = sync_store.acquire(job["job_id"], lease_seconds=10)
    sync_store.fail(
        job["job_id"],
        token,
        error_code="RETRY",
        error_message="retry",
        retryable=True,
        retry_delay_seconds=100,
    )
    manager._dispatch(job["job_id"], delay=100)
    assert job["job_id"] in manager._job_schedules

    cancelled = manager.cancel(job["job_id"])
    assert cancelled["status"] == "cancelled"
    assert manager._job_schedules == {}
    assert len(manager._connection_schedules) == 1
    health = manager.health(connection["connection_id"])
    assert health["last_job_status"] == "cancelled"
    assert health["next_run_at"] == 400.0
    due = next(
        entry[0]
        for entry in manager._schedule_heap
        if entry[2] == "connection"
        and manager._connection_schedules.get(entry[3]) == entry[1]
    )
    assert due == 400.0
    manager.shutdown()
    sync_store.close()
    connection_store.close()


def test_replaced_retry_schedule_cannot_dispatch_stale_callback(monkeypatch, tmp_path):
    connection_store = ConnectionStore(str(tmp_path / "state.db"))
    sync_store = ConnectorSyncStore(str(tmp_path / "state.db"))
    connection = _connection(connection_store, tmp_path)
    manager = SyncManager(
        connection_store,
        sync_store,
        ConnectorSyncRuntime(sync_store),
        sink_builder=lambda connection: None,
    )
    job = sync_store.create(
        tenant_id="tenant",
        kb_id="kb",
        connection_id=connection["connection_id"],
        connector_type="local-directory",
    )
    submissions = []

    def submit(callback, *args):
        submissions.append((callback, args))
        future = Future()
        future.set_result(None)
        return future

    monkeypatch.setattr(
        manager._executor,
        "submit",
        submit,
    )

    manager._dispatch(job["job_id"], delay=10)
    stale = manager._job_schedules[job["job_id"]]
    manager._dispatch(job["job_id"], delay=20, replace_timer=True)
    current = manager._job_schedules[job["job_id"]]
    with manager._lock:
        manager._fire_schedule_locked("job", job["job_id"], stale)

    assert submissions == []
    assert manager._job_schedules[job["job_id"]] == current
    manager.shutdown()
    sync_store.close()
    connection_store.close()


def test_replaced_periodic_schedule_cannot_submit_stale_callback(monkeypatch, tmp_path):
    clock = Clock()
    connection_store = ConnectionStore(str(tmp_path / "state.db"))
    sync_store = ConnectorSyncStore(str(tmp_path / "state.db"), clock=clock)
    connection = _connection(connection_store, tmp_path, scheduled=True)
    manager = SyncManager(
        connection_store,
        sync_store,
        ConnectorSyncRuntime(sync_store),
        sink_builder=lambda connection: None,
        clock=clock,
    )

    manager._schedule_connection(connection["connection_id"], next_run_at=200)
    stale = manager._connection_schedules[connection["connection_id"]]
    manager._schedule_connection(connection["connection_id"], next_run_at=400)
    current = manager._connection_schedules[connection["connection_id"]]
    with manager._lock:
        manager._fire_schedule_locked("connection", connection["connection_id"], stale)

    assert sync_store.list_jobs("tenant", "kb") == []
    assert manager._connection_schedules[connection["connection_id"]] == current
    manager.shutdown()
    sync_store.close()
    connection_store.close()


def test_kb_delete_fence_rejects_committing_without_partial_revocation(tmp_path):
    connection_store = ConnectionStore(str(tmp_path / "state.db"))
    sync_store = ConnectorSyncStore(str(tmp_path / "state.db"))
    connection = _connection(connection_store, tmp_path, scheduled=True)
    manager = SyncManager(
        connection_store,
        sync_store,
        ConnectorSyncRuntime(sync_store),
        sink_builder=lambda connection: None,
    )
    job = sync_store.create(
        tenant_id="tenant",
        kb_id="kb",
        connection_id=connection["connection_id"],
        connector_type="local-directory",
    )
    _, token = sync_store.acquire(job["job_id"], lease_seconds=60)
    sync_store.prepare_commit(job["job_id"], token)

    with pytest.raises(ValueError, match="commit in progress"):
        manager.prepare_knowledge_base_delete("tenant", "kb")

    assert connection_store.get(connection["connection_id"])["enabled"] is True
    assert sync_store.get(job["job_id"])["status"] == "committing"
    manager.shutdown()
    sync_store.close()
    connection_store.close()


def test_kb_delete_fence_cancels_and_purges_all_connector_state(tmp_path):
    connection_store = ConnectionStore(str(tmp_path / "state.db"))
    sync_store = ConnectorSyncStore(str(tmp_path / "state.db"))
    first = _connection(connection_store, tmp_path, scheduled=True)
    second = _connection(connection_store, tmp_path, scheduled=False)
    manager = SyncManager(
        connection_store,
        sync_store,
        ConnectorSyncRuntime(sync_store),
        sink_builder=lambda connection: None,
    )
    jobs = [
        sync_store.create(
            tenant_id="tenant",
            kb_id="kb",
            connection_id=connection["connection_id"],
            connector_type="local-directory",
        )
        for connection in (first, second)
    ]

    fenced = manager.prepare_knowledge_base_delete("tenant", "kb", timeout_seconds=0.5)
    assert fenced == {
        "connections": 2,
        "cancelled": 2,
        "previously_enabled_connection_ids": sorted(
            [first["connection_id"], second["connection_id"]]
        ),
    }
    assert all(
        connection_store.get(connection["connection_id"])["enabled"] is False
        for connection in (first, second)
    )
    assert all(sync_store.get(job["job_id"])["status"] == "cancelled" for job in jobs)

    purged = manager.purge_knowledge_base("tenant", "kb")
    assert purged == {"connections": 2, "jobs": 2, "checkpoints": 0, "health": 2}
    assert connection_store.list_entries("tenant", "kb") == []
    assert sync_store.list_jobs("tenant", "kb") == []
    manager.shutdown()
    sync_store.close()
    connection_store.close()


def test_heap_scheduler_is_bounded_for_thousands_of_entries(tmp_path):
    connection_store = ConnectionStore(str(tmp_path / "state.db"))
    sync_store = ConnectorSyncStore(str(tmp_path / "state.db"))
    manager = SyncManager(
        connection_store,
        sync_store,
        ConnectorSyncRuntime(sync_store),
        sink_builder=lambda connection: None,
    )

    for index in range(3000):
        manager._schedule_connection(f"connection-{index}", next_run_at=10**12)
    scheduler = manager._scheduler_thread
    assert scheduler is not None and scheduler.is_alive()
    assert (
        sum(
            thread is scheduler
            for thread in threading.enumerate()
            if thread.name == "cogdoc-sync-scheduler"
        )
        == 1
    )
    assert len(manager._connection_schedules) == 3000

    for index in range(3000):
        manager._schedule_connection(f"connection-{index}", next_run_at=10**12 + 1)
    assert len(manager._schedule_heap) <= 2 * len(manager._connection_schedules)
    manager.shutdown()
    assert not scheduler.is_alive()
    sync_store.close()
    connection_store.close()


def test_recover_pages_every_job_beyond_first_thousand(monkeypatch, tmp_path):
    connection_store = ConnectionStore(str(tmp_path / "state.db"))
    sync_store = ConnectorSyncStore(str(tmp_path / "state.db"))
    manager = SyncManager(
        connection_store,
        sync_store,
        ConnectorSyncRuntime(sync_store),
        sink_builder=lambda connection: None,
        clock=lambda: 100.0,
    )
    jobs = [
        {
            "job_id": f"job-{sequence}",
            "job_sequence": sequence,
            "connection_id": f"connection-{sequence}",
            "status": "pending",
            "retry_at": None,
            "lease_expires_at": None,
        }
        for sequence in range(1, 1002)
    ]
    cursors = []

    def recoverable(*, limit, after_sequence):
        cursors.append(after_sequence)
        return [job for job in jobs if job["job_sequence"] > after_sequence][:limit]

    dispatched = []
    monkeypatch.setattr(sync_store, "recoverable", recoverable)
    monkeypatch.setattr(sync_store, "latest_terminal_jobs", lambda: [])
    monkeypatch.setattr(connection_store, "enabled", lambda: [])
    monkeypatch.setattr(
        manager,
        "_dispatch",
        lambda job_id, *, delay, replace_timer=False: dispatched.append(job_id),
    )

    assert manager.recover() == 1001
    assert cursors == [0, 1000]
    assert dispatched[0] == "job-1" and dispatched[-1] == "job-1001"
    manager.shutdown()
    sync_store.close()
    connection_store.close()


def test_restore_kb_delete_reenables_only_originally_enabled_connections(tmp_path):
    connection_store = ConnectionStore(str(tmp_path / "state.db"))
    sync_store = ConnectorSyncStore(str(tmp_path / "state.db"))
    scheduled = _connection(connection_store, tmp_path, scheduled=True)
    disabled = _connection(connection_store, tmp_path, scheduled=False)
    connection_store.set_enabled(disabled["connection_id"], False)
    manager = SyncManager(
        connection_store,
        sync_store,
        ConnectorSyncRuntime(sync_store),
        sink_builder=lambda connection: None,
    )

    fenced = manager.prepare_knowledge_base_delete("tenant", "kb", timeout_seconds=0.5)
    assert fenced["previously_enabled_connection_ids"] == [scheduled["connection_id"]]
    restored = manager.restore_knowledge_base_delete(
        "tenant", "kb", fenced["previously_enabled_connection_ids"]
    )

    assert restored == {"restored": 1, "scheduled": 1}
    assert connection_store.get(scheduled["connection_id"])["enabled"] is True
    assert connection_store.get(disabled["connection_id"])["enabled"] is False
    assert scheduled["connection_id"] in manager._connection_schedules
    manager.shutdown()
    sync_store.close()
    connection_store.close()


def test_credential_rotation_between_snapshot_and_create_cancels_before_build(
    monkeypatch, tmp_path
):
    connection_store = ConnectionStore(str(tmp_path / "state.db"))
    sync_store = ConnectorSyncStore(str(tmp_path / "state.db"))
    connection = _credential_connection(connection_store)
    live_revision = [1]
    builders = []
    manager = SyncManager(
        connection_store,
        sync_store,
        ConnectorSyncRuntime(sync_store),
        sink_builder=lambda value: builders.append(("sink", value)),
        connector_builder=lambda value: builders.append(("connector", value)),
        credential_snapshotter=lambda value: (
            str(value["credential_id"]),
            live_revision[0],
        ),
        job_admission_checker=lambda job, _connection: (
            int(job["credential_revision"]) == live_revision[0]
        ),
    )
    create_if_idle = sync_store.create_if_idle

    def rotate_after_create(**kwargs):
        job = create_if_idle(**kwargs)
        live_revision[0] = 2
        return job

    monkeypatch.setattr(sync_store, "create_if_idle", rotate_after_create)
    job = manager.submit(connection["connection_id"])
    terminal = _wait_terminal(sync_store, job["job_id"])

    assert job["credential_id"] == "cred-1"
    assert job["credential_revision"] == 1
    assert terminal["status"] == "cancelled"
    assert builders == []
    manager.shutdown()
    sync_store.close()
    connection_store.close()


def test_authority_revoked_inside_connector_builder_never_builds_sink(tmp_path):
    connection_store = ConnectionStore(str(tmp_path / "state.db"))
    sync_store = ConnectorSyncStore(str(tmp_path / "state.db"))
    connection = _connection(connection_store, tmp_path)
    sink_builds = []
    manager = SyncManager(
        connection_store,
        sync_store,
        ConnectorSyncRuntime(sync_store),
        sink_builder=lambda value: sink_builds.append(value),
        connector_builder=lambda value: (_ for _ in ()).throw(
            SyncCancelled("authority revoked during build")
        ),
    )
    job = sync_store.create(
        tenant_id="tenant",
        kb_id="kb",
        connection_id=connection["connection_id"],
        connector_type="local-directory",
        connection_revision=int(connection["revision"]),
    )

    manager._run(job["job_id"])

    assert sync_store.get(job["job_id"])["status"] == "cancelled"
    assert sink_builds == []
    manager.shutdown()
    sync_store.close()
    connection_store.close()


def test_dead_letter_replay_snapshots_current_credential_revision(
    monkeypatch, tmp_path
):
    connection_store = ConnectionStore(str(tmp_path / "state.db"))
    sync_store = ConnectorSyncStore(str(tmp_path / "state.db"))
    connection = _credential_connection(connection_store)
    original = sync_store.create(
        tenant_id="tenant",
        kb_id="kb",
        connection_id=connection["connection_id"],
        connector_type="notion",
        credential_id="cred-1",
        credential_revision=1,
    )
    _, token = sync_store.acquire(original["job_id"], lease_seconds=60)
    sync_store.fail(
        original["job_id"],
        token,
        error_code="FAILED",
        error_message="failed",
        retryable=False,
        dead_letter=True,
    )
    manager = SyncManager(
        connection_store,
        sync_store,
        ConnectorSyncRuntime(sync_store),
        sink_builder=lambda value: None,
        credential_snapshotter=lambda value: (str(value["credential_id"]), 2),
        job_admission_checker=lambda job, connection: True,
    )
    monkeypatch.setattr(manager, "_dispatch", lambda *args, **kwargs: None)

    replay = manager.replay(original["job_id"])

    assert replay["credential_revision"] == 2
    assert replay["credential_id"] == "cred-1"
    manager.shutdown()
    sync_store.close()
    connection_store.close()


def test_legacy_credential_job_revision_zero_is_rejected_before_build(tmp_path):
    connection_store = ConnectionStore(str(tmp_path / "state.db"))
    sync_store = ConnectorSyncStore(str(tmp_path / "state.db"))
    connection = _credential_connection(connection_store)
    job = sync_store.create(
        tenant_id="tenant",
        kb_id="kb",
        connection_id=connection["connection_id"],
        connector_type="notion",
        credential_id="cred-1",
        credential_revision=1,
    )
    sync_store._conn.execute(
        "UPDATE connector_sync_jobs SET credential_revision=0 WHERE job_id=?",
        (job["job_id"],),
    )
    builders = []
    manager = SyncManager(
        connection_store,
        sync_store,
        ConnectorSyncRuntime(sync_store),
        sink_builder=lambda value: builders.append("sink"),
        connector_builder=lambda value: builders.append("connector"),
        credential_snapshotter=lambda value: (str(value["credential_id"]), 1),
        job_admission_checker=lambda queued, connection: (
            int(queued["credential_revision"]) == 1
        ),
    )

    manager._run(job["job_id"])

    assert sync_store.get(job["job_id"])["status"] == "cancelled"
    assert builders == []
    manager.shutdown()
    sync_store.close()
    connection_store.close()


def test_committing_recovery_skips_connector_builder(tmp_path):
    clock = Clock()
    connection_store = ConnectionStore(str(tmp_path / "state.db"))
    sync_store = ConnectorSyncStore(str(tmp_path / "state.db"), clock=clock)
    connection = _connection(connection_store, tmp_path)
    job = sync_store.create(
        tenant_id="tenant",
        kb_id="kb",
        connection_id=connection["connection_id"],
        connector_type="local-directory",
    )
    _, token = sync_store.acquire(job["job_id"], lease_seconds=10)
    sync_store.prepare_commit(job["job_id"], token)
    clock.value += 11
    connector_builds = []

    class RecoverySink:
        def begin(self, **scope):
            self.scope = scope

        def recover_commit(self, *, heartbeat):
            heartbeat()

        def finalize(self):
            return None

    manager = SyncManager(
        connection_store,
        sync_store,
        ConnectorSyncRuntime(sync_store),
        sink_builder=lambda value: RecoverySink(),
        connector_builder=lambda value: connector_builds.append(value),
        clock=clock,
    )

    manager._run(job["job_id"])

    assert sync_store.get(job["job_id"])["status"] == "succeeded"
    assert connector_builds == []
    manager.shutdown()
    sync_store.close()
    connection_store.close()


def test_restart_cleans_terminal_work_after_connection_was_deleted(tmp_path):
    path = str(tmp_path / "state.db")
    connection_store = ConnectionStore(path)
    sync_store = ConnectorSyncStore(path, clock=lambda: 100.0)
    connection = _connection(connection_store, tmp_path)
    job = sync_store.create(
        tenant_id="tenant",
        kb_id="kb",
        connection_id=connection["connection_id"],
        connector_type="local-directory",
    )
    _, token = sync_store.acquire(job["job_id"], lease_seconds=10)
    sync_store.prepare_commit(job["job_id"], token)
    completed = sync_store.complete(
        job["job_id"],
        token,
        cursor=None,
        counters={
            "pages_processed": 0,
            "documents_seen": 0,
            "documents_fetched": 0,
            "deleted_seen": 0,
            "bytes_fetched": 0,
        },
    )
    assert completed["cleanup_pending"] is True
    marker = tmp_path / "terminal-cleanup-marker"
    marker.write_text("pending", encoding="utf-8")
    assert connection_store.delete(connection["connection_id"])
    sync_store.close()
    connection_store.close()

    reopened_connections = ConnectionStore(path)
    reopened_sync = ConnectorSyncStore(path, clock=lambda: 100.0)
    cleaned = []

    def cleanup(terminal):
        cleaned.append(terminal["job_id"])
        marker.unlink()

    manager = SyncManager(
        reopened_connections,
        reopened_sync,
        ConnectorSyncRuntime(reopened_sync),
        sink_builder=lambda connection: (_ for _ in ()).throw(
            AssertionError("deleted connection must not be required for cleanup")
        ),
        cleanup_callback=cleanup,
        clock=lambda: 100.0,
    )
    manager.recover()

    assert cleaned == [job["job_id"]]
    assert not marker.exists()
    assert reopened_sync.get(job["job_id"])["cleanup_pending"] is False
    manager.shutdown()
    reopened_sync.close()
    reopened_connections.close()


def test_maintenance_pages_cleanup_beyond_one_thousand(monkeypatch, tmp_path):
    connection_store = ConnectionStore(str(tmp_path / "state.db"))
    sync_store = ConnectorSyncStore(str(tmp_path / "state.db"))
    jobs = [
        {"job_id": f"job-{value}", "job_sequence": value} for value in range(1, 1002)
    ]
    cursors = []
    cleaned = []

    def pending(*, limit, after_sequence, tenant_id=None, kb_id=None):
        del tenant_id, kb_id
        cursors.append(after_sequence)
        return [job for job in jobs if job["job_sequence"] > after_sequence][:limit]

    monkeypatch.setattr(sync_store, "cleanup_pending", pending)
    monkeypatch.setattr(sync_store, "mark_cleanup_complete", lambda job_id: {})
    monkeypatch.setattr(sync_store, "prune_terminal_jobs", lambda **kwargs: 0)
    manager = SyncManager(
        connection_store,
        sync_store,
        ConnectorSyncRuntime(sync_store),
        sink_builder=lambda connection: None,
        cleanup_callback=lambda job: cleaned.append(job["job_id"]),
    )

    result = manager._run_maintenance()

    assert result == {"cleanup_attempted": 1001, "cleaned": 1001, "pruned": 0}
    assert cursors == [0, 1000]
    assert cleaned[-1] == "job-1001"
    manager.shutdown()
    sync_store.close()
    connection_store.close()


def test_recover_reconciles_terminal_projection_before_retention(monkeypatch, tmp_path):
    connection_store = ConnectionStore(str(tmp_path / "state.db"))
    sync_store = ConnectorSyncStore(str(tmp_path / "state.db"))
    runtime = ConnectorSyncRuntime(sync_store)
    manager = SyncManager(
        connection_store,
        sync_store,
        runtime,
        sink_builder=lambda connection: None,
    )
    order = []
    terminal = {"job_id": "terminal"}
    monkeypatch.setattr(sync_store, "latest_terminal_jobs", lambda: [terminal])
    monkeypatch.setattr(sync_store, "recoverable", lambda **kwargs: [])
    monkeypatch.setattr(connection_store, "enabled", lambda: [])
    monkeypatch.setattr(runtime, "reconcile", lambda job: order.append("reconcile"))
    monkeypatch.setattr(
        manager,
        "_run_maintenance",
        lambda: order.append("retention") or {},
    )

    assert manager.recover() == 0
    assert order == ["reconcile", "retention"]
    manager.shutdown()
    sync_store.close()
    connection_store.close()
