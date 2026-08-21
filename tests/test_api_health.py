import json
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from cogdoc.api.app import _unhandled_error_response, create_app
from cogdoc.api.auth_store import AuthStore
from cogdoc.api.oidc import OIDCFlowStore, OIDCManager
from cogdoc.api.resource_access import ResourceAccessStore
from cogdoc.api.scim import parse_scim_access_registry
from cogdoc.api.session_store import SessionStore
from cogdoc.connectors.connection_store import ConnectionStore
from cogdoc.connectors.credential_store import CredentialVault
from cogdoc.connectors.oauth import OAuthCoordinator, OAuthSessionStore
from cogdoc.connectors.sync_store import ConnectorSyncStore
from cogdoc.service.source_artifact_store import SourceArtifactStore
from cogdoc.service.source_catalog import SourceCatalog


# 声明异步测试使用的后端。
@pytest.fixture
def anyio_backend():
    return "asyncio"


# 创建测试客户端。
async def _client(app, raise_app_exceptions=True):
    transport = ASGITransport(app=app, raise_app_exceptions=raise_app_exceptions)
    return AsyncClient(transport=transport, base_url="http://testserver")


def _connector_ready_app(tmp_path, *, with_oauth=True):
    vault = CredentialVault(
        str(tmp_path / "vault.db"),
        master_keys={"v1": b"h" * 32},
        active_key_version="v1",
    )
    options = {
        "session_store": SessionStore(),
        "connection_store": ConnectionStore(str(tmp_path / "connections.db")),
        "connector_sync_store": ConnectorSyncStore(str(tmp_path / "sync.db")),
        "source_catalog": SourceCatalog(str(tmp_path / "catalog.db")),
        "source_artifact_store": SourceArtifactStore(tmp_path / "artifacts"),
        "connector_credential_vault": vault,
    }
    if with_oauth:
        oauth_sessions = OAuthSessionStore(str(tmp_path / "oauth.db"), vault)
        options["connector_oauth"] = OAuthCoordinator(oauth_sessions, vault, {})
    app = create_app(
        **options,
    )
    return app


def _close_connector_ready_stores(app) -> None:
    for name in (
        "connection_store",
        "connector_sync_store",
        "source_catalog",
        "connector_credential_vault",
        "connector_oauth_session_store",
    ):
        try:
            getattr(app.state, name).close()
        except Exception:
            pass


# 验证 healthz is ok 场景。
@pytest.mark.anyio
async def test_healthz_is_ok(monkeypatch):
    import cogdoc.api.app as app_module

    monkeypatch.setattr(app_module, "configure_logging", lambda: None)
    app = create_app(session_store=SessionStore())

    async with app.router.lifespan_context(app):
        async with await _client(app) as client:
            resp = await client.get("/healthz")

    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


# 验证 readyz reports native dependency 场景。
@pytest.mark.anyio
async def test_readyz_reports_native_dependency(monkeypatch):
    import cogdoc.api.app as app_module

    monkeypatch.setattr(app_module, "configure_logging", lambda: None)
    app = create_app(session_store=SessionStore())

    async with app.router.lifespan_context(app):
        async with await _client(app) as client:
            resp = await client.get("/readyz")

    assert resp.status_code == 200
    assert resp.json() == {"status": "ready", "rust_core": True}


# 验证 readyz returns 503 when native missing 场景。
@pytest.mark.anyio
async def test_readyz_returns_503_when_native_missing(monkeypatch):
    import cogdoc.api.app as app_module
    import cogdoc.api.routes.health as health_module

    monkeypatch.setattr(app_module, "configure_logging", lambda: None)

    # 构造或驱动 missing 测试场景。
    def _missing(*symbols):
        raise RuntimeError("rust_core 未安装")

    monkeypatch.setattr(health_module, "ensure_rust_core", _missing)
    app = create_app(session_store=SessionStore())

    async with app.router.lifespan_context(app):
        async with await _client(app) as client:
            resp = await client.get("/readyz")

    assert resp.status_code == 503
    assert resp.json()["status"] == "not_ready"
    assert resp.json()["rust_core"] is False
    assert "rust_core 未安装" not in resp.text


@pytest.mark.anyio
async def test_structured_liveness_is_independent_of_startup(monkeypatch):
    import cogdoc.api.app as app_module

    monkeypatch.setattr(app_module, "configure_logging", lambda: None)
    app = create_app(session_store=SessionStore())

    async with await _client(app) as client:
        resp = await client.get("/health/live")

    assert resp.status_code == 200
    assert resp.json()["status"] == "alive"
    assert resp.json()["timestamp"]


