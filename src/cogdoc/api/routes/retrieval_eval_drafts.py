from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from cogdoc.api.eval_review_auth import require_eval_reviewer
from cogdoc.api.retrieval_eval_draft_store import (
    DraftRevisionConflictError,
    RetrievalEvalDraftStore,
)
from cogdoc.api.schemas import RetrievalEvalDraftReviewRequest
from cogdoc.api.tenant_scope import (
    externalize_kb_fields,
    request_principal,
    resolve_kb_scope,
    row_is_authorized,
    scope_for_storage_id,
)
from cogdoc.service.index_provenance import current_index_provenance
from cogdoc.tools.eval.retrieval_eval_drafts import (
    DraftStatus,
    EvidenceUnitTask,
    apply_review_annotations,
    detect_stale_reasons,
    export_retrieval_eval_case,
)


router = APIRouter(prefix="/v1/retrieval-eval-drafts", tags=["retrieval-eval"])
_MAX_EXPORT_ROWS = 2**31 - 1


def _store(request: Request) -> RetrievalEvalDraftStore:
    store: RetrievalEvalDraftStore | None = getattr(
        request.app.state, "retrieval_eval_draft_store", None
    )
    if store is None:
        raise HTTPException(status_code=503, detail="证据评测草稿存储不可用")
    return store


def _kb_storage_id(request: Request, kb_id: str) -> str:
    scope = resolve_kb_scope(request, kb_id, allow_legacy_default=True)
    if scope is None:
        raise HTTPException(status_code=404, detail="知识库不存在")
    return scope.storage_id


def _owns_storage_id(request: Request, storage_id: object) -> bool:
    value = str(storage_id or "")
    if not value:
        return False
    if scope_for_storage_id(request, value) is not None:
        return True
    principal = request_principal(request)
    if principal.tenant_id != "default":
        return False
    if value.startswith("t-"):
        return False
    registry = request.app.state.kb_registry
    getter = getattr(registry, "get_by_storage_id", None)
    # Unregistered rows are historical single-tenant data. A physical ID that
    # is registered to another tenant must never enter this compatibility path.
    return not callable(getter) or getter(value) is None


def _require_owned_draft(request: Request, draft_id: str) -> dict[str, Any]:
    row = _store(request).get(draft_id)
    if row is None or not _row_allowed(request, row):
        raise HTTPException(status_code=404, detail="证据评测草稿不存在")
    return row


def _row_allowed(request: Request, row: Mapping[str, Any]) -> bool:
    storage_id = str(row.get("kb_id") or "")
    scope = scope_for_storage_id(request, storage_id) if storage_id else None
    if scope is not None:
        return row_is_authorized(request, scope, row)
    return _owns_storage_id(request, storage_id)


def _review_actor(request: Request, legacy_actor: str) -> str:
    principal = request_principal(request)
    return legacy_actor if principal.tenant_id == "default" else principal.subject_id


def _snapshot(kb_id: str) -> dict[str, Any]:
    return {"kb_id": kb_id, **current_index_provenance(kb_id)}


def _stale_reasons(
    row: Mapping[str, Any],
    snapshots: dict[str, dict[str, Any]] | None = None,
) -> list[str]:
    kb_id = str(row.get("kb_id") or "")
    if snapshots is None:
        snapshot = _snapshot(kb_id)
    else:
        cached_snapshot = snapshots.get(kb_id)
        if cached_snapshot is None:
            snapshot = _snapshot(kb_id)
            snapshots[kb_id] = snapshot
        else:
            snapshot = cached_snapshot
    return list(detect_stale_reasons(row, snapshot))


