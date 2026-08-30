from cogdoc.tools.eval.scoring import (
    LLMJudge,
    _normalize_score,
    aggregate_case,
    aggregate_run,
    evaluate_trial,
)
import pytest


def test_deterministic_quality_and_fatal_gate_are_independent():
    report = evaluate_trial(
        {
            "trial_id": "t1",
            "execution_status": "SUCCESS",
            "agent_output": "答案 [a.pdf:P1] sk-secret-value-12345678901234567890",
            "expected": "答案 [a.pdf:P1] sk-secret-value-12345678901234567890",
        },
        [
            {"type": "exact_match", "role": "QUALITY", "requires": ["agent_output", "expected"]},
            {
                "type": "safety_assertion",
                "role": "GATE",
                "requires": ["agent_output"],
                "gate_policy": {"level": "FATAL", "required": True},
                "config": {"forbidden_patterns": [r"sk-[A-Za-z0-9-]{20,}"]},
            },
        ],
    )

    assert report["quality_score"] == 1.0
    assert report["gate_decision"] == "FATAL"
    assert report["decision"] == "FAIL"


@pytest.mark.parametrize(
    ("judge_score", "normalized"),
    [(1.0, 0.0), (2.0, 0.25), (3.0, 0.5), (4.0, 0.75), (5.0, 1.0)],
)
def test_judge_score_normalization_preserves_one_to_five_order(
    judge_score, normalized
):
    assert _normalize_score(judge_score) == normalized


def test_trace_incomplete_can_score_but_never_passes():
    report = evaluate_trial(
        {
            "trial_id": "t2",
            "execution_status": "TRACE_INCOMPLETE",
            "agent_output": "ok",
            "expected": "ok",
        },
        [{"type": "exact_match", "requires": ["agent_output", "expected"]}],
    )
    assert report["quality_score"] == 1.0
    assert report["decision"] == "NEEDS_REVIEW"


@pytest.mark.parametrize(
    "execution_status",
    ("PROTOCOL_ERROR", "CONFIG_ERROR", "TIMEOUT", "TARGET_ERROR"),
)
def test_failed_execution_can_score_but_never_passes(execution_status):
    report = evaluate_trial(
        {
            "trial_id": "failed",
            "execution_status": execution_status,
            "agent_output": "ok",
            "expected": "ok",
        },
        [{"type": "exact_match", "requires": ["agent_output", "expected"]}],
    )

    assert report["quality_score"] == 1.0
    assert report["decision"] == "FAIL"


def test_unknown_execution_status_requires_review():
    report = evaluate_trial(
        {
            "execution_status": "FUTURE_STATUS",
            "agent_output": "ok",
            "expected": "ok",
        },
        [{"type": "exact_match", "requires": ["agent_output", "expected"]}],
    )

    assert report["decision"] == "NEEDS_REVIEW"


def test_missing_execution_status_is_not_assumed_successful():
    report = evaluate_trial(
        {"agent_output": "ok", "expected": "ok"},
        [{"type": "exact_match", "requires": ["agent_output", "expected"]}],
    )

    assert report["execution_status"] == "UNKNOWN"
    assert report["decision"] == "NEEDS_REVIEW"


def test_required_unobservable_gate_forces_review():
    report = evaluate_trial(
        {
            "execution_status": "SUCCESS",
            "agent_output": "ok",
            "expected": "ok",
        },
        [
            {"type": "exact_match", "requires": ["agent_output", "expected"]},
            {
                "type": "safety_assertion",
                "role": "GATE",
                "requires": ["tool_trace"],
                "gate_policy": {"level": "CRITICAL", "required": True},
            },
        ],
    )

    assert report["gate_decision"] == "GATE_REVIEW"
    assert report["decision"] == "NEEDS_REVIEW"


