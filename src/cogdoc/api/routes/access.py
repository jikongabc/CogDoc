from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, is_dataclass
from functools import wraps
from typing import Any, Literal

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from cogdoc.api.resource_access import (
    ResourceAccessConflictError,
    ResourceAccessNotFoundError,
)
from cogdoc.api.schemas import ErrorCode, ErrorResponse, build_error_response
from cogdoc.api.tenancy import Permission, Principal, Role
from cogdoc.api.tenant_scope import (
    KnowledgeBaseScope,
    is_user_session_principal,
    request_principal,
)


router = APIRouter(prefix="/v1", tags=["resource-access"])

_ERROR_RESPONSES = {
    400: {"model": ErrorResponse},
    403: {"model": ErrorResponse},
    404: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
    503: {"model": ErrorResponse},
}


class _ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)


def _canonical_text(value: str, *, field: str, max_length: int = 160) -> str:
    if not value or value != value.strip():
        raise ValueError(f"{field} must be a non-empty canonical string")
    if len(value) > max_length:
        raise ValueError(f"{field} is too long")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"{field} must not contain control characters")
    return value


class KnowledgeBasePolicyUpdateRequest(_ApiModel):
    schema_version: Literal["v1"] = "v1"
    policy: Literal["workspace", "private"]
    role_ids: list[str] | None = Field(default=None, max_length=100)


class KnowledgeBasePolicyResponse(_ApiModel):
    schema_version: Literal["v1"] = "v1"
    kb_id: str
    configured: bool
    owner_id: str
    policy: Literal["workspace", "private"] | None
    acl_epoch: int = Field(ge=0)
    created_at: str = ""
    updated_at: str = ""
    role_ids: list[str] = Field(default_factory=list)


class DocumentPolicyUpdateRequest(_ApiModel):
    schema_version: Literal["v1"] = "v1"
    policy: Literal["inherit", "workspace", "private"]
    source: str | None = Field(default=None, min_length=1, max_length=1024)
    role_ids: list[str] | None = Field(default=None, max_length=100)

    @field_validator("source")
    @classmethod
    def _validate_source(cls, value: str | None) -> str | None:
        return (
            None
            if value is None
            else _canonical_text(value, field="source", max_length=1024)
        )


class DocumentPolicyResponse(_ApiModel):
    schema_version: Literal["v1"] = "v1"
    kb_id: str
    document_id: str
    source: str
    owner_id: str
    policy: Literal["inherit", "workspace", "private"]
    acl_epoch: int = Field(ge=0)
    created_at: str = ""
    updated_at: str = ""
    role_ids: list[str] = Field(default_factory=list)


class SubjectGrantRequest(_ApiModel):
    schema_version: Literal["v1"] = "v1"
    subject_id: str = Field(min_length=1, max_length=160)
    role: Role

    @field_validator("subject_id")
    @classmethod
    def _validate_subject_id(cls, value: str) -> str:
        return _canonical_text(value, field="subject_id")


class SubjectGrantResponse(_ApiModel):
    schema_version: Literal["v1"] = "v1"
    kb_id: str
    document_id: str | None = None
    subject_id: str
    role: Role
    acl_epoch: int = Field(ge=0)
    created_at: str = ""
    updated_at: str = ""


class SubjectGrantListResponse(_ApiModel):
    schema_version: Literal["v1"] = "v1"
    kb_id: str
    document_id: str | None = None
    grants: list[SubjectGrantResponse] = Field(default_factory=list)


