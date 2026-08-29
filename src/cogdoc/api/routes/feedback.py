import json
import logging
import math
import re
from collections.abc import Mapping

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from cogdoc.agents.feedback_understanding import (
    FeedbackAnalysis,
    analyze_feedback,
    feedback_target_items,
)
from cogdoc.api.schemas import (
    FeedbackIssueType,
    FeedbackListResponse,
    FeedbackRequest,
    FeedbackResponse,
    FeedbackType,
)
from cogdoc.api.ha_chat_authority import (
    HAChatAuthorityChanged,
    capture_ha_chat_epoch,
    ha_authority_guard,
)
from cogdoc.api.tenant_scope import (
    externalize_kb_fields,
    request_principal,
    resolve_kb_scope,
    row_is_authorized,
    scope_for_storage_id,
)
from cogdoc.api.webhooks import notify_pending_created
from cogdoc.api.tenancy import Permission
from cogdoc.observability.logger import log_event
from cogdoc.observability.trace import trace_path
from cogdoc.ha.feedback import HA_KB_EPOCH_FIELD, StaleAuxiliaryWrite
from cogdoc.tools.eval.retrieval_eval_drafts import build_retrieval_eval_draft

router = APIRouter(prefix="/v1", tags=["feedback"])
_TRACE_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")
_CLIENT_ATTRIBUTION_FIELDS = (
    "related_document_id",
    "related_source",
    "related_source_sha256",
    "related_chunk_ids",
    "related_page_start",
    "related_page_end",
    "related_chunk_text_hash",
    "related_anchor_text",
)


def _kb_storage_id(request: Request, kb_id: str) -> str:
    scope = resolve_kb_scope(request, kb_id, allow_legacy_default=True)
    if scope is None:
        raise HTTPException(status_code=404, detail="知识库不存在")
    return scope.storage_id


def _ha_feedback_authority(
    request: Request, storage_id: str | None, permission: Permission
) -> tuple[int, Mapping[str, object]] | None:
    if not storage_id or not getattr(
        request.app.state, "ha_feedback_multiwriter_mode", False
    ):
        return None
    scope = scope_for_storage_id(request, storage_id)
    if scope is None:
        raise HAChatAuthorityChanged("shared feedback scope is unavailable")
    expected_epoch = capture_ha_chat_epoch(request.app.state.kb_registry, storage_id)
    guard = ha_authority_guard(
        request,
        scope,
        expected_epoch,
        permission=permission,
    )
    evidence = getattr(guard, "evidence", None)
    if not isinstance(evidence, Mapping):
        raise HAChatAuthorityChanged("shared feedback authority is unavailable")
    return expected_epoch, evidence


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
    # Records predating the tenant registry belong to the historical default
    # workspace. A registered foreign record must never take this fallback.
    return not callable(getter) or getter(value) is None


def _feedback_record(request: Request, feedback_id: str) -> Mapping | None:
    rows = request.app.state.feedback_store.export_records()
    return next(
        (row for row in rows if str(row.get("feedback_id") or "") == feedback_id),
        None,
    )


def _feedback_records_by_id(request: Request) -> dict[str, Mapping]:
    return {
        str(row.get("feedback_id") or ""): row
        for row in request.app.state.feedback_store.export_records()
        if str(row.get("feedback_id") or "")
    }


def _row_allowed(request: Request, row: Mapping | None) -> bool:
    if not isinstance(row, Mapping):
        return False
    storage_id = str(row.get("kb_id") or "")
    scope = scope_for_storage_id(request, storage_id) if storage_id else None
    if scope is not None:
        return row_is_authorized(request, scope, row)
    return _owns_storage_id(request, storage_id)


def _retrieval_feedback_record(request: Request, feedback_id: str) -> Mapping | None:
    rows = request.app.state.retrieval_feedback_store.export_records()
    return next(
        (
            row
            for row in rows
            if str(row.get("retrieval_feedback_id") or "") == feedback_id
        ),
        None,
    )


def _require_owned_retrieval_feedback(request: Request, feedback_id: str) -> Mapping:
    row = _retrieval_feedback_record(request, feedback_id)
    source_row = (
        _feedback_record(request, str(row.get("feedback_id") or ""))
        if isinstance(row, Mapping)
        else None
    )
    if row is None or not _row_allowed(request, source_row or row):
        raise HTTPException(status_code=404, detail="检索反馈不存在")
    return row


