import pytest
from unittest.mock import MagicMock
from cogdoc.service.kb_epoch import EpochStore
from cogdoc.service.kb_state import KBState
from cogdoc.service import ingest_service
from cogdoc.service.ingest_service import (
    INDEX_BUILD_VERSION,
    IncrementalPlan,
    IndexInconsistencyError,
    _embedding_contract_changed,
    _populate_staging,
    _fill_staging_incremental,
    _review_changed_derived_knowledge,
    _chunk_text_hash,
    _stale_bindings_from_document_changes,
    _transactional_empty,
)
from cogdoc.api.derived_knowledge_store import DerivedKnowledgeStore
from cogdoc.tools.chunk_identity import build_chunk_id
from cogdoc.tools.embedder import Embedder


# 构造状态。
def _make_state(tmp_path, kb_id="kb"):
    epochs = EpochStore(path=str(tmp_path / "epochs.json"))
    return KBState(kb_id, path=str(tmp_path / kb_id / "state.json"), epochs=epochs)


# 构造注册文档。
def _reg_doc(source, sha, local_idx, chunk_index, page_start=1, page_end=1):
    # 构造自洽复用分块，分块标识由哈希、名称、页跨度和局部序号生成。
    chunk_id = build_chunk_id(sha, source, page_start, page_end, local_idx)
    return {
        "text": f"text-{chunk_id}",
        "meta": {
            "chunk_id": chunk_id,
            "source": source,
            "source_sha256": sha,
            "local_chunk_index": local_idx,
            "chunk_index": chunk_index,
            "page": page_start,
            "page_start": page_start,
            "page_end": page_end,
            "origin": "file",
        },
    }


# 构造测试用文档列表。
def _docs(*pairs):
    return [{"name": n, "sha256": h} for n, h in pairs]


# 写入活跃代。
def _seed_active(state, documents, registry):
    gen_id = state.begin_generation(Embedder.MODEL_NAME, INDEX_BUILD_VERSION)
    state.mark_ready(gen_id, expected_count=len(registry), documents=documents)
    state.switch_active(gen_id)
    return gen_id


# 替换上一代存储。
def _patch_prev_stores(monkeypatch, registry, embeddings):
    fake_vec = MagicMock()
    fake_vec.embeddings_by_chunk_id.return_value = dict(embeddings)
    fake_bm25 = MagicMock()
    fake_bm25.export_registry.return_value = [dict(d) for d in registry]
    monkeypatch.setattr(
        ingest_service, "VectorRetriever", lambda collection_id: fake_vec
    )
    monkeypatch.setattr(
        ingest_service, "BM25Retriever", lambda collection_id: fake_bm25
    )
    return fake_vec, fake_bm25


# 构造嵌入映射。
def _emb_for(registry):
    return {d["meta"]["chunk_id"]: [0.1] for d in registry}


# 构造测试用清单。
def _manifest(kb_id, documents, build_version=INDEX_BUILD_VERSION):
    return {
        "doc_id": kb_id,
        "index_build_version": build_version,
        "documents": documents,
    }


# 未修改文档复用向量。


# 验证未变文档复用且不重新嵌入。
def test_unchanged_docs_reuse_without_embedding(tmp_path, monkeypatch):
    kb_id = "kb-reuse"
    state = _make_state(tmp_path, kb_id)
    documents = _docs(("a.pdf", "H1"))
    registry = [_reg_doc("a.pdf", "H1", 0, 0)]
    _seed_active(state, documents, registry)
    _patch_prev_stores(monkeypatch, registry, _emb_for(registry))

    parsed_names = []
    monkeypatch.setattr(
        ingest_service,
        "_parse_and_chunk",
        lambda gdir, names, hmap, **kw: parsed_names.append(list(names)) or ([], []),
    )

    staging = MagicMock()
    all_chunks, _ = _populate_staging(
        kb_id, state, "/gen", ["a.pdf"], _manifest(kb_id, documents), {}, staging
    )

    assert parsed_names == [[]]
    staging.vector_retriever.add_with_embeddings.assert_called_once()
    staging.vector_retriever.add_documents.assert_not_called()
    assert {c["meta"]["chunk_id"] for c in all_chunks} == {
        registry[0]["meta"]["chunk_id"]
    }


