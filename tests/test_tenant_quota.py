import os
from pathlib import Path

import pytest

from cogdoc.api.tenant_quota import (
    TenantMutationInProgress,
    TenantQuotaExceeded,
    TenantQuotaManager,
    TenantQuotaPolicy,
)
from cogdoc.service.mutation_paths import mutation_backup_path


class _Registry:
    def __init__(self, root: Path):
        self.root = root
        self.records: list[dict] = []

    def list(self, tenant_id=None):
        if tenant_id is None:
            return list(self.records)
        return [row for row in self.records if row["tenant_id"] == tenant_id]

    def source_dir(self, storage_id):
        return str(self.root / storage_id / "sources")


def _write(registry, storage_id, name, content):
    path = Path(registry.source_dir(storage_id))
    path.mkdir(parents=True, exist_ok=True)
    (path / name).write_bytes(content)


def test_quota_counts_only_the_requested_tenant(tmp_path):
    registry = _Registry(tmp_path)
    registry.records.extend(
        [
            {"tenant_id": "a", "storage_id": "a-kb", "kb_id": "kb"},
            {"tenant_id": "b", "storage_id": "b-kb", "kb_id": "kb"},
        ]
    )
    _write(registry, "a-kb", "a.pdf", b"123")
    _write(registry, "b-kb", "b.pdf", b"123456789")
    manager = TenantQuotaManager(
        registry,
        TenantQuotaPolicy(max_knowledge_bases=2, max_documents=2, max_storage_bytes=8),
    )

    snapshot = manager.snapshot("a")
    assert snapshot["usage"] == {
        "knowledge_bases": 1,
        "documents": 1,
        "storage_bytes": 3,
    }
    token = manager.reserve_upload("a", "a-kb", registry.source_dir("a-kb"), "x.pdf", 5)
    assert manager.snapshot("a")["reserved"]["storage_bytes"] == 5
    manager.release(token)


def test_quota_counts_every_supported_document_format(tmp_path):
    registry = _Registry(tmp_path)
    registry.records.append(
        {"tenant_id": "a", "storage_id": "a-kb", "kb_id": "kb"}
    )
    _write(registry, "a-kb", "notes.md", b"123")
    _write(registry, "a-kb", "readme.txt", b"4567")
    _write(registry, "a-kb", "internal.json", b"not-a-source")
    manager = TenantQuotaManager(registry, TenantQuotaPolicy(max_documents=2))

    assert manager.snapshot("a")["usage"] == {
        "knowledge_bases": 1,
        "documents": 2,
        "storage_bytes": 7,
    }
    with pytest.raises(TenantQuotaExceeded):
        manager.reserve_upload(
            "a", "a-kb", registry.source_dir("a-kb"), "third.html", 1
        )


def test_connector_snapshot_reserves_growth_until_materialization(tmp_path):
    registry = _Registry(tmp_path)
    registry.records.append(
        {"tenant_id": "a", "storage_id": "a-kb", "kb_id": "kb"}
    )
    _write(registry, "a-kb", "existing.md", b"old")
    baseline = tmp_path / "baseline"
    proposed = tmp_path / "proposed"
    other = tmp_path / "other"
    baseline.mkdir()
    proposed.mkdir()
    other.mkdir()
    (baseline / "existing.md").write_bytes(b"old")
    (proposed / "existing.md").write_bytes(b"old")
    (proposed / "new.txt").write_bytes(b"more")
    (other / "another.md").write_bytes(b"x")
    manager = TenantQuotaManager(
        registry,
        TenantQuotaPolicy(max_documents=2, max_storage_bytes=7),
    )

    token = manager.reserve_connector_snapshot(
        "a",
        "a-kb",
        registry.source_dir("a-kb"),
        str(baseline),
        str(proposed),
        "sync-1",
    )
    assert token
    assert manager.snapshot("a")["reserved"] == {
        "knowledge_bases": 0,
        "documents": 1,
        "storage_bytes": 4,
    }
    with pytest.raises(TenantQuotaExceeded):
        manager.reserve_connector_snapshot(
            "a",
            "a-kb",
            registry.source_dir("a-kb"),
            str(tmp_path / "missing"),
            str(other),
            "sync-2",
        )
    manager.release(token)


