import copy

import pytest

from cogdoc.tools.retriever.fusion import (
    RankedCandidateList,
    fuse_ranked_candidates,
    select_rerank_candidates,
)


def _doc(chunk_id: str, *, score: float | None = None) -> dict:
    doc = {"text": chunk_id, "meta": {"chunk_id": chunk_id}}
    if score is not None:
        doc["retrieval"] = {"retrieval_score": score}
    return doc


def _ids(docs) -> list[str]:
    return [doc["meta"]["chunk_id"] for doc in docs]


def test_later_query_candidate_is_not_starved_by_original_results():
    original = [_doc(f"original-{index}") for index in range(9)]
    later = _doc("requirement-hit")

    fused = fuse_ranked_candidates(
        [
            RankedCandidateList("original", "hybrid", original, is_original=True),
            RankedCandidateList("focused", "hybrid", [later], requirement_ids=("r1",)),
        ],
        rrf_k=60,
        top_n=9,
    )

    assert "requirement-hit" in _ids(fused)
    assert _ids(fused).index("requirement-hit") < 2


def test_fusion_accumulates_distinct_rankings_and_preserves_provenance():
    first = _doc("shared", score=0.2)
    duplicate = _doc("shared", score=999.0)
    best = _doc("shared", score=0.9)
    rankings = [
        RankedCandidateList(
            "original",
            "hybrid",
            [_doc("other"), first, duplicate],
            requirement_ids=("r1",),
            is_original=True,
            retrieval_round=1,
        ),
        RankedCandidateList(
            "focused",
            "derived_knowledge",
            [best],
            requirement_ids=("r2", "r1"),
            retrieval_round=2,
        ),
    ]

    fused = fuse_ranked_candidates(rankings, rrf_k=60)
    shared = next(doc for doc in fused if doc["meta"]["chunk_id"] == "shared")
    retrieval = shared["retrieval"]

    assert retrieval["query_fusion_score"] == pytest.approx(1 / 62 + 1 / 61)
    assert retrieval["query_hit_count"] == 2
    assert retrieval["matched_queries"] == ["original", "focused"]
    assert retrieval["matched_channels"] == ["hybrid", "derived_knowledge"]
    assert retrieval["matched_requirement_ids"] == ["r1", "r2"]
    assert retrieval["best_query_rank"] == 1
    assert retrieval["original_query_hit"] is True
    assert retrieval["retrieval_round"] == 2
    assert retrieval["retrieval_score"] == 0.9
    assert retrieval["rewrite_query"] == "focused"


def test_same_query_across_channels_scores_twice_but_counts_one_query_hit():
    fused = fuse_ranked_candidates(
        [
            RankedCandidateList("query", "hybrid", [_doc("shared")]),
            RankedCandidateList("query", "derived_knowledge", [_doc("shared")]),
        ],
        rrf_k=10,
    )

    retrieval = fused[0]["retrieval"]
    assert retrieval["query_fusion_score"] == pytest.approx(2 / 11)
    assert retrieval["query_hit_count"] == 1
    assert retrieval["matched_channels"] == ["hybrid", "derived_knowledge"]


def test_weighted_fusion_exposes_per_channel_contributions():
    fused = fuse_ranked_candidates(
        [
            RankedCandidateList("query", "rag_vector", [_doc("vector")], weight=2.0),
            RankedCandidateList("query", "rag_bm25", [_doc("lexical")], weight=1.0),
        ],
        rrf_k=10,
    )

    assert _ids(fused) == ["vector", "lexical"]
    assert fused[0]["retrieval"]["channel_contributions"] == pytest.approx(
        {"rag_vector": 2 / 11}
    )


@pytest.mark.parametrize("weight", [-1.0, float("inf"), float("nan")])
def test_fusion_rejects_invalid_route_weight(weight):
    with pytest.raises(ValueError, match="ranking weight"):
        fuse_ranked_candidates(
            [RankedCandidateList("query", "route", [_doc("c1")], weight=weight)],
            rrf_k=60,
        )


def test_fusion_ties_are_deterministic_by_chunk_id():
    rankings = [
        RankedCandidateList("q1", "hybrid", [_doc("b"), _doc("a")]),
        RankedCandidateList("q2", "hybrid", [_doc("a"), _doc("b")]),
    ]

    for _ in range(5):
        assert _ids(fuse_ranked_candidates(rankings, rrf_k=60)) == ["a", "b"]


def test_requirement_quota_reserves_late_candidates_and_keeps_fused_order():
    docs = [_doc("top-1"), _doc("top-2"), _doc("r1"), _doc("r2")]
    docs[2]["retrieval"] = {"matched_requirement_ids": ["req-1"]}
    docs[3]["retrieval"] = {"matched_requirement_ids": ["req-2"]}

    selected = select_rerank_candidates(
        docs,
        max_candidates=3,
        requirement_ids=("req-1", "req-2"),
    )

    assert _ids(selected) == ["top-1", "r1", "r2"]


def test_route_quota_reserves_a_candidate_for_each_recall_channel():
    docs = [_doc("top-vector"), _doc("second-vector"), _doc("late-lexical")]
    docs[0]["retrieval"] = {"matched_channels": ["rag_vector"]}
    docs[1]["retrieval"] = {"matched_channels": ["rag_vector"]}
    docs[2]["retrieval"] = {"matched_channels": ["rag_bm25"]}

    selected = select_rerank_candidates(
        docs,
        max_candidates=2,
        requirement_ids=(),
        per_channel=1,
    )

    assert _ids(selected) == ["top-vector", "late-lexical"]


def test_fusion_top_n_reserves_independent_route_before_truncation():
    fused = fuse_ranked_candidates(
        [
            RankedCandidateList(
                "query",
                "rag_vector",
                [_doc("shared"), _doc("vector-only")],
                weight=2.0,
            ),
            RankedCandidateList(
                "query",
                "derived_knowledge_lexical",
                [_doc("derived-only")],
                weight=0.1,
            ),
        ],
        rrf_k=60,
        top_n=2,
        per_channel_min=1,
    )

    assert _ids(fused) == ["shared", "derived-only"]


def test_fusion_and_quota_do_not_modify_inputs():
    rankings = [
        RankedCandidateList(
            "rewrite",
            "hybrid",
            [_doc("c1", score=0.4), _doc("c2")],
            requirement_ids=("r1",),
        )
    ]
    before = copy.deepcopy(rankings[0].docs)

    fused = fuse_ranked_candidates(rankings, rrf_k=60)
    select_rerank_candidates(fused, max_candidates=1, requirement_ids=("r1",))

    assert rankings[0].docs == before
    assert "query_fusion_score" not in rankings[0].docs[0]["retrieval"]
