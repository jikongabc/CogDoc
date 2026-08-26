import hashlib
import inspect
import sqlite3
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from threading import Lock

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from cogdoc.api.auth_store import (
    AuthAuthenticationError,
    AuthAuthorizationError,
    AuthStoreError,
)
from cogdoc.api.error_mapping import status_for_code
from cogdoc.api.offload import run_sync
from cogdoc.api.schemas import ErrorCode, build_error_response
from cogdoc.api.tenancy import Principal, Role, fingerprint_api_key, required_permission


# 探针与文档路径永远放行：鉴权/限流不能挡住存活就绪检查与 OpenAPI。
_EXEMPT_PATHS = frozenset(
    {
        "/healthz",
        "/health/live",
        "/readyz",
        "/health/ready",
        "/metrics",
        "/docs",
        "/redoc",
        "/openapi.json",
    }
)
_PUBLIC_AUTH_PATHS = frozenset(
    {
        "/v1/auth/config",
        "/v1/auth/register",
        "/v1/auth/login",
        "/v1/auth/oidc/authorize",
        "/v1/auth/oidc/callback",
        "/v1/auth/oidc/exchange",
        "/v1/auth/invitations/accept",
        "/v1/auth/connector-oauth/callback/notion",
        "/v1/auth/connector-oauth/callback/atlassian",
        "/v1/auth/connector-oauth/callback/microsoft",
    }
)
WORKSPACE_HEADER = "X-CogDoc-Workspace"
# 仅豁免限流（仍走鉴权）：前端刷新/轮询会高频读取这些轻量状态接口。
_RATE_LIMIT_EXEMPT_GET_PATHS = frozenset(
    (
        "/v1/knowledge-bases",
        "/v1/index-jobs",
        "/v1/sync-jobs",
        "/v1/research-jobs/summaries",
        "/v1/ha/jobs",
        "/v1/audit-events/exports",
        "/v1/sessions",
        "/v1/traces",
    )
)
_SCIM_PREFIX = "/scim/v2"
_RATE_LIMIT_EXEMPT_GET_PREFIXES = (
    "/v1/index-jobs/",
    "/v1/knowledge-bases/",
    "/v1/sessions/",
    "/v1/traces/",
)


class _AuthenticationBackendUnavailable(RuntimeError):
    """Internal sentinel separating invalid credentials from store failures."""


def _compatible_authenticate_session(
    authenticate: Callable[..., object],
    token: str,
    workspace_id: str | None,
) -> object:
    """Invoke current and trusted legacy session-provider signatures once.

    Signature binding happens before invocation so a ``TypeError`` raised by
    the provider itself remains a backend failure and is never replayed as a
    second authentication attempt.  A legacy one-argument provider can only
    attest its active workspace; callers must compare the returned principal
    with an explicit target before granting access.
    """

    variants: list[tuple[tuple[object, ...], dict[str, object]]] = []
    if workspace_id is not None:
        variants.extend(
            (
                ((token, workspace_id), {}),
                ((), {"token": token, "workspace_id": workspace_id}),
                ((), {"session_token": token, "workspace_id": workspace_id}),
            )
        )
    variants.extend(
        (
            ((token,), {}),
            ((), {"token": token}),
            ((), {"session_token": token}),
        )
    )
    try:
        signature = inspect.signature(authenticate)
    except (TypeError, ValueError):
        arguments = (token, workspace_id) if workspace_id is not None else (token,)
        return authenticate(*arguments)
    for args, kwargs in variants:
        try:
            signature.bind(*args, **kwargs)
        except TypeError:
            continue
        return authenticate(*args, **kwargs)
    raise TypeError("unsupported authenticate_session provider signature")


# 判断ratelimitexempt。
def _is_rate_limit_exempt(request: Request) -> bool:
    if request.method != "GET":
        return False
    path = request.url.path
    return path in _RATE_LIMIT_EXEMPT_GET_PATHS or path.startswith(
        _RATE_LIMIT_EXEMPT_GET_PREFIXES
    )


