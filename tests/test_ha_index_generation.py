from __future__ import annotations

import hashlib
import threading

import pytest

from cogdoc.ha.index_generation import (
    GEN_PREPARED,
    GEN_PUBLISHED,
    IndexConflict,
    IndexGenerationStore,
    IndexIntegrityError,
    LocalIndexRepository,
    StaleIndexFence,
)
from cogdoc.ha.storage import SQLiteBackend


class Clock:
    def __init__(self, value=1000.0):
        self.value = value

    def __call__(self):
        return self.value


def _manifest(files):
    return {
        "schema_version": "index-manifest-v1",
        "contract": {
            "chunk_version": "v7",
            "embedding_model": "test-model",
            "dimensions": 3,
        },
        "files": [
            {
                "path": name,
                "sha256": hashlib.sha256(content).hexdigest(),
                "byte_size": len(content),
            }
            for name, content in files.items()
        ],
    }


@pytest.fixture
def index(tmp_path):
    backend = SQLiteBackend(tmp_path / "ha.db")
    clock = Clock()
    store = IndexGenerationStore(backend, clock=clock)
    repository = LocalIndexRepository(tmp_path / "indexes")
    yield store, repository, clock, tmp_path
    backend.close()


def _prepare(index, build="build-1", files=None):
    store, repository, _clock, tmp_path = index
    files = files or {"vector/data.bin": b"vector", "bm25/index.bin": b"bm25"}
    source = tmp_path / build
    for name, content in files.items():
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    generation = store.begin_build("tenant", "kb", build, "worker")
    prepared = store.prepare(
        generation["generation_id"], generation["lease_token"], _manifest(files)
    )
    repository.materialize(prepared, source)
    return prepared


def test_publication_switches_head_only_after_full_verification(index):
    store, repository, _clock, _tmp_path = index
    prepared = _prepare(index)
    assert prepared["status"] == GEN_PREPARED
    assert store.current("tenant", "kb") is None
    published = store.publish(
        prepared["generation_id"], prepared["lease_token"], repository.verify
    )
    assert published["status"] == GEN_PUBLISHED
    assert store.current("tenant", "kb")["generation_id"] == published["generation_id"]


def test_crash_after_durable_generation_before_db_publish_keeps_old_head(index):
    store, repository, _clock, _tmp_path = index
    first = _prepare(index, "first", {"index.bin": b"old"})
    store.publish(first["generation_id"], first["lease_token"], repository.verify)
    old = store.current("tenant", "kb")
    second = _prepare(index, "second", {"index.bin": b"new"})
    repository.verify(second)
    # Simulated process loss before publish CAS.
    assert store.current("tenant", "kb")["generation_id"] == old["generation_id"]


def test_publication_hook_is_atomic_with_current_pointer(index):
    store, repository, _clock, _tmp_path = index
    prepared = _prepare(index)

    def fail(_connection, _generation):
        raise RuntimeError("outbox unavailable")

    with pytest.raises(RuntimeError, match="outbox"):
        store.publish(
            prepared["generation_id"],
            prepared["lease_token"],
            repository.verify,
            on_publish=fail,
        )
    assert store.current("tenant", "kb") is None
    assert store.get(prepared["generation_id"])["status"] == GEN_PREPARED


def test_newer_fence_prevents_stale_builder_publication(index):
    store, repository, _clock, tmp_path = index
    old = _prepare(index, "old", {"index.bin": b"old"})
    newer = store.begin_build("tenant", "kb", "new", "worker-2")
    with pytest.raises(StaleIndexFence, match="superseded"):
        store.publish(old["generation_id"], old["lease_token"], repository.verify)
    assert store.current("tenant", "kb") is None
    assert newer["fencing_token"] > old["fencing_token"]


def test_expired_lease_requires_resume_and_rotates_token(index):
    store, repository, clock, _tmp_path = index
    prepared = _prepare(index)
    clock.value += 301
    with pytest.raises(StaleIndexFence, match="expired"):
        store.publish(
            prepared["generation_id"], prepared["lease_token"], repository.verify
        )
    resumed = store.resume_build(prepared["generation_id"], "replacement")
    assert resumed["lease_token"] != prepared["lease_token"]
    published = store.publish(
        resumed["generation_id"], resumed["lease_token"], repository.verify
    )
    assert published["status"] == GEN_PUBLISHED


