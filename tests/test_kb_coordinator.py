import threading
import pytest
from unittest.mock import MagicMock, patch
from cogdoc.api.ingest import IndexJobManager
from cogdoc.service import ingest_service
from cogdoc.service.ingest_service import delete_kb_index_transactional
from cogdoc.service.kb_epoch import EpochStore
from cogdoc.service.kb_state import KBState, StaleGenerationError


# 构造状态。
def _make_state(tmp_path, kb_id="kb"):
    epochs = EpochStore(path=str(tmp_path / "epochs.json"))
    return KBState(
        kb_id, path=str(tmp_path / kb_id / "state.json"), epochs=epochs
    ), epochs


# 测试单知识库执行器。


# 验证 different kbs get different executors。
def test_different_kbs_get_different_executors():
    mgr = IndexJobManager(
        ingest_fn=lambda kb, d: MagicMock(document_count=0, chunk_count=0)
    )
    ex_a = mgr._get_executor("kb-a")
    ex_b = mgr._get_executor("kb-b")
    assert ex_a is not ex_b
    mgr.shutdown()


# 验证 same kb reuses executor。
def test_same_kb_reuses_executor():
    mgr = IndexJobManager(
        ingest_fn=lambda kb, d: MagicMock(document_count=0, chunk_count=0)
    )
    ex1 = mgr._get_executor("kb-x")
    ex2 = mgr._get_executor("kb-x")
    assert ex1 is ex2
    mgr.shutdown()


# 验证 default ingest fn is transactional。
def test_default_ingest_fn_is_transactional():
    # 默认必须指向事务化构建，而非旧 build_kb_index。
    mgr = IndexJobManager()
    assert mgr._ingest_fn is ingest_service.build_kb_index_transactional
    mgr.shutdown()


# 验证任务管理器向支持该参数的入库函数注入派生知识存储。
def test_index_job_manager_injects_knowledge_store():
    seen = []
    knowledge_store = object()

    def ingest(kb_id, source_dir, *, knowledge_store=None):
        seen.append(knowledge_store)
        return MagicMock(document_count=0, chunk_count=0)

    mgr = IndexJobManager(
        ingest_fn=ingest,
        source_dir_for=lambda kb: "/fake",
        knowledge_store=knowledge_store,
    )
    mgr.submit("kb")
    mgr.run_blocking("kb", lambda: None)
    mgr.shutdown()

    assert seen == [knowledge_store]


# 验证 **kwargs 适配器会收到两类可选注入参数。
def test_index_job_manager_injects_supported_var_keywords():
    seen = []
    knowledge_store = object()

    def ingest(kb_id, source_dir, **kwargs):
        seen.append(kwargs)
        return MagicMock(document_count=0, chunk_count=0)

    mgr = IndexJobManager(
        ingest_fn=ingest,
        source_dir_for=lambda kb: "/fake",
        knowledge_store=knowledge_store,
    )
    mgr.submit("kb")
    mgr.run_blocking("kb", lambda: None)
    mgr.shutdown()

    assert seen == [{"on_commit": None, "knowledge_store": knowledge_store}]


# 验证 positional-only 同名参数不会被误当成可关键字注入。
def test_index_job_manager_does_not_keyword_inject_positional_only_parameters():
    seen = []
    knowledge_store = object()

    def ingest(
        kb_id,
        source_dir,
        on_commit=None,
        knowledge_store=None,
        /,
    ):
        seen.append((on_commit, knowledge_store))
        return MagicMock(document_count=0, chunk_count=0)

    mgr = IndexJobManager(
        ingest_fn=ingest,
        source_dir_for=lambda kb: "/fake",
        knowledge_store=knowledge_store,
    )
    mgr.submit("kb")
    mgr.run_blocking("kb", lambda: None)
    mgr.shutdown()

    assert seen == [(None, None)]


def test_post_commit_mirror_failure_never_rolls_back_committed_index():
    from cogdoc.api.persistence import InMemoryJobStore

    store = InMemoryJobStore()
    mirrored = []

    def mirror(kb_id, result):
        mirrored.append((kb_id, result.generation_id))
        raise RuntimeError("mirror unavailable")

    mgr = IndexJobManager(
        ingest_fn=lambda _kb, _source: MagicMock(
            document_count=1,
            chunk_count=2,
            generation_id="local-generation",
        ),
        source_dir_for=lambda _kb: "/fake",
        job_store=store,
        after_index_commit=mirror,
    )
    job = mgr.submit("kb")
    mgr.run_blocking("kb", lambda: None)
    mgr.shutdown()

    assert mirrored == [("kb", "local-generation")]
    assert store.get(job["job_id"])["status"] == "failed"
    assert store.get(job["job_id"])["error_code"] == "HA_MIRROR_FAILED"


