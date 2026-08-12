import hashlib
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from cogdoc.api.access_control import (
    AccessControlMiddleware,
    TokenBucketRateLimiter,
    build_rate_limiter,
)
from cogdoc.api.auth_store import (
    AuthAuthenticationError,
    AuthAuthorizationError,
    AuthStore,
    AuthStoreError,
)
from cogdoc.api.app import create_app
from cogdoc.api.session_store import SessionStore
from cogdoc.api.tenancy import (
    Permission,
    Principal,
    ROLE_PERMISSIONS,
    Role,
    fingerprint_api_key,
    required_permission,
)


# 声明异步测试使用的后端。
@pytest.fixture
def anyio_backend():
    return "asyncio"


# 创建测试应用实例。
def _app(monkeypatch, **kwargs):
    import cogdoc.api.app as app_module

    monkeypatch.setattr(app_module, "configure_logging", lambda: None)

    # 创建测试运行器。
    def runner(doc_id, query, is_local, chat_history, forced_task):
        from cogdoc.service.chat_service import ChatResult

        return ChatResult(
            answer="ok",
            task_type="qa",
            citations=[],
            evidence=[],
            critique="",
            is_valid=True,
            trace_id="t",
            request_id="t",
            steps=[],
            chat_messages=[],
            raw_output={"answer": "ok"},
        )

    return create_app(chat_runner=runner, session_store=SessionStore(), **kwargs)


