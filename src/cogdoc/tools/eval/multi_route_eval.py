from __future__ import annotations

import hashlib
import json
import math
import statistics
from collections.abc import Mapping, Sequence
from typing import Any


ROUTES = (
    "rag_vector",
    "rag_bm25",
    "derived_knowledge_vector",
    "derived_knowledge_lexical",
)


def ablation_configs() -> dict[str, dict[str, float]]:
    enabled = {route: 1.0 for route in ROUTES}
    configs = {"all": enabled}
    for route in ROUTES:
        configs[f"only:{route}"] = {
            candidate: float(candidate == route) for candidate in ROUTES
        }
        configs[f"without:{route}"] = {
            candidate: float(candidate != route) for candidate in ROUTES
        }
    return configs


def _identity(hit: Mapping[str, Any]) -> str:
    return str(hit.get("chunk_id") or "")


def _relevant(hit: Mapping[str, Any], case: Mapping[str, Any]) -> bool:
    chunks = set(str(value) for value in case.get("expected_chunk_ids", []))
    sources = set(str(value) for value in case.get("expected_sources", []))
    return (_identity(hit) in chunks if chunks else False) or (
        str(hit.get("source") or "") in sources if sources else False
    )


def ranking_metrics(
    hits: Sequence[Mapping[str, Any]],
    case: Mapping[str, Any],
    *,
    k: int,
) -> dict[str, float]:
    expected = set(str(value) for value in case.get("expected_chunk_ids", []))
    if not expected:
        expected = set(str(value) for value in case.get("expected_sources", []))
        retrieved = [str(hit.get("source") or "") for hit in hits[:k]]
    else:
        retrieved = [_identity(hit) for hit in hits[:k]]
    matched = expected & set(retrieved)
    recall = len(matched) / len(expected) if expected else 0.0
    reciprocal_rank = 0.0
    gains = []
    seen_relevant: set[str] = set()
    for rank, hit in enumerate(hits[:k], start=1):
        identity = _identity(hit) if case.get("expected_chunk_ids") else str(
            hit.get("source") or ""
        )
        relevant = _relevant(hit, case) and identity not in seen_relevant
        if relevant:
            seen_relevant.add(identity)
        gains.append(float(relevant))
        if relevant and reciprocal_rank == 0:
            reciprocal_rank = 1.0 / rank
    dcg = sum(gain / math.log2(rank + 1) for rank, gain in enumerate(gains, start=1))
    ideal_count = min(k, len(expected))
    idcg = sum(1.0 / math.log2(rank + 1) for rank in range(1, ideal_count + 1))
    return {
        f"recall@{k}": recall,
        "mrr": reciprocal_rank,
        f"ndcg@{k}": dcg / idcg if idcg else 0.0,
    }


def requirement_coverage(
    hits: Sequence[Mapping[str, Any]], requirements: Sequence[Mapping[str, Any]]
) -> float:
    if not requirements:
        return 1.0
    covered = 0
    for requirement in requirements:
        chunks = set(str(value) for value in requirement.get("acceptable_chunk_ids", []))
        sources = set(str(value) for value in requirement.get("acceptable_sources", []))
        if any(
            (_identity(hit) in chunks if chunks else False)
            or (str(hit.get("source") or "") in sources if sources else False)
            for hit in hits
        ):
            covered += 1
    return covered / len(requirements)


def percentile(values: Sequence[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * fraction) - 1))
    return ordered[position]


def aggregate_case_metrics(rows: Sequence[Mapping[str, Any]]) -> dict[str, float]:
    numeric: dict[str, list[float]] = {}
    latencies = []
    for row in rows:
        for key, value in row.get("metrics", {}).items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                numeric.setdefault(str(key), []).append(float(value))
        latency = row.get("latency_ms")
        if isinstance(latency, (int, float)) and not isinstance(latency, bool):
            latencies.append(float(latency))
    result = {key: statistics.mean(values) for key, values in numeric.items() if values}
    result["latency_p50_ms"] = percentile(latencies, 0.5)
    result["latency_p95_ms"] = percentile(latencies, 0.95)
    result["sample_count"] = float(len(rows))
    return result


def layered_metrics(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    dimensions = ("query_type", "doc_type", "chunk_type")
    output: dict[str, Any] = {}
    for dimension in dimensions:
        groups: dict[str, list[Mapping[str, Any]]] = {}
        for row in rows:
            key = str(row.get(dimension) or "unknown")
            groups.setdefault(key, []).append(row)
        output[dimension] = {
            key: aggregate_case_metrics(group) for key, group in sorted(groups.items())
        }
    return output


def report_sha256(value: Mapping[str, Any]) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def weighted_rrf(
    routes: Mapping[str, Sequence[Mapping[str, Any]]],
    weights: Mapping[str, float],
    *,
    rrf_k: float,
    top_k: int,
    per_route_min: int,
) -> list[dict[str, Any]]:
    candidates: dict[str, dict[str, Any]] = {}
    for route, hits in routes.items():
        weight = float(weights.get(route, 0.0))
        if weight <= 0:
            continue
        seen: set[str] = set()
        for rank, hit in enumerate(hits, start=1):
            identity = _identity(hit)
            if not identity or identity in seen:
                continue
            seen.add(identity)
            state = candidates.setdefault(
                identity, {**hit, "score": 0.0, "matched_routes": []}
            )
            state["score"] += weight / (rrf_k + rank)
            state["matched_routes"].append(route)
    ordered = sorted(candidates.values(), key=lambda item: (-item["score"], _identity(item)))
    if top_k <= 0:
        return []
    selected: set[int] = set()
    if per_route_min:
        for route, weight in weights.items():
            if weight <= 0:
                continue
            for index, item in enumerate(ordered):
                if len(selected) >= top_k:
                    break
                if route in item["matched_routes"]:
                    selected.add(index)
                    break
    for index in range(len(ordered)):
        if len(selected) >= top_k:
            break
        selected.add(index)
    return [item for index, item in enumerate(ordered) if index in selected]
