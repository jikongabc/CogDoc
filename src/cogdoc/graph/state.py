from typing import TypedDict, List, Optional, Annotated, Any, Dict
from typing_extensions import NotRequired
from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages


# 合并图状态中的列表字段。
def merge_lists(old_list: Optional[Any], new_list: Optional[Any]) -> List[Any]:
    # 空列表表示显式清空历史。
    if new_list is not None and len(new_list) == 0:
        return []
    old = list(old_list) if old_list is not None else []
    new = list(new_list) if new_list is not None else []
    return old + new


# chunk_id 是检索链路里的稳定身份键。
class DocMeta(TypedDict):
    chunk_id: str
    document_id: str
    source_sha256: str
    local_chunk_index: int
    chunk_index: int
    source: str
    page: int
    page_start: int
    page_end: int
    score: float
    origin: str
    context: NotRequired[str]
    source_type: NotRequired[str]
    knowledge_id: NotRequired[str]
    parent_chunk_id: NotRequired[str]
    section_title: NotRequired[str]
    section_path: NotRequired[str]
    section_level: NotRequired[int]
    child_index_in_parent: NotRequired[int]


# retrieval 只保存本次检索产生的动态指标。
class RetrievalMetrics(TypedDict, total=False):
    distance: float
    bm25_score: float
    rrf_score: float
    rerank_score: float
    search_channel: str
    rewrite_query: str
    context_anchor_chunk_id: str
    context_expansion: str
    query_fusion_score: float
    query_hit_count: int
    matched_queries: List[str]
    matched_channels: List[str]
    matched_requirement_ids: List[str]
    # 通用 Evidence Unit 命名；迁移期与 matched_requirement_ids 双写。
    matched_unit_ids: List[str]
    best_query_rank: int
    original_query_hit: bool
    retrieval_round: int
    # Evidence Pack 将进入模型的隔离文本视图映射回原 child 正文。
    evidence_text_start: int
    evidence_text_end: int
    evidence_trimmed_overlap_chars: int
    evidence_span_selected: bool
    evidence_span_input_start: int
    evidence_span_input_end: int
    evidence_span_start: int
    evidence_span_end: int
    evidence_span_original_chars: int
    evidence_span_selected_chars: int
    evidence_span_score: float
    evidence_span_matched_terms: List[str]
    evidence_span_matched_requirement_ids: List[str]
    evidence_span_matched_unit_ids: List[str]
    evidence_span_reason: str
    # 本次响应内冻结的精确证据身份；只供模型内部引用与审计。
    evidence_id: str


# RetrievedDoc 是检索、重排和生成节点共享的文档结构。
class RetrievedDoc(TypedDict):
    text: str
    meta: DocMeta
    retrieval: NotRequired[RetrievalMetrics]
    # Pack 内部的可恢复原文视图；模型、API 与 trace formatter 都不得读取。
    _evidence_source_text: NotRequired[str]
    _evidence_source_start: NotRequired[int]
    _evidence_source_end: NotRequired[int]
    _evidence_source_overlap_chars: NotRequired[int]
    _evidence_span_source_text: NotRequired[str]
    _evidence_span_source_start: NotRequired[int]
    _evidence_span_source_end: NotRequired[int]
    _evidence_span_source_overlap_chars: NotRequired[int]


# ChatMessage 保存会话历史中的单条消息。
class ChatMessage(TypedDict):
    role: str
    content: str
    timestamp: Optional[str]


# AgentStepTrace 保存图节点的输入输出摘要。
class AgentStepTrace(TypedDict):
    step_name: str
    input_summary: str
    output_summary: str


# Evidence 面向前端和审计展示。
class Evidence(TypedDict, total=False):
    evidence_id: str
    chunk_id: str
    parent_chunk_id: str
    section_title: str
    section_path: str
    section_level: int
    child_index_in_parent: int
    source_type: str
    knowledge_id: str
    chunk_index: int
    source: str
    page: int
    page_start: int
    page_end: int
    rerank_score: Optional[float]
    rewrite_query: Optional[str]
    text_preview: str
    retrieval: Dict[str, Any]


# EvidenceLedgerEntry 是内部精确引用注册表，不含证据正文。
class EvidenceLedgerEntry(TypedDict, total=False):
    evidence_id: str
    chunk_id: str
    source_type: str
    knowledge_id: str
    source: str
    page: int
    page_start: int
    page_end: int
    span_start: int
    span_end: int
    display_citation: str


class CitationOccurrence(TypedDict):
    index: int
    answer_start: int
    answer_end: int


