import base64
import json
import pickle
import types
import pytest
from cogdoc.service import ingest_service
from cogdoc.service.ingest_service import plan_incremental
from cogdoc.tools.chunk_identity import build_chunk_id
from cogdoc.tools.retriever.bm25_retriever import BM25Retriever


# 构造测试用 manifest。
def _manifest(doc_id, docs, version="v1"):
    return {
        "doc_id": doc_id,
        "index_build_version": version,
        "documents": [{"name": n, "sha256": s} for n, s in docs],
    }


# 增量计划单元。


# 验证 plan none without previous。
def test_plan_none_without_previous():
    assert plan_incremental({}, _manifest("kb", [("a.pdf", "x")])) is None


# 验证 plan none on version change。
def test_plan_none_on_version_change():
    prev = _manifest("kb", [("a.pdf", "x")], version="v1")
    cur = _manifest("kb", [("a.pdf", "x")], version="v2")
    assert plan_incremental(prev, cur) is None


# 验证 index build version covers parser and tokenizer。
def test_index_build_version_covers_parser_and_tokenizer():
    # 解析器/分词器版本变化也必须使旧索引不可增量复用。
    from cogdoc.service.ingest_service import INDEX_BUILD_VERSION
    from cogdoc.tools.parser import PARSER_VERSION
    from cogdoc.tools.tokenizer import TOKENIZER_VERSION

    assert PARSER_VERSION in INDEX_BUILD_VERSION
    assert TOKENIZER_VERSION in INDEX_BUILD_VERSION


# 验证 plan none on doc id mismatch。
def test_plan_none_on_doc_id_mismatch():
    prev = _manifest("kb1", [("a.pdf", "x")])
    cur = _manifest("kb2", [("a.pdf", "x")])
    assert plan_incremental(prev, cur) is None


# 验证 plan detects added changed removed。
def test_plan_detects_added_changed_removed():
    prev = _manifest("kb", [("keep.pdf", "k"), ("change.pdf", "c1"), ("gone.pdf", "g")])
    cur = _manifest("kb", [("keep.pdf", "k"), ("change.pdf", "c2"), ("new.pdf", "n")])
    plan = plan_incremental(prev, cur)
    # 只重解析新增+改变，未变文档跳过。
    assert plan.to_parse == ["change.pdf", "new.pdf"]
    # 按文件名删除 = 被删 + 改变的文档。
    assert plan.removed_sources == {"gone.pdf", "change.pdf"}


# 验证 plan all unchanged is noop plan。
def test_plan_all_unchanged_is_noop_plan():
    prev = _manifest("kb", [("a.pdf", "x")])
    cur = _manifest("kb", [("a.pdf", "x")])
    plan = plan_incremental(prev, cur)
    assert plan.to_parse == []
    assert plan.removed_sources == set()


# 验证 plan same content files deleted independently。
def test_plan_same_content_files_deleted_independently():
    # a.pdf 与 b.pdf 内容相同（同 sha）但文件名不同；删 a.pdf 只删 a.pdf，b.pdf 保留。
    prev = _manifest("kb", [("a.pdf", "dup"), ("b.pdf", "dup")])
    cur = _manifest("kb", [("b.pdf", "dup")])
    plan = plan_incremental(prev, cur)
    assert plan.to_parse == []
    assert plan.removed_sources == {"a.pdf"}


# 增量入库编排。


# 初始化实例状态。
class _FakeEngine:
    # 初始化实例状态。
    def __init__(self):
        self.indexed = None
        self.cleared = False
        self.upserts = []
        self._count = 0
        self._consistent = True  # 入口判定与写后校验共用
        self.consistent_after_mutate = True
        self.raise_on_mutate = False

    # 清理。
    def clear(self):
        self.cleared = True

    # 写入索引。
    def index(self, chunks):
        if self.raise_on_mutate:
            raise RuntimeError("boom")
        self.indexed = chunks
        self._consistent = self.consistent_after_mutate

    # 增量写入documents。
    def upsert_documents(self, new_chunks, removed_sources):
        if self.raise_on_mutate:
            raise RuntimeError("boom")
        self.upserts.append((new_chunks, removed_sources))
        self._consistent = self.consistent_after_mutate

    # 统计数量。
    def count(self):
        return self._count

    # 判断 consistent 是否成立。
    def is_consistent(self):
        return self._consistent

    # 构造或驱动 max分块索引 测试场景。
    def max_chunk_index(self):
        return 41


