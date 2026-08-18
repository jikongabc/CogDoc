import pytest
from httpx import ASGITransport, AsyncClient

from cogdoc.api.app import create_app
from cogdoc.api.claim_verification_review_store import (
    ClaimVerificationReviewStore,
)
from cogdoc.api.claim_verification_store import ClaimVerificationObservationStore
from cogdoc.api.ingest import KnowledgeBaseRegistry
from cogdoc.api.session_store import SessionStore
from cogdoc.config.settings import Settings
from cogdoc.service.chat_service import ChatEvent, ChatResult
from cogdoc.tools.eval.claim_verification_eval import run_eval


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _candidate(
    review_id: str, *, kb_id: str, source: str = "guide.pdf"
) -> dict:
    return {
        "review_id": review_id,
        "kb_id": kb_id,
        "task_type": "qa",
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
                "source": source,
                "page": 2,
                "page_start": 2,
                "page_end": 2,
                "text": "截止日期原文",
                "text_truncated": False,
            }
        ],
        "evidence_complete": True,
    }


def _app(monkeypatch, store, tmp_path, *, runner=None, stream_runner=None):
    import cogdoc.api.app as app_module

    monkeypatch.setattr(app_module, "configure_logging", lambda: None)
    registry = KnowledgeBaseRegistry(
        str(tmp_path / "registry.json"),
        source_dir_for=lambda storage_id: str(tmp_path / "sources" / storage_id),
    )
    tenant_a = registry.create("kb", "tenant-a", "alice")
    tenant_b = registry.create("kb", "tenant-b", "bob")
    app = create_app(
        chat_runner=runner,
        chat_stream_runner=stream_runner,
        session_store=SessionStore(),
        kb_registry=registry,
        claim_verification_observation_store=ClaimVerificationObservationStore(),
        claim_verification_review_store=store,
        api_principals={
            "a-reviewer": {
                "tenant_id": "tenant-a",
                "subject_id": "alice",
                "role": "reviewer",
            },
            "a-viewer": {
                "tenant_id": "tenant-a",
                "subject_id": "amy",
                "role": "viewer",
            },
            "b-reviewer": {
                "tenant_id": "tenant-b",
                "subject_id": "bob",
                "role": "reviewer",
            },
        },
    )
    return app, str(tenant_a["storage_id"]), str(tenant_b["storage_id"])


@pytest.mark.anyio
async def test_review_api_is_reviewer_only_tenant_scoped_and_minimizes_list(
    monkeypatch, tmp_path,
):
    store = ClaimVerificationReviewStore()
    review_id = "a" * 32
    app, tenant_a_kb, tenant_b_kb = _app(monkeypatch, store, tmp_path)
    store.record_candidates(
        "tenant-a", [_candidate(review_id, kb_id=tenant_a_kb)]
    )
    store.record_candidates(
        "tenant-b", [_candidate("b" * 32, kb_id=tenant_b_kb)]
    )

    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            denied = await client.get(
                "/v1/claim-verification/reviews",
                headers={"X-API-Key": "a-viewer"},
            )
            listed = await client.get(
                "/v1/claim-verification/reviews",
                headers={"X-API-Key": "a-reviewer"},
            )
            detail = await client.get(
                f"/v1/claim-verification/reviews/{review_id}",
                headers={"X-API-Key": "a-reviewer"},
            )
            cross_tenant = await client.get(
                f"/v1/claim-verification/reviews/{review_id}",
                headers={"X-API-Key": "b-reviewer"},
            )
            bad_cursor = await client.get(
                "/v1/claim-verification/reviews?cursor=invalid",
                headers={"X-API-Key": "a-reviewer"},
            )

    assert denied.status_code == 403
    assert listed.status_code == 200
    assert listed.json()["tenant_id"] == "tenant-a"
    assert len(listed.json()["items"]) == 1
    assert listed.json()["items"][0]["evidence_count"] == 1
    assert "evidence" not in listed.json()["items"][0]
    assert "kb_id" not in listed.json()["items"][0]
    assert detail.status_code == 200
    assert detail.json()["evidence"][0]["text"] == "截止日期原文"
    assert "kb_id" not in detail.json()
    assert "authorization_source" not in detail.json()["evidence"][0]
    assert cross_tenant.status_code == 404
    assert bad_cursor.status_code == 422


