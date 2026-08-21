"""Workspace-scoped service-account and one-time token administration."""

from __future__ import annotations

from functools import wraps
from typing import Any, Mapping

from fastapi import APIRouter, Query, Request, Response

from cogdoc.api.auth_store import (
    AuthAuthorizationError,
    AuthConflictError,
    AuthNotFoundError,
    AuthStoreError,
    AuthValidationError,
)
from cogdoc.api.routes.auth import _RouteError, _authorize_workspace, _store_call
from cogdoc.api.schemas import (
    ErrorCode,
    ErrorResponse,
    ServiceAccount,
    ServiceAccountCreateRequest,
    ServiceAccountListResponse,
    ServiceAccountPolicy,
    ServiceAccountPolicyResponse,
    ServiceAccountPolicyUpdate,
    ServiceAccountResponse,
    ServiceAccountUpdateRequest,
    ServiceToken,
    ServiceTokenCreateRequest,
    ServiceTokenCreateResponse,
    ServiceTokenListResponse,
)
from cogdoc.api.tenancy import Permission


router = APIRouter(
    prefix="/v1/workspaces/{workspace_id}/service-accounts", tags=["auth"]
)
policy_router = APIRouter(
    prefix="/v1/workspaces/{workspace_id}/service-account-policy", tags=["auth"]
)
_ERROR_RESPONSES = {
    400: {"model": ErrorResponse},
    401: {"model": ErrorResponse},
    403: {"model": ErrorResponse},
    404: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
    503: {"model": ErrorResponse},
}


def _translate(exc: Exception) -> _RouteError:
    if isinstance(exc, AuthValidationError):
        return _RouteError(400, ErrorCode.BAD_REQUEST, "服务账号请求无效")
    if isinstance(exc, AuthAuthorizationError):
        return _RouteError(403, ErrorCode.FORBIDDEN, "无权管理服务账号")
    if isinstance(exc, AuthNotFoundError):
        return _RouteError(
            404, ErrorCode.SERVICE_ACCOUNT_NOT_FOUND, "服务账号或令牌不存在"
        )
    if isinstance(exc, AuthConflictError):
        return _RouteError(409, ErrorCode.AUTH_CONFLICT, "服务账号状态发生冲突")
    if isinstance(exc, AuthStoreError):
        return _RouteError(503, ErrorCode.INTERNAL_ERROR, "身份服务暂不可用")
    return _RouteError(503, ErrorCode.INTERNAL_ERROR, "服务账号操作失败")


def _guarded(function):
    @wraps(function)
    async def wrapped(*args, **kwargs):
        try:
            return await function(*args, **kwargs)
        except _RouteError as exc:
            return exc.response()
        except Exception as exc:
            return _translate(exc).response()

    return wrapped


def _account(value: Mapping[str, Any]) -> ServiceAccount:
    return ServiceAccount.model_validate(dict(value))


def _token(value: Mapping[str, Any]) -> ServiceToken:
    return ServiceToken.model_validate(dict(value))


async def _manager(request: Request, workspace_id: str):
    return await _authorize_workspace(request, workspace_id, Permission.MANAGE_ACCESS)


@policy_router.get("", response_model=ServiceAccountPolicyResponse)
@_guarded
async def get_service_account_policy(workspace_id: str, request: Request):
    context = await _manager(request, workspace_id)
    row = await _store_call(
        request,
        "get_service_account_policy",
        {"workspace_id": workspace_id, "actor_user_id": context.user_id},
    )
    return ServiceAccountPolicyResponse(policy=ServiceAccountPolicy.model_validate(row))


@policy_router.put("", response_model=ServiceAccountPolicyResponse)
@_guarded
async def update_service_account_policy(
    workspace_id: str,
    payload: ServiceAccountPolicyUpdate,
    request: Request,
):
    context = await _manager(request, workspace_id)
    row = await _store_call(
        request,
        "set_service_account_policy",
        {
            "workspace_id": workspace_id,
            "actor_user_id": context.user_id,
            **payload.model_dump(),
        },
    )
    return ServiceAccountPolicyResponse(policy=ServiceAccountPolicy.model_validate(row))


@router.get("", response_model=ServiceAccountListResponse, responses=_ERROR_RESPONSES)
@_guarded
async def list_service_accounts(workspace_id: str, request: Request):
    context = await _manager(request, workspace_id)
    rows = await _store_call(
        request,
        "list_service_accounts",
        {"workspace_id": workspace_id, "actor_user_id": context.user_id},
    )
    return ServiceAccountListResponse(
        workspace_id=workspace_id,
        service_accounts=[_account(row) for row in rows],
    )


