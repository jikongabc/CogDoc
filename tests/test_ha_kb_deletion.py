from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from cogdoc.api.app import create_app
from cogdoc.api.ingest import IndexJobManager
from cogdoc.api.resource_access import ResourceAccessStore
from cogdoc.api.tenancy import Principal
from cogdoc.connectors.credential_store import CredentialVault
from cogdoc.connectors.oauth import OAuthSessionStore
from cogdoc.ha.session_store import StaleSessionLease
from cogdoc.ha.api_state import (
    DistributedIndexJobStore,
    DistributedKnowledgeBaseRegistry,
    DistributedMutationCoordinator,
    StaleMutationFence,
)
from cogdoc.ha.kb_deletion import (
    DELETE_COMPLETE,
    DELETE_FENCED,
    DistributedKBDeletionCoordinator,
)
from cogdoc.ha.runtime import HAConfig, HARuntime, manifest_for_directory
from cogdoc.source_model import SourceDocument, build_version_id


class _MultiwriterRuntime:
    """Expose SQLite's deterministic backend through the HA API test seam."""

    api_multi_writer_safe = True

    def __init__(self, runtime: HARuntime) -> None:
        self._runtime = runtime

    def __getattr__(self, name: str):
        return getattr(self._runtime, name)


def _runtime(tmp_path: Path) -> HARuntime:
    return HARuntime(
        HAConfig(
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
            worker_id="delete-test",
            scheduler_enabled=False,
            outbox_enabled=False,
            maintenance_enabled=False,
            index_worker_enabled=False,
        )
    )


def _state(tmp_path: Path):
    runtime = _runtime(tmp_path)
    registry = DistributedKnowledgeBaseRegistry(runtime.backend, tmp_path / "cache")
    mutations = DistributedMutationCoordinator(
        runtime.backend, registry, owner_id="writer", lease_seconds=30
    )
    runtime.api_mutation_coordinator = mutations
    deletion = runtime.bind_api_kb_deletion(registry)
    record = registry.create("docs", "tenant", "owner")
    return runtime, registry, mutations, deletion, record


def _publish_content(
    tmp_path: Path,
    runtime: HARuntime,
    mutations: DistributedMutationCoordinator,
    storage: str,
):
    source_dir = tmp_path / "source"
    source_dir.mkdir(exist_ok=True)
    (source_dir / "guide.md").write_bytes(b"published source")
    index_dir = tmp_path / "index"
    index_dir.mkdir(exist_ok=True)
    (index_dir / "portable.bin").write_bytes(b"published index")
    with mutations.lease(storage) as lease:
        source = runtime.source_generations.stage_directory(
            tenant_id="tenant",
            storage_id=storage,
            source_dir=source_dir,
            lease=lease,
        )
        index = runtime.index_generations.begin_build(
            "tenant", storage, "initial", "worker"
        )
        manifest = manifest_for_directory(
            index_dir,
            contract={
                "chunk_version": "v1",
                "embedding_model": "model",
                "dimensions": 3,
            },
        )
        index = runtime.index_generations.prepare(
            index["generation_id"], index["lease_token"], manifest
        )
        runtime.index_repository.materialize(index, index_dir)
        index = runtime.index_generations.publish(
            index["generation_id"],
            index["lease_token"],
            runtime.index_repository.verify,
            on_publish=runtime.source_generations.publication_hook(
                source["generation_id"], lease
            ),
        )
    document = SourceDocument.create(
        connector_type="url",
        external_id="connection:guide",
        display_name="guide.md",
        content_sha256=hashlib.sha256(b"published source").hexdigest(),
        byte_size=len(b"published source"),
    )
    runtime.source_catalog.upsert("tenant", storage, document)
    raw = b"raw source"
    digest = hashlib.sha256(raw).hexdigest()
    version = build_version_id(document.source_id, digest)
    runtime.source_artifact_store.put(
        "tenant",
        storage,
        document.source_id,
        version,
        raw,
        content_sha256=digest,
        media_type="text/plain",
        display_name="guide.txt",
        created_at=1,
    )
    return index, source, document, version


