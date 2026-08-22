from __future__ import annotations

from contextlib import contextmanager

import pytest

from cogdoc.ha.postgres import PostgresBackend
from cogdoc.ha.storage import (
    StorageConfigurationError,
    StorageError,
    TransientStorageError,
    retry_transaction,
)


class FakeCursor:
    def __init__(self, row=None):
        self.row = row
        self.rowcount = 1

    def fetchone(self):
        return self.row

    def fetchall(self):
        return []


class FakeTransaction:
    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        self.connection.events.append("begin")

    def __exit__(self, exc_type, _exc, _tb):
        self.connection.events.append("rollback" if exc_type else "commit")


class FakeConnection:
    def __init__(self, lock_results=None, *, mapping_rows=False):
        self.events = []
        self.lock_results = list(lock_results or [])
        self.closed = False
        self.mapping_rows = mapping_rows

    def transaction(self):
        return FakeTransaction(self)

    def execute(self, sql, params=None):
        self.events.append((sql, params))
        if "pg_try_advisory_lock" in sql:
            value = self.lock_results.pop(0)
            return FakeCursor({"locked": value} if self.mapping_rows else (value,))
        if "pg_advisory_unlock" in sql:
            return FakeCursor({"unlocked": True} if self.mapping_rows else (True,))
        if sql == "SELECT pg_export_snapshot()":
            return FakeCursor(
                {"snapshot": "00000003-0000001B-1"}
                if self.mapping_rows
                else ("00000003-0000001B-1",)
            )
        if sql == "SELECT 1":
            return FakeCursor({"value": 1} if self.mapping_rows else (1,))
        return FakeCursor()

    def close(self):
        self.closed = True


class FakePool:
    def __init__(self, connection):
        self.value = connection
        self.closed = False

    @contextmanager
    def connection(self, timeout=None):
        assert timeout == 2.0
        yield self.value

    def close(self):
        self.closed = True


def _backend(connection, pools):
    def factory(**kwargs):
        assert kwargs["min_size"] == 1
        assert kwargs["max_size"] == 4
        pool = FakePool(connection)
        pools.append(pool)
        return pool

    return PostgresBackend(
        "postgresql://db/cogdoc",
        schema="cogdoc_test",
        max_size=4,
        pool_timeout_seconds=2,
        pool_factory=factory,
    )


def test_postgres_transaction_configures_bounded_read_and_reopens():
    connection = FakeConnection()
    pools = []
    backend = _backend(connection, pools)
    with backend.transaction() as active:
        assert active is connection
    sql = [event[0] for event in connection.events if isinstance(event, tuple)]
    assert "SET TRANSACTION READ ONLY" in sql
    assert 'SET LOCAL search_path TO "cogdoc_test"' in sql
    assert connection.events[1] == "begin"
    assert connection.events[-1] == "commit"
    backend.close()
    assert pools[0].closed
    with pytest.raises(StorageError, match="closed"):
        backend.check()
    backend.reopen()
    assert len(pools) == 2
    backend.close()


def test_postgres_advisory_lock_polls_and_releases():
    connection = FakeConnection([False, True], mapping_rows=True)
    backend = _backend(connection, [])
    ticks = iter([0.0, 0.0, 0.1, 0.1])
    sleeps = []
    with backend.advisory_lock(
        "migrate",
        timeout_seconds=1,
        monotonic=lambda: next(ticks),
        sleep=sleeps.append,
    ):
        pass
    assert sleeps == [0.05]
    assert any(
        isinstance(event, tuple) and "pg_advisory_unlock" in event[0]
        for event in connection.events
    )
    backend.close()


def test_postgres_check_supports_production_dict_rows():
    backend = _backend(FakeConnection(mapping_rows=True), [])
    assert backend.check()
    backend.close()


def test_postgres_exported_snapshot_holds_repeatable_read_transaction():
    connection = FakeConnection(mapping_rows=True)
    backend = _backend(connection, [])

    with backend.exported_snapshot(statement_timeout_seconds=600) as (
        active,
        snapshot_id,
    ):
        assert active is connection
        assert snapshot_id == "00000003-0000001B-1"
        assert connection.events[-1] == ("SELECT pg_export_snapshot()", None)

    sql = [event[0] for event in connection.events if isinstance(event, tuple)]
    assert "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY" in sql
    assert "SET LOCAL statement_timeout = 600000" in sql
    assert "SET LOCAL idle_in_transaction_session_timeout = 0" in sql
    assert connection.events[-1] == "commit"
    with pytest.raises(ValueError, match="snapshot statement timeout"):
        with backend.exported_snapshot(statement_timeout_seconds=0):
            pass
    backend.close()


class RetryBackend:
    kind = "postgres"

    def __init__(self):
        self.calls = 0

    @contextmanager
    def transaction(self, *, write=False):
        self.calls += 1
        if self.calls < 3:
            raise TransientStorageError("retry")
        yield object()

    def check(self):
        return True

    def close(self):
        pass

    def reopen(self):
        pass

    def sql(self, *, sqlite, postgres):
        return postgres


def test_retry_transaction_restarts_complete_unit():
    backend = RetryBackend()
    sleeps = []
    assert (
        retry_transaction(
            backend,
            lambda _connection: "done",
            write=True,
            attempts=3,
            sleep=sleeps.append,
        )
        == "done"
    )
    assert sleeps == [0.01, 0.05]


@pytest.mark.parametrize("schema", ["", "Mixed", "bad-name", "a" * 64])
def test_postgres_rejects_unsafe_schema(schema):
    with pytest.raises(StorageConfigurationError, match="schema"):
        PostgresBackend(
            "postgresql://db/cogdoc", schema=schema, pool_factory=lambda **_: None
        )