def test_ha_publication_precedes_destructive_post_commit_cleanup():
    order = []
    mgr = IndexJobManager(
        ingest_fn=lambda _kb, _source: MagicMock(
            document_count=0,
            chunk_count=0,
            generation_id="local-generation",
        ),
        source_dir_for=lambda _kb: "/fake",
        after_index_commit=lambda _kb, _result: order.append("ha-published"),
    )

    result = mgr._run_ingest(
        "missing-job-is-tolerated",
        "kb",
        "/fake",
        on_succeeded=lambda: order.append("acl-cleanup"),
    )
    mgr.shutdown()

    assert result is True
    assert order == ["ha-published", "acl-cleanup"]


def test_delete_fences_access_before_mutation_and_keeps_fence_on_ha_failure(
    tmp_path,
):
    from cogdoc.api.persistence import InMemoryJobStore
    from cogdoc.api.resource_access import AccessMode, ResourceAccessStore
    from cogdoc.api.tenancy import Principal, Role
    from cogdoc.service.mutation_journal import MutationJournal

    source = tmp_path / "document.txt"
    source.write_text("private evidence")
    order = []
    access = ResourceAccessStore(tmp_path / "access.db")
    access.set_kb_policy("tenant", "kb", "owner", "workspace")
    access.set_document_policy(
        "tenant", "kb", "document", source.name, policy="inherit"
    )
    viewer = Principal(
        tenant_id="tenant",
        subject_id="viewer",
        role=Role.VIEWER,
        key_fingerprint="test-viewer",
    )
    assert access.authorize_query(viewer, "kb").mode is AccessMode.ALL

    def mirror(_kb_id, _result):
        order.append("ha-failed")
        raise RuntimeError("object store unavailable")

    def begin_retirement():
        order.append("access-fenced")
        access.begin_document_retirement(
            "tenant", "kb", "document-delete:document", ("document",)
        )

    def finish_retirement():
        order.append("retirement-finished")
        access.finish_document_retirement(
            "tenant", "kb", "document-delete:document", ("document",)
        )

    store = InMemoryJobStore()
    manager = IndexJobManager(
        ingest_fn=lambda _kb, _source: MagicMock(
            document_count=0,
            chunk_count=0,
            generation_id="local-without-document",
        ),
        source_dir_for=lambda _kb: str(tmp_path),
        job_store=store,
        journal=MutationJournal(str(tmp_path / "journal")),
        after_index_commit=mirror,
    )
    job = manager.submit_delete_doc(
        "kb",
        str(source),
        on_succeeded=finish_retirement,
        on_retiring=begin_retirement,
    )
    manager.run_blocking("kb", lambda: None)
    manager.shutdown()

    assert order == ["access-fenced", "ha-failed"]
    assert store.get(job["job_id"])["error_code"] == "HA_MIRROR_FAILED"
    assert not source.exists()
    assert access.authorize_query(viewer, "kb").mode is AccessMode.DENY
    assert access.retiring_document_ids("tenant", "kb", "document-delete:document") == (
        "document",
    )
    access.close()


# 验证 run blocking serializes behind submitted job。
def test_run_blocking_serializes_behind_submitted_job():
    # 同一 KB：先 submit 的 ingest 必须先完成，run_blocking 排在其后执行。
    order = []
    ingest_started = threading.Event()
    proceed = threading.Event()

    # 构造或驱动 慢任务ingest 测试场景。
    def slow_ingest(kb_id, source_dir):
        ingest_started.set()
        proceed.wait()
        order.append("ingest")
        return MagicMock(document_count=1, chunk_count=1)

    store = MagicMock()
    store.get.return_value = {"kb_id": "kb", "status": "pending"}

    mgr = IndexJobManager(
        ingest_fn=slow_ingest,
        source_dir_for=lambda kb: "/fake",
        job_store=store,
    )

    mgr.submit("kb")
    ingest_started.wait(timeout=2)  # ingest 已启动并阻塞在 proceed

    result_holder = []
    t = threading.Thread(
        target=lambda: mgr.run_blocking("kb", lambda: result_holder.append("delete"))
    )
    t.start()

    import time

    time.sleep(0.03)  # 让 run_blocking 把 delete 排进队列
    proceed.set()
    t.join(timeout=2)
    mgr.shutdown()

    assert order == ["ingest"]
    assert result_holder == ["delete"]


# 验证 shutdown stops all executors。
def test_shutdown_stops_all_executors():
    mgr = IndexJobManager(
        ingest_fn=lambda kb, d: MagicMock(document_count=0, chunk_count=0)
    )
    mgr._get_executor("kb-a")
    mgr._get_executor("kb-b")
    assert len(mgr._executors) == 2
    mgr.shutdown()
    assert len(mgr._executors) == 0


# 测试事务化删除知识库索引。


