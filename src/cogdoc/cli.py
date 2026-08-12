import argparse
import atexit
import json
import os
import shlex
import shutil
import signal
import sys
from uuid import uuid4

try:
    import readline
except ImportError:
    readline = None

from cogdoc.agents.conversation_memory import extract_final_answer
from cogdoc.agents.feedback_understanding import analyze_feedback
from cogdoc.api.ingest import KBExistsError, KnowledgeBaseRegistry
from cogdoc.api.persistence import SqliteSessionStore
from cogdoc.api.time_utils import now_iso
from cogdoc.command_modes import parse_forced_mode
from cogdoc.config.settings import get_settings
from cogdoc.service.retriever_factory import RetrieverFactory
from cogdoc.graph.workflow import UNKNOWN_RESPONSE
from cogdoc.observability.logger import configure_logging
from cogdoc.service.chat_service import run_chat
from cogdoc.service.ingest_service import (
    KBCleanupError,
    build_kb_index_transactional,
    cancel_all_timers,
    delete_kb_index_transactional,
    drain_purge_queue,
    mark_kb_deleted,
)
from cogdoc.service.kb_locks import kb_write_lock
from cogdoc.service.kb_state import KBState
from cogdoc.service.mutation_journal import shared_mutation_journal
from cogdoc.service.process_lock import (
    acquire_single_instance_lock,
    locking_supported,
    release_single_instance_lock,
    strict_single_process,
)
from cogdoc.state_runtime import StateRuntime
from cogdoc.tools.embedder import Embedder
from cogdoc.tools.manifest import load_index_manifest
from cogdoc.tools.reranker import BGEReranker
from cogdoc.tools.retriever.derived_knowledge import DerivedKnowledgeIndex
from cogdoc.tools.rust_core_loader import ensure_rust_core

# Tab 补全的命令与 /kb 子命令候选。
COMPLETION_COMMANDS = [
    "/kb",
    "/inbox",
    "/add",
    "/docs",
    "/ls",
    "/rm",
    "/new",
    "/chats",
    "/open",
    "/rmchat",
    "/dk",
    "/knowledge",
    "/feedback",
    "/tuning",
    "/review",
    "/local",
    "/cloud",
    "/config",
    "/qa",
    "/summary",
    "/compare",
    "/help",
    "exit",
    "quit",
]
KB_SUBCOMMANDS = ["new", "use", "rm", "list"]
DK_SUBCOMMANDS = [
    "list",
    "show",
    "add",
    "save-answer",
    "correction",
    "no-evidence",
    "approve",
    "reject",
    "archive",
    "delete",
    "revise",
    "candidates",
    "stale-scan",
    "status",
    "batch-approve",
    "batch-reject",
]
FEEDBACK_SUBCOMMANDS = ["list", "analysis"]
TUNING_SUBCOMMANDS = ["list", "enable", "disable"]
REVIEW_SUBCOMMANDS = ["summary", "metrics", "export"]
rust_core = None


# 释放运行时锁。
def _release_runtime_lock(lock_fh) -> None:
    # 仅在后台 Timer 确已排空时显式释放锁；否则留给进程退出由 OS 释放。
    if cancel_all_timers():
        release_single_instance_lock(lock_fh)


# 加载并返回 Rust 原生扩展模块。
def get_rust_core():
    global rust_core
    if rust_core is None:
        rust_core = ensure_rust_core("scan_pdf_manifest_native", "rrf_fusion_native")
    return rust_core


# 在中断信号期间安全输出提示。
def safe_print_on_interrupt(message: str) -> None:
    # 打印退出提示时临时忽略 SIGINT，避免 Ctrl+C 连按打断清理路径。
    previous_handler = signal.getsignal(signal.SIGINT)
    try:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        print(message)
    finally:
        signal.signal(signal.SIGINT, previous_handler)


# 脱敏密钥。
def _mask_key(key: str) -> str:
    # 提示当前 key 时只露头尾，避免整串明文回显到终端。
    return f"{key[:3]}***{key[-4:]}" if len(key) > 7 else "***"


# 完成 预热流程知识库 处理。
def _warm_kb(kb_id: str) -> None:
    # 切库时预热该库 bm25 分词资源；索引尚未落盘时静默跳过，留待提问时按需加载。
    try:
        engine = RetrieverFactory.get_engine(kb_id)
        if engine.bm25_retriever.exists():
            engine.bm25_retriever.warm_up()
    except Exception:
        pass


# 完成 知识库documents 处理。
def _kb_documents(kb_id: str) -> list[dict]:
    # generation state 是事务提交指针且内含 documents；manifest 是提交后派生缓存，写失败时可能滞后。
    active = KBState(kb_id).active()
    if active is not None:
        return active.get("documents", [])
    return load_index_manifest(kb_id).get("documents", [])


HELP_TEXT = """\
可用命令（全部以 / 开头）：
  知识库
    /kb                    列出全部知识库
    /kb new <名称>         新建知识库并切入
    /kb use <名称>         切换当前知识库
    /kb rm  <名称>         删除知识库（需确认）
  文档（针对当前知识库）
    /inbox                 列出 your_documents 收件箱里的 PDF
    /add <文件名.pdf>      把收件箱里的 PDF 加入当前库并重建索引
    /add                   把收件箱里所有尚未入库的 PDF 一次性加入
    /docs /ls              列出当前库内文档
    /rm  <文件名.pdf>      从当前库移除文档并重建索引
  对话（针对当前知识库，历史持久化）
    /new                   开启一个新对话
    /chats                 列出当前库的历史对话
    /open <对话ID>         打开/恢复历史对话（支持 ID 前缀）
    /rmchat <对话ID>       删除一个对话（需确认）
  派生知识（针对当前知识库）
    /dk                    列出待审核/过期派生知识（/knowledge 同义）
    /dk list [状态]        列出派生知识，支持 --doc/--origin/--created-by/--conflict/--from/--to
    /dk show <ID>          查看派生知识详情
    /dk add <文本>         新增待审核派生知识，支持 --origin/--source/--chunk-ids/--page-start/--page-end/--chunk-hash/--anchor/--note/--certainty
    /dk save-answer [文本] 将当前对话最后一条答案保存为派生知识（origin=saved_answer）
    /dk correction <文本>  记录纠错反馈并创建派生知识（origin=correction）
    /dk no-evidence <文本> 记录无依据补充并创建派生知识（origin=no_evidence，不进调权）
    /dk approve <ID>       通过派生知识并刷新派生知识索引，支持重绑字段与 --note
    /dk reject <ID>        驳回派生知识，支持 --note
    /dk archive <ID>       归档派生知识并刷新派生知识索引，支持 --note
    /dk delete <ID>        删除派生知识（需确认）
    /dk revise <ID> <文本> 基于已通过/过期知识创建修订草稿，支持重绑字段
    /dk candidates <ID>   查看过期知识新版分块候选
    /dk stale-scan         扫描并标记过期派生知识
    /dk status             查看派生知识索引状态
    /dk batch-approve <ID...> / batch-reject <ID...>
  反馈与调权
    /feedback list         查看用户反馈记录，支持 thumbs_up/thumbs_down/correction
    /feedback analysis     查看反馈分析记录
    /tuning list           查看检索调权，支持 enabled/disabled/all
    /tuning enable <ID>    启用调权
    /tuning disable <ID>   禁用调权
  审核看板
    /review summary        审核队列摘要
    /review metrics        反馈闭环指标
    /review export         导出审核队列 JSON
  模式与强制意图
    /local  /cloud         切换本地 Ollama / 云端 API（云端缺 key 会提示配置）
    /config                配置云端 Base URL / 模型 / API Key（写入 .env）
    /qa <问题>             强制问答
    /summary <文件名>      强制总结指定文档
    /compare <A> <B> ...   强制对比多篇文档（≥2，本地模式限 2）
  其他
    /help                  显示本帮助
    exit / quit            退出
直接输入文本 = 在当前对话里向当前知识库提问。\
"""