@pytest.mark.anyio
async def test_review_api_rechecks_source_acl_across_list_detail_label_and_export(
    monkeypatch, tmp_path,
):
    import cogdoc.api.routes.claim_verification as review_routes
    from cogdoc.tools.retriever.scope import RetrievalScope

    now = [1_000_000.0]
    store = ClaimVerificationReviewStore(clock=lambda: now[0])
    app, tenant_a_kb, _ = _app(monkeypatch, store, tmp_path)
    first_id = "1" * 32
    hidden_id = "2" * 32
    newest_id = "3" * 32
    store.record_candidates(
        "tenant-a", [_candidate(first_id, kb_id=tenant_a_kb)]
    )
    now[0] += 1
    store.record_candidates(
        "tenant-a",
        [_candidate(hidden_id, kb_id=tenant_a_kb, source="private.pdf")],
    )
    now[0] += 1
    store.record_candidates(
        "tenant-a", [_candidate(newest_id, kb_id=tenant_a_kb)]
    )
    store.label(
        "tenant-a",
        hidden_id,
        expected_verdict="supported",
        reviewer="system",
        expected_revision=1,
    )
    monkeypatch.setattr(
        review_routes,
        "retrieval_scope_for_request",
        lambda request, scope: RetrievalScope(allowed_sources=("guide.pdf",)),
    )

    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            first_page = await client.get(
                "/v1/claim-verification/reviews?limit=1",
                headers={"X-API-Key": "a-reviewer"},
            )
            second_page = await client.get(
                "/v1/claim-verification/reviews",
                params={"limit": 1, "cursor": first_page.json()["next_cursor"]},
                headers={"X-API-Key": "a-reviewer"},
            )
            hidden_detail = await client.get(
                f"/v1/claim-verification/reviews/{hidden_id}",
                headers={"X-API-Key": "a-reviewer"},
            )
            hidden_label = await client.post(
                f"/v1/claim-verification/reviews/{hidden_id}/label",
                headers={"X-API-Key": "a-reviewer"},
                json={"expected_verdict": "supported", "expected_revision": 2},
            )
            summary = await client.get(
                "/v1/claim-verification/reviews/summary",
                headers={"X-API-Key": "a-reviewer"},
            )
            exported = await client.get(
                "/v1/claim-verification/reviews/export",
                headers={"X-API-Key": "a-reviewer"},
            )

    assert [item["review_id"] for item in first_page.json()["items"]] == [
        newest_id
    ]
    assert [item["review_id"] for item in second_page.json()["items"]] == [
        first_id
    ]
    assert hidden_detail.status_code == 404
    assert hidden_label.status_code == 404
    assert summary.status_code == 200
    assert summary.json()["total_count"] == 2
    assert summary.json()["pending_count"] == 2
    assert summary.json()["reviewed_count"] == 0
    assert summary.json()["actual_verdict_counts"]["supported"] == 2
    assert exported.json()["items"] == []


@pytest.mark.anyio
async def test_review_summary_uses_store_aggregate_instead_of_evidence_pages(
    monkeypatch, tmp_path,
):
    class AggregateOnlyStore(ClaimVerificationReviewStore):
        def list_page(self, *args, **kwargs):
            raise AssertionError("summary must not load evidence pages")

    store = AggregateOnlyStore()
    app, tenant_a_kb, _ = _app(monkeypatch, store, tmp_path)
    store.record_candidates(
        "tenant-a", [_candidate("7" * 32, kb_id=tenant_a_kb)]
    )

    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            summary = await client.get(
                "/v1/claim-verification/reviews/summary",
                headers={"X-API-Key": "a-reviewer"},
            )

    assert summary.status_code == 200
    assert summary.json()["total_count"] == 1


@pytest.mark.anyio
async def test_review_label_conflict_and_export_are_closed_and_tenant_scoped(
    monkeypatch, tmp_path,
):
    store = ClaimVerificationReviewStore()
    review_id = "c" * 32
    app, tenant_a_kb, _ = _app(monkeypatch, store, tmp_path)
    store.record_candidates(
        "tenant-a", [_candidate(review_id, kb_id=tenant_a_kb)]
    )

    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            labeled = await client.post(
                f"/v1/claim-verification/reviews/{review_id}/label",
                headers={"X-API-Key": "a-reviewer"},
                json={
                    "expected_verdict": "unsupported",
                    "expected_revision": 1,
                    "review_note": "证据日期不一致",
                },
            )
            conflict = await client.post(
                f"/v1/claim-verification/reviews/{review_id}/label",
                headers={"X-API-Key": "a-reviewer"},
                json={
                    "expected_verdict": "supported",
                    "expected_revision": 1,
                },
            )
            exported = await client.get(
                "/v1/claim-verification/reviews/export",
                headers={"X-API-Key": "a-reviewer"},
            )
            other_export = await client.get(
                "/v1/claim-verification/reviews/export",
                headers={"X-API-Key": "b-reviewer"},
            )
            summary = await client.get(
                "/v1/claim-verification/reviews/summary",
                headers={"X-API-Key": "a-reviewer"},
            )

    assert labeled.status_code == 200
    assert labeled.json()["revision"] == 2
    assert labeled.json()["reviewer"] == "alice"
    assert conflict.status_code == 409
    assert conflict.json()["error_code"] == "CLAIM_REVIEW_REVISION_CONFLICT"
    assert exported.json()["count"] == 1
    item = exported.json()["items"][0]
    assert item["expected_verdict"] == "unsupported"
    assert item["actual_verdict"] == "supported"
    report = run_eval(exported.json()["items"], bootstrap_iterations=5)
    assert report["config"]["num_cases"] == 1
    assert other_export.json()["count"] == 0
    assert summary.json()["agreement_count"] == 0
    assert summary.json()["disagreement_count"] == 1
    assert summary.json()["agreement_rate"] == 0.0


