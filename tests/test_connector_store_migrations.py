from concurrent.futures import ThreadPoolExecutor
import sqlite3
from threading import Barrier

from cogdoc.connectors.connection_store import ConnectionStore
from cogdoc.connectors.sync_store import ConnectorSyncStore


def _open_together(factory, count=8):
    barrier = Barrier(count)

    def open_store():
        barrier.wait()
        return factory()

    with ThreadPoolExecutor(max_workers=count) as executor:
        futures = [executor.submit(open_store) for _ in range(count)]
        return [future.result() for future in futures]


def _columns(db_path, table):
    with sqlite3.connect(db_path) as connection:
        return {
            str(row[1])
            for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        }


def test_connection_store_additive_migration_is_concurrent_startup_safe(tmp_path):
    db_path = str(tmp_path / "connections.db")
    ConnectionStore(db_path).close()
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "ALTER TABLE connector_connections DROP COLUMN credential_fields_json"
        )
        connection.execute(
            "ALTER TABLE connector_connections DROP COLUMN credential_id"
        )
        connection.execute("ALTER TABLE connector_connections DROP COLUMN deleting")
        connection.execute(
            "ALTER TABLE connector_connections DROP COLUMN delete_index_job_id"
        )

    stores = _open_together(lambda: ConnectionStore(db_path))
    try:
        assert {
            "credential_id",
            "credential_fields_json",
            "deleting",
            "delete_index_job_id",
        } <= _columns(db_path, "connector_connections")
    finally:
        for store in stores:
            store.close()


def test_sync_store_additive_migration_is_concurrent_startup_safe(tmp_path):
    db_path = str(tmp_path / "sync.db")
    initial = ConnectorSyncStore(db_path, clock=lambda: 100.0)
    older = initial.create(
        tenant_id="tenant",
        kb_id="docs",
        connection_id="conn-1",
        connector_type="local-directory",
    )
    newer = initial.create(
        tenant_id="tenant",
        kb_id="docs",
        connection_id="conn-1",
        connector_type="local-directory",
    )
    initial.close()
    with sqlite3.connect(db_path) as connection:
        connection.execute("DROP INDEX idx_connector_sync_jobs_scope_sequence")
        connection.execute("DROP INDEX idx_connector_sync_jobs_sequence")
        connection.execute("DROP INDEX idx_connector_sync_jobs_replay_of")
        connection.execute("DROP INDEX idx_connector_sync_jobs_terminal_cleanup")
        connection.execute("DROP TABLE connector_sync_job_sequence")
        connection.execute("ALTER TABLE connector_sync_jobs DROP COLUMN job_sequence")
        connection.execute(
            "ALTER TABLE connector_sync_jobs DROP COLUMN connection_revision"
        )
        connection.execute(
            "ALTER TABLE connector_sync_jobs DROP COLUMN health_failure_recorded"
        )
        connection.execute(
            "ALTER TABLE connector_sync_jobs DROP COLUMN health_duration_seconds"
        )
        connection.execute("ALTER TABLE connector_sync_jobs DROP COLUMN credential_id")
        connection.execute(
            "ALTER TABLE connector_sync_jobs DROP COLUMN credential_revision"
        )
        connection.execute(
            "ALTER TABLE connector_sync_jobs DROP COLUMN cleanup_pending"
        )
        connection.execute(
            "ALTER TABLE connector_sync_jobs DROP COLUMN attempt_started_at"
        )
        connection.execute("ALTER TABLE connector_sync_jobs DROP COLUMN replay_of")
        connection.execute("ALTER TABLE connector_sync_jobs DROP COLUMN start_cursor")
        connection.execute(
            "ALTER TABLE connector_sync_health DROP COLUMN last_success_sequence"
        )
        connection.execute(
            "ALTER TABLE connector_sync_health DROP COLUMN last_job_sequence"
        )

    stores = _open_together(lambda: ConnectorSyncStore(db_path))
    try:
        assert {
            "start_cursor",
            "replay_of",
            "job_sequence",
            "connection_revision",
            "health_duration_seconds",
            "health_failure_recorded",
            "credential_id",
            "credential_revision",
            "cleanup_pending",
            "attempt_started_at",
        } <= _columns(db_path, "connector_sync_jobs")
        assert {"last_job_sequence", "last_success_sequence"} <= _columns(
            db_path, "connector_sync_health"
        )
        assert stores[0].get(older["job_id"])["job_sequence"] == 1
        assert stores[0].get(newer["job_id"])["job_sequence"] == 2
        assert stores[0].get(older["job_id"])["connection_revision"] == 0
        assert stores[0].get(older["job_id"])["credential_revision"] == 0
        health = stores[0].health_snapshot("tenant", "docs", "conn-1")
        assert health["last_job_id"] == newer["job_id"]
        with sqlite3.connect(db_path) as connection:
            watermarks = connection.execute(
                "SELECT last_job_sequence,last_success_sequence "
                "FROM connector_sync_health WHERE tenant_id=? AND kb_id=? "
                "AND connection_id=?",
                ("tenant", "docs", "conn-1"),
            ).fetchone()
        assert watermarks == (2, 0)
    finally:
        for store in stores:
            store.close()


def test_sync_store_reopen_does_not_rewrite_valid_null_start_cursor(tmp_path):
    db_path = str(tmp_path / "sync.db")
    store = ConnectorSyncStore(db_path)
    job = store.create(
        tenant_id="tenant",
        kb_id="docs",
        connection_id="conn-1",
        connector_type="local-directory",
    )
    _, token = store.acquire(job["job_id"], lease_seconds=60)
    store.checkpoint(
        job["job_id"],
        token,
        cursor="provider-page-2",
        counters={
            "pages_processed": 1,
            "documents_seen": 1,
            "documents_fetched": 1,
            "deleted_seen": 0,
            "bytes_fetched": 7,
        },
        lease_seconds=60,
    )
    assert store.get(job["job_id"])["start_cursor"] is None
    store.close()

    reopened = ConnectorSyncStore(db_path)
    try:
        persisted = reopened.get(job["job_id"])
        assert persisted["start_cursor"] is None
        assert persisted["cursor"] == "provider-page-2"
    finally:
        reopened.close()


def test_sync_store_migrates_legacy_job_revision_fail_closed(tmp_path):
    db_path = str(tmp_path / "sync.db")
    store = ConnectorSyncStore(db_path)
    job = store.create(
        tenant_id="tenant",
        kb_id="docs",
        connection_id="conn-1",
        connector_type="local-directory",
        connection_revision=7,
    )
    store.close()
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "ALTER TABLE connector_sync_jobs DROP COLUMN connection_revision"
        )

    reopened = ConnectorSyncStore(db_path)
    try:
        persisted = reopened.get(job["job_id"])
        assert persisted["job_sequence"] == job["job_sequence"]
        assert persisted["connection_revision"] == 0
    finally:
        reopened.close()