# CitationLedgerEntry 是可安全返回给 API 的实际引用账本。
class CitationLedgerEntry(TypedDict, total=False):
    evidence_id: str
    chunk_id: str
    source_type: str
    knowledge_id: str
    source: str
    page: int
    page_start: int
    page_end: int
    span_start: int
    span_end: int
    occurrences: List[CitationOccurrence]


# EvidenceRequirementPlan 是问题改写阶段生成、服务端分配稳定标识的原子证据需求。
class EvidenceRequirementPlan(TypedDict):
    requirement_id: str
    question: str
    retrieval_query: str
    recovery_query: str


# EvidenceRequirementAssessment 保存生成前证据校验器的逐需求闭集判断。
class EvidenceRequirementAssessment(TypedDict):
    requirement_id: str
    verdict: str
    evidence_chunk_ids: List[str]
    reason: str


# SummarySectionPlan 定义单文档摘要的固定章节。
class SummarySectionPlan(TypedDict):
    section_id: str
    title: str
    instruction: str


# SummarySectionResult 保存单个章节的带引用摘要。
class SummarySectionResult(TypedDict):
    section_id: str
    title: str
    content: str
    unit_id: NotRequired[str]
    evidence: NotRequired[List[Evidence]]
    status: NotRequired[str]
    failure_stage: NotRequired[str]
    error_class: NotRequired[str]


# CompareDimensionPlan 定义多文档对比的固定维度。
class CompareDimensionPlan(TypedDict):
    dimension_id: str
    title: str
    instruction: str


# CompareCell 保存某篇文档在某个对比维度下的带引用短描述。
class CompareCell(TypedDict):
    dimension_id: str
    source: str
    content: str
    unit_id: NotRequired[str]
    evidence: NotRequired[List[Evidence]]
    status: NotRequired[str]
    failure_stage: NotRequired[str]
    error_class: NotRequired[str]


# DocumentProfile 是 Compare 子图里单篇文档的结构化画像。
class DocumentProfile(TypedDict):
    source: str
    cells: List[CompareCell]


