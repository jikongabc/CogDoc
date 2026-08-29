from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
import logging
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


logger = logging.getLogger(__name__)


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

    @staticmethod
    def _close_engine(engine: Any) -> None:
        close = getattr(engine, "close", None)
        if callable(close):
            close()

    @classmethod
    def _close_engines(cls, engines: list[Any]) -> None:
        for engine in engines:
            try:
                cls._close_engine(engine)
            except Exception:
                logger.exception("failed to close an evicted retrieval engine")

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

        # A generation can advance while an engine is being constructed.  A
        # stale engine must never escape merely because it finished building
        # first. Retry a bounded number of times and fail closed under churn.
        for _attempt in range(3):
            gen_id = cls._resolve_gen_id(kb_id)
            cache_key = (kb_id, gen_id)

            with cls._lock:
                engine = cls._engines.get(cache_key)
                if engine is not None:
                    cls._engines.move_to_end(cache_key)
            if engine is not None:
                if (
                    shared_lifecycle_store().status(kb_id) != LIFECYCLE_ACTIVE
                    or cls._resolve_gen_id(kb_id) != gen_id
                ):
                    with cls._lock:
                        stale = (
                            cls._engines.pop(cache_key, None)
                            if cls._engines.get(cache_key) is engine
                            else None
                        )
                    if stale is not None:
                        cls._close_engines([stale])
                    continue
                return engine

            built = cls._build_engine(kb_id, gen_id)
            if (
                shared_lifecycle_store().status(kb_id) != LIFECYCLE_ACTIVE
                or cls._resolve_gen_id(kb_id) != gen_id
            ):
                cls._close_engines([built])
                continue

            evicted: list[Any] = []
            with cls._lock:
                engine = cls._engines.get(cache_key)
                if engine is None:
                    cls._engines[cache_key] = built
                    engine = built
                    while len(cls._engines) > cls._max_engines:
                        _, stale = cls._engines.popitem(last=False)
                        evicted.append(stale)
                elif built is not engine:
                    evicted.append(built)
                cls._engines.move_to_end(cache_key)
            cls._close_engines(evicted)
            # Close the remaining publish window before exposing the cached
            # engine. invalidate() removes the stale entry when the head moved.
            if (
                shared_lifecycle_store().status(kb_id) != LIFECYCLE_ACTIVE
                or cls._resolve_gen_id(kb_id) != gen_id
            ):
                with cls._lock:
                    stale = cls._engines.pop(cache_key, None)
                if stale is not None:
                    cls._close_engines([stale])
                continue
            return engine
        return HybridRetriever(NullRetriever(), NullRetriever())

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
            cls._close_engines([engine])
            raise IndexCorruptError(
                f"generation {gen_id}: expected_count={expected}, actual={actual}, "
                f"consistent={consistent}; rebuild required"
            )
        return engine

    @classmethod
    def invalidate(cls, kb_id: str) -> None:
        with cls._lock:
            stale_keys = [key for key in cls._engines if key[0] == kb_id]
            stale = [cls._engines.pop(key) for key in stale_keys]
        cls._close_engines(stale)

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
            stale = list(cls._engines.values())
            cls._engines.clear()
        cls._close_engines(stale)

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
            stale: list[Any] = []
            if cls._external_provider is provider:
                cls._external_provider = None
                stale = list(cls._engines.values())
                cls._engines.clear()
        cls._close_engines(stale)
