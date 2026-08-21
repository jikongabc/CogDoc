from __future__ import annotations

import copy
from contextlib import nullcontext
import hashlib
import inspect
import io
import json
import math
import time
import zipfile
from datetime import datetime, timedelta, timezone
from threading import Event
from typing import Literal

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, Response

from cogdoc.api.offload import run_sync, run_sync_until
from cogdoc.api.eval_review_auth import require_eval_reviewer
from cogdoc.api.research_job_store import (
    ResearchJobRevisionConflictError,
    ResearchJobStateConflictError,
    ResearchJobStore,
    research_current_review_invariant,
    research_run_control,
)
from cogdoc.api.research_access import (
    build_research_authorization,
    research_authorization,
    research_retrieval_scope,
)
from cogdoc.api.schemas import (
    ErrorCode,
    ResearchJob,
    ResearchJobCreate,
    ResearchJobListResponse,
    ResearchJobResponse,
    ResearchJobSummaryPage,
    ResearchPlanGenerateRequest,
    ResearchPlanUpdate,
    ResearchProvenanceResponse,
    ResearchReportPublishRequest,
    ResearchReportReviewUpdate,
    build_error_response,
)
from cogdoc.api.tenant_scope import (
    externalize_kb_fields,
    is_user_session_principal,
    request_principal,
    resource_access_decision,
    resolve_kb_scope,
    scope_for_storage_id,
    tenant_storage_ids,
)
from cogdoc.api.tenancy import Permission, ROLE_PERMISSIONS, Role, required_permission
from cogdoc.daemon_executor import DaemonExecutorCapacityError
from cogdoc.config.settings import get_settings
from cogdoc.research_control import (
    ResearchCancelled,
    ResearchDeadlineExceeded,
)
from cogdoc.service.research_execution import (
    ResearchEvidenceStaleError,
    ResearchExecutionCapacityError,
)
from cogdoc.service.research_provenance import (
    RESEARCH_ARTIFACT_VERSION,
    research_artifact_sha256,
    research_artifact_integrity_status,
    research_artifact_matches_job_projection,
    research_publication_sha256,
)
from cogdoc.service.research_summary import (
    ResearchSummaryCursorError,
    ResearchSummaryProjectionError,
    decode_research_summary_cursor,
    if_none_match_matches,
    paginate_research_job_summaries,
    project_research_job_summaries,
    research_summary_page_etag,
)


router = APIRouter(prefix="/v1/research-jobs", tags=["research"])


def _error(code: ErrorCode, message: str, status: int) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content=build_error_response(code, message).model_dump(),
    )


def _store(request: Request) -> ResearchJobStore | None:
    return getattr(request.app.state, "research_job_store", None)


def _manager(request: Request):
    return getattr(request.app.state, "research_execution_manager", None)


def _job_is_authorized(request: Request, row) -> bool:
    """Authorize both the KB and the exact evidence boundary frozen by a job."""

    if not isinstance(row, dict):
        return False
    storage_id = str(row.get("kb_id") or "")
    scope = scope_for_storage_id(request, storage_id) if storage_id else None
    if scope is None:
        return False
    principal = request_principal(request)
    if is_user_session_principal(principal):
        auth_store = getattr(request.app.state, "auth_store", None)
        membership_reader = getattr(auth_store, "membership", None)
        session_is_active = getattr(auth_store, "session_is_active", None)
        if not callable(membership_reader) or not callable(session_is_active):
            return False
        try:
            if not session_is_active(
                session_id=principal.key_fingerprint.removeprefix("session:"),
                user_id=principal.subject_id,
                workspace_id=principal.tenant_id,
            ):
                return False
            membership = membership_reader(principal.tenant_id, principal.subject_id)
            live_role = Role(str(membership.get("role") or ""))
            live_membership_id = str(
                membership.get("member_id") or membership.get("membership_id") or ""
            )
        except Exception:
            return False
        if (
            principal.membership_id is not None
            and live_membership_id != principal.membership_id
        ):
            return False
        # The middleware principal is a request-start snapshot. If membership
        # changed while a long-running research operation was in flight, do not
        # reuse its stale role for the KB/resource ACL checks below. A retry will
        # authenticate a fresh principal with the live role.
        if live_role != principal.role:
            return False
        if (
            required_permission(request.method, request.url.path)
            not in ROLE_PERMISSIONS[live_role]
        ):
            return False
    authorization = research_authorization(row)
    if authorization is None:
        # Old artifacts predate creator/source binding. Keep API-key/local mode
        # compatible, but expose them to real users only to tenant governance.
        return not is_user_session_principal(principal) or principal.role in {
            Role.OWNER,
            Role.ADMIN,
        }
    if str(authorization.get("tenant_id") or "") != principal.tenant_id:
        return False
    decision = resource_access_decision(request, scope)
    if decision is None:
        return True
    if decision is False or not getattr(decision, "is_allowed", False):
        return False
    current_mode = str(getattr(getattr(decision, "mode", None), "value", ""))
    if current_mode == "all":
        return True
    if current_mode != "subset":
        return False
    if str(authorization.get("mode") or "") != "subset":
        return False
    current_sources = {str(item) for item in decision.allowed_sources}
    frozen_sources = {str(item) for item in authorization.get("allowed_sources") or ()}
    return bool(frozen_sources and frozen_sources <= current_sources)


def _accepts_keyword(operation, name: str) -> bool:
    try:
        parameters = inspect.signature(operation).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        parameter.name == name or parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters
    )


def _provenance_matches_job_scope(status, row) -> bool:
    """Reject a manager result that attempts to project another KB's state."""

    expected_kb_id = str(row.get("kb_id") or "") if isinstance(row, dict) else ""
    if not expected_kb_id or not isinstance(status, dict):
        return False
    for field in ("captured", "current"):
        snapshot = status.get(field)
        if snapshot in (None, {}):
            continue
        if not isinstance(snapshot, dict) or snapshot.get("kb_id") != expected_kb_id:
            return False
    return True


