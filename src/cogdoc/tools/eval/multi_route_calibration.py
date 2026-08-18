from __future__ import annotations

import hashlib
import itertools
import random
import statistics
from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any

from cogdoc.tools.eval.multi_route_eval import (
    ROUTES,
    aggregate_case_metrics,
    layered_metrics,
    percentile,
    ranking_metrics,
    requirement_coverage,
    weighted_rrf,
)


DEFAULT_VALIDATION_FRACTION = 0.2
DEFAULT_SPLIT_SEED = "cogdoc-multi-route-v1"
DEFAULT_INNER_FOLDS = 5
DEFAULT_BOOTSTRAP_ITERATIONS = 2000
DEFAULT_CONFIDENCE_LEVEL = 0.95


def _case_id(case: Mapping[str, Any], position: int) -> str:
    return str(case.get("case_id") or case.get("id") or position)


def _stratum(case: Mapping[str, Any]) -> tuple[str, str, str]:
    return (
        str(case.get("query_type") or "unknown"),
        str(case.get("doc_type") or "unknown"),
        "no_answer" if bool(case.get("no_answer")) else "answerable",
    )


def stratified_holdout(
    cases: Sequence[Mapping[str, Any]],
    *,
    validation_fraction: float = DEFAULT_VALIDATION_FRACTION,
    seed: str = DEFAULT_SPLIT_SEED,
) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]], dict[str, Any]]:
    """Split cases reproducibly while preserving useful evaluation strata."""
    if not 0 < validation_fraction < 1:
        raise ValueError("validation_fraction must be between 0 and 1")
    if len(cases) < 2:
        raise ValueError("holdout calibration requires at least two cases")

    groups: dict[tuple[str, str, str], list[tuple[int, Mapping[str, Any]]]] = defaultdict(list)
    for position, case in enumerate(cases):
        groups[_stratum(case)].append((position, case))

    train_positions: set[int] = set()
    validation_positions: set[int] = set()
    strata: dict[str, dict[str, int]] = {}
    for key, rows in sorted(groups.items()):
        ordered = sorted(
            rows,
            key=lambda row: hashlib.sha256(
                f"{seed}:{_case_id(row[1], row[0])}:{row[0]}".encode()
            ).hexdigest(),
        )
        if len(ordered) == 1:
            validation_count = 0
        else:
            validation_count = max(1, round(len(ordered) * validation_fraction))
            validation_count = min(len(ordered) - 1, validation_count)
        validation_positions.update(position for position, _ in ordered[:validation_count])
        train_positions.update(position for position, _ in ordered[validation_count:])
        strata["|".join(key)] = {
            "total": len(ordered),
            "train": len(ordered) - validation_count,
            "validation": validation_count,
        }

    # Highly fragmented inputs can consist entirely of singleton strata. Keep the
    # split usable without silently dropping stratification for normal datasets.
    if not validation_positions:
        ordered = sorted(
            range(len(cases)),
            key=lambda position: hashlib.sha256(
                f"{seed}:{_case_id(cases[position], position)}:{position}".encode()
            ).hexdigest(),
        )
        validation_count = max(1, round(len(ordered) * validation_fraction))
        validation_count = min(len(ordered) - 1, validation_count)
        validation_positions = set(ordered[:validation_count])
        train_positions = set(ordered[validation_count:])

    train = [case for position, case in enumerate(cases) if position in train_positions]
    validation = [
        case for position, case in enumerate(cases) if position in validation_positions
    ]
    metadata = {
        "seed": seed,
        "validation_fraction": validation_fraction,
        "train_count": len(train),
        "validation_count": len(validation),
        "train_case_ids": [_case_id(case, position) for position, case in enumerate(train)],
        "validation_case_ids": [
            _case_id(case, position) for position, case in enumerate(validation)
        ],
        "strata": strata,
    }
    return train, validation, metadata


