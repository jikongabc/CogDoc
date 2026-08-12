import os
from pathlib import Path

import pytest

from cogdoc.api.tenant_quota import (
    TenantMutationInProgress,
    TenantQuotaExceeded,
    TenantQuotaManager,
    TenantQuotaPolicy,
)


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
    manager.release(replacement)


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
