from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from cogdoc.api.offload import run_sync
from cogdoc.api.schemas import (
    ClaimVerificationObservationSummaryResponse,
    ErrorCode,
    build_error_response,
)
from cogdoc.api.tenant_scope import request_principal
from cogdoc.config.settings import get_settings
from cogdoc.service.claim_verification_policy import (
    resolve_claim_verification_policy,
)


router = APIRouter(prefix="/v1/claim-verification", tags=["claim-verification"])


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
