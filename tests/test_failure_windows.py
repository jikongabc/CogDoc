import os
from unittest.mock import MagicMock, patch
import pytest
from cogdoc.api.ingest import IndexJobManager
from cogdoc.api.persistence import InMemoryJobStore
from cogdoc.service.mutation_journal import MutationJournal, MutationJournalError
from cogdoc.service.kb_lifecycle import (
    LifecycleStore,
    LIFECYCLE_DELETING,
    LIFECYCLE_ACTIVE,
)
from cogdoc.service.purge_queue import PurgeQueue, PurgeQueueCorruptError


# 模拟失败ingest。
def _boom_ingest(kb_id, source_dir):
    raise ValueError("build failed")


# 测试上传回滚失败时保留日志。


# 验证 upload build failure restore fail keeps journal。
def test_upload_build_failure_restore_fail_keeps_journal(tmp_path):
    # 覆盖上传构建失败、且恢复旧文件失败：journal 必须保留，供启动恢复重试。
    source_dir = str(tmp_path / "src")
    os.makedirs(source_dir)
    dest = os.path.join(source_dir, "a.pdf")
    with open(dest, "wb") as f:
        f.write(b"OLD")

    journal = MutationJournal(journal_dir=str(tmp_path / "j"))
    mgr = IndexJobManager(
        ingest_fn=_boom_ingest,
        source_dir_for=lambda kb: source_dir,
        job_store=InMemoryJobStore(),
        journal=journal,
    )
    real_replace = __import__("os").replace

    # 构造或驱动 replace失败路径恢复状态 测试场景。
    def replace_fail_restore(src, dst):
        # 仅恢复方向（src 是 .bak）失败；备份创建方向放行，确保走到回滚失败分支。
        if str(src).endswith(".cogdoc-bak"):
            raise OSError("disk error")
        return real_replace(src, dst)

    with patch("cogdoc.api.ingest.os.replace", side_effect=replace_fail_restore):
        job = mgr.submit_upload("kb", source_dir, "a.pdf", b"NEW")
        mgr.run_blocking("kb", lambda: None)
    mgr.shutdown()

    assert any(p.name.endswith(".json") for p in (tmp_path / "j").iterdir())
    assert mgr.get(job["job_id"])["status"] == "failed"


# 验证 upload new file build failure remove fail keeps journal。
def test_upload_new_file_build_failure_remove_fail_keeps_journal(tmp_path):
    # 新增上传构建失败、删除残缺新文件失败：journal 保留。
    source_dir = str(tmp_path / "src")
    journal = MutationJournal(journal_dir=str(tmp_path / "j"))
    mgr = IndexJobManager(
        ingest_fn=_boom_ingest,
        source_dir_for=lambda kb: source_dir,
        job_store=InMemoryJobStore(),
        journal=journal,
    )
    with patch("cogdoc.api.ingest.os.remove", side_effect=OSError("locked")):
        mgr.submit_upload("kb", source_dir, "a.pdf", b"NEW")
        mgr.run_blocking("kb", lambda: None)
    mgr.shutdown()

    assert any(p.name.endswith(".json") for p in (tmp_path / "j").iterdir())


# 验证 journal recover remove fail keeps entry。
def test_journal_recover_remove_fail_keeps_entry(tmp_path):
    # 恢复时删除未提交新文件失败：保留 journal 条目下次重试。
    src = tmp_path / "src"
    src.mkdir()
    dest = src / "a.pdf"
    dest.write_bytes(b"NEW")
    backup = src / "a.pdf.job1.cogdoc-bak"

    j = MutationJournal(journal_dir=str(tmp_path / "j"))
    j.begin_upload("job1", "kb", str(dest), str(backup), had_old=False)
    state = MagicMock()
    state.active.return_value = None  # 未提交
    with (
        patch("cogdoc.service.kb_state.KBState", return_value=state),
        patch(
            "cogdoc.service.mutation_journal.os.remove", side_effect=OSError("locked")
        ),
    ):
        with pytest.raises(MutationJournalError, match="未恢复"):
            j.recover_all()
    # 删除失败 → 启动 fail-closed，条目保留供下次重试。
    assert (tmp_path / "j" / "job1.json").exists()