def test_connector_recovery_does_not_double_charge_already_published_prefix(tmp_path):
    registry = _Registry(tmp_path)
    registry.records.append(
        {"tenant_id": "a", "storage_id": "a-kb", "kb_id": "kb"}
    )
    _write(registry, "a-kb", "old.md", b"old")
    _write(registry, "a-kb", "new.md", b"new")
    baseline = tmp_path / "baseline-prefix"
    proposed = tmp_path / "proposed-prefix"
    baseline.mkdir()
    proposed.mkdir()
    (baseline / "old.md").write_bytes(b"old")
    (proposed / "old.md").write_bytes(b"old")
    (proposed / "new.md").write_bytes(b"new")
    manager = TenantQuotaManager(
        registry,
        TenantQuotaPolicy(max_documents=2, max_storage_bytes=6),
    )

    token = manager.reserve_connector_snapshot(
        "a",
        "a-kb",
        registry.source_dir("a-kb"),
        str(baseline),
        str(proposed),
        "recover",
    )
    assert token
    assert manager.snapshot("a")["reserved"]["documents"] == 0
    assert manager.snapshot("a")["reserved"]["storage_bytes"] == 0
    manager.release(token)


def test_inflight_reservations_close_quota_race(tmp_path):
    registry = _Registry(tmp_path)
    registry.records.append(
        {"tenant_id": "a", "storage_id": "a-kb", "kb_id": "kb"}
    )
    manager = TenantQuotaManager(
        registry,
        TenantQuotaPolicy(max_documents=1, max_storage_bytes=10),
    )
    token = manager.reserve_upload(
        "a", "a-kb", registry.source_dir("a-kb"), "first.pdf", 6
    )
    with pytest.raises(TenantQuotaExceeded) as exc_info:
        manager.reserve_upload(
            "a", "a-kb", registry.source_dir("a-kb"), "second.pdf", 1
        )
    assert exc_info.value.resource == "documents"
    manager.release(token)
    manager.reserve_upload(
        "a", "a-kb", registry.source_dir("a-kb"), "second.pdf", 1
    )


def test_replacement_uses_net_bytes_and_duplicate_pending_is_rejected(tmp_path):
    registry = _Registry(tmp_path)
    registry.records.append(
        {"tenant_id": "a", "storage_id": "a-kb", "kb_id": "kb"}
    )
    _write(registry, "a-kb", "same.pdf", b"12345")
    manager = TenantQuotaManager(
        registry,
        TenantQuotaPolicy(max_documents=1, max_storage_bytes=7),
    )
    token = manager.reserve_upload(
        "a", "a-kb", registry.source_dir("a-kb"), "same.pdf", 7
    )
    with pytest.raises(TenantMutationInProgress):
        manager.reserve_upload(
            "a", "a-kb", registry.source_dir("a-kb"), "same.pdf", 6
        )
    manager.release(token)


