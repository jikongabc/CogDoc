from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any, TypeGuard

from cogdoc.config.settings import get_settings
from cogdoc.service.index_provenance import current_index_provenance


RESEARCH_PROVENANCE_VERSION = "research-provenance-v1"
RESEARCH_CONTRACT_VERSION = "research-retrieval-verification-v2"
_IDENTITY_FIELDS = (
    "kb_epoch",
    "acl_epoch",
    "index_generation",
    "index_build_version",
    "chunk_identity_version",
    "derived_knowledge_revision",
    "retrieval_tuning_revision",
    "research_contract_version",
    "research_contract_revision",
)

# These are the runtime settings that can change research retrieval, evidence
# verification, claim/requirement auditing, or the generated report.  API keys
# are deliberately excluded: credential rotation must not make evidence stale.
_RESEARCH_CONTRACT_SETTING_FIELDS = (
    "cogdoc_derived_knowledge_index_auto_refresh",
    "cogdoc_research_retrieval_top_k",
    "hybrid_rrf_k",
    "qa_rerank_on_cpu",
    "reranker_min_cuda_free_mb",
    "evidence_unit_verify_max_chars_per_doc",
    "evidence_unit_verify_max_units_per_batch",
    "claim_verification_max_claims",
    "claim_verification_max_claims_per_batch",
    "claim_verification_max_docs_per_batch",
    "claim_verification_max_chars_per_doc",
    "claim_verification_max_repair_attempts",
    "cogdoc_research_provider_call_timeout_seconds",
    "llm_structured_output_method",
    "llm_model_name",
    "llm_base_url",
    "llm_timeout_seconds",
    "llm_max_retries",
    "ollama_model_name",
    "ollama_base_url",
    "ollama_timeout_seconds",
    "ollama_max_retries",
    "llm_research_planner_model_name",
    "llm_source_resolver_model_name",
    "llm_evidence_verifier_model_name",
    "llm_claim_verifier_model_name",
    "llm_claim_repairer_model_name",
    "llm_summary_generator_model_name",
    "ollama_research_planner_model_name",
    "ollama_source_resolver_model_name",
    "ollama_evidence_verifier_model_name",
    "ollama_claim_verifier_model_name",
    "ollama_claim_repairer_model_name",
    "ollama_summary_generator_model_name",
    "llm_research_planner_backend",
    "llm_source_resolver_backend",
    "llm_evidence_verifier_backend",
    "llm_claim_verifier_backend",
    "llm_claim_repairer_backend",
    "llm_summary_generator_backend",
)


def _canonical_hash(rows: Sequence[Mapping[str, Any]]) -> str:
    payload = json.dumps(
        list(rows),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str) and value.strip():
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return []
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    return sorted({str(item) for item in value if str(item)})


