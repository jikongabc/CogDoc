from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from concurrent.futures import Executor
from typing import BinaryIO
from urllib.parse import quote

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from cogdoc.api.offload import run_sync
from cogdoc.api.connector_scope import (
    KBIncarnationChanged,
    assert_active_kb_incarnation,
    capture_kb_epoch,
    guarded_kb_mutation,
)
from cogdoc.api.schemas import (
    ErrorCode,
    SourceArtifactDelete,
    SourceArtifactPurge,
    SourceArtifactRestore,
    SourceArtifactUsage,
    SourceArtifactVersionSummary,
    SourceCatalogEntry,
    SourceCatalogList,
    SourceVersion,
    SourceVersionDiff,
    SourceVersionList,
    build_error_response,
)
from cogdoc.api.tenant_scope import (
    request_principal,
    resource_access_decision,
    resolve_kb_scope,
)
from cogdoc.api.tenancy import Permission
from cogdoc.service.source_artifact_store import (
    ArtifactConflictError,
    ArtifactIntegrityError,
    ArtifactLimitError,
    ArtifactNotFoundError,
)
from cogdoc.service.kb_locks import kb_write_lock
from cogdoc.tools.chunk_identity import build_document_id


router = APIRouter(prefix="/v1/knowledge-bases/{kb_id}", tags=["source-operations"])


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


def _source_name(row: Mapping) -> str:
    metadata = row.get("metadata")
    if isinstance(metadata, Mapping):
        materialized = str(metadata.get("materialized_name") or "")
        if materialized:
            return materialized
    return str(row.get("display_name") or "")


def _public_source(request: Request, scope, row: Mapping) -> SourceCatalogEntry:
    values = {
        key: row.get(key) for key in SourceCatalogEntry.model_fields if key in row
    }
    source_name = _source_name(row)
    if source_name:
        document_id = build_document_id(source_name)
        values["document_id"] = document_id
        access_store = getattr(request.app.state, "resource_access_store", None)
        if access_store is not None:
            policy = access_store.get_document_policy(
                scope.tenant_id, scope.storage_id, document_id
            )
            if isinstance(policy, Mapping):
                values["access_configured"] = True
                values["access_policy"] = policy.get("policy")
                values["acl_epoch"] = policy.get("acl_epoch")
    return SourceCatalogEntry.model_validate(values)


async def _catalog_source(request: Request, scope, source_id: str):
    return await run_sync(
        request.app.state.offload_executor,
        request.app.state.source_catalog.get,
        scope.tenant_id,
        scope.storage_id,
        source_id,
        include_deleted=True,
    )


def _assert_catalog_artifact_match(
    catalog_version: Mapping, artifact_metadata: Mapping
) -> None:
    """Reject content that disagrees with the durable source catalog."""

    fields = ("source_id", "version_id", "content_sha256")
    if any(
        str(catalog_version.get(field) or "").casefold()
        != str(artifact_metadata.get(field) or "").casefold()
        for field in fields
    ):
        raise ArtifactIntegrityError(
            "source artifact metadata does not match its catalog version"
        )


def _public_artifact_version(metadata: Mapping) -> SourceArtifactVersionSummary:
    """Project immutable artifact metadata without physical tenant/KB keys."""

    return SourceArtifactVersionSummary.model_validate(
        {
            field: metadata.get(field)
            for field in SourceArtifactVersionSummary.model_fields
        }
    )


async def _stream_verified_handle(
    handle: BinaryIO, executor: Executor
) -> AsyncIterator[bytes]:
    try:
        while chunk := await run_sync(executor, handle.read, 64 * 1024):
            yield chunk
    finally:
        handle.close()


