from types import SimpleNamespace

from cogdoc.service.claim_verification_policy import (
    claim_verification_policy_projection,
    resolve_claim_verification_policy,
)


def _settings(mode: str, percent: float, seed: str = "policy-v1") -> SimpleNamespace:
    return SimpleNamespace(
        claim_verification_mode=mode,
        claim_verification_enabled=False,
        claim_verification_rollout_percent=percent,
        claim_verification_rollout_seed=seed,
    )


def test_full_rollout_selects_configured_mode_and_is_sticky():
    first = resolve_claim_verification_policy(
        _settings("enforce", 100.0), cohort_key="kb\0session-a"
    )
    second = resolve_claim_verification_policy(
        _settings("enforce", 100.0), cohort_key="kb\0session-a"
    )

    assert first == second
    assert first.configured_mode == "enforce"
    assert first.effective_mode == "enforce"
    assert first.cohort_selected is True
    assert 0 <= first.cohort_bucket <= 9999
    assert len(first.policy_id) == 16
    assert "session-a" not in str(first.to_state())


def test_zero_percent_rolls_enforce_back_to_shadow_and_shadow_back_to_off():
    enforce = resolve_claim_verification_policy(
        _settings("enforce", 0.0), cohort_key="cohort"
    )
    shadow = resolve_claim_verification_policy(
        _settings("shadow", 0.0), cohort_key="cohort"
    )

    assert enforce.effective_mode == "shadow"
    assert enforce.fallback_mode == "shadow"
    assert enforce.cohort_selected is False
    assert shadow.effective_mode == "off"
    assert shadow.fallback_mode == "off"
    assert shadow.cohort_selected is False


def test_partial_rollout_uses_exact_bucket_threshold():
    policy = resolve_claim_verification_policy(
        _settings("enforce", 37.25), cohort_key="partial-cohort"
    )

    assert policy.cohort_selected is (policy.cohort_bucket < 3725)
    assert policy.effective_mode == (
        "enforce" if policy.cohort_selected else "shadow"
    )


def test_seed_rebuckets_and_changes_policy_version_without_exposing_seed():
    first = resolve_claim_verification_policy(
        _settings("shadow", 50.0, "seed-one"), cohort_key="cohort"
    )
    second = resolve_claim_verification_policy(
        _settings("shadow", 50.0, "seed-two"), cohort_key="cohort"
    )

    assert first.policy_id != second.policy_id
    assert "seed-one" not in str(first.to_state())
    assert "seed-two" not in str(second.to_state())


def test_public_policy_projection_bounds_malformed_fields():
    projection = claim_verification_policy_projection(
        {
            "configured_mode": "invalid",
            "rollout_percent": float("inf"),
            "cohort_bucket": 999999,
            "cohort_selected": False,
            "fallback_mode": "invalid",
            "policy_id": "not-a-hash",
            "cohort_key": "must-not-leak",
        },
        effective_mode="shadow",
    )

    assert projection == {
        "configured_mode": "off",
        "rollout_percent": 100.0,
        "cohort_bucket": 9999,
        "cohort_selected": False,
        "fallback_mode": "off",
        "policy_id": "",
    }