# 替换结果。
def _patch(monkeypatch, engine, manifest, parsed_names):
    monkeypatch.setattr(
        ingest_service.RetrieverFactory, "get_engine", lambda kb: engine
    )
    saved, invalidated = [], []
    monkeypatch.setattr(
        ingest_service, "_invalidate_engine_cache", lambda kb: invalidated.append(kb)
    )
    monkeypatch.setattr(
        ingest_service,
        "ensure_rust_core",
        lambda *s: types.SimpleNamespace(
            scan_pdf_manifest_native=lambda kb, d: manifest
        ),
    )
    monkeypatch.setattr(ingest_service, "stamp_chunk_identity_contract", lambda m: m)
    monkeypatch.setattr(ingest_service, "stamp_index_build_version", lambda m: m)
    monkeypatch.setattr(
        ingest_service, "save_index_manifest", lambda m: saved.append(m)
    )
    # 记录实际被解析的文档名，验证未变文档不解析。
    monkeypatch.setattr(
        ingest_service,
        "smart_parse",
        lambda path: parsed_names.append(path.rsplit("/", 1)[-1]) or [object()],
    )
    monkeypatch.setattr(
        ingest_service,
        "chunk_paper",
        lambda pages, source_sha256: [{"meta": {}, "text": source_sha256}],
    )
    return saved, invalidated


# 验证 incremental only parses changed docs。
def test_incremental_only_parses_changed_docs(tmp_path, monkeypatch):
    src = tmp_path / "sources"
    src.mkdir()
    for name in ("keep.pdf", "change.pdf", "new.pdf"):
        (src / name).write_bytes(b"%PDF-1.4")

    cur = _manifest("kb", [("keep.pdf", "k"), ("change.pdf", "c2"), ("new.pdf", "n")])
    prev = _manifest("kb", [("keep.pdf", "k"), ("change.pdf", "c1"), ("gone.pdf", "g")])
    engine = _FakeEngine()
    engine._count = 5
    parsed = []
    _patch(monkeypatch, engine, cur, parsed)
    monkeypatch.setattr(ingest_service, "load_index_manifest", lambda kb: prev)

    result = ingest_service.build_kb_index("kb", str(src))

    # keep.pdf 未变，不应被解析。
    assert sorted(parsed) == ["change.pdf", "new.pdf"]
    assert engine.indexed is None  # 没走全量
    new_chunks, removed = engine.upserts[0]
    assert removed == {"gone.pdf", "change.pdf"}
    assert {c["text"] for c in new_chunks} == {"c2", "n"}
    # 新块从现存最大编号(41)+1 续号，唯一且不与未变文档冲突。
    assert sorted(c["meta"]["chunk_index"] for c in new_chunks) == [42, 43]
    # document_count=库内总数，chunk_count=引擎现存总数。
    assert result.document_count == 3
    assert result.chunk_count == 5


# 验证 incremental falls back when index empty。
def test_incremental_falls_back_when_index_empty(tmp_path, monkeypatch):
    src = tmp_path / "sources"
    src.mkdir()
    (src / "a.pdf").write_bytes(b"%PDF-1.4")

    # manifest 判「未变」，但引擎不一致（向量/BM25 丢失）：必须全量自愈，而非空操作。
    cur = _manifest("kb", [("a.pdf", "x")])
    engine = _FakeEngine()
    engine._consistent = False
    _patch(monkeypatch, engine, cur, [])
    monkeypatch.setattr(ingest_service, "load_index_manifest", lambda kb: cur)

    ingest_service.build_kb_index("kb", str(src))

    assert engine.indexed is not None
    assert engine.upserts == []


# 验证 incremental failure invalidates cache。
def test_incremental_failure_invalidates_cache(tmp_path, monkeypatch):
    src = tmp_path / "sources"
    src.mkdir()
    (src / "a.pdf").write_bytes(b"%PDF-1.4")

    cur = _manifest("kb", [("a.pdf", "x2")])
    prev = _manifest("kb", [("a.pdf", "x1")])  # 改变 -> 走增量
    engine = _FakeEngine()
    engine.raise_on_mutate = True  # upsert 中途异常（如嵌入失败）
    saved, invalidated = _patch(monkeypatch, engine, cur, [])
    monkeypatch.setattr(ingest_service, "load_index_manifest", lambda kb: prev)

    with pytest.raises(RuntimeError):
        ingest_service.build_kb_index("kb", str(src))

    # 失败必须失效缓存（驱逐半更新引擎），且不保存新 manifest（下次入库自愈）。
    assert invalidated == ["kb"]
    assert saved == []