# GraphState 是 LangGraph 节点间传递的全局状态。
class GraphState(TypedDict):
    messages: Annotated[List[BaseMessage], add_messages]
    docs: NotRequired[List[RetrievedDoc]]

    query: NotRequired[str]
    doc_id: NotRequired[str]
    request_id: NotRequired[str]
    trace_id: NotRequired[str]
    is_local: NotRequired[bool]
    task_type: NotRequired[str]
    router_reason: NotRequired[str]
    top_k: NotRequired[int]
    retrieval_scope: NotRequired[Any]

    rewritten_queries: NotRequired[List[str]]
    evidence_requirements: NotRequired[List[EvidenceRequirementPlan]]
    rewrite_similarity_threshold: NotRequired[float]
    retrieved_docs: NotRequired[List[RetrievedDoc]]
    reranked_docs: NotRequired[List[RetrievedDoc]]
    verification_docs: NotRequired[List[RetrievedDoc]]
    retrieval_first_stage_supported: NotRequired[bool]
    retrieval_confidence: NotRequired[float]
    retrieval_abstained: NotRequired[bool]
    retrieval_abstain_reason: NotRequired[str]
    retrieval_signals: NotRequired[Dict[str, float]]
    evidence_verification_pending: NotRequired[bool]
    evidence_verification_required: NotRequired[bool]
    evidence_supported: NotRequired[bool]
    evidence_verification_reason: NotRequired[str]
    evidence_verified_chunk_ids: NotRequired[List[str]]
    evidence_requirement_assessments: NotRequired[List[EvidenceRequirementAssessment]]
    missing_evidence_requirement_ids: NotRequired[List[str]]
    evidence_verifier_error: NotRequired[str]
    retrieval_retry_count: NotRequired[int]
    retrieval_round: NotRequired[int]
    retrieval_top_k_used: NotRequired[int]
    retrieval_query_count: NotRequired[int]
    retrieval_ranking_count: NotRequired[int]
    retrieval_channel_counts: NotRequired[Dict[str, int]]
    retrieval_carryover_count: NotRequired[int]
    parent_context_expanded_count: NotRequired[int]
    neighbor_context_expanded_count: NotRequired[int]
    evidence_span_input_count: NotRequired[int]
    evidence_span_output_count: NotRequired[int]
    evidence_span_compressed_count: NotRequired[int]
    evidence_span_fallback_count: NotRequired[int]
    evidence_span_input_chars: NotRequired[int]
    evidence_span_selected_chars: NotRequired[int]
    evidence_span_reason_counts: NotRequired[Dict[str, int]]
    evidence_pack_input_count: NotRequired[int]
    evidence_pack_kept_count: NotRequired[int]
    evidence_pack_dropped_count: NotRequired[int]
    evidence_pack_input_chars: NotRequired[int]
    evidence_pack_kept_chars: NotRequired[int]
    evidence_pack_overlap_removed_chars: NotRequired[int]
    evidence_pack_drop_reason_counts: NotRequired[Dict[str, int]]
    evidence_pack_anchor_count: NotRequired[int]
    evidence_pack_pinned_count: NotRequired[int]
    evidence_pack_over_budget: NotRequired[bool]
    retrieval_feedback_error: NotRequired[str]
    retrieval_retry_reason: NotRequired[str]
    adaptive_retrieval_retry_pending: NotRequired[bool]
    context: NotRequired[str]

    # 生成后逐声明证据审计，与生成前 evidence verifier 分属两道门禁。
    claim_audit_required: NotRequired[bool]
    claim_audit_passed: NotRequired[bool]
    claim_audit: NotRequired[Dict[str, Any]]
    claim_verifier_error: NotRequired[str]
    claim_repair_count: NotRequired[int]
    claim_repair_error: NotRequired[str]
    claim_repair_citation_valid: NotRequired[bool]
    claim_repair_critique: NotRequired[str]
    # 与最终 answer 摘要及渲染顺序绑定的通用结构化 claim 审计投影。
    claim_audit_projection: NotRequired[Dict[str, Any]]
    # 仅程序确定性生成的引导/错误答案可携带；原因码与完整答案绑定。
    claim_audit_exemption: NotRequired[Dict[str, str]]

    # evidence_ledger 在最终渲染前保存 response-scoped EID 注册表；
    # citation_ledger 仅保存最终答案中真实出现的安全公开映射。
    evidence_ledger: NotRequired[List[EvidenceLedgerEntry]]
    citation_ledger: NotRequired[List[CitationLedgerEntry]]

    answer: NotRequired[str]
    critique: NotRequired[str]
    sources: NotRequired[List[DocMeta]]
    evidence: NotRequired[List[Evidence]]
    # QA/Summary/Compare 共享的生成前证据单元计划、闭集和聚合指标。
    evidence_units: NotRequired[List[Dict[str, Any]]]
    evidence_unit_results: NotRequired[List[Dict[str, Any]]]
    evidence_unit_metrics: NotRequired[Dict[str, Any]]
    evidence_unit_retry_history: NotRequired[List[List[str]]]
    evidence_unit_assessments: NotRequired[List[Dict[str, Any]]]
    evidence_unit_verification_metrics: NotRequired[Dict[str, Any]]
    evidence_unit_verification_protocol_errors: NotRequired[List[str]]
    evidence_unit_verifier_error: NotRequired[str]
    evidence_unit_gate_decisions: NotRequired[List[Dict[str, Any]]]
    evidence_unit_generate_ids: NotRequired[List[str]]
    evidence_unit_retry_ids: NotRequired[List[str]]
    evidence_unit_terminal_ids: NotRequired[List[str]]
    evidence_unit_batch_can_generate: NotRequired[bool]
    evidence_unit_gate_metrics: NotRequired[Dict[str, Any]]
    evidence_unit_adapter_outcome: NotRequired[str]
    summary_source: NotRequired[str]
    summary_docs: NotRequired[List[RetrievedDoc]]
    summary_section_plans: NotRequired[List[SummarySectionPlan]]
    summary_section_results: NotRequired[List[SummarySectionResult]]
    compare_sources: NotRequired[List[str]]
    compare_docs_by_source: NotRequired[Dict[str, List[RetrievedDoc]]]
    compare_dimensions: NotRequired[List[CompareDimensionPlan]]
    document_profiles: NotRequired[List[DocumentProfile]]
    compare_table_answer: NotRequired[str]
    compare_conclusion: NotRequired[str]
    compare_conclusion_warning: NotRequired[str]

    chat_history: NotRequired[Annotated[List[ChatMessage], merge_lists]]
    # 单次图运行的易失性工作记忆；不落盘，任务结束后由会话分层记忆接管。
    working_memory: NotRequired[Dict[str, Any]]

    route: NotRequired[str]
    iteration_count: NotRequired[int]
    max_iteration_count: NotRequired[int]

    steps_trace: NotRequired[Annotated[List[AgentStepTrace], merge_lists]]

    error: NotRequired[Optional[str]]


# ParsedPage 是 PDF 解析后的页级输入。
class ParsedPage(TypedDict):
    page: int
    source: str
    text: str
    is_ocr_fallback: bool
