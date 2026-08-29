from __future__ import annotations

import base64
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urlsplit

import pytest

from cogdoc.connectors.credential_store import (
    CredentialExpiredError,
    CredentialRevisionConflict,
    CredentialVault,
)
from cogdoc.connectors.http_transport import HttpResponse
from cogdoc.connectors.oauth import (
    MAX_OAUTH_RESPONSE_BYTES,
    NOTION_API_VERSION,
    AtlassianOAuthAdapter,
    MicrosoftOAuthAdapter,
    NotionOAuthAdapter,
    OAuthAuthorizationSession,
    OAuthCoordinator,
    OAuthProviderError,
    OAuthReplayError,
    OAuthSessionExpired,
    OAuthSessionStore,
    OAuthStateMismatch,
    OAuthTokens,
)


MASTER_KEY = b"k" * 32


class FakeTransport:
    def __init__(self, *responses: HttpResponse):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def request(self, method, url, *, headers=None, body=None):
        self.calls.append(
            {"method": method, "url": url, "headers": headers or {}, "body": body}
        )
        return self.responses.pop(0)


def _response(payload, *, status=200, url="https://provider.example/token"):
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    return HttpResponse(status=status, headers={}, body=body, url=url)


def _vault(tmp_path, clock=lambda: 1_000.0):
    return CredentialVault(
        str(tmp_path / "state.db"),
        master_keys={"v1": MASTER_KEY},
        active_key_version="v1",
        clock=clock,
    )


def _session(provider):
    return OAuthAuthorizationSession(
        session_id="oauth-session",
        provider=provider,
        state="callback-state",
        code_challenge="challenge-value",
        code_challenge_method="S256",
        redirect_uri="https://cogdoc.example/oauth/callback",
        expires_at=2_000.0,
    )


def _database_bytes(path) -> bytes:
    result = b""
    for suffix in ("", "-wal", "-shm"):
        candidate = path.parent / f"{path.name}{suffix}"
        if candidate.exists():
            result += candidate.read_bytes()
    return result


def test_oauth_session_persists_only_state_hash_and_encrypted_pkce_verifier(tmp_path):
    vault = _vault(tmp_path)
    sessions = OAuthSessionStore(
        str(tmp_path / "state.db"), vault, clock=lambda: 1_000.0
    )
    session = sessions.create(
        provider="microsoft",
        tenant_id="tenant-a",
        kb_id="kb-a",
        connection_id="conn-a",
        user_id="user-a",
        redirect_uri="https://cogdoc.example/oauth/callback",
    )
    before_consume = _database_bytes(tmp_path / "state.db")
    assert session.state.encode() not in before_consume
    assert len(session.state) >= 43
    assert session.code_challenge_method == "S256"
    assert "callback-state" not in repr(session)

    consumed = sessions.consume(
        session.state,
        provider="microsoft",
        tenant_id="tenant-a",
        kb_id="kb-a",
        connection_id="conn-a",
        user_id="user-a",
    )
    assert 43 <= len(consumed.code_verifier) <= 128
    assert consumed.code_verifier.encode() not in before_consume
    assert consumed.redirect_uri == session.redirect_uri
    assert "code_verifier" not in repr(consumed)
    assert vault.list_metadata("tenant-a", "kb-a") == []
    sessions.close()
    vault.close()


def test_oauth_callback_is_scope_bound_and_one_shot(tmp_path):
    vault = _vault(tmp_path)
    sessions = OAuthSessionStore(
        str(tmp_path / "state.db"), vault, clock=lambda: 1_000.0
    )
    session = sessions.create(
        provider="notion",
        tenant_id="tenant-a",
        kb_id="kb-a",
        connection_id="conn-a",
        user_id="user-a",
        redirect_uri="https://cogdoc.example/oauth/callback",
    )
    with pytest.raises(OAuthStateMismatch):
        sessions.consume(
            session.state,
            provider="notion",
            tenant_id="tenant-a",
            kb_id="kb-a",
            connection_id="conn-a",
            user_id="different-user",
        )
    sessions.consume(
        session.state,
        provider="notion",
        tenant_id="tenant-a",
        kb_id="kb-a",
        connection_id="conn-a",
        user_id="user-a",
    )
    with pytest.raises(OAuthReplayError):
        sessions.consume(
            session.state,
            provider="notion",
            tenant_id="tenant-a",
            kb_id="kb-a",
            connection_id="conn-a",
            user_id="user-a",
        )
    sessions.close()
    vault.close()


