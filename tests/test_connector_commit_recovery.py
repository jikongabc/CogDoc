import asyncio
import hashlib
import json
import time
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from cogdoc.api.app import create_app
from cogdoc.api.ingest import IndexJobManager, KnowledgeBaseRegistry
from cogdoc.connectors.base import ConnectorPage, ConnectorSourceRef, FetchedSource
from cogdoc.connectors.connection_store import ConnectionStore
from cogdoc.connectors.materialized_sink import MaterializedSyncSink
from cogdoc.connectors.sync_runtime import ConnectorSyncRuntime, SyncLimits
from cogdoc.connectors.sync_store import ConnectorSyncStore
from cogdoc.service.source_artifact_store import SourceArtifactStore
from cogdoc.service.source_catalog import SourceCatalog


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def _wait_for_sync_job(client, kb_id, job_id):
    deadline = time.monotonic() + 5
    response = None
    while time.monotonic() < deadline:
        response = await client.get(f"/v1/knowledge-bases/{kb_id}/sync-jobs/{job_id}")
        payload = response.json()
        if payload["status"] not in {"pending", "running", "committing"}:
            return payload
        await asyncio.sleep(0.03)
    raise AssertionError(response.text if response is not None else "sync timed out")


@pytest.mark.anyio
async def test_local_sync_succeeds_during_second_lifespan_of_same_app(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("cogdoc.api.app.configure_logging", lambda: None)
    database = str(tmp_path / "connector.db")
    provider_root = tmp_path / "provider"
    provider_root.mkdir()
    (provider_root / "guide.md").write_text(
        "# Guide\n\nStable content for a real local connector synchronization.",
        encoding="utf-8",
    )

    def source_dir_for(kb_id):
        return str(tmp_path / "knowledge-bases" / kb_id / "sources")

    registry = KnowledgeBaseRegistry(
        registry_path=str(tmp_path / "registry.json"), source_dir_for=source_dir_for
    )
    index_jobs = IndexJobManager(
        ingest_fn=lambda *_: SimpleNamespace(
            document_count=1, chunk_count=1, ocr_summary={}
        ),
        source_dir_for=source_dir_for,
        kb_exists=registry.exists,
    )
    connections = ConnectionStore(database)
    sync_jobs = ConnectorSyncStore(database)
    catalog = SourceCatalog(database)
    app = create_app(
        kb_registry=registry,
        index_jobs=index_jobs,
        connection_store=connections,
        connector_sync_store=sync_jobs,
        source_catalog=catalog,
        source_artifact_store=SourceArtifactStore(tmp_path / "artifacts"),
        close_state_runtime_on_shutdown=False,
    )

    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            created = await client.post("/v1/knowledge-bases", json={"kb_id": "docs"})
            assert created.status_code == 201, created.text

    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            created = await client.post(
                "/v1/knowledge-bases/docs/connections",
                json={
                    "connector_type": "local-directory",
                    "name": "Local docs",
                    "config": {"root": str(provider_root)},
                    "secret_env": {},
                    "workspace_visible": True,
                },
            )
            assert created.status_code == 201, created.text
            started = await client.post(
                "/v1/knowledge-bases/docs/connections/"
                f"{created.json()['connection_id']}/sync"
            )
            assert started.status_code == 202, started.text
            result = await _wait_for_sync_job(client, "docs", started.json()["job_id"])
            assert result["status"] == "succeeded", json.dumps(result, sort_keys=True)

    assert len(catalog.list_sources("default", "docs")) == 1
    connections.close()
    sync_jobs.close()
    catalog.close()


class _OneDocumentConnector:
    connector_type = "local-directory"

    def __init__(self, content):
        self.content = content

    def list_page(self, cursor, *, limit):
        del cursor, limit
        return ConnectorPage(
            (
                ConnectorSourceRef(
                    "guide.md",
                    "guide.md",
                    content_sha256=hashlib.sha256(self.content).hexdigest(),
                    byte_size=len(self.content),
                ),
            ),
            complete=True,
            snapshot=True,
        )

    def fetch(self, ref):
        return FetchedSource(ref, self.content)


def test_retry_after_materialized_commit_failure_recovers_without_duplicates(
    tmp_path,
):
    database = str(tmp_path / "connector.db")
    store = ConnectorSyncStore(database)
    catalog = SourceCatalog(database)
    submitted = []

    def transient_index_submitter(kb_id):
        submitted.append(kb_id)
        if len(submitted) == 1:
            raise RuntimeError("index queue is temporarily unavailable")
        return {}

    def new_sink():
        sink = MaterializedSyncSink(
            source_dir=str(tmp_path / "sources"),
            catalog=catalog,
            index_submitter=transient_index_submitter,
            owner_id="owner",
            workspace_visible=False,
        )
        return sink

    job = store.create(
        tenant_id="tenant",
        kb_id="docs",
        connection_id="conn-1",
        connector_type="local-directory",
    )
    runtime = ConnectorSyncRuntime(store, limits=SyncLimits(retry_base_seconds=0))
    connector = _OneDocumentConnector(
        b"# Guide\n\nCrash-recoverable connector content."
    )

    first = runtime.run(job["job_id"], connector, new_sink())
    assert first["status"] == "committing", first
    journals = list((tmp_path / ".sources.sync-work").glob("*.journal.json"))
    assert len(journals) == 1
    assert json.loads(journals[0].read_text(encoding="utf-8"))["phase"] in {
        "swapped",
        "materialized",
    }

    retried = runtime.run(job["job_id"], connector, new_sink())
    assert retried["status"] == "succeeded", json.dumps(retried, sort_keys=True)
    assert submitted == ["docs", "docs"]
    assert not list((tmp_path / ".sources.sync-work").glob("*.journal.json"))
    assert len(catalog.list_sources("tenant", "docs")) == 1
    assert len(list((tmp_path / "sources").glob("*.md"))) == 1
    assert len(list((tmp_path / "sources" / ".connections").rglob("*.md"))) == 1

    store.close()
    catalog.close()


def test_index_timeout_keeps_sync_committing_and_reuses_job_on_retry(tmp_path):
    database = str(tmp_path / "connector.db")
    store = ConnectorSyncStore(database)
    catalog = SourceCatalog(database)
    submitted = []
    statuses = {}

    def submit_index(_kb_id):
        job_id = f"index-{len(submitted) + 1}"
        submitted.append(job_id)
        statuses[job_id] = "running"
        return {"job_id": job_id}

    def new_sink():
        return MaterializedSyncSink(
            source_dir=str(tmp_path / "sources"),
            catalog=catalog,
            index_submitter=submit_index,
            index_status_reader=lambda job_id: {"status": statuses[job_id]},
            owner_id="owner",
            workspace_visible=False,
            index_timeout_seconds=0.01,
        )

    job = store.create(
        tenant_id="tenant",
        kb_id="docs",
        connection_id="conn-1",
        connector_type="local-directory",
    )
    runtime = ConnectorSyncRuntime(store, limits=SyncLimits(retry_base_seconds=0))
    connector = _OneDocumentConnector(b"# Guide\n\nBounded index recovery.")

    timed_out = runtime.run(job["job_id"], connector, new_sink())
    assert timed_out["status"] == "committing", timed_out
    journals = list((tmp_path / ".sources.sync-work").glob("*.journal.json"))
    assert len(journals) == 1
    journal = json.loads(journals[0].read_text(encoding="utf-8"))
    assert journal["phase"] == "materialized"
    assert journal["index_job_id"] == "index-1"
    assert submitted == ["index-1"]

    still_running = runtime.run(job["job_id"], connector, new_sink())
    assert still_running["status"] == "committing", still_running
    assert submitted == ["index-1"]

    statuses["index-1"] = "succeeded"
    recovered = runtime.run(job["job_id"], connector, new_sink())
    assert recovered["status"] == "succeeded", recovered
    assert submitted == ["index-1"]
    assert not list((tmp_path / ".sources.sync-work").glob("*.journal.json"))

    store.close()
    catalog.close()


def test_committing_job_recovers_when_process_dies_before_journal_write(tmp_path):
    database = str(tmp_path / "connector.db")
    now = [100.0]
    store = ConnectorSyncStore(database, clock=lambda: now[0])
    catalog = SourceCatalog(database)
    submitted = []

    def new_sink():
        return MaterializedSyncSink(
            source_dir=str(tmp_path / "sources"),
            catalog=catalog,
            index_submitter=lambda kb_id: submitted.append(kb_id) or {},
            owner_id="owner",
            workspace_visible=False,
        )

    content = b"# Guide\n\nDurable staging before the commit authority boundary."
    connector = _OneDocumentConnector(content)
    job = store.create(
        tenant_id="tenant",
        kb_id="docs",
        connection_id="conn-1",
        connector_type="local-directory",
    )
    acquired, token = store.acquire(job["job_id"], lease_seconds=1)
    sink = new_sink()
    sink.begin(
        job_id=job["job_id"],
        tenant_id="tenant",
        kb_id="docs",
        connection_id="conn-1",
        connector_type="local-directory",
        attempt=acquired["attempt"],
    )
    page = connector.list_page(None, limit=100)
    fetched = connector.fetch(page.items[0])
    sink.upsert(
        fetched.document(
            connector.connector_type,
            content_sha256=hashlib.sha256(content).hexdigest(),
        ),
        content,
    )
    counters = {
        "pages_processed": 1,
        "documents_seen": 1,
        "documents_fetched": 1,
        "deleted_seen": 0,
        "bytes_fetched": len(content),
    }
    store.checkpoint(
        job["job_id"],
        token,
        cursor=None,
        counters=counters,
        lease_seconds=1,
    )
    sink.prepare_commit(snapshot=True, seen_external_ids=frozenset({"guide.md"}))
    store.prepare_commit(job["job_id"], token)
    assert not list((tmp_path / ".sources.sync-work").glob("*.journal.json"))

    # Simulate process loss after the DB authority transition but before the
    # first journal rename. The private final staging tree is sufficient to
    # synthesize the prepared journal and complete without provider replay.
    now[0] = 102.0

    class ProviderMustNotReplay:
        connector_type = "local-directory"

        def list_page(self, cursor, *, limit):
            raise AssertionError((cursor, limit))

        def fetch(self, ref):
            raise AssertionError(ref)

    runtime = ConnectorSyncRuntime(
        store,
        limits=SyncLimits(lease_seconds=1, retry_base_seconds=0),
    )
    recovered = runtime.run(job["job_id"], ProviderMustNotReplay(), new_sink())
    assert recovered["status"] == "succeeded"
    assert submitted == ["docs"]
    assert len(catalog.list_sources("tenant", "docs")) == 1
    assert not list((tmp_path / ".sources.sync-work").glob("*.journal.json"))

    store.close()
    catalog.close()
