from typing import Any, Literal, cast
from langchain_core.messages import AIMessage
from langgraph.graph import StateGraph, START, END
from cogdoc.graph.state import GraphState
from cogdoc.agents.router import RouterAgent
from cogdoc.graph.subgraphs.qa import qa_subgraph_node
from cogdoc.graph.subgraphs.summary import summary_subgraph_node
from cogdoc.graph.subgraphs.compare import compare_subgraph_node
from cogdoc.agents.claim_evidence_verifier import (
    ClaimEvidenceVerifierAgent,
    ClaimRepairAgent,
    block_unfaithful_answer,
    documents_for_state,
    matching_claim_audit_exemption,
    state_has_only_no_evidence_units,
)
from cogdoc.agents.citation_validator import CitationValidatorAgent
from cogdoc.config.settings import (
    CLAIM_VERIFICATION_MODES,
    get_settings,
    resolve_claim_verification_mode,
)
from cogdoc.observability.logger import log_event
from cogdoc.service.claim_verification_rollout import (
    build_claim_verification_rollout,
)
from cogdoc.tools.citation_ledger import (
    CitationLedgerError,
    render_display_citations,
    validate_evidence_citations,
)


UNKNOWN_RESPONSE = (
    "我是面向本地知识库的文档问答助手，你这条更像闲聊或与库内文档无关。"
    "可以问我库里文档的内容，或用 /summary、/compare 指定模式。"
)


# 路由 by task。
def route_by_task(
    state: GraphState,
) -> Literal["qa_subgraph", "summary_subgraph", "compare_subgraph", "unknown_node"]:
    # 路由结果只允许落到已注册的子图节点。
    task = state.get("task_type", "qa")

    if task == "qa":
        return "qa_subgraph"
    elif task == "summary":
        return "summary_subgraph"
    elif task == "compare":
        return "compare_subgraph"
    else:
        return "unknown_node"


# 完成 未知意图node 处理。
def unknown_node(state: GraphState) -> dict:
    answer = UNKNOWN_RESPONSE
    return {"answer": answer, "messages": [AIMessage(content=answer)]}


def claim_audit_node(state: GraphState) -> dict:
    settings = get_settings()
    raw_state_mode = state.get("claim_verification_mode")
    mode = (
        str(raw_state_mode).strip().lower()
        if str(raw_state_mode or "").strip().lower() in CLAIM_VERIFICATION_MODES
        else resolve_claim_verification_mode(settings)
    )
    output = ClaimEvidenceVerifierAgent.audit(state, force_enabled=mode != "off")
    output["claim_verification_mode"] = mode
    rollout_state = {**state, **output}
    output["claim_verification_rollout"] = build_claim_verification_rollout(
        rollout_state,
        mode=mode,
        max_repair_attempts=settings.claim_verification_max_repair_attempts,
    )
    audit = output.get("claim_audit") or {}
    rollout = output["claim_verification_rollout"]
    metrics = audit.get("metrics") or {}
    counts = audit.get("counts") or {}
    log_event(
        "claim_audit",
        "claim_audit_completed",
        state,
        status=audit.get("status", "not_run"),
        claim_count=counts.get("claim_count", 0),
        claim_support_rate=metrics.get("claim_support_rate"),
        citation_coverage=metrics.get("citation_coverage"),
        unsupported_claim_rate=metrics.get("unsupported_claim_rate"),
        verifier_error=output.get("claim_verifier_error", ""),
        verification_mode=mode,
        rollout_decision=rollout.get("decision", "skipped"),
    )
    return output


def claim_audit_check(state: GraphState) -> str:
    rollout = state.get("claim_verification_rollout")
    if isinstance(rollout, dict):
        mode = str(rollout.get("mode") or "off")
        decision = str(rollout.get("decision") or "skipped")
        if mode in {"off", "shadow"}:
            return "citation_finalize_node"
        if decision == "repair":
            return "claim_repair_node"
        if decision == "block":
            return "claim_block_node"
        if decision in {"allow", "allow_exempt"}:
            return "citation_finalize_node"
    audit = state.get("claim_audit") or {}
    status = str(audit.get("status") or "not_run")
    if status in {"not_run", "passed", "repaired"}:
        return "citation_finalize_node"
    if status == "failed" and int(state.get("claim_repair_count", 0) or 0) < (
        get_settings().claim_verification_max_repair_attempts
    ):
        return "claim_repair_node"
    return "claim_block_node"


def claim_repair_node(state: GraphState) -> dict:
    output = ClaimRepairAgent.repair(state)
    log_event(
        "claim_audit",
        "claim_repair_completed",
        state,
        repair_count=output.get("claim_repair_count", 0),
        repair_error=output.get("claim_repair_error", ""),
    )
    return output


def claim_repair_check(state: GraphState) -> str:
    return (
        "claim_block_node"
        if state.get("claim_repair_error")
        else "claim_repair_citation_node"
    )


def claim_repair_citation_node(state: GraphState) -> dict:
    if state.get("evidence_ledger") is not None:
        result = CitationValidatorAgent.validate_evidence_citations(
            str(state.get("answer") or ""), state.get("evidence_ledger", [])
        )
    else:
        result = CitationValidatorAgent.validate_citations(
            str(state.get("answer") or ""),
            cast(list[dict[str, Any]], documents_for_state(state)),
        )
    return {
        "claim_repair_citation_valid": bool(result.get("is_valid")),
        "claim_repair_critique": str(result.get("critique") or ""),
    }


