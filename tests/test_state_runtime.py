import asyncio
import gc
import weakref
from contextlib import contextmanager
from functools import partial
from types import SimpleNamespace

import pytest
from cogdoc.api.app import create_app
from cogdoc.api.derived_knowledge_store import DerivedKnowledgeStore
from cogdoc.api.feedback_analysis_store import FeedbackAnalysisStore
from cogdoc.api.feedback_store import FeedbackStore
from cogdoc.api.ingest import IndexJobManager
from cogdoc.api.retrieval_feedback_store import RetrievalFeedbackStore
from cogdoc.api.retrieval_eval_draft_store import (
    RetrievalEvalDraftStore,
    SqliteRetrievalEvalDraftStore,
)
from cogdoc.api.session_store import SessionStore
from cogdoc.config.settings import Settings
from cogdoc.state_runtime import StateRuntime


# 验证运行时保留注入存储的对象身份。
def test_state_runtime_preserves_injected_store_identity(tmp_path):
    feedback_store = FeedbackStore(
        feedback_path=str(tmp_path / "feedback.jsonl"),
        bad_cases_path=str(tmp_path / "bad_cases.jsonl"),
    )
    feedback_analysis_store = FeedbackAnalysisStore(
        path=str(tmp_path / "feedback_analysis.jsonl")
    )
    knowledge_store = DerivedKnowledgeStore(path=str(tmp_path / "knowledge.jsonl"))
    retrieval_feedback_store = RetrievalFeedbackStore(
        path=str(tmp_path / "retrieval_feedback.jsonl")
    )
    retrieval_eval_draft_store = RetrievalEvalDraftStore(
        path=str(tmp_path / "retrieval_eval_drafts.jsonl")
    )

    runtime = StateRuntime.from_settings(
        feedback_store=feedback_store,
        feedback_analysis_store=feedback_analysis_store,
        knowledge_store=knowledge_store,
        retrieval_feedback_store=retrieval_feedback_store,
        retrieval_eval_draft_store=retrieval_eval_draft_store,
    )

    assert runtime.feedback_store is feedback_store
    assert runtime.feedback_analysis_store is feedback_analysis_store
    assert runtime.knowledge_store is knowledge_store
    assert runtime.retrieval_feedback_store is retrieval_feedback_store
    assert runtime.retrieval_eval_draft_store is retrieval_eval_draft_store
    assert runtime.derived_knowledge_retriever.store is knowledge_store
    assert runtime.derived_knowledge_retriever is runtime.derived_knowledge_retriever


# 验证 runtime 幂等关闭所有长连接存储，且关闭后不再惰性打开索引。
def test_state_runtime_close_is_idempotent_and_closes_unique_stores():
    class Store:
        def __init__(self):
            self.close_count = 0

        def close(self):
            self.close_count += 1

    shared = Store()
    analysis = Store()
    knowledge = Store()
    drafts = Store()
    research = Store()
    runtime = StateRuntime(
        feedback_store=shared,
        feedback_analysis_store=analysis,
        knowledge_store=knowledge,
        retrieval_feedback_store=shared,
        derived_knowledge_index_persist_directory="/tmp/cogdoc-test-index",
        derived_knowledge_index_state_directory="/tmp/cogdoc-test-index-state",
        retrieval_eval_draft_store=drafts,
        research_job_store=research,
    )

    runtime.close()
    runtime.close()

    assert runtime.closed is True
    assert shared.close_count == 1
    assert analysis.close_count == 1
    assert knowledge.close_count == 1
    assert drafts.close_count == 1
    assert research.close_count == 1
    with pytest.raises(RuntimeError, match="closed"):
        _ = runtime.derived_knowledge_index
    with pytest.raises(RuntimeError, match="closed"):
        _ = runtime.derived_knowledge_retriever