# 仅新增和修改文档调用嵌入。


# 验证新增文档会进入嵌入流程。
def test_new_doc_goes_through_embedding(tmp_path, monkeypatch):
    kb_id = "kb-add"
    state = _make_state(tmp_path, kb_id)
    prev_docs = _docs(("a.pdf", "H1"))
    registry = [_reg_doc("a.pdf", "H1", 0, 0)]
    _seed_active(state, prev_docs, registry)
    _patch_prev_stores(monkeypatch, registry, _emb_for(registry))

    new_chunk = {
        "text": "nt",
        "meta": {"chunk_id": "c-new", "source": "b.pdf", "chunk_index": 1},
    }
    parsed_names = []
    monkeypatch.setattr(
        ingest_service,
        "_parse_and_chunk",
        lambda gdir, names, hmap, **kw: (
            parsed_names.append(list(names)) or ([new_chunk], [])
        ),
    )

    cur_docs = prev_docs + _docs(("b.pdf", "H2"))
    staging = MagicMock()
    all_chunks, _ = _populate_staging(
        kb_id,
        state,
        "/gen",
        ["a.pdf", "b.pdf"],
        _manifest(kb_id, cur_docs),
        {},
        staging,
    )

    assert parsed_names == [["b.pdf"]]
    reused = staging.vector_retriever.add_with_embeddings.call_args.args[0]
    added = staging.vector_retriever.add_documents.call_args.args[0]
    assert {c["meta"]["chunk_id"] for c in reused} == {registry[0]["meta"]["chunk_id"]}
    assert {c["meta"]["chunk_id"] for c in added} == {"c-new"}


# 纯删除不调用嵌入。


# 验证纯删除不触发嵌入。
def test_pure_delete_zero_embedding(tmp_path, monkeypatch):
    kb_id = "kb-del"
    state = _make_state(tmp_path, kb_id)
    prev_docs = _docs(("a.pdf", "H1"), ("b.pdf", "H2"))
    registry = [_reg_doc("a.pdf", "H1", 0, 0), _reg_doc("b.pdf", "H2", 0, 1)]
    _seed_active(state, prev_docs, registry)
    _patch_prev_stores(monkeypatch, registry, _emb_for(registry))

    parsed_names = []
    monkeypatch.setattr(
        ingest_service,
        "_parse_and_chunk",
        lambda gdir, names, hmap, **kw: parsed_names.append(list(names)) or ([], []),
    )

    cur_docs = _docs(("a.pdf", "H1"))
    staging = MagicMock()
    all_chunks, _ = _populate_staging(
        kb_id, state, "/gen", ["a.pdf"], _manifest(kb_id, cur_docs), {}, staging
    )

    assert parsed_names == [[]]
    staging.vector_retriever.add_documents.assert_not_called()
    assert {c["meta"]["chunk_id"] for c in all_chunks} == {
        registry[0]["meta"]["chunk_id"]
    }


# 验证文档变化会产生过期绑定。
def test_stale_bindings_from_document_changes():
    previous = _docs(("a.pdf", "H1"), ("b.pdf", "H2"), ("c.pdf", "H3"))
    current = _docs(("a.pdf", "H1"), ("b.pdf", "H2-new"), ("d.pdf", "H4"))

    bindings = _stale_bindings_from_document_changes(previous, current)

    assert bindings == [("b.pdf", "H2"), ("c.pdf", "H3")]


# 验证空库提交会标记旧文档绑定过期。
def test_transactional_empty_marks_previous_bindings_stale(tmp_path, monkeypatch):
    kb_id = "kb-empty-stale"
    state = _make_state(tmp_path, kb_id)
    _seed_active(state, _docs(("a.pdf", "H1")), [_reg_doc("a.pdf", "H1", 0, 0)])
    calls = []
    knowledge_store = object()

    monkeypatch.setattr(ingest_service.RetrieverFactory, "invalidate", lambda kb: None)
    monkeypatch.setattr(ingest_service, "_remove_manifest", lambda kb: None)
    monkeypatch.setattr(
        ingest_service, "_schedule_generation_cleanup", lambda kb, gen_id: None
    )
    monkeypatch.setattr(
        ingest_service,
        "_mark_stale_derived_knowledge_quiet",
        lambda kb, bindings, state=None, knowledge_store=None: calls.append(
            (kb, bindings, knowledge_store)
        ),
    )

    result = _transactional_empty(
        kb_id,
        state,
        knowledge_store=knowledge_store,
    )

    assert result.document_count == 0
    assert calls == [(kb_id, [("a.pdf", "H1")], knowledge_store)]