# 测试生命周期损坏时保留删除标记。


# 验证 lifecycle corrupt read fail closed。
def test_lifecycle_corrupt_read_fail_closed(tmp_path):
    path = tmp_path / "lifecycle.json"
    path.write_text("{ 损坏 json", encoding="utf-8")
    store = LifecycleStore(path=str(path))
    # 损坏 → 读路径 fail-closed，任意 KB 视为 deleting 拦读。
    assert store.status("anything") == LIFECYCLE_DELETING


# 验证 lifecycle corrupt set quarantines。
def test_lifecycle_corrupt_set_quarantines(tmp_path):
    path = tmp_path / "lifecycle.json"
    path.write_text('{"kbA": "deleted" 损坏', encoding="utf-8")
    store = LifecycleStore(path=str(path))
    with pytest.raises(RuntimeError, match="损坏已隔离"):
        store.set("kbB", LIFECYCLE_DELETING)
    # 损坏文件被改名留存（含 kbA tombstone），不静默丢弃。
    corrupt_files = list(tmp_path.glob("lifecycle.json.corrupt-*"))
    assert corrupt_files
    assert store.status("kbB") == LIFECYCLE_DELETING
    with pytest.raises(RuntimeError, match="degraded"):
        store.set("kbC", LIFECYCLE_ACTIVE)


# 测试清理队列损坏时保留任务。


# 验证 purge queue corrupt quarantines。
def test_purge_queue_corrupt_quarantines(tmp_path):
    path = tmp_path / "pq.json"
    path.write_text("[ 损坏", encoding="utf-8")
    q = PurgeQueue(path=str(path))
    with pytest.raises(PurgeQueueCorruptError):
        q.add("kb", "g1", not_before=0)
    # 损坏文件留存，队列进入 degraded，拒绝覆盖旧任务。
    assert list(tmp_path.glob("pq.json.corrupt-*"))
    assert (tmp_path / "pq.json.degraded").exists()
    with pytest.raises(PurgeQueueCorruptError):
        q.due(now=100)


def test_purge_queue_duplicate_merges_segment_metadata(tmp_path):
    path = tmp_path / "pq.json"
    queue = PurgeQueue(path=str(path))
    segment_a = "11111111-1111-1111-1111-111111111111"
    segment_b = "22222222-2222-2222-2222-222222222222"

    queue.add("kb", "g1", not_before=20, segment_ids=(segment_a,))
    queue.add("kb", "g1", not_before=10, segment_ids=(segment_b,))

    assert queue.due(now=10) == [
        {
            "kb_id": "kb",
            "gen_id": "g1",
            "not_before": 10,
            "segment_ids": [segment_a, segment_b],
        }
    ]


def test_purge_queue_identical_empty_duplicate_does_not_rewrite(tmp_path, monkeypatch):
    queue = PurgeQueue(path=str(tmp_path / "pq.json"))
    save_count = 0
    original_save = queue._save

    def counting_save(items):
        nonlocal save_count
        save_count += 1
        original_save(items)

    monkeypatch.setattr(queue, "_save", counting_save)

    queue.add("kb", "g1", not_before=10)
    queue.add("kb", "g1", not_before=10)

    assert save_count == 1
    assert queue.due(now=10) == [
        {"kb_id": "kb", "gen_id": "g1", "not_before": 10}
    ]


def test_purge_queue_duplicate_still_validates_new_segment_metadata(tmp_path):
    queue = PurgeQueue(path=str(tmp_path / "pq.json"))
    queue.add("kb", "g1", not_before=0)

    with pytest.raises(ValueError, match="segment ids"):
        queue.add("kb", "g1", not_before=0, segment_ids=("../outside",))


