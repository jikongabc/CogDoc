from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from cogdoc.ha.backup import HABackupCoordinator, HABackupError
from cogdoc.ha.index_generation import IndexGenerationStore
from cogdoc.ha.migration_catalog import REGISTERED_MIGRATIONS
from cogdoc.ha.migrations import MigrationRunner
from cogdoc.ha.object_store import LocalObjectStore
from cogdoc.ha.outbox import OutboxStore
from cogdoc.ha.recovery import HARecoveryManifest, RecoveryManifestError
from cogdoc.ha.scheduler import ScheduleStore
from cogdoc.ha.source_generation import SourceGenerationStore
from cogdoc.ha.storage import SQLiteBackend
from cogdoc.ha.tasks import LeaseJobStore


class _SnapshotBackend:
    kind = "sqlite"
    dsn = "postgresql://backup-user:secret-marker@database.example/cogdoc"
    schema = "cogdoc"

    def __init__(self, path: Path) -> None:
        self._backend = SQLiteBackend(path)
        self.snapshot_open = False
        self.requested_timeout: float | None = None

    def transaction(self, *, write: bool = False):
        return self._backend.transaction(write=write)

    def check(self) -> bool:
        return self._backend.check()

    def close(self) -> None:
        self._backend.close()

    def reopen(self) -> None:
        self._backend.reopen()

    def sql(self, *, sqlite: str, postgres: str) -> str:
        return self._backend.sql(sqlite=sqlite, postgres=postgres)

    @contextmanager
    def exported_snapshot(self, *, statement_timeout_seconds: float = 3600.0):
        self.requested_timeout = statement_timeout_seconds
        with self._backend.transaction() as connection:
            self.snapshot_open = True
            try:
                yield connection, "00000003-0000001B-1"
            finally:
                self.snapshot_open = False


def _coordinator(tmp_path: Path, runner):
    backend = _SnapshotBackend(tmp_path / "state.db")
    IndexGenerationStore(backend)
    LeaseJobStore(backend)
    OutboxStore(backend)
    ScheduleStore(backend)
    with backend.transaction(write=True) as connection:
        for statement in REGISTERED_MIGRATIONS[1].expand:
            connection.execute(statement)
    MigrationRunner(backend, REGISTERED_MIGRATIONS, owner_id="migration").run()
    objects = LocalObjectStore(tmp_path / "objects")
    sources = SourceGenerationStore(backend, objects)
    recovery = HARecoveryManifest(backend, objects, sources, clock=lambda: 20.0)
    return backend, HABackupCoordinator(
        backend, recovery, runner=runner, clock=lambda: 21.0
    )


def test_backup_uses_one_exported_snapshot_and_publishes_atomic_bundle(
    tmp_path: Path,
) -> None:
    calls = []
    holder: dict[str, Any] = {}

    def runner(command, **kwargs):
        backend = holder["backend"]
        assert kwargs["timeout"] == 600.0
        if command[0] == "pg_restore":
            assert command[1] == "--list"
            calls.append((command, kwargs))
            return SimpleNamespace(returncode=0, stderr=b"")
        assert backend.snapshot_open is True
        assert backend.dsn not in command
        assert kwargs["env"]["PGDATABASE"] == backend.dsn
        dump = Path(
            next(arg.split("=", 1)[1] for arg in command if arg.startswith("--file="))
        )
        dump.write_bytes(b"postgres custom dump")
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stderr=b"")

    backend, coordinator = _coordinator(tmp_path, runner)
    holder["backend"] = backend

    created = coordinator.create(
        tmp_path / "backups",
        name="recovery-20260822",
        timeout_seconds=600,
    )

    path = Path(created["path"])
    assert path.is_dir()
    assert sorted(item.name for item in path.iterdir()) == [
        "bundle.json",
        "database.dump",
        "recovery-manifest.json",
    ]
    command = calls[0][0]
    assert command[0] == "pg_dump"
    assert "--format=custom" in command
    assert "--snapshot=00000003-0000001B-1" in command
    assert "--schema=cogdoc" in command
    assert "--no-owner" in command
    assert "--no-privileges" in command
    assert all("secret-marker" not in argument for argument in command)
    assert calls[1][0][0:2] == ["pg_restore", "--list"]
    assert backend.requested_timeout == 600.0
    verified = coordinator.verify(path, timeout_seconds=600)
    assert verified["status"] == "verified"
    assert verified["objects"] == 0
    assert verified["database_bytes"] == len(b"postgres custom dump")
    assert not list(path.parent.glob(".recovery-20260822.*"))
    backend.close()


