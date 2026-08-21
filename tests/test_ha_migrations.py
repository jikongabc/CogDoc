from __future__ import annotations

import hashlib
import threading
import time

import pytest

from cogdoc.ha.migrations import (
    MIGRATION_CONTRACTED,
    MIGRATION_EXPANDED,
    MIGRATION_VALIDATED,
    Migration,
    MigrationConflict,
    MigrationRunner,
)
from cogdoc.ha.migration_catalog import REGISTERED_MIGRATIONS
from cogdoc.ha.runtime import HAConfig, HARuntime
from cogdoc.ha.storage import SQLiteBackend


def _checksum(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _count(backend, query):
    with backend.transaction() as connection:
        return connection.execute(query).fetchone()[0]


def test_expand_backfill_validate_and_explicit_contract(tmp_path):
    backend = SQLiteBackend(tmp_path / "migration.db")
    with backend.transaction(write=True) as connection:
        connection.execute("CREATE TABLE docs(id INTEGER PRIMARY KEY,value TEXT)")
        connection.executemany(
            "INSERT INTO docs(id,value) VALUES(?,?)", [(1, "a"), (2, "b"), (3, "c")]
        )

    def backfill(database, cursor, limit):
        after = int(cursor or 0)
        with database.transaction(write=True) as connection:
            rows = connection.execute(
                "SELECT id FROM docs WHERE id>? ORDER BY id LIMIT ?", (after, limit)
            ).fetchall()
            for row in rows:
                connection.execute(
                    "UPDATE docs SET normalized=upper(value) WHERE id=?", (row[0],)
                )
        return None if len(rows) < limit else str(rows[-1][0])

    migration = Migration(
        1,
        "normalized docs",
        _checksum("normalized docs v1"),
        expand=("ALTER TABLE docs ADD COLUMN normalized TEXT",),
        backfill=backfill,
        validate=lambda database: (
            _count(database, "SELECT COUNT(*) FROM docs WHERE normalized IS NULL") == 0
        ),
        contract=("CREATE INDEX docs_normalized ON docs(normalized)",),
    )
    runner = MigrationRunner(backend, [migration], owner_id="one")
    result = runner.run(batch_size=2)
    assert result[0]["phase"] == MIGRATION_VALIDATED
    assert (
        _count(backend, "SELECT COUNT(*) FROM docs WHERE normalized IN ('A','B','C')")
        == 3
    )
    result = runner.run(allow_contract=True, minimum_compatible_version=1)
    assert result[0]["phase"] == MIGRATION_CONTRACTED
    assert (
        _count(
            backend, "SELECT COUNT(*) FROM sqlite_master WHERE name='docs_normalized'"
        )
        == 1
    )
    backend.close()


def test_backfill_crash_replays_cursor_idempotently(tmp_path):
    backend = SQLiteBackend(tmp_path / "migration.db")
    calls = []

    def backfill(database, cursor, _limit):
        calls.append(cursor)
        if len(calls) == 1:
            raise RuntimeError("crash")
        with database.transaction(write=True) as connection:
            connection.execute("INSERT OR IGNORE INTO target(id) VALUES(1)")
        return None

    migration = Migration(
        1,
        "resumable",
        _checksum("resumable"),
        expand=("CREATE TABLE target(id INTEGER PRIMARY KEY)",),
        backfill=backfill,
    )
    runner = MigrationRunner(backend, [migration], owner_id="one")
    with pytest.raises(RuntimeError, match="crash"):
        runner.run()
    assert runner.get(1)["phase"] == MIGRATION_EXPANDED
    assert runner.run()[0]["phase"] == MIGRATION_VALIDATED
    assert calls == [None, None]
    assert _count(backend, "SELECT COUNT(*) FROM target") == 1
    backend.close()


def test_checksum_drift_and_unsafe_contract_are_rejected(tmp_path):
    backend = SQLiteBackend(tmp_path / "migration.db")
    first = Migration(1, "stable", _checksum("one"))
    MigrationRunner(backend, [first], owner_id="one").run()
    changed = Migration(1, "stable", _checksum("two"))
    with pytest.raises(MigrationConflict, match="checksum"):
        MigrationRunner(backend, [changed], owner_id="two").run()
    contract = Migration(
        2,
        "contract",
        _checksum("contract"),
        contract=("CREATE TABLE contracted(id INTEGER)",),
    )
    runner = MigrationRunner(backend, [contract], owner_id="one")
    assert runner.run()[0]["phase"] == MIGRATION_VALIDATED
    with pytest.raises(MigrationConflict, match="oldest live"):
        runner.run(allow_contract=True, minimum_compatible_version=1)
    backend.close()


def test_cross_instance_migration_lock_is_exclusive(tmp_path):
    path = tmp_path / "migration.db"
    backend_a = SQLiteBackend(path)
    backend_b = SQLiteBackend(path)
    runner_a = MigrationRunner(backend_a, [], owner_id="a")
    runner_b = MigrationRunner(backend_b, [], owner_id="b")
    entered = threading.Event()
    release = threading.Event()

    def hold():
        with runner_a.lock():
            entered.set()
            release.wait(5)

    thread = threading.Thread(target=hold)
    thread.start()
    assert entered.wait(2)
    with pytest.raises(MigrationConflict, match="already running"):
        with runner_b.lock():
            pass
    release.set()
    thread.join()
    backend_a.close()
    backend_b.close()


def test_long_backfill_heartbeats_and_lost_runner_cannot_advance(tmp_path):
    class Clock:
        value = 1000.0

        def __call__(self):
            return self.value

    path = tmp_path / "migration.db"
    clock = Clock()
    backend_a = SQLiteBackend(path)
    backend_b = SQLiteBackend(path)
    callback_entered = threading.Event()
    callback_release = threading.Event()

    def backfill(_database, _cursor, _limit):
        callback_entered.set()
        callback_release.wait(2)
        return None

    migration = Migration(
        1,
        "fenced backfill",
        _checksum("fenced backfill"),
        expand=("CREATE TABLE fenced(id INTEGER PRIMARY KEY)",),
        backfill=backfill,
    )
    runner_a = MigrationRunner(
        backend_a,
        [migration],
        owner_id="a",
        clock=clock,
        lock_seconds=5,
        heartbeat_interval_seconds=0.01,
    )
    errors = []

    def run_first():
        try:
            runner_a.run()
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=run_first)
    thread.start()
    assert callback_entered.wait(2)
    clock.value += 6
    time.sleep(0.05)
    runner_b = MigrationRunner(
        backend_b,
        [],
        owner_id="b",
        clock=clock,
        lock_seconds=5,
    )
    with runner_b.lock():
        callback_release.set()
        thread.join(2)
    assert not thread.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], MigrationConflict)
    assert runner_a.get(1)["phase"] == MIGRATION_EXPANDED
    backend_a.close()
    backend_b.close()


def test_registered_catalog_validates_runtime_baseline(tmp_path):
    config = HAConfig(
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
        worker_id="migration-test",
        scheduler_enabled=False,
        outbox_enabled=False,
    )
    runtime = HARuntime(config)
    rows = MigrationRunner(
        runtime.backend, REGISTERED_MIGRATIONS, owner_id="migration"
    ).run()
    assert [(row["version"], row["phase"]) for row in rows] == [(1, "validated")]
    runtime.shutdown()
