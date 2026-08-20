from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from httpx import ASGITransport, AsyncClient

from cogdoc.api.app import create_app
from cogdoc.api.ingest import IndexJobManager, KnowledgeBaseRegistry
from cogdoc.connectors.connection_store import ConnectionStore
from cogdoc.connectors.credential_store import CredentialVault
from cogdoc.connectors.http_transport import HttpResponse
from cogdoc.connectors.oauth import (
    NotionOAuthAdapter,
    OAuthCoordinator,
    OAuthSessionStore,
    OAuthTokens,
)
from cogdoc.connectors.sync_store import ConnectorSyncStore
from cogdoc.service.source_artifact_store import SourceArtifactStore
from cogdoc.service.source_catalog import SourceCatalog
from cogdoc.service.kb_lifecycle import (
    LIFECYCLE_ACTIVE,
    LIFECYCLE_DELETING,
    shared_lifecycle_store,
)


@pytest.fixture
def anyio_backend():
    return "asyncio"


class _RefreshingNotionAdapter:
    provider = "notion"

    def __init__(self, redirect_uri: str) -> None:
        self.redirect_uri = redirect_uri
        self.refresh_tokens: list[str] = []

    def refresh(self, refresh_token: str) -> OAuthTokens:
        self.refresh_tokens.append(refresh_token)
        return OAuthTokens(
            access_token="fresh-access-marker",
            refresh_token="fresh-refresh-marker",
            token_type="bearer",
            expires_in=3_600,
            scopes=("read_content",),
            provider_metadata={},
        )


class _FailingTokenTransport:
    def __init__(self) -> None:
        self.calls = 0

    def request(self, method, url, *, headers=None, body=None):
        del method, headers, body
        self.calls += 1
        return HttpResponse(
            status=503,
            headers={},
            body=json.dumps(
                {"error": "temporarily_unavailable", "trace": "provider-secret-marker"}
            ).encode(),
            url=url,
        )


def _app(tmp_path, *, vault, oauth, redirect_uris):
    db_path = str(tmp_path / "state.db")

    def source_dir_for(kb_id):
        return str(tmp_path / "kb" / kb_id / "sources")

    registry = KnowledgeBaseRegistry(
        registry_path=str(tmp_path / "registry.json"),
        source_dir_for=source_dir_for,
    )
    jobs = IndexJobManager(
        ingest_fn=lambda *_: SimpleNamespace(
            document_count=0, chunk_count=0, ocr_summary={}
        ),
        source_dir_for=source_dir_for,
        kb_exists=registry.exists,
    )
    connections = ConnectionStore(db_path)
    sync_jobs = ConnectorSyncStore(db_path)
    catalog = SourceCatalog(db_path)
    app = create_app(
        kb_registry=registry,
        index_jobs=jobs,
        connection_store=connections,
        connector_sync_store=sync_jobs,
        source_catalog=catalog,
        source_artifact_store=SourceArtifactStore(tmp_path / "artifacts"),
        connector_credential_vault=vault,
        connector_oauth=oauth,
        connector_oauth_redirect_uris=redirect_uris,
    )
    return app, connections, sync_jobs, catalog


def _database_bytes(db_path) -> bytes:
    return b"".join(
        candidate.read_bytes()
        for candidate in (
            db_path,
            db_path.with_name(db_path.name + "-wal"),
            db_path.with_name(db_path.name + "-shm"),
        )
        if candidate.exists()
    )


def _plaintext_database_fields(db_path) -> str:
    with sqlite3.connect(db_path) as connection:
        credentials = connection.execute(
            "SELECT provider,credential_kind,label,subject,scopes_json,"
            "secret_fields_json,key_version,created_by,updated_by "
            "FROM connector_credentials"
        ).fetchall()
        connector_rows = connection.execute(
            "SELECT connector_type,name,config_json,secret_env_json,"
            "credential_fields_json FROM connector_connections"
        ).fetchall()
    return repr((credentials, connector_rows))


