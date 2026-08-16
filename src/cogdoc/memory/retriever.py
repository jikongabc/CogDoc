from __future__ import annotations

import hashlib
import math
from collections import Counter, OrderedDict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from threading import RLock
from typing import Any

from cogdoc.memory.manager import (
    MemoryPolicy,
    assemble_memory_context,
    build_memory_context,
)
from cogdoc.tools.tokenizer import tokenize_mixed_text


EmbeddingFunction = Callable[[list[str]], Sequence[Sequence[float]]]


@dataclass(frozen=True)
class MemoryRetrievalResult:
    """Auditable result for the conversation-memory trust domain."""

    context: list[dict[str, Any]]
    channel_counts: dict[str, int]
    selected_tier_counts: dict[str, int]


# 调用默认嵌入模型。
def _default_embed(texts: list[str]) -> Sequence[Sequence[float]]:
    from cogdoc.tools.embedder import Embedder

    return Embedder.embed_documents(texts)


# 生成文本缓存键。
def _text_key(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# 计算余弦相似度。
def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    if not left or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm <= 0.0 or right_norm <= 0.0:
        return 0.0
    return dot / (left_norm * right_norm)


# 构造短期记忆候选。
def _short_candidates(messages: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    candidates = []
    for index, message in enumerate(messages):
        content = str(message.get("content", "") or "").strip()
        if content:
            candidates.append(
                {
                    "id": f"short:{index}",
                    "tier": "short",
                    "content": content,
                    "order": index,
                    "payload": dict(message),
                }
            )
    return candidates


# 构造中期记忆候选。
def _mid_candidates(mid_term: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    if not mid_term:
        return []
    candidates = []
    for field, priority in (("goals", 1.0), ("decisions", 1.0), ("summary", 0.6)):
        values = list(mid_term.get(field, []) or [])
        for index, value in enumerate(values):
            content = str(value or "").strip()
            if content:
                candidates.append(
                    {
                        "id": f"mid:{field}:{index}",
                        "tier": "mid",
                        "field": field,
                        "content": content,
                        "order": index,
                        "priority": priority,
                    }
                )
    return candidates


# 构造长期记忆候选。
def _long_candidates(facts: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    candidates = []
    for index, fact in enumerate(facts):
        content = str(fact.get("content", "") or "").strip()
        if content:
            candidates.append(
                {
                    "id": f"long:{fact.get('id') or index}",
                    "tier": "long",
                    "content": content,
                    "order": index,
                    "importance": float(fact.get("importance", 0.0) or 0.0),
                    "updated_at": float(fact.get("updated_at", 0.0) or 0.0),
                    "payload": dict(fact),
                }
            )
    return candidates


# 按关键词相关性排序候选。
def _lexical_rank(
    query: str, candidates: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    query_tokens: Counter[str] = Counter(tokenize_mixed_text(query))
    if not query_tokens or not candidates:
        return []
    documents: list[Counter[str]] = [
        Counter(tokenize_mixed_text(str(item["content"]))) for item in candidates
    ]
    document_count = len(documents)
    document_frequency: Counter[str] = Counter()
    for tokens in documents:
        document_frequency.update(tokens.keys())
    scored: list[tuple[float, dict[str, Any]]] = []
    for candidate, tokens in zip(candidates, documents):
        score = 0.0
        for token, query_frequency in query_tokens.items():
            frequency = tokens.get(token, 0)
            if frequency <= 0:
                continue
            inverse_frequency = math.log(
                1.0
                + (document_count - document_frequency[token] + 0.5)
                / (document_frequency[token] + 0.5)
            )
            score += inverse_frequency * query_frequency * frequency / (frequency + 1.2)
        if score > 0.0:
            scored.append((score, dict(candidate)))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [candidate for _, candidate in scored]


# 管理查询感知的多路记忆召回。
class MemoryRetriever:
    # 初始化记忆召回器。
    def __init__(
        self,
        policy: MemoryPolicy,
        embedding_fn: EmbeddingFunction | None = None,
    ):
        self.policy = policy
        self.embedding_fn = embedding_fn or _default_embed
        self._vector_cache: OrderedDict[str, list[float]] = OrderedDict()
        self._vector_cache_limit = 4096
        self._lock = RLock()

    # 批量读取并缓存文本向量。
    def _vectors(self, texts: Sequence[str]) -> list[list[float]]:
        keys = [_text_key(text) for text in texts]
        missing_texts = []
        missing_keys = []
        with self._lock:
            for key, value in zip(keys, texts):
                if key in self._vector_cache:
                    self._vector_cache.move_to_end(key)
                else:
                    missing_keys.append(key)
                    missing_texts.append(value)
        if missing_texts:
            vectors = self.embedding_fn(missing_texts)
            if len(vectors) != len(missing_texts):
                raise ValueError("记忆嵌入数量不匹配")
            with self._lock:
                for key, vector in zip(missing_keys, vectors):
                    self._vector_cache[key] = [float(value) for value in vector]
                    self._vector_cache.move_to_end(key)
                while len(self._vector_cache) > self._vector_cache_limit:
                    self._vector_cache.popitem(last=False)
        with self._lock:
            return [list(self._vector_cache[key]) for key in keys]

    # 按语义相关性排序候选。
    def _semantic_rank(
        self, query: str, candidates: Sequence[Mapping[str, Any]]
    ) -> list[dict[str, Any]]:
        if not self.policy.memory_semantic_enabled or not candidates:
            return []
        candidate_texts = [str(candidate["content"]) for candidate in candidates]
        try:
            query_vectors = self.embedding_fn([query])
            if len(query_vectors) != 1:
                raise ValueError("查询嵌入数量不匹配")
            query_vector = [float(value) for value in query_vectors[0]]
            candidate_vectors = self._vectors(candidate_texts)
        except Exception:
            return []
        scored = [
            (_cosine(query_vector, vector), dict(candidate))
            for candidate, vector in zip(candidates, candidate_vectors)
        ]
        scored = [item for item in scored if item[0] > 0.0]
        scored.sort(key=lambda item: item[0], reverse=True)
        return [candidate for _, candidate in scored]

    # 将单路排名融合到候选得分。
    def _merge_channel(
        self,
        scores: dict[str, float],
        ranked: Sequence[Mapping[str, Any]],
        weight: float,
    ) -> None:
        for rank, candidate in enumerate(ranked, start=1):
            candidate_id = str(candidate["id"])
            scores[candidate_id] = scores.get(candidate_id, 0.0) + weight / (
                self.policy.memory_rrf_k + rank
            )

    # 从融合结果选择分层候选。
    def _select(
        self,
        candidates: Sequence[Mapping[str, Any]],
        scores: Mapping[str, float],
        tier: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        selected = [dict(item) for item in candidates if item.get("tier") == tier]
        selected.sort(
            key=lambda item: (scores.get(str(item["id"]), 0.0), item.get("order", 0)),
            reverse=True,
        )
        return selected[: max(0, limit)]

    # 选择短期候选并保证最新上下文连续性。
    def _select_short(
        self,
        candidates: Sequence[Mapping[str, Any]],
        scores: Mapping[str, float],
        limit: int,
    ) -> list[dict[str, Any]]:
        limit = max(0, limit)
        if limit <= 0:
            return []
        short = [dict(item) for item in candidates if item.get("tier") == "short"]
        recent_count = min(self.policy.memory_retrieval_recent_pin, limit, len(short))
        selected = short[-recent_count:] if recent_count else []
        selected_ids = {str(item["id"]) for item in selected}
        remaining = [item for item in short if str(item["id"]) not in selected_ids]
        remaining.sort(
            key=lambda item: (scores.get(str(item["id"]), 0.0), item["order"]),
            reverse=True,
        )
        selected.extend(remaining[: limit - len(selected)])
        selected.sort(key=lambda item: item["order"])
        return selected

    # 执行短期、中期和长期多路召回。
    def retrieve(
        self,
        query: str,
        short_term: Sequence[Mapping[str, Any]],
        mid_term: Mapping[str, Any] | None,
        long_term_facts: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        return self.retrieve_result(
            query,
            short_term,
            mid_term,
            long_term_facts,
        ).context

    def retrieve_result(
        self,
        query: str,
        short_term: Sequence[Mapping[str, Any]],
        mid_term: Mapping[str, Any] | None,
        long_term_facts: Sequence[Mapping[str, Any]],
    ) -> MemoryRetrievalResult:
        """Retrieve memory routes while keeping memory separate from evidence."""

        if not query.strip() or not self.policy.memory_retrieval_enabled:
            context = build_memory_context(
                short_term,
                mid_term,
                long_term_facts,
                self.policy,
            )
            return MemoryRetrievalResult(
                context=context,
                channel_counts={"memory_static": len(context)},
                selected_tier_counts={
                    "short": sum(
                        1 for message in context if message.get("role") != "memory"
                    ),
                    "mid": sum(
                        1
                        for message in context
                        if str(message.get("content", "")).startswith("【中期记忆】")
                    ),
                    "long": sum(
                        1
                        for message in context
                        if str(message.get("content", "")).startswith("【长期记忆】")
                    ),
                },
            )
        short = _short_candidates(short_term)
        mid = _mid_candidates(mid_term)
        long = _long_candidates(long_term_facts)
        all_candidates = [*short, *mid, *long]
        scores: dict[str, float] = {}
        recency_ranked = list(reversed(short))
        lexical_ranked = _lexical_rank(query, all_candidates)
        self._merge_channel(scores, recency_ranked, self.policy.memory_recency_weight)
        self._merge_channel(scores, lexical_ranked, self.policy.memory_lexical_weight)
        semantic_candidates = [*mid, *long]
        if self.policy.memory_semantic_include_short:
            semantic_candidates = [*short, *semantic_candidates]
        semantic_ranked = self._semantic_rank(query, semantic_candidates)
        importance_ranked = sorted(
            long,
            key=lambda item: (item["importance"], item["updated_at"]),
            reverse=True,
        )
        mid_priority_ranked = sorted(
            mid,
            key=lambda item: (item["priority"], item["order"]),
            reverse=True,
        )
        self._merge_channel(scores, semantic_ranked, self.policy.memory_semantic_weight)
        self._merge_channel(
            scores, importance_ranked, self.policy.memory_importance_weight
        )
        self._merge_channel(
            scores, mid_priority_ranked, self.policy.memory_mid_priority_weight
        )
        selected_mid = self._select(
            all_candidates,
            scores,
            "mid",
            self.policy.memory_retrieval_mid_limit,
        )
        selected_long = self._select(
            all_candidates,
            scores,
            "long",
            self.policy.context_long_term_limit,
        )
        memory_count = int(bool(selected_mid)) + int(bool(selected_long))
        short_limit = min(
            self.policy.memory_retrieval_short_limit,
            max(0, self.policy.short_term_message_limit - memory_count),
        )
        selected_short = self._select_short(all_candidates, scores, short_limit)
        selected_mid.sort(key=lambda item: (item["field"], item["order"]))
        selected_mid_state: dict[str, list[str]] = {
            "goals": [],
            "decisions": [],
            "summary": [],
        }
        for item in selected_mid:
            selected_mid_state[item["field"]].append(item["content"])
        context = assemble_memory_context(
            [item["payload"] for item in selected_short],
            selected_mid_state,
            [item["payload"] for item in selected_long],
        )
        return MemoryRetrievalResult(
            context=context,
            channel_counts={
                "memory_recency": len(recency_ranked),
                "memory_lexical": len(lexical_ranked),
                "memory_semantic": len(semantic_ranked),
                "memory_long_importance": len(importance_ranked),
                "memory_mid_priority": len(mid_priority_ranked),
            },
            selected_tier_counts={
                "short": len(selected_short),
                "mid": len(selected_mid),
                "long": len(selected_long),
            },
        )
