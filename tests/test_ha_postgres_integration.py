from __future__ import annotations

import os
import hashlib
import threading
import time
import uuid

import pytest

from cogdoc.ha.index_generation import GEN_PREPARED, IndexGenerationStore
from cogdoc.ha.api_state import (
    DistributedKnowledgeBaseRegistry,
    DistributedMutationCoordinator,
    MutationBusy,
)
from cogdoc.ha.object_store import LocalObjectStore
from cogdoc.ha.outbox import OUTBOX_DELIVERED, OutboxStore
from cogdoc.ha.postgres import PostgresBackend
from cogdoc.ha.scheduler import (
    DistributedScheduler,
    SCHEDULE_ONCE,
    ScheduleStore,
)
from cogdoc.ha.tasks import LeaseJobStore
from cogdoc.ha.source_generation import SourceGenerationStore
from cogdoc.ha.source_catalog import DistributedSourceCatalog
from cogdoc.ha.source_artifact_store import DistributedSourceArtifactStore
from cogdoc.ha.tenant_quota import DistributedTenantQuotaManager
from cogdoc.api.tenant_quota import TenantQuotaPolicy
from cogdoc.api.auth_store import AuthAuthenticationError, AuthStore
from cogdoc.api.research_job_store import SqliteResearchJobStore
from cogdoc.api.oidc import OIDCConfigurationError, OIDCFlowStore
from cogdoc.api.resource_access import ResourceAccessStore
from cogdoc.source_model import SourceDocument
from cogdoc.connectors.connection_store import ConnectionStore
from cogdoc.connectors.credential_store import CredentialVault
from cogdoc.connectors.oauth import OAuthSessionStore
from cogdoc.connectors.sync_store import ConnectorSyncStore
from cogdoc.ha.research import ResearchDispatchStore
from cogdoc.ha.session_store import DistributedSessionStore, SessionBusy


@pytest.mark.skipif(
    not os.environ.get("COGDOC_TEST_POSTGRES_DSN"),
    reason="COGDOC_TEST_POSTGRES_DSN is not configured",
)
def test_real_postgres_exported_snapshot_is_stable_across_writers():
    dsn = os.environ["COGDOC_TEST_POSTGRES_DSN"]
    schema = f"cogdoc_snapshot_{uuid.uuid4().hex[:16]}"
    exporter = PostgresBackend(dsn, schema=schema, max_size=2)
    writer = PostgresBackend(dsn, schema=schema, max_size=2)
    try:
        with exporter.transaction(write=True) as connection:
            connection.execute("CREATE TABLE snapshot_probe(value INTEGER NOT NULL)")
            connection.execute("INSERT INTO snapshot_probe(value) VALUES(1)")
        with exporter.exported_snapshot(statement_timeout_seconds=60) as (
            connection,
            snapshot_id,
        ):
            assert snapshot_id
            before = connection.execute(
                "SELECT COUNT(*) AS value FROM snapshot_probe"
            ).fetchone()
            with writer.transaction(write=True) as other:
                other.execute("INSERT INTO snapshot_probe(value) VALUES(2)")
            after = connection.execute(
                "SELECT COUNT(*) AS value FROM snapshot_probe"
            ).fetchone()
            assert int(before["value"]) == 1
            assert int(after["value"]) == 1
        with exporter.transaction() as connection:
            current = connection.execute(
                "SELECT COUNT(*) AS value FROM snapshot_probe"
            ).fetchone()
        assert int(current["value"]) == 2
    finally:
        writer.close()
        exporter.close()


