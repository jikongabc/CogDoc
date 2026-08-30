import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from cogdoc.config.settings import Settings, get_settings
from cogdoc.tools.tokenizer import tokenize_mixed_text


# 疑问词描述的是用户想要的答案形态，并不是应当在证据中逐字出现的实体。
# 仅在精确词覆盖信号中忽略它们；BM25、向量召回与重排仍使用完整原始查询。
_QUERY_FUNCTION_WORDS = frozenset(
    {
        "什么",
        "为何",
        "为什么",
        "怎么",
        "怎样",
        "如何",
        "是否",
        "哪里",
        "哪个",
        "哪些",
        "哪种",
        "何时",
        "多少",
        # English tokens are stemmed by ``tokenize_mixed_text`` (for example,
        # ``why`` -> ``whi`` and ``many`` -> ``mani``).  These interrogatives
        # describe the requested answer shape rather than the topic that must
        # occur verbatim in evidence.
        "who",
        "what",
        "when",
        "where",
        "which",
        "whether",
        "whi",
        "how",
        "mani",
        "much",
        "can",
        "could",
        "should",
        "would",
        "will",
        "doe",
        "did",
    }
)


def _coverage_terms(text: str) -> set[str]:
    terms: set[str] = set()
    for token in tokenize_mixed_text(text):
        normalized = token.strip().casefold()
        if len(normalized) < 2:
            continue
        terms.add(normalized)
        # 标识符分词会保留连字符/句点；拆分后的纯英文组件还要重新走同一
        # stemmer，否则查询末尾 ``Apple?`` 得到 ``appl``，而证据末尾
        # ``Apple.`` 拆出原始 ``apple``，相同词会被误判为未覆盖。
        for part in re.split(r"[-_.]+", normalized):
            if len(part) < 2:
                continue
            terms.add(part)
            terms.update(
                stemmed.strip().casefold()
                for stemmed in tokenize_mixed_text(part)
                if len(stemmed.strip()) >= 2
            )
    return terms


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

    query_terms = _coverage_terms(query) - _QUERY_FUNCTION_WORDS
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
        evidence_terms.update(_coverage_terms(searchable))
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
            # Zero is an observed negative signal, not a missing signal.  Keep
            # it so the legacy ``allow_missing_signals`` switch cannot turn a
            # known lexical mismatch into confidence 1.0.
            "query_lexical_coverage": query_lexical_coverage,
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
    exact_query_terms_covered = query_lexical_coverage == 1.0
    supported = (
        semantic_supported
        or lexical_supported
        or knowledge_vector_supported
        or knowledge_lexical_supported
    )
    # Exact term overlap proves topicality, not that the evidence answers the
    # question.  Keep it at the verifier admission threshold so small-corpus
    # BM25 failures can be recovered by the closed-set evidence verifier without
    # turning a mention, negation, or empty field into confidence 1.0.
    lexical_verification_score = (
        settings.qa_evidence_verify_borderline_min_score
        if exact_query_terms_covered
        else 0.0
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
        lexical_verification_score,
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
    reason = (
        "supported"
        if supported
        else (
            "lexical_coverage_requires_verification"
            if exact_query_terms_covered
            else "below_threshold"
        )
    )
    return RetrievalSupport(supported, score, reason, signals)
