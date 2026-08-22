from __future__ import annotations

import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from cogdoc.api.app import create_app
from cogdoc.api.auth_store import AuthStore
from cogdoc.api.ingest import IndexJobManager
from cogdoc.api.research_job_store import (
    ResearchJobStateConflictError,
    research_run_control,
)
from cogdoc.api.resource_access import ResourceAccessStore
from cogdoc.ha.api_state import (
    DistributedIndexJobStore,
    DistributedKnowledgeBaseRegistry,
    DistributedMutationCoordinator,
)
from cogdoc.ha.connector_commit import DistributedConnectorCommitStore
from cogdoc.ha.index_generation import IndexGenerationStore
from cogdoc.ha.object_store import LocalObjectStore
from cogdoc.ha.source_artifact_store import DistributedSourceArtifactStore
from cogdoc.ha.source_catalog import DistributedSourceCatalog
from cogdoc.ha.source_generation import SourceGenerationStore
from cogdoc.ha.storage import SQLiteBackend
from cogdoc.service.mutation_journal import MutationJournal
from cogdoc.service.chat_service import ChatResult
from cogdoc.connectors.connection_store import ConnectionStore


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _wait_for_research_status(store, job_id: str, status: str) -> dict:
    deadline = time.monotonic() + 5
    row = None
    while time.monotonic() < deadline:
        row = store.get(job_id)
        if row is not None and row.get("status") == status:
            return row
        time.sleep(0.01)
    raise AssertionError(f"research job did not reach {status}: {row!r}")


def _runtime(tmp_path: Path):
    backend = SQLiteBackend(tmp_path / "shared.db")
    objects = LocalObjectStore(tmp_path / "objects")
    registry = DistributedKnowledgeBaseRegistry(backend, tmp_path / "cache")
    registry.create("docs", "default", "default")
    coordinator = DistributedMutationCoordinator(
        backend, registry, owner_id="node-a", lease_seconds=30
    )
    source_generations = SourceGenerationStore(backend, objects)
    return registry, SimpleNamespace(
        api_multi_writer_safe=True,
        start=lambda: None,
        shutdown=lambda: None,
        check=lambda: True,
        backend=backend,
        config=SimpleNamespace(worker_id="node-a", mutation_lease_seconds=30),
        api_mutation_coordinator=coordinator,
        source_generations=source_generations,
        source_catalog=DistributedSourceCatalog(backend),
        source_artifact_store=DistributedSourceArtifactStore(
            backend, objects, owner_id="node-a"
        ),
        connector_commits=DistributedConnectorCommitStore(
            backend, objects, coordinator
        ),
        index_generations=None,
        tenant_quota_manager=None,
    )


def _index_manager(runtime, registry, tmp_path: Path, owner_id: str):
    return IndexJobManager(
        ingest_fn=lambda *_args, **_kwargs: SimpleNamespace(
            document_count=0, chunk_count=0, ocr_summary={}
        ),
        source_dir_for=registry.source_dir,
        job_store=DistributedIndexJobStore(
            runtime.backend, owner_id=owner_id, lease_seconds=30
        ),
        kb_exists=registry.exists,
        epoch_reader=registry.current,
        lifecycle_reader=registry.status,
        mutation_coordinator=runtime.api_mutation_coordinator,
        source_generation_store=runtime.source_generations,
        journal=MutationJournal(tmp_path / f"{owner_id}-journal.json"),
    )


def _publish_empty_index(
    store: IndexGenerationStore,
    tenant_id: str,
    kb_id: str,
    build_id: str,
) -> dict:
    current = store.current(tenant_id, kb_id)
    generation = store.begin_build(
        tenant_id,
        kb_id,
        build_id,
        "test-indexer",
        base_generation_id=(
            str(current["generation_id"]) if current is not None else None
        ),
    )
    prepared = store.prepare(
        str(generation["generation_id"]),
        str(generation["lease_token"]),
        {
            "schema_version": "index-manifest-v1",
            "contract": {
                "chunk_version": "chunk-v1",
                "embedding_model": "embedding-v1",
                "dimensions": 1,
            },
            "files": [],
        },
    )
    return store.publish(
        str(prepared["generation_id"]),
        str(prepared["lease_token"]),
        lambda _generation: None,
    )


class _ChatIndexProvider:
    def __init__(self, registry, generation_id: str) -> None:
        self.registry = registry
        self.generation_id = generation_id
        self.entries = 0

    def __call__(self, kb_id: str):
        return SimpleNamespace(kb_id=kb_id, generation_id=self.generation_id)

    @contextmanager
    def pin(self, kb_id: str):
        self.entries += 1
        yield {
            "tenant_id": "default",
            "kb_id": kb_id,
            "generation_id": self.generation_id,
        }


