from __future__ import annotations

from httpx import ASGITransport, AsyncClient
import pytest

from cogdoc.api.app import create_app
from cogdoc.api.ingest import KnowledgeBaseRegistry
from cogdoc.api.tenant_scope import (
    PhysicalIdentityProjectionError,
    externalize_kb_fields,
)
from cogdoc.service.chat_service import ChatResult


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _result(trace_id: str, task_type: str) -> ChatResult:
    return ChatResult(
        answer="tenant-safe answer",
        task_type=task_type,
        citations=[],
        evidence=[],
        critique="",
        is_valid=True,
        trace_id=trace_id,
        request_id=trace_id,
        steps=[],
        chat_messages=[],
        raw_output={"answer": "tenant-safe answer"},
    )


@pytest.mark.anyio
async def test_chat_sessions_and_agent_calls_use_physical_tenant_scope(
    tmp_path, monkeypatch
):
    import cogdoc.api.app as app_module

    monkeypatch.setattr(app_module, "configure_logging", lambda: None)
    registry = KnowledgeBaseRegistry(
        registry_path=str(tmp_path / "registry.json"),
        source_dir_for=lambda storage_id: str(tmp_path / storage_id / "sources"),
    )
    tenant_a = registry.create("shared", "tenant-a", "alice")
    tenant_b = registry.create("shared", "tenant-b", "bob")
    runner_calls = []
    retrieve_calls = []

    def runner(doc_id, query, is_local, chat_history, forced_task):
        runner_calls.append((doc_id, forced_task, tuple(chat_history)))
        return _result(f"trace-{len(runner_calls)}", forced_task or "qa")

    def retrieve_runner(body):
        retrieve_calls.append(body.doc_id)
        return []

    app = create_app(
        kb_registry=registry,
        chat_runner=runner,
        api_principals={
            "key-a": {
                "tenant_id": "tenant-a",
                "subject_id": "alice",
                "role": "owner",
            },
            "key-b": {
                "tenant_id": "tenant-b",
                "subject_id": "bob",
                "role": "owner",
            },
        },
    )
    app.state.retrieve_runner = retrieve_runner
    headers_a = {"X-API-Key": "key-a"}
    headers_b = {"X-API-Key": "key-b"}

    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            chat_a = await client.post(
                "/v1/chat",
                json={"query": "A", "doc_id": "shared", "session_id": "same"},
                headers=headers_a,
            )
            chat_b = await client.post(
                "/v1/chat",
                json={"query": "B", "doc_id": "shared", "session_id": "same"},
                headers=headers_b,
            )
            summary_a = await client.post(
                "/v1/summary",
                json={"query": "summary", "doc_id": "shared", "session_id": "s2"},
                headers=headers_a,
            )
            retrieve_b = await client.post(
                "/v1/retrieve",
                json={"query": "find", "doc_id": "shared"},
                headers=headers_b,
            )
            sessions_a = await client.get(
                "/v1/sessions", params={"doc_id": "shared"}, headers=headers_a
            )
            sessions_b = await client.get(
                "/v1/sessions", params={"doc_id": "shared"}, headers=headers_b
            )
            physical_probe = await client.post(
                "/v1/chat",
                json={"query": "probe", "doc_id": tenant_a["storage_id"]},
                headers=headers_b,
            )

    assert chat_a.status_code == chat_b.status_code == 200
    assert summary_a.status_code == retrieve_b.status_code == 200
    assert chat_a.json()["doc_id"] == chat_b.json()["doc_id"] == "shared"
    assert summary_a.json()["doc_id"] == retrieve_b.json()["doc_id"] == "shared"
    assert [call[0] for call in runner_calls] == [
        tenant_a["storage_id"],
        tenant_b["storage_id"],
        tenant_a["storage_id"],
    ]
    assert retrieve_calls == [tenant_b["storage_id"]]
    assert {row["session_id"] for row in sessions_a.json()["sessions"]} == {
        "same",
        "s2",
    }
    assert {row["session_id"] for row in sessions_b.json()["sessions"]} == {"same"}
    assert physical_probe.status_code == 404


def test_unresolved_physical_id_projection_fails_closed(tmp_path):
    registry = KnowledgeBaseRegistry(
        registry_path=str(tmp_path / "registry.json"),
        source_dir_for=lambda storage_id: str(tmp_path / storage_id / "sources"),
    )
    app = create_app(kb_registry=registry)
    from starlette.requests import Request

    request = Request({"type": "http", "app": app, "headers": []})
    with pytest.raises(PhysicalIdentityProjectionError):
        externalize_kb_fields({"doc_id": "t-" + "a" * 64}, request)
