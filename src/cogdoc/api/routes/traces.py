import json
import re
from collections.abc import Mapping
from datetime import datetime, timezone
from json import JSONDecodeError
from pathlib import Path
from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from cogdoc.api.schemas import (
    ErrorCode,
    ErrorResponse,
    TraceListItem,
    TraceListResponse,
    TraceResponse,
    build_error_response,
)
from cogdoc.observability.trace import build_trace_payload, trace_dir, trace_path
from cogdoc.service.kb_epoch import shared_epoch_store
from cogdoc.api.tenant_scope import (
    externalize_kb_fields,
    internal_session_id,
    request_principal,
    resolve_kb_scope,
    row_is_authorized,
    session_id_is_authorized,
    scope_for_storage_id,
)


router = APIRouter(prefix="/v1", tags=["traces"])
_TRACE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


# 判断跟踪标识是否安全。
def _is_safe_trace_id(trace_id: str) -> bool:
    return bool(_TRACE_ID_PATTERN.fullmatch(trace_id))


# 构建跟踪查询错误响应。
def _trace_error(code: ErrorCode, message: str, status: int) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content=build_error_response(code, message).model_dump(),
    )


# 兼容旧版跟踪载荷。
def _normalize_trace_payload(trace_id: str, payload: dict) -> dict:
    if payload.get("schema_version"):
        return payload
    return build_trace_payload(
        trace_id=str(payload.get("trace_id") or trace_id),
        request_id=str(
            payload.get("request_id") or payload.get("trace_id") or trace_id
        ),
        task_type=str(payload.get("task_type") or "unknown"),
        steps=list(payload.get("steps") or []),
        status="ok",
    )


