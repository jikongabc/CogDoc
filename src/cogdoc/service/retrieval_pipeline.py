from __future__ import annotations

import copy
import inspect
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, cast

from cogdoc.graph.state import RetrievalMetrics, RetrievedDoc
from cogdoc.tools.retriever.fusion import (
    RankedCandidateList,
    fuse_ranked_candidates,
)
from cogdoc.tools.retriever.scope import RetrievalScope


HYBRID_CHANNEL = "hybrid"
DERIVED_KNOWLEDGE_CHANNEL = "derived_knowledge"
RAG_VECTOR_CHANNEL = "rag_vector"
RAG_LEXICAL_CHANNEL = "rag_bm25"
DERIVED_KNOWLEDGE_VECTOR_CHANNEL = "derived_knowledge_vector"
DERIVED_KNOWLEDGE_LEXICAL_CHANNEL = "derived_knowledge_lexical"

DEFAULT_ROUTE_WEIGHTS: dict[str, float] = {
    HYBRID_CHANNEL: 1.0,
    DERIVED_KNOWLEDGE_CHANNEL: 0.9,
    RAG_VECTOR_CHANNEL: 1.0,
    RAG_LEXICAL_CHANNEL: 1.0,
    DERIVED_KNOWLEDGE_VECTOR_CHANNEL: 0.9,
    DERIVED_KNOWLEDGE_LEXICAL_CHANNEL: 0.8,
}


def configured_route_weights() -> dict[str, float]:
    """Resolve route weights once from the application retrieval settings."""

    from cogdoc.config.settings import get_settings

    settings = get_settings()
    return {
        **DEFAULT_ROUTE_WEIGHTS,
        RAG_VECTOR_CHANNEL: settings.qa_rag_vector_route_weight,
        RAG_LEXICAL_CHANNEL: settings.qa_rag_bm25_route_weight,
        DERIVED_KNOWLEDGE_VECTOR_CHANNEL: (
            settings.qa_derived_knowledge_vector_route_weight
        ),
        DERIVED_KNOWLEDGE_LEXICAL_CHANNEL: (
            settings.qa_derived_knowledge_lexical_route_weight
        ),
    }


class _DocumentRetriever(Protocol):
    def search(
        self,
        query: str,
        top_k: int = 3,
        *,
        scope: RetrievalScope | None = None,
    ) -> list[RetrievedDoc]: ...


class _DerivedKnowledgeRetriever(Protocol):
    def search(
        self,
        kb_id: str,
        query: str,
        top_k: int = 3,
        *,
        scope: RetrievalScope | None = None,
    ) -> list[RetrievedDoc]: ...


class _DocumentChannelSearch(Protocol):
    def __call__(
        self,
        query: str,
        top_k: int = 3,
        *,
        scope: RetrievalScope | None = None,
    ) -> Mapping[str, Sequence[RetrievedDoc]]: ...


class _KnowledgeChannelSearch(Protocol):
    def __call__(
        self,
        kb_id: str,
        query: str,
        top_k: int = 3,
        *,
        scope: RetrievalScope | None = None,
    ) -> Mapping[str, Sequence[RetrievedDoc]]: ...


class _RetrievalFeedbackStore(Protocol):
    def boosts_for_query(self, kb_id: str, query: str) -> dict[str, float]: ...


@dataclass(frozen=True)
class RetrievalQuery:
    text: str
    requirement_ids: Sequence[str] = ()
    is_original: bool = False


@dataclass(frozen=True)
class RetrievalPipelineResult:
    docs: list[RetrievedDoc]
    queries: list[RetrievalQuery]
    ranking_count: int
    channel_counts: dict[str, int]
    feedback_error: str = ""
    route_rankings: list[RankedCandidateList] = field(default_factory=list)


def _clean_query_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    normalized = unicodedata.normalize("NFKC", value)
    return re.sub(r"\s+", " ", normalized).strip()


def _query_dedupe_key(value: str) -> str:
    return "".join(char.lower() if "A" <= char <= "Z" else char for char in value)


