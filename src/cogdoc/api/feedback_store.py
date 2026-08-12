from __future__ import annotations

import json
import os
from threading import RLock
from typing import Any, TypeAlias
from uuid import uuid4

from cogdoc.api.persistence import _connect, _execute_write_with_retry
from cogdoc.api.time_utils import now_iso
from cogdoc.config.settings import get_settings

_BAD_CASE_TYPES = {"thumbs_down", "correction"}
_QUICK_FEEDBACK_TYPES = {"thumbs_up", "thumbs_down"}
_EVIDENCE_PREVIEW_LIMIT = 6
_MISSING_MTIME = -1.0
_OPTIONAL_STRUCTURE_FIELDS = {
    "parent_chunk_id",
    "section_title",
    "section_path",
    "section_level",
    "child_index_in_parent",
}

_FeedbackRows: TypeAlias = list[dict[str, Any]]
_SqlParams: TypeAlias = list[Any]


# 清理评测草稿引用项。
def _eval_ref(item: Any) -> Any:
    if not isinstance(item, dict):
        return item
    cleaned = dict(item)
    if not cleaned.get("retrieval"):
        cleaned.pop("retrieval", None)
    # Keep legacy feedback drafts stable when clients did not submit structure,
    # while retaining real Parent–Child coordinates when they are available.
    for field in _OPTIONAL_STRUCTURE_FIELDS:
        if cleaned.get(field) in (None, ""):
            cleaned.pop(field, None)
    return cleaned


# 构建可转入质量评测集的坏样本草稿。
def _build_eval_draft(entry: dict[str, Any]) -> dict[str, Any]:
    feedback = str(entry.get("feedback") or "")
    correction = entry.get("correction")
    draft = {
        "case_type": "faithfulness",
        "layer": "feedback",
        "query": entry.get("query", ""),
        "answer": correction or entry.get("answer", ""),
        "is_faithful": False,
        "reviewer": "user_feedback",
        "trace_id": entry.get("trace_id", ""),
        "kb_id": entry.get("kb_id", ""),
        "feedback": feedback,
    }
    if entry.get("comment"):
        draft["comment"] = entry["comment"]
    if correction:
        draft["correction"] = correction
    if entry.get("citations"):
        draft["citations"] = [_eval_ref(item) for item in entry["citations"]]
    if entry.get("evidence"):
        draft["evidence"] = [
            _eval_ref(item) for item in entry["evidence"][:_EVIDENCE_PREVIEW_LIMIT]
        ]
    # 纠错草稿把 answer 替换成了人工答案，原回答的 occurrence 偏移已不再
    # 适用；其余坏样本保留安全公开 ledger，供离线完整性诊断。
    if entry.get("citation_ledger") and not correction:
        draft["citation_ledger"] = [
            _eval_ref(item) for item in entry["citation_ledger"]
        ]
        if entry.get("evidence_ledger"):
            draft["evidence_ledger"] = [
                _eval_ref(item) for item in entry["evidence_ledger"]
            ]
    return draft


