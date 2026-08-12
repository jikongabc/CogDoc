import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient

from cogdoc.api.research_job_store import ResearchJobStore
from cogdoc.api.routes import research as research_routes
from cogdoc.api.routes import traces as trace_routes
from cogdoc.api.tenancy import Principal, Role
from cogdoc.observability.trace import build_trace_payload
from cogdoc.service.research_provenance import (
    RESEARCH_ARTIFACT_VERSION,
    research_artifact_integrity_status,
)


@pytest.fixture
def anyio_backend():
    return "asyncio"


@dataclass(frozen=True)
class _KBRecord:
    tenant_id: str
    kb_id: str
    storage_id: str
    owner_id: str
    created_at: str = "2026-08-12T00:00:00+00:00"

    def as_dict(self):
        return vars(self).copy()


class _TenantRegistry:
    def __init__(self, records):
        self._records = list(records)

    def resolve(self, kb_id, tenant_id="default"):
        for record in self._records:
            if record.tenant_id == tenant_id and record.kb_id == kb_id:
                return record.as_dict()
        return None

    def get_by_storage_id(self, storage_id):
        for record in self._records:
            if record.storage_id == storage_id:
                return record.as_dict()
        return None

    def get(self, storage_id, tenant_id=None):
        if tenant_id is not None:
            return self.resolve(storage_id, tenant_id)
        return self.get_by_storage_id(storage_id)

    def list(self, tenant_id=None):
        return [
            record.as_dict()
            for record in self._records
            if tenant_id is None or record.tenant_id == tenant_id
        ]


class _ResearchManager:
    def __init__(self, store):
        self.store = store
        self.mutation_calls = []

    @staticmethod
    def _snapshot(kb_id):
        return {
            "schema_version": "research-provenance-v1",
            "kb_id": kb_id,
            "index_generation": "g1",
            "index_build_version": "build-v1",
            "chunk_identity_version": "chunk-v1",
            "source_versions": [],
            "derived_knowledge_revision": "d1",
            "retrieval_tuning_revision": "t1",
            "research_contract_version": "contract-v1",
            "research_contract_revision": "r1",
            "captured_at": "2026-08-12T00:00:00+00:00",
        }

    def provenance(self, target):
        row = target if isinstance(target, dict) else self.store.get(target)
        if row is None:
            raise KeyError(target)
        snapshot = self._snapshot(row["kb_id"])
        return {
            "status": "current",
            "stale_reasons": [],
            "captured": snapshot,
            "current": snapshot,
        }

    def provenance_many(self, rows):
        return [self.provenance(row) for row in rows]

    def __getattr__(self, action):
        if action not in {
            "start",
            "resume",
            "pause",
            "cancel",
            "refresh",
            "compile",
            "review_report",
            "publish_report",
        }:
            raise AttributeError(action)

        def mutate(job_id, **_kwargs):
            self.mutation_calls.append((action, job_id))
            row = self.store.get(job_id)
            if row is None:
                raise KeyError(job_id)
            return row

        return mutate


@pytest.fixture
def tenant_app(tmp_path):
    storage_a = "tenant_storage_a"
    storage_b = "tenant_storage_b"
    # Tenant B intentionally owns a logical slug equal to tenant A's physical
    # ID.  Authorization must never reinterpret that opaque ID as this alias.
    registry = _TenantRegistry(
        [
            _KBRecord("tenant-a", "shared", storage_a, "alice"),
            _KBRecord("tenant-b", "shared", storage_b, "bob"),
            _KBRecord("tenant-b", storage_a, "tenant_storage_b_alias", "bob"),
        ]
    )
    store = ResearchJobStore(str(tmp_path / "research.jsonl"))
    job_a = store.create(
        kb_id=storage_a,
        objective="tenant A objective",
        section_titles=["Evidence"],
    )
    job_b = store.create(
        kb_id=storage_b,
        objective="tenant B objective",
        section_titles=["Evidence"],
    )
    manager = _ResearchManager(store)
    executor = ThreadPoolExecutor(max_workers=4)
    principals = {
        "key-a": Principal.for_api_key(
            "key-a",
            tenant_id="tenant-a",
            subject_id="alice",
            role=Role.OWNER,
        ),
        "key-b": Principal.for_api_key(
            "key-b",
            tenant_id="tenant-b",
            subject_id="bob",
            role=Role.OWNER,
        ),
        "review-a": Principal.for_api_key(
            "review-a",
            tenant_id="tenant-a",
            subject_id="reviewer-a",
            role=Role.OWNER,
        ),
        "review-b": Principal.for_api_key(
            "review-b",
            tenant_id="tenant-b",
            subject_id="reviewer-b",
            role=Role.OWNER,
        ),
    }

    app = FastAPI()

    @app.middleware("http")
    async def inject_principal(request: Request, call_next):
        authorization = request.headers.get("authorization", "")
        token = authorization.removeprefix("Bearer ")
        request.state.principal = principals[token]
        return await call_next(request)

    app.include_router(research_routes.router)
    app.include_router(trace_routes.router)
    app.state.kb_registry = registry
    app.state.research_job_store = store
    app.state.research_execution_manager = manager
    app.state.research_plan_generator = lambda *_args, **_kwargs: []
    app.state.offload_executor = executor
    app.state.research_planning_executor = executor
    app.state.eval_review_api_keys = {"review-a", "review-b"}

    try:
        yield {
            "app": app,
            "store": store,
            "manager": manager,
            "job_a": job_a,
            "job_b": job_b,
            "storage_a": storage_a,
            "storage_b": storage_b,
        }
    finally:
        executor.shutdown(wait=True)