@pytest.mark.skipif(
    not os.environ.get("COGDOC_TEST_POSTGRES_DSN"),
    reason="COGDOC_TEST_POSTGRES_DSN is not configured",
)
def test_real_postgres_connector_control_plane_contract():
    dsn = os.environ["COGDOC_TEST_POSTGRES_DSN"]
    schema = f"cogdoc_connectors_{uuid.uuid4().hex[:16]}"
    first_backend = PostgresBackend(dsn, schema=schema, max_size=4)
    second_backend = PostgresBackend(dsn, schema=schema, max_size=4)
    try:
        first_connections = ConnectionStore(backend=first_backend)
        second_connections = ConnectionStore(backend=second_backend)
        created = first_connections.create(
            tenant_id="tenant",
            kb_id="kb",
            connector_type="url",
            name="web",
            config={"urls": ["https://example.com/docs"]},
            secret_env={},
            owner_id="owner",
        )
        assert second_connections.get(created["connection_id"])["name"] == "web"

        first_sync = ConnectorSyncStore(backend=first_backend)
        second_sync = ConnectorSyncStore(backend=second_backend)
        job = first_sync.create_if_idle(
            tenant_id="tenant",
            kb_id="kb",
            connection_id=created["connection_id"],
            connector_type="url",
            connection_revision=created["revision"],
        )
        acquired, token = second_sync.acquire(job["job_id"], lease_seconds=30)
        assert acquired["status"] == "running" and token

        keys = {"v1": b"k" * 32}
        first_vault = CredentialVault(
            backend=first_backend, master_keys=keys, active_key_version="v1"
        )
        second_vault = CredentialVault(
            backend=second_backend, master_keys=keys, active_key_version="v1"
        )
        credential = first_vault.create(
            tenant_id="tenant",
            kb_id="kb",
            connection_id=None,
            provider="notion",
            credential_kind="token",
            label="shared",
            secret_values={"token": "secret"},
            actor_id="owner",
        )
        assert second_vault.get_for_use(
            credential["credential_id"],
            tenant_id="tenant",
            kb_id="kb",
            connection_id=None,
            actor_id="sync",
        ) == {"token": "secret"}

        sessions = OAuthSessionStore(None, first_vault, backend=first_backend)
        started = sessions.create(
            provider="notion",
            tenant_id="tenant",
            kb_id="kb",
            connection_id=None,
            user_id="owner",
            redirect_uri="https://app.example/callback",
        )
        consumed = OAuthSessionStore(
            None, second_vault, backend=second_backend
        ).consume_callback(started.state, "notion")
        assert consumed.session_id == started.session_id
    finally:
        second_backend.close()
        first_backend.close()


@pytest.mark.skipif(
    not os.environ.get("COGDOC_TEST_POSTGRES_DSN"),
    reason="COGDOC_TEST_POSTGRES_DSN is not configured",
)
def test_real_postgres_identity_access_plane_contract():
    dsn = os.environ["COGDOC_TEST_POSTGRES_DSN"]
    schema = f"cogdoc_identity_{uuid.uuid4().hex[:16]}"
    first_backend = PostgresBackend(dsn, schema=schema, max_size=4)
    second_backend = PostgresBackend(dsn, schema=schema, max_size=4)
    try:
        first_auth = AuthStore(None, backend=first_backend, scrypt_n=1 << 10)
        second_auth = AuthStore(None, backend=second_backend, scrypt_n=1 << 10)
        registered = first_auth.register(
            "alice@example.com", "correct horse battery staple", "Alice"
        )
        token = str(registered["access_token"])
        assert (
            second_auth.authenticate_session(token).principal.subject_id
            == (registered["user"]["user_id"])
        )
        assert second_auth.revoke_session(
            str(registered["user"]["user_id"]),
            str(registered["session"]["session_id"]),
        )
        with pytest.raises(AuthAuthenticationError):
            first_auth.authenticate_session(token)

        first_acl = ResourceAccessStore(None, backend=first_backend)
        second_acl = ResourceAccessStore(None, backend=second_backend)
        first_acl.set_kb_policy("tenant", "kb", "owner", "private")
        assert second_acl.acl_epoch("tenant", "kb") == 1

        first_flow = OIDCFlowStore(None, b"o" * 32, backend=first_backend)
        second_flow = OIDCFlowStore(None, b"o" * 32, backend=second_backend)
        flow = first_flow.create(intent="login", return_url="/login")
        assert second_flow.consume_state(flow.state).flow_id == flow.flow_id
        with pytest.raises(OIDCConfigurationError):
            OIDCFlowStore(None, b"x" * 32, backend=second_backend)
    finally:
        second_backend.close()
        first_backend.close()