def _chat_result(doc_id, query, *_args, **_kwargs):
    del doc_id
    return ChatResult(
        answer=f"answer:{query}",
        task_type="qa",
        citations=[],
        evidence=[],
        critique="",
        is_valid=True,
        trace_id=f"trace:{query}",
        request_id=f"trace:{query}",
        steps=[],
        chat_messages=[
            {"role": "user", "content": query},
            {"role": "assistant", "content": f"answer:{query}"},
        ],
        raw_output={},
    )


@pytest.mark.anyio
async def test_ha_research_components_participate_in_readiness(
    tmp_path: Path,
) -> None:
    registry, runtime = _runtime(tmp_path)
    app = create_app(
        kb_registry=registry,
        index_jobs=_index_manager(runtime, registry, tmp_path, "node-a"),
        ha_runtime=runtime,
        close_state_runtime_on_shutdown=False,
    )
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://node-a"
        ) as client:
            response = await client.get("/health/ready")
            assert response.status_code == 200, response.text
            components = response.json()["components"]
            assert components["ha_research_jobs"] == {
                "status": "ready",
                "required": True,
            }
            assert components["ha_research_dispatch"] == {
                "status": "ready",
                "required": True,
            }
            assert components["ha_research_worker"] == {
                "status": "ready",
                "required": True,
            }
    runtime.backend.close()


@pytest.mark.anyio
async def test_ha_chat_session_and_generation_are_shared_across_apps(
    tmp_path: Path,
) -> None:
    registry, runtime = _runtime(tmp_path)
    first_provider = _ChatIndexProvider(registry, "generation-one")
    first = create_app(
        kb_registry=registry,
        index_jobs=_index_manager(runtime, registry, tmp_path, "node-a"),
        ha_runtime=runtime,
        ha_index_provider=first_provider,
        chat_runner=_chat_result,
        close_state_runtime_on_shutdown=False,
    )
    second_registry = DistributedKnowledgeBaseRegistry(
        runtime.backend, tmp_path / "cache-b"
    )
    second_runtime = SimpleNamespace(**vars(runtime))
    second_runtime.config = SimpleNamespace(
        worker_id="node-b", mutation_lease_seconds=30
    )
    second_runtime.api_mutation_coordinator = DistributedMutationCoordinator(
        runtime.backend, second_registry, owner_id="node-b", lease_seconds=30
    )
    second_runtime.connector_commits = DistributedConnectorCommitStore(
        runtime.backend,
        runtime.source_generations.object_store,
        second_runtime.api_mutation_coordinator,
    )
    second_provider = _ChatIndexProvider(second_registry, "generation-two")
    second = create_app(
        kb_registry=second_registry,
        index_jobs=_index_manager(second_runtime, second_registry, tmp_path, "node-b"),
        ha_runtime=second_runtime,
        ha_index_provider=second_provider,
        chat_runner=_chat_result,
        close_state_runtime_on_shutdown=False,
    )
    second.state.retrieve_runner = lambda _body, **_kwargs: []

    async with AsyncClient(
        transport=ASGITransport(app=first), base_url="http://node-a"
    ) as first_client:
        response = await first_client.post(
            "/v1/chat",
            json={"doc_id": "docs", "query": "shared", "session_id": "s1"},
        )
        assert response.status_code == 200, response.text

    async with AsyncClient(
        transport=ASGITransport(app=second), base_url="http://node-b"
    ) as second_client:
        history = await second_client.get(
            "/v1/sessions/s1/history", params={"doc_id": "docs"}
        )
        assert history.status_code == 200, history.text
        assert history.json()["messages"][-1]["index_generation_id"] == (
            "generation-one"
        )
        summary = await second_client.post(
            "/v1/summary",
            json={"doc_id": "docs", "query": "summary", "session_id": "s1"},
        )
        assert summary.status_code == 200, summary.text
        retrieved = await second_client.post(
            "/v1/retrieve",
            json={"doc_id": "docs", "query": "find", "top_k": 3},
        )
        assert retrieved.status_code == 200, retrieved.text
        assert second_provider.entries == 2
        updated = await second_client.get(
            "/v1/sessions/s1/history", params={"doc_id": "docs"}
        )
        assert updated.json()["messages"][-1]["index_generation_id"] == (
            "generation-two"
        )
        readiness = await second_client.get("/health/ready")
        # This test deliberately does not enter app lifespan; unrelated
        # lifecycle/research-worker probes remain not-ready, while the HA chat
        # components themselves must already be wired and healthy.
        assert readiness.status_code == 503, readiness.text
        components = readiness.json()["components"]
        assert components["ha_chat_memory"] == {
            "status": "ready",
            "required": True,
        }
        assert components["ha_chat_execution"] == {
            "status": "ready",
            "required": True,
        }

    first.state.sync_manager.shutdown(wait=True)
    second.state.sync_manager.shutdown(wait=True)
    first.state.index_jobs.shutdown(wait=True)
    second.state.index_jobs.shutdown(wait=True)
    first.state.offload_executor.shutdown(wait=True)
    second.state.offload_executor.shutdown(wait=True)
    runtime.backend.close()


