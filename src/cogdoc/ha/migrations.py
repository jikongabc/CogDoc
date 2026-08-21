from __future__ import annotations

import contextlib
import math
import secrets
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from typing import Any, Final

from cogdoc.ha.storage import DatabaseBackend, execute_script


MIGRATION_REGISTERED: Final = "registered"
MIGRATION_EXPANDED: Final = "expanded"
MIGRATION_BACKFILLED: Final = "backfilled"
MIGRATION_VALIDATED: Final = "validated"
MIGRATION_CONTRACTED: Final = "contracted"


class MigrationError(RuntimeError):
    pass


class MigrationConflict(MigrationError):
    pass


Backfill = Callable[[DatabaseBackend, str | None, int], str | None]
Validation = Callable[[DatabaseBackend], bool]


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    checksum: str
    expand: tuple[str, ...] = ()
    backfill: Backfill | None = None
    validate: Validation | None = None
    contract: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.version) is not int or not 1 <= self.version <= 2_147_483_647:
            raise ValueError("migration version is invalid")
        if (
            not self.name
            or self.name != self.name.strip()
            or len(self.name) > 200
            or any(ord(char) < 32 or ord(char) == 127 for char in self.name)
        ):
            raise ValueError("migration name is invalid")
        if len(self.checksum) != 64 or any(
            char not in "0123456789abcdef" for char in self.checksum
        ):
            raise ValueError("migration checksum must be lowercase SHA-256")
        if any(not statement.strip() for statement in (*self.expand, *self.contract)):
            raise ValueError("migration statements cannot be empty")


