import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor

import pytest

from cogdoc.api.audit import (
    GENESIS_HASH,
    AuditCorruptionError,
    AuditIntegrityError,
    AuditStore,
)


def _record(
    store: AuditStore,
    *,
    tenant: str = "tenant-a",
    index: int = 1,
):
    return store.record(
        tenant=tenant,
        principal="user-1",
        action="knowledge_base.read",
        method="get",
        path=f"/v1/knowledge-bases/kb-{index}",
        status=200,
        resource={"type": "knowledge_base", "id": f"kb-{index}"},
        result={"outcome": "allowed"},
        request_id=f"req-{tenant}-{index}",
    )


def _hash_without_event_hash(event: dict) -> str:
    unsigned = {key: value for key, value in event.items() if key != "event_hash"}
    payload = json.dumps(
        unsigned,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def test_audit_store_builds_and_restores_independent_tenant_hash_chains(tmp_path):
    path = tmp_path / "audit.jsonl"
    store = AuditStore(path)

    a1 = _record(store, tenant="tenant-a", index=1)
    b1 = _record(store, tenant="tenant-b", index=1)
    a2 = _record(store, tenant="tenant-a", index=2)

    assert a1["sequence"] == 1
    assert b1["sequence"] == 1
    assert a2["sequence"] == 2
    assert a1["prev_hash"] == GENESIS_HASH
    assert b1["prev_hash"] == GENESIS_HASH
    assert a2["prev_hash"] == a1["event_hash"]
    assert all(
        event["event_hash"] == _hash_without_event_hash(event)
        for event in (a1, b1, a2)
    )
    assert store.verify() is True

    reopened = AuditStore(path)
    a3 = _record(reopened, tenant="tenant-a", index=3)
    assert a3["sequence"] == 3
    assert a3["prev_hash"] == a2["event_hash"]


def test_audit_list_never_crosses_tenant_boundary(tmp_path):
    store = AuditStore(tmp_path / "audit.jsonl")
    _record(store, tenant="tenant-a", index=1)
    _record(store, tenant="tenant-b", index=1)
    _record(store, tenant="tenant-a", index=2)

    tenant_a = store.list("tenant-a")
    tenant_b = store.list("tenant-b")

    assert [event["sequence"] for event in tenant_a] == [2, 1]
    assert [event["tenant"] for event in tenant_a] == ["tenant-a", "tenant-a"]
    assert [event["sequence"] for event in tenant_b] == [1]
    assert tenant_b[0]["tenant"] == "tenant-b"


def test_audit_store_detects_tampering_live_and_on_startup(tmp_path):
    path = tmp_path / "audit.jsonl"
    store = AuditStore(path)
    _record(store, index=1)
    _record(store, index=2)

    lines = path.read_text(encoding="utf-8").splitlines()
    first = json.loads(lines[0])
    first["result"]["outcome"] = "denied"
    lines[0] = json.dumps(first, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(AuditIntegrityError, match="hash"):
        store.verify()
    with pytest.raises(AuditIntegrityError, match="hash"):
        AuditStore(path)


def test_audit_check_detects_same_size_rewrite_even_when_mtime_is_restored(tmp_path):
    path = tmp_path / "audit.jsonl"
    store = AuditStore(path)
    _record(store)
    before = path.stat()
    payload = path.read_bytes()
    tampered = payload.replace(b'"outcome":"allowed"', b'"outcome":"altered"')
    assert len(tampered) == len(payload) and tampered != payload
    path.write_bytes(tampered)
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))

    with pytest.raises(AuditIntegrityError, match="hash"):
        store.check()


def test_audit_store_rejects_truncated_record_instead_of_ignoring_it(tmp_path):
    path = tmp_path / "audit.jsonl"
    store = AuditStore(path)
    _record(store)
    raw = path.read_bytes()
    path.write_bytes(raw[:-4])

    with pytest.raises(AuditCorruptionError, match="truncated"):
        AuditStore(path)


def test_audit_store_rejects_live_history_rollback(tmp_path):
    path = tmp_path / "audit.jsonl"
    store = AuditStore(path)
    _record(store, index=1)
    first_record = path.read_bytes()
    _record(store, index=2)
    path.write_bytes(first_record)

    with pytest.raises(AuditCorruptionError, match="truncated or.*replaced"):
        store.verify()


def test_audit_list_uses_exclusive_tenant_sequence_cursor(tmp_path):
    store = AuditStore(tmp_path / "audit.jsonl")
    for index in range(1, 7):
        _record(store, index=index)

    first_page = store.list("tenant-a", limit=2)
    second_page = store.list(
        "tenant-a", limit=2, before_sequence=first_page[-1]["sequence"]
    )
    third_page = store.list(
        "tenant-a", limit=2, before_sequence=second_page[-1]["sequence"]
    )

    assert [event["sequence"] for event in first_page] == [6, 5]
    assert [event["sequence"] for event in second_page] == [4, 3]
    assert [event["sequence"] for event in third_page] == [2, 1]


def test_audit_record_is_thread_safe_and_sequences_remain_contiguous(tmp_path):
    path = tmp_path / "audit.jsonl"
    store = AuditStore(path)
    total = 120

    def append(index: int):
        return _record(store, index=index)

    with ThreadPoolExecutor(max_workers=12) as executor:
        events = list(executor.map(append, range(1, total + 1)))

    assert sorted(event["sequence"] for event in events) == list(
        range(1, total + 1)
    )
    persisted = store.list("tenant-a", limit=total)
    assert [event["sequence"] for event in persisted] == list(
        range(total, 0, -1)
    )
    assert len(path.read_bytes().splitlines()) == total
    assert AuditStore(path).verify() is True


def test_audit_store_rejects_request_bodies_credentials_and_query_strings(tmp_path):
    path = tmp_path / "audit.jsonl"
    store = AuditStore(path)

    with pytest.raises(ValueError, match="bodies or credentials"):
        store.record(
            "tenant-a",
            "user-1",
            "knowledge_base.create",
            "POST",
            "/v1/knowledge-bases",
            201,
            {"type": "knowledge_base"},
            {"body": {"kb_id": "private"}},
            "req-1",
        )
    with pytest.raises(ValueError, match="bodies or credentials"):
        store.record(
            "tenant-a",
            "user-1",
            "knowledge_base.read",
            "GET",
            "/v1/knowledge-bases/kb",
            200,
            {"authorization": "Bearer raw-api-key"},
            "allowed",
            "req-2",
        )
    with pytest.raises(ValueError, match="query strings"):
        store.record(
            "tenant-a",
            "user-1",
            "knowledge_base.read",
            "GET",
            "/v1/knowledge-bases?api_key=raw-api-key",
            200,
            "knowledge_base:kb",
            "allowed",
            "req-3",
        )

    _record(store)
    persisted = path.read_text(encoding="utf-8")
    assert "raw-api-key" not in persisted
    assert '"body"' not in persisted


def test_audit_append_flushes_and_fsyncs_before_return(tmp_path, monkeypatch):
    import cogdoc.api.audit as audit_module

    calls = []
    real_fsync = audit_module.os.fsync

    def tracking_fsync(descriptor):
        calls.append(descriptor)
        return real_fsync(descriptor)

    monkeypatch.setattr(audit_module.os, "fsync", tracking_fsync)
    store = AuditStore(tmp_path / "audit.jsonl")

    event = _record(store)

    assert event["sequence"] == 1
    # One fsync makes the line durable; a second persists first-file creation
    # in the parent directory.
    assert len(calls) >= 2
