from unittest.mock import MagicMock, patch
import pytest
from cogdoc.service.mutation_journal import (
    MutationJournal,
    MutationJournalError,
)


# 构造测试变更日志。
def _journal(tmp_path):
    return MutationJournal(journal_dir=str(tmp_path / "journal"))


# 构造测试用活跃代。
def _active(gen_id):
    # 让 recovery 的严格提交证据读取返回指定 active gen。
    state = MagicMock()
    state.generation_is_committed.side_effect = lambda candidate: candidate == gen_id
    return patch("cogdoc.service.kb_state.KBState", return_value=state)


# 验证 recover upload overwrite staged rolls back。
def test_recover_upload_overwrite_staged_rolls_back(tmp_path):
    # 覆盖上传在提交前崩溃（gen_id 未记录）：磁盘是新内容、备份是旧内容 → 回滚到旧文件。
    src = tmp_path / "src"
    src.mkdir()
    dest = src / "a.pdf"
    dest.write_bytes(b"NEW")
    backup = src / "a.pdf.job1.cogdoc-bak"
    backup.write_bytes(b"OLD")

    j = _journal(tmp_path)
    j.begin_upload("job1", "kb", str(dest), str(backup), had_old=True)
    with _active(None):
        j.recover_all()

    assert dest.read_bytes() == b"OLD"
    assert not backup.exists()


# 验证 recover upload new staged removes file。
def test_recover_upload_new_staged_removes_file(tmp_path):
    # 新增上传提交前崩溃：删除未提交的新文件。
    src = tmp_path / "src"
    src.mkdir()
    dest = src / "a.pdf"
    dest.write_bytes(b"NEW")
    backup = src / "a.pdf.job1.cogdoc-bak"

    j = _journal(tmp_path)
    j.begin_upload("job1", "kb", str(dest), str(backup), had_old=False)
    with _active(None):
        j.recover_all()

    assert not dest.exists()


# 验证 recover upload committed keeps new。
def test_recover_upload_committed_keeps_new(tmp_path):
    # 覆盖上传已提交（gen_id == active）：前滚保留新文件，清旧备份。
    src = tmp_path / "src"
    src.mkdir()
    dest = src / "a.pdf"
    dest.write_bytes(b"NEW")
    backup = src / "a.pdf.job1.cogdoc-bak"
    backup.write_bytes(b"OLD")

    j = _journal(tmp_path)
    j.begin_upload("job1", "kb", str(dest), str(backup), had_old=True)
    j.record_generation("job1", "gNEW")
    with _active("gNEW"):
        j.recover_all()

    assert dest.read_bytes() == b"NEW"
    assert not backup.exists()


# 验证 recover upload gen recorded but not active rolls back。
def test_recover_upload_gen_recorded_but_not_active_rolls_back(tmp_path):
    # gen_id 已记录但 active 不是它（switch_active 前崩溃）：判未提交 → 回滚旧文件。
    src = tmp_path / "src"
    src.mkdir()
    dest = src / "a.pdf"
    dest.write_bytes(b"NEW")
    backup = src / "a.pdf.job1.cogdoc-bak"
    backup.write_bytes(b"OLD")

    j = _journal(tmp_path)
    j.begin_upload("job1", "kb", str(dest), str(backup), had_old=True)
    j.record_generation("job1", "gNEW")
    with _active("gOLD"):  # active 仍是旧代
        j.recover_all()

    assert dest.read_bytes() == b"OLD"


def test_recovery_pauses_when_generation_state_is_corrupt(tmp_path, monkeypatch):
    import cogdoc.service.kb_state as kb_state_module

    state_path = tmp_path / "state.json"
    state_path.write_text("{broken", encoding="utf-8")
    monkeypatch.setattr(
        kb_state_module,
        "get_settings",
        lambda: MagicMock(kb_state_path=lambda _kb_id: str(state_path)),
    )
    src = tmp_path / "src"
    src.mkdir()
    dest = src / "a.pdf"
    backup = src / "a.pdf.job1.cogdoc-bak"
    dest.write_bytes(b"NEW")
    backup.write_bytes(b"OLD")
    journal = _journal(tmp_path)
    journal.begin_upload("job1", "kb", str(dest), str(backup), had_old=True)
    journal.record_generation("job1", "gNEW")

    with pytest.raises(MutationJournalError, match="recovery is paused"):
        journal.recover_all()

    assert dest.read_bytes() == b"NEW"
    assert backup.read_bytes() == b"OLD"
    assert (tmp_path / "journal" / "job1.json").exists()


