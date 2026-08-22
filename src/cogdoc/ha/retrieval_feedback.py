from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Callable, TypeAlias
from uuid import uuid4

from cogdoc.api.retrieval_eval_draft_store import (
    DraftRevisionConflictError,
    RetrievalEvalDraftStore,
    _check_expected_revision,
    _clone,
    _partition_value,
    _record,
    _status_value,
)
from cogdoc.api.retrieval_feedback_store import (
    RetrievalFeedbackStore,
    _aggregate_retrieval_feedback_group,
    _attributed_feedback_weight,
    _feedback_group_key,
    _now_iso,
    _record_targets,
    _required_text,
    _target_chunks,
    query_hash,
)
from cogdoc.api.tenancy import Permission
from cogdoc.ha.feedback import (
    HA_KB_EPOCH_FIELD,
    StaleAuxiliaryWrite,
    _canonical_record,
    _placeholders,
    _row_value,
    assert_live_auxiliary_authority,
    assert_active_kb_epoch,
)
from cogdoc.ha.storage import DatabaseBackend, DatabaseConnection, execute_script
from cogdoc.tools.eval.retrieval_eval_drafts import (
    DatasetPartition,
    DraftStatus,
    RetrievalEvalDraft,
    apply_review_annotations,
    approve_draft,
    draft_snapshot_identity_key,
    export_retrieval_eval_case,
    reject_draft,
)


_Rows: TypeAlias = list[dict[str, Any]]


