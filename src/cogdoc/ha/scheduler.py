from __future__ import annotations

import json
import logging
import math
import threading
import time
import uuid
from calendar import monthrange
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any, Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from cogdoc.ha.storage import DatabaseBackend, execute_script
from cogdoc.ha.tasks import LeaseJobStore


SCHEDULE_ONCE: Final = "once"
SCHEDULE_INTERVAL: Final = "interval"
SCHEDULE_CRON: Final = "cron"
_SCHEDULE_TYPES = frozenset({SCHEDULE_ONCE, SCHEDULE_INTERVAL, SCHEDULE_CRON})
_MAX_PAYLOAD_BYTES = 1024 * 1024
_MONTH_NAMES = {
    name: index
    for index, name in enumerate(
        (
            "JAN",
            "FEB",
            "MAR",
            "APR",
            "MAY",
            "JUN",
            "JUL",
            "AUG",
            "SEP",
            "OCT",
            "NOV",
            "DEC",
        ),
        1,
    )
}
_WEEKDAY_NAMES = {
    "SUN": 0,
    "MON": 1,
    "TUE": 2,
    "WED": 3,
    "THU": 4,
    "FRI": 5,
    "SAT": 6,
}


def _clean(value: str, field: str, maximum: int = 255) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError(f"{field} is invalid")
    return value


def _payload(value: Any) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("schedule payload must be JSON serializable") from exc
    if len(encoded.encode()) > _MAX_PAYLOAD_BYTES:
        raise ValueError("schedule payload exceeds 1 MiB")
    return encoded


def _cron_number(
    value: str, *, names: Mapping[str, int], minimum: int, maximum: int
) -> int:
    normalized = value.upper()
    if normalized in names:
        return names[normalized]
    try:
        result = int(value)
    except ValueError as exc:
        raise ValueError("cron field contains an invalid value") from exc
    if result == 7 and maximum == 6:
        result = 0
    if not minimum <= result <= maximum:
        raise ValueError("cron field value is out of range")
    return result


def _cron_field(
    expression: str,
    *,
    minimum: int,
    maximum: int,
    names: Mapping[str, int] | None = None,
) -> tuple[frozenset[int], bool]:
    names = names or {}
    wildcard = expression == "*"
    values: set[int] = set()
    for part in expression.split(","):
        if not part:
            raise ValueError("cron field contains an empty item")
        base, separator, raw_step = part.partition("/")
        if separator:
            try:
                step = int(raw_step)
            except ValueError as exc:
                raise ValueError("cron step is invalid") from exc
            if step < 1 or step > maximum - minimum + 1:
                raise ValueError("cron step is out of range")
        else:
            step = 1
        if base == "*":
            start, end = minimum, maximum
        elif "-" in base:
            raw_start, raw_end = base.split("-", 1)
            start = _cron_number(
                raw_start, names=names, minimum=minimum, maximum=maximum
            )
            end = _cron_number(raw_end, names=names, minimum=minimum, maximum=maximum)
            if start > end:
                raise ValueError("cron ranges must be ascending")
        else:
            start = _cron_number(base, names=names, minimum=minimum, maximum=maximum)
            end = start
        values.update(range(start, end + 1, step))
    if not values:
        raise ValueError("cron field cannot be empty")
    return frozenset(values), wildcard


class CronExpression:
    """Strict five-field cron expression evaluated in an IANA timezone."""

    def __init__(self, expression: str, timezone_name: str = "UTC") -> None:
        if not isinstance(expression, str) or len(expression) > 256:
            raise ValueError("cron expression is invalid")
        parts = expression.split()
        if len(parts) != 5:
            raise ValueError("cron expression must contain five fields")
        self.expression = expression
        try:
            self.timezone = ZoneInfo(timezone_name)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("schedule timezone is invalid") from exc
        self.minutes, _ = _cron_field(parts[0], minimum=0, maximum=59)
        self.hours, _ = _cron_field(parts[1], minimum=0, maximum=23)
        self.days, self.days_wildcard = _cron_field(parts[2], minimum=1, maximum=31)
        self.months, _ = _cron_field(
            parts[3], minimum=1, maximum=12, names=_MONTH_NAMES
        )
        self.weekdays, self.weekdays_wildcard = _cron_field(
            parts[4], minimum=0, maximum=6, names=_WEEKDAY_NAMES
        )

    def matches(self, candidate: datetime) -> bool:
        local = candidate.astimezone(self.timezone)
        cron_weekday = (local.weekday() + 1) % 7
        day_matches = local.day in self.days
        weekday_matches = cron_weekday in self.weekdays
        if not self.days_wildcard and not self.weekdays_wildcard:
            calendar_matches = day_matches or weekday_matches
        else:
            calendar_matches = day_matches and weekday_matches
        return bool(
            local.minute in self.minutes
            and local.hour in self.hours
            and local.month in self.months
            and calendar_matches
        )

    def next_after(self, epoch_seconds: float) -> float:
        if not math.isfinite(epoch_seconds) or epoch_seconds < 0:
            raise ValueError("cron cursor is invalid")
        candidate = datetime.fromtimestamp(epoch_seconds, timezone.utc).replace(
            second=0, microsecond=0
        ) + timedelta(minutes=1)
        # Five years is deliberately finite. An impossible expression such as
        # February 31 must fail configuration rather than spin forever.
        for _ in range(5 * 366 * 24 * 60):
            local = candidate.astimezone(self.timezone)
            if local.day <= monthrange(local.year, local.month)[1] and self.matches(
                candidate
            ):
                return candidate.timestamp()
            candidate += timedelta(minutes=1)
        raise ValueError("cron expression has no occurrence within five years")


