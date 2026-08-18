import pytest
from pydantic import ValidationError
from cogdoc.api.schemas import (
    API_SCHEMA_VERSION,
    ChatRequest,
    CitationLedgerEntry,
    CitationOccurrence,
    ErrorCode,
    FeedbackRequest,
    TraceResponse,
    build_error_response,
    chat_result_to_response,
)
from cogdoc.service.chat_service import ChatResult


# 验证对话请求默认值和强制任务。
def test_chat_request_defaults_and_forced_task():
    request = ChatRequest(query="  总结 a.pdf  ", mode="summary")

    assert request.schema_version == API_SCHEMA_VERSION
    assert request.query == "总结 a.pdf"
    assert request.doc_id
    assert request.forced_task == "summary"
    assert ChatRequest(query="问题").forced_task is None


# 验证对话请求拒绝空白和未知字段。
def test_chat_request_rejects_blank_and_unknown_fields():
    with pytest.raises(ValidationError):
        ChatRequest(query="  ")

    with pytest.raises(ValidationError):
        ChatRequest(query="问题", unexpected=True)


# 验证对话结果响应不泄漏原始正文。
def test_chat_result_to_response_maps_stable_fields_without_raw_text():
    result = ChatResult(
        answer="需要满足报名要求。[a.pdf:P1]",
        task_type="qa",
        citations=[
            {
                "chunk_id": "chunk:a:1",
                "source": "a.pdf",
                "page": 1,
                "page_start": 1,
                "page_end": 2,
                "text": "不应进入 API 响应的全文",
            }
        ],
        evidence=[
            {
                "chunk_id": "chunk:a:1",
                "chunk_index": 0,
                "source": "a.pdf",
                "page": 1,
                "page_start": 1,
                "page_end": 2,
                "rerank_score": "0.98",
                "rewrite_query": "报名要求",
                "text_preview": "报名要求摘要",
                "retrieval": {
                    "search_channel": "derived_knowledge",
                    "matched_terms": ["报名"],
                },
                "text": "不应进入 API 响应的全文",
            }
        ],
        critique="",
        is_valid=True,
        trace_id="trace-1",
        request_id="trace-1",
        steps=[{"node_name": "runtime.setup"}],
        chat_messages=[{"role": "user", "content": "报名要求是什么"}],
        raw_output={"answer": "raw"},
        trace_path="/tmp/trace-1.json",
    )

    response = chat_result_to_response(result, doc_id="kb", session_id="s1")
    payload = response.model_dump()

    assert payload["schema_version"] == "v1"
    assert payload["doc_id"] == "kb"
    assert payload["session_id"] == "s1"
    assert payload["task_type"] == "qa"
    assert payload["answer"] == "需要满足报名要求。[a.pdf:P1]"
    assert payload["citations"] == [
        {
            "chunk_id": "chunk:a:1",
            "source_type": "document",
            "knowledge_id": "",
            "source": "a.pdf",
            "page": 1,
            "page_start": 1,
            "page_end": 2,
        }
    ]
    assert payload["evidence"][0]["rerank_score"] == 0.98
    assert payload["evidence"][0]["source_type"] == "document"
    assert payload["evidence"][0]["text_preview"] == "报名要求摘要"
    assert payload["evidence"][0]["retrieval"]["search_channel"] == (
        "derived_knowledge"
    )
    assert payload["evidence"][0]["retrieval"]["matched_terms"] == ["报名"]
    assert "raw_output" not in payload
    assert "steps" not in payload
    assert "trace_path" not in payload
    assert "不应进入 API 响应的全文" not in str(payload)
    assert payload["claim_audit"] is None
    assert payload["claim_verification"] is None


