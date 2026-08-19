import hashlib

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
