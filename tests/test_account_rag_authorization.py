from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
import json
from typing import Any

from httpx import ASGITransport, AsyncClient
import pytest

from cogdoc.api.access_control import TokenBucketRateLimiter
from cogdoc.api.app import create_app
from cogdoc.api.auth_store import AuthStore
from cogdoc.api.derived_knowledge_store import DerivedKnowledgeStore
from cogdoc.api.feedback_analysis_store import FeedbackAnalysisStore
from cogdoc.api.feedback_store import FeedbackStore
from cogdoc.api.ingest import IndexJobManager, KnowledgeBaseRegistry
from cogdoc.api.resource_access import ResourceAccessStore
from cogdoc.api.research_job_store import ResearchJobStore
from cogdoc.api.retrieval_eval_draft_store import RetrievalEvalDraftStore
from cogdoc.api.retrieval_feedback_store import RetrievalFeedbackStore
from cogdoc.api.schemas import Document
from cogdoc.api.session_store import SessionStore
from cogdoc.service.chat_service import ChatResult
from cogdoc.state_runtime import StateRuntime
from cogdoc.tools.chunk_identity import build_document_id
from cogdoc.tools.retriever.scope import RetrievalAccessMode


PASSWORD = "correct horse battery staple"
ALLOWED_SOURCE = "allowed.pdf"
SECRET_SOURCE = "secret.pdf"


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _hit(source: str, text: str) -> dict[str, Any]:
    return {
        "text": text,
        "meta": {
            "chunk_id": f"chunk-{source}",
            "document_id": build_document_id(source),
            "chunk_index": 0,
            "source": source,
            "page": 1,
            "page_start": 1,
            "page_end": 1,
        },
        "retrieval": {"score": 0.99},
    }


@dataclass
class _Harness:
    client: AsyncClient
    app: Any
    registry: KnowledgeBaseRegistry
    acl_store: ResourceAccessStore
    workspace_id: str
    alice: dict[str, Any]
    bob: dict[str, Any]
    admin: dict[str, Any]
    carol: dict[str, Any]
    alice_storage_id: str
    carol_storage_id: str
    managed_policy: dict[str, Any]
    retrieve_calls: list[dict[str, Any]]
    chat_calls: list[dict[str, Any]]
    source_chunk_calls: list[tuple[str, str]]


