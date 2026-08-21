from __future__ import annotations

import sqlite3

import pytest

from cogdoc.ha.portable_index import (
    PortableIndexIntegrityError,
    PortableIndexInstaller,
    PortableIndexStore,
)


def _documents():
    return [
        {
            "text": "alpha",
            "meta": {
                "chunk_id": "chunk-a",
                "document_id": "doc-a",
                "source": "a.md",
                "source_sha256": "a" * 64,
                "local_chunk_index": 0,
                "chunk_index": 0,
                "page": 1,
                "page_start": 1,
                "page_end": 1,
                "origin": "file",
            },
        },
        {
            "text": "beta",
            "meta": {
                "chunk_id": "chunk-b",
                "document_id": "doc-b",
                "source": "b.md",
                "source_sha256": "b" * 64,
                "local_chunk_index": 0,
                "chunk_index": 1,
                "page": 1,
                "page_start": 1,
                "page_end": 1,
                "origin": "file",
            },
        },
    ]


def test_portable_index_round_trip_is_strict_and_ordered(tmp_path):
    path = tmp_path / "index.sqlite"
    store = PortableIndexStore()
    metadata = store.write(
        path,
        _documents(),
        [[1.0, 0.0, 0.5], [0.0, 1.0, -0.5]],
        embedding_model="model",
        dimensions=3,
        chunk_version="chunks-v1",
    )

    assert metadata.expected_count == 2
    loaded_metadata, documents, embeddings = store.load(path)
    assert loaded_metadata == metadata
    assert documents == _documents()
    assert embeddings == [[1.0, 0.0, 0.5], [0.0, 1.0, -0.5]]
    assert store.verify(path) == metadata


def test_portable_index_rejects_mismatched_and_non_finite_embeddings(tmp_path):
    store = PortableIndexStore()
    with pytest.raises(ValueError, match="count"):
        store.write(
            tmp_path / "missing.sqlite",
            _documents(),
            [[1.0, 2.0, 3.0]],
            embedding_model="model",
            dimensions=3,
            chunk_version="v1",
        )
    with pytest.raises(ValueError, match="non-finite"):
        store.write(
            tmp_path / "nan.sqlite",
            _documents()[:1],
            [[1.0, float("nan"), 3.0]],
            embedding_model="model",
            dimensions=3,
            chunk_version="v1",
        )


def test_portable_index_detects_logical_row_tampering(tmp_path):
    path = tmp_path / "index.sqlite"
    store = PortableIndexStore()
    store.write(
        path,
        _documents(),
        [[1.0, 0.0, 0.5], [0.0, 1.0, -0.5]],
        embedding_model="model",
        dimensions=3,
        chunk_version="v1",
    )
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE chunks SET text='tampered' WHERE ordinal=0")
        connection.commit()

    with pytest.raises(PortableIndexIntegrityError, match="checksum"):
        store.verify(path)


def test_portable_index_rejects_duplicate_chunk_identity_without_partial_file(tmp_path):
    path = tmp_path / "index.sqlite"
    documents = _documents()
    documents[1]["meta"]["chunk_id"] = "chunk-a"
    with pytest.raises(ValueError, match="duplicated"):
        PortableIndexStore().write(
            path,
            documents,
            [[1.0, 0.0, 0.5], [0.0, 1.0, -0.5]],
            embedding_model="model",
            dimensions=3,
            chunk_version="v1",
        )
    assert not path.exists()


def test_portable_index_rejects_incomplete_document_identity_without_partial_file(
    tmp_path,
):
    path = tmp_path / "incomplete.sqlite"
    documents = _documents()
    del documents[0]["meta"]["document_id"]

    with pytest.raises(ValueError, match="document_id"):
        PortableIndexStore().write(
            path,
            documents,
            [[1.0, 0.0, 0.5], [0.0, 1.0, -0.5]],
            embedding_model="model",
            dimensions=3,
            chunk_version="v1",
        )

    assert not path.exists()


def test_portable_index_rejects_view_substitution(tmp_path):
    path = tmp_path / "malicious.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE metadata(format,embedding_model,dimensions,chunk_version,expected_count)"
        )
        connection.execute("CREATE VIEW chunks AS SELECT 1")
    with pytest.raises(PortableIndexIntegrityError, match="schema"):
        PortableIndexStore().verify(path)


def test_portable_installer_commits_only_consistent_vector_and_bm25(
    tmp_path, monkeypatch
):
    from cogdoc.config.settings import get_settings
    from cogdoc.tools.embedder import Embedder

    monkeypatch.setenv("COGDOC_DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()
    monkeypatch.setattr(Embedder, "EMBEDDING_DIM", 3)
    monkeypatch.setattr(Embedder, "EMBEDDING_CONTRACT_VERSION", "model-contract")
    cache = tmp_path / "cache" / "generation"
    cache.mkdir(parents=True)
    path = cache / "portable-index.sqlite"
    PortableIndexStore().write(
        path,
        _documents(),
        [[1.0, 0.0, 0.5], [0.0, 1.0, -0.5]],
        embedding_model="model-contract",
        dimensions=3,
        chunk_version="v1",
    )

    installer = PortableIndexInstaller()
    engine = installer.install("kb", "generation", path)
    assert engine.count() == 2
    assert engine.is_consistent() is True
    # Marker replay validates both stores rather than rewriting them.
    assert installer.install("kb", "generation", path).count() == 2
    assert not (cache / ".installed.json").exists()
    assert (cache.parent / ".installed-generation.json").exists()
    get_settings.cache_clear()


def test_portable_installer_rejects_incompatible_embedding_contract(
    tmp_path, monkeypatch
):
    from cogdoc.config.settings import get_settings
    from cogdoc.tools.embedder import Embedder

    monkeypatch.setenv("COGDOC_DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()
    monkeypatch.setattr(Embedder, "EMBEDDING_DIM", 3)
    monkeypatch.setattr(Embedder, "EMBEDDING_CONTRACT_VERSION", "current")
    cache = tmp_path / "cache" / "generation"
    cache.mkdir(parents=True)
    path = cache / "portable-index.sqlite"
    PortableIndexStore().write(
        path,
        _documents()[:1],
        [[1.0, 0.0, 0.5]],
        embedding_model="old",
        dimensions=3,
        chunk_version="v1",
    )

    with pytest.raises(PortableIndexIntegrityError, match="incompatible"):
        PortableIndexInstaller().install("kb", "generation", path)
    assert not (cache.parent / ".installed-generation.json").exists()
    get_settings.cache_clear()
