from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import cogdoc.ha.index_mirror as mirror_module
from cogdoc.config.settings import get_settings
from cogdoc.ha.index_mirror import HAIndexMirror
from cogdoc.ha.api_state import (
    DistributedKnowledgeBaseRegistry,
    DistributedMutationCoordinator,
)
from cogdoc.ha.portable_index import PORTABLE_INDEX_FILENAME, PortableIndexStore
from cogdoc.ha.runtime import HAConfig, HARuntime
from cogdoc.service.kb_state import KBState


class Registry:
    def __init__(self, row):
        self.row = row

    def get_by_storage_id(self, storage_id):
        return dict(self.row) if storage_id == self.row["storage_id"] else None

    def list(self):
        return [dict(self.row)]


class RegistryRows:
    def __init__(self, rows):
        self.rows = rows

    def get_by_storage_id(self, storage_id):
        return next(
            (dict(row) for row in self.rows if row["storage_id"] == storage_id),
            None,
        )

    def list(self):
        return [dict(row) for row in self.rows]


def _config(tmp_path):
    return HAConfig(
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
        worker_id="mirror-worker",
        scheduler_enabled=False,
        outbox_enabled=False,
    )


def _active_generation(kb_id):
    state = KBState(kb_id)
    generation_id = state.begin_generation("model", "build", "chunks-v1")
    state.mark_ready(generation_id, expected_count=0, documents=[])
    state.switch_active(generation_id)
    return generation_id


def _fake_export(
    _kb_id,
    _generation_id,
    destination,
    *,
    embedding_model,
    dimensions,
    chunk_version,
):
    return PortableIndexStore().write(
        destination / PORTABLE_INDEX_FILENAME,
        [],
        [],
        embedding_model=embedding_model,
        dimensions=dimensions,
        chunk_version=chunk_version,
    )


@pytest.fixture
def isolated_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("COGDOC_DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_post_commit_mirror_publishes_prepared_portable_generation(
    tmp_path, monkeypatch, isolated_settings
):
    monkeypatch.setattr(mirror_module, "export_retrieval_generation", _fake_export)
    runtime = HARuntime(_config(tmp_path))
    local_generation = _active_generation("kb")
    mirror = HAIndexMirror(
        runtime,
        Registry({"tenant_id": "tenant", "storage_id": "kb"}),
    )

    job = mirror.mirror_result("kb", SimpleNamespace(generation_id=local_generation))
    assert job["status"] == "published"
    current = runtime.index_generations.current("tenant", "kb")
    assert current is not None
    assert current["build_id"] == f"local:{local_generation}"
    runtime.shutdown()


def test_multiwriter_mirror_publishes_source_and_index_heads_atomically(
    tmp_path, monkeypatch, isolated_settings
):
    monkeypatch.setattr(mirror_module, "export_retrieval_generation", _fake_export)
    runtime = HARuntime(_config(tmp_path))
    registry = DistributedKnowledgeBaseRegistry(
        runtime.backend, tmp_path / "source-cache"
    )
    record = registry.create("docs", "tenant", "owner")
    storage_id = str(record["storage_id"])
    source = Path(registry.source_dir(storage_id))
    source.mkdir(parents=True, exist_ok=True)
    (source / "document.md").write_text("shared source", encoding="utf-8")
    coordinator = DistributedMutationCoordinator(
        runtime.backend, registry, owner_id="api-a", lease_seconds=30
    )
    runtime.api_registry = registry
    runtime.api_mutation_coordinator = coordinator
    local_generation = _active_generation(storage_id)
    mirror = HAIndexMirror(runtime, registry)

    with coordinator.lease(storage_id):
        published = mirror.mirror_result(
            storage_id, SimpleNamespace(generation_id=local_generation)
        )
    assert published["status"] == "published"
    assert runtime.index_generations.current("tenant", storage_id) is not None
    source_head = runtime.source_generations.current(storage_id)
    assert source_head is not None
    replica = tmp_path / "source-replica"
    runtime.source_generations.materialize_current(storage_id, replica)
    assert (replica / "document.md").read_text(encoding="utf-8") == "shared source"
    runtime.shutdown()


def test_connector_mirror_advances_existing_source_head_with_index(
    tmp_path, monkeypatch, isolated_settings
):
    monkeypatch.setattr(mirror_module, "export_retrieval_generation", _fake_export)
    runtime = HARuntime(_config(tmp_path))
    registry = DistributedKnowledgeBaseRegistry(
        runtime.backend, tmp_path / "source-cache"
    )
    record = registry.create("docs", "tenant", "owner")
    storage_id = str(record["storage_id"])
    source = Path(registry.source_dir(storage_id))
    source.mkdir(parents=True, exist_ok=True)
    coordinator = DistributedMutationCoordinator(
        runtime.backend, registry, owner_id="api-a", lease_seconds=30
    )
    runtime.api_registry = registry
    runtime.api_mutation_coordinator = coordinator
    mirror = HAIndexMirror(runtime, registry)

    (source / "document.md").write_text("old source", encoding="utf-8")
    with coordinator.lease(storage_id) as lease:
        old = runtime.source_generations.stage_for_commit(
            storage_id=storage_id,
            source_dir=source,
            lease=lease,
            build_id="old-index",
        )
        runtime.source_generations.publish(old["generation_id"], lease)

    (source / "document.md").write_text("connector source", encoding="utf-8")
    local_generation = _active_generation(storage_id)
    with coordinator.lease(storage_id) as lease:
        runtime.source_generations.stage_for_commit(
            storage_id=storage_id,
            source_dir=source,
            lease=lease,
            build_id=local_generation,
        )
        published = mirror.mirror_result(
            storage_id, SimpleNamespace(generation_id=local_generation)
        )

    assert published["status"] == "published"
    replica = tmp_path / "connector-replica"
    runtime.source_generations.materialize_current(storage_id, replica)
    assert (replica / "document.md").read_text(encoding="utf-8") == "connector source"
    assert runtime.index_generations.current("tenant", storage_id) is not None
    runtime.shutdown()


