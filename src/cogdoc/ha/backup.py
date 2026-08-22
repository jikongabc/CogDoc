from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, Protocol

from cogdoc.ha.recovery import HARecoveryManifest
from cogdoc.ha.storage import DatabaseConnection


HA_BACKUP_BUNDLE_FORMAT = "cogdoc-ha-backup-v1"
_BUNDLE_NAME = "bundle.json"
_DUMP_NAME = "database.dump"
_RECOVERY_NAME = "recovery-manifest.json"
_MAX_BUNDLE_BYTES = 1024 * 1024
_READ_CHUNK = 1024 * 1024
_SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_SAFE_EXECUTABLE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}\Z")


class HABackupError(RuntimeError):
    pass


class ExportedSnapshotBackend(Protocol):
    dsn: str
    schema: str

    def exported_snapshot(
        self, *, statement_timeout_seconds: float = 3600.0
    ) -> AbstractContextManager[tuple[DatabaseConnection, str]]: ...


def _canonical(value: Mapping[str, Any]) -> bytes:
    try:
        return json.dumps(
            dict(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    except (TypeError, ValueError) as exc:
        raise HABackupError("HA backup metadata must be finite JSON") from exc


def _clean_name(value: object, field: str) -> str:
    text = str(value or "")
    if _SAFE_NAME.fullmatch(text) is None or text in {".", ".."}:
        raise ValueError(f"{field} is invalid")
    return text


def _executable(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{field} executable is invalid")
    path = Path(value)
    if path.is_absolute():
        if not path.is_file() or not os.access(path, os.X_OK):
            raise ValueError(f"{field} executable is unavailable")
        return str(path)
    if len(path.parts) != 1 or _SAFE_EXECUTABLE.fullmatch(value) is None:
        raise ValueError(f"{field} executable is invalid")
    return value


def _hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_READ_CHUNK), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    encoded = json.dumps(
        dict(value),
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
        allow_nan=False,
    ).encode()
    if len(encoded) > _MAX_BUNDLE_BYTES:
        raise HABackupError("HA backup bundle metadata is too large")
    with path.open("xb") as handle:
        os.chmod(path, 0o600)
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())


class HABackupCoordinator:
    """Create and verify a PostgreSQL/object-store recovery point.

    The exporter transaction stays open while pg_dump consumes the exported
    snapshot and while the authority inventory is read from that same snapshot.
    The final directory is published with one atomic rename only after the dump,
    object verification and all metadata fsync barriers have succeeded.
    """

    def __init__(
        self,
        backend: ExportedSnapshotBackend,
        recovery: HARecoveryManifest,
        *,
        runner: Callable[..., Any] = subprocess.run,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if recovery.backend is not backend:
            raise ValueError("backup and recovery must share one database backend")
        self.backend = backend
        self.recovery = recovery
        self._runner = runner
        self._clock = clock

    def create(
        self,
        output_dir: str | os.PathLike[str],
        *,
        name: str,
        pg_dump_binary: str = "pg_dump",
        pg_restore_binary: str = "pg_restore",
        timeout_seconds: float = 3600.0,
        verify_content: bool = True,
    ) -> dict[str, Any]:
        bundle_name = _clean_name(name, "backup name")
        executable = _executable(pg_dump_binary, "pg_dump")
        restore_executable = _executable(pg_restore_binary, "pg_restore")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(float(timeout_seconds))
            or not 60 <= float(timeout_seconds) <= 86_400
        ):
            raise ValueError("backup timeout must be between 60 and 86400 seconds")
        root = Path(output_dir)
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        root = root.resolve(strict=True)
        destination = root / bundle_name
        if destination.exists() or destination.is_symlink():
            raise HABackupError("HA backup destination already exists")
        staging = Path(tempfile.mkdtemp(prefix=f".{bundle_name}.", dir=root))
        os.chmod(staging, 0o700)
        dump_path = staging / _DUMP_NAME
        try:
            with self.backend.exported_snapshot(
                statement_timeout_seconds=float(timeout_seconds)
            ) as (connection, snapshot_id):
                environment = os.environ.copy()
                environment["PGDATABASE"] = self.backend.dsn
                command = [
                    executable,
                    "--format=custom",
                    f"--file={dump_path}",
                    f"--snapshot={snapshot_id}",
                    f"--schema={self.backend.schema}",
                    "--no-owner",
                    "--no-privileges",
                ]
                try:
                    completed = self._runner(
                        command,
                        check=False,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        env=environment,
                        timeout=float(timeout_seconds),
                    )
                except (OSError, subprocess.TimeoutExpired) as exc:
                    raise HABackupError("pg_dump did not complete") from exc
                if int(getattr(completed, "returncode", 1)) != 0:
                    raise HABackupError("pg_dump failed")
                if not dump_path.is_file() or dump_path.is_symlink():
                    raise HABackupError("pg_dump did not create a regular dump file")
                os.chmod(dump_path, 0o600)
                with dump_path.open("rb") as handle:
                    os.fsync(handle.fileno())
                dump_sha256, dump_size = _hash_file(dump_path)
                if dump_size < 1:
                    raise HABackupError("pg_dump created an empty dump")
                self._verify_dump_archive(
                    dump_path,
                    executable=restore_executable,
                    timeout_seconds=float(timeout_seconds),
                )
                recovery_manifest = self.recovery.capture(
                    snapshot_id,
                    database_sha256=dump_sha256,
                    verify_content=verify_content,
                    connection=connection,
                )
                self.recovery.write(staging / _RECOVERY_NAME, recovery_manifest)
            bundle: dict[str, Any] = {
                "format": HA_BACKUP_BUNDLE_FORMAT,
                "created_at": float(self._clock()),
                "database": {
                    "file": _DUMP_NAME,
                    "sha256": dump_sha256,
                    "byte_size": dump_size,
                    "snapshot_id": recovery_manifest["database_snapshot_id"],
                    "schema": self.backend.schema,
                },
                "recovery_manifest": {
                    "file": _RECOVERY_NAME,
                    "sha256": recovery_manifest["manifest_sha256"],
                    "objects": len(recovery_manifest["objects"]),
                },
            }
            bundle["bundle_sha256"] = hashlib.sha256(_canonical(bundle)).hexdigest()
            _write_json(staging / _BUNDLE_NAME, bundle)
            _fsync_directory(staging)
            os.rename(staging, destination)
            _fsync_directory(root)
            return {"path": str(destination), **bundle}
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise

    def verify(
        self,
        path: str | os.PathLike[str],
        *,
        pg_restore_binary: str = "pg_restore",
        timeout_seconds: float = 3600.0,
        verify_content: bool = True,
    ) -> dict[str, Any]:
        restore_executable = _executable(pg_restore_binary, "pg_restore")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(float(timeout_seconds))
            or not 60 <= float(timeout_seconds) <= 86_400
        ):
            raise ValueError("backup timeout must be between 60 and 86400 seconds")
        root = Path(path)
        if root.is_symlink() or not root.is_dir():
            raise HABackupError("HA backup path is not a regular directory")
        root = root.resolve(strict=True)
        bundle_path = root / _BUNDLE_NAME
        try:
            if (
                bundle_path.is_symlink()
                or bundle_path.stat().st_size > _MAX_BUNDLE_BYTES
            ):
                raise HABackupError("HA backup bundle metadata is invalid")
            raw = json.loads(bundle_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise HABackupError("HA backup bundle metadata cannot be read") from exc
        if not isinstance(raw, dict) or raw.get("format") != HA_BACKUP_BUNDLE_FORMAT:
            raise HABackupError("HA backup bundle format is invalid")
        created_at = raw.get("created_at")
        if (
            isinstance(created_at, bool)
            or not isinstance(created_at, (int, float))
            or not math.isfinite(float(created_at))
        ):
            raise HABackupError("HA backup bundle timestamp is invalid")
        expected = str(raw.get("bundle_sha256") or "")
        unsigned = dict(raw)
        unsigned.pop("bundle_sha256", None)
        if hashlib.sha256(_canonical(unsigned)).hexdigest() != expected:
            raise HABackupError("HA backup bundle checksum is invalid")
        database = raw.get("database")
        recovery_entry = raw.get("recovery_manifest")
        if not isinstance(database, Mapping) or not isinstance(recovery_entry, Mapping):
            raise HABackupError("HA backup bundle entries are invalid")
        if (
            database.get("file") != _DUMP_NAME
            or recovery_entry.get("file") != _RECOVERY_NAME
        ):
            raise HABackupError("HA backup bundle filenames are invalid")
        dump_path = root / _DUMP_NAME
        if dump_path.is_symlink() or not dump_path.is_file():
            raise HABackupError("HA database dump is missing")
        dump_sha256, dump_size = _hash_file(dump_path)
        if dump_sha256 != database.get("sha256") or dump_size != database.get(
            "byte_size"
        ):
            raise HABackupError("HA database dump is corrupt")
        self._verify_dump_archive(
            dump_path,
            executable=restore_executable,
            timeout_seconds=float(timeout_seconds),
        )
        manifest_path = root / _RECOVERY_NAME
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise HABackupError("HA recovery manifest is missing")
        manifest = self.recovery.read(manifest_path)
        if (
            manifest.get("manifest_sha256") != recovery_entry.get("sha256")
            or manifest.get("database_snapshot_id") != database.get("snapshot_id")
            or manifest.get("database_sha256") != dump_sha256
        ):
            raise HABackupError("HA recovery manifest disagrees with database dump")
        if database.get("schema") != self.backend.schema:
            raise HABackupError("HA backup schema does not match this deployment")
        result = self.recovery.verify(manifest, verify_content=verify_content)
        if (
            isinstance(recovery_entry.get("objects"), bool)
            or recovery_entry.get("objects") != result["objects"]
        ):
            raise HABackupError("HA recovery object count changed")
        self.recovery.verify_database_authority(manifest)
        return {
            "status": "verified",
            "path": str(root),
            "database_bytes": dump_size,
            **result,
        }

    def _verify_dump_archive(
        self, dump_path: Path, *, executable: str, timeout_seconds: float
    ) -> None:
        try:
            completed = self._runner(
                [executable, "--list", str(dump_path)],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise HABackupError(
                "pg_restore archive verification did not complete"
            ) from exc
        if int(getattr(completed, "returncode", 1)) != 0:
            raise HABackupError("PostgreSQL dump archive is invalid")


__all__ = [
    "HABackupCoordinator",
    "HABackupError",
    "HA_BACKUP_BUNDLE_FORMAT",
]
