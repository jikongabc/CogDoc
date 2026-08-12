from __future__ import annotations

import json
import os
import unicodedata
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from threading import RLock
from typing import Any
from uuid import uuid4

from cogdoc.api.persistence import connect_sqlite
from cogdoc.api.time_utils import now_iso
from cogdoc.config.settings import get_settings
from cogdoc.research_control import (
    RESEARCH_RESOURCE_NAMES,
    ResearchBudgetExceeded,
    ResearchCancelled,
    ResearchDeadlineExceeded,
    ResearchPaused,
    normalize_resource_costs,
)
from cogdoc.service.research_artifact_composer import compose_research_markdown
from cogdoc.service.research_provenance import (
    RESEARCH_ARTIFACT_VERSION,
    build_research_verification_snapshot,
    freeze_research_execution_nodes,
    research_artifact_integrity_status,
    research_artifact_matches_job_projection,
    research_artifact_sha256,
    research_publication_sha256,
)
from cogdoc.service.research_summary import (
    RESEARCH_SUMMARY_STORAGE_VERSION,
    compact_research_job_summary,
)


class ResearchJobRevisionConflictError(ValueError):
    """The caller edited an obsolete research-job revision."""


class ResearchJobStateConflictError(ValueError):
    """The requested execution transition is invalid for the current state."""


def build_research_plan(
    objective: str,
    section_titles: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    """Build a safe, editable initial plan without inventing domain facts."""

    titles = [" ".join(str(title).split()) for title in (section_titles or [])]
    titles = [title for title in titles if title]
    if not titles:
        titles = [
            "目标与范围",
            "关键事实与证据",
            "综合分析",
            "风险与局限",
            "结论与建议",
        ]
    normalized_objective = " ".join(objective.split())
    sections = []
    for position, title in enumerate(titles, start=1):
        question = (
            f"围绕“{normalized_objective}”，需要查明哪些与“{title}”"
            "直接相关且可由知识库验证的信息？"
        )
        sections.append(
            _research_section_record(
                position=position,
                title=title,
                research_question=question,
                evidence_requirements=(),
                success_criteria="仅纳入可由知识库直接证据支持的结论，并明确证据缺口。",
            )
        )
    return sections


def _clone(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False))


def _research_contract_key(value: str) -> str:
    return unicodedata.normalize("NFKC", " ".join(value.split())).casefold()


def _default_recovery_query(question: str, retrieval_query: str) -> str:
    candidate = f"{question} 相关证据 适用范围"
    if _research_contract_key(candidate) == _research_contract_key(retrieval_query):
        candidate = f"{question} 补充检索 边界条件"
    return candidate


def _normalize_requirement(
    raw: Mapping[str, Any],
    *,
    section_id: str,
    position: int,
    fallback_question: str,
) -> dict[str, str]:
    question = " ".join(str(raw.get("question") or fallback_question).split())
    retrieval_query = " ".join(str(raw.get("retrieval_query") or question).split())
    recovery_query = " ".join(str(raw.get("recovery_query") or "").split())
    if not recovery_query:
        recovery_query = _default_recovery_query(question, retrieval_query)
    if not question or not retrieval_query or not recovery_query:
        raise ValueError("research evidence requirements cannot be blank")
    if _research_contract_key(retrieval_query) == _research_contract_key(
        recovery_query
    ):
        raise ValueError("retrieval_query and recovery_query must be distinct")
    return {
        "requirement_id": f"{section_id}:r{position}",
        "question": question,
        "retrieval_query": retrieval_query,
        "recovery_query": recovery_query,
    }


def _research_section_record(
    *,
    position: int,
    title: str,
    research_question: str,
    evidence_requirements: Sequence[Mapping[str, Any]],
    success_criteria: str,
) -> dict[str, Any]:
    section_id = f"s{position}"
    raw_requirements = list(evidence_requirements) or [
        {
            "question": research_question,
            "retrieval_query": research_question,
            "recovery_query": _default_recovery_query(
                research_question, research_question
            ),
        }
    ]
    if len(raw_requirements) > 3:
        raise ValueError(
            "research sections support at most three evidence requirements"
        )
    requirements = [
        _normalize_requirement(
            raw,
            section_id=section_id,
            position=requirement_position,
            fallback_question=research_question,
        )
        for requirement_position, raw in enumerate(raw_requirements, start=1)
    ]
    requirement_keys = [
        _research_contract_key(requirement["question"]) for requirement in requirements
    ]
    if len(requirement_keys) != len(set(requirement_keys)):
        raise ValueError(
            "research evidence requirement questions must be unique per section"
        )
    return {
        "section_id": section_id,
        "position": position,
        "title": title,
        "research_question": research_question,
        "evidence_requirements": requirements,
        "evidence_requirement_ids": [
            requirement["requirement_id"] for requirement in requirements
        ],
        "evidence_requirement_results": [],
        "success_criteria": success_criteria,
        "status": "pending",
        "evidence_status": "unsearched",
        "evidence": [],
        "execution_metrics": {},
        "citation_ledger": [],
        "claim_audit": {},
        "coverage_audit": {},
        "revision_instruction": "",
        "review_status": "not_started",
        "review_note": "",
        "reviewed_at": None,
        "error": "",
    }