def test_chat_result_to_response_whitelists_public_citation_ledger_fields():
    sensitive_text = "不应公开的证据正文"
    sensitive_meta = "不应公开的私有元数据"
    answer = "报名要求见说明。[a.pdf:P1]"
    citation_start = answer.index("[a.pdf:P1]")
    result = ChatResult(
        answer=answer,
        task_type="qa",
        citations=[],
        evidence=[
            {
                "evidence_id": "E1000",
                "chunk_id": "chunk:a:1",
                "source_type": "document",
                "source": "a.pdf",
                "page": 1,
                "page_start": 1,
                "page_end": 1,
                "retrieval": {
                    "evidence_id": "E1000",
                    "evidence_text_start": 12,
                    "evidence_text_end": 36,
                },
            }
        ],
        critique="",
        is_valid=True,
        trace_id="trace-ledger",
        request_id="trace-ledger",
        steps=[],
        chat_messages=[],
        raw_output={
            "evidence_ledger": [
                {
                    "evidence_id": "E1000",
                    "chunk_id": "chunk:a:1",
                    "source_type": "document",
                    "source": "a.pdf",
                    "page": 1,
                    "page_start": 1,
                    "page_end": 1,
                    "span_start": 12,
                    "span_end": 36,
                    "text": sensitive_text,
                    "meta": {"private": sensitive_meta},
                }
            ]
        },
        citation_ledger=[
            {
                "evidence_id": "E1000",
                "chunk_id": "chunk:a:1",
                "source_type": "document",
                "source": "a.pdf",
                "page": 1,
                "page_start": 1,
                "page_end": 1,
                "span_start": 12,
                "span_end": 36,
                "display_citation": "[a.pdf:P1]",
                "text": sensitive_text,
                "meta": {"private": sensitive_meta},
                "occurrences": [
                    {
                        "index": 0,
                        "answer_start": citation_start,
                        "answer_end": citation_start + len("[a.pdf:P1]"),
                        "text": sensitive_text,
                        "private": sensitive_meta,
                    }
                ],
            }
        ],
    )

    payload = chat_result_to_response(result, doc_id="kb").model_dump()

    assert payload["citation_ledger"] == [
        {
            "evidence_id": "E1000",
            "chunk_id": "chunk:a:1",
            "source_type": "document",
            "knowledge_id": "",
            "source": "a.pdf",
            "page": 1,
            "page_start": 1,
            "page_end": 1,
            "span_start": 12,
            "span_end": 36,
            "occurrences": [
                {
                    "index": 0,
                    "answer_start": citation_start,
                    "answer_end": citation_start + len("[a.pdf:P1]"),
                }
            ],
        }
    ]
    serialized = str(payload)
    assert "display_citation" not in serialized
    assert sensitive_text not in serialized
    assert sensitive_meta not in serialized


def test_chat_result_to_response_uses_global_registry_for_repaired_citation():
    answer = "补充检索后的结论。[b.pdf:P2]"
    citation = "[b.pdf:P2]"
    citation_start = answer.index(citation)
    result = ChatResult(
        answer=answer,
        task_type="qa",
        citations=[],
        # 对外 evidence 仍是修复前候选，不包含最终被引用的 b.pdf。
        evidence=[
            {
                "evidence_id": "E001",
                "chunk_id": "chunk:a:1",
                "source": "a.pdf",
                "page": 1,
                "retrieval": {
                    "evidence_id": "E001",
                    "evidence_text_start": 0,
                    "evidence_text_end": 8,
                },
            }
        ],
        critique="",
        is_valid=True,
        trace_id="trace-repaired-ledger",
        request_id="trace-repaired-ledger",
        steps=[],
        chat_messages=[],
        # 全局 registry 冻结了初始候选和修复阶段补充使用的证据。
        raw_output={
            "evidence_ledger": [
                {
                    "evidence_id": "E001",
                    "chunk_id": "chunk:a:1",
                    "source_type": "document",
                    "source": "a.pdf",
                    "page": 1,
                    "span_start": 0,
                    "span_end": 8,
                },
                {
                    "evidence_id": "E002",
                    "chunk_id": "chunk:b:2",
                    "source_type": "document",
                    "source": "b.pdf",
                    "page": 2,
                    "span_start": 4,
                    "span_end": 18,
                },
            ]
        },
        citation_ledger=[
            {
                "evidence_id": "E002",
                "chunk_id": "chunk:b:2",
                "source_type": "document",
                "source": "b.pdf",
                "page": 2,
                "span_start": 4,
                "span_end": 18,
                "occurrences": [
                    {
                        "index": 0,
                        "answer_start": citation_start,
                        "answer_end": citation_start + len(citation),
                    }
                ],
            }
        ],
    )

    payload = chat_result_to_response(result, doc_id="kb").model_dump()

    assert [item["chunk_id"] for item in payload["evidence"]] == ["chunk:a:1"]
    assert len(payload["citation_ledger"]) == 1
    assert payload["citation_ledger"][0]["evidence_id"] == "E002"
    assert payload["citation_ledger"][0]["chunk_id"] == "chunk:b:2"


