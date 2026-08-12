import logging
from collections.abc import Mapping

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import JSONResponse

from cogdoc.api.schemas import (
    DerivedKnowledge,
    ErrorCode,
    ErrorResponse,
    FeedbackLoopMetricsResponse,
    KnowledgeBatchReviewRequest,
    KnowledgeBatchReviewResponse,
    KnowledgeConflictCandidate,
    KnowledgeCreateRequest,
    KnowledgeCreateResponse,
    KnowledgeListResponse,
    KnowledgeOrigin,
    KnowledgePendingCountResponse,
    KnowledgeReviewRequest,
    KnowledgeReviseRequest,
    KnowledgeStatus,
    KnowledgeStaleScanResponse,
    ReviewQueueExportResponse,
    ReviewQueueSummaryResponse,
    build_error_response,
)
from cogdoc.api.time_utils import now_iso
from cogdoc.api.tenant_scope import (
    externalize_kb_fields,
    request_principal,
    resource_access_decision,
    resolve_kb_scope,
    row_is_authorized,
    scope_for_storage_id,
)
from cogdoc.api.webhooks import notify_pending_created
from cogdoc.observability.logger import log_event
from cogdoc.service.kb_state import KBState
from cogdoc.tools.manifest import load_index_manifest

router = APIRouter(prefix="/v1", tags=["knowledge"])

_ERROR_RESPONSES = {
    400: {"model": ErrorResponse},
    404: {"model": ErrorResponse},
}


# 计算比率。
def _rate(numerator: int, denominator: int | None) -> float | None:
    if denominator is None or denominator <= 0:
        return None
    return round(numerator / denominator, 4)


# 读取服务端已记录回答数。
def _stored_answer_count(request: Request, kb_id: str) -> int:
    counter = getattr(request.app.state.session_store, "answer_count", None)
    if not callable(counter):
        return 0
    try:
        return int(counter(kb_id))
    except Exception as exc:
        log_event(
            "knowledge",
            "answer_count_failed",
            {},
            level=logging.WARNING,
            kb_id=kb_id,
            error_class=type(exc).__name__,
        )
        return 0


# 重建派生知识索引。
def _refresh_derived_knowledge_index(kb_id: str, store) -> None:
    from cogdoc.tools.retriever.derived_knowledge import DerivedKnowledgeIndex

    DerivedKnowledgeIndex(store).rebuild(kb_id)


# 查询派生知识索引状态。
def _derived_knowledge_index_status(kb_id: str, store) -> dict:
    from cogdoc.tools.retriever.derived_knowledge import DerivedKnowledgeIndex

    return DerivedKnowledgeIndex(store).status(kb_id)


# 容错刷新派生知识索引。
def _refresh_derived_knowledge_index_quiet(
    refresher,
    kb_id: str,
    store,
    error_recorder=None,
) -> None:
    try:
        refresher(kb_id, store)
    except Exception as exc:
        try:
            if error_recorder is not None:
                error_recorder(kb_id, type(exc).__name__)
            else:
                from cogdoc.tools.retriever.derived_knowledge import (
                    DerivedKnowledgeIndex,
                )

                DerivedKnowledgeIndex(store).record_error(
                    kb_id,
                    type(exc).__name__,
                )
        except Exception:
            pass
        log_event(
            "knowledge",
            "derived_knowledge_index_refresh_failed",
            {},
            level=logging.WARNING,
            kb_id=kb_id,
            error_class=type(exc).__name__,
        )


# 后台刷新派生知识索引。
def _queue_derived_knowledge_index_refresh(request: Request, kb_id: str | None) -> None:
    if not kb_id:
        return
    if not getattr(request.app.state, "derived_knowledge_index_auto_refresh", False):
        return
    refresher = (
        getattr(
            request.app.state,
            "derived_knowledge_index_refresher",
            None,
        )
        or _refresh_derived_knowledge_index
    )
    try:
        request.app.state.offload_executor.submit(
            _refresh_derived_knowledge_index_quiet,
            refresher,
            kb_id,
            request.app.state.knowledge_store,
            getattr(
                request.app.state,
                "derived_knowledge_index_error_recorder",
                None,
            ),
        )
    except RuntimeError as exc:
        log_event(
            "knowledge",
            "derived_knowledge_index_refresh_submit_failed",
            {},
            level=logging.WARNING,
            kb_id=kb_id,
            error_class=type(exc).__name__,
        )


