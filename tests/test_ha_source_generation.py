from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from cogdoc.ha.api_state import (
    DistributedKnowledgeBaseRegistry,
    DistributedMutationCoordinator,
    StaleMutationFence,
)
from cogdoc.ha.index_generation import IndexGenerationStore
from cogdoc.ha.object_store import LocalObjectStore, ObjectIntegrityError
from cogdoc.ha.outbox import OutboxStore
from cogdoc.ha.source_generation import (
    SOURCE_ACTIVE,
    SourceGenerationStore,
)
from cogdoc.ha.storage import SQLiteBackend


class _Clock:
    def __init__(self) -> None:
        self.value = 100.0

    def __call__(self) -> float:
        return self.value


def _runtime(tmp_path: Path):
    backend = SQLiteBackend(tmp_path / "state.db")
    registry = DistributedKnowledgeBaseRegistry(backend, tmp_path / "node-a")
    record = registry.create("docs", "tenant-a", "owner-a")
    clock = _Clock()
    coordinator = DistributedMutationCoordinator(
        backend, registry, owner_id="node-a", lease_seconds=5, clock=clock
    )
    objects = LocalObjectStore(tmp_path / "objects")
    outbox = OutboxStore(backend, clock=clock)
    generations = SourceGenerationStore(backend, objects, outbox=outbox, clock=clock)
    return backend, record, clock, coordinator, objects, generations


def test_source_generation_is_invisible_until_fenced_publish_and_materializes(
    tmp_path: Path,
) -> None:
    backend, record, _clock, coordinator, _objects, generations = _runtime(tmp_path)
    storage_id = str(record["storage_id"])
    source = tmp_path / "source"
    source.mkdir()
    (source / "report.md").write_text("version one", encoding="utf-8")
    (source / "nested").mkdir()
    (source / "nested" / "facts.txt").write_text("facts", encoding="utf-8")
    lease = coordinator.acquire(storage_id)

    manifest = generations.stage_directory(
        tenant_id="tenant-a", storage_id=storage_id, source_dir=source, lease=lease
    )
    assert generations.current(storage_id) is None
    published = generations.publish(str(manifest["generation_id"]), lease)
    assert published["status"] == SOURCE_ACTIVE
    current_manifest = generations.current_manifest(storage_id)
    assert current_manifest is not None
    assert [item["path"] for item in current_manifest["files"]] == [
        "nested/facts.txt",
        "report.md",
    ]

    replica = tmp_path / "node-b" / "source"
    assert (
        generations.materialize_current(storage_id, replica)
        == manifest["generation_id"]
    )
    assert (replica / "report.md").read_text(encoding="utf-8") == "version one"
    assert (replica / "nested" / "facts.txt").read_text(encoding="utf-8") == "facts"
    assert (
        json.loads((replica / ".cogdoc-source-generation.json").read_text())[
            "generation_id"
        ]
        == manifest["generation_id"]
    )
    with backend.transaction() as connection:
        event = connection.execute(
            "SELECT topic,aggregate_id FROM ha_outbox"
        ).fetchone()
    assert event["topic"] == "kb.source-generation.published"
    assert event["aggregate_id"] == storage_id


def test_missing_source_head_atomically_clears_stale_local_cache(tmp_path: Path) -> None:
    _backend, record, _clock, _coordinator, _objects, generations = _runtime(
        tmp_path
    )
    target = tmp_path / "stale-cache"
    target.mkdir()
    (target / "private.md").write_text("old incarnation", encoding="utf-8")

    assert generations.materialize_current(str(record["storage_id"]), target) is None
    assert target.is_dir()
    assert list(target.iterdir()) == []