def stratified_kfold(
    cases: Sequence[Mapping[str, Any]],
    *,
    folds: int = DEFAULT_INNER_FOLDS,
    seed: str = DEFAULT_SPLIT_SEED,
) -> tuple[list[list[Mapping[str, Any]]], dict[str, Any]]:
    """Assign every case to one deterministic, approximately stratified fold."""
    if folds < 2:
        raise ValueError("folds must be at least 2")
    if folds > len(cases):
        raise ValueError("folds cannot exceed the number of cases")
    output: list[list[tuple[int, Mapping[str, Any]]]] = [[] for _ in range(folds)]
    groups: dict[tuple[str, str, str], list[tuple[int, Mapping[str, Any]]]] = defaultdict(list)
    for position, case in enumerate(cases):
        groups[_stratum(case)].append((position, case))
    strata: dict[str, list[int]] = {}
    for key, rows in sorted(groups.items()):
        ordered = sorted(
            rows,
            key=lambda row: hashlib.sha256(
                f"{seed}:kfold:{_case_id(row[1], row[0])}:{row[0]}".encode()
            ).hexdigest(),
        )
        offset = int(
            hashlib.sha256(f"{seed}:{'|'.join(key)}".encode()).hexdigest(), 16
        ) % folds
        counts = [0] * folds
        for row in ordered:
            fold_index = min(
                range(folds),
                key=lambda index: (
                    counts[index],
                    len(output[index]),
                    (index - offset) % folds,
                ),
            )
            output[fold_index].append(row)
            counts[fold_index] += 1
        strata["|".join(key)] = counts
    fold_cases = [
        [case for _, case in sorted(rows, key=lambda row: row[0])] for rows in output
    ]
    if any(not fold for fold in fold_cases):
        # Sparse strata can align into an empty fold. Rebalance deterministically
        # without duplicating or dropping cases.
        while any(not fold for fold in fold_cases):
            empty = next(index for index, fold in enumerate(fold_cases) if not fold)
            donor = max(range(folds), key=lambda index: len(fold_cases[index]))
            fold_cases[empty].append(fold_cases[donor].pop())
    metadata = {
        "seed": seed,
        "fold_count": folds,
        "fold_sizes": [len(fold) for fold in fold_cases],
        "fold_case_ids": [
            [_case_id(case, position) for position, case in enumerate(fold)]
            for fold in fold_cases
        ],
        "strata_fold_counts": strata,
    }
    return fold_cases, metadata


def _candidate_weights(grid: Sequence[float]):
    # RRF is scale-invariant for ranking, so pin one route to avoid redundant trials.
    for values in itertools.product(grid, repeat=len(ROUTES) - 1):
        yield {ROUTES[0]: 1.0, **dict(zip(ROUTES[1:], values, strict=True))}


def _objective(rows: Sequence[Mapping[str, float]]) -> float:
    if not rows:
        return float("-inf")
    return statistics.mean(
        row["recall"] * 0.45
        + row["mrr"] * 0.2
        + row["ndcg"] * 0.2
        + row["coverage"] * 0.15
        for row in rows
    )


def _fusion_rows(
    cases: Sequence[Mapping[str, Any]],
    *,
    weights: Mapping[str, float],
    rrf_k: float,
    top_k: int,
    route_min: int,
) -> list[dict[str, float]]:
    rows = []
    for case in cases:
        if bool(case.get("no_answer")):
            continue
        routes = case.get("results", {}).get("all", {}).get("routes", {})
        hits = weighted_rrf(
            routes,
            weights,
            rrf_k=rrf_k,
            top_k=top_k,
            per_route_min=route_min,
        )
        metrics = ranking_metrics(hits, case, k=top_k)
        rows.append(
            {
                "recall": metrics[f"recall@{top_k}"],
                "mrr": metrics["mrr"],
                "ndcg": metrics[f"ndcg@{top_k}"],
                "coverage": requirement_coverage(
                    hits, case.get("gold_requirements", [])
                ),
            }
        )
    return rows


def _mean_fusion_metrics(rows: Sequence[Mapping[str, float]]) -> dict[str, float]:
    return {
        key: statistics.mean(row[key] for row in rows)
        for key in ("recall", "mrr", "ndcg", "coverage")
    }


