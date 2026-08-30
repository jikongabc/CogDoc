from threading import RLock

import pytest

from cogdoc.api.derived_knowledge_store import DerivedKnowledgeStore
from cogdoc.tools.retriever.derived_knowledge import (
    DerivedKnowledgeIndex,
    DerivedKnowledgeRetriever,
)


# 验证只召回已审核派生知识。
def test_derived_knowledge_retriever_returns_approved_only(tmp_path):
    store = DerivedKnowledgeStore(path=str(tmp_path / "knowledge.jsonl"))
    approved, _ = store.create(
        {
            "kb_id": "kb",
            "text": "差旅报销需要在七天内提交。",
            "status": "approved",
            "version": 2,
            "certainty": "high",
            "related_source": "policy.pdf",
            "related_source_sha256": "sha",
            "related_chunk_ids": ["chunk-1"],
            "related_page_start": 2,
            "related_page_end": 3,
            "related_chunk_text_hash": "hash",
            "related_anchor_text": "报销需要七天内提交",
        }
    )
    store.create(
        {
            "kb_id": "kb",
            "text": "差旅报销可以一个月后提交。",
            "status": "pending",
        }
    )

    docs = DerivedKnowledgeRetriever(store).search("kb", "差旅报销提交", top_k=5)

    assert len(docs) == 1
    assert docs[0]["meta"]["knowledge_id"] == approved["knowledge_id"]
    assert docs[0]["meta"]["kb_id"] == "kb"
    assert docs[0]["meta"]["version"] == 2
    assert docs[0]["meta"]["source_type"] == "derived_knowledge"
    assert docs[0]["meta"]["source"] == f"knowledge:{approved['knowledge_id']}"
    assert docs[0]["meta"]["related_chunk_ids"] == ["chunk-1"]
    assert docs[0]["meta"]["page"] == 2
    assert docs[0]["meta"]["page_start"] == 2
    assert docs[0]["meta"]["page_end"] == 3
    assert docs[0]["meta"]["related_chunk_text_hash"] == "hash"
    assert docs[0]["meta"]["related_anchor_text"] == "报销需要七天内提交"
    assert docs[0]["retrieval"]["search_channel"] == "derived_knowledge"
    assert docs[0]["retrieval"]["status_filter"] == "approved"
    assert docs[0]["retrieval"]["match_coverage"] > 0
    assert docs[0]["retrieval"]["query_term_count"] > 0
    assert docs[0]["retrieval"]["knowledge_term_count"] > 0
    assert "差旅" in docs[0]["retrieval"]["matched_terms"]


@pytest.mark.parametrize(
    ("expected_count", "actual_count"),
    [(2, 1), (0, 1)],
    ids=("partially-missing", "stale-residual-row"),
)
def test_derived_index_rebuilds_when_collection_count_disagrees_with_state(
    expected_count, actual_count
):
    class Store:
        @staticmethod
        def revision_token():
            return "current"

    class Embedder:
        EMBEDDING_CONTRACT_VERSION = "embedding-v1"

    class Collection:
        @staticmethod
        def count():
            return actual_count

    index = object.__new__(DerivedKnowledgeIndex)
    index._lock = RLock()
    index.store = Store()
    index.embedder = Embedder()
    index._read_state = lambda _kb_id: {
        "revision_token": "current",
        "embedding_contract": "embedding-v1",
        "count": expected_count,
    }
    index._collection = lambda _kb_id: Collection()
    rebuilt = []
    index.rebuild = lambda kb_id, *, revision_token=None: rebuilt.append(
        (kb_id, revision_token)
    )

    index.ensure_fresh("kb")

    assert rebuilt == [("kb", "current")]


class FakeIndex:
    def __init__(self, docs=None, fail=False):
        self.docs = docs or []
        self.fail = fail
        self.ensured = []

    def ensure_fresh(self, kb_id):
        self.ensured.append(kb_id)
        if self.fail:
            raise RuntimeError("index failed")

    def search(self, kb_id, query, top_k):
        return self.docs[:top_k]


