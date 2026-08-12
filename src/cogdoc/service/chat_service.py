import logging
from dataclasses import dataclass, field
from typing import Any, Iterator
from cogdoc.config.settings import get_settings
from cogdoc.agents.conversation_memory import extract_chat_turn, extract_final_answer
from cogdoc.agents.router import FORCED_TASK_TYPES
from cogdoc.observability.logger import configure_logging, log_event, new_trace_id
from cogdoc.observability.trace import build_trace_step, export_trace, monotonic_ms
from cogdoc.service.index_provenance import current_index_provenance
from cogdoc.tools.public_citation_ledger import (
    contains_internal_evidence_identifier,
    contains_internal_evidence_reference,
    validate_public_citation_ledger,
)

# 编译后的工作流图首次调用时惰性载入，避免模块级循环依赖。
app = None


# 服务层灾难失败时携带稳定的错误归因，交付层据此映射错误码。
class ChatServiceError(Exception):
    # 服务层灾难失败时携带稳定的错误归因，交付层据此映射错误码。
    def __init__(
        self,
        stage: str,
        error_class: str,
        message: str,
        trace_id: str | None = None,
    ):
        super().__init__(message)
        self.stage = stage
        self.error_class = error_class
        self.message = message
        self.trace_id = trace_id


# 定义对话事件数据结构。
@dataclass(frozen=True)
class ChatEvent:
    type: str
    payload: dict[str, Any] = field(default_factory=dict)


# 定义对话结果数据结构。
@dataclass(frozen=True)
class ChatResult:
    answer: str
    task_type: str
    citations: list[dict[str, Any]]
    evidence: list[dict[str, Any]]
    critique: str
    is_valid: bool
    trace_id: str
    request_id: str
    steps: list[dict[str, Any]]
    chat_messages: list[dict[str, Any]]
    raw_output: dict[str, Any]
    trace_path: str | None = None
    citation_ledger: list[dict[str, Any]] = field(default_factory=list)


# 完成 提取流程词项 处理。
def _extract_token(data: Any) -> str:
    message = data[0] if isinstance(data, tuple) and data else data
    content = getattr(message, "content", "")
    return content if isinstance(content, str) else ""


# 构建对话结果。
def _build_result(
    task_type: str,
    task_output: dict[str, Any],
    query: str,
    trace_id: str,
    trace_steps: list[dict[str, Any]],
    trace_path: str | None,
) -> ChatResult:
    answer = _delivery_answer(task_type, task_output)
    critique = _public_critique(task_type, str(task_output.get("critique", "") or ""))
    return ChatResult(
        answer=answer,
        task_type=task_type,
        citations=list(task_output.get("sources", []) or []),
        evidence=list(task_output.get("evidence", []) or []),
        critique=critique,
        is_valid=not bool(critique),
        trace_id=trace_id,
        request_id=trace_id,
        steps=trace_steps,
        chat_messages=extract_chat_turn(task_type, task_output, query),
        raw_output=task_output,
        trace_path=trace_path,
        citation_ledger=list(task_output.get("citation_ledger", []) or []),
    )


_CLAIM_AUDITED_TASKS = {"qa", "summary", "compare"}
_PUBLIC_AUDIT_CRITIQUE = "回答未通过引用或声明证据校验。"
_PUBLIC_CITATION_REJECTION = "引用格式未通过，正在重新生成。"
_PUBLIC_VERIFICATION_DETAIL_HIDDEN = "证据校验已完成，内部证据标识已隐藏。"


def _delivery_answer(task_type: str, task_output: dict[str, Any]) -> str:
    """Return the exact string whose public-ledger offsets were calculated.

    Finalized answers must not be stripped or otherwise normalized after occurrence
    offsets are frozen.  Compare's legacy messages fallback remains available only
    when no explicit answer exists.
    """

    if "answer" in task_output:
        return str(task_output.get("answer") or "")
    return extract_final_answer(task_type, task_output)


def _public_critique(task_type: str, critique: str) -> str:
    if task_type in _CLAIM_AUDITED_TASKS and critique:
        return _PUBLIC_AUDIT_CRITIQUE
    return critique


