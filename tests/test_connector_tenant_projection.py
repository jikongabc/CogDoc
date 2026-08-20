from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from httpx import ASGITransport, AsyncClient, Response

from cogdoc.api.app import create_app
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
    def request(self, method, url, *, headers=None, body=None):
        del method, headers, body
        return HttpResponse(
            status=200,
            headers={},
            body=json.dumps(
                {
                    "access_token": "tenant-projection-access-token",
                    "refresh_token": "tenant-projection-refresh-token",
                    "token_type": "bearer",
                    "workspace_id": "workspace-a",
                }
            ).encode(),
            url=url,
        )


def _assert_logical_kb(response: Response, physical_kb_id: str) -> None:
    assert response.status_code < 300, response.text
    assert physical_kb_id not in response.text
    payload = response.json()
    projected_ids: list[str] = []

    def collect(value) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key == "kb_id":
                    projected_ids.append(child)
                collect(child)
        elif isinstance(value, list):
            for child in value:
                collect(child)

    collect(payload)
    assert projected_ids
    assert set(projected_ids) == {"docs"}


async def _wait_for_sync_job(
    client: AsyncClient,
    job_id: str,
    headers: dict[str, str],
    physical_kb_id: str,
) -> Response:
    deadline = time.monotonic() + 5
    response = None
    while time.monotonic() < deadline:
        response = await client.get(
            f"/v1/knowledge-bases/docs/sync-jobs/{job_id}", headers=headers
        )
        _assert_logical_kb(response, physical_kb_id)
        if response.json()["status"] not in {
            "pending",
            "running",
            "committing",
            "retry_wait",
        }:
            return response
        await asyncio.sleep(0.03)
    raise AssertionError(response.text if response is not None else "sync timed out")


