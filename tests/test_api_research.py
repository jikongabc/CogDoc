import asyncio
import copy
import hashlib
import io
import json
import time
import zipfile
from threading import Event, current_thread
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from cogdoc.api.app import create_app
from cogdoc.api.ingest import KnowledgeBaseRegistry
from cogdoc.api.resource_access import AccessMode, ResourceAccessStore
from cogdoc.api.routes import research as research_routes
from cogdoc.api.tenancy import Principal, Role
from cogdoc.config.settings import Settings
from cogdoc.daemon_executor import DaemonExecutorCapacityError
from cogdoc.research_control import ResearchDeadlineExceeded
from cogdoc.state_runtime import StateRuntime
from cogdoc.service.research_artifact_composer import (
    canonical_research_gap_content,
    compose_research_markdown,
)
from cogdoc.service.research_execution import ResearchExecutionManager
from cogdoc.service.research_provenance import (
    RESEARCH_CONTRACT_VERSION,
    RESEARCH_PROVENANCE_VERSION,
)


REVIEW_HEADERS = {"Authorization": "Bearer test-review-key"}


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _make_app(
    tmp_path,
    monkeypatch,
    retrieve=None,
    report_builder=None,
    *,
    max_pending=None,
):
    import cogdoc.api.app as app_module

    monkeypatch.setattr(app_module, "configure_logging", lambda: None)
    settings = Settings(
        _env_file=None,
        cogdoc_data_dir=str(tmp_path / "data"),
        cogdoc_state_backend="jsonl",
        cogdoc_feedback_store="jsonl",
    )
    runtime = StateRuntime.from_settings(settings)
    registry = KnowledgeBaseRegistry(
        registry_path=str(tmp_path / "registry.json"),
        source_dir_for=lambda kb_id: str(tmp_path / "kb" / kb_id / "sources"),
    )
    registry.create("kb")
    research_manager = ResearchExecutionManager(
        runtime.research_job_store,
        retrieve=retrieve or (lambda _kb_id, _query: []),
        kb_exists=registry.exists,
        report_builder=report_builder,
        provenance_reader=lambda kb_id: {
            "schema_version": RESEARCH_PROVENANCE_VERSION,
            "kb_id": kb_id,
            "index_generation": "generation-1",
            "index_build_version": "index-build-v1",
            "chunk_identity_version": "chunk-identity-v1",
            "source_versions": [{"source": "rules.pdf", "sha256": "source-sha-1"}],
            "derived_knowledge_revision": "derived-1",
            "retrieval_tuning_revision": "tuning-1",
            "research_contract_version": RESEARCH_CONTRACT_VERSION,
            "research_contract_revision": "contract-1",
            "captured_at": "2026-08-10T00:00:00+00:00",
        },
        max_pending=max_pending,
    )
    app = create_app(
        state_runtime=runtime,
        close_state_runtime_on_shutdown=True,
        kb_registry=registry,
        research_execution_manager=research_manager,
    )
    # Research approval/publication uses the independent reviewer credential;
    # keep ordinary test endpoints unauthenticated so each test can focus on
    # the state transition it exercises.
    app.state.eval_review_api_keys = {"test-review-key"}
    return app


def _report_payload(job, sections, *, status, verification_metrics):
    title_by_id = {
        section["section_id"]: section["title"] for section in job["sections"]
    }
    normalized = [
        {"title": title_by_id[section["section_id"]], **section} for section in sections
    ]
    markdown, ledger = compose_research_markdown(job, normalized)
    return {
        "status": status,
        "markdown": markdown,
        "citation_ledger": list(ledger),
        "verification_metrics": verification_metrics,
        "sections": normalized,
    }


def _grounded_report_section(job, section_id: str, content: str) -> dict:
    source = f"{section_id}.pdf"
    citation = f"[{source}:P1]"
    answer = f"{content}{citation}"
    start = len(content)
    plan = next(
        section for section in job["sections"] if section["section_id"] == section_id
    )
    requirement_ids = list(plan["evidence_requirement_ids"])
    return {
        "section_id": section_id,
        "status": "generated",
        "verification_status": "supported",
        "verification_reason_code": "supported",
        "evidence_requirement_results": [
            {
                "requirement_id": requirement_id,
                "status": "supported",
                "reason_code": "supported",
                "evidence_count": 1,
            }
            for requirement_id in requirement_ids
        ],
        "content": answer,
        "citation_ledger": [
            {
                "evidence_id": "E001",
                "chunk_id": f"chunk:{section_id}",
                "source_type": "document",
                "source": source,
                "page": 1,
                "page_start": 1,
                "page_end": 1,
                "span_start": 0,
                "span_end": max(len(content), 1),
                "occurrences": [
                    {
                        "index": 0,
                        "answer_start": start,
                        "answer_end": start + len(citation),
                    }
                ],
            }
        ],
        "claim_audit": {
            "status": "passed",
            "counts": {
                "claim_count": 1,
                "supported": 1,
                "unsupported": 0,
                "insufficient": 0,
                "cited": 1,
            },
        },
        "coverage_audit": {
            "status": "passed",
            "requirement_count": len(requirement_ids),
            "covered_count": len(requirement_ids),
            "missing_requirement_ids": [],
        },
        "evidence": [
            {
                "chunk_id": f"chunk:{section_id}",
                "source_type": "document",
                "source": source,
                "page": 1,
                "page_start": 1,
                "page_end": 1,
                "span_start": 0,
                "span_end": max(len(content), 1),
            }
        ],
        "error": "",
    }