def _trusted_trace(payload: Mapping[str, object]) -> Mapping[str, object] | None:
    trace_id = str(payload.get("trace_id") or "")
    if not _TRACE_ID_RE.fullmatch(trace_id):
        return None
    path = trace_path(trace_id)
    if not path.exists() or not path.is_file():
        return None
    try:
        trace = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(trace, Mapping) or str(trace.get("trace_id") or "") != trace_id:
        return None
    config = trace.get("config")
    config = config if isinstance(config, Mapping) else {}
    requested_kb = str(payload.get("kb_id") or "")
    traced_kb = str(config.get("doc_id") or "")
    # A trace may only become an attribution authority for the exact requested KB.
    # Missing legacy scope is not enough to authenticate a cross-record reference.
    if requested_kb and traced_kb != requested_kb:
        return None
    output = trace.get("output")
    if not isinstance(output, Mapping):
        return None
    return trace


def _hydrate_feedback_from_trace(
    payload: dict,
    trace: Mapping[str, object] | None = None,
) -> bool:
    trusted_trace = trace or _trusted_trace(payload)
    if trusted_trace is None:
        return False
    output = trusted_trace.get("output")
    if not isinstance(output, Mapping):
        return False
    raw_input = trusted_trace.get("input")
    trace_input = raw_input if isinstance(raw_input, Mapping) else {}
    query = trace_input.get("query")
    if isinstance(query, str) and query.strip():
        payload["query"] = query
    else:
        payload.pop("query", None)
    answer = output.get("answer")
    if isinstance(answer, str):
        payload["answer"] = answer
    else:
        payload.pop("answer", None)
    evidence = output.get("evidence")
    payload["evidence"] = evidence if isinstance(evidence, list) else []
    internal_ledger = output.get("evidence_ledger")
    payload["evidence_ledger"] = (
        internal_ledger if isinstance(internal_ledger, list) else []
    )
    if "citation_ledger" in output:
        # 保留畸形类型，让下游整表校验失败关闭；不能静默删除后回退宽闭集。
        payload["citation_ledger"] = output.get("citation_ledger")
    else:
        # 旧 trace 没有该字段，继续使用同一 trace 内的 legacy 来源。
        payload.pop("citation_ledger", None)
    sources = output.get("sources")
    payload["citations"] = sources if isinstance(sources, list) else []
    # trace 命中后的归因是一个原子快照；客户端手填的关联字段
    # 不得与服务端 answer/evidence/ledger 拼接。
    for field in _CLIENT_ATTRIBUTION_FIELDS:
        payload.pop(field, None)
    return True


def _eligible_retrieval_eval_trace(trace: Mapping[str, object]) -> bool:
    """Only complete successful server traces may seed an annotation proposal."""

    raw_completeness = trace.get("evidence_completeness")
    if isinstance(raw_completeness, bool) or not isinstance(
        raw_completeness, str | int | float
    ):
        return False
    try:
        completeness = float(raw_completeness)
    except (TypeError, ValueError, OverflowError):
        return False
    return (
        trace.get("execution_status") == "SUCCESS"
        and math.isfinite(completeness)
        and completeness >= 1.0
        and trace.get("task_type") in {"qa", "summary", "compare"}
        and isinstance(trace.get("input"), Mapping)
        and isinstance(trace.get("output"), Mapping)
    )


def _create_retrieval_eval_draft_quiet(
    request: Request,
    feedback_id: str,
    payload: Mapping[str, object],
    trace: Mapping[str, object] | None,
    authority: tuple[int, Mapping[str, object]] | None = None,
) -> tuple[str | None, str | None]:
    store = getattr(request.app.state, "retrieval_eval_draft_store", None)
    if store is None or trace is None or not _eligible_retrieval_eval_trace(trace):
        return None, None
    try:
        draft = build_retrieval_eval_draft(
            {**payload, "feedback_id": feedback_id},
            trace,
        )
        ensure_authorized = getattr(store, "ensure_authorized", None)
        row = (
            ensure_authorized(
                draft,
                expected_epoch=authority[0],
                authority=authority[1],
            )
            if callable(ensure_authorized) and authority is not None
            else store.ensure(draft)
        )
        return str(row["draft_id"]), str(row["status"])
    except Exception as exc:
        # Feedback recording is the primary user action. A curation-pipeline
        # failure is observable but must not turn a recorded thumbs-down into 5xx.
        log_event(
            "feedback",
            "retrieval_eval_draft_create_failed",
            {},
            level=logging.WARNING,
            feedback_id=feedback_id,
            error_class=type(exc).__name__,
        )
        return None, None