# 验证事务化删除会推进纪元。
def test_delete_transactional_bumps_epoch(tmp_path):
    epochs = EpochStore(path=str(tmp_path / "epochs.json"))
    mock_state = MagicMock()
    mock_state.generation_ids.return_value = []

    with (
        patch("cogdoc.service.ingest_service.shared_epoch_store", return_value=epochs),
        patch("cogdoc.service.ingest_service.KBState", return_value=mock_state),
        patch("cogdoc.service.ingest_service._schedule_kb_purge"),
        patch("cogdoc.service.ingest_service._remove_manifest"),
        patch("cogdoc.service.ingest_service.RetrieverFactory"),
    ):
        delete_kb_index_transactional("kb-del")

    assert epochs.current("kb-del") == 1


# 验证 delete schedules purge for all generations。
def test_delete_schedules_purge_for_all_generations(tmp_path):
    # 物理清理延迟到 grace period：删库只调度，不同步删 Chroma/BM25（保护在途检索）。
    epochs = EpochStore(path=str(tmp_path / "epochs.json"))
    mock_state = MagicMock()
    mock_state.generation_ids.return_value = ["g1", "g2"]
    scheduled = []

    with (
        patch("cogdoc.service.ingest_service.shared_epoch_store", return_value=epochs),
        patch("cogdoc.service.ingest_service.KBState", return_value=mock_state),
        patch(
            "cogdoc.service.ingest_service._schedule_kb_purge",
            side_effect=lambda kb, gids: scheduled.extend(gids),
        ),
        patch("cogdoc.service.ingest_service._remove_manifest"),
        patch("cogdoc.service.ingest_service.RetrieverFactory"),
    ):
        delete_kb_index_transactional("kb-del")

    assert set(scheduled) == {"g1", "g2"}


# 验证事务化删除会失效引擎缓存。
def test_delete_transactional_invalidates_engine_cache(tmp_path):
    epochs = EpochStore(path=str(tmp_path / "epochs.json"))
    mock_state = MagicMock()
    mock_state.generation_ids.return_value = []
    mock_factory = MagicMock()

    with (
        patch("cogdoc.service.ingest_service.shared_epoch_store", return_value=epochs),
        patch("cogdoc.service.ingest_service.KBState", return_value=mock_state),
        patch("cogdoc.service.ingest_service._schedule_kb_purge"),
        patch("cogdoc.service.ingest_service._remove_manifest"),
        patch("cogdoc.service.ingest_service.RetrieverFactory", mock_factory),
    ):
        delete_kb_index_transactional("kb-del")

    mock_factory.invalidate.assert_called_once_with("kb-del")


# 验证 delete does not raise when purge deferred。
def test_delete_does_not_raise_when_purge_deferred(tmp_path):
    # 物理清理改为异步 best-effort：删库本身不再因 Chroma 失败同步抛 KBCleanupError。
    epochs = EpochStore(path=str(tmp_path / "epochs.json"))
    mock_state = MagicMock()
    mock_state.generation_ids.return_value = ["g1"]

    with (
        patch("cogdoc.service.ingest_service.shared_epoch_store", return_value=epochs),
        patch("cogdoc.service.ingest_service.KBState", return_value=mock_state),
        patch("cogdoc.service.ingest_service._schedule_kb_purge"),
        patch("cogdoc.service.ingest_service._remove_manifest"),
        patch("cogdoc.service.ingest_service.RetrieverFactory"),
    ):
        delete_kb_index_transactional("kb-del")  # 不抛错


# 验证 drain purge queue retries and dequeues on success。
def test_drain_purge_queue_retries_and_dequeues_on_success(tmp_path, monkeypatch):
    # 持久 purge 队列：到期条目清理成功才出队，失败保留待下一轮重试。
    from cogdoc.service.purge_queue import PurgeQueue

    q = PurgeQueue(path=str(tmp_path / "pq.json"))
    monkeypatch.setattr("cogdoc.service.ingest_service.shared_purge_queue", lambda: q)
    q.add("kb", "g1", not_before=0)
    q.add("kb", "g2", not_before=0)

    calls = []

    # 清理。
    def purge(kb, gid, _segment_ids=()):
        calls.append(gid)
        if gid == "g1":
            raise RuntimeError("chroma down")  # g1 失败保留

    monkeypatch.setattr(
        "cogdoc.service.ingest_service._purge_generation_external", purge
    )
    done = ingest_service.drain_purge_queue(now=100)

    assert done == 1  # 仅 g2 成功出队
    assert set(calls) == {"g1", "g2"}
    remaining = {i["gen_id"] for i in q.due(now=100)}
    assert remaining == {"g1"}


# 验证 drain purge queue skips not yet due。
def test_drain_purge_queue_skips_not_yet_due(tmp_path, monkeypatch):
    # 未过 grace period 的条目不清理。
    from cogdoc.service.purge_queue import PurgeQueue

    q = PurgeQueue(path=str(tmp_path / "pq.json"))
    monkeypatch.setattr("cogdoc.service.ingest_service.shared_purge_queue", lambda: q)
    q.add("kb", "g1", not_before=1000)
    called = []
    monkeypatch.setattr(
        "cogdoc.service.ingest_service._purge_generation_external",
        lambda kb, gid: called.append(gid),
    )
    assert ingest_service.drain_purge_queue(now=10) == 0
    assert called == []


