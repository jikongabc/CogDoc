import sys
import pytest


class FakeWebhookDispatcher:
    enabled = True

    def __init__(self):
        self.events = []

    def emit(self, event, payload):
        self.events.append((event, payload))
        return True


@pytest.fixture
def webhook_dispatcher():
    return FakeWebhookDispatcher()


# 重置检索器检索引擎缓存。
@pytest.fixture(autouse=True)
def _reset_retriever_engine_cache():
    # 防止进程级引擎缓存在测试间留脏；仅在用过检索栈时清，不强行拉起重依赖。
    yield
    module = sys.modules.get("cogdoc.graph.subgraphs.qa")
    if module is not None:
        factory = module.RetrieverFactory
        with factory._lock:
            factory._engines.clear()


# 隔离版本存储。
@pytest.fixture(autouse=True)
def _isolate_epoch_store(tmp_path, monkeypatch):
    # 全局版本、生命周期、变更日志和删除队列单例隔离到每个测试临时目录。
    import cogdoc.service.kb_epoch as ke
    import cogdoc.service.kb_lifecycle as kl
    import cogdoc.service.mutation_journal as mj
    import cogdoc.service.purge_queue as pq
    from cogdoc.config.settings import get_settings
    import cogdoc.api.app as app_module

    monkeypatch.setattr(
        ke, "_shared", ke.EpochStore(path=str(tmp_path / "epochs.json"))
    )
    monkeypatch.setattr(
        kl, "_shared", kl.LifecycleStore(path=str(tmp_path / "lifecycle.json"))
    )
    monkeypatch.setattr(
        mj, "_shared", mj.MutationJournal(journal_dir=str(tmp_path / "journal"))
    )
    monkeypatch.setattr(
        pq, "_shared", pq.PurgeQueue(path=str(tmp_path / "purge_queue.json"))
    )
    # App lifespans must never scan a developer's real Chroma directory. The
    # filesystem sweeper has dedicated temp-directory tests.
    monkeypatch.setattr(
        app_module,
        "sweep_orphan_segment_directories",
        lambda _path: {"scanned": 0, "removed": 0, "bytes_reclaimed": 0},
    )
    # Developer .env credentials must not turn disabled-vault test apps into
    # credential-bearing production apps.
    monkeypatch.setenv("COGDOC_CREDENTIAL_MASTER_KEYS", "")
    monkeypatch.setenv("COGDOC_CONNECTOR_VAULT_KEYS", "")

    # 测试在同一进程内反复拉起应用生命周期，关闭严格单实例避免进程锁争用误杀。
    monkeypatch.setenv("COGDOC_ALLOW_MULTI", "1")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
    # 取消测试中残留的后台定时器，避免后台线程跨测试触发真实清理。
    import cogdoc.service.ingest_service as isvc

    isvc.cancel_all_timers()
