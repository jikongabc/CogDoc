from __future__ import annotations

import asyncio
import json
from threading import Event
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from httpx import ASGITransport, AsyncClient

from cogdoc.api.app import create_app
from cogdoc.api.connector_scope import (
    KBIncarnationChanged,
    capture_kb_epoch,
    guarded_kb_mutation,
)
from cogdoc.api.ingest import IndexJobManager, KnowledgeBaseRegistry
from cogdoc.connectors.connection_store import ConnectionStore
from cogdoc.connectors.credential_store import CredentialVault
from cogdoc.connectors.http_transport import HttpResponse
from cogdoc.connectors.oauth import (
    NotionOAuthAdapter,
    OAuthCoordinator,
    OAuthSessionStore,
)
from cogdoc.connectors.sync_store import ConnectorSyncStore
from cogdoc.service.source_artifact_store import SourceArtifactStore
from cogdoc.service.source_catalog import SourceCatalog


@pytest.fixture
def anyio_backend():
    return "asyncio"


class _TokenTransport:
    def __init__(self) -> None:
        self.calls = 0

    def request(self, method, url, *, headers=None, body=None):
        del method, headers, body
        self.calls += 1
        return HttpResponse(
            status=200,
            headers={},
            body=json.dumps(
                {
                    "access_token": "must-never-survive-kb-delete",
                    "token_type": "bearer",
                    "workspace_id": "old-workspace",
                }
            ).encode(),
            url=url,
        )


async def _wait_for_job(client: AsyncClient, job_id: str) -> dict:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        response = await client.get(f"/v1/knowledge-bases/docs/sync-jobs/{job_id}")
        if response.status_code == 200 and response.json()["status"] not in {
            "pending",
            "running",
            "committing",
            "retry_wait",
        }:
            return response.json()
        await asyncio.sleep(0.02)
    raise AssertionError("connector sync did not finish")


async def _wait_for_event(event: Event, timeout: float) -> bool:
    """Wait without relying on a bare to_thread completion wakeup."""

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not event.is_set():
        if loop.time() >= deadline:
            return False
        await asyncio.sleep(0.01)
    return True