def _normalize_requirement_id(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return unicodedata.normalize("NFKC", value).strip()


@dataclass
class _MutableQuery:
    text: str
    requirement_ids: list[str]
    is_original: bool


def _requirement_value(requirement: Mapping[str, Any], key: str) -> str:
    return _clean_query_text(requirement.get(key))


def build_retrieval_queries(
    original_query: str,
    rewritten_queries: Sequence[str] = (),
    evidence_requirements: Sequence[Mapping[str, Any]] = (),
    prioritized_requirement_ids: Sequence[str] = (),
    max_queries: int = 7,
) -> list[RetrievalQuery]:
    """Build a bounded, role-preserving query plan in deterministic priority order."""

    if max_queries < 0:
        raise ValueError("max_queries must be non-negative")
    if max_queries == 0:
        return []

    planned: dict[str, _MutableQuery] = {}

    def add_query(
        value: Any,
        *,
        requirement_ids: Sequence[str] = (),
        is_original: bool = False,
    ) -> None:
        text = _clean_query_text(value)
        if not text:
            return
        dedupe_key = _query_dedupe_key(text)
        normalized_ids: list[str] = []
        for raw_id in requirement_ids:
            requirement_id = _normalize_requirement_id(raw_id)
            if requirement_id and requirement_id not in normalized_ids:
                normalized_ids.append(requirement_id)

        existing = planned.get(dedupe_key)
        if existing is None:
            planned[dedupe_key] = _MutableQuery(text, normalized_ids, is_original)
            return
        for requirement_id in normalized_ids:
            if requirement_id not in existing.requirement_ids:
                existing.requirement_ids.append(requirement_id)
        existing.is_original = existing.is_original or is_original

    add_query(original_query, is_original=True)

    normalized_priorities: list[str] = []
    for raw_id in prioritized_requirement_ids:
        requirement_id = _normalize_requirement_id(raw_id)
        if requirement_id and requirement_id not in normalized_priorities:
            normalized_priorities.append(requirement_id)
    priority_set = set(normalized_priorities)

    requirements_by_id: dict[str, list[Mapping[str, Any]]] = {}
    remaining_requirements: list[tuple[str, Mapping[str, Any]]] = []
    for requirement in evidence_requirements:
        requirement_id = _normalize_requirement_id(requirement.get("requirement_id"))
        requirements_by_id.setdefault(requirement_id, []).append(requirement)
        if requirement_id not in priority_set:
            remaining_requirements.append((requirement_id, requirement))

    for requirement_id in normalized_priorities:
        for requirement in requirements_by_id.get(requirement_id, ()):
            query = _requirement_value(requirement, "recovery_query")
            if not query:
                query = _requirement_value(requirement, "retrieval_query")
            add_query(query, requirement_ids=(requirement_id,))

    for requirement_id, requirement in remaining_requirements:
        add_query(
            _requirement_value(requirement, "retrieval_query"),
            requirement_ids=(requirement_id,),
        )

    for rewritten_query in rewritten_queries:
        add_query(rewritten_query)

    return [
        RetrievalQuery(
            text=query.text,
            requirement_ids=tuple(query.requirement_ids),
            is_original=query.is_original,
        )
        for query in list(planned.values())[:max_queries]
    ]


def _chunk_id(doc: Mapping[str, Any]) -> str:
    meta = doc.get("meta")
    if not isinstance(meta, Mapping):
        return ""
    return str(meta.get("chunk_id") or "")


def _apply_feedback_boosts(
    docs: Sequence[RetrievedDoc],
    *,
    kb_id: str,
    query: str,
    store: _RetrievalFeedbackStore | None,
) -> tuple[list[RetrievedDoc], str]:
    if not docs or not query or store is None:
        return list(docs), ""
    try:
        boosts = store.boosts_for_query(kb_id, query)
        if not boosts:
            return list(docs), ""

        adjusted: list[tuple[int, float, RetrievedDoc]] = []
        for index, doc in enumerate(docs):
            boost = float(boosts.get(_chunk_id(doc), 0.0))
            adjusted_doc = doc
            if boost:
                adjusted_doc = copy.deepcopy(doc)
                raw_retrieval = adjusted_doc.get("retrieval")
                retrieval = (
                    dict(raw_retrieval) if isinstance(raw_retrieval, Mapping) else {}
                )
                retrieval["feedback_boost"] = boost
                adjusted_doc["retrieval"] = cast(RetrievalMetrics, retrieval)
            adjusted.append((index, boost, adjusted_doc))
        adjusted.sort(key=lambda item: (-item[1], item[0]))
    except Exception as exc:
        return list(docs), type(exc).__name__
    return [doc for _, _, doc in adjusted], ""


def apply_retrieval_feedback(
    kb_id: str,
    query: str,
    docs: Sequence[RetrievedDoc],
    store: _RetrievalFeedbackStore | None = None,
) -> list[RetrievedDoc]:
    """Compatibility helper preserving the existing feedback ordering contract."""

    if store is None:
        from cogdoc.state_runtime import default_state_runtime

        store = default_state_runtime().retrieval_feedback_store
    adjusted, _ = _apply_feedback_boosts(
        docs,
        kb_id=kb_id,
        query=query,
        store=store,
    )
    return adjusted


def retrieve_candidate_pool(
    engine: _DocumentRetriever,
    derived_knowledge_retriever: _DerivedKnowledgeRetriever,
    retrieval_feedback_store: _RetrievalFeedbackStore | None,
    *,
    kb_id: str,
    original_query: str,
    queries: Sequence[RetrievalQuery],
    top_k: int,
    rrf_k: float,
    retrieval_round: int = 0,
    fusion_top_n: int | None = None,
    scope: RetrievalScope | None = None,
    route_weights: Mapping[str, float] | None = None,
    route_min_candidates: int | None = None,
) -> RetrievalPipelineResult:
    """Retrieve allowed channels for each query, fuse them, then apply feedback.

    ``scope=None`` preserves the legacy unscoped method calls exactly.  A
    provided scope is forwarded to every enabled channel so source allowlists
    take effect inside each channel before its top-k boundary.
    """

    if top_k < 0:
        raise ValueError("top_k must be non-negative")
    if route_min_candidates is not None and route_min_candidates < 0:
        raise ValueError("route_min_candidates must be non-negative")

    weights = configured_route_weights()
    if route_weights is not None:
        weights.update({str(key): float(value) for key, value in route_weights.items()})
    if route_min_candidates is None:
        from cogdoc.config.settings import get_settings

        route_min_candidates = get_settings().qa_retrieval_docs_per_route

    def accepts_channels(callable_object: Any) -> bool:
        try:
            parameters = inspect.signature(callable_object).parameters
        except (TypeError, ValueError):
            return False
        return "channels" in parameters or any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        )

    rankings: list[RankedCandidateList] = []
    executed_queries = [query for query in queries if query.text.strip()]

    engine_search_many_channels = getattr(engine, "search_many_channels", None)
    engine_search_channels = getattr(engine, "search_channels", None)
    uses_document_routes = callable(engine_search_many_channels) or callable(
        engine_search_channels
    )
    channel_counts = (
        {RAG_VECTOR_CHANNEL: 0, RAG_LEXICAL_CHANNEL: 0}
        if uses_document_routes
        else {HYBRID_CHANNEL: 0}
    )

    document_channel_rows: Sequence[Mapping[str, list[RetrievedDoc]]] | None = None
    enabled_document_channels = {
        channel
        for channel, route in (("vector", RAG_VECTOR_CHANNEL), ("bm25", RAG_LEXICAL_CHANNEL))
        if weights.get(route, 1.0) > 0
    }
    if not enabled_document_channels and uses_document_routes:
        document_channel_rows = [
            {"vector": [], "bm25": []} for _ in executed_queries
        ]
    elif callable(engine_search_many_channels) and executed_queries:
        query_texts = [query.text for query in executed_queries]
        kwargs: dict[str, Any] = {"top_k": top_k}
        if scope is not None:
            kwargs["scope"] = scope
        if accepts_channels(engine_search_many_channels):
            kwargs["channels"] = enabled_document_channels
        document_channel_rows = engine_search_many_channels(query_texts, **kwargs)
        if len(document_channel_rows) != len(executed_queries):
            raise RuntimeError(
                "retriever search_many_channels returned an invalid row count"
            )

    engine_search_many = getattr(engine, "search_many", None)
    hybrid_rankings: Sequence[list[RetrievedDoc]] | None = None
    if (
        document_channel_rows is None
        and not callable(engine_search_channels)
        and callable(engine_search_many)
        and executed_queries
    ):
        query_texts = [query.text for query in executed_queries]
        hybrid_rankings = (
            engine_search_many(query_texts, top_k=top_k)
            if scope is None
            else engine_search_many(query_texts, top_k=top_k, scope=scope)
        )
        if len(hybrid_rankings) != len(executed_queries):
            raise RuntimeError("retriever search_many returned an invalid row count")

    knowledge_search_many_channels = getattr(
        derived_knowledge_retriever, "search_many_channels", None
    )
    knowledge_search_channels = getattr(
        derived_knowledge_retriever, "search_channels", None
    )
    uses_knowledge_routes = callable(knowledge_search_many_channels) or callable(
        knowledge_search_channels
    )
    if uses_knowledge_routes:
        channel_counts.update(
            {
                DERIVED_KNOWLEDGE_VECTOR_CHANNEL: 0,
                DERIVED_KNOWLEDGE_LEXICAL_CHANNEL: 0,
            }
        )
    else:
        channel_counts[DERIVED_KNOWLEDGE_CHANNEL] = 0

    knowledge_channel_rows: Sequence[Mapping[str, list[RetrievedDoc]]] | None = None
    enabled_knowledge_channels = {
        channel
        for channel, route in (
            ("embedding", DERIVED_KNOWLEDGE_VECTOR_CHANNEL),
            ("lexical", DERIVED_KNOWLEDGE_LEXICAL_CHANNEL),
        )
        if weights.get(route, 1.0) > 0
    }
    if not enabled_knowledge_channels and uses_knowledge_routes:
        knowledge_channel_rows = [
            {"embedding": [], "lexical": []} for _ in executed_queries
        ]
    if (
        knowledge_channel_rows is None
        and
        callable(knowledge_search_many_channels)
        and executed_queries
        and (scope is None or scope.include_derived_knowledge)
    ):
        query_texts = [query.text for query in executed_queries]
        kwargs = {"top_k": top_k}
        if scope is not None:
            kwargs["scope"] = scope
        if accepts_channels(knowledge_search_many_channels):
            kwargs["channels"] = enabled_knowledge_channels
        knowledge_channel_rows = knowledge_search_many_channels(
            kb_id, query_texts, **kwargs
        )
        if len(knowledge_channel_rows) != len(executed_queries):
            raise RuntimeError(
                "derived knowledge search_many_channels returned an invalid row count"
            )

    knowledge_search_many = getattr(derived_knowledge_retriever, "search_many", None)
    knowledge_rankings: Sequence[list[RetrievedDoc]] | None = None
    if (
        knowledge_channel_rows is None
        and not callable(knowledge_search_channels)
        and callable(knowledge_search_many)
        and executed_queries
        and (scope is None or scope.include_derived_knowledge)
    ):
        query_texts = [query.text for query in executed_queries]
        knowledge_rankings = (
            knowledge_search_many(kb_id, query_texts, top_k=top_k)
            if scope is None
            else knowledge_search_many(kb_id, query_texts, top_k=top_k, scope=scope)
        )
        if len(knowledge_rankings) != len(executed_queries):
            raise RuntimeError(
                "derived knowledge search_many returned an invalid row count"
            )

    def add_ranking(
        query: RetrievalQuery,
        channel: str,
        docs: Sequence[RetrievedDoc],
    ) -> None:
        if weights.get(channel, 1.0) <= 0:
            return
        materialized = list(docs)
        channel_counts[channel] = channel_counts.get(channel, 0) + len(materialized)
        if materialized:
            rankings.append(
                RankedCandidateList(
                    query=query.text,
                    channel=channel,
                    docs=materialized,
                    requirement_ids=query.requirement_ids,
                    is_original=query.is_original,
                    retrieval_round=retrieval_round,
                    weight=weights.get(channel, 1.0),
                )
            )

    for query_index, query in enumerate(executed_queries):
        if uses_document_routes:
            document_channel_search = cast(
                _DocumentChannelSearch, engine_search_channels
            )
            document_routes = (
                document_channel_rows[query_index]
                if document_channel_rows is not None
                else (
                    document_channel_search(query.text, top_k=top_k)
                    if scope is None
                    else document_channel_search(query.text, top_k=top_k, scope=scope)
                )
            )
            add_ranking(query, RAG_VECTOR_CHANNEL, document_routes.get("vector", ()))
            add_ranking(query, RAG_LEXICAL_CHANNEL, document_routes.get("bm25", ()))
        else:
            hybrid_docs = (
                list(hybrid_rankings[query_index])
                if hybrid_rankings is not None
                else (
                    engine.search(query=query.text, top_k=top_k)
                    if scope is None
                    else engine.search(query=query.text, top_k=top_k, scope=scope)
                )
            )
            add_ranking(query, HYBRID_CHANNEL, hybrid_docs)

        knowledge_allowed = scope is None or scope.include_derived_knowledge
        if uses_knowledge_routes:
            knowledge_channel_search = cast(
                _KnowledgeChannelSearch, knowledge_search_channels
            )
            knowledge_routes: Mapping[str, Sequence[RetrievedDoc]] = {}
            if knowledge_allowed:
                knowledge_routes = (
                    knowledge_channel_rows[query_index]
                    if knowledge_channel_rows is not None
                    else (
                        knowledge_channel_search(kb_id, query.text, top_k=top_k)
                        if scope is None
                        else knowledge_channel_search(
                            kb_id, query.text, top_k=top_k, scope=scope
                        )
                    )
                )
            add_ranking(
                query,
                DERIVED_KNOWLEDGE_VECTOR_CHANNEL,
                knowledge_routes.get("embedding", ()),
            )
            add_ranking(
                query,
                DERIVED_KNOWLEDGE_LEXICAL_CHANNEL,
                knowledge_routes.get("lexical", ()),
            )
        else:
            knowledge_docs = (
                list(knowledge_rankings[query_index])
                if knowledge_rankings is not None
                else (
                    derived_knowledge_retriever.search(kb_id, query.text, top_k=top_k)
                    if scope is None
                    else (
                        derived_knowledge_retriever.search(
                            kb_id, query.text, top_k=top_k, scope=scope
                        )
                        if scope.include_derived_knowledge
                        else []
                    )
                )
            )
            add_ranking(query, DERIVED_KNOWLEDGE_CHANNEL, knowledge_docs)

    fused = fuse_ranked_candidates(
        rankings,
        rrf_k=rrf_k,
        top_n=fusion_top_n,
        per_channel_min=route_min_candidates,
    )
    # A retriever implementation is not an authorization authority.  Apply a
    # second guard after channel fusion so a stale/custom backend cannot smuggle
    # an out-of-scope source into prompts, traces, or persisted evidence even if
    # it ignored the pre-top-k scope contract.
    if scope is not None:
        fused = [doc for doc in fused if scope.allows_document(doc)]
    adjusted, feedback_error = _apply_feedback_boosts(
        fused,
        kb_id=kb_id,
        query=original_query,
        store=retrieval_feedback_store,
    )
    return RetrievalPipelineResult(
        docs=adjusted,
        queries=executed_queries,
        ranking_count=len(rankings),
        channel_counts=channel_counts,
        feedback_error=feedback_error,
        route_rankings=rankings,
    )