def _run_auto_plan(
    generator,
    source_reader,
    *,
    kb_id: str,
    objective: str,
    is_local: bool,
    deadline_at: str,
    stop_event: Event,
    retrieval_scope=None,
):
    try:
        sources = source_reader(kb_id)
    except Exception:
        # An empty or legacy index can still receive a fact-free editable plan.
        sources = []
    if retrieval_scope is not None:
        sources = [
            source for source in sources if retrieval_scope.allows_source(source)
        ]
    if stop_event.is_set():
        raise ResearchCancelled("research planning request stopped")
    kwargs: dict[str, bool | str | Event] = {"is_local": is_local}
    if _accepts_keyword(generator, "deadline_at"):
        kwargs["deadline_at"] = deadline_at
    if _accepts_keyword(generator, "stop_event"):
        kwargs["stop_event"] = stop_event
    return generator(objective, sources, **kwargs)


def _commit_auto_plan_if_authorized(
    request: Request,
    store: ResearchJobStore,
    current: dict,
    job_id: str,
    *,
    sections,
    expected_revision: int,
    is_local: bool,
):
    """Revalidate the live ACL in the same worker call as the plan commit."""

    if not _job_is_authorized(request, current):
        return None
    return store.update_plan(
        job_id,
        sections=sections,
        expected_revision=expected_revision,
        is_local=is_local,
    )


def _observe_lifecycle(
    request: Request,
    *,
    action: str,
    outcome: str,
    row=None,
    job_id: str = "",
    error_class: str = "",
) -> None:
    """Emit bounded operational telemetry without affecting API delivery."""

    observer = getattr(request.app.state, "research_observer", None)
    if observer is None:
        return
    source = row if isinstance(row, dict) else {}
    try:
        observer.lifecycle(
            action=action,
            outcome=outcome,
            job_id=str(source.get("job_id") or job_id),
            kb_id=str(source.get("kb_id") or ""),
            execution_id=str(
                (
                    source.get("report_execution_id")
                    if action in {"generate", "review", "publish"}
                    else source.get("execution_id")
                )
                or ""
            ),
            status=str(source.get("status") or ""),
            error_class=error_class,
        )
    except Exception:
        # Telemetry is intentionally side-channel only.
        return


def _artifact_status(report) -> str:
    try:
        return research_artifact_integrity_status(report)
    except Exception:
        return "invalid"


def _verified_artifact_matches_sections(row, report) -> bool:
    return research_artifact_matches_job_projection(row, report)


def _published_state_matches(row, report) -> bool:
    if type(row) is not dict or type(report) is not dict:
        return False
    row_published_at = row.get("published_at")
    report_published_at = report.get("published_at")
    row_published_by = row.get("published_by")
    actor_matches = row.get("artifact_schema_floor") != RESEARCH_ARTIFACT_VERSION or (
        type(row_published_by) is str
        and bool(row_published_by)
        and report.get("published_by") == row_published_by
    )
    publication_matches = True
    if row.get("artifact_schema_floor") == RESEARCH_ARTIFACT_VERSION:
        try:
            expected_publication_sha256 = research_publication_sha256(
                artifact_sha256=str(report.get("sha256") or ""),
                report_version=int(row.get("report_version") or 0),
                published_at=str(row_published_at or ""),
                published_by=str(row_published_by or ""),
                review_history=row.get("review_history"),
                sections=row.get("sections"),
            )
        except (TypeError, ValueError):
            publication_matches = False
        else:
            publication_matches = (
                row.get("publication_sha256") == expected_publication_sha256
                and report.get("publication_sha256") == expected_publication_sha256
            )
    return (
        row.get("status") == "completed"
        and row.get("review_status") == "published"
        and row.get("report_status") == "published"
        and type(row_published_at) is str
        and bool(row_published_at)
        and report_published_at == row_published_at
        and actor_matches
        and publication_matches
    )


def _verified_published_artifact_matches(row, report) -> bool:
    return (
        _published_state_matches(row, report)
        and research_current_review_invariant(row)
        and _verified_artifact_matches_sections(row, report)
    )


def _redact_unverified_section_outputs(payload: dict, *, clear_evidence: bool) -> None:
    sections = payload.get("sections")
    if not isinstance(sections, list):
        return
    for section in sections:
        if not isinstance(section, dict):
            continue
        section["content"] = ""
        section["citation_ledger"] = []
        section["evidence_requirement_results"] = []
        section["claim_audit"] = None
        section["coverage_audit"] = None
        section["generation_status"] = ""
        if clear_evidence:
            section["evidence"] = []
            section["verification_status"] = ""
            section["verification_reason_code"] = ""
            section["error"] = ""
        # Operational run-control state uses a private reserved metrics key in
        # durable storage. Never expose that container (or any imported model
        # diagnostics) through the public section shape.
        section["execution_metrics"] = _public_execution_metrics(
            section.get("execution_metrics")
        )


def _remove_uncommitted_evidence_previews(payload: dict) -> None:
    sections = payload.get("sections")
    if not isinstance(sections, list):
        return
    for section in sections:
        if not isinstance(section, dict):
            continue
        evidence = section.get("evidence")
        if not isinstance(evidence, list):
            evidence = []
            section["evidence"] = evidence
        for item in evidence:
            if isinstance(item, dict):
                # verification.json binds coordinates and text_hash, but not
                # source prose. Never let a mutable preview ride on a verified
                # artifact's integrity label.
                item["text_preview"] = ""
        # These diagnostics are mutable execution details rather than artifact
        # commitments.  Keep only the bounded counters used by the UI and never
        # expose arbitrary model/provider payloads imported into the store.
        section["execution_metrics"] = _public_execution_metrics(
            section.get("execution_metrics")
        )
        section["error"] = ""
        section["verification_reason_code"] = ""


def _public_nonnegative_number(value):
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if value < 0 or not math.isfinite(float(value)):
        return None
    return value


def _public_execution_metrics(value) -> dict:
    if not isinstance(value, dict):
        return {}
    projected: dict = {}
    for field in ("candidate_count", "evidence_count", "query_count", "duration_ms"):
        normalized = _public_nonnegative_number(value.get(field))
        if normalized is not None:
            projected[field] = normalized
    requirements = value.get("requirements")
    if isinstance(requirements, list):
        rows = []
        for raw in requirements[:16]:
            if not isinstance(raw, dict):
                continue
            requirement_id = raw.get("requirement_id")
            candidate_count = _public_nonnegative_number(raw.get("candidate_count"))
            if type(requirement_id) is not str or not requirement_id:
                continue
            row = {"requirement_id": requirement_id[:128]}
            if candidate_count is not None:
                row["candidate_count"] = candidate_count
            rows.append(row)
        projected["requirements"] = rows
    return projected


