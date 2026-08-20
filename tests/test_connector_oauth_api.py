import asyncio
import json
import sqlite3
from threading import Event
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from httpx import ASGITransport, AsyncClient

from cogdoc.api.app import create_app
from cogdoc.api.auth_store import AuthStore
from cogdoc.api.ingest import IndexJobManager, KnowledgeBaseRegistry
from cogdoc.api.resource_access import ResourceAccessStore
from cogdoc.api.tenancy import Role
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


async def _wait_for_event(event: Event, timeout: float) -> bool:
    """Wait without relying on a bare to_thread completion wakeup."""

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not event.is_set():
        if loop.time() >= deadline:
            return False
        await asyncio.sleep(0.01)
    return True


@pytest.fixture
def anyio_backend():
    return "asyncio"


class _TokenTransport:
    def __init__(self):
        self.calls = 0

    def request(self, method, url, *, headers=None, body=None):
        del method, headers, body
        self.calls += 1
        return HttpResponse(
            status=200,
            headers={},
            body=json.dumps(
                {
                    "access_token": "provider-access-token",
                    "refresh_token": "provider-refresh-token",
                    "token_type": "bearer",
                    "workspace_id": "workspace-1",
                }
            ).encode(),
            url=url,
        )


def test_create_app_rejects_missing_or_mismatched_injected_oauth_vault(tmp_path):
    coordinator_vault = CredentialVault(
        str(tmp_path / "coordinator.db"),
        master_keys={"v1": b"c" * 32},
        active_key_version="v1",
    )
    other_vault = CredentialVault(
        str(tmp_path / "other.db"),
        master_keys={"v1": b"d" * 32},
        active_key_version="v1",
    )
    coordinator = SimpleNamespace(credential_vault=coordinator_vault)
    with pytest.raises(ValueError, match="requires a credential vault"):
        create_app(connector_oauth=coordinator)
    with pytest.raises(ValueError, match="must share the app credential vault"):
        create_app(
            connector_oauth=coordinator,
            connector_credential_vault=other_vault,
        )
    coordinator_vault.close()
    other_vault.close()


def test_startup_reconciles_crash_after_pending_connection_binding(tmp_path):
    db_path = str(tmp_path / "state.db")
    connections = ConnectionStore(db_path)
    original = connections.create(
        tenant_id="default",
        kb_id="docs",
        connector_type="notion",
        name="Notion",
        config={},
        secret_env={"token": "NOTION_TOKEN"},
        owner_id="owner",
    )
    vault = CredentialVault(
        db_path,
        master_keys={"v1": b"p" * 32},
        active_key_version="v1",
    )
    pending = vault.create(
        tenant_id="default",
        kb_id="docs",
        connection_id=original["connection_id"],
        provider="notion",
        credential_kind="oauth",
        label="pending callback",
        secret_values={"token": "new-token"},
        actor_id="owner",
        pending_activation=True,
    )
    vault.prepare_binding(
        pending["credential_id"],
        tenant_id="default",
        kb_id="docs",
        connection_id=original["connection_id"],
        expected_credential_revision=1,
        expected_connection_revision=1,
        previous_credential_id=None,
        previous_credential_fields=(),
        previous_secret_env={"token": "NOTION_TOKEN"},
    )
    connections.set_credential(
        original["connection_id"],
        pending["credential_id"],
        pending["secret_fields"],
        expected_revision=1,
    )
    connections.close()
    vault.close()

    reopened_connections = ConnectionStore(db_path)
    reopened_vault = CredentialVault(
        db_path,
        master_keys={"v1": b"p" * 32},
        active_key_version="v1",
    )
    app = create_app(
        connection_store=reopened_connections,
        connector_sync_store=ConnectorSyncStore(db_path),
        source_catalog=SourceCatalog(db_path),
        source_artifact_store=SourceArtifactStore(tmp_path / "artifacts"),
        connector_credential_vault=reopened_vault,
    )
    assert app.state.reconcile_connector_oauth_bindings() == 1
    restored = reopened_connections.get(
        original["connection_id"], include_secret_refs=True
    )
    assert restored is not None
    assert restored["credential_id"] is None
    assert restored["credential_source"] == "environment"
    assert restored["secret_env"] == {"token": "NOTION_TOKEN"}
    assert reopened_vault.pending_bindings() == []
    assert reopened_vault.get_metadata(
        pending["credential_id"],
        tenant_id="default",
        kb_id="docs",
        include_inactive=True,
    ) is None
    reopened_connections.close()
    reopened_vault.close()


