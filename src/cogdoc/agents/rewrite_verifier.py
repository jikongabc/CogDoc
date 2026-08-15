import json
from collections.abc import Mapping
from typing import Any, List, Tuple

from cogdoc.agents.conversation_memory import format_recent_chat_history
from cogdoc.tools.embedder import Embedder


# 关键词改写与自然句问题的相似度阈值需用真实数据继续标定。
DEFAULT_SIMILARITY_THRESHOLD = 0.5
# 相似度基准只取最近 1-2 轮，足以提供指代对象又不过度稀释当前问题语义。
SIMILARITY_BASELINE_HISTORY_LIMIT = 4


# 完成 余弦相似度归一化值 处理。
def _cosine_normalized(a: List[float], b: List[float]) -> float:
    # 输入向量已由 Embedder 做 L2 归一化，cosine 等于点积。
    if not a or not b or len(a) != len(b):
        return 0.0
    return sum(x * y for x, y in zip(a, b))


# 过滤重写问题by相似度。
def filter_rewrites_by_similarity(
    original_vec: List[float],
    rewrite_vecs: List[List[float]],
    rewrites: List[str],
    threshold: float,
) -> Tuple[List[str], List[Tuple[str, float]]]:
    # 纯函数只负责按相似度过滤改写，便于脱离模型测试。
    kept: List[str] = []
    dropped: List[Tuple[str, float]] = []

    for idx, text in enumerate(rewrites):
        vec = rewrite_vecs[idx] if idx < len(rewrite_vecs) else []
        sim = _cosine_normalized(original_vec, vec)
        if sim >= threshold:
            kept.append(text)
        else:
            dropped.append((text, sim))

    return kept, dropped


def _normalize_requirement(
    raw: Mapping[str, Any], position: int
) -> dict[str, str] | None:
    question = " ".join(str(raw.get("question") or "").split())
    if not question:
        return None
    return {
        "requirement_id": str(raw.get("requirement_id") or f"r{position + 1}"),
        "question": question,
        "retrieval_query": " ".join(str(raw.get("retrieval_query") or "").split())
        or question,
        "recovery_query": " ".join(str(raw.get("recovery_query") or "").split())
        or question,
    }


def _original_requirement(query: str) -> dict[str, str]:
    return {
        "requirement_id": "r1",
        "question": query,
        "retrieval_query": query,
        "recovery_query": query,
    }


def _vector_at(vectors: list[list[float]], index: int) -> list[float]:
    return vectors[index] if index < len(vectors) else []


# 定义 RewriteVerifyAgent 数据结构。
class RewriteVerifyAgent:
    # 校验 rewrites。
    @staticmethod
    def verify_rewrites(state: Mapping[str, Any]) -> dict[str, Any]:
        # 全部改写被过滤时返回空列表，retrieve 节点会只用原问题检索。
        query = state.get("query", "")
        rewrites = list(state.get("rewritten_queries", []))
        raw_requirements = state.get("evidence_requirements") or []
        requirements = [
            normalized
            for position, raw in enumerate(list(raw_requirements)[:3])
            if isinstance(raw, Mapping)
            and (normalized := _normalize_requirement(raw, position)) is not None
        ]
        threshold = state.get(
            "rewrite_similarity_threshold", DEFAULT_SIMILARITY_THRESHOLD
        )

        if not query:
            output = {"rewritten_queries": rewrites}
            if raw_requirements:
                output["evidence_requirements"] = requirements
            return output
        if not rewrites and not raw_requirements:
            return {"rewritten_queries": rewrites}
        if state.get("query_rewrite_fast_path"):
            output: dict[str, Any] = {
                "rewritten_queries": rewrites,
                "query_rewrite_fast_path": True,
                "steps_trace": [
                    {
                        "step_name": "verify_rewrite",
                        "input_summary": json.dumps(rewrites, ensure_ascii=False),
                        "output_summary": json.dumps(
                            {"fast_path": True, "kept": rewrites},
                            ensure_ascii=False,
                        ),
                    }
                ],
            }
            if raw_requirements:
                output["evidence_requirements"] = requirements or [
                    _original_requirement(query)
                ]
            return output

        # 有历史时用近期对话补全相似度基准，避免指代改写被裸省略句误杀。
        history_text = format_recent_chat_history(
            state.get("chat_history"), limit=SIMILARITY_BASELINE_HISTORY_LIMIT
        )
        baseline_text = f"{history_text}\n{query}" if history_text else query

        # 改写、需求问题与两类需求查询在一次 embedding 中批量校验。
        texts = [baseline_text] + rewrites
        requirement_vector_indexes: list[tuple[int, int, int]] = []
        for requirement in requirements:
            question_index = len(texts)
            texts.append(requirement["question"])
            retrieval_index = len(texts)
            texts.append(requirement["retrieval_query"])
            recovery_index = len(texts)
            texts.append(requirement["recovery_query"])
            requirement_vector_indexes.append(
                (question_index, retrieval_index, recovery_index)
            )

        vectors = Embedder.embed_documents(texts)
        original_vec = vectors[0] if vectors else []
        rewrite_vecs = [
            _vector_at(vectors, index) for index in range(1, len(rewrites) + 1)
        ]

        kept, dropped = filter_rewrites_by_similarity(
            original_vec,
            rewrite_vecs,
            rewrites,
            threshold,
        )

        kept_requirements: list[dict[str, str]] = []
        dropped_requirements: list[dict[str, Any]] = []
        requirement_query_fallbacks: list[dict[str, Any]] = []
        for requirement, vector_indexes in zip(
            requirements, requirement_vector_indexes
        ):
            question_vec = _vector_at(vectors, vector_indexes[0])
            question_similarity = _cosine_normalized(original_vec, question_vec)
            if question_similarity < threshold:
                dropped_requirements.append(
                    {
                        "requirement_id": requirement["requirement_id"],
                        "question": requirement["question"],
                        "similarity": round(question_similarity, 4),
                    }
                )
                continue

            guarded = dict(requirement)
            for field, vector_index in (
                ("retrieval_query", vector_indexes[1]),
                ("recovery_query", vector_indexes[2]),
            ):
                similarity = _cosine_normalized(
                    question_vec, _vector_at(vectors, vector_index)
                )
                if similarity < threshold:
                    requirement_query_fallbacks.append(
                        {
                            "requirement_id": requirement["requirement_id"],
                            "field": field,
                            "query": requirement[field],
                            "similarity": round(similarity, 4),
                        }
                    )
                    guarded[field] = requirement["question"]
            kept_requirements.append(guarded)

        requirement_fallback_used = bool(raw_requirements and not kept_requirements)
        if requirement_fallback_used:
            kept_requirements = [_original_requirement(query)]

        trace_payload = {
            "threshold": threshold,
            "kept": kept,
            "dropped": [
                {"query": text, "similarity": round(score, 4)}
                for text, score in dropped
            ],
        }
        if raw_requirements:
            trace_payload["requirement_guard"] = {
                "kept": kept_requirements,
                "dropped": dropped_requirements,
                "query_fallbacks": requirement_query_fallbacks,
                "original_fallback_used": requirement_fallback_used,
            }

        output = {
            "rewritten_queries": kept,
            "steps_trace": [
                {
                    "step_name": "verify_rewrite",
                    "input_summary": json.dumps(rewrites, ensure_ascii=False),
                    "output_summary": json.dumps(trace_payload, ensure_ascii=False),
                }
            ],
        }
        if raw_requirements:
            output["evidence_requirements"] = kept_requirements
        return output