@pytest.mark.anyio
async def test_structured_readiness_reports_components(monkeypatch):
    import cogdoc.api.app as app_module

    monkeypatch.setattr(app_module, "configure_logging", lambda: None)
    app = create_app(session_store=SessionStore())

    async with app.router.lifespan_context(app):
        async with await _client(app) as client:
            resp = await client.get("/health/ready")

    payload = resp.json()
    assert resp.status_code == 200
    assert payload["status"] == "ready"
    assert payload["components"]["lifecycle"]["status"] == "ready"
    assert payload["components"]["state"]["status"] == "ready"
    assert payload["components"]["rust_core"]["status"] == "ready"
    assert payload["components"]["authentication"] == {
        "status": "degraded",
        "required": False,
    }
    for component in (
        "connection_store",
        "connector_sync_store",
        "source_catalog",
        "source_artifact_store",
    ):
        assert payload["components"][component] == {
            "status": "ready",
            "required": True,
        }
    assert payload["components"]["connector_credential_vault"] == {
        "status": "disabled",
        "required": False,
    }
    assert payload["components"]["connector_oauth_session_store"] == {
        "status": "disabled",
        "required": False,
    }
    assert payload["components"]["scim_directory"] == {
        "status": "disabled",
        "required": False,
    }


@pytest.mark.anyio
async def test_scim_readiness_requires_healthy_auth_store(tmp_path, monkeypatch):
    import cogdoc.api.app as app_module

    monkeypatch.setattr(app_module, "configure_logging", lambda: None)
    auth = AuthStore(str(tmp_path / "auth.db"), scrypt_n=1 << 10)
    access = ResourceAccessStore(tmp_path / "access.db")
    owner = auth.register("owner@example.com", "correct horse battery", "Owner")
    registry = parse_scim_access_registry(
        json.dumps(
            [
                {
                    "token": "s" * 32,
                    "workspace_id": owner["workspace"]["workspace_id"],
                }
            ]
        ),
        issuer="https://id.example.com",
    )
    app = create_app(
        session_store=SessionStore(),
        auth_store=auth,
        resource_access_store=access,
        scim_access_registry=registry,
    )
    try:
        async with app.router.lifespan_context(app):
            async with await _client(app) as client:
                ready = await client.get("/health/ready")
                auth.close()
                app.state.readiness_probe_cache = None
                unavailable = await client.get("/health/ready")
        assert ready.status_code == 200
        assert ready.json()["components"]["scim_directory"] == {
            "status": "ready",
            "required": True,
        }
        assert unavailable.status_code == 503
        assert unavailable.json()["components"]["scim_directory"] == {
            "status": "not_ready",
            "required": True,
        }
    finally:
        auth.close()
        access.close()


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("component", "required_table"),
    [
        ("connection_store", "connector_connections"),
        ("connector_sync_store", "connector_sync_jobs"),
        ("source_catalog", "source_catalog_documents"),
        ("connector_credential_vault", "connector_credentials"),
        ("connector_oauth_session_store", "connector_oauth_sessions"),
    ],
)
@pytest.mark.parametrize("failure_kind", ["closed", "damaged"])
async def test_connector_readiness_fails_for_closed_or_damaged_sqlite_component(
    tmp_path, monkeypatch, component, required_table, failure_kind
):
    import cogdoc.api.app as app_module

    monkeypatch.setattr(app_module, "configure_logging", lambda: None)
    app = _connector_ready_app(tmp_path)
    try:
        async with app.router.lifespan_context(app):
            target = getattr(app.state, component)
            if failure_kind == "closed":
                target.close()
            else:
                target._conn.execute(f"DROP TABLE {required_table}")
            async with await _client(app) as client:
                structured = await client.get("/health/ready")
                legacy = await client.get("/readyz")
    finally:
        _close_connector_ready_stores(app)

    assert structured.status_code == legacy.status_code == 503
    assert structured.json()["components"][component] == {
        "status": "not_ready",
        "required": True,
    }


@pytest.mark.anyio
async def test_vault_is_required_without_enabling_oauth_sessions(tmp_path, monkeypatch):
    import cogdoc.api.app as app_module

    monkeypatch.setattr(app_module, "configure_logging", lambda: None)
    app = _connector_ready_app(tmp_path, with_oauth=False)
    try:
        async with app.router.lifespan_context(app):
            async with await _client(app) as client:
                response = await client.get("/health/ready")
    finally:
        _close_connector_ready_stores(app)

    assert response.status_code == 200
    assert response.json()["components"]["connector_credential_vault"] == {
        "status": "ready",
        "required": True,
    }
    assert response.json()["components"]["connector_oauth_session_store"] == {
        "status": "disabled",
        "required": False,
    }


