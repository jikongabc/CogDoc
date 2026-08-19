import copy
import logging
import math
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from typing import Any, cast
from langchain_core.messages import AIMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import StateGraph, START, END
from cogdoc.config.settings import get_settings
from cogdoc.agents.evidence_verifier import (
    EvidenceVerifierAgent,
    select_verification_docs,
    should_verify_evidence,
)
from cogdoc.graph.state import GraphState, Evidence, RetrievedDoc
from cogdoc.observability.logger import log_event
from cogdoc.service.kb_readers import kb_read_lease
from cogdoc.service.retriever_factory import RetrieverFactory
from cogdoc.tools.retriever.metadata import safe_retrieval_metadata
from cogdoc.tools.retriever.parent_context import select_parent_context
from cogdoc.tools.evidence_rendering import (
    EVIDENCE_BLOCK_SEPARATOR,
    evidence_block_char_count,
)
from cogdoc.tools.citation_ledger import assign_evidence_ids
from cogdoc.tools.retriever.evidence_pack import (
    DROP_DUPLICATE_CHUNK_ID,
    EvidencePack,
    build_evidence_pack_from_sources,
)
from cogdoc.tools.retriever.evidence_spans import EvidenceSpanSelector
from cogdoc.tools.retriever.confidence import assess_retrieval_support
from cogdoc.tools.retriever.fusion import select_rerank_candidates
from cogdoc.tools.reranker import (
    BGEReranker,  # noqa: F401 - compatibility hook for tests/extensions
    dynamic_rerank_top_n,
    requirement_query_map,
    rerank_with_requirement_policy,
)
from cogdoc.service.retrieval_pipeline import (
    apply_retrieval_feedback,
    build_retrieval_queries,
    retrieve_candidate_pool,
)
from cogdoc.service.evidence_unit_pipeline import evidence_unit_plan_state
from cogdoc.service.evidence_units import build_qa_evidence_units
from cogdoc.service.claim_audit_projection import (
    ClaimAuditProjectionSegment,
    build_claim_audit_projection,
)
from cogdoc.agents.answer_markers import NO_RELEVANT_CONTENT_ANSWER
from cogdoc.agents.qa_generator import Generator
from cogdoc.agents.query_rewriter import QueryRewriteAgent
from cogdoc.agents.rewrite_verifier import RewriteVerifyAgent
from cogdoc.agents.citation_validator import CitationValidatorAgent


# 保留旧的模块内测试/扩展入口，实际实现已统一下沉到共享检索 pipeline。
_apply_retrieval_feedback = apply_retrieval_feedback


NEIGHBOR_CONTEXT_RADIUS = 1
CITATION_CORRECTION_PROMPT_TEMPLATE = (
    "\n\n【引用校验失败通知】\n"
    "你上一轮的回答已被引用校验器拦截，错误详情如下：\n\n"
    "{critique}\n\n"
    "请严格按照上述修正要求重新生成答案，确保每处引用使用对应标签中"
    "完全一致的 [E001] Evidence ID。"
)
QA_GENERATION_FAILURE_ANSWER = "模型未生成可用答案，请稍后重试。"


# 处理问题改写节点。
def rewrite_node(state: GraphState) -> dict:
    return QueryRewriteAgent.rewrite_query(state)


# 校验问题改写节点。
def verify_rewrite_node(state: GraphState) -> dict:
    # 在检索前过滤语义漂移的问题改写。
    return RewriteVerifyAgent.verify_rewrites(state)


# 定位命中文本块在源文档文本块序列中的位置。
def _find_source_chunk_index(
    source_chunks: list[RetrievedDoc], target_doc: RetrievedDoc
) -> int:
    target_meta = target_doc.get("meta", {})
    target_id = str(target_meta.get("chunk_id", ""))
    if target_id:
        for idx, doc in enumerate(source_chunks):
            if str(doc.get("meta", {}).get("chunk_id", "") or "") == target_id:
                return idx

    target_local = target_meta.get("local_chunk_index")
    if target_local is None:
        return -1
    for idx, doc in enumerate(source_chunks):
        if doc.get("meta", {}).get("local_chunk_index") == target_local:
            return idx
    return -1


# 复制上下文文本块并标记带入来源；chunk_id/page 始终保持该 child 自己的身份。
def _copy_context_doc(
    doc: RetrievedDoc, anchor_chunk_id: str, expansion: str
) -> RetrievedDoc:
    copied = copy.deepcopy(doc)
    retrieval = copied.setdefault("retrieval", {})
    retrieval["search_channel"] = (
        "parent_context" if expansion == "section" else "neighbor"
    )
    retrieval["context_anchor_chunk_id"] = anchor_chunk_id
    retrieval["context_expansion"] = expansion
    return copied


# 生成缺失文本块标识时的临时去重键。
def _missing_chunk_key(expanded: "OrderedDict[str, RetrievedDoc]") -> str:
    return f"__missing_chunk_id_{len(expanded)}"


def _order_value(doc: Mapping[str, Any], key: str) -> int | None:
    value = doc.get("meta", {}).get(key)
    if value is None or isinstance(value, bool):
        return None
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        return None
    return normalized if normalized >= 0 else None


