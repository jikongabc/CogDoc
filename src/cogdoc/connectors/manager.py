from __future__ import annotations

import heapq
import math
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import nullcontext
from typing import Any

from cogdoc.connectors.base import (
    ConnectorError,
    RetryableConnectorError,
    SyncCancelled,
)
from cogdoc.connectors.connection_store import ConnectionStore
from cogdoc.connectors.factory import build_connector
from cogdoc.connectors.sync_runtime import ConnectorSyncRuntime
from cogdoc.connectors.sync_store import (
    SYNC_COMMITTING,
    SYNC_DEAD_LETTER,
    SYNC_FAILED,
    SYNC_PENDING,
    SYNC_RETRY_WAIT,
    SYNC_RUNNING,
    SYNC_SUCCEEDED,
    SYNC_TERMINAL,
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
        credential_snapshotter: Callable[[Mapping[str, Any]], tuple[str | None, int]]
        | None = None,
        job_admission_checker: Callable[[Mapping[str, Any], Mapping[str, Any]], bool]
        | None = None,
        cleanup_callback: Callable[[Mapping[str, Any]], None] | None = None,
        maintenance_callback: Callable[[], None] | None = None,
        execution_context: Callable[[Mapping[str, Any]], Any] | None = None,
        terminal_retention_seconds: float = 30 * 86_400,
        maintenance_interval_seconds: float = 3_600,
        maintenance_max_items: int = 10_000,
        max_workers: int = 2,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.connection_store = connection_store
        self.sync_store = sync_store
        self.runtime = runtime
        self.sink_builder = sink_builder
        self.connector_builder = connector_builder
        self.credential_snapshotter = (
            credential_snapshotter or self._default_credential_snapshot
        )
        self.job_admission_checker = (
            job_admission_checker or self._default_job_admission
        )
        self.cleanup_callback = cleanup_callback
        self.maintenance_callback = maintenance_callback
        self.execution_context = execution_context or (lambda _job: nullcontext())
        self._execution_context_explicit = execution_context is not None
        if terminal_retention_seconds <= 0 or maintenance_interval_seconds <= 0:
            raise ValueError("sync maintenance intervals must be positive")
        if not 1 <= maintenance_max_items <= 100_000:
            raise ValueError("maintenance_max_items must be between 1 and 100000")
        self._terminal_retention_seconds = float(terminal_retention_seconds)
        self._maintenance_interval_seconds = float(maintenance_interval_seconds)
        self._maintenance_max_items = maintenance_max_items
        self._clock = clock
        self._max_workers = max_workers
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="cogdoc-sync"
        )
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._futures: dict[str, Future] = {}
        self._schedule_heap: list[tuple[float, int, str, str]] = []
        self._job_schedules: dict[str, int] = {}
        self._connection_schedules: dict[str, int] = {}
        self._maintenance_schedules: dict[str, int] = {}
        self._maintenance_future: Future | None = None
        self._schedule_token = 0
        self._scheduler_thread: threading.Thread | None = None
        self._scheduler_stop = False
        self._control_plane_started = False
        self._closed = False

    def bind_execution_context(
        self, execution_context: Callable[[Mapping[str, Any]], Any]
    ) -> None:
        """Bind the trusted HA lease/rebase context before any dispatch."""

        if not callable(execution_context):
            raise TypeError("sync execution context must be callable")
        with self._lock:
            if self._control_plane_started or self._futures:
                raise RuntimeError("cannot bind sync execution context after startup")
            if (
                self._execution_context_explicit
                and self.execution_context is not execution_context
            ):
                raise RuntimeError("sync execution context is already bound")
            self.execution_context = execution_context
            self._execution_context_explicit = True

    @staticmethod
    def _default_credential_snapshot(
        connection: Mapping[str, Any],
    ) -> tuple[str | None, int]:
        if connection.get("credential_id") is not None:
            raise RuntimeError("credential snapshotter is required")
        return None, 0

    @staticmethod
    def _default_job_admission(
        job: Mapping[str, Any], connection: Mapping[str, Any]
    ) -> bool:
        return bool(
            job.get("credential_id") is None
            and connection.get("credential_id") is None
            and int(job.get("credential_revision") or 0) == 0
        )

    def bind_control_plane(
        self,
        *,
        observer,
        continuation_checker: Callable[[Mapping[str, Any]], bool],
        credential_snapshotter: Callable[[Mapping[str, Any]], tuple[str | None, int]],
        job_admission_checker: Callable[[Mapping[str, Any], Mapping[str, Any]], bool],
        cleanup_callback: Callable[[Mapping[str, Any]], None],
        maintenance_callback: Callable[[], None],
        terminal_retention_seconds: float,
    ) -> None:
        """Bind app authority and observation dependencies before first use."""

        if not all(
            callable(callback)
            for callback in (
                continuation_checker,
                credential_snapshotter,
                job_admission_checker,
                cleanup_callback,
                maintenance_callback,
            )
        ):
            raise TypeError("sync manager control-plane callbacks must be callable")
        with self._lock:
            if self._control_plane_started or self._futures:
                raise RuntimeError("sync manager control plane is already active")
            self.credential_snapshotter = credential_snapshotter
            self.job_admission_checker = job_admission_checker
            self.cleanup_callback = cleanup_callback
            self.maintenance_callback = maintenance_callback
            if terminal_retention_seconds <= 0:
                raise ValueError("terminal_retention_seconds must be positive")
            self._terminal_retention_seconds = float(terminal_retention_seconds)
            bind_runtime = getattr(self.runtime, "bind_controls", None)
            if not callable(bind_runtime):
                raise TypeError("sync runtime does not support control-plane binding")
            bind_runtime(observer=observer, continuation_checker=continuation_checker)

    def _snapshot_credential(
        self, connection: Mapping[str, Any]
    ) -> tuple[str | None, int]:
        credential_id, revision = self.credential_snapshotter(connection)
        expected_id = connection.get("credential_id")
        if expected_id is None:
            if credential_id is not None or revision != 0:
                raise ValueError("credential snapshot does not match connection")
            return None, 0
        if str(credential_id or "") != str(expected_id):
            raise ValueError("credential snapshot does not match connection")
        if type(revision) is not int or revision < 1:
            raise ValueError("credential snapshot revision is invalid")
        return str(credential_id), revision

    def submit(self, connection_id: str) -> dict[str, Any]:
        with self._lock:
            self._control_plane_started = True
            connection = self.connection_store.get(
                connection_id, include_secret_refs=True
            )
            if connection is None:
                raise KeyError(connection_id)
            if not connection["enabled"]:
                raise ValueError("connection is disabled")
            credential_id, credential_revision = self._snapshot_credential(connection)
            checkpoint = self.sync_store.checkpoint_for(
                connection["tenant_id"], connection["kb_id"], connection_id
            )
            job = self.sync_store.create_if_idle(
                tenant_id=connection["tenant_id"],
                kb_id=connection["kb_id"],
                connection_id=connection_id,
                connector_type=connection["connector_type"],
                connection_revision=int(connection["revision"]),
                credential_id=credential_id,
                credential_revision=credential_revision,
                resume_cursor=checkpoint.get("cursor") if checkpoint else None,
            )
            schedule = connection.get("config", {}).get("schedule_seconds")
            if type(schedule) is int and 60 <= schedule <= 31_536_000:
                self._cancel_schedule_timer(connection_id)
                self.sync_store.ensure_schedule(
                    tenant_id=connection["tenant_id"],
                    kb_id=connection["kb_id"],
                    connection_id=connection_id,
                    schedule_seconds=schedule,
                )
                self.sync_store.set_next_run(
                    connection["tenant_id"], connection["kb_id"], connection_id, None
                )
            if job["status"] == SYNC_PENDING:
                self._dispatch(job["job_id"], delay=0)
            return job

    def replay(self, job_id: str) -> dict[str, Any]:
        """Replay a dead-letter as a new job; never mutate the original."""

        with self._lock:
            self._control_plane_started = True
            original = self.sync_store.get(job_id)
            if original is None:
                raise KeyError(job_id)
            connection = self.connection_store.get(
                original["connection_id"], include_secret_refs=True
            )
            if connection is None or not connection["enabled"]:
                raise ValueError("connection is unavailable or disabled")
            credential_id, credential_revision = self._snapshot_credential(connection)
            replay = self.sync_store.replay_dead_letter(
                job_id,
                connection_revision=int(connection["revision"]),
                credential_id=credential_id,
                credential_revision=credential_revision,
            )
            self._cancel_schedule_timer(connection["connection_id"])
            self.sync_store.set_next_run(
                connection["tenant_id"],
                connection["kb_id"],
                connection["connection_id"],
                None,
            )
            self._dispatch(replay["job_id"], delay=0)
            return replay

    def health(self, connection_id: str) -> dict[str, Any]:
        connection = self.connection_store.get(connection_id)
        if connection is None:
            raise KeyError(connection_id)
        return self.sync_store.health_snapshot(
            connection["tenant_id"], connection["kb_id"], connection_id
        )

    def recover(self) -> int:
        # FastAPI application factories (and their tests) may enter the same
        # lifespan more than once. A ThreadPoolExecutor cannot be restarted,
        # so create a fresh bounded worker pool after a clean shutdown.
        with self._lock:
            self._control_plane_started = True
            if self._closed:
                self._executor = ThreadPoolExecutor(
                    max_workers=self._max_workers,
                    thread_name_prefix="cogdoc-sync",
                )
                self._closed = False
                self._scheduler_stop = False
        count = 0
        now = self._clock()
        # Repair the externally visible catalog before retention can prune the
        # only terminal ledger row that closes a prior crash window.
        for terminal in self.sync_store.latest_terminal_jobs():
            self.runtime.reconcile(terminal)
        self._run_maintenance()
        active_connections: set[str] = set()
        after_sequence = 0
        while True:
            recoverable = self.sync_store.recoverable(
                limit=1000, after_sequence=after_sequence
            )
            if not recoverable:
                break
            for job in recoverable:
                active_connections.add(str(job["connection_id"]))
                delay = max(0.0, float(job.get("retry_at") or now) - now)
                if job["status"] in {SYNC_RUNNING, SYNC_COMMITTING}:
                    delay = max(
                        delay,
                        max(
                            0.0,
                            float(job.get("lease_expires_at") or now) - now,
                        ),
                    )
                self._dispatch(job["job_id"], delay=delay)
                count += 1
            after_sequence = int(recoverable[-1]["job_sequence"])
            if len(recoverable) < 1000:
                break
        for connection in self.connection_store.enabled():
            schedule = connection.get("config", {}).get("schedule_seconds")
            if type(schedule) is int and 60 <= schedule <= 31_536_000:
                health = self.sync_store.ensure_schedule(
                    tenant_id=connection["tenant_id"],
                    kb_id=connection["kb_id"],
                    connection_id=connection["connection_id"],
                    schedule_seconds=schedule,
                )
                if connection["connection_id"] in active_connections:
                    self.sync_store.set_next_run(
                        connection["tenant_id"],
                        connection["kb_id"],
                        connection["connection_id"],
                        None,
                    )
                else:
                    next_run_at = float(health.get("next_run_at") or now)
                    self._schedule_connection(
                        connection["connection_id"], next_run_at=next_run_at
                    )
            else:
                self.sync_store.clear_schedule(
                    connection["tenant_id"],
                    connection["kb_id"],
                    connection["connection_id"],
                )
        with self._lock:
            self._enqueue_locked(
                "maintenance",
                "connector-maintenance",
                self._clock() + self._maintenance_interval_seconds,
                replace=True,
            )
        return count

    def cancel(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            previous = self.sync_store.get(job_id)
            result = self.sync_store.request_cancel(job_id)
            if result.get("status") != "cancelled":
                return result
            self._cancel_job_timer(job_id)
            self.sync_store.record_health(job_id, duration_seconds=0.0)
            if previous is None or previous.get("status") != "cancelled":
                self.runtime.observe_cancelled(result)
            connection = self.connection_store.get(str(result["connection_id"]))
            if connection is None or not connection["enabled"]:
                return result
            schedule = connection.get("config", {}).get("schedule_seconds")
            if type(schedule) is int and 60 <= schedule <= 31_536_000:
                next_run_at = self._clock() + schedule
                self.sync_store.ensure_schedule(
                    tenant_id=connection["tenant_id"],
                    kb_id=connection["kb_id"],
                    connection_id=connection["connection_id"],
                    schedule_seconds=schedule,
                )
                self.sync_store.set_next_run(
                    connection["tenant_id"],
                    connection["kb_id"],
                    connection["connection_id"],
                    next_run_at,
                )
                self._schedule_connection(
                    connection["connection_id"], next_run_at=next_run_at
                )
            return result

    def set_connection_enabled(
        self, connection_id: str, enabled: bool
    ) -> dict[str, Any]:
        """Serialize admission with connection revocation."""

        with self._lock:
            connection = self.connection_store.get(
                connection_id, include_secret_refs=True
            )
            if connection is None:
                raise KeyError(connection_id)
            if not enabled:
                outcome = self.sync_store.cancel_connection(
                    connection["tenant_id"], connection["kb_id"], connection_id
                )
                if outcome["committing"]:
                    raise ValueError(
                        "connection has a commit in progress; retry after it completes"
                    )
                self._cancel_job_timer_for_connection(connection_id)
                self._cancel_schedule_timer(connection_id)
                self._reconcile_latest_for_connection(connection_id)
            updated = self.connection_store.set_enabled(connection_id, enabled)
            if enabled:
                schedule = updated.get("config", {}).get("schedule_seconds")
                if type(schedule) is int and 60 <= schedule <= 31_536_000:
                    health = self.sync_store.ensure_schedule(
                        tenant_id=updated["tenant_id"],
                        kb_id=updated["kb_id"],
                        connection_id=connection_id,
                        schedule_seconds=schedule,
                    )
                    next_run_at = float(
                        health.get("next_run_at") or self._clock() + schedule
                    )
                    self.sync_store.set_next_run(
                        updated["tenant_id"],
                        updated["kb_id"],
                        connection_id,
                        next_run_at,
                    )
                    self._schedule_connection(connection_id, next_run_at=next_run_at)
            return updated

    def delete_connection(self, connection_id: str) -> bool:
        """Delete only after every revocable job is durably cancelled."""

        with self._lock:
            connection = self.connection_store.get(
                connection_id, include_secret_refs=True
            )
            if connection is None:
                raise KeyError(connection_id)
            outcome = self.sync_store.cancel_connection(
                connection["tenant_id"], connection["kb_id"], connection_id
            )
            if outcome["committing"]:
                raise ValueError(
                    "connection has a commit in progress; retry after it completes"
                )
            self._cancel_job_timer_for_connection(connection_id)
            self._cancel_schedule_timer(connection_id)
            self._reconcile_latest_for_connection(connection_id)
            return self.connection_store.delete(connection_id)

    def prepare_connection_delete(
        self,
        tenant_id: str,
        kb_id: str,
        connection_id: str,
        *,
        timeout_seconds: float = 30.0,
    ) -> dict[str, Any]:
        """Fence one connection and wait for every pre-commit worker to stop.

        The durable definition remains present and disabled so the caller can
        clean connection-owned data before deleting the row. A commit that has
        already crossed the visibility boundary is never cancelled, but the
        connection remains fenced while the caller retries deletion later.
        """

        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        fenced = self.fence_connection_delete(tenant_id, kb_id, connection_id)
        return self.drain_connection_delete(
            tenant_id,
            kb_id,
            connection_id,
            timeout_seconds=timeout_seconds,
            cancelled=int(fenced["cancelled"]),
        )

    def fence_connection_delete(
        self,
        tenant_id: str,
        kb_id: str,
        connection_id: str,
    ) -> dict[str, Any]:
        """Durably fence admission and request cancellation without waiting."""

        tenant = str(tenant_id or "").strip()
        knowledge_base = str(kb_id or "").strip()
        connection_key = str(connection_id or "").strip()
        if not tenant or not knowledge_base or not connection_key:
            raise ValueError("connection delete scope is required")
        with self._lock:
            connection = self.connection_store.get(
                connection_key, include_secret_refs=True
            )
            if connection is None:
                raise KeyError(connection_key)
            if (
                str(connection["tenant_id"]) != tenant
                or str(connection["kb_id"]) != knowledge_base
            ):
                raise ValueError("connection does not belong to knowledge base")
            outcome = self.sync_store.cancel_connection(
                tenant, knowledge_base, connection_key
            )
            disabled = self.connection_store.fence_delete(
                tenant,
                knowledge_base,
                connection_key,
            )
            self._cancel_job_timer_for_connection(connection_key)
            self._cancel_schedule_timer(connection_key)
            self.sync_store.clear_schedule(tenant, knowledge_base, connection_key)
            self._reconcile_latest_for_connection(connection_key)
            for job_id, future in tuple(self._futures.items()):
                job = self.sync_store.get(job_id)
                if (
                    job is not None
                    and job["tenant_id"] == tenant
                    and job["kb_id"] == knowledge_base
                    and job["connection_id"] == connection_key
                ):
                    future.cancel()
            if outcome["committing"]:
                raise ValueError("connection has a commit in progress")
        return {
            "connection": disabled,
            "cancelled": int(outcome["cancelled"]),
            "remaining": int(outcome.get("running") or 0),
        }

    def drain_connection_delete(
        self,
        tenant_id: str,
        kb_id: str,
        connection_id: str,
        *,
        timeout_seconds: float = 30.0,
        cancelled: int = 0,
    ) -> dict[str, Any]:
        """Wait outside control-plane locks for the fenced workers to stop."""

        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        tenant = str(tenant_id or "").strip()
        knowledge_base = str(kb_id or "").strip()
        connection_key = str(connection_id or "").strip()
        if not tenant or not knowledge_base or not connection_key:
            raise ValueError("connection delete scope is required")
        if type(cancelled) is not int or cancelled < 0:
            raise ValueError("cancelled must be a non-negative integer")
        deadline = time.monotonic() + timeout_seconds
        while True:
            activity = self.sync_store.connection_activity(
                tenant, knowledge_base, connection_key
            )
            if activity["total"] == 0:
                connection = self.connection_store.get(
                    connection_key, include_secret_refs=True
                )
                if connection is None:
                    raise KeyError(connection_key)
                if (
                    connection["tenant_id"] != tenant
                    or connection["kb_id"] != knowledge_base
                    or connection["enabled"]
                    or not connection.get("deleting")
                ):
                    raise ValueError("connection deletion fence is not active")
                return {
                    "connection": connection,
                    "cancelled": cancelled,
                    "remaining": 0,
                }
            retry = self.sync_store.cancel_connection(
                tenant, knowledge_base, connection_key
            )
            cancelled += int(retry["cancelled"])
            if retry["committing"]:
                raise ValueError("connection has a commit in progress")
            if time.monotonic() >= deadline:
                raise TimeoutError("connector job did not quiesce before deletion")
            time.sleep(0.01)

    def prepare_knowledge_base_delete(
        self,
        tenant_id: str,
        kb_id: str,
        *,
        timeout_seconds: float = 30.0,
    ) -> dict[str, Any]:
        """Fence every connector mutation and wait for pre-commit work to stop.

        A committing job owns a visibility transition and cannot be revoked
        safely. In that case no sync row is mutated and the caller must retry
        the KB deletion after recovery completes.
        """

        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        with self._lock:
            connections = self.connection_store.list_entries(tenant_id, kb_id)
            previously_enabled_connection_ids = sorted(
                str(connection["connection_id"])
                for connection in connections
                if connection["enabled"]
            )
            outcome = self.sync_store.cancel_scope(tenant_id, kb_id)
            if outcome["committing"]:
                raise ValueError("knowledge base has a connector commit in progress")
            self.connection_store.disable_scope(tenant_id, kb_id)
            connection_ids = {
                str(connection["connection_id"]) for connection in connections
            }
            for connection_id in connection_ids:
                self._cancel_schedule_timer(connection_id)
                self._cancel_job_timer_for_connection(connection_id)
                self._reconcile_latest_for_connection(connection_id)
            for job_id, future in tuple(self._futures.items()):
                job = self.sync_store.get(job_id)
                if (
                    job is not None
                    and job["tenant_id"] == tenant_id
                    and job["kb_id"] == kb_id
                ):
                    future.cancel()

        deadline = time.monotonic() + timeout_seconds
        while True:
            activity = self.sync_store.scope_activity(tenant_id, kb_id)
            if activity["total"] == 0:
                return {
                    "connections": len(connections),
                    "cancelled": int(outcome["cancelled"]),
                    "previously_enabled_connection_ids": (
                        previously_enabled_connection_ids
                    ),
                }
            # Leases whose workers crashed after the initial fence become
            # terminal on the next atomic cancellation pass.
            retry = self.sync_store.cancel_scope(tenant_id, kb_id)
            if retry["committing"]:
                raise ValueError("knowledge base has a connector commit in progress")
            if time.monotonic() >= deadline:
                raise TimeoutError("connector jobs did not quiesce before deletion")
            time.sleep(0.01)

    def restore_knowledge_base_delete(
        self,
        tenant_id: str,
        kb_id: str,
        previously_enabled_connection_ids: Iterable[str],
    ) -> dict[str, int]:
        """Restore only connections enabled before a rolled-back KB deletion."""

        connection_ids = tuple(
            dict.fromkeys(
                str(connection_id).strip()
                for connection_id in previously_enabled_connection_ids
                if str(connection_id).strip()
            )
        )
        with self._lock:
            connections = []
            for connection_id in connection_ids:
                connection = self.connection_store.get(
                    connection_id, include_secret_refs=True
                )
                if (
                    connection is None
                    or connection["tenant_id"] != tenant_id
                    or connection["kb_id"] != kb_id
                ):
                    raise ValueError(
                        "restore connection does not belong to knowledge base"
                    )
                connections.append(connection)
            scheduled = 0
            for connection in connections:
                updated = self.connection_store.set_enabled(
                    str(connection["connection_id"]), True
                )
                schedule = updated.get("config", {}).get("schedule_seconds")
                if type(schedule) is int and 60 <= schedule <= 31_536_000:
                    health = self.sync_store.ensure_schedule(
                        tenant_id=tenant_id,
                        kb_id=kb_id,
                        connection_id=str(updated["connection_id"]),
                        schedule_seconds=schedule,
                    )
                    next_run_at = float(
                        health.get("next_run_at") or self._clock() + schedule
                    )
                    self.sync_store.set_next_run(
                        tenant_id,
                        kb_id,
                        str(updated["connection_id"]),
                        next_run_at,
                    )
                    self._schedule_connection(
                        str(updated["connection_id"]), next_run_at=next_run_at
                    )
                    scheduled += 1
            return {"restored": len(connections), "scheduled": scheduled}

    def purge_knowledge_base(self, tenant_id: str, kb_id: str) -> dict[str, int]:
        """Remove fenced connector definitions and durable sync state."""

        with self._lock:
            activity = self.sync_store.scope_activity(tenant_id, kb_id)
            if activity["total"]:
                raise ValueError("knowledge base still has active sync jobs")
            after_sequence = 0
            while True:
                pending = self.sync_store.cleanup_pending(
                    limit=1000,
                    after_sequence=after_sequence,
                    tenant_id=tenant_id,
                    kb_id=kb_id,
                )
                if not pending:
                    break
                failed = [
                    str(job["job_id"])
                    for job in pending
                    if not self._cleanup_terminal_job(job)
                ]
                if failed:
                    raise RuntimeError("knowledge base has pending connector cleanup")
                after_sequence = int(pending[-1]["job_sequence"])
            connections = self.connection_store.delete_scope(tenant_id, kb_id)
            result = self.sync_store.delete_scope(tenant_id, kb_id)
        return {"connections": connections, **result}

    def shutdown(self, *, wait: bool = True) -> None:
        with self._lock:
            self._closed = True
            self._scheduler_stop = True
            self._job_schedules.clear()
            self._connection_schedules.clear()
            self._maintenance_schedules.clear()
            self._schedule_heap.clear()
            scheduler = self._scheduler_thread
            self._condition.notify_all()
        if scheduler is not None and scheduler is not threading.current_thread():
            scheduler.join(timeout=5)
        self._executor.shutdown(wait=wait, cancel_futures=True)

    def _dispatch(
        self, job_id: str, *, delay: float, replace_timer: bool = False
    ) -> None:
        with self._lock:
            if self._closed:
                return
            if delay > 0:
                if job_id in self._job_schedules and not replace_timer:
                    return
                self._enqueue_locked("job", job_id, self._clock() + delay, replace=True)
                return
            if self._job_schedules.pop(job_id, None) is not None:
                self._compact_schedules_locked()
                self._condition.notify_all()
            current = self._futures.get(job_id)
            if current is not None and not current.done():
                return
            future = self._executor.submit(self._run, job_id)
            self._futures[job_id] = future
            future.add_done_callback(
                lambda completed: self._forget_future(job_id, completed)
            )

    def _forget_future(self, job_id: str, completed: Future) -> None:
        with self._lock:
            if self._futures.get(job_id) is completed:
                self._futures.pop(job_id, None)
                self._condition.notify_all()

    def _run(self, job_id: str) -> None:
        job = self.sync_store.get(job_id)
        if job is None or job.get("status") in {
            SYNC_SUCCEEDED,
            SYNC_FAILED,
            SYNC_DEAD_LETTER,
            "cancelled",
        }:
            return
        connection = self.connection_store.get(
            str(job["connection_id"]), include_secret_refs=True
        )
        if job.get("status") != SYNC_COMMITTING and not self._job_is_admitted(
            job, connection
        ):
            self._cancel_before_build(job)
            return
        try:
            with self.execution_context(job):
                self._run_in_context(job_id)
        except Exception:
            # Distributed KB leases are intentionally non-blocking. Another
            # writer may own the KB for a short document/index mutation; keep
            # the durable job runnable and retry instead of losing its only
            # dispatch future. Persistent integrity/configuration failures are
            # likewise bounded by this single scheduler entry and stay visible.
            current = self.sync_store.get(job_id)
            if current is not None and current.get("status") not in SYNC_TERMINAL:
                self._dispatch(job_id, delay=1.0, replace_timer=True)

    def _run_in_context(self, job_id: str) -> None:
        job = self.sync_store.get(job_id)
        if job is None or job.get("status") in {
            SYNC_SUCCEEDED,
            SYNC_FAILED,
            SYNC_DEAD_LETTER,
            "cancelled",
        }:
            return
        connection = self.connection_store.get(
            job["connection_id"], include_secret_refs=True
        )
        scope_matches = self._connection_scope_matches(job, connection)
        connector: Any
        if job["status"] == SYNC_COMMITTING:
            # Commit recovery already crossed the provider authority boundary.
            # It still requires the original scoped sink, but never credentials.
            if not scope_matches:
                return

            commit_connector_type = str(job["connector_type"])

            class CommitRecoveryConnector:
                connector_type = commit_connector_type

                def list_page(self, cursor, *, limit):  # pragma: no cover
                    del cursor, limit
                    raise AssertionError("commit recovery must not list provider pages")

                def fetch(self, ref):  # pragma: no cover
                    del ref
                    raise AssertionError("commit recovery must not fetch provider data")

            connector = CommitRecoveryConnector()
        elif not self._job_is_admitted(job, connection):
            self._cancel_before_build(job)
            return
        else:
            assert connection is not None
            failed_connector_type = str(connection["connector_type"])
            build_connection = dict(connection)
            build_connection["sync_job_id"] = str(job["job_id"])
            build_connection["sync_connection_revision"] = int(
                job.get("connection_revision") or 0
            )
            build_connection["sync_credential_id"] = job.get("credential_id")
            build_connection["sync_credential_revision"] = int(
                job.get("credential_revision") or 0
            )
            try:
                connector = self.connector_builder(build_connection)
            except SyncCancelled:
                self._cancel_before_build(job)
                return
            except Exception as build_error:
                retryable_build_error = isinstance(build_error, RetryableConnectorError)

                class FailedConnector:
                    connector_type = failed_connector_type

                    def list_page(self, cursor, *, limit):
                        del cursor, limit
                        if retryable_build_error:
                            raise RetryableConnectorError(
                                "connector configuration is temporarily unavailable"
                            )
                        raise ConnectorError("connector configuration is unavailable")

                    def fetch(self, ref):
                        del ref
                        raise ConnectorError("connector configuration is unavailable")

                connector = FailedConnector()
        assert connection is not None
        try:
            sink = self.sink_builder(connection)
        except Exception:

            class FailedSink:
                def begin(self, **scope):
                    del scope
                    raise RetryableConnectorError("sync sink is unavailable")

                def abort(self):
                    return None

            sink = FailedSink()
        result = self.runtime.run(job_id, connector, sink)
        latest_connection = self.connection_store.get(
            str(connection["connection_id"]), include_secret_refs=True
        )
        if latest_connection is None or not latest_connection["enabled"]:
            self._cancel_job_timer(job_id)
            self._cancel_schedule_timer(str(connection["connection_id"]))
            self.sync_store.clear_schedule(
                str(connection["tenant_id"]),
                str(connection["kb_id"]),
                str(connection["connection_id"]),
            )
            return
        connection = latest_connection
        if (
            result.get("status") in {SYNC_RETRY_WAIT, SYNC_COMMITTING}
            and result.get("retry_at") is not None
        ):
            self._dispatch(
                job_id,
                delay=max(
                    0.05,
                    float(result.get("retry_at") or self._clock()) - self._clock(),
                ),
                replace_timer=True,
            )
            return
        if result.get("status") in {
            SYNC_SUCCEEDED,
            SYNC_FAILED,
            "cancelled",
        }:
            self._cancel_job_timer(job_id)
            schedule = connection.get("config", {}).get("schedule_seconds")
            if type(schedule) is int and 60 <= schedule <= 31_536_000:
                next_run_at = self._clock() + schedule
                self.sync_store.ensure_schedule(
                    tenant_id=connection["tenant_id"],
                    kb_id=connection["kb_id"],
                    connection_id=connection["connection_id"],
                    schedule_seconds=schedule,
                )
                self.sync_store.set_next_run(
                    connection["tenant_id"],
                    connection["kb_id"],
                    connection["connection_id"],
                    next_run_at,
                )
                self._schedule_connection(
                    connection["connection_id"], next_run_at=next_run_at
                )
        elif result.get("status") == SYNC_DEAD_LETTER:
            self._cancel_job_timer(job_id)
            self._cancel_schedule_timer(connection["connection_id"])
            self.sync_store.set_next_run(
                connection["tenant_id"],
                connection["kb_id"],
                connection["connection_id"],
                None,
            )

    @staticmethod
    def _connection_scope_matches(
        job: Mapping[str, Any], connection: Mapping[str, Any] | None
    ) -> bool:
        return bool(
            connection is not None
            and str(connection.get("connection_id")) == str(job["connection_id"])
            and str(connection.get("tenant_id")) == str(job["tenant_id"])
            and str(connection.get("kb_id")) == str(job["kb_id"])
            and str(connection.get("connector_type")) == str(job["connector_type"])
        )

    def _job_is_admitted(
        self, job: Mapping[str, Any], connection: Mapping[str, Any] | None
    ) -> bool:
        if not self._connection_scope_matches(job, connection):
            return False
        assert connection is not None
        if not connection.get("enabled"):
            return False
        if int(connection.get("revision") or 0) != int(
            job.get("connection_revision") or 0
        ):
            return False
        if (connection.get("credential_id") or None) != (
            job.get("credential_id") or None
        ):
            return False
        try:
            return bool(self.job_admission_checker(job, connection))
        except Exception:
            return False

    def _cancel_before_build(self, job: Mapping[str, Any]) -> None:
        result = self.sync_store.request_cancel(str(job["job_id"]))
        if result.get("status") == "cancelled":
            self.runtime.observe_cancelled(result)
            return
        lease_expires_at = float(result.get("lease_expires_at") or self._clock())
        self._dispatch(
            str(job["job_id"]),
            delay=max(0.05, lease_expires_at - self._clock()),
            replace_timer=True,
        )

    def _reconcile_latest_for_connection(self, connection_id: str) -> None:
        health = None
        connection = self.connection_store.get(connection_id)
        if connection is not None:
            health = self.sync_store.health_snapshot(
                str(connection["tenant_id"]),
                str(connection["kb_id"]),
                connection_id,
            )
        if health is None or not health.get("last_job_id"):
            return
        job = self.sync_store.get(str(health["last_job_id"]))
        if job is not None:
            self.runtime.reconcile(job)

    def _cancel_job_timer(self, job_id: str) -> None:
        with self._lock:
            if self._job_schedules.pop(job_id, None) is not None:
                self._compact_schedules_locked()
                self._condition.notify_all()

    def _cancel_job_timer_for_connection(self, connection_id: str) -> None:
        with self._lock:
            job_ids = tuple(self._job_schedules)
        for job_id in job_ids:
            job = self.sync_store.get(job_id)
            if job is not None and job["connection_id"] == connection_id:
                self._cancel_job_timer(job_id)

    def _cancel_schedule_timer(self, connection_id: str) -> None:
        with self._lock:
            if self._connection_schedules.pop(connection_id, None) is not None:
                self._compact_schedules_locked()
                self._condition.notify_all()

    def _schedule_connection(self, connection_id: str, *, next_run_at: float) -> None:
        with self._lock:
            if self._closed:
                return
            self._enqueue_locked("connection", connection_id, next_run_at, replace=True)

    def _enqueue_locked(
        self,
        kind: str,
        key: str,
        due_at: float,
        *,
        replace: bool,
    ) -> int:
        schedules = self._schedules_for_kind(kind)
        if key in schedules and not replace:
            return schedules[key]
        self._schedule_token += 1
        token = self._schedule_token
        schedules[key] = token
        heapq.heappush(self._schedule_heap, (max(0.0, float(due_at)), token, kind, key))
        self._compact_schedules_locked()
        self._start_scheduler_locked()
        self._condition.notify_all()
        return token

    def _start_scheduler_locked(self) -> None:
        if self._closed:
            return
        if self._scheduler_thread is not None and self._scheduler_thread.is_alive():
            return
        self._scheduler_stop = False
        self._scheduler_thread = threading.Thread(
            target=self._scheduler_loop,
            name="cogdoc-sync-scheduler",
            daemon=True,
        )
        self._scheduler_thread.start()

    def _compact_schedules_locked(self) -> None:
        active = (
            len(self._job_schedules)
            + len(self._connection_schedules)
            + len(self._maintenance_schedules)
        )
        if len(self._schedule_heap) <= max(64, active * 2):
            return
        self._schedule_heap = [
            entry
            for entry in self._schedule_heap
            if (self._schedules_for_kind(entry[2]).get(entry[3])) == entry[1]
        ]
        heapq.heapify(self._schedule_heap)

    def _scheduler_loop(self) -> None:
        while True:
            with self._condition:
                if self._scheduler_stop:
                    return
                while self._schedule_heap:
                    due_at, token, kind, key = self._schedule_heap[0]
                    schedules = self._schedules_for_kind(kind)
                    if schedules.get(key) != token:
                        heapq.heappop(self._schedule_heap)
                        continue
                    delay = due_at - self._clock()
                    if delay > 0:
                        self._condition.wait(timeout=min(delay, 60.0))
                        break
                    heapq.heappop(self._schedule_heap)
                    try:
                        self._fire_schedule_locked(kind, key, token)
                    except Exception:
                        # A single connector/configuration failure must not kill
                        # the sole scheduler for every other connection.
                        pass
                    break
                else:
                    self._condition.wait()

    def _fire_schedule_locked(self, kind: str, key: str, token: int) -> None:
        schedules = self._schedules_for_kind(kind)
        if schedules.get(key) != token:
            return
        schedules.pop(key, None)
        if self._closed:
            return
        if kind == "job":
            current = self._futures.get(key)
            if current is not None and not current.done():
                self._enqueue_locked("job", key, self._clock() + 0.05, replace=True)
                return
            self._dispatch(key, delay=0)
            return
        if kind == "maintenance":
            if (
                self._maintenance_future is not None
                and not self._maintenance_future.done()
            ):
                self._enqueue_locked(
                    "maintenance",
                    key,
                    self._clock() + self._maintenance_interval_seconds,
                    replace=True,
                )
                return
            self._maintenance_future = self._executor.submit(self._run_maintenance)
            self._maintenance_future.add_done_callback(self._maintenance_finished)
            return
        try:
            self.submit(key)
        except (KeyError, ValueError, RuntimeError):
            return

    def _schedules_for_kind(self, kind: str) -> dict[str, int]:
        if kind == "job":
            return self._job_schedules
        if kind == "connection":
            return self._connection_schedules
        if kind == "maintenance":
            return self._maintenance_schedules
        raise ValueError("unsupported sync schedule kind")

    def _maintenance_finished(self, completed: Future) -> None:
        del completed
        with self._lock:
            if self._closed:
                return
            self._enqueue_locked(
                "maintenance",
                "connector-maintenance",
                self._clock() + self._maintenance_interval_seconds,
                replace=True,
            )

    def _run_maintenance(self) -> dict[str, int]:
        attempted = 0
        cleaned = 0
        after_sequence = 0
        while attempted < self._maintenance_max_items:
            limit = min(1000, self._maintenance_max_items - attempted)
            pending = self.sync_store.cleanup_pending(
                limit=limit, after_sequence=after_sequence
            )
            if not pending:
                break
            for job in pending:
                attempted += 1
                if self._cleanup_terminal_job(job):
                    cleaned += 1
            after_sequence = int(pending[-1]["job_sequence"])
            if len(pending) < limit:
                break

        pruned = 0
        while pruned < self._maintenance_max_items:
            batch = min(1000, self._maintenance_max_items - pruned)
            deleted = self.sync_store.prune_terminal_jobs(
                older_than=max(0.0, self._clock() - self._terminal_retention_seconds),
                limit=batch,
            )
            pruned += deleted
            if deleted < batch:
                break
        if self.maintenance_callback is not None:
            try:
                self.maintenance_callback()
            except Exception:
                pass
        return {"cleanup_attempted": attempted, "cleaned": cleaned, "pruned": pruned}

    def _cleanup_terminal_job(self, job: Mapping[str, Any]) -> bool:
        try:
            if self.cleanup_callback is not None:
                self.cleanup_callback(job)
            else:
                connection = self.connection_store.get(
                    str(job["connection_id"]), include_secret_refs=True
                )
                if connection is None:
                    return False
                sink = self.sink_builder(connection)
                cleanup = getattr(sink, "cleanup", None)
                if not callable(cleanup):
                    return False
                cleanup(job)
            self.sync_store.mark_cleanup_complete(str(job["job_id"]))
            return True
        except Exception:
            return False
