from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from typing import Any

from cogdoc.service.claim_verification_policy import (
    claim_verification_policy_projection,
)
from cogdoc.tools.eval.claim_verification_eval import CLAIM_VERDICTS


_TASK_TYPES = frozenset({"qa", "summary", "compare"})
_MODES = frozenset({"shadow", "enforce"})


def _bounded_percent(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    if number != number or number in {float("inf"), float("-inf")}:
        return 0.0
    return min(100.0, max(0.0, number))


def _sample_rank(seed: str, identity: str) -> int:
    digest = hashlib.sha256(f"{seed}\0{identity}".encode()).digest()
    return int.from_bytes(digest[:8], "big")


def _sampled(seed: str, identity: str, percent: float) -> bool:
    threshold = round(_bounded_percent(percent) * 100)
    return _sample_rank(seed, identity) % 10_000 < threshold


def _safe_ids(value: Any, *, maximum: int = 12) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(
        value, (str, bytes, bytearray)
    ):
        return []
    result: list[str] = []
    for item in value:
        normalized = str(item or "").strip()
        if normalized and len(normalized) <= 256 and normalized not in result:
            result.append(normalized)
        if len(result) >= maximum:
            break
    return result


def _doc_snapshot(doc: Mapping[str, Any], *, max_chars: int) -> dict[str, Any] | None:
    meta_value = doc.get("meta")
    meta = meta_value if isinstance(meta_value, Mapping) else {}
    chunk_id = str(meta.get("chunk_id") or doc.get("chunk_id") or "").strip()
    if not chunk_id or len(chunk_id) > 256:
        return None
    text = str(doc.get("text") or doc.get("text_preview") or "")
    bounded_text = text[:max_chars]
    page = meta.get("page", doc.get("page"))
    return {
        "chunk_id": chunk_id,
        "source": str(meta.get("source") or doc.get("source") or "")[:512],
        "authorization_source": str(
            meta.get("related_source")
            if meta.get("source_type") == "derived_knowledge"
            else meta.get("source") or doc.get("source") or ""
        )[:512],
        "page": page if isinstance(page, int) else None,
        "page_start": (
            meta.get("page_start", doc.get("page_start", page))
            if isinstance(meta.get("page_start", doc.get("page_start", page)), int)
            else None
        ),
        "page_end": (
            meta.get("page_end", doc.get("page_end", page))
            if isinstance(meta.get("page_end", doc.get("page_end", page)), int)
            else None
        ),
        "text": bounded_text,
        "text_truncated": len(text) > len(bounded_text),
    }


def _evidence_by_chunk(
    raw_output: Mapping[str, Any],
    public_evidence: Any,
    *,
    max_chars: int,
) -> dict[str, dict[str, Any]]:
    candidates: list[Any] = []
    reranked = raw_output.get("reranked_docs")
    if isinstance(reranked, list):
        candidates.extend(reranked)
    if isinstance(public_evidence, list):
        candidates.extend(public_evidence)
    result: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            continue
        snapshot = _doc_snapshot(candidate, max_chars=max_chars)
        if snapshot is None:
            continue
        chunk_id = str(snapshot["chunk_id"])
        existing = result.get(chunk_id)
        if existing is None or len(str(existing.get("text") or "")) < len(
            str(snapshot.get("text") or "")
        ):
            result[chunk_id] = snapshot
    return result


def _duration_ms(audit: Mapping[str, Any]) -> float | None:
    verifier = audit.get("verifier")
    if not isinstance(verifier, Mapping):
        return None
    raw_value = verifier.get("duration_ms")
    if raw_value is None:
        return None
    try:
        value = float(raw_value)
    except (TypeError, ValueError, OverflowError):
        return None
    return round(value, 3) if value >= 0 and value == value else None


def build_claim_review_candidates(
    result: Any,
    *,
    tenant_id: str,
    kb_id: str,
    sample_percent: float,
    sample_seed: str,
    max_claims: int,
    max_evidence_per_claim: int,
    max_chars_per_evidence: int,
) -> list[dict[str, Any]]:
    """Build opt-in, claim-level review rows without persisting query or answer."""

    tenant = str(tenant_id or "").strip()
    storage_id = str(kb_id or "").strip()
    task_type = str(getattr(result, "task_type", "") or "")
    trace_id = str(getattr(result, "trace_id", "") or "").strip()
    raw_output = getattr(result, "raw_output", None)
    if (
        not tenant
        or not storage_id
        or len(storage_id) > 512
        or task_type not in _TASK_TYPES
        or not trace_id
        or not isinstance(raw_output, Mapping)
        or _bounded_percent(sample_percent) <= 0
    ):
        return []
    rollout = raw_output.get("claim_verification_rollout")
    audit = raw_output.get("claim_audit")
    if not isinstance(rollout, Mapping) or not isinstance(audit, Mapping):
        return []
    mode = str(rollout.get("mode") or "")
    if mode not in _MODES or not bool(rollout.get("executed")):
        return []
    policy = claim_verification_policy_projection(rollout, effective_mode=mode)
    policy_id = str(policy.get("policy_id") or "")
    claims = audit.get("claims")
    if not policy_id or not isinstance(claims, list):
        return []

    evidence = _evidence_by_chunk(
        raw_output,
        getattr(result, "evidence", None),
        max_chars=max(1, int(max_chars_per_evidence)),
    )
    ranked: list[tuple[int, dict[str, Any]]] = []
    for claim in claims:
        if not isinstance(claim, Mapping):
            continue
        claim_id = str(claim.get("claim_id") or "").strip()
        claim_text = str(claim.get("text") or "").strip()
        verdict = str(claim.get("verdict") or "")
        if (
            not claim_id
            or len(claim_id) > 64
            or not claim_text
            or len(claim_text) > 8_000
            or verdict not in CLAIM_VERDICTS
        ):
            continue
        identity = f"{policy_id}\0{trace_id}\0{claim_id}"
        if not _sampled(str(sample_seed or "cogdoc-review-v1"), identity, sample_percent):
            continue
        cited_ids = _safe_ids(claim.get("cited_chunk_ids"))
        supporting_ids = _safe_ids(claim.get("supporting_chunk_ids"))
        selected_evidence = [
            evidence[chunk_id]
            for chunk_id in cited_ids[: max(1, int(max_evidence_per_claim))]
            if chunk_id in evidence
        ]
        review_id = hashlib.sha256(
            f"{tenant}\0{storage_id}\0{identity}".encode("utf-8")
        ).hexdigest()[:32]
        ranked.append(
            (
                _sample_rank(str(sample_seed or "cogdoc-review-v1"), identity),
                {
                    "review_id": review_id,
                    "kb_id": storage_id,
                    "task_type": task_type,
                    "policy_id": policy_id,
                    "effective_mode": mode,
                    "decision": str(rollout.get("decision") or "")[:32],
                    "claim_id": claim_id,
                    "claim": claim_text,
                    "actual_verdict": verdict,
                    "reason": str(claim.get("reason") or "")[:1_000],
                    "confidence": claim.get("confidence"),
                    "duration_ms": _duration_ms(audit),
                    "cited_chunk_ids": cited_ids,
                    "supporting_chunk_ids": supporting_ids,
                    "evidence": selected_evidence,
                    "evidence_complete": set(cited_ids).issubset(evidence),
                },
            )
        )
    ranked.sort(key=lambda item: (item[0], item[1]["review_id"]))
    return [item[1] for item in ranked[: max(1, int(max_claims))]]