def test_backup_failure_removes_staging_and_never_publishes(
    tmp_path: Path,
) -> None:
    def runner(command, **_kwargs):
        if command[0] == "pg_restore":
            return SimpleNamespace(returncode=0, stderr=b"")
        dump = Path(
            next(arg.split("=", 1)[1] for arg in command if arg.startswith("--file="))
        )
        dump.write_bytes(b"partial")
        return SimpleNamespace(returncode=2, stderr=b"secret-marker")

    backend, coordinator = _coordinator(tmp_path, runner)
    output = tmp_path / "backups"

    with pytest.raises(HABackupError, match="pg_dump failed"):
        coordinator.create(output, name="failed", timeout_seconds=60)

    assert not (output / "failed").exists()
    assert list(output.iterdir()) == []
    backend.close()


def test_backup_rejects_invalid_pg_restore_archive_before_publish(
    tmp_path: Path,
) -> None:
    def runner(command, **_kwargs):
        if command[0] == "pg_restore":
            return SimpleNamespace(returncode=1)
        dump = Path(
            next(arg.split("=", 1)[1] for arg in command if arg.startswith("--file="))
        )
        dump.write_bytes(b"not a custom archive")
        return SimpleNamespace(returncode=0)

    backend, coordinator = _coordinator(tmp_path, runner)
    output = tmp_path / "backups"
    with pytest.raises(HABackupError, match="archive is invalid"):
        coordinator.create(output, name="invalid")
    assert list(output.iterdir()) == []
    backend.close()


def test_backup_rejects_incomplete_knowledge_base_deletion(tmp_path: Path) -> None:
    calls: list[str] = []

    def runner(command, **_kwargs):
        calls.append(command[0])
        if command[0] == "pg_restore":
            return SimpleNamespace(returncode=0, stderr=b"")
        assert command[0] == "pg_dump"
        dump = Path(
            next(arg.split("=", 1)[1] for arg in command if arg.startswith("--file="))
        )
        dump.write_bytes(b"discarded dump")
        return SimpleNamespace(returncode=0, stderr=b"")

    backend, coordinator = _coordinator(tmp_path, runner)
    marker = backend.sql(sqlite="?", postgres="%s")
    with backend.transaction(write=True) as connection:
        connection.execute(
            "INSERT INTO ha_api_kb_deletions(storage_id,tenant_id,kb_epoch,phase,"
            "artifact_versions,catalog_documents,started_at,updated_at) VALUES("
            f"{','.join(marker for _ in range(8))})",
            ("storage", "tenant", 2, "cleaned", 1, 1, 1, 1),
        )

    with pytest.raises(
        RecoveryManifestError, match="knowledge-base deletion is incomplete"
    ):
        coordinator.create(tmp_path / "backups", name="unsafe")
    assert calls == ["pg_dump", "pg_restore"]
    assert not (tmp_path / "backups" / "unsafe").exists()
    backend.close()


