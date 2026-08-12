from typing import Any, cast

from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import StateGraph, START, END
from cogdoc.config.settings import get_settings
from cogdoc.agents.claim_evidence_verifier import (
    CLAIM_AUDIT_EXEMPTION_GUIDANCE,
    make_claim_audit_exemption,
)
from cogdoc.agents.summary_planner import SectionPlannerAgent
from cogdoc.agents.summary_generator import (
    GlobalSummaryAgent,
    SectionSummaryAgent,
    section_context_limit,
)
from cogdoc.agents.source_resolver import resolve_summary_source
from cogdoc.graph.state import GraphState
from cogdoc.observability.logger import log_event
from cogdoc.service.evidence_unit_pipeline import EvidenceUnitPipelinePolicy
from cogdoc.service.evidence_unit_workflow import retrieve_verified_evidence_units
from cogdoc.service.evidence_units import (
    EvidenceUnitBudget,
    build_summary_evidence_units,
)
from cogdoc.service.kb_readers import kb_read_lease
from cogdoc.service.retriever_factory import RetrieverFactory
from cogdoc.tools.document_loader import select_source_for_summary


# 完成 documentloadernode 处理。
def document_loader_node(state: GraphState) -> dict:
    # Summary MVP 从当前索引直接加载单个 source 的全部 chunk。
    query = state.get("query", "")
    doc_id = state.get("doc_id", "default")
    is_local = state.get("is_local", False)
    with kb_read_lease(doc_id):
        engine = RetrieverFactory.get_engine(doc_id)
        sources = engine.list_sources()
    retrieval_scope = state.get("retrieval_scope")
    if retrieval_scope is not None:
        sources = [source for source in sources if retrieval_scope.allows_source(source)]
    selected_source = select_source_for_summary(query, sources)

    resolution_trace = []
    if selected_source is None and sources and state.get("chat_history"):
        # 字面匹配不到时，用近期对话消解“总结这个文件/上面那篇”等多轮指代。
        resolved = resolve_summary_source(
            query, sources, state.get("chat_history"), is_local
        )
        if resolved:
            selected_source = resolved
            resolution_trace = [
                {
                    "step_name": "summary_source_resolution",
                    "input_summary": query,
                    "output_summary": resolved,
                }
            ]

    if selected_source is None:
        source_list = "，".join(sources) if sources else "当前知识库没有可用文档"
        message = (
            "请在摘要问题中明确指定要总结的文件名（可直接说出文件名）。"
            f"当前可用文档：{source_list}"
        )
        result = {
            "summary_source": "",
            "summary_docs": [],
            "evidence_ledger": [],
            "citation_ledger": [],
            "answer": message,
            "messages": [AIMessage(content=message)],
            "claim_audit_exemption": make_claim_audit_exemption(
                message,
                CLAIM_AUDIT_EXEMPTION_GUIDANCE,
            ),
        }
        log_event(
            "summary",
            "summary_document_loader",
            state,
            selected=False,
            source_count=len(sources),
        )
        return result

    with kb_read_lease(doc_id):
        docs = RetrieverFactory.get_engine(doc_id).load_source_chunks(selected_source)
    if retrieval_scope is not None:
        docs = [doc for doc in docs if retrieval_scope.allows_document(doc)]
    if not docs:
        message = f"未能从当前索引加载文档：{selected_source}。请重建索引后再试。"
        result = {
            "summary_source": selected_source,
            "summary_docs": [],
            "evidence_ledger": [],
            "citation_ledger": [],
            "answer": message,
            "messages": [AIMessage(content=message)],
            "claim_audit_exemption": make_claim_audit_exemption(
                message,
                CLAIM_AUDIT_EXEMPTION_GUIDANCE,
            ),
        }
        log_event(
            "summary",
            "summary_document_loader",
            state,
            selected=True,
            loaded=False,
            source=selected_source,
        )
        return result

    result = {
        "summary_source": selected_source,
        # Keep the full source corpus only until section units have selected
        # their closed evidence views. EIDs are frozen exactly once afterwards.
        "summary_docs": docs,
        "citation_ledger": [],
        "steps_trace": resolution_trace
        + [
            {
                "step_name": "summary_document_loader",
                "input_summary": query,
                "output_summary": f"{selected_source}: {len(docs)} chunks",
            }
        ],
    }
    log_event(
        "summary",
        "summary_document_loader",
        state,
        selected=True,
        loaded=True,
        source=selected_source,
        chunk_count=len(docs),
    )
    return result


# 完成 章节规划器node 处理。
def section_planner_node(state: GraphState) -> dict:
    return SectionPlannerAgent.plan_sections(cast(dict[str, Any], state))