# 完成 错误响应 处理。
def _error(code: ErrorCode, message: str, status: int) -> JSONResponse:
    return JSONResponse(
        status_code=status, content=build_error_response(code, message).model_dump()
    )


def _registered_storage_record(request: Request, storage_id: str):
    """Return a registry record by physical ID without tenant projection."""

    registry = request.app.state.kb_registry
    getter = getattr(registry, "get_by_storage_id", None)
    if callable(getter):
        return getter(storage_id)
    getter = getattr(registry, "get", None)
    return getter(storage_id) if callable(getter) else None


def _resolve_external_kb(request: Request, kb_id: str):
    """Resolve a tenant-local slug while preserving safe default compatibility.

    Legacy default-tenant tests and deployments may have knowledge rows without
    a registry entry.  That fallback is allowed only when the supplied value is
    not a registered physical ID, otherwise a default caller could address a
    named tenant by guessing its opaque storage ID.
    """

    try:
        scope = resolve_kb_scope(request, kb_id)
    except (TypeError, ValueError):
        scope = None
    if scope is not None:
        return scope
    if request_principal(request).tenant_id != "default":
        return None
    if isinstance(_registered_storage_record(request, kb_id), Mapping):
        return None
    try:
        return resolve_kb_scope(request, kb_id, allow_legacy_default=True)
    except (TypeError, ValueError):
        return None


def _knowledge_row_for_request(request: Request, knowledge_id: str):
    """Fetch an opaque knowledge ID only when it belongs to this tenant."""

    row = request.app.state.knowledge_store.get(knowledge_id)
    if not isinstance(row, Mapping):
        return None
    storage_id = str(row.get("kb_id") or "")
    if not storage_id:
        return None
    scope = scope_for_storage_id(request, storage_id)
    if scope is not None:
        return row if row_is_authorized(request, scope, row) else None
    # Unregistered knowledge is a legacy default-tenant artifact.  A registry
    # hit with a different tenant is an explicit denial, never a legacy row.
    if request_principal(request).tenant_id != "default":
        return None
    if isinstance(_registered_storage_record(request, storage_id), Mapping):
        return None
    return row


def _knowledge_not_found(knowledge_id: str) -> JSONResponse:
    return _error(
        ErrorCode.KNOWLEDGE_NOT_FOUND,
        f"知识不存在: {knowledge_id}",
        404,
    )


# 构建公开知识视图。
def _public(row: Mapping, request: Request) -> DerivedKnowledge:
    return DerivedKnowledge.model_validate(externalize_kb_fields(row, request))


def _full_kb_access(request: Request, scope) -> bool:
    decision = resource_access_decision(request, scope)
    return decision is None or str(
        getattr(getattr(decision, "mode", None), "value", "")
    ) == "all"


# 查询派生知识索引状态。
@router.get("/knowledge/index-status")
async def knowledge_index_status(request: Request, kb_id: str = Query(min_length=1)):
    scope = _resolve_external_kb(request, kb_id)
    if scope is None:
        return _error(ErrorCode.KB_NOT_FOUND, "知识库不存在", 404)
    statuser = (
        getattr(request.app.state, "derived_knowledge_index_statuser", None)
        or _derived_knowledge_index_status
    )
    try:
        status = statuser(scope.storage_id, request.app.state.knowledge_store)
    except Exception as exc:
        log_event(
            "knowledge",
            "derived_knowledge_index_status_failed",
            {},
            level=logging.WARNING,
            kb_id=scope.storage_id,
            error_class=type(exc).__name__,
        )
        status = {
            "kb_id": scope.storage_id,
            "state": "error",
            "error_class": type(exc).__name__,
        }
    status = externalize_kb_fields(status, request)
    status["kb_id"] = scope.external_id
    status["auto_refresh_enabled"] = bool(
        getattr(request.app.state, "derived_knowledge_index_auto_refresh", False)
    )
    return status


# 构建冲突候选视图。
def _conflict_public(row: dict) -> KnowledgeConflictCandidate:
    return KnowledgeConflictCandidate(
        knowledge_id=row["knowledge_id"],
        text=row["text"],
        status=row["status"],
        origin=row.get("origin") or "manual_entry",
        related_source=row.get("related_source"),
        created_at=row["created_at"],
    )