def _ordered_expanded_docs(
    reranked_docs: list[RetrievedDoc], expanded_docs: list[RetrievedDoc]
) -> list[RetrievedDoc]:
    """Keep parent groups in anchor-rank order and children in document order."""

    emitted: set[int] = set()
    ordered: list[RetrievedDoc] = []
    seen_parent_ids: set[str] = set()
    indexed_docs = list(enumerate(expanded_docs))

    def append_candidates(candidates: list[tuple[int, RetrievedDoc]]) -> None:
        for expanded_index, candidate in candidates:
            if expanded_index in emitted:
                continue
            emitted.add(expanded_index)
            ordered.append(candidate)

    for anchor in reranked_docs:
        anchor_meta = anchor.get("meta", {})
        parent_chunk_id = str(anchor_meta.get("parent_chunk_id") or "")
        anchor_chunk_id = str(anchor_meta.get("chunk_id") or "")
        if parent_chunk_id:
            if parent_chunk_id in seen_parent_ids:
                continue
            seen_parent_ids.add(parent_chunk_id)
            candidates = [
                (index, doc)
                for index, doc in indexed_docs
                if str(doc.get("meta", {}).get("parent_chunk_id") or "")
                == parent_chunk_id
            ]
            candidates.sort(
                key=lambda item: (
                    _order_value(item[1], "child_index_in_parent")
                    if _order_value(item[1], "child_index_in_parent") is not None
                    else math.inf,
                    _order_value(item[1], "chunk_index")
                    if _order_value(item[1], "chunk_index") is not None
                    else math.inf,
                    item[0],
                )
            )
            append_candidates(candidates)
            continue
        if not anchor_chunk_id:
            continue
        candidates = [
            (index, doc)
            for index, doc in indexed_docs
            if str(doc.get("meta", {}).get("chunk_id") or "") == anchor_chunk_id
            or str(doc.get("retrieval", {}).get("context_anchor_chunk_id") or "")
            == anchor_chunk_id
        ]
        candidates.sort(
            key=lambda item: (
                _order_value(item[1], "chunk_index")
                if _order_value(item[1], "chunk_index") is not None
                else math.inf,
                item[0],
            )
        )
        append_candidates(candidates)

    append_candidates(indexed_docs)
    return ordered


# 为重排命中补充同章节 child 窗口；旧索引或结构不完整时回退前后邻块。
def _expand_with_neighbor_chunks(
    doc_id: str, reranked_docs: list[RetrievedDoc], state: GraphState | None = None
) -> list[RetrievedDoc]:
    if not reranked_docs:
        return []

    expanded: "OrderedDict[str, RetrievedDoc]" = OrderedDict()
    source_cache: dict[str, list[RetrievedDoc]] = {}
    settings = get_settings()
    parent_context_enabled = bool(getattr(settings, "qa_parent_context_enabled", True))
    parent_context_max_chunks = max(
        1, int(getattr(settings, "qa_parent_context_max_chunks", 5))
    )
    parent_context_max_chars = max(
        0, int(getattr(settings, "qa_parent_context_max_chars", 3600))
    )
    try:
        with kb_read_lease(doc_id):
            engine = RetrieverFactory.get_engine(doc_id)
            for doc in reranked_docs:
                meta = doc.get("meta", {})
                if meta.get("source_type") == "derived_knowledge":
                    expanded[
                        str(meta.get("chunk_id", "")) or _missing_chunk_key(expanded)
                    ] = copy.deepcopy(doc)
                    continue
                source = str(meta.get("source", "") or "")
                anchor_chunk_id = str(meta.get("chunk_id", ""))
                if not source or not anchor_chunk_id:
                    expanded[anchor_chunk_id or _missing_chunk_key(expanded)] = (
                        copy.deepcopy(doc)
                    )
                    continue

                if source not in source_cache:
                    source_cache[source] = engine.load_source_chunks(source)
                source_chunks = source_cache[source]
                hit_idx = _find_source_chunk_index(source_chunks, doc)
                if hit_idx < 0:
                    expanded[anchor_chunk_id or _missing_chunk_key(expanded)] = (
                        copy.deepcopy(doc)
                    )
                    continue

                context_docs: list[RetrievedDoc] = []
                expansion = "neighbor"
                if parent_context_enabled:
                    selection = select_parent_context(
                        source_chunks,
                        doc,
                        max_chunks=parent_context_max_chunks,
                        max_chars=parent_context_max_chars,
                    )
                    if not selection.fallback_required:
                        context_docs = selection.docs
                        expansion = "section"
                if not context_docs:
                    start = max(0, hit_idx - NEIGHBOR_CONTEXT_RADIUS)
                    end = min(len(source_chunks), hit_idx + NEIGHBOR_CONTEXT_RADIUS + 1)
                    context_docs = source_chunks[start:end]

                for context_doc in context_docs:
                    context_chunk_id = str(
                        context_doc.get("meta", {}).get("chunk_id", "") or ""
                    )
                    if not context_chunk_id:
                        continue
                    if context_chunk_id == anchor_chunk_id:
                        expanded[context_chunk_id] = copy.deepcopy(doc)
                    elif context_chunk_id not in expanded:
                        expanded[context_chunk_id] = _copy_context_doc(
                            context_doc, anchor_chunk_id, expansion
                        )
    except Exception as exc:
        log_event(
            "qa",
            "qa_context_expand_failed",
            state,
            level=logging.WARNING,
            error_class=type(exc).__name__,
        )
        return reranked_docs

    return _ordered_expanded_docs(reranked_docs, list(expanded.values()))


