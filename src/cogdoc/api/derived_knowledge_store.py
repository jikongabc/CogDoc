from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
import unicodedata
from contextlib import contextmanager
from threading import RLock
from typing import Any, Iterator
from uuid import uuid4

from cogdoc.api.time_utils import now_iso
from cogdoc.config.settings import get_settings


ACTIVE_STATUSES = {"pending", "approved", "stale"}
VALID_STATUSES = ACTIVE_STATUSES | {"rejected", "archived"}
REVISION_SOURCE_STATUSES = {"approved", "stale"}
SIMILARITY_CONFLICT_THRESHOLD = 0.72
AUTO_REBIND_REVIEW_NOTE = "文档更新后自动重绑"
ALLOWED_BINDING_UPDATE_FIELDS = {
    "related_document_id",
    "related_source",
    "related_source_sha256",
    "related_chunk_ids",
    "related_page_start",
    "related_page_end",
    "related_chunk_text_hash",
    "related_anchor_text",
}


# 归一化知识正文，供精确去重与后续相似检测打底。
def normalize_knowledge_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text)
    return re.sub(r"\s+", " ", normalized).strip()


# 计算归一化知识哈希。
def normalized_knowledge_hash(text: str) -> str:
    return hashlib.sha256(normalize_knowledge_text(text).encode("utf-8")).hexdigest()


# 提取相似度计算用的字词片段。
def _similarity_terms(text: str) -> set[str]:
    compact = re.sub(r"[^\w\u4e00-\u9fff]+", "", normalize_knowledge_text(text))
    if len(compact) <= 2:
        return {compact} if compact else set()
    return {compact[i : i + 2] for i in range(len(compact) - 1)}


# 计算文本片段重叠度。
def _text_similarity(left: str, right: str) -> float:
    left_terms = _similarity_terms(left)
    right_terms = _similarity_terms(right)
    if not left_terms or not right_terms:
        return 0.0
    overlap = len(left_terms & right_terms)
    jaccard = overlap / len(left_terms | right_terms)
    containment = overlap / min(len(left_terms), len(right_terms))
    return max(jaccard, containment)


# 规范化创建时间上限。
def _created_before_bound(value: str) -> str:
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return f"{value}T23:59:59.999999"
    return value


