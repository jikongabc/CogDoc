from dataclasses import dataclass, field

import pytest

from cogdoc.connectors.base import (
    ConnectorError,
    ConnectorPage,
    ConnectorSourceRef,
    FetchedSource,
    RetryableConnectorError,
)
from cogdoc.connectors.sync_runtime import ConnectorSyncRuntime, SyncLimits
from cogdoc.connectors.sync_store import ConnectorSyncStore


def test_connector_contract_rejects_unbounded_provider_metadata_before_staging():
    with pytest.raises(ValueError, match="external_id exceeds"):
        ConnectorSourceRef("x" * 4_097, "page.md")
    with pytest.raises(ValueError, match="display_name exceeds"):
        ConnectorSourceRef("page", "x" * 2_049)
    with pytest.raises(ValueError, match="metadata exceeds"):
        ConnectorSourceRef("page", "page.md", metadata={"blob": "x" * 65_536})
    with pytest.raises(ValueError, match="next_cursor exceeds"):
        ConnectorPage((), next_cursor="x" * 16_385)
    with pytest.raises(ValueError, match="acl grant count exceeds"):
        FetchedSource(
            ConnectorSourceRef("page", "page.md"),
            b"content",
            acl={"grants": [{}] * 4_097},
        )


def test_connector_contract_rejects_control_characters_and_invalid_types():
    with pytest.raises(ValueError, match="control characters"):
        ConnectorSourceRef("page\nother", "page.md")
    with pytest.raises(ValueError, match="media_type is invalid"):
        ConnectorSourceRef("page", "page.md", media_type="not-a-media-type")
    with pytest.raises(ValueError, match="64-character hex"):
        ConnectorSourceRef("page", "page.md", content_sha256="not-a-hash")
    with pytest.raises(TypeError, match="flags must be booleans"):
        ConnectorPage((), complete=1)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="finite JSON object"):
        ConnectorSourceRef("page", "page.md", metadata={"value": float("nan")})


class Connector:
    connector_type = "git"

    def __init__(self, pages, content):
        self.pages = iter(pages)
        self.content = content

    def list_page(self, cursor, *, limit):
        return next(self.pages)

    def fetch(self, ref):
        return FetchedSource(ref, self.content[ref.external_id])


@dataclass
class Sink:
    begun: bool = False
    upserts: list = field(default_factory=list)
    deletes: list = field(default_factory=list)
    committed: bool = False
    aborted: bool = False

    def begin(self, **scope):
        self.begun = True
        self.attempt = scope["attempt"]

    def upsert(self, document, content, *, acl=None):
        self.upserts.append((document, content, acl))

    def delete(self, external_id):
        self.deletes.append(external_id)

    def prepare_commit(self, *, snapshot, seen_external_ids):
        self.prepared = (snapshot, seen_external_ids)

    def commit(self, *, snapshot, seen_external_ids, heartbeat):
        heartbeat()
        self.committed = True
        self.snapshot = snapshot
        self.seen = seen_external_ids

    def recover_commit(self, *, heartbeat):
        heartbeat()
        self.committed = True
        self.recovered = True

    def abort(self):
        self.aborted = True

    def finalize(self):
        self.finalized = True


def _store_job(tmp_path):
    store = ConnectorSyncStore(str(tmp_path / "state.db"))
    job = store.create(
        tenant_id="tenant",
        kb_id="kb",
        connection_id="c1",
        connector_type="git",
    )
    return store, job


def test_runtime_pages_fetches_deletes_and_commits_snapshot(tmp_path):
    store, job = _store_job(tmp_path)
    a = ConnectorSourceRef("a", "a.md")
    b = ConnectorSourceRef("b", "b.md")
    connector = Connector(
        [
            ConnectorPage((a,), next_cursor="next", snapshot=True),
            ConnectorPage((b,), ("gone",), complete=True, snapshot=True),
        ],
        {"a": b"alpha", "b": b"beta"},
    )
    sink = Sink()
    result = ConnectorSyncRuntime(store).run(job["job_id"], connector, sink)
    assert result["status"] == "succeeded"
    assert result["pages_processed"] == 2 and result["documents_fetched"] == 2
    assert sink.committed and sink.snapshot and sink.seen == frozenset({"a", "b"})
    assert sink.deletes == ["gone"]
    assert sink.finalized is True
    store.close()


