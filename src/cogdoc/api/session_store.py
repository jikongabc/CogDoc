import time
from dataclasses import dataclass, field
from threading import RLock
from typing import Any
from cogdoc.memory.manager import (
    MemoryPolicy,
    rank_long_term_facts,
    update_memory,
)
from cogdoc.memory.retriever import EmbeddingFunction, MemoryRetriever


# 定义内存会话记录。
@dataclass
class SessionEntry:
    memory: list[dict[str, Any]] = field(default_factory=list)
    mid_memory: dict[str, Any] = field(default_factory=dict)
    display: list[dict[str, Any]] = field(default_factory=list)
    updated_at: float = field(default_factory=time.monotonic)


# 管理内存版分层记忆。
class SessionStore:
    # 初始化内存版分层记忆。
    def __init__(
        self,
        max_sessions: int = 1024,
        ttl_seconds: int = 604800,
        memory_policy: MemoryPolicy | None = None,
        memory_embedding_fn: EmbeddingFunction | None = None,
    ):
        self.max_sessions = max_sessions
        self.ttl_seconds = ttl_seconds
        self.memory_policy = memory_policy or MemoryPolicy()
        self.memory_retriever = MemoryRetriever(
            self.memory_policy, embedding_fn=memory_embedding_fn
        )
        self._lock = RLock()
        self._entries: dict[tuple[str, str], SessionEntry] = {}
        self._long_memory: dict[str, list[dict[str, Any]]] = {}

    # 记录结果。
    def record(
        self,
        doc_id: str,
        session_id: str | None,
        memory_messages: list[dict[str, Any]],
        display_messages: list[dict[str, Any]],
    ) -> None:
        # 记忆与展示分开累积：记忆可能为空（答案被门控），展示只要有问答就留。
        if not session_id or (not memory_messages and not display_messages):
            return
        with self._lock:
            self._purge_expired_locked()
            entry = self._entries.setdefault((doc_id, session_id), SessionEntry())
            entry.memory, entry.mid_memory, facts = update_memory(
                entry.memory,
                entry.mid_memory,
                memory_messages or [],
                display_messages or [],
                self.memory_policy,
            )
            entry.display.extend(display_messages or [])
            self._merge_long_memory_locked(doc_id, facts)
            entry.updated_at = time.monotonic()
            self._evict_overflow_locked()

    # 构造分层历史上下文。
    def get_history(
        self, doc_id: str, session_id: str | None, query: str = ""
    ) -> list[dict[str, Any]]:
        if not session_id:
            return []
        with self._lock:
            self._purge_expired_locked()
            entry = self._entries.get((doc_id, session_id))
            if entry is None:
                return self.memory_retriever.retrieve(
                    query, [], {}, self._long_memory.get(doc_id, [])
                )
            entry.updated_at = time.monotonic()
            return self.memory_retriever.retrieve(
                query,
                entry.memory,
                entry.mid_memory,
                self._long_memory.get(doc_id, []),
            )

    # 返回三层记忆快照。
    def get_memory_snapshot(
        self, doc_id: str, session_id: str | None
    ) -> dict[str, Any]:
        if not session_id:
            return {"short_term": [], "mid_term": {}, "long_term": []}
        with self._lock:
            self._purge_expired_locked()
            entry = self._entries.get((doc_id, session_id))
            return {
                "short_term": list(entry.memory) if entry else [],
                "mid_term": dict(entry.mid_memory) if entry else {},
                "long_term": list(self._long_memory.get(doc_id, [])),
            }

    # 获取 display。
    def get_display(self, doc_id: str, session_id: str | None) -> list[dict[str, Any]]:
        # 前端展示：取完整对话。
        return self._read(doc_id, session_id, "display")

    # 读取。
    def _read(self, doc_id: str, session_id: str | None, field_name: str) -> list:
        if not session_id:
            return []
        with self._lock:
            self._purge_expired_locked()
            entry = self._entries.get((doc_id, session_id))
            if entry is None:
                return []
            entry.updated_at = time.monotonic()
            return list(getattr(entry, field_name))

    # 清理。
    def clear(self, doc_id: str, session_id: str | None) -> None:
        # 删除某会话的全部历史。
        if not session_id:
            return
        with self._lock:
            self._entries.pop((doc_id, session_id), None)

    # 清理 kb。
    def clear_kb(self, doc_id: str) -> None:
        # 删库时连带清掉该 KB 下所有会话，避免同名新库复用 doc_id 后捡到旧历史。
        with self._lock:
            user_prefix = f"{doc_id}~u-"
            for key in [
                k
                for k in self._entries
                if k[0] == doc_id or k[0].startswith(user_prefix)
            ]:
                self._entries.pop(key, None)
            for memory_doc_id in [
                key
                for key in self._long_memory
                if key == doc_id or key.startswith(user_prefix)
            ]:
                self._long_memory.pop(memory_doc_id, None)

    # 清除知识库长期记忆。
    def clear_long_term(self, doc_id: str) -> None:
        with self._lock:
            self._long_memory.pop(doc_id, None)

    # 合并长期记忆并限制容量。
    def _merge_long_memory_locked(
        self, doc_id: str, facts: list[dict[str, Any]]
    ) -> None:
        if not facts:
            return
        current = self._long_memory.setdefault(doc_id, [])
        by_id = {fact.get("id"): fact for fact in current}
        now = time.time()
        for fact in facts:
            by_id[fact.get("id")] = dict(fact, updated_at=now)
        current[:] = rank_long_term_facts(
            list(by_id.values()), self.memory_policy.long_term_fact_limit
        )

    # 列出 sessions。
    def list_sessions(self, doc_id: str) -> list[dict[str, Any]]:
        # 列出某库下的会话，title 取展示历史里首条用户消息，按最近活跃排序。
        with self._lock:
            self._purge_expired_locked()
            sessions = []
            for (entry_doc, session_id), entry in self._entries.items():
                if entry_doc != doc_id:
                    continue
                title = next(
                    (
                        t.get("content", "")
                        for t in entry.display
                        if t.get("role") == "user"
                    ),
                    "",
                )
                sessions.append(
                    {
                        "session_id": session_id,
                        "title": (title.strip()[:40] or "新对话"),
                        "message_count": len(entry.display),
                        "_updated_at": entry.updated_at,
                    }
                )
            sessions.sort(key=lambda s: s["_updated_at"], reverse=True)
            for s in sessions:
                s.pop("_updated_at")
            return sessions

    # 统计已记录回答数。
    def answer_count(self, doc_id: str) -> int:
        with self._lock:
            self._purge_expired_locked()
            return sum(
                1
                for (entry_doc, _session_id), entry in self._entries.items()
                if entry_doc == doc_id
                for message in entry.display
                if message.get("role") == "assistant"
            )

    # 清理 expired locked。
    def _purge_expired_locked(self) -> None:
        if self.ttl_seconds <= 0:
            return
        now = time.monotonic()
        expired = [
            key
            for key, entry in self._entries.items()
            if now - entry.updated_at > self.ttl_seconds
        ]
        for key in expired:
            self._entries.pop(key, None)

    # 淘汰溢出记录locked。
    def _evict_overflow_locked(self) -> None:
        overflow = len(self._entries) - self.max_sessions
        if overflow <= 0:
            return
        oldest_keys = sorted(
            self._entries,
            key=lambda key: self._entries[key].updated_at,
        )[:overflow]
        for key in oldest_keys:
            self._entries.pop(key, None)