@pytest.mark.anyio
async def test_research_api_create_list_get_and_update_plan(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            created_response = await client.post(
                "/v1/research-jobs",
                json={
                    "kb_id": "kb",
                    "objective": "比较赛事并形成有证据的选择建议",
                    "section_titles": ["参赛门槛", "时间成本"],
                },
            )
            assert created_response.status_code == 201
            created = created_response.json()["job"]
            assert created["revision"] == 1
            assert [section["title"] for section in created["sections"]] == [
                "参赛门槛",
                "时间成本",
            ]

            listed = await client.get("/v1/research-jobs?kb_id=kb")
            assert listed.status_code == 200
            assert [job["job_id"] for job in listed.json()["jobs"]] == [
                created["job_id"]
            ]

            fetched = await client.get(f"/v1/research-jobs/{created['job_id']}")
            assert fetched.status_code == 200
            assert fetched.json()["job"] == created

            updated_response = await client.put(
                f"/v1/research-jobs/{created['job_id']}/plan",
                json={
                    "expected_revision": 1,
                    "sections": [
                        {
                            "title": "选择建议",
                            "research_question": "各项证据共同支持哪种选择？",
                            "evidence_requirements": [
                                {
                                    "question": "各项证据共同支持哪种选择？",
                                    "retrieval_query": "证据 选择建议",
                                    "recovery_query": "选择依据 综合证据",
                                }
                            ],
                        }
                    ],
                },
            )
            assert updated_response.status_code == 200
            updated = updated_response.json()["job"]
            assert updated["revision"] == 2
            assert updated["sections"][0]["section_id"] == "s1"

            conflict = await client.put(
                f"/v1/research-jobs/{created['job_id']}/plan",
                json={
                    "expected_revision": 1,
                    "sections": [
                        {
                            "title": "旧计划",
                            "research_question": "会覆盖吗？",
                            "evidence_requirements": [
                                {
                                    "question": "会覆盖吗？",
                                    "retrieval_query": "旧计划 覆盖",
                                    "recovery_query": "过期计划 修改",
                                }
                            ],
                        }
                    ],
                },
            )
            assert conflict.status_code == 409
            assert conflict.json()["error_code"] == "RESEARCH_JOB_REVISION_CONFLICT"


@pytest.mark.anyio
async def test_research_summary_api_is_bounded_paginated_and_cacheable(
    tmp_path, monkeypatch
):
    app = _make_app(tmp_path, monkeypatch)
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            created_ids = []
            for position in range(3):
                response = await client.post(
                    "/v1/research-jobs",
                    json={
                        "kb_id": "kb",
                        "objective": f"研究目标 {position}",
                        "section_titles": ["范围", "结论"],
                    },
                )
                assert response.status_code == 201
                created_ids.append(response.json()["job"]["job_id"])

            first = await client.get(
                "/v1/research-jobs/summaries",
                params={"kb_id": "kb", "limit": 2},
            )
            assert first.status_code == 200
            assert first.headers["cache-control"] == "private, no-cache"
            assert "Authorization" in first.headers["vary"]
            etag = first.headers["etag"]
            first_payload = first.json()
            assert first_payload["has_more"] is True
            assert first_payload["next_cursor"]
            assert len(first_payload["jobs"]) == 2
            encoded = json.dumps(first_payload, ensure_ascii=False)
            for forbidden in (
                '"sections"',
                '"evidence"',
                '"content"',
                '"report"',
                '"report_history"',
                '"published_report"',
            ):
                assert forbidden not in encoded

            unchanged = await client.get(
                "/v1/research-jobs/summaries",
                params={"kb_id": "kb", "limit": 2},
                headers={"If-None-Match": f"W/{etag}"},
            )
            assert unchanged.status_code == 304
            assert unchanged.content == b""
            assert unchanged.headers["etag"] == etag

            second = await client.get(
                "/v1/research-jobs/summaries",
                params={
                    "kb_id": "kb",
                    "limit": 2,
                    "cursor": first_payload["next_cursor"],
                },
            )
            assert second.status_code == 200
            second_payload = second.json()
            assert second_payload["has_more"] is False
            assert second_payload["next_cursor"] is None
            page_ids = {
                item["job_id"]
                for item in [*first_payload["jobs"], *second_payload["jobs"]]
            }
            assert page_ids == set(created_ids)

            malformed = await client.get(
                "/v1/research-jobs/summaries",
                params={"kb_id": "kb", "cursor": "not-canonical"},
            )
            assert malformed.status_code == 400
            assert malformed.json()["error_code"] == "BAD_REQUEST"


@pytest.mark.anyio
async def test_research_api_projects_bounded_run_control_without_private_lease(
    tmp_path, monkeypatch
):
    app = _make_app(tmp_path, monkeypatch)
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            created = await client.post(
                "/v1/research-jobs",
                json={"kb_id": "kb", "objective": "检查执行预算"},
            )
            job_id = created.json()["job"]["job_id"]
            started = await client.post(f"/v1/research-jobs/{job_id}/start")

            assert started.status_code == 202
            public_job = started.json()["job"]
            evidence = public_job["execution_control"]["evidence"]
            assert evidence["phase"] == "evidence"
            assert evidence["control_state"] == "running"
            assert evidence["attempt_id"] == public_job["execution_id"]
            assert evidence["limits"]["retrieval_queries"] >= 1
            assert evidence["used"]["retrieval_queries"] == 0
            encoded = json.dumps(public_job, ensure_ascii=False)
            assert "_research_control" not in encoded
            assert '"lease_id"' not in encoded
            assert '"draining_lease_id"' not in encoded


@pytest.mark.anyio
async def test_research_api_queue_full_is_retryable_and_does_not_start_job(
    tmp_path, monkeypatch
):
    entered = Event()
    release = Event()

    def blocking_retrieve(_kb_id, _query):
        entered.set()
        assert release.wait(3)
        return []

    app = _make_app(
        tmp_path,
        monkeypatch,
        retrieve=blocking_retrieve,
        max_pending=1,
    )
    try:
        async with app.router.lifespan_context(app):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://testserver"
            ) as client:
                first = await client.post(
                    "/v1/research-jobs",
                    json={"kb_id": "kb", "objective": "占用队列"},
                )
                second = await client.post(
                    "/v1/research-jobs",
                    json={"kb_id": "kb", "objective": "等待容量"},
                )
                first_id = first.json()["job"]["job_id"]
                second_id = second.json()["job"]["job_id"]
                started = await client.post(f"/v1/research-jobs/{first_id}/start")
                assert started.status_code == 202
                assert entered.wait(2)

                rejected = await client.post(f"/v1/research-jobs/{second_id}/start")
                assert rejected.status_code == 503
                assert rejected.headers["retry-after"] == "1"
                assert rejected.json()["error_code"] == "RESEARCH_CAPACITY_EXHAUSTED"
                current = await client.get(f"/v1/research-jobs/{second_id}")
                assert current.json()["job"]["status"] == "planned"
                release.set()
    finally:
        release.set()


@pytest.mark.anyio
async def test_research_api_rejects_equivalent_primary_and_recovery_queries(
    tmp_path, monkeypatch
):
    app = _make_app(tmp_path, monkeypatch)
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            created = await client.post(
                "/v1/research-jobs",
                json={"kb_id": "kb", "objective": "严格计划"},
            )
            job = created.json()["job"]
            rejected = await client.put(
                f"/v1/research-jobs/{job['job_id']}/plan",
                json={
                    "expected_revision": job["revision"],
                    "sections": [
                        {
                            "title": "资格",
                            "research_question": "资格是什么？",
                            "evidence_requirements": [
                                {
                                    "question": "报名资格是什么？",
                                    "retrieval_query": "ＡＢＣ 资格",
                                    "recovery_query": "abc 资格",
                                }
                            ],
                        }
                    ],
                },
            )

    assert rejected.status_code == 422


@pytest.mark.anyio
async def test_research_api_rejects_unknown_kb_and_returns_stable_not_found(
    tmp_path, monkeypatch
):
    app = _make_app(tmp_path, monkeypatch)
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            missing_kb = await client.post(
                "/v1/research-jobs",
                json={"kb_id": "missing", "objective": "研究目标"},
            )
            missing_job = await client.get("/v1/research-jobs/rj_missing")

    assert missing_kb.status_code == 404
    assert missing_kb.json()["error_code"] == "KB_NOT_FOUND"
    assert missing_job.status_code == 404
    assert missing_job.json()["error_code"] == "RESEARCH_JOB_NOT_FOUND"


