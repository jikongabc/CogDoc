from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import tempfile
import time
import uuid
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any

from cogdoc.ha.api_state import MutationLease, StaleMutationFence
from cogdoc.ha.object_store import ObjectIntegrityError, ObjectNotFound, ObjectStore
from cogdoc.ha.outbox import OutboxStore
from cogdoc.ha.storage import DatabaseBackend, DatabaseConnection
from cogdoc.service.kb_lifecycle import LIFECYCLE_ACTIVE
from cogdoc.service.mutation_paths import MUTATION_BACKUP_SUFFIX
from cogdoc.tools.source_parser import SUPPORTED_EXTENSIONS


SOURCE_PREPARED = "prepared"
SOURCE_ACTIVE = "active"
SOURCE_SUPERSEDED = "superseded"
_LOCAL_MARKER = ".cogdoc-source-generation.json"
_READ_SIZE = 1024 * 1024


class SourceGenerationError(RuntimeError):
    pass


class SourceGenerationConflict(SourceGenerationError):
    pass


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    except (TypeError, ValueError) as exc:
        raise ValueError("source generation manifest must be finite JSON") from exc


def _row(row: Any | None) -> dict[str, Any] | None:
    if row is None:
        return None
    if isinstance(row, Mapping):
        return dict(row)
    keys = getattr(row, "keys", None)
    if callable(keys):
        return {str(key): row[key] for key in keys()}
    raise SourceGenerationError("database row mapping is unavailable")


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class SourceGenerationStore:
    """Immutable source snapshots published with the KB mutation fence.

    Files and the canonical manifest are immutable objects. The database head
    is the sole visibility authority and advances only after all objects and a
    commit marker are durable. A stale node cannot publish because the same
    transaction validates the lease token, fencing token, KB epoch and prior
    head generation.
    """

    def __init__(
        self,
        backend: DatabaseBackend,
        object_store: ObjectStore,
        *,
        outbox: OutboxStore | None = None,
        max_files: int = 100_000,
        max_total_bytes: int = 10 * 1024 * 1024 * 1024,
        clock: Any = time.time,
    ) -> None:
        if type(max_files) is not int or not 1 <= max_files <= 1_000_000:
            raise ValueError("source generation max_files is invalid")
        if type(max_total_bytes) is not int or max_total_bytes < 1:
            raise ValueError("source generation max_total_bytes is invalid")
        self.backend = backend
        self.object_store = object_store
        self.outbox = outbox
        self.max_files = max_files
        self.max_total_bytes = max_total_bytes
        self._clock = clock
        with backend.transaction(write=True) as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS ha_source_generations (
                generation_id TEXT PRIMARY KEY,storage_id TEXT NOT NULL,tenant_id TEXT NOT NULL,
                kb_epoch BIGINT NOT NULL,base_generation_id TEXT,build_id TEXT,status TEXT NOT NULL,
                manifest_key TEXT NOT NULL,manifest_sha256 TEXT NOT NULL,file_count INTEGER NOT NULL,
                total_bytes BIGINT NOT NULL,document_count INTEGER NOT NULL,
                document_bytes BIGINT NOT NULL,fencing_token BIGINT NOT NULL,created_at DOUBLE PRECISION NOT NULL,
                published_at DOUBLE PRECISION)"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS ha_source_heads (
                storage_id TEXT PRIMARY KEY,generation_id TEXT NOT NULL,revision BIGINT NOT NULL,
                updated_at DOUBLE PRECISION NOT NULL)"""
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_ha_source_generations_scope "
                "ON ha_source_generations(storage_id,created_at)"
            )

    @staticmethod
    def _scope_prefix(storage_id: str) -> str:
        digest = hashlib.sha256(storage_id.encode()).hexdigest()
        return f"sources/{digest}"

    @staticmethod
    def _safe_relative(path: Path, root: Path) -> str:
        relative = path.relative_to(root).as_posix()
        parsed = PurePosixPath(relative)
        if (
            not relative
            or parsed.is_absolute()
            or any(part in {"", ".", ".."} for part in parsed.parts)
            or "\\" in relative
            or any(
                ord(character) < 32 or ord(character) == 127 for character in relative
            )
        ):
            raise SourceGenerationError("source path is unsafe")
        return relative

    def _head(self, storage_id: str) -> dict[str, Any] | None:
        marker = self.backend.sql(sqlite="?", postgres="%s")
        with self.backend.transaction() as connection:
            return _row(
                connection.execute(
                    f"SELECT * FROM ha_source_heads WHERE storage_id={marker}",
                    (storage_id,),
                ).fetchone()
            )

    def stage_directory(
        self,
        *,
        tenant_id: str,
        storage_id: str,
        source_dir: str | os.PathLike[str],
        lease: MutationLease,
        generation_id: str | None = None,
        build_id: str | None = None,
    ) -> dict[str, Any]:
        if lease.storage_id != storage_id:
            raise StaleMutationFence("source scope does not match mutation lease")
        if not tenant_id or not storage_id:
            raise ValueError("source generation scope is invalid")
        generation_id = generation_id or f"src-{uuid.uuid4().hex}"
        if not generation_id.startswith("src-") or len(generation_id) > 128:
            raise ValueError("source generation_id is invalid")
        if build_id is not None and (not build_id or len(build_id.encode()) > 255):
            raise ValueError("source generation build_id is invalid")
        root = Path(source_dir).resolve(strict=True)
        if not root.is_dir() or root.is_symlink():
            raise SourceGenerationError("source directory is unsafe")
        base = self._head(storage_id)
        base_generation_id = None if base is None else str(base["generation_id"])
        prefix = f"{self._scope_prefix(storage_id)}/{generation_id}"
        entries: list[dict[str, Any]] = []
        total_bytes = 0
        document_count = 0
        document_bytes = 0
        for candidate in sorted(root.rglob("*")):
            if candidate.is_symlink():
                raise SourceGenerationError("source snapshot contains a symlink")
            if not candidate.is_file():
                continue
            relative = self._safe_relative(candidate, root)
            if relative == _LOCAL_MARKER or candidate.name.endswith(
                MUTATION_BACKUP_SUFFIX
            ):
                continue
            size = candidate.stat().st_size
            total_bytes += size
            if len(entries) + 1 > self.max_files or total_bytes > self.max_total_bytes:
                raise SourceGenerationError("source snapshot exceeds configured limits")
            digest = hashlib.sha256()
            with candidate.open("rb") as handle:
                for chunk in iter(lambda: handle.read(_READ_SIZE), b""):
                    digest.update(chunk)
            sha256 = digest.hexdigest()
            if (
                len(PurePosixPath(relative).parts) == 1
                and candidate.suffix.casefold() in SUPPORTED_EXTENSIONS
            ):
                document_count += 1
                document_bytes += size
            object_key = f"{prefix}/files/{relative}"
            self.object_store.put_file(object_key, candidate, sha256=sha256)
            entries.append(
                {
                    "path": relative,
                    "object_key": object_key,
                    "byte_size": size,
                    "sha256": sha256,
                }
            )
        manifest = {
            "schema_version": 1,
            "tenant_id": tenant_id,
            "storage_id": storage_id,
            "kb_epoch": lease.kb_epoch,
            "generation_id": generation_id,
            "build_id": build_id,
            "base_generation_id": base_generation_id,
            "fencing_token": lease.fencing_token,
            "file_count": len(entries),
            "total_bytes": total_bytes,
            "document_count": document_count,
            "document_bytes": document_bytes,
            "files": entries,
        }
        manifest_bytes = _canonical(manifest)
        manifest_hash = hashlib.sha256(manifest_bytes).hexdigest()
        manifest_key = f"{prefix}/manifest.json"
        self.object_store.put_bytes(manifest_key, manifest_bytes, sha256=manifest_hash)
        marker_payload = _canonical(
            {
                "generation_id": generation_id,
                "manifest_key": manifest_key,
                "manifest_sha256": manifest_hash,
            }
        )
        self.object_store.put_bytes(
            f"{prefix}/COMMITTED",
            marker_payload,
            sha256=hashlib.sha256(marker_payload).hexdigest(),
        )
        db_marker = self.backend.sql(sqlite="?", postgres="%s")
        with self.backend.transaction(write=True) as connection:
            connection.execute(
                "INSERT INTO ha_source_generations(generation_id,storage_id,tenant_id,kb_epoch,"
                "base_generation_id,build_id,status,manifest_key,manifest_sha256,file_count,total_bytes,"
                "document_count,document_bytes,fencing_token,created_at) VALUES("
                f"{','.join([db_marker] * 15)})",
                (
                    generation_id,
                    storage_id,
                    tenant_id,
                    lease.kb_epoch,
                    base_generation_id,
                    build_id,
                    SOURCE_PREPARED,
                    manifest_key,
                    manifest_hash,
                    len(entries),
                    total_bytes,
                    document_count,
                    document_bytes,
                    lease.fencing_token,
                    float(self._clock()),
                ),
            )
        return manifest

    def stage_for_commit(
        self,
        *,
        storage_id: str,
        source_dir: str | os.PathLike[str],
        lease: MutationLease,
        build_id: str,
    ) -> dict[str, Any]:
        marker = self.backend.sql(sqlite="?", postgres="%s")
        with self.backend.transaction() as connection:
            row = _row(
                connection.execute(
                    f"SELECT tenant_id,lifecycle,epoch FROM ha_api_knowledge_bases "
                    f"WHERE storage_id={marker}",
                    (storage_id,),
                ).fetchone()
            )
        if (
            row is None
            or row["lifecycle"] != LIFECYCLE_ACTIVE
            or int(row["epoch"]) != lease.kb_epoch
        ):
            raise StaleMutationFence("source generation KB incarnation changed")
        return self.stage_directory(
            tenant_id=str(row["tenant_id"]),
            storage_id=storage_id,
            source_dir=source_dir,
            lease=lease,
            build_id=build_id,
        )

    def prepared_for_build(
        self, storage_id: str, build_id: str, lease: MutationLease
    ) -> dict[str, Any] | None:
        marker = self.backend.sql(sqlite="?", postgres="%s")
        with self.backend.transaction() as connection:
            row = _row(
                connection.execute(
                    f"SELECT * FROM ha_source_generations WHERE storage_id={marker} "
                    f"AND build_id={marker} AND status={marker} AND kb_epoch={marker} "
                    f"AND fencing_token={marker} ORDER BY created_at DESC,generation_id DESC LIMIT 1",
                    (
                        storage_id,
                        build_id,
                        SOURCE_PREPARED,
                        lease.kb_epoch,
                        lease.fencing_token,
                    ),
                ).fetchone()
            )
        if row is not None:
            self._load_manifest(row)
        return row

    def publish(self, generation_id: str, lease: MutationLease) -> dict[str, Any]:
        hook = self.publication_hook(generation_id, lease)
        with self.backend.transaction(write=True) as connection:
            published = hook(connection, {})
        return published

    def publication_hook(self, generation_id: str, lease: MutationLease):
        """Return an index-publication hook after verifying immutable objects.

        ``IndexGenerationStore.publish`` invokes the hook inside the same
        transaction that advances the index head. This couples source and
        index visibility without keeping a database transaction open during
        object-store verification.
        """

        generation = self._get(generation_id)
        if generation is None:
            raise ObjectNotFound("source generation is unavailable")
        self._load_manifest(generation)

        def publish_with_index(
            connection: DatabaseConnection, _index_generation: Mapping[str, Any]
        ) -> dict[str, Any]:
            return self._publish_locked(connection, generation_id, lease)

        return publish_with_index

    def _get(self, generation_id: str) -> dict[str, Any] | None:
        marker = self.backend.sql(sqlite="?", postgres="%s")
        with self.backend.transaction() as connection:
            return _row(
                connection.execute(
                    f"SELECT * FROM ha_source_generations WHERE generation_id={marker}",
                    (generation_id,),
                ).fetchone()
            )

    def _publish_locked(
        self,
        connection: DatabaseConnection,
        generation_id: str,
        lease: MutationLease,
    ) -> dict[str, Any]:
        now = float(self._clock())
        marker = self.backend.sql(sqlite="?", postgres="%s")
        lock = self.backend.sql(sqlite="", postgres=" FOR UPDATE")
        generation = _row(
            connection.execute(
                f"SELECT * FROM ha_source_generations WHERE generation_id={marker}{lock}",
                (generation_id,),
            ).fetchone()
        )
        if generation is None:
            raise ObjectNotFound("source generation is unavailable")
        if generation["status"] == SOURCE_ACTIVE:
            return generation
        if generation["status"] != SOURCE_PREPARED:
            raise SourceGenerationConflict("source generation is not publishable")
        current = _row(
            connection.execute(
                f"SELECT * FROM ha_source_heads WHERE storage_id={marker}{lock}",
                (lease.storage_id,),
            ).fetchone()
        )
        kb = _row(
            connection.execute(
                f"SELECT lifecycle,epoch FROM ha_api_knowledge_bases WHERE storage_id={marker}{lock}",
                (lease.storage_id,),
            ).fetchone()
        )
        authority = _row(
            connection.execute(
                f"SELECT * FROM ha_api_mutation_leases WHERE storage_id={marker}{lock}",
                (lease.storage_id,),
            ).fetchone()
        )
        current_id = None if current is None else str(current["generation_id"])
        if (
            generation["storage_id"] != lease.storage_id
            or int(generation["kb_epoch"]) != lease.kb_epoch
            or int(generation["fencing_token"]) != lease.fencing_token
            or generation["base_generation_id"] != current_id
            or kb is None
            or kb["lifecycle"] != LIFECYCLE_ACTIVE
            or int(kb["epoch"]) != lease.kb_epoch
            or authority is None
            or authority["lease_token"] != lease.lease_token
            or int(authority["fencing_token"]) != lease.fencing_token
            or float(authority["lease_expires_at"]) <= now
        ):
            raise StaleMutationFence("source generation publication lost authority")
        if current_id is not None:
            connection.execute(
                f"UPDATE ha_source_generations SET status={marker} WHERE generation_id={marker} "
                f"AND status={marker}",
                (SOURCE_SUPERSEDED, current_id, SOURCE_ACTIVE),
            )
        if current is None:
            connection.execute(
                "INSERT INTO ha_source_heads(storage_id,generation_id,revision,updated_at) "
                f"VALUES({marker},{marker},1,{marker})",
                (lease.storage_id, generation_id, now),
            )
            revision = 1
        else:
            revision = int(current["revision"]) + 1
            changed = connection.execute(
                f"UPDATE ha_source_heads SET generation_id={marker},revision={marker},"
                f"updated_at={marker} WHERE storage_id={marker} AND generation_id={marker}",
                (generation_id, revision, now, lease.storage_id, current_id),
            )
            if changed.rowcount != 1:
                raise StaleMutationFence("source generation head CAS failed")
        connection.execute(
            f"UPDATE ha_source_generations SET status={marker},published_at={marker} "
            f"WHERE generation_id={marker} AND status={marker}",
            (SOURCE_ACTIVE, now, generation_id, SOURCE_PREPARED),
        )
        if self.outbox is not None:
            self.outbox.append(
                connection,
                tenant_id=str(generation["tenant_id"]),
                topic="kb.source-generation.published",
                aggregate_type="knowledge_base",
                aggregate_id=lease.storage_id,
                aggregate_revision=revision,
                payload={
                    "storage_id": lease.storage_id,
                    "kb_epoch": lease.kb_epoch,
                    "generation_id": generation_id,
                    "manifest_sha256": generation["manifest_sha256"],
                },
                idempotency_key=f"source-generation:{generation_id}",
            )
        published = dict(generation)
        published.update(status=SOURCE_ACTIVE, published_at=now, revision=revision)
        return published

    def current(self, storage_id: str) -> dict[str, Any] | None:
        marker = self.backend.sql(sqlite="?", postgres="%s")
        with self.backend.transaction() as connection:
            row = _row(
                connection.execute(
                    "SELECT generations.*,heads.revision FROM ha_source_heads AS heads "
                    "JOIN ha_source_generations AS generations ON generations.generation_id="
                    f"heads.generation_id WHERE heads.storage_id={marker}",
                    (storage_id,),
                ).fetchone()
            )
        return row

    def current_manifest(self, storage_id: str) -> dict[str, Any] | None:
        """Return the verified immutable manifest behind the current DB head."""

        generation = self.current(storage_id)
        if generation is None:
            return None
        return self._validated_manifest(generation, storage_id)

    def _validated_manifest(
        self, generation: Mapping[str, Any], storage_id: str
    ) -> dict[str, Any]:
        manifest = self._load_manifest(generation)
        files = manifest.get("files")
        if not isinstance(files, list) or len(files) > self.max_files:
            raise ObjectIntegrityError("source manifest file list is invalid")
        try:
            manifest_epoch = int(manifest.get("kb_epoch", -1))
            manifest_file_count = int(manifest.get("file_count", -1))
            manifest_total_bytes = int(manifest.get("total_bytes", -1))
            manifest_document_count = int(manifest.get("document_count", -1))
            manifest_document_bytes = int(manifest.get("document_bytes", -1))
        except (TypeError, ValueError) as exc:
            raise ObjectIntegrityError(
                "source manifest authority fields are invalid"
            ) from exc
        if (
            manifest.get("tenant_id") != generation["tenant_id"]
            or manifest.get("storage_id") != storage_id
            or manifest.get("generation_id") != generation["generation_id"]
            or manifest_epoch != int(generation["kb_epoch"])
            or manifest_file_count != len(files)
            or manifest_file_count != int(generation["file_count"])
            or manifest_total_bytes != int(generation["total_bytes"])
        ):
            raise ObjectIntegrityError("source manifest authority fields do not match")
        generation_prefix = str(generation["manifest_key"]).rsplit("/", 1)[0]
        seen_paths: set[str] = set()
        total_bytes = 0
        document_count = 0
        document_bytes = 0
        for item in files:
            if not isinstance(item, Mapping):
                raise ObjectIntegrityError("source manifest entry is invalid")
            relative = str(item.get("path") or "")
            parsed = PurePosixPath(relative)
            try:
                byte_size = int(item["byte_size"])
                digest = str(item["sha256"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ObjectIntegrityError("source manifest entry is invalid") from exc
            if (
                not relative
                or parsed.is_absolute()
                or any(part in {"", ".", ".."} for part in parsed.parts)
                or "\\" in relative
                or relative in seen_paths
                or byte_size < 0
                or len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)
                or item.get("object_key") != f"{generation_prefix}/files/{relative}"
            ):
                raise ObjectIntegrityError("source manifest entry is invalid")
            seen_paths.add(relative)
            total_bytes += byte_size
            if (
                len(parsed.parts) == 1
                and parsed.suffix.casefold() in SUPPORTED_EXTENSIONS
            ):
                document_count += 1
                document_bytes += byte_size
            if total_bytes > self.max_total_bytes:
                raise ObjectIntegrityError("source manifest total size exceeds limit")
        if total_bytes != manifest_total_bytes:
            raise ObjectIntegrityError("source manifest total size does not match")
        if (
            document_count != manifest_document_count
            or document_bytes != manifest_document_bytes
            or document_count != int(generation["document_count"])
            or document_bytes != int(generation["document_bytes"])
        ):
            raise ObjectIntegrityError("source manifest document usage does not match")
        return dict(manifest)

    def _load_manifest(self, generation: Mapping[str, Any]) -> dict[str, Any]:
        key = str(generation["manifest_key"])
        payload = b"".join(self.object_store.iter_bytes(key))
        actual = hashlib.sha256(payload).hexdigest()
        if actual != generation["manifest_sha256"]:
            raise ObjectIntegrityError("source manifest hash does not match authority")
        try:
            value = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ObjectIntegrityError("source manifest is invalid") from exc
        if not isinstance(value, dict) or value.get("generation_id") != generation.get(
            "generation_id"
        ):
            raise ObjectIntegrityError("source manifest identity does not match")
        marker_key = f"{key.rsplit('/', 1)[0]}/COMMITTED"
        if self.object_store.head(marker_key) is None:
            raise ObjectIntegrityError("source generation commit marker is missing")
        try:
            marker = json.loads(b"".join(self.object_store.iter_bytes(marker_key)))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ObjectIntegrityError(
                "source generation commit marker is invalid"
            ) from exc
        if not isinstance(marker, Mapping) or marker != {
            "generation_id": generation["generation_id"],
            "manifest_key": key,
            "manifest_sha256": generation["manifest_sha256"],
        }:
            raise ObjectIntegrityError("source generation commit marker does not match")
        return value

    def materialize_current(
        self, storage_id: str, target: str | os.PathLike[str]
    ) -> str | None:
        generation = self.current(storage_id)
        if generation is None:
            # A recreated/new KB has no shared head. Never retain a stale
            # process-local cache under its deterministic storage id: install
            # an empty directory with the same atomic swap/durability contract
            # used for ordinary generations.
            target_path = Path(target)
            target_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            temporary = Path(
                tempfile.mkdtemp(
                    prefix=f".tmp-source-{storage_id[:12]}-", dir=target_path.parent
                )
            )
            backup = target_path.parent / f".old-source-{uuid.uuid4().hex}"
            try:
                _fsync_directory(temporary)
                if target_path.exists():
                    os.replace(target_path, backup)
                os.replace(temporary, target_path)
                _fsync_directory(target_path.parent)
                if backup.exists():
                    shutil.rmtree(backup)
                    _fsync_directory(target_path.parent)
            except BaseException:
                shutil.rmtree(temporary, ignore_errors=True)
                if backup.exists() and not target_path.exists():
                    os.replace(backup, target_path)
                    _fsync_directory(target_path.parent)
                raise
            return None
        manifest = self._validated_manifest(generation, storage_id)
        files = manifest["files"]
        generation_prefix = str(generation["manifest_key"]).rsplit("/", 1)[0]
        seen_paths: set[str] = set()
        target_path = Path(target)
        target_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = Path(
            tempfile.mkdtemp(
                prefix=f".tmp-source-{storage_id[:12]}-", dir=target_path.parent
            )
        )
        backup = target_path.parent / f".old-source-{uuid.uuid4().hex}"
        total_bytes = 0
        try:
            for item in files:
                if not isinstance(item, Mapping):
                    raise ObjectIntegrityError("source manifest entry is invalid")
                relative = str(item.get("path") or "")
                parsed = PurePosixPath(relative)
                if (
                    not relative
                    or parsed.is_absolute()
                    or any(part in {"", ".", ".."} for part in parsed.parts)
                    or "\\" in relative
                    or relative in seen_paths
                ):
                    raise ObjectIntegrityError("source manifest path is unsafe")
                seen_paths.add(relative)
                expected_object_key = f"{generation_prefix}/files/{relative}"
                if item.get("object_key") != expected_object_key:
                    raise ObjectIntegrityError("source manifest object key is invalid")
                destination = temporary.joinpath(*parsed.parts)
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                digest = hashlib.sha256()
                size = 0
                with destination.open("xb") as handle:
                    for chunk in self.object_store.iter_bytes(str(item["object_key"])):
                        digest.update(chunk)
                        size += len(chunk)
                        total_bytes += len(chunk)
                        if total_bytes > self.max_total_bytes:
                            raise ObjectIntegrityError(
                                "source materialization exceeds limit"
                            )
                        handle.write(chunk)
                    handle.flush()
                    os.fsync(handle.fileno())
                if (
                    size != int(item["byte_size"])
                    or digest.hexdigest() != item["sha256"]
                ):
                    raise ObjectIntegrityError("source object does not match manifest")
                _fsync_directory(destination.parent)
            if total_bytes != int(manifest.get("total_bytes", -1)):
                raise ObjectIntegrityError("source manifest total size does not match")
            local_marker = _canonical(
                {
                    "generation_id": generation["generation_id"],
                    "manifest_sha256": generation["manifest_sha256"],
                }
            )
            with (temporary / _LOCAL_MARKER).open("xb") as handle:
                handle.write(local_marker)
                handle.flush()
                os.fsync(handle.fileno())
            _fsync_directory(temporary)
            if target_path.exists():
                os.replace(target_path, backup)
            os.replace(temporary, target_path)
            _fsync_directory(target_path.parent)
            if backup.exists():
                shutil.rmtree(backup)
                _fsync_directory(target_path.parent)
        except BaseException:
            shutil.rmtree(temporary, ignore_errors=True)
            if backup.exists() and not target_path.exists():
                os.replace(backup, target_path)
                _fsync_directory(target_path.parent)
            raise
        return str(generation["generation_id"])

    def garbage_candidates(
        self, *, before: float, limit: int = 100
    ) -> list[dict[str, Any]]:
        if not math.isfinite(before) or before < 0:
            raise ValueError("source generation retention cutoff is invalid")
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("source generation cleanup limit is invalid")
        marker = self.backend.sql(sqlite="?", postgres="%s")
        with self.backend.transaction() as connection:
            rows = connection.execute(
                "SELECT generations.* FROM ha_source_generations AS generations "
                "LEFT JOIN ha_source_heads AS heads ON heads.generation_id=generations.generation_id "
                "WHERE heads.generation_id IS NULL AND ((generations.status="
                f"{marker} AND generations.created_at<{marker}) OR "
                f"(generations.status={marker} AND generations.published_at<{marker})) "
                f"ORDER BY generations.created_at,generations.generation_id LIMIT {limit}",
                (SOURCE_PREPARED, before, SOURCE_SUPERSEDED, before),
            ).fetchall()
        return [value for item in rows if (value := _row(item)) is not None]

    def delete_generation_objects(self, generation: Mapping[str, Any]) -> None:
        manifest_key = str(generation["manifest_key"])
        prefix = f"{manifest_key.rsplit('/', 1)[0]}/"
        keys = [item.key for item in self.object_store.list_prefix(prefix)]
        committed = f"{prefix}COMMITTED"
        if committed in keys:
            self.object_store.delete(committed)
        if manifest_key in keys:
            self.object_store.delete(manifest_key)
        for key in keys:
            if key not in {committed, manifest_key}:
                self.object_store.delete(key)

    def forget_collectable(self, generation_id: str, *, before: float) -> bool:
        marker = self.backend.sql(sqlite="?", postgres="%s")
        with self.backend.transaction(write=True) as connection:
            changed = connection.execute(
                f"DELETE FROM ha_source_generations WHERE generation_id={marker} "
                "AND NOT EXISTS(SELECT 1 FROM ha_source_heads WHERE generation_id="
                f"ha_source_generations.generation_id) AND ((status={marker} AND created_at<{marker}) "
                f"OR (status={marker} AND published_at<{marker}))",
                (
                    generation_id,
                    SOURCE_PREPARED,
                    before,
                    SOURCE_SUPERSEDED,
                    before,
                ),
            )
        return changed.rowcount == 1

    def check(self) -> bool:
        try:
            with self.backend.transaction() as connection:
                row = connection.execute(
                    "SELECT COUNT(*) AS row_count FROM ha_source_generations"
                ).fetchone()
            return row is not None and self.object_store.check()
        except Exception:
            return False


__all__ = [
    "SOURCE_ACTIVE",
    "SOURCE_PREPARED",
    "SOURCE_SUPERSEDED",
    "SourceGenerationConflict",
    "SourceGenerationError",
    "SourceGenerationStore",
]
