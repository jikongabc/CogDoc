import argparse
import copy
import json
import math
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    # 包源码在源码目录下，项目根目录用于解析数据文件相对路径。
    sys.path.insert(0, str(ROOT / "src"))

from cogdoc.config.settings import get_settings  # noqa: E402
from cogdoc.graph.state import RetrievedDoc  # noqa: E402
from cogdoc.tools.eval.retrieval_metrics import (  # noqa: E402
    aggregate,
    annotation_coverage_stats,
    audit_coverage,
    coverage_minimums,
    evidence_metric_minimums,
    evidence_metric_sample_kind,
    evaluate_evidence_unit_outcomes,
    evaluate_query,
    evaluate_requirement_coverage,
    evaluate_thresholds,
    infer_retrieval_layer,
    metric_direction,
    percentile,
    requirement_coverage_rate,
)


# 返回项目根目录路径。
def _project_path(path: str) -> Path:
    resolved = Path(path)
    return resolved if resolved.is_absolute() else ROOT / resolved


_settings = get_settings()
DEFAULT_EVAL_SET = _project_path(_settings.eval_set_path)
# 真实评测集不入库，干净检出时回退到示例评测集。
EXAMPLE_EVAL_SET = _project_path(_settings.eval_example_set_path)
DEFAULT_K_VALUES = [1, 3, 5, 9]
DIAGNOSTIC_METRICS = {
    "adaptive_retry_trigger_rate",
    "retrieval_query_count",
    "parent_context_trigger_rate",
    "parent_context_expanded_count",
    "neighbor_context_expanded_count",
    "requirement_coverage_abstention_rate",
}
DIAGNOSTIC_METRIC_PREFIXES = (
    "evidence_pack_",
    "evidence_span_",
    "no_evidence_unit_",
)

EVIDENCE_SPAN_COUNT_METRICS = (
    "evidence_span_input_count",
    "evidence_span_output_count",
    "evidence_span_compressed_count",
    "evidence_span_fallback_count",
    "evidence_span_input_chars",
    "evidence_span_selected_chars",
)


