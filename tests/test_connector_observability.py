from concurrent.futures import ThreadPoolExecutor
from threading import Event

from prometheus_client import generate_latest

from cogdoc.api.metrics import Metrics
from cogdoc.connectors.sync_observer import SyncObservation
from cogdoc.service.connector_observability import ConnectorOperationsObserver


class _Catalog:
    def __init__(self):
        self.calls = []

    def record_connection_health(
        self,
        tenant_id,
        kb_id,
        connection_id,
        health_status,
        *,
        last_sync_at=None,
        last_sync_error=None,
        job_sequence=0,
        job_attempt=0,
        event_rank=0,
    ):
        self.calls.append(
            {
                "tenant_id": tenant_id,
                "kb_id": kb_id,
                "connection_id": connection_id,
                "health_status": health_status,
                "last_sync_at": last_sync_at,
                "last_sync_error": last_sync_error,
                "job_sequence": job_sequence,
                "job_attempt": job_attempt,
                "event_rank": event_rank,
            }
        )


def _observation(kind, *, error_code=None):
    return SyncObservation(
        kind=kind,
        job_id="sync-secret-identifier",
        tenant_id="tenant-secret-identifier",
        kb_id="kb-secret-identifier",
        connection_id="connection-secret-identifier",
        connector_type="notion",
        job_sequence=7,
        attempt=2,
        duration_seconds=3.5,
        backlog=4,
        counters={"documents_fetched": 7, "bytes_fetched": 42},
        error_code=error_code,
    )


def test_connector_observer_updates_health_metrics_and_secret_free_webhook():
    metrics = Metrics()
    catalog = _Catalog()
    webhooks = []
    observer = ConnectorOperationsObserver(
        metrics,
        catalog,
        webhook_submitter=lambda event, payload: webhooks.append((event, payload)),
        clock=lambda: 1234.0,
    )

    observer.started(_observation("started"))
    observer.succeeded(_observation("succeeded"))

    assert [row["health_status"] for row in catalog.calls] == [
        "syncing",
        "healthy",
    ]
    assert catalog.calls[-1]["last_sync_at"] == 1234.0
    assert webhooks[0][0] == "connector.sync.succeeded"
    assert webhooks[0][1]["connection_id"] == "connection-secret-identifier"
    assert webhooks[0][1]["job_sequence"] == 7
    assert webhooks[0][1]["event_sequence"] == 233
    assert webhooks[0][1]["event_rank"] == 33
    assert webhooks[0][1]["outcome"] == "succeeded"
    assert "secret_values" not in webhooks[0][1]

    exposition = generate_latest(metrics.registry).decode()
    assert 'connector_type="notion",outcome="succeeded"' in exposition
    # Operational identifiers belong in webhook/log correlation only, never
    # Prometheus labels or help text.
    assert "connection-secret-identifier" not in exposition
    assert "tenant-secret-identifier" not in exposition
    assert "kb-secret-identifier" not in exposition
    assert "sync-secret-identifier" not in exposition


def test_connector_observer_projects_retry_error_without_exception_text():
    metrics = Metrics()
    catalog = _Catalog()
    webhooks = []
    observer = ConnectorOperationsObserver(
        metrics,
        catalog,
        webhook_submitter=lambda event, payload: webhooks.append((event, payload)),
    )
    observer.retry(_observation("retry", error_code="RATELIMITED"))
    assert catalog.calls[0]["health_status"] == "degraded"
    assert catalog.calls[0]["last_sync_error"] == "RATELIMITED"
    assert webhooks[0][1]["error_code"] == "RATELIMITED"


def test_cancelled_projects_stale_and_reconcile_has_no_event_side_effects():
    metrics = Metrics()
    catalog = _Catalog()
    webhooks = []
    observer = ConnectorOperationsObserver(
        metrics,
        catalog,
        webhook_submitter=lambda event, payload: webhooks.append((event, payload)),
    )

    observer.cancelled(_observation("cancelled"))
    assert catalog.calls[-1]["health_status"] == "stale"
    assert webhooks[-1][0] == "connector.sync.cancelled"
    event_count = generate_latest(metrics.registry).decode()

    catalog.calls.clear()
    webhooks.clear()
    observer.reconcile(_observation("succeeded"))
    assert catalog.calls[-1]["health_status"] == "healthy"
    assert webhooks == []
    assert generate_latest(metrics.registry).decode() == event_count


def test_catalog_attempt_watermark_rejects_old_observer_after_check(tmp_path):
    from cogdoc.service.source_catalog import SourceCatalog
    from cogdoc.source_model import SourceDocument

    catalog = SourceCatalog(str(tmp_path / "catalog.db"))
    document = SourceDocument.create(
        connector_type="notion",
        external_id="page-1",
        display_name="page-1",
        content_sha256="a" * 64,
        metadata={"connection_id": "connection-secret-identifier"},
    )
    catalog.upsert("tenant-secret-identifier", "kb-secret-identifier", document)
    entered = Event()
    release = Event()

    def checker(observation):
        if observation.kind == "retry" and observation.attempt == 1:
            entered.set()
            assert release.wait(timeout=5)
        return True

    observer = ConnectorOperationsObserver(
        Metrics(), catalog, current_job_checker=checker
    )
    old = SyncObservation(
        **{**_observation("retry").__dict__, "attempt": 1}
    )
    newer = SyncObservation(
        **{
            **old.__dict__,
            "kind": "succeeded",
            "attempt": 2,
            "error_code": None,
        }
    )
    with ThreadPoolExecutor(max_workers=2) as executor:
        old_write = executor.submit(observer.retry, old)
        assert entered.wait(timeout=5)
        observer.succeeded(newer)
        release.set()
        old_write.result(timeout=5)

    persisted = catalog.get(
        "tenant-secret-identifier", "kb-secret-identifier", document.source_id
    )
    assert persisted["health_status"] == "healthy"
    assert persisted["last_sync_error"] is None
    assert persisted["health_job_sequence"] == 7
    assert persisted["health_job_attempt"] == 2
    assert persisted["health_event_rank"] == 33
    catalog.close()
