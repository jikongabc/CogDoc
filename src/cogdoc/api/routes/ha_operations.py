from __future__ import annotations

from typing import Any, Literal, Mapping

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from cogdoc.api.offload import run_sync
from cogdoc.api.tenancy import Permission
from cogdoc.api.tenant_scope import request_principal
from cogdoc.ha.tasks import JobConflict


router = APIRouter(prefix="/v1/ha", tags=["ha"])

JobStatus = Literal[
    "queued",
    "running",
    "retry_wait",
    "succeeded",
    "failed",
    "dead_letter",
    "cancelled",
]
GenerationStatus = Literal["building", "prepared", "published", "aborted"]


class DeadLetterReplay(BaseModel):
    replay_key: str = Field(min_length=1, max_length=200)


class ScheduleUpdate(BaseModel):
    enabled: bool
    expected_revision: int = Field(ge=1)


def _runtime(request: Request) -> Any:
    principal = request_principal(request)
    if not principal.allows(Permission.MANAGE_ACCESS):
        raise HTTPException(status_code=403, detail="需要知识库访问管理权限")
    runtime = getattr(request.app.state, "ha_runtime", None)
    if runtime is None:
        raise HTTPException(status_code=503, detail="HA 控制面未启用")
    return runtime


def _cursor(
    created_at: float | None,
    identifier: str | None,
    *,
    identifier_name: str,
) -> tuple[float, str] | None:
    if created_at is None and identifier is None:
        return None
    if created_at is None or identifier is None:
        raise HTTPException(
            status_code=422,
            detail=f"before_created_at 与 {identifier_name} 必须同时提供",
        )
    return created_at, identifier


def _public_job(row: Mapping[str, Any]) -> dict[str, Any]:
    # lease_token is an active worker capability. Payload/result may carry
    # physical KB identities or provider metadata, so neither belongs in the
    # tenant administration surface.
    return {
        key: row.get(key)
        for key in (
            "job_id",
            "queue_name",
            "status",
            "priority",
            "available_at",
            "cancel_requested",
            "attempt",
            "max_attempts",
            "error_code",
            "replay_of",
            "created_at",
            "updated_at",
            "finished_at",
            "revision",
        )
    }


def _public_schedule(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: row.get(key)
        for key in (
            "schedule_id",
            "queue_name",
            "schedule_type",
            "schedule_spec",
            "timezone_name",
            "enabled",
            "next_run_at",
            "last_run_at",
            "fire_sequence",
            "created_at",
            "updated_at",
            "revision",
        )
    }


def _external_kb_id(request: Request, physical_id: str, tenant_id: str) -> str | None:
    registry = getattr(request.app.state, "kb_registry", None)
    record = (
        registry.get_by_storage_id(physical_id)
        if registry is not None and hasattr(registry, "get_by_storage_id")
        else None
    )
    if record is None or str(record.get("tenant_id")) != tenant_id:
        return None
    return str(record.get("kb_id") or "") or None


def _public_generation(
    request: Request, row: Mapping[str, Any], tenant_id: str
) -> dict[str, Any]:
    manifest = row.get("manifest")
    manifest_summary: dict[str, Any] | None = None
    if isinstance(manifest, Mapping):
        files = manifest.get("files")
        contract = manifest.get("contract")
        manifest_summary = {
            "schema_version": manifest.get("schema_version"),
            "file_count": len(files) if isinstance(files, list) else None,
            "total_bytes": manifest.get("total_bytes"),
            "contract": dict(contract) if isinstance(contract, Mapping) else None,
        }
    physical_id = str(row.get("kb_id") or "")
    external_id = _external_kb_id(request, physical_id, tenant_id)
    return {
        "generation_id": row.get("generation_id"),
        "kb_id": external_id,
        "kb_available": external_id is not None,
        "build_id": row.get("build_id"),
        "status": row.get("status"),
        "base_generation_id": row.get("base_generation_id"),
        "fencing_token": row.get("fencing_token"),
        "manifest_sha256": row.get("manifest_sha256"),
        "manifest": manifest_summary,
        "created_at": row.get("created_at"),
        "prepared_at": row.get("prepared_at"),
        "published_at": row.get("published_at"),
        "aborted_at": row.get("aborted_at"),
    }


@router.get("/jobs")
async def list_ha_jobs(
    request: Request,
    queue: str | None = Query(default=None, min_length=1, max_length=128),
    status: JobStatus | None = None,
    before_created_at: float | None = Query(default=None, ge=0),
    before_job_id: str | None = Query(default=None, min_length=1, max_length=255),
    limit: int = Query(default=100, ge=1, le=200),
):
    runtime = _runtime(request)
    principal = request_principal(request)
    rows = await run_sync(
        request.app.state.offload_executor,
        runtime.jobs.list_jobs,
        queue=queue,
        tenant_id=principal.tenant_id,
        status=status,
        before=_cursor(
            before_created_at, before_job_id, identifier_name="before_job_id"
        ),
        limit=limit + 1,
    )
    has_more = len(rows) > limit
    jobs = [_public_job(row) for row in rows[:limit]]
    next_cursor = (
        {
            "before_created_at": jobs[-1]["created_at"],
            "before_job_id": jobs[-1]["job_id"],
        }
        if has_more
        else None
    )
    return {"schema_version": "v1", "jobs": jobs, "next_cursor": next_cursor}