def test_runtime_budget_failure_aborts_without_retry(tmp_path):
    store, job = _store_job(tmp_path)
    ref = ConnectorSourceRef("a", "a.md")
    connector = Connector([ConnectorPage((ref,), complete=True)], {"a": b"too large"})
    sink = Sink()
    runtime = ConnectorSyncRuntime(store, limits=SyncLimits(max_document_bytes=3))
    result = runtime.run(job["job_id"], connector, sink)
    assert result["status"] == "failed" and result["error_code"] == "SYNCBUDGETEXCEEDED"
    assert sink.aborted and not sink.committed
    store.close()


def test_retryable_connector_failure_enters_bounded_retry_wait(tmp_path):
    store, job = _store_job(tmp_path)

    class Broken:
        connector_type = "git"

        def list_page(self, cursor, *, limit):
            raise RetryableConnectorError("provider throttled")

    sink = Sink()
    result = ConnectorSyncRuntime(store).run(job["job_id"], Broken(), sink)
    assert result["status"] == "retry_wait" and result["retry_at"] is not None
    assert sink.aborted
    store.close()


def test_runtime_recovers_expired_committing_job_without_relisting(tmp_path):
    class Clock:
        value = 100.0

        def __call__(self):
            return self.value

    clock = Clock()
    store = ConnectorSyncStore(str(tmp_path / "state.db"), clock=clock)
    job = store.create(
        tenant_id="tenant",
        kb_id="kb",
        connection_id="c1",
        connector_type="git",
    )
    _, token = store.acquire(job["job_id"], lease_seconds=10)
    store.checkpoint(
        job["job_id"],
        token,
        cursor="done",
        counters={
            "pages_processed": 1,
            "documents_seen": 0,
            "documents_fetched": 0,
            "deleted_seen": 0,
            "bytes_fetched": 0,
        },
        lease_seconds=10,
    )
    store.prepare_commit(job["job_id"], token)
    clock.value += 11

    class MustNotList:
        connector_type = "git"

        def list_page(self, cursor, *, limit):
            raise AssertionError("committing recovery must not relist")

    sink = Sink()
    result = ConnectorSyncRuntime(store).run(job["job_id"], MustNotList(), sink)
    assert result["status"] == "succeeded"
    assert sink.recovered is True
    store.close()


def test_rate_limit_exhaustion_dead_letters_and_emits_lifecycle(tmp_path):
    store, job = _store_job(tmp_path)

    class Throttled:
        connector_type = "git"

        def list_page(self, cursor, *, limit):
            del cursor, limit
            raise RetryableConnectorError("provider throttled")

    class Observer:
        def __init__(self):
            self.events = []

        def __getattr__(self, name):
            return lambda observation: self.events.append((name, observation))

    observer = Observer()
    runtime = ConnectorSyncRuntime(
        store,
        limits=SyncLimits(max_attempts=2, retry_base_seconds=0),
        observer=observer,
    )
    first = runtime.run(job["job_id"], Throttled(), Sink())
    second = runtime.run(job["job_id"], Throttled(), Sink())

    assert first["status"] == "retry_wait"
    assert second["status"] == "dead_letter" and second["attempt"] == 2
    assert [name for name, _ in observer.events] == [
        "started",
        "retry",
        "started",
        "dead_letter",
    ]
    terminal = observer.events[-1][1]
    assert terminal.duration_seconds >= 0 and terminal.backlog == 0
    assert store.health_snapshot("tenant", "kb", "c1")["health_status"] == (
        "dead_letter"
    )
    store.close()


