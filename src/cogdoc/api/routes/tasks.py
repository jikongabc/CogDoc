from __future__ import annotations

from collections.abc import Mapping

from fastapi import APIRouter, Query, Request

from cogdoc.api.offload import run_sync
from cogdoc.api.schemas import ConnectorSyncJob, ConnectorSyncJobList
from cogdoc.api.tenant_scope import request_principal, tenant_kb_scopes


router = APIRouter(prefix="/v1", tags=["tasks"])


def _public_sync_job(row: Mapping, external_kb_id: str) -> ConnectorSyncJob:
    values = {key: row.get(key) for key in ConnectorSyncJob.model_fields}
    values["kb_id"] = external_kb_id
    return ConnectorSyncJob.model_validate(values)


@router.get("/sync-jobs", response_model=ConnectorSyncJobList)
async def list_workspace_sync_jobs(
    request: Request,
    limit: int = Query(default=200, ge=1, le=500),
):
    """Return one bounded, ACL-filtered sync queue for the active workspace."""

    principal = request_principal(request)
    scopes = tenant_kb_scopes(request)
    external_ids = {scope.storage_id: scope.external_id for scope in scopes}
    rows = await run_sync(
        request.app.state.offload_executor,
        request.app.state.connector_sync_store.list_workspace_jobs,
        principal.tenant_id,
        set(external_ids),
        limit=limit,
    )
    jobs = []
    for row in rows:
        external_id = external_ids.get(str(row.get("kb_id") or ""))
        if external_id is not None:
            jobs.append(_public_sync_job(row, external_id))
    return ConnectorSyncJobList(jobs=jobs)