class ScheduleStore:
    """Durable schedule ledger with a crash-safe fire handoff."""

    def __init__(
        self, backend: DatabaseBackend, *, clock: Callable[[], float] = time.time
    ) -> None:
        self.backend = backend
        self._clock = clock
        execute_script(
            backend,
            [
                backend.sql(
                    sqlite="""CREATE TABLE IF NOT EXISTS ha_schedules (
                    schedule_id TEXT PRIMARY KEY,tenant_id TEXT NOT NULL,queue_name TEXT NOT NULL,
                    payload_json TEXT NOT NULL,schedule_type TEXT NOT NULL,schedule_spec TEXT NOT NULL,
                    timezone_name TEXT NOT NULL,enabled INTEGER NOT NULL,next_run_at REAL NOT NULL,
                    last_run_at REAL,fire_sequence INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,updated_at REAL NOT NULL,revision INTEGER NOT NULL DEFAULT 1)""",
                    postgres="""CREATE TABLE IF NOT EXISTS ha_schedules (
                    schedule_id TEXT PRIMARY KEY,tenant_id TEXT NOT NULL,queue_name TEXT NOT NULL,
                    payload_json TEXT NOT NULL,schedule_type TEXT NOT NULL,schedule_spec TEXT NOT NULL,
                    timezone_name TEXT NOT NULL,enabled INTEGER NOT NULL,
                    next_run_at DOUBLE PRECISION NOT NULL,last_run_at DOUBLE PRECISION,
                    fire_sequence INTEGER NOT NULL DEFAULT 0,created_at DOUBLE PRECISION NOT NULL,
                    updated_at DOUBLE PRECISION NOT NULL,revision INTEGER NOT NULL DEFAULT 1)""",
                ),
                backend.sql(
                    sqlite="""CREATE TABLE IF NOT EXISTS ha_schedule_fires (
                    fire_id TEXT PRIMARY KEY,schedule_id TEXT NOT NULL,tenant_id TEXT NOT NULL,
                    queue_name TEXT NOT NULL,payload_json TEXT NOT NULL,due_at REAL NOT NULL,
                    status TEXT NOT NULL,job_id TEXT,created_at REAL NOT NULL,delivered_at REAL,
                    FOREIGN KEY(schedule_id) REFERENCES ha_schedules(schedule_id) ON DELETE CASCADE)""",
                    postgres="""CREATE TABLE IF NOT EXISTS ha_schedule_fires (
                    fire_id TEXT PRIMARY KEY,schedule_id TEXT NOT NULL REFERENCES ha_schedules(schedule_id) ON DELETE CASCADE,
                    tenant_id TEXT NOT NULL,queue_name TEXT NOT NULL,payload_json TEXT NOT NULL,
                    due_at DOUBLE PRECISION NOT NULL,status TEXT NOT NULL,job_id TEXT,
                    created_at DOUBLE PRECISION NOT NULL,delivered_at DOUBLE PRECISION)""",
                ),
                "CREATE INDEX IF NOT EXISTS idx_ha_schedules_due ON ha_schedules(enabled,next_run_at)",
                "CREATE INDEX IF NOT EXISTS idx_ha_schedule_fires_pending ON ha_schedule_fires(status,due_at,fire_id)",
            ],
        )

    @staticmethod
    def _row(row: Any | None) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        result["payload"] = json.loads(str(result.pop("payload_json")))
        result["enabled"] = bool(result.get("enabled", True))
        return result

    def create(
        self,
        tenant_id: str,
        queue: str,
        payload: Any,
        *,
        schedule_type: str,
        schedule_spec: str,
        timezone_name: str = "UTC",
        first_run_at: float | None = None,
        schedule_id: str | None = None,
    ) -> dict[str, Any]:
        tenant_id = _clean(tenant_id, "tenant_id")
        queue = _clean(queue, "queue", 128)
        if schedule_type not in _SCHEDULE_TYPES:
            raise ValueError("schedule_type is invalid")
        schedule_spec = _clean(schedule_spec, "schedule_spec", 256)
        now = self._clock()
        if schedule_type == SCHEDULE_ONCE:
            try:
                configured = float(schedule_spec)
            except ValueError as exc:
                raise ValueError(
                    "once schedule must contain an epoch timestamp"
                ) from exc
            next_run = configured if first_run_at is None else float(first_run_at)
        elif schedule_type == SCHEDULE_INTERVAL:
            try:
                interval = float(schedule_spec)
            except ValueError as exc:
                raise ValueError("interval schedule must contain seconds") from exc
            if not math.isfinite(interval) or not 1 <= interval <= 366 * 86_400:
                raise ValueError("interval must be between 1 second and 366 days")
            next_run = now + interval if first_run_at is None else float(first_run_at)
        else:
            cron = CronExpression(schedule_spec, timezone_name)
            next_run = (
                cron.next_after(now) if first_run_at is None else float(first_run_at)
            )
        if not math.isfinite(next_run) or next_run < 0:
            raise ValueError("first_run_at is invalid")
        schedule_id = (
            f"has-{uuid.uuid4().hex}"
            if schedule_id is None
            else _clean(schedule_id, "schedule_id")
        )
        payload_json = _payload(payload)
        placeholders = self.backend.sql(
            sqlite="?,?,?,?,?,?,?,?,?,?,?,?,?",
            postgres="%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s",
        )
        with self.backend.transaction(write=True) as connection:
            connection.execute(
                "INSERT INTO ha_schedules(schedule_id,tenant_id,queue_name,payload_json,"
                "schedule_type,schedule_spec,timezone_name,enabled,next_run_at,last_run_at,"
                f"fire_sequence,created_at,updated_at) VALUES({placeholders})",
                (
                    schedule_id,
                    tenant_id,
                    queue,
                    payload_json,
                    schedule_type,
                    schedule_spec,
                    timezone_name,
                    1,
                    next_run,
                    None,
                    0,
                    now,
                    now,
                ),
            )
        result = self.get(schedule_id)
        assert result is not None
        return result

    def get(self, schedule_id: str) -> dict[str, Any] | None:
        schedule_id = _clean(schedule_id, "schedule_id")
        marker = self.backend.sql(sqlite="?", postgres="%s")
        with self.backend.transaction() as connection:
            return self._row(
                connection.execute(
                    f"SELECT * FROM ha_schedules WHERE schedule_id={marker}",
                    (schedule_id,),
                ).fetchone()
            )

    def list_schedules(
        self,
        *,
        tenant_id: str,
        queue: str | None = None,
        enabled: bool | None = None,
        before: tuple[float, str] | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        tenant_id = _clean(tenant_id, "tenant_id")
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("schedule limit must be between 1 and 1000")
        marker = self.backend.sql(sqlite="?", postgres="%s")
        conditions = [f"tenant_id={marker}"]
        values: list[Any] = [tenant_id]
        if queue is not None:
            conditions.append(f"queue_name={marker}")
            values.append(_clean(queue, "queue", 128))
        if enabled is not None:
            if type(enabled) is not bool:
                raise ValueError("enabled must be a boolean")
            conditions.append(f"enabled={marker}")
            values.append(int(enabled))
        if before is not None:
            if not isinstance(before, tuple) or len(before) != 2:
                raise ValueError("schedule cursor is invalid")
            created_at = float(before[0])
            if not math.isfinite(created_at) or created_at < 0:
                raise ValueError("schedule cursor is invalid")
            schedule_id = _clean(before[1], "before_schedule_id")
            conditions.append(
                f"(created_at<{marker} OR (created_at={marker} AND schedule_id<{marker}))"
            )
            values.extend((created_at, created_at, schedule_id))
        with self.backend.transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM ha_schedules WHERE "
                + " AND ".join(conditions)
                + f" ORDER BY created_at DESC,schedule_id DESC LIMIT {limit}",
                tuple(values),
            ).fetchall()
            return [item for row in rows if (item := self._row(row)) is not None]

    def set_enabled(
        self,
        schedule_id: str,
        enabled: bool,
        *,
        expected_revision: int,
        tenant_id: str | None = None,
    ) -> dict[str, Any]:
        schedule_id = _clean(schedule_id, "schedule_id")
        if tenant_id is not None:
            tenant_id = _clean(tenant_id, "tenant_id")
        if type(enabled) is not bool or type(expected_revision) is not int:
            raise ValueError("enabled and expected_revision are invalid")
        marker = self.backend.sql(sqlite="?", postgres="%s")
        now = self._clock()
        with self.backend.transaction(write=True) as connection:
            tenant_clause = "" if tenant_id is None else f" AND tenant_id={marker}"
            values: tuple[Any, ...] = (
                (int(enabled), now, schedule_id, expected_revision)
                if tenant_id is None
                else (int(enabled), now, schedule_id, expected_revision, tenant_id)
            )
            changed = connection.execute(
                f"UPDATE ha_schedules SET enabled={marker},updated_at={marker},revision=revision+1 "
                f"WHERE schedule_id={marker} AND revision={marker}{tenant_clause}",
                values,
            )
            if changed.rowcount != 1:
                raise RuntimeError("schedule revision changed")
        result = self.get(schedule_id)
        assert result is not None
        return result

    def materialize_due(self, *, limit: int = 1000) -> int:
        if type(limit) is not int or not 1 <= limit <= 10_000:
            raise ValueError("limit must be between 1 and 10000")
        now = self._clock()
        marker = self.backend.sql(sqlite="?", postgres="%s")
        with self.backend.transaction(write=True) as connection:
            suffix = self.backend.sql(sqlite="", postgres=" FOR UPDATE SKIP LOCKED")
            rows = connection.execute(
                f"SELECT * FROM ha_schedules WHERE enabled=1 AND next_run_at<={marker} "
                f"ORDER BY next_run_at,schedule_id LIMIT {limit}{suffix}",
                (now,),
            ).fetchall()
            for raw in rows:
                row = dict(raw)
                sequence = int(row["fire_sequence"]) + 1
                fire_id = f"{row['schedule_id']}:{sequence}"
                due_at = float(row["next_run_at"])
                connection.execute(
                    self.backend.sql(
                        sqlite="INSERT OR IGNORE INTO ha_schedule_fires(fire_id,schedule_id,tenant_id,queue_name,payload_json,due_at,status,created_at) VALUES(?,?,?,?,?,?,?,?)",
                        postgres="INSERT INTO ha_schedule_fires(fire_id,schedule_id,tenant_id,queue_name,payload_json,due_at,status,created_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(fire_id) DO NOTHING",
                    ),
                    (
                        fire_id,
                        row["schedule_id"],
                        row["tenant_id"],
                        row["queue_name"],
                        row["payload_json"],
                        due_at,
                        "pending",
                        now,
                    ),
                )
                schedule_type = str(row["schedule_type"])
                enabled = 1
                if schedule_type == SCHEDULE_ONCE:
                    next_run = due_at
                    enabled = 0
                elif schedule_type == SCHEDULE_INTERVAL:
                    interval = float(row["schedule_spec"])
                    missed = max(1, int((now - due_at) // interval) + 1)
                    next_run = due_at + missed * interval
                else:
                    next_run = CronExpression(
                        str(row["schedule_spec"]), str(row["timezone_name"])
                    ).next_after(now)
                connection.execute(
                    f"UPDATE ha_schedules SET enabled={marker},next_run_at={marker},"
                    f"last_run_at={marker},fire_sequence={marker},updated_at={marker},"
                    f"revision=revision+1 WHERE schedule_id={marker} AND fire_sequence={marker}",
                    (
                        enabled,
                        next_run,
                        due_at,
                        sequence,
                        now,
                        row["schedule_id"],
                        sequence - 1,
                    ),
                )
            return len(rows)

    def dispatch_pending(self, jobs: LeaseJobStore, *, limit: int = 1000) -> int:
        if jobs.backend is not self.backend:
            raise ValueError("scheduler and job store must share one backend")
        if type(limit) is not int or not 1 <= limit <= 10_000:
            raise ValueError("limit must be between 1 and 10000")
        marker = self.backend.sql(sqlite="?", postgres="%s")
        with self.backend.transaction() as connection:
            rows = connection.execute(
                f"SELECT * FROM ha_schedule_fires WHERE status='pending' "
                f"ORDER BY due_at,fire_id LIMIT {int(limit)}"
            ).fetchall()
        delivered = 0
        for raw in rows:
            fire = dict(raw)
            job = jobs.enqueue(
                str(fire["queue_name"]),
                str(fire["tenant_id"]),
                json.loads(str(fire["payload_json"])),
                available_at=float(fire["due_at"]),
                idempotency_key=f"schedule:{fire['fire_id']}",
            )
            with self.backend.transaction(write=True) as connection:
                changed = connection.execute(
                    f"UPDATE ha_schedule_fires SET status='delivered',job_id={marker},"
                    f"delivered_at={marker} WHERE fire_id={marker} AND status='pending'",
                    (job["job_id"], self._clock(), fire["fire_id"]),
                )
                delivered += int(changed.rowcount == 1)
        return delivered

    def pending_fires(self) -> list[dict[str, Any]]:
        with self.backend.transaction() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM ha_schedule_fires WHERE status='pending' ORDER BY due_at,fire_id"
                ).fetchall()
            ]

    def prune_delivered(self, *, before: float, limit: int = 1000) -> int:
        if not math.isfinite(before):
            raise ValueError("schedule fire prune cutoff must be finite")
        if type(limit) is not int or not 1 <= limit <= 10_000:
            raise ValueError("schedule fire prune limit must be between 1 and 10000")
        marker = self.backend.sql(sqlite="?", postgres="%s")
        with self.backend.transaction(write=True) as connection:
            rows = connection.execute(
                f"SELECT fire_id FROM ha_schedule_fires WHERE status='delivered' "
                f"AND delivered_at<={marker} ORDER BY delivered_at,fire_id LIMIT {limit}",
                (before,),
            ).fetchall()
            removed = 0
            for row in rows:
                fire_id = str(row["fire_id"] if isinstance(row, Mapping) else row[0])
                changed = connection.execute(
                    f"DELETE FROM ha_schedule_fires WHERE fire_id={marker} "
                    f"AND status='delivered' AND delivered_at<={marker}",
                    (fire_id, before),
                )
                removed += int(changed.rowcount == 1)
            return removed


