from __future__ import annotations

from dataclasses import dataclass, field
from threading import Lock
from typing import Any

import weakref

from cogdoc.api.derived_knowledge_store import (
    DerivedKnowledgeStore,
    SqliteDerivedKnowledgeStore,
)
from cogdoc.api.feedback_analysis_store import (
    FeedbackAnalysisStore,
    SqliteFeedbackAnalysisStore,
)
from cogdoc.api.feedback_store import FeedbackStore, SqliteFeedbackStore
from cogdoc.api.retrieval_feedback_store import (
    RetrievalFeedbackStore,
    SqliteRetrievalFeedbackStore,
)
from cogdoc.api.retrieval_eval_draft_store import (
    RetrievalEvalDraftStore,
    SqliteRetrievalEvalDraftStore,
)
from cogdoc.api.research_job_store import ResearchJobStore, SqliteResearchJobStore
from cogdoc.config.settings import Settings, get_settings


@dataclass
class StateRuntime:
    """One coherent set of state stores shared by every serving entry point."""

    feedback_store: FeedbackStore
    feedback_analysis_store: FeedbackAnalysisStore
    knowledge_store: DerivedKnowledgeStore
    retrieval_feedback_store: RetrievalFeedbackStore
    derived_knowledge_index_persist_directory: str | None = None
    derived_knowledge_index_state_directory: str | None = None
    # Appended after the legacy positional fields to preserve direct-construction
    # compatibility. Runtimes from ``from_settings`` always own a real store.
    retrieval_eval_draft_store: RetrievalEvalDraftStore | None = None
    research_job_store: ResearchJobStore | None = None
    _derived_knowledge_index: Any = field(default=None, init=False, repr=False)
    _derived_knowledge_retriever: Any = field(default=None, init=False, repr=False)
    _index_lock: Lock = field(default_factory=Lock, init=False, repr=False)
    _retriever_lock: Lock = field(default_factory=Lock, init=False, repr=False)
    _close_lock: Lock = field(default_factory=Lock, init=False, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        # Direct construction remains supported, but paths are captured once instead
        # of consulting mutable global settings when the lazy index is first used.
        if (
            self.derived_knowledge_index_persist_directory is not None
            and self.derived_knowledge_index_state_directory is not None
        ):
            return
        settings = get_settings()
        if self.derived_knowledge_index_persist_directory is None:
            self.derived_knowledge_index_persist_directory = settings.chroma_persist_dir
        if self.derived_knowledge_index_state_directory is None:
            self.derived_knowledge_index_state_directory = str(
                settings.data_dir / "knowledge" / "derived_index_state"
            )

    @classmethod
    def from_settings(
        cls,
        settings: Settings | None = None,
        *,
        feedback_store: FeedbackStore | None = None,
        feedback_analysis_store: FeedbackAnalysisStore | None = None,
        knowledge_store: DerivedKnowledgeStore | None = None,
        retrieval_feedback_store: RetrievalFeedbackStore | None = None,
        retrieval_eval_draft_store: RetrievalEvalDraftStore | None = None,
        research_job_store: ResearchJobStore | None = None,
    ) -> "StateRuntime":
        settings = settings or get_settings()
        return cls(
            feedback_store=(
                feedback_store
                if feedback_store is not None
                else cls.default_feedback_store(settings)
            ),
            feedback_analysis_store=(
                feedback_analysis_store
                if feedback_analysis_store is not None
                else cls.default_feedback_analysis_store(settings)
            ),
            knowledge_store=(
                knowledge_store
                if knowledge_store is not None
                else cls.default_knowledge_store(settings)
            ),
            retrieval_feedback_store=(
                retrieval_feedback_store
                if retrieval_feedback_store is not None
                else cls.default_retrieval_feedback_store(settings)
            ),
            retrieval_eval_draft_store=(
                retrieval_eval_draft_store
                if retrieval_eval_draft_store is not None
                else cls.default_retrieval_eval_draft_store(settings)
            ),
            research_job_store=(
                research_job_store
                if research_job_store is not None
                else cls.default_research_job_store(settings)
            ),
            derived_knowledge_index_persist_directory=settings.chroma_persist_dir,
            derived_knowledge_index_state_directory=str(
                settings.data_dir / "knowledge" / "derived_index_state"
            ),
        )

    @staticmethod
    def default_feedback_store(settings: Settings | None = None) -> FeedbackStore:
        settings = settings or get_settings()
        if settings.cogdoc_state_backend == "sqlite":
            return SqliteFeedbackStore(
                db_path=settings.state_db_path,
                export_jsonl=False,
            )
        if settings.cogdoc_feedback_store.strip().lower() == "sqlite":
            return SqliteFeedbackStore(
                db_path=settings.feedback_db_path,
                feedback_path=settings.feedback_log_path,
                bad_cases_path=settings.bad_cases_path,
            )
        return FeedbackStore(
            feedback_path=settings.feedback_log_path,
            bad_cases_path=settings.bad_cases_path,
        )

    @staticmethod
    def default_feedback_analysis_store(
        settings: Settings | None = None,
    ) -> FeedbackAnalysisStore:
        settings = settings or get_settings()
        if settings.cogdoc_state_backend == "sqlite":
            return SqliteFeedbackAnalysisStore(settings.state_db_path)
        return FeedbackAnalysisStore(settings.feedback_analysis_path)

    @staticmethod
    def default_knowledge_store(
        settings: Settings | None = None,
    ) -> DerivedKnowledgeStore:
        settings = settings or get_settings()
        if settings.cogdoc_state_backend == "sqlite":
            return SqliteDerivedKnowledgeStore(settings.state_db_path)
        return DerivedKnowledgeStore(settings.derived_knowledge_path)

    @staticmethod
    def default_retrieval_feedback_store(
        settings: Settings | None = None,
    ) -> RetrievalFeedbackStore:
        settings = settings or get_settings()
        if settings.cogdoc_state_backend == "sqlite":
            return SqliteRetrievalFeedbackStore(settings.state_db_path)
        return RetrievalFeedbackStore(settings.retrieval_feedback_path)

    @staticmethod
    def default_retrieval_eval_draft_store(
        settings: Settings | None = None,
    ) -> RetrievalEvalDraftStore:
        settings = settings or get_settings()
        if settings.cogdoc_state_backend == "sqlite":
            return SqliteRetrievalEvalDraftStore(settings.state_db_path)
        return RetrievalEvalDraftStore(settings.retrieval_eval_drafts_path)

    @staticmethod
    def default_research_job_store(
        settings: Settings | None = None,
    ) -> ResearchJobStore:
        settings = settings or get_settings()
        if settings.cogdoc_state_backend == "sqlite":
            return SqliteResearchJobStore(settings.state_db_path)
        return ResearchJobStore(settings.research_jobs_path)

    @property
    def derived_knowledge_index(self):
        if self._closed:
            raise RuntimeError("StateRuntime is closed")
        if self._derived_knowledge_index is None:
            with self._index_lock:
                if self._derived_knowledge_index is None:
                    from cogdoc.tools.retriever.derived_knowledge import (
                        DerivedKnowledgeIndex,
                    )

                    self._derived_knowledge_index = DerivedKnowledgeIndex(
                        self.knowledge_store,
                        persist_directory=(
                            self.derived_knowledge_index_persist_directory
                        ),
                        state_directory=self.derived_knowledge_index_state_directory,
                    )
        return self._derived_knowledge_index

    def _validate_knowledge_store(self, store: Any | None) -> None:
        if store is not None and store is not self.knowledge_store:
            raise ValueError("knowledge store does not belong to this StateRuntime")

    def bind_derived_knowledge_index(self, index: Any) -> None:
        """Bind a preconfigured index authority before retrieval starts.

        HA deployments use this seam to replace the node-local mutable Chroma
        collection with immutable, content-verified shared generations.  A
        runtime that has already materialized another index cannot be rebound:
        silently swapping it would let concurrent readers observe two
        authorities for the same knowledge base.
        """

        if index is None:
            raise TypeError("derived knowledge index is required")
        if self._closed:
            raise RuntimeError("StateRuntime is closed")
        with self._index_lock, self._retriever_lock:
            current = self._derived_knowledge_index
            if current is not None and current is not index:
                raise ValueError("derived knowledge index is already initialized")
            # A retriever only captures an index factory, but replacing an
            # already-used retriever would still make in-flight callers depend
            # on construction order.  Fail closed instead.
            if self._derived_knowledge_retriever is not None:
                raise ValueError("derived knowledge retriever is already initialized")
            self._derived_knowledge_index = index

    @property
    def has_bound_derived_knowledge_index(self) -> bool:
        return self._derived_knowledge_index is not None

    def refresh_derived_knowledge_index(
        self,
        kb_id: str,
        store: Any | None = None,
    ) -> None:
        self._validate_knowledge_store(store)
        self.derived_knowledge_index.rebuild(kb_id)

    def derived_knowledge_index_status(
        self,
        kb_id: str,
        store: Any | None = None,
    ) -> dict[str, Any]:
        self._validate_knowledge_store(store)
        return self.derived_knowledge_index.status(kb_id)

    def record_derived_knowledge_index_error(
        self,
        kb_id: str,
        error_class: str,
    ) -> None:
        self.derived_knowledge_index.record_error(kb_id, error_class)

    def clear_derived_knowledge_index(self, kb_id: str) -> None:
        if self._closed:
            raise RuntimeError("StateRuntime is closed")
        # Only snapshot the current authority under the construction lock.
        # Cleanup drains readers, and an existing reader may still need this
        # lock to finish lazy index construction; holding it while draining
        # would recreate the same lock-order inversion at the runtime layer.
        with self._index_lock:
            index = self._derived_knowledge_index
        if index is None:
            from cogdoc.tools.retriever.derived_knowledge import (
                clear_derived_knowledge_index_storage,
            )

            persist_directory = self.derived_knowledge_index_persist_directory
            state_directory = self.derived_knowledge_index_state_directory
            if persist_directory is None or state_directory is None:
                raise RuntimeError("derived knowledge index paths are unavailable")
            clear_derived_knowledge_index_storage(
                kb_id,
                persist_directory=persist_directory,
                state_directory=state_directory,
            )
            return
        clear_kb = getattr(index, "clear_kb", None)
        if not callable(clear_kb):
            raise RuntimeError("derived knowledge index does not support KB cleanup")
        clear_kb(kb_id)

    @property
    def derived_knowledge_retriever(self):
        # Construction opens the vector index lazily; keep one retriever per runtime.
        if self._closed:
            raise RuntimeError("StateRuntime is closed")
        if self._derived_knowledge_retriever is None:
            with self._retriever_lock:
                if self._derived_knowledge_retriever is None:
                    from cogdoc.tools.retriever.derived_knowledge import (
                        DerivedKnowledgeRetriever,
                    )

                    runtime_ref = weakref.ref(self)

                    def resolve_index():
                        runtime = runtime_ref()
                        if runtime is None or runtime.closed:
                            raise RuntimeError("StateRuntime is unavailable")
                        return runtime.derived_knowledge_index

                    self._derived_knowledge_retriever = DerivedKnowledgeRetriever(
                        self.knowledge_store,
                        index_factory=resolve_index,
                    )
        return self._derived_knowledge_retriever

    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        """Close owned long-lived store connections once.

        JSONL stores do not expose ``close``; SQLite stores do.  All closeable
        stores are attempted even if one fails so shutdown cannot leak the rest.
        """

        with self._close_lock:
            if self._closed:
                return
            self._closed = True
            errors: list[Exception] = []
            seen: set[int] = set()
            for store in (
                self.feedback_store,
                self.feedback_analysis_store,
                self.knowledge_store,
                self.retrieval_feedback_store,
                self.retrieval_eval_draft_store,
                self.research_job_store,
                self._derived_knowledge_index,
                self._derived_knowledge_retriever,
            ):
                if store is None:
                    continue
                if id(store) in seen:
                    continue
                seen.add(id(store))
                close = getattr(store, "close", None)
                if not callable(close):
                    continue
                try:
                    close()
                except Exception as exc:  # pragma: no cover - defensive shutdown
                    errors.append(exc)
            if errors:
                raise RuntimeError(
                    f"failed to close {len(errors)} StateRuntime store(s)"
                ) from errors[0]

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            # Interpreter shutdown and garbage collection cannot surface a
            # cleanup failure. Explicit close/reset paths still report errors.
            pass


_default_runtime: StateRuntime | None = None
_default_runtime_key: tuple[str, ...] | None = None
_default_runtime_lock = Lock()
_retired_default_runtimes: list[weakref.ReferenceType[StateRuntime]] = []


def _settings_key(settings: Settings) -> tuple[str, ...]:
    return (
        str(settings.cogdoc_state_backend),
        str(settings.cogdoc_feedback_store),
        settings.state_db_path,
        settings.feedback_db_path,
        settings.feedback_log_path,
        settings.bad_cases_path,
        settings.feedback_analysis_path,
        settings.derived_knowledge_path,
        settings.retrieval_feedback_path,
        settings.retrieval_eval_drafts_path,
        settings.research_jobs_path,
        settings.chroma_persist_dir,
        str(settings.data_dir / "knowledge" / "derived_index_state"),
    )


def default_state_runtime() -> StateRuntime:
    """Compatibility runtime for direct graph/CLI calls outside an API app."""

    global _default_runtime, _default_runtime_key
    settings = get_settings()
    key = _settings_key(settings)
    with _default_runtime_lock:
        if (
            _default_runtime is None
            or _default_runtime.closed
            or _default_runtime_key != key
        ):
            previous = _default_runtime
            _default_runtime = StateRuntime.from_settings(settings)
            _default_runtime_key = key
            if previous is not None:
                # A caller may still be using the object returned immediately
                # before this settings boundary. Retire it and close only at
                # the explicit reset/quiescence boundary.
                _retired_default_runtimes.append(weakref.ref(previous))
        runtime = _default_runtime
    return runtime


def reset_default_state_runtime(*, close: bool = True) -> None:
    """Drop the compatibility singleton after a configuration/test boundary."""

    global _default_runtime, _default_runtime_key
    with _default_runtime_lock:
        previous = [
            runtime
            for reference in _retired_default_runtimes
            if (runtime := reference()) is not None
        ]
        if _default_runtime is not None:
            previous.append(_default_runtime)
        _retired_default_runtimes.clear()
        _default_runtime = None
        _default_runtime_key = None
    if close:
        for runtime in previous:
            runtime.close()
