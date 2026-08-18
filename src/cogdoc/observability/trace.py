import json
import math
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from cogdoc.config.settings import Settings, get_settings
from cogdoc.service.claim_verification_policy import (
    claim_verification_policy_projection,
)
from cogdoc.service.claim_verification_rollout import ROLLOUT_DECISIONS
from cogdoc.tools.retriever.metadata import safe_retrieval_metadata


TRACE_SCHEMA_VERSION = "v1"
TRACE_PREVIEW_CHARS = 120
_PRIVATE_EVIDENCE_KEY_PREFIXES = ("_evidence_",)
_DOCUMENT_IDENTITY_META_KEYS = frozenset(("chunk_id", "knowledge_id"))
_DOCUMENT_POSITION_META_KEYS = frozenset(
    ("page", "page_start", "page_end", "chunk_index", "local_chunk_index")
)


def _nonnegative_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def _finite_float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _mapping_or_empty(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


# 返回单调毫秒时间。
def monotonic_ms() -> float:
    return time.monotonic() * 1000


# 构建短文本预览。
def _preview(text: Any, limit: int = TRACE_PREVIEW_CHARS) -> str:
    compact = " ".join(str(text or "").split())
    return compact[:limit]


def _is_private_evidence_key(value: Any) -> bool:
    key = str(value)
    return any(key.startswith(prefix) for prefix in _PRIVATE_EVIDENCE_KEY_PREFIXES)


def _is_retrieved_doc(value: Mapping[str, Any]) -> bool:
    if "text" not in value:
        return False
    meta = value.get("meta")
    if not isinstance(meta, Mapping):
        return False
    has_identity = any(
        meta.get(key) not in (None, "") for key in _DOCUMENT_IDENTITY_META_KEYS
    )
    has_source_position = bool(meta.get("source")) and any(
        key in meta for key in _DOCUMENT_POSITION_META_KEYS
    )
    return has_identity or has_source_position or bool(meta.get("source_type"))


# 将 LangChain / Pydantic 等运行期对象转成可写入 trace JSON 的结构。
def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, Mapping):
        # RetrievedDoc 的正文只保留短预览；pack/span 为跨轮恢复保存的私有
        # 原文无论嵌套在哪一层都不得进入 trace。
        if _is_retrieved_doc(value):
            return _json_safe(_doc_ref(value))
        return {
            str(key): _json_safe(item)
            for key, item in value.items()
            if not _is_private_evidence_key(key)
        }
    if isinstance(value, list | tuple | set):
        return [_json_safe(item) for item in value]
    if hasattr(value, "model_dump"):
        try:
            return _json_safe(value.model_dump())
        except Exception:
            pass
    if hasattr(value, "dict"):
        try:
            return _json_safe(value.dict())
        except Exception:
            pass
    if hasattr(value, "content"):
        payload = {
            "type": getattr(value, "type", value.__class__.__name__),
            "content": getattr(value, "content", ""),
        }
        return _json_safe(payload)
    return str(value)


# 构建文档引用摘要。
def _doc_ref(doc: Mapping[str, Any]) -> dict:
    meta = _mapping_or_empty(doc.get("meta"))
    retrieval = _mapping_or_empty(doc.get("retrieval"))
    return {
        "chunk_id": meta.get("chunk_id", ""),
        "parent_chunk_id": meta.get("parent_chunk_id", ""),
        "section_title": meta.get("section_title", ""),
        "section_path": meta.get("section_path", ""),
        "source_type": meta.get("source_type", "document"),
        "knowledge_id": meta.get("knowledge_id", ""),
        "source": meta.get("source", ""),
        "page": meta.get("page", 0),
        "page_start": meta.get("page_start", meta.get("page", 0)),
        "page_end": meta.get("page_end", meta.get("page", 0)),
        "rewrite_query": retrieval.get("rewrite_query", ""),
        "retrieval": safe_retrieval_metadata(retrieval),
        "text_preview": _preview(doc.get("text", "")),
    }


