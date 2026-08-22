from __future__ import annotations

import hashlib
import sqlite3
import time
from pathlib import Path

import pytest

from cogdoc.ha.object_store import LocalObjectStore
from cogdoc.ha.source_artifact_store import DistributedSourceArtifactStore
from cogdoc.ha.storage import SQLiteBackend
from cogdoc.service.source_artifact_store import (
    ArtifactConflictError,
    ArtifactIntegrityError,
    ArtifactLimitError,
    ArtifactNotFoundError,
)
from cogdoc.source_model import build_version_id


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _version(content: bytes, source: str = "source-one") -> str:
    return build_version_id(source, _digest(content))


def _stores(
    tmp_path: Path, **limits: int
) -> tuple[
    DistributedSourceArtifactStore, DistributedSourceArtifactStore, LocalObjectStore
]:
    database = tmp_path / "shared.db"
    objects = LocalObjectStore(tmp_path / "objects")
    defaults = {
        "max_file_bytes": 1024 * 1024,
        "max_total_bytes": 8 * 1024 * 1024,
        "max_bytes_per_tenant": 4 * 1024 * 1024,
        "max_versions_per_source": 4,
        "user_max_versions_per_source": 3,
    }
    defaults.update(limits)
    return (
        DistributedSourceArtifactStore(
            SQLiteBackend(database), objects, owner_id="node-a", **defaults
        ),
        DistributedSourceArtifactStore(
            SQLiteBackend(database), objects, owner_id="node-b", **defaults
        ),
        objects,
    )


def _item(content: bytes, *, source: str = "source-one", created: float = 1.0):
    return {
        "source_id": source,
        "version_id": _version(content, source),
        "content_sha256": _digest(content),
        "byte_size": len(content),
        "media_type": "text/plain",
        "display_name": "source.txt",
        "created_at": created,
    }


def _put(
    store: DistributedSourceArtifactStore,
    content: bytes,
    *,
    tenant: str = "tenant",
    kb: str = "kb",
    source: str = "source-one",
    reservation_token: str | None = None,
    created: float = 1.0,
):
    return store.put(
        tenant,
        kb,
        source,
        _version(content, source),
        content,
        content_sha256=_digest(content),
        media_type="text/plain",
        display_name="source.txt",
        created_at=created,
        reservation_token=reservation_token,
    )


def test_two_nodes_share_immutable_artifacts_and_scope(tmp_path: Path) -> None:
    first, second, _objects = _stores(tmp_path)
    content = b"shared immutable bytes"

    created = _put(first, content)

    assert second.read("tenant", "kb", "source-one", created["version_id"]) == content
    assert _put(second, content, created=2.0) == created
    with pytest.raises(ArtifactNotFoundError):
        second.read("other", "kb", "source-one", created["version_id"])
    with pytest.raises(ArtifactIntegrityError, match="content address"):
        first.put(
            "tenant",
            "kb",
            "source-one",
            "not-content-addressed",
            content,
            content_sha256=_digest(content),
            media_type="text/plain",
            display_name="source.txt",
            created_at=1,
        )


def test_reservation_is_cluster_atomic_idempotent_and_consumed(tmp_path: Path) -> None:
    first, second, _objects = _stores(tmp_path, max_total_bytes=10)
    one = b"123456"
    two = b"abcdef"
    token = first.reserve_batch("tenant", "kb", [_item(one)], reservation_key="job")
    assert (
        first.reserve_batch("tenant", "kb", [_item(one)], reservation_key="job")
        == token
    )
    assert first.reservation_usage("tenant", "kb") == {
        "reservations": 1,
        "reserved_versions": 1,
        "reserved_bytes": len(one),
    }
    with pytest.raises(ArtifactLimitError, match="max_total"):
        second.reserve_batch("tenant", "kb", [_item(two)], reservation_key="other")
    _put(first, one, reservation_token=token)
    assert second.reservation_usage("tenant", "kb")["reserved_bytes"] == 0
    first.release_reservation(token)
    assert second.reservation_usage("tenant", "kb")["reservations"] == 0


