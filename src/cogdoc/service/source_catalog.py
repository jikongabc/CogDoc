from __future__ import annotations

import json
import time
from threading import RLock
from typing import Any, Iterable

from cogdoc.api.persistence import connect_sqlite
from cogdoc.source_model import SourceDocument


class SourceCatalog:
    """Durable current-source and immutable-version catalog.

    Index generations remain the atomic retrieval commit pointer. This catalog
    records connector identity and history, allowing later sync attempts to be
    reconciled without trusting mutable files or provider listing order.
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
                connector_type TEXT NOT NULL,
                external_id TEXT NOT NULL,
                display_name TEXT NOT NULL,
                media_type TEXT NOT NULL,
                kind TEXT NOT NULL,
                origin_uri TEXT,
                current_version_id TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
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
            CREATE INDEX IF NOT EXISTS idx_source_catalog_current
                ON source_catalog_documents(tenant_id, kb_id, deleted_at, display_name);
            """
        )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def upsert(
        self, tenant_id: str, kb_id: str, document: SourceDocument
    ) -> dict[str, Any]:
        tenant = str(tenant_id or "").strip()
        knowledge_base = str(kb_id or "").strip()
        if not tenant or not knowledge_base:
            raise ValueError("tenant_id and kb_id are required")
        now = time.time()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._upsert_locked(tenant, knowledge_base, document, now)
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
        self._conn.execute(
            "INSERT INTO source_catalog_documents "
            "(tenant_id,kb_id,source_id,connector_type,external_id,display_name,media_type,kind,origin_uri,current_version_id,metadata_json,deleted_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(tenant_id,kb_id,source_id) DO UPDATE SET "
            "display_name=excluded.display_name,media_type=excluded.media_type,kind=excluded.kind,"
            "origin_uri=excluded.origin_uri,current_version_id=excluded.current_version_id,"
            "metadata_json=excluded.metadata_json,deleted_at=NULL,updated_at=excluded.updated_at",
            (
                tenant_id,
                kb_id,
                document.source_id,
                document.connector_type,
                document.external_id,
                document.display_name,
                document.media_type,
                document.kind.value,
                document.origin_uri,
                version.version_id,
                json.dumps(document.metadata, ensure_ascii=False, sort_keys=True),
                None,
                now,
            ),
        )

    def reconcile(
        self,
        tenant_id: str,
        kb_id: str,
        documents: Iterable[SourceDocument],
        *,
        connector_type: str | None = None,
    ) -> dict[str, int]:
        tenant = str(tenant_id or "").strip()
        knowledge_base = str(kb_id or "").strip()
        if not tenant or not knowledge_base:
            raise ValueError("tenant_id and kb_id are required")
        materialized = list(documents)
        normalized_connector = (
            str(connector_type).strip().casefold()
            if connector_type is not None
            else None
        )
        if normalized_connector is not None and any(
            item.connector_type != normalized_connector for item in materialized
        ):
            raise ValueError("reconcile documents must belong to connector_type")
        seen = {item.source_id for item in materialized}
        deleted = 0
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                now = time.time()
                for item in materialized:
                    self._upsert_locked(tenant, knowledge_base, item, now)
                if normalized_connector is None:
                    self._conn.execute("COMMIT")
                    return {"upserted": len(materialized), "deleted": 0}
                rows = self._conn.execute(
                    "SELECT source_id FROM source_catalog_documents "
                    "WHERE tenant_id=? AND kb_id=? AND connector_type=? AND deleted_at IS NULL",
                    (tenant, knowledge_base, normalized_connector),
                ).fetchall()
                for (source_id,) in rows:
                    if source_id not in seen:
                        self._conn.execute(
                            "UPDATE source_catalog_documents SET deleted_at=?,updated_at=? "
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
        clause = "" if include_deleted else " AND d.deleted_at IS NULL"
        with self._lock:
            row = self._conn.execute(
                "SELECT d.source_id,d.connector_type,d.external_id,d.display_name,d.media_type,d.kind,"
                "d.origin_uri,d.current_version_id,d.metadata_json,d.deleted_at,d.updated_at,"
                "v.content_sha256,v.byte_size,v.etag,v.modified_at,v.fetched_at "
                "FROM source_catalog_documents d JOIN source_catalog_versions v "
                "ON v.tenant_id=d.tenant_id AND v.kb_id=d.kb_id AND v.source_id=d.source_id "
                "AND v.version_id=d.current_version_id "
                "WHERE d.tenant_id=? AND d.kb_id=? AND d.source_id=?" + clause,
                (tenant_id, kb_id, source_id),
            ).fetchone()
        return self._row(row) if row else None

    def list_sources(
        self, tenant_id: str, kb_id: str, *, include_deleted: bool = False
    ) -> list[dict[str, Any]]:
        clause = "" if include_deleted else " AND d.deleted_at IS NULL"
        with self._lock:
            rows = self._conn.execute(
                "SELECT d.source_id,d.connector_type,d.external_id,d.display_name,d.media_type,d.kind,"
                "d.origin_uri,d.current_version_id,d.metadata_json,d.deleted_at,d.updated_at,"
                "v.content_sha256,v.byte_size,v.etag,v.modified_at,v.fetched_at "
                "FROM source_catalog_documents d JOIN source_catalog_versions v "
                "ON v.tenant_id=d.tenant_id AND v.kb_id=d.kb_id AND v.source_id=d.source_id "
                "AND v.version_id=d.current_version_id "
                "WHERE d.tenant_id=? AND d.kb_id=?"
                + clause
                + " ORDER BY d.display_name,d.source_id",
                (tenant_id, kb_id),
            ).fetchall()
        return [self._row(row) for row in rows]

    def versions(
        self, tenant_id: str, kb_id: str, source_id: str
    ) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT version_id,content_sha256,byte_size,etag,modified_at,fetched_at,created_at "
                "FROM source_catalog_versions WHERE tenant_id=? AND kb_id=? AND source_id=? "
                "ORDER BY created_at DESC,version_id",
                (tenant_id, kb_id, source_id),
            ).fetchall()
        return [
            {
                "version_id": row[0],
                "content_sha256": row[1],
                "byte_size": row[2],
                "etag": row[3],
                "modified_at": row[4],
                "fetched_at": row[5],
                "created_at": row[6],
            }
            for row in rows
        ]

    def tombstone(self, tenant_id: str, kb_id: str, source_ids: Iterable[str]) -> int:
        values = tuple(
            dict.fromkeys(str(item).strip() for item in source_ids if str(item).strip())
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
                        "UPDATE source_catalog_documents SET deleted_at=?,updated_at=? "
                        "WHERE tenant_id=? AND kb_id=? AND source_id=? AND deleted_at IS NULL",
                        (now, now, tenant_id, kb_id, source_id),
                    ).rowcount
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return changed

    @staticmethod
    def _row(row) -> dict[str, Any]:
        return {
            "source_id": row[0],
            "connector_type": row[1],
            "external_id": row[2],
            "display_name": row[3],
            "media_type": row[4],
            "kind": row[5],
            "origin_uri": row[6],
            "version_id": row[7],
            "metadata": json.loads(row[8] or "{}"),
            "deleted_at": row[9],
            "updated_at": row[10],
            "content_sha256": row[11],
            "byte_size": row[12],
            "etag": row[13],
            "modified_at": row[14],
            "fetched_at": row[15],
        }