def _public_execution_control(row) -> dict:
    resources = (
        "retrieval_queries",
        "candidate_docs",
        "llm_calls",
        "model_input_chars",
    )
    allowed_states = {
        "running",
        "paused",
        "cancelled",
        "expired",
        "budget_exhausted",
        "failed",
        "completed",
    }
    projected = {}
    for phase in ("evidence", "report"):
        raw = research_run_control(row, phase)
        state = raw.get("control_state")
        if state not in allowed_states:
            continue
        limits = raw.get("limits")
        used = raw.get("used")
        if not isinstance(limits, dict) or not isinstance(used, dict):
            continue
        bounded_limits = {}
        bounded_used = {}
        valid = True
        for name in resources:
            limit = limits.get(name)
            consumed = used.get(name)
            if (
                type(limit) is not int
                or limit < 0
                or type(consumed) is not int
                or consumed < 0
            ):
                valid = False
                break
            bounded_limits[name] = limit
            bounded_used[name] = min(consumed, limit)
        if not valid:
            continue
        text_fields = {}
        for name in (
            "attempt_id",
            "deadline_at",
            "started_at",
            "heartbeat_at",
            "terminal_reason",
        ):
            value = raw.get(name)
            if type(value) is not str or len(value) > 128:
                valid = False
                break
            text_fields[name] = value
        if not valid:
            continue
        finished_at = raw.get("finished_at")
        if finished_at is not None and (
            type(finished_at) is not str or len(finished_at) > 128
        ):
            continue
        projected[phase] = {
            "phase": phase,
            **text_fields,
            "control_state": state,
            "limits": bounded_limits,
            "used": bounded_used,
            "finished_at": finished_at,
        }
    return projected


def _public_review_history(value) -> list[dict]:
    if not isinstance(value, list):
        return []
    projected: list[dict] = []
    for raw in value[-100:]:
        if not isinstance(raw, dict):
            continue
        report_version = raw.get("report_version")
        reviewed_at = raw.get("reviewed_at")
        result = raw.get("result")
        reviewer = raw.get("reviewer", "")
        if (
            isinstance(report_version, bool)
            or not isinstance(report_version, int)
            or report_version < 0
            or type(reviewed_at) is not str
            or type(result) is not str
            or type(reviewer) is not str
        ):
            continue
        decisions = []
        raw_decisions = raw.get("decisions")
        if isinstance(raw_decisions, list):
            for decision in raw_decisions[:12]:
                if not isinstance(decision, dict):
                    continue
                section_id = decision.get("section_id")
                verdict = decision.get("decision")
                note = decision.get("note", "")
                if (
                    type(section_id) is not str
                    or type(verdict) is not str
                    or type(note) is not str
                ):
                    continue
                decisions.append(
                    {
                        "section_id": section_id[:64],
                        "decision": verdict[:32],
                        "note": note[:2000],
                    }
                )
        projected.append(
            {
                "report_version": report_version,
                "reviewed_at": reviewed_at,
                "decisions": decisions,
                "result": result[:32],
                "reviewer": reviewer[:128],
            }
        )
    return projected


def _safe_public_job_payload(row) -> dict:
    """Never expose report bodies that did not pass the v2 artifact gate."""

    payload = copy.deepcopy(dict(row))
    # Authorization snapshots contain physical resource boundaries and are an
    # internal execution capability, never an API response field.
    payload.pop("authorization", None)
    payload["execution_control"] = _public_execution_control(row)
    has_verified_current_projection = False
    had_current_artifact = any(
        payload.get(field) is not None for field in ("report", "published_report")
    )
    report = payload.get("report")
    if report is not None:
        if not _verified_artifact_matches_sections(payload, report):
            payload["report"] = None
        else:
            has_verified_current_projection = True
    published_report = payload.get("published_report")
    if published_report is not None:
        if not _verified_published_artifact_matches(payload, published_report):
            payload["published_report"] = None
        else:
            has_verified_current_projection = True
    history = payload.get("report_history")
    if isinstance(history, list):
        safe_history = []
        for item in history:
            if (
                not isinstance(item, dict)
                or _artifact_status(item.get("report")) != "verified"
            ):
                continue
            safe_history.append(item)
        payload["report_history"] = safe_history
    if has_verified_current_projection:
        _remove_uncommitted_evidence_previews(payload)
    else:
        _redact_unverified_section_outputs(payload, clear_evidence=had_current_artifact)
    payload["review_history"] = _public_review_history(payload.get("review_history"))
    return payload


def _rehash_externalized_artifact(report: dict) -> None:
    """Keep a v2 artifact self-verifying after physical KB IDs are projected."""

    if report.get("artifact_schema_version") != RESEARCH_ARTIFACT_VERSION:
        return
    report["sha256"] = research_artifact_sha256(
        content=report["content"],
        citation_ledger=report["citation_ledger"],
        provenance=report["provenance"],
        verification=report["verification"],
        metadata={
            "version": report["version"],
            "generated_at": report["generated_at"],
        },
    )


def _externalize_integrity_bound_job(payload: dict, request: Request) -> dict:
    """Project physical KB IDs and rebuild response-local integrity bindings."""

    public = externalize_kb_fields(copy.deepcopy(payload), request)
    for field in ("report", "published_report"):
        artifact = public.get(field)
        if isinstance(artifact, dict):
            _rehash_externalized_artifact(artifact)
    history = public.get("report_history")
    if isinstance(history, list):
        for version in history:
            artifact = version.get("report") if isinstance(version, dict) else None
            if isinstance(artifact, dict):
                _rehash_externalized_artifact(artifact)

    published = public.get("published_report")
    if (
        isinstance(published, dict)
        and published.get("artifact_schema_version") == RESEARCH_ARTIFACT_VERSION
    ):
        publication_sha256 = research_publication_sha256(
            artifact_sha256=published["sha256"],
            report_version=public["report_version"],
            published_at=public["published_at"],
            published_by=public["published_by"],
            review_history=public["review_history"],
            sections=public["sections"],
        )
        public["publication_sha256"] = publication_sha256
        published["publication_sha256"] = publication_sha256
    return public


