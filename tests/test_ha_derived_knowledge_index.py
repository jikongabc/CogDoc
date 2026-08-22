from __future__ import annotations

import time

import pytest
from cogdoc.ha.api_state import DistributedKnowledgeBaseRegistry
from cogdoc.ha.derived_knowledge import DistributedDerivedKnowledgeStore
from cogdoc.ha.derived_knowledge_index import HADerivedKnowledgeIndex, _scope_id
from cogdoc.ha.index_generation import IndexConflict
from cogdoc.ha.object_store import LocalObjectStore
from cogdoc.ha.storage import SQLiteBackend
from cogdoc.tools.embedder import Embedder


class _FakeEngine:
    def __init__(self, result, *, before_return=None):
        self.result = result
        self.before_return = before_return
        self.calls = 0

    def search(self, _query, *, top_k, scope=None):
        del top_k, scope
        self.calls += 1
        if self.before_return is not None:
            self.before_return()
        return list(self.result)


def test_derived_generation_is_verified_and_does_not_touch_core_head(
    tmp_path, monkeypatch
):
    backend = SQLiteBackend(tmp_path / "generation.db")
    registry = DistributedKnowledgeBaseRegistry(backend, tmp_path / "source-cache")
    record = registry.create("docs", "tenant", "owner")
    storage_id = str(record["storage_id"])
    assert storage_id == "storage-kb" or storage_id
    store = DistributedDerivedKnowledgeStore(backend)
    row, _ = store.create(
        {"kb_id": storage_id, "text": "Approved knowledge", "status": "pending"}
    )
    store.set_status(row["knowledge_id"], "approved", actor="owner")
    monkeypatch.setattr(Embedder, "EMBEDDING_DIM", 3)
    monkeypatch.setattr(
        Embedder,
        "embed_documents",
        lambda texts: [[float(index + 1), 0.0, 1.0] for index, _ in enumerate(texts)],
    )
    index = HADerivedKnowledgeIndex(
        backend,
        LocalObjectStore(tmp_path / "objects"),
        store,
        registry,
        worker_id="worker",
        cache_root=tmp_path / "cache",
    )

    index.rebuild(storage_id)

    status = index.status(storage_id)
    assert status["state"] == "fresh"
    assert status["approved_count"] == 1
    assert index.generations.current("tenant", storage_id) is None
    assert index.generations.current("tenant", _scope_id(storage_id, 1)) is not None


def test_stale_snapshot_never_advances_derived_head(tmp_path, monkeypatch):
    backend = SQLiteBackend(tmp_path / "stale.db")
    registry = DistributedKnowledgeBaseRegistry(backend, tmp_path / "source-cache")
    record = registry.create("docs", "tenant", "owner")
    storage_id = str(record["storage_id"])
    store = DistributedDerivedKnowledgeStore(backend)
    row, _ = store.create({"kb_id": storage_id, "text": "One"})
    store.set_status(row["knowledge_id"], "approved", actor="owner")
    monkeypatch.setattr(Embedder, "EMBEDDING_DIM", 3)
    monkeypatch.setattr(
        Embedder, "embed_documents", lambda texts: [[1.0, 0.0, 0.0] for _ in texts]
    )
    index = HADerivedKnowledgeIndex(
        backend,
        LocalObjectStore(tmp_path / "objects"),
        store,
        registry,
        worker_id="worker",
        cache_root=tmp_path / "cache",
    )
    original_materialize = index.repository.materialize

    def materialize_then_mutate(generation, source):
        original_materialize(generation, source)
        changed, _ = store.create({"kb_id": storage_id, "text": "Two"})
        store.set_status(changed["knowledge_id"], "approved", actor="owner")

    monkeypatch.setattr(index.repository, "materialize", materialize_then_mutate)

    with pytest.raises(IndexConflict, match="snapshot became stale"):
        index.rebuild(storage_id)

    assert index.generations.current("tenant", _scope_id(storage_id, 1)) is None


