import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from cogdoc.tools.eval.claim_verification_eval import (  # noqa: E402
    DEFAULT_BOOTSTRAP_ITERATIONS,
    DEFAULT_BOOTSTRAP_SEED,
    DEFAULT_CONFIDENCE_LEVEL,
    evaluate_gate,
    run_eval,
)


DEFAULT_EVAL_SET = ROOT / "eval" / "claim_verification_eval.jsonl"
EXAMPLE_EVAL_SET = ROOT / "eval" / "claim_verification_eval.example.jsonl"


def resolve_eval_set(requested: Path | None) -> Path:
    if requested is not None:
        return requested
    if DEFAULT_EVAL_SET.exists():
        return DEFAULT_EVAL_SET
    print(
        f"未找到本地声明核验集 {DEFAULT_EVAL_SET}，回退到示例 "
        f"{EXAMPLE_EVAL_SET.name}。"
    )
    return EXAMPLE_EVAL_SET


def load_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    seen: set[str] = set()
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = raw_line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{line_number}: invalid JSON: {exc.msg}") from exc
        if not isinstance(value, dict):
            raise ValueError(f"{path}:{line_number}: each row must be an object")
        case_id = str(value.get("id") or "").strip()
        if case_id and case_id in seen:
            raise ValueError(f"{path}:{line_number}: duplicate case id: {case_id}")
        seen.add(case_id)
        rows.append(value)
    return rows


def _atomic_write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _fmt(value: object) -> str:
    if value is None:
        return "-"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


def print_summary(report: dict) -> None:
    aggregate = report["aggregate"]
    print(f"\n声明核验评测 | cases={aggregate['sample_count']}")
    for metric in (
        "observable_rate",
        "exact_accuracy",
        "support_precision",
        "support_recall",
        "unsafe_accept_rate",
        "unsafe_rejection_recall",
        "not_factual_recall",
        "unobservable_fail_closed_rate",
        "latency_p95_ms",
    ):
        print(f"  {metric:<34} {_fmt(aggregate.get(metric))}")
    print("\n按层级:")
    for layer, metrics in report["by_layer"].items():
        print(
            f"  {layer:<14} n={metrics['sample_count']:<4} "
            f"support_recall={_fmt(metrics['support_recall'])} "
            f"unsafe_accept={_fmt(metrics['unsafe_accept_rate'])}"
        )
    gate = report.get("gate")
    if isinstance(gate, dict):
        state = "PASS" if gate.get("passed") else "FAIL"
        print(
            f"\n发布门禁: {state} "
            f"(failed_checks={gate.get('failed_check_count', 0)})"
        )
        for check in gate.get("checks", []):
            if check.get("passed"):
                continue
            layer = f" layer={check['layer']}" if check.get("layer") else ""
            print(
                f"  - {check['kind']} {check['metric']}{layer}: "
                f"actual={_fmt(check.get('actual'))}, "
                f"threshold={_fmt(check.get('threshold'))}"
            )
    print()


def _baseline_artifact(report: dict, *, source_eval_set: Path) -> dict:
    return {
        "schema_version": "claim_verification_baseline_v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_eval_set": str(source_eval_set),
        "eval_contract_sha256": report["config"]["eval_contract_sha256"],
        "accepted_metrics": report["aggregate"],
        "by_layer": report["by_layer"],
        "confidence": report["confidence"],
        "gate": report.get("gate"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="离线声明语义核验发布门禁")
    parser.add_argument("--eval-set", type=Path)
    parser.add_argument("--gate", type=Path)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--promote-baseline", type=Path)
    parser.add_argument(
        "--bootstrap-iterations", type=int, default=DEFAULT_BOOTSTRAP_ITERATIONS
    )
    parser.add_argument(
        "--confidence-level", type=float, default=DEFAULT_CONFIDENCE_LEVEL
    )
    parser.add_argument("--bootstrap-seed", default=DEFAULT_BOOTSTRAP_SEED)
    parser.add_argument("--summary", action="store_true")
    args = parser.parse_args()
    if args.baseline and not args.gate:
        parser.error("--baseline requires --gate")
    if args.promote_baseline and not args.gate:
        parser.error("--promote-baseline requires --gate")

    eval_set = resolve_eval_set(args.eval_set)
    items = load_jsonl(eval_set)
    if not items:
        print(f"评测集为空: {eval_set}")
        return 1
    report = run_eval(
        items,
        bootstrap_iterations=args.bootstrap_iterations,
        confidence_level=args.confidence_level,
        bootstrap_seed=args.bootstrap_seed,
    )
    report["created_at"] = datetime.now(timezone.utc).isoformat()
    report["source_eval_set"] = str(eval_set)

    baseline = None
    if args.baseline:
        baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
    if args.gate:
        gate = json.loads(args.gate.read_text(encoding="utf-8"))
        if not isinstance(gate, dict):
            raise ValueError("claim-verification gate must be an object")
        report["gate"] = evaluate_gate(report, gate, baseline=baseline)

    if args.output:
        _atomic_write_json(args.output, report)
    if args.promote_baseline and report["gate"]["passed"]:
        _atomic_write_json(
            args.promote_baseline,
            _baseline_artifact(report, source_eval_set=eval_set),
        )
    if args.summary or not args.output:
        print_summary(report)
    if args.output:
        print(f"报告已写入 {args.output}")
    if args.promote_baseline:
        if report["gate"]["passed"]:
            print(f"基线已晋级 {args.promote_baseline}")
        else:
            print("门禁未通过，基线未修改")
    return 0 if report.get("gate", {}).get("passed", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