# 创建测试客户端。
async def _client(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")


def _policy_app(
    *,
    api_keys: set[str] | None = None,
    principals: dict[str, Principal] | None = None,
    rate_limiter: TokenBucketRateLimiter | None = None,
):
    app = FastAPI()

    @app.api_route(
        "/{path:path}",
        methods=["GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE"],
    )
    async def echo_principal(request: Request, path: str):
        principal = request.state.principal
        if principal is None:
            return {"principal": None}
        return {
            "tenant_id": principal.tenant_id,
            "subject_id": principal.subject_id,
            "role": principal.role.value,
            "key_fingerprint": principal.key_fingerprint,
        }

    app.add_middleware(
        AccessControlMiddleware,
        api_keys=set(api_keys or set()),
        principals=principals,
        rate_limiter=rate_limiter
        or TokenBucketRateLimiter(capacity=0, refill_per_second=0.0),
    )
    return app


class _CapturingAudit:
    def __init__(self, *, fail_terminal: bool = False):
        self.events = []
        self.fail_terminal = fail_terminal

    def check(self):
        return True

    def verify(self):
        return True

    def record(self, **event):
        if self.fail_terminal and not event["action"].endswith(".attempt"):
            raise OSError("terminal append failed")
        self.events.append(event)
        return event


class _SessionAuthStore:
    def __init__(self, failure: Exception | None = None):
        self.failure = failure
        self.context = SimpleNamespace(
            principal=Principal.for_user_session(
                tenant_id="workspace-a",
                subject_id="user-a",
                role=Role.VIEWER,
                session_id="session-a",
            )
        )

    def authenticate_session(self, _token: str):
        if self.failure is not None:
            raise self.failure
        return self.context


class _TargetRejectingSessionAuthStore(_SessionAuthStore):
    def authenticate_session(
        self, _token: str, workspace_id: str | None = None
    ):
        if workspace_id is not None:
            raise AuthAuthorizationError("target rejected")
        return self.context


# 限流器单元。


# 验证 token bucket allows burst then throttles 场景。
def test_token_bucket_allows_burst_then_throttles():
    limiter = TokenBucketRateLimiter(capacity=2, refill_per_second=0.0)
    assert limiter.allow("k") is True
    assert limiter.allow("k") is True
    assert limiter.allow("k") is False


# 验证 token bucket isolates identities 场景。
def test_token_bucket_isolates_identities():
    limiter = TokenBucketRateLimiter(capacity=1, refill_per_second=0.0)
    assert limiter.allow("a") is True
    assert limiter.allow("a") is False
    # 另一身份不受影响。
    assert limiter.allow("b") is True


# 验证 token bucket disabled when capacity zero 场景。
def test_token_bucket_disabled_when_capacity_zero():
    limiter = TokenBucketRateLimiter(capacity=0, refill_per_second=0.0)
    assert all(limiter.allow("k") for _ in range(100))


# 验证 identity cap is enforced under distinct flood 场景。
def test_identity_cap_is_enforced_under_distinct_flood():
    # 大量各做一次请求的不同身份（桶都未回满），仍必须把内存压回上限。
    limiter = TokenBucketRateLimiter(
        capacity=5, refill_per_second=0.0, max_identities=10
    )
    for i in range(1000):
        limiter.allow(f"id-{i}")
    assert len(limiter._buckets) <= 10


# 验证 eviction is lru keeps recently active 场景。
def test_eviction_is_lru_keeps_recently_active():
    # 访问会刷新活跃度：淘汰时丢最久未活跃，而非最早创建。
    limiter = TokenBucketRateLimiter(
        capacity=5, refill_per_second=0.0, max_identities=2
    )
    limiter.allow("a")
    limiter.allow("b")
    limiter.allow("a")  # a 重新变为最近活跃
    limiter.allow("c")  # 超额，淘汰最久未活跃的 b
    assert set(limiter._buckets) == {"a", "c"}


# 验证 build rate limiter converts per minute 场景。
def test_build_rate_limiter_converts_per_minute():
    limiter = build_rate_limiter(per_minute=120, burst=60)
    assert limiter.capacity == 60
    assert limiter.refill_per_second == pytest.approx(2.0)


@pytest.mark.anyio
async def test_session_auth_context_is_forwarded_to_downstream_request_state():
    store = _SessionAuthStore()
    app = FastAPI()

    @app.get("/v1/tenant")
    async def authenticated_context(request: Request):
        return {
            "subject_id": request.state.principal.subject_id,
            "same_context": request.state.auth_context is store.context,
        }

    app.add_middleware(
        AccessControlMiddleware,
        api_keys=set(),
        principals=None,
        rate_limiter=TokenBucketRateLimiter(capacity=0, refill_per_second=0.0),
        auth_store=store,
    )
    async with await _client(app) as client:
        response = await client.get(
            "/v1/tenant", headers={"Authorization": "Bearer session-token"}
        )

    assert response.status_code == 200
    assert response.json() == {"subject_id": "user-a", "same_context": True}


@pytest.mark.anyio
async def test_workspace_path_uses_target_role_and_audit_tenant(tmp_path):
    store = AuthStore(str(tmp_path / "auth.db"), scrypt_n=1 << 10)
    audit = _CapturingAudit()
    owner = store.register(
        "target-audit@example.com", "correct horse battery", "Audit Owner"
    )
    target = store.create_workspace(owner["user"]["user_id"], "Target Workspace")
    app = FastAPI()
    app.state.audit_store = audit

    @app.get("/v1/workspaces/{workspace_id}")
    async def target_workspace(request: Request, workspace_id: str):
        return {
            "workspace_id": workspace_id,
            "principal_tenant": request.state.principal.tenant_id,
        }

    app.add_middleware(
        AccessControlMiddleware,
        api_keys=set(),
        principals=None,
        rate_limiter=TokenBucketRateLimiter(capacity=0, refill_per_second=0.0),
        auth_store=store,
    )
    try:
        async with await _client(app) as client:
            response = await client.get(
                f"/v1/workspaces/{target['workspace_id']}",
                headers={"Authorization": f"Bearer {owner['access_token']}"},
            )
    finally:
        store.close()

    assert response.status_code == 200
    assert response.json()["principal_tenant"] == target["workspace_id"]
    assert audit.events[-1]["tenant"] == target["workspace_id"]


@pytest.mark.anyio
async def test_workspace_header_pins_shared_session_and_path_conflicts_fail_closed(
    tmp_path,
):
    store = AuthStore(str(tmp_path / "auth.db"), scrypt_n=1 << 10)
    owner = store.register(
        "two-tabs@example.com", "correct horse battery", "Two Tabs"
    )
    workspace_a = owner["workspace"]["workspace_id"]
    workspace_b = store.create_workspace(owner["user"]["user_id"], "Workspace B")[
        "workspace_id"
    ]
    reached: list[str] = []
    app = FastAPI()

    @app.get("/v1/tenant")
    async def tenant(request: Request):
        reached.append(request.state.principal.tenant_id)
        return {"tenant_id": request.state.principal.tenant_id}

    @app.get("/v1/workspaces/{workspace_id}")
    async def target_workspace(request: Request, workspace_id: str):
        reached.append(request.state.principal.tenant_id)
        return {"workspace_id": workspace_id}

    app.add_middleware(
        AccessControlMiddleware,
        api_keys=set(),
        principals=None,
        rate_limiter=TokenBucketRateLimiter(capacity=0, refill_per_second=0.0),
        auth_store=store,
    )
    token_header = {"Authorization": f"Bearer {owner['access_token']}"}
    try:
        async with await _client(app) as client:
            tab_a = await client.get(
                "/v1/tenant",
                headers={**token_header, "X-CogDoc-Workspace": workspace_a},
            )
            tab_b = await client.get(
                "/v1/tenant",
                headers={**token_header, "X-CogDoc-Workspace": workspace_b},
            )
            tab_a_again = await client.get(
                "/v1/tenant",
                headers={**token_header, "X-CogDoc-Workspace": workspace_a},
            )
            conflict = await client.get(
                f"/v1/workspaces/{workspace_b}",
                headers={**token_header, "X-CogDoc-Workspace": workspace_a},
            )
            malformed = await client.get(
                "/v1/tenant",
                headers={**token_header, "X-CogDoc-Workspace": " workspace-a"},
            )
    finally:
        store.close()

    assert tab_a.json() == tab_a_again.json() == {"tenant_id": workspace_a}
    assert tab_b.json() == {"tenant_id": workspace_b}
    assert conflict.status_code == 404
    assert conflict.json()["error_code"] == "WORKSPACE_NOT_FOUND"
    assert malformed.status_code == 400
    assert malformed.json()["error_code"] == "BAD_REQUEST"
    assert reached == [workspace_a, workspace_b, workspace_a]


@pytest.mark.anyio
async def test_workspace_path_keeps_valid_outsider_opaque_and_invalid_token_401(
    tmp_path,
):
    store = AuthStore(str(tmp_path / "auth.db"), scrypt_n=1 << 10)
    audit = _CapturingAudit()
    owner = store.register("opaque-owner@example.com", "correct horse battery", "Owner")
    outsider = store.register(
        "opaque-outsider@example.com", "correct horse battery", "Outsider"
    )
    target_id = owner["workspace"]["workspace_id"]
    outsider_workspace_id = outsider["workspace"]["workspace_id"]
    reached: list[str] = []
    app = FastAPI()
    app.state.audit_store = audit

    @app.get("/v1/workspaces/{workspace_id}")
    async def target_workspace(workspace_id: str):
        reached.append(workspace_id)
        return {"workspace_id": workspace_id}

    app.add_middleware(
        AccessControlMiddleware,
        api_keys=set(),
        principals=None,
        rate_limiter=TokenBucketRateLimiter(capacity=0, refill_per_second=0.0),
        auth_store=store,
    )
    try:
        async with await _client(app) as client:
            foreign = await client.get(
                f"/v1/workspaces/{target_id}",
                headers={"Authorization": f"Bearer {outsider['access_token']}"},
            )
            missing = await client.get(
                "/v1/workspaces/wsp_definitely-missing",
                headers={"Authorization": f"Bearer {outsider['access_token']}"},
            )
            invalid = await client.get(
                f"/v1/workspaces/{target_id}",
                headers={"Authorization": "Bearer invalid-session-token"},
            )
    finally:
        store.close()

    assert foreign.status_code == missing.status_code == 404
    assert foreign.json() == missing.json()
    assert foreign.json()["message"] == "工作区不存在"
    assert foreign.json()["error_code"] == "WORKSPACE_NOT_FOUND"
    assert invalid.status_code == 401
    assert invalid.json()["error_code"] == "UNAUTHORIZED"
    assert reached == []
    assert len(audit.events) == 2
    assert {event["tenant"] for event in audit.events} == {outsider_workspace_id}
    assert {event["principal"] for event in audit.events} == {
        outsider["user"]["user_id"]
    }
    assert {event["status"] for event in audit.events} == {404}


@pytest.mark.anyio
async def test_workspace_path_supports_one_argument_session_provider_fail_closed():
    store = _SessionAuthStore()
    app = FastAPI()

    @app.get("/v1/workspaces/{workspace_id}")
    async def target_workspace(request: Request, workspace_id: str):
        return {
            "workspace_id": workspace_id,
            "principal_tenant": request.state.principal.tenant_id,
        }

    app.add_middleware(
        AccessControlMiddleware,
        api_keys=set(),
        principals=None,
        rate_limiter=TokenBucketRateLimiter(capacity=0, refill_per_second=0.0),
        auth_store=store,
    )
    async with await _client(app) as client:
        active = await client.get(
            "/v1/workspaces/workspace-a",
            headers={"Authorization": "Bearer session-token"},
        )
        unknown = await client.get(
            "/v1/workspaces/workspace-b",
            headers={"Authorization": "Bearer session-token"},
        )

    assert active.status_code == 200
    assert active.json()["principal_tenant"] == "workspace-a"
    assert unknown.status_code == 404
    assert unknown.json()["error_code"] == "WORKSPACE_NOT_FOUND"


@pytest.mark.anyio
async def test_target_provider_rejection_stays_fail_closed_after_token_validation():
    app = FastAPI()

    @app.get("/v1/workspaces/{workspace_id}")
    async def target_workspace(workspace_id: str):
        return {"workspace_id": workspace_id}

    app.add_middleware(
        AccessControlMiddleware,
        api_keys=set(),
        principals=None,
        rate_limiter=TokenBucketRateLimiter(capacity=0, refill_per_second=0.0),
        auth_store=_TargetRejectingSessionAuthStore(),
    )
    async with await _client(app) as client:
        response = await client.get(
            "/v1/workspaces/workspace-a",
            headers={"Authorization": "Bearer session-token"},
        )

    assert response.status_code == 404
    assert response.json()["error_code"] == "WORKSPACE_NOT_FOUND"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("failure", "status_code", "error_code"),
    [
        (AuthAuthenticationError("invalid session"), 401, "UNAUTHORIZED"),
        (AuthStoreError("database unavailable"), 503, "INTERNAL_ERROR"),
    ],
)
async def test_session_auth_separates_invalid_credentials_from_backend_failure(
    failure, status_code, error_code
):
    app = FastAPI()
    app.add_middleware(
        AccessControlMiddleware,
        api_keys=set(),
        principals=None,
        rate_limiter=TokenBucketRateLimiter(capacity=0, refill_per_second=0.0),
        auth_store=_SessionAuthStore(failure),
    )
    async with await _client(app) as client:
        response = await client.get(
            "/v1/tenant", headers={"Authorization": "Bearer session-token"}
        )

    assert response.status_code == status_code
    assert response.json()["error_code"] == error_code


