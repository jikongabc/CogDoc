import base64
import json
import os
import pickle
import copy
from threading import RLock
from typing import Any, List, cast
from cogdoc.config.settings import get_settings
from cogdoc.graph.state import DocMeta, RetrievedDoc
from cogdoc.service.durable_io import atomic_write_bytes
from cogdoc.tools.chunk_identity import build_document_id
from cogdoc.tools.document_loader import list_sources, load_source_chunks
from cogdoc.tools.tokenizer import tokenize_mixed_text, tokenize_corpus
from cogdoc.tools.rust_core_loader import ensure_rust_core
from cogdoc.tools.retriever.base_retriever import BaseRetriever
from cogdoc.tools.retriever.metadata import copy_optional_structure_metadata
from cogdoc.tools.retriever.retrieval_text import retrieval_text
from cogdoc.tools.retriever.scope import RetrievalScope


# BM25 计算与索引序列化均下放 rust_core，持久化只存 chunk 注册表 + 原生索引字节。
_rust_core = ensure_rust_core("Bm25Index")

# 落盘载荷格式标记；BM25 持久化结构变化时 bump，旧格式按无索引处理触发重建。
_PERSIST_FORMAT = "bm25_index_json_v2"
_LEGACY_PERSIST_FORMAT = "bm25_index_bytes_v1"


class _DataOnlyUnpickler(pickle.Unpickler):
    """Read the former primitive-only payload without permitting globals."""

    def find_class(self, module, name):
        raise pickle.UnpicklingError("pickle globals are disabled")

    def persistent_load(self, pid):
        raise pickle.UnpicklingError("pickle persistent ids are disabled")


