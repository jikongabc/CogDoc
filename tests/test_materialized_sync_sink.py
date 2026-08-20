import hashlib
import json
from pathlib import Path
import shutil
from threading import Event, Thread

import pytest

from cogdoc.api.resource_access import ResourceAccessStore
from cogdoc.api.tenant_quota import TenantQuotaManager, TenantQuotaPolicy
from cogdoc.connectors.base import RetryableConnectorError
from cogdoc.connectors.materialized_sink import MaterializedSyncSink
from cogdoc.service.external_acl import ExternalAclSynchronizer, ExternalAclSyncStore
from cogdoc.service.kb_locks import kb_write_lock
from cogdoc.service.source_artifact_store import SourceArtifactStore
from cogdoc.service.source_catalog import SourceCatalog
from cogdoc.service.source_model import SourceDocument
from cogdoc.tools.chunker import chunk_paper
from cogdoc.tools.chunk_identity import build_document_id
from cogdoc.tools.source_parser import parse_source


def _document(
    external_id,
    name,
    content,
    *,
    media_type=None,
    fetched_at=None,
):
    return SourceDocument.create(
        connector_type="local-directory",
        external_id=external_id,
        display_name=name,
        content_sha256=hashlib.sha256(content).hexdigest(),
        byte_size=len(content),
        media_type=media_type,
        fetched_at=fetched_at,
    )


def _sink(
    tmp_path,
    catalog,
    submitted,
    *,
    job="job-1",
    connection="conn-1",
    submitter=None,
    index_status_reader=None,
    artifact_store=None,
    artifact_versions_to_keep=10,
    workspace_visible=False,
    acl_sync=None,
    quota_reserver=None,
    quota_releaser=None,
    recovering_commit=False,
    connector_type="local-directory",
    index_timeout_seconds=30.0,
):
    sink = MaterializedSyncSink(
        source_dir=str(tmp_path / "sources"),
        catalog=catalog,
        index_submitter=submitter or (lambda kb_id: submitted.append(kb_id) or {}),
        index_status_reader=index_status_reader,
        owner_id="owner",
        workspace_visible=workspace_visible,
        acl_sync=acl_sync,
        artifact_store=artifact_store,
        artifact_versions_to_keep=artifact_versions_to_keep,
        quota_reserver=quota_reserver,
        quota_releaser=quota_releaser,
        index_timeout_seconds=index_timeout_seconds,
    )
    sink.begin(
        job_id=job,
        tenant_id="tenant",
        kb_id="storage-kb",
        connection_id=connection,
        connector_type=connector_type,
        attempt=1,
        recovering_commit=recovering_commit,
    )
    return sink


def test_materialized_sink_batches_manifest_and_quota_handoff(tmp_path, monkeypatch):
    catalog = SourceCatalog(str(tmp_path / "state.db"))
    reservations = []
    releases = []

    def reserve(*args):
        reservations.append(args)
        return "quota-token"

    sink = _sink(
        tmp_path,
        catalog,
        [],
        quota_reserver=reserve,
        quota_releaser=releases.append,
    )
    manifest_writes = 0
    original_write = sink._write_manifest

    def counted_write(directory):
        nonlocal manifest_writes
        manifest_writes += 1
        original_write(directory)

    monkeypatch.setattr(sink, "_write_manifest", counted_write)
    seen = set()
    for index in range(250):
        external_id = f"doc-{index}"
        content = f"body-{index}".encode()
        sink.upsert(
            _document(external_id, f"document-{index}.md", content),
            content,
        )
        seen.add(external_id)
    assert manifest_writes == 0

    sink.prepare_commit(snapshot=True, seen_external_ids=frozenset(seen))
    assert manifest_writes == 1
    assert len(reservations) == 1
    _, _, source_dir, baseline, proposed, job_id = reservations[0]
    assert source_dir == str(tmp_path / "sources")
    assert baseline.endswith("/.connections/conn-1")
    assert proposed.endswith("conn-1-job-1.staging")
    assert job_id == "job-1"

    # Runtime calls prepare once before its DB authority transition and commit
    # calls it defensively again. The reservation remains exactly-once.
    sink.commit(
        snapshot=True,
        seen_external_ids=frozenset(seen),
        heartbeat=lambda: None,
    )
    assert len(reservations) == 1
    sink.finalize()
    assert releases == ["quota-token"]
    catalog.close()


def test_materialized_sink_delete_removes_the_staged_file(tmp_path):
    catalog = SourceCatalog(str(tmp_path / "state.db"))
    sink = _sink(tmp_path, catalog, [])
    content = b"staged content to delete"
    sink.upsert(_document("obsolete", "obsolete.md", content), content)
    filename = sink._rows["obsolete"]["filename"]
    staged = sink.staging / filename
    assert staged.is_file()

    sink.delete("obsolete")

    assert "obsolete" not in sink._rows
    assert not staged.exists()
    sink.abort()
    catalog.close()


def test_prepare_commit_persists_staging_parent_before_authority_handoff(
    tmp_path, monkeypatch
):
    catalog = SourceCatalog(str(tmp_path / "state.db"))
    sink = _sink(tmp_path, catalog, [])
    content = b"durable connector content"
    sink.upsert(_document("source", "guide.md", content), content)
    barriers = []
    original = sink._fsync_directory

    def record(directory):
        barriers.append(directory)
        original(directory)

    monkeypatch.setattr(sink, "_fsync_directory", record)
    sink.prepare_commit(
        snapshot=True,
        seen_external_ids=frozenset({"source"}),
    )

    assert sink.staging is not None
    assert sink.staging.parent in barriers
    sink.abort()
    catalog.close()


def test_materialized_sink_never_overwrites_unowned_top_level_source(tmp_path):
    catalog = SourceCatalog(str(tmp_path / "state.db"))
    sink = _sink(tmp_path, catalog, [])
    content = b"connector-content"
    sink.upsert(_document("report", "report.md", content), content)
    reserved_name = next(iter(sink._rows.values()))["filename"]
    existing = tmp_path / "sources" / reserved_name
    existing.write_bytes(b"user-content")

    with pytest.raises(ValueError, match="conflicts with an existing source"):
        sink.prepare_commit(
            snapshot=True,
            seen_external_ids=frozenset({"report"}),
        )

    assert existing.read_bytes() == b"user-content"
    sink.abort()
    catalog.close()


def test_recovery_replays_file_published_before_contract_sidecar(tmp_path, monkeypatch):
    catalog = SourceCatalog(str(tmp_path / "state.db"))
    content = b"content durable before its ownership sidecar"
    interrupted = _sink(tmp_path, catalog, [], job="job-publish-crash")
    interrupted.upsert(_document("guide", "guide.md", content), content)
    reserved_name = next(iter(interrupted._rows.values()))["filename"]
    target = tmp_path / "sources" / reserved_name
    original_replace = __import__("os").replace
    failed = False

    def fail_after_target_replace(source, destination):
        nonlocal failed
        original_replace(source, destination)
        if not failed and target == destination:
            failed = True
            raise OSError("power loss after publishing target")

    monkeypatch.setattr(
        "cogdoc.connectors.materialized_sink.os.replace", fail_after_target_replace
    )
    with pytest.raises(RetryableConnectorError):
        interrupted.commit(
            snapshot=True,
            seen_external_ids=frozenset({"guide"}),
            heartbeat=lambda: None,
        )
    assert target.read_bytes() == content
    assert not (tmp_path / "sources" / ".cogdoc-source-contracts.json").exists()

    monkeypatch.setattr(
        "cogdoc.connectors.materialized_sink.os.replace", original_replace
    )
    recovered = _sink(
        tmp_path,
        catalog,
        [],
        job="job-publish-crash",
        recovering_commit=True,
    )
    recovered.recover_commit(heartbeat=lambda: None)
    recovered.finalize()

    contracts = json.loads(
        (tmp_path / "sources" / ".cogdoc-source-contracts.json").read_text()
    )["documents"]
    assert contracts[reserved_name]["metadata"]["connection_id"] == "conn-1"
    assert target.read_bytes() == content
    catalog.close()