# 租户身份与集中权限策略。


def test_role_permission_matrix_is_explicit_and_least_privilege():
    assert ROLE_PERMISSIONS[Role.VIEWER] == {
        Permission.READ,
        Permission.QUERY,
    }
    assert ROLE_PERMISSIONS[Role.REVIEWER] == {
        Permission.READ,
        Permission.QUERY,
        Permission.REVIEW,
        Permission.PUBLISH,
    }
    assert ROLE_PERMISSIONS[Role.EDITOR] == {
        Permission.READ,
        Permission.QUERY,
        Permission.WRITE,
    }
    assert Permission.DELETE in ROLE_PERMISSIONS[Role.ADMIN]
    assert Permission.MANAGE_ACCESS in ROLE_PERMISSIONS[Role.ADMIN]
    assert Permission.MANAGE_TENANT not in ROLE_PERMISSIONS[Role.ADMIN]
    assert ROLE_PERMISSIONS[Role.OWNER] == frozenset(Permission)


@pytest.mark.parametrize(
    ("method", "path", "expected"),
    [
        ("GET", "/v1/knowledge-bases", Permission.READ),
        ("POST", "/v1/chat", Permission.QUERY),
        ("POST", "/v1/knowledge-bases", Permission.WRITE),
        ("DELETE", "/v1/knowledge-bases/kb", Permission.DELETE),
        ("GET", "/v1/review-queue", Permission.REVIEW),
        ("POST", "/v1/knowledge/k-1/approve", Permission.REVIEW),
        ("PUT", "/v1/research-jobs/r-1/review", Permission.REVIEW),
        ("POST", "/v1/research-jobs/r-1/publish", Permission.PUBLISH),
        ("GET", "/v1/retrieval-eval-drafts/d-1", Permission.REVIEW),
        ("POST", "/v1/principals", Permission.MANAGE_ACCESS),
        ("GET", "/v1/tenants/t-1", Permission.MANAGE_TENANT),
        ("CONNECT", "/v1/unknown", Permission.MANAGE_TENANT),
    ],
)
def test_required_permission_uses_ordered_method_path_policy(method, path, expected):
    assert required_permission(method, path) is expected


