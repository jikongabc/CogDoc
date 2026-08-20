import logging
import threading
from collections.abc import Callable, Mapping
from cogdoc.observability.logger import log_event
from cogdoc.service import ingest_service
from cogdoc.service.kb_locks import compact_locks
from cogdoc.service.kb_state import KBState


# 常驻后台回收：GC 崩溃遗留的僵尸 generation(#12)、淘汰空闲 executor(#13)、压缩锁表(#15)。
class BackgroundSweeper:
    # 常驻后台回收：GC 崩溃遗留的僵尸 generation(#12)、淘汰空闲 executor(#13)、压缩锁表(#15)。
    def __init__(
        self,
        kb_ids_provider: Callable[[], list],
        index_jobs,
        interval_seconds: float = 300.0,
        executor_idle_seconds: float = 900.0,
        maintenance_tasks: Mapping[str, Callable[[], object]] | None = None,
    ):
        self._kb_ids = kb_ids_provider
        self._index_jobs = index_jobs
        self._interval = interval_seconds
        self._idle = executor_idle_seconds
        self._maintenance_tasks = dict(maintenance_tasks or {})
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # 启动结果。
    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._loop, name="cogdoc-sweeper", daemon=True
        )
        self._thread.start()

    # 停止。
    def stop(self, join_timeout: float | None = None) -> None:
        # 置停止信号并 join：返回后保证清扫线程不再操作索引，可安全释放进程锁。
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=join_timeout)

    # 循环执行结果。
    def _loop(self) -> None:
        # wait 返回 True 表示收到停止信号；否则超时一轮，执行一次清扫。
        while not self._stop.wait(self._interval):
            try:
                self.sweep_once()
            except Exception:
                log_event("sweeper", "sweep_failed", {}, level=logging.ERROR)

    # 清扫单次执行。
    def sweep_once(self) -> None:
        kb_ids = list(self._kb_ids())
        tasks = (
            ("purge_queue", ingest_service.drain_purge_queue),
            ("generation_gc", lambda: self._gc_stale_generations(kb_ids)),
            ("model_rebuild", lambda: self._rebuild_stale_models(kb_ids)),
            ("executor_evict", lambda: self._index_jobs.evict_idle(self._idle)),
            ("lock_compact", lambda: compact_locks(set(kb_ids))),
            *tuple(self._maintenance_tasks.items()),
        )
        for task_name, task in tasks:
            try:
                task()
            except Exception as exc:
                log_event(
                    "sweeper",
                    "sweep_task_failed",
                    {},
                    level=logging.ERROR,
                    task=task_name,
                    error_class=type(exc).__name__,
                )

    # 完成 rebuildstale模型列表 处理。
    def _rebuild_stale_models(self, kb_ids: list) -> None:
        # active 代的嵌入模型 / 构建版本与当前不符：自动排队重建，不必等用户再次改文档。
        for kb_id in kb_ids:
            try:
                active = KBState(kb_id).active()
            except Exception:
                continue
            if active is None:
                continue
            stale = (
                active.get("embedding_model") != ingest_service.Embedder.MODEL_NAME
                or active.get("index_build_version")
                != ingest_service.INDEX_BUILD_VERSION
            )
            if stale and not self._index_jobs.is_busy(kb_id):
                try:
                    self._index_jobs.submit(kb_id)  # 重建生成与当前模型/版本一致的新代
                except Exception:
                    pass

    # 回收stale索引代。
    def _gc_stale_generations(self, kb_ids: list) -> None:
        # 只回收 failed / superseded / 超租约的在飞代，绝不动 active；清理失败留待下轮重试。
        for kb_id in kb_ids:
            try:
                stale = KBState(kb_id).stale_generation_ids()
            except Exception:
                continue
            for gen_id in stale:
                try:
                    ingest_service._cleanup_generation_storage(kb_id, gen_id)
                except Exception:
                    pass