def test_distributed_delete_atomically_revokes_heads_and_preserves_old_generations(
    tmp_path: Path,
) -> None:
    runtime, registry, mutations, deletion, record = _state(tmp_path)
    storage = str(record["storage_id"])
    index, source, document, _version = _publish_content(
        tmp_path, runtime, mutations, storage
    )
    deletion.chat_sessions.record(
        f"{storage}~u-owner",
        "session",
        [{"role": "user", "content": "private question"}],
        [
            {"role": "user", "content": "private question"},
            {
                "role": "assistant",
                "content": "private answer",
                "trace_id": "old-chat",
            },
        ],
        storage_id=storage,
    )

    result = deletion.delete("tenant", storage)

    assert result["phase"] == DELETE_COMPLETE
    assert registry.get_by_storage_id(storage) is None
    assert runtime.index_generations.current("tenant", storage) is None
    assert runtime.source_generations.current(storage) is None
    assert runtime.index_generations.get(index["generation_id"]) is not None
    assert deletion.chat_sessions.list_sessions(f"{storage}~u-owner") == []
    marker = runtime.backend.sql(sqlite="?", postgres="%s")
    with runtime.backend.transaction() as connection:
        source_row = connection.execute(
            f"SELECT status FROM ha_source_generations WHERE generation_id={marker}",
            (source["generation_id"],),
        ).fetchone()
        event = connection.execute(
            f"SELECT aggregate_id FROM ha_outbox WHERE topic={marker}",
            ("kb.deleted",),
        ).fetchone()
    assert source_row is not None
    assert runtime.source_catalog.get("tenant", storage, document.source_id) is None
    assert runtime.source_artifact_store.usage("tenant", storage) == {
        "active_bytes": 0,
        "active_versions": 0,
        "trash_bytes": 0,
        "trash_versions": 0,
    }
    assert event is not None and event["aggregate_id"] == storage
    runtime.shutdown()


def test_delete_failure_is_fenced_and_retry_resumes_without_head_loss(
    tmp_path: Path, monkeypatch
) -> None:
    runtime, registry, mutations, deletion, record = _state(tmp_path)
    storage = str(record["storage_id"])
    index, _source, _document, _version = _publish_content(
        tmp_path, runtime, mutations, storage
    )
    original = runtime.source_catalog.delete_scope
    attempts = 0

    def fail_once(tenant_id, kb_id):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("injected catalog failure")
        return original(tenant_id, kb_id)

    monkeypatch.setattr(runtime.source_catalog, "delete_scope", fail_once)
    with pytest.raises(OSError, match="injected"):
        deletion.delete("tenant", storage)

    fenced = deletion.get(storage)
    assert fenced is not None and fenced["phase"] == DELETE_FENCED
    assert registry.status(storage) == "deleting"
    assert (
        runtime.index_generations.current("tenant", storage)["generation_id"]
        == index["generation_id"]
    )
    with pytest.raises(StaleMutationFence):
        runtime.index_generations.begin_build("tenant", storage, "late", "worker")

    assert deletion.delete("tenant", storage)["phase"] == DELETE_COMPLETE
    assert runtime.index_generations.current("tenant", storage) is None
    runtime.shutdown()


def test_recreate_same_slug_gets_new_epoch_and_empty_authority(tmp_path: Path) -> None:
    runtime, registry, mutations, deletion, old = _state(tmp_path)
    storage = str(old["storage_id"])
    _publish_content(tmp_path, runtime, mutations, storage)
    deletion.delete("tenant", storage)

    new = registry.create("docs", "tenant", "new-owner")
    assert new["storage_id"] == storage
    assert int(new["epoch"]) > int(old["epoch"])
    assert deletion.get(storage) is None
    assert runtime.index_generations.current("tenant", storage) is None
    assert runtime.source_generations.current(storage) is None
    assert runtime.source_catalog.list_sources("tenant", storage) == []
    assert (
        runtime.source_artifact_store.usage("tenant", storage)["active_versions"] == 0
    )

    assert deletion.delete("tenant", storage)["phase"] == DELETE_COMPLETE
    runtime.shutdown()


def test_delete_fence_revalidates_acl_epoch_inside_its_transaction(
    tmp_path: Path,
) -> None:
    runtime, registry, mutations, deletion, record = _state(tmp_path)
    storage = str(record["storage_id"])
    access = ResourceAccessStore(None, backend=runtime.backend)
    access.set_kb_policy("tenant", storage, "owner", "workspace")
    authority = {
        "tenant_id": "tenant",
        "storage_id": storage,
        "kb_epoch": registry.current(storage),
        "acl_epoch": access.acl_epoch("tenant", storage),
        "acl_required": True,
        "auth_kind": "api_principal",
        "subject_id": "owner",
        "role": "owner",
        "permission": "delete",
    }
    access.set_kb_policy("tenant", storage, "owner", "private")

    with pytest.raises(StaleSessionLease, match="authorization generation"):
        deletion.delete("tenant", storage, authority=authority)
    assert registry.status(storage) == "active"
    assert deletion.get(storage) is None
    runtime.shutdown()