@pytest.mark.anyio
async def test_oauth_start_public_callback_and_replay_are_end_to_end_scoped(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("cogdoc.api.app.configure_logging", lambda: None)
    db_path = str(tmp_path / "state.db")
    callback = "https://api.example/v1/auth/connector-oauth/callback/notion"
    vault = CredentialVault(
        db_path,
        master_keys={"v1": b"o" * 32},
        active_key_version="v1",
    )
    sessions = OAuthSessionStore(db_path, vault)
    transport = _TokenTransport()
    coordinator = OAuthCoordinator(
        sessions,
        vault,
        {
            "notion": NotionOAuthAdapter(
                client_id="client-id",
                client_secret="client-secret",
                redirect_uri=callback,
                transport=transport,
            )
        },
    )
    stale = sessions.create(
        provider="notion",
        tenant_id="default",
        kb_id="docs",
        connection_id=None,
        user_id="stale-user",
        redirect_uri=callback,
    )
    stale_verifier_id = next(iter(sessions.internal_credential_ids("default", "docs")))
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE connector_oauth_sessions SET expires_at=0 WHERE session_id=?",
            (stale.session_id,),
        )

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
    app = create_app(
        kb_registry=registry,
        index_jobs=jobs,
        connection_store=ConnectionStore(db_path),
        connector_sync_store=ConnectorSyncStore(db_path),
        source_catalog=SourceCatalog(db_path),
        source_artifact_store=SourceArtifactStore(tmp_path / "artifacts"),
        connector_credential_vault=vault,
        connector_oauth=coordinator,
        connector_oauth_redirect_uris={"notion": callback},
        api_keys={"admin-key"},
    )
    auth = {"Authorization": "Bearer admin-key"}

    async with app.router.lifespan_context(app):
        assert (
            vault.get_metadata(stale_verifier_id, tenant_id="default", kb_id="docs")
            is None
        )
        assert sessions.internal_credential_ids("default", "docs") == set()
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            assert (
                await client.post(
                    "/v1/knowledge-bases",
                    json={"kb_id": "docs"},
                    headers=auth,
                )
            ).status_code == 201
            started = await client.post(
                "/v1/knowledge-bases/docs/connector-oauth/authorize",
                json={"provider": "notion"},
                headers=auth,
            )
            assert started.status_code == 201, started.text
            start = started.json()
            assert start["redirect_uri"] == callback
            assert started.headers["cache-control"] == "no-store"
            state = parse_qs(urlsplit(start["authorization_url"]).query)["state"][0]

            # Provider redirects do not carry CogDoc API credentials. The
            # high-entropy, one-shot state is the callback authority.
            completed = await client.get(
                "/v1/auth/connector-oauth/callback/notion",
                params={"state": state, "code": "provider-code"},
            )
            assert completed.status_code == 200, completed.text
            credential_id = completed.json()["credential_id"]
            assert completed.headers["cache-control"] == "no-store"
            assert "provider-access-token" not in completed.text
            assert vault.get_metadata(credential_id, tenant_id="default", kb_id="docs")[
                "secret_fields"
            ] == ["refresh_token", "token"]

            created = await client.post(
                "/v1/knowledge-bases/docs/connections",
                json={
                    "connector_type": "notion",
                    "name": "Notion OAuth",
                    "config": {},
                    "credential_id": credential_id,
                },
                headers=auth,
            )
            assert created.status_code == 201, created.text
            connection = created.json()
            assert connection["credential_source"] == "vault"

            refresh_started = Event()
            allow_refresh = Event()
            notion_adapter = coordinator._adapters["notion"]
            original_refresh = notion_adapter.refresh

            def blocking_refresh(refresh_token):
                refresh_started.set()
                if not allow_refresh.wait(timeout=5):
                    raise TimeoutError("test did not release OAuth refresh")
                return original_refresh(refresh_token)

            monkeypatch.setattr(notion_adapter, "refresh", blocking_refresh)
            refresh_request = asyncio.create_task(
                client.post(
                    "/v1/knowledge-bases/docs/connector-credentials/"
                    f"{credential_id}/refresh",
                    params={"expected_revision": 1},
                    headers=auth,
                )
            )
            assert await _wait_for_event(refresh_started, 2)
            # Provider I/O must not hold the global cross-store reference lock;
            # unrelated tenants can continue credential mutations.
            await asyncio.wait_for(
                app.state.connector_credential_reference_lock.acquire(),
                timeout=1,
            )
            app.state.connector_credential_reference_lock.release()
            allow_refresh.set()
            refreshed = await refresh_request
            monkeypatch.setattr(notion_adapter, "refresh", original_refresh)
            assert refreshed.status_code == 200, refreshed.text
            assert refreshed.json()["revision"] == 2
            assert "provider-access-token" not in refreshed.text

            bound_start = await client.post(
                "/v1/knowledge-bases/docs/connector-oauth/authorize",
                json={
                    "provider": "notion",
                    "connection_id": connection["connection_id"],
                },
                headers=auth,
            )
            assert bound_start.status_code == 201
            bound_state = parse_qs(
                urlsplit(bound_start.json()["authorization_url"]).query
            )["state"][0]
            bound = await client.get(
                "/v1/auth/connector-oauth/callback/notion",
                params={"state": bound_state, "code": "bound-provider-code"},
            )
            assert bound.status_code == 200, bound.text
            credential_id = bound.json()["credential_id"]
            assert (
                app.state.connection_store.get(connection["connection_id"])[
                    "credential_id"
                ]
                == credential_id
            )
            assert vault.get_metadata(
                credential_id, tenant_id="default", kb_id="docs"
            ) is not None

            replay = await client.get(
                "/v1/auth/connector-oauth/callback/notion",
                params={"state": state, "code": "provider-code"},
            )
            assert replay.status_code == 400
            assert replay.json()["error_code"] == "OAUTH_SESSION_INVALID"

            cancelled_start = await client.post(
                "/v1/knowledge-bases/docs/connector-oauth/authorize",
                json={"provider": "notion"},
                headers=auth,
            )
            assert cancelled_start.status_code == 201
            cancelled_state = parse_qs(
                urlsplit(cancelled_start.json()["authorization_url"]).query
            )["state"][0]
            verifier = next(
                row
                for row in vault.list_metadata("default", "docs")
                if row["credential_kind"] == "oauth-session"
            )
            verifier_id = str(verifier["credential_id"])
            provider_error = await client.get(
                "/v1/auth/connector-oauth/callback/notion",
                params={"state": cancelled_state, "error": "access_denied"},
            )
            assert provider_error.status_code == 400
            assert provider_error.json()["error_code"] == "OAUTH_SESSION_INVALID"
            assert (
                vault.get_metadata(verifier_id, tenant_id="default", kb_id="docs")
                is None
            )

            credentials = await client.get(
                "/v1/knowledge-bases/docs/connector-credentials", headers=auth
            )
            assert credentials.status_code == 200
            visible_credential_ids = {
                row["credential_id"] for row in credentials.json()["credentials"]
            }
            assert credential_id in visible_credential_ids
            assert verifier_id not in visible_credential_ids
            audit = await client.get(
                "/v1/knowledge-bases/docs/connector-credentials/audit/events",
                headers=auth,
            )
            assert audit.status_code == 200
            assert verifier_id not in {
                row["credential_id"] for row in audit.json()["events"]
            }
            cancelled_replay = await client.get(
                "/v1/auth/connector-oauth/callback/notion",
                params={"state": cancelled_state, "code": "provider-code"},
            )
            assert cancelled_replay.status_code == 400
            assert transport.calls == 3

            raced_start = await client.post(
                "/v1/knowledge-bases/docs/connector-oauth/authorize",
                json={
                    "provider": "notion",
                    "connection_id": connection["connection_id"],
                },
                headers=auth,
            )
            assert raced_start.status_code == 201
            raced_state = parse_qs(
                urlsplit(raced_start.json()["authorization_url"]).query
            )["state"][0]
            known_ids = {
                str(item["credential_id"])
                for item in vault.list_metadata("default", "docs")
            }
            original_projection = registry.get_by_storage_id
            deleted_during_projection: list[str] = []

            def delete_before_projection(storage_id):
                # Callback credentials are deliberately hidden while
                # pending. Reach through the test database to force the
                # exact process-race deletion window.
                with sqlite3.connect(db_path) as connection_db:
                    issued_row = connection_db.execute(
                        "SELECT credential_id FROM connector_credentials "
                        "WHERE credential_kind='oauth' AND lifecycle='pending' "
                        "ORDER BY created_at DESC LIMIT 1"
                    ).fetchone()
                if issued_row is not None:
                    issued_id = str(issued_row[0])
                    assert issued_id not in known_ids
                    assert vault.delete(
                        issued_id,
                        tenant_id="default",
                        kb_id="docs",
                        connection_id=connection["connection_id"],
                        actor_id="race-injector",
                    )
                    deleted_during_projection.append(issued_id)
                return original_projection(storage_id)

            monkeypatch.setattr(registry, "get_by_storage_id", delete_before_projection)
            raced = await client.get(
                "/v1/auth/connector-oauth/callback/notion",
                params={"state": raced_state, "code": "provider-code"},
            )
            monkeypatch.setattr(registry, "get_by_storage_id", original_projection)
            assert raced.status_code == 409
            assert raced.json()["error_code"] == "OAUTH_SESSION_INVALID"
            assert len(deleted_during_projection) == 1
            persisted_connection = app.state.connection_store.get(
                connection["connection_id"]
            )
            assert persisted_connection["credential_id"] == credential_id
            assert transport.calls == 4

            rotated_start = await client.post(
                "/v1/knowledge-bases/docs/connector-oauth/authorize",
                json={
                    "provider": "notion",
                    "connection_id": connection["connection_id"],
                },
                headers=auth,
            )
            assert rotated_start.status_code == 201
            rotated_state = parse_qs(
                urlsplit(rotated_start.json()["authorization_url"]).query
            )["state"][0]
            known_ids = {
                str(item["credential_id"])
                for item in vault.list_metadata("default", "docs")
            }
            rotated_during_projection: list[str] = []

            def rotate_before_projection(storage_id):
                with sqlite3.connect(db_path) as connection_db:
                    issued_row = connection_db.execute(
                        "SELECT credential_id FROM connector_credentials "
                        "WHERE credential_kind='oauth' AND lifecycle='pending' "
                        "ORDER BY created_at DESC LIMIT 1"
                    ).fetchone()
                if issued_row is not None and not rotated_during_projection:
                    issued_id = str(issued_row[0])
                    assert issued_id not in known_ids
                    issued = vault.get_metadata(
                        issued_id,
                        tenant_id="default",
                        kb_id="docs",
                        include_inactive=True,
                    )
                    assert issued is not None
                    vault.activate(
                        issued_id,
                        tenant_id="default",
                        kb_id="docs",
                        connection_id=connection["connection_id"],
                        actor_id="concurrent-admin",
                        expected_revision=1,
                    )
                    vault.rotate(
                        issued_id,
                        tenant_id="default",
                        kb_id="docs",
                        connection_id=connection["connection_id"],
                        actor_id="concurrent-admin",
                        secret_values={
                            field: f"concurrent-{field}"
                            for field in issued["secret_fields"]
                        },
                        expected_revision=1,
                    )
                    rotated_during_projection.append(issued_id)
                return original_projection(storage_id)

            monkeypatch.setattr(registry, "get_by_storage_id", rotate_before_projection)
            rotated = await client.get(
                "/v1/auth/connector-oauth/callback/notion",
                params={"state": rotated_state, "code": "provider-code"},
            )
            monkeypatch.setattr(registry, "get_by_storage_id", original_projection)
            assert rotated.status_code == 409
            assert len(rotated_during_projection) == 1
            concurrently_rotated = vault.get_metadata(
                rotated_during_projection[0], tenant_id="default", kb_id="docs"
            )
            assert concurrently_rotated is not None
            assert concurrently_rotated["revision"] == 2
            assert (
                app.state.connection_store.get(connection["connection_id"])[
                    "credential_id"
                ]
                == credential_id
            )
            assert transport.calls == 5

    database_bytes = b"".join(
        path.read_bytes()
        for path in (
            tmp_path / "state.db",
            tmp_path / "state.db-wal",
            tmp_path / "state.db-shm",
        )
        if path.exists()
    )
    assert b"provider-access-token" not in database_bytes
    assert b"provider-refresh-token" not in database_bytes