def test_stable_worker_id_reclaims_expired_build_with_a_new_capability(index):
    store, repository, clock, _tmp_path = index
    prepared = _prepare(index)
    clock.value += 301

    reclaimed = store.begin_build("tenant", "kb", "build-1", "worker")

    assert reclaimed["generation_id"] == prepared["generation_id"]
    assert reclaimed["lease_token"] != prepared["lease_token"]
    with pytest.raises(StaleIndexFence):
        store.publish(
            prepared["generation_id"], prepared["lease_token"], repository.verify
        )
    published = store.publish(
        reclaimed["generation_id"], reclaimed["lease_token"], repository.verify
    )
    assert published["status"] == GEN_PUBLISHED


def test_corrupt_or_unmanifested_files_never_become_current(index):
    store, repository, _clock, _tmp_path = index
    prepared = _prepare(index)
    target = repository._target(prepared)
    (target / "vector/data.bin").write_bytes(b"tampered")
    with pytest.raises(IndexIntegrityError, match="metadata|corrupt"):
        store.publish(
            prepared["generation_id"], prepared["lease_token"], repository.verify
        )
    assert store.current("tenant", "kb") is None


def test_reader_refuses_corruption_after_publication(index):
    store, repository, _clock, _tmp_path = index
    prepared = _prepare(index)
    store.publish(prepared["generation_id"], prepared["lease_token"], repository.verify)
    current = store.resolve_current("tenant", "kb", repository.verify)
    assert current["generation_id"] == prepared["generation_id"]
    (repository._target(prepared) / "vector/data.bin").write_bytes(b"tampered")
    with pytest.raises(IndexIntegrityError):
        store.resolve_current("tenant", "kb", repository.verify)


def test_delayed_gc_never_returns_current_generation(index):
    store, repository, clock, _tmp_path = index
    first = _prepare(index, "first", {"index.bin": b"old"})
    store.publish(first["generation_id"], first["lease_token"], repository.verify)
    second = _prepare(index, "second", {"index.bin": b"new"})
    store.publish(second["generation_id"], second["lease_token"], repository.verify)
    clock.value += 3600
    candidates = store.garbage_candidates(before=clock.value)
    assert [row["generation_id"] for row in candidates] == [first["generation_id"]]
    repository.delete_generation(first)
    assert store.forget_collectable(first["generation_id"], before=clock.value)
    assert not store.forget_collectable(second["generation_id"], before=clock.value)
    repository.verify(store.resolve_current("tenant", "kb", repository.verify))


def test_live_reader_lease_protects_superseded_generation_from_gc(index):
    store, _repository, clock, _tmp_path = index
    first = _prepare(index, "reader-first", {"index.bin": b"old"})
    store.publish(first["generation_id"], first["lease_token"], lambda _: None)
    reader = store.acquire_reader("tenant", "kb", "api-node", lease_seconds=30)
    assert reader is not None
    second = _prepare(index, "reader-second", {"index.bin": b"new"})
    store.publish(second["generation_id"], second["lease_token"], lambda _: None)
    clock.value += 10

    assert store.garbage_candidates(before=clock.value) == []
    assert not store.forget_collectable(first["generation_id"], before=clock.value)
    store.heartbeat_reader(
        str(reader["reader_id"]),
        str(reader["reader_lease_token"]),
        lease_seconds=30,
    )
    assert store.release_reader(
        str(reader["reader_id"]), str(reader["reader_lease_token"])
    )
    assert [
        row["generation_id"] for row in store.garbage_candidates(before=clock.value)
    ] == [first["generation_id"]]


