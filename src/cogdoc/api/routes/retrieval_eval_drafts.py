from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from cogdoc.api.eval_review_auth import require_eval_reviewer
from cogdoc.api.ha_chat_authority import (
    HAChatAuthorityChanged,
    capture_ha_chat_epoch,
    ha_authority_guard,
)
from cogdoc.api.offload import run_sync
from cogdoc.api.retrieval_eval_draft_store import (
    DraftRevisionConflictError,
    RetrievalEvalDraftStore,
)
from cogdoc.api.schemas import RetrievalEvalDraftReviewRequest
from cogdoc.api.tenancy import Permission
from cogdoc.api.tenant_scope import (
    externalize_kb_fields,
    request_principal,
    retrieval_scope_for_request,
    resolve_kb_scope,
    row_is_authorized,
    scope_for_storage_id,
)
from cogdoc.config.settings import get_settings
from cogdoc.ha.feedback import StaleAuxiliaryWrite
from cogdoc.service.kb_readers import kb_read_lease
from cogdoc.service.index_provenance import current_index_provenance
from cogdoc.service.retrieval_pipeline import (
    build_retrieval_queries,
    retrieve_candidate_pool,
)
from cogdoc.service.retriever_factory import RetrieverFactory
from cogdoc.tools.eval.retrieval_eval_drafts import (
    DraftStatus,
    EvidenceUnitTask,
    apply_review_annotations,
    detect_stale_reasons,
    export_retrieval_eval_case,
)
from cogdoc.tools.retriever.metadata import safe_retrieval_metadata
from cogdoc.tools.retriever.scope import RetrievalScope


router = APIRouter(prefix="/v1/retrieval-eval-drafts", tags=["retrieval-eval"])
_MAX_EXPORT_ROWS = 2**31 - 1


def _candidate_retrieval_scope(request: Request, kb_id: str) -> RetrievalScope:
    scope = scope_for_storage_id(request, kb_id)
    if scope is None:
        return RetrievalScope(include_derived_knowledge=False)
    allowed = retrieval_scope_for_request(request, scope)
    return RetrievalScope(
        allowed_sources=allowed.allowed_sources,
        include_derived_knowledge=False,
        access_mode=allowed.access_mode,
    )


def _retrieve_draft_candidates(
    row: Mapping[str, Any],
    *,
    runtime: Any,
    retrieval_scope: RetrievalScope,
    top_k: int,
) -> dict[str, Any]:
    """Retrieve reviewer-visible source chunks without promoting them to gold."""

    kb_id = str(row.get("kb_id") or "")
    original_query = str(row.get("query") or "")
    units = row.get("units")
    requirements: list[dict[str, str]] = []
    if isinstance(units, list):
        for unit in units:
            if not isinstance(unit, Mapping):
                continue
            requirements.append(
                {
                    "requirement_id": str(unit.get("unit_id") or ""),
                    "retrieval_query": str(unit.get("retrieval_query") or ""),
                    "recovery_query": str(unit.get("recovery_query") or ""),
                }
            )
    queries = build_retrieval_queries(
        original_query,
        rewritten_queries=[
            requirement["recovery_query"]
            for requirement in requirements
            if requirement["recovery_query"]
        ],
        evidence_requirements=requirements,
        max_queries=max(1, min(25, len(requirements) * 2 + 1)),
    )
    with kb_read_lease(kb_id):
        result = retrieve_candidate_pool(
            RetrieverFactory.get_engine(kb_id),
            runtime.derived_knowledge_retriever,
            runtime.retrieval_feedback_store,
            kb_id=kb_id,
            original_query=original_query,
            queries=queries,
            top_k=top_k,
            rrf_k=float(get_settings().hybrid_rrf_k),
            fusion_top_n=top_k,
            scope=retrieval_scope,
        )

    source_versions = {
        str(item.get("source") or ""): str(item.get("sha256") or "")
        for item in row.get("source_versions") or []
        if isinstance(item, Mapping)
    }
    candidates: list[dict[str, Any]] = []
    for rank, doc in enumerate(result.docs, start=1):
        if not isinstance(doc, Mapping):
            continue
        raw_meta = doc.get("meta")
        meta = raw_meta if isinstance(raw_meta, Mapping) else {}
        source = str(meta.get("source") or "")
        retrieval = safe_retrieval_metadata(doc.get("retrieval"))
        text = str(doc.get("text") or "")
        page = meta.get("page")
        candidates.append(
            {
                "rank": rank,
                "text": text,
                "text_length": len(text),
                "chunk_id": str(meta.get("chunk_id") or ""),
                "parent_chunk_id": str(meta.get("parent_chunk_id") or ""),
                "source": source,
                "source_id": str(meta.get("source_id") or ""),
                "source_version_id": str(meta.get("source_version_id") or ""),
                "media_type": str(meta.get("media_type") or ""),
                "location": dict(meta.get("source_location") or {})
                if isinstance(meta.get("source_location"), Mapping)
                else {},
                "source_sha256": str(
                    meta.get("source_sha256") or source_versions.get(source) or ""
                ),
                "page": page,
                "page_start": meta.get("page_start", page),
                "page_end": meta.get("page_end", page),
                "section_title": str(meta.get("section_title") or ""),
                "matched_requirement_ids": list(
                    retrieval.get("matched_requirement_ids")
                    or retrieval.get("matched_unit_ids")
                    or []
                ),
                "retrieval": retrieval,
            }
        )
    return {
        "queries": [query.text for query in result.queries],
        "ranking_count": result.ranking_count,
        "channel_counts": result.channel_counts,
        "feedback_error": result.feedback_error,
        "candidates": candidates,
    }


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