def _integer(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0


def _effective_float(value: Any, *, default: float) -> float | str:
    """Mirror feedback boost coercion while keeping corrupt rows hashable."""

    effective = value or default
    try:
        number = float(effective)
    except (TypeError, ValueError, OverflowError):
        return f"invalid:{effective!r}"
    return number if math.isfinite(number) else f"invalid:{effective!r}"


def _sort_canonical_rows(rows: list[dict[str, Any]], *, id_field: str) -> None:
    rows.sort(
        key=lambda row: (
            str(row.get(id_field) or ""),
            json.dumps(
                row,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
    )


def _derived_knowledge_revision(state_runtime, kb_id: str) -> str:
    store = getattr(state_runtime, "knowledge_store", None)
    if store is None:
        return ""
    rows = store.list(kb_id=kb_id, status="approved")
    canonical = [
        {
            "knowledge_id": str(row.get("knowledge_id") or ""),
            "kb_id": str(row.get("kb_id") or ""),
            "text": str(row.get("text") or ""),
            "normalized_hash": str(row.get("normalized_hash") or ""),
            "version": _integer(row.get("version")) or 1,
            "origin": str(row.get("origin") or "manual_entry"),
            "status": str(row.get("status") or ""),
            "certainty": str(row.get("certainty") or ""),
            "source_note": str(row.get("source_note") or ""),
            "related_document_id": str(row.get("related_document_id") or ""),
            "related_source": str(row.get("related_source") or ""),
            "related_source_sha256": str(row.get("related_source_sha256") or ""),
            "related_chunk_ids": _string_list(row.get("related_chunk_ids")),
            "related_page_start": _integer(row.get("related_page_start")),
            "related_page_end": _integer(row.get("related_page_end")),
            "related_chunk_text_hash": str(row.get("related_chunk_text_hash") or ""),
            "related_anchor_text": str(row.get("related_anchor_text") or ""),
        }
        for row in rows
        if isinstance(row, Mapping)
    ]
    _sort_canonical_rows(canonical, id_field="knowledge_id")
    return _canonical_hash(canonical)


def _feedback_target_chunk_ids(row: Mapping[str, Any]) -> list[str]:
    """Return the chunk identities actually consumed by boosts_for_query()."""

    chunk_ids: set[str] = set()
    raw_targets = row.get("target_chunks")
    if isinstance(raw_targets, list):
        for item in raw_targets:
            if isinstance(item, Mapping):
                chunk_id = str(item.get("chunk_id") or "")
                if chunk_id:
                    chunk_ids.add(chunk_id)
    legacy_chunk_id = str(row.get("chunk_id") or "")
    if legacy_chunk_id:
        chunk_ids.add(legacy_chunk_id)
    return sorted(chunk_ids)


def _feedback_rows(store, kb_id: str) -> list[Mapping[str, Any]]:
    exporter = getattr(store, "export_records", None)
    if callable(exporter):
        try:
            exported = exporter()
        except Exception:
            exported = None
        if isinstance(exported, Sequence) and not isinstance(
            exported, (str, bytes, bytearray)
        ):
            return [
                row
                for row in exported
                if isinstance(row, Mapping)
                and row.get("kb_id") == kb_id
                and row.get("enabled") is True
            ]
    rows = store.list(kb_id=kb_id, enabled=True, limit=2**31 - 1)
    return [row for row in rows if isinstance(row, Mapping)]


def _retrieval_tuning_revision(state_runtime, kb_id: str) -> str:
    store = getattr(state_runtime, "retrieval_feedback_store", None)
    if store is None:
        return ""
    rows = _feedback_rows(store, kb_id)
    canonical = [
        {
            "retrieval_feedback_id": str(
                row.get("retrieval_feedback_id") or row.get("feedback_id") or ""
            ),
            "query_hash": str(row.get("query_hash") or ""),
            "weight_delta": _effective_float(row.get("weight_delta"), default=0.0),
            "confidence": _effective_float(row.get("confidence"), default=1.0),
            "target_chunk_ids": _feedback_target_chunk_ids(row),
        }
        for row in rows
    ]
    _sort_canonical_rows(canonical, id_field="retrieval_feedback_id")
    return _canonical_hash(canonical)


def _research_contract_revision() -> str:
    from cogdoc.tools.reranker import BGEReranker

    settings = get_settings()
    contract = {
        "contract_version": RESEARCH_CONTRACT_VERSION,
        # The document/derived indexes already expose the embedding and
        # tokenizer contracts through index_build_version.  The reranker is a
        # query-time dependency, so its stable model identity belongs here.
        "reranker": {
            "model": getattr(BGEReranker, "MODEL_NAME", ""),
            "revision": getattr(BGEReranker, "MODEL_REVISION", ""),
            "max_length": getattr(BGEReranker, "MAX_LENGTH", None),
        },
        "settings": {
            name: getattr(settings, name, None)
            for name in _RESEARCH_CONTRACT_SETTING_FIELDS
        },
    }
    return _canonical_hash([contract])


def capture_research_provenance(
    kb_id: str,
    *,
    state_runtime,
    index_provenance_reader=None,
    include_auxiliary_state: bool = True,
) -> dict[str, Any]:
    """Freeze every persisted input that can alter a research evidence run."""

    snapshot = (
        current_index_provenance(kb_id)
        if index_provenance_reader is None
        else dict(index_provenance_reader(kb_id))
    )
    auxiliary_revision = hashlib.sha256(b"ha-primary-index-only-v1").hexdigest()
    return {
        "schema_version": RESEARCH_PROVENANCE_VERSION,
        "kb_id": kb_id,
        **snapshot,
        "derived_knowledge_revision": (
            _derived_knowledge_revision(state_runtime, kb_id)
            if include_auxiliary_state
            else auxiliary_revision
        ),
        "retrieval_tuning_revision": (
            _retrieval_tuning_revision(state_runtime, kb_id)
            if include_auxiliary_state
            else auxiliary_revision
        ),
        "research_contract_version": RESEARCH_CONTRACT_VERSION,
        "research_contract_revision": _research_contract_revision(),
        "captured_at": datetime.now(timezone.utc).isoformat(),
    }


def is_trackable_research_provenance(
    snapshot: Mapping[str, Any] | None,
) -> TypeGuard[Mapping[str, Any]]:
    if not isinstance(snapshot, Mapping):
        return False
    return bool(
        snapshot.get("schema_version") == RESEARCH_PROVENANCE_VERSION
        and snapshot.get("kb_id")
        and snapshot.get("index_generation")
        and snapshot.get("index_build_version")
        and snapshot.get("chunk_identity_version")
        and snapshot.get("derived_knowledge_revision")
        and snapshot.get("retrieval_tuning_revision")
        and snapshot.get("research_contract_version")
        and snapshot.get("research_contract_revision")
    )


def research_provenance_stale_reasons(
    captured: Mapping[str, Any] | None,
    current: Mapping[str, Any] | None,
) -> tuple[str, ...]:
    if not is_trackable_research_provenance(captured):
        return ("evidence_provenance_untracked",)
    if not is_trackable_research_provenance(current):
        return ("current_index_provenance_unavailable",)
    reasons: list[str] = []
    if str(captured.get("kb_id") or "") != str(current.get("kb_id") or ""):
        reasons.append("kb_id_changed")
    for field in _IDENTITY_FIELDS:
        if str(captured.get(field) or "") != str(current.get(field) or ""):
            reasons.append(f"{field}_changed")

    def versions(snapshot: Mapping[str, Any]) -> dict[str, str]:
        rows = snapshot.get("source_versions")
        if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes, bytearray)):
            return {}
        return {
            str(row.get("source") or ""): str(
                row.get("sha256") or row.get("source_sha256") or ""
            )
            for row in rows
            if isinstance(row, Mapping) and row.get("source")
        }

    before = versions(captured)
    after = versions(current)
    for source in sorted(before.keys() - after.keys()):
        reasons.append(f"source_removed:{source}")
    for source in sorted(after.keys() - before.keys()):
        reasons.append(f"source_added:{source}")
    for source in sorted(before.keys() & after.keys()):
        if before[source] != after[source]:
            reasons.append(f"source_sha256_changed:{source}")
    return tuple(dict.fromkeys(reasons))


def research_provenance_status(
    captured: Mapping[str, Any] | None,
    current: Mapping[str, Any] | None,
) -> dict[str, Any]:
    reasons = research_provenance_stale_reasons(captured, current)
    if reasons == ("evidence_provenance_untracked",):
        status = "untracked"
    elif reasons:
        status = "stale"
    else:
        status = "current"
    return {
        "status": status,
        "stale_reasons": list(reasons),
        "captured": dict(captured or {}),
        "current": dict(current or {}),
    }


RESEARCH_ARTIFACT_VERSION = "research-artifact-v2"
RESEARCH_VERIFICATION_VERSION = "research-verification-v2"

_VERIFICATION_SECTION_LIMIT = 12
_VERIFICATION_REQUIREMENT_LIMIT = 16
_VERIFICATION_EVIDENCE_LIMIT = 128
_VERIFICATION_METRIC_KEY_LIMIT = 128
_VERIFICATION_TEXT_LIMIT = 256

_CLAIM_COUNT_KEYS = (
    "claim_count",
    "supported",
    "unsupported",
    "insufficient",
    "cited",
    "skipped_statements",
)
_CLAIM_METRIC_KEYS = (
    "claim_support_rate",
    "citation_coverage",
    "unsupported_claim_rate",
)
_CLAIM_AUDIT_KEYS = {
    "status",
    "reason_code",
    "counts",
    "metrics",
    "repair",
    "verifier",
}
_COVERAGE_AUDIT_KEYS = {
    "status",
    "reason_code",
    "requirement_count",
    "covered_count",
    "missing_requirement_ids",
    "repair",
    "auditor",
}
_REPAIR_KEYS = {"attempted", "attempt_count", "succeeded", "error"}
_VERIFIER_KEYS = {"duration_ms", "call_count", "version"}
_AUDITOR_KEYS = {"call_count", "version"}
_REQUIREMENT_RESULT_KEYS = {
    "requirement_id",
    "status",
    "reason_code",
    "evidence_count",
}
_REQUIREMENT_PLAN_KEYS = {
    "requirement_id",
    "question",
    "retrieval_query",
    "recovery_query",
}
_VERIFICATION_EXECUTION_KEYS = {
    "job_id",
    "kb_id",
    "execution_id",
    "report_execution_id",
    "title",
    "objective",
    "is_local",
    "nodes",
}
_VERIFICATION_NODE_KEYS = {"node", "backend", "model", "protocol_version"}
_RESEARCH_REPORT_NODES = (
    "evidence_verifier",
    "summary_generator",
    "claim_verifier",
    "claim_repairer",
)
_EVIDENCE_COMMITMENT_KEYS = {
    "chunk_id",
    "source_type",
    "knowledge_id",
    "source",
    "source_sha256",
    "text_hash",
    "page",
    "page_start",
    "page_end",
    "span_start",
    "span_end",
    "section_title",
    "search_channel",
    "rerank_score",
    "rrf_score",
}
_VERIFICATION_SECTION_KEYS = {
    "section_id",
    "position",
    "title",
    "research_question",
    "success_criteria",
    "revision_instruction",
    "requirements",
    "generation_status",
    "verification_status",
    "verification_reason_code",
    "requirement_results",
    "claim_audit",
    "coverage_audit",
    "evidence_commitments",
}
_ARTIFACT_METADATA_KEYS = {"version", "generated_at"}


def _plain_json(value: Any, *, path: str = "artifact") -> None:
    """Reject values that JSON would silently coerce or emit non-portably."""

    if value is None or type(value) in {str, bool, int}:
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError(f"{path} contains a non-finite number")
        return
    if type(value) is list:
        for index, item in enumerate(value):
            _plain_json(item, path=f"{path}[{index}]")
        return
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise TypeError(f"{path} contains a non-string object key")
            _plain_json(item, path=f"{path}.{key}")
        return
    raise TypeError(f"{path} must contain only plain JSON values")


def _canonical_json(value: Any) -> str:
    _plain_json(value)
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _bounded_source_text(
    row: Mapping[str, Any],
    key: str,
    *,
    default: str = "",
    limit: int = _VERIFICATION_TEXT_LIMIT,
) -> str:
    value = row.get(key, default)
    if value is None:
        value = default
    if type(value) is not str:
        raise TypeError(f"verification {key} must be a string")
    if len(value) > limit:
        raise ValueError(f"verification {key} exceeds its size limit")
    return value


def _bounded_source_int(row: Mapping[str, Any], key: str, *, default: int = 0) -> int:
    value = row.get(key, default)
    if type(value) is not int:
        raise TypeError(f"verification {key} must be an integer")
    if value < 0:
        raise ValueError(f"verification {key} must be non-negative")
    return value


def _bounded_source_number(
    row: Mapping[str, Any], key: str, *, default: int | float = 0
) -> int | float:
    value = row.get(key, default)
    if type(value) not in {int, float}:
        raise TypeError(f"verification {key} must be a number")
    if type(value) is float and not math.isfinite(value):
        raise ValueError(f"verification {key} must be finite")
    return value


def _bounded_source_bool(
    row: Mapping[str, Any], key: str, *, default: bool = False
) -> bool:
    value = row.get(key, default)
    if type(value) is not bool:
        raise TypeError(f"verification {key} must be a boolean")
    return value


def _metric_mapping(value: Mapping[str, Any], *, depth: int = 0) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError("verification aggregate metrics must be a mapping")
    if len(value) > _VERIFICATION_METRIC_KEY_LIMIT:
        raise ValueError("verification aggregate metrics exceed their size limit")
    output: dict[str, Any] = {}
    for key, item in value.items():
        if type(key) is not str or not key or len(key) > 128:
            raise TypeError("verification metric keys must be bounded strings")
        if isinstance(item, Mapping):
            if depth >= 2:
                raise ValueError("verification aggregate metrics are too deeply nested")
            output[key] = _metric_mapping(item, depth=depth + 1)
        elif item is None or type(item) in {bool, int}:
            output[key] = item
        elif type(item) is float:
            if not math.isfinite(item):
                raise ValueError("verification metrics must be finite")
            output[key] = item
        else:
            raise TypeError(
                "verification metrics must contain only numbers, booleans, null, "
                "or nested metric mappings"
            )
    return output


def _repair_summary(value: Any) -> dict[str, Any]:
    raw = value if isinstance(value, Mapping) else {}
    return {
        "attempted": _bounded_source_bool(raw, "attempted"),
        "attempt_count": _bounded_source_int(raw, "attempt_count"),
        "succeeded": _bounded_source_bool(raw, "succeeded"),
        "error": _bounded_source_text(raw, "error", limit=64),
    }


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _claim_audit_summary(value: Any) -> dict[str, Any]:
    raw = value if isinstance(value, Mapping) else {}
    counts = _as_mapping(raw.get("counts"))
    metrics = _as_mapping(raw.get("metrics"))
    verifier = _as_mapping(raw.get("verifier"))
    return {
        "status": _bounded_source_text(raw, "status", default="not_run", limit=32),
        "reason_code": _bounded_source_text(raw, "reason_code", limit=128),
        "counts": {key: _bounded_source_int(counts, key) for key in _CLAIM_COUNT_KEYS},
        "metrics": {
            key: (
                None
                if metrics.get(key) is None
                else _bounded_source_number(metrics, key)
            )
            for key in _CLAIM_METRIC_KEYS
        },
        "repair": _repair_summary(raw.get("repair")),
        "verifier": {
            "duration_ms": _bounded_source_number(verifier, "duration_ms", default=0.0),
            "call_count": _bounded_source_int(verifier, "call_count"),
            "version": _bounded_source_text(
                verifier, "version", default="v1", limit=16
            ),
        },
    }


def _coverage_audit_summary(value: Any) -> dict[str, Any]:
    raw = value if isinstance(value, Mapping) else {}
    missing = raw.get("missing_requirement_ids", [])
    if not isinstance(missing, Sequence) or isinstance(
        missing, (str, bytes, bytearray)
    ):
        raise TypeError("coverage missing_requirement_ids must be a sequence")
    if len(missing) > _VERIFICATION_REQUIREMENT_LIMIT:
        raise ValueError("coverage missing_requirement_ids exceed their size limit")
    missing_ids = []
    for requirement_id in missing:
        if type(requirement_id) is not str or not requirement_id:
            raise TypeError("coverage requirement IDs must be non-empty strings")
        if len(requirement_id) > 128:
            raise ValueError("coverage requirement ID exceeds its size limit")
        missing_ids.append(requirement_id)
    if len(set(missing_ids)) != len(missing_ids):
        raise ValueError("coverage missing requirement IDs must be unique")
    auditor = _as_mapping(raw.get("auditor"))
    return {
        "status": _bounded_source_text(raw, "status", default="not_run", limit=32),
        "reason_code": _bounded_source_text(raw, "reason_code", limit=128),
        "requirement_count": _bounded_source_int(raw, "requirement_count"),
        "covered_count": _bounded_source_int(raw, "covered_count"),
        "missing_requirement_ids": missing_ids,
        "repair": _repair_summary(raw.get("repair")),
        "auditor": {
            "call_count": _bounded_source_int(auditor, "call_count"),
            "version": _bounded_source_text(auditor, "version", default="v1", limit=16),
        },
    }


def _requirement_results(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise TypeError("evidence requirement results must be a sequence")
    if len(value) > _VERIFICATION_REQUIREMENT_LIMIT:
        raise ValueError("evidence requirement results exceed their size limit")
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in value:
        if not isinstance(raw, Mapping):
            raise TypeError("evidence requirement results must contain mappings")
        requirement_id = _bounded_source_text(raw, "requirement_id", limit=128)
        result = {
            "requirement_id": requirement_id,
            "status": _bounded_source_text(raw, "status", limit=32),
            "reason_code": _bounded_source_text(raw, "reason_code", limit=128),
            "evidence_count": _bounded_source_int(raw, "evidence_count"),
        }
        if not requirement_id or requirement_id in seen:
            raise ValueError("evidence requirement IDs must be non-empty and unique")
        seen.add(requirement_id)
        results.append(result)
    return results


def _optional_source_int(row: Mapping[str, Any], key: str) -> int | None:
    value = row.get(key)
    if value is None:
        return None
    return _bounded_source_int(row, key)


def _optional_source_number(row: Mapping[str, Any], key: str) -> int | float | None:
    value = row.get(key)
    if value is None:
        return None
    return _bounded_source_number(row, key)


def _evidence_commitments(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise TypeError("research evidence must be a sequence")
    if len(value) > _VERIFICATION_EVIDENCE_LIMIT:
        raise ValueError("research evidence exceeds its size limit")
    commitments: list[dict[str, Any]] = []
    for raw in value:
        if not isinstance(raw, Mapping):
            raise TypeError("research evidence must contain mappings")
        commitments.append(
            {
                "chunk_id": _bounded_source_text(raw, "chunk_id", limit=512),
                "source_type": _bounded_source_text(
                    raw, "source_type", default="document", limit=64
                ),
                "knowledge_id": _bounded_source_text(raw, "knowledge_id", limit=256),
                "source": _bounded_source_text(raw, "source", limit=512),
                "source_sha256": _bounded_source_text(raw, "source_sha256", limit=128),
                "text_hash": _bounded_source_text(raw, "text_hash", limit=128),
                "page": _optional_source_int(raw, "page"),
                "page_start": _optional_source_int(raw, "page_start"),
                "page_end": _optional_source_int(raw, "page_end"),
                "span_start": _optional_source_int(raw, "span_start"),
                "span_end": _optional_source_int(raw, "span_end"),
                "section_title": _bounded_source_text(raw, "section_title", limit=256),
                "search_channel": _bounded_source_text(
                    raw, "search_channel", limit=128
                ),
                "rerank_score": _optional_source_number(raw, "rerank_score"),
                "rrf_score": _optional_source_number(raw, "rrf_score"),
            }
        )
    return commitments


def freeze_research_execution_nodes(*, is_local: bool) -> list[dict[str, str]]:
    """Resolve report nodes once; callers persist this execution-time fact."""

    settings = get_settings()
    nodes: list[dict[str, str]] = []
    for node in _RESEARCH_REPORT_NODES:
        effective_is_local = settings.is_local_for_node(node, request_is_local=is_local)
        if is_local and not effective_is_local:
            raise ValueError(f"local research mode cannot commit cloud node {node}")
        nodes.append(
            {
                "node": node,
                "backend": "local" if effective_is_local else "cloud",
                "model": settings.model_name_for_node(
                    node, is_local=effective_is_local
                ),
                "protocol_version": RESEARCH_CONTRACT_VERSION,
            }
        )
    return nodes


def _execution_node_commitments(value: Any) -> list[dict[str, str]]:
    if type(value) is not list or len(value) != len(_RESEARCH_REPORT_NODES):
        raise TypeError("research execution nodes have an invalid size")
    nodes: list[dict[str, str]] = []
    for expected_node, raw in zip(_RESEARCH_REPORT_NODES, value, strict=True):
        if type(raw) is not dict or set(raw) != _VERIFICATION_NODE_KEYS:
            raise TypeError("research execution node has an invalid field set")
        node = {
            "node": _bounded_source_text(raw, "node", limit=64),
            "backend": _bounded_source_text(raw, "backend", limit=16),
            "model": _bounded_source_text(raw, "model", limit=256),
            "protocol_version": _bounded_source_text(
                raw, "protocol_version", limit=128
            ),
        }
        if node["node"] != expected_node:
            raise ValueError("research execution node order is invalid")
        if node["backend"] not in {"local", "cloud"}:
            raise ValueError("research execution node backend is invalid")
        if not node["model"] or not node["protocol_version"]:
            raise ValueError("research execution node identity is incomplete")
        nodes.append(node)
    return nodes


def _execution_commitment(job: Mapping[str, Any]) -> dict[str, Any]:
    request_is_local = _bounded_source_bool(job, "is_local")
    nodes = _execution_node_commitments(job.get("report_execution_nodes"))
    if request_is_local and any(node["backend"] != "local" for node in nodes):
        raise ValueError("local research execution contains a cloud node")
    commitment = {
        "job_id": _bounded_source_text(job, "job_id", limit=128),
        "kb_id": _bounded_source_text(job, "kb_id", limit=128),
        "execution_id": _bounded_source_text(job, "execution_id", limit=128),
        "report_execution_id": _bounded_source_text(
            job, "report_execution_id", limit=128
        ),
        "title": _bounded_source_text(job, "title", limit=160),
        "objective": _bounded_source_text(job, "objective", limit=4000),
        "is_local": request_is_local,
        "nodes": nodes,
    }
    for key in (
        "job_id",
        "kb_id",
        "execution_id",
        "report_execution_id",
        "title",
        "objective",
    ):
        if not commitment[key]:
            raise ValueError(f"research verification execution.{key} is required")
    return commitment


def _requirement_plan(value: Any) -> list[dict[str, str]]:
    if type(value) is not list or not value:
        raise TypeError("research verification requirements must be a non-empty list")
    if len(value) > _VERIFICATION_REQUIREMENT_LIMIT:
        raise ValueError("research verification requirements exceed their size limit")
    requirements: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in value:
        if type(raw) is not dict:
            raise TypeError("research verification requirements must contain objects")
        requirement = {
            "requirement_id": _bounded_source_text(raw, "requirement_id", limit=128),
            "question": _bounded_source_text(raw, "question", limit=1000),
            "retrieval_query": _bounded_source_text(raw, "retrieval_query", limit=1000),
            "recovery_query": _bounded_source_text(raw, "recovery_query", limit=1000),
        }
        if any(not value for value in requirement.values()):
            raise ValueError("research verification requirement fields are required")
        requirement_id = requirement["requirement_id"]
        if requirement_id in seen:
            raise ValueError("research verification requirement IDs must be unique")
        seen.add(requirement_id)
        requirements.append(requirement)
    return requirements


def build_research_verification_snapshot(
    *,
    job: Mapping[str, Any],
    verification_metrics: Mapping[str, Any],
    sections: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Build the bounded, body-free verification commitment for one report."""

    if not isinstance(sections, Sequence) or isinstance(
        sections, (str, bytes, bytearray)
    ):
        raise TypeError("research verification sections must be a sequence")
    if len(sections) > _VERIFICATION_SECTION_LIMIT:
        raise ValueError("research verification sections exceed their size limit")
    projected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in sections:
        if not isinstance(raw, Mapping):
            raise TypeError("research verification sections must contain mappings")
        section_id = _bounded_source_text(raw, "section_id", limit=128)
        if not section_id or section_id in seen:
            raise ValueError("research verification section IDs must be unique")
        seen.add(section_id)
        generation_key = "generation_status" if "generation_status" in raw else "status"
        requirements = _requirement_plan(raw.get("evidence_requirements"))
        declared_requirement_ids = raw.get("evidence_requirement_ids")
        requirement_ids = [
            requirement["requirement_id"] for requirement in requirements
        ]
        if (
            type(declared_requirement_ids) is not list
            or declared_requirement_ids != requirement_ids
        ):
            raise ValueError(
                "research verification requirement IDs do not match the plan"
            )
        projected.append(
            {
                "section_id": section_id,
                "position": _bounded_source_int(raw, "position"),
                "title": _bounded_source_text(raw, "title", limit=160),
                "research_question": _bounded_source_text(
                    raw, "research_question", limit=2000
                ),
                "success_criteria": _bounded_source_text(
                    raw, "success_criteria", limit=1000
                ),
                "revision_instruction": _bounded_source_text(
                    raw, "revision_instruction", limit=2000
                ),
                "requirements": requirements,
                "generation_status": _bounded_source_text(
                    raw, generation_key, limit=32
                ),
                "verification_status": _bounded_source_text(
                    raw, "verification_status", limit=32
                ),
                "verification_reason_code": _bounded_source_text(
                    raw, "verification_reason_code", limit=128
                ),
                "requirement_results": _requirement_results(
                    raw.get("evidence_requirement_results", [])
                ),
                "claim_audit": _claim_audit_summary(raw.get("claim_audit")),
                "coverage_audit": _coverage_audit_summary(raw.get("coverage_audit")),
                "evidence_commitments": _evidence_commitments(raw.get("evidence", [])),
            }
        )
    snapshot = {
        "schema_version": RESEARCH_VERIFICATION_VERSION,
        "execution": _execution_commitment(job),
        "aggregate": _metric_mapping(verification_metrics),
        "sections": projected,
    }
    validate_research_verification_snapshot(snapshot)
    return snapshot


def _require_exact_keys(value: Any, expected: set[str], *, path: str) -> dict[str, Any]:
    if type(value) is not dict:
        raise TypeError(f"{path} must be a plain object")
    if set(value) != expected:
        raise ValueError(f"{path} has an invalid field set")
    return value


def _validate_fixed_text(value: Any, *, path: str, limit: int) -> None:
    if type(value) is not str:
        raise TypeError(f"{path} must be a string")
    if len(value) > limit:
        raise ValueError(f"{path} exceeds its size limit")


def _validate_nonnegative_int(value: Any, *, path: str) -> None:
    if type(value) is not int or value < 0:
        raise TypeError(f"{path} must be a non-negative integer")


def _validate_number(value: Any, *, path: str, optional: bool = False) -> None:
    if optional and value is None:
        return
    if type(value) not in {int, float}:
        raise TypeError(f"{path} must be a number")
    if type(value) is float and not math.isfinite(value):
        raise ValueError(f"{path} must be finite")


def _validate_repair(value: Any, *, path: str) -> None:
    repair = _require_exact_keys(value, _REPAIR_KEYS, path=path)
    if type(repair["attempted"]) is not bool:
        raise TypeError(f"{path}.attempted must be a boolean")
    _validate_nonnegative_int(repair["attempt_count"], path=f"{path}.attempt_count")
    if type(repair["succeeded"]) is not bool:
        raise TypeError(f"{path}.succeeded must be a boolean")
    _validate_fixed_text(repair["error"], path=f"{path}.error", limit=64)


def _validate_claim_audit(value: Any, *, path: str) -> None:
    audit = _require_exact_keys(value, _CLAIM_AUDIT_KEYS, path=path)
    _validate_fixed_text(audit["status"], path=f"{path}.status", limit=32)
    _validate_fixed_text(audit["reason_code"], path=f"{path}.reason_code", limit=128)
    counts = _require_exact_keys(
        audit["counts"], set(_CLAIM_COUNT_KEYS), path=f"{path}.counts"
    )
    for key in _CLAIM_COUNT_KEYS:
        _validate_nonnegative_int(counts[key], path=f"{path}.counts.{key}")
    metrics = _require_exact_keys(
        audit["metrics"], set(_CLAIM_METRIC_KEYS), path=f"{path}.metrics"
    )
    for key in _CLAIM_METRIC_KEYS:
        _validate_number(metrics[key], path=f"{path}.metrics.{key}", optional=True)
    _validate_repair(audit["repair"], path=f"{path}.repair")
    verifier = _require_exact_keys(
        audit["verifier"], _VERIFIER_KEYS, path=f"{path}.verifier"
    )
    _validate_number(verifier["duration_ms"], path=f"{path}.verifier.duration_ms")
    _validate_nonnegative_int(
        verifier["call_count"], path=f"{path}.verifier.call_count"
    )
    _validate_fixed_text(verifier["version"], path=f"{path}.verifier.version", limit=16)


def _validate_coverage_audit(value: Any, *, path: str) -> None:
    audit = _require_exact_keys(value, _COVERAGE_AUDIT_KEYS, path=path)
    _validate_fixed_text(audit["status"], path=f"{path}.status", limit=32)
    _validate_fixed_text(audit["reason_code"], path=f"{path}.reason_code", limit=128)
    _validate_nonnegative_int(
        audit["requirement_count"], path=f"{path}.requirement_count"
    )
    _validate_nonnegative_int(audit["covered_count"], path=f"{path}.covered_count")
    if audit["covered_count"] > audit["requirement_count"]:
        raise ValueError(f"{path}.covered_count exceeds requirement_count")
    missing = audit["missing_requirement_ids"]
    if type(missing) is not list or len(missing) > _VERIFICATION_REQUIREMENT_LIMIT:
        raise TypeError(f"{path}.missing_requirement_ids must be a bounded list")
    for index, requirement_id in enumerate(missing):
        _validate_fixed_text(
            requirement_id,
            path=f"{path}.missing_requirement_ids[{index}]",
            limit=128,
        )
    if len(set(missing)) != len(missing):
        raise ValueError(f"{path}.missing_requirement_ids must be unique")
    _validate_repair(audit["repair"], path=f"{path}.repair")
    auditor = _require_exact_keys(
        audit["auditor"], _AUDITOR_KEYS, path=f"{path}.auditor"
    )
    _validate_nonnegative_int(auditor["call_count"], path=f"{path}.auditor.call_count")
    _validate_fixed_text(auditor["version"], path=f"{path}.auditor.version", limit=16)


def validate_research_verification_snapshot(snapshot: Any) -> None:
    """Validate the exact persisted verification.json schema without coercion."""

    root = _require_exact_keys(
        snapshot,
        {"schema_version", "execution", "aggregate", "sections"},
        path="verification",
    )
    if root["schema_version"] != RESEARCH_VERIFICATION_VERSION:
        raise ValueError("unsupported research verification schema")
    execution = _require_exact_keys(
        root["execution"],
        _VERIFICATION_EXECUTION_KEYS,
        path="verification.execution",
    )
    for key, limit in (
        ("job_id", 128),
        ("kb_id", 128),
        ("execution_id", 128),
        ("report_execution_id", 128),
        ("title", 160),
        ("objective", 4000),
    ):
        _validate_fixed_text(
            execution[key], path=f"verification.execution.{key}", limit=limit
        )
        if not execution[key]:
            raise ValueError(f"verification.execution.{key} must not be blank")
    if type(execution["is_local"]) is not bool:
        raise TypeError("verification.execution.is_local must be a boolean")
    nodes = execution["nodes"]
    if type(nodes) is not list or len(nodes) != len(_RESEARCH_REPORT_NODES):
        raise TypeError("verification.execution.nodes has an invalid size")
    seen_nodes: set[str] = set()
    for node_index, node_value in enumerate(nodes):
        node_path = f"verification.execution.nodes[{node_index}]"
        node = _require_exact_keys(node_value, _VERIFICATION_NODE_KEYS, path=node_path)
        for key, limit in (
            ("node", 64),
            ("backend", 16),
            ("model", 256),
            ("protocol_version", 128),
        ):
            _validate_fixed_text(node[key], path=f"{node_path}.{key}", limit=limit)
            if not node[key]:
                raise ValueError(f"{node_path}.{key} must not be blank")
        if node["node"] in seen_nodes or node["node"] not in _RESEARCH_REPORT_NODES:
            raise ValueError("verification.execution.nodes contains an invalid node")
        if node["backend"] not in {"local", "cloud"}:
            raise ValueError(f"{node_path}.backend is invalid")
        if execution["is_local"] and node["backend"] != "local":
            raise ValueError("local verification execution contains a cloud node")
        seen_nodes.add(node["node"])
    if tuple(node["node"] for node in nodes) != _RESEARCH_REPORT_NODES:
        raise ValueError("verification.execution.nodes order is invalid")
    aggregate = root["aggregate"]
    if type(aggregate) is not dict or _metric_mapping(aggregate) != aggregate:
        raise TypeError("verification.aggregate is not a strict metric mapping")
    sections = root["sections"]
    if type(sections) is not list or len(sections) > _VERIFICATION_SECTION_LIMIT:
        raise TypeError("verification.sections must be a bounded list")
    seen_sections: set[str] = set()
    seen_positions: set[int] = set()
    for section_index, value in enumerate(sections):
        path = f"verification.sections[{section_index}]"
        section = _require_exact_keys(value, _VERIFICATION_SECTION_KEYS, path=path)
        _validate_fixed_text(
            section["section_id"], path=f"{path}.section_id", limit=128
        )
        if not section["section_id"] or section["section_id"] in seen_sections:
            raise ValueError("verification section IDs must be non-empty and unique")
        seen_sections.add(section["section_id"])
        if type(section["position"]) is not int or section["position"] < 1:
            raise TypeError(f"{path}.position must be a positive integer")
        if section["position"] in seen_positions:
            raise ValueError("verification section positions must be unique")
        seen_positions.add(section["position"])
        for key, limit in (
            ("title", 160),
            ("research_question", 2000),
            ("success_criteria", 1000),
            ("revision_instruction", 2000),
        ):
            _validate_fixed_text(section[key], path=f"{path}.{key}", limit=limit)
        if not section["title"] or not section["research_question"]:
            raise ValueError(f"{path} title and research_question must not be blank")
        requirements = section["requirements"]
        if (
            type(requirements) is not list
            or not requirements
            or len(requirements) > _VERIFICATION_REQUIREMENT_LIMIT
        ):
            raise TypeError(f"{path}.requirements must be a non-empty bounded list")
        planned_requirement_ids: list[str] = []
        for requirement_index, requirement_value in enumerate(requirements):
            requirement_path = f"{path}.requirements[{requirement_index}]"
            requirement = _require_exact_keys(
                requirement_value,
                _REQUIREMENT_PLAN_KEYS,
                path=requirement_path,
            )
            for key, limit in (
                ("requirement_id", 128),
                ("question", 1000),
                ("retrieval_query", 1000),
                ("recovery_query", 1000),
            ):
                _validate_fixed_text(
                    requirement[key], path=f"{requirement_path}.{key}", limit=limit
                )
                if not requirement[key]:
                    raise ValueError(f"{requirement_path}.{key} must not be blank")
            planned_requirement_ids.append(requirement["requirement_id"])
        if len(set(planned_requirement_ids)) != len(planned_requirement_ids):
            raise ValueError(f"{path}.requirements must have unique IDs")
        for key, limit in (
            ("generation_status", 32),
            ("verification_status", 32),
            ("verification_reason_code", 128),
        ):
            _validate_fixed_text(section[key], path=f"{path}.{key}", limit=limit)
        requirements = section["requirement_results"]
        if (
            type(requirements) is not list
            or len(requirements) > _VERIFICATION_REQUIREMENT_LIMIT
        ):
            raise TypeError(f"{path}.requirement_results must be a bounded list")
        seen_requirements: set[str] = set()
        for result_index, result_value in enumerate(requirements):
            result_path = f"{path}.requirement_results[{result_index}]"
            result = _require_exact_keys(
                result_value, _REQUIREMENT_RESULT_KEYS, path=result_path
            )
            for key, limit in (
                ("requirement_id", 128),
                ("status", 32),
                ("reason_code", 128),
            ):
                _validate_fixed_text(
                    result[key], path=f"{result_path}.{key}", limit=limit
                )
            if (
                not result["requirement_id"]
                or result["requirement_id"] in seen_requirements
            ):
                raise ValueError(
                    "verification requirement IDs must be non-empty and unique"
                )
            seen_requirements.add(result["requirement_id"])
            _validate_nonnegative_int(
                result["evidence_count"], path=f"{result_path}.evidence_count"
            )
        if section["generation_status"] == "generated":
            result_ids = [result["requirement_id"] for result in requirements]
            if result_ids != planned_requirement_ids:
                raise ValueError(
                    f"{path}.requirement_results do not match planned requirements"
                )
        _validate_claim_audit(section["claim_audit"], path=f"{path}.claim_audit")
        _validate_coverage_audit(
            section["coverage_audit"], path=f"{path}.coverage_audit"
        )
        commitments = section["evidence_commitments"]
        if (
            type(commitments) is not list
            or len(commitments) > _VERIFICATION_EVIDENCE_LIMIT
        ):
            raise TypeError(f"{path}.evidence_commitments must be a bounded list")
        for evidence_index, evidence_value in enumerate(commitments):
            evidence_path = f"{path}.evidence_commitments[{evidence_index}]"
            evidence = _require_exact_keys(
                evidence_value, _EVIDENCE_COMMITMENT_KEYS, path=evidence_path
            )
            for key, limit in (
                ("chunk_id", 512),
                ("source_type", 64),
                ("knowledge_id", 256),
                ("source", 512),
                ("source_sha256", 128),
                ("text_hash", 128),
                ("section_title", 256),
                ("search_channel", 128),
            ):
                _validate_fixed_text(
                    evidence[key], path=f"{evidence_path}.{key}", limit=limit
                )
            for key in (
                "page",
                "page_start",
                "page_end",
                "span_start",
                "span_end",
            ):
                if evidence[key] is not None:
                    _validate_nonnegative_int(
                        evidence[key], path=f"{evidence_path}.{key}"
                    )
            if (evidence["span_start"] is None) != (evidence["span_end"] is None):
                raise ValueError(f"{evidence_path} has an incomplete text span")
            if (
                evidence["span_start"] is not None
                and evidence["span_end"] <= evidence["span_start"]
            ):
                raise ValueError(f"{evidence_path} has an invalid text span")
            for key in ("rerank_score", "rrf_score"):
                _validate_number(
                    evidence[key], path=f"{evidence_path}.{key}", optional=True
                )
    _plain_json(root, path="verification")


def research_artifact_sha256(
    *,
    content: str,
    citation_ledger: list[dict[str, Any]],
    provenance: dict[str, Any],
    verification: dict[str, Any],
    metadata: dict[str, Any],
    artifact_schema_version: str = RESEARCH_ARTIFACT_VERSION,
) -> str:
    """Hash the exact v2 artifact; callers cannot silently filter or coerce it."""

    if type(content) is not str:
        raise TypeError("research artifact content must be a string")
    if type(citation_ledger) is not list or any(
        type(item) is not dict for item in citation_ledger
    ):
        raise TypeError("research artifact citation_ledger must be a list of objects")
    if type(provenance) is not dict:
        raise TypeError("research artifact provenance must be an object")
    if not is_trackable_research_provenance(provenance):
        raise ValueError("research artifact provenance must be trackable")
    if type(verification) is not dict:
        raise TypeError("research artifact verification must be an object")
    metadata = _require_exact_keys(
        metadata, _ARTIFACT_METADATA_KEYS, path="artifact.metadata"
    )
    if type(metadata["version"]) is not int or metadata["version"] < 1:
        raise TypeError("research artifact metadata.version must be a positive integer")
    if (
        type(metadata["generated_at"]) is not str
        or not metadata["generated_at"]
        or len(metadata["generated_at"]) > 128
    ):
        raise TypeError("research artifact metadata.generated_at must be a timestamp")
    if artifact_schema_version != RESEARCH_ARTIFACT_VERSION:
        raise ValueError("unsupported research artifact schema")
    validate_research_verification_snapshot(verification)
    payload = {
        "artifact_schema_version": artifact_schema_version,
        "content": content,
        "citation_ledger": citation_ledger,
        "provenance": provenance,
        "verification": verification,
        "metadata": metadata,
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _legacy_research_artifact_sha256(report: dict[str, Any]) -> str:
    content = report.get("content")
    ledger = report.get("citation_ledger")
    provenance = report.get("provenance")
    if type(content) is not str:
        raise TypeError("legacy research artifact content must be a string")
    if type(ledger) is not list or any(type(item) is not dict for item in ledger):
        raise TypeError("legacy citation ledger must be a list of objects")
    if type(provenance) is not dict:
        raise TypeError("legacy provenance must be an object")
    payload = {
        "content": content,
        "citation_ledger": ledger,
        "provenance": provenance,
    }
    return _canonical_hash([payload])


def _legacy_v2_artifact_sha256(report: dict[str, Any]) -> str:
    """Recompute the short-lived artifact-v2/verification-v1 envelope."""

    content = report.get("content")
    ledger = report.get("citation_ledger")
    provenance = report.get("provenance")
    verification = report.get("verification")
    version = report.get("version")
    generated_at = report.get("generated_at")
    if type(content) is not str:
        raise TypeError("legacy v2 content must be a string")
    if type(ledger) is not list or any(type(item) is not dict for item in ledger):
        raise TypeError("legacy v2 citation ledger must be a list of objects")
    if type(provenance) is not dict or not is_trackable_research_provenance(provenance):
        raise ValueError("legacy v2 provenance must be trackable")
    if (
        type(verification) is not dict
        or verification.get("schema_version") != "research-verification-v1"
    ):
        raise ValueError("legacy v2 verification schema is invalid")
    if type(version) is not int or version < 1:
        raise TypeError("legacy v2 version must be positive")
    if type(generated_at) is not str or not generated_at:
        raise TypeError("legacy v2 generated_at must be a timestamp")
    payload = {
        "artifact_schema_version": RESEARCH_ARTIFACT_VERSION,
        "content": content,
        "citation_ledger": ledger,
        "provenance": provenance,
        "verification": verification,
        "metadata": {"version": version, "generated_at": generated_at},
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def research_artifact_integrity_status(report: Any) -> str:
    """Return ``verified``, ``legacy-unverified``, or ``invalid``."""

    if type(report) is not dict or type(report.get("content")) is not str:
        return "invalid"
    schema_version = report.get("artifact_schema_version")
    if schema_version in {None, ""}:
        # Old reports without a verification commitment remain downloadable as
        # explicitly unverified Markdown, but never qualify for a bundle.
        if report.get("format", "markdown") != "markdown":
            return "invalid"
        if "verification" in report:
            return "invalid"
        legacy_sha = report.get("sha256")
        if legacy_sha in {None, ""}:
            # The original report schema had neither provenance nor a checksum.
            # Their presence after only the v2 marker/hash disappeared is an
            # attempted downgrade, not a legacy artifact.
            if "provenance" in report:
                return "invalid"
            return "legacy-unverified"
        if type(legacy_sha) is not str:
            return "invalid"
        try:
            expected = _legacy_research_artifact_sha256(report)
        except (TypeError, ValueError):
            return "invalid"
        return "legacy-unverified" if legacy_sha == expected else "invalid"
    if schema_version != RESEARCH_ARTIFACT_VERSION:
        return "invalid"
    if report.get("format") != "markdown":
        return "invalid"
    if type(report.get("sha256")) is not str:
        return "invalid"
    verification = report.get("verification")
    if type(verification) is not dict:
        return "invalid"
    verification_metrics = report.get("verification_metrics")
    if type(verification_metrics) is not dict:
        return "invalid"
    if verification_metrics != verification.get("aggregate"):
        return "invalid"
    content = report.get("content")
    citation_ledger = report.get("citation_ledger")
    provenance = report.get("provenance")
    if (
        type(content) is not str
        or type(citation_ledger) is not list
        or any(type(row) is not dict for row in citation_ledger)
        or type(provenance) is not dict
    ):
        return "invalid"
    if verification.get("schema_version") == "research-verification-v1":
        try:
            expected = _legacy_v2_artifact_sha256(report)
            from cogdoc.tools.public_citation_ledger import (
                validate_public_citation_ledger,
            )

            if not validate_public_citation_ledger(
                report["content"], report["citation_ledger"]
            ).is_valid:
                return "invalid"
        except (TypeError, ValueError):
            return "invalid"
        return "legacy-unverified" if report["sha256"] == expected else "invalid"
    try:
        expected = research_artifact_sha256(
            content=content,
            citation_ledger=citation_ledger,
            provenance=provenance,
            verification=verification,
            metadata={
                "version": report.get("version"),
                "generated_at": report.get("generated_at"),
            },
            artifact_schema_version=schema_version,
        )
        from cogdoc.tools.public_citation_ledger import (
            validate_public_citation_ledger,
        )

        if not validate_public_citation_ledger(
            report["content"], report["citation_ledger"]
        ).is_valid:
            return "invalid"
    except (TypeError, ValueError):
        return "invalid"
    return "verified" if report["sha256"] == expected else "invalid"


def research_artifact_matches_job_projection(
    job: Mapping[str, Any], report: Mapping[str, Any]
) -> bool:
    """Match a verified artifact to the exact mutable job state it represents."""

    if not isinstance(job, Mapping) or type(report) is not dict:
        return False
    if research_artifact_integrity_status(report) != "verified":
        return False
    sections = job.get("sections")
    evidence_provenance = job.get("evidence_provenance")
    if (
        type(sections) is not list
        or any(type(section) is not dict for section in sections)
        or type(evidence_provenance) is not dict
        or type(report.get("provenance")) is not dict
        or report.get("provenance") != evidence_provenance
    ):
        return False
    try:
        from cogdoc.service.research_artifact_composer import (
            compose_research_markdown,
        )

        content, ledger = compose_research_markdown(job, sections)
        verification_metrics = report.get("verification_metrics")
        if not isinstance(verification_metrics, Mapping):
            return False
        verification = build_research_verification_snapshot(
            job=job,
            verification_metrics=verification_metrics,
            sections=sections,
        )
    except (TypeError, ValueError):
        return False
    return (
        report.get("content") == content
        and type(report.get("citation_ledger")) is list
        and report.get("citation_ledger") == list(ledger)
        and type(report.get("verification")) is dict
        and report.get("verification") == verification
    )


def research_publication_sha256(
    *,
    artifact_sha256: str,
    report_version: int,
    published_at: str,
    published_by: str,
    review_history: list[dict[str, Any]],
    sections: list[dict[str, Any]],
) -> str:
    """Bind publication metadata and exact human-review trace to an artifact."""

    for field, value in (
        ("artifact_sha256", artifact_sha256),
        ("published_at", published_at),
        ("published_by", published_by),
    ):
        if type(value) is not str or not value:
            raise TypeError(f"research publication {field} must be a non-empty string")
    if type(report_version) is not int or report_version < 1:
        raise TypeError(
            "research publication report_version must be a positive integer"
        )
    if type(review_history) is not list or any(
        type(event) is not dict for event in review_history
    ):
        raise TypeError("research publication review_history must be a strict list")
    if type(sections) is not list or any(
        type(section) is not dict for section in sections
    ):
        raise TypeError("research publication sections must be a strict list")
    section_reviews = []
    for section in sections:
        section_id = section.get("section_id")
        generation_status = section.get("generation_status")
        review_status = section.get("review_status")
        review_note = section.get("review_note")
        reviewed_at = section.get("reviewed_at")
        if (
            type(section_id) is not str
            or not section_id
            or type(generation_status) is not str
            or type(review_status) is not str
            or type(review_note) is not str
            or type(reviewed_at) is not str
            or not reviewed_at
        ):
            raise TypeError("research publication section review is invalid")
        section_reviews.append(
            {
                "section_id": section_id,
                "generation_status": generation_status,
                "review_status": review_status,
                "review_note": review_note,
                "reviewed_at": reviewed_at,
            }
        )
    payload = {
        "schema_version": "research-publication-v1",
        "artifact_sha256": artifact_sha256,
        "report_version": report_version,
        "published_at": published_at,
        "published_by": published_by,
        "review_history": review_history,
        "section_reviews": section_reviews,
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()
