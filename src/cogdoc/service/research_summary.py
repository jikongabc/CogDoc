from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any


RESEARCH_SUMMARY_CURSOR_VERSION = "research-summary-cursor-v1"
RESEARCH_SUMMARY_REPRESENTATION_VERSION = "research-summary-page-v1"
RESEARCH_SUMMARY_STORAGE_VERSION = "research-summary-storage-v1"
RESEARCH_SUMMARY_PAGE_LIMIT = 100

_CURSOR_TOKEN_LIMIT = 1024
_CURSOR_JSON_LIMIT = 512
_ETAG_HEADER_LIMIT = 8192
_JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_CURSOR_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_JOB_STATUSES = {
    "planned",
    "running",
    "paused",
    "evidence_ready",
    "generating",
    "completed",
    "failed",
    "cancelled",
}
_SECTION_STATUSES = {"pending", "running", "completed", "failed"}
_REPORT_STATUSES = {
    "not_started",
    "generating",
    "ready",
    "ready_with_gaps",
    "published",
    "failed",
}
_REVIEW_STATUSES = {
    "not_started",
    "pending",
    "approved",
    "changes_requested",
    "published",
}
_PROVENANCE_STATUSES = {"untracked", "current", "stale"}
_PUBLIC_PROVENANCE_REASON_CODES = {
    "evidence_provenance_untracked",
    "current_index_provenance_unavailable",
    "kb_id_changed",
    "index_generation_changed",
    "index_build_version_changed",
    "chunk_identity_version_changed",
    "derived_knowledge_revision_changed",
    "retrieval_tuning_revision_changed",
    "research_contract_version_changed",
    "research_contract_revision_changed",
    "source_removed",
    "source_added",
    "source_sha256_changed",
}


class ResearchSummaryProjectionError(ValueError):
    """A durable research row cannot be represented as a public summary."""


class ResearchSummaryCursorError(ValueError):
    """A research-summary cursor is malformed, non-canonical, or unsupported."""


@dataclass(frozen=True, slots=True)
class ResearchSummaryCursor:
    updated_at: str
    job_id: str


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _normalized_text(value: Any, *, field: str, limit: int) -> str:
    if type(value) is not str:
        raise ResearchSummaryProjectionError(f"research summary {field} must be a string")
    normalized = " ".join(value.split())
    if len(normalized) <= limit:
        return normalized
    if limit <= 1:
        return normalized[:limit]
    return normalized[: limit - 1].rstrip() + "…"


def _required_text(value: Any, *, field: str, limit: int) -> str:
    normalized = _normalized_text(value, field=field, limit=limit)
    if not normalized:
        raise ResearchSummaryProjectionError(
            f"research summary {field} must not be blank"
        )
    return normalized


