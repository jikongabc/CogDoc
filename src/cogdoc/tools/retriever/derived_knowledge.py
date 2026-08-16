from collections import Counter
import hashlib
import json
import logging
import os
from collections.abc import Sequence
from typing import Any, Callable

from cogdoc.api.derived_knowledge_store import DerivedKnowledgeStore
from cogdoc.config.settings import get_settings
from cogdoc.graph.state import RetrievedDoc
from cogdoc.observability.logger import log_event
from cogdoc.tools.retriever.retrieval_text import retrieval_text
from cogdoc.tools.retriever.scope import RetrievalScope
from cogdoc.tools.tokenizer import tokenize_mixed_text


def _int_or_zero(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _embedder():
    from cogdoc.tools.embedder import Embedder

    return Embedder


def _collection_name(kb_id: str) -> str:
    digest = hashlib.sha256(kb_id.encode("utf-8")).hexdigest()[:24]
    return f"dk-{digest}"


def _state_name(kb_id: str) -> str:
    digest = hashlib.sha256(kb_id.encode("utf-8")).hexdigest()
    return f"{digest}.json"


def _json_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, str) and value.strip():
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return []
        if isinstance(decoded, list):
            return [str(item) for item in decoded]
    return []


def _knowledge_doc(
    row: dict[str, Any],
    *,
    score: float,
    rank: int,
    explanation: dict[str, Any],
    search_channel: str,
) -> RetrievedDoc:
    knowledge_id = str(row.get("knowledge_id") or "")
    related_source = str(row.get("related_source") or "")
    chunk_ids = _json_list(row.get("related_chunk_ids"))
    page_start = _int_or_zero(row.get("related_page_start"))
    page_end = _int_or_zero(row.get("related_page_end"))
    meta = {
        "chunk_id": f"knowledge:{knowledge_id}",
        "knowledge_id": knowledge_id,
        "kb_id": str(row.get("kb_id") or ""),
        "version": _int_or_zero(row.get("version")) or 1,
        "source_sha256": str(row.get("related_source_sha256") or ""),
        "local_chunk_index": rank,
        "chunk_index": rank,
        "source": f"knowledge:{knowledge_id}",
        "page": page_start,
        "page_start": page_start,
        "page_end": page_end,
        "origin": str(row.get("origin") or "manual_entry"),
        "source_type": "derived_knowledge",
        "status": str(row.get("status") or ""),
        "certainty": str(row.get("certainty") or ""),
        "related_document_id": str(row.get("related_document_id") or ""),
        "related_source": related_source,
        "related_chunk_ids": chunk_ids,
        "related_page_start": page_start,
        "related_page_end": page_end,
        "related_chunk_text_hash": str(row.get("related_chunk_text_hash") or ""),
        "related_anchor_text": str(row.get("related_anchor_text") or ""),
    }
    if row.get("source_note"):
        meta["context"] = str(row["source_note"])
    return {
        "text": str(row.get("text") or ""),
        "meta": meta,
        "retrieval": {
            **explanation,
            "knowledge_score": score,
            "retrieval_score": score,
            "search_channel": search_channel,
            "status_filter": "approved",
        },
    }


def _stored_meta(row: dict[str, Any], rank: int) -> dict[str, str | int]:
    doc = _knowledge_doc(
        row,
        score=0.0,
        rank=rank,
        explanation={},
        search_channel="derived_knowledge_embedding",
    )
    meta = doc["meta"]
    stored = {
        "knowledge_id": str(meta.get("knowledge_id") or ""),
        "kb_id": str(meta.get("kb_id") or ""),
        "version": int(meta.get("version") or 1),
        "chunk_id": str(meta.get("chunk_id") or ""),
        "source_sha256": str(meta.get("source_sha256") or ""),
        "local_chunk_index": int(meta.get("local_chunk_index") or 0),
        "chunk_index": int(meta.get("chunk_index") or 0),
        "source": str(meta.get("source") or ""),
        "page": int(meta.get("page") or 0),
        "page_start": int(meta.get("page_start") or 0),
        "page_end": int(meta.get("page_end") or 0),
        "origin": str(meta.get("origin") or "manual_entry"),
        "source_type": "derived_knowledge",
        "status": str(meta.get("status") or ""),
        "certainty": str(meta.get("certainty") or ""),
        "related_document_id": str(meta.get("related_document_id") or ""),
        "related_source": str(meta.get("related_source") or ""),
        "related_chunk_ids": json.dumps(
            meta.get("related_chunk_ids") or [], ensure_ascii=False
        ),
        "related_page_start": int(meta.get("related_page_start") or 0),
        "related_page_end": int(meta.get("related_page_end") or 0),
        "related_chunk_text_hash": str(meta.get("related_chunk_text_hash") or ""),
        "related_anchor_text": str(meta.get("related_anchor_text") or ""),
    }
    if meta.get("context"):
        stored["context"] = str(meta["context"])
    return stored


