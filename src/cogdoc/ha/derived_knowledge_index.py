from __future__ import annotations

import hashlib
import json
import math
import shutil
import tempfile
import threading
import time
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from cogdoc.ha.index_generation import (
    GEN_BUILDING,
    GEN_PREPARED,
    GEN_PUBLISHED,
    IndexConflict,
    IndexGenerationStore,
    StaleIndexFence,
)
from cogdoc.ha.object_store import ObjectIndexRepository, ObjectStore
from cogdoc.ha.portable_index import (
    PORTABLE_INDEX_FILENAME,
    PortableIndexInstaller,
    PortableIndexStore,
)
from cogdoc.ha.runtime import manifest_for_directory
from cogdoc.ha.storage import DatabaseBackend, DatabaseConnection
from cogdoc.graph.state import RetrievedDoc
from cogdoc.tools.retriever.scope import RetrievalScope


DERIVED_KNOWLEDGE_CHUNK_VERSION = "derived-knowledge-v1"


def _snapshot_build_id(digest: str, event_sequence: int) -> str:
    """Identify one occurrence of a snapshot, not only its content.

    An approved set can legitimately return to an older digest after archive or
    delete operations.  The event watermark prevents that later occurrence from
    resolving to an already-published generation whose head is no longer current.
    """

    return f"{digest}:{event_sequence}"


def _build_digest(build_id: Any) -> str:
    """Read the content identity from both legacy and occurrence build IDs."""

    return str(build_id or "").partition(":")[0]


@contextmanager
def _lease_heartbeat(
    heartbeat: Callable[[], None], *, interval_seconds: float
) -> Iterator[None]:
    stopped = threading.Event()
    failure: list[BaseException] = []

    def run() -> None:
        while not stopped.wait(interval_seconds):
            try:
                heartbeat()
            except BaseException as exc:  # keep stale capability observable
                failure.append(exc)
                stopped.set()
                return

    thread = threading.Thread(
        target=run,
        name="cogdoc-derived-index-heartbeat",
        daemon=True,
    )
    thread.start()
    try:
        yield
    finally:
        stopped.set()
        thread.join(max(1.0, interval_seconds * 2))
    if failure:
        raise StaleIndexFence("derived index lease heartbeat failed") from failure[0]


def _scope_id(storage_id: str, kb_epoch: int) -> str:
    if type(kb_epoch) is not int or kb_epoch < 1:
        raise ValueError("knowledge-base epoch is invalid")
    identity = f"{storage_id}\0{kb_epoch}".encode()
    return "derived-" + hashlib.sha256(identity).hexdigest()


def _indexed_documents(rows: Sequence[Mapping[str, Any]]) -> list[RetrievedDoc]:
    documents: list[RetrievedDoc] = []
    for ordinal, row in enumerate(rows):
        knowledge_id = str(row["knowledge_id"])
        text = str(row.get("text") or "")
        source_hash = str(row.get("related_source_sha256") or "")
        if len(source_hash) != 64 or any(
            ch not in "0123456789abcdef" for ch in source_hash
        ):
            source_hash = hashlib.sha256(text.encode()).hexdigest()
        source = str(row.get("related_source") or f"knowledge:{knowledge_id}")
        metadata = {
            "chunk_id": f"knowledge:{knowledge_id}",
            "document_id": knowledge_id,
            "source": source,
            "source_sha256": source_hash,
            "origin": str(row.get("origin") or "manual_entry"),
            "local_chunk_index": ordinal,
            "chunk_index": ordinal,
            "page": 0,
            "page_start": 0,
            "page_end": 0,
            "source_type": "derived_knowledge",
            "knowledge_id": knowledge_id,
        }
        context = str(row.get("source_note") or "")
        if context:
            metadata["context"] = context
        documents.append({"text": text, "meta": metadata})  # type: ignore[typeddict-item]
    return documents


