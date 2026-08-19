from __future__ import annotations

import json
import secrets
import time
from threading import RLock
from typing import Any, Callable
from uuid import uuid4

from cogdoc.api.persistence import connect_sqlite
from cogdoc.connectors.base import StaleSyncLease


SYNC_PENDING = "pending"
SYNC_RUNNING = "running"
SYNC_COMMITTING = "committing"
SYNC_RETRY_WAIT = "retry_wait"
SYNC_SUCCEEDED = "succeeded"
SYNC_FAILED = "failed"
SYNC_CANCELLED = "cancelled"
SYNC_TERMINAL = frozenset({SYNC_SUCCEEDED, SYNC_FAILED, SYNC_CANCELLED})


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
                revision INTEGER NOT NULL DEFAULT 0
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
            """
        )
        columns = {
            row[1]
            for row in self._conn.execute("PRAGMA table_info(connector_sync_jobs)")
        }
        if "start_cursor" not in columns:
            self._conn.execute(
                "ALTER TABLE connector_sync_jobs ADD COLUMN start_cursor TEXT"
            )
            self._conn.execute(
                "UPDATE connector_sync_jobs SET start_cursor=cursor "
                "WHERE start_cursor IS NULL"
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def create(
        self,
        *,
        tenant_id: str,
        kb_id: str,
        connection_id: str,
        connector_type: str,
        resume_cursor: str | None = None,
    ) -> dict[str, Any]:
        values = [
            str(value or "").strip()
            for value in (tenant_id, kb_id, connection_id, connector_type)
        ]
        if not all(values):
            raise ValueError("sync job scope and connector_type are required")
        values[3] = values[3].casefold()
        now = self._clock()
        job_id = f"sync-{uuid4().hex}"
        with self._lock:
            self._conn.execute(
                "INSERT INTO connector_sync_jobs "
                "(job_id,tenant_id,kb_id,connection_id,connector_type,status,start_cursor,cursor,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    job_id,
                    *values,
                    SYNC_PENDING,
                    resume_cursor,
                    resume_cursor,
                    now,
                    now,
                ),
            )
        return self.get(job_id) or {}

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT job_id,tenant_id,kb_id,connection_id,connector_type,status,start_cursor,cursor,lease_token,"
                "lease_expires_at,cancel_requested,attempt,pages_processed,documents_seen,documents_fetched,"
                "deleted_seen,bytes_fetched,error_code,error_message,retry_at,created_at,started_at,updated_at,"
                "finished_at,revision FROM connector_sync_jobs WHERE job_id=?",
                (job_id,),
            ).fetchone()
        return self._row(row) if row else None

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
                "SELECT job_id,tenant_id,kb_id,connection_id,connector_type,status,start_cursor,cursor,lease_token,"
                "lease_expires_at,cancel_requested,attempt,pages_processed,documents_seen,documents_fetched,"
                "deleted_seen,bytes_fetched,error_code,error_message,retry_at,created_at,started_at,updated_at,"
                "finished_at,revision FROM connector_sync_jobs WHERE tenant_id=? AND kb_id=?"
                + clause
                + " ORDER BY created_at DESC,job_id DESC LIMIT ?",
                params,
            ).fetchall()
        return [self._row(row) for row in rows]

    def recoverable(self, *, limit: int = 1000) -> list[dict[str, Any]]:
        """Return jobs that a restarted worker may acquire now or after retry_at."""

        if not 1 <= limit <= 10_000:
            raise ValueError("limit must be between 1 and 10000")
        with self._lock:
            rows = self._conn.execute(
                "SELECT job_id,tenant_id,kb_id,connection_id,connector_type,status,start_cursor,cursor,lease_token,"
                "lease_expires_at,cancel_requested,attempt,pages_processed,documents_seen,documents_fetched,"
                "deleted_seen,bytes_fetched,error_code,error_message,retry_at,created_at,started_at,updated_at,"
                "finished_at,revision FROM connector_sync_jobs WHERE status IN (?,?,?,?) "
                "ORDER BY created_at,job_id LIMIT ?",
                (SYNC_PENDING, SYNC_RUNNING, SYNC_COMMITTING, SYNC_RETRY_WAIT, limit),
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
                        "attempt=attempt+1,updated_at=?,revision=revision+1 WHERE job_id=?",
                        (token, now + lease_seconds, now, job_id),
                    )
                else:
                    # A restarted attempt replays from the last successful-sync
                    # cursor. Its predecessor may have rolled back staged data,
                    # so resuming a page cursor would silently omit documents.
                    self._conn.execute(
                        "UPDATE connector_sync_jobs SET status=?,lease_token=?,lease_expires_at=?,"
                        "attempt=attempt+1,started_at=COALESCE(started_at,?),updated_at=?,retry_at=NULL,"
                        "cursor=?,pages_processed=0,documents_seen=0,documents_fetched=0,"
                        "deleted_seen=0,bytes_fetched=0,error_code=NULL,error_message=NULL,"
                        "revision=revision+1 WHERE job_id=?",
                        (
                            SYNC_RUNNING,
                            token,
                            now + lease_seconds,
                            now,
                            now,
                            start_cursor,
                            job_id,
                        ),
                    )
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
            row = self._conn.execute(
                "SELECT status FROM connector_sync_jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            if row is None:
                raise KeyError(job_id)
            if row[0] not in SYNC_TERMINAL:
                if row[0] == SYNC_COMMITTING:
                    # The visibility commit already crossed its authority
                    # boundary. Cancellation cannot make its outcome ambiguous.
                    return self.get(job_id) or {}
                terminal = row[0] in {SYNC_PENDING, SYNC_RETRY_WAIT}
                self._conn.execute(
                    "UPDATE connector_sync_jobs SET cancel_requested=1,status=?,finished_at=?,updated_at=?,"
                    "revision=revision+1 WHERE job_id=?",
                    (
                        SYNC_CANCELLED if terminal else row[0],
                        now if terminal else None,
                        now,
                        job_id,
                    ),
                )
        return self.get(job_id) or {}

    def prepare_commit(self, job_id: str, token: str) -> dict[str, Any]:
        """Cross the cancellation boundary before making sink data visible."""

        now = self._clock()
        with self._lock:
            updated = self._conn.execute(
                "UPDATE connector_sync_jobs SET status=?,updated_at=?,revision=revision+1 "
                "WHERE job_id=? AND status=? AND lease_token=? AND lease_expires_at>? "
                "AND cancel_requested=0",
                (SYNC_COMMITTING, now, job_id, SYNC_RUNNING, token, now),
            ).rowcount
        if updated != 1:
            raise StaleSyncLease("sync lease is stale or cancellation was requested")
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
                    "updated_at=?,finished_at=?,revision=revision+1 "
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
                self._conn.execute("COMMIT")
            except Exception:
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")
                raise
        return self.get(job_id) or {}

    def fail(
        self,
        job_id: str,
        token: str,
        *,
        error_code: str,
        error_message: str,
        retryable: bool,
        retry_delay_seconds: float = 0,
    ) -> dict[str, Any]:
        now = self._clock()
        status = SYNC_RETRY_WAIT if retryable else SYNC_FAILED
        retry_at = now + max(0.0, retry_delay_seconds) if retryable else None
        with self._lock:
            updated = self._conn.execute(
                "UPDATE connector_sync_jobs SET status=?,lease_token=NULL,lease_expires_at=NULL,error_code=?,"
                "error_message=?,retry_at=?,updated_at=?,finished_at=?,revision=revision+1 "
                "WHERE job_id=? AND status IN (?,?) AND lease_token=? "
                "AND lease_expires_at>?",
                (
                    status,
                    str(error_code)[:128],
                    str(error_message)[:1000],
                    retry_at,
                    now,
                    None if retryable else now,
                    job_id,
                    SYNC_RUNNING,
                    SYNC_COMMITTING,
                    token,
                    now,
                ),
            ).rowcount
        if updated != 1:
            raise StaleSyncLease("sync lease is stale")
        return self.get(job_id) or {}

    def mark_cancelled(self, job_id: str, token: str) -> dict[str, Any]:
        now = self._clock()
        with self._lock:
            updated = self._conn.execute(
                "UPDATE connector_sync_jobs SET status=?,lease_token=NULL,lease_expires_at=NULL,"
                "cancel_requested=1,updated_at=?,finished_at=?,revision=revision+1 "
                "WHERE job_id=? AND status=? AND lease_token=? AND lease_expires_at>?",
                (SYNC_CANCELLED, now, now, job_id, SYNC_RUNNING, token, now),
            ).rowcount
        if updated != 1:
            raise StaleSyncLease("sync lease is stale")
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
        )
        result = dict(zip(keys, row, strict=True))
        result["cancel_requested"] = bool(result["cancel_requested"])
        # Lease tokens are authority, never part of a public/store read model.
        result.pop("lease_token", None)
        return result