def _attributed_chunks(
    body: FeedbackRequest,
    payload: Mapping[str, object],
    *,
    trusted_trace: bool = False,
) -> tuple[list[str], str | None]:
    # 非空 ledger 的归因必须来自服务端 trace 中的精确 occurrence；显式的
    # related_chunk_ids 也不能越过该闭集。空 ledger 保留旧版手动关联行为。
    if trusted_trace or "citation_ledger" in payload:
        targets = feedback_target_items(payload)
        return (
            [item["chunk_id"] for item in targets],
            next((item["source"] for item in targets if item["source"]), None),
        )
    return (
        body.related_chunk_ids
        or [item.chunk_id for item in body.citations if item.chunk_id],
        body.related_source
        or next((item.source for item in body.citations if item.source), None),
    )


# 构建纠错派生知识草稿。
def _knowledge_payload(
    body: FeedbackRequest,
    payload: Mapping[str, object] | None = None,
    *,
    trusted_trace: bool = False,
) -> dict | None:
    correction = body.correction_text or body.correction
    if not correction or not body.kb_id:
        return None
    related_chunk_ids, related_source = _attributed_chunks(
        body,
        payload or body.model_dump(exclude_none=True),
        trusted_trace=trusted_trace,
    )
    return {
        "kb_id": body.kb_id,
        "text": correction,
        "related_document_id": (None if trusted_trace else body.related_document_id),
        "related_source": related_source,
        "related_source_sha256": (
            None if trusted_trace else body.related_source_sha256
        ),
        "related_chunk_ids": related_chunk_ids,
        "related_page_start": None if trusted_trace else body.related_page_start,
        "related_page_end": None if trusted_trace else body.related_page_end,
        "related_chunk_text_hash": (
            None if trusted_trace else body.related_chunk_text_hash
        ),
        "related_anchor_text": (None if trusted_trace else body.related_anchor_text),
        "source_note": body.source_note or body.feedback_text or body.comment,
        "certainty": body.certainty,
        "status": "pending",
        "origin": (
            "no_evidence"
            if body.feedback_type == FeedbackIssueType.NO_EVIDENCE
            else "correction"
        ),
        "created_from_trace_id": body.trace_id,
        "created_by": body.created_by,
    }


# 构建反馈理解建议的知识草稿。
def _analysis_knowledge_payload(
    body: FeedbackRequest,
    analysis: FeedbackAnalysis,
    confidence: float,
    payload: Mapping[str, object] | None = None,
    *,
    trusted_trace: bool = False,
) -> dict | None:
    extracted_claim = analysis.get("extracted_claim")
    correction = extracted_claim.strip() if isinstance(extracted_claim, str) else ""
    if (
        not body.kb_id
        or not correction
        or analysis.get("recommended_action") != "create_pending_knowledge"
        or confidence < 0.8
    ):
        return None
    related_chunk_ids, related_source = _attributed_chunks(
        body,
        payload or body.model_dump(exclude_none=True),
        trusted_trace=trusted_trace,
    )
    return {
        "kb_id": body.kb_id,
        "text": correction,
        "related_document_id": (None if trusted_trace else body.related_document_id),
        "related_source": related_source,
        "related_source_sha256": (
            None if trusted_trace else body.related_source_sha256
        ),
        "related_chunk_ids": related_chunk_ids,
        "related_page_start": None if trusted_trace else body.related_page_start,
        "related_page_end": None if trusted_trace else body.related_page_end,
        "related_chunk_text_hash": (
            None if trusted_trace else body.related_chunk_text_hash
        ),
        "related_anchor_text": (None if trusted_trace else body.related_anchor_text),
        "source_note": body.source_note or body.feedback_text or body.comment,
        "certainty": "high" if confidence >= 0.9 else body.certainty,
        "status": "pending",
        "origin": "agent_suggested",
        "created_from_trace_id": body.trace_id,
        "created_by": body.created_by,
    }


# 分析反馈，失败时降级为仅记录原始反馈。
def _analyze_feedback_quiet(feedback_id: str, payload: dict) -> FeedbackAnalysis | None:
    try:
        return analyze_feedback(payload)
    except Exception as exc:
        log_event(
            "feedback",
            "feedback_analysis_failed",
            {},
            level=logging.WARNING,
            feedback_id=feedback_id,
            error_class=type(exc).__name__,
        )
        return None


