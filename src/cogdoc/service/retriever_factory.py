from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from threading import RLock
from typing import Any

from cogdoc.config.settings import get_settings
from cogdoc.service.kb_lifecycle import LIFECYCLE_ACTIVE, shared_lifecycle_store
from cogdoc.service.kb_state import KBState
from cogdoc.tools.retriever.base_retriever import NullRetriever
from cogdoc.tools.retriever.bm25_retriever import BM25Retriever
from cogdoc.tools.retriever.hybrid import HybridRetriever, IndexCorruptError
from cogdoc.tools.retriever.vector_retriever import (
    EmbeddingModelMismatchError,
    VectorRetriever,
)
from cogdoc.tools.embedder import resolve_embedder


class RetrieverFactory:
    """Process-local, generation-aware retrieval engine registry.

    The factory is a service dependency shared by every evidence-producing task.
    Keeping it outside the QA graph prevents Summary, Compare, evaluation, and
    future agents from depending on a task-specific subgraph merely to read an
    index.
    """

    _engines: "OrderedDict[tuple, HybridRetriever]" = OrderedDict()
    _lock = RLock()
    _max_engines = 32
    _external_provider: Callable[[str], Any] | None = None
    _context_provider: ContextVar[Callable[[str], Any] | None] = ContextVar(
        "cogdoc_retriever_provider", default=None
    )

    @classmethod
    def get_engine(cls, kb_id: str) -> HybridRetriever:
        if shared_lifecycle_store().status(kb_id) != LIFECYCLE_ACTIVE:
            return HybridRetriever(NullRetriever(), NullRetriever())

        provider = cls._context_provider.get()
        if provider is None:
            with cls._lock:
                provider = cls._external_provider
        if provider is not None:
            engine = provider(kb_id)
            if shared_lifecycle_store().status(kb_id) != LIFECYCLE_ACTIVE:
                return HybridRetriever(NullRetriever(), NullRetriever())
            return engine

        gen_id = cls._resolve_gen_id(kb_id)
        cache_key = (kb_id, gen_id)

        with cls._lock:
            engine = cls._engines.get(cache_key)
            if engine is not None:
                if shared_lifecycle_store().status(kb_id) != LIFECYCLE_ACTIVE:
                    return HybridRetriever(NullRetriever(), NullRetriever())
                cls._engines.move_to_end(cache_key)
                return engine

        built = cls._build_engine(kb_id, gen_id)

        with cls._lock:
            if shared_lifecycle_store().status(kb_id) != LIFECYCLE_ACTIVE:
                return HybridRetriever(NullRetriever(), NullRetriever())
            current_gen_id = cls._resolve_gen_id(kb_id)
            if current_gen_id != gen_id:
                return built
            engine = cls._engines.get(cache_key)
            if engine is None:
                cls._engines[cache_key] = built
                engine = built
                while len(cls._engines) > cls._max_engines:
                    cls._engines.popitem(last=False)
            cls._engines.move_to_end(cache_key)
            return engine

    @classmethod
    def _resolve_gen_id(cls, kb_id: str) -> str | None:
        active = KBState(kb_id).active()
        if active is None or active.get("expected_count") == 0:
            return None
        return active["id"]

    @classmethod
    def _build_engine(cls, kb_id: str, gen_id: str | None) -> HybridRetriever:
        if gen_id is None:
            return HybridRetriever(NullRetriever(), NullRetriever())

        collection_id = get_settings().kb_collection_id(kb_id, gen_id)
        gen_state = KBState(kb_id).get(gen_id)
        if gen_state is None:
            return HybridRetriever(NullRetriever(), NullRetriever())
        try:
            embedder = resolve_embedder(str(gen_state.get("embedding_model") or "local"))
        except RuntimeError:
            return HybridRetriever(NullRetriever(), NullRetriever())
        except ValueError:
            # Pre-profile generations may contain an arbitrary local alias.
            from cogdoc.tools.embedder import Embedder

            embedder = Embedder
        try:
            engine = HybridRetriever(
                vector_retriever=(
                    VectorRetriever(collection_id=collection_id)
                    if getattr(embedder, "PROFILE_ID", "local") == "local"
                    else VectorRetriever(
                        collection_id=collection_id, embedder=embedder
                    )
                ),
                bm25_retriever=BM25Retriever(collection_id=collection_id),
            )
        except EmbeddingModelMismatchError:
            return HybridRetriever(NullRetriever(), NullRetriever())

        expected = gen_state.get("expected_count")
        actual = engine.count()
        consistent = engine.is_consistent()
        if actual != expected or not consistent:
            raise IndexCorruptError(
                f"generation {gen_id}: expected_count={expected}, actual={actual}, "
                f"consistent={consistent}; rebuild required"
            )
        return engine

    @classmethod
    def invalidate(cls, kb_id: str) -> None:
        with cls._lock:
            stale_keys = [key for key in cls._engines if key[0] == kb_id]
            for key in stale_keys:
                del cls._engines[key]

    @classmethod
    def bind_external_provider(cls, provider: Callable[[str], Any]) -> None:
        if not callable(provider):
            raise TypeError("retrieval engine provider must be callable")
        with cls._lock:
            if (
                cls._external_provider is not None
                and cls._external_provider is not provider
            ):
                raise RuntimeError("retrieval engine provider is already bound")
            cls._external_provider = provider
            cls._engines.clear()

    @classmethod
    @contextmanager
    def provider_context(cls, provider: Callable[[str], Any]):
        """Bind a request-scoped provider without mutating process-global state."""

        if not callable(provider):
            raise TypeError("retrieval engine provider must be callable")
        token = cls._context_provider.set(provider)
        try:
            yield
        finally:
            cls._context_provider.reset(token)

    @classmethod
    def unbind_external_provider(cls, provider: Callable[[str], Any]) -> None:
        with cls._lock:
            if cls._external_provider is provider:
                cls._external_provider = None
                cls._engines.clear()