# 验证事务化删除拒绝在途暂存代。
def test_delete_transactional_rejects_inflight_staging(tmp_path):
    # 纪元自增后，在途暂存代因基准纪元不符被拒。
    state, epochs = _make_state(tmp_path, "kb-del")
    gen_id = state.begin_generation("m", "v")
    state.mark_ready(gen_id, expected_count=1, documents=[])

    with (
        patch("cogdoc.service.ingest_service.shared_epoch_store", return_value=epochs),
        patch("cogdoc.service.ingest_service.KBState", return_value=state),
        patch("cogdoc.service.ingest_service._schedule_kb_purge"),
        patch("cogdoc.service.ingest_service._remove_manifest"),
        patch("cogdoc.service.ingest_service.RetrieverFactory"),
    ):
        delete_kb_index_transactional("kb-del")

    with pytest.raises(StaleGenerationError):
        state.switch_active(gen_id)


# 验证 delete nonempty kb with active generation succeeds。
def test_delete_nonempty_kb_with_active_generation_succeeds(tmp_path):
    # 回归：含 active generation 的正常 KB 删库不得因 remove_generation 拒删 active 而误报失败。
    state, epochs = _make_state(tmp_path, "kb-live")
    gen_id = state.begin_generation("m", "v")
    state.mark_ready(gen_id, expected_count=1, documents=[])
    state.switch_active(gen_id)
    assert state.active() is not None

    removed = []
    with (
        patch("cogdoc.service.ingest_service.shared_epoch_store", return_value=epochs),
        patch("cogdoc.service.ingest_service.KBState", return_value=state),
        patch("cogdoc.service.ingest_service._schedule_kb_purge"),
        patch(
            "cogdoc.service.ingest_service._remove_manifest",
            side_effect=lambda kb: removed.append(kb),
        ),
        patch("cogdoc.service.ingest_service.RetrieverFactory"),
    ):
        delete_kb_index_transactional("kb-live")  # 不应抛 KBCleanupError

    assert removed == ["kb-live"]


# 验证 submit compat path aborts on stale epoch。
def test_submit_compat_path_aborts_on_stale_epoch(tmp_path, monkeypatch):
    # submit() 兼容路径同样受 epoch 守卫：删库 bump 后入队的旧任务必须放弃构建。
    from cogdoc.api.persistence import InMemoryJobStore

    epochs = EpochStore(path=str(tmp_path / "ep.json"))
    monkeypatch.setattr("cogdoc.api.ingest.shared_epoch_store", lambda: epochs)

    store = InMemoryJobStore()
    called = []

    # 构造或驱动 跟踪任务ingest 测试场景。
    def track_ingest(kb_id, d):
        called.append(1)
        return MagicMock(document_count=0, chunk_count=0)

    mgr = IndexJobManager(
        ingest_fn=track_ingest, source_dir_for=lambda kb: str(tmp_path), job_store=store
    )
    gate = threading.Event()
    mgr._get_executor("kb").submit(gate.wait)
    job = mgr.submit("kb")  # 基准纪元为零
    epochs.bump("kb")
    gate.set()
    mgr.run_blocking("kb", lambda: None)
    mgr.shutdown()

    assert called == []
    assert store.get(job["job_id"])["status"] == "failed"


# 验证 run blocking rejects same kb executor thread。
def test_run_blocking_rejects_same_kb_executor_thread():
    # run_blocking 从同 KB executor 线程内调用会自等待死锁，必须运行时拒绝。
    mgr = IndexJobManager(
        ingest_fn=lambda kb, d: MagicMock(document_count=0, chunk_count=0)
    )
    captured = {}
    done = threading.Event()

    # 构造或驱动 重入调用 测试场景。
    def reenter():
        try:
            mgr.run_blocking("kb", lambda: None)
        except Exception as exc:
            captured["err"] = exc
        finally:
            done.set()

    mgr._get_executor("kb").submit(reenter)
    done.wait(timeout=2)
    mgr.shutdown()
    assert isinstance(captured.get("err"), RuntimeError)


# 测试上传和删除文档任务。


# 验证 submit upload writes file before ingest。
def test_submit_upload_writes_file_before_ingest(tmp_path):
    # 文件写入与 ingest 必须在同一 executor command 内，ingest 时文件已落盘。
    seen_files = []

    # 捕获ingest。
    def capture_ingest(kb_id, source_dir):
        import os

        seen_files.extend(os.listdir(source_dir))
        return MagicMock(document_count=1, chunk_count=1)

    source_dir = str(tmp_path / "sources")
    mgr = IndexJobManager(
        ingest_fn=capture_ingest, source_dir_for=lambda kb: source_dir
    )
    job = mgr.submit_upload("kb", source_dir, "a.pdf", b"%PDF content")
    # 阻塞等 executor 完成；通过 run_blocking 排在同 KB 队尾
    mgr.run_blocking("kb", lambda: None)
    mgr.shutdown()

    assert job["job_id"]
    assert "a.pdf" in seen_files