# 记录反馈理解结果，失败时不阻断反馈提交。
def _record_feedback_analysis_quiet(
    request: Request,
    feedback_id: str,
    payload: dict,
    analysis: FeedbackAnalysis,
    authority: tuple[int, Mapping[str, object]] | None = None,
) -> dict | None:
    try:
        store = request.app.state.feedback_analysis_store
        record_authorized = getattr(store, "record_authorized", None)
        return (
            record_authorized(
                feedback_id,
                payload,
                analysis,
                expected_epoch=authority[0],
                authority=authority[1],
            )
            if authority is not None and callable(record_authorized)
            else store.record(feedback_id, payload, analysis)
        )
    except Exception as exc:
        log_event(
            "feedback",
            "feedback_analysis_record_failed",
            {},
            level=logging.WARNING,
            feedback_id=feedback_id,
            error_class=type(exc).__name__,
        )
        return None


# 创建知识草稿，失败时不阻断反馈提交。
def _create_knowledge_quiet(
    request: Request,
    feedback_id: str,
    knowledge_payload: dict,
    authority: tuple[int, Mapping[str, object]] | None = None,
) -> tuple[str | None, str | None, bool]:
    try:
        store = request.app.state.knowledge_store
        create_authorized = getattr(store, "create_authorized", None)
        knowledge, deduplicated = (
            create_authorized(
                knowledge_payload,
                expected_epoch=authority[0],
                authority=authority[1],
            )
            if authority is not None and callable(create_authorized)
            else store.create(knowledge_payload)
        )
        if not deduplicated:
            notify_pending_created(request.app, knowledge, "feedback")
        return knowledge["knowledge_id"], knowledge["status"], deduplicated
    except Exception as exc:
        log_event(
            "feedback",
            "knowledge_create_failed",
            {},
            level=logging.WARNING,
            feedback_id=feedback_id,
            error_class=type(exc).__name__,
        )
        return None, None, False


