from __future__ import annotations

import hashlib
import math
import re
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from typing import Any

from cogdoc.ha.storage import (
    BackendKind,
    DatabaseConnection,
    StorageConfigurationError,
    StorageError,
    TransientStorageError,
)


_SCHEMA = re.compile(r"[a-z_][a-z0-9_]{0,62}\Z")
_RETRYABLE_SQLSTATES = frozenset({"40001", "40P01", "55P03", "57014"})


def _default_pool_factory(**kwargs: Any) -> Any:
    try:
        from psycopg_pool import ConnectionPool  # type: ignore[import-not-found]
        from psycopg.rows import dict_row  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - exercised without HA extra
        raise StorageConfigurationError(
            "PostgreSQL backend requires the 'cogdoc[ha]' extra"
        ) from exc
    connection_kwargs = dict(kwargs.pop("kwargs", {}))
    connection_kwargs["row_factory"] = dict_row
    return ConnectionPool(**kwargs, kwargs=connection_kwargs)


def _sqlstate(exc: BaseException) -> str | None:
    value = getattr(exc, "sqlstate", None)
    if isinstance(value, str):
        return value
    diagnostic = getattr(exc, "diag", None)
    value = getattr(diagnostic, "sqlstate", None)
    return value if isinstance(value, str) else None


def _first(row: Any | None) -> Any:
    if row is None:
        return None
    if isinstance(row, Mapping):
        return next(iter(row.values()), None)
    return row[0]


def _lock_key(namespace: str, name: str) -> int:
    digest = hashlib.blake2b(
        f"cogdoc-ha-v1\0{namespace}\0{name}".encode(), digest_size=8
    ).digest()
    return int.from_bytes(digest, "big", signed=True)


class PostgresBackend:
    """Pooled PostgreSQL backend with bounded transactions and advisory locks."""

    kind: BackendKind = "postgres"

    def __init__(
        self,
        dsn: str,
        *,
        schema: str = "cogdoc",
        min_size: int = 1,
        max_size: int = 10,
        pool_timeout_seconds: float = 10.0,
        statement_timeout_seconds: float = 30.0,
        lock_timeout_seconds: float = 5.0,
        pool_factory: Callable[..., Any] | None = None,
    ) -> None:
        if not isinstance(dsn, str) or not dsn.strip():
            raise StorageConfigurationError("PostgreSQL DSN is required")
        if not _SCHEMA.fullmatch(schema):
            raise StorageConfigurationError("PostgreSQL schema name is invalid")
        if type(min_size) is not int or type(max_size) is not int:
            raise StorageConfigurationError("PostgreSQL pool sizes must be integers")
        if min_size < 0 or max_size < 1 or min_size > max_size or max_size > 100:
            raise StorageConfigurationError("PostgreSQL pool sizes are invalid")
        for value, name in (
            (pool_timeout_seconds, "pool timeout"),
            (statement_timeout_seconds, "statement timeout"),
            (lock_timeout_seconds, "lock timeout"),
        ):
            if (
                not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
            ):
                raise StorageConfigurationError(f"PostgreSQL {name} must be positive")
        self.dsn = dsn.strip()
        self.schema = schema
        self._min_size = min_size
        self._max_size = max_size
        self._pool_timeout = float(pool_timeout_seconds)
        self._statement_timeout_ms = max(1, int(statement_timeout_seconds * 1000))
        self._lock_timeout_ms = max(1, int(lock_timeout_seconds * 1000))
        self._pool_factory = pool_factory or _default_pool_factory
        self._closed = True
        self._pool = self._open_pool()
        self._closed = False

    def _open_pool(self) -> Any:
        pool = self._pool_factory(
            conninfo=self.dsn,
            min_size=self._min_size,
            max_size=self._max_size,
            timeout=self._pool_timeout,
            open=True,
            kwargs={"autocommit": True},
        )
        try:
            with pool.connection(timeout=self._pool_timeout) as connection:
                connection.execute(f'CREATE SCHEMA IF NOT EXISTS "{self.schema}"')
        except BaseException:
            pool.close()
            raise
        return pool

    @contextmanager
    def transaction(self, *, write: bool = False) -> Iterator[DatabaseConnection]:
        if self._closed:
            raise StorageError("database backend is closed")
        try:
            with self._pool.connection(timeout=self._pool_timeout) as connection:
                with connection.transaction():
                    connection.execute(
                        f"SET LOCAL statement_timeout = {self._statement_timeout_ms}"
                    )
                    connection.execute(
                        f"SET LOCAL lock_timeout = {self._lock_timeout_ms}"
                    )
                    if not write:
                        connection.execute("SET TRANSACTION READ ONLY")
                    connection.execute(f'SET LOCAL search_path TO "{self.schema}"')
                    yield connection
        except BaseException as exc:
            if _sqlstate(exc) in _RETRYABLE_SQLSTATES:
                raise TransientStorageError(
                    "PostgreSQL transaction must be retried"
                ) from exc
            raise

    @contextmanager
    def advisory_lock(
        self,
        name: str,
        *,
        timeout_seconds: float = 10.0,
        poll_seconds: float = 0.05,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> Iterator[None]:
        if not isinstance(name, str) or not name or len(name) > 512:
            raise ValueError("advisory lock name is invalid")
        if timeout_seconds <= 0 or poll_seconds <= 0:
            raise ValueError("advisory lock timings must be positive")
        key = _lock_key(self.schema, name)
        deadline = monotonic() + timeout_seconds
        acquired = False
        with self._pool.connection(timeout=self._pool_timeout) as connection:
            try:
                while monotonic() < deadline:
                    row = connection.execute(
                        "SELECT pg_try_advisory_lock(%s)", (key,)
                    ).fetchone()
                    acquired = bool(_first(row))
                    if acquired:
                        break
                    sleep(min(poll_seconds, max(0.0, deadline - monotonic())))
                if not acquired:
                    raise TransientStorageError("PostgreSQL advisory lock timed out")
                yield
            finally:
                if acquired:
                    row = connection.execute(
                        "SELECT pg_advisory_unlock(%s)", (key,)
                    ).fetchone()
                    if _first(row) is not True:
                        connection.close()

    def check(self) -> bool:
        with self.transaction() as connection:
            row = connection.execute("SELECT 1").fetchone()
            return row is not None and int(_first(row)) == 1

    def close(self) -> None:
        if self._closed:
            return
        self._pool.close()
        self._closed = True

    def reopen(self) -> None:
        if not self._closed:
            return
        self._pool = self._open_pool()
        self._closed = False

    def sql(self, *, sqlite: str, postgres: str) -> str:
        del sqlite
        return postgres


__all__ = ["PostgresBackend"]