class DistributedRetrievalFeedbackStore(RetrievalFeedbackStore):
    """Shared retrieval-tuning ledger; each user feedback is applied once."""

    def __init__(self, backend: DatabaseBackend) -> None:
        self.backend = backend
        self._authority_checker: Callable[..., None] | None = None
        execute_script(
            backend,
            (
                """CREATE TABLE IF NOT EXISTS ha_retrieval_feedback (
                retrieval_feedback_id TEXT PRIMARY KEY,feedback_id TEXT NOT NULL UNIQUE,
                feedback_group_key TEXT NOT NULL,kb_id TEXT NOT NULL,query_hash TEXT NOT NULL,
                enabled INTEGER NOT NULL,created_at TEXT NOT NULL,data TEXT NOT NULL)""",
                "CREATE INDEX IF NOT EXISTS idx_ha_retrieval_feedback_kb_created "
                "ON ha_retrieval_feedback(kb_id,created_at DESC,retrieval_feedback_id DESC)",
                "CREATE INDEX IF NOT EXISTS idx_ha_retrieval_feedback_boosts "
                "ON ha_retrieval_feedback(kb_id,query_hash,enabled)",
                "CREATE INDEX IF NOT EXISTS idx_ha_retrieval_feedback_group "
                "ON ha_retrieval_feedback(feedback_group_key)",
            ),
        )

    def bind_authority_checker(self, checker: Callable[..., None]) -> None:
        if not callable(checker):
            raise TypeError("retrieval feedback authority checker must be callable")
        if (
            self._authority_checker is not None
            and self._authority_checker is not checker
        ):
            raise ValueError("retrieval feedback authority checker is already bound")
        self._authority_checker = checker

    def record_from_feedback(self, feedback_id: str, payload: dict[str, Any]) -> _Rows:
        return self._record_from_feedback(
            feedback_id,
            payload,
            authority=None,
            explicit_epoch=None,
        )

    def record_from_feedback_authorized(
        self,
        feedback_id: str,
        payload: dict[str, Any],
        *,
        expected_epoch: int,
        authority: Mapping[str, Any],
    ) -> _Rows:
        return self._record_from_feedback(
            feedback_id,
            payload,
            authority=authority,
            explicit_epoch=expected_epoch,
        )

    def _record_from_feedback(
        self,
        feedback_id: str,
        payload: dict[str, Any],
        *,
        authority: Mapping[str, Any] | None,
        explicit_epoch: int | None,
    ) -> _Rows:
        raw_epoch = payload.get(HA_KB_EPOCH_FIELD)
        expected_epoch = (
            explicit_epoch
            if explicit_epoch is not None
            else (int(raw_epoch) if raw_epoch is not None else None)
        )
        kb_id = _required_text(payload, "kb_id")
        query_text = _required_text(payload, "query")
        if not kb_id or not query_text:
            return []
        user_score, weight_delta = _attributed_feedback_weight(payload)
        targets = _target_chunks(payload)
        if weight_delta == 0 or not targets:
            return []
        source_types = sorted({target["source_type"] for target in targets})
        record, encoded = _canonical_record(
            {
                "retrieval_feedback_id": uuid4().hex,
                "feedback_id": feedback_id,
                "kb_id": kb_id,
                "query_hash": query_hash(query_text),
                "query_text": query_text,
                "chunk_id": targets[0]["chunk_id"],
                "source_type": source_types[0] if len(source_types) == 1 else "mixed",
                "target_chunks": targets,
                "chunk_count": len(targets),
                "trace_id": payload.get("trace_id"),
                "user_score": user_score,
                "agent_score": None,
                "agent_reason": None,
                "weight_delta": weight_delta,
                "confidence": 1.0,
                "enabled": True,
                "disabled_at": None,
                "disabled_by": None,
                "disable_reason": None,
                "created_at": _now_iso(),
            }
        )
        marker = self.backend.sql(sqlite="?", postgres="%s")
        with self.backend.transaction(write=True) as connection:
            assert_active_kb_epoch(
                connection,
                self.backend,
                storage_id=kb_id,
                expected_epoch=expected_epoch,
            )
            if authority is not None:
                assert_live_auxiliary_authority(
                    self._authority_checker,
                    connection,
                    authority,
                    permission=Permission.WRITE,
                )
            inserted = connection.execute(
                "INSERT INTO ha_retrieval_feedback("
                "retrieval_feedback_id,feedback_id,feedback_group_key,kb_id,query_hash,"
                "enabled,created_at,data) VALUES("
                f"{_placeholders(self.backend, 8)}) "
                "ON CONFLICT(feedback_id) DO NOTHING",
                (
                    record["retrieval_feedback_id"],
                    feedback_id,
                    _feedback_group_key(record),
                    kb_id,
                    record["query_hash"],
                    1,
                    record["created_at"],
                    encoded,
                ),
            )
            if inserted.rowcount == 1:
                return [record]
            row = connection.execute(
                f"SELECT data FROM ha_retrieval_feedback WHERE feedback_id={marker}",
                (feedback_id,),
            ).fetchone()
            if row is None:
                raise RuntimeError("deduplicated retrieval feedback is unavailable")
            return [json.loads(str(_row_value(row, "data", 0)))]

    def boosts_for_query(self, kb_id: str, query: str) -> dict[str, float]:
        marker = self.backend.sql(sqlite="?", postgres="%s")
        with self.backend.transaction() as connection:
            rows = connection.execute(
                "SELECT data FROM ha_retrieval_feedback "
                f"WHERE kb_id={marker} AND query_hash={marker} AND enabled=1",
                (kb_id, query_hash(query)),
            ).fetchall()
        boosts: dict[str, float] = {}
        for raw in rows:
            row = json.loads(str(_row_value(raw, "data", 0)))
            for target in _record_targets(row):
                chunk_id = target["chunk_id"]
                boosts[chunk_id] = boosts.get(chunk_id, 0.0) + float(
                    row.get("weight_delta") or 0.0
                ) * float(row.get("confidence") or 1.0)
        return boosts

    def list(
        self, *, kb_id: str, enabled: bool | None = None, limit: int = 100
    ) -> _Rows:
        marker = self.backend.sql(sqlite="?", postgres="%s")
        clauses = [f"kb_id={marker}"]
        params: list[Any] = [kb_id]
        if enabled is not None:
            clauses.append(f"enabled={marker}")
            params.append(int(enabled))
        params.append(max(0, min(int(limit), 10_000)))
        with self.backend.transaction() as connection:
            rows = connection.execute(
                "SELECT data FROM ha_retrieval_feedback WHERE "
                + " AND ".join(clauses)
                + f" ORDER BY created_at DESC,retrieval_feedback_id DESC LIMIT {marker}",
                tuple(params),
            ).fetchall()
        decoded = [json.loads(str(_row_value(row, "data", 0))) for row in rows]
        groups: dict[str, _Rows] = {}
        for row in decoded:
            groups.setdefault(_feedback_group_key(row), []).append(row)
        return [
            _aggregate_retrieval_feedback_group(group) for group in groups.values()
        ][:limit]

    def counts(self, *, kb_id: str) -> dict[str, int]:
        marker = self.backend.sql(sqlite="?", postgres="%s")
        with self.backend.transaction() as connection:
            rows = connection.execute(
                "SELECT feedback_group_key,MAX(enabled) AS any_enabled "
                f"FROM ha_retrieval_feedback WHERE kb_id={marker} "
                "GROUP BY feedback_group_key",
                (kb_id,),
            ).fetchall()
        enabled = sum(bool(_row_value(row, "any_enabled", 1)) for row in rows)
        return {"total": len(rows), "enabled": enabled, "disabled": len(rows) - enabled}

    def clear_kb(self, kb_id: str) -> None:
        marker = self.backend.sql(sqlite="?", postgres="%s")
        with self.backend.transaction(write=True) as connection:
            connection.execute(
                f"DELETE FROM ha_retrieval_feedback WHERE kb_id={marker}", (kb_id,)
            )

    def set_enabled(
        self,
        retrieval_feedback_id: str,
        enabled: bool,
        *,
        actor: str | None = None,
        reason: str | None = None,
    ) -> dict[str, Any] | None:
        return self._set_enabled(
            retrieval_feedback_id,
            enabled,
            actor=actor,
            reason=reason,
            expected_epoch=None,
            authority=None,
        )

    def set_enabled_authorized(
        self,
        retrieval_feedback_id: str,
        enabled: bool,
        *,
        expected_epoch: int,
        authority: Mapping[str, Any],
        actor: str | None = None,
        reason: str | None = None,
    ) -> dict[str, Any] | None:
        return self._set_enabled(
            retrieval_feedback_id,
            enabled,
            actor=actor,
            reason=reason,
            expected_epoch=expected_epoch,
            authority=authority,
        )

    def _set_enabled(
        self,
        retrieval_feedback_id: str,
        enabled: bool,
        *,
        actor: str | None,
        reason: str | None,
        expected_epoch: int | None,
        authority: Mapping[str, Any] | None,
    ) -> dict[str, Any] | None:
        marker = self.backend.sql(sqlite="?", postgres="%s")
        lock = self.backend.sql(sqlite="", postgres=" FOR UPDATE")
        with self.backend.transaction(write=True) as connection:
            current_row = connection.execute(
                "SELECT data,feedback_group_key FROM ha_retrieval_feedback "
                f"WHERE retrieval_feedback_id={marker}{lock}",
                (retrieval_feedback_id,),
            ).fetchone()
            if current_row is None:
                return None
            current = json.loads(str(_row_value(current_row, "data", 0)))
            if authority is not None:
                assert_active_kb_epoch(
                    connection,
                    self.backend,
                    storage_id=str(current.get("kb_id") or ""),
                    expected_epoch=expected_epoch,
                )
                assert_live_auxiliary_authority(
                    self._authority_checker,
                    connection,
                    authority,
                    permission=Permission.WRITE,
                )
            group_key = str(_row_value(current_row, "feedback_group_key", 1))
            rows = connection.execute(
                "SELECT data FROM ha_retrieval_feedback "
                f"WHERE feedback_group_key={marker}{lock}",
                (group_key,),
            ).fetchall()
            updated_rows: _Rows = []
            disabled_at = _now_iso() if not enabled else None
            for row in rows:
                updated = {
                    **json.loads(str(_row_value(row, "data", 0))),
                    "enabled": enabled,
                    "disabled_at": disabled_at,
                    "disabled_by": actor if not enabled else None,
                    "disable_reason": reason if not enabled else None,
                }
                clean, encoded = _canonical_record(updated)
                self._upsert(connection, clean, encoded)
                updated_rows.append(clean)
        return _aggregate_retrieval_feedback_group(updated_rows)

    def export_records(self) -> _Rows:
        with self.backend.transaction() as connection:
            rows = connection.execute(
                "SELECT data FROM ha_retrieval_feedback "
                "ORDER BY created_at,retrieval_feedback_id"
            ).fetchall()
        return [json.loads(str(_row_value(row, "data", 0))) for row in rows]

    def import_records(self, records: _Rows) -> dict[str, int]:
        incoming = [_canonical_record(record) for record in records]
        marker = self.backend.sql(sqlite="?", postgres="%s")
        changed = 0
        with self.backend.transaction(write=True) as connection:
            for record, encoded in incoming:
                record_id = str(record.get("retrieval_feedback_id") or "")
                if not record_id:
                    raise ValueError("retrieval_feedback_id is required")
                existing = connection.execute(
                    "SELECT data FROM ha_retrieval_feedback "
                    f"WHERE retrieval_feedback_id={marker}",
                    (record_id,),
                ).fetchone()
                if (
                    existing is not None
                    and str(_row_value(existing, "data", 0)) == encoded
                ):
                    continue
                self._upsert(connection, record, encoded)
                changed += 1
        return {"imported": changed, "skipped": len(incoming) - changed}

    def _upsert(
        self,
        connection: DatabaseConnection,
        record: Mapping[str, Any],
        encoded: str,
    ) -> None:
        connection.execute(
            "INSERT INTO ha_retrieval_feedback("
            "retrieval_feedback_id,feedback_id,feedback_group_key,kb_id,query_hash,"
            "enabled,created_at,data) VALUES("
            f"{_placeholders(self.backend, 8)}) "
            "ON CONFLICT(retrieval_feedback_id) DO UPDATE SET "
            "feedback_id=excluded.feedback_id,feedback_group_key=excluded.feedback_group_key,"
            "kb_id=excluded.kb_id,query_hash=excluded.query_hash,enabled=excluded.enabled,"
            "created_at=excluded.created_at,data=excluded.data",
            (
                record["retrieval_feedback_id"],
                record["feedback_id"],
                _feedback_group_key(dict(record)),
                record["kb_id"],
                record["query_hash"],
                int(record.get("enabled") is True),
                record["created_at"],
                encoded,
            ),
        )

    def close(self) -> None:
        """The HA runtime owns the shared backend lifecycle."""