@pytest.mark.anyio
async def test_expired_oauth_refreshes_before_connector_build_without_leaking_secrets(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("cogdoc.api.app.configure_logging", lambda: None)
    db_path = tmp_path / "state.db"
    vault = CredentialVault(
        str(db_path),
        master_keys={"v1": b"e" * 32},
        active_key_version="v1",
    )
    sessions = OAuthSessionStore(str(db_path), vault)
    callback = "https://api.example/v1/auth/connector-oauth/callback/notion"
    adapter = _RefreshingNotionAdapter(callback)
    oauth = OAuthCoordinator(sessions, vault, {"notion": adapter})
    app, connections, sync_jobs, catalog = _app(
        tmp_path,
        vault=vault,
        oauth=oauth,
        redirect_uris={"notion": callback},
    )
    response_texts: list[str] = []

    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            response = await client.post("/v1/knowledge-bases", json={"kb_id": "docs"})
            assert response.status_code == 201
            response_texts.append(response.text)

            response = await client.post(
                "/v1/knowledge-bases/docs/connector-credentials",
                json={
                    "provider": "notion",
                    "credential_kind": "oauth",
                    "label": "Expired OAuth token",
                    "scopes": ["read_content"],
                    "secret_values": {
                        "token": "expired-access-marker",
                        "refresh_token": "expired-refresh-marker",
                        "access_token_expires_at": "1",
                    },
                },
            )
            assert response.status_code == 201, response.text
            response_texts.append(response.text)
            credential_id = response.json()["credential_id"]

            response = await client.post(
                "/v1/knowledge-bases/docs/connections",
                json={
                    "connector_type": "notion",
                    "name": "Product workspace",
                    "config": {},
                    "credential_id": credential_id,
                },
            )
            assert response.status_code == 201, response.text
            response_texts.append(response.text)
            connection_id = response.json()["connection_id"]

            private_connection = connections.get(
                connection_id, include_secret_refs=True
            )
            assert private_connection is not None
            credential = vault.get_metadata(
                credential_id, tenant_id="default", kb_id="docs"
            )
            assert credential is not None
            cancelled_job = sync_jobs.create(
                tenant_id="default",
                kb_id="docs",
                connection_id=connection_id,
                connector_type="notion",
                connection_revision=int(private_connection["revision"]),
                credential_id=credential_id,
                credential_revision=int(credential["revision"]),
            )
            sync_jobs.request_cancel(cancelled_job["job_id"])
            build_connection = dict(private_connection)
            build_connection.update(
                sync_job_id=cancelled_job["job_id"],
                sync_connection_revision=cancelled_job["connection_revision"],
                sync_credential_id=cancelled_job["credential_id"],
                sync_credential_revision=cancelled_job["credential_revision"],
            )
            with pytest.raises(RuntimeError, match="authority has been revoked"):
                app.state.sync_manager.connector_builder(build_connection)
            assert adapter.refresh_tokens == []

            frozen_job = sync_jobs.create(
                tenant_id="default",
                kb_id="docs",
                connection_id=connection_id,
                connector_type="notion",
                connection_revision=int(private_connection["revision"]),
                credential_id=credential_id,
                credential_revision=int(credential["revision"]),
            )
            build_connection.update(
                sync_job_id=frozen_job["job_id"],
                sync_connection_revision=frozen_job["connection_revision"],
                sync_credential_id=frozen_job["credential_id"],
                sync_credential_revision=frozen_job["credential_revision"],
            )
            lifecycle = shared_lifecycle_store()
            lifecycle.set("docs", LIFECYCLE_DELETING)
            try:
                with pytest.raises(RuntimeError, match="authority has been revoked"):
                    app.state.sync_manager.connector_builder(build_connection)
            finally:
                lifecycle.set("docs", LIFECYCLE_ACTIVE)
            assert adapter.refresh_tokens == []
            connector = app.state.sync_manager.connector_builder(build_connection)
            assert connector.headers["Authorization"] == "Bearer fresh-access-marker"
            assert adapter.refresh_tokens == ["expired-refresh-marker"]

            response = await client.get(
                "/v1/knowledge-bases/docs/connector-credentials"
            )
            assert response.status_code == 200
            response_texts.append(response.text)
            assert response.json()["credentials"][0]["revision"] == 2
            response = await client.get("/v1/knowledge-bases/docs/connections")
            assert response.status_code == 200
            response_texts.append(response.text)

    secret_markers = (
        "expired-access-marker",
        "expired-refresh-marker",
        "fresh-access-marker",
        "fresh-refresh-marker",
    )
    public_responses = "".join(response_texts)
    plaintext_fields = _plaintext_database_fields(db_path)
    database_bytes = _database_bytes(db_path)
    for marker in secret_markers:
        assert marker not in public_responses
        assert marker not in plaintext_fields
        assert marker.encode() not in database_bytes

    sessions.close()
    vault.close()
    connections.close()
    sync_jobs.close()
    catalog.close()


@pytest.mark.anyio
async def test_failed_provider_exchange_consumes_state_and_blocks_replay(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("cogdoc.api.app.configure_logging", lambda: None)
    db_path = tmp_path / "state.db"
    callback = "https://api.example/v1/auth/connector-oauth/callback/notion"
    vault = CredentialVault(
        str(db_path),
        master_keys={"v1": b"f" * 32},
        active_key_version="v1",
    )
    sessions = OAuthSessionStore(str(db_path), vault)
    provider = _FailingTokenTransport()
    oauth = OAuthCoordinator(
        sessions,
        vault,
        {
            "notion": NotionOAuthAdapter(
                client_id="client-id",
                client_secret="client-secret",
                redirect_uri=callback,
                transport=provider,
            )
        },
    )
    app, connections, sync_jobs, catalog = _app(
        tmp_path,
        vault=vault,
        oauth=oauth,
        redirect_uris={"notion": callback},
    )

    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            assert (
                await client.post("/v1/knowledge-bases", json={"kb_id": "docs"})
            ).status_code == 201
            started = await client.post(
                "/v1/knowledge-bases/docs/connector-oauth/authorize",
                json={"provider": "notion"},
            )
            assert started.status_code == 201, started.text
            state = parse_qs(urlsplit(started.json()["authorization_url"]).query)[
                "state"
            ][0]

            failed = await client.get(
                "/v1/auth/connector-oauth/callback/notion",
                params={"state": state, "code": "one-shot-provider-code"},
            )
            assert failed.status_code == 502, failed.text
            assert failed.json()["error_code"] == "OAUTH_PROVIDER_UNAVAILABLE"
            assert "provider-secret-marker" not in failed.text

            replay = await client.get(
                "/v1/auth/connector-oauth/callback/notion",
                params={"state": state, "code": "replayed-provider-code"},
            )
            assert replay.status_code == 400, replay.text
            assert replay.json()["error_code"] == "OAUTH_SESSION_INVALID"
            assert provider.calls == 1
            assert vault.list_metadata("default", "docs") == []

    database_bytes = _database_bytes(db_path)
    assert state.encode() not in database_bytes
    assert b"one-shot-provider-code" not in database_bytes
    assert b"replayed-provider-code" not in database_bytes
    assert b"provider-secret-marker" not in database_bytes

    sessions.close()
    vault.close()
    connections.close()
    sync_jobs.close()
    catalog.close()
