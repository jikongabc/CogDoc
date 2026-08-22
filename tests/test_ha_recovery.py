from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import cogdoc.ha.recovery as recovery_module
from cogdoc.ha.api_state import (
    DistributedKnowledgeBaseRegistry,
    DistributedMutationCoordinator,
)
from cogdoc.ha.migration_catalog import REGISTERED_MIGRATIONS
from cogdoc.ha.migrations import MigrationRunner
from cogdoc.ha.object_store import ObjectIndexRepository
from cogdoc.ha.recovery import HARecoveryManifest, RecoveryManifestError
from cogdoc.ha.runtime import HAConfig, HARuntime, manifest_for_directory
from cogdoc.source_model import build_version_id


def _runtime(tmp_path: Path) -> HARuntime:
    return HARuntime(
        HAConfig(
            enabled=True,
            database_url="",
            database_schema="cogdoc",
            object_store="local",
            object_root=str(tmp_path / "objects"),
            s3_bucket="",
            s3_prefix="cogdoc",
            s3_endpoint_url=None,
            s3_region=None,
            s3_require_versioning=True,
            worker_id="recovery-test",
            scheduler_enabled=False,
            outbox_enabled=False,
            maintenance_enabled=False,
            index_worker_enabled=False,
        )
    )


def _published_state(tmp_path: Path):
    runtime = _runtime(tmp_path)
    MigrationRunner(runtime.backend, REGISTERED_MIGRATIONS, owner_id="migration").run()
    registry = DistributedKnowledgeBaseRegistry(
        runtime.backend, tmp_path / "source-cache"
    )
    kb = registry.create("docs", "tenant", "owner")
    storage_id = str(kb["storage_id"])
    coordinator = DistributedMutationCoordinator(
        runtime.backend, registry, owner_id="api", lease_seconds=30
    )
    source = tmp_path / "source"
    source.mkdir()
    source_content = b"source generation"
    (source / "document.md").write_bytes(source_content)
    index = tmp_path / "index"
    index.mkdir()
    (index / "index.bin").write_bytes(b"portable index")
    with coordinator.lease(storage_id) as lease:
        source_manifest = runtime.source_generations.stage_directory(
            tenant_id="tenant",
            storage_id=storage_id,
            source_dir=source,
            lease=lease,
        )
        generation = runtime.index_generations.begin_build(
            "tenant", storage_id, "build", "worker"
        )
        manifest = manifest_for_directory(
            index,
            contract={
                "chunk_version": "v1",
                "embedding_model": "model",
                "dimensions": 3,
            },
        )
        prepared = runtime.index_generations.prepare(
            generation["generation_id"], generation["lease_token"], manifest
        )
        runtime.index_repository.materialize(prepared, index)
        runtime.index_generations.publish(
            prepared["generation_id"],
            prepared["lease_token"],
            runtime.index_repository.verify,
            on_publish=runtime.source_generations.publication_hook(
                source_manifest["generation_id"], lease
            ),
        )
    artifact = b"raw artifact"
    artifact_digest = hashlib.sha256(artifact).hexdigest()
    artifact_source = "source-one"
    artifact_version = build_version_id(artifact_source, artifact_digest)
    runtime.source_artifact_store.put(
        "tenant",
        storage_id,
        artifact_source,
        artifact_version,
        artifact,
        content_sha256=artifact_digest,
        media_type="text/plain",
        display_name="raw.txt",
        created_at=1,
    )
    recovery = HARecoveryManifest(
        runtime.backend, runtime.object_store, runtime.source_generations
    )
    return runtime, recovery


