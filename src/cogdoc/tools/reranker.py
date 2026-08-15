import torch
import copy
import threading
from dataclasses import dataclass
from collections.abc import Mapping, Sequence
from typing import Any, List
from transformers import AutoModelForSequenceClassification, AutoTokenizer
from cogdoc.graph.state import RetrievedDoc
from cogdoc.tools.device import (
    model_inference_semaphore,
    required_cuda_free_bytes,
    resolve_device,
)


# 返回跳过 CPU 重排的候选文档。
def skipped_cpu_rerank_docs(
    docs: List[RetrievedDoc], top_n: int, reason: str = "cpu_disabled"
) -> List[RetrievedDoc]:
    selected = copy.deepcopy(docs[:top_n])
    for doc in selected:
        doc.setdefault("retrieval", {})["rerank_skipped_reason"] = reason
    return selected


@dataclass(frozen=True)
class RerankExecution:
    """The ranked documents and the runtime policy decision that produced them."""

    docs: List[RetrievedDoc]
    device: str
    skipped_reason: str = ""


# 定义 BGEReranker 数据结构。
class BGEReranker:
    _tokenizer = None  # Tokenizer单例
    _model = None  # 模型单例
    _models = {}  # 按设备缓存模型单例
    _lock = threading.RLock()  # 保护懒加载与设备缓存
    MODEL_NAME = "BAAI/bge-reranker-v2-m3"  # Reranker模型名称
    MAX_LENGTH = 512  # 模型单次处理的最大Token长度保护

    # bge-reranker-v2-m3 权重约 2.3G + 推理活化余量，空闲低于此值回落 CPU，避免 CUDA OOM。
    REQUIRED_CUDA_FREE_BYTES = required_cuda_free_bytes("RERANKER_MIN_CUDA_FREE_MB")

    device = None  # None=未显式指定，加载时按空闲显存自动判定；set_device 后固定

    # 设置 device。
    @classmethod
    def set_device(cls, device: str) -> None:
        with cls._lock:
            if device != cls.device:
                cls._model = cls._models.get(
                    device
                )  # 切换当前设备视图，不清空其它设备缓存
                cls.device = device

    # 完成 default设备 处理。
    @classmethod
    def default_device(cls) -> str:
        with cls._lock:
            current_device = cls.device
            model_loaded = cls._model is not None
            if current_device is None and "cuda" in cls._models:
                current_device = (
                    "cuda"  # 已加载 GPU 模型时继续复用，避免被自身显存占用误判。
                )
                model_loaded = True
            return resolve_device(
                cls.REQUIRED_CUDA_FREE_BYTES, current_device, model_loaded
            )

    # 获取 resources。
    @classmethod
    def _get_resources(cls, device: str | None = None):
        # Tokenizer 与模型按进程级单例懒加载。
        with cls._lock:
            explicit_device = device is not None
            target_device = device or cls.device
            if target_device is None:
                target_device = (
                    cls.default_device()
                )  # 直连调用也按显存选设备，不退化成 CPU
            if cls._tokenizer is None:
                cls._tokenizer = AutoTokenizer.from_pretrained(cls.MODEL_NAME)
            model = cls._models.get(target_device)
            if model is None:
                model = AutoModelForSequenceClassification.from_pretrained(
                    cls.MODEL_NAME
                )
                model.to(target_device)
                model.eval()
                cls._models[target_device] = model
            if not explicit_device:
                cls.device = target_device
                cls._model = model
            elif cls.device == target_device:
                cls._model = model
            return cls._tokenizer, model, target_device

    # 完成 预热流程预热流程 处理。
    @classmethod
    def warm_up(cls) -> None:
        cls._get_resources()

    # 重排。
    @classmethod
    def rerank(
        cls,
        query: str,
        docs: List[RetrievedDoc],
        top_n: int = 3,
        device: str | None = None,
    ) -> List[RetrievedDoc]:
        # 精排只修改深拷贝结果，避免污染召回缓存。
        if not docs:
            return []  # 无候选文档直接返回
        if len(docs) <= 1:
            return copy.deepcopy(docs)[:top_n]  # 单文档无需精排

        pairs = [(query, str(doc.get("text") or "")) for doc in docs]
        scores = cls.score_pairs(pairs, device=device)

        ranked_docs: List[RetrievedDoc] = []
        for idx, score in enumerate(scores):
            doc_copy = copy.deepcopy(docs[idx])  # 深拷贝避免污染原数据

            retrieval_meta = doc_copy.setdefault(
                "retrieval", {}
            )  # 获取或创建检索元数据
            retrieval_meta["rerank_score"] = float(score)  # 写入精排得分

            ranked_docs.append(doc_copy)

        ranked_docs.sort(
            key=lambda x: x["retrieval"]["rerank_score"], reverse=True
        )  # 按精排得分降序排序

        return ranked_docs[:top_n]  # 返回TopN结果

    @classmethod
    def score_pairs(
        cls,
        pairs: Sequence[tuple[str, str]],
        *,
        device: str | None = None,
    ) -> list[float]:
        """Score arbitrary query/chunk pairs in one cross-encoder forward pass."""

        if not pairs:
            return []
        with model_inference_semaphore("reranker"):
            tokenizer, model, target_device = cls._get_resources(device)
            with torch.no_grad():
                inputs = tokenizer(
                    [[query, text] for query, text in pairs],
                    padding=True,
                    truncation=True,
                    max_length=cls.MAX_LENGTH,
                    return_tensors="pt",
                ).to(target_device)
                outputs = model(**inputs, return_dict=True)
                return [
                    float(score)
                    for score in outputs.logits.view(-1).float().cpu().tolist()
                ]


