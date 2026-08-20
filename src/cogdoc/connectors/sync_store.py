from __future__ import annotations

import json
import math
import secrets
import sqlite3
import time
from threading import RLock
from typing import Any, Callable
from uuid import uuid4

from cogdoc.api.persistence import connect_sqlite
from cogdoc.connectors.base import StaleSyncLease, SyncCancelled


SYNC_PENDING = "pending"
SYNC_RUNNING = "running"
SYNC_COMMITTING = "committing"
SYNC_RETRY_WAIT = "retry_wait"
SYNC_SUCCEEDED = "succeeded"
SYNC_FAILED = "failed"
SYNC_DEAD_LETTER = "dead_letter"
SYNC_CANCELLED = "cancelled"
SYNC_TERMINAL = frozenset(
    {SYNC_SUCCEEDED, SYNC_FAILED, SYNC_DEAD_LETTER, SYNC_CANCELLED}
)
_FAILURE_STATUSES = frozenset({SYNC_RETRY_WAIT, SYNC_FAILED, SYNC_DEAD_LETTER})
_HEALTH_STATUS = {
    SYNC_PENDING: "queued",
    SYNC_RUNNING: "syncing",
    SYNC_COMMITTING: "syncing",
    SYNC_RETRY_WAIT: "retrying",
    SYNC_SUCCEEDED: "healthy",
    SYNC_FAILED: "failed",
    SYNC_DEAD_LETTER: "dead_letter",
    SYNC_CANCELLED: "cancelled",
}


def _is_failure_state(status: str, retry_at: float | None) -> bool:
    return bool(
        status in _FAILURE_STATUSES
        or (status == SYNC_COMMITTING and retry_at is not None)
    )


_JOB_SELECT = (
    "job_id,tenant_id,kb_id,connection_id,connector_type,status,start_cursor,cursor,lease_token,"
    "lease_expires_at,cancel_requested,attempt,pages_processed,documents_seen,documents_fetched,"
    "deleted_seen,bytes_fetched,error_code,error_message,retry_at,created_at,started_at,updated_at,"
    "finished_at,revision,replay_of,job_sequence,connection_revision,"
    "health_duration_seconds,health_failure_recorded,credential_id,credential_revision,"
    "cleanup_pending,attempt_started_at"
)