# 初始化实例状态。
class BM25Retriever(BaseRetriever):
    # 初始化实例状态。
    def __init__(self, collection_id: str, persist_directory: str | None = None):
        persist_directory = persist_directory or get_settings().bm25_persist_dir
        os.makedirs(persist_directory, exist_ok=True)
        self.db_path = os.path.join(persist_directory, f"bm25_{collection_id}.pkl")

        # 保护 (bm25, doc_registry) 二元组的原子替换与一致快照读取；分词语料随索引存于 Rust 侧。
        self._lock = RLock()
        self._init_collection()

    # 完成 initcollection 处理。
    def _init_collection(self) -> None:
        self.bm25 = None
        self.doc_registry: List[RetrievedDoc] = []

        if os.path.exists(self.db_path):
            try:
                legacy = False
                try:
                    with open(self.db_path, encoding="utf-8") as f:
                        state = json.load(f)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    with open(self.db_path, "rb") as f:
                        state = _DataOnlyUnpickler(f).load()
                    legacy = True
                if not isinstance(state, dict):
                    raise ValueError("invalid bm25 persist payload")
                # 旧格式或缺索引字节按无索引处理：上层据空索引/版本不符触发重建。
                expected_format = (
                    _LEGACY_PERSIST_FORMAT if legacy else _PERSIST_FORMAT
                )
                if state.get("format") != expected_format:
                    raise ValueError("unsupported bm25 persist format")
                registry = state.get("doc_registry", [])
                encoded_index = state.get("index_base64")
                index_bytes = state.get("index_bytes") if legacy else None
                if not legacy and isinstance(encoded_index, str) and encoded_index:
                    index_bytes = base64.b64decode(encoded_index, validate=True)
                if registry and isinstance(index_bytes, bytes) and index_bytes:
                    self.bm25 = _rust_core.Bm25Index.from_bytes(index_bytes)
                    self.doc_registry = registry
                    if legacy:
                        # Migrate immediately; future starts never interpret
                        # even the restricted legacy pickle envelope.
                        self._persist(self.doc_registry, self.bm25)
            except Exception:
                self.bm25, self.doc_registry = None, []

    # 分词结果。
    def _tokenize(self, text: str) -> List[str]:
        return tokenize_mixed_text(text)

    # 完成 预热流程预热流程 处理。
    def warm_up(self) -> None:
        self._tokenize("知识库 检索 warmup")

    # 检查存在性。
    def exists(self) -> bool:
        return self.bm25 is not None and len(self.doc_registry) > 0

    # 统计数量。
    def count(self) -> int:
        return len(self.doc_registry)

    # 切分 ids。
    def chunk_ids(self) -> set:
        # 一致性校验用：registry 内全部 chunk_id。
        return {str(d["meta"]["chunk_id"]) for d in self.doc_registry}

    # 完成 max分块索引 处理。
    def max_chunk_index(self) -> int:
        # 增量续号用：现存最大展示编号，空索引返回 -1。
        return max(
            (int(d["meta"]["chunk_index"]) for d in self.doc_registry), default=-1
        )

    # 清理。
    def clear(self) -> None:
        # 容忍「文件本就不存在」，但清理后必须确为空；删除失败会让 _init 重载旧数据，借此识破。
        try:
            if os.path.exists(self.db_path):
                os.remove(self.db_path)
        except Exception:
            pass
        with self._lock:
            self._init_collection()
            if self.doc_registry:
                raise RuntimeError("bm25 index was not cleared")

    # 清理 doc。
    @staticmethod
    def _clean_doc(c: RetrievedDoc) -> RetrievedDoc:
        # 只留 chunk 身份元数据，去掉检索期临时字段。
        meta = c["meta"]
        source = str(meta["source"])
        cleaned_meta: dict[str, Any] = {
            "chunk_id": str(meta["chunk_id"]),
            "document_id": str(
                meta.get("document_id") or build_document_id(source)
            ),
            "source_sha256": str(meta["source_sha256"]),
            "local_chunk_index": int(meta["local_chunk_index"]),
            "chunk_index": int(meta["chunk_index"]),
            "source": source,
            "page": int(meta["page"]),
            "page_start": int(meta["page_start"]),
            "page_end": int(meta["page_end"]),
            "origin": str(meta.get("origin", "file")),
        }
        if meta.get("context"):
            cleaned_meta["context"] = str(meta["context"])
        copy_optional_structure_metadata(meta, cleaned_meta)
        return {
            "text": c["text"],
            "meta": cast(DocMeta, cleaned_meta),
        }

    # 持久化结果。
    def _persist(self, registry, index) -> None:
        # JSON + base64 is data-only and cannot execute constructors while
        # loading. Existing pickle generations fail closed as an empty index
        # and are rebuilt by the normal generation consistency path.
        payload = json.dumps(
            {
                "format": _PERSIST_FORMAT,
                "doc_registry": registry,
                "index_base64": base64.b64encode(index.to_bytes()).decode("ascii"),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        atomic_write_bytes(self.db_path, payload)

    # 切换in。
    def _swap_in(self, registry, index) -> None:
        # 先落盘再切内存：持久化失败时内存与磁盘不会错位（内存仍是旧的一致状态）。
        if registry and index is not None:
            self._persist(registry, index)
        else:
            registry, index = [], None
            try:
                if os.path.exists(self.db_path):
                    os.remove(self.db_path)
            except Exception:
                pass
        # 加锁原子替换二元组：并发 search 永远看到 index 与 registry 匹配的快照。
        with self._lock:
            self.bm25 = index
            self.doc_registry = registry

    # 写入索引。
    def index(self, chunks: List[RetrievedDoc]) -> None:
        # 全量重建：在局部构建后原子替换。增量入库走 upsert_documents。
        if not chunks:
            return
        registry = [self._clean_doc(c) for c in chunks]
        corpus = tokenize_corpus([retrieval_text(c) for c in chunks])
        self._swap_in(registry, _rust_core.Bm25Index(corpus))

    # 增量写入documents。
    def upsert_documents(self, new_chunks: List[RetrievedDoc], removed_sources) -> None:
        # 增量：保留未变文档（按文件名过滤），追加新 chunk，整体重建 Bm25Index。 BM25 分数依赖全局 IDF/avgdl 故必须整体重建，但未变文档的分词由 Rust 侧 corpus 复用，新 chunk 批量分词，均不回 Python 逐条切词。
        drop = {str(s) for s in removed_sources if s}
        with self._lock:
            registry = self.doc_registry
            base = self.bm25
        keep_indices = [
            i for i, doc in enumerate(registry) if doc["meta"]["source"] not in drop
        ]
        new_registry = [registry[i] for i in keep_indices]
        new_registry.extend(self._clean_doc(c) for c in new_chunks)
        new_tokens = tokenize_corpus([retrieval_text(c) for c in new_chunks])

        if not new_registry:
            self._swap_in([], None)
            return
        if base is not None:
            new_index = base.rebuild_from_kept(keep_indices, new_tokens)
        else:
            # 旧索引为空（首次增量或自愈后）：直接从新 chunk 分词构建。
            new_index = _rust_core.Bm25Index(new_tokens)
        self._swap_in(new_registry, new_index)

    # 导出 registry。
    def export_registry(self) -> List[RetrievedDoc]:
        # 跨代复用权威：返回 registry 深拷贝作文本/metadata 真值来源，向量按 chunk_id 关联。
        with self._lock:
            return copy.deepcopy(self.doc_registry)

    # 列出 sources。
    def list_sources(self) -> List[str]:
        return list_sources(self.doc_registry)

    # 加载 source chunks。
    def load_source_chunks(self, source: str) -> List[RetrievedDoc]:
        return load_source_chunks(self.doc_registry, source)

    # 检索。
    def search(
        self,
        query: str,
        top_k: int = 3,
        *,
        scope: RetrievalScope | None = None,
    ) -> List[RetrievedDoc]:
        if scope is not None and scope.denies_all:
            return []
        # 一致快照：bm25 与 registry 必须取自同一次原子替换，否则下标会错配到新 registry。
        with self._lock:
            bm25 = self.bm25
            registry = self.doc_registry
        if not bm25 or not registry:
            return []

        # BM25 分数只用于本路排序，融合分数由 RRF 写入。
        tokenized_query = self._tokenize(query)
        allowed_indices: list[int] | None = None
        if scope is not None and scope.is_source_restricted:
            allowed_indices = [
                index
                for index, doc in enumerate(registry)
                if scope.allows_source(doc.get("meta", {}).get("source"))
            ]
            if not allowed_indices:
                return []

        if allowed_indices is None:
            ranked = bm25.score_topk(tokenized_query, top_k)
        else:
            filtered_topk = getattr(bm25, "score_topk_filtered", None)
            if callable(filtered_topk):
                ranked = filtered_topk(tokenized_query, top_k, allowed_indices)
            else:
                # Compatibility with a stale native extension: rank the complete
                # registry, then filter.  This remains semantically exact because
                # all N rows (not only the global top-k) are inspected; the
                # bounded degradation is O(N log N) time/O(N) output from the
                # native call, capped by the current registry snapshot size.
                allowed = set(allowed_indices)
                ranked = [
                    item
                    for item in bm25.score_topk(tokenized_query, len(registry))
                    if item[0] in allowed
                ][:top_k]

        retrieved_docs: List[RetrievedDoc] = []
        for idx, score in ranked:
            if score <= 0:
                continue

            doc_copy = copy.deepcopy(registry[idx])
            meta_data = doc_copy["meta"]
            chunk_id = meta_data.get("chunk_id")
            if not chunk_id:
                # 旧索引缺少 chunk_id 时必须重建，不能现场拼回。
                raise RuntimeError(
                    "BM25 index is missing stable chunk_id metadata; rebuild the index."
                )
            source_sha256 = str(meta_data.get("source_sha256", ""))
            source = str(meta_data["source"])
            page_start = int(meta_data["page_start"])
            page_end = int(meta_data["page_end"])
            local_chunk_index = int(meta_data["local_chunk_index"])

            meta: dict[str, Any] = {
                "chunk_id": str(chunk_id),
                "document_id": str(
                    meta_data.get("document_id") or build_document_id(source)
                ),
                "source_sha256": source_sha256,
                "local_chunk_index": local_chunk_index,
                "chunk_index": int(meta_data["chunk_index"]),
                "source": source,
                "page": int(meta_data["page"]),
                "page_start": page_start,
                "page_end": page_end,
                "origin": str(meta_data["origin"]),
            }
            if meta_data.get("context"):
                meta["context"] = str(meta_data["context"])
            copy_optional_structure_metadata(meta_data, meta)
            retrieved_docs.append(
                {
                    "text": doc_copy["text"],
                    "meta": cast(DocMeta, meta),
                    "retrieval": {"bm25_score": float(score), "search_channel": "bm25"},
                }
            )
        return retrieved_docs