# 验证 full rebuild inconsistent write fails。
def test_full_rebuild_inconsistent_write_fails(tmp_path, monkeypatch):
    src = tmp_path / "sources"
    src.mkdir()
    (src / "a.pdf").write_bytes(b"%PDF-1.4")

    cur = _manifest("kb", [("a.pdf", "x")])
    engine = _FakeEngine()
    engine.consistent_after_mutate = (
        False  # 写后两路不一致（如向量 clear 静默失败残留旧块）
    )
    saved, invalidated = _patch(monkeypatch, engine, cur, [])
    monkeypatch.setattr(ingest_service, "load_index_manifest", lambda kb: {})

    with pytest.raises(ingest_service.IndexInconsistencyError):
        ingest_service.build_kb_index("kb", str(src))

    # 校验失败不能误报成功：失效缓存、不保存 manifest。
    assert invalidated == ["kb"]
    assert saved == []


# 验证 empty kb removes stale manifest。
def test_empty_kb_removes_stale_manifest(tmp_path, monkeypatch):
    src = tmp_path / "sources"
    src.mkdir()  # 没有 PDF
    engine = _FakeEngine()
    monkeypatch.setattr(
        ingest_service.RetrieverFactory, "get_engine", lambda kb: engine
    )
    monkeypatch.setattr(ingest_service, "_invalidate_engine_cache", lambda kb: None)
    manifest_file = tmp_path / "kb.json"
    manifest_file.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(ingest_service, "manifest_path", lambda kb: str(manifest_file))

    result = ingest_service.build_kb_index("kb", str(src))

    assert result.document_count == 0
    assert engine.cleared is True
    # 删光文件后必须连 manifest 一起清，否则加回相同文件会被误判未变。
    assert not manifest_file.exists()


# 验证 falls back to full rebuild on version change。
def test_falls_back_to_full_rebuild_on_version_change(tmp_path, monkeypatch):
    src = tmp_path / "sources"
    src.mkdir()
    (src / "a.pdf").write_bytes(b"%PDF-1.4")

    cur = _manifest("kb", [("a.pdf", "x")], version="v2")
    prev = _manifest("kb", [("a.pdf", "x")], version="v1")
    engine = _FakeEngine()
    _patch(monkeypatch, engine, cur, [])
    monkeypatch.setattr(ingest_service, "load_index_manifest", lambda kb: prev)

    ingest_service.build_kb_index("kb", str(src))

    # 版本变化必须全量重建（engine.index 被调），不走增量。
    assert engine.indexed is not None
    assert engine.upserts == []


# 校验关键词索引增量等价。


# _chunk：处理对应功能。
def _chunk(name, sha, local_idx, text):
    cid = build_chunk_id(sha, name, 1, 1, local_idx)
    return {
        "text": text,
        "meta": {
            "chunk_id": cid,
            "source_sha256": sha,
            "local_chunk_index": local_idx,
            "chunk_index": local_idx,
            "source": name,
            "page": 1,
            "page_start": 1,
            "page_end": 1,
            "origin": "file",
        },
    }


# 切分 ids。
def _chunk_ids(retriever):
    return {d["meta"]["chunk_id"] for d in retriever.doc_registry}


# 验证 bm25 incremental equals full rebuild。
def test_bm25_incremental_equals_full_rebuild(tmp_path):
    persist = str(tmp_path / "bm25")
    # 初始库 a.pdf + b.pdf。
    initial = [
        _chunk("a.pdf", "aaa", 0, "alpha one"),
        _chunk("a.pdf", "aaa", 1, "alpha two"),
        _chunk("b.pdf", "bbb", 0, "beta one"),
    ]
    inc = BM25Retriever("kb", persist_directory=persist)
    inc.index(initial)

    # 删 a.pdf、改 b.pdf(内容 bbb->bbb2)、加 c.pdf。
    new_chunks = [
        _chunk("b.pdf", "bbb2", 0, "beta changed"),
        _chunk("c.pdf", "ccc", 0, "gamma one"),
        _chunk("c.pdf", "ccc", 1, "gamma two"),
    ]
    inc.upsert_documents(new_chunks, removed_sources={"a.pdf", "b.pdf"})

    # 全量重建最终文档集 b.pdf(bbb2) + c.pdf。
    full = BM25Retriever("kb-full", persist_directory=str(tmp_path / "bm25_full"))
    full.index(new_chunks)

    assert _chunk_ids(inc) == _chunk_ids(full)
    assert inc.count() == full.count() == 3
    # a.pdf 的 chunk 已不在索引。
    assert all("a.pdf" not in cid for cid in _chunk_ids(inc))