def test_authority_loss_after_token_store_stays_unusable_when_delete_fails(
    tmp_path, monkeypatch
):
    vault = _vault(tmp_path)
    sessions = OAuthSessionStore(
        str(tmp_path / "state.db"), vault, clock=lambda: 1_000.0
    )
    checks = iter((True, True, False))
    coordinator = OAuthCoordinator(
        sessions,
        vault,
        {
            "notion": NotionOAuthAdapter(
                client_id="client",
                client_secret="secret",
                redirect_uri="https://cogdoc.example/oauth/callback",
                transport=FakeTransport(
                    _response(
                        {
                            "access_token": "live-access",
                            "refresh_token": "live-refresh",
                            "token_type": "bearer",
                        }
                    )
                ),
            )
        },
        clock=lambda: 1_000.0,
        authorization_checker=lambda _session: next(checks),
    )
    start = coordinator.begin(
        provider="notion",
        tenant_id="tenant-a",
        kb_id="kb-a",
        connection_id=None,
        user_id="user-a",
    )

    original_delete = vault.delete

    def fail_stored_token_delete(credential_id, **kwargs):
        metadata = vault.get_metadata(
            credential_id,
            tenant_id=kwargs["tenant_id"],
            kb_id=kwargs["kb_id"],
            include_inactive=True,
        )
        if metadata is not None and metadata["credential_kind"] == "oauth":
            raise sqlite3.OperationalError("injected delete failure")
        return original_delete(credential_id, **kwargs)

    monkeypatch.setattr(vault, "delete", fail_stored_token_delete)
    with pytest.raises(OAuthSessionExpired, match="authority changed"):
        coordinator.complete_callback(
            provider="notion",
            state=parse_qs(urlsplit(start.authorization_url).query)["state"][0],
            code="provider-code",
        )

    with sqlite3.connect(tmp_path / "state.db") as connection:
        row = connection.execute(
            "SELECT credential_id,lifecycle FROM connector_credentials "
            "WHERE credential_kind='oauth'"
        ).fetchone()
    assert row is not None and row[1] == "quarantined"
    credential_id = str(row[0])
    assert vault.list_metadata("tenant-a", "kb-a") == []
    assert vault.get_metadata(
        credential_id, tenant_id="tenant-a", kb_id="kb-a"
    ) is None
    with pytest.raises(CredentialExpiredError, match="not active"):
        vault.get_for_use(
            credential_id,
            tenant_id="tenant-a",
            kb_id="kb-a",
            connection_id=None,
            actor_id="worker",
        )
    sessions.close()
    vault.close()


def test_public_callback_restores_server_scope_and_rejects_cross_provider(tmp_path):
    vault = _vault(tmp_path)
    sessions = OAuthSessionStore(
        str(tmp_path / "state.db"), vault, clock=lambda: 1_000.0
    )
    session = sessions.create(
        provider="notion",
        tenant_id="stored-tenant",
        kb_id="stored-kb",
        connection_id="stored-connection",
        user_id="stored-user",
        redirect_uri="https://cogdoc.example/oauth/callback",
    )
    with pytest.raises(OAuthStateMismatch):
        sessions.consume_callback(session.state, provider="microsoft")

    consumed = sessions.consume_callback(session.state, provider="notion")
    assert (
        consumed.tenant_id,
        consumed.kb_id,
        consumed.connection_id,
        consumed.user_id,
    ) == ("stored-tenant", "stored-kb", "stored-connection", "stored-user")
    with pytest.raises(OAuthReplayError):
        sessions.consume_callback(session.state, provider="notion")
    sessions.close()
    vault.close()


def test_oauth_session_ttl_and_cancellation_are_fail_closed(tmp_path):
    now = [1_000.0]
    vault = _vault(tmp_path, clock=lambda: now[0])
    sessions = OAuthSessionStore(
        str(tmp_path / "state.db"), vault, clock=lambda: now[0]
    )
    expired = sessions.create(
        provider="notion",
        tenant_id="tenant-a",
        kb_id="kb-a",
        connection_id=None,
        user_id="user-a",
        redirect_uri="http://localhost:8080/oauth/callback",
        ttl_seconds=30,
    )
    now[0] = expired.expires_at
    with pytest.raises(OAuthSessionExpired):
        sessions.consume(
            expired.state,
            provider="notion",
            tenant_id="tenant-a",
            kb_id="kb-a",
            connection_id=None,
            user_id="user-a",
        )

    now[0] = 2_000.0
    cancelled = sessions.create(
        provider="notion",
        tenant_id="tenant-a",
        kb_id="kb-a",
        connection_id="conn-a",
        user_id="user-a",
        redirect_uri="https://cogdoc.example/oauth/callback",
    )
    assert sessions.cancel(
        cancelled.session_id,
        tenant_id="tenant-a",
        kb_id="kb-a",
        connection_id="conn-a",
        user_id="user-a",
    )
    with pytest.raises(OAuthReplayError):
        sessions.consume(
            cancelled.state,
            provider="notion",
            tenant_id="tenant-a",
            kb_id="kb-a",
            connection_id="conn-a",
            user_id="user-a",
        )
    sessions.close()
    vault.close()


