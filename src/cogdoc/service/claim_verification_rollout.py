from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from cogdoc.config.settings import resolve_claim_verification_mode
from cogdoc.service.claim_verification_policy import (
    claim_verification_policy_projection,
)


ROLLOUT_DECISIONS = frozenset(
    {
        "skipped",
        "allow",
        "allow_exempt",
        "repair",
        "block",
        "would_allow",
        "would_allow_exempt",
        "would_repair",
        "would_block",
    }
)


def _nonnegative_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def _audit_or_empty(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def build_claim_verification_rollout(
    state: Mapping[str, Any],
    *,
    mode: str | None = None,
    max_repair_attempts: int = 0,
) -> dict[str, Any]:
    """Project one audit into a bounded rollout decision without claim prose."""

    resolved_mode = resolve_claim_verification_mode(
        {"claim_verification_mode": mode}
        if mode is not None
        else state
    )
    audit = _audit_or_empty(state.get("claim_audit"))
    status = str(audit.get("status") or "not_run")[:32]
    reason_code = str(audit.get("reason_code") or "")[:128]
    repair_count = _nonnegative_int(state.get("claim_repair_count"))
    executed = status not in {"", "not_run"}

    exemption_reason = ""
    if status == "not_run":
        try:
            # Lazy import avoids the service package's compatibility exports
            # forming a cycle while claim_evidence_verifier itself initializes.
            from cogdoc.agents.claim_evidence_verifier import (
                matching_claim_audit_exemption,
            )

            exemption_reason = str(
                matching_claim_audit_exemption(
                    state,
                    answer=str(state.get("answer") or ""),
                    task_type=str(state.get("task_type") or ""),
                )
                or ""
            )
        except Exception:
            # Rollout projection is a release boundary. Unexpected exemption
            # parsing must not turn an incomplete audit into an allow decision.
            exemption_reason = ""

    allowed = status in {"passed", "repaired"} or bool(
        status == "not_run"
        and exemption_reason
        and exemption_reason == reason_code
    )
    repair_eligible = bool(
        not allowed
        and status == "failed"
        and repair_count < _nonnegative_int(max_repair_attempts)
    )
    exempt = bool(allowed and status == "not_run")

    if resolved_mode == "off":
        decision = "skipped"
    elif resolved_mode == "shadow":
        decision = (
            "would_allow_exempt"
            if exempt
            else "would_allow"
            if allowed
            else "would_repair"
            if repair_eligible
            else "would_block"
        )
    else:
        decision = (
            "allow_exempt"
            if exempt
            else "allow"
            if allowed
            else "repair"
            if repair_eligible
            else "block"
        )

    intervention_projected = resolved_mode != "off" and not allowed
    policy = claim_verification_policy_projection(
        state.get("claim_verification_policy"),
        effective_mode=resolved_mode,
    )
    return {
        "version": "v1",
        "mode": resolved_mode,
        **policy,
        "decision": decision,
        "executed": executed,
        "enforced": resolved_mode == "enforce",
        "released": resolved_mode != "enforce" or allowed,
        "would_intervene": intervention_projected,
        "would_repair": intervention_projected and repair_eligible,
        "would_block": intervention_projected and not repair_eligible,
        "audit_status": status,
        "reason_code": reason_code,
        "repair_count": repair_count,
    }


def ensure_claim_verification_rollout(
    state: dict[str, Any],
    *,
    mode: str,
    max_repair_attempts: int,
    policy: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    existing = state.get("claim_verification_rollout")
    expected_policy = claim_verification_policy_projection(
        policy,
        effective_mode=mode,
    )
    if policy is not None:
        state["claim_verification_policy"] = dict(policy)
    if (
        isinstance(existing, Mapping)
        and existing.get("mode") == mode
        and all(existing.get(key) == value for key, value in expected_policy.items())
    ):
        return dict(existing)
    rollout = build_claim_verification_rollout(
        state,
        mode=mode,
        max_repair_attempts=max_repair_attempts,
    )
    state["claim_verification_rollout"] = rollout
    return rollout
