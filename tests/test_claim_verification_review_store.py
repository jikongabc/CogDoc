import sqlite3

import pytest

from cogdoc.api.claim_verification_review_store import (
    ClaimReviewRevisionConflictError,
    ClaimVerificationReviewStore,
    SqliteClaimVerificationReviewStore,
)


def _candidate(review_id: str, *, task_type: str = "qa") -> dict:
    return {
        "review_id": review_id,
        "kb_id": "kb-a",
        "task_type": task_type,
        "policy_id": "1111111111111111",
        "effective_mode": "shadow",
        "decision": "would_allow",
        "claim_id": "c1",
        "claim": "报名截止日期是 9 月 30 日。",
        "actual_verdict": "supported",
        "reason": "精确匹配",
        "confidence": 0.9,
        "duration_ms": 12.5,
        "cited_chunk_ids": ["chunk-1"],
        "supporting_chunk_ids": ["chunk-1"],
        "evidence": [
            {
                "chunk_id": "chunk-1",
                "source": "guide.pdf",
                "page": 2,
                "page_start": 2,
                "page_end": 2,
                "text": "截止日期原文",
                "text_truncated": False,
            }
        ],
        "evidence_complete": True,
    }


def test_memory_review_store_is_tenant_scoped_idempotent_and_paginated():
    now = [1_000_000.0]
    store = ClaimVerificationReviewStore(clock=lambda: now[0])
    first_id = "1" * 32
    second_id = "2" * 32
    assert store.record_candidates("tenant-a", [_candidate(first_id)]) == 1
    assert store.record_candidates("tenant-a", [_candidate(first_id)]) == 0
    now[0] += 1
    assert store.record_candidates("tenant-a", [_candidate(second_id)]) == 1
    assert store.record_candidates("tenant-b", [_candidate(first_id)]) == 1

    page = store.list_page("tenant-a", limit=1)
    assert [item["review_id"] for item in page["items"]] == [second_id]
    assert page["next_cursor"]
    next_page = store.list_page(
        "tenant-a", limit=1, cursor=page["next_cursor"]
    )
    assert [item["review_id"] for item in next_page["items"]] == [first_id]
    assert store.list_page("tenant-b")["items"][0]["review_id"] == first_id
    with pytest.raises(ValueError, match="cursor"):
        store.list_page("tenant-a", cursor="not-a-cursor")


@pytest.mark.parametrize(
    "store_factory",
    [
        lambda path: ClaimVerificationReviewStore(),
        lambda path: SqliteClaimVerificationReviewStore(path),
    ],
)
def test_review_summary_buckets_exclude_evidence_text(tmp_path, store_factory):
    store = store_factory(str(tmp_path / "state.db"))
    store.record_candidates("tenant-a", [_candidate("9" * 32)])

    try:
        buckets = store.summary_buckets("tenant-a")
    finally:
        close = getattr(store, "close", None)
        if callable(close):
            close()

    assert buckets[0]["authorization_sources"] == ["guide.pdf"]
    assert buckets[0]["total_count"] == 1
    assert buckets[0]["pending_count"] == 1
    assert buckets[0]["actual_verdict_counts"]["supported"] == 1
    assert "evidence" not in buckets[0]
    assert "claim" not in buckets[0]
    assert "reason" not in buckets[0]


@pytest.mark.parametrize(
    "store_factory",
    [
        lambda path: ClaimVerificationReviewStore(),
        lambda path: SqliteClaimVerificationReviewStore(path),
    ],
)
def test_review_store_clear_kb_is_tenant_and_kb_scoped(tmp_path, store_factory):
    store = store_factory(str(tmp_path / "state.db"))
    other_kb = {**_candidate("2" * 32), "kb_id": "kb-b"}
    store.record_candidates("tenant-a", [_candidate("1" * 32), other_kb])
    store.record_candidates("tenant-b", [_candidate("1" * 32)])

    try:
        store.clear_kb("tenant-a", "kb-a")

        assert store.list_page("tenant-a", kb_id="kb-a")["items"] == []
        assert len(store.list_page("tenant-a", kb_id="kb-b")["items"]) == 1
        assert len(store.list_page("tenant-b", kb_id="kb-a")["items"]) == 1
    finally:
        close = getattr(store, "close", None)
        if callable(close):
            close()


def test_sqlite_review_store_backfills_compact_authorization_sources(tmp_path):
    path = str(tmp_path / "state.db")
    first = SqliteClaimVerificationReviewStore(path)
    first.record_candidates("tenant-a", [_candidate("8" * 32)])
    first.close()

    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "ALTER TABLE claim_verification_reviews "
            "DROP COLUMN authorization_sources"
        )
        connection.commit()
    finally:
        connection.close()

    migrated = SqliteClaimVerificationReviewStore(path)
    try:
        assert migrated.summary_buckets("tenant-a")[0][
            "authorization_sources"
        ] == ["guide.pdf"]
    finally:
        migrated.close()


