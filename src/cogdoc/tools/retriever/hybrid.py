from collections.abc import Sequence
import logging
from typing import List

from cogdoc.config.settings import get_settings
from cogdoc.graph.state import RetrievedDoc
from cogdoc.observability.logger import log_event
from cogdoc.tools.retriever.base_retriever import BaseRetriever
from cogdoc.tools.retriever.scope import RetrievalScope
from cogdoc.tools.rust_core_loader import ensure_rust_core


# Hybrid 检索依赖 Rust RRF 融合函数。
rust_core = ensure_rust_core("rrf_fusion_native")


# 检索入口发现两路索引不一致（半更新/部分写）：宁可报错也不返回坏结果。
class IndexCorruptError(RuntimeError):
    pass


# 初始化实例状态。
class HybridRetriever(BaseRetriever):
    # 初始化实例状态。
    def __init__(
        self,
        vector_retriever: BaseRetriever,
        bm25_retriever: BaseRetriever,
        k: int = None,
    ):
        self.vector_retriever = vector_retriever  # 向量检索器
        self.bm25_retriever = bm25_retriever  # BM25检索器
        self.k = k if k is not None else get_settings().hybrid_rrf_k  # RRF平滑系数

    # 检查存在性。
    def exists(self) -> bool:
        return (
            self.vector_retriever.exists() and self.bm25_retriever.exists()
        )  # 两路索引均存在才视为可用

    # 清理。
    def clear(self) -> None:
        self.vector_retriever.clear()  # 清空向量索引
        self.bm25_retriever.clear()  # 清空BM25索引

    # 写入索引。
    def index(self, chunks: List[RetrievedDoc]) -> None:
        if not chunks:
            return
        self.vector_retriever.index(chunks)  # 构建向量索引
        self.bm25_retriever.index(chunks)  # 构建BM25索引

    # 增量写入documents。
    def upsert_documents(self, new_chunks: List[RetrievedDoc], removed_sources) -> None:
        # 增量入库：先删两路里删/改文档的旧 chunk，再加新 chunk。
        self.vector_retriever.delete_by_source(removed_sources)
        self.vector_retriever.add_documents(new_chunks)
        self.bm25_retriever.upsert_documents(new_chunks, removed_sources)

    # 统计数量。
    def count(self) -> int:
        # 以 BM25 registry 为权威 chunk 总数。
        return self.bm25_retriever.count()

    # 切分 ids。
    def chunk_ids(self) -> set:
        # 以 BM25 registry 为权威 chunk_id 集合；事务化构建后校验 staging 与预期完全吻合。
        return self.bm25_retriever.chunk_ids()

    # 判断 consistent 是否成立。
    def is_consistent(self) -> bool:
        # 两路 chunk_id 集合相等且非空才可增量；比数量更强，等量但内容不同/损坏也能识破，否则回退全量自愈。
        bm25_ids = self.bm25_retriever.chunk_ids()
        return bool(bm25_ids) and self.vector_retriever.chunk_ids() == bm25_ids

    # 判断 corrupt 是否成立。
    def is_corrupt(self) -> bool:
        # 检索热路径廉价校验：两路数量不等=半更新/部分写损坏；两路皆空属正常空库，不算损坏。
        return self.vector_retriever.count() != self.bm25_retriever.count()

    # 确保servable。
    def _ensure_servable(self) -> None:
        if self.is_corrupt():
            raise IndexCorruptError(
                "retrieval index stores are inconsistent; rebuild required"
            )

    @staticmethod
    def _log_route_failure(channel: str, exc: Exception) -> None:
        log_event(
            "retrieval",
            "retrieval_route_failed",
            {},
            level=logging.WARNING,
            channel=channel,
            error_class=type(exc).__name__,
        )

    def _search_one_route(
        self,
        retriever: BaseRetriever,
        channel: str,
        query: str,
        top_k: int,
        scope: RetrievalScope | None,
    ) -> List[RetrievedDoc]:
        try:
            docs = (
                retriever.search(query, top_k=top_k)
                if scope is None
                else retriever.search(query, top_k=top_k, scope=scope)
            )
        except Exception as exc:
            self._log_route_failure(channel, exc)
            return []
        return list(docs)

    # 完成 max分块索引 处理。
    def max_chunk_index(self) -> int:
        # 增量续号用：现存最大展示编号。
        return self.bm25_retriever.max_chunk_index()

    # 列出 sources。
    def list_sources(self) -> List[str]:
        # 单文档摘要从 BM25 registry 读取完整文档列表。
        self._ensure_servable()
        return self.bm25_retriever.list_sources()

    # 加载 source chunks。
    def load_source_chunks(self, source: str) -> List[RetrievedDoc]:
        # 单文档摘要加载指定 source 的全部 chunk。
        self._ensure_servable()
        return self.bm25_retriever.load_source_chunks(source)

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
        # 两路召回后交给 native RRF 做去重融合。
        channels = self.search_channels(query, top_k=top_k, scope=scope)

        return rust_core.rrf_fusion_native(
            channels["vector"], channels["bm25"], float(self.k), top_k
        )

    def search_channels(
        self,
        query: str,
        top_k: int = 3,
        *,
        scope: RetrievalScope | None = None,
    ) -> dict[str, List[RetrievedDoc]]:
        """Return dense and lexical rankings before fusion.

        The public ``search`` method remains backward compatible.  Retrieval
        orchestration can use this method to weight, trace and degrade the two
        independent routes without losing their provenance in an early merge.
        """

        if scope is not None and scope.denies_all:
            return {"vector": [], "bm25": []}
        self._ensure_servable()
        recall_top_k = max(0, top_k) * 3
        vector_results = self._search_one_route(
            self.vector_retriever,
            "rag_vector",
            query,
            recall_top_k,
            scope,
        )
        bm25_results = self._search_one_route(
            self.bm25_retriever,
            "rag_bm25",
            query,
            recall_top_k,
            scope,
        )
        return {"vector": list(vector_results), "bm25": list(bm25_results)}

    def search_many(
        self,
        queries: Sequence[str],
        top_k: int = 3,
        *,
        scope: RetrievalScope | None = None,
    ) -> List[List[RetrievedDoc]]:
        """Batch the vector side while preserving per-query BM25/RRF semantics."""

        channel_rows = self.search_many_channels(queries, top_k=top_k, scope=scope)
        return [
            rust_core.rrf_fusion_native(
                row["vector"],
                row["bm25"],
                float(self.k),
                top_k,
            )
            for row in channel_rows
        ]

    def search_many_channels(
        self,
        queries: Sequence[str],
        top_k: int = 3,
        *,
        scope: RetrievalScope | None = None,
    ) -> List[dict[str, List[RetrievedDoc]]]:
        """Batch dense retrieval and preserve one ranking per recall route."""

        normalized_queries = [str(query) for query in queries]
        if not normalized_queries:
            return []
        if scope is not None and scope.denies_all:
            return [{"vector": [], "bm25": []} for _ in normalized_queries]
        self._ensure_servable()
        recall_top_k = max(0, top_k) * 3

        vector_search_many = getattr(self.vector_retriever, "search_many", None)
        if callable(vector_search_many):
            try:
                vector_rankings = (
                    vector_search_many(normalized_queries, top_k=recall_top_k)
                    if scope is None
                    else vector_search_many(
                        normalized_queries, top_k=recall_top_k, scope=scope
                    )
                )
                if len(vector_rankings) != len(normalized_queries):
                    raise RuntimeError(
                        "vector search_many returned an invalid row count"
                    )
            except Exception as exc:
                self._log_route_failure("rag_vector", exc)
                vector_rankings = [
                    self._search_one_route(
                        self.vector_retriever,
                        "rag_vector",
                        query,
                        recall_top_k,
                        scope,
                    )
                    for query in normalized_queries
                ]
        else:
            vector_rankings = [
                self._search_one_route(
                    self.vector_retriever,
                    "rag_vector",
                    query,
                    recall_top_k,
                    scope,
                )
                for query in normalized_queries
            ]

        bm25_rankings = [
            self._search_one_route(
                self.bm25_retriever,
                "rag_bm25",
                query,
                recall_top_k,
                scope,
            )
            for query in normalized_queries
        ]
        return [
            {"vector": list(vector_docs), "bm25": list(bm25_docs)}
            for vector_docs, bm25_docs in zip(
                vector_rankings, bm25_rankings, strict=True
            )
        ]