def test_reconcile_repairs_crash_window_after_local_commit(
    tmp_path, monkeypatch, isolated_settings
):
    monkeypatch.setattr(mirror_module, "export_retrieval_generation", _fake_export)
    runtime = HARuntime(_config(tmp_path))
    local_generation = _active_generation("kb")
    mirror = HAIndexMirror(
        runtime,
        Registry({"tenant_id": "tenant", "storage_id": "kb"}),
    )

    assert mirror.reconcile_once() == (1, 1)
    assert mirror.reconcile_once() == (1, 0)
    assert runtime.index_generations.current("tenant", "kb")["build_id"] == (
        f"local:{local_generation}"
    )
    runtime.shutdown()


def test_reconcile_failure_for_one_kb_does_not_starve_later_kbs(
    tmp_path, monkeypatch, isolated_settings
):
    runtime = HARuntime(_config(tmp_path))
    first_generation = _active_generation("broken-kb")
    second_generation = _active_generation("healthy-kb")
    mirror = HAIndexMirror(
        runtime,
        RegistryRows(
            [
                {"tenant_id": "tenant", "storage_id": "broken-kb"},
                {"tenant_id": "tenant", "storage_id": "healthy-kb"},
            ]
        ),
    )
    calls = []

    def selective(tenant_id, kb_id, generation_id):
        calls.append((tenant_id, kb_id, generation_id))
        if kb_id == "broken-kb":
            raise RuntimeError("corrupt local generation")
        return {"status": "published"}

    monkeypatch.setattr(mirror, "mirror", selective)
    with pytest.raises(RuntimeError, match="1 knowledge base"):
        mirror.reconcile_once()
    assert calls == [
        ("tenant", "broken-kb", first_generation),
        ("tenant", "healthy-kb", second_generation),
    ]
    runtime.shutdown()


def test_prepared_mirror_retry_reexports_source_and_finishes_upload(
    tmp_path, monkeypatch, isolated_settings
):
    monkeypatch.setattr(mirror_module, "export_retrieval_generation", _fake_export)
    runtime = HARuntime(_config(tmp_path))
    local_generation = _active_generation("kb")
    mirror = HAIndexMirror(
        runtime,
        Registry({"tenant_id": "tenant", "storage_id": "kb"}),
    )
    original = runtime.index_repository.materialize
    calls = []

    def fail_once(generation, directory):
        calls.append(1)
        if len(calls) == 1:
            raise OSError("upload interrupted")
        return original(generation, directory)

    monkeypatch.setattr(runtime.index_repository, "materialize", fail_once)
    with pytest.raises(OSError, match="interrupted"):
        mirror.mirror("tenant", "kb", local_generation)
    row = runtime.index_generations.get(
        runtime.index_generations.begin_build(
            "tenant",
            "kb",
            f"local:{local_generation}",
            runtime.config.worker_id,
        )["generation_id"]
    )
    assert row["status"] == "prepared"

    mirror.mirror("tenant", "kb", local_generation)
    assert runtime.index_generations.current("tenant", "kb")["status"] == "published"
    runtime.shutdown()


def test_real_local_build_exports_publishes_and_installs_without_reembedding(
    tmp_path, monkeypatch, isolated_settings
):
    from cogdoc.service.ingest_service import build_kb_index_transactional
    from cogdoc.tools.embedder import Embedder

    monkeypatch.setattr(Embedder, "EMBEDDING_DIM", 3)
    monkeypatch.setattr(Embedder, "EMBEDDING_CONTRACT_VERSION", "test-contract")
    embedded = []

    def embed_documents(texts):
        embedded.extend(texts)
        return [[1.0, 0.0, 0.5] for _text in texts]

    monkeypatch.setattr(Embedder, "embed_documents", embed_documents)
    source = tmp_path / "data" / "kb" / "kb" / "sources"
    source.mkdir(parents=True)
    (source / "document.txt").write_text(
        "Portable retrieval generations preserve verified evidence. " * 80
    )
    result = build_kb_index_transactional("kb", str(source))
    assert result.chunk_count > 0
    assert embedded
    runtime = HARuntime(_config(tmp_path))
    mirror = HAIndexMirror(
        runtime,
        Registry({"tenant_id": "tenant", "storage_id": "kb"}),
    )

    published = mirror.mirror_result("kb", result)
    assert published["status"] == "published"
    engine = runtime.index_replica.get_engine("tenant", "kb")
    assert engine.count() == result.chunk_count
    assert engine.is_consistent() is True
    assert engine.list_sources() == ["document.txt"]
    # Installation consumed exported embeddings; it never invoked the model.
    assert len(embedded) == result.chunk_count
    runtime.shutdown()
