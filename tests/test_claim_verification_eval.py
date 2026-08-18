from __future__ import annotations

import json
import sys

import pytest

from scripts import eval_claim_verification
from cogdoc.tools.eval.claim_verification_eval import (
    bootstrap_intervals,
    eval_contract_sha256,
    evaluate_case,
    evaluate_gate,
    run_eval,
)


def _case(
    case_id: str,
    expected: str,
    actual: str,
    *,
    layer: str = "qa",
    duration_ms: float = 10.0,
) -> dict:
    return {
        "id": case_id,
        "layer": layer,
        "expected_verdict": expected,
        "actual_verdict": actual,
        "duration_ms": duration_ms,
    }


def _perfect_items() -> list[dict]:
    return [
        _case("s1", "supported", "supported", layer="qa"),
        _case("s2", "supported", "supported", layer="summary"),
        _case("u1", "unsupported", "unsupported", layer="qa"),
        _case("u2", "unsupported", "insufficient", layer="compare"),
        _case("i1", "insufficient", "insufficient", layer="summary"),
        _case("i2", "insufficient", "unsupported", layer="compare"),
        _case("n1", "not_factual", "not_factual", layer="qa"),
    ]


def test_run_eval_reports_claim_level_safety_metrics():
    report = run_eval(_perfect_items(), bootstrap_iterations=25)

    aggregate = report["aggregate"]
    assert aggregate["sample_count"] == 7
    assert aggregate["supported_sample_count"] == 2
    assert aggregate["unsafe_sample_count"] == 4
    assert aggregate["observable_rate"] == 1.0
    assert aggregate["support_precision"] == 1.0
    assert aggregate["support_recall"] == 1.0
    assert aggregate["unsafe_accept_rate"] == 0.0
    assert aggregate["unsafe_rejection_recall"] == 1.0
    assert aggregate["not_factual_recall"] == 1.0
    assert aggregate["exact_accuracy"] == 5 / 7
    assert aggregate["confusion_matrix"]["unsupported"]["insufficient"] == 1
    assert set(report["by_layer"]) == {"compare", "qa", "summary"}


def test_eval_extracts_selected_claim_from_nested_trace_audit():
    row = evaluate_case(
        {
            "id": "trace-1",
            "layer": "qa",
            "claim_id": "c2",
            "expected_verdict": "unsupported",
            "trace": {
                "output": {
                    "claim_audit": {
                        "status": "failed",
                        "claims": [
                            {"claim_id": "c1", "verdict": "supported"},
                            {"claim_id": "c2", "verdict": "unsupported"},
                        ],
                        "verifier": {"duration_ms": 42.5},
                    }
                }
            },
        }
    )

    assert row["actual_verdict"] == "unsupported"
    assert row["verdict_source"] == "claim_audit"
    assert row["correct"] is True
    assert row["duration_ms"] == 42.5


def test_rejected_audit_remains_observable_when_it_has_claim_details():
    row = evaluate_case(
        {
            "id": "rejected-1",
            "layer": "summary",
            "expected_verdict": "unsupported",
            "claim_audit": {
                "status": "rejected",
                "claims": [{"claim_id": "c1", "verdict": "unsupported"}],
            },
        }
    )

    assert row["observable"] is True
    assert row["actual_verdict"] == "unsupported"
    assert row["decision"] == "reject"


@pytest.mark.parametrize(
    "audit",
    [
        {
            "status": "passed",
            "claims": [
                {"claim_id": "c1", "verdict": "supported"},
                "malformed",
            ],
        },
        {
            "status": "passed",
            "claims": [
                {"claim_id": "c1", "verdict": "supported"},
                {"claim_id": "c1", "verdict": "supported"},
            ],
        },
        {
            "status": "passed",
            "claims": [{"claim_id": "c1", "verdict": "supported"}],
            "verifier": {"duration_ms": "NaN"},
        },
    ],
)
def test_malformed_audit_is_rejected_as_unobservable(audit):
    row = evaluate_case(
        {
            "id": "malformed",
            "layer": "qa",
            "claim_id": "c1",
            "expected_verdict": "supported",
            "claim_audit": audit,
        }
    )

    assert row["observable"] is False
    assert row["decision"] == "reject"
    assert row["duration_ms"] is None