@router.get("/source-catalog", response_model=SourceCatalogList)
async def list_catalog_sources(
    kb_id: str,
    request: Request,
    connection_id: str | None = None,
    health_status: str | None = None,
    include_deleted: bool = False,
):
    # The operations catalog contains provider IDs, local origins and failure
    # diagnostics; the ordinary reader-facing /sources endpoint remains the
    # least-privilege way to browse permitted filenames.
    scope = _scope(request, kb_id, Permission.MANAGE_ACCESS)
    if scope is None:
        return _error(ErrorCode.KB_NOT_FOUND, "知识库不存在", 404)
    try:
        rows = await run_sync(
            request.app.state.offload_executor,
            request.app.state.source_catalog.list_sources,
            scope.tenant_id,
            scope.storage_id,
            include_deleted=include_deleted,
            connection_id=connection_id,
            health_status=health_status,
        )
    except ValueError as exc:
        return _error(ErrorCode.BAD_REQUEST, str(exc), 400)
    return SourceCatalogList(
        sources=[_public_source(request, scope, row) for row in rows]
    )


@router.get("/source-catalog/{source_id}", response_model=SourceCatalogEntry)
async def get_catalog_source(kb_id: str, source_id: str, request: Request):
    scope = _scope(request, kb_id, Permission.MANAGE_ACCESS)
    if scope is None:
        return _error(ErrorCode.KB_NOT_FOUND, "知识库不存在", 404)
    row = await _catalog_source(request, scope, source_id)
    if row is None:
        return _error(ErrorCode.SOURCE_NOT_FOUND, "来源不存在", 404)
    return _public_source(request, scope, row)


@router.get("/source-catalog/{source_id}/versions", response_model=SourceVersionList)
async def list_source_versions(kb_id: str, source_id: str, request: Request):
    scope = _scope(request, kb_id, Permission.MANAGE_ACCESS)
    if scope is None:
        return _error(ErrorCode.KB_NOT_FOUND, "知识库不存在", 404)
    source = await _catalog_source(request, scope, source_id)
    if source is None:
        return _error(ErrorCode.SOURCE_NOT_FOUND, "来源不存在", 404)
    rows = await run_sync(
        request.app.state.offload_executor,
        request.app.state.source_catalog.list_versions,
        scope.tenant_id,
        scope.storage_id,
        source_id,
        include_deleted=True,
    )
    artifact_versions = await run_sync(
        request.app.state.source_artifact_executor,
        request.app.state.source_artifact_store.list_versions,
        scope.tenant_id,
        scope.storage_id,
        source_id,
    )
    available = {str(row["version_id"]) for row in artifact_versions}
    projected = []
    for row in rows:
        values = {key: row.get(key) for key in SourceVersion.model_fields}
        values["artifact_available"] = str(row["version_id"]) in available
        projected.append(SourceVersion.model_validate(values))
    return SourceVersionList(versions=projected)


@router.get("/source-catalog/{source_id}/versions/{version_id}/content")
async def download_source_version(
    kb_id: str, source_id: str, version_id: str, request: Request
):
    scope = _scope(request, kb_id, Permission.MANAGE_ACCESS)
    if scope is None:
        return _error(ErrorCode.KB_NOT_FOUND, "知识库不存在", 404)
    version = await run_sync(
        request.app.state.offload_executor,
        request.app.state.source_catalog.get_version,
        scope.tenant_id,
        scope.storage_id,
        source_id,
        version_id,
        include_deleted=True,
    )
    if version is None:
        return _error(ErrorCode.SOURCE_VERSION_NOT_FOUND, "来源版本不存在", 404)
    try:
        metadata, handle = await run_sync(
            request.app.state.source_artifact_executor,
            request.app.state.source_artifact_store.open_verified,
            scope.tenant_id,
            scope.storage_id,
            source_id,
            version_id,
        )
        try:
            _assert_catalog_artifact_match(version, metadata)
        except BaseException:
            handle.close()
            raise
    except ArtifactNotFoundError:
        return _error(ErrorCode.SOURCE_VERSION_NOT_FOUND, "来源原始版本不可用", 404)
    except ArtifactIntegrityError:
        return _error(ErrorCode.INTERNAL_ERROR, "来源原始版本完整性校验失败", 503)
    filename = str(metadata.get("display_name") or f"{source_id}-{version_id}")
    return StreamingResponse(
        _stream_verified_handle(handle, request.app.state.source_artifact_executor),
        media_type=str(metadata.get("media_type") or "application/octet-stream"),
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}",
            "Content-Length": str(metadata["byte_size"]),
            "X-Content-Type-Options": "nosniff",
            "X-CogDoc-Content-SHA256": str(metadata["content_sha256"]),
        },
    )