def test_commit_recovery_uses_backup_for_quota_after_first_swap_rename(tmp_path):
    catalog = SourceCatalog(str(tmp_path / "state.db"))
    initial = _sink(tmp_path, catalog, [], job="job-initial")
    initial.upsert(_document("old", "old.md", b"old"), b"old")
    initial.commit(
        snapshot=True,
        seen_external_ids=frozenset({"old"}),
        heartbeat=lambda: None,
    )
    initial.finalize()

    interrupted = _sink(tmp_path, catalog, [], job="job-interrupted")
    interrupted.upsert(_document("new", "new.md", b"new"), b"new")
    interrupted.prepare_commit(
        snapshot=True, seen_external_ids=frozenset({"old", "new"})
    )
    interrupted._write_journal("prepared")
    assert interrupted.current is not None and interrupted.backup is not None
    interrupted.current.replace(interrupted.backup)

    class _Registry:
        def list(self, tenant_id=None):
            rows = [{"tenant_id": "tenant", "storage_id": "storage-kb", "kb_id": "kb"}]
            return rows if tenant_id in (None, "tenant") else []

        def source_dir(self, _storage_id):
            return str(tmp_path / "sources")

    quota = TenantQuotaManager(
        _Registry(), TenantQuotaPolicy(max_documents=2, max_storage_bytes=6)
    )
    recovered = _sink(
        tmp_path,
        catalog,
        [],
        job="job-interrupted",
        quota_reserver=quota.reserve_connector_snapshot,
        quota_releaser=quota.release,
        recovering_commit=True,
    )
    recovered.recover_commit(heartbeat=lambda: None)
    assert quota.snapshot("tenant")["reserved"]["documents"] == 1
    recovered.finalize()
    assert quota.snapshot("tenant")["reserved"]["documents"] == 0
    assert len(list((tmp_path / "sources").glob("*.md"))) == 2
    catalog.close()


class _CapturingAclSynchronizer:
    def __init__(self):
        self.calls = []

    def apply(self, **kwargs):
        self.calls.append(kwargs)
        return {}


class _StaticIdentityResolver:
    def resolve(self, tenant_id, grant):
        del tenant_id
        if grant.external_subject == "alice@example.com":
            return "alice", None
        return None


@pytest.mark.parametrize(
    ("connection_visible", "provider_visible", "complete", "expected_visible"),
    [
        pytest.param(False, True, True, False, id="connection-private"),
        pytest.param(True, True, True, True, id="both-visible"),
        pytest.param(True, False, True, False, id="provider-private"),
        pytest.param(True, True, False, False, id="provider-incomplete"),
    ],
)
def test_materialized_sink_requires_both_provider_and_connection_workspace_visibility(
    tmp_path,
    connection_visible,
    provider_visible,
    complete,
    expected_visible,
):
    catalog = SourceCatalog(str(tmp_path / "state.db"))
    acl_sync = _CapturingAclSynchronizer()
    content = b"provider document"
    sink = _sink(
        tmp_path,
        catalog,
        [],
        workspace_visible=connection_visible,
        acl_sync=acl_sync,
    )
    sink.upsert(
        _document("provider-doc", "provider.md", content),
        content,
        acl={
            "complete": complete,
            "workspace_visible": provider_visible,
            "provider_version": "acl-v1",
            "grants": [
                {
                    "external_subject": "alice@example.com",
                    "subject_type": "user",
                    "permission": "read",
                }
            ],
        },
    )

    sink.commit(
        snapshot=True,
        seen_external_ids=frozenset({"provider-doc"}),
        heartbeat=lambda: None,
    )

    applied = acl_sync.calls[0]["snapshot"]
    assert applied.workspace_visible is expected_visible
    assert applied.complete is complete
    assert applied.provider_version == "acl-v1"
    assert [grant.external_subject for grant in applied.grants] == ["alice@example.com"]
    sink.finalize()
    catalog.close()


def test_materialized_sink_acl_mapping_error_emits_empty_quarantine_snapshot(
    tmp_path,
):
    catalog = SourceCatalog(str(tmp_path / "state.db"))
    acl_sync = _CapturingAclSynchronizer()
    content = b"content remains materializable"
    sink = _sink(
        tmp_path,
        catalog,
        [],
        workspace_visible=True,
        acl_sync=acl_sync,
    )
    duplicate = {
        "complete": True,
        "workspace_visible": True,
        "provider_version": "untrusted-version",
        "grants": [
            {
                "external_subject": "alice@example.com",
                "subject_type": "user",
                "permission": "write",
            },
            {
                "external_subject": " Alice@Example.COM ",
                "subject_type": "user",
                "permission": "read",
            },
        ],
    }
    sink.upsert(
        _document("provider-doc", "provider.md", content),
        content,
        acl=duplicate,
    )

    sink.commit(
        snapshot=True,
        seen_external_ids=frozenset({"provider-doc"}),
        heartbeat=lambda: None,
    )

    snapshot = acl_sync.calls[0]["snapshot"]
    assert snapshot.complete is False
    assert snapshot.workspace_visible is False
    assert snapshot.grants == ()
    assert snapshot.provider_version is None
    materialized_name = catalog.list_sources("tenant", "storage-kb")[0]["display_name"]
    assert (tmp_path / "sources" / materialized_name).read_bytes() == content
    sink.finalize()
    catalog.close()


@pytest.mark.parametrize(
    ("connector_type", "expected_complete", "expected_visible"),
    [
        pytest.param("local-directory", True, True, id="explicit-connection-policy"),
        pytest.param("confluence", False, False, id="confluence-acl-missing"),
        pytest.param("sharepoint", False, False, id="sharepoint-acl-missing"),
    ],
)
def test_missing_acl_is_private_only_for_acl_capable_connectors(
    tmp_path, connector_type, expected_complete, expected_visible
):
    catalog = SourceCatalog(str(tmp_path / "state.db"))
    acl_sync = _CapturingAclSynchronizer()
    content = b"content with a missing ACL mapping"
    sink = _sink(
        tmp_path,
        catalog,
        [],
        workspace_visible=True,
        acl_sync=acl_sync,
        connector_type=connector_type,
    )
    sink.upsert(_document("provider-doc", "provider.md", content), content)

    sink.commit(
        snapshot=True,
        seen_external_ids=frozenset({"provider-doc"}),
        heartbeat=lambda: None,
    )

    snapshot = acl_sync.calls[0]["snapshot"]
    assert snapshot.complete is expected_complete
    assert snapshot.workspace_visible is expected_visible
    assert snapshot.grants == ()
    sink.finalize()
    catalog.close()