# 对话历史落 SqliteSessionStore，重启不丢。
class Console:
    # 对话历史落 SqliteSessionStore，重启不丢。
    def __init__(self, state_runtime: StateRuntime | None = None):
        settings = get_settings()
        self._owns_state_runtime = state_runtime is None
        self.state_runtime = state_runtime or StateRuntime.from_settings(settings)
        self.registry = KnowledgeBaseRegistry()
        self.sessions = SqliteSessionStore(
            settings.state_db_path, memory_policy=settings.memory_policy
        )
        self.knowledge_store = self.state_runtime.knowledge_store
        self.knowledge_index = DerivedKnowledgeIndex(self.knowledge_store)
        self.feedback_store = self.state_runtime.feedback_store
        self.feedback_analysis_store = self.state_runtime.feedback_analysis_store
        self.retrieval_feedback_store = self.state_runtime.retrieval_feedback_store
        self.retrieval_eval_draft_store = self.state_runtime.retrieval_eval_draft_store
        # 用绝对路径，提示与列表里一眼看清 PDF 该放哪。
        self.inbox_dir = os.path.abspath(settings.cogdoc_doc_dir)
        self.active_kb: str | None = None
        self.active_session_id: str | None = None
        self.is_local = True
        self._completion_matches: list[str] = []
        os.makedirs(self.inbox_dir, exist_ok=True)

    def close(self) -> None:
        if self._owns_state_runtime:
            self.state_runtime.close()

    # 工具。

    # 确认结果。
    def _confirm(self, prompt: str) -> bool:
        return input(f"{prompt} [y/N] ").strip().lower() in ("y", "yes")

    # 校验知识库。
    def _require_kb(self) -> bool:
        if self.active_kb is None:
            print(
                "⚠️ 还没有选择知识库。用 /kb 查看、/kb new <名> 创建、/kb use <名> 切换。"
            )
            return False
        return True

    # 完成 收件箱PDF 列表 处理。
    def _inbox_pdfs(self) -> list[str]:
        os.makedirs(self.inbox_dir, exist_ok=True)
        return sorted(
            f
            for f in os.listdir(self.inbox_dir)
            if f.lower().endswith(".pdf")
            and os.path.isfile(os.path.join(self.inbox_dir, f))
        )

    # 解析会话。
    def _resolve_session(self, prefix: str) -> str | None:
        # 对话 ID 是 32 位 hex，太长，按前缀唯一匹配。
        if not prefix:
            print("用法: 需要提供对话 ID（可用前缀）。")
            return None
        matches = [
            s["session_id"]
            for s in self.sessions.list_sessions(self.active_kb)
            if s["session_id"].startswith(prefix)
        ]
        if not matches:
            print(f"⚠️ 找不到匹配的对话: {prefix}")
            return None
        if len(matches) > 1:
            print(f"⚠️ ID 前缀不唯一，匹配到 {len(matches)} 个，请补全后重试。")
            return None
        return matches[0]

    # 完成 rebuild 处理。
    def _rebuild(self) -> None:
        # 重建后索引已变，需重新预热新 bm25。
        kb = self.active_kb
        try:
            result = build_kb_index_transactional(
                kb,
                self.registry.source_dir(kb),
                knowledge_store=self.knowledge_store,
            )
        except Exception as e:
            print(f"❌ 索引重建失败: {e}")
            return
        if result.document_count == 0:
            print("⚠️ 当前知识库已无 PDF，索引已清空。")
        else:
            for d in result.documents:
                print(f"  -> {d.name}: {d.chunk_count} 个 Chunk")
            print(f"✅ 重建完成，共 {result.chunk_count} 个知识片段。")
        _warm_kb(kb)

    # 知识库命令。

    # 切换到知识库。
    def _use_kb(self, name: str) -> None:
        self.active_kb = name
        self.active_session_id = None
        _warm_kb(name)
        print(f"📚 已切换到知识库: {name}（/new 开始新对话，/chats 查看历史）")

    # 删除 kb。
    def _delete_kb(self, kb_id: str) -> None:
        # 写锁内先事务清理索引并落 tombstone，再撤 registry，避免半删除态。
        with kb_write_lock(kb_id):
            delete_kb_index_transactional(kb_id)
            mark_kb_deleted(kb_id)
            try:
                for store in (
                    self.knowledge_store,
                    self.feedback_store,
                    self.feedback_analysis_store,
                    self.retrieval_feedback_store,
                    getattr(self, "retrieval_eval_draft_store", None),
                ):
                    clear_kb = getattr(store, "clear_kb", None)
                    if callable(clear_kb):
                        clear_kb(kb_id)
            except Exception as exc:
                raise KBCleanupError(f"KB 派生/反馈状态删除失败: {kb_id}") from exc
            # 连带清掉该库的会话历史，否则同名新库复用 doc_id 会捡到旧对话。
            try:
                self.sessions.clear_kb(kb_id)
            except Exception as exc:
                raise KBCleanupError(f"KB 会话状态删除失败: {kb_id}") from exc
            self.registry.delete(kb_id)

    # 完成 cmd知识库 处理。
    def cmd_kb(self, sub: str, name: str) -> None:
        if sub in ("", "list"):
            # The local console is the backward-compatible default workspace;
            # tenant-scoped API workspaces must never appear as ambiguous
            # duplicate slugs in its selector.
            records = self.registry.list(tenant_id="default")
            if not records:
                print("（暂无知识库。用 /kb new <名称> 创建一个。）")
                return
            print("📚 知识库列表:")
            for r in records:
                kb = r["kb_id"]
                marker = "→" if kb == self.active_kb else " "
                print(f" {marker} {kb}  ({len(_kb_documents(kb))} 个文档)")
            return
        if sub == "new":
            if not name:
                print("用法: /kb new <名称>")
                return
            try:
                self.registry.create(name)
            except KBExistsError:
                print(f"⚠️ 知识库已存在: {name}")
                return
            print(f"✅ 已创建知识库: {name}")
            self._use_kb(name)
            return
        if sub == "use":
            if not name:
                print("用法: /kb use <名称>")
                return
            if not self.registry.exists(name):
                print(f"⚠️ 知识库不存在: {name}")
                return
            self._use_kb(name)
            return
        if sub == "rm":
            if not name:
                print("用法: /kb rm <名称>")
                return
            if not self.registry.exists(name):
                print(f"⚠️ 知识库不存在: {name}")
                return
            if not self._confirm(
                f"确认删除知识库 【{name}】 及其全部文档与索引？此操作不可恢复"
            ):
                print("已取消。")
                return
            try:
                self._delete_kb(name)
            except KBCleanupError:
                print(f"❌ 知识库清理未完成，请重试: {name}")
                return
            print(f"🗑️ 已删除知识库: {name}")
            if self.active_kb == name:
                self.active_kb = None
                self.active_session_id = None
            return
        print(f"❓ 未知 /kb 子命令: {sub}。可用: new / use / rm（或不带参数列出）。")

    # 文档命令。

    # 完成 cmd收件箱 处理。
    def cmd_inbox(self) -> None:
        pdfs = self._inbox_pdfs()
        if not pdfs:
            print(f"（收件箱 {self.inbox_dir} 里没有 PDF。把 PDF 放进去再 /add。）")
            return
        in_kb = (
            {d.get("name") for d in _kb_documents(self.active_kb)}
            if self.active_kb
            else set()
        )
        print(f"📥 收件箱 {self.inbox_dir}:")
        for f in pdfs:
            tag = " （已在当前库）" if f in in_kb else ""
            print(f"   • {f}{tag}")

    # 完成 cmdadd 处理。
    def cmd_add(self, arg: str) -> None:
        if not self._require_kb():
            return
        pdfs = self._inbox_pdfs()
        if not pdfs:
            print(f"（收件箱 {self.inbox_dir} 里没有 PDF。）")
            return
        if arg:
            name = os.path.basename(arg)
            if name not in pdfs:
                print(f"⚠️ 收件箱里找不到该 PDF: {name}（用 /inbox 查看）")
                return
            targets = [name]
        else:
            existing = {d.get("name") for d in _kb_documents(self.active_kb)}
            targets = [f for f in pdfs if f not in existing]
            if not targets:
                print("收件箱里没有需要新增的 PDF。")
                return
        dst_dir = self.registry.source_dir(self.active_kb)
        os.makedirs(dst_dir, exist_ok=True)
        for f in targets:
            shutil.copy2(os.path.join(self.inbox_dir, f), os.path.join(dst_dir, f))
        print(f"📎 已复制 {len(targets)} 个 PDF 进知识库源目录，开始同步重建索引...")
        self._rebuild()

    # 完成 cmd文档列表 处理。
    def cmd_docs(self) -> None:
        if not self._require_kb():
            return
        docs = _kb_documents(self.active_kb)
        if not docs:
            print("（当前知识库还没有文档。用 /add 加入。）")
            return
        print(f"📄 知识库 【{self.active_kb}】 文档:")
        for d in docs:
            print(f"   • {d.get('name')}")

    # 完成 cmdrm 处理。
    def cmd_rm(self, arg: str) -> None:
        if not self._require_kb():
            return
        if not arg:
            print("用法: /rm <文件名.pdf>")
            return
        name = os.path.basename(arg)
        path = os.path.join(self.registry.source_dir(self.active_kb), name)
        if not os.path.exists(path):
            print(f"⚠️ 当前库里找不到该文档: {name}")
            return
        os.remove(path)
        print(f"🗑️ 已移除文档 {name}，开始同步重建索引...")
        self._rebuild()

    # 对话命令。

    # 完成 cmdnew 处理。
    def cmd_new(self) -> None:
        if not self._require_kb():
            return
        self.active_session_id = uuid4().hex
        print(f"🆕 已开启新对话（{self.active_session_id[:8]}）。")

    # 完成 cmdchats 处理。
    def cmd_chats(self) -> None:
        if not self._require_kb():
            return
        sessions = self.sessions.list_sessions(self.active_kb)
        if not sessions:
            print("（当前知识库还没有历史对话。用 /new 开始。）")
            return
        print(f"💬 知识库 【{self.active_kb}】 历史对话:")
        for s in sessions:
            marker = "→" if s["session_id"] == self.active_session_id else " "
            print(
                f" {marker} {s['session_id'][:8]}  {s['title']}  （{s['message_count']} 条）"
            )
        print("（用 /open <ID前缀> 打开，/rmchat <ID前缀> 删除）")

    # 完成 cmdopen 处理。
    def cmd_open(self, arg: str) -> None:
        if not self._require_kb():
            return
        sid = self._resolve_session(arg)
        if sid is None:
            return
        self.active_session_id = sid
        messages = self.sessions.get_display(self.active_kb, sid)
        print(f"📖 已打开对话 {sid[:8]}（{len(messages)} 条消息）:")
        for m in messages:
            role = "你" if m.get("role") == "user" else "AI"
            print(f"  [{role}] {m.get('content', '')}")
        print("-" * 50)

    # 完成 cmdrmchat 处理。
    def cmd_rmchat(self, arg: str) -> None:
        if not self._require_kb():
            return
        sid = self._resolve_session(arg)
        if sid is None:
            return
        if not self._confirm(f"确认删除对话 {sid[:8]}？"):
            print("已取消。")
            return
        self.sessions.clear(self.active_kb, sid)
        if self.active_session_id == sid:
            self.active_session_id = None
        print(f"🗑️ 已删除对话 {sid[:8]}。")

    # 派生知识命令。

    # 刷新派生知识索引。
    def _refresh_derived_knowledge_index(self, kb_id: str) -> None:
        try:
            self.knowledge_index.rebuild(kb_id)
            print("🔄 派生知识索引已刷新。")
        except Exception as exc:
            print(f"⚠️ 派生知识索引刷新失败，后续检索仍会尝试自动刷新: {exc}")

    # 格式化派生知识摘要。
    def _knowledge_title(self, item: dict) -> str:
        text = str(item.get("text") or "").replace("\n", " ")
        return text[:80] + ("..." if len(text) > 80 else "")

    # 解析派生知识命令参数。
    def _parse_dk_args(self, prog: str, tokens: list[str], configure):
        parser = argparse.ArgumentParser(prog=prog, add_help=True)
        configure(parser)
        try:
            return parser.parse_args(tokens)
        except SystemExit:
            return None

    # 拆分逗号列表。
    def _split_csv(self, value: str | None) -> list[str]:
        if not value:
            return []
        return [part.strip() for part in value.split(",") if part.strip()]

    # 解析可选整数。
    def _optional_int(self, value: str | None) -> int | None:
        if value in (None, ""):
            return None
        try:
            return int(value)
        except ValueError:
            raise ValueError(f"必须是整数: {value}") from None

    # 当前库文档哈希。
    def _document_sha(self, source: str | None) -> str | None:
        if not source or not self.active_kb:
            return None
        for doc in _kb_documents(self.active_kb):
            if doc.get("name") == source:
                return str(doc.get("sha256") or "") or None
        return None

    # 派生知识绑定字段参数。
    def _add_binding_args(self, parser) -> None:
        parser.add_argument("--doc-id")
        parser.add_argument("--source")
        parser.add_argument("--source-sha")
        parser.add_argument("--chunk-ids")
        parser.add_argument("--page-start")
        parser.add_argument("--page-end")
        parser.add_argument("--chunk-hash")
        parser.add_argument("--anchor")

    # 从参数构造绑定字段。
    def _binding_payload(self, opts) -> dict:
        source = getattr(opts, "source", None)
        return {
            "related_document_id": getattr(opts, "doc_id", None),
            "related_source": source,
            "related_source_sha256": getattr(opts, "source_sha", None)
            or self._document_sha(source),
            "related_chunk_ids": self._split_csv(getattr(opts, "chunk_ids", None)),
            "related_page_start": self._optional_int(getattr(opts, "page_start", None)),
            "related_page_end": self._optional_int(getattr(opts, "page_end", None)),
            "related_chunk_text_hash": getattr(opts, "chunk_hash", None),
            "related_anchor_text": getattr(opts, "anchor", None),
        }

    # 基于绑定字段构造反馈引用目标。
    def _feedback_refs(self, binding: dict) -> list[dict]:
        source = binding.get("related_source")
        chunk_ids = binding.get("related_chunk_ids") or []
        return [
            {
                "chunk_id": chunk_id,
                "source": source,
                "source_type": "document",
            }
            for chunk_id in chunk_ids
        ]

    # 输出 JSON。
    def _print_json(self, payload: dict) -> None:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))

    # 当前对话最后一条助手消息。
    def _last_assistant_message(self) -> dict | None:
        if not self.active_kb or not self.active_session_id:
            return None
        messages = self.sessions.get_display(self.active_kb, self.active_session_id)
        for message in reversed(messages):
            if message.get("role") == "assistant":
                return message
        return None

    # 创建派生知识并提示。
    def _create_derived_knowledge(self, payload: dict) -> dict | None:
        try:
            row, deduplicated = self.knowledge_store.create(payload)
        except ValueError as exc:
            print(f"❌ 创建派生知识失败: {exc}")
            return None
        except Exception as exc:
            print(f"❌ 创建派生知识异常: {exc}")
            return None
        marker = "（已存在，未重复新增）" if deduplicated else ""
        print(f"✅ 已保存为待审核派生知识: {row['knowledge_id']} {marker}")
        return row

    # 记录反馈，并按需要创建分析、调权和派生知识。
    def _record_feedback_flow(
        self,
        *,
        feedback_type: str,
        correction: str,
        comment: str | None,
        binding: dict,
        skip_retrieval_feedback: bool,
        certainty: str,
        query: str | None = None,
        answer: str | None = None,
        trace_id: str | None = None,
    ) -> None:
        trace = trace_id or f"cli-{uuid4().hex}"
        refs = self._feedback_refs(binding)
        payload = {
            "trace_id": trace,
            "feedback": "correction",
            "kb_id": self.active_kb,
            "session_id": self.active_session_id,
            "query": query,
            "answer": answer,
            "citations": refs,
            "evidence": refs,
            "feedback_type": feedback_type,
            "feedback_text": comment,
            "comment": comment,
            "correction_text": correction,
            "correction": correction,
            "save_as_knowledge": True,
            "skip_retrieval_feedback": skip_retrieval_feedback,
            "certainty": certainty,
            "created_by": "cli",
            **binding,
        }
        payload = {
            key: value for key, value in payload.items() if value not in (None, "")
        }
        try:
            result = self.feedback_store.record(payload)
        except Exception as exc:
            print(f"❌ 反馈记录失败: {exc}")
            return
        feedback_id = result["feedback_id"]
        if not skip_retrieval_feedback:
            try:
                self.retrieval_feedback_store.record_from_feedback(feedback_id, payload)
            except Exception as exc:
                print(f"⚠️ 调权记录失败，反馈已保留: {exc}")
        try:
            analysis = analyze_feedback(payload)
            self.feedback_analysis_store.record(feedback_id, payload, analysis)
        except Exception as exc:
            print(f"⚠️ 反馈分析失败，原始反馈已保留: {exc}")
        row = self._create_derived_knowledge(
            {
                "kb_id": self.active_kb,
                "text": correction,
                "status": "pending",
                "origin": feedback_type,
                "source_note": comment,
                "certainty": certainty,
                "created_from_trace_id": trace,
                "created_by": "cli",
                **binding,
            }
        )
        if row is not None:
            print(f"📝 反馈已记录: {feedback_id}")

    # 输出派生知识详情。
    def _print_knowledge_detail(self, item: dict) -> None:
        print(f"ID: {item.get('knowledge_id')}")
        print(f"状态: {item.get('status')}  版本: {item.get('version')}")
        print(f"来源: {item.get('origin')}  可信度: {item.get('certainty')}")
        print(f"创建: {item.get('created_by') or '-'}  {item.get('created_at') or '-'}")
        if item.get("reviewed_by") or item.get("review_note"):
            print(
                f"审核: {item.get('reviewed_by') or '-'}  "
                f"{item.get('reviewed_at') or '-'}  {item.get('review_note') or '-'}"
            )
        if item.get("conflict_group_id"):
            print(f"冲突组: {item.get('conflict_group_id')}")
        print("绑定:")
        print(f"  文档标识: {item.get('related_document_id') or '-'}")
        print(f"  关联文档: {item.get('related_source') or '-'}")
        print(f"  文档哈希: {item.get('related_source_sha256') or '-'}")
        print(f"  分块: {', '.join(item.get('related_chunk_ids') or []) or '-'}")
        print(
            f"  页码: {item.get('related_page_start') or '-'}"
            f" - {item.get('related_page_end') or '-'}"
        )
        print(f"  分块哈希: {item.get('related_chunk_text_hash') or '-'}")
        print(f"  锚点: {item.get('related_anchor_text') or '-'}")
        if item.get("source_note"):
            print(f"来源说明: {item.get('source_note')}")
        print("内容:")
        print(str(item.get("text") or ""))

    # 按 ID 读取派生知识。
    def _get_knowledge(self, knowledge_id: str) -> dict | None:
        if not self.active_kb:
            return None
        for item in self.knowledge_store.list(kb_id=self.active_kb):
            if item.get("knowledge_id") == knowledge_id:
                return item
        return None

    # 查找过期知识新版分块候选。
    def _stale_rebind_candidates(self, item: dict) -> list[dict]:
        source = str(item.get("related_source") or "")
        if not source:
            return []
        from cogdoc.service.source_chunks import chunk_preview, source_chunks

        anchor = str(item.get("related_anchor_text") or "").strip() or None
        try:
            chunks = [
                chunk_preview(chunk, anchor).model_dump()
                for chunk in source_chunks(self.active_kb, source)
            ]
        except Exception as exc:
            print(f"⚠️ 读取来源分块失败: {exc}")
            return []
        related_page = self._optional_int(str(item.get("related_page_start") or ""))
        scored = []
        for chunk in chunks:
            page = self._optional_int(
                str(chunk.get("page_start") or chunk.get("page") or "")
            )
            anchor_hit = bool(chunk.get("anchor_hit"))
            page_hit = related_page is not None and page == related_page
            if not anchor_hit and not page_hit:
                continue
            scored.append((0 if anchor_hit else 1, chunk))
        return [chunk for _, chunk in sorted(scored, key=lambda pair: pair[0])[:3]]

    # 输出派生知识列表。
    def _print_knowledge_rows(self, rows: list[dict]) -> None:
        if not rows:
            print("（没有匹配的派生知识。）")
            return
        for item in rows:
            conflict = (
                f" 冲突:{item.get('conflict_group_id')}"
                if item.get("conflict_group_id")
                else ""
            )
            source = item.get("related_source") or "-"
            print(
                f"• {item.get('knowledge_id')} "
                f"[{item.get('status')}] v{item.get('version')} "
                f"{item.get('origin')}/{item.get('certainty')} "
                f"来源:{source}{conflict}"
            )
            print(f"  {self._knowledge_title(item)}")

    # 派生知识计数。
    def _print_dk_counts(
        self,
        *,
        document_id: str | None = None,
        origin: str | None = None,
        created_by: str | None = None,
        has_conflict: bool | None = None,
        created_after: str | None = None,
        created_before: str | None = None,
    ) -> None:
        counts = self.knowledge_store.counts(
            kb_id=self.active_kb,
            document_id=document_id,
            origin=origin,
            created_by=created_by,
            has_conflict=has_conflict,
            created_after=created_after,
            created_before=created_before,
        )
        by_status = counts.get("by_status", {})
        by_origin = counts.get("by_origin", {})
        print(
            "派生知识统计: "
            f"总数 {counts.get('total', 0)} · "
            f"待审核 {by_status.get('pending', 0)} · "
            f"已通过 {by_status.get('approved', 0)} · "
            f"过期 {by_status.get('stale', 0)} · "
            f"已驳回 {by_status.get('rejected', 0)} · "
            f"已归档 {by_status.get('archived', 0)}"
        )
        if by_origin:
            parts = [f"{key}:{value}" for key, value in sorted(by_origin.items())]
            print("来源: " + " · ".join(parts))

    # 处理派生知识命令。
    def cmd_dk(self, arg: str) -> None:
        if not self._require_kb():
            return
        try:
            tokens = shlex.split(arg)
        except ValueError as exc:
            print(f"❌ 参数解析失败: {exc}")
            return
        sub = tokens[0].lower() if tokens else ""
        rest = tokens[1:]
        if sub in ("", "list"):
            opts = self._parse_dk_args(
                "/dk list",
                rest,
                lambda parser: (
                    parser.add_argument(
                        "status",
                        nargs="?",
                        choices=[
                            "review",
                            "all",
                            "pending",
                            "approved",
                            "stale",
                            "rejected",
                            "archived",
                        ],
                    ),
                    parser.add_argument("--doc"),
                    parser.add_argument("--origin"),
                    parser.add_argument("--created-by"),
                    parser.add_argument("--conflict", action="store_true"),
                    parser.add_argument("--from", dest="created_after"),
                    parser.add_argument("--to", dest="created_before"),
                ),
            )
            if opts is None:
                return
            filters = {
                "document_id": opts.doc,
                "origin": opts.origin,
                "created_by": opts.created_by,
                "has_conflict": True if opts.conflict else None,
                "created_after": opts.created_after,
                "created_before": opts.created_before,
            }
            self._print_dk_counts(**filters)
            status = opts.status
            if status in (None, "review"):
                rows = self.knowledge_store.list(
                    kb_id=self.active_kb, status="pending", **filters
                )
                rows.extend(
                    self.knowledge_store.list(
                        kb_id=self.active_kb, status="stale", **filters
                    )
                )
            else:
                rows = self.knowledge_store.list(
                    kb_id=self.active_kb,
                    status=None if status == "all" else status,
                    **filters,
                )
            self._print_knowledge_rows(rows)
            return
        if sub == "show":
            knowledge_id = rest[0] if rest else ""
            if not knowledge_id:
                print("用法: /dk show <知识ID>")
                return
            item = self._get_knowledge(knowledge_id)
            if item is None:
                print(f"⚠️ 找不到派生知识: {knowledge_id}")
                return
            self._print_knowledge_detail(item)
            return
        if sub == "add":

            def configure(parser):
                self._add_binding_args(parser)
                parser.add_argument("--note")
                parser.add_argument(
                    "--certainty",
                    choices=["high", "medium", "low"],
                    default="medium",
                )
                parser.add_argument(
                    "--origin",
                    choices=[
                        "manual_entry",
                        "correction",
                        "no_evidence",
                        "saved_answer",
                        "agent_suggested",
                    ],
                    default="manual_entry",
                )
                parser.add_argument("text", nargs="*")

            opts = self._parse_dk_args("/dk add", rest, configure)
            if opts is None:
                return
            text = " ".join(opts.text).strip()
            if not text:
                print("用法: /dk add <派生知识文本>")
                return
            try:
                binding = self._binding_payload(opts)
            except ValueError as exc:
                print(f"❌ 新增失败: {exc}")
                return
            try:
                row, deduplicated = self.knowledge_store.create(
                    {
                        "kb_id": self.active_kb,
                        "text": text,
                        "status": "pending",
                        "origin": opts.origin,
                        "source_note": opts.note,
                        "certainty": opts.certainty,
                        "created_by": "cli",
                        **binding,
                    }
                )
            except ValueError as exc:
                print(f"❌ 新增失败: {exc}")
                return
            marker = "（已存在，未重复新增）" if deduplicated else ""
            print(f"✅ 已保存为待审核派生知识: {row['knowledge_id']} {marker}")
            return
        if sub == "save-answer":

            def configure(parser):
                self._add_binding_args(parser)
                parser.add_argument("--note")
                parser.add_argument(
                    "--certainty",
                    choices=["high", "medium", "low"],
                    default="medium",
                )
                parser.add_argument("text", nargs="*")

            opts = self._parse_dk_args("/dk save-answer", rest, configure)
            if opts is None:
                return
            last = self._last_assistant_message()
            text = " ".join(opts.text).strip()
            if not text and last:
                text = str(last.get("content") or "").strip()
            if not text:
                if self.active_session_id is None:
                    print(
                        "当前没有打开对话。请先提问，或用 /open <对话ID> 打开历史对话。"
                    )
                else:
                    print(
                        "当前对话还没有 AI 回答。请先完成一轮问答，或直接传入答案文本。"
                    )
                print("用法: /dk save-answer [答案文本]")
                return
            try:
                binding = self._binding_payload(opts)
            except ValueError as exc:
                print(f"❌ 保存失败: {exc}")
                return
            self._create_derived_knowledge(
                {
                    "kb_id": self.active_kb,
                    "text": text,
                    "status": "pending",
                    "origin": "saved_answer",
                    "source_note": opts.note,
                    "certainty": opts.certainty,
                    "created_from_trace_id": str(last.get("trace_id") or "")
                    if last
                    else None,
                    "created_by": "cli",
                    **binding,
                }
            )
            return
        if sub in ("correction", "no-evidence"):

            def configure(parser):
                self._add_binding_args(parser)
                parser.add_argument("--query")
                parser.add_argument("--answer")
                parser.add_argument("--trace-id")
                parser.add_argument("--comment")
                parser.add_argument(
                    "--certainty",
                    choices=["high", "medium", "low"],
                    default="medium",
                )
                parser.add_argument("text", nargs="*")

            opts = self._parse_dk_args(f"/dk {sub}", rest, configure)
            if opts is None:
                return
            correction = " ".join(opts.text).strip()
            if not correction:
                print(f"用法: /dk {sub} <正确说法>")
                return
            try:
                binding = self._binding_payload(opts)
            except ValueError as exc:
                print(f"❌ 提交失败: {exc}")
                return
            if not binding.get("related_chunk_ids"):
                print(
                    "❌ 请传 --chunk-ids 关联被纠错/补充的分块；"
                    "没有分块上下文时请用 /dk add 或 /dk save-answer。"
                )
                return
            feedback_type = "no_evidence" if sub == "no-evidence" else "correction"
            self._record_feedback_flow(
                feedback_type=feedback_type,
                correction=correction,
                comment=opts.comment,
                binding=binding,
                skip_retrieval_feedback=(feedback_type == "no_evidence"),
                certainty=opts.certainty,
                query=opts.query,
                answer=opts.answer,
                trace_id=opts.trace_id,
            )
            return
        if sub in ("approve", "reject", "archive"):

            def configure(parser):
                parser.add_argument("knowledge_id")
                parser.add_argument("--note")
                self._add_binding_args(parser)

            opts = self._parse_dk_args(f"/dk {sub}", rest, configure)
            if opts is None:
                return
            knowledge_id = opts.knowledge_id
            if not knowledge_id:
                print(f"用法: /dk {sub} <知识ID>")
                return
            try:
                binding = self._binding_payload(opts)
            except ValueError as exc:
                print(f"❌ 审核失败: {exc}")
                return
            binding = {
                key: value
                for key, value in binding.items()
                if value not in (None, [], "")
            }
            row = self.knowledge_store.set_status(
                knowledge_id,
                {"approve": "approved", "reject": "rejected", "archive": "archived"}[
                    sub
                ],
                actor="cli",
                note=opts.note,
                binding_updates=binding or None,
            )
            if row is None:
                print(f"⚠️ 找不到派生知识: {knowledge_id}")
                return
            action_label = {"approve": "通过", "reject": "驳回", "archive": "归档"}[sub]
            print(f"✅ 已{action_label}: {knowledge_id}")
            if sub in ("approve", "archive"):
                self._refresh_derived_knowledge_index(
                    str(row.get("kb_id") or self.active_kb)
                )
            return
        if sub == "delete":
            knowledge_id = rest[0] if rest else ""
            if not knowledge_id:
                print("用法: /dk delete <知识ID>")
                return
            if not self._confirm(f"确认删除派生知识 {knowledge_id}？此操作不可恢复"):
                print("已取消。")
                return
            row = self.knowledge_store.delete(knowledge_id)
            if row is None:
                print(f"⚠️ 找不到派生知识: {knowledge_id}")
                return
            print(f"🗑️ 已删除派生知识: {knowledge_id}")
            self._refresh_derived_knowledge_index(
                str(row.get("kb_id") or self.active_kb)
            )
            return
        if sub == "revise":

            def configure(parser):
                parser.add_argument("knowledge_id")
                self._add_binding_args(parser)
                parser.add_argument("--note")
                parser.add_argument(
                    "--certainty",
                    choices=["high", "medium", "low"],
                    default="medium",
                )
                parser.add_argument("text", nargs="*")

            opts = self._parse_dk_args("/dk revise", rest, configure)
            if opts is None:
                return
            if not opts.text:
                print("用法: /dk revise <知识ID> <新文本>")
                return
            knowledge_id = opts.knowledge_id
            text = " ".join(opts.text).strip()
            try:
                binding = self._binding_payload(opts)
            except ValueError as exc:
                print(f"❌ 修订失败: {exc}")
                return
            try:
                row = self.knowledge_store.revise(
                    knowledge_id,
                    {
                        "text": text,
                        "status": "pending",
                        "source_note": opts.note,
                        "certainty": opts.certainty,
                        "created_by": "cli",
                        **binding,
                    },
                )
            except ValueError as exc:
                print(f"❌ 修订失败: {exc}")
                return
            if row is None:
                print(f"⚠️ 找不到派生知识: {knowledge_id}")
                return
            print(f"✅ 已创建修订草稿: {row['knowledge_id']}（原知识 {knowledge_id}）")
            return
        if sub in ("batch-approve", "batch-reject"):
            ids = rest
            if not ids:
                print(f"用法: /dk {sub} <知识ID...>")
                return
            status = "approved" if sub == "batch-approve" else "rejected"
            updated, missing = self.knowledge_store.batch_set_status(
                ids,
                status,
                actor="cli",
            )
            print(f"✅ 已处理 {len(updated)} 条。")
            if missing:
                print("未找到: " + ", ".join(missing))
            if status == "approved" and updated:
                self._refresh_derived_knowledge_index(self.active_kb)
            return
        if sub == "candidates":
            knowledge_id = rest[0] if rest else ""
            if not knowledge_id:
                print("用法: /dk candidates <知识ID>")
                return
            item = self._get_knowledge(knowledge_id)
            if item is None:
                print(f"⚠️ 找不到派生知识: {knowledge_id}")
                return
            candidates = self._stale_rebind_candidates(item)
            if not candidates:
                print("（未找到可直接确认的新版分块候选。）")
                return
            for idx, chunk in enumerate(candidates, start=1):
                chunk_id = str(chunk.get("chunk_id") or "")
                page_start = chunk.get("page_start", chunk.get("page"))
                page_end = chunk.get("page_end", page_start)
                print(
                    f"候选 {idx}: chunk={chunk_id or '-'} "
                    f"page={page_start or '-'}-{page_end or '-'} "
                    f"sha={chunk.get('source_sha256') or '-'} "
                    f"hash={chunk.get('text_hash') or '-'}"
                )
                print(f"  {chunk.get('text_preview') or ''}")
                print(
                    "  采用: "
                    f"/dk approve {knowledge_id} --source {item.get('related_source')} "
                    f"--source-sha {chunk.get('source_sha256') or ''} "
                    f"--chunk-ids {chunk_id} --page-start {page_start or ''} "
                    f"--page-end {page_end or ''} --chunk-hash {chunk.get('text_hash') or ''} "
                    "--note 采用候选分块复核通过"
                )
            return
        if sub == "stale-scan":
            stale = self.knowledge_store.mark_stale_by_documents(
                self.active_kb,
                _kb_documents(self.active_kb),
            )
            print(f"✅ 过期派生知识扫描完成，新增标记 {len(stale)} 条。")
            if stale:
                self._print_knowledge_rows(stale)
                self._refresh_derived_knowledge_index(self.active_kb)
            return
        if sub == "status":
            try:
                status = self.knowledge_index.status(self.active_kb)
            except Exception as exc:
                print(f"❌ 读取派生知识索引状态失败: {exc}")
                return
            print(
                f"派生知识索引: {status.get('state')} · "
                f"已通过 {status.get('approved_count')} · "
                f"已索引 {status.get('indexed_count')} · "
                f"collection {status.get('collection_name')}"
            )
            if status.get("last_error") or status.get("collection_error"):
                print(
                    f"错误: last={status.get('last_error') or '-'} "
                    f"collection={status.get('collection_error') or '-'}"
                )
            return
        print("❓ 未知 /dk 子命令。输入 /help 查看派生知识命令。")

    # 打印反馈记录。
    def _print_feedback_rows(self, rows: list[dict]) -> None:
        if not rows:
            print("（没有匹配的反馈。）")
            return
        for row in rows:
            title = (
                f"• {row.get('feedback_id')} [{row.get('feedback')}] "
                f"{row.get('feedback_type') or '-'} trace:{row.get('trace_id') or '-'}"
            )
            print(title)
            if row.get("query"):
                print(f"  Q: {row.get('query')}")
            if row.get("correction"):
                print(f"  纠正: {row.get('correction')}")
            elif row.get("comment"):
                print(f"  备注: {row.get('comment')}")

    # 反馈记录命令。
    def cmd_feedback(self, arg: str) -> None:
        if not self._require_kb():
            return
        try:
            tokens = shlex.split(arg)
        except ValueError as exc:
            print(f"❌ 参数解析失败: {exc}")
            return
        sub = tokens[0].lower() if tokens else "list"
        rest = tokens[1:]
        if sub == "list":

            def configure(parser):
                parser.add_argument(
                    "feedback",
                    nargs="?",
                    choices=["all", "thumbs_up", "thumbs_down", "correction"],
                    default="all",
                )
                parser.add_argument("--type")
                parser.add_argument("--trace-id")
                parser.add_argument("--session-id")
                parser.add_argument("--bad-case", action="store_true")
                parser.add_argument("--limit", type=int, default=100)

            opts = self._parse_dk_args("/feedback list", rest, configure)
            if opts is None:
                return
            rows = self.feedback_store.list(
                kb_id=self.active_kb,
                feedback=None if opts.feedback == "all" else opts.feedback,
                feedback_type=opts.type,
                trace_id=opts.trace_id,
                session_id=opts.session_id,
                is_bad_case=True if opts.bad_case else None,
                limit=opts.limit,
            )
            self._print_feedback_rows(rows)
            return
        if sub == "analysis":

            def configure(parser):
                parser.add_argument("--action")
                parser.add_argument("--trace-id")
                parser.add_argument("--needs-review", action="store_true")
                parser.add_argument("--limit", type=int, default=100)

            opts = self._parse_dk_args("/feedback analysis", rest, configure)
            if opts is None:
                return
            rows = self.feedback_analysis_store.list(
                kb_id=self.active_kb,
                trace_id=opts.trace_id,
                recommended_action=opts.action,
                needs_review=True if opts.needs_review else None,
                limit=opts.limit,
            )
            if not rows:
                print("（没有匹配的反馈分析。）")
                return
            for row in rows:
                print(
                    f"• {row.get('feedback_analysis_id')} "
                    f"{row.get('feedback_type')} · {row.get('recommended_action')} · "
                    f"{row.get('confidence')}"
                )
                if row.get("extracted_claim"):
                    print(f"  {row.get('extracted_claim')}")
            return
        print("❓ 未知 /feedback 子命令。可用: list / analysis")

    # 打印调权记录。
    def _print_tuning_rows(self, rows: list[dict]) -> None:
        if not rows:
            print("（没有匹配的调权。）")
            return
        for row in rows:
            status = "启用" if row.get("enabled") is True else "禁用"
            chunks = row.get("target_chunks") or []
            chunk_count = row.get("chunk_count") or len(chunks) or 1
            print(
                f"• {row.get('retrieval_feedback_id')} {status} "
                f"{row.get('weight_delta')} · {chunk_count} 个分块"
            )
            if row.get("query_text"):
                print(f"  Q: {row.get('query_text')}")
            if chunks:
                print(
                    "  分块: "
                    + ", ".join(str(item.get("chunk_id")) for item in chunks[:8])
                )

    # 检索调权命令。
    def cmd_tuning(self, arg: str) -> None:
        if not self._require_kb():
            return
        try:
            tokens = shlex.split(arg)
        except ValueError as exc:
            print(f"❌ 参数解析失败: {exc}")
            return
        sub = tokens[0].lower() if tokens else "list"
        rest = tokens[1:]
        if sub == "list":

            def configure(parser):
                parser.add_argument(
                    "status",
                    nargs="?",
                    choices=["enabled", "disabled", "all"],
                    default="enabled",
                )
                parser.add_argument("--limit", type=int, default=100)

            opts = self._parse_dk_args("/tuning list", rest, configure)
            if opts is None:
                return
            enabled = None
            if opts.status == "enabled":
                enabled = True
            elif opts.status == "disabled":
                enabled = False
            rows = self.retrieval_feedback_store.list(
                kb_id=self.active_kb,
                enabled=enabled,
                limit=opts.limit,
            )
            counts = self.retrieval_feedback_store.counts(kb_id=self.active_kb)
            print(
                f"调权统计: 总数 {counts['total']} · "
                f"启用 {counts['enabled']} · 禁用 {counts['disabled']}"
            )
            self._print_tuning_rows(rows)
            return
        if sub in ("enable", "disable"):
            feedback_id = rest[0] if rest else ""
            if not feedback_id:
                print(f"用法: /tuning {sub} <调权ID>")
                return
            reason = " ".join(rest[1:]).strip() or None
            row = self.retrieval_feedback_store.set_enabled(
                feedback_id,
                sub == "enable",
                actor="cli",
                reason=reason,
            )
            if row is None:
                print(f"⚠️ 找不到调权记录: {feedback_id}")
                return
            print(f"✅ 已{'启用' if sub == 'enable' else '禁用'}调权: {feedback_id}")
            return
        print("❓ 未知 /tuning 子命令。可用: list / enable / disable")

    # 审核队列摘要。
    def _review_summary_payload(self) -> dict:
        knowledge = self.knowledge_store.counts(kb_id=self.active_kb)
        conflicts = self.knowledge_store.conflict_counts(kb_id=self.active_kb)
        auto_review = self.knowledge_store.auto_review_counts(kb_id=self.active_kb)
        feedback = self.feedback_store.counts(kb_id=self.active_kb)
        analysis = self.feedback_analysis_store.counts(kb_id=self.active_kb)
        retrieval = self.retrieval_feedback_store.counts(kb_id=self.active_kb)
        return {
            "kb_id": self.active_kb,
            "knowledge": knowledge["by_status"],
            "knowledge_total": knowledge["total"],
            "knowledge_origin": knowledge["by_origin"],
            "knowledge_conflicts": conflicts,
            "knowledge_auto_review": {
                **auto_review,
                "stale_pending": int(knowledge["by_status"].get("stale", 0)),
            },
            "feedback_counts": feedback,
            "feedback_analysis": {
                **analysis["by_action"],
                "needs_review": analysis["needs_review"],
                "total": analysis["total"],
            },
            "feedback_analysis_type": analysis["by_type"],
            "retrieval_feedback": retrieval,
        }

    # 反馈闭环指标。
    def _review_metrics_payload(self, answer_count: int | None = None) -> dict:
        answer_total = max(
            answer_count or 0, self.sessions.answer_count(self.active_kb)
        )
        denominator = answer_total if answer_total > 0 else None
        feedback = self.feedback_store.counts(kb_id=self.active_kb)
        knowledge = self.knowledge_store.counts(kb_id=self.active_kb)
        analysis = self.feedback_analysis_store.counts(kb_id=self.active_kb)
        retrieval = self.retrieval_feedback_store.counts(kb_id=self.active_kb)
        stale_review = self.knowledge_store.stale_review_counts(kb_id=self.active_kb)

        def rate(num: int, den: int | None):
            return None if not den else round(num / den, 4)

        by_feedback = feedback["by_feedback"]
        by_type = feedback["by_type"]
        by_status = knowledge["by_status"]
        by_action = analysis["by_action"]
        feedback_total = int(feedback["total"])
        negative_total = int(feedback["bad_cases"])
        correction_total = int(by_feedback.get("correction", 0))
        no_evidence_total = int(by_type.get("no_evidence", 0))
        knowledge_total = int(knowledge["total"])
        approved_total = int(by_status.get("approved", 0))
        rejected_total = int(by_status.get("rejected", 0))
        pending_created = int(by_action.get("create_pending_knowledge", 0))
        retrieval_total = int(retrieval["total"])
        retrieval_disabled = int(retrieval["disabled"])
        stale_total = int(stale_review["total"])
        stale_reviewed = int(stale_review["reviewed"])
        return {
            "kb_id": self.active_kb,
            "counts": {
                "answer_total": answer_total,
                "feedback_total": feedback_total,
                "negative_feedback_total": negative_total,
                "no_evidence_feedback_total": no_evidence_total,
                "correction_feedback_total": correction_total,
                "knowledge_total": knowledge_total,
                "approved_knowledge_total": approved_total,
                "rejected_knowledge_total": rejected_total,
                "pending_created_total": pending_created,
                "retrieval_feedback_total": retrieval_total,
                "retrieval_feedback_disabled": retrieval_disabled,
                "stale_knowledge_total": stale_total,
                "stale_knowledge_reviewed": stale_reviewed,
            },
            "rates": {
                "feedback_rate": rate(feedback_total, denominator),
                "negative_feedback_rate": rate(negative_total, denominator),
                "no_evidence_rate": rate(no_evidence_total, denominator),
                "pending_approval_rate": rate(approved_total, knowledge_total),
                "pending_rejection_rate": rate(rejected_total, knowledge_total),
                "feedback_to_pending_rate": rate(pending_created, correction_total),
                "retrieval_feedback_rollback_rate": rate(
                    retrieval_disabled, retrieval_total
                ),
                "stale_review_completion_rate": rate(stale_reviewed, stale_total),
            },
        }

    # 审核看板命令。
    def cmd_review(self, arg: str) -> None:
        if not self._require_kb():
            return
        try:
            tokens = shlex.split(arg)
        except ValueError as exc:
            print(f"❌ 参数解析失败: {exc}")
            return
        sub = tokens[0].lower() if tokens else "summary"
        rest = tokens[1:]
        if sub == "summary":
            self._print_json(self._review_summary_payload())
            return
        if sub == "metrics":

            def configure(parser):
                parser.add_argument("--answer-count", type=int)

            opts = self._parse_dk_args("/review metrics", rest, configure)
            if opts is None:
                return
            self._print_json(self._review_metrics_payload(opts.answer_count))
            return
        if sub == "export":

            def configure(parser):
                parser.add_argument("--limit", type=int, default=200)

            opts = self._parse_dk_args("/review export", rest, configure)
            if opts is None:
                return
            limit = opts.limit
            payload = {
                "kb_id": self.active_kb,
                "generated_at": now_iso(),
                "summary": self._review_summary_payload(),
                "pending_knowledge": self.knowledge_store.list(
                    kb_id=self.active_kb, status="pending", limit=limit
                ),
                "stale_knowledge": self.knowledge_store.list(
                    kb_id=self.active_kb, status="stale", limit=limit
                ),
                "auto_review_events": self.knowledge_store.auto_review_events(
                    kb_id=self.active_kb, limit=limit
                ),
                "feedback_analysis_needs_review": self.feedback_analysis_store.list(
                    kb_id=self.active_kb, needs_review=True, limit=limit
                ),
                "retrieval_feedback_enabled": self.retrieval_feedback_store.list(
                    kb_id=self.active_kb, enabled=True, limit=limit
                ),
                "feedback_bad_cases": self.feedback_store.list(
                    kb_id=self.active_kb, is_bad_case=True, limit=limit
                ),
            }
            self._print_json(payload)
            return
        print("❓ 未知 /review 子命令。可用: summary / metrics / export")

    # 云端配置。

    # 配置云端配置。
    def _configure_cloud(self, first_time: bool) -> bool:
        # 写入 .env 并即时生效；返回云端是否可用（有 key）。
        from cogdoc.config.llm_config import apply_llm_config

        settings = get_settings()
        if first_time:
            print(
                "⚠️ 还没有配置云端 API Key，无法使用云端模式。现在配置（回车保留当前值）："
            )
        else:
            print("✏️ 修改云端模型配置（回车保留当前值）：")
        base = input(f"  云端 Base URL [{settings.llm_base_url}]: ").strip()
        model = input(f"  云端模型名 [{settings.llm_model_name}]: ").strip()
        cur_key = settings.llm_api_key
        key_hint = _mask_key(cur_key) if cur_key else "未设置"
        key = input(f"  云端 API Key [{key_hint}]: ").strip()
        final_key = key or cur_key
        if not final_key:
            print("❌ 未提供 API Key，云端模式不可用。")
            return False
        apply_llm_config(
            api_key=final_key,
            base_url=base or settings.llm_base_url,
            model=model or settings.llm_model_name,
        )
        print("✅ 云端配置已写入 .env 并即时生效。")
        return True

    # 问答。

    # 输出回答。
    def _print_answer(self, task_type: str, output: dict) -> None:
        if task_type == "qa":
            if "critique" not in output:
                print("\n⚠️ 未返回引证校验状态，已拒绝输出未确认答案。")
            elif output.get("critique"):
                print("\n❌ 引证校验未通过，已达最大自愈次数，本轮答案已拦截。")
            else:
                ans = output.get("answer", "")
                print(f"\n🤖 {ans}" if ans else "\n⚠️ 模型返回了空内容。")
        elif task_type == "summary":
            ans = output.get("answer", "")
            print(f"\n🤖 {ans}" if ans else "\n⚠️ 摘要为空。")
        else:
            content = extract_final_answer(task_type, output) or UNKNOWN_RESPONSE
            print(f"\n🤖 {content}")
        print()

    # 执行一次控制台问答。
    def do_chat(self, query: str, forced_task: str | None) -> None:
        if not self._require_kb():
            return
        if self.active_session_id is None:
            self.active_session_id = uuid4().hex
            print(f"🆕 （已自动开启新对话 {self.active_session_id[:8]}）")
        kb, sid = self.active_kb, self.active_session_id
        chat_history = self.sessions.get_history(kb, sid, query)
        final_result = None
        try:
            for event in run_chat(
                doc_id=kb,
                query=query,
                is_local=self.is_local,
                chat_history=chat_history,
                forced_task=forced_task,
                session_id=sid,
                state_runtime=self.state_runtime,
            ):
                if event.type == "error":
                    print(f"\n⚠️ 执行中断: {event.payload.get('message', '')}")
                elif event.type == "final":
                    result = event.payload["result"]
                    output = event.payload.get("output", result.raw_output)
                    self._print_answer(result.task_type, output)
                    final_result = result
        except Exception as e:
            print(f"⚠️ 问答执行异常: {e}")
            return
        if final_result is not None:
            self.sessions.record(
                kb,
                sid,
                final_result.chat_messages,
                [
                    {"role": "user", "content": query},
                    {
                        "role": "assistant",
                        "content": final_result.answer,
                        "trace_id": final_result.trace_id,
                        "query": query,
                        "task_type": final_result.task_type,
                    },
                ],
            )

    # 补全。

    # 构造names。
    def _doc_names(self) -> list[str]:
        if self.active_kb is None:
            return []
        return [d.get("name", "") for d in _kb_documents(self.active_kb)]

    # 完成 会话前缀列表 处理。
    def _session_prefixes(self) -> list[str]:
        if self.active_kb is None:
            return []
        return [
            s["session_id"][:8] for s in self.sessions.list_sessions(self.active_kb)
        ]

    # 完成 补全候选项 处理。
    def _completion_candidates(self, tokens: list[str]) -> list[str]:
        # tokens 是光标前已完成的词；为空说明正在补第一个词，给出全部命令。
        if not tokens:
            return COMPLETION_COMMANDS
        cmd = tokens[0].lower()
        if cmd == "/kb":
            if len(tokens) == 1:
                return KB_SUBCOMMANDS
            if len(tokens) == 2 and tokens[1].lower() in ("use", "rm"):
                return [
                    r["kb_id"]
                    for r in self.registry.list(tenant_id="default")
                ]
            return []
        if cmd in ("/dk", "/knowledge"):
            if len(tokens) == 1:
                return DK_SUBCOMMANDS
            if len(tokens) == 2 and tokens[1].lower() in (
                "show",
                "approve",
                "reject",
                "archive",
                "delete",
                "revise",
                "candidates",
                "batch-approve",
                "batch-reject",
            ):
                rows = (
                    self.knowledge_store.list(kb_id=self.active_kb)
                    if self.active_kb
                    else []
                )
                return [str(row.get("knowledge_id")) for row in rows]
            if len(tokens) > 2 and tokens[1].lower() in (
                "batch-approve",
                "batch-reject",
            ):
                rows = (
                    self.knowledge_store.list(kb_id=self.active_kb)
                    if self.active_kb
                    else []
                )
                selected = set(tokens[2:])
                return [
                    str(row.get("knowledge_id"))
                    for row in rows
                    if str(row.get("knowledge_id")) not in selected
                ]
            return []
        if cmd == "/feedback":
            return FEEDBACK_SUBCOMMANDS if len(tokens) == 1 else []
        if cmd == "/tuning":
            if len(tokens) == 1:
                return TUNING_SUBCOMMANDS
            if len(tokens) == 2 and tokens[1].lower() in ("enable", "disable"):
                rows = (
                    self.retrieval_feedback_store.list(
                        kb_id=self.active_kb, enabled=None
                    )
                    if self.active_kb
                    else []
                )
                return [str(row.get("retrieval_feedback_id")) for row in rows]
            return []
        if cmd == "/review":
            return REVIEW_SUBCOMMANDS if len(tokens) == 1 else []
        if cmd == "/add":
            return self._inbox_pdfs() if len(tokens) == 1 else []
        if cmd in ("/rm", "/summary"):
            return self._doc_names() if len(tokens) == 1 else []
        if cmd == "/compare":
            return self._doc_names()
        if cmd in ("/open", "/rmchat"):
            return self._session_prefixes() if len(tokens) == 1 else []
        return []

    # 补全结果。
    def complete(self, text: str, state: int) -> str | None:
        # readline 对同一补全会按 state 递增回调，state==0 时重算候选并缓存。
        try:
            if state == 0:
                tokens = readline.get_line_buffer()[: readline.get_begidx()].split()
                self._completion_matches = [
                    c
                    for c in self._completion_candidates(tokens)
                    if c and c.startswith(text)
                ]
            return (
                self._completion_matches[state]
                if state < len(self._completion_matches)
                else None
            )
        except Exception:
            return None

    # 分发。

    # 分发结果。
    def dispatch(self, raw: str) -> bool:
        # 返回 False 表示退出控制台。
        text = raw.strip()
        if not text:
            return True
        low = text.lower()
        if low in ("exit", "quit", "/exit", "/quit"):
            return False
        if low == "/help":
            print(HELP_TEXT)
            return True
        if low == "/local":
            self.is_local = True
            print("🔄 已切换到：本地 Ollama 模式。")
            return True
        if low == "/cloud":
            if not get_settings().llm_api_key and not self._configure_cloud(
                first_time=True
            ):
                return True
            self.is_local = False
            print("🔄 已切换到：云端 API 模式。")
            return True
        if low == "/config":
            self._configure_cloud(first_time=False)
            return True
        if low == "/inbox":
            self.cmd_inbox()
            return True
        if low in ("/docs", "/ls"):
            self.cmd_docs()
            return True
        if low == "/new":
            self.cmd_new()
            return True
        if low == "/chats":
            self.cmd_chats()
            return True

        if low.startswith("/kb"):
            toks = text.split(maxsplit=2)
            sub = toks[1].lower() if len(toks) > 1 else ""
            name = toks[2].strip() if len(toks) > 2 else ""
            self.cmd_kb(sub, name)
            return True

        parts = text.split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""
        if cmd == "/add":
            self.cmd_add(arg)
            return True
        if cmd == "/rm":
            self.cmd_rm(arg)
            return True
        if cmd == "/open":
            self.cmd_open(arg)
            return True
        if cmd == "/rmchat":
            self.cmd_rmchat(arg)
            return True
        if cmd in ("/dk", "/knowledge"):
            self.cmd_dk(arg)
            return True
        if cmd == "/feedback":
            self.cmd_feedback(arg)
            return True
        if cmd == "/tuning":
            self.cmd_tuning(arg)
            return True
        if cmd == "/review":
            self.cmd_review(arg)
            return True

        forced_task, cleaned_query = parse_forced_mode(text)
        if forced_task:
            if not cleaned_query:
                print(f"⚠️ 请输入 /{forced_task} 后面的具体问题或文档指令。")
                return True
            self.do_chat(cleaned_query, forced_task)
            return True

        if text.startswith("/"):
            print(f"❓ 未知命令: {cmd}。输入 /help 查看可用命令。")
            return True

        self.do_chat(text, None)
        return True