# 验证 direct-call singleton 随显式 Settings 边界切换，不继续写入旧路径。
def test_default_state_runtime_rebuilds_after_settings_change(tmp_path, monkeypatch):
    import cogdoc.state_runtime as runtime_module

    settings = Settings(
        _env_file=None,
        cogdoc_data_dir=str(tmp_path / "a"),
        cogdoc_state_backend="jsonl",
        cogdoc_feedback_store="jsonl",
    )
    monkeypatch.setattr(runtime_module, "get_settings", lambda: settings)
    runtime_module.reset_default_state_runtime()
    try:
        runtime_a = runtime_module.default_state_runtime()
        settings = Settings(
            _env_file=None,
            cogdoc_data_dir=str(tmp_path / "b"),
            cogdoc_state_backend="jsonl",
            cogdoc_feedback_store="jsonl",
        )
        runtime_b = runtime_module.default_state_runtime()

        assert runtime_b is not runtime_a
        assert runtime_a.closed is True
        assert runtime_b.knowledge_store._path == settings.derived_knowledge_path
    finally:
        runtime_module.reset_default_state_runtime()


# 验证 API 只在显式获得 runtime 所有权时于 lifespan 尾部关闭它。
def test_create_app_runtime_shutdown_ownership(tmp_path, monkeypatch):
    import cogdoc.api.app as app_module

    monkeypatch.setattr(app_module, "configure_logging", lambda: None)
    monkeypatch.setattr(app_module, "acquire_single_instance_lock", lambda: object())
    monkeypatch.setattr(app_module, "release_single_instance_lock", lambda lock: None)
    monkeypatch.setattr(app_module, "cancel_all_timers", lambda: True)
    monkeypatch.setattr(app_module, "drain_purge_queue", lambda: None)
    monkeypatch.setattr(
        app_module,
        "shared_mutation_journal",
        lambda: SimpleNamespace(recover_all=lambda: []),
    )

    class Sweeper:
        def __init__(self, **kwargs):
            pass

        def start(self):
            pass

        def stop(self):
            pass

    monkeypatch.setattr(app_module, "BackgroundSweeper", Sweeper)

    def runtime_for(name):
        return StateRuntime.from_settings(
            Settings(
                _env_file=None,
                cogdoc_data_dir=str(tmp_path / name),
                cogdoc_state_backend="jsonl",
                cogdoc_feedback_store="jsonl",
            )
        )

    async def cycle(app):
        async with app.router.lifespan_context(app):
            assert app.state.lifecycle_status == "ready"

    caller_owned = runtime_for("caller")
    caller_app = create_app(
        state_runtime=caller_owned,
        session_store=SessionStore(),
    )
    asyncio.run(cycle(caller_app))
    assert caller_owned.closed is False
    assert caller_app.state.single_instance_lock_handle is None
    caller_owned.close()

    app_owned = runtime_for("app")
    owned_app = create_app(
        state_runtime=app_owned,
        close_state_runtime_on_shutdown=True,
        session_store=SessionStore(),
    )
    asyncio.run(cycle(owned_app))
    assert app_owned.closed is True
    assert owned_app.state.single_instance_lock_handle is None

    internally_owned = runtime_for("internal")
    monkeypatch.setattr(
        app_module.StateRuntime,
        "from_settings",
        lambda *args, **kwargs: internally_owned,
    )
    internal_app = create_app(session_store=SessionStore())
    asyncio.run(cycle(internal_app))
    assert internally_owned.closed is True
    with pytest.raises(RuntimeError, match="StateRuntime is closed"):
        asyncio.run(cycle(internal_app))