# 逐行对象格式派生知识存储：追加快照，读取时按知识标识折叠最新状态。
class DerivedKnowledgeStore:
    def __init__(self, path: str | None = None):
        self._path = path or get_settings().derived_knowledge_path
        self._lock = RLock()
        os.makedirs(os.path.dirname(self._path), exist_ok=True)

    # 创建知识；同库精确归一化哈希已存在时返回现有主记录。
    def create(self, payload: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        text = str(payload.get("text") or "").strip()
        if not text:
            raise ValueError("text must not be blank")
        kb_id = str(payload.get("kb_id") or "").strip()
        if not kb_id:
            raise ValueError("kb_id must not be blank")

        normalized_text = normalize_knowledge_text(text)
        normalized_hash = normalized_knowledge_hash(text)
        with self._lock:
            existing = self._find_duplicate(kb_id, normalized_hash)
            if existing is not None:
                return existing, True
            similar = self._find_similar(kb_id, normalized_text)
            conflict_group_id = None
            if similar:
                conflict_group_id = self._ensure_conflict_group(similar)
            now = now_iso()
            entry = {
                "knowledge_id": f"K{uuid4().hex}",
                "kb_id": kb_id,
                "text": text,
                "normalized_text": normalized_text,
                "normalized_hash": normalized_hash,
                "version": int(payload.get("version") or 1),
                "previous_version_id": payload.get("previous_version_id"),
                "conflict_group_id": conflict_group_id,
                "related_document_id": payload.get("related_document_id"),
                "related_source": payload.get("related_source"),
                "related_source_sha256": payload.get("related_source_sha256"),
                "related_chunk_ids": list(payload.get("related_chunk_ids") or []),
                "related_page_start": payload.get("related_page_start"),
                "related_page_end": payload.get("related_page_end"),
                "related_chunk_text_hash": payload.get("related_chunk_text_hash"),
                "related_anchor_text": payload.get("related_anchor_text"),
                "source_note": payload.get("source_note"),
                "certainty": payload.get("certainty") or "medium",
                "status": "pending" if similar else payload.get("status") or "pending",
                "origin": payload.get("origin") or "manual_entry",
                "created_from_trace_id": payload.get("created_from_trace_id"),
                "created_by": payload.get("created_by"),
                "created_at": now,
                "updated_at": now,
                "archived_at": None,
                "reviewed_by": None,
                "reviewed_at": None,
                "review_note": None,
            }
            self._append(entry)
            return entry, False

    # 创建修订版本，不覆盖原知识。
    def revise(
        self, knowledge_id: str, payload: dict[str, Any]
    ) -> dict[str, Any] | None:
        text = str(payload.get("text") or "").strip()
        if not text:
            raise ValueError("text must not be blank")
        with self._lock:
            current = self._latest().get(knowledge_id)
            if current is None:
                return None
            if current.get("status") not in REVISION_SOURCE_STATUSES:
                raise ValueError(
                    f"knowledge with status {current.get('status')} cannot be revised"
                )
            normalized_text = normalize_knowledge_text(text)
            normalized_hash = normalized_knowledge_hash(text)
            existing = self._find_duplicate(str(current["kb_id"]), normalized_hash)
            if existing is not None:
                raise ValueError(
                    f"duplicate active knowledge exists: {existing['knowledge_id']}"
                )
            now = now_iso()
            entry = {
                "knowledge_id": f"K{uuid4().hex}",
                "kb_id": current["kb_id"],
                "text": text,
                "normalized_text": normalized_text,
                "normalized_hash": normalized_hash,
                "version": int(current.get("version") or 1) + 1,
                "previous_version_id": current["knowledge_id"],
                "conflict_group_id": current.get("conflict_group_id"),
                "related_document_id": payload.get(
                    "related_document_id", current.get("related_document_id")
                ),
                "related_source": payload.get(
                    "related_source", current.get("related_source")
                ),
                "related_source_sha256": payload.get(
                    "related_source_sha256", current.get("related_source_sha256")
                ),
                "related_chunk_ids": list(
                    payload.get("related_chunk_ids", current.get("related_chunk_ids"))
                    or []
                ),
                "related_page_start": payload.get(
                    "related_page_start", current.get("related_page_start")
                ),
                "related_page_end": payload.get(
                    "related_page_end", current.get("related_page_end")
                ),
                "related_chunk_text_hash": payload.get(
                    "related_chunk_text_hash",
                    current.get("related_chunk_text_hash"),
                ),
                "related_anchor_text": payload.get(
                    "related_anchor_text", current.get("related_anchor_text")
                ),
                "source_note": payload.get("source_note", current.get("source_note")),
                "certainty": payload.get("certainty") or current.get("certainty"),
                "status": payload.get("status") or "pending",
                "origin": current.get("origin") or "manual_entry",
                "created_from_trace_id": payload.get("created_from_trace_id"),
                "created_by": payload.get("created_by"),
                "created_at": now,
                "updated_at": now,
                "archived_at": None,
                "reviewed_by": None,
                "reviewed_at": None,
                "review_note": payload.get("review_note"),
            }
            self._append(entry)
            if entry["status"] == "approved":
                self._archive_previous_version(
                    entry, payload.get("created_by"), entry["knowledge_id"]
                )
            return entry

    # 查询最新知识快照。
    def list(
        self,
        *,
        kb_id: str,
        status: str | None = None,
        document_id: str | None = None,
        origin: str | None = None,
        created_by: str | None = None,
        conflict_group_id: str | None = None,
        has_conflict: bool | None = None,
        created_after: str | None = None,
        created_before: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        with self._lock:
            rows = list(self._latest().values())
        rows = [row for row in rows if row.get("kb_id") == kb_id]
        if status is not None:
            rows = [row for row in rows if row.get("status") == status]
        rows = self._filter_rows(
            rows,
            document_id=document_id,
            origin=origin,
            created_by=created_by,
            created_after=created_after,
            created_before=created_before,
        )
        if conflict_group_id is not None:
            rows = [
                row for row in rows if row.get("conflict_group_id") == conflict_group_id
            ]
        if has_conflict is not None:
            rows = [
                row
                for row in rows
                if bool(row.get("conflict_group_id")) == has_conflict
            ]
        sorted_rows = sorted(
            rows, key=lambda row: str(row.get("created_at", "")), reverse=True
        )
        return sorted_rows[:limit] if limit is not None else sorted_rows

    # 按标识查询最新知识快照。
    def get(self, knowledge_id: str) -> dict[str, Any] | None:
        with self._lock:
            return self._latest().get(knowledge_id)

    # 导出可迁移的历史快照；不折叠状态，确保审核事件和修订链完整。
    def export_records(self, *, kb_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._read_history()
        if kb_id is not None:
            rows = [row for row in rows if row.get("kb_id") == kb_id]
        return rows

    # 返回存储文件修订标记，供外部索引判断是否需要刷新。
    def revision_token(self) -> str:
        with self._lock:
            if not os.path.exists(self._path):
                return "missing"
            stat = os.stat(self._path)
        return f"{stat.st_mtime_ns}:{stat.st_size}"

    # 删除某 KB 的全部派生知识历史，避免同名重建后继承旧审核状态。
    def clear_kb(self, kb_id: str) -> None:
        with self._lock:
            rows = [row for row in self._read_history() if row.get("kb_id") != kb_id]
            self._rewrite_history(rows)

    # 统计知识审核队列。
    def counts(
        self,
        *,
        kb_id: str,
        document_id: str | None = None,
        origin: str | None = None,
        created_by: str | None = None,
        has_conflict: bool | None = None,
        created_after: str | None = None,
        created_before: str | None = None,
    ) -> dict[str, dict[str, int] | int]:
        rows = self.list(
            kb_id=kb_id,
            document_id=document_id,
            origin=origin,
            created_by=created_by,
            has_conflict=has_conflict,
            created_after=created_after,
            created_before=created_before,
        )
        by_status: dict[str, int] = {}
        by_origin: dict[str, int] = {}
        for row in rows:
            status = str(row.get("status") or "unknown")
            row_origin = str(row.get("origin") or "unknown")
            by_status[status] = by_status.get(status, 0) + 1
            by_origin[row_origin] = by_origin.get(row_origin, 0) + 1
        return {
            "total": len(rows),
            "by_status": by_status,
            "by_origin": by_origin,
        }

    # 统计冲突组审核规模。
    def conflict_counts(
        self,
        *,
        kb_id: str,
        document_id: str | None = None,
        origin: str | None = None,
        created_by: str | None = None,
        has_conflict: bool | None = None,
        created_after: str | None = None,
        created_before: str | None = None,
    ) -> dict[str, int]:
        if has_conflict is False:
            return {"total": 0, "groups": 0, "pending": 0, "stale": 0}
        rows = self.list(
            kb_id=kb_id,
            document_id=document_id,
            origin=origin,
            created_by=created_by,
            has_conflict=True,
            created_after=created_after,
            created_before=created_before,
        )
        groups = {
            str(row.get("conflict_group_id"))
            for row in rows
            if row.get("conflict_group_id")
        }
        pending = sum(1 for row in rows if row.get("status") == "pending")
        stale = sum(1 for row in rows if row.get("status") == "stale")
        return {
            "total": len(rows),
            "groups": len(groups),
            "pending": pending,
            "stale": stale,
        }

    # 查询同一冲突组的其他知识。
    def conflicts_for(self, row: dict[str, Any]) -> list[dict[str, Any]]:
        group_id = row.get("conflict_group_id")
        if not group_id:
            return []
        with self._lock:
            rows = list(self._latest().values())
        conflicts = [
            item
            for item in rows
            if item.get("conflict_group_id") == group_id
            and item.get("knowledge_id") != row.get("knowledge_id")
            and item.get("status") in ACTIVE_STATUSES
        ]
        conflicts.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
        return conflicts

    # 统计曾过期知识的复核完成情况。
    def stale_review_counts(self, *, kb_id: str) -> dict[str, int]:
        with self._lock:
            history = self._read_history()
            latest = self._latest()
        stale_ids = {
            str(row.get("knowledge_id"))
            for row in history
            if row.get("kb_id") == kb_id
            and row.get("status") == "stale"
            and row.get("knowledge_id")
        }
        reviewed = sum(
            1
            for knowledge_id in stale_ids
            if latest.get(knowledge_id, {}).get("status") != "stale"
        )
        return {"total": len(stale_ids), "reviewed": reviewed}

    # 统计自动复核重绑情况。
    def auto_review_counts(
        self,
        *,
        kb_id: str,
        document_id: str | None = None,
        origin: str | None = None,
        created_by: str | None = None,
        has_conflict: bool | None = None,
        created_after: str | None = None,
        created_before: str | None = None,
    ) -> dict[str, int]:
        with self._lock:
            history = self._read_history()
        rows = [
            row
            for row in history
            if row.get("kb_id") == kb_id
            and row.get("reviewed_by") == "system"
            and row.get("review_note") == AUTO_REBIND_REVIEW_NOTE
            and row.get("status") == "approved"
        ]
        rows = self._filter_rows(
            rows,
            document_id=document_id,
            origin=origin,
            created_by=created_by,
            created_after=created_after,
            created_before=created_before,
        )
        if has_conflict is not None:
            rows = [
                row
                for row in rows
                if bool(row.get("conflict_group_id")) == has_conflict
            ]
        return {"auto_rebound": len(rows)}

    # 列出自动复核重绑事件。
    def auto_review_events(
        self,
        *,
        kb_id: str,
        document_id: str | None = None,
        origin: str | None = None,
        created_by: str | None = None,
        has_conflict: bool | None = None,
        created_after: str | None = None,
        created_before: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        with self._lock:
            history = self._read_history()
        rows = [
            row
            for row in history
            if row.get("kb_id") == kb_id
            and row.get("reviewed_by") == "system"
            and row.get("review_note") == AUTO_REBIND_REVIEW_NOTE
            and row.get("status") == "approved"
        ]
        rows = self._filter_rows(
            rows,
            document_id=document_id,
            origin=origin,
            created_by=created_by,
            created_after=created_after,
            created_before=created_before,
        )
        if has_conflict is not None:
            rows = [
                row
                for row in rows
                if bool(row.get("conflict_group_id")) == has_conflict
            ]
        rows.sort(key=lambda row: str(row.get("reviewed_at") or ""), reverse=True)
        return rows[:limit]

    # 修改审核状态，保留历史快照。
    def set_status(
        self,
        knowledge_id: str,
        status: str,
        *,
        actor: str | None = None,
        note: str | None = None,
        binding_updates: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        if status not in VALID_STATUSES:
            raise ValueError(f"invalid status: {status}")
        with self._lock:
            current = self._latest().get(knowledge_id)
            if current is None:
                return None
            updated = {**current}
            now = now_iso()
            updated["status"] = status
            updated["updated_at"] = now
            updated["reviewed_by"] = actor
            updated["reviewed_at"] = now
            updated["review_note"] = note
            if binding_updates:
                for key, value in binding_updates.items():
                    if key in ALLOWED_BINDING_UPDATE_FIELDS and value is not None:
                        updated[key] = value
            if status == "archived":
                updated["archived_at"] = now
            self._append(updated)
            if status == "approved" and current.get("previous_version_id"):
                self._archive_previous_version(current, actor, knowledge_id)
            return updated

    # 批量修改审核状态。
    def batch_set_status(
        self,
        knowledge_ids: list[str],
        status: str,
        *,
        actor: str | None = None,
        note: str | None = None,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        updated, missing = [], []
        for knowledge_id in knowledge_ids:
            row = self.set_status(knowledge_id, status, actor=actor, note=note)
            if row is None:
                missing.append(knowledge_id)
            else:
                updated.append(row)
        return updated, missing

    # 按知识通用条件过滤行。
    def _filter_rows(
        self,
        rows: list[dict[str, Any]],
        *,
        document_id: str | None = None,
        origin: str | None = None,
        created_by: str | None = None,
        created_after: str | None = None,
        created_before: str | None = None,
    ) -> list[dict[str, Any]]:
        if document_id is not None:
            rows = [
                row
                for row in rows
                if row.get("related_document_id") == document_id
                or row.get("related_source") == document_id
            ]
        if origin is not None:
            rows = [row for row in rows if row.get("origin") == origin]
        if created_by is not None:
            rows = [row for row in rows if row.get("created_by") == created_by]
        if created_after is not None:
            rows = [
                row for row in rows if str(row.get("created_at", "")) >= created_after
            ]
        if created_before is not None:
            bound = _created_before_bound(created_before)
            rows = [row for row in rows if str(row.get("created_at", "")) <= bound]
        return rows

    # 彻底删除单条知识及其历史快照。
    def delete(self, knowledge_id: str) -> dict[str, Any] | None:
        with self._lock:
            latest = self._latest().get(knowledge_id)
            if latest is None:
                return None
            rows = [
                row
                for row in self._read_history()
                if row.get("knowledge_id") != knowledge_id
            ]
            self._rewrite_history(rows)
            return latest

    # 按当前文档清单扫描已通过知识，标记绑定旧文档或缺失文档的记录为过期。
    def mark_stale_by_documents(
        self, kb_id: str, documents: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        current_hashes = {
            str(doc.get("name")): str(doc.get("sha256"))
            for doc in documents
            if doc.get("name")
        }
        stale = []
        with self._lock:
            rows = list(self._latest().values())
        for row in rows:
            source = str(row.get("related_source") or "")
            old_hash = str(row.get("related_source_sha256") or "")
            current_hash = current_hashes.get(source)
            if (
                row.get("kb_id") == kb_id
                and row.get("status") == "approved"
                and source
                and old_hash
                and current_hash != old_hash
            ):
                updated = self.set_status(
                    row["knowledge_id"],
                    "stale",
                    actor="system",
                    note="手动扫描发现绑定文档已变化或缺失",
                )
                if updated is not None:
                    stale.append(updated)
        return stale

    # 文档哈希变化后，将绑定旧哈希的派生知识标记为过期。
    def mark_stale_for_source(
        self, kb_id: str, source: str, old_source_sha256: str
    ) -> list[dict[str, Any]]:
        stale = []
        with self._lock:
            rows = list(self._latest().values())
        for row in rows:
            if (
                row.get("kb_id") == kb_id
                and row.get("related_source") == source
                and row.get("related_source_sha256") == old_source_sha256
                and row.get("status") == "approved"
            ):
                updated = self.set_status(row["knowledge_id"], "stale")
                if updated is not None:
                    stale.append(updated)
        return stale

    # 查找同库未归档的精确重复记录。
    def _find_duplicate(
        self, kb_id: str, normalized_hash: str
    ) -> dict[str, Any] | None:
        for row in self._latest().values():
            if (
                row.get("kb_id") == kb_id
                and row.get("normalized_hash") == normalized_hash
                and row.get("status") in ACTIVE_STATUSES
            ):
                return row
        return None

    # 查找同库活跃相似知识。
    def _find_similar(self, kb_id: str, normalized_text: str) -> list[dict[str, Any]]:
        rows = []
        for row in self._latest().values():
            if row.get("kb_id") != kb_id or row.get("status") not in ACTIVE_STATUSES:
                continue
            score = _text_similarity(
                normalized_text, str(row.get("normalized_text") or "")
            )
            if score >= SIMILARITY_CONFLICT_THRESHOLD:
                rows.append({**row, "similarity": round(score, 4)})
        rows.sort(key=lambda item: float(item.get("similarity") or 0.0), reverse=True)
        return rows

    # 确保相似知识属于同一个冲突组。
    def _ensure_conflict_group(self, rows: list[dict[str, Any]]) -> str:
        group_id = next(
            (
                str(row.get("conflict_group_id"))
                for row in rows
                if row.get("conflict_group_id")
            ),
            "",
        )
        if not group_id:
            group_id = f"C{uuid4().hex}"
        now = now_iso()
        for row in rows:
            if row.get("conflict_group_id") == group_id:
                continue
            updated = {
                **row,
                "conflict_group_id": group_id,
                "updated_at": now,
            }
            updated.pop("similarity", None)
            self._append(updated)
        return group_id

    # 新版本通过后归档旧版本。
    def _archive_previous_version(
        self, current: dict[str, Any], actor: str | None, replacement_id: str
    ) -> None:
        previous_id = str(current.get("previous_version_id") or "")
        if not previous_id:
            return
        previous = self._latest().get(previous_id)
        if previous is None or previous.get("status") == "archived":
            return
        now = now_iso()
        archived = {
            **previous,
            "status": "archived",
            "updated_at": now,
            "archived_at": now,
            "reviewed_by": actor,
            "reviewed_at": now,
            "review_note": f"由新版本 {replacement_id} 替代",
        }
        self._append(archived)

    # 读取最新快照。
    def _latest(self) -> dict[str, dict[str, Any]]:
        latest: dict[str, dict[str, Any]] = {}
        for row in self._read_history():
            knowledge_id = str(row.get("knowledge_id") or "")
            if knowledge_id:
                latest[knowledge_id] = row
        return latest

    # 读取全部历史快照。
    def _read_history(self) -> list[dict[str, Any]]:
        rows = []
        if not os.path.exists(self._path):
            return rows
        with open(self._path, encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                rows.append(json.loads(line))
        return rows

    # 追加。
    def _append(self, entry: dict[str, Any]) -> None:
        with open(self._path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    # 重写历史。
    def _rewrite_history(self, rows: list[dict[str, Any]]) -> None:
        tmp_path = f"{self._path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        os.replace(tmp_path, self._path)


# SQLite 追加事件版派生知识存储：公开行为与 JSONL 实现一致，完整快照保存在 record_json。
class SqliteDerivedKnowledgeStore(DerivedKnowledgeStore):
    def __init__(
        self,
        db_path: str | None = None,
        *,
        path: str | None = None,
        busy_timeout_ms: int = 5000,
    ):
        if db_path is not None and path is not None and db_path != path:
            raise ValueError("db_path and path must refer to the same database")
        if db_path is None:
            db_path = path
        if db_path is None:
            db_path = get_settings().state_db_path
        self._path = db_path
        self._lock = RLock()
        self._transaction_depth = 0
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self._conn = sqlite3.connect(
            db_path,
            check_same_thread=False,
            isolation_level=None,
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute(f"PRAGMA busy_timeout={max(0, int(busy_timeout_ms))}")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS derived_knowledge_events ("
            "event_id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "event_key TEXT NOT NULL UNIQUE, "
            "knowledge_id TEXT NOT NULL, "
            "kb_id TEXT NOT NULL, "
            "status TEXT NOT NULL, "
            "normalized_hash TEXT, "
            "conflict_group_id TEXT, "
            "related_document_id TEXT, "
            "related_source TEXT, "
            "origin TEXT, "
            "created_by TEXT, "
            "created_at TEXT NOT NULL, "
            "reviewed_at TEXT, "
            "record_json TEXT NOT NULL)"
        )
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS derived_knowledge_meta ("
            "key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        self._conn.execute(
            "INSERT OR IGNORE INTO derived_knowledge_meta (key, value) "
            "VALUES (?, ?)",
            ("revision", "0"),
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_derived_knowledge_latest "
            "ON derived_knowledge_events(knowledge_id, event_id DESC)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_derived_knowledge_kb "
            "ON derived_knowledge_events(kb_id, event_id DESC)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_derived_knowledge_status "
            "ON derived_knowledge_events(kb_id, status, event_id DESC)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_derived_knowledge_hash "
            "ON derived_knowledge_events(kb_id, normalized_hash, event_id DESC)"
        )

    # 创建及冲突组更新必须原子提交。
    def create(self, payload: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        with self._write_transaction():
            return super().create(payload)

    # 修订记录及旧版本归档必须原子提交。
    def revise(
        self, knowledge_id: str, payload: dict[str, Any]
    ) -> dict[str, Any] | None:
        with self._write_transaction():
            return super().revise(knowledge_id, payload)

    # 审核快照及可能发生的旧版本归档必须原子提交。
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

    # 整批审核共享一个事务，任一异常不会留下半批结果。
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
                knowledge_ids,
                status,
                actor=actor,
                note=note,
            )

    # 删除单条知识的全部事件。
    def delete(self, knowledge_id: str) -> dict[str, Any] | None:
        with self._write_transaction():
            latest = self.get(knowledge_id)
            if latest is None:
                return None
            cursor = self._conn.execute(
                "DELETE FROM derived_knowledge_events WHERE knowledge_id=?",
                (knowledge_id,),
            )
            if cursor.rowcount:
                self._bump_revision()
            return latest

    # 删除某 KB 的全部历史事件。
    def clear_kb(self, kb_id: str) -> None:
        with self._write_transaction():
            cursor = self._conn.execute(
                "DELETE FROM derived_knowledge_events WHERE kb_id=?",
                (kb_id,),
            )
            if cursor.rowcount:
                self._bump_revision()

    # 文档清单扫描产生的多条 stale 事件共享一个事务。
    def mark_stale_by_documents(
        self, kb_id: str, documents: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        with self._write_transaction():
            return super().mark_stale_by_documents(kb_id, documents)

    # 单文档变更产生的多条 stale 事件共享一个事务。
    def mark_stale_for_source(
        self, kb_id: str, source: str, old_source_sha256: str
    ) -> list[dict[str, Any]]:
        with self._write_transaction():
            return super().mark_stale_for_source(kb_id, source, old_source_sha256)

    # SQLite 修订号不依赖文件系统时间精度，可直接用于派生知识索引失效判断。
    def revision_token(self) -> str:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM derived_knowledge_meta WHERE key=?",
                ("revision",),
            ).fetchone()
        return f"sqlite:{row[0] if row else '0'}"

    # 导出完整事件历史，可直接作为 JSONL 到 SQLite 的迁移载荷。
    def export_records(self, *, kb_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            if kb_id is None:
                rows = self._conn.execute(
                    "SELECT record_json FROM derived_knowledge_events "
                    "ORDER BY event_id"
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT record_json FROM derived_knowledge_events "
                    "WHERE kb_id=? ORDER BY event_id",
                    (kb_id,),
                ).fetchall()
        return [json.loads(row[0]) for row in rows]

    # 幂等导入历史：相同载荷重复导入不会重复生成审核事件。
    def import_records(
        self,
        records: list[dict[str, Any]],
    ) -> dict[str, int]:
        prepared: list[tuple[dict[str, Any], str, str]] = []
        occurrences: dict[str, int] = {}
        for record in records:
            if not isinstance(record, dict):
                raise ValueError("each imported record must be an object")
            if not str(record.get("knowledge_id") or "").strip():
                raise ValueError("imported record knowledge_id must not be blank")
            if not str(record.get("kb_id") or "").strip():
                raise ValueError("imported record kb_id must not be blank")
            encoded = json.dumps(
                record,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
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
                sort_keys=True,
                separators=(",", ":"),
            )
            digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
            occurrence = occurrences.get(digest, 0)
            occurrences[digest] = occurrence + 1
            prepared.append((record, f"import:{digest}:{occurrence}", encoded))

        imported = 0
        skipped = 0
        with self._write_transaction():
            for record, event_key, encoded in prepared:
                existing = self._conn.execute(
                    "SELECT record_json FROM derived_knowledge_events "
                    "WHERE event_key=?",
                    (event_key,),
                ).fetchone()
                if existing is not None:
                    existing_encoded = json.dumps(
                        json.loads(existing[0]),
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    if existing_encoded != encoded:
                        raise ValueError(f"import event key conflict: {event_key}")
                    skipped += 1
                    continue
                self._insert_record(record, event_key=event_key)
                imported += 1
            if imported:
                self._bump_revision()
        return {"imported": imported, "skipped": skipped}

    # 显式释放长期连接，主要供短生命周期迁移工具使用。
    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # 读取全部历史事件。
    def _read_history(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT record_json FROM derived_knowledge_events ORDER BY event_id"
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    # 直接读取每个知识标识的最新事件，避免在 Python 中折叠完整历史。
    def _latest(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT record_json FROM derived_knowledge_events "
                "WHERE event_id IN ("
                "SELECT MAX(event_id) FROM derived_knowledge_events "
                "GROUP BY knowledge_id)"
            ).fetchall()
        latest: dict[str, dict[str, Any]] = {}
        for (encoded,) in rows:
            record = json.loads(encoded)
            latest[str(record["knowledge_id"])] = record
        return latest

    # 追加事件；公开写入口已持有事务。
    def _append(self, entry: dict[str, Any]) -> None:
        self._insert_record(entry, event_key=f"event:{uuid4().hex}")
        self._bump_revision()

    # 兼容父类持久化原语，整体替换事件历史。
    def _rewrite_history(self, rows: list[dict[str, Any]]) -> None:
        self._conn.execute("DELETE FROM derived_knowledge_events")
        for entry in rows:
            self._insert_record(entry, event_key=f"event:{uuid4().hex}")
        self._bump_revision()

    # 参数化写入完整 JSON 快照及常用索引列。
    def _insert_record(
        self,
        entry: dict[str, Any],
        *,
        event_key: str,
    ) -> bool:
        cursor = self._conn.execute(
            "INSERT INTO derived_knowledge_events ("
            "event_key, knowledge_id, kb_id, status, normalized_hash, "
            "conflict_group_id, related_document_id, related_source, origin, "
            "created_by, created_at, reviewed_at, record_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event_key,
                str(entry.get("knowledge_id") or ""),
                str(entry.get("kb_id") or ""),
                str(entry.get("status") or ""),
                entry.get("normalized_hash"),
                entry.get("conflict_group_id"),
                entry.get("related_document_id"),
                entry.get("related_source"),
                entry.get("origin"),
                entry.get("created_by"),
                str(entry.get("created_at") or ""),
                entry.get("reviewed_at"),
                json.dumps(entry, ensure_ascii=False, separators=(",", ":")),
            ),
        )
        return cursor.rowcount == 1

    # 在当前事务内递增持久修订号。
    def _bump_revision(self) -> None:
        self._conn.execute(
            "UPDATE derived_knowledge_meta "
            "SET value=CAST(value AS INTEGER) + 1 WHERE key=?",
            ("revision",),
        )

    # busy_timeout 之外保留短退避，处理多个 store 实例同时抢写锁。
    def _begin_immediate(self) -> None:
        delay = 0.1
        for attempt in range(3):
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                return
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() or attempt == 2:
                    raise
                time.sleep(delay)
                delay = min(delay * 2, 0.5)

    # 允许 batch/stale 等公共写入口嵌套调用 set_status 而不重复开事务。
    @contextmanager
    def _write_transaction(self) -> Iterator[None]:
        with self._lock:
            outermost = self._transaction_depth == 0
            if outermost:
                self._begin_immediate()
            self._transaction_depth += 1
            try:
                yield
            except BaseException:
                self._transaction_depth -= 1
                if outermost:
                    self._conn.rollback()
                raise
            else:
                self._transaction_depth -= 1
                if outermost:
                    self._conn.commit()
