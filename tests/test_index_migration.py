import sys
from types import SimpleNamespace

from scripts import migrate_v7_indexes

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


def test_migration_cli_uses_runtime_knowledge_store(monkeypatch, capsys):
    knowledge_store = object()
    refresh = object()
    captured = {}

    class Registry:
        def list(self):
            return [{"kb_id": "kb", "storage_id": "storage"}]

    class Runner:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def plan(self, records):
            assert records == [{"kb_id": "kb", "storage_id": "storage"}]
            return {"status": "completed", "items": []}

    monkeypatch.setattr(migrate_v7_indexes, "KnowledgeBaseRegistry", Registry)
    monkeypatch.setattr(
        migrate_v7_indexes,
        "default_state_runtime",
        lambda: SimpleNamespace(
            knowledge_store=knowledge_store,
            refresh_derived_knowledge_index=refresh,
        ),
    )
    monkeypatch.setattr(migrate_v7_indexes, "IndexMigrationRunner", Runner)
    monkeypatch.setattr(sys, "argv", ["migrate_v7_indexes.py", "scan"])

    assert migrate_v7_indexes.main() == 0
    assert captured == {
        "knowledge_store": knowledge_store,
        "refresh_derived_knowledge": refresh,
    }
    assert '"status": "completed"' in capsys.readouterr().out


def test_rollback_restores_manifest_contract_and_reverts_on_manifest_failure(
    tmp_path, monkeypatch
):
    active_id = {"value": "new"}
    generations = {
        "old": {
            "id": "old",
            "documents": [{"name": "paper.pdf", "sha256": "old-sha"}],
            "chunk_identity_version": "v6",
            "index_build_version": "build-v6",
        },
        "new": {
            "id": "new",
            "documents": [{"name": "paper.pdf", "sha256": "old-sha"}],
            "chunk_identity_version": "v7",
            "index_build_version": "build-v7",
        },
    }

    class State:
        def __init__(self, storage_id):
            assert storage_id == "storage"

        def rollback_active(
            self,
            generation_id,
            *,
            expected_current_id=None,
            protect_replaced=False,
        ):
            assert active_id["value"] == expected_current_id
            replaced = active_id["value"]
            active_id["value"] = generation_id
            return replaced

        def active(self):
            return generations[active_id["value"]]

    monkeypatch.setattr(index_migration, "KBState", State)
    monkeypatch.setattr(index_migration, "kb_write_lock", lambda _storage_id: _null_context())
    monkeypatch.setattr(index_migration.RetrieverFactory, "invalidate", lambda _storage_id: None)
    store = IndexMigrationStore(tmp_path)
    run = {
        "run_id": "a" * 32,
        "items": [
            {
                "storage_id": "storage",
                "status": "succeeded",
                "generation_id": "new",
                "previous_generation_id": "old",
            }
        ],
    }
    store.save(run)
    manifests = []
    runner = IndexMigrationRunner(store=store, save_manifest=manifests.append)

    result = runner.rollback(run["run_id"])
    assert active_id["value"] == "old"
    assert manifests == [
        {
            "doc_id": "storage",
            "documents": [{"name": "paper.pdf", "sha256": "old-sha"}],
            "chunk_identity_version": "v6",
            "index_build_version": "build-v6",
        }
    ]
    assert result["items"][0]["status"] == "rolled_back"

    active_id["value"] = "new"
    run["items"][0]["status"] = "succeeded"
    store.save(run)

    def fail_manifest(_manifest):
        raise OSError("disk full")

    failed = IndexMigrationRunner(store=store, save_manifest=fail_manifest).rollback(
        run["run_id"]
    )
    assert active_id["value"] == "new"
    assert "OSError: disk full" in failed["items"][0]["rollback_error"]


class _null_context:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False
