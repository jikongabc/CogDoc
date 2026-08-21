from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from cogdoc.api.app import create_app
from cogdoc.api.ingest import KnowledgeBaseRegistry
from cogdoc.ha.object_store import LocalObjectStore
from cogdoc.ha.runtime import HAConfig, HARuntime
from cogdoc.ha.scheduler import SCHEDULE_INTERVAL
from cogdoc.ha.storage import SQLiteBackend


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _runtime(tmp_path):
    backend = SQLiteBackend(tmp_path / "ha.db")
    config = HAConfig(
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
        worker_id="api-worker",
        scheduler_enabled=False,
        outbox_enabled=False,
        maintenance_enabled=False,
        index_worker_enabled=False,
        index_reads_enabled=False,
    )
    return (
        HARuntime(
            config,
            backend=backend,
            object_store=LocalObjectStore(tmp_path / "objects"),
        ),
        backend,
    )


@pytest.mark.anyio
async def test_ha_operations_are_admin_only_tenant_scoped_and_redacted(tmp_path):
    runtime, backend = _runtime(tmp_path)
    registry = KnowledgeBaseRegistry(
        str(tmp_path / "registry.json"),
        source_dir_for=lambda storage_id: str(tmp_path / storage_id / "sources"),
    )
    record = registry.create("docs", "tenant-a", "alice")
    other = registry.create("private", "tenant-b", "bob")
    own_job = runtime.jobs.enqueue(
        "index-build", "tenant-a", {"kb_id": record["storage_id"], "secret": "marker-a"}
    )
    second_own_job = runtime.jobs.enqueue(
        "index-build", "tenant-a", {"kb_id": record["storage_id"], "secret": "marker-c"}
    )
    foreign_job = runtime.jobs.enqueue(
        "index-build", "tenant-b", {"kb_id": other["storage_id"], "secret": "marker-b"}
    )
    own_schedule = runtime.schedules.create(
        "tenant-a",
        "index-build",
        {"kb_id": record["storage_id"], "secret": "schedule-marker"},
        schedule_type=SCHEDULE_INTERVAL,
        schedule_spec="60",
    )
    foreign_schedule = runtime.schedules.create(
        "tenant-b",
        "index-build",
        {"kb_id": other["storage_id"]},
        schedule_type=SCHEDULE_INTERVAL,
        schedule_spec="60",
    )
    own_generation = runtime.index_generations.begin_build(
        "tenant-a", record["storage_id"], "build-a", "builder-a"
    )
    foreign_generation = runtime.index_generations.begin_build(
        "tenant-b", other["storage_id"], "build-b", "builder-b"
    )
    app = create_app(
        kb_registry=registry,
        ha_runtime=runtime,
        api_principals={
            "owner-a": {
                "tenant_id": "tenant-a",
                "subject_id": "alice",
                "role": "owner",
            },
            "viewer-a": {
                "tenant_id": "tenant-a",
                "subject_id": "amy",
                "role": "viewer",
            },
            "owner-b": {
                "tenant_id": "tenant-b",
                "subject_id": "bob",
                "role": "owner",
            },
        },
    )
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            viewer = await client.get("/v1/ha/jobs", headers={"X-API-Key": "viewer-a"})
            assert viewer.status_code == 403

            jobs = await client.get(
                "/v1/ha/jobs?limit=1", headers={"X-API-Key": "owner-a"}
            )
            assert jobs.status_code == 200
            body = jobs.json()
            assert body["jobs"][0]["job_id"] in {
                own_job["job_id"],
                second_own_job["job_id"],
            }
            assert "payload" not in body["jobs"][0]
            assert "lease_token" not in body["jobs"][0]
            assert "marker-a" not in jobs.text and "marker-b" not in jobs.text
            assert body["next_cursor"] is not None
            cursor_page = await client.get(
                "/v1/ha/jobs",
                params={"limit": 1, **body["next_cursor"]},
                headers={"X-API-Key": "owner-a"},
            )
            assert cursor_page.status_code == 200
            assert {
                body["jobs"][0]["job_id"],
                cursor_page.json()["jobs"][0]["job_id"],
            } == {own_job["job_id"], second_own_job["job_id"]}
            assert cursor_page.json()["next_cursor"] is None

            hidden = await client.get(
                f"/v1/ha/jobs/{foreign_job['job_id']}",
                headers={"X-API-Key": "owner-a"},
            )
            assert hidden.status_code == 404
            cancelled = await client.post(
                f"/v1/ha/jobs/{own_job['job_id']}/cancel",
                headers={"X-API-Key": "owner-a"},
            )
            assert cancelled.status_code == 200
            assert cancelled.json()["status"] == "cancelled"

            schedules = await client.get(
                "/v1/ha/schedules", headers={"X-API-Key": "owner-a"}
            )
            assert schedules.status_code == 200
            assert [row["schedule_id"] for row in schedules.json()["schedules"]] == [
                own_schedule["schedule_id"]
            ]
            assert "payload" not in schedules.json()["schedules"][0]
            assert "schedule-marker" not in schedules.text
            paused = await client.patch(
                f"/v1/ha/schedules/{own_schedule['schedule_id']}",
                json={"enabled": False, "expected_revision": 1},
                headers={"X-API-Key": "owner-a"},
            )
            assert paused.status_code == 200
            assert paused.json()["enabled"] is False
            stale = await client.patch(
                f"/v1/ha/schedules/{own_schedule['schedule_id']}",
                json={"enabled": True, "expected_revision": 1},
                headers={"X-API-Key": "owner-a"},
            )
            assert stale.status_code == 409
            hidden_schedule = await client.patch(
                f"/v1/ha/schedules/{foreign_schedule['schedule_id']}",
                json={"enabled": False, "expected_revision": 1},
                headers={"X-API-Key": "owner-a"},
            )
            assert hidden_schedule.status_code == 404

            generations = await client.get(
                "/v1/ha/index-generations",
                headers={"X-API-Key": "owner-a"},
            )
            assert generations.status_code == 200
            rows = generations.json()["generations"]
            assert [row["generation_id"] for row in rows] == [
                own_generation["generation_id"]
            ]
            assert rows[0]["kb_id"] == "docs"
            assert rows[0]["kb_available"] is True
            assert record["storage_id"] not in generations.text
            assert other["storage_id"] not in generations.text
            assert own_generation["lease_token"] not in generations.text
            assert foreign_generation["generation_id"] not in generations.text
    backend.close()