@pytest.mark.anyio
async def test_research_api_generates_and_applies_atomic_plan(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    captured = {}

    def generate(objective, sources, *, is_local=False):
        captured.update(
            {
                "objective": objective,
                "sources": sources,
                "is_local": is_local,
                "thread_name": current_thread().name,
            }
        )
        return [
            {
                "title": "资格",
                "research_question": "参赛资格是什么？",
                "evidence_requirements": [
                    {
                        "question": "允许哪些对象报名？",
                        "retrieval_query": "报名对象",
                        "recovery_query": "参赛资格 人员",
                    }
                ],
                "success_criteria": "找到明确对象限制或标记证据缺口。",
            }
        ]

    app.state.research_plan_generator = generate
    app.state.research_source_reader = lambda _kb_id: ["rules.pdf"]
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            created = await client.post(
                "/v1/research-jobs",
                json={
                    "kb_id": "kb",
                    "objective": "形成参赛建议",
                    "is_local": True,
                },
            )
            job = created.json()["job"]
            generated = await client.post(
                f"/v1/research-jobs/{job['job_id']}/plan/auto",
                json={"expected_revision": job["revision"]},
            )

    assert generated.status_code == 200
    updated = generated.json()["job"]
    assert updated["revision"] == 2
    assert updated["is_local"] is True
    assert updated["sections"][0]["evidence_requirement_ids"] == ["s1:r1"]
    assert updated["sections"][0]["evidence_requirements"][0] == {
        "requirement_id": "s1:r1",
        "question": "允许哪些对象报名？",
        "retrieval_query": "报名对象",
        "recovery_query": "参赛资格 人员",
    }
    assert captured == {
        "objective": "形成参赛建议",
        "sources": ["rules.pdf"],
        "is_local": True,
        "thread_name": captured["thread_name"],
    }
    assert captured["thread_name"].startswith("cogdoc-research-planning-")


@pytest.mark.anyio
async def test_research_auto_plan_rejects_stale_revision_before_external_reads(
    tmp_path, monkeypatch
):
    app = _make_app(tmp_path, monkeypatch)
    calls = []
    app.state.research_source_reader = lambda _kb_id: calls.append("sources") or []
    app.state.research_plan_generator = lambda *_args, **_kwargs: (
        calls.append("model") or []
    )
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            created = await client.post(
                "/v1/research-jobs",
                json={"kb_id": "kb", "objective": "形成参赛建议"},
            )
            job = created.json()["job"]
            response = await client.post(
                f"/v1/research-jobs/{job['job_id']}/plan/auto",
                json={"expected_revision": job["revision"] + 1},
            )

    assert response.status_code == 409
    assert response.json()["error_code"] == "RESEARCH_JOB_REVISION_CONFLICT"
    assert calls == []


@pytest.mark.anyio
async def test_research_auto_plan_returns_retryable_capacity_error(
    tmp_path, monkeypatch
):
    app = _make_app(tmp_path, monkeypatch)
    calls = []
    app.state.research_planning_executor.shutdown(
        wait=False,
        cancel_futures=True,
    )

    class _FullPlanningExecutor:
        def submit(self, *_args, **_kwargs):
            raise DaemonExecutorCapacityError("full")

        def shutdown(self, **_kwargs):
            return True

    app.state.research_planning_executor = _FullPlanningExecutor()
    app.state.research_source_reader = lambda _kb_id: calls.append("sources") or []
    app.state.research_plan_generator = lambda *_args, **_kwargs: (
        calls.append("model") or []
    )
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            created = await client.post(
                "/v1/research-jobs",
                json={"kb_id": "kb", "objective": "形成参赛建议"},
            )
            job = created.json()["job"]
            response = await client.post(
                f"/v1/research-jobs/{job['job_id']}/plan/auto",
                json={"expected_revision": job["revision"]},
            )

    assert response.status_code == 503
    assert response.json()["error_code"] == "RESEARCH_CAPACITY_EXHAUSTED"
    assert response.headers["Retry-After"] == "1"
    assert calls == []


@pytest.mark.anyio
async def test_research_auto_plan_maps_control_deadline_to_503(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    app.state.research_source_reader = lambda _kb_id: []
    app.state.research_plan_generator = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        ResearchDeadlineExceeded()
    )
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            created = await client.post(
                "/v1/research-jobs",
                json={"kb_id": "kb", "objective": "形成参赛建议"},
            )
            job = created.json()["job"]
            response = await client.post(
                f"/v1/research-jobs/{job['job_id']}/plan/auto",
                json={"expected_revision": job["revision"]},
            )

    assert response.status_code == 503
    assert response.json()["error_code"] == "MODEL_UNAVAILABLE"


@pytest.mark.anyio
async def test_research_auto_plan_deadline_covers_source_read_and_skips_model(
    tmp_path,
    monkeypatch,
):
    app = _make_app(tmp_path, monkeypatch)
    model_called = Event()

    def slow_sources(_kb_id):
        time.sleep(0.15)
        return ["rules.pdf"]

    app.state.research_source_reader = slow_sources
    app.state.research_plan_generator = lambda *_args, **_kwargs: model_called.set()
    monkeypatch.setattr(
        research_routes,
        "get_settings",
        lambda: SimpleNamespace(cogdoc_research_planning_deadline_seconds=0.05),
    )
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            created = await client.post(
                "/v1/research-jobs",
                json={"kb_id": "kb", "objective": "形成参赛建议"},
            )
            job = created.json()["job"]
            response = await client.post(
                f"/v1/research-jobs/{job['job_id']}/plan/auto",
                json={"expected_revision": job["revision"]},
            )
            await asyncio.sleep(0.2)

    assert response.status_code == 503
    assert response.json()["error_code"] == "MODEL_UNAVAILABLE"
    assert model_called.is_set() is False


@pytest.mark.anyio
async def test_research_auto_plan_rejects_completed_job_before_external_reads(
    tmp_path, monkeypatch
):
    app = _make_app(tmp_path, monkeypatch)
    calls = []
    app.state.research_source_reader = lambda _kb_id: calls.append("sources") or []
    app.state.research_plan_generator = lambda *_args, **_kwargs: (
        calls.append("model") or []
    )
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            created = await client.post(
                "/v1/research-jobs",
                json={"kb_id": "kb", "objective": "不可重规划"},
            )
            job = created.json()["job"]
            completed = app.state.research_job_store.get(job["job_id"])
            completed["status"] = "completed"
            monkeypatch.setattr(
                app.state.research_job_store,
                "get",
                lambda _job_id: copy.deepcopy(completed),
            )
            response = await client.post(
                f"/v1/research-jobs/{job['job_id']}/plan/auto",
                json={"expected_revision": job["revision"]},
            )

    assert response.status_code == 409
    assert response.json()["error_code"] == "RESEARCH_JOB_STATE_CONFLICT"
    assert calls == []


@pytest.mark.anyio
async def test_research_auto_plan_rechecks_authorization_before_persisting(
    tmp_path, monkeypatch
):
    app = _make_app(tmp_path, monkeypatch)
    generated_sections = [
        {
            "title": "revoked output",
            "research_question": "must never be persisted",
            "evidence_requirements": [],
            "success_criteria": "must never be persisted",
        }
    ]
    authorized = True

    def generate_then_revoke(*_args, **_kwargs):
        nonlocal authorized
        authorized = False
        return generated_sections

    app.state.research_plan_generator = generate_then_revoke
    monkeypatch.setattr(
        research_routes,
        "_job_is_authorized",
        lambda _request, _row: authorized,
    )
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            created = await client.post(
                "/v1/research-jobs",
                json={"kb_id": "kb", "objective": "authorization race"},
            )
            job = created.json()["job"]
            before = app.state.research_job_store.get(job["job_id"])
            response = await client.post(
                f"/v1/research-jobs/{job['job_id']}/plan/auto",
                json={"expected_revision": job["revision"]},
            )
            after = app.state.research_job_store.get(job["job_id"])

    assert response.status_code == 404
    assert response.json()["error_code"] == "RESEARCH_JOB_NOT_FOUND"
    assert after == before
    assert "revoked output" not in json.dumps(after, ensure_ascii=False)


@pytest.mark.anyio
async def test_research_auto_plan_rechecks_live_session_membership_before_commit(
    tmp_path, monkeypatch
):
    app = _make_app(tmp_path, monkeypatch)
    principal = Principal.for_user_session(
        tenant_id="workspace-a",
        subject_id="user-a",
        role=Role.EDITOR,
        session_id="session-a",
    )
    membership = {"role": "editor"}

    class MembershipStore:
        def membership(self, workspace_id, user_id):
            assert (workspace_id, user_id) == ("workspace-a", "user-a")
            return membership or None

    app.state.auth_store = MembershipStore()
    monkeypatch.setattr(
        research_routes, "request_principal", lambda _request: principal
    )
    monkeypatch.setattr(
        research_routes,
        "scope_for_storage_id",
        lambda _request, storage_id: SimpleNamespace(
            tenant_id="workspace-a", storage_id=storage_id
        ),
    )
    monkeypatch.setattr(
        research_routes, "resource_access_decision", lambda *_args, **_kwargs: None
    )
    app.state.research_source_reader = lambda _kb_id: []

    def generate_then_remove(*_args, **_kwargs):
        membership.clear()
        return [
            {
                "title": "must stay transient",
                "research_question": "revoked member",
                "evidence_requirements": [],
                "success_criteria": "not persisted",
            }
        ]

    app.state.research_plan_generator = generate_then_remove
    job = app.state.research_job_store.create(
        kb_id="kb",
        objective="membership race",
        authorization={
            "version": "research-auth-v1",
            "tenant_id": "workspace-a",
            "created_by": "user-a",
            "creator_role": "editor",
            "auth_kind": "user_session",
            "mode": "all",
            "acl_epoch": 1,
            "allowed_sources": [],
        },
    )
    before = app.state.research_job_store.get(job["job_id"])

    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            response = await client.post(
                f"/v1/research-jobs/{job['job_id']}/plan/auto",
                json={"expected_revision": job["revision"]},
            )
            after = app.state.research_job_store.get(job["job_id"])

    assert response.status_code == 404
    assert after == before


def test_research_authorization_rejects_stale_admin_role_for_private_kb(
    tmp_path, monkeypatch
):
    access_store = ResourceAccessStore(tmp_path / "resource-access.db")
    access_store.set_kb_policy(
        "workspace-a", "kb", "resource-owner", "private"
    )
    membership = {"role": "admin"}
    principal = Principal.for_user_session(
        tenant_id="workspace-a",
        subject_id="user-a",
        role=Role.ADMIN,
        session_id="session-a",
    )

    class MembershipStore:
        @staticmethod
        def membership(workspace_id, user_id):
            assert (workspace_id, user_id) == ("workspace-a", "user-a")
            return membership

    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                auth_store=MembershipStore(),
                resource_access_store=access_store,
            )
        ),
        state=SimpleNamespace(principal=principal),
        method="GET",
        url=SimpleNamespace(path="/v1/research-jobs/rj-private"),
    )
    scope = SimpleNamespace(
        tenant_id="workspace-a",
        external_id="private",
        storage_id="kb",
        owner_id="resource-owner",
    )
    monkeypatch.setattr(
        research_routes,
        "scope_for_storage_id",
        lambda _request, _storage_id: scope,
    )
    row = {
        "job_id": "rj-private",
        "kb_id": "kb",
        "authorization": {
            "version": "research-auth-v1",
            "tenant_id": "workspace-a",
            "created_by": "user-a",
            "creator_role": "admin",
            "auth_kind": "user_session",
            "mode": "all",
            "acl_epoch": 1,
            "allowed_sources": [],
        },
    }
    try:
        # The request-start admin snapshot can bypass this private ACL, while a
        # freshly authenticated editor cannot. The live-role guard must stop the
        # stale admin snapshot from reaching that bypass after a demotion.
        assert access_store.allowed_sources(principal, "kb").mode is AccessMode.ALL
        fresh_editor = Principal.for_user_session(
            tenant_id="workspace-a",
            subject_id="user-a",
            role=Role.EDITOR,
            session_id="session-a",
        )
        assert (
            access_store.allowed_sources(fresh_editor, "kb").mode
            is AccessMode.DENY
        )
        assert research_routes._job_is_authorized(request, row)

        membership["role"] = "editor"

        assert not research_routes._job_is_authorized(request, row)
    finally:
        access_store.close()


