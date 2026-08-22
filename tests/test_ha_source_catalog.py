from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from cogdoc.ha.source_catalog import DistributedSourceCatalog
from cogdoc.ha.storage import SQLiteBackend
from cogdoc.source_model import SourceDocument


def _doc(connection: str, external: str, content: bytes) -> SourceDocument:
    return SourceDocument.create(
        connector_type="git",
        external_id=f"{connection}:{external}",
        display_name=external,
        content_sha256=hashlib.sha256(content).hexdigest(),
        byte_size=len(content),
        metadata={"connection_id": connection},
    )


def _stores(
    tmp_path: Path,
) -> tuple[DistributedSourceCatalog, DistributedSourceCatalog]:
    path = tmp_path / "shared.db"
    return (
        DistributedSourceCatalog(SQLiteBackend(path)),
        DistributedSourceCatalog(SQLiteBackend(path)),
    )


def test_two_nodes_share_current_and_immutable_versions(tmp_path: Path) -> None:
    first, second = _stores(tmp_path)
    old = _doc("conn-a", "a.md", b"old")
    new = _doc("conn-a", "a.md", b"new")

    first.upsert("tenant-a", "kb-a", old)
    second.upsert("tenant-a", "kb-a", new)

    current = first.get("tenant-a", "kb-a", old.source_id)
    assert current is not None
    assert current["content_sha256"] == new.version.content_sha256
    assert len(second.list_versions("tenant-a", "kb-a", old.source_id)) == 2
    assert second.get("tenant-b", "kb-a", old.source_id) is None


def test_reconcile_is_connection_scoped_and_tombstone_is_shared(tmp_path: Path) -> None:
    first, second = _stores(tmp_path)
    a = _doc("conn-a", "a.md", b"a")
    b = _doc("conn-b", "b.md", b"b")
    first.reconcile("tenant", "kb", [a], connection_id="conn-a")
    first.reconcile("tenant", "kb", [b], connection_id="conn-b")

    result = second.reconcile("tenant", "kb", [], connection_id="conn-a")

    assert result == {"upserted": 0, "deleted": 1}
    assert first.get("tenant", "kb", a.source_id) is None
    assert first.get("tenant", "kb", b.source_id) is not None
    deleted = first.get("tenant", "kb", a.source_id, include_deleted=True)
    assert deleted is not None
    assert deleted["health_status"] == "stale"


def test_health_watermark_rejects_old_node_event(tmp_path: Path) -> None:
    first, second = _stores(tmp_path)
    document = _doc("conn-a", "a.md", b"a")
    first.upsert("tenant", "kb", document)

    assert (
        second.record_connection_health(
            "tenant",
            "kb",
            "conn-a",
            "healthy",
            job_sequence=2,
            job_attempt=1,
            event_rank=3,
        )
        == 1
    )
    assert (
        first.record_connection_health(
            "tenant",
            "kb",
            "conn-a",
            "error",
            last_sync_error="old",
            job_sequence=1,
            job_attempt=9,
            event_rank=9,
        )
        == 0
    )
    current = first.get("tenant", "kb", document.source_id)
    assert current is not None
    assert current["health_status"] == "healthy"
    assert current["last_sync_error"] is None


def test_same_health_job_orders_attempt_and_event_rank(tmp_path: Path) -> None:
    first, second = _stores(tmp_path)
    document = _doc("conn-a", "a.md", b"a")
    first.upsert("tenant", "kb", document)
    assert (
        first.record_connection_health(
            "tenant",
            "kb",
            "conn-a",
            "degraded",
            job_sequence=4,
            job_attempt=2,
            event_rank=1,
        )
        == 1
    )
    assert (
        second.record_connection_health(
            "tenant",
            "kb",
            "conn-a",
            "healthy",
            job_sequence=4,
            job_attempt=2,
            event_rank=2,
        )
        == 1
    )
    assert (
        first.record_connection_health(
            "tenant",
            "kb",
            "conn-a",
            "error",
            job_sequence=4,
            job_attempt=1,
            event_rank=99,
        )
        == 0
    )


def test_delete_scope_is_idempotent_and_preserves_other_scope(tmp_path: Path) -> None:
    first, second = _stores(tmp_path)
    a = _doc("conn-a", "a.md", b"a")
    b = _doc("conn-b", "b.md", b"b")
    first.upsert("tenant", "kb-a", a)
    first.upsert("tenant", "kb-b", b)

    assert second.delete_scope("tenant", "kb-a") == {"documents": 1, "versions": 1}
    assert first.delete_scope("tenant", "kb-a") == {"documents": 0, "versions": 0}
    assert first.get("tenant", "kb-b", b.source_id) is not None


def test_reconcile_rejects_mixed_connection_metadata_atomically(tmp_path: Path) -> None:
    first, _second = _stores(tmp_path)
    document = _doc("conn-a", "a.md", b"a")

    with pytest.raises(ValueError, match="connection_id conflicts"):
        first.reconcile("tenant", "kb", [document], connection_id="conn-b")

    assert first.list_sources("tenant", "kb", include_deleted=True) == []
