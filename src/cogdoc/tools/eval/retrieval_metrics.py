import math
from statistics import mean
from typing import Any, Dict, List, Mapping, Sequence

RECOMMENDED_RETRIEVAL_LAYERS = (
    "single-source",
    "multi-source",
    "hard",
    "no-answer",
)
RETRIEVAL_COVERAGE_PROFILES = {
    "smoke": {layer: 1 for layer in RECOMMENDED_RETRIEVAL_LAYERS},
    "baseline": {
        "single-source": 40,
        "multi-source": 20,
        "hard": 20,
        "no-answer": 20,
    },
}
ANNOTATION_MINIMUM_PREFIX = "annotation:"
EVIDENCE_SAMPLE_KINDS = (
    "evidence_requirements",
    "gold_requirements",
    "chunk_gold",
    "span_gold",
    "hard_negatives",
)
# ``smoke`` only proves that a clean checkout has a structurally runnable eval set.
# A release baseline additionally needs enough independent annotated queries for an
# evidence metric to be representative instead of being determined by one example.
RETRIEVAL_ANNOTATION_COVERAGE_PROFILES = {
    "smoke": {kind: 0 for kind in EVIDENCE_SAMPLE_KINDS},
    "baseline": {
        "evidence_requirements": 20,
        "gold_requirements": 20,
        "chunk_gold": 20,
        "span_gold": 10,
        "hard_negatives": 10,
    },
}
DEFAULT_EVIDENCE_METRIC_MINIMUM_SAMPLES = dict(
    RETRIEVAL_ANNOTATION_COVERAGE_PROFILES["baseline"]
)
LOWER_IS_BETTER_PREFIXES = (
    "latency_",
    "no_answer_false_positive@",
    "no_evidence_unit_false_positive@",
    "evidence_span_fallback_rate",
    "requirement_coverage_abstention_rate",
)


def _string_values(value: Any) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return ()
    return tuple(str(item).strip() for item in value if str(item).strip())


def _valid_evidence_requirements(value: Any) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return ()
    valid = []
    for requirement in value[:3]:
        if not isinstance(requirement, Mapping):
            continue
        if all(
            str(requirement.get(field) or "").strip()
            for field in (
                "requirement_id",
                "question",
                "retrieval_query",
                "recovery_query",
            )
        ):
            valid.append(requirement)
    return tuple(valid)


def _valid_gold_requirements(value: Any) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return ()
    return tuple(
        requirement
        for requirement in value
        if isinstance(requirement, Mapping)
        and str(requirement.get("requirement_id") or "").strip()
        and (
            _string_values(requirement.get("acceptable_chunk_ids"))
            or _string_values(requirement.get("acceptable_sources"))
        )
    )


def _has_valid_gold_span(requirement: Mapping[str, Any]) -> bool:
    if not str(requirement.get("requirement_id") or "").strip():
        return False
    raw_spans = requirement.get("acceptable_spans")
    if not isinstance(raw_spans, Sequence) or isinstance(
        raw_spans, (str, bytes, bytearray)
    ):
        return False
    for span in raw_spans:
        if not isinstance(span, Mapping):
            continue
        chunk_id = str(span.get("chunk_id") or "").strip()
        start = span.get("start")
        end = span.get("end")
        if (
            start is None
            or end is None
            or isinstance(start, bool)
            or isinstance(end, bool)
        ):
            continue
        try:
            normalized_start = int(start)
            normalized_end = int(end)
        except (TypeError, ValueError, OverflowError):
            continue
        if chunk_id and normalized_start >= 0 and normalized_end > normalized_start:
            return True
    return False