# 读取当前知识库文档清单。
def _current_documents(kb_id: str) -> list[dict]:
    active = KBState(kb_id).active()
    documents = (
        active.get("documents", [])
        if active is not None
        else load_index_manifest(kb_id).get("documents", [])
    )
    return [
        {"name": str(doc.get("name") or ""), "sha256": str(doc.get("sha256") or "")}
        for doc in documents
        if doc.get("name")
    ]


# 构建审核队列摘要。
def _build_review_queue_summary(
    request: Request,
    *,
    storage_id: str,
    external_id: str,
    document_id: str | None = None,
    origin: KnowledgeOrigin | None = None,
    created_by: str | None = None,
    has_conflict: bool | None = None,
    created_after: str | None = None,
    created_before: str | None = None,
) -> ReviewQueueSummaryResponse:
    origin_value = origin.value if origin is not None else None
    knowledge = request.app.state.knowledge_store.counts(
        kb_id=storage_id,
        document_id=document_id,
        origin=origin_value,
        created_by=created_by,
        has_conflict=has_conflict,
        created_after=created_after,
        created_before=created_before,
    )
    knowledge_conflicts = request.app.state.knowledge_store.conflict_counts(
        kb_id=storage_id,
        document_id=document_id,
        origin=origin_value,
        created_by=created_by,
        has_conflict=has_conflict,
        created_after=created_after,
        created_before=created_before,
    )
    auto_review = request.app.state.knowledge_store.auto_review_counts(
        kb_id=storage_id,
        document_id=document_id,
        origin=origin_value,
        created_by=created_by,
        has_conflict=has_conflict,
        created_after=created_after,
        created_before=created_before,
    )
    feedback_rows = request.app.state.feedback_store.counts(kb_id=storage_id)
    feedback = request.app.state.feedback_analysis_store.counts(kb_id=storage_id)
    retrieval = request.app.state.retrieval_feedback_store.counts(kb_id=storage_id)
    return ReviewQueueSummaryResponse(
        kb_id=external_id,
        knowledge=knowledge["by_status"],
        knowledge_origin=knowledge["by_origin"],
        knowledge_conflicts=knowledge_conflicts,
        knowledge_auto_review={
            **auto_review,
            "stale_pending": int(knowledge["by_status"].get("stale", 0)),
        },
        feedback_counts=feedback_rows,
        feedback_analysis={
            **feedback["by_action"],
            "needs_review": feedback["needs_review"],
            "total": feedback["total"],
        },
        feedback_analysis_type=feedback["by_type"],
        retrieval_feedback=retrieval,
    )


# 新增派生知识。
@router.post("/knowledge", status_code=201, responses=_ERROR_RESPONSES)
async def create_knowledge(body: KnowledgeCreateRequest, request: Request):
    scope = _resolve_external_kb(request, body.kb_id)
    if scope is None:
        return _error(
            ErrorCode.KB_NOT_FOUND,
            "知识库不存在",
            404,
        )
    payload = body.model_dump(exclude_none=True)
    payload["kb_id"] = scope.storage_id
    payload["created_by"] = request_principal(request).subject_id
    payload["status"] = KnowledgeStatus.PENDING.value
    if not row_is_authorized(request, scope, payload):
        return _error(ErrorCode.DOCUMENT_NOT_FOUND, "文档不存在", 404)
    try:
        row, deduplicated = request.app.state.knowledge_store.create(payload)
    except ValueError as exc:
        return _error(ErrorCode.BAD_REQUEST, str(exc), 400)
    conflicts = []
    if not deduplicated:
        conflicts = [
            item
            for item in request.app.state.knowledge_store.conflicts_for(row)
            if item.get("kb_id") == scope.storage_id
        ]
    if not deduplicated:
        notify_pending_created(request.app, row, "knowledge_create")
    return KnowledgeCreateResponse(
        knowledge=_public(row, request),
        deduplicated=deduplicated,
        requires_review=bool(conflicts),
        conflicts=[_conflict_public(item) for item in conflicts],
    )


