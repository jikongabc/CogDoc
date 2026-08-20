from __future__ import annotations

import time
from collections.abc import Callable

from cogdoc.connectors.connection_store import SUPPORTED_CONNECTOR_TYPES
from cogdoc.connectors.sync_observer import SyncObservation


_EVENTS = frozenset(
    {
        "started",
        "progress",
        "retry",
        "succeeded",
        "failed",
        "dead_letter",
        "cancelled",
    }
)
_TERMINAL_ATTEMPTS = frozenset(
    {"retry", "succeeded", "failed", "dead_letter", "cancelled"}
)


class ConnectorOperationsObserver:
    """Project secret-free sync observations into metrics, health and webhooks."""

    def __init__(
        self,
        metrics,
        source_catalog,
        *,
        webhook_submitter: Callable[[str, dict], None] | None = None,
        current_job_checker: Callable[[SyncObservation], bool] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.metrics = metrics
        self.source_catalog = source_catalog
        self.webhook_submitter = webhook_submitter
        self.current_job_checker = current_job_checker
        self._clock = clock

    def started(self, observation: SyncObservation) -> None:
        self._record("started", observation)

    def progress(self, observation: SyncObservation) -> None:
        self._record("progress", observation)

    def retry(self, observation: SyncObservation) -> None:
        self._record("retry", observation)

    def succeeded(self, observation: SyncObservation) -> None:
        self._record("succeeded", observation)

    def failed(self, observation: SyncObservation) -> None:
        self._record("failed", observation)

    def dead_letter(self, observation: SyncObservation) -> None:
        self._record("dead_letter", observation)

    def cancelled(self, observation: SyncObservation) -> None:
        self._record("cancelled", observation)

    def reconcile(self, observation: SyncObservation) -> None:
        """Repair only the durable source projection after a process restart."""

        self._project_health(observation.kind, observation, check_current=False)

    def _record(self, outcome: str, observation: SyncObservation) -> None:
        normalized_outcome = outcome if outcome in _EVENTS else "failed"
        connector_type = (
            observation.connector_type
            if observation.connector_type in SUPPORTED_CONNECTOR_TYPES
            else "unknown"
        )
        self.metrics.connector_sync_events.labels(
            connector_type, normalized_outcome
        ).inc()
        self.metrics.connector_sync_backlog.labels(connector_type).observe(
            observation.backlog
        )
        if normalized_outcome in _TERMINAL_ATTEMPTS:
            self.metrics.connector_sync_duration.labels(
                connector_type, normalized_outcome
            ).observe(observation.duration_seconds)
            documents = max(0, int(observation.counters.get("documents_fetched", 0)))
            if documents:
                self.metrics.connector_sync_documents.labels(
                    connector_type, normalized_outcome
                ).inc(documents)

        self._project_health(normalized_outcome, observation, check_current=True)

        if (
            self.webhook_submitter is not None
            and normalized_outcome in _TERMINAL_ATTEMPTS
        ):
            self.webhook_submitter(
                f"connector.sync.{normalized_outcome}",
                {
                    "job_id": observation.job_id,
                    "job_sequence": observation.job_sequence,
                    # One terminal event is emitted per attempt, making
                    # (job_sequence,event_sequence) a stable consumer ordering key.
                    "event_sequence": observation.event_sequence,
                    "event_rank": observation.event_rank,
                    "outcome": normalized_outcome,
                    "tenant_id": observation.tenant_id,
                    "kb_id": observation.kb_id,
                    "connection_id": observation.connection_id,
                    "connector_type": connector_type,
                    "attempt": observation.attempt,
                    "duration_seconds": observation.duration_seconds,
                    "backlog": observation.backlog,
                    "counters": dict(observation.counters),
                    "error_code": observation.error_code,
                    "retry_at": observation.retry_at,
                },
            )

    def _project_health(
        self,
        outcome: str,
        observation: SyncObservation,
        *,
        check_current: bool,
    ) -> None:
        health = {
            "started": "syncing",
            "retry": "degraded",
            "succeeded": "healthy",
            "failed": "error",
            "dead_letter": "error",
            "cancelled": "stale",
        }.get(outcome)
        current_for_health = not check_current or self.current_job_checker is None or bool(
            self.current_job_checker(observation)
        )
        if health is not None and current_for_health:
            self.source_catalog.record_connection_health(
                observation.tenant_id,
                observation.kb_id,
                observation.connection_id,
                health,
                last_sync_at=(
                    self._clock() if outcome == "succeeded" else None
                ),
                last_sync_error=(
                    observation.error_code
                    if outcome in {"retry", "failed", "dead_letter"}
                    else None
                ),
                job_sequence=observation.job_sequence,
                job_attempt=observation.attempt,
                event_rank=observation.event_rank,
            )
