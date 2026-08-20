from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import Future
from contextlib import contextmanager
from threading import Event, RLock
from cogdoc.daemon_executor import DaemonFutureExecutor


class ResearchPlanningRuntime:
    """Bounded automatic-planning executor with cooperative shutdown control.

    Planning requests own transient controls rather than durable research-job
    leases. Registering those controls behind the same lock as shutdown closes
    the admission race: every admitted request is either visible to shutdown or
    rejected after shutdown begins.
    """

    def __init__(
        self,
        *,
        max_workers: int,
        max_pending: int,
        thread_name_prefix: str = "cogdoc-research-planning",
    ):
        self._max_workers = max_workers
        self._max_pending = max_pending
        self._thread_name_prefix = thread_name_prefix
        self._executor = DaemonFutureExecutor(
            max_workers=max_workers,
            max_pending=max_pending,
            thread_name_prefix=thread_name_prefix,
        )
        self._lock = RLock()
        self._closed = False
        self._active_controls: set[Event] = set()

    def submit(self, function, /, *args, **kwargs) -> Future:
        with self._lock:
            if self._closed:
                raise RuntimeError("ResearchPlanningRuntime is closed")
            return self._executor.submit(function, *args, **kwargs)

    @contextmanager
    def register(self, stop_event: Event) -> Iterator[None]:
        if not isinstance(stop_event, Event):
            raise TypeError("stop_event must be a threading.Event")
        with self._lock:
            if self._closed:
                stop_event.set()
                raise RuntimeError("ResearchPlanningRuntime is closed")
            self._active_controls.add(stop_event)
        try:
            yield
        finally:
            with self._lock:
                self._active_controls.discard(stop_event)

    def shutdown(self, *, wait: bool = False, cancel_futures: bool = True) -> bool:
        with self._lock:
            self._closed = True
            controls = tuple(self._active_controls)
        for control in controls:
            control.set()
        self._executor.shutdown(wait=wait, cancel_futures=cancel_futures)
        return self.is_drained()

    def reopen(self) -> None:
        """Recreate the bounded executor after a fully drained shutdown."""

        with self._lock:
            if not self._closed:
                return
            if self._active_controls or not self._executor.is_drained():
                raise RuntimeError("cannot reopen ResearchPlanningRuntime while active")
            self._executor = DaemonFutureExecutor(
                max_workers=self._max_workers,
                max_pending=self._max_pending,
                thread_name_prefix=self._thread_name_prefix,
            )
            self._closed = False

    def is_drained(self) -> bool:
        with self._lock:
            controls_drained = not self._active_controls
        return controls_drained and self._executor.is_drained()

    @property
    def executor(self) -> DaemonFutureExecutor:
        """Expose the underlying bounded executor for diagnostics/tests only."""

        return self._executor


__all__ = ["ResearchPlanningRuntime"]