# 按身份分桶的令牌桶：突发容量 capacity，恒定速率 refill_per_second 补充。
class TokenBucketRateLimiter:
    # 按身份分桶的令牌桶：突发容量 capacity，恒定速率 refill_per_second 补充。
    def __init__(
        self,
        capacity: int,
        refill_per_second: float,
        max_identities: int = 10000,
    ):
        self.capacity = capacity
        self.refill_per_second = refill_per_second
        self.max_identities = max_identities
        # OrderedDict 维护按最近访问排序：末尾最新、头部最旧，淘汰 popitem(last=False) 为 O(1)。
        self._buckets: "OrderedDict[str, tuple[float, float]]" = OrderedDict()
        self._lock = Lock()

    # 放行结果。
    def allow(self, identity: str) -> bool:
        # capacity<=0 表示关闭限流，直接放行。
        if self.capacity <= 0:
            return True
        now = time.monotonic()
        with self._lock:
            tokens, last = self._buckets.get(identity, (float(self.capacity), now))
            tokens = min(self.capacity, tokens + (now - last) * self.refill_per_second)
            allowed = tokens >= 1.0
            if allowed:
                tokens -= 1.0
            self._buckets[identity] = (tokens, now)
            self._buckets.move_to_end(identity)  # 标记为最近活跃
            # 内存无条件有界：超额时从头部（最久未活跃）逐个淘汰，O(overflow) 无需排序。
            while len(self._buckets) > self.max_identities:
                self._buckets.popitem(last=False)
            return allowed


# 完成 提取流程API密钥 处理。
def _extract_api_key(request: Request) -> str | None:
    # 先认 Authorization: Bearer，再退回 X-API-Key。
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip() or None
    key = request.headers.get("x-api-key", "")
    return key.strip() or None


def _target_workspace_id(path: str) -> str | None:
    """Return the explicit workspace scope for account-management routes.

    A human session can belong to several workspaces.  Resolving the target at
    the middleware boundary makes the RBAC decision and audit tenant match the
    workspace that the route is about, instead of whichever workspace happened
    to be active on the previous request.
    """

    parts = path.split("/")
    if len(parts) < 4 or parts[:3] != ["", "v1", "workspaces"]:
        return None
    candidate = parts[3]
    if (
        not candidate
        or len(candidate) > 160
        or any(ord(character) < 33 or ord(character) == 127 for character in candidate)
    ):
        return None
    return candidate


def _header_workspace_id(request: Request) -> str | None:
    """Return an exact canonical workspace selector supplied by a client tab."""

    raw = request.headers.get(WORKSPACE_HEADER)
    if raw is None:
        return None
    if (
        not raw
        or raw != raw.strip()
        or len(raw) > 160
        or any(ord(character) < 33 or ord(character) == 127 for character in raw)
    ):
        raise ValueError("invalid workspace header")
    return raw


# 拒绝结果。
def _reject(
    code: ErrorCode, message: str, *, status_code: int | None = None
) -> JSONResponse:
    error = build_error_response(code, message)
    return JSONResponse(
        status_code=status_for_code(code) if status_code is None else status_code,
        content=error.model_dump(),
    )


def _legacy_principal(api_key: str) -> Principal:
    fingerprint = fingerprint_api_key(api_key)
    return Principal(
        tenant_id="default",
        subject_id=f"api-key:{fingerprint}",
        role=Role.ADMIN,
        key_fingerprint=fingerprint,
    )


