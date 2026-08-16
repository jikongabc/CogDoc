import pytest

from cogdoc.tools.eval.multi_route_calibration import (
    calibrate_abstention,
    calibrate_fusion,
)
from cogdoc.tools.eval.multi_route_eval import (
    ablation_configs,
    layered_metrics,
    ranking_metrics,
    requirement_coverage,
    weighted_rrf,
)


def _hit(chunk_id, source="paper.pdf"):
    return {"chunk_id": chunk_id, "source": source, "retrieval": {}}


def test_ablation_matrix_has_full_only_and_leave_one_out_views():
    configs = ablation_configs()
    assert len(configs) == 9
    assert sum(configs["only:rag_bm25"].values()) == 1
    assert configs["without:rag_bm25"]["rag_bm25"] == 0


def test_ranking_and_requirement_metrics_use_chunk_gold():
    case = {"expected_chunk_ids": ["a", "b"], "expected_sources": []}
    metrics = ranking_metrics([_hit("x"), _hit("a"), _hit("b")], case, k=3)
    assert metrics["recall@3"] == 1
    assert metrics["mrr"] == 0.5
    assert metrics["ndcg@3"] > 0.6
    assert requirement_coverage(
        [_hit("a")],
        [
            {"acceptable_chunk_ids": ["a"]},
            {"acceptable_chunk_ids": ["b"]},
        ],
    ) == 0.5


def test_layered_metrics_preserve_three_slice_dimensions():
    rows = [
        {
            "query_type": "hard",
            "doc_type": "paper",
            "chunk_type": "table",
            "metrics": {"recall@3": 1.0},
            "latency_ms": 10,
        }
    ]
    slices = layered_metrics(rows)
    assert slices["query_type"]["hard"]["recall@3"] == 1
    assert slices["doc_type"]["paper"]["sample_count"] == 1
    assert slices["chunk_type"]["table"]["latency_p95_ms"] == 10


def test_fusion_calibration_returns_replayable_best_config():
    routes = {
        "rag_vector": [_hit("gold"), _hit("x")],
        "rag_bm25": [_hit("x"), _hit("gold")],
        "derived_knowledge_vector": [],
        "derived_knowledge_lexical": [],
    }
    cases = [
        {
            "expected_chunk_ids": ["gold"],
            "expected_sources": [],
            "gold_requirements": [{"acceptable_chunk_ids": ["gold"]}],
            "results": {"all": {"routes": routes}},
        }
    ]
    result = calibrate_fusion(
        cases,
        rrf_k=60,
        weight_grid=(0.0, 1.0),
        top_k_grid=(1,),
        route_min_grid=(0,),
    )
    replay = weighted_rrf(
        routes,
        result["route_weights"],
        rrf_k=60,
        top_k=result["top_k"],
        per_route_min=result["route_min_candidates"],
    )
    assert replay[0]["chunk_id"] == "gold"
    assert result["metrics"]["recall"] == 1


def test_abstention_calibration_separates_answerable_and_no_answer():
    supported_hit = _hit("a")
    supported_hit["retrieval"] = {"distance": 0.1}
    no_answer_hit = _hit("x")
    no_answer_hit["retrieval"] = {"distance": 1.5}
    cases = [
        {
            "no_answer": False,
            "results": {"all": {"routes": {"rag_vector": [supported_hit]}}},
        },
        {
            "no_answer": True,
            "results": {"all": {"routes": {"rag_vector": [no_answer_hit]}}},
        },
    ]
    current = {
        "vector_distance_max": 0.7,
        "bm25_score_min": 10.0,
        "knowledge_vector_score_min": 0.5,
        "knowledge_lexical_score_min": 0.5,
    }
    result = calibrate_abstention(cases, current)
    assert result["accuracy"] == pytest.approx(1.0)
