from __future__ import annotations

import hashlib
import json
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Callable

from cogdoc.connectors.base import (
    ConnectorError,
    SourceConnector,
    StaleSyncLease,
    SyncBudgetExceeded,
    SyncCancelled,
    SyncSink,
)
from cogdoc.connectors.sync_observer import (
    NO_OP_SYNC_OBSERVER,
    SyncEventKind,
    SyncObservation,
    SyncObserver,
)
from cogdoc.connectors.sync_store import ConnectorSyncStore


@dataclass(frozen=True)
class SyncLimits:
    page_size: int = 100
    max_pages: int = 10_000
    max_documents: int = 100_000
    max_document_bytes: int = 100 * 1024 * 1024
    max_total_bytes: int = 10 * 1024 * 1024 * 1024
    deadline_seconds: float = 3600.0
    lease_seconds: float = 60.0
    max_attempts: int = 5
    retry_base_seconds: float = 5.0

    def __post_init__(self) -> None:
        for name in (
            "page_size",
            "max_pages",
            "max_documents",
            "max_document_bytes",
            "max_total_bytes",
            "max_attempts",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or value <= 0:
                raise ValueError(f"{name} must be positive")
        if (
            self.deadline_seconds <= 0
            or self.lease_seconds <= 0
            or self.retry_base_seconds < 0
        ):
            raise ValueError("sync time limits are invalid")


class ConnectorSyncRuntime:
    def __init__(
        self,
        store: ConnectorSyncStore,
        *,
        limits: SyncLimits | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        observer: SyncObserver | None = None,
        continuation_checker: Callable[[Mapping[str, Any]], bool] | None = None,
    ) -> None:
        self.store = store
        self.limits = limits or SyncLimits()
        self._monotonic = monotonic
        self.observer = observer or NO_OP_SYNC_OBSERVER
        self._continuation_checker = continuation_checker
        self._binding_lock = threading.RLock()
        self._started = False

    def bind_controls(
        self,
        *,
        observer: SyncObserver,
        continuation_checker: Callable[[Mapping[str, Any]], bool],
    ) -> None:
        """Bind trusted app dependencies before the first job acquisition."""

        if observer is None or not callable(continuation_checker):
            raise TypeError("sync runtime controls are required")
        with self._binding_lock:
            if self._started:
                raise RuntimeError("sync runtime has already started")
            self.observer = observer
            self._continuation_checker = continuation_checker

    def run(
        self, job_id: str, connector: SourceConnector, sink: SyncSink
    ) -> dict[str, Any]:
        with self._binding_lock:
            self._started = True
        observed_at = self._monotonic()
        try:
            job, token = self.store.acquire(
                job_id, lease_seconds=self.limits.lease_seconds
            )
        except StaleSyncLease:
            # An expired, cancellation-requested lease is terminalized by the
            # acquire transaction. Project that durable cancellation now so a
            # crash/restart cannot leave SourceCatalog permanently ``syncing``.
            cancelled = self.store.get(job_id)
            if cancelled is None or cancelled.get("status") != "cancelled":
                raise
            self._observe("cancelled", cancelled, {}, observed_at)
            return cancelled
        self._observe("started", job, {}, observed_at)
        if job["connector_type"] != str(connector.connector_type).strip().casefold():
            result = self.store.fail(
                job_id,
                token,
                error_code="CONNECTOR_TYPE_MISMATCH",
                error_message="connector does not match the sync job",
                retryable=False,
            )
            self._record_health_quiet(
                job_id, duration_seconds=self._duration(observed_at)
            )
            self._observe(
                "failed",
                result,
                {},
                observed_at,
                error_code="CONNECTOR_TYPE_MISMATCH",
            )
            return result
        deadline = self._monotonic() + self.limits.deadline_seconds
        counters = {
            "pages_processed": int(job["pages_processed"]),
            "documents_seen": int(job["documents_seen"]),
            "documents_fetched": int(job["documents_fetched"]),
            "deleted_seen": int(job["deleted_seen"]),
            "bytes_fetched": int(job["bytes_fetched"]),
        }
        cursor = job.get("cursor")
        attempt_budget_bytes = int(counters["bytes_fetched"])
        seen_external_ids: set[str] = set()
        deleted_external_ids: set[str] = set()
        snapshot: bool | None = None
        begun = False
        commit_prepared = job["status"] == "committing"
        try:
            sink.begin(
                job_id=job_id,
                tenant_id=job["tenant_id"],
                kb_id=job["kb_id"],
                connection_id=job["connection_id"],
                connector_type=job["connector_type"],
                attempt=int(job["attempt"]),
                recovering_commit=job["status"] == "committing",
            )
            begun = True
            if job["status"] == "committing":
                sink.recover_commit(
                    heartbeat=lambda: self.store.heartbeat(
                        job_id, token, lease_seconds=self.limits.lease_seconds
                    )
                )
                completed = self.store.complete(
                    job_id, token, cursor=cursor, counters=counters
                )
                self._record_health_quiet(
                    job_id, duration_seconds=self._duration(observed_at)
                )
                self._observe("succeeded", completed, counters, observed_at)
                self._finalize_quiet(job_id, sink)
                return completed
            while True:
                self._guard(job_id, token, deadline)
                if counters["pages_processed"] >= self.limits.max_pages:
                    raise SyncBudgetExceeded("connector page budget exhausted")
                page = connector.list_page(cursor, limit=self.limits.page_size)
                if (
                    len(page.items) > self.limits.page_size
                    or len(page.deleted_external_ids) > self.limits.page_size
                ):
                    raise ConnectorError("connector exceeded the requested page size")
                if not page.complete and page.next_cursor == cursor:
                    raise ConnectorError("connector cursor did not advance")
                page_ids = [ref.external_id for ref in page.items]
                if len(set(page_ids)) != len(page_ids):
                    raise ConnectorError(
                        "connector page contains duplicate external IDs"
                    )
                if seen_external_ids.intersection(page_ids):
                    raise ConnectorError(
                        "connector repeated an external ID across pages"
                    )
                if snapshot is None:
                    snapshot = page.snapshot
                elif snapshot != page.snapshot:
                    raise ConnectorError("connector changed sync mode between pages")
                for ref in page.items:
                    self._guard(job_id, token, deadline)
                    attempt_budget_bytes += len(
                        json.dumps(
                            dict(ref.metadata),
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode("utf-8")
                    )
                    if attempt_budget_bytes > self.limits.max_total_bytes:
                        raise SyncBudgetExceeded(
                            "connector total byte budget exhausted"
                        )
                    if ref.external_id in deleted_external_ids:
                        raise ConnectorError(
                            "connector upserted an external ID deleted on an earlier page"
                        )
                    counters["documents_seen"] += 1
                    if counters["documents_seen"] > self.limits.max_documents:
                        raise SyncBudgetExceeded("connector document budget exhausted")
                    if (
                        ref.byte_size is not None
                        and ref.byte_size > self.limits.max_document_bytes
                    ):
                        raise SyncBudgetExceeded(
                            "connector document exceeds the byte limit"
                        )
                    fetched = connector.fetch(ref)
                    if fetched.ref.external_id != ref.external_id:
                        raise ConnectorError(
                            "connector fetch returned another external ID"
                        )
                    if fetched.ref.metadata != ref.metadata:
                        attempt_budget_bytes += len(
                            json.dumps(
                                dict(fetched.ref.metadata),
                                ensure_ascii=False,
                                sort_keys=True,
                                separators=(",", ":"),
                            ).encode("utf-8")
                        )
                    size = len(fetched.content)
                    if size > self.limits.max_document_bytes:
                        raise SyncBudgetExceeded(
                            "connector document exceeds the byte limit"
                        )
                    acl_bytes = (
                        len(
                            json.dumps(
                                dict(fetched.acl),
                                ensure_ascii=False,
                                sort_keys=True,
                                separators=(",", ":"),
                            ).encode("utf-8")
                        )
                        if fetched.acl is not None
                        else 0
                    )
                    attempt_budget_bytes += size + acl_bytes
                    counters["bytes_fetched"] += size
                    if attempt_budget_bytes > self.limits.max_total_bytes:
                        raise SyncBudgetExceeded(
                            "connector total byte budget exhausted"
                        )
                    digest = hashlib.sha256(fetched.content).hexdigest()
                    if ref.content_sha256 and ref.content_sha256.casefold() != digest:
                        raise ConnectorError("connector content digest mismatch")
                    document = fetched.document(
                        connector.connector_type, content_sha256=digest
                    )
                    sink.upsert(document, fetched.content, acl=fetched.acl)
                    counters["documents_fetched"] += 1
                    seen_external_ids.add(ref.external_id)
                for external_id in page.deleted_external_ids:
                    self._guard(job_id, token, deadline)
                    if (
                        counters["documents_seen"] + counters["deleted_seen"] + 1
                        > self.limits.max_documents
                    ):
                        raise SyncBudgetExceeded(
                            "connector document and deletion budget exhausted"
                        )
                    if external_id in seen_external_ids:
                        raise ConnectorError(
                            "connector both upserted and deleted one external ID"
                        )
                    if external_id in deleted_external_ids:
                        raise ConnectorError(
                            "connector repeated a deleted external ID across pages"
                        )
                    sink.delete(external_id)
                    deleted_external_ids.add(external_id)
                    counters["deleted_seen"] += 1
                counters["pages_processed"] += 1
                cursor = page.next_cursor
                self.store.checkpoint(
                    job_id,
                    token,
                    cursor=cursor,
                    counters=counters,
                    lease_seconds=self.limits.lease_seconds,
                )
                self._observe("progress", job, counters, observed_at)
                if page.complete:
                    break
            self._guard(job_id, token, deadline)
            sink.prepare_commit(
                snapshot=bool(snapshot),
                seen_external_ids=frozenset(seen_external_ids),
            )
            self.store.prepare_commit(job_id, token)
            commit_prepared = True
            mark_committing = getattr(sink, "mark_committing", None)
            if callable(mark_committing):
                mark_committing()
            sink.commit(
                snapshot=bool(snapshot),
                seen_external_ids=frozenset(seen_external_ids),
                heartbeat=lambda: self.store.heartbeat(
                    job_id, token, lease_seconds=self.limits.lease_seconds
                ),
            )
            completed = self.store.complete(
                job_id, token, cursor=cursor, counters=counters
            )
            self._record_health_quiet(
                job_id, duration_seconds=self._duration(observed_at)
            )
            self._observe("succeeded", completed, counters, observed_at)
            self._finalize_quiet(job_id, sink)
            return completed
        except SyncCancelled:
            if begun:
                try:
                    sink.abort()
                except Exception:
                    pass
            result = self.store.mark_cancelled(job_id, token)
            self._record_health_quiet(
                job_id, duration_seconds=self._duration(observed_at)
            )
            self._observe("cancelled", result, counters, observed_at)
            return result
        except Exception as exc:
            if begun:
                try:
                    sink.abort()
                except Exception:
                    pass
            can_retry = bool(getattr(exc, "retryable", False))
            # Once the durable job has crossed into ``committing``, sink
            # visibility can be ambiguous.  Never replay provider pages or
            # dead-letter that authority-boundary outcome; retain the commit
            # lease state so the next attempt executes ``recover_commit`` only.
            retryable = commit_prepared or (
                can_retry and int(job["attempt"]) < self.limits.max_attempts
            )
            dead_letter = can_retry and not retryable
            delay = self.limits.retry_base_seconds * (
                2 ** min(10, max(0, int(job["attempt"]) - 1))
            )
            error_code = type(exc).__name__.upper()
            result = self.store.fail(
                job_id,
                token,
                error_code=error_code,
                error_message=(
                    str(exc)
                    if isinstance(exc, SyncBudgetExceeded)
                    or type(exc) is ConnectorError
                    else "connector synchronization failed"
                ),
                retryable=retryable,
                retry_delay_seconds=delay,
                dead_letter=dead_letter,
                preserve_committing=commit_prepared,
            )
            self._record_health_quiet(
                job_id, duration_seconds=self._duration(observed_at)
            )
            kind: SyncEventKind = (
                "retry" if retryable else "dead_letter" if dead_letter else "failed"
            )
            self._observe(
                kind,
                result,
                counters,
                observed_at,
                error_code=error_code,
                retry_at=result.get("retry_at"),
            )
            return result

    def reconcile(self, job: Mapping[str, Any]) -> None:
        """Idempotently restore the catalog projection without emitting events."""

        status_to_kind: dict[str, SyncEventKind] = {
            "succeeded": "succeeded",
            "failed": "failed",
            "dead_letter": "dead_letter",
            "cancelled": "cancelled",
        }
        kind = status_to_kind.get(str(job.get("status")))
        callback = getattr(self.observer, "reconcile", None)
        if kind is None or not callable(callback):
            return
        counters = {
            name: int(job.get(name, 0))
            for name in (
                "pages_processed",
                "documents_seen",
                "documents_fetched",
                "deleted_seen",
                "bytes_fetched",
            )
        }
        observation = SyncObservation(
            kind=kind,
            job_id=str(job["job_id"]),
            tenant_id=str(job["tenant_id"]),
            kb_id=str(job["kb_id"]),
            connection_id=str(job["connection_id"]),
            connector_type=str(job["connector_type"]),
            job_sequence=int(job["job_sequence"]),
            attempt=int(job["attempt"]),
            duration_seconds=max(
                0.0, float(job.get("health_duration_seconds") or 0.0)
            ),
            backlog=self.store.backlog_size(
                str(job["tenant_id"]),
                str(job["kb_id"]),
                connection_id=str(job["connection_id"]),
            ),
            counters=counters,
            error_code=(str(job["error_code"]) if job.get("error_code") else None),
            retry_at=(float(job["retry_at"]) if job.get("retry_at") else None),
        )
        try:
            callback(observation)
        except Exception:
            pass

    def observe_cancelled(self, job: Mapping[str, Any]) -> None:
        """Emit one cancellation event for work rejected before acquisition."""

        self._observe("cancelled", dict(job), {}, self._monotonic())

    def _guard(self, job_id: str, token: str, deadline: float) -> None:
        if self._monotonic() >= deadline:
            raise SyncBudgetExceeded("connector sync deadline exhausted")
        if self.store.cancellation_requested(job_id, token):
            raise SyncCancelled("connector sync was cancelled")
        if self._continuation_checker is not None:
            current = self.store.get(job_id)
            if current is None or not self._continuation_checker(current):
                raise SyncCancelled("connector connection was revoked")
        self.store.heartbeat(job_id, token, lease_seconds=self.limits.lease_seconds)

    def _finalize_quiet(self, job_id: str, sink: SyncSink) -> None:
        try:
            sink.finalize()
            self.store.mark_cleanup_complete(job_id)
        except Exception:
            # The terminal job/checkpoint and cleanup_pending watermark are
            # already durable. Manager recovery retries this idempotently.
            pass

    def _duration(self, started_at: float) -> float:
        return max(0.0, self._monotonic() - started_at)

    def _record_health_quiet(self, job_id: str, *, duration_seconds: float) -> None:
        try:
            self.store.record_health(job_id, duration_seconds=duration_seconds)
        except Exception:
            # Health is a rebuildable projection of the durable job ledger. It
            # must not turn a committed synchronization into a reported failure.
            pass

    def _observe(
        self,
        kind: SyncEventKind,
        job: dict[str, Any],
        counters: dict[str, int],
        started_at: float,
        *,
        error_code: str | None = None,
        retry_at: float | None = None,
    ) -> None:
        observation = SyncObservation(
            kind=kind,
            job_id=str(job["job_id"]),
            tenant_id=str(job["tenant_id"]),
            kb_id=str(job["kb_id"]),
            connection_id=str(job["connection_id"]),
            connector_type=str(job["connector_type"]),
            job_sequence=int(job["job_sequence"]),
            attempt=int(job["attempt"]),
            duration_seconds=self._duration(started_at),
            backlog=self.store.backlog_size(
                str(job["tenant_id"]),
                str(job["kb_id"]),
                connection_id=str(job["connection_id"]),
            ),
            counters=counters,
            error_code=error_code,
            retry_at=retry_at,
        )
        try:
            callback = getattr(self.observer, kind)
            callback(observation)
        except Exception:
            # Observability is deliberately outside the lease and commit
            # authority boundary. A broken exporter must never fail a sync.
            pass