@pytest.mark.parametrize("undrained_component", ["research", "planning"])
def test_create_app_retains_process_lock_while_background_work_is_undrained(
    tmp_path, monkeypatch, undrained_component
):
    import cogdoc.api.app as app_module
    from cogdoc.service.process_lock import acquire_single_instance_lock

    lock_path = str(tmp_path / "deferred.lock")
    monkeypatch.setattr(app_module, "configure_logging", lambda: None)
    monkeypatch.setattr(
        app_module,
        "acquire_single_instance_lock",
        lambda: acquire_single_instance_lock(lock_path),
    )
    monkeypatch.setattr(app_module, "cancel_all_timers", lambda: True)
    monkeypatch.setattr(app_module, "drain_purge_queue", lambda: None)
    monkeypatch.setattr(
        app_module,
        "shared_mutation_journal",
        lambda: SimpleNamespace(recover_all=lambda: []),
    )

    class Sweeper:
        def __init__(self, **kwargs):
            pass

        def start(self):
            pass

        def stop(self):
            pass

    class UndrainedResearchManager:
        def bind_observer(self, _observer):
            pass

        def reconcile_orphans(self):
            return 0

        def shutdown(self, *, wait=True):
            assert wait is False
            return undrained_component != "research"

    class UndrainedPlanningRuntime:
        def shutdown(self, *, wait=False, cancel_futures=True):
            assert wait is False
            assert cancel_futures is True
            return False

    monkeypatch.setattr(app_module, "BackgroundSweeper", Sweeper)
    runtime = StateRuntime.from_settings(
        Settings(
            _env_file=None,
            cogdoc_data_dir=str(tmp_path / "runtime"),
            cogdoc_state_backend="jsonl",
            cogdoc_feedback_store="jsonl",
        )
    )
    app = create_app(
        state_runtime=runtime,
        close_state_runtime_on_shutdown=True,
        research_execution_manager=UndrainedResearchManager(),
        session_store=SessionStore(),
    )
    if undrained_component == "planning":
        app.state.research_planning_executor = UndrainedPlanningRuntime()

    async def cycle():
        async with app.router.lifespan_context(app):
            assert app.state.lifecycle_status == "ready"

    contender = None
    retained_ref = None
    try:
        asyncio.run(cycle())
        retained_ref = weakref.ref(app.state.single_instance_lock_handle)
        gc.collect()

        assert retained_ref() is not None
        assert retained_ref().closed is False
        assert runtime.closed is False
        contender = acquire_single_instance_lock(lock_path)
        assert contender is None
    finally:
        if contender is not None:
            app_module.release_single_instance_lock(contender)
        retained = retained_ref() if retained_ref is not None else None
        app_module._release_retained_single_instance_lock(retained)
        app.state.single_instance_lock_handle = None
        runtime.close()


# 验证显式 Settings 会完整决定 JSONL runtime 路径，不会回退全局缓存配置。
def test_state_runtime_uses_explicit_settings_for_all_jsonl_paths(tmp_path):
    settings = Settings(
        _env_file=None,
        cogdoc_data_dir=str(tmp_path),
        cogdoc_state_backend="jsonl",
        cogdoc_feedback_store="jsonl",
    )

    runtime = StateRuntime.from_settings(settings)

    assert runtime.feedback_store._feedback_path == settings.feedback_log_path
    assert runtime.feedback_store._bad_cases_path == settings.bad_cases_path
    assert runtime.feedback_analysis_store._path == settings.feedback_analysis_path
    assert runtime.knowledge_store._path == settings.derived_knowledge_path
    assert runtime.retrieval_feedback_store._path == settings.retrieval_feedback_path
    assert (
        runtime.retrieval_eval_draft_store._path == settings.retrieval_eval_drafts_path
    )
    assert (
        runtime.derived_knowledge_index_persist_directory == settings.chroma_persist_dir
    )
    assert runtime.derived_knowledge_index_state_directory == str(
        settings.data_dir / "knowledge" / "derived_index_state"
    )


def test_state_runtime_uses_unified_sqlite_for_retrieval_eval_drafts(tmp_path):
    settings = Settings(
        _env_file=None,
        cogdoc_data_dir=str(tmp_path),
        cogdoc_state_backend="sqlite",
    )
    runtime = StateRuntime.from_settings(settings)
    try:
        assert isinstance(
            runtime.retrieval_eval_draft_store, SqliteRetrievalEvalDraftStore
        )
        database = runtime.retrieval_eval_draft_store._conn.execute(
            "PRAGMA database_list"
        ).fetchone()[2]
        assert database == settings.state_db_path
    finally:
        runtime.close()


