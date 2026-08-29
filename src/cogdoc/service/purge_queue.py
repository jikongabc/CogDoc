import json
import logging
import math
import os
import re
import time
from threading import Lock
from cogdoc.config.settings import get_settings
from cogdoc.service.durable_io import atomic_write_json, atomic_write_text
from cogdoc.observability.logger import log_event

# 删库的外部资源（Chroma 集合 / BM25 pkl）清理是跨进程持久任务：写在 KB 目录外的 purge_queue.json， 删库瞬间入队并带 not_before（grace period），sweeper / 启动时反复重试，进程中途退出也不丢，避免孤儿索引。

_SEGMENT_ID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


# 表示 PurgeQueueCorruptError 异常。
class PurgeQueueCorruptError(RuntimeError):
    pass


# 初始化实例状态。
class PurgeQueue:
    # 初始化实例状态。
    def __init__(self, path: str | None = None):
        self._path = path or os.path.join(get_settings().kb_root, "purge_queue.json")
        self._degraded_path = f"{self._path}.degraded"
        self._lock = Lock()

    # 加载。
    def _load(self) -> list:
        # 文件不存在=空队列；损坏交由 _load_or_corrupt 处理，不在此静默吞掉。
        try:
            with open(self._path, encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, list) or not all(_valid_item(i) for i in data):
                raise json.JSONDecodeError("purge queue 结构损坏", "", 0)
            return data
        except FileNotFoundError:
            return []

    # 加载 or corrupt。
    def _load_or_corrupt(self) -> tuple:
        try:
            return self._load(), False
        except json.JSONDecodeError:
            return [], True

    # 隔离损坏文件。
    def _quarantine_corrupt(self) -> None:
        # 损坏队列改名留存供人工恢复，记 error；不静默丢弃待清理的 Chroma/BM25 记录。
        try:
            os.replace(self._path, f"{self._path}.corrupt-{time.time_ns()}")
        except OSError:
            pass
        try:
            atomic_write_text(self._degraded_path, str(int(time.time())))
        except OSError:
            pass
        log_event("purge", "purge_queue_corrupt_quarantined", {}, level=logging.ERROR)

    # 保存。
    def _save(self, items: list) -> None:
        atomic_write_json(self._path, items)

    # 加载 safe。
    def _load_safe(self) -> list:
        # 损坏即隔离并拒绝继续，避免用空队列覆盖后让外部索引永久失去追踪。
        if os.path.exists(self._degraded_path):
            raise PurgeQueueCorruptError(
                f"purge queue 处于 degraded 状态，需人工恢复: {self._path}"
            )
        items, corrupt = self._load_or_corrupt()
        if corrupt:
            self._quarantine_corrupt()
            raise PurgeQueueCorruptError(
                f"purge queue 损坏已隔离，需恢复后重试: {self._path}"
            )
        return items

    # 添加。
    def add(
        self,
        kb_id: str,
        gen_id: str,
        not_before: float,
        *,
        segment_ids: list[str] | tuple[str, ...] = (),
    ) -> None:
        with self._lock:
            items = self._load_safe()
            if not all(
                isinstance(segment_id, str)
                and _SEGMENT_ID.fullmatch(segment_id) is not None
                for segment_id in segment_ids
            ):
                raise ValueError("purge queue segment ids are invalid")
            normalized_segments = sorted(set(segment_ids))
            if len(normalized_segments) > 64:
                raise ValueError("purge queue has too many segment ids")
            candidate = {
                "kb_id": kb_id,
                "gen_id": gen_id,
                "not_before": not_before,
                **({"segment_ids": normalized_segments} if normalized_segments else {}),
            }
            if not _valid_item(candidate):
                raise ValueError("purge queue item is invalid")

            existing = next(
                (
                    item
                    for item in items
                    if item["kb_id"] == kb_id and item["gen_id"] == gen_id
                ),
                None,
            )
            if existing is None:
                items.append(candidate)
                self._save(items)
                return

            existing_segments = list(existing.get("segment_ids") or [])
            merged_segments = sorted(set(existing_segments) | set(normalized_segments))
            if len(merged_segments) > 64:
                raise ValueError("purge queue has too many segment ids")
            earliest_not_before = min(existing["not_before"], not_before)
            changed = existing["not_before"] != earliest_not_before
            existing["not_before"] = earliest_not_before
            if merged_segments != existing_segments:
                existing["segment_ids"] = merged_segments
                changed = True
            if not _valid_item(existing):
                raise ValueError("purge queue item is invalid")
            if changed:
                self._save(items)

    # 筛选结果。
    def due(self, now: float | None = None) -> list:
        # 返回已过 grace period、可立即清理的条目。
        now = now if now is not None else time.time()
        with self._lock:
            return [dict(i) for i in self._load_safe() if i.get("not_before", 0) <= now]

    # 移除结果。
    def remove(self, kb_id: str, gen_id: str) -> None:
        with self._lock:
            items = self._load_safe()
            kept = [
                i for i in items if not (i["kb_id"] == kb_id and i["gen_id"] == gen_id)
            ]
            if len(kept) != len(items):
                self._save(kept)


_shared: PurgeQueue | None = None
_shared_lock = Lock()


# 完成 shared清理任务队列 处理。
def shared_purge_queue() -> PurgeQueue:
    # 进程内共享单例；双重检查锁防并发重复构造。
    global _shared
    if _shared is None:
        with _shared_lock:
            if _shared is None:
                _shared = PurgeQueue()
    return _shared


# 完成 合法性条目 处理。
def _valid_item(item) -> bool:
    not_before = item.get("not_before") if isinstance(item, dict) else None
    segment_ids = item.get("segment_ids", []) if isinstance(item, dict) else None
    return (
        isinstance(item, dict)
        and isinstance(item.get("kb_id"), str)
        and bool(item["kb_id"])
        and isinstance(item.get("gen_id"), str)
        and bool(item["gen_id"])
        and isinstance(not_before, (int, float))
        and not isinstance(not_before, bool)
        and math.isfinite(not_before)
        and not_before >= 0
        and isinstance(segment_ids, list)
        and len(segment_ids) <= 64
        and all(
            isinstance(segment_id, str)
            and _SEGMENT_ID.fullmatch(segment_id) is not None
            for segment_id in segment_ids
        )
        and len(segment_ids) == len(set(segment_ids))
    )