def calibrate_fusion(
    cases: Sequence[Mapping[str, Any]],
    *,
    rrf_k: float,
    weight_grid: Sequence[float] = (0.0, 0.5, 1.0, 1.5),
    top_k_grid: Sequence[int] = (6, 9, 12),
    route_min_grid: Sequence[int] = (0, 1),
) -> dict[str, Any]:
    best: dict[str, Any] | None = None
    trials = 0
    for weights in _candidate_weights(weight_grid):
        if not any(weights.values()):
            continue
        for top_k in top_k_grid:
            for route_min in route_min_grid:
                rows = _fusion_rows(
                    cases,
                    weights=weights,
                    rrf_k=rrf_k,
                    top_k=top_k,
                    route_min=route_min,
                )
                if not rows:
                    continue
                objective = _objective(rows)
                candidate = {
                    "objective": objective,
                    "route_weights": weights,
                    "top_k": top_k,
                    "route_min_candidates": route_min,
                    "metrics": _mean_fusion_metrics(rows),
                }
                trials += 1
                if best is None or (objective, str(candidate)) > (
                    best["objective"], str(best)
                ):
                    best = candidate
    if best is None:
        raise ValueError("fusion calibration requires at least one answerable case")
    return {**best, "trial_count": trials}


def calibrate_fusion_cross_validated(
    cases: Sequence[Mapping[str, Any]],
    *,
    rrf_k: float,
    folds: int = DEFAULT_INNER_FOLDS,
    seed: str = DEFAULT_SPLIT_SEED,
    robustness_penalty: float = 0.25,
    weight_grid: Sequence[float] = (0.0, 0.5, 1.0, 1.5),
    top_k_grid: Sequence[int] = (6, 9, 12),
    route_min_grid: Sequence[int] = (0, 1),
) -> dict[str, Any]:
    if robustness_penalty < 0:
        raise ValueError("robustness_penalty must be non-negative")
    fold_cases, fold_metadata = stratified_kfold(cases, folds=folds, seed=seed)
    best: dict[str, Any] | None = None
    best_key: tuple[Any, ...] | None = None
    trials = 0
    for weights in _candidate_weights(weight_grid):
        for top_k in top_k_grid:
            for route_min in route_min_grid:
                fold_results = []
                all_rows: list[dict[str, float]] = []
                for fold_index, fold in enumerate(fold_cases):
                    rows = _fusion_rows(
                        fold,
                        weights=weights,
                        rrf_k=rrf_k,
                        top_k=top_k,
                        route_min=route_min,
                    )
                    if not rows:
                        continue
                    all_rows.extend(rows)
                    fold_results.append(
                        {
                            "fold": fold_index,
                            "sample_count": len(rows),
                            "objective": _objective(rows),
                            "metrics": _mean_fusion_metrics(rows),
                        }
                    )
                if not fold_results:
                    continue
                objectives = [row["objective"] for row in fold_results]
                objective_mean = statistics.mean(objectives)
                objective_stdev = (
                    statistics.pstdev(objectives) if len(objectives) > 1 else 0.0
                )
                robust_objective = objective_mean - robustness_penalty * objective_stdev
                candidate = {
                    "objective": objective_mean,
                    "robust_objective": robust_objective,
                    "objective_stdev": objective_stdev,
                    "worst_fold_objective": min(objectives),
                    "route_weights": dict(weights),
                    "top_k": top_k,
                    "route_min_candidates": route_min,
                    "metrics": _mean_fusion_metrics(all_rows),
                    "folds": fold_results,
                }
                trials += 1
                selection_key = (
                    robust_objective,
                    candidate["worst_fold_objective"],
                    objective_mean,
                    str(candidate["route_weights"]),
                    top_k,
                    route_min,
                )
                if best_key is None or selection_key > best_key:
                    best = candidate
                    best_key = selection_key
    if best is None:
        raise ValueError("cross-validation requires answerable cases in at least one fold")
    return {
        **best,
        "trial_count": trials,
        "robustness_penalty": robustness_penalty,
        "cross_validation": fold_metadata,
    }


