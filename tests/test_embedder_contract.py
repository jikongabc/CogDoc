import math
import numpy as np
import pytest
from unittest.mock import MagicMock
from cogdoc.tools.embedder import Embedder
from cogdoc.tools.retriever.vector_retriever import VectorRetriever


# 构造合法嵌入向量。
def _good(n=1):
    return [[0.0] * Embedder.EMBEDDING_DIM for _ in range(n)]


# 初始化实例状态。
class _FakeModel:
    # 初始化实例状态。
    def __init__(self, dim):
        self._dim = dim
        self.last_kwargs = {}

    # 构造或驱动 encode 测试场景。
    def encode(self, texts, **kwargs):
        self.last_kwargs = kwargs
        return np.zeros((len(texts), self._dim))


# 重置嵌入模型单例。
def _reset_model():
    Embedder._model = None


# 验证 get model pins revision。
def test_get_model_pins_revision(monkeypatch):
    captured = {}

    # 构造测试用 SentenceTransformer。
    def fake_st(name, device=None, revision=None):
        captured["name"] = name
        captured["revision"] = revision
        return _FakeModel(Embedder.EMBEDDING_DIM)

    monkeypatch.setattr("cogdoc.tools.embedder.SentenceTransformer", fake_st)
    monkeypatch.setattr("cogdoc.tools.embedder.resolve_device", lambda *a, **k: "cpu")
    _reset_model()
    try:
        Embedder.get_model()
        assert captured["revision"] == Embedder.MODEL_REVISION
    finally:
        _reset_model()


# 验证 embed documents uses normalize flag。
def test_embed_documents_uses_normalize_flag(monkeypatch):
    fake = _FakeModel(Embedder.EMBEDDING_DIM)
    monkeypatch.setattr(Embedder, "get_model", classmethod(lambda cls: fake))
    Embedder.embed_documents(["x"])
    assert fake.last_kwargs.get("normalize_embeddings") == Embedder.NORMALIZE


# 验证 embed query uses normalize flag。
def test_embed_query_uses_normalize_flag(monkeypatch):
    fake = _FakeModel(Embedder.EMBEDDING_DIM)
    monkeypatch.setattr(Embedder, "get_model", classmethod(lambda cls: fake))
    Embedder.embed_query("x")
    assert fake.last_kwargs.get("normalize_embeddings") == Embedder.NORMALIZE


def test_embed_queries_batches_query_contract(monkeypatch):
    fake = _FakeModel(Embedder.EMBEDDING_DIM)
    monkeypatch.setattr(Embedder, "get_model", classmethod(lambda cls: fake))

    vectors = Embedder.embed_queries(["x", "y"])

    assert len(vectors) == 2
    assert fake.last_kwargs.get("normalize_embeddings") == Embedder.NORMALIZE
    assert fake.last_kwargs.get("batch_size") == 64


# 验证 embed documents rejects wrong dim。
def test_embed_documents_rejects_wrong_dim(monkeypatch):
    fake = _FakeModel(Embedder.EMBEDDING_DIM + 1)  # 维度与契约不符
    monkeypatch.setattr(Embedder, "get_model", classmethod(lambda cls: fake))
    with pytest.raises(ValueError, match="contract"):
        Embedder.embed_documents(["x"])


# 验证 embed query rejects wrong dim。
def test_embed_query_rejects_wrong_dim(monkeypatch):
    fake = _FakeModel(Embedder.EMBEDDING_DIM + 1)
    monkeypatch.setattr(Embedder, "get_model", classmethod(lambda cls: fake))
    with pytest.raises(ValueError, match="contract"):
        Embedder.embed_query("x")


def test_embed_queries_rejects_wrong_dim(monkeypatch):
    fake = _FakeModel(Embedder.EMBEDDING_DIM + 1)
    monkeypatch.setattr(Embedder, "get_model", classmethod(lambda cls: fake))
    with pytest.raises(ValueError, match="contract"):
        Embedder.embed_queries(["x", "y"])


# 整批校验嵌入向量。


# 验证 validate checks every vector not only first。
def test_validate_checks_every_vector_not_only_first():
    # 第一个合法、第二个维度错：必须整批校验才能识破。
    batch = _good(1) + [[0.0] * (Embedder.EMBEDDING_DIM + 1)]
    with pytest.raises(ValueError, match="contract"):
        Embedder.validate_embeddings(batch)


# 验证 validate rejects nan。
def test_validate_rejects_nan():
    bad = [[math.nan] + [0.0] * (Embedder.EMBEDDING_DIM - 1)]
    with pytest.raises(ValueError, match="non-finite"):
        Embedder.validate_embeddings(bad)


# 验证 validate rejects inf。
def test_validate_rejects_inf():
    bad = [[math.inf] + [0.0] * (Embedder.EMBEDDING_DIM - 1)]
    with pytest.raises(ValueError, match="non-finite"):
        Embedder.validate_embeddings(bad)


# 验证 validate accepts finite correct dim。
def test_validate_accepts_finite_correct_dim():
    Embedder.validate_embeddings(_good(3))  # 不应抛错


# 写入前校验嵌入向量。


# 验证 add with embeddings rejects length mismatch。
def test_add_with_embeddings_rejects_length_mismatch():
    stub = MagicMock()
    chunk = {"meta": {"chunk_id": "c1"}, "text": "t"}
    with pytest.raises(ValueError, match="length mismatch"):
        VectorRetriever.add_with_embeddings(stub, [chunk], [])  # 1 chunk / 0 向量
    stub.collection.upsert.assert_not_called()


# 验证 add with embeddings rejects non finite。
def test_add_with_embeddings_rejects_non_finite():
    stub = MagicMock()
    chunk = {"meta": {"chunk_id": "c1"}, "text": "t"}
    bad = [[math.nan] + [0.0] * (Embedder.EMBEDDING_DIM - 1)]
    with pytest.raises(ValueError, match="non-finite"):
        VectorRetriever.add_with_embeddings(stub, [chunk], bad)
    stub.collection.upsert.assert_not_called()