# 验证 submit delete doc missing file job fails。
def test_submit_delete_doc_missing_file_job_fails(tmp_path):
    # 文件不存在时 submit_delete_doc 仍返回 job；executor 执行时检测，job 以 DOCUMENT_NOT_FOUND 失败。
    from cogdoc.api.persistence import InMemoryJobStore

    store = InMemoryJobStore()
    mgr = IndexJobManager(
        ingest_fn=lambda kb, d: MagicMock(document_count=0, chunk_count=0),
        job_store=store,
    )
    job = mgr.submit_delete_doc("kb", str(tmp_path / "nonexistent.pdf"))
    assert job is not None
    # 等 executor 跑完
    mgr.run_blocking("kb", lambda: None)
    mgr.shutdown()
    record = store.get(job["job_id"])
    assert record["status"] == "failed"
    assert record["error_code"] == "DOCUMENT_NOT_FOUND"


# 验证 submit delete doc removes file before ingest。
def test_submit_delete_doc_removes_file_before_ingest(tmp_path):
    source_dir = str(tmp_path / "sources")
    import os

    os.makedirs(source_dir)
    path = os.path.join(source_dir, "a.pdf")
    with open(path, "wb") as f:
        f.write(b"%PDF content")

    seen_files = []

    # 捕获ingest。
    def capture_ingest(kb_id, source_dir):
        seen_files.extend(os.listdir(source_dir))
        return MagicMock(document_count=0, chunk_count=0)

    mgr = IndexJobManager(
        ingest_fn=capture_ingest, source_dir_for=lambda kb: source_dir
    )
    job = mgr.submit_delete_doc("kb", path)
    mgr.run_blocking("kb", lambda: None)
    mgr.shutdown()

    assert job is not None
    assert "a.pdf" not in seen_files
    assert not os.path.exists(path)


def test_upload_revocation_during_build_denies_commit_and_restores_source(tmp_path):
    import os

    from cogdoc.api.persistence import InMemoryJobStore

    source_dir = str(tmp_path / "sources")
    os.makedirs(source_dir)
    destination = os.path.join(source_dir, "a.pdf")
    with open(destination, "wb") as stream:
        stream.write(b"OLD")
    build_started = threading.Event()
    allow_commit = threading.Event()
    authorized = [True]
    switched_generations = []

    def delayed_ingest(kb_id, source_dir, *, on_commit=None):
        assert open(destination, "rb").read() == b"NEW"
        build_started.set()
        assert allow_commit.wait(timeout=5)
        assert on_commit is not None
        on_commit("generation-new")
        # Models KBState.switch_active: it must be unreachable after revocation.
        switched_generations.append("generation-new")
        return MagicMock(document_count=1, chunk_count=1)

    def authorization_guard() -> None:
        if not authorized[0]:
            raise PermissionError("membership was revoked")

    store = InMemoryJobStore()
    mgr = IndexJobManager(
        ingest_fn=delayed_ingest,
        source_dir_for=lambda kb: source_dir,
        job_store=store,
    )
    job = mgr.submit_upload(
        "guard-upload-kb",
        source_dir,
        "a.pdf",
        b"NEW",
        authorization_guard=authorization_guard,
    )
    assert build_started.wait(timeout=5)
    authorized[0] = False
    allow_commit.set()
    mgr.run_blocking("guard-upload-kb", lambda: None)
    mgr.shutdown()

    assert switched_generations == []
    assert open(destination, "rb").read() == b"OLD"
    assert store.get(job["job_id"])["status"] == "failed"


def test_document_delete_revocation_during_build_denies_commit_and_restores_source(
    tmp_path,
):
    import os

    from cogdoc.api.persistence import InMemoryJobStore

    source_dir = str(tmp_path / "sources")
    os.makedirs(source_dir)
    path = os.path.join(source_dir, "a.pdf")
    with open(path, "wb") as stream:
        stream.write(b"DOC")
    build_started = threading.Event()
    allow_commit = threading.Event()
    authorized = [True]
    switched_generations = []
    acl_cleared = []

    def delayed_ingest(kb_id, source_dir, *, on_commit=None):
        assert not os.path.exists(path)
        build_started.set()
        assert allow_commit.wait(timeout=5)
        assert on_commit is not None
        on_commit("generation-without-doc")
        switched_generations.append("generation-without-doc")
        return MagicMock(document_count=0, chunk_count=0)

    def authorization_guard() -> None:
        if not authorized[0]:
            raise PermissionError("membership was revoked")

    store = InMemoryJobStore()
    mgr = IndexJobManager(
        ingest_fn=delayed_ingest,
        source_dir_for=lambda kb: source_dir,
        job_store=store,
    )
    job = mgr.submit_delete_doc(
        "guard-delete-kb",
        path,
        on_succeeded=lambda: acl_cleared.append(True),
        authorization_guard=authorization_guard,
    )
    assert build_started.wait(timeout=5)
    authorized[0] = False
    allow_commit.set()
    mgr.run_blocking("guard-delete-kb", lambda: None)
    mgr.shutdown()

    assert switched_generations == []
    assert acl_cleared == []
    assert open(path, "rb").read() == b"DOC"
    assert store.get(job["job_id"])["status"] == "failed"