def _signals(case: Mapping[str, Any]) -> dict[str, float | None]:
    routes = case.get("results", {}).get("all", {}).get("routes", {})

    def values(route, key):
        output = []
        for hit in routes.get(route, []):
            retrieval = hit.get("retrieval", {})
            value = retrieval.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                output.append(float(value))
        return output

    distances = values("rag_vector", "distance")
    bm25 = values("rag_bm25", "bm25_score")
    knowledge_vector = values("derived_knowledge_vector", "retrieval_score")
    knowledge_lexical = values("derived_knowledge_lexical", "retrieval_score")
    return {
        "vector_distance_max": min(distances) if distances else None,
        "bm25_score_min": max(bm25) if bm25 else None,
        "knowledge_vector_score_min": max(knowledge_vector) if knowledge_vector else None,
        "knowledge_lexical_score_min": max(knowledge_lexical) if knowledge_lexical else None,
    }


def _threshold_candidates(values: Sequence[float], *, lower_is_better: bool) -> list[float]:
    if not values:
        return []
    ordered = sorted(set(values))
    if len(ordered) > 12:
        ordered = [ordered[round(index * (len(ordered) - 1) / 11)] for index in range(12)]
    edge = (max(ordered) + 1.0) if lower_is_better else max(0.0, min(ordered) - 1.0)
    return sorted(set([*ordered, edge]))


def _abstention_accuracy(
    observations: Sequence[tuple[bool, Mapping[str, float | None]]],
    thresholds: Mapping[str, float],
) -> float:
    correct = 0
    for no_answer, signals in observations:
        supported = False
        for name, threshold in thresholds.items():
            value = signals.get(name)
            if value is None:
                continue
            if (
                value <= threshold
                if name == "vector_distance_max"
                else value >= threshold
            ):
                supported = True
                break
        correct += no_answer == (not supported)
    return correct / len(observations) if observations else 0.0


def calibrate_abstention(
    cases: Sequence[Mapping[str, Any]], current: Mapping[str, float]
) -> dict[str, Any]:
    observations = [(bool(case.get("no_answer")), _signals(case)) for case in cases]
    thresholds = {key: float(value) for key, value in current.items()}
    directions = {
        "vector_distance_max": True,
        "bm25_score_min": False,
        "knowledge_vector_score_min": False,
        "knowledge_lexical_score_min": False,
    }
    for _ in range(2):
        for name, lower_is_better in directions.items():
            values = []
            for _, signals in observations:
                value = signals.get(name)
                if value is not None:
                    values.append(value)
            candidates = _threshold_candidates(values, lower_is_better=lower_is_better)
            if not candidates:
                continue
            thresholds[name] = max(
                candidates,
                key=lambda value: (
                    _abstention_accuracy(observations, {**thresholds, name: value}),
                    -abs(value - float(current[name])),
                ),
            )
    return {
        "thresholds": thresholds,
        "accuracy": _abstention_accuracy(observations, thresholds),
        "sample_count": len(observations),
    }


def calibrate_abstention_cross_validated(
    cases: Sequence[Mapping[str, Any]],
    current: Mapping[str, float],
    *,
    folds: int = DEFAULT_INNER_FOLDS,
    seed: str = DEFAULT_SPLIT_SEED,
) -> dict[str, Any]:
    fold_cases, fold_metadata = stratified_kfold(cases, folds=folds, seed=seed)
    fold_results = []
    learned_thresholds: dict[str, list[float]] = defaultdict(list)
    for fold_index, validation in enumerate(fold_cases):
        training = [
            case
            for candidate_index, candidate_fold in enumerate(fold_cases)
            if candidate_index != fold_index
            for case in candidate_fold
        ]
        learned = calibrate_abstention(training, current)
        for name, value in learned["thresholds"].items():
            learned_thresholds[name].append(float(value))
        observations = [
            (bool(case.get("no_answer")), _signals(case)) for case in validation
        ]
        fold_results.append(
            {
                "fold": fold_index,
                "sample_count": len(validation),
                "accuracy": _abstention_accuracy(observations, learned["thresholds"]),
                "current_accuracy": _abstention_accuracy(observations, current),
                "thresholds": learned["thresholds"],
            }
        )
    thresholds = {
        name: statistics.median(values) for name, values in learned_thresholds.items()
    }
    observations = [(bool(case.get("no_answer")), _signals(case)) for case in cases]
    accuracies = [float(row["accuracy"]) for row in fold_results]
    current_accuracies = [float(row["current_accuracy"]) for row in fold_results]
    return {
        "thresholds": thresholds,
        "accuracy": _abstention_accuracy(observations, thresholds),
        "sample_count": len(observations),
        "cross_validation": {
            **fold_metadata,
            "accuracy_mean": statistics.mean(accuracies),
            "accuracy_stdev": statistics.pstdev(accuracies),
            "accuracy_worst_fold": min(accuracies),
            "current_accuracy_mean": statistics.mean(current_accuracies),
            "folds": fold_results,
        },
    }