def test_oauth_session_purge_is_bounded_and_removes_verifier_audit(tmp_path):
    now = [1_000.0]
    db_path = str(tmp_path / "state.db")
    vault = _vault(tmp_path, clock=lambda: now[0])
    sessions = OAuthSessionStore(db_path, vault, clock=lambda: now[0])
    created = [
        sessions.create(
            provider="notion",
            tenant_id="tenant-a",
            kb_id="kb-a",
            connection_id=None,
            user_id="user-a",
            redirect_uri="https://cogdoc.example/oauth/callback",
            ttl_seconds=30,
        )
        for _ in range(3)
    ]
    internal_ids = sessions.internal_credential_ids("tenant-a", "kb-a")
    assert len(internal_ids) == 3

    sessions.consume_callback(created[0].state, provider="notion")
    assert sessions.cancel_callback(created[1].state, provider="notion")
    # Successful consume/cancel removes both encrypted verifier rows and their
    # create/use/delete audit trail, while preserving the one-shot state row.
    remaining_metadata = vault.list_metadata("tenant-a", "kb-a")
    assert len(remaining_metadata) == 1
    # Even an active verifier is omitted by the vault's public audit reader.
    assert vault.audit_events("tenant-a", "kb-a", limit=1000) == []

    now[0] = 1_030.0
    assert sessions.purge_expired(limit=2) == 2
    with sqlite3.connect(db_path) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM connector_oauth_sessions"
            ).fetchone()[0]
            == 1
        )
    assert sessions.purge_expired(limit=2) == 1
    assert sessions.purge_expired(limit=2) == 0
    assert sessions.internal_credential_ids("tenant-a", "kb-a") == set()
    assert vault.list_metadata("tenant-a", "kb-a") == []
    assert vault.audit_events("tenant-a", "kb-a", limit=1000) == []
    with sqlite3.connect(db_path) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM connector_oauth_sessions"
            ).fetchone()[0]
            == 0
        )
    sessions.close()
    vault.close()


def test_oauth_create_opportunistically_purges_expired_session(tmp_path):
    now = [1_000.0]
    vault = _vault(tmp_path, clock=lambda: now[0])
    sessions = OAuthSessionStore(
        str(tmp_path / "state.db"), vault, clock=lambda: now[0]
    )
    expired = sessions.create(
        provider="notion",
        tenant_id="tenant-a",
        kb_id="kb-a",
        connection_id=None,
        user_id="user-a",
        redirect_uri="https://cogdoc.example/oauth/callback",
        ttl_seconds=30,
    )
    expired_id = next(iter(sessions.internal_credential_ids("tenant-a", "kb-a")))
    now[0] = expired.expires_at
    sessions.create(
        provider="notion",
        tenant_id="tenant-a",
        kb_id="kb-a",
        connection_id=None,
        user_id="user-a",
        redirect_uri="https://cogdoc.example/oauth/callback",
        ttl_seconds=30,
    )
    assert vault.get_metadata(expired_id, tenant_id="tenant-a", kb_id="kb-a") is None
    assert all(
        event["credential_id"] != expired_id
        for event in vault.audit_events("tenant-a", "kb-a", limit=1000)
    )
    sessions.close()
    vault.close()