def annotation_coverage_stats(items: Sequence[Mapping[str, Any]]) -> dict:
    """Count effective annotated queries and annotation units.

    Query counts are the denominators used to decide whether aggregate metrics are
    mature enough for a release gate. Unit counts expose annotation depth without
    allowing several requirements in one query to masquerade as independent samples.
    """

    effective_sample_counts = {kind: 0 for kind in EVIDENCE_SAMPLE_KINDS}
    effective_annotation_counts = {kind: 0 for kind in EVIDENCE_SAMPLE_KINDS}
    declared_sample_counts = {kind: 0 for kind in EVIDENCE_SAMPLE_KINDS}
    for item in items:
        evidence_requirements = _valid_evidence_requirements(
            item.get("evidence_requirements")
        )
        gold_requirements = _valid_gold_requirements(item.get("gold_requirements"))
        raw_gold = item.get("gold_requirements")
        all_gold_mappings = (
            tuple(row for row in raw_gold if isinstance(row, Mapping))
            if isinstance(raw_gold, Sequence)
            and not isinstance(raw_gold, (str, bytes, bytearray))
            else ()
        )
        chunk_gold = tuple(
            requirement
            for requirement in gold_requirements
            if _string_values(requirement.get("acceptable_chunk_ids"))
        )
        span_gold = tuple(
            requirement
            for requirement in all_gold_mappings
            if _has_valid_gold_span(requirement)
        )
        hard_negatives = (
            _string_values(item.get("hard_negative_chunk_ids"))
            if gold_requirements
            else ()
        )
        annotations = {
            "evidence_requirements": evidence_requirements,
            "gold_requirements": gold_requirements,
            "chunk_gold": chunk_gold,
            "span_gold": span_gold,
            "hard_negatives": hard_negatives,
        }
        declared = {
            "evidence_requirements": bool(item.get("evidence_requirements")),
            "gold_requirements": bool(item.get("gold_requirements")),
            "chunk_gold": any(
                isinstance(requirement, Mapping)
                and bool(requirement.get("acceptable_chunk_ids"))
                for requirement in all_gold_mappings
            ),
            "span_gold": any(
                isinstance(requirement, Mapping)
                and bool(requirement.get("acceptable_spans"))
                for requirement in all_gold_mappings
            ),
            "hard_negatives": bool(item.get("hard_negative_chunk_ids")),
        }
        for kind, values in annotations.items():
            if values:
                effective_sample_counts[kind] += 1
                effective_annotation_counts[kind] += len(values)
            if declared[kind]:
                declared_sample_counts[kind] += 1

    return {
        "effective_sample_counts": effective_sample_counts,
        "effective_annotation_counts": effective_annotation_counts,
        "declared_sample_counts": declared_sample_counts,
        "invalid_sample_counts": {
            kind: max(
                0,
                declared_sample_counts[kind] - effective_sample_counts[kind],
            )
            for kind in EVIDENCE_SAMPLE_KINDS
        },
    }


def _minimum_count(value: Any, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} 的最小样本数必须是非负整数")
    return value


def evidence_metric_minimums(
    overrides: Mapping[str, Any] | None = None,
) -> Dict[str, int]:
    minimums = dict(DEFAULT_EVIDENCE_METRIC_MINIMUM_SAMPLES)
    for kind, raw_count in (overrides or {}).items():
        if kind not in EVIDENCE_SAMPLE_KINDS:
            raise ValueError(f"未知证据标注类型: {kind}")
        minimums[kind] = _minimum_count(raw_count, name=kind)
    return minimums


def evidence_metric_sample_kind(metric: str) -> str | None:
    if metric.startswith(
        (
            "requirement_recall@",
            "all_requirements_covered@",
            "evidence_unit_recall@",
            "all_evidence_units_covered@",
            "evidence_ndcg@",
        )
    ) or metric in {
        "evidence_pack_requirement_coverage_post",
        "generation_requirement_coverage",
    }:
        return "gold_requirements"
    if metric.startswith("chunk_precision@"):
        return "chunk_gold"
    if metric == "evidence_span_gold_recall_post":
        return "span_gold"
    if metric.startswith(
        ("hard_negative_rejection@", "evidence_unit_hard_negative_rejection@")
    ):
        return "hard_negatives"
    if metric == "requirement_full_coverage_rate":
        return "evidence_requirements"
    return None


# 计算atk。
def recall_at_k(
    retrieved_sources: Sequence[str], expected_sources: Sequence[str], k: int
) -> float:
    expected = set(expected_sources)
    if not expected:
        return 0.0
    top = set(retrieved_sources[:k])
    return len(expected & top) / len(expected)


# 计算atk。
def hit_at_k(
    retrieved_sources: Sequence[str], expected_sources: Sequence[str], k: int
) -> float:
    expected = set(expected_sources)
    if not expected:
        return 0.0
    return 1.0 if expected & set(retrieved_sources[:k]) else 0.0


# 计算排序。
def reciprocal_rank(
    retrieved_sources: Sequence[str], expected_sources: Sequence[str]
) -> float:
    expected = set(expected_sources)
    for rank, source in enumerate(retrieved_sources, start=1):
        if source in expected:
            return 1.0 / rank
    return 0.0