def _supported_by_thresholds(
    signals: Mapping[str, float | None], thresholds: Mapping[str, float]
) -> bool:
    for name, threshold in thresholds.items():
        value = signals.get(name)
        if value is None:
            continue
        if name == "vector_distance_max":
            if value <= float(threshold):
                return True
        elif value >= float(threshold):
            return True
    return False


def evaluate_config(
    cases: Sequence[Mapping[str, Any]],
    config: Mapping[str, Any],
    *,
    rrf_k: float,
) -> dict[str, Any]:
    """Replay a config from captured route rankings without touching live indexes."""
    top_k = int(config["top_k"])
    weights = config["route_weights"]
    route_min = int(config["route_min_candidates"])
    thresholds = config["abstention_thresholds"]
    rows: list[dict[str, Any]] = []
    answerable_count = 0
    no_answer_count = 0
    for case in cases:
        all_result = case.get("results", {}).get("all", {})
        routes = all_result.get("routes", {})
        hits = weighted_rrf(
            routes,
            weights,
            rrf_k=rrf_k,
            top_k=top_k,
            per_route_min=route_min,
        )
        expected_no_answer = bool(case.get("no_answer"))
        supported = _supported_by_thresholds(_signals(case), thresholds)
        metrics: dict[str, float] = {
            "abstention_accuracy": float(expected_no_answer == (not supported)),
        }
        if expected_no_answer:
            no_answer_count += 1
        else:
            answerable_count += 1
            ranking = ranking_metrics(hits, case, k=top_k)
            metrics.update(
                {
                    "recall": ranking[f"recall@{top_k}"],
                    "mrr": ranking["mrr"],
                    "ndcg": ranking[f"ndcg@{top_k}"],
                    "requirement_coverage": requirement_coverage(
                        hits, case.get("gold_requirements", [])
                    ),
                }
            )
        latency = all_result.get("latency_ms")
        rows.append(
            {
                "case_id": str(case.get("case_id") or ""),
                "query_type": str(case.get("query_type") or "unknown"),
                "doc_type": str(case.get("doc_type") or "unknown"),
                "chunk_type": str((hits[0] if hits else {}).get("chunk_type") or "none"),
                "metrics": metrics,
                "latency_ms": latency,
            }
        )
    overall = aggregate_case_metrics(rows)
    overall["answerable_sample_count"] = float(answerable_count)
    overall["no_answer_sample_count"] = float(no_answer_count)
    return {"overall": overall, "slices": layered_metrics(rows), "rows": rows}


def _confidence_bounds(
    values: Sequence[float], confidence_level: float
) -> tuple[float, float]:
    alpha = (1.0 - confidence_level) / 2.0
    return percentile(values, alpha), percentile(values, 1.0 - alpha)


