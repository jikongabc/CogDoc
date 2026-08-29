from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
import threading
import time

import pytest

from cogdoc.ha.api_state import (
    DistributedIndexJobStore,
    DistributedKnowledgeBaseRegistry,
    DistributedMutationCoordinator,
    MutationBusy,
    StaleMutationFence,
)
from cogdoc.ha.storage import SQLiteBackend
from cogdoc.api.ingest import IndexJobManager, KBExistsError
from cogdoc.ha.object_store import LocalObjectStore
from cogdoc.ha.source_generation import SOURCE_PREPARED, SourceGenerationStore
from cogdoc.service.mutation_journal import MutationJournal
from cogdoc.service.kb_lifecycle import LIFECYCLE_ACTIVE, LIFECYCLE_DELETING


class _Clock:
    def __init__(self, value: float = 100.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


def _backends(tmp_path: Path) -> tuple[SQLiteBackend, SQLiteBackend]:
    path = tmp_path / "shared.db"
    return SQLiteBackend(path), SQLiteBackend(path)


def test_distributed_registry_preserves_tombstone_epoch_across_recreate(
    tmp_path: Path,
) -> None:
    first_backend, second_backend = _backends(tmp_path)
    first = DistributedKnowledgeBaseRegistry(first_backend, tmp_path / "cache-a")
    second = DistributedKnowledgeBaseRegistry(second_backend, tmp_path / "cache-b")

    created = first.create("docs", "tenant-a", "owner-a")
    assert second.resolve("docs", "tenant-a") == created
    assert second.source_dir("docs", "tenant-a") != first.source_dir("docs", "tenant-a")
    storage_id = str(created["storage_id"])
    first_epoch = int(created["epoch"])

    first.set(storage_id, LIFECYCLE_DELETING)
    assert second.status(storage_id) == LIFECYCLE_DELETING
    assert second.delete(storage_id)
    assert first.resolve("docs", "tenant-a") is None

    recreated = second.create("docs", "tenant-a", "owner-b")
    assert recreated["storage_id"] == storage_id
    assert recreated["owner_id"] == "owner-b"
    assert int(recreated["epoch"]) > first_epoch
    assert first.status(storage_id) == LIFECYCLE_ACTIVE
    assert first.check()


def test_distributed_registry_create_is_atomic_across_nodes(tmp_path: Path) -> None:
    first_backend, second_backend = _backends(tmp_path)
    first = DistributedKnowledgeBaseRegistry(first_backend, tmp_path / "cache-a")
    second = DistributedKnowledgeBaseRegistry(second_backend, tmp_path / "cache-b")
    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def create(registry: DistributedKnowledgeBaseRegistry, owner_id: str) -> None:
        barrier.wait()
        try:
            registry.create("docs", "tenant-a", owner_id)
        except KBExistsError:
            outcomes.append("exists")
        else:
            outcomes.append("created")

    threads = [
        threading.Thread(target=create, args=(first, "owner-a")),
        threading.Thread(target=create, args=(second, "owner-b")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert all(not thread.is_alive() for thread in threads)
    assert sorted(outcomes) == ["created", "exists"]
    assert len(first.list("tenant-a")) == 1


def test_index_worker_delegates_connector_mutation_lease_without_rebase(
    tmp_path: Path,
) -> None:
    backend = SQLiteBackend(tmp_path / "shared.db")
    registry = DistributedKnowledgeBaseRegistry(backend, tmp_path / "cache")
    record = registry.create("docs", "tenant-a", "owner-a")
    storage_id = str(record["storage_id"])
    coordinator = DistributedMutationCoordinator(
        backend, registry, owner_id="sync-worker", lease_seconds=30
    )
    jobs = DistributedIndexJobStore(
        backend, owner_id="index-worker", lease_seconds=30
    )
    generations = SourceGenerationStore(
        backend, LocalObjectStore(tmp_path / "objects")
    )
    source = Path(registry.source_dir(storage_id))
    source.mkdir(parents=True, exist_ok=True)
    (source / "connector.md").write_text("new connector snapshot", encoding="utf-8")
    observed_tokens: list[str] = []

    def ingest(kb_id: str, source_dir: str, *, on_commit=None):
        assert kb_id == storage_id
        assert Path(source_dir, "connector.md").read_text() == "new connector snapshot"
        lease = coordinator.current_lease()
        assert lease is not None
        observed_tokens.append(lease.lease_token)
        assert on_commit is not None
        on_commit("index-generation-one")
        return SimpleNamespace(document_count=1, chunk_count=1, ocr_summary={})

    manager = IndexJobManager(
        ingest_fn=ingest,
        source_dir_for=registry.source_dir,
        job_store=jobs,
        kb_exists=registry.exists,
        epoch_reader=registry.current,
        lifecycle_reader=registry.status,
        mutation_coordinator=coordinator,
        source_generation_store=generations,
        journal=MutationJournal(tmp_path / "journal.json"),
    )
    with coordinator.lease(storage_id) as lease:
        job = manager.submit_with_mutation_lease(
            storage_id, lease, idempotency_key="sync-one:initial"
        )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            current = manager.get(job["job_id"])
            if current is not None and current["status"] in {"succeeded", "failed"}:
                break
            time.sleep(0.01)
        assert current is not None and current["status"] == "succeeded"
        assert observed_tokens == [lease.lease_token]
        prepared = generations.prepared_for_build(
            storage_id, "index-generation-one", lease
        )
        assert prepared is not None
        assert prepared["fencing_token"] == lease.fencing_token
        replayed = manager.submit_with_mutation_lease(
            storage_id, lease, idempotency_key="sync-one:initial"
        )
        assert replayed["job_id"] == job["job_id"]
        assert observed_tokens == [lease.lease_token]
    manager.shutdown(wait=True)
    backend.close()


def test_legacy_registry_import_is_idempotent_and_preserves_epoch(
    tmp_path: Path,
) -> None:
    backend = SQLiteBackend(tmp_path / "shared.db")
    registry = DistributedKnowledgeBaseRegistry(backend, tmp_path / "cache")
    storage_id = registry.storage_id_for("docs", "tenant-a")
    records = [
        {
            "kb_id": "docs",
            "tenant_id": "tenant-a",
            "owner_id": "owner-a",
            "storage_id": storage_id,
            "created_at": "2026-08-22T00:00:00+00:00",
        }
    ]

    assert registry.import_legacy(records, epochs={storage_id: 17}) == {
        "imported": 1,
        "skipped": 0,
    }
    assert registry.current(storage_id) == 17
    assert registry.import_legacy(records, epochs={storage_id: 17}) == {
        "imported": 0,
        "skipped": 1,
    }


def test_mutation_lease_fences_expired_owner_and_kb_epoch(tmp_path: Path) -> None:
    first_backend, second_backend = _backends(tmp_path)
    registry_a = DistributedKnowledgeBaseRegistry(first_backend, tmp_path / "cache-a")
    registry_b = DistributedKnowledgeBaseRegistry(second_backend, tmp_path / "cache-b")
    storage_id = str(registry_a.create("docs", "tenant-a", "owner")["storage_id"])
    clock = _Clock()
    first = DistributedMutationCoordinator(
        first_backend, registry_a, owner_id="api-a", lease_seconds=5, clock=clock
    )
    second = DistributedMutationCoordinator(
        second_backend, registry_b, owner_id="api-b", lease_seconds=5, clock=clock
    )

    old = first.acquire(storage_id)
    with pytest.raises(MutationBusy):
        second.acquire(storage_id)

    clock.value += 6
    current = second.acquire(storage_id)
    assert current.fencing_token == old.fencing_token + 1
    with pytest.raises(StaleMutationFence):
        first.heartbeat(old)
    with pytest.raises(StaleMutationFence):
        first.assert_live(old)

    registry_b.bump(storage_id)
    with pytest.raises(StaleMutationFence):
        second.assert_live(current)


def test_index_job_store_rejects_stale_worker_update(tmp_path: Path) -> None:
    first_backend, second_backend = _backends(tmp_path)
    clock = _Clock()
    first = DistributedIndexJobStore(
        first_backend, owner_id="worker-a", lease_seconds=5, clock=clock
    )
    second = DistributedIndexJobStore(
        second_backend, owner_id="worker-b", lease_seconds=5, clock=clock
    )
    record = {"job_id": "job-1", "kb_id": "kb-1", "status": "pending"}
    first.create(record)
    token = first.claim("job-1")
    assert token is not None
    with first.bind_claim("job-1", token):
        first.update("job-1", status="running")
        first.heartbeat("job-1", token)

    clock.value += 6
    assert second.reconcile_orphans() == 1
    with first.bind_claim("job-1", token), pytest.raises(StaleMutationFence):
        first.update("job-1", status="succeeded")
    with pytest.raises(StaleMutationFence, match="live claim"):
        first.update("job-1", status="succeeded")
    assert second.get("job-1")["status"] == "failed"  # type: ignore[index]


def test_pending_job_is_not_failed_before_worker_claims_it(tmp_path: Path) -> None:
    backend = SQLiteBackend(tmp_path / "shared.db")
    clock = _Clock()
    store = DistributedIndexJobStore(
        backend, owner_id="worker-a", lease_seconds=5, clock=clock
    )
    store.create({"job_id": "job-1", "kb_id": "kb-1", "status": "pending"})

    clock.value += 100
    assert store.reconcile_orphans() == 0
    assert store.get("job-1")["status"] == "pending"  # type: ignore[index]


def test_distributed_index_job_store_clear_kb_removes_only_scope(
    tmp_path: Path,
) -> None:
    backend = SQLiteBackend(tmp_path / "shared.db")
    store = DistributedIndexJobStore(
        backend, owner_id="worker-a", lease_seconds=30
    )
    store.create({"job_id": "old-kb", "kb_id": "kb-a", "status": "succeeded"})
    store.create(
        {"job_id": "other-kb", "kb_id": "kb-b", "status": "succeeded"}
    )

    store.clear_kb("kb-a")

    assert store.get("old-kb") is None
    assert [row["job_id"] for row in store.list({"kb-b"})] == ["other-kb"]


def test_executor_submit_failure_claims_before_terminal_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = SQLiteBackend(tmp_path / "shared.db")
    registry = DistributedKnowledgeBaseRegistry(backend, tmp_path / "cache")
    storage_id = str(registry.create("docs", "tenant-a", "owner")["storage_id"])
    store = DistributedIndexJobStore(backend, owner_id="api-a", lease_seconds=30)
    coordinator = DistributedMutationCoordinator(
        backend, registry, owner_id="api-a", lease_seconds=30
    )

    def ingest(_kb_id, _source_dir, *, on_commit=None):
        return SimpleNamespace(document_count=0, chunk_count=0)

    manager = IndexJobManager(
        ingest_fn=ingest,
        source_dir_for=registry.source_dir,
        job_store=store,
        kb_exists=registry.exists,
        epoch_reader=registry.current,
        lifecycle_reader=registry.status,
        mutation_coordinator=coordinator,
    )
    monkeypatch.setattr(
        manager,
        "_submit_tracked",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("no thread")),
    )

    with pytest.raises(RuntimeError, match="no thread"):
        manager.submit(storage_id)

    jobs = []
    with backend.transaction() as connection:
        rows = connection.execute(
            "SELECT record_json FROM ha_api_index_jobs"
        ).fetchall()
    for row in rows:
        jobs.append(json.loads(row["record_json"]))
    assert len(jobs) == 1
    assert jobs[0]["status"] == "failed"
    manager.shutdown()


def test_two_index_managers_never_publish_one_kb_concurrently(tmp_path: Path) -> None:
    first_backend, second_backend = _backends(tmp_path)
    registry_a = DistributedKnowledgeBaseRegistry(first_backend, tmp_path / "cache-a")
    registry_b = DistributedKnowledgeBaseRegistry(second_backend, tmp_path / "cache-b")
    storage_id = str(registry_a.create("docs", "tenant-a", "owner")["storage_id"])
    coordinator_a = DistributedMutationCoordinator(
        first_backend, registry_a, owner_id="api-a", lease_seconds=30
    )
    coordinator_b = DistributedMutationCoordinator(
        second_backend, registry_b, owner_id="api-b", lease_seconds=30
    )
    store_a = DistributedIndexJobStore(
        first_backend, owner_id="api-a", lease_seconds=30
    )
    store_b = DistributedIndexJobStore(
        second_backend, owner_id="api-b", lease_seconds=30
    )
    started = threading.Event()
    release = threading.Event()
    published: list[str] = []

    def ingest(_kb_id, _source_dir, *, on_commit=None):
        started.set()
        release.wait(timeout=5)
        assert on_commit is not None
        on_commit("local-generation")
        published.append(threading.current_thread().name)
        return SimpleNamespace(document_count=1, chunk_count=1)

    manager_a = IndexJobManager(
        ingest_fn=ingest,
        source_dir_for=lambda _kb: str(tmp_path / "cache-a"),
        job_store=store_a,
        kb_exists=registry_a.exists,
        epoch_reader=registry_a.current,
        lifecycle_reader=registry_a.status,
        mutation_coordinator=coordinator_a,
    )
    manager_b = IndexJobManager(
        ingest_fn=ingest,
        source_dir_for=lambda _kb: str(tmp_path / "cache-b"),
        job_store=store_b,
        kb_exists=registry_b.exists,
        epoch_reader=registry_b.current,
        lifecycle_reader=registry_b.status,
        mutation_coordinator=coordinator_b,
    )
    first_job = manager_a.submit(storage_id)
    assert started.wait(timeout=2)
    second_job = manager_b.submit(storage_id)

    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if manager_b.get(second_job["job_id"])["status"] == "failed":  # type: ignore[index]
            break
        time.sleep(0.01)
    release.set()
    manager_a.shutdown()
    manager_b.shutdown()

    assert store_a.get(first_job["job_id"])["status"] == "succeeded"  # type: ignore[index]
    assert store_b.get(second_job["job_id"])["status"] == "failed"  # type: ignore[index]
    assert len(published) == 1


def test_upload_stages_durable_source_before_local_index_switch(tmp_path: Path) -> None:
    backend = SQLiteBackend(tmp_path / "shared.db")
    registry = DistributedKnowledgeBaseRegistry(backend, tmp_path / "cache")
    storage_id = str(registry.create("docs", "tenant-a", "owner")["storage_id"])
    coordinator = DistributedMutationCoordinator(
        backend, registry, owner_id="api-a", lease_seconds=30
    )
    job_store = DistributedIndexJobStore(backend, owner_id="api-a", lease_seconds=30)
    sources = SourceGenerationStore(backend, LocalObjectStore(tmp_path / "objects"))
    observed: list[str] = []

    def ingest(_kb_id, _source_dir, *, on_commit=None):
        assert on_commit is not None
        on_commit("local-generation-1")
        with backend.transaction() as connection:
            row = connection.execute(
                "SELECT status FROM ha_source_generations WHERE build_id=?",
                ("local-generation-1",),
            ).fetchone()
        observed.append(str(row["status"]))
        return SimpleNamespace(document_count=1, chunk_count=1)

    manager = IndexJobManager(
        ingest_fn=ingest,
        source_dir_for=registry.source_dir,
        job_store=job_store,
        kb_exists=registry.exists,
        journal=MutationJournal(str(tmp_path / "journal")),
        epoch_reader=registry.current,
        lifecycle_reader=registry.status,
        mutation_coordinator=coordinator,
        source_generation_store=sources,
    )
    source_dir = registry.source_dir(storage_id)
    job = manager.submit_upload(storage_id, source_dir, "document.md", b"durable")
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if job_store.get(job["job_id"])["status"] in {"succeeded", "failed"}:  # type: ignore[index]
            break
        time.sleep(0.01)
    manager.shutdown()

    assert job_store.get(job["job_id"])["status"] == "succeeded"  # type: ignore[index]
    assert observed == [SOURCE_PREPARED]
    assert sources.current(storage_id) is None
    with backend.transaction() as connection:
        prepared = connection.execute(
            "SELECT manifest_key FROM ha_source_generations WHERE build_id=?",
            ("local-generation-1",),
        ).fetchone()
    assert prepared is not None
