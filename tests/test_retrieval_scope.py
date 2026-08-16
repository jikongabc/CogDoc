from threading import RLock

import pytest

from cogdoc.tools.retriever.bm25_retriever import BM25Retriever
from cogdoc.tools.retriever.derived_knowledge import (
    DerivedKnowledgeIndex,
    DerivedKnowledgeRetriever,
)
from cogdoc.tools.retriever.hybrid import HybridRetriever
from cogdoc.tools.retriever.scope import RetrievalAccessMode, RetrievalScope
from cogdoc.tools.retriever.vector_retriever import VectorRetriever


def _doc(chunk_id: str, source: str, *, text: str | None = None) -> dict:
    return {
        "text": text or chunk_id,
        "meta": {
            "chunk_id": chunk_id,
            "source_sha256": f"sha:{source}",
            "local_chunk_index": 0,
            "chunk_index": 0,
            "source": source,
            "page": 1,
            "page_start": 1,
            "page_end": 1,
            "origin": "file",
        },
    }


def test_scope_normalizes_allowlist_and_matches_physical_and_derived_docs():
    scope = RetrievalScope(allowed_sources=("a.pdf", "", "a.pdf", "b.pdf"))

    assert scope.allowed_sources == ("a.pdf", "b.pdf")
    assert scope.allows_document(_doc("a", "a.pdf")) is True
    assert scope.allows_document(_doc("c", "c.pdf")) is False
    assert (
        scope.allows_document(
            {
                "text": "knowledge",
                "meta": {
                    "source_type": "derived_knowledge",
                    "source": "knowledge:k1",
                    "related_source": "a.pdf",
                },
            }
        )
        is True
    )


def test_scope_rejects_a_scalar_source_allowlist():
    with pytest.raises(TypeError, match="sequence"):
        RetrievalScope(allowed_sources="a.pdf")  # type: ignore[arg-type]


def test_scope_preserves_exact_source_identity():
    scope = RetrievalScope(allowed_sources=(" a.pdf ",))

    assert scope.allows_source(" a.pdf ") is True
    assert scope.allows_source("a.pdf") is False


def test_scope_distinguishes_deny_from_all_and_rejects_ambiguous_subset():
    unrestricted = RetrievalScope()
    denied = RetrievalScope.deny()

    assert unrestricted.access_mode is RetrievalAccessMode.ALL
    assert unrestricted.allows_source("secret.pdf") is True
    assert denied.access_mode is RetrievalAccessMode.DENY
    assert denied.allows_source("secret.pdf") is False
    assert denied.allows_document(_doc("secret", "secret.pdf")) is False
    with pytest.raises(ValueError, match="at least one source"):
        RetrievalScope(access_mode=RetrievalAccessMode.SUBSET)


def test_scope_intersection_never_promotes_empty_authorization_to_all():
    task = RetrievalScope(allowed_sources=("a.pdf", "b.pdf"))
    authorization = RetrievalScope(allowed_sources=("b.pdf", "c.pdf"))

    combined = task.intersect(authorization)
    assert combined.allowed_sources == ("b.pdf",)
    assert combined.access_mode is RetrievalAccessMode.SUBSET
    assert task.intersect(RetrievalScope(allowed_sources=("c.pdf",))).denies_all
    assert RetrievalScope().intersect(RetrievalScope.deny()).denies_all


def test_deny_scope_stops_vector_search_before_embedding_or_backend(monkeypatch):
    collection = _VectorCollection()
    retriever = VectorRetriever.__new__(VectorRetriever)
    retriever.collection = collection
    embedded = []
    monkeypatch.setattr(
        "cogdoc.tools.retriever.vector_retriever.Embedder.embed_query",
        lambda query: embedded.append(query),
    )

    assert retriever.search("secret", scope=RetrievalScope.deny()) == []
    assert embedded == []
    assert collection.options is None


class _VectorCollection:
    def __init__(self):
        self.options = None

    def query(self, **options):
        self.options = options
        return {
            "documents": [["target"]],
            "ids": [["target"]],
            "metadatas": [[_doc("target", "a.pdf")["meta"]]],
            "distances": [[0.1]],
        }


