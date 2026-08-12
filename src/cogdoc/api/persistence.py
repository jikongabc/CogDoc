import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from threading import RLock
from typing import Any
from cogdoc.memory.manager import MemoryPolicy, update_memory
from cogdoc.memory.retriever import EmbeddingFunction, MemoryRetriever


# 建立连接结果。
def connect_sqlite(
    db_path: str, busy_timeout_ms: int = 5000
) -> sqlite3.Connection:
    # 单连接跨线程复用：WAL 提升并发读写、busy_timeout 等锁而非立刻报错；外层用 RLock 串行化。 isolation_level=None 走 autocommit：每条 DML 立即提交，绝不留悬挂写事务长期占住 WAL 写锁 （否则 session/job 两条连接里任一处漏 commit 都会无限期堵死另一条连接的写，busy_timeout 也救不了）。
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        "PRAGMA synchronous=NORMAL"
    )  # WAL 下安全且少一次 fsync，缩短写锁持有时间
    conn.execute(
        f"PRAGMA busy_timeout={max(0, int(busy_timeout_ms))}"
    )  # 跨连接写竞争默认最多等 5s；配合重试总上界仍远小于客户端超时
    return conn


# 保留内部旧名称，避免破坏现有持久化存储调用方。
_connect = connect_sqlite


# 执行写入withretry。
def _execute_write_with_retry(fn, attempts: int = 3) -> None:
    # busy_timeout 兜底之外再加少量退避重试。总耗时上界（5s busy_timeout × 3 + 退避）约十几秒， 远小于客户端 180s，绝不能把重试堆到接近客户端超时（那会表现为"一直转、最终失败"）。
    delay = 0.1
    for i in range(attempts):
        try:
            fn()
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() or i == attempts - 1:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 0.5)


