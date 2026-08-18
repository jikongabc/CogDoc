from cogdoc.agents.claim_evidence_verifier import (
    CLAIM_AUDIT_EXEMPTION_GUIDANCE,
    make_claim_audit_exemption,
)
from cogdoc.service.claim_verification_rollout import (
    build_claim_verification_rollout,
    ensure_claim_verification_rollout,
)


def _state(status: str, *, reason_code: str = "", repair_count: int = 0) -> dict:
    return {
        "task_type": "qa",
        "answer": "候选答案。[a.pdf:P1]",
        "claim_repair_count": repair_count,
        "claim_audit": {"status": status, "reason_code": reason_code},
    }


def test_shadow_failed_audit_projects_would_repair_without_blocking_release():
    rollout = build_claim_verification_rollout(
        _state("failed", reason_code="unsupported_claims"),
        mode="shadow",
        max_repair_attempts=1,
    )

    assert rollout == {
        "version": "v1",
        "mode": "shadow",
        "configured_mode": "shadow",
        "rollout_percent": 100.0,
        "cohort_bucket": 0,
        "cohort_selected": True,
        "fallback_mode": "off",
        "policy_id": "",
        "decision": "would_repair",
        "executed": True,
        "enforced": False,
        "released": True,
        "would_intervene": True,
        "would_repair": True,
        "would_block": False,
        "audit_status": "failed",
        "reason_code": "unsupported_claims",
        "repair_count": 0,
    }


def test_shadow_error_projects_would_block_but_still_releases():
    rollout = build_claim_verification_rollout(
        _state("error", reason_code="verifier_timeout"),
        mode="shadow",
        max_repair_attempts=1,
    )

    assert rollout["decision"] == "would_block"
    assert rollout["released"] is True
    assert rollout["would_block"] is True


def test_enforce_failed_audit_projects_bounded_repair_then_block():
    repair = build_claim_verification_rollout(
        _state("failed", repair_count=0),
        mode="enforce",
        max_repair_attempts=1,
    )
    block = build_claim_verification_rollout(
        _state("failed", repair_count=1),
        mode="enforce",
        max_repair_attempts=1,
    )

    assert repair["decision"] == "repair"
    assert repair["released"] is False
    assert block["decision"] == "block"
    assert block["would_block"] is True


def test_matching_answer_bound_exemption_is_an_explicit_allow():
    answer = "请在摘要问题中明确指定要总结的文件名。"
    state = {
        "task_type": "summary",
        "answer": answer,
        "claim_audit_exemption": make_claim_audit_exemption(
            answer, CLAIM_AUDIT_EXEMPTION_GUIDANCE
        ),
        "claim_audit": {
            "status": "not_run",
            "reason_code": CLAIM_AUDIT_EXEMPTION_GUIDANCE,
        },
    }

    rollout = build_claim_verification_rollout(
        state,
        mode="shadow",
        max_repair_attempts=1,
    )

    assert rollout["decision"] == "would_allow_exempt"
    assert rollout["executed"] is False
    assert rollout["would_intervene"] is False


def test_off_mode_is_skipped_even_if_an_injected_audit_failed():
    rollout = build_claim_verification_rollout(
        _state("failed"), mode="off", max_repair_attempts=1
    )

    assert rollout["decision"] == "skipped"
    assert rollout["enforced"] is False
    assert rollout["released"] is True
    assert rollout["would_intervene"] is False
    assert rollout["would_repair"] is False
    assert rollout["would_block"] is False


def test_ensure_rebuilds_projection_when_request_mode_changes():
    state = _state("failed")
    state["claim_verification_rollout"] = {
        "mode": "off",
        "decision": "skipped",
    }

    rollout = ensure_claim_verification_rollout(
        state,
        mode="shadow",
        max_repair_attempts=0,
    )

    assert rollout["mode"] == "shadow"
    assert rollout["decision"] == "would_block"
    assert state["claim_verification_rollout"] == rollout


def test_ensure_rebuilds_stale_policy_metadata_even_when_mode_matches():
    state = _state("failed")
    state["claim_verification_rollout"] = {
        "mode": "shadow",
        "configured_mode": "shadow",
        "rollout_percent": 100.0,
        "cohort_bucket": 1,
        "cohort_selected": True,
        "fallback_mode": "off",
        "policy_id": "1111111111111111",
    }
    policy = {
        "configured_mode": "enforce",
        "rollout_percent": 25.0,
        "cohort_bucket": 4321,
        "cohort_selected": False,
        "fallback_mode": "shadow",
        "policy_id": "2222222222222222",
    }

    rollout = ensure_claim_verification_rollout(
        state,
        mode="shadow",
        max_repair_attempts=1,
        policy=policy,
    )

    assert rollout["configured_mode"] == "enforce"
    assert rollout["cohort_bucket"] == 4321
    assert rollout["policy_id"] == "2222222222222222"
