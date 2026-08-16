from cogdoc.memory.manager import MemoryPolicy
from cogdoc.memory.retriever import MemoryRetriever


# 构造可预测的嵌入向量。
def _embed(texts: list[str]):
    vectors = {
        "语义问题": [1.0, 0.0],
        "语义相关事实": [1.0, 0.0],
        "高优先级无关事实": [0.0, 1.0],
    }
    return [vectors.get(text, [0.5, 0.5]) for text in texts]


# 验证语义通道可召回低优先级相关事实。
def test_semantic_channel_beats_unrelated_priority_candidate():
    policy = MemoryPolicy(
        context_long_term_limit=1,
        memory_retrieval_mid_limit=0,
    )
    retriever = MemoryRetriever(policy, embedding_fn=_embed)
    facts = [
        {
            "id": "high",
            "content": "高优先级无关事实",
            "importance": 1.0,
            "updated_at": 2.0,
        },
        {
            "id": "relevant",
            "content": "语义相关事实",
            "importance": 0.8,
            "updated_at": 1.0,
        },
    ]

    context = retriever.retrieve("语义问题", [], {}, facts)

    assert "语义相关事实" in context[0]["content"]
    assert "高优先级无关事实" not in context[0]["content"]


def test_memory_retrieval_result_reports_routes_and_selected_tiers():
    policy = MemoryPolicy(
        context_long_term_limit=1,
        memory_retrieval_mid_limit=1,
    )
    retriever = MemoryRetriever(policy, embedding_fn=_embed)

    result = retriever.retrieve_result(
        "语义问题",
        [{"role": "user", "content": "最近消息"}],
        {"goals": ["完成检索"], "decisions": [], "summary": []},
        [{"id": "fact", "content": "语义相关事实", "importance": 1.0}],
    )

    assert set(result.channel_counts) == {
        "memory_recency",
        "memory_lexical",
        "memory_semantic",
        "memory_long_importance",
        "memory_mid_priority",
    }
    assert result.selected_tier_counts == {"short": 1, "mid": 1, "long": 1}
    assert result.context == retriever.retrieve(
        "语义问题",
        [{"role": "user", "content": "最近消息"}],
        {"goals": ["完成检索"], "decisions": [], "summary": []},
        [{"id": "fact", "content": "语义相关事实", "importance": 1.0}],
    )


# 验证最终上下文保持 RRF 的长期事实顺序。
def test_long_term_rrf_order_survives_context_assembly():
    policy = MemoryPolicy(
        context_long_term_limit=2,
        memory_retrieval_mid_limit=0,
    )
    retriever = MemoryRetriever(policy, embedding_fn=_embed)
    facts = [
        {
            "id": "high",
            "content": "高优先级无关事实",
            "importance": 1.0,
            "updated_at": 2.0,
        },
        {
            "id": "relevant",
            "content": "语义相关事实",
            "importance": 0.8,
            "updated_at": 1.0,
        },
    ]

    context = retriever.retrieve("语义问题", [], {}, facts)
    content = context[0]["content"]

    assert content.index("语义相关事实") < content.index("高优先级无关事实")


# 验证关键词通道可覆盖静态优先级排序。
def test_lexical_channel_recalls_exact_long_term_fact():
    policy = MemoryPolicy(
        context_long_term_limit=1,
        memory_semantic_enabled=False,
        memory_retrieval_mid_limit=0,
    )
    retriever = MemoryRetriever(policy)
    facts = [
        {"id": "high", "content": "默认使用中文", "importance": 1.0},
        {"id": "exact", "content": "数据库采用 PostgreSQL", "importance": 0.8},
    ]

    context = retriever.retrieve("PostgreSQL 数据库", [], {}, facts)

    assert "PostgreSQL" in context[0]["content"]
    assert "默认使用中文" not in context[0]["content"]


# 验证语义通道失败时降级到其他召回通道。
def test_semantic_failure_falls_back_to_priority_channel():
    policy = MemoryPolicy(context_long_term_limit=1)

    # 模拟嵌入服务失败。
    def failing_embed(_texts):
        raise RuntimeError("embedding unavailable")

    retriever = MemoryRetriever(policy, embedding_fn=failing_embed)
    facts = [
        {"id": "high", "content": "稳定事实", "importance": 1.0},
        {"id": "low", "content": "普通事实", "importance": 0.8},
    ]

    context = retriever.retrieve("无匹配查询", [], {}, facts)

    assert "稳定事实" in context[0]["content"]


