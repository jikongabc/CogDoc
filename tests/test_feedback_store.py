import json

import pytest

from cogdoc.api.feedback_store import FeedbackStore, SqliteFeedbackStore


# 读取逐行对象文件。
def _read_jsonl(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


# 验证数据库反馈存储保留查询统计和导出副本。
def test_sqlite_feedback_store_records_lists_counts_and_exports(tmp_path):
    store = SqliteFeedbackStore(
        db_path=str(tmp_path / "feedback.db"),
        feedback_path=str(tmp_path / "feedback.jsonl"),
        bad_cases_path=str(tmp_path / "bad_cases.jsonl"),
    )

    up = store.record(
        {
            "kb_id": "kb",
            "trace_id": "t1",
            "session_id": "s1",
            "feedback": "thumbs_up",
            "feedback_type": "other",
        }
    )
    down = store.record(
        {
            "kb_id": "kb",
            "trace_id": "t2",
            "session_id": "s1",
            "feedback": "thumbs_down",
            "feedback_type": "bad_retrieval",
            "query": "问题",
            "answer": "答案",
        }
    )

    assert up["is_bad_case"] is False
    assert down["is_bad_case"] is True
    assert [row["trace_id"] for row in store.list(kb_id="kb", session_id="s1")] == [
        "t2",
        "t1",
    ]
    assert store.list(kb_id="kb", is_bad_case=True)[0]["trace_id"] == "t2"
    assert store.counts(kb_id="kb") == {
        "total": 2,
        "bad_cases": 1,
        "by_feedback": {"thumbs_up": 1, "thumbs_down": 1},
        "by_type": {"other": 1, "bad_retrieval": 1},
    }
    assert len(_read_jsonl(tmp_path / "feedback.jsonl")) == 2
    assert _read_jsonl(tmp_path / "bad_cases.jsonl")[0]["trace_id"] == "t2"


# 非纠错坏样本保留安全公开 ledger，供离线 occurrence 完整性诊断。
def test_feedback_bad_case_eval_draft_keeps_public_citation_ledger(tmp_path):
    store = SqliteFeedbackStore(
        db_path=str(tmp_path / "feedback.db"),
        feedback_path=str(tmp_path / "feedback.jsonl"),
        bad_cases_path=str(tmp_path / "bad_cases.jsonl"),
    )
    answer = "结论[a.pdf:P1]。"
    start = answer.index("[a.pdf:P1]")
    ledger = [
        {
            "evidence_id": "E001",
            "chunk_id": "c1",
            "source_type": "document",
            "source": "a.pdf",
            "page": 1,
            "span_start": 0,
            "span_end": 20,
            "occurrences": [
                {
                    "index": 0,
                    "answer_start": start,
                    "answer_end": start + len("[a.pdf:P1]"),
                }
            ],
        }
    ]
    evidence_ledger = [
        {
            "evidence_id": "E001",
            "chunk_id": "c1",
            "source_type": "document",
            "source": "a.pdf",
            "page": 1,
            "span_start": 0,
            "span_end": 20,
            "display_citation": "[a.pdf:P1]",
        }
    ]

    store.record(
        {
            "kb_id": "kb",
            "trace_id": "t-ledger",
            "feedback": "thumbs_down",
            "answer": answer,
            "citation_ledger": ledger,
            "evidence_ledger": evidence_ledger,
        }
    )

    bad_case = _read_jsonl(tmp_path / "bad_cases.jsonl")[0]
    assert bad_case["eval_draft"]["citation_ledger"] == ledger
    assert bad_case["eval_draft"]["evidence_ledger"] == evidence_ledger


# 验证数据库反馈存储同一回答只保留第一条赞踩反馈。
def test_sqlite_feedback_store_deduplicates_quick_trace_feedback(tmp_path):
    store = SqliteFeedbackStore(
        db_path=str(tmp_path / "feedback.db"),
        feedback_path=str(tmp_path / "feedback.jsonl"),
        bad_cases_path=str(tmp_path / "bad_cases.jsonl"),
    )

    first = store.record({"kb_id": "kb", "trace_id": "t1", "feedback": "thumbs_up"})
    second = store.record({"kb_id": "kb", "trace_id": "t1", "feedback": "thumbs_down"})

    assert first["deduplicated"] is False
    assert second["deduplicated"] is True
    assert second["feedback_id"] == first["feedback_id"]
    assert store.counts(kb_id="kb")["total"] == 1
    assert len(_read_jsonl(tmp_path / "feedback.jsonl")) == 1


# 验证赞踩去重同时包含知识库作用域。
def test_sqlite_feedback_store_keeps_quick_feedback_isolated_by_kb(tmp_path):
    store = SqliteFeedbackStore(
        db_path=str(tmp_path / "feedback.db"),
        feedback_path=str(tmp_path / "feedback.jsonl"),
        bad_cases_path=str(tmp_path / "bad_cases.jsonl"),
    )

    first = store.record({"kb_id": "", "trace_id": "t1", "feedback": "thumbs_up"})
    second = store.record({"kb_id": "kb", "trace_id": "t1", "feedback": "thumbs_down"})

    assert first["deduplicated"] is False
    assert second["deduplicated"] is False
    assert store.counts(kb_id="kb")["total"] == 1
    assert len(_read_jsonl(tmp_path / "feedback.jsonl")) == 2


def test_jsonl_feedback_store_keeps_quick_feedback_isolated_by_kb(tmp_path):
    store = FeedbackStore(
        feedback_path=str(tmp_path / "feedback.jsonl"),
        bad_cases_path=str(tmp_path / "bad_cases.jsonl"),
    )

    first = store.record(
        {
            "kb_id": "tenant-a-storage",
            "trace_id": "same",
            "feedback": "thumbs_up",
        }
    )
    second = store.record(
        {
            "kb_id": "tenant-b-storage",
            "trace_id": "same",
            "feedback": "thumbs_down",
        }
    )

    assert first["deduplicated"] is False
    assert second["deduplicated"] is False
    assert store.counts(kb_id="tenant-a-storage")["total"] == 1
    assert store.counts(kb_id="tenant-b-storage")["total"] == 1


# 验证数据库反馈存储允许同一回答后续纠错。
def test_sqlite_feedback_store_allows_correction_after_quick_feedback(tmp_path):
    store = SqliteFeedbackStore(
        db_path=str(tmp_path / "feedback.db"),
        feedback_path=str(tmp_path / "feedback.jsonl"),
        bad_cases_path=str(tmp_path / "bad_cases.jsonl"),
    )

    first = store.record({"kb_id": "kb", "trace_id": "t1", "feedback": "thumbs_down"})
    correction = store.record(
        {"kb_id": "kb", "trace_id": "t1", "feedback": "correction"}
    )

    assert first["deduplicated"] is False
    assert correction["deduplicated"] is False
    assert store.counts(kb_id="kb")["total"] == 2
    assert len(_read_jsonl(tmp_path / "feedback.jsonl")) == 2


# 验证数据库反馈存储可导入旧逐行对象文件。
def test_sqlite_feedback_store_bootstraps_from_jsonl(tmp_path):
    feedback_path = tmp_path / "feedback.jsonl"
    feedback_path.write_text(
        json.dumps(
            {
                "feedback_id": "f1",
                "created_at": "2026-01-01T00:00:00+00:00",
                "kb_id": "kb",
                "trace_id": "t1",
                "feedback": "correction",
                "feedback_type": "correction",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    store = SqliteFeedbackStore(
        db_path=str(tmp_path / "feedback.db"),
        feedback_path=str(feedback_path),
        bad_cases_path=str(tmp_path / "bad_cases.jsonl"),
    )

    assert store.list(kb_id="kb")[0]["feedback_id"] == "f1"
    assert store.counts(kb_id="kb")["bad_cases"] == 1


# 批量导入中任一记录失败时，不保留前面已写入的记录。
def test_sqlite_feedback_store_import_is_atomic(tmp_path):
    store = SqliteFeedbackStore(
        db_path=str(tmp_path / "feedback.db"),
        feedback_path=str(tmp_path / "feedback.jsonl"),
        bad_cases_path=str(tmp_path / "bad_cases.jsonl"),
        export_jsonl=False,
    )

    with pytest.raises(ValueError, match="feedback_id is required"):
        store.import_records(
            [
                {
                    "feedback_id": "f1",
                    "created_at": "2026-01-01T00:00:00+00:00",
                    "kb_id": "kb",
                    "feedback": "thumbs_up",
                },
                {"kb_id": "kb", "feedback": "thumbs_down"},
            ]
        )

    assert store.export_records() == []


def test_sqlite_feedback_store_import_reports_imported_and_skipped(tmp_path):
    store = SqliteFeedbackStore(
        db_path=str(tmp_path / "feedback.db"),
        feedback_path=str(tmp_path / "feedback.jsonl"),
        bad_cases_path=str(tmp_path / "bad_cases.jsonl"),
        export_jsonl=False,
    )
    records = [
        {
            "feedback_id": "f1",
            "created_at": "2026-01-01T00:00:00+00:00",
            "kb_id": "kb",
            "feedback": "thumbs_up",
        }
    ]

    assert store.import_records(records) == {"imported": 1, "skipped": 0}
    assert store.import_records(records) == {"imported": 0, "skipped": 1}


# 验证数据库反馈存储按 KB 清理数据库和导出副本。
def test_sqlite_feedback_store_clear_kb(tmp_path):
    store = SqliteFeedbackStore(
        db_path=str(tmp_path / "feedback.db"),
        feedback_path=str(tmp_path / "feedback.jsonl"),
        bad_cases_path=str(tmp_path / "bad_cases.jsonl"),
    )
    store.record({"kb_id": "kb", "trace_id": "t1", "feedback": "thumbs_down"})
    store.record({"kb_id": "other", "trace_id": "t2", "feedback": "thumbs_down"})

    store.clear_kb("kb")

    assert store.counts(kb_id="kb")["total"] == 0
    assert store.counts(kb_id="other")["total"] == 1
    assert [row["kb_id"] for row in _read_jsonl(tmp_path / "feedback.jsonl")] == [
        "other"
    ]
    assert [row["kb_id"] for row in _read_jsonl(tmp_path / "bad_cases.jsonl")] == [
        "other"
    ]
