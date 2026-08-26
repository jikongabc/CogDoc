from __future__ import annotations

import json
import os
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from threading import Lock
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cogdoc.config.settings import get_settings
from cogdoc.service.ingest_service import (
    INDEX_BUILD_VERSION,
    _cleanup_generation_storage,
    build_kb_index_transactional,
    index_build_version,
)
from cogdoc.service.kb_locks import kb_write_lock
from cogdoc.service.kb_state import KBState
from cogdoc.service.retriever_factory import RetrieverFactory
from cogdoc.tools.chunk_identity import CHUNK_IDENTITY_VERSION
from cogdoc.tools.manifest import save_index_manifest
from cogdoc.tools.embedder import resolve_embedder


ProgressCallback = Callable[[Mapping[str, Any]], None]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_error(exc: Exception) -> str:
    message = " ".join(str(exc).split())
    return f"{type(exc).__name__}: {message}"[:500]


class IndexMigrationStore:
    """Small atomic store for resumable, auditable index migration runs."""

    def __init__(self, directory: str | os.PathLike[str] | None = None):
        root = (
            Path(directory)
            if directory is not None
            else get_settings().data_dir / "reliability" / "index-migrations"
        )
        self.directory = root

    def save(self, run: Mapping[str, Any]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        run_id = str(run.get("run_id") or "")
        if not run_id or any(char not in "0123456789abcdef" for char in run_id):
            raise ValueError("invalid migration run_id")
        target = self.directory / f"{run_id}.json"
        temporary = self.directory / f".{run_id}.tmp"
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(run, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)

    def load(self, run_id: str) -> dict[str, Any]:
        if not run_id or any(char not in "0123456789abcdef" for char in run_id):
            raise ValueError("invalid migration run_id")
        path = self.directory / f"{run_id}.json"
        with path.open(encoding="utf-8") as handle:
            value = json.load(handle)
        if not isinstance(value, dict) or value.get("run_id") != run_id:
            raise ValueError("migration record is invalid")
        return value


def inspect_index_generation(storage_id: str) -> dict[str, Any]:
    active = KBState(storage_id).active()
    try:
        embedder = resolve_embedder(str((active or {}).get("embedding_model") or "local"))
        target_build = index_build_version(embedder)
    except (RuntimeError, ValueError):
        target_build = INDEX_BUILD_VERSION
    actual_identity = str((active or {}).get("chunk_identity_version") or "")
    actual_build = str((active or {}).get("index_build_version") or "")
    reasons = []
    if active is None:
        reasons.append("missing_active_generation")
    if actual_identity != CHUNK_IDENTITY_VERSION:
        reasons.append("chunk_identity_version_mismatch")
    if actual_build != target_build:
        reasons.append("index_build_version_mismatch")
    return {
        "storage_id": storage_id,
        "active_generation_id": str((active or {}).get("id") or ""),
        "actual_chunk_identity_version": actual_identity,
        "target_chunk_identity_version": CHUNK_IDENTITY_VERSION,
        "actual_index_build_version": actual_build,
        "target_index_build_version": target_build,
        "needs_migration": bool(reasons),
        "reasons": reasons,
    }


class IndexMigrationRunner:
    """Batch v7 migration with retained old generations and explicit rollback."""

    def __init__(
        self,
        *,
        store: IndexMigrationStore | None = None,
        build: Callable[..., Any] = build_kb_index_transactional,
        source_dir_for: Callable[[str], str] | None = None,
        knowledge_store: Any = None,
        refresh_derived_knowledge: Callable[[str], None] | None = None,
        save_manifest: Callable[[dict[str, Any]], None] = save_index_manifest,
    ):
        self.store = store or IndexMigrationStore()
        self._build = build
        self._source_dir_for = source_dir_for or get_settings().kb_source_dir
        self._knowledge_store = knowledge_store
        self._refresh_derived = refresh_derived_knowledge
        self._save_manifest = save_manifest

    def plan(self, records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        items = []
        for record in records:
            storage_id = str(record.get("storage_id") or "")
            if not storage_id:
                continue
            item = inspect_index_generation(storage_id)
            item.update(
                {
                    "kb_id": str(record.get("kb_id") or storage_id),
                    "tenant_id": str(record.get("tenant_id") or "default"),
                }
            )
            items.append(item)
        return {
            "schema_version": "v1",
            "target_chunk_identity_version": CHUNK_IDENTITY_VERSION,
            "target_index_build_version": INDEX_BUILD_VERSION,
            "total": len(items),
            "needs_migration": sum(bool(item["needs_migration"]) for item in items),
            "items": items,
        }

    def run(
        self,
        records: Sequence[Mapping[str, Any]],
        *,
        include_current: bool = False,
        progress: ProgressCallback | None = None,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        plan = self.plan(records)
        run: dict[str, Any] = {
            "schema_version": "v1",
            "run_id": run_id or uuid.uuid4().hex,
            "status": "running",
            "created_at": _now_iso(),
            "finished_at": None,
            "target_chunk_identity_version": CHUNK_IDENTITY_VERSION,
            "target_index_build_version": INDEX_BUILD_VERSION,
            "authorized_storage_ids": [
                str(record.get("storage_id") or "")
                for record in records
                if str(record.get("storage_id") or "")
            ],
            "items": [],
        }
        self.store.save(run)
        total = len(plan["items"])
        for position, planned in enumerate(plan["items"], start=1):
            item = {
                **planned,
                "status": "pending",
                "started_at": None,
                "finished_at": None,
                "generation_id": None,
                "previous_generation_id": None,
                "document_count": None,
                "chunk_count": None,
                "derived_knowledge_refreshed": False,
                "error": None,
            }
            run["items"].append(item)
            if not planned["needs_migration"] and not include_current:
                item.update(status="skipped", finished_at=_now_iso())
                self.store.save(run)
                self._emit(progress, run, item, position, total)
                continue

            item.update(status="running", started_at=_now_iso())
            self.store.save(run)
            self._emit(progress, run, item, position, total)
            try:
                result = self._build(
                    planned["storage_id"],
                    self._source_dir_for(planned["storage_id"]),
                    knowledge_store=self._knowledge_store,
                    retain_previous_generation=True,
                )
                item.update(
                    status="succeeded",
                    finished_at=_now_iso(),
                    generation_id=result.generation_id,
                    previous_generation_id=result.previous_generation_id,
                    document_count=result.document_count,
                    chunk_count=result.chunk_count,
                )
                if self._refresh_derived is not None:
                    try:
                        self._refresh_derived(planned["storage_id"])
                        item["derived_knowledge_refreshed"] = True
                    except Exception as exc:
                        # The document index commit already happened.  Preserve
                        # the rollback identity and report the secondary refresh
                        # failure without pretending the migration never committed.
                        item["status"] = "succeeded_with_refresh_failure"
                        item["refresh_error"] = _safe_error(exc)
            except Exception as exc:
                item.update(
                    status="failed", finished_at=_now_iso(), error=_safe_error(exc)
                )
            self.store.save(run)
            self._emit(progress, run, item, position, total)

        failed = sum(
            item["status"] in {"failed", "succeeded_with_refresh_failure"}
            for item in run["items"]
        )
        run["status"] = "completed_with_failures" if failed else "completed"
        run["finished_at"] = _now_iso()
        run["summary"] = self._summary(run)
        self.store.save(run)
        return run

    def rollback(
        self,
        run_id: str,
        *,
        storage_ids: Sequence[str] | None = None,
        progress: ProgressCallback | None = None,
    ) -> dict[str, Any]:
        run = self.store.load(run_id)
        selected = set(storage_ids or ())
        candidates = [
            item
            for item in run.get("items", [])
            if isinstance(item, dict)
            and item.get("status") in {"succeeded", "succeeded_with_refresh_failure"}
            and item.get("previous_generation_id")
            and (not selected or item.get("storage_id") in selected)
        ]
        for position, item in enumerate(candidates, start=1):
            storage_id = str(item["storage_id"])
            try:
                with kb_write_lock(storage_id):
                    state = KBState(storage_id)
                    replaced = state.rollback_active(
                        str(item["previous_generation_id"])
                    )
                    active = state.active()
                    if active is None:
                        raise RuntimeError("rolled-back generation is not active")
                    try:
                        self._save_manifest(
                            {
                                "doc_id": storage_id,
                                "documents": list(active.get("documents") or []),
                                "chunk_identity_version": str(
                                    active.get("chunk_identity_version") or ""
                                ),
                                "index_build_version": str(
                                    active.get("index_build_version") or ""
                                ),
                            }
                        )
                    except Exception:
                        # Keep state and manifest on the migrated generation when
                        # restoring the previous manifest cannot be committed.
                        state.rollback_active(replaced)
                        raise
                    RetrieverFactory.invalidate(storage_id)
                item.update(
                    status="rolled_back",
                    rolled_back_at=_now_iso(),
                    rolled_back_generation_id=replaced,
                )
            except Exception as exc:
                item.update(rollback_error=_safe_error(exc))
            self.store.save(run)
            self._emit(progress, run, item, position, len(candidates))
        run["summary"] = self._summary(run)
        self.store.save(run)
        return run

    def finalize(self, run_id: str) -> dict[str, Any]:
        """Delete retained non-active generations after rollout acceptance."""

        run = self.store.load(run_id)
        for item in run.get("items", []):
            if not isinstance(item, dict) or item.get("status") not in {
                "succeeded",
                "succeeded_with_refresh_failure",
            }:
                continue
            previous = str(item.get("previous_generation_id") or "")
            if not previous or item.get("retained_generation_cleaned_at"):
                continue
            try:
                _cleanup_generation_storage(str(item["storage_id"]), previous)
                item["retained_generation_cleaned_at"] = _now_iso()
            except Exception as exc:
                item["finalize_error"] = _safe_error(exc)
            self.store.save(run)
        run["summary"] = self._summary(run)
        self.store.save(run)
        return run

    @staticmethod
    def _summary(run: Mapping[str, Any]) -> dict[str, int]:
        statuses: dict[str, int] = {}
        for item in run.get("items", []):
            if isinstance(item, Mapping):
                status = str(item.get("status") or "unknown")
                statuses[status] = statuses.get(status, 0) + 1
        return statuses

    @staticmethod
    def _emit(
        callback: ProgressCallback | None,
        run: Mapping[str, Any],
        item: Mapping[str, Any],
        position: int,
        total: int,
    ) -> None:
        if callback is not None:
            callback(
                {
                    "run_id": run["run_id"],
                    "position": position,
                    "total": total,
                    "storage_id": item.get("storage_id"),
                    "kb_id": item.get("kb_id"),
                    "status": item.get("status"),
                    "error": item.get("error") or item.get("rollback_error"),
                    "time": time.time(),
                }
            )


class IndexMigrationManager:
    """Single-flight background coordinator for durable migration runs."""

    def __init__(self, runner: IndexMigrationRunner):
        self.runner = runner
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="cogdoc-index-migration"
        )
        self._futures: dict[str, Future[dict[str, Any]]] = {}
        self._lock = Lock()
        self._closed = False

    def submit(
        self,
        records: Sequence[Mapping[str, Any]],
        *,
        include_current: bool = False,
    ) -> dict[str, Any]:
        with self._lock:
            if self._closed:
                raise RuntimeError("index migration manager is closed")
            active = [future for future in self._futures.values() if not future.done()]
            if active:
                raise RuntimeError("another index migration operation is running")
            run_id = uuid.uuid4().hex
            queued = {
                "schema_version": "v1",
                "run_id": run_id,
                "status": "queued",
                "created_at": _now_iso(),
                "finished_at": None,
                "target_chunk_identity_version": CHUNK_IDENTITY_VERSION,
                "target_index_build_version": INDEX_BUILD_VERSION,
                "authorized_storage_ids": [
                    str(record.get("storage_id") or "")
                    for record in records
                    if str(record.get("storage_id") or "")
                ],
                "items": [],
            }
            self.runner.store.save(queued)
            future = self._executor.submit(
                self.runner.run,
                list(records),
                include_current=include_current,
                run_id=run_id,
            )
            self._futures[run_id] = future
            return queued

    def get(self, run_id: str) -> dict[str, Any]:
        return self.runner.store.load(run_id)

    def rollback(self, run_id: str, storage_ids: Sequence[str] = ()) -> dict[str, Any]:
        return self._serialized(self.runner.rollback, run_id, storage_ids=storage_ids)

    def finalize(self, run_id: str) -> dict[str, Any]:
        return self._serialized(self.runner.finalize, run_id)

    def _serialized(self, function: Callable[..., dict[str, Any]], *args, **kwargs):
        with self._lock:
            if self._closed:
                raise RuntimeError("index migration manager is closed")
            if any(not future.done() for future in self._futures.values()):
                raise RuntimeError("another index migration operation is running")
            future = self._executor.submit(function, *args, **kwargs)
        return future.result()

    def reopen(self) -> None:
        """Recreate the terminal executor for a subsequent app lifespan."""

        with self._lock:
            if not self._closed:
                return
            if any(not future.done() for future in self._futures.values()):
                raise RuntimeError("cannot reopen while an index migration is running")
            self._futures.clear()
            self._executor = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="cogdoc-index-migration"
            )
            self._closed = False

    def shutdown(self, *, wait: bool = True) -> None:
        with self._lock:
            self._closed = True
        self._executor.shutdown(wait=wait, cancel_futures=not wait)