def test_principal_factory_normalizes_role_and_never_retains_raw_key():
    principal = Principal.for_api_key(
        "top-secret",
        tenant_id=" tenant-a ",
        subject_id=" alice ",
        role="editor",
    )
    assert principal.tenant_id == "tenant-a"
    assert principal.subject_id == "alice"
    assert principal.role is Role.EDITOR
    assert principal.key_fingerprint == fingerprint_api_key("top-secret")
    assert "top-secret" not in principal.key_fingerprint
    assert principal.rate_limit_identity == "tenant-a\x1falice"


def test_explicit_principal_mapping_rejects_mismatched_fingerprint():
    principal = Principal.for_api_key(
        "right-key", tenant_id="tenant-a", subject_id="alice", role=Role.VIEWER
    )
    with pytest.raises(ValueError, match="key_fingerprint"):
        AccessControlMiddleware(
            FastAPI(),
            api_keys=set(),
            principals={"wrong-key": principal},
            rate_limiter=TokenBucketRateLimiter(capacity=0, refill_per_second=0.0),
        )


# 鉴权。


# 验证 auth disabled when no keys 场景。
@pytest.mark.anyio
async def test_auth_disabled_when_no_keys(monkeypatch):
    app = _app(monkeypatch, api_keys=set())
    async with app.router.lifespan_context(app):
        async with await _client(app) as c:
            resp = await c.post("/v1/chat", json={"query": "q", "doc_id": "kb"})
    assert resp.status_code == 200