def test_warning_gate_does_not_override_quality_decision():
    report = evaluate_trial(
        {
            "execution_status": "SUCCESS",
            "agent_output": "ok forbidden",
            "expected": "ok forbidden",
        },
        [
            {"type": "exact_match", "requires": ["agent_output", "expected"]},
            {
                "type": "safety_assertion",
                "role": "GATE",
                "requires": ["agent_output"],
                "gate_policy": {"level": "WARNING"},
                "config": {"forbidden_patterns": ["forbidden"]},
            },
        ],
    )

    assert report["gate_decision"] == "PASS_WITH_WARNING"
    assert report["decision"] == "PASS"


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"pass_threshold": float("nan")}, "pass_threshold"),
        ({"margin": -0.1}, "margin"),
    ],
)
def test_invalid_trial_thresholds_are_rejected(kwargs, message):
    with pytest.raises(ValueError, match=message):
        evaluate_trial({"execution_status": "SUCCESS"}, [], **kwargs)


def test_missing_required_evidence_is_not_observable():
    report = evaluate_trial(
        {"trial_id": "t3", "execution_status": "SUCCESS", "agent_output": "ok"},
        [{"type": "ragas_metric", "requires": ["agent_output", "retrieved_context"], "config": {"metric": "faithfulness"}}],
    )
    assert report["quality_score"] is None
    assert report["decision"] == "NEEDS_REVIEW"
    assert report["evaluators"][0]["status"] == "NOT_OBSERVABLE"


def test_claim_audit_assertion_recomputes_details_through_all_aliases():
    audit = {
        "status": "passed",
        "claims": [
            {
                "claim_id": "c1",
                "verdict": "supported",
                "cited_chunk_ids": ["chunk-1"],
                "supporting_chunk_ids": ["chunk-1"],
            },
            {
                "claim_id": "c2",
                "verdict": "unsupported",
                "cited_chunk_ids": [],
                "supporting_chunk_ids": [],
            },
            {
                "claim_id": "c3",
                "verdict": "not_factual",
                "cited_chunk_ids": [],
                "supporting_chunk_ids": [],
            },
        ],
        # 故意伪造汇总，assertion 必须忽略它们。
        "counts": {"claim_count": 100, "supported": 100},
        "metrics": {"claim_support_rate": 1.0, "citation_coverage": 1.0},
    }
    evaluator = {
        "type": "claim_audit_assertion",
        "requires": ["claim_audit"],
        "config": {
            "min_claim_support_rate": 0.5,
            "min_citation_coverage": 0.5,
            "max_unsupported_claim_rate": 0.5,
        },
    }
    trials = [
        {"claim_audit": audit},
        {"output": {"claim_audit": audit}},
        {"trace": {"output": {"claim_audit": audit}}},
    ]

    for trial in trials:
        report = evaluate_trial(
            {"execution_status": "SUCCESS", **trial},
            [evaluator],
        )
        result = report["evaluators"][0]
        assert result["status"] == "PASS"
        assert result["details"]["counts"] == {
            "claim_count": 2,
            "supported": 1,
            "unsupported": 1,
            "insufficient": 0,
            "cited": 1,
            "not_factual": 1,
        }
        assert result["details"]["metrics"]["claim_support_rate"] == 0.5
        assert result["details"]["metrics"]["citation_coverage"] == 0.5


def test_claim_audit_assertion_is_not_observable_when_audit_or_claims_missing():
    for trial, missing in [
        ({}, "claim_audit"),
        ({"claim_audit": {"status": "passed"}}, "claim_audit.claims"),
    ]:
        report = evaluate_trial(
            {"execution_status": "SUCCESS", **trial},
            [{"type": "claim_audit_assertion"}],
        )

        result = report["evaluators"][0]
        assert result["status"] == "NOT_OBSERVABLE"
        assert result["missing_evidence"] == [missing]
        assert report["quality_score"] is None
        assert report["decision"] == "NEEDS_REVIEW"


def test_claim_audit_assertion_defaults_to_strict_supported_and_cited_gate():
    report = evaluate_trial(
        {
            "execution_status": "SUCCESS",
            "claim_audit": {
                "status": "failed",
                "claims": [
                    {
                        "claim_id": "c1",
                        "verdict": "insufficient",
                        "cited_chunk_ids": ["chunk-1"],
                    }
                ],
            },
        },
        [{"type": "claim_audit_assertion"}],
    )

    result = report["evaluators"][0]
    assert result["status"] == "FAIL"
    assert result["details"]["metrics"]["insufficient_claim_rate"] == 1.0
    assert result["details"]["checks"]["status_allowed"] is False
    assert result["details"]["checks"]["support_rate"] is False
    assert report["decision"] == "FAIL"