def _nonnegative_int_or_none(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        normalized = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return normalized if normalized >= 0 else None


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _nonnegative_int(value: Any) -> int:
    normalized = _nonnegative_int_or_none(value)
    return normalized if normalized is not None else 0


def _safe_context_item(doc: Mapping[str, Any]) -> dict[str, Any]:
    """Serialize only evidence identity and source offsets, never private text."""

    meta = _mapping(doc.get("meta"))
    retrieval = _mapping(doc.get("retrieval"))
    item: dict[str, Any] = {
        "chunk_id": str(meta.get("chunk_id") or ""),
        "parent_chunk_id": str(meta.get("parent_chunk_id") or ""),
        "source": str(meta.get("source") or ""),
        "section_path": str(meta.get("section_path") or ""),
        "context_expansion": str(retrieval.get("context_expansion") or "anchor"),
    }
    for field in (
        "evidence_span_input_start",
        "evidence_span_input_end",
        "evidence_span_start",
        "evidence_span_end",
        "evidence_text_start",
        "evidence_text_end",
        "evidence_span_original_chars",
        "evidence_span_selected_chars",
    ):
        normalized = _nonnegative_int_or_none(retrieval.get(field))
        if normalized is not None:
            item[field] = normalized
    if "evidence_span_input_start" not in item and "evidence_text_start" in item:
        item["evidence_span_input_start"] = item["evidence_text_start"]
    if "evidence_span_input_end" not in item and "evidence_text_end" in item:
        item["evidence_span_input_end"] = item["evidence_text_end"]
    if "evidence_span_selected" in retrieval:
        item["evidence_span_selected"] = bool(retrieval.get("evidence_span_selected"))
    if retrieval.get("evidence_span_reason"):
        item["evidence_span_reason"] = str(retrieval.get("evidence_span_reason"))[:80]
    raw_requirement_ids = retrieval.get("evidence_span_matched_requirement_ids")
    if isinstance(raw_requirement_ids, Sequence) and not isinstance(
        raw_requirement_ids, (str, bytes)
    ):
        item["evidence_span_matched_requirement_ids"] = [
            str(requirement_id)
            for requirement_id in raw_requirement_ids[:3]
            if str(requirement_id)
        ]
    return item


def _acceptable_gold_spans(
    requirement: Mapping[str, Any],
) -> tuple[tuple[str, int, int], ...]:
    if not str(requirement.get("requirement_id") or "").strip():
        return ()
    raw_spans = requirement.get("acceptable_spans")
    if not isinstance(raw_spans, Sequence) or isinstance(raw_spans, (str, bytes)):
        return ()
    spans: list[tuple[str, int, int]] = []
    for raw_span in raw_spans:
        if not isinstance(raw_span, Mapping):
            continue
        chunk_id = str(raw_span.get("chunk_id") or "").strip()
        start = _nonnegative_int_or_none(raw_span.get("start"))
        end = _nonnegative_int_or_none(raw_span.get("end"))
        if chunk_id and start is not None and end is not None and end > start:
            spans.append((chunk_id, start, end))
    return tuple(spans)


def _annotated_span_requirement_count(
    requirements: Sequence[Mapping[str, Any]],
) -> int:
    return sum(
        bool(_acceptable_gold_spans(requirement)) for requirement in requirements
    )


def evidence_span_gold_recall(
    context_items: Sequence[Mapping[str, Any]],
    gold_requirements: Sequence[Mapping[str, Any]],
    *,
    start_key: str,
    end_key: str,
) -> float | None:
    """Mean best character recall over atomic requirements with span gold.

    ``acceptable_spans`` are alternatives for one atomic requirement.  Offsets
    are zero-based, half-open positions in the canonical child text.  Invalid
    optional annotations are ignored, and an entirely unannotated row produces
    no metric instead of a misleading zero.
    """

    annotated = [
        spans
        for requirement in gold_requirements
        if (spans := _acceptable_gold_spans(requirement))
    ]
    if not annotated:
        return None

    actual_by_chunk: dict[str, list[tuple[int, int]]] = {}
    for item in context_items:
        if not isinstance(item, Mapping):
            continue
        chunk_id = str(item.get("chunk_id") or "").strip()
        start = _nonnegative_int_or_none(item.get(start_key))
        end = _nonnegative_int_or_none(item.get(end_key))
        if chunk_id and start is not None and end is not None and end > start:
            actual_by_chunk.setdefault(chunk_id, []).append((start, end))

    requirement_scores: list[float] = []
    for alternatives in annotated:
        best = 0.0
        for chunk_id, gold_start, gold_end in alternatives:
            gold_chars = gold_end - gold_start
            for actual_start, actual_end in actual_by_chunk.get(chunk_id, []):
                overlap = max(
                    0,
                    min(gold_end, actual_end) - max(gold_start, actual_start),
                )
                best = max(best, overlap / gold_chars)
        requirement_scores.append(best)
    return statistics.mean(requirement_scores)


def _is_diagnostic_metric(metric: str) -> bool:
    return metric in DIAGNOSTIC_METRICS or metric.startswith(DIAGNOSTIC_METRIC_PREFIXES)


# 解析默认评测集。
def resolve_default_eval_set() -> Path:
    if DEFAULT_EVAL_SET.exists():
        return DEFAULT_EVAL_SET
    print(
        f"未找到本地评测集 {DEFAULT_EVAL_SET}，回退到示例 {EXAMPLE_EVAL_SET.name}。\n"
        f"提示：复制为 {DEFAULT_EVAL_SET.name} 并按你的真实语料填写后再跑，结果才有意义。"
    )
    return EXAMPLE_EVAL_SET


# 加载评测集。
def load_eval_set(path: Path) -> List[dict]:
    items = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            items.append(json.loads(line))
    return items


# 检索来源。
def retrieve_sources(query: str, doc_id: str, top_k: int, rerank: bool) -> List[str]:
    return retrieve_result(query, doc_id, top_k, rerank)["sources"]


# 执行检索并用线上同一套规则判断是否有足够证据。
def retrieve_result(
    query: str,
    doc_id: str,
    top_k: int,
    rerank: bool,
    *,
    verify_evidence: bool = False,
    is_local_verifier: bool = False,
    rewritten_queries: List[str] | None = None,
    evidence_requirements: List[dict] | None = None,
) -> dict:
    from cogdoc.graph.subgraphs.qa import (
        RetrieverFactory,
        _build_qa_evidence_pack,
        _carry_verified_docs,
        _evidence_pack_metrics,
        _expand_with_neighbor_chunks,
        _verification_candidates_with_context,
        _verification_docs_from_pack,
    )
    from cogdoc.service.kb_readers import kb_read_lease
    from cogdoc.service.retrieval_pipeline import (
        build_retrieval_queries,
        retrieve_candidate_pool,
    )
    from cogdoc.state_runtime import default_state_runtime
    from cogdoc.tools.retriever.confidence import assess_retrieval_support
    from cogdoc.tools.retriever.fusion import select_rerank_candidates

    settings = get_settings()
    runtime = default_state_runtime()
    rewritten_queries = list(rewritten_queries or [])
    evidence_requirements = list(evidence_requirements or [])[:3]
    requirement_ids = [
        str(item.get("requirement_id") or "")
        for item in evidence_requirements
        if isinstance(item, dict) and item.get("requirement_id")
    ]
    retry_count = 0
    prioritized_requirement_ids: List[str] = []
    pinned_ids: set[str] = set()
    verified_docs: Dict[str, RetrievedDoc] = {}
    initial_supported: bool | None = None
    verification: dict = {}
    verification_required = False
    total_query_count = 0
    total_ranking_count = 0
    total_channel_counts: Dict[str, int] = {}
    retrieval_feedback_error = ""
    retrieval_carryover_count = 0
    generation_docs: List[RetrievedDoc] = []
    verification_docs: List[RetrievedDoc] = []
    pre_pack_docs: List[RetrievedDoc] = []
    packed_docs: List[RetrievedDoc] = []
    rerank_devices: set[str] = set()
    rerank_skip_reasons: set[str] = set()
    span_metrics: dict[str, Any] = {
        "evidence_span_input_count": 0,
        "evidence_span_output_count": 0,
        "evidence_span_compressed_count": 0,
        "evidence_span_fallback_count": 0,
        "evidence_span_input_chars": 0,
        "evidence_span_selected_chars": 0,
        "evidence_span_reason_counts": {},
    }
    pack_metrics: dict = {
        "evidence_pack_input_count": 0,
        "evidence_pack_kept_count": 0,
        "evidence_pack_dropped_count": 0,
        "evidence_pack_input_chars": 0,
        "evidence_pack_kept_chars": 0,
        "evidence_pack_overlap_removed_chars": 0,
        "evidence_pack_drop_reason_counts": {},
        "evidence_pack_anchor_count": 0,
        "evidence_pack_pinned_count": 0,
        "evidence_pack_over_budget": False,
    }

    while True:
        round_top_k = top_k
        if retry_count:
            round_top_k = min(
                int(
                    math.ceil(
                        top_k
                        * (settings.qa_adaptive_retrieval_top_k_multiplier**retry_count)
                    )
                ),
                max(top_k, settings.qa_adaptive_retrieval_max_top_k),
            )
        queries = build_retrieval_queries(
            query,
            rewritten_queries=rewritten_queries,
            evidence_requirements=evidence_requirements,
            prioritized_requirement_ids=prioritized_requirement_ids,
            max_queries=settings.qa_retrieval_max_queries,
        )
        with kb_read_lease(doc_id):
            engine = RetrieverFactory.get_engine(doc_id)
            pipeline_result = retrieve_candidate_pool(
                engine,
                runtime.derived_knowledge_retriever,
                runtime.retrieval_feedback_store,
                kb_id=doc_id,
                original_query=query,
                queries=queries,
                top_k=round_top_k,
                rrf_k=float(settings.hybrid_rrf_k),
                retrieval_round=retry_count,
            )
        total_query_count += len(pipeline_result.queries)
        total_ranking_count += pipeline_result.ranking_count
        for channel, count in pipeline_result.channel_counts.items():
            total_channel_counts[channel] = total_channel_counts.get(channel, 0) + count
        if pipeline_result.feedback_error and not retrieval_feedback_error:
            retrieval_feedback_error = pipeline_result.feedback_error
        current_docs = pipeline_result.docs
        ranked_docs, retrieval_carryover_count = _carry_verified_docs(
            {
                "messages": [],
                "retrieval_retry_count": retry_count,
                "evidence_verified_chunk_ids": list(pinned_ids),
                "evidence_requirement_assessments": list(
                    verification.get("evidence_requirement_assessments") or []
                ),
                "verification_docs": list(verified_docs.values()),
            },
            current_docs,
        )
        from cogdoc.tools.reranker import (
            dynamic_rerank_top_n,
            requirement_query_map,
            rerank_with_requirement_policy,
        )

        if rerank and ranked_docs:

            max_candidates = max(
                settings.qa_rerank_max_candidates,
                settings.qa_rerank_top_n,
            )
            candidates = (
                select_rerank_candidates(
                    ranked_docs,
                    max_candidates=max_candidates,
                    requirement_ids=requirement_ids,
                )
                if max_candidates > 0
                else ranked_docs
            )
            rerank_execution = rerank_with_requirement_policy(
                query=query,
                docs=candidates,
                requirement_queries=requirement_query_map(evidence_requirements),
                top_n=len(candidates),
                allow_cpu=bool(getattr(settings, "qa_rerank_on_cpu", False)),
                per_requirement=int(
                    getattr(settings, "qa_rerank_docs_per_requirement", 2)
                ),
            )
            ranked_docs = rerank_execution.docs
            rerank_devices.add(rerank_execution.device)
            if rerank_execution.skipped_reason:
                rerank_skip_reasons.add(rerank_execution.skipped_reason)
        # 完整排名用于 recall@n；放行判断严格复用线上 generation top-n 预算。
        anchor_top_n = dynamic_rerank_top_n(
            base_top_n=max(0, int(settings.qa_rerank_top_n)),
            max_docs=settings.qa_evidence_pack_max_docs,
            requirement_count=len(requirement_ids),
            docs_per_requirement=int(
                getattr(settings, "qa_rerank_docs_per_requirement", 2)
            ),
        )
        decision_docs = ranked_docs[:anchor_top_n]
        support = assess_retrieval_support(
            decision_docs, settings, requirement_ids=requirement_ids
        )
        generation_docs = decision_docs
        if decision_docs and hasattr(engine, "load_source_chunks"):
            # Hydrate before verifier exactly as the online QA graph does.  Rank
            # metrics still use the unexpanded anchors above.
            generation_docs = _expand_with_neighbor_chunks(doc_id, decision_docs)
        pre_pack_docs = _verification_candidates_with_context(
            decision_docs,
            generation_docs,
            ranked_docs,
        )
        evidence_pack, span_metrics = _build_qa_evidence_pack(
            query=query,
            evidence_requirements=[
                requirement
                for requirement in evidence_requirements
                if isinstance(requirement, Mapping)
            ],
            anchors=decision_docs,
            expanded_docs=generation_docs,
            ranked_candidates=ranked_docs,
            pinned_chunk_ids=pinned_ids,
            requirement_ids=requirement_ids,
            settings=settings,
        )
        packed_docs = list(evidence_pack.kept_docs)
        generation_docs = packed_docs
        pack_metrics = _evidence_pack_metrics(
            evidence_pack,
            pinned_chunk_ids=pinned_ids,
        )
        if initial_supported is None:
            initial_supported = support.supported

        # Anchor/pinned evidence is a hard constraint.  When it alone exceeds
        # the global pack budget, retrying cannot repair the configured limit;
        # fail closed before invoking the model verifier.
        if evidence_pack.over_budget_hard_constraints:
            verification_required = False
            verification = {}
            verification_docs = []
            prioritized_requirement_ids = []
            break

        if verify_evidence:
            # 每轮结论只描述当前候选集；定向重试 ID 和 pinned chunk 单独保留。
            verification_required = False
            verification = {}
            from cogdoc.agents.evidence_verifier import (
                EvidenceVerifierAgent,
                should_verify_evidence,
            )

            verification_docs = _verification_docs_from_pack(
                evidence_pack,
                max_docs=settings.qa_evidence_verify_max_docs,
                requirement_ids=requirement_ids,
                pinned_chunk_ids=pinned_ids,
            )
            verify_state = {
                "query": query,
                "is_local": is_local_verifier,
                "rewritten_queries": rewritten_queries,
                "evidence_requirements": evidence_requirements,
                "retrieval_first_stage_supported": support.supported,
                "retrieval_abstained": not support.supported,
                "retrieval_abstain_reason": support.reason,
                "retrieval_confidence": support.score,
                "verification_docs": verification_docs,
            }
            if should_verify_evidence(verify_state, settings):
                verification_required = True
                verification = EvidenceVerifierAgent.verify(verify_state)
                round_verified_ids = {
                    str(chunk_id)
                    for chunk_id in verification.get("evidence_verified_chunk_ids", [])
                    if str(chunk_id)
                }
                round_verified_docs: Dict[str, RetrievedDoc] = {}
                for verified_doc in verification_docs:
                    chunk_id = str(verified_doc.get("meta", {}).get("chunk_id") or "")
                    if chunk_id in round_verified_ids:
                        round_verified_docs[chunk_id] = copy.deepcopy(verified_doc)
                pinned_ids = round_verified_ids
                verified_docs = round_verified_docs
                if verification.get("evidence_supported"):
                    break
                prioritized_requirement_ids = list(
                    verification.get("missing_evidence_requirement_ids") or []
                )
                # 与线上 retry node 一致：多需求失败但没有明确缺口时，
                # 定向重试全部需求，而不是静默结束自适应检索。
                if not prioritized_requirement_ids and len(requirement_ids) > 1:
                    prioritized_requirement_ids = list(requirement_ids)
                if verification.get("evidence_verifier_error"):
                    break
            elif support.supported:
                break
            else:
                # 线上终轮未进入 verifier 时清空旧 verified 结论；若仍有下一轮，
                # 也不能继续携带已不属于当前闭集结论的快照。
                if retry_count:
                    pinned_ids.clear()
                    verified_docs.clear()
                if not prioritized_requirement_ids:
                    if len(requirement_ids) <= 1:
                        break
                    prioritized_requirement_ids = list(requirement_ids)
        else:
            # 关闭模型校验只关闭 LLM gate；线上对多需求首阶段失败仍会补检索。
            if support.supported:
                break
            if not prioritized_requirement_ids:
                if len(requirement_ids) <= 1:
                    break
                prioritized_requirement_ids = list(requirement_ids)

        if (
            not settings.qa_adaptive_retrieval_enabled
            or retry_count >= settings.qa_adaptive_retrieval_max_retries
            or not prioritized_requirement_ids
        ):
            break
        retry_count += 1

    pack_over_budget = bool(pack_metrics["evidence_pack_over_budget"])
    supported = False
    if not pack_over_budget:
        supported = (
            bool(verification.get("evidence_supported"))
            if verification_required
            else support.supported
        )
    missing_requirement_ids = list(
        verification.get("missing_evidence_requirement_ids") or []
    )
    if retry_count > 0 and not supported and not missing_requirement_ids:
        missing_requirement_ids = list(prioritized_requirement_ids)
    # Hydration diagnostics describe the final pre-pack candidate set even when
    # the request later abstains and generation_context_items is intentionally empty.
    parent_context_expanded_count = sum(
        doc.get("retrieval", {}).get("context_expansion") == "section"
        for doc in pre_pack_docs
    )
    neighbor_context_expanded_count = sum(
        doc.get("retrieval", {}).get("context_expansion") == "neighbor"
        for doc in pre_pack_docs
    )
    if not supported:
        # Online abstention clears reranked_docs and never enters generation.
        generation_docs = []
    result = {
        "sources": [
            str(doc.get("meta", {}).get("source") or "") for doc in ranked_docs
        ],
        "items": [
            {
                "chunk_id": str(doc.get("meta", {}).get("chunk_id") or ""),
                "source": str(doc.get("meta", {}).get("source") or ""),
                "matched_unit_ids": list(
                    doc.get("retrieval", {}).get("matched_unit_ids")
                    or doc.get("retrieval", {}).get("matched_requirement_ids")
                    or []
                ),
            }
            for doc in ranked_docs
        ],
        "generation_context_items": [
            _safe_context_item(doc) for doc in generation_docs
        ],
        "evidence_pack_input_items": [
            {
                "chunk_id": str(doc.get("meta", {}).get("chunk_id") or ""),
                "source": str(doc.get("meta", {}).get("source") or ""),
            }
            for doc in pre_pack_docs
        ],
        "evidence_pack_context_items": [_safe_context_item(doc) for doc in packed_docs],
        "supported": supported,
        "first_stage_supported": bool(initial_supported),
        "confidence": support.score,
        "reason": (
            "evidence_pack_hard_budget_exceeded"
            if pack_over_budget
            else str(verification.get("retrieval_abstain_reason") or support.reason)
        ),
        "signals": support.signals,
        "evidence_verification_required": verification_required,
        "evidence_supported": supported,
        "evidence_verification_reason": str(
            "evidence_pack_hard_budget_exceeded"
            if pack_over_budget
            else verification.get("evidence_verification_reason")
            or ("not_required" if verify_evidence else "not_requested")
        ),
        "evidence_verified_chunk_ids": list(
            verification.get("evidence_verified_chunk_ids") or []
        ),
        "evidence_requirement_assessments": list(
            verification.get("evidence_requirement_assessments") or []
        ),
        "missing_evidence_requirement_ids": missing_requirement_ids,
        "evidence_verifier_error": str(
            verification.get("evidence_verifier_error") or ""
        ),
        "retrieval_retry_count": retry_count,
        "adaptive_retrieval_rescued": bool(retry_count and supported),
        # 自适应评测按完整请求累计成本，不能只报告末轮结果。
        "retrieval_query_count": total_query_count,
        "retrieval_ranking_count": total_ranking_count,
        "retrieval_channel_counts": total_channel_counts,
        "retrieval_carryover_count": retrieval_carryover_count,
        "parent_context_expanded_count": parent_context_expanded_count,
        "neighbor_context_expanded_count": neighbor_context_expanded_count,
        "retrieval_feedback_error": retrieval_feedback_error,
        "rerank_devices": sorted(rerank_devices),
        "rerank_skip_reasons": sorted(rerank_skip_reasons),
        **span_metrics,
        **pack_metrics,
    }
    return result


# 运行评测。
def run_eval(
    items: List[dict],
    k_values: List[int],
    rerank: bool,
    verify_evidence: bool = False,
    is_local_verifier: bool = False,
    evidence_metric_minimum_samples: Mapping[str, Any] | None = None,
) -> dict:
    top_k = max(k_values)
    rows: List[dict] = []
    settings = get_settings()

    # 模型加载、设备选择和首轮内核初始化单独计时，不污染稳态请求 P95。
    warmup_item = items[0]
    if verify_evidence:
        from cogdoc.agents.evidence_verifier import requires_evidence_verification

        warmup_item = next(
            (
                item
                for item in items
                if requires_evidence_verification(str(item.get("query") or ""))
            ),
            items[0],
        )
    warmup_started = time.perf_counter()
    retrieve_result(
        warmup_item["query"],
        warmup_item.get("doc_id", "default"),
        top_k,
        rerank,
        verify_evidence=verify_evidence,
        is_local_verifier=is_local_verifier,
        rewritten_queries=warmup_item.get("rewritten_queries", []),
        evidence_requirements=warmup_item.get("evidence_requirements", []),
    )
    warmup_latency_ms = (time.perf_counter() - warmup_started) * 1000.0

    for item in items:
        started = time.perf_counter()
        retrieval_result = retrieve_result(
            item["query"],
            item.get("doc_id", "default"),
            top_k,
            rerank,
            verify_evidence=verify_evidence,
            is_local_verifier=is_local_verifier,
            rewritten_queries=item.get("rewritten_queries", []),
            evidence_requirements=item.get("evidence_requirements", []),
        )
        retrieved = retrieval_result["sources"]
        latency_ms = (time.perf_counter() - started) * 1000.0
        metrics = evaluate_query(retrieved, item["expected_sources"], k_values)
        metrics.update(
            evaluate_requirement_coverage(
                retrieval_result.get("items")
                or [{"source": source} for source in retrieved],
                item.get("gold_requirements", []),
                k_values,
                hard_negative_chunk_ids=item.get("hard_negative_chunk_ids", []),
            )
        )
        metrics.update(
            evaluate_evidence_unit_outcomes(
                retrieval_result.get("items") or [],
                item.get("expected_unit_statuses"),
                k_values,
                hard_negative_chunk_ids_by_unit=item.get(
                    "hard_negative_chunk_ids_by_unit"
                ),
            )
        )
        retry_count = int(retrieval_result.get("retrieval_retry_count", 0) or 0)
        metrics["adaptive_retry_trigger_rate"] = float(retry_count > 0)
        if item.get("evidence_requirements"):
            metrics["requirement_coverage_abstention_rate"] = float(
                retrieval_result.get("reason") == "requirement_coverage_incomplete"
            )
        if retry_count > 0:
            metrics["adaptive_rescue_rate"] = float(
                retrieval_result.get("adaptive_retrieval_rescued", False)
            )
        assessments = list(
            retrieval_result.get("evidence_requirement_assessments") or []
        )
        if assessments:
            metrics["requirement_full_coverage_rate"] = float(
                all(row.get("verdict") == "supported" for row in assessments)
            )
        metrics["retrieval_query_count"] = float(
            retrieval_result.get("retrieval_query_count", 0) or 0
        )
        parent_context_count = int(
            retrieval_result.get("parent_context_expanded_count", 0) or 0
        )
        metrics["parent_context_trigger_rate"] = float(parent_context_count > 0)
        metrics["parent_context_expanded_count"] = float(parent_context_count)
        metrics["neighbor_context_expanded_count"] = float(
            retrieval_result.get("neighbor_context_expanded_count", 0) or 0
        )
        for metric_name in EVIDENCE_SPAN_COUNT_METRICS:
            metrics[metric_name] = float(retrieval_result.get(metric_name, 0) or 0)
        span_input_chars = int(
            retrieval_result.get("evidence_span_input_chars", 0) or 0
        )
        span_selected_chars = int(
            retrieval_result.get("evidence_span_selected_chars", 0) or 0
        )
        if span_input_chars > 0:
            metrics["evidence_span_retained_char_ratio"] = (
                span_selected_chars / span_input_chars
            )
        span_eligible_count = int(
            retrieval_result.get("evidence_span_compressed_count", 0) or 0
        ) + int(retrieval_result.get("evidence_span_fallback_count", 0) or 0)
        if span_eligible_count > 0:
            metrics["evidence_span_fallback_rate"] = (
                int(retrieval_result.get("evidence_span_fallback_count", 0) or 0)
                / span_eligible_count
            )
        for metric_name in (
            "evidence_pack_input_count",
            "evidence_pack_kept_count",
            "evidence_pack_dropped_count",
            "evidence_pack_input_chars",
            "evidence_pack_kept_chars",
            "evidence_pack_overlap_removed_chars",
            "evidence_pack_anchor_count",
            "evidence_pack_pinned_count",
        ):
            metrics[metric_name] = float(retrieval_result.get(metric_name, 0) or 0)
        metrics["evidence_pack_over_budget"] = float(
            bool(retrieval_result.get("evidence_pack_over_budget", False))
        )
        pack_requirement_coverage_pre: float | None = None
        pack_requirement_coverage_post: float | None = None
        span_gold_recall_pre: float | None = None
        span_gold_recall_post: float | None = None
        has_effective_gold = any(
            metric.startswith("requirement_recall@") for metric in metrics
        )
        if has_effective_gold:
            pack_requirement_coverage_pre = requirement_coverage_rate(
                retrieval_result.get("evidence_pack_input_items", []),
                item["gold_requirements"],
            )
            pack_requirement_coverage_post = requirement_coverage_rate(
                retrieval_result.get("evidence_pack_context_items", []),
                item["gold_requirements"],
            )
            metrics["evidence_pack_requirement_coverage_pre"] = (
                pack_requirement_coverage_pre
            )
            metrics["evidence_pack_requirement_coverage_post"] = (
                pack_requirement_coverage_post
            )
            metrics["generation_requirement_coverage"] = requirement_coverage_rate(
                retrieval_result.get("generation_context_items", []),
                item["gold_requirements"],
            )
        span_context_items = retrieval_result.get("evidence_pack_context_items", [])
        span_gold_recall_pre = evidence_span_gold_recall(
            span_context_items,
            item.get("gold_requirements", []),
            start_key="evidence_span_input_start",
            end_key="evidence_span_input_end",
        )
        span_gold_recall_post = evidence_span_gold_recall(
            span_context_items,
            item.get("gold_requirements", []),
            start_key="evidence_text_start",
            end_key="evidence_text_end",
        )
        if span_gold_recall_pre is not None and span_gold_recall_post is not None:
            metrics["evidence_span_gold_recall_pre"] = span_gold_recall_pre
            metrics["evidence_span_gold_recall_post"] = span_gold_recall_post
        if item["expected_sources"]:
            metrics["answerable_acceptance_rate"] = (
                1.0 if retrieval_result["supported"] else 0.0
            )
            if verify_evidence:
                metrics["answerable_first_stage_acceptance_rate"] = (
                    1.0 if retrieval_result["first_stage_supported"] else 0.0
                )
        else:
            metrics["no_answer_abstention_rate"] = (
                0.0 if retrieval_result["supported"] else 1.0
            )
            if verify_evidence:
                metrics["no_answer_first_stage_abstention_rate"] = (
                    0.0 if retrieval_result["first_stage_supported"] else 1.0
                )
        rows.append(
            {
                "id": item.get("id"),
                "layer": str(item.get("layer") or infer_retrieval_layer(item)),
                "query": item["query"],
                "expected_sources": item["expected_sources"],
                "retrieved_sources": retrieved,
                "retrieved_items": retrieval_result.get("items", []),
                "retrieval_supported": retrieval_result["supported"],
                "retrieval_confidence": retrieval_result["confidence"],
                "retrieval_abstain_reason": retrieval_result["reason"],
                "retrieval_signals": retrieval_result["signals"],
                "retrieval_first_stage_supported": retrieval_result[
                    "first_stage_supported"
                ],
                "evidence_verification_required": retrieval_result[
                    "evidence_verification_required"
                ],
                "evidence_supported": retrieval_result["evidence_supported"],
                "evidence_verification_reason": retrieval_result[
                    "evidence_verification_reason"
                ],
                "evidence_verified_chunk_ids": retrieval_result[
                    "evidence_verified_chunk_ids"
                ],
                "evidence_verifier_error": retrieval_result.get(
                    "evidence_verifier_error", ""
                ),
                "evidence_requirement_assessments": assessments,
                "missing_evidence_requirement_ids": retrieval_result.get(
                    "missing_evidence_requirement_ids", []
                ),
                "retrieval_retry_count": retry_count,
                "adaptive_retrieval_rescued": retrieval_result.get(
                    "adaptive_retrieval_rescued", False
                ),
                "retrieval_query_count": retrieval_result.get(
                    "retrieval_query_count", 0
                ),
                "retrieval_ranking_count": retrieval_result.get(
                    "retrieval_ranking_count", 0
                ),
                "retrieval_channel_counts": retrieval_result.get(
                    "retrieval_channel_counts", {}
                ),
                "retrieval_carryover_count": retrieval_result.get(
                    "retrieval_carryover_count", 0
                ),
                "generation_context_items": retrieval_result.get(
                    "generation_context_items", []
                ),
                "evidence_span_input_count": int(
                    retrieval_result.get("evidence_span_input_count", 0) or 0
                ),
                "evidence_span_output_count": int(
                    retrieval_result.get("evidence_span_output_count", 0) or 0
                ),
                "evidence_span_compressed_count": int(
                    retrieval_result.get("evidence_span_compressed_count", 0) or 0
                ),
                "evidence_span_fallback_count": int(
                    retrieval_result.get("evidence_span_fallback_count", 0) or 0
                ),
                "evidence_span_input_chars": span_input_chars,
                "evidence_span_selected_chars": span_selected_chars,
                "evidence_span_reason_counts": {
                    str(reason): _nonnegative_int(count)
                    for reason, count in _mapping(
                        retrieval_result.get("evidence_span_reason_counts")
                    ).items()
                },
                "evidence_span_gold_recall_pre": span_gold_recall_pre,
                "evidence_span_gold_recall_post": span_gold_recall_post,
                "evidence_pack_input_items": retrieval_result.get(
                    "evidence_pack_input_items", []
                ),
                "evidence_pack_context_items": retrieval_result.get(
                    "evidence_pack_context_items", []
                ),
                "evidence_pack_input_count": int(
                    retrieval_result.get("evidence_pack_input_count", 0) or 0
                ),
                "evidence_pack_kept_count": int(
                    retrieval_result.get("evidence_pack_kept_count", 0) or 0
                ),
                "evidence_pack_dropped_count": int(
                    retrieval_result.get("evidence_pack_dropped_count", 0) or 0
                ),
                "evidence_pack_input_chars": int(
                    retrieval_result.get("evidence_pack_input_chars", 0) or 0
                ),
                "evidence_pack_kept_chars": int(
                    retrieval_result.get("evidence_pack_kept_chars", 0) or 0
                ),
                "evidence_pack_overlap_removed_chars": int(
                    retrieval_result.get("evidence_pack_overlap_removed_chars", 0) or 0
                ),
                "evidence_pack_drop_reason_counts": dict(
                    retrieval_result.get("evidence_pack_drop_reason_counts", {}) or {}
                ),
                "evidence_pack_over_budget": bool(
                    retrieval_result.get("evidence_pack_over_budget", False)
                ),
                "evidence_pack_requirement_coverage_pre": (
                    pack_requirement_coverage_pre
                ),
                "evidence_pack_requirement_coverage_post": (
                    pack_requirement_coverage_post
                ),
                "parent_context_expanded_count": parent_context_count,
                "neighbor_context_expanded_count": retrieval_result.get(
                    "neighbor_context_expanded_count", 0
                ),
                "retrieval_feedback_error": retrieval_result.get(
                    "retrieval_feedback_error", ""
                ),
                "rerank_devices": list(retrieval_result.get("rerank_devices", [])),
                "rerank_skip_reasons": list(
                    retrieval_result.get("rerank_skip_reasons", [])
                ),
                "latency_ms": latency_ms,
                "metrics": metrics,
            }
        )

    annotation_stats = annotation_coverage_stats(items)
    aggregate_metrics = _aggregate_rows(rows)
    metric_denominators = _metric_denominators(
        rows,
        aggregate_metrics,
        annotation_stats["effective_sample_counts"],
    )
    sample_minimums = evidence_metric_minimums(evidence_metric_minimum_samples)
    baseline_gated_metrics: list[str] = []
    baseline_skipped_metrics: dict[str, dict[str, Any]] = {}
    for metric in aggregate_metrics:
        if metric_direction(metric) != "higher":
            continue
        sample_kind = evidence_metric_sample_kind(metric)
        if sample_kind is not None:
            denominator = metric_denominators.get(metric, 0)
            required = sample_minimums[sample_kind]
            if denominator < required:
                baseline_skipped_metrics[metric] = {
                    "sample_kind": sample_kind,
                    "denominator": denominator,
                    "required": required,
                    "reason": "insufficient_samples",
                }
                continue
            # Post-pack/span gold metrics are intentionally promoted only after
            # their independent query denominator reaches the maturity floor.
            baseline_gated_metrics.append(metric)
            continue
        if not _is_diagnostic_metric(metric):
            baseline_gated_metrics.append(metric)
    by_layer = {
        layer: {
            "count": len(layer_rows),
            "aggregate": _aggregate_rows(layer_rows),
        }
        for layer, layer_rows in _group_rows(rows, "layer").items()
    }
    return {
        "config": {
            "k_values": k_values,
            "rerank": rerank,
            "verify_evidence": verify_evidence,
            "is_local_verifier": is_local_verifier,
            "num_queries": len(items),
            "answerable_queries": sum(bool(item["expected_sources"]) for item in items),
            "no_answer_queries": sum(not item["expected_sources"] for item in items),
            "requirement_annotated_queries": sum(
                bool(item.get("gold_requirements")) for item in items
            ),
            "span_annotated_queries": sum(
                any(
                    _acceptable_gold_spans(requirement)
                    for requirement in item.get("gold_requirements", [])
                    if isinstance(requirement, Mapping)
                )
                for item in items
            ),
            "span_annotated_requirements": sum(
                _annotated_span_requirement_count(
                    [
                        requirement
                        for requirement in item.get("gold_requirements", [])
                        if isinstance(requirement, Mapping)
                    ]
                )
                for item in items
            ),
            **annotation_stats,
            "evidence_metric_minimum_samples": sample_minimums,
            "qa_evidence_span_enabled": settings.qa_evidence_span_enabled,
            "qa_evidence_span_max_chars_per_doc": (
                settings.qa_evidence_span_max_chars_per_doc
            ),
            "qa_evidence_span_context_sentences": (
                settings.qa_evidence_span_context_sentences
            ),
            "qa_evidence_pack_max_docs": settings.qa_evidence_pack_max_docs,
            "qa_evidence_pack_max_chars": settings.qa_evidence_pack_max_chars,
            "rerank_devices": sorted(
                {
                    str(device)
                    for row in rows
                    for device in row.get("rerank_devices", [])
                    if str(device)
                }
            ),
            "rerank_skip_reasons": sorted(
                {
                    str(reason)
                    for row in rows
                    for reason in row.get("rerank_skip_reasons", [])
                    if str(reason)
                }
            ),
            "rerank_skipped_query_count": sum(
                bool(row.get("rerank_skip_reasons")) for row in rows
            ),
            "warmup_latency_ms": warmup_latency_ms,
        },
        "aggregate": aggregate_metrics,
        "metric_denominators": metric_denominators,
        "baseline_gated_metrics": sorted(baseline_gated_metrics),
        "baseline_skipped_metrics": dict(sorted(baseline_skipped_metrics.items())),
        "metric_directions": {
            metric: metric_direction(metric) for metric in aggregate_metrics
        },
        "by_layer": by_layer,
        "rows": rows,
    }


# 按报告行聚合质量和性能指标。
def _aggregate_rows(rows: List[dict]) -> Dict[str, float]:
    if not rows:
        return {}
    result = aggregate([row["metrics"] for row in rows])
    latencies = [float(row["latency_ms"]) for row in rows]
    result["latency_mean_ms"] = statistics.mean(latencies)
    result["latency_p95_ms"] = percentile(latencies, 95)
    return result


def _metric_denominators(
    rows: Sequence[Mapping[str, Any]],
    aggregate_metrics: Mapping[str, float],
    effective_sample_counts: Mapping[str, int],
) -> Dict[str, int]:
    denominators = {
        metric: sum(metric in row.get("metrics", {}) for row in rows)
        for metric in aggregate_metrics
    }
    for metric, denominator in denominators.items():
        sample_kind = evidence_metric_sample_kind(metric)
        if sample_kind is not None:
            # A runtime metric may occasionally be emitted for a malformed draft
            # annotation. Such a row is observable, but it is not an effective
            # independent gold sample and therefore cannot mature a release gate.
            denominators[metric] = min(
                denominator,
                effective_sample_counts.get(sample_kind, 0),
            )
    for metric in ("latency_mean_ms", "latency_p95_ms"):
        if metric in aggregate_metrics:
            denominators[metric] = len(rows)
    return denominators


# 按字段分组报告行。
def _group_rows(rows: List[dict], key: str) -> Dict[str, List[dict]]:
    grouped: Dict[str, List[dict]] = {}
    for row in rows:
        grouped.setdefault(str(row[key]), []).append(row)
    return dict(sorted(grouped.items()))


# 输出报告。
def print_report(report: dict) -> None:
    cfg = report["config"]
    print(
        f"\n检索评测  |  queries={cfg['num_queries']}  rerank={cfg['rerank']}  k={cfg['k_values']}\n"
    )
    print(f"  warmup={cfg['warmup_latency_ms']:.1f}ms（不计入稳态延迟）\n")

    for row in report["rows"]:
        if row["expected_sources"]:
            recalls = "  ".join(
                f"r@{k}={row['metrics'][f'recall@{k}']:.2f}" for k in cfg["k_values"]
            )
            score = f"{row['metrics']['mrr']:.2f} MRR  {recalls}"
            score += "  accepted" if row["retrieval_supported"] else "  false-abstain"
        else:
            flags = "  ".join(
                f"fp@{k}={row['metrics'][f'no_answer_false_positive@{k}']:.0f}"
                for k in cfg["k_values"]
            )
            decision = "accepted" if row["retrieval_supported"] else "abstained"
            score = f"{flags}  {decision}"
        print(
            f"  [{row['layer']}] [{score}] {row['latency_ms']:.1f}ms  | {row['query']}"
        )
        print(f"        expected={row['expected_sources']}")
        print(f"        top={row['retrieved_sources'][: max(cfg['k_values'])]}")
        print(
            f"        confidence={row['retrieval_confidence']:.4f} "
            f"reason={row['retrieval_abstain_reason']} "
            f"signals={row['retrieval_signals']}"
        )
        if row.get("retrieval_query_count") or row.get("retrieval_retry_count"):
            print(
                "        adaptive="
                f"queries={row.get('retrieval_query_count', 0)} "
                f"rankings={row.get('retrieval_ranking_count', 0)} "
                f"retry={row.get('retrieval_retry_count', 0)} "
                f"carryover={row.get('retrieval_carryover_count', 0)} "
                f"rescued={row.get('adaptive_retrieval_rescued', False)} "
                f"channels={row.get('retrieval_channel_counts', {})}"
            )
        if row.get("evidence_pack_input_count") or row.get("evidence_pack_over_budget"):
            print(
                "        evidence_pack="
                f"docs={row.get('evidence_pack_input_count', 0)}"
                f"->{row.get('evidence_pack_kept_count', 0)} "
                f"chars={row.get('evidence_pack_input_chars', 0)}"
                f"->{row.get('evidence_pack_kept_chars', 0)} "
                f"overlap_removed="
                f"{row.get('evidence_pack_overlap_removed_chars', 0)} "
                f"drops={row.get('evidence_pack_drop_reason_counts', {})} "
                f"over_budget={row.get('evidence_pack_over_budget', False)}"
            )
        if row.get("evidence_span_input_count") or row.get(
            "evidence_span_fallback_count"
        ):
            print(
                "        evidence_span="
                f"docs={row.get('evidence_span_input_count', 0)}"
                f"->{row.get('evidence_span_output_count', 0)} "
                f"chars={row.get('evidence_span_input_chars', 0)}"
                f"->{row.get('evidence_span_selected_chars', 0)} "
                f"compressed={row.get('evidence_span_compressed_count', 0)} "
                f"fallback={row.get('evidence_span_fallback_count', 0)} "
                f"reasons={row.get('evidence_span_reason_counts', {})}"
            )
            if row.get("evidence_span_gold_recall_pre") is not None:
                print(
                    "        gold_span_recall="
                    f"{row['evidence_span_gold_recall_pre']:.4f}"
                    f"->{row['evidence_span_gold_recall_post']:.4f}"
                )
        if cfg.get("verify_evidence"):
            print(
                "        evidence_verify="
                f"{row['evidence_verification_required']} "
                f"supported={row['evidence_supported']} "
                f"chunks={row['evidence_verified_chunk_ids']} "
                f"reason={row['evidence_verification_reason']}"
            )
            if row.get("evidence_requirement_assessments"):
                print(f"        requirements={row['evidence_requirement_assessments']}")

    print("\n聚合:")
    for key, value in report["aggregate"].items():
        print(f"  {key:<34} {value:.4f}")
    print("\n按检索层:")
    for layer, summary in report["by_layer"].items():
        metrics = "  ".join(
            f"{key}={value:.4f}" for key, value in summary["aggregate"].items()
        )
        print(f"  {layer:<14} count={summary['count']}  {metrics}")
    print()


# 输出覆盖审计结果。
def print_coverage(coverage: dict) -> None:
    print("\n覆盖审计:")
    print(f"  layer_counts={coverage['layer_counts']}")
    print(f"  minimums={coverage['minimum_layer_counts']}")
    if coverage["missing_layers"]:
        print(f"  缺少 layer: {coverage['missing_layers']}")
    if coverage["insufficient_layers"]:
        print(f"  数量不足: {coverage['insufficient_layers']}")
    print(f"  evidence_samples={coverage['effective_sample_counts']}")
    print(f"  evidence_units={coverage['effective_annotation_counts']}")
    print(f"  evidence_minimums={coverage['minimum_annotation_counts']}")
    invalid_samples = {
        kind: count
        for kind, count in coverage["invalid_sample_counts"].items()
        if count
    }
    if invalid_samples:
        print(f"  无效标注: {invalid_samples}")
    if coverage["insufficient_annotations"]:
        print(f"  证据标注数量不足: {coverage['insufficient_annotations']}")
    if coverage["is_coverage_complete"]:
        print("  覆盖完整")
    print()


# 生成基线对比。
def compare_baseline(report: dict, baseline_path: Path) -> int:
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    base_agg = baseline.get("aggregate", {})
    cur_agg = report["aggregate"]
    print(f"\n对比基线 {baseline_path}:")
    regressed = False
    gated_metrics = report.get("baseline_gated_metrics")
    metric_names = (
        sorted(gated_metrics) if gated_metrics is not None else sorted(cur_agg)
    )
    for key in metric_names:
        cur = cur_agg[key]
        base = base_agg.get(key)
        if base is None:
            print(f"  {key:<12} {cur:.4f}  (基线缺该指标)")
            continue
        delta = cur - base
        direction = report.get("metric_directions", {}).get(key, metric_direction(key))
        flag = ""
        regressed_metric = delta < -1e-9 if direction == "higher" else delta > 1e-9
        improved_metric = delta > 1e-9 if direction == "higher" else delta < -1e-9
        if regressed_metric:
            flag = "  ⚠ 回退"
            regressed = True
        elif improved_metric:
            flag = "  ✅ 提升"
        print(f"  {key:<12} {cur:.4f}  (基线 {base:.4f}, Δ{delta:+.4f}){flag}")
    print()
    return 1 if regressed else 0


# 启动入口。
def main() -> int:
    parser = argparse.ArgumentParser(description="离线检索评测 harness")
    parser.add_argument(
        "--eval-set",
        type=Path,
        default=None,
        help="评测集 JSONL；缺省用本地 retrieval_eval.jsonl，没有则回退 example",
    )
    parser.add_argument("--rerank", action="store_true", help="在检索后加 BGE 精排")
    parser.add_argument(
        "--verify-evidence",
        action="store_true",
        help="对精确事实问题执行二阶段证据充分性模型校验",
    )
    parser.add_argument(
        "--local-verifier",
        action="store_true",
        help="二阶段证据校验使用本地 Ollama；必须同时指定 --verify-evidence",
    )
    parser.add_argument(
        "--k",
        type=int,
        nargs="+",
        default=DEFAULT_K_VALUES,
        help="recall/hit 的 k 截断值",
    )
    parser.add_argument("--json", type=Path, default=None, help="把报告写入 JSON 文件")
    parser.add_argument(
        "--baseline",
        type=Path,
        default=None,
        help="与基线 JSON 报告对比，回退则退出码非零",
    )
    parser.add_argument(
        "--check-coverage",
        action="store_true",
        help="检查评测集是否覆盖单源、多源和无答案层级",
    )
    parser.add_argument(
        "--coverage-only",
        action="store_true",
        help="只检查评测集覆盖面，不执行真实检索",
    )
    parser.add_argument(
        "--coverage-profile",
        choices=("smoke", "baseline"),
        default="smoke",
        help=(
            "smoke 每层至少 1 条；baseline 要求 40/20/20/20 共 100 条，"
            "并检查证据标注分母"
        ),
    )
    parser.add_argument(
        "--gate",
        type=Path,
        default=None,
        help="绝对指标门禁 JSON，包含 minimum/maximum 两组阈值",
    )
    args = parser.parse_args()
    if args.local_verifier and not args.verify_evidence:
        parser.error("--local-verifier 必须与 --verify-evidence 同时使用")
    if args.coverage_only and (
        args.check_coverage or args.json or args.baseline or args.gate
    ):
        parser.error(
            "--coverage-only 不能与 --check-coverage、--json、--baseline 或 --gate 同时使用"
        )

    threshold_config = None
    if args.gate:
        threshold_config = json.loads(args.gate.read_text(encoding="utf-8"))
    configured_sample_minimums = (
        threshold_config.get("minimum_samples", {}) if threshold_config else {}
    )
    if not isinstance(configured_sample_minimums, Mapping):
        parser.error("gate.minimum_samples 必须是对象")
    try:
        coverage_requirements = coverage_minimums(
            args.coverage_profile,
            annotation_minimums=configured_sample_minimums,
        )
        metric_sample_minimums = evidence_metric_minimums(configured_sample_minimums)
    except ValueError as exc:
        parser.error(str(exc))

    eval_set = args.eval_set or resolve_default_eval_set()
    items = load_eval_set(eval_set)
    if not items:
        print(f"评测集为空: {eval_set}")
        return 1

    coverage = audit_coverage(items, coverage_requirements)
    if args.coverage_only:
        print_coverage(coverage)
        return 0 if coverage["is_coverage_complete"] else 1

    report = run_eval(
        items,
        sorted(args.k),
        args.rerank,
        verify_evidence=args.verify_evidence,
        is_local_verifier=args.local_verifier,
        evidence_metric_minimum_samples=metric_sample_minimums,
    )
    threshold_gate = None
    if threshold_config is not None:
        threshold_gate = evaluate_thresholds(
            report["aggregate"],
            threshold_config,
            metric_denominators=report["metric_denominators"],
            minimum_samples=metric_sample_minimums,
        )
        report["threshold_gate"] = threshold_gate
    print_report(report)
    if threshold_gate:
        print("绝对指标门禁:")
        for row in threshold_gate["rows"]:
            current = "-" if row["current"] is None else f"{row['current']:.4f}"
            status = "通过" if row["passed"] else "失败"
            sample_suffix = ""
            if row.get("minimum_samples") is not None:
                sample_count = (
                    "-" if row.get("sample_count") is None else row["sample_count"]
                )
                sample_suffix = (
                    f" samples={sample_count}/{row['minimum_samples']}"
                    f" reason={row.get('failure_reason') or '-'}"
                )
            print(
                f"  {row['metric']:<34} {current} "
                f"{row['bound']}={row['limit']:.4f}  {status}{sample_suffix}"
            )
        print()
    if args.check_coverage:
        report["coverage"] = coverage
        print_coverage(coverage)

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"报告已写入 {args.json}")

    if args.baseline:
        baseline_status = compare_baseline(report, args.baseline)
        if args.check_coverage and not coverage["is_coverage_complete"]:
            return 1
        if threshold_gate and not threshold_gate["passed"]:
            return 1
        return baseline_status
    if threshold_gate and not threshold_gate["passed"]:
        return 1
    if args.check_coverage and not coverage["is_coverage_complete"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