@pytest.mark.anyio
async def test_auth_disabled_injects_default_local_owner():
    app = _policy_app()
    async with await _client(app) as c:
        response = await c.delete("/v1/knowledge-bases/kb")
    assert response.status_code == 200
    assert response.json() == {
        "tenant_id": "default",
        "subject_id": "local",
        "role": "owner",
        "key_fingerprint": "auth-disabled",
    }


@pytest.mark.anyio
async def test_legacy_api_key_injects_default_admin_principal():
    app = _policy_app(api_keys={"legacy-secret"})
    async with await _client(app) as c:
        response = await c.get(
            "/v1/knowledge-bases", headers={"X-API-Key": "legacy-secret"}
        )
    assert response.status_code == 200
    body = response.json()
    assert body["tenant_id"] == "default"
    assert body["role"] == "admin"
    assert body["subject_id"].startswith("api-key:sha256:")
    assert body["key_fingerprint"] == fingerprint_api_key("legacy-secret")


@pytest.mark.anyio
async def test_explicit_principal_is_injected_into_request_state():
    principal = Principal.for_api_key(
        "viewer-secret",
        tenant_id="tenant-a",
        subject_id="alice",
        role=Role.VIEWER,
    )
    app = _policy_app(principals={"viewer-secret": principal})
    async with await _client(app) as c:
        response = await c.get(
            "/v1/knowledge-bases", headers={"Authorization": "Bearer viewer-secret"}
        )
    assert response.status_code == 200
    assert response.json() == {
        "tenant_id": "tenant-a",
        "subject_id": "alice",
        "role": "viewer",
        "key_fingerprint": fingerprint_api_key("viewer-secret"),
    }


@pytest.mark.anyio
async def test_audit_hashes_client_request_id_before_persistence():
    principal = Principal.for_api_key(
        "owner-secret",
        tenant_id="tenant-a",
        subject_id="alice",
        role=Role.OWNER,
    )
    app = _policy_app(principals={"owner-secret": principal})
    audit = _CapturingAudit()
    app.state.audit_store = audit
    raw_request_id = "owner-secret-must-never-be-persisted"
    async with await _client(app) as client:
        response = await client.post(
            "/v1/knowledge-bases",
            headers={
                "X-API-Key": "owner-secret",
                "X-Request-ID": raw_request_id,
            },
        )

    expected = hashlib.sha256(
        b"cogdoc-audit-request-id-v1\0" + raw_request_id.encode()
    ).hexdigest()
    assert response.status_code == 200
    assert len(audit.events) == 2
    assert {event["request_id"] for event in audit.events} == {
        f"client-sha256:{expected}"
    }
    assert {event["path"] for event in audit.events} == {"/v1/knowledge-bases"}
    assert all(
        event["resource"]["path_sha256"]
        and event["resource"]["route_family"] == "/v1/knowledge-bases"
        for event in audit.events
    )
    assert raw_request_id not in repr(audit.events)


