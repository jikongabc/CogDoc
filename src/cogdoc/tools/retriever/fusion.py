from __future__ import annotations

import copy
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, cast

from cogdoc.graph.state import RetrievalMetrics, RetrievedDoc


@dataclass(frozen=True)
class RankedCandidateList:
    """One query/channel ranking participating in query-level fusion."""

    query: str
    channel: str
    docs: Sequence[RetrievedDoc]
    requirement_ids: Sequence[str] = ()
    is_original: bool = False
    retrieval_round: int = 0
    weight: float = 1.0


@dataclass
class _CandidateState:
    chunk_id: str
    contributions: list[float] = field(default_factory=list)
    matched_queries: list[str] = field(default_factory=list)
    matched_channels: list[str] = field(default_factory=list)
    channel_contributions: dict[str, float] = field(default_factory=dict)
    matched_requirement_ids: list[str] = field(default_factory=list)
    best_doc: RetrievedDoc | None = None
    best_query: str = ""
    best_channel: str = ""
    best_rank: int = 0
    best_is_original: bool = False
    best_ranking_index: int = 0
    original_query_hit: bool = False
    retrieval_round: int = 0


def _append_unique(values: list[str], incoming: Sequence[str]) -> None:
    seen = set(values)
    for value in incoming:
        normalized = str(value or "").strip()
        if normalized and normalized not in seen:
            seen.add(normalized)
            values.append(normalized)


def _candidate_chunk_id(doc: Mapping[str, Any]) -> str:
    meta = doc.get("meta")
    chunk_id = meta.get("chunk_id") if isinstance(meta, Mapping) else None
    normalized = str(chunk_id or "").strip()
    if not normalized:
        raise ValueError("ranked candidate is missing a stable chunk_id")
    return normalized


def _is_better_occurrence(
    candidate: _CandidateState,
    *,
    rank: int,
    is_original: bool,
    ranking_index: int,
) -> bool:
    if candidate.best_doc is None:
        return True
    return (rank, not is_original, ranking_index) < (
        candidate.best_rank,
        not candidate.best_is_original,
        candidate.best_ranking_index,
    )


def _materialize_candidate(candidate: _CandidateState) -> RetrievedDoc:
    if candidate.best_doc is None:  # pragma: no cover - internal invariant
        raise RuntimeError("fused candidate has no source occurrence")

    doc = copy.deepcopy(candidate.best_doc)
    raw_retrieval = doc.get("retrieval")
    retrieval: dict[str, Any] = (
        dict(raw_retrieval) if isinstance(raw_retrieval, Mapping) else {}
    )
    retrieval.setdefault("search_channel", candidate.best_channel)
    if candidate.best_is_original:
        retrieval.pop("rewrite_query", None)
    elif candidate.best_query:
        retrieval["rewrite_query"] = candidate.best_query

    retrieval.update(
        {
            "query_fusion_score": math.fsum(candidate.contributions),
            "query_hit_count": len(candidate.matched_queries),
            "matched_queries": list(candidate.matched_queries),
            "matched_channels": list(candidate.matched_channels),
            "channel_contributions": {
                channel: score
                for channel, score in candidate.channel_contributions.items()
            },
            "matched_requirement_ids": list(candidate.matched_requirement_ids),
            "best_query_rank": candidate.best_rank,
            "original_query_hit": candidate.original_query_hit,
            "retrieval_round": candidate.retrieval_round,
        }
    )
    doc["retrieval"] = cast(RetrievalMetrics, retrieval)
    return doc