# 提交反馈。
@router.post("/feedback", status_code=201)
async def submit_feedback(body: FeedbackRequest, request: Request):
    # 控制层只落盘，不做评判；坏样本归集逻辑在存储层里。
    principal = request_principal(request)
    if body.kb_id:
        storage_id = _kb_storage_id(request, body.kb_id)
    elif principal.tenant_id == "default":
        storage_id = None
    else:
        raise HTTPException(status_code=404, detail="知识库不存在")
    created_by = principal.subject_id
    body = body.model_copy(update={"kb_id": storage_id, "created_by": created_by})
    payload = body.model_dump(exclude_none=True)
    if storage_id is not None and getattr(
        request.app.state, "ha_feedback_multiwriter_mode", False
    ):
        current_epoch = request.app.state.kb_registry.current(storage_id)
        if type(current_epoch) is not int or current_epoch < 1:
            raise HTTPException(status_code=409, detail="知识库代际已变化")
        payload[HA_KB_EPOCH_FIELD] = current_epoch
    if body.feedback_text and not payload.get("comment"):
        payload["comment"] = body.feedback_text
    if body.correction_text and not payload.get("correction"):
        payload["correction"] = body.correction_text
    trace = _trusted_trace(payload)
    trusted_trace = _hydrate_feedback_from_trace(payload, trace)
    if storage_id is not None:
        scope = scope_for_storage_id(request, storage_id)
        access_store = getattr(request.app.state, "resource_access_store", None)
        if access_store is not None and (
            scope is None or not row_is_authorized(request, scope, payload)
        ):
            raise HTTPException(status_code=404, detail="文档不存在")
    try:
        authority = _ha_feedback_authority(request, storage_id, Permission.WRITE)
        feedback_store = request.app.state.feedback_store
        record_authorized = getattr(feedback_store, "record_authorized", None)
        result = (
            record_authorized(
                payload,
                expected_epoch=authority[0],
                authority=authority[1],
            )
            if authority is not None and callable(record_authorized)
            else feedback_store.record(payload)
        )
    except (HAChatAuthorityChanged, StaleAuxiliaryWrite) as exc:
        raise HTTPException(status_code=409, detail="知识库代际已变化") from exc
    if result.get("deduplicated"):
        existing = _feedback_record(request, str(result["feedback_id"]))
        if existing is None or not _row_allowed(request, existing):
            raise HTTPException(status_code=404, detail="反馈不存在")
    retrieval_eval_draft_id = None
    retrieval_eval_draft_status = None
    feedback_value = getattr(body.feedback, "value", body.feedback)
    feedback_type_value = getattr(body.feedback_type, "value", body.feedback_type)
    if (
        feedback_value == FeedbackType.THUMBS_DOWN.value
        and feedback_type_value == FeedbackIssueType.BAD_RETRIEVAL.value
    ):
        retrieval_eval_draft_id, retrieval_eval_draft_status = (
            _create_retrieval_eval_draft_quiet(
                request,
                result["feedback_id"],
                payload,
                trace,
                authority,
            )
        )
    if not body.skip_retrieval_feedback:
        retrieval_store = request.app.state.retrieval_feedback_store
        record_retrieval_authorized = getattr(
            retrieval_store, "record_from_feedback_authorized", None
        )
        already_projected = result.get("deduplicated") and any(
            str(row.get("feedback_id") or "") == str(result["feedback_id"])
            for row in retrieval_store.export_records()
        )
        try:
            if already_projected:
                pass
            elif authority is not None and callable(record_retrieval_authorized):
                record_retrieval_authorized(
                    result["feedback_id"],
                    payload,
                    expected_epoch=authority[0],
                    authority=authority[1],
                )
            else:
                retrieval_store.record_from_feedback(result["feedback_id"], payload)
        except (HAChatAuthorityChanged, StaleAuxiliaryWrite) as exc:
            # The primary feedback row may already be durable.  Surface a
            # retryable conflict instead of reporting complete success; the
            # retry follows the deduplicated path and repairs the idempotent
            # retrieval-feedback projection.
            raise HTTPException(status_code=409, detail="知识库代际已变化") from exc
    if result.get("deduplicated"):
        return FeedbackResponse(
            feedback_id=result["feedback_id"],
            status="duplicate_ignored",
            is_bad_case=result["is_bad_case"],
            retrieval_eval_draft_id=retrieval_eval_draft_id,
            retrieval_eval_draft_status=retrieval_eval_draft_status,
        )
    analysis = _analyze_feedback_quiet(result["feedback_id"], payload)
    analysis_row = None
    if analysis is not None:
        analysis_row = _record_feedback_analysis_quiet(
            request, result["feedback_id"], payload, analysis, authority
        )
    knowledge_id = None
    knowledge_status = None
    knowledge_deduplicated = False
    knowledge_payload = _knowledge_payload(body, payload, trusted_trace=trusted_trace)
    if knowledge_payload is None and analysis is not None:
        knowledge_payload = _analysis_knowledge_payload(
            body,
            analysis,
            float(analysis.get("confidence") or 0.0),
            payload,
            trusted_trace=trusted_trace,
        )
    if knowledge_payload is not None:
        if HA_KB_EPOCH_FIELD in payload:
            knowledge_payload[HA_KB_EPOCH_FIELD] = payload[HA_KB_EPOCH_FIELD]
        knowledge_id, knowledge_status, knowledge_deduplicated = (
            _create_knowledge_quiet(
                request,
                result["feedback_id"],
                knowledge_payload,
                authority,
            )
        )
    return FeedbackResponse(
        feedback_id=result["feedback_id"],
        is_bad_case=result["is_bad_case"],
        feedback_analysis_id=(
            analysis_row["feedback_analysis_id"] if analysis_row else None
        ),
        feedback_analysis_action=(
            analysis_row["recommended_action"] if analysis_row else None
        ),
        feedback_analysis_confidence=(
            analysis_row["confidence"] if analysis_row else None
        ),
        knowledge_id=knowledge_id,
        knowledge_status=knowledge_status,
        knowledge_deduplicated=knowledge_deduplicated,
        retrieval_eval_draft_id=retrieval_eval_draft_id,
        retrieval_eval_draft_status=retrieval_eval_draft_status,
    )


# 查询反馈记录。
@router.get("/feedback", response_model=FeedbackListResponse)
async def list_feedback(
    request: Request,
    kb_id: str = Query(min_length=1),
    trace_id: str | None = None,
    session_id: str | None = None,
    feedback: FeedbackType | None = None,
    feedback_type: FeedbackIssueType | None = None,
    is_bad_case: bool | None = None,
    limit: int = Query(default=100, ge=1, le=500),
):
    storage_id = _kb_storage_id(request, kb_id)
    rows = request.app.state.feedback_store.list(
        kb_id=storage_id,
        trace_id=trace_id,
        session_id=session_id,
        feedback=feedback.value if feedback is not None else None,
        feedback_type=feedback_type.value if feedback_type is not None else None,
        is_bad_case=is_bad_case,
        limit=limit,
    )
    rows = [row for row in rows if _row_allowed(request, row)]
    return FeedbackListResponse(feedback=externalize_kb_fields(rows, request))


