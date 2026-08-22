from __future__ import annotations

import json
import time
from collections.abc import Iterable, Mapping
from typing import Any

from cogdoc.ha.storage import DatabaseBackend, DatabaseConnection
from cogdoc.service.source_catalog import (
    SourceCatalog,
    _health_status,
    _optional_identifier,
    _required_identifier,
    _scope,
    _timestamp,
)
from cogdoc.source_model import SourceDocument


class DistributedSourceCatalog:
    """PostgreSQL/SQLite shared implementation of the SourceCatalog contract."""

    def __init__(self, backend: DatabaseBackend, *, clock: Any = time.time) -> None:
        self.backend = backend
        self._clock = clock
        with backend.transaction(write=True) as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS ha_source_catalog_documents (
                tenant_id TEXT NOT NULL,kb_id TEXT NOT NULL,source_id TEXT NOT NULL,
                connection_id TEXT,connector_type TEXT NOT NULL,external_id TEXT NOT NULL,
                display_name TEXT NOT NULL,media_type TEXT NOT NULL,kind TEXT NOT NULL,
                origin_uri TEXT,current_version_id TEXT NOT NULL,metadata_json TEXT NOT NULL,
                health_status TEXT NOT NULL DEFAULT 'unknown',last_sync_at DOUBLE PRECISION,
                last_sync_error TEXT,health_job_sequence BIGINT NOT NULL DEFAULT 0,
                health_job_attempt BIGINT NOT NULL DEFAULT 0,
                health_event_rank BIGINT NOT NULL DEFAULT 0,deleted_at DOUBLE PRECISION,
                updated_at DOUBLE PRECISION NOT NULL,
                PRIMARY KEY(tenant_id,kb_id,source_id),
                UNIQUE(tenant_id,kb_id,connector_type,external_id))"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS ha_source_catalog_versions (
                tenant_id TEXT NOT NULL,kb_id TEXT NOT NULL,source_id TEXT NOT NULL,
                version_id TEXT NOT NULL,content_sha256 TEXT NOT NULL,byte_size BIGINT,
                etag TEXT,modified_at TEXT,fetched_at DOUBLE PRECISION NOT NULL,
                created_at DOUBLE PRECISION NOT NULL,
                PRIMARY KEY(tenant_id,kb_id,source_id,version_id))"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS ha_source_catalog_locks (
                tenant_id TEXT NOT NULL,kb_id TEXT NOT NULL,scope_kind TEXT NOT NULL,
                scope_id TEXT NOT NULL,updated_at DOUBLE PRECISION NOT NULL,
                PRIMARY KEY(tenant_id,kb_id,scope_kind,scope_id))"""
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_ha_source_catalog_current ON "
                "ha_source_catalog_documents(tenant_id,kb_id,deleted_at,display_name)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_ha_source_catalog_connection ON "
                "ha_source_catalog_documents(tenant_id,kb_id,connection_id,deleted_at,updated_at)"
            )

    @staticmethod
    def _mapping(row: Any | None) -> dict[str, Any] | None:
        if row is None:
            return None
        if isinstance(row, Mapping):
            return dict(row)
        keys = getattr(row, "keys", None)
        if callable(keys):
            return {str(key): row[key] for key in keys()}
        raise RuntimeError("source catalog row mapping is unavailable")

    def _markers(self, count: int) -> str:
        marker = self.backend.sql(sqlite="?", postgres="%s")
        return ",".join(marker for _index in range(count))

    @staticmethod
    def _document(row: Any) -> dict[str, Any]:
        value = DistributedSourceCatalog._mapping(row)
        assert value is not None
        value["metadata"] = json.loads(str(value.pop("metadata_json") or "{}"))
        value["version_id"] = value.pop("current_version_id")
        return value

    @staticmethod
    def _version(row: Any) -> dict[str, Any]:
        value = DistributedSourceCatalog._mapping(row)
        assert value is not None
        current = value.pop("current_version_id")
        value["is_current"] = value["version_id"] == current
        return value

    @staticmethod
    def _current_select() -> str:
        return (
            "SELECT d.source_id,d.connection_id,d.connector_type,d.external_id,d.display_name,"
            "d.media_type,d.kind,d.origin_uri,d.current_version_id,d.metadata_json,"
            "d.health_status,d.last_sync_at,d.last_sync_error,d.deleted_at,d.updated_at,"
            "d.health_job_sequence,d.health_job_attempt,d.health_event_rank,"
            "v.content_sha256,v.byte_size,v.etag,v.modified_at,v.fetched_at "
            "FROM ha_source_catalog_documents d JOIN ha_source_catalog_versions v ON "
            "v.tenant_id=d.tenant_id AND v.kb_id=d.kb_id AND v.source_id=d.source_id "
            "AND v.version_id=d.current_version_id"
        )

    @staticmethod
    def _version_select() -> str:
        return (
            "SELECT v.source_id,v.version_id,v.content_sha256,v.byte_size,v.etag,"
            "v.modified_at,v.fetched_at,v.created_at,d.current_version_id "
            "FROM ha_source_catalog_versions v JOIN ha_source_catalog_documents d ON "
            "d.tenant_id=v.tenant_id AND d.kb_id=v.kb_id AND d.source_id=v.source_id"
        )

    def _upsert_locked(
        self,
        connection: DatabaseConnection,
        tenant_id: str,
        kb_id: str,
        document: SourceDocument,
        now: float,
        *,
        connection_id: str | None,
        health_status: str | None,
        last_sync_at: float | None,
        last_sync_error: str | None,
    ) -> None:
        marker = self.backend.sql(sqlite="?", postgres="%s")
        version = document.version
        version_insert = self.backend.sql(
            sqlite="INSERT OR IGNORE",
            postgres="INSERT",
        )
        version_conflict = self.backend.sql(
            sqlite="",
            postgres=" ON CONFLICT(tenant_id,kb_id,source_id,version_id) DO NOTHING",
        )
        connection.execute(
            f"{version_insert} INTO ha_source_catalog_versions(tenant_id,kb_id,source_id,"
            "version_id,content_sha256,byte_size,etag,modified_at,fetched_at,created_at) "
            f"VALUES({self._markers(10)}){version_conflict}",
            (
                tenant_id,
                kb_id,
                document.source_id,
                version.version_id,
                version.content_sha256,
                version.byte_size,
                version.etag,
                version.modified_at,
                version.fetched_at,
                now,
            ),
        )
        update_health = health_status is not None
        update_sync_time = last_sync_at is not None
        update_sync_error = update_health or last_sync_error is not None
        connection.execute(
            "INSERT INTO ha_source_catalog_documents(tenant_id,kb_id,source_id,connection_id,"
            "connector_type,external_id,display_name,media_type,kind,origin_uri,current_version_id,"
            "metadata_json,health_status,last_sync_at,last_sync_error,deleted_at,updated_at) "
            f"VALUES({self._markers(17)}) ON CONFLICT(tenant_id,kb_id,source_id) DO UPDATE SET "
            "connection_id=COALESCE(excluded.connection_id,ha_source_catalog_documents.connection_id),"
            "display_name=excluded.display_name,media_type=excluded.media_type,kind=excluded.kind,"
            "origin_uri=excluded.origin_uri,current_version_id=excluded.current_version_id,"
            "metadata_json=excluded.metadata_json,health_status=CASE WHEN "
            f"{marker} THEN excluded.health_status ELSE ha_source_catalog_documents.health_status END,"
            f"last_sync_at=CASE WHEN {marker} THEN excluded.last_sync_at "
            "ELSE ha_source_catalog_documents.last_sync_at END,last_sync_error=CASE WHEN "
            f"{marker} THEN excluded.last_sync_error ELSE ha_source_catalog_documents.last_sync_error END,"
            "deleted_at=NULL,updated_at=excluded.updated_at",
            (
                tenant_id,
                kb_id,
                document.source_id,
                connection_id,
                document.connector_type,
                document.external_id,
                document.display_name,
                document.media_type,
                document.kind.value,
                document.origin_uri,
                version.version_id,
                json.dumps(document.metadata, ensure_ascii=False, sort_keys=True),
                health_status or "unknown",
                last_sync_at,
                last_sync_error,
                None,
                now,
                update_health,
                update_sync_time,
                update_sync_error,
            ),
        )

    def _scope_lock(
        self,
        connection: DatabaseConnection,
        tenant_id: str,
        kb_id: str,
        scope_kind: str,
        scope_id: str,
    ) -> None:
        now = float(self._clock())
        insert = self.backend.sql(
            sqlite=(
                "INSERT OR IGNORE INTO ha_source_catalog_locks"
                "(tenant_id,kb_id,scope_kind,scope_id,updated_at) VALUES(?,?,?,?,?)"
            ),
            postgres=(
                "INSERT INTO ha_source_catalog_locks"
                "(tenant_id,kb_id,scope_kind,scope_id,updated_at) VALUES(%s,%s,%s,%s,%s) "
                "ON CONFLICT(tenant_id,kb_id,scope_kind,scope_id) DO NOTHING"
            ),
        )
        connection.execute(insert, (tenant_id, kb_id, scope_kind, scope_id, now))
        marker = self.backend.sql(sqlite="?", postgres="%s")
        lock = self.backend.sql(sqlite="", postgres=" FOR UPDATE")
        row = connection.execute(
            "SELECT updated_at FROM ha_source_catalog_locks WHERE tenant_id="
            f"{marker} AND kb_id={marker} AND scope_kind={marker} AND scope_id={marker}{lock}",
            (tenant_id, kb_id, scope_kind, scope_id),
        ).fetchone()
        if row is None:
            raise RuntimeError("source catalog snapshot lock is unavailable")

    def upsert(
        self,
        tenant_id: str,
        kb_id: str,
        document: SourceDocument,
        *,
        connection_id: str | None = None,
        health_status: str | None = None,
        last_sync_at: float | None = None,
        last_sync_error: str | None = None,
    ) -> dict[str, Any]:
        tenant, knowledge_base = _scope(tenant_id, kb_id)
        connection_id = SourceCatalog._document_connection_id(document, connection_id)
        if connection_id is not None and health_status is None:
            health_status = "healthy"
            if last_sync_at is None:
                last_sync_at = float(self._clock())
        status = _health_status(health_status) if health_status is not None else None
        synced = _timestamp(last_sync_at, "last_sync_at")
        error = (
            None if status == "healthy" else SourceCatalog._sync_error(last_sync_error)
        )
        with self.backend.transaction(write=True) as connection:
            self._upsert_locked(
                connection,
                tenant,
                knowledge_base,
                document,
                float(self._clock()),
                connection_id=connection_id,
                health_status=status,
                last_sync_at=synced,
                last_sync_error=error,
            )
        return (
            self.get(tenant, knowledge_base, document.source_id, include_deleted=True)
            or {}
        )

    def reconcile(
        self,
        tenant_id: str,
        kb_id: str,
        documents: Iterable[SourceDocument],
        *,
        connector_type: str | None = None,
        connection_id: str | None = None,
    ) -> dict[str, int]:
        tenant, knowledge_base = _scope(tenant_id, kb_id)
        materialized = list(documents)
        connector = (
            _required_identifier(connector_type, "connector_type").casefold()
            if connector_type is not None
            else None
        )
        connection_id = _optional_identifier(connection_id, "connection_id")
        if connector is not None and any(
            document.connector_type != connector for document in materialized
        ):
            raise ValueError("reconcile documents must belong to connector_type")
        seen = {document.source_id for document in materialized}
        marker = self.backend.sql(sqlite="?", postgres="%s")
        deleted = 0
        with self.backend.transaction(write=True) as connection:
            now = float(self._clock())
            # Connector-scoped and legacy connector-type snapshots may overlap.
            # One KB-wide catalog lock gives both paths a common serialization
            # point and prevents cross-scope tombstones or row-lock deadlocks.
            self._scope_lock(connection, tenant, knowledge_base, "catalog", "*")
            for document in materialized:
                resolved_connection = SourceCatalog._document_connection_id(
                    document, connection_id
                )
                self._upsert_locked(
                    connection,
                    tenant,
                    knowledge_base,
                    document,
                    now,
                    connection_id=resolved_connection,
                    health_status="healthy",
                    last_sync_at=now,
                    last_sync_error=None,
                )
            if connection_id is None and connector is None:
                return {"upserted": len(materialized), "deleted": 0}
            scope_column = (
                "connection_id" if connection_id is not None else "connector_type"
            )
            scope_value = connection_id if connection_id is not None else connector
            lock = self.backend.sql(sqlite="", postgres=" FOR UPDATE")
            rows = connection.execute(
                "SELECT source_id FROM ha_source_catalog_documents WHERE tenant_id="
                f"{marker} AND kb_id={marker} AND {scope_column}={marker} "
                f"AND deleted_at IS NULL{lock}",
                (tenant, knowledge_base, scope_value),
            ).fetchall()
            for row in rows:
                value = self._mapping(row)
                assert value is not None
                source_id = str(value["source_id"])
                if source_id in seen:
                    continue
                changed = connection.execute(
                    "UPDATE ha_source_catalog_documents SET deleted_at="
                    f"{marker},updated_at={marker},health_status='stale' WHERE tenant_id="
                    f"{marker} AND kb_id={marker} AND source_id={marker} AND deleted_at IS NULL",
                    (now, now, tenant, knowledge_base, source_id),
                )
                deleted += changed.rowcount
        return {"upserted": len(materialized), "deleted": deleted}

    def get(
        self,
        tenant_id: str,
        kb_id: str,
        source_id: str,
        *,
        include_deleted: bool = False,
    ) -> dict[str, Any] | None:
        tenant, kb = _scope(tenant_id, kb_id)
        source = _required_identifier(source_id, "source_id")
        marker = self.backend.sql(sqlite="?", postgres="%s")
        clause = "" if include_deleted else " AND d.deleted_at IS NULL"
        with self.backend.transaction() as connection:
            row = connection.execute(
                self._current_select()
                + f" WHERE d.tenant_id={marker} AND d.kb_id={marker} "
                f"AND d.source_id={marker}{clause}",
                (tenant, kb, source),
            ).fetchone()
        return self._document(row) if row is not None else None

    def list_sources(
        self,
        tenant_id: str,
        kb_id: str,
        *,
        include_deleted: bool = False,
        connection_id: str | None = None,
        health_status: str | None = None,
    ) -> list[dict[str, Any]]:
        tenant, kb = _scope(tenant_id, kb_id)
        marker = self.backend.sql(sqlite="?", postgres="%s")
        clauses = [f"d.tenant_id={marker}", f"d.kb_id={marker}"]
        params: list[Any] = [tenant, kb]
        if not include_deleted:
            clauses.append("d.deleted_at IS NULL")
        if connection_id is not None:
            clauses.append(f"d.connection_id={marker}")
            params.append(_required_identifier(connection_id, "connection_id"))
        if health_status is not None:
            clauses.append(f"d.health_status={marker}")
            params.append(_health_status(health_status))
        with self.backend.transaction() as connection:
            rows = connection.execute(
                self._current_select()
                + " WHERE "
                + " AND ".join(clauses)
                + " ORDER BY d.display_name,d.source_id",
                tuple(params),
            ).fetchall()
        return [self._document(row) for row in rows]

    def get_version(
        self,
        tenant_id: str,
        kb_id: str,
        source_id: str,
        version_id: str,
        *,
        include_deleted: bool = False,
    ) -> dict[str, Any] | None:
        tenant, kb = _scope(tenant_id, kb_id)
        source = _required_identifier(source_id, "source_id")
        version = _required_identifier(version_id, "version_id")
        marker = self.backend.sql(sqlite="?", postgres="%s")
        clause = "" if include_deleted else " AND d.deleted_at IS NULL"
        with self.backend.transaction() as connection:
            row = connection.execute(
                self._version_select()
                + f" WHERE d.tenant_id={marker} AND d.kb_id={marker} "
                f"AND d.source_id={marker} AND v.version_id={marker}{clause}",
                (tenant, kb, source, version),
            ).fetchone()
        return self._version(row) if row is not None else None

    def version(self, *args: Any, **kwargs: Any) -> dict[str, Any] | None:
        return self.get_version(*args, **kwargs)

    def list_versions(
        self,
        tenant_id: str,
        kb_id: str,
        source_id: str,
        *,
        include_deleted: bool = False,
    ) -> list[dict[str, Any]]:
        tenant, kb = _scope(tenant_id, kb_id)
        source = _required_identifier(source_id, "source_id")
        marker = self.backend.sql(sqlite="?", postgres="%s")
        clause = "" if include_deleted else " AND d.deleted_at IS NULL"
        with self.backend.transaction() as connection:
            rows = connection.execute(
                self._version_select()
                + f" WHERE d.tenant_id={marker} AND d.kb_id={marker} "
                f"AND d.source_id={marker}{clause} ORDER BY v.created_at DESC,v.version_id",
                (tenant, kb, source),
            ).fetchall()
        return [self._version(row) for row in rows]

    def versions(
        self,
        tenant_id: str,
        kb_id: str,
        source_id: str,
        *,
        include_deleted: bool = True,
    ) -> list[dict[str, Any]]:
        return self.list_versions(
            tenant_id, kb_id, source_id, include_deleted=include_deleted
        )

    def set_source_health(
        self,
        tenant_id: str,
        kb_id: str,
        source_id: str,
        health_status: str,
        *,
        last_sync_at: float | None = None,
        last_sync_error: str | None = None,
    ) -> dict[str, Any] | None:
        tenant, kb = _scope(tenant_id, kb_id)
        source = _required_identifier(source_id, "source_id")
        status = _health_status(health_status)
        synced = _timestamp(last_sync_at, "last_sync_at")
        if synced is None and status == "healthy":
            synced = float(self._clock())
        error = (
            None if status == "healthy" else SourceCatalog._sync_error(last_sync_error)
        )
        marker = self.backend.sql(sqlite="?", postgres="%s")
        with self.backend.transaction(write=True) as connection:
            connection.execute(
                "UPDATE ha_source_catalog_documents SET health_status="
                f"{marker},last_sync_at=COALESCE({marker},last_sync_at),last_sync_error={marker},"
                f"updated_at={marker} WHERE tenant_id={marker} AND kb_id={marker} "
                f"AND source_id={marker}",
                (status, synced, error, float(self._clock()), tenant, kb, source),
            )
        return self.get(tenant, kb, source, include_deleted=True)

    def record_connection_health(
        self,
        tenant_id: str,
        kb_id: str,
        connection_id: str,
        health_status: str,
        *,
        last_sync_at: float | None = None,
        last_sync_error: str | None = None,
        job_sequence: int = 0,
        job_attempt: int = 0,
        event_rank: int = 0,
        include_deleted: bool = False,
    ) -> int:
        tenant, kb = _scope(tenant_id, kb_id)
        connection_id = _required_identifier(connection_id, "connection_id")
        status = _health_status(health_status)
        synced = _timestamp(last_sync_at, "last_sync_at")
        if synced is None and status == "healthy":
            synced = float(self._clock())
        error = (
            None if status == "healthy" else SourceCatalog._sync_error(last_sync_error)
        )
        if any(
            type(value) is not int or value < 0
            for value in (job_sequence, job_attempt, event_rank)
        ):
            raise ValueError("health event watermarks must be non-negative integers")
        marker = self.backend.sql(sqlite="?", postgres="%s")
        deleted = "" if include_deleted else " AND deleted_at IS NULL"
        with self.backend.transaction(write=True) as connection:
            changed = connection.execute(
                "UPDATE ha_source_catalog_documents SET health_status="
                f"{marker},last_sync_at=COALESCE({marker},last_sync_at),last_sync_error={marker},"
                f"health_job_sequence={marker},health_job_attempt={marker},health_event_rank={marker},"
                f"updated_at={marker} WHERE tenant_id={marker} AND kb_id={marker} "
                f"AND connection_id={marker} AND ({marker}>health_job_sequence OR "
                f"({marker}=health_job_sequence AND ({marker}>health_job_attempt OR "
                f"({marker}=health_job_attempt AND {marker}>=health_event_rank)))){deleted}",
                (
                    status,
                    synced,
                    error,
                    job_sequence,
                    job_attempt,
                    event_rank,
                    float(self._clock()),
                    tenant,
                    kb,
                    connection_id,
                    job_sequence,
                    job_sequence,
                    job_attempt,
                    job_attempt,
                    event_rank,
                ),
            )
        return changed.rowcount

    def tombstone(self, tenant_id: str, kb_id: str, source_ids: Iterable[str]) -> int:
        tenant, kb = _scope(tenant_id, kb_id)
        values = tuple(
            dict.fromkeys(
                _required_identifier(item, "source_id")
                for item in source_ids
                if str(item or "").strip()
            )
        )
        marker = self.backend.sql(sqlite="?", postgres="%s")
        changed = 0
        with self.backend.transaction(write=True) as connection:
            self._scope_lock(connection, tenant, kb, "catalog", "*")
            now = float(self._clock())
            for source in values:
                cursor = connection.execute(
                    "UPDATE ha_source_catalog_documents SET deleted_at="
                    f"{marker},updated_at={marker},health_status='stale' WHERE tenant_id="
                    f"{marker} AND kb_id={marker} AND source_id={marker} AND deleted_at IS NULL",
                    (now, now, tenant, kb, source),
                )
                changed += cursor.rowcount
        return changed

    def delete_scope(self, tenant_id: str, kb_id: str) -> dict[str, int]:
        tenant, kb = _scope(tenant_id, kb_id)
        marker = self.backend.sql(sqlite="?", postgres="%s")
        with self.backend.transaction(write=True) as connection:
            self._scope_lock(connection, tenant, kb, "catalog", "*")
            versions = connection.execute(
                f"DELETE FROM ha_source_catalog_versions WHERE tenant_id={marker} AND kb_id={marker}",
                (tenant, kb),
            ).rowcount
            documents = connection.execute(
                f"DELETE FROM ha_source_catalog_documents WHERE tenant_id={marker} AND kb_id={marker}",
                (tenant, kb),
            ).rowcount
        return {"documents": int(documents), "versions": int(versions)}

    def check(self) -> bool:
        with self.backend.transaction() as connection:
            for table in ("ha_source_catalog_documents", "ha_source_catalog_versions"):
                if (
                    connection.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
                    is None
                ):
                    # Empty is healthy; the query itself is the schema probe.
                    continue
        return True

    def close(self) -> None:
        """The owning HARuntime closes the shared backend."""


__all__ = ["DistributedSourceCatalog"]
