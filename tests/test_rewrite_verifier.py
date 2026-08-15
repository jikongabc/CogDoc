from cogdoc.agents import rewrite_verifier
from cogdoc.agents.rewrite_verifier import (
    RewriteVerifyAgent,
    filter_rewrites_by_similarity,
)


# 验证 keeps above threshold drops below 场景。
def test_keeps_above_threshold_drops_below():
    # 相似度高于阈值的改写保留，低于阈值的改写丢弃。
    kept, dropped = filter_rewrites_by_similarity(
        [1.0, 0.0],
        [[1.0, 0.0], [0.0, 1.0]],
        ["good", "bad"],
        0.5,
    )

    assert kept == ["good"]
    assert dropped == [("bad", 0.0)]


# 验证 similarity equal to threshold is kept 场景。
def test_similarity_equal_to_threshold_is_kept():
    # 相似度等于阈值时按通过处理。
    kept, dropped = filter_rewrites_by_similarity(
        [1.0, 0.0],
        [[0.5, 0.0]],
        ["borderline"],
        0.5,
    )

    assert kept == ["borderline"]
    assert dropped == []


# 验证 all below returns empty 场景。
def test_all_below_returns_empty():
    # 全部低于阈值时返回空列表，由检索节点回退原问题。
    kept, dropped = filter_rewrites_by_similarity(
        [1.0, 0.0],
        [[0.0, 1.0]],
        ["bad"],
        0.5,
    )

    assert kept == []
    assert dropped == [("bad", 0.0)]


# 验证 length mismatch is zero 场景。
def test_length_mismatch_is_zero():
    # 向量维度不一致时按不相关处理。
    kept, dropped = filter_rewrites_by_similarity(
        [1.0, 0.0],
        [[1.0]],
        ["x"],
        0.5,
    )

    assert kept == []
    assert dropped == [("x", 0.0)]


# 验证 missing rewrite vector is dropped not silently lost 场景。
def test_missing_rewrite_vector_is_dropped_not_silently_lost():
    # 向量数量少于改写数量时，缺失向量的改写必须进入 dropped。
    kept, dropped = filter_rewrites_by_similarity(
        [1.0, 0.0],
        [[1.0, 0.0]],
        ["good", "missing"],
        0.5,
    )

    assert kept == ["good"]
    assert dropped == [("missing", 0.0)]


# 验证 verify rewrites overwrites with filtered queries 场景。
def test_verify_rewrites_overwrites_with_filtered_queries(monkeypatch):
    # verify 节点直接覆盖 rewritten_queries，不依赖 reducer 累加。
    monkeypatch.setattr(
        rewrite_verifier.Embedder,
        "embed_documents",
        lambda texts: [[1.0, 0.0], [1.0, 0.0], [0.0, 1.0]],
    )

    result = RewriteVerifyAgent.verify_rewrites(
        {
            "query": "original",
            "rewritten_queries": ["good", "bad"],
            "rewrite_similarity_threshold": 0.5,
        }
    )

    assert result["rewritten_queries"] == ["good"]
    assert result["steps_trace"][0]["step_name"] == "verify_rewrite"
    assert "bad" in result["steps_trace"][0]["output_summary"]


# 验证 baseline augmented with chat history 场景。
def test_baseline_augmented_with_chat_history(monkeypatch):
    # 覆盖历史补全改写不再被裸省略句基准误杀。
    captured = {}

    # 捕获结果。
    def capture(texts):
        captured["texts"] = texts
        return [[1.0, 0.0]] * len(texts)

    monkeypatch.setattr(rewrite_verifier.Embedder, "embed_documents", capture)

    result = RewriteVerifyAgent.verify_rewrites(
        {
            "query": "它的作者是谁？",
            "rewritten_queries": ["Transformer 作者"],
            "chat_history": [
                {
                    "role": "user",
                    "content": "讲讲 Transformer 这篇论文",
                    "timestamp": None,
                },
                {
                    "role": "assistant",
                    "content": "它提出了自注意力架构。",
                    "timestamp": None,
                },
            ],
        }
    )

    baseline_text = captured["texts"][0]
    assert "Transformer" in baseline_text
    assert "它的作者是谁？" in baseline_text
    assert result["rewritten_queries"] == ["Transformer 作者"]


# 验证 baseline is bare query without history 场景。
def test_baseline_is_bare_query_without_history(monkeypatch):
    # 单轮（无历史）基准仍是原问题，行为与改造前一致。
    captured = {}

    # 捕获结果。
    def capture(texts):
        captured["texts"] = texts
        return [[1.0, 0.0]] * len(texts)

    monkeypatch.setattr(rewrite_verifier.Embedder, "embed_documents", capture)

    RewriteVerifyAgent.verify_rewrites(
        {"query": "原始问题", "rewritten_queries": ["改写"]}
    )

    assert captured["texts"][0] == "原始问题"