def test_research_legacy_job_rejects_stale_session_role_after_demotion(
    monkeypatch,
):
    membership = {"role": "admin"}
    principal = Principal.for_user_session(
        tenant_id="workspace-a",
        subject_id="user-a",
        role=Role.ADMIN,
        session_id="session-a",
    )

    class MembershipStore:
        @staticmethod
        def membership(_workspace_id, _user_id):
            return membership

    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                auth_store=MembershipStore(), resource_access_store=None
            )
        ),
        state=SimpleNamespace(principal=principal),
        method="GET",
        url=SimpleNamespace(path="/v1/research-jobs/rj-legacy"),
    )
    monkeypatch.setattr(
        research_routes,
        "scope_for_storage_id",
        lambda _request, storage_id: SimpleNamespace(storage_id=storage_id),
    )
    legacy_row = {"job_id": "rj-legacy", "kb_id": "kb"}
    assert research_routes._job_is_authorized(request, legacy_row)

    membership["role"] = "editor"

    assert not research_routes._job_is_authorized(request, legacy_row)


@pytest.mark.anyio
async def test_research_api_executes_evidence_and_exposes_progress(
    tmp_path, monkeypatch
):
    def retrieve(_kb_id, query):
        return [
            {
                "text": f"{query} 的直接证据",
                "meta": {"chunk_id": query, "source": "rules.pdf", "page": 1},
                "retrieval": {"rerank_score": 0.8},
            }
        ]

    app = _make_app(tmp_path, monkeypatch, retrieve=retrieve)
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            created = await client.post(
                "/v1/research-jobs",
                json={
                    "kb_id": "kb",
                    "objective": "形成证据矩阵",
                    "section_titles": ["门槛", "时间"],
                },
            )
            job_id = created.json()["job"]["job_id"]
            started = await client.post(f"/v1/research-jobs/{job_id}/start")
            assert started.status_code == 202
            assert started.json()["job"]["status"] == "running"

            body = None
            for _ in range(200):
                response = await client.get(f"/v1/research-jobs/{job_id}")
                body = response.json()["job"]
                if body["status"] == "evidence_ready":
                    break
                await asyncio.sleep(0.01)

            assert body["status"] == "evidence_ready"
            assert all(
                section["evidence_status"] == "partial" for section in body["sections"]
            )
            assert body["sections"][0]["evidence"][0]["source"] == "rules.pdf"
            conflict = await client.post(f"/v1/research-jobs/{job_id}/start")
            assert conflict.status_code == 409
            assert conflict.json()["error_code"] == "RESEARCH_JOB_STATE_CONFLICT"


