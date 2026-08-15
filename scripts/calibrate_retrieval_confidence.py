#!/usr/bin/env python3
"""Calibrate abstention thresholds from retrieval-evaluation report rows."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any, Mapping, Sequence


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _fold(row: Mapping[str, Any], fold_count: int = 5) -> int:
    identity = str(row.get("id") or row.get("query") or "")
    return int(hashlib.sha256(identity.encode()).hexdigest()[:8], 16) % fold_count


def _rates(
    rows: Sequence[Mapping[str, Any]], *, max_distance: float, min_bm25: float
) -> dict[str, float | int]:
    tp = tn = fp = fn = 0
    for row in rows:
        signals = row.get("retrieval_signals")
        signals = signals if isinstance(signals, Mapping) else {}
        distance = _finite(signals.get("distance"))
        bm25 = _finite(signals.get("bm25_score"))
        predicted = bool(
            (distance is not None and distance <= max_distance)
            or (bm25 is not None and bm25 >= min_bm25)
        )
        answerable = bool(row.get("expected_sources"))
        if answerable and predicted:
            tp += 1
        elif answerable:
            fn += 1
        elif predicted:
            fp += 1
        else:
            tn += 1
    tpr = tp / max(tp + fn, 1)
    tnr = tn / max(tn + fp, 1)
    return {
        "sample_count": tp + tn + fp + fn,
        "answerable_acceptance_rate": tpr,
        "no_answer_abstention_rate": tnr,
        "balanced_accuracy": (tpr + tnr) / 2,
        "false_accept_rate": fp / max(fp + tn, 1),
        "false_reject_rate": fn / max(fn + tp, 1),
    }


def calibrate(
    rows: Sequence[Mapping[str, Any]],
    *,
    min_answerable_acceptance: float = 0.95,
) -> dict[str, Any]:
    if not 0.0 <= min_answerable_acceptance <= 1.0:
        raise ValueError("min_answerable_acceptance must be within [0, 1]")
    distances = sorted(
        {
            value
            for row in rows
            if isinstance(row.get("retrieval_signals"), Mapping)
            if (value := _finite(row["retrieval_signals"].get("distance"))) is not None
        }
    )
    bm25_scores = sorted(
        {
            value
            for row in rows
            if isinstance(row.get("retrieval_signals"), Mapping)
            if (value := _finite(row["retrieval_signals"].get("bm25_score")))
            is not None
        }
    )
    distance_candidates = [0.0, *distances]
    bm25_candidates = [*bm25_scores, math.inf]
    if not distance_candidates or not bm25_candidates:
        raise ValueError("report rows do not contain calibratable vector/BM25 signals")
    folds = [[row for row in rows if _fold(row) == index] for index in range(5)]
    scored: list[tuple[tuple[float, ...], float, float, dict[str, Any]]] = []
    for max_distance in distance_candidates:
        for min_bm25 in bm25_candidates:
            metrics = _rates(rows, max_distance=max_distance, min_bm25=min_bm25)
            if float(metrics["answerable_acceptance_rate"]) < min_answerable_acceptance:
                continue
            fold_metrics = [
                _rates(fold, max_distance=max_distance, min_bm25=min_bm25)
                for fold in folds
            ]
            balanced = [float(item["balanced_accuracy"]) for item in fold_metrics]
            key = (
                min(balanced),
                sum(balanced) / len(balanced),
                -float(metrics["false_accept_rate"]),
                -float(metrics["false_reject_rate"]),
                -max_distance,
            )
            scored.append((key, max_distance, min_bm25, metrics))
    if not scored:
        raise ValueError("no threshold pair satisfies the answerable-acceptance floor")
    _, max_distance, min_bm25, overall_metrics = max(scored, key=lambda item: item[0])
    fold_metrics = [
        _rates(fold, max_distance=max_distance, min_bm25=min_bm25) for fold in folds
    ]
    return {
        "method": "deterministic_5_fold_worst_case_balanced_accuracy",
        "constraints": {
            "minimum_answerable_acceptance_rate": min_answerable_acceptance
        },
        "recommended": {
            "QA_ABSTAIN_MAX_VECTOR_DISTANCE": max_distance,
            "QA_ABSTAIN_MIN_BM25_SCORE": min_bm25,
        },
        "overall": overall_metrics,
        "folds": fold_metrics,
        "warning": (
            "Apply only after checking annotation coverage and validation sample size; "
            "this script never edits runtime configuration."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--report", type=Path, default=Path("eval/retrieval_eval_report.json")
    )
    parser.add_argument(
        "--json",
        type=Path,
        default=Path("artifacts/reliability/confidence-calibration.json"),
    )
    parser.add_argument("--min-answerable-acceptance", type=float, default=0.95)
    args = parser.parse_args()
    payload = json.loads(args.report.read_text(encoding="utf-8"))
    rows = payload.get("rows") if isinstance(payload, Mapping) else None
    if not isinstance(rows, list) or not rows:
        parser.error("report must contain non-empty rows")
    result = calibrate(rows, min_answerable_acceptance=args.min_answerable_acceptance)
    args.json.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.json.with_name(args.json.name + ".tmp")
    temporary.write_text(json.dumps(result, indent=2), encoding="utf-8")
    os.replace(temporary, args.json)
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