def test_registry_create_and_artifact_scope_activation_are_one_transaction(
    tmp_path: Path,
) -> None:
    runtime, registry, mutations, deletion, record = _state(tmp_path)
    storage = str(record["storage_id"])
    marker = runtime.backend.sql(sqlite="?", postgres="%s")
    with runtime.backend.transaction(write=True) as connection:
        connection.execute(
            "UPDATE ha_api_knowledge_bases SET lifecycle='deleted' WHERE storage_id="
            f"{marker}",
            (storage,),
        )
        connection.execute(
            "INSERT INTO ha_api_kb_deletions(storage_id,tenant_id,kb_epoch,phase,"
            "artifact_versions,catalog_documents,started_at,updated_at) VALUES("
            f"{','.join(marker for _ in range(8))})",
            (storage, "tenant", int(record["epoch"]), DELETE_FENCED, 0, 0, 1, 1),
        )

    with pytest.raises(StaleMutationFence, match="incomplete"):
        registry.create("docs", "tenant", "new-owner")

    with runtime.backend.transaction() as connection:
        row = connection.execute(
            f"SELECT lifecycle,epoch,owner_id FROM ha_api_knowledge_bases WHERE storage_id={marker}",
            (storage,),
        ).fetchone()
    assert dict(row) == {
        "lifecycle": "deleted",
        "epoch": int(record["epoch"]),
        "owner_id": "owner",
    }
    assert registry.get_by_storage_id(storage) is None
    assert deletion.get(storage)["phase"] == DELETE_FENCED
    runtime.shutdown()


def test_prepared_index_cannot_publish_after_delete_fence(tmp_path: Path) -> None:
    runtime, _registry, _mutations, deletion, record = _state(tmp_path)
    storage = str(record["storage_id"])
    build = runtime.index_generations.begin_build(
        "tenant", storage, "prepared-before-delete", "worker"
    )
    index_dir = tmp_path / "late-index"
    index_dir.mkdir()
    (index_dir / "portable.bin").write_bytes(b"late")
    manifest = manifest_for_directory(
        index_dir,
        contract={"chunk_version": "v1", "embedding_model": "m", "dimensions": 1},
    )
    prepared = runtime.index_generations.prepare(
        build["generation_id"], build["lease_token"], manifest
    )
    runtime.index_repository.materialize(prepared, index_dir)

    deletion.delete("tenant", storage)

    with pytest.raises(StaleMutationFence, match="not active"):
        runtime.index_generations.publish(
            build["generation_id"],
            build["lease_token"],
            runtime.index_repository.verify,
        )
    assert runtime.index_generations.current("tenant", storage) is None
    runtime.shutdown()


def test_concurrent_delete_requests_converge_on_one_tombstone(tmp_path: Path) -> None:
    runtime, registry, _mutations, deletion, record = _state(tmp_path)
    storage = str(record["storage_id"])
    second = DistributedKBDeletionCoordinator(
        runtime.backend,
        registry,
        runtime.index_generations,
        runtime.source_generations,
        runtime.source_catalog,
        runtime.source_artifact_store,
        runtime.outbox,
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(lambda owner: owner.delete("tenant", storage), (deletion, second))
        )

    assert {result["phase"] for result in results} == {DELETE_COMPLETE}
    with runtime.backend.transaction() as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM ha_outbox WHERE topic='kb.deleted' AND aggregate_id=?",
            (storage,),
        ).fetchone()[0]
    assert count == 1
    runtime.shutdown()


