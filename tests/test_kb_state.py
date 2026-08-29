import pytest
from cogdoc.service.kb_epoch import EpochStore
from cogdoc.service.kb_state import (
    GENERATION_BUILDING,
    GENERATION_READY,
    KBState,
    StaleGenerationError,
    new_generation_id,
)


# 构造或驱动 状态 测试场景。
def _state(tmp_path, kb_id="kb"):
    # 共享同一个 epochs 文件，让重开的实例看到同一份 epoch。
    epochs = EpochStore(path=str(tmp_path / "epochs.json"))
    return KBState(kb_id, path=str(tmp_path / kb_id / "state.json"), epochs=epochs)


# 验证 generation ids are unique and short。
def test_generation_ids_are_unique_and_short():
    ids = {new_generation_id() for _ in range(100)}
    assert len(ids) == 100
    assert all(len(i) <= 13 for i in ids)


# 验证 initial state has no active。
def test_initial_state_has_no_active(tmp_path):
    st = _state(tmp_path)
    assert st.active() is None
    assert st.epoch == 0


# 验证 build ready switch lifecycle。
def test_build_ready_switch_lifecycle(tmp_path):
    st = _state(tmp_path)
    gen = st.begin_generation(
        embedding_model="m1",
        index_build_version="v1",
        chunk_identity_version="chunks-v1",
    )
    assert st.get(gen)["status"] == GENERATION_BUILDING
    with pytest.raises(ValueError):
        st.switch_active(gen)  # building 不能激活

    st.mark_ready(gen, expected_count=3, documents=[{"name": "a.pdf", "sha256": "x"}])
    st.switch_active(gen)
    active = st.active()
    assert active["id"] == gen and active["status"] == GENERATION_READY
    assert active["expected_count"] == 3
    assert active["chunk_identity_version"] == "chunks-v1"


# 验证 illegal transitions raise。
def test_illegal_transitions_raise(tmp_path):
    st = _state(tmp_path)
    g = st.begin_generation("m1", "v1")
    st.mark_failed(g)
    # failed 不能复活成 ready。
    with pytest.raises(ValueError):
        st.mark_ready(g, 1, [])
    # 缺失 generation 抛错而非静默。
    with pytest.raises(KeyError):
        st.mark_ready("nope", 1, [])


# 验证 active generation cannot be failed or removed。
def test_active_generation_cannot_be_failed_or_removed(tmp_path):
    st = _state(tmp_path)
    g = st.begin_generation("m1", "v1")
    st.mark_ready(g, 1, [])
    st.switch_active(g)
    with pytest.raises(ValueError):
        st.mark_failed(g)
    with pytest.raises(ValueError):
        st.remove_generation(g)


# 验证纪元变更后拒绝过期代际。
def test_switch_rejects_stale_generation_after_epoch_bump(tmp_path):
    st = _state(tmp_path)
    g = st.begin_generation("m1", "v1")  # 基准纪元为零
    st.mark_ready(g, 1, [])
    st.bump_epoch()  # 模拟构建期间删库
    with pytest.raises(StaleGenerationError):
        st.switch_active(g)
    assert st.active() is None  # 在线索引未被污染


# 验证 epoch survives separate store。
def test_epoch_survives_separate_store(tmp_path):
    # epoch 存在 KB 目录外：删 state.json（删库）后重建，epoch 续增不归零。
    epochs = EpochStore(path=str(tmp_path / "epochs.json"))
    st = KBState("kb", path=str(tmp_path / "kb" / "state.json"), epochs=epochs)
    st.bump_epoch()
    st.bump_epoch()
    # 删库：抹掉 KB 目录（含 state.json），但 epochs.json 在目录外仍在。
    import shutil

    shutil.rmtree(tmp_path / "kb", ignore_errors=True)
    fresh = KBState("kb", path=str(tmp_path / "kb" / "state.json"), epochs=epochs)
    assert fresh.epoch == 2  # 没归零
    # 删库前的旧代际若现在才提交，会因纪元不匹配被拒。
    g = fresh.begin_generation("m1", "v1")
    fresh.mark_ready(g, 0, [])
    fresh.bump_epoch()  # 又一次删库
    with pytest.raises(StaleGenerationError):
        fresh.switch_active(g)