def test_smaller_pending_replacement_does_not_lend_speculative_quota(tmp_path):
    registry = _Registry(tmp_path)
    registry.records.append(
        {"tenant_id": "a", "storage_id": "a-kb", "kb_id": "kb"}
    )
    _write(registry, "a-kb", "large.pdf", b"12345678")
    manager = TenantQuotaManager(
        registry,
        TenantQuotaPolicy(max_documents=2, max_storage_bytes=9),
    )

    replacement = manager.reserve_upload(
        "a", "a-kb", registry.source_dir("a-kb"), "large.pdf", 1
    )
    assert manager.snapshot("a")["reserved"]["storage_bytes"] == 0
    with pytest.raises(TenantQuotaExceeded):
        manager.reserve_upload(
            "a", "a-kb", registry.source_dir("a-kb"), "new.pdf", 2
        )

    source_dir = Path(registry.source_dir("a-kb"))
    (source_dir / "large.pdf").replace(
        source_dir / "large.pdf.upload-job.cogdoc-bak"
    )
    # The worker has moved the old source out of its supported extension, but
    # it can still roll back. Admission must continue counting those 8 bytes.
    assert manager.snapshot("a")["usage"]["storage_bytes"] == 8
    with pytest.raises(TenantQuotaExceeded):
        manager.reserve_upload(
            "a", "a-kb", registry.source_dir("a-kb"), "new.pdf", 2
        )

    (source_dir / "large.pdf").write_bytes(b"x")
    assert manager.snapshot("a")["usage"] == {
        "knowledge_bases": 1,
        "documents": 1,
        "storage_bytes": 8,
    }
    manager.release(replacement)


def test_backup_with_dotted_mutation_id_counts_under_original_document(tmp_path):
    registry = _Registry(tmp_path)
    registry.records.append(
        {"tenant_id": "a", "storage_id": "a-kb", "kb_id": "kb"}
    )
    source_dir = Path(registry.source_dir("a-kb"))
    source_dir.mkdir(parents=True)
    source = source_dir / "report.v1.pdf"
    source.write_bytes(b"12345678")
    source.replace(mutation_backup_path(str(source), "upload.job.segment.1"))
    manager = TenantQuotaManager(
        registry,
        TenantQuotaPolicy(max_documents=1, max_storage_bytes=8),
    )

    assert manager.snapshot("a")["usage"] == {
        "knowledge_bases": 1,
        "documents": 1,
        "storage_bytes": 8,
    }
    with pytest.raises(TenantQuotaExceeded):
        manager.reserve_upload(
            "a", "a-kb", registry.source_dir("a-kb"), "second.pdf", 1
        )


def test_knowledge_base_reservations_are_bounded(tmp_path):
    registry = _Registry(tmp_path)
    manager = TenantQuotaManager(
        registry,
        TenantQuotaPolicy(max_knowledge_bases=1),
    )
    token = manager.reserve_knowledge_base("a")
    with pytest.raises(TenantQuotaExceeded):
        manager.reserve_knowledge_base("a")
    manager.release(token)
    assert manager.reserve_knowledge_base("a")


def test_upload_does_not_treat_symlink_as_existing_document(tmp_path):
    registry = _Registry(tmp_path)
    registry.records.append(
        {"tenant_id": "a", "storage_id": "a-kb", "kb_id": "kb"}
    )
    source_dir = Path(registry.source_dir("a-kb"))
    source_dir.mkdir(parents=True)
    target = tmp_path / "outside.pdf"
    target.write_bytes(b"large-outside-file")
    (source_dir / "linked.pdf").symlink_to(target)
    manager = TenantQuotaManager(registry, TenantQuotaPolicy(max_documents=1))

    token = manager.reserve_upload("a", "a-kb", str(source_dir), "linked.pdf", 3)
    assert manager.snapshot("a")["reserved"]["documents"] == 1
    manager.release(token)


def test_unreadable_committed_entry_rejects_quota_accounting(tmp_path, monkeypatch):
    registry = _Registry(tmp_path)
    registry.records.append(
        {"tenant_id": "a", "storage_id": "a-kb", "kb_id": "kb"}
    )

    class _Entry:
        name = "unreadable.pdf"

        def is_file(self, *, follow_symlinks):
            return True

        def stat(self, *, follow_symlinks):
            raise PermissionError("denied")

    class _Entries:
        def __iter__(self):
            return iter([_Entry()])

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    monkeypatch.setattr(os, "scandir", lambda _: _Entries())
    manager = TenantQuotaManager(registry, TenantQuotaPolicy(max_documents=1))

    with pytest.raises(PermissionError, match="denied"):
        manager.snapshot("a")