class _RouteError(RuntimeError):
    def __init__(self, status_code: int, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message

    def response(self) -> JSONResponse:
        return JSONResponse(
            status_code=self.status_code,
            content=build_error_response(self.code, self.message).model_dump(),
        )


def _guarded(function: Callable) -> Callable:
    @wraps(function)
    async def wrapped(*args, **kwargs):
        try:
            return await function(*args, **kwargs)
        except _RouteError as exc:
            return exc.response()
        except Exception:
            # Store payload validation and unforeseen dependency failures must
            # never turn into a permissive or partially serialized response.
            return _RouteError(
                503, ErrorCode.INTERNAL_ERROR, "资源访问服务暂不可用"
            ).response()

    return wrapped


def _management_context(
    request: Request, kb_id: str
) -> tuple[Principal, KnowledgeBaseScope, Any]:
    principal = request_principal(request)
    if not principal.allows(Permission.MANAGE_ACCESS):
        raise _RouteError(403, ErrorCode.FORBIDDEN, "当前身份无权管理资源访问")
    # The ordinary tenant-scope helper also enforces the *current* resource ACL.
    # This management endpoint must be able to create a missing policy or repair
    # a deny-all policy, so it performs the same tenant-local registry resolution
    # without consulting the policy being managed.
    try:
        registry = request.app.state.kb_registry
        resolver = getattr(registry, "resolve", None)
        record = (
            resolver(kb_id, principal.tenant_id)
            if callable(resolver)
            else registry.get(kb_id)
            if principal.tenant_id == "default"
            else None
        )
    except (TypeError, ValueError):
        record = None
    except Exception as exc:
        raise _RouteError(
            503, ErrorCode.INTERNAL_ERROR, "资源访问服务暂不可用"
        ) from exc
    if (
        not isinstance(record, Mapping)
        or str(record.get("tenant_id") or "default") != principal.tenant_id
    ):
        # Do not echo a user-supplied physical ID or reveal another tenant's slug.
        raise _RouteError(404, ErrorCode.KB_NOT_FOUND, "知识库不存在")
    external_id = str(record.get("kb_id") or "")
    storage_id = str(record.get("storage_id") or external_id)
    if not external_id or external_id != kb_id or not storage_id:
        raise _RouteError(503, ErrorCode.INTERNAL_ERROR, "资源访问服务返回无效数据")
    scope = KnowledgeBaseScope(
        tenant_id=principal.tenant_id,
        external_id=external_id,
        storage_id=storage_id,
        owner_id=str(record.get("owner_id") or "default"),
        created_at=str(record.get("created_at") or ""),
    )
    store = getattr(request.app.state, "resource_access_store", None)
    if store is None:
        raise _RouteError(503, ErrorCode.INTERNAL_ERROR, "资源访问服务暂不可用")
    return principal, scope, store


def _store_call(store: Any, operation: str, *args, **kwargs) -> Any:
    function = getattr(store, operation, None)
    if not callable(function):
        raise _RouteError(503, ErrorCode.INTERNAL_ERROR, "资源访问服务暂不可用")
    try:
        return function(*args, **kwargs)
    except (ResourceAccessNotFoundError, ResourceAccessConflictError):
        raise
    except Exception as exc:
        raise _RouteError(
            503, ErrorCode.INTERNAL_ERROR, "资源访问服务暂不可用"
        ) from exc


def _record_mapping(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _RouteError(503, ErrorCode.INTERNAL_ERROR, "资源访问服务返回无效数据")
    return value


def _validate_record_scope(
    row: Mapping[str, Any],
    principal: Principal,
    scope: KnowledgeBaseScope,
    *,
    document_id: str | None | object = ...,
) -> None:
    if (
        str(row.get("tenant_id") or "") != principal.tenant_id
        or str(row.get("kb_id") or "") != scope.storage_id
    ):
        raise _RouteError(503, ErrorCode.INTERNAL_ERROR, "资源访问服务返回无效数据")
    if document_id is not ... and row.get("document_id") != document_id:
        raise _RouteError(503, ErrorCode.INTERNAL_ERROR, "资源访问服务返回无效数据")


def _kb_policy_response(
    row: Mapping[str, Any] | None,
    *,
    principal: Principal,
    scope: KnowledgeBaseScope,
    epoch: int,
    role_ids: Sequence[str] = (),
) -> KnowledgeBasePolicyResponse:
    if row is None:
        return KnowledgeBasePolicyResponse(
            kb_id=scope.external_id,
            configured=False,
            owner_id=scope.owner_id,
            policy=None,
            acl_epoch=epoch,
            role_ids=list(role_ids),
        )
    _validate_record_scope(row, principal, scope)
    return KnowledgeBasePolicyResponse(
        kb_id=scope.external_id,
        configured=True,
        owner_id=str(row.get("owner_id") or ""),
        policy=str(row.get("policy") or ""),
        acl_epoch=int(row.get("acl_epoch", epoch)),
        created_at=str(row.get("created_at") or ""),
        updated_at=str(row.get("updated_at") or ""),
        role_ids=list(role_ids),
    )


def _document_policy_response(
    row: Mapping[str, Any],
    *,
    principal: Principal,
    scope: KnowledgeBaseScope,
    document_id: str,
    role_ids: Sequence[str] = (),
) -> DocumentPolicyResponse:
    _validate_record_scope(row, principal, scope, document_id=document_id)
    return DocumentPolicyResponse(
        kb_id=scope.external_id,
        document_id=document_id,
        source=str(row.get("source") or ""),
        owner_id=str(row.get("owner_id") or ""),
        policy=str(row.get("policy") or ""),
        acl_epoch=int(row.get("acl_epoch", 0)),
        created_at=str(row.get("created_at") or ""),
        updated_at=str(row.get("updated_at") or ""),
        role_ids=list(role_ids),
    )


def _validated_role_ids(request: Request, tenant_id: str, values: Sequence[str]) -> list[str]:
    selected = list(dict.fromkeys(_canonical_text(value, field="role_id") for value in values))
    auth_store = getattr(request.app.state, "auth_store", None)
    if auth_store is None:
        allowed = {role.value for role in Role}
    else:
        rows = auth_store.list_workspace_roles(tenant_id)
        allowed = {
            str(row.get("role_id") or "")
            for row in rows
            if isinstance(row, Mapping)
        }
    if any(role_id not in allowed for role_id in selected):
        raise _RouteError(422, ErrorCode.BAD_REQUEST, "包含不存在的工作区角色")
    return selected


def _grant_response(
    row: Mapping[str, Any],
    *,
    principal: Principal,
    scope: KnowledgeBaseScope,
    document_id: str | None,
) -> SubjectGrantResponse:
    _validate_record_scope(row, principal, scope, document_id=document_id)
    return SubjectGrantResponse(
        kb_id=scope.external_id,
        document_id=document_id,
        subject_id=str(row.get("subject_id") or ""),
        role=str(row.get("role") or ""),
        acl_epoch=int(row.get("acl_epoch", 0)),
        created_at=str(row.get("created_at") or ""),
        updated_at=str(row.get("updated_at") or ""),
    )


def _compatible_membership_call(
    function: Callable, tenant_id: str, subject_id: str
) -> Any:
    variants = (
        {"workspace_id": tenant_id, "subject_id": subject_id},
        {"workspace_id": tenant_id, "user_id": subject_id},
        {"workspace_id": tenant_id, "member_id": subject_id},
        {"tenant_id": tenant_id, "subject_id": subject_id},
        {"tenant_id": tenant_id, "user_id": subject_id},
    )
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
    raise TypeError("unsupported membership lookup signature")


def _membership_is_active(value: Any, tenant_id: str, subject_id: str) -> bool:
    if value is None or value is False:
        return False
    if value is True:
        return True
    if not isinstance(value, Mapping):
        if hasattr(value, "model_dump"):
            value = value.model_dump()
        elif is_dataclass(value) and not isinstance(value, type):
            value = asdict(value)
        else:
            return False
    if not isinstance(value, Mapping):
        return False
    nested = value.get("membership") or value.get("member")
    row = nested if isinstance(nested, Mapping) else value
    if not row:
        return False
    status = str(row.get("status") or "active").casefold()
    if status in {"disabled", "inactive", "removed", "revoked", "deleted"}:
        return False
    row_tenant = str(
        row.get("workspace_id") or row.get("tenant_id") or row.get("workspace") or ""
    )
    row_subject = str(
        row.get("subject_id")
        or row.get("user_id")
        or row.get("member_id")
        or row.get("id")
        or ""
    )
    return (not row_tenant or row_tenant == tenant_id) and (
        not row_subject or row_subject == subject_id
    )


def _membership_incarnation_id(value: Any) -> str | None:
    if not isinstance(value, Mapping):
        if hasattr(value, "model_dump"):
            value = value.model_dump()
        elif is_dataclass(value) and not isinstance(value, type):
            value = asdict(value)
        else:
            return None
    if not isinstance(value, Mapping):
        return None
    nested = value.get("membership") or value.get("member")
    row = nested if isinstance(nested, Mapping) else value
    membership_id = (
        row.get("member_id")
        or row.get("membership_id")
        or value.get("member_id")
        or value.get("membership_id")
    )
    if not isinstance(membership_id, str):
        return None
    try:
        return _canonical_text(membership_id, field="member_id")
    except (TypeError, ValueError):
        return None


def _require_workspace_member(
    request: Request, principal: Principal, subject_id: str
) -> str | None:
    # API-key-only deployments have no user directory and retain their legacy
    # subject-grant behavior.  Once an AuthStore is configured, every caller --
    # including a service API key -- must bind a grant to the target member's
    # durable membership incarnation.
    auth_store = getattr(request.app.state, "auth_store", None)
    if auth_store is None:
        if is_user_session_principal(principal):
            raise _RouteError(503, ErrorCode.INTERNAL_ERROR, "身份服务暂不可用")
        return None
    function = next(
        (
            candidate
            for name in ("membership", "get_member")
            if callable(candidate := getattr(auth_store, name, None))
        ),
        None,
    )
    if function is None:
        raise _RouteError(503, ErrorCode.INTERNAL_ERROR, "身份服务暂不可用")
    try:
        membership = _compatible_membership_call(
            function, principal.tenant_id, subject_id
        )
    except (KeyError, LookupError):
        membership = None
    except Exception as exc:
        raise _RouteError(503, ErrorCode.INTERNAL_ERROR, "身份服务暂不可用") from exc
    if not _membership_is_active(membership, principal.tenant_id, subject_id):
        raise _RouteError(422, ErrorCode.BAD_REQUEST, "授权对象不是当前工作区成员")
    membership_id = _membership_incarnation_id(membership)
    if membership_id is None:
        raise _RouteError(503, ErrorCode.INTERNAL_ERROR, "身份服务返回无效成员数据")
    return membership_id


def _document_id_or_404(document_id: str) -> str:
    try:
        return _canonical_text(document_id, field="document_id")
    except (TypeError, ValueError) as exc:
        raise _RouteError(404, ErrorCode.DOCUMENT_NOT_FOUND, "文档不存在") from exc


def _require_document_policy(
    store: Any,
    principal: Principal,
    scope: KnowledgeBaseScope,
    document_id: str,
) -> Mapping[str, Any]:
    value = _store_call(
        store,
        "get_document_policy",
        principal.tenant_id,
        scope.storage_id,
        document_id,
    )
    if value is None:
        raise _RouteError(404, ErrorCode.DOCUMENT_NOT_FOUND, "文档不存在")
    row = _record_mapping(value)
    _validate_record_scope(row, principal, scope, document_id=document_id)
    return row


def _subject_id_or_404(subject_id: str) -> str:
    try:
        return _canonical_text(subject_id, field="subject_id")
    except (TypeError, ValueError) as exc:
        raise _RouteError(404, ErrorCode.MEMBER_NOT_FOUND, "授权记录不存在") from exc


@router.get(
    "/knowledge-bases/{kb_id}/access",
    response_model=KnowledgeBasePolicyResponse,
    responses=_ERROR_RESPONSES,
)
@_guarded
async def get_kb_access_policy(kb_id: str, request: Request):
    principal, scope, store = _management_context(request, kb_id)
    row = _store_call(store, "get_kb_policy", principal.tenant_id, scope.storage_id)
    epoch = _store_call(store, "acl_epoch", principal.tenant_id, scope.storage_id)
    return _kb_policy_response(
        _record_mapping(row) if row is not None else None,
        principal=principal,
        scope=scope,
        epoch=int(epoch),
        role_ids=_store_call(
            store, "list_kb_roles", principal.tenant_id, scope.storage_id
        ),
    )


@router.patch(
    "/knowledge-bases/{kb_id}/access",
    response_model=KnowledgeBasePolicyResponse,
    responses=_ERROR_RESPONSES,
)
@router.put(
    "/knowledge-bases/{kb_id}/access",
    response_model=KnowledgeBasePolicyResponse,
    responses=_ERROR_RESPONSES,
    include_in_schema=False,
)
@_guarded
async def update_kb_access_policy(
    kb_id: str, body: KnowledgeBasePolicyUpdateRequest, request: Request
):
    principal, scope, store = _management_context(request, kb_id)
    existing_value = _store_call(
        store, "get_kb_policy", principal.tenant_id, scope.storage_id
    )
    existing = (
        _record_mapping(existing_value) if existing_value is not None else None
    )
    row = _store_call(
        store,
        "set_kb_policy",
        principal.tenant_id,
        scope.storage_id,
        scope.owner_id,
        body.policy,
        owner_membership_id=(
            existing.get("owner_membership_id") if existing is not None else None
        ),
    )
    record = _record_mapping(row)
    if body.role_ids is not None:
        selected_roles = _validated_role_ids(
            request, principal.tenant_id, body.role_ids
        )
        roles_result = _store_call(
            store,
            "replace_kb_roles",
            principal.tenant_id,
            scope.storage_id,
            selected_roles,
        )
        record["acl_epoch"] = int(
            _record_mapping(roles_result).get("acl_epoch", record.get("acl_epoch", 0))
        )
    return _kb_policy_response(
        record,
        principal=principal,
        scope=scope,
        epoch=int(record.get("acl_epoch", 0)),
        role_ids=_store_call(
            store, "list_kb_roles", principal.tenant_id, scope.storage_id
        ),
    )


@router.get(
    "/knowledge-bases/{kb_id}/documents/{document_id}/access",
    response_model=DocumentPolicyResponse,
    responses=_ERROR_RESPONSES,
)
@_guarded
async def get_document_access_policy(kb_id: str, document_id: str, request: Request):
    principal, scope, store = _management_context(request, kb_id)
    document_id = _document_id_or_404(document_id)
    row = _store_call(
        store,
        "get_document_policy",
        principal.tenant_id,
        scope.storage_id,
        document_id,
    )
    if row is None:
        raise _RouteError(404, ErrorCode.DOCUMENT_NOT_FOUND, "文档不存在")
    return _document_policy_response(
        _record_mapping(row),
        principal=principal,
        scope=scope,
        document_id=document_id,
        role_ids=_store_call(
            store,
            "list_document_roles",
            principal.tenant_id,
            scope.storage_id,
            document_id,
        ),
    )


@router.patch(
    "/knowledge-bases/{kb_id}/documents/{document_id}/access",
    response_model=DocumentPolicyResponse,
    responses=_ERROR_RESPONSES,
)
@router.put(
    "/knowledge-bases/{kb_id}/documents/{document_id}/access",
    response_model=DocumentPolicyResponse,
    responses=_ERROR_RESPONSES,
    include_in_schema=False,
)
@_guarded
async def update_document_access_policy(
    kb_id: str,
    document_id: str,
    body: DocumentPolicyUpdateRequest,
    request: Request,
):
    principal, scope, store = _management_context(request, kb_id)
    document_id = _document_id_or_404(document_id)
    existing_value = _store_call(
        store,
        "get_document_policy",
        principal.tenant_id,
        scope.storage_id,
        document_id,
    )
    existing = _record_mapping(existing_value) if existing_value is not None else None
    if existing is not None:
        _validate_record_scope(existing, principal, scope, document_id=document_id)
    source = body.source or (str(existing.get("source") or "") if existing else "")
    if not source:
        raise _RouteError(
            422,
            ErrorCode.BAD_REQUEST,
            "首次配置文档访问策略时必须提供 source",
        )
    owner_id = str(existing.get("owner_id") or "") if existing else None
    owner_membership_id = (
        existing.get("owner_membership_id") if existing is not None else None
    )
    try:
        row = _store_call(
            store,
            "set_document_policy",
            principal.tenant_id,
            scope.storage_id,
            document_id,
            source,
            owner_id,
            body.policy,
            owner_membership_id=owner_membership_id,
        )
    except ResourceAccessNotFoundError as exc:
        raise _RouteError(409, ErrorCode.BAD_REQUEST, "请先配置知识库访问策略") from exc
    except ResourceAccessConflictError as exc:
        raise _RouteError(409, ErrorCode.BAD_REQUEST, "source 已绑定其他文档") from exc
    if body.role_ids is not None:
        selected_roles = _validated_role_ids(
            request, principal.tenant_id, body.role_ids
        )
        roles_result = _store_call(
            store,
            "replace_document_roles",
            principal.tenant_id,
            scope.storage_id,
            document_id,
            selected_roles,
        )
        row = {**_record_mapping(row), "acl_epoch": _record_mapping(roles_result).get("acl_epoch", 0)}
    return _document_policy_response(
        _record_mapping(row),
        principal=principal,
        scope=scope,
        document_id=document_id,
        role_ids=_store_call(
            store,
            "list_document_roles",
            principal.tenant_id,
            scope.storage_id,
            document_id,
        ),
    )


def _list_subject_grants(
    request: Request,
    kb_id: str,
    *,
    document_id: str | None,
) -> SubjectGrantListResponse:
    principal, scope, store = _management_context(request, kb_id)
    if document_id is not None:
        document_id = _document_id_or_404(document_id)
        _require_document_policy(store, principal, scope, document_id)
    arguments = (principal.tenant_id, scope.storage_id)
    kwargs = {} if document_id is None else {"document_id": document_id}
    rows = _store_call(store, "list_grants", *arguments, **kwargs)
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes, bytearray)):
        raise _RouteError(503, ErrorCode.INTERNAL_ERROR, "资源访问服务返回无效数据")
    grants: list[SubjectGrantResponse] = []
    for value in rows:
        row = _record_mapping(value)
        # ResourceAccessStore returns every grant when document_id is omitted;
        # the KB endpoint intentionally exposes only KB-level grants.
        if document_id is None and row.get("document_id") is not None:
            continue
        grants.append(
            _grant_response(
                row,
                principal=principal,
                scope=scope,
                document_id=document_id,
            )
        )
    return SubjectGrantListResponse(
        kb_id=scope.external_id,
        document_id=document_id,
        grants=grants,
    )


