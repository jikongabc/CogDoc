from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any

from cogdoc.ha.api_state import MutationLease, StaleMutationFence
from cogdoc.ha.object_store import ObjectIntegrityError, ObjectStore
from cogdoc.ha.storage import DatabaseBackend


_READ_SIZE = 1024 * 1024
_MAX_MANIFEST_BYTES = 64 * 1024 * 1024
_PHASES = {"prepared": 0, "swapped": 1, "materialized": 2, "indexed": 3}


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()


def _row(value: Any | None) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, Mapping):
        return dict(value)
    keys = getattr(value, "keys", None)
    if callable(keys):
        return {str(key): value[key] for key in keys()}
    raise RuntimeError("connector commit row mapping is unavailable")


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class DistributedConnectorCommitStore:
    """Shared, immutable handoff for a connector job in ``committing``.

    The private connection snapshot is uploaded before the sync ledger crosses
    its authority boundary. Local phase files remain a fast path, while this
    store is the crash/multi-node recovery authority.
    """

    def __init__(
        self,
        backend: DatabaseBackend,
        object_store: ObjectStore,
        mutation_coordinator: Any,
        *,
        max_files: int = 100_000,
        max_total_bytes: int = 10 * 1024 * 1024 * 1024,
        clock: Any = time.time,
    ) -> None:
        if type(max_files) is not int or not 1 <= max_files <= 1_000_000:
            raise ValueError("connector commit max_files is invalid")
        if type(max_total_bytes) is not int or max_total_bytes < 1:
            raise ValueError("connector commit max_total_bytes is invalid")
        self.backend = backend
        self.object_store = object_store
        self.mutation_coordinator = mutation_coordinator
        self.max_files = max_files
        self.max_total_bytes = max_total_bytes
        self._clock = clock
        with backend.transaction(write=True) as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS ha_connector_commits (
                job_id TEXT PRIMARY KEY,tenant_id TEXT NOT NULL,kb_id TEXT NOT NULL,
                connection_id TEXT NOT NULL,connector_type TEXT NOT NULL,
                kb_epoch BIGINT NOT NULL,fencing_token BIGINT NOT NULL,
                phase TEXT NOT NULL,index_job_id TEXT,manifest_key TEXT NOT NULL,
                manifest_sha256 TEXT NOT NULL,file_count INTEGER NOT NULL,
                total_bytes BIGINT NOT NULL,created_at DOUBLE PRECISION NOT NULL,
                updated_at DOUBLE PRECISION NOT NULL)"""
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_ha_connector_commits_scope "
                "ON ha_connector_commits(tenant_id,kb_id,created_at,job_id)"
            )

    @staticmethod
    def _prefix(job_id: str) -> str:
        return f"connector-commits/{hashlib.sha256(job_id.encode()).hexdigest()}"

    def _lease(self, kb_id: str) -> MutationLease:
        current = getattr(self.mutation_coordinator, "current_lease", None)
        assert_live = getattr(self.mutation_coordinator, "assert_live", None)
        lease = current() if callable(current) else None
        if not isinstance(lease, MutationLease) or lease.storage_id != kb_id:
            raise StaleMutationFence("connector commit has no live KB mutation lease")
        if not callable(assert_live):
            raise RuntimeError("connector commit authority cannot validate leases")
        assert_live(lease)
        return lease

    def prepare(
        self,
        *,
        job_id: str,
        tenant_id: str,
        kb_id: str,
        connection_id: str,
        connector_type: str,
        staging: str | os.PathLike[str],
    ) -> dict[str, Any]:
        lease = self._lease(kb_id)
        root = Path(staging).resolve(strict=True)
        if root.is_symlink() or not root.is_dir():
            raise ObjectIntegrityError("connector commit staging is unsafe")
        prefix = self._prefix(job_id)
        files: list[dict[str, Any]] = []
        total_bytes = 0
        for candidate in sorted(root.rglob("*")):
            if candidate.is_symlink():
                raise ObjectIntegrityError("connector commit contains a symlink")
            if not candidate.is_file():
                continue
            relative = candidate.relative_to(root).as_posix()
            parsed = PurePosixPath(relative)
            if (
                not relative
                or parsed.is_absolute()
                or any(part in {"", ".", ".."} for part in parsed.parts)
                or "\\" in relative
            ):
                raise ObjectIntegrityError("connector commit path is unsafe")
            size = candidate.stat().st_size
            total_bytes += size
            if len(files) + 1 > self.max_files or total_bytes > self.max_total_bytes:
                raise ObjectIntegrityError("connector commit exceeds configured limits")
            file_digest = hashlib.sha256()
            with candidate.open("rb") as handle:
                for chunk in iter(lambda: handle.read(_READ_SIZE), b""):
                    file_digest.update(chunk)
            sha256 = file_digest.hexdigest()
            object_key = f"{prefix}/files/{relative}"
            self.object_store.put_file(object_key, candidate, sha256=sha256)
            files.append(
                {
                    "path": relative,
                    "object_key": object_key,
                    "byte_size": size,
                    "sha256": sha256,
                }
            )
        manifest = {
            "schema_version": 1,
            "job_id": job_id,
            "tenant_id": tenant_id,
            "kb_id": kb_id,
            "connection_id": connection_id,
            "connector_type": connector_type,
            "kb_epoch": lease.kb_epoch,
            "fencing_token": lease.fencing_token,
            "file_count": len(files),
            "total_bytes": total_bytes,
            "files": files,
        }
        encoded = _canonical(manifest)
        if len(encoded) > _MAX_MANIFEST_BYTES:
            raise ObjectIntegrityError("connector commit manifest is too large")
        manifest_digest = hashlib.sha256(encoded).hexdigest()
        manifest_key = f"{prefix}/manifest.json"
        self.object_store.put_bytes(
            manifest_key, encoded, sha256=manifest_digest
        )
        now = float(self._clock())
        marker = self.backend.sql(sqlite="?", postgres="%s")
        with self.backend.transaction(write=True) as connection:
            connection.execute(
                "INSERT INTO ha_connector_commits(job_id,tenant_id,kb_id,connection_id,"
                "connector_type,kb_epoch,fencing_token,phase,index_job_id,manifest_key,"
                "manifest_sha256,file_count,total_bytes,created_at,updated_at) VALUES("
                f"{','.join([marker] * 15)}) ON CONFLICT(job_id) DO NOTHING",
                (
                    job_id,
                    tenant_id,
                    kb_id,
                    connection_id,
                    connector_type,
                    lease.kb_epoch,
                    lease.fencing_token,
                    "prepared",
                    None,
                    manifest_key,
                    manifest_digest,
                    len(files),
                    total_bytes,
                    now,
                    now,
                ),
            )
            stored = _row(
                connection.execute(
                    f"SELECT * FROM ha_connector_commits WHERE job_id={marker}",
                    (job_id,),
                ).fetchone()
            )
        if stored is None or any(
            stored[field] != value
            for field, value in {
                "tenant_id": tenant_id,
                "kb_id": kb_id,
                "connection_id": connection_id,
                "connector_type": connector_type,
                "manifest_sha256": manifest_digest,
            }.items()
        ) or int(stored["kb_epoch"]) != lease.kb_epoch:
            raise ObjectIntegrityError("connector commit handoff conflicts")
        return stored

    def set_phase(self, job_id: str, phase: str, index_job_id: str | None) -> None:
        if phase not in _PHASES:
            raise ValueError("connector commit phase is invalid")
        marker = self.backend.sql(sqlite="?", postgres="%s")
        with self.backend.transaction() as connection:
            row = _row(
                connection.execute(
                    f"SELECT kb_id,phase,index_job_id FROM ha_connector_commits "
                    f"WHERE job_id={marker}",
                    (job_id,),
                ).fetchone()
            )
            if row is None:
                raise KeyError(job_id)
        self._lease(str(row["kb_id"]))
        if _PHASES[phase] < _PHASES[str(row["phase"])]:
            return
        with self.backend.transaction(write=True) as connection:
            connection.execute(
                f"UPDATE ha_connector_commits SET phase={marker},index_job_id={marker},"
                f"updated_at={marker} WHERE job_id={marker} AND phase={marker}",
                (phase, index_job_id, float(self._clock()), job_id, row["phase"]),
            )

    def restore(
        self,
        *,
        job_id: str,
        tenant_id: str,
        kb_id: str,
        connection_id: str,
        connector_type: str,
        staging: str | os.PathLike[str],
    ) -> dict[str, Any]:
        lease = self._lease(kb_id)
        marker = self.backend.sql(sqlite="?", postgres="%s")
        with self.backend.transaction() as connection:
            record = _row(
                connection.execute(
                    f"SELECT * FROM ha_connector_commits WHERE job_id={marker}",
                    (job_id,),
                ).fetchone()
            )
        if record is None:
            raise ObjectIntegrityError("connector commit handoff is missing")
        if any(
            record[field] != value
            for field, value in {
                "tenant_id": tenant_id,
                "kb_id": kb_id,
                "connection_id": connection_id,
                "connector_type": connector_type,
            }.items()
        ) or int(record["kb_epoch"]) != lease.kb_epoch:
            raise StaleMutationFence("connector commit scope changed")
        manifest_chunks: list[bytes] = []
        manifest_size = 0
        for chunk in self.object_store.iter_bytes(str(record["manifest_key"])):
            manifest_size += len(chunk)
            if manifest_size > _MAX_MANIFEST_BYTES:
                raise ObjectIntegrityError("connector commit manifest is too large")
            manifest_chunks.append(chunk)
        manifest_bytes = b"".join(manifest_chunks)
        if hashlib.sha256(manifest_bytes).hexdigest() != record["manifest_sha256"]:
            raise ObjectIntegrityError("connector commit manifest hash mismatch")
        manifest = json.loads(manifest_bytes)
        if not isinstance(manifest, Mapping) or any(
            manifest.get(field) != value
            for field, value in {
                "schema_version": 1,
                "job_id": job_id,
                "tenant_id": tenant_id,
                "kb_id": kb_id,
                "connection_id": connection_id,
                "connector_type": connector_type,
                "kb_epoch": int(record["kb_epoch"]),
                "fencing_token": int(record["fencing_token"]),
                "file_count": int(record["file_count"]),
                "total_bytes": int(record["total_bytes"]),
            }.items()
        ):
            raise ObjectIntegrityError("connector commit manifest authority mismatch")
        files = manifest.get("files")
        if not isinstance(files, list) or len(files) != int(record["file_count"]):
            raise ObjectIntegrityError("connector commit manifest files are invalid")
        target = Path(staging)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = Path(tempfile.mkdtemp(prefix=".tmp-commit-", dir=target.parent))
        seen: set[str] = set()
        total = 0
        try:
            for item in files:
                if not isinstance(item, Mapping):
                    raise ObjectIntegrityError("connector commit file is invalid")
                relative = str(item.get("path") or "")
                parsed = PurePosixPath(relative)
                if (
                    not relative
                    or parsed.is_absolute()
                    or any(part in {"", ".", ".."} for part in parsed.parts)
                    or "\\" in relative
                    or relative in seen
                    or item.get("object_key") != f"{self._prefix(job_id)}/files/{relative}"
                ):
                    raise ObjectIntegrityError("connector commit file path is invalid")
                seen.add(relative)
                destination = temporary.joinpath(*parsed.parts)
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                digest = hashlib.sha256()
                size = 0
                with destination.open("xb") as handle:
                    for chunk in self.object_store.iter_bytes(str(item["object_key"])):
                        digest.update(chunk)
                        size += len(chunk)
                        total += len(chunk)
                        if total > self.max_total_bytes:
                            raise ObjectIntegrityError("connector commit restore is too large")
                        handle.write(chunk)
                    handle.flush()
                    os.fsync(handle.fileno())
                if size != int(item["byte_size"]) or digest.hexdigest() != item["sha256"]:
                    raise ObjectIntegrityError("connector commit object is corrupt")
                _fsync_directory(destination.parent)
            if total != int(record["total_bytes"]):
                raise ObjectIntegrityError("connector commit total size mismatch")
            _fsync_directory(temporary)
            if target.exists():
                shutil.rmtree(target)
            os.replace(temporary, target)
            _fsync_directory(target.parent)
        except BaseException:
            shutil.rmtree(temporary, ignore_errors=True)
            raise
        return record

    def finalize(self, job_id: str) -> None:
        marker = self.backend.sql(sqlite="?", postgres="%s")
        with self.backend.transaction() as connection:
            row = _row(
                connection.execute(
                    f"SELECT kb_id FROM ha_connector_commits WHERE job_id={marker}",
                    (job_id,),
                ).fetchone()
            )
        if row is None:
            return
        # This is garbage collection of an immutable private handoff, not a
        # source/index visibility mutation. It must remain possible after the
        # KB lifecycle fence enters ``deleting``.
        prefix = self._prefix(job_id) + "/"
        for item in tuple(self.object_store.list_prefix(prefix)):
            self.object_store.delete(item.key)
        with self.backend.transaction(write=True) as connection:
            connection.execute(
                f"DELETE FROM ha_connector_commits WHERE job_id={marker}", (job_id,)
            )

    def check(self) -> bool:
        with self.backend.transaction() as connection:
            connection.execute("SELECT 1 FROM ha_connector_commits LIMIT 1").fetchone()
        return True