@pytest.mark.anyio
async def test_research_api_cancel_and_unknown_action_are_stable(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            created = await client.post(
                "/v1/research-jobs",
                json={"kb_id": "kb", "objective": "可取消任务"},
            )
            job_id = created.json()["job"]["job_id"]
            invalid_resume = await client.post(f"/v1/research-jobs/{job_id}/resume")
            cancelled = await client.post(f"/v1/research-jobs/{job_id}/cancel")
            missing = await client.post("/v1/research-jobs/rj_missing/pause")

    assert invalid_resume.status_code == 409
    assert invalid_resume.json()["error_code"] == "RESEARCH_JOB_STATE_CONFLICT"
    assert cancelled.status_code == 200
    assert cancelled.json()["job"]["status"] == "cancelled"
    assert missing.status_code == 404
    assert missing.json()["error_code"] == "RESEARCH_JOB_NOT_FOUND"


@pytest.mark.anyio
async def test_research_api_generates_and_downloads_markdown_report(
    tmp_path, monkeypatch
):
    def report_builder(job):
        return _report_payload(
            job,
            [
                {
                    "section_id": "s1",
                    "status": "no_evidence",
                    "verification_status": "no_evidence",
                    "verification_reason_code": "no_direct_support",
                    "evidence_requirement_results": [
                        {
                            "requirement_id": "s1:r1",
                            "status": "no_evidence",
                            "reason_code": "no_direct_support",
                            "evidence_count": 0,
                            "claim_text": "不得通过 API 泄漏的需求声明",
                            "model_reason": "不得泄漏的需求模型理由",
                        }
                    ],
                    "content": canonical_research_gap_content(
                        "no_evidence", "no_evidence"
                    ),
                    "claim_audit": {
                        "status": "failed",
                        "reason_code": "claims_not_supported",
                        "claims": [
                            {
                                "text": "不得通过 Research API 泄漏的声明全文",
                                "reason": "不得泄漏的模型理由",
                            }
                        ],
                        "counts": {"claim_count": 1, "unsupported": 1},
                        "metrics": {"claim_support_rate": 0.0},
                        "repair": {"attempted": False},
                        "verifier": {"duration_ms": 1.5, "call_count": 1},
                    },
                    "coverage_audit": {
                        "status": "failed",
                        "reason_code": "requirements_missing",
                        "requirement_count": 1,
                        "covered_count": 0,
                        "missing_requirement_ids": ["s1:r1"],
                        "assessments": [
                            {
                                "claim_text": "不得通过 API 泄漏的覆盖声明",
                                "model_reason": "不得泄漏的覆盖模型理由",
                            }
                        ],
                    },
                    "evidence": [],
                    "error": "",
                }
            ],
            status="ready_with_gaps",
            verification_metrics={"no_evidence_count": 1},
        )

    app = _make_app(
        tmp_path,
        monkeypatch,
        report_builder=report_builder,
    )
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            created = await client.post(
                "/v1/research-jobs",
                json={
                    "kb_id": "kb",
                    "objective": "形成报告",
                    "section_titles": ["证据"],
                },
            )
            job_id = created.json()["job"]["job_id"]
            before = await client.get(f"/v1/research-jobs/{job_id}/report")
            assert before.status_code == 409

            await client.post(f"/v1/research-jobs/{job_id}/start")
            for _ in range(200):
                current = await client.get(f"/v1/research-jobs/{job_id}")
                if current.json()["job"]["status"] == "evidence_ready":
                    break
                await asyncio.sleep(0.01)
            generated = await client.post(f"/v1/research-jobs/{job_id}/generate")
            assert generated.status_code == 202
            lifecycle_metrics = (await client.get("/metrics")).text
            assert (
                'cogdoc_research_lifecycle_total{action="generate",outcome="accepted"} 1.0'
                in lifecycle_metrics
            )

            body = None
            for _ in range(200):
                current = await client.get(f"/v1/research-jobs/{job_id}")
                body = current.json()["job"]
                if body["status"] == "completed":
                    break
                await asyncio.sleep(0.01)
            downloaded = await client.get(f"/v1/research-jobs/{job_id}/report")

            premature_publish = await client.post(
                f"/v1/research-jobs/{job_id}/publish",
                json={"expected_revision": body["revision"]},
                headers=REVIEW_HEADERS,
            )
            assert premature_publish.status_code == 409
            reviewed = await client.put(
                f"/v1/research-jobs/{job_id}/review",
                json={
                    "expected_revision": body["revision"],
                    "decisions": [
                        {
                            "section_id": "s1",
                            "decision": "accepted_gap",
                            "note": "接受当前证据限制",
                        }
                    ],
                },
                headers=REVIEW_HEADERS,
            )
            assert reviewed.status_code == 200
            reviewed_job = reviewed.json()["job"]
            assert reviewed_job["review_status"] == "approved"
            published = await client.post(
                f"/v1/research-jobs/{job_id}/publish",
                json={"expected_revision": reviewed_job["revision"]},
                headers=REVIEW_HEADERS,
            )
            published_download = await client.get(
                f"/v1/research-jobs/{job_id}/published-report"
            )
            published_bundle = await client.get(
                f"/v1/research-jobs/{job_id}/published-bundle"
            )
            published_bundle_repeat = await client.get(
                f"/v1/research-jobs/{job_id}/published-bundle"
            )

    assert body["report_status"] == "ready_with_gaps"
    assert body["sections"][0]["verification_status"] == "no_evidence"
    assert body["sections"][0]["evidence_requirement_results"] == [
        {
            "requirement_id": "s1:r1",
            "status": "no_evidence",
            "reason_code": "no_direct_support",
            "evidence_count": 0,
        }
    ]
    assert body["sections"][0]["claim_audit"]["status"] == "failed"
    assert body["sections"][0]["coverage_audit"] == {
        "status": "failed",
        "reason_code": "requirements_missing",
        "requirement_count": 1,
        "covered_count": 0,
        "missing_requirement_ids": ["s1:r1"],
        "repair": {
            "attempted": False,
            "attempt_count": 0,
            "succeeded": False,
            "error": "",
        },
        "auditor": {"call_count": 0, "version": "v1"},
    }
    assert "claims" not in body["sections"][0]["claim_audit"]
    assert "不得通过" not in json.dumps(body, ensure_ascii=False)
    assert downloaded.status_code == 200
    assert downloaded.text.startswith("# 研究报告")
    assert downloaded.headers["content-disposition"].endswith(f'{job_id}.md"')
    assert downloaded.headers["x-cogdoc-integrity"] == "verified"
    assert published.status_code == 200
    assert published.json()["job"]["review_status"] == "published"
    expected_reviewer = (
        "eval-review:" + hashlib.sha256(b"test-review-key").hexdigest()[:16]
    )
    assert reviewed_job["review_history"][-1]["reviewer"] == expected_reviewer
    assert published.json()["job"]["published_by"] == expected_reviewer
    assert (
        published.json()["job"]["published_report"]["published_by"] == expected_reviewer
    )
    assert published.json()["job"]["publication_sha256"]
    assert (
        published.json()["job"]["published_report"]["publication_sha256"]
        == published.json()["job"]["publication_sha256"]
    )
    assert published_download.status_code == 200
    assert published_download.text == downloaded.text
    assert published_download.headers["x-cogdoc-integrity"] == "verified"
    assert published_bundle.status_code == 200
    assert published_bundle.headers["x-cogdoc-integrity"] == "verified"
    assert published_bundle_repeat.content == published_bundle.content
    with zipfile.ZipFile(io.BytesIO(published_bundle.content)) as archive:
        assert sorted(archive.namelist()) == [
            "citation-ledger.json",
            "manifest.json",
            "provenance.json",
            "report.md",
            "verification.json",
        ]
        assert archive.read("report.md").decode() == downloaded.text
        verification = json.loads(archive.read("verification.json"))
        assert verification["schema_version"] == "research-verification-v2"
        assert verification["execution"]["job_id"] == job_id
        assert verification["execution"]["is_local"] is False
        assert (
            verification["sections"][0]["requirements"][0]["requirement_id"] == "s1:r1"
        )
        assert verification["aggregate"] == {"no_evidence_count": 1}
        assert verification["sections"][0]["claim_audit"]["status"] == "failed"
        assert verification["sections"][0]["coverage_audit"]["status"] == "failed"
        assert "claims" not in verification["sections"][0]["claim_audit"]
        assert "assessments" not in verification["sections"][0]["coverage_audit"]
        assert "不得通过" not in json.dumps(verification, ensure_ascii=False)
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["schema_version"] == "research-bundle-v2"
        assert manifest["artifact_schema_version"] == "research-artifact-v2"
        assert manifest["artifact_sha256"]
        assert manifest["published_by"] == expected_reviewer
        assert (
            manifest["publication_sha256"]
            == published.json()["job"]["publication_sha256"]
        )
        for name, expected_sha256 in manifest["files"].items():
            assert hashlib.sha256(archive.read(name)).hexdigest() == expected_sha256


def test_research_bundle_rejects_job_identity_mismatch(monkeypatch):
    monkeypatch.setattr(
        research_routes,
        "_verified_published_artifact_matches",
        lambda _row, _report: True,
    )
    row = {"job_id": "job-A"}
    report = {"verification": {"execution": {"job_id": "job-A"}}}

    with pytest.raises(ValueError, match="job identity"):
        research_routes._published_bundle("job-B", row, report)


def test_research_public_projection_drops_uncommitted_execution_and_review_data(
    monkeypatch,
):
    monkeypatch.setattr(
        research_routes,
        "_verified_artifact_matches_sections",
        lambda _row, _report: True,
    )
    payload = research_routes._safe_public_job_payload(
        {
            "report": {},
            "published_report": None,
            "report_history": [],
            "sections": [
                {
                    "evidence": [],
                    "execution_metrics": {
                        "candidate_count": 2,
                        "duration_ms": 3.5,
                        "model_prompt": "TOP SECRET METRIC",
                        "requirements": [
                            {
                                "requirement_id": "s1:r1",
                                "candidate_count": 2,
                                "chain_of_thought": "TOP SECRET NESTED",
                            }
                        ],
                    },
                    "verification_reason_code": "TOP SECRET REASON",
                    "error": "TOP SECRET ERROR",
                }
            ],
            "review_history": [
                {
                    "report_version": 1,
                    "reviewed_at": "2026-08-10T00:00:00+00:00",
                    "result": "approved",
                    "model_chain_of_thought": "TOP SECRET REVIEW",
                    "decisions": [
                        {
                            "section_id": "s1",
                            "decision": "approved",
                            "note": "人工确认",
                            "private_reason": "TOP SECRET DECISION",
                        }
                    ],
                }
            ],
        }
    )

    serialized = json.dumps(payload, ensure_ascii=False)
    assert "TOP SECRET" not in serialized
    assert payload["sections"][0]["execution_metrics"] == {
        "candidate_count": 2,
        "duration_ms": 3.5,
        "requirements": [{"requirement_id": "s1:r1", "candidate_count": 2}],
    }
    assert payload["review_history"][0]["decisions"][0]["note"] == "人工确认"


@pytest.mark.anyio
async def test_research_review_and_publish_require_independent_reviewer_key(
    tmp_path, monkeypatch
):
    app = _make_app(tmp_path, monkeypatch)
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            created = await client.post(
                "/v1/research-jobs",
                json={"kb_id": "kb", "objective": "权限边界"},
            )
            job = created.json()["job"]
            review_body = {
                "expected_revision": job["revision"],
                "decisions": [
                    {
                        "section_id": "s1",
                        "decision": "changes_requested",
                        "note": "需要补充证据",
                    }
                ],
            }
            no_key_review = await client.put(
                f"/v1/research-jobs/{job['job_id']}/review",
                json=review_body,
            )
            wrong_key_review = await client.put(
                f"/v1/research-jobs/{job['job_id']}/review",
                json=review_body,
                headers={"Authorization": "Bearer wrong-key"},
            )
            no_key_publish = await client.post(
                f"/v1/research-jobs/{job['job_id']}/publish",
                json={"expected_revision": job["revision"]},
            )

    assert no_key_review.status_code == 403
    assert wrong_key_review.status_code == 403
    assert no_key_publish.status_code == 403


@pytest.mark.anyio
async def test_research_api_hides_invalid_artifact_bodies_everywhere(
    tmp_path, monkeypatch
):
    app = _make_app(tmp_path, monkeypatch)
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            created = await client.post(
                "/v1/research-jobs",
                json={"kb_id": "kb", "objective": "完整性边界"},
            )
            job_id = created.json()["job"]["job_id"]
            raw = app.state.research_job_store.get(job_id)
            tampered = copy.deepcopy(raw)
            invalid_artifact = {
                "artifact_schema_version": "research-artifact-v2",
                "format": "markdown",
                "content": "# TOP SECRET TAMPERED REPORT\n",
                "citation_ledger": ["malformed-ledger-entry"],
                "verification_metrics": {},
                "verification": {},
                "provenance": {},
                "sha256": "not-a-valid-artifact-hash",
                "version": 1,
                "generated_at": "2026-08-10T00:00:00+00:00",
            }
            tampered.update(
                {
                    "status": "completed",
                    "report_status": "published",
                    "review_status": "published",
                    "report": copy.deepcopy(invalid_artifact),
                    "published_report": copy.deepcopy(invalid_artifact),
                }
            )
            tampered["sections"][0].update(
                {
                    "content": "TOP SECRET POISONED SECTION",
                    "generation_status": "generated",
                    "evidence_requirement_results": [
                        {
                            "requirement_id": "s1:r1",
                            "status": "supported",
                            "reason_code": "TOP SECRET MODEL REASON",
                            "evidence_count": 1,
                        }
                    ],
                    "evidence": [
                        {
                            "chunk_id": "top-secret-chunk",
                            "text_preview": "TOP SECRET EVIDENCE PREVIEW",
                        }
                    ],
                    "verification_reason_code": "TOP SECRET MODEL REASON",
                    "error": "TOP SECRET ERROR",
                }
            )
            monkeypatch.setattr(
                app.state.research_job_store,
                "get",
                lambda _job_id: copy.deepcopy(tampered),
            )
            monkeypatch.setattr(
                app.state.research_job_store,
                "list",
                lambda **_kwargs: [copy.deepcopy(tampered)],
            )

            fetched = await client.get(f"/v1/research-jobs/{job_id}")
            listed = await client.get("/v1/research-jobs")
            draft = await client.get(f"/v1/research-jobs/{job_id}/report")
            published = await client.get(f"/v1/research-jobs/{job_id}/published-report")
            bundle = await client.get(f"/v1/research-jobs/{job_id}/published-bundle")

    assert fetched.status_code == 200
    assert fetched.json()["job"]["report"] is None
    assert fetched.json()["job"]["published_report"] is None
    assert fetched.json()["job"]["sections"][0]["content"] == ""
    assert fetched.json()["job"]["sections"][0]["evidence_requirement_results"] == []
    assert fetched.json()["job"]["sections"][0]["evidence"] == []
    assert fetched.json()["job"]["sections"][0]["verification_reason_code"] == ""
    assert listed.status_code == 200
    assert listed.json()["jobs"][0]["report"] is None
    assert listed.json()["jobs"][0]["published_report"] is None
    assert listed.json()["jobs"][0]["sections"][0]["content"] == ""
    for response in (fetched, listed, draft, published, bundle):
        assert "TOP SECRET" not in response.text
    assert draft.status_code == 409
    assert published.status_code == 409
    assert bundle.status_code == 409


@pytest.mark.parametrize("projection_drift", ["section", "provenance"])
@pytest.mark.anyio
async def test_research_api_hides_verified_artifact_after_current_projection_drift(
    tmp_path, monkeypatch, projection_drift
):
    def report_builder(job):
        return _report_payload(
            job,
            [
                {
                    "section_id": "s1",
                    "status": "no_evidence",
                    "verification_status": "no_evidence",
                    "verification_reason_code": "no_direct_support",
                    "evidence_requirement_results": [
                        {
                            "requirement_id": "s1:r1",
                            "status": "no_evidence",
                            "reason_code": "no_direct_support",
                            "evidence_count": 0,
                        }
                    ],
                    "content": canonical_research_gap_content(
                        "no_evidence", "no_evidence"
                    ),
                    "citation_ledger": [],
                    "claim_audit": {"status": "not_run"},
                    "coverage_audit": {"status": "not_run"},
                    "evidence": [],
                    "error": "",
                }
            ],
            status="ready_with_gaps",
            verification_metrics={"no_evidence_count": 1},
        )

    app = _make_app(tmp_path, monkeypatch, report_builder=report_builder)
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            created = await client.post(
                "/v1/research-jobs",
                json={
                    "kb_id": "kb",
                    "objective": "章节投影完整性",
                    "section_titles": ["唯一章节"],
                },
            )
            job_id = created.json()["job"]["job_id"]
            await client.post(f"/v1/research-jobs/{job_id}/start")
            completed = None
            generate_requested = False
            for _ in range(200):
                response = await client.get(f"/v1/research-jobs/{job_id}")
                assert response.status_code == 200, response.text
                current = response.json()["job"]
                if current["status"] == "evidence_ready" and not generate_requested:
                    generated = await client.post(
                        f"/v1/research-jobs/{job_id}/generate"
                    )
                    assert generated.status_code == 202, generated.text
                    generate_requested = True
                if current["status"] == "completed":
                    completed = current
                    break
                assert current["status"] != "failed", current.get("error")
                await asyncio.sleep(0.02)
            assert completed is not None
            reviewed = await client.put(
                f"/v1/research-jobs/{job_id}/review",
                json={
                    "expected_revision": completed["revision"],
                    "decisions": [
                        {
                            "section_id": "s1",
                            "decision": "accepted_gap",
                            "note": "接受缺口",
                        }
                    ],
                },
                headers=REVIEW_HEADERS,
            )
            published = await client.post(
                f"/v1/research-jobs/{job_id}/publish",
                json={"expected_revision": reviewed.json()["job"]["revision"]},
                headers=REVIEW_HEADERS,
            )
            assert published.status_code == 200

            tampered = app.state.research_job_store.get(job_id)
            assert tampered["report"]["sha256"]
            if projection_drift == "section":
                tampered["sections"][0]["content"] = "TOP SECRET POISONED SECTION"
            else:
                tampered["evidence_provenance"]["index_generation"] = (
                    "tampered-generation"
                )
            app.state.research_job_store.import_records([tampered])

            fetched = await client.get(f"/v1/research-jobs/{job_id}")
            listed = await client.get("/v1/research-jobs")
            draft = await client.get(f"/v1/research-jobs/{job_id}/report")
            published_download = await client.get(
                f"/v1/research-jobs/{job_id}/published-report"
            )
            bundle = await client.get(f"/v1/research-jobs/{job_id}/published-bundle")

    assert fetched.json()["job"]["report"] is None
    assert fetched.json()["job"]["published_report"] is None
    assert fetched.json()["job"]["sections"][0]["content"] == ""
    assert listed.json()["jobs"][0]["sections"][0]["content"] == ""
    for response in (fetched, listed, draft, published_download, bundle):
        assert "TOP SECRET" not in response.text
    assert draft.status_code == 409
    assert published_download.status_code == 409
    assert bundle.status_code == 409


@pytest.mark.parametrize(
    "publication_drift", ["review_trace", "publisher", "timestamp"]
)
@pytest.mark.anyio
async def test_research_api_rejects_published_projection_with_drifted_review_trace(
    tmp_path, monkeypatch, publication_drift
):
    def report_builder(job):
        return _report_payload(
            job,
            [
                {
                    "section_id": "s1",
                    "status": "no_evidence",
                    "verification_status": "no_evidence",
                    "verification_reason_code": "no_direct_support",
                    "evidence_requirement_results": [],
                    "content": canonical_research_gap_content(
                        "no_evidence", "no_evidence"
                    ),
                    "citation_ledger": [],
                    "claim_audit": {},
                    "coverage_audit": {},
                    "evidence": [],
                    "error": "",
                }
            ],
            status="ready_with_gaps",
            verification_metrics={"no_evidence_count": 1},
        )

    app = _make_app(tmp_path, monkeypatch, report_builder=report_builder)
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            created = await client.post(
                "/v1/research-jobs",
                json={
                    "kb_id": "kb",
                    "objective": "发布审阅轨迹",
                    "section_titles": ["唯一章节"],
                },
            )
            job_id = created.json()["job"]["job_id"]
            await client.post(f"/v1/research-jobs/{job_id}/start")
            completed = None
            generation_requested = False
            for _ in range(200):
                response = await client.get(f"/v1/research-jobs/{job_id}")
                assert response.status_code == 200, response.text
                current = response.json()["job"]
                if current["status"] == "evidence_ready" and not generation_requested:
                    generated = await client.post(
                        f"/v1/research-jobs/{job_id}/generate"
                    )
                    assert generated.status_code == 202, generated.text
                    generation_requested = True
                if current["status"] == "completed":
                    completed = current
                    break
                assert current["status"] != "failed", current.get("error")
                await asyncio.sleep(0.02)
            assert completed is not None
            reviewed = await client.put(
                f"/v1/research-jobs/{job_id}/review",
                json={
                    "expected_revision": completed["revision"],
                    "decisions": [
                        {
                            "section_id": "s1",
                            "decision": "accepted_gap",
                            "note": "明确接受证据缺口",
                        }
                    ],
                },
                headers=REVIEW_HEADERS,
            )
            assert reviewed.status_code == 200, reviewed.text
            published = await client.post(
                f"/v1/research-jobs/{job_id}/publish",
                json={"expected_revision": reviewed.json()["job"]["revision"]},
                headers=REVIEW_HEADERS,
            )
            assert published.status_code == 200, published.text

            tampered = app.state.research_job_store.get(job_id)
            if publication_drift == "review_trace":
                tampered["sections"][0]["reviewed_at"] = "2099-01-01T00:00:00Z"
            elif publication_drift == "publisher":
                tampered["published_by"] = "eval-review:tampered"
                tampered["published_report"]["published_by"] = "eval-review:tampered"
            else:
                tampered["published_at"] = "2099-01-01T00:00:00Z"
                tampered["published_report"]["published_at"] = "2099-01-01T00:00:00Z"
            app.state.research_job_store.import_records([tampered])

            fetched = await client.get(f"/v1/research-jobs/{job_id}")
            listed = await client.get("/v1/research-jobs")
            markdown = await client.get(f"/v1/research-jobs/{job_id}/published-report")
            bundle = await client.get(f"/v1/research-jobs/{job_id}/published-bundle")

    assert fetched.status_code == 200
    assert fetched.json()["job"]["report"] is not None
    assert fetched.json()["job"]["published_report"] is None
    assert listed.status_code == 200
    assert listed.json()["jobs"][0]["published_report"] is None
    assert markdown.status_code == 409
    assert bundle.status_code == 409


@pytest.mark.anyio
async def test_research_api_serves_legacy_markdown_as_unverified_without_bundle(
    tmp_path, monkeypatch
):
    app = _make_app(tmp_path, monkeypatch)
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            created = await client.post(
                "/v1/research-jobs",
                json={"kb_id": "kb", "objective": "迁移旧报告"},
            )
            job_id = created.json()["job"]["job_id"]
            legacy_row = app.state.research_job_store.get(job_id)
            legacy_row.pop("artifact_schema_floor", None)
            legacy_row.update(
                {
                    "status": "completed",
                    "review_status": "published",
                    "report_status": "published",
                    "published_at": "2025-01-02T00:00:00+00:00",
                }
            )
            legacy_row["published_report"] = {
                "format": "markdown",
                "content": "# Legacy report\n",
                "version": 1,
                "generated_at": "2025-01-01T00:00:00+00:00",
                "published_at": "2025-01-02T00:00:00+00:00",
            }
            monkeypatch.setattr(
                app.state.research_job_store,
                "get",
                lambda _job_id: copy.deepcopy(legacy_row),
            )

            fetched = await client.get(f"/v1/research-jobs/{job_id}")
            markdown = await client.get(f"/v1/research-jobs/{job_id}/published-report")
            bundle = await client.get(f"/v1/research-jobs/{job_id}/published-bundle")

    assert fetched.status_code == 200
    assert fetched.json()["job"]["published_report"] is None
    assert "Legacy report" not in fetched.text
    assert markdown.status_code == 200
    assert markdown.text == "# Legacy report\n"
    assert markdown.headers["x-cogdoc-integrity"] == "legacy-unverified"
    assert bundle.status_code == 409
    assert "无法生成验证包" in bundle.json()["message"]


@pytest.mark.anyio
async def test_research_api_regenerates_rejected_report_as_new_version(
    tmp_path, monkeypatch
):
    seen_revision_instructions = []
    seen_regeneration_scopes = []

    def report_builder(job):
        seen_revision_instructions.append(
            job["sections"][0].get("revision_instruction", "")
        )
        seen_regeneration_scopes.append(job.get("regeneration_section_ids", []))
        return _report_payload(
            job,
            [
                _grounded_report_section(
                    job,
                    "s1",
                    f"正文 v{len(seen_revision_instructions)}。",
                )
            ],
            status="ready",
            verification_metrics={"supported_count": 1},
        )

    app = _make_app(
        tmp_path,
        monkeypatch,
        report_builder=report_builder,
    )
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            created = await client.post(
                "/v1/research-jobs",
                json={
                    "kb_id": "kb",
                    "objective": "形成可修订报告",
                    "section_titles": ["证据"],
                },
            )
            job_id = created.json()["job"]["job_id"]
            await client.post(f"/v1/research-jobs/{job_id}/start")
            first = None
            for _ in range(200):
                current = (await client.get(f"/v1/research-jobs/{job_id}")).json()[
                    "job"
                ]
                if current["status"] == "evidence_ready":
                    await client.post(f"/v1/research-jobs/{job_id}/generate")
                if current["status"] == "completed":
                    first = current
                    break
                await asyncio.sleep(0.01)
            assert first["report_version"] == 1
            rejected = await client.put(
                f"/v1/research-jobs/{job_id}/review",
                json={
                    "expected_revision": first["revision"],
                    "decisions": [
                        {
                            "section_id": "s1",
                            "decision": "changes_requested",
                            "note": "补充明确时间范围",
                        }
                    ],
                },
                headers=REVIEW_HEADERS,
            )
            rejected_job = rejected.json()["job"]
            regenerated = await client.post(f"/v1/research-jobs/{job_id}/generate")
            assert regenerated.status_code == 202
            second = None
            for _ in range(200):
                current = (await client.get(f"/v1/research-jobs/{job_id}")).json()[
                    "job"
                ]
                if current["status"] == "completed":
                    second = current
                    break
                await asyncio.sleep(0.01)

    assert rejected_job["review_status"] == "changes_requested"
    assert second["report_version"] == 2
    assert len(second["report_history"]) == 1
    assert seen_revision_instructions == ["", "补充明确时间范围"]
    assert seen_regeneration_scopes == [[], ["s1"]]