@pytest.mark.anyio
async def test_oidc_flow_store_is_required_and_fails_closed(tmp_path, monkeypatch):
    import cogdoc.api.app as app_module

    monkeypatch.setattr(app_module, "configure_logging", lambda: None)
    auth_store = AuthStore(str(tmp_path / "auth.db"), scrypt_n=1 << 10)
    access_store = ResourceAccessStore(tmp_path / "access.db")
    flow_store = OIDCFlowStore(str(tmp_path / "flows.db"), b"o" * 32)
    oidc_manager = OIDCManager(
        SimpleNamespace(config=SimpleNamespace(display_name="Enterprise SSO")),
        flow_store,
        auth_store,
    )
    app = create_app(
        session_store=SessionStore(),
        auth_store=auth_store,
        resource_access_store=access_store,
        oidc_manager=oidc_manager,
    )
    try:
        async with app.router.lifespan_context(app):
            flow_store.close()
            async with await _client(app) as client:
                response = await client.get("/health/ready")
    finally:
        flow_store.close()
        auth_store.close()
        access_store.close()

    assert response.status_code == 503
    assert response.json()["components"]["oidc_flow_store"] == {
        "status": "not_ready",
        "required": True,
    }


@pytest.mark.anyio
@pytest.mark.parametrize("failure_kind", ["unavailable", "not_writable"])
async def test_connector_readiness_requires_accessible_writable_artifact_root(
    tmp_path, monkeypatch, failure_kind
):
    import cogdoc.api.app as app_module

    monkeypatch.setattr(app_module, "configure_logging", lambda: None)
    app = _connector_ready_app(tmp_path)
    artifact_store = app.state.source_artifact_store
    try:
        async with app.router.lifespan_context(app):
            if failure_kind == "unavailable":
                artifact_store.root.rename(tmp_path / "artifacts-unavailable")
            else:
                monkeypatch.setattr(
                    artifact_store,
                    "check",
                    lambda: (_ for _ in ()).throw(
                        PermissionError("artifact root is read-only")
                    ),
                )
            async with await _client(app) as client:
                structured = await client.get("/health/ready")
                legacy = await client.get("/readyz")
    finally:
        _close_connector_ready_stores(app)

    assert structured.status_code == legacy.status_code == 503
    assert structured.json()["components"]["source_artifact_store"] == {
        "status": "not_ready",
        "required": True,
    }


@pytest.mark.anyio
async def test_structured_readiness_rejects_service_before_startup(monkeypatch):
    import cogdoc.api.app as app_module

    monkeypatch.setattr(app_module, "configure_logging", lambda: None)
    app = create_app(session_store=SessionStore())

    async with await _client(app) as client:
        resp = await client.get("/health/ready")

    payload = resp.json()
    assert resp.status_code == 503
    assert payload["status"] == "not_ready"
    assert payload["components"]["lifecycle"]["status"] == "not_ready"


@pytest.mark.anyio
async def test_required_ocr_dependency_blocks_readiness(monkeypatch):
    import cogdoc.api.app as app_module
    import cogdoc.api.routes.health as health_module

    monkeypatch.setattr(app_module, "configure_logging", lambda: None)
    monkeypatch.setattr(
        health_module,
        "_ocr_readiness_component",
        lambda: {"status": "not_ready", "required": True},
    )
    app = create_app(session_store=SessionStore())

    async with app.router.lifespan_context(app):
        async with await _client(app) as client:
            resp = await client.get("/health/ready")

    assert resp.status_code == 503
    assert resp.json()["components"]["ocr"] == {
        "status": "not_ready",
        "required": True,
    }


@pytest.mark.anyio
async def test_optional_ocr_dependency_is_degraded_but_ready(monkeypatch):
    import cogdoc.api.app as app_module
    import cogdoc.api.routes.health as health_module

    monkeypatch.setattr(app_module, "configure_logging", lambda: None)
    monkeypatch.setattr(
        health_module,
        "_ocr_readiness_component",
        lambda: {"status": "degraded", "required": False},
    )
    app = create_app(session_store=SessionStore())

    async with app.router.lifespan_context(app):
        async with await _client(app) as client:
            resp = await client.get("/health/ready")

    assert resp.status_code == 200
    assert resp.json()["components"]["ocr"] == {
        "status": "degraded",
        "required": False,
    }


@pytest.mark.anyio
async def test_account_mode_requires_healthy_authentication_and_acl_stores(
    tmp_path, monkeypatch
):
    import cogdoc.api.app as app_module

    monkeypatch.setattr(app_module, "configure_logging", lambda: None)
    auth_store = AuthStore(str(tmp_path / "auth.db"), scrypt_n=1 << 10)
    access_store = ResourceAccessStore(tmp_path / "access.db")
    app = create_app(
        session_store=SessionStore(),
        auth_store=auth_store,
        resource_access_store=access_store,
    )

    try:
        async with app.router.lifespan_context(app):
            async with await _client(app) as client:
                structured = await client.get("/health/ready")
                legacy = await client.get("/readyz")
    finally:
        auth_store.close()
        access_store.close()

    assert structured.status_code == legacy.status_code == 200
    components = structured.json()["components"]
    assert components["authentication"] == {
        "status": "ready",
        "required": True,
    }
    assert components["resource_access"] == {
        "status": "ready",
        "required": True,
    }