# 验证短期通道保留最近消息的原始顺序。
def test_short_term_channel_keeps_recent_message_order():
    policy = MemoryPolicy(
        memory_semantic_enabled=False,
        memory_retrieval_short_limit=4,
        memory_retrieval_mid_limit=0,
        context_long_term_limit=0,
    )
    retriever = MemoryRetriever(policy)
    messages = [{"role": "user", "content": f"问题{index}"} for index in range(8)]

    context = retriever.retrieve("新的问题", messages, {}, [])

    assert [message["content"] for message in context] == [
        "问题4",
        "问题5",
        "问题6",
        "问题7",
    ]


# 验证短期槽位在召回前确定且不再二次截断。
def test_short_term_relevance_survives_reduced_context_budget():
    policy = MemoryPolicy(
        short_term_message_limit=4,
        memory_semantic_enabled=False,
        memory_retrieval_short_limit=8,
        memory_retrieval_mid_limit=1,
        memory_retrieval_recent_pin=1,
        context_long_term_limit=1,
    )
    retriever = MemoryRetriever(policy)
    messages = [
        {"role": "user", "content": "关键旧消息"},
        *[{"role": "user", "content": f"普通消息{index}"} for index in range(5)],
    ]
    mid_term = {"goals": ["普通目标"]}
    facts = [{"id": "fact", "content": "普通事实", "importance": 1.0}]

    context = retriever.retrieve("关键旧消息", messages, mid_term, facts)

    assert len(context) == 4
    assert context[-2]["content"] == "关键旧消息"
    assert context[-1]["content"] == "普通消息4"


# 验证短期语义召回可按配置开启。
def test_short_term_semantic_retrieval_is_configurable():
    vectors = {
        "语义问题": [1.0, 0.0],
        "较早相关消息": [1.0, 0.0],
        "最新无关消息": [0.0, 1.0],
    }

    # 返回短期召回测试向量。
    def embed(texts):
        return [vectors[text] for text in texts]

    policy = MemoryPolicy(
        memory_retrieval_short_limit=1,
        memory_retrieval_recent_pin=0,
        memory_retrieval_mid_limit=0,
        context_long_term_limit=0,
        memory_semantic_include_short=True,
        memory_recency_weight=0.0,
        memory_lexical_weight=0.0,
    )
    retriever = MemoryRetriever(policy, embedding_fn=embed)
    messages = [
        {"role": "user", "content": "较早相关消息"},
        {"role": "user", "content": "最新无关消息"},
    ]

    context = retriever.retrieve("语义问题", messages, {}, [])

    assert context[0]["content"] == "较早相关消息"


# 验证通道权重可以改变融合结果。
def test_channel_weights_are_configurable():
    policy = MemoryPolicy(
        context_long_term_limit=1,
        memory_retrieval_mid_limit=0,
        memory_semantic_weight=0.0,
        memory_lexical_weight=0.0,
        memory_importance_weight=2.0,
    )
    retriever = MemoryRetriever(policy, embedding_fn=_embed)
    facts = [
        {"id": "high", "content": "高优先级无关事实", "importance": 1.0},
        {"id": "relevant", "content": "语义相关事实", "importance": 0.8},
    ]

    context = retriever.retrieve("语义问题", [], {}, facts)

    assert "高优先级无关事实" in context[0]["content"]


# 验证中期目标和决策参与关键词召回。
def test_mid_term_lexical_retrieval_selects_relevant_decision():
    policy = MemoryPolicy(
        memory_semantic_enabled=False,
        memory_retrieval_short_limit=0,
        memory_retrieval_mid_limit=1,
        context_long_term_limit=0,
    )
    retriever = MemoryRetriever(policy)
    mid_term = {
        "goals": ["完成前端页面"],
        "decisions": ["数据库采用 Qdrant"],
        "summary": ["用户讨论了部署方式"],
    }

    context = retriever.retrieve("Qdrant 数据库", [], mid_term, [])

    assert "数据库采用 Qdrant" in context[0]["content"]
    assert "完成前端页面" not in context[0]["content"]


# 验证相同记忆文本只嵌入一次。
def test_semantic_vectors_are_cached_between_queries():
    calls = []

    # 记录每次嵌入的文本。
    def recording_embed(texts):
        calls.append(list(texts))
        return [[1.0, 0.0] for _ in texts]

    policy = MemoryPolicy(context_long_term_limit=1)
    retriever = MemoryRetriever(policy, embedding_fn=recording_embed)
    facts = [{"id": "fact", "content": "缓存事实", "importance": 1.0}]

    retriever.retrieve("问题一", [], {}, facts)
    retriever.retrieve("问题二", [], {}, facts)

    embedded = [text for batch in calls for text in batch]
    assert embedded.count("缓存事实") == 1
    assert len(retriever._vector_cache) == 1