def _public_verification_reason(value: Any) -> str:
    text = str(value or "")
    # Verifier reason 是模型文本，可能回显 prompt 中的 EID；进度通道只需
    # 传达结论，不应暴露内部 response-scoped 标识。
    if contains_internal_evidence_identifier(text):
        return _PUBLIC_VERIFICATION_DETAIL_HIDDEN
    return text


def _enforce_claim_gate_release(
    task_type: str,
    task_output: dict[str, Any],
    settings: Any,
) -> bool:
    """Block a candidate if an enabled claim gate did not reach a releasable state."""

    if (
        not settings.claim_verification_enabled
        or task_type not in _CLAIM_AUDITED_TASKS
        or not extract_final_answer(task_type, task_output)
    ):
        return False

    from cogdoc.agents.answer_markers import (
        NO_RELEVANT_CONTENT_ANSWER,
        NO_RELEVANT_CONTENT_MARKER,
    )

    answer = extract_final_answer(task_type, task_output)
    if answer in {NO_RELEVANT_CONTENT_MARKER, NO_RELEVANT_CONTENT_ANSWER}:
        return False

    audit = task_output.get("claim_audit")
    audit = audit if isinstance(audit, dict) else {}
    status = str(audit.get("status") or "")
    reason_code = str(audit.get("reason_code") or "")
    if status in {"passed", "repaired"}:
        return False

    from cogdoc.agents.claim_evidence_verifier import (
        block_unfaithful_answer,
        matching_claim_audit_exemption,
    )

    exemption_reason = matching_claim_audit_exemption(
        task_output,
        answer=answer,
        task_type=task_type,
    )
    if status == "not_run" and exemption_reason and exemption_reason == reason_code:
        return False

    task_output.update(
        block_unfaithful_answer(
            task_output,
            reason_code=reason_code or "audit_incomplete",
        )
    )
    task_output["citation_ledger"] = []
    return True


def _enforce_citation_finalize_release(
    task_type: str,
    task_output: dict[str, Any],
) -> bool:
    """Fail closed when an audited answer still contains an internal Evidence ID."""

    answer = _delivery_answer(task_type, task_output)
    if task_type not in _CLAIM_AUDITED_TASKS or not answer:
        return False

    from cogdoc.agents.claim_evidence_verifier import block_unfaithful_answer

    reason_code = ""
    if contains_internal_evidence_reference(answer):
        reason_code = "citation_finalize_incomplete"
    else:
        ledger = task_output.get("citation_ledger")
        validation = validate_public_citation_ledger(
            answer,
            ledger,
            evidence=(
                task_output.get("evidence_ledger") or task_output.get("evidence")
            ),
            ledger_present="citation_ledger" in task_output,
            require_evidence=bool(ledger),
        )
        if not validation.is_valid:
            reason_code = "citation_ledger_invalid"
    if not reason_code:
        return False

    task_output.update(
        block_unfaithful_answer(
            task_output,
            reason_code=reason_code,
        )
    )
    task_output["citation_ledger"] = []
    return True


# 构建运行时错误步骤。
def _runtime_error_step(node_name: str, exc: Exception) -> dict[str, Any]:
    return {
        "node_name": node_name,
        "duration_ms": 0.0,
        "model": None,
        "token": None,
        "retrieval_top_k": None,
        "critique": None,
        "error_class": type(exc).__name__,
        "counts": {},
        "evidence": [],
    }


# 构建跟踪配置摘要。
def _query_preview(query: str, limit: int = 80) -> str:
    return " ".join((query or "").split())[:limit]