# 查询派生知识。
@router.get("/knowledge", response_model=KnowledgeListResponse)
async def list_knowledge(
    request: Request,
    kb_id: str = Query(min_length=1),
    status: KnowledgeStatus | None = None,
    document_id: str | None = None,
    origin: KnowledgeOrigin | None = None,
    created_by: str | None = None,
    conflict_group_id: str | None = None,
    has_conflict: bool | None = None,
    created_after: str | None = None,
    created_before: str | None = None,
):
    scope = _resolve_external_kb(request, kb_id)
    if scope is None:
        return _error(ErrorCode.KB_NOT_FOUND, "知识库不存在", 404)
    rows = request.app.state.knowledge_store.list(
        kb_id=scope.storage_id,
        status=status.value if status is not None else None,
        document_id=document_id,
        origin=origin.value if origin is not None else None,
        created_by=created_by,
        conflict_group_id=conflict_group_id,
        has_conflict=has_conflict,
        created_after=created_after,
        created_before=created_before,
    )
    rows = [row for row in rows if row_is_authorized(request, scope, row)]
    return KnowledgeListResponse(knowledge=[_public(row, request) for row in rows])


# 查询待审核计数。
@router.get("/knowledge/pending-count", response_model=KnowledgePendingCountResponse)
async def pending_knowledge_count(
    request: Request,
    kb_id: str = Query(min_length=1),
):
    scope = _resolve_external_kb(request, kb_id)
    if scope is None:
        return _error(ErrorCode.KB_NOT_FOUND, "知识库不存在", 404)
    if not _full_kb_access(request, scope):
        return _error(ErrorCode.KB_NOT_FOUND, "知识库不存在", 404)
    knowledge = request.app.state.knowledge_store.counts(kb_id=scope.storage_id)
    analysis = request.app.state.feedback_analysis_store.counts(
        kb_id=scope.storage_id
    )
    by_status = knowledge["by_status"]
    pending = int(by_status.get(KnowledgeStatus.PENDING.value, 0))
    stale = int(by_status.get(KnowledgeStatus.STALE.value, 0))
    needs_review = int(analysis["needs_review"])
    return KnowledgePendingCountResponse(
        kb_id=scope.external_id,
        pending=pending,
        stale=stale,
        feedback_analysis_needs_review=needs_review,
        total=pending + stale,
    )


# 查询反馈闭环指标。
@router.get("/feedback-loop-metrics", response_model=FeedbackLoopMetricsResponse)
async def feedback_loop_metrics(
    request: Request,
    kb_id: str = Query(min_length=1),
    answer_count: int | None = Query(default=None, ge=0),
):
    scope = _resolve_external_kb(request, kb_id)
    if scope is None:
        return _error(ErrorCode.KB_NOT_FOUND, "知识库不存在", 404)
    if not _full_kb_access(request, scope):
        return _error(ErrorCode.KB_NOT_FOUND, "知识库不存在", 404)
    storage_id = scope.storage_id
    answer_total = max(
        answer_count or 0,
        _stored_answer_count(request, storage_id),
    )
    answer_denominator = answer_total if answer_total > 0 else None
    feedback = request.app.state.feedback_store.counts(kb_id=storage_id)
    knowledge = request.app.state.knowledge_store.counts(kb_id=storage_id)
    stale_review = request.app.state.knowledge_store.stale_review_counts(
        kb_id=storage_id
    )
    analysis = request.app.state.feedback_analysis_store.counts(kb_id=storage_id)
    retrieval = request.app.state.retrieval_feedback_store.counts(kb_id=storage_id)
    by_feedback = feedback["by_feedback"]
    by_type = feedback["by_type"]
    by_status = knowledge["by_status"]
    by_action = analysis["by_action"]
    feedback_total = int(feedback["total"])
    negative_total = int(feedback["bad_cases"])
    correction_total = int(by_feedback.get("correction", 0))
    no_evidence_total = int(by_type.get("no_evidence", 0))
    knowledge_total = int(knowledge["total"])
    approved_total = int(by_status.get(KnowledgeStatus.APPROVED.value, 0))
    rejected_total = int(by_status.get(KnowledgeStatus.REJECTED.value, 0))
    pending_created = int(by_action.get("create_pending_knowledge", 0))
    retrieval_total = int(retrieval["total"])
    retrieval_disabled = int(retrieval["disabled"])
    stale_total = int(stale_review["total"])
    stale_reviewed = int(stale_review["reviewed"])
    return FeedbackLoopMetricsResponse(
        kb_id=scope.external_id,
        counts={
            "answer_total": answer_total,
            "feedback_total": feedback_total,
            "negative_feedback_total": negative_total,
            "no_evidence_feedback_total": no_evidence_total,
            "correction_feedback_total": correction_total,
            "knowledge_total": knowledge_total,
            "approved_knowledge_total": approved_total,
            "rejected_knowledge_total": rejected_total,
            "pending_created_total": pending_created,
            "retrieval_feedback_total": retrieval_total,
            "retrieval_feedback_disabled": retrieval_disabled,
            "stale_knowledge_total": stale_total,
            "stale_knowledge_reviewed": stale_reviewed,
        },
        rates={
            "feedback_rate": _rate(feedback_total, answer_denominator),
            "negative_feedback_rate": _rate(negative_total, answer_denominator),
            "no_evidence_rate": _rate(no_evidence_total, answer_denominator),
            "pending_approval_rate": _rate(approved_total, knowledge_total),
            "pending_rejection_rate": _rate(rejected_total, knowledge_total),
            "feedback_to_pending_rate": _rate(pending_created, correction_total),
            "retrieval_feedback_rollback_rate": _rate(
                retrieval_disabled, retrieval_total
            ),
            "stale_review_completion_rate": _rate(stale_reviewed, stale_total),
        },
    )


