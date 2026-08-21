"""Enterprise OIDC login, account linking, and workspace admission routes."""

from __future__ import annotations

from collections.abc import Mapping
from functools import wraps
from typing import Any

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import RedirectResponse

from cogdoc.api.auth_store import (
    AuthAuthenticationError,
    AuthAuthorizationError,
    AuthConflictError,
    AuthNotFoundError,
    AuthStoreError,
    AuthValidationError,
)
from cogdoc.api.offload import run_sync
from cogdoc.api.oidc import (
    OIDCCallbackError,
    OIDCConfigurationError,
    OIDCFlowError,
    OIDCProtocolError,
    OIDCTransportError,
)
from cogdoc.api.routes.auth import (
    _RouteError,
    _authenticate,
    _complete_context,
    _session_response,
    _store_call,
    _authorize_workspace,
)
from cogdoc.api.schemas import (
    ErrorCode,
    ErrorResponse,
    OIDCExchangeResponse,
    OIDCHandoffRequest,
    OIDCIdentity,
    OIDCIdentityListResponse,
    OIDCStartRequest,
    OIDCStartResponse,
    SCIMDirectoryStatus,
    SCIMDirectoryStatusResponse,
    WorkspaceOIDCPolicy,
    WorkspaceOIDCPolicyResponse,
    WorkspaceOIDCPolicyUpdateRequest,
)
from cogdoc.api.tenancy import Permission


router = APIRouter(prefix="/v1", tags=["auth", "oidc"])

_ERROR_RESPONSES = {
    400: {"model": ErrorResponse},
    401: {"model": ErrorResponse},
    403: {"model": ErrorResponse},
    404: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
    429: {"model": ErrorResponse},
    503: {"model": ErrorResponse},
}


def _guarded(function):
    @wraps(function)
    async def wrapped(*args, **kwargs):
        try:
            return await function(*args, **kwargs)
        except _RouteError as exc:
            return exc.response()

    return wrapped


def _manager(request: Request) -> Any:
    manager = getattr(request.app.state, "oidc_manager", None)
    if manager is None:
        raise _RouteError(503, ErrorCode.OIDC_PROVIDER_UNAVAILABLE, "企业登录未配置")
    return manager


async def _manager_call(request: Request, operation: str, /, **kwargs: Any) -> Any:
    manager = _manager(request)
    function = getattr(manager, operation, None)
    if not callable(function):
        raise _RouteError(503, ErrorCode.OIDC_PROVIDER_UNAVAILABLE, "企业登录未配置")
    executor = getattr(request.app.state, "offload_executor", None)
    if executor is None or getattr(executor, "_shutdown", False):
        raise _RouteError(503, ErrorCode.INTERNAL_ERROR, "服务正在关闭，请重试")
    return await run_sync(executor, function, **kwargs)


def _translate(exc: Exception) -> _RouteError:
    if isinstance(exc, (OIDCFlowError, OIDCConfigurationError, AuthValidationError)):
        return _RouteError(400, ErrorCode.OIDC_FLOW_INVALID, "企业登录请求无效")
    if isinstance(exc, (AuthAuthenticationError, AuthAuthorizationError)):
        return _RouteError(403, ErrorCode.FORBIDDEN, "企业身份未获准访问")
    if isinstance(exc, AuthNotFoundError):
        return _RouteError(404, ErrorCode.SESSION_NOT_FOUND, "联邦身份不存在")
    if isinstance(exc, AuthConflictError):
        return _RouteError(409, ErrorCode.AUTH_CONFLICT, "企业身份状态发生冲突")
    if isinstance(exc, (OIDCProtocolError, OIDCTransportError)):
        return _RouteError(
            503, ErrorCode.OIDC_PROVIDER_UNAVAILABLE, "企业身份提供方暂不可用"
        )
    if isinstance(exc, AuthStoreError):
        return _RouteError(503, ErrorCode.INTERNAL_ERROR, "身份服务暂不可用")
    return _RouteError(503, ErrorCode.INTERNAL_ERROR, "企业登录暂不可用")


