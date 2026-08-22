from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from cogdoc.ha.index_generation import IndexGenerationStore, StaleIndexFence
from cogdoc.ha.index_replica import HAIndexReplica, RegistryIndexProvider
from cogdoc.ha.object_store import LocalObjectStore, ObjectIndexRepository
from cogdoc.ha.portable_index import (
    PORTABLE_INDEX_FILENAME,
    PortableIndexIntegrityError,
    PortableIndexStore,
)
from cogdoc.ha.runtime import manifest_for_directory
from cogdoc.ha.storage import SQLiteBackend


class Installer:
    def __init__(self):
        self.calls = []

    def install(self, kb_id, generation_id, path):
        self.calls.append((kb_id, generation_id, path))
        return SimpleNamespace(generation_id=generation_id)


def _publish(backend, repository, directory, build_id, *, model="model"):
    generations = IndexGenerationStore(backend)
    portable = directory / PORTABLE_INDEX_FILENAME
    PortableIndexStore().write(
        portable,
        [
            {
                "text": build_id,
                "meta": {
                    "chunk_id": f"chunk-{build_id}",
                    "document_id": "doc-a",
                    "source_sha256": "a" * 64,
                    "local_chunk_index": 0,
                    "chunk_index": 0,
                    "source": "a.md",
                    "page": 1,
                    "page_start": 1,
                    "page_end": 1,
                    "origin": "file",
                },
            }
        ],
        [[1.0, 0.0, 0.5]],
        embedding_model=model,
        dimensions=3,
        chunk_version="v1",
    )
    generation = generations.begin_build("tenant", "kb", build_id, "worker")
    generation = generations.prepare(
        generation["generation_id"],
        generation["lease_token"],
        manifest_for_directory(
            directory,
            contract={
                "chunk_version": "v1",
                "embedding_model": "model",
                "dimensions": 3,
            },
        ),
    )
    repository.materialize(generation, directory)
    return generations.publish(
        generation["generation_id"], generation["lease_token"], repository.verify
    )


def test_replica_downloads_installs_and_caches_only_current_generation(tmp_path):
    backend = SQLiteBackend(tmp_path / "authority.db")
    repository = ObjectIndexRepository(LocalObjectStore(tmp_path / "objects"))
    source = tmp_path / "build"
    source.mkdir()
    published = _publish(backend, repository, source, "one")
    installer = Installer()
    replica = HAIndexReplica(
        IndexGenerationStore(backend),
        repository,
        tmp_path / "cache",
        installer=installer,
    )

    first = replica.get_engine("tenant", "kb")
    second = replica.get_engine("tenant", "kb")
    assert first is second
    assert first.generation_id == published["generation_id"]
    assert len(installer.calls) == 1
    cached_path = installer.calls[0][2]
    assert cached_path.is_file()
    assert repository.verify_local(published, cached_path.parent) is None
    backend.close()


def test_replica_rejects_inner_outer_contract_mismatch(tmp_path):
    backend = SQLiteBackend(tmp_path / "authority.db")
    repository = ObjectIndexRepository(LocalObjectStore(tmp_path / "objects"))
    source = tmp_path / "build"
    source.mkdir()
    _publish(backend, repository, source, "one", model="different")
    replica = HAIndexReplica(
        IndexGenerationStore(backend),
        repository,
        tmp_path / "cache",
        installer=Installer(),
    )

    with pytest.raises(PortableIndexIntegrityError, match="contract"):
        replica.get_engine("tenant", "kb")
    backend.close()


def test_replica_head_race_never_returns_superseded_engine(tmp_path, monkeypatch):
    backend = SQLiteBackend(tmp_path / "authority.db")
    repository = ObjectIndexRepository(LocalObjectStore(tmp_path / "objects"))
    first_dir = tmp_path / "first"
    first_dir.mkdir()
    first = _publish(backend, repository, first_dir, "one")
    second_dir = tmp_path / "second"
    second_dir.mkdir()
    installer = Installer()
    generations = IndexGenerationStore(backend)
    replica = HAIndexReplica(
        generations, repository, tmp_path / "cache", installer=installer
    )
    original_install = installer.install
    switched = []

    def install_and_switch(*args):
        result = original_install(*args)
        if not switched:
            switched.append(_publish(backend, repository, second_dir, "two"))
        return result

    monkeypatch.setattr(installer, "install", install_and_switch)
    result = replica.get_engine("tenant", "kb")
    assert result.generation_id == switched[0]["generation_id"]
    assert result.generation_id != first["generation_id"]
    assert len(installer.calls) == 2
    backend.close()


