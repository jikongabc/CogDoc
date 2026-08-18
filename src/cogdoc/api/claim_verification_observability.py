from __future__ import annotations

import logging
from typing import Any

from fastapi import Request

from cogdoc.api.tenant_scope import request_principal
from cogdoc.observability.logger import log_event


def record_claim_verification_observation(request: Request, result: Any) -> bool:
    """Persist one privacy-minimized rollout event without affecting delivery."""

    raw_output = getattr(result, "raw_output", None)
    if not isinstance(raw_output, dict):
        return False
    rollout = raw_output.get("claim_verification_rollout")
    if not isinstance(rollout, dict):
        return False
    store = getattr(request.app.state, "claim_verification_observation_store", None)
    if store is None:
        return False
    try:
        return bool(
            store.record(
                request_principal(request).tenant_id,
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
        return False