def _job_with_provenance(row, provenance) -> ResearchJob:
    payload = _safe_public_job_payload(row)
    if provenance is not None:
        payload["provenance_status"] = provenance["status"]
        payload["provenance_stale_reasons"] = provenance["stale_reasons"]
    return ResearchJob.model_validate(payload)


async def _public_job(row, request: Request) -> ResearchJob:
    manager = _manager(request)
    provenance = None
    if manager is not None:
        provenance = await run_sync(
            request.app.state.offload_executor,
            manager.provenance,
            row,
        )
    return ResearchJob.model_validate(
        _externalize_integrity_bound_job(
            _job_with_provenance(row, provenance).model_dump(mode="json"), request
        )
    )


async def _public_jobs(rows, request: Request) -> list[ResearchJob]:
    manager = _manager(request)
    if manager is None:
        return [await _public_job(row, request) for row in rows]
    statuses = await run_sync(
        request.app.state.offload_executor,
        manager.provenance_many,
        rows,
    )
    return [
        ResearchJob.model_validate(
            _externalize_integrity_bound_job(
                _job_with_provenance(row, provenance).model_dump(mode="json"),
                request,
            )
        )
        for row, provenance in zip(rows, statuses)
    ]


def _research_sources(kb_id: str) -> list[str]:
    from cogdoc.service.kb_readers import kb_read_lease
    from cogdoc.service.retriever_factory import RetrieverFactory

    with kb_read_lease(kb_id):
        return RetrieverFactory.get_engine(kb_id).list_sources()


def _json_bundle_file(value) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _published_bundle(job_id: str, row: dict, report: dict) -> bytes:
    if not _verified_published_artifact_matches(row, report):
        raise ValueError("only verified research artifacts can be bundled")
    execution = (report.get("verification") or {}).get("execution")
    committed_job_id = execution.get("job_id") if isinstance(execution, dict) else None
    if (
        type(job_id) is not str
        or job_id != row.get("job_id")
        or job_id != committed_job_id
    ):
        raise ValueError("research bundle job identity does not match the artifact")
    files = {
        "report.md": report["content"].encode("utf-8"),
        "citation-ledger.json": _json_bundle_file(report["citation_ledger"]),
        "provenance.json": _json_bundle_file(report["provenance"]),
        "verification.json": _json_bundle_file(report["verification"]),
    }
    manifest = {
        "schema_version": "research-bundle-v2",
        "artifact_schema_version": RESEARCH_ARTIFACT_VERSION,
        "job_id": job_id,
        "title": str(row.get("title") or ""),
        "report_version": int(report.get("version") or 1),
        "artifact_sha256": str(report.get("sha256") or ""),
        "generated_at": str(report.get("generated_at") or ""),
        "published_at": str(report.get("published_at") or ""),
        "published_by": str(report.get("published_by") or ""),
        "publication_sha256": str(report.get("publication_sha256") or ""),
        "files": {
            name: hashlib.sha256(content).hexdigest()
            for name, content in sorted(files.items())
        },
    }
    files["manifest.json"] = _json_bundle_file(manifest)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, content in sorted(files.items()):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, content)
    return output.getvalue()


@router.post("", status_code=201, response_model=ResearchJobResponse)
async def create_research_job(body: ResearchJobCreate, request: Request):
    scope = resolve_kb_scope(request, body.kb_id)
    if scope is None:
        _observe_lifecycle(
            request,
            action="create",
            outcome="not_found",
            error_class="KnowledgeBaseNotFound",
        )
        return _error(
            ErrorCode.KB_NOT_FOUND,
            "知识库不存在",
            404,
        )
    store = _store(request)
    if store is None:
        _observe_lifecycle(
            request,
            action="create",
            outcome="unavailable",
            error_class="ResearchStoreUnavailable",
        )
        return _error(ErrorCode.INTERNAL_ERROR, "研究任务存储不可用", 503)
    principal = request_principal(request)
    resource_store = getattr(request.app.state, "resource_access_store", None)
    authorization = None
    if resource_store is not None:
        decision = resource_access_decision(request, scope, permission=Permission.QUERY)
        authorization = build_research_authorization(principal, decision)
        if authorization["mode"] == "deny":
            return _error(
                ErrorCode.KB_NOT_FOUND,
                "知识库不存在",
                404,
            )
    elif is_user_session_principal(principal):
        return _error(
            ErrorCode.INTERNAL_ERROR,
            "研究权限服务不可用",
            503,
        )
    create_kwargs = {
        "kb_id": scope.storage_id,
        "objective": body.objective,
        "title": body.title,
        "section_titles": body.section_titles,
        "is_local": body.is_local,
    }
    if authorization is not None and _accepts_keyword(store.create, "authorization"):
        create_kwargs["authorization"] = authorization
    elif authorization is not None:
        return _error(
            ErrorCode.INTERNAL_ERROR,
            "研究任务存储不支持权限快照",
            503,
        )
    row = await run_sync(
        request.app.state.offload_executor,
        store.create,
        **create_kwargs,
    )
    _observe_lifecycle(request, action="create", outcome="succeeded", row=row)
    return ResearchJobResponse(job=await _public_job(row, request))


@router.get("", response_model=ResearchJobListResponse)
async def list_research_jobs(
    request: Request,
    kb_id: str | None = None,
    status: Literal[
        "planned",
        "running",
        "paused",
        "evidence_ready",
        "generating",
        "completed",
        "failed",
        "cancelled",
    ]
    | None = None,
    limit: int = Query(default=100, ge=1, le=500),
):
    store = _store(request)
    if store is None:
        return _error(ErrorCode.INTERNAL_ERROR, "研究任务存储不可用", 503)
    allowed_ids = tenant_storage_ids(request)
    if kb_id is not None:
        scope = resolve_kb_scope(request, kb_id)
        if scope is None:
            return ResearchJobListResponse()
        rows = await run_sync(
            request.app.state.offload_executor,
            store.list,
            kb_id=scope.storage_id,
            status=status,
            limit=limit,
        )
    else:
        rows = []
        for storage_id in allowed_ids:
            rows.extend(
                await run_sync(
                    request.app.state.offload_executor,
                    store.list,
                    kb_id=storage_id,
                    status=status,
                    limit=limit,
                )
            )
        rows.sort(
            key=lambda row: (
                str(row.get("updated_at") or ""),
                str(row.get("job_id") or ""),
            ),
            reverse=True,
        )
        rows = rows[:limit]
    rows = [row for row in rows if _job_is_authorized(request, row)]
    return ResearchJobListResponse(jobs=await _public_jobs(rows, request))