def bootstrap_metric_intervals(
    rows: Sequence[Mapping[str, Any]],
    *,
    iterations: int = DEFAULT_BOOTSTRAP_ITERATIONS,
    confidence_level: float = DEFAULT_CONFIDENCE_LEVEL,
    seed: str = DEFAULT_SPLIT_SEED,
) -> dict[str, dict[str, float]]:
    if iterations < 100:
        raise ValueError("bootstrap iterations must be at least 100")
    if not 0 < confidence_level < 1:
        raise ValueError("confidence_level must be between 0 and 1")
    if not rows:
        return {}
    rng = random.Random(
        int(hashlib.sha256(f"{seed}:bootstrap".encode()).hexdigest(), 16)
    )
    metrics = sorted(
        {
            str(metric)
            for row in rows
            for metric, value in row.get("metrics", {}).items()
            if isinstance(value, (int, float)) and not isinstance(value, bool)
        }
    )
    samples: dict[str, list[float]] = {metric: [] for metric in metrics}
    samples["latency_p95_ms"] = []
    for _ in range(iterations):
        selected = [rows[rng.randrange(len(rows))] for _ in range(len(rows))]
        for metric in metrics:
            values = [
                float(row["metrics"][metric])
                for row in selected
                if isinstance(row.get("metrics", {}).get(metric), (int, float))
            ]
            if values:
                samples[metric].append(statistics.mean(values))
        latencies = [
            float(row["latency_ms"])
            for row in selected
            if isinstance(row.get("latency_ms"), (int, float))
        ]
        if latencies:
            samples["latency_p95_ms"].append(percentile(latencies, 0.95))
    output: dict[str, dict[str, float]] = {}
    for metric, estimates in samples.items():
        if not estimates:
            continue
        if metric == "latency_p95_ms":
            observed = [
                float(row["latency_ms"])
                for row in rows
                if isinstance(row.get("latency_ms"), (int, float))
            ]
            estimate = percentile(observed, 0.95)
            sample_count = len(observed)
        else:
            observed = [
                float(row["metrics"][metric])
                for row in rows
                if isinstance(row.get("metrics", {}).get(metric), (int, float))
            ]
            estimate = statistics.mean(observed)
            sample_count = len(observed)
        lower, upper = _confidence_bounds(estimates, confidence_level)
        output[metric] = {
            "estimate": estimate,
            "lower": lower,
            "upper": upper,
            "sample_count": float(sample_count),
        }
    return output


def bootstrap_delta_intervals(
    candidate_rows: Sequence[Mapping[str, Any]],
    reference_rows: Sequence[Mapping[str, Any]],
    *,
    iterations: int = DEFAULT_BOOTSTRAP_ITERATIONS,
    confidence_level: float = DEFAULT_CONFIDENCE_LEVEL,
    seed: str = DEFAULT_SPLIT_SEED,
) -> dict[str, dict[str, float]]:
    if len(candidate_rows) != len(reference_rows):
        raise ValueError("paired bootstrap requires equal row counts")
    if iterations < 100:
        raise ValueError("bootstrap iterations must be at least 100")
    if not 0 < confidence_level < 1:
        raise ValueError("confidence_level must be between 0 and 1")
    pairs = list(zip(candidate_rows, reference_rows, strict=True))
    if not pairs:
        return {}
    for candidate, reference in pairs:
        if candidate.get("case_id") != reference.get("case_id"):
            raise ValueError("paired bootstrap case order does not match")
    metrics = sorted(
        set.intersection(
            {
                str(metric)
                for row in candidate_rows
                for metric in row.get("metrics", {})
            },
            {
                str(metric)
                for row in reference_rows
                for metric in row.get("metrics", {})
            },
        )
    )
    rng = random.Random(
        int(hashlib.sha256(f"{seed}:paired-bootstrap".encode()).hexdigest(), 16)
    )
    samples: dict[str, list[float]] = {metric: [] for metric in metrics}
    samples["latency_p95_ms"] = []
    for _ in range(iterations):
        selected = [pairs[rng.randrange(len(pairs))] for _ in range(len(pairs))]
        for metric in metrics:
            deltas = [
                float(candidate["metrics"][metric])
                - float(reference["metrics"][metric])
                for candidate, reference in selected
                if isinstance(candidate.get("metrics", {}).get(metric), (int, float))
                and isinstance(reference.get("metrics", {}).get(metric), (int, float))
            ]
            if deltas:
                samples[metric].append(statistics.mean(deltas))
        candidate_latencies = [
            float(candidate["latency_ms"])
            for candidate, _ in selected
            if isinstance(candidate.get("latency_ms"), (int, float))
        ]
        reference_latencies = [
            float(reference["latency_ms"])
            for _, reference in selected
            if isinstance(reference.get("latency_ms"), (int, float))
        ]
        if candidate_latencies and reference_latencies:
            samples["latency_p95_ms"].append(
                percentile(candidate_latencies, 0.95)
                - percentile(reference_latencies, 0.95)
            )
    output: dict[str, dict[str, float]] = {}
    for metric, estimates in samples.items():
        if not estimates:
            continue
        lower, upper = _confidence_bounds(estimates, confidence_level)
        output[metric] = {
            "estimate": statistics.mean(estimates),
            "lower": lower,
            "upper": upper,
            "sample_count": float(len(pairs)),
        }
    return output