# 处理modifiedAT。
def _modified_at(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()


# 处理跟踪listitem。
def _trace_list_item(
    path: Path,
    doc_id: str = "",
    session_id: str = "",
    *,
    request: Request | None = None,
) -> TraceListItem | None:
    trace_id = path.stem
    if not _is_safe_trace_id(trace_id):
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        trace = TraceResponse.model_validate(
            _normalize_trace_payload(trace_id, payload)
        )
        trace_doc_id = str(trace.config.get("doc_id") or "")
        if doc_id and trace_doc_id != doc_id:
            return None
        if session_id and str(trace.config.get("session_id") or "") != session_id:
            return None
        if request is not None:
            scope = scope_for_storage_id(request, trace_doc_id)
            trace_session_id = str(trace.config.get("session_id") or "")
            external_doc_id = _external_trace_doc_id(request, trace_doc_id)
            acl_store = getattr(request.app.state, "resource_access_store", None)
            if (
                external_doc_id is None
                or not _trace_incarnation_is_current(
                    request, trace_doc_id, trace.config
                )
                or not session_id_is_authorized(request, trace_session_id)
                or (
                    acl_store is not None
                    and (
                        scope is None
                        or not row_is_authorized(
                            request,
                            scope,
                            {
                                "kb_id": trace_doc_id,
                                "config": trace.config,
                                "output": trace.output,
                            },
                        )
                    )
                )
            ):
                return None
        return TraceListItem(
            trace_id=trace.trace_id,
            request_id=trace.request_id,
            query_preview=str(trace.config.get("query_preview") or ""),
            task_type=trace.task_type,
            status=trace.status,
            duration_ms=trace.duration_ms,
            modified_at=_modified_at(path),
            summary=trace.summary,
        )
    except (OSError, JSONDecodeError, ValidationError, TypeError, ValueError):
        return None


def _external_trace_doc_id(request: Request, storage_id: str) -> str | None:
    """Authorize one persisted trace namespace and return its public KB ID.

    A physical ID is never reinterpreted as a logical slug for named tenants.
    That distinction prevents an attacker from registering another tenant's
    opaque storage ID as its own KB slug and then using the alias to read the
    other tenant's trace.  Only the legacy default workspace may fall back to
    an unregistered direct ID; unscoped legacy traces are likewise local-only.
    """

    principal = request_principal(request)
    if not storage_id:
        return "" if principal.tenant_id == "default" else None

    registry = request.app.state.kb_registry
    getter = getattr(registry, "get_by_storage_id", None)
    try:
        persisted_record = (
            getter(storage_id) if callable(getter) else registry.get(storage_id)
        )
    except (TypeError, ValueError):
        return None
    if persisted_record is not None:
        scope = scope_for_storage_id(request, storage_id)
        return scope.external_id if scope is not None else None

    if principal.tenant_id != "default":
        return None
    if storage_id.startswith("t-"):
        return None
    scope = resolve_kb_scope(
        request,
        storage_id,
        allow_legacy_default=True,
    )
    return scope.external_id if scope is not None else None


def _trace_incarnation_is_current(
    request: Request, storage_id: str, config: Mapping[str, object]
) -> bool:
    """Reject traces from an earlier same-slug KB incarnation."""

    registry = request.app.state.kb_registry
    current = getattr(registry, "current", None)
    try:
        current_epoch = (
            int(current(storage_id))
            if callable(current)
            else shared_epoch_store().current(storage_id)
        )
    except (KeyError, TypeError, ValueError, RuntimeError):
        return False
    trace_epoch = config.get("kb_epoch")
    if trace_epoch is None:
        # Legacy epochs zero and one predate incarnation-aware traces and cannot
        # contain data from an earlier incarnation. Once the epoch advances,
        # ambiguity is resolved fail-closed instead of exposing an old trace.
        return current_epoch <= 1
    return type(trace_epoch) is int and trace_epoch == current_epoch


# 列出最近跟踪文件。
@router.get("/traces", response_model=TraceListResponse)
async def list_traces(
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
    doc_id: str = Query(default=""),
    session_id: str = Query(default=""),
):
    storage_doc_id = ""
    if doc_id:
        scope = resolve_kb_scope(request, doc_id, allow_legacy_default=True)
        if scope is None:
            return TraceListResponse()
        storage_doc_id = scope.storage_id
    internal_trace_session_id = internal_session_id(request, session_id) or ""
    base_dir = trace_dir()
    if not base_dir.exists() or not base_dir.is_dir():
        return TraceListResponse()
    candidates = []
    for path in base_dir.glob("*.json"):
        try:
            candidates.append((path.stat().st_mtime, path))
        except OSError:
            continue
    traces = []
    for _, path in sorted(candidates, reverse=True):
        item = _trace_list_item(
            path,
            doc_id=storage_doc_id,
            session_id=internal_trace_session_id,
            request=request,
        )
        if item is not None:
            traces.append(item)
        if len(traces) >= limit:
            break
    return TraceListResponse(traces=traces)


# 查询单次请求的跟踪文件。
@router.get(
    "/traces/{trace_id}",
    response_model=TraceResponse,
    responses={404: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
)
async def get_trace(trace_id: str, request: Request):
    if not _is_safe_trace_id(trace_id):
        return _trace_error(ErrorCode.TRACE_NOT_FOUND, f"trace 不存在: {trace_id}", 404)
    path = trace_path(trace_id)
    if not path.exists() or not path.is_file():
        return _trace_error(ErrorCode.TRACE_NOT_FOUND, f"trace 不存在: {trace_id}", 404)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except JSONDecodeError:
        return _trace_error(
            ErrorCode.INTERNAL_ERROR, f"trace 文件损坏: {trace_id}", 500
        )
    normalized = _normalize_trace_payload(trace_id, payload)
    config = normalized.get("config")
    storage_id = str(config.get("doc_id") or "") if isinstance(config, dict) else ""
    external_doc_id = _external_trace_doc_id(request, storage_id)
    trace_session_id = (
        str(config.get("session_id") or "") if isinstance(config, dict) else ""
    )
    scope = scope_for_storage_id(request, storage_id) if storage_id else None
    acl_store = getattr(request.app.state, "resource_access_store", None)
    if (
        external_doc_id is None
        or not _trace_incarnation_is_current(
            request, storage_id, config if isinstance(config, dict) else {}
        )
        or not session_id_is_authorized(request, trace_session_id)
        or (
            acl_store is not None
            and (scope is None or not row_is_authorized(request, scope, normalized))
        )
    ):
        return _trace_error(ErrorCode.TRACE_NOT_FOUND, f"trace 不存在: {trace_id}", 404)
    public_payload = externalize_kb_fields(normalized, request)
    if isinstance(config, dict):
        public_payload = {
            **public_payload,
            "config": {**public_payload.get("config", {}), "doc_id": external_doc_id},
        }
    return TraceResponse.model_validate(public_payload)
