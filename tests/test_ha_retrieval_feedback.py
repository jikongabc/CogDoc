from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from cogdoc.api.retrieval_eval_draft_store import DraftRevisionConflictError
from cogdoc.api.tenancy import Permission
from cogdoc.ha.api_state import DistributedKnowledgeBaseRegistry
from cogdoc.ha.feedback import StaleAuxiliaryWrite
from cogdoc.ha.index_generation import IndexGenerationStore
from cogdoc.ha.retrieval_feedback import (
    DistributedRetrievalEvalDraftStore,
    DistributedRetrievalFeedbackStore,
)
from cogdoc.ha.storage import SQLiteBackend
from cogdoc.tools.eval.retrieval_eval_drafts import build_retrieval_eval_draft


def _payload():
    return {
        "kb_id": "storage-kb",
        "query": "Where is the guide?",
        "feedback": "thumbs_down",
        "feedback_type": "bad_retrieval",
        "evidence": [
            {"chunk_id": "chunk-one", "source_type": "document"},
            {"chunk_id": "chunk-two", "source_type": "document"},
        ],
    }


def test_retrieval_feedback_is_idempotent_shared_and_atomically_disabled(tmp_path):
    path = tmp_path / "retrieval.db"
    first = DistributedRetrievalFeedbackStore(SQLiteBackend(path))
    second = DistributedRetrievalFeedbackStore(SQLiteBackend(path))
    with ThreadPoolExecutor(max_workers=2) as executor:
        rows = list(
            executor.map(
                lambda store: store.record_from_feedback("feedback-one", _payload()),
                (first, second),
            )
        )
    record_id = rows[0][0]["retrieval_feedback_id"]
    assert rows[1][0]["retrieval_feedback_id"] == record_id
    assert second.boosts_for_query("storage-kb", "Where is the guide?") == {
        "chunk-one": -0.35,
        "chunk-two": -0.35,
    }

    disabled = first.set_enabled(record_id, False, actor="reviewer", reason="bad")
    assert disabled is not None and disabled["enabled"] is False
    assert second.boosts_for_query("storage-kb", "Where is the guide?") == {}
    assert second.counts(kb_id="storage-kb") == {
        "total": 1,
        "enabled": 0,
        "disabled": 1,
    }


def _draft(kb_id="storage-kb"):
    return build_retrieval_eval_draft(
        {
            "feedback_id": "feedback-one",
            "kb_id": kb_id,
            "query": "question",
            "task_type": "qa",
        },
        {
            "trace_id": "trace-one",
            "task_type": "qa",
            "input": {"query": "question"},
            "output": {
                "evidence_requirements": [
                    {
                        "requirement_id": "r1",
                        "question": "question",
                        "retrieval_query": "question",
                        "recovery_query": "question details",
                    }
                ]
            },
            "config": {
                "doc_id": kb_id,
                "index_generation": "generation-1",
                "index_build_version": "hybrid-v2",
                "chunk_identity_version": "chunk-v5",
            },
        },
    )


def test_eval_draft_cross_node_ensure_and_revision_cas(tmp_path):
    path = tmp_path / "drafts.db"
    first = DistributedRetrievalEvalDraftStore(SQLiteBackend(path))
    second = DistributedRetrievalEvalDraftStore(SQLiteBackend(path))
    draft = _draft()
    with ThreadPoolExecutor(max_workers=2) as executor:
        rows = list(executor.map(lambda store: store.ensure(draft), (first, second)))
    assert rows[0]["draft_id"] == rows[1]["draft_id"]
    revision = int(rows[0]["revision"])

    approved = first.approve(
        rows[0]["draft_id"],
        reviewer="reviewer",
        annotations={"no_answer": True},
        expected_revision=revision,
    )
    assert approved["status"] == "approved"
    with pytest.raises(DraftRevisionConflictError):
        second.reject(
            rows[0]["draft_id"],
            reviewer="other",
            reason="stale",
            expected_revision=revision,
        )