# 验证文档更新后可按文本哈希自动重绑。
def test_changed_document_rebinds_knowledge_by_chunk_hash(tmp_path):
    store = DerivedKnowledgeStore(path=str(tmp_path / "knowledge.jsonl"))
    text = "差旅报销需要七天内提交。"
    row, _ = store.create(
        {
            "kb_id": "kb",
            "text": "报销规则",
            "status": "approved",
            "related_source": "a.pdf",
            "related_source_sha256": "H1",
            "related_chunk_text_hash": _chunk_text_hash(text),
        }
    )
    chunk = _reg_doc("a.pdf", "H2", 0, 0, page_start=2, page_end=3)
    chunk["text"] = text
    reviewed = _review_changed_derived_knowledge(
        "kb",
        [("a.pdf", "H1")],
        [chunk],
        knowledge_store=store,
    )
    updated = store.list(kb_id="kb")[0]

    assert reviewed == {"stale": 0, "rebound": 1}
    assert updated["knowledge_id"] == row["knowledge_id"]
    assert updated["status"] == "approved"
    assert updated["related_source_sha256"] == "H2"
    assert updated["related_page_start"] == 2
    assert updated["related_page_end"] == 3


# 验证文档更新后可按锚点自动重绑。
def test_changed_document_rebinds_knowledge_by_anchor(tmp_path):
    store = DerivedKnowledgeStore(path=str(tmp_path / "knowledge.jsonl"))
    row, _ = store.create(
        {
            "kb_id": "kb",
            "text": "入职规则",
            "status": "approved",
            "related_source": "a.pdf",
            "related_source_sha256": "H1",
            "related_anchor_text": "直属经理确认",
        }
    )
    chunk = _reg_doc("a.pdf", "H2", 1, 1, page_start=4, page_end=4)
    chunk["text"] = "入职审批需要直属经理确认后提交。"
    reviewed = _review_changed_derived_knowledge(
        "kb",
        [("a.pdf", "H1")],
        [chunk],
        knowledge_store=store,
    )
    updated = store.list(kb_id="kb")[0]

    assert reviewed == {"stale": 0, "rebound": 1}
    assert updated["knowledge_id"] == row["knowledge_id"]
    assert updated["status"] == "approved"
    assert updated["related_source_sha256"] == "H2"
    assert updated["related_chunk_ids"] == [chunk["meta"]["chunk_id"]]


# 验证无法定位新版分块时仍标记过期。
def test_changed_document_marks_knowledge_stale_without_rebind(tmp_path):
    store = DerivedKnowledgeStore(path=str(tmp_path / "knowledge.jsonl"))
    row, _ = store.create(
        {
            "kb_id": "kb",
            "text": "旧规则",
            "status": "approved",
            "related_source": "a.pdf",
            "related_source_sha256": "H1",
            "related_anchor_text": "旧规则原文",
        }
    )
    chunk = _reg_doc("a.pdf", "H2", 0, 0)
    chunk["text"] = "新版内容没有旧锚点。"
    reviewed = _review_changed_derived_knowledge(
        "kb",
        [("a.pdf", "H1")],
        [chunk],
        knowledge_store=store,
    )
    updated = store.list(kb_id="kb")[0]

    assert reviewed == {"stale": 1, "rebound": 0}
    assert updated["knowledge_id"] == row["knowledge_id"]
    assert updated["status"] == "stale"


# 索引编号不一致时回退全量。