# 配置补全。
def _setup_completion(console: "Console") -> None:
    # 无 readline（如 Windows 原生）则静默跳过，不影响主流程。
    if readline is None:
        return
    # 仅以空白为分隔符，使补全词保留前导 / 与中文文件名（默认分隔符会切碎它们）。
    readline.set_completer_delims(" \t\n")
    readline.set_completer(console.complete)
    # macOS 自带的是 libedit，绑定语法与 GNU readline 不同。
    if "libedit" in (getattr(readline, "__doc__", "") or ""):
        readline.parse_and_bind("bind ^I rl_complete")
    else:
        readline.parse_and_bind("tab: complete")


# 启动横幅：纯静态 ASCII（ansi_shadow 字体），不引入运行时依赖。
BANNER = r"""
 ██████╗ ██████╗  ██████╗ ██████╗  ██████╗  ██████╗
██╔════╝██╔═══██╗██╔════╝ ██╔══██╗██╔═══██╗██╔════╝
██║     ██║   ██║██║  ███╗██║  ██║██║   ██║██║
██║     ██║   ██║██║   ██║██║  ██║██║   ██║██║
╚██████╗╚██████╔╝╚██████╔╝██████╔╝╚██████╔╝╚██████╗
 ╚═════╝ ╚═════╝  ╚═════╝ ╚═════╝  ╚═════╝  ╚═════╝
"""


