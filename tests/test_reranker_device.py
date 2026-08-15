import pytest
from cogdoc.tools import device, reranker
from cogdoc.tools.reranker import (
    BGEReranker,
    dynamic_rerank_top_n,
    rerank_with_device_policy,
    rerank_with_requirement_policy,
)


# 恢复重排器全局状态。
@pytest.fixture(autouse=True)
def restore_reranker_state():
    # 每个测试前后恢复 reranker 单例状态。
    saved_device = BGEReranker.device
    saved_model = BGEReranker._model
    saved_models = dict(BGEReranker._models)
    saved_tokenizer = BGEReranker._tokenizer
    saved_required_cuda_free_bytes = BGEReranker.REQUIRED_CUDA_FREE_BYTES
    BGEReranker._models = {}
    BGEReranker.REQUIRED_CUDA_FREE_BYTES = 2800 * 1024 * 1024
    yield
    BGEReranker.device = saved_device
    BGEReranker._model = saved_model
    BGEReranker._models = saved_models
    BGEReranker._tokenizer = saved_tokenizer
    BGEReranker.REQUIRED_CUDA_FREE_BYTES = saved_required_cuda_free_bytes


# 模拟支持设备迁移的重排模型。
class _FakeModel:
    # 模拟支持设备迁移的重排模型。
    def to(self, dev):
        # 记录模型被迁移到的目标设备。
        self.device = dev
        return self

    # 切换评估模式结果。
    def eval(self):
        # 模拟模型进入推理模式。
        return self


# 构造模型loading。
def _stub_model_loading(monkeypatch):
    # 替换模型加载流程，避免测试下载真实权重。
    monkeypatch.setattr(
        reranker.AutoTokenizer, "from_pretrained", lambda name: object()
    )
    fake = _FakeModel()
    monkeypatch.setattr(
        reranker.AutoModelForSequenceClassification,
        "from_pretrained",
        lambda name: fake,
    )
    return fake


# 验证 default device is one of known backends 场景。
def test_default_device_is_one_of_known_backends():
    # 验证默认 reranker 设备属于支持的后端。
    assert BGEReranker.default_device() in {"cuda", "mps", "cpu"}


# 验证 default device uses gpu when enough free memory 场景。
def test_default_device_uses_gpu_when_enough_free_memory(monkeypatch):
    # 空闲显存充足时走 cuda。
    monkeypatch.setattr(device.torch.cuda, "is_available", lambda: True)
    monkeypatch.setattr(device, "cuda_free_bytes", lambda: 6 * 1024 * 1024 * 1024)
    BGEReranker.device = "cpu"
    BGEReranker._model = None

    assert BGEReranker.default_device() == "cuda"


# 验证 default device falls back to cpu when gpu low 场景。
def test_default_device_falls_back_to_cpu_when_gpu_low(monkeypatch):
    # 显存被其它进程占满、空闲不足阈值时回落 CPU，避免 CUDA OOM。
    monkeypatch.setattr(device.torch.cuda, "is_available", lambda: True)
    monkeypatch.setattr(device, "cuda_free_bytes", lambda: 90 * 1024 * 1024)
    monkeypatch.setattr(device, "mps_available", lambda: False)
    BGEReranker.device = "cpu"
    BGEReranker._model = None

    assert BGEReranker.default_device() == "cpu"


# 验证 default device sticky once loaded on cuda 场景。
def test_default_device_sticky_once_loaded_on_cuda(monkeypatch):
    # 已在 cuda 且模型在显存里：自身占用已计入空闲值，即便此刻空闲不足也不抖回 CPU。
    monkeypatch.setattr(device, "cuda_free_bytes", lambda: 0)
    BGEReranker.device = "cuda"
    BGEReranker._model = object()

    assert BGEReranker.default_device() == "cuda"


# 验证 default device reuses cached cuda model 场景。
def test_default_device_reuses_cached_cuda_model(monkeypatch):
    # 显式设备调用加载过 cuda 模型后，云端默认设备继续复用已缓存模型。
    monkeypatch.setattr(device, "cuda_free_bytes", lambda: 0)
    BGEReranker.device = None
    BGEReranker._model = None
    BGEReranker._models["cuda"] = object()

    assert BGEReranker.default_device() == "cuda"


# 验证 get resources resolves device when unset 场景。
def test_get_resources_resolves_device_when_unset(monkeypatch):
    # 直连调用（未 set_device，device=None）按显存自动选设备，不退化成 CPU。
    monkeypatch.setattr(device.torch.cuda, "is_available", lambda: True)
    monkeypatch.setattr(device, "cuda_free_bytes", lambda: 6 * 1024 * 1024 * 1024)
    fake = _stub_model_loading(monkeypatch)
    BGEReranker.device = None
    BGEReranker._model = None
    BGEReranker._tokenizer = None

    BGEReranker._get_resources()

    assert BGEReranker.device == "cuda"
    assert fake.device == "cuda"