# 构建失败回滚源文件。


# _boom_ingest：处理对应功能。
def _boom_ingest(kb_id, source_dir):
    raise ValueError("build failed")


# 验证 upload build failure restores old file。
def test_upload_build_failure_restores_old_file(tmp_path):
    # 覆盖上传：构建失败时旧文件内容必须恢复，备份清理。
    import os
    from cogdoc.api.persistence import InMemoryJobStore

    source_dir = str(tmp_path / "sources")
    os.makedirs(source_dir)
    dest = os.path.join(source_dir, "a.pdf")
    with open(dest, "wb") as f:
        f.write(b"OLD")

    store = InMemoryJobStore()
    mgr = IndexJobManager(
        ingest_fn=_boom_ingest, source_dir_for=lambda kb: source_dir, job_store=store
    )
    job = mgr.submit_upload("kb", source_dir, "a.pdf", b"NEW")
    mgr.run_blocking("kb", lambda: None)
    mgr.shutdown()

    with open(dest, "rb") as f:
        assert f.read() == b"OLD"
    assert not os.path.exists(dest + ".cogdoc-bak")
    assert store.get(job["job_id"])["status"] == "failed"


# 验证 upload build failure removes new file。
def test_upload_build_failure_removes_new_file(tmp_path):
    # 新增上传：构建失败时新文件必须删除，回到上传前状态。
    import os
    from cogdoc.api.persistence import InMemoryJobStore

    source_dir = str(tmp_path / "sources")
    store = InMemoryJobStore()
    mgr = IndexJobManager(
        ingest_fn=_boom_ingest, source_dir_for=lambda kb: source_dir, job_store=store
    )
    job = mgr.submit_upload("kb", source_dir, "a.pdf", b"NEW")
    mgr.run_blocking("kb", lambda: None)
    mgr.shutdown()

    assert not os.path.exists(os.path.join(source_dir, "a.pdf"))
    assert store.get(job["job_id"])["status"] == "failed"


# 验证 delete doc build failure restores file。
def test_delete_doc_build_failure_restores_file(tmp_path):
    # 删文档：构建失败时被删文件必须从隔离区恢复。
    import os
    from cogdoc.api.persistence import InMemoryJobStore

    source_dir = str(tmp_path / "sources")
    os.makedirs(source_dir)
    path = os.path.join(source_dir, "a.pdf")
    with open(path, "wb") as f:
        f.write(b"DOC")

    store = InMemoryJobStore()
    mgr = IndexJobManager(
        ingest_fn=_boom_ingest, source_dir_for=lambda kb: source_dir, job_store=store
    )
    job = mgr.submit_delete_doc("kb", path)
    mgr.run_blocking("kb", lambda: None)
    mgr.shutdown()

    assert os.path.exists(path)
    with open(path, "rb") as f:
        assert f.read() == b"DOC"
    assert not os.path.exists(path + ".cogdoc-bak")
    assert store.get(job["job_id"])["status"] == "failed"


# 删除后重建的纪元守卫。


# 验证 upload stale epoch aborts。
def test_upload_stale_epoch_aborts(tmp_path, monkeypatch):
    # 提交后、执行前 epoch 被 bump（模拟删库）：陈旧上传放弃，不调用 ingest、不写文件。
    import os
    from cogdoc.api.persistence import InMemoryJobStore

    epochs = EpochStore(path=str(tmp_path / "ep.json"))
    monkeypatch.setattr("cogdoc.api.ingest.shared_epoch_store", lambda: epochs)

    source_dir = str(tmp_path / "sources")
    store = InMemoryJobStore()
    called = []

    # 构造或驱动 跟踪任务ingest 测试场景。
    def track_ingest(kb_id, d):
        called.append(1)
        return MagicMock(document_count=0, chunk_count=0)

    mgr = IndexJobManager(
        ingest_fn=track_ingest, source_dir_for=lambda kb: source_dir, job_store=store
    )
    gate = threading.Event()
    # 占住单线程 executor，保证 upload 入队后、执行前能 bump epoch
    mgr._get_executor("kb").submit(gate.wait)
    job = mgr.submit_upload("kb", source_dir, "a.pdf", b"NEW")  # 基准纪元为零
    epochs.bump("kb")  # 模拟删库：epoch 0→1
    gate.set()
    mgr.run_blocking("kb", lambda: None)
    mgr.shutdown()

    assert called == []
    assert not os.path.exists(os.path.join(source_dir, "a.pdf"))
    rec = store.get(job["job_id"])
    assert rec["status"] == "failed"
    assert "已被删除或重建" in rec["message"]