@pytest.mark.parametrize(
    "invalid_acl",
    [
        pytest.param(
            {
                "complete": True,
                "workspace_visible": True,
                "provider_version": "acl-v2",
                "grants": [
                    {
                        "external_subject": "Alice@Example.COM",
                        "subject_type": "user",
                        "permission": "write",
                    },
                    {
                        "external_subject": " alice@example.com ",
                        "subject_type": "user",
                        "permission": "read",
                    },
                ],
            },
            id="normalized-duplicate",
        ),
        pytest.param(
            {
                "complete": "yes",
                "workspace_visible": True,
                "provider_version": "acl-v2",
                "grants": [],
            },
            id="malformed-flags",
        ),
        pytest.param(None, id="missing-provider-acl"),
    ],
)
def test_invalid_or_missing_acl_revokes_old_grant_without_blocking_content_commit(
    tmp_path, invalid_acl
):
    catalog = SourceCatalog(str(tmp_path / "catalog.db"))
    access_store = ResourceAccessStore(tmp_path / "access.db")
    access_store.set_kb_policy("tenant", "storage-kb", "owner", "private")
    acl_state = ExternalAclSyncStore(str(tmp_path / "access.db"))
    acl_sync = ExternalAclSynchronizer(
        access_store, _StaticIdentityResolver(), acl_state
    )
    submitted = []
    first_content = b"content with a valid provider ACL"
    first = _sink(
        tmp_path,
        catalog,
        submitted,
        job="job-valid-acl",
        workspace_visible=True,
        acl_sync=acl_sync,
        connector_type="confluence",
    )
    first.upsert(
        _document("provider-doc", "provider.md", first_content),
        first_content,
        acl={
            "complete": True,
            "workspace_visible": True,
            "provider_version": "acl-v1",
            "grants": [
                {
                    "external_subject": "alice@example.com",
                    "subject_type": "user",
                    "permission": "read",
                }
            ],
        },
    )
    first.commit(
        snapshot=True,
        seen_external_ids=frozenset({"provider-doc"}),
        heartbeat=lambda: None,
    )
    first.finalize()

    materialized_name = catalog.list_sources("tenant", "storage-kb")[0]["display_name"]
    document_id = build_document_id(materialized_name)
    initial_policy = access_store.get_document_policy(
        "tenant", "storage-kb", document_id
    )
    assert initial_policy is not None and initial_policy["policy"] == "workspace"
    assert [
        grant["subject_id"]
        for grant in access_store.list_grants(
            "tenant", "storage-kb", document_id=document_id
        )
    ] == ["alice"]

    replacement_content = b"new content survives an invalid provider ACL"
    replacement = _sink(
        tmp_path,
        catalog,
        submitted,
        job="job-invalid-acl",
        workspace_visible=True,
        acl_sync=acl_sync,
        connector_type="confluence",
    )
    replacement.upsert(
        _document("provider-doc", "provider.md", replacement_content),
        replacement_content,
        acl=invalid_acl,
    )
    replacement.commit(
        snapshot=True,
        seen_external_ids=frozenset({"provider-doc"}),
        heartbeat=lambda: None,
    )
    replacement.finalize()

    policy = access_store.get_document_policy("tenant", "storage-kb", document_id)
    assert policy is not None and policy["policy"] == "private"
    assert (
        access_store.list_grants("tenant", "storage-kb", document_id=document_id) == []
    )
    checkpoint = acl_state.get("tenant", "storage-kb", document_id, "connector:conn-1")
    assert checkpoint is not None and checkpoint["status"] == "quarantined"
    assert (
        tmp_path / "sources" / materialized_name
    ).read_bytes() == replacement_content
    assert (
        tmp_path / "sources" / ".connections" / "conn-1" / materialized_name
    ).read_bytes() == replacement_content
    assert submitted == ["storage-kb", "storage-kb"]
    acl_state.close()
    access_store.close()
    catalog.close()


def test_stale_acl_retirement_survives_failed_index_and_finishes_on_recovery(
    tmp_path,
):
    catalog = SourceCatalog(str(tmp_path / "catalog.db"))
    access_store = ResourceAccessStore(tmp_path / "access.db")
    access_store.set_kb_policy("tenant", "storage-kb", "owner", "private")
    acl_state = ExternalAclSyncStore(str(tmp_path / "access.db"))
    acl_sync = ExternalAclSynchronizer(
        access_store, _StaticIdentityResolver(), acl_state
    )
    content = b"content that becomes stale"
    initial = _sink(
        tmp_path,
        catalog,
        [],
        job="job-stale-initial",
        acl_sync=acl_sync,
        connector_type="confluence",
    )
    initial.upsert(
        _document("stale", "stale.md", content),
        content,
        acl={
            "complete": True,
            "workspace_visible": False,
            "grants": [
                {
                    "external_subject": "alice@example.com",
                    "subject_type": "user",
                    "permission": "read",
                }
            ],
        },
    )
    initial.commit(
        snapshot=True,
        seen_external_ids=frozenset({"stale"}),
        heartbeat=lambda: None,
    )
    initial.finalize()

    catalog_row = catalog.list_sources("tenant", "storage-kb")[0]
    document_id = build_document_id(catalog_row["display_name"])
    managed_by = "connector:conn-1"
    assert access_store.list_grants("tenant", "storage-kb", document_id=document_id)
    assert acl_state.get("tenant", "storage-kb", document_id, managed_by) is not None

    failed = _sink(
        tmp_path,
        catalog,
        [],
        job="job-stale-delete",
        submitter=lambda _kb_id: {"job_id": "failed-index"},
        index_status_reader=lambda _job_id: {"status": "failed"},
        acl_sync=acl_sync,
        connector_type="confluence",
    )
    with pytest.raises(RetryableConnectorError):
        failed.commit(
            snapshot=True,
            seen_external_ids=frozenset(),
            heartbeat=lambda: None,
        )

    policy = access_store.get_document_policy("tenant", "storage-kb", document_id)
    assert policy is not None and policy["policy"] == "private"
    assert (
        access_store.list_grants("tenant", "storage-kb", document_id=document_id) == []
    )
    assert access_store.retiring_document_ids("tenant", "storage-kb", managed_by) == (
        document_id,
    )
    assert acl_state.get("tenant", "storage-kb", document_id, managed_by) is not None
    assert catalog.list_sources("tenant", "storage-kb") == []

    recovered = _sink(
        tmp_path,
        catalog,
        [],
        job="job-stale-delete",
        submitter=lambda _kb_id: {"job_id": "recovered-index"},
        index_status_reader=lambda _job_id: {"status": "succeeded"},
        acl_sync=acl_sync,
        connector_type="confluence",
        recovering_commit=True,
    )
    recovered.recover_commit(heartbeat=lambda: None)

    assert access_store.retiring_document_ids("tenant", "storage-kb", managed_by) == ()
    assert access_store.get_document_policy("tenant", "storage-kb", document_id) is None
    assert acl_state.get("tenant", "storage-kb", document_id, managed_by) is None
    recovered.finalize()
    acl_state.close()
    access_store.close()
    catalog.close()


