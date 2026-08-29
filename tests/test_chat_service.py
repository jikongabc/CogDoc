import pytest
from cogdoc.agents.answer_markers import NO_RELEVANT_CONTENT_ANSWER
from cogdoc.agents.claim_evidence_verifier import (
    CLAIM_AUDIT_BLOCKED_ANSWER,
    CLAIM_AUDIT_EXEMPTION_GUIDANCE,
    make_claim_audit_exemption,
)
from cogdoc.config.settings import Settings
from cogdoc.service import chat_service
from cogdoc.service.chat_service import ChatServiceError, run_chat_sync


# 构造测试用文档。
def _doc() -> dict:
    return {
        "text": "报名要求。",
        "meta": {
            "chunk_id": "chunk:a:1",
            "source": "a.pdf",
            "page": 1,
            "page_start": 1,
            "page_end": 1,
            "chunk_index": 0,
            "local_chunk_index": 0,
            "source_sha256": "sha",
            "origin": "file",
        },
        "retrieval": {"rrf_score": 0.1},
    }


def _public_ledger(answer: str) -> list[dict]:
    citation = "[a.pdf:P1]"
    start = answer.index(citation)
    return [
        {
            "evidence_id": "E001",
            "chunk_id": "chunk:a:1",
            "source_type": "document",
            "source": "a.pdf",
            "page": 1,
            "span_start": 0,
            "span_end": 5,
            "occurrences": [
                {
                    "index": 0,
                    "answer_start": start,
                    "answer_end": start + len(citation),
                }
            ],
        }
    ]


# 定义假应用数据结构。
class FakeApp:
    # 流式返回结果。
    def stream(self, initial_state, config, stream_mode, subgraphs):
        assert initial_state["trace_id"]
        assert config["configurable"]["trace_id"] == initial_state["trace_id"]
        yield (
            (),
            "updates",
            {
                "intent_router": {
                    "query": "报名要求是什么",
                    "doc_id": "kb",
                    "is_local": False,
                    "task_type": "qa",
                    "router_reason": "用户询问信息",
                }
            },
        )
        yield (
            ("qa_subgraph",),
            "updates",
            {"rewrite_node": {"rewritten_queries": ["报名要求"]}},
        )
        yield (
            ("qa_subgraph",),
            "updates",
            {"retrieve_node": {"retrieved_docs": [_doc()]}},
        )
        yield (
            ("qa_subgraph",),
            "updates",
            {"citation_node": {"critique": "", "iteration_count": 1}},
        )
        yield (
            (),
            "updates",
            {
                "qa_subgraph": {
                    "answer": (answer := "需要满足报名要求。[a.pdf:P1]"),
                    "critique": "",
                    "reranked_docs": [_doc()],
                    "sources": [_doc()["meta"]],
                    "evidence": [{"chunk_id": "chunk:a:1", "source": "a.pdf"}],
                    "citation_ledger": _public_ledger(answer),
                }
            },
        )


