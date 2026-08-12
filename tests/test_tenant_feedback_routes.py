from __future__ import annotations

import json

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


def _headers(key: str) -> dict[str, str]:
    return {"X-API-Key": key}


def _make_app(tmp_path, monkeypatch):
    import cogdoc.api.app as app_module

    monkeypatch.setattr(app_module, "configure_logging", lambda: None)
    registry = KnowledgeBaseRegistry(
        registry_path=str(tmp_path / "registry.json"),
        source_dir_for=lambda storage_id: str(
            tmp_path / "knowledge-bases" / storage_id / "sources"
        ),
    )
    tenant_a = registry.create("shared", "tenant-a", "alice")
    tenant_b = registry.create("shared", "tenant-b", "bob")
    stores = {
        "feedback_store": FeedbackStore(
            str(tmp_path / "feedback.jsonl"),
            str(tmp_path / "bad-cases.jsonl"),
        ),
        "feedback_analysis_store": FeedbackAnalysisStore(
            str(tmp_path / "feedback-analysis.jsonl")
        ),
        "knowledge_store": DerivedKnowledgeStore(
            str(tmp_path / "knowledge.jsonl")
        ),
        "retrieval_feedback_store": RetrievalFeedbackStore(
            str(tmp_path / "retrieval-feedback.jsonl")
        ),
        "retrieval_eval_draft_store": RetrievalEvalDraftStore(
            str(tmp_path / "retrieval-eval-drafts.jsonl")
        ),
    }
    app = create_app(
        kb_registry=registry,
        **stores,
        api_principals={
            "key-a": {
                "tenant_id": "tenant-a",
                "subject_id": "alice",
                "role": "editor",
            },
            "key-b": {
                "tenant_id": "tenant-b",
                "subject_id": "bob",
                "role": "editor",
            },
        },
    )
    return app, stores, tenant_a["storage_id"], tenant_b["storage_id"]


def _feedback(trace_id: str, chunk_id: str, correction: str) -> dict:
    return {
        "trace_id": trace_id,
        "feedback": "correction",
        "feedback_type": "bad_retrieval",
        "kb_id": "shared",
        "query": f"query-{trace_id}",
        "correction_text": correction,
        "save_as_knowledge": True,
        "created_by": "client-spoof",
        "citations": [{"chunk_id": chunk_id, "source": f"{chunk_id}.pdf"}],
    }


@pytest.mark.anyio
async def test_feedback_state_is_tenant_scoped_and_actors_are_server_derived(
    tmp_path, monkeypatch
):
    app, stores, storage_a, storage_b = _make_app(tmp_path, monkeypatch)

    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            created_a = await client.post(
                "/v1/feedback",
                json=_feedback("trace-a", "chunk-a", "correct-a"),
                headers=_headers("key-a"),
            )
            created_b = await client.post(
                "/v1/feedback",
                json=_feedback("trace-b", "chunk-b", "correct-b"),
                headers=_headers("key-b"),
            )
            assert created_a.status_code == created_b.status_code == 201

            listed_a = await client.get(
                "/v1/feedback",
                params={"kb_id": "shared"},
                headers=_headers("key-a"),
            )
            listed_b = await client.get(
                "/v1/feedback",
                params={"kb_id": "shared"},
                headers=_headers("key-b"),
            )
            analysis_a = await client.get(
                "/v1/feedback-analysis",
                params={"kb_id": "shared"},
                headers=_headers("key-a"),
            )
            foreign_analysis = await client.get(
                "/v1/feedback-analysis",
                params={
                    "kb_id": "shared",
                    "feedback_id": created_b.json()["feedback_id"],
                },
                headers=_headers("key-a"),
            )
            retrieval_a = await client.get(
                "/v1/retrieval-feedback",
                params={"kb_id": "shared"},
                headers=_headers("key-a"),
            )
            retrieval_b = await client.get(
                "/v1/retrieval-feedback",
                params={"kb_id": "shared"},
                headers=_headers("key-b"),
            )

            assert listed_a.status_code == listed_b.status_code == 200
            assert [row["trace_id"] for row in listed_a.json()["feedback"]] == [
                "trace-a"
            ]
            assert [row["trace_id"] for row in listed_b.json()["feedback"]] == [
                "trace-b"
            ]
            assert listed_a.json()["feedback"][0]["kb_id"] == "shared"
            assert listed_b.json()["feedback"][0]["kb_id"] == "shared"
            assert storage_a not in json.dumps(listed_a.json())
            assert storage_b not in json.dumps(listed_b.json())
            assert len(analysis_a.json()["feedback_analysis"]) == 1
            assert analysis_a.json()["feedback_analysis"][0]["kb_id"] == "shared"
            assert foreign_analysis.status_code == 404
            assert len(retrieval_a.json()["retrieval_feedback"]) == 1
            assert len(retrieval_b.json()["retrieval_feedback"]) == 1
            assert retrieval_a.json()["retrieval_feedback"][0]["kb_id"] == "shared"
            assert retrieval_b.json()["retrieval_feedback"][0]["kb_id"] == "shared"

            foreign_id = retrieval_b.json()["retrieval_feedback"][0][
                "retrieval_feedback_id"
            ]
            cross_disable = await client.post(
                f"/v1/retrieval-feedback/{foreign_id}/disable",
                json={"actor": "alice", "reason": "not mine"},
                headers=_headers("key-a"),
            )
            assert cross_disable.status_code == 404
            assert stores["retrieval_feedback_store"].list(kb_id=storage_b)[0][
                "enabled"
            ] is True

            own_disable = await client.post(
                f"/v1/retrieval-feedback/{foreign_id}/disable",
                json={"actor": "client-spoof", "reason": "reviewed"},
                headers=_headers("key-b"),
            )
            cross_enable = await client.post(
                f"/v1/retrieval-feedback/{foreign_id}/enable",
                headers=_headers("key-a"),
            )
            disabled = stores["retrieval_feedback_store"].list(kb_id=storage_b)[0]
            assert own_disable.status_code == 200
            assert cross_enable.status_code == 404
            assert disabled["enabled"] is False
            assert disabled["disabled_by"] == "bob"

            own_enable = await client.post(
                f"/v1/retrieval-feedback/{foreign_id}/enable",
                headers=_headers("key-b"),
            )
            assert own_enable.status_code == 200
            assert stores["retrieval_feedback_store"].list(kb_id=storage_b)[0][
                "enabled"
            ] is True

        persisted_feedback = stores["feedback_store"].export_records()
        by_trace = {row["trace_id"]: row for row in persisted_feedback}
        assert by_trace["trace-a"]["kb_id"] == storage_a
        assert by_trace["trace-a"]["created_by"] == "alice"
        assert by_trace["trace-b"]["kb_id"] == storage_b
        assert by_trace["trace-b"]["created_by"] == "bob"
        assert stores["knowledge_store"].list(kb_id=storage_a)[0]["created_by"] == (
            "alice"
        )
        assert stores["knowledge_store"].list(kb_id=storage_b)[0]["created_by"] == (
            "bob"
        )