# 验证 epoch invalid entry fails closed。
def test_epoch_invalid_entry_fails_closed(tmp_path):
    import json
    from cogdoc.service.kb_epoch import EpochCorruptError

    path = tmp_path / "epochs.json"
    path.write_text(json.dumps({"kb": "bad"}), encoding="utf-8")
    store = EpochStore(path=str(path))
    with pytest.raises(EpochCorruptError):
        store.current("kb")
    assert (tmp_path / "epochs.json.degraded").exists()


# 验证 stale excludes active and building。
def test_stale_excludes_active_and_building(tmp_path):
    st = _state(tmp_path)
    g1 = st.begin_generation("m1", "v1")
    st.mark_ready(g1, 1, [])
    st.switch_active(g1)
    g2 = st.begin_generation("m1", "v1")  # building，未超租约
    g3 = st.begin_generation("m1", "v1")
    st.mark_failed(g3)

    stale = st.stale_generation_ids(lease_seconds=3600)
    assert g3 in stale  # failed 可回收
    assert g1 not in stale  # active 不回收
    assert g2 not in stale  # 在建的 staging 不回收


# 验证 stale reaps zombie building past lease。
def test_stale_reaps_zombie_building_past_lease(tmp_path):
    st = _state(tmp_path)
    g = st.begin_generation("m1", "v1")
    # 租约设 -1：任何 building 都算超租约（模拟构建进程崩溃留下的僵尸）。
    assert g in st.stale_generation_ids(lease_seconds=-1)


# 验证 fresh ready not active is not stale。
def test_fresh_ready_not_active_is_not_stale(tmp_path):
    # 关键：mark_ready 之后、switch_active 之前的 staging 绝不能被 GC 当 stale 误删。
    st = _state(tmp_path)
    g1 = st.begin_generation("m1", "v1")
    st.mark_ready(g1, 1, [])
    st.switch_active(g1)
    g2 = st.begin_generation("m1", "v1")
    st.mark_ready(g2, 1, [])  # ready 但尚未 active
    assert g2 not in st.stale_generation_ids(lease_seconds=3600)


# 验证 switch supersedes old active and returns it。
def test_switch_supersedes_old_active_and_returns_it(tmp_path):
    st = _state(tmp_path)
    g1 = st.begin_generation("m1", "v1")
    st.mark_ready(g1, 1, [])
    assert st.switch_active(g1) is None  # 首次提交无旧 active

    g2 = st.begin_generation("m1", "v1")
    st.mark_ready(g2, 1, [])
    previous = st.switch_active(g2)
    assert previous == g1
    # 旧 active 被标记 superseded，立即可回收；新 active 不回收。
    assert st.get(g1)["status"] == "superseded"
    assert g1 in st.stale_generation_ids()
    assert g2 not in st.stale_generation_ids()


def test_rollback_active_reactivates_retained_superseded_generation(tmp_path):
    st = _state(tmp_path)
    g1 = st.begin_generation("embed", "build-v1", "chunk-v6")
    st.mark_ready(g1, 1, [])
    st.switch_active(g1)
    g2 = st.begin_generation("embed", "build-v2", "chunk-v7")
    st.mark_ready(g2, 1, [])
    st.switch_active(g2)

    replaced = st.rollback_active(g1)

    assert replaced == g2
    assert st.active()["id"] == g1
    assert st.get(g1)["status"] == "ready"
    assert st.get(g2)["status"] == "superseded"


def test_retained_superseded_generation_is_hidden_from_sweeper_until_release(
    tmp_path,
):
    st = _state(tmp_path)
    old = st.begin_generation("embed", "build-v1", "chunk-v6")
    st.mark_ready(old, 1, [])
    st.switch_active(old)
    new = st.begin_generation("embed", "build-v2", "chunk-v7")
    st.mark_ready(new, 1, [])
    st.switch_active(new, retain_previous_generation=True)

    assert st.get(old)["gc_protected"] is True
    assert old not in st.stale_generation_ids()

    st.release_generation_retention(old)
    assert old in st.stale_generation_ids()