def test_materialized_sink_atomically_replaces_snapshot_and_catalog(tmp_path):
    catalog = SourceCatalog(str(tmp_path / "state.db"))
    submitted = []
    first_content = (
        b"# Guide\n\nThis is enough meaningful content for a stable connector chunk."
    )
    first = _sink(tmp_path, catalog, submitted, job="job-1")
    first.upsert(_document("a", "guide.md", first_content), first_content)
    first.commit(
        snapshot=True, seen_external_ids=frozenset({"a"}), heartbeat=lambda: None
    )
    first.finalize()

    rows = catalog.list_sources("tenant", "storage-kb")
    assert len(rows) == 1 and rows[0]["metadata"]["provider_external_id"] == "a"
    visible = list((tmp_path / "sources" / ".connections" / "conn-1").glob("*.md"))
    assert len(visible) == 1 and visible[0].read_bytes() == first_content
    root_visible = list((tmp_path / "sources").glob("*.md"))
    contracts = json.loads(
        (tmp_path / "sources" / ".cogdoc-source-contracts.json").read_text()
    )["documents"]
    contract = SourceDocument.from_manifest_document(contracts[root_visible[0].name])
    chunks = chunk_paper(
        parse_source(str(root_visible[0]), source_document=contract),
        contract.version.content_sha256,
    )
    assert chunks[0]["meta"]["source_id"] == contract.source_id
    assert chunks[0]["meta"]["source_version_id"] == contract.version.version_id

    second = _sink(tmp_path, catalog, submitted, job="job-2")
    second.upsert(_document("b", "other.md", b"second"), b"second")
    second.commit(
        snapshot=True, seen_external_ids=frozenset({"b"}), heartbeat=lambda: None
    )
    second.finalize()

    current = catalog.list_sources("tenant", "storage-kb")
    deleted = catalog.list_sources("tenant", "storage-kb", include_deleted=True)
    assert [row["metadata"]["provider_external_id"] for row in current] == ["b"]
    assert len(deleted) == 2
    assert submitted == ["storage-kb", "storage-kb"]
    catalog.close()


def test_materialized_sink_recovers_swapped_commit_journal(tmp_path):
    catalog = SourceCatalog(str(tmp_path / "state.db"))

    def fail_index(_kb_id):
        raise RuntimeError("queue unavailable")

    failed = _sink(tmp_path, catalog, [], job="job-recover", submitter=fail_index)
    failed.upsert(_document("a", "guide.md", b"body"), b"body")
    with pytest.raises(RetryableConnectorError):
        failed.commit(
            snapshot=True,
            seen_external_ids=frozenset({"a"}),
            heartbeat=lambda: None,
        )
    failed.abort()

    submitted = []
    recovered = _sink(tmp_path, catalog, submitted, job="job-recover")
    recovered.recover_commit(heartbeat=lambda: None)
    recovered.finalize()
    assert submitted == ["storage-kb"]
    assert len(catalog.list_sources("tenant", "storage-kb")) == 1
    catalog.close()


@pytest.mark.parametrize("stuck_status", ["pending", "running"])
def test_materialized_sink_index_timeout_persists_and_reuses_job_after_restart(
    tmp_path,
    stuck_status,
):
    catalog = SourceCatalog(str(tmp_path / "state.db"))
    submitted = []
    statuses = {}

    def submit(_kb_id):
        job_id = f"index-{len(submitted) + 1}"
        submitted.append(job_id)
        statuses[job_id] = stuck_status
        return {"job_id": job_id}

    failed = _sink(
        tmp_path,
        catalog,
        [],
        job="job-index-timeout",
        submitter=submit,
        index_status_reader=lambda job_id: {"status": statuses[job_id]},
        index_timeout_seconds=0.01,
    )
    failed.upsert(_document("a", "guide.md", b"body"), b"body")
    with pytest.raises(RetryableConnectorError) as exc_info:
        failed.commit(
            snapshot=True,
            seen_external_ids=frozenset({"a"}),
            heartbeat=lambda: None,
        )
    assert isinstance(exc_info.value.__cause__, TimeoutError)
    assert failed.journal is not None
    journal = json.loads(failed.journal.read_text(encoding="utf-8"))
    assert journal["phase"] == "materialized"
    assert journal["index_job_id"] == "index-1"
    assert submitted == ["index-1"]
    failed.abort()

    statuses["index-1"] = "succeeded"
    recovered = _sink(
        tmp_path,
        catalog,
        [],
        job="job-index-timeout",
        submitter=submit,
        index_status_reader=lambda job_id: {"status": statuses[job_id]},
        index_timeout_seconds=0.01,
        recovering_commit=True,
    )
    recovered.recover_commit(heartbeat=lambda: None)

    assert submitted == ["index-1"]
    assert recovered.journal is not None
    journal = json.loads(recovered.journal.read_text(encoding="utf-8"))
    assert journal["phase"] == "indexed"
    assert journal["index_job_id"] == "index-1"
    recovered.finalize()
    catalog.close()


@pytest.mark.parametrize("terminal_status", ["failed", "cancelled"])
def test_materialized_sink_recovery_replaces_only_terminal_index_job(
    tmp_path,
    terminal_status,
):
    catalog = SourceCatalog(str(tmp_path / "state.db"))
    submitted = []
    statuses = {}

    def submit(_kb_id):
        job_id = f"index-{len(submitted) + 1}"
        submitted.append(job_id)
        statuses[job_id] = "running" if job_id == "index-1" else "succeeded"
        return {"job_id": job_id}

    failed = _sink(
        tmp_path,
        catalog,
        [],
        job="job-terminal-index",
        submitter=submit,
        index_status_reader=lambda job_id: {"status": statuses[job_id]},
        index_timeout_seconds=0.01,
    )
    failed.upsert(_document("a", "guide.md", b"body"), b"body")
    with pytest.raises(RetryableConnectorError):
        failed.commit(
            snapshot=True,
            seen_external_ids=frozenset({"a"}),
            heartbeat=lambda: None,
        )
    failed.abort()

    statuses["index-1"] = terminal_status
    recovered = _sink(
        tmp_path,
        catalog,
        [],
        job="job-terminal-index",
        submitter=submit,
        index_status_reader=lambda job_id: {"status": statuses[job_id]},
        index_timeout_seconds=0.01,
        recovering_commit=True,
    )
    recovered.recover_commit(heartbeat=lambda: None)

    assert submitted == ["index-1", "index-2"]
    assert recovered.journal is not None
    journal = json.loads(recovered.journal.read_text(encoding="utf-8"))
    assert journal["phase"] == "indexed"
    assert journal["index_job_id"] == "index-2"
    recovered.finalize()
    catalog.close()


def test_materialized_sink_submits_at_most_one_terminal_replacement_per_attempt(
    tmp_path,
):
    catalog = SourceCatalog(str(tmp_path / "state.db"))
    submitted = []

    def submit(_kb_id):
        job_id = f"index-{len(submitted) + 1}"
        submitted.append(job_id)
        return {"job_id": job_id}

    failed = _sink(
        tmp_path,
        catalog,
        [],
        job="job-bounded-replacement",
        submitter=submit,
        index_status_reader=lambda _job_id: {"status": "failed"},
    )
    failed.upsert(_document("a", "guide.md", b"body"), b"body")
    with pytest.raises(RetryableConnectorError):
        failed.commit(
            snapshot=True,
            seen_external_ids=frozenset({"a"}),
            heartbeat=lambda: None,
        )
    assert submitted == ["index-1"]
    failed.abort()

    recovered = _sink(
        tmp_path,
        catalog,
        [],
        job="job-bounded-replacement",
        submitter=submit,
        index_status_reader=lambda _job_id: {"status": "failed"},
        recovering_commit=True,
    )
    with pytest.raises(RuntimeError, match="connector index job failed"):
        recovered.recover_commit(heartbeat=lambda: None)

    assert submitted == ["index-1", "index-2"]
    assert recovered.journal is not None
    journal = json.loads(recovered.journal.read_text(encoding="utf-8"))
    assert journal["phase"] == "materialized"
    assert journal["index_job_id"] == "index-2"
    recovered.abort()
    catalog.close()