def _identity(value: Mapping[str, Any]) -> OIDCIdentity:
    try:
        return OIDCIdentity.model_validate(dict(value))
    except (TypeError, ValueError) as exc:
        raise _RouteError(
            503, ErrorCode.INTERNAL_ERROR, "身份服务返回了无效结果"
        ) from exc


@router.post(
    "/auth/oidc/authorize",
    response_model=OIDCStartResponse,
    responses=_ERROR_RESPONSES,
)
@_guarded
async def begin_oidc_login(payload: OIDCStartRequest, request: Request):
    try:
        result = await _manager_call(
            request,
            "begin_login",
            return_url=payload.return_url,
            workspace_id=payload.workspace_id,
        )
    except _RouteError:
        raise
    except Exception as exc:
        raise _translate(exc) from exc
    try:
        return OIDCStartResponse.model_validate(result)
    except (TypeError, ValueError) as exc:
        raise _RouteError(
            503, ErrorCode.INTERNAL_ERROR, "企业登录服务返回了无效结果"
        ) from exc


@router.get("/auth/oidc/callback", responses=_ERROR_RESPONSES)
async def oidc_callback(
    request: Request,
    state: str = Query(min_length=16, max_length=512),
    code: str | None = Query(default=None, min_length=1, max_length=8192),
    error: str | None = Query(default=None, min_length=1, max_length=256),
):
    try:
        if error is not None:
            redirect = await _manager_call(request, "callback_error", state=state)
        elif code is None:
            raise OIDCFlowError("OIDC callback is missing a code")
        else:
            redirect = await _manager_call(
                request, "complete_callback", state=state, code=code
            )
    except OIDCCallbackError as exc:
        return RedirectResponse(exc.redirect_url, status_code=303)
    except _RouteError as exc:
        return exc.response()
    except Exception as exc:
        return _translate(exc).response()
    return RedirectResponse(str(redirect), status_code=303)


@router.post(
    "/auth/oidc/exchange",
    response_model=OIDCExchangeResponse,
    responses=_ERROR_RESPONSES,
)
@_guarded
async def exchange_oidc_handoff(payload: OIDCHandoffRequest, request: Request):
    try:
        result = await _manager_call(request, "exchange_handoff", code=payload.code)
    except _RouteError:
        raise
    except Exception as exc:
        raise _translate(exc) from exc
    if not isinstance(result, Mapping):
        raise _RouteError(503, ErrorCode.INTERNAL_ERROR, "身份服务返回了无效结果")
    kind = result.get("kind")
    if kind == "login" and isinstance(result.get("session"), Mapping):
        try:
            context = await _complete_context(request, result["session"])
        except _RouteError:
            raise
        return OIDCExchangeResponse(kind="login", session=_session_response(context))
    if kind == "link" and isinstance(result.get("identity"), Mapping):
        return OIDCExchangeResponse(kind="link", identity=_identity(result["identity"]))
    raise _RouteError(503, ErrorCode.INTERNAL_ERROR, "身份服务返回了无效结果")


@router.post(
    "/auth/oidc/link/authorize",
    response_model=OIDCStartResponse,
    responses=_ERROR_RESPONSES,
)
@_guarded
async def begin_oidc_link(payload: OIDCStartRequest, request: Request):
    context = await _authenticate(request)
    try:
        result = await _manager_call(
            request,
            "begin_link",
            return_url=payload.return_url,
            workspace_id=context.workspace_id,
            user_id=context.user_id,
            session_id=context.session_id,
        )
    except _RouteError:
        raise
    except Exception as exc:
        raise _translate(exc) from exc
    return OIDCStartResponse.model_validate(result)


@router.get(
    "/auth/oidc/identities",
    response_model=OIDCIdentityListResponse,
    responses=_ERROR_RESPONSES,
)
@_guarded
async def list_oidc_identities(request: Request):
    context = await _authenticate(request)
    try:
        rows = await _store_call(
            request, "list_oidc_identities", {"user_id": context.user_id}
        )
    except _RouteError:
        raise
    except Exception as exc:
        raise _translate(exc) from exc
    if not isinstance(rows, list):
        raise _RouteError(503, ErrorCode.INTERNAL_ERROR, "身份服务返回了无效结果")
    return OIDCIdentityListResponse(
        identities=[_identity(row) for row in rows if isinstance(row, Mapping)]
    )


