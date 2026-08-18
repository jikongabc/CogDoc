from __future__ import annotations

import hashlib
import json
import math
import random
from collections import defaultdict
from collections.abc import Mapping, Sequence
from statistics import NormalDist, mean
from typing import Any


CLAIM_VERDICTS = ("supported", "unsupported", "insufficient", "not_factual")
UNSAFE_VERDICTS = frozenset({"unsupported", "insufficient"})
ACCEPTED_VERDICTS = frozenset({"supported", "not_factual"})
OBSERVABLE_AUDIT_STATUSES = frozenset(
    {"passed", "repaired", "failed", "rejected"}
)
DEFAULT_BOOTSTRAP_ITERATIONS = 2_000
DEFAULT_CONFIDENCE_LEVEL = 0.95
DEFAULT_BOOTSTRAP_SEED = "cogdoc-claim-verification-v1"


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _percentile(values: Sequence[float], quantile: float) -> float:
    if not values:
        raise ValueError("percentile requires at least one value")
    ordered = sorted(values)
    position = max(0.0, min(1.0, quantile)) * (len(ordered) - 1)
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _finite_non_negative(value: Any, *, field: str) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{field} must be a finite non-negative number") from exc
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{field} must be a finite non-negative number")
    return number


def _audit_payload(item: Mapping[str, Any]) -> Mapping[str, Any] | None:
    candidates: list[Any] = [item.get("claim_audit")]
    output = item.get("output")
    if isinstance(output, Mapping):
        candidates.append(output.get("claim_audit"))
    trace = item.get("trace")
    if isinstance(trace, Mapping):
        trace_output = trace.get("output")
        if isinstance(trace_output, Mapping):
            candidates.append(trace_output.get("claim_audit"))
    return next((value for value in candidates if isinstance(value, Mapping)), None)


def _audit_duration_ms(audit: Mapping[str, Any] | None) -> float | None:
    if audit is None:
        return None
    verifier = audit.get("verifier")
    if not isinstance(verifier, Mapping):
        return None
    return _finite_non_negative(
        verifier.get("duration_ms"), field="claim_audit.verifier.duration_ms"
    )


def _audit_is_well_formed(audit: Mapping[str, Any]) -> bool:
    if str(audit.get("status") or "") not in OBSERVABLE_AUDIT_STATUSES:
        return False
    claims = audit.get("claims")
    if not isinstance(claims, list) or not all(
        isinstance(claim, Mapping) for claim in claims
    ):
        return False
    claim_ids = [str(claim.get("claim_id") or "").strip() for claim in claims]
    if any(not claim_id for claim_id in claim_ids) or len(claim_ids) != len(
        set(claim_ids)
    ):
        return False
    if any(str(claim.get("verdict") or "") not in CLAIM_VERDICTS for claim in claims):
        return False
    verifier = audit.get("verifier")
    if verifier is None:
        return True
    if not isinstance(verifier, Mapping):
        return False
    try:
        _audit_duration_ms(audit)
    except ValueError:
        return False
    return True


def _claim_from_audit(
    item: Mapping[str, Any], audit: Mapping[str, Any]
) -> Mapping[str, Any] | None:
    if not _audit_is_well_formed(audit):
        return None
    claims = audit.get("claims")
    assert isinstance(claims, list)
    candidates = claims
    claim_id = str(item.get("claim_id") or "").strip()
    if claim_id:
        return next(
            (
                claim
                for claim in candidates
                if str(claim.get("claim_id") or "").strip() == claim_id
            ),
            None,
        )
    if len(candidates) == 1:
        return candidates[0]
    return None


def _actual_verdict(
    item: Mapping[str, Any], audit: Mapping[str, Any] | None
) -> tuple[str | None, str]:
    if "actual_verdict" in item:
        actual = str(item.get("actual_verdict") or "").strip()
        if actual not in CLAIM_VERDICTS:
            raise ValueError(f"invalid actual_verdict: {actual or '<empty>'}")
        return actual, "recorded"
    if audit is None:
        return None, "missing_audit"
    claim = _claim_from_audit(item, audit)
    if claim is None:
        status = str(audit.get("status") or "")
        return None, f"audit_{status or 'malformed'}"
    actual = str(claim.get("verdict") or "").strip()
    if actual not in CLAIM_VERDICTS:
        return None, "audit_invalid_verdict"
    return actual, "claim_audit"


