"""通用 Agent 评测评分底座。

规则能判定的内容优先走确定性评测器；开放式内容才调用 LLM Judge。
Judge 的 pass 只是诊断信号，Trial/Case/Run 的最终决策由本模块确定。
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from statistics import median, pstdev
from typing import Any, Mapping, Sequence

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from cogdoc.agents.qa_generator import Generator
from cogdoc.agents.structured_output import invoke_structured
from cogdoc.config.settings import get_settings


EXECUTION_COMPLETED = {"SUCCESS", "TRACE_INCOMPLETE"}
EXECUTION_FAILED = {"PROTOCOL_ERROR", "CONFIG_ERROR", "TIMEOUT", "TARGET_ERROR"}
SEVERITY_RANK = {
    "PASS": -1,
    "PASS_WITH_WARNING": 0,
    "GATE_REVIEW": 1,
    "GATE_ERROR": 2,
    "GATE_FAIL": 3,
    "FATAL_GATE_UNOBSERVABLE": 4,
    "FATAL": 5,
}
CLAIM_VERDICTS = {"supported", "unsupported", "insufficient", "not_factual"}


class JudgeOutput(BaseModel):
    overall_score: float = Field(ge=1, le=5)
    dimension_scores: dict[str, float] = Field(default_factory=dict)
    pass_: bool = Field(default=False, alias="pass")
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    rationale: str = ""
    concerns: list[str] = Field(default_factory=list)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    recommended_action: str = "NEEDS_REVIEW"

    model_config = {"populate_by_name": True}


@dataclass(frozen=True)
class EvaluatorSpec:
    type: str
    role: str
    weight: float = 1.0
    requires: tuple[str, ...] = ()
    optional: tuple[str, ...] = ()
    gate_policy: Mapping[str, Any] | None = None
    config: Mapping[str, Any] | None = None


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _normalize_score(value: Any) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("score must be finite")
    # Judge scores use the closed 1..5 scale.  Always shift the origin before
    # clamping: 1 means the worst score (0.0), not a perfect score (1.0).
    return max(0.0, min(1.0, (number - 1.0) / 4.0))


def _default_role(evaluator_type: str, config: Mapping[str, Any]) -> str:
    if evaluator_type in {"safety_assertion", "abstention_assertion"}:
        return "GATE"
    if evaluator_type == "llm_judge" and config.get("diagnostic_only"):
        return "DIAGNOSTIC"
    return "QUALITY"


def spec_from_dict(raw: Mapping[str, Any]) -> EvaluatorSpec:
    evaluator_type = str(raw.get("type") or "").strip()
    if not evaluator_type:
        raise ValueError("evaluator.type is required")
    config = dict(raw.get("config") or {})
    role = str(raw.get("role") or _default_role(evaluator_type, config)).upper()
    if role not in {"GATE", "QUALITY", "DIAGNOSTIC"}:
        raise ValueError(f"unsupported evaluator role: {role}")
    weight = float(raw.get("weight", 1.0))
    if not math.isfinite(weight) or weight < 0:
        raise ValueError("evaluator.weight must be a finite non-negative number")
    return EvaluatorSpec(
        type=evaluator_type,
        role=role,
        weight=weight,
        requires=tuple(str(item) for item in raw.get("requires", ())),
        optional=tuple(str(item) for item in raw.get("optional", ())),
        gate_policy=raw.get("gate_policy") or {},
        config=config,
    )


def _path(payload: Mapping[str, Any], name: str) -> Any:
    value: Any = payload
    for part in name.split("."):
        if not isinstance(value, Mapping) or part not in value:
            return None
        value = value[part]
    return value


def _evidence_available(trial: Mapping[str, Any], name: str) -> bool:
    aliases = {
        "agent_output": ("agent_output", "output", "answer"),
        "case_input": ("case_input", "input", "query"),
        "expected": ("expected",),
        "retrieved_context": ("retrieved_context", "context", "evidence"),
        "tool_trace": ("tool_trace", "tools", "trace.tools"),
        "trajectory": ("trajectory", "trace.steps"),
        "citations": ("citations",),
        "conversation_turns": ("conversation_turns", "turns"),
        "claim_audit": (
            "claim_audit",
            "output.claim_audit",
            "trace.output.claim_audit",
        ),
        "trace": ("trace",),
    }
    return any(_path(trial, alias) is not None for alias in aliases.get(name, (name,)))


def _value(trial: Mapping[str, Any], name: str, default: Any = None) -> Any:
    for alias in {
        "agent_output": ("agent_output", "output", "answer"),
        "case_input": ("case_input", "input", "query"),
        "retrieved_context": ("retrieved_context", "context", "evidence"),
        "tool_trace": ("tool_trace", "tools"),
        "trajectory": ("trajectory",),
        "claim_audit": (
            "claim_audit",
            "output.claim_audit",
            "trace.output.claim_audit",
        ),
    }.get(name, (name,)):
        value = _path(trial, alias)
        if value is not None:
            return value
    return default


def _configured_rate(
    config: Mapping[str, Any], name: str, default: float
) -> float:
    value = float(config.get(name, default))
    if not 0.0 <= value <= 1.0:
        raise ValueError(f"{name} must be between 0 and 1")
    return value


def _identifier_set(value: Any) -> set[str]:
    if not isinstance(value, Sequence) or isinstance(
        value, (str, bytes, bytearray)
    ):
        return set()
    return {str(item).strip() for item in value if str(item).strip()}


def _claim_audit_assertion(
    audit: Mapping[str, Any], config: Mapping[str, Any]
) -> dict[str, Any]:
    """依据逐条 verdict 重算门禁，忽略 audit 自带的 counts / metrics。"""
    claims = audit.get("claims")
    if not isinstance(claims, list):
        raise ValueError("claim_audit.claims must be a list")

    counts = {
        "claim_count": 0,
        "supported": 0,
        "unsupported": 0,
        "insufficient": 0,
        "cited": 0,
        "not_factual": 0,
    }
    for claim in claims:
        if not isinstance(claim, Mapping):
            raise ValueError("claim_audit.claims must contain objects")
        verdict = str(claim.get("verdict") or "")
        if verdict not in CLAIM_VERDICTS:
            raise ValueError(f"unsupported claim verdict: {verdict or '<missing>'}")
        if verdict == "not_factual":
            counts["not_factual"] += 1
            continue
        cited_ids = _identifier_set(claim.get("cited_chunk_ids"))
        supporting_ids = _identifier_set(claim.get("supporting_chunk_ids"))
        # 外部 eval case 可能伪造自相矛盾的 supported。按运行时门禁的
        # 同一规则重算：必须至少有一个 supporting id，且全部属于合法引用集。
        if verdict == "supported" and (
            not supporting_ids or not supporting_ids.issubset(cited_ids)
        ):
            verdict = "insufficient"
        counts["claim_count"] += 1
        counts[verdict] += 1
        if cited_ids:
            counts["cited"] += 1

    denominator = counts["claim_count"]

    def rate(name: str) -> float | None:
        return counts[name] / denominator if denominator else None

    metrics = {
        "claim_support_rate": rate("supported"),
        "citation_coverage": rate("cited"),
        "unsupported_claim_rate": rate("unsupported"),
        "insufficient_claim_rate": rate("insufficient"),
    }
    allowed_raw = config.get("allowed_statuses", ("passed", "repaired"))
    if isinstance(allowed_raw, str):
        allowed_statuses = {allowed_raw}
    elif isinstance(allowed_raw, Sequence):
        allowed_statuses = {str(item) for item in allowed_raw}
    else:
        raise ValueError("allowed_statuses must be a string or sequence")

    allow_empty = bool(config.get("allow_empty", True))
    min_support = _configured_rate(config, "min_claim_support_rate", 1.0)
    min_citation = _configured_rate(config, "min_citation_coverage", 1.0)
    max_unsupported = _configured_rate(
        config, "max_unsupported_claim_rate", 0.0
    )
    max_insufficient = _configured_rate(
        config, "max_insufficient_claim_rate", 0.0
    )
    empty_allowed = denominator > 0 or allow_empty
    checks = {
        "status_allowed": str(audit.get("status") or "") in allowed_statuses,
        "claim_count_allowed": empty_allowed,
        "support_rate": (
            empty_allowed
            if metrics["claim_support_rate"] is None
            else metrics["claim_support_rate"] >= min_support
        ),
        "citation_coverage": (
            empty_allowed
            if metrics["citation_coverage"] is None
            else metrics["citation_coverage"] >= min_citation
        ),
        "unsupported_claim_rate": (
            empty_allowed
            if metrics["unsupported_claim_rate"] is None
            else metrics["unsupported_claim_rate"] <= max_unsupported
        ),
        "insufficient_claim_rate": (
            empty_allowed
            if metrics["insufficient_claim_rate"] is None
            else metrics["insufficient_claim_rate"] <= max_insufficient
        ),
    }
    passed = all(checks.values())
    return {
        "status": "PASS" if passed else "FAIL",
        "score": 1.0 if passed else 0.0,
        "passed": passed,
        "details": {
            "audit_status": str(audit.get("status") or ""),
            "counts": counts,
            "metrics": metrics,
            "checks": checks,
            "thresholds": {
                "allowed_statuses": sorted(allowed_statuses),
                "allow_empty": allow_empty,
                "min_claim_support_rate": min_support,
                "min_citation_coverage": min_citation,
                "max_unsupported_claim_rate": max_unsupported,
                "max_insufficient_claim_rate": max_insufficient,
            },
        },
    }


def _deterministic(spec: EvaluatorSpec, trial: Mapping[str, Any]) -> dict[str, Any]:
    expected = _value(trial, "expected", {})
    actual = _value(trial, "agent_output", "")
    config = spec.config or {}
    kind = spec.type
    passed = False
    details: dict[str, Any] = {}

    if kind == "exact_match":
        passed = actual == expected
    elif kind == "regex_match":
        patterns = config.get("patterns", expected if isinstance(expected, list) else [expected])
        passed = all(re.search(str(pattern), _text(actual)) for pattern in patterns)
    elif kind == "keyword_match":
        keywords = config.get("keywords", expected if isinstance(expected, list) else [expected])
        passed = all(str(word).lower() in _text(actual).lower() for word in keywords)
    elif kind == "numeric_assertion":
        tolerance = float(config.get("tolerance", 0.0))
        passed = abs(float(actual) - float(expected)) <= tolerance
    elif kind in {"json_assertion", "state_assertion"}:
        value = actual
        if isinstance(value, str):
            value = json.loads(value)
        expected_map = expected if isinstance(expected, Mapping) else config.get("expected", {})
        passed = all(_path(value, str(key)) == expected_value for key, expected_value in expected_map.items())
    elif kind == "set_assertion":
        passed = set(actual or ()) == set(expected or ())
    elif kind in {"sequence_assertion", "trajectory_assertion"}:
        observed = actual if isinstance(actual, list) else _value(trial, "trajectory", [])
        target = expected if isinstance(expected, list) else config.get("expected", [])
        cursor = 0
        for item in observed:
            if cursor < len(target) and item == target[cursor]:
                cursor += 1
        passed = cursor == len(target)
        details = {"matched": cursor, "expected": len(target)}
    elif kind == "tool_assertion":
        observed = _value(trial, "tool_trace", []) or []
        target = expected if isinstance(expected, list) else config.get("expected", [])
        passed = observed == target or all(item in observed for item in target)
    elif kind == "claim_audit_assertion":
        audit = _value(trial, "claim_audit")
        if not isinstance(audit, Mapping):
            raise ValueError("claim_audit must be an object")
        return _claim_audit_assertion(audit, config)
    elif kind in {"safety_assertion", "abstention_assertion"}:
        forbidden = config.get("forbidden_patterns", [])
        if kind == "abstention_assertion":
            expected_abstain = bool(config.get("expected_abstention", expected is True))
            actual_abstain = bool(trial.get("abstained") or config.get("abstain_predicate", False))
            passed = actual_abstain == expected_abstain
        else:
            matches = [pattern for pattern in forbidden if re.search(str(pattern), _text(actual), re.I)]
            passed = not matches
            details = {"matched_patterns": matches}
    else:
        raise ValueError(f"unsupported deterministic evaluator: {kind}")

    return {
        "status": "PASS" if passed else "FAIL",
        "score": 1.0 if passed else 0.0,
        "passed": passed,
        "details": details,
    }


class LLMJudge:
    """统一的开放式结果裁判，可使用 DeepSeek/OpenAI-compatible 云端 API。"""

    def __init__(self, *, is_local: bool = False, model_name: str | None = None):
        self.is_local = is_local
        self.model_name = model_name

    def _client(self):
        return Generator._get_client(is_local=self.is_local, custom_model_name=self.model_name)

    @staticmethod
    def _rubric_text(rubric: Mapping[str, Any]) -> str:
        return json.dumps(rubric, ensure_ascii=False, indent=2, sort_keys=True)

    def evaluate(self, trial: Mapping[str, Any], spec: EvaluatorSpec) -> dict[str, Any]:
        rubric = dict(spec.config or {})
        dimensions = rubric.get("dimensions") or ["correctness", "completeness", "relevance", "compliance", "fluency"]
        metric = rubric.get("metric")
        if metric:
            metric_rubrics = {
                "faithfulness": "将回答拆成原子陈述，统计可由 retrieved_context 支持的陈述比例；只看上下文，不因答案流畅加分。",
                "answer_relevancy": "判断回答是否直接解决 case_input；忽略与问题无关的扩写。可用反向问题验证语义相关性。",
                "context_relevancy": "判断 retrieved_context 中真正能帮助回答 case_input 的片段比例；无关召回应扣分。",
            }
            rubric.setdefault("metric_definition", metric_rubrics.get(str(metric), "按指定指标评分。"))
            dimensions = [str(metric)]
        payload = {
            "agent_type": trial.get("agent_type", "general"),
            "case_input": trial.get("case_input", trial.get("input", trial.get("query"))),
            "expected": trial.get("expected", {}),
            "agent_output": _value(trial, "agent_output", ""),
            "retrieved_context": _value(trial, "retrieved_context", []),
            "citations": trial.get("citations", []),
            "tool_trace": _value(trial, "tool_trace", []),
            "conversation_turns": trial.get("conversation_turns", []),
            "rubric": rubric,
        }
        system = (
            "你是 Agent Evaluation Judge。只依据输入、期望目标、Agent 输出、证据和轨迹评分。"
            "禁止根据篇幅、格式华丽、语气热情额外加分；不确定时降低 confidence。"
            "每个维度 1-5 分，必须给出可核验的 evidence；只返回 JSON。\n"
            f"评分维度: {', '.join(str(item) for item in dimensions)}\n"
            "输出字段必须包含 overall_score, dimension_scores, pass, confidence, rationale, concerns, evidence, recommended_action。"
        )
        human = "评测上下文:\n" + json.dumps(payload, ensure_ascii=False, indent=2) + "\n评分 Rubric:\n" + self._rubric_text(rubric)
        result = invoke_structured(self._client(), JudgeOutput, [SystemMessage(content=system), HumanMessage(content=human)])
        status = "PASS"
        if not result.evidence:
            status = "NEEDS_REVIEW"
        if result.confidence < 0.4 and not result.evidence:
            status = "NEEDS_REVIEW"
        return {
            "status": status,
            "score": _normalize_score(result.overall_score),
            "raw_score": result.overall_score,
            "dimension_scores": result.dimension_scores,
            "pass": result.pass_,
            "confidence": result.confidence,
            "rationale": result.rationale,
            "concerns": result.concerns,
            "evidence": result.evidence,
            "recommended_action": result.recommended_action,
        }


def evaluate_one(spec: EvaluatorSpec, trial: Mapping[str, Any], judge: LLMJudge | None = None) -> dict[str, Any]:
    missing = [name for name in spec.requires if not _evidence_available(trial, name)]
    if missing:
        return {"status": "NOT_OBSERVABLE", "score": None, "missing_evidence": missing}
    if spec.type == "claim_audit_assertion":
        audit = _value(trial, "claim_audit")
        if not isinstance(audit, Mapping):
            return {
                "status": "NOT_OBSERVABLE",
                "score": None,
                "missing_evidence": ["claim_audit"],
            }
        if not isinstance(audit.get("claims"), list):
            return {
                "status": "NOT_OBSERVABLE",
                "score": None,
                "missing_evidence": ["claim_audit.claims"],
            }
    if spec.type in {"llm_judge", "ragas_metric", "code_review", "deepeval_metric", "ragas"}:
        if judge is None:
            return {"status": "NOT_OBSERVABLE", "score": None, "error": "judge_not_configured"}
        return judge.evaluate(trial, spec)
    try:
        return _deterministic(spec, trial)
    except Exception as exc:
        return {"status": "ERROR", "score": None, "error": type(exc).__name__, "message": str(exc)[:200]}


def evaluate_gate(spec: EvaluatorSpec, result: Mapping[str, Any]) -> str:
    level = str((spec.gate_policy or {}).get("level", "CRITICAL")).upper()
    rank = {"WARNING": 0, "CRITICAL": 1, "FATAL": 2}.get(level, 1)
    status = str(result.get("status", "ERROR"))
    if rank == 0 and status != "PASS":
        return "PASS_WITH_WARNING"
    if status == "ERROR":
        return "FATAL" if rank >= 2 else "GATE_ERROR"
    if status == "FAIL":
        return "FATAL" if rank >= 2 else "GATE_FAIL"
    if status == "NOT_OBSERVABLE":
        if rank >= 2:
            return "FATAL_GATE_UNOBSERVABLE"
        if rank >= 1 and bool((spec.gate_policy or {}).get("required", True)):
            return "GATE_REVIEW"
        return "PASS_WITH_WARNING"
    if status == "NEEDS_REVIEW":
        return "GATE_REVIEW"
    return "PASS"


def evaluate_trial(trial: Mapping[str, Any], evaluators: Sequence[Mapping[str, Any]], judge: LLMJudge | None = None, *, pass_threshold: float = 0.8, margin: float = 0.05) -> dict[str, Any]:
    if not math.isfinite(pass_threshold) or not 0.0 <= pass_threshold <= 1.0:
        raise ValueError("pass_threshold must be between 0 and 1")
    if not math.isfinite(margin) or not 0.0 <= margin <= pass_threshold:
        raise ValueError("margin must be between 0 and pass_threshold")
    specs = [spec_from_dict(raw) for raw in evaluators]
    results = []
    gate_decisions = []
    quality_scores = []
    for spec in specs:
        result = evaluate_one(spec, trial, judge)
        row = {"type": spec.type, "role": spec.role, "weight": spec.weight, **result}
        results.append(row)
        if spec.role == "GATE":
            gate_decisions.append(evaluate_gate(spec, result))
        elif spec.role == "QUALITY" and result.get("score") is not None:
            quality_scores.append((float(result["score"]), spec.weight))
    quality_score = None
    if quality_scores and sum(weight for _, weight in quality_scores) > 0:
        quality_score = sum(score * weight for score, weight in quality_scores) / sum(weight for _, weight in quality_scores)
    execution_status = str(trial.get("execution_status") or "UNKNOWN")
    has_review = any(row.get("status") == "NEEDS_REVIEW" for row in results)
    if any(item in gate_decisions for item in {"FATAL", "FATAL_GATE_UNOBSERVABLE", "GATE_ERROR", "GATE_FAIL"}):
        decision = "FAIL"
    elif execution_status in EXECUTION_FAILED:
        # A deterministic evaluator can still score partial output from a
        # timed-out or failed execution.  That score is diagnostic only: an
        # execution which did not complete must never be promoted to PASS.
        decision = "FAIL"
    elif execution_status not in EXECUTION_COMPLETED:
        # Unknown producer statuses fail closed without conflating a protocol
        # version skew with a confirmed quality failure.
        decision = "NEEDS_REVIEW"
    elif "GATE_REVIEW" in gate_decisions:
        decision = "NEEDS_REVIEW"
    elif quality_score is None:
        decision = "NEEDS_REVIEW"
    elif execution_status == "TRACE_INCOMPLETE" or has_review:
        decision = "NEEDS_REVIEW"
    elif quality_score >= pass_threshold:
        decision = "PASS"
    elif quality_score >= pass_threshold - margin:
        decision = "DEGRADED"
    else:
        decision = "FAIL"
    return {
        "trial_id": trial.get("trial_id"),
        "execution_status": execution_status,
        "quality_score": quality_score,
        "decision": decision,
        "gate_decision": max(gate_decisions or ["PASS"], key=lambda item: SEVERITY_RANK[item]),
        "evaluators": results,
    }


def aggregate_case(trials: Sequence[Mapping[str, Any]], *, min_trials: int = 3, min_success_rate: float = 0.8, max_stddev: float = 0.15) -> dict[str, Any]:
    if type(min_trials) is not int or min_trials < 1:
        raise ValueError("min_trials must be a positive integer")
    if not math.isfinite(min_success_rate) or not 0.0 <= min_success_rate <= 1.0:
        raise ValueError("min_success_rate must be between 0 and 1")
    if not math.isfinite(max_stddev) or not 0.0 <= max_stddev <= 1.0:
        raise ValueError("max_stddev must be between 0 and 1")
    all_trials = list(trials)
    n_total = len(all_trials)
    completed = [trial for trial in all_trials if trial.get("execution_status") in EXECUTION_COMPLETED]
    evaluable = [trial for trial in completed if trial.get("quality_score") is not None]
    scores = [float(trial["quality_score"]) for trial in evaluable]
    if any(not math.isfinite(score) or not 0.0 <= score <= 1.0 for score in scores):
        raise ValueError("trial quality_score must be finite and between 0 and 1")
    n_passed = sum(1 for trial in all_trials if trial.get("execution_status") == "SUCCESS" and trial.get("decision") == "PASS")
    completion_rate = len(completed) / n_total if n_total else 0.0
    pass_rate = n_passed / n_total if n_total else 0.0
    buckets = []
    for trial in all_trials:
        if trial.get("execution_status") == "TRACE_INCOMPLETE":
            buckets.append("EVIDENCE_GAP")
        elif trial.get("execution_status") != "SUCCESS":
            buckets.append("EXEC_FAIL")
        elif trial.get("decision") == "PASS":
            buckets.append("PASS")
        else:
            buckets.append("NON_PASS")
    consistency = max(Counter(buckets).values()) / n_total if n_total else 0.0
    stddev = pstdev(scores) if len(scores) >= 2 else 0.0
    stability = "INSUFFICIENT" if n_total < min_trials else "UNSTABLE" if completion_rate < 0.5 or pass_rate < min_success_rate or stddev > max_stddev else "STABLE"
    return {
        "quality_score": median(scores) if scores else None,
        "n_total": n_total,
        "n_completed": len(completed),
        "n_quality_evaluable": len(evaluable),
        "n_passed": n_passed,
        "execution_completion_rate": completion_rate,
        "quality_evaluable_rate": len(evaluable) / n_total if n_total else 0.0,
        "observed_pass_rate": pass_rate,
        "consistency": consistency,
        "score_stddev": stddev,
        "stability_status": stability,
    }


def aggregate_run(cases: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    rows = list(cases)
    scores = [float(row["quality_score"]) for row in rows if row.get("quality_score") is not None]
    if any(not math.isfinite(score) or not 0.0 <= score <= 1.0 for score in scores):
        raise ValueError("case quality_score must be finite and between 0 and 1")
    has_unstable = any(row.get("stability_status") == "UNSTABLE" for row in rows)
    has_insufficient = any(row.get("stability_status") == "INSUFFICIENT" for row in rows)
    decision = "FAIL" if has_unstable else "NEEDS_REVIEW" if has_insufficient or not scores else "PASS"
    return {
        "quality_score": sum(scores) / len(scores) if scores else None,
        "case_count": len(rows),
        "quality_evaluable_case_count": len(scores),
        "stable_case_rate": sum(row.get("stability_status") == "STABLE" for row in rows) / len(rows) if rows else 0.0,
        "execution_completion_rate": sum(float(row.get("execution_completion_rate", 0.0)) for row in rows) / len(rows) if rows else 0.0,
        "decision": decision,
    }


def judge_from_settings() -> LLMJudge | None:
    settings = get_settings()
    if not settings.llm_judge_enabled or not settings.llm_api_key:
        return None
    return LLMJudge(is_local=False, model_name=getattr(settings, "llm_judge_model_name", "") or None)