# 校验候选先放 rerank anchor，再放同章节上下文，最后补其余排名候选。
def _verification_candidates_with_context(
    reranked_docs: list[RetrievedDoc],
    expanded_docs: list[RetrievedDoc],
    ranked_candidates: list[RetrievedDoc],
) -> list[RetrievedDoc]:
    merged: "OrderedDict[str, RetrievedDoc]" = OrderedDict()
    seen_identityless_objects: set[int] = set()

    def add(doc: RetrievedDoc) -> None:
        chunk_id = str(doc.get("meta", {}).get("chunk_id", "") or "")
        if not chunk_id:
            object_id = id(doc)
            if object_id in seen_identityless_objects:
                return
            seen_identityless_objects.add(object_id)
        key = chunk_id or _missing_chunk_key(merged)
        if key not in merged:
            merged[key] = doc

    for doc in reranked_docs:
        add(doc)

    # Verifier budgets are usually smaller than generation windows.  Feed it
    # the nearest left/right siblings first so a five-child balanced window is
    # not truncated into anchor + two left-side children.
    expanded_positions = {id(doc): index for index, doc in enumerate(expanded_docs)}
    for anchor_rank, anchor in enumerate(reranked_docs):
        anchor_chunk_id = str(anchor.get("meta", {}).get("chunk_id") or "")
        anchor_order = _order_value(anchor, "child_index_in_parent")
        if anchor_order is None:
            anchor_order = _order_value(anchor, "chunk_index")
        siblings = [
            doc
            for doc in expanded_docs
            if anchor_chunk_id
            and str(doc.get("retrieval", {}).get("context_anchor_chunk_id") or "")
            == anchor_chunk_id
        ]

        def proximity(doc: RetrievedDoc) -> tuple[float, float, int, int]:
            child_order = _order_value(doc, "child_index_in_parent")
            if child_order is None:
                child_order = _order_value(doc, "chunk_index")
            if anchor_order is None or child_order is None:
                return (math.inf, math.inf, anchor_rank, expanded_positions[id(doc)])
            return (
                abs(child_order - anchor_order),
                child_order,
                anchor_rank,
                expanded_positions[id(doc)],
            )

        for doc in sorted(siblings, key=proximity):
            add(doc)

    for doc in (*expanded_docs, *ranked_candidates):
        add(doc)
    return list(merged.values())


def _build_qa_evidence_pack(
    *,
    query: str,
    evidence_requirements: Sequence[Mapping[str, Any]],
    anchors: list[RetrievedDoc],
    expanded_docs: list[RetrievedDoc],
    ranked_candidates: list[RetrievedDoc],
    pinned_chunk_ids: set[str],
    requirement_ids: list[str],
    settings: Any,
) -> tuple[EvidencePack, dict[str, Any]]:
    """Build the one closed evidence set shared by verifier and generator."""

    span_metrics: dict[str, Any] = {
        "evidence_span_input_count": 0,
        "evidence_span_output_count": 0,
        "evidence_span_compressed_count": 0,
        "evidence_span_fallback_count": 0,
        "evidence_span_input_chars": 0,
        "evidence_span_selected_chars": 0,
        "evidence_span_reason_counts": {},
    }
    document_transform = None
    if bool(getattr(settings, "qa_evidence_span_enabled", True)):
        selector = EvidenceSpanSelector(
            query=query,
            evidence_requirements=evidence_requirements,
            max_chars_per_doc=max(
                1,
                int(getattr(settings, "qa_evidence_span_max_chars_per_doc", 420)),
            ),
            context_sentences=max(
                0,
                int(getattr(settings, "qa_evidence_span_context_sentences", 1)),
            ),
        )

        def select_span(
            doc: RetrievedDoc, matched_requirement_ids: tuple[str, ...]
        ) -> RetrievedDoc:
            selected = selector.select(
                doc, matched_requirement_ids=matched_requirement_ids
            )
            retrieval = selected.get("retrieval", {})
            retrieval["evidence_span_matched_unit_ids"] = list(
                retrieval.get("evidence_span_matched_requirement_ids") or []
            )
            reason = str(retrieval.get("evidence_span_reason") or "")
            original_chars = max(
                0, int(retrieval.get("evidence_span_original_chars") or 0)
            )
            selected_chars = max(
                0, int(retrieval.get("evidence_span_selected_chars") or 0)
            )
            span_metrics["evidence_span_input_count"] += 1
            span_metrics["evidence_span_output_count"] += 1
            span_metrics["evidence_span_input_chars"] += original_chars
            span_metrics["evidence_span_selected_chars"] += selected_chars
            span_metrics["evidence_span_compressed_count"] += int(
                bool(retrieval.get("evidence_span_selected"))
            )
            span_metrics["evidence_span_fallback_count"] += int(
                reason.startswith("fallback_")
            )
            reason_counts = span_metrics["evidence_span_reason_counts"]
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
            return selected

        document_transform = select_span

    pack = build_evidence_pack_from_sources(
        anchors=anchors,
        expanded_docs=expanded_docs,
        verification_candidates=ranked_candidates,
        pinned_chunk_ids=pinned_chunk_ids,
        requirement_ids=requirement_ids,
        max_docs=settings.qa_evidence_pack_max_docs,
        max_chars=settings.qa_evidence_pack_max_chars,
        document_char_cost=evidence_block_char_count,
        separator_chars=len(EVIDENCE_BLOCK_SEPARATOR),
        document_transform=document_transform,
    )
    return pack, span_metrics


def _verification_docs_from_pack(
    pack: EvidencePack,
    *,
    max_docs: int,
    requirement_ids: list[str],
    pinned_chunk_ids: set[str],
) -> list[RetrievedDoc]:
    """Select verifier rows only from the exact materialized generation pack."""

    packed_docs = list(pack.kept_docs)
    packed_anchors = [
        item.doc
        for item in sorted(
            (item for item in pack.kept if "anchor" in item.provenance),
            key=lambda item: (
                item.anchor_rank if item.anchor_rank is not None else math.inf,
                item.ref.input_order,
            ),
        )
    ]
    priority_pool = _verification_candidates_with_context(
        packed_anchors,
        packed_docs,
        [],
    )
    return list(
        select_verification_docs(
            priority_pool,
            max_docs,
            requirement_ids=requirement_ids,
            pinned_chunk_ids=pinned_chunk_ids,
        )
    )