@pytest.mark.anyio
@pytest.mark.parametrize("closed_store", ["authentication", "resource_access"])
async def test_account_mode_readiness_fails_closed_when_security_store_is_closed(
    tmp_path, monkeypatch, closed_store
):
    import cogdoc.api.app as app_module

    monkeypatch.setattr(app_module, "configure_logging", lambda: None)
    auth_store = AuthStore(str(tmp_path / "auth.db"), scrypt_n=1 << 10)
    access_store = ResourceAccessStore(tmp_path / "access.db")
    app = create_app(
        session_store=SessionStore(),
        auth_store=auth_store,
        resource_access_store=access_store,
    )

    try:
        async with app.router.lifespan_context(app):
            if closed_store == "authentication":
                auth_store.close()
            else:
                access_store.close()
            async with await _client(app) as client:
                structured = await client.get("/health/ready")
                legacy = await client.get("/readyz")
    finally:
        auth_store.close()
        access_store.close()

    assert structured.status_code == legacy.status_code == 503
    failed = structured.json()["components"][closed_store]
    assert failed == {"status": "not_ready", "required": True}


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("failed_store", "failure_kind"),
    [
        ("authentication", "damaged"),
        ("resource_access", "damaged"),
        ("authentication", "exception"),
        ("resource_access", "exception"),
    ],
)
async def test_account_mode_readiness_rejects_damaged_or_exceptional_store(
    tmp_path, monkeypatch, failed_store, failure_kind
):
    import cogdoc.api.app as app_module

    monkeypatch.setattr(app_module, "configure_logging", lambda: None)
    auth_store = AuthStore(str(tmp_path / "auth.db"), scrypt_n=1 << 10)
    access_store = ResourceAccessStore(tmp_path / "access.db")
    app = create_app(
        session_store=SessionStore(),
        auth_store=auth_store,
        resource_access_store=access_store,
    )

    try:
        async with app.router.lifespan_context(app):
            target_store = (
                auth_store if failed_store == "authentication" else access_store
            )
            if failure_kind == "damaged":
                # Simulate a partially damaged migration while the connection
                # itself is alive; it must not look like an empty database.
                table = (
                    "auth_sessions"
                    if failed_store == "authentication"
                    else "resource_access_acl_epochs"
                )
                target_store._conn.execute(f"DROP TABLE {table}")
            else:
                monkeypatch.setattr(
                    target_store,
                    "check",
                    lambda: (_ for _ in ()).throw(RuntimeError("probe failed")),
                )
            async with await _client(app) as client:
                response = await client.get("/health/ready")
    finally:
        auth_store.close()
        access_store.close()

    assert response.status_code == 503
    assert response.json()["components"][failed_store] == {
        "status": "not_ready",
        "required": True,
    }


# 验证 unhandled error response maps shutdown to 503 场景。
def test_unhandled_error_response_maps_shutdown_to_503():
    resp = _unhandled_error_response(
        RuntimeError("cannot schedule new futures after shutdown")
    )
    payload = json.loads(resp.body)
    assert resp.status_code == 503
    assert payload["error_code"] == "MODEL_UNAVAILABLE"
    assert payload["details"]["error_class"] == "RuntimeError"


# 验证 unhandled error response maps generic to 500 without stack 场景。
def test_unhandled_error_response_maps_generic_to_500_without_stack():
    resp = _unhandled_error_response(ValueError("某个内部细节"))
    payload = json.loads(resp.body)
    assert resp.status_code == 500
    assert payload["error_code"] == "INTERNAL_ERROR"
    # 不回显原始异常信息，不漏栈。
    assert "某个内部细节" not in payload["message"]
    assert "Traceback" not in payload["message"]


# 验证 chat unexpected exception maps to internal error 场景。
@pytest.mark.anyio
async def test_chat_unexpected_exception_maps_to_internal_error(monkeypatch):
    import cogdoc.api.app as app_module

    monkeypatch.setattr(app_module, "configure_logging", lambda: None)

    # 模拟失败结果。
    def boom(doc_id, query, is_local, chat_history, forced_task):
        raise ValueError("非 ChatServiceError 的意外异常")

    app = create_app(chat_runner=boom, session_store=SessionStore())

    async with app.router.lifespan_context(app):
        async with await _client(app, raise_app_exceptions=False) as client:
            resp = await client.post("/v1/chat", json={"query": "问题", "doc_id": "kb"})

    assert resp.status_code == 500
    assert resp.json()["error_code"] == "INTERNAL_ERROR"