def test_review_label_uses_revision_and_exports_gate_compatible_rows():
    store = ClaimVerificationReviewStore()
    review_id = "a" * 32
    store.record_candidates("tenant-a", [_candidate(review_id)])

    reviewed = store.label(
        "tenant-a",
        review_id,
        expected_verdict="unsupported",
        reviewer="alice",
        review_note="日期并不匹配",
        expected_revision=1,
    )

    assert reviewed["status"] == "reviewed"
    assert reviewed["revision"] == 2
    with pytest.raises(ClaimReviewRevisionConflictError):
        store.label(
            "tenant-a",
            review_id,
            expected_verdict="supported",
            reviewer="bob",
            expected_revision=1,
        )
    assert store.export_reviewed("tenant-b") == []
    exported = store.export_reviewed("tenant-a")
    assert exported == [
        {
            "id": review_id,
            "layer": "qa",
            "claim_id": "c1",
            "claim": "报名截止日期是 9 月 30 日。",
            "expected_verdict": "unsupported",
            "actual_verdict": "supported",
            "duration_ms": 12.5,
            "reviewer": "alice",
            "notes": "日期并不匹配",
            "policy_id": "1111111111111111",
        }
    ]


@pytest.mark.parametrize(
    "store_factory",
    [
        lambda path, clock: ClaimVerificationReviewStore(
            retention_days=1, clock=clock
        ),
        lambda path, clock: SqliteClaimVerificationReviewStore(
            path, retention_days=1, clock=clock
        ),
    ],
)
def test_expired_review_cannot_be_labeled(tmp_path, store_factory):
    now = [1_000_000.0]
    store = store_factory(str(tmp_path / "state.db"), lambda: now[0])
    review_id = "e" * 32
    store.record_candidates("tenant-a", [_candidate(review_id)])
    now[0] += 86401

    try:
        with pytest.raises(KeyError):
            store.label(
                "tenant-a",
                review_id,
                expected_verdict="supported",
                reviewer="alice",
                expected_revision=1,
            )
    finally:
        close = getattr(store, "close", None)
        if callable(close):
            close()


@pytest.mark.parametrize(
    "store_factory",
    [
        lambda path, clock: ClaimVerificationReviewStore(
            retention_days=1, clock=clock
        ),
        lambda path, clock: SqliteClaimVerificationReviewStore(
            path, retention_days=1, clock=clock
        ),
    ],
)
def test_expired_idempotency_key_can_be_sampled_again(tmp_path, store_factory):
    now = [1_000_000.0]
    store = store_factory(str(tmp_path / "state.db"), lambda: now[0])
    candidate = _candidate("f" * 32)
    assert store.record_candidates("tenant-a", [candidate]) == 1
    now[0] += 86401

    try:
        assert store.record_candidates("tenant-a", [candidate]) == 1
        assert len(store.list_page("tenant-a")["items"]) == 1
    finally:
        close = getattr(store, "close", None)
        if callable(close):
            close()


def test_sqlite_review_store_survives_restart_and_enforces_cap_and_retention(tmp_path):
    now = [1_000_000.0]
    path = str(tmp_path / "state.db")
    first = SqliteClaimVerificationReviewStore(
        path, max_per_tenant=2, retention_days=1, clock=lambda: now[0]
    )
    first.record_candidates("tenant-a", [_candidate("1" * 32)])
    now[0] += 1
    first.record_candidates("tenant-a", [_candidate("2" * 32)])
    now[0] += 1
    first.record_candidates("tenant-a", [_candidate("3" * 32)])
    first.label(
        "tenant-a",
        "3" * 32,
        expected_verdict="supported",
        reviewer="alice",
        expected_revision=1,
    )
    first.close()

    second = SqliteClaimVerificationReviewStore(
        path, max_per_tenant=2, retention_days=1, clock=lambda: now[0]
    )
    try:
        assert [
            item["review_id"] for item in second.list_page("tenant-a")["items"]
        ] == ["3" * 32, "2" * 32]
        assert second.export_reviewed("tenant-a")[0]["reviewer"] == "alice"
        now[0] += 25 * 3600
        second.record_candidates("tenant-b", [_candidate("4" * 32)])
        assert second.list_page("tenant-a")["items"] == []
    finally:
        second.close()


@pytest.mark.parametrize(
    "store_factory",
    [
        lambda path: ClaimVerificationReviewStore(),
        lambda path: SqliteClaimVerificationReviewStore(path),
    ],
)
def test_review_store_clear_document_is_source_and_scope_bound(tmp_path, store_factory):
    store = store_factory(str(tmp_path / "state.db"))
    target = _candidate("a" * 32)
    other_source = {
        **_candidate("b" * 32),
        "evidence": [
            {
                "chunk_id": "chunk-2",
                "source": "other.pdf",
                "text": "其他证据",
            }
        ],
    }
    store.record_candidates("tenant-a", [target, other_source])
    store.record_candidates("tenant-b", [target])

    try:
        assert store.clear_document("tenant-a", "kb-a", "guide.pdf") == 1
        remaining = store.list_page("tenant-a", kb_id="kb-a")["items"]
        other_tenant = store.list_page("tenant-b", kb_id="kb-a")["items"]

        assert [row["review_id"] for row in remaining] == ["b" * 32]
        assert [row["review_id"] for row in other_tenant] == ["a" * 32]
    finally:
        close = getattr(store, "close", None)
        if callable(close):
            close()