@router.get("/summaries", response_model=ResearchJobSummaryPage)
async def list_research_job_summaries(
    request: Request,
    kb_id: str | None = None,
    status: Literal[
        "planned",
        "running",
        "paused",
        "evidence_ready",
        "generating",
        "completed",
        "failed",
        "cancelled",
    ]
    | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    cursor: str | None = Query(default=None, max_length=1024),
):
    """Return a bounded collection projection without report/evidence bodies."""

    store = _store(request)
    if store is None:
        return _error(ErrorCode.INTERNAL_ERROR, "研究任务存储不可用", 503)
    try:
        decoded = decode_research_summary_cursor(cursor) if cursor else None
    except ResearchSummaryCursorError as exc:
        return _error(ErrorCode.BAD_REQUEST, str(exc), 400)
    allowed_ids = tenant_storage_ids(request)
    storage_kb_id = None
    if kb_id is not None:
        scope = resolve_kb_scope(request, kb_id)
        if scope is None:
            return ResearchJobSummaryPage()
        storage_kb_id = scope.storage_id
    if storage_kb_id is not None:
        query_storage_ids = [storage_kb_id]
    else:
        query_storage_ids = sorted(allowed_ids)
    rows = []
    for query_storage_id in query_storage_ids:
        rows.extend(
            await run_sync(
                request.app.state.offload_executor,
                store.list_summary_rows,
                kb_id=query_storage_id,
                status=status,
                limit=limit + 1,
                before_updated_at=(decoded.updated_at if decoded else None),
                before_job_id=(decoded.job_id if decoded else None),
            )
        )
    rows.sort(
        key=lambda row: (
            str(row.get("updated_at") or ""),
            str(row.get("job_id") or ""),
        ),
        reverse=True,
    )
    rows = rows[: limit + 1]
    authorized_rows = []
    for summary_row in rows:
        full_row = await run_sync(
            request.app.state.offload_executor,
            store.get,
            str(summary_row.get("job_id") or ""),
        )
        if full_row is not None and _job_is_authorized(request, full_row):
            authorized_rows.append(summary_row)
    rows = authorized_rows
    manager = _manager(request)
    provenances = None
    if manager is not None:
        provenances = await run_sync(
            request.app.state.offload_executor,
            manager.provenance_many,
            rows,
        )
    try:
        summaries = project_research_job_summaries(rows, provenances)
        page = ResearchJobSummaryPage.model_validate(
            paginate_research_job_summaries(
                summaries,
                limit=limit,
            )
        )
    except ResearchSummaryProjectionError:
        return _error(
            ErrorCode.INTERNAL_ERROR,
            "研究任务摘要投影失败",
            500,
        )
    payload = externalize_kb_fields(page.model_dump(mode="json"), request)
    etag = research_summary_page_etag(payload)
    headers = {
        "ETag": etag,
        "Cache-Control": "private, no-cache",
        "Vary": "Authorization, X-API-Key",
    }
    if if_none_match_matches(request.headers.get("if-none-match"), etag):
        return Response(status_code=304, headers=headers)
    return JSONResponse(content=payload, headers=headers)


@router.get("/{job_id}", response_model=ResearchJobResponse)
async def get_research_job(job_id: str, request: Request):
    store = _store(request)
    if store is None:
        return _error(ErrorCode.INTERNAL_ERROR, "研究任务存储不可用", 503)
    row = await run_sync(request.app.state.offload_executor, store.get, job_id)
    if row is None or not _job_is_authorized(request, row):
        return _error(
            ErrorCode.RESEARCH_JOB_NOT_FOUND,
            f"研究任务不存在: {job_id}",
            404,
        )
    return ResearchJobResponse(job=await _public_job(row, request))


@router.get("/{job_id}/provenance", response_model=ResearchProvenanceResponse)
async def get_research_provenance(job_id: str, request: Request):
    store = _store(request)
    if store is None:
        return _error(ErrorCode.INTERNAL_ERROR, "研究任务存储不可用", 503)
    current = await run_sync(request.app.state.offload_executor, store.get, job_id)
    if current is None or not _job_is_authorized(request, current):
        return _error(
            ErrorCode.RESEARCH_JOB_NOT_FOUND,
            f"研究任务不存在: {job_id}",
            404,
        )
    manager = _manager(request)
    if manager is None:
        return _error(ErrorCode.INTERNAL_ERROR, "研究执行器不可用", 503)
    try:
        status = await run_sync(
            request.app.state.offload_executor,
            manager.provenance,
            job_id,
        )
    except KeyError:
        return _error(
            ErrorCode.RESEARCH_JOB_NOT_FOUND,
            f"研究任务不存在: {job_id}",
            404,
        )
    if not _provenance_matches_job_scope(status, current):
        return _error(
            ErrorCode.INTERNAL_ERROR,
            "研究来源快照作用域校验失败",
            500,
        )
    return ResearchProvenanceResponse.model_validate(
        externalize_kb_fields(
            ResearchProvenanceResponse(
                job_id=job_id,
                status=status["status"],
                stale_reasons=status["stale_reasons"],
                captured=status["captured"] or None,
                current=status["current"] or None,
            ).model_dump(mode="json"),
            request,
        )
    )


