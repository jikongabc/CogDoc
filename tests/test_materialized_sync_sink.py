import hashlib
import json

import pytest

from cogdoc.connectors.base import RetryableConnectorError
from cogdoc.connectors.materialized_sink import MaterializedSyncSink
from cogdoc.service.source_catalog import SourceCatalog
from cogdoc.service.source_model import SourceDocument
from cogdoc.tools.chunker import chunk_paper
from cogdoc.tools.source_parser import parse_source


def _document(external_id, name, content):
    return SourceDocument.create(
        connector_type="local-directory",
        external_id=external_id,
        display_name=name,
        content_sha256=hashlib.sha256(content).hexdigest(),
        byte_size=len(content),
    )


def _sink(tmp_path, catalog, submitted, *, job="job-1", submitter=None):
    sink = MaterializedSyncSink(
        source_dir=str(tmp_path / "sources"),
        catalog=catalog,
        index_submitter=submitter or (lambda kb_id: submitted.append(kb_id) or {}),
        owner_id="owner",
        workspace_visible=False,
    )
    sink.begin(
        job_id=job,
        tenant_id="tenant",
        kb_id="storage-kb",
        connection_id="conn-1",
        connector_type="local-directory",
        attempt=1,
    )
    return sink


def test_materialized_sink_atomically_replaces_snapshot_and_catalog(tmp_path):
    catalog = SourceCatalog(str(tmp_path / "state.db"))
    submitted = []
    first_content = (
        b"# Guide\n\nThis is enough meaningful content for a stable connector chunk."
    )
    first = _sink(tmp_path, catalog, submitted, job="job-1")
    first.upsert(_document("a", "guide.md", first_content), first_content)
    first.commit(
        snapshot=True, seen_external_ids=frozenset({"a"}), heartbeat=lambda: None
    )
    first.finalize()

    rows = catalog.list_sources("tenant", "storage-kb")
    assert len(rows) == 1 and rows[0]["metadata"]["provider_external_id"] == "a"
    visible = list((tmp_path / "sources" / ".connections" / "conn-1").glob("*.md"))
    assert len(visible) == 1 and visible[0].read_bytes() == first_content
    root_visible = list((tmp_path / "sources").glob("*.md"))
    contracts = json.loads(
        (tmp_path / "sources" / ".cogdoc-source-contracts.json").read_text()
    )["documents"]
    contract = SourceDocument.from_manifest_document(contracts[root_visible[0].name])
    chunks = chunk_paper(
        parse_source(str(root_visible[0]), source_document=contract),
        contract.version.content_sha256,
    )
    assert chunks[0]["meta"]["source_id"] == contract.source_id
    assert chunks[0]["meta"]["source_version_id"] == contract.version.version_id

    second = _sink(tmp_path, catalog, submitted, job="job-2")
    second.upsert(_document("b", "other.md", b"second"), b"second")
    second.commit(
        snapshot=True, seen_external_ids=frozenset({"b"}), heartbeat=lambda: None
    )
    second.finalize()

    current = catalog.list_sources("tenant", "storage-kb")
    deleted = catalog.list_sources("tenant", "storage-kb", include_deleted=True)
    assert [row["metadata"]["provider_external_id"] for row in current] == ["b"]
    assert len(deleted) == 2
    assert submitted == ["storage-kb", "storage-kb"]
    catalog.close()


def test_materialized_sink_recovers_swapped_commit_journal(tmp_path):
    catalog = SourceCatalog(str(tmp_path / "state.db"))

    def fail_index(_kb_id):
        raise RuntimeError("queue unavailable")

    failed = _sink(tmp_path, catalog, [], job="job-recover", submitter=fail_index)
    failed.upsert(_document("a", "guide.md", b"body"), b"body")
    with pytest.raises(RetryableConnectorError):
        failed.commit(
            snapshot=True,
            seen_external_ids=frozenset({"a"}),
            heartbeat=lambda: None,
        )
    failed.abort()

    submitted = []
    recovered = _sink(tmp_path, catalog, submitted, job="job-recover")
    recovered.recover_commit(heartbeat=lambda: None)
    recovered.finalize()
    assert submitted == ["storage-kb"]
    assert len(catalog.list_sources("tenant", "storage-kb")) == 1
    catalog.close()