def test_materialized_sink_persists_async_job_before_rejecting_missing_reader(
    tmp_path,
):
    catalog = SourceCatalog(str(tmp_path / "state.db"))
    sink = _sink(
        tmp_path,
        catalog,
        [],
        job="job-missing-index-reader",
        submitter=lambda _kb_id: {"job_id": "accepted-index"},
    )
    sink.upsert(_document("a", "guide.md", b"body"), b"body")

    with pytest.raises(RetryableConnectorError) as exc_info:
        sink.commit(
            snapshot=True,
            seen_external_ids=frozenset({"a"}),
            heartbeat=lambda: None,
        )

    assert isinstance(exc_info.value.__cause__, RuntimeError)
    assert "status reader is unavailable" in str(exc_info.value.__cause__)
    assert sink.journal is not None
    journal = json.loads(sink.journal.read_text(encoding="utf-8"))
    assert journal["phase"] == "materialized"
    assert journal["index_job_id"] == "accepted-index"
    sink.abort()

    recovered = _sink(
        tmp_path,
        catalog,
        [],
        job="job-missing-index-reader",
        submitter=lambda _kb_id: pytest.fail("persisted index job was resubmitted"),
        recovering_commit=True,
    )
    with pytest.raises(RuntimeError, match="status reader is unavailable"):
        recovered.recover_commit(heartbeat=lambda: None)
    assert recovered.journal is not None
    journal = json.loads(recovered.journal.read_text(encoding="utf-8"))
    assert journal["phase"] == "materialized"
    assert journal["index_job_id"] == "accepted-index"
    recovered.abort()
    catalog.close()


def test_materialized_sink_does_not_replace_disappeared_index_job(tmp_path):
    catalog = SourceCatalog(str(tmp_path / "state.db"))
    submitted = []

    def submit(_kb_id):
        job_id = f"index-{len(submitted) + 1}"
        submitted.append(job_id)
        return {"job_id": job_id}

    failed = _sink(
        tmp_path,
        catalog,
        [],
        job="job-disappeared-index",
        submitter=submit,
        index_status_reader=lambda _job_id: None,
    )
    failed.upsert(_document("a", "guide.md", b"body"), b"body")
    with pytest.raises(RetryableConnectorError) as exc_info:
        failed.commit(
            snapshot=True,
            seen_external_ids=frozenset({"a"}),
            heartbeat=lambda: None,
        )
    assert "index job disappeared" in str(exc_info.value.__cause__)
    failed.abort()

    recovered = _sink(
        tmp_path,
        catalog,
        [],
        job="job-disappeared-index",
        submitter=submit,
        index_status_reader=lambda _job_id: None,
        recovering_commit=True,
    )
    with pytest.raises(RuntimeError, match="index job disappeared"):
        recovered.recover_commit(heartbeat=lambda: None)

    assert submitted == ["index-1"]
    assert recovered.journal is not None
    journal = json.loads(recovered.journal.read_text(encoding="utf-8"))
    assert journal["index_job_id"] == "index-1"
    recovered.abort()
    catalog.close()


def test_materialized_sink_rejects_unaccepted_asynchronous_index_job(tmp_path):
    catalog = SourceCatalog(str(tmp_path / "state.db"))
    sink = _sink(
        tmp_path,
        catalog,
        [],
        job="job-index-not-accepted",
        submitter=lambda _kb_id: {},
        index_status_reader=lambda _job_id: {"status": "succeeded"},
    )
    sink.upsert(_document("a", "guide.md", b"body"), b"body")

    with pytest.raises(RetryableConnectorError) as exc_info:
        sink.commit(
            snapshot=True,
            seen_external_ids=frozenset({"a"}),
            heartbeat=lambda: None,
        )

    assert "index job was not accepted" in str(exc_info.value.__cause__)
    assert sink.journal is not None
    journal = json.loads(sink.journal.read_text(encoding="utf-8"))
    assert journal["phase"] == "materialized"
    assert journal["index_job_id"] is None
    sink.abort()
    catalog.close()


@pytest.mark.parametrize("timeout", [True, 0, -1, float("inf"), float("nan")])
def test_materialized_sink_rejects_invalid_index_timeout(tmp_path, timeout):
    catalog = SourceCatalog(str(tmp_path / "state.db"))
    with pytest.raises(ValueError, match="index timeout must be positive"):
        MaterializedSyncSink(
            source_dir=str(tmp_path / "sources"),
            catalog=catalog,
            index_submitter=lambda _kb_id: {},
            owner_id="owner",
            workspace_visible=False,
            index_timeout_seconds=timeout,
        )
    catalog.close()


def test_materialized_sink_same_content_rename_updates_catalog_not_artifact_identity(
    tmp_path,
):
    catalog = SourceCatalog(str(tmp_path / "state.db"))
    artifact_store = SourceArtifactStore(
        tmp_path / "artifacts",
        max_versions_per_source=3,
        user_max_versions_per_source=2,
    )
    submitted = []
    content = b'{"title":"same content"}'
    first = _sink(
        tmp_path,
        catalog,
        submitted,
        job="job-rename-1",
        artifact_store=artifact_store,
        artifact_versions_to_keep=2,
    )
    first.upsert(
        _document(
            "a",
            "guide.md",
            content,
            media_type="text/markdown",
            fetched_at=1,
        ),
        content,
    )
    first.commit(
        snapshot=True, seen_external_ids=frozenset({"a"}), heartbeat=lambda: None
    )
    first.finalize()

    second = _sink(
        tmp_path,
        catalog,
        submitted,
        job="job-rename-2",
        artifact_store=artifact_store,
        artifact_versions_to_keep=2,
    )
    second.upsert(
        _document(
            "a",
            "renamed.txt",
            content,
            media_type="text/plain",
            fetched_at=2,
        ),
        content,
    )
    second.commit(
        snapshot=True, seen_external_ids=frozenset({"a"}), heartbeat=lambda: None
    )
    second.finalize()

    current = catalog.list_sources("tenant", "storage-kb")
    assert len(current) == 1
    assert current[0]["display_name"].endswith(".txt")
    assert current[0]["media_type"] == "text/plain"
    assert current[0]["metadata"]["original_display_name"] == "renamed.txt"
    versions = artifact_store.list_versions(
        "tenant", "storage-kb", current[0]["source_id"]
    )
    assert len(versions) == 1
    assert versions[0]["media_type"] == "text/markdown"
    assert versions[0]["display_name"] == "guide.md"
    assert submitted == ["storage-kb", "storage-kb"]
    catalog.close()


