from __future__ import annotations

import itertools
import statistics
from collections.abc import Mapping, Sequence
from typing import Any

from cogdoc.tools.eval.multi_route_eval import ROUTES, ranking_metrics, requirement_coverage, weighted_rrf


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
                rows = []
                for case in cases:
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
                objective = _objective(rows)
                candidate = {
                    "objective": objective,
                    "route_weights": weights,
                    "top_k": top_k,
                    "route_min_candidates": route_min,
                    "metrics": {
                        key: statistics.mean(row[key] for row in rows)
                        for key in ("recall", "mrr", "ndcg", "coverage")
                    }
                    if rows
                    else {},
                }
                trials += 1
                if best is None or (objective, str(candidate)) > (
                    best["objective"], str(best)
                ):
                    best = candidate
    if best is None:
        raise ValueError("calibration requires at least one evaluation case")
    return {**best, "trial_count": trials}


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
