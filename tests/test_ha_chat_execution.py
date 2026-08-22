from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from cogdoc.api.connector_scope import capture_kb_epoch
from cogdoc.api.ingest import KnowledgeBaseRegistry
from cogdoc.ha.chat_execution import HAChatCoordinator
from cogdoc.ha.session_store import DistributedSessionStore, StaleSessionLease
from cogdoc.ha.storage import SQLiteBackend
from cogdoc.service.retriever_factory import RetrieverFactory
from cogdoc.service.chat_service import ChatEvent


class _PinnedProvider:
    def __init__(self, registry, generation_id: str = "generation-1") -> None:
        self.registry = registry
        self.generation_id = generation_id
        self.entries = 0
        self.exits = 0
        self.calls: list[str] = []

    def __call__(self, kb_id: str):
        self.calls.append(kb_id)
        return SimpleNamespace(generation_id=self.generation_id)

    @contextmanager
    def pin(self, kb_id: str):
        self.entries += 1
        try:
            yield {
                "tenant_id": "tenant",
                "kb_id": kb_id,
                "generation_id": self.generation_id,
            }
        finally:
            self.exits += 1


def _registry(tmp_path):
    registry = KnowledgeBaseRegistry(
        str(tmp_path / "registry.json"),
        source_dir_for=lambda kb_id: str(tmp_path / "sources" / kb_id),
    )
    row = registry.create("docs", tenant_id="tenant", owner_id="owner")
    return registry, str(row["storage_id"])


def _result(trace_id: str = "trace-1"):
    return SimpleNamespace(
        answer="answer",
        trace_id=trace_id,
        task_type="qa",
        chat_messages=[
            {"role": "user", "content": "question"},
            {"role": "assistant", "content": "answer"},
        ],
    )


def test_coordinator_records_once_under_the_pinned_generation(tmp_path):
    registry, storage_id = _registry(tmp_path)
    store = DistributedSessionStore(SQLiteBackend(tmp_path / "chat.db"))
    provider = _PinnedProvider(registry)
    coordinator = HAChatCoordinator(store, provider, registry, worker_id="node-a")
    guards = []

    def run_with_default_factory(_history):
        assert RetrieverFactory.get_engine(storage_id).generation_id == "generation-1"
        return _result()

    result = coordinator.run(
        tenant_id="tenant",
        storage_id=storage_id,
        expected_epoch=capture_kb_epoch(storage_id),
        session_scope_id=storage_id,
        session_id="session",
        query="question",
        authority_guard=lambda: guards.append("checked"),
        runner=run_with_default_factory,
    )

    assert result.answer == "answer"
    assert provider.entries == provider.exits == 1
    assert provider.calls == [storage_id]
    assert len(guards) >= 2
    display = store.get_display(storage_id, "session")
    assert display[-1]["index_generation_id"] == "generation-1"
    assert display[-1]["trace_id"] == "trace-1"


def test_stream_revocation_stops_frames_and_does_not_record(tmp_path):
    registry, storage_id = _registry(tmp_path)
    store = DistributedSessionStore(SQLiteBackend(tmp_path / "chat.db"))
    provider = _PinnedProvider(registry)
    coordinator = HAChatCoordinator(store, provider, registry, worker_id="node-a")
    allowed = [True]

    def guard() -> None:
        if not allowed[0]:
            raise PermissionError("revoked")

    def runner(_history):
        yield ChatEvent("request_started", {"trace_id": "trace"})
        allowed[0] = False
        yield ChatEvent("token", {"content": "secret"})
        yield ChatEvent("final", {"result": _result()})

    events = coordinator.stream(
        tenant_id="tenant",
        storage_id=storage_id,
        expected_epoch=capture_kb_epoch(storage_id),
        session_scope_id=storage_id,
        session_id="session",
        query="question",
        authority_guard=guard,
        runner=runner,
    )
    assert next(events).type == "request_started"
    with pytest.raises(PermissionError, match="revoked"):
        next(events)
    assert store.get_display(storage_id, "session") == []


def test_abandoned_stream_cannot_commit_a_late_final_answer(tmp_path):
    registry, storage_id = _registry(tmp_path)
    store = DistributedSessionStore(SQLiteBackend(tmp_path / "chat.db"))
    coordinator = HAChatCoordinator(
        store, _PinnedProvider(registry), registry, worker_id="node-a"
    )
    stopped = [False]

    def runner(_history):
        yield ChatEvent("request_started", {"trace_id": "trace"})
        yield ChatEvent("final", {"result": _result("trace-late")})

    events = coordinator.stream(
        tenant_id="tenant",
        storage_id=storage_id,
        expected_epoch=capture_kb_epoch(storage_id),
        session_scope_id=storage_id,
        session_id="session",
        query="question",
        authority_guard=lambda: None,
        runner=runner,
        stop_requested=lambda: stopped[0],
    )
    assert next(events).type == "request_started"
    assert next(events).type == "final"
    stopped[0] = True
    with pytest.raises(StaleSessionLease, match="abandoned"):
        next(events)
    assert store.get_display(storage_id, "session") == []


def test_changed_kb_incarnation_rejects_the_memory_commit(tmp_path):
    registry, storage_id = _registry(tmp_path)
    store = DistributedSessionStore(SQLiteBackend(tmp_path / "chat.db"))
    provider = _PinnedProvider(registry)
    coordinator = HAChatCoordinator(store, provider, registry, worker_id="node-a")
    expected_epoch = capture_kb_epoch(storage_id)

    def runner(_history):
        from cogdoc.service.kb_epoch import shared_epoch_store

        shared_epoch_store().bump(storage_id)
        return _result()

    with pytest.raises(RuntimeError, match="knowledge base changed"):
        coordinator.run(
            tenant_id="tenant",
            storage_id=storage_id,
            expected_epoch=expected_epoch,
            session_scope_id=storage_id,
            session_id="session",
            query="question",
            authority_guard=lambda: None,
            runner=runner,
        )
    assert store.get_display(storage_id, "session") == []


def test_reader_lease_failure_prevents_memory_commit(tmp_path):
    registry, storage_id = _registry(tmp_path)
    store = DistributedSessionStore(SQLiteBackend(tmp_path / "chat.db"))

    class FailedReader(_PinnedProvider):
        @contextmanager
        def pin(self, kb_id: str):
            yield {
                "tenant_id": "tenant",
                "kb_id": kb_id,
                "generation_id": "generation-1",
                "check": lambda: (_ for _ in ()).throw(
                    RuntimeError("reader lease lost")
                ),
            }

    coordinator = HAChatCoordinator(
        store, FailedReader(registry), registry, worker_id="node-a"
    )
    with pytest.raises(RuntimeError, match="reader lease lost"):
        coordinator.run(
            tenant_id="tenant",
            storage_id=storage_id,
            expected_epoch=capture_kb_epoch(storage_id),
            session_scope_id=storage_id,
            session_id="session",
            query="question",
            authority_guard=lambda: None,
            runner=lambda _history: _result(),
        )
    assert store.get_display(storage_id, "session") == []
