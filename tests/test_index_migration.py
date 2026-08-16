from types import SimpleNamespace

from cogdoc.service import index_migration
from cogdoc.service.index_migration import (
    IndexMigrationManager,
    IndexMigrationRunner,
    IndexMigrationStore,
)


def test_migration_continues_after_failure_and_refreshes_success(tmp_path, monkeypatch):
    monkeypatch.setattr(
        index_migration,
        "inspect_index_generation",
        lambda storage_id: {
            "storage_id": storage_id,
            "active_generation_id": "old",
            "actual_chunk_identity_version": "v6",
            "target_chunk_identity_version": "v7",
            "actual_index_build_version": "old",
            "target_index_build_version": "new",
            "needs_migration": True,
            "reasons": ["chunk_identity_version_mismatch"],
        },
    )
    refreshed = []

    def build(kb_id, _source_dir, **kwargs):
        assert kwargs["retain_previous_generation"] is True
        if kb_id == "broken":
            raise RuntimeError("boom")
        return SimpleNamespace(
            generation_id="new",
            previous_generation_id="old",
            document_count=2,
            chunk_count=8,
        )

    runner = IndexMigrationRunner(
        store=IndexMigrationStore(tmp_path),
        build=build,
        source_dir_for=lambda kb_id: f"/sources/{kb_id}",
        refresh_derived_knowledge=refreshed.append,
    )
    result = runner.run(
        [
            {"kb_id": "good", "storage_id": "good"},
            {"kb_id": "broken", "storage_id": "broken"},
        ]
    )

    assert result["status"] == "completed_with_failures"
    assert result["summary"] == {"succeeded": 1, "failed": 1}
    assert refreshed == ["good"]
    assert runner.store.load(result["run_id"])["items"][0]["generation_id"] == "new"


def test_migration_manager_returns_durable_queued_run_before_background_work(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        index_migration,
        "inspect_index_generation",
        lambda storage_id: {
            "storage_id": storage_id,
            "active_generation_id": "current",
            "actual_chunk_identity_version": "current",
            "target_chunk_identity_version": "current",
            "actual_index_build_version": "current",
            "target_index_build_version": "current",
            "needs_migration": False,
            "reasons": [],
        },
    )
    runner = IndexMigrationRunner(store=IndexMigrationStore(tmp_path))
    manager = IndexMigrationManager(runner)

    queued = manager.submit([{"kb_id": "kb", "storage_id": "kb"}])
    assert queued["status"] == "queued"
    assert queued["authorized_storage_ids"] == ["kb"]

    manager.shutdown(wait=True)
    completed = manager.get(queued["run_id"])
    assert completed["status"] == "completed"
    assert completed["summary"] == {"skipped": 1}