# 查询审核队列摘要。
@router.get("/review-queue", response_model=ReviewQueueSummaryResponse)
async def review_queue_summary(
    request: Request,
    kb_id: str = Query(min_length=1),
    document_id: str | None = None,
    origin: KnowledgeOrigin | None = None,
    created_by: str | None = None,
    has_conflict: bool | None = None,
    created_after: str | None = None,
    created_before: str | None = None,
):
    scope = _resolve_external_kb(request, kb_id)
    if scope is None:
        return _error(ErrorCode.KB_NOT_FOUND, "知识库不存在", 404)
    if not _full_kb_access(request, scope):
        return _error(ErrorCode.KB_NOT_FOUND, "知识库不存在", 404)
    return _build_review_queue_summary(
        request,
        storage_id=scope.storage_id,
        external_id=scope.external_id,
        document_id=document_id,
        origin=origin,
        created_by=created_by,
        has_conflict=has_conflict,
        created_after=created_after,
        created_before=created_before,
    )


# 导出当前审核队列。
@router.get("/review-queue/export", response_model=ReviewQueueExportResponse)
async def review_queue_export(
    request: Request,
    kb_id: str = Query(min_length=1),
    knowledge_document_id: str | None = None,
    knowledge_origin: KnowledgeOrigin | None = None,
    knowledge_created_by: str | None = None,
    knowledge_has_conflict: bool | None = None,
    knowledge_created_after: str | None = None,
    knowledge_created_before: str | None = None,
    limit: int = Query(default=200, ge=1, le=1000),
):
    scope = _resolve_external_kb(request, kb_id)
    if scope is None:
        return _error(ErrorCode.KB_NOT_FOUND, "知识库不存在", 404)
    if not _full_kb_access(request, scope):
        return _error(ErrorCode.KB_NOT_FOUND, "知识库不存在", 404)
    storage_id = scope.storage_id
    origin_value = knowledge_origin.value if knowledge_origin is not None else None
    summary = _build_review_queue_summary(
        request,
        storage_id=storage_id,
        external_id=scope.external_id,
        document_id=knowledge_document_id,
        origin=knowledge_origin,
        created_by=knowledge_created_by,
        has_conflict=knowledge_has_conflict,
        created_after=knowledge_created_after,
        created_before=knowledge_created_before,
    )
    pending = request.app.state.knowledge_store.list(
        kb_id=storage_id,
        status=KnowledgeStatus.PENDING.value,
        document_id=knowledge_document_id,
        origin=origin_value,
        created_by=knowledge_created_by,
        has_conflict=knowledge_has_conflict,
        created_after=knowledge_created_after,
        created_before=knowledge_created_before,
        limit=limit,
    )
    stale = request.app.state.knowledge_store.list(
        kb_id=storage_id,
        status=KnowledgeStatus.STALE.value,
        document_id=knowledge_document_id,
        origin=origin_value,
        created_by=knowledge_created_by,
        has_conflict=knowledge_has_conflict,
        created_after=knowledge_created_after,
        created_before=knowledge_created_before,
        limit=limit,
    )
    analysis = request.app.state.feedback_analysis_store.list(
        kb_id=storage_id,
        needs_review=True,
        limit=limit,
    )
    retrieval = request.app.state.retrieval_feedback_store.list(
        kb_id=storage_id,
        enabled=True,
        limit=limit,
    )
    feedback = request.app.state.feedback_store.list(
        kb_id=storage_id,
        is_bad_case=True,
        limit=limit,
    )
    auto_review_events = request.app.state.knowledge_store.auto_review_events(
        kb_id=storage_id,
        document_id=knowledge_document_id,
        origin=origin_value,
        created_by=knowledge_created_by,
        has_conflict=knowledge_has_conflict,
        created_after=knowledge_created_after,
        created_before=knowledge_created_before,
        limit=limit,
    )
    return ReviewQueueExportResponse(
        kb_id=scope.external_id,
        generated_at=now_iso(),
        summary=summary,
        pending_knowledge=[_public(row, request) for row in pending],
        stale_knowledge=[_public(row, request) for row in stale],
        auto_review_events=externalize_kb_fields(auto_review_events, request),
        feedback_analysis_needs_review=externalize_kb_fields(analysis, request),
        retrieval_feedback_enabled=externalize_kb_fields(retrieval, request),
        feedback_bad_cases=externalize_kb_fields(feedback, request),
    )