def test_materialized_sink_syncs_new_version_after_restore_at_user_limit(tmp_path):
    catalog = SourceCatalog(str(tmp_path / "state.db"))
    artifact_store = SourceArtifactStore(
        tmp_path / "artifacts",
        max_versions_per_source=3,
        user_max_versions_per_source=2,
    )
    submitted = []
    contents = (b"version one", b"version two", b"version three")

    for index, content in enumerate(contents[:2], start=1):
        sink = _sink(
            tmp_path,
            catalog,
            submitted,
            job=f"job-version-{index}",
            artifact_store=artifact_store,
            artifact_versions_to_keep=2,
        )
        sink.upsert(_document("a", "guide.md", content, fetched_at=index), content)
        sink.commit(
            snapshot=True,
            seen_external_ids=frozenset({"a"}),
            heartbeat=lambda: None,
        )
        sink.finalize()

    source_id = catalog.list_sources("tenant", "storage-kb")[0]["source_id"]
    first_version = next(
        row
        for row in artifact_store.list_versions("tenant", "storage-kb", source_id)
        if row["content_sha256"] == hashlib.sha256(contents[0]).hexdigest()
    )
    deleted = artifact_store.delete_version(
        "tenant", "storage-kb", source_id, first_version["version_id"]
    )
    artifact_store.restore("tenant", "storage-kb", deleted["recovery_token"])
    assert artifact_store.usage("tenant", "storage-kb")["active_versions"] == 2

    third = _sink(
        tmp_path,
        catalog,
        submitted,
        job="job-version-3",
        artifact_store=artifact_store,
        artifact_versions_to_keep=2,
    )
    third.upsert(_document("a", "guide.md", contents[2], fetched_at=3), contents[2])
    third.commit(
        snapshot=True, seen_external_ids=frozenset({"a"}), heartbeat=lambda: None
    )
    third.finalize()

    active = artifact_store.list_versions("tenant", "storage-kb", source_id)
    assert {row["content_sha256"] for row in active} == {
        hashlib.sha256(contents[1]).hexdigest(),
        hashlib.sha256(contents[2]).hexdigest(),
    }
    usage = artifact_store.usage("tenant", "storage-kb")
    assert usage["active_versions"] == 2
    assert usage["trash_versions"] == 1
    assert usage["active_bytes"] > 0
    assert usage["trash_bytes"] > 0
    catalog.close()


def test_materialized_sink_many_documents_does_not_rescan_artifact_tree(
    tmp_path, monkeypatch
):
    catalog = SourceCatalog(str(tmp_path / "state.db"))
    artifact_store = SourceArtifactStore(
        tmp_path / "artifacts", max_total_bytes=1_000_000
    )
    physical_usage_calls = 0
    original_physical_usage = artifact_store._physical_usage_bytes

    def counted_physical_usage():
        nonlocal physical_usage_calls
        physical_usage_calls += 1
        return original_physical_usage()

    monkeypatch.setattr(artifact_store, "_physical_usage_bytes", counted_physical_usage)
    sink = _sink(
        tmp_path,
        catalog,
        [],
        job="job-many-documents",
        artifact_store=artifact_store,
    )
    external_ids = set()
    for index in range(40):
        external_id = f"document-{index}"
        content = f"content-{index}".encode()
        external_ids.add(external_id)
        sink.upsert(_document(external_id, f"document-{index}.md", content), content)
    sink.commit(
        snapshot=True,
        seen_external_ids=frozenset(external_ids),
        heartbeat=lambda: None,
    )
    sink.finalize()

    assert physical_usage_calls == 0
    assert len(catalog.list_sources("tenant", "storage-kb")) == 40
    catalog.close()


def test_connection_cleanup_removes_only_owned_projection_and_retains_raw_versions(
    tmp_path,
):
    catalog = SourceCatalog(str(tmp_path / "state.db"))
    artifact_store = SourceArtifactStore(tmp_path / "artifacts")
    first_content = b"connection one raw version"
    first = _sink(
        tmp_path,
        catalog,
        [],
        job="job-first",
        connection="conn-1",
        artifact_store=artifact_store,
    )
    first.upsert(_document("first", "first.md", first_content), first_content)
    first.commit(
        snapshot=True,
        seen_external_ids=frozenset({"first"}),
        heartbeat=lambda: None,
    )
    first.finalize()

    second_content = b"connection two remains"
    second = _sink(
        tmp_path,
        catalog,
        [],
        job="job-second",
        connection="conn-2",
        artifact_store=artifact_store,
    )
    second.upsert(_document("second", "second.md", second_content), second_content)
    second.commit(
        snapshot=True,
        seen_external_ids=frozenset({"second"}),
        heartbeat=lambda: None,
    )
    second.finalize()

    source_dir = tmp_path / "sources"
    user_file = source_dir / "user.md"
    user_file.write_text("manual upload", encoding="utf-8")
    work_root = tmp_path / ".sources.sync-work"
    owned_work = work_root / "conn-1-crashed.staging"
    other_work = work_root / "conn-2-crashed.staging"
    prefixed_connection_work = work_root / "conn-1-other-crashed.staging"
    owned_work.mkdir(parents=True)
    other_work.mkdir()
    prefixed_connection_work.mkdir()
    access_store = ResourceAccessStore(tmp_path / "access.db")
    access_store.set_kb_policy("tenant", "storage-kb", "owner", "private")
    projected = catalog.list_sources("tenant", "storage-kb")
    for row in projected:
        access_store.set_document_policy(
            "tenant",
            "storage-kb",
            build_document_id(row["display_name"]),
            row["display_name"],
            "owner",
            "private",
        )
    historical_document_id = build_document_id(".cogdoc-connector-historical-name.txt")
    access_store.set_document_policy(
        "tenant",
        "storage-kb",
        historical_document_id,
        ".cogdoc-connector-historical-name.txt",
        "owner",
        "private",
    )
    acl_cleanups = []
    index_jobs = []

    result = MaterializedSyncSink.cleanup_connection(
        source_dir=source_dir,
        tenant_id="tenant",
        kb_id="storage-kb",
        connection_id="conn-1",
        catalog=catalog,
        index_submitter=lambda kb_id: (
            index_jobs.append(kb_id) or {"job_id": "cleanup-index"}
        ),
        index_status_reader=lambda _job_id: {"status": "succeeded"},
        resource_access_store=access_store,
        acl_document_ids=(historical_document_id,),
        work_job_ids=("crashed",),
        acl_state_cleaner=lambda *args: acl_cleanups.append(args),
    )

    active = catalog.list_sources("tenant", "storage-kb")
    deleted = catalog.list_sources(
        "tenant", "storage-kb", include_deleted=True, connection_id="conn-1"
    )
    assert [row["connection_id"] for row in active] == ["conn-2"]
    assert len(deleted) == 1 and deleted[0]["deleted_at"] is not None
    assert not (source_dir / ".connections" / "conn-1").exists()
    assert (source_dir / ".connections" / "conn-2").is_dir()
    assert not owned_work.exists() and other_work.is_dir()
    assert prefixed_connection_work.is_dir()
    assert user_file.read_text(encoding="utf-8") == "manual upload"
    assert list(source_dir.glob(".cogdoc-connector-*.md")) == [
        source_dir / active[0]["display_name"]
    ]
    contracts = json.loads(
        (source_dir / ".cogdoc-source-contracts.json").read_text(encoding="utf-8")
    )["documents"]
    assert set(contracts) == {active[0]["display_name"]}
    artifact_versions = artifact_store.list_versions(
        "tenant", "storage-kb", deleted[0]["source_id"]
    )
    assert len(artifact_versions) == 1
    assert (
        artifact_store.read(
            "tenant",
            "storage-kb",
            deleted[0]["source_id"],
            artifact_versions[0]["version_id"],
        )
        == first_content
    )
    assert (
        access_store.get_document_policy(
            "tenant", "storage-kb", build_document_id(deleted[0]["display_name"])
        )
        is None
    )
    assert (
        access_store.get_document_policy(
            "tenant", "storage-kb", build_document_id(active[0]["display_name"])
        )
        is not None
    )
    assert (
        access_store.get_document_policy("tenant", "storage-kb", historical_document_id)
        is None
    )
    assert acl_cleanups[0][2] == "connector:conn-1"
    assert index_jobs == ["storage-kb"]
    assert result["files"] == 1
    access_store.close()
    catalog.close()