# 处理跟踪配置。
def _trace_config(
    doc_id: str,
    query: str,
    is_local: bool,
    forced_task: str | None,
    settings: Any,
    session_id: str | None = None,
) -> dict[str, Any]:
    return {
        "doc_id": doc_id,
        **current_index_provenance(doc_id),
        "session_id": session_id or "",
        "query_preview": _query_preview(query),
        "query_length": len(query),
        "is_local": is_local,
        "forced_task": forced_task,
        "qa_retrieval_top_k": settings.qa_retrieval_top_k,
        "qa_rerank_top_n": settings.qa_rerank_top_n,
        "qa_rerank_max_candidates": settings.qa_rerank_max_candidates,
        "qa_parent_context_enabled": settings.qa_parent_context_enabled,
        "qa_parent_context_max_chunks": settings.qa_parent_context_max_chunks,
        "qa_parent_context_max_chars": settings.qa_parent_context_max_chars,
        "qa_evidence_span_enabled": settings.qa_evidence_span_enabled,
        "qa_evidence_span_max_chars_per_doc": (
            settings.qa_evidence_span_max_chars_per_doc
        ),
        "qa_evidence_span_context_sentences": (
            settings.qa_evidence_span_context_sentences
        ),
        "qa_evidence_pack_max_docs": settings.qa_evidence_pack_max_docs,
        "qa_evidence_pack_max_chars": settings.qa_evidence_pack_max_chars,
        "qa_abstain_enabled": settings.qa_abstain_enabled,
        "qa_abstain_max_vector_distance": (settings.qa_abstain_max_vector_distance),
        "qa_abstain_min_bm25_score": settings.qa_abstain_min_bm25_score,
        "qa_abstain_min_knowledge_score": settings.qa_abstain_min_knowledge_score,
        "qa_evidence_verify_enabled": settings.qa_evidence_verify_enabled,
        "qa_evidence_verify_max_docs": settings.qa_evidence_verify_max_docs,
        "qa_evidence_verify_max_chars_per_doc": (
            settings.qa_evidence_verify_max_chars_per_doc
        ),
        "qa_evidence_verify_borderline_min_score": (
            settings.qa_evidence_verify_borderline_min_score
        ),
        "qa_retrieval_max_queries": settings.qa_retrieval_max_queries,
        "qa_adaptive_retrieval_enabled": settings.qa_adaptive_retrieval_enabled,
        "qa_adaptive_retrieval_max_retries": (
            settings.qa_adaptive_retrieval_max_retries
        ),
        "qa_adaptive_retrieval_top_k_multiplier": (
            settings.qa_adaptive_retrieval_top_k_multiplier
        ),
        "qa_adaptive_retrieval_max_top_k": (settings.qa_adaptive_retrieval_max_top_k),
        "claim_verification_enabled": settings.claim_verification_enabled,
        "claim_verification_max_claims": settings.claim_verification_max_claims,
        "claim_verification_max_claims_per_batch": (
            settings.claim_verification_max_claims_per_batch
        ),
        "claim_verification_max_docs_per_batch": (
            settings.claim_verification_max_docs_per_batch
        ),
        "claim_verification_max_chars_per_doc": (
            settings.claim_verification_max_chars_per_doc
        ),
        "claim_verification_max_repair_attempts": (
            settings.claim_verification_max_repair_attempts
        ),
        "model": settings.ollama_model_name if is_local else settings.llm_model_name,
    }


# 构建跟踪错误摘要。
def _trace_error(stage: str, exc: Exception) -> dict[str, Any]:
    return {
        "stage": stage,
        "error_class": type(exc).__name__,
        "message_preview": str(exc)[:200],
    }


