from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator

from cogdoc.api.audit_exports import AuditExportConflict, AuditExportError
from cogdoc.api.offload import run_sync
from cogdoc.api.tenant_scope import request_principal


router = APIRouter(prefix="/v1/audit-events/exports", tags=["audit"])


class AuditExportCreate(BaseModel):
    from_sequence: int | None = Field(default=None, ge=1)
    to_sequence: int | None = Field(default=None, ge=1)
    actions: list[str] = Field(default_factory=list, max_length=50)
    statuses: list[int] = Field(default_factory=list, max_length=50)
    retention_seconds: int = Field(default=86_400, ge=300, le=604_800)

    @field_validator("actions")
    @classmethod
    def validate_actions(cls, value: list[str]) -> list[str]:
        result = []
        for item in value:
            cleaned = item.strip()
            if not cleaned or len(cleaned) > 200:
                raise ValueError("actions must contain 1-200 character values")
            result.append(cleaned)
        return result

    @field_validator("statuses")
    @classmethod
    def validate_statuses(cls, value: list[int]) -> list[int]:
        if any(type(item) is not int or not 100 <= item <= 599 for item in value):
            raise ValueError("statuses must contain HTTP status codes")
        return value


def _manager(request: Request):
    manager = getattr(request.app.state, "audit_export_manager", None)
    if manager is None:
        raise HTTPException(status_code=503, detail="审计导出服务不可用")
    return manager


@router.post("", status_code=202)
async def create_audit_export(body: AuditExportCreate, request: Request):
    principal = request_principal(request)
    if (
        body.from_sequence is not None
        and body.to_sequence is not None
        and body.from_sequence > body.to_sequence
    ):
        raise HTTPException(status_code=422, detail="起始序号不能大于结束序号")
    try:
        return _manager(request).submit(
            tenant_id=principal.tenant_id,
            actor_id=principal.subject_id,
            **body.model_dump(),
        )
    except AuditExportConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("")
async def list_audit_exports(
    request: Request,
    limit: int = Query(default=100, ge=1, le=100),
):
    principal = request_principal(request)
    store = _manager(request).store
    return {
        "schema_version": "v1",
        "exports": await run_sync(
            request.app.state.offload_executor,
            store.list_jobs,
            principal.tenant_id,
            limit=limit,
        ),
    }


@router.get("/{job_id}")
async def get_audit_export(job_id: str, request: Request):
    principal = request_principal(request)
    row = await run_sync(
        request.app.state.offload_executor,
        _manager(request).store.get,
        job_id,
        principal.tenant_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="审计导出不存在")
    return row


@router.get("/{job_id}/content", response_class=FileResponse)
async def download_audit_export(job_id: str, request: Request):
    principal = request_principal(request)
    try:
        path = await run_sync(
            request.app.state.source_artifact_executor,
            _manager(request).store.artifact_path,
            job_id,
            principal.tenant_id,
        )
    except AuditExportConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except AuditExportError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return FileResponse(
        path,
        media_type="application/x-ndjson",
        filename=f"{job_id}.ndjson",
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


@router.delete("/{job_id}", status_code=204)
async def delete_audit_export(
    job_id: str,
    request: Request,
    expected_revision: int = Query(ge=1),
) -> Response:
    principal = request_principal(request)
    try:
        deleted = await run_sync(
            request.app.state.offload_executor,
            _manager(request).store.delete,
            job_id,
            principal.tenant_id,
            expected_revision=expected_revision,
        )
    except AuditExportConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="审计导出不存在")
    return Response(status_code=204)