def _runner(doc_id, query, is_local, chat_history, forced_task):
    return ChatResult(
        answer="报名截止日期是 9 月 30 日。[guide.pdf:P2]",
        task_type="qa",
        citations=[],
        evidence=[],
        critique="",
        is_valid=True,
        trace_id="trace-review-sample",
        request_id="trace-review-sample",
        steps=[],
        chat_messages=[],
        raw_output={
            "claim_verification_rollout": {
                "mode": "shadow",
                "configured_mode": "shadow",
                "policy_id": "1111111111111111",
                "rollout_percent": 100,
                "cohort_selected": True,
                "decision": "would_allow",
                "audit_status": "passed",
                "executed": True,
            },
            "claim_audit": {
                "status": "passed",
                "verifier": {"duration_ms": 10},
                "claims": [
                    {
                        "claim_id": "c1",
                        "text": "报名截止日期是 9 月 30 日。",
                        "verdict": "supported",
                        "reason": "有引用",
                        "confidence": 0.9,
                        "cited_chunk_ids": ["chunk-1"],
                        "supporting_chunk_ids": ["chunk-1"],
                    }
                ],
            },
            "reranked_docs": [
                {
                    "text": "报名截止日期是 9 月 30 日。",
                    "meta": {
                        "chunk_id": "chunk-1",
                        "source": "guide.pdf",
                        "page": 2,
                    },
                }
            ],
        },
    )


@pytest.mark.anyio
async def test_sync_chat_opt_in_sampling_creates_review_item(monkeypatch, tmp_path):
    import cogdoc.api.claim_verification_observability as observability

    settings = Settings(
        _env_file=None,
        claim_verification_review_sample_percent=100,
    )
    monkeypatch.setattr(observability, "get_settings", lambda: settings)
    store = ClaimVerificationReviewStore()
    app, _, _ = _app(monkeypatch, store, tmp_path, runner=_runner)

    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            chat = await client.post(
                "/v1/chat",
                json={"query": "截止日期？", "doc_id": "kb"},
                headers={"X-API-Key": "a-reviewer"},
            )
            reviews = await client.get(
                "/v1/claim-verification/reviews",
                headers={"X-API-Key": "a-reviewer"},
            )

    assert chat.status_code == 200
    assert reviews.status_code == 200
    assert len(reviews.json()["items"]) == 1


@pytest.mark.anyio
async def test_stream_final_creates_exactly_one_review_item(monkeypatch, tmp_path):
    import cogdoc.api.claim_verification_observability as observability

    def stream_runner(doc_id, query, is_local, chat_history, forced_task):
        yield ChatEvent(
            "final",
            {"result": _runner(doc_id, query, is_local, chat_history, forced_task)},
        )

    settings = Settings(
        _env_file=None,
        claim_verification_review_sample_percent=100,
    )
    monkeypatch.setattr(observability, "get_settings", lambda: settings)
    store = ClaimVerificationReviewStore()
    app, _, _ = _app(
        monkeypatch, store, tmp_path, stream_runner=stream_runner
    )

    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            response = await client.post(
                "/v1/chat/stream",
                json={"query": "截止日期？", "doc_id": "kb"},
                headers={"X-API-Key": "a-reviewer"},
            )
            reviews = await client.get(
                "/v1/claim-verification/reviews",
                headers={"X-API-Key": "a-reviewer"},
            )

    assert response.status_code == 200
    assert len(reviews.json()["items"]) == 1


@pytest.mark.anyio
async def test_review_sampling_failure_never_breaks_chat(monkeypatch, tmp_path):
    import cogdoc.api.claim_verification_observability as observability

    class FailingReviewStore:
        def record_candidates(self, tenant_id, candidates):
            raise RuntimeError("disk unavailable")

    settings = Settings(
        _env_file=None,
        claim_verification_review_sample_percent=100,
    )
    monkeypatch.setattr(observability, "get_settings", lambda: settings)
    app, _, _ = _app(
        monkeypatch, FailingReviewStore(), tmp_path, runner=_runner
    )

    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            chat = await client.post(
                "/v1/chat",
                json={"query": "截止日期？", "doc_id": "kb"},
                headers={"X-API-Key": "a-reviewer"},
            )

    assert chat.status_code == 200
    assert chat.json()["answer"].startswith("报名截止日期")


def test_review_summary_merges_bucket_without_counting_pending_as_labeled():
    from cogdoc.api.routes.claim_verification import _review_queue_summary

    summary = _review_queue_summary(
        "tenant-a",
        [
            {
                "total_count": 1,
                "pending_count": 1,
                "reviewed_count": 0,
                "shadow_count": 1,
                "enforce_count": 0,
                "evidence_incomplete_count": 0,
                "agreement_count": 0,
                "disagreement_count": 0,
                "oldest_pending_at": "2026-08-19T00:00:00+00:00",
                "actual_verdict_counts": {"supported": 1},
                "expected_verdict_counts": {},
            }
        ],
    )

    assert summary["total_count"] == 1
    assert summary["agreement_count"] == 0
    assert summary["disagreement_count"] == 0
    assert summary["agreement_rate"] is None
    assert summary["expected_verdict_counts"]["unsupported"] == 0