def test_recreated_kb_never_reads_previous_incarnation_generation(
    tmp_path, monkeypatch
):
    backend = SQLiteBackend(tmp_path / "incarnation.db")
    registry = DistributedKnowledgeBaseRegistry(backend, tmp_path / "source-cache")
    first = registry.create("docs", "tenant", "owner")
    storage_id = str(first["storage_id"])
    store = DistributedDerivedKnowledgeStore(backend)
    row, _ = store.create({"kb_id": storage_id, "text": "Old private knowledge"})
    store.set_status(row["knowledge_id"], "approved", actor="owner")
    monkeypatch.setattr(Embedder, "EMBEDDING_DIM", 3)
    monkeypatch.setattr(
        Embedder, "embed_documents", lambda texts: [[1.0, 0.0, 0.0] for _ in texts]
    )
    index = HADerivedKnowledgeIndex(
        backend,
        LocalObjectStore(tmp_path / "objects"),
        store,
        registry,
        worker_id="worker",
        cache_root=tmp_path / "cache",
    )
    index.rebuild(storage_id)
    assert index.status(storage_id)["state"] == "fresh"

    assert registry.delete("docs", "tenant") is True
    recreated = registry.create("docs", "tenant", "new-owner")
    assert recreated["storage_id"] == storage_id
    assert int(recreated["epoch"]) > int(first["epoch"])

    status = index.status(storage_id)
    assert status["state"] == "missing"
    assert status["generation_id"] is None
    assert (
        index.generations.current("tenant", _scope_id(storage_id, int(first["epoch"])))
        is not None
    )
    assert (
        index.generations.current(
            "tenant", _scope_id(storage_id, int(recreated["epoch"]))
        )
        is None
    )


def test_stale_approved_snapshot_never_serves_cached_vector_results(
    tmp_path, monkeypatch
):
    backend = SQLiteBackend(tmp_path / "stale-read.db")
    registry = DistributedKnowledgeBaseRegistry(backend, tmp_path / "source-cache")
    record = registry.create("docs", "tenant", "owner")
    storage_id = str(record["storage_id"])
    store = DistributedDerivedKnowledgeStore(backend)
    row, _ = store.create({"kb_id": storage_id, "text": "RETIRED SECRET"})
    store.set_status(row["knowledge_id"], "approved", actor="owner")
    monkeypatch.setattr(Embedder, "EMBEDDING_DIM", 3)
    monkeypatch.setattr(
        Embedder, "embed_documents", lambda texts: [[1.0, 0.0, 0.0] for _ in texts]
    )
    index = HADerivedKnowledgeIndex(
        backend,
        LocalObjectStore(tmp_path / "objects"),
        store,
        registry,
        worker_id="worker",
        cache_root=tmp_path / "cache",
    )
    index.rebuild(storage_id)
    generation = index.generations.current(
        "tenant", _scope_id(storage_id, int(record["epoch"]))
    )
    assert generation is not None
    fake = _FakeEngine([{"text": "RETIRED SECRET", "meta": {}}])
    index._engines[storage_id] = (
        int(record["epoch"]),
        str(generation["generation_id"]),
        fake,
    )

    store.set_status(row["knowledge_id"], "archived", actor="owner")

    assert index.status(storage_id)["state"] == "stale"
    assert index.search(storage_id, "secret", 3) == []
    assert fake.calls == 0


def test_cached_engine_is_rejected_when_kb_epoch_changes_after_head_read(
    tmp_path, monkeypatch
):
    backend = SQLiteBackend(tmp_path / "epoch-race.db")
    registry = DistributedKnowledgeBaseRegistry(backend, tmp_path / "source-cache")
    old = registry.create("docs", "tenant", "owner")
    storage_id = str(old["storage_id"])
    store = DistributedDerivedKnowledgeStore(backend)
    row, _ = store.create({"kb_id": storage_id, "text": "OLD INCARNATION SECRET"})
    store.set_status(row["knowledge_id"], "approved", actor="owner")
    monkeypatch.setattr(Embedder, "EMBEDDING_DIM", 3)
    monkeypatch.setattr(
        Embedder, "embed_documents", lambda texts: [[1.0, 0.0, 0.0] for _ in texts]
    )
    index = HADerivedKnowledgeIndex(
        backend,
        LocalObjectStore(tmp_path / "objects"),
        store,
        registry,
        worker_id="worker",
        cache_root=tmp_path / "cache",
    )
    index.rebuild(storage_id)
    generation = index.generations.current(
        "tenant", _scope_id(storage_id, int(old["epoch"]))
    )
    assert generation is not None
    fake = _FakeEngine([{"text": "OLD INCARNATION SECRET", "meta": {}}])
    index._engines[storage_id] = (
        int(old["epoch"]),
        str(generation["generation_id"]),
        fake,
    )
    assert registry.delete("docs", "tenant") is True
    recreated = registry.create("docs", "tenant", "new-owner")
    assert int(recreated["epoch"]) > int(old["epoch"])
    original_active_record = index._active_record
    calls = 0

    def race_active_record(candidate):
        nonlocal calls
        calls += 1
        return dict(old) if calls == 1 else original_active_record(candidate)

    monkeypatch.setattr(index, "_active_record", race_active_record)

    assert index.search(storage_id, "secret", 3) == []
    assert fake.calls == 0