# 验证同步对话返回结构化结果。
def test_run_chat_sync_returns_structured_result(monkeypatch):
    monkeypatch.setattr(chat_service, "app", FakeApp())
    monkeypatch.setattr(chat_service, "configure_logging", lambda: None)
    monkeypatch.setattr(chat_service, "log_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(chat_service, "export_trace", lambda **kwargs: None)

    result = run_chat_sync("kb", "报名要求是什么", is_local=False)

    assert result.task_type == "qa"
    assert result.answer == "需要满足报名要求。[a.pdf:P1]"
    assert result.is_valid is True
    assert result.citations[0]["source"] == "a.pdf"
    assert result.evidence[0]["chunk_id"] == "chunk:a:1"
    assert result.chat_messages
    assert [step["node_name"] for step in result.steps][:2] == [
        "runtime.setup",
        "intent_router",
    ]
    assert any(step["retrieval_top_k"] == 9 for step in result.steps)


# 验证对话会导出可审计跟踪。
def test_run_chat_exports_auditable_trace(monkeypatch):
    exported = []
    monkeypatch.setattr(chat_service, "app", FakeApp())
    monkeypatch.setattr(chat_service, "configure_logging", lambda: None)
    monkeypatch.setattr(chat_service, "log_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        chat_service, "export_trace", lambda **kwargs: exported.append(kwargs)
    )
    monkeypatch.setattr(
        chat_service,
        "shared_epoch_store",
        lambda: type("Epoch", (), {"current": lambda self, _storage_id: 7})(),
    )

    result = run_chat_sync("kb", "报名要求是什么", is_local=False)

    assert result.trace_path is None
    assert exported[0]["status"] == "ok"
    assert exported[0]["task_type"] == "qa"
    assert exported[0]["duration_ms"] >= 0
    assert exported[0]["config"]["doc_id"] == "kb"
    assert exported[0]["config"]["kb_epoch"] == 7
    assert exported[0]["config"]["query_preview"] == "报名要求是什么"
    assert exported[0]["config"]["query_length"] == len("报名要求是什么")
    assert exported[0]["error"] is None


# 验证流式对话事件顺序稳定。
def test_run_chat_emits_golden_event_sequence(monkeypatch):
    monkeypatch.setattr(chat_service, "app", FakeApp())
    monkeypatch.setattr(chat_service, "configure_logging", lambda: None)
    monkeypatch.setattr(chat_service, "log_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(chat_service, "export_trace", lambda **kwargs: None)

    events = list(chat_service.run_chat("kb", "报名要求是什么", is_local=False))

    assert [event.type for event in events] == [
        "request_started",
        "router_decided",
        "rewrite_queries",
        "citation_passed",
        "final",
    ]


# 低置信度检索只发拒答进度并返回稳定无答案结果，不进入引用校验事件。
class RetrievalAbstainApp:
    def stream(self, initial_state, config, stream_mode, subgraphs):
        yield (
            (),
            "updates",
            {"intent_router": {"task_type": "qa", "router_reason": "文档问答"}},
        )
        yield (
            ("qa_subgraph",),
            "updates",
            {
                "rerank_node": {
                    "retrieval_abstained": True,
                    "retrieval_confidence": 0.91,
                    "retrieval_abstain_reason": "below_threshold",
                    "retrieval_signals": {"distance": 0.95, "bm25_score": 5.0},
                    "reranked_docs": [_doc()],
                }
            },
        )
        yield (
            (),
            "updates",
            {
                "qa_subgraph": {
                    "answer": NO_RELEVANT_CONTENT_ANSWER,
                    "critique": "",
                    "retrieval_abstained": True,
                    "reranked_docs": [],
                    "sources": [],
                    "evidence": [],
                }
            },
        )


def test_run_chat_emits_retrieval_abstention_event(monkeypatch):
    monkeypatch.setattr(chat_service, "app", RetrievalAbstainApp())
    monkeypatch.setattr(chat_service, "configure_logging", lambda: None)
    monkeypatch.setattr(chat_service, "log_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(chat_service, "export_trace", lambda **kwargs: None)

    events = list(chat_service.run_chat("kb", "无关问题", is_local=False))

    assert [event.type for event in events] == [
        "request_started",
        "router_decided",
        "retrieval_abstained",
        "final",
    ]
    assert events[2].payload == {"confidence": 0.91, "reason": "below_threshold"}
    result = events[-1].payload["result"]
    assert result.answer == NO_RELEVANT_CONTENT_ANSWER
    assert result.citations == []
    assert result.evidence == []


# 二阶段证据不足会发出独立进度事件并返回稳定拒答。
class EvidenceRejectedApp:
    def stream(self, initial_state, config, stream_mode, subgraphs):
        yield (
            (),
            "updates",
            {"intent_router": {"task_type": "qa", "router_reason": "文档问答"}},
        )
        yield (
            ("qa_subgraph",),
            "updates",
            {
                "rerank_node": {
                    "retrieval_abstained": True,
                    "retrieval_confidence": 0.9,
                    "retrieval_abstain_reason": "below_threshold",
                    "evidence_verification_pending": True,
                    "reranked_docs": [_doc()],
                }
            },
        )
        yield (
            ("qa_subgraph",),
            "updates",
            {
                "evidence_verify_node": {
                    "evidence_verification_required": True,
                    "evidence_supported": False,
                    "evidence_verification_reason": "缺少明确报销比例",
                    "evidence_verified_chunk_ids": [],
                    "retrieval_abstained": True,
                    "retrieval_abstain_reason": "evidence_not_supported",
                }
            },
        )
        yield (
            (),
            "updates",
            {
                "qa_subgraph": {
                    "answer": NO_RELEVANT_CONTENT_ANSWER,
                    "critique": "",
                    "retrieval_abstained": True,
                    "reranked_docs": [],
                    "sources": [],
                    "evidence": [],
                }
            },
        )


def test_run_chat_emits_evidence_rejected_event(monkeypatch):
    monkeypatch.setattr(chat_service, "app", EvidenceRejectedApp())
    monkeypatch.setattr(chat_service, "configure_logging", lambda: None)
    monkeypatch.setattr(chat_service, "log_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(chat_service, "export_trace", lambda **kwargs: None)

    events = list(chat_service.run_chat("kb", "报销比例是多少", is_local=False))

    assert [event.type for event in events] == [
        "request_started",
        "router_decided",
        "evidence_rejected",
        "final",
    ]
    assert events[2].payload == {
        "supported": False,
        "reason": "缺少明确报销比例",
        "evidence_chunk_ids": [],
    }
    assert events[-1].payload["result"].answer == NO_RELEVANT_CONTENT_ANSWER


class EvidenceReasonWithEidApp(EvidenceRejectedApp):
    def stream(self, initial_state, config, stream_mode, subgraphs):
        for namespace, mode, data in super().stream(
            initial_state, config, stream_mode, subgraphs
        ):
            if "evidence_verify_node" in data:
                verify_output = data["evidence_verify_node"]
                verify_output["evidence_verification_reason"] = (
                    "证据 [E001] 未覆盖报销比例"
                )
                verify_output["evidence_requirement_assessments"] = [
                    {
                        "requirement_id": "r1",
                        "supported": False,
                        "reason": "要求 r1 在 [E001] 中缺少明确数值",
                    }
                ]
            yield namespace, mode, data


def test_evidence_verification_event_hides_internal_eids(monkeypatch):
    monkeypatch.setattr(chat_service, "app", EvidenceReasonWithEidApp())
    monkeypatch.setattr(chat_service, "configure_logging", lambda: None)
    monkeypatch.setattr(chat_service, "log_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(chat_service, "export_trace", lambda **kwargs: None)

    events = list(chat_service.run_chat("kb", "报销比例是多少", is_local=False))

    rejected = next(event for event in events if event.type == "evidence_rejected")
    assert "[E001]" not in str(rejected.payload)
    assert rejected.payload["reason"] == ("证据校验已完成，内部证据标识已隐藏。")
    assert rejected.payload["requirement_assessments"][0]["reason"] == (
        "证据校验已完成，内部证据标识已隐藏。"
    )
    assert chat_service._public_verification_reason("证据 E001 不足") == (
        "证据校验已完成，内部证据标识已隐藏。"
    )
    assert chat_service._public_verification_reason("证据 [E-ID:002] 不足") == (
        "证据校验已完成，内部证据标识已隐藏。"
    )
    assert chat_service._public_verification_reason("普通校验原因") == "普通校验原因"


# 补检索中的低置信度不应提前发终态拒答，且 retry 事件与 trace 步骤各出现一次。
class AdaptiveRetryApp:
    def stream(self, initial_state, config, stream_mode, subgraphs):
        yield (
            (),
            "updates",
            {"intent_router": {"task_type": "qa", "router_reason": "文档问答"}},
        )
        yield (
            ("qa_subgraph",),
            "updates",
            {
                "rerank_node": {
                    "retrieval_abstained": True,
                    "retrieval_confidence": 0.4,
                    "retrieval_abstain_reason": "below_threshold",
                    "evidence_verification_pending": False,
                    "adaptive_retrieval_retry_pending": True,
                }
            },
        )
        yield (
            ("qa_subgraph",),
            "updates",
            {
                "retrieval_retry_node": {
                    "retrieval_retry_count": 1,
                    "retrieval_round": 1,
                    "retrieval_retry_reason": "missing_requirements",
                    "missing_evidence_requirement_ids": ["r2"],
                }
            },
        )
        yield (
            ("qa_subgraph",),
            "updates",
            {
                "rerank_node": {
                    "retrieval_abstained": False,
                    "retrieval_confidence": 0.9,
                    "retrieval_abstain_reason": "supported",
                    "evidence_verification_pending": False,
                    "adaptive_retrieval_retry_pending": False,
                }
            },
        )
        yield (
            (),
            "updates",
            {
                "qa_subgraph": {
                    "answer": "补检索后的答案。[a.pdf:P1]",
                    "critique": "",
                    "retrieval_abstained": False,
                    "retrieval_retry_count": 1,
                    "reranked_docs": [_doc()],
                    "sources": [_doc()["meta"]],
                    "evidence": [
                        {"chunk_id": "chunk:a:1", "source": "a.pdf", "page": 1}
                    ],
                }
            },
        )


def test_run_chat_emits_one_retry_without_premature_abstention(monkeypatch):
    monkeypatch.setattr(chat_service, "app", AdaptiveRetryApp())
    monkeypatch.setattr(chat_service, "configure_logging", lambda: None)
    monkeypatch.setattr(chat_service, "log_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(chat_service, "export_trace", lambda **kwargs: None)

    events = list(chat_service.run_chat("kb", "比较 A 和 B", is_local=False))

    assert [event.type for event in events] == [
        "request_started",
        "router_decided",
        "retrieval_retry",
        "final",
    ]
    assert events[2].payload == {
        "retry_count": 1,
        "reason": "missing_requirements",
        "missing_requirement_ids": ["r2"],
    }
    step_names = [step["node_name"] for step in events[-1].payload["result"].steps]
    assert sum(name.endswith(".rerank_node") for name in step_names) == 2
    assert sum(name.endswith(".retrieval_retry_node") for name in step_names) == 1


# 补检索状态已经产生但尚无答案时发生中断，局部状态不能被包装成空 final。
class StreamInterruptAfterRetryApp:
    def stream(self, initial_state, config, stream_mode, subgraphs):
        yield (
            (),
            "updates",
            {"intent_router": {"task_type": "qa", "router_reason": "文档问答"}},
        )
        yield (
            ("qa_subgraph",),
            "updates",
            {
                "retrieval_retry_node": {
                    "retrieval_retry_count": 1,
                    "retrieval_round": 1,
                    "retrieval_retry_reason": "missing_requirements",
                    "missing_evidence_requirement_ids": ["r2"],
                }
            },
        )
        raise TimeoutError("补检索流中断")


def test_stream_interrupt_after_retry_does_not_emit_empty_final(monkeypatch):
    exported = []
    monkeypatch.setattr(chat_service, "app", StreamInterruptAfterRetryApp())
    monkeypatch.setattr(chat_service, "configure_logging", lambda: None)
    monkeypatch.setattr(chat_service, "log_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        chat_service, "export_trace", lambda **kwargs: exported.append(kwargs)
    )

    events = list(chat_service.run_chat("kb", "比较 A 和 B", is_local=False))

    assert [event.type for event in events] == [
        "request_started",
        "router_decided",
        "retrieval_retry",
        "error",
    ]
    assert exported[0]["status"] == "failed"
    assert exported[0]["execution_status"] == "TARGET_ERROR"


# 模型流已有候选 token、图却未落地最终答案时，不能伪装成空成功结果。
class BufferedWithoutFinalAnswerApp:
    def stream(self, initial_state, config, stream_mode, subgraphs):
        class CandidateMessage:
            content = "候选答案。[E001]"

        yield (
            (),
            "updates",
            {"intent_router": {"task_type": "qa", "router_reason": "文档问答"}},
        )
        yield (("qa_subgraph",), "messages", CandidateMessage())


def test_buffered_tokens_without_final_answer_emit_runtime_error(monkeypatch):
    settings = Settings(_env_file=None, claim_verification_enabled=False)
    exported = []
    monkeypatch.setattr(chat_service, "app", BufferedWithoutFinalAnswerApp())
    monkeypatch.setattr(chat_service, "get_settings", lambda: settings)
    monkeypatch.setattr(chat_service, "configure_logging", lambda: None)
    monkeypatch.setattr(chat_service, "log_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        chat_service, "export_trace", lambda **kwargs: exported.append(kwargs)
    )

    events = list(chat_service.run_chat("kb", "报名要求是什么", is_local=False))

    assert [event.type for event in events] == [
        "request_started",
        "router_decided",
        "error",
    ]
    error = events[-1].payload
    assert error["stage"] == "runtime"
    assert error["error_class"] == "RuntimeError"
    assert "no releasable final answer" in error["message"]
    assert not any(event.type in {"token", "final"} for event in events)
    assert exported[0]["status"] == "failed"
    assert exported[0]["execution_status"] == "TARGET_ERROR"
    assert exported[0]["error"]["stage"] == "runtime"
    assert exported[0]["steps"][-1]["node_name"] == ("runtime.missing_final_answer")


def test_buffered_tokens_without_final_answer_raise_in_sync_mode(monkeypatch):
    settings = Settings(_env_file=None, claim_verification_enabled=False)
    monkeypatch.setattr(chat_service, "app", BufferedWithoutFinalAnswerApp())
    monkeypatch.setattr(chat_service, "get_settings", lambda: settings)
    monkeypatch.setattr(chat_service, "configure_logging", lambda: None)
    monkeypatch.setattr(chat_service, "log_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(chat_service, "export_trace", lambda **kwargs: None)

    with pytest.raises(ChatServiceError) as excinfo:
        run_chat_sync("kb", "报名要求是什么", is_local=False)

    assert excinfo.value.stage == "runtime"
    assert excinfo.value.error_class == "RuntimeError"
    assert "no releasable final answer" in excinfo.value.message


# 路由后流式迭代中途崩溃，父子图始终未产出可信输出。
class StreamInterruptApp:
    # 路由后流式迭代中途崩溃，父子图始终未产出可信输出。
    def stream(self, initial_state, config, stream_mode, subgraphs):
        yield (
            (),
            "updates",
            {"intent_router": {"task_type": "qa", "router_reason": "x"}},
        )
        raise TimeoutError("流中断")


# 验证无可信输出时流式中断会抛错。
def test_run_chat_sync_raises_on_stream_interrupt_without_output(monkeypatch):
    monkeypatch.setattr(chat_service, "app", StreamInterruptApp())
    monkeypatch.setattr(chat_service, "configure_logging", lambda: None)
    monkeypatch.setattr(chat_service, "log_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(chat_service, "export_trace", lambda **kwargs: None)

    with pytest.raises(ChatServiceError) as excinfo:
        run_chat_sync("kb", "报名要求是什么", is_local=False)

    assert excinfo.value.stage == "stream"
    assert excinfo.value.error_class == "TimeoutError"


# 验证无可信输出时跟踪标记失败。
def test_run_chat_exports_failed_trace_on_stream_interrupt(monkeypatch):
    exported = []
    monkeypatch.setattr(chat_service, "app", StreamInterruptApp())
    monkeypatch.setattr(chat_service, "configure_logging", lambda: None)
    monkeypatch.setattr(chat_service, "log_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        chat_service, "export_trace", lambda **kwargs: exported.append(kwargs)
    )

    with pytest.raises(ChatServiceError):
        run_chat_sync("kb", "报名要求是什么", is_local=False)

    assert exported[0]["status"] == "failed"
    assert exported[0]["execution_status"] == "TARGET_ERROR"
    assert exported[0]["error"]["stage"] == "stream"
    assert exported[0]["error"]["error_class"] == "TimeoutError"


# 父子图输出已落地后流才中断，属于可降级返回而非彻底失败。
class StreamInterruptWithPartialApp:
    # 父子图输出已落地后流才中断，属于可降级返回而非彻底失败。
    def stream(self, initial_state, config, stream_mode, subgraphs):
        yield (
            (),
            "updates",
            {"intent_router": {"task_type": "qa", "router_reason": "x"}},
        )
        yield (
            (),
            "updates",
            {
                "qa_subgraph": {
                    "answer": "部分答案",
                    "critique": "",
                    "reranked_docs": [],
                }
            },
        )
        raise TimeoutError("流中断")


# 验证部分输出已落地时可降级返回。
def test_run_chat_sync_returns_degraded_result_when_partial_output(monkeypatch):
    monkeypatch.setattr(chat_service, "app", StreamInterruptWithPartialApp())
    monkeypatch.setattr(chat_service, "configure_logging", lambda: None)
    monkeypatch.setattr(chat_service, "log_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(chat_service, "export_trace", lambda **kwargs: None)

    result = run_chat_sync("kb", "报名要求是什么", is_local=False)

    assert result.raw_output.get("answer") == "部分答案"


# 验证部分输出已落地时跟踪标记降级。
def test_run_chat_exports_degraded_trace_when_partial_output(monkeypatch):
    exported = []
    monkeypatch.setattr(chat_service, "app", StreamInterruptWithPartialApp())
    monkeypatch.setattr(chat_service, "configure_logging", lambda: None)
    monkeypatch.setattr(chat_service, "log_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        chat_service, "export_trace", lambda **kwargs: exported.append(kwargs)
    )

    result = run_chat_sync("kb", "报名要求是什么", is_local=False)

    assert result.raw_output.get("answer") == "部分答案"
    assert exported[0]["status"] == "degraded"
    assert exported[0]["execution_status"] == "TRACE_INCOMPLETE"
    assert exported[0]["error"]["stage"] == "stream"


# 验证门禁开启时，审计前流中断也不能把部分候选答案作为降级结果释放。
def test_claim_gate_blocks_partial_output_when_stream_ends_before_audit(monkeypatch):
    settings = Settings(_env_file=None, claim_verification_enabled=True)
    monkeypatch.setattr(chat_service, "app", StreamInterruptWithPartialApp())
    monkeypatch.setattr(chat_service, "get_settings", lambda: settings)
    monkeypatch.setattr(chat_service, "configure_logging", lambda: None)
    monkeypatch.setattr(chat_service, "log_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(chat_service, "export_trace", lambda **kwargs: None)

    events = list(chat_service.run_chat("kb", "报名要求是什么", is_local=False))

    token_events = [event for event in events if event.type == "token"]
    assert len(token_events) == 1
    assert "部分答案" not in token_events[0].payload["content"]
    assert token_events[0].payload["verified"] is False
    result = next(event.payload["result"] for event in events if event.type == "final")
    assert result.raw_output["claim_audit"]["status"] == "rejected"
    assert result.raw_output["claim_audit"]["reason_code"] == "audit_incomplete"
    assert result.is_valid is False


class ClaimGateStreamingApp:
    def stream(self, initial_state, config, stream_mode, subgraphs):
        class CandidateMessage:
            content = "未经验证的候选答案"

        yield (
            (),
            "updates",
            {"intent_router": {"task_type": "qa", "router_reason": "文档问答"}},
        )
        # LangGraph 的 messages 流会在审计前吐出生成候选；服务层必须压住它。
        yield (("qa_subgraph",), "messages", CandidateMessage())
        yield (
            (),
            "updates",
            {
                "qa_subgraph": {
                    "answer": (answer := "最终验证答案。[a.pdf:P1]"),
                    "critique": "",
                    "reranked_docs": [_doc()],
                    "sources": [_doc()["meta"]],
                    "evidence": [
                        {"chunk_id": "chunk:a:1", "source": "a.pdf", "page": 1}
                    ],
                    "citation_ledger": _public_ledger(answer),
                }
            },
        )
        yield (
            (),
            "updates",
            {
                "claim_audit_node": {
                    "claim_audit_required": True,
                    "claim_audit_passed": True,
                    "claim_audit": {
                        "status": "passed",
                        "counts": {
                            "claim_count": 1,
                            "supported": 1,
                            "unsupported": 0,
                            "insufficient": 0,
                        },
                        "metrics": {
                            "claim_support_rate": 1.0,
                            "citation_coverage": 1.0,
                            "unsupported_claim_rate": 0.0,
                        },
                    },
                }
            },
        )


class ShadowFailedClaimApp:
    def stream(self, initial_state, config, stream_mode, subgraphs):
        yield (
            (),
            "updates",
            {"intent_router": {"task_type": "qa", "router_reason": "文档问答"}},
        )
        answer = "灰度期间仍交付的候选答案。[a.pdf:P1]"
        yield (
            (),
            "updates",
            {
                "qa_subgraph": {
                    "answer": answer,
                    "critique": "",
                    "reranked_docs": [_doc()],
                    "sources": [_doc()["meta"]],
                    "evidence": [
                        {"chunk_id": "chunk:a:1", "source": "a.pdf", "page": 1}
                    ],
                    "citation_ledger": _public_ledger(answer),
                }
            },
        )
        yield (
            (),
            "updates",
            {
                "claim_audit_node": {
                    "claim_audit_required": True,
                    "claim_audit_passed": False,
                    "claim_audit": {
                        "status": "failed",
                        "reason_code": "unsupported_claims",
                        "counts": {
                            "claim_count": 1,
                            "supported": 0,
                            "unsupported": 1,
                            "insufficient": 0,
                        },
                    },
                }
            },
        )


class ClaimGateCitationRetryApp:
    def stream(self, initial_state, config, stream_mode, subgraphs):
        yield (
            (),
            "updates",
            {"intent_router": {"task_type": "qa", "router_reason": "文档问答"}},
        )
        yield (
            ("qa_subgraph",),
            "updates",
            {"generate_node": {"answer": "未验证且引用错误的候选答案。[a.pdf:P99]"}},
        )
        yield (
            ("qa_subgraph",),
            "updates",
            {
                "citation_node": {
                    "critique": "页码不存在，内部证据 [E999] 无效",
                    "iteration_count": 2,
                    "max_iteration_count": 2,
                }
            },
        )
        final_output = {
            "answer": "最终验证答案。[a.pdf:P1]",
            "critique": "",
            "reranked_docs": [_doc()],
            "sources": [_doc()["meta"]],
            "evidence": [{"chunk_id": "chunk:a:1", "source": "a.pdf", "page": 1}],
            "citation_ledger": _public_ledger("最终验证答案。[a.pdf:P1]"),
        }
        yield ((), "updates", {"qa_subgraph": final_output})
        yield (
            (),
            "updates",
            {
                "claim_audit_node": {
                    "claim_audit_required": True,
                    "claim_audit_passed": True,
                    "claim_audit": {
                        "status": "passed",
                        "counts": {"claim_count": 1, "supported": 1},
                        "metrics": {"claim_support_rate": 1.0},
                    },
                }
            },
        )


class CitationLedgerStreamingApp:
    def stream(self, initial_state, config, stream_mode, subgraphs):
        class CandidateMessage:
            content = "候选答案。[E001]"

        public_ledger = [
            {
                "evidence_id": "E001",
                "chunk_id": "chunk:a:1",
                "source_type": "document",
                "source": "a.pdf",
                "page": 1,
                "page_start": 1,
                "page_end": 1,
                "span_start": 0,
                "span_end": 5,
                "occurrences": [{"index": 0, "answer_start": 5, "answer_end": 15}],
            }
        ]
        yield (
            (),
            "updates",
            {"intent_router": {"task_type": "qa", "router_reason": "文档问答"}},
        )
        yield (("qa_subgraph",), "messages", CandidateMessage())
        yield (
            (),
            "updates",
            {
                "qa_subgraph": {
                    "answer": "候选答案。[E001]",
                    "critique": "",
                    "reranked_docs": [_doc()],
                    "sources": [_doc()["meta"]],
                    "evidence": [
                        {"chunk_id": "chunk:a:1", "source": "a.pdf", "page": 1}
                    ],
                    "evidence_ledger": [
                        {
                            "evidence_id": "E001",
                            "chunk_id": "chunk:a:1",
                            "display_citation": "[a.pdf:P1]",
                        }
                    ],
                }
            },
        )
        yield (
            (),
            "updates",
            {
                "claim_audit_node": {
                    "claim_audit_required": True,
                    "claim_audit_passed": True,
                    "claim_audit": {
                        "status": "passed",
                        "counts": {"claim_count": 1, "supported": 1},
                        "metrics": {"claim_support_rate": 1.0},
                    },
                }
            },
        )
        yield (
            (),
            "updates",
            {
                "citation_finalize_node": {
                    "answer": "候选答案。[a.pdf:P1]",
                    "citation_ledger": public_ledger,
                }
            },
        )


@pytest.mark.parametrize("claim_gate_enabled", [False, True])
def test_audited_stream_buffers_eids_until_citation_finalizer(
    monkeypatch, claim_gate_enabled
):
    settings = Settings(
        _env_file=None,
        claim_verification_enabled=claim_gate_enabled,
    )
    exported = []
    monkeypatch.setattr(chat_service, "app", CitationLedgerStreamingApp())
    monkeypatch.setattr(chat_service, "get_settings", lambda: settings)
    monkeypatch.setattr(chat_service, "configure_logging", lambda: None)
    monkeypatch.setattr(chat_service, "log_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        chat_service, "export_trace", lambda **kwargs: exported.append(kwargs)
    )

    events = list(chat_service.run_chat("kb", "报名要求是什么", is_local=False))

    token_events = [event for event in events if event.type == "token"]
    assert [event.payload["content"] for event in token_events] == [
        "候选答案。[a.pdf:P1]"
    ]
    assert all("[E001]" not in event.payload["content"] for event in token_events)
    if claim_gate_enabled:
        assert token_events[0].payload["verified"] is True
    else:
        assert "verified" not in token_events[0].payload

    result = next(event.payload["result"] for event in events if event.type == "final")
    assert result.answer == "候选答案。[a.pdf:P1]"
    assert result.citation_ledger[0]["chunk_id"] == "chunk:a:1"
    assert exported[0]["output_payload"]["answer"] == "候选答案。[a.pdf:P1]"
    assert exported[0]["output_payload"]["citation_ledger"] == (result.citation_ledger)
    assert "citation_finalize_node" in [step["node_name"] for step in result.steps]


class MissingCitationFinalizerApp:
    def stream(self, initial_state, config, stream_mode, subgraphs):
        class CandidateMessage:
            content = "不能发布。[E001]"

        yield (
            (),
            "updates",
            {"intent_router": {"task_type": "qa", "router_reason": "文档问答"}},
        )
        yield (("qa_subgraph",), "messages", CandidateMessage())
        yield (
            (),
            "updates",
            {
                "qa_subgraph": {
                    "answer": "不能发布。[E001]",
                    "critique": "",
                    "reranked_docs": [_doc()],
                    "citation_ledger": [{"evidence_id": "E001"}],
                }
            },
        )


def test_audited_stream_fails_closed_when_finalizer_leaves_internal_eid(monkeypatch):
    settings = Settings(_env_file=None, claim_verification_enabled=False)
    monkeypatch.setattr(chat_service, "app", MissingCitationFinalizerApp())
    monkeypatch.setattr(chat_service, "get_settings", lambda: settings)
    monkeypatch.setattr(chat_service, "configure_logging", lambda: None)
    monkeypatch.setattr(chat_service, "log_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(chat_service, "export_trace", lambda **kwargs: None)

    events = list(chat_service.run_chat("kb", "报名要求是什么", is_local=False))

    token = next(event for event in events if event.type == "token")
    assert token.payload["content"] == CLAIM_AUDIT_BLOCKED_ANSWER
    assert "[E001]" not in token.payload["content"]
    result = next(event.payload["result"] for event in events if event.type == "final")
    assert result.answer == CLAIM_AUDIT_BLOCKED_ANSWER
    assert result.citation_ledger == []
    assert result.raw_output["claim_audit"]["reason_code"] == (
        "citation_finalize_incomplete"
    )


@pytest.mark.parametrize(
    "citation",
    [
        "[e001]",
        "［Ｅ００１］",
        "[E 001]",
        "[E-002]",
        "[E-ID:002]",
        "[E001,E002]",
        "[E001.]",
        "[E-002",
        "[E001：P1]",
        "［Ｅ００１：Ｐ１］",
        "[E001:P1",
        "[[E001]]",
        "[prefix [E001]]",
    ],
)
def test_release_guard_rejects_malformed_internal_evidence_ids(citation):
    output = {"answer": f"不能发布。{citation}", "citation_ledger": []}

    blocked = chat_service._enforce_citation_finalize_release("qa", output)

    assert blocked is True
    assert output["answer"] == CLAIM_AUDIT_BLOCKED_ANSWER
    assert output["citation_ledger"] == []


def test_claim_gate_hides_candidate_token_and_emits_only_verified_answer(monkeypatch):
    settings = Settings(_env_file=None, claim_verification_enabled=True)
    monkeypatch.setattr(chat_service, "app", ClaimGateStreamingApp())
    monkeypatch.setattr(chat_service, "get_settings", lambda: settings)
    monkeypatch.setattr(chat_service, "configure_logging", lambda: None)
    monkeypatch.setattr(chat_service, "log_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(chat_service, "export_trace", lambda **kwargs: None)

    events = list(chat_service.run_chat("kb", "报名要求是什么", is_local=False))

    token_events = [event for event in events if event.type == "token"]
    assert [event.payload["content"] for event in token_events] == [
        "最终验证答案。[a.pdf:P1]"
    ]
    assert token_events[0].payload["verified"] is True
    assert all("未经验证" not in event.payload["content"] for event in token_events)
    assert [event.type for event in events] == [
        "request_started",
        "router_decided",
        "claim_audit",
        "token",
        "final",
    ]
    assert events[-1].payload["result"].answer == "最终验证答案。[a.pdf:P1]"


def test_shadow_claim_gate_records_intervention_but_releases_original_answer(
    monkeypatch,
):
    settings = Settings(_env_file=None, claim_verification_mode="shadow")
    exported = []
    monkeypatch.setattr(chat_service, "app", ShadowFailedClaimApp())
    monkeypatch.setattr(chat_service, "get_settings", lambda: settings)
    monkeypatch.setattr(chat_service, "configure_logging", lambda: None)
    monkeypatch.setattr(chat_service, "log_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        chat_service, "export_trace", lambda **kwargs: exported.append(kwargs)
    )

    events = list(chat_service.run_chat("kb", "报名要求是什么", is_local=False))

    token = next(event for event in events if event.type == "token")
    assert token.payload == {
        "content": "灰度期间仍交付的候选答案。[a.pdf:P1]",
        "verification_mode": "shadow",
        "shadow_would_intervene": True,
    }
    result = next(event.payload["result"] for event in events if event.type == "final")
    rollout = result.raw_output["claim_verification_rollout"]
    assert result.answer == "灰度期间仍交付的候选答案。[a.pdf:P1]"
    assert result.is_valid is True
    assert result.chat_messages == []
    assert rollout["mode"] == "shadow"
    assert rollout["decision"] == "would_repair"
    assert rollout["released"] is True
    assert exported[0]["output_payload"]["claim_verification_rollout"] == rollout


def test_shadow_mode_release_guard_never_replaces_failed_candidate():
    settings = Settings(_env_file=None, claim_verification_mode="shadow")
    answer = "保留候选。[a.pdf:P1]"
    output = {
        "answer": answer,
        "claim_audit": {
            "status": "failed",
            "reason_code": "unsupported_claims",
        },
    }

    blocked = chat_service._enforce_claim_gate_release("qa", output, settings)

    assert blocked is False
    assert output["answer"] == answer


def test_enforce_release_guard_preserves_frozen_rollout_policy():
    settings = Settings(_env_file=None, claim_verification_mode="enforce")
    output = {
        "answer": "不受支持的候选。[a.pdf:P1]",
        "claim_audit": {
            "status": "failed",
            "reason_code": "unsupported_claims",
        },
    }
    policy = {
        "configured_mode": "enforce",
        "effective_mode": "enforce",
        "rollout_percent": 25.0,
        "cohort_bucket": 1234,
        "cohort_selected": True,
        "fallback_mode": "shadow",
        "policy_id": "2222222222222222",
    }

    blocked = chat_service._enforce_claim_gate_release(
        "qa", output, settings, mode="enforce", policy=policy
    )

    assert blocked is True
    rollout = output["claim_verification_rollout"]
    assert rollout["configured_mode"] == "enforce"
    assert rollout["rollout_percent"] == 25.0
    assert rollout["cohort_bucket"] == 1234
    assert rollout["policy_id"] == "2222222222222222"


def test_partial_enforce_rollout_falls_back_to_shadow_without_blocking(monkeypatch):
    settings = Settings(
        _env_file=None,
        claim_verification_mode="enforce",
        claim_verification_rollout_percent=0.0,
        claim_verification_rollout_seed="test-policy",
    )
    monkeypatch.setattr(chat_service, "app", ShadowFailedClaimApp())
    monkeypatch.setattr(chat_service, "get_settings", lambda: settings)
    monkeypatch.setattr(chat_service, "configure_logging", lambda: None)
    monkeypatch.setattr(chat_service, "log_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(chat_service, "export_trace", lambda **kwargs: None)

    events = list(
        chat_service.run_chat(
            "kb", "报名要求是什么", is_local=False, session_id="sticky-session"
        )
    )

    started = next(event for event in events if event.type == "request_started")
    result = next(event.payload["result"] for event in events if event.type == "final")
    rollout = result.raw_output["claim_verification_rollout"]
    assert started.payload["claim_verification_configured_mode"] == "enforce"
    assert started.payload["claim_verification_mode"] == "shadow"
    assert result.answer == "灰度期间仍交付的候选答案。[a.pdf:P1]"
    assert rollout["configured_mode"] == "enforce"
    assert rollout["mode"] == "shadow"
    assert rollout["fallback_mode"] == "shadow"
    assert rollout["cohort_selected"] is False
    assert rollout["decision"] == "would_repair"


@pytest.mark.parametrize("claim_gate_enabled", [False, True])
def test_audited_tasks_hide_internal_critique_from_citation_progress_event(
    monkeypatch, claim_gate_enabled
):
    settings = Settings(
        _env_file=None,
        claim_verification_enabled=claim_gate_enabled,
    )
    monkeypatch.setattr(chat_service, "app", ClaimGateCitationRetryApp())
    monkeypatch.setattr(chat_service, "get_settings", lambda: settings)
    monkeypatch.setattr(chat_service, "configure_logging", lambda: None)
    monkeypatch.setattr(chat_service, "log_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(chat_service, "export_trace", lambda **kwargs: None)

    events = list(chat_service.run_chat("kb", "报名要求是什么", is_local=False))

    rejected = next(event for event in events if event.type == "citation_rejected")
    assert "round_answer" not in rejected.payload
    assert "未验证" not in str(rejected.payload)
    assert "[E999]" not in str(rejected.payload)
    assert "页码不存在" not in str(rejected.payload)
    assert rejected.payload["critique"] == "引用格式未通过，正在重新生成。"
    assert rejected.payload["will_retry"] is True


class CompareCitationRetryApp:
    def stream(self, initial_state, config, stream_mode, subgraphs):
        yield (
            (),
            "updates",
            {"intent_router": {"task_type": "compare", "router_reason": "文档对比"}},
        )
        yield (
            ("compare_subgraph",),
            "updates",
            {
                "compare_citation_node": {
                    "critique": "内部绑定 [E777] 失败",
                }
            },
        )
        answer = "对比结论。[a.pdf:P1]"
        yield (
            (),
            "updates",
            {
                "compare_subgraph": {
                    "answer": answer,
                    "critique": "",
                    "sources": [_doc()["meta"]],
                    "evidence": [
                        {"chunk_id": "chunk:a:1", "source": "a.pdf", "page": 1}
                    ],
                    "citation_ledger": _public_ledger(answer),
                }
            },
        )


def test_compare_citation_progress_hides_internal_critique(monkeypatch):
    settings = Settings(_env_file=None, claim_verification_enabled=False)
    monkeypatch.setattr(chat_service, "app", CompareCitationRetryApp())
    monkeypatch.setattr(chat_service, "get_settings", lambda: settings)
    monkeypatch.setattr(chat_service, "configure_logging", lambda: None)
    monkeypatch.setattr(chat_service, "log_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(chat_service, "export_trace", lambda **kwargs: None)

    events = list(chat_service.run_chat("kb", "对比文档", is_local=False))

    rejected = next(
        event for event in events if event.type == "compare_citation_rejected"
    )
    assert rejected.payload == {"critique": "引用格式未通过，正在重新生成。"}
    assert "[E777]" not in str(rejected.payload)


def test_delivery_preserves_answer_offsets_and_sanitizes_audited_critique():
    answer = "  结论。[a.pdf:P1]"
    output = {
        "answer": answer,
        "critique": "内部证据 [E999] 无效",
        "evidence": [{"chunk_id": "chunk:a:1", "source": "a.pdf", "page": 1}],
        "citation_ledger": _public_ledger(answer),
    }

    assert chat_service._enforce_citation_finalize_release("qa", output) is False
    result = chat_service._build_result("qa", output, "问题", "trace", [], None)

    occurrence = result.citation_ledger[0]["occurrences"][0]
    assert result.answer == answer
    assert result.answer[occurrence["answer_start"] : occurrence["answer_end"]] == (
        "[a.pdf:P1]"
    )
    assert result.critique == "回答未通过引用或声明证据校验。"
    assert "[E999]" not in result.critique


def test_release_guard_uses_global_registry_for_repaired_citation():
    answer = "补充检索后的结论。[b.pdf:P2]"
    citation = "[b.pdf:P2]"
    citation_start = answer.index(citation)
    output = {
        "answer": answer,
        # 修复前的公开 evidence 不包含实际引用条目。
        "evidence": [
            {
                "evidence_id": "E001",
                "chunk_id": "chunk:a:1",
                "source": "a.pdf",
                "page": 1,
                "span_start": 0,
                "span_end": 8,
            }
        ],
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
        ],
        "citation_ledger": [
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
    }

    blocked = chat_service._enforce_citation_finalize_release("qa", output)

    assert blocked is False
    assert output["answer"] == answer
    assert output["citation_ledger"][0]["evidence_id"] == "E002"


def test_release_guard_clears_entire_mixed_invalid_public_ledger():
    first = "[a.pdf:P1]"
    second = "[b.pdf:P2]"
    answer = f"结论{first}和{second}"
    first_start = answer.index(first)
    second_start = answer.index(second)
    output = {
        "answer": answer,
        "evidence": [
            {"chunk_id": "c1", "source": "a.pdf", "page": 1},
            {"chunk_id": "c2", "source": "b.pdf", "page": 2},
        ],
        "citation_ledger": [
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
                # 第二行有冗余前导零，整表都必须失败关闭。
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
    }

    assert chat_service._enforce_citation_finalize_release("qa", output) is True
    assert output["answer"] == CLAIM_AUDIT_BLOCKED_ANSWER
    assert output["citation_ledger"] == []
    assert output["claim_audit"]["reason_code"] == "citation_ledger_invalid"


def test_claim_gate_does_not_release_broad_not_run_reason():
    settings = Settings(_env_file=None, claim_verification_enabled=True)
    output = {
        "answer": "报名截止日期是 9 月 30 日。[a.pdf:P1]",
        "citation_ledger": [{"evidence_id": "E001"}],
        "claim_audit": {
            "status": "not_run",
            "reason_code": "no_evidence_documents",
        },
    }

    blocked = chat_service._enforce_claim_gate_release("qa", output, settings)

    assert blocked is True
    assert output["answer"] == CLAIM_AUDIT_BLOCKED_ANSWER
    assert output["citation_ledger"] == []
    assert output["claim_audit"]["status"] == "rejected"


def test_claim_gate_releases_only_matching_answer_bound_exemption():
    settings = Settings(_env_file=None, claim_verification_enabled=True)
    answer = "请在摘要问题中明确指定要总结的文件名。"
    output = {
        "answer": answer,
        "claim_audit_exemption": make_claim_audit_exemption(
            answer,
            CLAIM_AUDIT_EXEMPTION_GUIDANCE,
        ),
        "claim_audit": {
            "status": "not_run",
            "reason_code": CLAIM_AUDIT_EXEMPTION_GUIDANCE,
        },
    }

    blocked = chat_service._enforce_claim_gate_release("summary", output, settings)

    assert blocked is False
    assert output["answer"] == answer


def test_claim_gate_blocks_exemption_when_audit_reason_does_not_match():
    settings = Settings(_env_file=None, claim_verification_enabled=True)
    answer = "请在摘要问题中明确指定要总结的文件名。"
    output = {
        "answer": answer,
        "claim_audit_exemption": make_claim_audit_exemption(
            answer,
            CLAIM_AUDIT_EXEMPTION_GUIDANCE,
        ),
        "claim_audit": {
            "status": "not_run",
            "reason_code": "upstream_error",
        },
    }

    blocked = chat_service._enforce_claim_gate_release("summary", output, settings)

    assert blocked is True
    assert output["answer"] == CLAIM_AUDIT_BLOCKED_ANSWER