@pytest.mark.anyio
async def test_oauth_callback_revalidates_membership_and_connection_revision(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("cogdoc.api.app.configure_logging", lambda: None)
    db_path = str(tmp_path / "state.db")
    callback = "https://api.example/v1/auth/connector-oauth/callback/notion"
    auth_store = AuthStore(str(tmp_path / "accounts.db"), scrypt_n=1 << 10)
    access_store = ResourceAccessStore(tmp_path / "access.db")
    owner = auth_store.register(
        "owner@example.com", "correct horse battery staple", "Owner", "Workspace"
    )
    admin = auth_store.register(
        "admin@example.com", "correct horse battery staple", "Admin", "Personal"
    )
    workspace_id = str(owner["workspace"]["workspace_id"])
    owner_id = str(owner["user"]["user_id"])
    admin_id = str(admin["user"]["user_id"])
    auth_store.add_member(workspace_id, admin_id, Role.ADMIN, owner_id)
    vault = CredentialVault(
        db_path,
        master_keys={"v1": b"m" * 32},
        active_key_version="v1",
    )
    sessions = OAuthSessionStore(db_path, vault)
    provider = _TokenTransport()
    coordinator = OAuthCoordinator(
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

    def source_dir_for(storage_id):
        return str(tmp_path / "kb" / storage_id / "sources")

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
        connector_oauth=coordinator,
        connector_oauth_redirect_uris={"notion": callback},
        auth_store=auth_store,
        resource_access_store=access_store,
    )
    owner_headers = {
        "Authorization": f"Bearer {owner['access_token']}",
        "X-CogDoc-Workspace": workspace_id,
    }
    admin_headers = {
        "Authorization": f"Bearer {admin['access_token']}",
        "X-CogDoc-Workspace": workspace_id,
    }

    async def start_state(client, connection_id):
        result = await client.post(
            "/v1/knowledge-bases/docs/connector-oauth/authorize",
            json={"provider": "notion", "connection_id": connection_id},
            headers=admin_headers,
        )
        assert result.status_code == 201, result.text
        return parse_qs(urlsplit(result.json()["authorization_url"]).query)["state"][0]

    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            created_kb = await client.post(
                "/v1/knowledge-bases",
                json={"kb_id": "docs", "access_policy": "workspace"},
                headers=owner_headers,
            )
            assert created_kb.status_code == 201, created_kb.text
            storage_id = str(registry.resolve("docs", workspace_id)["storage_id"])
            credential = await client.post(
                "/v1/knowledge-bases/docs/connector-credentials",
                json={
                    "provider": "notion",
                    "credential_kind": "api-token",
                    "label": "Original",
                    "secret_values": {"token": "original-token"},
                },
                headers=owner_headers,
            )
            assert credential.status_code == 201, credential.text
            original_credential_id = credential.json()["credential_id"]
            connection = await client.post(
                "/v1/knowledge-bases/docs/connections",
                json={
                    "connector_type": "notion",
                    "name": "Notion",
                    "config": {},
                    "credential_id": original_credential_id,
                },
                headers=owner_headers,
            )
            assert connection.status_code == 201, connection.text
            connection_id = connection.json()["connection_id"]

            removed_state = await start_state(client, connection_id)
            assert auth_store.remove_member(workspace_id, admin_id, owner_id)
            removed = await client.get(
                "/v1/auth/connector-oauth/callback/notion",
                params={"state": removed_state, "code": "code"},
            )
            assert removed.status_code == 400
            assert provider.calls == 0

            auth_store.add_member(workspace_id, admin_id, Role.ADMIN, owner_id)
            reincarnated_state = await start_state(client, connection_id)
            assert auth_store.remove_member(workspace_id, admin_id, owner_id)
            auth_store.add_member(workspace_id, admin_id, Role.ADMIN, owner_id)
            reincarnated = await client.get(
                "/v1/auth/connector-oauth/callback/notion",
                params={"state": reincarnated_state, "code": "code"},
            )
            assert reincarnated.status_code == 400
            assert provider.calls == 0

            demoted_state = await start_state(client, connection_id)
            auth_store.update_member_role(workspace_id, admin_id, Role.VIEWER, owner_id)
            demoted = await client.get(
                "/v1/auth/connector-oauth/callback/notion",
                params={"state": demoted_state, "code": "code"},
            )
            assert demoted.status_code == 400
            assert provider.calls == 0

            auth_store.update_member_role(workspace_id, admin_id, Role.ADMIN, owner_id)
            stale_connection_state = await start_state(client, connection_id)
            connections.set_enabled(connection_id, False)
            connections.set_enabled(connection_id, True)
            stale_connection = await client.get(
                "/v1/auth/connector-oauth/callback/notion",
                params={"state": stale_connection_state, "code": "code"},
            )
            assert stale_connection.status_code == 400
            assert provider.calls == 0
            assert (
                connections.get(connection_id)["credential_id"]
                == original_credential_id
            )
            assert {
                item["credential_id"]
                for item in vault.list_metadata(workspace_id, storage_id)
                if item["credential_kind"] != "oauth-session"
            } == {original_credential_id}

            credential_race_state = await start_state(client, connection_id)
            winner = vault.create(
                tenant_id=workspace_id,
                kb_id=storage_id,
                connection_id=connection_id,
                provider="notion",
                credential_kind="api-token",
                label="Concurrent winner",
                secret_values={"token": "winner-token"},
                actor_id=owner_id,
            )
            original_set_credential = connections.set_credential
            credential_race_callback_ids: list[str] = []

            def bind_winner_before_callback(
                target_connection_id,
                callback_credential_id,
                credential_fields,
                *,
                expected_revision=None,
            ):
                if not credential_race_callback_ids:
                    credential_race_callback_ids.append(callback_credential_id)
                    original_set_credential(
                        target_connection_id,
                        str(winner["credential_id"]),
                        winner["secret_fields"],
                        expected_revision=expected_revision,
                    )
                return original_set_credential(
                    target_connection_id,
                    callback_credential_id,
                    credential_fields,
                    expected_revision=expected_revision,
                )

            monkeypatch.setattr(
                connections, "set_credential", bind_winner_before_callback
            )
            credential_race = await client.get(
                "/v1/auth/connector-oauth/callback/notion",
                params={"state": credential_race_state, "code": "code"},
            )
            monkeypatch.setattr(connections, "set_credential", original_set_credential)
            assert credential_race.status_code == 409
            assert len(credential_race_callback_ids) == 1
            assert (
                connections.get(connection_id)["credential_id"]
                == winner["credential_id"]
            )
            assert (
                vault.get_metadata(
                    credential_race_callback_ids[0],
                    tenant_id=workspace_id,
                    kb_id=storage_id,
                )
                is None
            )
            assert (
                vault.get_metadata(
                    str(winner["credential_id"]),
                    tenant_id=workspace_id,
                    kb_id=storage_id,
                )["revision"]
                == 1
            )

            enable_race_state = await start_state(client, connection_id)
            enable_race_callback_ids: list[str] = []

            def disable_before_callback_binding(
                target_connection_id,
                callback_credential_id,
                credential_fields,
                *,
                expected_revision=None,
            ):
                if not enable_race_callback_ids:
                    enable_race_callback_ids.append(callback_credential_id)
                    connections.set_enabled(target_connection_id, False)
                return original_set_credential(
                    target_connection_id,
                    callback_credential_id,
                    credential_fields,
                    expected_revision=expected_revision,
                )

            monkeypatch.setattr(
                connections, "set_credential", disable_before_callback_binding
            )
            enable_race = await client.get(
                "/v1/auth/connector-oauth/callback/notion",
                params={"state": enable_race_state, "code": "code"},
            )
            monkeypatch.setattr(connections, "set_credential", original_set_credential)
            assert enable_race.status_code == 409
            assert len(enable_race_callback_ids) == 1
            persisted_winner = connections.get(connection_id)
            assert persisted_winner["enabled"] is False
            assert persisted_winner["credential_id"] == winner["credential_id"]
            assert (
                vault.get_metadata(
                    enable_race_callback_ids[0],
                    tenant_id=workspace_id,
                    kb_id=storage_id,
                )
                is None
            )
            assert provider.calls == 2

            # Cancellation after the cross-store connection CAS must not
            # strand a pending credential. The callback defers cancellation
            # until activation (or rollback) reaches a durable terminal state.
            connections.set_enabled(connection_id, True)
            cancelled_state = await start_state(client, connection_id)
            activation_started = Event()
            allow_activation = Event()
            original_activate = vault.activate

            def blocking_activate(*args, **kwargs):
                activation_started.set()
                if not allow_activation.wait(timeout=5):
                    raise TimeoutError("test did not release OAuth activation")
                return original_activate(*args, **kwargs)

            monkeypatch.setattr(vault, "activate", blocking_activate)
            cancelled_callback = asyncio.create_task(
                client.get(
                    "/v1/auth/connector-oauth/callback/notion",
                    params={"state": cancelled_state, "code": "code"},
                )
            )
            assert await _wait_for_event(activation_started, 2)
            cancelled_callback.cancel()
            allow_activation.set()
            with pytest.raises(asyncio.CancelledError):
                await cancelled_callback
            monkeypatch.setattr(vault, "activate", original_activate)

            activated_connection = connections.get(connection_id)
            activated_id = str(activated_connection["credential_id"])
            assert activated_id != str(winner["credential_id"])
            assert vault.get_metadata(
                activated_id,
                tenant_id=workspace_id,
                kb_id=storage_id,
            ) is not None
            assert vault.get_for_use(
                activated_id,
                tenant_id=workspace_id,
                kb_id=storage_id,
                connection_id=connection_id,
                actor_id="cancelled-callback-test",
            )["token"] == "provider-access-token"
            assert vault.pending_bindings() == []

    connections.close()
    sync_jobs.close()
    catalog.close()
    access_store.close()
    auth_store.close()
    vault.close()