@router.post(
    "",
    response_model=ServiceAccountResponse,
    status_code=201,
    responses=_ERROR_RESPONSES,
)
@_guarded
async def create_service_account(
    workspace_id: str, payload: ServiceAccountCreateRequest, request: Request
):
    context = await _manager(request, workspace_id)
    row = await _store_call(
        request,
        "create_service_account",
        {
            "workspace_id": workspace_id,
            "name": payload.name,
            "description": payload.description,
            "role": payload.role,
            "actor_user_id": context.user_id,
        },
    )
    return ServiceAccountResponse(service_account=_account(row))


@router.get(
    "/{service_account_id}",
    response_model=ServiceAccountResponse,
    responses=_ERROR_RESPONSES,
)
@_guarded
async def get_service_account(
    workspace_id: str, service_account_id: str, request: Request
):
    context = await _manager(request, workspace_id)
    row = await _store_call(
        request,
        "get_service_account",
        {
            "workspace_id": workspace_id,
            "service_account_id": service_account_id,
            "actor_user_id": context.user_id,
        },
    )
    return ServiceAccountResponse(service_account=_account(row))


@router.patch(
    "/{service_account_id}",
    response_model=ServiceAccountResponse,
    responses=_ERROR_RESPONSES,
)
@_guarded
async def update_service_account(
    workspace_id: str,
    service_account_id: str,
    payload: ServiceAccountUpdateRequest,
    request: Request,
):
    context = await _manager(request, workspace_id)
    row = await _store_call(
        request,
        "update_service_account",
        {
            "workspace_id": workspace_id,
            "service_account_id": service_account_id,
            "name": payload.name,
            "description": payload.description,
            "role": payload.role,
            "active": payload.active,
            "expected_revision": payload.expected_revision,
            "actor_user_id": context.user_id,
        },
    )
    return ServiceAccountResponse(service_account=_account(row))


@router.delete("/{service_account_id}", status_code=204, responses=_ERROR_RESPONSES)
@_guarded
async def delete_service_account(
    workspace_id: str,
    service_account_id: str,
    request: Request,
    expected_revision: int = Query(ge=1),
):
    context = await _manager(request, workspace_id)
    await _store_call(
        request,
        "delete_service_account",
        {
            "workspace_id": workspace_id,
            "service_account_id": service_account_id,
            "expected_revision": expected_revision,
            "actor_user_id": context.user_id,
        },
    )
    return Response(status_code=204)


@router.get(
    "/{service_account_id}/tokens",
    response_model=ServiceTokenListResponse,
    responses=_ERROR_RESPONSES,
)
@_guarded
async def list_service_tokens(
    workspace_id: str, service_account_id: str, request: Request
):
    context = await _manager(request, workspace_id)
    rows = await _store_call(
        request,
        "list_service_tokens",
        {
            "workspace_id": workspace_id,
            "service_account_id": service_account_id,
            "actor_user_id": context.user_id,
        },
    )
    return ServiceTokenListResponse(
        service_account_id=service_account_id,
        tokens=[_token(row) for row in rows],
    )


@router.post(
    "/{service_account_id}/tokens",
    response_model=ServiceTokenCreateResponse,
    status_code=201,
    responses=_ERROR_RESPONSES,
)
@_guarded
async def create_service_token(
    workspace_id: str,
    service_account_id: str,
    payload: ServiceTokenCreateRequest,
    request: Request,
    response: Response,
):
    context = await _manager(request, workspace_id)
    row = await _store_call(
        request,
        "create_service_token",
        {
            "workspace_id": workspace_id,
            "service_account_id": service_account_id,
            "label": payload.label,
            "ttl_seconds": (
                None
                if payload.expires_in_days is None
                else payload.expires_in_days * 86400
            ),
            "actor_user_id": context.user_id,
            "permissions": payload.permissions,
        },
    )
    raw_token = str(row.get("token") or "")
    if not raw_token:
        raise AuthStoreError("service token secret is unavailable")
    response.headers["Cache-Control"] = "no-store"
    return ServiceTokenCreateResponse(
        service_token=_token(
            {key: value for key, value in row.items() if key != "token"}
        ),
        token=raw_token,
    )


@router.delete(
    "/{service_account_id}/tokens/{token_id}",
    status_code=204,
    responses=_ERROR_RESPONSES,
)
@_guarded
async def revoke_service_token(
    workspace_id: str,
    service_account_id: str,
    token_id: str,
    request: Request,
    expected_revision: int = Query(ge=1),
):
    context = await _manager(request, workspace_id)
    await _store_call(
        request,
        "revoke_service_token",
        {
            "workspace_id": workspace_id,
            "service_account_id": service_account_id,
            "token_id": token_id,
            "expected_revision": expected_revision,
            "actor_user_id": context.user_id,
        },
    )
    return Response(status_code=204)
