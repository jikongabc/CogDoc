from __future__ import annotations

import hashlib
import inspect
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, is_dataclass
from functools import wraps
from typing import Any, Callable

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from cogdoc.api.access_control import TokenBucketRateLimiter
from cogdoc.api.auth_store import (
    AuthAuthenticationError,
    AuthAuthorizationError,
    AuthInviteError,
    AuthNotFoundError,
    AuthStoreError,
    AuthValidationError,
)
from cogdoc.api.offload import run_sync
from cogdoc.api.schemas import (
    AuthChangePasswordRequest,
    AuthLoginRequest,
    AuthMeResponse,
    AuthRegisterRequest,
    AuthSessionInfo,
    AuthSessionListResponse,
    AuthSessionResponse,
    AuthUser,
    AuthWorkspace,
    ErrorCode,
    ErrorResponse,
    WorkspaceCreateRequest,
    WorkspaceInvite,
    WorkspaceInviteAcceptRequest,
    WorkspaceInviteCreateRequest,
    WorkspaceInviteCreateResponse,
    WorkspaceInviteListResponse,
    WorkspaceListResponse,
    WorkspaceMember,
    WorkspaceMemberListResponse,
    WorkspaceMemberResponse,
    WorkspaceMemberUpdateRequest,
    WorkspaceResponse,
    WorkspaceUpdateRequest,
    build_error_response,
)
from cogdoc.api.tenancy import Permission, ROLE_PERMISSIONS, Role


router = APIRouter(prefix="/v1", tags=["auth", "workspaces"])

_ERROR_RESPONSES = {
    400: {"model": ErrorResponse},
    401: {"model": ErrorResponse},
    403: {"model": ErrorResponse},
    404: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
    429: {"model": ErrorResponse},
    503: {"model": ErrorResponse},
}

# Login and registration are intentionally outside the global authenticated
# limiter.  Keep their brute-force bucket bounded and app-scoped in its key; a
# deployment can inject ``app.state.auth_public_rate_limiter`` to tune it.
_DEFAULT_PUBLIC_LIMITER = TokenBucketRateLimiter(
    capacity=20,
    refill_per_second=20 / 60,
    max_identities=20_000,
)


