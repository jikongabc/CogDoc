import copy
import json
import math
import os
import time
import uuid
from threading import RLock
from cogdoc.config.settings import get_settings
from cogdoc.service.kb_epoch import EpochStore, shared_epoch_store


# generation 生命周期： building → ready（构建完成，待提交）→ active（switch_active 提交） building → failed（构建失败，丢弃） 旧 active 被新代取代时 → superseded（可立即回收）
GENERATION_BUILDING = "building"
GENERATION_READY = "ready"
GENERATION_FAILED = "failed"
GENERATION_SUPERSEDED = "superseded"
_VALID_STATUS = {
    GENERATION_BUILDING,
    GENERATION_READY,
    GENERATION_FAILED,
    GENERATION_SUPERSEDED,
}

# 在飞代（building / ready 未提交）的租约：超过此秒数视为构建进程已崩溃，方可回收僵尸； 否则会误删「刚 mark_ready、正要 switch_active」的 staging。
DEFAULT_INFLIGHT_LEASE_SECONDS = 3600


# 切换时 epoch 已变（KB 被删/被取代）：该 staging 必须丢弃，绝不回写在线索引。
class StaleGenerationError(Exception):
    pass


# 新建索引代id。
def new_generation_id() -> str:
    # 短 id，进 chroma collection 名（col-{kb_id}-{gen} 有 60 字符上限）。
    return f"g{uuid.uuid4().hex[:12]}"