@pytest.mark.anyio
async def test_ha_connector_routes_use_shared_control_plane_across_apps(
    tmp_path: Path,
) -> None:
    registry, runtime = _runtime(tmp_path)
    first = create_app(
        kb_registry=registry,
        index_jobs=_index_manager(runtime, registry, tmp_path, "node-a"),
        ha_runtime=runtime,
        close_state_runtime_on_shutdown=False,
    )
    second_registry = DistributedKnowledgeBaseRegistry(
        runtime.backend, tmp_path / "cache-b"
    )
    second_runtime = SimpleNamespace(**vars(runtime))
    second_runtime.config = SimpleNamespace(
        worker_id="node-b", mutation_lease_seconds=30
    )
    second_runtime.api_mutation_coordinator = DistributedMutationCoordinator(
        runtime.backend, second_registry, owner_id="node-b", lease_seconds=30
    )
    second_runtime.connector_commits = DistributedConnectorCommitStore(
        runtime.backend,
        runtime.source_generations.object_store,
        second_runtime.api_mutation_coordinator,
    )
    second = create_app(
        kb_registry=second_registry,
        index_jobs=_index_manager(second_runtime, second_registry, tmp_path, "node-b"),
        ha_runtime=second_runtime,
        close_state_runtime_on_shutdown=False,
    )

    async with AsyncClient(
        transport=ASGITransport(app=first), base_url="http://node-a"
    ) as first_client:
        research = await first_client.post(
            "/v1/research-jobs",
            json={"kb_id": "docs", "objective": "Shared research"},
        )
        assert research.status_code == 201, research.text
        research_job_id = research.json()["job"]["job_id"]
        created = await first_client.post(
            "/v1/knowledge-bases/docs/connections",
            json={
                "connector_type": "url",
                "name": "Shared web",
                "config": {"urls": ["https://example.com/docs"]},
                "secret_env": {},
            },
        )
        assert created.status_code == 201, created.text
        local = await first_client.post(
            "/v1/knowledge-bases/docs/connections",
            json={
                "connector_type": "local-directory",
                "name": "Unsafe local",
                "config": {"root": str(tmp_path)},
                "secret_env": {},
            },
        )
        assert local.status_code == 400

    async with AsyncClient(
        transport=ASGITransport(app=second), base_url="http://node-b"
    ) as second_client:
        shared_research = await second_client.get(
            f"/v1/research-jobs/{research_job_id}"
        )
        assert shared_research.status_code == 200, shared_research.text
        assert shared_research.json()["job"]["objective"] == "Shared research"
        listed = await second_client.get("/v1/knowledge-bases/docs/connections")
        assert listed.status_code == 200, listed.text
        assert [item["name"] for item in listed.json()["connections"]] == ["Shared web"]

    first.state.sync_manager.shutdown(wait=True)
    second.state.sync_manager.shutdown(wait=True)
    first.state.index_jobs.shutdown(wait=True)
    second.state.index_jobs.shutdown(wait=True)
    runtime.backend.close()