@pytest.mark.anyio
async def test_terminal_audit_failure_replaces_uncommitted_response_with_503():
    principal = Principal.for_api_key(
        "owner-secret",
        tenant_id="tenant-a",
        subject_id="alice",
        role=Role.OWNER,
    )
    app = _policy_app(principals={"owner-secret": principal})
    audit = _CapturingAudit(fail_terminal=True)
    app.state.audit_store = audit
    async with await _client(app) as client:
        response = await client.post(
            "/v1/knowledge-bases", headers={"X-API-Key": "owner-secret"}
        )

    assert response.status_code == 503
    assert response.json()["error_code"] == "INTERNAL_ERROR"
    assert [event["status"] for event in audit.events] == [102]


@pytest.mark.anyio
async def test_explicit_principal_overrides_legacy_identity_for_same_key():
    principal = Principal.for_api_key(
        "shared-secret",
        tenant_id="tenant-a",
        subject_id="alice",
        role=Role.VIEWER,
    )
    app = _policy_app(
        api_keys={"shared-secret"}, principals={"shared-secret": principal}
    )
    async with await _client(app) as c:
        accepted = await c.get(
            "/v1/knowledge-bases", headers={"X-API-Key": "shared-secret"}
        )
        forbidden = await c.post(
            "/v1/knowledge-bases", headers={"X-API-Key": "shared-secret"}
        )
    assert accepted.status_code == 200
    assert accepted.json()["tenant_id"] == "tenant-a"
    assert accepted.json()["role"] == "viewer"
    assert forbidden.status_code == 403


@pytest.mark.anyio
async def test_principals_only_configuration_rejects_missing_and_unknown_keys():
    principal = Principal.for_api_key(
        "known", tenant_id="tenant-a", subject_id="alice", role=Role.VIEWER
    )
    app = _policy_app(principals={"known": principal})
    async with await _client(app) as c:
        missing = await c.get("/v1/knowledge-bases")
        unknown = await c.get("/v1/knowledge-bases", headers={"X-API-Key": "unknown"})
    assert missing.status_code == 401
    assert unknown.status_code == 401


@pytest.mark.anyio
async def test_viewer_can_read_and_query_but_cannot_mutate_delete_or_review():
    principal = Principal.for_api_key(
        "viewer", tenant_id="tenant-a", subject_id="alice", role=Role.VIEWER
    )
    app = _policy_app(principals={"viewer": principal})
    headers = {"X-API-Key": "viewer"}
    async with await _client(app) as c:
        read = await c.get("/v1/knowledge-bases", headers=headers)
        query = await c.post("/v1/chat", headers=headers)
        write = await c.post("/v1/knowledge-bases", headers=headers)
        delete = await c.delete("/v1/knowledge-bases/kb", headers=headers)
        review = await c.get("/v1/review-queue", headers=headers)
    assert read.status_code == 200
    assert query.status_code == 200
    for forbidden in (write, delete, review):
        assert forbidden.status_code == 403
        assert forbidden.json()["error_code"] == "FORBIDDEN"


@pytest.mark.parametrize(
    ("role", "method", "path", "expected_status"),
    [
        (Role.REVIEWER, "POST", "/v1/knowledge/k-1/approve", 200),
        (Role.REVIEWER, "POST", "/v1/research-jobs/r-1/publish", 200),
        (Role.REVIEWER, "POST", "/v1/knowledge-bases", 403),
        (Role.EDITOR, "POST", "/v1/knowledge-bases", 200),
        (Role.EDITOR, "PUT", "/v1/research-jobs/r-1/review", 403),
        (Role.EDITOR, "DELETE", "/v1/knowledge-bases/kb", 403),
        (Role.ADMIN, "DELETE", "/v1/knowledge-bases/kb", 200),
        (Role.ADMIN, "POST", "/v1/principals", 200),
        (Role.ADMIN, "GET", "/v1/tenants/tenant-a", 403),
        (Role.OWNER, "GET", "/v1/tenants/tenant-a", 200),
    ],
)
@pytest.mark.anyio
async def test_role_policy_enforced(role, method, path, expected_status):
    api_key = f"key-{role.value}-{method}-{path}"
    principal = Principal.for_api_key(
        api_key,
        tenant_id="tenant-a",
        subject_id=role.value,
        role=role,
    )
    app = _policy_app(principals={api_key: principal})
    async with await _client(app) as c:
        response = await c.request(method, path, headers={"X-API-Key": api_key})
    assert response.status_code == expected_status
    if expected_status == 403:
        assert response.json()["error_code"] == "FORBIDDEN"