# 启动入口。
def main():
    configure_logging()

    # CLI 与 API 共用进程锁：CLI 会构建索引（写），不得与运行中的实例并发写同一数据目录。
    lock_fh = acquire_single_instance_lock()
    if lock_fh is None and strict_single_process():
        reason = (
            "当前平台不支持进程锁，无法保证单实例"
            if not locking_supported()
            else "已有 CogDoc 实例（API 或 CLI）在运行"
        )
        print(f"❌ {reason}；如确需放行请设 COGDOC_ALLOW_MULTI=1。")
        sys.exit(1)
    atexit.register(_release_runtime_lock, lock_fh)

    # 必须在任何构建前回放 journal，使源目录与 active 代一致。
    shared_mutation_journal().recover_all()
    drain_purge_queue()

    try:
        get_rust_core()
    except RuntimeError as exc:
        print(f"❌ {exc}")
        _release_runtime_lock(lock_fh)
        sys.exit(1)

    # 全局检索/重排模型是单例，启动时预热一次；per-KB bm25 在切库时按需预热。
    try:
        print("🧠 正在预热检索与重排模型，请稍候...")
        Embedder.get_model()
        BGEReranker.warm_up()
        print("✅ 模型资源预热完成。")
    except Exception as e:
        print(f"⚠️ 预热阶段失败，稍后提问时仍会尝试按需加载: {e}")

    console = Console()
    atexit.register(console.close)
    _setup_completion(console)

    print(BANNER)
    print("=" * 60)
    print("🚀 CogDoc 控制台 | 多知识库 + 多对话 | 输入 /help 查看命令")
    print(f"📥 收件箱目录: {console.inbox_dir}（把 PDF 放进来，再 /add 入库）")
    records = console.registry.list(tenant_id="default")
    if not records:
        print("ℹ️ 当前还没有知识库。用 /kb new <名称> 创建你的第一个知识库。")
    elif len(records) == 1:
        # 仅一个库时自动切入。
        console._use_kb(records[0]["kb_id"])
    else:
        print("ℹ️ 已有知识库，用 /kb 查看、/kb use <名称> 切入。")
    print("=" * 60)

    while True:
        try:
            scope = console.active_kb or "未选库"
            chat = (
                console.active_session_id[:8] if console.active_session_id else "无对话"
            )
            mode = "本地" if console.is_local else "云端"
            user_input = input(f"[{scope}|{chat}|{mode}] >>> ")
            if not console.dispatch(user_input):
                print("👋 控制台正在释放资源，再见。")
                break
        except KeyboardInterrupt:
            safe_print_on_interrupt("\n👋 检测到系统中断信号（Ctrl+C），安全关闭。")
            break
        except EOFError:
            print("\n👋 输入流结束，安全关闭。")
            break
        except Exception as e:
            print(f"⚠️ [控制台内部异常捕获]: {e}")

    try:
        console.close()
    finally:
        _release_runtime_lock(lock_fh)


if __name__ == "__main__":
    main()
