from __future__ import annotations

import hashlib
import os
import shutil
import threading
from collections import OrderedDict
from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from cogdoc.ha.index_generation import (
    GEN_PUBLISHED,
    IndexGenerationStore,
    StaleIndexFence,
    normalize_manifest,
)
from cogdoc.ha.object_store import ObjectIndexRepository
from cogdoc.ha.portable_index import (
    PORTABLE_INDEX_FILENAME,
    PortableIndexInstaller,
    PortableIndexIntegrityError,
    PortableIndexStore,
)


class IndexReplicaError(RuntimeError):
    pass


class HAIndexReplica:
    """Verified local retrieval cache whose only authority is the DB head."""

    def __init__(
        self,
        generations: IndexGenerationStore,
        repository: ObjectIndexRepository,
        cache_root: str | Path,
        *,
        installer: PortableIndexInstaller | None = None,
        max_cached_engines: int = 32,
    ) -> None:
        if type(max_cached_engines) is not int or not 1 <= max_cached_engines <= 1024:
            raise ValueError("max_cached_engines is invalid")
        self.generations = generations
        self.repository = repository
        self.cache_root = Path(cache_root)
        self.cache_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.cache_root.is_symlink():
            raise ValueError("HA index replica cache root is unsafe")
        self.installer = installer or PortableIndexInstaller()
        self.max_cached_engines = max_cached_engines
        self._engines: OrderedDict[tuple[str, str, str], Any] = OrderedDict()
        self._locks: dict[tuple[str, str], threading.Lock] = {}
        self._lock = threading.RLock()
        self._integrity_errors: dict[tuple[str, str, str], str] = {}

    def record_integrity_result(
        self,
        tenant_id: str,
        kb_id: str,
        generation_id: str,
        error: BaseException | None,
    ) -> None:
        key = (tenant_id, kb_id, generation_id)
        with self._lock:
            if error is None:
                self._integrity_errors.pop(key, None)
            else:
                self._integrity_errors[key] = type(error).__name__

    def check(self) -> bool:
        try:
            return bool(
                self.cache_root.is_dir()
                and not self.cache_root.is_symlink()
                and os.access(self.cache_root, os.R_OK | os.W_OK | os.X_OK)
                and not self._integrity_errors
            )
        except OSError:
            return False

    @staticmethod
    def _scope(value: str) -> str:
        return hashlib.sha256(value.encode()).hexdigest()

    def _generation_path(self, tenant_id: str, kb_id: str, generation_id: str) -> Path:
        return (
            self.cache_root
            / self._scope(tenant_id)
            / self._scope(kb_id)
            / generation_id
        )

    def _scope_lock(self, tenant_id: str, kb_id: str) -> threading.Lock:
        key = (tenant_id, kb_id)
        with self._lock:
            return self._locks.setdefault(key, threading.Lock())

    def get_engine(self, tenant_id: str, kb_id: str) -> Any:
        from cogdoc.tools.retriever.base_retriever import NullRetriever
        from cogdoc.tools.retriever.hybrid import HybridRetriever

        with self._scope_lock(tenant_id, kb_id):
            for _attempt in range(3):
                generation = self.generations.current(tenant_id, kb_id)
                if generation is None:
                    return HybridRetriever(NullRetriever(), NullRetriever())
                generation_id = str(generation["generation_id"])
                key = (tenant_id, kb_id, generation_id)
                with self._lock:
                    cached = self._engines.get(key)
                    if cached is not None:
                        self._engines.move_to_end(key)
                if cached is None:
                    directory = self._generation_path(tenant_id, kb_id, generation_id)
                    self.repository.materialize_local(generation, directory)
                    portable = directory / PORTABLE_INDEX_FILENAME
                    metadata = PortableIndexStore().verify(portable)
                    manifest, _digest = normalize_manifest(generation["manifest"])
                    contract = manifest["contract"]
                    if (
                        metadata.embedding_model != contract["embedding_model"]
                        or metadata.dimensions != contract["dimensions"]
                        or metadata.chunk_version != contract["chunk_version"]
                    ):
                        raise PortableIndexIntegrityError(
                            "portable index does not match generation contract"
                        )
                    cached = self.installer.install(kb_id, generation_id, portable)
                current = self.generations.current(tenant_id, kb_id)
                if current is None or current["generation_id"] != generation_id:
                    continue
                with self._lock:
                    self._engines[key] = cached
                    self._engines.move_to_end(key)
                    while len(self._engines) > self.max_cached_engines:
                        self._engines.popitem(last=False)
                return cached
        raise IndexReplicaError(
            "index head changed repeatedly while loading its replica"
        )

    def get_engine_for_generation(
        self, tenant_id: str, kb_id: str, generation_id: str
    ) -> Any:
        """Load one immutable published generation without consulting the head."""

        from cogdoc.tools.retriever.base_retriever import NullRetriever
        from cogdoc.tools.retriever.hybrid import HybridRetriever

        with self._scope_lock(tenant_id, kb_id):
            generation = self.generations.get(generation_id)
            if generation is None:
                raise IndexReplicaError("pinned index generation no longer exists")
            if (
                str(generation.get("tenant_id") or "") != tenant_id
                or str(generation.get("kb_id") or "") != kb_id
                or generation.get("status") != GEN_PUBLISHED
            ):
                raise IndexReplicaError("pinned index generation scope is invalid")
            key = (tenant_id, kb_id, generation_id)
            with self._lock:
                cached = self._engines.get(key)
                if cached is not None:
                    self._engines.move_to_end(key)
                    return cached
            manifest, _digest = normalize_manifest(generation["manifest"])
            if not manifest["files"]:
                cached = HybridRetriever(NullRetriever(), NullRetriever())
            else:
                directory = self._generation_path(tenant_id, kb_id, generation_id)
                self.repository.materialize_local(generation, directory)
                portable = directory / PORTABLE_INDEX_FILENAME
                metadata = PortableIndexStore().verify(portable)
                contract = manifest["contract"]
                if (
                    metadata.embedding_model != contract["embedding_model"]
                    or metadata.dimensions != contract["dimensions"]
                    or metadata.chunk_version != contract["chunk_version"]
                ):
                    raise PortableIndexIntegrityError(
                        "portable index does not match generation contract"
                    )
                cached = self.installer.install(kb_id, generation_id, portable)
            with self._lock:
                self._engines[key] = cached
                self._engines.move_to_end(key)
                while len(self._engines) > self.max_cached_engines:
                    self._engines.popitem(last=False)
            return cached

    def invalidate(self, tenant_id: str, kb_id: str) -> None:
        with self._lock:
            stale = [key for key in self._engines if key[:2] == (tenant_id, kb_id)]
            for key in stale:
                del self._engines[key]

    def prune_local(self, tenant_id: str, kb_id: str, *, keep: int = 2) -> int:
        if type(keep) is not int or not 1 <= keep <= 100:
            raise ValueError("replica keep count is invalid")
        current = self.generations.current(tenant_id, kb_id)
        current_id = None if current is None else str(current["generation_id"])
        scope = self._generation_path(tenant_id, kb_id, "placeholder").parent
        if not scope.exists():
            return 0
        candidates = sorted(
            (
                path
                for path in scope.iterdir()
                if path.is_dir() and not path.is_symlink() and path.name != current_id
            ),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        removed = 0
        for path in candidates[max(0, keep - (1 if current_id else 0)) :]:
            shutil.rmtree(path)
            removed += 1
        return removed


class RegistryIndexProvider:
    """Resolve process-internal storage IDs without exposing tenant identities."""

    def __init__(
        self,
        replica: HAIndexReplica,
        registry: Any,
        *,
        worker_id: str = "api-reader",
        reader_lease_seconds: float = 600.0,
    ) -> None:
        if not worker_id or worker_id != worker_id.strip():
            raise ValueError("index reader worker_id is invalid")
        if not 15 <= reader_lease_seconds <= 3600:
            raise ValueError("index reader lease_seconds is invalid")
        self.replica = replica
        self.registry = registry
        self.worker_id = worker_id
        self.reader_lease_seconds = float(reader_lease_seconds)
        self._pinned: ContextVar[dict[str, tuple[str, str]]] = ContextVar(
            f"cogdoc_ha_index_pin_{id(self)}", default={}
        )
        self._active_pin_lock = threading.RLock()
        self._active_pins: dict[str, dict[tuple[str, str], int]] = {}

    def _assert_no_detached_pin(self, kb_id: str) -> None:
        """Never borrow another request's generation outside its context."""

        with self._active_pin_lock:
            active = self._active_pins.get(kb_id, {})
            if active:
                raise StaleIndexFence(
                    "index retrieval lost its pinned execution context"
                )

    def _register_active_pin(
        self, kb_id: str, tenant_id: str, generation_id: str
    ) -> None:
        key = (tenant_id, generation_id)
        with self._active_pin_lock:
            active = self._active_pins.setdefault(kb_id, {})
            active[key] = active.get(key, 0) + 1

    def _unregister_active_pin(
        self, kb_id: str, tenant_id: str, generation_id: str
    ) -> None:
        key = (tenant_id, generation_id)
        with self._active_pin_lock:
            active = self._active_pins.get(kb_id)
            if active is None or key not in active:
                return
            if active[key] <= 1:
                del active[key]
            else:
                active[key] -= 1
            if not active:
                self._active_pins.pop(kb_id, None)

    def scrub_current(self, *, limit: int = 100) -> bool:
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("index scrub limit is invalid")
        healthy = True
        for generation in self.replica.generations.list_current(limit=limit):
            tenant_id = str(generation["tenant_id"])
            kb_id = str(generation["kb_id"])
            generation_id = str(generation["generation_id"])
            try:
                self.replica.get_engine_for_generation(tenant_id, kb_id, generation_id)
            except BaseException as exc:
                self.replica.record_integrity_result(
                    tenant_id, kb_id, generation_id, exc
                )
                healthy = False
            else:
                self.replica.record_integrity_result(
                    tenant_id, kb_id, generation_id, None
                )
        return healthy and self.replica.check()

    def check(self) -> bool:
        return self.replica.check()

    def __call__(self, kb_id: str) -> Any:
        from cogdoc.tools.retriever.base_retriever import NullRetriever
        from cogdoc.tools.retriever.hybrid import HybridRetriever

        record = self.registry.get_by_storage_id(kb_id)
        if (
            not isinstance(record, Mapping)
            or str(record.get("storage_id") or "") != kb_id
            or not record.get("tenant_id")
        ):
            return HybridRetriever(NullRetriever(), NullRetriever())
        tenant_id = str(record["tenant_id"])
        pinned = self._pinned.get().get(kb_id)
        if pinned is not None:
            pinned_tenant, generation_id = pinned
            if pinned_tenant != tenant_id:
                raise IndexReplicaError("pinned index tenant changed")
            if not generation_id:
                return HybridRetriever(NullRetriever(), NullRetriever())
            return self.replica.get_engine_for_generation(
                tenant_id, kb_id, generation_id
            )
        self._assert_no_detached_pin(kb_id)
        return self.replica.get_engine(tenant_id, kb_id)

    @contextmanager
    def pin(self, kb_id: str):
        """Pin every retrieval in the current execution context to one head."""

        record = self.registry.get_by_storage_id(kb_id)
        if (
            not isinstance(record, Mapping)
            or str(record.get("storage_id") or "") != kb_id
            or not record.get("tenant_id")
        ):
            raise IndexReplicaError("knowledge base scope is unavailable")
        tenant_id = str(record["tenant_id"])
        reader = self.replica.generations.acquire_reader(
            tenant_id,
            kb_id,
            self.worker_id,
            lease_seconds=self.reader_lease_seconds,
        )
        if reader is None:
            generation_id = ""
            reader_id = ""
            lease_token = ""
        else:
            generation_id = str(reader["generation_id"])
            reader_id = str(reader["reader_id"])
            lease_token = str(reader["reader_lease_token"])
        stopped = threading.Event()
        heartbeat_error: list[BaseException] = []

        def heartbeat() -> None:
            interval = max(5.0, self.reader_lease_seconds / 3)
            while not stopped.wait(interval):
                try:
                    self.replica.generations.heartbeat_reader(
                        reader_id,
                        lease_token,
                        lease_seconds=self.reader_lease_seconds,
                    )
                except BaseException as exc:
                    heartbeat_error.append(exc)
                    stopped.set()

        def check_reader() -> None:
            if heartbeat_error:
                raise StaleIndexFence(
                    "index reader lease heartbeat failed"
                ) from heartbeat_error[0]
            if reader_id:
                self.replica.generations.heartbeat_reader(
                    reader_id,
                    lease_token,
                    lease_seconds=self.reader_lease_seconds,
                )

        thread = None
        if reader_id:
            thread = threading.Thread(
                target=heartbeat,
                name="cogdoc-ha-index-reader",
                daemon=True,
            )
            thread.start()
        if generation_id:
            try:
                self.replica.get_engine_for_generation(tenant_id, kb_id, generation_id)
            except BaseException as exc:
                self.replica.record_integrity_result(
                    tenant_id, kb_id, generation_id, exc
                )
                stopped.set()
                if thread is not None:
                    thread.join(timeout=min(5.0, self.reader_lease_seconds))
                try:
                    self.replica.generations.release_reader(reader_id, lease_token)
                except Exception:
                    pass
                raise
            else:
                self.replica.record_integrity_result(
                    tenant_id, kb_id, generation_id, None
                )
        current = dict(self._pinned.get())
        current[kb_id] = (tenant_id, generation_id)
        token = self._pinned.set(current)
        self._register_active_pin(kb_id, tenant_id, generation_id)
        try:
            yield {
                "tenant_id": tenant_id,
                "kb_id": kb_id,
                "generation_id": generation_id,
                "check": check_reader,
            }
            check_reader()
        finally:
            self._pinned.reset(token)
            self._unregister_active_pin(kb_id, tenant_id, generation_id)
            stopped.set()
            if thread is not None:
                thread.join(timeout=min(5.0, self.reader_lease_seconds))
            if reader_id:
                try:
                    self.replica.generations.release_reader(reader_id, lease_token)
                except Exception:
                    # Expiry-based GC remains the durable cleanup fallback.
                    pass


__all__ = ["HAIndexReplica", "IndexReplicaError", "RegistryIndexProvider"]
