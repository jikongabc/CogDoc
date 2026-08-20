from __future__ import annotations

import json
import math
import sqlite3
import time
from collections.abc import Iterable
from threading import RLock
from typing import Any

from cogdoc.api.persistence import connect_sqlite
from cogdoc.source_model import SourceDocument


SOURCE_HEALTH_STATUSES = frozenset(
    {"unknown", "syncing", "healthy", "degraded", "stale", "error"}
)
_MAX_IDENTIFIER_LENGTH = 512
_MAX_SYNC_ERROR_LENGTH = 4_000


def _required_identifier(value: object, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field_name} is required")
    if len(text) > _MAX_IDENTIFIER_LENGTH or "\x00" in text:
        raise ValueError(f"{field_name} is invalid")
    return text


def _optional_identifier(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    return _required_identifier(value, field_name)


def _scope(tenant_id: object, kb_id: object) -> tuple[str, str]:
    return (
        _required_identifier(tenant_id, "tenant_id"),
        _required_identifier(kb_id, "kb_id"),
    )


def _health_status(value: object) -> str:
    status = _required_identifier(value, "health_status").casefold()
    if status not in SOURCE_HEALTH_STATUSES:
        raise ValueError("unsupported source health status")
    return status


def _timestamp(value: float | None, field_name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a finite non-negative timestamp")
    timestamp = float(value)
    if not math.isfinite(timestamp) or timestamp < 0:
        raise ValueError(f"{field_name} must be a finite non-negative timestamp")
    return timestamp


class SourceCatalog:
    """Durable current-source and immutable-version catalog.

    Every read and write requires a tenant and knowledge-base scope. Index
    generations remain the atomic retrieval commit pointer; this catalog only
    records source identity, version history, connection ownership, and source
    sync health for the operations control plane.
    """

    def __init__(self, db_path: str):
        self._lock = RLock()
        self._conn = connect_sqlite(db_path)
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS source_catalog_documents (
                tenant_id TEXT NOT NULL,
                kb_id TEXT NOT NULL,
                source_id TEXT NOT NULL,
                connection_id TEXT,
                connector_type TEXT NOT NULL,
                external_id TEXT NOT NULL,
                display_name TEXT NOT NULL,
                media_type TEXT NOT NULL,
                kind TEXT NOT NULL,
                origin_uri TEXT,
                current_version_id TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                health_status TEXT NOT NULL DEFAULT 'unknown',
                last_sync_at REAL,
                last_sync_error TEXT,
                health_job_sequence INTEGER NOT NULL DEFAULT 0,
                health_job_attempt INTEGER NOT NULL DEFAULT 0,
                health_event_rank INTEGER NOT NULL DEFAULT 0,
                deleted_at REAL,
                updated_at REAL NOT NULL,
                PRIMARY KEY (tenant_id, kb_id, source_id),
                UNIQUE (tenant_id, kb_id, connector_type, external_id)
            );
            CREATE TABLE IF NOT EXISTS source_catalog_versions (
                tenant_id TEXT NOT NULL,
                kb_id TEXT NOT NULL,
                source_id TEXT NOT NULL,
                version_id TEXT NOT NULL,
                content_sha256 TEXT NOT NULL,
                byte_size INTEGER,
                etag TEXT,
                modified_at TEXT,
                fetched_at REAL NOT NULL,
                created_at REAL NOT NULL,
                PRIMARY KEY (tenant_id, kb_id, source_id, version_id)
            );
            """
        )
        # CREATE TABLE IF NOT EXISTS does not evolve installations created by
        # source-document-v1. Additive migrations remain safe on every startup.
        self._ensure_document_columns()
        self._backfill_connection_ids()
        self._conn.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_source_catalog_current
                ON source_catalog_documents(tenant_id, kb_id, deleted_at, display_name);
            CREATE INDEX IF NOT EXISTS idx_source_catalog_connection
                ON source_catalog_documents(
                    tenant_id, kb_id, connection_id, deleted_at, updated_at DESC
                );
            """
        )

    def _ensure_document_columns(self) -> None:
        definitions = {
            "connection_id": "TEXT",
            "health_status": "TEXT NOT NULL DEFAULT 'unknown'",
            "last_sync_at": "REAL",
            "last_sync_error": "TEXT",
            "health_job_sequence": "INTEGER NOT NULL DEFAULT 0",
            "health_job_attempt": "INTEGER NOT NULL DEFAULT 0",
            "health_event_rank": "INTEGER NOT NULL DEFAULT 0",
        }
        with self._lock:
            for name, definition in definitions.items():
                columns = {
                    str(row[1])
                    for row in self._conn.execute(
                        "PRAGMA table_info(source_catalog_documents)"
                    ).fetchall()
                }
                if name in columns:
                    continue
                try:
                    self._conn.execute(
                        f"ALTER TABLE source_catalog_documents ADD COLUMN {name} {definition}"
                    )
                except sqlite3.OperationalError as exc:
                    # A second process may have completed the same additive
                    # migration after our PRAGMA read.
                    current = {
                        str(row[1])
                        for row in self._conn.execute(
                            "PRAGMA table_info(source_catalog_documents)"
                        ).fetchall()
                    }
                    if name not in current:
                        raise exc

    def _backfill_connection_ids(self) -> None:
        """Recover connection ownership already present in v1 metadata."""

        with self._lock:
            rows = self._conn.execute(
                "SELECT tenant_id,kb_id,source_id,metadata_json "
                "FROM source_catalog_documents WHERE connection_id IS NULL"
            ).fetchall()
            updates: list[tuple[str, str, str, str]] = []
            for tenant_id, kb_id, source_id, raw_metadata in rows:
                try:
                    metadata = json.loads(raw_metadata or "{}")
                    raw_connection = (
                        metadata.get("connection_id")
                        if isinstance(metadata, dict)
                        else None
                    )
                    connection_id = _optional_identifier(
                        raw_connection, "connection_id"
                    )
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                if connection_id is not None:
                    updates.append(
                        (connection_id, str(tenant_id), str(kb_id), str(source_id))
                    )
            if not updates:
                return
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.executemany(
                    "UPDATE source_catalog_documents SET connection_id=? "
                    "WHERE tenant_id=? AND kb_id=? AND source_id=? AND connection_id IS NULL",
                    updates,
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def check(self) -> bool:
        """Fail readiness when either source catalog table is unavailable."""

        with self._lock:
            for table in (
                "source_catalog_documents",
                "source_catalog_versions",
            ):
                self._conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
        return True

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
        connection = self._document_connection_id(document, connection_id)
        # Existing connector materialization stores connection_id in metadata.
        # Treat such an upsert as a successful source sync without requiring a
        # breaking change to that call site.
        if connection is not None and health_status is None:
            health_status = "healthy"
            if last_sync_at is None:
                last_sync_at = time.time()
        status = _health_status(health_status) if health_status is not None else None
        synced_at = _timestamp(last_sync_at, "last_sync_at")
        error = None if status == "healthy" else self._sync_error(last_sync_error)
        now = time.time()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._upsert_locked(
                    tenant,
                    knowledge_base,
                    document,
                    now,
                    connection_id=connection,
                    health_status=status,
                    last_sync_at=synced_at,
                    last_sync_error=error,
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return (
            self.get(tenant, knowledge_base, document.source_id, include_deleted=True)
            or {}
        )

    def _upsert_locked(
        self,
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
        version = document.version
        self._conn.execute(
            "INSERT OR IGNORE INTO source_catalog_versions "
            "(tenant_id,kb_id,source_id,version_id,content_sha256,byte_size,etag,modified_at,fetched_at,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
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
        self._conn.execute(
            "INSERT INTO source_catalog_documents "
            "(tenant_id,kb_id,source_id,connection_id,connector_type,external_id,display_name,media_type,kind,"
            "origin_uri,current_version_id,metadata_json,health_status,last_sync_at,last_sync_error,deleted_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(tenant_id,kb_id,source_id) DO UPDATE SET "
            "connection_id=COALESCE(excluded.connection_id,source_catalog_documents.connection_id),"
            "display_name=excluded.display_name,media_type=excluded.media_type,kind=excluded.kind,"
            "origin_uri=excluded.origin_uri,current_version_id=excluded.current_version_id,"
            "metadata_json=excluded.metadata_json,"
            "health_status=CASE WHEN ? THEN excluded.health_status ELSE source_catalog_documents.health_status END,"
            "last_sync_at=CASE WHEN ? THEN excluded.last_sync_at ELSE source_catalog_documents.last_sync_at END,"
            "last_sync_error=CASE WHEN ? THEN excluded.last_sync_error ELSE source_catalog_documents.last_sync_error END,"
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
        normalized_connector = (
            _required_identifier(connector_type, "connector_type").casefold()
            if connector_type is not None
            else None
        )
        connection = _optional_identifier(connection_id, "connection_id")
        if normalized_connector is not None and any(
            item.connector_type != normalized_connector for item in materialized
        ):
            raise ValueError("reconcile documents must belong to connector_type")
        if connection is not None:
            for item in materialized:
                metadata_connection = self._document_connection_id(item, None)
                if (
                    metadata_connection is not None
                    and metadata_connection != connection
                ):
                    raise ValueError("reconcile documents must belong to connection_id")
        seen = {item.source_id for item in materialized}
        deleted = 0
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                now = time.time()
                for item in materialized:
                    item_connection = self._document_connection_id(item, connection)
                    self._upsert_locked(
                        tenant,
                        knowledge_base,
                        item,
                        now,
                        connection_id=item_connection,
                        health_status="healthy",
                        last_sync_at=now,
                        last_sync_error=None,
                    )
                scope_sql: str | None = None
                scope_value: str | None = None
                if connection is not None:
                    scope_sql = "connection_id=?"
                    scope_value = connection
                elif normalized_connector is not None:
                    scope_sql = "connector_type=?"
                    scope_value = normalized_connector
                if scope_sql is None:
                    self._conn.execute("COMMIT")
                    return {"upserted": len(materialized), "deleted": 0}
                rows = self._conn.execute(
                    "SELECT source_id FROM source_catalog_documents "
                    f"WHERE tenant_id=? AND kb_id=? AND {scope_sql} AND deleted_at IS NULL",
                    (tenant, knowledge_base, scope_value),
                ).fetchall()
                for (source_id,) in rows:
                    if source_id not in seen:
                        self._conn.execute(
                            "UPDATE source_catalog_documents "
                            "SET deleted_at=?,updated_at=?,health_status='stale' "
                            "WHERE tenant_id=? AND kb_id=? AND source_id=? AND deleted_at IS NULL",
                            (now, now, tenant, knowledge_base, source_id),
                        )
                        deleted += 1
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return {"upserted": len(materialized), "deleted": deleted}

    def get(
        self,
        tenant_id: str,
        kb_id: str,
        source_id: str,
        *,
        include_deleted: bool = False,
    ) -> dict[str, Any] | None:
        tenant, knowledge_base = _scope(tenant_id, kb_id)
        source = _required_identifier(source_id, "source_id")
        clause = "" if include_deleted else " AND d.deleted_at IS NULL"
        with self._lock:
            row = self._conn.execute(
                self._current_select()
                + " WHERE d.tenant_id=? AND d.kb_id=? AND d.source_id=?"
                + clause,
                (tenant, knowledge_base, source),
            ).fetchone()
        return self._row(row) if row else None

    def list_sources(
        self,
        tenant_id: str,
        kb_id: str,
        *,
        include_deleted: bool = False,
        connection_id: str | None = None,
        health_status: str | None = None,
    ) -> list[dict[str, Any]]:
        tenant, knowledge_base = _scope(tenant_id, kb_id)
        connection = _optional_identifier(connection_id, "connection_id")
        status = _health_status(health_status) if health_status is not None else None
        clauses = ["d.tenant_id=?", "d.kb_id=?"]
        params: list[Any] = [tenant, knowledge_base]
        if not include_deleted:
            clauses.append("d.deleted_at IS NULL")
        if connection is not None:
            clauses.append("d.connection_id=?")
            params.append(connection)
        if status is not None:
            clauses.append("d.health_status=?")
            params.append(status)
        with self._lock:
            rows = self._conn.execute(
                self._current_select()
                + " WHERE "
                + " AND ".join(clauses)
                + " ORDER BY d.display_name,d.source_id",
                tuple(params),
            ).fetchall()
        return [self._row(row) for row in rows]

    def get_version(
        self,
        tenant_id: str,
        kb_id: str,
        source_id: str,
        version_id: str,
        *,
        include_deleted: bool = False,
    ) -> dict[str, Any] | None:
        tenant, knowledge_base = _scope(tenant_id, kb_id)
        source = _required_identifier(source_id, "source_id")
        version = _required_identifier(version_id, "version_id")
        deleted_clause = "" if include_deleted else " AND d.deleted_at IS NULL"
        with self._lock:
            row = self._conn.execute(
                self._version_select()
                + " WHERE d.tenant_id=? AND d.kb_id=? AND d.source_id=? AND v.version_id=?"
                + deleted_clause,
                (tenant, knowledge_base, source, version),
            ).fetchone()
        return self._version_row(row) if row else None

    def version(
        self,
        tenant_id: str,
        kb_id: str,
        source_id: str,
        version_id: str,
        *,
        include_deleted: bool = False,
    ) -> dict[str, Any] | None:
        """Compatibility-friendly singular spelling for operations callers."""

        return self.get_version(
            tenant_id,
            kb_id,
            source_id,
            version_id,
            include_deleted=include_deleted,
        )

    def list_versions(
        self,
        tenant_id: str,
        kb_id: str,
        source_id: str,
        *,
        include_deleted: bool = False,
    ) -> list[dict[str, Any]]:
        tenant, knowledge_base = _scope(tenant_id, kb_id)
        source = _required_identifier(source_id, "source_id")
        deleted_clause = "" if include_deleted else " AND d.deleted_at IS NULL"
        with self._lock:
            rows = self._conn.execute(
                self._version_select()
                + " WHERE d.tenant_id=? AND d.kb_id=? AND d.source_id=?"
                + deleted_clause
                + " ORDER BY v.created_at DESC,v.version_id",
                (tenant, knowledge_base, source),
            ).fetchall()
        return [self._version_row(row) for row in rows]

    def versions(
        self,
        tenant_id: str,
        kb_id: str,
        source_id: str,
        *,
        include_deleted: bool = True,
    ) -> list[dict[str, Any]]:
        """Backward-compatible alias for :meth:`list_versions`."""

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
        tenant, knowledge_base = _scope(tenant_id, kb_id)
        source = _required_identifier(source_id, "source_id")
        status = _health_status(health_status)
        synced_at = _timestamp(last_sync_at, "last_sync_at")
        if synced_at is None and status == "healthy":
            synced_at = time.time()
        error = None if status == "healthy" else self._sync_error(last_sync_error)
        now = time.time()
        with self._lock:
            self._conn.execute(
                "UPDATE source_catalog_documents "
                "SET health_status=?,last_sync_at=COALESCE(?,last_sync_at),"
                "last_sync_error=?,updated_at=? "
                "WHERE tenant_id=? AND kb_id=? AND source_id=?",
                (status, synced_at, error, now, tenant, knowledge_base, source),
            )
        return self.get(tenant, knowledge_base, source, include_deleted=True)

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
        tenant, knowledge_base = _scope(tenant_id, kb_id)
        connection = _required_identifier(connection_id, "connection_id")
        status = _health_status(health_status)
        synced_at = _timestamp(last_sync_at, "last_sync_at")
        if synced_at is None and status == "healthy":
            synced_at = time.time()
        error = None if status == "healthy" else self._sync_error(last_sync_error)
        if type(job_sequence) is not int or job_sequence < 0:
            raise ValueError("job_sequence must be a non-negative integer")
        if type(job_attempt) is not int or job_attempt < 0:
            raise ValueError("job_attempt must be a non-negative integer")
        if type(event_rank) is not int or event_rank < 0:
            raise ValueError("event_rank must be a non-negative integer")
        deleted_clause = "" if include_deleted else " AND deleted_at IS NULL"
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE source_catalog_documents "
                "SET health_status=?,last_sync_at=COALESCE(?,last_sync_at),"
                "last_sync_error=?,health_job_sequence=?,health_job_attempt=?,"
                "health_event_rank=?,updated_at=? "
                "WHERE tenant_id=? AND kb_id=? AND connection_id=? "
                "AND (? > health_job_sequence OR (? = health_job_sequence AND "
                "(? > health_job_attempt OR (? = health_job_attempt AND "
                "? >= health_event_rank))))" + deleted_clause,
                (
                    status,
                    synced_at,
                    error,
                    job_sequence,
                    job_attempt,
                    event_rank,
                    time.time(),
                    tenant,
                    knowledge_base,
                    connection,
                    job_sequence,
                    job_sequence,
                    job_attempt,
                    job_attempt,
                    event_rank,
                ),
            )
        return cursor.rowcount

    def tombstone(self, tenant_id: str, kb_id: str, source_ids: Iterable[str]) -> int:
        tenant, knowledge_base = _scope(tenant_id, kb_id)
        values = tuple(
            dict.fromkeys(
                _required_identifier(item, "source_id")
                for item in source_ids
                if str(item or "").strip()
            )
        )
        if not values:
            return 0
        now = time.time()
        changed = 0
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                for source_id in values:
                    changed += self._conn.execute(
                        "UPDATE source_catalog_documents "
                        "SET deleted_at=?,updated_at=?,health_status='stale' "
                        "WHERE tenant_id=? AND kb_id=? AND source_id=? AND deleted_at IS NULL",
                        (now, now, tenant, knowledge_base, source_id),
                    ).rowcount
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return changed

    def delete_scope(self, tenant_id: str, kb_id: str) -> dict[str, int]:
        """Permanently remove an already-deleting KB incarnation's catalog."""

        tenant, knowledge_base = _scope(tenant_id, kb_id)
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                versions = self._conn.execute(
                    "DELETE FROM source_catalog_versions WHERE tenant_id=? AND kb_id=?",
                    (tenant, knowledge_base),
                ).rowcount
                documents = self._conn.execute(
                    "DELETE FROM source_catalog_documents WHERE tenant_id=? AND kb_id=?",
                    (tenant, knowledge_base),
                ).rowcount
                self._conn.execute("COMMIT")
            except Exception:
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")
                raise
        return {"documents": int(documents), "versions": int(versions)}

    @staticmethod
    def _document_connection_id(
        document: SourceDocument, explicit: str | None
    ) -> str | None:
        connection = _optional_identifier(explicit, "connection_id")
        raw_metadata = document.metadata.get("connection_id")
        metadata_connection = _optional_identifier(raw_metadata, "connection_id")
        if (
            connection is not None
            and metadata_connection is not None
            and connection != metadata_connection
        ):
            raise ValueError("connection_id conflicts with document metadata")
        return connection or metadata_connection

    @staticmethod
    def _sync_error(value: object) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            return None
        return text[:_MAX_SYNC_ERROR_LENGTH]

    @staticmethod
    def _current_select() -> str:
        return (
            "SELECT d.source_id,d.connection_id,d.connector_type,d.external_id,d.display_name,"
            "d.media_type,d.kind,d.origin_uri,d.current_version_id,d.metadata_json,"
            "d.health_status,d.last_sync_at,d.last_sync_error,d.deleted_at,d.updated_at,"
            "d.health_job_sequence,d.health_job_attempt,d.health_event_rank,"
            "v.content_sha256,v.byte_size,v.etag,v.modified_at,v.fetched_at "
            "FROM source_catalog_documents d JOIN source_catalog_versions v "
            "ON v.tenant_id=d.tenant_id AND v.kb_id=d.kb_id AND v.source_id=d.source_id "
            "AND v.version_id=d.current_version_id"
        )

    @staticmethod
    def _version_select() -> str:
        return (
            "SELECT v.source_id,v.version_id,v.content_sha256,v.byte_size,v.etag,"
            "v.modified_at,v.fetched_at,v.created_at,d.current_version_id "
            "FROM source_catalog_versions v JOIN source_catalog_documents d "
            "ON d.tenant_id=v.tenant_id AND d.kb_id=v.kb_id AND d.source_id=v.source_id"
        )

    @staticmethod
    def _row(row: Any) -> dict[str, Any]:
        return {
            "source_id": row[0],
            "connection_id": row[1],
            "connector_type": row[2],
            "external_id": row[3],
            "display_name": row[4],
            "media_type": row[5],
            "kind": row[6],
            "origin_uri": row[7],
            "version_id": row[8],
            "metadata": json.loads(row[9] or "{}"),
            "health_status": row[10],
            "last_sync_at": row[11],
            "last_sync_error": row[12],
            "deleted_at": row[13],
            "updated_at": row[14],
            "health_job_sequence": row[15],
            "health_job_attempt": row[16],
            "health_event_rank": row[17],
            "content_sha256": row[18],
            "byte_size": row[19],
            "etag": row[20],
            "modified_at": row[21],
            "fetched_at": row[22],
        }

    @staticmethod
    def _version_row(row: Any) -> dict[str, Any]:
        return {
            "source_id": row[0],
            "version_id": row[1],
            "content_sha256": row[2],
            "byte_size": row[3],
            "etag": row[4],
            "modified_at": row[5],
            "fetched_at": row[6],
            "created_at": row[7],
            "is_current": row[1] == row[8],
        }