def _evidence_pack_metrics(
    pack: EvidencePack,
    *,
    pinned_chunk_ids: set[str],
) -> dict[str, Any]:
    drop_reason_counts: dict[str, int] = {}
    for item in pack.dropped:
        # Duplicate stage entries were normalized before the global budget and
        # therefore are not generation-evidence drops.
        if item.reason == DROP_DUPLICATE_CHUNK_ID:
            continue
        drop_reason_counts[item.reason] = drop_reason_counts.get(item.reason, 0) + 1
    kept_ids = {item.ref.chunk_id for item in pack.kept if item.ref.chunk_id}
    return {
        "evidence_pack_input_count": pack.input_count,
        "evidence_pack_kept_count": len(pack.kept),
        "evidence_pack_dropped_count": max(0, pack.input_count - len(pack.kept)),
        "evidence_pack_input_chars": pack.input_estimated_chars,
        "evidence_pack_kept_chars": pack.estimated_chars,
        "evidence_pack_overlap_removed_chars": pack.overlap_removed_chars,
        "evidence_pack_drop_reason_counts": drop_reason_counts,
        "evidence_pack_anchor_count": sum(
            "anchor" in item.provenance for item in pack.kept
        ),
        "evidence_pack_pinned_count": len(kept_ids & pinned_chunk_ids),
        "evidence_pack_over_budget": pack.over_budget_hard_constraints,
    }


def _retrieval_top_k(retry_count: int) -> int:
    settings = get_settings()
    base = settings.qa_retrieval_top_k
    if retry_count <= 0:
        return base
    scaled = math.ceil(
        base * (settings.qa_adaptive_retrieval_top_k_multiplier**retry_count)
    )
    return min(scaled, max(base, settings.qa_adaptive_retrieval_max_top_k))


def _evidence_requirement_ids(state: Mapping[str, Any]) -> list[str]:
    return [
        str(requirement.get("requirement_id") or "")
        for requirement in list(state.get("evidence_requirements") or [])
        if isinstance(requirement, dict) and requirement.get("requirement_id")
    ]


def _verified_requirement_ids_by_chunk(
    state: Mapping[str, Any],
) -> dict[str, set[str]]:
    matched_by_chunk: dict[str, set[str]] = {}
    for assessment in list(state.get("evidence_requirement_assessments") or []):
        if not isinstance(assessment, Mapping):
            continue
        if str(assessment.get("verdict") or "") != "supported":
            continue
        requirement_id = str(assessment.get("requirement_id") or "")
        if not requirement_id:
            continue
        for chunk_id in list(assessment.get("evidence_chunk_ids") or []):
            normalized = str(chunk_id or "")
            if normalized:
                matched_by_chunk.setdefault(normalized, set()).add(requirement_id)
    return matched_by_chunk


def _with_verified_requirement_ids(
    doc: RetrievedDoc, requirement_ids: set[str]
) -> RetrievedDoc:
    copied = copy.deepcopy(doc)
    if not requirement_ids:
        return copied
    retrieval = copied.setdefault("retrieval", {})
    existing = retrieval.get("matched_requirement_ids")
    existing_items = existing if isinstance(existing, list) else []
    normalized = {str(item) for item in existing_items if item is not None}
    retrieval["matched_requirement_ids"] = sorted(normalized | requirement_ids)
    return copied


def _carry_verified_docs(
    state: GraphState, current_docs: list[RetrievedDoc]
) -> tuple[list[RetrievedDoc], int]:
    if not state.get("retrieval_retry_count"):
        return current_docs, 0
    verified_ids = {
        str(chunk_id)
        for chunk_id in state.get("evidence_verified_chunk_ids", [])
        if str(chunk_id)
    }
    if not verified_ids:
        return current_docs, 0

    requirement_ids_by_chunk = _verified_requirement_ids_by_chunk(state)
    seen_ids = {str(doc.get("meta", {}).get("chunk_id") or "") for doc in current_docs}
    carryover: list[RetrievedDoc] = []
    for doc in state.get("verification_docs", []):
        chunk_id = str(doc.get("meta", {}).get("chunk_id") or "")
        if chunk_id not in verified_ids or chunk_id in seen_ids:
            continue
        seen_ids.add(chunk_id)
        carryover.append(
            _with_verified_requirement_ids(
                doc, requirement_ids_by_chunk.get(chunk_id, set())
            )
        )
    annotated_current = [
        _with_verified_requirement_ids(
            doc,
            requirement_ids_by_chunk.get(
                str(doc.get("meta", {}).get("chunk_id") or ""), set()
            ),
        )
        if str(doc.get("meta", {}).get("chunk_id") or "") in verified_ids
        else doc
        for doc in current_docs
    ]
    return carryover + annotated_current, len(carryover)


