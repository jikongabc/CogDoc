import pytest
from cogdoc.tools.embedder import Embedder
from cogdoc.tools.retriever.bm25_retriever import BM25Retriever
from cogdoc.tools.retriever.vector_retriever import VectorRetriever, _meta_from_stored


_OPTIONAL_CHUNK_META_FIELDS = (
    "parent_chunk_id",
    "section_title",
    "section_path",
    "section_level",
    "child_index_in_parent",
    "parent_child_count",
    "parent_char_count",
    "chunk_type",
    "document_profile",
    "chunking_strategy_version",
    "chunk_char_count",
    "chunk_quality_score",
)


# BM25 新 native 接口返回 (doc_id, score)，测试只需要一个命中。
class _DummyBM25:
    # BM25 新 native 接口返回 (doc_id, score)，测试只需要一个命中。
    def score_topk(self, tokenized_query, top_n):
        return [(0, 1.0)]


# 模拟旧 Chroma 索引结果缺少稳定 chunk_id。
class _DummyVectorCollection:
    # 模拟旧 Chroma 索引结果缺少稳定 chunk_id。
    def query(self, query_embeddings, n_results):
        return {
            "documents": [["legacy text"]],
            "ids": [["legacy-id"]],
            "metadatas": [
                [
                    {
                        "chunk_index": 0,
                        "source": "legacy.pdf",
                        "page": 1,
                        "page_start": 1,
                        "page_end": 1,
                        "origin": "file",
                    }
                ]
            ],
            "distances": [[0.1]],
        }


# 验证 bm25 search rejects legacy docs without chunk id。
def test_bm25_search_rejects_legacy_docs_without_chunk_id():
    # 旧 BM25 索引不能现场补 chunk_id，必须提示重建。
    retriever = BM25Retriever.__new__(BM25Retriever)
    from threading import RLock

    retriever._lock = RLock()
    retriever.bm25 = _DummyBM25()
    retriever.doc_registry = [
        {
            "text": "legacy text",
            "meta": {
                "chunk_index": 0,
                "source": "legacy.pdf",
                "page": 1,
                "page_start": 1,
                "page_end": 1,
                "origin": "file",
            },
        }
    ]

    with pytest.raises(RuntimeError, match="missing stable chunk_id"):
        retriever.search("legacy", top_k=1)


# 验证 vector search rejects legacy docs without chunk id。
def test_vector_search_rejects_legacy_docs_without_chunk_id(monkeypatch):
    # 旧向量索引同样不能绕过稳定 chunk identity 契约。
    retriever = VectorRetriever.__new__(VectorRetriever)
    retriever.collection = _DummyVectorCollection()
    monkeypatch.setattr(Embedder, "embed_query", lambda query: [0.0])

    with pytest.raises(RuntimeError, match="missing stable chunk_id"):
        retriever.search("legacy", top_k=1)


# 验证 retriever metadata preserves chunk context。
def test_retriever_metadata_preserves_chunk_context():
    # 定位上下文属于 chunk 契约的一部分，BM25 registry 与向量元数据都必须保留。
    meta = {
        "chunk_id": "chunk:1",
        "source_sha256": "sha",
        "local_chunk_index": 0,
        "chunk_index": 3,
        "source": "paper.pdf",
        "page": 1,
        "page_start": 1,
        "page_end": 2,
        "origin": "vector",
        "context": "前文：背景\n后文：结论",
    }
    doc = {"text": "正文", "meta": meta}

    assert BM25Retriever._clean_doc(doc)["meta"]["context"] == meta["context"]
    assert _meta_from_stored(meta)["context"] == meta["context"]


def _structured_doc(
    chunk_id: str = "chunk:structured", text: str = "structuralneedle"
) -> dict:
    return {
        "text": text,
        "meta": {
            "chunk_id": chunk_id,
            "source_sha256": "sha",
            "local_chunk_index": 2,
            "chunk_index": 7,
            "source": "paper.pdf",
            "page": 3,
            "page_start": 3,
            "page_end": 4,
            "origin": "file",
            "parent_chunk_id": "parent:1",
            "section_title": "2.1 Architecture",
            "section_path": "System / Design / Architecture",
            "section_level": 2,
            "child_index_in_parent": 1,
            "parent_child_count": 4,
            "parent_char_count": 420,
            "chunk_type": "table",
            "document_profile": "structured",
            "chunking_strategy_version": "adaptive-structural-v2",
            "chunk_char_count": len(text),
            "chunk_quality_score": 0.95,
        },
    }


# Vector/BM25 的写入物化与读取重建必须完整保留结构字段。
def test_retriever_storage_materialization_preserves_structure_metadata():
    doc = _structured_doc()
    expected = {key: doc["meta"][key] for key in _OPTIONAL_CHUNK_META_FIELDS}

    cleaned_meta = BM25Retriever._clean_doc(doc)["meta"]
    vector = VectorRetriever.__new__(VectorRetriever)
    ids, stored_metas, texts = vector._materialize([doc])
    restored_meta = _meta_from_stored(stored_metas[0])

    assert ids == ["chunk:structured"]
    assert texts == ["structuralneedle"]
    for key, value in expected.items():
        assert cleaned_meta[key] == value
        assert stored_metas[0][key] == value
        assert restored_meta[key] == value


# 旧索引元数据不含新字段时不应被注入空值或默认结构。
def test_retriever_storage_keeps_legacy_metadata_shape():
    doc = _structured_doc()
    for key in _OPTIONAL_CHUNK_META_FIELDS:
        doc["meta"].pop(key)

    cleaned_meta = BM25Retriever._clean_doc(doc)["meta"]
    vector = VectorRetriever.__new__(VectorRetriever)
    _, stored_metas, _ = vector._materialize([doc])
    restored_meta = _meta_from_stored(stored_metas[0])

    for key in _OPTIONAL_CHUNK_META_FIELDS:
        assert key not in cleaned_meta
        assert key not in stored_metas[0]
        assert key not in restored_meta


# BM25 registry 落盘重载后，load_source_chunks 与 search 两条读路都要透传结构元数据。
def test_bm25_structure_metadata_round_trips_through_persistence(tmp_path):
    structured = _structured_doc()
    other_a = _structured_doc("chunk:other-a", "otheralpha")
    other_a["meta"]["source"] = "other-a.pdf"
    other_b = _structured_doc("chunk:other-b", "otherbeta")
    other_b["meta"]["source"] = "other-b.pdf"
    for doc in (other_a, other_b):
        for key in _OPTIONAL_CHUNK_META_FIELDS:
            doc["meta"].pop(key)

    persist_directory = str(tmp_path / "bm25")
    BM25Retriever("structure", persist_directory=persist_directory).index(
        [structured, other_a, other_b]
    )
    restored = BM25Retriever("structure", persist_directory=persist_directory)

    loaded_meta = restored.load_source_chunks("paper.pdf")[0]["meta"]
    hits = restored.search("structuralneedle", top_k=3)
    assert hits
    searched_meta = hits[0]["meta"]
    for key in _OPTIONAL_CHUNK_META_FIELDS:
        assert loaded_meta[key] == structured["meta"][key]
        assert searched_meta[key] == structured["meta"][key]