def test_authentication_failure_is_not_retried_or_dead_lettered(tmp_path):
    store, job = _store_job(tmp_path)

    class Unauthorized:
        connector_type = "git"

        def list_page(self, cursor, *, limit):
            del cursor, limit
            raise ConnectorError("provider authentication failed")

    result = ConnectorSyncRuntime(store).run(job["job_id"], Unauthorized(), Sink())

    assert result["status"] == "failed"
    assert result["attempt"] == 1
    assert result["retry_at"] is None
    store.close()


def test_observer_failure_cannot_fail_a_successful_sync(tmp_path):
    store, job = _store_job(tmp_path)

    class BrokenObserver:
        def __getattr__(self, name):
            del name

            def fail(observation):
                del observation
                raise RuntimeError("exporter unavailable")

            return fail

    connector = Connector([ConnectorPage(complete=True)], {})
    result = ConnectorSyncRuntime(store, observer=BrokenObserver()).run(
        job["job_id"], connector, Sink()
    )

    assert result["status"] == "succeeded"
    store.close()


def test_observer_reports_progress_and_success_counters(tmp_path):
    store, job = _store_job(tmp_path)
    events = []

    class Observer:
        def __getattr__(self, name):
            return lambda observation: events.append((name, observation))

    ref = ConnectorSourceRef("a", "a.md")
    connector = Connector(
        [ConnectorPage((ref,), complete=True, snapshot=True)], {"a": b"alpha"}
    )
    result = ConnectorSyncRuntime(store, observer=Observer()).run(
        job["job_id"], connector, Sink()
    )

    assert result["status"] == "succeeded"
    assert [name for name, _ in events] == ["started", "progress", "succeeded"]
    assert events[1][1].counters["documents_fetched"] == 1
    assert events[-1][1].backlog == 0
    store.close()


def test_mid_run_revocation_emits_cancelled_observation(tmp_path):
    store, job = _store_job(tmp_path)
    events = []
    checks = iter((True, False))

    class Observer:
        def __getattr__(self, name):
            return lambda observation: events.append((name, observation))

    ref = ConnectorSourceRef("a", "a.md")
    result = ConnectorSyncRuntime(
        store,
        observer=Observer(),
        continuation_checker=lambda _job: next(checks),
    ).run(
        job["job_id"],
        Connector([ConnectorPage((ref,), complete=True)], {"a": b"alpha"}),
        Sink(),
    )

    assert result["status"] == "cancelled"
    assert [name for name, _ in events] == ["started", "cancelled"]
    assert events[-1][1].job_sequence == job["job_sequence"]
    assert store.health_snapshot("tenant", "kb", "c1")["health_status"] == ("cancelled")
    store.close()


def test_retry_after_second_page_replays_from_attempt_start_cursor(tmp_path):
    store = ConnectorSyncStore(str(tmp_path / "state.db"))
    job = store.create(
        tenant_id="tenant",
        kb_id="kb",
        connection_id="c1",
        connector_type="git",
        resume_cursor="base",
    )
    ref = ConnectorSourceRef("a", "a.md")

    class FailSecondPageOnce:
        connector_type = "git"

        def __init__(self):
            self.cursors = []

        def list_page(self, cursor, *, limit):
            del limit
            self.cursors.append(cursor)
            if len(self.cursors) in {1, 3}:
                return ConnectorPage((ref,), next_cursor="page-2")
            if len(self.cursors) == 2:
                raise RetryableConnectorError("page two unavailable")
            return ConnectorPage(complete=True, next_cursor="done")

        def fetch(self, source_ref):
            return FetchedSource(source_ref, b"alpha")

    connector = FailSecondPageOnce()
    runtime = ConnectorSyncRuntime(store, limits=SyncLimits(retry_base_seconds=0))
    first = runtime.run(job["job_id"], connector, Sink())
    second = runtime.run(job["job_id"], connector, Sink())

    assert first["status"] == "retry_wait"
    assert second["status"] == "succeeded"
    assert connector.cursors == ["base", "page-2", "base", "page-2"]
    assert second["pages_processed"] == 2
    assert second["documents_fetched"] == 1
    store.close()