# 检索节点。
def retrieve_node(
    state: GraphState,
    config: RunnableConfig | None = None,
) -> dict:
    original_query = state.get("query", "")
    doc_id = state.get("doc_id", "default")

    settings = get_settings()
    retry_count = max(0, int(state.get("retrieval_retry_count", 0) or 0))
    retrieval_top_k = _retrieval_top_k(retry_count)
    queries = build_retrieval_queries(
        original_query,
        rewritten_queries=state.get("rewritten_queries", []),
        evidence_requirements=state.get("evidence_requirements", []),
        prioritized_requirement_ids=state.get("missing_evidence_requirement_ids", []),
        max_queries=settings.qa_retrieval_max_queries,
    )
    configurable = (config or {}).get("configurable", {})
    runtime = configurable.get("state_runtime")
    retrieval_scope = configurable.get("retrieval_scope")
    if runtime is None:
        from cogdoc.state_runtime import default_state_runtime

        runtime = default_state_runtime()
    with kb_read_lease(doc_id):
        engine = RetrieverFactory.get_engine(doc_id)
        retrieval_result = retrieve_candidate_pool(
            kb_id=doc_id,
            original_query=original_query,
            queries=queries,
            top_k=retrieval_top_k,
            engine=engine,
            derived_knowledge_retriever=runtime.derived_knowledge_retriever,
            retrieval_feedback_store=runtime.retrieval_feedback_store,
            rrf_k=float(settings.hybrid_rrf_k),
            retrieval_round=retry_count,
            scope=retrieval_scope,
        )
    retrieved_docs, carryover_count = _carry_verified_docs(state, retrieval_result.docs)
    raw_requirements = [
        requirement
        for requirement in list(state.get("evidence_requirements") or [])
        if isinstance(requirement, Mapping)
    ]
    evidence_units = (
        build_qa_evidence_units(
            original_query,
            raw_requirements,
            max_retrieval_retries=settings.qa_adaptive_retrieval_max_retries,
        )
        if raw_requirements
        else ()
    )
    unit_id_by_requirement = {
        unit.binding.requirement_id: unit.unit_id for unit in evidence_units
    }
    attributed_docs: list[RetrievedDoc] = []
    for doc in retrieved_docs:
        snapshot = copy.deepcopy(doc)
        retrieval = snapshot.setdefault("retrieval", {})
        matched_requirements = retrieval.get("matched_requirement_ids")
        if isinstance(matched_requirements, list):
            retrieval["matched_unit_ids"] = [
                unit_id_by_requirement[requirement_id]
                for requirement_id in matched_requirements
                if requirement_id in unit_id_by_requirement
            ]
        attributed_docs.append(snapshot)
    retrieved_docs = attributed_docs

    if retrieval_result.feedback_error:
        log_event(
            "qa",
            "retrieval_feedback_boost_failed",
            state,
            level=logging.WARNING,
            kb_id=doc_id,
            error_class=retrieval_result.feedback_error,
        )

    log_event(
        "qa",
        "qa_retrieve",
        state,
        query_count=len(retrieval_result.queries),
        ranking_count=retrieval_result.ranking_count,
        channel_counts=retrieval_result.channel_counts,
        retrieved_count=len(retrieved_docs),
        verified_carryover_count=carryover_count,
        retrieval_top_k=retrieval_top_k,
        retrieval_round=retry_count,
    )
    return {
        "evidence_units": [evidence_unit_plan_state(unit) for unit in evidence_units],
        "retrieved_docs": retrieved_docs,
        "retrieval_round": retry_count,
        "retrieval_top_k_used": retrieval_top_k,
        "retrieval_query_count": len(retrieval_result.queries),
        "retrieval_ranking_count": retrieval_result.ranking_count,
        "retrieval_channel_counts": retrieval_result.channel_counts,
        "retrieval_carryover_count": carryover_count,
        "retrieval_feedback_error": retrieval_result.feedback_error,
    }