@pytest.mark.skipif(
    not os.environ.get("COGDOC_TEST_POSTGRES_DSN"),
    reason="COGDOC_TEST_POSTGRES_DSN is not configured",
)
def test_real_postgres_research_dispatch_and_takeover_contract():
    dsn = os.environ["COGDOC_TEST_POSTGRES_DSN"]
    schema = f"cogdoc_research_{uuid.uuid4().hex[:16]}"
    first_backend = PostgresBackend(dsn, schema=schema, max_size=4)
    second_backend = PostgresBackend(dsn, schema=schema, max_size=4)
    try:
        first_jobs = SqliteResearchJobStore(None, backend=first_backend)
        second_jobs = SqliteResearchJobStore(None, backend=second_backend)
        first_dispatch = ResearchDispatchStore(first_backend)
        second_dispatch = ResearchDispatchStore(second_backend)
        created = first_jobs.create(
            kb_id="kb", objective="shared", section_titles=["section"]
        )
        started = first_jobs.start(created["job_id"])
        queued = first_dispatch.enqueue(
            created["job_id"], "evidence", str(started["execution_id"])
        )
        claimed = second_dispatch.claim("node-b", lease_seconds=30)
        assert claimed is not None
        assert claimed["dispatch_id"] == queued["dispatch_id"]
        activated = second_jobs.activate_distributed_attempt(
            created["job_id"],
            phase="evidence",
            attempt_id=str(started["execution_id"]),
        )
        assert activated["revision"] > started["revision"]
        first_jobs.clear_kb("kb")
        assert second_jobs.get(created["job_id"]) is None
        assert second_dispatch.get(str(queued["dispatch_id"])) is None
    finally:
        second_backend.close()
        first_backend.close()