class ConnectorSyncStore:
    def __init__(self, db_path: str, *, clock: Callable[[], float] = time.time):
        self._lock = RLock()
        self._clock = clock
        self._conn = connect_sqlite(db_path)
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS connector_sync_jobs (
                job_id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                kb_id TEXT NOT NULL,
                connection_id TEXT NOT NULL,
                connector_type TEXT NOT NULL,
                status TEXT NOT NULL,
                start_cursor TEXT,
                cursor TEXT,
                lease_token TEXT,
                lease_expires_at REAL,
                cancel_requested INTEGER NOT NULL DEFAULT 0,
                attempt INTEGER NOT NULL DEFAULT 0,
                pages_processed INTEGER NOT NULL DEFAULT 0,
                documents_seen INTEGER NOT NULL DEFAULT 0,
                documents_fetched INTEGER NOT NULL DEFAULT 0,
                deleted_seen INTEGER NOT NULL DEFAULT 0,
                bytes_fetched INTEGER NOT NULL DEFAULT 0,
                error_code TEXT,
                error_message TEXT,
                retry_at REAL,
                created_at REAL NOT NULL,
                started_at REAL,
                updated_at REAL NOT NULL,
                finished_at REAL,
                revision INTEGER NOT NULL DEFAULT 0,
                replay_of TEXT,
                job_sequence INTEGER,
                connection_revision INTEGER NOT NULL DEFAULT 0,
                health_duration_seconds REAL,
                health_failure_recorded INTEGER NOT NULL DEFAULT 0,
                credential_id TEXT,
                credential_revision INTEGER NOT NULL DEFAULT 0,
                cleanup_pending INTEGER NOT NULL DEFAULT 0,
                attempt_started_at REAL
            );
            CREATE INDEX IF NOT EXISTS idx_connector_sync_jobs_scope
                ON connector_sync_jobs(tenant_id, kb_id, connection_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_connector_sync_jobs_runnable
                ON connector_sync_jobs(status, retry_at, lease_expires_at);
            CREATE TABLE IF NOT EXISTS connector_sync_checkpoints (
                tenant_id TEXT NOT NULL,
                kb_id TEXT NOT NULL,
                connection_id TEXT NOT NULL,
                cursor TEXT,
                last_job_id TEXT NOT NULL,
                last_success_at REAL NOT NULL,
                counters_json TEXT NOT NULL,
                PRIMARY KEY (tenant_id, kb_id, connection_id)
            );
            CREATE TABLE IF NOT EXISTS connector_sync_health (
                tenant_id TEXT NOT NULL,
                kb_id TEXT NOT NULL,
                connection_id TEXT NOT NULL,
                schedule_seconds INTEGER,
                next_run_at REAL,
                health_status TEXT NOT NULL DEFAULT 'unknown',
                last_job_id TEXT,
                last_job_status TEXT,
                last_started_at REAL,
                last_success_at REAL,
                last_failure_at REAL,
                last_error_code TEXT,
                last_duration_seconds REAL,
                consecutive_failures INTEGER NOT NULL DEFAULT 0,
                last_job_sequence INTEGER NOT NULL DEFAULT 0,
                last_success_sequence INTEGER NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL,
                PRIMARY KEY (tenant_id, kb_id, connection_id)
            );
            CREATE INDEX IF NOT EXISTS idx_connector_sync_health_due
                ON connector_sync_health(next_run_at, tenant_id, kb_id, connection_id);
            CREATE INDEX IF NOT EXISTS idx_connector_sync_health_last_job
                ON connector_sync_health(last_job_id);
            CREATE INDEX IF NOT EXISTS idx_connector_sync_checkpoints_last_job
                ON connector_sync_checkpoints(last_job_id);
            """
        )
        self._ensure_start_cursor_column()
        self._ensure_job_column("replay_of", "TEXT")
        self._ensure_job_column("connection_revision", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_job_column("health_duration_seconds", "REAL")
        self._ensure_job_column("health_failure_recorded", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_job_column("credential_id", "TEXT")
        self._ensure_job_column("credential_revision", "INTEGER NOT NULL DEFAULT 0")
        cleanup_columns = {
            str(row[1])
            for row in self._conn.execute(
                "PRAGMA table_info(connector_sync_jobs)"
            ).fetchall()
        }
        cleanup_migration = "cleanup_pending" not in cleanup_columns
        self._ensure_job_column("cleanup_pending", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_job_column("attempt_started_at", "REAL")
        if cleanup_migration:
            self._conn.execute(
                "UPDATE connector_sync_jobs SET cleanup_pending=1 WHERE status=?",
                (SYNC_SUCCEEDED,),
            )
        self._ensure_job_sequence()
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_connector_sync_jobs_replay_of "
            "ON connector_sync_jobs(replay_of)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_connector_sync_jobs_terminal_cleanup "
            "ON connector_sync_jobs(status,cleanup_pending,finished_at,job_sequence)"
        )
        self._ensure_health_projection()

    def _ensure_start_cursor_column(self) -> None:
        """Add and backfill the legacy cursor column in one migration transaction.

        ``NULL`` is a valid start cursor for newly-created jobs.  Therefore the
        legacy backfill must run only when this process actually adds the
        column, never on each store construction.
        """

        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                columns = {
                    str(row[1])
                    for row in self._conn.execute(
                        "PRAGMA table_info(connector_sync_jobs)"
                    ).fetchall()
                }
                if "start_cursor" not in columns:
                    self._conn.execute(
                        "ALTER TABLE connector_sync_jobs ADD COLUMN start_cursor TEXT"
                    )
                    self._conn.execute(
                        "UPDATE connector_sync_jobs SET start_cursor=cursor"
                    )
                self._conn.execute("COMMIT")
            except Exception:
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")
                raise

    def _ensure_job_column(self, name: str, definition: str) -> None:
        """Add a job column once; tolerate another process winning the race."""

        with self._lock:
            columns = {
                str(row[1])
                for row in self._conn.execute(
                    "PRAGMA table_info(connector_sync_jobs)"
                ).fetchall()
            }
            if name in columns:
                return
            try:
                self._conn.execute(
                    f"ALTER TABLE connector_sync_jobs ADD COLUMN {name} {definition}"
                )
            except sqlite3.OperationalError:
                refreshed = {
                    str(row[1])
                    for row in self._conn.execute(
                        "PRAGMA table_info(connector_sync_jobs)"
                    ).fetchall()
                }
                if name not in refreshed:
                    raise

    def _ensure_job_sequence(self) -> None:
        """Backfill a durable creation order and initialize its monotonic counter."""

        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                columns = {
                    str(row[1])
                    for row in self._conn.execute(
                        "PRAGMA table_info(connector_sync_jobs)"
                    ).fetchall()
                }
                if "job_sequence" not in columns:
                    self._conn.execute(
                        "ALTER TABLE connector_sync_jobs ADD COLUMN job_sequence INTEGER"
                    )
                self._conn.execute(
                    "CREATE TABLE IF NOT EXISTS connector_sync_job_sequence ("
                    "singleton INTEGER PRIMARY KEY CHECK(singleton=1),"
                    "last_value INTEGER NOT NULL)"
                )
                persisted = self._conn.execute(
                    "SELECT last_value FROM connector_sync_job_sequence WHERE singleton=1"
                ).fetchone()
                maximum = int(
                    self._conn.execute(
                        "SELECT COALESCE(MAX(job_sequence),0) FROM connector_sync_jobs"
                    ).fetchone()[0]
                )
                next_sequence = max(maximum, int(persisted[0]) if persisted else 0)
                missing = self._conn.execute(
                    "SELECT rowid FROM connector_sync_jobs WHERE job_sequence IS NULL "
                    "ORDER BY created_at,rowid"
                ).fetchall()
                for (rowid,) in missing:
                    next_sequence += 1
                    self._conn.execute(
                        "UPDATE connector_sync_jobs SET job_sequence=? WHERE rowid=?",
                        (next_sequence, rowid),
                    )
                self._conn.execute(
                    "INSERT INTO connector_sync_job_sequence(singleton,last_value) VALUES (1,?) "
                    "ON CONFLICT(singleton) DO UPDATE SET last_value=MAX(last_value,excluded.last_value)",
                    (next_sequence,),
                )
                self._conn.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_connector_sync_jobs_sequence "
                    "ON connector_sync_jobs(job_sequence)"
                )
                self._conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_connector_sync_jobs_scope_sequence "
                    "ON connector_sync_jobs(tenant_id,kb_id,connection_id,job_sequence DESC)"
                )
                self._conn.execute("COMMIT")
            except Exception:
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")
                raise

    def _next_job_sequence(self) -> int:
        """Allocate one sequence inside the caller's immediate transaction."""

        updated = self._conn.execute(
            "UPDATE connector_sync_job_sequence SET last_value=last_value+1 WHERE singleton=1"
        ).rowcount
        if updated != 1:  # pragma: no cover - initialized by store migration
            raise RuntimeError("sync job sequence is unavailable")
        return int(
            self._conn.execute(
                "SELECT last_value FROM connector_sync_job_sequence WHERE singleton=1"
            ).fetchone()[0]
        )

    def _ensure_health_projection(self) -> None:
        """Migrate health watermarks and repair crash-stale canonical rows."""

        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                columns = {
                    str(row[1])
                    for row in self._conn.execute(
                        "PRAGMA table_info(connector_sync_health)"
                    ).fetchall()
                }
                for name in ("last_job_sequence", "last_success_sequence"):
                    if name not in columns:
                        self._conn.execute(
                            f"ALTER TABLE connector_sync_health ADD COLUMN {name} "
                            "INTEGER NOT NULL DEFAULT 0"
                        )
                self._conn.execute(
                    "UPDATE connector_sync_jobs SET health_failure_recorded=1 "
                    "WHERE (status IN (?,?,?) OR (status=? AND retry_at IS NOT NULL)) "
                    "AND health_failure_recorded=0",
                    (*tuple(_FAILURE_STATUSES), SYNC_COMMITTING),
                )
                self._conn.execute(
                    "UPDATE connector_sync_jobs SET health_duration_seconds="
                    "MAX(0.0,COALESCE(finished_at,updated_at)-"
                    "COALESCE(started_at,created_at)) "
                    "WHERE status IN (?,?,?,?) AND health_duration_seconds IS NULL",
                    tuple(SYNC_TERMINAL),
                )
                self._conn.execute(
                    "UPDATE connector_sync_health SET last_job_sequence=COALESCE("
                    "(SELECT job_sequence FROM connector_sync_jobs WHERE job_id="
                    "connector_sync_health.last_job_id),0),last_success_sequence=COALESCE("
                    "(SELECT MAX(job_sequence) FROM connector_sync_jobs WHERE tenant_id="
                    "connector_sync_health.tenant_id AND kb_id=connector_sync_health.kb_id "
                    "AND connection_id=connector_sync_health.connection_id AND status=?),0)",
                    (SYNC_SUCCEEDED,),
                )
                self._reconcile_health_locked()
                self._conn.execute("COMMIT")
            except Exception:
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")
                raise

    def _reconcile_health_locked(self) -> None:
        latest_rows = self._conn.execute(
            "SELECT jobs.job_id,jobs.status,jobs.retry_at,health.last_job_id,"
            "health.last_job_status "
            "FROM connector_sync_jobs jobs LEFT JOIN connector_sync_health health "
            "ON health.tenant_id=jobs.tenant_id AND health.kb_id=jobs.kb_id "
            "AND health.connection_id=jobs.connection_id WHERE NOT EXISTS ("
            "SELECT 1 FROM connector_sync_jobs newer WHERE newer.tenant_id=jobs.tenant_id "
            "AND newer.kb_id=jobs.kb_id AND newer.connection_id=jobs.connection_id "
            "AND newer.job_sequence>jobs.job_sequence)"
        ).fetchall()
        for job_id, status, retry_at, projected_job_id, projected_status in latest_rows:
            count_failure = bool(
                _is_failure_state(str(status), retry_at)
                and (
                    projected_job_id != job_id
                    or not _is_failure_state(str(projected_status), retry_at)
                )
            )
            self._project_health_locked(str(job_id), count_failure=count_failure)

    def _project_health_locked(
        self,
        job_id: str,
        *,
        duration_seconds: float | None = None,
        count_failure: bool = False,
    ) -> tuple[str, str, str]:
        row = self._conn.execute(
            "SELECT tenant_id,kb_id,connection_id,status,job_sequence,created_at,"
            "started_at,attempt_started_at,updated_at,finished_at,error_code,retry_at,"
            "health_duration_seconds "
            "FROM connector_sync_jobs WHERE job_id=?",
            (job_id,),
        ).fetchone()
        if row is None:
            raise KeyError(job_id)
        (
            tenant_id,
            kb_id,
            connection_id,
            status,
            job_sequence,
            created_at,
            started_at,
            attempt_started_at,
            updated_at,
            finished_at,
            error_code,
            retry_at,
            persisted_duration,
        ) = row
        attempt_ended = bool(
            status in SYNC_TERMINAL
            or status == SYNC_RETRY_WAIT
            or (status == SYNC_COMMITTING and retry_at is not None)
        )
        if duration_seconds is not None and attempt_ended:
            persisted_duration = duration_seconds
        elif persisted_duration is None and attempt_ended:
            persisted_duration = max(
                0.0,
                float(finished_at if finished_at is not None else updated_at)
                - float(
                    attempt_started_at
                    if attempt_started_at is not None
                    else started_at
                    if started_at is not None
                    else created_at
                ),
            )
        if persisted_duration is not None:
            self._conn.execute(
                "UPDATE connector_sync_jobs SET health_duration_seconds=? WHERE job_id=?",
                (persisted_duration, job_id),
            )
        scope = (str(tenant_id), str(kb_id), str(connection_id))
        previous = self._conn.execute(
            "SELECT health_status,last_job_id,last_job_status,last_started_at,"
            "last_success_at,last_failure_at,last_error_code,last_duration_seconds,"
            "consecutive_failures,last_job_sequence,last_success_sequence "
            "FROM connector_sync_health WHERE tenant_id=? AND kb_id=? AND connection_id=?",
            scope,
        ).fetchone()
        if previous is None:
            previous = (
                "unknown",
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                0,
                0,
                0,
            )
        (
            health_status,
            last_job_id,
            last_job_status,
            last_started_at,
            last_success_at,
            last_failure_at,
            last_error_code,
            last_duration_seconds,
            consecutive_failures,
            last_job_sequence,
            last_success_sequence,
        ) = previous
        sequence = int(job_sequence)
        latest_sequence = int(last_job_sequence)
        success_sequence = int(last_success_sequence)
        if status == SYNC_SUCCEEDED:
            success_at = float(finished_at if finished_at is not None else updated_at)
            last_success_at = max(
                success_at,
                float(last_success_at) if last_success_at is not None else success_at,
            )
            success_sequence = max(success_sequence, sequence)
            if sequence >= latest_sequence:
                consecutive_failures = 0
        elif _is_failure_state(str(status), retry_at):
            failure_at = float(updated_at)
            last_failure_at = max(
                failure_at,
                float(last_failure_at) if last_failure_at is not None else failure_at,
            )
            if count_failure and sequence > success_sequence:
                consecutive_failures = int(consecutive_failures) + 1
        if sequence >= latest_sequence:
            health_status = (
                "retrying"
                if status == SYNC_COMMITTING and retry_at is not None
                else _HEALTH_STATUS[str(status)]
            )
            last_job_id = job_id
            last_job_status = str(status)
            last_started_at = started_at
            last_error_code = error_code
            last_duration_seconds = persisted_duration
            latest_sequence = sequence
        now = self._clock()
        self._conn.execute(
            "INSERT INTO connector_sync_health (tenant_id,kb_id,connection_id,health_status,"
            "last_job_id,last_job_status,last_started_at,last_success_at,last_failure_at,"
            "last_error_code,last_duration_seconds,consecutive_failures,last_job_sequence,"
            "last_success_sequence,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(tenant_id,kb_id,connection_id) DO UPDATE SET "
            "health_status=excluded.health_status,last_job_id=excluded.last_job_id,"
            "last_job_status=excluded.last_job_status,last_started_at=excluded.last_started_at,"
            "last_success_at=excluded.last_success_at,last_failure_at=excluded.last_failure_at,"
            "last_error_code=excluded.last_error_code,"
            "last_duration_seconds=excluded.last_duration_seconds,"
            "consecutive_failures=excluded.consecutive_failures,"
            "last_job_sequence=excluded.last_job_sequence,"
            "last_success_sequence=excluded.last_success_sequence,updated_at=excluded.updated_at",
            (
                *scope,
                health_status,
                last_job_id,
                last_job_status,
                last_started_at,
                last_success_at,
                last_failure_at,
                last_error_code,
                last_duration_seconds,
                int(consecutive_failures),
                latest_sequence,
                success_sequence,
                now,
            ),
        )
        return scope

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def check(self) -> bool:
        """Fail readiness when any durable sync-control table is unavailable."""

        with self._lock:
            for table in (
                "connector_sync_jobs",
                "connector_sync_job_sequence",
                "connector_sync_checkpoints",
                "connector_sync_health",
            ):
                self._conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
        return True

    def create(
        self,
        *,
        tenant_id: str,
        kb_id: str,
        connection_id: str,
        connector_type: str,
        connection_revision: int = 1,
        credential_id: str | None = None,
        credential_revision: int = 0,
        resume_cursor: str | None = None,
    ) -> dict[str, Any]:
        values = [
            str(value or "").strip()
            for value in (tenant_id, kb_id, connection_id, connector_type)
        ]
        if not all(values):
            raise ValueError("sync job scope and connector_type are required")
        if type(connection_revision) is not int or connection_revision < 1:
            raise ValueError("connection_revision must be a positive integer")
        credential, credential_revision = self._credential_snapshot(
            credential_id, credential_revision
        )
        values[3] = values[3].casefold()
        now = self._clock()
        job_id = f"sync-{uuid4().hex}"
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                job_sequence = self._next_job_sequence()
                self._conn.execute(
                    "INSERT INTO connector_sync_jobs "
                    "(job_id,tenant_id,kb_id,connection_id,connector_type,status,start_cursor,"
                    "cursor,created_at,updated_at,job_sequence,connection_revision,credential_id,"
                    "credential_revision) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        job_id,
                        *values,
                        SYNC_PENDING,
                        resume_cursor,
                        resume_cursor,
                        now,
                        now,
                        job_sequence,
                        connection_revision,
                        credential,
                        credential_revision,
                    ),
                )
                self._project_health_locked(job_id)
                self._conn.execute("COMMIT")
            except Exception:
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")
                raise
        return self.get(job_id) or {}

    def create_if_idle(
        self,
        *,
        tenant_id: str,
        kb_id: str,
        connection_id: str,
        connector_type: str,
        connection_revision: int = 1,
        credential_id: str | None = None,
        credential_revision: int = 0,
        resume_cursor: str | None = None,
    ) -> dict[str, Any]:
        """Atomically return an active job or create the connection's next job."""

        values = [
            str(value or "").strip()
            for value in (tenant_id, kb_id, connection_id, connector_type)
        ]
        if not all(values):
            raise ValueError("sync job scope and connector_type are required")
        if type(connection_revision) is not int or connection_revision < 1:
            raise ValueError("connection_revision must be a positive integer")
        credential, credential_revision = self._credential_snapshot(
            credential_id, credential_revision
        )
        values[3] = values[3].casefold()
        now = self._clock()
        job_id = f"sync-{uuid4().hex}"
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                active = self._conn.execute(
                    f"SELECT {_JOB_SELECT} FROM connector_sync_jobs "
                    "WHERE tenant_id=? AND kb_id=? AND connection_id=? "
                    "AND status IN (?,?,?,?) ORDER BY job_sequence LIMIT 1",
                    (
                        values[0],
                        values[1],
                        values[2],
                        SYNC_PENDING,
                        SYNC_RUNNING,
                        SYNC_COMMITTING,
                        SYNC_RETRY_WAIT,
                    ),
                ).fetchone()
                if active is None:
                    job_sequence = self._next_job_sequence()
                    self._conn.execute(
                        "INSERT INTO connector_sync_jobs "
                        "(job_id,tenant_id,kb_id,connection_id,connector_type,status,start_cursor,cursor,"
                        "created_at,updated_at,job_sequence,connection_revision,credential_id,"
                        "credential_revision) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            job_id,
                            *values,
                            SYNC_PENDING,
                            resume_cursor,
                            resume_cursor,
                            now,
                            now,
                            job_sequence,
                            connection_revision,
                            credential,
                            credential_revision,
                        ),
                    )
                    self._project_health_locked(job_id)
                self._conn.execute("COMMIT")
            except Exception:
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")
                raise
        return self._row(active) if active is not None else self.get(job_id) or {}

    def replay_dead_letter(
        self,
        job_id: str,
        *,
        connection_revision: int = 1,
        credential_id: str | None = None,
        credential_revision: int = 0,
    ) -> dict[str, Any]:
        """Create a fresh job linked to an immutable dead-letter job."""

        if type(connection_revision) is not int or connection_revision < 1:
            raise ValueError("connection_revision must be a positive integer")
        credential, credential_revision = self._credential_snapshot(
            credential_id, credential_revision
        )
        now = self._clock()
        replay_id = f"sync-{uuid4().hex}"
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT tenant_id,kb_id,connection_id,connector_type,status "
                    "FROM connector_sync_jobs WHERE job_id=?",
                    (job_id,),
                ).fetchone()
                if row is None:
                    raise KeyError(job_id)
                tenant_id, kb_id, connection_id, connector_type, status = row
                if status != SYNC_DEAD_LETTER:
                    raise ValueError("only a dead-letter sync job can be replayed")
                active = self._conn.execute(
                    "SELECT 1 FROM connector_sync_jobs WHERE tenant_id=? AND kb_id=? "
                    "AND connection_id=? AND status IN (?,?,?,?) LIMIT 1",
                    (
                        tenant_id,
                        kb_id,
                        connection_id,
                        SYNC_PENDING,
                        SYNC_RUNNING,
                        SYNC_COMMITTING,
                        SYNC_RETRY_WAIT,
                    ),
                ).fetchone()
                if active is not None:
                    raise ValueError("connection already has an active sync job")
                checkpoint = self._conn.execute(
                    "SELECT cursor FROM connector_sync_checkpoints "
                    "WHERE tenant_id=? AND kb_id=? AND connection_id=?",
                    (tenant_id, kb_id, connection_id),
                ).fetchone()
                cursor = checkpoint[0] if checkpoint else None
                job_sequence = self._next_job_sequence()
                self._conn.execute(
                    "INSERT INTO connector_sync_jobs "
                    "(job_id,tenant_id,kb_id,connection_id,connector_type,status,start_cursor,cursor,"
                    "created_at,updated_at,replay_of,job_sequence,connection_revision,credential_id,"
                    "credential_revision) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        replay_id,
                        tenant_id,
                        kb_id,
                        connection_id,
                        connector_type,
                        SYNC_PENDING,
                        cursor,
                        cursor,
                        now,
                        now,
                        job_id,
                        job_sequence,
                        connection_revision,
                        credential,
                        credential_revision,
                    ),
                )
                self._project_health_locked(replay_id)
                self._conn.execute("COMMIT")
            except Exception:
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")
                raise
        return self.get(replay_id) or {}

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_JOB_SELECT} FROM connector_sync_jobs WHERE job_id=?",
                (job_id,),
            ).fetchone()
        return self._row(row) if row else None

    @staticmethod
    def _credential_snapshot(
        credential_id: str | None, credential_revision: int
    ) -> tuple[str | None, int]:
        if credential_id is None:
            if credential_revision != 0:
                raise ValueError("credential_revision requires credential_id")
            return None, 0
        credential = str(credential_id).strip()
        if not credential:
            raise ValueError("credential_id must be non-empty")
        if type(credential_revision) is not int or credential_revision < 1:
            raise ValueError("credential_revision must be a positive integer")
        return credential, credential_revision

    def list_jobs(
        self,
        tenant_id: str,
        kb_id: str,
        *,
        connection_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        clause = " AND connection_id=?" if connection_id else ""
        params: tuple[Any, ...] = (
            (tenant_id, kb_id, connection_id, limit)
            if connection_id
            else (tenant_id, kb_id, limit)
        )
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_JOB_SELECT} FROM connector_sync_jobs WHERE tenant_id=? AND kb_id=?"
                + clause
                + " ORDER BY job_sequence DESC LIMIT ?",
                params,
            ).fetchall()
        return [self._row(row) for row in rows]

    def recoverable(
        self, *, limit: int = 1000, after_sequence: int = 0
    ) -> list[dict[str, Any]]:
        """Return jobs that a restarted worker may acquire now or after retry_at."""

        if not 1 <= limit <= 10_000:
            raise ValueError("limit must be between 1 and 10000")
        if type(after_sequence) is not int or after_sequence < 0:
            raise ValueError("after_sequence must be a non-negative integer")
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_JOB_SELECT} FROM connector_sync_jobs WHERE status IN (?,?,?,?) "
                "AND job_sequence>? "
                "ORDER BY job_sequence LIMIT ?",
                (
                    SYNC_PENDING,
                    SYNC_RUNNING,
                    SYNC_COMMITTING,
                    SYNC_RETRY_WAIT,
                    after_sequence,
                    limit,
                ),
            ).fetchall()
        return [self._row(row) for row in rows]

    def latest_terminal_jobs(self) -> list[dict[str, Any]]:
        """Return the latest durable terminal job for observer reconciliation."""

        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_JOB_SELECT} FROM connector_sync_jobs jobs "
                "WHERE jobs.status IN (?,?,?,?) AND NOT EXISTS ("
                "SELECT 1 FROM connector_sync_jobs newer WHERE "
                "newer.tenant_id=jobs.tenant_id AND newer.kb_id=jobs.kb_id "
                "AND newer.connection_id=jobs.connection_id "
                "AND newer.job_sequence>jobs.job_sequence) ORDER BY jobs.job_sequence",
                tuple(SYNC_TERMINAL),
            ).fetchall()
        return [self._row(row) for row in rows]

    def acquire(
        self, job_id: str, *, lease_seconds: float
    ) -> tuple[dict[str, Any], str]:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        now = self._clock()
        token = secrets.token_urlsafe(24)
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT status,lease_expires_at,cancel_requested,retry_at,start_cursor "
                    "FROM connector_sync_jobs WHERE job_id=?",
                    (job_id,),
                ).fetchone()
                if row is None:
                    raise KeyError(job_id)
                status, lease_expires_at, cancel_requested, retry_at, start_cursor = row
                if cancel_requested:
                    self._conn.execute(
                        "UPDATE connector_sync_jobs SET status=?,finished_at=?,updated_at=?,revision=revision+1 "
                        "WHERE job_id=?",
                        (SYNC_CANCELLED, now, now, job_id),
                    )
                    self._project_health_locked(job_id)
                    self._conn.execute("COMMIT")
                    raise StaleSyncLease("sync job was cancelled before acquisition")
                runnable = (
                    status == SYNC_PENDING
                    or (
                        status == SYNC_RETRY_WAIT
                        and (retry_at is None or retry_at <= now)
                    )
                    or (
                        status == SYNC_RUNNING
                        and (lease_expires_at is None or lease_expires_at <= now)
                    )
                    or (
                        status == SYNC_COMMITTING
                        and (retry_at is None or retry_at <= now)
                        and (lease_expires_at is None or lease_expires_at <= now)
                    )
                )
                if not runnable:
                    raise StaleSyncLease(
                        f"sync job is not acquirable from status {status}"
                    )
                if status == SYNC_COMMITTING:
                    self._conn.execute(
                        "UPDATE connector_sync_jobs SET lease_token=?,lease_expires_at=?,"
                        "attempt=attempt+1,attempt_started_at=?,updated_at=?,retry_at=NULL,"
                        "error_code=NULL,error_message=NULL,health_duration_seconds=NULL,"
                        "revision=revision+1 WHERE job_id=?",
                        (token, now + lease_seconds, now, now, job_id),
                    )
                else:
                    # A restarted attempt replays from the last successful-sync
                    # cursor. Its predecessor may have rolled back staged data,
                    # so resuming a page cursor would silently omit documents.
                    self._conn.execute(
                        "UPDATE connector_sync_jobs SET status=?,lease_token=?,lease_expires_at=?,"
                        "attempt=attempt+1,started_at=COALESCE(started_at,?),attempt_started_at=?,"
                        "updated_at=?,retry_at=NULL,health_duration_seconds=NULL,"
                        "cursor=?,pages_processed=0,documents_seen=0,documents_fetched=0,"
                        "deleted_seen=0,bytes_fetched=0,error_code=NULL,error_message=NULL,"
                        "revision=revision+1 WHERE job_id=?",
                        (
                            SYNC_RUNNING,
                            token,
                            now + lease_seconds,
                            now,
                            now,
                            now,
                            start_cursor,
                            job_id,
                        ),
                    )
                self._project_health_locked(job_id)
                self._conn.execute("COMMIT")
            except Exception:
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")
                raise
        return self.get(job_id) or {}, token

    def heartbeat(self, job_id: str, token: str, *, lease_seconds: float) -> None:
        now = self._clock()
        with self._lock:
            updated = self._conn.execute(
                "UPDATE connector_sync_jobs SET lease_expires_at=?,updated_at=?,revision=revision+1 "
                "WHERE job_id=? AND status IN (?,?) AND lease_token=? AND lease_expires_at>?",
                (
                    now + lease_seconds,
                    now,
                    job_id,
                    SYNC_RUNNING,
                    SYNC_COMMITTING,
                    token,
                    now,
                ),
            ).rowcount
        if updated != 1:
            raise StaleSyncLease("sync lease is stale")

    def checkpoint(
        self,
        job_id: str,
        token: str,
        *,
        cursor: str | None,
        counters: dict[str, int],
        lease_seconds: float,
    ) -> None:
        now = self._clock()
        allowed = {
            "pages_processed",
            "documents_seen",
            "documents_fetched",
            "deleted_seen",
            "bytes_fetched",
        }
        if set(counters) != allowed or any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in counters.values()
        ):
            raise ValueError("checkpoint counters are incomplete or invalid")
        with self._lock:
            updated = self._conn.execute(
                "UPDATE connector_sync_jobs SET cursor=?,pages_processed=?,documents_seen=?,documents_fetched=?,"
                "deleted_seen=?,bytes_fetched=?,lease_expires_at=?,updated_at=?,revision=revision+1 "
                "WHERE job_id=? AND status=? AND lease_token=? AND lease_expires_at>?",
                (
                    cursor,
                    counters["pages_processed"],
                    counters["documents_seen"],
                    counters["documents_fetched"],
                    counters["deleted_seen"],
                    counters["bytes_fetched"],
                    now + lease_seconds,
                    now,
                    job_id,
                    SYNC_RUNNING,
                    token,
                    now,
                ),
            ).rowcount
        if updated != 1:
            raise StaleSyncLease("sync lease is stale")

    def cancellation_requested(self, job_id: str, token: str) -> bool:
        now = self._clock()
        with self._lock:
            row = self._conn.execute(
                "SELECT cancel_requested FROM connector_sync_jobs "
                "WHERE job_id=? AND status=? AND lease_token=? AND lease_expires_at>?",
                (job_id, SYNC_RUNNING, token, now),
            ).fetchone()
        if row is None:
            raise StaleSyncLease("sync lease is stale")
        return bool(row[0])

    def request_cancel(self, job_id: str) -> dict[str, Any]:
        now = self._clock()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT status,lease_expires_at FROM connector_sync_jobs WHERE job_id=?",
                    (job_id,),
                ).fetchone()
                if row is None:
                    raise KeyError(job_id)
                status = str(row[0])
                if status in {SYNC_PENDING, SYNC_RETRY_WAIT}:
                    self._conn.execute(
                        "UPDATE connector_sync_jobs SET cancel_requested=1,status=?,"
                        "finished_at=?,updated_at=?,revision=revision+1 "
                        "WHERE job_id=? AND status=?",
                        (SYNC_CANCELLED, now, now, job_id, status),
                    )
                elif status == SYNC_RUNNING:
                    if row[1] is None or float(row[1]) <= now:
                        self._conn.execute(
                            "UPDATE connector_sync_jobs SET cancel_requested=1,status=?,"
                            "lease_token=NULL,lease_expires_at=NULL,finished_at=?,updated_at=?,"
                            "revision=revision+1 WHERE job_id=? AND status=?",
                            (SYNC_CANCELLED, now, now, job_id, SYNC_RUNNING),
                        )
                    else:
                        self._conn.execute(
                            "UPDATE connector_sync_jobs SET cancel_requested=1,updated_at=?,"
                            "revision=revision+1 WHERE job_id=? AND status=?",
                            (now, job_id, SYNC_RUNNING),
                        )
                # A committing job already crossed the visibility authority
                # boundary. Terminal and committing states are immutable here.
                self._project_health_locked(job_id)
                self._conn.execute("COMMIT")
            except Exception:
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")
                raise
        return self.get(job_id) or {}

    def cancel_connection(
        self, tenant_id: str, kb_id: str, connection_id: str
    ) -> dict[str, int]:
        """Revoke every pre-commit job and its persistent schedule atomically."""

        now = self._clock()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                committing = int(
                    self._conn.execute(
                        "SELECT COUNT(*) FROM connector_sync_jobs WHERE tenant_id=? "
                        "AND kb_id=? AND connection_id=? AND status=?",
                        (tenant_id, kb_id, connection_id, SYNC_COMMITTING),
                    ).fetchone()[0]
                )
                terminalized = 0
                running = 0
                if not committing:
                    terminalized = self._conn.execute(
                        "UPDATE connector_sync_jobs SET cancel_requested=1,status=?,"
                        "finished_at=?,updated_at=?,revision=revision+1 "
                        "WHERE tenant_id=? AND kb_id=? AND connection_id=? "
                        "AND (status IN (?,?) OR (status=? AND "
                        "(lease_expires_at IS NULL OR lease_expires_at<=?)))",
                        (
                            SYNC_CANCELLED,
                            now,
                            now,
                            tenant_id,
                            kb_id,
                            connection_id,
                            SYNC_PENDING,
                            SYNC_RETRY_WAIT,
                            SYNC_RUNNING,
                            now,
                        ),
                    ).rowcount
                    running = self._conn.execute(
                        "UPDATE connector_sync_jobs SET cancel_requested=1,updated_at=?,"
                        "revision=revision+1 WHERE tenant_id=? AND kb_id=? AND connection_id=? "
                        "AND status=? AND cancel_requested=0",
                        (now, tenant_id, kb_id, connection_id, SYNC_RUNNING),
                    ).rowcount
                    self._conn.execute(
                        "UPDATE connector_sync_health SET schedule_seconds=NULL,next_run_at=NULL,"
                        "updated_at=? WHERE tenant_id=? AND kb_id=? AND connection_id=?",
                        (now, tenant_id, kb_id, connection_id),
                    )
                    latest = self._conn.execute(
                        "SELECT job_id FROM connector_sync_jobs WHERE tenant_id=? AND kb_id=? "
                        "AND connection_id=? ORDER BY job_sequence DESC LIMIT 1",
                        (tenant_id, kb_id, connection_id),
                    ).fetchone()
                    if latest is not None:
                        self._project_health_locked(str(latest[0]))
                self._conn.execute("COMMIT")
            except Exception:
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")
                raise
        return {
            "cancelled": int(terminalized + running),
            "committing": committing,
        }

    def scope_activity(self, tenant_id: str, kb_id: str) -> dict[str, int]:
        """Return authoritative active-state counts for KB teardown."""

        tenant = str(tenant_id or "").strip()
        knowledge_base = str(kb_id or "").strip()
        if not tenant or not knowledge_base:
            raise ValueError("sync scope is required")
        counts = {
            SYNC_PENDING: 0,
            SYNC_RUNNING: 0,
            SYNC_COMMITTING: 0,
            SYNC_RETRY_WAIT: 0,
        }
        with self._lock:
            rows = self._conn.execute(
                "SELECT status,COUNT(*) FROM connector_sync_jobs "
                "WHERE tenant_id=? AND kb_id=? AND status IN (?,?,?,?) GROUP BY status",
                (
                    tenant,
                    knowledge_base,
                    SYNC_PENDING,
                    SYNC_RUNNING,
                    SYNC_COMMITTING,
                    SYNC_RETRY_WAIT,
                ),
            ).fetchall()
        for status, count in rows:
            counts[str(status)] = int(count)
        counts["total"] = sum(counts.values())
        return counts

    def connection_activity(
        self, tenant_id: str, kb_id: str, connection_id: str
    ) -> dict[str, int]:
        """Return exact active-state counts for one connection teardown."""

        tenant = str(tenant_id or "").strip()
        knowledge_base = str(kb_id or "").strip()
        connection = str(connection_id or "").strip()
        if not tenant or not knowledge_base or not connection:
            raise ValueError("sync connection scope is required")
        counts = {
            SYNC_PENDING: 0,
            SYNC_RUNNING: 0,
            SYNC_COMMITTING: 0,
            SYNC_RETRY_WAIT: 0,
        }
        with self._lock:
            rows = self._conn.execute(
                "SELECT status,COUNT(*) FROM connector_sync_jobs "
                "WHERE tenant_id=? AND kb_id=? AND connection_id=? "
                "AND status IN (?,?,?,?) GROUP BY status",
                (
                    tenant,
                    knowledge_base,
                    connection,
                    SYNC_PENDING,
                    SYNC_RUNNING,
                    SYNC_COMMITTING,
                    SYNC_RETRY_WAIT,
                ),
            ).fetchall()
        for status, count in rows:
            counts[str(status)] = int(count)
        counts["total"] = sum(counts.values())
        return counts

    def connection_job_ids(
        self, tenant_id: str, kb_id: str, connection_id: str
    ) -> tuple[str, ...]:
        """Return exact durable job identities used to clean private work paths."""

        tenant = str(tenant_id or "").strip()
        knowledge_base = str(kb_id or "").strip()
        connection = str(connection_id or "").strip()
        if not tenant or not knowledge_base or not connection:
            raise ValueError("sync connection scope is required")
        with self._lock:
            rows = self._conn.execute(
                "SELECT job_id FROM connector_sync_jobs "
                "WHERE tenant_id=? AND kb_id=? AND connection_id=? "
                "ORDER BY job_sequence",
                (tenant, knowledge_base, connection),
            ).fetchall()
        return tuple(str(row[0]) for row in rows)

    def retire_connection(
        self, tenant_id: str, kb_id: str, connection_id: str
    ) -> dict[str, int]:
        """Remove live projections after one connection is durably quiescent.

        Terminal job rows remain as the bounded audit ledger and are pruned by
        normal retention maintenance.
        """

        tenant = str(tenant_id or "").strip()
        knowledge_base = str(kb_id or "").strip()
        connection = str(connection_id or "").strip()
        if not tenant or not knowledge_base or not connection:
            raise ValueError("sync connection scope is required")
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                active = int(
                    self._conn.execute(
                        "SELECT COUNT(*) FROM connector_sync_jobs WHERE tenant_id=? "
                        "AND kb_id=? AND connection_id=? AND status IN (?,?,?,?)",
                        (
                            tenant,
                            knowledge_base,
                            connection,
                            SYNC_PENDING,
                            SYNC_RUNNING,
                            SYNC_COMMITTING,
                            SYNC_RETRY_WAIT,
                        ),
                    ).fetchone()[0]
                )
                if active:
                    raise ValueError("connection still has active sync jobs")
                checkpoints = self._conn.execute(
                    "DELETE FROM connector_sync_checkpoints WHERE tenant_id=? "
                    "AND kb_id=? AND connection_id=?",
                    (tenant, knowledge_base, connection),
                ).rowcount
                health = self._conn.execute(
                    "DELETE FROM connector_sync_health WHERE tenant_id=? "
                    "AND kb_id=? AND connection_id=?",
                    (tenant, knowledge_base, connection),
                ).rowcount
                self._conn.execute("COMMIT")
            except Exception:
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")
                raise
        return {"checkpoints": int(checkpoints), "health": int(health)}

    def cancel_scope(self, tenant_id: str, kb_id: str) -> dict[str, int]:
        """Atomically fence a KB if no job crossed the commit boundary."""

        tenant = str(tenant_id or "").strip()
        knowledge_base = str(kb_id or "").strip()
        if not tenant or not knowledge_base:
            raise ValueError("sync scope is required")
        now = self._clock()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                committing = int(
                    self._conn.execute(
                        "SELECT COUNT(*) FROM connector_sync_jobs WHERE tenant_id=? "
                        "AND kb_id=? AND status=?",
                        (tenant, knowledge_base, SYNC_COMMITTING),
                    ).fetchone()[0]
                )
                if committing:
                    self._conn.execute("COMMIT")
                    return {"cancelled": 0, "committing": committing}
                terminalized = self._conn.execute(
                    "UPDATE connector_sync_jobs SET cancel_requested=1,status=?,"
                    "finished_at=?,updated_at=?,revision=revision+1 "
                    "WHERE tenant_id=? AND kb_id=? AND "
                    "(status IN (?,?) OR (status=? AND "
                    "(lease_expires_at IS NULL OR lease_expires_at<=?)))",
                    (
                        SYNC_CANCELLED,
                        now,
                        now,
                        tenant,
                        knowledge_base,
                        SYNC_PENDING,
                        SYNC_RETRY_WAIT,
                        SYNC_RUNNING,
                        now,
                    ),
                ).rowcount
                running = self._conn.execute(
                    "UPDATE connector_sync_jobs SET cancel_requested=1,updated_at=?,"
                    "revision=revision+1 WHERE tenant_id=? AND kb_id=? "
                    "AND status=? AND cancel_requested=0",
                    (now, tenant, knowledge_base, SYNC_RUNNING),
                ).rowcount
                self._conn.execute(
                    "UPDATE connector_sync_health SET schedule_seconds=NULL,next_run_at=NULL,"
                    "updated_at=? WHERE tenant_id=? AND kb_id=?",
                    (now, tenant, knowledge_base),
                )
                latest_jobs = self._conn.execute(
                    "SELECT jobs.job_id FROM connector_sync_jobs jobs WHERE jobs.tenant_id=? "
                    "AND jobs.kb_id=? AND NOT EXISTS (SELECT 1 FROM connector_sync_jobs newer "
                    "WHERE newer.tenant_id=jobs.tenant_id AND newer.kb_id=jobs.kb_id "
                    "AND newer.connection_id=jobs.connection_id "
                    "AND newer.job_sequence>jobs.job_sequence)",
                    (tenant, knowledge_base),
                ).fetchall()
                for (latest_job_id,) in latest_jobs:
                    self._project_health_locked(str(latest_job_id))
                self._conn.execute("COMMIT")
            except Exception:
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")
                raise
        return {
            "cancelled": int(terminalized + running),
            "committing": 0,
        }

    def delete_scope(self, tenant_id: str, kb_id: str) -> dict[str, int]:
        """Delete terminal sync history/checkpoints after KB data is fenced."""

        tenant = str(tenant_id or "").strip()
        knowledge_base = str(kb_id or "").strip()
        if not tenant or not knowledge_base:
            raise ValueError("sync scope is required")
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                active = int(
                    self._conn.execute(
                        "SELECT COUNT(*) FROM connector_sync_jobs WHERE tenant_id=? "
                        "AND kb_id=? AND status IN (?,?,?,?)",
                        (
                            tenant,
                            knowledge_base,
                            SYNC_PENDING,
                            SYNC_RUNNING,
                            SYNC_COMMITTING,
                            SYNC_RETRY_WAIT,
                        ),
                    ).fetchone()[0]
                )
                if active:
                    raise ValueError("knowledge base still has active sync jobs")
                jobs = self._conn.execute(
                    "DELETE FROM connector_sync_jobs WHERE tenant_id=? AND kb_id=?",
                    (tenant, knowledge_base),
                ).rowcount
                checkpoints = self._conn.execute(
                    "DELETE FROM connector_sync_checkpoints WHERE tenant_id=? AND kb_id=?",
                    (tenant, knowledge_base),
                ).rowcount
                health = self._conn.execute(
                    "DELETE FROM connector_sync_health WHERE tenant_id=? AND kb_id=?",
                    (tenant, knowledge_base),
                ).rowcount
                self._conn.execute("COMMIT")
            except Exception:
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")
                raise
        return {
            "jobs": int(jobs),
            "checkpoints": int(checkpoints),
            "health": int(health),
        }

    def prepare_commit(self, job_id: str, token: str) -> dict[str, Any]:
        """Cross the cancellation boundary before making sink data visible."""

        now = self._clock()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT status,lease_token,lease_expires_at,cancel_requested "
                    "FROM connector_sync_jobs WHERE job_id=?",
                    (job_id,),
                ).fetchone()
                if row is None:
                    raise KeyError(job_id)
                if bool(row[3]):
                    raise SyncCancelled("sync was cancelled before commit")
                if row[0] != SYNC_RUNNING or row[1] != token or row[2] <= now:
                    raise StaleSyncLease("sync lease is stale")
                self._conn.execute(
                    "UPDATE connector_sync_jobs SET status=?,updated_at=?,"
                    "revision=revision+1 WHERE job_id=?",
                    (SYNC_COMMITTING, now, job_id),
                )
                self._project_health_locked(job_id)
                self._conn.execute("COMMIT")
            except Exception:
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")
                raise
        return self.get(job_id) or {}

    def complete(
        self, job_id: str, token: str, *, cursor: str | None, counters: dict[str, int]
    ) -> dict[str, Any]:
        now = self._clock()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                updated = self._conn.execute(
                    "UPDATE connector_sync_jobs SET status=?,cursor=?,lease_token=NULL,lease_expires_at=NULL,"
                    "pages_processed=?,documents_seen=?,documents_fetched=?,deleted_seen=?,bytes_fetched=?,"
                    "updated_at=?,finished_at=?,cleanup_pending=1,revision=revision+1 "
                    "WHERE job_id=? AND status=? AND lease_token=?",
                    (
                        SYNC_SUCCEEDED,
                        cursor,
                        counters["pages_processed"],
                        counters["documents_seen"],
                        counters["documents_fetched"],
                        counters["deleted_seen"],
                        counters["bytes_fetched"],
                        now,
                        now,
                        job_id,
                        SYNC_COMMITTING,
                        token,
                    ),
                ).rowcount
                if updated != 1:
                    raise StaleSyncLease(
                        "sync lease is stale or cancellation was requested"
                    )
                job = self._conn.execute(
                    "SELECT tenant_id,kb_id,connection_id FROM connector_sync_jobs WHERE job_id=?",
                    (job_id,),
                ).fetchone()
                self._conn.execute(
                    "INSERT INTO connector_sync_checkpoints "
                    "(tenant_id,kb_id,connection_id,cursor,last_job_id,last_success_at,counters_json) "
                    "VALUES (?,?,?,?,?,?,?) ON CONFLICT(tenant_id,kb_id,connection_id) DO UPDATE SET "
                    "cursor=excluded.cursor,last_job_id=excluded.last_job_id,last_success_at=excluded.last_success_at,"
                    "counters_json=excluded.counters_json",
                    (*job, cursor, job_id, now, json.dumps(counters, sort_keys=True)),
                )
                self._project_health_locked(job_id)
                self._conn.execute("COMMIT")
            except Exception:
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")
                raise
        return self.get(job_id) or {}

    def mark_cleanup_complete(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            updated = self._conn.execute(
                "UPDATE connector_sync_jobs SET cleanup_pending=0,revision=revision+1 "
                "WHERE job_id=? AND status=? AND cleanup_pending=1",
                (job_id, SYNC_SUCCEEDED),
            ).rowcount
            if updated == 0:
                row = self._conn.execute(
                    "SELECT status FROM connector_sync_jobs WHERE job_id=?",
                    (job_id,),
                ).fetchone()
                if row is None:
                    raise KeyError(job_id)
                if row[0] != SYNC_SUCCEEDED:
                    raise ValueError("only succeeded jobs have terminal cleanup")
        return self.get(job_id) or {}

    def cleanup_pending(
        self,
        *,
        limit: int = 1000,
        after_sequence: int = 0,
        tenant_id: str | None = None,
        kb_id: str | None = None,
    ) -> list[dict[str, Any]]:
        if not 1 <= limit <= 10_000:
            raise ValueError("limit must be between 1 and 10000")
        if type(after_sequence) is not int or after_sequence < 0:
            raise ValueError("after_sequence must be a non-negative integer")
        if (tenant_id is None) != (kb_id is None):
            raise ValueError("cleanup scope requires tenant_id and kb_id")
        scope_clause = ""
        params: list[Any] = [SYNC_SUCCEEDED, after_sequence]
        if tenant_id is not None and kb_id is not None:
            scope_clause = " AND tenant_id=? AND kb_id=?"
            params.extend((tenant_id, kb_id))
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_JOB_SELECT} FROM connector_sync_jobs WHERE status=? "
                "AND cleanup_pending=1 AND job_sequence>? " + scope_clause + " "
                "ORDER BY job_sequence LIMIT ?",
                tuple(params),
            ).fetchall()
        return [self._row(row) for row in rows]

    def prune_terminal_jobs(self, *, older_than: float, limit: int = 1000) -> int:
        """Boundedly prune clean terminal leaves while retaining replay lineage."""

        if (
            isinstance(older_than, bool)
            or not math.isfinite(float(older_than))
            or float(older_than) < 0
        ):
            raise ValueError("older_than must be a finite non-negative timestamp")
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                deleted = self._conn.execute(
                    "DELETE FROM connector_sync_jobs WHERE job_id IN ("
                    "SELECT candidate.job_id FROM connector_sync_jobs candidate "
                    "WHERE candidate.status IN (?,?,?,?) "
                    "AND candidate.cleanup_pending=0 AND candidate.finished_at<? "
                    "AND NOT EXISTS (SELECT 1 FROM connector_sync_jobs child "
                    "WHERE child.replay_of=candidate.job_id) "
                    "AND NOT EXISTS (SELECT 1 FROM connector_sync_health health "
                    "WHERE health.last_job_id=candidate.job_id) "
                    "AND NOT EXISTS (SELECT 1 FROM connector_sync_checkpoints checkpoint "
                    "WHERE checkpoint.last_job_id=candidate.job_id) "
                    "ORDER BY candidate.job_sequence LIMIT ?)",
                    (*tuple(SYNC_TERMINAL), float(older_than), limit),
                ).rowcount
                self._conn.execute("COMMIT")
            except Exception:
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")
                raise
        return int(deleted)

    def fail(
        self,
        job_id: str,
        token: str,
        *,
        error_code: str,
        error_message: str,
        retryable: bool,
        retry_delay_seconds: float = 0,
        dead_letter: bool = False,
        preserve_committing: bool = False,
    ) -> dict[str, Any]:
        if preserve_committing and (not retryable or dead_letter):
            raise ValueError("a preserved commit failure must be retryable")
        if retryable and dead_letter:
            raise ValueError("a dead-letter failure cannot also be retryable")
        now = self._clock()
        status = (
            SYNC_COMMITTING
            if preserve_committing
            else SYNC_RETRY_WAIT
            if retryable
            else SYNC_DEAD_LETTER
            if dead_letter
            else SYNC_FAILED
        )
        retry_at = now + max(0.0, retry_delay_seconds) if retryable else None
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                previous = self._conn.execute(
                    "SELECT health_failure_recorded FROM connector_sync_jobs WHERE job_id=?",
                    (job_id,),
                ).fetchone()
                if previous is None:
                    raise KeyError(job_id)
                failure_state = _is_failure_state(status, retry_at)
                count_failure = bool(failure_state and not bool(previous[0]))
                updated = self._conn.execute(
                    "UPDATE connector_sync_jobs SET status=?,lease_token=NULL,lease_expires_at=NULL,error_code=?,"
                    "error_message=?,retry_at=?,updated_at=?,finished_at=?,"
                    "health_failure_recorded=CASE WHEN ? THEN 1 ELSE health_failure_recorded END,"
                    "revision=revision+1 WHERE job_id=? AND status IN (?,?) AND lease_token=? "
                    "AND lease_expires_at>?",
                    (
                        status,
                        str(error_code)[:128],
                        str(error_message)[:1000],
                        retry_at,
                        now,
                        None if retryable else now,
                        int(failure_state),
                        job_id,
                        SYNC_COMMITTING if preserve_committing else SYNC_RUNNING,
                        SYNC_COMMITTING,
                        token,
                        now,
                    ),
                ).rowcount
                if updated != 1:
                    raise StaleSyncLease("sync lease is stale")
                self._project_health_locked(job_id, count_failure=count_failure)
                self._conn.execute("COMMIT")
            except Exception:
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")
                raise
        return self.get(job_id) or {}

    def backlog_size(
        self,
        tenant_id: str,
        kb_id: str,
        *,
        connection_id: str | None = None,
    ) -> int:
        clause = " AND connection_id=?" if connection_id else ""
        params: tuple[Any, ...] = (
            (
                SYNC_PENDING,
                SYNC_RUNNING,
                SYNC_COMMITTING,
                SYNC_RETRY_WAIT,
                tenant_id,
                kb_id,
                connection_id,
            )
            if connection_id
            else (
                SYNC_PENDING,
                SYNC_RUNNING,
                SYNC_COMMITTING,
                SYNC_RETRY_WAIT,
                tenant_id,
                kb_id,
            )
        )
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM connector_sync_jobs WHERE status IN (?,?,?,?) "
                "AND tenant_id=? AND kb_id=?" + clause,
                params,
            ).fetchone()
        return int(row[0])

    def ensure_schedule(
        self,
        *,
        tenant_id: str,
        kb_id: str,
        connection_id: str,
        schedule_seconds: int,
    ) -> dict[str, Any]:
        if (
            type(schedule_seconds) is not int
            or not 60 <= schedule_seconds <= 31_536_000
        ):
            raise ValueError("schedule_seconds must be between 60 and 31536000")
        now = self._clock()
        with self._lock:
            self._conn.execute(
                "INSERT INTO connector_sync_health "
                "(tenant_id,kb_id,connection_id,schedule_seconds,next_run_at,updated_at) "
                "VALUES (?,?,?,?,?,?) ON CONFLICT(tenant_id,kb_id,connection_id) DO UPDATE SET "
                "next_run_at=CASE WHEN connector_sync_health.schedule_seconds IS NOT excluded.schedule_seconds "
                "THEN excluded.next_run_at ELSE connector_sync_health.next_run_at END,"
                "schedule_seconds=excluded.schedule_seconds,updated_at=excluded.updated_at",
                (
                    tenant_id,
                    kb_id,
                    connection_id,
                    schedule_seconds,
                    now + schedule_seconds,
                    now,
                ),
            )
        return self.health_snapshot(tenant_id, kb_id, connection_id)

    def clear_schedule(
        self, tenant_id: str, kb_id: str, connection_id: str
    ) -> dict[str, Any]:
        now = self._clock()
        with self._lock:
            self._conn.execute(
                "INSERT INTO connector_sync_health "
                "(tenant_id,kb_id,connection_id,schedule_seconds,next_run_at,updated_at) "
                "VALUES (?,?,?,NULL,NULL,?) ON CONFLICT(tenant_id,kb_id,connection_id) DO UPDATE SET "
                "schedule_seconds=NULL,next_run_at=NULL,updated_at=excluded.updated_at",
                (tenant_id, kb_id, connection_id, now),
            )
        return self.health_snapshot(tenant_id, kb_id, connection_id)

    def set_next_run(
        self,
        tenant_id: str,
        kb_id: str,
        connection_id: str,
        next_run_at: float | None,
    ) -> dict[str, Any]:
        if next_run_at is not None and next_run_at < 0:
            raise ValueError("next_run_at must be non-negative")
        now = self._clock()
        with self._lock:
            self._conn.execute(
                "INSERT INTO connector_sync_health "
                "(tenant_id,kb_id,connection_id,next_run_at,updated_at) VALUES (?,?,?,?,?) "
                "ON CONFLICT(tenant_id,kb_id,connection_id) DO UPDATE SET "
                "next_run_at=excluded.next_run_at,updated_at=excluded.updated_at",
                (tenant_id, kb_id, connection_id, next_run_at, now),
            )
        return self.health_snapshot(tenant_id, kb_id, connection_id)

    def record_health(self, job_id: str, *, duration_seconds: float) -> dict[str, Any]:
        """Persist precise duration and idempotently project one ledger row."""

        if not math.isfinite(float(duration_seconds)) or duration_seconds < 0:
            raise ValueError("duration_seconds must be finite and non-negative")
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                scope = self._project_health_locked(
                    job_id, duration_seconds=duration_seconds
                )
                self._conn.execute("COMMIT")
            except Exception:
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")
                raise
        return self.health_snapshot(*scope)

    def health_snapshot(
        self, tenant_id: str, kb_id: str, connection_id: str
    ) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT schedule_seconds,next_run_at,health_status,last_job_id,last_job_status,"
                "last_started_at,last_success_at,last_failure_at,last_error_code,last_duration_seconds,"
                "consecutive_failures,updated_at FROM connector_sync_health "
                "WHERE tenant_id=? AND kb_id=? AND connection_id=?",
                (tenant_id, kb_id, connection_id),
            ).fetchone()
        values = row or (
            None,
            None,
            "unknown",
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            0,
            None,
        )
        keys = (
            "schedule_seconds",
            "next_run_at",
            "health_status",
            "last_job_id",
            "last_job_status",
            "last_started_at",
            "last_success_at",
            "last_failure_at",
            "last_error_code",
            "last_duration_seconds",
            "consecutive_failures",
            "updated_at",
        )
        return {
            "tenant_id": tenant_id,
            "kb_id": kb_id,
            "connection_id": connection_id,
            **dict(zip(keys, values, strict=True)),
            "backlog": self.backlog_size(tenant_id, kb_id, connection_id=connection_id),
        }

    def mark_cancelled(self, job_id: str, token: str) -> dict[str, Any]:
        now = self._clock()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                updated = self._conn.execute(
                    "UPDATE connector_sync_jobs SET status=?,lease_token=NULL,lease_expires_at=NULL,"
                    "cancel_requested=1,updated_at=?,finished_at=?,revision=revision+1 "
                    "WHERE job_id=? AND status=? AND lease_token=? AND lease_expires_at>?",
                    (SYNC_CANCELLED, now, now, job_id, SYNC_RUNNING, token, now),
                ).rowcount
                if updated != 1:
                    raise StaleSyncLease("sync lease is stale")
                self._project_health_locked(job_id)
                self._conn.execute("COMMIT")
            except Exception:
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")
                raise
        return self.get(job_id) or {}

    def checkpoint_for(
        self, tenant_id: str, kb_id: str, connection_id: str
    ) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT cursor,last_job_id,last_success_at,counters_json FROM connector_sync_checkpoints "
                "WHERE tenant_id=? AND kb_id=? AND connection_id=?",
                (tenant_id, kb_id, connection_id),
            ).fetchone()
        if row is None:
            return None
        return {
            "cursor": row[0],
            "last_job_id": row[1],
            "last_success_at": row[2],
            "counters": json.loads(row[3]),
        }

    @staticmethod
    def _row(row) -> dict[str, Any]:
        keys = (
            "job_id",
            "tenant_id",
            "kb_id",
            "connection_id",
            "connector_type",
            "status",
            "start_cursor",
            "cursor",
            "lease_token",
            "lease_expires_at",
            "cancel_requested",
            "attempt",
            "pages_processed",
            "documents_seen",
            "documents_fetched",
            "deleted_seen",
            "bytes_fetched",
            "error_code",
            "error_message",
            "retry_at",
            "created_at",
            "started_at",
            "updated_at",
            "finished_at",
            "revision",
            "replay_of",
            "job_sequence",
            "connection_revision",
            "health_duration_seconds",
            "health_failure_recorded",
            "credential_id",
            "credential_revision",
            "cleanup_pending",
            "attempt_started_at",
        )
        result = dict(zip(keys, row, strict=True))
        result["cancel_requested"] = bool(result["cancel_requested"])
        result["health_failure_recorded"] = bool(result["health_failure_recorded"])
        result["cleanup_pending"] = bool(result["cleanup_pending"])
        # Lease tokens are authority, never part of a public/store read model.
        result.pop("lease_token", None)
        return result
