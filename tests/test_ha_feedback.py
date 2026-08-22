from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from cogdoc.api.tenancy import Permission
from cogdoc.ha.feedback import (
    HA_KB_EPOCH_FIELD,
    DistributedFeedbackAnalysisStore,
    DistributedFeedbackStore,
    StaleAuxiliaryWrite,
)
from cogdoc.ha.api_state import DistributedKnowledgeBaseRegistry
from cogdoc.ha.storage import SQLiteBackend


def _feedback(*, trace_id: str = "trace-one", feedback: str = "thumbs_down"):
    return {
        "kb_id": "storage-kb",
        "trace_id": trace_id,
        "session_id": "session",
        "feedback": feedback,
        "feedback_type": "bad_retrieval",
        "query": "question",
        "answer": "answer",
    }


def test_feedback_is_shared_and_quick_feedback_deduplicates_across_nodes(tmp_path):
    path = tmp_path / "feedback.db"
    first = DistributedFeedbackStore(SQLiteBackend(path))
    second = DistributedFeedbackStore(SQLiteBackend(path))

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(lambda store: store.record(_feedback()), (first, second))
        )

    assert len({result["feedback_id"] for result in results}) == 1
    assert sorted(result["deduplicated"] for result in results) == [False, True]
    rows = second.list(kb_id="storage-kb")
    assert len(rows) == 1
    assert rows[0]["eval_draft"]["case_type"] == "faithfulness"
    assert first.counts(kb_id="storage-kb") == {
        "total": 1,
        "bad_cases": 1,
        "by_feedback": {"thumbs_down": 1},
        "by_type": {"bad_retrieval": 1},
    }


def test_corrections_remain_distinct_and_import_is_conflict_safe(tmp_path):
    store = DistributedFeedbackStore(SQLiteBackend(tmp_path / "corrections.db"))
    first = store.record(_feedback(feedback="correction"))
    second = store.record(_feedback(feedback="correction"))
    assert first["feedback_id"] != second["feedback_id"]

    records = store.export_records()
    assert store.import_records(records) == {"imported": 0, "skipped": 2}
    conflicting = {**records[0], "answer": "different"}
    with pytest.raises(ValueError, match="already bound"):
        store.import_records([conflicting])


def test_feedback_analysis_is_shared_filterable_and_scope_clear_isolated(tmp_path):
    path = tmp_path / "analysis.db"
    first = DistributedFeedbackAnalysisStore(SQLiteBackend(path))
    second = DistributedFeedbackAnalysisStore(SQLiteBackend(path))
    row = first.record(
        "feedback-one",
        {"kb_id": "kb-one", "trace_id": "trace", "query": "question"},
        {
            "recommended_action": "review",
            "needs_review": True,
            "confidence": 0.9,
            "feedback_type": "bad_retrieval",
        },
    )
    first.record(
        "feedback-two",
        {"kb_id": "kb-two", "trace_id": "trace-two"},
        {
            "recommended_action": "ignore",
            "needs_review": False,
            "confidence": 0.2,
            "feedback_type": "other",
        },
    )

    assert second.list(kb_id="kb-one", min_confidence=0.8) == [row]
    assert second.counts(kb_id="kb-one")["needs_review"] == 1
    second.clear_kb("kb-one")
    assert first.list(kb_id="kb-one") == []
    assert len(first.list(kb_id="kb-two")) == 1


def test_feedback_records_are_size_bounded(tmp_path):
    store = DistributedFeedbackStore(SQLiteBackend(tmp_path / "bounded.db"))
    with pytest.raises(ValueError, match="size limit"):
        store.record({**_feedback(), "comment": "x" * (2 * 1024 * 1024)})


def test_feedback_write_is_fenced_by_kb_incarnation(tmp_path):
    backend = SQLiteBackend(tmp_path / "epoch.db")
    registry = DistributedKnowledgeBaseRegistry(backend, tmp_path / "cache")
    storage_id = str(registry.create("docs", "tenant", "owner")["storage_id"])
    store = DistributedFeedbackStore(backend)
    payload = {
        **_feedback(),
        "kb_id": storage_id,
        HA_KB_EPOCH_FIELD: registry.current(storage_id),
    }
    registry.bump(storage_id)

    with pytest.raises(StaleAuxiliaryWrite, match="incarnation"):
        store.record(payload)

    assert store.list(kb_id=storage_id) == []


def test_feedback_pipeline_writes_recheck_live_authority_in_each_transaction(
    tmp_path,
):
    backend = SQLiteBackend(tmp_path / "authority.db")
    registry = DistributedKnowledgeBaseRegistry(backend, tmp_path / "cache")
    record = registry.create("docs", "tenant", "owner")
    storage_id = str(record["storage_id"])
    epoch = int(record["epoch"])
    feedback = DistributedFeedbackStore(backend)
    analysis = DistributedFeedbackAnalysisStore(backend)
    calls: list[Permission] = []

    def checker(_connection, authority, *, required_permission):
        assert authority == {"proof": "live"}
        calls.append(required_permission)

    feedback.bind_authority_checker(checker)
    analysis.bind_authority_checker(checker)
    created = feedback.record_authorized(
        {**_feedback(), "kb_id": storage_id},
        expected_epoch=epoch,
        authority={"proof": "live"},
    )
    analysis.record_authorized(
        created["feedback_id"],
        {"kb_id": storage_id},
        {"recommended_action": "review", "confidence": 1.0},
        expected_epoch=epoch,
        authority={"proof": "live"},
    )
    assert calls == [Permission.WRITE, Permission.WRITE]

    def revoked(*_args, **_kwargs):
        raise PermissionError("revoked")

    other = DistributedFeedbackAnalysisStore(backend)
    other.bind_authority_checker(revoked)
    with pytest.raises(StaleAuxiliaryWrite, match="authority is stale"):
        other.record_authorized(
            created["feedback_id"],
            {"kb_id": storage_id},
            {"recommended_action": "ignore", "confidence": 0.1},
            expected_epoch=epoch,
            authority={"proof": "revoked"},
        )
    assert len(analysis.list(kb_id=storage_id)) == 1
