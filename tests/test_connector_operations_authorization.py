from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from cogdoc.api.app import create_app
from cogdoc.api.ingest import IndexJobManager, KnowledgeBaseRegistry
from cogdoc.api.tenancy import Principal, Role
from cogdoc.connectors.connection_store import ConnectionStore
from cogdoc.connectors.credential_store import CredentialVault
from cogdoc.connectors.sync_store import ConnectorSyncStore
from cogdoc.service.source_artifact_store import SourceArtifactStore
from cogdoc.service.source_catalog import SourceCatalog


@pytest.fixture
def anyio_backend():
    return "asyncio"


class _UnexpectedOAuth:
    def __init__(self, credential_vault):
        self.credential_vault = credential_vault
        self.redirect_uris = {
            "notion": "https://api.example/v1/auth/connector-oauth/callback/notion"
        }

    def begin(self, **kwargs):
        raise AssertionError(f"unauthorized OAuth call reached coordinator: {kwargs}")

    def bind_authorization_checker(self, checker):
        self.authorization_checker = checker


@pytest.mark.anyio
async def test_control_plane_requires_manage_access_without_acl_store(
    tmp_path, monkeypatch
):
    """Route-local RBAC must not depend on ResourceAccessStore being enabled."""

    monkeypatch.setattr("cogdoc.api.app.configure_logging", lambda: None)
    db_path = str(tmp_path / "state.db")
    source_root = tmp_path / "provider"
    source_root.mkdir()

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
    vault = CredentialVault(
        db_path,
        master_keys={"v1": b"r" * 32},
        active_key_version="v1",
    )
    principals = {
        f"{role.value}-key": Principal.for_api_key(
            f"{role.value}-key",
            tenant_id="tenant-a",
            subject_id=f"{role.value}-user",
            role=role,
        )
        for role in (Role.ADMIN, Role.EDITOR, Role.VIEWER)
    }
    app = create_app(
        kb_registry=registry,
        index_jobs=jobs,
        connection_store=ConnectionStore(db_path),
        connector_sync_store=ConnectorSyncStore(db_path),
        source_catalog=SourceCatalog(db_path),
        source_artifact_store=SourceArtifactStore(tmp_path / "artifacts"),
        connector_credential_vault=vault,
        connector_oauth=_UnexpectedOAuth(vault),
        connector_oauth_redirect_uris={
            "notion": "https://api.example/v1/auth/connector-oauth/callback/notion"
        },
        api_principals=principals,
        resource_access_store=None,
    )
    app.state.connector_local_allowed_roots = (str(source_root),)
    headers = {
        role: {"Authorization": f"Bearer {role}-key"}
        for role in ("admin", "editor", "viewer")
    }

    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            assert (
                await client.post(
                    "/v1/knowledge-bases",
                    json={"kb_id": "docs"},
                    headers=headers["admin"],
                )
            ).status_code == 201
            other_tenant_root = tmp_path / "other-tenant-sources"
            other_tenant_root.mkdir()
            rejected_local = await client.post(
                "/v1/knowledge-bases/docs/connections",
                json={
                    "connector_type": "local-directory",
                    "name": "Cross-tenant local source",
                    "config": {"root": str(other_tenant_root)},
                },
                headers=headers["admin"],
            )
            assert rejected_local.status_code == 400
            assert "allowlist" in rejected_local.text
            rejected_symlinks = await client.post(
                "/v1/knowledge-bases/docs/connections",
                json={
                    "connector_type": "local-directory",
                    "name": "Symlink traversal",
                    "config": {
                        "root": str(source_root),
                        "follow_symlinks": True,
                    },
                },
                headers=headers["admin"],
            )
            assert rejected_symlinks.status_code == 400
            connection = await client.post(
                "/v1/knowledge-bases/docs/connections",
                json={
                    "connector_type": "local-directory",
                    "name": "Admin source",
                    "config": {"root": str(source_root)},
                },
                headers=headers["admin"],
            )
            assert connection.status_code == 201
            connection_id = connection.json()["connection_id"]

            marker = "viewer-visible-plaintext-token"
            rejected_url = await client.post(
                "/v1/knowledge-bases/docs/connections",
                json={
                    "connector_type": "url",
                    "name": "Unsafe signed URL",
                    "config": {
                        "urls": ["https://example.test/document?access_token=" + marker]
                    },
                },
                headers=headers["admin"],
            )
            assert rejected_url.status_code == 400

            # Reader-facing connection status remains readable.
            viewer_connections = await client.get(
                "/v1/knowledge-bases/docs/connections",
                headers=headers["viewer"],
            )
            assert viewer_connections.status_code == 200
            assert marker not in viewer_connections.text
            viewer_connection = viewer_connections.json()["connections"][0]
            assert viewer_connection["config"] == {}
            assert viewer_connection["credential_id"] is None
            admin_connections = await client.get(
                "/v1/knowledge-bases/docs/connections",
                headers=headers["admin"],
            )
            assert admin_connections.json()["connections"][0]["config"]["root"] == str(
                source_root
            )

            protected_calls = (
                (
                    "get",
                    "/v1/knowledge-bases/docs/connector-credentials",
                    None,
                ),
                ("get", "/v1/knowledge-bases/docs/source-catalog", None),
                (
                    "post",
                    "/v1/knowledge-bases/docs/connector-credentials",
                    {
                        "provider": "notion",
                        "label": "must not persist",
                        "secret_values": {"token": "must-not-persist"},
                    },
                ),
                (
                    "post",
                    "/v1/knowledge-bases/docs/connector-oauth/authorize",
                    {"provider": "notion"},
                ),
                (
                    "post",
                    f"/v1/knowledge-bases/docs/connections/{connection_id}/sync",
                    None,
                ),
            )
            for role in ("viewer", "editor"):
                for method, path, payload in protected_calls:
                    response = await client.request(
                        method,
                        path,
                        json=payload,
                        headers=headers[role],
                    )
                    assert response.status_code in {403, 404}, (
                        role,
                        method,
                        path,
                        response.text,
                    )

            assert (
                await client.get(
                    "/v1/knowledge-bases/docs/connector-credentials",
                    headers=headers["admin"],
                )
            ).status_code == 200

    assert b"must-not-persist" not in (tmp_path / "state.db").read_bytes()
    assert marker.encode() not in (tmp_path / "state.db").read_bytes()
