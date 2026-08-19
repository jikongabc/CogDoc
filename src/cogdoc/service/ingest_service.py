import hashlib
import json
import logging
import os
import shutil
import threading
import time
from dataclasses import dataclass, field
from cogdoc.api.derived_knowledge_store import (
    AUTO_REBIND_REVIEW_NOTE,
    DerivedKnowledgeStore,
)
from cogdoc.config.settings import get_settings
from cogdoc.service.retriever_factory import RetrieverFactory
from cogdoc.observability.logger import log_event
from cogdoc.service.kb_epoch import shared_epoch_store
from cogdoc.service.kb_lifecycle import (
    LIFECYCLE_DELETED,
    LIFECYCLE_DELETING,
    shared_lifecycle_store,
)
from cogdoc.service.kb_locks import kb_write_lock
from cogdoc.service.kb_readers import has_readers
from cogdoc.service.purge_queue import shared_purge_queue
from cogdoc.service.kb_state import KBState
from cogdoc.source_model import SourceDocument
from cogdoc.tools.chunk_identity import (
    CHUNKING_STRATEGY_VERSION,
    CHUNK_IDENTITY_VERSION,
    build_chunk_id,
)
from cogdoc.tools.chunker import chunk_paper, chunking_stats_dict
from cogdoc.tools.manifest import (
    load_index_manifest,
    manifest_path,
    save_index_manifest,
    stamp_chunk_identity_contract,
    stamp_source_document_contract,
)
from cogdoc.tools.embedder import Embedder
from cogdoc.tools.parser import PARSER_VERSION, smart_parse
from cogdoc.tools.source_parser import (
    SOURCE_PARSER_VERSION,
    list_supported_files,
    parse_source,
    scan_source_manifest,
)
from cogdoc.tools.retriever.bm25_retriever import BM25Retriever
from cogdoc.tools.retriever.hybrid import HybridRetriever
from cogdoc.tools.retriever.vector_retriever import VectorRetriever
from cogdoc.tools.rust_core_loader import ensure_rust_core
from cogdoc.tools.tokenizer import TOKENIZER_VERSION


# 增量复用门控：任一构建组件版本变化都使旧索引不可复用，强制全量重建。
INDEX_BUILD_VERSION = (
    f"{CHUNK_IDENTITY_VERSION}"
    f"|parser={PARSER_VERSION}"
    f"|source_parser={SOURCE_PARSER_VERSION}"
    f"|tokenizer={TOKENIZER_VERSION}"
    f"|embedder={Embedder.EMBEDDING_CONTRACT_VERSION}"
)
_SOURCE_CONTRACTS_SIDECAR = ".cogdoc-source-contracts.json"


# 新建不含页面文本或错误详情的 OCR 汇总。
def _empty_ocr_summary() -> dict:
    return {
        "candidate_pages": 0,
        "attempted_pages": 0,
        "succeeded_pages": 0,
        "degraded_pages": 0,
        "failed_pages": 0,
        "status_counts": {},
    }


# 从本次已解析页面生成 OCR 可观测性统计，不触发重复解析。
def _summarize_ocr_pages(pages: list) -> dict:
    summary = _empty_ocr_summary()
    statuses = summary["status_counts"]
    for page in pages:
        if not isinstance(page, dict):
            continue
        status = str(page.get("ocr_status") or "")
        if not status:
            continue
        statuses[status] = statuses.get(status, 0) + 1
        if status == "not_needed":
            continue
        summary["candidate_pages"] += 1
        if status not in {"disabled", "limit"}:
            summary["attempted_pages"] += 1
        if status == "succeeded":
            summary["succeeded_pages"] += 1
        elif page.get("extraction_method") == "native":
            summary["degraded_pages"] += 1
        else:
            summary["failed_pages"] += 1
    return summary


# 合并多个文档或构建分支的 OCR 汇总。
def _merge_ocr_summary(target: dict, source: dict) -> None:
    for key in (
        "candidate_pages",
        "attempted_pages",
        "succeeded_pages",
        "degraded_pages",
        "failed_pages",
    ):
        target[key] += source[key]
    statuses = target["status_counts"]
    for status, count in source["status_counts"].items():
        statuses[status] = statuses.get(status, 0) + count


# 成功构建后记录安全汇总；日志失败不能影响已提交索引。
def _log_ocr_summary(result) -> None:
    try:
        log_event("ingest", "ocr_summary", {}, kb_id=result.kb_id, **result.ocr_summary)
    except Exception:
        pass


def _log_chunking_summary(source: str, chunks: list) -> None:
    try:
        log_event(
            "ingest",
            "chunking_summary",
            {},
            source=source,
            chunking_strategy_version=CHUNKING_STRATEGY_VERSION,
            **chunking_stats_dict(chunks),
        )
    except Exception:
        pass


# 写入索引构建版本。
def stamp_index_build_version(manifest: dict) -> dict:
    # 写入当前构建版本，启动检查与入库共用同一门控。
    manifest["index_build_version"] = INDEX_BUILD_VERSION
    return manifest


