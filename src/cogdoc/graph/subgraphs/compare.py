from typing import Any, cast

from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import StateGraph, START, END
from cogdoc.config.settings import get_settings
from cogdoc.agents.claim_evidence_verifier import (
    CLAIM_AUDIT_EXEMPTION_GUIDANCE,
    CLAIM_AUDIT_EXEMPTION_UPSTREAM_ERROR,
    make_claim_audit_exemption,
)
from cogdoc.agents.compare_generator import CompareGeneratorAgent
from cogdoc.agents.compare_profile import (
    DocumentProfileAgent,
    default_compare_dimensions,
)
from cogdoc.agents.source_resolver import resolve_compare_sources
from cogdoc.graph.state import GraphState
from cogdoc.observability.logger import log_event
from cogdoc.service.evidence_unit_pipeline import EvidenceUnitPipelinePolicy
from cogdoc.service.evidence_unit_workflow import retrieve_verified_evidence_units
from cogdoc.service.evidence_units import (
    EvidenceUnitBudget,
    build_compare_evidence_units,
)
from cogdoc.service.kb_readers import kb_read_lease
from cogdoc.service.retriever_factory import RetrieverFactory
from cogdoc.tools.document_loader import select_sources_for_compare


LOCAL_COMPARE_MAX_SOURCES = 2


# 完成 documentloadernode 处理。
def document_loader_node(state: GraphState) -> dict:
    # Compare MVP 只处理用户显式点名的多文档对比。
    query = state.get("query", "")
    doc_id = state.get("doc_id", "default")
    is_local = state.get("is_local", False)
    with kb_read_lease(doc_id):
        engine = RetrieverFactory.get_engine(doc_id)
        sources = engine.list_sources()
    retrieval_scope = state.get("retrieval_scope")
    if retrieval_scope is not None:
        sources = [source for source in sources if retrieval_scope.allows_source(source)]
    selected_sources = select_sources_for_compare(query, sources)

    resolution_trace = []
    if len(selected_sources) < 2 and sources and state.get("chat_history"):
        # 字面匹配不足时，用近期对话消解“这个文件/上面那篇”等多轮指代。
        resolved = resolve_compare_sources(
            query, sources, state.get("chat_history"), is_local
        )
        if len(resolved) >= 2:
            selected_sources = resolved
            resolution_trace = [
                {
                    "step_name": "compare_source_resolution",
                    "input_summary": query,
                    "output_summary": "，".join(resolved),
                }
            ]

    if len(selected_sources) < 2:
        source_list = "，".join(sources) if sources else "当前知识库没有可用文档"
        message = (
            "请在对比问题中点名至少 2 篇要对比的文件（可直接说出文件名）。"
            f"当前可用文档：{source_list}"
        )
        result = {
            "compare_sources": [],
            "compare_docs_by_source": {},
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
            "compare",
            "compare_document_loader",
            state,
            selected_count=len(selected_sources),
            source_count=len(sources),
        )
        return result

    if is_local and len(selected_sources) > LOCAL_COMPARE_MAX_SOURCES:
        # 本地模式限制文档数，避免文档数乘维度数放大 LLM 调用。
        selected_list = "，".join(selected_sources)
        message = (
            f"本地 Ollama 模式最多支持同时对比 {LOCAL_COMPARE_MAX_SOURCES} 篇文档，"
            f"当前点名了 {len(selected_sources)} 篇：{selected_list}。"
            "请只保留 2 篇文档后重试，或切换到云端 API 模式。"
        )
        result = {
            "compare_sources": [],
            "compare_docs_by_source": {},
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
            "compare",
            "compare_document_loader",
            state,
            selected_count=len(selected_sources),
            local_limit=LOCAL_COMPARE_MAX_SOURCES,
            limited=True,
        )
        return result

    docs_by_source = {}
    with kb_read_lease(doc_id):
        engine = RetrieverFactory.get_engine(doc_id)
        for source in selected_sources:
            docs = engine.load_source_chunks(source)
            if retrieval_scope is not None:
                docs = [doc for doc in docs if retrieval_scope.allows_document(doc)]
            if docs:
                docs_by_source[source] = docs

    if len(docs_by_source) < 2:
        loaded_sources = "，".join(docs_by_source.keys()) if docs_by_source else "无"
        message = (
            "未能从当前索引加载至少 2 篇对比文档。"
            f"已加载：{loaded_sources}。请重建索引后再试。"
        )
        result = {
            "compare_sources": list(docs_by_source.keys()),
            "compare_docs_by_source": docs_by_source,
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
            "compare",
            "compare_document_loader",
            state,
            selected_count=len(selected_sources),
            loaded_count=len(docs_by_source),
        )
        return result

    compare_sources = list(docs_by_source.keys())
    result = {
        "compare_sources": compare_sources,
        # Full source corpora are temporary fallback inputs. The next node
        # replaces them with the exact source-scoped generation closures.
        "compare_docs_by_source": docs_by_source,
        "citation_ledger": [],
        "compare_dimensions": state.get("compare_dimensions")
        or default_compare_dimensions(is_local=is_local),
        "steps_trace": resolution_trace
        + [
            {
                "step_name": "compare_document_loader",
                "input_summary": query,
                "output_summary": "，".join(
                    f"{source}: {len(docs_by_source[source])} chunks"
                    for source in compare_sources
                ),
            }
        ],
    }
    log_event(
        "compare",
        "compare_document_loader",
        state,
        selected_count=len(compare_sources),
        loaded_count=len(docs_by_source),
        chunk_count=sum(len(docs) for docs in docs_by_source.values()),
    )
    return result