def _row_from_stored(text: str, meta: dict[str, Any]) -> dict[str, Any]:
    return {
        "knowledge_id": str(meta.get("knowledge_id") or ""),
        "kb_id": str(meta.get("kb_id") or ""),
        "version": _int_or_zero(meta.get("version")) or 1,
        "text": text,
        "related_source_sha256": str(meta.get("source_sha256") or ""),
        "related_document_id": str(meta.get("related_document_id") or ""),
        "related_source": str(meta.get("related_source") or ""),
        "related_chunk_ids": _json_list(meta.get("related_chunk_ids")),
        "related_page_start": _int_or_zero(meta.get("related_page_start")),
        "related_page_end": _int_or_zero(meta.get("related_page_end")),
        "related_chunk_text_hash": str(meta.get("related_chunk_text_hash") or ""),
        "related_anchor_text": str(meta.get("related_anchor_text") or ""),
        "source_note": str(meta.get("context") or ""),
        "certainty": str(meta.get("certainty") or ""),
        "status": str(meta.get("status") or ""),
        "origin": str(meta.get("origin") or "manual_entry"),
    }


class DerivedKnowledgeIndex:
    def __init__(
        self,
        store: DerivedKnowledgeStore | None = None,
        *,
        persist_directory: str | None = None,
        state_directory: str | None = None,
    ):
        settings = get_settings()
        self.store = store or DerivedKnowledgeStore()
        self.persist_directory = persist_directory or settings.chroma_persist_dir
        self.state_directory = state_directory or str(
            settings.data_dir / "knowledge" / "derived_index_state"
        )
        os.makedirs(self.persist_directory, exist_ok=True)
        os.makedirs(self.state_directory, exist_ok=True)
        import chromadb

        self.client = chromadb.PersistentClient(path=self.persist_directory)
        self.embedder = _embedder()

    def ensure_fresh(self, kb_id: str) -> None:
        token = self.store.revision_token()
        state = self._read_state(kb_id)
        if (
            state.get("revision_token") == token
            and state.get("embedding_contract")
            == self.embedder.EMBEDDING_CONTRACT_VERSION
        ):
            expected_count = _int_or_zero(state.get("count"))
            if expected_count <= 0 or self._collection(kb_id).count() > 0:
                return
        self.rebuild(kb_id, revision_token=token)

    def rebuild(self, kb_id: str, *, revision_token: str | None = None) -> None:
        revision_token = revision_token or self.store.revision_token()
        rows = self.store.list(kb_id=kb_id, status="approved")
        collection = self._reset_collection(kb_id)
        if rows:
            docs = [
                _knowledge_doc(
                    row,
                    score=0.0,
                    rank=index,
                    explanation={},
                    search_channel="derived_knowledge_embedding",
                )
                for index, row in enumerate(rows)
            ]
            texts = [str(doc.get("text") or "") for doc in docs]
            vector_texts = [retrieval_text(doc) for doc in docs]
            embeddings = self.embedder.embed_documents(vector_texts)
            collection.upsert(
                ids=[str(row["knowledge_id"]) for row in rows],
                embeddings=embeddings,
                documents=texts,
                metadatas=[_stored_meta(row, index) for index, row in enumerate(rows)],
            )
        self._write_state(
            kb_id,
            {
                "revision_token": revision_token,
                "embedding_contract": self.embedder.EMBEDDING_CONTRACT_VERSION,
                "count": len(rows),
            },
        )

    def status(self, kb_id: str) -> dict[str, Any]:
        state = self._read_state(kb_id)
        current_token = self.store.revision_token()
        current_contract = self.embedder.EMBEDDING_CONTRACT_VERSION
        approved_count = len(self.store.list(kb_id=kb_id, status="approved"))
        indexed_count, collection_error = self._collection_count(kb_id)
        state_count = _int_or_zero(state.get("count"))
        is_fresh = (
            state.get("revision_token") == current_token
            and state.get("embedding_contract") == current_contract
            and state_count == approved_count
            and indexed_count == approved_count
        )
        if collection_error:
            state_label = "error"
        elif is_fresh:
            state_label = "fresh"
        elif state:
            state_label = "stale"
        else:
            state_label = "missing"
        return {
            "kb_id": kb_id,
            "state": state_label,
            "current_revision_token": current_token,
            "indexed_revision_token": state.get("revision_token"),
            "embedding_contract": current_contract,
            "indexed_embedding_contract": state.get("embedding_contract"),
            "approved_count": approved_count,
            "indexed_count": indexed_count,
            "state_count": state_count,
            "collection_name": _collection_name(kb_id),
            "collection_error": collection_error,
            "last_error": state.get("last_error"),
        }

    def record_error(self, kb_id: str, error_class: str) -> None:
        state = self._read_state(kb_id)
        state["last_error"] = error_class
        self._write_state(kb_id, state)

    def search(
        self,
        kb_id: str,
        query: str,
        top_k: int,
        *,
        scope: RetrievalScope | None = None,
    ) -> list[RetrievedDoc]:
        if scope is not None and (
            scope.denies_all or not scope.include_derived_knowledge
        ):
            return []
        rows = self.search_many(kb_id, [query], top_k, scope=scope)
        return rows[0] if rows else []

    def search_many(
        self,
        kb_id: str,
        queries: Sequence[str],
        top_k: int,
        *,
        scope: RetrievalScope | None = None,
    ) -> list[list[RetrievedDoc]]:
        if not queries:
            return []
        if scope is not None and (
            scope.denies_all or not scope.include_derived_knowledge
        ):
            return [[] for _ in queries]
        collection = self._collection(kb_id)
        if collection.count() <= 0 or top_k <= 0:
            return [[] for _ in queries]
        embed_many = getattr(self.embedder, "embed_queries", None)
        embeddings = (
            embed_many(list(queries))
            if callable(embed_many)
            else [self.embedder.embed_query(query) for query in queries]
        )
        query_options: dict[str, Any] = {
            "query_embeddings": embeddings,
            "n_results": top_k,
        }
        if scope is not None and scope.is_source_restricted:
            sources = list(scope.allowed_sources)
            query_options["where"] = (
                {"related_source": sources[0]}
                if len(sources) == 1
                else {"related_source": {"$in": sources}}
            )
        results = collection.query(**query_options)
        document_rows = results.get("documents") if results else None
        metadata_rows = results.get("metadatas") if results else None
        distance_rows = results.get("distances") if results else None
        output: list[list[RetrievedDoc]] = []
        for index in range(len(queries)):
            docs = (
                document_rows[index]
                if document_rows and index < len(document_rows)
                else []
            )
            metas = (
                metadata_rows[index]
                if metadata_rows and index < len(metadata_rows)
                else []
            )
            distances = (
                distance_rows[index]
                if distance_rows and index < len(distance_rows)
                else [0.0] * len(docs)
            )
            output.append(self._materialize_search_row(docs, metas, distances))
        return output

    @staticmethod
    def _materialize_search_row(
        docs: Sequence[Any], metas: Sequence[dict[str, Any]], distances: Sequence[Any]
    ) -> list[RetrievedDoc]:
        retrieved: list[RetrievedDoc] = []
        for rank, text in enumerate(docs):
            distance = float(distances[rank])
            score = 1.0 / (1.0 + max(distance, 0.0))
            row = _row_from_stored(str(text or ""), metas[rank])
            retrieved.append(
                _knowledge_doc(
                    row,
                    score=score,
                    rank=rank,
                    explanation={"distance": distance},
                    search_channel="derived_knowledge_embedding",
                )
            )
        return retrieved

    def _collection(self, kb_id: str):
        return self.client.get_or_create_collection(
            name=_collection_name(kb_id),
            metadata={
                "source": "derived_knowledge",
                "embedding_contract": self.embedder.EMBEDDING_CONTRACT_VERSION,
            },
        )

    def _reset_collection(self, kb_id: str):
        name = _collection_name(kb_id)
        try:
            self.client.delete_collection(name)
        except Exception:
            collection = self._collection(kb_id)
            ids = collection.get(include=[]).get("ids") or []
            if ids:
                collection.delete(ids=ids)
            return collection
        return self._collection(kb_id)

    def _collection_count(self, kb_id: str) -> tuple[int, str | None]:
        name = _collection_name(kb_id)
        try:
            names = {
                str(getattr(collection, "name", collection))
                for collection in self.client.list_collections()
            }
            if name not in names:
                return 0, None
            return int(self.client.get_collection(name).count()), None
        except Exception as exc:
            return 0, type(exc).__name__

    def _state_path(self, kb_id: str) -> str:
        return os.path.join(self.state_directory, _state_name(kb_id))

    def _read_state(self, kb_id: str) -> dict[str, Any]:
        path = self._state_path(kb_id)
        if not os.path.exists(path):
            return {}
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write_state(self, kb_id: str, data: dict[str, Any]) -> None:
        with open(self._state_path(kb_id), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, sort_keys=True)