def fuse_ranked_candidates(
    rankings: Sequence[RankedCandidateList],
    *,
    rrf_k: float,
    top_n: int | None = None,
    per_channel_min: int = 0,
) -> list[RetrievedDoc]:
    """Fuse independent rankings with deterministic weighted RRF."""

    if not math.isfinite(rrf_k) or rrf_k < 0:
        raise ValueError("rrf_k must be a finite non-negative number")
    if top_n is not None and top_n < 0:
        raise ValueError("top_n must be non-negative or None")
    if per_channel_min < 0:
        raise ValueError("per_channel_min must be non-negative")
    if top_n == 0:
        return []

    candidates: dict[str, _CandidateState] = {}
    for ranking_index, ranking in enumerate(rankings):
        weight = float(ranking.weight)
        if not math.isfinite(weight) or weight < 0:
            raise ValueError("ranking weight must be a finite non-negative number")
        if weight == 0:
            continue
        seen_in_ranking: set[str] = set()
        unique_rank = 0
        for doc in ranking.docs:
            chunk_id = _candidate_chunk_id(doc)
            if chunk_id in seen_in_ranking:
                continue
            seen_in_ranking.add(chunk_id)
            unique_rank += 1

            denominator = rrf_k + unique_rank
            if denominator <= 0:  # Only possible for rrf_k=0 and invalid rank.
                raise ValueError("rrf_k plus candidate rank must be positive")

            candidate = candidates.setdefault(
                chunk_id, _CandidateState(chunk_id=chunk_id)
            )
            contribution = weight / denominator
            candidate.contributions.append(contribution)
            candidate.channel_contributions[ranking.channel] = math.fsum(
                (
                    candidate.channel_contributions.get(ranking.channel, 0.0),
                    contribution,
                )
            )
            _append_unique(candidate.matched_queries, (ranking.query,))
            _append_unique(candidate.matched_channels, (ranking.channel,))
            _append_unique(candidate.matched_requirement_ids, ranking.requirement_ids)
            candidate.original_query_hit = (
                candidate.original_query_hit or ranking.is_original
            )
            candidate.retrieval_round = max(
                candidate.retrieval_round, ranking.retrieval_round
            )

            if _is_better_occurrence(
                candidate,
                rank=unique_rank,
                is_original=ranking.is_original,
                ranking_index=ranking_index,
            ):
                candidate.best_doc = doc
                candidate.best_query = ranking.query
                candidate.best_channel = ranking.channel
                candidate.best_rank = unique_rank
                candidate.best_is_original = ranking.is_original
                candidate.best_ranking_index = ranking_index

    ordered = sorted(
        candidates.values(),
        key=lambda candidate: (
            -math.fsum(candidate.contributions),
            candidate.chunk_id,
        ),
    )
    if top_n is not None and len(ordered) > top_n:
        selected: set[int] = set()
        if per_channel_min:
            channels: list[str] = []
            for ranking in rankings:
                _append_unique(channels, (ranking.channel,))
            for channel in channels:
                covered = sum(
                    1
                    for index in selected
                    if channel in ordered[index].matched_channels
                )
                for index, candidate in enumerate(ordered):
                    if covered >= per_channel_min or len(selected) >= top_n:
                        break
                    if index in selected or channel not in candidate.matched_channels:
                        continue
                    selected.add(index)
                    covered += 1
        for index in range(len(ordered)):
            if len(selected) >= top_n:
                break
            selected.add(index)
        ordered = [
            candidate for index, candidate in enumerate(ordered) if index in selected
        ]
    return [_materialize_candidate(candidate) for candidate in ordered]


def _matched_requirement_ids(doc: Mapping[str, Any]) -> set[str]:
    retrieval = doc.get("retrieval")
    if not isinstance(retrieval, Mapping):
        return set()
    values = retrieval.get("matched_requirement_ids")
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        return set()
    return {str(value).strip() for value in values if str(value).strip()}


def _matched_channels(doc: Mapping[str, Any]) -> set[str]:
    retrieval = doc.get("retrieval")
    if not isinstance(retrieval, Mapping):
        return set()
    values = retrieval.get("matched_channels")
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        return set()
    return {str(value).strip() for value in values if str(value).strip()}


def select_rerank_candidates(
    docs: Sequence[RetrievedDoc],
    *,
    max_candidates: int,
    requirement_ids: Sequence[str],
    per_requirement: int = 1,
    per_channel: int = 0,
) -> list[RetrievedDoc]:
    """Select a bounded pool with requirement and recall-route coverage."""

    if max_candidates < 0:
        raise ValueError("max_candidates must be non-negative")
    if per_requirement < 0:
        raise ValueError("per_requirement must be non-negative")
    if per_channel < 0:
        raise ValueError("per_channel must be non-negative")
    if max_candidates == 0:
        return []

    normalized_requirements: list[str] = []
    _append_unique(normalized_requirements, requirement_ids)
    matches = [_matched_requirement_ids(doc) for doc in docs]
    channel_matches = [_matched_channels(doc) for doc in docs]
    selected: set[int] = set()

    for requirement_id in normalized_requirements:
        covered = sum(1 for index in selected if requirement_id in matches[index])
        for index, doc_requirements in enumerate(matches):
            if covered >= per_requirement or len(selected) >= max_candidates:
                break
            if index in selected or requirement_id not in doc_requirements:
                continue
            selected.add(index)
            covered += 1

    channels: list[str] = []
    for doc_channels in channel_matches:
        _append_unique(channels, tuple(sorted(doc_channels)))
    for channel in channels:
        covered = sum(1 for index in selected if channel in channel_matches[index])
        for index, doc_channels in enumerate(channel_matches):
            if covered >= per_channel or len(selected) >= max_candidates:
                break
            if index in selected or channel not in doc_channels:
                continue
            selected.add(index)
            covered += 1

    for index in range(len(docs)):
        if len(selected) >= max_candidates:
            break
        selected.add(index)

    return [doc for index, doc in enumerate(docs) if index in selected]
