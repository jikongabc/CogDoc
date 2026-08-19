from __future__ import annotations

from collections.abc import Mapping

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from cogdoc.api.offload import run_sync
from cogdoc.api.schemas import (
    Connection,
    ConnectionCreate,
    ConnectionEnabledUpdate,
    ConnectionList,
    ConnectorSyncJob,
    ConnectorSyncJobList,
    ErrorCode,
    build_error_response,
)
from cogdoc.api.tenant_scope import (
    request_principal,
    resource_access_decision,
    resolve_kb_scope,
)
from cogdoc.api.tenancy import Permission


router = APIRouter(prefix="/v1/knowledge-bases/{kb_id}", tags=["connections"])


def _error(code: ErrorCode, message: str, status: int) -> JSONResponse:
    return JSONResponse(
        status_code=status, content=build_error_response(code, message).model_dump()
    )


def _scope(request: Request, kb_id: str, permission: Permission):
    scope = resolve_kb_scope(request, kb_id)
    if scope is None:
        return None
    decision = resource_access_decision(request, scope, permission=permission)
    return (
        scope
        if decision is None
        or (decision is not False and getattr(decision, "is_allowed", False))
        else None
    )


def _public_connection(row: Mapping) -> Connection:
    return Connection.model_validate(
        {key: row.get(key) for key in Connection.model_fields}
    )


def _public_job(row: Mapping) -> ConnectorSyncJob:
    fields = ConnectorSyncJob.model_fields
    return ConnectorSyncJob.model_validate({key: row.get(key) for key in fields})


@router.get("/connections", response_model=ConnectionList)
async def list_connections(kb_id: str, request: Request):
    scope = _scope(request, kb_id, Permission.READ)
    if scope is None:
        return _error(ErrorCode.KB_NOT_FOUND, "知识库不存在", 404)
    rows = await run_sync(
        request.app.state.offload_executor,
        request.app.state.connection_store.list_entries,
        scope.tenant_id,
        scope.storage_id,
    )
    return ConnectionList(connections=[_public_connection(row) for row in rows])


@router.post("/connections", response_model=Connection, status_code=201)
async def create_connection(kb_id: str, body: ConnectionCreate, request: Request):
    scope = _scope(request, kb_id, Permission.MANAGE_ACCESS)
    if scope is None:
        return _error(ErrorCode.KB_NOT_FOUND, "知识库不存在", 404)
    principal = request_principal(request)
    try:
        row = await run_sync(
            request.app.state.offload_executor,
            request.app.state.connection_store.create,
            tenant_id=scope.tenant_id,
            kb_id=scope.storage_id,
            connector_type=body.connector_type,
            name=body.name,
            config=body.config,
            secret_env=body.secret_env,
            owner_id=principal.subject_id,
            workspace_visible=body.workspace_visible,
        )
    except (TypeError, ValueError) as exc:
        return _error(ErrorCode.BAD_REQUEST, str(exc), 400)
    return _public_connection(row)


def _owned_connection(request: Request, scope, connection_id: str):
    row = request.app.state.connection_store.get(connection_id)
    if (
        row is None
        or row["tenant_id"] != scope.tenant_id
        or row["kb_id"] != scope.storage_id
    ):
        return None
    return row


@router.patch("/connections/{connection_id}", response_model=Connection)
async def update_connection(
    kb_id: str, connection_id: str, body: ConnectionEnabledUpdate, request: Request
):
    scope = _scope(request, kb_id, Permission.MANAGE_ACCESS)
    if scope is None:
        return _error(ErrorCode.KB_NOT_FOUND, "知识库不存在", 404)
    if _owned_connection(request, scope, connection_id) is None:
        return _error(ErrorCode.DOCUMENT_NOT_FOUND, "连接不存在", 404)
    row = await run_sync(
        request.app.state.offload_executor,
        request.app.state.connection_store.set_enabled,
        connection_id,
        body.enabled,
    )
    return _public_connection(row)


@router.delete("/connections/{connection_id}", status_code=204)
async def delete_connection(kb_id: str, connection_id: str, request: Request):
    scope = _scope(request, kb_id, Permission.MANAGE_ACCESS)
    if scope is None:
        return _error(ErrorCode.KB_NOT_FOUND, "知识库不存在", 404)
    if _owned_connection(request, scope, connection_id) is None:
        return _error(ErrorCode.DOCUMENT_NOT_FOUND, "连接不存在", 404)
    await run_sync(
        request.app.state.offload_executor,
        request.app.state.connection_store.delete,
        connection_id,
    )
    return None


@router.post(
    "/connections/{connection_id}/sync",
    response_model=ConnectorSyncJob,
    status_code=202,
)
async def start_sync(kb_id: str, connection_id: str, request: Request):
    scope = _scope(request, kb_id, Permission.MANAGE_ACCESS)
    if scope is None:
        return _error(ErrorCode.KB_NOT_FOUND, "知识库不存在", 404)
    if _owned_connection(request, scope, connection_id) is None:
        return _error(ErrorCode.DOCUMENT_NOT_FOUND, "连接不存在", 404)
    try:
        row = await run_sync(
            request.app.state.offload_executor,
            request.app.state.sync_manager.submit,
            connection_id,
        )
    except (TypeError, ValueError) as exc:
        return _error(ErrorCode.BAD_REQUEST, str(exc), 409)
    return _public_job(row)


@router.get("/sync-jobs", response_model=ConnectorSyncJobList)
async def list_sync_jobs(kb_id: str, request: Request):
    scope = _scope(request, kb_id, Permission.READ)
    if scope is None:
        return _error(ErrorCode.KB_NOT_FOUND, "知识库不存在", 404)
    rows = await run_sync(
        request.app.state.offload_executor,
        request.app.state.connector_sync_store.list_jobs,
        scope.tenant_id,
        scope.storage_id,
    )
    return ConnectorSyncJobList(jobs=[_public_job(row) for row in rows])


@router.get("/sync-jobs/{job_id}", response_model=ConnectorSyncJob)
async def get_sync_job(kb_id: str, job_id: str, request: Request):
    scope = _scope(request, kb_id, Permission.READ)
    if scope is None:
        return _error(ErrorCode.KB_NOT_FOUND, "知识库不存在", 404)
    row = await run_sync(
        request.app.state.offload_executor,
        request.app.state.connector_sync_store.get,
        job_id,
    )
    if (
        row is None
        or row["tenant_id"] != scope.tenant_id
        or row["kb_id"] != scope.storage_id
    ):
        return _error(ErrorCode.JOB_NOT_FOUND, "同步任务不存在", 404)
    return _public_job(row)


@router.post("/sync-jobs/{job_id}/cancel", response_model=ConnectorSyncJob)
async def cancel_sync_job(kb_id: str, job_id: str, request: Request):
    scope = _scope(request, kb_id, Permission.MANAGE_ACCESS)
    if scope is None:
        return _error(ErrorCode.KB_NOT_FOUND, "知识库不存在", 404)
    row = request.app.state.connector_sync_store.get(job_id)
    if (
        row is None
        or row["tenant_id"] != scope.tenant_id
        or row["kb_id"] != scope.storage_id
    ):
        return _error(ErrorCode.JOB_NOT_FOUND, "同步任务不存在", 404)
    try:
        cancelled = await run_sync(
            request.app.state.offload_executor,
            request.app.state.sync_manager.cancel,
            job_id,
        )
    except ValueError as exc:
        return _error(ErrorCode.BAD_REQUEST, str(exc), 409)
    return _public_job(cancelled)