def section_evidence_node(
    state: GraphState,
    config: RunnableConfig | None = None,
) -> dict:
    """Retrieve and freeze one source-safe evidence closure per section."""

    query = state.get("query", "")
    source = state.get("summary_source", "")
    plans = list(state.get("summary_section_plans") or [])
    full_docs = list(state.get("summary_docs") or [])
    if not source or not plans:
        return {
            "evidence_units": [],
            "evidence_unit_results": [],
            "evidence_unit_metrics": {},
            "summary_docs": [],
            "evidence_ledger": [],
        }

    units = build_summary_evidence_units(query, source, plans)
    is_local = bool(state.get("is_local", False))
    max_docs_per_unit = section_context_limit(is_local, len(full_docs))
    max_chars_per_unit = 3600 if is_local else 5200
    budget = EvidenceUnitBudget(
        max_total_docs=max(
            max_docs_per_unit,
            min(len(units) * max_docs_per_unit, 16 if is_local else 24),
        ),
        max_total_chars=max(
            max_chars_per_unit,
            min(len(units) * max_chars_per_unit, 14000 if is_local else 24000),
        ),
        max_docs_per_unit=max_docs_per_unit,
        max_chars_per_unit=max_chars_per_unit,
    ).reserve_plan_capacity(units)
    policy = EvidenceUnitPipelinePolicy(
        rerank_top_n=min(3, max_docs_per_unit),
        evidence_span_max_chars_per_doc=360 if is_local else 420,
    )
    configurable = (config or {}).get("configurable", {})
    runtime = configurable.get("state_runtime")
    authorization_scope = configurable.get("retrieval_scope")
    if runtime is None:
        from cogdoc.state_runtime import default_state_runtime

        runtime = default_state_runtime()
    doc_id = state.get("doc_id", "default")
    settings = get_settings()
    with kb_read_lease(doc_id):
        batch = retrieve_verified_evidence_units(
            units,
            kb_id=doc_id,
            original_query=query,
            engine=RetrieverFactory.get_engine(doc_id),
            derived_knowledge_retriever=runtime.derived_knowledge_retriever,
            retrieval_feedback_store=runtime.retrieval_feedback_store,
            budget=budget,
            policy=policy,
            rrf_k=float(settings.hybrid_rrf_k),
            fallback_docs_by_source={source: full_docs},
            verification_enabled=settings.evidence_unit_verify_enabled,
            is_local=is_local,
            max_chars_per_verification_doc=(
                settings.evidence_unit_verify_max_chars_per_doc
            ),
            max_units_per_verification_batch=(
                settings.evidence_unit_verify_max_units_per_batch
            ),
            structured_client=configurable.get("evidence_unit_structured_client"),
            authorization_scope=authorization_scope,
        )

    metrics = batch.metrics
    log_event(
        "summary",
        "summary_evidence_units",
        state,
        **metrics,
    )
    return {
        **batch.to_state(),
        "summary_docs": list(batch.grounded_docs),
        "citation_ledger": [],
        "steps_trace": [
            {
                "step_name": "summary_evidence_units",
                "input_summary": f"{len(units)} sections",
                "output_summary": (
                    f"ready={metrics['ready_count']} "
                    f"no_evidence={metrics['no_evidence_count']} "
                    "failed="
                    f"{metrics['retrieval_error_count'] + metrics['verification_error_count'] + metrics['budget_exhausted_count']}"
                ),
            }
        ],
    }


# 完成 章节摘要node 处理。
def section_summary_node(state: GraphState) -> dict:
    return SectionSummaryAgent.summarize_sections(cast(dict[str, Any], state))


# 完成 global摘要node 处理。
def global_summary_node(state: GraphState) -> dict:
    return GlobalSummaryAgent.build_final_summary(cast(dict[str, Any], state))


# 完成 documentloadercheck 处理。
def document_loader_check(state: GraphState) -> str:
    # 文档加载失败时直接结束，避免下游节点在空 docs 上运行。
    if not state.get("summary_docs"):
        return END
    return "section_planner_node"


summary_graph = StateGraph(GraphState)

summary_graph.add_node("document_loader_node", document_loader_node)
summary_graph.add_node("section_planner_node", section_planner_node)
summary_graph.add_node("section_evidence_node", section_evidence_node)
summary_graph.add_node("section_summary_node", section_summary_node)
summary_graph.add_node("global_summary_node", global_summary_node)

summary_graph.add_edge(START, "document_loader_node")
summary_graph.add_conditional_edges(
    "document_loader_node",
    document_loader_check,
    {
        "section_planner_node": "section_planner_node",
        END: END,
    },
)
summary_graph.add_edge("section_planner_node", "section_evidence_node")
summary_graph.add_edge("section_evidence_node", "section_summary_node")
summary_graph.add_edge("section_summary_node", "global_summary_node")
summary_graph.add_edge("global_summary_node", END)

summary_subgraph_node = summary_graph.compile()