class _RouteError(RuntimeError):
    def __init__(
        self,
        status_code: int,
        code: ErrorCode,
        message: str,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.headers = dict(headers or {})

    def response(self) -> JSONResponse:
        return JSONResponse(
            status_code=self.status_code,
            content=build_error_response(self.code, self.message).model_dump(),
            headers=self.headers,
        )


def _guarded(function: Callable) -> Callable:
    @wraps(function)
    async def wrapped(*args, **kwargs):
        try:
            return await function(*args, **kwargs)
        except _RouteError as exc:
            return exc.response()

    return wrapped


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if hasattr(value, "model_dump"):
        result = value.model_dump()
        return dict(result) if isinstance(result, Mapping) else {}
    if is_dataclass(value) and not isinstance(value, type):
        result = asdict(value)
        return dict(result) if isinstance(result, Mapping) else {}
    if hasattr(value, "__dict__"):
        return {
            str(key): item
            for key, item in vars(value).items()
            if not str(key).startswith("_")
        }
    return {}


def _nested_mapping(payload: Mapping[str, Any], *names: str) -> dict[str, Any]:
    for name in names:
        nested = _mapping(payload.get(name))
        if nested:
            return nested
    return {}


def _text(payload: Mapping[str, Any], *names: str, default: str = "") -> str:
    for name in names:
        value = payload.get(name)
        if isinstance(value, str) and value:
            return value
    return default


def _integer(payload: Mapping[str, Any], *names: str, default: int = 0) -> int:
    for name in names:
        value = payload.get(name)
        if type(value) is int:
            return max(0, value)
    return default


def _store(request: Request) -> Any:
    store = getattr(request.app.state, "auth_store", None)
    if store is None:
        raise _RouteError(503, ErrorCode.INTERNAL_ERROR, "身份服务暂不可用")
    return store


def _compatible_call(function: Callable, variants: Sequence[dict[str, Any]]) -> Any:
    """Call one documented AuthStore operation across harmless name variants.

    The storage implementation is injected independently.  Signature binding is
    performed before invocation, so a ``TypeError`` raised *inside* the store is
    never mistaken for an alternate signature and replayed as a second mutation.
    """

    try:
        signature = inspect.signature(function)
    except (TypeError, ValueError):
        return function(**variants[0])
    for arguments in variants:
        try:
            signature.bind(**arguments)
        except TypeError:
            continue
        return function(**arguments)
    raise TypeError(f"unsupported AuthStore signature for {function.__name__}")


def _compatible_authenticate_session_call(
    function: Callable,
    token: str,
    workspace_id: str | None,
) -> Any:
    """Call both target-aware and trusted legacy session providers safely."""

    variants: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
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
        signature = inspect.signature(function)
    except (TypeError, ValueError):
        arguments = (token, workspace_id) if workspace_id is not None else (token,)
        return function(*arguments)
    for args, kwargs in variants:
        try:
            signature.bind(*args, **kwargs)
        except TypeError:
            continue
        return function(*args, **kwargs)
    raise TypeError("unsupported authenticate_session provider signature")


async def _store_call(
    request: Request,
    operation: str | Sequence[str],
    *variants: dict[str, Any],
) -> Any:
    store = _store(request)
    names = (operation,) if isinstance(operation, str) else tuple(operation)
    function = next(
        (
            candidate
            for name in names
            if callable(candidate := getattr(store, name, None))
        ),
        None,
    )
    if function is None:
        raise _RouteError(503, ErrorCode.INTERNAL_ERROR, "身份服务暂不可用")
    call_variants = variants or ({},)
    executor = getattr(request.app.state, "offload_executor", None)
    if executor is not None and not getattr(executor, "_shutdown", False):
        return await run_sync(executor, _compatible_call, function, call_variants)
    return _compatible_call(function, call_variants)


async def _authenticate_session_call(
    request: Request,
    token: str,
    workspace_id: str | None,
) -> Any:
    store = _store(request)
    function = getattr(store, "authenticate_session", None)
    if not callable(function):
        raise _RouteError(503, ErrorCode.INTERNAL_ERROR, "身份服务暂不可用")
    executor = getattr(request.app.state, "offload_executor", None)
    if executor is not None and not getattr(executor, "_shutdown", False):
        return await run_sync(
            executor,
            _compatible_authenticate_session_call,
            function,
            token,
            workspace_id,
        )
    return _compatible_authenticate_session_call(function, token, workspace_id)


async def _revoke_all_member_grants(
    request: Request,
    *,
    workspace_id: str,
    user_id: str,
    membership_id: str,
) -> None:
    """Fail closed while removing every persisted ACL grant for a member."""

    store = getattr(request.app.state, "resource_access_store", None)
    if store is None:
        raise _RouteError(503, ErrorCode.INTERNAL_ERROR, "资源权限服务暂不可用")
    function = next(
        (
            candidate
            for name in ("revoke_all_subject_grants", "revoke_subject_all")
            if callable(candidate := getattr(store, name, None))
        ),
        None,
    )
    if function is None:
        raise _RouteError(503, ErrorCode.INTERNAL_ERROR, "资源权限服务暂不可用")
    variants = (
        {
            "tenant_id": workspace_id,
            "subject_id": user_id,
            "membership_id": membership_id,
        },
        {
            "workspace_id": workspace_id,
            "user_id": user_id,
            "membership_id": membership_id,
        },
    )
    executor = getattr(request.app.state, "offload_executor", None)
    try:
        if executor is not None and not getattr(executor, "_shutdown", False):
            await run_sync(executor, _compatible_call, function, variants)
        else:
            _compatible_call(function, variants)
    except _RouteError:
        raise
    except Exception as exc:
        raise _RouteError(
            503,
            ErrorCode.INTERNAL_ERROR,
            "资源权限服务暂不可用，成员未被移除",
        ) from exc


def _exception_identity(exc: Exception) -> str:
    code = getattr(exc, "code", "")
    return f"{type(exc).__name__} {code}".casefold()


def _is_not_found(exc: Exception) -> bool:
    identity = _exception_identity(exc)
    return isinstance(exc, (KeyError, LookupError)) or any(
        token in identity for token in ("notfound", "not_found", "unknown")
    )


def _is_conflict(exc: Exception) -> bool:
    identity = _exception_identity(exc)
    return isinstance(exc, FileExistsError) or any(
        token in identity
        for token in ("conflict", "exists", "duplicate", "lastowner", "last_owner")
    )


def _is_authorization_failure(exc: Exception) -> bool:
    identity = _exception_identity(exc)
    return isinstance(exc, PermissionError) or any(
        token in identity for token in ("authorization", "forbidden", "permission")
    )


def _is_validation_failure(exc: Exception) -> bool:
    identity = _exception_identity(exc)
    return isinstance(exc, ValueError) or any(
        token in identity for token in ("validation", "invalid")
    )


def _is_expected_auth_failure(exc: Exception) -> bool:
    identity = _exception_identity(exc)
    return isinstance(
        exc,
        (
            AuthAuthenticationError,
            AuthAuthorizationError,
            AuthInviteError,
            AuthNotFoundError,
            AuthValidationError,
            ValueError,
            KeyError,
            LookupError,
            PermissionError,
        ),
    ) or any(
        token in identity
        for token in (
            "authentication",
            "authorization",
            "credential",
            "password",
            "session",
            "token",
            "expired",
            "revoked",
        )
    )


def _is_identity_backend_failure(exc: Exception) -> bool:
    return isinstance(exc, (AuthStoreError, sqlite3.Error))


def _raise_login_failure(exc: Exception) -> None:
    if _is_expected_auth_failure(exc):
        # Deliberately identical for an unknown email, wrong password, disabled
        # account, or invalid workspace selection.
        raise _RouteError(401, ErrorCode.UNAUTHORIZED, "邮箱或密码错误") from exc
    raise _RouteError(503, ErrorCode.INTERNAL_ERROR, "身份服务暂不可用") from exc


def _raise_session_failure(exc: Exception) -> None:
    if _is_expected_auth_failure(exc):
        raise _RouteError(401, ErrorCode.UNAUTHORIZED, "登录状态无效或已过期") from exc
    if _is_identity_backend_failure(exc):
        raise _RouteError(503, ErrorCode.INTERNAL_ERROR, "身份服务暂不可用") from exc
    raise _RouteError(503, ErrorCode.INTERNAL_ERROR, "身份服务暂不可用") from exc


def _raise_workspace_missing(exc: Exception) -> None:
    if _is_expected_auth_failure(exc) or _is_not_found(exc):
        # A nonexistent workspace and a workspace belonging to somebody else are
        # intentionally indistinguishable.
        raise _RouteError(404, ErrorCode.WORKSPACE_NOT_FOUND, "工作区不存在") from exc
    raise _RouteError(503, ErrorCode.INTERNAL_ERROR, "身份服务暂不可用") from exc


def _raise_mutation_failure(
    exc: Exception,
    *,
    missing_code: ErrorCode,
    missing_message: str,
) -> None:
    if _is_not_found(exc):
        raise _RouteError(404, missing_code, missing_message) from exc
    if _is_authorization_failure(exc):
        raise _RouteError(403, ErrorCode.FORBIDDEN, "当前身份无权执行此操作") from exc
    if _is_conflict(exc):
        raise _RouteError(409, ErrorCode.AUTH_CONFLICT, "操作与当前状态冲突") from exc
    if _is_validation_failure(exc):
        raise _RouteError(400, ErrorCode.BAD_REQUEST, "请求无效") from exc
    raise _RouteError(503, ErrorCode.INTERNAL_ERROR, "身份服务暂不可用") from exc


def _extract_session_token(request: Request) -> str:
    authorization = request.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
        if token:
            return token
    fallback = request.headers.get("x-api-key", "").strip()
    if fallback:
        return fallback
    raise _RouteError(401, ErrorCode.UNAUTHORIZED, "缺少登录凭据")


def _enforce_public_rate_limit(request: Request, operation: str) -> None:
    limiter = getattr(
        request.app.state, "auth_public_rate_limiter", _DEFAULT_PUBLIC_LIMITER
    )
    client_host = request.client.host if request.client is not None else "unknown"
    identity = hashlib.sha256(
        f"auth-public-v1\0{id(request.app)}\0{operation}\0{client_host}".encode()
    ).hexdigest()
    if not limiter.allow(identity):
        raise _RouteError(
            429,
            ErrorCode.REQUEST_THROTTLED,
            "请求过于频繁，请稍后重试",
            headers={"Retry-After": "60"},
        )


@dataclass(frozen=True, slots=True)
class _AuthContext:
    token: str
    user: dict[str, Any]
    workspace: dict[str, Any]
    session: dict[str, Any]
    role: Role

    @property
    def user_id(self) -> str:
        return _text(self.user, "user_id", "id", "subject_id")

    @property
    def workspace_id(self) -> str:
        return _text(self.workspace, "workspace_id", "id", "tenant_id")

    @property
    def session_id(self) -> str:
        return _text(self.session, "session_id", "id")


def _split_context_result(
    value: Any, *, token_hint: str = ""
) -> tuple[str, dict[str, Any]]:
    token = token_hint
    payload: dict[str, Any] = {}
    if isinstance(value, tuple):
        if len(value) == 3:
            user, workspace, returned_token = value
            payload = {"user": user, "workspace": workspace}
            if isinstance(returned_token, str):
                token = returned_token
        elif len(value) == 2:
            first, second = value
            if isinstance(first, str):
                token, payload = first, _mapping(second)
            elif isinstance(second, str):
                payload, token = _mapping(first), second
            else:
                payload = {"user": first, "workspace": second}
        else:
            payload = {}
    elif isinstance(value, str):
        token = value
    else:
        payload = _mapping(value)

    nested_context = _nested_mapping(payload, "context", "auth_context")
    if nested_context:
        payload = {**nested_context, **payload}
    token = _text(payload, "access_token", "session_token", "token", default=token)
    return token, payload


def _context_from_payload(
    token: str, payload: Mapping[str, Any]
) -> _AuthContext | None:
    user = _nested_mapping(payload, "user", "account")
    workspace = _nested_mapping(payload, "workspace", "tenant")
    session = _nested_mapping(payload, "session")
    membership = _nested_mapping(payload, "membership", "member")
    principal = _nested_mapping(payload, "principal")
    if not user:
        user = {
            key: payload[key]
            for key in ("user_id", "subject_id", "email", "display_name")
            if key in payload
        }
    if not workspace:
        workspace = {
            key: payload[key]
            for key in ("workspace_id", "tenant_id", "workspace_name")
            if key in payload
        }
    role_value = _text(
        membership,
        "role",
        default=_text(
            principal,
            "role",
            default=_text(payload, "role", default=_text(workspace, "role")),
        ),
    )
    try:
        role = Role(role_value)
    except (TypeError, ValueError):
        return None
    if not token or not _text(user, "user_id", "id", "subject_id"):
        return None
    if not _text(workspace, "workspace_id", "id", "tenant_id"):
        return None
    if membership:
        workspace = {**workspace, "role": role.value}
    return _AuthContext(token, user, workspace, session, role)


async def _complete_context(
    request: Request,
    value: Any,
    *,
    token_hint: str = "",
    workspace_id: str | None = None,
) -> _AuthContext:
    token, payload = _split_context_result(value, token_hint=token_hint)
    context = _context_from_payload(token, payload)
    if context is not None and (
        workspace_id is None or context.workspace_id == workspace_id
    ):
        return context
    if not token:
        raise _RouteError(503, ErrorCode.INTERNAL_ERROR, "身份服务返回了无效结果")
    try:
        resolved = await _authenticate_session_call(request, token, workspace_id)
    except _RouteError:
        raise
    except Exception as exc:
        _raise_session_failure(exc)
    resolved_token, resolved_payload = _split_context_result(resolved, token_hint=token)
    context = _context_from_payload(resolved_token, resolved_payload)
    if context is None:
        raise _RouteError(503, ErrorCode.INTERNAL_ERROR, "身份服务返回了无效结果")
    if workspace_id is not None and context.workspace_id != workspace_id:
        raise _RouteError(404, ErrorCode.WORKSPACE_NOT_FOUND, "工作区不存在")
    return context


async def _authenticate(request: Request) -> _AuthContext:
    token = _extract_session_token(request)
    cached = getattr(request.state, "auth_context", None)
    if cached is not None:
        cached_token, cached_payload = _split_context_result(cached, token_hint=token)
        context = _context_from_payload(cached_token, cached_payload)
        if context is None:
            raise _RouteError(503, ErrorCode.INTERNAL_ERROR, "身份服务返回了无效结果")
        principal = getattr(request.state, "principal", None)
        if principal is not None and (
            getattr(principal, "tenant_id", None) != context.workspace_id
            or getattr(principal, "subject_id", None) != context.user_id
            or getattr(principal, "role", None) != context.role
        ):
            raise _RouteError(503, ErrorCode.INTERNAL_ERROR, "身份服务返回了不一致结果")
        return context
    try:
        value = await _authenticate_session_call(request, token, None)
    except _RouteError:
        raise
    except Exception as exc:
        _raise_session_failure(exc)
    if value is None:
        raise _RouteError(401, ErrorCode.UNAUTHORIZED, "登录状态无效或已过期")
    return await _complete_context(request, value, token_hint=token)


async def _authorize_workspace(
    request: Request,
    workspace_id: str,
    permission: Permission | None = None,
) -> _AuthContext:
    current = await _authenticate(request)
    try:
        value = await _authenticate_session_call(
            request, current.token, workspace_id
        )
        if value is None:
            raise LookupError(workspace_id)
        target = await _complete_context(
            request, value, token_hint=current.token, workspace_id=workspace_id
        )
    except _RouteError as exc:
        if exc.status_code == 503:
            raise
        raise _RouteError(404, ErrorCode.WORKSPACE_NOT_FOUND, "工作区不存在") from exc
    except Exception as exc:
        _raise_workspace_missing(exc)
    if permission is not None and permission not in ROLE_PERMISSIONS[target.role]:
        raise _RouteError(403, ErrorCode.FORBIDDEN, "当前身份无权执行此操作")
    return target


def _user_model(value: Mapping[str, Any]) -> AuthUser:
    user_id = _text(value, "user_id", "id", "subject_id")
    email = _text(value, "email")
    if not user_id or not email:
        raise _RouteError(503, ErrorCode.INTERNAL_ERROR, "身份服务返回了无效结果")
    return AuthUser(
        user_id=user_id,
        email=email,
        display_name=_text(value, "display_name", "name", default=email),
        created_at=_text(value, "created_at"),
        updated_at=_text(value, "updated_at"),
    )


def _workspace_model(value: Any, *, role: Role | str) -> AuthWorkspace:
    payload = _mapping(value)
    nested = _nested_mapping(payload, "workspace", "tenant")
    if nested:
        payload = nested
    workspace_id = _text(payload, "workspace_id", "id", "tenant_id")
    try:
        normalized_role = role if isinstance(role, Role) else Role(role)
    except ValueError as exc:
        raise _RouteError(
            503, ErrorCode.INTERNAL_ERROR, "身份服务返回了无效结果"
        ) from exc
    if not workspace_id:
        raise _RouteError(503, ErrorCode.INTERNAL_ERROR, "身份服务返回了无效结果")
    return AuthWorkspace(
        workspace_id=workspace_id,
        name=_text(payload, "name", "workspace_name", default=workspace_id),
        role=normalized_role.value,
        created_at=_text(payload, "created_at"),
        updated_at=_text(payload, "updated_at"),
        revision=_integer(payload, "revision", "version"),
    )


def _session_response(context: _AuthContext) -> AuthSessionResponse:
    return AuthSessionResponse(
        access_token=context.token,
        expires_at=_text(context.session, "expires_at"),
        user=_user_model(context.user),
        workspace=_workspace_model(context.workspace, role=context.role),
        permissions=sorted(
            permission.value for permission in ROLE_PERMISSIONS[context.role]
        ),
    )


def _rows(value: Any, *keys: str) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    payload = _mapping(value)
    for key in keys:
        rows = payload.get(key)
        if isinstance(rows, (list, tuple)):
            return list(rows)
    return []


def _workspace_rows(value: Any) -> list[AuthWorkspace]:
    result = []
    for raw in _rows(value, "workspaces", "items"):
        payload = _mapping(raw)
        membership = _nested_mapping(payload, "membership", "member")
        role = _text(membership, "role", default=_text(payload, "role"))
        result.append(_workspace_model(payload, role=role))
    return result


def _member_model(value: Any) -> WorkspaceMember:
    payload = _mapping(value)
    user = _nested_mapping(payload, "user", "account")
    user_id = _text(payload, "user_id", default=_text(user, "user_id", "id"))
    member_id = _text(payload, "member_id", "membership_id", "id", default=user_id)
    email = _text(payload, "email", default=_text(user, "email"))
    role = _text(payload, "role")
    if not member_id or not user_id or not email or not role:
        raise _RouteError(503, ErrorCode.INTERNAL_ERROR, "身份服务返回了无效结果")
    return WorkspaceMember(
        member_id=member_id,
        user_id=user_id,
        email=email,
        display_name=_text(
            payload,
            "display_name",
            default=_text(user, "display_name", "name", default=email),
        ),
        role=role,
        joined_at=_text(payload, "joined_at", "created_at"),
        updated_at=_text(payload, "updated_at"),
    )


def _invite_model(value: Any) -> WorkspaceInvite:
    payload = _mapping(value)
    invite_id = _text(payload, "invite_id", "id")
    workspace_id = _text(payload, "workspace_id", "tenant_id")
    email = _text(payload, "email")
    role = _text(payload, "role")
    if not invite_id or not workspace_id or not email or not role:
        raise _RouteError(503, ErrorCode.INTERNAL_ERROR, "身份服务返回了无效结果")
    return WorkspaceInvite(
        invite_id=invite_id,
        workspace_id=workspace_id,
        email=email,
        role=role,
        status=_text(payload, "status", default="pending"),
        created_by=_text(payload, "created_by", "inviter_id"),
        created_at=_text(payload, "created_at"),
        expires_at=_text(payload, "expires_at"),
    )


@router.post(
    "/auth/register",
    response_model=AuthSessionResponse,
    status_code=201,
    responses=_ERROR_RESPONSES,
)
@_guarded
async def register(payload: AuthRegisterRequest, request: Request):
    _enforce_public_rate_limit(request, "register")
    if not getattr(request.app.state, "self_registration_enabled", True):
        raise _RouteError(403, ErrorCode.FORBIDDEN, "当前部署未开放自主注册")
    try:
        result = await _store_call(
            request,
            "register",
            {
                "email": payload.email,
                "password": payload.password,
                "display_name": payload.display_name,
                "workspace_name": payload.workspace_name,
            },
        )
    except _RouteError:
        raise
    except Exception as exc:
        if _is_conflict(exc):
            # The stable conflict response intentionally supports clients that
            # need to direct an existing account to login. Deployments requiring
            # non-enumeration should disable self-registration and use invites.
            raise _RouteError(409, ErrorCode.AUTH_CONFLICT, "无法创建账号") from exc
        if _is_validation_failure(exc):
            raise _RouteError(400, ErrorCode.BAD_REQUEST, "无法创建账号") from exc
        raise _RouteError(503, ErrorCode.INTERNAL_ERROR, "身份服务暂不可用") from exc
    context = await _complete_context(request, result)
    return _session_response(context)


@router.post(
    "/auth/login", response_model=AuthSessionResponse, responses=_ERROR_RESPONSES
)
@_guarded
async def login(payload: AuthLoginRequest, request: Request):
    _enforce_public_rate_limit(request, "login")
    try:
        result = await _store_call(
            request,
            ("authenticate_password", "login"),
            {
                "email": payload.email,
                "password": payload.password,
                "workspace_id": payload.workspace_id,
            },
            {"email": payload.email, "password": payload.password},
        )
    except _RouteError:
        raise
    except Exception as exc:
        _raise_login_failure(exc)
    if result is None:
        raise _RouteError(401, ErrorCode.UNAUTHORIZED, "邮箱或密码错误")
    try:
        context = await _complete_context(
            request, result, workspace_id=payload.workspace_id
        )
    except _RouteError as exc:
        if exc.status_code != 503:
            raise _RouteError(401, ErrorCode.UNAUTHORIZED, "邮箱或密码错误") from exc
        raise
    return _session_response(context)


@router.post("/auth/logout", status_code=204, responses=_ERROR_RESPONSES)
@_guarded
async def logout(request: Request):
    context = await _authenticate(request)
    try:
        await _store_call(
            request,
            "logout",
            {"token": context.token},
            {"session_token": context.token},
        )
    except _RouteError:
        raise
    except Exception as exc:
        _raise_session_failure(exc)
    return Response(status_code=204)


@router.post("/auth/logout-all", status_code=204, responses=_ERROR_RESPONSES)
@_guarded
async def logout_all(request: Request):
    context = await _authenticate(request)
    try:
        await _store_call(
            request,
            ("logout_all", "logout"),
            {"user_id": context.user_id},
            {"token": context.token, "all_sessions": True},
            {"session_token": context.token, "all_sessions": True},
        )
    except _RouteError:
        raise
    except Exception as exc:
        _raise_session_failure(exc)
    return Response(status_code=204)


@router.post("/auth/change-password", status_code=204, responses=_ERROR_RESPONSES)
@_guarded
async def change_password(payload: AuthChangePasswordRequest, request: Request):
    _enforce_public_rate_limit(request, "change-password")
    context = await _authenticate(request)
    try:
        await _store_call(
            request,
            "change_password",
            {
                "token": context.token,
                "current_password": payload.current_password,
                "new_password": payload.new_password,
            },
            {
                "session_token": context.token,
                "current_password": payload.current_password,
                "new_password": payload.new_password,
            },
            {
                "user_id": context.user_id,
                "current_password": payload.current_password,
                "new_password": payload.new_password,
                "current_token": context.token,
            },
            {
                "user_id": context.user_id,
                "current_password": payload.current_password,
                "new_password": payload.new_password,
            },
        )
    except _RouteError:
        raise
    except Exception as exc:
        if _is_expected_auth_failure(exc):
            raise _RouteError(400, ErrorCode.BAD_REQUEST, "无法修改密码") from exc
        raise _RouteError(503, ErrorCode.INTERNAL_ERROR, "身份服务暂不可用") from exc
    return Response(status_code=204)


@router.get("/auth/me", response_model=AuthMeResponse, responses=_ERROR_RESPONSES)
@_guarded
async def me(request: Request):
    context = await _authenticate(request)
    try:
        raw_workspaces = await _store_call(
            request,
            "list_workspaces",
            {"user_id": context.user_id},
            {"token": context.token},
        )
    except _RouteError:
        raise
    except Exception as exc:
        _raise_session_failure(exc)
    return AuthMeResponse(
        user=_user_model(context.user),
        workspace=_workspace_model(context.workspace, role=context.role),
        permissions=sorted(
            permission.value for permission in ROLE_PERMISSIONS[context.role]
        ),
        workspaces=_workspace_rows(raw_workspaces),
    )


@router.get(
    "/auth/sessions", response_model=AuthSessionListResponse, responses=_ERROR_RESPONSES
)
@_guarded
async def list_sessions(request: Request):
    context = await _authenticate(request)
    try:
        result = await _store_call(
            request,
            "list_sessions",
            {"user_id": context.user_id, "current_token": context.token},
            {"user_id": context.user_id},
            {"token": context.token},
        )
    except _RouteError:
        raise
    except Exception as exc:
        _raise_session_failure(exc)
    sessions = []
    for raw in _rows(result, "sessions", "items"):
        item = _mapping(raw)
        session_id = _text(item, "session_id", "id")
        if not session_id:
            raise _RouteError(503, ErrorCode.INTERNAL_ERROR, "身份服务返回了无效结果")
        sessions.append(
            AuthSessionInfo(
                session_id=session_id,
                created_at=_text(item, "created_at"),
                last_seen_at=_text(item, "last_seen_at"),
                expires_at=_text(item, "expires_at"),
                current=bool(item.get("current", session_id == context.session_id)),
            )
        )
    return AuthSessionListResponse(sessions=sessions)


@router.delete(
    "/auth/sessions/{session_id}", status_code=204, responses=_ERROR_RESPONSES
)
@_guarded
async def delete_session(session_id: str, request: Request):
    context = await _authenticate(request)
    try:
        deleted = await _store_call(
            request,
            ("delete_session", "revoke_session"),
            {"user_id": context.user_id, "session_id": session_id},
            {"token": context.token, "session_id": session_id},
        )
    except _RouteError:
        raise
    except Exception as exc:
        _raise_mutation_failure(
            exc,
            missing_code=ErrorCode.SESSION_NOT_FOUND,
            missing_message="会话不存在",
        )
    if deleted is False:
        raise _RouteError(404, ErrorCode.SESSION_NOT_FOUND, "会话不存在")
    return Response(status_code=204)


@router.get(
    "/workspaces", response_model=WorkspaceListResponse, responses=_ERROR_RESPONSES
)
@_guarded
async def list_workspaces(request: Request):
    context = await _authenticate(request)
    try:
        result = await _store_call(
            request,
            "list_workspaces",
            {"user_id": context.user_id},
            {"token": context.token},
        )
    except _RouteError:
        raise
    except Exception as exc:
        _raise_session_failure(exc)
    return WorkspaceListResponse(workspaces=_workspace_rows(result))


@router.post(
    "/workspaces",
    response_model=WorkspaceResponse,
    status_code=201,
    responses=_ERROR_RESPONSES,
)
@_guarded
async def create_workspace(payload: WorkspaceCreateRequest, request: Request):
    context = await _authenticate(request)
    try:
        result = await _store_call(
            request,
            "create_workspace",
            {"owner_user_id": context.user_id, "name": payload.name},
            {"user_id": context.user_id, "name": payload.name},
            {"owner_id": context.user_id, "name": payload.name},
        )
    except _RouteError:
        raise
    except Exception as exc:
        _raise_mutation_failure(
            exc,
            missing_code=ErrorCode.WORKSPACE_NOT_FOUND,
            missing_message="工作区不存在",
        )
    return WorkspaceResponse(workspace=_workspace_model(result, role=Role.OWNER))


@router.get(
    "/workspaces/{workspace_id}",
    response_model=WorkspaceResponse,
    responses=_ERROR_RESPONSES,
)
@_guarded
async def get_workspace(workspace_id: str, request: Request):
    context = await _authorize_workspace(request, workspace_id)
    try:
        result = await _store_call(
            request,
            "get_workspace",
            {"workspace_id": workspace_id, "user_id": context.user_id},
            {"workspace_id": workspace_id},
        )
    except _RouteError:
        raise
    except Exception as exc:
        _raise_workspace_missing(exc)
    if result is None:
        raise _RouteError(404, ErrorCode.WORKSPACE_NOT_FOUND, "工作区不存在")
    return WorkspaceResponse(workspace=_workspace_model(result, role=context.role))


@router.patch(
    "/workspaces/{workspace_id}",
    response_model=WorkspaceResponse,
    responses=_ERROR_RESPONSES,
)
@_guarded
async def update_workspace(
    workspace_id: str, payload: WorkspaceUpdateRequest, request: Request
):
    context = await _authorize_workspace(
        request, workspace_id, Permission.MANAGE_TENANT
    )
    try:
        result = await _store_call(
            request,
            ("update_workspace", "rename_workspace"),
            {
                "workspace_id": workspace_id,
                "name": payload.name,
                "actor_user_id": context.user_id,
                "expected_revision": payload.expected_revision,
            },
            {
                "workspace_id": workspace_id,
                "name": payload.name,
                "expected_revision": payload.expected_revision,
                "actor_id": context.user_id,
            },
            {
                "workspace_id": workspace_id,
                "name": payload.name,
                "expected_revision": payload.expected_revision,
            },
            {"workspace_id": workspace_id, "name": payload.name},
        )
    except _RouteError:
        raise
    except Exception as exc:
        _raise_mutation_failure(
            exc,
            missing_code=ErrorCode.WORKSPACE_NOT_FOUND,
            missing_message="工作区不存在",
        )
    if result is None or result is False:
        raise _RouteError(404, ErrorCode.WORKSPACE_NOT_FOUND, "工作区不存在")
    return WorkspaceResponse(workspace=_workspace_model(result, role=context.role))


@router.delete(
    "/workspaces/{workspace_id}", status_code=204, responses=_ERROR_RESPONSES
)
@_guarded
async def delete_workspace(workspace_id: str, request: Request):
    context = await _authorize_workspace(
        request, workspace_id, Permission.MANAGE_TENANT
    )
    registry = getattr(request.app.state, "kb_registry", None)
    if registry is not None and callable(getattr(registry, "list", None)):
        try:
            try:
                knowledge_bases = registry.list(tenant_id=workspace_id)
            except TypeError:
                knowledge_bases = [
                    row
                    for row in registry.list()
                    if str(row.get("tenant_id") or "default") == workspace_id
                ]
        except Exception as exc:
            raise _RouteError(
                503, ErrorCode.INTERNAL_ERROR, "资源目录暂不可用"
            ) from exc
        if knowledge_bases:
            raise _RouteError(
                409,
                ErrorCode.AUTH_CONFLICT,
                "请先删除工作区内的所有知识库",
            )
    try:
        deleted = await _store_call(
            request,
            "delete_workspace",
            {"workspace_id": workspace_id, "actor_user_id": context.user_id},
            {"workspace_id": workspace_id, "actor_id": context.user_id},
            {"workspace_id": workspace_id},
        )
    except _RouteError:
        raise
    except Exception as exc:
        _raise_mutation_failure(
            exc,
            missing_code=ErrorCode.WORKSPACE_NOT_FOUND,
            missing_message="工作区不存在",
        )
    if deleted is False:
        raise _RouteError(404, ErrorCode.WORKSPACE_NOT_FOUND, "工作区不存在")
    return Response(status_code=204)


@router.post(
    "/workspaces/{workspace_id}/switch",
    response_model=AuthSessionResponse,
    responses=_ERROR_RESPONSES,
)
@_guarded
async def switch_workspace(workspace_id: str, request: Request):
    context = await _authorize_workspace(request, workspace_id)
    store = _store(request)
    if callable(getattr(store, "switch_workspace", None)):
        try:
            result = await _store_call(
                request,
                "switch_workspace",
                {"token": context.token, "workspace_id": workspace_id},
                {"session_token": context.token, "workspace_id": workspace_id},
                {"user_id": context.user_id, "workspace_id": workspace_id},
            )
        except _RouteError:
            raise
        except Exception as exc:
            _raise_workspace_missing(exc)
        context = await _complete_context(
            request, result, token_hint=context.token, workspace_id=workspace_id
        )
    return _session_response(context)


@router.get(
    "/workspaces/{workspace_id}/members",
    response_model=WorkspaceMemberListResponse,
    responses=_ERROR_RESPONSES,
)
@_guarded
async def list_members(workspace_id: str, request: Request):
    context = await _authorize_workspace(
        request, workspace_id, Permission.MANAGE_ACCESS
    )
    try:
        result = await _store_call(
            request,
            "list_members",
            {"workspace_id": workspace_id, "actor_user_id": context.user_id},
            {"workspace_id": workspace_id},
        )
    except _RouteError:
        raise
    except Exception as exc:
        _raise_workspace_missing(exc)
    return WorkspaceMemberListResponse(
        workspace_id=workspace_id,
        members=[_member_model(row) for row in _rows(result, "members", "items")],
    )


@router.patch(
    "/workspaces/{workspace_id}/members/{member_id}",
    response_model=WorkspaceMemberResponse,
    responses=_ERROR_RESPONSES,
)
@_guarded
async def update_member(
    workspace_id: str,
    member_id: str,
    payload: WorkspaceMemberUpdateRequest,
    request: Request,
):
    context = await _authorize_workspace(
        request, workspace_id, Permission.MANAGE_ACCESS
    )
    try:
        result = await _store_call(
            request,
            ("update_member", "update_member_role"),
            {
                "workspace_id": workspace_id,
                "member_user_id": member_id,
                "role": payload.role,
                "actor_user_id": context.user_id,
                "expected_revision": payload.expected_revision,
            },
            {
                "workspace_id": workspace_id,
                "member_id": member_id,
                "role": payload.role,
                "expected_revision": payload.expected_revision,
                "actor_user_id": context.user_id,
            },
            {
                "workspace_id": workspace_id,
                "user_id": member_id,
                "role": payload.role,
                "expected_revision": payload.expected_revision,
                "actor_user_id": context.user_id,
            },
            {
                "workspace_id": workspace_id,
                "member_id": member_id,
                "role": payload.role,
                "expected_revision": payload.expected_revision,
                "actor_id": context.user_id,
            },
            {
                "workspace_id": workspace_id,
                "member_id": member_id,
                "role": payload.role,
                "actor_id": context.user_id,
            },
            {
                "workspace_id": workspace_id,
                "member_id": member_id,
                "role": payload.role,
            },
        )
    except _RouteError:
        raise
    except Exception as exc:
        _raise_mutation_failure(
            exc,
            missing_code=ErrorCode.MEMBER_NOT_FOUND,
            missing_message="成员不存在",
        )
    if result is None or result is False:
        raise _RouteError(404, ErrorCode.MEMBER_NOT_FOUND, "成员不存在")
    return WorkspaceMemberResponse(member=_member_model(result))


@router.delete(
    "/workspaces/{workspace_id}/members/{member_id}",
    status_code=204,
    responses=_ERROR_RESPONSES,
)
@_guarded
async def delete_member(workspace_id: str, member_id: str, request: Request):
    context = await _authorize_workspace(
        request, workspace_id, Permission.MANAGE_ACCESS
    )
    try:
        # Resolve a membership ID to the durable user ID before touching ACLs.
        # Resource grants are keyed by user ID, while this endpoint deliberately
        # accepts either identifier for backwards compatibility.
        listed = await _store_call(
            request,
            "list_members",
            {"workspace_id": workspace_id, "actor_user_id": context.user_id},
            {"workspace_id": workspace_id},
        )
        target = next(
            (
                _member_model(row)
                for row in _rows(listed, "members", "items")
                if member_id
                in {
                    _text(_mapping(row), "member_id", "membership_id", "id"),
                    _text(_mapping(row), "user_id"),
                    _text(
                        _nested_mapping(_mapping(row), "user", "account"),
                        "user_id",
                        "id",
                    ),
                }
            ),
            None,
        )
        if target is None:
            raise _RouteError(404, ErrorCode.MEMBER_NOT_FOUND, "成员不存在")
        if target.role == Role.OWNER.value:
            raise _RouteError(403, ErrorCode.FORBIDDEN, "工作区所有者不能被移除")

        # ACL cleanup intentionally precedes the identity mutation.  If the ACL
        # transaction fails, membership remains and the request is retryable. If
        # the later identity mutation loses a race, the surviving membership has
        # fewer privileges, which is the safe failure direction.
        await _revoke_all_member_grants(
            request,
            workspace_id=workspace_id,
            user_id=target.user_id,
            membership_id=target.member_id,
        )
        deleted = await _store_call(
            request,
            ("delete_member", "remove_member"),
            {
                "workspace_id": workspace_id,
                "member_user_id": target.member_id,
                "actor_user_id": context.user_id,
            },
            {
                "workspace_id": workspace_id,
                "member_id": target.member_id,
                "actor_user_id": context.user_id,
            },
            {
                "workspace_id": workspace_id,
                "user_id": target.member_id,
                "actor_user_id": context.user_id,
            },
            {
                "workspace_id": workspace_id,
                "member_id": target.member_id,
                "actor_id": context.user_id,
            },
            {"workspace_id": workspace_id, "member_id": target.member_id},
        )
    except _RouteError:
        raise
    except Exception as exc:
        _raise_mutation_failure(
            exc,
            missing_code=ErrorCode.MEMBER_NOT_FOUND,
            missing_message="成员不存在",
        )
    if deleted is False:
        raise _RouteError(404, ErrorCode.MEMBER_NOT_FOUND, "成员不存在")
    return Response(status_code=204)


@router.post(
    "/workspaces/{workspace_id}/invites",
    response_model=WorkspaceInviteCreateResponse,
    status_code=201,
    responses=_ERROR_RESPONSES,
)
@_guarded
async def create_invite(
    workspace_id: str, payload: WorkspaceInviteCreateRequest, request: Request
):
    context = await _authorize_workspace(
        request, workspace_id, Permission.MANAGE_ACCESS
    )
    try:
        result = await _store_call(
            request,
            ("create_invite", "invite_member"),
            {
                "workspace_id": workspace_id,
                "email": payload.email,
                "role": payload.role,
                "actor_user_id": context.user_id,
            },
            {
                "workspace_id": workspace_id,
                "email": payload.email,
                "role": payload.role,
                "created_by": context.user_id,
            },
            {
                "workspace_id": workspace_id,
                "email": payload.email,
                "role": payload.role,
                "actor_id": context.user_id,
            },
        )
    except _RouteError:
        raise
    except Exception as exc:
        _raise_mutation_failure(
            exc,
            missing_code=ErrorCode.WORKSPACE_NOT_FOUND,
            missing_message="工作区不存在",
        )
    token, raw_invite = _split_context_result(result)
    if isinstance(result, tuple) and len(result) == 2:
        first, second = result
        if isinstance(first, str):
            token, raw_invite = first, _mapping(second)
        elif isinstance(second, str):
            raw_invite, token = _mapping(first), second
    else:
        result_payload = _mapping(result)
        raw_invite = _nested_mapping(result_payload, "invite") or result_payload
        token = _text(
            result_payload, "invite_token", "raw_token", "token", default=token
        )
    if not token:
        raise _RouteError(503, ErrorCode.INTERNAL_ERROR, "身份服务返回了无效结果")
    return WorkspaceInviteCreateResponse(
        invite=_invite_model(raw_invite), invite_token=token
    )


@router.get(
    "/workspaces/{workspace_id}/invites",
    response_model=WorkspaceInviteListResponse,
    responses=_ERROR_RESPONSES,
)
@_guarded
async def list_invites(workspace_id: str, request: Request):
    context = await _authorize_workspace(
        request, workspace_id, Permission.MANAGE_ACCESS
    )
    try:
        result = await _store_call(
            request,
            "list_invites",
            {"workspace_id": workspace_id, "actor_user_id": context.user_id},
            {"workspace_id": workspace_id},
        )
    except _RouteError:
        raise
    except Exception as exc:
        _raise_workspace_missing(exc)
    return WorkspaceInviteListResponse(
        workspace_id=workspace_id,
        invites=[_invite_model(row) for row in _rows(result, "invites", "items")],
    )


@router.delete(
    "/workspaces/{workspace_id}/invites/{invite_id}",
    status_code=204,
    responses=_ERROR_RESPONSES,
)
@_guarded
async def revoke_invite(workspace_id: str, invite_id: str, request: Request):
    context = await _authorize_workspace(
        request, workspace_id, Permission.MANAGE_ACCESS
    )
    try:
        revoked = await _store_call(
            request,
            ("revoke_invite", "delete_invite"),
            {
                "workspace_id": workspace_id,
                "invite_id": invite_id,
                "actor_user_id": context.user_id,
            },
            {"invite_id": invite_id, "actor_user_id": context.user_id},
            {
                "workspace_id": workspace_id,
                "invite_id": invite_id,
                "actor_id": context.user_id,
            },
            {"workspace_id": workspace_id, "invite_id": invite_id},
        )
    except _RouteError:
        raise
    except Exception as exc:
        _raise_mutation_failure(
            exc,
            missing_code=ErrorCode.INVITE_NOT_FOUND,
            missing_message="邀请不存在",
        )
    if revoked is False:
        raise _RouteError(404, ErrorCode.INVITE_NOT_FOUND, "邀请不存在")
    return Response(status_code=204)


@router.post(
    "/auth/invitations/accept",
    response_model=AuthSessionResponse,
    responses=_ERROR_RESPONSES,
)
@_guarded
async def accept_invite(payload: WorkspaceInviteAcceptRequest, request: Request):
    _enforce_public_rate_limit(request, "accept-invite")
    authorization = request.headers.get("authorization", "")
    has_bearer = authorization.lower().startswith("bearer ") and bool(
        authorization[7:].strip()
    )
    has_fallback = bool(request.headers.get("x-api-key", "").strip())
    context = await _authenticate(request) if has_bearer or has_fallback else None
    if context is not None and any(
        value is not None
        for value in (payload.email, payload.password, payload.display_name)
    ):
        raise _RouteError(400, ErrorCode.BAD_REQUEST, "登录状态下不应提交账号凭据")
    if context is None and (payload.email is None or payload.password is None):
        raise _RouteError(
            400,
            ErrorCode.INVITE_INVALID,
            "邀请无效或已过期",
        )
    try:
        if context is not None:
            result = await _store_call(
                request,
                "accept_invite",
                {"token": payload.token, "user_id": context.user_id},
                {"invite_token": payload.token, "user_id": context.user_id},
            )
        else:
            result = await _store_call(
                request,
                "accept_invite",
                {
                    "token": payload.token,
                    "user_id": None,
                    "email": payload.email,
                    "password": payload.password,
                    "display_name": payload.display_name,
                },
                {
                    "invite_token": payload.token,
                    "email": payload.email,
                    "password": payload.password,
                    "display_name": payload.display_name,
                },
            )
    except _RouteError:
        raise
    except Exception as exc:
        if _is_expected_auth_failure(exc) or _is_not_found(exc) or _is_conflict(exc):
            # Invalid, expired, already-used, and wrong-recipient tokens are one
            # opaque public outcome.
            raise _RouteError(
                400, ErrorCode.INVITE_INVALID, "邀请无效或已过期"
            ) from exc
        raise _RouteError(503, ErrorCode.INTERNAL_ERROR, "身份服务暂不可用") from exc
    if context is None:
        accepted_context = await _complete_context(request, result)
        return _session_response(accepted_context)

    result_payload = _mapping(result)
    workspace_payload = _nested_mapping(result_payload, "workspace", "tenant")
    if not workspace_payload:
        workspace_payload = result_payload
    workspace_id = _text(workspace_payload, "workspace_id", "id", "tenant_id")
    if not workspace_id:
        raise _RouteError(503, ErrorCode.INTERNAL_ERROR, "身份服务返回了无效结果")
    accepted_context = await _authorize_workspace(request, workspace_id)
    return _session_response(accepted_context)


__all__ = ["router"]