# 构建证据引用摘要。
def _evidence_ref(item: Mapping[str, Any]) -> dict:
    return {
        "chunk_id": item.get("chunk_id", ""),
        "parent_chunk_id": item.get("parent_chunk_id", ""),
        "section_title": item.get("section_title", ""),
        "section_path": item.get("section_path", ""),
        "source_type": item.get("source_type", "document"),
        "knowledge_id": item.get("knowledge_id", ""),
        "source": item.get("source", ""),
        "page": item.get("page", 0),
        "page_start": item.get("page_start", item.get("page", 0)),
        "page_end": item.get("page_end", item.get("page", 0)),
        "retrieval": safe_retrieval_metadata(item.get("retrieval") or {}),
        "text_preview": _preview(item.get("text_preview", "")),
    }


# 构建跟踪步骤。
def build_trace_step(
    node_name: str,
    output: Mapping[str, Any],
    duration_ms: float,
    model_name: str | None = None,
    retrieval_top_k: int | None = None,
) -> dict:
    step = {
        "node_name": node_name,
        "duration_ms": round(max(duration_ms, 0.0), 3),
        "model": model_name,
        "token": None,
        "retrieval_top_k": retrieval_top_k,
        "critique": None,
        "error_class": None,
        "counts": {},
        "evidence": [],
        # 评分和 Bad Case 回灌需要原始节点结果；展示层仍使用下方的摘要字段。
        "output_snapshot": _json_safe(dict(output)),
    }

    if "task_type" in output:
        step["task_type"] = output.get("task_type")
    if "router_reason" in output:
        step["router_reason"] = _preview(output.get("router_reason"), 240)
    if "error" in output:
        step["error_class"] = output.get("error") or "error"
    if "critique" in output:
        critique = str(output.get("critique") or "")
        step["critique"] = _preview(critique, 300) if critique else ""
    if "retrieval_abstained" in output:
        step["retrieval_abstained"] = bool(output.get("retrieval_abstained"))
        step["retrieval_confidence"] = output.get("retrieval_confidence")
        step["retrieval_abstain_reason"] = _preview(
            output.get("retrieval_abstain_reason"), 80
        )
        signals = output.get("retrieval_signals")
        if isinstance(signals, Mapping):
            step["retrieval_signals"] = {
                str(key): value for key, value in signals.items()
            }
    if "evidence_verification_required" in output:
        step["evidence_verification_required"] = bool(
            output.get("evidence_verification_required")
        )
        step["evidence_supported"] = bool(output.get("evidence_supported"))
        step["evidence_verification_reason"] = _preview(
            output.get("evidence_verification_reason"), 300
        )
        step["evidence_verified_chunk_ids"] = [
            _preview(chunk_id, 120)
            for chunk_id in list(output.get("evidence_verified_chunk_ids") or [])[:5]
        ]
        if output.get("evidence_verifier_error"):
            step["evidence_verifier_error"] = _preview(
                output.get("evidence_verifier_error"), 80
            )
        assessments = output.get("evidence_requirement_assessments")
        if isinstance(assessments, list):
            step["evidence_requirement_assessments"] = [
                {
                    "requirement_id": _preview(item.get("requirement_id"), 32),
                    "verdict": _preview(item.get("verdict"), 32),
                    "evidence_chunk_ids": [
                        _preview(chunk_id, 120)
                        for chunk_id in list(item.get("evidence_chunk_ids") or [])[:5]
                    ],
                    "reason": _preview(item.get("reason"), 200),
                }
                for item in assessments[:5]
                if isinstance(item, Mapping)
            ]
            step["missing_evidence_requirement_ids"] = [
                _preview(item, 32)
                for item in list(output.get("missing_evidence_requirement_ids") or [])[
                    :5
                ]
            ]
    if "evidence_verification_pending" in output:
        step["evidence_verification_pending"] = bool(
            output.get("evidence_verification_pending")
        )
    if "retrieval_retry_count" in output:
        step["retrieval_retry_count"] = _nonnegative_int(
            output.get("retrieval_retry_count")
        )
        step["retrieval_retry_reason"] = _preview(
            output.get("retrieval_retry_reason"), 80
        )
    if "retrieval_round" in output:
        step["retrieval_round"] = _nonnegative_int(output.get("retrieval_round"))
    if "retrieval_top_k_used" in output:
        step["retrieval_top_k_used"] = _nonnegative_int(
            output.get("retrieval_top_k_used")
        )
    if "retrieval_query_count" in output:
        step["retrieval_query_count"] = _nonnegative_int(
            output.get("retrieval_query_count")
        )
    if "retrieval_ranking_count" in output:
        step["retrieval_ranking_count"] = _nonnegative_int(
            output.get("retrieval_ranking_count")
        )
    channel_counts = output.get("retrieval_channel_counts")
    if isinstance(channel_counts, Mapping):
        step["retrieval_channel_counts"] = {
            str(channel): _nonnegative_int(count)
            for channel, count in channel_counts.items()
        }
    if "retrieval_carryover_count" in output:
        step["retrieval_carryover_count"] = _nonnegative_int(
            output.get("retrieval_carryover_count")
        )
    if "parent_context_expanded_count" in output:
        step["parent_context_expanded_count"] = _nonnegative_int(
            output.get("parent_context_expanded_count")
        )
    if "neighbor_context_expanded_count" in output:
        step["neighbor_context_expanded_count"] = _nonnegative_int(
            output.get("neighbor_context_expanded_count")
        )
    for field in (
        "evidence_span_input_count",
        "evidence_span_output_count",
        "evidence_span_compressed_count",
        "evidence_span_fallback_count",
        "evidence_span_input_chars",
        "evidence_span_selected_chars",
        "evidence_pack_input_count",
        "evidence_pack_kept_count",
        "evidence_pack_dropped_count",
        "evidence_pack_input_chars",
        "evidence_pack_kept_chars",
        "evidence_pack_overlap_removed_chars",
        "evidence_pack_anchor_count",
        "evidence_pack_pinned_count",
    ):
        if field in output:
            step[field] = _nonnegative_int(output.get(field))
    span_reason_counts = output.get("evidence_span_reason_counts")
    if isinstance(span_reason_counts, Mapping):
        step["evidence_span_reason_counts"] = {
            _preview(reason, 80): _nonnegative_int(count)
            for reason, count in span_reason_counts.items()
            if _preview(reason, 80)
        }
    drop_reason_counts = output.get("evidence_pack_drop_reason_counts")
    if isinstance(drop_reason_counts, Mapping):
        step["evidence_pack_drop_reason_counts"] = {
            _preview(reason, 80): _nonnegative_int(count)
            for reason, count in drop_reason_counts.items()
        }
    if "evidence_pack_over_budget" in output:
        step["evidence_pack_over_budget"] = bool(
            output.get("evidence_pack_over_budget")
        )
    if "adaptive_retrieval_retry_pending" in output:
        step["adaptive_retrieval_retry_pending"] = bool(
            output.get("adaptive_retrieval_retry_pending")
        )
    if isinstance(output.get("claim_audit"), Mapping):
        audit = output["claim_audit"]
        step["claim_audit"] = {
            "status": audit.get("status", "not_run"),
            "reason_code": _preview(audit.get("reason_code"), 80),
            "counts": _json_safe(dict(audit.get("counts") or {})),
            "metrics": _json_safe(dict(audit.get("metrics") or {})),
            "repair": _json_safe(dict(audit.get("repair") or {})),
            "verifier": _json_safe(dict(audit.get("verifier") or {})),
            "claim_previews": [
                {
                    "claim_id": _preview(claim.get("claim_id"), 32),
                    "text": _preview(claim.get("text"), 160),
                    "verdict": _preview(claim.get("verdict"), 32),
                    "reason": _preview(claim.get("reason"), 200),
                    "supporting_chunk_ids": [
                        _preview(chunk_id, 120)
                        for chunk_id in list(claim.get("supporting_chunk_ids") or [])[
                            :5
                        ]
                    ],
                }
                for claim in list(audit.get("claims") or [])[:5]
                if isinstance(claim, Mapping)
            ],
        }
    if isinstance(output.get("claim_verification_rollout"), Mapping):
        rollout = output["claim_verification_rollout"]
        rollout_mode = str(rollout.get("mode") or "off")
        if rollout_mode not in {"off", "shadow", "enforce"}:
            rollout_mode = "off"
        policy = claim_verification_policy_projection(
            rollout, effective_mode=rollout_mode
        )
        step["claim_verification"] = {
            "version": "v1",
            "mode": rollout_mode,
            **policy,
            "decision": _preview(rollout.get("decision"), 32),
            "executed": bool(rollout.get("executed", False)),
            "enforced": bool(rollout.get("enforced", False)),
            "released": bool(rollout.get("released", True)),
            "would_intervene": bool(rollout.get("would_intervene", False)),
            "would_repair": bool(rollout.get("would_repair", False)),
            "would_block": bool(rollout.get("would_block", False)),
            "audit_status": _preview(rollout.get("audit_status"), 32),
            "reason_code": _preview(rollout.get("reason_code"), 128),
            "repair_count": _nonnegative_int(rollout.get("repair_count")),
        }
    if output.get("rewritten_queries"):
        step["rewritten_queries"] = [
            _preview(query, 120)
            for query in list(output.get("rewritten_queries") or [])[:5]
        ]
    if output.get("evidence_requirements"):
        step["evidence_requirements"] = [
            {
                "requirement_id": _preview(item.get("requirement_id"), 32),
                "question": _preview(item.get("question"), 160),
                "retrieval_query": _preview(item.get("retrieval_query"), 160),
                "recovery_query": _preview(item.get("recovery_query"), 160),
            }
            for item in list(output.get("evidence_requirements") or [])[:5]
            if isinstance(item, Mapping)
        ]
    if output.get("evidence_units"):
        step["evidence_units"] = [
            {
                "unit_id": _preview(item.get("unit_id"), 40),
                "task_kind": _preview(item.get("task_kind"), 40),
                "label": _preview(item.get("label"), 120),
                "retrieval_query": _preview(item.get("retrieval_query"), 160),
                "recovery_query": _preview(item.get("recovery_query"), 160),
                "allowed_sources": [
                    _preview(source, 160)
                    for source in list(item.get("allowed_sources") or [])[:5]
                ],
                "admission_group": _preview(item.get("admission_group"), 40),
                "max_retrieval_retries": _nonnegative_int(
                    item.get("max_retrieval_retries")
                ),
                "binding": _json_safe(dict(item.get("binding") or {})),
            }
            for item in list(output.get("evidence_units") or [])[:30]
            if isinstance(item, Mapping)
        ]
    if output.get("evidence_unit_results"):
        step["evidence_unit_results"] = [
            {
                "unit_id": _preview(item.get("unit_id"), 40),
                "status": _preview(item.get("status"), 40),
                "retrieval_round": _nonnegative_int(item.get("retrieval_round")),
                "candidate_count": _nonnegative_int(item.get("candidate_count")),
                "selected_count": _nonnegative_int(item.get("selected_count")),
                "selected_chars": _nonnegative_int(item.get("selected_chars")),
                "reason_code": _preview(item.get("reason_code"), 80),
                "error_class": _preview(item.get("error_class"), 80),
                "grounding_evidence_ids": [
                    _preview(evidence_id, 40)
                    for evidence_id in list(
                        item.get("grounding_evidence_ids") or []
                    )[:8]
                ],
                "retry_attempted": bool(item.get("retry_attempted", False)),
                "gate_action": _preview(item.get("gate_action"), 24),
                "gate_reason_code": _preview(item.get("gate_reason_code"), 80),
                "selected_chunk_ids": [
                    _preview(_mapping_or_empty(doc.get("meta")).get("chunk_id"), 120)
                    for doc in list(item.get("selected_docs") or [])[:8]
                    if isinstance(doc, Mapping)
                ],
            }
            for item in list(output.get("evidence_unit_results") or [])[:30]
            if isinstance(item, Mapping)
        ]
    if isinstance(output.get("evidence_unit_metrics"), Mapping):
        step["evidence_unit_metrics"] = _json_safe(
            dict(output["evidence_unit_metrics"])
        )
    if output.get("evidence_unit_retry_history"):
        step["evidence_unit_retry_history"] = [
            [_preview(unit_id, 40) for unit_id in list(round_ids)[:30]]
            for round_ids in list(output.get("evidence_unit_retry_history") or [])[:10]
            if isinstance(round_ids, (list, tuple))
        ]
    if isinstance(output.get("evidence_unit_verification_metrics"), Mapping):
        step["evidence_unit_verification_metrics"] = _json_safe(
            dict(output["evidence_unit_verification_metrics"])
        )
    if output.get("evidence_unit_verification_protocol_errors"):
        step["evidence_unit_verification_protocol_errors"] = [
            _preview(error, 120)
            for error in list(
                output.get("evidence_unit_verification_protocol_errors") or []
            )[:12]
        ]
    if output.get("evidence_unit_verifier_error"):
        step["evidence_unit_verifier_error"] = _preview(
            output.get("evidence_unit_verifier_error"), 80
        )
    if output.get("evidence_unit_gate_decisions"):
        step["evidence_unit_gate_decisions"] = [
            {
                "unit_id": _preview(item.get("unit_id"), 40),
                "action": _preview(item.get("action"), 24),
                "verification_status": _preview(
                    item.get("verification_status"), 40
                ),
                "retrieval_round": _nonnegative_int(
                    item.get("retrieval_round")
                ),
                "retries_remaining": _nonnegative_int(
                    item.get("retries_remaining")
                ),
                "reason_code": _preview(item.get("reason_code"), 80),
            }
            for item in list(output.get("evidence_unit_gate_decisions") or [])[:30]
            if isinstance(item, Mapping)
        ]
    if isinstance(output.get("evidence_unit_gate_metrics"), Mapping):
        step["evidence_unit_gate_metrics"] = _json_safe(
            dict(output["evidence_unit_gate_metrics"])
        )
    if "evidence_unit_batch_can_generate" in output:
        step["evidence_unit_batch_can_generate"] = bool(
            output.get("evidence_unit_batch_can_generate")
        )
    if output.get("evidence_unit_adapter_outcome"):
        step["evidence_unit_adapter_outcome"] = _preview(
            output.get("evidence_unit_adapter_outcome"), 40
        )
    if output.get("steps_trace"):
        step["steps_trace"] = [
            {
                "step_name": _preview(item.get("step_name", ""), 80),
                "input_summary": _preview(item.get("input_summary", ""), 400),
                "output_summary": _preview(item.get("output_summary", ""), 800),
            }
            for item in list(output.get("steps_trace") or [])[:5]
            if isinstance(item, Mapping)
        ]

    count_fields = {
        "rewritten_queries": "rewritten_query_count",
        "evidence_requirements": "evidence_requirement_count",
        "evidence_units": "evidence_unit_count",
        "evidence_unit_results": "evidence_unit_result_count",
        "evidence_unit_assessments": "evidence_unit_assessment_count",
        "evidence_unit_gate_decisions": "evidence_unit_gate_decision_count",
        "retrieved_docs": "retrieved_count",
        "reranked_docs": "reranked_count",
        "verification_docs": "verification_candidate_count",
        "summary_docs": "summary_doc_count",
        "summary_section_results": "summary_section_count",
        "compare_sources": "compare_source_count",
        "document_profiles": "document_profile_count",
        "evidence": "evidence_count",
    }
    for source_key, target_key in count_fields.items():
        value = output.get(source_key)
        if isinstance(value, list):
            step["counts"][target_key] = len(value)

    if output.get("retrieved_docs"):
        step["evidence"] = [_doc_ref(doc) for doc in output["retrieved_docs"][:5]]
    elif output.get("reranked_docs"):
        step["evidence"] = [_doc_ref(doc) for doc in output["reranked_docs"][:5]]
    elif output.get("evidence"):
        step["evidence"] = [_evidence_ref(item) for item in output["evidence"][:8]]

    return step