# 验证 verify rewrites skips empty inputs 场景。
def test_verify_rewrites_skips_empty_inputs(monkeypatch):
    # 空原问题或空改写无需加载 embedding 模型。
    def fail_if_called(_texts):
        raise AssertionError("embed_documents should not be called")

    monkeypatch.setattr(rewrite_verifier.Embedder, "embed_documents", fail_if_called)

    assert RewriteVerifyAgent.verify_rewrites(
        {"query": "", "rewritten_queries": ["x"]}
    ) == {"rewritten_queries": ["x"]}
    assert RewriteVerifyAgent.verify_rewrites(
        {"query": "q", "rewritten_queries": []}
    ) == {"rewritten_queries": []}


def test_fast_path_skips_redundant_embedding_verification(monkeypatch):
    monkeypatch.setattr(
        rewrite_verifier.Embedder,
        "embed_documents",
        lambda _texts: (_ for _ in ()).throw(
            AssertionError("fast path must not embed duplicate query text")
        ),
    )

    result = RewriteVerifyAgent.verify_rewrites(
        {
            "query": "报名截止日期是什么？",
            "rewritten_queries": ["报名截止日期是什么？"],
            "query_rewrite_fast_path": True,
            "evidence_requirements": [
                {
                    "requirement_id": "r1",
                    "question": "报名截止日期是什么？",
                    "retrieval_query": "报名截止日期是什么？",
                    "recovery_query": "报名截止日期是什么？",
                }
            ],
        }
    )

    assert result["query_rewrite_fast_path"] is True
    assert result["evidence_requirements"][0]["requirement_id"] == "r1"


# 验证 verify rewrites handles empty embedding result 场景。
def test_verify_rewrites_handles_empty_embedding_result(monkeypatch):
    # embedding 异常少返回空向量时，所有改写按低相似丢弃并写入 trace。
    monkeypatch.setattr(rewrite_verifier.Embedder, "embed_documents", lambda texts: [])

    result = RewriteVerifyAgent.verify_rewrites(
        {
            "query": "original",
            "rewritten_queries": ["missing"],
            "rewrite_similarity_threshold": 0.5,
        }
    )

    assert result["rewritten_queries"] == []
    assert "missing" in result["steps_trace"][0]["output_summary"]


# 需求先对原问题守卫，再将漂移的主/恢复查询回退到需求问题。
def test_requirement_guard_drops_drift_and_falls_back_queries(monkeypatch):
    captured = {"calls": 0}

    def embed(texts):
        captured["calls"] += 1
        assert len(texts) == 8
        return [
            [1.0, 0.0],  # original baseline
            [1.0, 0.0],  # legacy rewrite
            [1.0, 0.0],  # r1 question
            [0.0, 1.0],  # r1 primary drift
            [1.0, 0.0],  # r1 recovery
            [0.0, 1.0],  # r2 question drift
            [0.0, 1.0],
            [0.0, 1.0],
        ]

    monkeypatch.setattr(rewrite_verifier.Embedder, "embed_documents", embed)
    result = RewriteVerifyAgent.verify_rewrites(
        {
            "query": "A 的日期和 B 的费用",
            "rewritten_queries": ["A 日期 B 费用"],
            "evidence_requirements": [
                {
                    "requirement_id": "r1",
                    "question": "A 的日期是什么？",
                    "retrieval_query": "无关新实体",
                    "recovery_query": "A 什么时候",
                },
                {
                    "requirement_id": "r2",
                    "question": "完全无关的问题",
                    "retrieval_query": "无关",
                    "recovery_query": "无关",
                },
            ],
        }
    )

    assert captured["calls"] == 1
    assert result["evidence_requirements"] == [
        {
            "requirement_id": "r1",
            "question": "A 的日期是什么？",
            "retrieval_query": "A 的日期是什么？",
            "recovery_query": "A 什么时候",
        }
    ]
    trace = result["steps_trace"][0]["output_summary"]
    assert '"requirement_id": "r2"' in trace
    assert '"field": "retrieval_query"' in trace


# 规划需求全部漂移时，仍保留一个原问题需求保证检索可继续。
def test_requirement_guard_uses_original_fallback_when_all_drift(monkeypatch):
    monkeypatch.setattr(
        rewrite_verifier.Embedder,
        "embed_documents",
        lambda texts: [[1.0, 0.0]] + [[0.0, 1.0]] * (len(texts) - 1),
    )
    result = RewriteVerifyAgent.verify_rewrites(
        {
            "query": "原问题",
            "rewritten_queries": [],
            "evidence_requirements": [
                {
                    "requirement_id": "r1",
                    "question": "无关新实体",
                    "retrieval_query": "无关",
                    "recovery_query": "无关",
                }
            ],
        }
    )

    assert result["evidence_requirements"] == [
        {
            "requirement_id": "r1",
            "question": "原问题",
            "retrieval_query": "原问题",
            "recovery_query": "原问题",
        }
    ]
    assert (
        '"original_fallback_used": true' in result["steps_trace"][0]["output_summary"]
    )
