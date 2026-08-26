from cogdoc.agents.answer_markers import NO_RELEVANT_CONTENT_ANSWER
from cogdoc.config.settings import Settings
from cogdoc.graph.subgraphs import qa
from cogdoc.graph.subgraphs.qa import (
    abstain_node,
    evidence_check,
    rerank_node,
    retrieval_retry_node,
    retrieval_check,
)
from cogdoc.tools.retriever.confidence import assess_retrieval_support


def _settings(**overrides):
    return Settings(_env_file=None, **overrides)


def _doc(*, distance=None, bm25_score=None, source_type="document", text="evidence"):
    retrieval = {}
    if distance is not None:
        retrieval["distance"] = distance
    if bm25_score is not None:
        retrieval["bm25_score"] = bm25_score
    return {
        "text": text,
        "meta": {
            "chunk_id": "chunk:test:0",
            "source_type": source_type,
            "source": "test.pdf",
            "page": 1,
        },
        "retrieval": retrieval,
    }


# 验证空候选和双低分候选会触发拒答。
def test_support_rejects_empty_and_low_confidence_candidates():
    settings = _settings()

    assert assess_retrieval_support([], settings).reason == "no_candidates"
    result = assess_retrieval_support([_doc(distance=0.95, bm25_score=5.0)], settings)

    assert result.supported is False
    assert result.reason == "below_threshold"
    assert result.signals == {"distance": 0.95, "bm25_score": 5.0}


# 验证向量或 BM25 任一达到阈值即可保留证据。
def test_support_accepts_semantic_or_lexical_signal():
    settings = _settings()

    semantic = assess_retrieval_support([_doc(distance=0.7, bm25_score=1.0)], settings)
    lexical = assess_retrieval_support([_doc(distance=1.1, bm25_score=13.0)], settings)

    assert semantic.supported is True
    assert lexical.supported is True


# 小语料中 BM25 的 IDF 可能退化；完整精确词覆盖仍应作为独立支持信号。
def test_support_accepts_exact_query_terms_when_bm25_has_no_score():
    result = assess_retrieval_support(
        [_doc(distance=0.95, text="项目负责人负责发布审核。")],
        _settings(),
        query="项目负责人是谁",
    )
    partial = assess_retrieval_support(
        [_doc(distance=0.95, text="项目已经完成发布审核。")],
        _settings(),
        query="项目负责人是谁",
    )

    assert result.supported is True
    assert result.signals["query_lexical_coverage"] == 1.0
    assert partial.supported is False


def test_partial_query_coverage_does_not_raise_borderline_confidence():
    query = "alpha beta gamma delta epsilon zeta eta theta iota kappa"
    result = assess_retrieval_support(
        [
            _doc(
                distance=1.6,
                bm25_score=1.0,
                text="alpha beta gamma delta epsilon zeta eta theta iota",
            )
        ],
        _settings(),
        query=query,
    )

    assert result.signals["query_lexical_coverage"] == 0.9
    assert result.score < 0.75
    assert result.supported is False


# 缺失所有评分信号时默认拒绝，显式兼容开关可恢复旧行为。
def test_support_fails_closed_when_confidence_signals_are_unavailable():
    result = assess_retrieval_support([_doc()], _settings())

    assert result.supported is False
    assert result.reason == "signals_unavailable"

    compatible = assess_retrieval_support(
        [_doc()], _settings(qa_abstain_allow_missing_signals=True)
    )
    assert compatible.supported is True


# 验证派生知识使用独立支持度阈值。
def test_support_uses_derived_knowledge_score():
    doc = _doc(source_type="derived_knowledge")
    doc["retrieval"]["retrieval_score"] = 0.4

    result = assess_retrieval_support([doc], _settings())

    assert result.supported is False
    assert result.signals == {"knowledge_lexical_score": 0.4}


def test_support_aggregates_candidates_and_requires_atomic_coverage():
    first = _doc(distance=0.95, bm25_score=1.0)
    first["retrieval"]["matched_requirement_ids"] = ["r1"]
    second = _doc(distance=0.7, bm25_score=1.0)
    second["meta"]["chunk_id"] = "chunk:test:1"
    second["retrieval"]["matched_requirement_ids"] = ["r2"]

    supported = assess_retrieval_support(
        [first, second], _settings(), requirement_ids=["r1", "r2"]
    )
    incomplete = assess_retrieval_support(
        [first], _settings(), requirement_ids=["r1", "r2"]
    )

    assert supported.supported is True
    assert supported.signals["distance"] == 0.7
    assert supported.signals["requirement_coverage"] == 1.0
    assert incomplete.supported is False
    assert incomplete.reason == "requirement_coverage_incomplete"


# 验证重排节点把低置信度判断写回图状态，供条件边直接拒答。
def test_rerank_node_marks_low_confidence_retrieval_for_abstention(monkeypatch):
    settings = _settings(qa_rerank_on_cpu=False)
    monkeypatch.setattr(qa, "get_settings", lambda: settings)
    monkeypatch.setattr(qa.BGEReranker, "default_device", lambda: "cpu")
    monkeypatch.setattr(
        qa, "_expand_with_neighbor_chunks", lambda _doc_id, docs, _state: docs
    )
    monkeypatch.setattr(qa, "log_event", lambda *args, **kwargs: None)

    output = rerank_node(
        {
            "query": "unrelated question",
            "doc_id": "kb",
            "retrieved_docs": [_doc(distance=0.95, bm25_score=5.0)],
        }
    )

    assert output["retrieval_abstained"] is True
    assert output["retrieval_abstain_reason"] == "below_threshold"
    assert output["retrieval_signals"] == {"distance": 0.95, "bm25_score": 5.0}
    assert output["verification_docs"]
    assert output["evidence_verification_pending"] is False

    borderline = rerank_node(
        {
            "query": "比赛时长是多少",
            "doc_id": "kb",
            "retrieved_docs": [_doc(distance=0.94, bm25_score=5.0)],
        }
    )
    assert borderline["retrieval_abstained"] is True
    assert borderline["evidence_verification_pending"] is True


