import pytest
from collections import OrderedDict
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from cogdoc.config.settings import get_settings
from cogdoc.service.kb_epoch import EpochStore
from cogdoc.service.kb_state import KBState
from cogdoc.service.retriever_factory import RetrieverFactory
from cogdoc.tools.retriever.base_retriever import NullRetriever, NullWriteError
from cogdoc.tools.retriever.hybrid import HybridRetriever, IndexCorruptError
from cogdoc.tools.retriever.evidence_pack import (
    EvidencePackCandidate,
    build_evidence_pack,
)
from cogdoc.tools.evidence_rendering import (
    EVIDENCE_BLOCK_SEPARATOR,
    evidence_block_char_count,
)
from cogdoc.tools.retriever.vector_retriever import EmbeddingModelMismatchError
from cogdoc.agents.qa_generator import Generator
from cogdoc.graph.subgraphs import qa


class _NoRetrievalFeedback:
    def boosts_for_query(self, kb_id, query):
        return {}


def _runtime(knowledge_retriever, retrieval_feedback_store=None):
    return SimpleNamespace(
        derived_knowledge_retriever=knowledge_retriever,
        retrieval_feedback_store=(retrieval_feedback_store or _NoRetrievalFeedback()),
    )


def _runtime_config(runtime):
    return {"configurable": {"state_runtime": runtime}}


# 构造状态。
def _make_state(tmp_path, kb_id="kb"):
    epochs = EpochStore(path=str(tmp_path / "epochs.json"))
    return KBState(kb_id, path=str(tmp_path / kb_id / "state.json"), epochs=epochs)


# 构造或驱动 新建实例工厂 测试场景。
def _fresh_factory():
    # 每个测试独立清空缓存，自动夹具结束后也会清理。
    RetrieverFactory._engines = OrderedDict()


# 空检索器契约。


# 验证空检索器读路径安全。
def test_null_retriever_read_safe():
    nr = NullRetriever()
    assert nr.exists() is False
    assert nr.count() == 0
    assert nr.chunk_ids() == set()
    assert nr.max_chunk_index() == -1
    assert nr.search("q") == []
    assert nr.list_sources() == []
    assert nr.load_source_chunks("x") == []
    nr.clear()
    nr.delete_by_source(["x"])


# 验证空检索器写路径报错。
def test_null_retriever_write_raises():
    # 写方法必须显式报错，不能静默忽略，以便及早暴露误用。
    nr = NullRetriever()
    with pytest.raises(NullWriteError):
        nr.index([])
    with pytest.raises(NullWriteError):
        nr.add_documents([])
    with pytest.raises(NullWriteError):
        nr.upsert_documents([], set())


# 验证空混合引擎不视为损坏。
def test_null_hybrid_engine_not_corrupt():
    # 两路空检索器计数相等，检索返回空列表。
    engine = HybridRetriever(NullRetriever(), NullRetriever())
    assert engine.is_corrupt() is False
    assert engine.count() == 0
    assert engine.search("q") == []


# 验证生成提示区分派生知识引用格式。
def test_generator_prompt_includes_knowledge_citation_format():
    docs = [
        {
            "text": "差旅报销需要七天内提交。",
            "meta": {
                "chunk_id": "knowledge:K123",
                "knowledge_id": "K123",
                "source_type": "derived_knowledge",
                "source": "knowledge:K123",
                "page": 0,
                "certainty": "high",
                "related_source": "policy.pdf",
            },
        }
    ]

    messages = Generator.format_prompt("报销规则是什么", docs)
    rendered = "\n".join(str(message.content) for message in messages)

    assert '<Knowledge knowledge_id="K123"' in rendered
    assert 'evidence_id="E001"' in rendered
    assert "[E001]" in rendered