def cell_evidence_node(
    state: GraphState,
    config: RunnableConfig | None = None,
) -> dict:
    """Retrieve one isolated evidence closure per source×dimension cell."""

    query = state.get("query", "")
    sources = list(state.get("compare_sources") or [])
    dimensions = list(state.get("compare_dimensions") or [])
    full_docs_by_source = dict(state.get("compare_docs_by_source") or {})
    if len(sources) < 2 or not dimensions:
        return {
            "evidence_units": [],
            "evidence_unit_results": [],
            "evidence_unit_metrics": {},
            "compare_docs_by_source": {},
            "evidence_ledger": [],
        }

    units = build_compare_evidence_units(query, sources, dimensions)
    is_local = bool(state.get("is_local", False))
    max_docs_per_unit = 2 if is_local else 4
    max_chars_per_unit = 1800 if is_local else 3200
    budget = EvidenceUnitBudget(
        max_total_docs=max(
            max_docs_per_unit,
            min(len(units) * max_docs_per_unit, 16 if is_local else 32),
        ),
        max_total_chars=max(
            max_chars_per_unit,
            min(len(units) * max_chars_per_unit, 14000 if is_local else 26000),
        ),
        max_docs_per_unit=max_docs_per_unit,
        max_chars_per_unit=max_chars_per_unit,
    ).reserve_plan_capacity(units)
    policy = EvidenceUnitPipelinePolicy(
        rerank_top_n=min(3, max_docs_per_unit),
        evidence_span_max_chars_per_doc=320 if is_local else 420,
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
            fallback_docs_by_source=full_docs_by_source,
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
        "compare",
        "compare_evidence_units",
        state,
        **metrics,
    )
    return {
        **batch.to_state(),
        "compare_docs_by_source": batch.grounded_docs_by_source,
        "citation_ledger": [],
        "steps_trace": [
            {
                "step_name": "compare_evidence_units",
                "input_summary": (
                    f"{len(sources)} sources x {len(dimensions)} dimensions"
                ),
                "output_summary": (
                    f"ready={metrics['ready_count']} "
                    f"no_evidence={metrics['no_evidence_count']} "
                    "failed="
                    f"{metrics['retrieval_error_count'] + metrics['verification_error_count'] + metrics['budget_exhausted_count']}"
                ),
            }
        ],
    }


# 完成 document画像node 处理。
def document_profile_node(state: GraphState) -> dict:
    try:
        return DocumentProfileAgent.build_profiles(cast(dict[str, Any], state))
    except Exception as exc:
        # 模型异常转为可打印答案，避免流式管道中断。
        if state.get("is_local", False):
            suggestion = "建议：释放内存或执行 `ollama stop <model>` 后重试；也可以切换到更小的本地模型或云端 API。"
        else:
            suggestion = "建议：稍后重试，或检查云端 API 配置与网络状态。"
        message = (
            f"模型生成对比画像失败。错误：{type(exc).__name__}: {exc}\n{suggestion}"
        )
        result = {
            "document_profiles": [],
            "answer": message,
            "messages": [AIMessage(content=message)],
            "error": str(exc),
            "claim_audit_exemption": make_claim_audit_exemption(
                message,
                CLAIM_AUDIT_EXEMPTION_UPSTREAM_ERROR,
            ),
            "steps_trace": [
                {
                    "step_name": "compare_document_profile_error",
                    "input_summary": state.get("query", ""),
                    "output_summary": message,
                }
            ],
        }
        log_event(
            "compare",
            "compare_document_profile_error",
            state,
            error_class=type(exc).__name__,
        )
        return result


# 生成对比 table node。
def compare_table_node(state: GraphState) -> dict:
    return CompareGeneratorAgent.build_compare_answer(cast(dict[str, Any], state))


# 完成 引用node 处理。
def citation_node(state: GraphState) -> dict:
    return CompareGeneratorAgent.validate_compare_answer(cast(dict[str, Any], state))


# 完成 documentloadercheck 处理。
def document_loader_check(state: GraphState) -> str:
    if len(state.get("compare_docs_by_source", {})) < 2:
        return END
    return "document_profile_node"


# 完成 document画像check 处理。
def document_profile_check(state: GraphState) -> str:
    if not state.get("document_profiles"):
        return END
    return "compare_table_node"


compare_graph = StateGraph(GraphState)

compare_graph.add_node("document_loader_node", document_loader_node)
compare_graph.add_node("cell_evidence_node", cell_evidence_node)
compare_graph.add_node("document_profile_node", document_profile_node)
compare_graph.add_node("compare_table_node", compare_table_node)
# 避免 run.py 将 compare 校验误识别为 QA citation 节点。
compare_graph.add_node("compare_citation_node", citation_node)

compare_graph.add_edge(START, "document_loader_node")
compare_graph.add_conditional_edges(
    "document_loader_node",
    document_loader_check,
    {
        "document_profile_node": "cell_evidence_node",
        END: END,
    },
)
compare_graph.add_edge("cell_evidence_node", "document_profile_node")
compare_graph.add_conditional_edges(
    "document_profile_node",
    document_profile_check,
    {
        "compare_table_node": "compare_table_node",
        END: END,
    },
)
compare_graph.add_edge("compare_table_node", "compare_citation_node")
compare_graph.add_edge("compare_citation_node", END)

compare_subgraph_node = compare_graph.compile()
