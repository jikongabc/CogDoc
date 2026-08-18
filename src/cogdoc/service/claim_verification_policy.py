from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from cogdoc.config.settings import resolve_claim_verification_mode


CLAIM_VERIFICATION_POLICY_VERSION = "v1"
_BUCKET_COUNT = 10_000


def _bounded_percent(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return 100.0
    if number != number or number in {float("inf"), float("-inf")}:
        return 100.0
    return min(100.0, max(0.0, number))


def _bounded_bool(value: Any, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    normalized = str(value or "").strip().lower()
    if normalized in {"true", "1"}:
        return True
    if normalized in {"false", "0"}:
        return False
    return default


@dataclass(frozen=True)
class ClaimVerificationPolicy:
    configured_mode: str
    effective_mode: str
    rollout_percent: float
    cohort_bucket: int
    cohort_selected: bool
    fallback_mode: str
    policy_id: str

    def to_state(self) -> dict[str, Any]:
        return {
            "version": CLAIM_VERIFICATION_POLICY_VERSION,
            "configured_mode": self.configured_mode,
            "effective_mode": self.effective_mode,
            "rollout_percent": self.rollout_percent,
            "cohort_bucket": self.cohort_bucket,
            "cohort_selected": self.cohort_selected,
            "fallback_mode": self.fallback_mode,
            "policy_id": self.policy_id,
        }


def _policy_id(*, mode: str, percent: float, seed: str) -> str:
    payload = json.dumps(
        {
            "version": CLAIM_VERIFICATION_POLICY_VERSION,
            "mode": mode,
            "rollout_percent": percent,
            "seed": seed,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def resolve_claim_verification_policy(
    settings: Any,
    *,
    cohort_key: str,
) -> ClaimVerificationPolicy:
    """Resolve one sticky request cohort without exposing its raw identity."""

    configured_mode = resolve_claim_verification_mode(settings)
    percent = _bounded_percent(
        getattr(settings, "claim_verification_rollout_percent", 100.0)
    )
    seed = str(
        getattr(settings, "claim_verification_rollout_seed", "cogdoc-v1")
        or "cogdoc-v1"
    )
    digest = hashlib.sha256(f"{seed}\0{cohort_key}".encode("utf-8")).digest()
    bucket = int.from_bytes(digest[:8], "big") % _BUCKET_COUNT
    selected = bucket < round(percent * 100)
    fallback_mode = {
        "off": "off",
        "shadow": "off",
        "enforce": "shadow",
    }[configured_mode]
    effective_mode = configured_mode if selected else fallback_mode
    return ClaimVerificationPolicy(
        configured_mode=configured_mode,
        effective_mode=effective_mode,
        rollout_percent=round(percent, 4),
        cohort_bucket=bucket,
        cohort_selected=selected,
        fallback_mode=fallback_mode,
        policy_id=_policy_id(mode=configured_mode, percent=percent, seed=seed),
    )


def claim_verification_policy_from_state(
    value: Any,
) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def claim_verification_policy_projection(
    value: Any,
    *,
    effective_mode: str,
) -> dict[str, Any]:
    """Return the bounded, identity-free policy fields safe for public rollout data."""

    policy = claim_verification_policy_from_state(value)
    configured_mode = resolve_claim_verification_mode(
        {"claim_verification_mode": policy.get("configured_mode", effective_mode)}
    )
    fallback_mode = {
        "off": "off",
        "shadow": "off",
        "enforce": "shadow",
    }[configured_mode]
    try:
        bucket = int(policy.get("cohort_bucket", 0) or 0)
    except (TypeError, ValueError, OverflowError):
        bucket = 0
    bucket = min(_BUCKET_COUNT - 1, max(0, bucket))
    raw_policy_id = str(policy.get("policy_id") or "")[:32]
    policy_id = (
        raw_policy_id
        if len(raw_policy_id) == 16
        and all(character in "0123456789abcdef" for character in raw_policy_id)
        else ""
    )
    return {
        "configured_mode": configured_mode,
        "rollout_percent": round(
            _bounded_percent(policy.get("rollout_percent", 100.0)), 4
        ),
        "cohort_bucket": bucket,
        "cohort_selected": _bounded_bool(
            policy.get("cohort_selected"), default=True
        ),
        "fallback_mode": fallback_mode,
        "policy_id": policy_id,
    }