# 验证 bm25 same content files deleted independently。
def test_bm25_same_content_files_deleted_independently(tmp_path):
    # a.pdf 与 b.pdf 内容相同（同 sha），文件名不同 -> chunk_id 不同 -> 可独立删除。
    inc = BM25Retriever("kb", persist_directory=str(tmp_path / "bm25"))
    inc.index(
        [_chunk("a.pdf", "dup", 0, "same text"), _chunk("b.pdf", "dup", 0, "same text")]
    )
    assert inc.count() == 2

    inc.upsert_documents([], removed_sources={"a.pdf"})

    sources = {d["meta"]["source"] for d in inc.doc_registry}
    assert sources == {"b.pdf"}
    assert inc.count() == 1


# 验证 bm25 upsert to empty clears index。
def test_bm25_upsert_to_empty_clears_index(tmp_path):
    persist = str(tmp_path / "bm25")
    inc = BM25Retriever("kb", persist_directory=persist)
    inc.index([_chunk("a.pdf", "aaa", 0, "alpha")])
    inc.upsert_documents([], removed_sources={"a.pdf"})
    assert inc.count() == 0
    assert inc.exists() is False


def test_bm25_migrates_legacy_data_only_pickle_to_json(tmp_path):
    persist = str(tmp_path / "bm25")
    original = BM25Retriever("kb", persist_directory=persist)
    original.index([_chunk("a.pdf", "aaa", 0, "alpha")])
    with open(original.db_path, encoding="utf-8") as handle:
        safe_payload = json.load(handle)
    legacy_payload = {
        "format": "bm25_index_bytes_v1",
        "doc_registry": safe_payload["doc_registry"],
        "index_bytes": base64.b64decode(safe_payload["index_base64"]),
    }
    with open(original.db_path, "wb") as handle:
        pickle.dump(legacy_payload, handle)

    restored = BM25Retriever("kb", persist_directory=persist)

    assert restored.count() == 1
    with open(restored.db_path, encoding="utf-8") as handle:
        migrated = json.load(handle)
    assert migrated["format"] == "bm25_index_json_v2"


def test_bm25_restricted_legacy_loader_rejects_pickle_globals(tmp_path):
    persist = tmp_path / "bm25"
    persist.mkdir()
    path = persist / "bm25_kb.pkl"
    path.write_bytes(pickle.dumps(__import__("os").system))

    restored = BM25Retriever("kb", persist_directory=str(persist))

    assert restored.exists() is False


# 校验向量索引增量等价。


# 验证 vector incremental equals full rebuild。
def test_vector_incremental_equals_full_rebuild(tmp_path, monkeypatch):
    from cogdoc.tools.embedder import Embedder
    from cogdoc.tools.retriever.vector_retriever import VectorRetriever

    monkeypatch.setattr(
        Embedder, "embed_documents", lambda texts: [[0.1, 0.2, 0.3] for _ in texts]
    )
    initial = [
        _chunk("a.pdf", "aaa", 0, "alpha one"),
        _chunk("a.pdf", "aaa", 1, "alpha two"),
        _chunk("b.pdf", "bbb", 0, "beta one"),
    ]
    new_chunks = [
        _chunk("b.pdf", "bbb2", 0, "beta changed"),
        _chunk("c.pdf", "ccc", 0, "gamma one"),
        _chunk("c.pdf", "ccc", 1, "gamma two"),
    ]

    inc = VectorRetriever("kb", persist_directory=str(tmp_path / "chroma"))
    inc.index(initial)
    # 模拟 HybridRetriever.upsert_documents 的顺序：先按文件名删，再加新 chunk。
    inc.delete_by_source({"a.pdf", "b.pdf"})
    inc.add_documents(new_chunks)

    full = VectorRetriever("kbfull", persist_directory=str(tmp_path / "chroma_full"))
    full.index(new_chunks)

    assert set(inc.collection.get()["ids"]) == set(full.collection.get()["ids"])
    assert inc.count() == full.count() == 3


# 混合检索委托一致性。


