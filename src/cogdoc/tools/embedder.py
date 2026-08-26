import math
import threading
import time
from urllib.parse import urlparse

import httpx
from sentence_transformers import SentenceTransformer
from typing import List
from cogdoc.config.settings import get_settings
from cogdoc.tools.device import (
    model_inference_semaphore,
    required_cuda_free_bytes,
    resolve_device,
)


# 单例模型，整个程序只加载一次。
class Embedder:
    PROFILE_ID = "local"
    DISPLAY_NAME = "BGE-M3"
    _model = None
    _lock = threading.RLock()
    MODEL_NAME = "BAAI/bge-m3"
    # 固定到 HF commit SHA：分支会移动（远端更新权重后契约不变但模型已变），SHA 才能真正钉死权重版本。 升级权重必须改此 SHA，连带 EMBEDDING_CONTRACT_VERSION 变化使旧向量失效、强制全量重建。
    MODEL_REVISION = "5617a9f61b028005a4858fdac845db406aefb181"
    EMBEDDING_DIM = 1024  # bge-m3 dense 输出维度，编码后强校验
    NORMALIZE = True  # 归一化方式，影响距离度量，变更即不可复用旧向量

    # 嵌入兼容契约：模型名/revision/维度/归一化任一变化都使旧向量不可复用，强制全量重建。
    EMBEDDING_CONTRACT_VERSION = (
        f"{MODEL_NAME}@{MODEL_REVISION}|dim={EMBEDDING_DIM}|norm={NORMALIZE}"
    )

    # bge-m3 权重约 2.2G + 批量活化余量，空闲低于此值回落 CPU，避免 CUDA OOM。
    REQUIRED_CUDA_FREE_BYTES = required_cuda_free_bytes("EMBEDDER_MIN_CUDA_FREE_MB")

    device = "cpu"  # 实际设备在首次加载时按空闲显存动态判定，默认安全回落 CPU

    # 加载模型：pin revision，使契约声明的权重版本约束实际加载。
    @classmethod
    def get_model(cls) -> SentenceTransformer:
        with cls._lock:
            if cls._model is None:
                cls.device = resolve_device(cls.REQUIRED_CUDA_FREE_BYTES)
                cls._model = SentenceTransformer(
                    cls.MODEL_NAME, device=cls.device, revision=cls.MODEL_REVISION
                )
            return cls._model

    # 校验嵌入向量。
    @classmethod
    def validate_embeddings(cls, embeddings) -> None:
        # 统一校验：逐个向量维度等于契约值，且数值全为有限值（拒绝 NaN/Inf）。 编码后与跨代复用写入前共用，绝不让不兼容或污染的向量进入索引。
        for vector in embeddings:
            if len(vector) != cls.EMBEDDING_DIM:
                raise ValueError(
                    f"embedding dim {len(vector)} != contract {cls.EMBEDDING_DIM}: "
                    "嵌入契约与实际模型不符"
                )
            if not all(math.isfinite(value) for value in vector):
                raise ValueError("embedding contains non-finite value (NaN/Inf)")

    # 问题向量化。
    @classmethod
    def embed_query(cls, text: str) -> List[float]:
        with model_inference_semaphore("embedder"):
            vector = (
                cls.get_model()
                .encode([text], normalize_embeddings=cls.NORMALIZE)[0]
                .tolist()
            )
        cls.validate_embeddings([vector])
        return vector

    # 批量问题向量化；与单问题入口保持相同的 query 编码契约。
    @classmethod
    def embed_queries(cls, texts: List[str]) -> List[List[float]]:
        if not texts:
            return []
        with model_inference_semaphore("embedder"):
            vectors = (
                cls.get_model()
                .encode(
                    texts,
                    batch_size=64,
                    normalize_embeddings=cls.NORMALIZE,
                    show_progress_bar=False,
                )
                .tolist()
            )
        cls.validate_embeddings(vectors)
        return vectors

    # 文档向量化。
    @classmethod
    def embed_documents(cls, texts: List[str]) -> List[List[float]]:
        with model_inference_semaphore("embedder"):
            vectors = (
                cls.get_model()
                .encode(
                    texts,
                    batch_size=64,
                    normalize_embeddings=cls.NORMALIZE,
                    show_progress_bar=False,
                )
                .tolist()
            )
        cls.validate_embeddings(vectors)
        return vectors