def test_chat_result_to_response_clears_entire_mixed_invalid_ledger():
    first = "[a.pdf:P1]"
    second = "[b.pdf:P2]"
    answer = f"结论{first}和{second}"
    first_start = answer.index(first)
    second_start = answer.index(second)
    result = ChatResult(
        answer=answer,
        task_type="qa",
        citations=[],
        evidence=[
            {"chunk_id": "c1", "source": "a.pdf", "page": 1},
            {"chunk_id": "c2", "source": "b.pdf", "page": 2},
        ],
        critique="",
        is_valid=True,
        trace_id="trace-invalid-ledger",
        request_id="trace-invalid-ledger",
        steps=[],
        chat_messages=[],
        raw_output={},
        citation_ledger=[
            {
                "evidence_id": "E001",
                "chunk_id": "c1",
                "source_type": "document",
                "source": "a.pdf",
                "page": 1,
                "span_start": 0,
                "span_end": 4,
                "occurrences": [
                    {
                        "index": 0,
                        "answer_start": first_start,
                        "answer_end": first_start + len(first),
                    }
                ],
            },
            {
                "evidence_id": "E01000",
                "chunk_id": "c2",
                "source_type": "document",
                "source": "b.pdf",
                "page": 2,
                "span_start": 0,
                "span_end": 4,
                "occurrences": [
                    {
                        "index": 1,
                        "answer_start": second_start,
                        "answer_end": second_start + len(second),
                    }
                ],
            },
        ],
    )

    payload = chat_result_to_response(result, doc_id="kb").model_dump()

    assert payload["citation_ledger"] == []
    assert payload["critique"] == "回答未通过引用或声明证据校验。"
    assert payload["is_valid"] is False
    assert "E1000" not in payload["critique"]


def test_public_citation_models_accept_canonical_four_digit_evidence_id():
    entry = CitationLedgerEntry(
        evidence_id="E1000",
        chunk_id="c1",
        source="a.pdf",
        page=1,
        span_start=0,
        span_end=4,
        occurrences=[CitationOccurrence(index=0, answer_start=0, answer_end=10)],
    )

    assert entry.evidence_id == "E1000"


def test_public_citation_models_reject_noncanonical_ids_and_ranges():
    with pytest.raises(ValidationError):
        CitationLedgerEntry(
            evidence_id="E01000",
            chunk_id="c1",
            source="a.pdf",
            page=1,
            span_start=0,
            span_end=4,
            occurrences=[CitationOccurrence(index=0, answer_start=0, answer_end=10)],
        )

    with pytest.raises(ValidationError):
        CitationOccurrence(index=0, answer_start=10, answer_end=10)


def test_feedback_request_forbids_client_supplied_citation_ledger():
    with pytest.raises(ValidationError):
        FeedbackRequest(
            trace_id="trace-1",
            feedback="thumbs_down",
            citation_ledger=[],
        )


# 验证公开响应只暴露声明审计摘要，不泄漏逐条声明、理由或证据。
def test_chat_result_to_response_exposes_only_safe_claim_audit_summary():
    sensitive_claim = "敏感声明正文：报名费为 999 元"
    sensitive_reason = "敏感逐条理由：金额与证据不一致"
    sensitive_evidence = "敏感证据全文"
    result = ChatResult(
        answer="生成内容未通过校验。",
        task_type="qa",
        citations=[],
        evidence=[],
        critique="声明证据校验未通过",
        is_valid=False,
        trace_id="trace-audit",
        request_id="trace-audit",
        steps=[],
        chat_messages=[],
        raw_output={
            "answer": "原始答案",
            "claim_audit": {
                "status": "rejected",
                "reason_code": "claims_not_supported",
                "claims": [
                    {
                        "claim_id": "c1",
                        "text": sensitive_claim,
                        "verdict": "unsupported",
                        "reason": sensitive_reason,
                        "evidence": sensitive_evidence,
                    }
                ],
                "counts": {
                    "claim_count": 1,
                    "supported": 0,
                    "unsupported": 1,
                    "insufficient": 0,
                    "cited": 0,
                    "skipped_statements": 0,
                },
                "metrics": {
                    "claim_support_rate": 0.0,
                    "citation_coverage": 0.0,
                    "unsupported_claim_rate": 1.0,
                },
                "repair": {
                    "attempted": True,
                    "attempt_count": 1,
                    "succeeded": False,
                    "private_note": sensitive_reason,
                },
                "verifier": {
                    "duration_ms": 87.5,
                    "reason": sensitive_reason,
                    "evidence": sensitive_evidence,
                },
            },
        },
    )

    payload = chat_result_to_response(result, doc_id="kb").model_dump()

    assert payload["claim_audit"] == {
        "status": "rejected",
        "reason_code": "claims_not_supported",
        "counts": result.raw_output["claim_audit"]["counts"],
        "metrics": result.raw_output["claim_audit"]["metrics"],
        "repair": {
            "attempted": True,
            "attempt_count": 1,
            "succeeded": False,
        },
        "duration_ms": 87.5,
    }
    serialized = str(payload)
    assert sensitive_claim not in serialized
    assert sensitive_reason not in serialized
    assert sensitive_evidence not in serialized
    assert "claims" not in payload["claim_audit"]
    assert "verifier" not in payload["claim_audit"]