def test_oauth_cleanup_supports_separate_session_and_vault_databases(tmp_path):
    now = [1_000.0]
    vault_path = tmp_path / "vault.db"
    session_path = tmp_path / "sessions.db"
    vault = CredentialVault(
        str(vault_path),
        master_keys={"v1": MASTER_KEY},
        active_key_version="v1",
        clock=lambda: now[0],
    )
    sessions = OAuthSessionStore(str(session_path), vault, clock=lambda: now[0])
    consumed = sessions.create(
        provider="notion",
        tenant_id="tenant-a",
        kb_id="kb-a",
        connection_id=None,
        user_id="user-a",
        redirect_uri="https://cogdoc.example/oauth/callback",
        ttl_seconds=30,
    )
    sessions.consume_callback(consumed.state, provider="notion")
    assert vault.list_metadata("tenant-a", "kb-a") == []
    assert vault.audit_events("tenant-a", "kb-a") == []

    cancelled = sessions.create(
        provider="notion",
        tenant_id="tenant-a",
        kb_id="kb-a",
        connection_id=None,
        user_id="user-a",
        redirect_uri="https://cogdoc.example/oauth/callback",
        ttl_seconds=30,
    )
    assert sessions.cancel_callback(cancelled.state, provider="notion")
    assert vault.list_metadata("tenant-a", "kb-a") == []
    assert vault.audit_events("tenant-a", "kb-a") == []

    expired = sessions.create(
        provider="notion",
        tenant_id="tenant-a",
        kb_id="kb-a",
        connection_id=None,
        user_id="user-a",
        redirect_uri="https://cogdoc.example/oauth/callback",
        ttl_seconds=30,
    )
    now[0] = expired.expires_at
    assert sessions.purge_expired(limit=10) >= 1
    assert vault.list_metadata("tenant-a", "kb-a") == []
    assert vault.audit_events("tenant-a", "kb-a") == []
    with sqlite3.connect(session_path) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM sqlite_master "
                "WHERE type='table' AND name='connector_credential_events'"
            ).fetchone()[0]
            == 0
        )
    sessions.close()
    vault.close()


def test_oauth_startup_reconciles_expired_unreferenced_verifier(tmp_path):
    now = [1_000.0]
    vault = CredentialVault(
        str(tmp_path / "vault.db"),
        master_keys={"v1": MASTER_KEY},
        active_key_version="v1",
        clock=lambda: now[0],
    )
    orphan = vault.create(
        tenant_id="tenant-a",
        kb_id="kb-a",
        connection_id=None,
        provider="notion",
        credential_kind="oauth-session",
        label="crash-window verifier",
        secret_values={"code_verifier": "v" * 64},
        actor_id="user-a",
        expires_at=1_030.0,
    )
    session_path = str(tmp_path / "sessions.db")
    sessions = OAuthSessionStore(session_path, vault, clock=lambda: now[0])
    assert (
        vault.get_metadata(orphan["credential_id"], tenant_id="tenant-a", kb_id="kb-a")
        is not None
    )
    sessions.close()

    now[0] = 1_030.0
    sessions = OAuthSessionStore(session_path, vault, clock=lambda: now[0])
    assert (
        vault.get_metadata(orphan["credential_id"], tenant_id="tenant-a", kb_id="kb-a")
        is None
    )
    assert vault.audit_events("tenant-a", "kb-a") == []
    sessions.close()
    vault.close()


def test_consumed_callback_survives_transient_post_consume_cleanup_failure(
    tmp_path, monkeypatch
):
    vault = _vault(tmp_path)
    sessions = OAuthSessionStore(
        str(tmp_path / "state.db"), vault, clock=lambda: 1_000.0
    )
    session = sessions.create(
        provider="notion",
        tenant_id="tenant-a",
        kb_id="kb-a",
        connection_id=None,
        user_id="user-a",
        redirect_uri="https://cogdoc.example/oauth/callback",
    )
    original_purge = vault.purge_internal_audit_events

    def fail_audit_cleanup(*args, **kwargs):
        del args, kwargs
        raise RuntimeError("transient audit cleanup failure")

    monkeypatch.setattr(vault, "purge_internal_audit_events", fail_audit_cleanup)
    consumed = sessions.consume_callback(session.state, provider="notion")
    assert 43 <= len(consumed.code_verifier) <= 128
    assert sessions.internal_credential_ids("tenant-a", "kb-a")

    monkeypatch.setattr(vault, "purge_internal_audit_events", original_purge)
    assert sessions.purge_expired(limit=10) == 1
    assert sessions.internal_credential_ids("tenant-a", "kb-a") == set()
    assert vault.audit_events("tenant-a", "kb-a") == []
    sessions.close()
    vault.close()


