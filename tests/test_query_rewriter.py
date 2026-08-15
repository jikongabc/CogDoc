from cogdoc.agents import query_rewriter
from cogdoc.agents.query_rewriter import (
    EvidenceRequirementDraft,
    QueryRewriteAgent,
    QueryRewriteOutput,
)
from cogdoc.agents.qa_generator import Generator


# 模拟本地模型结构化输出不可用，验证重写链路必须降级到原始问题。
class _RaisingLLM:
    # 模拟本地模型结构化输出不可用，验证重写链路必须降级到原始问题。
    def with_structured_output(self, schema, **kwargs):
        assert kwargs["method"] == "json_mode"
        return self

    # 调用测试替身并返回预设结果。
    def invoke(self, messages):
        raise RuntimeError("LLM 暂不可用")


# 模拟结构化输出成功，避免测试依赖真实 LLM。
class _OkLLM:
    # 模拟结构化输出成功，避免测试依赖真实 LLM。
    def __init__(self, queries, requirements=None):
        self._queries = queries
        self._requirements = requirements or []

    # 返回支持结构化输出的测试替身。
    def with_structured_output(self, schema, **kwargs):
        assert kwargs["method"] == "json_mode"
        return self

    # 调用测试替身并返回预设结果。
    def invoke(self, messages):
        return QueryRewriteOutput(
            queries=self._queries,
            evidence_requirements=self._requirements,
        )


# 定义 _CapturingLLM 数据结构。
class _CapturingLLM:
    # 初始化 _CapturingLLM 实例。
    def __init__(self):
        self.messages = None

    # 返回支持结构化输出的测试替身。
    def with_structured_output(self, schema, **kwargs):
        assert kwargs["method"] == "json_mode"
        return self

    # 调用测试替身并返回预设结果。
    def invoke(self, messages):
        self.messages = messages
        return QueryRewriteOutput(queries=["Transformer 作者"])


# 验证 empty query returns empty list 场景。
def test_empty_query_returns_empty_list():
    assert QueryRewriteAgent.rewrite_query({"query": ""}) == {
        "rewritten_queries": [],
        "evidence_requirements": [],
    }


# 验证 missing query key returns empty list 场景。
def test_missing_query_key_returns_empty_list():
    assert QueryRewriteAgent.rewrite_query({}) == {
        "rewritten_queries": [],
        "evidence_requirements": [],
    }


# 验证 llm failure falls back to original query 场景。
def test_llm_failure_falls_back_to_original_query(monkeypatch):
    monkeypatch.setattr(
        query_rewriter,
        "should_use_query_rewrite_fast_path",
        lambda *args, **kwargs: False,
    )
    monkeypatch.setattr(Generator, "_get_client", lambda **kwargs: _RaisingLLM())
    query = "大模型如何做检索增强"
    result = QueryRewriteAgent.rewrite_query({"query": query})
    assert result == {
        "rewritten_queries": [query],
        "evidence_requirements": [
            {
                "requirement_id": "r1",
                "question": query,
                "retrieval_query": query,
                "recovery_query": query,
            }
        ],
    }


# 验证 successful rewrite passes through 场景。
def test_successful_rewrite_passes_through(monkeypatch):
    monkeypatch.setattr(
        query_rewriter,
        "should_use_query_rewrite_fast_path",
        lambda *args, **kwargs: False,
    )
    monkeypatch.setattr(Generator, "_get_client", lambda **kwargs: _OkLLM(["q1", "q2"]))
    result = QueryRewriteAgent.rewrite_query({"query": "原始问题"})
    assert result == {
        "rewritten_queries": ["q1", "q2"],
        "evidence_requirements": [
            {
                "requirement_id": "r1",
                "question": "原始问题",
                "retrieval_query": "原始问题",
                "recovery_query": "原始问题",
            }
        ],
    }