def test_backup_rejects_inflight_connector_commit(tmp_path: Path) -> None:
    def runner(command, **_kwargs):
        if command[0] == "pg_restore":
            return SimpleNamespace(returncode=0, stderr=b"")
        dump = Path(
            next(arg.split("=", 1)[1] for arg in command if arg.startswith("--file="))
        )
        dump.write_bytes(b"discarded dump")
        return SimpleNamespace(returncode=0, stderr=b"")

    backend, coordinator = _coordinator(tmp_path, runner)
    marker = backend.sql(sqlite="?", postgres="%s")
    with backend.transaction(write=True) as connection:
        connection.execute(
            "INSERT INTO ha_connector_commits(job_id,tenant_id,kb_id,connection_id,"
            "connector_type,kb_epoch,fencing_token,phase,index_job_id,manifest_key,"
            "manifest_sha256,file_count,total_bytes,created_at,updated_at) VALUES("
            f"{','.join(marker for _ in range(15))})",
            (
                "sync-one",
                "tenant",
                "storage",
                "conn-one",
                "url",
                1,
                1,
                "prepared",
                None,
                "connector-commits/one/manifest.json",
                "a" * 64,
                0,
                0,
                1,
                1,
            ),
        )

    with pytest.raises(RecoveryManifestError, match="connector commit is incomplete"):
        coordinator.create(tmp_path / "backups", name="unsafe-connector")
    assert not (tmp_path / "backups" / "unsafe-connector").exists()
    backend.close()


def test_backup_verification_rejects_dump_and_bundle_tampering(tmp_path: Path) -> None:
    def runner(command, **_kwargs):
        if command[0] == "pg_restore":
            return SimpleNamespace(returncode=0, stderr=b"")
        dump = Path(
            next(arg.split("=", 1)[1] for arg in command if arg.startswith("--file="))
        )
        dump.write_bytes(b"dump")
        return SimpleNamespace(returncode=0, stderr=b"")

    backend, coordinator = _coordinator(tmp_path, runner)
    first = Path(coordinator.create(tmp_path / "backups", name="first")["path"])
    (first / "database.dump").write_bytes(b"tampered")
    with pytest.raises(HABackupError, match="dump is corrupt"):
        coordinator.verify(first)

    second = Path(coordinator.create(tmp_path / "backups", name="second")["path"])
    bundle_path = second / "bundle.json"
    bundle = json.loads(bundle_path.read_text())
    bundle["database"]["snapshot_id"] = "forged"
    bundle_path.write_text(json.dumps(bundle))
    with pytest.raises(HABackupError, match="checksum"):
        coordinator.verify(second)
    backend.close()


def test_backup_verification_rejects_wrong_restored_database(tmp_path: Path) -> None:
    def runner(command, **_kwargs):
        if command[0] == "pg_restore":
            return SimpleNamespace(returncode=0, stderr=b"")
        dump = Path(
            next(arg.split("=", 1)[1] for arg in command if arg.startswith("--file="))
        )
        dump.write_bytes(b"dump")
        return SimpleNamespace(returncode=0, stderr=b"")

    backend, coordinator = _coordinator(tmp_path, runner)
    path = Path(coordinator.create(tmp_path / "backups", name="point")["path"])
    marker = backend.sql(sqlite="?", postgres="%s")
    with backend.transaction(write=True) as connection:
        connection.execute(
            f"UPDATE ha_schema_migrations SET name={marker} WHERE version={marker}",
            ("wrong recovery point", 2),
        )

    with pytest.raises(RecoveryManifestError, match="database authority differs"):
        coordinator.verify(path)
    backend.close()


def test_backup_rejects_unsafe_names_bounds_and_existing_destination(
    tmp_path: Path,
) -> None:
    backend, coordinator = _coordinator(
        tmp_path, lambda *_args, **_kwargs: SimpleNamespace(returncode=1)
    )
    with pytest.raises(ValueError, match="backup name"):
        coordinator.create(tmp_path, name="../escape")
    with pytest.raises(ValueError, match="timeout"):
        coordinator.create(tmp_path, name="safe", timeout_seconds=1)
    (tmp_path / "exists").mkdir()
    with pytest.raises(HABackupError, match="already exists"):
        coordinator.create(tmp_path, name="exists")
    backend.close()