def _grant_subject(
    request: Request,
    kb_id: str,
    body: SubjectGrantRequest,
    *,
    document_id: str | None,
) -> SubjectGrantResponse:
    principal, scope, store = _management_context(request, kb_id)
    if document_id is not None:
        document_id = _document_id_or_404(document_id)
        _require_document_policy(store, principal, scope, document_id)
    membership_id = _require_workspace_member(request, principal, body.subject_id)
    try:
        arguments = (
            principal.tenant_id,
            scope.storage_id,
            body.subject_id,
            body.role,
            document_id,
        )
        row = (
            _store_call(store, "grant_subject", *arguments)
            if membership_id is None
            else _store_call(
                store,
                "grant_subject",
                *arguments,
                membership_id=membership_id,
            )
        )
    except ResourceAccessNotFoundError as exc:
        if document_id is not None:
            raise _RouteError(404, ErrorCode.DOCUMENT_NOT_FOUND, "文档不存在") from exc
        raise _RouteError(409, ErrorCode.BAD_REQUEST, "请先配置知识库访问策略") from exc
    except ResourceAccessConflictError as exc:
        raise _RouteError(
            409,
            ErrorCode.AUTH_CONFLICT,
            "成员关系已变更，请刷新后重试",
        ) from exc
    return _grant_response(
        _record_mapping(row),
        principal=principal,
        scope=scope,
        document_id=document_id,
    )


