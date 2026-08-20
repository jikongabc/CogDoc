import hashlib
import json
import sqlite3

import pytest

from cogdoc.service.source_catalog import SourceCatalog
from cogdoc.service.source_model import SourceDocument


def _doc(external_id: str, name: str, content: bytes) -> SourceDocument:
    return SourceDocument.create(
        connector_type="git",
        external_id=external_id,
        display_name=name,
        content_sha256=hashlib.sha256(content).hexdigest(),
        byte_size=len(content),
        origin_uri=f"https://example.test/repo/{external_id}",
    )


def test_catalog_keeps_current_source_and_immutable_versions(tmp_path):
    store = SourceCatalog(str(tmp_path / "state.db"))
    first = _doc("docs/a.md", "a.md", b"one")
    second = _doc("docs/a.md", "renamed.md", b"two")
    store.upsert("tenant", "kb", first)
    store.upsert("tenant", "kb", second)

    current = store.get("tenant", "kb", first.source_id)
    assert current["display_name"] == "renamed.md"
    assert current["content_sha256"] == second.version.content_sha256
    assert len(store.versions("tenant", "kb", first.source_id)) == 2
    store.close()


def test_catalog_migrates_legacy_schema_idempotently(tmp_path):
    db_path = tmp_path / "legacy.db"
    connection = sqlite3.connect(db_path)
    connection.executescript(
        """
        CREATE TABLE source_catalog_documents (
            tenant_id TEXT NOT NULL,
            kb_id TEXT NOT NULL,
            source_id TEXT NOT NULL,
            connector_type TEXT NOT NULL,
            external_id TEXT NOT NULL,
            display_name TEXT NOT NULL,
            media_type TEXT NOT NULL,
            kind TEXT NOT NULL,
            origin_uri TEXT,
            current_version_id TEXT NOT NULL,
            metadata_json TEXT NOT NULL,
            deleted_at REAL,
            updated_at REAL NOT NULL,
            PRIMARY KEY (tenant_id, kb_id, source_id),
            UNIQUE (tenant_id, kb_id, connector_type, external_id)
        );
        CREATE TABLE source_catalog_versions (
            tenant_id TEXT NOT NULL,
            kb_id TEXT NOT NULL,
            source_id TEXT NOT NULL,
            version_id TEXT NOT NULL,
            content_sha256 TEXT NOT NULL,
            byte_size INTEGER,
            etag TEXT,
            modified_at TEXT,
            fetched_at REAL NOT NULL,
            created_at REAL NOT NULL,
            PRIMARY KEY (tenant_id, kb_id, source_id, version_id)
        );
        """
    )
    document = SourceDocument.create(
        connector_type="git",
        external_id="conn-old:legacy.md",
        display_name="legacy.md",
        content_sha256=hashlib.sha256(b"legacy").hexdigest(),
        metadata={"connection_id": "conn-old"},
    )
    connection.execute(
        "INSERT INTO source_catalog_versions VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            "tenant",
            "kb",
            document.source_id,
            document.version.version_id,
            document.version.content_sha256,
            document.version.byte_size,
            None,
            None,
            document.version.fetched_at,
            1.0,
        ),
    )
    connection.execute(
        "INSERT INTO source_catalog_documents VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "tenant",
            "kb",
            document.source_id,
            document.connector_type,
            document.external_id,
            document.display_name,
            document.media_type,
            document.kind.value,
            None,
            document.version.version_id,
            json.dumps(document.metadata),
            None,
            1.0,
        ),
    )
    connection.commit()
    connection.close()

    catalog = SourceCatalog(str(db_path))
    assert (
        catalog.get("tenant", "kb", document.source_id)["connection_id"] == "conn-old"
    )
    catalog.close()
    SourceCatalog(str(db_path)).close()

    connection = sqlite3.connect(db_path)
    columns = {
        row[1]
        for row in connection.execute(
            "PRAGMA table_info(source_catalog_documents)"
        ).fetchall()
    }
    connection.close()
    assert {
        "connection_id",
        "health_status",
        "last_sync_at",
        "last_sync_error",
    }.issubset(columns)