def _timestamp(value: Any, *, field: str, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if type(value) is not str or not value or len(value) > 128:
        raise ResearchSummaryProjectionError(
            f"research summary {field} must be a bounded timestamp"
        )
    return value


def _strict_nonnegative_int(value: Any, *, field: str) -> int:
    if type(value) is not int or value < 0:
        raise ResearchSummaryProjectionError(
            f"research summary {field} must be a non-negative integer"
        )
    return value


def _strict_bool(value: Any, *, field: str) -> bool:
    if type(value) is not bool:
        raise ResearchSummaryProjectionError(
            f"research summary {field} must be a boolean"
        )
    return value


def _enum(value: Any, allowed: set[str], *, field: str) -> str:
    if type(value) is not str or value not in allowed:
        raise ResearchSummaryProjectionError(
            f"research summary {field} has an unsupported value"
        )
    return value


def _section_counts(value: Any) -> dict[str, int]:
    if type(value) is not list:
        raise ResearchSummaryProjectionError(
            "research summary sections must be a strict list"
        )
    counts = {status: 0 for status in sorted(_SECTION_STATUSES)}
    for section in value:
        if type(section) is not dict:
            raise ResearchSummaryProjectionError(
                "research summary sections must contain plain objects"
            )
        status = _enum(
            section.get("status", "pending"),
            _SECTION_STATUSES,
            field="section.status",
        )
        counts[status] += 1
    return {
        "total": len(value),
        "pending": counts["pending"],
        "running": counts["running"],
        "completed": counts["completed"],
        "failed": counts["failed"],
    }


def _artifact_availability(value: Any) -> tuple[bool, int]:
    if not isinstance(value, Mapping):
        return False, 0
    content = value.get("content")
    if type(content) is not str or not content:
        return False, 0
    return True, len(content.encode("utf-8"))


def _provenance_projection(
    row: Mapping[str, Any], provenance: Mapping[str, Any] | None
) -> tuple[str, list[str]]:
    source: Mapping[str, Any] = provenance if provenance is not None else row
    status = (
        source.get("status", "untracked")
        if provenance is not None
        else source.get("provenance_status", "untracked")
    )
    status = _enum(status, _PROVENANCE_STATUSES, field="provenance_status")
    raw_reasons = source.get(
        "stale_reasons", source.get("provenance_stale_reasons", [])
    )
    if type(raw_reasons) is not list:
        raise ResearchSummaryProjectionError(
            "research summary provenance_stale_reasons must be a strict list"
        )
    reasons: list[str] = []
    for raw in raw_reasons[:16]:
        reason = _normalized_text(
            raw,
            field="provenance_stale_reasons",
            limit=256,
        ).partition(":")[0]
        reason = reason if reason in _PUBLIC_PROVENANCE_REASON_CODES else "unknown"
        if reason and reason not in reasons:
            reasons.append(reason)
    return status, reasons


def _project_compact_research_summary(
    row: Mapping[str, Any], provenance: Mapping[str, Any] | None
) -> dict[str, Any]:
    if row.get("storage_version") != RESEARCH_SUMMARY_STORAGE_VERSION:
        raise ResearchSummaryProjectionError(
            "research summary storage version is unsupported"
        )
    provenance_status, stale_reasons = _provenance_projection(row, provenance)
    section_counts = row.get("section_counts")
    if type(section_counts) is not dict:
        raise ResearchSummaryProjectionError(
            "research summary section_counts must be a strict object"
        )
    normalized_counts = {
        name: _strict_nonnegative_int(
            section_counts.get(name), field=f"section_counts.{name}"
        )
        for name in ("total", "pending", "running", "completed", "failed")
    }
    if sum(normalized_counts[name] for name in _SECTION_STATUSES) != normalized_counts[
        "total"
    ]:
        raise ResearchSummaryProjectionError(
            "research summary section counts do not cover the plan"
        )
    has_report = row.get("has_report")
    has_published_report = row.get("has_published_report")
    if type(has_report) is not bool or type(has_published_report) is not bool:
        raise ResearchSummaryProjectionError(
            "research summary artifact hints must be booleans"
        )
    report_size_bytes = _strict_nonnegative_int(
        row.get("report_size_bytes"), field="report_size_bytes"
    )
    if not has_report and report_size_bytes:
        raise ResearchSummaryProjectionError(
            "research summary report size requires an available report"
        )
    published_at = _timestamp(
        row.get("published_at"), field="published_at", optional=True
    )
    if has_published_report and published_at is None:
        raise ResearchSummaryProjectionError(
            "research summary published artifact requires published_at"
        )
    return {
        "job_id": _required_text(row.get("job_id"), field="job_id", limit=128),
        "kb_id": _required_text(row.get("kb_id"), field="kb_id", limit=128),
        "title": _required_text(row.get("title"), field="title", limit=160),
        "objective_preview": _required_text(
            row.get("objective_preview"), field="objective_preview", limit=240
        ),
        "is_local": _strict_bool(row.get("is_local"), field="is_local"),
        "status": _enum(row.get("status"), _JOB_STATUSES, field="status"),
        "revision": _strict_positive_revision(row.get("revision")),
        "created_at": _timestamp(row.get("created_at"), field="created_at"),
        "updated_at": _timestamp(row.get("updated_at"), field="updated_at"),
        "section_counts": normalized_counts,
        "report_status": _enum(
            row.get("report_status"), _REPORT_STATUSES, field="report_status"
        ),
        "report_version": _strict_nonnegative_int(
            row.get("report_version"), field="report_version"
        ),
        "review_status": _enum(
            row.get("review_status"), _REVIEW_STATUSES, field="review_status"
        ),
        "provenance_status": provenance_status,
        "provenance_stale_reasons": stale_reasons,
        "report_history_count": _strict_nonnegative_int(
            row.get("report_history_count"), field="report_history_count"
        ),
        "has_report": has_report,
        "has_published_report": has_published_report,
        "report_size_bytes": report_size_bytes,
        "published_at": published_at,
        "error": _normalized_text(row.get("error"), field="error", limit=256),
    }


def _strict_positive_revision(value: Any) -> int:
    if type(value) is not int or value < 1:
        raise ResearchSummaryProjectionError(
            "research summary revision must be a positive integer"
        )
    return value


def project_research_job_summary(
    row: Mapping[str, Any],
    provenance: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Project one durable job without copying or traversing heavy artifacts."""

    if not isinstance(row, Mapping):
        raise ResearchSummaryProjectionError("research summary row must be a mapping")
    if "storage_version" in row:
        return _project_compact_research_summary(row, provenance)
    job_id = _required_text(row.get("job_id"), field="job_id", limit=128)
    kb_id = _required_text(row.get("kb_id"), field="kb_id", limit=128)
    objective = _required_text(row.get("objective"), field="objective", limit=4000)
    raw_title = row.get("title") or objective[:160]
    title = _required_text(raw_title, field="title", limit=160)
    is_local = row.get("is_local", False)
    if type(is_local) is not bool:
        raise ResearchSummaryProjectionError(
            "research summary is_local must be a boolean"
        )
    revision = _strict_positive_revision(row.get("revision"))
    history = row.get("report_history", [])
    if type(history) is not list:
        raise ResearchSummaryProjectionError(
            "research summary report_history must be a strict list"
        )
    provenance_status, stale_reasons = _provenance_projection(row, provenance)
    has_report, report_size_bytes = _artifact_availability(row.get("report"))
    has_published_report, _ = _artifact_availability(row.get("published_report"))
    published_at = _timestamp(
        row.get("published_at"), field="published_at", optional=True
    )
    return {
        "job_id": job_id,
        "kb_id": kb_id,
        "title": title,
        "objective_preview": _normalized_text(
            objective,
            field="objective_preview",
            limit=240,
        ),
        "is_local": is_local,
        "status": _enum(row.get("status"), _JOB_STATUSES, field="status"),
        "revision": revision,
        "created_at": _timestamp(row.get("created_at"), field="created_at"),
        "updated_at": _timestamp(row.get("updated_at"), field="updated_at"),
        "section_counts": _section_counts(row.get("sections")),
        "report_status": _enum(
            row.get("report_status", "not_started"),
            _REPORT_STATUSES,
            field="report_status",
        ),
        "report_version": _strict_nonnegative_int(
            row.get("report_version", 0), field="report_version"
        ),
        "review_status": _enum(
            row.get("review_status", "not_started"),
            _REVIEW_STATUSES,
            field="review_status",
        ),
        "provenance_status": provenance_status,
        "provenance_stale_reasons": stale_reasons,
        "report_history_count": len(history),
        "has_report": has_report,
        "has_published_report": has_published_report,
        "report_size_bytes": report_size_bytes,
        "published_at": published_at,
        "error": _normalized_text(row.get("error", ""), field="error", limit=256),
    }


def compact_research_job_summary(row: Mapping[str, Any]) -> dict[str, Any]:
    """Build the SQLite collection index without copying artifact bodies."""

    summary = project_research_job_summary(
        row,
        {"status": "untracked", "stale_reasons": []},
    )
    return {
        "storage_version": RESEARCH_SUMMARY_STORAGE_VERSION,
        **summary,
        # Needed only to compare the captured identity with the current KB.
        # The public projector never includes this value in its response.
        "evidence_provenance": row.get("evidence_provenance")
        if isinstance(row.get("evidence_provenance"), Mapping)
        else {},
    }


def project_research_job_summaries(
    rows: Sequence[Mapping[str, Any]],
    provenances: Sequence[Mapping[str, Any] | None] | None = None,
) -> list[dict[str, Any]]:
    if isinstance(rows, (str, bytes, bytearray)) or not isinstance(rows, Sequence):
        raise ResearchSummaryProjectionError("research summary rows must be a sequence")
    if provenances is None:
        provenance_rows: Sequence[Mapping[str, Any] | None] = [None] * len(rows)
    else:
        if len(provenances) != len(rows):
            raise ResearchSummaryProjectionError(
                "research summary provenance cardinality must match rows"
            )
        provenance_rows = provenances
    return [
        project_research_job_summary(row, provenance)
        for row, provenance in zip(rows, provenance_rows)
    ]


def _validate_cursor_timestamp(value: Any) -> str:
    if type(value) is not str or not value or len(value) > 128:
        raise ResearchSummaryCursorError("research summary cursor timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ResearchSummaryCursorError(
            "research summary cursor timestamp is invalid"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ResearchSummaryCursorError(
            "research summary cursor timestamp must include a timezone"
        )
    return value


def _timestamp_sort_key(value: Any) -> datetime:
    validated = _validate_cursor_timestamp(value)
    return datetime.fromisoformat(validated.replace("Z", "+00:00"))


def _validate_cursor_job_id(value: Any) -> str:
    if type(value) is not str or not _JOB_ID_RE.fullmatch(value):
        raise ResearchSummaryCursorError("research summary cursor job_id is invalid")
    return value


def encode_research_summary_cursor(*, updated_at: str, job_id: str) -> str:
    payload = {
        "job_id": _validate_cursor_job_id(job_id),
        "updated_at": _validate_cursor_timestamp(updated_at),
        "version": RESEARCH_SUMMARY_CURSOR_VERSION,
    }
    raw = _canonical_json(payload).encode("utf-8")
    if len(raw) > _CURSOR_JSON_LIMIT:
        raise ResearchSummaryCursorError("research summary cursor payload is too large")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ResearchSummaryCursorError(
                "research summary cursor contains duplicate fields"
            )
        result[key] = value
    return result


def decode_research_summary_cursor(value: str) -> ResearchSummaryCursor:
    if (
        type(value) is not str
        or not value
        or len(value) > _CURSOR_TOKEN_LIMIT
        or not _CURSOR_TOKEN_RE.fullmatch(value)
    ):
        raise ResearchSummaryCursorError("research summary cursor token is invalid")
    padding = "=" * (-len(value) % 4)
    try:
        raw = base64.b64decode(
            value + padding,
            altchars=b"-_",
            validate=True,
        )
    except (binascii.Error, ValueError) as exc:
        raise ResearchSummaryCursorError(
            "research summary cursor is not valid base64url"
        ) from exc
    if not raw or len(raw) > _CURSOR_JSON_LIMIT:
        raise ResearchSummaryCursorError("research summary cursor payload is invalid")
    try:
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                ResearchSummaryCursorError(
                    "research summary cursor contains a non-finite value"
                )
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ResearchSummaryCursorError(
            "research summary cursor is not valid JSON"
        ) from exc
    if type(payload) is not dict or set(payload) != {"job_id", "updated_at", "version"}:
        raise ResearchSummaryCursorError("research summary cursor shape is invalid")
    if payload["version"] != RESEARCH_SUMMARY_CURSOR_VERSION:
        raise ResearchSummaryCursorError("research summary cursor version is unsupported")
    cursor = ResearchSummaryCursor(
        updated_at=_validate_cursor_timestamp(payload["updated_at"]),
        job_id=_validate_cursor_job_id(payload["job_id"]),
    )
    canonical = encode_research_summary_cursor(
        updated_at=cursor.updated_at,
        job_id=cursor.job_id,
    )
    if canonical != value:
        raise ResearchSummaryCursorError("research summary cursor is not canonical")
    return cursor


def paginate_research_job_summaries(
    summaries: Sequence[Mapping[str, Any]],
    *,
    limit: int,
    cursor: str | None = None,
) -> dict[str, Any]:
    if type(limit) is not int or not 1 <= limit <= RESEARCH_SUMMARY_PAGE_LIMIT:
        raise ValueError(
            f"research summary limit must be between 1 and {RESEARCH_SUMMARY_PAGE_LIMIT}"
        )
    if isinstance(summaries, (str, bytes, bytearray)) or not isinstance(
        summaries, Sequence
    ):
        raise TypeError("research summaries must be a sequence")
    normalized: list[dict[str, Any]] = []
    seen: set[tuple[datetime, str]] = set()
    for raw in summaries:
        if not isinstance(raw, Mapping):
            raise TypeError("research summaries must contain mappings")
        job_id = _validate_cursor_job_id(raw.get("job_id"))
        updated_at = _validate_cursor_timestamp(raw.get("updated_at"))
        identity = (_timestamp_sort_key(updated_at), job_id)
        if identity in seen:
            raise ValueError("research summaries contain a duplicate sort key")
        seen.add(identity)
        normalized.append(dict(raw))
    normalized.sort(
        key=lambda item: (
            _timestamp_sort_key(item["updated_at"]),
            str(item["job_id"]),
        ),
        reverse=True,
    )
    if cursor is not None:
        decoded = decode_research_summary_cursor(cursor)
        cursor_key = (_timestamp_sort_key(decoded.updated_at), decoded.job_id)
        normalized = [
            item
            for item in normalized
            if (
                _timestamp_sort_key(item["updated_at"]),
                str(item["job_id"]),
            )
            < cursor_key
        ]
    has_more = len(normalized) > limit
    jobs = normalized[:limit]
    next_cursor = None
    if has_more and jobs:
        next_cursor = encode_research_summary_cursor(
            updated_at=str(jobs[-1]["updated_at"]),
            job_id=str(jobs[-1]["job_id"]),
        )
    return {"jobs": jobs, "next_cursor": next_cursor, "has_more": has_more}


def research_summary_page_etag(page: Mapping[str, Any]) -> str:
    if not isinstance(page, Mapping):
        raise TypeError("research summary page must be a mapping")
    payload = {
        "representation": RESEARCH_SUMMARY_REPRESENTATION_VERSION,
        "page": dict(page),
    }
    digest = hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()
    return f'"rs-{digest}"'


def if_none_match_matches(value: str | None, current_etag: str) -> bool:
    """Apply weak comparison for a safe GET without accepting malformed tokens."""

    if value is None or type(value) is not str or len(value) > _ETAG_HEADER_LIMIT:
        return False
    if type(current_etag) is not str or not re.fullmatch(
        r'"[\x21\x23-\x7e]+"', current_etag
    ):
        raise ValueError("current ETag is invalid")
    for raw in value.split(","):
        candidate = raw.strip()
        if candidate == "*":
            return True
        if candidate.startswith("W/"):
            candidate = candidate[2:].strip()
        if not re.fullmatch(r'"[\x21\x23-\x7e]+"', candidate):
            continue
        if candidate == current_etag:
            return True
    return False
