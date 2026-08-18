from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from fastapi import APIRouter, Path, Query, Request
from fastapi.responses import JSONResponse

from cogdoc.api.claim_verification_review_store import (
    ClaimReviewRevisionConflictError,
)
from cogdoc.api.offload import run_sync
from cogdoc.api.schemas import (
    ClaimVerificationObservationSummaryResponse,
    ClaimVerificationReviewDetail,
    ClaimVerificationReviewExportResponse,
    ClaimVerificationReviewLabelRequest,
    ClaimVerificationReviewListResponse,
    ClaimVerificationReviewSummary,
    ClaimVerificationReviewSummaryResponse,
    ErrorCode,
    build_error_response,
)
from cogdoc.api.tenant_scope import (
    request_principal,
    retrieval_scope_for_request,
    scope_for_storage_id,
)
from cogdoc.config.settings import get_settings
from cogdoc.service.claim_verification_policy import (
    resolve_claim_verification_policy,
)


router = APIRouter(prefix="/v1/claim-verification", tags=["claim-verification"])


def _review_store(request: Request):
    return getattr(request.app.state, "claim_verification_review_store", None)


def _review_summary(row: dict) -> dict:
    return {
        key: value
        for key, value in {
            **row,
            "evidence_count": len(row.get("evidence") or []),
        }.items()
        if key
        not in {
            "_cursor",
            "kb_id",
            "evidence",
            "cited_chunk_ids",
            "supporting_chunk_ids",
        }
    }


def _review_detail(row: dict) -> dict:
    evidence = [
        {
            key: value
            for key, value in item.items()
            if key != "authorization_source"
        }
        for item in row.get("evidence") or []
        if isinstance(item, Mapping)
    ]
    return {
        **{
            key: value
            for key, value in row.items()
            if key not in {"_cursor", "kb_id"}
        },
        "evidence": evidence,
        "evidence_count": len(evidence),
    }


def _review_is_authorized(
    request: Request,
    row: Mapping[str, object],
    *,
    scope_cache: dict[str, Any | None] | None = None,
) -> bool:
    kb_id = str(row.get("kb_id") or "")
    cache = scope_cache if scope_cache is not None else {}
    if kb_id not in cache:
        scope = scope_for_storage_id(request, kb_id) if kb_id else None
        cache[kb_id] = (
            retrieval_scope_for_request(request, scope)
            if scope is not None
            else None
        )
    retrieval_scope = cache[kb_id]
    if retrieval_scope is None:
        return False
    if retrieval_scope.denies_all:
        return False
    evidence = row.get("evidence")
    if not isinstance(evidence, list):
        return False
    for item in evidence:
        if not isinstance(item, Mapping):
            return False
        source = str(
            item.get("authorization_source") or item.get("source") or ""
        )
        if not source or not retrieval_scope.allows_source(source):
            return False
    return True


def _review_summary_is_authorized(
    request: Request,
    row: Mapping[str, object],
    *,
    scope_cache: dict[str, Any | None],
) -> bool:
    kb_id = str(row.get("kb_id") or "")
    if kb_id not in scope_cache:
        scope = scope_for_storage_id(request, kb_id) if kb_id else None
        scope_cache[kb_id] = (
            retrieval_scope_for_request(request, scope)
            if scope is not None
            else None
        )
    retrieval_scope = scope_cache[kb_id]
    if retrieval_scope is None or retrieval_scope.denies_all:
        return False
    sources = row.get("authorization_sources")
    if not isinstance(sources, list):
        return False
    return all(
        isinstance(source, str)
        and bool(source)
        and retrieval_scope.allows_source(source)
        for source in sources
    )