def test_recover_finishes_persisted_fenced_delete(tmp_path: Path, monkeypatch) -> None:
    runtime, registry, mutations, deletion, record = _state(tmp_path)
    storage = str(record["storage_id"])
    _publish_content(tmp_path, runtime, mutations, storage)
    original = runtime.source_catalog.delete_scope
    monkeypatch.setattr(
        runtime.source_catalog,
        "delete_scope",
        lambda *_args: (_ for _ in ()).throw(OSError("crash")),
    )
    with pytest.raises(OSError):
        deletion.delete("tenant", storage)
    monkeypatch.setattr(runtime.source_catalog, "delete_scope", original)

    replacement = DistributedKBDeletionCoordinator(
        runtime.backend,
        registry,
        runtime.index_generations,
        runtime.source_generations,
        runtime.source_catalog,
        runtime.source_artifact_store,
        runtime.outbox,
    )
    assert replacement.recover() == {"recovered": 1, "failed": 0}
    recovered = replacement.get(storage)
    assert recovered is not None
    assert recovered["phase"] == DELETE_COMPLETE
    runtime.shutdown()


def test_control_plane_cleanup_is_replayed_before_authority_heads_are_removed(
    tmp_path: Path,
) -> None:
    runtime, registry, mutations, deletion, record = _state(tmp_path)
    storage = str(record["storage_id"])
    index, _source, _document, _version = _publish_content(
        tmp_path, runtime, mutations, storage
    )
    calls: list[tuple[str, str]] = []
    fail_once = [True]

    def cleanup(tenant_id: str, kb_id: str) -> None:
        calls.append((tenant_id, kb_id))
        if fail_once[0]:
            fail_once[0] = False
            raise OSError("injected control-plane crash")

    deletion.bind_control_plane_cleanup(cleanup)
    with pytest.raises(OSError, match="control-plane crash"):
        deletion.delete("tenant", storage)

    assert deletion.get(storage)["phase"] == DELETE_FENCED
    assert registry.status(storage) == "deleting"
    assert (
        runtime.index_generations.current("tenant", storage)["generation_id"]
        == index["generation_id"]
    )

    assert deletion.recover() == {"recovered": 1, "failed": 0}
    assert calls == [("tenant", storage), ("tenant", storage)]
    assert deletion.get(storage)["phase"] == DELETE_COMPLETE
    assert runtime.index_generations.current("tenant", storage) is None
    runtime.shutdown()


def test_all_in_process_cleanup_participants_must_converge(tmp_path: Path) -> None:
    runtime, _registry, _mutations, deletion, record = _state(tmp_path)
    storage = str(record["storage_id"])
    calls: list[str] = []
    deletion.bind_control_plane_cleanup(lambda *_args: calls.append("node-a"))
    deletion.bind_control_plane_cleanup(lambda *_args: calls.append("node-b"))

    assert deletion.delete("tenant", storage)["phase"] == DELETE_COMPLETE
    assert calls == ["node-a", "node-b"]
    runtime.shutdown()


def test_recovery_drains_more_than_one_batch(tmp_path: Path) -> None:
    runtime, registry, _mutations, deletion, first = _state(tmp_path)
    records = [
        first,
        registry.create("docs-two", "tenant", "owner"),
        registry.create("docs-three", "tenant", "owner"),
    ]
    for record in records:
        deletion._begin("tenant", str(record["storage_id"]))

    assert deletion.recover(limit=2) == {"recovered": 3, "failed": 0}
    assert all(
        deletion.get(str(record["storage_id"]))["phase"] == DELETE_COMPLETE
        for record in records
    )
    runtime.shutdown()