# 构建跟踪目录路径。
def trace_dir(settings: Settings | None = None) -> Path:
    settings = settings or get_settings()
    base_dir = Path(settings.cogdoc_trace_dir)
    if not base_dir.is_absolute():
        base_dir = settings.project_root / base_dir
    return base_dir


# 构建跟踪文件路径。
def trace_path(trace_id: str, settings: Settings | None = None) -> Path:
    base_dir = trace_dir(settings)
    return base_dir / f"{trace_id}.json"


# 判断跟踪是否属于指定范围。
def _trace_matches_scope(
    payload: Mapping[str, Any], doc_id: str, session_id: str
) -> bool:
    config = _mapping_or_empty(payload.get("config"))
    if doc_id and str(config.get("doc_id") or "") != doc_id:
        return False
    if session_id and str(config.get("session_id") or "") != session_id:
        return False
    return True


# 清理指定知识库或会话的跟踪文件。
def delete_trace_files(
    doc_id: str = "", session_id: str = "", settings: Settings | None = None
) -> int:
    if not doc_id and not session_id:
        return 0
    base_dir = trace_dir(settings)
    if not base_dir.exists() or not base_dir.is_dir():
        return 0
    deleted = 0
    for path in base_dir.glob("*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(payload, Mapping) and _trace_matches_scope(
                payload, doc_id, session_id
            ):
                path.unlink()
                deleted += 1
        except (OSError, json.JSONDecodeError):
            continue
    return deleted


