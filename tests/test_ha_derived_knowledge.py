from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from cogdoc.ha.derived_knowledge import DistributedDerivedKnowledgeStore
from cogdoc.ha.api_state import DistributedKnowledgeBaseRegistry
from cogdoc.ha.feedback import HA_KB_EPOCH_FIELD, StaleAuxiliaryWrite
from cogdoc.ha.storage import SQLiteBackend
from cogdoc.api.tenancy import Permission


def _payload(text: str = "The policy is active"):
    return {
        "kb_id": "storage-kb",
        "text": text,
        "status": "pending",
        "created_by": "reviewer",
        "related_source": "guide.md",
        "related_source_sha256": "old-hash",
    }


def test_cross_node_create_deduplicates_and_serializes_conflicts(tmp_path):
    path = tmp_path / "knowledge.db"
    first = DistributedDerivedKnowledgeStore(SQLiteBackend(path))
    second = DistributedDerivedKnowledgeStore(SQLiteBackend(path))
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(lambda store: store.create(_payload()), (first, second))
        )

    assert len({row[0]["knowledge_id"] for row in results}) == 1
    assert sorted(row[1] for row in results) == [False, True]
    assert len(second.list(kb_id="storage-kb")) == 1


def test_review_revision_history_and_stale_transition_are_shared(tmp_path):
    path = tmp_path / "review.db"
    first = DistributedDerivedKnowledgeStore(SQLiteBackend(path))
    second = DistributedDerivedKnowledgeStore(SQLiteBackend(path))
    row, _deduplicated = first.create(_payload())
    approved = second.set_status(row["knowledge_id"], "approved", actor="admin")
    assert approved is not None and approved["status"] == "approved"
    before = first.revision_token()

    stale = first.mark_stale_for_source("storage-kb", "guide.md", "old-hash")

    assert [item["knowledge_id"] for item in stale] == [row["knowledge_id"]]
    assert second.get(row["knowledge_id"])["status"] == "stale"
    assert first.revision_token() != before
    assert len(first.export_records(kb_id="storage-kb")) == 3


def test_import_is_idempotent_and_scope_clear_isolated(tmp_path):
    store = DistributedDerivedKnowledgeStore(SQLiteBackend(tmp_path / "import.db"))
    store.create(_payload())
    store.create({**_payload("Other"), "kb_id": "other-kb"})
    records = store.export_records(kb_id="storage-kb")
    assert store.import_records(records) == {"imported": 1, "skipped": 0}
    assert store.import_records(records) == {"imported": 0, "skipped": 1}

    store.clear_kb("storage-kb")

    assert store.list(kb_id="storage-kb") == []
    assert len(store.list(kb_id="other-kb")) == 1


def test_knowledge_create_is_fenced_by_kb_incarnation(tmp_path):
    backend = SQLiteBackend(tmp_path / "epoch.db")
    registry = DistributedKnowledgeBaseRegistry(backend, tmp_path / "cache")
    storage_id = str(registry.create("docs", "tenant", "owner")["storage_id"])
    store = DistributedDerivedKnowledgeStore(backend)
    payload = {
        **_payload(),
        "kb_id": storage_id,
        HA_KB_EPOCH_FIELD: registry.current(storage_id),
    }
    registry.bump(storage_id)

    with pytest.raises(StaleAuxiliaryWrite, match="incarnation"):
        store.create(payload)

    assert store.list(kb_id=storage_id) == []


def test_authorized_mutations_check_live_authority_inside_shared_transaction(tmp_path):
    backend = SQLiteBackend(tmp_path / "authority.db")
    registry = DistributedKnowledgeBaseRegistry(backend, tmp_path / "cache")
    record = registry.create("docs", "tenant", "owner")
    storage_id = str(record["storage_id"])
    epoch = int(record["epoch"])
    store = DistributedDerivedKnowledgeStore(backend)
    calls: list[Permission] = []

    def checker(connection, authority, *, required_permission):
        assert connection is store._active_connection
        assert authority == {"proof": "live"}
        calls.append(required_permission)

    store.bind_authority_checker(checker)
    row, _ = store.create_authorized(
        {**_payload(), "kb_id": storage_id},
        expected_epoch=epoch,
        authority={"proof": "live"},
    )
    snapshot = store.authority_snapshot(row["knowledge_id"])
    assert snapshot is not None
    approved = store.set_status_authorized(
        row["knowledge_id"],
        "approved",
        expected_epoch=epoch,
        expected_event_sequence=snapshot[1],
        authority={"proof": "live"},
        actor="reviewer",
    )
    assert approved is not None and approved["status"] == "approved"
    assert calls == [Permission.WRITE, Permission.REVIEW]

    registry.bump(storage_id)
    with pytest.raises(StaleAuxiliaryWrite, match="incarnation"):
        store.delete_authorized(
            row["knowledge_id"],
            expected_epoch=epoch,
            expected_event_sequence=store.authority_snapshot(row["knowledge_id"])[1],
            authority={"proof": "live"},
        )
    assert store.get(row["knowledge_id"]) is not None