@pytest.mark.anyio
async def test_kb_delete_erases_connector_capabilities_and_blocks_old_incarnation(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("cogdoc.api.app.configure_logging", lambda: None)
    monkeypatch.setattr(
        "cogdoc.api.routes.documents.delete_kb_index_transactional", lambda _kb: None
    )
    database = str(tmp_path / "state.db")
    provider_root = tmp_path / "provider"
    provider_root.mkdir()
    (provider_root / "private.md").write_text(
        "# Old private source\n\nThis must not cross a KB incarnation.",
        encoding="utf-8",
    )

    def source_dir_for(storage_id: str) -> str:
        return str(tmp_path / "knowledge-bases" / storage_id / "sources")

    registry = KnowledgeBaseRegistry(
        registry_path=str(tmp_path / "registry.json"),
        source_dir_for=source_dir_for,
    )
    index_jobs = IndexJobManager(
        ingest_fn=lambda *_: SimpleNamespace(
            document_count=1, chunk_count=1, ocr_summary={}
        ),
        source_dir_for=source_dir_for,
        kb_exists=registry.exists,
    )
    connections = ConnectionStore(database)
    sync_jobs = ConnectorSyncStore(database)
    catalog = SourceCatalog(database)
    artifacts = SourceArtifactStore(tmp_path / "artifacts")
    vault = CredentialVault(
        database,
        master_keys={"v1": b"k" * 32},
        active_key_version="v1",
    )
    sessions = OAuthSessionStore(database, vault)
    token_transport = _TokenTransport()
    callback_uri = "https://api.example/v1/auth/connector-oauth/callback/notion"
    oauth = OAuthCoordinator(
        sessions,
        vault,
        {
            "notion": NotionOAuthAdapter(
                client_id="client-id",
                client_secret="client-secret",
                redirect_uri=callback_uri,
                transport=token_transport,
            )
        },
    )
    app = create_app(
        kb_registry=registry,
        index_jobs=index_jobs,
        connection_store=connections,
        connector_sync_store=sync_jobs,
        source_catalog=catalog,
        source_artifact_store=artifacts,
        connector_credential_vault=vault,
        connector_oauth=oauth,
        derived_knowledge_index_clearer=lambda _storage_id: None,
        connector_oauth_redirect_uris={"notion": callback_uri},
    )

    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            created_kb = await client.post(
                "/v1/knowledge-bases", json={"kb_id": "docs"}
            )
            assert created_kb.status_code == 201, created_kb.text
            record = registry.resolve("docs")
            assert record is not None
            storage_id = str(record["storage_id"])
            old_epoch = capture_kb_epoch(storage_id)

            credential = await client.post(
                "/v1/knowledge-bases/docs/connector-credentials",
                json={
                    "provider": "notion",
                    "credential_kind": "static",
                    "label": "Old KB token",
                    "secret_values": {"token": "old-secret"},
                },
            )
            assert credential.status_code == 201, credential.text
            connection = await client.post(
                "/v1/knowledge-bases/docs/connections",
                json={
                    "connector_type": "local-directory",
                    "name": "Old source",
                    "config": {"root": str(provider_root)},
                },
            )
            assert connection.status_code == 201, connection.text
            connection_id = str(connection.json()["connection_id"])
            submitted = await client.post(
                f"/v1/knowledge-bases/docs/connections/{connection_id}/sync"
            )
            assert submitted.status_code == 202, submitted.text
            terminal = await _wait_for_job(client, str(submitted.json()["job_id"]))
            assert terminal["status"] == "succeeded"
            assert catalog.list_sources("default", storage_id)
            assert artifacts.usage("default", storage_id)["active_versions"] == 1

            authorization = await client.post(
                "/v1/knowledge-bases/docs/connector-oauth/authorize",
                json={"provider": "notion"},
            )
            assert authorization.status_code == 201, authorization.text
            state = parse_qs(urlsplit(authorization.json()["authorization_url"]).query)[
                "state"
            ][0]

            assert (
                await client.post("/v1/knowledge-bases", json={"kb_id": "other"})
            ).status_code == 201
            artifact_cleanup_started = Event()
            allow_artifact_cleanup = Event()
            original_delete_scope = artifacts.delete_scope

            def blocking_delete_scope(tenant_id, target_kb_id):
                if target_kb_id == storage_id:
                    artifact_cleanup_started.set()
                    if not allow_artifact_cleanup.wait(timeout=5):
                        raise TimeoutError("test did not release KB artifact cleanup")
                return original_delete_scope(tenant_id, target_kb_id)

            monkeypatch.setattr(artifacts, "delete_scope", blocking_delete_scope)
            deletion = asyncio.create_task(client.delete("/v1/knowledge-bases/docs"))
            assert await _wait_for_event(artifact_cleanup_started, 2)
            try:
                unrelated_credential = await asyncio.wait_for(
                    client.post(
                        "/v1/knowledge-bases/other/connector-credentials",
                        json={
                            "provider": "notion",
                            "credential_kind": "static",
                            "label": "Other KB token",
                            "secret_values": {"token": "other-secret"},
                        },
                    ),
                    timeout=1,
                )
                assert unrelated_credential.status_code == 201
            finally:
                allow_artifact_cleanup.set()
            deleted = await deletion
            monkeypatch.setattr(artifacts, "delete_scope", original_delete_scope)
            assert deleted.status_code == 204, deleted.text
            assert registry.resolve("docs") is None
            assert connections.list_entries("default", storage_id) == []
            assert sync_jobs.list_jobs("default", storage_id) == []
            assert catalog.list_sources("default", storage_id) == []
            assert artifacts.usage("default", storage_id) == {
                "active_bytes": 0,
                "active_versions": 0,
                "trash_bytes": 0,
                "trash_versions": 0,
            }
            assert vault.list_metadata("default", storage_id) == []
            assert sessions.internal_credential_ids("default", storage_id) == set()

            recreated = await client.post("/v1/knowledge-bases", json={"kb_id": "docs"})
            assert recreated.status_code == 201, recreated.text
            assert registry.resolve("docs")["storage_id"] == storage_id
            assert (
                await client.get("/v1/knowledge-bases/docs/connections")
            ).json() == {"connections": []}
            assert (
                await client.get("/v1/knowledge-bases/docs/connector-credentials")
            ).json() == {"credentials": []}
            assert (
                await client.get("/v1/knowledge-bases/docs/source-catalog")
            ).json() == {"sources": []}

            stale_callback = await client.get(
                "/v1/auth/connector-oauth/callback/notion",
                params={"state": state, "code": "old-code"},
            )
            assert stale_callback.status_code == 400
            assert stale_callback.json()["error_code"] == "OAUTH_SESSION_INVALID"
            assert token_transport.calls == 0

            with pytest.raises(KBIncarnationChanged):
                guarded_kb_mutation(
                    registry,
                    "default",
                    storage_id,
                    old_epoch,
                    connections.create,
                    tenant_id="default",
                    kb_id=storage_id,
                    connector_type="local-directory",
                    name="stale request",
                    config={"root": str(provider_root)},
                    secret_env={},
                    owner_id="default",
                )
            assert connections.list_entries("default", storage_id) == []
