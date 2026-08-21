from __future__ import annotations

import os
import threading
import time
import uuid

import pytest

from cogdoc.ha.index_generation import GEN_PREPARED, IndexGenerationStore
from cogdoc.ha.outbox import OUTBOX_DELIVERED, OutboxStore
from cogdoc.ha.postgres import PostgresBackend
from cogdoc.ha.scheduler import (
    DistributedScheduler,
    SCHEDULE_ONCE,
    ScheduleStore,
)
from cogdoc.ha.tasks import LeaseJobStore


@pytest.mark.skipif(
    not os.environ.get("COGDOC_TEST_POSTGRES_DSN"),
    reason="COGDOC_TEST_POSTGRES_DSN is not configured",
)
def test_real_postgres_skip_locked_claim_and_schema_bootstrap():
    dsn = os.environ["COGDOC_TEST_POSTGRES_DSN"]
    schema = f"cogdoc_test_{uuid.uuid4().hex[:16]}"
    backend_a = PostgresBackend(dsn, schema=schema, max_size=4)
    backend_b = PostgresBackend(dsn, schema=schema, max_size=4)
    try:
        jobs_a = LeaseJobStore(backend_a)
        jobs_b = LeaseJobStore(backend_b)
        queued = jobs_a.enqueue("index", "tenant", {"kb": "kb"}, idempotency_key="one")
        barrier = threading.Barrier(2)
        claims = []

        def claim(store, worker):
            barrier.wait()
            claims.append(store.claim("index", worker))

        threads = [
            threading.Thread(target=claim, args=(jobs_a, "a")),
            threading.Thread(target=claim, args=(jobs_b, "b")),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        winners = [row for row in claims if row is not None]
        assert len(winners) == 1
        assert winners[0]["job_id"] == queued["job_id"]

        schedules = ScheduleStore(backend_a)
        schedules.create(
            "tenant",
            "scheduled",
            {"kind": "durable"},
            schedule_type=SCHEDULE_ONCE,
            schedule_spec=str(time.time() - 1),
        )
        assert DistributedScheduler(schedules, jobs_a).run_once() == (1, 1)
        assert len(jobs_a.list_jobs(queue="scheduled")) == 1

        outbox = OutboxStore(backend_a)
        authority = IndexGenerationStore(backend_a)
        generation = authority.begin_build("tenant", "kb", "build", "worker")
        manifest = {
            "schema_version": "index-manifest-v1",
            "contract": {
                "chunk_version": "v1",
                "embedding_model": "model",
                "dimensions": 3,
            },
            "files": [],
        }
        prepared = authority.prepare(
            generation["generation_id"], generation["lease_token"], manifest
        )
        assert prepared["status"] == GEN_PREPARED

        def append_publication(connection, candidate):
            outbox.append(
                connection,
                tenant_id="tenant",
                topic="index.published",
                aggregate_type="knowledge_base",
                aggregate_id="kb",
                aggregate_revision=int(candidate["fencing_token"]),
                payload={"generation_id": candidate["generation_id"]},
                idempotency_key=f"index:{candidate['generation_id']}",
            )

        published = authority.publish(
            prepared["generation_id"],
            prepared["lease_token"],
            lambda _candidate: None,
            on_publish=append_publication,
        )
        assert (
            authority.current("tenant", "kb")["generation_id"]
            == published["generation_id"]
        )
        event = outbox.claim("dispatcher")
        assert event is not None
        delivered = outbox.delivered(event["event_id"], event["lease_token"])
        assert delivered["status"] == OUTBOX_DELIVERED

        candidate = authority.begin_build("tenant", "kb", "build-rollback", "worker")
        candidate = authority.prepare(
            candidate["generation_id"], candidate["lease_token"], manifest
        )

        def fail_publication(connection, row):
            outbox.append(
                connection,
                tenant_id="tenant",
                topic="index.published",
                aggregate_type="knowledge_base",
                aggregate_id="kb",
                aggregate_revision=int(row["fencing_token"]),
                payload={"generation_id": row["generation_id"]},
                idempotency_key=f"index:{row['generation_id']}",
            )
            raise RuntimeError("rollback publication")

        with pytest.raises(RuntimeError, match="rollback publication"):
            authority.publish(
                candidate["generation_id"],
                candidate["lease_token"],
                lambda _candidate: None,
                on_publish=fail_publication,
            )
        assert (
            authority.current("tenant", "kb")["generation_id"]
            == published["generation_id"]
        )
        with backend_a.transaction() as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM ha_outbox WHERE idempotency_key=%s",
                (f"index:{candidate['generation_id']}",),
            ).fetchone()
        assert next(iter(count.values())) == 0
    finally:
        # Test-only teardown uses a separately pooled autocommit connection;
        # production migrations never drop schemas.
        with backend_a._pool.connection() as connection:
            connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        backend_a.close()
        backend_b.close()