# 验证同一进程两个显式 data_dir 即使使用相同 kb_id，也不会共享派生知识 collection。
def test_state_runtime_isolates_derived_indexes_by_explicit_data_dir(
    tmp_path,
    monkeypatch,
):
    from cogdoc.tools.retriever import derived_knowledge as derived_module

    backends = {}

    class FakeCollection:
        pass

    class FakePersistentClient:
        def __init__(self, path):
            self.path = path
            self.collections = backends.setdefault(path, {})

        def get_or_create_collection(self, name, metadata=None):
            return self.collections.setdefault(name, FakeCollection())

    monkeypatch.setattr("chromadb.PersistentClient", FakePersistentClient)
    monkeypatch.setattr(
        derived_module,
        "_embedder",
        lambda: SimpleNamespace(EMBEDDING_CONTRACT_VERSION="test-v1"),
    )
    settings_a = Settings(
        _env_file=None,
        cogdoc_data_dir=str(tmp_path / "app-a"),
        cogdoc_state_backend="jsonl",
        cogdoc_feedback_store="jsonl",
    )
    settings_b = Settings(
        _env_file=None,
        cogdoc_data_dir=str(tmp_path / "app-b"),
        cogdoc_state_backend="jsonl",
        cogdoc_feedback_store="jsonl",
    )
    runtime_a = StateRuntime.from_settings(settings_a)
    runtime_b = StateRuntime.from_settings(settings_b)

    index_a = runtime_a.derived_knowledge_index
    index_b = runtime_b.derived_knowledge_index
    collection_a = index_a._collection("same-kb")
    collection_b = index_b._collection("same-kb")
    collection_a.owner = "app-a"

    assert index_a.persist_directory == settings_a.chroma_persist_dir
    assert index_b.persist_directory == settings_b.chroma_persist_dir
    assert index_a.state_directory != index_b.state_directory
    assert collection_a is not collection_b
    assert not hasattr(collection_b, "owner")
    assert runtime_a.derived_knowledge_retriever._index_or_none() is index_a
    assert runtime_b.derived_knowledge_retriever._index_or_none() is index_b


# 验证 API 的管理接口、聊天和检索共享同一个 runtime，而不是各建一套 store。
def test_create_app_binds_one_state_runtime_to_all_entry_points(tmp_path):
    runtime = StateRuntime.from_settings(
        Settings(
            _env_file=None,
            cogdoc_data_dir=str(tmp_path),
            cogdoc_state_backend="jsonl",
            cogdoc_feedback_store="jsonl",
        )
    )
    app = create_app(state_runtime=runtime, session_store=SessionStore())
    try:
        assert app.state.state_runtime is runtime
        assert app.state.feedback_store is runtime.feedback_store
        assert app.state.feedback_analysis_store is runtime.feedback_analysis_store
        assert app.state.knowledge_store is runtime.knowledge_store
        assert app.state.retrieval_feedback_store is runtime.retrieval_feedback_store
        assert (
            app.state.retrieval_eval_draft_store is runtime.retrieval_eval_draft_store
        )
        assert app.state.index_jobs._knowledge_store is runtime.knowledge_store
        assert isinstance(app.state.chat_runner, partial)
        assert app.state.chat_runner.keywords["state_runtime"] is runtime
        assert isinstance(app.state.chat_stream_runner, partial)
        assert app.state.chat_stream_runner.keywords["state_runtime"] is runtime
        assert app.state.derived_knowledge_index_refresher.__self__ is runtime
        assert app.state.derived_knowledge_index_statuser.__self__ is runtime
    finally:
        app.state.offload_executor.shutdown(wait=True)
        app.state.index_jobs.shutdown(wait=True)


