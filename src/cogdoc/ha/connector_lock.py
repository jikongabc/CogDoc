from __future__ import annotations

import asyncio
import math
import threading
import time
import uuid
from concurrent.futures import Executor
from typing import Any, Callable

from cogdoc.ha.storage import DatabaseBackend


_EXECUTOR_WATCHDOG_SECONDS = 0.05


async def _run_executor(
    executor: Executor, function: Callable[..., Any], *args: Any
) -> Any:
    """Await executor work even if a threadsafe loop wakeup is lost."""

    concurrent_future = executor.submit(function, *args)
    wrapped_future = asyncio.wrap_future(concurrent_future)
    try:
        while True:
            try:
                return await asyncio.wait_for(
                    asyncio.shield(wrapped_future),
                    timeout=_EXECUTOR_WATCHDOG_SECONDS,
                )
            except TimeoutError:
                if concurrent_future.done():
                    return concurrent_future.result()
    except asyncio.CancelledError:
        wrapped_future.cancel()
        concurrent_future.cancel()
        raise


class DistributedConnectorReferenceLock:
    """Async, lease-backed mutex for cross-store credential references."""

    def __init__(
        self,
        backend: DatabaseBackend,
        *,
        owner_id: str,
        executor_provider: Callable[[], Executor],
        lease_seconds: float = 120.0,
        acquire_timeout_seconds: float = 30.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not owner_id or len(owner_id.encode()) > 255:
            raise ValueError("connector reference lock owner is invalid")
        if (
            not math.isfinite(lease_seconds)
            or not math.isfinite(acquire_timeout_seconds)
            or lease_seconds < 5
            or acquire_timeout_seconds <= 0
        ):
            raise ValueError("connector reference lock timing is invalid")
        self.backend = backend
        self.owner_id = owner_id
        self.executor_provider = executor_provider
        self.lease_seconds = float(lease_seconds)
        self.acquire_timeout_seconds = float(acquire_timeout_seconds)
        self._clock = clock
        self._local_lock = asyncio.Lock()
        self._token: str | None = None
        self._stop: threading.Event | None = None
        self._heartbeat_thread: threading.Thread | None = None
        with backend.transaction(write=True) as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS ha_connector_reference_locks (
                lock_id TEXT PRIMARY KEY,lease_owner TEXT NOT NULL,lease_token TEXT NOT NULL,
                lease_expires_at DOUBLE PRECISION NOT NULL,
                updated_at DOUBLE PRECISION NOT NULL)"""
            )

    def _try_acquire(self, token: str) -> bool:
        marker = self.backend.sql(sqlite="?", postgres="%s")
        now = float(self._clock())
        lock = self.backend.sql(sqlite="", postgres=" FOR UPDATE")
        with self.backend.transaction(write=True) as connection:
            connection.execute(
                "INSERT INTO ha_connector_reference_locks(lock_id,lease_owner,lease_token,"
                f"lease_expires_at,updated_at) VALUES({marker},{marker},{marker},{marker},{marker}) "
                "ON CONFLICT(lock_id) DO NOTHING",
                ("credential-references", "", "", 0.0, now),
            )
            row = connection.execute(
                "SELECT lease_token,lease_expires_at FROM ha_connector_reference_locks "
                f"WHERE lock_id={marker}{lock}",
                ("credential-references",),
            ).fetchone()
            if row is None or (
                float(row["lease_expires_at"]) > now and row["lease_token"] != token
            ):
                return False
            changed = connection.execute(
                "UPDATE ha_connector_reference_locks SET lease_owner="
                f"{marker},lease_token={marker},lease_expires_at={marker},updated_at={marker} "
                f"WHERE lock_id={marker} AND (lease_expires_at<={marker} OR lease_token={marker})",
                (
                    self.owner_id,
                    token,
                    now + self.lease_seconds,
                    now,
                    "credential-references",
                    now,
                    token,
                ),
            )
            return changed.rowcount == 1

    def _heartbeat(self, token: str, stop: threading.Event) -> None:
        interval = max(1.0, self.lease_seconds / 3)
        marker = self.backend.sql(sqlite="?", postgres="%s")
        while not stop.wait(interval):
            now = float(self._clock())
            try:
                with self.backend.transaction(write=True) as connection:
                    changed = connection.execute(
                        "UPDATE ha_connector_reference_locks SET lease_expires_at="
                        f"{marker},updated_at={marker} WHERE lock_id={marker} "
                        f"AND lease_token={marker}",
                        (
                            now + self.lease_seconds,
                            now,
                            "credential-references",
                            token,
                        ),
                    )
                if changed.rowcount != 1:
                    return
            except Exception:
                return

    def _release(self, token: str) -> None:
        marker = self.backend.sql(sqlite="?", postgres="%s")
        with self.backend.transaction(write=True) as connection:
            connection.execute(
                "UPDATE ha_connector_reference_locks SET lease_owner='',lease_token='',"
                f"lease_expires_at=0,updated_at={marker} WHERE lock_id={marker} "
                f"AND lease_token={marker}",
                (float(self._clock()), "credential-references", token),
            )

    async def __aenter__(self) -> DistributedConnectorReferenceLock:
        await self._local_lock.acquire()
        try:
            if self._token is not None:
                raise RuntimeError("connector reference lock is not reentrant")
            token = f"ref-{uuid.uuid4().hex}"
            loop = asyncio.get_running_loop()
            deadline = loop.time() + self.acquire_timeout_seconds
            while True:
                acquired = await _run_executor(
                    self.executor_provider(), self._try_acquire, token
                )
                if acquired:
                    break
                if loop.time() >= deadline:
                    raise TimeoutError("connector reference lock acquisition timed out")
                await asyncio.sleep(min(0.05, max(0.0, deadline - loop.time())))
            stop = threading.Event()
            thread = threading.Thread(
                target=self._heartbeat,
                args=(token, stop),
                name="cogdoc-connector-reference-lease",
                daemon=True,
            )
            self._token = token
            self._stop = stop
            self._heartbeat_thread = thread
            thread.start()
            return self
        except BaseException:
            self._local_lock.release()
            raise

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        del exc_type, exc, traceback
        token = self._token
        stop = self._stop
        thread = self._heartbeat_thread
        self._token = None
        self._stop = None
        self._heartbeat_thread = None
        if stop is not None:
            stop.set()
        if thread is not None:
            thread.join(min(10.0, self.lease_seconds))
        try:
            if token is not None:
                await _run_executor(self.executor_provider(), self._release, token)
        finally:
            self._local_lock.release()

    def check(self) -> bool:
        with self.backend.transaction() as connection:
            connection.execute(
                "SELECT 1 FROM ha_connector_reference_locks LIMIT 1"
            ).fetchone()
        return True