class CloudEmbedder:
    """Server-owned OpenAI-compatible embedding backend.

    This class deliberately exposes the same class-level contract as Embedder,
    so retrieval/index components can receive either backend without knowing
    where inference runs. Secrets are resolved for each request and never enter
    index metadata or API responses.
    """

    PROFILE_ID = "cloud"
    DISPLAY_NAME = "Cloud Embedding"
    NORMALIZE = True

    @classmethod
    def _settings(cls):
        return get_settings()

    @classmethod
    def is_configured(cls) -> bool:
        settings = cls._settings()
        return bool(
            settings.cloud_embedding_api_key.strip()
            and settings.cloud_embedding_base_url.strip()
            and settings.cloud_embedding_model.strip()
        )

    @classmethod
    def _base_fingerprint(cls) -> str:
        parsed = urlparse(cls._settings().cloud_embedding_base_url)
        authority = f"{parsed.scheme.lower()}://{parsed.netloc.lower()}{parsed.path.rstrip('/')}"
        # The endpoint participates in the compatibility contract because two
        # providers may serve different weights under the same model alias.
        import hashlib

        return hashlib.sha256(authority.encode()).hexdigest()[:16]

    @classmethod
    def model_name(cls) -> str:
        return cls._settings().cloud_embedding_model.strip()

    @classmethod
    def embedding_dim(cls) -> int:
        return cls._settings().cloud_embedding_dimensions

    @classmethod
    def contract_version(cls) -> str:
        return (
            f"openai-compatible:{cls.model_name()}@{cls._base_fingerprint()}"
            f"|dim={cls.embedding_dim()}|norm={cls.NORMALIZE}"
        )

    @classmethod
    def validate_embeddings(cls, embeddings) -> None:
        expected = cls.embedding_dim()
        for vector in embeddings:
            if len(vector) != expected:
                raise ValueError(
                    f"embedding dim {len(vector)} != contract {expected}: 云端模型输出维度不符"
                )
            if not all(math.isfinite(float(value)) for value in vector):
                raise ValueError("embedding contains non-finite value (NaN/Inf)")

    @classmethod
    def _normalize(cls, vector: list[float]) -> list[float]:
        norm = math.sqrt(sum(value * value for value in vector))
        if not math.isfinite(norm) or norm <= 0:
            raise ValueError("cloud embedding returned a zero or invalid vector")
        return [value / norm for value in vector]

    @classmethod
    def _request_batch(cls, texts: List[str]) -> List[List[float]]:
        settings = cls._settings()
        if not cls.is_configured():
            raise RuntimeError("云端 Embedding 尚未在服务端配置")
        url = settings.cloud_embedding_base_url.rstrip("/") + "/embeddings"
        payload = {
            "model": settings.cloud_embedding_model,
            "input": texts,
            "dimensions": settings.cloud_embedding_dimensions,
        }
        headers = {
            "Authorization": f"Bearer {settings.cloud_embedding_api_key}",
            "Content-Type": "application/json",
        }
        last_error: Exception | None = None
        for attempt in range(settings.cloud_embedding_max_retries + 1):
            try:
                with httpx.Client(timeout=settings.cloud_embedding_timeout_seconds) as client:
                    response = client.post(url, json=payload, headers=headers)
                response.raise_for_status()
                body = response.json()
                rows = body.get("data") if isinstance(body, dict) else None
                if not isinstance(rows, list) or len(rows) != len(texts):
                    raise ValueError("cloud embedding response count does not match input")
                ordered = sorted(rows, key=lambda row: int(row.get("index", 0)))
                if [int(row.get("index", -1)) for row in ordered] != list(
                    range(len(texts))
                ):
                    raise ValueError("cloud embedding response indices are invalid")
                vectors = [
                    cls._normalize([float(value) for value in row["embedding"]])
                    for row in ordered
                ]
                cls.validate_embeddings(vectors)
                return vectors
            except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
                last_error = exc
                if attempt < settings.cloud_embedding_max_retries:
                    time.sleep(min(2.0, 0.25 * (2**attempt)))
        raise RuntimeError("云端 Embedding 请求失败") from last_error

    @classmethod
    def embed_documents(cls, texts: List[str]) -> List[List[float]]:
        if not texts:
            return []
        batch_size = cls._settings().cloud_embedding_batch_size
        vectors: List[List[float]] = []
        for offset in range(0, len(texts), batch_size):
            vectors.extend(cls._request_batch(texts[offset : offset + batch_size]))
        return vectors

    @classmethod
    def embed_queries(cls, texts: List[str]) -> List[List[float]]:
        return cls.embed_documents(texts)

    @classmethod
    def embed_query(cls, text: str) -> List[float]:
        return cls.embed_documents([text])[0]