def _principal_registry(
    api_keys: set[str],
    principals: Mapping[str, Principal] | None,
) -> dict[str, Principal]:
    """Build a fingerprint-keyed registry without retaining raw credentials.

    The explicit mapping contract is ``{raw_api_key: Principal}``.  Explicit
    entries override the legacy ``api_keys`` identity for the same credential,
    but their declared fingerprint must match that credential.
    """

    registry = {
        principal.key_fingerprint: principal
        for principal in (_legacy_principal(api_key) for api_key in api_keys)
    }
    for api_key, principal in (principals or {}).items():
        if not isinstance(principal, Principal):
            raise TypeError("principals values must be Principal instances")
        fingerprint = fingerprint_api_key(api_key)
        if principal.key_fingerprint != fingerprint:
            raise ValueError(
                "principal key_fingerprint does not match its API-key mapping"
            )
        registry[fingerprint] = principal
    return registry


def _audit_action(method: str, path: str) -> str:
    permission = required_permission(method, path).value
    parts = [part for part in path.split("/") if part]
    resource = parts[1] if len(parts) > 1 and parts[0] == "v1" else "http"
    return f"{resource}.{permission}"


def _audit_route_metadata(path: str) -> tuple[str, dict[str, str]]:
    """Return a non-sensitive route family plus an irreversible path handle."""

    parts = [part for part in path.split("/") if part]
    if len(parts) > 1 and parts[0] == "v1":
        family = f"/v1/{parts[1]}"
    else:
        family = "/http"
    path_hash = hashlib.sha256(
        b"cogdoc-audit-path-v1\0" + path.encode("utf-8", errors="replace")
    ).hexdigest()
    return family, {"route_family": family, "path_sha256": path_hash}


async def _send_and_audit(
    app: ASGIApp,
    scope: Scope,
    receive: Receive,
    send: Send,
    *,
    principal: Principal,
    record_attempt: bool = True,
) -> None:
    status_code = 500
    terminal_recorded = False
    app_state = getattr(scope.get("app"), "state", None)
    audit_store = getattr(app_state, "audit_store", None)
    path = str(scope.get("path") or "/")
    audit_path, audit_resource = _audit_route_metadata(path)
    method = str(scope.get("method") or "").upper()
    request_id = None
    for raw_name, raw_value in scope.get("headers", ()):
        if raw_name.lower() == b"x-request-id":
            # The header is client-controlled and may itself contain a secret.
            # Preserve correlation without ever persisting its raw value.
            digest = hashlib.sha256(
                b"cogdoc-audit-request-id-v1\0" + raw_value
            ).hexdigest()
            request_id = f"client-sha256:{digest}"
            break

    async def audit_call(operation, *args, **kwargs):
        executor = getattr(app_state, "offload_executor", None)
        if executor is not None and not getattr(executor, "_shutdown", False):
            return await run_sync(executor, operation, *args, **kwargs)
        return operation(*args, **kwargs)

    if audit_store is not None:
        try:
            # Every mutation boundary re-reads and verifies the complete chain;
            # read-only requests use the stat-signature fast path.
            verifier = (
                audit_store.check
                if method in {"GET", "HEAD", "OPTIONS"}
                else audit_store.verify
            )
            await audit_call(verifier)
            if record_attempt and method not in {"GET", "HEAD", "OPTIONS"}:
                await audit_call(
                    audit_store.record,
                    tenant=principal.tenant_id,
                    principal=principal.subject_id,
                    action=f"{_audit_action(method, path)}.attempt",
                    method=method,
                    path=audit_path,
                    status=102,
                    resource=audit_resource,
                    result={"outcome": "started"},
                    request_id=request_id,
                )
        except Exception:
            response = _reject(
                ErrorCode.INTERNAL_ERROR,
                "审计存储不可用，操作已安全拒绝",
                status_code=503,
            )
            await response(scope, receive, send)
            return

    async def record_terminal() -> None:
        nonlocal terminal_recorded
        if audit_store is None or terminal_recorded:
            return
        await audit_call(
            audit_store.record,
            tenant=principal.tenant_id,
            principal=principal.subject_id,
            action=_audit_action(method, path),
            method=method,
            path=audit_path,
            status=status_code,
            resource=audit_resource,
            result={
                "outcome": "allowed" if status_code < 400 else "failed",
                "phase": "response_commit",
            },
            request_id=request_id,
        )
        terminal_recorded = True

    class _AuditCommitRejected(RuntimeError):
        pass

    async def capture(message):
        nonlocal status_code
        if message.get("type") == "http.response.start":
            status_code = int(message.get("status") or 500)
            try:
                # Persist the terminal HTTP decision before a success/error is
                # observable by the client.  A failed append therefore yields
                # a 503 and poisons subsequent operations instead of silently
                # returning an unaudited success.
                await record_terminal()
            except Exception as exc:
                response = _reject(
                    ErrorCode.INTERNAL_ERROR,
                    "审计终态写入失败，响应已安全拒绝",
                    status_code=503,
                )
                await response(scope, receive, send)
                raise _AuditCommitRejected from exc
        await send(message)

    try:
        await app(scope, receive, capture)
    except _AuditCommitRejected:
        return
    except Exception:
        # FastAPI normally converts application errors into an HTTP response,
        # which is audited by ``capture``.  A raw ASGI exception still receives
        # a durable terminal record before it can cross this boundary.
        if audit_store is not None and not terminal_recorded:
            status_code = 500
            await record_terminal()
        raise
    if audit_store is not None and not terminal_recorded:
        # A non-conforming ASGI app returned without starting a response. Keep
        # the audit chain complete even though the server will reject it.
        await record_terminal()