@pytest.mark.anyio
async def test_ha_http_delete_purges_connector_chat_and_acl_capabilities(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runtime, registry, mutations, deletion, record = _state(tmp_path)
    import cogdoc.service.kb_epoch as kb_epoch_module
    import cogdoc.service.kb_lifecycle as kb_lifecycle_module

    monkeypatch.setattr(kb_epoch_module, "_shared", registry)
    monkeypatch.setattr(kb_lifecycle_module, "_shared", registry)
    storage = str(record["storage_id"])
    access = ResourceAccessStore(None, backend=runtime.backend)
    access.set_kb_policy("tenant", storage, "owner", "private")
    vault = CredentialVault(
        None,
        backend=runtime.backend,
        master_keys={"v1": b"k" * 32},
        active_key_version="v1",
    )
    jobs = IndexJobManager(
        ingest_fn=lambda *_args, **_kwargs: type(
            "Result",
            (),
            {"document_count": 0, "chunk_count": 0, "ocr_summary": {}},
        )(),
        source_dir_for=registry.source_dir,
        job_store=DistributedIndexJobStore(
            runtime.backend, owner_id="api-delete", lease_seconds=30
        ),
        epoch_reader=registry.current,
        lifecycle_reader=registry.status,
        mutation_coordinator=mutations,
        source_generation_store=runtime.source_generations,
    )
    app = create_app(
        kb_registry=registry,
        index_jobs=jobs,
        ha_runtime=_MultiwriterRuntime(runtime),
        resource_access_store=access,
        connector_credential_vault=vault,
        api_principals={
            "owner-key": Principal.for_api_key(
                "owner-key",
                tenant_id="tenant",
                subject_id="owner",
                role="owner",
            )
        },
        close_state_runtime_on_shutdown=False,
    )
    oauth_sessions = OAuthSessionStore(
        None,
        vault,
        backend=runtime.backend,
        epoch_reader=registry.current,
    )
    app.state.connector_oauth_session_store = oauth_sessions
    connection = app.state.connection_store.create(
        tenant_id="tenant",
        kb_id=storage,
        connector_type="url",
        name="old capability",
        config={"urls": ["https://example.com/document"]},
        secret_env={},
        owner_id="owner",
    )
    credential = vault.create(
        tenant_id="tenant",
        kb_id=storage,
        connection_id=None,
        provider="notion",
        credential_kind="manual",
        label="old secret",
        secret_values={"token": "old-private-token"},
        actor_id="owner",
    )
    research = app.state.research_job_store.create(
        kb_id=storage,
        objective="old private research",
    )
    dispatch = app.state.ha_research_dispatch_store.enqueue(
        research["job_id"], "evidence", "attempt-old"
    )
    oauth_session = oauth_sessions.create(
        provider="notion",
        tenant_id="tenant",
        kb_id=storage,
        connection_id=None,
        user_id="owner",
        redirect_uri="https://cogdoc.example/oauth/callback",
    )
    app.state.session_store.record(
        storage,
        "old-session",
        [{"role": "user", "content": "old"}],
        [
            {"role": "user", "content": "old"},
            {
                "role": "assistant",
                "content": "private",
                "trace_id": "old-turn",
            },
        ],
        storage_id=storage,
    )
    catalog_delete = runtime.source_catalog.delete_scope
    catalog_attempts = [0]

    def fail_catalog_once(tenant_id: str, kb_id: str):
        catalog_attempts[0] += 1
        if catalog_attempts[0] == 1:
            raise OSError("injected post-ACL cleanup crash")
        return catalog_delete(tenant_id, kb_id)

    monkeypatch.setattr(runtime.source_catalog, "delete_scope", fail_catalog_once)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        first = await client.delete(
            "/v1/knowledge-bases/docs",
            headers={"X-API-Key": "owner-key"},
        )
        assert first.status_code == 500, first.text
        assert access.get_kb_policy("tenant", storage) is None
        response = await client.delete(
            "/v1/knowledge-bases/docs",
            headers={"X-API-Key": "owner-key"},
        )

    assert response.status_code == 204, response.text
    assert deletion.get(storage)["phase"] == DELETE_COMPLETE
    assert app.state.connection_store.get(connection["connection_id"]) is None
    assert app.state.connector_sync_store.list_jobs("tenant", storage) == []
    assert app.state.session_store.list_sessions(storage) == []
    assert access.get_kb_policy("tenant", storage) is None
    assert (
        vault.get_metadata(
            credential["credential_id"], tenant_id="tenant", kb_id=storage
        )
        is None
    )
    assert app.state.research_job_store.get(research["job_id"]) is None
    assert app.state.ha_research_dispatch_store.get(dispatch["dispatch_id"]) is None
    assert (
        oauth_sessions.cancel(
            oauth_session.session_id,
            tenant_id="tenant",
            kb_id=storage,
            connection_id=None,
            user_id="owner",
        )
        is False
    )

    recreated = registry.create("docs", "tenant", "new-owner")
    assert recreated["storage_id"] == storage
    assert app.state.connection_store.list_entries("tenant", storage) == []
    assert app.state.session_store.list_sessions(storage) == []

    app.state.sync_manager.shutdown(wait=True)
    jobs.shutdown(wait=True)
    app.state.connector_cleanup_executor.shutdown(wait=True)
    app.state.source_artifact_executor.shutdown(wait=True)
    app.state.chat_stream_executor.shutdown(wait=False, cancel_futures=True)
    app.state.offload_executor.shutdown(wait=True)
    runtime.shutdown()