def _normalize_sections(sections: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    seen_titles: set[str] = set()
    for position, section in enumerate(sections, start=1):
        title = " ".join(str(section.get("title") or "").split())
        question = " ".join(str(section.get("research_question") or "").split())
        if not title or not question:
            raise ValueError(
                "research sections require non-blank title and research_question"
            )
        title_key = _research_contract_key(title)
        if title_key in seen_titles:
            raise ValueError(f"duplicate research section title: {title}")
        seen_titles.add(title_key)
        raw_requirements = section.get("evidence_requirements") or []
        if isinstance(raw_requirements, (str, bytes, bytearray)) or not isinstance(
            raw_requirements, Sequence
        ):
            raise ValueError("evidence_requirements must be a sequence")
        if not all(isinstance(item, Mapping) for item in raw_requirements):
            raise ValueError("evidence_requirements must contain objects")
        success_criteria = " ".join(
            str(
                section.get("success_criteria")
                or "仅纳入可由知识库直接证据支持的结论，并明确证据缺口。"
            ).split()
        )
        normalized.append(
            _research_section_record(
                position=position,
                title=title,
                research_question=question,
                evidence_requirements=raw_requirements,
                success_criteria=success_criteria,
            )
        )
    if not normalized:
        raise ValueError("research plan requires at least one section")
    return normalized


def _touch(row: dict[str, Any]) -> dict[str, Any]:
    row["revision"] = int(row.get("revision") or 0) + 1
    row["updated_at"] = now_iso()
    return row


_RESEARCH_CONTROL_METRICS_KEY = "_research_control"
_RESEARCH_CONTROL_PHASES = {"evidence", "report"}


def _utc_now(value: str | datetime | None = None) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif value:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    else:
        parsed = datetime.now(timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _deadline_after(seconds: float, *, now: str | datetime | None = None) -> str:
    return (_utc_now(now) + timedelta(seconds=float(seconds))).isoformat()


def _research_resource_limits(
    overrides: Mapping[str, Any] | None = None,
) -> dict[str, int]:
    settings = get_settings()
    limits = {
        "retrieval_queries": settings.cogdoc_research_max_retrieval_queries,
        "candidate_docs": settings.cogdoc_research_max_candidate_docs,
        "llm_calls": settings.cogdoc_research_max_llm_calls,
        "model_input_chars": settings.cogdoc_research_max_model_input_chars,
    }
    for name, value in dict(overrides or {}).items():
        if name not in limits:
            raise ValueError(f"unknown research resource limit: {name}")
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(
                f"research resource limit {name} must be a positive integer"
            )
        limits[name] = value
    return limits


def _research_control_root(
    row: dict[str, Any], *, create: bool = False
) -> dict[str, Any]:
    sections = row.get("sections")
    if (
        not isinstance(sections, list)
        or not sections
        or not isinstance(sections[0], dict)
    ):
        return {}
    metrics = sections[0].get("execution_metrics")
    if not isinstance(metrics, dict):
        if not create:
            return {}
        metrics = {}
        sections[0]["execution_metrics"] = metrics
    root = metrics.get(_RESEARCH_CONTROL_METRICS_KEY)
    if not isinstance(root, dict):
        if not create:
            return {}
        root = {}
        metrics[_RESEARCH_CONTROL_METRICS_KEY] = root
    return root


def research_run_control(row: Mapping[str, Any], phase: str) -> dict[str, Any]:
    """Return a cloned internal run-control snapshot without changing the row."""

    if phase not in _RESEARCH_CONTROL_PHASES:
        raise ValueError(f"unknown research phase: {phase}")
    cloned = dict(_clone(row))
    root = _research_control_root(cloned)
    value = root.get(phase)
    return dict(_clone(value)) if isinstance(value, Mapping) else {}


def _new_research_control(
    *,
    phase: str,
    attempt_id: str,
    deadline_at: str | None,
    limits: Mapping[str, Any] | None,
    now: str | datetime | None = None,
) -> dict[str, Any]:
    if phase not in _RESEARCH_CONTROL_PHASES:
        raise ValueError(f"unknown research phase: {phase}")
    settings = get_settings()
    default_seconds = (
        settings.cogdoc_research_evidence_deadline_seconds
        if phase == "evidence"
        else settings.cogdoc_research_report_deadline_seconds
    )
    timestamp = _utc_now(now).isoformat()
    return {
        "phase": phase,
        "attempt_id": attempt_id,
        "lease_id": uuid4().hex,
        "draining_lease_id": "",
        "control_state": "running",
        "deadline_at": deadline_at or _deadline_after(default_seconds, now=now),
        "limits": _research_resource_limits(limits),
        "used": {name: 0 for name in RESEARCH_RESOURCE_NAMES},
        "started_at": timestamp,
        "heartbeat_at": timestamp,
        "finished_at": None,
        "terminal_reason": "",
    }


def _control_deadline_expired(
    control: Mapping[str, Any], *, now: str | datetime | None = None
) -> bool:
    deadline_at = control.get("deadline_at")
    if not isinstance(deadline_at, str) or not deadline_at:
        return False
    try:
        return _utc_now(now) >= _utc_now(deadline_at)
    except (TypeError, ValueError):
        return True


def _finish_control(
    control: dict[str, Any], *, state: str, reason: str, now=None
) -> None:
    control.update(
        {
            "control_state": state,
            "lease_id": "",
            "draining_lease_id": "",
            "heartbeat_at": _utc_now(now).isoformat(),
            "finished_at": _utc_now(now).isoformat(),
            "terminal_reason": reason,
        }
    )


def _mark_run_failed(
    row: dict[str, Any], *, phase: str, error_class: str, now=None
) -> dict[str, Any]:
    control = _research_control_root(row).get(phase)
    if isinstance(control, dict):
        state = (
            "expired"
            if error_class == "ResearchDeadlineExceeded"
            else "budget_exhausted"
            if error_class == "ResearchBudgetExceeded"
            else "failed"
        )
        _finish_control(control, state=state, reason=error_class, now=now)
    if phase == "report":
        row["status"] = "failed"
        row["report_status"] = "failed"
    else:
        for section in row.get("sections") or []:
            if section.get("status") == "running":
                section["status"] = "failed"
                section["error"] = error_class
        row["status"] = "failed"
    row["error"] = error_class
    return _touch(row)


def _research_claim_audit_passed(value: Any) -> bool:
    if not isinstance(value, Mapping) or str(value.get("status") or "") not in {
        "passed",
        "repaired",
    }:
        return False
    counts = value.get("counts")
    if not isinstance(counts, Mapping):
        return False
    required = (
        "claim_count",
        "supported",
        "unsupported",
        "insufficient",
        "cited",
    )
    if any(type(counts.get(key)) is not int for key in required):
        return False
    claim_count = counts["claim_count"]
    return (
        claim_count >= 1
        and counts["supported"] == claim_count
        and counts["cited"] == claim_count
        and counts["unsupported"] == 0
        and counts["insufficient"] == 0
    )


def _research_coverage_audit_passed(value: Any) -> bool:
    if not isinstance(value, Mapping) or str(value.get("status") or "") not in {
        "passed",
        "repaired",
    }:
        return False
    requirement_count = value.get("requirement_count")
    covered_count = value.get("covered_count")
    missing = value.get("missing_requirement_ids")
    return (
        type(requirement_count) is int
        and requirement_count >= 1
        and type(covered_count) is int
        and covered_count == requirement_count
        and type(missing) is list
        and not missing
    )


def _research_expected_requirement_ids(
    section: Mapping[str, Any],
) -> tuple[str, ...] | None:
    """Return the canonical atomic obligations, rejecting legacy ambiguity."""

    requirements = section.get("evidence_requirements")
    declared_ids = section.get("evidence_requirement_ids")
    if (
        type(requirements) is not list
        or not requirements
        or type(declared_ids) is not list
        or len(requirements) != len(declared_ids)
    ):
        return None
    requirement_ids: list[str] = []
    for requirement in requirements:
        if type(requirement) is not dict:
            return None
        requirement_id = requirement.get("requirement_id")
        if type(requirement_id) is not str or not requirement_id:
            return None
        requirement_ids.append(requirement_id)
    if (
        any(type(requirement_id) is not str for requirement_id in declared_ids)
        or list(declared_ids) != requirement_ids
        or len(set(requirement_ids)) != len(requirement_ids)
    ):
        return None
    return tuple(requirement_ids)


def _research_generated_section_passed(section: Mapping[str, Any]) -> bool:
    """Validate every machine gate against the section's immutable plan."""

    generation_status = str(
        section.get("generation_status")
        if "generation_status" in section
        else section.get("status") or ""
    )
    if generation_status != "generated":
        return False
    if str(section.get("verification_status") or "") != "supported":
        return False
    if not _research_claim_audit_passed(section.get("claim_audit")):
        return False
    expected_ids = _research_expected_requirement_ids(section)
    results = section.get("evidence_requirement_results")
    if expected_ids is None or type(results) is not list:
        return False
    result_ids: list[str] = []
    for result in results:
        if type(result) is not dict:
            return False
        requirement_id = result.get("requirement_id")
        status = result.get("status")
        evidence_count = result.get("evidence_count")
        if (
            type(requirement_id) is not str
            or type(status) is not str
            or status != "supported"
            or type(evidence_count) is not int
            or evidence_count < 1
        ):
            return False
        result_ids.append(requirement_id)
    if tuple(result_ids) != expected_ids:
        return False
    coverage = section.get("coverage_audit")
    if not _research_coverage_audit_passed(coverage):
        return False
    assert isinstance(coverage, Mapping)
    if coverage.get("requirement_count") != len(expected_ids) or coverage.get(
        "covered_count"
    ) != len(expected_ids):
        return False
    evidence = section.get("evidence")
    return (
        type(evidence) is list
        and bool(evidence)
        and all(type(item) is dict for item in evidence)
    )


def research_current_review_invariant(row: Mapping[str, Any]) -> bool:
    """Bind every current section review to its latest persisted history event."""

    sections = row.get("sections")
    history = row.get("review_history")
    report_version = row.get("report_version")
    if (
        type(sections) is not list
        or not sections
        or any(type(section) is not dict for section in sections)
        or type(history) is not list
        or type(report_version) is not int
        or report_version < 1
    ):
        return False
    section_ids = [section.get("section_id") for section in sections]
    if any(
        type(section_id) is not str or not section_id for section_id in section_ids
    ) or len(set(section_ids)) != len(section_ids):
        return False
    known_section_ids = set(section_ids)
    latest: dict[str, tuple[str, str, str]] = {}
    base_event_fields = {
        "report_version",
        "reviewed_at",
        "decisions",
        "result",
    }
    for event in history:
        if type(event) is not dict or frozenset(event) not in {
            frozenset(base_event_fields),
            frozenset({*base_event_fields, "reviewer"}),
        }:
            return False
        event_version = event.get("report_version")
        reviewed_at = event.get("reviewed_at")
        decisions = event.get("decisions")
        result = event.get("result")
        reviewer = event.get("reviewer")
        if (
            type(event_version) is not int
            or event_version < 0
            or event_version > report_version
            or type(reviewed_at) is not str
            or not reviewed_at
            or len(reviewed_at) > 128
            or type(decisions) is not list
            or type(result) is not str
            or (
                reviewer is not None
                and (type(reviewer) is not str or not reviewer or len(reviewer) > 128)
            )
        ):
            return False
        if not decisions:
            if result != "evidence_refreshed":
                return False
            continue
        if event_version < 1 or result not in {
            "pending",
            "approved",
            "changes_requested",
        }:
            return False
        seen_in_event: set[str] = set()
        for raw_decision in decisions:
            if type(raw_decision) is not dict or set(raw_decision) != {
                "section_id",
                "decision",
                "note",
            }:
                return False
            section_id = raw_decision.get("section_id")
            decision = raw_decision.get("decision")
            note = raw_decision.get("note")
            if (
                type(section_id) is not str
                or section_id not in known_section_ids
                or section_id in seen_in_event
                or type(decision) is not str
                or decision not in {"approved", "accepted_gap", "changes_requested"}
                or type(note) is not str
                or len(note) > 2000
                or note != " ".join(note.split())
                or (decision in {"changes_requested", "accepted_gap"} and not note)
            ):
                return False
            seen_in_event.add(section_id)
            latest[section_id] = (decision, note, reviewed_at)

    for section in sections:
        section_id = section["section_id"]
        generated = section.get("generation_status") == "generated"
        required_decision = "approved" if generated else "accepted_gap"
        reviewed_at = section.get("reviewed_at")
        note = section.get("review_note")
        if (
            section.get("review_status") != required_decision
            or type(reviewed_at) is not str
            or not reviewed_at
            or len(reviewed_at) > 128
            or type(note) is not str
            or len(note) > 2000
            or note != " ".join(note.split())
            or latest.get(section_id) != (required_decision, note, reviewed_at)
            or (generated and not _research_generated_section_passed(section))
        ):
            return False
    return True


def _canonical_report_matches_sections(
    row: Mapping[str, Any], report: Mapping[str, Any]
) -> bool:
    """Bind the artifact to the complete persisted current-job projection."""

    return research_artifact_matches_job_projection(row, report)


def _bounded_requirement_result(value: Mapping[str, Any]) -> dict[str, Any]:
    requirement_id = value.get("requirement_id")
    status = value.get("status", "")
    reason_code = value.get("reason_code", "")
    evidence_count = value.get("evidence_count", 0)
    if (
        type(requirement_id) is not str
        or not requirement_id
        or len(requirement_id) > 128
    ):
        raise ResearchJobStateConflictError(
            "research requirement result has an invalid requirement_id"
        )
    if type(status) is not str or len(status) > 32:
        raise ResearchJobStateConflictError(
            "research requirement result has an invalid status"
        )
    if type(reason_code) is not str or len(reason_code) > 128:
        raise ResearchJobStateConflictError(
            "research requirement result has an invalid reason_code"
        )
    if type(evidence_count) is not int or evidence_count < 0:
        raise ResearchJobStateConflictError(
            "research requirement result has an invalid evidence_count"
        )
    return {
        "requirement_id": requirement_id,
        "status": status,
        "reason_code": reason_code,
        "evidence_count": evidence_count,
    }


def _start_job(
    row: dict[str, Any],
    *,
    evidence_provenance: Mapping[str, Any] | None = None,
    deadline_at: str | None = None,
    resource_limits: Mapping[str, Any] | None = None,
    now: str | datetime | None = None,
) -> dict[str, Any]:
    status = str(row.get("status") or "planned")
    if status == "running":
        return row
    if status not in {"planned", "paused", "failed"}:
        raise ResearchJobStateConflictError(
            f"research job cannot start from status {status}"
        )
    if status in {"planned", "failed"}:
        row["execution_id"] = uuid4().hex
        row["report_execution_id"] = ""
        row["report_execution_nodes"] = []
        row["report_status"] = "not_started"
        row["report"] = None
        row["published_report"] = None
        row["review_status"] = "not_started"
        if evidence_provenance is not None:
            row["evidence_provenance"] = dict(_clone(evidence_provenance))
        root = _research_control_root(row, create=True)
        root.clear()
        root["evidence"] = _new_research_control(
            phase="evidence",
            attempt_id=row["execution_id"],
            deadline_at=deadline_at,
            limits=resource_limits,
            now=now,
        )
    else:
        root = _research_control_root(row, create=True)
        control = root.get("evidence")
        if not isinstance(control, dict):
            control = _new_research_control(
                phase="evidence",
                attempt_id=str(row.get("execution_id") or uuid4().hex),
                deadline_at=deadline_at,
                limits=resource_limits,
                now=now,
            )
            row["execution_id"] = control["attempt_id"]
            root["evidence"] = control
        elif _control_deadline_expired(control, now=now):
            return _mark_run_failed(
                row,
                phase="evidence",
                error_class="ResearchDeadlineExceeded",
                now=now,
            )
        else:
            control.update(
                {
                    "lease_id": uuid4().hex,
                    "draining_lease_id": "",
                    "control_state": "running",
                    "heartbeat_at": _utc_now(now).isoformat(),
                    "finished_at": None,
                    "terminal_reason": "",
                }
            )
    # A paused process may have lost its worker after a section was claimed.
    # Resume must make every non-terminal claim eligible again or the queue can
    # retain a permanent ``running`` section that no worker will ever claim.
    for section in row.get("sections") or []:
        if section.get("status") in {"running", "failed"}:
            section["status"] = "pending"
            section["error"] = ""
    row["status"] = "running"
    row["started_at"] = row.get("started_at") or now_iso()
    row["evidence_completed_at"] = None
    row["error"] = ""
    return _touch(row)


def _resume_job(
    row: dict[str, Any], *, now: str | datetime | None = None
) -> dict[str, Any]:
    status = str(row.get("status") or "")
    if status == "running":
        return row
    if status != "paused":
        raise ResearchJobStateConflictError(
            f"research job cannot resume from status {status}"
        )
    return _start_job(row, now=now)


def _reset_section_for_evidence(section: dict[str, Any]) -> None:
    section.update(
        {
            "status": "pending",
            "evidence_status": "unsearched",
            "evidence_requirement_results": [],
            "evidence": [],
            "execution_metrics": {},
            "citation_ledger": [],
            "claim_audit": {},
            "coverage_audit": {},
            "verification_status": "",
            "verification_reason_code": "",
            "generation_status": "",
            "content": "",
            "revision_instruction": "",
            "review_status": "not_started",
            "review_note": "",
            "reviewed_at": None,
            "error": "",
        }
    )


def _refresh_evidence(
    row: dict[str, Any],
    *,
    evidence_provenance: Mapping[str, Any],
    deadline_at: str | None = None,
    resource_limits: Mapping[str, Any] | None = None,
    now: str | datetime | None = None,
) -> dict[str, Any]:
    status = str(row.get("status") or "")
    if status in {"running", "generating"}:
        raise ResearchJobStateConflictError(
            f"research evidence cannot refresh from status {status}"
        )
    if row.get("review_status") == "published":
        raise ResearchJobStateConflictError("published research report is immutable")
    current_report = row.get("report")
    if isinstance(current_report, Mapping):
        history = list(row.get("report_history") or [])
        history.append(
            {
                "version": int(row.get("report_version") or 1),
                "report_status": str(row.get("report_status") or "ready"),
                "review_status": str(row.get("review_status") or "not_started"),
                "archived_at": now_iso(),
                "report": dict(_clone(current_report)),
            }
        )
        row["report_history"] = history[-10:]
    for section in row.get("sections") or []:
        if isinstance(section, dict):
            _reset_section_for_evidence(section)
    review_history = list(row.get("review_history") or [])
    review_history.append(
        {
            "report_version": int(row.get("report_version") or 0),
            "reviewed_at": now_iso(),
            "decisions": [],
            "result": "evidence_refreshed",
        }
    )
    row.update(
        {
            "status": "running",
            "execution_id": uuid4().hex,
            "started_at": now_iso(),
            "evidence_completed_at": None,
            "evidence_provenance": dict(_clone(evidence_provenance)),
            "report_status": "not_started",
            "report_execution_id": "",
            "report_execution_nodes": [],
            "report_completed_at": None,
            "report": None,
            "review_status": "not_started",
            "review_history": review_history[-100:],
            "published_report": None,
            "published_at": None,
            "published_by": "",
            "publication_sha256": "",
            "regeneration_section_ids": [],
            "last_regenerated_section_ids": [],
            "error": "",
        }
    )
    _research_control_root(row, create=True)["evidence"] = _new_research_control(
        phase="evidence",
        attempt_id=row["execution_id"],
        deadline_at=deadline_at,
        limits=resource_limits,
        now=now,
    )
    return _touch(row)


def _begin_report(
    row: dict[str, Any],
    *,
    deadline_at: str | None = None,
    resource_limits: Mapping[str, Any] | None = None,
    now: str | datetime | None = None,
) -> dict[str, Any]:
    status = str(row.get("status") or "")
    retrying_failed_report = status == "failed" and row.get("report_status") == "failed"
    regenerating_reviewed_report = (
        status == "completed" and row.get("review_status") == "changes_requested"
    )
    if status == "generating":
        control = _research_control_root(row).get("report")
        if isinstance(control, Mapping) and _control_deadline_expired(control, now=now):
            return _mark_run_failed(
                row,
                phase="report",
                error_class="ResearchDeadlineExceeded",
                now=now,
            )
        return row
    if (
        status != "evidence_ready"
        and not retrying_failed_report
        and not regenerating_reviewed_report
    ):
        raise ResearchJobStateConflictError(
            f"research report cannot generate from status {status}"
        )
    current_report = row.get("report")
    if regenerating_reviewed_report and isinstance(current_report, Mapping):
        history = list(row.get("report_history") or [])
        history.append(
            {
                "version": int(row.get("report_version") or 1),
                "report_status": str(row.get("report_status") or "ready"),
                "review_status": str(row.get("review_status") or "not_started"),
                "archived_at": now_iso(),
                "report": dict(_clone(current_report)),
            }
        )
        row["report_history"] = history[-10:]
        row["regeneration_section_ids"] = [
            str(section.get("section_id") or "")
            for section in row.get("sections") or []
            if section.get("review_status") == "changes_requested"
        ]
    elif status == "evidence_ready" and not row.get("regeneration_section_ids"):
        row["regeneration_section_ids"] = []
    if row.get("regeneration_section_ids"):
        requested = {
            str(section_id)
            for section_id in row.get("regeneration_section_ids") or []
            if str(section_id)
        }
        # A pre-v2 generated section has never passed both mandatory semantic
        # gates. Selective regeneration upgrades it instead of preserving
        # unaudited or obligation-incomplete prose into a publishable report.
        row["regeneration_section_ids"] = [
            str(section.get("section_id") or "")
            for section in row.get("sections") or []
            if str(section.get("section_id") or "") in requested
            or (
                section.get("generation_status") == "generated"
                and not _research_generated_section_passed(section)
            )
        ]
    row["status"] = "generating"
    row["report_status"] = "generating"
    row["report_execution_id"] = uuid4().hex
    _research_control_root(row, create=True)["report"] = _new_research_control(
        phase="report",
        attempt_id=row["report_execution_id"],
        deadline_at=deadline_at,
        limits=resource_limits,
        now=now,
    )
    try:
        row["report_execution_nodes"] = freeze_research_execution_nodes(
            is_local=bool(row.get("is_local", False))
        )
    except ValueError as exc:
        raise ResearchJobStateConflictError(
            "research report execution backend is incompatible with job privacy mode"
        ) from exc
    row["report"] = None
    row["published_report"] = None
    row["review_status"] = "not_started"
    row["error"] = ""
    return _touch(row)


def _complete_report(
    row: dict[str, Any],
    *,
    report_execution_id: str,
    result: Mapping[str, Any],
    lease_id: str | None = None,
) -> dict[str, Any]:
    if (
        row.get("status") != "generating"
        or row.get("report_execution_id") != report_execution_id
    ):
        return row
    control = _research_control_root(row).get("report")
    if lease_id is not None:
        if (
            not isinstance(control, Mapping)
            or control.get("attempt_id") != report_execution_id
            or control.get("lease_id") != lease_id
            or control.get("control_state") != "running"
        ):
            return row
    if isinstance(control, Mapping) and _control_deadline_expired(control):
        return _mark_run_failed(
            row,
            phase="report",
            error_class="ResearchDeadlineExceeded",
        )
    raw_sections = result.get("sections")
    if type(raw_sections) is not list or any(
        type(section) is not dict for section in raw_sections
    ):
        raise ResearchJobStateConflictError(
            "research report sections must be a strict list of objects"
        )
    generated_sections = list(raw_sections)
    persisted_sections = row.get("sections")
    if type(persisted_sections) is not list or any(
        type(section) is not dict for section in persisted_sections
    ):
        raise ResearchJobStateConflictError(
            "research job plan sections must be a strict list of objects"
        )
    known_section_ids = {
        str(section.get("section_id") or "") for section in persisted_sections
    }
    if "" in known_section_ids or len(known_section_ids) != len(persisted_sections):
        raise ResearchJobStateConflictError(
            "research job plan contains invalid or duplicate section IDs"
        )
    returned_section_ids: set[str] = set()
    for generated in generated_sections:
        section_id = generated.get("section_id")
        if (
            type(section_id) is not str
            or not section_id
            or section_id not in known_section_ids
            or section_id in returned_section_ids
        ):
            raise ResearchJobStateConflictError(
                "research report contains an invalid or duplicate section_id"
            )
        returned_section_ids.add(section_id)
        for field in (
            "status",
            "verification_status",
            "verification_reason_code",
            "content",
            "error",
        ):
            if field in generated and type(generated[field]) is not str:
                raise ResearchJobStateConflictError(
                    f"research report section {field} must be a strict string"
                )
        for field in (
            "evidence_requirement_results",
            "citation_ledger",
            "evidence",
        ):
            value = generated.get(field, [])
            if type(value) is not list or any(type(item) is not dict for item in value):
                raise ResearchJobStateConflictError(
                    f"research report section {field} must be a strict list of objects"
                )
        for field in ("claim_audit", "coverage_audit"):
            if type(generated.get(field, {})) is not dict:
                raise ResearchJobStateConflictError(
                    f"research report section {field} must be a strict object"
                )
        if str(generated.get("status") or "") != "generated":
            continue
        raw_audit = generated.get("claim_audit")
        if not _research_claim_audit_passed(raw_audit):
            raise ResearchJobStateConflictError(
                "generated research section requires a passed claim audit"
            )
        raw_coverage = generated.get("coverage_audit")
        if not _research_coverage_audit_passed(raw_coverage):
            raise ResearchJobStateConflictError(
                "generated research section requires a passed requirement coverage audit"
            )
    required_section_ids = {
        str(section_id)
        for section_id in row.get("regeneration_section_ids") or []
        if str(section_id)
    } or known_section_ids
    if not required_section_ids.issubset(returned_section_ids):
        raise ResearchJobStateConflictError(
            "research report omitted a required section result"
        )
    section_results = {
        str(section.get("section_id") or ""): section for section in generated_sections
    }
    for section in persisted_sections:
        section_id = str(section.get("section_id") or "")
        generated = section_results.get(section_id)
        if generated is None:
            continue
        if section_id not in required_section_ids:
            # A selective report result may echo preserved sections for
            # composition, but it has no authority to mutate them. Their prior
            # audited prose, evidence, and human review remain the source of
            # truth; any differing global Markdown will fail canonical compare.
            continue
        verification_status = str(
            generated.get("verification_status") or "verification_error"
        )
        section.update(
            {
                "status": "completed",
                "evidence_status": (
                    verification_status
                    if verification_status in {"supported", "contradictory"}
                    else "missing"
                ),
                "verification_status": verification_status,
                "verification_reason_code": str(
                    generated.get("verification_reason_code") or ""
                ),
                "evidence_requirement_results": [
                    _bounded_requirement_result(item)
                    for item in generated.get("evidence_requirement_results") or []
                ],
                "generation_status": str(generated.get("status") or ""),
                "content": str(generated.get("content") or ""),
                "citation_ledger": [
                    dict(_clone(item))
                    for item in generated.get("citation_ledger") or []
                    if isinstance(item, Mapping)
                ],
                "claim_audit": dict(_clone(generated.get("claim_audit") or {})),
                "coverage_audit": dict(_clone(generated.get("coverage_audit") or {})),
                # Generation output is untrusted and can never approve itself.
                # Preserve review state only for an untouched section during a
                # selective regeneration; every regenerated section re-enters
                # the explicit human-review queue.
                "review_status": (
                    "pending"
                    if section_id in required_section_ids
                    else str(section.get("review_status") or "pending")
                ),
                "review_note": (
                    ""
                    if section_id in required_section_ids
                    else str(section.get("review_note") or "")
                ),
                "reviewed_at": (
                    None
                    if section_id in required_section_ids
                    else section.get("reviewed_at")
                ),
                "evidence": [
                    dict(_clone(item))
                    for item in generated.get("evidence") or []
                    if isinstance(item, Mapping)
                ],
                "error": str(generated.get("error") or ""),
            }
        )
    for section in persisted_sections:
        if section.get("generation_status") != "generated":
            continue
        if not _research_generated_section_passed(section):
            raise ResearchJobStateConflictError(
                "generated research section does not satisfy its complete "
                "atomic requirement plan"
            )
    timestamp = now_iso()
    report_version = int(row.get("report_version") or 0) + 1
    raw_ledger = result.get("citation_ledger")
    raw_metrics = result.get("verification_metrics")
    raw_content = result.get("markdown")
    provenance = row.get("evidence_provenance")
    if type(raw_ledger) is not list or any(
        type(item) is not dict for item in raw_ledger
    ):
        raise ResearchJobStateConflictError(
            "research report citation ledger must be a strict list of objects"
        )
    if type(raw_metrics) is not dict:
        raise ResearchJobStateConflictError(
            "research report verification metrics must be a strict object"
        )
    if type(raw_content) is not str:
        raise ResearchJobStateConflictError(
            "research report content must be a strict string"
        )
    if type(provenance) is not dict:
        raise ResearchJobStateConflictError(
            "research report evidence provenance must be a strict object"
        )
    try:
        canonical_content, canonical_ledger = compose_research_markdown(
            row, persisted_sections
        )
    except (TypeError, ValueError) as exc:
        raise ResearchJobStateConflictError(
            "research report sections cannot form a canonical artifact"
        ) from exc
    if raw_content != canonical_content or raw_ledger != list(canonical_ledger):
        raise ResearchJobStateConflictError(
            "research report body or ledger does not match its canonical sections"
        )
    try:
        verification = build_research_verification_snapshot(
            job=row,
            verification_metrics=raw_metrics,
            sections=[
                section
                for section in row.get("sections") or []
                if isinstance(section, Mapping)
            ],
        )
    except (TypeError, ValueError) as exc:
        raise ResearchJobStateConflictError(
            "research report verification snapshot is invalid"
        ) from exc
    row["status"] = "completed"
    row["artifact_schema_floor"] = RESEARCH_ARTIFACT_VERSION
    row["report_status"] = (
        "ready"
        if all(
            section.get("generation_status") == "generated"
            for section in persisted_sections
        )
        else "ready_with_gaps"
    )
    row["report"] = {
        "artifact_schema_version": RESEARCH_ARTIFACT_VERSION,
        "format": "markdown",
        "content": raw_content,
        "citation_ledger": _clone(raw_ledger),
        "verification_metrics": _clone(raw_metrics),
        "verification": verification,
        "provenance": _clone(provenance),
        "version": report_version,
        "generated_at": timestamp,
    }
    try:
        row["report"]["sha256"] = research_artifact_sha256(
            content=row["report"]["content"],
            citation_ledger=row["report"]["citation_ledger"],
            provenance=row["report"]["provenance"],
            verification=row["report"]["verification"],
            metadata={"version": report_version, "generated_at": timestamp},
        )
    except (TypeError, ValueError) as exc:
        raise ResearchJobStateConflictError(
            "research report artifact inputs are not verifiable"
        ) from exc
    if research_artifact_integrity_status(row["report"]) != "verified":
        raise ResearchJobStateConflictError(
            "research report artifact failed deterministic validation"
        )
    row["report_version"] = report_version
    row["report_completed_at"] = timestamp
    row["last_regenerated_section_ids"] = list(
        row.get("regeneration_section_ids") or []
    )
    row["regeneration_section_ids"] = []
    row["review_status"] = "pending"
    row["published_report"] = None
    row["published_at"] = None
    row["published_by"] = ""
    row["publication_sha256"] = ""
    row["error"] = ""
    if isinstance(control, dict):
        if lease_id is not None:
            control["last_commit_lease_id"] = lease_id
        _finish_control(control, state="completed", reason="")
    return _touch(row)


def _fail_report(
    row: dict[str, Any],
    *,
    report_execution_id: str,
    error_class: str,
    lease_id: str | None = None,
) -> dict[str, Any]:
    if (
        row.get("status") != "generating"
        or row.get("report_execution_id") != report_execution_id
    ):
        return row
    control = _research_control_root(row).get("report")
    if lease_id is not None and (
        not isinstance(control, Mapping)
        or control.get("attempt_id") != report_execution_id
        or control.get("lease_id") != lease_id
    ):
        return row
    row["status"] = "failed"
    row["report_status"] = "failed"
    row["error"] = error_class
    if isinstance(control, dict):
        _finish_control(control, state="failed", reason=error_class)
    return _touch(row)


def _review_report(
    row: dict[str, Any],
    *,
    decisions: Sequence[Mapping[str, Any]],
    expected_revision: int,
    reviewer_actor: str = "internal",
) -> dict[str, Any]:
    if not decisions:
        raise ValueError("research report review requires at least one decision")
    reviewer = " ".join(str(reviewer_actor or "").split())
    if not reviewer or len(reviewer) > 128:
        raise ValueError("research report reviewer identity is invalid")
    actual = int(row.get("revision") or 0)
    if actual != expected_revision:
        raise ResearchJobRevisionConflictError(
            "research job revision conflict: "
            f"expected {expected_revision}, found {actual}"
        )
    if row.get("status") != "completed" or not isinstance(row.get("report"), Mapping):
        raise ResearchJobStateConflictError(
            "research report can only be reviewed after generation"
        )
    if row.get("review_status") == "published":
        raise ResearchJobStateConflictError("published research report is immutable")
    report = row.get("report")
    if (
        not isinstance(report, Mapping)
        or research_artifact_integrity_status(report) != "verified"
        or not _canonical_report_matches_sections(row, report)
    ):
        raise ResearchJobStateConflictError(
            "research report does not match its verified section state"
        )
    section_by_id = {
        str(section.get("section_id") or ""): section
        for section in row.get("sections") or []
        if isinstance(section, dict)
    }
    seen: set[str] = set()
    review_event: list[dict[str, Any]] = []
    timestamp = now_iso()
    for raw in decisions:
        section_id = str(raw.get("section_id") or "")
        decision = str(raw.get("decision") or "")
        note = " ".join(str(raw.get("note") or "").split())
        if len(note) > 2000:
            raise ValueError("research report review note exceeds 2000 characters")
        if section_id in seen:
            raise ValueError(f"duplicate review section_id: {section_id}")
        seen.add(section_id)
        section = section_by_id.get(section_id)
        if section is None:
            raise ValueError(f"unknown review section_id: {section_id}")
        generated = section.get("generation_status") == "generated"
        allowed = (
            {"approved", "changes_requested"}
            if generated
            else {"accepted_gap", "changes_requested"}
        )
        if decision not in allowed:
            raise ValueError(
                f"review decision {decision} is invalid for section {section_id}"
            )
        if (
            generated
            and decision == "approved"
            and not _research_generated_section_passed(section)
        ):
            raise ResearchJobStateConflictError(
                f"section {section_id} must pass every semantic and requirement "
                "gate before approval"
            )
        if (
            section.get("review_status") == "changes_requested"
            and decision != "changes_requested"
        ):
            raise ResearchJobStateConflictError(
                f"section {section_id} must be regenerated after changes are requested"
            )
        if decision in {"changes_requested", "accepted_gap"} and not note:
            raise ValueError(f"{decision} review requires a non-blank note")
        section["review_status"] = decision
        section["review_note"] = note
        section["reviewed_at"] = timestamp
        if decision == "changes_requested":
            section["revision_instruction"] = note
        review_event.append(
            {"section_id": section_id, "decision": decision, "note": note}
        )

    section_reviews = [
        str(section.get("review_status") or "pending")
        for section in row.get("sections") or []
    ]
    if "changes_requested" in section_reviews:
        review_status = "changes_requested"
    elif all(status in {"approved", "accepted_gap"} for status in section_reviews):
        review_status = "approved"
    else:
        review_status = "pending"
    row["review_status"] = review_status
    history = list(row.get("review_history") or [])
    history.append(
        {
            "report_version": int(row.get("report_version") or 1),
            "reviewed_at": timestamp,
            "decisions": review_event,
            "result": review_status,
            "reviewer": reviewer,
        }
    )
    row["review_history"] = history[-100:]
    return _touch(row)


def _publish_report(
    row: dict[str, Any],
    *,
    expected_revision: int,
    publisher_actor: str = "internal",
) -> dict[str, Any]:
    publisher = " ".join(str(publisher_actor or "").split())
    if not publisher or len(publisher) > 128:
        raise ValueError("research report publisher identity is invalid")
    actual = int(row.get("revision") or 0)
    if actual != expected_revision:
        raise ResearchJobRevisionConflictError(
            "research job revision conflict: "
            f"expected {expected_revision}, found {actual}"
        )
    if row.get("status") != "completed" or row.get("review_status") != "approved":
        raise ResearchJobStateConflictError(
            "research report requires complete section review before publication"
        )
    for section in row.get("sections") or []:
        is_generated = section.get("generation_status") == "generated"
        if is_generated and not _research_generated_section_passed(section):
            raise ResearchJobStateConflictError(
                "research report contains a generated section that no longer "
                "matches its atomic requirement plan"
            )
    if not research_current_review_invariant(row):
        raise ResearchJobStateConflictError(
            "research report section review state is incomplete or untraceable"
        )
    report = row.get("report")
    if not isinstance(report, Mapping):
        raise ResearchJobStateConflictError("research report is unavailable")
    try:
        current_verification = build_research_verification_snapshot(
            job=row,
            verification_metrics=report.get("verification_metrics"),
            sections=[
                section
                for section in row.get("sections") or []
                if isinstance(section, Mapping)
            ],
        )
    except (TypeError, ValueError) as exc:
        raise ResearchJobStateConflictError(
            "research report verification state is invalid"
        ) from exc
    if report.get("verification") != current_verification:
        raise ResearchJobStateConflictError(
            "research report verification state does not match the artifact"
        )
    if research_artifact_integrity_status(report) != "verified":
        raise ResearchJobStateConflictError(
            "research report artifact integrity check failed"
        )
    if not _canonical_report_matches_sections(row, report):
        raise ResearchJobStateConflictError(
            "research report body does not match its verified section state"
        )
    timestamp = now_iso()
    published = dict(_clone(report))
    published["published_at"] = timestamp
    published["published_by"] = publisher
    publication_sha256 = research_publication_sha256(
        artifact_sha256=str(report.get("sha256") or ""),
        report_version=int(row.get("report_version") or 0),
        published_at=timestamp,
        published_by=publisher,
        review_history=row.get("review_history"),
        sections=row.get("sections"),
    )
    published["publication_sha256"] = publication_sha256
    row["published_report"] = published
    row["report_status"] = "published"
    row["review_status"] = "published"
    row["published_at"] = timestamp
    row["published_by"] = publisher
    row["publication_sha256"] = publication_sha256
    return _touch(row)


def _pause_job(row: dict[str, Any]) -> dict[str, Any]:
    status = str(row.get("status") or "")
    if status == "paused":
        return row
    if status != "running":
        raise ResearchJobStateConflictError(
            f"research job cannot pause from status {status}"
        )
    control = _research_control_root(row).get("evidence")
    if isinstance(control, dict):
        control["draining_lease_id"] = str(control.get("lease_id") or "")
        control["lease_id"] = ""
        control["control_state"] = "paused"
        control["heartbeat_at"] = _utc_now().isoformat()
    row["status"] = "paused"
    return _touch(row)


def _cancel_job(row: dict[str, Any]) -> dict[str, Any]:
    status = str(row.get("status") or "")
    if status == "cancelled":
        return row
    if status in {"evidence_ready", "completed"}:
        raise ResearchJobStateConflictError(
            f"research job cannot cancel from status {status}"
        )
    row["status"] = "cancelled"
    row["execution_id"] = ""
    row["report_execution_id"] = ""
    if row.get("report_status") == "generating":
        # Public schemas do not expose a report-level cancelled state yet.
        row["report_status"] = "failed"
    root = _research_control_root(row)
    for phase in _RESEARCH_CONTROL_PHASES:
        control = root.get(phase)
        if isinstance(control, dict):
            _finish_control(
                control,
                state="cancelled",
                reason="ResearchCancelled",
            )
    for section in row.get("sections") or []:
        if section.get("status") == "running":
            section["status"] = "pending"
            section["error"] = ""
    row["error"] = "ResearchCancelled"
    return _touch(row)


def _claim_next_section(
    row: dict[str, Any], execution_id: str, *, lease_id: str | None = None
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    if row.get("status") != "running" or row.get("execution_id") != execution_id:
        return row, None
    control = _research_control_root(row).get("evidence")
    if lease_id is not None and (
        not isinstance(control, Mapping)
        or control.get("attempt_id") != execution_id
        or control.get("lease_id") != lease_id
        or control.get("control_state") != "running"
    ):
        return row, None
    if isinstance(control, Mapping) and _control_deadline_expired(control):
        return (
            _mark_run_failed(
                row,
                phase="evidence",
                error_class="ResearchDeadlineExceeded",
            ),
            None,
        )
    for section in row.get("sections") or []:
        if section.get("status") != "pending":
            continue
        section["status"] = "running"
        section["error"] = ""
        _touch(row)
        return row, section
    if not any(
        section.get("status") == "running" for section in row.get("sections") or []
    ):
        row["status"] = "evidence_ready"
        row["evidence_completed_at"] = now_iso()
        if isinstance(control, dict):
            _finish_control(control, state="completed", reason="")
        _touch(row)
    return row, None


def _complete_section(
    row: dict[str, Any],
    section_id: str,
    *,
    execution_id: str,
    evidence_status: str,
    evidence: Sequence[Mapping[str, Any]],
    execution_metrics: Mapping[str, Any],
    lease_id: str | None = None,
) -> dict[str, Any]:
    if evidence_status not in {"partial", "missing"}:
        raise ValueError(
            "retrieval execution may only produce partial or missing evidence"
        )
    if (
        row.get("status") not in {"running", "paused"}
        or row.get("execution_id") != execution_id
    ):
        return row
    control = _research_control_root(row).get("evidence")
    if lease_id is not None:
        allowed_lease = (
            control.get("lease_id")
            if row.get("status") == "running" and isinstance(control, Mapping)
            else control.get("draining_lease_id")
            if row.get("status") == "paused" and isinstance(control, Mapping)
            else ""
        )
        if (
            not isinstance(control, Mapping)
            or control.get("attempt_id") != execution_id
            or allowed_lease != lease_id
        ):
            return row
    if isinstance(control, Mapping) and _control_deadline_expired(control):
        return _mark_run_failed(
            row,
            phase="evidence",
            error_class="ResearchDeadlineExceeded",
        )
    sections = row.get("sections") or []
    target = next(
        (section for section in sections if section.get("section_id") == section_id),
        None,
    )
    if target is None:
        raise KeyError(section_id)
    if target.get("status") != "running":
        return row
    metrics = dict(_clone(execution_metrics))
    prior_metrics = target.get("execution_metrics")
    if isinstance(prior_metrics, Mapping) and isinstance(
        prior_metrics.get(_RESEARCH_CONTROL_METRICS_KEY), Mapping
    ):
        metrics[_RESEARCH_CONTROL_METRICS_KEY] = dict(
            _clone(prior_metrics[_RESEARCH_CONTROL_METRICS_KEY])
        )
    target.update(
        {
            "status": "completed",
            "evidence_status": evidence_status,
            "evidence": [dict(_clone(item)) for item in evidence],
            "execution_metrics": metrics,
            "error": "",
        }
    )
    current_control = _research_control_root(row).get("evidence")
    if isinstance(current_control, dict) and lease_id is not None:
        current_control["last_commit_lease_id"] = lease_id
        current_control["last_commit_section_id"] = section_id
    if row.get("status") == "running" and not any(
        section.get("status") in {"pending", "running"} for section in sections
    ):
        row["status"] = "evidence_ready"
        row["evidence_completed_at"] = now_iso()
        if isinstance(current_control, dict):
            _finish_control(current_control, state="completed", reason="")
    return _touch(row)


def _fail_section(
    row: dict[str, Any],
    section_id: str,
    *,
    execution_id: str,
    error_class: str,
    lease_id: str | None = None,
) -> dict[str, Any]:
    if row.get("execution_id") != execution_id:
        return row
    control = _research_control_root(row).get("evidence")
    if lease_id is not None:
        allowed = (
            {
                str(control.get("lease_id") or ""),
                str(control.get("draining_lease_id") or ""),
            }
            if isinstance(control, Mapping)
            else set()
        )
        if lease_id not in allowed:
            return row
    target = next(
        (
            section
            for section in row.get("sections") or []
            if section.get("section_id") == section_id
        ),
        None,
    )
    if target is None:
        raise KeyError(section_id)
    if target.get("status") != "running":
        return row
    if row.get("status") == "paused":
        # Pause wins an atomic race with a draining worker failure. The
        # section remains retryable; a late exception must not replace the
        # user's durable pause with a terminal job failure.
        target["status"] = "pending"
        target["error"] = ""
        return _touch(row)
    target["status"] = "failed"
    target["error"] = error_class
    row["status"] = "failed"
    row["error"] = error_class
    if isinstance(control, dict):
        _finish_control(control, state="failed", reason=error_class)
    return _touch(row)


def _reconcile_running_job(
    row: Mapping[str, Any], *, terminal_reason: str = "service_restarted"
) -> dict[str, Any]:
    updated = dict(_clone(row))
    control = _research_control_root(updated).get("evidence")
    if isinstance(control, Mapping) and _control_deadline_expired(control):
        return _mark_run_failed(
            updated,
            phase="evidence",
            error_class="ResearchDeadlineExceeded",
        )
    for section in updated.get("sections") or []:
        if section.get("status") == "running":
            section["status"] = "pending"
            section["error"] = ""
    updated["status"] = "paused"
    updated["error"] = terminal_reason
    if isinstance(control, dict):
        control.update(
            {
                "control_state": "paused",
                "lease_id": "",
                "draining_lease_id": "",
                "terminal_reason": terminal_reason,
            }
        )
    return _touch(updated)


def _reconcile_generating_job(
    row: Mapping[str, Any], *, terminal_reason: str = "service_restarted"
) -> dict[str, Any]:
    updated = dict(_clone(row))
    control = _research_control_root(updated).get("report")
    if isinstance(control, Mapping) and _control_deadline_expired(control):
        return _mark_run_failed(
            updated,
            phase="report",
            error_class="ResearchDeadlineExceeded",
        )
    updated["status"] = "evidence_ready"
    updated["report_status"] = "failed"
    updated["report_execution_id"] = ""
    updated["error"] = terminal_reason
    if isinstance(control, dict):
        _finish_control(control, state="failed", reason=terminal_reason)
    return _touch(updated)


def _reconcile_termination_reason(row: Mapping[str, Any]) -> str:
    if row.get("error") == "ResearchDeadlineExceeded":
        return "deadline_exceeded"
    if row.get("error") == "service_shutdown":
        return "shutdown"
    return "service_restarted"


class ResearchJobStore:
    """Atomic JSON store for durable, editable research plans."""

    def __init__(self, path: str | None = None):
        self._path = path or get_settings().research_jobs_path
        self._lock = RLock()
        os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)

    def create(
        self,
        *,
        kb_id: str,
        objective: str,
        title: str = "",
        section_titles: Sequence[str] | None = None,
        is_local: bool = False,
        authorization: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        timestamp = now_iso()
        clean_objective = " ".join(objective.split())
        record = {
            "job_id": f"rj_{uuid4().hex}",
            "kb_id": kb_id,
            "title": " ".join(title.split()) or clean_objective[:80],
            "objective": clean_objective,
            "is_local": bool(is_local),
            **(
                {"authorization": _clone(dict(authorization))}
                if authorization is not None
                else {}
            ),
            "artifact_schema_floor": RESEARCH_ARTIFACT_VERSION,
            "status": "planned",
            "revision": 1,
            "created_at": timestamp,
            "updated_at": timestamp,
            "sections": build_research_plan(clean_objective, section_titles),
            "execution_id": "",
            "started_at": None,
            "evidence_completed_at": None,
            "report_status": "not_started",
            "report_execution_id": "",
            "report_execution_nodes": [],
            "report_completed_at": None,
            "report": None,
            "report_version": 0,
            "report_history": [],
            "review_status": "not_started",
            "review_history": [],
            "published_report": None,
            "published_at": None,
            "published_by": "",
            "publication_sha256": "",
            "regeneration_section_ids": [],
            "last_regenerated_section_ids": [],
            "evidence_provenance": {},
            "error": "",
        }
        with self._lock:
            rows = self._read_all_locked()
            rows.append(record)
            self._write_all_locked(rows)
        return _clone(record)

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            for row in self._read_all_locked():
                if row.get("job_id") == job_id:
                    return _clone(row)
        return None

    def list(
        self,
        *,
        kb_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._read_all_locked()
        if kb_id is not None:
            rows = [row for row in rows if row.get("kb_id") == kb_id]
        if status is not None:
            rows = [row for row in rows if row.get("status") == status]
        rows.sort(
            key=lambda row: (
                str(row.get("updated_at") or ""),
                str(row.get("job_id") or ""),
            ),
            reverse=True,
        )
        return _clone(rows[: max(0, limit)])

    def list_summary_rows(
        self,
        *,
        kb_id: str | None = None,
        status: str | None = None,
        limit: int = 21,
        before_updated_at: str | None = None,
        before_job_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Read a stable keyset page and discard all heavy job projections."""

        if (before_updated_at is None) != (before_job_id is None):
            raise ValueError("research summary cursor fields must be supplied together")
        with self._lock:
            rows = self._read_all_locked()
        if kb_id is not None:
            rows = [row for row in rows if row.get("kb_id") == kb_id]
        if status is not None:
            rows = [row for row in rows if row.get("status") == status]
        if before_updated_at is not None and before_job_id is not None:
            before = (before_updated_at, before_job_id)
            rows = [
                row
                for row in rows
                if (
                    str(row.get("updated_at") or ""),
                    str(row.get("job_id") or ""),
                )
                < before
            ]
        rows.sort(
            key=lambda row: (
                str(row.get("updated_at") or ""),
                str(row.get("job_id") or ""),
            ),
            reverse=True,
        )
        return [
            _clone(compact_research_job_summary(row)) for row in rows[: max(0, limit)]
        ]

    def update_plan(
        self,
        job_id: str,
        *,
        sections: Sequence[Mapping[str, Any]],
        expected_revision: int,
        is_local: bool | None = None,
    ) -> dict[str, Any]:
        normalized = _normalize_sections(sections)
        with self._lock:
            rows = self._read_all_locked()
            for position, row in enumerate(rows):
                if row.get("job_id") != job_id:
                    continue
                if row.get("status") != "planned":
                    raise ResearchJobStateConflictError(
                        "research plan can only be edited before execution starts"
                    )
                actual = int(row.get("revision") or 0)
                if actual != expected_revision:
                    raise ResearchJobRevisionConflictError(
                        "research job revision conflict: "
                        f"expected {expected_revision}, found {actual}"
                    )
                updated = {
                    **row,
                    "is_local": (
                        bool(is_local)
                        if is_local is not None
                        else bool(row.get("is_local", False))
                    ),
                    "sections": normalized,
                    "status": "planned",
                    "revision": actual + 1,
                    "updated_at": now_iso(),
                    "execution_id": "",
                    "started_at": None,
                    "evidence_completed_at": None,
                    "report_status": "not_started",
                    "report_execution_id": "",
                    "report_execution_nodes": [],
                    "report_completed_at": None,
                    "report": None,
                    "report_version": 0,
                    "report_history": [],
                    "review_status": "not_started",
                    "review_history": [],
                    "published_report": None,
                    "published_at": None,
                    "published_by": "",
                    "publication_sha256": "",
                    "regeneration_section_ids": [],
                    "last_regenerated_section_ids": [],
                    "evidence_provenance": {},
                    "error": "",
                }
                rows[position] = updated
                self._write_all_locked(rows)
                return _clone(updated)
        raise KeyError(job_id)

    def start(
        self,
        job_id: str,
        *,
        evidence_provenance: Mapping[str, Any] | None = None,
        deadline_at: str | None = None,
        resource_limits: Mapping[str, Any] | None = None,
        now: str | datetime | None = None,
    ) -> dict[str, Any]:
        return self._mutate(
            job_id,
            lambda row: _start_job(
                row,
                evidence_provenance=evidence_provenance,
                deadline_at=deadline_at,
                resource_limits=resource_limits,
                now=now,
            ),
        )

    def resume(
        self, job_id: str, *, now: str | datetime | None = None
    ) -> dict[str, Any]:
        return self._mutate(job_id, lambda row: _resume_job(row, now=now))

    def refresh_evidence(
        self,
        job_id: str,
        *,
        evidence_provenance: Mapping[str, Any],
        deadline_at: str | None = None,
        resource_limits: Mapping[str, Any] | None = None,
        now: str | datetime | None = None,
    ) -> dict[str, Any]:
        return self._mutate(
            job_id,
            lambda row: _refresh_evidence(
                row,
                evidence_provenance=evidence_provenance,
                deadline_at=deadline_at,
                resource_limits=resource_limits,
                now=now,
            ),
        )

    def pause(self, job_id: str) -> dict[str, Any]:
        return self._mutate(job_id, _pause_job)

    def cancel(self, job_id: str) -> dict[str, Any]:
        return self._mutate(job_id, _cancel_job)

    def begin_report(
        self,
        job_id: str,
        *,
        deadline_at: str | None = None,
        resource_limits: Mapping[str, Any] | None = None,
        now: str | datetime | None = None,
    ) -> dict[str, Any]:
        return self._mutate(
            job_id,
            lambda row: _begin_report(
                row,
                deadline_at=deadline_at,
                resource_limits=resource_limits,
                now=now,
            ),
        )

    def complete_report(
        self,
        job_id: str,
        *,
        report_execution_id: str,
        result: Mapping[str, Any],
        lease_id: str | None = None,
    ) -> dict[str, Any]:
        return self._mutate(
            job_id,
            lambda row: _complete_report(
                row,
                report_execution_id=report_execution_id,
                result=result,
                lease_id=lease_id,
            ),
            write_if_unchanged=False,
        )

    def fail_report(
        self,
        job_id: str,
        *,
        report_execution_id: str,
        error_class: str,
        lease_id: str | None = None,
    ) -> dict[str, Any]:
        return self._mutate(
            job_id,
            lambda row: _fail_report(
                row,
                report_execution_id=report_execution_id,
                error_class=error_class,
                lease_id=lease_id,
            ),
            write_if_unchanged=False,
        )

    def review_report(
        self,
        job_id: str,
        *,
        decisions: Sequence[Mapping[str, Any]],
        expected_revision: int,
        reviewer_actor: str = "internal",
    ) -> dict[str, Any]:
        return self._mutate(
            job_id,
            lambda row: _review_report(
                row,
                decisions=decisions,
                expected_revision=expected_revision,
                reviewer_actor=reviewer_actor,
            ),
        )

    def publish_report(
        self,
        job_id: str,
        *,
        expected_revision: int,
        publisher_actor: str = "internal",
    ) -> dict[str, Any]:
        return self._mutate(
            job_id,
            lambda row: _publish_report(
                row,
                expected_revision=expected_revision,
                publisher_actor=publisher_actor,
            ),
        )

    def claim_next_section(
        self, job_id: str, execution_id: str, *, lease_id: str | None = None
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        claimed: dict[str, Any] | None = None

        def transition(row: dict[str, Any]) -> dict[str, Any]:
            nonlocal claimed
            updated, claimed = _claim_next_section(row, execution_id, lease_id=lease_id)
            return updated

        row = self._mutate(job_id, transition, write_if_unchanged=False)
        return row, _clone(claimed) if claimed is not None else None

    def complete_section(
        self,
        job_id: str,
        section_id: str,
        *,
        execution_id: str,
        evidence_status: str,
        evidence: Sequence[Mapping[str, Any]],
        execution_metrics: Mapping[str, Any],
        lease_id: str | None = None,
    ) -> dict[str, Any]:
        return self._mutate(
            job_id,
            lambda row: _complete_section(
                row,
                section_id,
                execution_id=execution_id,
                evidence_status=evidence_status,
                evidence=evidence,
                execution_metrics=execution_metrics,
                lease_id=lease_id,
            ),
            write_if_unchanged=False,
        )

    def fail_section(
        self,
        job_id: str,
        section_id: str,
        *,
        execution_id: str,
        error_class: str,
        lease_id: str | None = None,
    ) -> dict[str, Any]:
        return self._mutate(
            job_id,
            lambda row: _fail_section(
                row,
                section_id,
                execution_id=execution_id,
                error_class=error_class,
                lease_id=lease_id,
            ),
            write_if_unchanged=False,
        )

    def reserve_research_resources(
        self,
        job_id: str,
        *,
        phase: str,
        attempt_id: str,
        lease_id: str,
        costs: Mapping[str, int] | None = None,
        now: str | datetime | None = None,
    ) -> dict[str, Any]:
        signal: (
            ResearchPaused
            | ResearchCancelled
            | ResearchDeadlineExceeded
            | ResearchBudgetExceeded
            | None
        ) = None
        normalized = normalize_resource_costs(costs)

        def transition(row: dict[str, Any]) -> dict[str, Any]:
            nonlocal signal
            root = _research_control_root(row)
            control = root.get(phase)
            if not isinstance(control, dict) or control.get("attempt_id") != attempt_id:
                signal = ResearchCancelled("research attempt is no longer current")
                return row
            state = str(control.get("control_state") or "")
            if state == "paused":
                signal = ResearchPaused()
                return row
            if state != "running" or control.get("lease_id") != lease_id:
                signal = ResearchCancelled("research lease is no longer current")
                return row
            if _control_deadline_expired(control, now=now):
                signal = ResearchDeadlineExceeded(durable=True)
                return _mark_run_failed(
                    row,
                    phase=phase,
                    error_class="ResearchDeadlineExceeded",
                    now=now,
                )
            limits = control.get("limits")
            used = control.get("used")
            if not isinstance(limits, Mapping) or not isinstance(used, dict):
                signal = ResearchBudgetExceeded("research resource state is invalid")
                return _mark_run_failed(
                    row,
                    phase=phase,
                    error_class="ResearchBudgetExceeded",
                    now=now,
                )
            for name, amount in normalized.items():
                prior = used.get(name, 0)
                limit = limits.get(name)
                if (
                    isinstance(prior, bool)
                    or not isinstance(prior, int)
                    or isinstance(limit, bool)
                    or not isinstance(limit, int)
                    or prior + amount > limit
                ):
                    signal = ResearchBudgetExceeded(
                        f"research resource budget exceeded: {name}"
                    )
                    return _mark_run_failed(
                        row,
                        phase=phase,
                        error_class="ResearchBudgetExceeded",
                        now=now,
                    )
            for name, amount in normalized.items():
                used[name] = int(used.get(name) or 0) + amount
            control["heartbeat_at"] = _utc_now(now).isoformat()
            return _touch(row)

        updated = self._mutate(job_id, transition, write_if_unchanged=False)
        if signal is not None:
            raise signal
        return updated

    def fail_run(
        self,
        job_id: str,
        *,
        phase: str,
        attempt_id: str,
        lease_id: str,
        error_class: str,
    ) -> dict[str, Any]:
        def transition(row: dict[str, Any]) -> dict[str, Any]:
            control = _research_control_root(row).get(phase)
            # A user pause is the durable winner over a callback or scheduler
            # failure arriving on the draining lease.  Resume will requeue any
            # claimed section and rotate the lease; the late failure must not
            # turn that retryable state into a terminal job failure.
            if row.get("status") == "paused" or (
                isinstance(control, Mapping)
                and control.get("control_state") == "paused"
            ):
                return row
            if (
                not isinstance(control, Mapping)
                or control.get("attempt_id") != attempt_id
            ):
                return row
            current_lease = str(control.get("lease_id") or "")
            if (
                not lease_id
                or control.get("control_state") != "running"
                or lease_id != current_lease
                or row.get("status") == "cancelled"
            ):
                return row
            return _mark_run_failed(row, phase=phase, error_class=error_class)

        return self._mutate(job_id, transition, write_if_unchanged=False)

    def reconcile_running_outcomes(
        self, *, terminal_reason: str = "service_restarted"
    ) -> dict[str, int]:
        if terminal_reason not in {"service_restarted", "service_shutdown"}:
            raise ValueError("unsupported research reconciliation reason")
        with self._lock:
            rows = self._read_all_locked()
            outcomes = {
                "service_restarted": 0,
                "deadline_exceeded": 0,
                "shutdown": 0,
            }
            for position, row in enumerate(rows):
                if row.get("status") == "running":
                    rows[position] = _reconcile_running_job(
                        row, terminal_reason=terminal_reason
                    )
                elif row.get("status") == "generating":
                    rows[position] = _reconcile_generating_job(
                        row, terminal_reason=terminal_reason
                    )
                else:
                    continue
                outcomes[_reconcile_termination_reason(rows[position])] += 1
            if sum(outcomes.values()):
                self._write_all_locked(rows)
        return outcomes

    def reconcile_running(self) -> int:
        return sum(self.reconcile_running_outcomes().values())

    def clear_kb(self, kb_id: str) -> None:
        with self._lock:
            rows = [row for row in self._read_all_locked() if row.get("kb_id") != kb_id]
            self._write_all_locked(rows)

    def export_records(self) -> list[dict[str, Any]]:
        with self._lock:
            return _clone(self._read_all_locked())

    def import_records(self, records: Sequence[Mapping[str, Any]]) -> dict[str, int]:
        incoming = [dict(_clone(record)) for record in records]
        with self._lock:
            rows = self._read_all_locked()
            positions = {
                str(row.get("job_id") or ""): idx for idx, row in enumerate(rows)
            }
            changed = 0
            for record in incoming:
                job_id = str(record.get("job_id") or "")
                if not job_id:
                    raise ValueError("research job import requires job_id")
                position = positions.get(job_id)
                if position is not None and rows[position] == record:
                    continue
                if position is None:
                    positions[job_id] = len(rows)
                    rows.append(record)
                else:
                    rows[position] = record
                changed += 1
            if changed:
                self._write_all_locked(rows)
        return {"imported": changed, "skipped": len(incoming) - changed}

    def _read_all_locked(self) -> list[dict[str, Any]]:
        if not os.path.exists(self._path):
            return []
        with open(self._path, encoding="utf-8") as handle:
            payload = json.load(handle)
        if not isinstance(payload, list):
            raise ValueError("research job store must contain a JSON list")
        return [dict(row) for row in payload if isinstance(row, Mapping)]

    def _mutate(
        self,
        job_id: str,
        transition,
        *,
        write_if_unchanged: bool = True,
    ) -> dict[str, Any]:
        with self._lock:
            rows = self._read_all_locked()
            for position, row in enumerate(rows):
                if row.get("job_id") != job_id:
                    continue
                updated = transition(_clone(row))
                if write_if_unchanged or updated != row:
                    rows[position] = updated
                    self._write_all_locked(rows)
                return _clone(updated)
        raise KeyError(job_id)

    def _write_all_locked(self, rows: list[dict[str, Any]]) -> None:
        temporary_path = f"{self._path}.tmp"
        try:
            with open(temporary_path, "w", encoding="utf-8") as handle:
                json.dump(rows, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, self._path)
        finally:
            if os.path.exists(temporary_path):
                os.unlink(temporary_path)


class SqliteResearchJobStore(ResearchJobStore):
    """SQLite adapter with the same contract as ``ResearchJobStore``."""

    def __init__(self, db_path: str):
        self._lock = RLock()
        self._closed = False
        self._conn = connect_sqlite(db_path)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS research_jobs ("
            "job_id TEXT PRIMARY KEY, kb_id TEXT NOT NULL, status TEXT NOT NULL, "
            "updated_at TEXT NOT NULL, data TEXT NOT NULL, "
            "summary TEXT NOT NULL DEFAULT '{}')"
        )
        columns = {
            row[1]
            for row in self._conn.execute("PRAGMA table_info(research_jobs)").fetchall()
        }
        if "summary" not in columns:
            self._conn.execute(
                "ALTER TABLE research_jobs "
                "ADD COLUMN summary TEXT NOT NULL DEFAULT '{}'"
            )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_research_jobs_queue "
            "ON research_jobs(kb_id, status, updated_at DESC)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_research_jobs_summary_page "
            "ON research_jobs(kb_id, updated_at DESC, job_id DESC)"
        )
        # One-time mixed-version backfill. Subsequent transitions keep the
        # compact collection projection in the same atomic upsert as `data`.
        for job_id, raw_data, raw_summary in self._conn.execute(
            "SELECT job_id, data, summary FROM research_jobs"
        ).fetchall():
            try:
                stored = json.loads(raw_summary)
            except (TypeError, json.JSONDecodeError):
                stored = {}
            if (
                isinstance(stored, Mapping)
                and stored.get("storage_version") == RESEARCH_SUMMARY_STORAGE_VERSION
            ):
                continue
            compact = compact_research_job_summary(json.loads(raw_data))
            self._conn.execute(
                "UPDATE research_jobs SET summary=? WHERE job_id=?",
                (json.dumps(compact, ensure_ascii=False), job_id),
            )

    def create(
        self,
        *,
        kb_id: str,
        objective: str,
        title: str = "",
        section_titles: Sequence[str] | None = None,
        is_local: bool = False,
        authorization: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        timestamp = now_iso()
        clean_objective = " ".join(objective.split())
        record = {
            "job_id": f"rj_{uuid4().hex}",
            "kb_id": kb_id,
            "title": " ".join(title.split()) or clean_objective[:80],
            "objective": clean_objective,
            "is_local": bool(is_local),
            **(
                {"authorization": _clone(dict(authorization))}
                if authorization is not None
                else {}
            ),
            "artifact_schema_floor": RESEARCH_ARTIFACT_VERSION,
            "status": "planned",
            "revision": 1,
            "created_at": timestamp,
            "updated_at": timestamp,
            "sections": build_research_plan(clean_objective, section_titles),
            "execution_id": "",
            "started_at": None,
            "evidence_completed_at": None,
            "report_status": "not_started",
            "report_execution_id": "",
            "report_execution_nodes": [],
            "report_completed_at": None,
            "report": None,
            "report_version": 0,
            "report_history": [],
            "review_status": "not_started",
            "review_history": [],
            "published_report": None,
            "published_at": None,
            "published_by": "",
            "publication_sha256": "",
            "regeneration_section_ids": [],
            "last_regenerated_section_ids": [],
            "evidence_provenance": {},
            "error": "",
        }
        with self._lock:
            self._ensure_open()
            self._upsert_locked(record)
        return _clone(record)

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            self._ensure_open()
            row = self._conn.execute(
                "SELECT data FROM research_jobs WHERE job_id=?", (job_id,)
            ).fetchone()
        return _clone(json.loads(row[0])) if row is not None else None

    def list(
        self,
        *,
        kb_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if kb_id is not None:
            clauses.append("kb_id=?")
            params.append(kb_id)
        if status is not None:
            clauses.append("status=?")
            params.append(status)
        query = "SELECT data FROM research_jobs"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY updated_at DESC, job_id DESC LIMIT ?"
        params.append(max(0, limit))
        with self._lock:
            self._ensure_open()
            rows = self._conn.execute(query, tuple(params)).fetchall()
        return [_clone(json.loads(row[0])) for row in rows]

    def list_summary_rows(
        self,
        *,
        kb_id: str | None = None,
        status: str | None = None,
        limit: int = 21,
        before_updated_at: str | None = None,
        before_job_id: str | None = None,
    ) -> list[dict[str, Any]]:
        if (before_updated_at is None) != (before_job_id is None):
            raise ValueError("research summary cursor fields must be supplied together")
        clauses: list[str] = []
        params: list[Any] = []
        if kb_id is not None:
            clauses.append("kb_id=?")
            params.append(kb_id)
        if status is not None:
            clauses.append("status=?")
            params.append(status)
        if before_updated_at is not None and before_job_id is not None:
            clauses.append("(updated_at < ? OR (updated_at = ? AND job_id < ?))")
            params.extend((before_updated_at, before_updated_at, before_job_id))
        query = "SELECT summary FROM research_jobs"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY updated_at DESC, job_id DESC LIMIT ?"
        params.append(max(0, limit))
        with self._lock:
            self._ensure_open()
            rows = self._conn.execute(query, tuple(params)).fetchall()
        return [_clone(json.loads(row[0])) for row in rows]

    def update_plan(
        self,
        job_id: str,
        *,
        sections: Sequence[Mapping[str, Any]],
        expected_revision: int,
        is_local: bool | None = None,
    ) -> dict[str, Any]:
        normalized = _normalize_sections(sections)
        with self._lock:
            self._ensure_open()
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT data FROM research_jobs WHERE job_id=?", (job_id,)
                ).fetchone()
                if row is None:
                    raise KeyError(job_id)
                current = dict(json.loads(row[0]))
                if current.get("status") != "planned":
                    raise ResearchJobStateConflictError(
                        "research plan can only be edited before execution starts"
                    )
                actual = int(current.get("revision") or 0)
                if actual != expected_revision:
                    raise ResearchJobRevisionConflictError(
                        "research job revision conflict: "
                        f"expected {expected_revision}, found {actual}"
                    )
                updated = {
                    **current,
                    "is_local": (
                        bool(is_local)
                        if is_local is not None
                        else bool(current.get("is_local", False))
                    ),
                    "sections": normalized,
                    "status": "planned",
                    "revision": actual + 1,
                    "updated_at": now_iso(),
                    "execution_id": "",
                    "started_at": None,
                    "evidence_completed_at": None,
                    "report_status": "not_started",
                    "report_execution_id": "",
                    "report_execution_nodes": [],
                    "report_completed_at": None,
                    "report": None,
                    "report_version": 0,
                    "report_history": [],
                    "review_status": "not_started",
                    "review_history": [],
                    "published_report": None,
                    "published_at": None,
                    "published_by": "",
                    "publication_sha256": "",
                    "regeneration_section_ids": [],
                    "last_regenerated_section_ids": [],
                    "evidence_provenance": {},
                    "error": "",
                }
                self._upsert_locked(updated)
                self._conn.execute("COMMIT")
                return _clone(updated)
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def start(
        self,
        job_id: str,
        *,
        evidence_provenance: Mapping[str, Any] | None = None,
        deadline_at: str | None = None,
        resource_limits: Mapping[str, Any] | None = None,
        now: str | datetime | None = None,
    ) -> dict[str, Any]:
        return self._mutate_sqlite(
            job_id,
            lambda row: _start_job(
                row,
                evidence_provenance=evidence_provenance,
                deadline_at=deadline_at,
                resource_limits=resource_limits,
                now=now,
            ),
        )

    def resume(
        self, job_id: str, *, now: str | datetime | None = None
    ) -> dict[str, Any]:
        return self._mutate_sqlite(job_id, lambda row: _resume_job(row, now=now))

    def refresh_evidence(
        self,
        job_id: str,
        *,
        evidence_provenance: Mapping[str, Any],
        deadline_at: str | None = None,
        resource_limits: Mapping[str, Any] | None = None,
        now: str | datetime | None = None,
    ) -> dict[str, Any]:
        return self._mutate_sqlite(
            job_id,
            lambda row: _refresh_evidence(
                row,
                evidence_provenance=evidence_provenance,
                deadline_at=deadline_at,
                resource_limits=resource_limits,
                now=now,
            ),
        )

    def pause(self, job_id: str) -> dict[str, Any]:
        return self._mutate_sqlite(job_id, _pause_job)

    def cancel(self, job_id: str) -> dict[str, Any]:
        return self._mutate_sqlite(job_id, _cancel_job)

    def begin_report(
        self,
        job_id: str,
        *,
        deadline_at: str | None = None,
        resource_limits: Mapping[str, Any] | None = None,
        now: str | datetime | None = None,
    ) -> dict[str, Any]:
        return self._mutate_sqlite(
            job_id,
            lambda row: _begin_report(
                row,
                deadline_at=deadline_at,
                resource_limits=resource_limits,
                now=now,
            ),
        )

    def complete_report(
        self,
        job_id: str,
        *,
        report_execution_id: str,
        result: Mapping[str, Any],
        lease_id: str | None = None,
    ) -> dict[str, Any]:
        return self._mutate_sqlite(
            job_id,
            lambda row: _complete_report(
                row,
                report_execution_id=report_execution_id,
                result=result,
                lease_id=lease_id,
            ),
        )

    def fail_report(
        self,
        job_id: str,
        *,
        report_execution_id: str,
        error_class: str,
        lease_id: str | None = None,
    ) -> dict[str, Any]:
        return self._mutate_sqlite(
            job_id,
            lambda row: _fail_report(
                row,
                report_execution_id=report_execution_id,
                error_class=error_class,
                lease_id=lease_id,
            ),
        )

    def review_report(
        self,
        job_id: str,
        *,
        decisions: Sequence[Mapping[str, Any]],
        expected_revision: int,
        reviewer_actor: str = "internal",
    ) -> dict[str, Any]:
        return self._mutate_sqlite(
            job_id,
            lambda row: _review_report(
                row,
                decisions=decisions,
                expected_revision=expected_revision,
                reviewer_actor=reviewer_actor,
            ),
        )

    def publish_report(
        self,
        job_id: str,
        *,
        expected_revision: int,
        publisher_actor: str = "internal",
    ) -> dict[str, Any]:
        return self._mutate_sqlite(
            job_id,
            lambda row: _publish_report(
                row,
                expected_revision=expected_revision,
                publisher_actor=publisher_actor,
            ),
        )

    def claim_next_section(
        self, job_id: str, execution_id: str, *, lease_id: str | None = None
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        claimed: dict[str, Any] | None = None

        def transition(row: dict[str, Any]) -> dict[str, Any]:
            nonlocal claimed
            updated, claimed = _claim_next_section(row, execution_id, lease_id=lease_id)
            return updated

        row = self._mutate_sqlite(job_id, transition)
        return row, _clone(claimed) if claimed is not None else None

    def complete_section(
        self,
        job_id: str,
        section_id: str,
        *,
        execution_id: str,
        evidence_status: str,
        evidence: Sequence[Mapping[str, Any]],
        execution_metrics: Mapping[str, Any],
        lease_id: str | None = None,
    ) -> dict[str, Any]:
        return self._mutate_sqlite(
            job_id,
            lambda row: _complete_section(
                row,
                section_id,
                execution_id=execution_id,
                evidence_status=evidence_status,
                evidence=evidence,
                execution_metrics=execution_metrics,
                lease_id=lease_id,
            ),
        )

    def fail_section(
        self,
        job_id: str,
        section_id: str,
        *,
        execution_id: str,
        error_class: str,
        lease_id: str | None = None,
    ) -> dict[str, Any]:
        return self._mutate_sqlite(
            job_id,
            lambda row: _fail_section(
                row,
                section_id,
                execution_id=execution_id,
                error_class=error_class,
                lease_id=lease_id,
            ),
        )

    def reconcile_running_outcomes(
        self, *, terminal_reason: str = "service_restarted"
    ) -> dict[str, int]:
        if terminal_reason not in {"service_restarted", "service_shutdown"}:
            raise ValueError("unsupported research reconciliation reason")
        with self._lock:
            self._ensure_open()
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                # Acquire the write reservation before reading. Otherwise a
                # second connection can complete work between this SELECT and
                # BEGIN, and an orphan-recovery snapshot would overwrite the
                # newer terminal state.
                rows = self._conn.execute(
                    "SELECT job_id, data FROM research_jobs "
                    "WHERE status IN ('running', 'generating')"
                ).fetchall()
                outcomes = {
                    "service_restarted": 0,
                    "deadline_exceeded": 0,
                    "shutdown": 0,
                }
                for _, raw in rows:
                    current = json.loads(raw)
                    updated = (
                        _reconcile_running_job(current, terminal_reason=terminal_reason)
                        if current.get("status") == "running"
                        else _reconcile_generating_job(
                            current, terminal_reason=terminal_reason
                        )
                    )
                    outcomes[_reconcile_termination_reason(updated)] += 1
                    self._upsert_locked(updated)
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return outcomes

    def reconcile_running(self) -> int:
        return sum(self.reconcile_running_outcomes().values())

    def clear_kb(self, kb_id: str) -> None:
        with self._lock:
            self._ensure_open()
            self._conn.execute("DELETE FROM research_jobs WHERE kb_id=?", (kb_id,))

    def export_records(self) -> list[dict[str, Any]]:
        return self.list(limit=2**31 - 1)

    def import_records(self, records: Sequence[Mapping[str, Any]]) -> dict[str, int]:
        incoming = [dict(_clone(record)) for record in records]
        with self._lock:
            self._ensure_open()
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                changed = 0
                for record in incoming:
                    job_id = str(record.get("job_id") or "")
                    if not job_id:
                        raise ValueError("research job import requires job_id")
                    existing = self._conn.execute(
                        "SELECT data FROM research_jobs WHERE job_id=?", (job_id,)
                    ).fetchone()
                    if existing is not None and json.loads(existing[0]) == record:
                        continue
                    self._upsert_locked(record)
                    changed += 1
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return {"imported": changed, "skipped": len(incoming) - changed}

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._conn.close()
            self._closed = True

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("SqliteResearchJobStore is closed")

    def _upsert_locked(self, record: Mapping[str, Any]) -> None:
        summary = compact_research_job_summary(record)
        self._conn.execute(
            "INSERT INTO research_jobs"
            "(job_id, kb_id, status, updated_at, data, summary) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(job_id) DO UPDATE SET "
            "kb_id=excluded.kb_id, status=excluded.status, "
            "updated_at=excluded.updated_at, data=excluded.data, "
            "summary=excluded.summary",
            (
                record["job_id"],
                record["kb_id"],
                record["status"],
                record["updated_at"],
                json.dumps(record, ensure_ascii=False),
                json.dumps(summary, ensure_ascii=False),
            ),
        )

    # Shared durable-control helpers in the base class route their atomic
    # transitions through this adapter without duplicating the policy logic.
    def _mutate(
        self,
        job_id: str,
        transition,
        *,
        write_if_unchanged: bool = True,
    ) -> dict[str, Any]:
        del write_if_unchanged
        return self._mutate_sqlite(job_id, transition)

    def _mutate_sqlite(self, job_id: str, transition) -> dict[str, Any]:
        with self._lock:
            self._ensure_open()
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT data FROM research_jobs WHERE job_id=?", (job_id,)
                ).fetchone()
                if row is None:
                    raise KeyError(job_id)
                current = dict(json.loads(row[0]))
                updated = transition(_clone(current))
                if updated != current:
                    self._upsert_locked(updated)
                self._conn.execute("COMMIT")
                return _clone(updated)
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