# 模型只起草需求，稳定标识由服务端按有效顺序分配。
def test_requirement_planner_assigns_stable_ids(monkeypatch):
    requirements = [
        EvidenceRequirementDraft(
            question=" A 的日期是什么？ ",
            retrieval_query=" A 日期 ",
            recovery_query=" A 什么时候 ",
        ),
        EvidenceRequirementDraft(
            question="B 的费用是多少？",
            retrieval_query="B 费用",
            recovery_query="B 价格",
        ),
    ]
    monkeypatch.setattr(
        Generator,
        "_get_client",
        lambda **kwargs: _OkLLM(["A 日期", "B 费用"], requirements),
    )

    result = QueryRewriteAgent.rewrite_query({"query": "A 的日期和 B 的费用"})

    assert [
        requirement["requirement_id"] for requirement in result["evidence_requirements"]
    ] == ["r1", "r2"]
    assert result["evidence_requirements"][0] == {
        "requirement_id": "r1",
        "question": "A 的日期是什么？",
        "retrieval_query": "A 日期",
        "recovery_query": "A 什么时候",
    }


# 重复需求不会制造虚假的多需求覆盖与补检索。
def test_requirement_planner_deduplicates_normalized_questions(monkeypatch):
    requirements = [
        EvidenceRequirementDraft(
            question="Policy A",
            retrieval_query="policy primary",
            recovery_query="policy recovery",
        ),
        EvidenceRequirementDraft(
            question="ＰＯＬＩＣＹ   A",
            retrieval_query="duplicate primary",
            recovery_query="duplicate recovery",
        ),
    ]
    monkeypatch.setattr(
        Generator,
        "_get_client",
        lambda **kwargs: _OkLLM(["policy"], requirements),
    )
    monkeypatch.setattr(
        query_rewriter,
        "should_use_query_rewrite_fast_path",
        lambda *args, **kwargs: False,
    )

    result = QueryRewriteAgent.rewrite_query({"query": "Policy A"})

    assert result["evidence_requirements"] == [
        {
            "requirement_id": "r1",
            "question": "Policy A",
            "retrieval_query": "policy primary",
            "recovery_query": "policy recovery",
        }
    ]


def test_simple_self_contained_query_uses_fast_path(monkeypatch):
    monkeypatch.setattr(
        Generator,
        "_get_client_for_node",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("fast path must not call the rewrite model")
        ),
    )

    result = QueryRewriteAgent.rewrite_query({"query": "报名截止日期是什么？"})

    assert result["rewritten_queries"] == ["报名截止日期是什么？"]
    assert result["evidence_requirements"][0]["requirement_id"] == "r1"
    assert result["query_rewrite_fast_path"] is True


def test_single_intent_conjunction_can_still_use_fast_path():
    assert query_rewriter.should_use_query_rewrite_fast_path("如何安装和启动服务？")


def test_explicit_parallel_or_multi_question_query_requires_planning():
    assert not query_rewriter.should_use_query_rewrite_fast_path(
        "A 与 B 分别有哪些条件？"
    )
    assert not query_rewriter.should_use_query_rewrite_fast_path(
        "A 是什么，以及 B 如何配置？"
    )
    assert not query_rewriter.should_use_query_rewrite_fast_path("A 是什么？B 是什么？")


# 验证 rewrite prompt includes recent chat history 场景。
def test_rewrite_prompt_includes_recent_chat_history(monkeypatch):
    llm = _CapturingLLM()
    monkeypatch.setattr(Generator, "_get_client", lambda **kwargs: llm)

    result = QueryRewriteAgent.rewrite_query(
        {
            "query": "它的作者是谁？",
            "chat_history": [
                {
                    "role": "user",
                    "content": "Transformer 这篇论文讲了什么？",
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

    assert result["rewritten_queries"] == ["Transformer 作者"]
    assert result["evidence_requirements"][0]["question"] == "它的作者是谁？"
    user_prompt = llm.messages[1]["content"]
    assert "用户: Transformer 这篇论文讲了什么？" in user_prompt
    assert "助手: 它提出了自注意力架构。" in user_prompt
    assert "【当前提问】\n它的作者是谁？" in user_prompt
    assert "原子需求" in llm.messages[0]["content"]
