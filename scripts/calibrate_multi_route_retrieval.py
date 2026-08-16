import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from cogdoc.tools.eval.multi_route_calibration import (  # noqa: E402
    calibrate_abstention,
    calibrate_fusion,
)
from cogdoc.tools.eval.multi_route_eval import report_sha256  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="从四路消融报告自动校准召回参数")
    parser.add_argument("report", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = json.loads(args.report.read_text(encoding="utf-8"))
    current = report["current_config"]
    fusion = calibrate_fusion(report["cases"], rrf_k=float(report.get("rrf_k", 60)))
    abstention = calibrate_abstention(
        report["cases"], current["abstention_thresholds"]
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
    artifact = {
        "schema_version": "v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_report": str(args.report),
        "source_report_sha256": report_sha256(report),
        "current_config": current,
        "recommended_config": recommended,
        "recommended_env": env,
        "rollback_config": current,
        "calibration": {"fusion": fusion, "abstention": abstention},
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(artifact, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
