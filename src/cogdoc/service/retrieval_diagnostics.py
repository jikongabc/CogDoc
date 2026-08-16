from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from typing import Any

from cogdoc.config.settings import get_settings
from cogdoc.graph.state import RetrievedDoc
from cogdoc.service.retrieval_pipeline import (
    RetrievalQuery,
    retrieve_candidate_pool,
)
from cogdoc.tools.retriever.confidence import assess_retrieval_support
from cogdoc.tools.retriever.metadata import safe_retrieval_metadata
from cogdoc.tools.retriever.scope import RetrievalScope
from cogdoc.tools.reranker import rerank_with_device_policy


def _meta(doc: Mapping[str, Any]) -> Mapping[str, Any]:
    value = doc.get("meta")
    return value if isinstance(value, Mapping) else {}


def _chunk_id(doc: Mapping[str, Any]) -> str:
    return str(_meta(doc).get("chunk_id") or "")


def _preview(doc: Mapping[str, Any], limit: int = 360) -> str:
    return " ".join(str(doc.get("text") or "").split())[:limit]


def _public_doc(doc: Mapping[str, Any], *, rank: int) -> dict[str, Any]:
    meta = _meta(doc)
    retrieval = safe_retrieval_metadata(doc.get("retrieval"))
    return {
        "rank": rank,
        "chunk_id": str(meta.get("chunk_id") or ""),
        "parent_chunk_id": str(meta.get("parent_chunk_id") or ""),
        "source": str(meta.get("source") or ""),
        "source_sha256": str(meta.get("source_sha256") or ""),
        "source_type": str(meta.get("source_type") or "document"),
        "page_start": meta.get("page_start", meta.get("page")),
        "page_end": meta.get("page_end", meta.get("page")),
        "section_title": str(meta.get("section_title") or ""),
        "chunk_type": str(meta.get("chunk_type") or meta.get("block_kind") or ""),
        "text_preview": _preview(doc),
        "retrieval": retrieval,
    }


def run_retrieval_diagnostics(
    *,
    engine: Any,
    derived_knowledge_retriever: Any,
    retrieval_feedback_store: Any,
    kb_id: str,
    query: str,
    queries: Sequence[RetrievalQuery],
    top_k: int,
    scope: RetrievalScope,
    rerank: bool = True,
    rerank_top_n: int | None = None,
    route_weights: Mapping[str, float] | None = None,
    route_min_candidates: int = 1,
    requirement_ids: Sequence[str] = (),
) -> dict[str, Any]:
    settings = get_settings()
    started = time.perf_counter()
    retrieval = retrieve_candidate_pool(
        engine,
        derived_knowledge_retriever,
        retrieval_feedback_store,
        kb_id=kb_id,
        original_query=query,
        queries=queries,
        top_k=top_k,
        rrf_k=float(settings.hybrid_rrf_k),
        fusion_top_n=max(top_k, rerank_top_n or settings.qa_rerank_max_candidates),
        scope=scope,
        route_weights=route_weights,
        route_min_candidates=route_min_candidates,
    )
    retrieval_ms = (time.perf_counter() - started) * 1000
    fused_docs = list(retrieval.docs)
    pre_ranks = {_chunk_id(doc): rank for rank, doc in enumerate(fused_docs, start=1)}

    rerank_started = time.perf_counter()
    rerank_device = ""
    rerank_skipped_reason = "disabled"
    final_docs: list[RetrievedDoc] = fused_docs
    if rerank and fused_docs:
        execution = rerank_with_device_policy(
            query=query,
            docs=fused_docs[: settings.qa_rerank_max_candidates],
            top_n=rerank_top_n or settings.qa_rerank_top_n,
            allow_cpu=settings.qa_rerank_on_cpu,
        )
        final_docs = execution.docs
        rerank_device = execution.device
        rerank_skipped_reason = execution.skipped_reason
    rerank_ms = (time.perf_counter() - rerank_started) * 1000 if rerank else 0.0

    route_rows = []
    for ranking in retrieval.route_rankings:
        route_rows.append(
            {
                "query": ranking.query,
                "channel": ranking.channel,
                "weight": ranking.weight,
                "requirement_ids": list(ranking.requirement_ids),
                "is_original": ranking.is_original,
                "hits": [
                    _public_doc(doc, rank=rank)
                    for rank, doc in enumerate(ranking.docs, start=1)
                ],
            }
        )

    final_rows = []
    for rank, doc in enumerate(final_docs, start=1):
        row = _public_doc(doc, rank=rank)
        before = pre_ranks.get(row["chunk_id"])
        row["rank_before_rerank"] = before
        row["rank_delta"] = (before - rank) if before is not None else None
        final_rows.append(row)

    support = assess_retrieval_support(
        final_docs, settings, requirement_ids=requirement_ids
    )
    covered = {
        str(requirement_id)
        for doc in final_docs
        for requirement_id in (
            safe_retrieval_metadata(doc.get("retrieval")).get(
                "matched_requirement_ids", []
            )
            or []
        )
    }
    normalized_requirements = {
        str(requirement_id).strip()
        for requirement_id in requirement_ids
        if str(requirement_id).strip()
    }
    return {
        "query_plan": [
            {
                "query": item.text,
                "requirement_ids": list(item.requirement_ids),
                "is_original": item.is_original,
            }
            for item in retrieval.queries
        ],
        "routes": route_rows,
        "channel_counts": retrieval.channel_counts,
        "ranking_count": retrieval.ranking_count,
        "fused": [
            _public_doc(doc, rank=rank)
            for rank, doc in enumerate(fused_docs, start=1)
        ],
        "final": final_rows,
        "rerank": {
            "enabled": rerank,
            "device": rerank_device,
            "skipped_reason": rerank_skipped_reason,
        },
        "decision": {
            "supported": support.supported,
            "score": support.score,
            "reason": support.reason,
            "signals": support.signals,
            "missing_requirement_ids": sorted(normalized_requirements - covered),
        },
        "latency_ms": {
            "retrieval": round(retrieval_ms, 3),
            "rerank": round(rerank_ms, 3),
            "total": round(retrieval_ms + rerank_ms, 3),
        },
        "feedback_error": retrieval.feedback_error,
    }