# 反馈追加落逐行对象文件；点踩和纠错另写坏样本，供评测集自我进化。
class FeedbackStore:
    # 反馈追加落逐行对象文件；点踩和纠错另写坏样本，供评测集自我进化。
    def __init__(
        self,
        feedback_path: str | None = None,
        bad_cases_path: str | None = None,
    ):
        settings = get_settings()
        self._feedback_path = feedback_path or settings.feedback_log_path
        self._bad_cases_path = bad_cases_path or settings.bad_cases_path
        self._lock = RLock()
        self._cache_mtime: float | None = None
        self._cache_rows: _FeedbackRows | None = None
        for path in (self._feedback_path, self._bad_cases_path):
            directory = os.path.dirname(path)
            if directory:
                os.makedirs(directory, exist_ok=True)

    # 记录结果。
    def record(self, payload: dict[str, Any]) -> dict[str, Any]:
        existing = self._find_existing(payload)
        if existing is not None:
            return {
                "feedback_id": existing["feedback_id"],
                "is_bad_case": existing.get("feedback") in _BAD_CASE_TYPES,
                "deduplicated": True,
            }
        feedback_id = uuid4().hex
        entry = {"feedback_id": feedback_id, "created_at": now_iso(), **payload}
        is_bad_case = payload.get("feedback") in _BAD_CASE_TYPES
        if is_bad_case:
            entry["eval_draft"] = _build_eval_draft(entry)
        with self._lock:
            self._append(self._feedback_path, entry)
            if is_bad_case:
                self._append(self._bad_cases_path, entry)
        return {
            "feedback_id": feedback_id,
            "is_bad_case": is_bad_case,
            "deduplicated": False,
        }

    # 同一 KB 的同一回答只接受第一条赞踩反馈；纠错仍需继续入审核。
    def _find_existing(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        if payload.get("feedback") not in _QUICK_FEEDBACK_TYPES:
            return None
        trace_id = str(payload.get("trace_id") or "")
        if not trace_id:
            return None
        kb_id = str(payload.get("kb_id") or "")
        with self._lock:
            rows = self._read_all()
        for row in rows:
            if (
                str(row.get("trace_id") or "") == trace_id
                and str(row.get("kb_id") or "") == kb_id
                and row.get("feedback") in _QUICK_FEEDBACK_TYPES
            ):
                return row
        return None

    # 查询反馈记录。
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
    ) -> _FeedbackRows:
        with self._lock:
            rows = self._read_all()
        rows = [row for row in rows if row.get("kb_id") == kb_id]
        if trace_id is not None:
            rows = [row for row in rows if row.get("trace_id") == trace_id]
        if session_id is not None:
            rows = [row for row in rows if row.get("session_id") == session_id]
        if feedback is not None:
            rows = [row for row in rows if row.get("feedback") == feedback]
        if feedback_type is not None:
            rows = [row for row in rows if row.get("feedback_type") == feedback_type]
        if is_bad_case is not None:
            rows = [
                row
                for row in rows
                if (row.get("feedback") in _BAD_CASE_TYPES) is is_bad_case
            ]
        rows.sort(key=lambda row: str(row.get("created_at") or ""), reverse=True)
        return rows[:limit]

    # 统计反馈记录。
    def counts(self, *, kb_id: str) -> dict[str, Any]:
        with self._lock:
            rows = self._read_all()
        rows = [row for row in rows if row.get("kb_id") == kb_id]
        by_feedback: dict[str, int] = {}
        by_type: dict[str, int] = {}
        bad_cases = 0
        for row in rows:
            feedback = str(row.get("feedback") or "unknown")
            feedback_type = str(row.get("feedback_type") or "unknown")
            by_feedback[feedback] = by_feedback.get(feedback, 0) + 1
            by_type[feedback_type] = by_type.get(feedback_type, 0) + 1
            if row.get("feedback") in _BAD_CASE_TYPES:
                bad_cases += 1
        return {
            "total": len(rows),
            "bad_cases": bad_cases,
            "by_feedback": by_feedback,
            "by_type": by_type,
        }

    # 导出完整反馈记录，供统一状态库迁移使用。
    def export_records(self) -> _FeedbackRows:
        with self._lock:
            return [
                json.loads(json.dumps(row, ensure_ascii=False))
                for row in self._read_all()
            ]

    # 删除某 KB 的反馈记录和坏样本导出。
    def clear_kb(self, kb_id: str) -> None:
        with self._lock:
            self._rewrite_without_kb(self._feedback_path, kb_id)
            self._rewrite_without_kb(self._bad_cases_path, kb_id)
            self._cache_mtime = None
            self._cache_rows = None

    # 读取全部反馈。
    def _read_all(self) -> _FeedbackRows:
        mtime = (
            os.path.getmtime(self._feedback_path)
            if os.path.exists(self._feedback_path)
            else _MISSING_MTIME
        )
        if self._cache_mtime == mtime and self._cache_rows is not None:
            return self._cache_rows
        if not os.path.exists(self._feedback_path):
            self._cache_mtime = mtime
            self._cache_rows = []
            return []
        rows: _FeedbackRows = []
        with open(self._feedback_path, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    rows.append(json.loads(line))
        self._cache_mtime = mtime
        self._cache_rows = rows
        return rows

    # 追加。
    def _append(self, path: str, entry: dict[str, Any]) -> None:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        if path == self._feedback_path:
            self._cache_mtime = None
            self._cache_rows = None

    # 重写文件，移除指定 KB。
    def _rewrite_without_kb(self, path: str, kb_id: str) -> None:
        rows = []
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    row = json.loads(line)
                    if row.get("kb_id") != kb_id:
                        rows.append(row)
        tmp_path = f"{path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        os.replace(tmp_path, path)


# 反馈落盘到数据库，逐行对象文件仅作为导出副本。
class SqliteFeedbackStore(FeedbackStore):
    def __init__(
        self,
        db_path: str | None = None,
        feedback_path: str | None = None,
        bad_cases_path: str | None = None,
        export_jsonl: bool = True,
    ):
        settings = get_settings()
        self._db_path = db_path or settings.feedback_db_path
        self._feedback_path = feedback_path or settings.feedback_log_path
        self._bad_cases_path = bad_cases_path or settings.bad_cases_path
        self._export_jsonl = export_jsonl
        self._lock = RLock()
        self._conn = _connect(self._db_path)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS feedback_entries ("
            "feedback_id TEXT PRIMARY KEY, kb_id TEXT, trace_id TEXT, "
            "session_id TEXT, feedback TEXT, feedback_type TEXT, "
            "is_bad_case INTEGER, created_at TEXT, data TEXT)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_feedback_entries_kb_created "
            "ON feedback_entries(kb_id, created_at DESC)"
        )
        self._conn.commit()
        for path in (self._feedback_path, self._bad_cases_path):
            directory = os.path.dirname(path)
            if directory:
                os.makedirs(directory, exist_ok=True)
        with self._lock:
            self._bootstrap_from_jsonl_locked()

    # 记录结果。
    def record(self, payload: dict[str, Any]) -> dict[str, Any]:
        existing = self._find_existing_locked(payload)
        if existing is not None:
            return {
                "feedback_id": existing["feedback_id"],
                "is_bad_case": bool(existing["is_bad_case"]),
                "deduplicated": True,
            }
        feedback_id = uuid4().hex
        entry = {"feedback_id": feedback_id, "created_at": now_iso(), **payload}
        is_bad_case = payload.get("feedback") in _BAD_CASE_TYPES
        if is_bad_case:
            entry["eval_draft"] = _build_eval_draft(entry)
        with self._lock:
            self._insert_locked(entry, is_bad_case)
            if self._export_jsonl:
                self._append_export(self._feedback_path, entry)
                if is_bad_case:
                    self._append_export(self._bad_cases_path, entry)
        return {
            "feedback_id": feedback_id,
            "is_bad_case": is_bad_case,
            "deduplicated": False,
        }

    # 同一 KB 的同一回答只接受第一条赞踩反馈；纠错仍需继续入审核。
    def _find_existing_locked(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        if payload.get("feedback") not in _QUICK_FEEDBACK_TYPES:
            return None
        trace_id = str(payload.get("trace_id") or "")
        if not trace_id:
            return None
        kb_id = str(payload.get("kb_id") or "")
        with self._lock:
            row = self._conn.execute(
                "SELECT feedback_id, is_bad_case FROM feedback_entries "
                "WHERE trace_id=? AND kb_id=? "
                "AND feedback IN ('thumbs_up', 'thumbs_down') "
                "ORDER BY created_at ASC LIMIT 1",
                (trace_id, kb_id),
            ).fetchone()
        if row is None:
            return None
        return {"feedback_id": row[0], "is_bad_case": bool(row[1])}

    # 查询反馈记录。
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
    ) -> _FeedbackRows:
        where = ["kb_id=?"]
        params: _SqlParams = [kb_id]
        if trace_id is not None:
            where.append("trace_id=?")
            params.append(trace_id)
        if session_id is not None:
            where.append("session_id=?")
            params.append(session_id)
        if feedback is not None:
            where.append("feedback=?")
            params.append(feedback)
        if feedback_type is not None:
            where.append("feedback_type=?")
            params.append(feedback_type)
        if is_bad_case is not None:
            where.append("is_bad_case=?")
            params.append(1 if is_bad_case else 0)
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(
                "SELECT data FROM feedback_entries WHERE "
                + " AND ".join(where)
                + " ORDER BY created_at DESC LIMIT ?",
                params,
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    # 统计反馈记录。
    def counts(self, *, kb_id: str) -> dict[str, Any]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT feedback, feedback_type, is_bad_case "
                "FROM feedback_entries WHERE kb_id=?",
                (kb_id,),
            ).fetchall()
        by_feedback: dict[str, int] = {}
        by_type: dict[str, int] = {}
        bad_cases = 0
        for feedback, feedback_type, is_bad_case in rows:
            feedback_key = str(feedback or "unknown")
            type_key = str(feedback_type or "unknown")
            by_feedback[feedback_key] = by_feedback.get(feedback_key, 0) + 1
            by_type[type_key] = by_type.get(type_key, 0) + 1
            if is_bad_case:
                bad_cases += 1
        return {
            "total": len(rows),
            "bad_cases": bad_cases,
            "by_feedback": by_feedback,
            "by_type": by_type,
        }

    # 导出稳定顺序的完整反馈记录。
    def export_records(self) -> _FeedbackRows:
        with self._lock:
            rows = self._conn.execute(
                "SELECT data FROM feedback_entries "
                "ORDER BY created_at ASC, feedback_id ASC"
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    # 按 feedback_id 幂等导入，保留原始标识与时间戳。
    def import_records(self, records: _FeedbackRows) -> dict[str, int]:
        imported = 0
        skipped = 0
        with self._lock:
            self._begin_locked()
            try:
                for raw in records:
                    entry = json.loads(json.dumps(raw, ensure_ascii=False))
                    feedback_id = str(entry.get("feedback_id") or "")
                    if not feedback_id:
                        raise ValueError("feedback_id is required")
                    existing = self._conn.execute(
                        "SELECT data FROM feedback_entries WHERE feedback_id=?",
                        (feedback_id,),
                    ).fetchone()
                    if existing is not None and json.loads(existing[0]) == entry:
                        skipped += 1
                        continue
                    self._insert_locked(entry, entry.get("feedback") in _BAD_CASE_TYPES)
                    imported += 1
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return {"imported": imported, "skipped": skipped}

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # 删除某 KB 的反馈记录和导出副本。
    def clear_kb(self, kb_id: str) -> None:
        with self._lock:

            def _do():
                self._conn.execute(
                    "DELETE FROM feedback_entries WHERE kb_id=?", (kb_id,)
                )
                self._conn.commit()

            _execute_write_with_retry(_do)
            self._rewrite_export_without_kb(self._feedback_path, kb_id)
            self._rewrite_export_without_kb(self._bad_cases_path, kb_id)

    # 从旧逐行对象文件导入数据库。
    def _bootstrap_from_jsonl_locked(self) -> None:
        count = self._conn.execute("SELECT COUNT(*) FROM feedback_entries").fetchone()[
            0
        ]
        if count or not os.path.exists(self._feedback_path):
            return
        self._begin_locked()
        try:
            with open(self._feedback_path, encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    entry = json.loads(line)
                    feedback_id = str(entry.get("feedback_id") or uuid4().hex)
                    entry["feedback_id"] = feedback_id
                    is_bad_case = entry.get("feedback") in _BAD_CASE_TYPES
                    self._insert_locked(entry, is_bad_case)
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    def _begin_locked(self) -> None:
        self._conn.execute("BEGIN IMMEDIATE")

    # 写入当前事务；调用方负责提交或回滚。
    def _insert_locked(self, entry: dict[str, Any], is_bad_case: bool) -> None:
        def _do() -> None:
            self._conn.execute(
                "INSERT OR REPLACE INTO feedback_entries "
                "(feedback_id, kb_id, trace_id, session_id, feedback, feedback_type, "
                "is_bad_case, created_at, data) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    entry["feedback_id"],
                    entry.get("kb_id"),
                    entry.get("trace_id"),
                    entry.get("session_id"),
                    entry.get("feedback"),
                    entry.get("feedback_type"),
                    1 if is_bad_case else 0,
                    entry.get("created_at"),
                    json.dumps(entry, ensure_ascii=False),
                ),
            )

        _execute_write_with_retry(_do)

    # 追加导出副本。
    def _append_export(self, path: str, entry: dict[str, Any]) -> None:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    # 重写导出副本，移除指定 KB。
    def _rewrite_export_without_kb(self, path: str, kb_id: str) -> None:
        rows = []
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    row = json.loads(line)
                    if row.get("kb_id") != kb_id:
                        rows.append(row)
        tmp_path = f"{path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        os.replace(tmp_path, path)
