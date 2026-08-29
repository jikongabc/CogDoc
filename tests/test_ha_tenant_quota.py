from __future__ import annotations

import threading
from pathlib import Path

import pytest

from cogdoc.api.tenant_quota import (
    TenantMutationInProgress,
    TenantQuotaExceeded,
    TenantQuotaPolicy,
    TenantQuotaReservationLost,
)
from cogdoc.ha.api_state import (
    DistributedKnowledgeBaseRegistry,
    DistributedMutationCoordinator,
)
from cogdoc.ha.object_store import LocalObjectStore
from cogdoc.ha.source_generation import SourceGenerationStore
from cogdoc.ha.storage import SQLiteBackend
from cogdoc.ha.tenant_quota import DistributedTenantQuotaManager


class _Clock:
    def __init__(self) -> None:
        self.value = 100.0

    def __call__(self) -> float:
        return self.value


def _cluster(tmp_path: Path, policy: TenantQuotaPolicy):
    clock = _Clock()
    first_backend = SQLiteBackend(tmp_path / "shared.db")
    second_backend = SQLiteBackend(tmp_path / "shared.db")
    first_registry = DistributedKnowledgeBaseRegistry(
        first_backend, tmp_path / "cache-a"
    )
    _second_registry = DistributedKnowledgeBaseRegistry(
        second_backend, tmp_path / "cache-b"
    )
    record = first_registry.create("docs", "tenant-a", "owner")
    objects = LocalObjectStore(tmp_path / "objects")
    first_sources = SourceGenerationStore(first_backend, objects, clock=clock)
    second_sources = SourceGenerationStore(second_backend, objects, clock=clock)
    first = DistributedTenantQuotaManager(
        first_backend,
        first_sources,
        policy,
        owner_id="api-a",
        lease_seconds=5,
        clock=clock,
    )
    second = DistributedTenantQuotaManager(
        second_backend,
        second_sources,
        policy,
        owner_id="api-b",
        lease_seconds=5,
        clock=clock,
    )
    return clock, record, first_registry, first_sources, first, second


def test_cross_node_upload_reservations_enforce_one_cluster_limit(
    tmp_path: Path,
) -> None:
    _clock, record, registry, _sources, first, second = _cluster(
        tmp_path, TenantQuotaPolicy(max_documents=1, max_storage_bytes=10)
    )
    storage_id = str(record["storage_id"])
    token = first.reserve_upload(
        "tenant-a", storage_id, registry.source_dir(storage_id), "a.md", 6
    )

    with pytest.raises(TenantQuotaExceeded):
        second.reserve_upload(
            "tenant-a", storage_id, registry.source_dir(storage_id), "b.md", 5
        )
    with pytest.raises(TenantMutationInProgress):
        second.reserve_upload(
            "tenant-a", storage_id, registry.source_dir(storage_id), "a.md", 1
        )

    first.release(token)
    second.release(
        second.reserve_upload(
            "tenant-a", storage_id, registry.source_dir(storage_id), "b.md", 5
        )
    )


def test_expired_crashed_node_reservation_is_reclaimed(tmp_path: Path) -> None:
    clock, record, registry, _sources, first, second = _cluster(
        tmp_path, TenantQuotaPolicy(max_documents=1)
    )
    storage_id = str(record["storage_id"])
    first.reserve_upload(
        "tenant-a", storage_id, registry.source_dir(storage_id), "a.md", 1
    )
    clock.value += 6

    token = second.reserve_upload(
        "tenant-a", storage_id, registry.source_dir(storage_id), "b.md", 1
    )
    assert second.snapshot("tenant-a")["reserved"]["documents"] == 1
    second.release(token)


def test_heartbeat_keeps_live_reservation_and_release_is_owner_scoped(
    tmp_path: Path,
) -> None:
    clock, record, registry, _sources, first, second = _cluster(
        tmp_path, TenantQuotaPolicy(max_documents=1)
    )
    storage_id = str(record["storage_id"])
    token = first.reserve_upload(
        "tenant-a", storage_id, registry.source_dir(storage_id), "a.md", 1
    )
    second.release(token)
    clock.value += 4
    assert first.heartbeat() == 1
    clock.value += 4

    with pytest.raises(TenantQuotaExceeded):
        second.reserve_upload(
            "tenant-a", storage_id, registry.source_dir(storage_id), "b.md", 1
        )
    first.release(token)


