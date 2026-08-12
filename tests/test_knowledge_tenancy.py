from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from httpx import ASGITransport, AsyncClient

from cogdoc.api.app import create_app
from cogdoc.api.derived_knowledge_store import DerivedKnowledgeStore
from cogdoc.api.feedback_analysis_store import FeedbackAnalysisStore
from cogdoc.api.feedback_store import FeedbackStore
from cogdoc.api.ingest import KnowledgeBaseRegistry
from cogdoc.api.retrieval_eval_draft_store import RetrievalEvalDraftStore
from cogdoc.api.retrieval_feedback_store import RetrievalFeedbackStore


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _make_app(tmp_path, monkeypatch):
    import cogdoc.api.app as app_module

    monkeypatch.setattr(app_module, "configure_logging", lambda: None)
    registry = KnowledgeBaseRegistry(
        registry_path=str(tmp_path / "registry.json"),
        source_dir_for=lambda storage_id: str(
            tmp_path / "sources" / storage_id
        ),
    )
    registry.create("shared", tenant_id="tenant-a", owner_id="alice")
    registry.create("shared", tenant_id="tenant-b", owner_id="bob")
    registry.create("a-only", tenant_id="tenant-a", owner_id="alice")
    knowledge_store = DerivedKnowledgeStore(
        path=str(tmp_path / "knowledge.jsonl")
    )
    app = create_app(
        kb_registry=registry,
        knowledge_store=knowledge_store,
        feedback_store=FeedbackStore(
            feedback_path=str(tmp_path / "feedback.jsonl"),
            bad_cases_path=str(tmp_path / "bad_cases.jsonl"),
        ),
        feedback_analysis_store=FeedbackAnalysisStore(
            path=str(tmp_path / "feedback_analysis.jsonl")
        ),
        retrieval_feedback_store=RetrievalFeedbackStore(
            path=str(tmp_path / "retrieval_feedback.jsonl")
        ),
        retrieval_eval_draft_store=RetrievalEvalDraftStore(
            path=str(tmp_path / "retrieval_eval_drafts.jsonl")
        ),
        api_principals={
            "a-owner": {
                "tenant_id": "tenant-a",
                "subject_id": "alice",
                "role": "owner",
            },
            "b-owner": {
                "tenant_id": "tenant-b",
                "subject_id": "bob",
                "role": "owner",
            },
            "default-owner": {
                "tenant_id": "default",
                "subject_id": "local-admin",
                "role": "owner",
            },
        },
    )
    return app, registry, knowledge_store


@asynccontextmanager
async def _client(app):
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            yield client


def _headers(key: str) -> dict[str, str]:
    return {"X-API-Key": key}


@pytest.mark.anyio
async def test_knowledge_uses_tenant_storage_and_server_principal_identity(
    tmp_path, monkeypatch
):
    app, registry, store = _make_app(tmp_path, monkeypatch)
    a_storage = registry.resolve("shared", "tenant-a")["storage_id"]
    b_storage = registry.resolve("shared", "tenant-b")["storage_id"]

    async with _client(app) as client:
        created_a = await client.post(
            "/v1/knowledge",
            json={
                "kb_id": "shared",
                "text": "tenant A knowledge",
                "created_by": "forged-creator",
            },
            headers=_headers("a-owner"),
        )
        created_b = await client.post(
            "/v1/knowledge",
            json={
                "kb_id": "shared",
                "text": "tenant B knowledge",
                "created_by": "forged-creator",
            },
            headers=_headers("b-owner"),
        )
        a_id = created_a.json()["knowledge"]["knowledge_id"]
        b_id = created_b.json()["knowledge"]["knowledge_id"]
        listed_a = await client.get(
            "/v1/knowledge",
            params={"kb_id": "shared"},
            headers=_headers("a-owner"),
        )
        listed_b = await client.get(
            "/v1/knowledge",
            params={"kb_id": "shared"},
            headers=_headers("b-owner"),
        )
        approved = await client.post(
            f"/v1/knowledge/{a_id}/approve",
            json={"actor": "forged-reviewer"},
            headers=_headers("a-owner"),
        )
        revised = await client.post(
            f"/v1/knowledge/{a_id}/revise",
            json={"text": "tenant A revised", "created_by": "forged-creator"},
            headers=_headers("a-owner"),
        )

    assert created_a.status_code == created_b.status_code == 201
    assert created_a.json()["knowledge"]["kb_id"] == "shared"
    assert created_b.json()["knowledge"]["kb_id"] == "shared"
    assert created_a.json()["knowledge"]["created_by"] == "alice"
    assert created_b.json()["knowledge"]["created_by"] == "bob"
    assert [row["knowledge_id"] for row in listed_a.json()["knowledge"]] == [a_id]
    assert [row["knowledge_id"] for row in listed_b.json()["knowledge"]] == [b_id]
    assert approved.json()["reviewed_by"] == "alice"
    assert revised.status_code == 201
    assert revised.json()["knowledge"]["created_by"] == "alice"
    assert revised.json()["knowledge"]["kb_id"] == "shared"
    assert store.get(a_id)["kb_id"] == a_storage
    assert store.get(b_id)["kb_id"] == b_storage
    assert a_storage != b_storage