# 定义入库文档结果。
@dataclass(frozen=True)
class IngestDocResult:
    name: str
    chunk_count: int


# 定义入库结果。
@dataclass(frozen=True)
class IngestResult:
    kb_id: str
    document_count: int
    chunk_count: int
    documents: list[IngestDocResult] = field(default_factory=list)
    ocr_summary: dict = field(default_factory=_empty_ocr_summary)
    generation_id: str | None = None
    previous_generation_id: str | None = None


# 写后两路索引不一致（如向量清理静默失败、部分写）：标记入库失败而非误报成功。
class IndexInconsistencyError(Exception):
    pass


# 删库时部分代资源清理失败，保留清单以支持调用方重试。
class KBCleanupError(Exception):
    pass


# 需要重新解析的文档名（新增+内容改变）与需要从索引删除的文件名（删除+改变）。
@dataclass(frozen=True)
class IncrementalPlan:
    to_parse: list[str]
    removed_sources: set[str]


# 计算需要标记过期的旧文档绑定。
def _stale_bindings_from_document_changes(
    previous_documents: list[dict], current_documents: list[dict]
) -> list[tuple[str, str]]:
    previous = {
        str(doc.get("name")): str(doc.get("sha256"))
        for doc in previous_documents
        if doc.get("name") and doc.get("sha256")
    }
    current = {
        str(doc.get("name")): str(doc.get("sha256"))
        for doc in current_documents
        if doc.get("name") and doc.get("sha256")
    }
    return [
        (source, old_hash)
        for source, old_hash in sorted(previous.items())
        if current.get(source) != old_hash
    ]


# 标记派生知识过期。
def _mark_stale_derived_knowledge(
    kb_id: str,
    bindings: list[tuple[str, str]],
    knowledge_store: DerivedKnowledgeStore | None = None,
) -> int:
    if not bindings:
        return 0
    marked = 0
    store = knowledge_store if knowledge_store is not None else DerivedKnowledgeStore()
    for source, old_hash in bindings:
        marked += len(store.mark_stale_for_source(kb_id, source, old_hash))
    return marked