# 验证 startup warns when auth disabled 场景。
@pytest.mark.anyio
async def test_startup_warns_when_auth_disabled(monkeypatch):
    import cogdoc.api.app as app_module

    monkeypatch.setattr(app_module, "configure_logging", lambda: None)
    events = []
    monkeypatch.setattr(app_module, "log_event", lambda *a, **k: events.append((a, k)))
    app = _app(monkeypatch, api_keys=set())
    async with app.router.lifespan_context(app):
        pass
    # 鉴权关闭时启动应发一条 auth_disabled 告警。
    assert any(a[:2] == ("startup", "auth_disabled") for a, _ in events)


# 验证 no startup warning when auth enabled 场景。
@pytest.mark.anyio
async def test_no_startup_warning_when_auth_enabled(monkeypatch):
    import cogdoc.api.app as app_module

    monkeypatch.setattr(app_module, "configure_logging", lambda: None)
    events = []
    monkeypatch.setattr(app_module, "log_event", lambda *a, **k: events.append((a, k)))
    app = _app(monkeypatch, api_keys={"secret"})
    async with app.router.lifespan_context(app):
        pass
    assert not any(a[:2] == ("startup", "auth_disabled") for a, _ in events)


# 验证 missing key rejected 401 场景。
@pytest.mark.anyio
async def test_missing_key_rejected_401(monkeypatch):
    app = _app(monkeypatch, api_keys={"secret"})
    async with app.router.lifespan_context(app):
        async with await _client(app) as c:
            resp = await c.post("/v1/chat", json={"query": "q", "doc_id": "kb"})
    assert resp.status_code == 401
    assert resp.json()["error_code"] == "UNAUTHORIZED"


# 验证 wrong key rejected 401 场景。
@pytest.mark.anyio
async def test_wrong_key_rejected_401(monkeypatch):
    app = _app(monkeypatch, api_keys={"secret"})
    async with app.router.lifespan_context(app):
        async with await _client(app) as c:
            resp = await c.post(
                "/v1/chat",
                json={"query": "q", "doc_id": "kb"},
                headers={"Authorization": "Bearer nope"},
            )
    assert resp.status_code == 401


# 验证 bearer and x api key accepted 场景。
@pytest.mark.anyio
async def test_bearer_and_x_api_key_accepted(monkeypatch):
    app = _app(monkeypatch, api_keys={"secret"})
    async with app.router.lifespan_context(app):
        async with await _client(app) as c:
            via_bearer = await c.post(
                "/v1/chat",
                json={"query": "q", "doc_id": "kb"},
                headers={"Authorization": "Bearer secret"},
            )
            via_header = await c.post(
                "/v1/chat",
                json={"query": "q", "doc_id": "kb"},
                headers={"X-API-Key": "secret"},
            )
    assert via_bearer.status_code == 200
    assert via_header.status_code == 200


# 验证 health endpoints exempt from auth 场景。
@pytest.mark.anyio
async def test_health_endpoints_exempt_from_auth(monkeypatch):
    app = _app(monkeypatch, api_keys={"secret"})
    async with app.router.lifespan_context(app):
        async with await _client(app) as c:
            healthz = await c.get("/healthz")
            health_live = await c.get("/health/live")
            readyz = await c.get("/readyz")
            health_ready = await c.get("/health/ready")
    # 没带 key 也能过探针。
    assert healthz.status_code == 200
    assert health_live.status_code == 200
    assert readyz.status_code in (200, 503)
    assert health_ready.status_code in (200, 503)


# 端到端限流。


# 验证 rate limit returns 429 after capacity 场景。
@pytest.mark.anyio
async def test_rate_limit_returns_429_after_capacity(monkeypatch):
    limiter = TokenBucketRateLimiter(capacity=2, refill_per_second=0.0)
    app = _app(monkeypatch, api_keys=set(), rate_limiter=limiter)
    async with app.router.lifespan_context(app):
        async with await _client(app) as c:
            body = {"query": "q", "doc_id": "kb"}
            first = await c.post("/v1/chat", json=body)
            second = await c.post("/v1/chat", json=body)
            third = await c.post("/v1/chat", json=body)
    assert first.status_code == 200
    assert second.status_code == 200
    assert third.status_code == 429
    assert third.json()["error_code"] == "REQUEST_THROTTLED"


