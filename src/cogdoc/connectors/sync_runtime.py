from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from typing import Callable

from cogdoc.connectors.base import (
    ConnectorError,
    SourceConnector,
    SyncBudgetExceeded,
    SyncCancelled,
    SyncSink,
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
    ) -> None:
        self.store = store
        self.limits = limits or SyncLimits()
        self._monotonic = monotonic

    def run(self, job_id: str, connector: SourceConnector, sink: SyncSink) -> dict:
        job, token = self.store.acquire(job_id, lease_seconds=self.limits.lease_seconds)
        if job["connector_type"] != str(connector.connector_type).strip().casefold():
            self.store.fail(
                job_id,
                token,
                error_code="CONNECTOR_TYPE_MISMATCH",
                error_message="connector does not match the sync job",
                retryable=False,
            )
            return self.store.get(job_id) or {}
        deadline = self._monotonic() + self.limits.deadline_seconds
        counters = {
            "pages_processed": int(job["pages_processed"]),
            "documents_seen": int(job["documents_seen"]),
            "documents_fetched": int(job["documents_fetched"]),
            "deleted_seen": int(job["deleted_seen"]),
            "bytes_fetched": int(job["bytes_fetched"]),
        }
        cursor = job.get("cursor")
        seen_external_ids: set[str] = set()
        deleted_external_ids: set[str] = set()
        snapshot: bool | None = None
        begun = False
        try:
            sink.begin(
                job_id=job_id,
                tenant_id=job["tenant_id"],
                kb_id=job["kb_id"],
                connection_id=job["connection_id"],
                connector_type=job["connector_type"],
                attempt=int(job["attempt"]),
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
                self._finalize_quiet(sink)
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
                    size = len(fetched.content)
                    if size > self.limits.max_document_bytes:
                        raise SyncBudgetExceeded(
                            "connector document exceeds the byte limit"
                        )
                    counters["bytes_fetched"] += size
                    if counters["bytes_fetched"] > self.limits.max_total_bytes:
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
                if page.complete:
                    break
            self._guard(job_id, token, deadline)
            self.store.prepare_commit(job_id, token)
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
            self._finalize_quiet(sink)
            return completed
        except SyncCancelled:
            if begun:
                try:
                    sink.abort()
                except Exception:
                    pass
            return self.store.mark_cancelled(job_id, token)
        except Exception as exc:
            if begun:
                try:
                    sink.abort()
                except Exception:
                    pass
            retryable = (
                bool(getattr(exc, "retryable", False))
                and int(job["attempt"]) < self.limits.max_attempts
            )
            delay = self.limits.retry_base_seconds * (
                2 ** max(0, int(job["attempt"]) - 1)
            )
            return self.store.fail(
                job_id,
                token,
                error_code=type(exc).__name__.upper(),
                error_message=(
                    str(exc)
                    if isinstance(exc, SyncBudgetExceeded)
                    or type(exc) is ConnectorError
                    else "connector synchronization failed"
                ),
                retryable=retryable,
                retry_delay_seconds=delay,
            )

    def _guard(self, job_id: str, token: str, deadline: float) -> None:
        if self._monotonic() >= deadline:
            raise SyncBudgetExceeded("connector sync deadline exhausted")
        if self.store.cancellation_requested(job_id, token):
            raise SyncCancelled("connector sync was cancelled")
        self.store.heartbeat(job_id, token, lease_seconds=self.limits.lease_seconds)

    @staticmethod
    def _finalize_quiet(sink: SyncSink) -> None:
        try:
            sink.finalize()
        except Exception:
            # The terminal job/checkpoint is already durable. A leftover
            # idempotent sink journal is safe for a later sweeper to remove.
            pass
