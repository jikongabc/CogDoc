from __future__ import annotations

import json
import time

from cogdoc.api.audit import AuditStore
from cogdoc.api.audit_exports import (
    AuditExportConflict,
    AuditExportManager,
    AuditExportStore,
)


def _record(store: AuditStore, tenant: str, action: str, status: int = 200) -> None:
    store.record(
        tenant,
        "actor",
        action,
        "GET",
        "/v1/test",
        status,
        {"kind": "test"},
        {"outcome": "allowed"},
        None,
    )


def _wait(store: AuditExportStore, job_id: str, tenant: str) -> dict:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        row = store.get(job_id, tenant)
        assert row is not None
        if row["status"] in {"succeeded", "failed"}:
            return row
        time.sleep(0.01)
    raise AssertionError("export did not finish")


def test_export_is_tenant_filtered_integrity_checked_and_revision_deleted(tmp_path):
    audit = AuditStore(tmp_path / "audit.jsonl")
    _record(audit, "a", "document.read")
    _record(audit, "b", "secret.read")
    _record(audit, "a", "document.write", 201)
    store = AuditExportStore(tmp_path / "state.db", tmp_path / "exports")
    manager = AuditExportManager(store, audit)
    try:
        created = manager.submit(
            tenant_id="a",
            actor_id="owner-a",
            actions=["document.write"],
            statuses=[201],
        )
        completed = _wait(store, created["job_id"], "a")
        assert completed["event_count"] == 1
        path = store.artifact_path(created["job_id"], "a")
        assert path.stat().st_mode & 0o777 == 0o600
        records = [json.loads(line) for line in path.read_text().splitlines()]
        assert records[0]["record_type"] == "manifest"
        assert records[0]["tenant_id"] == "a"
        assert records[1]["action"] == "document.write"
        assert "secret.read" not in path.read_text()
        assert store.get(created["job_id"], "b") is None
        try:
            store.delete(created["job_id"], "a", expected_revision=1)
        except AuditExportConflict:
            pass
        else:
            raise AssertionError("stale revision must fail")
        assert store.delete(
            created["job_id"], "a", expected_revision=completed["revision"]
        )
        assert not path.exists()
    finally:
        manager.shutdown()
        store.close()


def test_recover_reuses_durable_job_and_tampering_fails_closed(tmp_path):
    audit = AuditStore(tmp_path / "audit.jsonl")
    _record(audit, "a", "read")
    store = AuditExportStore(tmp_path / "state.db", tmp_path / "exports")
    pending = store.create("a", "owner")
    manager = AuditExportManager(store, audit)
    try:
        manager.recover()
        completed = _wait(store, pending["job_id"], "a")
        path = store.artifact_path(pending["job_id"], "a")
        path.write_bytes(path.read_bytes() + b"tampered")
        try:
            store.artifact_path(pending["job_id"], "a")
        except Exception as exc:
            assert "integrity" in str(exc)
        else:
            raise AssertionError("tampered export must fail closed")
        assert completed["chain_head"]
    finally:
        manager.shutdown()
        store.close()


def test_startup_scavenges_private_temporary_artifacts(tmp_path):
    root = tmp_path / "exports"
    root.mkdir()
    stale = root / ".audit-export-dead.abc.tmp"
    stale.write_text("partial")
    store = AuditExportStore(tmp_path / "state.db", root)
    try:
        assert not stale.exists()
    finally:
        store.close()


def test_manager_pushes_filters_into_verified_snapshot(tmp_path, monkeypatch):
    audit = AuditStore(tmp_path / "audit.jsonl")
    _record(audit, "a", "document.read")
    _record(audit, "a", "document.write", 201)
    store = AuditExportStore(tmp_path / "state.db", tmp_path / "exports")
    manager = AuditExportManager(store, audit)
    calls = []
    original = audit.snapshot

    def snapshot(*args, **kwargs):
        calls.append(kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(audit, "snapshot", snapshot)
    try:
        created = manager.submit(
            tenant_id="a",
            actor_id="owner-a",
            actions=["document.write"],
            statuses=[201],
        )
        completed = _wait(store, created["job_id"], "a")
        assert completed["event_count"] == 1
        assert calls == [
            {
                "from_sequence": None,
                "to_sequence": None,
                "actions": ("document.write",),
                "statuses": (201,),
            }
        ]
    finally:
        manager.shutdown()
        store.close()