def test_unobservable_audit_fails_closed_without_becoming_correct():
    report = run_eval(
        [
            {
                "id": "error-1",
                "layer": "qa",
                "expected_verdict": "supported",
                "claim_audit": {"status": "error", "claims": []},
            }
        ],
        bootstrap_iterations=10,
    )

    row = report["rows"][0]
    assert row["observable"] is False
    assert row["decision"] == "reject"
    assert row["correct"] is False
    assert report["aggregate"]["observable_rate"] == 0.0
    assert report["aggregate"]["unobservable_fail_closed_rate"] == 1.0


def test_unsafe_claim_misclassified_as_not_factual_counts_as_accept():
    report = run_eval(
        [_case("unsafe-skip", "unsupported", "not_factual")],
        bootstrap_iterations=10,
    )

    assert report["rows"][0]["decision"] == "accept"
    assert report["aggregate"]["unsafe_accept_rate"] == 1.0
    assert report["aggregate"]["unsafe_rejection_recall"] == 0.0


def test_eval_rejects_duplicate_ids():
    with pytest.raises(ValueError, match="duplicate"):
        run_eval(
            [_case("same", "supported", "supported")] * 2,
            bootstrap_iterations=5,
        )


def test_eval_rejects_invalid_recorded_verdict():
    with pytest.raises(ValueError, match="actual_verdict"):
        evaluate_case(
            {
                "id": "bad",
                "layer": "qa",
                "expected_verdict": "supported",
                "actual_verdict": "maybe",
            }
        )


def test_eval_contract_binds_claim_text_but_not_recorded_prediction():
    first = {
        "id": "contract",
        "layer": "qa",
        "claim_id": "c1",
        "expected_verdict": "supported",
        "claim_audit": {
            "status": "passed",
            "claims": [
                {"claim_id": "c1", "text": "Stable claim", "verdict": "supported"}
            ],
        },
    }
    changed_prediction = {
        **first,
        "claim_audit": {
            "status": "failed",
            "claims": [
                {
                    "claim_id": "c1",
                    "text": "Stable claim",
                    "verdict": "unsupported",
                }
            ],
        },
    }
    changed_claim = {
        **first,
        "claim_audit": {
            "status": "passed",
            "claims": [
                {"claim_id": "c1", "text": "Different claim", "verdict": "supported"}
            ],
        },
    }

    assert eval_contract_sha256([first]) == eval_contract_sha256(
        [changed_prediction]
    )
    assert eval_contract_sha256([first]) != eval_contract_sha256([changed_claim])


def test_bootstrap_intervals_are_deterministic():
    rows = [evaluate_case(item) for item in _perfect_items()]

    first = bootstrap_intervals(rows, iterations=50, seed="fixed")
    second = bootstrap_intervals(rows, iterations=50, seed="fixed")

    assert first == second
    assert first["observable_rate"]["estimate"] == 1.0
    assert first["observable_rate"]["lower"] < 1.0
    assert first["observable_rate"]["upper"] == 1.0
    assert first["observable_rate"]["method"] == "wilson_score"
    assert first["latency_p95_ms"]["method"] == (
        "deterministic_percentile_bootstrap"
    )


def test_gate_uses_confidence_bounds_and_layer_checks():
    report = run_eval(_perfect_items(), bootstrap_iterations=50)
    gate = {
        "minimum_samples": {
            "sample_count": 7,
            "supported_sample_count": 2,
            "unsafe_sample_count": 4,
        },
        "minimum": {
            "observable_rate": 0.6,
            "support_precision": 0.3,
            "support_recall": 0.3,
            "unsafe_rejection_recall": 0.5,
        },
        "maximum": {"unsafe_accept_rate": 0.5, "latency_p95_ms": 10.0},
        "per_layer": {
            "required": ["qa", "summary", "compare"],
            "minimum_samples": 2,
            "maximum": {"unsafe_accept_rate": 0.0},
        },
    }

    result = evaluate_gate(report, gate)

    assert result["passed"] is True
    assert result["failed_check_count"] == 0
    support_check = next(
        check for check in result["checks"] if check["metric"] == "support_recall"
    )
    assert support_check["bound"] == "lower"