def test_vector_scope_is_sent_to_chroma_before_top_k(monkeypatch):
    collection = _VectorCollection()
    retriever = VectorRetriever.__new__(VectorRetriever)
    retriever.collection = collection
    monkeypatch.setattr(
        "cogdoc.tools.retriever.vector_retriever.Embedder.embed_query",
        lambda query: [0.1, 0.2],
    )

    docs = retriever.search(
        "query", top_k=1, scope=RetrievalScope(allowed_sources=("a.pdf",))
    )

    assert [doc["meta"]["source"] for doc in docs] == ["a.pdf"]
    assert collection.options["n_results"] == 1
    assert collection.options["where"] == {"source": "a.pdf"}


def test_vector_unscoped_search_keeps_the_legacy_query_shape(monkeypatch):
    collection = _VectorCollection()
    retriever = VectorRetriever.__new__(VectorRetriever)
    retriever.collection = collection
    monkeypatch.setattr(
        "cogdoc.tools.retriever.vector_retriever.Embedder.embed_query",
        lambda query: [0.1, 0.2],
    )

    retriever.search("query", top_k=1)

    assert set(collection.options) == {"query_embeddings", "n_results"}


class _BatchVectorCollection:
    def __init__(self):
        self.options = None

    def query(self, **options):
        self.options = options
        return {
            "documents": [["first"], ["second"]],
            "ids": [["first"], ["second"]],
            "metadatas": [
                [_doc("first", "a.pdf")["meta"]],
                [_doc("second", "a.pdf")["meta"]],
            ],
            "distances": [[0.1], [0.2]],
        }


def test_vector_search_many_batches_embeddings_and_chroma_query(monkeypatch):
    collection = _BatchVectorCollection()
    retriever = VectorRetriever.__new__(VectorRetriever)
    retriever.collection = collection
    embedded = []

    def embed_queries(queries):
        embedded.append(list(queries))
        return [[0.1, 0.2], [0.3, 0.4]]

    monkeypatch.setattr(
        "cogdoc.tools.retriever.vector_retriever.Embedder.embed_queries",
        embed_queries,
    )

    rows = retriever.search_many(
        ["first query", "second query"],
        top_k=1,
        scope=RetrievalScope(allowed_sources=("a.pdf",)),
    )

    assert embedded == [["first query", "second query"]]
    assert [[doc["meta"]["chunk_id"] for doc in row] for row in rows] == [
        ["first"],
        ["second"],
    ]
    assert collection.options["where"] == {"source": "a.pdf"}
    assert len(collection.options["query_embeddings"]) == 2


class _LegacyNativeBm25:
    """Models a loaded extension from before score_topk_filtered existed."""

    def __init__(self):
        self.calls = []

    def score_topk(self, query, top_n):
        self.calls.append((query, top_n))
        return [(0, 100.0), (1, 90.0), (2, 80.0)][:top_n]


def test_bm25_stale_native_fallback_filters_the_full_ranking_before_top_k():
    native = _LegacyNativeBm25()
    retriever = BM25Retriever.__new__(BM25Retriever)
    retriever._lock = RLock()
    retriever.bm25 = native
    retriever.doc_registry = [
        _doc("other-1", "other.pdf"),
        _doc("other-2", "other.pdf"),
        _doc("target", "target.pdf"),
    ]
    retriever._tokenize = lambda query: [query]

    docs = retriever.search(
        "query",
        top_k=1,
        scope=RetrievalScope(allowed_sources=("target.pdf",)),
    )

    assert [doc["meta"]["chunk_id"] for doc in docs] == ["target"]
    assert native.calls == [(["query"], 3)]


class _ScopedChannel:
    def __init__(self, channel: str):
        self.channel = channel
        self.calls = []

    def count(self):
        return 1

    def search(self, query, top_k, *, scope):
        self.calls.append((query, top_k, scope))
        doc = _doc(f"{self.channel}-target", "target.pdf")
        doc["retrieval"] = {"search_channel": self.channel}
        return [doc]


def test_hybrid_forwards_scope_to_both_channels_before_fusion():
    vector = _ScopedChannel("vector")
    bm25 = _ScopedChannel("bm25")
    retriever = HybridRetriever(vector, bm25)
    scope = RetrievalScope(allowed_sources=("target.pdf",))

    docs = retriever.search("query", top_k=1, scope=scope)

    assert vector.calls == [("query", 3, scope)]
    assert bm25.calls == [("query", 3, scope)]
    assert {doc["meta"]["source"] for doc in docs} == {"target.pdf"}


class _BatchVectorChannel(_ScopedChannel):
    def search_many(self, queries, top_k):
        self.calls.append((tuple(queries), top_k))
        rows = []
        for query in queries:
            doc = _doc(f"vector-{query}", "target.pdf")
            doc["retrieval"] = {"search_channel": "vector"}
            rows.append([doc])
        return rows