@pytest.mark.anyio
async def test_ha_research_rejects_evidence_when_index_head_changes(
    tmp_path: Path,
) -> None:
    registry, runtime = _runtime(tmp_path)
    runtime.index_generations = IndexGenerationStore(runtime.backend)
    storage_id = str(registry.get("docs", "default")["storage_id"])
    first = _publish_empty_index(
        runtime.index_generations, "default", storage_id, "build-one"
    )
    app = create_app(
        kb_registry=registry,
        index_jobs=_index_manager(runtime, registry, tmp_path, "node-a"),
        ha_runtime=runtime,
        close_state_runtime_on_shutdown=False,
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://node-a"
    ) as client:
        healthy = await client.post(
            "/v1/research-jobs",
            json={
                "kb_id": "docs",
                "objective": "Use one stable index generation",
                "section_titles": ["Evidence"],
            },
        )
        assert healthy.status_code == 201, healthy.text
        healthy_job_id = healthy.json()["job"]["job_id"]
        accepted = await client.post(f"/v1/research-jobs/{healthy_job_id}/start")
        assert accepted.status_code == 202, accepted.text
        assert app.state.research_execution_manager.dispatch_once() is True
        healthy_row = _wait_for_research_status(
            app.state.research_job_store, healthy_job_id, "evidence_ready"
        )
        assert healthy_row["sections"][0]["status"] == "completed"

        created = await client.post(
            "/v1/research-jobs",
            json={
                "kb_id": "docs",
                "objective": "Do not mix index generations",
                "section_titles": ["Evidence"],
            },
        )
        assert created.status_code == 201, created.text
        job_id = created.json()["job"]["job_id"]
        started = await client.post(f"/v1/research-jobs/{job_id}/start")
        assert started.status_code == 202, started.text
        assert (
            started.json()["job"]["evidence_provenance"]["index_generation"]
            == first["generation_id"]
        )
    _publish_empty_index(runtime.index_generations, "default", storage_id, "build-two")
    assert app.state.research_execution_manager.dispatch_once() is True
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        row = app.state.research_job_store.get(job_id)
        if row is not None and row["status"] == "failed":
            break
        time.sleep(0.01)
    else:
        raise AssertionError("stale research worker did not fail")
    row = app.state.research_job_store.get(job_id)
    assert row is not None and row["status"] == "failed"
    assert row["sections"][0]["evidence"] == []
    app.state.research_execution_manager.shutdown(wait=True)
    app.state.sync_manager.shutdown(wait=True)
    app.state.index_jobs.shutdown(wait=True)
    runtime.backend.close()


@pytest.mark.anyio
async def test_ha_research_commit_is_fenced_by_live_session_and_acl_epoch(
    tmp_path: Path,
) -> None:
    registry, runtime = _runtime(tmp_path)
    runtime.index_generations = IndexGenerationStore(runtime.backend)
    auth = AuthStore(None, backend=runtime.backend, scrypt_n=1 << 10)
    access = ResourceAccessStore(None, backend=runtime.backend)
    registration = auth.register(
        "research-owner@example.com",
        "correct horse battery staple",
        "Research Owner",
    )
    workspace_id = str(registration["workspace"]["workspace_id"])
    user_id = str(registration["user"]["user_id"])
    membership = auth.membership(workspace_id, user_id)
    assert membership is not None
    member_id = str(membership["member_id"])
    session_id = str(registration["session"]["session_id"])
    registry.create("secure", workspace_id, user_id)
    storage_id = str(registry.get("secure", workspace_id)["storage_id"])
    access.set_kb_policy(
        workspace_id,
        storage_id,
        user_id,
        "workspace",
        owner_membership_id=member_id,
    )
    _publish_empty_index(
        runtime.index_generations, workspace_id, storage_id, "secure-build"
    )
    app = create_app(
        kb_registry=registry,
        index_jobs=_index_manager(runtime, registry, tmp_path, "node-a"),
        auth_store=auth,
        resource_access_store=access,
        ha_runtime=runtime,
        close_state_runtime_on_shutdown=False,
    )
    headers = {"Authorization": f"Bearer {registration['access_token']}"}
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://node-a"
    ) as client:
        created = await client.post(
            "/v1/research-jobs",
            headers=headers,
            json={
                "kb_id": "secure",
                "objective": "Never persist evidence after revocation",
                "section_titles": ["Evidence"],
            },
        )
        assert created.status_code == 201, created.text
        job_id = created.json()["job"]["job_id"]
        started = await client.post(
            f"/v1/research-jobs/{job_id}/start", headers=headers
        )
        assert started.status_code == 202, started.text
        assert started.json()["job"]["evidence_provenance"]["acl_epoch"] > 0

    row = app.state.research_job_store.get(job_id)
    attempt_id = str(row["execution_id"])
    activated = app.state.research_job_store.activate_distributed_attempt(
        job_id, phase="evidence", attempt_id=attempt_id
    )
    lease_id = str(research_run_control(activated, "evidence")["lease_id"])
    _claimed, section = app.state.research_job_store.claim_next_section(
        job_id, attempt_id, lease_id=lease_id
    )
    assert section is not None
    assert auth.revoke_session(user_id, session_id)
    with pytest.raises(ResearchJobStateConflictError):
        app.state.research_job_store.complete_section(
            job_id,
            str(section["section_id"]),
            execution_id=attempt_id,
            evidence_status="missing",
            evidence=[],
            execution_metrics={},
            lease_id=lease_id,
        )
    persisted = app.state.research_job_store.get(job_id)
    assert persisted["sections"][0]["status"] == "running"
    assert persisted["sections"][0]["evidence"] == []
    app.state.research_execution_manager.shutdown(wait=True)
    app.state.sync_manager.shutdown(wait=True)
    app.state.index_jobs.shutdown(wait=True)
    auth.close()
    access.close()
    runtime.backend.close()