# 重排节点。
def rerank_node(state: GraphState) -> dict:
    query = state.get("query", "")
    docs = state.get("retrieved_docs", [])
    doc_id = state.get("doc_id", "default")
    settings = get_settings()
    raw_requirements = state.get("evidence_requirements", [])
    evidence_requirements = [
        requirement
        for requirement in raw_requirements
        if isinstance(requirement, Mapping)
    ]
    requirement_queries = requirement_query_map(evidence_requirements)

    max_candidates = max(settings.qa_rerank_max_candidates, settings.qa_rerank_top_n)
    requirement_ids = _evidence_requirement_ids(state)
    anchor_top_n = dynamic_rerank_top_n(
        base_top_n=settings.qa_rerank_top_n,
        max_docs=settings.qa_evidence_pack_max_docs,
        requirement_count=len(requirement_ids),
        docs_per_requirement=settings.qa_rerank_docs_per_requirement,
    )
    candidate_docs = (
        select_rerank_candidates(
            docs,
            max_candidates=max_candidates,
            requirement_ids=requirement_ids,
            per_channel=getattr(settings, "qa_rerank_docs_per_route", 1),
        )
        if max_candidates > 0
        else docs
    )
    rerank_execution = rerank_with_requirement_policy(
        query=query,
        docs=candidate_docs,
        requirement_queries=requirement_queries,
        top_n=len(candidate_docs),
        allow_cpu=settings.qa_rerank_on_cpu,
        per_requirement=settings.qa_rerank_docs_per_requirement,
    )
    ranked_candidates = rerank_execution.docs
    target_device = rerank_execution.device
    rerank_skipped_reason = rerank_execution.skipped_reason
    reranked_docs = ranked_candidates[:anchor_top_n]
    # 上下文扩展不改变一阶段支持度判断；它只补齐 verifier 与生成所见闭集。
    expanded_docs = _expand_with_neighbor_chunks(doc_id, reranked_docs, state)
    parent_context_expanded_count = sum(
        doc.get("retrieval", {}).get("context_expansion") == "section"
        for doc in expanded_docs
    )
    neighbor_context_expanded_count = sum(
        doc.get("retrieval", {}).get("context_expansion") == "neighbor"
        for doc in expanded_docs
    )
    pinned_ids = set(state.get("evidence_verified_chunk_ids", []))
    evidence_pack, span_metrics = _build_qa_evidence_pack(
        query=query,
        evidence_requirements=evidence_requirements,
        anchors=reranked_docs,
        expanded_docs=expanded_docs,
        ranked_candidates=ranked_candidates,
        pinned_chunk_ids=pinned_ids,
        requirement_ids=requirement_ids,
        settings=settings,
    )
    packed_docs, evidence_ledger = assign_evidence_ids(evidence_pack.kept_docs)
    pack_metrics = _evidence_pack_metrics(
        evidence_pack,
        pinned_chunk_ids=pinned_ids,
    )
    raw_verification_docs = (
        []
        if evidence_pack.over_budget_hard_constraints
        else _verification_docs_from_pack(
            evidence_pack,
            max_docs=settings.qa_evidence_verify_max_docs,
            requirement_ids=requirement_ids,
            pinned_chunk_ids=pinned_ids,
        )
    )
    packed_by_chunk_id = {
        str(doc.get("meta", {}).get("chunk_id") or ""): doc for doc in packed_docs
    }
    verification_docs = [
        packed_by_chunk_id.get(str(doc.get("meta", {}).get("chunk_id") or ""), doc)
        for doc in raw_verification_docs
    ]
    support = assess_retrieval_support(
        reranked_docs, settings, requirement_ids=requirement_ids
    )
    retrieval_abstained = (
        not support.supported or evidence_pack.over_budget_hard_constraints
    )
    retrieval_abstain_reason = (
        "evidence_pack_hard_budget_exceeded"
        if evidence_pack.over_budget_hard_constraints
        else support.reason
    )
    decision_state = {
        **state,
        "retrieval_first_stage_supported": support.supported,
        "retrieval_confidence": support.score,
        "retrieval_abstained": retrieval_abstained,
        "retrieval_abstain_reason": retrieval_abstain_reason,
        **span_metrics,
        **pack_metrics,
    }
    verification_pending = bool(
        not evidence_pack.over_budget_hard_constraints
        and should_verify_evidence(decision_state, settings)
    )
    retry_pending = bool(
        not support.supported
        and not verification_pending
        and _can_retry_retrieval(decision_state)
    )
    # 下游沿用重排结果字段名，实际内容已包含有界结构/邻块上下文。
    log_event(
        "qa",
        "qa_rerank",
        state,
        candidate_count=len(docs),
        rerank_candidate_count=len(candidate_docs),
        reranked_count=len(reranked_docs),
        verification_candidate_count=len(verification_docs),
        expanded_count=len(expanded_docs),
        parent_context_expanded_count=parent_context_expanded_count,
        neighbor_context_expanded_count=neighbor_context_expanded_count,
        device=target_device,
        rerank_skipped_reason=rerank_skipped_reason,
        retrieval_confidence=round(support.score, 6),
        retrieval_abstained=retrieval_abstained,
        retrieval_abstain_reason=retrieval_abstain_reason,
        evidence_verification_pending=verification_pending,
        adaptive_retrieval_retry_pending=retry_pending,
        retrieval_round=state.get("retrieval_round", 0),
        requirement_count=len(requirement_ids),
        rerank_anchor_limit=anchor_top_n,
        **span_metrics,
        **pack_metrics,
    )
    result = {
        "reranked_docs": packed_docs,
        "evidence_ledger": evidence_ledger,
        "citation_ledger": [],
        "verification_docs": verification_docs,
        "retrieval_first_stage_supported": support.supported,
        "retrieval_confidence": support.score,
        "retrieval_abstained": retrieval_abstained,
        "retrieval_abstain_reason": retrieval_abstain_reason,
        "retrieval_signals": support.signals,
        "evidence_verification_pending": verification_pending,
        "adaptive_retrieval_retry_pending": retry_pending,
        "parent_context_expanded_count": parent_context_expanded_count,
        "neighbor_context_expanded_count": neighbor_context_expanded_count,
        **span_metrics,
        **pack_metrics,
    }
    # 补检索终轮若因置信度过低不再进入 verifier，不能把上一轮逐需求结论
    # 冒充为当前候选集的终态结论；缺失 ID 仍保留用于解释拒答与追踪。
    if state.get("retrieval_retry_count", 0) and not verification_pending:
        result.update(
            {
                "evidence_verification_required": False,
                "evidence_supported": False,
                "evidence_verification_reason": "",
                "evidence_verified_chunk_ids": [],
                "evidence_requirement_assessments": [],
            }
        )
    return result


# 对精确事实问题执行结构化证据充分性校验。
def evidence_verify_node(state: GraphState) -> dict:
    output = EvidenceVerifierAgent.verify(state)
    retry_state: dict[str, Any] = dict(state)
    retry_state.update(output)
    output["adaptive_retrieval_retry_pending"] = bool(
        not output.get("evidence_supported") and _can_retry_retrieval(retry_state)
    )
    log_event(
        "qa",
        "qa_evidence_verify",
        state,
        evidence_supported=output.get("evidence_supported", False),
        evidence_verified_chunk_count=len(
            output.get("evidence_verified_chunk_ids", [])
        ),
        generation_evidence_count=len(state.get("reranked_docs", [])),
        evidence_verification_reason=output.get("evidence_verification_reason", ""),
        evidence_verifier_error=output.get("evidence_verifier_error", ""),
    )
    return output


# 证据不足时不调用 LLM，返回稳定拒答并清空候选，避免无关证据进入引用和会话记忆。
def abstain_node(state: GraphState) -> dict:
    log_event(
        "qa",
        "qa_retrieval_abstained",
        state,
        retrieval_confidence=round(state.get("retrieval_confidence", 0.0), 6),
        retrieval_abstain_reason=state.get("retrieval_abstain_reason", ""),
        evidence_verification_reason=state.get("evidence_verification_reason", ""),
    )
    return {
        "messages": [AIMessage(content=NO_RELEVANT_CONTENT_ANSWER)],
        "answer": NO_RELEVANT_CONTENT_ANSWER,
        "sources": [],
        "evidence": [],
        "reranked_docs": [],
        "evidence_ledger": [],
        "citation_ledger": [],
        "critique": "",
        "retrieval_abstained": True,
        "adaptive_retrieval_retry_pending": False,
    }


