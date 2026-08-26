import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from cogdoc.config.settings import Settings, get_settings
from cogdoc.tools.tokenizer import tokenize_mixed_text


@dataclass(frozen=True)
class RetrievalSupport:
    supported: bool
    score: float
    reason: str
    signals: dict[str, float]


def _finite_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _threshold_ratio(value: float | None, threshold: float) -> float:
    if value is None:
        return 0.0
    if threshold <= 0:
        return 1.0
    return min(max(value / threshold, 0.0), 1.0)


def _distance_ratio(value: float | None, maximum: float) -> float:
    if value is None:
        return 0.0
    if maximum <= 0:
        return 1.0 if value <= maximum else 0.0
    if value <= maximum:
        return 1.0
    return min(max(maximum / value, 0.0), 1.0)


def _query_lexical_coverage(
    query: str, docs: Sequence[Mapping[str, Any]]
) -> float | None:
    """Measure exact meaningful query-term coverage in the retrieved evidence.

    BM25 can assign an IDF of zero in very small collections when a useful entity
    occurs in every chunk. Exact token coverage remains a trustworthy independent
    signal in that case; single-character tokens are excluded to avoid broad
    matches on particles and isolated numbers.
    """

    query_terms = {
        token.strip().casefold()
        for token in tokenize_mixed_text(query)
        if len(token.strip()) >= 2
    }
    if not query_terms:
        return None
    evidence_terms: set[str] = set()
    for doc in docs:
        meta = doc.get("meta") if isinstance(doc.get("meta"), Mapping) else {}
        searchable = "\n".join(
            str(value or "")
            for value in (
                doc.get("text"),
                meta.get("source"),
                meta.get("section_path"),
                meta.get("context"),
            )
        )
        evidence_terms.update(
            token.strip().casefold()
            for token in tokenize_mixed_text(searchable)
            if len(token.strip()) >= 2
        )
    return len(query_terms & evidence_terms) / len(query_terms)


def assess_retrieval_support(
    docs: Sequence[Mapping[str, Any]],
    settings: Settings | None = None,
    *,
    query: str = "",
    requirement_ids: Sequence[str] = (),
) -> RetrievalSupport:
    """Aggregate bounded candidate signals and enforce atomic-requirement coverage."""

    settings = settings or get_settings()
    if not docs:
        return RetrievalSupport(False, 0.0, "no_candidates", {})
    if not settings.qa_abstain_enabled:
        return RetrievalSupport(True, 1.0, "disabled", {})

    distances: list[float] = []
    bm25_scores: list[float] = []
    rerank_scores: list[float] = []
    knowledge_vector_scores: list[float] = []
    knowledge_lexical_scores: list[float] = []
    covered_requirements: set[str] = set()
    normalized_requirements = {
        str(requirement_id).strip()
        for requirement_id in requirement_ids
        if str(requirement_id).strip()
    }
    for doc in docs:
        meta = doc.get("meta") if isinstance(doc.get("meta"), Mapping) else {}
        retrieval = (
            doc.get("retrieval") if isinstance(doc.get("retrieval"), Mapping) else {}
        )
        matched = retrieval.get("matched_requirement_ids")
        if isinstance(matched, Sequence) and not isinstance(matched, (str, bytes)):
            covered_requirements.update(str(value).strip() for value in matched)
        rerank_score = _finite_float(retrieval.get("rerank_score"))
        if rerank_score is not None:
            rerank_scores.append(rerank_score)
        if meta.get("source_type") == "derived_knowledge":
            knowledge_score = _finite_float(
                retrieval.get("retrieval_score", retrieval.get("knowledge_score"))
            )
            channel = str(retrieval.get("search_channel") or "")
            if knowledge_score is not None:
                if channel == "derived_knowledge_embedding":
                    knowledge_vector_scores.append(knowledge_score)
                else:
                    knowledge_lexical_scores.append(knowledge_score)
            continue
        distance = _finite_float(retrieval.get("distance"))
        bm25_score = _finite_float(retrieval.get("bm25_score"))
        if distance is not None:
            distances.append(distance)
        if bm25_score is not None:
            bm25_scores.append(bm25_score)

    distance = min(distances) if distances else None
    bm25_score = max(bm25_scores) if bm25_scores else None
    knowledge_vector_score = (
        max(knowledge_vector_scores) if knowledge_vector_scores else None
    )
    knowledge_lexical_score = (
        max(knowledge_lexical_scores) if knowledge_lexical_scores else None
    )
    query_lexical_coverage = _query_lexical_coverage(query, docs) if query else None
    signals = {
        key: value
        for key, value in {
            "distance": distance,
            "bm25_score": bm25_score,
            "knowledge_vector_score": knowledge_vector_score,
            "knowledge_lexical_score": knowledge_lexical_score,
            "query_lexical_coverage": (
                query_lexical_coverage if query_lexical_coverage else None
            ),
            "rerank_score": max(rerank_scores) if rerank_scores else None,
            "rerank_margin": (
                max(rerank_scores) - sorted(rerank_scores, reverse=True)[1]
                if len(rerank_scores) > 1
                else None
            ),
        }.items()
        if value is not None
    }
    if not signals:
        supported = settings.qa_abstain_allow_missing_signals
        return RetrievalSupport(
            supported,
            1.0 if supported else 0.0,
            "signals_unavailable",
            {},
        )

    semantic_supported = (
        distance is not None and distance <= settings.qa_abstain_max_vector_distance
    )
    lexical_supported = (
        bm25_score is not None and bm25_score >= settings.qa_abstain_min_bm25_score
    )
    knowledge_vector_supported = (
        knowledge_vector_score is not None
        and knowledge_vector_score >= settings.qa_abstain_min_knowledge_vector_score
    )
    knowledge_lexical_supported = (
        knowledge_lexical_score is not None
        and knowledge_lexical_score >= settings.qa_abstain_min_knowledge_lexical_score
    )
    exact_query_terms_supported = query_lexical_coverage == 1.0
    supported = (
        semantic_supported
        or lexical_supported
        or knowledge_vector_supported
        or knowledge_lexical_supported
        or exact_query_terms_supported
    )
    score = max(
        _distance_ratio(distance, settings.qa_abstain_max_vector_distance),
        _threshold_ratio(bm25_score, settings.qa_abstain_min_bm25_score),
        _threshold_ratio(
            knowledge_vector_score,
            settings.qa_abstain_min_knowledge_vector_score,
        ),
        _threshold_ratio(
            knowledge_lexical_score,
            settings.qa_abstain_min_knowledge_lexical_score,
        ),
        1.0 if exact_query_terms_supported else 0.0,
    )
    if normalized_requirements:
        coverage = len(normalized_requirements & covered_requirements) / len(
            normalized_requirements
        )
        signals["requirement_coverage"] = coverage
        if coverage < 1.0:
            return RetrievalSupport(
                False, score, "requirement_coverage_incomplete", signals
            )
    return RetrievalSupport(
        supported,
        score,
        "supported" if supported else "below_threshold",
        signals,
    )