# 验证两路存储分叉时回退全量。
def test_diverged_stores_fallback_to_full(tmp_path, monkeypatch):
    kb_id = "kb-diverge"
    state = _make_state(tmp_path, kb_id)
    documents = _docs(("a.pdf", "H1"))
    registry = [_reg_doc("a.pdf", "H1", 0, 0)]
    _seed_active(state, documents, registry)
    _patch_prev_stores(
        monkeypatch, registry, {"cX": [0.1]}
    )  # 向量 ID 与 BM25 不同，同为 1 个

    full = [_reg_doc("a.pdf", "H1", 0, 0)]
    monkeypatch.setattr(ingest_service, "_parse_and_chunk", lambda *a, **k: (full, []))
    monkeypatch.setattr(ingest_service, "log_event", lambda *a, **k: None)

    staging = MagicMock()
    all_chunks, _ = _populate_staging(
        kb_id, state, "/gen", ["a.pdf"], _manifest(kb_id, documents), {}, staging
    )

    staging.clear.assert_called_once()
    staging.index.assert_called_once_with(full)
    assert all_chunks == full


# 验证填充时遇到分叉存储会报错。
def test_diverged_stores_raise_in_fill(tmp_path, monkeypatch):
    kb_id = "kb-diverge2"
    state = _make_state(tmp_path, kb_id)
    documents = _docs(("a.pdf", "H1"))
    registry = [_reg_doc("a.pdf", "H1", 0, 0)]
    _seed_active(state, documents, registry)
    _patch_prev_stores(monkeypatch, registry, {"cX": [0.1]})

    prev_active = state.active()
    with pytest.raises(IndexInconsistencyError, match="diverge"):
        _fill_staging_incremental(
            kb_id, MagicMock(), prev_active, IncrementalPlan([], set()), "/gen", {}
        )


# 关键词索引内容损坏时回退全量。


# 验证复用拒绝非活跃来源。
def test_reuse_rejects_source_not_in_active(tmp_path, monkeypatch):
    kb_id = "kb-badsrc"
    state = _make_state(tmp_path, kb_id)
    documents = _docs(("a.pdf", "H1"))
    # 注册表来源不在活跃文档中，表示数据已损坏。
    registry = [_reg_doc("ghost.pdf", "H1", 0, 0)]
    _seed_active(state, documents, registry)
    _patch_prev_stores(monkeypatch, registry, _emb_for(registry))

    prev_active = state.active()
    with pytest.raises(IndexInconsistencyError, match="source/hash"):
        _fill_staging_incremental(
            kb_id, MagicMock(), prev_active, IncrementalPlan([], set()), "/gen", {}
        )


# 验证复用拒绝哈希不匹配。
def test_reuse_rejects_sha_mismatch(tmp_path, monkeypatch):
    kb_id = "kb-badsha"
    state = _make_state(tmp_path, kb_id)
    documents = _docs(("a.pdf", "H1"))
    registry = [_reg_doc("a.pdf", "H1", 0, 0)]
    _seed_active(state, documents, registry)
    # 篡改注册表来源哈希，使其与活跃文档不一致。
    registry[0]["meta"]["source_sha256"] = "TAMPERED"
    _patch_prev_stores(monkeypatch, registry, _emb_for(registry))

    prev_active = state.active()
    with pytest.raises(IndexInconsistencyError, match="source/hash"):
        _fill_staging_incremental(
            kb_id, MagicMock(), prev_active, IncrementalPlan([], set()), "/gen", {}
        )


# 验证复用拒绝分块标识和元数据不匹配。
def test_reuse_rejects_chunk_id_metadata_mismatch(tmp_path, monkeypatch):
    kb_id = "kb-badid"
    state = _make_state(tmp_path, kb_id)
    documents = _docs(("a.pdf", "H1"))
    registry = [_reg_doc("a.pdf", "H1", 0, 0)]
    _seed_active(state, documents, registry)
    # 分块标识与元数据页跨度不再自洽。
    registry[0]["meta"]["page_end"] = 99
    _patch_prev_stores(monkeypatch, registry, _emb_for(registry))

    prev_active = state.active()
    with pytest.raises(IndexInconsistencyError, match="chunk_id/metadata"):
        _fill_staging_incremental(
            kb_id, MagicMock(), prev_active, IncrementalPlan([], set()), "/gen", {}
        )


# 复用写入失败时回退全量。