def test_ha_connector_rejects_process_local_store_injection(tmp_path: Path) -> None:
    registry, runtime = _runtime(tmp_path)
    local = ConnectionStore(str(tmp_path / "local.db"))
    with pytest.raises(ValueError, match="connection_store.*shared backend"):
        create_app(
            kb_registry=registry,
            index_jobs=_index_manager(runtime, registry, tmp_path, "node-a"),
            connection_store=local,
            ha_runtime=runtime,
            close_state_runtime_on_shutdown=False,
        )
    local.close()
    runtime.backend.close()


def test_ha_identity_rejects_process_local_store_injection(tmp_path: Path) -> None:
    registry, runtime = _runtime(tmp_path)
    auth = AuthStore(str(tmp_path / "local-auth.db"), scrypt_n=1 << 10)
    access = ResourceAccessStore(tmp_path / "local-access.db")
    manager = _index_manager(runtime, registry, tmp_path, "node-a")
    with pytest.raises(ValueError, match="authentication store.*runtime backend"):
        create_app(
            kb_registry=registry,
            index_jobs=manager,
            auth_store=auth,
            resource_access_store=access,
            ha_runtime=runtime,
            close_state_runtime_on_shutdown=False,
        )
    manager.shutdown(wait=True)
    auth.close()
    access.close()
    runtime.backend.close()


@pytest.mark.anyio
async def test_ha_account_routes_share_sessions_and_revocation_across_apps(
    tmp_path: Path,
) -> None:
    registry, runtime = _runtime(tmp_path)
    first_auth = AuthStore(None, backend=runtime.backend, scrypt_n=1 << 10)
    second_auth = AuthStore(None, backend=runtime.backend, scrypt_n=1 << 10)
    first_acl = ResourceAccessStore(None, backend=runtime.backend)
    second_acl = ResourceAccessStore(None, backend=runtime.backend)
    first = create_app(
        kb_registry=registry,
        index_jobs=_index_manager(runtime, registry, tmp_path, "node-a"),
        auth_store=first_auth,
        resource_access_store=first_acl,
        self_registration_enabled=True,
        ha_runtime=runtime,
        close_state_runtime_on_shutdown=False,
    )
    second_registry = DistributedKnowledgeBaseRegistry(
        runtime.backend, tmp_path / "cache-b"
    )
    second_runtime = SimpleNamespace(**vars(runtime))
    second_runtime.config = SimpleNamespace(
        worker_id="node-b", mutation_lease_seconds=30
    )
    second_runtime.api_mutation_coordinator = DistributedMutationCoordinator(
        runtime.backend, second_registry, owner_id="node-b", lease_seconds=30
    )
    second_runtime.connector_commits = DistributedConnectorCommitStore(
        runtime.backend,
        runtime.source_generations.object_store,
        second_runtime.api_mutation_coordinator,
    )
    second = create_app(
        kb_registry=second_registry,
        index_jobs=_index_manager(second_runtime, second_registry, tmp_path, "node-b"),
        auth_store=second_auth,
        resource_access_store=second_acl,
        self_registration_enabled=True,
        ha_runtime=second_runtime,
        close_state_runtime_on_shutdown=False,
    )

    async with AsyncClient(
        transport=ASGITransport(app=first), base_url="http://node-a"
    ) as client:
        created = await client.post(
            "/v1/auth/register",
            json={
                "email": "alice@example.com",
                "password": "correct horse battery staple",
                "display_name": "Alice",
            },
        )
        assert created.status_code == 201, created.text
        token = created.json()["access_token"]

    headers = {"Authorization": f"Bearer {token}"}
    async with AsyncClient(
        transport=ASGITransport(app=second), base_url="http://node-b"
    ) as client:
        current = await client.get("/v1/auth/me", headers=headers)
        assert current.status_code == 200, current.text
        assert current.json()["user"]["email"] == "alice@example.com"
        assert (
            await client.post("/v1/auth/logout", headers=headers)
        ).status_code == 204

    async with AsyncClient(
        transport=ASGITransport(app=first), base_url="http://node-a"
    ) as client:
        assert (await client.get("/v1/auth/me", headers=headers)).status_code == 401

    for app in (first, second):
        app.state.sync_manager.shutdown(wait=True)
        app.state.index_jobs.shutdown(wait=True)
    first_auth.close()
    second_auth.close()
    first_acl.close()
    second_acl.close()
    runtime.backend.close()
