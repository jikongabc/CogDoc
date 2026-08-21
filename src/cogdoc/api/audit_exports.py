"""Durable, tenant-scoped audit export jobs and immutable artifacts."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
import uuid
from collections.abc import Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from threading import RLock
from typing import Any

from cogdoc.api.audit import AuditStore


EXPORT_SCHEMA_VERSION = "v1"
_TERMINAL = frozenset({"succeeded", "failed", "expired", "deleted"})


class AuditExportError(RuntimeError):
    pass


class AuditExportConflict(AuditExportError):
    pass


class AuditExportStore:
    """SQLite job ledger plus content-addressed NDJSON export artifacts."""

    def __init__(self, db_path: str | os.PathLike[str], root: str | os.PathLike[str]):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._lock = RLock()
        self._db_path = os.fspath(db_path)
        self._conn = self._connect()
        self._closed = False
        self._initialize_schema()
        self._scavenge_temporary_files()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self._db_path, check_same_thread=False, isolation_level=None
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def _initialize_schema(self) -> None:
        """Create or verify the small export ledger after each open."""

        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS audit_export_jobs (
            job_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, actor_id TEXT NOT NULL,
            status TEXT NOT NULL, filters_json TEXT NOT NULL, event_count INTEGER,
            first_sequence INTEGER, last_sequence INTEGER, chain_head TEXT,
            artifact_sha256 TEXT, byte_size INTEGER, error_code TEXT,
            created_at REAL NOT NULL, started_at REAL, completed_at REAL,
            expires_at REAL NOT NULL, revision INTEGER NOT NULL DEFAULT 1)"""
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_audit_exports_tenant_created "
            "ON audit_export_jobs(tenant_id, created_at DESC)"
        )

    def create(
        self,
        tenant_id: str,
        actor_id: str,
        *,
        from_sequence: int | None = None,
        to_sequence: int | None = None,
        actions: Sequence[str] = (),
        statuses: Sequence[int] = (),
        retention_seconds: int = 86_400,
    ) -> dict[str, Any]:
        if not tenant_id.strip() or not actor_id.strip():
            raise ValueError("tenant_id and actor_id are required")
        if not 300 <= retention_seconds <= 7 * 86_400:
            raise ValueError("retention_seconds must be between 300 and 604800")
        filters = {
            "from_sequence": from_sequence,
            "to_sequence": to_sequence,
            "actions": sorted(set(actions)),
            "statuses": sorted(set(statuses)),
        }
        # Reuse AuditStore's validation contract before persisting work.
        if from_sequence is not None and from_sequence < 1:
            raise ValueError("from_sequence must be positive")
        if to_sequence is not None and to_sequence < 1:
            raise ValueError("to_sequence must be positive")
        if (
            from_sequence is not None
            and to_sequence is not None
            and from_sequence > to_sequence
        ):
            raise ValueError("from_sequence must not exceed to_sequence")
        if any(not isinstance(item, str) or not item.strip() for item in actions):
            raise ValueError("actions must be non-empty strings")
        if any(type(item) is not int or not 100 <= item <= 599 for item in statuses):
            raise ValueError("statuses must be HTTP status integers")
        now = time.time()
        job_id = f"audit-export-{uuid.uuid4().hex}"
        with self._lock:
            active = self._conn.execute(
                "SELECT COUNT(*) FROM audit_export_jobs WHERE tenant_id=? "
                "AND status IN ('pending','running')",
                (tenant_id.strip(),),
            ).fetchone()[0]
            if active >= 2:
                raise AuditExportConflict("tenant already has two active audit exports")
            self._conn.execute(
                "INSERT INTO audit_export_jobs(job_id,tenant_id,actor_id,status,filters_json,created_at,expires_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (
                    job_id,
                    tenant_id.strip(),
                    actor_id.strip(),
                    "pending",
                    _json(filters),
                    now,
                    now + retention_seconds,
                ),
            )
            return self.get(job_id, tenant_id.strip()) or {}

    def claim(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT * FROM audit_export_jobs WHERE job_id=?", (job_id,)
                ).fetchone()
                if row is None or row["status"] not in {"pending", "running"}:
                    self._conn.execute("ROLLBACK")
                    return None
                self._conn.execute(
                    "UPDATE audit_export_jobs SET status='running',started_at=COALESCE(started_at,?),revision=revision+1 WHERE job_id=?",
                    (time.time(), job_id),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            return self._row(
                self._conn.execute(
                    "SELECT * FROM audit_export_jobs WHERE job_id=?", (job_id,)
                ).fetchone()
            )

    def complete(
        self,
        job_id: str,
        events: Sequence[Mapping[str, Any]],
        *,
        source_chain_head: str | None,
    ) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM audit_export_jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            if row is None or row["status"] != "running":
                raise AuditExportConflict("audit export is not running")
            tenant = str(row["tenant_id"])
            digest = hashlib.sha256()
            target = self.root / f"{job_id}.ndjson"
            temporary = self.root / f".{job_id}.{uuid.uuid4().hex}.tmp"
            try:
                descriptor = os.open(
                    temporary,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
                with os.fdopen(descriptor, "wb") as handle:
                    header = {
                        "schema_version": EXPORT_SCHEMA_VERSION,
                        "record_type": "manifest",
                        "tenant_id": tenant,
                        "event_count": len(events),
                        "first_sequence": events[0]["sequence"] if events else None,
                        "last_sequence": events[-1]["sequence"] if events else None,
                        "source_chain_head": source_chain_head,
                        "filters": json.loads(str(row["filters_json"])),
                    }
                    for item in (header, *events):
                        encoded = (_json(item) + "\n").encode("utf-8")
                        handle.write(encoded)
                        digest.update(encoded)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, target)
                _fsync_dir(self.root)
            finally:
                temporary.unlink(missing_ok=True)
            size = target.stat().st_size
            now = time.time()
            self._conn.execute(
                "UPDATE audit_export_jobs SET status='succeeded',event_count=?,first_sequence=?,last_sequence=?,chain_head=?,artifact_sha256=?,byte_size=?,completed_at=?,revision=revision+1 WHERE job_id=? AND status='running'",
                (
                    len(events),
                    events[0]["sequence"] if events else None,
                    events[-1]["sequence"] if events else None,
                    source_chain_head,
                    digest.hexdigest(),
                    size,
                    now,
                    job_id,
                ),
            )
            return self.get(job_id, tenant) or {}

    def fail(self, job_id: str, error_code: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE audit_export_jobs SET status='failed',error_code=?,completed_at=?,revision=revision+1 WHERE job_id=? AND status IN ('pending','running')",
                (error_code[:64], time.time(), job_id),
            )

    def get(self, job_id: str, tenant_id: str) -> dict[str, Any] | None:
        with self._lock:
            return self._row(
                self._conn.execute(
                    "SELECT * FROM audit_export_jobs WHERE job_id=? AND tenant_id=? AND status!='deleted'",
                    (job_id, tenant_id),
                ).fetchone()
            )

    def list_jobs(self, tenant_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        with self._lock:
            result: list[dict[str, Any]] = []
            for row in self._conn.execute(
                "SELECT * FROM audit_export_jobs WHERE tenant_id=? AND status!='deleted' ORDER BY created_at DESC,job_id DESC LIMIT ?",
                (tenant_id, limit),
            ).fetchall():
                item = self._row(row)
                assert item is not None
                result.append(item)
            return result

    def recoverable(self) -> list[str]:
        with self._lock:
            return [
                str(row[0])
                for row in self._conn.execute(
                    "SELECT job_id FROM audit_export_jobs WHERE status IN ('pending','running') ORDER BY created_at"
                ).fetchall()
            ]

    def artifact_path(self, job_id: str, tenant_id: str) -> Path:
        with self._lock:
            row = self._conn.execute(
                "SELECT status,artifact_sha256,byte_size FROM audit_export_jobs WHERE job_id=? AND tenant_id=?",
                (job_id, tenant_id),
            ).fetchone()
            if row is None or row["status"] != "succeeded":
                raise AuditExportConflict("audit export is not downloadable")
            target = self.root / f"{job_id}.ndjson"
            digest = hashlib.sha256()
            size = 0
            with target.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    size += len(chunk)
                    digest.update(chunk)
            if size != row["byte_size"] or digest.hexdigest() != row["artifact_sha256"]:
                raise AuditExportError(
                    "audit export artifact failed integrity verification"
                )
            return target

    def delete(self, job_id: str, tenant_id: str, *, expected_revision: int) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT status,revision FROM audit_export_jobs WHERE job_id=? AND tenant_id=?",
                (job_id, tenant_id),
            ).fetchone()
            if row is None or row["status"] == "deleted":
                return False
            if int(row["revision"]) != expected_revision:
                raise AuditExportConflict("audit export revision changed")
            if row["status"] not in _TERMINAL:
                raise AuditExportConflict("active audit export cannot be deleted")
            (self.root / f"{job_id}.ndjson").unlink(missing_ok=True)
            _fsync_dir(self.root)
            self._conn.execute(
                "UPDATE audit_export_jobs SET status='deleted',revision=revision+1 WHERE job_id=?",
                (job_id,),
            )
            return True

    def purge_expired(self, *, limit: int = 1000) -> int:
        now = time.time()
        with self._lock:
            rows = self._conn.execute(
                "SELECT job_id FROM audit_export_jobs WHERE expires_at<=? AND status IN ('succeeded','failed') LIMIT ?",
                (now, limit),
            ).fetchall()
            for row in rows:
                job_id = str(row[0])
                (self.root / f"{job_id}.ndjson").unlink(missing_ok=True)
                self._conn.execute(
                    "UPDATE audit_export_jobs SET status='expired',revision=revision+1 WHERE job_id=?",
                    (job_id,),
                )
            if rows:
                _fsync_dir(self.root)
            return len(rows)

    def check(self) -> bool:
        with self._lock:
            self._conn.execute("SELECT 1 FROM audit_export_jobs LIMIT 1").fetchone()
            self.root.stat()
            return True

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
            self._initialize_schema()
            self._scavenge_temporary_files()
            self._closed = False

    def _scavenge_temporary_files(self) -> None:
        changed = False
        for path in self.root.glob(".audit-export-*.tmp"):
            try:
                if path.is_symlink() or not path.is_file():
                    raise AuditExportError("unsafe audit export temporary entry")
                path.unlink()
                changed = True
            except FileNotFoundError:
                continue
        if changed:
            _fsync_dir(self.root)

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        result["filters"] = json.loads(result.pop("filters_json"))
        return result


class AuditExportManager:
    def __init__(
        self, store: AuditExportStore, audit_store: AuditStore, *, workers: int = 1
    ):
        self.store = store
        self.audit_store = audit_store
        self._workers = workers
        self._lock = RLock()
        self._executor = ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="cogdoc-audit-export"
        )
        self._futures: dict[str, Future[None]] = {}
        self._closed = False

    def submit(self, **kwargs: Any) -> dict[str, Any]:
        job = self.store.create(**kwargs)
        self._dispatch(str(job["job_id"]))
        return job

    def recover(self) -> None:
        self.store.purge_expired()
        for job_id in self.store.recoverable():
            self._dispatch(job_id)

    def _dispatch(self, job_id: str) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeError("audit export manager is closed")
            existing = self._futures.get(job_id)
            if existing is not None and not existing.done():
                return
            future = self._executor.submit(self._run, job_id)
            self._futures[job_id] = future
            future.add_done_callback(lambda _future: self._forget(job_id, _future))

    def _forget(self, job_id: str, future: Future[None]) -> None:
        with self._lock:
            if self._futures.get(job_id) is future:
                self._futures.pop(job_id, None)

    def _run(self, job_id: str) -> None:
        job = self.store.claim(job_id)
        if job is None:
            return
        try:
            filters = job["filters"]
            source_events = self.audit_store.snapshot(
                str(job["tenant_id"]),
                from_sequence=filters.get("from_sequence"),
                to_sequence=filters.get("to_sequence"),
                actions=tuple(filters.get("actions") or ()),
                statuses=tuple(filters.get("statuses") or ()),
            )
            self.store.complete(
                job_id,
                source_events,
                source_chain_head=(
                    str(source_events[-1]["event_hash"]) if source_events else None
                ),
            )
        except Exception as exc:
            self.store.fail(job_id, type(exc).__name__.upper())

    def shutdown(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            executor = self._executor
        executor.shutdown(wait=True, cancel_futures=False)

    def reopen(self) -> None:
        with self._lock:
            if not self._closed:
                return
            self.store.reopen()
            self._executor = ThreadPoolExecutor(
                max_workers=self._workers,
                thread_name_prefix="cogdoc-audit-export",
            )
            self._futures.clear()
            self._closed = False


def _json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _fsync_dir(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = [
    "AuditExportConflict",
    "AuditExportError",
    "AuditExportManager",
    "AuditExportStore",
]