def test_snapshot_change_during_vector_search_discards_queued_results(
    tmp_path, monkeypatch
):
    backend = SQLiteBackend(tmp_path / "post-search-race.db")
    registry = DistributedKnowledgeBaseRegistry(backend, tmp_path / "source-cache")
    record = registry.create("docs", "tenant", "owner")
    storage_id = str(record["storage_id"])
    store = DistributedDerivedKnowledgeStore(backend)
    row, _ = store.create({"kb_id": storage_id, "text": "SHORT LIVED"})
    store.set_status(row["knowledge_id"], "approved", actor="owner")
    monkeypatch.setattr(Embedder, "EMBEDDING_DIM", 3)
    monkeypatch.setattr(
        Embedder, "embed_documents", lambda texts: [[1.0, 0.0, 0.0] for _ in texts]
    )
    index = HADerivedKnowledgeIndex(
        backend,
        LocalObjectStore(tmp_path / "objects"),
        store,
        registry,
        worker_id="worker",
        cache_root=tmp_path / "cache",
    )
    index.rebuild(storage_id)
    generation = index.generations.current(
        "tenant", _scope_id(storage_id, int(record["epoch"]))
    )
    assert generation is not None
    fake = _FakeEngine(
        [{"text": "SHORT LIVED", "meta": {}}],
        before_return=lambda: store.set_status(
            row["knowledge_id"], "archived", actor="owner"
        ),
    )
    index._engines[storage_id] = (
        int(record["epoch"]),
        str(generation["generation_id"]),
        fake,
    )

    assert index.search(storage_id, "short", 3) == []
    assert fake.calls == 1


def test_durable_refresh_claim_completes_and_failure_remains_recoverable(
    tmp_path, monkeypatch
):
    backend = SQLiteBackend(tmp_path / "refresh-worker.db")
    registry = DistributedKnowledgeBaseRegistry(backend, tmp_path / "source-cache")
    storage_id = str(registry.create("docs", "tenant", "owner")["storage_id"])
    store = DistributedDerivedKnowledgeStore(backend)
    store.create({"kb_id": storage_id, "text": "Queued"})
    index = HADerivedKnowledgeIndex(
        backend,
        LocalObjectStore(tmp_path / "objects"),
        store,
        registry,
        worker_id="worker",
        cache_root=tmp_path / "cache",
    )
    rebuilt = []
    monkeypatch.setattr(
        index,
        "rebuild",
        lambda candidate, _store=None: rebuilt.append((candidate, _store)),
    )

    assert index.refresh_pending(storage_id, store) is True
    assert rebuilt == [(storage_id, store)]
    assert store.pending_refreshes() == []

    store.create({"kb_id": storage_id, "text": "Queued again"})

    def fail(_candidate, _store=None):
        raise OSError("object store unavailable")

    monkeypatch.setattr(index, "rebuild", fail)
    with pytest.raises(OSError, match="object store unavailable"):
        index.refresh_pending(storage_id)
    pending = store.pending_refreshes()
    assert len(pending) == 1
    assert pending[0]["status"] == "pending"
    assert pending[0]["last_error"] == "OSError"


