import importlib
from langchain_core.messages import AIMessage

from cogdoc.agents.claim_evidence_verifier import (
    CLAIM_AUDIT_BLOCKED_ANSWER,
    CLAIM_AUDIT_EXEMPTION_GUIDANCE,
    make_claim_audit_exemption,
)
from cogdoc.agents.router import RouterAgent
from cogdoc.config.settings import Settings
from cogdoc.graph import workflow
from cogdoc.graph.workflow import route_by_task, unknown_node


# 验证 route by task sends unknown to terminal node 场景。
def test_route_by_task_sends_unknown_to_terminal_node():
    assert route_by_task({"task_type": "unknown"}) == "unknown_node"


# 验证 unknown node returns readable answer and message 场景。
def test_unknown_node_returns_readable_answer_and_message():
    result = unknown_node({"task_type": "unknown"})

    assert "本地知识库" in result["answer"]
    assert result["messages"]
    assert result["messages"][0].content == result["answer"]


# 验证 workflow unknown route produces answer 场景。
def test_workflow_unknown_route_produces_answer(monkeypatch):
    # 构造route意图。
    def fake_route_intent(state, config):
        return {
            "query": "你好",
            "doc_id": "kb",
            "is_local": False,
            "task_type": "unknown",
            "router_reason": "纯闲聊",
        }

    with monkeypatch.context() as patcher:
        patcher.setattr(RouterAgent, "route_intent", staticmethod(fake_route_intent))
        reloaded = importlib.reload(workflow)
        result = reloaded.app.invoke(
            {
                "messages": [],
                "chat_history": [],
                "iteration_count": 0,
                "max_iteration_count": 2,
            },
            config={"configurable": {"query": "你好", "doc_id": "kb"}},
        )

    importlib.reload(workflow)

    assert result["task_type"] == "unknown"
    assert "本地知识库" in result["answer"]


# 验证三个 RAG 任务都必须经过父图声明审计，所有公开答案再统一终态化。
def test_workflow_places_claim_gate_after_all_rag_subgraphs():
    edges = {(edge.source, edge.target) for edge in workflow.app.get_graph().edges}

    assert ("qa_subgraph", "claim_audit_node") in edges
    assert ("summary_subgraph", "claim_audit_node") in edges
    assert ("compare_subgraph", "claim_audit_node") in edges
    assert ("unknown_node", "claim_audit_node") not in edges
    assert ("unknown_node", "citation_finalize_node") in edges


# 验证失败声明只有有限修复机会，校验器错误立即走稳定拦截。
def test_claim_audit_route_is_bounded_and_fail_closed(monkeypatch):
    settings = Settings(
        _env_file=None,
        claim_verification_max_repair_attempts=1,
    )
    monkeypatch.setattr(workflow, "get_settings", lambda: settings)

    assert (
        workflow.claim_audit_check({"claim_audit": {"status": "passed"}})
        == "citation_finalize_node"
    )
    assert (
        workflow.claim_audit_check(
            {"claim_audit": {"status": "failed"}, "claim_repair_count": 0}
        )
        == "claim_repair_node"
    )
    assert (
        workflow.claim_audit_check(
            {"claim_audit": {"status": "failed"}, "claim_repair_count": 1}
        )
        == "claim_block_node"
    )
    assert (
        workflow.claim_audit_check({"claim_audit": {"status": "error"}})
        == "claim_block_node"
    )
    assert (
        workflow.claim_repair_check({"claim_repair_error": "TimeoutError"})
        == "claim_block_node"
    )
    assert (
        workflow.claim_repair_citation_check(
            {"claim_repair_citation_valid": True, "claim_repair_count": 1}
        )
        == "claim_audit_node"
    )
    assert (
        workflow.claim_repair_citation_check(
            {"claim_repair_citation_valid": False, "claim_repair_count": 0}
        )
        == "claim_repair_node"
    )
    assert (
        workflow.claim_repair_citation_check(
            {"claim_repair_citation_valid": False, "claim_repair_count": 1}
        )
        == "claim_block_node"
    )


def test_shadow_claim_audit_records_would_repair_and_never_mutates_answer(
    monkeypatch,
):
    settings = Settings(
        _env_file=None,
        claim_verification_mode="shadow",
        claim_verification_max_repair_attempts=1,
    )
    calls = []
    monkeypatch.setattr(workflow, "get_settings", lambda: settings)
    monkeypatch.setattr(
        workflow.ClaimEvidenceVerifierAgent,
        "audit",
        staticmethod(
            lambda state, force_enabled=False: calls.append(force_enabled)
            or {
                "claim_audit": {
                    "status": "failed",
                    "reason_code": "unsupported_claims",
                },
                "claim_audit_passed": False,
            }
        ),
    )
    state = {
        "task_type": "qa",
        "answer": "原始候选答案。[a.pdf:P1]",
        "claim_verification_mode": "shadow",
        "claim_verification_policy": {
            "configured_mode": "enforce",
            "effective_mode": "shadow",
            "rollout_percent": 25.0,
            "cohort_bucket": 4321,
            "cohort_selected": False,
            "fallback_mode": "shadow",
            "policy_id": "2222222222222222",
        },
    }

    output = workflow.claim_audit_node(state)

    assert calls == [True]
    assert output.get("answer") is None
    assert output["claim_verification_rollout"]["decision"] == "would_repair"
    assert output["claim_verification_rollout"]["configured_mode"] == "enforce"
    assert output["claim_verification_rollout"]["policy_id"] == "2222222222222222"
    assert workflow.claim_audit_check(output) == "citation_finalize_node"


