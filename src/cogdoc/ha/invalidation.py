from __future__ import annotations

import logging
import json
import math
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from cogdoc.ha.storage import DatabaseBackend


LOGGER = logging.getLogger(__name__)


class CacheInvalidationFeed:
    """Broadcast outbox events through one durable cursor per API node."""

    def __init__(
        self,
        backend: DatabaseBackend,
        handler: Callable[[str, Mapping[str, Any], str], None],
        *,
        consumer_id: str,
        topics: Sequence[str] = (
            "index.published",
            "kb.source-generation.published",
            "kb.deleted",
        ),
        interval_seconds: float = 0.5,
        batch_size: int = 100,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if (
            not consumer_id
            or consumer_id != consumer_id.strip()
            or len(consumer_id.encode()) > 255
        ):
            raise ValueError("cache invalidation consumer_id is invalid")
        normalized_topics = tuple(dict.fromkeys(str(topic).strip() for topic in topics))
        if not normalized_topics or any(not topic for topic in normalized_topics):
            raise ValueError("cache invalidation topics are invalid")
        if not math.isfinite(interval_seconds) or not 0.05 <= interval_seconds <= 60:
            raise ValueError("cache invalidation interval is invalid")
        if type(batch_size) is not int or not 1 <= batch_size <= 1000:
            raise ValueError("cache invalidation batch_size is invalid")
        self.backend = backend
        self.handler = handler
        self.consumer_id = consumer_id
        self.topics = normalized_topics
        self.interval_seconds = float(interval_seconds)
        self.batch_size = batch_size
        self._clock = clock
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._last_error: Exception | None = None
        with backend.transaction(write=True) as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS ha_invalidation_offsets (
                consumer_id TEXT PRIMARY KEY,last_created_at DOUBLE PRECISION NOT NULL,
                last_event_id TEXT NOT NULL,updated_at DOUBLE PRECISION NOT NULL)"""
            )

    @staticmethod
    def _mapping(row: Any) -> dict[str, Any]:
        if isinstance(row, Mapping):
            return dict(row)
        keys = getattr(row, "keys", None)
        if callable(keys):
            return {str(key): row[key] for key in keys()}
        raise RuntimeError("cache invalidation row mapping is unavailable")

    def _offset(self) -> tuple[float, str]:
        marker = self.backend.sql(sqlite="?", postgres="%s")
        with self.backend.transaction() as connection:
            row = connection.execute(
                f"SELECT last_created_at,last_event_id FROM ha_invalidation_offsets "
                f"WHERE consumer_id={marker}",
                (self.consumer_id,),
            ).fetchone()
        if row is None:
            return 0.0, ""
        value = self._mapping(row)
        return float(value["last_created_at"]), str(value["last_event_id"])

    def _advance(self, created_at: float, event_id: str) -> None:
        insert = self.backend.sql(
            sqlite=(
                "INSERT INTO ha_invalidation_offsets(consumer_id,last_created_at,"
                "last_event_id,updated_at) VALUES(?,?,?,?) ON CONFLICT(consumer_id) "
                "DO UPDATE SET last_created_at=excluded.last_created_at,"
                "last_event_id=excluded.last_event_id,updated_at=excluded.updated_at "
                "WHERE excluded.last_created_at>ha_invalidation_offsets.last_created_at "
                "OR (excluded.last_created_at=ha_invalidation_offsets.last_created_at "
                "AND excluded.last_event_id>ha_invalidation_offsets.last_event_id)"
            ),
            postgres=(
                "INSERT INTO ha_invalidation_offsets(consumer_id,last_created_at,"
                "last_event_id,updated_at) VALUES(%s,%s,%s,%s) ON CONFLICT(consumer_id) "
                "DO UPDATE SET last_created_at=EXCLUDED.last_created_at,"
                "last_event_id=EXCLUDED.last_event_id,updated_at=EXCLUDED.updated_at "
                "WHERE EXCLUDED.last_created_at>ha_invalidation_offsets.last_created_at "
                "OR (EXCLUDED.last_created_at=ha_invalidation_offsets.last_created_at "
                "AND EXCLUDED.last_event_id>ha_invalidation_offsets.last_event_id)"
            ),
        )
        with self.backend.transaction(write=True) as connection:
            connection.execute(
                insert, (self.consumer_id, created_at, event_id, self._clock())
            )

    def poll_once(self) -> int:
        created_at, event_id = self._offset()
        marker = self.backend.sql(sqlite="?", postgres="%s")
        topic_markers = ",".join(marker for _topic in self.topics)
        query = (
            "SELECT event_id,tenant_id,topic,payload_json,created_at FROM ha_outbox "
            f"WHERE topic IN ({topic_markers}) AND (created_at>{marker} OR "
            f"(created_at={marker} AND event_id>{marker})) "
            f"ORDER BY created_at,event_id LIMIT {self.batch_size}"
        )
        with self.backend.transaction() as connection:
            rows = connection.execute(
                query, (*self.topics, created_at, created_at, event_id)
            ).fetchall()
        processed = 0
        for raw in rows:
            row = self._mapping(raw)
            payload = json.loads(str(row["payload_json"]))
            if not isinstance(payload, Mapping):
                raise RuntimeError("cache invalidation payload is invalid")
            self.handler(str(row["topic"]), payload, str(row["tenant_id"]))
            self._advance(float(row["created_at"]), str(row["event_id"]))
            processed += 1
        return processed

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            reconcile = getattr(self.handler, "reconcile", None)
            if callable(reconcile):
                # A node may have been offline beyond outbox retention. Local
                # caches are disposable, so cold-start invalidation closes that
                # compaction gap before the node becomes ready.
                reconcile()
            self._stop.clear()
            self._wake.clear()
            self._last_error = None
            self._thread = threading.Thread(
                target=self._run,
                name=f"cogdoc-cache-invalidation-{self.consumer_id[:16]}",
                daemon=True,
            )
            self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                while self.poll_once() == self.batch_size:
                    if self._stop.is_set():
                        return
                self._last_error = None
            except Exception as exc:
                self._last_error = exc
                LOGGER.exception("cache invalidation polling failed")
            self._wake.wait(self.interval_seconds)
            self._wake.clear()

    def stop(self, timeout: float = 10.0) -> bool:
        self._stop.set()
        self._wake.set()
        with self._lock:
            thread = self._thread
        if thread is not None:
            thread.join(timeout)
        stopped = thread is None or not thread.is_alive()
        if stopped:
            with self._lock:
                self._thread = None
        return stopped

    def check(self) -> bool:
        with self._lock:
            thread = self._thread
        return self._last_error is None and (
            thread is None or thread.is_alive() or self._stop.is_set()
        )


class HACacheInvalidator:
    def __init__(self, replica: Any | None, registry: Any) -> None:
        self.replica = replica
        self.registry = registry

    def __call__(self, topic: str, payload: Mapping[str, Any], tenant_id: str) -> None:
        kb_id = str(payload.get("kb_id") or payload.get("storage_id") or "")
        if not kb_id:
            raise ValueError("cache invalidation event has no KB identity")
        record = self.registry.get_by_storage_id(kb_id)
        if topic != "kb.deleted" and (
            record is None or str(record.get("tenant_id")) != tenant_id
        ):
            return
        if self.replica is not None:
            self.replica.invalidate(tenant_id, kb_id)
        from cogdoc.service.retriever_factory import RetrieverFactory

        RetrieverFactory.invalidate(kb_id)

    def reconcile(self) -> None:
        for record in self.registry.list():
            if not isinstance(record, Mapping):
                continue
            tenant_id = str(record.get("tenant_id") or "")
            kb_id = str(record.get("storage_id") or "")
            if not tenant_id or not kb_id:
                continue
            if self.replica is not None:
                self.replica.invalidate(tenant_id, kb_id)
            from cogdoc.service.retriever_factory import RetrieverFactory

            RetrieverFactory.invalidate(kb_id)


__all__ = ["CacheInvalidationFeed", "HACacheInvalidator"]