def test_chat_result_to_response_exposes_bounded_claim_rollout_summary():
    result = ChatResult(
        answer="灰度候选答案。",
        task_type="qa",
        citations=[],
        evidence=[],
        critique="",
        is_valid=True,
        trace_id="trace-shadow",
        request_id="trace-shadow",
        steps=[],
        chat_messages=[],
        raw_output={
            "claim_verification_rollout": {
                "version": "future-version",
                "mode": "shadow",
                "configured_mode": "enforce",
                "rollout_percent": 25.0,
                "cohort_bucket": 1234,
                "cohort_selected": False,
                "fallback_mode": "shadow",
                "policy_id": "0123456789abcdef",
                "decision": "would_block",
                "executed": True,
                "enforced": False,
                "released": True,
                "would_intervene": True,
                "would_repair": False,
                "would_block": True,
                "audit_status": "error",
                "reason_code": "verifier_timeout",
                "repair_count": 2,
                "private_claim": "不得进入公开响应的声明正文",
            }
        },
    )

    summary = chat_result_to_response(result, doc_id="kb").model_dump()[
        "claim_verification"
    ]

    assert summary == {
        "version": "v1",
        "mode": "shadow",
        "configured_mode": "enforce",
        "rollout_percent": 25.0,
        "cohort_bucket": 1234,
        "cohort_selected": False,
        "fallback_mode": "shadow",
        "policy_id": "0123456789abcdef",
        "decision": "would_block",
        "executed": True,
        "enforced": False,
        "released": True,
        "would_intervene": True,
        "would_repair": False,
        "would_block": True,
        "audit_status": "error",
        "reason_code": "verifier_timeout",
        "repair_count": 2,
    }
    assert "private_claim" not in summary


# 验证零事实声明的比率保持不可用，而不是伪装成零分或满分。
def test_chat_result_to_response_preserves_none_rates_for_zero_claims():
    result = ChatResult(
        answer="仅包含标题。",
        task_type="summary",
        citations=[],
        evidence=[],
        critique="",
        is_valid=True,
        trace_id="trace-empty-audit",
        request_id="trace-empty-audit",
        steps=[],
        chat_messages=[],
        raw_output={
            "claim_audit": {
                "status": "passed",
                "reason_code": "no_factual_statements",
                "claims": [],
                "counts": {
                    "claim_count": 0,
                    "supported": 0,
                    "unsupported": 0,
                    "insufficient": 0,
                    "cited": 0,
                    "skipped_statements": 0,
                },
                "metrics": {
                    "claim_support_rate": None,
                    "citation_coverage": None,
                    "unsupported_claim_rate": None,
                },
                "repair": {
                    "attempted": False,
                    "attempt_count": 0,
                    "succeeded": False,
                },
                "verifier": {"duration_ms": 0.0},
            }
        },
    )

    summary = chat_result_to_response(result, doc_id="kb").model_dump()["claim_audit"]

    assert summary["counts"]["claim_count"] == 0
    assert summary["metrics"] == {
        "claim_support_rate": None,
        "citation_coverage": None,
        "unsupported_claim_rate": None,
    }
    assert summary["duration_ms"] == 0.0


# 验证未知任务类型会归一化。
def test_chat_result_to_response_normalizes_unknown_task():
    result = ChatResult(
        answer="无法识别",
        task_type="other",
        citations=[],
        evidence=[],
        critique="",
        is_valid=True,
        trace_id="trace-2",
        request_id="trace-2",
        steps=[],
        chat_messages=[],
        raw_output={},
    )

    response = chat_result_to_response(result, doc_id="kb")

    assert response.task_type == "unknown"


# 验证错误响应使用稳定错误码。
def test_error_response_uses_stable_error_code_values():
    response = build_error_response(
        ErrorCode.STREAM_INTERRUPTED,
        "stream closed",
        request_id="req-1",
        trace_id="trace-1",
        details={"stage": "stream"},
    )

    assert response.model_dump() == {
        "schema_version": "v1",
        "error_code": "STREAM_INTERRUPTED",
        "message": "stream closed",
        "request_id": "req-1",
        "trace_id": "trace-1",
        "details": {"stage": "stream"},
    }


# 验证跟踪响应使用稳定契约。
def test_trace_response_uses_stable_contract():
    response = TraceResponse(
        trace_id="trace-1",
        request_id="req-1",
        task_type="qa",
        status="ok",
        duration_ms=1.0,
        config={"doc_id": "kb"},
        summary={"step_count": 1, "node_names": ["intent_router"]},
        steps=[{"node_name": "intent_router"}],
    )
    payload = response.model_dump()

    assert payload["schema_version"] == "v1"
    assert payload["trace_id"] == "trace-1"
    assert payload["summary"]["step_count"] == 1
    assert payload["summary"]["error_count"] == 0
    assert payload["summary"]["claim_audit"] is None
    assert payload["summary"]["claim_verification"] is None
    assert payload["steps"][0]["node_name"] == "intent_router"