# 关闭状态和上限保护。


# 验证 get executor raises after shutdown。
def test_get_executor_raises_after_shutdown():
    mgr = IndexJobManager(
        ingest_fn=lambda kb, d: MagicMock(document_count=0, chunk_count=0)
    )
    mgr.shutdown()
    with pytest.raises(RuntimeError, match="closed"):
        mgr._get_executor("kb")


# 验证 get executor raises at max limit。
def test_get_executor_raises_at_max_limit():
    from cogdoc.api.ingest import _MAX_KB_EXECUTORS

    mgr = IndexJobManager(
        ingest_fn=lambda kb, d: MagicMock(document_count=0, chunk_count=0)
    )
    for i in range(_MAX_KB_EXECUTORS):
        mgr._get_executor(f"kb-{i}")
    with pytest.raises(RuntimeError, match="上限"):
        mgr._get_executor("kb-overflow")
    mgr.shutdown()


# 验证 release executor frees slot。
def test_release_executor_frees_slot():
    # 删库后释放 executor 槽位，同 kb_id 可再次创建新 executor。
    from cogdoc.api.ingest import _MAX_KB_EXECUTORS

    mgr = IndexJobManager(
        ingest_fn=lambda kb, d: MagicMock(document_count=0, chunk_count=0)
    )
    for i in range(_MAX_KB_EXECUTORS):
        mgr._get_executor(f"kb-{i}")
    # 上限已满；释放一个槽位后应能创建新 executor。
    mgr.release_executor("kb-0")
    ex_new = mgr._get_executor("kb-new")
    assert ex_new is not None
    mgr.shutdown()


# 验证删库任务中请求退役时，必须先排空同 KB 的旧队列。
def test_release_executor_defers_until_queued_delete_and_create_finish():
    mgr = IndexJobManager(
        ingest_fn=lambda kb, d: MagicMock(document_count=0, chunk_count=0)
    )
    first_running = threading.Event()
    allow_first = threading.Event()
    second_queued = threading.Event()
    second_running = threading.Event()
    allow_second = threading.Event()
    create_queued = threading.Event()
    create_ran = threading.Event()
    order = []

    def first_delete():
        first_running.set()
        assert allow_first.wait(2)
        order.append("delete-1")
        mgr.release_executor("kb")

    def second_delete():
        second_running.set()
        assert allow_second.wait(2)
        order.append("delete-2")
        mgr.release_executor("kb")

    def create_again():
        order.append("create")
        create_ran.set()

    original_submit = mgr._submit_tracked

    def tracked_submit(executor, kb_id, fn, *args):
        future = original_submit(executor, kb_id, fn, *args)
        if fn is second_delete:
            second_queued.set()
        elif fn is create_again:
            create_queued.set()
        return future

    mgr._submit_tracked = tracked_submit
    first_thread = threading.Thread(target=lambda: mgr.run_blocking("kb", first_delete))
    second_thread = threading.Thread(
        target=lambda: mgr.run_blocking("kb", second_delete)
    )
    first_thread.start()
    assert first_running.wait(2)
    second_thread.start()
    assert second_queued.wait(2)

    allow_first.set()
    first_thread.join(timeout=2)
    assert not first_thread.is_alive()
    assert second_running.wait(2)

    create_thread = threading.Thread(
        target=lambda: mgr.run_blocking("kb", create_again)
    )
    create_thread.start()
    assert create_queued.wait(2)
    assert create_ran.wait(0.05) is False

    allow_second.set()
    second_thread.join(timeout=2)
    create_thread.join(timeout=2)
    assert not second_thread.is_alive()
    assert not create_thread.is_alive()
    assert order == ["delete-1", "delete-2", "create"]
    assert "kb" not in mgr._executors
    mgr.shutdown()


# 验证 upload aborted when kb deleted。
def test_upload_aborted_when_kb_deleted(tmp_path):
    # kb_exists 在 executor command 内再次检查：标志在入队前已置 False 模拟删库完成后的状态。
    from cogdoc.api.persistence import InMemoryJobStore

    store = InMemoryJobStore()
    kb_deleted = [True]  # 直接置 True，保证 _run_with_write 检查时看到 KB 已删
    mgr = IndexJobManager(
        ingest_fn=lambda kb, d: MagicMock(document_count=1, chunk_count=1),
        source_dir_for=lambda kb: str(tmp_path / kb / "sources"),
        job_store=store,
        kb_exists=lambda kb: not kb_deleted[0],
    )
    source_dir = str(tmp_path / "kb" / "sources")
    job = mgr.submit_upload("kb", source_dir, "a.pdf", b"%PDF content")
    mgr.run_blocking("kb", lambda: None)
    mgr.shutdown()
    record = store.get(job["job_id"])
    assert record["status"] == "failed"
    assert "已被删除" in record["message"]


# 生命周期和删除标记读写门控。