class DistributedRetrievalEvalDraftStore(RetrievalEvalDraftStore):
    """Shared review queue with cross-node dedupe and revision CAS."""

    def __init__(self, backend: DatabaseBackend) -> None:
        self.backend = backend
        self._authority_checker: Callable[..., None] | None = None
        execute_script(
            backend,
            (
                """CREATE TABLE IF NOT EXISTS ha_retrieval_eval_drafts (
                draft_id TEXT PRIMARY KEY,dedupe_key TEXT NOT NULL UNIQUE,
                snapshot_key TEXT NOT NULL UNIQUE,kb_id TEXT NOT NULL,status TEXT NOT NULL,
                dataset_partition TEXT NOT NULL,updated_at TEXT NOT NULL,data TEXT NOT NULL)""",
                "CREATE INDEX IF NOT EXISTS idx_ha_retrieval_eval_queue "
                "ON ha_retrieval_eval_drafts(kb_id,dataset_partition,status,updated_at DESC,draft_id DESC)",
            ),
        )

    @staticmethod
    def _validated(draft: RetrievalEvalDraft | Mapping[str, Any]) -> dict[str, Any]:
        record = _record(draft)
        _canonical_record(record)
        return record

    def bind_authority_checker(self, checker: Callable[..., None]) -> None:
        if not callable(checker):
            raise TypeError("retrieval draft authority checker must be callable")
        if (
            self._authority_checker is not None
            and self._authority_checker is not checker
        ):
            raise ValueError("retrieval draft authority checker is already bound")
        self._authority_checker = checker

    def save(self, draft: RetrievalEvalDraft | Mapping[str, Any]) -> dict[str, Any]:
        record = self._validated(draft)
        with self.backend.transaction(write=True) as connection:
            self._upsert(connection, record)
        return _clone(record)

    def ensure(self, draft: RetrievalEvalDraft | Mapping[str, Any]) -> dict[str, Any]:
        record = self._validated(draft)
        with self.backend.transaction(write=True) as connection:
            return self._ensure_locked(connection, record)

    def ensure_authorized(
        self,
        draft: RetrievalEvalDraft | Mapping[str, Any],
        *,
        expected_epoch: int,
        authority: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        record = self._validated(draft)
        with self.backend.transaction(write=True) as connection:
            assert_active_kb_epoch(
                connection,
                self.backend,
                storage_id=str(record["kb_id"]),
                expected_epoch=expected_epoch,
            )
            if self._authority_checker is not None:
                assert_live_auxiliary_authority(
                    self._authority_checker,
                    connection,
                    authority,
                    permission=Permission.WRITE,
                )
            return self._ensure_locked(connection, record)

    def _ensure_locked(
        self, connection: DatabaseConnection, record: Mapping[str, Any]
    ) -> dict[str, Any]:
        marker = self.backend.sql(sqlite="?", postgres="%s")
        snapshot_key = draft_snapshot_identity_key(record)
        _clean, encoded = _canonical_record(record)
        inserted = connection.execute(
            "INSERT INTO ha_retrieval_eval_drafts("
            "draft_id,dedupe_key,snapshot_key,kb_id,status,dataset_partition,updated_at,data) "
            f"VALUES({_placeholders(self.backend, 8)}) ON CONFLICT DO NOTHING",
            (
                record["draft_id"],
                record["dedupe_key"],
                snapshot_key,
                record["kb_id"],
                record["status"],
                record["dataset_partition"],
                record["updated_at"],
                encoded,
            ),
        )
        if inserted.rowcount == 1:
            return _clone(record)
        row = connection.execute(
            "SELECT data FROM ha_retrieval_eval_drafts WHERE "
            f"draft_id={marker} OR dedupe_key={marker} OR snapshot_key={marker} "
            "ORDER BY draft_id LIMIT 1",
            (record["draft_id"], record["dedupe_key"], snapshot_key),
        ).fetchone()
        if row is None:
            raise RuntimeError("deduplicated retrieval draft is unavailable")
        return _record(json.loads(str(_row_value(row, "data", 0))))

    def get(self, draft_id: str) -> dict[str, Any] | None:
        marker = self.backend.sql(sqlite="?", postgres="%s")
        with self.backend.transaction() as connection:
            row = connection.execute(
                f"SELECT data FROM ha_retrieval_eval_drafts WHERE draft_id={marker}",
                (draft_id,),
            ).fetchone()
        return (
            _record(json.loads(str(_row_value(row, "data", 0))))
            if row is not None
            else None
        )

    def list(
        self,
        *,
        kb_id: str | None = None,
        status: DraftStatus | str | None = None,
        dataset_partition: DatasetPartition | str | None = None,
        limit: int = 100,
    ) -> _Rows:
        marker = self.backend.sql(sqlite="?", postgres="%s")
        clauses: list[str] = []
        params: list[Any] = []
        for column, value in (
            ("kb_id", kb_id),
            ("status", _status_value(status)),
            ("dataset_partition", _partition_value(dataset_partition)),
        ):
            if value is not None:
                clauses.append(f"{column}={marker}")
                params.append(value)
        query = "SELECT data FROM ha_retrieval_eval_drafts"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += f" ORDER BY updated_at DESC,draft_id DESC LIMIT {marker}"
        params.append(max(0, min(int(limit), 100_000)))
        with self.backend.transaction() as connection:
            rows = connection.execute(query, tuple(params)).fetchall()
        return [_record(json.loads(str(_row_value(row, "data", 0)))) for row in rows]

    def approve(
        self,
        draft_id: str,
        *,
        reviewer: str,
        annotations: Mapping[str, Any] | None = None,
        expected_revision: int | None = None,
        now: str | None = None,
    ) -> dict[str, Any]:
        return self._review(
            draft_id,
            lambda row: approve_draft(
                apply_review_annotations(row, annotations)
                if annotations is not None
                else row,
                reviewer=reviewer,
                now=now,
            ),
            expected_revision=expected_revision,
        )

    def review(
        self,
        draft_id: str,
        *,
        decision: DraftStatus | str,
        reviewer: str,
        annotations: Mapping[str, Any] | None = None,
        reason: str = "",
        expected_revision: int | None = None,
        now: str | None = None,
    ) -> dict[str, Any]:
        resolved = DraftStatus(decision)
        if resolved is DraftStatus.APPROVED:
            return self.approve(
                draft_id,
                reviewer=reviewer,
                annotations=annotations,
                expected_revision=expected_revision,
                now=now,
            )
        if resolved is DraftStatus.REJECTED:
            if annotations:
                raise ValueError("rejected reviews cannot contain gold annotations")
            return self.reject(
                draft_id,
                reviewer=reviewer,
                reason=reason,
                expected_revision=expected_revision,
                now=now,
            )
        raise ValueError("review decision must be approved or rejected")

    def review_authorized(
        self,
        draft_id: str,
        *,
        decision: DraftStatus | str,
        reviewer: str,
        expected_epoch: int,
        authority: Mapping[str, Any],
        annotations: Mapping[str, Any] | None = None,
        reason: str = "",
        expected_revision: int | None = None,
        expected_index_generation: str | None = None,
        now: str | None = None,
    ) -> dict[str, Any]:
        resolved = DraftStatus(decision)
        if resolved is DraftStatus.APPROVED and not expected_index_generation:
            raise StaleAuxiliaryWrite(
                "shared draft approval index generation is missing"
            )

        def transition(row: dict[str, Any]) -> RetrievalEvalDraft:
            if resolved is DraftStatus.APPROVED:
                candidate = (
                    apply_review_annotations(row, annotations)
                    if annotations is not None
                    else row
                )
                return approve_draft(candidate, reviewer=reviewer, now=now)
            if resolved is DraftStatus.REJECTED:
                if annotations:
                    raise ValueError("rejected reviews cannot contain gold annotations")
                return reject_draft(
                    row,
                    reviewer=reviewer,
                    reason=reason,
                    now=now,
                )
            raise ValueError("review decision must be approved or rejected")

        return self._review(
            draft_id,
            transition,
            expected_revision=expected_revision,
            expected_epoch=expected_epoch,
            authority=authority,
            expected_index_generation=expected_index_generation,
        )

    def reject(
        self,
        draft_id: str,
        *,
        reviewer: str,
        reason: str,
        expected_revision: int | None = None,
        now: str | None = None,
    ) -> dict[str, Any]:
        return self._review(
            draft_id,
            lambda row: reject_draft(row, reviewer=reviewer, reason=reason, now=now),
            expected_revision=expected_revision,
        )

    def _review(
        self,
        draft_id: str,
        transition: Callable[[dict[str, Any]], RetrievalEvalDraft],
        *,
        expected_revision: int | None,
        expected_epoch: int | None = None,
        authority: Mapping[str, Any] | None = None,
        expected_index_generation: str | None = None,
    ) -> dict[str, Any]:
        marker = self.backend.sql(sqlite="?", postgres="%s")
        lock = self.backend.sql(sqlite="", postgres=" FOR UPDATE")
        with self.backend.transaction(write=True) as connection:
            row = connection.execute(
                f"SELECT data FROM ha_retrieval_eval_drafts WHERE draft_id={marker}{lock}",
                (draft_id,),
            ).fetchone()
            if row is None:
                raise KeyError(draft_id)
            current = _record(json.loads(str(_row_value(row, "data", 0))))
            if authority is not None:
                assert_active_kb_epoch(
                    connection,
                    self.backend,
                    storage_id=str(current.get("kb_id") or ""),
                    expected_epoch=expected_epoch,
                )
                assert_live_auxiliary_authority(
                    self._authority_checker,
                    connection,
                    authority,
                    permission=Permission.REVIEW,
                )
                if expected_index_generation is not None:
                    if not expected_index_generation:
                        raise StaleAuxiliaryWrite(
                            "shared draft index generation is missing"
                        )
                    tenant_id = str(authority.get("tenant_id") or "")
                    if not tenant_id:
                        raise StaleAuxiliaryWrite(
                            "shared draft tenant authority is missing"
                        )
                    index_lock = self.backend.sql(sqlite="", postgres=" FOR SHARE")
                    head = connection.execute(
                        "SELECT current_generation_id FROM ha_index_heads "
                        f"WHERE tenant_id={marker} AND kb_id={marker}{index_lock}",
                        (tenant_id, str(current.get("kb_id") or "")),
                    ).fetchone()
                    live_generation = (
                        None
                        if head is None
                        else _row_value(head, "current_generation_id", 0)
                    )
                    if str(live_generation or "") != expected_index_generation:
                        raise StaleAuxiliaryWrite(
                            "shared draft index generation changed"
                        )
            _check_expected_revision(current, expected_revision)
            updated = _record(transition(current))
            self._upsert(connection, updated)
            return _clone(updated)

    def clear_kb(self, kb_id: str) -> None:
        marker = self.backend.sql(sqlite="?", postgres="%s")
        with self.backend.transaction(write=True) as connection:
            connection.execute(
                f"DELETE FROM ha_retrieval_eval_drafts WHERE kb_id={marker}",
                (kb_id,),
            )

    def export_eval_cases(self, *, dataset_partition: DatasetPartition | str) -> _Rows:
        rows = self.list(
            status=DraftStatus.APPROVED,
            dataset_partition=dataset_partition,
            limit=100_000,
        )
        rows.sort(key=lambda row: row["draft_id"])
        return [export_retrieval_eval_case(row) for row in rows]

    def export_records(self) -> _Rows:
        with self.backend.transaction() as connection:
            rows = connection.execute(
                "SELECT data FROM ha_retrieval_eval_drafts ORDER BY updated_at,draft_id"
            ).fetchall()
        return [_record(json.loads(str(_row_value(row, "data", 0)))) for row in rows]

    def import_records(self, records: _Rows) -> dict[str, int]:
        incoming = [self._validated(record) for record in records]
        marker = self.backend.sql(sqlite="?", postgres="%s")
        changed = 0
        with self.backend.transaction(write=True) as connection:
            for record in incoming:
                row = connection.execute(
                    f"SELECT data FROM ha_retrieval_eval_drafts WHERE draft_id={marker}",
                    (record["draft_id"],),
                ).fetchone()
                if (
                    row is not None
                    and _record(json.loads(str(_row_value(row, "data", 0)))) == record
                ):
                    continue
                self._upsert(connection, record)
                changed += 1
        return {"imported": changed, "skipped": len(incoming) - changed}

    def _upsert(
        self, connection: DatabaseConnection, record: Mapping[str, Any]
    ) -> None:
        clean, encoded = _canonical_record(record)
        connection.execute(
            "INSERT INTO ha_retrieval_eval_drafts("
            "draft_id,dedupe_key,snapshot_key,kb_id,status,dataset_partition,updated_at,data) "
            f"VALUES({_placeholders(self.backend, 8)}) "
            "ON CONFLICT(draft_id) DO UPDATE SET dedupe_key=excluded.dedupe_key,"
            "snapshot_key=excluded.snapshot_key,kb_id=excluded.kb_id,status=excluded.status,"
            "dataset_partition=excluded.dataset_partition,updated_at=excluded.updated_at,"
            "data=excluded.data",
            (
                clean["draft_id"],
                clean["dedupe_key"],
                draft_snapshot_identity_key(clean),
                clean["kb_id"],
                clean["status"],
                clean["dataset_partition"],
                clean["updated_at"],
                encoded,
            ),
        )

    def close(self) -> None:
        """The HA runtime owns the shared backend lifecycle."""


__all__ = [
    "DistributedRetrievalEvalDraftStore",
    "DistributedRetrievalFeedbackStore",
    "DraftRevisionConflictError",
]
