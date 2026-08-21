from __future__ import annotations

import json
import hashlib
import math
import secrets
import time
import uuid
from collections.abc import Mapping
from typing import Any, Callable, Final

from cogdoc.ha.storage import DatabaseBackend, execute_script


JOB_QUEUED: Final = "queued"
JOB_RUNNING: Final = "running"
JOB_RETRY_WAIT: Final = "retry_wait"
JOB_SUCCEEDED: Final = "succeeded"
JOB_FAILED: Final = "failed"
JOB_DEAD_LETTER: Final = "dead_letter"
JOB_CANCELLED: Final = "cancelled"
JOB_TERMINAL = frozenset({JOB_SUCCEEDED, JOB_FAILED, JOB_DEAD_LETTER, JOB_CANCELLED})
_PAYLOAD_MAX_BYTES = 1024 * 1024


class JobError(RuntimeError):
    pass


class StaleJobLease(JobError):
    pass


class JobConflict(JobError):
    pass


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


def _json(value: Any) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("job payload must be JSON serializable") from exc
    if len(encoded.encode("utf-8")) > _PAYLOAD_MAX_BYTES:
        raise ValueError("job payload exceeds 1 MiB")
    return encoded


def _column(row: Any, name: str, index: int) -> Any:
    return row.get(name) if isinstance(row, Mapping) else row[index]


