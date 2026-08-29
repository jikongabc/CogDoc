import threading
import time
from types import SimpleNamespace

import cogdoc.agents.summary_generator as summary_generator
from cogdoc.agents.summary_generator import resolve_section_workers, run_section_cells


# 验证 resolve workers serializes local and single task 场景。
def test_resolve_workers_serializes_local_and_single_task():
    assert resolve_section_workers(is_local=True, task_count=10) == 1
    assert resolve_section_workers(is_local=False, task_count=1) == 1
    assert resolve_section_workers(is_local=False, task_count=0) == 1


# 验证 resolve workers caps at task count and pool limit 场景。
def test_resolve_workers_caps_at_task_count_and_pool_limit():
    # 任务数小于上限时按任务数；超过上限时被 CLOUD_SECTION_MAX_WORKERS 钳制。
    assert resolve_section_workers(is_local=False, task_count=3) == 3
    assert resolve_section_workers(is_local=False, task_count=100) == 6


def test_resolve_workers_reads_current_settings(monkeypatch):
    monkeypatch.setattr(
        summary_generator,
        "get_settings",
        lambda: SimpleNamespace(cloud_section_max_workers=2),
    )

    assert resolve_section_workers(is_local=False, task_count=100) == 2


# 验证 run section cells preserves input order under jitter 场景。
def test_run_section_cells_preserves_input_order_under_jitter():
    # 先提交的任务故意睡更久，完成顺序与输入相反，验证返回仍按输入顺序。
    def worker(index):
        time.sleep((5 - index) * 0.02)
        return index

    result = run_section_cells(list(range(5)), worker, is_local=False)
    assert result == [0, 1, 2, 3, 4]


# 验证 run section cells runs concurrently when cloud 场景。
def test_run_section_cells_runs_concurrently_when_cloud():
    # 三个任务并发时，最大同时在场数应大于 1，证明确实并行而非串行。
    active = 0
    peak = 0
    lock = threading.Lock()

    # 构造或驱动 worker 测试场景。
    def worker(_):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.05)
        with lock:
            active -= 1
        return _

    run_section_cells([0, 1, 2], worker, is_local=False)
    assert peak > 1


# 验证 run section cells local runs serially 场景。
def test_run_section_cells_local_runs_serially():
    # 本地模式必须串行，任何时刻只有一个 worker 在场。
    active = 0
    peak = 0

    # 构造或驱动 worker 测试场景。
    def worker(_):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        active -= 1
        return _

    result = run_section_cells([0, 1, 2], worker, is_local=True)
    assert result == [0, 1, 2]
    assert peak == 1