async def _register(
    client: AsyncClient,
    *,
    email: str,
    display_name: str,
    workspace_name: str,
) -> dict[str, Any]:
    response = await client.post(
        "/v1/auth/register",
        json={
            "email": email,
            "password": PASSWORD,
            "display_name": display_name,
            "workspace_name": workspace_name,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _invite_new_user(
    client: AsyncClient,
    *,
    owner_token: str,
    workspace_id: str,
    email: str,
    display_name: str,
    role: str,
) -> dict[str, Any]:
    invitation = await client.post(
        f"/v1/workspaces/{workspace_id}/invites",
        headers=_headers(owner_token),
        json={"email": email, "role": role},
    )
    assert invitation.status_code == 201, invitation.text
    accepted = await client.post(
        "/v1/auth/invitations/accept",
        json={
            "token": invitation.json()["invite_token"],
            "email": email,
            "password": PASSWORD,
            "display_name": display_name,
        },
    )
    assert accepted.status_code == 200, accepted.text
    return accepted.json()


def _runtime(tmp_path) -> StateRuntime:
    return StateRuntime(
        feedback_store=FeedbackStore(
            feedback_path=str(tmp_path / "feedback.jsonl"),
            bad_cases_path=str(tmp_path / "bad-cases.jsonl"),
        ),
        feedback_analysis_store=FeedbackAnalysisStore(
            str(tmp_path / "feedback-analysis.jsonl")
        ),
        knowledge_store=DerivedKnowledgeStore(str(tmp_path / "knowledge.jsonl")),
        retrieval_feedback_store=RetrievalFeedbackStore(
            str(tmp_path / "retrieval-feedback.jsonl")
        ),
        retrieval_eval_draft_store=RetrievalEvalDraftStore(
            str(tmp_path / "retrieval-eval-drafts.jsonl")
        ),
        research_job_store=ResearchJobStore(str(tmp_path / "research-jobs.json")),
        derived_knowledge_index_persist_directory=str(tmp_path / "derived-index"),
        derived_knowledge_index_state_directory=str(tmp_path / "derived-state"),
    )


@asynccontextmanager
async def _provisioned_account_rag(tmp_path, monkeypatch):
    import cogdoc.api.app as app_module
    import cogdoc.api.routes.documents as documents_module

    monkeypatch.setattr(app_module, "configure_logging", lambda: None)
    source_root = tmp_path / "knowledge-bases"

    def source_dir_for(storage_id: str) -> str:
        return str(source_root / storage_id / "sources")

    registry = KnowledgeBaseRegistry(
        registry_path=str(tmp_path / "registry.json"),
        source_dir_for=source_dir_for,
    )
    jobs = IndexJobManager(
        ingest_fn=lambda _kb_id, _source_dir: None,
        source_dir_for=source_dir_for,
        kb_exists=registry.exists,
    )
    auth_store = AuthStore(str(tmp_path / "accounts.db"), scrypt_n=1 << 10)
    acl_store = ResourceAccessStore(
        tmp_path / "resource-access.db", legacy_workspace_default=False
    )
    runtime = _runtime(tmp_path)
    retrieve_calls: list[dict[str, Any]] = []
    chat_calls: list[dict[str, Any]] = []
    source_chunk_calls: list[tuple[str, str]] = []
    documents_by_storage_id: dict[str, list[Document]] = {}

    def chat_runner(
        doc_id,
        query,
        is_local,
        chat_history,
        forced_task,
        *,
        session_id=None,
        retrieval_scope=None,
    ):
        chat_calls.append(
            {
                "doc_id": doc_id,
                "query": query,
                "history": list(chat_history),
                "session_id": session_id,
                "retrieval_scope": retrieval_scope,
            }
        )
        answer = f"answer for {query}"
        return ChatResult(
            answer=answer,
            task_type=forced_task or "qa",
            citations=[],
            evidence=[],
            critique="",
            is_valid=True,
            trace_id=f"trace-{len(chat_calls)}",
            request_id=f"trace-{len(chat_calls)}",
            steps=[],
            chat_messages=[
                {"role": "user", "content": query, "timestamp": None},
                {"role": "assistant", "content": answer, "timestamp": None},
            ],
            raw_output={"answer": answer},
        )

    def malicious_retrieve_runner(body, *, retrieval_scope=None):
        retrieve_calls.append(
            {
                "doc_id": body.doc_id,
                "retrieval_scope": retrieval_scope,
            }
        )
        # Deliberately disregard the supplied scope. The HTTP boundary must
        # still reject the forbidden result before it reaches the response.
        return [
            _hit(ALLOWED_SOURCE, "allowed evidence"),
            _hit(SECRET_SOURCE, "forbidden evidence must never escape"),
        ]

    def source_chunks_reader(storage_id: str, source: str):
        source_chunk_calls.append((storage_id, source))
        return [_hit(source, f"chunk text for {source}")]

    app = create_app(
        chat_runner=chat_runner,
        session_store=SessionStore(),
        kb_registry=registry,
        index_jobs=jobs,
        state_runtime=runtime,
        close_state_runtime_on_shutdown=False,
        auth_store=auth_store,
        resource_access_store=acl_store,
        self_registration_enabled=True,
        rate_limiter=TokenBucketRateLimiter(capacity=0, refill_per_second=0.0),
    )
    app.state.retrieve_runner = malicious_retrieve_runner
    app.state.source_chunks_reader = source_chunks_reader
    app.state.source_list_reader = lambda storage_id: [
        document.name for document in documents_by_storage_id.get(storage_id, [])
    ]
    monkeypatch.setattr(
        documents_module,
        "_kb_documents",
        lambda storage_id: list(documents_by_storage_id.get(storage_id, [])),
    )

    try:
        async with app.router.lifespan_context(app):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://testserver"
            ) as client:
                alice = await _register(
                    client,
                    email="alice@example.com",
                    display_name="Alice",
                    workspace_name="Acme",
                )
                carol = await _register(
                    client,
                    email="carol@example.com",
                    display_name="Carol",
                    workspace_name="Carol Personal",
                )
                workspace_id = alice["workspace"]["workspace_id"]
                alice_token = alice["access_token"]
                bob = await _invite_new_user(
                    client,
                    owner_token=alice_token,
                    workspace_id=workspace_id,
                    email="bob@example.com",
                    display_name="Bob",
                    role="viewer",
                )
                admin = await _invite_new_user(
                    client,
                    owner_token=alice_token,
                    workspace_id=workspace_id,
                    email="admin@example.com",
                    display_name="Workspace Admin",
                    role="admin",
                )

                alice_kb = await client.post(
                    "/v1/knowledge-bases",
                    headers=_headers(alice_token),
                    json={"kb_id": "shared"},
                )
                carol_kb = await client.post(
                    "/v1/knowledge-bases",
                    headers=_headers(carol["access_token"]),
                    json={"kb_id": "shared"},
                )
                assert alice_kb.status_code == carol_kb.status_code == 201
                alice_record = registry.resolve("shared", workspace_id)
                carol_workspace_id = carol["workspace"]["workspace_id"]
                carol_record = registry.resolve("shared", carol_workspace_id)
                assert alice_record is not None and carol_record is not None
                alice_storage_id = str(alice_record["storage_id"])
                carol_storage_id = str(carol_record["storage_id"])
                documents_by_storage_id[alice_storage_id] = [
                    Document(
                        name=ALLOWED_SOURCE,
                        sha256="allowed-sha",
                        document_id=build_document_id(ALLOWED_SOURCE),
                    ),
                    Document(
                        name=SECRET_SOURCE,
                        sha256="secret-sha",
                        document_id=build_document_id(SECRET_SOURCE),
                    ),
                ]
                documents_by_storage_id[carol_storage_id] = [
                    Document(
                        name="carol.pdf",
                        sha256="carol-sha",
                        document_id=build_document_id("carol.pdf"),
                    )
                ]

                # An administrator, not only the creator, can manage the KB ACL.
                managed = await client.patch(
                    "/v1/knowledge-bases/shared/access",
                    headers=_headers(admin["access_token"]),
                    json={"policy": "private"},
                )
                assert managed.status_code == 200, managed.text
                for source in (ALLOWED_SOURCE, SECRET_SOURCE):
                    configured = await client.patch(
                        "/v1/knowledge-bases/shared/documents/"
                        f"{build_document_id(source)}/access",
                        headers=_headers(admin["access_token"]),
                        json={"policy": "private", "source": source},
                    )
                    assert configured.status_code == 200, configured.text

                yield _Harness(
                    client=client,
                    app=app,
                    registry=registry,
                    acl_store=acl_store,
                    workspace_id=workspace_id,
                    alice=alice,
                    bob=bob,
                    admin=admin,
                    carol=carol,
                    alice_storage_id=alice_storage_id,
                    carol_storage_id=carol_storage_id,
                    managed_policy=managed.json(),
                    retrieve_calls=retrieve_calls,
                    chat_calls=chat_calls,
                    source_chunk_calls=source_chunk_calls,
                )
    finally:
        auth_store.close()
        acl_store.close()
        runtime.close()


async def _grant_bob_allowed(harness: _Harness) -> None:
    response = await harness.client.post(
        "/v1/knowledge-bases/shared/documents/"
        f"{build_document_id(ALLOWED_SOURCE)}/access/grants",
        headers=_headers(harness.alice["access_token"]),
        json={
            "subject_id": harness.bob["user"]["user_id"],
            "role": "viewer",
        },
    )
    assert response.status_code == 200, response.text


async def _revoke_bob_allowed(harness: _Harness) -> None:
    response = await harness.client.delete(
        "/v1/knowledge-bases/shared/documents/"
        f"{build_document_id(ALLOWED_SOURCE)}/access/grants/"
        f"{harness.bob['user']['user_id']}",
        headers=_headers(harness.alice["access_token"]),
    )
    assert response.status_code == 204, response.text


@pytest.mark.anyio
async def test_real_bearer_principal_role_matrix_and_same_slug_tenant_isolation(
    tmp_path, monkeypatch
):
    async with _provisioned_account_rag(tmp_path, monkeypatch) as harness:
        client = harness.client
        alice_headers = _headers(harness.alice["access_token"])
        bob_headers = _headers(harness.bob["access_token"])
        admin_headers = _headers(harness.admin["access_token"])
        carol_headers = _headers(harness.carol["access_token"])

        alice_tenant = await client.get("/v1/tenant", headers=alice_headers)
        bob_tenant = await client.get("/v1/tenant", headers=bob_headers)
        admin_tenant = await client.get("/v1/tenant", headers=admin_headers)
        carol_tenant = await client.get("/v1/tenant", headers=carol_headers)

        assert alice_tenant.json()["tenant_id"] == harness.workspace_id
        assert alice_tenant.json()["subject_id"] == harness.alice["user"]["user_id"]
        assert alice_tenant.json()["role"] == "owner"
        assert bob_tenant.json()["tenant_id"] == harness.workspace_id
        assert bob_tenant.json()["subject_id"] == harness.bob["user"]["user_id"]
        assert bob_tenant.json()["role"] == "viewer"
        assert admin_tenant.json()["tenant_id"] == harness.workspace_id
        assert admin_tenant.json()["role"] == "admin"
        assert carol_tenant.json()["tenant_id"] != harness.workspace_id
        assert harness.managed_policy["policy"] == "private"

        # A real viewer session is stopped at the centralized RBAC boundary.
        viewer_write = await client.post(
            "/v1/knowledge-bases",
            headers=bob_headers,
            json={"kb_id": "viewer-must-not-write"},
        )
        viewer_manage = await client.patch(
            "/v1/knowledge-bases/shared/access",
            headers=bob_headers,
            json={"policy": "workspace"},
        )
        assert viewer_write.status_code == 403
        assert viewer_manage.status_code == 403

        # Alice and Carol own different physical KBs with the same public slug.
        assert harness.alice_storage_id != harness.carol_storage_id
        alice_list = await client.get("/v1/knowledge-bases", headers=alice_headers)
        carol_list = await client.get("/v1/knowledge-bases", headers=carol_headers)
        admin_list = await client.get("/v1/knowledge-bases", headers=admin_headers)
        bob_list = await client.get("/v1/knowledge-bases", headers=bob_headers)
        assert [(row["kb_id"], row["tenant_id"]) for row in alice_list.json()] == [
            ("shared", harness.workspace_id)
        ]
        assert [(row["kb_id"], row["tenant_id"]) for row in carol_list.json()] == [
            ("shared", harness.carol["workspace"]["workspace_id"])
        ]
        assert admin_list.json()[0]["document_count"] == 2
        assert bob_list.json() == []

        physical_probe = await client.get(
            f"/v1/knowledge-bases/{harness.alice_storage_id}",
            headers=carol_headers,
        )
        assert physical_probe.status_code == 404
        assert harness.alice_storage_id not in physical_probe.text

        # Middleware validates the opaque session against durable revocation on
        # every protected request; a token does not remain an in-memory principal.
        logged_out = await client.post("/v1/auth/logout", headers=bob_headers)
        after_logout = await client.get("/v1/tenant", headers=bob_headers)
        assert logged_out.status_code == 204
        assert after_logout.status_code == 401


@pytest.mark.anyio
async def test_workspace_header_isolates_two_tabs_sharing_one_session_and_slug(
    tmp_path, monkeypatch
):
    async with _provisioned_account_rag(tmp_path, monkeypatch) as harness:
        client = harness.client
        token = harness.alice["access_token"]
        workspace_a = harness.workspace_id
        tab_a = {
            **_headers(token),
            "X-CogDoc-Workspace": workspace_a,
        }
        created_workspace = await client.post(
            "/v1/workspaces",
            headers=tab_a,
            json={"name": "Second Browser Tab"},
        )
        assert created_workspace.status_code == 201, created_workspace.text
        workspace_b = created_workspace.json()["workspace"]["workspace_id"]
        tab_b = {
            **_headers(token),
            "X-CogDoc-Workspace": workspace_b,
        }

        created_same_slug = await client.post(
            "/v1/knowledge-bases",
            headers=tab_b,
            json={"kb_id": "shared", "access_policy": "workspace"},
        )
        assert created_same_slug.status_code == 201, created_same_slug.text

        # Interleave the tabs using the same raw Bearer. Each explicit selector
        # must resolve the public slug to its own physical tenant regardless of
        # which request most recently changed the legacy active-workspace field.
        b_first = await client.get("/v1/knowledge-bases", headers=tab_b)
        a_after_b = await client.get("/v1/knowledge-bases", headers=tab_a)
        b_after_a = await client.get("/v1/knowledge-bases", headers=tab_b)
        assert [row["tenant_id"] for row in b_first.json()] == [workspace_b]
        assert [row["tenant_id"] for row in a_after_b.json()] == [workspace_a]
        assert [row["tenant_id"] for row in b_after_a.json()] == [workspace_b]
        assert a_after_b.json()[0]["kb_id"] == b_after_a.json()[0]["kb_id"] == "shared"
        assert (
            harness.registry.resolve("shared", workspace_a)["storage_id"]
            != harness.registry.resolve("shared", workspace_b)["storage_id"]
        )

        conflict = await client.get(
            f"/v1/workspaces/{workspace_a}",
            headers=tab_b,
        )
        assert conflict.status_code == 404
        assert conflict.json()["error_code"] == "WORKSPACE_NOT_FOUND"


@pytest.mark.anyio
async def test_private_document_acl_filters_every_rag_read_and_malicious_runner(
    tmp_path, monkeypatch
):
    async with _provisioned_account_rag(tmp_path, monkeypatch) as harness:
        client = harness.client
        bob_headers = _headers(harness.bob["access_token"])
        alice_headers = _headers(harness.alice["access_token"])

        # A private KB is completely opaque before a document-level grant.
        hidden_list = await client.get("/v1/knowledge-bases", headers=bob_headers)
        hidden_documents = await client.get(
            "/v1/knowledge-bases/shared/documents", headers=bob_headers
        )
        hidden_chunks = await client.get(
            f"/v1/knowledge-bases/shared/sources/{SECRET_SOURCE}/chunks",
            headers=bob_headers,
        )
        hidden_retrieve = await client.post(
            "/v1/retrieve",
            headers=bob_headers,
            json={"doc_id": "shared", "query": "private question"},
        )
        hidden_chat = await client.post(
            "/v1/chat",
            headers=bob_headers,
            json={"doc_id": "shared", "query": "private question"},
        )
        assert hidden_list.json() == []
        assert hidden_documents.status_code == 404
        assert hidden_chunks.status_code == 404
        assert hidden_retrieve.status_code == 404
        assert hidden_chat.status_code == 404
        assert harness.retrieve_calls == []
        assert harness.chat_calls == []
        assert harness.source_chunk_calls == []

        # A caller-supplied internal ID is never mirrored into an opaque 404,
        # including the two primary RAG endpoints.
        for response in (
            await client.post(
                "/v1/retrieve",
                headers=bob_headers,
                json={"doc_id": harness.carol_storage_id, "query": "probe"},
            ),
            await client.post(
                "/v1/chat",
                headers=bob_headers,
                json={"doc_id": harness.carol_storage_id, "query": "probe"},
            ),
        ):
            assert response.status_code == 404
            assert harness.carol_storage_id not in response.text

        await _grant_bob_allowed(harness)

        visible_list = await client.get("/v1/knowledge-bases", headers=bob_headers)
        visible_documents = await client.get(
            "/v1/knowledge-bases/shared/documents", headers=bob_headers
        )
        visible_sources = await client.get(
            "/v1/knowledge-bases/shared/sources", headers=bob_headers
        )
        allowed_chunks = await client.get(
            f"/v1/knowledge-bases/shared/sources/{ALLOWED_SOURCE}/chunks",
            headers=bob_headers,
        )
        forbidden_chunks = await client.get(
            f"/v1/knowledge-bases/shared/sources/{SECRET_SOURCE}/chunks",
            headers=bob_headers,
        )
        retrieved = await client.post(
            "/v1/retrieve",
            headers=bob_headers,
            json={"doc_id": "shared", "query": "allowed question", "top_k": 8},
        )

        assert visible_list.json()[0]["document_count"] == 1
        assert [document["name"] for document in visible_documents.json()] == [
            ALLOWED_SOURCE
        ]
        assert visible_sources.json()["sources"] == [ALLOWED_SOURCE]
        assert allowed_chunks.status_code == 200
        assert allowed_chunks.json()["chunks"][0]["source"] == ALLOWED_SOURCE
        assert forbidden_chunks.status_code == 404
        assert harness.source_chunk_calls == [
            (harness.alice_storage_id, ALLOWED_SOURCE)
        ]
        assert retrieved.status_code == 200, retrieved.text
        assert [hit["source"] for hit in retrieved.json()["hits"]] == [ALLOWED_SOURCE]
        assert SECRET_SOURCE not in json.dumps(retrieved.json())
        bob_scope = harness.retrieve_calls[-1]["retrieval_scope"]
        assert harness.retrieve_calls[-1]["doc_id"] == harness.alice_storage_id
        assert bob_scope.access_mode is RetrievalAccessMode.SUBSET
        assert bob_scope.allowed_sources == (ALLOWED_SOURCE,)

        # Tenant owner bypass is explicit, while still using the same final guard.
        alice_retrieved = await client.post(
            "/v1/retrieve",
            headers=alice_headers,
            json={"doc_id": "shared", "query": "owner question"},
        )
        assert [hit["source"] for hit in alice_retrieved.json()["hits"]] == [
            ALLOWED_SOURCE,
            SECRET_SOURCE,
        ]
        assert (
            harness.retrieve_calls[-1]["retrieval_scope"].access_mode
            is RetrievalAccessMode.ALL
        )

        # Revocation changes the next authorization snapshot immediately.
        calls_before_revoke = len(harness.retrieve_calls)
        await _revoke_bob_allowed(harness)
        revoked_list = await client.get("/v1/knowledge-bases", headers=bob_headers)
        revoked_retrieve = await client.post(
            "/v1/retrieve",
            headers=bob_headers,
            json={"doc_id": "shared", "query": "must stay hidden"},
        )
        assert revoked_list.json() == []
        assert revoked_retrieve.status_code == 404
        assert len(harness.retrieve_calls) == calls_before_revoke


@pytest.mark.anyio
async def test_same_session_id_chat_history_is_user_scoped_and_revocation_hides_it(
    tmp_path, monkeypatch
):
    async with _provisioned_account_rag(tmp_path, monkeypatch) as harness:
        client = harness.client
        alice_headers = _headers(harness.alice["access_token"])
        bob_headers = _headers(harness.bob["access_token"])
        await _grant_bob_allowed(harness)

        alice_chat = await client.post(
            "/v1/chat",
            headers=alice_headers,
            json={
                "doc_id": "shared",
                "session_id": "same-public-id",
                "query": "alice-only question",
            },
        )
        bob_chat = await client.post(
            "/v1/chat",
            headers=bob_headers,
            json={
                "doc_id": "shared",
                "session_id": "same-public-id",
                "query": "bob-only question",
            },
        )
        assert alice_chat.status_code == bob_chat.status_code == 200

        alice_history = await client.get(
            "/v1/sessions/same-public-id/history",
            params={"doc_id": "shared"},
            headers=alice_headers,
        )
        bob_history = await client.get(
            "/v1/sessions/same-public-id/history",
            params={"doc_id": "shared"},
            headers=bob_headers,
        )
        alice_sessions = await client.get(
            "/v1/sessions", params={"doc_id": "shared"}, headers=alice_headers
        )
        bob_sessions = await client.get(
            "/v1/sessions", params={"doc_id": "shared"}, headers=bob_headers
        )

        alice_serialized = json.dumps(alice_history.json())
        bob_serialized = json.dumps(bob_history.json())
        assert "alice-only question" in alice_serialized
        assert "bob-only question" not in alice_serialized
        assert "bob-only question" in bob_serialized
        assert "alice-only question" not in bob_serialized
        assert [row["session_id"] for row in alice_sessions.json()["sessions"]] == [
            "same-public-id"
        ]
        assert [row["session_id"] for row in bob_sessions.json()["sessions"]] == [
            "same-public-id"
        ]

        alice_call = next(
            call
            for call in harness.chat_calls
            if call["query"] == "alice-only question"
        )
        bob_call = next(
            call for call in harness.chat_calls if call["query"] == "bob-only question"
        )
        assert alice_call["history"] == bob_call["history"] == []
        assert alice_call["doc_id"] == bob_call["doc_id"] == harness.alice_storage_id
        assert alice_call["session_id"] != bob_call["session_id"]
        assert alice_call["session_id"].endswith(":same-public-id")
        assert bob_call["session_id"].endswith(":same-public-id")
        assert alice_call["retrieval_scope"].access_mode is RetrievalAccessMode.ALL
        assert bob_call["retrieval_scope"].access_mode is RetrievalAccessMode.SUBSET
        assert bob_call["retrieval_scope"].allowed_sources == (ALLOWED_SOURCE,)

        await _revoke_bob_allowed(harness)
        revoked_history = await client.get(
            "/v1/sessions/same-public-id/history",
            params={"doc_id": "shared"},
            headers=bob_headers,
        )
        revoked_sessions = await client.get(
            "/v1/sessions", params={"doc_id": "shared"}, headers=bob_headers
        )
        alice_history_after = await client.get(
            "/v1/sessions/same-public-id/history",
            params={"doc_id": "shared"},
            headers=alice_headers,
        )
        assert revoked_history.json()["messages"] == []
        assert revoked_sessions.json()["sessions"] == []
        assert "alice-only question" in json.dumps(alice_history_after.json())


@pytest.mark.anyio
async def test_research_job_detail_and_action_disappear_after_source_revocation(
    tmp_path, monkeypatch
):
    async with _provisioned_account_rag(tmp_path, monkeypatch) as harness:
        client = harness.client
        alice_headers = _headers(harness.alice["access_token"])
        bob_headers = _headers(harness.bob["access_token"])
        bob_user_id = harness.bob["user"]["user_id"]

        # Research creation is a durable write, so use an editor membership and
        # an editor-capped document grant. The resulting job freezes exactly the
        # one source this principal can query at creation time.
        promoted = await client.patch(
            f"/v1/workspaces/{harness.workspace_id}/members/{bob_user_id}",
            headers=alice_headers,
            json={"role": "editor"},
        )
        granted = await client.post(
            "/v1/knowledge-bases/shared/documents/"
            f"{build_document_id(ALLOWED_SOURCE)}/access/grants",
            headers=alice_headers,
            json={"subject_id": bob_user_id, "role": "editor"},
        )
        assert promoted.status_code == granted.status_code == 200

        created = await client.post(
            "/v1/research-jobs",
            headers=bob_headers,
            json={
                "kb_id": "shared",
                "objective": "Build a report using only the authorized source",
            },
        )
        assert created.status_code == 201, created.text
        job_id = created.json()["job"]["job_id"]
        assert created.json()["job"]["kb_id"] == "shared"
        fetched = await client.get(f"/v1/research-jobs/{job_id}", headers=bob_headers)
        assert fetched.status_code == 200

        await _revoke_bob_allowed(harness)

        hidden_detail = await client.get(
            f"/v1/research-jobs/{job_id}", headers=bob_headers
        )
        hidden_action = await client.post(
            f"/v1/research-jobs/{job_id}/cancel", headers=bob_headers
        )
        hidden_list = await client.get("/v1/research-jobs", headers=bob_headers)
        assert hidden_detail.status_code == hidden_action.status_code == 404
        assert hidden_detail.json()["error_code"] == "RESEARCH_JOB_NOT_FOUND"
        assert hidden_action.json()["error_code"] == "RESEARCH_JOB_NOT_FOUND"
        assert hidden_list.json()["jobs"] == []