# 验证结构化子块把章节路径提供给生成器，但引用身份仍使用原 child chunk。
def test_generator_prompt_includes_section_path_without_replacing_child_identity():
    docs = [
        {
            "text": "训练分为预训练和微调两个阶段。",
            "meta": {
                "chunk_id": "child:7",
                "parent_chunk_id": "section:2",
                "section_title": "2.1 Training",
                "section_path": "Methods > Training",
                "source": "paper.pdf",
                "page": 4,
            },
        }
    ]

    rendered = Generator._build_context_string(docs)

    assert 'chunk_id="child:7" evidence_id="E001"' in rendered
    assert "章节路径：Methods &gt; Training" in rendered
    assert 'chunk_id="child:7"' in rendered
    assert "section:2" not in rendered


def test_generator_consumes_materialized_overlap_deduplicated_pack():
    overlap = "0123456789abcdef"
    docs = [
        {
            "text": f"left-{overlap}",
            "meta": {
                "chunk_id": "child:0",
                "parent_chunk_id": "section:1",
                "child_index_in_parent": 0,
                "source": "paper.pdf",
                "page": 1,
            },
        },
        {
            "text": f"{overlap}-right",
            "meta": {
                "chunk_id": "child:1",
                "parent_chunk_id": "section:1",
                "child_index_in_parent": 1,
                "source": "paper.pdf",
                "page": 1,
            },
        },
    ]
    pack = build_evidence_pack(
        [EvidencePackCandidate(doc) for doc in docs],
        max_docs=2,
        max_chars=1000,
        document_char_cost=evidence_block_char_count,
        separator_chars=len(EVIDENCE_BLOCK_SEPARATOR),
    )

    rendered = Generator._build_context_string(list(pack.kept_docs))

    assert pack.estimated_chars == len(rendered)
    assert rendered.count(overlap) == 1
    assert pack.kept_docs[1]["text"] == "-right"
    assert pack.kept_docs[1]["retrieval"]["evidence_text_start"] == len(overlap)
    assert docs[1]["text"] == f"{overlap}-right"


# 验证问答召回融合派生知识。
def test_retrieve_node_merges_approved_knowledge(monkeypatch):
    raw_doc = {
        "text": "文档报名要求。",
        "meta": {
            "chunk_id": "chunk:a:1",
            "source": "a.pdf",
            "page": 1,
            "page_start": 1,
            "page_end": 1,
        },
    }
    knowledge_doc = {
        "text": "补充报名要求。",
        "meta": {
            "chunk_id": "knowledge:K123",
            "knowledge_id": "K123",
            "source_type": "derived_knowledge",
            "source": "knowledge:K123",
            "page": 0,
            "page_start": 0,
            "page_end": 0,
        },
    }

    class Engine:
        def search(self, query, top_k):
            return [raw_doc]

    class Knowledge:
        def search(self, kb_id, query, top_k):
            return [knowledge_doc]

    monkeypatch.setattr(qa.RetrieverFactory, "get_engine", lambda doc_id: Engine())
    runtime = _runtime(Knowledge())

    result = qa.retrieve_node(
        {"query": "报名要求", "doc_id": "kb"},
        _runtime_config(runtime),
    )

    assert [doc["meta"]["chunk_id"] for doc in result["retrieved_docs"]] == [
        "chunk:a:1",
        "knowledge:K123",
    ]


# 验证检索反馈会调整召回排序。
def test_retrieve_node_applies_retrieval_feedback_boost(monkeypatch):
    docs = [
        {"text": "a", "meta": {"chunk_id": "c1"}},
        {"text": "b", "meta": {"chunk_id": "c2"}},
    ]

    class Engine:
        def search(self, query, top_k):
            return docs

    class Knowledge:
        def search(self, kb_id, query, top_k):
            return []

    class Feedback:
        def boosts_for_query(self, kb_id, query):
            return {"c2": 0.5, "c1": -0.2}

    monkeypatch.setattr(qa.RetrieverFactory, "get_engine", lambda doc_id: Engine())
    runtime = _runtime(Knowledge(), Feedback())

    result = qa.retrieve_node(
        {"query": "报名要求", "doc_id": "kb"},
        _runtime_config(runtime),
    )

    assert [doc["meta"]["chunk_id"] for doc in result["retrieved_docs"]] == [
        "c2",
        "c1",
    ]
    assert result["retrieved_docs"][0]["retrieval"]["feedback_boost"] == 0.5


