from __future__ import annotations

import logging
from typing import Any

from fastapi import Request

from cogdoc.api.tenant_scope import request_principal
from cogdoc.config.settings import get_settings
from cogdoc.observability.logger import log_event
from cogdoc.service.claim_verification_review import (
    build_claim_review_candidates,
)


def record_claim_verification_observation(
    request: Request, result: Any, *, kb_id: str
) -> bool:
    """Persist one privacy-minimized rollout event without affecting delivery."""

    raw_output = getattr(result, "raw_output", None)
    if not isinstance(raw_output, dict):
        return False
    rollout = raw_output.get("claim_verification_rollout")
    if not isinstance(rollout, dict):
        return False
    try:
        principal = request_principal(request)
    except Exception:
        return False
    observation_recorded = False
    store = getattr(request.app.state, "claim_verification_observation_store", None)
    if store is not None:
        try:
            observation_recorded = bool(
                store.record(
                    principal.tenant_id,
                    str(getattr(result, "task_type", "") or ""),
                    rollout,
                )
            )
        except Exception as exc:
            try:
                log_event(
                    "claim_verification",
                    "claim_verification_observation_failed",
                    {"trace_id": str(getattr(result, "trace_id", "") or "")},
                    level=logging.ERROR,
                    error_class=type(exc).__name__,
                )
            except Exception:
                pass
    review_store = getattr(
        request.app.state, "claim_verification_review_store", None
    )
    if review_store is None:
        return observation_recorded
    try:
        settings = get_settings()
        candidates = build_claim_review_candidates(
            result,
            tenant_id=principal.tenant_id,
            kb_id=kb_id,
            sample_percent=(
                settings.claim_verification_review_sample_percent
            ),
            sample_seed=settings.claim_verification_review_sample_seed,
            max_claims=(
                settings.claim_verification_review_max_claims_per_response
            ),
            max_evidence_per_claim=(
                settings.claim_verification_review_max_evidence_per_claim
            ),
            max_chars_per_evidence=(
                settings.claim_verification_review_max_chars_per_evidence
            ),
        )
        if candidates:
            review_store.record_candidates(principal.tenant_id, candidates)
    except Exception as exc:
        try:
            log_event(
                "claim_verification",
                "claim_verification_review_sampling_failed",
                {"trace_id": str(getattr(result, "trace_id", "") or "")},
                level=logging.ERROR,
                error_class=type(exc).__name__,
            )
        except Exception:
            pass
    return observation_recorded