def test_recovery_manifest_inventories_and_verifies_all_authority(
    tmp_path: Path,
) -> None:
    runtime, recovery = _published_state(tmp_path)
    derived_directory = tmp_path / "derived"
    derived_directory.mkdir()
    (derived_directory / "knowledge.bin").write_bytes(b"derived knowledge")
    derived_scope = "derived-" + "a" * 64
    generation = runtime.index_generations.begin_build(
        "tenant", derived_scope, "derived-build", "worker"
    )
    derived_manifest = manifest_for_directory(
        derived_directory,
        contract={
            "chunk_version": "derived-knowledge-v1",
            "embedding_model": "model",
            "dimensions": 3,
        },
    )
    prepared = runtime.index_generations.prepare(
        generation["generation_id"], generation["lease_token"], derived_manifest
    )
    derived_repository = ObjectIndexRepository(
        runtime.object_store, prefix="derived-knowledge-indexes"
    )
    derived_repository.materialize(prepared, derived_directory)
    runtime.index_generations.publish(
        prepared["generation_id"],
        prepared["lease_token"],
        derived_repository.verify,
    )

    manifest = recovery.capture(
        "pgdump-2026-08-22", database_sha256="a" * 64, verify_content=True
    )
    verified = recovery.verify(manifest, verify_content=True)

    kinds = {row["kind"] for row in manifest["objects"]}
    assert kinds == {
        "index_file",
        "index_manifest",
        "derived_index_file",
        "derived_index_manifest",
        "source_artifact",
        "source_commit",
        "source_file",
        "source_manifest",
    }
    assert verified["objects"] == len(manifest["objects"])
    assert verified["bytes"] == sum(row["byte_size"] for row in manifest["objects"])
    assert manifest["artifact_count"] == 1
    assert {row["index_kind"] for row in manifest["index_heads"]} == {
        "documents",
        "derived_knowledge",
    }
    assert [row["version"] for row in manifest["migrations"]] == [
        1,
        2,
        3,
        4,
        5,
        6,
            7,
            8,
            9,
        ]
    runtime.shutdown()


def test_recovery_manifest_detects_checksum_missing_object_and_version_drift(
    tmp_path: Path,
) -> None:
    runtime, recovery = _published_state(tmp_path)
    manifest = recovery.capture("snapshot")
    changed = json.loads(json.dumps(manifest))
    changed["database_snapshot_id"] = "forged"
    with pytest.raises(RecoveryManifestError, match="checksum"):
        recovery.verify(changed)

    target = manifest["objects"][0]
    runtime.object_store.delete(target["key"])
    with pytest.raises(RecoveryManifestError, match="missing or corrupt"):
        recovery.verify(manifest)
    runtime.shutdown()


def test_recovery_manifest_atomic_file_round_trip(tmp_path: Path) -> None:
    runtime, recovery = _published_state(tmp_path)
    manifest = recovery.capture("snapshot")
    path = recovery.write(tmp_path / "backup" / "recovery.json", manifest)

    loaded = recovery.read(path)

    assert loaded == manifest
    assert recovery.verify(loaded)["objects"] > 0
    recovery.verify_database_authority(loaded)
    assert list(path.parent.glob(".*.tmp")) == []
    runtime.shutdown()


def test_recovery_capture_rejects_invalid_snapshot_and_database_digest(
    tmp_path: Path,
) -> None:
    runtime, recovery = _published_state(tmp_path)
    with pytest.raises(ValueError, match="snapshot"):
        recovery.capture("")
    with pytest.raises(ValueError, match="database_sha256"):
        recovery.capture("snapshot", database_sha256="bad")
    runtime.shutdown()


def test_recovery_capture_rejects_unvalidated_schema_and_corrupt_authority(
    tmp_path: Path,
) -> None:
    runtime, recovery = _published_state(tmp_path)
    marker = runtime.backend.sql(sqlite="?", postgres="%s")
    with runtime.backend.transaction(write=True) as connection:
        connection.execute(
            f"UPDATE ha_schema_migrations SET phase={marker} WHERE version={marker}",
            ("backfill", 2),
        )
    with pytest.raises(RecoveryManifestError, match="validated recovery version"):
        recovery.capture("snapshot")
    with runtime.backend.transaction(write=True) as connection:
        connection.execute(
            f"UPDATE ha_schema_migrations SET phase={marker} WHERE version={marker}",
            ("validated", 2),
        )
        connection.execute(
            "UPDATE ha_source_artifacts SET object_key="
            f"{marker} WHERE source_id={marker}",
            ("source-artifacts/wrong", "source-one"),
        )
    with pytest.raises(RecoveryManifestError, match="object key"):
        recovery.capture("snapshot")
    runtime.shutdown()


def test_recovery_manifest_enforces_file_and_object_bounds(
    tmp_path: Path, monkeypatch
) -> None:
    runtime, recovery = _published_state(tmp_path)
    manifest = recovery.capture("snapshot")
    monkeypatch.setattr(recovery_module, "MAX_RECOVERY_OBJECTS", 1)
    with pytest.raises(RecoveryManifestError, match="object list"):
        recovery.verify(manifest)

    path = tmp_path / "oversized.json"
    path.write_bytes(b"{}" * 32)
    monkeypatch.setattr(recovery_module, "MAX_RECOVERY_MANIFEST_BYTES", 16)
    with pytest.raises(RecoveryManifestError, match="too large"):
        recovery.read(path)
    runtime.shutdown()