# 评估问题。
def evaluate_query(
    retrieved_sources: Sequence[str],
    expected_sources: Sequence[str],
    k_values: Sequence[int],
) -> Dict[str, float]:
    if not expected_sources:
        return {
            f"no_answer_false_positive@{k}": (1.0 if retrieved_sources[:k] else 0.0)
            for k in k_values
        }

    metrics: Dict[str, float] = {
        "mrr": reciprocal_rank(retrieved_sources, expected_sources)
    }
    for k in k_values:
        metrics[f"recall@{k}"] = recall_at_k(retrieved_sources, expected_sources, k)
        metrics[f"hit@{k}"] = hit_at_k(retrieved_sources, expected_sources, k)
    return metrics


def _identity(item: Mapping[str, Any]) -> tuple[str, str]:
    return str(item.get("chunk_id") or ""), str(item.get("source") or "")


def _item_unit_ids(item: Mapping[str, Any]) -> set[str]:
    raw_ids = item.get("matched_unit_ids") or item.get("matched_requirement_ids")
    if not isinstance(raw_ids, Sequence) or isinstance(
        raw_ids, (str, bytes, bytearray)
    ):
        return set()
    return {str(value).strip() for value in raw_ids if str(value).strip()}


def evaluate_evidence_unit_outcomes(
    retrieved_items: Sequence[Mapping[str, Any]],
    expected_unit_statuses: Mapping[str, Any] | None,
    k_values: Sequence[int],
    *,
    hard_negative_chunk_ids_by_unit: Mapping[str, Any] | None = None,
) -> Dict[str, float]:
    """Evaluate mixed supported/no-evidence units without a global no-answer flag."""

    statuses = {
        str(unit_id).strip(): str(status).strip()
        for unit_id, status in (expected_unit_statuses or {}).items()
        if str(unit_id).strip() and str(status).strip() in {"supported", "no_evidence"}
    }
    if not statuses:
        return {}
    no_evidence_ids = [
        unit_id for unit_id, status in statuses.items() if status == "no_evidence"
    ]
    raw_negatives = hard_negative_chunk_ids_by_unit or {}
    negatives_by_unit = {
        unit_id: set(_string_values(raw_negatives.get(unit_id))) for unit_id in statuses
    }
    negative_annotated_ids = [
        unit_id for unit_id, values in negatives_by_unit.items() if values
    ]

    metrics: Dict[str, float] = {
        "evidence_unit_count": float(len(statuses)),
        "supported_evidence_unit_count": float(
            sum(status == "supported" for status in statuses.values())
        ),
        "no_evidence_unit_count": float(len(no_evidence_ids)),
    }
    for k in k_values:
        top_items = list(retrieved_items[:k])
        if no_evidence_ids:
            false_positives = 0
            for unit_id in no_evidence_ids:
                false_positives += any(
                    unit_id in _item_unit_ids(item) for item in top_items
                )
            metrics[f"no_evidence_unit_false_positive@{k}"] = false_positives / len(
                no_evidence_ids
            )
        if negative_annotated_ids:
            rejected = 0
            for unit_id in negative_annotated_ids:
                negatives = negatives_by_unit[unit_id]
                hit = any(
                    _identity(item)[0] in negatives
                    and (not _item_unit_ids(item) or unit_id in _item_unit_ids(item))
                    for item in top_items
                )
                rejected += not hit
            metrics[f"evidence_unit_hard_negative_rejection@{k}"] = rejected / len(
                negative_annotated_ids
            )
    return metrics


def _requirement_is_covered(
    top_items: Sequence[Mapping[str, Any]], requirement: Mapping[str, Any]
) -> bool:
    expected_chunks = {
        str(value)
        for value in requirement.get("acceptable_chunk_ids", [])
        if str(value)
    }
    expected_sources = {
        str(value) for value in requirement.get("acceptable_sources", []) if str(value)
    }
    for item in top_items:
        chunk_id, source = _identity(item)
        if (chunk_id and chunk_id in expected_chunks) or (
            source and source in expected_sources
        ):
            return True
    return False