def test_expired_worker_reacquire_replays_from_attempt_start_cursor(tmp_path):
    class Clock:
        value = 100.0

        def __call__(self):
            return self.value

    clock = Clock()
    store = ConnectorSyncStore(str(tmp_path / "state.db"), clock=clock)
    job = store.create(
        tenant_id="tenant",
        kb_id="kb",
        connection_id="c1",
        connector_type="git",
        resume_cursor="base",
    )
    _, abandoned_token = store.acquire(job["job_id"], lease_seconds=10)
    store.checkpoint(
        job["job_id"],
        abandoned_token,
        cursor="page-2",
        counters={
            "pages_processed": 1,
            "documents_seen": 1,
            "documents_fetched": 1,
            "deleted_seen": 0,
            "bytes_fetched": 5,
        },
        lease_seconds=10,
    )
    clock.value += 11

    class ReplayedConnector:
        connector_type = "git"

        def __init__(self):
            self.cursors = []

        def list_page(self, cursor, *, limit):
            del limit
            self.cursors.append(cursor)
            return ConnectorPage(complete=True, next_cursor="done")

    connector = ReplayedConnector()
    result = ConnectorSyncRuntime(store).run(job["job_id"], connector, Sink())

    assert result["status"] == "succeeded"
    assert result["attempt"] == 2
    assert result["pages_processed"] == 1
    assert result["documents_fetched"] == 0
    assert connector.cursors == ["base"]
    store.close()


def test_metadata_and_acl_bytes_count_toward_attempt_total_budget(tmp_path):
    for listed_ref, fetched_ref, acl in (
        (
            ConnectorSourceRef("a", "a.md", metadata={"large": "12345"}),
            None,
            None,
        ),
        (
            ConnectorSourceRef("a", "a.md"),
            None,
            {"grants": [{"id": "12345"}]},
        ),
        (
            ConnectorSourceRef("a", "a.md"),
            ConnectorSourceRef("a", "a.md", metadata={"large": "12345"}),
            None,
        ),
    ):
        store, job = _store_job(tmp_path)

        class OverheadConnector:
            connector_type = "git"

            def list_page(self, cursor, *, limit):
                del cursor, limit
                return ConnectorPage((listed_ref,), complete=True)

            def fetch(self, ref):
                return FetchedSource(fetched_ref or ref, b"", acl=acl)

        result = ConnectorSyncRuntime(
            store, limits=SyncLimits(max_total_bytes=8)
        ).run(job["job_id"], OverheadConnector(), Sink())

        assert result["status"] == "failed"
        assert result["error_code"] == "SYNCBUDGETEXCEEDED"
        store.close()


def test_recovering_expired_cancelled_lease_emits_cancelled_without_build_work(
    tmp_path,
):
    class Clock:
        value = 100.0

        def __call__(self):
            return self.value

    class Observer:
        def __init__(self):
            self.cancelled_jobs = []

        def __getattr__(self, name):
            if name == "cancelled":
                return lambda observation: self.cancelled_jobs.append(
                    observation.job_id
                )
            return lambda observation: None

    clock = Clock()
    store = ConnectorSyncStore(str(tmp_path / "state.db"), clock=clock)
    job = store.create(
        tenant_id="tenant",
        kb_id="kb",
        connection_id="connection",
        connector_type="git",
    )
    store.acquire(job["job_id"], lease_seconds=5)
    store.request_cancel(job["job_id"])
    clock.value = 106.0
    observer = Observer()

    result = ConnectorSyncRuntime(
        store, monotonic=clock, observer=observer
    ).run(job["job_id"], object(), object())

    assert result["status"] == "cancelled"
    assert observer.cancelled_jobs == [job["job_id"]]
    store.close()
