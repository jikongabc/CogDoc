import hashlib
import inspect
import json
import logging
import os
import shutil
import threading
import time
from contextlib import nullcontext
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Lock, RLock
from typing import Any, Callable
from uuid import uuid4
from cogdoc.api.persistence import InMemoryJobStore
from cogdoc.config.settings import get_settings
from cogdoc.observability.logger import log_event
from cogdoc.service.durable_io import fsync_directory
from cogdoc.service.ingest_service import KBCleanupError, build_kb_index_transactional
from cogdoc.service.kb_epoch import shared_epoch_store
from cogdoc.service.kb_lifecycle import LIFECYCLE_ACTIVE, shared_lifecycle_store
from cogdoc.service.kb_locks import kb_write_lock
from cogdoc.service.mutation_journal import MutationJournal, shared_mutation_journal
from cogdoc.service.mutation_paths import mutation_backup_path


class _DelegatedMutationCall:
    def __init__(self, function: Callable, mutation_lease: object) -> None:
        self.function = function
        self.mutation_lease = mutation_lease

    def __call__(self, *args):
        return self.function(*args)


class _AbortableMutationCall:
    """Ensure admission-side state is released if a queued job never starts."""

    def __init__(self, function: Callable, on_unstarted: Callable[[], None]) -> None:
        self.function = function
        self._on_unstarted = on_unstarted
        self._started = False
        self._aborted = False
        self._lock = Lock()

    def __call__(self, *args):
        with self._lock:
            self._started = True
        return self.function(*args)

    def abort_if_unstarted(self) -> None:
        with self._lock:
            if self._started or self._aborted:
                return
            self._aborted = True
        self._on_unstarted()


# 返回当前 UTC 时间字符串。
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# 完成 silentremove 处理。
def _silent_remove(path: str) -> bool:
    # 删除文件，成功或本就不存在返回 True；删除失败返回 False，供调用方据此保留 journal 重试。
    try:
        os.remove(path)
        return True
    except FileNotFoundError:
        return True
    except OSError:
        return False


# 表示 KBExistsError 异常。
class KBExistsError(Exception):
    pass


# registry 损坏：宁可拒绝启动也不退回空表，否则现存 KB 全部消失、同名重建会复用旧 source/state/index。
class RegistryCorruptError(Exception):
    pass