def test_gate_fails_when_sample_maturity_is_missing():
    report = run_eval(_perfect_items(), bootstrap_iterations=10)

    result = evaluate_gate(
        report,
        {"minimum_samples": {"unsafe_sample_count": 100}},
    )

    assert result["passed"] is False
    assert result["failed_check_count"] == 1


def test_gate_compares_higher_and_lower_is_better_baseline_metrics():
    report = run_eval(
        [
            _case("s", "supported", "unsupported"),
            _case("u", "unsupported", "supported"),
        ],
        bootstrap_iterations=10,
    )
    baseline = {
        "eval_contract_sha256": report["config"]["eval_contract_sha256"],
        "accepted_metrics": {
            "support_recall": 1.0,
            "unsafe_accept_rate": 0.0,
        }
    }

    result = evaluate_gate(
        report,
        {
            "maximum_regression": {"support_recall": 0.1},
            "maximum_increase": {"unsafe_accept_rate": 0.1},
        },
        baseline=baseline,
    )

    assert result["passed"] is False
    assert result["failed_check_count"] == 2


def test_gate_rejects_baseline_from_a_different_eval_contract():
    report = run_eval(_perfect_items(), bootstrap_iterations=10)

    with pytest.raises(ValueError, match="eval contract"):
        evaluate_gate(
            report,
            {"maximum_regression": {"support_recall": 0.1}},
            baseline={
                "eval_contract_sha256": "0" * 64,
                "accepted_metrics": {"support_recall": 1.0},
            },
        )


def test_gate_skips_baseline_checks_for_first_promotion():
    report = run_eval(_perfect_items(), bootstrap_iterations=10)

    result = evaluate_gate(
        report,
        {
            "minimum_samples": {"sample_count": 1},
            "maximum_regression": {"support_recall": 0.0},
        },
    )

    assert result["passed"] is True
    assert all(check["kind"] != "maximum_regression" for check in result["checks"])


def test_gate_fails_closed_for_missing_required_layer():
    report = run_eval(_perfect_items(), bootstrap_iterations=10)

    result = evaluate_gate(
        report,
        {
            "per_layer": {
                "required": ["research"],
                "minimum_samples": 1,
            }
        },
    )

    assert result["passed"] is False
    assert result["checks"][0]["layer"] == "research"


def test_cli_promotes_baseline_only_after_gate_passes(tmp_path, monkeypatch):
    eval_set = tmp_path / "claims.jsonl"
    eval_set.write_text(
        "\n".join(json.dumps(item) for item in _perfect_items()),
        encoding="utf-8",
    )
    gate = tmp_path / "gate.json"
    gate.write_text(
        json.dumps({"minimum_samples": {"sample_count": 7}}),
        encoding="utf-8",
    )
    output = tmp_path / "report.json"
    baseline = tmp_path / "baseline.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "eval_claim_verification.py",
            "--eval-set",
            str(eval_set),
            "--gate",
            str(gate),
            "--output",
            str(output),
            "--promote-baseline",
            str(baseline),
            "--bootstrap-iterations",
            "10",
        ],
    )

    assert eval_claim_verification.main() == 0
    assert json.loads(output.read_text())["gate"]["passed"] is True
    promoted = json.loads(baseline.read_text())
    assert promoted["schema_version"] == "claim_verification_baseline_v1"
    assert promoted["accepted_metrics"]["unsafe_accept_rate"] == 0.0


def test_cli_does_not_replace_baseline_when_gate_fails(tmp_path, monkeypatch):
    eval_set = tmp_path / "claims.jsonl"
    eval_set.write_text(
        json.dumps(_case("one", "supported", "supported")), encoding="utf-8"
    )
    gate = tmp_path / "gate.json"
    gate.write_text(
        json.dumps({"minimum_samples": {"sample_count": 2}}), encoding="utf-8"
    )
    baseline = tmp_path / "baseline.json"
    baseline.write_text('{"keep": true}\n', encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "eval_claim_verification.py",
            "--eval-set",
            str(eval_set),
            "--gate",
            str(gate),
            "--promote-baseline",
            str(baseline),
            "--bootstrap-iterations",
            "5",
        ],
    )

    assert eval_claim_verification.main() == 1
    assert json.loads(baseline.read_text()) == {"keep": True}