# 计算一个有界上下文对全部 gold requirements 的覆盖比例。
def requirement_coverage_rate(
    retrieved: Sequence[Mapping[str, Any]],
    requirements: Sequence[Mapping[str, Any]],
) -> float:
    effective_requirements = _valid_gold_requirements(requirements)
    if not effective_requirements:
        return 0.0
    covered = sum(
        _requirement_is_covered(retrieved, requirement)
        for requirement in effective_requirements
    )
    return covered / len(effective_requirements)


def _requirement_mask(
    item: Mapping[str, Any], requirements: Sequence[Mapping[str, Any]]
) -> int:
    chunk_id, source = _identity(item)
    mask = 0
    for index, requirement in enumerate(requirements):
        expected_chunks = {
            str(value)
            for value in requirement.get("acceptable_chunk_ids", [])
            if str(value)
        }
        expected_sources = {
            str(value)
            for value in requirement.get("acceptable_sources", [])
            if str(value)
        }
        if (chunk_id and chunk_id in expected_chunks) or (
            source and source in expected_sources
        ):
            mask |= 1 << index
    return mask


def _ideal_requirement_masks(
    requirements: Sequence[Mapping[str, Any]], actual_masks: Sequence[int]
) -> List[int]:
    # 同一 gold identity 可同时覆盖多个需求；按 identity 合并 mask，才能正确表示
    # “一个 chunk 覆盖两个需求”的理想排序。
    by_identity: Dict[tuple[str, str], int] = {}
    for index, requirement in enumerate(requirements):
        bit = 1 << index
        for chunk_id in requirement.get("acceptable_chunk_ids", []):
            identity = ("chunk", str(chunk_id))
            if identity[1]:
                by_identity[identity] = by_identity.get(identity, 0) | bit
        for source in requirement.get("acceptable_sources", []):
            identity = ("source", str(source))
            if identity[1]:
                by_identity[identity] = by_identity.get(identity, 0) | bit

    # 实际 item 可能同时用 chunk 和 source 命中不同需求；将其可实现 mask 纳入
    # 理想候选，保证 IDCG 不会低于当前排序本身可实现的增益。
    return sorted({mask for mask in (*by_identity.values(), *actual_masks) if mask})


def _requirement_ndcg(
    actual_masks: Sequence[int], ideal_masks: Sequence[int], k: int
) -> float:
    covered = 0
    dcg = 0.0
    for rank, mask in enumerate(actual_masks[:k], start=1):
        new_requirements = (mask & ~covered).bit_count()
        dcg += new_requirements / math.log2(rank + 1)
        covered |= mask

    if not ideal_masks or k <= 0:
        return 0.0

    # requirement 数通常不超过 3；按覆盖 mask 做精确 DP，得到允许一块覆盖多个
    # 需求时真正可实现的 IDCG，而不是假定每个 rank 只能贡献 1。
    best_by_covered = {0: 0.0}
    for rank in range(1, k + 1):
        discount = math.log2(rank + 1)
        next_best = dict(best_by_covered)
        for current_mask, score in best_by_covered.items():
            for candidate_mask in ideal_masks:
                combined = current_mask | candidate_mask
                gain = (candidate_mask & ~current_mask).bit_count() / discount
                next_best[combined] = max(next_best.get(combined, 0.0), score + gain)
        best_by_covered = next_best
    ideal = max(best_by_covered.values(), default=0.0)
    return min(dcg / ideal, 1.0) if ideal else 0.0