def _revoke_subject(
    request: Request,
    kb_id: str,
    subject_id: str,
    *,
    document_id: str | None,
) -> Response:
    principal, scope, store = _management_context(request, kb_id)
    if document_id is not None:
        document_id = _document_id_or_404(document_id)
        _require_document_policy(store, principal, scope, document_id)
    subject_id = _subject_id_or_404(subject_id)
    revoked = _store_call(
        store,
        "revoke_subject",
        principal.tenant_id,
        scope.storage_id,
        subject_id,
        document_id,
    )
    if revoked is not True:
        raise _RouteError(404, ErrorCode.MEMBER_NOT_FOUND, "授权记录不存在")
    return Response(status_code=204)


@router.get(
    "/knowledge-bases/{kb_id}/access/grants",
    response_model=SubjectGrantListResponse,
    responses=_ERROR_RESPONSES,
)
@_guarded
async def list_kb_subject_grants(kb_id: str, request: Request):
    return _list_subject_grants(request, kb_id, document_id=None)


@router.post(
    "/knowledge-bases/{kb_id}/access/grants",
    response_model=SubjectGrantResponse,
    responses=_ERROR_RESPONSES,
)
@_guarded
async def grant_kb_subject(kb_id: str, body: SubjectGrantRequest, request: Request):
    return _grant_subject(request, kb_id, body, document_id=None)