def test_connection_work_cleanup_rejects_symlink(tmp_path):
    work_root = tmp_path / ".sources.sync-work"
    work_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = work_root / "conn-1-job-1.staging"
    link.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="must not be a symlink"):
        MaterializedSyncSink._cleanup_connection_work(
            work_root,
            "conn-1",
            ("job-1",),
        )

    assert link.is_symlink()
    assert outside.is_dir()


def test_connection_cleanup_fails_closed_on_conflicting_ownership_ledgers(tmp_path):
    catalog = SourceCatalog(str(tmp_path / "state.db"))
    content = b"must not be deleted when ownership records disagree"
    sink = _sink(tmp_path, catalog, [], job="job-conflicting-owner")
    sink.upsert(_document("first", "first.md", content), content)
    sink.commit(
        snapshot=True,
        seen_external_ids=frozenset({"first"}),
        heartbeat=lambda: None,
    )
    sink.finalize()

    source_dir = tmp_path / "sources"
    catalog_row = catalog.list_sources("tenant", "storage-kb")[0]
    materialized_name = catalog_row["display_name"]
    contract_path = source_dir / ".cogdoc-source-contracts.json"
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    contract["documents"][materialized_name]["metadata"]["connection_id"] = "conn-2"
    MaterializedSyncSink._write_json_atomic(contract_path, contract)

    with pytest.raises(ValueError, match="ownership ledgers conflict"):
        MaterializedSyncSink.cleanup_connection(
            source_dir=source_dir,
            tenant_id="tenant",
            kb_id="storage-kb",
            connection_id="conn-1",
            catalog=catalog,
            index_submitter=lambda _kb_id: {"job_id": "must-not-run"},
            index_status_reader=lambda _job_id: {"status": "succeeded"},
        )

    assert (source_dir / materialized_name).read_bytes() == content
    assert (source_dir / ".connections" / "conn-1").is_dir()
    assert catalog.list_sources("tenant", "storage-kb")
    catalog.close()


def test_connection_cleanup_validates_and_removes_legacy_materialization(tmp_path):
    catalog = SourceCatalog(str(tmp_path / "state.db"))
    content = b"legacy connector projection"
    sink = _sink(tmp_path, catalog, [], job="job-legacy-cleanup")
    sink.upsert(_document("quarterly", "Quarterly Report.md", content), content)
    sink.commit(
        snapshot=True,
        seen_external_ids=frozenset({"quarterly"}),
        heartbeat=lambda: None,
    )
    sink.finalize()

    source_dir = tmp_path / "sources"
    current = source_dir / ".connections" / "conn-1"
    manifest_path = current / ".cogdoc-connection.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_row = manifest["sources"]["quarterly"]
    previous_name = manifest_row["filename"]
    document = manifest_row["document"]
    source_id = document["source_id"]
    legacy_name = f"Quarterly Report--{source_id.removeprefix('src-')[:12]}.md"
    (source_dir / previous_name).replace(source_dir / legacy_name)
    (current / previous_name).replace(current / legacy_name)
    manifest_row["filename"] = legacy_name
    document["name"] = legacy_name
    document["metadata"]["materialized_name"] = legacy_name
    MaterializedSyncSink._write_json_atomic(manifest_path, manifest)
    contract_path = source_dir / ".cogdoc-source-contracts.json"
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    contract["documents"].pop(previous_name)
    contract["documents"][legacy_name] = document
    MaterializedSyncSink._write_json_atomic(contract_path, contract)
    catalog.upsert(
        "tenant",
        "storage-kb",
        SourceDocument.from_manifest_document(document),
        connection_id="conn-1",
    )

    (source_dir / legacy_name).write_bytes(b"manual file collision")
    with pytest.raises(ValueError, match="content conflicts"):
        MaterializedSyncSink.cleanup_connection(
            source_dir=source_dir,
            tenant_id="tenant",
            kb_id="storage-kb",
            connection_id="conn-1",
            catalog=catalog,
            index_submitter=lambda _kb_id: {"job_id": "must-not-run"},
            index_status_reader=lambda _job_id: {"status": "succeeded"},
        )
    assert (source_dir / legacy_name).read_bytes() == b"manual file collision"
    assert catalog.list_sources("tenant", "storage-kb")

    (source_dir / legacy_name).write_bytes(content)
    MaterializedSyncSink.cleanup_connection(
        source_dir=source_dir,
        tenant_id="tenant",
        kb_id="storage-kb",
        connection_id="conn-1",
        catalog=catalog,
        index_submitter=lambda _kb_id: {"job_id": "legacy-cleanup-index"},
        index_status_reader=lambda _job_id: {"status": "succeeded"},
    )
    assert not (source_dir / legacy_name).exists()
    assert not current.exists()
    assert catalog.list_sources("tenant", "storage-kb") == []
    catalog.close()


def test_connection_cleanup_is_retryable_after_index_failure(tmp_path):
    catalog = SourceCatalog(str(tmp_path / "state.db"))
    content = b"retryable connection cleanup"
    sink = _sink(tmp_path, catalog, [], job="job-cleanup-retry")
    sink.upsert(_document("first", "first.md", content), content)
    sink.commit(
        snapshot=True,
        seen_external_ids=frozenset({"first"}),
        heartbeat=lambda: None,
    )
    sink.finalize()

    source_dir = tmp_path / "sources"
    row = catalog.list_sources("tenant", "storage-kb")[0]
    document_id = build_document_id(row["display_name"])
    access_store = ResourceAccessStore(tmp_path / "retry-access.db")
    access_store.set_kb_policy("tenant", "storage-kb", "owner", "workspace")
    access_store.set_document_policy(
        "tenant",
        "storage-kb",
        document_id,
        row["display_name"],
        "owner",
        "workspace",
    )
    access_store.grant_subject(
        "tenant", "storage-kb", "alice", "viewer", document_id=document_id
    )
    with pytest.raises(RuntimeError, match="index job failed"):
        MaterializedSyncSink.cleanup_connection(
            source_dir=source_dir,
            tenant_id="tenant",
            kb_id="storage-kb",
            connection_id="conn-1",
            catalog=catalog,
            index_submitter=lambda _kb_id: {"job_id": "failed-index"},
            index_status_reader=lambda _job_id: {"status": "failed"},
            resource_access_store=access_store,
        )

    # The visible projection is already fail-closed, while the durable
    # tombstone still carries enough ownership to retry the index transition.
    assert catalog.list_sources("tenant", "storage-kb") == []
    assert (
        len(
            catalog.list_sources(
                "tenant", "storage-kb", include_deleted=True, connection_id="conn-1"
            )
        )
        == 1
    )
    assert not list(source_dir.glob(".cogdoc-connector-*"))
    policy = access_store.get_document_policy("tenant", "storage-kb", document_id)
    assert policy is not None and policy["policy"] == "private"
    assert (
        access_store.list_grants("tenant", "storage-kb", document_id=document_id) == []
    )

    submitted = []
    MaterializedSyncSink.cleanup_connection(
        source_dir=source_dir,
        tenant_id="tenant",
        kb_id="storage-kb",
        connection_id="conn-1",
        catalog=catalog,
        index_submitter=lambda kb_id: (
            submitted.append(kb_id) or {"job_id": "retry-index"}
        ),
        index_status_reader=lambda _job_id: {"status": "succeeded"},
        resource_access_store=access_store,
    )
    assert submitted == ["storage-kb"]
    assert access_store.get_document_policy("tenant", "storage-kb", document_id) is None
    access_store.close()
    catalog.close()