# 知识库元数据的 JSON 注册表；逻辑 (tenant_id, kb_id) 映射到稳定的物理 storage_id。
class KnowledgeBaseRegistry:
    # 非默认租户使用独立命名空间。完整 SHA-256 既不泄漏租户名，也避免危险字符进入路径。
    _TENANT_STORAGE_PREFIX = "t-"

    # 知识库元数据的 JSON 注册表；source/chroma/bm25/manifest 按 storage_id 物理隔离。
    def __init__(
        self,
        registry_path: str | None = None,
        source_dir_for: Callable[[str], str] | None = None,
    ):
        settings = get_settings()
        self._path = registry_path or settings.kb_registry_path
        self._degraded_path = f"{self._path}.degraded"
        self._source_dir_for = source_dir_for or settings.kb_source_dir
        self._lock = RLock()
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        self._entries = self._load()

    # 加载。
    def _load(self) -> dict:
        # 文件不存在=全新系统，空表。损坏（语法/结构）则隔离原文件并抛错 fail-closed，绝不退回空表。
        if os.path.exists(self._degraded_path):
            raise RegistryCorruptError(
                f"registry 处于 degraded 状态，需人工恢复: {self._path}"
            )
        try:
            with open(self._path, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return {}
        except json.JSONDecodeError:
            self._quarantine_corrupt()
            raise RegistryCorruptError(f"registry 损坏已隔离: {self._path}")
        if not isinstance(data, dict):
            self._quarantine_corrupt()
            raise RegistryCorruptError(f"registry 顶层非 dict 已隔离: {self._path}")

        # 旧版以 kb_id 为 key，且可能没有 tenant/owner/storage 字段。只在内存中补齐，
        # 不在读取时改盘；下一次真实 mutation 才会自然写出新格式。
        normalized: dict[str, dict] = {}
        logical_keys: set[tuple[str, str]] = set()
        try:
            for persisted_key, raw_record in data.items():
                if not isinstance(persisted_key, str) or not isinstance(
                    raw_record, dict
                ):
                    raise ValueError("registry entry must be an object")
                kb_id = raw_record.get("kb_id")
                tenant_id = raw_record.get("tenant_id", "default")
                owner_id = raw_record.get("owner_id", "default")
                if not isinstance(kb_id, str) or not kb_id:
                    raise ValueError("registry kb_id is invalid")
                if not isinstance(tenant_id, str) or not tenant_id:
                    raise ValueError("registry tenant_id is invalid")
                if not isinstance(owner_id, str) or not owner_id:
                    raise ValueError("registry owner_id is invalid")
                if self._validate_kb_id(kb_id) != kb_id:
                    raise ValueError("registry kb_id is not canonical")
                if (
                    self._normalize_identity_id(tenant_id, field="tenant_id")
                    != tenant_id
                ):
                    raise ValueError("registry tenant_id is not canonical")
                if self._normalize_identity_id(owner_id, field="owner_id") != owner_id:
                    raise ValueError("registry owner_id is not canonical")

                stored_storage_id = raw_record.get("storage_id")
                if stored_storage_id is None:
                    # No released version wrote a non-default tenant without a
                    # storage_id, so only the exact legacy default layout is safe.
                    if tenant_id != "default" or persisted_key != kb_id:
                        raise ValueError("registry legacy storage identity is invalid")
                    storage_id = kb_id
                elif not isinstance(stored_storage_id, str) or not stored_storage_id:
                    raise ValueError("registry storage_id is invalid")
                else:
                    storage_id = stored_storage_id

                expected_storage_id = self._make_storage_id(kb_id, tenant_id)
                if storage_id != expected_storage_id or persisted_key != storage_id:
                    raise ValueError("registry storage identity does not match record")
                logical_key = (tenant_id, kb_id)
                if logical_key in logical_keys or storage_id in normalized:
                    raise ValueError(
                        "registry contains duplicate knowledge base identity"
                    )
                logical_keys.add(logical_key)
                normalized[storage_id] = {
                    **raw_record,
                    "kb_id": kb_id,
                    "tenant_id": tenant_id,
                    "owner_id": owner_id,
                    "storage_id": storage_id,
                }
        except ValueError:
            self._quarantine_corrupt()
            raise RegistryCorruptError(f"registry 记录非法已隔离: {self._path}")
        return normalized

    # 隔离损坏文件。
    def _quarantine_corrupt(self) -> None:
        try:
            os.replace(self._path, f"{self._path}.corrupt-{time.time_ns()}")
        except OSError:
            pass
        try:
            with open(self._degraded_path, "w", encoding="utf-8") as f:
                f.write(str(int(time.time())))
        except OSError:
            pass

    # 保存 entries。
    def _save_entries(self, entries: dict) -> None:
        # 原子且持久地写候选表：先写临时文件并 fsync，再 rename 并 fsync
        # 父目录，避免掉电后出现“内存已提交、registry 名字却消失”。
        tmp_path = f"{self._path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(entries, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, self._path)
        parent = os.path.dirname(self._path) or "."
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        descriptor = os.open(parent, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @classmethod
    def _make_storage_id(cls, kb_id: str, tenant_id: str) -> str:
        if tenant_id == "default":
            return kb_id
        identity = json.dumps(
            [tenant_id, kb_id], ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        digest = hashlib.sha256(b"cogdoc-kb-storage-v1\0" + identity).hexdigest()
        return f"{cls._TENANT_STORAGE_PREFIX}{digest}"

    @classmethod
    def storage_id_for(cls, kb_id: str, tenant_id: str = "default") -> str:
        """Return the physical identity used for locks, paths, and indexes."""

        kb_id, tenant_id, _ = cls._validated_identity(kb_id, tenant_id)
        return cls._make_storage_id(kb_id, tenant_id)

    @staticmethod
    def _validate_kb_id(value: str) -> str:
        if not isinstance(value, str):
            raise ValueError("kb_id must be a string")
        if (
            not value
            or value != value.strip()
            or len(value) > 56
            or value in {".", ".."}
            or "\x00" in value
            or any(character in "/\\" or character.isspace() for character in value)
        ):
            raise ValueError("invalid kb_id")
        return value

    @staticmethod
    def _normalize_identity_id(value: str, *, field: str) -> str:
        if not isinstance(value, str):
            raise ValueError(f"{field} must be a string")
        normalized = value.strip()
        if (
            not normalized
            or len(normalized) > 160
            or any(
                ord(character) < 32 or ord(character) == 127 for character in normalized
            )
        ):
            raise ValueError(f"invalid {field}")
        return normalized

    @classmethod
    def _validated_identity(
        cls, kb_id: str, tenant_id: str, owner_id: str | None = None
    ) -> tuple[str, str, str | None]:
        kb_id = cls._validate_kb_id(kb_id)
        tenant_id = cls._normalize_identity_id(tenant_id, field="tenant_id")
        if owner_id is not None:
            owner_id = cls._normalize_identity_id(owner_id, field="owner_id")
        return kb_id, tenant_id, owner_id

    def _get_unlocked(self, kb_id: str, tenant_id: str) -> dict | None:
        storage_id = self._make_storage_id(kb_id, tenant_id)
        record = self._entries.get(storage_id)
        if record is None:
            return None
        if record.get("kb_id") != kb_id or record.get("tenant_id") != tenant_id:
            # A hash collision or corrupt in-memory state must never resolve to
            # another tenant's data.
            return None
        return record

    # 将租户内逻辑 slug 解析为带稳定物理身份的完整记录。
    def resolve(self, kb_id: str, tenant_id: str = "default") -> dict | None:
        kb_id, tenant_id, _ = self._validated_identity(kb_id, tenant_id)
        with self._lock:
            record = self._get_unlocked(kb_id, tenant_id)
            return dict(record) if record is not None else None

    # 按物理身份反查，用于 job/research/trace 等异步资源授权。
    def get_by_storage_id(self, storage_id: str) -> dict | None:
        if not isinstance(storage_id, str) or not storage_id:
            return None
        with self._lock:
            record = self._entries.get(storage_id)
            return dict(record) if record is not None else None

    # 返回目录。既接受逻辑 kb_id（可带 tenant），也接受记录中的 storage_id。
    def source_dir(self, kb_id: str, tenant_id: str | None = None) -> str:
        with self._lock:
            direct = self._entries.get(kb_id) if tenant_id is None else None
            if direct is not None:
                storage_id = str(direct["storage_id"])
            else:
                resolved_tenant = "default" if tenant_id is None else tenant_id
                kb_id, resolved_tenant, _ = self._validated_identity(
                    kb_id, resolved_tenant
                )
                record = self._get_unlocked(kb_id, resolved_tenant)
                storage_id = (
                    str(record["storage_id"])
                    if record is not None
                    else self._make_storage_id(kb_id, resolved_tenant)
                )
        return self._source_dir_for(storage_id)

    # 创建。
    def create(
        self,
        kb_id: str,
        tenant_id: str = "default",
        owner_id: str = "default",
    ) -> dict:
        kb_id, tenant_id, validated_owner_id = self._validated_identity(
            kb_id, tenant_id, owner_id
        )
        assert validated_owner_id is not None
        owner_id = validated_owner_id
        storage_id = self._make_storage_id(kb_id, tenant_id)
        with self._lock:
            if self._get_unlocked(kb_id, tenant_id) is not None:
                raise KBExistsError(kb_id)
            if storage_id in self._entries:
                # Defensive collision guard: never alias two logical identities.
                raise RegistryCorruptError("storage_id collision")
            # 新 incarnation：epoch 自增，令删库前在飞、捕获旧 epoch 的任务在重建后仍被守卫拦下。
            shared_epoch_store().bump(storage_id)
            os.makedirs(self._source_dir_for(storage_id), exist_ok=True)
            record = {
                "kb_id": kb_id,
                "created_at": _now_iso(),
                "tenant_id": tenant_id,
                "owner_id": owner_id,
                "storage_id": storage_id,
            }
            # registry 持久化是提交点：先写盘成功再更新内存。提交前 lifecycle 仍是旧态（如 deleted→读被拦）， 故"registry 已存在但 lifecycle 未 active"是 fail-closed，不会出现"registry 缺失但可读旧数据"。
            candidate = {**self._entries, storage_id: record}
            self._save_entries(candidate)
            self._entries = candidate
            # 提交后切 active，清除同名 KB 的 deleted tombstone，恢复读写。
            try:
                shared_lifecycle_store().set(storage_id, LIFECYCLE_ACTIVE)
            except Exception:
                # 先清目录、再撤 registry。目录清理失败时保留 registry 记录，让调用方可显式 DELETE 重试， 不能移除记录后让同名 create 复用半创建目录。
                kb_dir = os.path.dirname(self._source_dir_for(storage_id))
                try:
                    shutil.rmtree(kb_dir)
                except FileNotFoundError:
                    pass
                except OSError as cleanup_exc:
                    raise KBCleanupError(
                        f"KB 创建 finalize 失败且目录补偿失败: {kb_dir}"
                    ) from cleanup_exc
                candidate = {k: v for k, v in self._entries.items() if k != storage_id}
                self._save_entries(candidate)
                self._entries = candidate
                raise
            return dict(record)

    # 检查存在性。
    def exists(self, kb_id: str, tenant_id: str | None = None) -> bool:
        if tenant_id is None:
            with self._lock:
                if kb_id in self._entries:
                    return True
        resolved_tenant = "default" if tenant_id is None else tenant_id
        try:
            kb_id, resolved_tenant, _ = self._validated_identity(kb_id, resolved_tenant)
        except ValueError:
            return False
        with self._lock:
            return self._get_unlocked(kb_id, resolved_tenant) is not None

    # 返回结果。
    def get(self, kb_id: str, tenant_id: str | None = None) -> dict | None:
        if tenant_id is None:
            with self._lock:
                direct = self._entries.get(kb_id)
                if direct is not None:
                    return dict(direct)
        resolved_tenant = "default" if tenant_id is None else tenant_id
        try:
            kb_id, resolved_tenant, _ = self._validated_identity(kb_id, resolved_tenant)
        except ValueError:
            return None
        with self._lock:
            record = self._get_unlocked(kb_id, resolved_tenant)
            return dict(record) if record else None

    # 列出。
    def list(self, tenant_id: str | None = None) -> list[dict]:
        if tenant_id is not None:
            tenant_id = self._normalize_identity_id(tenant_id, field="tenant_id")
        with self._lock:
            return [
                dict(record)
                for record in self._entries.values()
                if tenant_id is None or record.get("tenant_id") == tenant_id
            ]

    # 删除。
    def delete(self, kb_id: str, tenant_id: str | None = None) -> bool:
        # 先删源目录，成功后才从 registry 移除：目录删失败时 registry 仍保留该 KB，DELETE 可重试不返回 404。
        with self._lock:
            record = self._entries.get(kb_id) if tenant_id is None else None
            if record is None:
                resolved_tenant = "default" if tenant_id is None else tenant_id
                try:
                    kb_id, resolved_tenant, _ = self._validated_identity(
                        kb_id, resolved_tenant
                    )
                except ValueError:
                    return False
                record = self._get_unlocked(kb_id, resolved_tenant)
            if record is None:
                return False
            storage_id = str(record["storage_id"])
            kb_dir = os.path.dirname(self._source_dir_for(storage_id))
            try:
                shutil.rmtree(kb_dir)
            except FileNotFoundError:
                pass  # 已删或上次删一半：幂等放过，继续移除 registry 记录
            except OSError as exc:
                raise KBCleanupError(f"KB 目录删除失败: {kb_dir}") from exc
            # 目录已清，再原子写出不含该 KB 的候选表并更新内存。
            candidate = {k: v for k, v in self._entries.items() if k != storage_id}
            self._save_entries(candidate)
            self._entries = candidate
            return True


_MAX_KB_EXECUTORS = 256  # 防止持续创建/删库积累无界线程对象
# 每个 kb_id 独享一个单线程 executor：不同 KB 并发构建，同 KB 内 mutation + 构建全部串行。
class IndexJobManager:
    # 每个 kb_id 独享一个单线程 executor：不同 KB 并发构建，同 KB 内 mutation + 构建全部串行。
    def __init__(
        self,
        ingest_fn: Callable[..., Any] = build_kb_index_transactional,
        source_dir_for: Callable[[str], str] | None = None,
        job_store: Any | None = None,
        kb_exists: "Callable[[str], bool] | None" = None,
        journal: MutationJournal | None = None,
        knowledge_store: object | None = None,
        after_index_commit: Callable[[str, object], None] | None = None,
        epoch_reader: Callable[[str], int] | None = None,
        lifecycle_reader: Callable[[str], str] | None = None,
        mutation_coordinator: object | None = None,
        source_generation_store: object | None = None,
    ):
        self._ingest_fn = ingest_fn
        self._knowledge_store = knowledge_store
        self._after_index_commit = after_index_commit
        self._epoch_reader = epoch_reader or shared_epoch_store().current
        self._lifecycle_reader = lifecycle_reader or shared_lifecycle_store().status
        self._mutation_coordinator = mutation_coordinator
        self._source_generation_store = source_generation_store
        self._source_dir_for = source_dir_for or get_settings().kb_source_dir
        self._store = job_store or InMemoryJobStore()
        self._kb_exists = kb_exists  # 防复活：KB 已删未重建时拒绝陈旧 mutation
        self._journal = (
            journal or shared_mutation_journal()
        )  # 源文件 mutation 崩溃恢复日志
        # 只向显式支持的 ingest_fn 注入提交回调与派生知识存储，兼容旧两参数函数。
        try:
            ingest_parameters = inspect.signature(ingest_fn).parameters
            accepts_var_keyword = any(
                parameter.kind is inspect.Parameter.VAR_KEYWORD
                for parameter in ingest_parameters.values()
            )
            self._ingest_takes_on_commit = self._accepts_keyword(
                ingest_parameters,
                "on_commit",
                accepts_var_keyword=accepts_var_keyword,
            )
            self._ingest_takes_knowledge_store = self._accepts_keyword(
                ingest_parameters,
                "knowledge_store",
                accepts_var_keyword=accepts_var_keyword,
            )
            self._ingest_takes_embedding_profile = self._accepts_keyword(
                ingest_parameters,
                "embedding_profile_id",
                accepts_var_keyword=accepts_var_keyword,
            )
        except (TypeError, ValueError):
            self._ingest_takes_on_commit = False
            self._ingest_takes_knowledge_store = False
            self._ingest_takes_embedding_profile = False
        if self._mutation_coordinator is not None and not self._ingest_takes_on_commit:
            raise ValueError(
                "distributed KB mutations require an ingest commit-fence callback"
            )
        self._executors: dict[str, ThreadPoolExecutor] = {}
        self._retired_executors: set[ThreadPoolExecutor] = set()
        self._retire_when_idle: set[str] = set()
        self._inflight: dict[
            str, int
        ] = {}  # 每 KB 在途命令数，0 且久未活动才可淘汰 executor
        self._last_active: dict[str, float] = {}
        self._ex_lock = Lock()
        self._closed = False

    @staticmethod
    def _accepts_keyword(
        parameters,
        name: str,
        *,
        accepts_var_keyword: bool,
    ) -> bool:
        parameter = parameters.get(name)
        if parameter is None:
            return accepts_var_keyword
        return parameter.kind in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        )

    def bind_knowledge_store(self, knowledge_store: object) -> None:
        """Bind once to the app runtime store, rejecting split-brain wiring."""

        if knowledge_store is None:
            raise ValueError("knowledge_store is required")
        with self._ex_lock:
            if self._knowledge_store is None:
                if any(self._inflight.values()):
                    raise RuntimeError(
                        "cannot bind knowledge_store while index jobs are running"
                    )
                self._knowledge_store = knowledge_store
                return
            if self._knowledge_store is not knowledge_store:
                raise ValueError(
                    "IndexJobManager knowledge_store does not match StateRuntime"
                )

    def bind_after_index_commit(self, callback: Callable[[str, object], None]) -> None:
        if not callable(callback):
            raise TypeError("after_index_commit callback must be callable")
        with self._ex_lock:
            if any(self._inflight.values()):
                raise RuntimeError(
                    "cannot bind after_index_commit while index jobs are running"
                )
            if (
                self._after_index_commit is not None
                and self._after_index_commit is not callback
            ):
                raise ValueError("IndexJobManager after_index_commit is already bound")
            self._after_index_commit = callback

    # 返回执行器locked。
    def _get_executor_locked(self, kb_id: str) -> ThreadPoolExecutor:
        # 调用方必须已持 _ex_lock；与 release_executor/shutdown 互斥。
        if self._closed:
            raise RuntimeError("IndexJobManager is closed")
        self._prune_retired_locked()
        ex = self._executors.get(kb_id)
        if ex is None:
            live_retired = sum(
                any(thread.is_alive() for thread in getattr(retired, "_threads", ()))
                for retired in self._retired_executors
            )
            if len(self._executors) + live_retired >= _MAX_KB_EXECUTORS:
                raise RuntimeError(
                    f"per-KB executor 数量已达上限 {_MAX_KB_EXECUTORS}，拒绝新 KB"
                )
            ex = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix=f"cogdoc-kb-{kb_id[:8]}"
            )
            self._executors[kb_id] = ex
        return ex

    # 完成 prune已退役执行器locked 处理。
    def _prune_retired_locked(self) -> None:
        # shutdown(wait=False) 后 executor 对象仍持有 Thread 引用；线程全部退出后即可丢弃句柄。
        self._retired_executors = {
            ex
            for ex in self._retired_executors
            if any(thread.is_alive() for thread in getattr(ex, "_threads", ()))
        }

    # 返回执行器。
    def _get_executor(self, kb_id: str) -> ThreadPoolExecutor:
        with self._ex_lock:
            return self._get_executor_locked(kb_id)

    # 新建记录。
    def _new_record(self, kb_id: str, *, job_id: str | None = None) -> dict:
        return {
            "job_id": job_id or uuid4().hex,
            "kb_id": kb_id,
            "status": "pending",
            "created_at": _now_iso(),
            "finished_at": None,
            "document_count": None,
            "chunk_count": None,
            "ocr_summary": None,
            "error_code": None,
            "message": None,
        }

    # 记录失败任务。
    def _fail_job(
        self, job_id: str, kb_id: str, exc: Exception, error_code: str = "INGEST_FAILED"
    ) -> None:
        self._store.update(
            job_id,
            status="failed",
            error_code=error_code,
            message=str(exc),
            finished_at=_now_iso(),
        )
        log_event(
            "ingest",
            "index_job_failed",
            {"trace_id": job_id},
            level=logging.ERROR,
            kb_id=kb_id,
            error_class=type(exc).__name__,
        )

    # 完成 safe恢复状态 处理。
    def _safe_restore(self, src: str, dst: str, job_id: str, kb_id: str) -> bool:
        # 回滚恢复源文件，成功返回 True。失败不外逃但返回 False：调用方据此保留 journal 供恢复重试。
        try:
            os.replace(src, dst)
            fsync_directory(os.path.dirname(dst))
            return True
        except OSError as exc:
            try:
                self._store.update(
                    job_id, message=f"回滚失败，源目录与索引不一致: {exc}"
                )
            except Exception:
                pass
            log_event(
                "ingest",
                "index_rollback_failed",
                {"trace_id": job_id},
                level=logging.ERROR,
                kb_id=kb_id,
                error_class=type(exc).__name__,
            )
            return False

    # 回滚 upload。
    def _rollback_upload(self, job_id, kb_id, dest, backup, had_old) -> bool:
        # 把源目录恢复到上传前状态，返回是否恢复成功（失败则调用方保留 journal 供启动重试）。
        if had_old:
            if os.path.exists(backup):
                return self._safe_restore(backup, dest, job_id, kb_id)  # 恢复旧文件
            return True  # 备份未生成（replace 前失败），dest 仍是原文件
        return _silent_remove(dest)  # 新增上传：删除残缺/未提交的新文件

    # 结束回滚。
    def _finish_rollback(self, job_id: str) -> None:
        # 进程内已确认磁盘恢复到一致态：先 best-effort 写 rolled_back 终态（供 clear 失败时的崩溃恢复）， 再无条件尝试清除条目。即便标记失败，只要清除成功就不会留下 source_moved 条目阻塞下次启动。
        self._journal.mark_rolled_back(job_id)
        self._journal.clear(job_id)

    # 准备提交。
    def _prepare_commit(self, job_id: str, gen_id: str) -> None:
        # 两份证据都在 switch_active 前写入；任一步失败都会中止提交并由外层回滚源文件。
        self._journal.record_generation(job_id, gen_id)
        self._store.update(job_id, committed_generation_id=gen_id)
        stored = self._store.get(job_id)
        if stored is None or stored.get("committed_generation_id") != gen_id:
            raise RuntimeError(f"job {job_id} 的 generation 提交证据未持久化")

    def _prepare_authorized_commit(
        self,
        job_id: str,
        gen_id: str,
        authorization_guard: Callable[[], None] | None,
        kb_id: str,
        source_dir: str,
    ) -> None:
        # Persist crash-recovery evidence first, then make the live authorization
        # check before staging the HA source snapshot and switching active.
        self._prepare_commit(job_id, gen_id)
        if authorization_guard is not None:
            authorization_guard()
        if self._mutation_coordinator is not None:
            assert_live = getattr(self._mutation_coordinator, "assert_live", None)
            if not callable(assert_live):
                raise RuntimeError("mutation coordinator does not support fencing")
            # This is the last fallible authority check before switch_active.
            # A worker whose lease expired can finish expensive construction,
            # but can never publish over the new lease owner's generation.
            assert_live()
            if self._source_generation_store is not None:
                current_lease = getattr(
                    self._mutation_coordinator, "current_lease", None
                )
                stage = getattr(self._source_generation_store, "stage_for_commit", None)
                lease = current_lease() if callable(current_lease) else None
                if lease is None or not callable(stage):
                    raise RuntimeError(
                        "distributed source generation staging is unavailable"
                    )
                stage(
                    storage_id=kb_id,
                    source_dir=source_dir,
                    lease=lease,
                    build_id=gen_id,
                )
                # Object upload may outlive the lease. Recheck immediately
                # before returning to the local switch_active call.
                assert_live()

    def _assert_distributed_commit(self) -> None:
        if self._mutation_coordinator is None:
            return
        assert_live = getattr(self._mutation_coordinator, "assert_live", None)
        if not callable(assert_live):
            raise RuntimeError("mutation coordinator does not support fencing")
        assert_live()

    def _prepare_delegated_commit(
        self, generation_id: str, kb_id: str, source_dir: str
    ) -> None:
        """Stage the caller-mutated source tree for the same index generation."""

        self._assert_distributed_commit()
        if self._source_generation_store is None or self._mutation_coordinator is None:
            raise RuntimeError("delegated source generation staging is unavailable")
        current_lease = getattr(self._mutation_coordinator, "current_lease", None)
        stage = getattr(self._source_generation_store, "stage_for_commit", None)
        lease = current_lease() if callable(current_lease) else None
        if lease is None or not callable(stage):
            raise RuntimeError("delegated source generation staging is unavailable")
        stage(
            storage_id=kb_id,
            source_dir=source_dir,
            lease=lease,
            build_id=generation_id,
        )
        self._assert_distributed_commit()

    # 拒绝ifunresolved。
    def _reject_if_unresolved(self, job_id: str, kb_id: str) -> bool:
        if not self._journal.has_entries(kb_id):
            return False
        self._fail_job(
            job_id,
            kb_id,
            RuntimeError(f"KB {kb_id} 存在未恢复 mutation journal，拒绝继续写入"),
        )
        return True

    def _prepare_source_cache(self, kb_id: str, source_dir: str) -> None:
        if self._source_generation_store is None:
            return
        materialize = getattr(
            self._source_generation_store, "materialize_current", None
        )
        if not callable(materialize):
            raise RuntimeError("source generation store cannot materialize snapshots")
        materialize(kb_id, source_dir)

    # 提交tracked。
    def _submit_tracked(self, ex, kb_id: str, fn: Callable, *args):
        # 调用方须已持 _ex_lock。包一层计数：在途归零且久未活动时 sweeper 才可淘汰该 executor。
        self._inflight[kb_id] = self._inflight.get(kb_id, 0) + 1
        self._last_active[kb_id] = time.time()

        # 执行后台任务并完成收尾。
        def runner():
            retire = None
            try:
                return fn(*args)
            finally:
                with self._ex_lock:
                    if self._executors.get(kb_id) is ex:
                        self._inflight[kb_id] = max(0, self._inflight.get(kb_id, 1) - 1)
                        self._last_active[kb_id] = time.time()
                        if (
                            self._inflight[kb_id] == 0
                            and kb_id in self._retire_when_idle
                        ):
                            self._retire_when_idle.discard(kb_id)
                            self._executors.pop(kb_id, None)
                            self._inflight.pop(kb_id, None)
                            self._last_active.pop(kb_id, None)
                            self._retired_executors.add(ex)
                            retire = ex
                if retire is not None:
                    retire.shutdown(wait=False)

        return ex.submit(runner)

    def _run_distributed_claimed(
        self,
        fn: Callable,
        job_id: str,
        kb_id: str,
        base_epoch: int,
        *args,
    ) -> object:
        try:
            return self._run_distributed_claimed_inner(
                fn, job_id, kb_id, base_epoch, *args
            )
        finally:
            abort_if_unstarted = getattr(fn, "abort_if_unstarted", None)
            if callable(abort_if_unstarted):
                try:
                    abort_if_unstarted()
                except Exception as exc:
                    try:
                        self._fail_job(job_id, kb_id, exc)
                    except Exception:
                        pass

    def _run_distributed_claimed_inner(
        self,
        fn: Callable,
        job_id: str,
        kb_id: str,
        base_epoch: int,
        *args,
    ) -> object:
        distributed = getattr(self._store, "distributed", False) is True
        claim = getattr(self._store, "claim", None) if distributed else None
        bind_claim = getattr(self._store, "bind_claim", None) if distributed else None
        heartbeat = getattr(self._store, "heartbeat", None) if distributed else None
        token = claim(job_id) if callable(claim) else None
        if callable(claim) and not isinstance(token, str):
            return None

        stopped = threading.Event()
        lost: list[Exception] = []

        def keep_job_alive() -> None:
            if not callable(heartbeat) or not isinstance(token, str):
                return
            interval = max(1.0, float(getattr(self._store, "lease_seconds", 300)) / 3)
            while not stopped.wait(interval):
                try:
                    heartbeat(job_id, token)
                except Exception as exc:
                    lost.append(exc)
                    return

        claim_context = (
            bind_claim(job_id, token)
            if isinstance(token, str) and callable(bind_claim)
            else nullcontext()
        )
        lease_factory = (
            getattr(self._mutation_coordinator, "lease", None)
            if self._mutation_coordinator is not None
            else None
        )
        delegated_lease = getattr(fn, "mutation_lease", None)
        bind_lease = (
            getattr(self._mutation_coordinator, "bind_lease", None)
            if self._mutation_coordinator is not None
            else None
        )
        if delegated_lease is not None:
            if not callable(bind_lease):
                raise RuntimeError("mutation coordinator cannot bind delegated leases")
            mutation_context = bind_lease(delegated_lease)
        else:
            mutation_context = (
                lease_factory(kb_id) if callable(lease_factory) else nullcontext()
            )
        keeper = None
        if isinstance(token, str) and callable(heartbeat):
            keeper = threading.Thread(
                target=keep_job_alive,
                name=f"cogdoc-index-job-lease-{job_id[:12]}",
                daemon=True,
            )
            keeper.start()
        try:
            with claim_context:
                try:
                    with mutation_context:
                        # The per-KB executor already orders API jobs. The
                        # shared write lock additionally linearizes these file
                        # and generation mutations with CLI/control-plane
                        # deletion paths that do not enter that executor.
                        with kb_write_lock(kb_id):
                            result = fn(job_id, kb_id, base_epoch, *args)
                    stopped.set()
                    if keeper is not None:
                        keeper.join(
                            min(
                                10.0,
                                float(getattr(self._store, "lease_seconds", 300)),
                            )
                        )
                    if lost:
                        raise RuntimeError(
                            "index job lease heartbeat was lost"
                        ) from lost[0]
                    return result
                except Exception as exc:
                    # Distributed stores require the claim token for every
                    # transition. Keep the binding alive while recording a
                    # mutation-lease conflict or worker failure, otherwise the
                    # row remains spuriously running until lease reconciliation.
                    try:
                        self._fail_job(job_id, kb_id, exc)
                    except Exception:
                        pass
                    return None
        finally:
            stopped.set()
            if keeper is not None:
                keeper.join(
                    min(10.0, float(getattr(self._store, "lease_seconds", 300)))
                )

    # 入队。
    def _enqueue(
        self,
        kb_id: str,
        fn: Callable,
        *args,
        idempotency_key: str | None = None,
    ) -> dict:
        # get-create-submit 全程持锁与 release_executor 互斥：失败不留 pending，入队成功则 ex 必存活。
        with self._ex_lock:
            ex = self._get_executor_locked(kb_id)
            deterministic_job_id = None
            if idempotency_key is not None:
                if (
                    not isinstance(idempotency_key, str)
                    or not idempotency_key
                    or len(idempotency_key.encode()) > 1024
                ):
                    raise ValueError("index idempotency key is invalid")
                deterministic_job_id = hashlib.sha256(
                    f"{kb_id}\x00{idempotency_key}".encode()
                ).hexdigest()
                existing = self._store.get(deterministic_job_id)
                if existing is not None:
                    return dict(existing)
            record = self._new_record(kb_id, job_id=deterministic_job_id)
            base_epoch = self._epoch_reader(kb_id)  # 执行期错配守卫基线
            try:
                self._store.create(record)
            except Exception:
                existing = (
                    self._store.get(deterministic_job_id)
                    if deterministic_job_id is not None
                    else None
                )
                if existing is not None:
                    return dict(existing)
                raise
            try:
                self._submit_tracked(
                    ex,
                    kb_id,
                    self._run_distributed_claimed,
                    fn,
                    record["job_id"],
                    kb_id,
                    base_epoch,
                    *args,
                )
            except Exception as exc:
                # 线程创建失败/资源耗尽：record 已建，标记失败而非遗留 pending。
                self._inflight[kb_id] = max(0, self._inflight.get(kb_id, 1) - 1)
                claim = getattr(self._store, "claim", None)
                bind_claim = getattr(self._store, "bind_claim", None)
                if (
                    getattr(self._store, "distributed", False) is True
                    and callable(claim)
                    and callable(bind_claim)
                ):
                    token = claim(record["job_id"])
                    if isinstance(token, str):
                        with bind_claim(record["job_id"], token):
                            self._fail_job(record["job_id"], kb_id, exc)
                else:
                    self._fail_job(record["job_id"], kb_id, exc)
                raise
        return dict(record)

    # 提交结果。
    def submit(self, kb_id: str) -> dict:
        # 向后兼容：仅触发索引，不含文件 mutation（文件变更已在调用方完成）。
        return self._enqueue(kb_id, self._run)

    def submit_with_mutation_lease(
        self,
        kb_id: str,
        mutation_lease: object,
        *,
        idempotency_key: str | None = None,
    ) -> dict:
        """Index an already-materialized source snapshot under its live lease."""

        if self._mutation_coordinator is None:
            raise RuntimeError("delegated mutation requires a distributed coordinator")
        assert_live = getattr(self._mutation_coordinator, "assert_live", None)
        if not callable(assert_live):
            raise RuntimeError("mutation coordinator does not support fencing")
        assert_live(mutation_lease)
        return self._enqueue(
            kb_id,
            _DelegatedMutationCall(self._run_delegated, mutation_lease),
            idempotency_key=idempotency_key,
        )

    # 提交上传。
    def submit_upload(
        self,
        kb_id: str,
        source_dir: str,
        filename: str,
        content: bytes,
        on_finished: Callable[[], None] | None = None,
        authorization_guard: Callable[[], None] | None = None,
        embedding_profile_id: str | None = None,
        on_aborted: Callable[[], None] | None = None,
        on_started: Callable[[], None] | None = None,
    ) -> dict:
        # 写文件与构建索引作为一个 executor command：保证每个 job 快照与其 mutation 精确对应。
        operation: Callable = self._run_with_write
        if on_aborted is not None or on_finished is not None:

            def cleanup_unstarted() -> None:
                try:
                    if on_aborted is not None:
                        on_aborted()
                finally:
                    if on_finished is not None:
                        on_finished()

            operation = _AbortableMutationCall(operation, cleanup_unstarted)
        return self._enqueue(
            kb_id,
            operation,
            source_dir,
            filename,
            content,
            on_finished,
            authorization_guard,
            embedding_profile_id,
            on_aborted,
            on_started,
        )

    def submit_upload_batch(
        self,
        kb_id: str,
        source_dir: str,
        uploads: list[tuple[str, bytes]],
        on_finished: Callable[[], None] | None = None,
        authorization_guard: Callable[[], None] | None = None,
        embedding_profile_id: str | None = None,
        on_aborted: Callable[[], None] | None = None,
        on_started: Callable[[], None] | None = None,
    ) -> dict:
        """Commit several source mutations and one index generation as one job."""

        if not uploads:
            raise ValueError("batch upload requires at least one file")
        operation: Callable = self._run_with_writes
        if on_aborted is not None or on_finished is not None:

            def cleanup_unstarted() -> None:
                try:
                    if on_aborted is not None:
                        on_aborted()
                finally:
                    if on_finished is not None:
                        on_finished()

            operation = _AbortableMutationCall(operation, cleanup_unstarted)
        return self._enqueue(
            kb_id,
            operation,
            source_dir,
            uploads,
            on_finished,
            authorization_guard,
            embedding_profile_id,
            on_aborted,
            on_started,
        )

    # 提交删除文档。
    def submit_delete_doc(
        self,
        kb_id: str,
        path: str,
        on_succeeded: Callable[[], None] | None = None,
        authorization_guard: Callable[[], None] | None = None,
        on_retiring: Callable[[], None] | None = None,
        finalize_missing: bool = False,
    ) -> dict:
        # 存在性检查在 executor command 内进行，保证与上传队列有序：upload 排在前则文件已落盘。
        return self._enqueue(
            kb_id,
            self._run_with_delete_doc,
            path,
            on_succeeded,
            authorization_guard,
            on_retiring,
            finalize_missing,
        )

    # 运行blocking。
    def run_blocking(self, kb_id: str, fn: Callable, *args) -> object:
        # 同 KB executor 线程内调用会单线程自等待死锁，运行时直接拒绝而非仅靠注释约束。
        if threading.current_thread().name.startswith(f"cogdoc-kb-{kb_id[:8]}"):
            raise RuntimeError(f"run_blocking 不可从 KB {kb_id} 自身 executor 线程调用")
        with self._ex_lock:
            ex = self._get_executor_locked(kb_id)
            fut = self._submit_tracked(ex, kb_id, fn, *args)
        return fut.result()

    # 释放执行器。
    def release_executor(self, kb_id: str) -> None:
        # 任务内请求释放时不能立即 pop：同 executor 可能还排着第二个
        # DELETE/CREATE。等整条队列归零后再退役，避免新旧 executor 并行改同一 KB。
        with self._ex_lock:
            if self._inflight.get(kb_id, 0) > 0:
                self._retire_when_idle.add(kb_id)
                return
            ex = self._executors.pop(kb_id, None)
            self._inflight.pop(kb_id, None)
            self._last_active.pop(kb_id, None)
            self._retire_when_idle.discard(kb_id)
            if ex is not None:
                self._retired_executors.add(ex)
        if ex is not None:
            ex.shutdown(wait=False)

    # 淘汰空闲执行器。
    def evict_idle(
        self, max_idle_seconds: float, now: float | None = None
    ) -> list[str]:
        # sweeper 调用：淘汰在途归零且超过 max_idle_seconds 未活动的 executor，回收 #13 的活跃 KB 上限。
        now = now if now is not None else time.time()
        evicted = []
        with self._ex_lock:
            self._prune_retired_locked()
            for kb_id in list(self._executors):
                if self._inflight.get(kb_id, 0) != 0:
                    continue
                if now - self._last_active.get(kb_id, 0.0) <= max_idle_seconds:
                    continue
                ex = self._executors.pop(kb_id)
                self._inflight.pop(kb_id, None)
                self._last_active.pop(kb_id, None)
                self._retire_when_idle.discard(kb_id)
                evicted.append((kb_id, ex))
                self._retired_executors.add(ex)
        for _, ex in evicted:
            ex.shutdown(wait=False)
        return [kb_id for kb_id, _ in evicted]

    # 执行器命令。

    # _stale：处理对应功能。
    def _stale(self, kb_id: str, base_epoch: int) -> bool:
        # epoch 变更 = KB 已删（可能已重建）；exists=False = 已删未重建；非 active = 删库进行中，禁新 mutation。 epoch/lifecycle 读损坏抛错时 fail-closed 视为 stale：拦掉 mutation 而非把损坏当 epoch 0。
        try:
            if self._epoch_reader(kb_id) != base_epoch:
                return True
        except Exception:
            return True
        if self._kb_exists is not None and not self._kb_exists(kb_id):
            return True
        try:
            if self._lifecycle_reader(kb_id) != LIFECYCLE_ACTIVE:
                return True
        except Exception:
            return True
        return False

    # 运行结果。
    def _run(self, job_id: str, kb_id: str, base_epoch: int) -> None:
        if self._store.get(job_id) is None:
            return
        if self._stale(kb_id, base_epoch):
            self._fail_job(
                job_id, kb_id, RuntimeError(f"KB {kb_id} 已被删除或重建，构建取消")
            )
            return
        if self._reject_if_unresolved(job_id, kb_id):
            return
        source_dir = self._source_dir_for(kb_id)
        try:
            self._prepare_source_cache(kb_id, source_dir)
        except Exception as exc:
            self._fail_job(job_id, kb_id, exc)
            return
        self._run_ingest(
            job_id,
            kb_id,
            source_dir,
            on_commit=(
                (lambda _generation_id: self._assert_distributed_commit())
                if self._mutation_coordinator is not None
                else None
            ),
        )

    def _run_delegated(self, job_id: str, kb_id: str, base_epoch: int) -> None:
        """Build the caller's source snapshot without restoring an older head."""

        if self._store.get(job_id) is None:
            return
        if self._stale(kb_id, base_epoch):
            self._fail_job(
                job_id, kb_id, RuntimeError(f"KB {kb_id} 已被删除或重建，构建取消")
            )
            return
        if self._reject_if_unresolved(job_id, kb_id):
            return
        self._assert_distributed_commit()
        source_dir = self._source_dir_for(kb_id)
        self._run_ingest(
            job_id,
            kb_id,
            source_dir,
            on_commit=lambda generation_id: self._prepare_delegated_commit(
                generation_id, kb_id, source_dir
            ),
        )

    # 运行with写入。
    def _run_with_write(
        self,
        job_id: str,
        kb_id: str,
        base_epoch: int,
        source_dir: str,
        filename: str,
        content: bytes,
        on_finished: Callable[[], None] | None = None,
        authorization_guard: Callable[[], None] | None = None,
        embedding_profile_id: str | None = None,
        on_aborted: Callable[[], None] | None = None,
        on_started: Callable[[], None] | None = None,
    ) -> None:
        committed = False

        def mark_committed() -> None:
            nonlocal committed
            committed = True

        try:
            if on_started is not None:
                on_started()
            self._run_with_write_reserved(
                job_id,
                kb_id,
                base_epoch,
                source_dir,
                filename,
                content,
                authorization_guard,
                embedding_profile_id,
                mark_committed,
            )
        finally:
            try:
                if not committed and on_aborted is not None:
                    on_aborted()
            finally:
                if on_finished is not None:
                    on_finished()

    def _run_with_write_reserved(
        self,
        job_id: str,
        kb_id: str,
        base_epoch: int,
        source_dir: str,
        filename: str,
        content: bytes,
        authorization_guard: Callable[[], None] | None = None,
        embedding_profile_id: str | None = None,
        on_committed: Callable[[], None] | None = None,
    ) -> None:
        if self._store.get(job_id) is None:
            return
        if self._stale(kb_id, base_epoch):
            self._fail_job(
                job_id, kb_id, RuntimeError(f"KB {kb_id} 已被删除或重建，上传取消")
            )
            return
        if self._reject_if_unresolved(job_id, kb_id):
            return
        try:
            self._prepare_source_cache(kb_id, source_dir)
        except Exception as exc:
            self._fail_job(job_id, kb_id, exc)
            return
        if authorization_guard is not None and not self._ingest_takes_on_commit:
            self._fail_job(
                job_id,
                kb_id,
                RuntimeError("索引构建器不支持提交前权限复验"),
            )
            return
        dest = os.path.join(source_dir, filename)
        backup = mutation_backup_path(dest, job_id)  # 唯一名，绝不覆盖上次崩溃遗留的备份
        had_old = os.path.exists(dest)
        try:
            os.makedirs(source_dir, exist_ok=True)
            # 先写 journal 再动文件：崩溃在任意点都能据 journal 恢复。
            self._journal.begin_upload(job_id, kb_id, dest, backup, had_old)
            if had_old:
                os.replace(dest, backup)  # 覆盖前备份旧文件，构建失败可回滚
                fsync_directory(source_dir)
                self._journal.mark_source_moved(job_id)
            with open(dest, "wb") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            fsync_directory(source_dir)
        except Exception as exc:
            # 写入中途失败：恢复到上传前状态，仅当磁盘一致才清 journal，否则保留供启动恢复。
            if self._rollback_upload(job_id, kb_id, dest, backup, had_old):
                self._finish_rollback(job_id)
            self._fail_job(job_id, kb_id, exc)
            return
        ok = self._run_ingest(
            job_id,
            kb_id,
            source_dir,
            on_commit=lambda gid: self._prepare_authorized_commit(
                job_id, gid, authorization_guard, kb_id, source_dir
            ),
            embedding_profile_id=embedding_profile_id,
        )
        if ok:
            # The generation authority has already advanced.  Mark this before
            # journal/log cleanup so a post-commit housekeeping failure never
            # rolls back the ACL of a now-queryable document.
            if on_committed is not None:
                on_committed()
            # 已提交：先打不可逆 committed 标记，再 best-effort 清备份，最后无条件清 journal。 不能因备份清理失败而保留 journal——否则后续切代/删库会让它被误判未提交而回滚已提交源文件。
            committed_marked = self._journal.mark_committed(job_id)
            if had_old:
                _silent_remove(backup)  # 孤儿备份无害（不被索引扫描）
            if committed_marked:
                self._journal.clear(job_id)
            else:
                log_event(
                    "ingest",
                    "mutation_journal_commit_mark_failed",
                    {"trace_id": job_id},
                    level=logging.ERROR,
                    kb_id=kb_id,
                )
        elif self._rollback_upload(job_id, kb_id, dest, backup, had_old):
            self._finish_rollback(job_id)

    def _prepare_batch_authorized_commit(
        self,
        job_id: str,
        journal_ids: list[str],
        gen_id: str,
        authorization_guard: Callable[[], None] | None,
        kb_id: str,
        source_dir: str,
    ) -> None:
        for journal_id in journal_ids:
            self._journal.record_generation(journal_id, gen_id)
        self._store.update(job_id, committed_generation_id=gen_id)
        stored = self._store.get(job_id)
        if stored is None or stored.get("committed_generation_id") != gen_id:
            raise RuntimeError(f"job {job_id} 的 generation 提交证据未持久化")
        if authorization_guard is not None:
            authorization_guard()
        if self._mutation_coordinator is not None:
            assert_live = getattr(self._mutation_coordinator, "assert_live", None)
            if not callable(assert_live):
                raise RuntimeError("mutation coordinator does not support fencing")
            assert_live()
            if self._source_generation_store is not None:
                current_lease = getattr(
                    self._mutation_coordinator, "current_lease", None
                )
                stage = getattr(self._source_generation_store, "stage_for_commit", None)
                lease = current_lease() if callable(current_lease) else None
                if lease is None or not callable(stage):
                    raise RuntimeError(
                        "distributed source generation staging is unavailable"
                    )
                stage(
                    storage_id=kb_id,
                    source_dir=source_dir,
                    lease=lease,
                    build_id=gen_id,
                )
                assert_live()

    def _run_with_writes(
        self,
        job_id: str,
        kb_id: str,
        base_epoch: int,
        source_dir: str,
        uploads: list[tuple[str, bytes]],
        on_finished: Callable[[], None] | None = None,
        authorization_guard: Callable[[], None] | None = None,
        embedding_profile_id: str | None = None,
        on_aborted: Callable[[], None] | None = None,
        on_started: Callable[[], None] | None = None,
    ) -> None:
        committed = False
        try:
            if on_started is not None:
                on_started()
            if self._store.get(job_id) is None:
                return
            if self._stale(kb_id, base_epoch):
                self._fail_job(
                    job_id,
                    kb_id,
                    RuntimeError(f"KB {kb_id} 已被删除或重建，批量上传取消"),
                )
                return
            if self._reject_if_unresolved(job_id, kb_id):
                return
            try:
                self._prepare_source_cache(kb_id, source_dir)
            except Exception as exc:
                self._fail_job(job_id, kb_id, exc)
                return
            if authorization_guard is not None and not self._ingest_takes_on_commit:
                self._fail_job(
                    job_id,
                    kb_id,
                    RuntimeError("索引构建器不支持提交前权限复验"),
                )
                return

            mutations: list[tuple[str, str, str, bool]] = []
            try:
                os.makedirs(source_dir, exist_ok=True)
                for index, (filename, content) in enumerate(uploads):
                    journal_id = f"{job_id}-{index}"
                    dest = os.path.join(source_dir, filename)
                    backup = mutation_backup_path(dest, journal_id)
                    had_old = os.path.exists(dest)
                    self._journal.begin_upload(journal_id, kb_id, dest, backup, had_old)
                    mutations.append((journal_id, dest, backup, had_old))
                    if had_old:
                        os.replace(dest, backup)
                        fsync_directory(source_dir)
                        self._journal.mark_source_moved(journal_id)
                    with open(dest, "wb") as handle:
                        handle.write(content)
                        handle.flush()
                        os.fsync(handle.fileno())
                    fsync_directory(source_dir)
            except Exception as exc:
                restored = True
                for journal_id, dest, backup, had_old in reversed(mutations):
                    if self._rollback_upload(journal_id, kb_id, dest, backup, had_old):
                        self._finish_rollback(journal_id)
                    else:
                        restored = False
                self._fail_job(job_id, kb_id, exc)
                if not restored:
                    self._store.update(
                        job_id, message="批量上传回滚失败，需恢复 mutation journal"
                    )
                return

            journal_ids = [mutation[0] for mutation in mutations]
            ok = self._run_ingest(
                job_id,
                kb_id,
                source_dir,
                on_commit=lambda gid: self._prepare_batch_authorized_commit(
                    job_id,
                    journal_ids,
                    gid,
                    authorization_guard,
                    kb_id,
                    source_dir,
                ),
                embedding_profile_id=embedding_profile_id,
            )
            committed = ok
            if ok:
                for journal_id, _dest, backup, had_old in mutations:
                    committed_marked = self._journal.mark_committed(journal_id)
                    if had_old:
                        _silent_remove(backup)
                    if committed_marked:
                        self._journal.clear(journal_id)
            else:
                for journal_id, dest, backup, had_old in reversed(mutations):
                    if self._rollback_upload(journal_id, kb_id, dest, backup, had_old):
                        self._finish_rollback(journal_id)
        finally:
            try:
                if not committed and on_aborted is not None:
                    on_aborted()
            finally:
                if on_finished is not None:
                    on_finished()

    # 运行with删除文档。
    def _run_with_delete_doc(
        self,
        job_id: str,
        kb_id: str,
        base_epoch: int,
        path: str,
        on_succeeded: Callable[[], None] | None = None,
        authorization_guard: Callable[[], None] | None = None,
        on_retiring: Callable[[], None] | None = None,
        finalize_missing: bool = False,
    ) -> None:
        if self._store.get(job_id) is None:
            return
        if self._stale(kb_id, base_epoch):
            self._fail_job(
                job_id, kb_id, RuntimeError(f"KB {kb_id} 已被删除或重建，删除取消")
            )
            return
        if self._reject_if_unresolved(job_id, kb_id):
            return
        if authorization_guard is not None and not self._ingest_takes_on_commit:
            self._fail_job(
                job_id,
                kb_id,
                RuntimeError("索引构建器不支持提交前权限复验"),
            )
            return
        if not os.path.exists(path) and finalize_missing and on_succeeded is not None:
            try:
                if authorization_guard is not None:
                    authorization_guard()
                on_succeeded()
                self._store.update(
                    job_id,
                    status="succeeded",
                    document_count=0,
                    chunk_count=0,
                    finished_at=_now_iso(),
                )
                log_event(
                    "ingest",
                    "document_delete_finalized",
                    {"trace_id": job_id},
                    kb_id=kb_id,
                )
            except Exception as exc:
                self._fail_job(job_id, kb_id, exc)
            return
        try:
            self._prepare_source_cache(kb_id, os.path.dirname(path))
        except Exception as exc:
            self._fail_job(job_id, kb_id, exc)
            return
        if not os.path.exists(path):
            self._fail_job(
                job_id,
                kb_id,
                FileNotFoundError(f"文档不存在: {os.path.basename(path)}"),
                error_code="DOCUMENT_NOT_FOUND",
            )
            return
        if on_retiring is not None:
            try:
                # The old local/HA generation may remain queryable until the
                # replacement is published. Fence access before moving the
                # source so every later failure is fail-closed.
                on_retiring()
            except Exception as exc:
                self._fail_job(job_id, kb_id, exc)
                return
        quarantine = mutation_backup_path(path, job_id)  # 唯一名，避免覆盖遗留备份
        try:
            self._journal.begin_delete(job_id, kb_id, path, quarantine)
            os.replace(path, quarantine)  # 移入隔离区而非直接删除，构建失败可恢复
            fsync_directory(os.path.dirname(path))
            self._journal.mark_source_moved(job_id)
        except Exception as exc:
            if os.path.exists(quarantine):
                restored = self._safe_restore(quarantine, path, job_id, kb_id)
            else:
                restored = os.path.exists(path)  # replace 前失败，原文件仍在
            if restored:
                self._finish_rollback(job_id)
            self._fail_job(job_id, kb_id, exc)
            return
        ok = self._run_ingest(
            job_id,
            kb_id,
            os.path.dirname(path),
            on_commit=lambda gid: self._prepare_authorized_commit(
                job_id, gid, authorization_guard, kb_id, os.path.dirname(path)
            ),
            on_succeeded=on_succeeded,
        )
        if ok:
            committed_marked = self._journal.mark_committed(job_id)
            _silent_remove(quarantine)  # 孤儿隔离文件无害，best-effort
            if committed_marked:
                self._journal.clear(job_id)
            else:
                log_event(
                    "ingest",
                    "mutation_journal_commit_mark_failed",
                    {"trace_id": job_id},
                    level=logging.ERROR,
                    kb_id=kb_id,
                )
        elif self._safe_restore(quarantine, path, job_id, kb_id):
            self._finish_rollback(job_id)

    # 运行ingest。
    def _run_ingest(
        self,
        job_id,
        kb_id,
        source_dir,
        on_commit=None,
        on_succeeded: Callable[[], None] | None = None,
        embedding_profile_id: str | None = None,
    ) -> bool:
        try:
            self._store.update(job_id, status="running")
        except Exception:
            pass
        try:
            ingest_kwargs = {}
            if self._ingest_takes_on_commit:
                # 提交点贴死 switch_active：build 在提交前用 gen_id 回调 record_generation。
                ingest_kwargs["on_commit"] = on_commit
            if self._ingest_takes_knowledge_store and self._knowledge_store is not None:
                ingest_kwargs["knowledge_store"] = self._knowledge_store
            if (
                self._ingest_takes_embedding_profile
                and embedding_profile_id is not None
            ):
                ingest_kwargs["embedding_profile_id"] = embedding_profile_id
            # 不支持新参数的旧/测试 ingest_fn 保持原有两参数调用契约。
            result = self._ingest_fn(kb_id, source_dir, **ingest_kwargs)
        except Exception as exc:
            # 构建未提交（active 仍是旧代）：返回 False 触发源文件回滚。
            self._fail_job(job_id, kb_id, exc)
            return False
        if self._after_index_commit is not None:
            try:
                self._after_index_commit(kb_id, result)
            except Exception as exc:
                # The local generation is already committed. Report the mirror
                # failure but never roll source files back across that authority
                # transition. Destructive ACL cleanup remains pending, so an
                # older HA generation cannot become visible under a newer policy.
                self._fail_job(job_id, kb_id, exc, error_code="HA_MIRROR_FAILED")
                return True
        if on_succeeded is not None:
            try:
                # Both local and HA authorities now exclude deleted sources.
                on_succeeded()
            except Exception as exc:
                self._fail_job(job_id, kb_id, exc)
                return True
        # 索引已提交：终态状态写入做退避重试（缓解 SQLite 瞬时锁），仍失败则记 error，不回滚已生效源文件。
        last_exc = None
        for attempt in range(4):
            try:
                self._store.update(
                    job_id,
                    status="succeeded",
                    document_count=result.document_count,
                    chunk_count=result.chunk_count,
                    ocr_summary=getattr(result, "ocr_summary", None),
                    finished_at=_now_iso(),
                )
                last_exc = None
                break
            except Exception as exc:
                last_exc = exc
                if attempt < 3:
                    time.sleep(
                        0.1 * (2**attempt)
                    )  # 0.1/0.2/0.4s 退避，给长事务锁释放窗口
        if last_exc is None:
            log_event(
                "ingest",
                "index_job_succeeded",
                {"trace_id": job_id},
                kb_id=kb_id,
                document_count=result.document_count,
            )
        else:
            # 任务实际已成功，状态持久化反复失败：记 error 供运维介入，避免长期停在 running 而无痕。
            log_event(
                "ingest",
                "index_job_commit_record_failed",
                {"trace_id": job_id},
                level=logging.ERROR,
                kb_id=kb_id,
                error_class=type(last_exc).__name__,
            )
        return True

    # 判断 busy 是否成立。
    def is_busy(self, kb_id: str) -> bool:
        # 该 KB 是否有在途命令；sweeper 据此避免重复排队同一个重建任务。
        with self._ex_lock:
            return self._inflight.get(kb_id, 0) > 0

    # 返回结果。
    def get(self, job_id: str) -> dict | None:
        return self._store.get(job_id)

    def list(self, kb_ids: set[str], *, limit: int = 200) -> list[dict]:
        list_jobs = getattr(self._store, "list", None)
        if not callable(list_jobs):
            raise RuntimeError("index job store does not support collection reads")
        return list_jobs(kb_ids, limit=limit)

    def clear_kb(self, kb_id: str) -> None:
        clear_jobs = getattr(self._store, "clear_kb", None)
        if not callable(clear_jobs):
            raise RuntimeError("index job store does not support KB cleanup")
        clear_jobs(kb_id)
        # Journal files live outside the per-KB source tree. They are recovery
        # capabilities, not audit history, and must not fence a future
        # incarnation that reuses the deterministic storage id.
        self._journal.clear_kb(kb_id)

    # 协调孤儿任务。
    def reconcile_orphans(self) -> None:
        reconcile = getattr(self._store, "reconcile_orphans", None)
        if callable(reconcile):
            reconcile()

    def reopen(self) -> None:
        """Re-enable admission after a fully drained lifespan shutdown."""

        with self._ex_lock:
            if not self._closed:
                return
            if (
                self._executors
                or self._retired_executors
                or any(self._inflight.values())
            ):
                raise RuntimeError("cannot reopen IndexJobManager while work remains")
            self._closed = False

    # 完成 shutdown 处理。
    def shutdown(self, wait: bool = True) -> None:
        with self._ex_lock:
            self._closed = True
            executors = list(self._executors.values())
            executors.extend(self._retired_executors)
            self._executors.clear()
            self._retired_executors.clear()
            self._inflight.clear()
            self._last_active.clear()
            self._retire_when_idle.clear()
        # 锁外排空：wait=True 等在途 mutation 跑完再返回，保证 lifespan 释放进程锁前无后台写线程。 不持 _ex_lock 等待，否则 runner finally 取 _ex_lock 会与之死锁。
        for ex in executors:
            ex.shutdown(wait=wait)