# 验证拒答节点不携带候选证据并直接结束 QA。
def test_abstain_node_returns_stable_answer_without_evidence():
    output = abstain_node(
        {
            "retrieval_abstained": True,
            "retrieval_confidence": 0.5,
            "retrieval_abstain_reason": "below_threshold",
        }
    )

    assert output["answer"] == NO_RELEVANT_CONTENT_ANSWER
    assert output["evidence"] == []
    assert output["sources"] == []
    assert output["reranked_docs"] == []
    assert retrieval_check(output) == "abstain_node"
    assert retrieval_check({"retrieval_abstained": False}) == "generate_node"
    assert (
        retrieval_check(
            {
                "query": "比赛时长是多少",
                "retrieval_first_stage_supported": False,
                "retrieval_abstained": True,
                "retrieval_abstain_reason": "below_threshold",
                "retrieval_confidence": 0.9,
            }
        )
        == "evidence_verify_node"
    )
    assert evidence_check({"evidence_supported": True}) == "generate_node"
    assert evidence_check({"evidence_supported": False}) == "abstain_node"


# 验证多需求覆盖不足会触发一次补检索，达到预算后稳定拒答。
def test_requirement_coverage_retry_is_bounded(monkeypatch):
    settings = _settings(
        qa_adaptive_retrieval_enabled=True,
        qa_adaptive_retrieval_max_retries=1,
    )
    monkeypatch.setattr(qa, "get_settings", lambda: settings)
    requirements = [
        {
            "requirement_id": "r1",
            "question": "A 的规则是什么",
            "retrieval_query": "A 规则",
            "recovery_query": "A 要求",
        },
        {
            "requirement_id": "r2",
            "question": "B 的规则是什么",
            "retrieval_query": "B 规则",
            "recovery_query": "B 要求",
        },
    ]
    first_round = {
        "evidence_requirements": requirements,
        "retrieval_abstained": True,
        "retrieval_abstain_reason": "below_threshold",
        "retrieval_retry_count": 0,
    }

    assert retrieval_check(first_round) == "retrieval_retry_node"
    retry = retrieval_retry_node(first_round)
    assert retry["retrieval_retry_count"] == 1
    assert retry["missing_evidence_requirement_ids"] == ["r1", "r2"]
    assert (
        retrieval_check({**first_round, **retry, "retrieval_abstained": True})
        == "abstain_node"
    )


# 验证逐需求校验部分缺失进入补检索，校验器异常则不重复无效调用。
def test_evidence_failure_retries_only_recoverable_requirement_gaps(monkeypatch):
    monkeypatch.setattr(
        qa,
        "get_settings",
        lambda: _settings(
            qa_adaptive_retrieval_enabled=True,
            qa_adaptive_retrieval_max_retries=1,
        ),
    )
    state = {
        "evidence_supported": False,
        "evidence_requirements": [
            {
                "requirement_id": "r1",
                "question": "A",
                "retrieval_query": "A",
                "recovery_query": "A 详情",
            }
        ],
        "missing_evidence_requirement_ids": ["r1"],
        "retrieval_retry_count": 0,
    }

    assert evidence_check(state) == "retrieval_retry_node"
    assert evidence_check({**state, "evidence_verifier_error": "TimeoutError"}) == (
        "abstain_node"
    )


# 验证补检索终轮未重新校验时不会泄漏上一轮 verifier 结论。
def test_terminal_retry_rerank_clears_stale_verifier_assessments(monkeypatch):
    settings = _settings(
        qa_rerank_on_cpu=False,
        qa_evidence_verify_enabled=True,
        qa_adaptive_retrieval_enabled=True,
        qa_adaptive_retrieval_max_retries=1,
    )
    monkeypatch.setattr(qa, "get_settings", lambda: settings)
    monkeypatch.setattr(qa.BGEReranker, "default_device", lambda: "cpu")
    monkeypatch.setattr(qa, "log_event", lambda *args, **kwargs: None)

    output = rerank_node(
        {
            "query": "A 和 B 的规则分别是什么",
            "doc_id": "kb",
            "retrieval_retry_count": 1,
            "retrieval_round": 1,
            "retrieved_docs": [],
            "evidence_requirements": [
                {
                    "requirement_id": "r1",
                    "question": "A 的规则",
                    "retrieval_query": "A 规则",
                    "recovery_query": "A 要求",
                },
                {
                    "requirement_id": "r2",
                    "question": "B 的规则",
                    "retrieval_query": "B 规则",
                    "recovery_query": "B 要求",
                },
            ],
            "evidence_verification_required": True,
            "evidence_supported": False,
            "evidence_verification_reason": "上一轮缺少 B",
            "evidence_verified_chunk_ids": ["c1"],
            "evidence_requirement_assessments": [
                {
                    "requirement_id": "r1",
                    "verdict": "supported",
                    "evidence_chunk_ids": ["c1"],
                    "reason": "上一轮 A 已覆盖",
                },
                {
                    "requirement_id": "r2",
                    "verdict": "missing",
                    "evidence_chunk_ids": [],
                    "reason": "上一轮缺少 B",
                },
            ],
            "missing_evidence_requirement_ids": ["r2"],
        }
    )

    assert output["retrieval_abstained"] is True
    assert output["evidence_verification_pending"] is False
    assert output["evidence_verification_required"] is False
    assert output["evidence_verification_reason"] == ""
    assert output["evidence_verified_chunk_ids"] == []
    assert output["evidence_requirement_assessments"] == []