# 根据检索置信度选择生成或确定性拒答。
def retrieval_check(state: GraphState) -> str:
    if state.get("evidence_pack_over_budget", False) or (
        state.get("retrieval_abstain_reason") == "evidence_pack_hard_budget_exceeded"
    ):
        return "abstain_node"
    if should_verify_evidence(state, get_settings()):
        return "evidence_verify_node"
    if state.get("retrieval_abstained", False) and _can_retry_retrieval(state):
        return "retrieval_retry_node"
    return (
        "abstain_node" if state.get("retrieval_abstained", False) else "generate_node"
    )


# 根据二阶段证据结论选择生成或拒答。
def evidence_check(state: GraphState) -> str:
    if state.get("evidence_supported", False):
        return "generate_node"
    if _can_retry_retrieval(state):
        return "retrieval_retry_node"
    return "abstain_node"


def _can_retry_retrieval(state: Mapping[str, Any]) -> bool:
    settings = get_settings()
    if not settings.qa_adaptive_retrieval_enabled:
        return False
    # Retrying retrieval cannot make configured hard anchor/pinned budgets larger.
    if state.get("retrieval_abstain_reason") == "evidence_pack_hard_budget_exceeded":
        return False
    retry_count = max(0, int(state.get("retrieval_retry_count", 0) or 0))
    if retry_count >= settings.qa_adaptive_retrieval_max_retries:
        return False
    if state.get("evidence_verifier_error"):
        return False
    requirement_ids = _evidence_requirement_ids(state)
    missing_ids = {
        str(item)
        for item in list(state.get("missing_evidence_requirement_ids") or [])
        if str(item)
    }
    return bool(missing_ids or len(requirement_ids) > 1)


# 覆盖不足时只增加一次检索轮次；查询替换、深度扩大和候选预算均在 retrieve_node 内统一执行。
def retrieval_retry_node(state: GraphState) -> dict:
    retry_count = max(0, int(state.get("retrieval_retry_count", 0) or 0)) + 1
    missing_ids = [
        str(item)
        for item in list(state.get("missing_evidence_requirement_ids") or [])
        if str(item)
    ]
    if not missing_ids:
        missing_ids = _evidence_requirement_ids(state)
    reason = (
        "missing_requirements"
        if state.get("evidence_verification_required")
        else str(state.get("retrieval_abstain_reason") or "coverage_incomplete")
    )
    log_event(
        "qa",
        "qa_adaptive_retrieval_retry",
        state,
        retry_count=retry_count,
        retry_reason=reason,
        missing_requirement_ids=missing_ids,
        next_top_k=_retrieval_top_k(retry_count),
    )
    return {
        "retrieval_retry_count": retry_count,
        "retrieval_round": retry_count,
        "retrieval_retry_reason": reason,
        "missing_evidence_requirement_ids": missing_ids,
        "evidence_verification_pending": False,
        "evidence_verification_required": False,
        "evidence_supported": False,
        "adaptive_retrieval_retry_pending": False,
    }


def _generation_evidence(doc: RetrievedDoc) -> Evidence:
    meta = doc.get("meta", {})
    page = meta.get("page", 0)
    evidence: Evidence = {
        "evidence_id": str(doc.get("retrieval", {}).get("evidence_id") or ""),
        "chunk_id": str(meta.get("chunk_id") or ""),
        "source_type": str(meta.get("source_type") or "document"),
        "knowledge_id": str(meta.get("knowledge_id") or ""),
        "chunk_index": cast(int, meta.get("chunk_index", -1)),
        "source": str(meta.get("source") or ""),
        "source_id": str(meta.get("source_id") or ""),
        "source_version_id": str(meta.get("source_version_id") or ""),
        "media_type": str(meta.get("media_type") or ""),
        "location": dict(meta.get("source_location") or {})
        if isinstance(meta.get("source_location"), Mapping)
        else {},
        "page": cast(int, page),
        "page_start": cast(int, meta.get("page_start", page)),
        "page_end": cast(int, meta.get("page_end", page)),
        "rerank_score": doc.get("retrieval", {}).get("rerank_score"),
        "rewrite_query": doc.get("retrieval", {}).get("rewrite_query"),
        "text_preview": doc["text"][:100],
        "retrieval": safe_retrieval_metadata(doc.get("retrieval")),
    }
    if meta.get("parent_chunk_id"):
        evidence["parent_chunk_id"] = str(meta["parent_chunk_id"])
    if meta.get("section_title"):
        evidence["section_title"] = str(meta["section_title"])
    if meta.get("section_path"):
        evidence["section_path"] = str(meta["section_path"])
    if meta.get("section_level") is not None:
        evidence["section_level"] = cast(int, meta["section_level"])
    if meta.get("child_index_in_parent") is not None:
        evidence["child_index_in_parent"] = cast(int, meta["child_index_in_parent"])
    return evidence