# 验证 job polling exempt from rate limit 场景。
@pytest.mark.anyio
async def test_job_polling_exempt_from_rate_limit(monkeypatch):
    # 即便桶极小，入库 job 状态轮询也不该被限流（否则长任务轮询会误判失败）。
    limiter = TokenBucketRateLimiter(capacity=1, refill_per_second=0.0)
    app = _app(monkeypatch, api_keys=set(), rate_limiter=limiter)
    async with app.router.lifespan_context(app):
        async with await _client(app) as c:
            statuses = [
                (await c.get("/v1/index-jobs/whatever")).status_code for _ in range(10)
            ]
    # 全部 404（job 不存在）而非 429，证明没走限流。
    assert all(code == 404 for code in statuses)


# 验证 job polling still requires auth 场景。
@pytest.mark.anyio
async def test_job_polling_still_requires_auth(monkeypatch):
    # 限流豁免不等于鉴权豁免：开了 key 还是要带。
    app = _app(monkeypatch, api_keys={"secret"})
    async with app.router.lifespan_context(app):
        async with await _client(app) as c:
            unauth = await c.get("/v1/index-jobs/whatever")
            authed = await c.get(
                "/v1/index-jobs/whatever", headers={"X-API-Key": "secret"}
            )
    assert unauth.status_code == 401
    assert authed.status_code == 404


# 验证 rate limit is per key 场景。
@pytest.mark.anyio
async def test_rate_limit_is_per_key(monkeypatch):
    limiter = TokenBucketRateLimiter(capacity=1, refill_per_second=0.0)
    app = _app(monkeypatch, api_keys={"k1", "k2"}, rate_limiter=limiter)
    async with app.router.lifespan_context(app):
        async with await _client(app) as c:
            body = {"query": "q", "doc_id": "kb"}
            k1_first = await c.post("/v1/chat", json=body, headers={"X-API-Key": "k1"})
            k1_second = await c.post("/v1/chat", json=body, headers={"X-API-Key": "k1"})
            k2_first = await c.post("/v1/chat", json=body, headers={"X-API-Key": "k2"})
    assert k1_first.status_code == 200
    assert k1_second.status_code == 429
    # 另一个 key 的额度独立，不受 k1 耗尽影响。
    assert k2_first.status_code == 200


@pytest.mark.anyio
async def test_rate_limit_is_shared_by_same_tenant_and_subject():
    limiter = TokenBucketRateLimiter(capacity=1, refill_per_second=0.0)
    first = Principal.for_api_key(
        "alice-key-1",
        tenant_id="tenant-a",
        subject_id="alice",
        role=Role.VIEWER,
    )
    rotated = Principal.for_api_key(
        "alice-key-2",
        tenant_id="tenant-a",
        subject_id="alice",
        role=Role.VIEWER,
    )
    app = _policy_app(
        principals={"alice-key-1": first, "alice-key-2": rotated},
        rate_limiter=limiter,
    )
    async with await _client(app) as c:
        first_response = await c.post("/v1/chat", headers={"X-API-Key": "alice-key-1"})
        rotated_response = await c.post(
            "/v1/chat", headers={"X-API-Key": "alice-key-2"}
        )
    assert first_response.status_code == 200
    assert rotated_response.status_code == 429


@pytest.mark.anyio
async def test_rate_limit_isolates_same_subject_across_tenants():
    limiter = TokenBucketRateLimiter(capacity=1, refill_per_second=0.0)
    tenant_a = Principal.for_api_key(
        "tenant-a-key",
        tenant_id="tenant-a",
        subject_id="alice",
        role=Role.VIEWER,
    )
    tenant_b = Principal.for_api_key(
        "tenant-b-key",
        tenant_id="tenant-b",
        subject_id="alice",
        role=Role.VIEWER,
    )
    app = _policy_app(
        principals={"tenant-a-key": tenant_a, "tenant-b-key": tenant_b},
        rate_limiter=limiter,
    )
    async with await _client(app) as c:
        response_a = await c.post("/v1/chat", headers={"X-API-Key": "tenant-a-key"})
        response_b = await c.post("/v1/chat", headers={"X-API-Key": "tenant-b-key"})
    assert response_a.status_code == 200
    assert response_b.status_code == 200
