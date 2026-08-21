from __future__ import annotations

import sqlite3
import threading

import pytest

from cogdoc.ha.storage import SQLiteBackend, StorageError, execute_script


def test_sqlite_backend_commits_rolls_back_and_reopens(tmp_path):
    backend = SQLiteBackend(tmp_path / "ha.db")
    execute_script(
        backend,
        ["CREATE TABLE records(id TEXT PRIMARY KEY, value INTEGER NOT NULL)"],
    )
    with backend.transaction(write=True) as connection:
        connection.execute("INSERT INTO records VALUES(?,?)", ("a", 1))
    with pytest.raises(RuntimeError):
        with backend.transaction(write=True) as connection:
            connection.execute("INSERT INTO records VALUES(?,?)", ("b", 2))
            raise RuntimeError("rollback")
    with backend.transaction() as connection:
        assert [tuple(row) for row in connection.execute("SELECT * FROM records")] == [
            ("a", 1)
        ]
    backend.close()
    with pytest.raises(StorageError, match="closed"):
        backend.check()
    backend.reopen()
    assert backend.check()
    backend.close()


def test_sqlite_backend_serializes_cross_thread_cas(tmp_path):
    backend = SQLiteBackend(tmp_path / "ha.db")
    execute_script(
        backend,
        ["CREATE TABLE counter(id INTEGER PRIMARY KEY, value INTEGER NOT NULL)"],
    )
    with backend.transaction(write=True) as connection:
        connection.execute("INSERT INTO counter VALUES(1,0)")

    barrier = threading.Barrier(8)

    def increment() -> None:
        barrier.wait()
        with backend.transaction(write=True) as connection:
            value = int(
                connection.execute("SELECT value FROM counter WHERE id=1").fetchone()[0]
            )
            connection.execute("UPDATE counter SET value=? WHERE id=1", (value + 1,))

    threads = [threading.Thread(target=increment) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    with backend.transaction() as connection:
        assert connection.execute("SELECT value FROM counter").fetchone()[0] == 8
    backend.close()


def test_sqlite_backend_rejects_nested_transactions(tmp_path):
    backend = SQLiteBackend(tmp_path / "ha.db")
    try:
        with backend.transaction():
            with pytest.raises(StorageError, match="nested"):
                with backend.transaction():
                    pass
    finally:
        backend.close()


def test_sqlite_backend_enforces_foreign_keys(tmp_path):
    backend = SQLiteBackend(tmp_path / "ha.db")
    execute_script(
        backend,
        [
            "CREATE TABLE parent(id TEXT PRIMARY KEY)",
            "CREATE TABLE child(id TEXT PRIMARY KEY,parent_id TEXT NOT NULL REFERENCES parent(id))",
        ],
    )
    try:
        with pytest.raises(sqlite3.IntegrityError):
            with backend.transaction(write=True) as connection:
                connection.execute("INSERT INTO child VALUES(?,?)", ("c", "missing"))
    finally:
        backend.close()
