from threading import Event, Thread

import pytest

from cogdoc.service import kb_readers
from cogdoc.service.kb_readers import (
    KBReadUnavailable,
    drain_kb_readers,
    kb_read_lease,
    wait_for_no_readers,
)


def test_wait_for_no_readers_times_out_then_observes_drain():
    entered = Event()
    release = Event()

    def reader():
        with kb_read_lease("kb"):
            entered.set()
            release.wait(timeout=2.0)

    thread = Thread(target=reader)
    thread.start()
    assert entered.wait(timeout=1.0)
    assert wait_for_no_readers("kb", timeout_seconds=0.01) is False

    release.set()
    thread.join(timeout=1.0)
    assert not thread.is_alive()
    assert wait_for_no_readers("kb", timeout_seconds=0.01) is True


def test_drain_closes_admission_until_existing_reader_exits():
    reader_entered = Event()
    release_reader = Event()
    drain_entered = Event()
    release_drain = Event()

    def reader():
        with kb_read_lease("kb-drain"):
            reader_entered.set()
            release_reader.wait(timeout=2.0)

    def drainer():
        with drain_kb_readers("kb-drain", timeout_seconds=2.0):
            drain_entered.set()
            release_drain.wait(timeout=2.0)

    reader_thread = Thread(target=reader)
    reader_thread.start()
    assert reader_entered.wait(timeout=1.0)

    drain_thread = Thread(target=drainer)
    drain_thread.start()
    with kb_readers._condition:
        assert kb_readers._condition.wait_for(
            lambda: "kb-drain" in kb_readers._draining,
            timeout=1.0,
        )

    with pytest.raises(KBReadUnavailable):
        with kb_read_lease("kb-drain"):
            pass
    assert not drain_entered.is_set()

    release_reader.set()
    assert drain_entered.wait(timeout=1.0)
    release_drain.set()
    reader_thread.join(timeout=1.0)
    drain_thread.join(timeout=1.0)
    assert not reader_thread.is_alive()
    assert not drain_thread.is_alive()

    with kb_read_lease("kb-drain"):
        pass


def test_derived_index_clear_does_not_hold_mutation_lock_while_draining(
    tmp_path, monkeypatch
):
    import cogdoc.tools.retriever.derived_knowledge as derived_module

    index = derived_module.DerivedKnowledgeIndex.__new__(
        derived_module.DerivedKnowledgeIndex
    )
    index.persist_directory = str(tmp_path / "chroma")
    index.state_directory = str(tmp_path / "state")
    index.client = object()
    index._lock = derived_module.RLock()
    observed = {}

    def clear_storage(_kb_id, **options):
        mutation_lock = options["mutation_lock"]
        observed["same_lock"] = mutation_lock is index._lock
        observed["owned_before_drain"] = mutation_lock._is_owned()

    monkeypatch.setattr(
        derived_module,
        "clear_derived_knowledge_index_storage",
        clear_storage,
    )

    index.clear_kb("kb")

    assert observed == {"same_lock": True, "owned_before_drain": False}