@router.get("/source-catalog/{source_id}/diff", response_model=SourceVersionDiff)
async def diff_source_versions(
    kb_id: str,
    source_id: str,
    request: Request,
    from_version_id: str = Query(min_length=1, max_length=200),
    to_version_id: str = Query(min_length=1, max_length=200),
):
    scope = _scope(request, kb_id, Permission.MANAGE_ACCESS)
    if scope is None:
        return _error(ErrorCode.KB_NOT_FOUND, "知识库不存在", 404)
    source = await _catalog_source(request, scope, source_id)
    if source is None:
        return _error(ErrorCode.SOURCE_NOT_FOUND, "来源不存在", 404)
    from_version = await run_sync(
        request.app.state.offload_executor,
        request.app.state.source_catalog.get_version,
        scope.tenant_id,
        scope.storage_id,
        source_id,
        from_version_id,
        include_deleted=True,
    )
    to_version = await run_sync(
        request.app.state.offload_executor,
        request.app.state.source_catalog.get_version,
        scope.tenant_id,
        scope.storage_id,
        source_id,
        to_version_id,
        include_deleted=True,
    )
    if from_version is None or to_version is None:
        return _error(ErrorCode.SOURCE_VERSION_NOT_FOUND, "来源版本不存在", 404)
    try:
        result = await run_sync(
            request.app.state.source_artifact_executor,
            request.app.state.source_artifact_store.diff,
            scope.tenant_id,
            scope.storage_id,
            source_id,
            from_version_id,
            to_version_id,
        )
        _assert_catalog_artifact_match(from_version, result["from"])
        _assert_catalog_artifact_match(to_version, result["to"])
    except ArtifactNotFoundError:
        return _error(ErrorCode.SOURCE_VERSION_NOT_FOUND, "来源原始版本不可用", 404)
    except ArtifactIntegrityError:
        return _error(ErrorCode.INTERNAL_ERROR, "来源原始版本完整性校验失败", 503)
    rendered = result.get("diff")
    lines = str(rendered or "").splitlines()
    added = sum(line.startswith("+") and not line.startswith("+++") for line in lines)
    removed = sum(line.startswith("-") and not line.startswith("---") for line in lines)
    return SourceVersionDiff(
        source_id=source_id,
        from_version_id=from_version_id,
        to_version_id=to_version_id,
        kind=result["kind"],
        truncated=bool(result["truncated"]),
        added_lines=added,
        removed_lines=removed,
        diff=rendered,
        from_version=_public_artifact_version(result["from"]),
        to_version=_public_artifact_version(result["to"]),
    )


@router.delete(
    "/source-catalog/{source_id}/versions/{version_id}/artifact",
    response_model=SourceArtifactDelete,
)
async def delete_source_artifact(
    kb_id: str, source_id: str, version_id: str, request: Request
):
    scope = _scope(request, kb_id, Permission.MANAGE_ACCESS)
    if scope is None:
        return _error(ErrorCode.KB_NOT_FOUND, "知识库不存在", 404)
    kb_epoch = capture_kb_epoch(scope.storage_id)

    def delete_historical_version():
        # Synchronize the current-version decision with materialized sink
        # swaps/catalog updates. The project is single-process by contract, so
        # this is the same authority boundary used by connector commits.
        with kb_write_lock(scope.storage_id):
            assert_active_kb_incarnation(
                request.app.state.kb_registry,
                scope.tenant_id,
                scope.storage_id,
                kb_epoch,
            )
            source = request.app.state.source_catalog.get(
                scope.tenant_id,
                scope.storage_id,
                source_id,
                include_deleted=True,
            )
            if source is None:
                raise KeyError(source_id)
            if source.get("version_id") == version_id:
                raise ArtifactConflictError("current source version cannot be deleted")
            version = request.app.state.source_catalog.get_version(
                scope.tenant_id,
                scope.storage_id,
                source_id,
                version_id,
                include_deleted=True,
            )
            if version is None:
                raise ArtifactNotFoundError("source version was not found")
            return request.app.state.source_artifact_store.delete_version(
                scope.tenant_id,
                scope.storage_id,
                source_id,
                version_id,
            )

    try:
        result = await run_sync(
            request.app.state.source_artifact_executor,
            delete_historical_version,
        )
    except KBIncarnationChanged:
        return _error(ErrorCode.KB_NOT_FOUND, "知识库已发生变化", 409)
    except KeyError:
        return _error(ErrorCode.SOURCE_NOT_FOUND, "来源不存在", 404)
    except ArtifactConflictError:
        return _error(ErrorCode.BAD_REQUEST, "当前在线版本不能删除", 409)
    except ArtifactNotFoundError:
        return _error(ErrorCode.SOURCE_VERSION_NOT_FOUND, "来源原始版本不可用", 404)
    return SourceArtifactDelete(
        source_id=source_id,
        version_id=version_id,
        recovery_token=result["recovery_token"],
        deleted=True,
    )


