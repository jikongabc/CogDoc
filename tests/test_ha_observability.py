from __future__ import annotations

from cogdoc.api.metrics import Metrics
from cogdoc.ha.observability import HAOperationalSnapshot
from cogdoc.ha.runtime import HAConfig, HARuntime


def _config(tmp_path):
    return HAConfig(
        enabled=True,
        database_url="",
        database_schema="cogdoc",
        object_store="local",
        object_root=str(tmp_path / "objects"),
        s3_bucket="",
        s3_prefix="cogdoc",
        s3_endpoint_url=None,
        s3_region=None,
        s3_require_versioning=True,
        worker_id="metrics-worker",
        scheduler_enabled=False,
        outbox_enabled=False,
    )


def test_operational_snapshot_uses_bounded_status_dimensions(tmp_path):
    runtime = HARuntime(_config(tmp_path))
    queued = runtime.jobs.enqueue("work", "tenant", {})
    dead = runtime.jobs.enqueue("work", "tenant", {}, max_attempts=1)
    claimed = runtime.jobs.claim("work", "worker", lease_seconds=1)
    assert claimed is not None
    # The first claim may select either same-priority row; force that row dead.
    runtime.jobs.fail(
        claimed["job_id"],
        claimed["lease_token"],
        "FAILED",
        retryable=False,
    )
    snapshot = HAOperationalSnapshot(runtime).collect()
    assert sum(snapshot["jobs"].values()) == 2
    assert snapshot["jobs"]["failed"] == 1
    assert snapshot["jobs"]["queued"] == 1
    assert snapshot["expired_job_leases"] == 0
    assert snapshot["current_generations"] == 0
    assert snapshot["live_instances"] == 0
    assert queued["job_id"] != dead["job_id"]
    runtime.shutdown()


def test_metrics_scrape_refreshes_ha_snapshot_without_identifier_labels(tmp_path):
    runtime = HARuntime(_config(tmp_path))
    runtime.jobs.enqueue("sensitive-queue", "tenant-secret", {})
    metrics = Metrics()
    metrics.bind_ha(runtime)

    rendered = metrics.render().decode()
    assert 'cogdoc_ha_jobs{status="queued"} 1.0' in rendered
    assert "cogdoc_ha_snapshot_up 1.0" in rendered
    assert "tenant-secret" not in rendered
    assert "sensitive-queue" not in rendered
    runtime.shutdown()


def test_metrics_snapshot_failure_is_explicit_not_silently_stale(tmp_path):
    runtime = HARuntime(_config(tmp_path))
    metrics = Metrics()
    metrics.bind_ha(runtime)
    runtime.shutdown()

    rendered = metrics.render().decode()
    assert "cogdoc_ha_snapshot_up 0.0" in rendered