class BatchIndex(FakeIndex):
    def __init__(self, rows):
        super().__init__()
        self.rows = rows
        self.batch_calls = []

    def search_many(self, kb_id, queries, top_k):
        self.batch_calls.append((kb_id, tuple(queries), top_k))
        return [self.rows.get(query, [])[:top_k] for query in queries]


def test_derived_knowledge_retriever_prefers_index(tmp_path):
    store = DerivedKnowledgeStore(path=str(tmp_path / "knowledge.jsonl"))
    indexed_doc = {
        "text": "向量索引知识",
        "meta": {
            "chunk_id": "knowledge:K1",
            "knowledge_id": "K1",
            "source_type": "derived_knowledge",
        },
        "retrieval": {"search_channel": "derived_knowledge_embedding"},
    }
    index = FakeIndex([indexed_doc])

    docs = DerivedKnowledgeRetriever(store, index=index).search(
        "kb", "任意问题", top_k=5
    )

    assert docs == [indexed_doc]
    assert index.ensured == ["kb"]


def test_derived_knowledge_retriever_falls_back_when_index_fails(tmp_path):
    store = DerivedKnowledgeStore(path=str(tmp_path / "knowledge.jsonl"))
    approved, _ = store.create(
        {
            "kb_id": "kb",
            "text": "合同审批必须先完成法务复核。",
            "status": "approved",
        }
    )

    docs = DerivedKnowledgeRetriever(store, index=FakeIndex(fail=True)).search(
        "kb", "合同审批法务", top_k=5
    )

    assert len(docs) == 1
    assert docs[0]["meta"]["knowledge_id"] == approved["knowledge_id"]
    assert docs[0]["retrieval"]["search_channel"] == "derived_knowledge"


def test_derived_knowledge_retriever_batches_index_and_falls_back_per_empty_row(
    tmp_path,
):
    store = DerivedKnowledgeStore(path=str(tmp_path / "knowledge.jsonl"))
    store.create(
        {
            "kb_id": "kb",
            "text": "合同审批必须完成法务复核。",
            "status": "approved",
        }
    )
    indexed = {
        "text": "indexed",
        "meta": {"chunk_id": "knowledge:k1", "source_type": "derived_knowledge"},
        "retrieval": {"search_channel": "derived_knowledge_embedding"},
    }
    index = BatchIndex({"indexed query": [indexed]})

    rows = DerivedKnowledgeRetriever(store, index=index).search_many(
        "kb", ["indexed query", "合同审批法务"], top_k=3
    )

    assert rows[0] == [indexed]
    assert rows[1][0]["retrieval"]["search_channel"] == "derived_knowledge"
    assert index.ensured == ["kb"]
    assert index.batch_calls == [("kb", ("indexed query", "合同审批法务"), 3)]


def test_derived_knowledge_multi_route_runs_embedding_and_lexical_together(tmp_path):
    store = DerivedKnowledgeStore(path=str(tmp_path / "knowledge.jsonl"))
    lexical, _ = store.create(
        {
            "kb_id": "kb",
            "text": "合同编号 ZX-42 必须完成法务复核。",
            "status": "approved",
        }
    )
    indexed = {
        "text": "语义相近的审批要求",
        "meta": {
            "chunk_id": "knowledge:semantic",
            "source_type": "derived_knowledge",
        },
        "retrieval": {"search_channel": "derived_knowledge_embedding"},
    }

    routes = DerivedKnowledgeRetriever(
        store, index=FakeIndex([indexed])
    ).search_channels("kb", "ZX-42", top_k=3)

    assert routes["embedding"] == [indexed]
    assert routes["lexical"][0]["meta"]["knowledge_id"] == lexical["knowledge_id"]
    assert routes["lexical"][0]["retrieval"]["search_channel"] == ("derived_knowledge")