def test_repeated_snapshot_digest_publishes_a_new_occurrence(tmp_path, monkeypatch):
    backend = SQLiteBackend(tmp_path / "snapshot-occurrence.db")
    registry = DistributedKnowledgeBaseRegistry(backend, tmp_path / "source-cache")
    storage_id = str(registry.create("docs", "tenant", "owner")["storage_id"])
    store = DistributedDerivedKnowledgeStore(backend)
    monkeypatch.setattr(Embedder, "EMBEDDING_DIM", 3)
    monkeypatch.setattr(
        Embedder, "embed_documents", lambda texts: [[1.0, 0.0, 0.0] for _ in texts]
    )
    index = HADerivedKnowledgeIndex(
        backend,
        LocalObjectStore(tmp_path / "objects"),
        store,
        registry,
        worker_id="worker",
        cache_root=tmp_path / "cache",
    )
    first, _ = store.create({"kb_id": storage_id, "text": "Approved"})
    store.set_status(first["knowledge_id"], "approved", actor="owner")
    assert index.refresh_pending(storage_id) is True
    head_a = index._current(storage_id)
    assert head_a is not None

    pending, _ = store.create({"kb_id": storage_id, "text": "Not approved"})
    assert index.refresh_pending(storage_id) is True
    head_b = index._current(storage_id)
    assert head_b is not None
    assert head_b["generation_id"] == head_a["generation_id"]

    store.set_status(pending["knowledge_id"], "approved", actor="owner")
    assert index.refresh_pending(storage_id) is True
    head_c = index._current(storage_id)
    assert head_c is not None
    assert head_c["generation_id"] != head_a["generation_id"]

    store.set_status(pending["knowledge_id"], "archived", actor="owner")
    assert index.refresh_pending(storage_id) is True
    head_d = index._current(storage_id)
    assert head_d is not None
    assert head_d["generation_id"] not in {
        head_a["generation_id"],
        head_c["generation_id"],
    }
    assert index.status(storage_id)["state"] == "fresh"
    assert store.pending_refreshes() == []


def test_generation_build_lease_is_renewed_during_slow_embedding(tmp_path, monkeypatch):
    clock = [100.0]
    backend = SQLiteBackend(tmp_path / "build-heartbeat.db")
    registry = DistributedKnowledgeBaseRegistry(backend, tmp_path / "source-cache")
    storage_id = str(registry.create("docs", "tenant", "owner")["storage_id"])
    store = DistributedDerivedKnowledgeStore(backend)
    row, _ = store.create({"kb_id": storage_id, "text": "Slow build"})
    store.set_status(row["knowledge_id"], "approved", actor="owner")
    monkeypatch.setattr(Embedder, "EMBEDDING_DIM", 3)

    def slow_embed(texts):
        clock[0] += 4.0
        time.sleep(0.03)
        clock[0] += 4.0
        time.sleep(0.03)
        return [[1.0, 0.0, 0.0] for _ in texts]

    monkeypatch.setattr(Embedder, "embed_documents", slow_embed)
    index = HADerivedKnowledgeIndex(
        backend,
        LocalObjectStore(tmp_path / "objects"),
        store,
        registry,
        worker_id="worker",
        cache_root=tmp_path / "cache",
        build_lease_seconds=5,
        refresh_lease_seconds=5,
        heartbeat_interval_seconds=0.01,
        clock=lambda: clock[0],
    )

    index.rebuild(storage_id)

    assert index.status(storage_id)["state"] == "fresh"


def test_refresh_claim_is_renewed_during_a_slow_build(tmp_path, monkeypatch):
    clock = [100.0]
    backend = SQLiteBackend(tmp_path / "refresh-heartbeat.db")
    registry = DistributedKnowledgeBaseRegistry(backend, tmp_path / "source-cache")
    storage_id = str(registry.create("docs", "tenant", "owner")["storage_id"])
    store = DistributedDerivedKnowledgeStore(backend, clock=lambda: clock[0])
    store.create({"kb_id": storage_id, "text": "Queued"})
    index = HADerivedKnowledgeIndex(
        backend,
        LocalObjectStore(tmp_path / "objects"),
        store,
        registry,
        worker_id="worker-a",
        cache_root=tmp_path / "cache",
        build_lease_seconds=5,
        refresh_lease_seconds=5,
        heartbeat_interval_seconds=0.01,
        clock=lambda: clock[0],
    )

    def slow_rebuild(_storage_id, _store=None):
        clock[0] += 4.0
        time.sleep(0.03)
        clock[0] += 4.0
        time.sleep(0.03)
        assert store.claim_refresh(storage_id, "worker-b", lease_seconds=5) is None

    monkeypatch.setattr(index, "rebuild", slow_rebuild)

    assert index.refresh_pending(storage_id) is True
    assert store.pending_refreshes() == []