def test_notion_adapter_matches_current_public_connection_flow():
    transport = FakeTransport(
        _response(
            {
                "access_token": "notion-access",
                "refresh_token": "notion-refresh",
                "token_type": "bearer",
                "workspace_id": "workspace-a",
                "bot_id": "bot-a",
            },
            url="https://api.notion.com/v1/oauth/token",
        )
    )
    adapter = NotionOAuthAdapter(
        client_id="notion-client",
        client_secret="notion-secret",
        redirect_uri="https://cogdoc.example/oauth/callback",
        transport=transport,
    )
    authorization_url = adapter.authorization_url(_session("notion"))
    parts = urlsplit(authorization_url)
    query = parse_qs(parts.query)
    assert parts.geturl().startswith("https://api.notion.com/v1/oauth/authorize?")
    assert query == {
        "owner": ["user"],
        "client_id": ["notion-client"],
        "redirect_uri": ["https://cogdoc.example/oauth/callback"],
        "response_type": ["code"],
        "state": ["callback-state"],
    }
    assert "code_challenge" not in query

    tokens = adapter.exchange_code("authorization-code", "unused-verifier")
    call = transport.calls[0]
    expected_basic = base64.b64encode(b"notion-client:notion-secret").decode()
    assert call["url"] == "https://api.notion.com/v1/oauth/token"
    assert call["headers"]["Authorization"] == f"Basic {expected_basic}"
    assert call["headers"]["Notion-Version"] == NOTION_API_VERSION
    assert json.loads(call["body"]) == {
        "grant_type": "authorization_code",
        "code": "authorization-code",
        "redirect_uri": "https://cogdoc.example/oauth/callback",
    }
    assert tokens.access_token == "notion-access"
    assert tokens.provider_metadata["workspace_id"] == "workspace-a"
    assert "notion-access" not in repr(tokens)


def test_atlassian_adapter_matches_current_3lo_and_rotating_refresh_flow():
    transport = FakeTransport(
        _response(
            {
                "access_token": "atl-access",
                "refresh_token": "atl-refresh-2",
                "expires_in": 3600,
                "scope": "read:page:confluence read:content-details:confluence offline_access",
            },
            url="https://auth.atlassian.com/oauth/token",
        ),
        _response(
            [
                {
                    "id": "cloud-123",
                    "name": "Docs",
                    "url": "https://docs.atlassian.net",
                    "scopes": [
                        "read:page:confluence",
                        "read:content-details:confluence",
                    ],
                }
            ],
            url="https://api.atlassian.com/oauth/token/accessible-resources",
        ),
    )
    adapter = AtlassianOAuthAdapter(
        client_id="atl-client",
        client_secret="atl-secret",
        redirect_uri="https://cogdoc.example/oauth/callback",
        scopes=[
            "read:page:confluence",
            "read:content-details:confluence",
            "offline_access",
        ],
        transport=transport,
    )
    query = parse_qs(urlsplit(adapter.authorization_url(_session("atlassian"))).query)
    assert query["audience"] == ["api.atlassian.com"]
    assert query["scope"] == [
        "read:page:confluence read:content-details:confluence offline_access"
    ]
    assert query["prompt"] == ["consent"]
    assert "code_challenge" not in query

    tokens = adapter.refresh("atl-refresh-1")
    assert json.loads(transport.calls[0]["body"]) == {
        "grant_type": "refresh_token",
        "client_id": "atl-client",
        "client_secret": "atl-secret",
        "refresh_token": "atl-refresh-1",
    }
    assert tokens.refresh_token == "atl-refresh-2"
    assert tokens.expires_in == 3600
    assert transport.calls[1]["url"] == (
        "https://api.atlassian.com/oauth/token/accessible-resources"
    )
    assert transport.calls[1]["headers"]["Authorization"] == "Bearer atl-access"
    assert json.loads(tokens.provider_metadata["accessible_resources"]) == [
        {
            "cloud_id": "cloud-123",
            "site_url": "https://docs.atlassian.net",
            "scopes": [
                "read:page:confluence",
                "read:content-details:confluence",
            ],
        }
    ]


def test_atlassian_default_transport_allows_token_and_resource_hosts():
    adapter = AtlassianOAuthAdapter(
        client_id="atl-client",
        client_secret="atl-secret",
        redirect_uri="https://cogdoc.example/oauth/callback",
        scopes=["read:page:confluence"],
    )
    assert adapter.transport.allowed_hosts == frozenset(
        {"auth.atlassian.com", "api.atlassian.com"}
    )


