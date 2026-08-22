from __future__ import annotations

import difflib
import hashlib
import json
import math
import tempfile
import threading
import time
from collections.abc import Iterable, Mapping
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO
from uuid import uuid4

from cogdoc.ha.object_store import (
    ObjectConflict,
    ObjectIntegrityError,
    ObjectNotFound,
    ObjectStore,
    ObjectStoreError,
)
from cogdoc.ha.storage import DatabaseBackend, DatabaseConnection
from cogdoc.service.source_artifact_store import (
    ArtifactConflictError,
    ArtifactIntegrityError,
    ArtifactLimitError,
    ArtifactNotFoundError,
    _required_scope,
    _safe_token,
    _sha256,
)
from cogdoc.source_model import build_version_id


_TEXT_MEDIA_TYPES = frozenset(
    {
        "application/csv",
        "application/json",
        "application/ld+json",
        "application/markdown",
        "application/sql",
        "application/toml",
        "application/xml",
        "application/x-ndjson",
        "application/x-yaml",
        "application/yaml",
        "image/svg+xml",
    }
)
_TEXT_SUFFIXES = frozenset(
    {
        ".csv",
        ".htm",
        ".html",
        ".json",
        ".md",
        ".rst",
        ".sql",
        ".toml",
        ".tsv",
        ".txt",
        ".xml",
        ".yaml",
        ".yml",
    }
)
_READ_CHUNK = 1024 * 1024


def _positive(value: int, field: str) -> int:
    if isinstance(value, bool) or int(value) < 1:
        raise ValueError(f"{field} must be positive")
    return int(value)