# 运行对话。
def run_chat(
    doc_id: str,
    query: str,
    is_local: bool = False,
    chat_history: list | None = None,
    forced_task: str | None = None,
    session_id: str | None = None,
    *,
    state_runtime=None,
    retrieval_scope=None,
) -> Iterator[ChatEvent]:
    global app
    if app is None:
        from cogdoc.graph.workflow import app as _compiled_app

        app = _compiled_app

    configure_logging()
    settings = get_settings()
    if state_runtime is None:
        from cogdoc.state_runtime import default_state_runtime

        state_runtime = default_state_runtime()
    trace_id = new_trace_id()
    trace_steps: list[dict[str, Any]] = []
    request_start_ms = monotonic_ms()
    last_trace_ms = None
    trace_config = _trace_config(
        doc_id, query, is_local, forced_task, settings, session_id=session_id
    )
    stream_error: dict[str, Any] | None = None
    initial_state = {
        "messages": [],
        "chat_history": list(chat_history or []),
        "working_memory": {
            "goal": query,
            "status": "running",
            "task_type": forced_task or "auto",
            "tool_results": [],
        },
        "iteration_count": 0,
        "max_iteration_count": 2,
        "request_id": trace_id,
        "trace_id": trace_id,
        "session_id": session_id,
        "retrieval_scope": retrieval_scope,
    }

    configurable = {
        "doc_id": doc_id,
        "query": query,
        "is_local": is_local,
        "request_id": trace_id,
        "trace_id": trace_id,
        "session_id": session_id,
        "state_runtime": state_runtime,
        "retrieval_scope": retrieval_scope,
    }
    if forced_task in FORCED_TASK_TYPES:
        configurable["forced_task"] = forced_task
    runtime_config = {"configurable": configurable}

    current_task = "qa"
    saw_parent_output = False
    fallback_outputs: dict[str, dict[str, Any]] = {
        "qa": {},
        "summary": {},
        "compare": {},
        "unknown": {},
    }
    final_outputs: dict[str, dict[str, Any]] = {}
    buffered_model_tokens = False

    log_event(
        "runtime",
        "request_start",
        initial_state,
        doc_id=doc_id,
        is_local=is_local,
        forced_task=forced_task,
        query_length=len(query),
    )
    yield ChatEvent(
        "request_started",
        {
            "trace_id": trace_id,
            "request_id": trace_id,
            "doc_id": doc_id,
            "is_local": is_local,
            "forced_task": forced_task,
        },
    )

    try:
        # 编译图和测试注入的流实现共享动态协议，显式落到 Any 边界。
        stream_app: Any = app
        token_stream = stream_app.stream(
            initial_state,
            config=runtime_config,
            stream_mode=["messages", "updates"],
            subgraphs=True,
        )

        try:
            for ns, mode, data in token_stream:
                in_subgraph = len(ns) > 0
                if mode == "messages":
                    token = _extract_token(data)
                    # 被审计任务的模型输出包含内部 Evidence ID，必须等待最终渲染；
                    # 旧 claim gate 对其他任务的缓冲语义保持不变。
                    if token:
                        if (
                            current_task in _CLAIM_AUDITED_TASKS
                            or settings.claim_verification_enabled
                        ):
                            buffered_model_tokens = True
                        else:
                            yield ChatEvent("token", {"content": token})
                    continue

                if mode == "updates":
                    now_ms = monotonic_ms()
                    if last_trace_ms is None:
                        trace_steps.append(
                            {
                                "node_name": "runtime.setup",
                                "duration_ms": round(
                                    max(now_ms - request_start_ms, 0.0), 3
                                ),
                                "model": None,
                                "token": None,
                                "retrieval_top_k": None,
                                "critique": None,
                                "error_class": None,
                                "counts": {},
                                "evidence": [],
                            }
                        )
                        duration_ms = 0.0
                    else:
                        duration_ms = now_ms - last_trace_ms
                    last_trace_ms = now_ms

                    namespace = ".".join(str(item) for item in ns)
                    model_name = (
                        settings.ollama_model_name
                        if is_local
                        else settings.llm_model_name
                    )
                    for node_name, node_output in data.items():
                        if not isinstance(node_output, dict):
                            continue
                        full_node_name = (
                            f"{namespace}.{node_name}" if namespace else node_name
                        )
                        retrieval_top_k = (
                            int(
                                node_output.get(
                                    "retrieval_top_k_used",
                                    settings.qa_retrieval_top_k,
                                )
                            )
                            if node_name == "retrieve_node"
                            else None
                        )
                        trace_steps.append(
                            build_trace_step(
                                full_node_name,
                                node_output,
                                duration_ms,
                                model_name=model_name,
                                retrieval_top_k=retrieval_top_k,
                            )
                        )

                if mode == "updates" and not in_subgraph and "intent_router" in data:
                    router_output = data["intent_router"]
                    current_task = router_output.get("task_type", "qa")
                    yield ChatEvent(
                        "router_decided",
                        {
                            "task_type": current_task,
                            "reason": router_output.get("router_reason", "无"),
                        },
                    )
                elif mode == "updates" and in_subgraph and "rewrite_node" in data:
                    rewrite_output = data["rewrite_node"]
                    yield ChatEvent(
                        "rewrite_queries",
                        {
                            "queries": list(
                                rewrite_output.get("rewritten_queries", [])
                            ),
                            "requirements": list(
                                rewrite_output.get("evidence_requirements", [])
                            ),
                        },
                    )
                elif mode == "updates" and in_subgraph and "rerank_node" in data:
                    rerank_output = data["rerank_node"]
                    fallback_outputs["qa"].update(rerank_output)
                    is_abstained = bool(rerank_output.get("retrieval_abstained"))
                    verification_pending = bool(
                        rerank_output.get("evidence_verification_pending", False)
                    )
                    retry_pending = bool(
                        rerank_output.get("adaptive_retrieval_retry_pending", False)
                    )
                    if is_abstained and not verification_pending and not retry_pending:
                        yield ChatEvent(
                            "retrieval_abstained",
                            {
                                "confidence": rerank_output.get(
                                    "retrieval_confidence", 0.0
                                ),
                                "reason": rerank_output.get(
                                    "retrieval_abstain_reason", ""
                                ),
                            },
                        )
                elif (
                    mode == "updates" and in_subgraph and "evidence_verify_node" in data
                ):
                    verify_output = data["evidence_verify_node"]
                    fallback_outputs["qa"].update(verify_output)
                    supported = bool(verify_output.get("evidence_supported"))
                    retry_pending = bool(
                        verify_output.get("adaptive_retrieval_retry_pending", False)
                    )
                    verification_payload = {
                        "supported": supported,
                        "reason": _public_verification_reason(
                            verify_output.get("evidence_verification_reason", "")
                        ),
                        "evidence_chunk_ids": list(
                            verify_output.get("evidence_verified_chunk_ids", [])
                        ),
                    }
                    requirement_assessments = list(
                        verify_output.get("evidence_requirement_assessments", [])
                    )
                    if requirement_assessments:
                        verification_payload["requirement_assessments"] = [
                            {
                                **assessment,
                                "reason": _public_verification_reason(
                                    assessment.get("reason", "")
                                ),
                            }
                            if isinstance(assessment, dict)
                            else assessment
                            for assessment in requirement_assessments
                        ]
                    if retry_pending:
                        verification_payload["will_retry"] = True
                    yield ChatEvent(
                        "evidence_verified" if supported else "evidence_rejected",
                        verification_payload,
                    )
                elif (
                    mode == "updates" and in_subgraph and "retrieval_retry_node" in data
                ):
                    retry_output = data["retrieval_retry_node"]
                    fallback_outputs["qa"].update(retry_output)
                    yield ChatEvent(
                        "retrieval_retry",
                        {
                            "retry_count": retry_output.get("retrieval_retry_count", 0),
                            "reason": retry_output.get("retrieval_retry_reason", ""),
                            "missing_requirement_ids": list(
                                retry_output.get("missing_evidence_requirement_ids", [])
                            ),
                        },
                    )
                elif mode == "updates" and in_subgraph and "citation_node" in data:
                    citation_output = data["citation_node"]
                    fallback_outputs["qa"].update(citation_output)
                    critique = citation_output.get("critique", "")
                    iter_num = citation_output.get("iteration_count", 1)
                    max_iter = citation_output.get(
                        "max_iteration_count",
                        initial_state.get("max_iteration_count", 2),
                    )
                    if critique:
                        round_answer = fallback_outputs["qa"].get("answer", "")
                        rejection_payload = {
                            "critique": (
                                _PUBLIC_CITATION_REJECTION
                                if current_task in _CLAIM_AUDITED_TASKS
                                else critique
                            ),
                            "iteration_count": iter_num,
                            "max_iteration_count": max_iter,
                            "will_retry": iter_num <= max_iter,
                        }
                        # Debug 预览也是发布通道；被审计任务的候选可能带内部 EID。
                        if (
                            current_task not in _CLAIM_AUDITED_TASKS
                            and not settings.claim_verification_enabled
                        ):
                            rejection_payload["round_answer"] = round_answer
                        yield ChatEvent(
                            "citation_rejected",
                            rejection_payload,
                        )
                    else:
                        yield ChatEvent(
                            "citation_passed",
                            {
                                "iteration_count": iter_num,
                                "max_iteration_count": max_iter,
                            },
                        )
                elif (
                    mode == "updates"
                    and in_subgraph
                    and "compare_citation_node" in data
                ):
                    compare_citation_output = data["compare_citation_node"]
                    fallback_outputs["compare"].update(compare_citation_output)
                    critique = compare_citation_output.get("critique", "")
                    if critique:
                        yield ChatEvent(
                            "compare_citation_rejected",
                            {"critique": _PUBLIC_CITATION_REJECTION},
                        )
                    else:
                        yield ChatEvent("compare_citation_passed", {})
                elif mode == "updates" and in_subgraph:
                    task_output = fallback_outputs.setdefault(current_task, {})
                    for value in data.values():
                        if isinstance(value, dict):
                            task_output.update(value)
                elif mode == "updates" and not in_subgraph:
                    postprocess_updated = False
                    for parent_key, task_name in {
                        "qa_subgraph": "qa",
                        "summary_subgraph": "summary",
                        "compare_subgraph": "compare",
                        "unknown_node": "unknown",
                    }.items():
                        if parent_key in data:
                            saw_parent_output = True
                            final_outputs[task_name] = data[parent_key]
                            postprocess_updated = True
                            break
                    if not postprocess_updated:
                        task_output = final_outputs.setdefault(
                            current_task,
                            fallback_outputs.get(current_task, {}),
                        )
                        for node_name in (
                            "claim_audit_node",
                            "claim_repair_node",
                            "claim_repair_citation_node",
                            "claim_block_node",
                            "citation_finalize_node",
                        ):
                            node_output = data.get(node_name)
                            if isinstance(node_output, dict):
                                task_output.update(node_output)
                                audit = node_output.get("claim_audit") or {}
                                if node_name == "claim_audit_node":
                                    yield ChatEvent(
                                        "claim_audit",
                                        {
                                            "status": audit.get("status", "not_run"),
                                            "counts": audit.get("counts", {}),
                                            "metrics": audit.get("metrics", {}),
                                        },
                                    )
                                elif node_name == "claim_repair_node":
                                    yield ChatEvent(
                                        "claim_repair",
                                        {
                                            "attempt_count": node_output.get(
                                                "claim_repair_count", 0
                                            ),
                                            "error": node_output.get(
                                                "claim_repair_error", ""
                                            ),
                                        },
                                    )
                                elif node_name == "claim_block_node":
                                    yield ChatEvent(
                                        "claim_rejected",
                                        {
                                            "status": audit.get("status", "rejected"),
                                            "reason_code": audit.get("reason_code", ""),
                                        },
                                    )
                                break

            if not saw_parent_output:
                task_output = fallback_outputs.get(current_task, {})
                if task_output:
                    final_outputs[current_task] = task_output

        except Exception as stream_err:
            stream_error = _trace_error("stream", stream_err)
            trace_steps.append(_runtime_error_step("runtime.stream", stream_err))
            log_event(
                "runtime",
                "request_stream_error",
                initial_state,
                level=logging.ERROR,
                error_class=type(stream_err).__name__,
            )
            yield ChatEvent(
                "error",
                {
                    "error_class": type(stream_err).__name__,
                    "message": str(stream_err),
                    "stage": "stream",
                    "trace_id": trace_id,
                },
            )

        task_output = final_outputs.get(current_task, {})
        gate_forced_block = _enforce_claim_gate_release(
            current_task,
            task_output,
            settings,
        )
        if gate_forced_block:
            trace_steps.append(
                build_trace_step(
                    "runtime.claim_gate_fail_closed",
                    task_output,
                    0.0,
                )
            )
            log_event(
                "claim_audit",
                "claim_gate_fail_closed",
                initial_state,
                task_type=current_task,
                stream_error=bool(stream_error),
            )
        citation_finalize_forced_block = _enforce_citation_finalize_release(
            current_task,
            task_output,
        )
        if citation_finalize_forced_block:
            trace_steps.append(
                build_trace_step(
                    "runtime.citation_finalize_fail_closed",
                    task_output,
                    0.0,
                )
            )
            log_event(
                "citation_ledger",
                "citation_finalize_fail_closed",
                initial_state,
                task_type=current_task,
                stream_error=bool(stream_error),
            )
        delivery_answer = _delivery_answer(current_task, task_output)
        has_releasable_output = bool(delivery_answer.strip())
        if buffered_model_tokens and not has_releasable_output and stream_error is None:
            missing_final_error = RuntimeError(
                "chat graph produced model tokens but no releasable final answer"
            )
            stream_error = _trace_error("runtime", missing_final_error)
            trace_steps.append(
                _runtime_error_step(
                    "runtime.missing_final_answer",
                    missing_final_error,
                )
            )
            log_event(
                "runtime",
                "missing_final_answer",
                initial_state,
                level=logging.ERROR,
                task_type=current_task,
            )
            yield ChatEvent(
                "error",
                {
                    "error_class": type(missing_final_error).__name__,
                    "message": str(missing_final_error),
                    "stage": "runtime",
                    "trace_id": trace_id,
                },
            )
        trace_status = "ok"
        if stream_error:
            trace_status = "degraded" if has_releasable_output else "failed"
        exported = export_trace(
            trace_id=trace_id,
            request_id=trace_id,
            task_type=current_task,
            steps=trace_steps,
            settings=settings,
            status=trace_status,
            duration_ms=monotonic_ms() - request_start_ms,
            error=stream_error,
            config=trace_config,
            input_payload={
                "query": query,
                "doc_id": doc_id,
                "session_id": session_id,
                "chat_history": list(chat_history or []),
            },
            output_payload=task_output,
            execution_status=(
                "SUCCESS"
                if trace_status == "ok"
                else "TRACE_INCOMPLETE"
                if trace_status == "degraded"
                else "TARGET_ERROR"
            ),
        )
        trace_path = str(exported) if exported else None
        result = _build_result(
            current_task,
            task_output,
            query,
            trace_id,
            trace_steps,
            trace_path,
        )
        # 检索/重排等局部状态不是可交付结果；流已失败且没有答案时只保留
        # 先前 error 事件，禁止再伪造一个空 final。
        if stream_error and not result.answer.strip():
            log_event(
                "runtime",
                "request_end",
                initial_state,
                task_type=current_task,
                has_output=False,
                trace_path=trace_path,
            )
            return
        emit_buffered_answer = settings.claim_verification_enabled or (
            current_task in _CLAIM_AUDITED_TASKS and buffered_model_tokens
        )
        if emit_buffered_answer and result.answer.strip():
            audit = result.raw_output.get("claim_audit")
            audit_status = (
                str(audit.get("status") or "") if isinstance(audit, dict) else ""
            )
            token_payload: dict[str, Any] = {"content": result.answer}
            if settings.claim_verification_enabled:
                token_payload["verified"] = result.is_valid and audit_status in {
                    "passed",
                    "repaired",
                }
            yield ChatEvent("token", token_payload)
        log_event(
            "runtime",
            "request_end",
            initial_state,
            task_type=current_task,
            has_output=bool(task_output),
            trace_path=trace_path,
        )
        yield ChatEvent("final", {"result": result, "output": task_output})

    except Exception as exc:
        trace_error = _trace_error("runtime", exc)
        trace_steps.append(_runtime_error_step("runtime.failed", exc))
        export_trace(
            trace_id=trace_id,
            request_id=trace_id,
            task_type="unknown",
            steps=trace_steps,
            settings=settings,
            status="failed",
            duration_ms=monotonic_ms() - request_start_ms,
            error=trace_error,
            config=trace_config,
            input_payload={
                "query": query,
                "doc_id": doc_id,
                "session_id": session_id,
                "chat_history": list(chat_history or []),
            },
            execution_status="TARGET_ERROR",
        )
        log_event(
            "runtime",
            "request_failed",
            initial_state,
            level=logging.ERROR,
            error_class=type(exc).__name__,
        )
        yield ChatEvent(
            "error",
            {
                "error_class": type(exc).__name__,
                "message": str(exc),
                "stage": "runtime",
                "trace_id": trace_id,
            },
        )


# 同步运行对话。
def run_chat_sync(*args: Any, **kwargs: Any) -> ChatResult:
    result = None
    last_error: dict[str, Any] | None = None
    for event in run_chat(*args, **kwargs):
        if event.type == "final":
            result = event.payload["result"]
        elif event.type == "error":
            last_error = event.payload
    # 出现错误事件且最终无可信输出即视为失败，不把空答案当成功返回。
    has_trustworthy_output = result is not None and bool(result.answer.strip())
    if last_error is not None and not has_trustworthy_output:
        raise ChatServiceError(
            stage=last_error.get("stage", "runtime"),
            error_class=last_error.get("error_class", "RuntimeError"),
            message=last_error.get("message", "")
            or "chat service did not produce a usable result",
            trace_id=last_error.get("trace_id"),
        )
    if result is not None:
        return result
    raise ChatServiceError(
        stage="runtime",
        error_class="RuntimeError",
        message="chat service did not produce a final result",
    )
