from cogdoc.service.retrieval_pipeline import (
    DERIVED_KNOWLEDGE_LEXICAL_CHANNEL,
    DERIVED_KNOWLEDGE_CHANNEL,
    DERIVED_KNOWLEDGE_VECTOR_CHANNEL,
    HYBRID_CHANNEL,
    RAG_LEXICAL_CHANNEL,
    RAG_VECTOR_CHANNEL,
    RetrievalQuery,
    RetrievalScope,
    build_retrieval_queries,
    retrieve_candidate_pool,
)


def _doc(chunk_id: str) -> dict:
    return {"text": chunk_id, "meta": {"chunk_id": chunk_id}}


def _ids(docs) -> list[str]:
    return [doc["meta"]["chunk_id"] for doc in docs]


def test_build_queries_deduplicates_normalized_text_and_merges_roles():
    queries = build_retrieval_queries(
        "  SAME   Query ",
        rewritten_queries=("same query", " Rewrite "),
        evidence_requirements=(
            {
                "requirement_id": "r1",
                "retrieval_query": "ＳＡＭＥ query",
                "recovery_query": "recover one",
            },
            {
                "requirement_id": "r2",
                "retrieval_query": "Second query",
                "recovery_query": "recover two",
            },
        ),
    )

    assert [query.text for query in queries] == [
        "SAME Query",
        "Second query",
        "Rewrite",
    ]
    assert queries[0].is_original is True
    assert queries[0].requirement_ids == ("r1",)
    assert queries[1].requirement_ids == ("r2",)


def test_prioritized_requirements_use_recovery_query_before_remaining_queries():
    requirements = (
        {
            "requirement_id": "r1",
            "retrieval_query": "first retrieval",
            "recovery_query": "first recovery",
        },
        {
            "requirement_id": "r2",
            "retrieval_query": "second retrieval",
            "recovery_query": "second recovery",
        },
        {
            "requirement_id": "r3",
            "retrieval_query": "third retrieval",
            "recovery_query": "",
        },
    )

    queries = build_retrieval_queries(
        "original",
        rewritten_queries=("rewrite",),
        evidence_requirements=requirements,
        prioritized_requirement_ids=("r3", "r2"),
    )

    assert [query.text for query in queries] == [
        "original",
        "third retrieval",
        "second recovery",
        "first retrieval",
        "rewrite",
    ]
    assert [query.requirement_ids for query in queries[1:4]] == [
        ("r3",),
        ("r2",),
        ("r1",),
    ]


class _Engine:
    def __init__(self, results):
        self.results = results
        self.calls = []

    def search(self, query, top_k):
        self.calls.append((query, top_k))
        return self.results.get(query, [])


class _Knowledge:
    def __init__(self, results):
        self.results = results
        self.calls = []

    def search(self, kb_id, query, top_k):
        self.calls.append((kb_id, query, top_k))
        return self.results.get(query, [])


class _BatchEngine(_Engine):
    def search_many(self, queries, top_k):
        self.calls.append((tuple(queries), top_k))
        return [self.results.get(query, []) for query in queries]

    def search(self, query, top_k):
        raise AssertionError("batch-capable engine must not use scalar search")


class _BatchKnowledge(_Knowledge):
    def search_many(self, kb_id, queries, top_k):
        self.calls.append((kb_id, tuple(queries), top_k))
        return [self.results.get(query, []) for query in queries]

    def search(self, kb_id, query, top_k):
        raise AssertionError(
            "batch-capable knowledge retriever must not use scalar search"
        )


class _MultiRouteEngine:
    def search_many_channels(self, queries, top_k):
        return [
            {
                "vector": [_doc(f"vector-{query}"), _doc("shared")],
                "bm25": [_doc(f"lexical-{query}"), _doc("shared")],
            }
            for query in queries
        ]


class _MultiRouteKnowledge:
    def search_many_channels(self, kb_id, queries, top_k):
        return [
            {
                "embedding": [_doc(f"knowledge-vector-{query}")],
                "lexical": [_doc(f"knowledge-lexical-{query}")],
            }
            for query in queries
        ]


class _Feedback:
    def __init__(self, boosts=None):
        self.boosts = boosts or {}
        self.calls = []

    def boosts_for_query(self, kb_id, query):
        self.calls.append((kb_id, query))
        return self.boosts