def test_connection_cleanup_recovers_after_contract_update_crash(tmp_path, monkeypatch):
    catalog = SourceCatalog(str(tmp_path / "state.db"))
    content = b"ownership survives a cleanup crash"
    sink = _sink(tmp_path, catalog, [], job="job-cleanup-contract-crash")
    sink.upsert(_document("first", "first.md", content), content)
    sink.commit(
        snapshot=True,
        seen_external_ids=frozenset({"first"}),
        heartbeat=lambda: None,
    )
    sink.finalize()

    source_dir = tmp_path / "sources"
    contract_path = source_dir / ".cogdoc-source-contracts.json"
    original_write = MaterializedSyncSink._write_json_atomic
    interrupted = False

    def fail_contract_update(path, payload):
        nonlocal interrupted
        if path == contract_path and not interrupted:
            interrupted = True
            raise OSError("simulated process loss before contract replacement")
        return original_write(path, payload)

    monkeypatch.setattr(
        MaterializedSyncSink, "_write_json_atomic", staticmethod(fail_contract_update)
    )
    with pytest.raises(OSError, match="simulated process loss"):
        MaterializedSyncSink.cleanup_connection(
            source_dir=source_dir,
            tenant_id="tenant",
            kb_id="storage-kb",
            connection_id="conn-1",
            catalog=catalog,
            index_submitter=lambda _kb_id: {"job_id": "not-reached"},
            index_status_reader=lambda _job_id: {"status": "succeeded"},
        )

    assert not list(source_dir.glob(".cogdoc-connector-*"))
    assert (source_dir / ".connections" / "conn-1").is_dir()
    assert catalog.list_sources("tenant", "storage-kb")
    assert json.loads(contract_path.read_text(encoding="utf-8"))["documents"]

    monkeypatch.setattr(
        MaterializedSyncSink, "_write_json_atomic", staticmethod(original_write)
    )
    MaterializedSyncSink.cleanup_connection(
        source_dir=source_dir,
        tenant_id="tenant",
        kb_id="storage-kb",
        connection_id="conn-1",
        catalog=catalog,
        index_submitter=lambda _kb_id: {"job_id": "cleanup-retry"},
        index_status_reader=lambda _job_id: {"status": "succeeded"},
    )
    assert catalog.list_sources("tenant", "storage-kb") == []
    assert not (source_dir / ".connections" / "conn-1").exists()
    assert json.loads(contract_path.read_text(encoding="utf-8"))["documents"] == {}
    catalog.close()


def test_connection_cleanup_recovers_after_partial_current_tree_removal(
    tmp_path, monkeypatch
):
    catalog = SourceCatalog(str(tmp_path / "state.db"))
    content = b"atomic retirement keeps partial tree retryable"
    sink = _sink(tmp_path, catalog, [], job="job-cleanup-tree-crash")
    sink.upsert(_document("first", "first.md", content), content)
    sink.commit(
        snapshot=True,
        seen_external_ids=frozenset({"first"}),
        heartbeat=lambda: None,
    )
    sink.finalize()

    source_dir = tmp_path / "sources"
    retired = (
        tmp_path / ".sources.connection-delete" / hashlib.sha256(b"conn-1").hexdigest()
    )
    original_rmtree = shutil.rmtree
    interrupted = False

    def partial_rmtree(path, *args, **kwargs):
        nonlocal interrupted
        candidate = Path(path)
        if candidate == retired and not interrupted:
            interrupted = True
            (candidate / ".cogdoc-connection.json").unlink()
            raise OSError("simulated partial current cleanup")
        return original_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(shutil, "rmtree", partial_rmtree)
    with pytest.raises(OSError, match="simulated partial current cleanup"):
        MaterializedSyncSink.cleanup_connection(
            source_dir=source_dir,
            tenant_id="tenant",
            kb_id="storage-kb",
            connection_id="conn-1",
            catalog=catalog,
            index_submitter=lambda _kb_id: {"job_id": "not-reached"},
            index_status_reader=lambda _job_id: {"status": "succeeded"},
        )

    assert retired.is_dir()
    assert not (retired / ".cogdoc-connection.json").exists()
    assert catalog.list_sources("tenant", "storage-kb")

    monkeypatch.setattr(shutil, "rmtree", original_rmtree)
    MaterializedSyncSink.cleanup_connection(
        source_dir=source_dir,
        tenant_id="tenant",
        kb_id="storage-kb",
        connection_id="conn-1",
        catalog=catalog,
        index_submitter=lambda _kb_id: {"job_id": "cleanup-retry"},
        index_status_reader=lambda _job_id: {"status": "succeeded"},
    )
    assert not retired.exists()
    assert catalog.list_sources("tenant", "storage-kb") == []
    catalog.close()


def test_connection_cleanup_releases_kb_lock_while_index_switches(tmp_path):
    catalog = SourceCatalog(str(tmp_path / "state.db"))
    content = b"index switch needs the KB mutation lock"
    sink = _sink(tmp_path, catalog, [], job="job-cleanup-index-lock")
    sink.upsert(_document("first", "first.md", content), content)
    sink.commit(
        snapshot=True,
        seen_external_ids=frozenset({"first"}),
        heartbeat=lambda: None,
    )
    sink.finalize()

    switched = Event()
    workers = []

    def submit_index(_kb_id):
        def switch_generation():
            with kb_write_lock("storage-kb"):
                switched.set()

        worker = Thread(target=switch_generation)
        worker.start()
        workers.append(worker)
        return {"job_id": "locking-index"}

    def read_index(_job_id):
        return {"status": "succeeded" if switched.wait(timeout=1) else "failed"}

    try:
        MaterializedSyncSink.cleanup_connection(
            source_dir=tmp_path / "sources",
            tenant_id="tenant",
            kb_id="storage-kb",
            connection_id="conn-1",
            catalog=catalog,
            index_submitter=submit_index,
            index_status_reader=read_index,
        )
    finally:
        for worker in workers:
            worker.join(timeout=1)

    assert switched.is_set()
    assert all(not worker.is_alive() for worker in workers)
    catalog.close()


def test_connection_cleanup_index_wait_has_a_deadline():
    submitted = []
    recorded = []
    statuses = {}

    def submit(_kb_id):
        job_id = f"stuck-index-{len(submitted) + 1}"
        submitted.append(job_id)
        statuses[job_id] = "running"
        return {"job_id": job_id}

    with pytest.raises(TimeoutError, match="index job timed out"):
        MaterializedSyncSink._rebuild_index(
            "storage-kb",
            index_submitter=submit,
            index_status_reader=lambda job_id: {"status": statuses[job_id]},
            timeout_seconds=0.02,
            job_recorder=recorded.append,
        )
    assert submitted == recorded == ["stuck-index-1"]

    statuses["stuck-index-1"] = "succeeded"
    MaterializedSyncSink._rebuild_index(
        "storage-kb",
        index_submitter=submit,
        index_status_reader=lambda job_id: {"status": statuses[job_id]},
        timeout_seconds=0.02,
        existing_job_id=recorded[0],
        job_recorder=recorded.append,
    )
    assert submitted == recorded == ["stuck-index-1"]