def claim_repair_citation_check(state: GraphState) -> str:
    if state.get("claim_repair_citation_valid"):
        return "claim_audit_node"
    if int(state.get("claim_repair_count", 0) or 0) < (
        get_settings().claim_verification_max_repair_attempts
    ):
        return "claim_repair_node"
    return "claim_block_node"


def claim_block_node(state: GraphState) -> dict:
    output = block_unfaithful_answer(state)
    mode = resolve_claim_verification_mode(
        {
            "claim_verification_mode": state.get("claim_verification_mode"),
            "claim_verification_enabled": True,
        }
    )
    output["claim_verification_mode"] = mode
    output["claim_verification_rollout"] = build_claim_verification_rollout(
        {**state, **output},
        mode=mode,
        max_repair_attempts=get_settings().claim_verification_max_repair_attempts,
    )
    log_event(
        "claim_audit",
        "claim_audit_rejected",
        state,
        repair_count=state.get("claim_repair_count", 0),
        verifier_error=state.get("claim_verifier_error", ""),
    )
    return output


def _final_answer_update(state: GraphState, answer: str) -> dict[str, Any]:
    output: dict[str, Any] = {"answer": answer}
    messages = state.get("messages", [])
    last_message = messages[-1] if messages else None
    message_id = getattr(last_message, "id", None)
    output["messages"] = [
        AIMessage(content=answer, id=message_id)
        if message_id
        else AIMessage(content=answer)
    ]
    return output


def citation_finalize_node(state: GraphState) -> dict:
    """Render audited internal EIDs exactly once at the parent workflow boundary."""

    ledger = state.get("evidence_ledger")
    if ledger is None:
        return {"citation_ledger": []}
    raw_answer = str(state.get("answer") or "")
    # API/session 都发布 strip 后的答案；occurrence 必须以同一字符串为坐标系。
    answer = raw_answer.strip()
    if not answer:
        output: dict[str, Any] = {"citation_ledger": []}
        if answer != raw_answer:
            output.update(_final_answer_update(state, answer))
        return output
    exempt = bool(
        matching_claim_audit_exemption(
            state,
            answer=answer,
            task_type=str(state.get("task_type") or ""),
        )
    )
    if exempt or state_has_only_no_evidence_units(state):
        no_evidence_check = validate_evidence_citations(
            answer, ledger, require_citation=False
        )
        if no_evidence_check.get("is_valid") and not no_evidence_check.get(
            "evidence_ids"
        ):
            output = {"citation_ledger": []}
            if answer != raw_answer:
                output.update(_final_answer_update(state, answer))
            return output
        return block_unfaithful_answer(
            state, reason_code="citation_ledger_finalize_failed"
        )
    if not ledger:
        empty_ledger_check = validate_evidence_citations(
            answer, ledger, require_citation=False
        )
        if empty_ledger_check.get("is_valid") and not empty_ledger_check.get(
            "evidence_ids"
        ):
            output = {"citation_ledger": []}
            if answer != raw_answer:
                output.update(_final_answer_update(state, answer))
            return output
        return block_unfaithful_answer(
            state, reason_code="citation_ledger_finalize_failed"
        )
    try:
        rendered = render_display_citations(answer, ledger)
    except CitationLedgerError:
        return block_unfaithful_answer(
            state, reason_code="citation_ledger_finalize_failed"
        )
    final_output: dict[str, Any] = {
        "citation_ledger": list(rendered.entries),
    }
    final_output.update(_final_answer_update(state, rendered.answer))
    return final_output


workflow = StateGraph(GraphState)

workflow.add_node("intent_router", RouterAgent.route_intent)
workflow.add_node("qa_subgraph", qa_subgraph_node)
workflow.add_node("summary_subgraph", summary_subgraph_node)
workflow.add_node("compare_subgraph", compare_subgraph_node)
workflow.add_node("unknown_node", unknown_node)
workflow.add_node("claim_audit_node", claim_audit_node)
workflow.add_node("claim_repair_node", claim_repair_node)
workflow.add_node("claim_repair_citation_node", claim_repair_citation_node)
workflow.add_node("claim_block_node", claim_block_node)
workflow.add_node("citation_finalize_node", citation_finalize_node)

workflow.add_edge(START, "intent_router")

workflow.add_conditional_edges(
    "intent_router",
    route_by_task,
    {
        "qa_subgraph": "qa_subgraph",
        "summary_subgraph": "summary_subgraph",
        "compare_subgraph": "compare_subgraph",
        "unknown_node": "unknown_node",
    },
)
workflow.add_edge("qa_subgraph", "claim_audit_node")
workflow.add_edge("summary_subgraph", "claim_audit_node")
workflow.add_edge("compare_subgraph", "claim_audit_node")
workflow.add_conditional_edges(
    "claim_audit_node",
    claim_audit_check,
    {
        "claim_repair_node": "claim_repair_node",
        "claim_block_node": "claim_block_node",
        "citation_finalize_node": "citation_finalize_node",
    },
)
workflow.add_conditional_edges(
    "claim_repair_node",
    claim_repair_check,
    {
        "claim_repair_citation_node": "claim_repair_citation_node",
        "claim_block_node": "claim_block_node",
    },
)
workflow.add_conditional_edges(
    "claim_repair_citation_node",
    claim_repair_citation_check,
    {
        "claim_audit_node": "claim_audit_node",
        "claim_repair_node": "claim_repair_node",
        "claim_block_node": "claim_block_node",
    },
)
workflow.add_edge("claim_block_node", END)
workflow.add_edge("citation_finalize_node", END)
workflow.add_edge("unknown_node", "citation_finalize_node")

app = workflow.compile()
