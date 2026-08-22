from __future__ import annotations

import hashlib
import json
import math
import os
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from cogdoc.ha.index_generation import GEN_PUBLISHED, normalize_manifest
from cogdoc.ha.migration_catalog import CURRENT_SCHEMA_VERSION
from cogdoc.ha.object_store import ObjectInfo, ObjectStore
from cogdoc.ha.source_generation import SOURCE_ACTIVE, SourceGenerationStore
from cogdoc.ha.storage import DatabaseBackend, DatabaseConnection
from cogdoc.source_model import build_version_id


RECOVERY_MANIFEST_FORMAT = "cogdoc-ha-recovery-v1"
MAX_RECOVERY_MANIFEST_BYTES = 64 * 1024 * 1024
MAX_RECOVERY_OBJECTS = 1_000_000


class RecoveryManifestError(RuntimeError):
    pass


def _mapping(row: Any | None) -> dict[str, Any] | None:
    if row is None:
        return None
    if isinstance(row, Mapping):
        return dict(row)
    keys = getattr(row, "keys", None)
    if callable(keys):
        return {str(key): row[key] for key in keys()}
    raise RecoveryManifestError("recovery database row mapping is unavailable")


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
        raise RecoveryManifestError("recovery manifest must be finite JSON") from exc


def _clean(value: object, field: str, maximum: int = 1024) -> str:
    text = str(value or "").strip()
    if (
        not text
        or len(text.encode()) > maximum
        or any(ord(char) < 32 or ord(char) == 127 for char in text)
    ):
        raise ValueError(f"{field} is invalid")
    return text