def test_catalog_scopes_source_and_version_reads_to_tenant_and_kb(tmp_path):
    store = SourceCatalog(str(tmp_path / "state.db"))
    document = _doc("docs/a.md", "a.md", b"one")
    store.upsert("tenant-a", "kb-a", document)

    assert store.get("tenant-b", "kb-a", document.source_id) is None
    assert store.get("tenant-a", "kb-b", document.source_id) is None
    assert (
        store.get_version(
            "tenant-b", "kb-a", document.source_id, document.version.version_id
        )
        is None
    )
    assert store.list_versions("tenant-a", "kb-b", document.source_id) == []
    version = store.version(
        "tenant-a", "kb-a", document.source_id, document.version.version_id
    )
    assert version is not None
    assert version["is_current"] is True

    with pytest.raises(ValueError, match="tenant_id"):
        store.list_sources("", "kb-a")
    store.close()


def test_catalog_tracks_connection_and_source_health(tmp_path):
    store = SourceCatalog(str(tmp_path / "state.db"))
    connected = SourceDocument.create(
        connector_type="git",
        external_id="conn-1:docs/a.md",
        display_name="a.md",
        content_sha256=hashlib.sha256(b"one").hexdigest(),
        metadata={"connection_id": "conn-1"},
    )
    legacy = _doc("manual.md", "manual.md", b"manual")
    current = store.upsert("tenant", "kb", connected)
    store.upsert("tenant", "kb", legacy)

    assert current["connection_id"] == "conn-1"
    assert current["health_status"] == "healthy"
    assert current["last_sync_at"] is not None
    assert [
        row["source_id"]
        for row in store.list_sources("tenant", "kb", connection_id="conn-1")
    ] == [connected.source_id]
    assert store.get("tenant", "kb", legacy.source_id)["connection_id"] is None

    assert (
        store.record_connection_health(
            "tenant",
            "kb",
            "conn-1",
            "error",
            last_sync_error="provider unavailable",
            job_sequence=2,
        )
        == 1
    )
    failed = store.get("tenant", "kb", connected.source_id)
    assert failed["health_status"] == "error"
    assert failed["last_sync_error"] == "provider unavailable"
    assert failed["health_job_sequence"] == 2
    assert (
        store.record_connection_health(
            "tenant",
            "kb",
            "conn-1",
            "syncing",
            job_sequence=1,
        )
        == 0
    )
    assert store.get("tenant", "kb", connected.source_id)["health_status"] == "error"
    recovered = store.set_source_health("tenant", "kb", connected.source_id, "healthy")
    assert recovered["health_status"] == "healthy"
    assert recovered["last_sync_error"] is None
    assert store.record_connection_health("other-tenant", "kb", "conn-1", "error") == 0
    store.close()


def test_catalog_reconcile_connection_does_not_tombstone_peer_connection(tmp_path):
    store = SourceCatalog(str(tmp_path / "state.db"))
    first = _doc("first", "first.md", b"first")
    second = _doc("second", "second.md", b"second")
    store.upsert("tenant", "kb", first, connection_id="conn-1")
    store.upsert("tenant", "kb", second, connection_id="conn-2")

    result = store.reconcile(
        "tenant", "kb", [], connector_type="git", connection_id="conn-1"
    )

    assert result == {"upserted": 0, "deleted": 1}
    assert store.get("tenant", "kb", first.source_id) is None
    assert store.get("tenant", "kb", second.source_id) is not None
    assert (
        store.get("tenant", "kb", first.source_id, include_deleted=True)[
            "health_status"
        ]
        == "stale"
    )
    store.close()


def test_catalog_reconcile_tombstones_only_one_connector(tmp_path):
    store = SourceCatalog(str(tmp_path / "state.db"))
    a = _doc("a", "a.md", b"a")
    b = _doc("b", "b.md", b"b")
    store.reconcile("tenant", "kb", [a, b], connector_type="git")
    result = store.reconcile("tenant", "kb", [b], connector_type="git")

    assert result == {"upserted": 1, "deleted": 1}
    assert [row["display_name"] for row in store.list_sources("tenant", "kb")] == [
        "b.md"
    ]
    assert len(store.list_sources("tenant", "kb", include_deleted=True)) == 2
    store.close()


def test_catalog_reconcile_rejects_mixed_connector_snapshot(tmp_path):
    store = SourceCatalog(str(tmp_path / "state.db"))
    with pytest.raises(ValueError, match="connector_type"):
        store.reconcile("tenant", "kb", [_doc("a", "a.md", b"a")], connector_type="s3")
    assert store.list_sources("tenant", "kb", include_deleted=True) == []
    store.close()