# 统一入口的鉴权 + 限流：先校验 API key，再按身份限流，最后放行到路由。
class AccessControlMiddleware:
    # 统一入口的鉴权 + 限流：先校验 API key，再按身份限流，最后放行到路由。
    def __init__(
        self,
        app: ASGIApp,
        *,
        api_keys: set[str],
        rate_limiter: TokenBucketRateLimiter,
        principals: Mapping[str, Principal] | None = None,
        auth_store=None,
    ):
        self.app = app
        self._principals = _principal_registry(set(api_keys), principals)
        self.auth_enabled = bool(self._principals or auth_store is not None)
        self._local_principal = Principal.local_owner()
        self._limiter = rate_limiter
        self._auth_store = auth_store

    async def _authenticate(
        self,
        api_key: str | None,
        app_state: object,
        *,
        workspace_id: str | None = None,
    ) -> tuple[Principal | None, object | None, bool]:
        if not self.auth_enabled:
            return self._local_principal, None, True
        if api_key is None:
            return None, None, False
        principal = self._principals.get(fingerprint_api_key(api_key))
        if principal is not None:
            target_authorized = (
                workspace_id is None or principal.tenant_id == workspace_id
            )
            return principal, None, target_authorized
        if self._auth_store is None:
            return None, None, False

        is_service_token = api_key.startswith("cog_svc_")
        authenticate = getattr(
            self._auth_store,
            "authenticate_service_token"
            if is_service_token
            else "authenticate_session",
            None,
        )
        if not callable(authenticate):
            return None, None, False
        executor = getattr(app_state, "offload_executor", None)

        async def authenticate_for(target: str | None) -> object:
            if executor is not None and not getattr(executor, "_shutdown", False):
                return await run_sync(
                    executor,
                    _compatible_authenticate_session,
                    authenticate,
                    api_key,
                    target,
                )
            return _compatible_authenticate_session(authenticate, api_key, target)

        target_rejected = False
        try:
            context = await authenticate_for(workspace_id)
        except (AuthAuthenticationError, AuthAuthorizationError):
            if workspace_id is None:
                return None, None, False
            # Target authorization and credential validity are deliberately
            # separated.  A valid user outside the target sees the same opaque
            # 404 as a missing workspace, without teaching the frontend that
            # its otherwise-valid session expired.
            target_rejected = True
            try:
                context = await authenticate_for(None)
            except (AuthAuthenticationError, AuthAuthorizationError):
                return None, None, False
            except (AuthStoreError, sqlite3.Error) as exc:
                raise _AuthenticationBackendUnavailable from exc
            except Exception as exc:
                raise _AuthenticationBackendUnavailable from exc
        except (AuthStoreError, sqlite3.Error) as exc:
            raise _AuthenticationBackendUnavailable from exc
        except Exception as exc:
            # An injected identity provider is still a trusted backend. Unknown
            # provider failures must fail closed as unavailable, never masquerade
            # as a bad user credential.
            raise _AuthenticationBackendUnavailable from exc
        candidate = getattr(context, "principal", None)
        if not isinstance(candidate, Principal):
            raise _AuthenticationBackendUnavailable
        target_authorized = not target_rejected and (
            workspace_id is None or candidate.tenant_id == workspace_id
        )
        return candidate, context, target_authorized

    # 分发结果。
    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        path = request.url.path
        request.state.auth_context = None
        if path in _EXEMPT_PATHS:
            # Public probes intentionally have no tenant authority.  Keeping the
            # attribute present avoids downstream instrumentation guessing.
            request.state.principal = None
            await self.app(scope, receive, send)
            return

        if path == _SCIM_PREFIX or path.startswith(f"{_SCIM_PREFIX}/"):
            app_state = getattr(scope.get("app"), "state", None)
            registry = getattr(app_state, "scim_access_registry", {})
            client = request.client.host if request.client is not None else "unknown"
            key = _extract_api_key(request)
            access = None
            fingerprint = None
            if key is not None:
                fingerprint = fingerprint_api_key(key)
                if isinstance(registry, Mapping):
                    access = registry.get(fingerprint)
            if access is None:
                if not self._limiter.allow(f"scim-public\x1f{client}"):
                    response = JSONResponse(
                        status_code=429,
                        content={
                            "schemas": ["urn:ietf:params:scim:api:messages:2.0:Error"],
                            "status": "429",
                            "detail": "SCIM request rate exceeded",
                        },
                        media_type="application/scim+json",
                    )
                    await response(scope, receive, send)
                    return
                response = JSONResponse(
                    status_code=401,
                    content={
                        "schemas": ["urn:ietf:params:scim:api:messages:2.0:Error"],
                        "status": "401",
                        "detail": "invalid SCIM bearer token",
                    },
                    headers={"WWW-Authenticate": "Bearer"},
                    media_type="application/scim+json",
                )
                await response(scope, receive, send)
                return
            workspace_id = str(getattr(access, "workspace_id", ""))
            label = str(getattr(access, "label", "directory"))
            if not workspace_id or fingerprint is None:
                response = JSONResponse(
                    status_code=503,
                    content={
                        "schemas": ["urn:ietf:params:scim:api:messages:2.0:Error"],
                        "status": "503",
                        "detail": "SCIM configuration unavailable",
                    },
                    media_type="application/scim+json",
                )
                await response(scope, receive, send)
                return
            identity = f"scim\x1f{fingerprint}"
            if not self._limiter.allow(identity):
                response = JSONResponse(
                    status_code=429,
                    content={
                        "schemas": ["urn:ietf:params:scim:api:messages:2.0:Error"],
                        "status": "429",
                        "detail": "SCIM request rate exceeded",
                    },
                    media_type="application/scim+json",
                )
                await response(scope, receive, send)
                return
            scim_principal = Principal(
                tenant_id=workspace_id,
                subject_id=f"scim:{label}",
                role=Role.ADMIN,
                key_fingerprint=fingerprint,
            )
            request.state.scim_access = access
            request.state.principal = scim_principal
            await _send_and_audit(
                self.app,
                scope,
                receive,
                send,
                principal=scim_principal,
            )
            return

        if path in _PUBLIC_AUTH_PATHS:
            request.state.principal = None
            client = request.client.host if request.client is not None else "unknown"
            identity = f"public-auth\x1f{client}"
            if not self._limiter.allow(identity):
                response = _reject(
                    ErrorCode.REQUEST_THROTTLED, "请求过于频繁，请稍后重试"
                )
                await response(scope, receive, send)
                return
            await self.app(scope, receive, send)
            return

        # 鉴权：显式 principal 优先；旧 api_keys 映射到 default/admin；未
        # 配置任何凭据时保持本地模式兼容，注入 default/local owner。
        key = _extract_api_key(request)
        path_workspace_id = _target_workspace_id(path)
        try:
            header_workspace_id = _header_workspace_id(request)
        except ValueError:
            response = _reject(
                ErrorCode.BAD_REQUEST,
                "工作区请求头无效",
                status_code=400,
            )
            await response(scope, receive, send)
            return
        workspace_conflict = bool(
            path_workspace_id
            and header_workspace_id
            and path_workspace_id != header_workspace_id
        )
        # Authenticate conflicting requests without a target first. This keeps
        # invalid credentials a 401 and avoids mutating a valid shared session's
        # active workspace for a request that will be rejected anyway.
        requested_workspace_id = (
            None if workspace_conflict else header_workspace_id or path_workspace_id
        )
        try:
            principal, auth_context, target_authorized = await self._authenticate(
                key,
                getattr(scope.get("app"), "state", None),
                workspace_id=requested_workspace_id,
            )
        except _AuthenticationBackendUnavailable:
            request.state.principal = None
            response = _reject(
                ErrorCode.INTERNAL_ERROR,
                "身份服务暂不可用",
                status_code=503,
            )
            await response(scope, receive, send)
            return
        if principal is None:
            request.state.principal = None
            if key is None:
                response = _reject(ErrorCode.UNAUTHORIZED, "缺少 API key")
                await response(scope, receive, send)
                return
            response = _reject(ErrorCode.UNAUTHORIZED, "无效的 API key")
            await response(scope, receive, send)
            return

        request.state.principal = principal
        request.state.auth_context = auth_context
        if workspace_conflict or not target_authorized:
            response = _reject(
                ErrorCode.WORKSPACE_NOT_FOUND,
                "工作区不存在",
                status_code=404,
            )
            await _send_and_audit(
                response,
                scope,
                receive,
                send,
                principal=principal,
                record_attempt=False,
            )
            return
        if getattr(auth_context, "service_account", None) is not None and (
            path == "/v1/auth"
            or path.startswith("/v1/auth/")
            or path == "/v1/workspaces"
            or path.startswith("/v1/workspaces/")
            or path == "/v1/principals"
            or path.startswith("/v1/principals/")
        ):
            response = _reject(
                ErrorCode.FORBIDDEN,
                "服务账号不能管理真人身份或服务凭据",
                status_code=403,
            )
            await _send_and_audit(
                response,
                scope,
                receive,
                send,
                principal=principal,
                record_attempt=False,
            )
            return
        permission = required_permission(request.method, path)
        if not principal.allows(permission):
            response = _reject(
                ErrorCode.FORBIDDEN,
                "当前身份无权执行此操作",
                status_code=403,
            )
            await _send_and_audit(
                response,
                scope,
                receive,
                send,
                principal=principal,
                record_attempt=False,
            )
            return

        # 高频只读端点过鉴权但不过限流，避免 Streamlit rerun/轮询误杀正常使用。
        if not _is_rate_limit_exempt(request):
            if not self._limiter.allow(principal.rate_limit_identity):
                response = _reject(
                    ErrorCode.REQUEST_THROTTLED, "请求过于频繁，请稍后重试"
                )
                await _send_and_audit(
                    response,
                    scope,
                    receive,
                    send,
                    principal=principal,
                    record_attempt=False,
                )
                return

        await _send_and_audit(
            self.app,
            scope,
            receive,
            send,
            principal=principal,
        )


# 构建 rate limiter。
def build_rate_limiter(per_minute: int, burst: int) -> TokenBucketRateLimiter:
    # 每分钟速率换算成每秒补充；burst 即令牌桶容量。
    return TokenBucketRateLimiter(capacity=burst, refill_per_second=per_minute / 60.0)