def _headers(key):
    return {"Authorization": f"Bearer {key}"}


@pytest.mark.anyio
async def test_research_lists_create_and_provenance_are_tenant_scoped_and_externalized(
    tenant_app,
):
    app = tenant_app["app"]
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        listed_a = await client.get("/v1/research-jobs", headers=_headers("key-a"))
        listed_b = await client.get("/v1/research-jobs", headers=_headers("key-b"))
        summaries_a = await client.get(
            "/v1/research-jobs/summaries", headers=_headers("key-a")
        )
        filtered_b = await client.get(
            "/v1/research-jobs", params={"kb_id": "shared"}, headers=_headers("key-b")
        )
        created_a = await client.post(
            "/v1/research-jobs",
            headers=_headers("key-a"),
            json={"kb_id": "shared", "objective": "new tenant A research"},
        )
        provenance_a = await client.get(
            f"/v1/research-jobs/{tenant_app['job_a']['job_id']}/provenance",
            headers=_headers("key-a"),
        )

    assert [row["job_id"] for row in listed_a.json()["jobs"]] == [
        tenant_app["job_a"]["job_id"]
    ]
    assert [row["job_id"] for row in listed_b.json()["jobs"]] == [
        tenant_app["job_b"]["job_id"]
    ]
    assert summaries_a.json()["jobs"][0]["kb_id"] == "shared"
    assert filtered_b.json()["jobs"][0]["kb_id"] == "shared"
    assert created_a.status_code == 201
    assert created_a.json()["job"]["kb_id"] == "shared"
    persisted_created = tenant_app["store"].get(created_a.json()["job"]["job_id"])
    assert persisted_created["kb_id"] == tenant_app["storage_a"]
    assert provenance_a.status_code == 200
    assert provenance_a.json()["captured"]["kb_id"] == "shared"
    assert provenance_a.json()["current"]["kb_id"] == "shared"
    response_text = " ".join(
        [
            listed_a.text,
            summaries_a.text,
            created_a.text,
            provenance_a.text,
        ]
    )
    assert tenant_app["storage_a"] not in response_text
    assert tenant_app["storage_b"] not in response_text


@pytest.mark.anyio
async def test_every_opaque_research_endpoint_checks_tenant_before_operation(
    tenant_app,
):
    job_id = tenant_app["job_a"]["job_id"]
    section_id = tenant_app["job_a"]["sections"][0]["section_id"]
    endpoints = [
        ("GET", f"/v1/research-jobs/{job_id}", None, "key-b"),
        ("GET", f"/v1/research-jobs/{job_id}/provenance", None, "key-b"),
        ("POST", f"/v1/research-jobs/{job_id}/start", None, "key-b"),
        ("POST", f"/v1/research-jobs/{job_id}/resume", None, "key-b"),
        ("POST", f"/v1/research-jobs/{job_id}/pause", None, "key-b"),
        ("POST", f"/v1/research-jobs/{job_id}/cancel", None, "key-b"),
        ("POST", f"/v1/research-jobs/{job_id}/refresh", None, "key-b"),
        ("POST", f"/v1/research-jobs/{job_id}/generate", None, "key-b"),
        ("GET", f"/v1/research-jobs/{job_id}/report", None, "key-b"),
        (
            "PUT",
            f"/v1/research-jobs/{job_id}/review",
            {
                "expected_revision": 1,
                "decisions": [{"section_id": section_id, "decision": "approved"}],
            },
            "review-b",
        ),
        (
            "POST",
            f"/v1/research-jobs/{job_id}/publish",
            {"expected_revision": 1},
            "review-b",
        ),
        ("GET", f"/v1/research-jobs/{job_id}/published-report", None, "key-b"),
        ("GET", f"/v1/research-jobs/{job_id}/published-bundle", None, "key-b"),
        (
            "PUT",
            f"/v1/research-jobs/{job_id}/plan",
            {
                "expected_revision": 1,
                "sections": [
                    {
                        "title": "Evidence",
                        "research_question": "What supports this?",
                        "evidence_requirements": [
                            {
                                "question": "What supports this?",
                                "retrieval_query": "primary evidence",
                                "recovery_query": "alternative evidence",
                            }
                        ],
                    }
                ],
            },
            "key-b",
        ),
        (
            "POST",
            f"/v1/research-jobs/{job_id}/plan/auto",
            {"expected_revision": 1},
            "key-b",
        ),
    ]

    app = tenant_app["app"]
    tenant_app["manager"].mutation_calls.clear()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        responses = [
            await client.request(
                method,
                path,
                headers=_headers(key),
                json=body,
            )
            for method, path, body, key in endpoints
        ]

    assert [response.status_code for response in responses] == [404] * len(endpoints)
    assert all(
        response.json()["error_code"] == "RESEARCH_JOB_NOT_FOUND"
        for response in responses
    )
    assert tenant_app["manager"].mutation_calls == []