@pytest.mark.anyio
async def test_connector_control_plane_never_exposes_physical_tenant_kb_id(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("cogdoc.api.app.configure_logging", lambda: None)
    db_path = str(tmp_path / "state.db")
    source_root = tmp_path / "provider"
    source_root.mkdir()
    (source_root / "guide.md").write_text(
        "# Tenant guide\n\nContent for the tenant projection sync test.",
        encoding="utf-8",
    )

    def source_dir_for(storage_id):
        return str(tmp_path / "kb" / storage_id / "sources")

    registry = KnowledgeBaseRegistry(
        registry_path=str(tmp_path / "registry.json"),
        source_dir_for=source_dir_for,
    )
    kb_record = registry.create("docs", "tenant-a", "admin-user")
    physical_kb_id = str(kb_record["storage_id"])
    assert physical_kb_id.startswith("t-")
    assert physical_kb_id != "docs"

    jobs = IndexJobManager(
        ingest_fn=lambda *_: SimpleNamespace(
            document_count=1, chunk_count=1, ocr_summary={}
        ),
        source_dir_for=source_dir_for,
        kb_exists=registry.exists,
    )
    connections = ConnectionStore(db_path)
    sync_jobs = ConnectorSyncStore(db_path)
    catalog = SourceCatalog(db_path)
    vault = CredentialVault(
        db_path,
        master_keys={"v1": b"p" * 32},
        active_key_version="v1",
    )
    oauth_sessions = OAuthSessionStore(db_path, vault)
    callback_uri = "https://api.example/v1/auth/connector-oauth/callback/notion"
    oauth = OAuthCoordinator(
        oauth_sessions,
        vault,
        {
            "notion": NotionOAuthAdapter(
                client_id="client-id",
                client_secret="client-secret",
                redirect_uri=callback_uri,
                transport=_TokenTransport(),
            )
        },
    )
    app = create_app(
        kb_registry=registry,
        index_jobs=jobs,
        connection_store=connections,
        connector_sync_store=sync_jobs,
        source_catalog=catalog,
        source_artifact_store=SourceArtifactStore(tmp_path / "artifacts"),
        connector_credential_vault=vault,
        connector_oauth=oauth,
        connector_oauth_redirect_uris={"notion": callback_uri},
        api_principals={
            "admin-key": {
                "tenant_id": "tenant-a",
                "subject_id": "admin-user",
                "role": "admin",
            }
        },
    )
    app.state.connector_local_allowed_roots = (str(source_root),)
    headers = {"Authorization": "Bearer admin-key"}

    try:
        async with app.router.lifespan_context(app):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://testserver"
            ) as client:
                credential = await client.post(
                    "/v1/knowledge-bases/docs/connector-credentials",
                    headers=headers,
                    json={
                        "provider": "notion",
                        "credential_kind": "static",
                        "label": "Tenant token",
                        "secret_values": {"token": "static-secret"},
                    },
                )
                _assert_logical_kb(credential, physical_kb_id)
                stored_credential = vault.get_metadata(
                    credential.json()["credential_id"],
                    tenant_id="tenant-a",
                    kb_id=physical_kb_id,
                )
                assert stored_credential is not None
                assert stored_credential["kb_id"] == physical_kb_id
                credential_list = await client.get(
                    "/v1/knowledge-bases/docs/connector-credentials",
                    headers=headers,
                )
                _assert_logical_kb(credential_list, physical_kb_id)
                audit = await client.get(
                    "/v1/knowledge-bases/docs/connector-credentials/audit/events",
                    headers=headers,
                )
                _assert_logical_kb(audit, physical_kb_id)

                connection = await client.post(
                    "/v1/knowledge-bases/docs/connections",
                    headers=headers,
                    json={
                        "connector_type": "local-directory",
                        "name": "Tenant docs",
                        "config": {"root": str(source_root)},
                    },
                )
                _assert_logical_kb(connection, physical_kb_id)
                connection_id = connection.json()["connection_id"]
                stored_connection = connections.get(connection_id)
                assert stored_connection is not None
                assert stored_connection["kb_id"] == physical_kb_id
                connection_list = await client.get(
                    "/v1/knowledge-bases/docs/connections", headers=headers
                )
                _assert_logical_kb(connection_list, physical_kb_id)

                started = await client.post(
                    f"/v1/knowledge-bases/docs/connections/{connection_id}/sync",
                    headers=headers,
                )
                _assert_logical_kb(started, physical_kb_id)
                stored_job = sync_jobs.get(started.json()["job_id"])
                assert stored_job is not None
                assert stored_job["kb_id"] == physical_kb_id
                completed = await _wait_for_sync_job(
                    client,
                    started.json()["job_id"],
                    headers,
                    physical_kb_id,
                )
                _assert_logical_kb(completed, physical_kb_id)
                assert completed.json()["status"] == "succeeded", completed.text
                sync_list = await client.get(
                    "/v1/knowledge-bases/docs/sync-jobs", headers=headers
                )
                _assert_logical_kb(sync_list, physical_kb_id)

                health = await client.get(
                    f"/v1/knowledge-bases/docs/connections/{connection_id}/health",
                    headers=headers,
                )
                _assert_logical_kb(health, physical_kb_id)
                health_list = await client.get(
                    "/v1/knowledge-bases/docs/connection-health", headers=headers
                )
                _assert_logical_kb(health_list, physical_kb_id)

                authorization = await client.post(
                    "/v1/knowledge-bases/docs/connector-oauth/authorize",
                    headers=headers,
                    json={"provider": "notion"},
                )
                assert authorization.status_code == 201, authorization.text
                assert physical_kb_id not in authorization.text
                state = parse_qs(
                    urlsplit(authorization.json()["authorization_url"]).query
                )["state"][0]
                callback = await client.get(
                    "/v1/auth/connector-oauth/callback/notion",
                    params={"state": state, "code": "provider-code"},
                )
                _assert_logical_kb(callback, physical_kb_id)
    finally:
        oauth_sessions.close()
        vault.close()
        connections.close()
        sync_jobs.close()
        catalog.close()