def test_authority_rejection_rolls_back_knowledge_write(tmp_path):
    backend = SQLiteBackend(tmp_path / "revoked.db")
    registry = DistributedKnowledgeBaseRegistry(backend, tmp_path / "cache")
    record = registry.create("docs", "tenant", "owner")
    store = DistributedDerivedKnowledgeStore(backend)

    def revoked(*_args, **_kwargs):
        raise PermissionError("revoked")

    store.bind_authority_checker(revoked)
    with pytest.raises(StaleAuxiliaryWrite, match="authority is stale"):
        store.create_authorized(
            {**_payload(), "kb_id": record["storage_id"]},
            expected_epoch=int(record["epoch"]),
            authority={"proof": "stale"},
        )
    assert store.list(kb_id=str(record["storage_id"])) == []


def test_refresh_outbox_is_transactional_coalesced_and_cross_node_fenced(tmp_path):
    now = [100.0]
    path = tmp_path / "refresh-outbox.db"
    backend = SQLiteBackend(path)
    registry = DistributedKnowledgeBaseRegistry(backend, tmp_path / "cache")
    record = registry.create("docs", "tenant", "owner")
    storage_id = str(record["storage_id"])
    first = DistributedDerivedKnowledgeStore(backend, clock=lambda: now[0])
    second = DistributedDerivedKnowledgeStore(SQLiteBackend(path), clock=lambda: now[0])

    row, _ = first.create({**_payload(), "kb_id": storage_id})
    pending = first.pending_refreshes()
    assert [item["kb_id"] for item in pending] == [storage_id]
    first_sequence = int(pending[0]["requested_sequence"])

    claim = first.claim_refresh(storage_id, "node-a", lease_seconds=10)
    assert claim is not None
    assert second.claim_refresh(storage_id, "node-b", lease_seconds=10) is None

    first.set_status(row["knowledge_id"], "approved", actor="reviewer")
    assert first.complete_refresh(
        storage_id,
        str(claim["lease_token"]),
        first_sequence,
    )
    queued = first.pending_refreshes()
    assert len(queued) == 1
    assert int(queued[0]["requested_sequence"]) > first_sequence

    next_claim = second.claim_refresh(storage_id, "node-b", lease_seconds=10)
    assert next_claim is not None
    assert second.complete_refresh(
        storage_id,
        str(next_claim["lease_token"]),
        int(next_claim["requested_sequence"]),
    )
    assert first.pending_refreshes() == []


def test_expired_refresh_lease_is_recoverable_and_stale_owner_is_fenced(tmp_path):
    now = [200.0]
    backend = SQLiteBackend(tmp_path / "refresh-expiry.db")
    registry = DistributedKnowledgeBaseRegistry(backend, tmp_path / "cache")
    storage_id = str(registry.create("docs", "tenant", "owner")["storage_id"])
    store = DistributedDerivedKnowledgeStore(backend, clock=lambda: now[0])
    store.create({**_payload(), "kb_id": storage_id})
    old = store.claim_refresh(storage_id, "old-node", lease_seconds=5)
    assert old is not None

    now[0] += 6
    assert [row["kb_id"] for row in store.pending_refreshes()] == [storage_id]
    replacement = store.claim_refresh(storage_id, "new-node", lease_seconds=5)
    assert replacement is not None
    assert not store.complete_refresh(
        storage_id,
        str(old["lease_token"]),
        int(old["requested_sequence"]),
    )
    assert store.complete_refresh(
        storage_id,
        str(replacement["lease_token"]),
        int(replacement["requested_sequence"]),
    )


def test_authorized_review_rejects_row_rebinding_after_outer_acl_check(tmp_path):
    backend = SQLiteBackend(tmp_path / "row-cas.db")
    registry = DistributedKnowledgeBaseRegistry(backend, tmp_path / "cache")
    record = registry.create("docs", "tenant", "owner")
    storage_id = str(record["storage_id"])
    store = DistributedDerivedKnowledgeStore(backend)
    store.bind_authority_checker(lambda *_args, **_kwargs: None)
    row, _ = store.create(
        {
            **_payload(),
            "kb_id": storage_id,
            "related_document_id": "doc-a",
        }
    )
    frozen = store.authority_snapshot(row["knowledge_id"])
    assert frozen is not None

    rebound = store.set_status(
        row["knowledge_id"],
        "pending",
        binding_updates={"related_document_id": "doc-b"},
    )
    assert rebound is not None and rebound["related_document_id"] == "doc-b"

    with pytest.raises(StaleAuxiliaryWrite, match="row changed"):
        store.set_status_authorized(
            row["knowledge_id"],
            "rejected",
            expected_epoch=int(record["epoch"]),
            expected_event_sequence=frozen[1],
            authority={"proof": "formerly-authorized"},
        )
    current = store.get(row["knowledge_id"])
    assert current is not None
    assert current["status"] == "pending"
    assert current["related_document_id"] == "doc-b"