@pytest.mark.anyio
async def test_trace_list_and_detail_enforce_tenant_without_doc_filter_and_externalize(
    tenant_app, tmp_path, monkeypatch
):
    trace_dir = tmp_path / "traces"
    trace_dir.mkdir()

    def write_trace(trace_id, storage_id, *, unscoped=False):
        config = {"query_preview": trace_id}
        if not unscoped:
            config["doc_id"] = storage_id
        payload = build_trace_payload(
            trace_id,
            f"req-{trace_id}",
            "qa",
            [],
            config=config,
            input_payload={"doc_id": storage_id, "nested": {"kb_id": storage_id}},
            output_payload={"kb_id": storage_id},
        )
        (trace_dir / f"{trace_id}.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )

    write_trace("trace-a", tenant_app["storage_a"])
    write_trace("trace-b", tenant_app["storage_b"])
    write_trace("trace-legacy", "legacy", unscoped=True)
    monkeypatch.setattr(trace_routes, "trace_dir", lambda: trace_dir)
    monkeypatch.setattr(
        trace_routes, "trace_path", lambda trace_id: trace_dir / f"{trace_id}.json"
    )

    app = tenant_app["app"]
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        listed_b = await client.get("/v1/traces", headers=_headers("key-b"))
        filtered_b = await client.get(
            "/v1/traces", params={"doc_id": "shared"}, headers=_headers("key-b")
        )
        forbidden_detail = await client.get(
            "/v1/traces/trace-a", headers=_headers("key-b")
        )
        own_detail = await client.get(
            "/v1/traces/trace-a", headers=_headers("key-a")
        )

    assert [row["trace_id"] for row in listed_b.json()["traces"]] == ["trace-b"]
    assert [row["trace_id"] for row in filtered_b.json()["traces"]] == ["trace-b"]
    assert forbidden_detail.status_code == 404
    assert forbidden_detail.json()["error_code"] == "TRACE_NOT_FOUND"
    assert own_detail.status_code == 200
    assert own_detail.json()["config"]["doc_id"] == "shared"
    assert own_detail.json()["input"]["doc_id"] == "shared"
    assert own_detail.json()["input"]["nested"]["kb_id"] == "shared"
    assert own_detail.json()["output"]["kb_id"] == "shared"
    assert tenant_app["storage_a"] not in own_detail.text


def test_externalized_research_artifact_and_public_bundle_remain_self_verifying(
    tenant_app,
):
    request = type("RequestStub", (), {})()
    request.state = type("StateStub", (), {"principal": Principal.for_api_key(
        "key-a",
        tenant_id="tenant-a",
        subject_id="alice",
        role=Role.OWNER,
    )})()
    request.app = tenant_app["app"]
    storage_id = tenant_app["storage_a"]
    job = tenant_app["job_a"]
    verification = {
        "schema_version": "research-verification-v2",
        "execution": {
            "job_id": job["job_id"],
            "kb_id": storage_id,
            "execution_id": "execution-1",
            "report_execution_id": "report-execution-1",
            "title": job["title"],
            "objective": job["objective"],
            "is_local": False,
            "nodes": [
                {
                    "node": node,
                    "backend": "cloud",
                    "model": "test-model",
                    "protocol_version": "v1",
                }
                for node in (
                    "evidence_verifier",
                    "summary_generator",
                    "claim_verifier",
                    "claim_repairer",
                )
            ],
        },
        "aggregate": {},
        "sections": [],
    }
    provenance = _ResearchManager._snapshot(storage_id)
    artifact = {
        "artifact_schema_version": RESEARCH_ARTIFACT_VERSION,
        "format": "markdown",
        "content": "# Public research",
        "citation_ledger": [],
        "verification_metrics": {},
        "verification": verification,
        "provenance": provenance,
        "sha256": "",
        "version": 1,
        "generated_at": "2026-08-12T00:00:00+00:00",
        "published_at": None,
        "published_by": "",
        "publication_sha256": "",
    }
    research_routes._rehash_externalized_artifact(artifact)
    row = {
        **job,
        "artifact_schema_floor": RESEARCH_ARTIFACT_VERSION,
        "status": "completed",
        "report_status": "ready",
        "report": artifact,
        "report_version": 1,
        "review_status": "not_started",
        "review_history": [],
        "published_report": None,
        "published_at": None,
        "published_by": "",
        "publication_sha256": "",
    }

    public = research_routes._externalize_integrity_bound_job(row, request)

    assert public["kb_id"] == "shared"
    assert public["report"]["provenance"]["kb_id"] == "shared"
    assert public["report"]["verification"]["execution"]["kb_id"] == "shared"
    assert research_artifact_integrity_status(public["report"]) == "verified"
    assert storage_id not in json.dumps(public, ensure_ascii=False)