def test_expired_reservation_cannot_publish_or_heartbeat(tmp_path: Path) -> None:
    clock, record, registry, _sources, first, _second = _cluster(
        tmp_path, TenantQuotaPolicy(max_documents=1)
    )
    storage_id = str(record["storage_id"])
    token = first.reserve_upload(
        "tenant-a", storage_id, registry.source_dir(storage_id), "a.md", 1
    )
    clock.value += 6

    with pytest.raises(TenantQuotaReservationLost, match="stale or expired"):
        first.assert_live(token)

    # Once ownership is lost the heartbeat must report it, rather than claim
    # that the asynchronous mutation is still protected by quota.
    second_token = first.reserve_upload(
        "tenant-a", storage_id, registry.source_dir(storage_id), "b.md", 1
    )
    clock.value += 6
    with pytest.raises(TenantQuotaReservationLost, match="expired"):
        first.heartbeat()
    first.release(second_token)


def test_published_source_usage_and_overwrite_delta_are_cluster_authoritative(
    tmp_path: Path,
) -> None:
    _clock, record, registry, sources, first, second = _cluster(
        tmp_path, TenantQuotaPolicy(max_documents=2, max_storage_bytes=10)
    )
    storage_id = str(record["storage_id"])
    source_dir = Path(registry.source_dir(storage_id))
    source_dir.mkdir(parents=True, exist_ok=True)
    (source_dir / "a.md").write_bytes(b"123456")
    (source_dir / ".cogdoc-source-contracts.json").write_bytes(b"internal")
    (source_dir / ".connections").mkdir()
    (source_dir / ".connections" / "duplicate.md").write_bytes(b"internal-copy")
    coordinator = DistributedMutationCoordinator(
        registry.backend, registry, owner_id="writer", lease_seconds=30
    )
    lease = coordinator.acquire(storage_id)
    candidate = sources.stage_directory(
        tenant_id="tenant-a",
        storage_id=storage_id,
        source_dir=source_dir,
        lease=lease,
    )
    sources.publish(str(candidate["generation_id"]), lease)

    snapshot = second.snapshot("tenant-a")
    assert snapshot["usage"] == {
        "knowledge_bases": 1,
        "documents": 1,
        "storage_bytes": 6,
    }
    overwrite = first.reserve_upload("tenant-a", storage_id, str(source_dir), "a.md", 4)
    addition = second.reserve_upload("tenant-a", storage_id, str(source_dir), "b.md", 4)
    assert second.snapshot("tenant-a")["reserved"] == {
        "knowledge_bases": 0,
        "documents": 1,
        "storage_bytes": 4,
    }
    first.release(overwrite)
    second.release(addition)


def test_concurrent_kb_reservations_are_serialized_per_tenant(tmp_path: Path) -> None:
    _clock, _record, _registry, _sources, first, second = _cluster(
        tmp_path, TenantQuotaPolicy(max_knowledge_bases=2)
    )
    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def reserve(manager: DistributedTenantQuotaManager) -> None:
        barrier.wait()
        try:
            manager.reserve_knowledge_base("tenant-a")
        except TenantQuotaExceeded:
            outcomes.append("rejected")
        else:
            outcomes.append("reserved")

    threads = [
        threading.Thread(target=reserve, args=(first,)),
        threading.Thread(target=reserve, args=(second,)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert sorted(outcomes) == ["rejected", "reserved"]


def test_distributed_connector_snapshot_reserves_cluster_growth(tmp_path: Path) -> None:
    _clock, record, registry, sources, first, second = _cluster(
        tmp_path, TenantQuotaPolicy(max_documents=2, max_storage_bytes=10)
    )
    storage_id = str(record["storage_id"])
    source_dir = Path(registry.source_dir(storage_id))
    source_dir.mkdir(parents=True, exist_ok=True)
    (source_dir / "existing.md").write_bytes(b"1234")
    coordinator = DistributedMutationCoordinator(
        registry.backend, registry, owner_id="writer", lease_seconds=30
    )
    lease = coordinator.acquire(storage_id)
    candidate = sources.stage_directory(
        tenant_id="tenant-a",
        storage_id=storage_id,
        source_dir=source_dir,
        lease=lease,
    )
    sources.publish(str(candidate["generation_id"]), lease)

    baseline = tmp_path / "baseline"
    proposed = tmp_path / "proposed"
    baseline.mkdir()
    proposed.mkdir()
    (proposed / "connector.md").write_bytes(b"12345")
    token = first.reserve_connector_snapshot(
        "tenant-a",
        storage_id,
        str(source_dir),
        str(baseline),
        str(proposed),
        "sync-1",
    )
    assert second.snapshot("tenant-a")["reserved"] == {
        "knowledge_bases": 0,
        "documents": 1,
        "storage_bytes": 5,
    }
    with pytest.raises(TenantQuotaExceeded):
        second.reserve_connector_snapshot(
            "tenant-a",
            storage_id,
            str(source_dir),
            str(baseline),
            str(proposed),
            "sync-2",
        )
    first.release(token)