def evaluate_case(item: Mapping[str, Any]) -> dict[str, Any]:
    case_id = str(item.get("id") or "").strip()
    if not case_id:
        raise ValueError("claim-verification case id is required")
    layer = str(item.get("layer") or "").strip()
    if not layer:
        raise ValueError(f"claim-verification case {case_id!r} requires layer")
    expected = str(item.get("expected_verdict") or "").strip()
    if expected not in CLAIM_VERDICTS:
        raise ValueError(
            f"claim-verification case {case_id!r} has invalid expected_verdict"
        )

    audit = _audit_payload(item)
    actual, source = _actual_verdict(item, audit)
    observed = actual is not None
    accepted = actual in ACCEPTED_VERDICTS
    duration_ms = (
        _finite_non_negative(item.get("duration_ms"), field="duration_ms")
        if source == "recorded"
        else _audit_duration_ms(audit)
        if audit is not None and _audit_is_well_formed(audit)
        else None
    )
    return {
        "id": case_id,
        "layer": layer,
        "language": str(item.get("language") or "unspecified"),
        "expected_verdict": expected,
        "actual_verdict": actual,
        "verdict_source": source,
        "observable": observed,
        # Missing/malformed/error audits are rejected, so evaluation never turns
        # an observability failure into an implicit supported decision.
        "decision": "accept" if accepted else "reject",
        "correct": observed and actual == expected,
        "duration_ms": duration_ms,
    }


def validate_items(items: Sequence[Mapping[str, Any]]) -> None:
    seen: set[str] = set()
    for item in items:
        case_id = str(item.get("id") or "").strip()
        if not case_id:
            raise ValueError("claim-verification case id is required")
        if case_id in seen:
            raise ValueError(f"duplicate claim-verification case id: {case_id}")
        seen.add(case_id)