def test_soft_delete_restore_prune_and_purge_are_shared(tmp_path: Path) -> None:
    first, second, objects = _stores(tmp_path)
    versions = [
        _put(first, value, created=float(index))
        for index, value in enumerate((b"one", b"two", b"three"), 1)
    ]

    deleted = second.delete_version(
        "tenant", "kb", "source-one", versions[0]["version_id"]
    )
    with pytest.raises(ArtifactNotFoundError):
        first.read("tenant", "kb", "source-one", versions[0]["version_id"])
    restored = first.restore("tenant", "kb", deleted["recovery_token"])
    assert restored["version_id"] == versions[0]["version_id"]

    pruned = second.prune_versions(
        "tenant",
        "kb",
        "source-one",
        keep_latest=2,
        protect_version_ids=(versions[-1]["version_id"],),
    )
    assert len(pruned) == 1
    assert second.usage("tenant", "kb") == {
        "active_bytes": len(b"two") + len(b"three"),
        "active_versions": 2,
        "trash_bytes": len(b"one"),
        "trash_versions": 1,
    }
    assert second.purge_trash("tenant", "kb", older_than=time.time() + 1) == 1
    assert list(objects.list_prefix("source-artifacts/"))


def test_existing_version_reservation_blocks_delete_until_release(
    tmp_path: Path,
) -> None:
    first, second, _objects = _stores(tmp_path)
    content = b"existing"
    created = _put(first, content)
    token = second.reserve_batch(
        "tenant", "kb", [_item(content)], reservation_key="replay"
    )
    with pytest.raises(ArtifactConflictError, match="reserved"):
        first.delete_version("tenant", "kb", "source-one", created["version_id"])
    second.release_reservation(token)
    first.delete_version("tenant", "kb", "source-one", created["version_id"])


def test_sync_reactivates_a_deleted_historical_version_without_duplicate_bytes(
    tmp_path: Path,
) -> None:
    first, second, objects = _stores(tmp_path)
    old = b"old"
    old_row = _put(first, old, created=1)
    _put(first, b"new", created=2)
    deleted = first.delete_version("tenant", "kb", "source-one", old_row["version_id"])
    token = second.reserve_batch(
        "tenant", "kb", [_item(old, created=3)], reservation_key="revert"
    )
    assert first.purge_trash("tenant", "kb", older_than=time.time() + 1) == 0

    restored = _put(second, old, reservation_token=token, created=3)

    assert restored["version_id"] == old_row["version_id"]
    with pytest.raises(ArtifactNotFoundError):
        first.restore("tenant", "kb", deleted["recovery_token"])
    assert len(list(objects.list_prefix("source-artifacts/"))) == 2


def test_object_metadata_and_content_corruption_fail_closed(tmp_path: Path) -> None:
    first, _second, objects = _stores(tmp_path)
    content = b"authentic"
    created = _put(first, content)
    key = next(iter(objects.list_prefix("source-artifacts/"))).key
    path = objects._path(key)
    path.write_bytes(b"corrupted")

    with pytest.raises(ArtifactIntegrityError):
        first.get_metadata(
            "tenant", "kb", "source-one", created["version_id"], verify_content=True
        )
    with pytest.raises(ArtifactIntegrityError):
        first.open_verified("tenant", "kb", "source-one", created["version_id"])


def test_scope_delete_isolated_and_orphan_collection(tmp_path: Path) -> None:
    first, second, objects = _stores(tmp_path)
    _put(first, b"a", kb="a")
    kept = _put(second, b"b", kb="b")
    orphan = b"orphan"
    objects.put_bytes(
        "source-artifacts/orphan/object",
        orphan,
        sha256=_digest(orphan),
    )
    with first.backend.transaction(write=True) as connection:
        connection.execute(
            "INSERT INTO ha_source_artifact_uploads(object_key,tenant_id,kb_id,source_id,"
            "version_id,metadata_json,reservation_token,reserved_bytes,lease_owner,"
            "lease_expires_at,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                "source-artifacts/orphan/object",
                "tenant",
                "orphan-kb",
                "source",
                "version",
                "{}",
                None,
                len(orphan),
                "crashed-node",
                0,
                0,
            ),
        )

    assert first.delete_scope("tenant", "a") == {
        "active_versions": 1,
        "trash_versions": 0,
        "freed_bytes": 1,
    }
    assert second.read("tenant", "b", "source-one", kept["version_id"]) == b"b"
    assert first.collect_orphans(limit=10) == 1