# 以原子证据需求和 chunk 级标注评估真实证据覆盖，避免“命中正确 PDF 的错误块”被算作成功。
def evaluate_requirement_coverage(
    retrieved_items: Sequence[Mapping[str, Any]],
    gold_requirements: Sequence[Mapping[str, Any]],
    k_values: Sequence[int],
    *,
    hard_negative_chunk_ids: Sequence[str] = (),
) -> Dict[str, float]:
    requirements = list(_valid_gold_requirements(gold_requirements))
    if not requirements:
        return {}

    expected_chunk_ids = {
        str(chunk_id)
        for requirement in requirements
        for chunk_id in requirement.get("acceptable_chunk_ids", [])
        if str(chunk_id)
    }
    expected_sources = {
        str(source)
        for requirement in requirements
        for source in requirement.get("acceptable_sources", [])
        if str(source)
    }
    hard_negatives = {str(value) for value in hard_negative_chunk_ids if str(value)}
    metrics: Dict[str, float] = {}
    for k in k_values:
        top_items = list(retrieved_items[:k])
        covered = sum(
            _requirement_is_covered(top_items, requirement)
            for requirement in requirements
        )
        metrics[f"requirement_recall@{k}"] = covered / len(requirements)
        metrics[f"all_requirements_covered@{k}"] = float(covered == len(requirements))
        # Generic aliases let Summary/Compare use the same release reports
        # without pretending that every atomic unit is a QA requirement.
        metrics[f"evidence_unit_recall@{k}"] = metrics[f"requirement_recall@{k}"]
        metrics[f"all_evidence_units_covered@{k}"] = metrics[
            f"all_requirements_covered@{k}"
        ]

        relevances = []
        for item in top_items:
            chunk_id, source = _identity(item)
            relevances.append(
                float(
                    bool(
                        (chunk_id and chunk_id in expected_chunk_ids)
                        or (source and source in expected_sources)
                    )
                )
            )
        actual_masks = [
            _requirement_mask(item, requirements) for item in retrieved_items
        ]
        metrics[f"evidence_ndcg@{k}"] = _requirement_ndcg(
            actual_masks,
            _ideal_requirement_masks(requirements, actual_masks),
            k,
        )
        if expected_chunk_ids:
            metrics[f"chunk_precision@{k}"] = (
                sum(relevances) / len(top_items) if top_items else 0.0
            )
        if hard_negatives:
            hard_negative_hit = any(
                _identity(item)[0] in hard_negatives for item in top_items
            )
            metrics[f"hard_negative_rejection@{k}"] = float(not hard_negative_hit)
    return metrics


# 聚合结果。
def aggregate(per_query_metrics: List[Dict[str, float]]) -> Dict[str, float]:
    if not per_query_metrics:
        return {}
    keys = sorted({key for metrics in per_query_metrics for key in metrics})
    return {
        key: mean(metrics[key] for metrics in per_query_metrics if key in metrics)
        for key in keys
    }


# 使用 nearest-rank 定义计算百分位，避免引入额外数值依赖。
def percentile(values: Sequence[float], percent: float) -> float:
    if not values:
        raise ValueError("percentile requires at least one value")
    if not 0 < percent <= 100:
        raise ValueError("percent must be in (0, 100]")
    ordered = sorted(float(value) for value in values)
    rank = max(1, math.ceil((percent / 100.0) * len(ordered)))
    return ordered[rank - 1]


# 返回指标优化方向，供基线比较正确处理延迟和误命中率。
def metric_direction(metric: str) -> str:
    if metric.startswith(LOWER_IS_BETTER_PREFIXES):
        return "lower"
    return "higher"


# 获取覆盖配置对应的每层最小样本数。
def coverage_minimums(
    profile: str,
    *,
    annotation_minimums: Mapping[str, Any] | None = None,
) -> Dict[str, int]:
    if profile not in RETRIEVAL_COVERAGE_PROFILES:
        raise ValueError(f"未知检索覆盖配置: {profile}")
    minimums = dict(RETRIEVAL_COVERAGE_PROFILES[profile])
    evidence_minimums = dict(RETRIEVAL_ANNOTATION_COVERAGE_PROFILES[profile])
    for kind, raw_count in (annotation_minimums or {}).items():
        if kind not in EVIDENCE_SAMPLE_KINDS:
            raise ValueError(f"未知证据标注类型: {kind}")
        evidence_minimums[kind] = _minimum_count(raw_count, name=kind)
    minimums.update(
        {
            f"{ANNOTATION_MINIMUM_PREFIX}{kind}": count
            for kind, count in evidence_minimums.items()
        }
    )
    return minimums


# 推断检索评测样本层级。
def infer_retrieval_layer(item: dict) -> str:
    expected = item.get("expected_sources", [])
    if not expected:
        return "no-answer"
    if len(set(expected)) > 1:
        return "multi-source"
    return "single-source"