@router.get("/jobs/{job_id}")
async def get_ha_job(job_id: str, request: Request):
    runtime = _runtime(request)
    principal = request_principal(request)
    row = await run_sync(request.app.state.offload_executor, runtime.jobs.get, job_id)
    if row is None or row.get("tenant_id") != principal.tenant_id:
        raise HTTPException(status_code=404, detail="HA 作业不存在")
    return _public_job(row)


@router.post("/jobs/{job_id}/cancel")
async def cancel_ha_job(job_id: str, request: Request):
    runtime = _runtime(request)
    principal = request_principal(request)
    row = await run_sync(
        request.app.state.offload_executor,
        runtime.jobs.request_cancel,
        job_id,
        tenant_id=principal.tenant_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="HA 作业不存在")
    return _public_job(row)


@router.post("/jobs/{job_id}/replay", status_code=201)
async def replay_ha_job(job_id: str, body: DeadLetterReplay, request: Request):
    runtime = _runtime(request)
    principal = request_principal(request)
    try:
        row = await run_sync(
            request.app.state.offload_executor,
            runtime.jobs.replay_dead_letter,
            job_id,
            replay_key=body.replay_key,
            tenant_id=principal.tenant_id,
        )
    except JobConflict as exc:
        # A foreign job and a non-dead-letter job are deliberately
        # indistinguishable at this boundary.
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _public_job(row)


@router.get("/schedules")
async def list_ha_schedules(
    request: Request,
    queue: str | None = Query(default=None, min_length=1, max_length=128),
    enabled: bool | None = None,
    before_created_at: float | None = Query(default=None, ge=0),
    before_schedule_id: str | None = Query(default=None, min_length=1, max_length=255),
    limit: int = Query(default=100, ge=1, le=200),
):
    runtime = _runtime(request)
    principal = request_principal(request)
    rows = await run_sync(
        request.app.state.offload_executor,
        runtime.schedules.list_schedules,
        tenant_id=principal.tenant_id,
        queue=queue,
        enabled=enabled,
        before=_cursor(
            before_created_at,
            before_schedule_id,
            identifier_name="before_schedule_id",
        ),
        limit=limit + 1,
    )
    has_more = len(rows) > limit
    schedules = [_public_schedule(row) for row in rows[:limit]]
    next_cursor = (
        {
            "before_created_at": schedules[-1]["created_at"],
            "before_schedule_id": schedules[-1]["schedule_id"],
        }
        if has_more
        else None
    )
    return {
        "schema_version": "v1",
        "schedules": schedules,
        "next_cursor": next_cursor,
    }


@router.patch("/schedules/{schedule_id}")
async def update_ha_schedule(schedule_id: str, body: ScheduleUpdate, request: Request):
    runtime = _runtime(request)
    principal = request_principal(request)
    try:
        row = await run_sync(
            request.app.state.offload_executor,
            runtime.schedules.set_enabled,
            schedule_id,
            body.enabled,
            expected_revision=body.expected_revision,
            tenant_id=principal.tenant_id,
        )
    except RuntimeError as exc:
        existing = await run_sync(
            request.app.state.offload_executor,
            runtime.schedules.get,
            schedule_id,
        )
        if existing is None or existing.get("tenant_id") != principal.tenant_id:
            raise HTTPException(status_code=404, detail="HA 调度不存在") from exc
        raise HTTPException(status_code=409, detail="HA 调度版本已变化") from exc
    return _public_schedule(row)


@router.get("/index-generations")
async def list_ha_index_generations(
    request: Request,
    kb_id: str | None = Query(default=None, min_length=1, max_length=160),
    status: GenerationStatus | None = None,
    before_created_at: float | None = Query(default=None, ge=0),
    before_generation_id: str | None = Query(
        default=None, min_length=1, max_length=255
    ),
    limit: int = Query(default=100, ge=1, le=200),
):
    runtime = _runtime(request)
    principal = request_principal(request)
    storage_id: str | None = None
    if kb_id is not None:
        record = request.app.state.kb_registry.resolve(kb_id, principal.tenant_id)
        if record is None:
            raise HTTPException(status_code=404, detail="知识库不存在")
        storage_id = str(record["storage_id"])
    rows = await run_sync(
        request.app.state.offload_executor,
        runtime.index_generations.list_tenant_generations,
        principal.tenant_id,
        kb_id=storage_id,
        status=status,
        before=_cursor(
            before_created_at,
            before_generation_id,
            identifier_name="before_generation_id",
        ),
        limit=limit + 1,
    )
    has_more = len(rows) > limit
    generations = [
        _public_generation(request, row, principal.tenant_id) for row in rows[:limit]
    ]
    next_cursor = (
        {
            "before_created_at": generations[-1]["created_at"],
            "before_generation_id": generations[-1]["generation_id"],
        }
        if has_more
        else None
    )
    return {
        "schema_version": "v1",
        "generations": generations,
        "next_cursor": next_cursor,
    }


__all__ = ["router"]