@router.delete(
    "/knowledge-bases/{kb_id}/access/grants/{subject_id}",
    status_code=204,
    responses=_ERROR_RESPONSES,
)
@_guarded
async def revoke_kb_subject(kb_id: str, subject_id: str, request: Request):
    return _revoke_subject(request, kb_id, subject_id, document_id=None)


@router.get(
    "/knowledge-bases/{kb_id}/documents/{document_id}/access/grants",
    response_model=SubjectGrantListResponse,
    responses=_ERROR_RESPONSES,
)
@_guarded
async def list_document_subject_grants(kb_id: str, document_id: str, request: Request):
    return _list_subject_grants(request, kb_id, document_id=document_id)


@router.post(
    "/knowledge-bases/{kb_id}/documents/{document_id}/access/grants",
    response_model=SubjectGrantResponse,
    responses=_ERROR_RESPONSES,
)
@_guarded
async def grant_document_subject(
    kb_id: str,
    document_id: str,
    body: SubjectGrantRequest,
    request: Request,
):
    return _grant_subject(request, kb_id, body, document_id=document_id)


@router.delete(
    "/knowledge-bases/{kb_id}/documents/{document_id}/access/grants/{subject_id}",
    status_code=204,
    responses=_ERROR_RESPONSES,
)
@_guarded
async def revoke_document_subject(
    kb_id: str, document_id: str, subject_id: str, request: Request
):
    return _revoke_subject(request, kb_id, subject_id, document_id=document_id)


__all__ = [
    "DocumentPolicyResponse",
    "DocumentPolicyUpdateRequest",
    "KnowledgeBasePolicyResponse",
    "KnowledgeBasePolicyUpdateRequest",
    "SubjectGrantListResponse",
    "SubjectGrantRequest",
    "SubjectGrantResponse",
    "router",
]