def _canonical_rows(rows: Sequence[Mapping[str, Any]]) -> str:
    encoded = json.dumps(
        _indexed_documents(rows),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


class HADerivedKnowledgeIndex:
    """Immutable portable generations for approved derived knowledge.

    A build is published only after the object repository verifies every byte
    and the publication transaction proves the KB epoch and approved snapshot
    are still identical. The core document-index head is never touched.
    """

    def __init__(
        self,
        backend: DatabaseBackend,
        object_store: ObjectStore,
        knowledge_store: Any,
        registry: Any,
        *,
        worker_id: str,
        cache_root: str | Path,
        reader_lease_seconds: float = 600.0,
        build_lease_seconds: float = 300.0,
        refresh_lease_seconds: float = 3600.0,
        heartbeat_interval_seconds: float = 30.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.backend = backend
        self.object_store = object_store
        self.knowledge_store = knowledge_store
        self.registry = registry
        self.worker_id = worker_id
        if (
            isinstance(reader_lease_seconds, bool)
            or not isinstance(reader_lease_seconds, (int, float))
            or not math.isfinite(reader_lease_seconds)
            or not 5 <= reader_lease_seconds <= 3600
        ):
            raise ValueError("derived index reader lease is invalid")
        self.reader_lease_seconds = float(reader_lease_seconds)
        if (
            isinstance(build_lease_seconds, bool)
            or not isinstance(build_lease_seconds, (int, float))
            or not math.isfinite(build_lease_seconds)
            or not 5 <= build_lease_seconds <= 3600
        ):
            raise ValueError("derived index build lease is invalid")
        if (
            isinstance(refresh_lease_seconds, bool)
            or not isinstance(refresh_lease_seconds, (int, float))
            or not math.isfinite(refresh_lease_seconds)
            or not 5 <= refresh_lease_seconds <= 3600
        ):
            raise ValueError("derived index refresh lease is invalid")
        if (
            isinstance(heartbeat_interval_seconds, bool)
            or not isinstance(heartbeat_interval_seconds, (int, float))
            or not math.isfinite(heartbeat_interval_seconds)
            or not 0.01
            <= heartbeat_interval_seconds
            < min(build_lease_seconds, refresh_lease_seconds)
        ):
            raise ValueError("derived index heartbeat interval is invalid")
        self.build_lease_seconds = float(build_lease_seconds)
        self.refresh_lease_seconds = float(refresh_lease_seconds)
        self.heartbeat_interval_seconds = float(heartbeat_interval_seconds)
        self.cache_root = Path(cache_root)
        self.cache_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.generations = IndexGenerationStore(backend, clock=clock)
        self.repository = ObjectIndexRepository(
            object_store, prefix="derived-knowledge-indexes"
        )
        self._installer = PortableIndexInstaller()
        # A physical storage id is reused when a tenant deletes and recreates
        # the same logical KB.  Epoch is therefore part of the cache identity;
        # generation_id alone is not an incarnation fence.
        self._engines: dict[str, tuple[int, str, Any]] = {}
        self._lock = threading.RLock()
        self._build_locks: dict[str, threading.Lock] = {}
        self._last_error: dict[str, str] = {}

    def _snapshot(
        self,
        storage_id: str,
        connection: DatabaseConnection | None = None,
    ) -> tuple[str, int, list[dict[str, Any]]]:
        marker = self.backend.sql(sqlite="?", postgres="%s")
        query = (
            "SELECT record_json,event_sequence FROM ha_derived_knowledge_events WHERE "
            f"kb_id={marker} AND event_sequence IN (SELECT MAX(event_sequence) "
            "FROM ha_derived_knowledge_events "
            f"WHERE kb_id={marker} GROUP BY knowledge_id)"
        )
        params = (storage_id, storage_id)

        def read_rows(
            reader: DatabaseConnection,
        ) -> tuple[list[Any], Any | None]:
            # Writers append/delete events before updating this per-KB row in
            # the same transaction. Locking the watermark first makes the
            # following event snapshot linearizable on PostgreSQL READ COMMITTED.
            lock = self.backend.sql(sqlite="", postgres=" FOR SHARE")
            watermark = reader.execute(
                "SELECT requested_sequence FROM ha_derived_knowledge_refreshes "
                f"WHERE kb_id={marker}{lock}",
                (storage_id,),
            ).fetchone()
            return reader.execute(query, params).fetchall(), watermark

        if connection is None:
            with self.backend.transaction() as reader:
                rows, watermark_row = read_rows(reader)
        else:
            rows, watermark_row = read_rows(connection)
        decoded = []
        event_sequence = 0
        for row in rows:
            raw = row[0] if not isinstance(row, Mapping) else row["record_json"]
            raw_sequence = (
                row[1] if not isinstance(row, Mapping) else row["event_sequence"]
            )
            event_sequence = max(event_sequence, int(raw_sequence))
            value = json.loads(str(raw))
            if value.get("kb_id") == storage_id and value.get("status") == "approved":
                decoded.append(value)
        if watermark_row is not None:
            event_sequence = max(
                event_sequence,
                int(
                    watermark_row[0]
                    if not isinstance(watermark_row, Mapping)
                    else watermark_row["requested_sequence"]
                ),
            )
        decoded.sort(key=lambda item: str(item.get("knowledge_id") or ""))
        return _canonical_rows(decoded), event_sequence, decoded

    @staticmethod
    def _documents(rows: Sequence[Mapping[str, Any]]) -> list[RetrievedDoc]:
        return _indexed_documents(rows)

    def _active_record(self, storage_id: str) -> dict[str, Any] | None:
        marker = self.backend.sql(sqlite="?", postgres="%s")
        with self.backend.transaction() as connection:
            row = connection.execute(
                "SELECT storage_id,tenant_id,kb_id,owner_id,epoch,revision "
                "FROM ha_api_knowledge_bases "
                f"WHERE storage_id={marker} AND lifecycle='active'",
                (storage_id,),
            ).fetchone()
        if row is None:
            return None
        return dict(row)

    def rebuild(self, storage_id: str, _store: Any | None = None) -> None:
        with self._lock:
            build_lock = self._build_locks.setdefault(storage_id, threading.Lock())
        with build_lock:
            self._rebuild(storage_id, _store)

    def refresh_pending(self, storage_id: str, _store: Any | None = None) -> bool:
        """Claim and durably finish one queued refresh for this KB."""

        if _store is not None and _store is not self.knowledge_store:
            raise ValueError("knowledge store does not belong to this HA index")
        refreshed = False
        while True:
            claim = self.knowledge_store.claim_refresh(
                storage_id,
                self.worker_id,
                lease_seconds=self.refresh_lease_seconds,
            )
            if claim is None:
                return refreshed
            token = str(claim["lease_token"])
            sequence = int(claim["requested_sequence"])
            try:
                with _lease_heartbeat(
                    lambda: self._heartbeat_refresh(storage_id, token),
                    interval_seconds=self.heartbeat_interval_seconds,
                ):
                    self.rebuild(storage_id, self.knowledge_store)
            except Exception as exc:
                self.knowledge_store.fail_refresh(
                    storage_id,
                    token,
                    sequence,
                    type(exc).__name__,
                )
                self.record_error(storage_id, type(exc).__name__)
                raise
            if not self.knowledge_store.complete_refresh(storage_id, token, sequence):
                # The lease was fenced by another worker. Its owner now owns
                # both publication and durable completion.
                return refreshed
            refreshed = True
            # Completing an older request changes the same outbox row back to
            # pending. Claim again before returning so a cross-node mutation
            # that arrived during the build cannot wait until another startup.

    def _heartbeat_refresh(self, storage_id: str, lease_token: str) -> None:
        if not self.knowledge_store.heartbeat_refresh(
            storage_id,
            lease_token,
            lease_seconds=self.refresh_lease_seconds,
        ):
            raise StaleIndexFence("derived refresh lease is stale")

    def _heartbeat_generation(self, generation_id: str, lease_token: str) -> None:
        try:
            self.generations.heartbeat(
                generation_id,
                lease_token,
                lease_seconds=self.build_lease_seconds,
            )
        except StaleIndexFence:
            # publish() atomically leaves the renewable states. A heartbeat
            # already in flight may observe that transition before the owner
            # exits its heartbeat context. Only our exact capability reaching
            # the immutable published state is a successful terminal race.
            current = self.generations.get(generation_id)
            if (
                current is not None
                and current.get("status") == GEN_PUBLISHED
                and current.get("lease_token") == lease_token
            ):
                return
            raise

    def recover_pending(self, *, batch_size: int = 1000) -> int:
        """Drain recoverable refresh rows without rebuilding active leases."""

        completed = 0
        while True:
            rows = self.knowledge_store.pending_refreshes(limit=batch_size)
            if not rows:
                return completed
            progressed = False
            for row in rows:
                storage_id = str(row["kb_id"])
                try:
                    refreshed = self.refresh_pending(storage_id)
                except Exception:
                    # Failure is durable and intentionally ends this startup
                    # sweep; request-time enqueue or the next startup retries.
                    continue
                progressed = refreshed or progressed
                completed += int(refreshed)
            if not progressed:
                return completed

    def _rebuild(self, storage_id: str, _store: Any | None = None) -> None:
        if _store is not None and _store is not self.knowledge_store:
            raise ValueError("knowledge store does not belong to this HA index")
        record = self._active_record(storage_id)
        if record is None:
            raise ValueError("knowledge-base is unavailable")
        tenant_id = str(record["tenant_id"])
        expected_epoch = int(record["epoch"])
        snapshot_digest, event_sequence, rows = self._snapshot(storage_id)
        build_id = _snapshot_build_id(snapshot_digest, event_sequence)
        scope = _scope_id(storage_id, expected_epoch)
        current = self.generations.current(tenant_id, scope)
        if (
            current is not None
            and _build_digest(current.get("build_id")) == snapshot_digest
        ):
            self.repository.verify(current)
            return
        generation = self.generations.begin_build(
            tenant_id,
            scope,
            build_id,
            self.worker_id,
            base_generation_id=(
                str(current["generation_id"]) if current is not None else None
            ),
            lease_seconds=self.build_lease_seconds,
        )
        if generation.get("status") == GEN_PUBLISHED:
            # A published generation can only be returned here when this exact
            # event occurrence is already current (handled above). Treat any
            # other result as a corrupt generation/head relationship.
            raise IndexConflict("published derived generation is not current")
        token = str(generation["lease_token"])
        generation_id = str(generation["generation_id"])
        with _lease_heartbeat(
            lambda: self._heartbeat_generation(generation_id, token),
            interval_seconds=self.heartbeat_interval_seconds,
        ):
            temporary = Path(
                tempfile.mkdtemp(prefix="derived-build-", dir=self.cache_root)
            )
            try:
                portable = temporary / PORTABLE_INDEX_FILENAME
                documents = self._documents(rows)
                from cogdoc.tools.embedder import Embedder

                embeddings = (
                    Embedder.embed_documents(
                        [str(document["text"]) for document in documents]
                    )
                    if documents
                    else []
                )
                PortableIndexStore().write(
                    portable,
                    documents,
                    embeddings,
                    embedding_model=Embedder.EMBEDDING_CONTRACT_VERSION,
                    dimensions=Embedder.EMBEDDING_DIM,
                    chunk_version=DERIVED_KNOWLEDGE_CHUNK_VERSION,
                )
                PortableIndexStore().verify(portable)
                manifest = manifest_for_directory(
                    temporary,
                    contract={
                        "chunk_version": DERIVED_KNOWLEDGE_CHUNK_VERSION,
                        "embedding_model": Embedder.EMBEDDING_CONTRACT_VERSION,
                        "dimensions": Embedder.EMBEDDING_DIM,
                    },
                )
                if generation.get("status") == GEN_BUILDING:
                    generation = self.generations.prepare(
                        generation_id, token, manifest
                    )
                if generation.get("status") != GEN_PREPARED:
                    raise IndexConflict("derived knowledge build is not publishable")
                self.repository.materialize(generation, temporary)

                def validate_authority(
                    connection: DatabaseConnection, generation: Mapping[str, Any]
                ) -> None:
                    del generation
                    marker = self.backend.sql(sqlite="?", postgres="%s")
                    lock = self.backend.sql(sqlite="", postgres=" FOR SHARE")
                    kb = connection.execute(
                        "SELECT tenant_id,lifecycle,epoch FROM ha_api_knowledge_bases "
                        f"WHERE storage_id={marker}{lock}",
                        (storage_id,),
                    ).fetchone()
                    if kb is None:
                        raise IndexConflict("knowledge-base disappeared during build")
                    tenant = kb[0] if not isinstance(kb, Mapping) else kb["tenant_id"]
                    lifecycle = (
                        kb[1] if not isinstance(kb, Mapping) else kb["lifecycle"]
                    )
                    epoch = kb[2] if not isinstance(kb, Mapping) else kb["epoch"]
                    live_digest, _live_sequence, _live_rows = self._snapshot(
                        storage_id, connection
                    )
                    if (
                        str(tenant) != tenant_id
                        or str(lifecycle) != "active"
                        or int(epoch) != expected_epoch
                        or live_digest != snapshot_digest
                    ):
                        raise IndexConflict("derived knowledge snapshot became stale")

                published = self.generations.publish(
                    generation_id,
                    token,
                    self.repository.verify,
                    on_publish=validate_authority,
                )
                with self._lock:
                    self._engines.pop(storage_id, None)
                    self._last_error.pop(storage_id, None)
                self.repository.verify(published)
            finally:
                shutil.rmtree(temporary, ignore_errors=True)

    def _current(self, storage_id: str) -> dict[str, Any] | None:
        record = self._active_record(storage_id)
        if record is None:
            return None
        return self.generations.resolve_current(
            str(record["tenant_id"]),
            _scope_id(storage_id, int(record["epoch"])),
            self.repository.verify,
        )

    def _generation_is_current(
        self,
        storage_id: str,
        tenant_id: str,
        kb_epoch: int,
        generation_id: str,
    ) -> bool:
        """Revalidate every authority input after an unbounded read boundary."""

        def same_incarnation(record: Mapping[str, Any] | None) -> bool:
            return bool(
                record is not None
                and str(record["tenant_id"]) == tenant_id
                and int(record["epoch"]) == kb_epoch
            )

        if not same_incarnation(self._active_record(storage_id)):
            return False
        generation = self.generations.current(
            tenant_id, _scope_id(storage_id, kb_epoch)
        )
        if generation is None or str(generation["generation_id"]) != generation_id:
            return False
        snapshot_digest, event_sequence, _rows = self._snapshot(storage_id)
        return bool(
            _build_digest(generation.get("build_id")) == snapshot_digest
            and same_incarnation(self._active_record(storage_id))
        )

    def _engine(self, storage_id: str) -> tuple[str, int, str, Any] | None:
        record = self._active_record(storage_id)
        if record is None:
            return None
        tenant_id = str(record["tenant_id"])
        kb_epoch = int(record["epoch"])
        scope = _scope_id(storage_id, kb_epoch)
        generation = self.generations.resolve_current(
            tenant_id, scope, self.repository.verify
        )
        if generation is None:
            return None
        generation_id = str(generation["generation_id"])
        snapshot_digest, _event_sequence, _rows = self._snapshot(storage_id)
        if _build_digest(generation.get("build_id")) != snapshot_digest:
            return None
        with self._lock:
            cached = self._engines.get(storage_id)
        if cached is not None and cached[:2] == (kb_epoch, generation_id):
            if self._generation_is_current(
                storage_id, tenant_id, kb_epoch, generation_id
            ):
                return tenant_id, kb_epoch, generation_id, cached[2]
            with self._lock:
                if self._engines.get(storage_id) is cached:
                    self._engines.pop(storage_id, None)
            return None

        leased = self.generations.acquire_reader(
            tenant_id,
            scope,
            self.worker_id,
            lease_seconds=self.reader_lease_seconds,
        )
        if leased is None:
            return None
        reader_id = str(leased["reader_id"])
        lease_token = str(leased["reader_lease_token"])
        try:
            # The head may have advanced between resolve_current() and lease
            # acquisition. Always install the generation frozen by the lease.
            generation_id = str(leased["generation_id"])
            self.repository.verify(leased)
            directory = self.repository.materialize_local(
                leased, self.cache_root / generation_id
            )
            engine = self._installer.install(
                scope, generation_id, directory / PORTABLE_INDEX_FILENAME
            )
        finally:
            self.generations.release_reader(reader_id, lease_token)
        if not self._generation_is_current(
            storage_id, tenant_id, kb_epoch, generation_id
        ):
            return None
        with self._lock:
            self._engines[storage_id] = (kb_epoch, generation_id, engine)
        return tenant_id, kb_epoch, generation_id, engine

    def ensure_fresh(self, storage_id: str) -> None:
        # Reads never build or advance authority. A missing/stale generation
        # falls back to the shared lexical ledger in DerivedKnowledgeRetriever.
        self._engine(storage_id)

    def search(
        self,
        storage_id: str,
        query: str,
        top_k: int,
        *,
        scope: RetrievalScope | None = None,
    ) -> list[RetrievedDoc]:
        resolved = self._engine(storage_id)
        if resolved is None:
            return []
        tenant_id, kb_epoch, generation_id, engine = resolved
        rows = list(engine.search(query, top_k=top_k, scope=scope))
        if not self._generation_is_current(
            storage_id, tenant_id, kb_epoch, generation_id
        ):
            return []
        return rows

    def search_many(
        self,
        storage_id: str,
        queries: Sequence[str],
        top_k: int,
        *,
        scope: RetrievalScope | None = None,
    ) -> list[list[RetrievedDoc]]:
        resolved = self._engine(storage_id)
        if resolved is None:
            return [[] for _ in queries]
        tenant_id, kb_epoch, generation_id, engine = resolved
        rows = [
            list(row) for row in engine.search_many(queries, top_k=top_k, scope=scope)
        ]
        if not self._generation_is_current(
            storage_id, tenant_id, kb_epoch, generation_id
        ):
            return [[] for _ in queries]
        return rows

    def status(self, storage_id: str) -> dict[str, Any]:
        digest, event_sequence, rows = self._snapshot(storage_id)
        del event_sequence
        generation = self._current(storage_id)
        indexed = _build_digest(generation.get("build_id")) if generation else ""
        return {
            "kb_id": storage_id,
            "state": (
                "missing"
                if generation is None
                else ("fresh" if indexed == digest else "stale")
            ),
            "current_revision_token": digest,
            "indexed_revision_token": indexed or None,
            "approved_count": len(rows),
            "generation_id": generation.get("generation_id") if generation else None,
            "last_error": self._last_error.get(storage_id),
        }

    def record_error(self, storage_id: str, error_class: str) -> None:
        with self._lock:
            self._last_error[storage_id] = str(error_class)


__all__ = ["DERIVED_KNOWLEDGE_CHUNK_VERSION", "HADerivedKnowledgeIndex"]
