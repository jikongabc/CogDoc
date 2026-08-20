from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from cogdoc.api.app import create_app
from cogdoc.api.ingest import IndexJobManager, KnowledgeBaseRegistry
from cogdoc.connectors.connection_store import ConnectionStore
from cogdoc.connectors.base import SyncCancelled
from cogdoc.connectors.credential_store import CredentialVault
from cogdoc.connectors.sync_store import ConnectorSyncStore
from cogdoc.service.source_catalog import SourceCatalog


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _app(tmp_path, vault):
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
    return create_app(
        kb_registry=registry,
        index_jobs=jobs,
        connection_store=ConnectionStore(db_path),
        connector_sync_store=ConnectorSyncStore(db_path),
        source_catalog=SourceCatalog(db_path),
        connector_credential_vault=vault,
    )


@pytest.mark.anyio
async def test_vault_api_never_returns_secret_and_connection_resolves_rotation(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("cogdoc.api.app.configure_logging", lambda: None)
    db_path = str(tmp_path / "state.db")
    vault = CredentialVault(
        db_path,
        master_keys={"v1": b"a" * 32},
        active_key_version="v1",
    )
    app = _app(tmp_path, vault)

    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            assert (
                await client.post("/v1/knowledge-bases", json={"kb_id": "docs"})
            ).status_code == 201
            created = await client.post(
                "/v1/knowledge-bases/docs/connector-credentials",
                json={
                    "provider": "notion",
                    "credential_kind": "static",
                    "label": "Notion team token",
                    "secret_values": {"token": "first-secret"},
                },
            )
            assert created.status_code == 201, created.text
            credential = created.json()
            assert credential["secret_fields"] == ["token"]
            assert "secret_values" not in credential
            assert "first-secret" not in created.text

            connection_response = await client.post(
                "/v1/knowledge-bases/docs/connections",
                json={
                    "connector_type": "notion",
                    "name": "Product notes",
                    "config": {},
                    "credential_id": credential["credential_id"],
                },
            )
            assert connection_response.status_code == 201, connection_response.text
            connection = connection_response.json()
            assert connection["credential_source"] == "vault"
            assert connection["secret_fields"] == ["token"]
            old_job = app.state.connector_sync_store.create(
                tenant_id="default",
                kb_id="docs",
                connection_id=connection["connection_id"],
                connector_type="notion",
                connection_revision=connection["revision"],
            )
            _, old_token = app.state.connector_sync_store.acquire(
                old_job["job_id"], lease_seconds=60
            )

            rotated = await client.patch(
                "/v1/knowledge-bases/docs/connector-credentials/"
                + credential["credential_id"],
                json={
                    "secret_values": {"token": "second-secret"},
                    "expected_revision": 1,
                },
            )
            assert rotated.status_code == 200, rotated.text
            assert rotated.json()["revision"] == 2
            assert "second-secret" not in rotated.text
            current_connection = app.state.connection_store.get(
                connection["connection_id"]
            )
            assert current_connection["revision"] == connection["revision"] + 1
            with pytest.raises(SyncCancelled, match="connection was revoked"):
                app.state.sync_manager.runtime._guard(
                    old_job["job_id"],
                    old_token,
                    app.state.sync_manager.runtime._monotonic() + 60,
                )
            app.state.connector_sync_store.mark_cancelled(
                old_job["job_id"], old_token
            )
            resolved = vault.get_for_use(
                credential["credential_id"],
                tenant_id="default",
                kb_id="docs",
                connection_id=None,
                actor_id="test",
            )
            assert resolved == {"token": "second-secret"}

            blocked = await client.delete(
                "/v1/knowledge-bases/docs/connector-credentials/"
                + credential["credential_id"]
            )
            assert blocked.status_code == 409
            events = await client.get(
                "/v1/knowledge-bases/docs/connector-credentials/audit/events"
            )
            assert events.status_code == 200
            assert {row["action"] for row in events.json()["events"]} >= {
                "create",
                "rotate",
                "use",
            }

    assert b"first-secret" not in (tmp_path / "state.db").read_bytes()
    assert b"second-secret" not in (tmp_path / "state.db").read_bytes()


@pytest.mark.anyio
async def test_vault_api_fails_closed_when_master_key_is_not_configured(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("cogdoc.api.app.configure_logging", lambda: None)
    app = _app(tmp_path, None)
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            assert (
                await client.post("/v1/knowledge-bases", json={"kb_id": "docs"})
            ).status_code == 201
            response = await client.get(
                "/v1/knowledge-bases/docs/connector-credentials"
            )
            assert response.status_code == 503
            assert response.json()["error_code"] == "CREDENTIAL_UNAVAILABLE"


@pytest.mark.anyio
async def test_internal_oauth_session_credentials_are_not_public_resources(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("cogdoc.api.app.configure_logging", lambda: None)
    db_path = str(tmp_path / "state.db")
    vault = CredentialVault(
        db_path,
        master_keys={"v1": b"i" * 32},
        active_key_version="v1",
    )
    internal = vault.create(
        tenant_id="default",
        kb_id="docs",
        connection_id=None,
        provider="notion",
        credential_kind="oauth-session",
        label="internal verifier",
        secret_values={"code_verifier": "v" * 64},
        actor_id="system",
    )
    credential_id = str(internal["credential_id"])
    app = _app(tmp_path, vault)
    # Make the refresh route available; it must reject the internal row before
    # attempting any coordinator operation.
    app.state.connector_oauth = object()

    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            assert (
                await client.post("/v1/knowledge-bases", json={"kb_id": "docs"})
            ).status_code == 201
            listed = await client.get("/v1/knowledge-bases/docs/connector-credentials")
            assert listed.status_code == 200
            assert listed.json()["credentials"] == []

            rejected_create = await client.post(
                "/v1/knowledge-bases/docs/connector-credentials",
                json={
                    "provider": "notion",
                    "credential_kind": "oauth-session",
                    "label": "not allowed",
                    "secret_values": {"code_verifier": "x" * 64},
                },
            )
            assert rejected_create.status_code == 400
            assert rejected_create.json()["error_code"] == "BAD_REQUEST"

            rotated = await client.patch(
                f"/v1/knowledge-bases/docs/connector-credentials/{credential_id}",
                json={"secret_values": {"code_verifier": "z" * 64}},
            )
            refreshed = await client.post(
                "/v1/knowledge-bases/docs/connector-credentials/"
                f"{credential_id}/refresh"
            )
            deleted = await client.delete(
                f"/v1/knowledge-bases/docs/connector-credentials/{credential_id}"
            )
            assert rotated.status_code == 404
            assert refreshed.status_code == 404
            assert deleted.status_code == 404

            audit = await client.get(
                "/v1/knowledge-bases/docs/connector-credentials/audit/events"
            )
            assert audit.status_code == 200
            assert audit.json()["events"] == []

    assert (
        vault.get_metadata(credential_id, tenant_id="default", kb_id="docs") is not None
    )