def test_retrieval_scope_clear_does_not_touch_another_kb(tmp_path):
    backend = SQLiteBackend(tmp_path / "scope.db")
    feedback = DistributedRetrievalFeedbackStore(backend)
    drafts = DistributedRetrievalEvalDraftStore(backend)
    feedback.record_from_feedback("one", _payload())
    other = {**_payload(), "kb_id": "other-kb"}
    feedback.record_from_feedback("two", other)
    draft = _draft().model_copy(update={"kb_id": "storage-kb"})
    drafts.ensure(draft)

    feedback.clear_kb("storage-kb")
    drafts.clear_kb("storage-kb")

    assert feedback.list(kb_id="storage-kb") == []
    assert len(feedback.list(kb_id="other-kb")) == 1
    assert drafts.list(kb_id="storage-kb") == []


def test_retrieval_tuning_and_draft_review_are_epoch_and_authority_fenced(tmp_path):
    backend = SQLiteBackend(tmp_path / "authority.db")
    registry = DistributedKnowledgeBaseRegistry(backend, tmp_path / "cache")
    record = registry.create("docs", "tenant", "owner")
    storage_id = str(record["storage_id"])
    epoch = int(record["epoch"])
    feedback = DistributedRetrievalFeedbackStore(backend)
    drafts = DistributedRetrievalEvalDraftStore(backend)
    calls: list[Permission] = []

    def checker(_connection, authority, *, required_permission):
        assert authority == {"proof": "live"}
        calls.append(required_permission)

    feedback.bind_authority_checker(checker)
    drafts.bind_authority_checker(checker)
    row = feedback.record_from_feedback_authorized(
        "feedback-one",
        {**_payload(), "kb_id": storage_id},
        expected_epoch=epoch,
        authority={"proof": "live"},
    )[0]
    disabled = feedback.set_enabled_authorized(
        row["retrieval_feedback_id"],
        False,
        expected_epoch=epoch,
        authority={"proof": "live"},
    )
    assert disabled is not None and disabled["enabled"] is False

    draft = _draft(storage_id)
    ensured = drafts.ensure_authorized(
        draft,
        expected_epoch=epoch,
        authority={"proof": "live"},
    )
    reviewed = drafts.review_authorized(
        ensured["draft_id"],
        decision="rejected",
        reviewer="reviewer",
        reason="not representative",
        expected_revision=int(ensured["revision"]),
        expected_epoch=epoch,
        authority={"proof": "live"},
    )
    assert reviewed["status"] == "rejected"
    assert calls == [
        Permission.WRITE,
        Permission.WRITE,
        Permission.WRITE,
        Permission.REVIEW,
    ]

    registry.bump(storage_id)
    with pytest.raises(StaleAuxiliaryWrite, match="incarnation"):
        feedback.set_enabled_authorized(
            row["retrieval_feedback_id"],
            True,
            expected_epoch=epoch,
            authority={"proof": "live"},
        )


def test_draft_approval_rechecks_shared_index_head_in_write_transaction(tmp_path):
    backend = SQLiteBackend(tmp_path / "draft-index-fence.db")
    registry = DistributedKnowledgeBaseRegistry(backend, tmp_path / "cache")
    record = registry.create("docs", "tenant", "owner")
    storage_id = str(record["storage_id"])
    epoch = int(record["epoch"])
    drafts = DistributedRetrievalEvalDraftStore(backend)
    IndexGenerationStore(backend)

    def checker(_connection, authority, *, required_permission):
        assert authority["tenant_id"] == "tenant"
        assert required_permission in {Permission.WRITE, Permission.REVIEW}

    drafts.bind_authority_checker(checker)
    ensured = drafts.ensure_authorized(
        _draft(storage_id),
        expected_epoch=epoch,
        authority={"tenant_id": "tenant"},
    )
    with backend.transaction(write=True) as connection:
        connection.execute(
            "INSERT INTO ha_index_heads(tenant_id,kb_id,current_generation_id,updated_at) "
            "VALUES('tenant',?,'generation-2',0)",
            (storage_id,),
        )

    with pytest.raises(StaleAuxiliaryWrite, match="index generation changed"):
        drafts.review_authorized(
            ensured["draft_id"],
            decision="approved",
            reviewer="reviewer",
            annotations={"no_answer": True},
            expected_revision=int(ensured["revision"]),
            expected_epoch=epoch,
            authority={"tenant_id": "tenant"},
            expected_index_generation="generation-1",
        )

    current = drafts.get(ensured["draft_id"])
    assert current is not None and current["status"] == "pending"
