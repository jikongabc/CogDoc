from __future__ import annotations

from pathlib import Path
import hashlib

import pytest

from cogdoc.ha.api_state import (
    DistributedKnowledgeBaseRegistry,
    DistributedMutationCoordinator,
    StaleMutationFence,
)
from cogdoc.ha.connector_commit import DistributedConnectorCommitStore
from cogdoc.ha.object_store import LocalObjectStore, ObjectConflict, ObjectIntegrityError
from cogdoc.ha.storage import SQLiteBackend
from cogdoc.ha.source_catalog import DistributedSourceCatalog
from cogdoc.connectors.materialized_sink import MaterializedSyncSink
from cogdoc.service.source_model import SourceDocument


def _plane(tmp_path: Path):
    backend = SQLiteBackend(tmp_path / "shared.db")
    registry = DistributedKnowledgeBaseRegistry(backend, tmp_path / "cache")
    kb = registry.create("docs", "tenant", "owner")
    coordinator = DistributedMutationCoordinator(
        backend, registry, owner_id="node-a", lease_seconds=30
    )
    objects = LocalObjectStore(tmp_path / "objects")
    store = DistributedConnectorCommitStore(backend, objects, coordinator)
    return backend, kb, coordinator, objects, store


def test_connector_commit_handoff_restores_on_another_local_cache(
    tmp_path: Path,
) -> None:
    backend, kb, coordinator, _objects, store = _plane(tmp_path)
    storage_id = str(kb["storage_id"])
    source = tmp_path / "node-a-staging"
    source.mkdir()
    (source / ".cogdoc-connector-sources.json").write_text(
        '{"schema_version":1,"sources":{}}', encoding="utf-8"
    )
    (source / "report.md").write_text("shared snapshot", encoding="utf-8")

    with coordinator.lease(storage_id):
        store.prepare(
            job_id="sync-one",
            tenant_id="tenant",
            kb_id=storage_id,
            connection_id="conn-one",
            connector_type="url",
            staging=source,
        )
        store.set_phase("sync-one", "materialized", "index-one")
        target = tmp_path / "node-b-staging"
        restored = store.restore(
            job_id="sync-one",
            tenant_id="tenant",
            kb_id=storage_id,
            connection_id="conn-one",
            connector_type="url",
            staging=target,
        )
        assert restored["phase"] == "materialized"
        assert restored["index_job_id"] == "index-one"
        assert (target / "report.md").read_text(encoding="utf-8") == "shared snapshot"
        store.finalize("sync-one")

    with backend.transaction() as connection:
        assert connection.execute("SELECT 1 FROM ha_connector_commits").fetchone() is None
    backend.close()


def test_connector_commit_is_immutable_and_fenced(tmp_path: Path) -> None:
    backend, kb, coordinator, objects, store = _plane(tmp_path)
    storage_id = str(kb["storage_id"])
    source = tmp_path / "staging"
    source.mkdir()
    (source / "report.md").write_text("one", encoding="utf-8")
    lease = coordinator.acquire(storage_id)
    with coordinator.bind_lease(lease):
        store.prepare(
            job_id="sync-one",
            tenant_id="tenant",
            kb_id=storage_id,
            connection_id="conn-one",
            connector_type="url",
            staging=source,
        )
        (source / "report.md").write_text("two", encoding="utf-8")
        with pytest.raises((ObjectConflict, ObjectIntegrityError)):
            store.prepare(
                job_id="sync-one",
                tenant_id="tenant",
                kb_id=storage_id,
                connection_id="conn-one",
                connector_type="url",
                staging=source,
            )

    coordinator.release(lease)
    with pytest.raises(StaleMutationFence):
        store.restore(
            job_id="sync-one",
            tenant_id="tenant",
            kb_id=storage_id,
            connection_id="conn-one",
            connector_type="url",
            staging=tmp_path / "restore",
        )
    assert tuple(objects.list_prefix("connector-commits/"))
    backend.close()


def test_materialized_commit_recovers_on_another_node(tmp_path: Path) -> None:
    backend, kb, first_coordinator, objects, first_store = _plane(tmp_path)
    storage_id = str(kb["storage_id"])
    catalog = DistributedSourceCatalog(backend)
    content = b"cross-node recovery"
    document = SourceDocument.create(
        connector_type="url",
        external_id="remote-1",
        display_name="report.md",
        content_sha256=hashlib.sha256(content).hexdigest(),
        byte_size=len(content),
    )
    first_source = tmp_path / "node-a" / "sources"
    with first_coordinator.lease(storage_id):
        first_sink = MaterializedSyncSink(
            source_dir=str(first_source),
            catalog=catalog,
            index_submitter=lambda _kb: {"job_id": "unused"},
            index_status_reader=lambda _job: {"status": "succeeded"},
            owner_id="owner",
            workspace_visible=False,
            commit_store=first_store,
        )
        first_sink.begin(
            job_id="sync-cross-node",
            tenant_id="tenant",
            kb_id=storage_id,
            connection_id="conn-one",
            connector_type="url",
            attempt=1,
        )
        first_sink.upsert(document, content)
        first_sink.prepare_commit(
            snapshot=True, seen_external_ids=frozenset({"remote-1"})
        )
        first_sink.mark_committing()

    second_registry = DistributedKnowledgeBaseRegistry(backend, tmp_path / "cache-b")
    second_coordinator = DistributedMutationCoordinator(
        backend, second_registry, owner_id="node-b", lease_seconds=30
    )
    second_store = DistributedConnectorCommitStore(
        backend, objects, second_coordinator
    )
    second_source = tmp_path / "node-b" / "sources"
    with second_coordinator.lease(storage_id):
        recovered = MaterializedSyncSink(
            source_dir=str(second_source),
            catalog=catalog,
            index_submitter=lambda _kb: {"job_id": "index-node-b"},
            index_status_reader=lambda _job: {"status": "succeeded"},
            owner_id="owner",
            workspace_visible=False,
            commit_store=second_store,
        )
        recovered.begin(
            job_id="sync-cross-node",
            tenant_id="tenant",
            kb_id=storage_id,
            connection_id="conn-one",
            connector_type="url",
            attempt=2,
            recovering_commit=True,
        )
        recovered.recover_commit(heartbeat=lambda: None)
        assert tuple(second_source.glob(".cogdoc-connector-*.md"))
        rows = catalog.list_sources("tenant", storage_id)
        assert len(rows) == 1
        assert rows[0]["connection_id"] == "conn-one"
        recovered.finalize()

    with backend.transaction() as connection:
        assert connection.execute("SELECT 1 FROM ha_connector_commits").fetchone() is None
    backend.close()
