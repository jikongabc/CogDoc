from __future__ import annotations

import hashlib
import json
import math
import secrets
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from typing import Any
from uuid import uuid4

from cogdoc.api.derived_knowledge_store import DerivedKnowledgeStore
from cogdoc.api.tenancy import Permission
from cogdoc.ha.feedback import (
    HA_KB_EPOCH_FIELD,
    StaleAuxiliaryWrite,
    _canonical_record,
    _placeholders,
    _row_value,
    assert_active_kb_epoch,
)
from cogdoc.ha.storage import DatabaseBackend, DatabaseConnection, execute_script


class DistributedDerivedKnowledgeStore(DerivedKnowledgeStore):
    """Shared append-only knowledge ledger with a cross-node writer fence."""

    def __init__(
        self,
        backend: DatabaseBackend,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.backend = backend
        self._clock = clock
        self._lock = threading.RLock()
        self._transaction = threading.local()
        self._authority_checker: Callable[..., None] | None = None
        execute_script(
            backend,
            (
                """CREATE TABLE IF NOT EXISTS ha_derived_knowledge_sequence (
                singleton INTEGER PRIMARY KEY CHECK(singleton=1),last_value BIGINT NOT NULL)""",
                "INSERT INTO ha_derived_knowledge_sequence(singleton,last_value) "
                "VALUES(1,0) ON CONFLICT(singleton) DO NOTHING",
                """CREATE TABLE IF NOT EXISTS ha_derived_knowledge_events (
                event_sequence BIGINT PRIMARY KEY,event_key TEXT NOT NULL UNIQUE,
                knowledge_id TEXT NOT NULL,kb_id TEXT NOT NULL,status TEXT NOT NULL,
                normalized_hash TEXT,conflict_group_id TEXT,related_document_id TEXT,
                related_source TEXT,origin TEXT,created_by TEXT,created_at TEXT NOT NULL,
                reviewed_at TEXT,record_json TEXT NOT NULL)""",
                "CREATE INDEX IF NOT EXISTS idx_ha_knowledge_latest "
                "ON ha_derived_knowledge_events(knowledge_id,event_sequence DESC)",
                "CREATE INDEX IF NOT EXISTS idx_ha_knowledge_kb "
                "ON ha_derived_knowledge_events(kb_id,event_sequence DESC)",
                "CREATE INDEX IF NOT EXISTS idx_ha_knowledge_status "
                "ON ha_derived_knowledge_events(kb_id,status,event_sequence DESC)",
                "CREATE INDEX IF NOT EXISTS idx_ha_knowledge_hash "
                "ON ha_derived_knowledge_events(kb_id,normalized_hash,event_sequence DESC)",
                """CREATE TABLE IF NOT EXISTS ha_derived_knowledge_refreshes (
                kb_id TEXT PRIMARY KEY,requested_sequence BIGINT NOT NULL,
                status TEXT NOT NULL,lease_owner TEXT,lease_token TEXT,
                lease_expires_at DOUBLE PRECISION,attempts BIGINT NOT NULL DEFAULT 0,
                last_error TEXT,updated_at DOUBLE PRECISION NOT NULL)""",
                "CREATE INDEX IF NOT EXISTS idx_ha_knowledge_refresh_recovery "
                "ON ha_derived_knowledge_refreshes(status,lease_expires_at,updated_at,kb_id)",
            ),
        )

    def bind_authority_checker(self, checker: Callable[..., None]) -> None:
        if not callable(checker):
            raise TypeError("derived knowledge authority checker must be callable")
        if (
            self._authority_checker is not None
            and self._authority_checker is not checker
        ):
            raise ValueError("derived knowledge authority checker is already bound")
        self._authority_checker = checker

    def _assert_authority(
        self,
        connection: DatabaseConnection,
        authority: Mapping[str, Any] | None,
        *,
        permission: Permission,
    ) -> None:
        if authority is None or self._authority_checker is None:
            raise StaleAuxiliaryWrite("shared knowledge mutation authority is missing")
        try:
            self._authority_checker(
                connection,
                authority,
                required_permission=permission,
            )
        except Exception as exc:
            raise StaleAuxiliaryWrite(
                "shared knowledge mutation authority is stale"
            ) from exc

    def _assert_record_epoch(
        self,
        connection: DatabaseConnection,
        record: Mapping[str, Any],
        expected_epoch: int,
    ) -> None:
        assert_active_kb_epoch(
            connection,
            self.backend,
            storage_id=str(record.get("kb_id") or ""),
            expected_epoch=expected_epoch,
        )

    def authority_snapshot(
        self, knowledge_id: str
    ) -> tuple[dict[str, Any], int] | None:
        marker = self.backend.sql(sqlite="?", postgres="%s")
        with self.backend.transaction() as connection:
            row = connection.execute(
                "SELECT event_sequence,record_json FROM ha_derived_knowledge_events "
                f"WHERE knowledge_id={marker} "
                "ORDER BY event_sequence DESC LIMIT 1",
                (knowledge_id,),
            ).fetchone()
        if row is None:
            return None
        return (
            json.loads(str(_row_value(row, "record_json", 1))),
            int(_row_value(row, "event_sequence", 0)),
        )

    def _assert_event_sequence(
        self,
        connection: DatabaseConnection,
        knowledge_id: str,
        expected_event_sequence: int,
    ) -> None:
        if type(expected_event_sequence) is not int or expected_event_sequence < 1:
            raise StaleAuxiliaryWrite("shared knowledge row version is missing")
        marker = self.backend.sql(sqlite="?", postgres="%s")
        row = connection.execute(
            "SELECT MAX(event_sequence) AS event_sequence "
            "FROM ha_derived_knowledge_events "
            f"WHERE knowledge_id={marker}",
            (knowledge_id,),
        ).fetchone()
        live = None if row is None else _row_value(row, "event_sequence", 0)
        if live is None or int(live) != expected_event_sequence:
            raise StaleAuxiliaryWrite("shared knowledge row changed")

    @property
    def _active_connection(self) -> DatabaseConnection | None:
        return getattr(self._transaction, "connection", None)

    @contextmanager
    def _write_transaction(self) -> Iterator[None]:
        with self._lock:
            active = self._active_connection
            if active is not None:
                yield
                return
            marker = self.backend.sql(sqlite="?", postgres="%s")
            with self.backend.transaction(write=True) as connection:
                # Serialize the read/derive/append sequence across all nodes.
                connection.execute(
                    "UPDATE ha_derived_knowledge_sequence SET last_value=last_value "
                    f"WHERE singleton={marker}",
                    (1,),
                )
                self._transaction.connection = connection
                try:
                    yield
                finally:
                    del self._transaction.connection

    def create(self, payload: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        payload = dict(payload)
        raw_epoch = payload.pop(HA_KB_EPOCH_FIELD, None)
        expected_epoch = int(raw_epoch) if raw_epoch is not None else None
        with self._write_transaction():
            assert_active_kb_epoch(
                self._required_connection(),
                self.backend,
                storage_id=str(payload.get("kb_id") or ""),
                expected_epoch=expected_epoch,
            )
            return super().create(payload)

    def create_authorized(
        self,
        payload: dict[str, Any],
        *,
        expected_epoch: int,
        authority: Mapping[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        payload = dict(payload)
        payload.pop(HA_KB_EPOCH_FIELD, None)
        with self._write_transaction():
            connection = self._required_connection()
            assert_active_kb_epoch(
                connection,
                self.backend,
                storage_id=str(payload.get("kb_id") or ""),
                expected_epoch=expected_epoch,
            )
            self._assert_authority(connection, authority, permission=Permission.WRITE)
            return super().create(payload)

    def revise(
        self, knowledge_id: str, payload: dict[str, Any]
    ) -> dict[str, Any] | None:
        with self._write_transaction():
            return super().revise(knowledge_id, payload)

    def revise_authorized(
        self,
        knowledge_id: str,
        payload: dict[str, Any],
        *,
        expected_epoch: int,
        expected_event_sequence: int,
        authority: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        with self._write_transaction():
            current = super().get(knowledge_id)
            if current is None:
                return None
            connection = self._required_connection()
            self._assert_event_sequence(
                connection, knowledge_id, expected_event_sequence
            )
            self._assert_record_epoch(connection, current, expected_epoch)
            self._assert_authority(connection, authority, permission=Permission.WRITE)
            return super().revise(knowledge_id, payload)

    def set_status(
        self,
        knowledge_id: str,
        status: str,
        *,
        actor: str | None = None,
        note: str | None = None,
        binding_updates: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        with self._write_transaction():
            return super().set_status(
                knowledge_id,
                status,
                actor=actor,
                note=note,
                binding_updates=binding_updates,
            )

    def set_status_authorized(
        self,
        knowledge_id: str,
        status: str,
        *,
        expected_epoch: int,
        expected_event_sequence: int,
        authority: Mapping[str, Any],
        actor: str | None = None,
        note: str | None = None,
        binding_updates: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        with self._write_transaction():
            current = super().get(knowledge_id)
            if current is None:
                return None
            connection = self._required_connection()
            self._assert_event_sequence(
                connection, knowledge_id, expected_event_sequence
            )
            self._assert_record_epoch(connection, current, expected_epoch)
            self._assert_authority(connection, authority, permission=Permission.REVIEW)
            return super().set_status(
                knowledge_id,
                status,
                actor=actor,
                note=note,
                binding_updates=binding_updates,
            )

    def batch_set_status(
        self,
        knowledge_ids: list[str],
        status: str,
        *,
        actor: str | None = None,
        note: str | None = None,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        with self._write_transaction():
            return super().batch_set_status(
                knowledge_ids, status, actor=actor, note=note
            )

    def batch_set_status_authorized(
        self,
        knowledge_ids: list[str],
        status: str,
        *,
        authorities: Mapping[str, tuple[int, Mapping[str, Any]]],
        expected_event_sequences: Mapping[str, int],
        actor: str | None = None,
        note: str | None = None,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        with self._write_transaction():
            connection = self._required_connection()
            checked: set[str] = set()
            for knowledge_id in knowledge_ids:
                current = super().get(knowledge_id)
                if current is None:
                    continue
                expected_sequence = expected_event_sequences.get(knowledge_id)
                if expected_sequence is None:
                    raise StaleAuxiliaryWrite(
                        "shared knowledge batch row version is missing"
                    )
                self._assert_event_sequence(connection, knowledge_id, expected_sequence)
                storage_id = str(current.get("kb_id") or "")
                frozen = authorities.get(storage_id)
                if frozen is None:
                    raise StaleAuxiliaryWrite(
                        "shared knowledge batch authority is missing"
                    )
                if storage_id in checked:
                    continue
                expected_epoch, authority = frozen
                self._assert_record_epoch(connection, current, expected_epoch)
                self._assert_authority(
                    connection, authority, permission=Permission.REVIEW
                )
                checked.add(storage_id)
            return super().batch_set_status(
                knowledge_ids, status, actor=actor, note=note
            )

    def delete(self, knowledge_id: str) -> dict[str, Any] | None:
        marker = self.backend.sql(sqlite="?", postgres="%s")
        with self._write_transaction():
            latest = self.get(knowledge_id)
            if latest is None:
                return None
            connection = self._required_connection()
            deleted = connection.execute(
                f"DELETE FROM ha_derived_knowledge_events WHERE knowledge_id={marker}",
                (knowledge_id,),
            )
            if deleted.rowcount:
                sequence = self._bump_revision(connection)
                self._enqueue_refresh(
                    connection, str(latest.get("kb_id") or ""), sequence
                )
            return latest

    def delete_authorized(
        self,
        knowledge_id: str,
        *,
        expected_epoch: int,
        expected_event_sequence: int,
        authority: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        with self._write_transaction():
            current = super().get(knowledge_id)
            if current is None:
                return None
            connection = self._required_connection()
            self._assert_event_sequence(
                connection, knowledge_id, expected_event_sequence
            )
            self._assert_record_epoch(connection, current, expected_epoch)
            self._assert_authority(connection, authority, permission=Permission.DELETE)
            return self.delete(knowledge_id)

    def clear_kb(self, kb_id: str) -> None:
        marker = self.backend.sql(sqlite="?", postgres="%s")
        with self._write_transaction():
            connection = self._required_connection()
            deleted = connection.execute(
                f"DELETE FROM ha_derived_knowledge_events WHERE kb_id={marker}",
                (kb_id,),
            )
            if deleted.rowcount:
                self._bump_revision(connection)
            # KB teardown owns this path.  Do not leave a rebuild task capable
            # of reviving a deleted incarnation.
            connection.execute(
                f"DELETE FROM ha_derived_knowledge_refreshes WHERE kb_id={marker}",
                (kb_id,),
            )

    def mark_stale_by_documents(
        self, kb_id: str, documents: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        with self._write_transaction():
            return super().mark_stale_by_documents(kb_id, documents)

    def mark_stale_by_documents_authorized(
        self,
        kb_id: str,
        documents: list[dict[str, Any]],
        *,
        expected_epoch: int,
        authority: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        with self._write_transaction():
            connection = self._required_connection()
            assert_active_kb_epoch(
                connection,
                self.backend,
                storage_id=kb_id,
                expected_epoch=expected_epoch,
            )
            self._assert_authority(connection, authority, permission=Permission.WRITE)
            return super().mark_stale_by_documents(kb_id, documents)

    def mark_stale_for_source(
        self, kb_id: str, source: str, old_source_sha256: str
    ) -> list[dict[str, Any]]:
        with self._write_transaction():
            return super().mark_stale_for_source(kb_id, source, old_source_sha256)

    def revision_token(self) -> str:
        connection = self._active_connection
        if connection is not None:
            row = connection.execute(
                "SELECT last_value FROM ha_derived_knowledge_sequence WHERE singleton=1"
            ).fetchone()
        else:
            with self.backend.transaction() as reader:
                row = reader.execute(
                    "SELECT last_value FROM ha_derived_knowledge_sequence WHERE singleton=1"
                ).fetchone()
        return f"ha:{int(_row_value(row, 'last_value', 0)) if row is not None else 0}"

    def export_records(self, *, kb_id: str | None = None) -> list[dict[str, Any]]:
        marker = self.backend.sql(sqlite="?", postgres="%s")
        query = "SELECT record_json FROM ha_derived_knowledge_events"
        params: tuple[Any, ...] = ()
        if kb_id is not None:
            query += f" WHERE kb_id={marker}"
            params = (kb_id,)
        query += " ORDER BY event_sequence"
        connection = self._active_connection
        if connection is not None:
            rows = connection.execute(query, params).fetchall()
        else:
            with self.backend.transaction() as reader:
                rows = reader.execute(query, params).fetchall()
        return [json.loads(str(_row_value(row, "record_json", 0))) for row in rows]

    def import_records(self, records: list[dict[str, Any]]) -> dict[str, int]:
        prepared: list[tuple[dict[str, Any], str, str]] = []
        occurrences: dict[str, int] = {}
        for raw in records:
            record, encoded = _canonical_record(raw)
            if not str(record.get("knowledge_id") or "").strip():
                raise ValueError("imported record knowledge_id must not be blank")
            if not str(record.get("kb_id") or "").strip():
                raise ValueError("imported record kb_id must not be blank")
            identity = json.dumps(
                {
                    key: record.get(key)
                    for key in (
                        "knowledge_id",
                        "kb_id",
                        "version",
                        "status",
                        "created_at",
                        "updated_at",
                        "reviewed_at",
                        "archived_at",
                    )
                },
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            digest = hashlib.sha256(identity.encode()).hexdigest()
            occurrence = occurrences.get(digest, 0)
            occurrences[digest] = occurrence + 1
            prepared.append((record, f"import:{digest}:{occurrence}", encoded))

        imported = skipped = 0
        marker = self.backend.sql(sqlite="?", postgres="%s")
        with self._write_transaction():
            connection = self._required_connection()
            for record, event_key, encoded in prepared:
                existing = connection.execute(
                    "SELECT record_json FROM ha_derived_knowledge_events "
                    f"WHERE event_key={marker}",
                    (event_key,),
                ).fetchone()
                if existing is not None:
                    existing_record, existing_encoded = _canonical_record(
                        json.loads(str(_row_value(existing, "record_json", 0)))
                    )
                    del existing_record
                    if existing_encoded != encoded:
                        raise ValueError(f"import event key conflict: {event_key}")
                    skipped += 1
                    continue
                self._insert_record(connection, record, event_key=event_key)
                imported += 1
        return {"imported": imported, "skipped": skipped}

    def _read_history(self) -> list[dict[str, Any]]:
        return self.export_records()

    def _latest(self) -> dict[str, dict[str, Any]]:
        query = (
            "SELECT record_json FROM ha_derived_knowledge_events WHERE "
            "event_sequence IN (SELECT MAX(event_sequence) "
            "FROM ha_derived_knowledge_events GROUP BY knowledge_id)"
        )
        connection = self._active_connection
        if connection is not None:
            rows = connection.execute(query).fetchall()
        else:
            with self.backend.transaction() as reader:
                rows = reader.execute(query).fetchall()
        latest: dict[str, dict[str, Any]] = {}
        for row in rows:
            record = json.loads(str(_row_value(row, "record_json", 0)))
            latest[str(record["knowledge_id"])] = record
        return latest

    def _append(self, entry: dict[str, Any]) -> None:
        connection = self._required_connection()
        self._insert_record(connection, entry, event_key=f"event:{uuid4().hex}")

    def _rewrite_history(self, rows: list[dict[str, Any]]) -> None:
        connection = self._required_connection()
        previous = connection.execute(
            "SELECT DISTINCT kb_id FROM ha_derived_knowledge_events"
        ).fetchall()
        connection.execute("DELETE FROM ha_derived_knowledge_events")
        sequence = self._bump_revision(connection)
        for row in previous:
            self._enqueue_refresh(
                connection, str(_row_value(row, "kb_id", 0)), sequence
            )
        for entry in rows:
            self._insert_record(connection, entry, event_key=f"event:{uuid4().hex}")

    def _insert_record(
        self,
        connection: DatabaseConnection,
        entry: Mapping[str, Any],
        *,
        event_key: str,
    ) -> None:
        clean, encoded = _canonical_record(entry)
        sequence = self._bump_revision(connection)
        connection.execute(
            "INSERT INTO ha_derived_knowledge_events("
            "event_sequence,event_key,knowledge_id,kb_id,status,normalized_hash,"
            "conflict_group_id,related_document_id,related_source,origin,created_by,"
            "created_at,reviewed_at,record_json) VALUES("
            f"{_placeholders(self.backend, 14)})",
            (
                sequence,
                event_key,
                str(clean.get("knowledge_id") or ""),
                str(clean.get("kb_id") or ""),
                str(clean.get("status") or ""),
                clean.get("normalized_hash"),
                clean.get("conflict_group_id"),
                clean.get("related_document_id"),
                clean.get("related_source"),
                clean.get("origin"),
                clean.get("created_by"),
                str(clean.get("created_at") or ""),
                clean.get("reviewed_at"),
                encoded,
            ),
        )
        self._enqueue_refresh(connection, str(clean.get("kb_id") or ""), sequence)

    def _enqueue_refresh(
        self, connection: DatabaseConnection, kb_id: str, sequence: int
    ) -> None:
        if not kb_id:
            raise ValueError("derived knowledge refresh kb_id is invalid")
        now = float(self._clock())
        placeholders = _placeholders(self.backend, 4)
        connection.execute(
            "INSERT INTO ha_derived_knowledge_refreshes("
            "kb_id,requested_sequence,status,updated_at) "
            f"VALUES({placeholders}) ON CONFLICT(kb_id) DO UPDATE SET "
            "requested_sequence=excluded.requested_sequence,updated_at=excluded.updated_at,"
            "status=CASE WHEN ha_derived_knowledge_refreshes.status='running' "
            "AND ha_derived_knowledge_refreshes.lease_expires_at>excluded.updated_at "
            "THEN ha_derived_knowledge_refreshes.status ELSE 'pending' END,"
            "lease_owner=CASE WHEN ha_derived_knowledge_refreshes.status='running' "
            "AND ha_derived_knowledge_refreshes.lease_expires_at>excluded.updated_at "
            "THEN ha_derived_knowledge_refreshes.lease_owner ELSE NULL END,"
            "lease_token=CASE WHEN ha_derived_knowledge_refreshes.status='running' "
            "AND ha_derived_knowledge_refreshes.lease_expires_at>excluded.updated_at "
            "THEN ha_derived_knowledge_refreshes.lease_token ELSE NULL END,"
            "lease_expires_at=CASE WHEN ha_derived_knowledge_refreshes.status='running' "
            "AND ha_derived_knowledge_refreshes.lease_expires_at>excluded.updated_at "
            "THEN ha_derived_knowledge_refreshes.lease_expires_at ELSE NULL END",
            (kb_id, sequence, "pending", now),
        )

    def pending_refreshes(self, *, limit: int = 1000) -> list[dict[str, Any]]:
        if type(limit) is not int or not 1 <= limit <= 10_000:
            raise ValueError("refresh recovery limit must be between 1 and 10000")
        now = float(self._clock())
        marker = self.backend.sql(sqlite="?", postgres="%s")
        with self.backend.transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM ha_derived_knowledge_refreshes WHERE status='pending' "
                f"OR (status='running' AND lease_expires_at<={marker}) "
                f"ORDER BY updated_at,kb_id LIMIT {limit}",
                (now,),
            ).fetchall()
        return [dict(row) for row in rows]

    def claim_refresh(
        self,
        kb_id: str,
        worker_id: str,
        *,
        lease_seconds: float = 3600.0,
    ) -> dict[str, Any] | None:
        if not isinstance(kb_id, str) or not kb_id:
            raise ValueError("refresh kb_id is invalid")
        if not isinstance(worker_id, str) or not worker_id:
            raise ValueError("refresh worker_id is invalid")
        if not math.isfinite(lease_seconds) or not 5 <= lease_seconds <= 3600:
            raise ValueError("refresh lease_seconds must be between 5 and 3600")
        now = float(self._clock())
        token = secrets.token_urlsafe(32)
        marker = self.backend.sql(sqlite="?", postgres="%s")
        lock = self.backend.sql(sqlite="", postgres=" FOR UPDATE")
        with self.backend.transaction(write=True) as connection:
            raw = connection.execute(
                "SELECT * FROM ha_derived_knowledge_refreshes "
                f"WHERE kb_id={marker}{lock}",
                (kb_id,),
            ).fetchone()
            if raw is None:
                return None
            row = dict(raw)
            expires = row.get("lease_expires_at")
            if (
                row.get("status") == "running"
                and expires is not None
                and float(expires) > now
            ):
                return None
            if row.get("status") == "completed":
                return None
            changed = connection.execute(
                "UPDATE ha_derived_knowledge_refreshes SET status='running',"
                f"lease_owner={marker},lease_token={marker},lease_expires_at={marker},"
                f"attempts=attempts+1,last_error=NULL,updated_at={marker} "
                f"WHERE kb_id={marker}",
                (worker_id, token, now + lease_seconds, now, kb_id),
            )
            if changed.rowcount != 1:
                return None
            row.update(
                {
                    "status": "running",
                    "lease_owner": worker_id,
                    "lease_token": token,
                    "lease_expires_at": now + lease_seconds,
                    "attempts": int(row.get("attempts") or 0) + 1,
                    "last_error": None,
                    "updated_at": now,
                }
            )
            return row

    def complete_refresh(
        self, kb_id: str, lease_token: str, requested_sequence: int
    ) -> bool:
        return self._finish_refresh(
            kb_id, lease_token, requested_sequence, error_class=None
        )

    def heartbeat_refresh(
        self,
        kb_id: str,
        lease_token: str,
        *,
        lease_seconds: float = 3600.0,
    ) -> bool:
        if not kb_id or not lease_token:
            raise ValueError("refresh heartbeat identity is invalid")
        if not math.isfinite(lease_seconds) or not 5 <= lease_seconds <= 3600:
            raise ValueError("refresh lease_seconds must be between 5 and 3600")
        now = float(self._clock())
        marker = self.backend.sql(sqlite="?", postgres="%s")
        with self.backend.transaction(write=True) as connection:
            changed = connection.execute(
                "UPDATE ha_derived_knowledge_refreshes SET "
                f"lease_expires_at={marker},updated_at={marker} "
                f"WHERE kb_id={marker} AND status='running' "
                f"AND lease_token={marker} AND lease_expires_at>{marker}",
                (now + lease_seconds, now, kb_id, lease_token, now),
            )
        return changed.rowcount == 1

    def fail_refresh(
        self,
        kb_id: str,
        lease_token: str,
        requested_sequence: int,
        error_class: str,
    ) -> bool:
        if not isinstance(error_class, str) or not error_class:
            raise ValueError("refresh error_class is invalid")
        return self._finish_refresh(
            kb_id,
            lease_token,
            requested_sequence,
            error_class=error_class[:255],
        )

    def _finish_refresh(
        self,
        kb_id: str,
        lease_token: str,
        requested_sequence: int,
        *,
        error_class: str | None,
    ) -> bool:
        if not kb_id or not lease_token or type(requested_sequence) is not int:
            raise ValueError("refresh completion identity is invalid")
        now = float(self._clock())
        marker = self.backend.sql(sqlite="?", postgres="%s")
        lock = self.backend.sql(sqlite="", postgres=" FOR UPDATE")
        with self.backend.transaction(write=True) as connection:
            raw = connection.execute(
                "SELECT requested_sequence,status,lease_token "
                "FROM ha_derived_knowledge_refreshes "
                f"WHERE kb_id={marker}{lock}",
                (kb_id,),
            ).fetchone()
            if raw is None:
                return False
            row = dict(raw)
            if row.get("status") != "running" or row.get("lease_token") != lease_token:
                return False
            live_sequence = int(row["requested_sequence"])
            if error_class is None and live_sequence == requested_sequence:
                completed = connection.execute(
                    "UPDATE ha_derived_knowledge_refreshes SET status='completed',"
                    "lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,"
                    f"last_error=NULL,updated_at={marker} "
                    f"WHERE kb_id={marker} AND lease_token={marker}",
                    (now, kb_id, lease_token),
                )
                return completed.rowcount == 1
            changed = connection.execute(
                "UPDATE ha_derived_knowledge_refreshes SET status='pending',"
                "lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,"
                f"last_error={marker},updated_at={marker} "
                f"WHERE kb_id={marker} AND lease_token={marker}",
                (error_class, now, kb_id, lease_token),
            )
            return changed.rowcount == 1

    def _bump_revision(self, connection: DatabaseConnection) -> int:
        marker = self.backend.sql(sqlite="?", postgres="%s")
        connection.execute(
            "UPDATE ha_derived_knowledge_sequence SET last_value=last_value+1 "
            f"WHERE singleton={marker}",
            (1,),
        )
        row = connection.execute(
            "SELECT last_value FROM ha_derived_knowledge_sequence WHERE singleton=1"
        ).fetchone()
        if row is None:
            raise RuntimeError("derived knowledge sequence is unavailable")
        return int(_row_value(row, "last_value", 0))

    def _required_connection(self) -> DatabaseConnection:
        connection = self._active_connection
        if connection is None:
            raise RuntimeError("derived knowledge write requires a transaction")
        return connection

    def close(self) -> None:
        """The HA runtime owns the shared backend lifecycle."""


__all__ = ["DistributedDerivedKnowledgeStore"]