def test_rollback_active_rejects_a_newer_active_generation(tmp_path):
    st = _state(tmp_path)
    generations = []
    for build in ("build-v1", "build-v2", "build-v3"):
        generation = st.begin_generation("embed", build, "chunk-v7")
        st.mark_ready(generation, 1, [])
        st.switch_active(generation)
        generations.append(generation)

    with pytest.raises(StaleGenerationError, match="active generation changed"):
        st.rollback_active(generations[0], expected_current_id=generations[1])

    assert st.active()["id"] == generations[2]


# 验证 mark ready rejects negative count。
def test_mark_ready_rejects_negative_count(tmp_path):
    st = _state(tmp_path)
    g = st.begin_generation("m1", "v1")
    with pytest.raises(ValueError):
        st.mark_ready(g, -1, [])


# 验证 mark failed on ready gen。
def test_mark_failed_on_ready_gen(tmp_path):
    # switch_active 失败时（StaleGenerationError）staging 处于 ready 态需要被丢弃。
    st = _state(tmp_path)
    g = st.begin_generation("m1", "v1")
    st.mark_ready(g, 3, [])
    st.mark_failed(g)  # ready → failed 必须成功
    assert st.get(g)["status"] == "failed"
    assert g in st.stale_generation_ids()  # failed 可立即被 GC


# 验证 mark failed on non active only。
def test_mark_failed_on_non_active_only(tmp_path):
    # active 代不可被 mark_failed，其他状态可以。
    st = _state(tmp_path)
    g = st.begin_generation("m1", "v1")
    st.mark_ready(g, 1, [])
    st.switch_active(g)
    with pytest.raises(ValueError):
        st.mark_failed(g)  # active 不可 fail


# 验证 returned documents are deep copied。
def test_returned_documents_are_deep_copied(tmp_path):
    st = _state(tmp_path)
    docs = [{"name": "a.pdf", "sha256": "x"}]
    g = st.begin_generation("m1", "v1")
    st.mark_ready(g, 1, docs)
    # 改输入列表不应影响已存状态。
    docs[0]["sha256"] = "tampered"
    got = st.get(g)
    assert got["documents"][0]["sha256"] == "x"
    # 改返回值也不应影响内存/磁盘状态。
    got["documents"][0]["sha256"] = "tampered2"
    assert st.get(g)["documents"][0]["sha256"] == "x"


# 验证 state persists across instances。
def test_state_persists_across_instances(tmp_path):
    st = _state(tmp_path)
    g = st.begin_generation("m1", "v1", "chunks-v1")
    st.mark_ready(g, 0, [])
    st.switch_active(g)
    reopened = _state(tmp_path)
    assert reopened.active()["id"] == g
    assert reopened.active()["expected_count"] == 0  # ready+0 合法空索引
    assert reopened.active()["chunk_identity_version"] == "chunks-v1"


def test_legacy_state_without_chunk_identity_version_remains_readable(tmp_path):
    import json

    path = tmp_path / "kb" / "state.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "kb_id": "kb",
                "active_generation": "glegacy",
                "generations": {
                    "glegacy": {
                        "id": "glegacy",
                        "status": GENERATION_READY,
                        "embedding_model": "m1",
                        "index_build_version": "v1",
                        "base_epoch": 0,
                        "expected_count": 0,
                        "documents": [],
                        "created_at": 1.0,
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    active = _state(tmp_path).active()
    assert active is not None
    assert active["id"] == "glegacy"
    assert "chunk_identity_version" not in active


# 验证 corrupt state file recovers。
def test_corrupt_state_file_recovers(tmp_path):
    path = tmp_path / "kb" / "state.json"
    path.parent.mkdir(parents=True)
    path.write_text("{ broken json", encoding="utf-8")
    st = KBState("kb", path=str(path), epochs=EpochStore(path=str(tmp_path / "e.json")))
    assert st.active() is None


# 验证 structurally invalid state sanitized。
def test_structurally_invalid_state_sanitized(tmp_path):
    # 合法 JSON 但 active 指向不存在的 generation、status 非法 —— 加载时清洗。
    import json

    path = tmp_path / "kb" / "state.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "active_generation": "ghost",
                "generations": {
                    "bad": {"id": "bad", "status": "weird"},
                },
            }
        ),
        encoding="utf-8",
    )
    st = KBState("kb", path=str(path), epochs=EpochStore(path=str(tmp_path / "e.json")))
    assert st.active() is None
    assert st.generation_ids() == []  # 非法 status 被丢弃