def test_atlassian_coordinator_binds_cloud_resource_to_connection_site(tmp_path):
    vault = _vault(tmp_path)
    sessions = OAuthSessionStore(
        str(tmp_path / "state.db"), vault, clock=lambda: 1_000.0
    )
    transport = FakeTransport(
        _response(
            {
                "access_token": "atl-access",
                "refresh_token": "atl-refresh",
                "expires_in": 3600,
                "scope": "read:page:confluence offline_access",
            },
            url="https://auth.atlassian.com/oauth/token",
        ),
        _response(
            [
                {
                    "id": "other-cloud",
                    "url": "https://other.atlassian.net",
                    "scopes": ["read:page:confluence"],
                },
                {
                    "id": "docs-cloud",
                    "url": "https://docs.atlassian.net",
                    "scopes": ["read:page:confluence"],
                },
            ],
            url="https://api.atlassian.com/oauth/token/accessible-resources",
        ),
    )
    adapter = AtlassianOAuthAdapter(
        client_id="atl-client",
        client_secret="atl-secret",
        redirect_uri="https://cogdoc.example/oauth/callback",
        scopes=["read:page:confluence", "offline_access"],
        transport=transport,
    )
    coordinator = OAuthCoordinator(
        sessions,
        vault,
        {"atlassian": adapter},
        clock=lambda: 1_000.0,
        connection_reader=lambda connection_id: {
            "connection_id": connection_id,
            "tenant_id": "tenant-a",
            "kb_id": "kb-a",
            "connector_type": "confluence",
            "config": {"base_url": "https://docs.atlassian.net"},
        },
    )
    start = coordinator.begin(
        provider="atlassian",
        tenant_id="tenant-a",
        kb_id="kb-a",
        connection_id="conn-a",
        user_id="user-a",
    )
    row = coordinator.complete_callback(
        provider="atlassian",
        state=(parse_qs(urlsplit(start.authorization_url).query)["state"][0]),
        code="authorization-code",
    )
    secrets = vault.get_for_use(
        row["credential_id"],
        tenant_id="tenant-a",
        kb_id="kb-a",
        connection_id="conn-a",
        actor_id="user-a",
    )
    assert secrets["cloud_id"] == "docs-cloud"
    assert secrets["site_url"] == "https://docs.atlassian.net"
    assert secrets["token"] == "atl-access"
    sessions.close()
    vault.close()