# 管理 SQLite 版分层记忆。
class SqliteSessionStore:
    # 初始化 SQLite 版分层记忆。
    def __init__(
        self,
        db_path: str,
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
        self._conn = _connect(db_path)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS sessions ("
            "doc_id TEXT, session_id TEXT, memory TEXT, display TEXT, "
            "mid_memory TEXT NOT NULL DEFAULT '{}', "
            "updated_at REAL, PRIMARY KEY (doc_id, session_id))"
        )
        columns = {
            row[1]
            for row in self._conn.execute("PRAGMA table_info(sessions)").fetchall()
        }
        if "mid_memory" not in columns:
            self._conn.execute(
                "ALTER TABLE sessions ADD COLUMN mid_memory TEXT NOT NULL DEFAULT '{}'"
            )
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS long_memories ("
            "doc_id TEXT, memory_id TEXT, type TEXT, content TEXT, "
            "importance REAL, updated_at REAL, "
            "PRIMARY KEY (doc_id, memory_id))"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_long_memories_order "
            "ON long_memories(doc_id, importance DESC, updated_at DESC)"
        )
        self._conn.commit()

    # 记录结果。
    def record(
        self,
        doc_id: str,
        session_id: str | None,
        memory_messages: list[dict[str, Any]],
        display_messages: list[dict[str, Any]],
    ) -> None:
        # 读改写追加：记忆可能为空（答案被门控），展示只要有问答就留。
        if not session_id or (not memory_messages and not display_messages):
            return
        with self._lock:
            self._purge_expired_locked()
            row = self._conn.execute(
                "SELECT memory, display, mid_memory FROM sessions "
                "WHERE doc_id=? AND session_id=?",
                (doc_id, session_id),
            ).fetchone()
            memory = json.loads(row[0]) if row else []
            display = json.loads(row[1]) if row else []
            mid_memory = json.loads(row[2]) if row and row[2] else {}
            memory, mid_memory, facts = update_memory(
                memory,
                mid_memory,
                memory_messages or [],
                display_messages or [],
                self.memory_policy,
            )
            display.extend(display_messages or [])

            # 执行内部回调。
            def _do():
                self._conn.execute(
                    "INSERT INTO sessions "
                    "(doc_id, session_id, memory, display, mid_memory, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(doc_id, session_id) DO UPDATE SET "
                    "memory=excluded.memory, display=excluded.display, "
                    "mid_memory=excluded.mid_memory, "
                    "updated_at=excluded.updated_at",
                    (
                        doc_id,
                        session_id,
                        json.dumps(memory, ensure_ascii=False),
                        json.dumps(display, ensure_ascii=False),
                        json.dumps(mid_memory, ensure_ascii=False),
                        time.time(),
                    ),
                )
                self._upsert_long_memories_locked(doc_id, facts)
                self._evict_overflow_locked()
                self._conn.commit()

            _execute_write_with_retry(_do)

    # 构造分层历史上下文。
    def get_history(
        self, doc_id: str, session_id: str | None, query: str = ""
    ) -> list[dict[str, Any]]:
        if not session_id:
            return []
        with self._lock:
            self._purge_expired_locked()
            row = self._conn.execute(
                "SELECT memory, mid_memory FROM sessions "
                "WHERE doc_id=? AND session_id=?",
                (doc_id, session_id),
            ).fetchone()
            facts = self._read_long_memories_locked(doc_id)
            if row is None:
                return self.memory_retriever.retrieve(query, [], {}, facts)
            self._touch_session_locked(doc_id, session_id)
            return self.memory_retriever.retrieve(
                query,
                json.loads(row[0]),
                json.loads(row[1]) if row[1] else {},
                facts,
            )

    # 刷新会话活跃时间。
    def _touch_session_locked(self, doc_id: str, session_id: str) -> None:
        try:
            self._conn.execute(
                "UPDATE sessions SET updated_at=? WHERE doc_id=? AND session_id=?",
                (time.time(), doc_id, session_id),
            )
            self._conn.commit()
        except sqlite3.OperationalError:
            pass

    # 返回三层记忆快照。
    def get_memory_snapshot(
        self, doc_id: str, session_id: str | None
    ) -> dict[str, Any]:
        if not session_id:
            return {"short_term": [], "mid_term": {}, "long_term": []}
        with self._lock:
            row = self._conn.execute(
                "SELECT memory, mid_memory FROM sessions "
                "WHERE doc_id=? AND session_id=?",
                (doc_id, session_id),
            ).fetchone()
            return {
                "short_term": json.loads(row[0]) if row else [],
                "mid_term": json.loads(row[1]) if row and row[1] else {},
                "long_term": self._read_long_memories_locked(doc_id),
            }

    # 返回展示记录。
    def get_display(self, doc_id: str, session_id: str | None) -> list[dict[str, Any]]:
        # 前端展示：取完整对话。
        return self._read(doc_id, session_id, "display")

    # 读取结果。
    def _read(self, doc_id: str, session_id: str | None, column: str) -> list:
        if not session_id:
            return []
        with self._lock:
            self._purge_expired_locked()
            row = self._conn.execute(
                f"SELECT {column} FROM sessions WHERE doc_id=? AND session_id=?",
                (doc_id, session_id),
            ).fetchone()
            if row is None:
                return []
            # 读时刷新 updated_at 仅用于 TTL/LRU，非关键：锁竞争时直接跳过，绝不让读阻塞在写锁上。
            try:
                self._conn.execute(
                    "UPDATE sessions SET updated_at=? WHERE doc_id=? AND session_id=?",
                    (time.time(), doc_id, session_id),
                )
                self._conn.commit()
            except sqlite3.OperationalError:
                pass
            return json.loads(row[0])

    # 清理。
    def clear(self, doc_id: str, session_id: str | None) -> None:
        # 删除某会话的全部历史。
        if not session_id:
            return
        with self._lock:
            # 执行内部回调。
            def _do():
                self._conn.execute(
                    "DELETE FROM sessions WHERE doc_id=? AND session_id=?",
                    (doc_id, session_id),
                )
                self._conn.commit()

            _execute_write_with_retry(_do)

    # 删库时连带清掉该 KB 下所有会话，避免同名新库复用 doc_id 后捡到旧历史。
    def clear_kb(self, doc_id: str) -> None:
        with self._lock:
            escaped_doc_id = (
                doc_id.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            )
            user_pattern = f"{escaped_doc_id}~u-%"
            # 执行内部回调。
            def _do():
                self._conn.execute(
                    "DELETE FROM sessions WHERE doc_id=? OR doc_id LIKE ? ESCAPE '\\'",
                    (doc_id, user_pattern),
                )
                self._conn.execute(
                    "DELETE FROM long_memories WHERE doc_id=? OR doc_id LIKE ? ESCAPE '\\'",
                    (doc_id, user_pattern),
                )
                self._conn.commit()

            _execute_write_with_retry(_do)

    # 清除知识库长期记忆。
    def clear_long_term(self, doc_id: str) -> None:
        with self._lock:
            # 执行长期记忆清理。
            def _do():
                self._conn.execute(
                    "DELETE FROM long_memories WHERE doc_id=?", (doc_id,)
                )
                self._conn.commit()

            _execute_write_with_retry(_do)

    # 写入长期记忆并限制容量。
    def _upsert_long_memories_locked(
        self, doc_id: str, facts: list[dict[str, Any]]
    ) -> None:
        now = time.time()
        for fact in facts:
            self._conn.execute(
                "INSERT INTO long_memories "
                "(doc_id, memory_id, type, content, importance, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(doc_id, memory_id) DO UPDATE SET "
                "content=excluded.content, importance=excluded.importance, "
                "updated_at=excluded.updated_at",
                (
                    doc_id,
                    fact["id"],
                    fact["type"],
                    fact["content"],
                    fact["importance"],
                    now,
                ),
            )
        self._conn.execute(
            "DELETE FROM long_memories WHERE rowid IN ("
            "SELECT rowid FROM long_memories WHERE doc_id=? "
            "ORDER BY importance DESC, updated_at DESC LIMIT -1 OFFSET ?)",
            (doc_id, self.memory_policy.long_term_fact_limit),
        )

    # 读取知识库长期记忆。
    def _read_long_memories_locked(
        self, doc_id: str, limit: int | None = None
    ) -> list[dict[str, Any]]:
        query = (
            "SELECT memory_id, type, content, importance, updated_at "
            "FROM long_memories WHERE doc_id=? "
            "ORDER BY importance DESC, updated_at DESC"
        )
        params: tuple[Any, ...] = (doc_id,)
        if limit is not None:
            if limit <= 0:
                return []
            query += " LIMIT ?"
            params = (doc_id, limit)
        rows = self._conn.execute(query, params).fetchall()
        return [
            {
                "id": row[0],
                "type": row[1],
                "content": row[2],
                "importance": row[3],
                "updated_at": row[4],
            }
            for row in rows
        ]

    # 列出 sessions。
    def list_sessions(self, doc_id: str) -> list[dict[str, Any]]:
        # 列出某库下的会话，title 取展示历史里首条用户消息，按最近活跃排序。
        with self._lock:
            self._purge_expired_locked()
            rows = self._conn.execute(
                "SELECT session_id, display FROM sessions WHERE doc_id=? "
                "ORDER BY updated_at DESC",
                (doc_id,),
            ).fetchall()
            sessions = []
            for session_id, display_json in rows:
                display = json.loads(display_json)
                title = next(
                    (t.get("content", "") for t in display if t.get("role") == "user"),
                    "",
                )
                sessions.append(
                    {
                        "session_id": session_id,
                        "title": (title.strip()[:40] or "新对话"),
                        "message_count": len(display),
                    }
                )
            return sessions

    # 统计已记录回答数。
    def answer_count(self, doc_id: str) -> int:
        with self._lock:
            self._purge_expired_locked()
            rows = self._conn.execute(
                "SELECT display FROM sessions WHERE doc_id=?",
                (doc_id,),
            ).fetchall()
            return sum(
                1
                for (display_json,) in rows
                for message in json.loads(display_json)
                if message.get("role") == "assistant"
            )

    # 清理 expired locked。
    def _purge_expired_locked(self) -> None:
        if self.ttl_seconds <= 0:
            return
        self._conn.execute(
            "DELETE FROM sessions WHERE updated_at < ?",
            (time.time() - self.ttl_seconds,),
        )

    # 淘汰溢出记录locked。
    def _evict_overflow_locked(self) -> None:
        # 超出上限按最旧活跃淘汰，和内存版语义一致。
        count = self._conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        overflow = count - self.max_sessions
        if overflow <= 0:
            return
        self._conn.execute(
            "DELETE FROM sessions WHERE rowid IN ("
            "SELECT rowid FROM sessions ORDER BY updated_at ASC LIMIT ?)",
            (overflow,),
        )


# 非终态：进程重启时这些任务的线程已没了，必须协调为失败，避免前端永远轮询 pending。
_NON_TERMINAL_STATUS = ("pending", "running")


# 入库任务记录的落盘版：整条 record 存 JSON，status 单列出来便于孤儿协调。
class SqliteJobStore:
    # 入库任务记录的落盘版：整条 record 存 JSON，status 单列出来便于孤儿协调。
    def __init__(self, db_path: str, reconcile_on_init: bool = True):
        self._lock = RLock()
        self._conn = _connect(db_path)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS index_jobs ("
            "job_id TEXT PRIMARY KEY, status TEXT, data TEXT)"
        )
        self._conn.commit()
        if reconcile_on_init:
            self.reconcile_orphans()

    # 创建。
    def create(self, record: dict) -> None:
        with self._lock:
            # 执行内部回调。
            def _do():
                self._conn.execute(
                    "INSERT INTO index_jobs (job_id, status, data) VALUES (?, ?, ?)",
                    (
                        record["job_id"],
                        record["status"],
                        json.dumps(record, ensure_ascii=False),
                    ),
                )
                self._conn.commit()

            _execute_write_with_retry(_do)

    # 更新结果。
    def update(self, job_id: str, **fields: Any) -> None:
        # 读改写整条记录，status 列同步更新。
        with self._lock:
            record = self._get_locked(job_id)
            if record is None:
                return
            record.update(fields)

            # 执行内部回调。
            def _do():
                self._conn.execute(
                    "UPDATE index_jobs SET status=?, data=? WHERE job_id=?",
                    (record["status"], json.dumps(record, ensure_ascii=False), job_id),
                )
                self._conn.commit()

            _execute_write_with_retry(_do)

    # 返回结果。
    def get(self, job_id: str) -> dict | None:
        with self._lock:
            return self._get_locked(job_id)

    # 返回locked。
    def _get_locked(self, job_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT data FROM index_jobs WHERE job_id=?", (job_id,)
        ).fetchone()
        return json.loads(row[0]) if row else None

    # 协调孤儿任务。
    def reconcile_orphans(self) -> None:
        # 启动时把上次进程残留的 pending/running 任务标记为失败：线程不可能复活。
        with self._lock:
            rows = self._conn.execute(
                "SELECT job_id FROM index_jobs WHERE status IN (?, ?)",
                _NON_TERMINAL_STATUS,
            ).fetchall()
            for (job_id,) in rows:
                record = self._get_locked(job_id)
                committed_gen = (
                    record.get("committed_generation_id") if record else None
                )
                committed = False
                if committed_gen and record:
                    try:
                        from cogdoc.service.kb_state import (
                            GENERATION_SUPERSEDED,
                            KBState,
                        )

                        # committed_generation_id 在 switch_active 之前写入，其存在不证明已提交。 只有正向证据可判已提交：该 gen 仍是 active，或已被新代取代（superseded）。 无法证明时（KB 已删/state 丢失/lifecycle 非 active）一律保守标 failed， 避免把"写了证据但从未 switch_active"的任务误判为成功。
                        state = KBState(record["kb_id"])
                        active = state.active()
                        generation = state.get(committed_gen)
                        committed = (
                            active is not None and active.get("id") == committed_gen
                        ) or (
                            generation is not None
                            and generation.get("status") == GENERATION_SUPERSEDED
                        )
                    except Exception:
                        committed = False
                if committed:
                    self.update(
                        job_id,
                        status="succeeded",
                        error_code=None,
                        message="服务重启后按 active/superseded generation 对账确认已提交",
                        finished_at=datetime.now(timezone.utc).isoformat(),
                    )
                else:
                    self.update(
                        job_id,
                        status="failed",
                        error_code="INGEST_FAILED",
                        message="服务重启，任务中断",
                        finished_at=datetime.now(timezone.utc).isoformat(),
                    )


# IndexJobManager 默认记录存储：纯内存 dict，保持原有非持久行为，便于测试隔离。
class InMemoryJobStore:
    # IndexJobManager 默认记录存储：纯内存 dict，保持原有非持久行为，便于测试隔离。
    def __init__(self):
        self._lock = RLock()
        self._jobs: dict[str, dict] = {}

    # 创建。
    def create(self, record: dict) -> None:
        with self._lock:
            self._jobs[record["job_id"]] = dict(record)

    # 更新结果。
    def update(self, job_id: str, **fields: Any) -> None:
        with self._lock:
            if job_id in self._jobs:
                self._jobs[job_id].update(fields)

    # 返回结果。
    def get(self, job_id: str) -> dict | None:
        with self._lock:
            record = self._jobs.get(job_id)
            return dict(record) if record else None

    # 协调孤儿任务。
    def reconcile_orphans(self) -> None:
        # 内存版启动即空，无孤儿可协调。
        return