# 验证 get engine returns empty when deleting。
def test_get_engine_returns_empty_when_deleting(monkeypatch):
    # 删库进行中：get_engine 短路返回空引擎，不构造读取正在拆除的代。
    from cogdoc.graph.subgraphs.qa import RetrieverFactory
    from cogdoc.tools.retriever.hybrid import HybridRetriever
    from cogdoc.service.kb_lifecycle import shared_lifecycle_store, LIFECYCLE_DELETING

    shared_lifecycle_store().set("kb-del", LIFECYCLE_DELETING)
    called = []
    monkeypatch.setattr(
        RetrieverFactory,
        "_build_engine",
        classmethod(lambda cls, k, g: called.append(1)),
    )
    engine = RetrieverFactory.get_engine("kb-del")
    assert called == []
    assert isinstance(engine, HybridRetriever)


# 验证 mutation rejected when deleting。
def test_mutation_rejected_when_deleting(tmp_path):
    # 删库进行中：新上传任务被 _stale 拦下并标记失败，不写文件不构建。
    from cogdoc.api.persistence import InMemoryJobStore
    from cogdoc.service.kb_lifecycle import shared_lifecycle_store, LIFECYCLE_DELETING
    import os

    shared_lifecycle_store().set("kb", LIFECYCLE_DELETING)
    store = InMemoryJobStore()
    called = []
    mgr = IndexJobManager(
        ingest_fn=lambda k, d: (
            called.append(1) or MagicMock(document_count=0, chunk_count=0)
        ),
        source_dir_for=lambda kb: str(tmp_path),
        job_store=store,
    )
    job = mgr.submit_upload("kb", str(tmp_path), "a.pdf", b"%PDF")
    mgr.run_blocking("kb", lambda: None)
    mgr.shutdown()

    assert called == []
    assert not os.path.exists(os.path.join(str(tmp_path), "a.pdf"))
    assert store.get(job["job_id"])["status"] == "failed"


# 验证 create resets lifecycle to active。
def test_create_resets_lifecycle_to_active(tmp_path):
    # 重建同名 KB：清除 deleted tombstone，恢复 active 可读写。
    from cogdoc.api.ingest import KnowledgeBaseRegistry
    from cogdoc.service.kb_lifecycle import (
        shared_lifecycle_store,
        LIFECYCLE_DELETED,
        LIFECYCLE_ACTIVE,
    )

    shared_lifecycle_store().set("kb", LIFECYCLE_DELETED)
    reg = KnowledgeBaseRegistry(
        registry_path=str(tmp_path / "r.json"),
        source_dir_for=lambda k: str(tmp_path / k / "src"),
    )
    reg.create("kb")
    assert shared_lifecycle_store().status("kb") == LIFECYCLE_ACTIVE


# 验证事务化删除会设置删除中状态。
def test_delete_transactional_sets_deleting(tmp_path):
    # delete_kb_index_transactional 一进入即落 deleting 门控读路径。
    from cogdoc.service.kb_lifecycle import shared_lifecycle_store, LIFECYCLE_DELETING

    epochs = EpochStore(path=str(tmp_path / "ep.json"))
    mock_state = MagicMock()
    mock_state.generation_ids.return_value = []
    with (
        patch("cogdoc.service.ingest_service.shared_epoch_store", return_value=epochs),
        patch("cogdoc.service.ingest_service.KBState", return_value=mock_state),
        patch("cogdoc.service.ingest_service._schedule_kb_purge"),
        patch("cogdoc.service.ingest_service._remove_manifest"),
        patch("cogdoc.service.ingest_service.RetrieverFactory"),
    ):
        delete_kb_index_transactional("kb-del")
    assert shared_lifecycle_store().status("kb-del") == LIFECYCLE_DELETING


# 日志在正常和失败路径都清空。


# 验证 successful upload clears journal。
def test_successful_upload_clears_journal(tmp_path):
    from cogdoc.service.mutation_journal import shared_mutation_journal

    source_dir = str(tmp_path / "src")
    mgr = IndexJobManager(
        ingest_fn=lambda k, d: MagicMock(document_count=1, chunk_count=1),
        source_dir_for=lambda kb: source_dir,
    )
    mgr.submit_upload("kb", source_dir, "a.pdf", b"%PDF")
    mgr.run_blocking("kb", lambda: None)
    mgr.shutdown()
    assert shared_mutation_journal().recover_all() == []


# 验证 failed upload clears journal。
def test_failed_upload_clears_journal(tmp_path):
    from cogdoc.service.mutation_journal import shared_mutation_journal

    source_dir = str(tmp_path / "src")
    mgr = IndexJobManager(ingest_fn=_boom_ingest, source_dir_for=lambda kb: source_dir)
    mgr.submit_upload("kb", source_dir, "a.pdf", b"%PDF")
    mgr.run_blocking("kb", lambda: None)
    mgr.shutdown()
    assert shared_mutation_journal().recover_all() == []