# 验证检索反馈读取失败时保持原排序。
def test_retrieve_node_ignores_retrieval_feedback_errors(monkeypatch):
    docs = [
        {"text": "a", "meta": {"chunk_id": "c1"}},
        {"text": "b", "meta": {"chunk_id": "c2"}},
    ]

    class BrokenFeedback:
        def boosts_for_query(self, kb_id, query):
            raise ValueError("broken")

    result = qa._apply_retrieval_feedback("kb", "问题", docs, BrokenFeedback())

    assert result == docs


# 验证同一编译图按请求隔离状态运行时。
def test_retrieve_node_isolates_injected_state_runtimes(monkeypatch):
    class Engine:
        def search(self, query, top_k):
            return []

    class Knowledge:
        def __init__(self, knowledge_id):
            self.knowledge_id = knowledge_id

        def search(self, kb_id, query, top_k):
            return [
                {
                    "text": self.knowledge_id,
                    "meta": {
                        "chunk_id": f"knowledge:{self.knowledge_id}",
                        "knowledge_id": self.knowledge_id,
                        "source_type": "derived_knowledge",
                        "source": f"knowledge:{self.knowledge_id}",
                    },
                }
            ]

    monkeypatch.setattr(qa.RetrieverFactory, "get_engine", lambda doc_id: Engine())
    state = {"query": "同一问题", "doc_id": "kb"}

    result_a = qa.retrieve_node(
        state,
        _runtime_config(_runtime(Knowledge("A"))),
    )
    result_b = qa.retrieve_node(
        state,
        _runtime_config(_runtime(Knowledge("B"))),
    )

    assert [doc["meta"]["knowledge_id"] for doc in result_a["retrieved_docs"]] == ["A"]
    assert [doc["meta"]["knowledge_id"] for doc in result_b["retrieved_docs"]] == ["B"]


# 集合编号哈希命名。


# 验证集合标识稳定。
def test_collection_id_deterministic():
    s = get_settings()
    assert s.kb_collection_id("kb1", "g123") == s.kb_collection_id("kb1", "g123")
    assert s.kb_collection_id("kb1", "g123") != s.kb_collection_id("kb2", "g123")


# 验证集合标识不超过存储限制。
def test_collection_id_within_chroma_limit():
    # 集合名前缀加短哈希和索引代标识，远低于存储限制。
    kb_id = "a" * 56  # 最长允许的 kb_id
    gen_id = "g" + "f" * 12
    collection_id = get_settings().kb_collection_id(kb_id, gen_id)
    assert len(f"col-{collection_id}") <= 60


# 解析生成编号。


# 验证无活跃代时解析为空。
def test_resolve_gen_id_no_active(tmp_path):
    state = _make_state(tmp_path)
    with patch("cogdoc.service.retriever_factory.KBState", return_value=state):
        assert RetrieverFactory._resolve_gen_id("kb") is None


# 验证空索引代解析为空。
def test_resolve_gen_id_expected_count_zero(tmp_path):
    state = _make_state(tmp_path)
    gen_id = state.begin_generation("m", "v")
    state.mark_ready(gen_id, expected_count=0, documents=[])
    state.switch_active(gen_id)
    with patch("cogdoc.service.retriever_factory.KBState", return_value=state):
        assert RetrieverFactory._resolve_gen_id("kb") is None


# 验证解析返回活跃代标识。
def test_resolve_gen_id_returns_active_id(tmp_path):
    state = _make_state(tmp_path)
    gen_id = state.begin_generation("m", "v")
    state.mark_ready(gen_id, expected_count=3, documents=[])
    state.switch_active(gen_id)
    with patch("cogdoc.service.retriever_factory.KBState", return_value=state):
        assert RetrieverFactory._resolve_gen_id("kb") == gen_id


# 构建引擎。


# 验证无索引代时构建空引擎。
def test_build_engine_null_when_gen_id_none():
    engine = RetrieverFactory._build_engine("kb", None)
    assert engine.count() == 0
    assert engine.is_corrupt() is False


