from __future__ import annotations

import logging
import math
import tempfile
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from cogdoc.ha.index_generation import GEN_PREPARED, GEN_PUBLISHED
from cogdoc.ha.portable_index import export_retrieval_generation
from cogdoc.ha.runtime import HARuntime, manifest_for_directory
from cogdoc.service.kb_lifecycle import LIFECYCLE_ACTIVE, shared_lifecycle_store
from cogdoc.service.kb_state import KBState
from cogdoc.tools.embedder import Embedder


LOGGER = logging.getLogger(__name__)


class HAIndexMirror:
    """Crash-reconcilable bridge from committed local generations to HA authority."""

    def __init__(
        self,
        runtime: HARuntime,
        registry: Any,
        *,
        interval_seconds: float = 30.0,
    ) -> None:
        if not math.isfinite(interval_seconds) or not 5 <= interval_seconds <= 3600:
            raise ValueError("HA index mirror interval is invalid")
        self.runtime = runtime
        self.registry = registry
        self.interval_seconds = float(interval_seconds)
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._last_error: BaseException | None = None

    @staticmethod
    def _build_id(local_generation_id: str) -> str:
        return f"local:{local_generation_id}"

    def mirror_result(self, kb_id: str, result: Any) -> dict[str, Any]:
        generation_id = str(getattr(result, "generation_id", "") or "")
        if not generation_id:
            raise RuntimeError("committed index result has no generation id")
        record = self.registry.get_by_storage_id(kb_id)
        if not isinstance(record, Mapping) or str(record.get("storage_id")) != kb_id:
            raise RuntimeError("committed index knowledge base is not registered")
        return self.mirror(
            str(record["tenant_id"]),
            kb_id,
            generation_id,
        )

    def mirror(
        self, tenant_id: str, kb_id: str, local_generation_id: str
    ) -> dict[str, Any]:
        active = KBState(kb_id).active()
        if active is None or active.get("id") != local_generation_id:
            raise RuntimeError("local index generation is no longer active")
        build_id = self._build_id(local_generation_id)
        current = self.runtime.index_generations.current(tenant_id, kb_id)
        if current is not None and current["build_id"] == build_id:
            return current
        generation = self.runtime.index_generations.begin_build(
            tenant_id,
            kb_id,
            build_id,
            self.runtime.config.worker_id,
            lease_seconds=self.runtime.config.index_worker_lease_seconds,
        )
        if generation["status"] == GEN_PUBLISHED:
            return generation
        lease_token = str(generation["lease_token"])
        generation_id = str(generation["generation_id"])
        heartbeat_stop = threading.Event()
        heartbeat_lost = threading.Event()

        def keep_lease() -> None:
            interval = max(1.0, self.runtime.config.index_worker_lease_seconds / 3)
            while not heartbeat_stop.wait(interval):
                try:
                    self.runtime.index_generations.heartbeat(
                        generation_id,
                        lease_token,
                        lease_seconds=self.runtime.config.index_worker_lease_seconds,
                    )
                except Exception:
                    heartbeat_lost.set()
                    return

        keeper = threading.Thread(
            target=keep_lease,
            name=f"cogdoc-ha-mirror-{local_generation_id}",
            daemon=True,
        )
        keeper.start()
        try:
            with tempfile.TemporaryDirectory(prefix="cogdoc-ha-index-") as temporary:
                directory = Path(temporary)
                metadata = export_retrieval_generation(
                    kb_id,
                    local_generation_id,
                    directory,
                    embedding_model=Embedder.EMBEDDING_CONTRACT_VERSION,
                    dimensions=Embedder.EMBEDDING_DIM,
                    chunk_version=str(active.get("chunk_identity_version") or ""),
                )
                manifest = manifest_for_directory(
                    directory,
                    contract={
                        "chunk_version": metadata.chunk_version,
                        "embedding_model": metadata.embedding_model,
                        "dimensions": metadata.dimensions,
                    },
                )
                if heartbeat_lost.is_set():
                    raise RuntimeError("HA index mirror lease was lost during export")
                if generation["status"] != GEN_PREPARED:
                    generation = self.runtime.index_generations.prepare(
                        generation_id, lease_token, manifest
                    )
                if generation["status"] == GEN_PREPARED:
                    self.runtime.index_repository.materialize(generation, directory)
            if heartbeat_lost.is_set():
                raise RuntimeError("HA index mirror lease was lost during upload")
            latest = KBState(kb_id).active()
            if latest is None or latest.get("id") != local_generation_id:
                try:
                    self.runtime.index_generations.abort(generation_id, lease_token)
                except Exception:
                    pass
                raise RuntimeError("local index advanced while HA mirror was uploading")
            published = self.runtime.publish_generation(generation)
            self._wake.set()
            return published
        finally:
            heartbeat_stop.set()
            keeper.join()

    def reconcile_once(self) -> tuple[int, int]:
        scanned = mirrored = 0
        failures = 0
        for record in self.registry.list():
            if not isinstance(record, Mapping):
                continue
            tenant_id = str(record.get("tenant_id") or "")
            kb_id = str(record.get("storage_id") or "")
            if not tenant_id or not kb_id:
                continue
            if shared_lifecycle_store().status(kb_id) != LIFECYCLE_ACTIVE:
                continue
            active = KBState(kb_id).active()
            if active is None:
                continue
            scanned += 1
            current = self.runtime.index_generations.current(tenant_id, kb_id)
            if current is not None and current["build_id"] == self._build_id(
                str(active["id"])
            ):
                continue
            try:
                self.mirror(tenant_id, kb_id, str(active["id"]))
                mirrored += 1
            except Exception:
                # One corrupt or temporarily unavailable KB must not starve
                # every later tenant in the deterministic registry scan. Keep
                # readiness failed for this pass, but continue bounded repair.
                failures += 1
                LOGGER.exception(
                    "HA index mirror failed for one knowledge base",
                    extra={"tenant_id": tenant_id, "kb_id": kb_id},
                )
        if failures:
            raise RuntimeError(
                f"HA index mirror reconciliation failed for {failures} knowledge base(s)"
            )
        return scanned, mirrored

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._last_error = None
            self._thread = threading.Thread(
                target=self._run,
                name="cogdoc-ha-index-mirror",
                daemon=True,
            )
            self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.reconcile_once()
                self._last_error = None
            except Exception as exc:
                self._last_error = exc
                LOGGER.exception("HA index mirror reconciliation failed")
            self._wake.wait(self.interval_seconds)
            self._wake.clear()

    def wake(self) -> None:
        self._wake.set()

    def stop(self, *, timeout_seconds: float = 10.0) -> bool:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout_seconds)
        stopped = thread is None or not thread.is_alive()
        if stopped:
            self._thread = None
        return stopped

    def check(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive() and self._last_error is None


__all__ = ["HAIndexMirror"]
