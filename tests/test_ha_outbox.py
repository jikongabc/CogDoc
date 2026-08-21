from __future__ import annotations

import threading

import pytest

from cogdoc.ha.outbox import (
    OUTBOX_DEAD_LETTER,
    OUTBOX_DELIVERED,
    OUTBOX_DELIVERING,
    OUTBOX_PENDING,
    OutboxConflict,
    OutboxDispatcher,
    OutboxStore,
    StaleOutboxLease,
    WebhookOutboxHandler,
)
from cogdoc.ha.storage import SQLiteBackend


class Clock:
    def __init__(self):
        self.value = 1000.0

    def __call__(self):
        return self.value


@pytest.fixture
def outbox(tmp_path):
    backend = SQLiteBackend(tmp_path / "outbox.db")
    clock = Clock()
    store = OutboxStore(backend, clock=clock)
    yield backend, store, clock
    backend.close()


def _event(store, **overrides):
    values = {
        "tenant_id": "tenant",
        "topic": "index.published",
        "aggregate_type": "knowledge_base",
        "aggregate_id": "kb",
        "aggregate_revision": 7,
        "payload": {"generation_id": "gen-1"},
        "headers": {"trace_id": "trace"},
        "idempotency_key": "kb:7",
    }
    values.update(overrides)
    return store.enqueue(**values)


def test_append_is_atomic_with_business_transaction(outbox):
    backend, store, _clock = outbox
    with pytest.raises(RuntimeError):
        with backend.transaction(write=True) as connection:
            event = store.append(
                connection,
                tenant_id="tenant",
                topic="changed",
                aggregate_type="kb",
                aggregate_id="kb",
                aggregate_revision=1,
                payload={"ok": True},
            )
            raise RuntimeError(event["event_id"])
    with backend.transaction() as connection:
        assert connection.execute("SELECT COUNT(*) FROM ha_outbox").fetchone()[0] == 0


def test_idempotency_replays_exact_event_and_rejects_collision(outbox):
    _backend, store, _clock = outbox
    first = _event(store)
    assert _event(store)["event_id"] == first["event_id"]
    with pytest.raises(OutboxConflict):
        _event(store, payload={"generation_id": "other"})


def test_crash_after_delivery_redelivers_same_event_id(outbox):
    _backend, store, clock = outbox
    event = _event(store)
    claimed = store.claim("worker", lease_seconds=5)
    assert claimed["event_id"] == event["event_id"]
    assert claimed["status"] == OUTBOX_DELIVERING
    # Side effect happened, but process exited before acknowledgement.
    clock.value += 6
    replay = store.claim("replacement", lease_seconds=5)
    assert replay["event_id"] == event["event_id"]
    assert replay["attempt"] == 2
    store.delivered(replay["event_id"], replay["lease_token"])
    assert store.get(event["event_id"])["status"] == OUTBOX_DELIVERED
    with pytest.raises(StaleOutboxLease):
        store.delivered(claimed["event_id"], claimed["lease_token"])


def test_failures_retry_then_dead_letter(outbox):
    _backend, store, clock = outbox
    event = _event(store, max_attempts=2)
    first = store.claim("worker", lease_seconds=5)
    retried = store.failed(
        event["event_id"], first["lease_token"], "HTTP503", retry_delay_seconds=10
    )
    assert retried["status"] == OUTBOX_PENDING
    assert store.claim("worker") is None
    clock.value += 10
    second = store.claim("worker", lease_seconds=5)
    dead = store.failed(
        event["event_id"], second["lease_token"], "HTTP503", retry_delay_seconds=10
    )
    assert dead["status"] == OUTBOX_DEAD_LETTER
    assert store.claim("worker") is None


def test_expired_final_attempt_is_reaped_to_dead_letter(outbox):
    _backend, store, clock = outbox
    event = _event(store, max_attempts=1)
    store.claim("worker", lease_seconds=5)
    clock.value += 6
    assert store.claim("replacement", lease_seconds=5) is None
    assert store.get(event["event_id"])["status"] == OUTBOX_DEAD_LETTER


def test_same_aggregate_is_delivered_in_revision_order(outbox):
    _backend, store, _clock = outbox
    later = _event(
        store,
        aggregate_revision=2,
        payload={"revision": 2},
        idempotency_key="revision-2",
    )
    earlier = _event(
        store,
        aggregate_revision=1,
        payload={"revision": 1},
        idempotency_key="revision-1",
    )
    first = store.claim("worker")
    assert first["event_id"] == earlier["event_id"]
    store.delivered(first["event_id"], first["lease_token"])
    second = store.claim("worker")
    assert second["event_id"] == later["event_id"]


def test_concurrent_claim_has_one_owner(tmp_path):
    database = tmp_path / "outbox.db"
    backend_a = SQLiteBackend(database)
    backend_b = SQLiteBackend(database)
    store_a = OutboxStore(backend_a)
    store_b = OutboxStore(backend_b)
    event = _event(store_a)
    barrier = threading.Barrier(2)
    results = []

    def claim(store, worker):
        barrier.wait()
        results.append(store.claim(worker))

    threads = [
        threading.Thread(target=claim, args=(store_a, "a")),
        threading.Thread(target=claim, args=(store_b, "b")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    winners = [row for row in results if row is not None]
    assert len(winners) == 1
    assert winners[0]["event_id"] == event["event_id"]
    backend_a.close()
    backend_b.close()


def test_dispatcher_passes_stable_id_and_prunes_delivered(outbox):
    _backend, store, clock = outbox
    event = _event(store)
    seen = []
    dispatcher = OutboxDispatcher(
        store,
        lambda topic, payload, headers, event_id: seen.append(
            (topic, payload, headers, event_id)
        ),
        worker_id="dispatcher",
    )
    assert dispatcher.run_once()
    assert seen == [
        (
            "index.published",
            {"generation_id": "gen-1"},
            {"trace_id": "trace"},
            event["event_id"],
        )
    ]
    assert store.get(event["event_id"])["status"] == OUTBOX_DELIVERED
    clock.value += 1
    assert store.prune_delivered(before=clock.value) == 1
    assert store.get(event["event_id"]) is None
    with pytest.raises(OutboxConflict, match="compacted"):
        _event(store)


def test_webhook_handler_signs_stable_id_without_redirects():
    requests = []

    class Response:
        def raise_for_status(self):
            return None

    class Client:
        def post(self, url, **kwargs):
            requests.append((url, kwargs))
            return Response()

    handler = WebhookOutboxHandler(
        "https://hooks.example/cogdoc", secret="secret", client=Client()
    )
    handler("index.published", {"generation": "g1"}, {"trace": "t"}, "evt-1")
    url, request = requests[0]
    assert url == "https://hooks.example/cogdoc"
    assert request["headers"]["Idempotency-Key"] == "evt-1"
    assert request["headers"]["X-CogDoc-Signature"].startswith("sha256=")
    assert b'"event_id":"evt-1"' in request["content"]