def test_expired_generation_cannot_replace_new_lease_owner(tmp_path: Path) -> None:
    backend, record, clock, first, objects, generations = _runtime(tmp_path)
    storage_id = str(record["storage_id"])
    second_registry = DistributedKnowledgeBaseRegistry(backend, tmp_path / "node-b")
    second = DistributedMutationCoordinator(
        backend, second_registry, owner_id="node-b", lease_seconds=5, clock=clock
    )
    source = tmp_path / "source"
    source.mkdir()
    (source / "report.md").write_text("old worker", encoding="utf-8")
    old_lease = first.acquire(storage_id)
    old = generations.stage_directory(
        tenant_id="tenant-a", storage_id=storage_id, source_dir=source, lease=old_lease
    )

    clock.value += 6
    new_lease = second.acquire(storage_id)
    (source / "report.md").write_text("new worker", encoding="utf-8")
    new = generations.stage_directory(
        tenant_id="tenant-a", storage_id=storage_id, source_dir=source, lease=new_lease
    )
    generations.publish(str(new["generation_id"]), new_lease)

    with pytest.raises(StaleMutationFence):
        generations.publish(str(old["generation_id"]), old_lease)
    replica = tmp_path / "replica"
    generations.materialize_current(storage_id, replica)
    assert (replica / "report.md").read_text(encoding="utf-8") == "new worker"
    assert objects.head(str(old["files"][0]["object_key"])) is not None


def test_missing_commit_marker_never_replaces_existing_replica(tmp_path: Path) -> None:
    _backend, record, _clock, coordinator, objects, generations = _runtime(tmp_path)
    storage_id = str(record["storage_id"])
    source = tmp_path / "source"
    source.mkdir()
    (source / "report.md").write_text("new", encoding="utf-8")
    lease = coordinator.acquire(storage_id)
    manifest = generations.stage_directory(
        tenant_id="tenant-a", storage_id=storage_id, source_dir=source, lease=lease
    )
    generations.publish(str(manifest["generation_id"]), lease)
    prefix = str(manifest["files"][0]["object_key"]).split("/files/", 1)[0]
    objects.delete(f"{prefix}/COMMITTED")
    replica = tmp_path / "replica"
    replica.mkdir()
    (replica / "old.md").write_text("old", encoding="utf-8")

    with pytest.raises(ObjectIntegrityError):
        generations.materialize_current(storage_id, replica)
    assert (replica / "old.md").read_text(encoding="utf-8") == "old"


def test_source_snapshot_rejects_symlinks(tmp_path: Path) -> None:
    _backend, record, _clock, coordinator, _objects, generations = _runtime(tmp_path)
    storage_id = str(record["storage_id"])
    source = tmp_path / "source"
    source.mkdir()
    target = tmp_path / "secret.txt"
    target.write_text("secret", encoding="utf-8")
    (source / "link.txt").symlink_to(target)

    with pytest.raises(Exception, match="symlink"):
        generations.stage_directory(
            tenant_id="tenant-a",
            storage_id=storage_id,
            source_dir=source,
            lease=coordinator.acquire(storage_id),
        )


def test_source_and_index_heads_publish_in_one_transaction(tmp_path: Path) -> None:
    backend, record, _clock, coordinator, _objects, sources = _runtime(tmp_path)
    storage_id = str(record["storage_id"])
    source = tmp_path / "source"
    source.mkdir()
    (source / "report.md").write_text("atomic", encoding="utf-8")
    mutation_lease = coordinator.acquire(storage_id)
    source_manifest = sources.stage_directory(
        tenant_id="tenant-a",
        storage_id=storage_id,
        source_dir=source,
        lease=mutation_lease,
    )
    indexes = IndexGenerationStore(backend)
    index = indexes.begin_build("tenant-a", storage_id, "build-1", "node-a")
    index = indexes.prepare(
        str(index["generation_id"]),
        str(index["lease_token"]),
        {
            "schema_version": "index-manifest-v1",
            "contract": {
                "chunk_version": "v1",
                "embedding_model": "test",
                "dimensions": 3,
            },
            "files": [],
        },
    )
    indexes.publish(
        str(index["generation_id"]),
        str(index["lease_token"]),
        lambda _generation: None,
        on_publish=sources.publication_hook(
            str(source_manifest["generation_id"]), mutation_lease
        ),
    )
    assert (
        indexes.current("tenant-a", storage_id)["generation_id"]
        == index["generation_id"]
    )  # type: ignore[index]
    assert (
        sources.current(storage_id)["generation_id"] == source_manifest["generation_id"]
    )  # type: ignore[index]


