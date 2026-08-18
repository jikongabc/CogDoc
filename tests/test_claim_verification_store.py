from cogdoc.api.claim_verification_store import (
    ClaimVerificationObservationStore,
    SqliteClaimVerificationObservationStore,
)


def _rollout(
    *,
    mode: str = "shadow",
    decision: str = "would_allow",
    audit_status: str = "passed",
    executed: bool = True,
    policy_id: str = "1111111111111111",
) -> dict:
    intervenes = decision in {"would_repair", "would_block", "repair", "block"}
    return {
        "version": "v1",
        "mode": mode,
        "configured_mode": "enforce" if mode == "shadow" else mode,
        "rollout_percent": 25.0,
        "cohort_bucket": 1234,
        "cohort_selected": mode != "shadow",
        "fallback_mode": "shadow" if mode == "shadow" else "off",
        "policy_id": policy_id,
        "decision": decision,
        "audit_status": audit_status,
        "executed": executed,
        "released": mode != "enforce" or not intervenes,
        "would_intervene": intervenes,
        "would_repair": decision in {"would_repair", "repair"},
        "would_block": decision in {"would_block", "block"},
    }


def test_memory_store_is_tenant_scoped_bounded_and_privacy_minimized():
    now = [1_000_000.0]
    store = ClaimVerificationObservationStore(
        max_per_tenant=2, clock=lambda: now[0]
    )

    assert store.record("tenant-a", "qa", _rollout()) is True
    assert store.record("tenant-a", "summary", _rollout()) is True
    assert store.record("tenant-a", "compare", _rollout()) is True
    assert store.record("tenant-b", "qa", _rollout()) is True

    tenant_a = store.summary(
        "tenant-a", window_hours=24, operational_min_samples=2
    )
    tenant_b = store.summary(
        "tenant-b", window_hours=24, operational_min_samples=2
    )
    assert tenant_a["total_count"] == 2
    assert tenant_a["by_task_type"] == {"compare": 1, "summary": 1}
    assert tenant_b["total_count"] == 1
    assert all(
        "answer" not in row and "query" not in row and "session_id" not in row
        for row in store._rows
    )


def test_summary_filters_policy_and_reports_operational_not_semantic_readiness():
    store = ClaimVerificationObservationStore()
    store.record("tenant-a", "qa", _rollout(policy_id="1111111111111111"))
    store.record(
        "tenant-a",
        "qa",
        _rollout(
            decision="would_block",
            audit_status="error",
            policy_id="1111111111111111",
        ),
    )
    store.record("tenant-a", "qa", _rollout(policy_id="2222222222222222"))

    summary = store.summary(
        "tenant-a",
        window_hours=24,
        effective_mode="shadow",
        policy_id="1111111111111111",
        operational_min_samples=2,
        operational_max_error_rate=0.4,
    )

    assert summary["total_count"] == 2
    assert summary["counts"]["errors"] == 1
    assert summary["rates"]["error_rate"] == 0.5
    readiness = summary["operational_readiness"]
    assert readiness["ready"] is False
    assert readiness["blockers"] == ["verifier_error_rate"]
    assert readiness["semantic_release_gate_required"] is True


def test_expired_observations_are_excluded_and_invalid_rollouts_are_ignored():
    now = [1_000_000.0]
    store = ClaimVerificationObservationStore(
        retention_days=1, clock=lambda: now[0]
    )
    assert store.record("tenant-a", "qa", {"mode": "invalid"}) is False
    assert store.record("tenant-a", "qa", _rollout()) is True
    now[0] += 25 * 3600

    summary = store.summary("tenant-a", window_hours=24)

    assert summary["total_count"] == 0
    assert summary["operational_readiness"]["ready"] is False
    assert summary["operational_readiness"]["blockers"] == [
        "minimum_samples",
        "verifier_error_rate",
    ]


def test_missing_policy_and_unexecuted_error_do_not_pollute_readiness():
    store = ClaimVerificationObservationStore()
    missing_policy = _rollout(policy_id="")
    assert store.record("tenant-a", "qa", missing_policy) is False
    assert store.record(
        "tenant-a",
        "qa",
        _rollout(audit_status="error", executed=False),
    ) is True

    summary = store.summary(
        "tenant-a", window_hours=24, operational_min_samples=1
    )

    assert summary["counts"]["errors"] == 0
    assert summary["rates"]["error_rate"] is None
    assert summary["operational_readiness"]["verifier_error_rate"] is None


def test_sqlite_store_survives_reopen_and_keeps_tenants_isolated(tmp_path):
    path = str(tmp_path / "state.db")
    first = SqliteClaimVerificationObservationStore(path)
    first.record("tenant-a", "qa", _rollout(decision="would_repair"))
    first.record("tenant-b", "qa", _rollout())
    first.close()

    second = SqliteClaimVerificationObservationStore(path)
    try:
        tenant_a = second.summary(
            "tenant-a", window_hours=24, operational_min_samples=1
        )
        tenant_b = second.summary(
            "tenant-b", window_hours=24, operational_min_samples=1
        )
    finally:
        second.close()

    assert tenant_a["total_count"] == 1
    assert tenant_a["by_decision"] == {"would_repair": 1}
    assert tenant_b["total_count"] == 1
    assert tenant_b["by_decision"] == {"would_allow": 1}


def test_sqlite_store_enforces_tenant_cap_and_global_retention(tmp_path):
    now = [1_000_000.0]
    store = SqliteClaimVerificationObservationStore(
        str(tmp_path / "state.db"),
        retention_days=1,
        max_per_tenant=2,
        clock=lambda: now[0],
    )
    try:
        store.record("tenant-a", "qa", _rollout())
        store.record("tenant-a", "summary", _rollout())
        store.record("tenant-a", "compare", _rollout())
        store.record("tenant-b", "qa", _rollout())

        capped = store.summary("tenant-a", window_hours=24)
        assert capped["total_count"] == 2
        assert capped["by_task_type"] == {"compare": 1, "summary": 1}

        now[0] += 25 * 3600
        store.record("tenant-b", "summary", _rollout())
        expired_a = store.summary("tenant-a", window_hours=24)
        retained_b = store.summary("tenant-b", window_hours=24)
    finally:
        store.close()

    assert expired_a["total_count"] == 0
    assert retained_b["total_count"] == 1
    assert retained_b["by_task_type"] == {"summary": 1}