async def _authorized_review_page(
    request: Request,
    store,
    tenant_id: str,
    *,
    status: str | None,
    limit: int,
    cursor: str | None,
) -> dict:
    authorized: list[dict] = []
    scan_cursor = cursor
    scope_cache: dict[str, Any | None] = {}
    while len(authorized) <= limit:
        page = await run_sync(
            request.app.state.offload_executor,
            store.list_page,
            tenant_id,
            status=status,
            limit=200,
            cursor=scan_cursor,
        )
        for row in page["items"]:
            if _review_is_authorized(
                request, row, scope_cache=scope_cache
            ):
                authorized.append(row)
                if len(authorized) > limit:
                    break
        if len(authorized) > limit or page["next_cursor"] is None:
            break
        scan_cursor = page["next_cursor"]
    next_cursor = (
        str(authorized[limit - 1]["_cursor"])
        if len(authorized) > limit
        else None
    )
    return {"items": authorized[:limit], "next_cursor": next_cursor}


async def _authorized_review_summary_buckets(
    request: Request, store, tenant_id: str
) -> list[dict]:
    buckets = await run_sync(
        request.app.state.offload_executor,
        store.summary_buckets,
        tenant_id,
    )
    scope_cache: dict[str, Any | None] = {}
    return [
        bucket
        for bucket in buckets
        if _review_summary_is_authorized(
            request, bucket, scope_cache=scope_cache
        )
    ]


def _review_queue_summary(tenant_id: str, buckets: list[dict]) -> dict:
    verdicts = ("supported", "unsupported", "insufficient", "not_factual")
    actual_counts = {verdict: 0 for verdict in verdicts}
    expected_counts = {verdict: 0 for verdict in verdicts}
    total = 0
    pending = reviewed = shadow = enforce = incomplete = 0
    agreement = disagreement = 0
    oldest_pending_at: str | None = None
    for bucket in buckets:
        total += int(bucket.get("total_count") or 0)
        pending += int(bucket.get("pending_count") or 0)
        reviewed += int(bucket.get("reviewed_count") or 0)
        shadow += int(bucket.get("shadow_count") or 0)
        enforce += int(bucket.get("enforce_count") or 0)
        incomplete += int(bucket.get("evidence_incomplete_count") or 0)
        agreement += int(bucket.get("agreement_count") or 0)
        disagreement += int(bucket.get("disagreement_count") or 0)
        bucket_oldest = str(bucket.get("oldest_pending_at") or "")
        if bucket_oldest and (
            oldest_pending_at is None or bucket_oldest < oldest_pending_at
        ):
            oldest_pending_at = bucket_oldest
        bucket_actual = bucket.get("actual_verdict_counts")
        bucket_expected = bucket.get("expected_verdict_counts")
        for verdict in verdicts:
            if isinstance(bucket_actual, Mapping):
                actual_counts[verdict] += int(bucket_actual.get(verdict) or 0)
            if isinstance(bucket_expected, Mapping):
                expected_counts[verdict] += int(
                    bucket_expected.get(verdict) or 0
                )
    labeled = agreement + disagreement
    return {
        "tenant_id": tenant_id,
        "total_count": total,
        "pending_count": pending,
        "reviewed_count": reviewed,
        "shadow_count": shadow,
        "enforce_count": enforce,
        "evidence_incomplete_count": incomplete,
        "agreement_count": agreement,
        "disagreement_count": disagreement,
        "agreement_rate": agreement / labeled if labeled else None,
        "oldest_pending_at": oldest_pending_at,
        "actual_verdict_counts": actual_counts,
        "expected_verdict_counts": expected_counts,
    }