# 审计检索评测集覆盖面。
def audit_coverage(
    items: List[dict], minimum_counts: Mapping[str, int] | None = None
) -> dict:
    combined_minimums = dict(
        coverage_minimums("smoke") if minimum_counts is None else minimum_counts
    )
    minimums = {
        key: _minimum_count(value, name=key)
        for key, value in combined_minimums.items()
        if not key.startswith(ANNOTATION_MINIMUM_PREFIX)
    }
    annotation_minimums = {kind: 0 for kind in EVIDENCE_SAMPLE_KINDS}
    for key, value in combined_minimums.items():
        if not key.startswith(ANNOTATION_MINIMUM_PREFIX):
            continue
        kind = key.removeprefix(ANNOTATION_MINIMUM_PREFIX)
        if kind not in EVIDENCE_SAMPLE_KINDS:
            raise ValueError(f"未知证据标注类型: {kind}")
        annotation_minimums[kind] = _minimum_count(value, name=kind)
    layer_counts: Dict[str, int] = {}
    for item in items:
        layer = str(item.get("layer") or infer_retrieval_layer(item))
        layer_counts[layer] = layer_counts.get(layer, 0) + 1
    missing_layers = [layer for layer in minimums if layer_counts.get(layer, 0) == 0]
    insufficient_layers = {
        layer: {
            "actual": layer_counts.get(layer, 0),
            "required": required,
        }
        for layer, required in minimums.items()
        if layer_counts.get(layer, 0) < required
    }
    annotation_stats = annotation_coverage_stats(items)
    effective_sample_counts = annotation_stats["effective_sample_counts"]
    missing_annotations = [
        kind
        for kind, required in annotation_minimums.items()
        if required > 0 and effective_sample_counts[kind] == 0
    ]
    insufficient_annotations = {
        kind: {
            "actual": effective_sample_counts[kind],
            "required": required,
        }
        for kind, required in annotation_minimums.items()
        if effective_sample_counts[kind] < required
    }
    return {
        "layers": sorted(layer_counts),
        "layer_counts": dict(sorted(layer_counts.items())),
        "minimum_layer_counts": minimums,
        "missing_layers": missing_layers,
        "insufficient_layers": insufficient_layers,
        **annotation_stats,
        "minimum_annotation_counts": annotation_minimums,
        "missing_annotations": missing_annotations,
        "insufficient_annotations": insufficient_annotations,
        "total_count": len(items),
        "is_coverage_complete": not insufficient_layers
        and not insufficient_annotations,
    }


# 根据绝对阈值生成门禁结果。minimum 指标越大越好，maximum 指标越小越好。
def evaluate_thresholds(
    aggregate_metrics: Mapping[str, float],
    config: dict,
    *,
    metric_denominators: Mapping[str, int] | None = None,
    minimum_samples: Mapping[str, Any] | None = None,
) -> dict:
    rows = []
    raw_configured_minimums = config.get("minimum_samples", {})
    if not isinstance(raw_configured_minimums, Mapping):
        raise ValueError("minimum_samples 必须是对象")
    configured_minimums = evidence_metric_minimums(minimum_samples)
    configured_minimums = evidence_metric_minimums(
        {**configured_minimums, **dict(raw_configured_minimums)}
    )
    raw_metric_minimums = config.get("metric_minimum_samples", {})
    if not isinstance(raw_metric_minimums, Mapping):
        raise ValueError("metric_minimum_samples 必须是对象")
    bounds = (
        ("minimum", lambda current, limit: current >= limit),
        ("maximum", lambda current, limit: current <= limit),
    )
    for bound_name, comparator in bounds:
        for metric, raw_limit in sorted(config.get(bound_name, {}).items()):
            current = aggregate_metrics.get(metric)
            limit = float(raw_limit)
            sample_kind = evidence_metric_sample_kind(metric)
            required_samples: int | None = None
            if sample_kind is not None:
                required_samples = configured_minimums[sample_kind]
            if metric in raw_metric_minimums:
                required_samples = _minimum_count(
                    raw_metric_minimums[metric], name=metric
                )
            sample_count = (
                metric_denominators.get(metric)
                if metric_denominators is not None
                else None
            )
            samples_sufficient = required_samples is None or required_samples == 0
            if required_samples and sample_count is not None:
                samples_sufficient = sample_count >= required_samples
            passed = (
                current is not None
                and samples_sufficient
                and comparator(float(current), limit)
            )
            rows.append(
                {
                    "metric": metric,
                    "bound": bound_name,
                    "limit": limit,
                    "current": current,
                    "sample_kind": sample_kind,
                    "sample_count": sample_count,
                    "minimum_samples": required_samples,
                    "passed": passed,
                    "failure_reason": (
                        "metric_missing"
                        if current is None
                        else "insufficient_samples"
                        if not samples_sufficient
                        else "threshold_not_met"
                        if not passed
                        else ""
                    ),
                }
            )
    return {"passed": bool(rows) and all(row["passed"] for row in rows), "rows": rows}
