from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
import sqlite3
from typing import Any

from cogdoc.ha.storage import DatabaseBackend, DatabaseConnection


class CompatRow:
    """Sequence-style row with optional named access across DB backends."""

    __slots__ = ("_names", "_values")

    def __init__(self, row: Any, names: Sequence[str] = ()) -> None:
        if isinstance(row, Mapping):
            self._names = tuple(str(name) for name in row)
            self._values = tuple(row[name] for name in row)
        else:
            self._names = tuple(names)
            self._values = tuple(row)

    def __getitem__(self, key: int | slice | str) -> Any:
        if isinstance(key, str):
            try:
                key = self._names.index(key)
            except ValueError as exc:
                raise KeyError(key) from exc
        return self._values[key]

    def __iter__(self) -> Iterator[Any]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def keys(self) -> tuple[str, ...]:
        return self._names


class CompatCursor:
    __slots__ = ("_cursor", "_rows", "_offset", "rowcount")

    def __init__(
        self,
        cursor: Any | None = None,
        *,
        rows: Sequence[CompatRow] | None = None,
        rowcount: int = 0,
    ) -> None:
        self._cursor = cursor
        self._rows = None if rows is None else tuple(rows)
        self._offset = 0
        self.rowcount = int(getattr(cursor, "rowcount", rowcount))

    @staticmethod
    def _names(cursor: Any) -> tuple[str, ...]:
        description = getattr(cursor, "description", None) or ()
        return tuple(str(column[0]) for column in description)

    def fetchone(self) -> CompatRow | None:
        if self._rows is not None:
            if self._offset >= len(self._rows):
                return None
            row = self._rows[self._offset]
            self._offset += 1
            return row
        cursor = self._cursor
        assert cursor is not None
        row = cursor.fetchone()
        return None if row is None else CompatRow(row, self._names(cursor))

    def fetchall(self) -> list[CompatRow]:
        if self._rows is not None:
            rows = list(self._rows[self._offset :])
            self._offset = len(self._rows)
            return rows
        cursor = self._cursor
        assert cursor is not None
        return [CompatRow(row, self._names(cursor)) for row in cursor.fetchall()]

    def __iter__(self) -> Iterator[CompatRow]:
        while (row := self.fetchone()) is not None:
            yield row


class BackendDBAPIConnection:
    """Small DB-API facade for stores shared by SQLite and PostgreSQL.

    The connector stores already delimit every multi-statement authority change
    with ``BEGIN IMMEDIATE``/``COMMIT``.  This facade maps that contract onto a
    backend transaction and buffers one-statement reads before returning the
    pooled PostgreSQL connection.
    """

    def __init__(self, backend: DatabaseBackend) -> None:
        self.backend = backend
        self._transaction: Any | None = None
        self._connection: DatabaseConnection | None = None

    @property
    def in_transaction(self) -> bool:
        return self._connection is not None

    def _sql(self, statement: str) -> str:
        if self.backend.kind == "sqlite":
            return statement
        statement = statement.replace(" IS ?", " IS NOT DISTINCT FROM ?")
        return (
            statement.replace(" BLOB", " BYTEA")
            .replace(" REAL", " DOUBLE PRECISION")
            .replace(
                "MAX(last_value,excluded.last_value)",
                "GREATEST(last_value,excluded.last_value)",
            )
            .replace(
                "MIN(expires_at,created_at+?)",
                "LEAST(expires_at,created_at+?)",
            )
            .replace("?", "%s")
            .replace(" IS NOT excluded.", " IS DISTINCT FROM excluded.")
        )

    @staticmethod
    def _write(statement: str) -> bool:
        command = statement.lstrip().split(None, 1)[0].upper()
        return command not in {"SELECT", "WITH", "EXPLAIN"}

    @staticmethod
    def _buffer(cursor: Any) -> CompatCursor:
        description = getattr(cursor, "description", None)
        rows: list[CompatRow] = []
        if description:
            names = tuple(str(column[0]) for column in description)
            rows = [CompatRow(row, names) for row in cursor.fetchall()]
        return CompatCursor(rows=rows, rowcount=int(getattr(cursor, "rowcount", 0)))

    def execute(self, statement: str, parameters: Sequence[Any] = ()) -> CompatCursor:
        command = statement.strip().upper()
        if command in {"BEGIN", "BEGIN IMMEDIATE"}:
            if self.in_transaction:
                raise RuntimeError("shared backend transaction is already active")
            self._transaction = self.backend.transaction(
                write=command == "BEGIN IMMEDIATE"
            )
            self._connection = self._transaction.__enter__()
            return CompatCursor()
        if command in {"COMMIT", "ROLLBACK"}:
            if not self.in_transaction:
                return CompatCursor()
            transaction = self._transaction
            assert transaction is not None
            self._transaction = None
            self._connection = None
            if command == "COMMIT":
                transaction.__exit__(None, None, None)
            else:
                error = RuntimeError("connector transaction rolled back")
                transaction.__exit__(RuntimeError, error, None)
            return CompatCursor()
        sql = self._sql(statement)
        try:
            if self._connection is not None:
                return CompatCursor(self._connection.execute(sql, tuple(parameters)))
            with self.backend.transaction(write=self._write(statement)) as connection:
                return self._buffer(connection.execute(sql, tuple(parameters)))
        except BaseException as exc:
            sqlstate = getattr(exc, "sqlstate", None)
            if sqlstate is None:
                sqlstate = getattr(getattr(exc, "__cause__", None), "sqlstate", None)
            if self.backend.kind == "postgres" and str(sqlstate).startswith("23"):
                raise sqlite3.IntegrityError(str(exc)) from exc
            raise

    def executescript(self, script: str) -> None:
        statements = [statement.strip() for statement in script.split(";")]
        with self.backend.transaction(write=True) as connection:
            for statement in statements:
                if statement:
                    connection.execute(self._sql(statement))

    def close(self) -> None:
        # The HA runtime owns the shared pool/backend lifecycle.
        if self.in_transaction:
            error = RuntimeError("connector store closed with an active transaction")
            transaction = self._transaction
            assert transaction is not None
            self._transaction = None
            self._connection = None
            transaction.__exit__(RuntimeError, error, None)

    def commit(self) -> None:
        self.execute("COMMIT")

    def rollback(self) -> None:
        self.execute("ROLLBACK")


__all__ = ["BackendDBAPIConnection", "CompatCursor", "CompatRow"]