# 查询反馈理解结果。
@router.get("/feedback-analysis")
async def list_feedback_analysis(
    request: Request,
    kb_id: str = Query(min_length=1),
    feedback_id: str | None = None,
    trace_id: str | None = None,
    recommended_action: str | None = None,
    needs_review: bool | None = None,
    min_confidence: float | None = Query(default=None, ge=0, le=1),
    limit: int = Query(default=100, ge=1, le=500),
):
    storage_id = _kb_storage_id(request, kb_id)
    if feedback_id is not None:
        feedback_row = _feedback_record(request, feedback_id)
        if (
            feedback_row is None
            or feedback_row.get("kb_id") != storage_id
            or not _row_allowed(request, feedback_row)
        ):
            raise HTTPException(status_code=404, detail="反馈不存在")
    rows = request.app.state.feedback_analysis_store.list(
        kb_id=storage_id,
        feedback_id=feedback_id,
        trace_id=trace_id,
        recommended_action=recommended_action,
        needs_review=needs_review,
        min_confidence=min_confidence,
        limit=limit,
    )
    feedback_by_id = _feedback_records_by_id(request)
    rows = [
        row
        for row in rows
        if _row_allowed(
            request,
            feedback_by_id.get(str(row.get("feedback_id") or "")) or row,
        )
    ]
    return {"feedback_analysis": externalize_kb_fields(rows, request)}


# 查询检索反馈。
@router.get("/retrieval-feedback")
async def list_retrieval_feedback(
    request: Request,
    kb_id: str = Query(min_length=1),
    enabled: bool | None = None,
    limit: int = Query(default=100, ge=1, le=500),
):
    storage_id = _kb_storage_id(request, kb_id)
    rows = request.app.state.retrieval_feedback_store.list(
        kb_id=storage_id, enabled=enabled, limit=limit
    )
    feedback_by_id = _feedback_records_by_id(request)
    rows = [
        row
        for row in rows
        if _row_allowed(
            request,
            feedback_by_id.get(str(row.get("feedback_id") or "")) or row,
        )
    ]
    return {"retrieval_feedback": externalize_kb_fields(rows, request)}


# 禁用检索反馈。
@router.post("/retrieval-feedback/{feedback_id}/disable")
async def disable_retrieval_feedback(feedback_id: str, body: dict, request: Request):
    current = _require_owned_retrieval_feedback(request, feedback_id)
    principal = request_principal(request)
    actor = principal.subject_id
    try:
        authority = _ha_feedback_authority(
            request, str(current.get("kb_id") or ""), Permission.WRITE
        )
        store = request.app.state.retrieval_feedback_store
        set_authorized = getattr(store, "set_enabled_authorized", None)
        row = (
            set_authorized(
                feedback_id,
                False,
                actor=actor,
                reason=body.get("reason"),
                expected_epoch=authority[0],
                authority=authority[1],
            )
            if authority is not None and callable(set_authorized)
            else store.set_enabled(
                feedback_id,
                False,
                actor=actor,
                reason=body.get("reason"),
            )
        )
    except (HAChatAuthorityChanged, StaleAuxiliaryWrite):
        return JSONResponse(status_code=409, content={"message": "访问权限已变化"})
    if row is None:
        return JSONResponse(status_code=404, content={"message": "检索反馈不存在"})
    return {"status": "disabled", "retrieval_feedback_id": feedback_id}


# 启用检索反馈。
@router.post("/retrieval-feedback/{feedback_id}/enable")
async def enable_retrieval_feedback(feedback_id: str, request: Request):
    current = _require_owned_retrieval_feedback(request, feedback_id)
    try:
        authority = _ha_feedback_authority(
            request, str(current.get("kb_id") or ""), Permission.WRITE
        )
        store = request.app.state.retrieval_feedback_store
        set_authorized = getattr(store, "set_enabled_authorized", None)
        row = (
            set_authorized(
                feedback_id,
                True,
                expected_epoch=authority[0],
                authority=authority[1],
            )
            if authority is not None and callable(set_authorized)
            else store.set_enabled(feedback_id, True)
        )
    except (HAChatAuthorityChanged, StaleAuxiliaryWrite):
        return JSONResponse(status_code=409, content={"message": "访问权限已变化"})
    if row is None:
        return JSONResponse(status_code=404, content={"message": "检索反馈不存在"})
    return {"status": "enabled", "retrieval_feedback_id": feedback_id}
