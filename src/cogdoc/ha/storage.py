from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from threading import RLock
from typing import Any, Literal, Protocol, runtime_checkable


BackendKind = Literal["sqlite", "postgres"]
Parameters = Sequence[Any] | Mapping[str, Any] | None


class StorageError(RuntimeError):
    """A durable control-plane storage operation failed."""


class StorageConfigurationError(StorageError, ValueError):
    """The selected durable backend cannot be configured safely."""


class TransientStorageError(StorageError):
    """A transaction may be retried from its beginning."""


@runtime_checkable
class DatabaseCursor(Protocol):
    rowcount: int

    def execute(self, query: str, params: Parameters = None) -> DatabaseCursor: ...

    def executemany(
        self, query: str, params: Sequence[Sequence[Any]]
    ) -> DatabaseCursor: ...

    def fetchone(self) -> Any | None: ...

    def fetchall(self) -> list[Any]: ...


@runtime_checkable
class DatabaseConnection(Protocol):
    def execute(self, query: str, params: Parameters = None) -> DatabaseCursor: ...

    def executemany(
        self, query: str, params: Sequence[Sequence[Any]]
    ) -> DatabaseCursor: ...


@runtime_checkable
class DatabaseBackend(Protocol):
    """Minimal transaction seam used by every distributed subsystem."""

    kind: BackendKind

    @contextmanager
    def transaction(self, *, write: bool = False) -> Iterator[DatabaseConnection]: ...

    def check(self) -> bool: ...

    def close(self) -> None: ...

    def reopen(self) -> None: ...

    def sql(self, *, sqlite: str, postgres: str) -> str: ...


class SQLiteBackend:
    """Serialized DB-API backend for local and deterministic test operation.

    One connection is protected by a re-entrant lock. Transactions are never
    exposed without that lock, so callers cannot accidentally interleave a
    read/CAS/write sequence with another thread in the same process. SQLite's
    ``BEGIN IMMEDIATE`` provides the matching cross-process writer fence.
    """

    kind: BackendKind = "sqlite"

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        if self.path.name in {"", ".", ".."}:
            raise StorageConfigurationError("SQLite path must name a database file")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._closed = True
        self._conn = self._connect()
        self._closed = False

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            check_same_thread=False,
            isolation_level=None,
            timeout=5.0,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    @contextmanager
    def transaction(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        with self._lock:
            if self._closed:
                raise StorageError("database backend is closed")
            if self._conn.in_transaction:
                raise StorageError("nested backend transactions are not supported")
            self._conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            try:
                yield self._conn
            except BaseException:
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def check(self) -> bool:
        with self.transaction() as connection:
            row = connection.execute("SELECT 1").fetchone()
            return row is not None and int(row[0]) == 1

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._conn.close()
            self._closed = True

    def reopen(self) -> None:
        with self._lock:
            if not self._closed:
                return
            self._conn = self._connect()
            self._closed = False

    def sql(self, *, sqlite: str, postgres: str) -> str:
        del postgres
        return sqlite


def execute_script(backend: DatabaseBackend, statements: Sequence[str]) -> None:
    """Execute explicit DDL statements atomically on either backend.

    ``executescript`` is deliberately avoided: SQLite's implementation may
    commit implicitly, which would violate the migration transaction contract.
    """

    with backend.transaction(write=True) as connection:
        for statement in statements:
            clean = statement.strip()
            if clean:
                connection.execute(clean)


def retry_transaction(
    backend: DatabaseBackend,
    operation: Callable[[DatabaseConnection], Any],
    *,
    write: bool = False,
    attempts: int = 3,
    sleep: Callable[[float], None] | None = None,
    backoff_seconds: Sequence[float] = (0.01, 0.05),
) -> Any:
    """Retry a whole transaction only for an explicitly transient failure."""

    if type(attempts) is not int or attempts < 1 or attempts > 10:
        raise ValueError("attempts must be between 1 and 10")
    if not backoff_seconds or any(delay < 0 for delay in backoff_seconds):
        raise ValueError("backoff_seconds must contain non-negative delays")
    if sleep is None:
        import time

        sleep = time.sleep
    for attempt in range(attempts):
        try:
            with backend.transaction(write=write) as connection:
                return operation(connection)
        except TransientStorageError:
            if attempt + 1 >= attempts:
                raise
            sleep(float(backoff_seconds[min(attempt, len(backoff_seconds) - 1)]))
    raise AssertionError("transaction retry loop did not return")


__all__ = [
    "BackendKind",
    "DatabaseBackend",
    "DatabaseConnection",
    "DatabaseCursor",
    "Parameters",
    "SQLiteBackend",
    "StorageConfigurationError",
    "StorageError",
    "TransientStorageError",
    "execute_script",
    "retry_transaction",
]
