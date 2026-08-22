from __future__ import annotations

from collections.abc import Mapping

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from cogdoc.api.offload import run_sync
from cogdoc.api.connector_scope import (
    KBIncarnationChanged,
    capture_kb_epoch,
    guarded_kb_mutation,
)
from cogdoc.api.schemas import (
    Connection,
    ConnectionCreate,
    ConnectionEnabledUpdate,
    ConnectionList,
    ConnectorSyncHealth,
    ConnectorSyncHealthList,
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
from cogdoc.connectors.connection_store import (
    ConnectionLimitError,
    connector_provider_matches,
)
from cogdoc.connectors.factory import (
    validate_connector_endpoint_policy,
    validate_connector_local_access_policy,
    validate_url_connector_host_policy,
)


router = APIRouter(prefix="/v1/knowledge-bases/{kb_id}", tags=["connections"])


def _error(code: ErrorCode, message: str, status: int) -> JSONResponse:
    return JSONResponse(
        status_code=status, content=build_error_response(code, message).model_dump()
    )


def _scope(request: Request, kb_id: str, permission: Permission):
    if not request_principal(request).allows(permission):
        return None
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


def _public_connection(
    row: Mapping,
    external_kb_id: str,
    *,
    include_sensitive_config: bool = True,
) -> Connection:
    values = {key: row.get(key) for key in Connection.model_fields}
    values["kb_id"] = external_kb_id
    if not include_sensitive_config:
        # READ can observe operational identity/status, but local paths,
        # upstream resource identifiers and vault capability IDs remain in the
        # manage-access control plane.
        values["config"] = {}
        values["credential_id"] = None
    return Connection.model_validate(values)


def _public_job(row: Mapping, external_kb_id: str) -> ConnectorSyncJob:
    fields = ConnectorSyncJob.model_fields
    values = {key: row.get(key) for key in fields}
    values["kb_id"] = external_kb_id
    return ConnectorSyncJob.model_validate(values)


def _public_health(row: Mapping, external_kb_id: str) -> ConnectorSyncHealth:
    fields = ConnectorSyncHealth.model_fields
    values = {key: row.get(key) for key in fields}
    values["kb_id"] = external_kb_id
    return ConnectorSyncHealth.model_validate(values)


@router.get("/connections", response_model=ConnectionList)
async def list_connections(kb_id: str, request: Request):
    scope = _scope(request, kb_id, Permission.READ)
    if scope is None:
        return _error(ErrorCode.KB_NOT_FOUND, "知识库不存在", 404)
    can_manage = _scope(request, kb_id, Permission.MANAGE_ACCESS) is not None
    rows = await run_sync(
        request.app.state.offload_executor,
        request.app.state.connection_store.list_entries,
        scope.tenant_id,
        scope.storage_id,
    )
    return ConnectionList(
        connections=[
            _public_connection(
                row,
                scope.external_id,
                include_sensitive_config=can_manage,
            )
            for row in rows
        ]
    )


@router.get("/connection-health", response_model=ConnectorSyncHealthList)
async def list_connection_health(kb_id: str, request: Request):
    scope = _scope(request, kb_id, Permission.READ)
    if scope is None:
        return _error(ErrorCode.KB_NOT_FOUND, "知识库不存在", 404)
    connections = await run_sync(
        request.app.state.offload_executor,
        request.app.state.connection_store.list_entries,
        scope.tenant_id,
        scope.storage_id,
    )
    rows = []
    for connection in connections:
        try:
            rows.append(
                await run_sync(
                    request.app.state.offload_executor,
                    request.app.state.sync_manager.health,
                    str(connection["connection_id"]),
                )
            )
        except KeyError:
            # A concurrent DELETE may have retired the definition after this
            # list snapshot. Omit that no-longer-visible connection.
            continue
    return ConnectorSyncHealthList(
        connections=[_public_health(row, scope.external_id) for row in rows]
    )


@router.post("/connections", response_model=Connection, status_code=201)
async def create_connection(kb_id: str, body: ConnectionCreate, request: Request):
    scope = _scope(request, kb_id, Permission.MANAGE_ACCESS)
    if scope is None:
        return _error(ErrorCode.KB_NOT_FOUND, "知识库不存在", 404)
    kb_epoch = capture_kb_epoch(scope.storage_id)
    if getattr(request.app.state, "ha_connector_multiwriter_mode", False) and (
        body.connector_type in {"local-directory", "git"}
    ):
        return _error(
            ErrorCode.BAD_REQUEST,
            "HA 多写节点不接受依赖节点本地文件系统的连接器",
            400,
        )
    if body.secret_env and request.app.state.auth_enabled:
        return _error(
            ErrorCode.BAD_REQUEST,
            "启用身份认证时不允许 secret_env；请使用知识库凭据库",
            400,
        )
    try:
        validate_connector_endpoint_policy(
            body.connector_type,
            body.config,
            confluence_allowed_hosts=(
                request.app.state.connector_confluence_allowed_hosts
            ),
            s3_endpoint_allowed_hosts=(
                request.app.state.connector_s3_endpoint_allowed_hosts
            ),
        )
        validate_connector_local_access_policy(
            body.connector_type,
            body.config,
            enforce=request.app.state.auth_enabled,
            local_allowed_roots=request.app.state.connector_local_allowed_roots,
            git_allowed_roots=request.app.state.connector_git_allowed_roots,
        )
        validate_url_connector_host_policy(
            body.connector_type,
            body.config,
            enforce=request.app.state.auth_enabled,
            allowed_hosts=request.app.state.connector_url_allowed_hosts,
        )
    except ValueError as exc:
        return _error(ErrorCode.BAD_REQUEST, str(exc), 400)
    principal = request_principal(request)
    async with request.app.state.connector_credential_reference_lock:
        credential_fields: list[str] = []
        if body.credential_id is not None:
            vault = getattr(request.app.state, "connector_credential_vault", None)
            if vault is None:
                return _error(ErrorCode.CREDENTIAL_UNAVAILABLE, "连接凭据库未配置", 503)
            metadata = await run_sync(
                request.app.state.offload_executor,
                vault.get_metadata,
                body.credential_id,
                tenant_id=scope.tenant_id,
                kb_id=scope.storage_id,
            )
            if metadata is None:
                return _error(ErrorCode.CREDENTIAL_NOT_FOUND, "连接凭据不存在", 404)
            if metadata.get("connection_id") is not None:
                return _error(ErrorCode.BAD_REQUEST, "该凭据已绑定其他连接", 409)
            provider = str(metadata.get("provider") or "").casefold()
            if not connector_provider_matches(body.connector_type, provider):
                return _error(ErrorCode.BAD_REQUEST, "凭据提供方与连接类型不匹配", 400)
            credential_fields = [str(item) for item in metadata["secret_fields"]]
        try:
            row = await run_sync(
                request.app.state.offload_executor,
                guarded_kb_mutation,
                request.app.state.kb_registry,
                scope.tenant_id,
                scope.storage_id,
                kb_epoch,
                request.app.state.connection_store.create,
                tenant_id=scope.tenant_id,
                kb_id=scope.storage_id,
                connector_type=body.connector_type,
                name=body.name,
                config=body.config,
                secret_env=body.secret_env,
                credential_id=body.credential_id,
                credential_fields=credential_fields,
                owner_id=principal.subject_id,
                workspace_visible=body.workspace_visible,
            )
        except KBIncarnationChanged:
            return _error(ErrorCode.KB_NOT_FOUND, "知识库已发生变化", 409)
        except ConnectionLimitError as exc:
            return _error(ErrorCode.BAD_REQUEST, str(exc), 409)
        except (TypeError, ValueError) as exc:
            return _error(ErrorCode.BAD_REQUEST, str(exc), 400)
        return _public_connection(row, scope.external_id)


def _owned_connection(request: Request, scope, connection_id: str):
    row = request.app.state.connection_store.get(connection_id)
    if (
        row is None
        or row["tenant_id"] != scope.tenant_id
        or row["kb_id"] != scope.storage_id
    ):
        return None
    return row


@router.get("/connections/{connection_id}/health", response_model=ConnectorSyncHealth)
async def get_connection_health(kb_id: str, connection_id: str, request: Request):
    scope = _scope(request, kb_id, Permission.READ)
    if scope is None:
        return _error(ErrorCode.KB_NOT_FOUND, "知识库不存在", 404)
    if _owned_connection(request, scope, connection_id) is None:
        return _error(ErrorCode.DOCUMENT_NOT_FOUND, "连接不存在", 404)
    try:
        row = await run_sync(
            request.app.state.offload_executor,
            request.app.state.sync_manager.health,
            connection_id,
        )
    except KeyError:
        return _error(ErrorCode.DOCUMENT_NOT_FOUND, "连接不存在", 404)
    return _public_health(row, scope.external_id)


@router.patch("/connections/{connection_id}", response_model=Connection)
async def update_connection(
    kb_id: str, connection_id: str, body: ConnectionEnabledUpdate, request: Request
):
    scope = _scope(request, kb_id, Permission.MANAGE_ACCESS)
    if scope is None:
        return _error(ErrorCode.KB_NOT_FOUND, "知识库不存在", 404)
    kb_epoch = capture_kb_epoch(scope.storage_id)
    if _owned_connection(request, scope, connection_id) is None:
        return _error(ErrorCode.DOCUMENT_NOT_FOUND, "连接不存在", 404)
    async with request.app.state.connector_credential_reference_lock:
        try:
            row = await run_sync(
                request.app.state.offload_executor,
                guarded_kb_mutation,
                request.app.state.kb_registry,
                scope.tenant_id,
                scope.storage_id,
                kb_epoch,
                request.app.state.sync_manager.set_connection_enabled,
                connection_id,
                body.enabled,
            )
        except KBIncarnationChanged:
            return _error(ErrorCode.KB_NOT_FOUND, "知识库已发生变化", 409)
        except KeyError:
            return _error(ErrorCode.DOCUMENT_NOT_FOUND, "连接不存在", 404)
        except ValueError as exc:
            return _error(ErrorCode.BAD_REQUEST, str(exc), 409)
    return _public_connection(row, scope.external_id)


@router.delete("/connections/{connection_id}", status_code=204)
async def delete_connection(kb_id: str, connection_id: str, request: Request):
    scope = _scope(request, kb_id, Permission.MANAGE_ACCESS)
    if scope is None:
        return _error(ErrorCode.KB_NOT_FOUND, "知识库不存在", 404)
    kb_epoch = capture_kb_epoch(scope.storage_id)
    # Reuse the queued document-mutation guard so a long connector drain/index
    # cannot outlive the exact membership incarnation and live ACL that
    # authorized deletion. Runtime import avoids coupling router load order.
    from cogdoc.api.routes.documents import _live_session_authorization_guard

    authorization_guard = _live_session_authorization_guard(
        request,
        scope,
        permission=Permission.MANAGE_ACCESS,
    )
    async with request.app.state.connector_credential_reference_lock:
        if _owned_connection(request, scope, connection_id) is None:
            return _error(ErrorCode.DOCUMENT_NOT_FOUND, "连接不存在", 404)
        try:
            if authorization_guard is not None:
                await run_sync(
                    request.app.state.offload_executor,
                    authorization_guard,
                )
            fenced = await run_sync(
                request.app.state.offload_executor,
                request.app.state.sync_manager.fence_connection_delete,
                scope.tenant_id,
                scope.storage_id,
                connection_id,
            )
        except ValueError as exc:
            return _error(ErrorCode.BAD_REQUEST, str(exc), 409)
        except KeyError:
            return _error(ErrorCode.DOCUMENT_NOT_FOUND, "连接不存在", 404)
        except PermissionError:
            return _error(ErrorCode.KB_NOT_FOUND, "知识库不存在", 404)
    try:
        await run_sync(
            request.app.state.connector_cleanup_executor,
            request.app.state.sync_manager.drain_connection_delete,
            scope.tenant_id,
            scope.storage_id,
            connection_id,
            cancelled=int(fenced["cancelled"]),
        )
    except ValueError as exc:
        return _error(ErrorCode.BAD_REQUEST, str(exc), 409)
    except KeyError:
        return _error(ErrorCode.DOCUMENT_NOT_FOUND, "连接不存在", 404)
    except TimeoutError:
        return _error(
            ErrorCode.KB_CLEANUP_FAILED,
            "来源同步尚未安全停止，请重试删除连接",
            500,
        )
    try:
        await run_sync(
            request.app.state.connector_cleanup_executor,
            request.app.state.connector_connection_cleanup,
            scope.tenant_id,
            scope.storage_id,
            connection_id,
            kb_epoch,
            authorization_guard,
        )
    except KBIncarnationChanged:
        return _error(ErrorCode.KB_NOT_FOUND, "知识库已发生变化", 409)
    except KeyError:
        return _error(ErrorCode.DOCUMENT_NOT_FOUND, "连接不存在", 404)
    except PermissionError:
        return _error(ErrorCode.KB_NOT_FOUND, "知识库不存在", 404)
    except Exception:
        return _error(
            ErrorCode.KB_CLEANUP_FAILED,
            "连接来源清理未完成，请重试删除",
            500,
        )
    async with request.app.state.connector_credential_reference_lock:
        try:
            await run_sync(
                request.app.state.offload_executor,
                request.app.state.connector_connection_delete_finalizer,
                scope.tenant_id,
                scope.storage_id,
                connection_id,
                kb_epoch,
                authorization_guard,
            )
        except KBIncarnationChanged:
            return _error(ErrorCode.KB_NOT_FOUND, "知识库已发生变化", 409)
        except KeyError:
            return _error(ErrorCode.DOCUMENT_NOT_FOUND, "连接不存在", 404)
        except PermissionError:
            return _error(ErrorCode.KB_NOT_FOUND, "知识库不存在", 404)
        except Exception:
            return _error(
                ErrorCode.KB_CLEANUP_FAILED,
                "连接定义清理未完成，请重试删除",
                500,
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
    kb_epoch = capture_kb_epoch(scope.storage_id)
    if _owned_connection(request, scope, connection_id) is None:
        return _error(ErrorCode.DOCUMENT_NOT_FOUND, "连接不存在", 404)
    try:
        row = await run_sync(
            request.app.state.offload_executor,
            guarded_kb_mutation,
            request.app.state.kb_registry,
            scope.tenant_id,
            scope.storage_id,
            kb_epoch,
            request.app.state.sync_manager.submit,
            connection_id,
        )
    except KBIncarnationChanged:
        return _error(ErrorCode.KB_NOT_FOUND, "知识库已发生变化", 409)
    except KeyError:
        return _error(ErrorCode.DOCUMENT_NOT_FOUND, "连接不存在", 404)
    except (TypeError, ValueError) as exc:
        return _error(ErrorCode.BAD_REQUEST, str(exc), 409)
    return _public_job(row, scope.external_id)


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
    return ConnectorSyncJobList(
        jobs=[_public_job(row, scope.external_id) for row in rows]
    )


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
    return _public_job(row, scope.external_id)


@router.post("/sync-jobs/{job_id}/cancel", response_model=ConnectorSyncJob)
async def cancel_sync_job(kb_id: str, job_id: str, request: Request):
    scope = _scope(request, kb_id, Permission.MANAGE_ACCESS)
    if scope is None:
        return _error(ErrorCode.KB_NOT_FOUND, "知识库不存在", 404)
    kb_epoch = capture_kb_epoch(scope.storage_id)
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
            guarded_kb_mutation,
            request.app.state.kb_registry,
            scope.tenant_id,
            scope.storage_id,
            kb_epoch,
            request.app.state.sync_manager.cancel,
            job_id,
        )
    except KBIncarnationChanged:
        return _error(ErrorCode.KB_NOT_FOUND, "知识库已发生变化", 409)
    except ValueError as exc:
        return _error(ErrorCode.BAD_REQUEST, str(exc), 409)
    return _public_job(cancelled, scope.external_id)


@router.post(
    "/sync-jobs/{job_id}/replay",
    response_model=ConnectorSyncJob,
    status_code=202,
)
async def replay_sync_job(kb_id: str, job_id: str, request: Request):
    scope = _scope(request, kb_id, Permission.MANAGE_ACCESS)
    if scope is None:
        return _error(ErrorCode.KB_NOT_FOUND, "知识库不存在", 404)
    kb_epoch = capture_kb_epoch(scope.storage_id)
    row = request.app.state.connector_sync_store.get(job_id)
    if (
        row is None
        or row["tenant_id"] != scope.tenant_id
        or row["kb_id"] != scope.storage_id
    ):
        return _error(ErrorCode.JOB_NOT_FOUND, "同步任务不存在", 404)
    try:
        replayed = await run_sync(
            request.app.state.offload_executor,
            guarded_kb_mutation,
            request.app.state.kb_registry,
            scope.tenant_id,
            scope.storage_id,
            kb_epoch,
            request.app.state.sync_manager.replay,
            job_id,
        )
    except KBIncarnationChanged:
        return _error(ErrorCode.KB_NOT_FOUND, "知识库已发生变化", 409)
    except ValueError as exc:
        return _error(ErrorCode.SYNC_REPLAY_CONFLICT, str(exc), 409)
    return _public_job(replayed, scope.external_id)