class MigrationRunner:
    """Crash-resumable expand/backfill/validate/contract migration runner."""

    def __init__(
        self,
        backend: DatabaseBackend,
        migrations: Sequence[Migration],
        *,
        owner_id: str,
        clock: Callable[[], float] = time.time,
        lock_seconds: float = 60,
        heartbeat_interval_seconds: float | None = None,
    ) -> None:
        self.backend = backend
        self.migrations = tuple(sorted(migrations, key=lambda item: item.version))
        if len({item.version for item in self.migrations}) != len(self.migrations):
            raise ValueError("migration versions must be unique")
        if (
            not owner_id
            or owner_id != owner_id.strip()
            or len(owner_id) > 255
            or any(ord(char) < 32 or ord(char) == 127 for char in owner_id)
        ):
            raise ValueError("migration owner_id is invalid")
        if not math.isfinite(lock_seconds) or not 5 <= lock_seconds <= 3600:
            raise ValueError("migration lock_seconds must be between 5 and 3600")
        self.owner_id = owner_id
        self._clock = clock
        self.lock_seconds = lock_seconds
        interval = (
            lock_seconds / 3
            if heartbeat_interval_seconds is None
            else heartbeat_interval_seconds
        )
        if not math.isfinite(interval) or interval <= 0 or interval >= lock_seconds:
            raise ValueError(
                "migration heartbeat interval must be positive and below lock_seconds"
            )
        self.heartbeat_interval_seconds = float(interval)
        execute_script(
            backend,
            [
                backend.sql(
                    sqlite="""CREATE TABLE IF NOT EXISTS ha_schema_migrations (
                    version INTEGER PRIMARY KEY,name TEXT NOT NULL,checksum TEXT NOT NULL,
                    phase TEXT NOT NULL,backfill_cursor TEXT,error TEXT,created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,expanded_at REAL,backfilled_at REAL,
                    validated_at REAL,contracted_at REAL)""",
                    postgres="""CREATE TABLE IF NOT EXISTS ha_schema_migrations (
                    version BIGINT PRIMARY KEY,name TEXT NOT NULL,checksum TEXT NOT NULL,
                    phase TEXT NOT NULL,backfill_cursor TEXT,error TEXT,
                    created_at DOUBLE PRECISION NOT NULL,updated_at DOUBLE PRECISION NOT NULL,
                    expanded_at DOUBLE PRECISION,backfilled_at DOUBLE PRECISION,
                    validated_at DOUBLE PRECISION,contracted_at DOUBLE PRECISION)""",
                ),
                backend.sql(
                    sqlite="""CREATE TABLE IF NOT EXISTS ha_migration_lock (
                    lock_id INTEGER PRIMARY KEY CHECK(lock_id=1),owner_id TEXT,lease_token TEXT,
                    lease_expires_at REAL,updated_at REAL NOT NULL)""",
                    postgres="""CREATE TABLE IF NOT EXISTS ha_migration_lock (
                    lock_id INTEGER PRIMARY KEY CHECK(lock_id=1),owner_id TEXT,lease_token TEXT,
                    lease_expires_at DOUBLE PRECISION,updated_at DOUBLE PRECISION NOT NULL)""",
                ),
                backend.sql(
                    sqlite="INSERT OR IGNORE INTO ha_migration_lock(lock_id,updated_at) VALUES(1,0)",
                    postgres="INSERT INTO ha_migration_lock(lock_id,updated_at) VALUES(1,0) ON CONFLICT(lock_id) DO NOTHING",
                ),
            ],
        )

    @contextlib.contextmanager
    def lock(self) -> Iterator[str]:
        advisory = getattr(self.backend, "advisory_lock", None)
        outer = (
            advisory("cogdoc-ha-schema-migrations", timeout_seconds=30)
            if callable(advisory)
            else contextlib.nullcontext()
        )
        with outer:
            token = secrets.token_urlsafe(32)
            now = self._clock()
            marker = self.backend.sql(sqlite="?", postgres="%s")
            with self.backend.transaction(write=True) as connection:
                suffix = self.backend.sql(sqlite="", postgres=" FOR UPDATE")
                row = connection.execute(
                    f"SELECT owner_id,lease_expires_at FROM ha_migration_lock WHERE lock_id=1{suffix}"
                ).fetchone()
                if row is None:
                    raise MigrationError("migration lock row is unavailable")
                owner = row["owner_id"] if isinstance(row, dict) else row[0]
                expires = row["lease_expires_at"] if isinstance(row, dict) else row[1]
                if expires is not None and float(expires) > now:
                    raise MigrationConflict(
                        f"migrations are already running by {owner}"
                    )
                changed = connection.execute(
                    f"UPDATE ha_migration_lock SET owner_id={marker},lease_token={marker},"
                    f"lease_expires_at={marker},updated_at={marker} WHERE lock_id=1 "
                    f"AND (lease_expires_at IS NULL OR lease_expires_at<={marker})",
                    (self.owner_id, token, now + self.lock_seconds, now, now),
                )
                if changed.rowcount != 1:
                    raise MigrationConflict("migration lock was claimed concurrently")
            try:
                yield token
            finally:
                with self.backend.transaction(write=True) as connection:
                    connection.execute(
                        f"UPDATE ha_migration_lock SET owner_id=NULL,lease_token=NULL,"
                        f"lease_expires_at=NULL,updated_at={marker} WHERE lock_id=1 "
                        f"AND lease_token={marker}",
                        (self._clock(), token),
                    )

    def _heartbeat(self, token: str) -> None:
        now = self._clock()
        marker = self.backend.sql(sqlite="?", postgres="%s")
        with self.backend.transaction(write=True) as connection:
            changed = connection.execute(
                f"UPDATE ha_migration_lock SET lease_expires_at={marker},updated_at={marker} "
                f"WHERE lock_id=1 AND lease_token={marker} AND lease_expires_at>{marker}",
                (now + self.lock_seconds, now, token, now),
            )
            if changed.rowcount != 1:
                raise MigrationConflict("migration lock lease expired")

    @contextlib.contextmanager
    def _lease_keeper(self, token: str) -> Iterator[threading.Event]:
        finished = threading.Event()
        lost = threading.Event()

        def keep() -> None:
            while not finished.wait(self.heartbeat_interval_seconds):
                try:
                    self._heartbeat(token)
                except BaseException:
                    lost.set()
                    return

        thread = threading.Thread(
            target=keep,
            name=f"cogdoc-ha-migration-heartbeat-{self.owner_id}",
            daemon=True,
        )
        thread.start()
        try:
            yield lost
        finally:
            finished.set()
            thread.join(self.heartbeat_interval_seconds + 1)
            if thread.is_alive():
                lost.set()

    @staticmethod
    def _require_lease(lost: threading.Event) -> None:
        if lost.is_set():
            raise MigrationConflict("migration lock lease was lost")

    def run(
        self,
        *,
        batch_size: int = 1000,
        max_batches: int | None = None,
        allow_contract: bool = False,
        minimum_compatible_version: int | None = None,
    ) -> list[dict[str, Any]]:
        if type(batch_size) is not int or not 1 <= batch_size <= 100_000:
            raise ValueError("migration batch_size must be between 1 and 100000")
        if max_batches is not None and (
            type(max_batches) is not int or not 1 <= max_batches <= 100_000
        ):
            raise ValueError("migration max_batches is invalid")
        if allow_contract and minimum_compatible_version is None:
            raise ValueError("contract migrations require minimum_compatible_version")
        results: list[dict[str, Any]] = []
        with self.lock() as token, self._lease_keeper(token) as lease_lost:
            batches = 0
            for migration in self.migrations:
                self._require_lease(lease_lost)
                state = self._register(migration)
                if state["phase"] == MIGRATION_REGISTERED:
                    self._apply_statements(migration.expand)
                    self._require_lease(lease_lost)
                    state = self._advance(
                        migration.version, MIGRATION_EXPANDED, "expanded_at"
                    )
                if state["phase"] == MIGRATION_EXPANDED:
                    if migration.backfill is not None:
                        while True:
                            if max_batches is not None and batches >= max_batches:
                                results.append(self.get(migration.version) or {})
                                return results
                            cursor = state["backfill_cursor"]
                            next_cursor = migration.backfill(
                                self.backend, cursor, batch_size
                            )
                            batches += 1
                            self._require_lease(lease_lost)
                            self._heartbeat(token)
                            if next_cursor is None:
                                break
                            if not isinstance(next_cursor, str) or not next_cursor:
                                raise MigrationError(
                                    "backfill cursor must be a non-empty string"
                                )
                            state = self._set_cursor(migration.version, next_cursor)
                    state = self._advance(
                        migration.version, MIGRATION_BACKFILLED, "backfilled_at"
                    )
                if state["phase"] == MIGRATION_BACKFILLED:
                    if migration.validate is not None and not migration.validate(
                        self.backend
                    ):
                        raise MigrationError(
                            f"migration {migration.version} validation failed"
                        )
                    self._require_lease(lease_lost)
                    state = self._advance(
                        migration.version, MIGRATION_VALIDATED, "validated_at"
                    )
                if state["phase"] == MIGRATION_VALIDATED and migration.contract:
                    if allow_contract:
                        assert minimum_compatible_version is not None
                        if minimum_compatible_version < migration.version:
                            raise MigrationConflict(
                                "contract migration is newer than the oldest live application"
                            )
                        self._apply_statements(migration.contract)
                        self._require_lease(lease_lost)
                        state = self._advance(
                            migration.version, MIGRATION_CONTRACTED, "contracted_at"
                        )
                results.append(state)
                self._require_lease(lease_lost)
                self._heartbeat(token)
        return results

    def _register(self, migration: Migration) -> dict[str, Any]:
        now = self._clock()
        marker = self.backend.sql(sqlite="?", postgres="%s")
        insert = self.backend.sql(sqlite="INSERT OR IGNORE", postgres="INSERT")
        suffix = self.backend.sql(
            sqlite="", postgres=" ON CONFLICT(version) DO NOTHING"
        )
        placeholders = self.backend.sql(
            sqlite="?,?,?,?,?,?,?", postgres="%s,%s,%s,%s,%s,%s,%s"
        )
        with self.backend.transaction(write=True) as connection:
            connection.execute(
                f"{insert} INTO ha_schema_migrations(version,name,checksum,phase,created_at,"
                f"updated_at,backfill_cursor) VALUES({placeholders}){suffix}",
                (
                    migration.version,
                    migration.name,
                    migration.checksum,
                    MIGRATION_REGISTERED,
                    now,
                    now,
                    None,
                ),
            )
            row = connection.execute(
                f"SELECT * FROM ha_schema_migrations WHERE version={marker}",
                (migration.version,),
            ).fetchone()
            if row is None:
                raise MigrationError("migration registration failed")
            state = dict(row)
            if (
                state["name"] != migration.name
                or state["checksum"] != migration.checksum
            ):
                raise MigrationConflict(
                    f"migration {migration.version} checksum or name changed after registration"
                )
            return state

    def _apply_statements(self, statements: Sequence[str]) -> None:
        if statements:
            execute_script(self.backend, statements)

    def _advance(
        self, version: int, phase: str, timestamp_column: str
    ) -> dict[str, Any]:
        if timestamp_column not in {
            "expanded_at",
            "backfilled_at",
            "validated_at",
            "contracted_at",
        }:
            raise ValueError("migration timestamp column is invalid")
        now = self._clock()
        marker = self.backend.sql(sqlite="?", postgres="%s")
        with self.backend.transaction(write=True) as connection:
            connection.execute(
                f"UPDATE ha_schema_migrations SET phase={marker},{timestamp_column}={marker},"
                f"updated_at={marker},error=NULL WHERE version={marker}",
                (phase, now, now, version),
            )
            row = connection.execute(
                f"SELECT * FROM ha_schema_migrations WHERE version={marker}", (version,)
            ).fetchone()
            if row is None:
                raise MigrationConflict(
                    f"migration {version} disappeared while advancing to {phase}"
                )
            return dict(row)

    def _set_cursor(self, version: int, cursor: str) -> dict[str, Any]:
        marker = self.backend.sql(sqlite="?", postgres="%s")
        with self.backend.transaction(write=True) as connection:
            connection.execute(
                f"UPDATE ha_schema_migrations SET backfill_cursor={marker},updated_at={marker} "
                f"WHERE version={marker} AND phase='{MIGRATION_EXPANDED}'",
                (cursor, self._clock(), version),
            )
            row = connection.execute(
                f"SELECT * FROM ha_schema_migrations WHERE version={marker}", (version,)
            ).fetchone()
            if row is None:
                raise MigrationConflict(
                    f"migration {version} disappeared while saving its cursor"
                )
            return dict(row)

    def get(self, version: int) -> dict[str, Any] | None:
        marker = self.backend.sql(sqlite="?", postgres="%s")
        with self.backend.transaction() as connection:
            row = connection.execute(
                f"SELECT * FROM ha_schema_migrations WHERE version={marker}", (version,)
            ).fetchone()
            return None if row is None else dict(row)


__all__ = [
    "MIGRATION_BACKFILLED",
    "MIGRATION_CONTRACTED",
    "MIGRATION_EXPANDED",
    "MIGRATION_REGISTERED",
    "MIGRATION_VALIDATED",
    "Migration",
    "MigrationConflict",
    "MigrationError",
    "MigrationRunner",
]
