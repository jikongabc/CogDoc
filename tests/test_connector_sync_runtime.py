from dataclasses import dataclass, field

from cogdoc.connectors.base import (
    ConnectorPage,
    ConnectorSourceRef,
    FetchedSource,
    RetryableConnectorError,
)
from cogdoc.connectors.sync_runtime import ConnectorSyncRuntime, SyncLimits
from cogdoc.connectors.sync_store import ConnectorSyncStore


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