def test_purge_queue_quarantines_unhashable_segment_metadata(tmp_path):
    path = tmp_path / "pq.json"
    path.write_text(
        '[{"kb_id":"kb","gen_id":"g1","not_before":0,"segment_ids":[[]]}]',
        encoding="utf-8",
    )
    queue = PurgeQueue(path=str(path))

    with pytest.raises(PurgeQueueCorruptError):
        queue.due(now=100)

    assert list(tmp_path.glob("pq.json.corrupt-*"))


# 测试切代后旧日志不误回滚。


# 验证 committed marker prevents rollback after gen switch。
def test_committed_marker_prevents_rollback_after_gen_switch(tmp_path):
    # journal 标记 committed 后，即便 active 已切到别的 gen（或 KB 已删 active=None），也判为已提交→前滚不回滚。
    src = tmp_path / "src"
    src.mkdir()
    dest = src / "a.pdf"
    dest.write_bytes(b"NEW")
    backup = src / "a.pdf.job1.cogdoc-bak"
    backup.write_bytes(b"OLD")

    j = MutationJournal(journal_dir=str(tmp_path / "j"))
    j.begin_upload("job1", "kb", str(dest), str(backup), had_old=True)
    j.record_generation("job1", "gOLD")
    j.mark_committed("job1")
    state = MagicMock()
    state.active.return_value = {"id": "gNEWER"}  # active 已前进到更新的代
    with patch("cogdoc.service.kb_state.KBState", return_value=state):
        j.recover_all()

    assert dest.read_bytes() == b"NEW"  # 未被误回滚
    assert not backup.exists()


# 验证 committed marker survives deleted kb。
def test_committed_marker_survives_deleted_kb(tmp_path):
    # KB 已删 active=None：committed 标记仍判为已提交，不恢复备份、不重建文件。
    src = tmp_path / "src"
    src.mkdir()
    dest = src / "a.pdf"
    dest.write_bytes(b"NEW")
    backup = src / "a.pdf.job1.cogdoc-bak"
    backup.write_bytes(b"OLD")

    j = MutationJournal(journal_dir=str(tmp_path / "j"))
    j.begin_upload("job1", "kb", str(dest), str(backup), had_old=True)
    j.mark_committed("job1")
    state = MagicMock()
    state.active.return_value = None
    with patch("cogdoc.service.kb_state.KBState", return_value=state):
        j.recover_all()

    assert dest.read_bytes() == b"NEW"


# 验证 journal struct corrupt quarantined。
def test_journal_struct_corrupt_quarantined(tmp_path):
    import json

    jdir = tmp_path / "j"
    jdir.mkdir()
    (jdir / "bad.json").write_text(
        json.dumps({"op": "upload"}), encoding="utf-8"
    )  # 缺字段
    j = MutationJournal(journal_dir=str(jdir))
    with pytest.raises(MutationJournalError):
        j.recover_all()
    assert list(jdir.glob("bad.json.corrupt-*"))
    assert (jdir / ".degraded").exists()


# 测试生命周期损坏后保持全局关闭。


# 验证 lifecycle degraded persists global fail closed。
def test_lifecycle_degraded_persists_global_fail_closed(tmp_path):
    path = tmp_path / "lifecycle.json"
    path.write_text("{ 损坏", encoding="utf-8")
    store = LifecycleStore(path=str(path))
    with pytest.raises(RuntimeError, match="损坏已隔离"):
        store.set("kbB", LIFECYCLE_DELETING)  # 触发 degraded 标记
    # degraded 常驻：即便文件已重建，其他未记录 KB 仍 fail-closed，不默认 active。
    assert (tmp_path / "lifecycle.json.degraded").exists()
    assert store.status("other-kb") == LIFECYCLE_DELETING


# 测试纪元损坏后关闭写入。


# 验证 epoch corrupt quarantines and raises。
def test_epoch_corrupt_quarantines_and_raises(tmp_path):
    from cogdoc.service.kb_epoch import EpochStore, EpochCorruptError

    path = tmp_path / "epochs.json"
    path.write_text("{ 损坏", encoding="utf-8")
    store = EpochStore(path=str(path))
    with pytest.raises(EpochCorruptError):
        store.current("kb")
    assert list(tmp_path.glob("epochs.json.corrupt-*"))