# 派生知识召回器，只读取已审核知识，不触碰原始文档索引。
class DerivedKnowledgeRetriever:
    def __init__(
        self,
        store: DerivedKnowledgeStore | None = None,
        *,
        index: DerivedKnowledgeIndex | None = None,
        index_factory: Callable[[], DerivedKnowledgeIndex] | None = None,
        enable_index: bool | None = None,
    ):
        if index is not None and index_factory is not None:
            raise ValueError("index and index_factory cannot both be provided")
        self.store = store or DerivedKnowledgeStore()
        self._index_factory = index_factory
        if index is not None:
            self._index = index
            self._index_enabled = True
        elif index_factory is not None:
            self._index = None
            self._index_enabled = enable_index is not False
        elif enable_index is False or (enable_index is None and store is not None):
            self._index = None
            self._index_enabled = False
        else:
            self._index = None
            self._index_enabled = True

    # 搜索已审核派生知识。
    def search(
        self,
        kb_id: str,
        query: str,
        top_k: int = 3,
        *,
        scope: RetrievalScope | None = None,
    ) -> list[RetrievedDoc]:
        if scope is not None and (
            scope.denies_all or not scope.include_derived_knowledge
        ):
            return []
        index = self._index_or_none()
        if index is not None:
            try:
                index.ensure_fresh(kb_id)
                docs = (
                    index.search(kb_id, query, top_k)
                    if scope is None
                    else index.search(kb_id, query, top_k, scope=scope)
                )
            except Exception as exc:
                log_event(
                    "retrieval",
                    "derived_knowledge_index_failed",
                    {},
                    level=logging.WARNING,
                    kb_id=kb_id,
                    error_class=type(exc).__name__,
                )
            else:
                if docs:
                    return docs
        return self._lexical_search(kb_id, query, top_k, scope=scope)

    def search_many(
        self,
        kb_id: str,
        queries: Sequence[str],
        top_k: int = 3,
        *,
        scope: RetrievalScope | None = None,
    ) -> list[list[RetrievedDoc]]:
        if not queries:
            return []
        if scope is not None and (
            scope.denies_all or not scope.include_derived_knowledge
        ):
            return [[] for _ in queries]
        indexed_rows: list[list[RetrievedDoc]] | None = None
        index = self._index_or_none()
        if index is not None:
            try:
                index.ensure_fresh(kb_id)
                search_many = getattr(index, "search_many", None)
                if callable(search_many):
                    indexed_rows = (
                        search_many(kb_id, queries, top_k)
                        if scope is None
                        else search_many(kb_id, queries, top_k, scope=scope)
                    )
                else:
                    indexed_rows = [
                        (
                            index.search(kb_id, query, top_k)
                            if scope is None
                            else index.search(kb_id, query, top_k, scope=scope)
                        )
                        for query in queries
                    ]
                if len(indexed_rows) != len(queries):
                    raise RuntimeError("derived index returned an invalid row count")
            except Exception as exc:
                log_event(
                    "retrieval",
                    "derived_knowledge_index_failed",
                    {},
                    level=logging.WARNING,
                    kb_id=kb_id,
                    error_class=type(exc).__name__,
                )
                indexed_rows = None
        return [
            row if row else self._lexical_search(kb_id, query, top_k, scope=scope)
            for query, row in zip(
                queries,
                indexed_rows if indexed_rows is not None else [[] for _ in queries],
            )
        ]

    def search_channels(
        self,
        kb_id: str,
        query: str,
        top_k: int = 3,
        *,
        scope: RetrievalScope | None = None,
    ) -> dict[str, list[RetrievedDoc]]:
        """Return embedding and lexical rankings as independent routes."""

        rows = self.search_many_channels(
            kb_id,
            [query],
            top_k=top_k,
            scope=scope,
        )
        return rows[0] if rows else {"embedding": [], "lexical": []}

    def search_many_channels(
        self,
        kb_id: str,
        queries: Sequence[str],
        top_k: int = 3,
        *,
        scope: RetrievalScope | None = None,
    ) -> list[dict[str, list[RetrievedDoc]]]:
        """Run both approved-knowledge routes, with independent degradation.

        Lexical retrieval is intentionally executed even when the embedding
        index succeeds: exact names, codes and policy terms are complementary
        to semantic similarity and should participate in final fusion.
        """

        if not queries:
            return []
        if scope is not None and (
            scope.denies_all or not scope.include_derived_knowledge
        ):
            return [{"embedding": [], "lexical": []} for _ in queries]

        embedding_rows: list[list[RetrievedDoc]] = [[] for _ in queries]
        index = self._index_or_none()
        if index is not None:
            try:
                index.ensure_fresh(kb_id)
                search_many = getattr(index, "search_many", None)
                if callable(search_many):
                    embedding_rows = (
                        search_many(kb_id, queries, top_k)
                        if scope is None
                        else search_many(kb_id, queries, top_k, scope=scope)
                    )
                else:
                    embedding_rows = [
                        (
                            index.search(kb_id, query, top_k)
                            if scope is None
                            else index.search(kb_id, query, top_k, scope=scope)
                        )
                        for query in queries
                    ]
                if len(embedding_rows) != len(queries):
                    raise RuntimeError("derived index returned an invalid row count")
            except Exception as exc:
                log_event(
                    "retrieval",
                    "derived_knowledge_index_failed",
                    {},
                    level=logging.WARNING,
                    kb_id=kb_id,
                    error_class=type(exc).__name__,
                )
                embedding_rows = [[] for _ in queries]

        return [
            {
                "embedding": list(embedding_docs),
                "lexical": self._lexical_search(
                    kb_id,
                    query,
                    top_k,
                    scope=scope,
                ),
            }
            for query, embedding_docs in zip(queries, embedding_rows, strict=True)
        ]

    def _index_or_none(self) -> DerivedKnowledgeIndex | None:
        if not self._index_enabled:
            return None
        if self._index is None:
            self._index = (
                self._index_factory()
                if self._index_factory is not None
                else DerivedKnowledgeIndex(self.store)
            )
        return self._index

    def _lexical_search(
        self,
        kb_id: str,
        query: str,
        top_k: int,
        *,
        scope: RetrievalScope | None = None,
    ) -> list[RetrievedDoc]:
        rows = self.store.list(kb_id=kb_id, status="approved")
        if scope is not None and scope.is_source_restricted:
            rows = [
                row for row in rows if scope.allows_source(row.get("related_source"))
            ]
        if not rows:
            return []
        query_terms = Counter(tokenize_mixed_text(query))
        if not query_terms:
            return []

        scored: list[tuple[float, dict[str, Any], dict[str, Any]]] = []
        for row in rows:
            text = str(row.get("text") or "")
            source_note = str(row.get("source_note") or "")
            terms = Counter(tokenize_mixed_text(f"{text}\n{source_note}"))
            if not terms:
                continue
            overlap = sum(
                min(count, terms.get(term, 0)) for term, count in query_terms.items()
            )
            if overlap <= 0:
                continue
            coverage = overlap / max(sum(query_terms.values()), 1)
            density = overlap / max(sum(terms.values()), 1)
            matched_terms = sorted(
                term for term in query_terms if terms.get(term, 0) > 0
            )
            explanation = {
                "matched_terms": matched_terms[:12],
                "query_term_count": sum(query_terms.values()),
                "knowledge_term_count": sum(terms.values()),
                "match_coverage": round(coverage, 6),
                "match_density": round(density, 6),
            }
            scored.append((coverage + density, row, explanation))

        scored.sort(key=lambda item: item[0], reverse=True)
        return [
            _knowledge_doc(
                row,
                score=score,
                rank=rank,
                explanation=explanation,
                search_channel="derived_knowledge",
            )
            for rank, (score, row, explanation) in enumerate(scored[:top_k])
        ]