@router.delete(
    "/auth/oidc/identities/{identity_id}",
    status_code=204,
    responses=_ERROR_RESPONSES,
)
@_guarded
async def unlink_oidc_identity(identity_id: str, request: Request):
    context = await _authenticate(request)
    try:
        await _store_call(
            request,
            "unlink_oidc_identity",
            {"identity_id": identity_id, "user_id": context.user_id},
        )
    except _RouteError:
        raise
    except Exception as exc:
        raise _translate(exc) from exc
    return Response(status_code=204)


@router.get(
    "/workspaces/{workspace_id}/oidc-policy",
    response_model=WorkspaceOIDCPolicyResponse,
    responses=_ERROR_RESPONSES,
)
@_guarded
async def get_workspace_oidc_policy(workspace_id: str, request: Request):
    context = await _authorize_workspace(
        request, workspace_id, Permission.MANAGE_ACCESS
    )
    try:
        policy = await _store_call(
            request,
            "get_oidc_policy",
            {"workspace_id": workspace_id, "actor_user_id": context.user_id},
        )
    except _RouteError:
        raise
    except Exception as exc:
        raise _translate(exc) from exc
    return WorkspaceOIDCPolicyResponse(
        policy=None if policy is None else WorkspaceOIDCPolicy.model_validate(policy)
    )


@router.put(
    "/workspaces/{workspace_id}/oidc-policy",
    response_model=WorkspaceOIDCPolicyResponse,
    responses=_ERROR_RESPONSES,
)
@_guarded
async def set_workspace_oidc_policy(
    workspace_id: str,
    payload: WorkspaceOIDCPolicyUpdateRequest,
    request: Request,
):
    context = await _authorize_workspace(
        request, workspace_id, Permission.MANAGE_ACCESS
    )
    manager = _manager(request)
    issuer = getattr(getattr(manager, "client", None), "config", None)
    issuer_value = getattr(issuer, "issuer", None)
    if not isinstance(issuer_value, str):
        raise _RouteError(503, ErrorCode.OIDC_PROVIDER_UNAVAILABLE, "企业登录未配置")
    try:
        policy = await _store_call(
            request,
            "set_oidc_policy",
            {
                "workspace_id": workspace_id,
                "issuer": issuer_value,
                "allowed_domains": payload.allowed_domains,
                "default_role": payload.default_role,
                "enabled": payload.enabled,
                "group_claim": payload.group_claim,
                "group_role_map": payload.group_role_map,
                "require_mapped_group": payload.require_mapped_group,
                "actor_user_id": context.user_id,
                "expected_revision": payload.expected_revision,
            },
        )
    except _RouteError:
        raise
    except Exception as exc:
        raise _translate(exc) from exc
    return WorkspaceOIDCPolicyResponse(
        policy=WorkspaceOIDCPolicy.model_validate(policy)
    )


@router.get(
    "/workspaces/{workspace_id}/scim-status",
    response_model=SCIMDirectoryStatusResponse,
    responses=_ERROR_RESPONSES,
)
@_guarded
async def get_workspace_scim_status(workspace_id: str, request: Request):
    context = await _authorize_workspace(
        request, workspace_id, Permission.MANAGE_ACCESS
    )
    registry = getattr(request.app.state, "scim_access_registry", {})
    accesses = [
        value
        for value in (registry.values() if isinstance(registry, Mapping) else ())
        if str(getattr(value, "workspace_id", "")) == workspace_id
    ]
    try:
        summary = await _store_call(
            request,
            "get_scim_summary",
            {"workspace_id": workspace_id, "actor_user_id": context.user_id},
        )
    except _RouteError:
        raise
    except Exception as exc:
        raise _translate(exc) from exc
    first = accesses[0] if accesses else None
    values = {
        "enabled": bool(accesses),
        "token_labels": sorted(
            {str(getattr(value, "label", "directory")) for value in accesses}
        ),
        "default_role": str(getattr(first, "default_role", "viewer")),
        "group_role_map": dict(getattr(first, "group_role_map", {})),
        **dict(summary),
    }
    return SCIMDirectoryStatusResponse(
        status=SCIMDirectoryStatus.model_validate(values)
    )