async def _execution_action(
    job_id: str,
    request: Request,
    action: str,
    *,
    lifecycle_action: str | None = None,
):
    observed_action = lifecycle_action or action
    store = _store(request)
    if store is None:
        return _error(ErrorCode.INTERNAL_ERROR, "研究任务存储不可用", 503)
    current = await run_sync(request.app.state.offload_executor, store.get, job_id)
    if current is None or not _job_is_authorized(request, current):
        return _error(
            ErrorCode.RESEARCH_JOB_NOT_FOUND,
            f"研究任务不存在: {job_id}",
            404,
        )
    manager = _manager(request)
    if manager is None:
        _observe_lifecycle(
            request,
            action=observed_action,
            outcome="unavailable",
            job_id=job_id,
            error_class="ResearchExecutionUnavailable",
        )
        return _error(ErrorCode.INTERNAL_ERROR, "研究执行器不可用", 503)
    operation = getattr(manager, action)
    try:
        row = await run_sync(
            request.app.state.offload_executor,
            operation,
            job_id,
        )
    except KeyError:
        _observe_lifecycle(
            request,
            action=observed_action,
            outcome="not_found",
            job_id=job_id,
            error_class="ResearchJobNotFound",
        )
        return _error(
            ErrorCode.RESEARCH_JOB_NOT_FOUND,
            f"研究任务不存在: {job_id}",
            404,
        )
    except ResearchEvidenceStaleError as exc:
        _observe_lifecycle(
            request,
            action=observed_action,
            outcome="stale",
            job_id=job_id,
            error_class=type(exc).__name__,
        )
        return _error(ErrorCode.RESEARCH_EVIDENCE_STALE, str(exc), 409)
    except ResearchExecutionCapacityError as exc:
        _observe_lifecycle(
            request,
            action=observed_action,
            outcome="unavailable",
            job_id=job_id,
            error_class=type(exc).__name__,
        )
        response = _error(ErrorCode.RESEARCH_CAPACITY_EXHAUSTED, str(exc), 503)
        response.headers["Retry-After"] = "1"
        return response
    except ResearchJobStateConflictError as exc:
        _observe_lifecycle(
            request,
            action=observed_action,
            outcome="conflict",
            job_id=job_id,
            error_class=type(exc).__name__,
        )
        return _error(ErrorCode.RESEARCH_JOB_STATE_CONFLICT, str(exc), 409)
    if not _job_is_authorized(request, row):
        return _error(
            ErrorCode.RESEARCH_JOB_NOT_FOUND,
            f"研究任务不存在: {job_id}",
            404,
        )
    _observe_lifecycle(
        request,
        action=observed_action,
        outcome=(
            "accepted"
            if observed_action in {"start", "resume", "refresh", "generate"}
            else "succeeded"
        ),
        row=row,
    )
    return ResearchJobResponse(job=await _public_job(row, request))


@router.post("/{job_id}/start", status_code=202, response_model=ResearchJobResponse)
async def start_research_job(job_id: str, request: Request):
    return await _execution_action(job_id, request, "start")


@router.post("/{job_id}/resume", status_code=202, response_model=ResearchJobResponse)
async def resume_research_job(job_id: str, request: Request):
    return await _execution_action(job_id, request, "resume")


@router.post("/{job_id}/pause", response_model=ResearchJobResponse)
async def pause_research_job(job_id: str, request: Request):
    return await _execution_action(job_id, request, "pause")


@router.post("/{job_id}/cancel", response_model=ResearchJobResponse)
async def cancel_research_job(job_id: str, request: Request):
    return await _execution_action(job_id, request, "cancel")


@router.post("/{job_id}/refresh", status_code=202, response_model=ResearchJobResponse)
async def refresh_research_job(job_id: str, request: Request):
    return await _execution_action(job_id, request, "refresh")


@router.post(
    "/{job_id}/generate",
    status_code=202,
    response_model=ResearchJobResponse,
)
async def generate_research_report(job_id: str, request: Request):
    return await _execution_action(
        job_id,
        request,
        "compile",
        lifecycle_action="generate",
    )


@router.get("/{job_id}/report")
async def download_research_report(job_id: str, request: Request):
    store = _store(request)
    if store is None:
        return _error(ErrorCode.INTERNAL_ERROR, "研究任务存储不可用", 503)
    row = await run_sync(request.app.state.offload_executor, store.get, job_id)
    if row is None or not _job_is_authorized(request, row):
        return _error(
            ErrorCode.RESEARCH_JOB_NOT_FOUND,
            f"研究任务不存在: {job_id}",
            404,
        )
    report = row.get("report")
    if (
        not isinstance(report, dict)
        or type(report.get("content")) is not str
        or not report["content"]
    ):
        return _error(
            ErrorCode.RESEARCH_JOB_STATE_CONFLICT,
            "研究报告尚未生成",
            409,
        )
    if not _verified_artifact_matches_sections(row, report):
        return _error(
            ErrorCode.RESEARCH_JOB_STATE_CONFLICT,
            "研究报告完整性校验失败",
            409,
        )
    return Response(
        content=report["content"],
        media_type="text/markdown; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{job_id}.md"',
            "X-CogDoc-Integrity": "verified",
        },
    )


@router.put("/{job_id}/review", response_model=ResearchJobResponse)
async def review_research_report(
    job_id: str,
    body: ResearchReportReviewUpdate,
    request: Request,
):
    store = _store(request)
    if store is None:
        return _error(ErrorCode.INTERNAL_ERROR, "研究任务存储不可用", 503)
    current = await run_sync(request.app.state.offload_executor, store.get, job_id)
    if current is None or not _job_is_authorized(request, current):
        return _error(
            ErrorCode.RESEARCH_JOB_NOT_FOUND,
            f"研究任务不存在: {job_id}",
            404,
        )
    # Resource existence/ownership is intentionally resolved before the
    # independent reviewer credential so opaque cross-tenant IDs always look
    # absent rather than becoming an authorization oracle.
    _reviewer = await require_eval_reviewer(request)
    manager = _manager(request)
    try:
        if manager is not None:
            row = await run_sync(
                request.app.state.offload_executor,
                manager.review_report,
                job_id,
                decisions=[decision.model_dump() for decision in body.decisions],
                expected_revision=body.expected_revision,
                reviewer_actor=_reviewer,
            )
        else:
            row = await run_sync(
                request.app.state.offload_executor,
                store.review_report,
                job_id,
                decisions=[decision.model_dump() for decision in body.decisions],
                expected_revision=body.expected_revision,
                reviewer_actor=_reviewer,
            )
    except KeyError:
        return _error(
            ErrorCode.RESEARCH_JOB_NOT_FOUND,
            f"研究任务不存在: {job_id}",
            404,
        )
    except ResearchJobRevisionConflictError as exc:
        return _error(ErrorCode.RESEARCH_JOB_REVISION_CONFLICT, str(exc), 409)
    except ResearchEvidenceStaleError as exc:
        return _error(ErrorCode.RESEARCH_EVIDENCE_STALE, str(exc), 409)
    except ResearchJobStateConflictError as exc:
        return _error(ErrorCode.RESEARCH_JOB_STATE_CONFLICT, str(exc), 409)
    except ValueError as exc:
        return _error(ErrorCode.BAD_REQUEST, str(exc), 422)
    if not _job_is_authorized(request, row):
        return _error(
            ErrorCode.RESEARCH_JOB_NOT_FOUND,
            f"研究任务不存在: {job_id}",
            404,
        )
    return ResearchJobResponse(job=await _public_job(row, request))


