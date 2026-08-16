from __future__ import annotations

from collections.abc import Mapping

from fastapi import APIRouter, Depends, HTTPException, Request

from cogdoc.api.eval_review_auth import require_eval_reviewer
from cogdoc.api.offload import run_sync
from cogdoc.api.schemas import IndexMigrationRequest, IndexMigrationRollbackRequest
from cogdoc.api.tenant_scope import (
    externalize_kb_fields,
    resolve_kb_scope,
    tenant_kb_scopes,
)


router = APIRouter(prefix="/v1/index-migrations", tags=["index-migrations"])


def _records(request: Request, kb_ids: list[str]) -> list[dict]:
    registry = request.app.state.kb_registry
    if kb_ids:
        records = []
        for kb_id in kb_ids:
            scope = resolve_kb_scope(request, kb_id)
            if scope is None:
                raise HTTPException(status_code=404, detail=f"知识库不存在: {kb_id}")
            record = registry.get_by_storage_id(scope.storage_id)
            if record is None:
                raise HTTPException(status_code=404, detail=f"知识库不存在: {kb_id}")
            records.append(record)
        return records
    records = []
    for scope in tenant_kb_scopes(request):
        record = registry.get_by_storage_id(scope.storage_id)
        if record is not None:
            records.append(record)
    return records


def _owned_run(request: Request, run_id: str) -> dict:
    try:
        run = request.app.state.index_migration_manager.get(run_id)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="索引迁移记录不存在") from exc
    allowed = {scope.storage_id for scope in tenant_kb_scopes(request)}
    stored = {
        str(value)
        for value in run.get("authorized_storage_ids", [])
        if str(value)
    }
    if not stored or not stored.issubset(allowed):
        raise HTTPException(status_code=404, detail="索引迁移记录不存在")
    return run


def _public(run: Mapping, request: Request) -> dict:
    def strip_internal(value):
        if isinstance(value, Mapping):
            return {
                key: strip_internal(item)
                for key, item in value.items()
                if key not in {"authorized_storage_ids", "storage_id"}
            }
        if isinstance(value, list):
            return [strip_internal(item) for item in value]
        return value

    payload = strip_internal(run)
    return externalize_kb_fields(payload, request)


@router.get("/scan")
async def scan_index_migrations(
    request: Request,
    _reviewer: str = Depends(require_eval_reviewer),
):
    result = await run_sync(
        request.app.state.offload_executor,
        request.app.state.index_migration_manager.runner.plan,
        _records(request, []),
    )
    return _public(result, request)


@router.post("", status_code=202)
async def start_index_migration(
    body: IndexMigrationRequest,
    request: Request,
    _reviewer: str = Depends(require_eval_reviewer),
):
    try:
        run = request.app.state.index_migration_manager.submit(
            _records(request, body.kb_ids), include_current=body.include_current
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _public(run, request)


@router.get("/{run_id}")
async def get_index_migration(
    run_id: str,
    request: Request,
    _reviewer: str = Depends(require_eval_reviewer),
):
    return _public(_owned_run(request, run_id), request)


@router.post("/{run_id}/rollback")
async def rollback_index_migration(
    run_id: str,
    body: IndexMigrationRollbackRequest,
    request: Request,
    _reviewer: str = Depends(require_eval_reviewer),
):
    run = _owned_run(request, run_id)
    allowed_storage_ids = set(run.get("authorized_storage_ids", []))
    requested = []
    for kb_id in body.kb_ids:
        scope = resolve_kb_scope(request, kb_id)
        if scope is not None:
            requested.append(scope.storage_id)
    if body.kb_ids and len(requested) != len(body.kb_ids):
        raise HTTPException(status_code=404, detail="知识库不存在")
    if not set(requested).issubset(allowed_storage_ids):
        raise HTTPException(status_code=404, detail="索引迁移记录不存在")
    try:
        result = await run_sync(
            request.app.state.offload_executor,
            request.app.state.index_migration_manager.rollback,
            run_id,
            requested,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _public(result, request)


@router.post("/{run_id}/finalize")
async def finalize_index_migration(
    run_id: str,
    request: Request,
    _reviewer: str = Depends(require_eval_reviewer),
):
    _owned_run(request, run_id)
    try:
        result = await run_sync(
            request.app.state.offload_executor,
            request.app.state.index_migration_manager.finalize,
            run_id,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _public(result, request)