def test_pipeline_retrieves_both_channels_and_fuses_provenance():
    engine = _Engine(
        {
            "original": [_doc("shared"), _doc("document")],
            "focused": [_doc("focused-document")],
        }
    )
    knowledge = _Knowledge(
        {
            "original": [_doc("knowledge")],
            "focused": [_doc("shared")],
        }
    )
    feedback = _Feedback()
    queries = [
        RetrievalQuery("original", is_original=True),
        RetrievalQuery("focused", requirement_ids=("r1",)),
    ]

    result = retrieve_candidate_pool(
        engine,
        knowledge,
        feedback,
        kb_id="kb",
        original_query="original",
        queries=queries,
        top_k=3,
        rrf_k=60,
        retrieval_round=2,
    )

    assert result.queries == queries
    assert result.ranking_count == 4
    assert result.channel_counts == {
        HYBRID_CHANNEL: 3,
        DERIVED_KNOWLEDGE_CHANNEL: 2,
    }
    assert engine.calls == [("original", 3), ("focused", 3)]
    assert knowledge.calls == [("kb", "original", 3), ("kb", "focused", 3)]
    shared = next(doc for doc in result.docs if doc["meta"]["chunk_id"] == "shared")
    assert shared["retrieval"]["matched_queries"] == ["original", "focused"]
    assert shared["retrieval"]["matched_channels"] == [
        HYBRID_CHANNEL,
        DERIVED_KNOWLEDGE_CHANNEL,
    ]
    assert shared["retrieval"]["matched_requirement_ids"] == ["r1"]
    assert shared["retrieval"]["retrieval_round"] == 2


def test_pipeline_keeps_all_four_production_routes_visible_until_fusion():
    result = retrieve_candidate_pool(
        _MultiRouteEngine(),
        _MultiRouteKnowledge(),
        None,
        kb_id="kb",
        original_query="query",
        queries=[RetrievalQuery("query", is_original=True)],
        top_k=3,
        rrf_k=60,
    )

    assert result.ranking_count == 4
    assert result.channel_counts == {
        RAG_VECTOR_CHANNEL: 2,
        RAG_LEXICAL_CHANNEL: 2,
        DERIVED_KNOWLEDGE_VECTOR_CHANNEL: 1,
        DERIVED_KNOWLEDGE_LEXICAL_CHANNEL: 1,
    }
    shared = next(doc for doc in result.docs if doc["meta"]["chunk_id"] == "shared")
    assert shared["retrieval"]["matched_channels"] == [
        RAG_VECTOR_CHANNEL,
        RAG_LEXICAL_CHANNEL,
    ]
    assert set(shared["retrieval"]["channel_contributions"]) == {
        RAG_VECTOR_CHANNEL,
        RAG_LEXICAL_CHANNEL,
    }


def test_pipeline_route_weight_can_disable_one_route_without_skipping_others():
    result = retrieve_candidate_pool(
        _MultiRouteEngine(),
        _MultiRouteKnowledge(),
        None,
        kb_id="kb",
        original_query="query",
        queries=[RetrievalQuery("query")],
        top_k=3,
        rrf_k=60,
        route_weights={RAG_VECTOR_CHANNEL: 0.0},
    )

    assert "vector-query" not in _ids(result.docs)
    assert "lexical-query" in _ids(result.docs)
    assert result.channel_counts[RAG_VECTOR_CHANNEL] == 2


def test_pipeline_uses_batch_search_without_changing_query_provenance():
    engine = _BatchEngine(
        {
            "original": [_doc("shared")],
            "focused": [_doc("shared"), _doc("focused")],
        }
    )
    queries = [
        RetrievalQuery("original", is_original=True),
        RetrievalQuery("focused", requirement_ids=("r1",)),
    ]

    result = retrieve_candidate_pool(
        engine,
        _Knowledge({}),
        None,
        kb_id="kb",
        original_query="original",
        queries=queries,
        top_k=3,
        rrf_k=60,
    )

    assert engine.calls == [(("original", "focused"), 3)]
    assert result.queries == queries
    shared = next(doc for doc in result.docs if doc["meta"]["chunk_id"] == "shared")
    assert shared["retrieval"]["matched_queries"] == ["original", "focused"]
    assert shared["retrieval"]["matched_requirement_ids"] == ["r1"]


