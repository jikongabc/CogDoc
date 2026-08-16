import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from cogdoc.api.ingest import KnowledgeBaseRegistry  # noqa: E402
from cogdoc.config.settings import get_settings  # noqa: E402
from cogdoc.service.kb_readers import kb_read_lease  # noqa: E402
from cogdoc.service.retrieval_diagnostics import run_retrieval_diagnostics  # noqa: E402
from cogdoc.service.retrieval_pipeline import build_retrieval_queries  # noqa: E402
from cogdoc.service.retriever_factory import RetrieverFactory  # noqa: E402
from cogdoc.state_runtime import default_state_runtime  # noqa: E402
from cogdoc.tools.eval.multi_route_eval import (  # noqa: E402
    ablation_configs,
    aggregate_case_metrics,
    layered_metrics,
    ranking_metrics,
    requirement_coverage,
)
from cogdoc.tools.retriever.scope import RetrievalScope  # noqa: E402


def _load(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _gold(case):
    requirements = case.get("gold_requirements") or []
    chunks = sorted(
        {
            str(value)
            for requirement in requirements
            for value in requirement.get("acceptable_chunk_ids", [])
        }
    )
    sources = sorted(
        {
            str(value)
            for requirement in requirements
            for value in requirement.get("acceptable_sources", [])
        }
    )
    if not sources:
        sources = [str(value) for value in case.get("expected_sources", [])]
    return chunks, sources, requirements


def _route_hits(result):
    routes = {}
    for ranking in result["routes"]:
        # Multiple rewritten queries contribute separate rankings to one route.
        routes.setdefault(ranking["channel"], []).extend(ranking["hits"])
    return routes


def main() -> int:
    settings = get_settings()
    parser = argparse.ArgumentParser(description="四路召回真实语料消融评测")
    parser.add_argument("--eval-set", type=Path, default=ROOT / settings.eval_set_path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--top-k", type=int, default=settings.qa_retrieval_top_k)
    parser.add_argument("--rerank", action="store_true")
    args = parser.parse_args()

    runtime = default_state_runtime()
    registry = KnowledgeBaseRegistry()
    configs = ablation_configs()
    observations = {name: [] for name in configs}
    raw_cases = []
    for position, case in enumerate(_load(args.eval_set), start=1):
        logical_id = str(case.get("doc_id") or case.get("kb_id") or "")
        record = registry.resolve(logical_id)
        storage_id = str((record or {}).get("storage_id") or logical_id)
        query = str(case.get("query") or "")
        chunks, sources, gold_requirements = _gold(case)
        requirements = case.get("evidence_requirements") or []
        query_plan = build_retrieval_queries(
            query, evidence_requirements=requirements, max_queries=max(1, len(requirements) * 2 + 1)
        )
        case_record = {
            "case_id": str(case.get("case_id") or case.get("id") or position),
            "query": query,
            "storage_id": storage_id,
            "expected_chunk_ids": chunks,
            "expected_sources": sources,
            "gold_requirements": gold_requirements,
            "no_answer": bool(case.get("no_answer")),
            "query_type": str(case.get("query_type") or case.get("layer") or "unknown"),
            "doc_type": str(case.get("doc_type") or "unknown"),
            "results": {},
        }
        for name, weights in configs.items():
            started = time.perf_counter()
            with kb_read_lease(storage_id):
                result = run_retrieval_diagnostics(
                    engine=RetrieverFactory.get_engine(storage_id),
                    derived_knowledge_retriever=runtime.derived_knowledge_retriever,
                    retrieval_feedback_store=None,
                    kb_id=storage_id,
                    query=query,
                    queries=query_plan,
                    top_k=args.top_k,
                    scope=RetrievalScope(include_derived_knowledge=True),
                    rerank=args.rerank,
                    rerank_top_n=args.top_k,
                    route_weights=weights,
                    route_min_candidates=1,
                    requirement_ids=[str(row.get("requirement_id") or "") for row in requirements],
                )
            hits = result["final"]
            metrics = ranking_metrics(hits, case_record, k=args.top_k)
            metrics["requirement_coverage"] = requirement_coverage(hits, gold_requirements)
            metrics["abstention_rate"] = float(not result["decision"]["supported"])
            expected_no_answer = bool(case.get("no_answer"))
            metrics["abstention_accuracy"] = float(
                expected_no_answer == (not result["decision"]["supported"])
            )
            chunk_type = str((hits[0] if hits else {}).get("chunk_type") or "none")
            row = {
                "case_id": case_record["case_id"],
                "query_type": case_record["query_type"],
                "doc_type": case_record["doc_type"],
                "chunk_type": chunk_type,
                "metrics": metrics,
                "latency_ms": (time.perf_counter() - started) * 1000,
            }
            observations[name].append(row)
            case_record["results"][name] = {
                "routes": _route_hits(result),
                "hits": hits,
                "decision": result["decision"],
                "latency_ms": row["latency_ms"],
            }
        raw_cases.append(case_record)
        print(f"{position}: {case_record['case_id']}", flush=True)

    report = {
        "schema_version": "v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "eval_set": str(args.eval_set),
        "top_k": args.top_k,
        "rerank": args.rerank,
        "rrf_k": settings.hybrid_rrf_k,
        "current_config": {
            "top_k": settings.qa_retrieval_top_k,
            "route_min_candidates": settings.qa_retrieval_docs_per_route,
            "route_weights": {
                "rag_vector": settings.qa_rag_vector_route_weight,
                "rag_bm25": settings.qa_rag_bm25_route_weight,
                "derived_knowledge_vector": settings.qa_derived_knowledge_vector_route_weight,
                "derived_knowledge_lexical": settings.qa_derived_knowledge_lexical_route_weight,
            },
            "abstention_thresholds": {
                "vector_distance_max": settings.qa_abstain_max_vector_distance,
                "bm25_score_min": settings.qa_abstain_min_bm25_score,
                "knowledge_vector_score_min": settings.qa_abstain_min_knowledge_vector_score,
                "knowledge_lexical_score_min": settings.qa_abstain_min_knowledge_lexical_score,
            },
        },
        "configs": configs,
        "summary": {
            name: {
                "overall": aggregate_case_metrics(rows),
                "slices": layered_metrics(rows),
            }
            for name, rows in observations.items()
        },
        "cases": raw_cases,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