@router.post(
    "/source-artifacts/{recovery_token}/restore",
    response_model=SourceArtifactRestore,
)
async def restore_source_artifact(kb_id: str, recovery_token: str, request: Request):
    scope = _scope(request, kb_id, Permission.MANAGE_ACCESS)
    if scope is None:
        return _error(ErrorCode.KB_NOT_FOUND, "知识库不存在", 404)
    kb_epoch = capture_kb_epoch(scope.storage_id)
    try:
        row = await run_sync(
            request.app.state.source_artifact_executor,
            guarded_kb_mutation,
            request.app.state.kb_registry,
            scope.tenant_id,
            scope.storage_id,
            kb_epoch,
            request.app.state.source_artifact_store.restore,
            scope.tenant_id,
            scope.storage_id,
            recovery_token,
        )
    except KBIncarnationChanged:
        return _error(ErrorCode.KB_NOT_FOUND, "知识库已发生变化", 409)
    except ArtifactNotFoundError:
        return _error(ErrorCode.SOURCE_VERSION_NOT_FOUND, "恢复令牌不存在", 404)
    except ArtifactConflictError:
        return _error(ErrorCode.BAD_REQUEST, "恢复版本与现有版本冲突", 409)
    except (ArtifactIntegrityError, ArtifactLimitError):
        return _error(ErrorCode.INTERNAL_ERROR, "来源原始版本无法恢复", 503)
    return SourceArtifactRestore(
        source_id=str(row["source_id"]), version_id=str(row["version_id"])
    )


@router.get("/source-artifacts/usage", response_model=SourceArtifactUsage)
async def source_artifact_usage(kb_id: str, request: Request):
    scope = _scope(request, kb_id, Permission.MANAGE_ACCESS)
    if scope is None:
        return _error(ErrorCode.KB_NOT_FOUND, "知识库不存在", 404)
    row = await run_sync(
        request.app.state.source_artifact_executor,
        request.app.state.source_artifact_store.usage,
        scope.tenant_id,
        scope.storage_id,
    )
    return SourceArtifactUsage.model_validate(row)


@router.delete("/source-artifacts/trash", response_model=SourceArtifactPurge)
async def purge_source_artifact_trash(
    kb_id: str,
    request: Request,
    older_than: float = Query(ge=0),
    limit: int = Query(default=100, ge=1, le=1000),
):
    scope = _scope(request, kb_id, Permission.MANAGE_ACCESS)
    if scope is None:
        return _error(ErrorCode.KB_NOT_FOUND, "知识库不存在", 404)
    kb_epoch = capture_kb_epoch(scope.storage_id)
    try:
        purged = await run_sync(
            request.app.state.source_artifact_executor,
            guarded_kb_mutation,
            request.app.state.kb_registry,
            scope.tenant_id,
            scope.storage_id,
            kb_epoch,
            request.app.state.source_artifact_store.purge_trash,
            scope.tenant_id,
            scope.storage_id,
            older_than=older_than,
            limit=limit,
        )
    except KBIncarnationChanged:
        return _error(ErrorCode.KB_NOT_FOUND, "知识库已发生变化", 409)
    except ValueError as exc:
        return _error(ErrorCode.BAD_REQUEST, str(exc), 400)
    return SourceArtifactPurge(purged=purged)