def test_scope_tombstone_blocks_old_incarnation_and_new_epoch_reactivates(
    tmp_path: Path,
) -> None:
    first, second, _objects = _stores(tmp_path)
    _put(first, b"old")
    first.delete_scope("tenant", "kb", kb_epoch=2)
    with pytest.raises(ArtifactConflictError, match="not writable"):
        _put(second, b"blocked")

    second.activate_scope("tenant", "kb", kb_epoch=3)
    created = _put(second, b"new")
    with pytest.raises(ArtifactConflictError, match="incarnation is stale"):
        first.delete_scope("tenant", "kb", kb_epoch=2)
    assert first.read("tenant", "kb", "source-one", created["version_id"]) == b"new"


def test_scope_delete_preserves_inflight_upload_intent_until_object_is_collected(
    tmp_path: Path,
) -> None:
    first, _second, objects = _stores(tmp_path)
    key = "source-artifacts/inflight/object"
    with first.backend.transaction(write=True) as connection:
        connection.execute(
            "INSERT INTO ha_source_artifact_uploads(object_key,tenant_id,kb_id,source_id,"
            "version_id,metadata_json,reservation_token,reserved_bytes,lease_owner,"
            "lease_expires_at,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                key,
                "tenant",
                "kb",
                "source",
                "version",
                "{}",
                None,
                4,
                "node",
                9,
                time.time(),
            ),
        )

    first.delete_scope("tenant", "kb", kb_epoch=2)
    assert first.collect_orphans(limit=10) == 0
    with first.backend.transaction() as connection:
        pending = connection.execute(
            "SELECT reserved_bytes FROM ha_source_artifact_uploads WHERE object_key=?",
            (key,),
        ).fetchone()
    assert pending is not None and pending[0] == 0

    content = b"late"
    objects.put_bytes(key, content, sha256=_digest(content))
    with first.backend.transaction(write=True) as connection:
        connection.execute(
            "UPDATE ha_source_artifact_uploads SET lease_expires_at=0 WHERE object_key=?",
            (key,),
        )
    assert first.collect_orphans(limit=10) == 1
    assert objects.head(key) is None


def test_database_finalize_failure_keeps_durable_orphan_intent_for_gc(
    tmp_path: Path,
) -> None:
    first, _second, objects = _stores(tmp_path)
    with first.backend.transaction(write=True) as connection:
        connection.execute(
            "CREATE TRIGGER reject_artifact_finalize BEFORE INSERT ON "
            "ha_source_artifacts BEGIN SELECT RAISE(FAIL,'injected finalize failure'); END"
        )

    with pytest.raises(sqlite3.IntegrityError, match="injected finalize failure"):
        _put(first, b"uploaded-before-database-commit")

    uploaded = list(objects.list_prefix("source-artifacts/"))
    assert len(uploaded) == 1
    with first.backend.transaction(write=True) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM ha_source_artifact_uploads"
        ).fetchone()
        assert int(count[0]) == 1
        connection.execute("UPDATE ha_source_artifact_uploads SET lease_expires_at=0")
    assert first.collect_orphans(limit=10) == 1
    assert objects.head(uploaded[0].key) is None


def test_limits_include_trash_and_outstanding_cluster_reservations(
    tmp_path: Path,
) -> None:
    first, second, _objects = _stores(
        tmp_path,
        max_total_bytes=8,
        max_bytes_per_tenant=6,
        max_versions_per_source=2,
        user_max_versions_per_source=1,
    )
    created = _put(first, b"1234")
    deleted = first.delete_version("tenant", "kb", "source-one", created["version_id"])
    with pytest.raises(ArtifactLimitError, match="tenant"):
        second.reserve_batch(
            "tenant", "kb", [_item(b"567")], reservation_key="over-tenant"
        )
    _put(first, b"x")
    with pytest.raises(ArtifactLimitError, match="user_max"):
        first.restore("tenant", "kb", deleted["recovery_token"])


def test_text_diff_and_verified_handle_are_bounded_and_rewound(tmp_path: Path) -> None:
    first, _second, _objects = _stores(tmp_path)
    old = _put(first, b"one\ntwo\n", created=1)
    new = _put(first, b"one\nthree\n", created=2)

    result = first.diff(
        "tenant", "kb", "source-one", old["version_id"], new["version_id"]
    )
    assert result["kind"] == "text"
    assert "-two" in result["diff"]
    assert "+three" in result["diff"]
    metadata, handle = first.open_verified(
        "tenant", "kb", "source-one", new["version_id"]
    )
    try:
        assert metadata["byte_size"] == len(b"one\nthree\n")
        assert handle.read() == b"one\nthree\n"
    finally:
        handle.close()
