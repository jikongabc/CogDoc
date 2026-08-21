from __future__ import annotations

import logging
import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from cogdoc.ha.index_generation import IndexGenerationStore
from cogdoc.ha.object_store import ObjectIndexRepository
from cogdoc.ha.outbox import OutboxStore
from cogdoc.ha.scheduler import ScheduleStore
from cogdoc.ha.tasks import LeaseJobStore


LOGGER = logging.getLogger(__name__)


class _MaintenanceOperationError(RuntimeError):
    def __init__(self, message: str, *, completed: int) -> None:
        super().__init__(message)
        self.completed = completed


@dataclass(frozen=True)
class MaintenanceSnapshot:
    running: bool
    last_started_at: float | None
    last_succeeded_at: float | None
    last_error_at: float | None
    last_error: str | None
    runs: int
    failures: int
    jobs_reaped: int
    jobs_pruned: int
    outbox_pruned: int
    fires_pruned: int
    generations_removed: int
    generations_scrubbed: int


class HAMaintenance:
    """Bounded, crash-safe maintenance for every durable HA ledger."""

    def __init__(
        self,
        jobs: LeaseJobStore,
        schedules: ScheduleStore,
        outbox: OutboxStore,
        generations: IndexGenerationStore,
        repository: ObjectIndexRepository,
        *,
        interval_seconds: float = 30.0,
        retention_seconds: float = 7 * 86_400.0,
        scrub_interval_seconds: float = 3600.0,
        batch_size: int = 100,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not math.isfinite(interval_seconds) or not 1 <= interval_seconds <= 3600:
            raise ValueError("maintenance interval must be between 1 and 3600 seconds")
        if not math.isfinite(retention_seconds) or not 60 <= retention_seconds:
            raise ValueError("maintenance retention must be at least 60 seconds")
        if (
            not math.isfinite(scrub_interval_seconds)
            or scrub_interval_seconds < interval_seconds
        ):
            raise ValueError("scrub interval must be at least the maintenance interval")
        if type(batch_size) is not int or not 1 <= batch_size <= 1000:
            raise ValueError("maintenance batch size must be between 1 and 1000")
        backends = {
            id(jobs.backend),
            id(schedules.backend),
            id(outbox.backend),
            id(generations.backend),
        }
        if len(backends) != 1:
            raise ValueError("HA maintenance stores must share one backend")
        self.jobs = jobs
        self.schedules = schedules
        self.outbox = outbox
        self.generations = generations
        self.repository = repository
        self.interval_seconds = float(interval_seconds)
        self.retention_seconds = float(retention_seconds)
        self.scrub_interval_seconds = float(scrub_interval_seconds)
        self.batch_size = batch_size
        self._clock = clock
        self._monotonic = monotonic
        self._condition = threading.Condition()
        self._thread: threading.Thread | None = None
        self._stopping = False
        self._next_scrub = 0.0
        self._scrub_after: tuple[str, str] | None = None
        self._state_lock = threading.Lock()
        self._state: dict[str, Any] = {
            "last_started_at": None,
            "last_succeeded_at": None,
            "last_error_at": None,
            "last_error": None,
            "runs": 0,
            "failures": 0,
            "jobs_reaped": 0,
            "jobs_pruned": 0,
            "outbox_pruned": 0,
            "fires_pruned": 0,
            "generations_removed": 0,
            "generations_scrubbed": 0,
        }

    def run_once(self) -> dict[str, int]:
        now = self._clock()
        before = now - self.retention_seconds
        with self._state_lock:
            self._state["last_started_at"] = now
        operations: tuple[tuple[str, Callable[[], int]], ...] = (
            (
                "jobs_reaped",
                lambda: self.jobs.reap_expired(limit=self.batch_size),
            ),
            (
                "outbox_pruned",
                lambda: self.outbox.prune_delivered(
                    before=before, limit=self.batch_size
                ),
            ),
            (
                "fires_pruned",
                lambda: self.schedules.prune_delivered(
                    before=before, limit=self.batch_size
                ),
            ),
            (
                "jobs_pruned",
                lambda: self.jobs.prune_terminal(before=before, limit=self.batch_size),
            ),
            ("generations_removed", lambda: self._collect(before)),
            ("generations_scrubbed", self._scrub_if_due),
        )
        result = {key: 0 for key, _operation in operations}
        failures: list[tuple[str, Exception]] = []
        for key, operation in operations:
            try:
                result[key] = operation()
            except Exception as exc:
                if isinstance(exc, _MaintenanceOperationError):
                    result[key] = exc.completed
                failures.append((key, exc))
                LOGGER.exception(
                    "HA maintenance operation failed", extra={"operation": key}
                )
        with self._state_lock:
            for key, value in result.items():
                self._state[key] += value
            if not failures:
                self._state["last_succeeded_at"] = self._clock()
                self._state["last_error"] = None
                self._state["runs"] += 1
        if failures:
            summary = "; ".join(f"{name} ({error})" for name, error in failures)
            raise RuntimeError(
                f"HA maintenance operations failed: {summary}"
            ) from failures[0][1]
        return result

    def _collect(self, before: float) -> int:
        removed = 0
        failures = 0
        for generation in self.generations.garbage_candidates(
            before=before, limit=self.batch_size
        ):
            try:
                self.repository.delete_generation(generation)
                removed += int(
                    self.generations.forget_collectable(
                        str(generation["generation_id"]), before=before
                    )
                )
            except Exception:
                failures += 1
                LOGGER.exception(
                    "HA generation collection failed",
                    extra={
                        "tenant_id": generation["tenant_id"],
                        "kb_id": generation["kb_id"],
                        "generation_id": generation["generation_id"],
                    },
                )
        if failures:
            raise _MaintenanceOperationError(
                f"HA collection failed for {failures} generation(s)",
                completed=removed,
            )
        return removed

    def _scrub_if_due(self) -> int:
        now = self._monotonic()
        if now < self._next_scrub:
            return 0
        rows = self.generations.list_current(
            limit=self.batch_size, after=self._scrub_after
        )
        failures = 0
        for generation in rows:
            try:
                self.repository.verify(generation)
            except Exception:
                failures += 1
                LOGGER.exception(
                    "HA current generation scrub failed",
                    extra={
                        "tenant_id": generation["tenant_id"],
                        "kb_id": generation["kb_id"],
                        "generation_id": generation["generation_id"],
                    },
                )
        if rows and len(rows) == self.batch_size:
            last = rows[-1]
            self._scrub_after = (str(last["tenant_id"]), str(last["kb_id"]))
            self._next_scrub = now
        else:
            self._scrub_after = None
            self._next_scrub = now + self.scrub_interval_seconds
        if failures:
            raise _MaintenanceOperationError(
                f"HA scrub failed for {failures} current generation(s)",
                completed=len(rows) - failures,
            )
        return len(rows)

    def start(self) -> None:
        with self._condition:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stopping = False
            self._thread = threading.Thread(
                target=self._run, name="cogdoc-ha-maintenance", daemon=True
            )
            self._thread.start()

    def wake(self) -> None:
        with self._condition:
            self._condition.notify_all()

    def _run(self) -> None:
        while True:
            with self._condition:
                if self._stopping:
                    return
            try:
                self.run_once()
            except Exception as exc:
                LOGGER.exception("HA maintenance cycle failed")
                with self._state_lock:
                    self._state["last_error_at"] = self._clock()
                    self._state["last_error"] = type(exc).__name__
                    self._state["failures"] += 1
            with self._condition:
                if self._stopping:
                    return
                self._condition.wait(self.interval_seconds)

    def stop(self, *, timeout_seconds: float = 10.0) -> bool:
        with self._condition:
            self._stopping = True
            self._condition.notify_all()
            thread = self._thread
        if thread is not None:
            thread.join(timeout_seconds)
        return thread is None or not thread.is_alive()

    def snapshot(self) -> MaintenanceSnapshot:
        with self._state_lock:
            state = dict(self._state)
        thread = self._thread
        return MaintenanceSnapshot(
            running=thread is not None and thread.is_alive(),
            **state,
        )

    def check(self) -> bool:
        snapshot = self.snapshot()
        return snapshot.running and snapshot.last_error is None


__all__ = ["HAMaintenance", "MaintenanceSnapshot"]