class DistributedScheduler:
    """One bounded poller per process; database rows coordinate all instances."""

    def __init__(
        self,
        schedules: ScheduleStore,
        jobs: LeaseJobStore,
        *,
        poll_seconds: float = 1.0,
        batch_size: int = 1000,
    ) -> None:
        if poll_seconds <= 0 or batch_size < 1:
            raise ValueError("scheduler bounds must be positive")
        self.schedules = schedules
        self.jobs = jobs
        self.poll_seconds = float(poll_seconds)
        self.batch_size = int(batch_size)
        self._condition = threading.Condition()
        self._stopping = False
        self._thread: threading.Thread | None = None
        self._last_error: BaseException | None = None

    def run_once(self) -> tuple[int, int]:
        fires = self.schedules.materialize_due(limit=self.batch_size)
        delivered = self.schedules.dispatch_pending(self.jobs, limit=self.batch_size)
        return fires, delivered

    def start(self) -> None:
        with self._condition:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stopping = False
            self._thread = threading.Thread(
                target=self._run, name="cogdoc-ha-scheduler", daemon=True
            )
            self._thread.start()

    def wake(self) -> None:
        with self._condition:
            self._condition.notify_all()

    def _run(self) -> None:
        while True:
            with self._condition:
                if self._stopping:
                    return
            try:
                self.run_once()
                self._last_error = None
            except Exception as exc:
                # Durable pending fire rows preserve work. Readiness/metrics can
                # report backend failure while this bounded poller keeps retrying.
                self._last_error = exc
                logging.getLogger(__name__).exception("HA scheduler cycle failed")
            with self._condition:
                if self._stopping:
                    return
                self._condition.wait(self.poll_seconds)

    def stop(self, *, timeout_seconds: float = 10.0) -> bool:
        with self._condition:
            self._stopping = True
            self._condition.notify_all()
            thread = self._thread
        if thread is not None:
            thread.join(timeout_seconds)
        return thread is None or not thread.is_alive()

    def check(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive() and self._last_error is None


__all__ = [
    "CronExpression",
    "DistributedScheduler",
    "SCHEDULE_CRON",
    "SCHEDULE_INTERVAL",
    "SCHEDULE_ONCE",
    "ScheduleStore",
]