def test_pipeline_batches_derived_knowledge_queries():
    knowledge = _BatchKnowledge({"original": [_doc("k1")], "focused": [_doc("k2")]})
    queries = [RetrievalQuery("original"), RetrievalQuery("focused")]

    result = retrieve_candidate_pool(
        _BatchEngine({}),
        knowledge,
        None,
        kb_id="kb",
        original_query="original",
        queries=queries,
        top_k=3,
        rrf_k=60,
    )

    assert knowledge.calls == [("kb", ("original", "focused"), 3)]
    assert result.channel_counts[DERIVED_KNOWLEDGE_CHANNEL] == 2


def test_pipeline_applies_existing_feedback_boost_ordering():
    engine = _Engine({"query": [_doc("c1"), _doc("c2")]})
    feedback = _Feedback({"c2": 0.5, "c1": -0.2})

    result = retrieve_candidate_pool(
        engine,
        _Knowledge({}),
        feedback,
        kb_id="kb",
        original_query="Original Query",
        queries=[RetrievalQuery("query", is_original=True)],
        top_k=3,
        rrf_k=60,
    )

    assert _ids(result.docs) == ["c2", "c1"]
    assert result.docs[0]["retrieval"]["feedback_boost"] == 0.5
    assert result.docs[1]["retrieval"]["feedback_boost"] == -0.2
    assert feedback.calls == [("kb", "Original Query")]
    assert result.feedback_error == ""


def test_pipeline_bypasses_feedback_failure_and_marks_result():
    class BrokenFeedback:
        def boosts_for_query(self, kb_id, query):
            raise RuntimeError("storage unavailable")

    result = retrieve_candidate_pool(
        _Engine({"query": [_doc("c1"), _doc("c2")]}),
        _Knowledge({}),
        BrokenFeedback(),
        kb_id="kb",
        original_query="query",
        queries=[RetrievalQuery("query", is_original=True)],
        top_k=3,
        rrf_k=60,
    )

    assert _ids(result.docs) == ["c1", "c2"]
    assert result.feedback_error == "RuntimeError"
    assert result.ranking_count == 1
    assert result.channel_counts == {
        HYBRID_CHANNEL: 2,
        DERIVED_KNOWLEDGE_CHANNEL: 0,
    }


class _ScopedEngine:
    def __init__(self):
        self.calls = []

    def search(self, query, top_k, *, scope):
        self.calls.append((query, top_k, scope))
        return [_doc("document")]


class _ScopedKnowledge:
    def __init__(self):
        self.calls = []

    def search(self, kb_id, query, top_k, *, scope):
        self.calls.append((kb_id, query, top_k, scope))
        return [_doc("knowledge")]


def test_pipeline_forwards_one_scope_to_every_enabled_channel():
    scope = RetrievalScope(allowed_sources=("a.pdf",))
    engine = _ScopedEngine()
    knowledge = _ScopedKnowledge()

    result = retrieve_candidate_pool(
        engine,
        knowledge,
        None,
        kb_id="kb",
        original_query="query",
        queries=[RetrievalQuery("query", is_original=True)],
        top_k=4,
        rrf_k=60,
        scope=scope,
    )

    assert engine.calls == [("query", 4, scope)]
    assert knowledge.calls == [("kb", "query", 4, scope)]
    assert result.channel_counts == {
        HYBRID_CHANNEL: 1,
        DERIVED_KNOWLEDGE_CHANNEL: 1,
    }


def test_pipeline_scope_can_disable_derived_knowledge_without_calling_it():
    scope = RetrievalScope(allowed_sources=("a.pdf",), include_derived_knowledge=False)
    engine = _ScopedEngine()
    knowledge = _ScopedKnowledge()

    result = retrieve_candidate_pool(
        engine,
        knowledge,
        None,
        kb_id="kb",
        original_query="query",
        queries=[RetrievalQuery("query", is_original=True)],
        top_k=4,
        rrf_k=60,
        scope=scope,
    )

    assert engine.calls == [("query", 4, scope)]
    assert knowledge.calls == []
    assert result.channel_counts == {
        HYBRID_CHANNEL: 1,
        DERIVED_KNOWLEDGE_CHANNEL: 0,
    }