# 删除派生知识。
@router.delete(
    "/knowledge/{knowledge_id}",
    status_code=204,
    responses=_ERROR_RESPONSES,
)
async def delete_knowledge(knowledge_id: str, request: Request):
    current = _knowledge_row_for_request(request, knowledge_id)
    if current is None:
        return _knowledge_not_found(knowledge_id)
    row = request.app.state.knowledge_store.delete(knowledge_id)
    if row is None:
        return _knowledge_not_found(knowledge_id)
    _queue_derived_knowledge_index_refresh(request, row.get("kb_id"))
    return Response(status_code=204)


# 扫描并标记过期知识。
@router.post(
    "/knowledge/stale-scan",
    response_model=KnowledgeStaleScanResponse,
    responses=_ERROR_RESPONSES,
)
async def scan_stale_knowledge(request: Request, kb_id: str = Query(min_length=1)):
    scope = _resolve_external_kb(request, kb_id)
    if scope is None:
        return _error(ErrorCode.KB_NOT_FOUND, "知识库不存在", 404)
    storage_id = scope.storage_id
    stale = request.app.state.knowledge_store.mark_stale_by_documents(
        storage_id, _current_documents(storage_id)
    )
    if stale:
        _queue_derived_knowledge_index_refresh(request, storage_id)
    return KnowledgeStaleScanResponse(
        kb_id=scope.external_id,
        stale_marked=len(stale),
        stale_knowledge=[_public(row, request) for row in stale],
    )


# 审核状态流转。
def _set_status(request: Request, knowledge_id: str, status: str, body):
    current = _knowledge_row_for_request(request, knowledge_id)
    if current is None:
        return _knowledge_not_found(knowledge_id)
    binding_updates = {
        key: value
        for key, value in {
            "related_document_id": body.related_document_id,
            "related_source": body.related_source,
            "related_source_sha256": body.related_source_sha256,
            "related_chunk_ids": body.related_chunk_ids,
            "related_page_start": body.related_page_start,
            "related_page_end": body.related_page_end,
            "related_chunk_text_hash": body.related_chunk_text_hash,
            "related_anchor_text": body.related_anchor_text,
        }.items()
        if value is not None
    }
    storage_id = str(current.get("kb_id") or "")
    scope = scope_for_storage_id(request, storage_id)
    access_store = getattr(request.app.state, "resource_access_store", None)
    if access_store is not None and (
        scope is None
        or not row_is_authorized(request, scope, {**current, **binding_updates})
    ):
        return _knowledge_not_found(knowledge_id)
    try:
        row = request.app.state.knowledge_store.set_status(
            knowledge_id,
            status,
            actor=request_principal(request).subject_id,
            note=body.note,
            binding_updates=binding_updates,
        )
    except ValueError as exc:
        return _error(ErrorCode.BAD_REQUEST, str(exc), 400)
    if row is None:
        return _knowledge_not_found(knowledge_id)
    _queue_derived_knowledge_index_refresh(request, row.get("kb_id"))
    return _public(row, request)