@router.post("/{job_id}/publish", response_model=ResearchJobResponse)
async def publish_research_report(
    job_id: str,
    body: ResearchReportPublishRequest,
    request: Request,
):
    store = _store(request)
    if store is None:
        return _error(ErrorCode.INTERNAL_ERROR, "研究任务存储不可用", 503)
    current = await run_sync(request.app.state.offload_executor, store.get, job_id)
    if current is None or not _job_is_authorized(request, current):
        return _error(
            ErrorCode.RESEARCH_JOB_NOT_FOUND,
            f"研究任务不存在: {job_id}",
            404,
        )
    _reviewer = await require_eval_reviewer(request)
    manager = _manager(request)
    try:
        if manager is not None:
            row = await run_sync(
                request.app.state.offload_executor,
                manager.publish_report,
                job_id,
                expected_revision=body.expected_revision,
                publisher_actor=_reviewer,
            )
        else:
            row = await run_sync(
                request.app.state.offload_executor,
                store.publish_report,
                job_id,
                expected_revision=body.expected_revision,
                publisher_actor=_reviewer,
            )
    except KeyError:
        return _error(
            ErrorCode.RESEARCH_JOB_NOT_FOUND,
            f"研究任务不存在: {job_id}",
            404,
        )
    except ResearchJobRevisionConflictError as exc:
        return _error(ErrorCode.RESEARCH_JOB_REVISION_CONFLICT, str(exc), 409)
    except ResearchEvidenceStaleError as exc:
        return _error(ErrorCode.RESEARCH_EVIDENCE_STALE, str(exc), 409)
    except ResearchJobStateConflictError as exc:
        return _error(ErrorCode.RESEARCH_JOB_STATE_CONFLICT, str(exc), 409)
    if not _job_is_authorized(request, row):
        return _error(
            ErrorCode.RESEARCH_JOB_NOT_FOUND,
            f"研究任务不存在: {job_id}",
            404,
        )
    return ResearchJobResponse(job=await _public_job(row, request))


@router.get("/{job_id}/published-report")
async def download_published_research_report(job_id: str, request: Request):
    store = _store(request)
    if store is None:
        return _error(ErrorCode.INTERNAL_ERROR, "研究任务存储不可用", 503)
    row = await run_sync(request.app.state.offload_executor, store.get, job_id)
    if row is None or not _job_is_authorized(request, row):
        return _error(
            ErrorCode.RESEARCH_JOB_NOT_FOUND,
            f"研究任务不存在: {job_id}",
            404,
        )
    report = row.get("published_report")
    if not _published_state_matches(row, report):
        return _error(
            ErrorCode.RESEARCH_JOB_STATE_CONFLICT,
            "研究报告尚未发布",
            409,
        )
    if (
        not isinstance(report, dict)
        or type(report.get("content")) is not str
        or not report["content"]
    ):
        return _error(
            ErrorCode.RESEARCH_JOB_STATE_CONFLICT,
            "研究报告尚未发布",
            409,
        )
    integrity = _artifact_status(report)
    if (
        integrity == "legacy-unverified"
        and row.get("artifact_schema_floor") == RESEARCH_ARTIFACT_VERSION
    ):
        integrity = "invalid"
    if integrity == "verified" and not _verified_published_artifact_matches(
        row, report
    ):
        integrity = "invalid"
    if integrity == "invalid":
        return _error(
            ErrorCode.RESEARCH_JOB_STATE_CONFLICT,
            "已发布研究报告完整性校验失败",
            409,
        )
    return Response(
        content=report["content"],
        media_type="text/markdown; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{job_id}-published.md"',
            "X-CogDoc-Integrity": integrity,
        },
    )


@router.get("/{job_id}/published-bundle")
async def download_published_research_bundle(job_id: str, request: Request):
    store = _store(request)
    if store is None:
        return _error(ErrorCode.INTERNAL_ERROR, "研究任务存储不可用", 503)
    row = await run_sync(request.app.state.offload_executor, store.get, job_id)
    if row is None or not _job_is_authorized(request, row):
        return _error(
            ErrorCode.RESEARCH_JOB_NOT_FOUND,
            f"研究任务不存在: {job_id}",
            404,
        )
    report = row.get("published_report")
    if not _published_state_matches(row, report):
        return _error(
            ErrorCode.RESEARCH_JOB_STATE_CONFLICT,
            "研究报告尚未发布",
            409,
        )
    if (
        not isinstance(report, dict)
        or type(report.get("content")) is not str
        or not report["content"]
    ):
        return _error(
            ErrorCode.RESEARCH_JOB_STATE_CONFLICT,
            "研究报告尚未发布",
            409,
        )
    integrity = _artifact_status(report)
    if integrity == "verified" and not _verified_published_artifact_matches(
        row, report
    ):
        integrity = "invalid"
    if integrity != "verified":
        message = (
            "旧版研究报告未包含完整性元数据，无法生成验证包"
            if integrity == "legacy-unverified"
            else "已发布研究报告完整性校验失败"
        )
        return _error(
            ErrorCode.RESEARCH_JOB_STATE_CONFLICT,
            message,
            409,
        )
    public_row = _externalize_integrity_bound_job(row, request)
    public_report = public_row.get("published_report")
    if not isinstance(public_report, dict):
        return _error(
            ErrorCode.RESEARCH_JOB_STATE_CONFLICT,
            "已发布研究报告公开投影失败",
            409,
        )
    bundle = await run_sync(
        request.app.state.offload_executor,
        _published_bundle,
        job_id,
        public_row,
        public_report,
    )
    return Response(
        content=bundle,
        media_type="application/zip",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{job_id}-published-bundle.zip"'
            ),
            "X-CogDoc-Integrity": "verified",
        },
    )