def _qa_generated_obligation_ids(state: Mapping[str, Any]) -> tuple[str, ...]:
    generated_ids = state.get("evidence_unit_generate_ids")
    if isinstance(generated_ids, Sequence) and not isinstance(
        generated_ids, (str, bytes, bytearray)
    ):
        return tuple(
            dict.fromkeys(
                unit_id
                for value in generated_ids
                if (unit_id := str(value or "").strip())
            )
        )

    assessments = state.get("evidence_unit_assessments")
    plans = state.get("evidence_units")
    if not isinstance(assessments, Sequence) or isinstance(
        assessments, (str, bytes, bytearray)
    ):
        return ()
    if not isinstance(plans, Sequence) or isinstance(plans, (str, bytes, bytearray)):
        return ()
    supported_ids = {
        str(item.get("unit_id") or "").strip()
        for item in assessments
        if isinstance(item, Mapping) and item.get("status") == "supported"
    }
    return tuple(
        unit_id
        for item in plans
        if isinstance(item, Mapping)
        and bool(item.get("required", True))
        and (unit_id := str(item.get("unit_id") or "").strip()) in supported_ids
    )


# 生成节点。
def generate_node(state: GraphState) -> dict:
    query = state.get("query", "")
    is_local = state.get("is_local", False)
    final_docs = state.get("reranked_docs", [])
    chat_history = state.get("chat_history", [])

    critique = state.get("critique", "")
    iteration_count = state.get("iteration_count", 0)

    llm = Generator._get_client_for_node("qa_generator", is_local=is_local)
    base_prompt = Generator.format_prompt(
        query=query, docs=final_docs, chat_history=chat_history
    )

    messages_payload = list(base_prompt)
    if critique and iteration_count > 0:
        correction_note = CITATION_CORRECTION_PROMPT_TEMPLATE.format(critique=critique)
        if messages_payload and isinstance(messages_payload[0], SystemMessage):
            messages_payload[0] = SystemMessage(
                content=cast(str, messages_payload[0].content) + correction_note
            )
        else:
            messages_payload.insert(0, SystemMessage(content=correction_note))

    response_message = llm.invoke(messages_payload)
    answer = str(response_message.content).strip()
    generation_error = ""
    if not answer:
        answer = QA_GENERATION_FAILURE_ANSWER
        response_message = AIMessage(content=answer)
        generation_error = "qa_generation_empty"

    # EID 只在父图声明审计通过后确定性渲染成用户可见页码引用。
    evidence = [_generation_evidence(doc) for doc in final_docs]
    obligation_ids = _qa_generated_obligation_ids(state)
    segment = (
        ClaimAuditProjectionSegment.operational(
            "qa:generation_error",
            answer,
            source_status=generation_error,
            obligation_ids=obligation_ids,
        )
        if generation_error
        else ClaimAuditProjectionSegment.generated(
            "qa:answer",
            answer,
            source_status="generated",
            obligation_ids=obligation_ids,
        )
    )
    projection = build_claim_audit_projection(answer, (segment,))

    output: dict[str, Any] = {
        "messages": [response_message],
        "answer": answer,
        "sources": [doc["meta"] for doc in final_docs],
        "evidence": evidence,
        "claim_audit_projection": projection.to_state(),
    }
    if generation_error:
        output["error"] = generation_error
    return output


# 处理引用节点。
def citation_node(state: GraphState) -> dict:
    answer = state.get("answer", "")
    final_docs = state.get("reranked_docs", [])
    iteration_count = state.get("iteration_count", 0)
    max_iteration_count = state.get("max_iteration_count", 2)

    check_res = CitationValidatorAgent.validate_evidence_citations(
        answer, state.get("evidence_ledger", [])
    )
    log_event(
        "qa",
        "qa_citation_check",
        state,
        is_valid=not bool(check_res["critique"]),
        iteration_count=iteration_count + 1,
        evidence_count=len(final_docs),
    )

    return {
        "critique": check_res["critique"],
        "iteration_count": iteration_count + 1,
        "max_iteration_count": max_iteration_count,
    }


# 处理引用检查。
def citation_check(state: GraphState) -> str:
    critique = state.get("critique", "")
    iteration_count = state.get("iteration_count", 0)
    max_iteration_count = state.get("max_iteration_count", 2)

    if not critique:
        return END

    if iteration_count <= max_iteration_count:
        return "generate_node"

    return END


sub_graph = StateGraph(GraphState)

sub_graph.add_node("rewrite_node", rewrite_node)
sub_graph.add_node("verify_rewrite_node", verify_rewrite_node)
sub_graph.add_node("retrieve_node", retrieve_node)
sub_graph.add_node("rerank_node", rerank_node)
sub_graph.add_node("evidence_verify_node", evidence_verify_node)
sub_graph.add_node("retrieval_retry_node", retrieval_retry_node)
sub_graph.add_node("abstain_node", abstain_node)
sub_graph.add_node("generate_node", generate_node)
sub_graph.add_node("citation_node", citation_node)

sub_graph.add_edge(START, "rewrite_node")
sub_graph.add_edge("rewrite_node", "verify_rewrite_node")
sub_graph.add_edge("verify_rewrite_node", "retrieve_node")
sub_graph.add_edge("retrieve_node", "rerank_node")
sub_graph.add_conditional_edges(
    "rerank_node",
    retrieval_check,
    {
        "evidence_verify_node": "evidence_verify_node",
        "retrieval_retry_node": "retrieval_retry_node",
        "abstain_node": "abstain_node",
        "generate_node": "generate_node",
    },
)
sub_graph.add_conditional_edges(
    "evidence_verify_node",
    evidence_check,
    {
        "retrieval_retry_node": "retrieval_retry_node",
        "abstain_node": "abstain_node",
        "generate_node": "generate_node",
    },
)
sub_graph.add_edge("retrieval_retry_node", "retrieve_node")
sub_graph.add_edge("abstain_node", END)
sub_graph.add_edge("generate_node", "citation_node")

sub_graph.add_conditional_edges(
    "citation_node", citation_check, {"generate_node": "generate_node", END: END}
)

qa_subgraph_node = sub_graph.compile()