class DistributedSourceArtifactStore:
    """Cluster-authoritative immutable source artifacts.

    PostgreSQL owns lifecycle, capacity and reservation authority. Object keys
    are immutable and contain only hashes of tenant-controlled identifiers.
    Upload-before-row publication makes a database failure produce a harmless
    orphan object, never an active row pointing at partially uploaded bytes.
    """

    def __init__(
        self,
        backend: DatabaseBackend,
        object_store: ObjectStore,
        *,
        owner_id: str,
        max_file_bytes: int = 64 * 1024 * 1024,
        max_total_bytes: int = 2 * 1024 * 1024 * 1024,
        max_bytes_per_tenant: int | None = None,
        max_versions_per_source: int = 50,
        user_max_versions_per_source: int | None = None,
        max_diff_bytes: int = 256 * 1024,
        max_diff_lines: int = 5_000,
        reservation_lease_seconds: float = 3600.0,
        clock: Any = time.time,
    ) -> None:
        if not owner_id or len(owner_id.encode()) > 255:
            raise ValueError("artifact owner_id is invalid")
        if (
            not math.isfinite(reservation_lease_seconds)
            or not 60 <= reservation_lease_seconds <= 86_400
        ):
            raise ValueError("artifact reservation lease must be between 60 and 86400")
        self.backend = backend
        self.object_store = object_store
        self.owner_id = owner_id
        self.max_file_bytes = _positive(max_file_bytes, "max_file_bytes")
        self.max_total_bytes = _positive(max_total_bytes, "max_total_bytes")
        self.max_bytes_per_tenant = _positive(
            max_total_bytes if max_bytes_per_tenant is None else max_bytes_per_tenant,
            "max_bytes_per_tenant",
        )
        self.max_versions_per_source = _positive(
            max_versions_per_source, "max_versions_per_source"
        )
        self.user_max_versions_per_source = _positive(
            max_versions_per_source
            if user_max_versions_per_source is None
            else user_max_versions_per_source,
            "user_max_versions_per_source",
        )
        if self.user_max_versions_per_source > self.max_versions_per_source:
            raise ValueError(
                "user_max_versions_per_source cannot exceed max_versions_per_source"
            )
        self.max_diff_bytes = _positive(max_diff_bytes, "max_diff_bytes")
        self.max_diff_lines = _positive(max_diff_lines, "max_diff_lines")
        self.reservation_lease_seconds = float(reservation_lease_seconds)
        self._clock = clock
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_error: BaseException | None = None
        with backend.transaction(write=True) as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS ha_source_artifact_locks (
                lock_id TEXT PRIMARY KEY,revision BIGINT NOT NULL,
                updated_at DOUBLE PRECISION NOT NULL)"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS ha_source_artifacts (
                tenant_id TEXT NOT NULL,kb_id TEXT NOT NULL,source_id TEXT NOT NULL,
                version_id TEXT NOT NULL,object_key TEXT NOT NULL UNIQUE,
                content_sha256 TEXT NOT NULL,byte_size BIGINT NOT NULL,
                media_type TEXT NOT NULL,display_name TEXT,created_at DOUBLE PRECISION NOT NULL,
                recovery_token TEXT UNIQUE,deleted_at DOUBLE PRECISION,
                object_version_id TEXT,object_etag TEXT,
                PRIMARY KEY(tenant_id,kb_id,source_id,version_id))"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS ha_source_artifact_reservations (
                token TEXT PRIMARY KEY,tenant_id TEXT NOT NULL,kb_id TEXT NOT NULL,
                reservation_key TEXT NOT NULL,fingerprint TEXT NOT NULL,
                lease_owner TEXT NOT NULL,lease_expires_at DOUBLE PRECISION NOT NULL,
                created_at DOUBLE PRECISION NOT NULL,
                UNIQUE(tenant_id,kb_id,reservation_key))"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS ha_source_artifact_reservation_items (
                token TEXT NOT NULL,tenant_id TEXT NOT NULL,kb_id TEXT NOT NULL,
                source_id TEXT NOT NULL,version_id TEXT NOT NULL,metadata_json TEXT NOT NULL,
                reserved_bytes BIGINT NOT NULL,reserved_version INTEGER NOT NULL,
                consumed INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(token,source_id,version_id),
                UNIQUE(tenant_id,kb_id,source_id,version_id))"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS ha_source_artifact_uploads (
                object_key TEXT PRIMARY KEY,tenant_id TEXT NOT NULL,kb_id TEXT NOT NULL,
                source_id TEXT NOT NULL,version_id TEXT NOT NULL,metadata_json TEXT NOT NULL,
                reservation_token TEXT,reserved_bytes BIGINT NOT NULL,
                lease_owner TEXT NOT NULL,lease_expires_at DOUBLE PRECISION NOT NULL,
                created_at DOUBLE PRECISION NOT NULL,
                UNIQUE(tenant_id,kb_id,source_id,version_id))"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS ha_source_artifact_scopes (
                tenant_id TEXT NOT NULL,kb_id TEXT NOT NULL,state TEXT NOT NULL,
                kb_epoch BIGINT NOT NULL DEFAULT 0,
                updated_at DOUBLE PRECISION NOT NULL,
                PRIMARY KEY(tenant_id,kb_id))"""
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_ha_source_artifact_scope ON "
                "ha_source_artifacts(tenant_id,kb_id,source_id,deleted_at,created_at)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_ha_source_artifact_trash ON "
                "ha_source_artifacts(tenant_id,kb_id,deleted_at)"
            )

    def _marker(self) -> str:
        return self.backend.sql(sqlite="?", postgres="%s")

    def _markers(self, count: int) -> str:
        return ",".join(self._marker() for _ in range(count))

    @staticmethod
    def _mapping(row: Any | None) -> dict[str, Any] | None:
        if row is None:
            return None
        if isinstance(row, Mapping):
            return dict(row)
        keys = getattr(row, "keys", None)
        if callable(keys):
            return {str(key): row[key] for key in keys()}
        raise RuntimeError("artifact database row mapping is unavailable")

    def _global_lock(self, connection: DatabaseConnection) -> None:
        now = float(self._clock())
        insert = self.backend.sql(
            sqlite="INSERT OR IGNORE INTO ha_source_artifact_locks(lock_id,revision,updated_at) VALUES('global',0,?)",
            postgres="INSERT INTO ha_source_artifact_locks(lock_id,revision,updated_at) VALUES('global',0,%s) ON CONFLICT(lock_id) DO NOTHING",
        )
        connection.execute(insert, (now,))
        suffix = self.backend.sql(sqlite="", postgres=" FOR UPDATE")
        if (
            connection.execute(
                f"SELECT revision FROM ha_source_artifact_locks WHERE lock_id='global'{suffix}"
            ).fetchone()
            is None
        ):
            raise RuntimeError("artifact capacity lock is unavailable")

    def _scope_writable_locked(
        self, connection: DatabaseConnection, tenant_id: str, kb_id: str
    ) -> None:
        marker = self._marker()
        row = self._mapping(
            connection.execute(
                "SELECT state FROM ha_source_artifact_scopes WHERE tenant_id="
                f"{marker} AND kb_id={marker}",
                (tenant_id, kb_id),
            ).fetchone()
        )
        if row is not None and str(row["state"]) != "active":
            raise ArtifactConflictError("artifact scope is not writable")

    def activate_scope(self, tenant_id: str, kb_id: str, *, kb_epoch: int) -> None:
        tenant = _required_scope(tenant_id, "tenant_id")
        kb = _required_scope(kb_id, "kb_id")
        if type(kb_epoch) is not int or kb_epoch < 1:
            raise ValueError("artifact scope KB epoch is invalid")
        marker = self._marker()
        lock = self.backend.sql(sqlite="", postgres=" FOR UPDATE")
        now = float(self._clock())
        with self.backend.transaction(write=True) as connection:
            self._global_lock(connection)
            row = self._mapping(
                connection.execute(
                    "SELECT state,kb_epoch FROM ha_source_artifact_scopes WHERE tenant_id="
                    f"{marker} AND kb_id={marker}{lock}",
                    (tenant, kb),
                ).fetchone()
            )
            if row is not None and int(row["kb_epoch"]) >= kb_epoch:
                if int(row["kb_epoch"]) == kb_epoch and row["state"] == "active":
                    return
                raise ArtifactConflictError("artifact scope incarnation is stale")
            insert = self.backend.sql(
                sqlite=(
                    "INSERT INTO ha_source_artifact_scopes(tenant_id,kb_id,state,kb_epoch,updated_at) "
                    "VALUES(?,?,?,?,?) ON CONFLICT(tenant_id,kb_id) DO UPDATE SET "
                    "state=excluded.state,kb_epoch=excluded.kb_epoch,updated_at=excluded.updated_at"
                ),
                postgres=(
                    "INSERT INTO ha_source_artifact_scopes(tenant_id,kb_id,state,kb_epoch,updated_at) "
                    "VALUES(%s,%s,%s,%s,%s) ON CONFLICT(tenant_id,kb_id) DO UPDATE SET "
                    "state=EXCLUDED.state,kb_epoch=EXCLUDED.kb_epoch,updated_at=EXCLUDED.updated_at"
                ),
            )
            connection.execute(insert, (tenant, kb, "active", kb_epoch, now))

    @staticmethod
    def _scope_hash(value: str) -> str:
        return hashlib.sha256(value.encode()).hexdigest()

    def _object_key(
        self, tenant_id: str, kb_id: str, source_id: str, version_id: str
    ) -> str:
        return (
            "source-artifacts/"
            f"{self._scope_hash(tenant_id)}/{self._scope_hash(kb_id)}/"
            f"{self._scope_hash(source_id)}/{self._scope_hash(version_id)}"
        )

    def _identity(
        self, tenant_id: str, kb_id: str, source_id: str, version_id: str
    ) -> tuple[str, str, str, str]:
        return (
            _required_scope(tenant_id, "tenant_id"),
            _required_scope(kb_id, "kb_id"),
            _safe_token(source_id, "source_id"),
            _safe_token(version_id, "version_id"),
        )

    def _metadata(
        self,
        tenant_id: str,
        kb_id: str,
        source_id: str,
        version_id: str,
        *,
        content_sha256: object,
        byte_size: object,
        media_type: object,
        display_name: object,
        created_at: object,
    ) -> dict[str, Any]:
        digest = _sha256(content_sha256)
        if version_id != build_version_id(source_id, digest):
            raise ArtifactIntegrityError(
                "artifact version_id does not match its content address"
            )
        if (
            isinstance(byte_size, bool)
            or not isinstance(byte_size, int)
            or byte_size < 0
        ):
            raise ValueError("byte_size must be a non-negative integer")
        if byte_size > self.max_file_bytes:
            raise ArtifactLimitError("artifact exceeds max_file_bytes")
        normalized_media = str(media_type or "").split(";", 1)[0].strip().casefold()
        if not normalized_media or len(normalized_media) > 255:
            raise ValueError("media_type is invalid")
        normalized_name = None
        if display_name is not None:
            normalized_name = str(display_name).strip()
            if (
                not normalized_name
                or len(normalized_name) > 1024
                or "\x00" in normalized_name
            ):
                raise ValueError("display_name is invalid")
        if isinstance(created_at, bool):
            raise ValueError("created_at must be a finite non-negative timestamp")
        try:
            timestamp = float(str(created_at))
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "created_at must be a finite non-negative timestamp"
            ) from exc
        if not math.isfinite(timestamp) or timestamp < 0:
            raise ValueError("created_at must be a finite non-negative timestamp")
        return {
            "tenant_id": tenant_id,
            "kb_id": kb_id,
            "source_id": source_id,
            "version_id": version_id,
            "content_sha256": digest,
            "byte_size": byte_size,
            "media_type": normalized_media,
            "display_name": normalized_name,
            "created_at": timestamp,
        }

    @staticmethod
    def _public(row: Mapping[str, Any]) -> dict[str, Any]:
        return {
            key: row.get(key)
            for key in (
                "tenant_id",
                "kb_id",
                "source_id",
                "version_id",
                "content_sha256",
                "byte_size",
                "media_type",
                "display_name",
                "created_at",
            )
        }

    def _expire_locked(self, connection: DatabaseConnection, now: float) -> None:
        marker = self._marker()
        expired = connection.execute(
            f"SELECT token FROM ha_source_artifact_reservations WHERE lease_expires_at<={marker}",
            (now,),
        ).fetchall()
        for row in expired:
            token = str(self._mapping(row)["token"])  # type: ignore[index]
            connection.execute(
                f"DELETE FROM ha_source_artifact_reservation_items WHERE token={marker}",
                (token,),
            )
            connection.execute(
                f"DELETE FROM ha_source_artifact_reservations WHERE token={marker}",
                (token,),
            )
        # Expired upload intents remain as a bounded, indexed orphan-GC ledger.
        # They no longer reserve capacity and may be atomically taken over by a
        # retry, while maintenance does not need to scan every object in S3.
        connection.execute(
            "UPDATE ha_source_artifact_uploads SET reserved_bytes=0 WHERE "
            f"lease_expires_at<={marker} AND reserved_bytes<>0",
            (now,),
        )

    def _usage_locked(
        self, connection: DatabaseConnection, tenant_id: str | None = None
    ) -> int:
        marker = self._marker()
        where = f" WHERE tenant_id={marker}" if tenant_id is not None else ""
        params = (tenant_id,) if tenant_id is not None else None
        query = (
            "SELECT COALESCE(SUM(byte_size),0) AS value FROM ha_source_artifacts"
            + where
        )
        cursor = (
            connection.execute(query, params)
            if params is not None
            else connection.execute(query)
        )
        row = self._mapping(cursor.fetchone())
        return int((row or {}).get("value", 0))

    def _reserved_locked(
        self, connection: DatabaseConnection, tenant_id: str | None = None
    ) -> int:
        marker = self._marker()
        where = " WHERE consumed=0"
        params: tuple[Any, ...] | None = None
        if tenant_id is not None:
            where += f" AND tenant_id={marker}"
            params = (tenant_id,)
        query = (
            "SELECT COALESCE(SUM(reserved_bytes),0) AS value "
            "FROM ha_source_artifact_reservation_items" + where
        )
        cursor = (
            connection.execute(query, params)
            if params is not None
            else connection.execute(query)
        )
        row = self._mapping(cursor.fetchone())
        upload_where = ""
        upload_params: tuple[Any, ...] | None = None
        if tenant_id is not None:
            upload_where = f" WHERE tenant_id={marker}"
            upload_params = (tenant_id,)
        upload_query = (
            "SELECT COALESCE(SUM(reserved_bytes),0) AS value "
            "FROM ha_source_artifact_uploads" + upload_where
        )
        upload_cursor = (
            connection.execute(upload_query, upload_params)
            if upload_params is not None
            else connection.execute(upload_query)
        )
        upload = self._mapping(upload_cursor.fetchone())
        return int((row or {}).get("value", 0)) + int((upload or {}).get("value", 0))

    def _capacity_locked(
        self, connection: DatabaseConnection, tenant_id: str, additional: int
    ) -> None:
        if (
            self._usage_locked(connection)
            + self._reserved_locked(connection)
            + additional
            > self.max_total_bytes
        ):
            raise ArtifactLimitError("artifact store exceeds max_total_bytes")
        if (
            self._usage_locked(connection, tenant_id)
            + self._reserved_locked(connection, tenant_id)
            + additional
            > self.max_bytes_per_tenant
        ):
            raise ArtifactLimitError(
                "tenant artifact storage exceeds max_bytes_per_tenant"
            )

    def _active_count_locked(
        self, connection: DatabaseConnection, tenant: str, kb: str, source: str
    ) -> int:
        m = self._marker()
        row = self._mapping(
            connection.execute(
                "SELECT COUNT(*) AS value FROM ha_source_artifacts WHERE "
                f"tenant_id={m} AND kb_id={m} AND source_id={m} AND deleted_at IS NULL",
                (tenant, kb, source),
            ).fetchone()
        )
        return int((row or {}).get("value", 0))

    def reserve_batch(
        self,
        tenant_id: str,
        kb_id: str,
        artifacts: Iterable[Mapping[str, Any]],
        *,
        reservation_key: str,
    ) -> str:
        tenant = _required_scope(tenant_id, "tenant_id")
        kb = _required_scope(kb_id, "kb_id")
        key = _safe_token(reservation_key, "reservation_key")
        entries: dict[tuple[str, str], tuple[dict[str, Any], str]] = {}
        for raw in artifacts:
            if not isinstance(raw, Mapping):
                raise TypeError("reserved artifact must be a mapping")
            if "created_at" not in raw:
                raise ValueError("reserved artifact created_at is required")
            source = _safe_token(raw.get("source_id"), "source_id")
            version = _safe_token(raw.get("version_id"), "version_id")
            metadata = self._metadata(
                tenant,
                kb,
                source,
                version,
                content_sha256=raw.get("content_sha256"),
                byte_size=raw.get("byte_size"),
                media_type=raw.get("media_type", "application/octet-stream"),
                display_name=raw.get("display_name"),
                created_at=raw.get("created_at"),
            )
            encoded = json.dumps(
                metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            previous = entries.get((source, version))
            if previous is not None and previous[1] != encoded:
                raise ArtifactConflictError("reservation contains conflicting metadata")
            entries[(source, version)] = (metadata, encoded)
        fingerprint = hashlib.sha256(
            b"cogdoc-ha-artifact-reservation-v1\0"
            + b"\0".join(entries[item][1].encode() for item in sorted(entries))
        ).hexdigest()
        now = float(self._clock())
        m = self._marker()
        with self.backend.transaction(write=True) as connection:
            self._global_lock(connection)
            self._expire_locked(connection, now)
            self._scope_writable_locked(connection, tenant, kb)
            existing = self._mapping(
                connection.execute(
                    "SELECT token,fingerprint,lease_owner FROM ha_source_artifact_reservations "
                    f"WHERE tenant_id={m} AND kb_id={m} AND reservation_key={m}",
                    (tenant, kb, key),
                ).fetchone()
            )
            if existing is not None:
                if str(existing["fingerprint"]) != fingerprint:
                    raise ArtifactConflictError(
                        "artifact reservation key identifies another batch"
                    )
                token = str(existing["token"])
                connection.execute(
                    "UPDATE ha_source_artifact_reservations SET lease_owner="
                    f"{m},lease_expires_at={m} WHERE token={m}",
                    (self.owner_id, now + self.reservation_lease_seconds, token),
                )
                return token
            requested = 0
            requested_by_source: dict[str, int] = {}
            prepared: list[tuple[str, str, str, int, int]] = []
            for (source, version), (metadata, encoded) in entries.items():
                row = self._mapping(
                    connection.execute(
                        "SELECT content_sha256,byte_size,deleted_at FROM ha_source_artifacts WHERE "
                        f"tenant_id={m} AND kb_id={m} AND source_id={m} AND version_id={m}",
                        (tenant, kb, source, version),
                    ).fetchone()
                )
                reserve = 0
                reserve_version = 0
                if row is not None:
                    if (
                        str(row["content_sha256"]) != metadata["content_sha256"]
                        or int(row["byte_size"]) != metadata["byte_size"]
                    ):
                        raise ArtifactConflictError("existing source version conflicts")
                    if row.get("deleted_at") is not None:
                        reserve_version = 1
                        requested_by_source[source] = (
                            requested_by_source.get(source, 0) + 1
                        )
                else:
                    reserve = int(metadata["byte_size"])
                    reserve_version = 1
                    requested += reserve
                    requested_by_source[source] = requested_by_source.get(source, 0) + 1
                prepared.append((source, version, encoded, reserve, reserve_version))
            for source, count in requested_by_source.items():
                reserved = self._mapping(
                    connection.execute(
                        "SELECT COUNT(*) AS value FROM ha_source_artifact_reservation_items "
                        f"WHERE tenant_id={m} AND kb_id={m} AND source_id={m} AND consumed=0 AND reserved_version=1",
                        (tenant, kb, source),
                    ).fetchone()
                )
                if (
                    self._active_count_locked(connection, tenant, kb, source)
                    + int((reserved or {}).get("value", 0))
                    + count
                    > self.max_versions_per_source
                ):
                    raise ArtifactLimitError("source exceeds max_versions_per_source")
            self._capacity_locked(connection, tenant, requested)
            token = f"res-{uuid4().hex}"
            connection.execute(
                "INSERT INTO ha_source_artifact_reservations(token,tenant_id,kb_id,reservation_key,"
                f"fingerprint,lease_owner,lease_expires_at,created_at) VALUES({self._markers(8)})",
                (
                    token,
                    tenant,
                    kb,
                    key,
                    fingerprint,
                    self.owner_id,
                    now + self.reservation_lease_seconds,
                    now,
                ),
            )
            for source, version, encoded, reserve, reserve_version in prepared:
                try:
                    connection.execute(
                        "INSERT INTO ha_source_artifact_reservation_items(token,tenant_id,kb_id,source_id,"
                        f"version_id,metadata_json,reserved_bytes,reserved_version,consumed) VALUES({self._markers(9)})",
                        (
                            token,
                            tenant,
                            kb,
                            source,
                            version,
                            encoded,
                            reserve,
                            reserve_version,
                            0,
                        ),
                    )
                except Exception as exc:
                    raise ArtifactConflictError(
                        "source version is reserved by another batch"
                    ) from exc
            return token

    def release_reservation(self, reservation_token: str | None) -> None:
        if reservation_token is None:
            return
        token = _safe_token(reservation_token, "reservation_token")
        m = self._marker()
        with self.backend.transaction(write=True) as connection:
            self._global_lock(connection)
            connection.execute(
                f"DELETE FROM ha_source_artifact_reservation_items WHERE token={m}",
                (token,),
            )
            connection.execute(
                f"DELETE FROM ha_source_artifact_reservations WHERE token={m}", (token,)
            )

    def reservation_usage(self, tenant_id: str, kb_id: str) -> dict[str, int]:
        tenant = _required_scope(tenant_id, "tenant_id")
        kb = _required_scope(kb_id, "kb_id")
        m = self._marker()
        now = float(self._clock())
        with self.backend.transaction(write=True) as connection:
            self._global_lock(connection)
            self._expire_locked(connection, now)
            reservations = self._mapping(
                connection.execute(
                    "SELECT COUNT(*) AS value FROM ha_source_artifact_reservations WHERE tenant_id="
                    f"{m} AND kb_id={m}",
                    (tenant, kb),
                ).fetchone()
            )
            remaining = self._mapping(
                connection.execute(
                    "SELECT COUNT(*) AS versions,COALESCE(SUM(reserved_bytes),0) AS bytes "
                    "FROM ha_source_artifact_reservation_items WHERE tenant_id="
                    f"{m} AND kb_id={m} AND consumed=0 AND reserved_version=1",
                    (tenant, kb),
                ).fetchone()
            )
        return {
            "reservations": int((reservations or {}).get("value", 0)),
            "reserved_versions": int((remaining or {}).get("versions", 0)),
            "reserved_bytes": int((remaining or {}).get("bytes", 0)),
        }

    def put(
        self,
        tenant_id: str,
        kb_id: str,
        source_id: str,
        version_id: str,
        content: bytes,
        *,
        content_sha256: str,
        media_type: str,
        display_name: str | None,
        created_at: float,
        reservation_token: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(content, bytes):
            raise TypeError("content must be bytes")
        tenant, kb, source, version = self._identity(
            tenant_id, kb_id, source_id, version_id
        )
        metadata = self._metadata(
            tenant,
            kb,
            source,
            version,
            content_sha256=content_sha256,
            byte_size=len(content),
            media_type=media_type,
            display_name=display_name,
            created_at=created_at,
        )
        if hashlib.sha256(content).hexdigest() != metadata["content_sha256"]:
            raise ArtifactIntegrityError("source artifact content hash does not match")
        token = (
            _safe_token(reservation_token, "reservation_token")
            if reservation_token
            else None
        )
        key = self._object_key(tenant, kb, source, version)
        m = self._marker()
        encoded = json.dumps(
            metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        now = float(self._clock())
        with self.backend.transaction(write=True) as connection:
            self._global_lock(connection)
            self._expire_locked(connection, now)
            self._scope_writable_locked(connection, tenant, kb)
            existing = self._mapping(
                connection.execute(
                    "SELECT * FROM ha_source_artifacts WHERE tenant_id="
                    f"{m} AND kb_id={m} AND source_id={m} AND version_id={m}",
                    (tenant, kb, source, version),
                ).fetchone()
            )
            if existing is not None:
                if str(existing["content_sha256"]) != metadata["content_sha256"] or int(
                    existing["byte_size"]
                ) != len(content):
                    raise ArtifactConflictError("existing source version conflicts")
                if existing.get("deleted_at") is not None:
                    if token is None:
                        if (
                            self._active_count_locked(connection, tenant, kb, source)
                            >= self.max_versions_per_source
                        ):
                            raise ArtifactLimitError(
                                "source exceeds max_versions_per_source"
                            )
                    else:
                        self._reservation_item_locked(
                            connection, token, tenant, kb, source, version, encoded
                        )
                    self._verify_head(existing)
                    connection.execute(
                        "UPDATE ha_source_artifacts SET recovery_token=NULL,deleted_at=NULL WHERE "
                        f"tenant_id={m} AND kb_id={m} AND source_id={m} AND version_id={m} AND deleted_at IS NOT NULL",
                        (tenant, kb, source, version),
                    )
                    if token is not None:
                        self._consume_locked(
                            connection, token, tenant, kb, source, version, encoded
                        )
                    return self._public(existing)
                if token is not None:
                    self._consume_locked(
                        connection, token, tenant, kb, source, version, encoded
                    )
                self._verify_head(existing)
                return self._public(existing)
            upload = self._mapping(
                connection.execute(
                    f"SELECT * FROM ha_source_artifact_uploads WHERE object_key={m}",
                    (key,),
                ).fetchone()
            )
            if upload is not None and float(upload["lease_expires_at"]) <= now:
                connection.execute(
                    f"DELETE FROM ha_source_artifact_uploads WHERE object_key={m}",
                    (key,),
                )
                upload = None
            if upload is not None:
                if (
                    str(upload["metadata_json"]) != encoded
                    or str(upload["lease_owner"]) != self.owner_id
                    or (upload.get("reservation_token") or None) != token
                ):
                    raise ArtifactConflictError(
                        "source version upload is already in progress"
                    )
                connection.execute(
                    "UPDATE ha_source_artifact_uploads SET lease_expires_at="
                    f"{m} WHERE object_key={m}",
                    (now + self.reservation_lease_seconds, key),
                )
            else:
                reserved_bytes = 0
                if token is None:
                    if (
                        self._active_count_locked(connection, tenant, kb, source)
                        >= self.max_versions_per_source
                    ):
                        raise ArtifactLimitError(
                            "source exceeds max_versions_per_source"
                        )
                    self._capacity_locked(connection, tenant, len(content))
                    reserved_bytes = len(content)
                else:
                    self._reservation_item_locked(
                        connection, token, tenant, kb, source, version, encoded
                    )
                connection.execute(
                    "INSERT INTO ha_source_artifact_uploads(object_key,tenant_id,kb_id,source_id,"
                    "version_id,metadata_json,reservation_token,reserved_bytes,lease_owner,"
                    f"lease_expires_at,created_at) VALUES({self._markers(11)})",
                    (
                        key,
                        tenant,
                        kb,
                        source,
                        version,
                        encoded,
                        token,
                        reserved_bytes,
                        self.owner_id,
                        now + self.reservation_lease_seconds,
                        now,
                    ),
                )
        temporary_name: str | None = None
        try:
            if len(content) <= 16 * 1024 * 1024:
                info = self.object_store.put_bytes(
                    key, content, sha256=metadata["content_sha256"]
                )
            else:
                with tempfile.NamedTemporaryFile(
                    mode="w+b", prefix="cogdoc-ha-artifact-", delete=False
                ) as temporary:
                    temporary.write(content)
                    temporary.flush()
                    temporary_name = temporary.name
                info = self.object_store.put_file(
                    key, Path(temporary_name), sha256=metadata["content_sha256"]
                )
        except (ObjectConflict, ObjectIntegrityError) as exc:
            raise ArtifactIntegrityError(str(exc)) from exc
        except ObjectStoreError as exc:
            raise ArtifactIntegrityError("source artifact upload failed") from exc
        finally:
            if temporary_name is not None:
                Path(temporary_name).unlink(missing_ok=True)
        if info.byte_size != len(content) or info.sha256 != metadata["content_sha256"]:
            raise ArtifactIntegrityError("uploaded artifact metadata does not match")
        with self.backend.transaction(write=True) as connection:
            self._global_lock(connection)
            self._expire_locked(connection, float(self._clock()))
            self._scope_writable_locked(connection, tenant, kb)
            existing = self._mapping(
                connection.execute(
                    "SELECT * FROM ha_source_artifacts WHERE tenant_id="
                    f"{m} AND kb_id={m} AND source_id={m} AND version_id={m}",
                    (tenant, kb, source, version),
                ).fetchone()
            )
            if existing is not None:
                if str(existing["content_sha256"]) != metadata["content_sha256"] or int(
                    existing["byte_size"]
                ) != len(content):
                    raise ArtifactConflictError("existing source version conflicts")
                if existing.get("deleted_at") is not None:
                    raise ArtifactConflictError("source version is soft-deleted")
                if token is not None:
                    self._consume_locked(
                        connection, token, tenant, kb, source, version, encoded
                    )
                connection.execute(
                    f"DELETE FROM ha_source_artifact_uploads WHERE object_key={m}",
                    (key,),
                )
                return self._public(existing)
            upload = self._mapping(
                connection.execute(
                    f"SELECT * FROM ha_source_artifact_uploads WHERE object_key={m}",
                    (key,),
                ).fetchone()
            )
            if (
                upload is None
                or str(upload["metadata_json"]) != encoded
                or str(upload["lease_owner"]) != self.owner_id
                or (upload.get("reservation_token") or None) != token
            ):
                raise ArtifactConflictError("artifact upload authority expired")
            if token is not None:
                self._reservation_item_locked(
                    connection, token, tenant, kb, source, version, encoded
                )
            connection.execute(
                "INSERT INTO ha_source_artifacts(tenant_id,kb_id,source_id,version_id,object_key,"
                "content_sha256,byte_size,media_type,display_name,created_at,recovery_token,deleted_at,"
                f"object_version_id,object_etag) VALUES({self._markers(14)})",
                (
                    tenant,
                    kb,
                    source,
                    version,
                    key,
                    metadata["content_sha256"],
                    len(content),
                    metadata["media_type"],
                    metadata["display_name"],
                    metadata["created_at"],
                    None,
                    None,
                    info.version_id,
                    info.etag,
                ),
            )
            if token is not None:
                self._consume_locked(
                    connection, token, tenant, kb, source, version, encoded
                )
            connection.execute(
                f"DELETE FROM ha_source_artifact_uploads WHERE object_key={m}", (key,)
            )
        return metadata

    def _reservation_item_locked(
        self,
        connection: DatabaseConnection,
        token: str,
        tenant: str,
        kb: str,
        source: str,
        version: str,
        encoded: str,
    ) -> dict[str, Any]:
        m = self._marker()
        row = self._mapping(
            connection.execute(
                "SELECT i.metadata_json,i.consumed,r.lease_owner,r.lease_expires_at FROM "
                "ha_source_artifact_reservation_items i JOIN ha_source_artifact_reservations r ON "
                f"r.token=i.token WHERE i.token={m} AND i.tenant_id={m} AND i.kb_id={m} AND i.source_id={m} AND i.version_id={m}",
                (token, tenant, kb, source, version),
            ).fetchone()
        )
        if (
            row is None
            or str(row["metadata_json"]) != encoded
            or str(row["lease_owner"]) != self.owner_id
            or float(row["lease_expires_at"]) <= float(self._clock())
        ):
            raise ArtifactConflictError("artifact reservation is unavailable")
        return row

    def _consume_locked(
        self,
        connection: DatabaseConnection,
        token: str,
        tenant: str,
        kb: str,
        source: str,
        version: str,
        encoded: str,
    ) -> None:
        row = self._reservation_item_locked(
            connection, token, tenant, kb, source, version, encoded
        )
        if not int(row["consumed"]):
            m = self._marker()
            connection.execute(
                "UPDATE ha_source_artifact_reservation_items SET consumed=1,reserved_bytes=0,reserved_version=0 WHERE "
                f"token={m} AND source_id={m} AND version_id={m}",
                (token, source, version),
            )

    def _row(
        self,
        tenant: str,
        kb: str,
        source: str,
        version: str,
        *,
        include_deleted: bool = False,
    ) -> dict[str, Any]:
        m = self._marker()
        deleted = "" if include_deleted else " AND deleted_at IS NULL"
        with self.backend.transaction() as connection:
            row = self._mapping(
                connection.execute(
                    "SELECT * FROM ha_source_artifacts WHERE tenant_id="
                    f"{m} AND kb_id={m} AND source_id={m} AND version_id={m}{deleted}",
                    (tenant, kb, source, version),
                ).fetchone()
            )
        if row is None:
            raise ArtifactNotFoundError("source artifact was not found")
        if version != build_version_id(source, str(row["content_sha256"])):
            raise ArtifactIntegrityError("artifact metadata is not content addressed")
        if str(row["object_key"]) != self._object_key(tenant, kb, source, version):
            raise ArtifactIntegrityError(
                "artifact object key does not match its identity"
            )
        return row

    def _verify_head(self, row: Mapping[str, Any]) -> None:
        try:
            info = self.object_store.head(str(row["object_key"]))
        except (ObjectIntegrityError, ObjectStoreError) as exc:
            raise ArtifactIntegrityError(
                "artifact object metadata is unavailable"
            ) from exc
        if info is None:
            raise ArtifactIntegrityError("artifact object is missing")
        if info.byte_size != int(row["byte_size"]) or info.sha256 != str(
            row["content_sha256"]
        ):
            raise ArtifactIntegrityError("artifact object metadata does not match")
        expected_version = str(row.get("object_version_id") or "") or None
        if expected_version is not None and info.version_id != expected_version:
            raise ArtifactIntegrityError("artifact object version does not match")

    def get_metadata(
        self,
        tenant_id: str,
        kb_id: str,
        source_id: str,
        version_id: str,
        *,
        verify_content: bool = False,
    ) -> dict[str, Any]:
        identity = self._identity(tenant_id, kb_id, source_id, version_id)
        row = self._row(*identity)
        self._verify_head(row)
        if verify_content:
            self._read_prefix(row, 0)
        return self._public(row)

    def _read_prefix(self, row: Mapping[str, Any], limit: int) -> tuple[bytes, bool]:
        prefix = bytearray()
        digest = hashlib.sha256()
        size = 0
        try:
            for chunk in self.object_store.iter_bytes(str(row["object_key"])):
                digest.update(chunk)
                size += len(chunk)
                if len(prefix) < limit:
                    prefix.extend(chunk[: limit - len(prefix)])
        except (ObjectNotFound, ObjectIntegrityError, ObjectStoreError) as exc:
            raise ArtifactIntegrityError("artifact object read failed") from exc
        if size != int(row["byte_size"]) or digest.hexdigest() != str(
            row["content_sha256"]
        ):
            raise ArtifactIntegrityError("artifact object content does not match")
        return bytes(prefix), size > limit

    def read(
        self, tenant_id: str, kb_id: str, source_id: str, version_id: str
    ) -> bytes:
        row = self._row(*self._identity(tenant_id, kb_id, source_id, version_id))
        value, _truncated = self._read_prefix(row, self.max_file_bytes)
        return value

    def open_verified(
        self, tenant_id: str, kb_id: str, source_id: str, version_id: str
    ) -> tuple[dict[str, Any], BinaryIO]:
        row = self._row(*self._identity(tenant_id, kb_id, source_id, version_id))
        handle = tempfile.TemporaryFile(mode="w+b")
        digest = hashlib.sha256()
        size = 0
        try:
            for chunk in self.object_store.iter_bytes(str(row["object_key"])):
                size += len(chunk)
                if size > self.max_file_bytes:
                    raise ArtifactIntegrityError("artifact exceeds max_file_bytes")
                digest.update(chunk)
                handle.write(chunk)
            if size != int(row["byte_size"]) or digest.hexdigest() != str(
                row["content_sha256"]
            ):
                raise ArtifactIntegrityError("artifact object content does not match")
            handle.flush()
            handle.seek(0)
        except BaseException as exc:
            handle.close()
            if isinstance(exc, ArtifactIntegrityError):
                raise
            raise ArtifactIntegrityError("artifact object read failed") from exc
        return self._public(row), handle

    def list_versions(
        self, tenant_id: str, kb_id: str, source_id: str
    ) -> list[dict[str, Any]]:
        tenant = _required_scope(tenant_id, "tenant_id")
        kb = _required_scope(kb_id, "kb_id")
        source = _safe_token(source_id, "source_id")
        m = self._marker()
        with self.backend.transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM ha_source_artifacts WHERE tenant_id="
                f"{m} AND kb_id={m} AND source_id={m} AND deleted_at IS NULL ORDER BY created_at DESC,version_id DESC",
                (tenant, kb, source),
            ).fetchall()
        return [self._public(self._mapping(row) or {}) for row in rows]

    @staticmethod
    def _is_text(row: Mapping[str, Any]) -> bool:
        media = str(row.get("media_type") or "").casefold()
        if media.startswith("text/") or media in _TEXT_MEDIA_TYPES:
            return True
        name = str(row.get("display_name") or "").casefold()
        return PurePosixPath(name).suffix in _TEXT_SUFFIXES

    def diff(
        self,
        tenant_id: str,
        kb_id: str,
        source_id: str,
        from_version_id: str,
        to_version_id: str,
    ) -> dict[str, Any]:
        before_row = self._row(
            *self._identity(tenant_id, kb_id, source_id, from_version_id)
        )
        after_row = self._row(
            *self._identity(tenant_id, kb_id, source_id, to_version_id)
        )
        result: dict[str, Any] = {
            "kind": "binary",
            "from_version_id": from_version_id,
            "to_version_id": to_version_id,
            "diff": None,
            "truncated": False,
            "from": self._public(before_row),
            "to": self._public(after_row),
        }
        text = self._is_text(before_row) and self._is_text(after_row)
        before, before_truncated = self._read_prefix(
            before_row, self.max_diff_bytes if text else 0
        )
        after, after_truncated = self._read_prefix(
            after_row, self.max_diff_bytes if text else 0
        )
        if not text:
            return result
        try:
            before_lines = before.decode("utf-8").splitlines()
            after_lines = after.decode("utf-8").splitlines()
        except UnicodeDecodeError:
            return result
        lines_truncated = (
            len(before_lines) > self.max_diff_lines
            or len(after_lines) > self.max_diff_lines
        )
        before_lines = before_lines[: self.max_diff_lines]
        after_lines = after_lines[: self.max_diff_lines]
        output: list[str] = []
        size = 0
        output_truncated = False
        for line in difflib.unified_diff(
            before_lines,
            after_lines,
            fromfile=from_version_id,
            tofile=to_version_id,
            lineterm="",
        ):
            encoded = (line + "\n").encode()
            if size + len(encoded) > self.max_diff_bytes:
                output_truncated = True
                break
            output.append(line)
            size += len(encoded)
        result.update(
            {
                "kind": "text",
                "diff": "\n".join(output),
                "truncated": before_truncated
                or after_truncated
                or lines_truncated
                or output_truncated,
            }
        )
        return result

    def delete_version(
        self, tenant_id: str, kb_id: str, source_id: str, version_id: str
    ) -> dict[str, Any]:
        tenant, kb, source, version = self._identity(
            tenant_id, kb_id, source_id, version_id
        )
        m = self._marker()
        now = float(self._clock())
        token = f"del-{int(now * 1_000_000)}-{uuid4().hex}"
        with self.backend.transaction(write=True) as connection:
            self._global_lock(connection)
            self._scope_writable_locked(connection, tenant, kb)
            reserved = connection.execute(
                "SELECT 1 FROM ha_source_artifact_reservation_items WHERE tenant_id="
                f"{m} AND kb_id={m} AND source_id={m} AND version_id={m} AND consumed=0",
                (tenant, kb, source, version),
            ).fetchone()
            if reserved is not None:
                raise ArtifactConflictError(
                    "source version is reserved by an in-flight batch"
                )
            row = self._mapping(
                connection.execute(
                    "SELECT * FROM ha_source_artifacts WHERE tenant_id="
                    f"{m} AND kb_id={m} AND source_id={m} AND version_id={m} AND deleted_at IS NULL",
                    (tenant, kb, source, version),
                ).fetchone()
            )
            if row is None:
                raise ArtifactNotFoundError("source artifact was not found")
            connection.execute(
                "UPDATE ha_source_artifacts SET recovery_token="
                f"{m},deleted_at={m} WHERE tenant_id={m} AND kb_id={m} AND source_id={m} AND version_id={m} AND deleted_at IS NULL",
                (token, now, tenant, kb, source, version),
            )
        return {"deleted": True, "recovery_token": token, "metadata": self._public(row)}

    def restore(
        self, tenant_id: str, kb_id: str, recovery_token: str
    ) -> dict[str, Any]:
        tenant = _required_scope(tenant_id, "tenant_id")
        kb = _required_scope(kb_id, "kb_id")
        token = _safe_token(recovery_token, "recovery_token")
        m = self._marker()
        with self.backend.transaction(write=True) as connection:
            self._global_lock(connection)
            self._scope_writable_locked(connection, tenant, kb)
            row = self._mapping(
                connection.execute(
                    "SELECT * FROM ha_source_artifacts WHERE tenant_id="
                    f"{m} AND kb_id={m} AND recovery_token={m} AND deleted_at IS NOT NULL",
                    (tenant, kb, token),
                ).fetchone()
            )
            if row is None:
                raise ArtifactNotFoundError("recovery token was not found")
            source = str(row["source_id"])
            reserved = self._mapping(
                connection.execute(
                    "SELECT COUNT(*) AS value FROM ha_source_artifact_reservation_items WHERE tenant_id="
                    f"{m} AND kb_id={m} AND source_id={m} AND consumed=0 AND reserved_version=1",
                    (tenant, kb, source),
                ).fetchone()
            )
            if (
                self._active_count_locked(connection, tenant, kb, source)
                + int((reserved or {}).get("value", 0))
                >= self.user_max_versions_per_source
            ):
                raise ArtifactLimitError("source exceeds user_max_versions_per_source")
            # Restore is a trust boundary: verify the full immutable payload,
            # not only object metadata that could have been jointly tampered.
            self._read_prefix(row, 0)
            connection.execute(
                "UPDATE ha_source_artifacts SET recovery_token=NULL,deleted_at=NULL WHERE tenant_id="
                f"{m} AND kb_id={m} AND recovery_token={m}",
                (tenant, kb, token),
            )
        return self._public(row)

    def prune_versions(
        self,
        tenant_id: str,
        kb_id: str,
        source_id: str,
        *,
        keep_latest: int,
        protect_version_ids: tuple[str, ...] = (),
    ) -> list[dict[str, Any]]:
        keep = _positive(keep_latest, "keep_latest")
        protected = {
            _safe_token(value, "protect_version_id") for value in protect_version_ids
        }
        rows = self.list_versions(tenant_id, kb_id, source_id)
        retained = protected & {str(row["version_id"]) for row in rows}
        if len(retained) > keep:
            raise ArtifactLimitError("protected versions exceed the keep_latest limit")
        for row in rows:
            if len(retained) >= keep:
                break
            retained.add(str(row["version_id"]))
        result = []
        for row in rows:
            version = str(row["version_id"])
            if version not in retained:
                result.append(self.delete_version(tenant_id, kb_id, source_id, version))
        return result

    def purge_trash(
        self, tenant_id: str, kb_id: str, *, older_than: float, limit: int = 100
    ) -> int:
        tenant = _required_scope(tenant_id, "tenant_id")
        kb = _required_scope(kb_id, "kb_id")
        if not math.isfinite(float(older_than)) or float(older_than) < 0:
            raise ValueError("older_than must be a finite non-negative timestamp")
        maximum = _positive(limit, "limit")
        m = self._marker()
        purged = 0
        with self.backend.transaction(write=True) as connection:
            self._global_lock(connection)
            self._scope_writable_locked(connection, tenant, kb)
            rows = connection.execute(
                "SELECT a.* FROM ha_source_artifacts a WHERE a.tenant_id="
                f"{m} AND a.kb_id={m} AND a.deleted_at IS NOT NULL AND a.deleted_at<{m} "
                "AND NOT EXISTS(SELECT 1 FROM ha_source_artifact_reservation_items i "
                "WHERE i.tenant_id=a.tenant_id AND i.kb_id=a.kb_id "
                "AND i.source_id=a.source_id AND i.version_id=a.version_id AND i.consumed=0) "
                f"ORDER BY a.deleted_at,a.recovery_token LIMIT {maximum}",
                (tenant, kb, float(older_than)),
            ).fetchall()
            for raw in rows:
                row = self._mapping(raw) or {}
                try:
                    self.object_store.delete(str(row["object_key"]))
                except ObjectStoreError as exc:
                    raise ArtifactIntegrityError(
                        "artifact object deletion failed"
                    ) from exc
                cursor = connection.execute(
                    "DELETE FROM ha_source_artifacts WHERE tenant_id="
                    f"{m} AND kb_id={m} AND source_id={m} AND version_id={m} AND recovery_token={m} AND deleted_at IS NOT NULL",
                    (
                        tenant,
                        kb,
                        row["source_id"],
                        row["version_id"],
                        row["recovery_token"],
                    ),
                )
                purged += max(0, int(cursor.rowcount))
        return purged

    def delete_scope(
        self, tenant_id: str, kb_id: str, *, kb_epoch: int | None = None
    ) -> dict[str, int]:
        tenant = _required_scope(tenant_id, "tenant_id")
        kb = _required_scope(kb_id, "kb_id")
        if kb_epoch is not None and (type(kb_epoch) is not int or kb_epoch < 1):
            raise ValueError("artifact scope KB epoch is invalid")
        m = self._marker()
        now = float(self._clock())
        with self.backend.transaction(write=True) as connection:
            self._global_lock(connection)
            existing_scope = self._mapping(
                connection.execute(
                    "SELECT kb_epoch FROM ha_source_artifact_scopes WHERE tenant_id="
                    f"{m} AND kb_id={m}",
                    (tenant, kb),
                ).fetchone()
            )
            epoch = (
                kb_epoch
                if kb_epoch is not None
                else int((existing_scope or {}).get("kb_epoch", 0))
            )
            if (
                kb_epoch is not None
                and existing_scope is not None
                and int(existing_scope["kb_epoch"]) > kb_epoch
            ):
                raise ArtifactConflictError("artifact scope incarnation is stale")
            insert = self.backend.sql(
                sqlite=(
                    "INSERT INTO ha_source_artifact_scopes(tenant_id,kb_id,state,kb_epoch,updated_at) "
                    "VALUES(?,?,?,?,?) ON CONFLICT(tenant_id,kb_id) DO UPDATE SET "
                    "state=excluded.state,kb_epoch=MAX(ha_source_artifact_scopes.kb_epoch,excluded.kb_epoch),"
                    "updated_at=excluded.updated_at"
                ),
                postgres=(
                    "INSERT INTO ha_source_artifact_scopes(tenant_id,kb_id,state,kb_epoch,updated_at) "
                    "VALUES(%s,%s,%s,%s,%s) ON CONFLICT(tenant_id,kb_id) DO UPDATE SET "
                    "state=EXCLUDED.state,kb_epoch=GREATEST(ha_source_artifact_scopes.kb_epoch,EXCLUDED.kb_epoch),"
                    "updated_at=EXCLUDED.updated_at"
                ),
            )
            connection.execute(insert, (tenant, kb, "deleting", epoch, now))
            rows = connection.execute(
                f"SELECT * FROM ha_source_artifacts WHERE tenant_id={m} AND kb_id={m}",
                (tenant, kb),
            ).fetchall()
        mapped = [self._mapping(row) or {} for row in rows]
        for row in mapped:
            try:
                self.object_store.delete(str(row["object_key"]))
            except ObjectStoreError as exc:
                raise ArtifactIntegrityError("artifact scope deletion failed") from exc
        with self.backend.transaction(write=True) as connection:
            self._global_lock(connection)
            connection.execute(
                f"DELETE FROM ha_source_artifact_reservation_items WHERE tenant_id={m} AND kb_id={m}",
                (tenant, kb),
            )
            connection.execute(
                f"DELETE FROM ha_source_artifact_reservations WHERE tenant_id={m} AND kb_id={m}",
                (tenant, kb),
            )
            connection.execute(
                "UPDATE ha_source_artifact_uploads SET reserved_bytes=0,lease_expires_at=0 "
                f"WHERE tenant_id={m} AND kb_id={m}",
                (tenant, kb),
            )
            connection.execute(
                f"DELETE FROM ha_source_artifacts WHERE tenant_id={m} AND kb_id={m}",
                (tenant, kb),
            )
            connection.execute(
                "UPDATE ha_source_artifact_scopes SET state='deleted',updated_at="
                f"{m} WHERE tenant_id={m} AND kb_id={m}",
                (float(self._clock()), tenant, kb),
            )
        active = [row for row in mapped if row.get("deleted_at") is None]
        trash = [row for row in mapped if row.get("deleted_at") is not None]
        return {
            "active_versions": len(active),
            "trash_versions": len(trash),
            "freed_bytes": sum(int(row["byte_size"]) for row in mapped),
        }

    def usage(self, tenant_id: str, kb_id: str) -> dict[str, int]:
        tenant = _required_scope(tenant_id, "tenant_id")
        kb = _required_scope(kb_id, "kb_id")
        m = self._marker()
        with self.backend.transaction() as connection:
            row = self._mapping(
                connection.execute(
                    "SELECT COALESCE(SUM(CASE WHEN deleted_at IS NULL THEN byte_size ELSE 0 END),0) AS active_bytes,"
                    "COALESCE(SUM(CASE WHEN deleted_at IS NULL THEN 1 ELSE 0 END),0) AS active_versions,"
                    "COALESCE(SUM(CASE WHEN deleted_at IS NOT NULL THEN byte_size ELSE 0 END),0) AS trash_bytes,"
                    "COALESCE(SUM(CASE WHEN deleted_at IS NOT NULL THEN 1 ELSE 0 END),0) AS trash_versions "
                    f"FROM ha_source_artifacts WHERE tenant_id={m} AND kb_id={m}",
                    (tenant, kb),
                ).fetchone()
            )
        return {
            key: int((row or {}).get(key, 0))
            for key in (
                "active_bytes",
                "active_versions",
                "trash_bytes",
                "trash_versions",
            )
        }

    def heartbeat(self) -> int:
        m = self._marker()
        now = float(self._clock())
        with self.backend.transaction(write=True) as connection:
            cursor = connection.execute(
                "UPDATE ha_source_artifact_reservations SET lease_expires_at="
                f"{m} WHERE lease_owner={m} AND lease_expires_at>{m}",
                (now + self.reservation_lease_seconds, self.owner_id, now),
            )
            connection.execute(
                "UPDATE ha_source_artifact_uploads SET lease_expires_at="
                f"{m} WHERE lease_owner={m} AND lease_expires_at>{m}",
                (now + self.reservation_lease_seconds, self.owner_id, now),
            )
            return max(0, int(cursor.rowcount))

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._last_error = None
        self._thread = threading.Thread(
            target=self._run, name="ha-artifact-reservations", daemon=True
        )
        self._thread.start()

    def _run(self) -> None:
        interval = max(5.0, self.reservation_lease_seconds / 3)
        while not self._stop.wait(interval):
            try:
                self.heartbeat()
                self._last_error = None
            except BaseException as exc:
                self._last_error = exc

    def stop(self, timeout: float = 5.0) -> bool:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
        self._thread = None
        return thread is None or not thread.is_alive()

    def check(self) -> bool:
        return (
            self.backend.check() is True
            and self.object_store.check() is True
            and self._last_error is None
        )

    def collect_orphans(self, *, limit: int = 100) -> int:
        maximum = _positive(limit, "limit")
        removed = 0
        m = self._marker()
        with self.backend.transaction(write=True) as connection:
            self._global_lock(connection)
            now = float(self._clock())
            self._expire_locked(connection, now)
            rows = connection.execute(
                "SELECT object_key,created_at FROM ha_source_artifact_uploads WHERE "
                f"lease_expires_at<={m} ORDER BY lease_expires_at,object_key LIMIT {maximum}",
                (now,),
            ).fetchall()
            for raw in rows:
                upload = self._mapping(raw) or {}
                key = str(upload["object_key"])
                exists = connection.execute(
                    f"SELECT 1 FROM ha_source_artifacts WHERE object_key={m}",
                    (key,),
                ).fetchone()
                if exists is None:
                    # Object publication is external to the DB transaction.  A
                    # scope fence may expire this intent while a bounded upload
                    # is still completing, so a missing HEAD is not proof that
                    # the intent can be forgotten yet.  Keep it for a full day
                    # (and at least two upload leases); if the object appears a
                    # later pass removes both object and ledger row.
                    head = self.object_store.head(key)
                    if head is not None:
                        self.object_store.delete(key)
                        removed += 1
                    elif now - float(upload.get("created_at") or 0) < max(
                        86_400.0, self.reservation_lease_seconds * 2
                    ):
                        connection.execute(
                            "UPDATE ha_source_artifact_uploads SET lease_expires_at="
                            f"{m} WHERE object_key={m} AND lease_expires_at<={m}",
                            (now + self.reservation_lease_seconds, key, now),
                        )
                        continue
                connection.execute(
                    f"DELETE FROM ha_source_artifact_uploads WHERE object_key={m} AND lease_expires_at<={m}",
                    (key, now),
                )
        return removed


__all__ = ["DistributedSourceArtifactStore"]
