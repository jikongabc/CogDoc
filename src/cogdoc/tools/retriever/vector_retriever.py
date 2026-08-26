import os
import chromadb
from collections.abc import Mapping, Sequence
from typing import Any, List, cast
from cogdoc.config.settings import get_settings
from cogdoc.graph.state import DocMeta, RetrievedDoc
from cogdoc.tools.chunk_identity import build_document_id
from cogdoc.tools.embedder import Embedder, embedding_contract
from cogdoc.tools.retriever.base_retriever import BaseRetriever
from cogdoc.tools.retriever.metadata import copy_optional_structure_metadata
from cogdoc.tools.retriever.retrieval_text import retrieval_text
from cogdoc.tools.retriever.scope import RetrievalScope


# 集合中记录的嵌入模型与当前系统模型不符：当前代不可用，需触发新代重建而非硬失败。
class EmbeddingModelMismatchError(RuntimeError):
    pass


# 初始化实例状态。
class VectorRetriever(BaseRetriever):
    def _embedding_backend(self):
        # Several low-level callers/tests construct a retriever with __new__ or
        # pass a collection stub. Preserve the historical local default when
        # no provider was installed by __init__.
        return vars(self).get("embedder", Embedder)

    # 初始化实例状态。
    def __init__(
        self,
        collection_id: str,
        persist_directory: str | None = None,
        *,
        embedder=None,
    ):
        persist_directory = persist_directory or get_settings().chroma_persist_dir
        self.embedder = embedder or Embedder
        self.embedding_contract = embedding_contract(self.embedder)
        os.makedirs(persist_directory, exist_ok=True)
        self.client = chromadb.PersistentClient(path=persist_directory)

        self.collection_name = f"col-{collection_id}"
        self._init_collection()

    # 完成 initcollection 处理。
    def _init_collection(self) -> None:
        # Chroma 集合名最长 60 字符；调用方必须保证 collection_name 合法，超长立即失败而非截断。
        if len(self.collection_name) > 60:
            raise ValueError(
                f"collection_name too long ({len(self.collection_name)} > 60): {self.collection_name!r}"
            )
        self.collection = self.client.get_or_create_collection(
            name=self.collection_name,
            metadata={"embedding_model": self.embedding_contract},
        )

        existing_meta = self.collection.metadata
        compatible_contracts = {self.embedding_contract}
        if self.embedder is Embedder:
            # Collections created before configurable providers stored only the
            # model alias. Existing local generations remain readable.
            compatible_contracts.add(Embedder.MODEL_NAME)
        if (
            existing_meta
            and existing_meta.get("embedding_model") not in compatible_contracts
        ):
            raise EmbeddingModelMismatchError(
                f"collection model={existing_meta.get('embedding_model')!r}, "
                f"requested contract={self.embedding_contract!r}"
            )

    # 检查存在性。
    def exists(self) -> bool:
        return self.collection.count() > 0

    # 统计数量。
    def count(self) -> int:
        return self.collection.count()

    # 切分 ids。
    def chunk_ids(self) -> set:
        # 一致性校验用：取全部主键（即 chunk_id），include=[] 只拉 id 不拉文档/向量。
        return set(self.collection.get(include=[])["ids"])

    # 清理。
    def clear(self) -> None:
        # 容忍「集合本就不存在」，但清理后必须确为空；残留旧块则抛错，不静默成功。
        try:
            self.client.delete_collection(name=self.collection_name)
        except Exception:
            pass
        self._init_collection()
        if self.collection.count() > 0:
            raise RuntimeError("vector index was not cleared")

    # 写入索引。
    def index(self, chunks: List[RetrievedDoc]) -> None:
        # 全量重建：先清后写。增量入库走 add_documents/delete_by_source。
        if not chunks:
            return
        self.clear()
        self._upsert_chunks(chunks)

    # 添加 documents。
    def add_documents(self, chunks: List[RetrievedDoc]) -> None:
        # 增量加入：按稳定 chunk_id upsert，不清空既有集合。
        if not chunks:
            return
        self._upsert_chunks(chunks)

    # 删除 by source。
    def delete_by_source(self, sources) -> None:
        # 按文件名删除其全部 chunk（删/改文档时清旧条目）；文件名是文档身份，区分同内容不同名。
        names = [s for s in {str(s) for s in sources} if s]
        if not names:
            return
        self.collection.delete(where=cast(Any, {"source": {"$in": names}}))

    # 展开结果。
    def _materialize(
        self, chunks: List[RetrievedDoc]
    ) -> tuple[list[str], list[dict[str, Any]], list[str]]:
        # 把 chunk 列表展开成 Chroma upsert 所需的 (ids, metadatas, texts)；主键直接用稳定 chunk_id。
        ids: list[str] = []
        metadatas: list[dict[str, Any]] = []
        texts: list[str] = []
        for c in chunks:
            meta = c["meta"]
            chunk_id = str(meta["chunk_id"])
            source = str(meta["source"])
            ids.append(chunk_id)
            texts.append(c["text"])
            stored_meta: dict[str, Any] = {
                "chunk_id": chunk_id,
                "document_id": str(
                    meta.get("document_id") or build_document_id(source)
                ),
                "source_sha256": meta["source_sha256"],
                "local_chunk_index": meta["local_chunk_index"],
                "chunk_index": meta["chunk_index"],
                "source": source,
                "page": meta["page"],
                "page_start": meta["page_start"],
                "page_end": meta["page_end"],
                "origin": meta.get("origin", "file"),
            }
            if meta.get("context"):
                stored_meta["context"] = str(meta["context"])
            copy_optional_structure_metadata(meta, stored_meta)
            metadatas.append(stored_meta)
        return ids, metadatas, texts

    # 增量写入分块列表。
    def _upsert_chunks(self, chunks: List[RetrievedDoc]) -> None:
        # 此路重新计算 embedding；跨代复用旧向量请走 add_with_embeddings 避免重算。
        embeddings = VectorRetriever._embedding_backend(self).embed_documents(
            [retrieval_text(c) for c in chunks]
        )
        ids, metadatas, texts = self._materialize(chunks)
        self.collection.upsert(
            ids=ids,
            embeddings=cast(Any, embeddings),
            documents=texts,
            metadatas=cast(Any, metadatas),
        )

    # 添加 with embeddings。
    def add_with_embeddings(self, chunks: List[RetrievedDoc], embeddings) -> None:
        # 带预算好的 embedding 写入：跨代增量复用上一代未变文档的向量时绝不重算。
        if not chunks:
            return
        # 写入前统一校验：chunk 与向量一一对应，且每个向量维度合法、数值有限，拒绝半截/污染数据。
        if len(chunks) != len(embeddings):
            raise ValueError(
                f"chunks/embeddings length mismatch: {len(chunks)} vs {len(embeddings)}"
            )
        VectorRetriever._embedding_backend(self).validate_embeddings(embeddings)
        ids, metadatas, texts = self._materialize(chunks)
        self.collection.upsert(
            ids=ids,
            embeddings=embeddings,
            documents=texts,
            metadatas=cast(Any, metadatas),
        )

    # 完成 嵌入向量by分块id 处理。
    def embeddings_by_chunk_id(self) -> dict:
        # 导出 {chunk_id: embedding}，供跨代复用按稳定 chunk_id 关联向量，绝不重算 embedding。 只提供向量，文本/metadata 权威另取自 BM25 registry，避免向量侧损坏被洗白。
        data = self.collection.get(include=["embeddings"])
        embeddings = data.get("embeddings")
        if embeddings is None:
            return {}
        return {str(chunk_id): embeddings[i] for i, chunk_id in enumerate(data["ids"])}

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
        # source allowlist 下推到 Chroma query，确保目标 source 在本通道
        # top-k 之前参与竞争，不能先取全库 top-k 再过滤。
        query_options: dict[str, Any] = {
            "query_embeddings": cast(
                Any, [VectorRetriever._embedding_backend(self).embed_query(query)]
            ),
            "n_results": top_k,
        }
        if scope is not None and scope.is_source_restricted:
            sources = list(scope.allowed_sources)
            query_options["where"] = cast(
                Any,
                {"source": sources[0]}
                if len(sources) == 1
                else {"source": {"$in": sources}},
            )
        # 返回结构保持与 BM25Retriever 一致。
        results = self.collection.query(**query_options)
        return self._materialize_search_row(results, 0)

    def search_many(
        self,
        queries: Sequence[str],
        top_k: int = 3,
        *,
        scope: RetrievalScope | None = None,
    ) -> List[List[RetrievedDoc]]:
        """Search several queries with one embedding batch and one Chroma call."""

        normalized_queries = [str(query) for query in queries]
        if not normalized_queries:
            return []
        if scope is not None and scope.denies_all:
            return [[] for _ in normalized_queries]

        query_options: dict[str, Any] = {
            "query_embeddings": cast(
                Any,
                VectorRetriever._embedding_backend(self).embed_queries(
                    normalized_queries
                ),
            ),
            "n_results": top_k,
        }
        if scope is not None and scope.is_source_restricted:
            sources = list(scope.allowed_sources)
            query_options["where"] = cast(
                Any,
                {"source": sources[0]}
                if len(sources) == 1
                else {"source": {"$in": sources}},
            )
        results = self.collection.query(**query_options)
        return [
            self._materialize_search_row(results, index)
            for index in range(len(normalized_queries))
        ]

    @staticmethod
    def _materialize_search_row(results: Any, row_index: int) -> List[RetrievedDoc]:
        if (
            not results
            or not results.get("documents")
            or row_index >= len(results["documents"])
            or not results["documents"][row_index]
        ):
            return []

        retrieved_docs: List[RetrievedDoc] = []
        docs = cast(Any, results["documents"])[row_index]
        ids = results["ids"][row_index]
        metas = cast(Any, results["metadatas"])[row_index]
        distances = (
            cast(Any, results["distances"])[row_index]
            if results.get("distances") is not None
            else [0.0] * len(ids)
        )

        for i in range(len(ids)):
            retrieved_docs.append(
                {
                    "text": docs[i],
                    "meta": _meta_from_stored(metas[i]),
                    "retrieval": {
                        "distance": float(distances[i]),
                        "search_channel": "vector",
                    },
                }
            )
        return retrieved_docs


# 完成 metafromstored 处理。
def _meta_from_stored(meta_data: Mapping[str, Any]) -> DocMeta:
    # 从 Chroma 存储元数据重建 chunk 身份元数据；缺 chunk_id 视为旧索引，必须重建而非现场拼回。
    chunk_id = meta_data.get("chunk_id")
    if not chunk_id:
        raise RuntimeError(
            "Vector index is missing stable chunk_id metadata; rebuild the index."
        )
    source = str(meta_data["source"])
    restored: dict[str, Any] = {
        "chunk_id": str(chunk_id),
        "document_id": str(
            meta_data.get("document_id") or build_document_id(source)
        ),
        "source_sha256": str(meta_data.get("source_sha256", "")),
        "local_chunk_index": int(meta_data["local_chunk_index"]),
        "chunk_index": int(meta_data["chunk_index"]),
        "source": source,
        "page": int(meta_data["page"]),
        "page_start": int(meta_data["page_start"]),
        "page_end": int(meta_data["page_end"]),
        "origin": str(meta_data["origin"]),
    }
    if meta_data.get("context"):
        restored["context"] = str(meta_data["context"])
    copy_optional_structure_metadata(meta_data, restored)
    return cast(DocMeta, restored)