def test_expired_reader_lease_is_collectable_and_prunable(index):
    store, _repository, clock, _tmp_path = index
    first = _prepare(index, "expired-first", {"index.bin": b"old"})
    store.publish(first["generation_id"], first["lease_token"], lambda _: None)
    reader = store.acquire_reader("tenant", "kb", "api-node", lease_seconds=5)
    assert reader is not None
    second = _prepare(index, "expired-second", {"index.bin": b"new"})
    store.publish(second["generation_id"], second["lease_token"], lambda _: None)
    clock.value += 6

    assert [
        row["generation_id"] for row in store.garbage_candidates(before=clock.value)
    ] == [first["generation_id"]]
    with pytest.raises(StaleIndexFence, match="reader lease"):
        store.heartbeat_reader(
            str(reader["reader_id"]),
            str(reader["reader_lease_token"]),
            lease_seconds=5,
        )
    assert store.prune_reader_leases(before=clock.value) == 1


def test_expired_prepared_generation_is_collectable_after_kb_head_is_deleted(index):
    store, _repository, clock, _tmp_path = index
    prepared = _prepare(index, "delete-race", {"index.bin": b"orphan"})
    with store.backend.transaction(write=True) as connection:
        connection.execute(
            "DELETE FROM ha_index_heads WHERE tenant_id=? AND kb_id=?",
            ("tenant", "kb"),
        )
    clock.value += 10_000

    assert [
        row["generation_id"] for row in store.garbage_candidates(before=clock.value)
    ] == [prepared["generation_id"]]
    assert store.forget_collectable(prepared["generation_id"], before=clock.value)


def test_current_generation_listing_is_stable_and_cursor_paginated(tmp_path):
    backend = SQLiteBackend(tmp_path / "authority.db")
    store = IndexGenerationStore(backend)
    for tenant_id, kb_id in (("b", "one"), ("a", "two"), ("a", "one")):
        generation = store.begin_build(tenant_id, kb_id, "build", "worker")
        prepared = store.prepare(
            generation["generation_id"], generation["lease_token"], _manifest({})
        )
        store.publish(
            prepared["generation_id"], prepared["lease_token"], lambda _: None
        )

    first = store.list_current(limit=2)
    assert [(row["tenant_id"], row["kb_id"]) for row in first] == [
        ("a", "one"),
        ("a", "two"),
    ]
    second = store.list_current(limit=2, after=("a", "two"))
    assert [(row["tenant_id"], row["kb_id"]) for row in second] == [("b", "one")]
    backend.close()


def test_materialize_rejects_symlinked_source_component(index):
    store, repository, _clock, tmp_path = index
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "index.bin").write_bytes(b"secret")
    source = tmp_path / "source"
    source.mkdir()
    (source / "linked").symlink_to(outside, target_is_directory=True)
    generation = store.begin_build("tenant", "kb", "symlink", "worker")
    prepared = store.prepare(
        generation["generation_id"],
        generation["lease_token"],
        _manifest({"linked/index.bin": b"secret"}),
    )
    with pytest.raises(IndexIntegrityError, match="symlink"):
        repository.materialize(prepared, source)


def test_concurrent_publish_has_single_winner_and_never_mixes_files(index):
    store, repository, _clock, _tmp_path = index
    prepared = _prepare(index)
    barrier = threading.Barrier(2)
    results = []

    def publish():
        barrier.wait()
        try:
            results.append(
                store.publish(
                    prepared["generation_id"],
                    prepared["lease_token"],
                    repository.verify,
                )["status"]
            )
        except (StaleIndexFence, IndexConflict):
            results.append("fenced")

    threads = [threading.Thread(target=publish) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results.count(GEN_PUBLISHED) == 1
    assert store.current("tenant", "kb")["generation_id"] == prepared["generation_id"]
    repository.verify(store.current("tenant", "kb"))


@pytest.mark.parametrize(
    "path", ["../escape", "/absolute", "same/../path", "back\\slash"]
)
def test_manifest_rejects_unsafe_paths(index, path):
    store, _repository, _clock, _tmp_path = index
    generation = store.begin_build("tenant", "kb", f"build-{hash(path)}", "worker")
    manifest = _manifest({path: b"content"})
    with pytest.raises(ValueError, match="path"):
        store.prepare(generation["generation_id"], generation["lease_token"], manifest)