def test_recovery_pauses_when_generation_state_is_not_utf8(tmp_path, monkeypatch):
    import cogdoc.service.kb_state as kb_state_module

    state_path = tmp_path / "state.json"
    state_path.write_bytes(b"\xff\xfe\x00")
    monkeypatch.setattr(
        kb_state_module,
        "get_settings",
        lambda: MagicMock(kb_state_path=lambda _kb_id: str(state_path)),
    )
    src = tmp_path / "src"
    src.mkdir()
    dest = src / "a.pdf"
    backup = src / "a.pdf.job1.cogdoc-bak"
    dest.write_bytes(b"NEW")
    backup.write_bytes(b"OLD")
    journal = _journal(tmp_path)
    journal.begin_upload("job1", "kb", str(dest), str(backup), had_old=True)
    journal.record_generation("job1", "gNEW")

    with pytest.raises(MutationJournalError, match="recovery is paused"):
        journal.recover_all()

    assert dest.read_bytes() == b"NEW"
    assert backup.read_bytes() == b"OLD"
    assert (tmp_path / "journal" / "job1.json").exists()


# 验证 recover delete staged restores。
def test_recover_delete_staged_restores(tmp_path):
    # 删除提交前崩溃：从隔离区恢复文件。
    src = tmp_path / "src"
    src.mkdir()
    dest = src / "a.pdf"
    quar = src / "a.pdf.job1.cogdoc-bak"
    quar.write_bytes(b"DOC")

    j = _journal(tmp_path)
    j.begin_delete("job1", "kb", str(dest), str(quar))
    with _active(None):
        j.recover_all()

    assert dest.read_bytes() == b"DOC"
    assert not quar.exists()


# 验证 recover delete committed removes quarantine。
def test_recover_delete_committed_removes_quarantine(tmp_path):
    # 删除已提交（rebuild 后新 gen active）：前滚确认删除，清隔离文件。
    src = tmp_path / "src"
    src.mkdir()
    dest = src / "a.pdf"
    quar = src / "a.pdf.job1.cogdoc-bak"
    quar.write_bytes(b"DOC")

    j = _journal(tmp_path)
    j.begin_delete("job1", "kb", str(dest), str(quar))
    j.record_generation("job1", "gNEW")
    with _active("gNEW"):
        j.recover_all()

    assert not dest.exists()
    assert not quar.exists()


# 验证 recover clears entries。
def test_recover_clears_entries(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    dest = src / "a.pdf"
    backup = src / "a.pdf.job1.cogdoc-bak"

    j = _journal(tmp_path)
    j.begin_upload("job1", "kb", str(dest), str(backup), had_old=False)
    with _active(None):
        assert j.recover_all() == ["job1"]
        assert j.recover_all() == []


# 验证 recover tolerates missing backup。
def test_recover_tolerates_missing_backup(tmp_path):
    # staged 回滚但备份缺失（崩溃在移动之前）：不报错，保持现状。
    src = tmp_path / "src"
    src.mkdir()
    dest = src / "a.pdf"
    dest.write_bytes(b"OLD")
    backup = src / "a.pdf.job1.cogdoc-bak"  # 不存在

    j = _journal(tmp_path)
    j.begin_upload("job1", "kb", str(dest), str(backup), had_old=True)
    with _active(None):
        j.recover_all()

    assert dest.read_bytes() == b"OLD"


# 验证 clear removes entry。
def test_clear_removes_entry(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    j = _journal(tmp_path)
    j.begin_upload("job1", "kb", str(src / "a.pdf"), str(src / "b"), had_old=False)
    j.clear("job1")
    with _active(None):
        assert j.recover_all() == []


def test_clear_kb_removes_only_matching_recovery_entries(tmp_path):
    journal = _journal(tmp_path)
    journal.begin_upload(
        "job-kb",
        "kb",
        str(tmp_path / "kb" / "source.pdf"),
        str(tmp_path / "kb" / "backup.pdf"),
        had_old=True,
    )
    journal.begin_upload(
        "job-other",
        "other",
        str(tmp_path / "other" / "source.pdf"),
        str(tmp_path / "other" / "backup.pdf"),
        had_old=True,
    )

    assert journal.clear_kb("kb") == 1
    assert journal.has_entries("kb") is False
    assert journal.has_entries("other") is True


def test_clear_kb_fails_closed_on_unattributable_journal(tmp_path):
    journal = _journal(tmp_path)
    journal_dir = tmp_path / "journal"
    journal_dir.mkdir()
    (journal_dir / "broken.json").write_text("{", encoding="utf-8")

    with pytest.raises(MutationJournalError):
        journal.clear_kb("kb")