class HARecoveryManifest:
    """Auditable object inventory tied to one external database snapshot.

    The caller creates a PostgreSQL dump/snapshot first and supplies its stable
    identifier. The manifest records every object needed by current index and
    source heads plus every retained raw artifact, including S3 VersionId when
    available. This makes a DB/object recovery point verifiable instead of
    relying on an eventually consistent bucket listing.
    """

    def __init__(
        self,
        backend: DatabaseBackend,
        object_store: ObjectStore,
        source_generations: SourceGenerationStore,
        *,
        clock: Any = time.time,
    ) -> None:
        if source_generations.backend is not backend:
            raise ValueError("recovery stores must share one database backend")
        if source_generations.object_store is not object_store:
            raise ValueError("recovery stores must share one object store")
        self.backend = backend
        self.object_store = object_store
        self.source_generations = source_generations
        self._clock = clock

    @staticmethod
    def _index_base(row: Mapping[str, Any], *, prefix: str = "indexes") -> str:
        tenant = hashlib.sha256(str(row["tenant_id"]).encode()).hexdigest()
        kb = hashlib.sha256(str(row["kb_id"]).encode()).hexdigest()
        return f"{prefix}/{tenant}/{kb}/{row['generation_id']}"

    @staticmethod
    def _reference(
        *, kind: str, key: str, sha256: str, byte_size: int, info: ObjectInfo
    ) -> dict[str, Any]:
        if info.key != key or info.sha256 != sha256 or info.byte_size != byte_size:
            raise RecoveryManifestError(f"{kind} object does not match authority")
        return {
            "kind": kind,
            "key": key,
            "sha256": sha256,
            "byte_size": byte_size,
            "version_id": info.version_id,
            "etag": info.etag,
        }

    def _head(
        self, *, kind: str, key: str, sha256: str, byte_size: int
    ) -> dict[str, Any]:
        info = self.object_store.head(key)
        if info is None:
            raise RecoveryManifestError(f"{kind} object is missing")
        return self._reference(
            kind=kind, key=key, sha256=sha256, byte_size=byte_size, info=info
        )

    @staticmethod
    def _read_snapshot_rows(
        connection: DatabaseConnection,
    ) -> dict[str, list[dict[str, Any]]]:
        index_rows = [
            _mapping(row) or {}
            for row in connection.execute(
                "SELECT g.* FROM ha_index_heads h JOIN ha_index_generations g ON "
                "g.generation_id=h.current_generation_id ORDER BY g.tenant_id,g.kb_id"
            ).fetchall()
        ]
        source_rows = [
            _mapping(row) or {}
            for row in connection.execute(
                "SELECT g.* FROM ha_source_heads h JOIN ha_source_generations g ON "
                "g.generation_id=h.generation_id ORDER BY g.tenant_id,g.storage_id"
            ).fetchall()
        ]
        artifact_rows = [
            _mapping(row) or {}
            for row in connection.execute(
                "SELECT * FROM ha_source_artifacts ORDER BY tenant_id,kb_id,source_id,version_id"
            ).fetchall()
        ]
        migrations = [
            _mapping(row) or {}
            for row in connection.execute(
                "SELECT version,name,checksum,phase FROM ha_schema_migrations ORDER BY version"
            ).fetchall()
        ]
        deletions = [
            _mapping(row) or {}
            for row in connection.execute(
                "SELECT storage_id,tenant_id,kb_epoch,phase,index_generation_id,"
                "source_generation_id,artifact_versions,catalog_documents "
                "FROM ha_api_kb_deletions ORDER BY tenant_id,storage_id"
            ).fetchall()
        ]
        connector_commits = [
            _mapping(row) or {}
            for row in connection.execute(
                "SELECT job_id,tenant_id,kb_id,connection_id,phase,manifest_sha256 "
                "FROM ha_connector_commits ORDER BY tenant_id,kb_id,job_id"
            ).fetchall()
        ]
        return {
            "indexes": index_rows,
            "sources": source_rows,
            "artifacts": artifact_rows,
            "migrations": migrations,
            "deletions": deletions,
            "connector_commits": connector_commits,
        }

    def _snapshot_rows(
        self, connection: DatabaseConnection | None = None
    ) -> dict[str, list[dict[str, Any]]]:
        if connection is not None:
            return self._read_snapshot_rows(connection)
        with self.backend.transaction() as owned_connection:
            return self._read_snapshot_rows(owned_connection)

    def capture(
        self,
        database_snapshot_id: str,
        *,
        database_sha256: str | None = None,
        verify_content: bool = False,
        connection: DatabaseConnection | None = None,
    ) -> dict[str, Any]:
        snapshot_id = _clean(database_snapshot_id, "database_snapshot_id")
        database_digest = None
        if database_sha256 is not None:
            database_digest = str(database_sha256).strip().casefold()
            if len(database_digest) != 64 or any(
                char not in "0123456789abcdef" for char in database_digest
            ):
                raise ValueError("database_sha256 is invalid")
        rows = self._snapshot_rows(connection)
        migrations = rows["migrations"]
        if (
            not migrations
            or int(migrations[-1].get("version", 0)) < CURRENT_SCHEMA_VERSION
            or any(
                str(row.get("phase")) not in {"validated", "contracted"}
                for row in migrations
            )
        ):
            raise RecoveryManifestError(
                "database schema is not at a validated recovery version"
            )
        if any(row.get("phase") != "deleted" for row in rows["deletions"]):
            raise RecoveryManifestError(
                "knowledge-base deletion is incomplete at recovery point"
            )
        if rows["connector_commits"]:
            # These objects are intentionally short-lived recovery handoffs.
            # Unlike published source/index generations they have no retention
            # guarantee after a terminal worker finalizes, so an online backup
            # must retry after the committing window closes.
            raise RecoveryManifestError(
                "connector commit is incomplete at recovery point"
            )
        references: dict[str, dict[str, Any]] = {}

        def append(reference: dict[str, Any]) -> None:
            key = str(reference["key"])
            previous = references.get(key)
            if previous is not None and any(
                previous[field] != reference[field]
                for field in ("sha256", "byte_size", "version_id")
            ):
                raise RecoveryManifestError("one object key has conflicting authority")
            references[key] = reference
            if len(references) > MAX_RECOVERY_OBJECTS:
                raise RecoveryManifestError("recovery manifest object limit exceeded")

        index_heads: list[dict[str, Any]] = []
        for row in rows["indexes"]:
            if row.get("status") != GEN_PUBLISHED:
                raise RecoveryManifestError("index head is not published")
            raw_manifest = row.get("manifest_json")
            manifest_value = (
                json.loads(str(raw_manifest))
                if isinstance(raw_manifest, str)
                else row.get("manifest")
            )
            if not isinstance(manifest_value, Mapping):
                raise RecoveryManifestError("index manifest is invalid")
            manifest, manifest_digest = normalize_manifest(manifest_value)
            if manifest_digest != row.get("manifest_sha256"):
                raise RecoveryManifestError("index manifest disagrees with database")
            contract = manifest.get("contract")
            chunk_version = (
                str(contract.get("chunk_version") or "")
                if isinstance(contract, Mapping)
                else ""
            )
            derived = chunk_version == "derived-knowledge-v1"
            base = self._index_base(
                row,
                prefix="derived-knowledge-indexes" if derived else "indexes",
            )
            object_kind = "derived_index" if derived else "index"
            encoded = _canonical(manifest)
            append(
                self._head(
                    kind=f"{object_kind}_manifest",
                    key=f"{base}/manifest.json",
                    sha256=manifest_digest,
                    byte_size=len(encoded),
                )
            )
            for item in manifest["files"]:
                append(
                    self._head(
                        kind=f"{object_kind}_file",
                        key=f"{base}/files/{item['path']}",
                        sha256=str(item["sha256"]),
                        byte_size=int(item["byte_size"]),
                    )
                )
            index_heads.append(
                {
                    "tenant_id": row["tenant_id"],
                    "kb_id": row["kb_id"],
                    "generation_id": row["generation_id"],
                    "manifest_sha256": manifest_digest,
                    "index_kind": "derived_knowledge" if derived else "documents",
                }
            )

        source_heads: list[dict[str, Any]] = []
        for row in rows["sources"]:
            if row.get("status") != SOURCE_ACTIVE:
                raise RecoveryManifestError("source head is not active")
            manifest = self.source_generations._validated_manifest(
                row, str(row["storage_id"])
            )
            manifest_key = str(row["manifest_key"])
            manifest_bytes = _canonical(manifest)
            append(
                self._head(
                    kind="source_manifest",
                    key=manifest_key,
                    sha256=str(row["manifest_sha256"]),
                    byte_size=len(manifest_bytes),
                )
            )
            for item in manifest["files"]:
                append(
                    self._head(
                        kind="source_file",
                        key=str(item["object_key"]),
                        sha256=str(item["sha256"]),
                        byte_size=int(item["byte_size"]),
                    )
                )
            committed_key = f"{manifest_key.rsplit('/', 1)[0]}/COMMITTED"
            committed = self.object_store.head(committed_key)
            if committed is None:
                raise RecoveryManifestError("source commit marker is missing")
            append(
                self._reference(
                    kind="source_commit",
                    key=committed_key,
                    sha256=committed.sha256,
                    byte_size=committed.byte_size,
                    info=committed,
                )
            )
            source_heads.append(
                {
                    "tenant_id": row["tenant_id"],
                    "storage_id": row["storage_id"],
                    "generation_id": row["generation_id"],
                    "manifest_sha256": row["manifest_sha256"],
                    "kb_epoch": int(row["kb_epoch"]),
                }
            )

        for row in rows["artifacts"]:
            source_id = str(row["source_id"])
            version_id = str(row["version_id"])
            digest = str(row["content_sha256"])
            if version_id != build_version_id(source_id, digest):
                raise RecoveryManifestError(
                    "source artifact content address is invalid"
                )
            expected_key = (
                "source-artifacts/"
                f"{hashlib.sha256(str(row['tenant_id']).encode()).hexdigest()}/"
                f"{hashlib.sha256(str(row['kb_id']).encode()).hexdigest()}/"
                f"{hashlib.sha256(source_id.encode()).hexdigest()}/"
                f"{hashlib.sha256(version_id.encode()).hexdigest()}"
            )
            if str(row["object_key"]) != expected_key:
                raise RecoveryManifestError("source artifact object key is invalid")
            reference = self._head(
                kind="source_artifact",
                key=expected_key,
                sha256=digest,
                byte_size=int(row["byte_size"]),
            )
            recorded_version = str(row.get("object_version_id") or "") or None
            if recorded_version is not None and (
                reference["version_id"] != recorded_version
            ):
                raise RecoveryManifestError("source artifact object version changed")
            append(reference)

        ordered = [references[key] for key in sorted(references)]
        if verify_content:
            for reference in ordered:
                content_digest = hashlib.sha256()
                size = 0
                for chunk in self.object_store.iter_bytes(str(reference["key"])):
                    content_digest.update(chunk)
                    size += len(chunk)
                if (
                    size != reference["byte_size"]
                    or content_digest.hexdigest() != reference["sha256"]
                ):
                    raise RecoveryManifestError("object content verification failed")
        payload: dict[str, Any] = {
            "format": RECOVERY_MANIFEST_FORMAT,
            "created_at": float(self._clock()),
            "database_snapshot_id": snapshot_id,
            "database_sha256": database_digest,
            "migrations": rows["migrations"],
            "kb_deletions": rows["deletions"],
            "index_heads": index_heads,
            "source_heads": source_heads,
            "artifact_count": len(rows["artifacts"]),
            "objects": ordered,
        }
        payload["manifest_sha256"] = hashlib.sha256(_canonical(payload)).hexdigest()
        return payload

    def verify(
        self, manifest: Mapping[str, Any], *, verify_content: bool = False
    ) -> dict[str, int]:
        if (
            not isinstance(manifest, Mapping)
            or manifest.get("format") != RECOVERY_MANIFEST_FORMAT
        ):
            raise RecoveryManifestError("recovery manifest format is invalid")
        expected = str(manifest.get("manifest_sha256") or "")
        payload = dict(manifest)
        payload.pop("manifest_sha256", None)
        if hashlib.sha256(_canonical(payload)).hexdigest() != expected:
            raise RecoveryManifestError("recovery manifest checksum is invalid")
        created = manifest.get("created_at")
        if (
            isinstance(created, bool)
            or not isinstance(created, (int, float))
            or not math.isfinite(float(created))
        ):
            raise RecoveryManifestError("recovery manifest timestamp is invalid")
        objects = manifest.get("objects")
        if not isinstance(objects, list) or len(objects) > MAX_RECOVERY_OBJECTS:
            raise RecoveryManifestError("recovery manifest object list is invalid")
        seen: set[str] = set()
        total = 0
        for raw in objects:
            if not isinstance(raw, Mapping):
                raise RecoveryManifestError("recovery object entry is invalid")
            key = _clean(raw.get("key"), "object key", 2048)
            if key in seen:
                raise RecoveryManifestError("recovery object key is duplicated")
            seen.add(key)
            digest = str(raw.get("sha256") or "")
            size = raw.get("byte_size")
            if (
                len(digest) != 64
                or any(char not in "0123456789abcdef" for char in digest)
                or isinstance(size, bool)
                or not isinstance(size, int)
                or size < 0
            ):
                raise RecoveryManifestError("recovery object metadata is invalid")
            info = self.object_store.head(key)
            if info is None or info.sha256 != digest or info.byte_size != size:
                raise RecoveryManifestError("recovery object is missing or corrupt")
            recorded_version = str(raw.get("version_id") or "") or None
            if recorded_version is not None and info.version_id != recorded_version:
                raise RecoveryManifestError("recovery object version changed")
            if verify_content:
                builder = hashlib.sha256()
                actual_size = 0
                for chunk in self.object_store.iter_bytes(key):
                    builder.update(chunk)
                    actual_size += len(chunk)
                if actual_size != size or builder.hexdigest() != digest:
                    raise RecoveryManifestError("recovery object content is corrupt")
            total += size
        return {"objects": len(seen), "bytes": total}

    def verify_database_authority(self, manifest: Mapping[str, Any]) -> None:
        """Require the connected database to match the recorded recovery point."""

        snapshot_id = _clean(
            manifest.get("database_snapshot_id"), "database_snapshot_id"
        )
        database_sha256 = manifest.get("database_sha256")
        current = self.capture(
            snapshot_id,
            database_sha256=(
                str(database_sha256) if database_sha256 is not None else None
            ),
            verify_content=False,
        )
        for field in (
            "migrations",
            "kb_deletions",
            "index_heads",
            "source_heads",
            "artifact_count",
            "objects",
        ):
            if current.get(field) != manifest.get(field):
                raise RecoveryManifestError(
                    "restored database authority differs from recovery manifest"
                )

    @staticmethod
    def write(path: str | Path, manifest: Mapping[str, Any]) -> Path:
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        encoded = json.dumps(
            dict(manifest),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        ).encode()
        if len(encoded) > MAX_RECOVERY_MANIFEST_BYTES:
            raise RecoveryManifestError("recovery manifest file is too large")
        temporary = destination.with_name(f".{destination.name}.{time.time_ns()}.tmp")
        try:
            with temporary.open("xb") as handle:
                os.chmod(temporary, 0o600)
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(destination)
            descriptor = os.open(destination.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        finally:
            temporary.unlink(missing_ok=True)
        return destination

    @staticmethod
    def read(path: str | Path) -> dict[str, Any]:
        source = Path(path)
        try:
            if source.stat().st_size > MAX_RECOVERY_MANIFEST_BYTES:
                raise RecoveryManifestError("recovery manifest file is too large")
            value = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RecoveryManifestError("recovery manifest cannot be read") from exc
        if not isinstance(value, dict):
            raise RecoveryManifestError("recovery manifest root is invalid")
        return value


__all__ = [
    "HARecoveryManifest",
    "MAX_RECOVERY_MANIFEST_BYTES",
    "MAX_RECOVERY_OBJECTS",
    "RECOVERY_MANIFEST_FORMAT",
    "RecoveryManifestError",
]