def dynamic_rerank_top_n(
    *,
    base_top_n: int,
    max_docs: int,
    requirement_count: int,
    docs_per_requirement: int,
) -> int:
    """Expand anchor capacity only when atomic evidence requirements need it."""

    if min(base_top_n, max_docs, requirement_count, docs_per_requirement) < 0:
        raise ValueError("rerank limits must be non-negative")
    required = min(max_docs, requirement_count * docs_per_requirement)
    # Never silently shrink the explicitly configured base anchor count.  If it
    # exceeds the evidence-pack budget, the existing hard-budget gate must fail
    # closed instead of hiding a configuration error.
    return max(base_top_n, required)


def requirement_query_map(
    requirements: Sequence[Mapping[str, Any]],
) -> dict[str, str]:
    result: dict[str, str] = {}
    for requirement in requirements:
        requirement_id = str(requirement.get("requirement_id") or "").strip()
        query = str(
            requirement.get("question") or requirement.get("retrieval_query") or ""
        ).strip()
        if requirement_id and query:
            result[requirement_id] = query
    return result


def _matched_requirements(doc: Mapping[str, Any]) -> set[str]:
    retrieval = doc.get("retrieval")
    values = (
        retrieval.get("matched_requirement_ids")
        if isinstance(retrieval, Mapping)
        else ()
    )
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        return set()
    return {str(value).strip() for value in values if str(value).strip()}


def _requirement_first_order(
    docs: Sequence[RetrievedDoc],
    requirement_ids: Sequence[str],
    *,
    per_requirement: int,
) -> list[int]:
    """Reserve the best attributed candidates, then fill by global relevance."""

    selected: list[int] = []
    selected_set: set[int] = set()
    matches = [_matched_requirements(doc) for doc in docs]
    for requirement_id in requirement_ids:
        eligible = [
            index for index, matched in enumerate(matches) if requirement_id in matched
        ]
        eligible.sort(
            key=lambda index: float(
                docs[index]
                .get("retrieval", {})
                .get("requirement_rerank_scores", {})
                .get(requirement_id, float("-inf"))
            ),
            reverse=True,
        )
        for index in eligible[:per_requirement]:
            if index not in selected_set:
                selected.append(index)
                selected_set.add(index)
    selected.extend(index for index in range(len(docs)) if index not in selected_set)
    return selected


def rerank_with_requirement_policy(
    *,
    query: str,
    docs: List[RetrievedDoc],
    requirement_queries: Mapping[str, str],
    top_n: int,
    allow_cpu: bool,
    per_requirement: int = 1,
) -> RerankExecution:
    """Batch global and per-requirement cross-encoder scores under one device policy."""

    if not docs or top_n <= 0:
        return RerankExecution([], BGEReranker.default_device())
    target_device = BGEReranker.default_device()
    requirement_ids = list(requirement_queries)
    if target_device == "cpu" and not allow_cpu:
        skipped = skipped_cpu_rerank_docs(docs, len(docs), "cpu_disabled")
        order = _requirement_first_order(
            skipped, requirement_ids, per_requirement=per_requirement
        )
        return RerankExecution(
            [skipped[index] for index in order[:top_n]],
            target_device,
            "cpu_disabled",
        )

    pairs: list[tuple[str, str]] = [(query, str(doc.get("text") or "")) for doc in docs]
    requirement_pair_keys: list[tuple[int, str]] = []
    for index, doc in enumerate(docs):
        matched = _matched_requirements(doc)
        for requirement_id in requirement_ids:
            if requirement_id in matched:
                pairs.append(
                    (requirement_queries[requirement_id], str(doc.get("text") or ""))
                )
                requirement_pair_keys.append((index, requirement_id))
    scores = BGEReranker.score_pairs(pairs, device=target_device)
    ranked: list[RetrievedDoc] = []
    for index, doc in enumerate(docs):
        copied = copy.deepcopy(doc)
        copied.setdefault("retrieval", {})["rerank_score"] = scores[index]
        copied["retrieval"]["requirement_rerank_scores"] = {}
        ranked.append(copied)
    offset = len(docs)
    for pair_offset, (index, requirement_id) in enumerate(requirement_pair_keys):
        ranked[index]["retrieval"]["requirement_rerank_scores"][requirement_id] = (
            scores[offset + pair_offset]
        )
    ranked.sort(
        key=lambda doc: float(
            doc.get("retrieval", {}).get("rerank_score", float("-inf"))
        ),
        reverse=True,
    )
    order = _requirement_first_order(
        ranked, requirement_ids, per_requirement=per_requirement
    )
    return RerankExecution([ranked[index] for index in order[:top_n]], target_device)


def rerank_with_device_policy(
    *,
    query: str,
    docs: List[RetrievedDoc],
    top_n: int,
    allow_cpu: bool,
) -> RerankExecution:
    """Apply the production CPU/GPU policy and expose the actual execution path.

    Keeping this decision next to the reranker prevents offline evaluation from
    silently running a CPU cross-encoder that production is configured to skip.
    """

    target_device = BGEReranker.default_device()
    if target_device == "cpu" and not allow_cpu:
        reason = "cpu_disabled"
        return RerankExecution(
            docs=skipped_cpu_rerank_docs(docs, top_n, reason),
            device=target_device,
            skipped_reason=reason,
        )
    return RerankExecution(
        docs=BGEReranker.rerank(
            query=query,
            docs=docs,
            top_n=top_n,
            device=target_device,
        ),
        device=target_device,
    )