@router.get("/{draft_id}/candidates")
async def list_retrieval_eval_draft_candidates(
    draft_id: str,
    request: Request,
    top_k: int = Query(default=12, ge=1, le=30),
    _reviewer: str = Depends(require_eval_reviewer),
):
    row = _require_owned_draft(request, draft_id)
    reasons = _stale_reasons(row)
    if reasons:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "索引版本已变化，不能继续标注这个草稿",
                "reasons": reasons,
            },
        )
    result = await run_sync(
        request.app.state.offload_executor,
        _retrieve_draft_candidates,
        row,
        runtime=request.app.state.state_runtime,
        retrieval_scope=_candidate_retrieval_scope(
            request, str(row.get("kb_id") or "")
        ),
        top_k=top_k,
    )
    return {
        "schema_version": "v1",
        "draft_id": draft_id,
        "is_stale": False,
        **result,
    }


@router.post("/{draft_id}/review")
async def review_retrieval_eval_draft(
    draft_id: str,
    body: RetrievalEvalDraftReviewRequest,
    request: Request,
    reviewer: str = Depends(require_eval_reviewer),
):
    store = _store(request)
    current = _require_owned_draft(request, draft_id)
    frozen_revision = int(current.get("revision") or 0)
    frozen_index_generation: str | None = None
    if (
        frozen_revision < 1
        or body.expected_revision is not None
        and body.expected_revision != frozen_revision
    ):
        raise HTTPException(status_code=409, detail="草稿版本已变化")
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
        frozen_index_generation = str(preview.get("index_generation") or "")
    try:
        authority = None
        if getattr(request.app.state, "ha_feedback_multiwriter_mode", False):
            scope = scope_for_storage_id(request, str(current.get("kb_id") or ""))
            if scope is None:
                raise HAChatAuthorityChanged("shared draft scope is unavailable")
            expected_epoch = capture_ha_chat_epoch(
                request.app.state.kb_registry, scope.storage_id
            )
            guard = ha_authority_guard(
                request,
                scope,
                expected_epoch,
                permission=Permission.REVIEW,
            )
            evidence = getattr(guard, "evidence", None)
            if not isinstance(evidence, Mapping):
                raise HAChatAuthorityChanged("shared draft authority is unavailable")
            authority = (expected_epoch, evidence)
        review_authorized = getattr(store, "review_authorized", None)
        options = {
            "decision": body.decision,
            "reviewer": _review_actor(request, reviewer),
            "annotations": body.annotations,
            "reason": body.reason,
            "expected_revision": frozen_revision,
        }
        updated = (
            review_authorized(
                draft_id,
                expected_epoch=authority[0],
                authority=authority[1],
                expected_index_generation=frozen_index_generation,
                **options,
            )
            if authority is not None and callable(review_authorized)
            else store.review(draft_id, **options)
        )
    except (HAChatAuthorityChanged, StaleAuxiliaryWrite) as exc:
        raise HTTPException(status_code=409, detail="访问权限已变化") from exc
    except DraftRevisionConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="证据评测草稿不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"schema_version": "v1", "draft": _public_row(updated, request)}