@pytest.mark.anyio
async def test_dead_letter_replay_is_tenant_scoped_and_idempotent(tmp_path):
    runtime, backend = _runtime(tmp_path)
    source = runtime.jobs.enqueue(
        "work", "tenant-a", {"secret": "hidden"}, max_attempts=1
    )
    claimed = runtime.jobs.claim("work", "worker")
    assert claimed is not None
    runtime.jobs.fail(
        source["job_id"], claimed["lease_token"], "UPSTREAM", retryable=True
    )
    foreign = runtime.jobs.enqueue("work", "tenant-b", {}, max_attempts=1)
    foreign_claim = runtime.jobs.claim("work", "worker")
    assert foreign_claim is not None
    runtime.jobs.fail(
        foreign["job_id"],
        foreign_claim["lease_token"],
        "UPSTREAM",
        retryable=True,
    )
    app = create_app(
        ha_runtime=runtime,
        api_principals={
            "owner-a": {
                "tenant_id": "tenant-a",
                "subject_id": "alice",
                "role": "owner",
            }
        },
    )
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            first = await client.post(
                f"/v1/ha/jobs/{source['job_id']}/replay",
                json={"replay_key": "operator-incident-1"},
                headers={"X-API-Key": "owner-a"},
            )
            second = await client.post(
                f"/v1/ha/jobs/{source['job_id']}/replay",
                json={"replay_key": "operator-incident-1"},
                headers={"X-API-Key": "owner-a"},
            )
            assert first.status_code == second.status_code == 201
            assert first.json()["job_id"] == second.json()["job_id"]
            assert first.json()["replay_of"] == source["job_id"]
            assert "payload" not in first.json()
            hidden = await client.post(
                f"/v1/ha/jobs/{foreign['job_id']}/replay",
                json={"replay_key": "foreign"},
                headers={"X-API-Key": "owner-a"},
            )
            assert hidden.status_code == 409
    backend.close()


@pytest.mark.anyio
async def test_ha_operations_fail_closed_when_runtime_is_disabled(tmp_path):
    app = create_app(
        api_principals={
            "owner": {
                "tenant_id": "tenant-a",
                "subject_id": "alice",
                "role": "owner",
            }
        }
    )
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/v1/ha/jobs", headers={"X-API-Key": "owner"})
    assert response.status_code == 503
