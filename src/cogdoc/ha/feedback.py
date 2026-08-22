from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, Callable, TypeAlias
from uuid import uuid4

from cogdoc.api.feedback_analysis_store import FeedbackAnalysisStore
from cogdoc.api.feedback_store import FeedbackStore, _build_eval_draft
from cogdoc.api.tenancy import Permission
from cogdoc.ha.storage import DatabaseBackend, DatabaseConnection, execute_script


_BAD_CASE_TYPES = {"thumbs_down", "correction"}
_QUICK_FEEDBACK_TYPES = {"thumbs_up", "thumbs_down"}
_MAX_RECORD_BYTES = 2 * 1024 * 1024
_Rows: TypeAlias = list[dict[str, Any]]
HA_KB_EPOCH_FIELD = "_ha_kb_epoch"


class StaleAuxiliaryWrite(RuntimeError):
    """An auxiliary write crossed a KB lifecycle/incarnation fence."""


def assert_live_auxiliary_authority(
    checker: Callable[..., None] | None,
    connection: DatabaseConnection,
    authority: Mapping[str, Any] | None,
    *,
    permission: Permission,
) -> None:
    if checker is None or authority is None:
        raise StaleAuxiliaryWrite("shared auxiliary mutation authority is missing")
    try:
        checker(connection, authority, required_permission=permission)
    except Exception as exc:
        raise StaleAuxiliaryWrite(
            "shared auxiliary mutation authority is stale"
        ) from exc


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_record(value: Mapping[str, Any]) -> tuple[dict[str, Any], str]:
    try:
        encoded = json.dumps(
            dict(value),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("feedback record must be finite JSON") from exc
    if len(encoded.encode("utf-8")) > _MAX_RECORD_BYTES:
        raise ValueError("feedback record exceeds the durable size limit")
    decoded = json.loads(encoded)
    if not isinstance(decoded, dict):
        raise ValueError("feedback record must be an object")
    return decoded, encoded


def _row_value(row: Any, name: str, index: int) -> Any:
    if isinstance(row, Mapping):
        return row[name]
    keys = getattr(row, "keys", None)
    if callable(keys):
        return row[name]
    return row[index]


def _quick_key(record: Mapping[str, Any]) -> str | None:
    if record.get("feedback") not in _QUICK_FEEDBACK_TYPES:
        return None
    trace_id = str(record.get("trace_id") or "")
    if not trace_id:
        return None
    kb_id = str(record.get("kb_id") or "")
    return hashlib.sha256(f"{kb_id}\0{trace_id}".encode()).hexdigest()


def _placeholders(backend: DatabaseBackend, count: int) -> str:
    marker = backend.sql(sqlite="?", postgres="%s")
    return ",".join(marker for _ in range(count))


def assert_active_kb_epoch(
    connection: DatabaseConnection,
    backend: DatabaseBackend,
    *,
    storage_id: str,
    expected_epoch: int | None,
) -> None:
    if expected_epoch is None:
        return
    marker = backend.sql(sqlite="?", postgres="%s")
    lock = backend.sql(sqlite="", postgres=" FOR SHARE")
    row = connection.execute(
        "SELECT lifecycle,epoch FROM ha_api_knowledge_bases "
        f"WHERE storage_id={marker}{lock}",
        (storage_id,),
    ).fetchone()
    if (
        row is None
        or str(_row_value(row, "lifecycle", 0)) != "active"
        or int(_row_value(row, "epoch", 1)) != expected_epoch
    ):
        raise StaleAuxiliaryWrite("knowledge-base incarnation changed")


class DistributedFeedbackStore(FeedbackStore):
    """Shared feedback ledger with atomic cross-node quick-feedback deduplication."""

    def __init__(self, backend: DatabaseBackend) -> None:
        self.backend = backend
        self._authority_checker: Callable[..., None] | None = None
        execute_script(
            backend,
            (
                """CREATE TABLE IF NOT EXISTS ha_feedback_entries (
                feedback_id TEXT PRIMARY KEY,kb_id TEXT,trace_id TEXT,session_id TEXT,
                feedback TEXT,feedback_type TEXT,is_bad_case INTEGER NOT NULL,
                quick_key TEXT UNIQUE,created_at TEXT NOT NULL,data TEXT NOT NULL)""",
                "CREATE INDEX IF NOT EXISTS idx_ha_feedback_kb_created "
                "ON ha_feedback_entries(kb_id,created_at DESC,feedback_id DESC)",
                "CREATE INDEX IF NOT EXISTS idx_ha_feedback_trace "
                "ON ha_feedback_entries(kb_id,trace_id)",
            ),
        )

    def bind_authority_checker(self, checker: Callable[..., None]) -> None:
        if not callable(checker):
            raise TypeError("feedback authority checker must be callable")
        if (
            self._authority_checker is not None
            and self._authority_checker is not checker
        ):
            raise ValueError("feedback authority checker is already bound")
        self._authority_checker = checker

    def record(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._record(payload, authority=None, explicit_epoch=None)

    def record_authorized(
        self,
        payload: dict[str, Any],
        *,
        expected_epoch: int,
        authority: Mapping[str, Any],
    ) -> dict[str, Any]:
        return self._record(
            payload,
            authority=authority,
            explicit_epoch=expected_epoch,
        )

    def _record(
        self,
        payload: dict[str, Any],
        *,
        authority: Mapping[str, Any] | None,
        explicit_epoch: int | None,
    ) -> dict[str, Any]:
        raw_payload = dict(payload)
        raw_epoch = raw_payload.pop(HA_KB_EPOCH_FIELD, None)
        expected_epoch = (
            explicit_epoch
            if explicit_epoch is not None
            else (int(raw_epoch) if raw_epoch is not None else None)
        )
        clean_payload, _payload_json = _canonical_record(raw_payload)
        feedback_id = uuid4().hex
        entry = {"feedback_id": feedback_id, "created_at": _now_iso(), **clean_payload}
        is_bad_case = entry.get("feedback") in _BAD_CASE_TYPES
        if is_bad_case:
            entry["eval_draft"] = _build_eval_draft(entry)
        entry, encoded = _canonical_record(entry)
        quick_key = _quick_key(entry)
        marker = self.backend.sql(sqlite="?", postgres="%s")
        conflict = " ON CONFLICT(quick_key) DO NOTHING" if quick_key else ""
        with self.backend.transaction(write=True) as connection:
            assert_active_kb_epoch(
                connection,
                self.backend,
                storage_id=str(entry.get("kb_id") or ""),
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
                "INSERT INTO ha_feedback_entries("
                "feedback_id,kb_id,trace_id,session_id,feedback,feedback_type,"
                "is_bad_case,quick_key,created_at,data) VALUES("
                f"{_placeholders(self.backend, 10)}){conflict}",
                (
                    feedback_id,
                    entry.get("kb_id"),
                    entry.get("trace_id"),
                    entry.get("session_id"),
                    entry.get("feedback"),
                    entry.get("feedback_type"),
                    int(is_bad_case),
                    quick_key,
                    entry["created_at"],
                    encoded,
                ),
            )
            if inserted.rowcount == 1:
                return {
                    "feedback_id": feedback_id,
                    "is_bad_case": is_bad_case,
                    "deduplicated": False,
                }
            if quick_key is None:
                raise RuntimeError("feedback insert did not create a row")
            existing = connection.execute(
                "SELECT feedback_id,is_bad_case FROM ha_feedback_entries "
                f"WHERE quick_key={marker}",
                (quick_key,),
            ).fetchone()
            if existing is None:
                raise RuntimeError("deduplicated feedback row is unavailable")
            return {
                "feedback_id": str(_row_value(existing, "feedback_id", 0)),
                "is_bad_case": bool(_row_value(existing, "is_bad_case", 1)),
                "deduplicated": True,
            }

    def list(
        self,
        *,
        kb_id: str,
        trace_id: str | None = None,
        session_id: str | None = None,
        feedback: str | None = None,
        feedback_type: str | None = None,
        is_bad_case: bool | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        marker = self.backend.sql(sqlite="?", postgres="%s")
        clauses = [f"kb_id={marker}"]
        params: list[Any] = [kb_id]
        for column, value in (
            ("trace_id", trace_id),
            ("session_id", session_id),
            ("feedback", feedback),
            ("feedback_type", feedback_type),
        ):
            if value is not None:
                clauses.append(f"{column}={marker}")
                params.append(value)
        if is_bad_case is not None:
            clauses.append(f"is_bad_case={marker}")
            params.append(int(is_bad_case))
        bounded_limit = max(0, min(int(limit), 10_000))
        params.append(bounded_limit)
        with self.backend.transaction() as connection:
            rows = connection.execute(
                "SELECT data FROM ha_feedback_entries WHERE "
                + " AND ".join(clauses)
                + f" ORDER BY created_at DESC,feedback_id DESC LIMIT {marker}",
                tuple(params),
            ).fetchall()
        return [json.loads(str(_row_value(row, "data", 0))) for row in rows]

    def counts(self, *, kb_id: str) -> dict[str, Any]:
        marker = self.backend.sql(sqlite="?", postgres="%s")
        with self.backend.transaction() as connection:
            rows = connection.execute(
                "SELECT feedback,feedback_type,is_bad_case,COUNT(*) AS amount "
                f"FROM ha_feedback_entries WHERE kb_id={marker} "
                "GROUP BY feedback,feedback_type,is_bad_case",
                (kb_id,),
            ).fetchall()
        by_feedback: dict[str, int] = {}
        by_type: dict[str, int] = {}
        total = bad_cases = 0
        for row in rows:
            amount = int(_row_value(row, "amount", 3))
            feedback_value = str(_row_value(row, "feedback", 0) or "unknown")
            type_value = str(_row_value(row, "feedback_type", 1) or "unknown")
            by_feedback[feedback_value] = by_feedback.get(feedback_value, 0) + amount
            by_type[type_value] = by_type.get(type_value, 0) + amount
            total += amount
            if bool(_row_value(row, "is_bad_case", 2)):
                bad_cases += amount
        return {
            "total": total,
            "bad_cases": bad_cases,
            "by_feedback": by_feedback,
            "by_type": by_type,
        }

    def export_records(self) -> _Rows:
        with self.backend.transaction() as connection:
            rows = connection.execute(
                "SELECT data FROM ha_feedback_entries ORDER BY created_at,feedback_id"
            ).fetchall()
        return [json.loads(str(_row_value(row, "data", 0))) for row in rows]

    def import_records(self, records: _Rows) -> dict[str, int]:
        imported = skipped = 0
        marker = self.backend.sql(sqlite="?", postgres="%s")
        with self.backend.transaction(write=True) as connection:
            for raw in records:
                entry, encoded = _canonical_record(raw)
                feedback_id = str(entry.get("feedback_id") or "")
                if not feedback_id:
                    raise ValueError("feedback_id is required")
                existing = connection.execute(
                    f"SELECT data FROM ha_feedback_entries WHERE feedback_id={marker}",
                    (feedback_id,),
                ).fetchone()
                if existing is not None:
                    if str(_row_value(existing, "data", 0)) != encoded:
                        raise ValueError(
                            "feedback_id is already bound to another record"
                        )
                    skipped += 1
                    continue
                self._insert_import(connection, entry, encoded)
                imported += 1
        return {"imported": imported, "skipped": skipped}

    def _insert_import(
        self,
        connection: DatabaseConnection,
        entry: Mapping[str, Any],
        encoded: str,
    ) -> None:
        connection.execute(
            "INSERT INTO ha_feedback_entries("
            "feedback_id,kb_id,trace_id,session_id,feedback,feedback_type,"
            "is_bad_case,quick_key,created_at,data) VALUES("
            f"{_placeholders(self.backend, 10)})",
            (
                entry["feedback_id"],
                entry.get("kb_id"),
                entry.get("trace_id"),
                entry.get("session_id"),
                entry.get("feedback"),
                entry.get("feedback_type"),
                int(entry.get("feedback") in _BAD_CASE_TYPES),
                _quick_key(entry),
                str(entry.get("created_at") or ""),
                encoded,
            ),
        )

    def clear_kb(self, kb_id: str) -> None:
        marker = self.backend.sql(sqlite="?", postgres="%s")
        with self.backend.transaction(write=True) as connection:
            connection.execute(
                f"DELETE FROM ha_feedback_entries WHERE kb_id={marker}", (kb_id,)
            )

    def close(self) -> None:
        """The HA runtime owns the shared backend lifecycle."""


class DistributedFeedbackAnalysisStore(FeedbackAnalysisStore):
    """Shared append-only feedback-analysis queue."""

    def __init__(self, backend: DatabaseBackend) -> None:
        self.backend = backend
        self._authority_checker: Callable[..., None] | None = None
        execute_script(
            backend,
            (
                """CREATE TABLE IF NOT EXISTS ha_feedback_analysis (
                feedback_analysis_id TEXT PRIMARY KEY,feedback_id TEXT,kb_id TEXT,
                trace_id TEXT,recommended_action TEXT,needs_review INTEGER,
                confidence DOUBLE PRECISION NOT NULL,created_at TEXT NOT NULL,
                data TEXT NOT NULL)""",
                "CREATE INDEX IF NOT EXISTS idx_ha_feedback_analysis_kb_created "
                "ON ha_feedback_analysis(kb_id,created_at DESC,feedback_analysis_id DESC)",
                "CREATE INDEX IF NOT EXISTS idx_ha_feedback_analysis_filters "
                "ON ha_feedback_analysis(kb_id,feedback_id,trace_id,recommended_action,needs_review)",
            ),
        )

    def bind_authority_checker(self, checker: Callable[..., None]) -> None:
        if not callable(checker):
            raise TypeError("feedback analysis authority checker must be callable")
        if (
            self._authority_checker is not None
            and self._authority_checker is not checker
        ):
            raise ValueError("feedback analysis authority checker is already bound")
        self._authority_checker = checker

    def record(
        self, feedback_id: str, payload: dict[str, Any], analysis: dict[str, Any]
    ) -> dict[str, Any]:
        return self._record(
            feedback_id,
            payload,
            analysis,
            authority=None,
            explicit_epoch=None,
        )

    def record_authorized(
        self,
        feedback_id: str,
        payload: dict[str, Any],
        analysis: dict[str, Any],
        *,
        expected_epoch: int,
        authority: Mapping[str, Any],
    ) -> dict[str, Any]:
        return self._record(
            feedback_id,
            payload,
            analysis,
            authority=authority,
            explicit_epoch=expected_epoch,
        )

    def _record(
        self,
        feedback_id: str,
        payload: dict[str, Any],
        analysis: dict[str, Any],
        *,
        authority: Mapping[str, Any] | None,
        explicit_epoch: int | None,
    ) -> dict[str, Any]:
        raw_epoch = payload.get(HA_KB_EPOCH_FIELD)
        expected_epoch = (
            explicit_epoch
            if explicit_epoch is not None
            else (int(raw_epoch) if raw_epoch is not None else None)
        )
        entry, encoded = _canonical_record(
            {
                "feedback_analysis_id": uuid4().hex,
                "feedback_id": feedback_id,
                "kb_id": payload.get("kb_id"),
                "trace_id": payload.get("trace_id"),
                "query": payload.get("query"),
                "created_at": _now_iso(),
                **analysis,
            }
        )
        with self.backend.transaction(write=True) as connection:
            assert_active_kb_epoch(
                connection,
                self.backend,
                storage_id=str(entry.get("kb_id") or ""),
                expected_epoch=expected_epoch,
            )
            if authority is not None:
                assert_live_auxiliary_authority(
                    self._authority_checker,
                    connection,
                    authority,
                    permission=Permission.WRITE,
                )
            self._upsert(connection, entry, encoded)
        return entry

    def list(
        self,
        *,
        kb_id: str,
        feedback_id: str | None = None,
        trace_id: str | None = None,
        recommended_action: str | None = None,
        needs_review: bool | None = None,
        min_confidence: float | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        marker = self.backend.sql(sqlite="?", postgres="%s")
        clauses = [f"kb_id={marker}"]
        params: list[Any] = [kb_id]
        for column, value in (
            ("feedback_id", feedback_id),
            ("trace_id", trace_id),
            ("recommended_action", recommended_action),
        ):
            if value is not None:
                clauses.append(f"{column}={marker}")
                params.append(value)
        if needs_review is not None:
            clauses.append(f"needs_review={marker}")
            params.append(int(needs_review))
        if min_confidence is not None:
            clauses.append(f"confidence>={marker}")
            params.append(float(min_confidence))
        params.append(max(0, min(int(limit), 10_000)))
        with self.backend.transaction() as connection:
            rows = connection.execute(
                "SELECT data FROM ha_feedback_analysis WHERE "
                + " AND ".join(clauses)
                + f" ORDER BY created_at DESC,feedback_analysis_id DESC LIMIT {marker}",
                tuple(params),
            ).fetchall()
        return [json.loads(str(_row_value(row, "data", 0))) for row in rows]

    def counts(self, *, kb_id: str) -> dict[str, dict[str, int] | int]:
        marker = self.backend.sql(sqlite="?", postgres="%s")
        with self.backend.transaction() as connection:
            rows = connection.execute(
                "SELECT recommended_action,needs_review,data,COUNT(*) AS amount "
                f"FROM ha_feedback_analysis WHERE kb_id={marker} "
                "GROUP BY recommended_action,needs_review,data",
                (kb_id,),
            ).fetchall()
        by_action: dict[str, int] = {}
        by_type: dict[str, int] = {}
        needs_review = total = 0
        for row in rows:
            amount = int(_row_value(row, "amount", 3))
            action = str(_row_value(row, "recommended_action", 0) or "unknown")
            decoded = json.loads(str(_row_value(row, "data", 2)))
            feedback_type = str(decoded.get("feedback_type") or "unknown")
            by_action[action] = by_action.get(action, 0) + amount
            by_type[feedback_type] = by_type.get(feedback_type, 0) + amount
            total += amount
            if bool(_row_value(row, "needs_review", 1)):
                needs_review += amount
        return {
            "total": total,
            "needs_review": needs_review,
            "by_action": by_action,
            "by_type": by_type,
        }

    def clear_kb(self, kb_id: str) -> None:
        marker = self.backend.sql(sqlite="?", postgres="%s")
        with self.backend.transaction(write=True) as connection:
            connection.execute(
                f"DELETE FROM ha_feedback_analysis WHERE kb_id={marker}", (kb_id,)
            )

    def export_records(self) -> _Rows:
        with self.backend.transaction() as connection:
            rows = connection.execute(
                "SELECT data FROM ha_feedback_analysis "
                "ORDER BY created_at,feedback_analysis_id"
            ).fetchall()
        return [json.loads(str(_row_value(row, "data", 0))) for row in rows]

    def import_records(self, records: _Rows) -> dict[str, int]:
        incoming = [_canonical_record(record) for record in records]
        marker = self.backend.sql(sqlite="?", postgres="%s")
        changed = 0
        with self.backend.transaction(write=True) as connection:
            for entry, encoded in incoming:
                record_id = str(entry.get("feedback_analysis_id") or "")
                if not record_id:
                    raise ValueError("feedback_analysis_id is required")
                existing = connection.execute(
                    "SELECT data FROM ha_feedback_analysis "
                    f"WHERE feedback_analysis_id={marker}",
                    (record_id,),
                ).fetchone()
                if (
                    existing is not None
                    and str(_row_value(existing, "data", 0)) == encoded
                ):
                    continue
                self._upsert(connection, entry, encoded)
                changed += 1
        return {"imported": changed, "skipped": len(incoming) - changed}

    def _upsert(
        self,
        connection: DatabaseConnection,
        entry: Mapping[str, Any],
        encoded: str,
    ) -> None:
        connection.execute(
            "INSERT INTO ha_feedback_analysis("
            "feedback_analysis_id,feedback_id,kb_id,trace_id,recommended_action,"
            "needs_review,confidence,created_at,data) VALUES("
            f"{_placeholders(self.backend, 9)}) "
            "ON CONFLICT(feedback_analysis_id) DO UPDATE SET "
            "feedback_id=excluded.feedback_id,kb_id=excluded.kb_id,"
            "trace_id=excluded.trace_id,recommended_action=excluded.recommended_action,"
            "needs_review=excluded.needs_review,confidence=excluded.confidence,"
            "created_at=excluded.created_at,data=excluded.data",
            (
                entry["feedback_analysis_id"],
                entry.get("feedback_id"),
                entry.get("kb_id"),
                entry.get("trace_id"),
                entry.get("recommended_action"),
                None
                if entry.get("needs_review") is None
                else int(entry.get("needs_review") is True),
                float(entry.get("confidence") or 0.0),
                str(entry.get("created_at") or ""),
                encoded,
            ),
        )

    def close(self) -> None:
        """The HA runtime owns the shared backend lifecycle."""


__all__ = [
    "DistributedFeedbackAnalysisStore",
    "DistributedFeedbackStore",
    "HA_KB_EPOCH_FIELD",
    "StaleAuxiliaryWrite",
    "assert_active_kb_epoch",
]