def test_failed_joint_publication_rolls_back_both_heads(tmp_path: Path) -> None:
    backend, record, _clock, coordinator, _objects, sources = _runtime(tmp_path)
    storage_id = str(record["storage_id"])
    source = tmp_path / "source"
    source.mkdir()
    (source / "report.md").write_text("not visible", encoding="utf-8")
    lease = coordinator.acquire(storage_id)
    source_manifest = sources.stage_directory(
        tenant_id="tenant-a", storage_id=storage_id, source_dir=source, lease=lease
    )
    indexes = IndexGenerationStore(backend)
    index = indexes.begin_build("tenant-a", storage_id, "build-1", "node-a")
    index = indexes.prepare(
        str(index["generation_id"]),
        str(index["lease_token"]),
        {
            "schema_version": "index-manifest-v1",
            "contract": {
                "chunk_version": "v1",
                "embedding_model": "test",
                "dimensions": 3,
            },
            "files": [],
        },
    )
    source_hook = sources.publication_hook(str(source_manifest["generation_id"]), lease)

    def fail_after_source(connection, generation):
        source_hook(connection, generation)
        raise RuntimeError("injected crash before transaction commit")

    with pytest.raises(RuntimeError, match="injected crash"):
        indexes.publish(
            str(index["generation_id"]),
            str(index["lease_token"]),
            lambda _generation: None,
            on_publish=fail_after_source,
        )
    assert indexes.current("tenant-a", storage_id) is None
    assert sources.current(storage_id) is None


def test_database_and_object_backup_restore_preserves_joint_generation(
    tmp_path: Path,
) -> None:
    backend, record, _clock, coordinator, _objects, sources = _runtime(tmp_path)
    storage_id = str(record["storage_id"])
    source = tmp_path / "source"
    source.mkdir()
    (source / "document.md").write_text("backup evidence", encoding="utf-8")
    lease = coordinator.acquire(storage_id)
    source_manifest = sources.stage_directory(
        tenant_id="tenant-a", storage_id=storage_id, source_dir=source, lease=lease
    )
    indexes = IndexGenerationStore(backend)
    index = indexes.begin_build("tenant-a", storage_id, "backup", "node-a")
    index = indexes.prepare(
        str(index["generation_id"]),
        str(index["lease_token"]),
        {
            "schema_version": "index-manifest-v1",
            "contract": {
                "chunk_version": "v1",
                "embedding_model": "test",
                "dimensions": 3,
            },
            "files": [],
        },
    )
    indexes.publish(
        str(index["generation_id"]),
        str(index["lease_token"]),
        lambda _generation: None,
        on_publish=sources.publication_hook(
            str(source_manifest["generation_id"]), lease
        ),
    )
    backend.close()

    restored_root = tmp_path / "restored"
    restored_root.mkdir()
    shutil.copy2(tmp_path / "state.db", restored_root / "state.db")
    shutil.copytree(tmp_path / "objects", restored_root / "objects")
    restored_backend = SQLiteBackend(restored_root / "state.db")
    restored_sources = SourceGenerationStore(
        restored_backend, LocalObjectStore(restored_root / "objects")
    )
    restored_indexes = IndexGenerationStore(restored_backend)
    assert (
        restored_indexes.current("tenant-a", storage_id)["generation_id"]
        == index["generation_id"]
    )  # type: ignore[index]
    restored_source = tmp_path / "restored-source"
    restored_sources.materialize_current(storage_id, restored_source)
    assert (restored_source / "document.md").read_text(encoding="utf-8") == (
        "backup evidence"
    )


def test_cleanup_removes_only_old_noncurrent_source_generations(tmp_path: Path) -> None:
    _backend, record, clock, coordinator, objects, sources = _runtime(tmp_path)
    storage_id = str(record["storage_id"])
    source = tmp_path / "source"
    source.mkdir()
    (source / "document.md").write_text("candidate", encoding="utf-8")
    lease = coordinator.acquire(storage_id)
    candidate = sources.stage_directory(
        tenant_id="tenant-a", storage_id=storage_id, source_dir=source, lease=lease
    )
    clock.value += 10
    rows = sources.garbage_candidates(before=clock.value, limit=10)
    assert [row["generation_id"] for row in rows] == [candidate["generation_id"]]
    sources.delete_generation_objects(rows[0])
    assert sources.forget_collectable(
        str(candidate["generation_id"]), before=clock.value
    )
    assert objects.head(str(candidate["files"][0]["object_key"])) is None
