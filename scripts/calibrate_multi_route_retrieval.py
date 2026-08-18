import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from cogdoc.tools.eval.multi_route_calibration import (  # noqa: E402
    DEFAULT_BOOTSTRAP_ITERATIONS,
    DEFAULT_CONFIDENCE_LEVEL,
    DEFAULT_INNER_FOLDS,
    DEFAULT_SPLIT_SEED,
    bootstrap_delta_intervals,
    bootstrap_metric_intervals,
    calibrate_abstention_cross_validated,
    calibrate_fusion_cross_validated,
    evaluate_config,
    evaluate_gate,
    metric_comparison,
    stratified_holdout,
)
from cogdoc.tools.eval.multi_route_eval import report_sha256  # noqa: E402


def _baseline_metrics(value):
    if isinstance(value.get("validation_metrics"), dict):
        return value["validation_metrics"]
    validation = value.get("validation", {})
    recommended = validation.get("recommended", {})
    if isinstance(recommended.get("overall"), dict):
        return recommended["overall"]
    if isinstance(value.get("overall"), dict):
        return value["overall"]
    raise ValueError("baseline does not contain validation.recommended.overall")


def _atomic_write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="从四路消融报告自动校准召回参数")
    parser.add_argument("report", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--gate", type=Path)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--validation-fraction", type=float, default=0.2)
    parser.add_argument("--split-seed", default=DEFAULT_SPLIT_SEED)
    parser.add_argument("--inner-folds", type=int, default=DEFAULT_INNER_FOLDS)
    parser.add_argument("--robustness-penalty", type=float, default=0.25)
    parser.add_argument(
        "--bootstrap-iterations", type=int, default=DEFAULT_BOOTSTRAP_ITERATIONS
    )
    parser.add_argument(
        "--confidence-level", type=float, default=DEFAULT_CONFIDENCE_LEVEL
    )
    parser.add_argument("--promote-baseline", type=Path)
    parser.add_argument("--summary", action="store_true")
    args = parser.parse_args()
    if args.promote_baseline and not args.gate:
        parser.error("--promote-baseline requires --gate")
    report = json.loads(args.report.read_text(encoding="utf-8"))
    current = report["current_config"]
    train, validation, split = stratified_holdout(
        report["cases"],
        validation_fraction=args.validation_fraction,
        seed=args.split_seed,
    )
    rrf_k = float(report.get("rrf_k", 60))
    inner_seed = f"{args.split_seed}:inner"
    fusion = calibrate_fusion_cross_validated(
        train,
        rrf_k=rrf_k,
        folds=args.inner_folds,
        seed=inner_seed,
        robustness_penalty=args.robustness_penalty,
    )
    abstention = calibrate_abstention_cross_validated(
        train,
        current["abstention_thresholds"],
        folds=args.inner_folds,
        seed=inner_seed,
    )
    recommended = {
        "top_k": fusion["top_k"],
        "route_min_candidates": fusion["route_min_candidates"],
        "route_weights": fusion["route_weights"],
        "abstention_thresholds": abstention["thresholds"],
    }
    env = {
        "QA_RETRIEVAL_TOP_K": recommended["top_k"],
        "QA_RAG_VECTOR_ROUTE_WEIGHT": recommended["route_weights"]["rag_vector"],
        "QA_RAG_BM25_ROUTE_WEIGHT": recommended["route_weights"]["rag_bm25"],
        "QA_DERIVED_KNOWLEDGE_VECTOR_ROUTE_WEIGHT": recommended["route_weights"]["derived_knowledge_vector"],
        "QA_DERIVED_KNOWLEDGE_LEXICAL_ROUTE_WEIGHT": recommended["route_weights"]["derived_knowledge_lexical"],
        "QA_RETRIEVAL_DOCS_PER_ROUTE": recommended["route_min_candidates"],
        "QA_ABSTAIN_MAX_VECTOR_DISTANCE": recommended["abstention_thresholds"]["vector_distance_max"],
        "QA_ABSTAIN_MIN_BM25_SCORE": recommended["abstention_thresholds"]["bm25_score_min"],
        "QA_ABSTAIN_MIN_KNOWLEDGE_VECTOR_SCORE": recommended["abstention_thresholds"]["knowledge_vector_score_min"],
        "QA_ABSTAIN_MIN_KNOWLEDGE_LEXICAL_SCORE": recommended["abstention_thresholds"]["knowledge_lexical_score_min"],
    }
    current_validation = evaluate_config(validation, current, rrf_k=rrf_k)
    recommended_validation = evaluate_config(validation, recommended, rrf_k=rrf_k)
    candidate_intervals = bootstrap_metric_intervals(
        recommended_validation["rows"],
        iterations=args.bootstrap_iterations,
        confidence_level=args.confidence_level,
        seed=f"{args.split_seed}:candidate",
    )
    delta_intervals = bootstrap_delta_intervals(
        recommended_validation["rows"],
        current_validation["rows"],
        iterations=args.bootstrap_iterations,
        confidence_level=args.confidence_level,
        seed=f"{args.split_seed}:delta",
    )
    baseline_artifact = None
    baseline_metrics = None
    if args.baseline:
        baseline_artifact = json.loads(args.baseline.read_text(encoding="utf-8"))
        baseline_split = baseline_artifact.get("split", {}).get("validation_case_ids")
        if baseline_split and baseline_split != split["validation_case_ids"]:
            raise ValueError("baseline validation split does not match the current split")
        baseline_metrics = _baseline_metrics(baseline_artifact)
    gate_result = None
    if args.gate:
        gate_config = json.loads(args.gate.read_text(encoding="utf-8"))
        gate_result = evaluate_gate(
            recommended_validation["overall"],
            gate=gate_config,
            current=current_validation["overall"],
            baseline=baseline_metrics,
            candidate_intervals=candidate_intervals,
            delta_intervals=delta_intervals,
            candidate_slices=recommended_validation["slices"],
            current_slices=current_validation["slices"],
        )
    promotion_status = (
        "not_evaluated"
        if gate_result is None
        else "eligible"
        if gate_result["passed"]
        else "rejected"
    )
    promotion = {
        "status": promotion_status,
        "eligible": gate_result["passed"] if gate_result is not None else None,
        "failed_check_count": sum(
            not check["passed"] for check in (gate_result or {}).get("checks", [])
        ),
        "baseline_written": False,
        "baseline_path": str(args.promote_baseline) if args.promote_baseline else None,
    }
    artifact = {
        "schema_version": "v3",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_report": str(args.report),
        "source_report_sha256": report_sha256(report),
        "current_config": current,
        "recommended_config": recommended,
        "recommended_env": env,
        "rollback_config": current,
        "calibration": {
            "strategy": "outer_holdout_inner_stratified_kfold_v1",
            "inner_folds": args.inner_folds,
            "fusion": fusion,
            "abstention": abstention,
        },
        "split": split,
        "validation": {
            "current": current_validation,
            "recommended": recommended_validation,
            "comparison_to_current": metric_comparison(
                recommended_validation["overall"], current_validation["overall"]
            ),
            "baseline": baseline_metrics,
            "comparison_to_baseline": metric_comparison(
                recommended_validation["overall"], baseline_metrics
            )
            if baseline_metrics
            else None,
            "confidence": {
                "method": "deterministic_percentile_bootstrap",
                "level": args.confidence_level,
                "iterations": args.bootstrap_iterations,
                "candidate": candidate_intervals,
                "candidate_minus_current": delta_intervals,
            },
        },
        "gate": gate_result,
        "promotion": promotion,
    }
    if args.promote_baseline and promotion_status == "eligible":
        promoted = {
            "schema_version": "multi_route_baseline_v1",
            "created_at": artifact["created_at"],
            "source_report_sha256": artifact["source_report_sha256"],
            "source_calibration_schema": artifact["schema_version"],
            "split": {
                "seed": split["seed"],
                "validation_fraction": split["validation_fraction"],
                "validation_case_ids": split["validation_case_ids"],
            },
            "accepted_config": recommended,
            "accepted_env": env,
            "validation_metrics": recommended_validation["overall"],
            "confidence": artifact["validation"]["confidence"],
            "gate": gate_result,
        }
        _atomic_write_json(args.promote_baseline, promoted)
        promotion["baseline_written"] = True
    _atomic_write_json(args.output, artifact)
    if args.summary:
        print(
            json.dumps(
                {
                    "schema_version": artifact["schema_version"],
                    "source_report_sha256": artifact["source_report_sha256"],
                    "recommended_config": recommended,
                    "validation": recommended_validation["overall"],
                    "comparison_to_current": artifact["validation"][
                        "comparison_to_current"
                    ],
                    "gate": gate_result,
                    "promotion": promotion,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(json.dumps(artifact, ensure_ascii=False, indent=2))
    return 0 if gate_result is None or gate_result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
