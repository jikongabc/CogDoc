from __future__ import annotations

import math
import hashlib
import inspect
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import CancelledError, Future
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import asdict, dataclass, is_dataclass
from functools import partial
from threading import Event, RLock
from typing import Any

from cogdoc.api.research_job_store import (
    ResearchJobStateConflictError,
    ResearchJobStore,
    research_run_control,
)
from cogdoc.api.research_access import (
    research_authorization,
    research_retrieval_scope,
)
from cogdoc.config.settings import get_settings
from cogdoc.daemon_executor import (
    DaemonExecutorCapacityError,
    DaemonFutureExecutor,
)
from cogdoc.research_control import (
    ResearchBudgetExceeded,
    ResearchCancelled,
    ResearchControlSignal,
    ResearchDeadlineExceeded,
    ResearchPaused,
    ResearchProviderCapacityExceeded,
    ResearchProviderError,
    ResearchProviderTimeout,
    ResearchRunController,
    bind_research_control,
)
from cogdoc.research_provider import (
    is_process_isolated_provider_call,
)
from cogdoc.research_isolation import run_spawn_isolated_provider
from cogdoc.service.kb_readers import kb_read_lease
from cogdoc.service.retrieval_pipeline import (
    build_retrieval_queries,
    retrieve_candidate_pool,
)
from cogdoc.service.research_provenance import (
    capture_research_provenance,
    is_trackable_research_provenance,
    research_provenance_status,
)
from cogdoc.service.research_observability import ResearchObserver
from cogdoc.service.retriever_factory import RetrieverFactory
from cogdoc.tools.reranker import BGEReranker, skipped_cpu_rerank_docs


ResearchRetriever = Callable[[str, str], Sequence[Mapping[str, Any]]]
ResearchReportBuilder = Callable[[Mapping[str, Any]], Any]


@dataclass(slots=True)
class _ResearchRunHandle:
    active_key: str
    job_id: str
    phase: str
    attempt_id: str
    lease_id: str
    future: Future
    control: ResearchRunController
    started_monotonic: float


class ResearchEvidenceStaleError(ResearchJobStateConflictError):
    def __init__(self, reasons: Sequence[str]):
        self.reasons = tuple(str(reason) for reason in reasons if str(reason))
        super().__init__(
            "research evidence provenance is stale: "
            + ", ".join(self.reasons or ("unknown",))
        )


class ResearchExecutionCapacityError(ResearchJobStateConflictError):
    """The bounded background admission queue has no free slot."""


def retrieve_research_evidence(
    kb_id: str,
    query: str,
    *,
    state_runtime,
    top_k: int = 8,
    retrieval_scope=None,
) -> list[Mapping[str, Any]]:
    """Reuse the production hybrid retrieval path for one research section."""

    settings = get_settings()
    with kb_read_lease(kb_id):
        engine = RetrieverFactory.get_engine(kb_id)
        result = retrieve_candidate_pool(
            engine,
            state_runtime.derived_knowledge_retriever,
            state_runtime.retrieval_feedback_store,
            kb_id=kb_id,
            original_query=query,
            queries=build_retrieval_queries(query, max_queries=1),
            top_k=top_k,
            rrf_k=float(settings.hybrid_rrf_k),
            scope=retrieval_scope,
        )
        docs = list(result.docs)
        if not docs:
            return []
        target_device = BGEReranker.default_device()
        if target_device == "cpu" and not settings.qa_rerank_on_cpu:
            return skipped_cpu_rerank_docs(docs, min(top_k, len(docs)))
        return BGEReranker.rerank(
            query,
            docs,
            top_n=min(top_k, len(docs)),
            device=target_device,
        )


def _safe_number(value: Any) -> int | float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def public_research_evidence(
    docs: Sequence[Mapping[str, Any]],
    *,
    limit: int = 5,
    preview_chars: int = 480,
) -> list[dict[str, Any]]:
    """Persist only bounded public evidence coordinates, never full chunk text."""

    evidence: list[dict[str, Any]] = []
    seen: set[tuple[str, int, int, str]] = set()
    for doc in docs:
        meta = doc.get("meta") if isinstance(doc.get("meta"), Mapping) else {}
        retrieval = (
            doc.get("retrieval")
            if isinstance(doc.get("retrieval"), Mapping)
            else {}
        )
        chunk_id = str(meta.get("chunk_id") or "")
        knowledge_id = str(meta.get("knowledge_id") or "")
        page = meta.get("page")
        raw_text = str(doc.get("text") or "")
        visible_text = raw_text.strip()
        leading_trim = len(raw_text) - len(raw_text.lstrip())
        raw_span_start = retrieval.get("evidence_text_start")
        span_start = (
            raw_span_start
            if type(raw_span_start) is int and raw_span_start >= 0
            else 0
        ) + leading_trim
        span_end = span_start + len(visible_text)
        identity = (chunk_id, span_start, span_end, knowledge_id)
        if not chunk_id and not knowledge_id:
            continue
        if identity in seen:
            continue
        seen.add(identity)
        normalized_text = " ".join(raw_text.split())
        item = {
            "chunk_id": chunk_id,
            "source_type": str(meta.get("source_type") or "document"),
            "knowledge_id": knowledge_id,
            "source": str(meta.get("source") or ""),
            "source_sha256": str(meta.get("source_sha256") or ""),
            "text_hash": hashlib.sha256(normalized_text.encode("utf-8")).hexdigest(),
            "page": page,
            # Do not synthesize range metadata that was absent from the exact
            # document view.  Citation binding treats supplied coordinates as
            # commitments, so a page fallback here would conflict with a ledger
            # that correctly omitted page_start/page_end.
            "page_start": meta.get("page_start"),
            "page_end": meta.get("page_end"),
            "span_start": span_start,
            "span_end": span_end,
            "section_title": str(meta.get("section_title") or ""),
            "text_preview": " ".join(str(doc.get("text") or "").split())[
                :preview_chars
            ],
            "search_channel": str(retrieval.get("search_channel") or ""),
            "rerank_score": _safe_number(retrieval.get("rerank_score")),
            "rrf_score": _safe_number(retrieval.get("rrf_score")),
        }
        evidence.append(item)
        if len(evidence) >= limit:
            break
    return evidence