@router.get(
    "/observations/summary",
    response_model=ClaimVerificationObservationSummaryResponse,
)
async def claim_verification_observation_summary(
    request: Request,
    window_hours: int = Query(default=168, ge=1, le=720),
    effective_mode: Literal["off", "shadow", "enforce"] | None = Query(
        default=None
    ),
    policy_id: str | None = Query(
        default=None, pattern=r"^[0-9a-f]{16}$"
    ),
):
    store = getattr(
        request.app.state, "claim_verification_observation_store", None
    )
    if store is None:
        return JSONResponse(
            status_code=503,
            content=build_error_response(
                ErrorCode.INTERNAL_ERROR, "声明核验观测存储不可用"
            ).model_dump(),
        )
    settings = get_settings()
    resolved_policy_id = policy_id or resolve_claim_verification_policy(
        settings, cohort_key="observation-summary"
    ).policy_id
    try:
        summary = await run_sync(
            request.app.state.offload_executor,
            store.summary,
            request_principal(request).tenant_id,
            window_hours=window_hours,
            effective_mode=effective_mode,
            policy_id=resolved_policy_id,
            operational_min_samples=(
                settings.claim_verification_operational_min_samples
            ),
            operational_max_error_rate=(
                settings.claim_verification_operational_max_error_rate
            ),
        )
    except Exception:
        return JSONResponse(
            status_code=503,
            content=build_error_response(
                ErrorCode.INTERNAL_ERROR, "声明核验观测存储暂时不可用"
            ).model_dump(),
        )
    return ClaimVerificationObservationSummaryResponse(**summary)


@router.get(
    "/reviews/export",
    response_model=ClaimVerificationReviewExportResponse,
)
async def export_claim_verification_reviews(
    request: Request,
    limit: int = Query(default=200, ge=1, le=1000),
    cursor: str | None = Query(default=None, max_length=256),
):
    store = _review_store(request)
    if store is None:
        return JSONResponse(
            status_code=503,
            content=build_error_response(
                ErrorCode.INTERNAL_ERROR, "声明核验判卷存储不可用"
            ).model_dump(),
        )
    principal = request_principal(request)
    try:
        page = await _authorized_review_page(
            request,
            store,
            principal.tenant_id,
            status="reviewed",
            limit=limit,
            cursor=cursor,
        )
        items = await run_sync(
            request.app.state.offload_executor,
            store.export_reviewed,
            principal.tenant_id,
            review_ids={str(row["review_id"]) for row in page["items"]},
        )
    except ValueError:
        return JSONResponse(
            status_code=422,
            content=build_error_response(
                ErrorCode.BAD_REQUEST, "判卷导出分页游标无效"
            ).model_dump(),
        )
    except Exception:
        return JSONResponse(
            status_code=503,
            content=build_error_response(
                ErrorCode.INTERNAL_ERROR, "声明核验判卷导出暂时不可用"
            ).model_dump(),
        )
    return ClaimVerificationReviewExportResponse(
        tenant_id=principal.tenant_id,
        count=len(items),
        items=items,
        next_cursor=page["next_cursor"],
    )


@router.get("/reviews", response_model=ClaimVerificationReviewListResponse)
async def list_claim_verification_reviews(
    request: Request,
    status: Literal["pending", "reviewed"] | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None, max_length=256),
):
    store = _review_store(request)
    if store is None:
        return JSONResponse(
            status_code=503,
            content=build_error_response(
                ErrorCode.INTERNAL_ERROR, "声明核验判卷存储不可用"
            ).model_dump(),
        )
    principal = request_principal(request)
    try:
        page = await _authorized_review_page(
            request,
            store,
            principal.tenant_id,
            status=status,
            limit=limit,
            cursor=cursor,
        )
    except ValueError:
        return JSONResponse(
            status_code=422,
            content=build_error_response(
                ErrorCode.BAD_REQUEST, "判卷分页游标无效"
            ).model_dump(),
        )
    except Exception:
        return JSONResponse(
            status_code=503,
            content=build_error_response(
                ErrorCode.INTERNAL_ERROR, "声明核验判卷列表暂时不可用"
            ).model_dump(),
        )
    return ClaimVerificationReviewListResponse(
        tenant_id=principal.tenant_id,
        items=[
            ClaimVerificationReviewSummary(**_review_summary(row))
            for row in page["items"]
        ],
        next_cursor=page["next_cursor"],
    )


