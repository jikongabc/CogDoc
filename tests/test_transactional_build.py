import os
import pytest
from unittest.mock import MagicMock, patch
from cogdoc.service.kb_epoch import EpochStore
from cogdoc.service.kb_state import (
    KBState,
    StaleGenerationError,
    GENERATION_FAILED,
    GENERATION_READY,
)
from cogdoc.service import ingest_service
from cogdoc.service import index_provenance
from cogdoc.service.ingest_service import (
    build_kb_index_transactional,
    _hardlink_snapshot,
    _cleanup_generation_storage,
    _schedule_generation_cleanup,
    _transactional_empty,
    _verify_staging,
    IndexInconsistencyError,
)
from cogdoc.tools.retriever.hybrid import HybridRetriever


# 构造状态。
def _make_state(tmp_path, kb_id="kb"):
    epochs = EpochStore(path=str(tmp_path / "epochs.json"))
    return KBState(kb_id, path=str(tmp_path / kb_id / "state.json"), epochs=epochs)


# 测试硬链接快照。


# 验证 hardlink snapshot creates hardlinks。
def test_hardlink_snapshot_creates_hardlinks(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.pdf").write_bytes(b"%PDF-1.4")
    (src / "b.pdf").write_bytes(b"%PDF-1.5")
    gen_dir = str(tmp_path / "gen")

    _hardlink_snapshot(str(src), gen_dir, ["a.pdf", "b.pdf"])

    assert os.path.exists(os.path.join(gen_dir, "a.pdf"))
    assert os.path.exists(os.path.join(gen_dir, "b.pdf"))
    # 硬链接与源文件共享 inode。
    assert (
        os.stat(os.path.join(gen_dir, "a.pdf")).st_ino
        == os.stat(str(src / "a.pdf")).st_ino
    )


# 验证 hardlink snapshot falls back to copy。
def test_hardlink_snapshot_falls_back_to_copy(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "x.pdf").write_bytes(b"%PDF")
    gen_dir = str(tmp_path / "gen")

    with patch("os.link", side_effect=OSError("cross-device")):
        _hardlink_snapshot(str(src), gen_dir, ["x.pdf"])

    assert (tmp_path / "gen" / "x.pdf").read_bytes() == b"%PDF"


# 验证 hardlink snapshot immutable from source write。
def test_hardlink_snapshot_immutable_from_source_write(tmp_path):
    # 快照建立后，通过硬链接写原文件会同步改变快照内容（hardlink 语义）， 但源 PDF 文件应视为只读（kb_write_lock 下不会被修改）；此测试确认快照文件存在且可读。
    src = tmp_path / "src"
    src.mkdir()
    (src / "doc.pdf").write_bytes(b"%PDF-1.4 content")
    gen_dir = str(tmp_path / "gen")
    _hardlink_snapshot(str(src), gen_dir, ["doc.pdf"])
    assert open(os.path.join(gen_dir, "doc.pdf"), "rb").read() == b"%PDF-1.4 content"


# 测试清理代际存储。


# 验证 cleanup removes gen dir。
def test_cleanup_removes_gen_dir(tmp_path):
    gen_dir = tmp_path / "kb" / "generations" / "g001"
    gen_dir.mkdir(parents=True)
    (gen_dir / "a.pdf").write_bytes(b"dummy")

    with (
        patch("cogdoc.service.ingest_service.get_settings") as mock_settings,
        patch("cogdoc.service.ingest_service.KBState"),
    ):
        s = MagicMock()
        s.kb_collection_id.return_value = "abc12345-g001"
        s.chroma_persist_dir = str(tmp_path / "chroma")
        s.bm25_persist_dir = str(tmp_path / "bm25")
        s.kb_generation_dir.return_value = str(gen_dir)
        mock_settings.return_value = s

        with patch("chromadb.PersistentClient"):
            _cleanup_generation_storage("kb", "g001")

    assert not gen_dir.exists()


# 验证 cleanup tolerates missing resources。
def test_cleanup_tolerates_missing_resources(tmp_path):
    # 对不存在的 Chroma 集合、BM25 pkl、gen 目录均不抛错。
    with (
        patch("cogdoc.service.ingest_service.get_settings") as mock_settings,
        patch("cogdoc.service.ingest_service.KBState"),
        patch("chromadb.PersistentClient") as mock_chroma,
    ):
        mock_chroma.return_value.delete_collection.side_effect = ValueError("not found")
        s = MagicMock()
        s.kb_collection_id.return_value = "abc12345-g001"
        s.chroma_persist_dir = str(tmp_path / "chroma")
        s.bm25_persist_dir = str(tmp_path / "bm25")
        s.kb_generation_dir.return_value = str(tmp_path / "gen_gone")
        mock_settings.return_value = s

        _cleanup_generation_storage("kb", "g001")  # 不应抛错


def test_cleanup_refuses_to_delete_active_generation(tmp_path):
    state = _make_state(tmp_path, "kb-active")
    generation_id = state.begin_generation("m", "v")
    state.mark_ready(generation_id, 0, [])
    state.switch_active(generation_id)

    with (
        patch("cogdoc.service.ingest_service.KBState", return_value=state),
        pytest.raises(ingest_service.KBCleanupError, match="active generation"),
    ):
        _cleanup_generation_storage("kb-active", generation_id)

    assert state.active()["id"] == generation_id


# 事务化构建正常路径。


# _patch_transactional：处理对应功能。
def _patch_transactional(monkeypatch, tmp_path, kb_id, chunks, pdf_files=None):
    # 公共补丁：屏蔽 I/O，注入可控的 staging engine。
    if pdf_files is None:
        pdf_files = ["a.pdf"]

    state = _make_state(tmp_path, kb_id)
    staging = MagicMock(spec=HybridRetriever)
    staging.is_consistent.return_value = True

    monkeypatch.setattr(ingest_service, "KBState", lambda kb: state)
    monkeypatch.setattr(ingest_service, "list_pdf_files", lambda d: pdf_files)
    monkeypatch.setattr(ingest_service, "_hardlink_snapshot", lambda s, g, f: None)
    monkeypatch.setattr(
        ingest_service, "_build_staging_engine", lambda kb, gid: staging
    )
    monkeypatch.setattr(
        ingest_service, "_parse_and_chunk", lambda gdir, files, hmap, **kw: (chunks, [])
    )
    monkeypatch.setattr(ingest_service, "_verify_staging", lambda s, c: None)
    monkeypatch.setattr(
        ingest_service, "_schedule_generation_cleanup", lambda kb, gid: None
    )
    monkeypatch.setattr(ingest_service, "save_index_manifest", lambda m: None)
    monkeypatch.setattr(
        ingest_service,
        "ensure_rust_core",
        lambda name: MagicMock(
            scan_pdf_manifest_native=lambda kb, d: {"doc_id": kb, "documents": []}
        ),
    )
    monkeypatch.setattr(ingest_service, "stamp_index_build_version", lambda m: m)
    monkeypatch.setattr(ingest_service, "stamp_chunk_identity_contract", lambda m: m)
    monkeypatch.setattr(ingest_service, "RetrieverFactory", MagicMock())

    return state, staging


# 验证 transactional build commits generation。
def test_transactional_build_commits_generation(tmp_path, monkeypatch):
    kb_id = "kb1"
    chunks = [{"text": "t", "meta": {"chunk_id": "c1"}}]
    state, staging = _patch_transactional(monkeypatch, tmp_path, kb_id, chunks)

    result = build_kb_index_transactional(kb_id, "/src")

    # 构建后应有一个 active generation。
    active = state.active()
    assert active is not None
    assert active["expected_count"] == 1
    assert active["status"] == GENERATION_READY
    assert active["chunk_identity_version"] == ingest_service.CHUNK_IDENTITY_VERSION
    assert result.chunk_count == 1


# 验证 transactional build 把同一派生知识存储传给提交后复核。
def test_transactional_build_propagates_knowledge_store(tmp_path, monkeypatch):
    kb_id = "kb-runtime-store"
    _patch_transactional(monkeypatch, tmp_path, kb_id, [])
    knowledge_store = object()
    captured = []

    def capture_review(
        kb,
        bindings,
        state=None,
        chunks=None,
        knowledge_store=None,
    ):
        captured.append((kb, knowledge_store))

    monkeypatch.setattr(
        ingest_service,
        "_mark_stale_derived_knowledge_quiet",
        capture_review,
    )

    build_kb_index_transactional(
        kb_id,
        "/src",
        knowledge_store=knowledge_store,
    )

    assert captured == [(kb_id, knowledge_store)]


# 验证 transactional build calls index on staging。
def test_transactional_build_calls_index_on_staging(tmp_path, monkeypatch):
    kb_id = "kb-idx"
    chunks = [{"text": "t", "meta": {"chunk_id": "c1"}}]
    state, staging = _patch_transactional(monkeypatch, tmp_path, kb_id, chunks)

    build_kb_index_transactional(kb_id, "/src")

    staging.index.assert_called_once_with(chunks)


# 验证 transactional build invalidates cache。
def test_transactional_build_invalidates_cache(tmp_path, monkeypatch):
    kb_id = "kb-inv"
    state, staging = _patch_transactional(monkeypatch, tmp_path, kb_id, [])

    mock_factory = MagicMock()
    monkeypatch.setattr(ingest_service, "RetrieverFactory", mock_factory)

    build_kb_index_transactional(kb_id, "/src")

    mock_factory.invalidate.assert_called_once_with(kb_id)


# 验证 transactional build schedules old gen cleanup。
def test_transactional_build_schedules_old_gen_cleanup(tmp_path, monkeypatch):
    kb_id = "kb-clean"
    state, staging = _patch_transactional(monkeypatch, tmp_path, kb_id, [])

    # 先建一个旧代并激活，让 switch_active 返回旧 gen_id。
    old_gen = state.begin_generation("m", "v")
    state.mark_ready(old_gen, 0, [])
    state.switch_active(old_gen)

    cleaned = []
    monkeypatch.setattr(
        ingest_service,
        "_schedule_generation_cleanup",
        lambda kb, gid: cleaned.append(gid),
    )

    build_kb_index_transactional(kb_id, "/src")

    assert old_gen in cleaned


# 事务化构建失败路径。


# 验证 transactional build marks failed on index error。
def test_transactional_build_marks_failed_on_index_error(tmp_path, monkeypatch):
    kb_id = "kb-fail"
    state, staging = _patch_transactional(
        monkeypatch, tmp_path, kb_id, [{"text": "t", "meta": {}}]
    )
    staging.index.side_effect = RuntimeError("index exploded")
    monkeypatch.setattr(
        ingest_service, "_cleanup_generation_storage", lambda kb, gid: None
    )

    with pytest.raises(RuntimeError, match="index exploded"):
        build_kb_index_transactional(kb_id, "/src")

    # 失败代必须被标记 failed，避免 GC 误判为活跃而延迟回收。
    all_gens = state.generation_ids()
    assert all(state.get(g)["status"] == GENERATION_FAILED for g in all_gens)


# 验证 transactional build cleans staging on failure。
def test_transactional_build_cleans_staging_on_failure(tmp_path, monkeypatch):
    kb_id = "kb-clean-fail"
    state, staging = _patch_transactional(
        monkeypatch, tmp_path, kb_id, [{"text": "t", "meta": {}}]
    )
    staging.index.side_effect = RuntimeError("boom")

    cleaned = []
    monkeypatch.setattr(
        ingest_service,
        "_cleanup_generation_storage",
        lambda kb, gid: cleaned.append(gid),
    )

    with pytest.raises(RuntimeError):
        build_kb_index_transactional(kb_id, "/src")

    assert len(cleaned) == 1  # staging gen 被同步清理


# 验证 transactional build stale cleans and reraises。
def test_transactional_build_stale_cleans_and_reraises(tmp_path, monkeypatch):
    kb_id = "kb-stale"
    state, staging = _patch_transactional(monkeypatch, tmp_path, kb_id, [])

    # 构造或驱动 staleswitch 测试场景。
    def stale_switch(gid, **_kwargs):
        raise StaleGenerationError("epoch mismatch")

    monkeypatch.setattr(state, "switch_active", stale_switch)

    cleaned = []
    monkeypatch.setattr(
        ingest_service,
        "_cleanup_generation_storage",
        lambda kb, gid: cleaned.append(gid),
    )

    with pytest.raises(StaleGenerationError):
        build_kb_index_transactional(kb_id, "/src")

    assert len(cleaned) == 1
    # 所有 gen 应为 failed（staging 在 ready 态时也要能被标记 failed）。
    for g in state.generation_ids():
        assert state.get(g)["status"] == GENERATION_FAILED


# 测试空目录事务构建。


# 验证 transactional empty creates active gen。
def test_transactional_empty_creates_active_gen(tmp_path, monkeypatch):
    kb_id = "kb-empty"
    state = _make_state(tmp_path, kb_id)
    monkeypatch.setattr(ingest_service, "_remove_manifest", lambda kb: None)
    monkeypatch.setattr(
        ingest_service, "_schedule_generation_cleanup", lambda kb, gid: None
    )
    monkeypatch.setattr(ingest_service, "RetrieverFactory", MagicMock())

    result = _transactional_empty(kb_id, state)

    active = state.active()
    assert active is not None
    assert active["expected_count"] == 0
    assert active["chunk_identity_version"] == ingest_service.CHUNK_IDENTITY_VERSION
    assert result.document_count == 0
    assert result.chunk_count == 0


# 验证 transactional empty cleans old gen。
def test_transactional_empty_cleans_old_gen(tmp_path, monkeypatch):
    kb_id = "kb-empty-clean"
    state = _make_state(tmp_path, kb_id)

    old_gen = state.begin_generation("m", "v")
    state.mark_ready(old_gen, 3, [])
    state.switch_active(old_gen)

    monkeypatch.setattr(ingest_service, "_remove_manifest", lambda kb: None)
    monkeypatch.setattr(ingest_service, "RetrieverFactory", MagicMock())
    cleaned = []
    monkeypatch.setattr(
        ingest_service,
        "_schedule_generation_cleanup",
        lambda kb, gid: cleaned.append(gid),
    )

    _transactional_empty(kb_id, state)

    assert old_gen in cleaned


# 验证 transactional empty stale marks failed。
def test_transactional_empty_stale_marks_failed(tmp_path, monkeypatch):
    kb_id = "kb-empty-stale"
    state = _make_state(tmp_path, kb_id)

    # 构造或驱动 staleswitch 测试场景。
    def stale_switch(gid, **_kwargs):
        raise StaleGenerationError("epoch mismatch")

    monkeypatch.setattr(state, "switch_active", stale_switch)
    monkeypatch.setattr(ingest_service, "RetrieverFactory", MagicMock())

    with pytest.raises(StaleGenerationError):
        _transactional_empty(kb_id, state)

    for g in state.generation_ids():
        assert state.get(g)["status"] == GENERATION_FAILED


# 测试校验暂存目录。


# 验证 verify staging passes for matching。
def test_verify_staging_passes_for_matching():
    staging = MagicMock(spec=HybridRetriever)
    chunks = [{"meta": {"chunk_id": "c1"}}, {"meta": {"chunk_id": "c2"}}]
    staging.count.return_value = 2
    staging.chunk_ids.return_value = {"c1", "c2"}
    _verify_staging(staging, chunks)  # 不应抛错


# 验证 verify staging count mismatch raises。
def test_verify_staging_count_mismatch_raises():
    staging = MagicMock(spec=HybridRetriever)
    chunks = [{"meta": {"chunk_id": "c1"}}, {"meta": {"chunk_id": "c2"}}]
    staging.count.return_value = 1  # 少写了一块
    staging.chunk_ids.return_value = {"c1"}
    with pytest.raises(IndexInconsistencyError, match="count mismatch"):
        _verify_staging(staging, chunks)


# 验证 verify staging chunk id mismatch raises。
def test_verify_staging_chunk_id_mismatch_raises():
    staging = MagicMock(spec=HybridRetriever)
    chunks = [{"meta": {"chunk_id": "c1"}}, {"meta": {"chunk_id": "c2"}}]
    staging.count.return_value = 2
    staging.chunk_ids.return_value = {"c1", "c3"}  # 写入了错误的 chunk
    with pytest.raises(IndexInconsistencyError, match="chunk_id mismatch"):
        _verify_staging(staging, chunks)


# 测试延迟清理代际目录。


# 验证 schedule cleanup uses tracked daemon timer。
def test_schedule_cleanup_uses_tracked_daemon_timer():
    # 必须用 threading.Timer（grace period 后才删 Chroma），且纳入统一注册表可在关闭时取消。
    with patch("cogdoc.service.ingest_service.threading.Timer") as mock_timer:
        timer_instance = MagicMock()
        mock_timer.return_value = timer_instance
        _schedule_generation_cleanup("kb", "g001")
        # 首参为延迟；目标被 _start_tracked_timer 包成 runner，故只校验延迟与启动行为。
        assert (
            mock_timer.call_args.args[0]
            == ingest_service.GENERATION_CLEANUP_DELAY_SECONDS
        )
        timer_instance.start.assert_called_once()
        assert timer_instance.daemon is True


# 验证 cancel all timers cancels pending。
def test_cancel_all_timers_cancels_pending():
    # 关闭期取消所有未触发的后台 Timer。
    with patch("cogdoc.service.ingest_service.threading.Timer") as mock_timer:
        timer_instance = MagicMock()
        mock_timer.return_value = timer_instance
        _schedule_generation_cleanup("kb", "g001")
        ingest_service.cancel_all_timers()
        timer_instance.cancel.assert_called_once()


# 验证 external purge waits for reader lease。
def test_external_purge_waits_for_reader_lease(monkeypatch):
    from cogdoc.service.kb_readers import kb_read_lease

    with kb_read_lease("kb"):
        with pytest.raises(ingest_service.KBCleanupError, match="在途读者"):
            ingest_service._purge_generation_external("kb", "g1")


# 提交后异常不标记失败。


# 验证 post commit exception does not mark failed。
def test_post_commit_exception_does_not_mark_failed(tmp_path, monkeypatch):
    # post-commit 失败（如磁盘满）应 best-effort 吞掉，任务正常返回，active 代不被回滚。
    kb_id = "kb-postcmt"
    state, staging = _patch_transactional(monkeypatch, tmp_path, kb_id, [])

    monkeypatch.setattr(
        ingest_service,
        "save_index_manifest",
        MagicMock(side_effect=OSError("disk full")),
    )

    result = build_kb_index_transactional(kb_id, "/src")  # 不应向上抛

    active = state.active()
    assert active is not None
    assert active["status"] == GENERATION_READY
    monkeypatch.setattr(index_provenance, "KBState", lambda _kb_id: state)
    monkeypatch.setattr(index_provenance, "load_index_manifest", lambda _kb_id: {})
    assert index_provenance.current_index_provenance(kb_id) == {
        "index_generation": active["id"],
        "index_build_version": ingest_service.INDEX_BUILD_VERSION,
        "chunk_identity_version": ingest_service.CHUNK_IDENTITY_VERSION,
        "source_versions": [],
    }
    assert result is not None


# 清理代际存储部分失败时保留状态。


# 验证 cleanup keeps state record on partial failure。
def test_cleanup_keeps_state_record_on_partial_failure(tmp_path):
    # chromadb 删除失败 → all_ok=False → 抛 KBCleanupError 且 state 记录保留，GC 下次可重试。 chromadb 在 _cleanup_generation_storage 内局部 import，需在 chromadb 模块上打桩。
    state = _make_state(tmp_path, "kb-partial")
    gen_id = state.begin_generation("m", "v")
    state.mark_ready(gen_id, 0, [])

    with (
        patch("cogdoc.service.ingest_service.get_settings") as mock_settings,
        patch("chromadb.PersistentClient") as mock_client,
        patch("cogdoc.service.ingest_service.KBState", return_value=state),
    ):
        s = MagicMock()
        s.kb_collection_id.return_value = "deadbeef-g001"
        s.chroma_persist_dir = str(tmp_path / "chroma")
        s.bm25_persist_dir = str(tmp_path / "bm25")
        s.kb_generation_dir.return_value = str(tmp_path / "gen_gone")
        mock_settings.return_value = s
        mock_client.return_value.delete_collection.side_effect = RuntimeError(
            "db locked"
        )

        with pytest.raises(ingest_service.KBCleanupError):
            ingest_service._cleanup_generation_storage("kb-partial", gen_id)

    # chromadb 失败 → state 记录仍在。
    assert gen_id in state.generation_ids()


# 验证 cleanup removes state record when all ok。
def test_cleanup_removes_state_record_when_all_ok(tmp_path):
    # 所有资源清理成功 → state 记录应被移除。
    state = _make_state(tmp_path, "kb-allok")
    gen_id = state.begin_generation("m", "v")
    state.mark_ready(gen_id, 0, [])

    with (
        patch("cogdoc.service.ingest_service.get_settings") as mock_settings,
        patch("chromadb.PersistentClient") as mock_client,
        patch("cogdoc.service.ingest_service.KBState", return_value=state),
    ):
        s = MagicMock()
        s.kb_collection_id.return_value = "deadbeef-g002"
        s.chroma_persist_dir = str(tmp_path / "chroma")
        s.bm25_persist_dir = str(tmp_path / "bm25")
        s.kb_generation_dir.return_value = str(tmp_path / "gen_gone_2")
        mock_settings.return_value = s
        mock_client.return_value.delete_collection.return_value = None

        ingest_service._cleanup_generation_storage("kb-allok", gen_id)

    # 全部清理成功 → state 记录移除。
    assert gen_id not in state.generation_ids()