# 计算分块文本哈希。
def _chunk_text_hash(text: object) -> str:
    normalized = " ".join(("" if text is None else str(text)).split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


# 按来源组织新版分块。
def _chunks_by_source(chunks: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for chunk in chunks:
        raw_meta = chunk.get("meta")
        meta = raw_meta if isinstance(raw_meta, dict) else {}
        source = str(meta.get("source") or "")
        if source:
            grouped.setdefault(source, []).append(chunk)
    return grouped


# 寻找可自动重绑的新版分块。
def _auto_rebind_updates(row: dict, chunks: list[dict]) -> dict | None:
    expected_hash = str(row.get("related_chunk_text_hash") or "")
    anchor = str(row.get("related_anchor_text") or "").strip()
    if not expected_hash and not anchor:
        return None
    scored = []
    for chunk in chunks:
        text = str(chunk.get("text") or "")
        chunk_hash = _chunk_text_hash(text)
        matched_by_hash = bool(expected_hash and chunk_hash == expected_hash)
        matched_by_anchor = bool(anchor and anchor in text)
        if not matched_by_hash and not matched_by_anchor:
            continue
        raw_meta = chunk.get("meta")
        meta = raw_meta if isinstance(raw_meta, dict) else {}
        priority = 0 if matched_by_hash else 1
        scored.append((priority, chunk_hash, meta))
    if not scored:
        return None
    _, chunk_hash, meta = min(scored, key=lambda item: item[0])
    chunk_id = str(meta.get("chunk_id") or "")
    updates = {
        "related_source_sha256": meta.get("source_sha256"),
        "related_page_start": meta.get("page_start", meta.get("page")),
        "related_page_end": meta.get("page_end", meta.get("page")),
        "related_chunk_text_hash": chunk_hash,
        "related_anchor_text": anchor or row.get("related_anchor_text"),
    }
    if chunk_id:
        updates["related_chunk_ids"] = [chunk_id]
    return updates


# 复核文档变化后的派生知识。
def _review_changed_derived_knowledge(
    kb_id: str,
    bindings: list[tuple[str, str]],
    chunks: list[dict] | None = None,
    knowledge_store: DerivedKnowledgeStore | None = None,
) -> dict[str, int]:
    if not bindings:
        return {"stale": 0, "rebound": 0}
    if not chunks:
        return {
            "stale": _mark_stale_derived_knowledge(
                kb_id,
                bindings,
                knowledge_store=knowledge_store,
            ),
            "rebound": 0,
        }
    store = knowledge_store if knowledge_store is not None else DerivedKnowledgeStore()
    grouped = _chunks_by_source(chunks)
    stale = 0
    rebound = 0
    for source, old_hash in bindings:
        rows = [
            row
            for row in store.list(kb_id=kb_id, status="approved", document_id=source)
            if row.get("related_source") == source
            and row.get("related_source_sha256") == old_hash
        ]
        for row in rows:
            updates = _auto_rebind_updates(row, grouped.get(source, []))
            if updates:
                updated = store.set_status(
                    row["knowledge_id"],
                    "approved",
                    actor="system",
                    note=AUTO_REBIND_REVIEW_NOTE,
                    binding_updates=updates,
                )
                rebound += 1 if updated is not None else 0
            else:
                updated = store.set_status(
                    row["knowledge_id"],
                    "stale",
                    actor="system",
                    note="文档更新后未找到可自动重绑分块",
                )
                stale += 1 if updated is not None else 0
    return {"stale": stale, "rebound": rebound}


# 提交后尽力标记派生知识过期。
def _mark_stale_derived_knowledge_quiet(
    kb_id: str,
    bindings: list[tuple[str, str]],
    state: dict | None = None,
    chunks: list[dict] | None = None,
    knowledge_store: DerivedKnowledgeStore | None = None,
) -> None:
    try:
        reviewed = _review_changed_derived_knowledge(
            kb_id,
            bindings,
            chunks,
            knowledge_store=knowledge_store,
        )
        if reviewed["stale"] or reviewed["rebound"]:
            log_event(
                "ingest",
                "derived_knowledge_reviewed_after_update",
                state,
                kb_id=kb_id,
                stale=reviewed["stale"],
                rebound=reviewed["rebound"],
            )
    except Exception as exc:
        log_event(
            "ingest",
            "derived_knowledge_stale_mark_failed",
            state,
            level=logging.WARNING,
            kb_id=kb_id,
            error_class=type(exc).__name__,
        )


# 列出文档文件。
def list_pdf_files(source_dir: str) -> list[str]:
    """Compatibility name for the now format-neutral source directory scan."""

    if not os.path.isdir(source_dir):
        return []
    return list_supported_files(source_dir)


def _source_contracts(source_dir: str) -> dict[str, SourceDocument]:
    path = os.path.join(source_dir, _SOURCE_CONTRACTS_SIDECAR)
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError:
        return {}
    documents = payload.get("documents") if isinstance(payload, dict) else None
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != 1
        or not isinstance(documents, dict)
    ):
        raise IndexInconsistencyError("source contract sidecar is invalid")
    result: dict[str, SourceDocument] = {}
    for name, row in documents.items():
        if (
            not isinstance(name, str)
            or os.path.basename(name) != name
            or not isinstance(row, dict)
        ):
            raise IndexInconsistencyError(
                "source contract sidecar contains invalid rows"
            )
        document = SourceDocument.from_manifest_document(row)
        if document.display_name != name:
            raise IndexInconsistencyError(
                "source contract display name is inconsistent"
            )
        result[name] = document
    return result


def _merge_source_contracts(manifest: dict, source_dir: str) -> dict:
    contracts = _source_contracts(source_dir)
    if not contracts:
        return manifest
    merged = dict(manifest)
    rows = []
    for scanned in manifest.get("documents", []):
        name = str(scanned.get("name") or "")
        document = contracts.get(name)
        if document is None:
            rows.append(scanned)
            continue
        if document.version.content_sha256 != str(scanned.get("sha256") or ""):
            raise IndexInconsistencyError("source contract content hash is stale")
        row = document.to_manifest_document()
        row["name"] = name
        row["size"] = scanned.get("size")
        row["sha256"] = scanned.get("sha256")
        rows.append(row)
    merged["documents"] = rows
    return merged


# 使失效检索引擎缓存。
def _invalidate_engine_cache(kb_id: str) -> None:
    # 只失效本库引擎，避免命中旧引擎读取旧索引。
    RetrieverFactory.invalidate(kb_id)


# 移除清单。
def _remove_manifest(kb_id: str) -> None:
    manifest_file = manifest_path(kb_id)
    if os.path.exists(manifest_file):
        os.remove(manifest_file)


# 删除知识库索引。
def delete_kb_index(kb_id: str) -> None:
    # 删除索引时清理两路索引和清单，并与该库写操作串行。
    with kb_write_lock(kb_id):
        # 先推进纪元，拒绝删库前仍在飞的构建提交。
        shared_epoch_store().bump(kb_id)
        try:
            RetrieverFactory.get_engine(kb_id).clear()
        except Exception:
            _invalidate_engine_cache(kb_id)
            raise
        _remove_manifest(kb_id)
        _invalidate_engine_cache(kb_id)


# 清理索引代外部资源。
def _purge_generation_external(kb_id: str, gen_id: str) -> None:
    # 删库专用，只清理知识库目录外的向量集合和关键词索引文件。
    if has_readers(kb_id):
        raise KBCleanupError(f"KB {kb_id} 仍有在途读者，延后清理 generation {gen_id}")
    settings = get_settings()
    collection_id = settings.kb_collection_id(kb_id, gen_id)
    ok = True
    try:
        import chromadb

        chromadb.PersistentClient(path=settings.chroma_persist_dir).delete_collection(
            f"col-{collection_id}"
        )
    except ValueError:
        pass
    except Exception:
        ok = False
    bm25_path = os.path.join(settings.bm25_persist_dir, f"bm25_{collection_id}.pkl")
    try:
        os.remove(bm25_path)
    except FileNotFoundError:
        pass
    except Exception:
        ok = False
    if not ok:
        raise KBCleanupError(f"generation {gen_id} 外部资源未清理")


# 后台定时器注册表，统一在进程关闭时取消。
_active_timers: set = set()
_timers_lock = threading.Lock()


# 启动受跟踪的定时器。
def _start_tracked_timer(delay: float, fn, args=()) -> None:
    # 执行后台任务并完成收尾。
    def runner():
        try:
            fn(*args)
        finally:
            with _timers_lock:
                _active_timers.discard(t)

    t = threading.Timer(delay, runner)
    t.daemon = True
    with _timers_lock:
        _active_timers.add(t)
    try:
        t.start()
    except Exception:
        with _timers_lock:
            _active_timers.discard(t)
        raise


# 取消全部定时器。
def cancel_all_timers(join_timeout: float | None = 30.0) -> bool:
    # 关闭期取消未触发定时器，并有界等待已进入执行的任务。
    with _timers_lock:
        timers = list(_active_timers)
        _active_timers.clear()
    for t in timers:
        t.cancel()  # 未启动则取消；已在跑则 cancel 无效，靠下面 join 等其结束
    for t in timers:
        if t.ident is not None:
            t.join(timeout=join_timeout)
    return not any(t.is_alive() for t in timers)


# 排空清理队列。
def drain_purge_queue(now: float | None = None) -> int:
    # 重试清理队列中已到期的外部资源，成功才出队。
    queue = shared_purge_queue()
    done = 0
    for item in queue.due(now):
        try:
            _purge_generation_external(item["kb_id"], item["gen_id"])
            queue.remove(item["kb_id"], item["gen_id"])
            done += 1
        except Exception:
            pass  # 失败保留条目，下一轮 sweeper 重试
    return done


# 调度知识库清理。
def _schedule_kb_purge(kb_id: str, gen_ids: list) -> None:
    # 物理清理先入持久队列，并启动定时器促其尽快执行。
    not_before = time.time() + GENERATION_CLEANUP_DELAY_SECONDS
    for gen_id in gen_ids:
        shared_purge_queue().add(kb_id, gen_id, not_before)
    try:
        _start_tracked_timer(GENERATION_CLEANUP_DELAY_SECONDS, drain_purge_queue)
    except Exception as exc:
        # 队列已持久化，定时器只是低延迟优化。
        log_event(
            "purge",
            "purge_timer_start_failed",
            {},
            level=logging.ERROR,
            kb_id=kb_id,
            error_class=type(exc).__name__,
        )


# 事务化删除知识库索引。
def delete_kb_index_transactional(kb_id: str) -> None:
    # 逻辑删除同步完成，物理清理延迟执行以避开在途读取。
    with kb_write_lock(kb_id):
        shared_lifecycle_store().set(kb_id, LIFECYCLE_DELETING)
        shared_epoch_store().bump(kb_id)
        gen_ids = KBState(kb_id).generation_ids()
        _remove_manifest(kb_id)
        RetrieverFactory.invalidate(kb_id)
        _schedule_kb_purge(kb_id, gen_ids)


# 标记知识库已删除。
def mark_kb_deleted(kb_id: str) -> None:
    # 删库全流程成功后落删除标记，防止旧任务复活读写。
    shared_lifecycle_store().set(kb_id, LIFECYCLE_DELETED)


# 按名称映射文档。
def _documents_by_name(manifest: dict) -> dict[str, str]:
    return {doc["name"]: doc["sha256"] for doc in manifest.get("documents", [])}


# 规划增量构建。
def plan_incremental(previous: dict, current: dict) -> IncrementalPlan | None:
    # 无上一版、库标识或分块身份契约变化时交由全量重建。
    if not previous:
        return None
    if previous.get("doc_id") != current.get("doc_id"):
        return None
    # 构建版本覆盖分块身份、解析器、分词器和嵌入契约。
    if previous.get("index_build_version") != current.get("index_build_version"):
        return None

    prev = _documents_by_name(previous)
    cur = _documents_by_name(current)
    added = [name for name in cur if name not in prev]
    changed = [name for name in cur if name in prev and prev[name] != cur[name]]
    removed = [name for name in prev if name not in cur]

    # 按文件名删除旧分块，删除和改变的文档都清旧块。
    removed_sources = set(removed) | set(changed)
    return IncrementalPlan(sorted(added + changed), removed_sources)


# 解析并分块。
def _parse_and_chunk(
    source_dir: str,
    names: list[str],
    source_hash_by_name: dict[str, str],
    start_index: int = 0,
    ocr_summary: dict | None = None,
) -> tuple[list, list[IngestDocResult]]:
    all_chunks = []
    source_contracts = _source_contracts(source_dir)
    next_chunk_index = start_index
    doc_results = []
    for pdf in names:
        source_path = os.path.join(source_dir, pdf)
        source_document = source_contracts.get(pdf)
        pages = (
            smart_parse(source_path)
            if pdf.lower().endswith(".pdf") and source_document is None
            else parse_source(source_path, source_document=source_document)
        )
        if ocr_summary is not None:
            _merge_ocr_summary(ocr_summary, _summarize_ocr_pages(pages))
        chunks = chunk_paper(pages, source_sha256=source_hash_by_name[pdf])
        _log_chunking_summary(pdf, chunks)
        for chunk in chunks:
            # 展示编号仅用于界面，分块标识才是身份键。
            chunk["meta"]["chunk_index"] = next_chunk_index
            next_chunk_index += 1
        all_chunks.extend(chunks)
        doc_results.append(IngestDocResult(pdf, len(chunks)))
    return all_chunks, doc_results


# 校验一致性。
def _verify_consistent(engine) -> None:
    # 写后校验两路分块标识一致，避免残留旧块却报成功。
    if not engine.is_consistent():
        raise IndexInconsistencyError("index stores inconsistent after write")


# 处理全量重建。
def _full_rebuild(
    engine, kb_id, source_dir, pdf_files, manifest, source_hash_by_name
) -> IngestResult:
    ocr_summary = _empty_ocr_summary()
    all_chunks, doc_results = _parse_and_chunk(
        source_dir, pdf_files, source_hash_by_name, ocr_summary=ocr_summary
    )
    try:
        if all_chunks:
            engine.index(all_chunks)
            _verify_consistent(engine)
        else:
            # 有文档但没抽出任何分块时，必须显式清旧索引。
            engine.clear()
    except Exception:
        # 失败也驱逐被破坏的缓存引擎，下次入库自愈。
        _invalidate_engine_cache(kb_id)
        raise
    save_index_manifest(manifest)
    _invalidate_engine_cache(kb_id)
    return IngestResult(
        kb_id, len(pdf_files), len(all_chunks), doc_results, ocr_summary
    )


# 应用增量构建。
def _incremental_apply(
    engine, kb_id, source_dir, pdf_files, manifest, plan, source_hash_by_name
) -> IngestResult:
    # 新块从现存最大编号续号：保证唯一、单调，且不重编未变文档的展示编号。
    ocr_summary = _empty_ocr_summary()
    new_chunks, doc_results = _parse_and_chunk(
        source_dir,
        plan.to_parse,
        source_hash_by_name,
        start_index=engine.max_chunk_index() + 1,
        ocr_summary=ocr_summary,
    )
    try:
        if plan.to_parse or plan.removed_sources:
            engine.upsert_documents(new_chunks, plan.removed_sources)
            _verify_consistent(engine)
    except Exception:
        # 半更新时必须失效缓存，避免读到坏索引。
        _invalidate_engine_cache(kb_id)
        raise
    save_index_manifest(manifest)
    _invalidate_engine_cache(kb_id)
    # 分块数取索引现存总数，文档数取库内文档总数。
    return IngestResult(kb_id, len(pdf_files), engine.count(), doc_results, ocr_summary)


# 构建知识库索引。
def build_kb_index(kb_id: str, source_dir: str) -> IngestResult:
    # 取知识库写锁串行化整个入库，避免文件变化与索引写入交错。
    with kb_write_lock(kb_id):
        result = _build_kb_index_locked(kb_id, source_dir)
    _log_ocr_summary(result)
    return result


# 在锁内构建知识库索引。
def _build_kb_index_locked(kb_id: str, source_dir: str) -> IngestResult:
    engine = RetrieverFactory.get_engine(kb_id)
    pdf_files = list_pdf_files(source_dir)
    if not pdf_files:
        # 空库时清索引并删清单，避免重新加回相同文件时误判未变。
        try:
            engine.clear()
        except Exception:
            # 清理失败时不能删清单报成功，否则旧文档仍可被检索。
            _invalidate_engine_cache(kb_id)
            raise
        _remove_manifest(kb_id)
        _invalidate_engine_cache(kb_id)
        return IngestResult(kb_id, 0, 0, [])

    abs_dir = os.path.abspath(source_dir)
    if all(name.lower().endswith(".pdf") for name in pdf_files):
        rust_core = ensure_rust_core("scan_pdf_manifest_native")
        scanned_manifest = rust_core.scan_pdf_manifest_native(kb_id, abs_dir)
    else:
        scanned_manifest = scan_source_manifest(kb_id, abs_dir)
    scanned_manifest = _merge_source_contracts(scanned_manifest, abs_dir)
    manifest = stamp_index_build_version(
        stamp_source_document_contract(stamp_chunk_identity_contract(scanned_manifest))
    )
    source_hash_by_name = _documents_by_name(manifest)

    # 有可比对上一版且契约未变则增量，否则全量自愈。
    plan = plan_incremental(load_index_manifest(kb_id), manifest)
    if plan is None or not engine.is_consistent():
        return _full_rebuild(
            engine, kb_id, source_dir, pdf_files, manifest, source_hash_by_name
        )
    return _incremental_apply(
        engine, kb_id, source_dir, pdf_files, manifest, plan, source_hash_by_name
    )


# 事务化构建阶段。

# 旧代延迟回收时间，给在途请求留出持有旧引擎的窗口。
GENERATION_CLEANUP_DELAY_SECONDS = 60.0


# 处理硬链接快照。
def _hardlink_snapshot(source_dir: str, gen_dir: str, filenames: list[str]) -> None:
    # 源文件硬链接到索引代工作区，跨文件系统时退化为复制。
    os.makedirs(gen_dir, exist_ok=True)
    for name in filenames:
        src = os.path.join(source_dir, name)
        dst = os.path.join(gen_dir, name)
        try:
            os.link(src, dst)
        except OSError:
            shutil.copy2(src, dst)
    sidecar = os.path.join(source_dir, _SOURCE_CONTRACTS_SIDECAR)
    if os.path.isfile(sidecar):
        shutil.copy2(sidecar, os.path.join(gen_dir, _SOURCE_CONTRACTS_SIDECAR))


# 校验暂存区。
def _verify_staging(staging: HybridRetriever, all_chunks: list) -> None:
    # 暂存区入库后精确校验数量和分块标识集合。
    expected_count = len(all_chunks)
    actual_count = staging.count()
    if actual_count != expected_count:
        raise IndexInconsistencyError(
            f"staging count mismatch: expected {expected_count}, got {actual_count}"
        )
    expected_ids = {str(c["meta"]["chunk_id"]) for c in all_chunks}
    actual_ids = staging.chunk_ids()
    if actual_ids != expected_ids:
        missing = expected_ids - actual_ids
        extra = actual_ids - expected_ids
        raise IndexInconsistencyError(
            f"staging chunk_id mismatch: {len(missing)} missing, {len(extra)} extra"
        )
    # 增量复用时交叉核对两路分块标识，识破向量漏写。
    if not staging.is_consistent():
        raise IndexInconsistencyError("staging vector/bm25 chunk_id sets diverge")


# 清理索引代存储。
def _cleanup_generation_storage(kb_id: str, gen_id: str) -> None:
    # 回收单个非活跃索引代的外部资源和快照目录。
    with kb_write_lock(kb_id):
        if has_readers(kb_id):
            raise KBCleanupError(
                f"KB {kb_id} 仍有在途读者，延后清理 generation {gen_id}"
            )
        settings = get_settings()
        collection_id = settings.kb_collection_id(kb_id, gen_id)
        all_ok = True

        try:
            import chromadb

            chromadb.PersistentClient(
                path=settings.chroma_persist_dir
            ).delete_collection(f"col-{collection_id}")
        except ValueError:
            pass  # Chroma 对 not-found 抛 ValueError：集合已清，视为成功
        except Exception:
            all_ok = False

        bm25_path = os.path.join(settings.bm25_persist_dir, f"bm25_{collection_id}.pkl")
        try:
            os.remove(bm25_path)
        except FileNotFoundError:
            pass
        except Exception:
            all_ok = False

        gen_dir = settings.kb_generation_dir(kb_id, gen_id)
        if os.path.exists(gen_dir):
            try:
                shutil.rmtree(gen_dir)
            except Exception:
                all_ok = False

        if all_ok:
            # 全部资源清理成功后才移除状态记录。
            try:
                KBState(kb_id).remove_generation(gen_id)
            except Exception:
                all_ok = False

        # 任一资源未清理则向上抛出：调用方据此保留记录并报告可重试失败。
        if not all_ok:
            raise KBCleanupError(f"generation {gen_id} 资源未完全清理")


# 静默清理索引代存储。
def _cleanup_generation_storage_quiet(kb_id: str, gen_id: str) -> None:
    # 异步清理失败由下次扫描重试，不向上抛噪声。
    try:
        _cleanup_generation_storage(kb_id, gen_id)
    except Exception:
        pass


# 调度索引代清理。
def _schedule_generation_cleanup(kb_id: str, gen_id: str) -> None:
    # 延迟一段时间后异步清理旧代，避免影响在途检索。
    _start_tracked_timer(
        GENERATION_CLEANUP_DELAY_SECONDS,
        _cleanup_generation_storage_quiet,
        args=(kb_id, gen_id),
    )


# 构建暂存区检索引擎。
def _build_staging_engine(kb_id: str, gen_id: str) -> HybridRetriever:
    collection_id = get_settings().kb_collection_id(kb_id, gen_id)
    return HybridRetriever(
        vector_retriever=VectorRetriever(collection_id=collection_id),
        bm25_retriever=BM25Retriever(collection_id=collection_id),
    )


# 规划事务化增量构建。
def _plan_transactional_incremental(state: KBState, manifest: dict):
    # 以活跃索引代的文档清单作差异基准。
    prev_active = state.active()
    if not prev_active:
        return None, None
    prev_snapshot = {
        "doc_id": manifest.get("doc_id"),
        "index_build_version": prev_active.get("index_build_version"),
        "documents": prev_active.get("documents", []),
    }
    plan = plan_incremental(prev_snapshot, manifest)
    return (plan, prev_active) if plan is not None else (None, None)


# 增量填充暂存区。
def _fill_staging_incremental(
    kb_id,
    staging,
    prev_active,
    plan,
    gen_dir,
    source_hash_by_name,
    ocr_summary=None,
):
    if ocr_summary is None:
        ocr_summary = _empty_ocr_summary()

    # 复用上一代未变文档的分块和向量，只解析新增或改动文档。
    prev_collection_id = get_settings().kb_collection_id(kb_id, prev_active["id"])
    prev_vector = VectorRetriever(collection_id=prev_collection_id)
    prev_bm25 = BM25Retriever(collection_id=prev_collection_id)

    # 旧代两路分块标识集合必须相等且非空，并与提交数量吻合。
    embedding_by_id = prev_vector.embeddings_by_chunk_id()
    bm25_registry = prev_bm25.export_registry()
    bm25_ids = {str(d["meta"]["chunk_id"]) for d in bm25_registry}
    expected_prev = prev_active.get("expected_count")
    if not bm25_ids or set(embedding_by_id) != bm25_ids:
        raise IndexInconsistencyError("previous generation stores diverge")
    if isinstance(expected_prev, int) and len(bm25_ids) != expected_prev:
        raise IndexInconsistencyError("previous generation size mismatch")

    # 文本和元数据以关键词注册表为权威，复用前逐块校验内容自洽。
    active_hashes = {
        d.get("name"): d.get("sha256") for d in prev_active.get("documents", [])
    }
    drop = {str(s) for s in plan.removed_sources if s}
    reused_chunks, reused_embeddings = [], []
    for doc in bm25_registry:
        meta = doc["meta"]
        source = str(meta["source"])
        if source in drop:
            continue
        if active_hashes.get(source) != str(meta["source_sha256"]):
            raise IndexInconsistencyError("previous generation source/hash corrupt")
        expected_id = build_chunk_id(
            str(meta["source_sha256"]),
            source,
            int(meta["page_start"]),
            int(meta["page_end"]),
            int(meta["local_chunk_index"]),
        )
        if expected_id != str(meta["chunk_id"]):
            raise IndexInconsistencyError(
                "previous generation chunk_id/metadata mismatch"
            )
        reused_chunks.append(doc)
        reused_embeddings.append(embedding_by_id[str(meta["chunk_id"])])

    # 新块从复用块最大展示编号之后续号。
    start_index = (
        max((int(c["meta"]["chunk_index"]) for c in reused_chunks), default=-1) + 1
    )
    branch_summary = _empty_ocr_summary()
    new_chunks, doc_results = _parse_and_chunk(
        gen_dir,
        plan.to_parse,
        source_hash_by_name,
        start_index=start_index,
        ocr_summary=branch_summary,
    )
    all_chunks = reused_chunks + new_chunks
    if reused_chunks:
        staging.vector_retriever.add_with_embeddings(reused_chunks, reused_embeddings)
    if new_chunks:
        staging.vector_retriever.add_documents(new_chunks)
    staging.bm25_retriever.index(all_chunks)
    _merge_ocr_summary(ocr_summary, branch_summary)
    return all_chunks, doc_results


# 填充暂存区。
def _populate_staging(
    kb_id,
    state,
    gen_dir,
    pdf_files,
    manifest,
    source_hash_by_name,
    staging,
    ocr_summary=None,
):
    if ocr_summary is None:
        ocr_summary = _empty_ocr_summary()

    # 决定增量复用还是全量填充暂存区。
    plan, prev_active = _plan_transactional_incremental(state, manifest)
    if plan is not None:
        try:
            return _fill_staging_incremental(
                kb_id,
                staging,
                prev_active,
                plan,
                gen_dir,
                source_hash_by_name,
                ocr_summary,
            )
        except Exception as exc:
            # 复用失败时清空暂存区并回退全量重建。
            log_event(
                "ingest",
                "incremental_reuse_fallback",
                {},
                level=logging.WARNING,
                kb_id=kb_id,
                error_class=type(exc).__name__,
            )
            staging.clear()
    all_chunks, doc_results = _parse_and_chunk(
        gen_dir, pdf_files, source_hash_by_name, ocr_summary=ocr_summary
    )
    if all_chunks:
        staging.index(all_chunks)
    return all_chunks, doc_results


# 事务化构建知识库索引。
def build_kb_index_transactional(
    kb_id: str,
    source_dir: str,
    on_commit=None,
    *,
    knowledge_store: DerivedKnowledgeStore | None = None,
    retain_previous_generation: bool = False,
) -> IngestResult:
    # 取知识库写锁串行化写操作，提交前回调失败会中止提交。
    with kb_write_lock(kb_id):
        result = _build_transactional_locked(
            kb_id,
            source_dir,
            on_commit,
            knowledge_store=knowledge_store,
            retain_previous_generation=retain_previous_generation,
        )
    _log_ocr_summary(result)
    return result


# 在锁内事务化构建。
def _build_transactional_locked(
    kb_id: str,
    source_dir: str,
    on_commit=None,
    *,
    knowledge_store: DerivedKnowledgeStore | None = None,
    retain_previous_generation: bool = False,
) -> IngestResult:
    state = KBState(kb_id)
    pdf_files = list_pdf_files(source_dir)
    previous_active = state.active()
    previous_documents = (
        previous_active.get("documents", []) if previous_active is not None else []
    )

    if not pdf_files:
        return _transactional_empty(
            kb_id,
            state,
            on_commit,
            previous_documents,
            knowledge_store=knowledge_store,
            retain_previous_generation=retain_previous_generation,
        )

    abs_dir = os.path.abspath(source_dir)
    if all(name.lower().endswith(".pdf") for name in pdf_files):
        rust_core = ensure_rust_core("scan_pdf_manifest_native")
        scanned_manifest = rust_core.scan_pdf_manifest_native(kb_id, abs_dir)
    else:
        scanned_manifest = scan_source_manifest(kb_id, abs_dir)
    scanned_manifest = _merge_source_contracts(scanned_manifest, abs_dir)
    manifest = stamp_index_build_version(
        stamp_source_document_contract(stamp_chunk_identity_contract(scanned_manifest))
    )
    source_hash_by_name = _documents_by_name(manifest)

    gen_id = state.begin_generation(
        Embedder.MODEL_NAME,
        INDEX_BUILD_VERSION,
        CHUNK_IDENTITY_VERSION,
    )
    gen_dir = get_settings().kb_generation_dir(kb_id, gen_id)
    ocr_summary = _empty_ocr_summary()

    try:
        _hardlink_snapshot(source_dir, gen_dir, pdf_files)
        staging = _build_staging_engine(kb_id, gen_id)
        all_chunks, doc_results = _populate_staging(
            kb_id,
            state,
            gen_dir,
            pdf_files,
            manifest,
            source_hash_by_name,
            staging,
            ocr_summary,
        )
        if all_chunks:
            _verify_staging(staging, all_chunks)
        state.mark_ready(
            gen_id,
            expected_count=len(all_chunks),
            documents=manifest.get("documents", []),
        )
        # 提交前记录索引代，写失败则在切换活跃代前中止。
        if on_commit is not None:
            on_commit(gen_id)
        old_gen = state.switch_active(gen_id)  # 提交点：持有 kb_write_lock 保证原子性
    except Exception:
        # 各步独立容错，保证原始异常不被次级异常覆盖。
        try:
            state.mark_failed(gen_id)
        except Exception:
            pass
        try:
            _cleanup_generation_storage(kb_id, gen_id)
        except Exception:
            pass
        raise

    # 提交后尽力执行派生操作，失败不回滚也不向上抛。
    try:
        RetrieverFactory.invalidate(kb_id)
    except Exception:
        pass
    stale_bindings = _stale_bindings_from_document_changes(
        previous_documents, manifest.get("documents", [])
    )
    if knowledge_store is None:
        _mark_stale_derived_knowledge_quiet(
            kb_id,
            stale_bindings,
            chunks=all_chunks,
        )
    else:
        _mark_stale_derived_knowledge_quiet(
            kb_id,
            stale_bindings,
            chunks=all_chunks,
            knowledge_store=knowledge_store,
        )
    try:
        save_index_manifest(manifest)
    except Exception:
        pass
    if old_gen and not retain_previous_generation:
        try:
            _schedule_generation_cleanup(kb_id, old_gen)
        except Exception:
            pass

    return IngestResult(
        kb_id,
        len(pdf_files),
        len(all_chunks),
        doc_results,
        ocr_summary,
        generation_id=gen_id,
        previous_generation_id=old_gen,
    )


# 处理事务化空库。
def _transactional_empty(
    kb_id: str,
    state: KBState,
    on_commit=None,
    previous_documents: list[dict] | None = None,
    *,
    knowledge_store: DerivedKnowledgeStore | None = None,
    retain_previous_generation: bool = False,
) -> IngestResult:
    if previous_documents is None:
        active = state.active()
        previous_documents = active.get("documents", []) if active is not None else []
    gen_id = state.begin_generation(
        Embedder.MODEL_NAME,
        INDEX_BUILD_VERSION,
        CHUNK_IDENTITY_VERSION,
    )
    try:
        state.mark_ready(gen_id, expected_count=0, documents=[])
        if on_commit is not None:
            on_commit(gen_id)  # 提交前记录 gen_id，写失败则中止提交
        old_gen = state.switch_active(gen_id)  # 提交点
    except Exception:
        try:
            state.mark_failed(gen_id)
        except Exception:
            pass
        raise

    # 提交后尽力执行派生操作，失败不回滚也不向上抛。
    try:
        RetrieverFactory.invalidate(kb_id)
    except Exception:
        pass
    stale_bindings = _stale_bindings_from_document_changes(previous_documents, [])
    if knowledge_store is None:
        _mark_stale_derived_knowledge_quiet(kb_id, stale_bindings)
    else:
        _mark_stale_derived_knowledge_quiet(
            kb_id,
            stale_bindings,
            knowledge_store=knowledge_store,
        )
    try:
        _remove_manifest(kb_id)
    except Exception:
        pass
    if old_gen and not retain_previous_generation:
        try:
            _schedule_generation_cleanup(kb_id, old_gen)
        except Exception:
            pass
    return IngestResult(
        kb_id,
        0,
        0,
        [],
        generation_id=gen_id,
        previous_generation_id=old_gen,
    )
