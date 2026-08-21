from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any

from cogdoc.ha.index_generation import (
    GEN_ABORTED,
    GEN_BUILDING,
    GEN_PREPARED,
    GEN_PUBLISHED,
)
from cogdoc.ha.outbox import (
    OUTBOX_DEAD_LETTER,
    OUTBOX_DELIVERED,
    OUTBOX_DELIVERING,
    OUTBOX_PENDING,
)
from cogdoc.ha.tasks import (
    JOB_CANCELLED,
    JOB_DEAD_LETTER,
    JOB_FAILED,
    JOB_QUEUED,
    JOB_RETRY_WAIT,
    JOB_RUNNING,
    JOB_SUCCEEDED,
)


JOB_STATUSES = (
    JOB_QUEUED,
    JOB_RUNNING,
    JOB_RETRY_WAIT,
    JOB_SUCCEEDED,
    JOB_FAILED,
    JOB_DEAD_LETTER,
    JOB_CANCELLED,
)
OUTBOX_STATUSES = (
    OUTBOX_PENDING,
    OUTBOX_DELIVERING,
    OUTBOX_DELIVERED,
    OUTBOX_DEAD_LETTER,
)
GENERATION_STATUSES = (GEN_BUILDING, GEN_PREPARED, GEN_PUBLISHED, GEN_ABORTED)


def _first(row: Any | None, name: str = "value") -> int:
    if row is None:
        return 0
    value = row.get(name) if isinstance(row, Mapping) else row[0]
    return int(value or 0)


def _counts(connection: Any, table: str, statuses: tuple[str, ...]) -> dict[str, int]:
    rows = connection.execute(
        f"SELECT status,COUNT(*) AS value FROM {table} GROUP BY status"
    ).fetchall()
    raw = {
        str(row["status"] if isinstance(row, Mapping) else row[0]): int(
            row["value"] if isinstance(row, Mapping) else row[1]
        )
        for row in rows
    }
    return {status: raw.get(status, 0) for status in statuses}


class HAOperationalSnapshot:
    """One bounded SQL snapshot for readiness dashboards and Prometheus."""

    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime

    def collect(self) -> dict[str, Any]:
        now = time.time()
        marker = self.runtime.backend.sql(sqlite="?", postgres="%s")
        with self.runtime.backend.transaction() as connection:
            jobs = _counts(connection, "ha_jobs", JOB_STATUSES)
            outbox = _counts(connection, "ha_outbox", OUTBOX_STATUSES)
            generations = _counts(
                connection, "ha_index_generations", GENERATION_STATUSES
            )
            expired_job_leases = _first(
                connection.execute(
                    f"SELECT COUNT(*) AS value FROM ha_jobs WHERE status='running' "
                    f"AND lease_expires_at<={marker}",
                    (now,),
                ).fetchone()
            )
            due_schedules = _first(
                connection.execute(
                    f"SELECT COUNT(*) AS value FROM ha_schedules WHERE enabled=1 "
                    f"AND next_run_at<={marker}",
                    (now,),
                ).fetchone()
            )
            enabled_schedules = _first(
                connection.execute(
                    "SELECT COUNT(*) AS value FROM ha_schedules WHERE enabled=1"
                ).fetchone()
            )
            current_generations = _first(
                connection.execute(
                    "SELECT COUNT(*) AS value FROM ha_index_heads "
                    "WHERE current_generation_id IS NOT NULL"
                ).fetchone()
            )
            live_instances = _first(
                connection.execute(
                    f"SELECT COUNT(*) AS value FROM ha_application_instances "
                    f"WHERE expires_at>{marker}",
                    (now,),
                ).fetchone()
            )
        maintenance = self.runtime.maintenance.snapshot()
        return {
            "jobs": jobs,
            "outbox": outbox,
            "generations": generations,
            "expired_job_leases": expired_job_leases,
            "enabled_schedules": enabled_schedules,
            "due_schedules": due_schedules,
            "current_generations": current_generations,
            "live_instances": live_instances,
            "maintenance_failures": maintenance.failures,
            "maintenance_last_succeeded_at": maintenance.last_succeeded_at,
        }


__all__ = [
    "GENERATION_STATUSES",
    "HAOperationalSnapshot",
    "JOB_STATUSES",
    "OUTBOX_STATUSES",
]
