import time
from unittest.mock import MagicMock
from cogdoc.api.ingest import IndexJobManager
from cogdoc.service import sweeper as sweeper_mod
from cogdoc.service.kb_epoch import EpochStore
from cogdoc.service.kb_state import KBState


# 验证 sweep gcs stale generations。
def test_sweep_gcs_stale_generations(tmp_path, monkeypatch):
    # 僵尸 failed 代应被清扫调用 _cleanup_generation_storage 回收。
    epochs = EpochStore(path=str(tmp_path / "ep.json"))
    state = KBState("kb1", path=str(tmp_path / "kb1" / "state.json"), epochs=epochs)
    gid = state.begin_generation("m", "v")
    state.mark_failed(gid)

    monkeypatch.setattr(sweeper_mod, "KBState", lambda kb_id: state)
    cleaned = []
    monkeypatch.setattr(
        sweeper_mod.ingest_service,
        "_cleanup_generation_storage",
        lambda kb, g: cleaned.append(g),
    )

    sw = sweeper_mod.BackgroundSweeper(
        kb_ids_provider=lambda: ["kb1"], index_jobs=MagicMock()
    )
    sw.sweep_once()
    assert gid in cleaned


# 验证 sweep calls evict and compact。
def test_sweep_calls_evict_and_compact(monkeypatch):
    jobs = MagicMock()
    compacted = []
    monkeypatch.setattr(
        sweeper_mod, "compact_locks", lambda keep: compacted.append(keep)
    )
    monkeypatch.setattr(
        sweeper_mod, "KBState", lambda kb_id: MagicMock(stale_generation_ids=lambda: [])
    )

    sw = sweeper_mod.BackgroundSweeper(
        kb_ids_provider=lambda: ["a", "b"], index_jobs=jobs
    )
    sw.sweep_once()
    jobs.evict_idle.assert_called_once()
    assert compacted == [{"a", "b"}]


def test_sweep_runs_bounded_connector_maintenance(monkeypatch):
    jobs = MagicMock()
    maintenance = MagicMock()
    monkeypatch.setattr(
        sweeper_mod, "KBState", lambda kb_id: MagicMock(stale_generation_ids=lambda: [])
    )
    sweeper = sweeper_mod.BackgroundSweeper(
        kb_ids_provider=lambda: [],
        index_jobs=jobs,
        maintenance_tasks={"connector_retention": maintenance},
    )

    sweeper.sweep_once()

    maintenance.assert_called_once_with()


# 验证 evict idle removes idle executor。
def test_evict_idle_removes_idle_executor():
    mgr = IndexJobManager(
        ingest_fn=lambda k, d: MagicMock(document_count=0, chunk_count=0)
    )
    mgr._get_executor("kb-idle")
    mgr._inflight["kb-idle"] = 0
    mgr._last_active["kb-idle"] = time.time() - 1000

    evicted = mgr.evict_idle(max_idle_seconds=900)
    assert evicted == ["kb-idle"]
    assert "kb-idle" not in mgr._executors
    mgr.shutdown()


# 验证 evict idle keeps busy executor。
def test_evict_idle_keeps_busy_executor():
    mgr = IndexJobManager(
        ingest_fn=lambda k, d: MagicMock(document_count=0, chunk_count=0)
    )
    mgr._get_executor("kb-busy")
    mgr._inflight["kb-busy"] = 1  # 在途，不可淘汰
    mgr._last_active["kb-busy"] = 0.0

    assert mgr.evict_idle(max_idle_seconds=0) == []
    assert "kb-busy" in mgr._executors
    mgr.shutdown()


# 验证 compact locks drops unkept。
def test_compact_locks_drops_unkept():
    import cogdoc.service.kb_locks as kl

    # with 进出后引用计数归零，方可被压缩；模拟锁曾被用过但当前空闲。
    with kl.kb_write_lock("keep-me"):
        pass
    with kl.kb_write_lock("drop-me"):
        pass
    kl.compact_locks({"keep-me"})
    assert "drop-me" not in kl._locks
    assert "keep-me" in kl._locks


# 验证 compact locks keeps referenced handle。
def test_compact_locks_keeps_referenced_handle():
    # 已发放但未释放的句柄（即便尚未 acquire）引用计数 >0，不得被回收，杜绝同名 KB 两把锁。
    import cogdoc.service.kb_locks as kl

    handle = kl.kb_write_lock("busy")  # 仅构造，未进入 with
    kl.compact_locks(set())
    assert "busy" in kl._locks
    with handle:  # 用一次再退出，归零
        pass
    kl.compact_locks(set())
    assert "busy" not in kl._locks
