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
from cogdoc.tools.eval.retrieval_eval_drafts import create_pending_draft


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _headers(key: str) -> dict[str, str]:
    return {"X-API-Key": key}


def _snapshot() -> dict:
    return {
        "index_generation": "gen-1",
        "index_build_version": "build-v1",
        "chunk_identity_version": "chunk-v1",
        "source_versions": [{"source": "a.pdf", "sha256": "sha-a"}],
    }


def _pending(storage_id: str, *, label: str):
    return create_pending_draft(
        kb_id=storage_id,
        query=f"question-{label}",
        units=[
            {
                "unit_id": "r1",
                "task_kind": "qa_requirement",
                "label": f"requirement-{label}",
                "retrieval_query": f"retrieve-{label}",
                "recovery_query": f"recover-{label}",
            }
        ],
        index_generation="gen-1",
        index_build_version="build-v1",
        chunk_identity_version="chunk-v1",
        source_versions=[{"source": "a.pdf", "sha256": "sha-a"}],
        origin_trace_id=f"trace-{label}",
        now="2026-08-12T00:00:00+00:00",
    )


def _gold_annotations() -> dict:
    return {
        "units": [
            {
                "unit_id": "r1",
                "acceptable_evidence": [
                    {
                        "chunk_id": "c1",
                        "source": "a.pdf",
                        "source_sha256": "sha-a",
                        "parent_chunk_id": "p1",
                        "start": 3,
                        "end": 12,
                    }
                ],
            }
        ]
    }


def _make_app(tmp_path, monkeypatch):
    import cogdoc.api.app as app_module
    import cogdoc.api.routes.retrieval_eval_drafts as route

    monkeypatch.setattr(app_module, "configure_logging", lambda: None)
    monkeypatch.setattr(route, "current_index_provenance", lambda _kb_id: _snapshot())
    registry = KnowledgeBaseRegistry(
        registry_path=str(tmp_path / "registry.json"),
        source_dir_for=lambda storage_id: str(
            tmp_path / "knowledge-bases" / storage_id / "sources"
        ),
    )
    tenant_a = registry.create("shared", "tenant-a", "alice")
    tenant_b = registry.create("shared", "tenant-b", "bob")
    draft_store = RetrievalEvalDraftStore(
        str(tmp_path / "retrieval-eval-drafts.jsonl")
    )
    draft_a = draft_store.ensure(_pending(tenant_a["storage_id"], label="a"))
    draft_b = draft_store.ensure(_pending(tenant_b["storage_id"], label="b"))
    app = create_app(
        kb_registry=registry,
        feedback_store=FeedbackStore(
            str(tmp_path / "feedback.jsonl"), str(tmp_path / "bad-cases.jsonl")
        ),
        feedback_analysis_store=FeedbackAnalysisStore(
            str(tmp_path / "feedback-analysis.jsonl")
        ),
        knowledge_store=DerivedKnowledgeStore(str(tmp_path / "knowledge.jsonl")),
        retrieval_feedback_store=RetrievalFeedbackStore(
            str(tmp_path / "retrieval-feedback.jsonl")
        ),
        retrieval_eval_draft_store=draft_store,
        api_principals={
            "review-a": {
                "tenant_id": "tenant-a",
                "subject_id": "alice",
                "role": "reviewer",
            },
            "review-b": {
                "tenant_id": "tenant-b",
                "subject_id": "bob",
                "role": "reviewer",
            },
        },
        eval_review_api_keys={"review-a", "review-b"},
    )
    return app, draft_store, draft_a, draft_b, tenant_a, tenant_b


@pytest.mark.anyio
async def test_retrieval_eval_list_review_and_export_are_tenant_scoped(
    tmp_path, monkeypatch
):
    app, store, draft_a, draft_b, tenant_a, tenant_b = _make_app(
        tmp_path, monkeypatch
    )

    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            list_a = await client.get(
                "/v1/retrieval-eval-drafts", headers=_headers("review-a")
            )
            list_b = await client.get(
                "/v1/retrieval-eval-drafts",
                params={"kb_id": "shared"},
                headers=_headers("review-b"),
            )
            assert [row["draft_id"] for row in list_a.json()["drafts"]] == [
                draft_a["draft_id"]
            ]
            assert [row["draft_id"] for row in list_b.json()["drafts"]] == [
                draft_b["draft_id"]
            ]
            assert list_a.json()["drafts"][0]["kb_id"] == "shared"
            assert list_b.json()["drafts"][0]["kb_id"] == "shared"
            assert tenant_a["storage_id"] not in json.dumps(list_a.json())
            assert tenant_b["storage_id"] not in json.dumps(list_b.json())

            foreign_get = await client.get(
                f"/v1/retrieval-eval-drafts/{draft_b['draft_id']}",
                headers=_headers("review-a"),
            )
            foreign_review = await client.post(
                f"/v1/retrieval-eval-drafts/{draft_b['draft_id']}/review",
                json={
                    "decision": "rejected",
                    "expected_revision": 1,
                    "reason": "not mine",
                },
                headers=_headers("review-a"),
            )
            assert foreign_get.status_code == foreign_review.status_code == 404
            assert store.get(draft_b["draft_id"])["status"] == "pending"

            approved_a = await client.post(
                f"/v1/retrieval-eval-drafts/{draft_a['draft_id']}/review",
                json={
                    "decision": "approved",
                    "expected_revision": 1,
                    "annotations": _gold_annotations(),
                },
                headers=_headers("review-a"),
            )
            approved_b = await client.post(
                f"/v1/retrieval-eval-drafts/{draft_b['draft_id']}/review",
                json={
                    "decision": "approved",
                    "expected_revision": 1,
                    "annotations": _gold_annotations(),
                },
                headers=_headers("review-b"),
            )
            assert approved_a.status_code == approved_b.status_code == 200
            assert approved_a.json()["draft"]["reviewed_by"] == "alice"
            assert approved_b.json()["draft"]["reviewed_by"] == "bob"
            assert approved_a.json()["draft"]["kb_id"] == "shared"
            assert approved_b.json()["draft"]["kb_id"] == "shared"

            generic_a = await client.get(
                "/v1/retrieval-eval-drafts/export",
                params={"format": "generic_v1"},
                headers=_headers("review-a"),
            )
            generic_b = await client.get(
                "/v1/retrieval-eval-drafts/export",
                params={"format": "generic_v1"},
                headers=_headers("review-b"),
            )
            retrieval_a = await client.get(
                "/v1/retrieval-eval-drafts/export",
                params={"format": "retrieval_eval_v1"},
                headers=_headers("review-a"),
            )

            assert generic_a.json()["exported_count"] == 1
            assert generic_b.json()["exported_count"] == 1
            assert generic_a.json()["items"][0]["draft_id"] == draft_a["draft_id"]
            assert generic_b.json()["items"][0]["draft_id"] == draft_b["draft_id"]
            assert generic_a.json()["items"][0]["kb_id"] == "shared"
            assert generic_b.json()["items"][0]["kb_id"] == "shared"
            assert retrieval_a.json()["exported_count"] == 1
            assert retrieval_a.json()["items"][0]["id"] == draft_a["draft_id"]
            assert retrieval_a.json()["items"][0]["doc_id"] == "shared"
            assert tenant_a["storage_id"] not in json.dumps(generic_a.json())
            assert tenant_b["storage_id"] not in json.dumps(generic_b.json())

        assert store.get(draft_a["draft_id"])["reviewed_by"] == "alice"
        assert store.get(draft_b["draft_id"])["reviewed_by"] == "bob"
