from __future__ import annotations

from pathlib import Path

import pytest

from cogdoc.ha.invalidation import CacheInvalidationFeed
from cogdoc.ha.outbox import OutboxStore
from cogdoc.ha.storage import SQLiteBackend


def _append(store: OutboxStore, backend: SQLiteBackend, revision: int) -> None:
    with backend.transaction(write=True) as connection:
        store.append(
            connection,
            tenant_id="tenant-a",
            topic="index.published",
            aggregate_type="knowledge_base",
            aggregate_id="kb-1",
            aggregate_revision=revision,
            payload={"kb_id": "kb-1", "generation_id": f"gen-{revision}"},
            idempotency_key=f"index:gen-{revision}",
        )


def test_every_api_node_consumes_the_same_invalidation_log(tmp_path: Path) -> None:
    first_backend = SQLiteBackend(tmp_path / "state.db")
    second_backend = SQLiteBackend(tmp_path / "state.db")
    outbox = OutboxStore(first_backend)
    _append(outbox, first_backend, 1)
    _append(outbox, first_backend, 2)
    first_seen: list[str] = []
    second_seen: list[str] = []
    first = CacheInvalidationFeed(
        first_backend,
        lambda _topic, payload, _tenant: first_seen.append(
            str(payload["generation_id"])
        ),
        consumer_id="api-a",
    )
    second = CacheInvalidationFeed(
        second_backend,
        lambda _topic, payload, _tenant: second_seen.append(
            str(payload["generation_id"])
        ),
        consumer_id="api-b",
    )

    assert first.poll_once() == 2
    assert second.poll_once() == 2
    assert first_seen == ["gen-1", "gen-2"]
    assert second_seen == ["gen-1", "gen-2"]

    restarted = CacheInvalidationFeed(
        second_backend,
        lambda _topic, payload, _tenant: second_seen.append(
            str(payload["generation_id"])
        ),
        consumer_id="api-b",
    )
    assert restarted.poll_once() == 0


def test_handler_failure_does_not_advance_durable_cursor(tmp_path: Path) -> None:
    backend = SQLiteBackend(tmp_path / "state.db")
    outbox = OutboxStore(backend)
    _append(outbox, backend, 1)
    attempts = 0

    def fail_once(_topic, _payload, _tenant):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("injected invalidation failure")

    feed = CacheInvalidationFeed(backend, fail_once, consumer_id="api-a")
    with pytest.raises(RuntimeError, match="injected"):
        feed.poll_once()
    assert feed.poll_once() == 1
    assert attempts == 2


def test_same_timestamp_events_use_event_id_as_stable_cursor(tmp_path: Path) -> None:
    backend = SQLiteBackend(tmp_path / "state.db")
    outbox = OutboxStore(backend, clock=lambda: 100.0)
    _append(outbox, backend, 1)
    _append(outbox, backend, 2)
    seen: list[str] = []
    feed = CacheInvalidationFeed(
        backend,
        lambda _topic, payload, _tenant: seen.append(str(payload["generation_id"])),
        consumer_id="api-a",
        batch_size=1,
    )

    assert feed.poll_once() == 1
    assert feed.poll_once() == 1
    assert feed.poll_once() == 0
    assert set(seen) == {"gen-1", "gen-2"}


def test_duplicate_consumer_process_cannot_regress_shared_cursor(
    tmp_path: Path,
) -> None:
    first_backend = SQLiteBackend(tmp_path / "state.db")
    second_backend = SQLiteBackend(tmp_path / "state.db")
    first = CacheInvalidationFeed(
        first_backend, lambda *_args: None, consumer_id="api-a"
    )
    second = CacheInvalidationFeed(
        second_backend, lambda *_args: None, consumer_id="api-a"
    )

    first._advance(101.0, "event-z")
    second._advance(100.0, "event-a")
    second._advance(101.0, "event-a")

    assert first._offset() == (101.0, "event-z")