# 验证构建引擎使用配置中的集合标识。
def test_build_engine_uses_settings_collection_id(tmp_path):
    # 验证传入检索器的集合标识来自配置。
    kb_id = "kb1"
    state = _make_state(tmp_path, kb_id)
    gen_id = state.begin_generation("m", "v")
    state.mark_ready(gen_id, expected_count=2, documents=[])
    state.switch_active(gen_id)

    expected_cid = get_settings().kb_collection_id(kb_id, gen_id)

    with (
        patch("cogdoc.service.retriever_factory.KBState", return_value=state),
        patch("cogdoc.service.retriever_factory.VectorRetriever") as MockVec,
        patch("cogdoc.service.retriever_factory.BM25Retriever") as MockBm25,
    ):
        mock_engine = MagicMock(spec=HybridRetriever)
        mock_engine.count.return_value = 2
        mock_engine.is_consistent.return_value = True
        with patch(
            "cogdoc.service.retriever_factory.HybridRetriever",
            return_value=mock_engine,
        ):
            RetrieverFactory._build_engine(kb_id, gen_id)

    MockVec.assert_called_once_with(collection_id=expected_cid)
    MockBm25.assert_called_once_with(collection_id=expected_cid)


# 验证构建引擎遇到模型不匹配时返回空引擎。
def test_build_engine_model_mismatch_returns_null(tmp_path):
    state = _make_state(tmp_path)
    gen_id = state.begin_generation("old", "v")
    state.mark_ready(gen_id, expected_count=3, documents=[])
    state.switch_active(gen_id)

    with (
        patch("cogdoc.service.retriever_factory.KBState", return_value=state),
        patch(
            "cogdoc.service.retriever_factory.VectorRetriever",
            side_effect=EmbeddingModelMismatchError("mismatch"),
        ),
    ):
        engine = RetrieverFactory._build_engine("kb", gen_id)

    assert engine.count() == 0
    assert engine.is_corrupt() is False


# 验证构建引擎遇到计数不一致时报错。
def test_build_engine_count_mismatch_raises(tmp_path):
    # 磁盘数据丢失导致计数不一致时必须报错。
    state = _make_state(tmp_path)
    gen_id = state.begin_generation("m", "v")
    state.mark_ready(gen_id, expected_count=5, documents=[])
    state.switch_active(gen_id)

    with (
        patch("cogdoc.service.retriever_factory.KBState", return_value=state),
        patch("cogdoc.service.retriever_factory.VectorRetriever"),
        patch("cogdoc.service.retriever_factory.BM25Retriever"),
    ):
        mock_engine = MagicMock(spec=HybridRetriever)
        mock_engine.count.return_value = 0  # 磁盘数据丢失，count 为 0
        mock_engine.is_consistent.return_value = True
        with patch(
            "cogdoc.service.retriever_factory.HybridRetriever",
            return_value=mock_engine,
        ):
            with pytest.raises(IndexCorruptError):
                RetrieverFactory._build_engine("kb", gen_id)


# 验证构建期间索引代被回收时返回空引擎。
def test_build_engine_gen_gcod_returns_null():
    # 该代在构造期间已被回收，返回空引擎且不写回缓存。
    with (
        patch("cogdoc.service.retriever_factory.KBState") as MockKBState,
        patch("cogdoc.service.retriever_factory.VectorRetriever"),
        patch("cogdoc.service.retriever_factory.BM25Retriever"),
    ):
        mock_state = MagicMock()
        mock_state.get.return_value = None  # 已被 GC 回收
        MockKBState.return_value = mock_state
        engine = RetrieverFactory._build_engine("kb", "g_gone_0000000")

    # 索引代已回收时返回真实空引擎，不判损坏。
    assert engine.count() == 0
    assert engine.is_corrupt() is False