# 验证 hybrid upsert delegates delete then add。
def test_hybrid_upsert_delegates_delete_then_add():
    from cogdoc.tools.retriever.hybrid import HybridRetriever

    calls = []

    # 定义 _V 数据结构。
    class _V:
        # 删除 by source。
        def delete_by_source(self, s):
            calls.append(("vec_delete", set(s)))

        # 添加 documents。
        def add_documents(self, c):
            calls.append(("vec_add", [x["t"] for x in c]))

    # 模拟关键词检索器的增量写入。
    class _B:
        # 增量写入documents。
        def upsert_documents(self, c, s):
            calls.append(("bm25_upsert", [x["t"] for x in c], set(s)))

    engine = HybridRetriever(_V(), _B())
    engine.upsert_documents([{"t": "n1"}], removed_sources={"old"})

    assert calls == [
        ("vec_delete", {"old"}),
        ("vec_add", ["n1"]),
        ("bm25_upsert", ["n1"], {"old"}),
    ]


# 验证 retrieval rejects corrupt index。
def test_retrieval_rejects_corrupt_index():
    from cogdoc.tools.retriever.hybrid import HybridRetriever, IndexCorruptError

    # 初始化实例状态。
    class _V:
        # 初始化实例状态。
        def __init__(self, n):
            self.n = n

        # 统计数量。
        def count(self):
            return self.n

        # 检索。
        def search(self, *a, **k):
            return []

    # 初始化实例状态。
    class _B:
        # 初始化实例状态。
        def __init__(self, n):
            self.n = n

        # 统计数量。
        def count(self):
            return self.n

        # 检索。
        def search(self, *a, **k):
            return []

        # 列出 sources。
        def list_sources(self):
            return []

        # 加载 source chunks。
        def load_source_chunks(self, s):
            return []

    # 两路数量不一致：检索/列源/取 chunk 都必须拒绝，而非返回半更新结果。
    corrupt = HybridRetriever(_V(3), _B(2))
    for call in (
        lambda: corrupt.search("q"),
        corrupt.list_sources,
        lambda: corrupt.load_source_chunks("a.pdf"),
    ):
        with pytest.raises(IndexCorruptError):
            call()

    # 两路皆空属正常空库，不算损坏（放行返回空，不报错）。
    assert HybridRetriever(_V(0), _B(0)).is_corrupt() is False
    assert HybridRetriever(_V(3), _B(3)).is_corrupt() is False


# 验证 bm25 clear failure raises。
def test_bm25_clear_failure_raises(tmp_path, monkeypatch):
    import cogdoc.tools.retriever.bm25_retriever as m

    r = BM25Retriever("kb", persist_directory=str(tmp_path / "bm25"))
    r.index([_chunk("a.pdf", "aaa", 0, "alpha text")])
    # 删除失败（如权限）-> _init 重载旧数据 -> clear 必须抛错，不静默成功。
    monkeypatch.setattr(
        m.os, "remove", lambda p: (_ for _ in ()).throw(OSError("denied"))
    )
    with pytest.raises(RuntimeError):
        r.clear()


# 验证 vector clear failure raises。
def test_vector_clear_failure_raises(tmp_path, monkeypatch):
    from cogdoc.tools.embedder import Embedder
    from cogdoc.tools.retriever.vector_retriever import VectorRetriever

    monkeypatch.setattr(
        Embedder, "embed_documents", lambda texts: [[0.1, 0.2, 0.3] for _ in texts]
    )
    v = VectorRetriever("kb", persist_directory=str(tmp_path / "chroma"))
    v.index([_chunk("a.pdf", "aaa", 0, "alpha text")])
    # 删除集合变 no-op（模拟静默失败）-> 旧块残留 -> clear 必须抛错。
    monkeypatch.setattr(v.client, "delete_collection", lambda name: None)
    with pytest.raises(RuntimeError):
        v.clear()


# 验证 hybrid is consistent compares chunk id sets。
def test_hybrid_is_consistent_compares_chunk_id_sets():
    from cogdoc.tools.retriever.hybrid import HybridRetriever

    # 初始化实例状态。
    class _C:
        # 初始化实例状态。
        def __init__(self, ids):
            self._ids = set(ids)

        # 切分 ids。
        def chunk_ids(self):
            return self._ids

    assert HybridRetriever(_C({"x", "y"}), _C({"x", "y"})).is_consistent() is True
    assert HybridRetriever(_C(set()), _C({"x"})).is_consistent() is False  # 向量丢失
    assert HybridRetriever(_C({"x"}), _C(set())).is_consistent() is False  # BM25 为空
    # 数量相等但 chunk_id 不同（部分缺失+混入等量错块/损坏）也必须判为不一致。
    assert HybridRetriever(_C({"x", "z"}), _C({"x", "y"})).is_consistent() is False
