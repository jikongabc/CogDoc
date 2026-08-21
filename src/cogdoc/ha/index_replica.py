from __future__ import annotations

import hashlib
import shutil
import threading
from collections import OrderedDict
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from cogdoc.ha.index_generation import IndexGenerationStore, normalize_manifest
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

    def __init__(self, replica: HAIndexReplica, registry: Any) -> None:
        self.replica = replica
        self.registry = registry

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
        return self.replica.get_engine(str(record["tenant_id"]), kb_id)


__all__ = ["HAIndexReplica", "IndexReplicaError", "RegistryIndexProvider"]