@pytest.mark.anyio
async def test_cross_tenant_opaque_mutations_are_indistinguishable_from_missing(
    tmp_path, monkeypatch
):
    app, _, store = _make_app(tmp_path, monkeypatch)

    async with _client(app) as client:
        created_a = await client.post(
            "/v1/knowledge",
            json={"kb_id": "shared", "text": "tenant A knowledge"},
            headers=_headers("a-owner"),
        )
        created_b = await client.post(
            "/v1/knowledge",
            json={"kb_id": "shared", "text": "tenant B knowledge"},
            headers=_headers("b-owner"),
        )
        a_id = created_a.json()["knowledge"]["knowledge_id"]
        b_id = created_b.json()["knowledge"]["knowledge_id"]

        cross_responses = [
            await client.post(
                f"/v1/knowledge/{a_id}/{action}",
                json={},
                headers=_headers("b-owner"),
            )
            for action in ("approve", "reject", "archive")
        ]
        cross_responses.append(
            await client.post(
                f"/v1/knowledge/{a_id}/revise",
                json={"text": "stolen revision"},
                headers=_headers("b-owner"),
            )
        )
        cross_responses.append(
            await client.delete(
                f"/v1/knowledge/{a_id}", headers=_headers("b-owner")
            )
        )
        batch = await client.post(
            "/v1/knowledge/batch-approve",
            json={
                "knowledge_ids": [b_id, a_id, "missing"],
                "actor": "forged-reviewer",
            },
            headers=_headers("b-owner"),
        )

    for response in cross_responses:
        assert response.status_code == 404
        assert response.json()["error_code"] == "KNOWLEDGE_NOT_FOUND"
    assert store.get(a_id)["status"] == "pending"
    assert batch.status_code == 200
    assert [row["knowledge_id"] for row in batch.json()["updated"]] == [b_id]
    assert batch.json()["updated"][0]["reviewed_by"] == "bob"
    assert batch.json()["missing_ids"] == [a_id, "missing"]