# 汇总跟踪步骤。
def summarize_trace_steps(steps: list[dict]) -> dict:
    error_steps = [
        step for step in steps if step.get("error_class") or step.get("critique")
    ]
    evidence_count = sum(len(step.get("evidence", [])) for step in steps)
    return {
        "step_count": len(steps),
        "error_count": len(error_steps),
        "evidence_ref_count": evidence_count,
        "node_names": [step.get("node_name", "") for step in steps],
    }


def _claim_audit_summary(output_payload: Mapping[str, Any] | None) -> dict | None:
    if not isinstance(output_payload, Mapping):
        return None
    audit = output_payload.get("claim_audit")
    if not isinstance(audit, Mapping):
        return None
    counts = _mapping_or_empty(audit.get("counts"))
    metrics = _mapping_or_empty(audit.get("metrics"))
    repair = _mapping_or_empty(audit.get("repair"))
    verifier = _mapping_or_empty(audit.get("verifier"))
    return {
        "status": str(audit.get("status") or "not_run"),
        "reason_code": _preview(audit.get("reason_code"), 80),
        "counts": {
            key: _nonnegative_int(counts.get(key, 0))
            for key in (
                "claim_count",
                "supported",
                "unsupported",
                "insufficient",
                "cited",
                "skipped_statements",
            )
            if key in counts
        },
        "metrics": {
            key: _finite_float_or_none(metrics.get(key))
            for key in (
                "claim_support_rate",
                "citation_coverage",
                "unsupported_claim_rate",
            )
            if key in metrics
        },
        "repair": {
            "attempted": bool(repair.get("attempted", False)),
            "attempt_count": _nonnegative_int(repair.get("attempt_count", 0)),
            "succeeded": bool(repair.get("succeeded", False)),
        },
        "duration_ms": _finite_float_or_none(verifier.get("duration_ms")),
    }