# 验证注入的任务管理器会绑定 app runtime store，且拒绝不同 store 的 split-brain 配置。
def test_create_app_binds_and_validates_injected_index_manager(tmp_path):
    runtime = StateRuntime.from_settings(
        Settings(
            _env_file=None,
            cogdoc_data_dir=str(tmp_path / "runtime"),
            cogdoc_state_backend="jsonl",
            cogdoc_feedback_store="jsonl",
        )
    )
    manager = IndexJobManager(ingest_fn=lambda kb_id, source_dir: SimpleNamespace())
    app = create_app(
        state_runtime=runtime,
        index_jobs=manager,
        session_store=SessionStore(),
    )
    try:
        assert app.state.index_jobs is manager
        assert manager._knowledge_store is runtime.knowledge_store
    finally:
        app.state.offload_executor.shutdown(wait=True)
        manager.shutdown(wait=True)

    other_store = DerivedKnowledgeStore(path=str(tmp_path / "other.jsonl"))
    mismatched = IndexJobManager(
        ingest_fn=lambda kb_id, source_dir: SimpleNamespace(),
        knowledge_store=other_store,
    )
    try:
        with pytest.raises(ValueError, match="does not match StateRuntime"):
            create_app(
                state_runtime=runtime,
                index_jobs=mismatched,
                session_store=SessionStore(),
            )
    finally:
        mismatched.shutdown(wait=True)


# 验证整套 runtime 与单 store 覆盖不能混用，避免身份语义不明确。
def test_create_app_rejects_mixed_runtime_and_store_overrides(tmp_path):
    runtime = StateRuntime.from_settings(
        Settings(
            _env_file=None,
            cogdoc_data_dir=str(tmp_path),
            cogdoc_state_backend="jsonl",
            cogdoc_feedback_store="jsonl",
        )
    )

    with pytest.raises(ValueError, match="state_runtime"):
        create_app(state_runtime=runtime, feedback_store=runtime.feedback_store)
    with pytest.raises(ValueError, match="state_runtime"):
        create_app(
            state_runtime=runtime,
            retrieval_eval_draft_store=runtime.retrieval_eval_draft_store,
        )


# 验证 CLI 删库镜像 API 清理四类 runtime 状态。
def test_cli_delete_kb_clears_all_runtime_stores(monkeypatch):
    from cogdoc import cli as cli_module

    cleared = []

    class Store:
        def __init__(self, name):
            self.name = name

        def clear_kb(self, kb_id):
            cleared.append((self.name, kb_id))

    class Registry:
        def delete(self, kb_id):
            cleared.append(("registry", kb_id))

    class Sessions:
        def clear_kb(self, kb_id):
            cleared.append(("sessions", kb_id))

    @contextmanager
    def lock(kb_id):
        yield

    console = cli_module.Console.__new__(cli_module.Console)
    console.knowledge_store = Store("knowledge")
    console.feedback_store = Store("feedback")
    console.feedback_analysis_store = Store("analysis")
    console.retrieval_feedback_store = Store("retrieval")
    console.retrieval_eval_draft_store = Store("eval-drafts")
    console.registry = Registry()
    console.sessions = Sessions()
    monkeypatch.setattr(cli_module, "kb_write_lock", lock)
    monkeypatch.setattr(cli_module, "delete_kb_index_transactional", lambda kb: None)
    monkeypatch.setattr(cli_module, "mark_kb_deleted", lambda kb: None)

    console._delete_kb("kb")

    assert cleared == [
        ("knowledge", "kb"),
        ("feedback", "kb"),
        ("analysis", "kb"),
        ("retrieval", "kb"),
        ("eval-drafts", "kb"),
        ("sessions", "kb"),
        ("registry", "kb"),
    ]


# 验证会话清理失败时保留 registry，使删库可重试。
def test_cli_delete_kb_keeps_registry_when_session_cleanup_fails(monkeypatch):
    from cogdoc import cli as cli_module

    deleted = []

    class Store:
        def clear_kb(self, kb_id):
            return None

    class Registry:
        def delete(self, kb_id):
            deleted.append(kb_id)

    class Sessions:
        def clear_kb(self, kb_id):
            raise RuntimeError("session cleanup failed")

    @contextmanager
    def lock(kb_id):
        yield

    console = cli_module.Console.__new__(cli_module.Console)
    console.knowledge_store = Store()
    console.feedback_store = Store()
    console.feedback_analysis_store = Store()
    console.retrieval_feedback_store = Store()
    console.retrieval_eval_draft_store = Store()
    console.registry = Registry()
    console.sessions = Sessions()
    monkeypatch.setattr(cli_module, "kb_write_lock", lock)
    monkeypatch.setattr(cli_module, "delete_kb_index_transactional", lambda kb: None)
    monkeypatch.setattr(cli_module, "mark_kb_deleted", lambda kb: None)

    with pytest.raises(cli_module.KBCleanupError, match="会话状态"):
        console._delete_kb("kb")

    assert deleted == []