def metric_comparison(
    candidate: Mapping[str, Any], reference: Mapping[str, Any]
) -> dict[str, dict[str, float]]:
    output: dict[str, dict[str, float]] = {}
    for key, candidate_value in candidate.items():
        reference_value = reference.get(key)
        if (
            isinstance(candidate_value, (int, float))
            and not isinstance(candidate_value, bool)
            and isinstance(reference_value, (int, float))
            and not isinstance(reference_value, bool)
        ):
            output[str(key)] = {
                "candidate": float(candidate_value),
                "reference": float(reference_value),
                "delta": float(candidate_value) - float(reference_value),
            }
    return output


def evaluate_gate(
    candidate: Mapping[str, Any],
    *,
    gate: Mapping[str, Any],
    current: Mapping[str, Any],
    baseline: Mapping[str, Any] | None = None,
    candidate_intervals: Mapping[str, Mapping[str, float]] | None = None,
    delta_intervals: Mapping[str, Mapping[str, float]] | None = None,
    candidate_slices: Mapping[str, Any] | None = None,
    current_slices: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate absolute thresholds and bounded regressions, failing closed."""
    checks: list[dict[str, Any]] = []
    confidence_policy = gate.get("confidence_bounds", {})

    def add_check(
        *,
        kind: str,
        metric: str,
        actual: float | None,
        threshold: float,
        passed: bool,
        reference: float | None = None,
        point_estimate: float | None = None,
        bound: str | None = None,
        slice_dimension: str | None = None,
        slice_name: str | None = None,
    ) -> None:
        checks.append(
            {
                "kind": kind,
                "metric": metric,
                "actual": actual,
                "threshold": threshold,
                "reference": reference,
                "point_estimate": point_estimate,
                "bound": bound,
                "slice_dimension": slice_dimension,
                "slice_name": slice_name,
                "passed": passed,
                "failure_reason": None if passed else "metric_missing_or_threshold_failed",
            }
        )

    for metric, threshold in gate.get("minimum", {}).items():
        value = candidate.get(metric)
        point = float(value) if isinstance(value, (int, float)) else None
        interval = (
            (candidate_intervals or {}).get(metric, {})
            if metric in confidence_policy.get("minimum", [])
            else {}
        )
        actual = float(interval["lower"]) if "lower" in interval else point
        add_check(
            kind="minimum",
            metric=str(metric),
            actual=actual,
            threshold=float(threshold),
            passed=actual is not None and actual >= float(threshold),
            point_estimate=point,
            bound="lower" if "lower" in interval else None,
        )
    for metric, threshold in gate.get("maximum", {}).items():
        value = candidate.get(metric)
        point = float(value) if isinstance(value, (int, float)) else None
        interval = (
            (candidate_intervals or {}).get(metric, {})
            if metric in confidence_policy.get("maximum", [])
            else {}
        )
        actual = float(interval["upper"]) if "upper" in interval else point
        add_check(
            kind="maximum",
            metric=str(metric),
            actual=actual,
            threshold=float(threshold),
            passed=actual is not None and actual <= float(threshold),
            point_estimate=point,
            bound="upper" if "upper" in interval else None,
        )
    for metric, allowed in gate.get("maximum_regression", {}).items():
        value = candidate.get(metric)
        reference_value = current.get(metric)
        actual = float(value) if isinstance(value, (int, float)) else None
        reference = (
            float(reference_value)
            if isinstance(reference_value, (int, float))
            else None
        )
        point_regression = (
            None if actual is None or reference is None else reference - actual
        )
        delta_interval = (
            (delta_intervals or {}).get(metric, {})
            if metric in confidence_policy.get("maximum_regression", [])
            else {}
        )
        regression = (
            -float(delta_interval["lower"])
            if "lower" in delta_interval
            else point_regression
        )
        add_check(
            kind="maximum_regression",
            metric=str(metric),
            actual=regression,
            threshold=float(allowed),
            reference=reference,
            passed=regression is not None and regression <= float(allowed),
            point_estimate=point_regression,
            bound="paired_lower" if "lower" in delta_interval else None,
        )
    baseline_limits = gate.get("baseline_maximum_regression", {})
    if baseline is not None:
        for metric, allowed in baseline_limits.items():
            value = candidate.get(metric)
            reference_value = baseline.get(metric)
            actual = float(value) if isinstance(value, (int, float)) else None
            reference = (
                float(reference_value)
                if isinstance(reference_value, (int, float))
                else None
            )
            regression = (
                None if actual is None or reference is None else reference - actual
            )
            add_check(
                kind="baseline_maximum_regression",
                metric=str(metric),
                actual=regression,
                threshold=float(allowed),
                reference=reference,
                passed=regression is not None and regression <= float(allowed),
                point_estimate=regression,
            )
    elif baseline_limits and gate.get("require_baseline", False):
        add_check(
            kind="baseline_required",
            metric="baseline",
            actual=None,
            threshold=1.0,
            passed=False,
        )
    for sample_name, minimum in gate.get("minimum_samples", {}).items():
        metric = (
            "sample_count" if str(sample_name) == "validation" else f"{sample_name}_sample_count"
        )
        value = candidate.get(metric)
        actual = float(value) if isinstance(value, (int, float)) else None
        add_check(
            kind="minimum_samples",
            metric=metric,
            actual=actual,
            threshold=float(minimum),
            passed=actual is not None and actual >= float(minimum),
        )
    minimum_slice_samples = int(gate.get("minimum_slice_samples", 1))
    for dimension, metric_limits in gate.get("slice_maximum_regression", {}).items():
        candidate_groups = (candidate_slices or {}).get(dimension, {})
        current_groups = (current_slices or {}).get(dimension, {})
        group_names = sorted(set(candidate_groups) | set(current_groups))
        for group_name in group_names:
            candidate_group = candidate_groups.get(group_name, {})
            current_group = current_groups.get(group_name, {})
            candidate_count = int(candidate_group.get("sample_count", 0))
            current_count = int(current_group.get("sample_count", 0))
            if min(candidate_count, current_count) < minimum_slice_samples:
                continue
            for metric, allowed in metric_limits.items():
                candidate_value = candidate_group.get(metric)
                current_value = current_group.get(metric)
                if not isinstance(candidate_value, (int, float)) and not isinstance(
                    current_value, (int, float)
                ):
                    continue
                actual = (
                    float(current_value) - float(candidate_value)
                    if isinstance(candidate_value, (int, float))
                    and isinstance(current_value, (int, float))
                    else None
                )
                add_check(
                    kind="slice_maximum_regression",
                    metric=str(metric),
                    actual=actual,
                    threshold=float(allowed),
                    reference=(
                        float(current_value)
                        if isinstance(current_value, (int, float))
                        else None
                    ),
                    point_estimate=actual,
                    passed=actual is not None and actual <= float(allowed),
                    slice_dimension=str(dimension),
                    slice_name=str(group_name),
                )
    return {"passed": all(check["passed"] for check in checks), "checks": checks}
