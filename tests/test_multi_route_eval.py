import json
import sys

import pytest

from scripts import calibrate_multi_route_retrieval
from scripts import eval_multi_route_retrieval
from scripts.eval_multi_route_retrieval import _is_no_answer

from cogdoc.tools.eval.multi_route_calibration import (
    bootstrap_delta_intervals,
    bootstrap_metric_intervals,
    calibrate_abstention,
    calibrate_abstention_cross_validated,
    calibrate_fusion,
    calibrate_fusion_cross_validated,
    evaluate_config,
    evaluate_gate,
    stratified_holdout,
    stratified_kfold,
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


def _synthetic_multi_route_report():
    cases = []
    for position in range(20):
        no_answer = position >= 16
        hit = _hit("noise" if no_answer else "gold")
        hit["retrieval"] = {"distance": 1.5 if no_answer else 0.1}
        cases.append(
            {
                "case_id": f"case-{position}",
                "query_type": "no-answer" if no_answer else "single",
                "doc_type": "paper",
                "no_answer": no_answer,
                "expected_chunk_ids": [] if no_answer else ["gold"],
                "expected_sources": [],
                "gold_requirements": (
                    [] if no_answer else [{"acceptable_chunk_ids": ["gold"]}]
                ),
                "results": {
                    "all": {
                        "routes": {"rag_vector": [hit]},
                        "latency_ms": 10 + position,
                    }
                },
            }
        )
    return {
        "rrf_k": 60,
        "current_config": {
            "top_k": 1,
            "route_min_candidates": 0,
            "route_weights": {
                "rag_vector": 1.0,
                "rag_bm25": 0.0,
                "derived_knowledge_vector": 0.0,
                "derived_knowledge_lexical": 0.0,
            },
            "abstention_thresholds": {
                "vector_distance_max": 0.7,
                "bm25_score_min": 10.0,
                "knowledge_vector_score_min": 0.5,
                "knowledge_lexical_score_min": 0.5,
            },
        },
        "cases": cases,
    }


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


def test_source_level_ndcg_deduplicates_chunks_from_the_same_document():
    case = {"expected_chunk_ids": [], "expected_sources": ["paper.pdf"]}
    metrics = ranking_metrics(
        [_hit("a"), _hit("b"), _hit("c")],
        case,
        k=3,
    )
    assert metrics["recall@3"] == 1
    assert metrics["ndcg@3"] == 1


def test_multi_route_eval_infers_no_answer_from_empty_gold():
    assert _is_no_answer({"layer": "no-answer"}, [], []) is True
    assert _is_no_answer({"no_answer": False}, [], []) is False
    assert _is_no_answer({}, [], ["paper.pdf"]) is False


def test_multi_route_release_eval_rejects_stale_index(monkeypatch):
    monkeypatch.setattr(
        eval_multi_route_retrieval,
        "inspect_index_generation",
        lambda _storage_id: {
            "needs_migration": True,
            "reasons": ["chunk_identity_version_mismatch"],
        },
    )
    with pytest.raises(RuntimeError, match="migrate_v7_indexes.py run"):
        eval_multi_route_retrieval._require_current_index("kb")


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


def test_stratified_holdout_is_reproducible_and_keeps_train_examples():
    cases = [
        {
            "case_id": f"case-{position}",
            "query_type": "hard" if position < 5 else "single",
            "doc_type": "paper",
            "no_answer": position in {4, 9},
        }
        for position in range(10)
    ]
    first = stratified_holdout(cases, validation_fraction=0.25, seed="fixed")
    second = stratified_holdout(cases, validation_fraction=0.25, seed="fixed")
    assert first[2] == second[2]
    assert first[0]
    assert first[1]
    assert set(first[2]["train_case_ids"]).isdisjoint(
        first[2]["validation_case_ids"]
    )
    assert len(first[0]) + len(first[1]) == len(cases)


def test_stratified_kfold_assigns_each_case_once_and_balances_strata():
    cases = [
        {
            "case_id": f"case-{position}",
            "query_type": "hard" if position < 10 else "single",
            "doc_type": "paper",
            "no_answer": position % 5 == 0,
        }
        for position in range(20)
    ]
    folds, metadata = stratified_kfold(cases, folds=5, seed="fixed")
    repeated, repeated_metadata = stratified_kfold(cases, folds=5, seed="fixed")
    assert metadata == repeated_metadata
    assert [[case["case_id"] for case in fold] for fold in folds] == [
        [case["case_id"] for case in fold] for fold in repeated
    ]
    assigned = [case["case_id"] for fold in folds for case in fold]
    assert sorted(assigned) == sorted(case["case_id"] for case in cases)
    assert max(metadata["fold_sizes"]) - min(metadata["fold_sizes"]) <= 1


def test_cross_validated_calibration_reports_fold_robustness():
    current_thresholds = {
        "vector_distance_max": 0.7,
        "bm25_score_min": 10.0,
        "knowledge_vector_score_min": 0.5,
        "knowledge_lexical_score_min": 0.5,
    }
    cases = []
    for position in range(10):
        no_answer = position >= 8
        hit = _hit("x" if no_answer else "gold")
        hit["retrieval"] = {"distance": 1.5 if no_answer else 0.1}
        cases.append(
            {
                "case_id": f"case-{position}",
                "query_type": "no-answer" if no_answer else "single",
                "doc_type": "paper",
                "no_answer": no_answer,
                "expected_chunk_ids": [] if no_answer else ["gold"],
                "expected_sources": [],
                "gold_requirements": (
                    [] if no_answer else [{"acceptable_chunk_ids": ["gold"]}]
                ),
                "results": {"all": {"routes": {"rag_vector": [hit]}}},
            }
        )
    fusion = calibrate_fusion_cross_validated(
        cases,
        rrf_k=60,
        folds=2,
        seed="fixed",
        weight_grid=(0.0, 1.0),
        top_k_grid=(1,),
        route_min_grid=(0,),
    )
    abstention = calibrate_abstention_cross_validated(
        cases,
        current_thresholds,
        folds=2,
        seed="fixed",
    )
    assert fusion["metrics"]["recall"] == 1
    assert fusion["cross_validation"]["fold_count"] == 2
    assert len(fusion["folds"]) == 2
    assert abstention["accuracy"] == 1
    assert abstention["cross_validation"]["accuracy_worst_fold"] == 1


def test_config_evaluation_separates_ranking_and_abstention_denominators():
    gold = _hit("gold")
    gold["retrieval"] = {"distance": 0.1}
    irrelevant = _hit("x")
    irrelevant["retrieval"] = {"distance": 1.5}
    cases = [
        {
            "case_id": "answerable",
            "expected_chunk_ids": ["gold"],
            "expected_sources": [],
            "gold_requirements": [{"acceptable_chunk_ids": ["gold"]}],
            "no_answer": False,
            "results": {
                "all": {
                    "routes": {"rag_vector": [gold]},
                    "latency_ms": 10,
                }
            },
        },
        {
            "case_id": "no-answer",
            "expected_chunk_ids": [],
            "expected_sources": [],
            "gold_requirements": [],
            "no_answer": True,
            "results": {
                "all": {
                    "routes": {"rag_vector": [irrelevant]},
                    "latency_ms": 20,
                }
            },
        },
    ]
    config = {
        "top_k": 1,
        "route_min_candidates": 0,
        "route_weights": {"rag_vector": 1.0},
        "abstention_thresholds": {
            "vector_distance_max": 0.7,
            "bm25_score_min": 10.0,
            "knowledge_vector_score_min": 0.5,
            "knowledge_lexical_score_min": 0.5,
        },
    }
    result = evaluate_config(cases, config, rrf_k=60)
    assert result["overall"]["recall"] == 1
    assert result["overall"]["abstention_accuracy"] == 1
    assert result["overall"]["answerable_sample_count"] == 1
    assert result["overall"]["no_answer_sample_count"] == 1
    assert result["overall"]["latency_p95_ms"] == 20


def test_multi_route_gate_fails_closed_on_regression_and_missing_metric():
    result = evaluate_gate(
        {"recall": 0.89, "sample_count": 20},
        gate={
            "minimum": {"recall": 0.8, "mrr": 0.7},
            "maximum_regression": {"recall": 0.01},
            "minimum_samples": {"validation": 20},
        },
        current={"recall": 0.92},
    )
    assert result["passed"] is False
    failed = [check for check in result["checks"] if not check["passed"]]
    assert {check["metric"] for check in failed} == {"mrr", "recall"}


def test_bootstrap_intervals_are_deterministic_and_gate_uses_worst_bound():
    candidate_rows = [
        {
            "case_id": str(position),
            "metrics": {"recall": 1.0 if position else 0.0},
            "latency_ms": 10 + position,
        }
        for position in range(10)
    ]
    current_rows = [
        {
            "case_id": str(position),
            "metrics": {"recall": 1.0},
            "latency_ms": 10 + position,
        }
        for position in range(10)
    ]
    intervals = bootstrap_metric_intervals(
        candidate_rows, iterations=200, confidence_level=0.9, seed="fixed"
    )
    repeated = bootstrap_metric_intervals(
        candidate_rows, iterations=200, confidence_level=0.9, seed="fixed"
    )
    deltas = bootstrap_delta_intervals(
        candidate_rows,
        current_rows,
        iterations=200,
        confidence_level=0.9,
        seed="fixed",
    )
    assert intervals == repeated
    assert intervals["recall"]["lower"] < intervals["recall"]["estimate"]
    result = evaluate_gate(
        {"recall": 0.9},
        gate={
            "minimum": {"recall": 0.85},
            "maximum_regression": {"recall": 0.05},
            "confidence_bounds": {
                "minimum": ["recall"],
                "maximum_regression": ["recall"],
            },
        },
        current={"recall": 1.0},
        candidate_intervals=intervals,
        delta_intervals=deltas,
    )
    assert result["passed"] is False
    assert all(check["bound"] for check in result["checks"])


def test_slice_gate_checks_only_mature_slices():
    result = evaluate_gate(
        {"recall": 1.0},
        gate={
            "slice_maximum_regression": {"query_type": {"recall": 0.1}},
            "minimum_slice_samples": 4,
        },
        current={"recall": 1.0},
        candidate_slices={
            "query_type": {
                "hard": {"recall": 0.7, "sample_count": 4},
                "tiny": {"recall": 0.0, "sample_count": 1},
            }
        },
        current_slices={
            "query_type": {
                "hard": {"recall": 1.0, "sample_count": 4},
                "tiny": {"recall": 1.0, "sample_count": 1},
            }
        },
    )
    assert result["passed"] is False
    assert len(result["checks"]) == 1
    assert result["checks"][0]["slice_name"] == "hard"


def test_baseline_gate_bootstraps_once_then_enforces_accepted_metrics():
    gate = {"baseline_maximum_regression": {"recall": 0.01}}
    first = evaluate_gate(
        {"recall": 0.9}, gate=gate, current={"recall": 0.9}
    )
    regression = evaluate_gate(
        {"recall": 0.9},
        gate=gate,
        current={"recall": 0.9},
        baseline={"recall": 0.95},
    )
    required = evaluate_gate(
        {"recall": 0.9},
        gate={**gate, "require_baseline": True},
        current={"recall": 0.9},
    )
    assert first["passed"] is True
    assert regression["passed"] is False
    assert required["checks"][0]["kind"] == "baseline_required"


def test_cli_promotes_baseline_atomically_only_after_gate_passes(
    tmp_path, monkeypatch, capsys
):
    report_path = tmp_path / "report.json"
    gate_path = tmp_path / "gate.json"
    output_path = tmp_path / "calibration.json"
    baseline_path = tmp_path / "baseline.json"
    report_path.write_text(
        json.dumps(_synthetic_multi_route_report()), encoding="utf-8"
    )
    baseline_path.write_text('{"sentinel": true}', encoding="utf-8")
    common_args = [
        "calibrate_multi_route_retrieval.py",
        str(report_path),
        "--gate",
        str(gate_path),
        "--output",
        str(output_path),
        "--promote-baseline",
        str(baseline_path),
        "--inner-folds",
        "2",
        "--bootstrap-iterations",
        "100",
        "--summary",
    ]

    gate_path.write_text(json.dumps({"minimum": {"recall": 1.1}}), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", common_args)
    assert calibrate_multi_route_retrieval.main() == 1
    assert json.loads(baseline_path.read_text(encoding="utf-8")) == {
        "sentinel": True
    }
    assert json.loads(output_path.read_text(encoding="utf-8"))["promotion"][
        "status"
    ] == "rejected"

    gate_path.write_text(json.dumps({"minimum": {"recall": 0.9}}), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", common_args)
    assert calibrate_multi_route_retrieval.main() == 0
    promoted = json.loads(baseline_path.read_text(encoding="utf-8"))
    assert promoted["schema_version"] == "multi_route_baseline_v1"
    assert promoted["validation_metrics"]["recall"] == 1
    assert json.loads(output_path.read_text(encoding="utf-8"))["promotion"] == {
        "status": "eligible",
        "eligible": True,
        "failed_check_count": 0,
        "baseline_written": True,
        "baseline_path": str(baseline_path),
    }
    capsys.readouterr()