def embedding_contract(embedder) -> str:
    value = getattr(embedder, "EMBEDDING_CONTRACT_VERSION", None)
    if isinstance(value, str) and value:
        return value
    contract = getattr(embedder, "contract_version", None)
    if callable(contract):
        value = contract()
    if not isinstance(value, str) or not value:
        raise ValueError("embedding backend has no compatibility contract")
    return value


def embedding_model_name(embedder) -> str:
    value = getattr(embedder, "MODEL_NAME", None)
    if isinstance(value, str) and value:
        return value
    model_name = getattr(embedder, "model_name", None)
    if callable(model_name):
        value = model_name()
    if not isinstance(value, str) or not value:
        raise ValueError("embedding backend has no model name")
    return value


def resolve_embedder(profile_or_contract: str | None = None):
    value = str(profile_or_contract or "local").strip()
    if value in {
        "",
        Embedder.PROFILE_ID,
        Embedder.MODEL_NAME,
        Embedder.EMBEDDING_CONTRACT_VERSION,
    }:
        return Embedder
    is_persisted_cloud_contract = value.startswith("openai-compatible:")
    if value == CloudEmbedder.PROFILE_ID or is_persisted_cloud_contract:
        if not CloudEmbedder.is_configured():
            raise RuntimeError("云端 Embedding 尚未在服务端配置")
        if value == CloudEmbedder.PROFILE_ID or value == CloudEmbedder.contract_version():
            return CloudEmbedder
        raise RuntimeError("云端 Embedding 配置与索引契约不一致")
    raise ValueError(f"未知 Embedding 配置: {value}")


def embedding_profile_id(profile_or_contract: str | None) -> str:
    try:
        return str(resolve_embedder(profile_or_contract).PROFILE_ID)
    except (RuntimeError, ValueError):
        # A persisted cloud contract remains identifiable even if its key was
        # removed; retrieval will fail closed instead of silently using local.
        if str(profile_or_contract or "").startswith("openai-compatible:"):
            return CloudEmbedder.PROFILE_ID
        return Embedder.PROFILE_ID


def public_embedding_model_name(profile_or_contract: str | None) -> str | None:
    """Return a non-secret model label even when a cloud key was removed."""

    value = str(profile_or_contract or "local").strip()
    if embedding_profile_id(value) == Embedder.PROFILE_ID:
        return Embedder.MODEL_NAME
    prefix = "openai-compatible:"
    contract_head = value.split("|", 1)[0]
    if contract_head.startswith(prefix):
        model_and_fingerprint = contract_head[len(prefix) :]
        model, separator, _fingerprint = model_and_fingerprint.rpartition("@")
        if separator and model:
            return model
    configured = CloudEmbedder.model_name()
    if configured:
        return configured
    return None


def public_embedding_profiles() -> list[dict]:
    cloud_model = CloudEmbedder.model_name() or None
    return [
        {
            "profile_id": Embedder.PROFILE_ID,
            "kind": "local",
            "label": "本地 · BGE-M3",
            "model": Embedder.MODEL_NAME,
            "dimensions": Embedder.EMBEDDING_DIM,
            "available": True,
            "description": "数据不离开当前部署；首次加载模型后开始向量化。",
        },
        {
            "profile_id": CloudEmbedder.PROFILE_ID,
            "kind": "cloud",
            "label": f"云端 · {cloud_model}" if cloud_model else "云端 Embedding",
            "model": cloud_model,
            "dimensions": CloudEmbedder.embedding_dim(),
            "available": CloudEmbedder.is_configured(),
            "description": (
                "由服务端调用已配置的云端 Embedding，密钥不会发送到浏览器。"
                if CloudEmbedder.is_configured()
                else "管理员尚未配置云端 Embedding 凭据。"
            ),
        },
    ]
