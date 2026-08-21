from __future__ import annotations

import math
import secrets
import threading
import time
from collections.abc import Callable
from typing import Any

from cogdoc.ha.storage import DatabaseBackend, execute_script


class VersionRegistryError(RuntimeError):
    pass


class ApplicationVersionRegistry:
    """Lease-based evidence for safe rolling schema contraction."""

    def __init__(
        self,
        backend: DatabaseBackend,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.backend = backend
        self._clock = clock
        execute_script(
            backend,
            [
                backend.sql(
                    sqlite="""CREATE TABLE IF NOT EXISTS ha_application_instances (
                    instance_id TEXT PRIMARY KEY,session_token TEXT NOT NULL,
                    release_id TEXT NOT NULL,
                    minimum_schema_version INTEGER NOT NULL,
                    maximum_schema_version INTEGER NOT NULL,
                    started_at REAL NOT NULL,last_heartbeat_at REAL NOT NULL,
                    expires_at REAL NOT NULL)""",
                    postgres="""CREATE TABLE IF NOT EXISTS ha_application_instances (
                    instance_id TEXT PRIMARY KEY,session_token TEXT NOT NULL,
                    release_id TEXT NOT NULL,
                    minimum_schema_version BIGINT NOT NULL,
                    maximum_schema_version BIGINT NOT NULL,
                    started_at DOUBLE PRECISION NOT NULL,
                    last_heartbeat_at DOUBLE PRECISION NOT NULL,
                    expires_at DOUBLE PRECISION NOT NULL)""",
                ),
                "CREATE INDEX IF NOT EXISTS idx_ha_application_instances_expiry "
                "ON ha_application_instances(expires_at)",
            ],
        )

    @staticmethod
    def _identity(value: str, field: str) -> str:
        if (
            not isinstance(value, str)
            or not value
            or value != value.strip()
            or len(value.encode()) > 255
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
        ):
            raise ValueError(f"{field} is invalid")
        return value

    @staticmethod
    def _schema_range(minimum: int, maximum: int) -> tuple[int, int]:
        if (
            type(minimum) is not int
            or type(maximum) is not int
            or not 1 <= minimum <= maximum <= 2_147_483_647
        ):
            raise ValueError("application schema compatibility range is invalid")
        return minimum, maximum

    def heartbeat(
        self,
        instance_id: str,
        session_token: str,
        release_id: str,
        *,
        minimum_schema_version: int,
        maximum_schema_version: int,
        ttl_seconds: float = 90.0,
    ) -> dict[str, Any]:
        instance_id = self._identity(instance_id, "instance_id")
        session_token = self._identity(session_token, "session_token")
        release_id = self._identity(release_id, "release_id")
        minimum, maximum = self._schema_range(
            minimum_schema_version, maximum_schema_version
        )
        if not math.isfinite(ttl_seconds) or not 10 <= ttl_seconds <= 3600:
            raise ValueError("application version heartbeat TTL is invalid")
        now = self._clock()
        marker = self.backend.sql(sqlite="?", postgres="%s")
        insert = self.backend.sql(sqlite="INSERT OR IGNORE", postgres="INSERT")
        conflict = self.backend.sql(
            sqlite="",
            postgres=" ON CONFLICT(instance_id) DO NOTHING",
        )
        placeholders = self.backend.sql(
            sqlite="?,?,?,?,?,?,?,?", postgres="%s,%s,%s,%s,%s,%s,%s,%s"
        )
        with self.backend.transaction(write=True) as connection:
            connection.execute(
                f"{insert} INTO ha_application_instances(instance_id,session_token,release_id,"
                "minimum_schema_version,maximum_schema_version,started_at,"
                f"last_heartbeat_at,expires_at) VALUES({placeholders}){conflict}",
                (
                    instance_id,
                    session_token,
                    release_id,
                    minimum,
                    maximum,
                    now,
                    now,
                    now + ttl_seconds,
                ),
            )
            changed = connection.execute(
                f"UPDATE ha_application_instances SET release_id={marker},"
                f"minimum_schema_version={marker},maximum_schema_version={marker},"
                f"last_heartbeat_at={marker},expires_at={marker},session_token={marker} "
                f"WHERE instance_id={marker} AND "
                f"(session_token={marker} OR expires_at<={marker})",
                (
                    release_id,
                    minimum,
                    maximum,
                    now,
                    now + ttl_seconds,
                    session_token,
                    instance_id,
                    session_token,
                    now,
                ),
            )
            if changed.rowcount != 1:
                raise VersionRegistryError(
                    "application instance id is owned by another live process"
                )
            row = connection.execute(
                f"SELECT * FROM ha_application_instances WHERE instance_id={marker}",
                (instance_id,),
            ).fetchone()
        if row is None:
            raise VersionRegistryError("application version heartbeat disappeared")
        return dict(row)

    def retire(self, instance_id: str, session_token: str) -> bool:
        instance_id = self._identity(instance_id, "instance_id")
        session_token = self._identity(session_token, "session_token")
        marker = self.backend.sql(sqlite="?", postgres="%s")
        with self.backend.transaction(write=True) as connection:
            changed = connection.execute(
                f"DELETE FROM ha_application_instances WHERE instance_id={marker} "
                f"AND session_token={marker}",
                (instance_id, session_token),
            )
            return changed.rowcount == 1

    def live(self) -> list[dict[str, Any]]:
        marker = self.backend.sql(sqlite="?", postgres="%s")
        with self.backend.transaction() as connection:
            rows = connection.execute(
                f"SELECT * FROM ha_application_instances WHERE expires_at>{marker} "
                "ORDER BY instance_id",
                (self._clock(),),
            ).fetchall()
        return [dict(row) for row in rows]

    def contract_floor(self) -> int | None:
        rows = self.live()
        if not rows:
            return None
        return min(int(row["minimum_schema_version"]) for row in rows)

    def check_instance(self, instance_id: str) -> bool:
        instance_id = self._identity(instance_id, "instance_id")
        marker = self.backend.sql(sqlite="?", postgres="%s")
        with self.backend.transaction() as connection:
            row = connection.execute(
                f"SELECT expires_at FROM ha_application_instances "
                f"WHERE instance_id={marker}",
                (instance_id,),
            ).fetchone()
        if row is None:
            return False
        expires = row["expires_at"] if isinstance(row, dict) else row[0]
        return float(expires) > self._clock()


class VersionHeartbeat:
    def __init__(
        self,
        registry: ApplicationVersionRegistry,
        *,
        instance_id: str,
        release_id: str,
        minimum_schema_version: int,
        maximum_schema_version: int,
        interval_seconds: float = 30.0,
        ttl_seconds: float = 90.0,
    ) -> None:
        if (
            not math.isfinite(interval_seconds)
            or not math.isfinite(ttl_seconds)
            or not 5 <= interval_seconds < ttl_seconds <= 3600
        ):
            raise ValueError("application version heartbeat timing is invalid")
        self.registry = registry
        self.instance_id = instance_id
        self.release_id = release_id
        self.minimum_schema_version = minimum_schema_version
        self.maximum_schema_version = maximum_schema_version
        self.interval_seconds = float(interval_seconds)
        self.ttl_seconds = float(ttl_seconds)
        self._session_token = secrets.token_urlsafe(32)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._last_error: BaseException | None = None

    def _beat(self) -> None:
        self.registry.heartbeat(
            self.instance_id,
            self._session_token,
            self.release_id,
            minimum_schema_version=self.minimum_schema_version,
            maximum_schema_version=self.maximum_schema_version,
            ttl_seconds=self.ttl_seconds,
        )

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._last_error = None
            self._beat()
            self._thread = threading.Thread(
                target=self._run,
                name="cogdoc-ha-version-heartbeat",
                daemon=True,
            )
            self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            try:
                self._beat()
            except BaseException as exc:
                self._last_error = exc
                return

    def stop(self, *, timeout_seconds: float = 10.0) -> bool:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout_seconds)
        stopped = thread is None or not thread.is_alive()
        if stopped:
            self._thread = None
            try:
                self.registry.retire(self.instance_id, self._session_token)
            except Exception:
                # The bounded TTL is the crash-safe retirement path.  A control
                # database outage must not keep process shutdown from completing.
                pass
        return stopped

    def check(self) -> bool:
        thread = self._thread
        return (
            self._last_error is None
            and thread is not None
            and thread.is_alive()
            and self.registry.check_instance(self.instance_id)
        )


__all__ = [
    "ApplicationVersionRegistry",
    "VersionHeartbeat",
    "VersionRegistryError",
]