# 每 KB 一份 state.json：事务化索引的提交指针；每次操作都先从磁盘重载， 多个实例不会丢更新（叠加外部 KB 写锁串行化）；epoch 存在 KB 目录外的 EpochStore。
class KBState:
    # 每 KB 一份 state.json：事务化索引的提交指针；每次操作都先从磁盘重载， 多个实例不会丢更新（叠加外部 KB 写锁串行化）；epoch 存在 KB 目录外的 EpochStore。
    def __init__(
        self,
        kb_id: str,
        path: str | None = None,
        epochs: EpochStore | None = None,
    ):
        self.kb_id = kb_id
        self._path = path or get_settings().kb_state_path(kb_id)
        self._epochs = epochs or shared_epoch_store()
        self._lock = RLock()

    # 加载。
    def _load(self) -> dict:
        # 语法损坏退回初始态；合法但结构/不变量损坏的也清洗： 未知 status 的 generation 丢弃，active 必须指向一个 ready generation 否则置空。
        try:
            with open(self._path, encoding="utf-8") as f:
                raw = json.load(f)
            if not isinstance(raw, dict):
                raw = {}
        except (FileNotFoundError, json.JSONDecodeError):
            raw = {}

        raw_gens = raw.get("generations")
        gens = {}
        if isinstance(raw_gens, dict):
            for gid, gen in raw_gens.items():
                # 必填字段缺失、status 非法、或 id 与 key 不一致的条目一律丢弃。
                if _valid_generation(gid, gen):
                    gens[gid] = gen

        active = raw.get("active_generation")
        if active not in gens or gens.get(active, {}).get("status") != GENERATION_READY:
            active = None

        return {"kb_id": self.kb_id, "active_generation": active, "generations": gens}

    # 保存。
    def _save(self, data: dict) -> None:
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        tmp_path = f"{self._path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, self._path)

    # 维护纪元和删除标记。

    # epoch：处理对应功能。
    @property
    def epoch(self) -> int:
        return self._epochs.current(self.kb_id)

    # 递增 epoch。
    def bump_epoch(self) -> int:
        return self._epochs.bump(self.kb_id)

    # 代际生命周期。

    # begin_generation：处理对应功能。
    def begin_generation(
        self,
        embedding_model: str,
        index_build_version: str,
        chunk_identity_version: str | None = None,
    ) -> str:
        if not isinstance(embedding_model, str) or not embedding_model:
            raise ValueError("embedding_model must be a non-empty string")
        if not isinstance(index_build_version, str) or not index_build_version:
            raise ValueError("index_build_version must be a non-empty string")
        if chunk_identity_version is not None and (
            not isinstance(chunk_identity_version, str) or not chunk_identity_version
        ):
            raise ValueError("chunk_identity_version must be a non-empty string")
        with self._lock:
            data = self._load()
            gen_id = new_generation_id()
            # 记录构建起点的 epoch；切换时比对，期间被删库则拒绝提交。
            generation = {
                "id": gen_id,
                "status": GENERATION_BUILDING,
                "embedding_model": embedding_model,
                "index_build_version": index_build_version,
                "base_epoch": self._epochs.current(self.kb_id),
                "expected_count": None,
                "documents": [],
                "created_at": time.time(),
            }
            # 新代将分块身份契约与构建版本一起持久化。旧 state 可以没有
            # 该字段；激活后的读者将从 state 中原子读取它，不再依赖提交后 manifest。
            if chunk_identity_version is not None:
                generation["chunk_identity_version"] = chunk_identity_version
            data["generations"][gen_id] = generation
            self._save(data)
            return gen_id

    # 标记ready。
    def mark_ready(
        self, gen_id: str, expected_count: int, documents: list[dict]
    ) -> None:
        # 仅 building→ready。
        with self._lock:
            data = self._load()
            gen = data["generations"].get(gen_id)
            if gen is None:
                raise KeyError(gen_id)
            if gen["status"] != GENERATION_BUILDING:
                raise ValueError(f"only building->ready allowed, was {gen['status']}")
            if (
                not isinstance(expected_count, int)
                or isinstance(expected_count, bool)
                or expected_count < 0
            ):
                raise ValueError("expected_count must be non-negative")
            if not isinstance(documents, list) or not all(
                isinstance(document, dict) for document in documents
            ):
                raise ValueError("documents must be a list of objects")
            gen["status"] = GENERATION_READY
            gen["expected_count"] = expected_count
            gen["documents"] = copy.deepcopy(documents)
            self._save(data)

    # 标记failed。
    def mark_failed(self, gen_id: str) -> None:
        # building|ready → failed；active 永不允许被标记失败。 ready 态允许失败：switch_active 抛 StaleGenerationError 后需要丢弃已 ready 的 staging。
        with self._lock:
            data = self._load()
            gen = data["generations"].get(gen_id)
            if gen is None:
                raise KeyError(gen_id)
            if gen_id == data["active_generation"]:
                raise ValueError("cannot fail the active generation")
            if gen["status"] not in (GENERATION_BUILDING, GENERATION_READY):
                raise ValueError(
                    f"only building|ready->failed allowed, was {gen['status']}"
                )
            gen["status"] = GENERATION_FAILED
            self._save(data)

    # 切换活跃代。
    def switch_active(self, gen_id: str) -> str | None:
        # 仅允许基准纪元仍匹配的就绪代切换为活跃代。
        with self._lock:
            data = self._load()
            gen = data["generations"].get(gen_id)
            if gen is None:
                raise KeyError(gen_id)
            if gen["status"] != GENERATION_READY:
                raise ValueError("only a ready generation can be activated")
            if gen.get("base_epoch") != self._epochs.current(self.kb_id):
                raise StaleGenerationError(
                    f"generation {gen_id} is stale: KB epoch advanced"
                )
            previous = data["active_generation"]
            if previous is not None and previous != gen_id:
                old = data["generations"].get(previous)
                if old is not None:
                    old["status"] = GENERATION_SUPERSEDED
            data["active_generation"] = gen_id
            self._save(data)
            return previous if previous != gen_id else None

    def rollback_active(self, gen_id: str) -> str:
        """Atomically reactivate one retained superseded generation.

        Rollback deliberately accepts only a superseded generation.  Building,
        failed, and unrelated ready generations are never valid rollback
        targets, which keeps this operation distinct from a normal commit.
        Generation storage must have been retained by the caller.
        """

        with self._lock:
            data = self._load()
            target = data["generations"].get(gen_id)
            if target is None:
                raise KeyError(gen_id)
            if target["status"] != GENERATION_SUPERSEDED:
                raise ValueError("only a superseded generation can be rolled back")
            current_id = data["active_generation"]
            if current_id is None:
                raise ValueError("cannot roll back without an active generation")
            current = data["generations"].get(current_id)
            if current is None or current["status"] != GENERATION_READY:
                raise ValueError("active generation state is invalid")
            current["status"] = GENERATION_SUPERSEDED
            target["status"] = GENERATION_READY
            data["active_generation"] = gen_id
            self._save(data)
            return current_id

    # 查询和回收。

    # active：处理对应功能。
    def active(self) -> dict | None:
        with self._lock:
            data = self._load()
            active = data["active_generation"]
            gen = data["generations"].get(active) if active else None
            return copy.deepcopy(gen) if gen else None

    # 返回结果。
    def get(self, gen_id: str) -> dict | None:
        with self._lock:
            gen = self._load()["generations"].get(gen_id)
            return copy.deepcopy(gen) if gen else None

    # 完成 索引代ids 处理。
    def generation_ids(self) -> list[str]:
        with self._lock:
            return list(self._load()["generations"].keys())

    # 移除索引代。
    def remove_generation(self, gen_id: str) -> None:
        with self._lock:
            data = self._load()
            if gen_id == data["active_generation"]:
                raise ValueError("cannot remove the active generation")
            data["generations"].pop(gen_id, None)
            self._save(data)

    # 完成 stale索引代ids 处理。
    def stale_generation_ids(
        self, lease_seconds: float = DEFAULT_INFLIGHT_LEASE_SECONDS
    ) -> list[str]:
        # 立即可回收：failed、superseded（被取代的旧 active）。 在飞代（building / ready 未提交）只在超过租约（构建进程崩溃留下的僵尸）才回收， 否则会误删「刚 mark_ready、正要 switch_active」的 staging。
        with self._lock:
            data = self._load()
            active = data["active_generation"]
            now = time.time()
            stale = []
            for gid, gen in data["generations"].items():
                if gid == active:
                    continue
                status = gen["status"]
                if status in (GENERATION_FAILED, GENERATION_SUPERSEDED):
                    stale.append(gid)
                elif status in (GENERATION_BUILDING, GENERATION_READY):
                    created = gen.get("created_at", 0)
                    if (
                        isinstance(created, (int, float))
                        and now - created > lease_seconds
                    ):
                        stale.append(gid)
            return stale