def test_registry_provider_and_factory_never_fall_back_to_local_stale_index():
    from cogdoc.service.kb_lifecycle import LIFECYCLE_ACTIVE, shared_lifecycle_store
    from cogdoc.service.retriever_factory import RetrieverFactory

    engine = object()

    class Replica:
        def get_engine(self, tenant_id, kb_id):
            assert (tenant_id, kb_id) == ("tenant", "kb")
            return engine

    provider = RegistryIndexProvider(
        Replica(),
        SimpleNamespace(
            get_by_storage_id=lambda value: {
                "tenant_id": "tenant",
                "storage_id": value,
            }
        ),
    )
    shared_lifecycle_store().set("kb", LIFECYCLE_ACTIVE)
    RetrieverFactory.bind_external_provider(provider)
    try:
        assert RetrieverFactory.get_engine("kb") is engine
    finally:
        RetrieverFactory.unbind_external_provider(provider)


def test_registry_provider_pins_one_generation_across_head_switch(tmp_path):
    backend = SQLiteBackend(tmp_path / "authority.db")
    repository = ObjectIndexRepository(LocalObjectStore(tmp_path / "objects"))
    first_dir = tmp_path / "first"
    first_dir.mkdir()
    first = _publish(backend, repository, first_dir, "one")
    installer = Installer()
    generations = IndexGenerationStore(backend)
    replica = HAIndexReplica(
        generations, repository, tmp_path / "cache", installer=installer
    )
    provider = RegistryIndexProvider(
        replica,
        SimpleNamespace(
            get_by_storage_id=lambda value: {
                "tenant_id": "tenant",
                "storage_id": value,
            }
        ),
        worker_id="node-a",
        reader_lease_seconds=30,
    )

    with provider.pin("kb") as snapshot:
        assert snapshot["generation_id"] == first["generation_id"]
        assert provider("kb").generation_id == first["generation_id"]
        second_dir = tmp_path / "second"
        second_dir.mkdir()
        second = _publish(backend, repository, second_dir, "two")
        assert provider("kb").generation_id == first["generation_id"]
        assert generations.garbage_candidates(before=time.time() + 3600) == []

    assert provider("kb").generation_id == second["generation_id"]
    assert [
        row["generation_id"]
        for row in generations.garbage_candidates(before=time.time() + 3600)
    ] == [first["generation_id"]]
    backend.close()


def test_registry_provider_rejects_a_spawned_thread_without_pin_context(tmp_path):
    backend = SQLiteBackend(tmp_path / "authority.db")
    repository = ObjectIndexRepository(LocalObjectStore(tmp_path / "objects"))
    source = tmp_path / "source"
    source.mkdir()
    _publish(backend, repository, source, "one")
    provider = RegistryIndexProvider(
        HAIndexReplica(
            IndexGenerationStore(backend),
            repository,
            tmp_path / "cache",
            installer=Installer(),
        ),
        SimpleNamespace(
            get_by_storage_id=lambda value: {
                "tenant_id": "tenant",
                "storage_id": value,
            }
        ),
        worker_id="node-a",
        reader_lease_seconds=30,
    )

    with provider.pin("kb"):
        with ThreadPoolExecutor(max_workers=1) as pool:
            with pytest.raises(StaleIndexFence, match="lost"):
                pool.submit(provider, "kb").result()
    backend.close()


def test_registry_provider_fails_closed_for_thread_hop_across_two_generations(
    tmp_path,
):
    backend = SQLiteBackend(tmp_path / "authority.db")
    repository = ObjectIndexRepository(LocalObjectStore(tmp_path / "objects"))
    first_dir = tmp_path / "first"
    first_dir.mkdir()
    _publish(backend, repository, first_dir, "one")
    provider = RegistryIndexProvider(
        HAIndexReplica(
            IndexGenerationStore(backend),
            repository,
            tmp_path / "cache",
            installer=Installer(),
        ),
        SimpleNamespace(
            get_by_storage_id=lambda value: {
                "tenant_id": "tenant",
                "storage_id": value,
            }
        ),
        worker_id="node-a",
        reader_lease_seconds=30,
    )

    with provider.pin("kb"):
        second_dir = tmp_path / "second"
        second_dir.mkdir()
        _publish(backend, repository, second_dir, "two")
        with provider.pin("kb"):
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(provider, "kb")
                with pytest.raises(StaleIndexFence, match="lost"):
                    future.result()
    backend.close()