# 验证部分写入后会清理并全量重建。
def test_partial_write_clears_and_full_rebuild(tmp_path, monkeypatch):
    kb_id = "kb-partial"
    state = _make_state(tmp_path, kb_id)
    documents = _docs(("a.pdf", "H1"))
    registry = [_reg_doc("a.pdf", "H1", 0, 0)]
    _seed_active(state, documents, registry)
    _patch_prev_stores(monkeypatch, registry, _emb_for(registry))

    full = [_reg_doc("a.pdf", "H1", 0, 0)]
    monkeypatch.setattr(ingest_service, "_parse_and_chunk", lambda *a, **k: (full, []))
    monkeypatch.setattr(ingest_service, "log_event", lambda *a, **k: None)

    staging = MagicMock()
    staging.vector_retriever.add_with_embeddings.side_effect = RuntimeError("disk full")

    all_chunks, _ = _populate_staging(
        kb_id, state, "/gen", ["a.pdf"], _manifest(kb_id, documents), {}, staging
    )

    staging.clear.assert_called_once()
    staging.index.assert_called_once_with(full)
    assert all_chunks == full


# 模型契约变化强制全量构建。


# 验证契约变化强制全量构建。
def test_contract_change_forces_full_build(tmp_path, monkeypatch):
    kb_id = "kb-contract"
    state = _make_state(tmp_path, kb_id)
    documents = _docs(("a.pdf", "H1"))
    registry = [_reg_doc("a.pdf", "H1", 0, 0)]
    _seed_active(state, documents, registry)
    fake_vec, _ = _patch_prev_stores(monkeypatch, registry, _emb_for(registry))

    full = [_reg_doc("a.pdf", "H1", 0, 0)]
    monkeypatch.setattr(ingest_service, "_parse_and_chunk", lambda *a, **k: (full, []))

    changed = _manifest(kb_id, documents, build_version="DIFFERENT-VERSION")
    staging = MagicMock()
    _populate_staging(kb_id, state, "/gen", ["a.pdf"], changed, {}, staging)

    fake_vec.embeddings_by_chunk_id.assert_not_called()
    staging.index.assert_called_once_with(full)
    staging.vector_retriever.add_with_embeddings.assert_not_called()


def test_explicit_embedding_switch_skips_incremental_reuse(tmp_path, monkeypatch):
    kb_id = "kb-explicit-switch"
    state = _make_state(tmp_path, kb_id)
    documents = _docs(("a.pdf", "H1"))
    _seed_active(state, documents, [_reg_doc("a.pdf", "H1", 0, 0)])
    full = [_reg_doc("a.pdf", "H1", 0, 0)]
    monkeypatch.setattr(ingest_service, "_parse_and_chunk", lambda *a, **k: (full, []))
    monkeypatch.setattr(
        ingest_service,
        "_plan_transactional_incremental",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("model switch must not open the incremental path")
        ),
    )
    staging = MagicMock()

    chunks, _ = _populate_staging(
        kb_id,
        state,
        "/gen",
        ["a.pdf"],
        _manifest(kb_id, documents),
        {},
        staging,
        force_full_rebuild=True,
    )

    assert chunks == full
    staging.clear.assert_not_called()
    staging.index.assert_called_once_with(full)


def test_embedding_contract_change_is_detected_before_staging_reuse():
    class CloudContract:
        @classmethod
        def contract_version(cls):
            return "openai-compatible:model@fingerprint|dim=3|norm=True"

    local_active = {"embedding_model": Embedder.MODEL_NAME}
    cloud_active = {"embedding_model": CloudContract.contract_version()}

    assert _embedding_contract_changed(local_active, Embedder) is False
    assert _embedding_contract_changed(local_active, CloudContract) is True
    assert _embedding_contract_changed(cloud_active, CloudContract) is False
    assert _embedding_contract_changed(cloud_active, Embedder) is True


# 嵌入契约版本参与构建门控。


# 验证嵌入契约进入构建版本。
def test_embedding_contract_in_build_version():
    assert Embedder.EMBEDDING_CONTRACT_VERSION in INDEX_BUILD_VERSION
    assert Embedder.MODEL_NAME in Embedder.EMBEDDING_CONTRACT_VERSION
    assert "dim=" in Embedder.EMBEDDING_CONTRACT_VERSION