@pytest.mark.skipif(
    not os.environ.get("COGDOC_TEST_POSTGRES_DSN"),
    reason="COGDOC_TEST_POSTGRES_DSN is not configured",
)
def test_real_postgres_skip_locked_claim_and_schema_bootstrap(tmp_path):
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

        registry_a = DistributedKnowledgeBaseRegistry(backend_a, tmp_path / "source-a")
        registry_b = DistributedKnowledgeBaseRegistry(backend_b, tmp_path / "source-b")
        kb = registry_a.create("docs", "tenant", "owner")
        storage_id = str(kb["storage_id"])
        mutation_a = DistributedMutationCoordinator(
            backend_a, registry_a, owner_id="api-a", lease_seconds=30
        )
        mutation_b = DistributedMutationCoordinator(
            backend_b, registry_b, owner_id="api-b", lease_seconds=30
        )
        lease = mutation_a.acquire(storage_id)
        with pytest.raises(MutationBusy):
            mutation_b.acquire(storage_id)
        source_dir = tmp_path / "source-a" / "payload"
        source_dir.mkdir(parents=True)
        (source_dir / "document.md").write_text("postgres authority")
        source_generations = SourceGenerationStore(
            backend_a, LocalObjectStore(tmp_path / "source-objects"), outbox=outbox
        )
        source_manifest = source_generations.stage_directory(
            tenant_id="tenant",
            storage_id=storage_id,
            source_dir=source_dir,
            lease=lease,
        )
        joint = authority.begin_build("tenant", storage_id, "joint", "api-a")
        joint = authority.prepare(
            joint["generation_id"], joint["lease_token"], manifest
        )
        authority.publish(
            joint["generation_id"],
            joint["lease_token"],
            lambda _candidate: None,
            on_publish=source_generations.publication_hook(
                source_manifest["generation_id"], lease
            ),
        )
        assert source_generations.current(storage_id) is not None
        assert authority.current("tenant", storage_id) is not None

        catalog_a = DistributedSourceCatalog(backend_a)
        catalog_b = DistributedSourceCatalog(backend_b)
        document = SourceDocument.create(
            connector_type="git",
            external_id="conn-a:document.md",
            display_name="document.md",
            content_sha256=hashlib.sha256(b"postgres authority").hexdigest(),
            byte_size=len(b"postgres authority"),
            metadata={"connection_id": "conn-a"},
        )
        catalog_a.upsert("tenant", storage_id, document)
        assert catalog_b.get("tenant", storage_id, document.source_id) is not None
        assert catalog_b.reconcile(
            "tenant", storage_id, [], connection_id="conn-a"
        ) == {"upserted": 0, "deleted": 1}

        quota_a = DistributedTenantQuotaManager(
            backend_a,
            source_generations,
            TenantQuotaPolicy(max_documents=2),
            owner_id="quota-a",
            lease_seconds=30,
        )
        quota_b = DistributedTenantQuotaManager(
            backend_b,
            SourceGenerationStore(
                backend_b, LocalObjectStore(tmp_path / "source-objects")
            ),
            TenantQuotaPolicy(max_documents=2),
            owner_id="quota-b",
            lease_seconds=30,
        )
        quota_token = quota_a.reserve_upload(
            "tenant", storage_id, str(source_dir), "second.md", 1
        )
        assert quota_b.snapshot("tenant")["reserved"]["documents"] == 1
        quota_a.release(quota_token)

        artifact_objects = LocalObjectStore(tmp_path / "artifact-objects")
        artifacts_a = DistributedSourceArtifactStore(
            backend_a, artifact_objects, owner_id="artifact-a"
        )
        artifacts_b = DistributedSourceArtifactStore(
            backend_b, artifact_objects, owner_id="artifact-b"
        )
        artifact_content = b"postgres authority"
        artifact_item = {
            "source_id": document.source_id,
            "version_id": document.version.version_id,
            "content_sha256": hashlib.sha256(artifact_content).hexdigest(),
            "byte_size": len(artifact_content),
            "media_type": "text/plain",
            "display_name": "document.md",
            "created_at": 1.0,
        }
        # SourceDocument above represents the same bytes, so its version is the
        # content-addressed identity consumed by the shared artifact store.
        assert artifact_item["content_sha256"] == document.version.content_sha256
        reservation = artifacts_a.reserve_batch(
            "tenant", storage_id, [artifact_item], reservation_key="pg-artifact"
        )
        artifacts_a.put(
            "tenant",
            storage_id,
            document.source_id,
            document.version.version_id,
            artifact_content,
            content_sha256=document.version.content_sha256,
            media_type="text/plain",
            display_name="document.md",
            created_at=1.0,
            reservation_token=reservation,
        )
        assert (
            artifacts_b.read(
                "tenant", storage_id, document.source_id, document.version.version_id
            )
            == artifact_content
        )
        artifacts_a.release_reservation(reservation)

        chat_a = DistributedSessionStore(backend_a)
        chat_b = DistributedSessionStore(backend_b)
        chat_a.record(
            f"{storage_id}~u-user",
            "session",
            [{"role": "user", "content": "shared"}],
            [
                {"role": "user", "content": "shared"},
                {
                    "role": "assistant",
                    "content": "answer",
                    "trace_id": "pg-trace",
                },
            ],
        )
        assert (
            chat_b.get_display(f"{storage_id}~u-user", "session")[-1]["trace_id"]
            == "pg-trace"
        )
        execution = chat_a.acquire_execution(
            f"{storage_id}~u-user", "session", "node-a", lease_seconds=30
        )
        with pytest.raises(SessionBusy):
            chat_b.acquire_execution(
                f"{storage_id}~u-user", "session", "node-b", lease_seconds=30
            )
        assert chat_a.release_execution(
            f"{storage_id}~u-user",
            "session",
            "node-a",
            str(execution["lease_token"]),
        )
    finally:
        # Test-only teardown uses a separately pooled autocommit connection;
        # production migrations never drop schemas.
        with backend_a._pool.connection() as connection:
            connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        backend_a.close()
        backend_b.close()