@pytest.mark.anyio
async def test_knowledge_aggregates_and_index_status_are_tenant_scoped(
    tmp_path, monkeypatch
):
    app, registry, _ = _make_app(tmp_path, monkeypatch)
    a_storage = registry.resolve("shared", "tenant-a")["storage_id"]
    b_storage = registry.resolve("shared", "tenant-b")["storage_id"]
    status_calls = []

    def statuser(storage_id, store):
        status_calls.append(storage_id)
        return {
            "kb_id": storage_id,
            "state": "fresh",
            "approved_count": 0,
            "indexed_count": 0,
        }

    app.state.derived_knowledge_index_statuser = statuser
    app.state.feedback_store.record(
        {
            "kb_id": a_storage,
            "trace_id": "a-trace",
            "feedback": "thumbs_down",
        }
    )
    app.state.feedback_store.record(
        {
            "kb_id": b_storage,
            "trace_id": "b-trace",
            "feedback": "thumbs_down",
        }
    )
    app.state.feedback_analysis_store.record(
        "a-feedback",
        {"kb_id": a_storage, "trace_id": "a-trace", "query": "q"},
        {
            "feedback_type": "correction",
            "sentiment": "negative",
            "target": {"chunk_ids": [], "sources": [], "source_type": "none"},
            "extracted_claim": "a claim",
            "recommended_action": "create_pending_knowledge",
            "weight_delta": 0,
            "confidence": 0.9,
            "needs_review": True,
        },
    )

    async with _client(app) as client:
        await client.post(
            "/v1/knowledge",
            json={"kb_id": "shared", "text": "tenant A pending"},
            headers=_headers("a-owner"),
        )
        await client.post(
            "/v1/knowledge",
            json={"kb_id": "shared", "text": "tenant B pending"},
            headers=_headers("b-owner"),
        )
        pending = await client.get(
            "/v1/knowledge/pending-count",
            params={"kb_id": "shared"},
            headers=_headers("a-owner"),
        )
        metrics = await client.get(
            "/v1/feedback-loop-metrics",
            params={"kb_id": "shared", "answer_count": 1},
            headers=_headers("a-owner"),
        )
        summary = await client.get(
            "/v1/review-queue",
            params={"kb_id": "shared"},
            headers=_headers("a-owner"),
        )
        exported = await client.get(
            "/v1/review-queue/export",
            params={"kb_id": "shared"},
            headers=_headers("a-owner"),
        )
        index_status = await client.get(
            "/v1/knowledge/index-status",
            params={"kb_id": "shared"},
            headers=_headers("a-owner"),
        )

    assert pending.json()["kb_id"] == "shared"
    assert pending.json()["pending"] == 1
    assert pending.json()["feedback_analysis_needs_review"] == 1
    assert metrics.json()["kb_id"] == "shared"
    assert metrics.json()["counts"]["knowledge_total"] == 1
    assert metrics.json()["counts"]["feedback_total"] == 1
    assert summary.json()["kb_id"] == "shared"
    assert summary.json()["knowledge"]["pending"] == 1
    assert summary.json()["feedback_counts"]["total"] == 1
    assert exported.json()["kb_id"] == "shared"
    assert len(exported.json()["pending_knowledge"]) == 1
    assert exported.json()["pending_knowledge"][0]["text"] == "tenant A pending"
    assert exported.json()["feedback_analysis_needs_review"][0]["kb_id"] == "shared"
    assert index_status.json()["kb_id"] == "shared"
    assert status_calls == [a_storage]
    assert a_storage not in str(exported.json())
    assert b_storage not in str(exported.json())


@pytest.mark.anyio
async def test_unknown_kb_and_foreign_physical_id_do_not_cross_tenant_boundary(
    tmp_path, monkeypatch
):
    app, registry, store = _make_app(tmp_path, monkeypatch)
    a_storage = registry.resolve("a-only", "tenant-a")["storage_id"]
    store.create({"kb_id": a_storage, "text": "private knowledge"})

    async with _client(app) as client:
        b_missing = [
            await client.get(
                path,
                params={"kb_id": "a-only"},
                headers=_headers("b-owner"),
            )
            for path in (
                "/v1/knowledge",
                "/v1/knowledge/pending-count",
                "/v1/feedback-loop-metrics",
                "/v1/review-queue",
                "/v1/review-queue/export",
                "/v1/knowledge/index-status",
            )
        ]
        b_missing.append(
            await client.post(
                "/v1/knowledge",
                json={"kb_id": "a-only", "text": "forbidden"},
                headers=_headers("b-owner"),
            )
        )
        b_missing.append(
            await client.post(
                "/v1/knowledge/stale-scan",
                params={"kb_id": "a-only"},
                headers=_headers("b-owner"),
            )
        )
        physical_probe = await client.get(
            "/v1/knowledge",
            params={"kb_id": a_storage},
            headers=_headers("default-owner"),
        )

    for response in b_missing:
        assert response.status_code == 404
        assert response.json()["error_code"] == "KB_NOT_FOUND"
    assert physical_probe.status_code == 404
    assert physical_probe.json()["error_code"] == "KB_NOT_FOUND"