def _claim_verification_summary(
    output_payload: Mapping[str, Any] | None,
) -> dict | None:
    if not isinstance(output_payload, Mapping):
        return None
    rollout = output_payload.get("claim_verification_rollout")
    if not isinstance(rollout, Mapping):
        return None
    mode = str(rollout.get("mode") or "off")
    if mode not in {"off", "shadow", "enforce"}:
        mode = "off"
    decision = str(rollout.get("decision") or "skipped")
    if decision not in ROLLOUT_DECISIONS:
        decision = "skipped"
    policy = claim_verification_policy_projection(rollout, effective_mode=mode)
    return {
        "version": "v1",
        "mode": mode,
        **policy,
        "decision": decision,
        "executed": bool(rollout.get("executed", False)),
        "enforced": bool(rollout.get("enforced", False)),
        "released": bool(rollout.get("released", True)),
        "would_intervene": bool(rollout.get("would_intervene", False)),
        "would_repair": bool(rollout.get("would_repair", False)),
        "would_block": bool(rollout.get("would_block", False)),
        "audit_status": _preview(rollout.get("audit_status"), 32),
        "reason_code": _preview(rollout.get("reason_code"), 128),
        "repair_count": _nonnegative_int(rollout.get("repair_count")),
    }


# 构建跟踪导出载荷。
def build_trace_payload(
    trace_id: str,
    request_id: str,
    task_type: str,
    steps: list[dict],
    status: str = "ok",
    duration_ms: float | None = None,
    error: Mapping[str, Any] | None = None,
    config: Mapping[str, Any] | None = None,
    input_payload: Mapping[str, Any] | None = None,
    output_payload: Mapping[str, Any] | None = None,
    execution_status: str | None = None,
) -> dict:
    resolved_status = execution_status or (
        "SUCCESS"
        if status == "ok"
        else "TRACE_INCOMPLETE"
        if status == "degraded"
        else "TARGET_ERROR"
    )
    required_evidence = {"input", "output", "steps"}
    available_evidence = {
        name
        for name, value in {
            "input": input_payload,
            "output": output_payload,
            "steps": steps,
        }.items()
        if value is not None and (value or name == "steps")
    }
    evidence_completeness = len(required_evidence & available_evidence) / len(
        required_evidence
    )
    summary = summarize_trace_steps(steps)
    claim_summary = _claim_audit_summary(output_payload)
    if claim_summary is not None:
        summary["claim_audit"] = claim_summary
    verification_summary = _claim_verification_summary(output_payload)
    if verification_summary is not None:
        summary["claim_verification"] = verification_summary
    return {
        "schema_version": TRACE_SCHEMA_VERSION,
        "trace_id": trace_id,
        "request_id": request_id,
        "task_type": task_type,
        "status": status,
        "duration_ms": None if duration_ms is None else round(max(duration_ms, 0.0), 3),
        "execution_status": resolved_status,
        "input": _json_safe(dict(input_payload or {})),
        "output": _json_safe(dict(output_payload or {})),
        "evidence_completeness": evidence_completeness,
        "config": _json_safe(dict(config or {})),
        "summary": summary,
        "error": _json_safe(dict(error or {})) or None,
        "steps": _json_safe(steps),
    }


# 导出跟踪文件。
def export_trace(
    trace_id: str,
    request_id: str,
    task_type: str,
    steps: list[dict],
    settings: Settings | None = None,
    status: str = "ok",
    duration_ms: float | None = None,
    error: Mapping[str, Any] | None = None,
    config: Mapping[str, Any] | None = None,
    input_payload: Mapping[str, Any] | None = None,
    output_payload: Mapping[str, Any] | None = None,
    execution_status: str | None = None,
) -> Path | None:
    settings = settings or get_settings()
    if not settings.cogdoc_trace_enabled:
        return None

    path = trace_path(trace_id, settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = build_trace_payload(
        trace_id=trace_id,
        request_id=request_id,
        task_type=task_type,
        steps=steps,
        status=status,
        duration_ms=duration_ms,
        error=error,
        config=config,
        input_payload=input_payload,
        output_payload=output_payload,
        execution_status=execution_status,
    )
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