def eval_contract_sha256(items: Sequence[Mapping[str, Any]]) -> str:
    """Bind baselines to reviewed inputs while excluding recorded predictions."""

    excluded = {
        "actual_verdict",
        "duration_ms",
        "claim_audit",
        "output",
        "trace",
        "reviewer",
        "notes",
    }
    contract: list[dict[str, Any]] = []
    for item in items:
        entry = {key: value for key, value in item.items() if key not in excluded}
        if "claim" not in entry:
            audit = _audit_payload(item)
            if isinstance(audit, Mapping):
                claim = _claim_from_audit(item, audit)
                if isinstance(claim, Mapping):
                    claim_text = claim.get("text") or claim.get("claim")
                    if isinstance(claim_text, str) and claim_text.strip():
                        entry["claim"] = claim_text.strip()
        contract.append(entry)
    contract.sort(key=lambda item: str(item.get("id") or ""))
    payload = json.dumps(
        contract, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _p95(values: Sequence[float]) -> float | None:
    return _percentile(values, 0.95) if values else None


def summarize_rows(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    total = len(rows)
    observed = [row for row in rows if bool(row.get("observable"))]
    expected_supported = [
        row for row in rows if row.get("expected_verdict") == "supported"
    ]
    expected_unsafe = [
        row for row in rows if row.get("expected_verdict") in UNSAFE_VERDICTS
    ]
    predicted_supported = [
        row for row in rows if row.get("actual_verdict") == "supported"
    ]
    true_supported = [
        row
        for row in predicted_supported
        if row.get("expected_verdict") == "supported"
    ]
    unsafe_accepts = [
        row
        for row in expected_unsafe
        if row.get("actual_verdict") in ACCEPTED_VERDICTS
    ]
    expected_not_factual = [
        row for row in rows if row.get("expected_verdict") == "not_factual"
    ]
    durations = [
        float(row["duration_ms"])
        for row in rows
        if row.get("duration_ms") is not None
    ]
    confusion = {
        expected: {
            actual: sum(
                row.get("expected_verdict") == expected
                and row.get("actual_verdict") == actual
                for row in rows
            )
            for actual in (*CLAIM_VERDICTS, "unobservable")
        }
        for expected in CLAIM_VERDICTS
    }
    for expected in CLAIM_VERDICTS:
        confusion[expected]["unobservable"] = sum(
            row.get("expected_verdict") == expected and not row.get("observable")
            for row in rows
        )

    return {
        "sample_count": total,
        "observed_sample_count": len(observed),
        "supported_sample_count": len(expected_supported),
        "unsafe_sample_count": len(expected_unsafe),
        "not_factual_sample_count": len(expected_not_factual),
        "observable_rate": _ratio(len(observed), total),
        "exact_accuracy": _ratio(
            sum(bool(row.get("correct")) for row in rows), total
        ),
        "support_precision": _ratio(len(true_supported), len(predicted_supported)),
        "support_recall": _ratio(
            sum(row.get("actual_verdict") == "supported" for row in expected_supported),
            len(expected_supported),
        ),
        "unsafe_accept_rate": _ratio(len(unsafe_accepts), len(expected_unsafe)),
        "unsafe_rejection_recall": _ratio(
            len(expected_unsafe) - len(unsafe_accepts), len(expected_unsafe)
        ),
        "not_factual_recall": _ratio(
            sum(
                row.get("actual_verdict") == "not_factual"
                for row in expected_not_factual
            ),
            len(expected_not_factual),
        ),
        "unobservable_fail_closed_rate": _ratio(
            sum(
                not row.get("observable") and row.get("decision") == "reject"
                for row in rows
            ),
            total - len(observed),
        ),
        "latency_mean_ms": mean(durations) if durations else None,
        "latency_p95_ms": _p95(durations),
        "latency_sample_count": len(durations),
        "confusion_matrix": confusion,
    }


def _metric_values(summary: Mapping[str, Any]) -> dict[str, float]:
    return {
        key: float(value)
        for key, value in summary.items()
        if isinstance(value, (int, float))
        and not isinstance(value, bool)
        and not key.endswith("_count")
    }


def _binary_metric_counts(
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, tuple[int, int]]:
    expected_supported = [
        row for row in rows if row.get("expected_verdict") == "supported"
    ]
    expected_unsafe = [
        row for row in rows if row.get("expected_verdict") in UNSAFE_VERDICTS
    ]
    predicted_supported = [
        row for row in rows if row.get("actual_verdict") == "supported"
    ]
    expected_not_factual = [
        row for row in rows if row.get("expected_verdict") == "not_factual"
    ]
    unobservable = [row for row in rows if not row.get("observable")]
    unsafe_accepts = sum(
        row.get("actual_verdict") in ACCEPTED_VERDICTS for row in expected_unsafe
    )
    return {
        "observable_rate": (
            sum(bool(row.get("observable")) for row in rows),
            len(rows),
        ),
        "exact_accuracy": (
            sum(bool(row.get("correct")) for row in rows),
            len(rows),
        ),
        "support_precision": (
            sum(
                row.get("expected_verdict") == "supported"
                for row in predicted_supported
            ),
            len(predicted_supported),
        ),
        "support_recall": (
            sum(
                row.get("actual_verdict") == "supported"
                for row in expected_supported
            ),
            len(expected_supported),
        ),
        "unsafe_accept_rate": (unsafe_accepts, len(expected_unsafe)),
        "unsafe_rejection_recall": (
            len(expected_unsafe) - unsafe_accepts,
            len(expected_unsafe),
        ),
        "not_factual_recall": (
            sum(
                row.get("actual_verdict") == "not_factual"
                for row in expected_not_factual
            ),
            len(expected_not_factual),
        ),
        "unobservable_fail_closed_rate": (
            sum(row.get("decision") == "reject" for row in unobservable),
            len(unobservable),
        ),
    }


def _wilson_interval(
    successes: int, total: int, *, confidence_level: float
) -> tuple[float, float]:
    if total < 1:
        raise ValueError("Wilson interval requires a positive denominator")
    z = NormalDist().inv_cdf(1.0 - (1.0 - confidence_level) / 2.0)
    estimate = successes / total
    z_squared = z * z
    denominator = 1.0 + z_squared / total
    center = (estimate + z_squared / (2.0 * total)) / denominator
    margin = (
        z
        * math.sqrt(
            estimate * (1.0 - estimate) / total
            + z_squared / (4.0 * total * total)
        )
        / denominator
    )
    return max(0.0, center - margin), min(1.0, center + margin)


def bootstrap_intervals(
    rows: Sequence[Mapping[str, Any]],
    *,
    iterations: int = DEFAULT_BOOTSTRAP_ITERATIONS,
    confidence_level: float = DEFAULT_CONFIDENCE_LEVEL,
    seed: str = DEFAULT_BOOTSTRAP_SEED,
) -> dict[str, dict[str, Any]]:
    if iterations < 1:
        raise ValueError("bootstrap iterations must be positive")
    if not 0 < confidence_level < 1:
        raise ValueError("confidence level must be between 0 and 1")
    if not rows:
        return {}
    estimates = _metric_values(summarize_rows(rows))
    binary_counts = _binary_metric_counts(rows)
    samples: dict[str, list[float]] = defaultdict(list)
    rng = random.Random(seed)
    materialized = list(rows)
    for _ in range(iterations):
        resampled = [rng.choice(materialized) for _ in materialized]
        for metric, value in _metric_values(summarize_rows(resampled)).items():
            if metric in estimates and metric not in binary_counts:
                samples[metric].append(value)
    alpha = (1.0 - confidence_level) / 2.0
    intervals: dict[str, dict[str, Any]] = {
        metric: {
            "estimate": estimate,
            "lower": _percentile(samples[metric], alpha),
            "upper": _percentile(samples[metric], 1.0 - alpha),
            "method": "deterministic_percentile_bootstrap",
        }
        for metric, estimate in estimates.items()
        if samples.get(metric)
    }
    for metric, (successes, total) in binary_counts.items():
        if not total:
            continue
        lower, upper = _wilson_interval(
            successes, total, confidence_level=confidence_level
        )
        intervals[metric] = {
            "estimate": successes / total,
            "lower": lower,
            "upper": upper,
            "method": "wilson_score",
            "successes": successes,
            "sample_count": total,
        }
    return intervals


def run_eval(
    items: Sequence[Mapping[str, Any]],
    *,
    bootstrap_iterations: int = DEFAULT_BOOTSTRAP_ITERATIONS,
    confidence_level: float = DEFAULT_CONFIDENCE_LEVEL,
    bootstrap_seed: str = DEFAULT_BOOTSTRAP_SEED,
) -> dict[str, Any]:
    validate_items(items)
    rows = [evaluate_case(item) for item in items]
    by_layer_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_layer_rows[str(row["layer"])].append(row)
    aggregate = summarize_rows(rows)
    return {
        "schema_version": "claim_verification_eval_v1",
        "config": {
            "num_cases": len(rows),
            "eval_contract_sha256": eval_contract_sha256(items),
            "bootstrap_iterations": bootstrap_iterations,
            "confidence_level": confidence_level,
            "bootstrap_seed": bootstrap_seed,
        },
        "aggregate": aggregate,
        "confidence": bootstrap_intervals(
            rows,
            iterations=bootstrap_iterations,
            confidence_level=confidence_level,
            seed=bootstrap_seed,
        ),
        "by_layer": {
            layer: summarize_rows(layer_rows)
            for layer, layer_rows in sorted(by_layer_rows.items())
        },
        "rows": rows,
    }


def _numeric_config(value: Any, *, name: str) -> dict[str, float]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError(f"gate {name} must be an object")
    output: dict[str, float] = {}
    for key, raw in value.items():
        try:
            number = float(raw)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"gate {name}.{key} must be numeric") from exc
        if not math.isfinite(number) or number < 0:
            raise ValueError(f"gate {name}.{key} must be finite and non-negative")
        output[str(key)] = number
    return output


def _metric_check(
    *,
    kind: str,
    metric: str,
    actual: Any,
    threshold: float,
    passed: bool,
    reference: float | None = None,
    layer: str | None = None,
    bound: str | None = None,
) -> dict[str, Any]:
    return {
        "kind": kind,
        "metric": metric,
        "actual": actual,
        "threshold": threshold,
        "reference": reference,
        "layer": layer,
        "bound": bound,
        "passed": bool(passed),
        "failure_reason": None if passed else "threshold_not_met",
    }


def evaluate_gate(
    report: Mapping[str, Any],
    gate: Mapping[str, Any],
    *,
    baseline: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    aggregate = report.get("aggregate")
    confidence = report.get("confidence")
    if not isinstance(aggregate, Mapping) or not isinstance(confidence, Mapping):
        raise ValueError("claim-verification report is missing aggregate/confidence")
    checks: list[dict[str, Any]] = []

    minimum_samples = _numeric_config(
        gate.get("minimum_samples"), name="minimum_samples"
    )
    for metric, threshold in minimum_samples.items():
        actual = aggregate.get(metric)
        passed = isinstance(actual, (int, float)) and float(actual) >= threshold
        checks.append(
            _metric_check(
                kind="minimum_samples",
                metric=metric,
                actual=actual,
                threshold=threshold,
                passed=passed,
            )
        )

    for kind, bound_name, comparator in (
        ("minimum", "lower", lambda actual, threshold: actual >= threshold),
        ("maximum", "upper", lambda actual, threshold: actual <= threshold),
    ):
        for metric, threshold in _numeric_config(gate.get(kind), name=kind).items():
            interval = confidence.get(metric)
            actual = interval.get(bound_name) if isinstance(interval, Mapping) else None
            passed = isinstance(actual, (int, float)) and comparator(
                float(actual), threshold
            )
            checks.append(
                _metric_check(
                    kind=kind,
                    metric=metric,
                    actual=actual,
                    threshold=threshold,
                    passed=passed,
                    bound=bound_name,
                )
            )

    baseline_aggregate: Mapping[str, Any] = {}
    if baseline is not None:
        report_config = report.get("config")
        report_contract = (
            report_config.get("eval_contract_sha256")
            if isinstance(report_config, Mapping)
            else None
        )
        baseline_contract = baseline.get("eval_contract_sha256")
        if not isinstance(baseline_contract, str) or baseline_contract != report_contract:
            raise ValueError(
                "claim-verification baseline eval contract does not match current set"
            )
        candidate = baseline.get("aggregate")
        if isinstance(candidate, Mapping):
            baseline_aggregate = candidate
        elif isinstance(baseline.get("accepted_metrics"), Mapping):
            baseline_aggregate = baseline["accepted_metrics"]
        else:
            raise ValueError("claim-verification baseline is missing aggregate metrics")
    for kind, direction in (
        ("maximum_regression", "higher"),
        ("maximum_increase", "lower"),
    ):
        configured = _numeric_config(gate.get(kind), name=kind)
        if baseline is None:
            continue
        for metric, threshold in configured.items():
            actual = aggregate.get(metric)
            reference = baseline_aggregate.get(metric)
            if not isinstance(actual, (int, float)) or not isinstance(
                reference, (int, float)
            ):
                delta = None
                passed = False
            else:
                delta = (
                    float(reference) - float(actual)
                    if direction == "higher"
                    else float(actual) - float(reference)
                )
                passed = delta <= threshold
            checks.append(
                _metric_check(
                    kind=kind,
                    metric=metric,
                    actual=delta,
                    threshold=threshold,
                    reference=float(reference)
                    if isinstance(reference, (int, float))
                    else None,
                    passed=passed,
                )
            )

    per_layer = gate.get("per_layer")
    if per_layer is not None:
        if not isinstance(per_layer, Mapping):
            raise ValueError("gate per_layer must be an object")
        layers = report.get("by_layer")
        if not isinstance(layers, Mapping):
            layers = {}
        required = per_layer.get("required") or []
        if not isinstance(required, list) or not all(
            isinstance(layer, str) and layer for layer in required
        ):
            raise ValueError("gate per_layer.required must be a list of names")
        layer_minimum_samples = int(per_layer.get("minimum_samples", 0))
        layer_minimum = _numeric_config(
            per_layer.get("minimum"), name="per_layer.minimum"
        )
        layer_maximum = _numeric_config(
            per_layer.get("maximum"), name="per_layer.maximum"
        )
        for layer in required:
            metrics = layers.get(layer)
            if not isinstance(metrics, Mapping):
                metrics = {}
            sample_count = metrics.get("sample_count")
            checks.append(
                _metric_check(
                    kind="layer_minimum_samples",
                    metric="sample_count",
                    actual=sample_count,
                    threshold=float(layer_minimum_samples),
                    passed=isinstance(sample_count, (int, float))
                    and float(sample_count) >= layer_minimum_samples,
                    layer=layer,
                )
            )
            for metric, threshold in layer_minimum.items():
                actual = metrics.get(metric)
                checks.append(
                    _metric_check(
                        kind="layer_minimum",
                        metric=metric,
                        actual=actual,
                        threshold=threshold,
                        passed=isinstance(actual, (int, float))
                        and float(actual) >= threshold,
                        layer=layer,
                    )
                )
            for metric, threshold in layer_maximum.items():
                actual = metrics.get(metric)
                checks.append(
                    _metric_check(
                        kind="layer_maximum",
                        metric=metric,
                        actual=actual,
                        threshold=threshold,
                        passed=isinstance(actual, (int, float))
                        and float(actual) <= threshold,
                        layer=layer,
                    )
                )

    return {
        "passed": bool(checks) and all(check["passed"] for check in checks),
        "checks": checks,
        "failed_check_count": sum(not check["passed"] for check in checks),
    }