def test_microsoft_adapter_uses_v2_endpoints_form_encoding_and_s256_pkce():
    transport = FakeTransport(
        _response(
            {
                "access_token": "ms-access",
                "refresh_token": "ms-refresh",
                "token_type": "Bearer",
                "expires_in": 3599,
                "scope": "offline_access Files.Read.All",
            },
            url="https://login.microsoftonline.com/organizations/oauth2/v2.0/token",
        )
    )
    adapter = MicrosoftOAuthAdapter(
        client_id="ms-client",
        client_secret="ms-secret",
        redirect_uri="https://cogdoc.example/oauth/callback",
        scopes=["offline_access", "Files.Read.All"],
        transport=transport,
    )
    query = parse_qs(urlsplit(adapter.authorization_url(_session("microsoft"))).query)
    assert query["response_mode"] == ["query"]
    assert query["code_challenge"] == ["challenge-value"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["state"] == ["callback-state"]

    verifier = "a" * 43
    tokens = adapter.exchange_code("authorization-code", verifier)
    call = transport.calls[0]
    assert call["url"] == (
        "https://login.microsoftonline.com/organizations/oauth2/v2.0/token"
    )
    assert call["headers"]["Content-Type"] == "application/x-www-form-urlencoded"
    body = parse_qs(call["body"].decode())
    assert body["grant_type"] == ["authorization_code"]
    assert body["code_verifier"] == [verifier]
    assert body["client_secret"] == ["ms-secret"]
    assert tokens.scopes == ("offline_access", "Files.Read.All")


@pytest.mark.parametrize("tenant", ["../common", "tenant/path", ".hidden", "a..b"])
def test_microsoft_tenant_cannot_escape_the_official_endpoint(tenant):
    with pytest.raises(ValueError, match="tenant"):
        MicrosoftOAuthAdapter(
            client_id="client",
            client_secret=None,
            redirect_uri="https://cogdoc.example/oauth/callback",
            scopes=["Files.Read.All"],
            tenant=tenant,
        )


def test_provider_response_is_bounded_and_error_body_is_not_disclosed():
    secret_error = b'{"error_description":"provider-secret-diagnostic"}'
    transport = FakeTransport(
        HttpResponse(
            status=400,
            headers={},
            body=secret_error,
            url="https://api.notion.com/v1/oauth/token",
        )
    )
    adapter = NotionOAuthAdapter(
        client_id="client",
        client_secret="secret",
        redirect_uri="https://cogdoc.example/oauth/callback",
        transport=transport,
    )
    with pytest.raises(OAuthProviderError) as error:
        adapter.exchange_code("code", "verifier")
    assert "provider-secret-diagnostic" not in str(error.value)

    oversized = FakeTransport(
        HttpResponse(
            status=200,
            headers={},
            body=b"x" * (MAX_OAUTH_RESPONSE_BYTES + 1),
            url="https://api.notion.com/v1/oauth/token",
        )
    )
    bounded = NotionOAuthAdapter(
        client_id="client",
        client_secret="secret",
        redirect_uri="https://cogdoc.example/oauth/callback",
        transport=oversized,
    )
    with pytest.raises(OAuthProviderError, match="byte limit"):
        bounded.exchange_code("code", "verifier")


def test_coordinator_exchanges_once_encrypts_tokens_and_rotates_refresh(tmp_path):
    now = [1_000.0]
    exchange = _response(
        {
            "access_token": "access-token-1",
            "refresh_token": "refresh-token-1",
            "expires_in": 3600,
            "scope": "offline_access Files.Read.All",
        },
        url="https://login.microsoftonline.com/organizations/oauth2/v2.0/token",
    )
    refresh = _response(
        {
            "access_token": "access-token-2",
            "refresh_token": "refresh-token-2",
            "expires_in": 3600,
            "scope": "offline_access Files.Read.All",
        },
        url="https://login.microsoftonline.com/organizations/oauth2/v2.0/token",
    )
    transport = FakeTransport(exchange, refresh)
    adapter = MicrosoftOAuthAdapter(
        client_id="client",
        client_secret="client-secret",
        redirect_uri="https://cogdoc.example/oauth/callback",
        scopes=["offline_access", "Files.Read.All"],
        transport=transport,
    )
    vault = _vault(tmp_path, clock=lambda: now[0])
    sessions = OAuthSessionStore(
        str(tmp_path / "state.db"), vault, clock=lambda: now[0]
    )
    coordinator = OAuthCoordinator(
        sessions, vault, {"microsoft": adapter}, clock=lambda: now[0]
    )

    start = coordinator.begin(
        provider="microsoft",
        tenant_id="tenant-a",
        kb_id="kb-a",
        connection_id="conn-a",
        user_id="user-a",
    )
    state = parse_qs(urlsplit(start.authorization_url).query)["state"][0]
    assert "authorization_url" not in repr(start)
    metadata = coordinator.complete_callback(
        provider="microsoft",
        state=state,
        code="one-shot-code",
        label="Microsoft SharePoint",
    )
    assert metadata["credential_kind"] == "oauth"
    assert metadata["secret_fields"] == [
        "access_token_expires_at",
        "refresh_token",
        "token",
    ]
    assert "access-token-1" not in json.dumps(metadata)
    assert b"access-token-1" not in _database_bytes(tmp_path / "state.db")
    with pytest.raises(OAuthReplayError):
        coordinator.complete_callback(
            provider="microsoft",
            state=state,
            code="replayed-code",
            label="replay",
        )

    now[0] = 2_000.0
    rotated = coordinator.refresh_credential(
        metadata["credential_id"],
        tenant_id="tenant-a",
        kb_id="kb-a",
        connection_id="conn-a",
        user_id="user-a",
        expected_revision=1,
    )
    assert rotated["revision"] == 2
    assert vault.get_for_use(
        metadata["credential_id"],
        tenant_id="tenant-a",
        kb_id="kb-a",
        connection_id="conn-a",
        actor_id="worker",
    ) == {
        "token": "access-token-2",
        "refresh_token": "refresh-token-2",
        "access_token_expires_at": "5600.0",
    }
    sessions.close()
    vault.close()


def test_refresh_without_expected_revision_still_uses_metadata_cas(tmp_path):
    vault = _vault(tmp_path)
    sessions = OAuthSessionStore(str(tmp_path / "state.db"), vault)
    metadata = vault.create(
        tenant_id="tenant-a",
        kb_id="kb-a",
        connection_id=None,
        provider="notion",
        credential_kind="oauth",
        label="Concurrent OAuth",
        secret_values={"token": "access-0", "refresh_token": "refresh-0"},
        actor_id="user-a",
    )

    class ConcurrentAdapter:
        provider = "notion"

        def __init__(self):
            self.barrier = threading.Barrier(2)
            self.lock = threading.Lock()
            self.calls = 0

        def refresh(self, refresh_token):
            assert refresh_token == "refresh-0"
            with self.lock:
                self.calls += 1
                call = self.calls
            self.barrier.wait(timeout=3)
            return OAuthTokens(
                access_token=f"access-{call}",
                refresh_token=f"refresh-{call}",
                token_type="bearer",
                expires_in=None,
                scopes=(),
                provider_metadata={},
            )

    adapter = ConcurrentAdapter()
    coordinator = OAuthCoordinator(sessions, vault, {"notion": adapter})

    def refresh():
        return coordinator.refresh_credential(
            str(metadata["credential_id"]),
            tenant_id="tenant-a",
            kb_id="kb-a",
            connection_id=None,
            user_id="user-a",
        )

    outcomes = []
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(refresh) for _ in range(2)]
        for future in futures:
            try:
                outcomes.append(future.result(timeout=5))
            except CredentialRevisionConflict as exc:
                outcomes.append(exc)

    successful = [item for item in outcomes if isinstance(item, dict)]
    conflicts = [
        item for item in outcomes if isinstance(item, CredentialRevisionConflict)
    ]
    assert len(successful) == len(conflicts) == 1
    assert successful[0]["revision"] == 2
    assert adapter.calls == 2
    assert (
        vault.get_metadata(
            str(metadata["credential_id"]), tenant_id="tenant-a", kb_id="kb-a"
        )["revision"]
        == 2
    )
    sessions.close()
    vault.close()