# 验证构建引擎遇到不一致时报错。
def test_build_engine_inconsistent_raises(tmp_path):
    # 计数正确但两路分块集合不等时必须报错。
    state = _make_state(tmp_path)
    gen_id = state.begin_generation("m", "v")
    state.mark_ready(gen_id, expected_count=3, documents=[])
    state.switch_active(gen_id)

    with (
        patch("cogdoc.service.retriever_factory.KBState", return_value=state),
        patch("cogdoc.service.retriever_factory.VectorRetriever"),
        patch("cogdoc.service.retriever_factory.BM25Retriever"),
    ):
        mock_engine = MagicMock(spec=HybridRetriever)
        mock_engine.count.return_value = 3  # count 正确
        mock_engine.is_consistent.return_value = False  # 但 chunk_id 集合不等
        with patch(
            "cogdoc.service.retriever_factory.HybridRetriever",
            return_value=mock_engine,
        ):
            with pytest.raises(IndexCorruptError):
                RetrieverFactory._build_engine("kb", gen_id)


# 缓存键和失效竞态保护。


# 验证缓存命中返回同一实例。
def test_cache_hit_returns_same_instance(tmp_path):
    _fresh_factory()
    kb_id = "kb-hit"
    gen_id = "g123456789abc"
    sentinel = MagicMock(spec=HybridRetriever)
    with RetrieverFactory._lock:
        RetrieverFactory._engines[(kb_id, gen_id)] = sentinel

    # 解析到同一索引代时命中缓存。
    with patch.object(RetrieverFactory, "_resolve_gen_id", return_value=gen_id):
        engine = RetrieverFactory.get_engine(kb_id)
    assert engine is sentinel


# 验证失效会清理知识库全部缓存。
def test_invalidate_clears_all_entries_for_kb():
    _fresh_factory()
    kb_id = "kb-inv"
    sentinel_a = MagicMock(spec=HybridRetriever)
    sentinel_b = MagicMock(spec=HybridRetriever)
    # 同一知识库的两个索引代条目都要被清掉。
    with RetrieverFactory._lock:
        RetrieverFactory._engines[(kb_id, "g001")] = sentinel_a
        RetrieverFactory._engines[(kb_id, "g002")] = sentinel_b
        RetrieverFactory._engines[("other-kb", "g003")] = MagicMock()

    RetrieverFactory.invalidate(kb_id)
    with RetrieverFactory._lock:
        keys = list(RetrieverFactory._engines.keys())
    assert (kb_id, "g001") not in keys
    assert (kb_id, "g002") not in keys
    assert ("other-kb", "g003") in keys  # 其他 kb 不受影响
    sentinel_a.close.assert_called_once_with()
    sentinel_b.close.assert_called_once_with()


# 验证竞态下旧引擎不写入缓存。
def test_race_stale_engine_not_cached(tmp_path):
    # 模拟构造期间索引代已切换，旧引擎不写回缓存。
    _fresh_factory()
    kb_id = "kb-race"
    old_gen = "g_old_0000000"
    new_gen = "g_new_1111111"

    call_count = [0]

    # 解析副作用效果。
    def resolve_side_effect(kb):
        call_count[0] += 1
        # 第一次（锁外解析）返回旧代；第二次（锁内插入前重解析）返回新代。
        return old_gen if call_count[0] == 1 else new_gen

    old_engine = MagicMock(spec=HybridRetriever)
    new_engine = MagicMock(spec=HybridRetriever)
    with (
        patch.object(
            RetrieverFactory, "_resolve_gen_id", side_effect=resolve_side_effect
        ),
        patch.object(
            RetrieverFactory,
            "_build_engine",
            side_effect=lambda _kb, generation: {
                old_gen: old_engine,
                new_gen: new_engine,
            }[generation],
        ),
    ):
        result = RetrieverFactory.get_engine(kb_id)

    # 构造期间切代后必须重建并返回新代，旧引擎不得逃逸。
    assert result is new_engine
    old_engine.close.assert_called_once_with()
    with RetrieverFactory._lock:
        assert (kb_id, old_gen) not in RetrieverFactory._engines
        assert RetrieverFactory._engines[(kb_id, new_gen)] is new_engine