class _ScalarChannel(_ScopedChannel):
    def search(self, query, top_k):
        self.calls.append((query, top_k))
        doc = _doc(f"bm25-{query}", "target.pdf")
        doc["retrieval"] = {"search_channel": "bm25"}
        return [doc]


def test_hybrid_search_many_batches_vector_and_preserves_per_query_bm25():
    vector = _BatchVectorChannel("vector")
    bm25 = _ScalarChannel("bm25")
    retriever = HybridRetriever(vector, bm25)

    rows = retriever.search_many(["q1", "q2"], top_k=1)

    assert vector.calls == [(("q1", "q2"), 3)]
    assert bm25.calls == [("q1", 3), ("q2", 3)]
    assert len(rows) == 2
    assert all(len(row) == 1 for row in rows)


def test_hybrid_search_many_channels_preserves_unfused_rankings():
    vector = _BatchVectorChannel("vector")
    bm25 = _ScalarChannel("bm25")
    retriever = HybridRetriever(vector, bm25)

    rows = retriever.search_many_channels(["q1", "q2"], top_k=1)

    assert [[doc["meta"]["chunk_id"] for doc in row["vector"]] for row in rows] == [
        ["vector-q1"],
        ["vector-q2"],
    ]
    assert [[doc["meta"]["chunk_id"] for doc in row["bm25"]] for row in rows] == [
        ["bm25-q1"],
        ["bm25-q2"],
    ]


def test_hybrid_route_failure_degrades_to_the_healthy_route():
    class FailingVector(_ScalarChannel):
        def search(self, query, top_k):
            raise RuntimeError("vector backend unavailable")

    retriever = HybridRetriever(FailingVector("vector"), _ScalarChannel("bm25"))

    routes = retriever.search_channels("query", top_k=1)

    assert routes["vector"] == []
    assert [doc["meta"]["chunk_id"] for doc in routes["bm25"]] == ["bm25-query"]


class _KnowledgeStore:
    def __init__(self):
        self.calls = []

    def list(self, **filters):
        self.calls.append(filters)
        return [
            {
                "knowledge_id": "other",
                "kb_id": "kb",
                "text": "shared query",
                "related_source": "other.pdf",
                "status": "approved",
            },
            {
                "knowledge_id": "target",
                "kb_id": "kb",
                "text": "shared query",
                "related_source": "target.pdf",
                "status": "approved",
            },
        ]


def test_derived_knowledge_lexical_scope_filters_rows_before_top_k():
    store = _KnowledgeStore()
    retriever = DerivedKnowledgeRetriever(store, enable_index=False)

    docs = retriever.search(
        "kb",
        "shared query",
        top_k=1,
        scope=RetrievalScope(allowed_sources=("target.pdf",)),
    )

    assert [doc["meta"]["knowledge_id"] for doc in docs] == ["target"]
    assert [doc["meta"]["related_source"] for doc in docs] == ["target.pdf"]


def test_derived_knowledge_channel_can_be_disabled_without_store_access():
    store = _KnowledgeStore()
    retriever = DerivedKnowledgeRetriever(store, enable_index=False)

    docs = retriever.search(
        "kb",
        "shared query",
        top_k=1,
        scope=RetrievalScope(include_derived_knowledge=False),
    )

    assert docs == []
    assert store.calls == []


class _KnowledgeCollection:
    def __init__(self):
        self.options = None

    def count(self):
        return 2

    def query(self, **options):
        self.options = options
        meta = {
            "knowledge_id": "target",
            "kb_id": "kb",
            "related_source": "target.pdf",
            "source_sha256": "sha",
        }
        return {
            "documents": [["shared query"]],
            "metadatas": [[meta]],
            "distances": [[0.2]],
        }


def test_derived_knowledge_index_scope_is_sent_to_chroma_before_top_k():
    collection = _KnowledgeCollection()
    index = DerivedKnowledgeIndex.__new__(DerivedKnowledgeIndex)
    index._collection = lambda kb_id: collection
    index.embedder = type(
        "Embedder", (), {"embed_query": staticmethod(lambda q: [0.2])}
    )

    docs = index.search(
        "kb",
        "query",
        1,
        scope=RetrievalScope(allowed_sources=("target.pdf",)),
    )

    assert [doc["meta"]["related_source"] for doc in docs] == ["target.pdf"]
    assert collection.options["where"] == {"related_source": "target.pdf"}