@router.get(
    "/reviews/summary",
    response_model=ClaimVerificationReviewSummaryResponse,
)
async def summarize_claim_verification_reviews(request: Request):
    store = _review_store(request)
    if store is None:
        return JSONResponse(
            status_code=503,
            content=build_error_response(
                ErrorCode.INTERNAL_ERROR, "声明核验判卷存储不可用"
            ).model_dump(),
        )
    principal = request_principal(request)
    try:
        buckets = await _authorized_review_summary_buckets(
            request, store, principal.tenant_id
        )
    except Exception:
        return JSONResponse(
            status_code=503,
            content=build_error_response(
                ErrorCode.INTERNAL_ERROR, "声明核验判卷汇总暂时不可用"
            ).model_dump(),
        )
    return ClaimVerificationReviewSummaryResponse(
        **_review_queue_summary(principal.tenant_id, buckets)
    )


@router.get(
    "/reviews/{review_id}",
    response_model=ClaimVerificationReviewDetail,
)
async def get_claim_verification_review(
    request: Request,
    review_id: str = Path(pattern=r"^[0-9a-f]{32}$"),
):
    store = _review_store(request)
    if store is None:
        return JSONResponse(
            status_code=503,
            content=build_error_response(
                ErrorCode.INTERNAL_ERROR, "声明核验判卷存储不可用"
            ).model_dump(),
        )
    try:
        row = await run_sync(
            request.app.state.offload_executor,
            store.get,
            request_principal(request).tenant_id,
            review_id,
        )
    except Exception:
        return JSONResponse(
            status_code=503,
            content=build_error_response(
                ErrorCode.INTERNAL_ERROR, "声明核验判卷详情暂时不可用"
            ).model_dump(),
        )
    if row is None:
        return JSONResponse(
            status_code=404,
            content=build_error_response(
                ErrorCode.CLAIM_REVIEW_NOT_FOUND, "声明核验判卷项不存在"
            ).model_dump(),
        )
    if not _review_is_authorized(request, row):
        return JSONResponse(
            status_code=404,
            content=build_error_response(
                ErrorCode.CLAIM_REVIEW_NOT_FOUND, "声明核验判卷项不存在"
            ).model_dump(),
        )
    return ClaimVerificationReviewDetail(**_review_detail(row))


@router.post(
    "/reviews/{review_id}/label",
    response_model=ClaimVerificationReviewDetail,
)
async def label_claim_verification_review(
    body: ClaimVerificationReviewLabelRequest,
    request: Request,
    review_id: str = Path(pattern=r"^[0-9a-f]{32}$"),
):
    store = _review_store(request)
    if store is None:
        return JSONResponse(
            status_code=503,
            content=build_error_response(
                ErrorCode.INTERNAL_ERROR, "声明核验判卷存储不可用"
            ).model_dump(),
        )
    principal = request_principal(request)
    try:
        existing = await run_sync(
            request.app.state.offload_executor,
            store.get,
            principal.tenant_id,
            review_id,
        )
        if existing is None or not _review_is_authorized(request, existing):
            raise KeyError(review_id)
        row = await run_sync(
            request.app.state.offload_executor,
            store.label,
            principal.tenant_id,
            review_id,
            expected_verdict=body.expected_verdict,
            reviewer=principal.subject_id,
            review_note=body.review_note,
            expected_revision=body.expected_revision,
        )
    except KeyError:
        return JSONResponse(
            status_code=404,
            content=build_error_response(
                ErrorCode.CLAIM_REVIEW_NOT_FOUND, "声明核验判卷项不存在"
            ).model_dump(),
        )
    except ClaimReviewRevisionConflictError:
        return JSONResponse(
            status_code=409,
            content=build_error_response(
                ErrorCode.CLAIM_REVIEW_REVISION_CONFLICT,
                "声明核验判卷项已被其他审核者更新",
            ).model_dump(),
        )
    except Exception:
        return JSONResponse(
            status_code=503,
            content=build_error_response(
                ErrorCode.INTERNAL_ERROR, "声明核验判卷写入暂时不可用"
            ).model_dump(),
        )
    return ClaimVerificationReviewDetail(**_review_detail(row))