def research_section_queries(
    section: Mapping[str, Any],
) -> list[tuple[str, str]]:
    """Return stable atomic preview queries for one research section."""

    rows = section.get("evidence_requirements")
    queries: list[tuple[str, str]] = []
    seen: set[str] = set()
    if isinstance(rows, Sequence) and not isinstance(rows, (str, bytes, bytearray)):
        for position, raw in enumerate(rows, start=1):
            if not isinstance(raw, Mapping):
                continue
            query = " ".join(
                str(raw.get("retrieval_query") or raw.get("question") or "").split()
            )
            key = query.casefold()
            if not query or key in seen:
                continue
            seen.add(key)
            requirement_id = str(
                raw.get("requirement_id")
                or f"{section.get('section_id') or 'section'}:r{position}"
            )
            queries.append((requirement_id, query))
    if queries:
        return queries
    fallback = " ".join(str(section.get("research_question") or "").split())
    return [(str(section.get("section_id") or "section"), fallback)] if fallback else []


class ResearchExecutionManager:
    """Durable section-at-a-time research evidence executor."""

    def __init__(
        self,
        store: ResearchJobStore,
        *,
        retrieve: ResearchRetriever,
        kb_exists: Callable[[str], bool],
        report_builder: ResearchReportBuilder | None = None,
        provenance_reader: Callable[[str], Mapping[str, Any]] | None = None,
        max_workers: int = 2,
        max_pending: int | None = None,
        provider_workers: int | None = None,
        provider_max_pending: int | None = None,
        retrieval_doc_reservation: int | None = None,
        observer: ResearchObserver | None = None,
        authorization_checker: Callable[[Mapping[str, Any]], bool] | None = None,
    ):
        self._store = store
        self._retrieve = retrieve
        self._kb_exists = kb_exists
        self._report_builder = report_builder
        self._provenance_reader = provenance_reader
        self._lock = RLock()
        self._active: dict[str, _ResearchRunHandle] = {}
        settings = get_settings()
        self._max_pending = int(
            max_pending
            if max_pending is not None
            else settings.cogdoc_research_max_pending
        )
        if self._max_pending < 1:
            raise ValueError("research max_pending must be positive")
        self._executor = DaemonFutureExecutor(
            max_workers=max(1, max_workers),
            max_pending=self._max_pending,
            thread_name_prefix="cogdoc-research",
        )
        # Reservations cover the durable-transition window before a WorkItem
        # reaches the executor. Physical running/queued occupancy (including a
        # cancelled Future whose queue tombstone has not yet been drained) is
        # read from the executor itself.
        self._submission_reservations = 0
        self._provider_worker_limit = int(
            provider_workers
            if provider_workers is not None
            else settings.cogdoc_research_provider_workers
        )
        self._provider_max_pending = int(
            provider_max_pending
            if provider_max_pending is not None
            else settings.cogdoc_research_provider_max_pending
        )
        if self._provider_max_pending < 1:
            raise ValueError("research provider_max_pending must be positive")
        self._provider_executor = DaemonFutureExecutor(
            max_workers=self._provider_worker_limit,
            max_pending=self._provider_max_pending,
            thread_name_prefix="cogdoc-research-provider",
        )
        self._provider_calls_in_use = 0
        self._provider_processes_in_use = 0
        self._provider_kill_grace_seconds = float(
            settings.cogdoc_research_provider_kill_grace_seconds
        )
        self._provider_ipc_max_bytes = int(
            settings.cogdoc_research_provider_ipc_max_bytes
        )
        self._retrieval_doc_reservation = max(
            1,
            int(
                retrieval_doc_reservation
                if retrieval_doc_reservation is not None
                else settings.cogdoc_research_retrieval_top_k
            ),
        )
        self._closed = False
        self._observer = observer
        self._authorization_checker = authorization_checker

    @classmethod
    def from_runtime(
        cls,
        store: ResearchJobStore,
        *,
        state_runtime,
        kb_exists: Callable[[str], bool],
        max_workers: int = 2,
        top_k: int = 8,
        max_pending: int | None = None,
        observer: ResearchObserver | None = None,
        authorization_checker: Callable[[Mapping[str, Any]], bool] | None = None,
    ) -> "ResearchExecutionManager":
        # Local import avoids coupling the evidence executor to report-generation
        # dependencies during module import.
        from cogdoc.service.research_report import ResearchReportBuilder as Builder

        return cls(
            store,
            retrieve=partial(
                retrieve_research_evidence,
                state_runtime=state_runtime,
                top_k=top_k,
            ),
            kb_exists=kb_exists,
            report_builder=lambda job: Builder.from_runtime(
                state_runtime=state_runtime,
                is_local=bool(job.get("is_local", False)),
            )(job),
            provenance_reader=partial(
                capture_research_provenance,
                state_runtime=state_runtime,
            ),
            max_workers=max_workers,
            max_pending=max_pending,
            retrieval_doc_reservation=top_k,
            observer=observer,
            authorization_checker=authorization_checker,
        )

    def bind_observer(self, observer: ResearchObserver | None) -> None:
        self._observer = observer

    def bind_authorization_checker(
        self, checker: Callable[[Mapping[str, Any]], bool] | None
    ) -> None:
        self._authorization_checker = checker

    def _assert_authorized(self, job: Mapping[str, Any]) -> None:
        authorization = research_authorization(job)
        if authorization is None:
            return
        checker = self._authorization_checker
        if checker is None or not checker(job):
            raise ResearchJobStateConflictError(
                "research authorization is stale or unavailable"
            )
        scope = research_retrieval_scope(job)
        if scope is None or scope.denies_all:
            raise ResearchJobStateConflictError("research authorization denies access")

    def _retrieve_for_job(
        self,
        job: Mapping[str, Any],
        kb_id: str,
        query: str,
    ) -> list[Mapping[str, Any]]:
        self._assert_authorized(job)
        scope = research_retrieval_scope(job)
        if scope is None:
            return list(self._retrieve(kb_id, query))
        try:
            parameters = inspect.signature(self._retrieve).parameters.values()
            accepts_scope = any(
                parameter.name == "retrieval_scope"
                or parameter.kind is inspect.Parameter.VAR_KEYWORD
                for parameter in parameters
            )
        except (TypeError, ValueError):
            accepts_scope = False
        if accepts_scope:
            docs = self._retrieve(kb_id, query, retrieval_scope=scope)
        elif scope.allows_all_sources:
            docs = self._retrieve(kb_id, query)
        else:
            raise ResearchJobStateConflictError(
                "research retriever cannot enforce the authorization subset"
            )
        # Final guard protects persisted evidence from a backend that accepted
        # but ignored the pre-top-k scope.
        return [doc for doc in docs if scope.allows_document(doc)]

    def _observe(self, method: str, **fields: Any) -> None:
        observer = self._observer
        operation = getattr(observer, method, None) if observer is not None else None
        if not callable(operation):
            return
        try:
            operation(**fields)
        except Exception:
            # Operational telemetry must never affect durable execution.
            return

    def reconcile_orphans(self) -> int:
        detailed_reconcile = getattr(
            self._store, "reconcile_running_outcomes", None
        )
        if callable(detailed_reconcile):
            outcomes = dict(detailed_reconcile())
            count = sum(max(0, int(value)) for value in outcomes.values())
        else:
            count = self._store.reconcile_running()
            outcomes = {"service_restarted": count}
        self._observe(
            "orphan_reconciled",
            count=count,
            termination_counts=outcomes,
        )
        return count

    def _reserve_submission(self) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeError("ResearchExecutionManager is closed")
            occupied = (
                self._executor.pending_count() + self._submission_reservations
            )
            if occupied >= self._max_pending:
                raise ResearchExecutionCapacityError(
                    "research execution queue is full"
                )
            self._submission_reservations += 1

    def _release_submission(self) -> None:
        with self._lock:
            if self._submission_reservations:
                self._submission_reservations -= 1

    def _provider_finished(self, _future: Future) -> None:
        with self._lock:
            if self._provider_calls_in_use:
                self._provider_calls_in_use -= 1

    def _acquire_provider_process_slot(
        self,
        control: ResearchRunController,
        *,
        expires: float,
    ) -> None:
        while True:
            with self._lock:
                if self._closed:
                    control.request_stop("shutdown")
                elif self._provider_processes_in_use < self._provider_worker_limit:
                    self._provider_processes_in_use += 1
                    return
            control.poll()
            if time.monotonic() >= expires:
                control.poll()
                raise ResearchProviderTimeout(
                    "research provider process slot wait timed out"
                )
            time.sleep(min(0.05, max(expires - time.monotonic(), 0.0)))

    def _release_provider_process_slot(self) -> None:
        with self._lock:
            if self._provider_processes_in_use:
                self._provider_processes_in_use -= 1

    def _run_process_provider_call(
        self,
        control: ResearchRunController,
        provider: str,
        operation: Callable[[], Any],
        timeout_seconds: float,
    ) -> Any:
        try:
            return run_spawn_isolated_provider(
                operation,
                provider=provider,
                timeout_seconds=timeout_seconds,
                kill_grace_seconds=self._provider_kill_grace_seconds,
                ipc_max_bytes=self._provider_ipc_max_bytes,
                poll=control.poll_local,
            )
        except ResearchProviderTimeout:
            # Persist the stronger phase deadline if it won the same race;
            # otherwise preserve the narrower provider-timeout classification.
            control.poll()
            raise

    def _run_provider_call(
        self,
        control: ResearchRunController,
        provider: str,
        operation: Callable[[], Any],
        timeout_seconds: float,
        on_admitted: Callable[[], None],
    ) -> Any:
        started = time.monotonic()
        isolation = (
            "process"
            if is_process_isolated_provider_call(operation)
            else "compatibility"
        )
        outcome = "failed"
        error_class = ""
        try:
            result = self._execute_provider_call(
                control,
                provider,
                operation,
                timeout_seconds,
                on_admitted,
            )
            outcome = "succeeded"
            return result
        except (ResearchProviderTimeout, ResearchDeadlineExceeded) as exc:
            outcome = "timeout"
            error_class = type(exc).__name__
            raise
        except (ResearchProviderCapacityExceeded, ResearchBudgetExceeded) as exc:
            outcome = "capacity"
            error_class = type(exc).__name__
            raise
        except ResearchControlSignal as exc:
            outcome = "cancelled"
            error_class = type(exc).__name__
            raise
        except BaseException as exc:
            error_class = type(exc).__name__
            raise
        finally:
            self._observe(
                "provider_call",
                provider=provider,
                isolation=isolation,
                outcome=outcome,
                job_id=control.job_id,
                execution_id=control.attempt_id,
                stage=control.phase,
                duration_ms=(time.monotonic() - started) * 1000.0,
                error_class=error_class,
            )

    def _execute_provider_call(
        self,
        control: ResearchRunController,
        provider: str,
        operation: Callable[[], Any],
        timeout_seconds: float,
        on_admitted: Callable[[], None],
    ) -> Any:
        """Poll one isolated provider call against the durable run lease."""

        control.poll_local()
        with self._lock:
            closed = self._closed
            if not closed and self._provider_calls_in_use >= self._provider_max_pending:
                raise ResearchProviderCapacityExceeded(
                    "research provider isolation queue is full"
                )
            if not closed:
                self._provider_calls_in_use += 1
        if closed:
            control.request_stop("shutdown")
            control.checkpoint()
            raise ResearchProviderError("research provider executor is closed")

        if is_process_isolated_provider_call(operation):
            expires = time.monotonic() + float(timeout_seconds)
            process_slot_acquired = False
            try:
                self._acquire_provider_process_slot(control, expires=expires)
                process_slot_acquired = True
                remaining = expires - time.monotonic()
                if remaining <= 0:
                    control.poll()
                    raise ResearchProviderTimeout(
                        "research provider process slot wait timed out"
                    )
                on_admitted()
                control.poll_local()
                remaining = expires - time.monotonic()
                if remaining <= 0:
                    control.poll()
                    raise ResearchProviderTimeout(
                        "research provider process slot wait timed out"
                    )
                return self._run_process_provider_call(
                    control,
                    provider,
                    operation,
                    remaining,
                )
            finally:
                if process_slot_acquired:
                    self._release_provider_process_slot()
                with self._lock:
                    if self._provider_calls_in_use:
                        self._provider_calls_in_use -= 1

        expires = time.monotonic() + float(timeout_seconds)

        def admitted_operation() -> Any:
            on_admitted()
            control.poll_local()
            if time.monotonic() >= expires:
                control.poll()
                raise ResearchProviderTimeout(
                    f"research {provider or 'unknown'} provider call timed out"
                )
            return operation()

        try:
            future = self._provider_executor.submit(admitted_operation)
        except DaemonExecutorCapacityError as exc:
            with self._lock:
                if self._provider_calls_in_use:
                    self._provider_calls_in_use -= 1
            raise ResearchProviderCapacityExceeded(
                "research provider compatibility queue is full"
            ) from exc
        except BaseException:
            with self._lock:
                if self._provider_calls_in_use:
                    self._provider_calls_in_use -= 1
            raise
        future.add_done_callback(self._provider_finished)
        try:
            while True:
                remaining = expires - time.monotonic()
                if remaining <= 0:
                    future.cancel()
                    # If the durable attempt deadline won the race, propagate
                    # that stronger control signal and persist its terminal row.
                    control.poll()
                    raise ResearchProviderTimeout(
                        f"research {provider or 'unknown'} provider call timed out"
                    )
                try:
                    return future.result(timeout=min(0.1, remaining))
                except FutureTimeoutError:
                    # An operation may itself raise TimeoutError. Distinguish it
                    # from Future.result's polling timeout before continuing.
                    if future.done():
                        return future.result()
                    control.poll()
                except CancelledError as exc:
                    control.checkpoint()
                    raise ResearchProviderError(
                        "research provider call was cancelled before execution"
                    ) from exc
        finally:
            if not future.done():
                future.cancel()

    def _transition_and_schedule(
        self,
        job_id: str,
        transition: Callable[[], dict[str, Any]],
        scheduler: Callable[[str, Mapping[str, Any]], bool],
    ) -> dict[str, Any]:
        """Atomically bridge a durable run transition to Future registration."""

        with self._lock:
            if self._closed:
                self._release_submission()
                raise RuntimeError("ResearchExecutionManager is closed")
            try:
                row = transition()
            except BaseException:
                self._release_submission()
                raise
            try:
                scheduler(job_id, row)
            except BaseException:
                self._release_submission()
                raise
            # The executor now owns admission for scheduled work. Release the
            # temporary pre-transition reservation in both branches.
            self._release_submission()
            return row

    def start(self, job_id: str) -> dict[str, Any]:
        self._reserve_submission()
        try:
            current = self._store.get(job_id)
        except BaseException:
            self._release_submission()
            raise
        if current is None:
            self._release_submission()
            raise KeyError(job_id)
        transition_started = False
        try:
            kb_id = str(current.get("kb_id") or "")
            self._assert_authorized(current)
            with kb_read_lease(kb_id):
                snapshot = self._read_provenance(kb_id)
                if (
                    self._provenance_reader is not None
                    and not is_trackable_research_provenance(snapshot)
                ):
                    raise ResearchJobStateConflictError(
                        "research evidence provenance is unavailable or incomplete"
                    )
                prior_status = (
                    research_provenance_status(
                        current.get("evidence_provenance"), snapshot
                    )
                    if snapshot is not None
                    else {"status": "untracked"}
                )
                if (
                    self._provenance_reader is not None
                    and current.get("status") == "failed"
                    and prior_status["status"] != "current"
                    and snapshot is not None
                ):
                    transition = partial(
                        self._store.refresh_evidence,
                        job_id,
                        evidence_provenance=snapshot,
                    )
                else:
                    transition = partial(
                        self._store.start,
                        job_id,
                        evidence_provenance=snapshot,
                    )
                transition_started = True
                return self._transition_and_schedule(
                    job_id, transition, self._schedule_evidence
                )
        except Exception as exc:
            if not transition_started:
                self._release_submission()
            if isinstance(exc, ResearchJobStateConflictError):
                raise
            if transition_started:
                raise
            raise ResearchJobStateConflictError(
                "research evidence provenance is unavailable"
            ) from exc
        except BaseException:
            if not transition_started:
                self._release_submission()
            raise

    def resume(self, job_id: str) -> dict[str, Any]:
        self._reserve_submission()
        transition_started = False
        try:
            current = self._store.get(job_id)
            if current is None:
                raise KeyError(job_id)
            # Preserve the state-machine error for an invalid transition.
            # Provenance is relevant only after we know there is a paused
            # execution to resume.
            if current.get("status") != "paused":
                self._assert_authorized(current)
                row = self._store.resume(job_id)
                self._release_submission()
                return row
            self._assert_authorized(current)
            with kb_read_lease(str(current.get("kb_id") or "")):
                self.assert_current(current)
                transition_started = True
                return self._transition_and_schedule(
                    job_id,
                    lambda: self._store.resume(job_id),
                    self._schedule_evidence,
                )
        except BaseException:
            if not transition_started:
                self._release_submission()
            raise

    def compile(self, job_id: str) -> dict[str, Any]:
        if self._report_builder is None:
            raise ResearchJobStateConflictError("research report builder is unavailable")
        self._reserve_submission()
        transition_started = False
        try:
            current = self._store.get(job_id)
            if current is None:
                raise KeyError(job_id)
            self._assert_authorized(current)
            with kb_read_lease(str(current.get("kb_id") or "")):
                self.assert_current(current)
                transition_started = True
                return self._transition_and_schedule(
                    job_id,
                    lambda: self._store.begin_report(job_id),
                    self._schedule_report,
                )
        except BaseException:
            if not transition_started:
                self._release_submission()
            raise

    def pause(self, job_id: str) -> dict[str, Any]:
        row = self._store.pause(job_id)
        self._signal_job(job_id, phase="evidence", reason="paused")
        return row

    def cancel(self, job_id: str) -> dict[str, Any]:
        row = self._store.cancel(job_id)
        self._signal_job(job_id, reason="cancelled")
        return row

    def refresh(self, job_id: str) -> dict[str, Any]:
        self._reserve_submission()
        try:
            current = self._store.get(job_id)
        except BaseException:
            self._release_submission()
            raise
        if current is None:
            self._release_submission()
            raise KeyError(job_id)
        transition_started = False
        try:
            kb_id = str(current.get("kb_id") or "")
            self._assert_authorized(current)
            with kb_read_lease(kb_id):
                snapshot = self._read_provenance(kb_id)
                if not is_trackable_research_provenance(snapshot):
                    raise ResearchJobStateConflictError(
                        "research evidence provenance is unavailable or incomplete"
                    )
                transition_started = True
                return self._transition_and_schedule(
                    job_id,
                    lambda: self._store.refresh_evidence(
                        job_id, evidence_provenance=snapshot
                    ),
                    self._schedule_evidence,
                )
        except Exception as exc:
            if not transition_started:
                self._release_submission()
            if isinstance(exc, ResearchJobStateConflictError):
                raise
            if transition_started:
                raise
            raise ResearchJobStateConflictError(
                "research evidence provenance is unavailable"
            ) from exc
        except BaseException:
            if not transition_started:
                self._release_submission()
            raise

    def review_report(
        self,
        job_id: str,
        *,
        decisions: Sequence[Mapping[str, Any]],
        expected_revision: int,
        reviewer_actor: str = "internal",
    ) -> dict[str, Any]:
        """Atomically hold the KB generation stable through review commit."""

        current = self._store.get(job_id)
        if current is None:
            raise KeyError(job_id)
        self._assert_authorized(current)
        with kb_read_lease(str(current.get("kb_id") or "")):
            self.assert_current(current)
            return self._store.review_report(
                job_id,
                decisions=decisions,
                expected_revision=expected_revision,
                reviewer_actor=reviewer_actor,
            )

    def publish_report(
        self,
        job_id: str,
        *,
        expected_revision: int,
        publisher_actor: str = "internal",
    ) -> dict[str, Any]:
        """Atomically hold the KB generation stable through publication."""

        current = self._store.get(job_id)
        if current is None:
            raise KeyError(job_id)
        self._assert_authorized(current)
        with kb_read_lease(str(current.get("kb_id") or "")):
            self.assert_current(current)
            return self._store.publish_report(
                job_id,
                expected_revision=expected_revision,
                publisher_actor=publisher_actor,
            )

    def _read_provenance(self, kb_id: str) -> Mapping[str, Any] | None:
        if self._provenance_reader is None:
            return None
        return dict(self._provenance_reader(kb_id))

    def provenance(self, job: Mapping[str, Any] | str) -> dict[str, Any]:
        row = self._store.get(job) if isinstance(job, str) else dict(job)
        if row is None:
            raise KeyError(job)
        try:
            current = self._read_provenance(str(row.get("kb_id") or ""))
        except Exception as exc:
            return {
                "status": (
                    "stale" if row.get("evidence_provenance") else "untracked"
                ),
                "stale_reasons": [f"provenance_reader_error:{type(exc).__name__}"],
                "captured": dict(row.get("evidence_provenance") or {}),
                "current": {},
            }
        if current is None:
            return {
                "status": "untracked",
                "stale_reasons": ["provenance_reader_unavailable"],
                "captured": dict(row.get("evidence_provenance") or {}),
                "current": {},
            }
        return research_provenance_status(
            row.get("evidence_provenance"),
            current,
        )

    def provenance_many(
        self, jobs: Sequence[Mapping[str, Any]]
    ) -> list[dict[str, Any]]:
        current_by_kb: dict[str, Mapping[str, Any] | None] = {}
        results: list[dict[str, Any]] = []
        for row in jobs:
            kb_id = str(row.get("kb_id") or "")
            if kb_id not in current_by_kb:
                try:
                    current_by_kb[kb_id] = self._read_provenance(kb_id)
                except Exception as exc:
                    current_by_kb[kb_id] = {
                        "__provenance_reader_error__": type(exc).__name__
                    }
            current = current_by_kb[kb_id]
            if isinstance(current, Mapping) and current.get(
                "__provenance_reader_error__"
            ):
                results.append(
                    {
                        "status": (
                            "stale" if row.get("evidence_provenance") else "untracked"
                        ),
                        "stale_reasons": [
                            "provenance_reader_error:"
                            + str(current["__provenance_reader_error__"])
                        ],
                        "captured": dict(row.get("evidence_provenance") or {}),
                        "current": {},
                    }
                )
                continue
            if current is None:
                results.append(
                    {
                        "status": "untracked",
                        "stale_reasons": ["provenance_reader_unavailable"],
                        "captured": dict(row.get("evidence_provenance") or {}),
                        "current": {},
                    }
                )
            else:
                results.append(
                    research_provenance_status(
                        row.get("evidence_provenance"),
                        current,
                    )
                )
        return results

    def assert_current(self, job: Mapping[str, Any] | str) -> dict[str, Any]:
        status = self.provenance(job)
        if self._provenance_reader is not None and status["status"] != "current":
            raise ResearchEvidenceStaleError(status["stale_reasons"])
        return status

    def shutdown(self, *, wait: bool = True) -> bool:
        already_closed = False
        with self._lock:
            if self._closed:
                already_closed = True
                handles = tuple(self._active.values())
                local_drained = (
                    not self._provider_calls_in_use
                    and not self._provider_processes_in_use
                    and not self._submission_reservations
                    and not any(not handle.future.done() for handle in handles)
                )
            else:
                self._closed = True
                handles = list(self._active.values())
        if already_closed:
            return local_drained and self._executor.is_drained()
        for handle in handles:
            handle.control.request_stop("shutdown")
            handle.future.cancel()
        reconciliation_error: BaseException | None = None
        try:
            reconcile = getattr(self._store, "reconcile_running_outcomes", None)
            if callable(reconcile):
                reconcile(terminal_reason="service_shutdown")
            else:
                legacy_reconcile = getattr(self._store, "reconcile_running", None)
                if callable(legacy_reconcile):
                    legacy_reconcile()
        except BaseException as exc:
            reconciliation_error = exc
        finally:
            # Daemon workers allow the application to stop even when an opaque
            # synchronous provider call ignores cancellation. Durable leases
            # were invalidated first, so any late reservation/commit fails.
            self._executor.shutdown(wait=wait, cancel_futures=True)
            # Provider calls live on a second bounded daemon pool. The Research
            # worker can stop immediately after lease invalidation even if an
            # opaque client ignores its native transport timeout.
            self._provider_executor.shutdown(wait=False, cancel_futures=True)
        if reconciliation_error is not None:
            raise reconciliation_error
        with self._lock:
            handles = tuple(self._active.values())
            local_drained = (
                not self._provider_calls_in_use
                and not self._provider_processes_in_use
                and not self._submission_reservations
                and not any(not handle.future.done() for handle in handles)
            )
        return local_drained and self._executor.is_drained()

    def _signal_job(
        self,
        job_id: str,
        *,
        phase: str | None = None,
        reason: str,
    ) -> None:
        with self._lock:
            handles = [
                handle
                for handle in self._active.values()
                if handle.job_id == job_id
                and (phase is None or handle.phase == phase)
            ]
        for handle in handles:
            handle.control.request_stop(reason)
            handle.future.cancel()

    def _controller(
        self,
        *,
        job_id: str,
        phase: str,
        attempt_id: str,
        lease_id: str,
        deadline_at: str,
    ) -> ResearchRunController:
        def persist_deadline() -> str:
            row = self._store.fail_run(
                job_id,
                phase=phase,
                attempt_id=attempt_id,
                lease_id=lease_id,
                error_class="ResearchDeadlineExceeded",
            )
            current = research_run_control(row, phase)
            if (
                current.get("attempt_id") == attempt_id
                and current.get("control_state") == "expired"
                and current.get("terminal_reason") == "ResearchDeadlineExceeded"
            ):
                return "deadline"
            if row.get("status") == "paused" or current.get(
                "control_state"
            ) == "paused":
                return "paused"
            return "superseded"

        return ResearchRunController(
            job_id=job_id,
            phase=phase,
            attempt_id=attempt_id,
            lease_id=lease_id,
            reserve_callback=lambda costs: self._store.reserve_research_resources(
                job_id,
                phase=phase,
                attempt_id=attempt_id,
                lease_id=lease_id,
                costs=costs,
            ),
            stop_event=Event(),
            deadline_at=deadline_at,
            provider_runner=self._run_provider_call,
            deadline_callback=persist_deadline,
        )

    def _schedule_evidence(self, job_id: str, row: Mapping[str, Any]) -> bool:
        execution_id = str(row.get("execution_id") or "")
        control_snapshot = research_run_control(row, "evidence")
        lease_id = str(control_snapshot.get("lease_id") or "")
        if not execution_id or not lease_id or row.get("status") != "running":
            return False
        # A refresh intentionally creates a new execution while an old paused
        # retrieval call may still be unwinding.  Key futures by execution so
        # the obsolete worker cannot suppress the fresh run; store mutations
        # remain guarded by execution_id.
        active_key = f"evidence:{job_id}:{lease_id}"
        with self._lock:
            if self._closed:
                raise RuntimeError("ResearchExecutionManager is closed")
            current = self._active.get(active_key)
            if current is not None and not current.future.done():
                return False
            controller = self._controller(
                job_id=job_id,
                phase="evidence",
                attempt_id=execution_id,
                lease_id=lease_id,
                deadline_at=str(control_snapshot.get("deadline_at") or ""),
            )
            try:
                future = self._executor.submit(
                    self._run_job,
                    job_id,
                    execution_id,
                    lease_id,
                    controller,
                )
            except BaseException as exc:
                failed = self._store.fail_run(
                    job_id,
                    phase="evidence",
                    attempt_id=execution_id,
                    lease_id=lease_id,
                    error_class="ResearchScheduleError",
                )
                self._observe(
                    "background_finished",
                    stage="evidence",
                    outcome="schedule_failed",
                    job_id=job_id,
                    kb_id=str(failed.get("kb_id") or row.get("kb_id") or ""),
                    execution_id=execution_id,
                    status=str(failed.get("status") or "failed"),
                    duration_ms=0.0,
                    error_class=type(exc).__name__,
                )
                if isinstance(exc, DaemonExecutorCapacityError):
                    raise ResearchExecutionCapacityError(
                        "research execution queue is full"
                    ) from exc
                raise
            handle = _ResearchRunHandle(
                active_key=active_key,
                job_id=job_id,
                phase="evidence",
                attempt_id=execution_id,
                lease_id=lease_id,
                future=future,
                control=controller,
                started_monotonic=time.monotonic(),
            )
            self._active[active_key] = handle
            self._observe(
                "background_started",
                stage="evidence",
                job_id=job_id,
                kb_id=str(row.get("kb_id") or ""),
                execution_id=execution_id,
                status=str(row.get("status") or "running"),
            )
            future.add_done_callback(lambda completed: self._forget(handle, completed))
            return True

    def _schedule_report(self, job_id: str, row: Mapping[str, Any]) -> bool:
        report_execution_id = str(row.get("report_execution_id") or "")
        control_snapshot = research_run_control(row, "report")
        lease_id = str(control_snapshot.get("lease_id") or "")
        if (
            not report_execution_id
            or not lease_id
            or row.get("status") != "generating"
        ):
            return False
        active_key = f"report:{job_id}:{lease_id}"
        with self._lock:
            if self._closed:
                raise RuntimeError("ResearchExecutionManager is closed")
            current = self._active.get(active_key)
            if current is not None and not current.future.done():
                return False
            controller = self._controller(
                job_id=job_id,
                phase="report",
                attempt_id=report_execution_id,
                lease_id=lease_id,
                deadline_at=str(control_snapshot.get("deadline_at") or ""),
            )
            try:
                future = self._executor.submit(
                    self._run_report,
                    job_id,
                    report_execution_id,
                    lease_id,
                    controller,
                )
            except BaseException as exc:
                failed = self._store.fail_run(
                    job_id,
                    phase="report",
                    attempt_id=report_execution_id,
                    lease_id=lease_id,
                    error_class="ResearchScheduleError",
                )
                self._observe(
                    "background_finished",
                    stage="report",
                    outcome="schedule_failed",
                    job_id=job_id,
                    kb_id=str(failed.get("kb_id") or row.get("kb_id") or ""),
                    execution_id=report_execution_id,
                    status=str(failed.get("status") or "failed"),
                    duration_ms=0.0,
                    error_class=type(exc).__name__,
                )
                if isinstance(exc, DaemonExecutorCapacityError):
                    raise ResearchExecutionCapacityError(
                        "research execution queue is full"
                    ) from exc
                raise
            handle = _ResearchRunHandle(
                active_key=active_key,
                job_id=job_id,
                phase="report",
                attempt_id=report_execution_id,
                lease_id=lease_id,
                future=future,
                control=controller,
                started_monotonic=time.monotonic(),
            )
            self._active[active_key] = handle
            self._observe(
                "background_started",
                stage="report",
                job_id=job_id,
                kb_id=str(row.get("kb_id") or ""),
                execution_id=report_execution_id,
                status=str(row.get("status") or "generating"),
            )
            future.add_done_callback(lambda completed: self._forget(handle, completed))
            return True

    def _background_result(
        self,
        handle: _ResearchRunHandle,
        error: BaseException | None,
        *,
        cancelled: bool,
    ) -> tuple[str, str, str, str, str]:
        """Derive a bounded outcome from the durable state after worker exit."""

        try:
            row = self._store.get(handle.job_id) or {}
        except Exception:
            row = {}
        status = str(row.get("status") or "")
        kb_id = str(row.get("kb_id") or "")
        error_class = type(error).__name__ if error is not None else str(
            row.get("error") or ""
        )
        control = research_run_control(row, handle.phase) if row else {}
        # The durable commit is authoritative.  A caller may observe the
        # terminal job state and immediately shut down the manager before this
        # Future callback runs; that late local stop signal must not rewrite a
        # successfully committed stage as cancelled or superseded telemetry.
        if (
            control.get("attempt_id") == handle.attempt_id
            and control.get("control_state") == "completed"
        ):
            return "succeeded", status, kb_id, error_class, ""
        stop_reason = handle.control.stop_reason
        if stop_reason == "shutdown":
            return "cancelled", status, kb_id, error_class, "shutdown"
        if stop_reason == "paused" or status == "paused":
            return "cancelled", status, kb_id, error_class, "paused"
        if stop_reason == "deadline":
            return (
                "failed",
                status,
                kb_id,
                error_class or "ResearchDeadlineExceeded",
                "deadline_exceeded",
            )
        if cancelled or stop_reason == "cancelled" or status == "cancelled":
            return "cancelled", status, kb_id, error_class, "cancelled"
        if error is not None and not isinstance(error, ResearchControlSignal):
            return "failed", status, kb_id, error_class, "worker_error"
        if control.get("attempt_id") != handle.attempt_id:
            return "superseded", status, kb_id, error_class, "superseded"
        control_state = str(control.get("control_state") or "")
        if control_state == "expired":
            return (
                "failed",
                status,
                kb_id,
                error_class or "ResearchDeadlineExceeded",
                "deadline_exceeded",
            )
        if control_state == "budget_exhausted":
            return (
                "failed",
                status,
                kb_id,
                error_class or "ResearchBudgetExceeded",
                "budget_exhausted",
            )
        if control_state == "cancelled":
            return "cancelled", status, kb_id, error_class, "cancelled"
        if status == "failed" or control_state == "failed":
            return "failed", status, kb_id, error_class, "worker_error"
        return "superseded", status, kb_id, error_class, "superseded"

    def _forget(self, handle: _ResearchRunHandle, future: Future) -> None:
        try:
            error: BaseException | None = None
            was_cancelled = future.cancelled()
            if not was_cancelled:
                try:
                    error = future.exception()
                except CancelledError:
                    was_cancelled = True
            if error is not None and not isinstance(error, ResearchControlSignal):
                try:
                    self._store.fail_run(
                        handle.job_id,
                        phase=handle.phase,
                        attempt_id=handle.attempt_id,
                        lease_id=handle.lease_id,
                        error_class=type(error).__name__,
                    )
                except BaseException:
                    # Startup reconciliation remains the durable fallback when
                    # callback-side persistence itself is unavailable.
                    pass
            outcome, status, kb_id, error_class, termination = (
                self._background_result(
                    handle,
                    error,
                    cancelled=was_cancelled,
                )
            )
            if termination:
                self._observe(
                    "control_terminated",
                    reason=termination,
                    job_id=handle.job_id,
                    kb_id=kb_id,
                    execution_id=handle.attempt_id,
                    stage=handle.phase,
                    status=status,
                    error_class=error_class,
                )
            self._observe(
                "background_finished",
                stage=handle.phase,
                outcome=outcome,
                job_id=handle.job_id,
                kb_id=kb_id,
                execution_id=handle.attempt_id,
                status=status,
                duration_ms=(time.monotonic() - handle.started_monotonic) * 1000,
                error_class=error_class,
            )
        finally:
            # Admission cleanup must not depend on Future exception inspection
            # or on the availability of the durable store.
            with self._lock:
                current = self._active.get(handle.active_key)
                if current is handle and current.future is future:
                    self._active.pop(handle.active_key, None)

    def _run_report(
        self,
        job_id: str,
        report_execution_id: str,
        lease_id: str,
        control: ResearchRunController,
    ) -> None:
        try:
            with bind_research_control(control):
                control.checkpoint()
                job = self._store.get(job_id)
                if job is None:
                    return
                self._assert_authorized(job)
                requested_section_ids = {
                    str(section_id)
                    for section_id in job.get("regeneration_section_ids") or []
                    if str(section_id)
                }
                with kb_read_lease(str(job.get("kb_id") or "")):
                    self.assert_current(job)
                    result = (
                        self._report_builder(job) if self._report_builder else None
                    )
                    if result is None:
                        raise RuntimeError(
                            "research report builder returned no result"
                        )
                    if isinstance(result, Mapping):
                        payload = dict(result)
                    elif is_dataclass(result):
                        payload = asdict(result)
                        # Dataclass report results use immutable tuples internally.
                        # Normalize only this trusted representation before crossing
                        # the store's strict JSON artifact boundary.
                        for field in ("sections", "citation_ledger"):
                            if isinstance(payload.get(field), tuple):
                                payload[field] = list(payload[field])
                    else:
                        raise TypeError(
                            "research report builder returned unsupported result"
                        )
                    control.checkpoint()
                    # The read lease prevents an index generation switch between
                    # this final check and the durable artifact commit.
                    self.assert_current(job_id)
                    control.checkpoint()
                    # Keep authorization as the final fallible external check:
                    # report generation and provenance/control checks may span a
                    # revocation, whose output must never cross the store boundary.
                    self._assert_authorized(job)
                    committed = self._store.complete_report(
                        job_id,
                        report_execution_id=report_execution_id,
                        result=payload,
                        lease_id=lease_id,
                    )
                    committed_control = research_run_control(committed, "report")
                    if committed_control.get("last_commit_lease_id") == lease_id:
                        for section in committed.get("sections") or []:
                            if not isinstance(section, Mapping):
                                continue
                            section_id = str(section.get("section_id") or "")
                            if (
                                requested_section_ids
                                and section_id not in requested_section_ids
                            ):
                                continue
                            common = {
                                "job_id": job_id,
                                "section_id": section_id,
                                "kb_id": str(committed.get("kb_id") or ""),
                                "execution_id": report_execution_id,
                                "error_class": str(section.get("error") or ""),
                            }
                            self._observe(
                                "claim_audit",
                                audit=section.get("claim_audit"),
                                **common,
                            )
                            self._observe(
                                "coverage_audit",
                                audit=section.get("coverage_audit"),
                                **common,
                            )
        except (ResearchPaused, ResearchCancelled):
            return
        except (ResearchDeadlineExceeded, ResearchBudgetExceeded):
            return
        except Exception as exc:
            self._store.fail_report(
                job_id,
                report_execution_id=report_execution_id,
                error_class=type(exc).__name__,
                lease_id=lease_id,
            )

    def _run_job(
        self,
        job_id: str,
        execution_id: str,
        lease_id: str,
        control: ResearchRunController,
    ) -> None:
        section_id = ""

        def checkpoint_for_draining_section() -> None:
            try:
                control.checkpoint()
            except ResearchPaused:
                # Pause keeps the claimed section's old lease as a draining
                # lease. Preserve the established between-sections contract:
                # an already-returned retrieval may be committed, while the
                # loop-top checkpoint still prevents claiming another section.
                pass

        try:
            with bind_research_control(control):
                while True:
                    control.checkpoint()
                    row, section = self._store.claim_next_section(
                        job_id,
                        execution_id,
                        lease_id=lease_id,
                    )
                    if section is None:
                        return
                    section_id = str(section.get("section_id") or "")
                    started = time.monotonic()
                    kb_id = str(row.get("kb_id") or "")
                    if not self._kb_exists(kb_id):
                        raise LookupError("knowledge base no longer exists")
                    with kb_read_lease(kb_id):
                        self.assert_current(row)
                        query_results: list[dict[str, Any]] = []
                        docs: list[Mapping[str, Any]] = []
                        seen_docs: set[tuple[str, str]] = set()
                        for requirement_id, query in research_section_queries(section):
                            control.reserve(
                                retrieval_queries=1,
                                candidate_docs=self._retrieval_doc_reservation,
                            )
                            retrieved = self._retrieve_for_job(row, kb_id, query)
                            checkpoint_for_draining_section()
                            self._assert_authorized(row)
                            self.assert_current(row)
                            query_results.append(
                                {
                                    "requirement_id": requirement_id,
                                    "candidate_count": len(retrieved),
                                }
                            )
                            for doc in retrieved:
                                meta = (
                                    doc.get("meta")
                                    if isinstance(doc.get("meta"), Mapping)
                                    else {}
                                )
                                identity = (
                                    str(meta.get("chunk_id") or ""),
                                    str(meta.get("knowledge_id") or ""),
                                )
                                if not any(identity) or identity in seen_docs:
                                    continue
                                seen_docs.add(identity)
                                docs.append(doc)
                        evidence = public_research_evidence(docs)
                        self.assert_current(row)
                        checkpoint_for_draining_section()
                        execution_metrics = {
                            "candidate_count": len(docs),
                            "evidence_count": len(evidence),
                            "query_count": len(query_results),
                            "requirements": query_results,
                            "duration_ms": round(
                                (time.monotonic() - started) * 1000, 3
                            ),
                        }
                        # Evidence shaping and provenance/control checks happen
                        # after retrieval. Revalidate the live ACL only once all
                        # commit inputs are ready, then cross the durable boundary.
                        self._assert_authorized(row)
                        committed = self._store.complete_section(
                            job_id,
                            section_id,
                            execution_id=execution_id,
                            evidence_status="partial" if evidence else "missing",
                            evidence=evidence,
                            execution_metrics=execution_metrics,
                            lease_id=lease_id,
                        )
                        committed_control = research_run_control(
                            committed, "evidence"
                        )
                        if (
                            committed_control.get("last_commit_lease_id") == lease_id
                            and committed_control.get("last_commit_section_id")
                            == section_id
                        ):
                            self._observe(
                                "section_completed",
                                job_id=job_id,
                                section_id=section_id,
                                kb_id=kb_id,
                                execution_id=execution_id,
                                status="completed",
                                candidate_count=len(docs),
                                evidence_count=len(evidence),
                                query_count=len(query_results),
                                duration_ms=(time.monotonic() - started) * 1000,
                            )
                    section_id = ""
        except (ResearchPaused, ResearchCancelled):
            return
        except (ResearchDeadlineExceeded, ResearchBudgetExceeded):
            return
        except Exception as exc:
            if section_id:
                failed = self._store.fail_section(
                    job_id,
                    section_id,
                    execution_id=execution_id,
                    error_class=type(exc).__name__,
                    lease_id=lease_id,
                )
                if failed.get("status") == "failed":
                    self._observe(
                        "section_completed",
                        job_id=job_id,
                        section_id=section_id,
                        kb_id=str(failed.get("kb_id") or ""),
                        execution_id=execution_id,
                        status="failed",
                        error_class=type(exc).__name__,
                    )
            else:
                self._store.fail_run(
                    job_id,
                    phase="evidence",
                    attempt_id=execution_id,
                    lease_id=lease_id,
                    error_class=type(exc).__name__,
                )
