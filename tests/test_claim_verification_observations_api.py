import pytest
from httpx import ASGITransport, AsyncClient

from cogdoc.api.app import create_app
from cogdoc.api.claim_verification_store import ClaimVerificationObservationStore
from cogdoc.api.ingest import KnowledgeBaseRegistry
from cogdoc.api.session_store import SessionStore
from cogdoc.config.settings import Settings
from cogdoc.service.chat_service import ChatEvent, ChatResult
from cogdoc.service.claim_verification_policy import (
    resolve_claim_verification_policy,
)


_SUMMARY_PATH = (
    "/v1/claim-verification/observations/summary?policy_id=1111111111111111"
)


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _rollout() -> dict:
    return {
        "version": "v1",
        "mode": "shadow",
        "configured_mode": "enforce",
        "rollout_percent": 25.0,
        "cohort_bucket": 9000,
        "cohort_selected": False,
        "fallback_mode": "shadow",
        "policy_id": "1111111111111111",
        "decision": "would_repair",
        "audit_status": "failed",
        "executed": True,
        "released": True,
        "would_intervene": True,
        "would_repair": True,
        "would_block": False,
    }


def _app(monkeypatch, store):
    import cogdoc.api.app as app_module

    monkeypatch.setattr(app_module, "configure_logging", lambda: None)
    return create_app(
        claim_verification_observation_store=store,
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


def _runner(doc_id, query, is_local, chat_history, forced_task):
    return ChatResult(
        answer="灰度回答",
        task_type=forced_task or "qa",
        citations=[],
        evidence=[],
        critique="",
        is_valid=True,
        trace_id="trace-observation",
        request_id="trace-observation",
        steps=[],
        chat_messages=[],
        raw_output={
            "answer": "灰度回答",
            "claim_verification_rollout": _rollout(),
        },
    )


@pytest.mark.anyio
async def test_observation_summary_requires_review_and_is_tenant_scoped(
    monkeypatch,
):
    store = ClaimVerificationObservationStore()
    store.record("tenant-a", "qa", _rollout())
    store.record("tenant-b", "qa", _rollout())
    store.record("tenant-b", "summary", _rollout())
    app = _app(monkeypatch, store)

    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            denied = await client.get(
                _SUMMARY_PATH,
                headers={"X-API-Key": "a-viewer"},
            )
            tenant_a = await client.get(
                _SUMMARY_PATH,
                headers={"X-API-Key": "a-reviewer"},
            )
            tenant_b = await client.get(
                _SUMMARY_PATH,
                headers={"X-API-Key": "b-reviewer"},
            )

    assert denied.status_code == 403
    assert tenant_a.status_code == 200
    assert tenant_a.json()["tenant_id"] == "tenant-a"
    assert tenant_a.json()["total_count"] == 1
    assert tenant_a.json()["policy_id_filter"] == "1111111111111111"
    assert tenant_b.json()["tenant_id"] == "tenant-b"
    assert tenant_b.json()["total_count"] == 2


@pytest.mark.anyio
async def test_observation_summary_validates_closed_filters(monkeypatch):
    app = _app(monkeypatch, ClaimVerificationObservationStore())

    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            bad_mode = await client.get(
                "/v1/claim-verification/observations/summary?effective_mode=invalid",
                headers={"X-API-Key": "a-reviewer"},
            )
            bad_policy = await client.get(
                "/v1/claim-verification/observations/summary?policy_id=secret",
                headers={"X-API-Key": "a-reviewer"},
            )

    assert bad_mode.status_code == 422
    assert bad_policy.status_code == 422


@pytest.mark.anyio
async def test_observation_summary_defaults_to_current_policy(monkeypatch):
    import cogdoc.api.routes.claim_verification as route_module

    settings = Settings(
        _env_file=None,
        claim_verification_mode="shadow",
        claim_verification_rollout_percent=37,
        claim_verification_rollout_seed="summary-current-policy",
    )
    current_policy_id = resolve_claim_verification_policy(
        settings, cohort_key="observation-summary"
    ).policy_id
    store = ClaimVerificationObservationStore()
    current = _rollout()
    current["policy_id"] = current_policy_id
    store.record("tenant-a", "qa", current)
    store.record("tenant-a", "qa", _rollout())
    monkeypatch.setattr(route_module, "get_settings", lambda: settings)
    app = _app(monkeypatch, store)

    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            response = await client.get(
                "/v1/claim-verification/observations/summary",
                headers={"X-API-Key": "a-reviewer"},
            )

    assert response.status_code == 200
    assert response.json()["total_count"] == 1
    assert response.json()["policy_id_filter"] == current_policy_id


@pytest.mark.anyio
async def test_sync_chat_records_one_observation(monkeypatch):
    import cogdoc.api.app as app_module

    monkeypatch.setattr(app_module, "configure_logging", lambda: None)
    store = ClaimVerificationObservationStore()
    app = create_app(
        chat_runner=_runner,
        session_store=SessionStore(),
        claim_verification_observation_store=store,
        api_keys=set(),
    )

    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            response = await client.post(
                "/v1/chat", json={"query": "问题", "doc_id": "kb"}
            )
            summary = await client.get(
                _SUMMARY_PATH
            )

    assert response.status_code == 200
    assert summary.status_code == 200
    assert summary.json()["total_count"] == 1
    assert summary.json()["by_decision"] == {"would_repair": 1}


@pytest.mark.anyio
async def test_observation_write_failure_never_breaks_chat_delivery(monkeypatch):
    import cogdoc.api.app as app_module

    class FailingStore:
        def record(self, tenant_id, task_type, rollout):
            raise RuntimeError("disk unavailable")

        def summary(self, tenant_id, **kwargs):
            raise RuntimeError("not used")

    monkeypatch.setattr(app_module, "configure_logging", lambda: None)
    app = create_app(
        chat_runner=_runner,
        session_store=SessionStore(),
        claim_verification_observation_store=FailingStore(),
        api_keys=set(),
    )

    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            response = await client.post(
                "/v1/chat", json={"query": "问题", "doc_id": "kb"}
            )
            summary = await client.get(
                _SUMMARY_PATH
            )

    assert response.status_code == 200
    assert response.json()["answer"] == "灰度回答"
    assert summary.status_code == 503


@pytest.mark.anyio
async def test_stream_final_records_exactly_one_observation(monkeypatch):
    import cogdoc.api.app as app_module

    def stream_runner(doc_id, query, is_local, chat_history, forced_task):
        yield ChatEvent(
            "final",
            {
                "result": _runner(
                    doc_id, query, is_local, chat_history, forced_task
                )
            },
        )

    monkeypatch.setattr(app_module, "configure_logging", lambda: None)
    store = ClaimVerificationObservationStore()
    app = create_app(
        chat_stream_runner=stream_runner,
        session_store=SessionStore(),
        claim_verification_observation_store=store,
        api_keys=set(),
    )

    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            response = await client.post(
                "/v1/chat/stream", json={"query": "问题", "doc_id": "kb"}
            )
            summary = await client.get(
                _SUMMARY_PATH
            )

    assert response.status_code == 200
    assert summary.json()["total_count"] == 1


@pytest.mark.anyio
async def test_agent_summary_records_tenant_scoped_observation(
    tmp_path, monkeypatch
):
    import cogdoc.api.app as app_module

    monkeypatch.setattr(app_module, "configure_logging", lambda: None)
    registry = KnowledgeBaseRegistry(
        registry_path=str(tmp_path / "registry.json"),
        source_dir_for=lambda storage_id: str(tmp_path / storage_id / "sources"),
    )
    registry.create("kb", "tenant-a", "alice")
    store = ClaimVerificationObservationStore()
    app = create_app(
        kb_registry=registry,
        chat_runner=_runner,
        session_store=SessionStore(),
        claim_verification_observation_store=store,
        api_principals={
            "owner": {
                "tenant_id": "tenant-a",
                "subject_id": "alice",
                "role": "owner",
            }
        },
    )

    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            response = await client.post(
                "/v1/summary",
                json={"query": "总结", "doc_id": "kb"},
                headers={"X-API-Key": "owner"},
            )
            summary = await client.get(
                _SUMMARY_PATH,
                headers={"X-API-Key": "owner"},
            )

    assert response.status_code == 200
    assert summary.status_code == 200
    assert summary.json()["tenant_id"] == "tenant-a"
    assert summary.json()["by_task_type"] == {"summary": 1}