def test_claim_audit_assertion_downgrades_inconsistent_supported_evidence():
    report = evaluate_trial(
        {
            "execution_status": "SUCCESS",
            "claim_audit": {
                "status": "passed",
                "claims": [
                    {
                        "claim_id": "c1",
                        "verdict": "supported",
                        "cited_chunk_ids": ["chunk-1"],
                        "supporting_chunk_ids": ["fabricated-chunk"],
                    }
                ],
            },
        },
        [{"type": "claim_audit_assertion"}],
    )

    result = report["evaluators"][0]
    assert result["status"] == "FAIL"
    assert result["details"]["counts"]["supported"] == 0
    assert result["details"]["counts"]["insufficient"] == 1


def test_claim_audit_assertion_rejects_string_in_place_of_citation_id_list():
    report = evaluate_trial(
        {
            "execution_status": "SUCCESS",
            "claim_audit": {
                "status": "passed",
                "claims": [
                    {
                        "claim_id": "c1",
                        "verdict": "supported",
                        "cited_chunk_ids": "chunk-1",
                        "supporting_chunk_ids": "chunk-1",
                    }
                ],
            },
        },
        [{"type": "claim_audit_assertion"}],
    )

    result = report["evaluators"][0]
    assert result["status"] == "FAIL"
    assert result["details"]["counts"]["supported"] == 0
    assert result["details"]["counts"]["insufficient"] == 1


def test_case_counts_execution_failures_and_mutually_exclusive_buckets():
    case = aggregate_case(
        [
            {"execution_status": "SUCCESS", "decision": "PASS", "quality_score": 1.0},
            {"execution_status": "TIMEOUT", "decision": "FAIL", "quality_score": None},
            {"execution_status": "TRACE_INCOMPLETE", "decision": "NEEDS_REVIEW", "quality_score": 0.5},
        ],
        min_trials=3,
        min_success_rate=0.8,
    )
    assert case["n_total"] == 3
    assert case["n_completed"] == 2
    assert case["n_passed"] == 1
    assert case["execution_completion_rate"] == 2 / 3
    assert case["observed_pass_rate"] == 1 / 3
    assert case["stability_status"] == "UNSTABLE"


def test_run_decision_and_stable_case_rate():
    report = aggregate_run(
        [
            {"quality_score": 0.9, "stability_status": "STABLE", "execution_completion_rate": 1.0},
            {"quality_score": None, "stability_status": "INSUFFICIENT", "execution_completion_rate": 0.0},
        ]
    )
    assert report["stable_case_rate"] == 0.5
    assert report["decision"] == "NEEDS_REVIEW"


def test_aggregates_reject_non_finite_scores_and_invalid_thresholds():
    with pytest.raises(ValueError, match="quality_score"):
        aggregate_case(
            [{"execution_status": "SUCCESS", "quality_score": float("nan")}]
        )
    with pytest.raises(ValueError, match="min_success_rate"):
        aggregate_case([], min_success_rate=2.0)
    with pytest.raises(ValueError, match="quality_score"):
        aggregate_run([{"quality_score": float("inf")}])


def test_llm_judge_uses_common_output_schema(monkeypatch):
    expected = {
        "overall_score": 4,
        "dimension_scores": {"correctness": 4},
        "pass": True,
        "confidence": 0.9,
        "rationale": "有证据支持",
        "concerns": [],
        "evidence": [{"dimension": "correctness", "source": "agent_output", "quote": "ok"}],
        "recommended_action": "PASS",
    }

    monkeypatch.setattr(LLMJudge, "_client", lambda self: object())
    monkeypatch.setattr(
        "cogdoc.tools.eval.scoring.invoke_structured",
        lambda client, schema, messages: schema.model_validate(expected),
    )
    judge = LLMJudge()
    result = judge.evaluate(
        {"case_input": "问题", "agent_output": "ok", "expected": "ok"},
        type("Spec", (), {"config": {"dimensions": ["correctness"]}, "type": "llm_judge"})(),
    )
    assert result["score"] == 0.75
    assert result["status"] == "PASS"
    assert result["evidence"]