def _public_row(
    row: Mapping[str, Any],
    request: Request,
    snapshots: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    reasons = _stale_reasons(row, snapshots)
    return externalize_kb_fields(
        {**row, "is_stale": bool(reasons), "stale_reasons": reasons},
        request,
    )


def _task_kinds(row: Mapping[str, Any]) -> set[str]:
    units = row.get("units")
    if not isinstance(units, list):
        return set()
    return {
        str(unit.get("task_kind") or "")
        for unit in units
        if isinstance(unit, Mapping) and unit.get("task_kind")
    }


# Static routes must be declared before /{draft_id}.
@router.get("/export")
async def export_retrieval_eval_drafts(
    request: Request,
    dataset_partition: Literal["training", "release_gate"] = "training",
    export_format: Literal["generic_v1", "retrieval_eval_v1"] = Query(
        default="generic_v1", alias="format"
    ),
    _reviewer: str = Depends(require_eval_reviewer),
):
    store = _store(request)
    rows = store.list(
        status=DraftStatus.APPROVED,
        dataset_partition=dataset_partition,
        limit=_MAX_EXPORT_ROWS,
    )
    rows = [row for row in rows if _row_allowed(request, row)]
    rows.sort(key=lambda row: str(row.get("draft_id") or ""))
    items: list[dict[str, Any]] = []
    stale: list[dict[str, Any]] = []
    incompatible: list[str] = []
    snapshots: dict[str, dict[str, Any]] = {}
    for row in rows:
        reasons = _stale_reasons(row, snapshots)
        if reasons:
            stale.append({"draft_id": row["draft_id"], "reasons": reasons})
            continue
        if export_format == "retrieval_eval_v1" and _task_kinds(row) != {
            EvidenceUnitTask.QA_REQUIREMENT.value
        }:
            incompatible.append(str(row["draft_id"]))
            continue
        item = (
            dict(row)
            if export_format == "generic_v1"
            else export_retrieval_eval_case(row)
        )
        items.append(externalize_kb_fields(item, request))
    return {
        "schema_version": "v1",
        "format": export_format,
        "dataset_partition": dataset_partition,
        "exported_count": len(items),
        "excluded_stale_count": len(stale),
        "excluded_incompatible_count": len(incompatible),
        "excluded_stale": stale,
        "excluded_incompatible": incompatible,
        "items": items,
    }


@router.get("")
async def list_retrieval_eval_drafts(
    request: Request,
    kb_id: str | None = None,
    status: Literal["pending", "approved", "rejected"] | None = None,
    dataset_partition: Literal["training", "release_gate"] | None = None,
    task_kind: Literal["qa_requirement", "summary_section", "compare_source_dimension"]
    | None = None,
    is_stale: bool | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    _reviewer: str = Depends(require_eval_reviewer),
):
    storage_id = _kb_storage_id(request, kb_id) if kb_id is not None else None
    rows = _store(request).list(
        kb_id=storage_id,
        status=status,
        dataset_partition=dataset_partition,
        limit=_MAX_EXPORT_ROWS
        if kb_id is None or task_kind is not None or is_stale is not None
        else limit,
    )
    decorated = []
    snapshots: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not _row_allowed(request, row):
            continue
        if task_kind is not None and task_kind not in _task_kinds(row):
            continue
        public = _public_row(row, request, snapshots)
        if is_stale is not None and public["is_stale"] is not is_stale:
            continue
        decorated.append(public)
        if len(decorated) >= limit:
            break
    return {"schema_version": "v1", "drafts": decorated}


@router.get("/{draft_id}")
async def get_retrieval_eval_draft(
    draft_id: str,
    request: Request,
    _reviewer: str = Depends(require_eval_reviewer),
):
    row = _require_owned_draft(request, draft_id)
    return {"schema_version": "v1", "draft": _public_row(row, request)}


@router.post("/{draft_id}/review")
async def review_retrieval_eval_draft(
    draft_id: str,
    body: RetrievalEvalDraftReviewRequest,
    request: Request,
    reviewer: str = Depends(require_eval_reviewer),
):
    store = _store(request)
    current = _require_owned_draft(request, draft_id)
    if body.decision == DraftStatus.APPROVED.value:
        try:
            preview: Mapping[str, Any] = current
            if body.annotations is not None:
                preview = apply_review_annotations(
                    current, body.annotations
                ).model_dump(mode="json")
            reasons = _stale_reasons(preview)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if reasons:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "索引版本已变化，草稿必须重新标注",
                    "reasons": reasons,
                },
            )
    try:
        updated = store.review(
            draft_id,
            decision=body.decision,
            reviewer=_review_actor(request, reviewer),
            annotations=body.annotations,
            reason=body.reason,
            expected_revision=body.expected_revision,
        )
    except DraftRevisionConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="证据评测草稿不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"schema_version": "v1", "draft": _public_row(updated, request)}