# 完成 合法性索引代 处理。
def _valid_generation(gid: object, gen: object) -> bool:
    if not isinstance(gid, str) or not isinstance(gen, dict):
        return False
    status = gen.get("status")
    base_epoch = gen.get("base_epoch")
    created_at = gen.get("created_at")
    expected_count = gen.get("expected_count")
    if "chunk_identity_version" in gen and (
        not isinstance(gen["chunk_identity_version"], str)
        or not gen["chunk_identity_version"]
    ):
        return False
    if (
        gen.get("id") != gid
        or status not in _VALID_STATUS
        or not isinstance(gen.get("embedding_model"), str)
        or not gen["embedding_model"]
        or not isinstance(gen.get("index_build_version"), str)
        or not gen["index_build_version"]
        or not isinstance(base_epoch, int)
        or isinstance(base_epoch, bool)
        or base_epoch < 0
        or not isinstance(created_at, (int, float))
        or isinstance(created_at, bool)
        or not math.isfinite(created_at)
        or created_at < 0
        or not isinstance(gen.get("documents"), list)
        or not all(isinstance(document, dict) for document in gen["documents"])
    ):
        return False
    if status == GENERATION_BUILDING:
        return expected_count is None
    return (expected_count is None and status == GENERATION_FAILED) or (
        isinstance(expected_count, int)
        and not isinstance(expected_count, bool)
        and expected_count >= 0
    )