@router.put("/{job_id}/plan", response_model=ResearchJobResponse)
async def update_research_plan(
    job_id: str,
    body: ResearchPlanUpdate,
    request: Request,
):
    store = _store(request)
    if store is None:
        return _error(ErrorCode.INTERNAL_ERROR, "研究任务存储不可用", 503)
    current = await run_sync(request.app.state.offload_executor, store.get, job_id)
    if current is None or not _job_is_authorized(request, current):
        return _error(
            ErrorCode.RESEARCH_JOB_NOT_FOUND,
            f"研究任务不存在: {job_id}",
            404,
        )
    try:
        row = await run_sync(
            request.app.state.offload_executor,
            store.update_plan,
            job_id,
            sections=[section.model_dump() for section in body.sections],
            expected_revision=body.expected_revision,
        )
    except KeyError:
        return _error(
            ErrorCode.RESEARCH_JOB_NOT_FOUND,
            f"研究任务不存在: {job_id}",
            404,
        )
    except ResearchJobRevisionConflictError as exc:
        return _error(ErrorCode.RESEARCH_JOB_REVISION_CONFLICT, str(exc), 409)
    except ResearchJobStateConflictError as exc:
        return _error(ErrorCode.RESEARCH_JOB_STATE_CONFLICT, str(exc), 409)
    except ValueError as exc:
        return _error(ErrorCode.BAD_REQUEST, str(exc), 422)
    if not _job_is_authorized(request, row):
        return _error(
            ErrorCode.RESEARCH_JOB_NOT_FOUND,
            f"研究任务不存在: {job_id}",
            404,
        )
    return ResearchJobResponse(job=await _public_job(row, request))


@router.post("/{job_id}/plan/auto", response_model=ResearchJobResponse)
async def generate_research_plan(
    job_id: str,
    body: ResearchPlanGenerateRequest,
    request: Request,
):
    store = _store(request)
    if store is None:
        return _error(ErrorCode.INTERNAL_ERROR, "研究任务存储不可用", 503)
    current = await run_sync(request.app.state.offload_executor, store.get, job_id)
    if current is None or not _job_is_authorized(request, current):
        return _error(
            ErrorCode.RESEARCH_JOB_NOT_FOUND,
            f"研究任务不存在: {job_id}",
            404,
        )
    actual_revision = int(current.get("revision") or 0)
    if actual_revision != body.expected_revision:
        return _error(
            ErrorCode.RESEARCH_JOB_REVISION_CONFLICT,
            "research job revision conflict: "
            f"expected {body.expected_revision}, found {actual_revision}",
            409,
        )
    if current.get("status") != "planned":
        return _error(
            ErrorCode.RESEARCH_JOB_STATE_CONFLICT,
            "research plan can only be edited before execution starts",
            409,
        )
    generator = getattr(request.app.state, "research_plan_generator", None)
    if generator is None:
        return _error(ErrorCode.MODEL_UNAVAILABLE, "研究规划器不可用", 503)
    source_reader = getattr(
        request.app.state, "research_source_reader", _research_sources
    )
    execution_is_local = (
        bool(current.get("is_local", False)) if body.is_local is None else body.is_local
    )
    planning_seconds = get_settings().cogdoc_research_planning_deadline_seconds
    planning_deadline_monotonic = time.monotonic() + planning_seconds
    planning_deadline_at = (
        datetime.now(timezone.utc) + timedelta(seconds=planning_seconds)
    ).isoformat()
    planning_stop = Event()
    planning_executor = getattr(
        request.app.state,
        "research_planning_executor",
        request.app.state.offload_executor,
    )
    register_planning = getattr(planning_executor, "register", None)
    try:
        registration = (
            register_planning(planning_stop)
            if callable(register_planning)
            else nullcontext()
        )
        with registration:
            sections = await run_sync_until(
                planning_executor,
                _run_auto_plan,
                generator,
                source_reader,
                kb_id=str(current.get("kb_id") or ""),
                objective=str(current.get("objective") or ""),
                is_local=execution_is_local,
                deadline_at=planning_deadline_at,
                stop_event=planning_stop,
                retrieval_scope=research_retrieval_scope(current),
                deadline_monotonic=planning_deadline_monotonic,
                on_cancel=planning_stop.set,
                on_timeout=planning_stop.set,
            )
    except DaemonExecutorCapacityError:
        response = _error(
            ErrorCode.RESEARCH_CAPACITY_EXHAUSTED,
            "智能研究规划队列已满，请稍后重试",
            503,
        )
        response.headers["Retry-After"] = "1"
        return response
    except (ResearchDeadlineExceeded, TimeoutError):
        return _error(
            ErrorCode.MODEL_UNAVAILABLE,
            "智能研究规划超时，请重试",
            503,
        )
    except ResearchCancelled:
        return _error(
            ErrorCode.MODEL_UNAVAILABLE,
            "智能研究规划已取消，请重试",
            503,
        )
    except Exception:
        return _error(ErrorCode.MODEL_UNAVAILABLE, "智能研究规划失败，请重试", 503)
    try:
        row = await run_sync(
            request.app.state.offload_executor,
            _commit_auto_plan_if_authorized,
            request,
            store,
            current,
            job_id,
            sections=sections,
            expected_revision=body.expected_revision,
            is_local=execution_is_local,
        )
    except ResearchJobRevisionConflictError as exc:
        return _error(ErrorCode.RESEARCH_JOB_REVISION_CONFLICT, str(exc), 409)
    except ResearchJobStateConflictError as exc:
        return _error(ErrorCode.RESEARCH_JOB_STATE_CONFLICT, str(exc), 409)
    except ValueError as exc:
        return _error(ErrorCode.BAD_REQUEST, str(exc), 422)
    if row is None:
        return _error(
            ErrorCode.RESEARCH_JOB_NOT_FOUND,
            f"研究任务不存在: {job_id}",
            404,
        )
    if not _job_is_authorized(request, row):
        return _error(
            ErrorCode.RESEARCH_JOB_NOT_FOUND,
            f"研究任务不存在: {job_id}",
            404,
        )
    return ResearchJobResponse(job=await _public_job(row, request))