# 审核通过知识。
@router.post(
    "/knowledge/{knowledge_id}/approve",
    response_model=DerivedKnowledge,
    responses=_ERROR_RESPONSES,
)
async def approve_knowledge(
    knowledge_id: str, body: KnowledgeReviewRequest, request: Request
):
    return _set_status(request, knowledge_id, KnowledgeStatus.APPROVED.value, body)


# 驳回知识。
@router.post(
    "/knowledge/{knowledge_id}/reject",
    response_model=DerivedKnowledge,
    responses=_ERROR_RESPONSES,
)
async def reject_knowledge(
    knowledge_id: str, body: KnowledgeReviewRequest, request: Request
):
    return _set_status(request, knowledge_id, KnowledgeStatus.REJECTED.value, body)


# 归档知识。
@router.post(
    "/knowledge/{knowledge_id}/archive",
    response_model=DerivedKnowledge,
    responses=_ERROR_RESPONSES,
)
async def archive_knowledge(
    knowledge_id: str, body: KnowledgeReviewRequest, request: Request
):
    return _set_status(request, knowledge_id, KnowledgeStatus.ARCHIVED.value, body)


# 创建知识修订版本。
@router.post(
    "/knowledge/{knowledge_id}/revise",
    status_code=201,
    response_model=KnowledgeCreateResponse,
    responses=_ERROR_RESPONSES,
)
async def revise_knowledge(
    knowledge_id: str, body: KnowledgeReviseRequest, request: Request
):
    current = _knowledge_row_for_request(request, knowledge_id)
    if current is None:
        return _knowledge_not_found(knowledge_id)
    payload = body.model_dump(exclude_none=True)
    payload["created_by"] = request_principal(request).subject_id
    payload["status"] = KnowledgeStatus.PENDING.value
    storage_id = str(current.get("kb_id") or "")
    scope = scope_for_storage_id(request, storage_id)
    access_store = getattr(request.app.state, "resource_access_store", None)
    if access_store is not None and (
        scope is None or not row_is_authorized(request, scope, {**current, **payload})
    ):
        return _knowledge_not_found(knowledge_id)
    try:
        row = request.app.state.knowledge_store.revise(knowledge_id, payload)
    except ValueError as exc:
        return _error(ErrorCode.BAD_REQUEST, str(exc), 400)
    if row is None:
        return _knowledge_not_found(knowledge_id)
    notify_pending_created(request.app, row, "knowledge_revision")
    return KnowledgeCreateResponse(
        knowledge=_public(row, request),
        deduplicated=False,
    )


# 批量审核。
def _batch_set_status(request: Request, body: KnowledgeBatchReviewRequest, status: str):
    owned_ids = []
    unavailable_ids = []
    for knowledge_id in body.knowledge_ids:
        if _knowledge_row_for_request(request, knowledge_id) is None:
            unavailable_ids.append(knowledge_id)
        else:
            owned_ids.append(knowledge_id)
    try:
        updated, missing = request.app.state.knowledge_store.batch_set_status(
            owned_ids,
            status,
            actor=request_principal(request).subject_id,
            note=body.note,
        )
    except ValueError as exc:
        return _error(ErrorCode.BAD_REQUEST, str(exc), 400)
    unavailable = set(unavailable_ids) | set(missing)
    missing = [
        knowledge_id
        for knowledge_id in body.knowledge_ids
        if knowledge_id in unavailable
    ]
    for kb_id in {str(row.get("kb_id") or "") for row in updated if row.get("kb_id")}:
        _queue_derived_knowledge_index_refresh(request, kb_id)
    return KnowledgeBatchReviewResponse(
        updated=[_public(row, request) for row in updated],
        missing_ids=missing,
    )


# 批量审核通过。
@router.post(
    "/knowledge/batch-approve",
    response_model=KnowledgeBatchReviewResponse,
    responses=_ERROR_RESPONSES,
)
async def batch_approve_knowledge(body: KnowledgeBatchReviewRequest, request: Request):
    return _batch_set_status(request, body, KnowledgeStatus.APPROVED.value)


# 批量驳回。
@router.post(
    "/knowledge/batch-reject",
    response_model=KnowledgeBatchReviewResponse,
    responses=_ERROR_RESPONSES,
)
async def batch_reject_knowledge(body: KnowledgeBatchReviewRequest, request: Request):
    return _batch_set_status(request, body, KnowledgeStatus.REJECTED.value)