def test_refresh_rejects_explicit_stale_revision_before_provider_call(tmp_path):
    vault = _vault(tmp_path)
    sessions = OAuthSessionStore(str(tmp_path / "state.db"), vault)
    metadata = vault.create(
        tenant_id="tenant-a",
        kb_id="kb-a",
        connection_id=None,
        provider="notion",
        credential_kind="oauth",
        label="OAuth",
        secret_values={"token": "access", "refresh_token": "refresh"},
        actor_id="user-a",
    )

    class UnexpectedAdapter:
        provider = "notion"

        def refresh(self, refresh_token):
            raise AssertionError(f"provider refresh should not run: {refresh_token}")

    coordinator = OAuthCoordinator(sessions, vault, {"notion": UnexpectedAdapter()})
    with pytest.raises(CredentialRevisionConflict):
        coordinator.refresh_credential(
            str(metadata["credential_id"]),
            tenant_id="tenant-a",
            kb_id="kb-a",
            connection_id=None,
            user_id="user-a",
            expected_revision=2,
        )
    assert (
        vault.get_metadata(
            str(metadata["credential_id"]), tenant_id="tenant-a", kb_id="kb-a"
        )["revision"]
        == 1
    )
    sessions.close()
    vault.close()


def test_refresh_revalidates_authority_after_provider_before_vault_cas(tmp_path):
    vault = _vault(tmp_path)
    sessions = OAuthSessionStore(str(tmp_path / "state.db"), vault)
    metadata = vault.create(
        tenant_id="tenant-a",
        kb_id="kb-a",
        connection_id=None,
        provider="notion",
        credential_kind="oauth",
        label="OAuth",
        secret_values={"token": "access-old", "refresh_token": "refresh-old"},
        actor_id="user-a",
    )

    class RefreshAdapter:
        provider = "notion"

        def refresh(self, refresh_token):
            assert refresh_token == "refresh-old"
            return OAuthTokens(
                access_token="access-new",
                refresh_token="refresh-new",
                token_type="bearer",
                expires_in=None,
                scopes=(),
                provider_metadata={},
            )

    authority = iter((True, False))
    coordinator = OAuthCoordinator(sessions, vault, {"notion": RefreshAdapter()})
    with pytest.raises(OAuthSessionExpired, match="authority changed"):
        coordinator.refresh_credential(
            str(metadata["credential_id"]),
            tenant_id="tenant-a",
            kb_id="kb-a",
            connection_id=None,
            user_id="user-a",
            expected_revision=1,
            authority_checker=lambda: next(authority),
        )

    assert vault.get_metadata(
        str(metadata["credential_id"]), tenant_id="tenant-a", kb_id="kb-a"
    )["revision"] == 1
    assert vault.get_for_use(
        str(metadata["credential_id"]),
        tenant_id="tenant-a",
        kb_id="kb-a",
        connection_id=None,
        actor_id="test",
    ) == {"token": "access-old", "refresh_token": "refresh-old"}
    sessions.close()
    vault.close()