class LeaseJobStore:
    """Durable multi-worker queue with fencing-token leases."""

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
                    sqlite="""CREATE TABLE IF NOT EXISTS ha_jobs (
                    job_id TEXT PRIMARY KEY, queue_name TEXT NOT NULL,
                    tenant_id TEXT NOT NULL, payload_json TEXT NOT NULL,
                    status TEXT NOT NULL, priority INTEGER NOT NULL,
                    available_at REAL NOT NULL, lease_owner TEXT, lease_token TEXT,
                    lease_expires_at REAL, cancel_requested INTEGER NOT NULL DEFAULT 0,
                    attempt INTEGER NOT NULL DEFAULT 0, max_attempts INTEGER NOT NULL,
                    result_json TEXT, error_code TEXT, idempotency_key TEXT,replay_of TEXT,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL, finished_at REAL,
                    revision INTEGER NOT NULL DEFAULT 1,
                    UNIQUE(queue_name,tenant_id,idempotency_key))""",
                    postgres="""CREATE TABLE IF NOT EXISTS ha_jobs (
                    job_id TEXT PRIMARY KEY, queue_name TEXT NOT NULL,
                    tenant_id TEXT NOT NULL, payload_json TEXT NOT NULL,
                    status TEXT NOT NULL, priority INTEGER NOT NULL,
                    available_at DOUBLE PRECISION NOT NULL, lease_owner TEXT, lease_token TEXT,
                    lease_expires_at DOUBLE PRECISION, cancel_requested INTEGER NOT NULL DEFAULT 0,
                    attempt INTEGER NOT NULL DEFAULT 0, max_attempts INTEGER NOT NULL,
                    result_json TEXT, error_code TEXT, idempotency_key TEXT,replay_of TEXT,
                    created_at DOUBLE PRECISION NOT NULL, updated_at DOUBLE PRECISION NOT NULL,
                    finished_at DOUBLE PRECISION, revision INTEGER NOT NULL DEFAULT 1,
                    UNIQUE(queue_name,tenant_id,idempotency_key))""",
                ),
                backend.sql(
                    sqlite="""CREATE TABLE IF NOT EXISTS ha_job_keys (
                    queue_name TEXT NOT NULL,tenant_id TEXT NOT NULL,idempotency_key TEXT NOT NULL,
                    job_id TEXT NOT NULL,fingerprint TEXT NOT NULL,terminal_status TEXT,
                    created_at REAL NOT NULL,PRIMARY KEY(queue_name,tenant_id,idempotency_key))""",
                    postgres="""CREATE TABLE IF NOT EXISTS ha_job_keys (
                    queue_name TEXT NOT NULL,tenant_id TEXT NOT NULL,idempotency_key TEXT NOT NULL,
                    job_id TEXT NOT NULL,fingerprint TEXT NOT NULL,terminal_status TEXT,
                    created_at DOUBLE PRECISION NOT NULL,
                    PRIMARY KEY(queue_name,tenant_id,idempotency_key))""",
                ),
                "CREATE INDEX IF NOT EXISTS idx_ha_jobs_claim ON ha_jobs(queue_name,status,available_at,priority,created_at)",
                "CREATE INDEX IF NOT EXISTS idx_ha_jobs_lease ON ha_jobs(status,lease_expires_at)",
                "CREATE INDEX IF NOT EXISTS idx_ha_jobs_tenant ON ha_jobs(tenant_id,created_at)",
            ],
        )

    @staticmethod
    def _row(row: Any | None) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        result["payload"] = json.loads(str(result.pop("payload_json")))
        raw_result = result.pop("result_json")
        result["result"] = None if raw_result is None else json.loads(str(raw_result))
        result["cancel_requested"] = bool(result["cancel_requested"])
        return result

    def enqueue(
        self,
        queue: str,
        tenant_id: str,
        payload: Any,
        *,
        priority: int = 0,
        max_attempts: int = 5,
        available_at: float | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        queue = _clean(queue, "queue", 128)
        tenant_id = _clean(tenant_id, "tenant_id")
        payload_json = _json(payload)
        if type(priority) is not int or not -1000 <= priority <= 1000:
            raise ValueError("priority must be between -1000 and 1000")
        if type(max_attempts) is not int or not 1 <= max_attempts <= 100:
            raise ValueError("max_attempts must be between 1 and 100")
        if idempotency_key is not None:
            idempotency_key = _clean(idempotency_key, "idempotency_key", 512)
        now = self._clock()
        ready = now if available_at is None else float(available_at)
        if not math.isfinite(ready) or ready < 0:
            raise ValueError("available_at is invalid")
        job_id = f"haj-{uuid.uuid4().hex}"
        placeholders = self.backend.sql(
            sqlite="?,?,?,?,?,?,?,?,?,?,?", postgres="%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s"
        )
        with self.backend.transaction(write=True) as connection:
            if idempotency_key is not None:
                marker = self.backend.sql(sqlite="?", postgres="%s")
                fingerprint = hashlib.sha256(payload_json.encode()).hexdigest()
                key_row = connection.execute(
                    f"SELECT job_id,fingerprint,terminal_status FROM ha_job_keys "
                    f"WHERE queue_name={marker} AND tenant_id={marker} "
                    f"AND idempotency_key={marker}",
                    (queue, tenant_id, idempotency_key),
                ).fetchone()
                if key_row is not None:
                    stored_fingerprint = str(_column(key_row, "fingerprint", 1))
                    if stored_fingerprint != fingerprint:
                        raise JobConflict(
                            "idempotency key was reused with a different payload"
                        )
                    existing_id = str(_column(key_row, "job_id", 0))
                    existing = connection.execute(
                        f"SELECT * FROM ha_jobs WHERE job_id={marker}", (existing_id,)
                    ).fetchone()
                    if existing is not None:
                        return self._row(existing) or {}
                    return {
                        "job_id": existing_id,
                        "queue_name": queue,
                        "tenant_id": tenant_id,
                        "payload": json.loads(payload_json),
                        "status": str(
                            _column(key_row, "terminal_status", 2) or "pruned"
                        ),
                        "idempotency_key": idempotency_key,
                        "result": None,
                        "replay_of": None,
                        "pruned": True,
                    }
                existing = connection.execute(
                    f"SELECT * FROM ha_jobs WHERE queue_name={marker} AND tenant_id={marker} AND idempotency_key={marker}",
                    (queue, tenant_id, idempotency_key),
                ).fetchone()
                if existing is not None:
                    current = self._row(existing)
                    assert current is not None
                    if current["payload"] != json.loads(payload_json):
                        raise JobConflict(
                            "idempotency key was reused with a different payload"
                        )
                    return current
            insert_prefix = self.backend.sql(
                sqlite="INSERT OR IGNORE", postgres="INSERT"
            )
            conflict_suffix = self.backend.sql(
                sqlite="",
                postgres=" ON CONFLICT(queue_name,tenant_id,idempotency_key) DO NOTHING",
            )
            inserted = connection.execute(
                f"{insert_prefix} INTO ha_jobs(job_id,queue_name,tenant_id,payload_json,status,priority,"
                "available_at,max_attempts,idempotency_key,created_at,updated_at) "
                f"VALUES({placeholders}){conflict_suffix}",
                (
                    job_id,
                    queue,
                    tenant_id,
                    payload_json,
                    JOB_QUEUED,
                    priority,
                    ready,
                    max_attempts,
                    idempotency_key,
                    now,
                    now,
                ),
            )
            marker = self.backend.sql(sqlite="?", postgres="%s")
            if inserted.rowcount == 1:
                if idempotency_key is not None:
                    key_placeholders = self.backend.sql(
                        sqlite="?,?,?,?,?,?,?", postgres="%s,%s,%s,%s,%s,%s,%s"
                    )
                    connection.execute(
                        "INSERT INTO ha_job_keys(queue_name,tenant_id,idempotency_key,job_id,"
                        f"fingerprint,terminal_status,created_at) VALUES({key_placeholders})",
                        (
                            queue,
                            tenant_id,
                            idempotency_key,
                            job_id,
                            hashlib.sha256(payload_json.encode()).hexdigest(),
                            None,
                            now,
                        ),
                    )
                row = connection.execute(
                    f"SELECT * FROM ha_jobs WHERE job_id={marker}", (job_id,)
                ).fetchone()
                return self._row(row) or {}
            if idempotency_key is None:  # pragma: no cover - UUID collision
                raise JobConflict("job identifier collision")
            row = connection.execute(
                f"SELECT * FROM ha_jobs WHERE queue_name={marker} AND tenant_id={marker} "
                f"AND idempotency_key={marker}",
                (queue, tenant_id, idempotency_key),
            ).fetchone()
            existing = self._row(row)
            if existing is None:
                raise JobConflict("idempotent job disappeared during enqueue")
            if existing["payload"] != json.loads(payload_json):
                raise JobConflict("idempotency key was reused with a different payload")
            return existing

    def claim(
        self, queue: str, worker_id: str, *, lease_seconds: float = 60.0
    ) -> dict[str, Any] | None:
        queue = _clean(queue, "queue", 128)
        worker_id = _clean(worker_id, "worker_id")
        if not math.isfinite(lease_seconds) or not 1 <= lease_seconds <= 3600:
            raise ValueError("lease_seconds must be between 1 and 3600")
        now = self._clock()
        token = secrets.token_urlsafe(32)
        marker = self.backend.sql(sqlite="?", postgres="%s")
        with self.backend.transaction(write=True) as connection:
            if self.backend.kind == "postgres":
                row = connection.execute(
                    "WITH candidate AS (SELECT job_id FROM ha_jobs "
                    f"WHERE queue_name={marker} AND cancel_requested=0 AND attempt<max_attempts AND "
                    f"((status IN ('{JOB_QUEUED}','{JOB_RETRY_WAIT}') AND available_at<={marker}) "
                    f"OR (status='{JOB_RUNNING}' AND lease_expires_at<={marker})) "
                    "ORDER BY priority DESC,available_at,created_at,job_id "
                    "FOR UPDATE SKIP LOCKED LIMIT 1) "
                    "UPDATE ha_jobs AS jobs SET status='running',lease_owner=%s,lease_token=%s,"
                    "lease_expires_at=%s,attempt=attempt+1,updated_at=%s,revision=revision+1 "
                    "FROM candidate WHERE jobs.job_id=candidate.job_id RETURNING jobs.*",
                    (queue, now, now, worker_id, token, now + lease_seconds, now),
                ).fetchone()
            else:
                candidate = connection.execute(
                    f"SELECT job_id FROM ha_jobs WHERE queue_name={marker} AND cancel_requested=0 "
                    "AND attempt<max_attempts AND "
                    f"((status IN ('{JOB_QUEUED}','{JOB_RETRY_WAIT}') AND available_at<={marker}) "
                    f"OR (status='{JOB_RUNNING}' AND lease_expires_at<={marker})) "
                    "ORDER BY priority DESC,available_at,created_at,job_id LIMIT 1",
                    (queue, now, now),
                ).fetchone()
                if candidate is None:
                    return None
                job_id = str(_column(candidate, "job_id", 0))
                changed = connection.execute(
                    "UPDATE ha_jobs SET status='running',lease_owner=?,lease_token=?,"
                    "lease_expires_at=?,attempt=attempt+1,updated_at=?,revision=revision+1 "
                    "WHERE job_id=? AND cancel_requested=0 AND attempt<max_attempts AND "
                    "((status IN ('queued','retry_wait') AND available_at<=?) "
                    "OR (status='running' AND lease_expires_at<=?))",
                    (worker_id, token, now + lease_seconds, now, job_id, now, now),
                )
                if changed.rowcount != 1:
                    return None
                row = connection.execute(
                    "SELECT * FROM ha_jobs WHERE job_id=?", (job_id,)
                ).fetchone()
            return self._row(row)

    def heartbeat(
        self, job_id: str, lease_token: str, *, lease_seconds: float = 60.0
    ) -> dict[str, Any]:
        job_id = _clean(job_id, "job_id")
        lease_token = _clean(lease_token, "lease_token", 512)
        if not math.isfinite(lease_seconds) or not 1 <= lease_seconds <= 3600:
            raise ValueError("lease_seconds must be between 1 and 3600")
        now = self._clock()
        marker = self.backend.sql(sqlite="?", postgres="%s")
        with self.backend.transaction(write=True) as connection:
            changed = connection.execute(
                f"UPDATE ha_jobs SET lease_expires_at={marker},updated_at={marker},revision=revision+1 "
                f"WHERE job_id={marker} AND status='{JOB_RUNNING}' AND lease_token={marker} "
                f"AND lease_expires_at>{marker} AND cancel_requested=0",
                (now + lease_seconds, now, job_id, lease_token, now),
            )
            if changed.rowcount != 1:
                raise StaleJobLease("job lease is stale, expired, or cancelled")
            return (
                self._row(
                    connection.execute(
                        f"SELECT * FROM ha_jobs WHERE job_id={marker}", (job_id,)
                    ).fetchone()
                )
                or {}
            )

    def complete(self, job_id: str, lease_token: str, result: Any) -> dict[str, Any]:
        return self._finish(job_id, lease_token, result=result, error_code=None)

    def fail(
        self,
        job_id: str,
        lease_token: str,
        error_code: str,
        *,
        retryable: bool,
        retry_delay_seconds: float = 0.0,
    ) -> dict[str, Any]:
        error_code = _clean(error_code, "error_code", 128)
        if not math.isfinite(retry_delay_seconds) or retry_delay_seconds < 0:
            raise ValueError("retry_delay_seconds is invalid")
        return self._finish(
            job_id,
            lease_token,
            result=None,
            error_code=error_code,
            retryable=retryable,
            retry_delay_seconds=retry_delay_seconds,
        )

    def _finish(
        self,
        job_id: str,
        lease_token: str,
        *,
        result: Any,
        error_code: str | None,
        retryable: bool = False,
        retry_delay_seconds: float = 0.0,
    ) -> dict[str, Any]:
        job_id = _clean(job_id, "job_id")
        lease_token = _clean(lease_token, "lease_token", 512)
        result_json = None if result is None else _json(result)
        now = self._clock()
        marker = self.backend.sql(sqlite="?", postgres="%s")
        with self.backend.transaction(write=True) as connection:
            lock_suffix = self.backend.sql(sqlite="", postgres=" FOR UPDATE")
            row = connection.execute(
                f"SELECT * FROM ha_jobs WHERE job_id={marker}{lock_suffix}", (job_id,)
            ).fetchone()
            current = self._row(row)
            if (
                current is None
                or current["status"] != JOB_RUNNING
                or current["lease_token"] != lease_token
                or float(current["lease_expires_at"] or 0) <= now
            ):
                raise StaleJobLease("job lease is stale or expired")
            cancelled = bool(current["cancel_requested"])
            if cancelled:
                status = JOB_CANCELLED
            elif error_code is None:
                status = JOB_SUCCEEDED
            elif retryable and int(current["attempt"]) < int(current["max_attempts"]):
                status = JOB_RETRY_WAIT
            elif retryable:
                status = JOB_DEAD_LETTER
            else:
                status = JOB_FAILED
            terminal = status in JOB_TERMINAL
            connection.execute(
                f"UPDATE ha_jobs SET status={marker},result_json={marker},error_code={marker},"
                f"available_at={marker},lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,"
                f"finished_at={marker},updated_at={marker},revision=revision+1 "
                f"WHERE job_id={marker} AND lease_token={marker} AND status='{JOB_RUNNING}'",
                (
                    status,
                    result_json if status == JOB_SUCCEEDED else None,
                    error_code,
                    now + retry_delay_seconds if status == JOB_RETRY_WAIT else now,
                    now if terminal else None,
                    now,
                    job_id,
                    lease_token,
                ),
            )
            return (
                self._row(
                    connection.execute(
                        f"SELECT * FROM ha_jobs WHERE job_id={marker}", (job_id,)
                    ).fetchone()
                )
                or {}
            )

    def request_cancel(
        self, job_id: str, *, tenant_id: str | None = None
    ) -> dict[str, Any] | None:
        job_id = _clean(job_id, "job_id")
        if tenant_id is not None:
            tenant_id = _clean(tenant_id, "tenant_id")
        now = self._clock()
        marker = self.backend.sql(sqlite="?", postgres="%s")
        with self.backend.transaction(write=True) as connection:
            lock_suffix = self.backend.sql(sqlite="", postgres=" FOR UPDATE")
            tenant_clause = "" if tenant_id is None else f" AND tenant_id={marker}"
            values: tuple[Any, ...] = (
                (job_id,) if tenant_id is None else (job_id, tenant_id)
            )
            row = connection.execute(
                f"SELECT * FROM ha_jobs WHERE job_id={marker}{tenant_clause}{lock_suffix}",
                values,
            ).fetchone()
            current = self._row(row)
            if current is None or current["status"] in JOB_TERMINAL:
                return current
            if current["status"] in {JOB_QUEUED, JOB_RETRY_WAIT}:
                connection.execute(
                    f"UPDATE ha_jobs SET status='{JOB_CANCELLED}',cancel_requested=1,"
                    f"finished_at={marker},updated_at={marker},revision=revision+1 WHERE job_id={marker}",
                    (now, now, job_id),
                )
            else:
                connection.execute(
                    f"UPDATE ha_jobs SET cancel_requested=1,updated_at={marker},"
                    f"revision=revision+1 WHERE job_id={marker}",
                    (now, job_id),
                )
            return self._row(
                connection.execute(
                    f"SELECT * FROM ha_jobs WHERE job_id={marker}", (job_id,)
                ).fetchone()
            )

    def replay_dead_letter(
        self,
        job_id: str,
        *,
        replay_key: str,
        tenant_id: str | None = None,
    ) -> dict[str, Any]:
        job_id = _clean(job_id, "job_id")
        replay_key = _clean(replay_key, "replay_key", 200)
        if tenant_id is not None:
            tenant_id = _clean(tenant_id, "tenant_id")
        now = self._clock()
        new_job_id = f"haj-{uuid.uuid4().hex}"
        idempotency_key = f"replay:{job_id}:{replay_key}"
        marker = self.backend.sql(sqlite="?", postgres="%s")
        lock = self.backend.sql(sqlite="", postgres=" FOR UPDATE")
        with self.backend.transaction(write=True) as connection:
            tenant_clause = "" if tenant_id is None else f" AND tenant_id={marker}"
            values: tuple[Any, ...] = (
                (job_id,) if tenant_id is None else (job_id, tenant_id)
            )
            raw = connection.execute(
                f"SELECT * FROM ha_jobs WHERE job_id={marker}{tenant_clause}{lock}",
                values,
            ).fetchone()
            source = self._row(raw)
            if source is None or source["status"] != JOB_DEAD_LETTER:
                raise JobConflict("only dead-letter jobs can be replayed")
            placeholders = self.backend.sql(
                sqlite="?,?,?,?,?,?,?,?,?,?,?,?",
                postgres="%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s",
            )
            insert_prefix = self.backend.sql(
                sqlite="INSERT OR IGNORE", postgres="INSERT"
            )
            conflict_suffix = self.backend.sql(
                sqlite="",
                postgres=" ON CONFLICT(queue_name,tenant_id,idempotency_key) DO NOTHING",
            )
            inserted = connection.execute(
                f"{insert_prefix} INTO ha_jobs(job_id,queue_name,tenant_id,payload_json,status,"
                "priority,available_at,max_attempts,idempotency_key,replay_of,created_at,updated_at) "
                f"VALUES({placeholders}){conflict_suffix}",
                (
                    new_job_id,
                    source["queue_name"],
                    source["tenant_id"],
                    _json(source["payload"]),
                    JOB_QUEUED,
                    int(source["priority"]),
                    now,
                    int(source["max_attempts"]),
                    idempotency_key,
                    job_id,
                    now,
                    now,
                ),
            )
            if inserted.rowcount != 1:
                existing = connection.execute(
                    f"SELECT * FROM ha_jobs WHERE queue_name={marker} AND tenant_id={marker} "
                    f"AND idempotency_key={marker}",
                    (source["queue_name"], source["tenant_id"], idempotency_key),
                ).fetchone()
                replay = self._row(existing)
                if replay is None or replay.get("replay_of") != job_id:
                    raise JobConflict("dead-letter replay key is already in use")
                return replay
            created = connection.execute(
                f"SELECT * FROM ha_jobs WHERE job_id={marker}", (new_job_id,)
            ).fetchone()
            return self._row(created) or {}

    def reap_expired(self, *, limit: int = 1000) -> int:
        if type(limit) is not int or not 1 <= limit <= 10_000:
            raise ValueError("limit must be between 1 and 10000")
        now = self._clock()
        marker = self.backend.sql(sqlite="?", postgres="%s")
        with self.backend.transaction(write=True) as connection:
            rows = connection.execute(
                f"SELECT job_id,cancel_requested,attempt,max_attempts FROM ha_jobs "
                f"WHERE status='{JOB_RUNNING}' "
                f"AND lease_expires_at<={marker} ORDER BY lease_expires_at,job_id LIMIT {limit}",
                (now,),
            ).fetchall()
            for row in rows:
                status = (
                    JOB_CANCELLED
                    if bool(_column(row, "cancel_requested", 1))
                    else (
                        JOB_DEAD_LETTER
                        if int(_column(row, "attempt", 2))
                        >= int(_column(row, "max_attempts", 3))
                        else JOB_RETRY_WAIT
                    )
                )
                connection.execute(
                    f"UPDATE ha_jobs SET status={marker},available_at={marker},lease_owner=NULL,"
                    f"lease_token=NULL,lease_expires_at=NULL,error_code={marker},"
                    f"finished_at={marker},updated_at={marker},"
                    f"revision=revision+1 WHERE job_id={marker} AND status='{JOB_RUNNING}' "
                    f"AND lease_expires_at<={marker}",
                    (
                        status,
                        now,
                        "LEASE_EXPIRED" if status == JOB_DEAD_LETTER else None,
                        now if status in JOB_TERMINAL else None,
                        now,
                        str(_column(row, "job_id", 0)),
                        now,
                    ),
                )
            return len(rows)

    def prune_terminal(self, *, before: float, limit: int = 1000) -> int:
        if not math.isfinite(before):
            raise ValueError("job prune cutoff must be finite")
        if type(limit) is not int or not 1 <= limit <= 10_000:
            raise ValueError("job prune limit must be between 1 and 10000")
        marker = self.backend.sql(sqlite="?", postgres="%s")
        terminal = ",".join(f"'{status}'" for status in sorted(JOB_TERMINAL))
        with self.backend.transaction(write=True) as connection:
            rows = connection.execute(
                f"SELECT job_id,queue_name,tenant_id,idempotency_key,status,payload_json "
                f"FROM ha_jobs WHERE status IN ({terminal}) AND finished_at<={marker} "
                f"ORDER BY finished_at,job_id LIMIT {limit}",
                (before,),
            ).fetchall()
            removed = 0
            for raw in rows:
                row = dict(raw)
                key = row.get("idempotency_key")
                if key:
                    fingerprint = hashlib.sha256(
                        str(row["payload_json"]).encode()
                    ).hexdigest()
                    insert = self.backend.sql(
                        sqlite="INSERT OR IGNORE",
                        postgres="INSERT",
                    )
                    suffix = self.backend.sql(
                        sqlite="",
                        postgres=" ON CONFLICT(queue_name,tenant_id,idempotency_key) DO NOTHING",
                    )
                    placeholders = self.backend.sql(
                        sqlite="?,?,?,?,?,?,?",
                        postgres="%s,%s,%s,%s,%s,%s,%s",
                    )
                    connection.execute(
                        f"{insert} INTO ha_job_keys(queue_name,tenant_id,idempotency_key,job_id,"
                        "fingerprint,terminal_status,created_at) "
                        f"VALUES({placeholders}){suffix}",
                        (
                            row["queue_name"],
                            row["tenant_id"],
                            key,
                            row["job_id"],
                            fingerprint,
                            row["status"],
                            self._clock(),
                        ),
                    )
                    connection.execute(
                        f"UPDATE ha_job_keys SET terminal_status={marker} WHERE queue_name={marker} "
                        f"AND tenant_id={marker} AND idempotency_key={marker} AND job_id={marker}",
                        (
                            row["status"],
                            row["queue_name"],
                            row["tenant_id"],
                            key,
                            row["job_id"],
                        ),
                    )
                changed = connection.execute(
                    f"DELETE FROM ha_jobs WHERE job_id={marker} AND status IN ({terminal}) "
                    f"AND finished_at<={marker}",
                    (row["job_id"], before),
                )
                removed += int(changed.rowcount == 1)
            return removed

    def get(self, job_id: str) -> dict[str, Any] | None:
        job_id = _clean(job_id, "job_id")
        marker = self.backend.sql(sqlite="?", postgres="%s")
        with self.backend.transaction() as connection:
            return self._row(
                connection.execute(
                    f"SELECT * FROM ha_jobs WHERE job_id={marker}", (job_id,)
                ).fetchone()
            )

    def list_jobs(
        self,
        *,
        queue: str | None = None,
        tenant_id: str | None = None,
        status: str | None = None,
        before: tuple[float, str] | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        conditions: list[str] = []
        values: list[Any] = []
        marker = self.backend.sql(sqlite="?", postgres="%s")
        if queue is not None:
            conditions.append(f"queue_name={marker}")
            values.append(_clean(queue, "queue", 128))
        if tenant_id is not None:
            conditions.append(f"tenant_id={marker}")
            values.append(_clean(tenant_id, "tenant_id"))
        if status is not None:
            if status not in {
                JOB_QUEUED,
                JOB_RUNNING,
                JOB_RETRY_WAIT,
                JOB_SUCCEEDED,
                JOB_FAILED,
                JOB_DEAD_LETTER,
                JOB_CANCELLED,
            }:
                raise ValueError("job status is invalid")
            conditions.append(f"status={marker}")
            values.append(status)
        if before is not None:
            if not isinstance(before, tuple) or len(before) != 2:
                raise ValueError("job cursor is invalid")
            created_at = float(before[0])
            if not math.isfinite(created_at) or created_at < 0:
                raise ValueError("job cursor is invalid")
            job_id = _clean(before[1], "before_job_id")
            conditions.append(
                f"(created_at<{marker} OR (created_at={marker} AND job_id<{marker}))"
            )
            values.extend((created_at, created_at, job_id))
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        with self.backend.transaction() as connection:
            return [
                item
                for row in connection.execute(
                    f"SELECT * FROM ha_jobs{where} ORDER BY created_at DESC,job_id DESC LIMIT {limit}",
                    tuple(values),
                ).fetchall()
                if (item := self._row(row)) is not None
            ]


__all__ = [
    "JOB_CANCELLED",
    "JOB_DEAD_LETTER",
    "JOB_FAILED",
    "JOB_QUEUED",
    "JOB_RETRY_WAIT",
    "JOB_RUNNING",
    "JOB_SUCCEEDED",
    "JOB_TERMINAL",
    "JobConflict",
    "JobError",
    "LeaseJobStore",
    "StaleJobLease",
]
