from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

from cogdoc.connectors.connection_store import ConnectionStore
from cogdoc.connectors.base import ConnectorError
from cogdoc.connectors.factory import build_connector
from cogdoc.connectors.sync_runtime import ConnectorSyncRuntime
from cogdoc.connectors.sync_store import (
    SYNC_COMMITTING,
    SYNC_PENDING,
    SYNC_RETRY_WAIT,
    SYNC_RUNNING,
    ConnectorSyncStore,
)


class SyncManager:
    """Bounded background orchestration for manual, retry, and periodic syncs."""

    def __init__(
        self,
        connection_store: ConnectionStore,
        sync_store: ConnectorSyncStore,
        runtime: ConnectorSyncRuntime,
        sink_builder: Callable[[Mapping[str, Any]], Any],
        *,
        connector_builder: Callable[[Mapping[str, Any]], Any] = build_connector,
        max_workers: int = 2,
    ) -> None:
        self.connection_store = connection_store
        self.sync_store = sync_store
        self.runtime = runtime
        self.sink_builder = sink_builder
        self.connector_builder = connector_builder
        self._max_workers = max_workers
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="cogdoc-sync"
        )
        self._lock = threading.RLock()
        self._futures: dict[str, Future] = {}
        self._timers: set[threading.Timer] = set()
        self._closed = False

    def submit(self, connection_id: str) -> dict[str, Any]:
        connection = self.connection_store.get(connection_id, include_secret_refs=True)
        if connection is None:
            raise KeyError(connection_id)
        if not connection["enabled"]:
            raise ValueError("connection is disabled")
        active = next(
            (
                row
                for row in self.sync_store.list_jobs(
                    connection["tenant_id"],
                    connection["kb_id"],
                    connection_id=connection_id,
                    limit=20,
                )
                if row["status"]
                in {SYNC_PENDING, SYNC_RUNNING, SYNC_COMMITTING, SYNC_RETRY_WAIT}
            ),
            None,
        )
        if active is not None:
            return active
        checkpoint = self.sync_store.checkpoint_for(
            connection["tenant_id"], connection["kb_id"], connection_id
        )
        job = self.sync_store.create(
            tenant_id=connection["tenant_id"],
            kb_id=connection["kb_id"],
            connection_id=connection_id,
            connector_type=connection["connector_type"],
            resume_cursor=checkpoint.get("cursor") if checkpoint else None,
        )
        self._dispatch(job["job_id"], delay=0)
        return job

    def recover(self) -> int:
        # FastAPI application factories (and their tests) may enter the same
        # lifespan more than once. A ThreadPoolExecutor cannot be restarted,
        # so create a fresh bounded worker pool after a clean shutdown.
        with self._lock:
            if self._closed:
                self._executor = ThreadPoolExecutor(
                    max_workers=self._max_workers,
                    thread_name_prefix="cogdoc-sync",
                )
                self._closed = False
        count = 0
        now = time.time()
        recoverable = self.sync_store.recoverable()
        active_connections = {job["connection_id"] for job in recoverable}
        for job in recoverable:
            delay = max(0.0, float(job.get("retry_at") or now) - now)
            if job["status"] in {SYNC_RUNNING, SYNC_COMMITTING}:
                delay = max(
                    delay, max(0.0, float(job.get("lease_expires_at") or now) - now)
                )
            self._dispatch(job["job_id"], delay=delay)
            count += 1
        for connection in self.connection_store.enabled():
            schedule = connection.get("config", {}).get("schedule_seconds")
            if (
                connection["connection_id"] not in active_connections
                and type(schedule) is int
                and 60 <= schedule <= 31_536_000
            ):
                self._schedule_connection(connection["connection_id"], schedule)
        return count

    def cancel(self, job_id: str) -> dict[str, Any]:
        return self.sync_store.request_cancel(job_id)

    def shutdown(self, *, wait: bool = True) -> None:
        with self._lock:
            self._closed = True
            timers = tuple(self._timers)
            self._timers.clear()
        for timer in timers:
            timer.cancel()
        self._executor.shutdown(wait=wait, cancel_futures=True)

    def _dispatch(self, job_id: str, *, delay: float) -> None:
        with self._lock:
            if self._closed:
                return
            if delay > 0:
                timer: threading.Timer

                def launch() -> None:
                    with self._lock:
                        self._timers.discard(timer)
                    self._dispatch(job_id, delay=0)

                timer = threading.Timer(delay, launch)
                timer.daemon = True
                self._timers.add(timer)
                timer.start()
                return
            current = self._futures.get(job_id)
            if current is not None and not current.done():
                return
            future = self._executor.submit(self._run, job_id)
            self._futures[job_id] = future

    def _run(self, job_id: str) -> None:
        job = self.sync_store.get(job_id)
        if job is None:
            return
        connection = self.connection_store.get(
            job["connection_id"], include_secret_refs=True
        )
        if connection is None or not connection["enabled"]:
            self.sync_store.request_cancel(job_id)
            return
        failed_connector_type = str(connection["connector_type"])
        try:
            connector = self.connector_builder(connection)
        except Exception:

            class FailedConnector:
                connector_type = failed_connector_type

                def list_page(self, cursor, *, limit):
                    del cursor, limit
                    raise ConnectorError("connector configuration is unavailable")

                def fetch(self, ref):
                    del ref
                    raise ConnectorError("connector configuration is unavailable")

            connector = FailedConnector()
        sink = self.sink_builder(connection)
        result = self.runtime.run(job_id, connector, sink)
        if result.get("status") == SYNC_RETRY_WAIT:
            self._dispatch(
                job_id,
                delay=max(
                    0.05,
                    float(result.get("retry_at") or time.time()) - time.time(),
                ),
            )
            return
        if result.get("status") == "succeeded":
            schedule = connection.get("config", {}).get("schedule_seconds")
            if type(schedule) is int and 60 <= schedule <= 31_536_000:
                self._schedule_connection(connection["connection_id"], schedule)

    def _schedule_connection(self, connection_id: str, delay: float) -> None:
        with self._lock:
            if self._closed:
                return
            timer: threading.Timer

            def submit_next() -> None:
                with self._lock:
                    self._timers.discard(timer)
                try:
                    self.submit(connection_id)
                except (KeyError, ValueError, RuntimeError):
                    return

            timer = threading.Timer(delay, submit_next)
            timer.daemon = True
            self._timers.add(timer)
            timer.start()