# 验证 CLI 重建把 runtime 的派生知识存储传入事务构建。
def test_cli_rebuild_uses_runtime_knowledge_store(monkeypatch):
    from cogdoc import cli as cli_module

    knowledge_store = object()
    captured = []
    console = cli_module.Console.__new__(cli_module.Console)
    console.active_kb = "kb"
    console.knowledge_store = knowledge_store
    console.registry = SimpleNamespace(source_dir=lambda kb: "/sources/kb")
    monkeypatch.setattr(
        cli_module,
        "build_kb_index_transactional",
        lambda kb, source_dir, *, knowledge_store=None: (
            captured.append((kb, source_dir, knowledge_store))
            or SimpleNamespace(document_count=0, chunk_count=0, documents=[])
        ),
    )
    monkeypatch.setattr(cli_module, "_warm_kb", lambda kb: None)

    console._rebuild()

    assert captured == [("kb", "/sources/kb", knowledge_store)]


# 验证 Debug 自动构建使用其 StateRuntime 的派生知识存储。
def test_debug_build_uses_runtime_knowledge_store(tmp_path, monkeypatch):
    from cogdoc import debug as debug_module

    knowledge_store = object()
    captured = []
    monkeypatch.setattr(
        debug_module,
        "build_kb_index_transactional",
        lambda kb, source_dir, *, knowledge_store=None: (
            captured.append((kb, source_dir, knowledge_store))
            or SimpleNamespace(document_count=0, chunk_count=0, documents=[])
        ),
    )

    debug_module.build_index(
        "kb",
        str(tmp_path),
        knowledge_store=knowledge_store,
    )

    assert captured == [("kb", str(tmp_path), knowledge_store)]


# 验证 Debug 对话与自动构建共享同一个 StateRuntime。
def test_debug_session_passes_state_runtime_to_chat(monkeypatch):
    from cogdoc import debug as debug_module

    runtime = object()
    captured = []

    def run_chat(**kwargs):
        captured.append(kwargs)
        return iter(())

    monkeypatch.setattr(debug_module, "run_chat", run_chat)
    session = debug_module.DebugSession(state_runtime=runtime)

    session.ask("kb", "问题")

    assert captured[0]["state_runtime"] is runtime


# 验证 Debug /retrieve 复用线上检索链并传入 session runtime。
def test_debug_retrieve_uses_runtime_online_retrieval(monkeypatch):
    from cogdoc import debug as debug_module
    from cogdoc.api.routes import agent as agent_module

    runtime = object()
    captured = {}
    docs = [
        {
            "text": "派生知识",
            "meta": {
                "chunk_id": "knowledge:K1",
                "source": "knowledge:K1",
                "source_type": "derived_knowledge",
            },
            "retrieval": {
                "feedback_boost": 0.5,
                "rerank_score": 0.9,
            },
        }
    ]

    def run_retrieve(body, *, state_runtime=None):
        captured["body"] = body
        captured["runtime"] = state_runtime
        return docs

    def render(query, rendered_docs, reranked, device):
        captured["render"] = (query, rendered_docs, reranked, device)

    monkeypatch.setattr(agent_module, "_run_retrieve", run_retrieve)
    monkeypatch.setattr(debug_module, "print_retrieve_debug_output", render)
    monkeypatch.setattr(debug_module.BGEReranker, "default_device", lambda: "cuda")
    monkeypatch.setattr(
        debug_module,
        "get_settings",
        lambda: SimpleNamespace(
            qa_retrieval_top_k=9,
            qa_rerank_top_n=3,
            qa_rerank_on_cpu=False,
        ),
    )

    debug_module.run_retrieve_debug(
        "kb",
        "问题",
        state_runtime=runtime,
    )

    assert captured["runtime"] is runtime
    assert captured["body"].doc_id == "kb"
    assert captured["body"].rerank is True
    assert captured["render"] == ("问题", docs, True, "cuda")