# 验证 get resources respects explicit cpu 场景。
def test_get_resources_respects_explicit_cpu(monkeypatch):
    # 本地模式 set_device("cpu") 后即便显存充足也不被覆盖。
    monkeypatch.setattr(device.torch.cuda, "is_available", lambda: True)
    monkeypatch.setattr(device, "cuda_free_bytes", lambda: 6 * 1024 * 1024 * 1024)
    fake = _stub_model_loading(monkeypatch)
    BGEReranker._model = None
    BGEReranker._tokenizer = None
    BGEReranker.set_device("cpu")

    BGEReranker._get_resources()

    assert fake.device == "cpu"


# 验证 switch to new device invalidates model singleton 场景。
def test_switch_to_new_device_invalidates_model_singleton():
    # 验证切换到新设备时会清空已加载模型单例。
    BGEReranker.device = "cuda"
    BGEReranker._model = object()

    BGEReranker.set_device("cpu")

    assert BGEReranker.device == "cpu"
    assert BGEReranker._model is None


# 验证 switch to same device keeps model singleton 场景。
def test_switch_to_same_device_keeps_model_singleton():
    # 验证切换到相同设备时保留已加载模型单例。
    BGEReranker.device = "cpu"
    sentinel = object()
    BGEReranker._model = sentinel

    BGEReranker.set_device("cpu")

    assert BGEReranker._model is sentinel


# 验证 local then cloud switch restores default device 场景。
def test_local_then_cloud_switch_restores_default_device():
    # 验证本地 CPU 模式后还能切回默认设备。
    default = BGEReranker.default_device()

    BGEReranker.set_device("cpu")
    assert BGEReranker.device == "cpu"

    BGEReranker.set_device(default)
    assert BGEReranker.device == default


def test_rerank_policy_skips_cpu_when_production_disallows_it(monkeypatch):
    docs = [{"text": "candidate", "meta": {"chunk_id": "c1"}}]
    monkeypatch.setattr(BGEReranker, "default_device", lambda: "cpu")

    result = rerank_with_device_policy(
        query="query",
        docs=docs,
        top_n=1,
        allow_cpu=False,
    )

    assert result.device == "cpu"
    assert result.skipped_reason == "cpu_disabled"
    assert result.docs[0]["retrieval"]["rerank_skipped_reason"] == "cpu_disabled"


def test_rerank_policy_runs_cross_encoder_when_cpu_is_allowed(monkeypatch):
    docs = [{"text": "candidate", "meta": {"chunk_id": "c1"}}]
    ranked = [{"text": "ranked", "meta": {"chunk_id": "c1"}}]
    calls = []
    monkeypatch.setattr(BGEReranker, "default_device", lambda: "cpu")

    def fake_rerank(*, query, docs, top_n, device):
        calls.append((query, docs, top_n, device))
        return ranked

    monkeypatch.setattr(BGEReranker, "rerank", fake_rerank)

    result = rerank_with_device_policy(
        query="query",
        docs=docs,
        top_n=1,
        allow_cpu=True,
    )

    assert result.docs == ranked
    assert result.device == "cpu"
    assert result.skipped_reason == ""
    assert calls == [("query", docs, 1, "cpu")]


def test_requirement_rerank_batches_pairs_and_reserves_each_requirement(monkeypatch):
    docs = [
        {
            "text": "A",
            "meta": {"chunk_id": "a"},
            "retrieval": {"matched_requirement_ids": ["r1"]},
        },
        {
            "text": "B",
            "meta": {"chunk_id": "b"},
            "retrieval": {"matched_requirement_ids": ["r2"]},
        },
        {"text": "global", "meta": {"chunk_id": "g"}, "retrieval": {}},
    ]
    monkeypatch.setattr(BGEReranker, "default_device", lambda: "cuda")
    seen = []

    def score_pairs(pairs, *, device=None):
        seen.extend(pairs)
        # Global scores favor g, requirement scores favor their attributed docs.
        return [0.2, 0.1, 0.9, 0.8, 0.7]

    monkeypatch.setattr(BGEReranker, "score_pairs", score_pairs)
    result = rerank_with_requirement_policy(
        query="all",
        docs=docs,
        requirement_queries={"r1": "need A", "r2": "need B"},
        top_n=3,
        allow_cpu=False,
        per_requirement=1,
    )

    assert [doc["meta"]["chunk_id"] for doc in result.docs] == ["a", "b", "g"]
    assert len(seen) == 5
    assert result.docs[0]["retrieval"]["requirement_rerank_scores"] == {"r1": 0.8}


def test_dynamic_rerank_budget_is_bounded_by_evidence_pack():
    assert (
        dynamic_rerank_top_n(
            base_top_n=3,
            max_docs=8,
            requirement_count=3,
            docs_per_requirement=2,
        )
        == 6
    )