def test_enforce_rollout_routes_repair_and_block_explicitly():
    assert (
        workflow.claim_audit_check(
            {
                "claim_verification_rollout": {
                    "mode": "enforce",
                    "decision": "repair",
                }
            }
        )
        == "claim_repair_node"
    )
    assert (
        workflow.claim_audit_check(
            {
                "claim_verification_rollout": {
                    "mode": "enforce",
                    "decision": "block",
                }
            }
        )
        == "claim_block_node"
    )


def test_citation_finalizer_renders_eid_and_builds_occurrence_ledger():
    result = workflow.citation_finalize_node(
        {
            "answer": "截止日期见说明。[E001]",
            "messages": [AIMessage(content="截止日期见说明。[E001]", id="answer")],
            "evidence_ledger": [
                {
                    "evidence_id": "E001",
                    "chunk_id": "chunk:guide:2",
                    "source_type": "document",
                    "source": "guide.pdf",
                    "page": 2,
                    "span_start": 10,
                    "span_end": 28,
                    "display_citation": "[guide.pdf:P2]",
                }
            ],
        }
    )

    assert result["answer"] == "截止日期见说明。[guide.pdf:P2]"
    assert result["messages"][0].id == "answer"
    assert result["messages"][0].content == result["answer"]
    assert result["citation_ledger"][0]["occurrences"] == [
        {"index": 0, "answer_start": 8, "answer_end": 22}
    ]


def test_citation_finalizer_fails_closed_for_unknown_eid():
    result = workflow.citation_finalize_node(
        {
            "answer": "伪造引用。[E999]",
            "evidence_ledger": [
                {
                    "evidence_id": "E001",
                    "chunk_id": "chunk:guide:2",
                    "source_type": "document",
                    "source": "guide.pdf",
                    "page": 2,
                    "span_start": 0,
                    "span_end": 10,
                    "display_citation": "[guide.pdf:P2]",
                }
            ],
        }
    )

    assert result["answer"] == CLAIM_AUDIT_BLOCKED_ANSWER
    assert result["citation_ledger"] == []
    assert result["evidence_ledger"] == []


def test_citation_finalizer_fails_closed_for_internal_id_with_empty_ledger():
    result = workflow.citation_finalize_node(
        {
            "answer": "内部引用不能发布。[E001]",
            "evidence_ledger": [],
        }
    )

    assert result["answer"] == CLAIM_AUDIT_BLOCKED_ANSWER
    assert result["citation_ledger"] == []
    assert result["claim_audit"]["reason_code"] == ("citation_ledger_finalize_failed")


def test_citation_finalizer_fails_closed_for_malformed_ledger():
    result = workflow.citation_finalize_node(
        {
            "answer": "结论。[E001]",
            "evidence_ledger": [
                {
                    "evidence_id": "E001",
                    "chunk_id": "chunk:guide:2",
                    "source_type": "document",
                    "source": "guide.pdf",
                    "page": 2,
                    "span_start": "bad",
                    "span_end": 10,
                    "display_citation": "[guide.pdf:P2]",
                }
            ],
        }
    )

    assert result["answer"] == CLAIM_AUDIT_BLOCKED_ANSWER
    assert result["citation_ledger"] == []


def test_citation_finalizer_strips_before_calculating_occurrence_offsets():
    result = workflow.citation_finalize_node(
        {
            "answer": "  结论[E001]  ",
            "messages": [AIMessage(content="  结论[E001]  ", id="answer")],
            "evidence_ledger": [
                {
                    "evidence_id": "E001",
                    "chunk_id": "chunk:guide:2",
                    "source_type": "document",
                    "source": "guide.pdf",
                    "page": 2,
                    "span_start": 0,
                    "span_end": 10,
                    "display_citation": "[guide.pdf:P2]",
                }
            ],
        }
    )

    assert result["answer"] == "结论[guide.pdf:P2]"
    occurrence = result["citation_ledger"][0]["occurrences"][0]
    assert occurrence["answer_start"] == 2
    assert (
        result["answer"][occurrence["answer_start"] : occurrence["answer_end"]]
        == "[guide.pdf:P2]"
    )


def test_citation_finalizer_keeps_deterministic_no_evidence_summary():
    answer = "# 摘要\n文档中未明确说明。"
    result = workflow.citation_finalize_node(
        {
            "task_type": "summary",
            "answer": answer,
            "summary_section_results": [
                {
                    "section_id": "limits",
                    "title": "限制",
                    "content": "文档中未明确说明。",
                    "evidence": [],
                }
            ],
            "evidence_ledger": [
                {
                    "evidence_id": "E001",
                    "chunk_id": "chunk:guide:2",
                    "source_type": "document",
                    "source": "guide.pdf",
                    "page": 2,
                    "span_start": 0,
                    "span_end": 10,
                    "display_citation": "[guide.pdf:P2]",
                }
            ],
        }
    )

    assert result == {"citation_ledger": []}


def test_citation_finalizer_keeps_bound_guidance_with_nonempty_registry():
    answer = "请明确指定要总结的文档。"
    result = workflow.citation_finalize_node(
        {
            "task_type": "summary",
            "answer": answer,
            "claim_audit_exemption": make_claim_audit_exemption(
                answer, CLAIM_AUDIT_EXEMPTION_GUIDANCE
            ),
            "evidence_ledger": [
                {
                    "evidence_id": "E001",
                    "chunk_id": "chunk:guide:2",
                    "source_type": "document",
                    "source": "guide.pdf",
                    "page": 2,
                    "span_start": 0,
                    "span_end": 10,
                    "display_citation": "[guide.pdf:P2]",
                }
            ],
        }
    )

    assert result == {"citation_ledger": []}
