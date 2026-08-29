import asyncio
import time
from contextlib import nullcontext
from types import SimpleNamespace
import pytest
from httpx import ASGITransport, AsyncClient
from cogdoc.api.app import create_app
from cogdoc.api.derived_knowledge_store import DerivedKnowledgeStore
from cogdoc.api.feedback_analysis_store import FeedbackAnalysisStore
from cogdoc.api.feedback_store import FeedbackStore
from cogdoc.api.ingest import IndexJobManager, KnowledgeBaseRegistry
from cogdoc.api.retrieval_feedback_store import RetrievalFeedbackStore
from cogdoc.api.retrieval_eval_draft_store import RetrievalEvalDraftStore
from cogdoc.api.research_job_store import ResearchJobStore
from cogdoc.api.resource_access import ResourceAccessStore
from cogdoc.api.routes import documents as documents_route
from cogdoc.tools.chunk_identity import build_document_id
from cogdoc.tools.eval.retrieval_eval_drafts import create_pending_draft


# 指定异步测试后端。
@pytest.fixture
def anyio_backend():
    return "asyncio"


# 模拟成功ingest。
def _ok_ingest(kb_id, source_dir):
    return SimpleNamespace(
        document_count=1,
        chunk_count=3,
        ocr_summary={
            "candidate_pages": 2,
            "attempted_pages": 2,
            "succeeded_pages": 1,
            "degraded_pages": 1,
            "failed_pages": 0,
            "status_counts": {"succeeded": 1, "timeout": 1},
        },
    )


# 构造应用。
def _make_app(
    tmp_path,
    ingest_fn=_ok_ingest,
    monkeypatch=None,
    resource_access_store=None,
):
    if monkeypatch is not None:
        import cogdoc.api.app as app_module

        monkeypatch.setattr(app_module, "configure_logging", lambda: None)

    # 返回目录for。
    def source_dir_for(kb_id: str) -> str:
        return str(tmp_path / "kb" / kb_id / "sources")

    registry = KnowledgeBaseRegistry(
        registry_path=str(tmp_path / "registry.json"), source_dir_for=source_dir_for
    )
    jobs = IndexJobManager(
        ingest_fn=ingest_fn,
        source_dir_for=source_dir_for,
        kb_exists=registry.exists,
    )
    app = create_app(
        kb_registry=registry,
        index_jobs=jobs,
        knowledge_store=DerivedKnowledgeStore(path=str(tmp_path / "knowledge.jsonl")),
        feedback_store=FeedbackStore(
            feedback_path=str(tmp_path / "feedback.jsonl"),
            bad_cases_path=str(tmp_path / "bad_cases.jsonl"),
        ),
        feedback_analysis_store=FeedbackAnalysisStore(
            path=str(tmp_path / "feedback_analysis.jsonl")
        ),
        retrieval_feedback_store=RetrievalFeedbackStore(
            path=str(tmp_path / "retrieval_feedback.jsonl")
        ),
        retrieval_eval_draft_store=RetrievalEvalDraftStore(
            path=str(tmp_path / "retrieval_eval_drafts.jsonl")
        ),
        research_job_store=ResearchJobStore(path=str(tmp_path / "research_jobs.json")),
        derived_knowledge_index_clearer=lambda _kb_id: None,
        resource_access_store=resource_access_store,
    )
    return app, source_dir_for


# 创建测试客户端。
async def _client(app):
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://testserver")


# 等待任务。
async def _wait_job(client, job_id, timeout=2.0):
    deadline = time.time() + timeout
    resp = await client.get(f"/v1/index-jobs/{job_id}")
    while time.time() < deadline and resp.json()["status"] in ("pending", "running"):
        await asyncio.sleep(0.02)
        resp = await client.get(f"/v1/index-jobs/{job_id}")
    return resp


# 验证 registry corrupt quarantines and fails closed。
def test_registry_corrupt_quarantines_and_fails_closed(tmp_path):
    from cogdoc.api.ingest import RegistryCorruptError

    reg_path = tmp_path / "registry.json"
    reg_path.write_text("{ 半截损坏的 json", encoding="utf-8")

    # 损坏的 registry 不再静默退回空表（否则现存 KB 全消失、同名重建复用旧数据），而是隔离并 fail-closed 抛错。
    with pytest.raises(RegistryCorruptError):
        KnowledgeBaseRegistry(
            registry_path=str(reg_path), source_dir_for=lambda kb: str(tmp_path / kb)
        )
    # 损坏文件被改名留存供人工恢复。
    assert list(tmp_path.glob("registry.json.corrupt-*"))


# 验证 create rolls back when lifecycle finalize fails。
def test_create_rolls_back_when_lifecycle_finalize_fails(tmp_path, monkeypatch):
    from cogdoc.api.ingest import KnowledgeBaseRegistry

    source_dir = tmp_path / "kb" / "sources"
    registry = KnowledgeBaseRegistry(
        registry_path=str(tmp_path / "registry.json"),
        source_dir_for=lambda _: str(source_dir),
    )
    monkeypatch.setattr(
        "cogdoc.api.ingest.shared_lifecycle_store",
        lambda: type(
            "BrokenLifecycle",
            (),
            {"set": lambda *args: (_ for _ in ()).throw(OSError("disk"))},
        )(),
    )
    with pytest.raises(OSError, match="disk"):
        registry.create("kb")
    assert not registry.exists("kb")
    assert not source_dir.parent.exists()


# 验证 create list get knowledge base。
@pytest.mark.anyio
async def test_create_list_get_knowledge_base(tmp_path, monkeypatch):
    app, _ = _make_app(tmp_path, monkeypatch=monkeypatch)
    async with app.router.lifespan_context(app):
        async with await _client(app) as client:
            created = await client.post("/v1/knowledge-bases", json={"kb_id": "papers"})
            dup = await client.post("/v1/knowledge-bases", json={"kb_id": "papers"})
            listed = await client.get("/v1/knowledge-bases")
            got = await client.get("/v1/knowledge-bases/papers")
            missing = await client.get("/v1/knowledge-bases/nope")

    assert created.status_code == 201
    assert created.json()["kb_id"] == "papers"
    assert created.json()["tenant_id"] == "default"
    assert dup.status_code == 409 and dup.json()["error_code"] == "KB_EXISTS"
    assert [kb["kb_id"] for kb in listed.json()] == ["papers"]
    assert got.status_code == 200 and got.json()["document_count"] == 0
    assert missing.status_code == 404 and missing.json()["error_code"] == "KB_NOT_FOUND"


# 验证 delete knowledge base。
@pytest.mark.anyio
async def test_delete_knowledge_base(tmp_path, monkeypatch):
    import os

    app, source_dir_for = _make_app(tmp_path, monkeypatch=monkeypatch)
    import cogdoc.api.routes.documents as docs_module

    monkeypatch.setattr(
        docs_module, "delete_kb_index_transactional", lambda kb_id: None
    )

    async with app.router.lifespan_context(app):
        async with await _client(app) as client:
            await client.post("/v1/knowledge-bases", json={"kb_id": "kb"})
            deleted = await client.delete("/v1/knowledge-bases/kb")
            after = await client.get("/v1/knowledge-bases")
            missing = await client.delete("/v1/knowledge-bases/ghost")

    assert deleted.status_code == 204
    assert after.json() == []
    assert not os.path.exists(os.path.dirname(source_dir_for("kb")))
    assert missing.status_code == 404 and missing.json()["error_code"] == "KB_NOT_FOUND"


# 验证删除 KB 会清理审核队列状态，同名重建不继承旧反馈。
@pytest.mark.anyio
async def test_delete_recreated_kb_clears_review_state(tmp_path, monkeypatch):
    app, _ = _make_app(tmp_path, monkeypatch=monkeypatch)
    import cogdoc.api.routes.documents as docs_module

    monkeypatch.setattr(
        docs_module, "delete_kb_index_transactional", lambda kb_id: None
    )

    async with app.router.lifespan_context(app):
        async with await _client(app) as client:
            await client.post("/v1/knowledge-bases", json={"kb_id": "kb"})
            await client.post(
                "/v1/knowledge", json={"kb_id": "kb", "text": "待审核知识。"}
            )
            app.state.feedback_store.record(
                {
                    "kb_id": "kb",
                    "trace_id": "t1",
                    "feedback": "thumbs_down",
                    "query": "问题",
                }
            )
            app.state.feedback_analysis_store.record(
                "fb1",
                {"kb_id": "kb", "trace_id": "t1", "query": "问题"},
                {
                    "feedback_type": "correction",
                    "sentiment": "negative",
                    "target": {
                        "chunk_ids": ["c1"],
                        "sources": ["a.pdf"],
                        "source_type": "document",
                    },
                    "extracted_claim": "正确说法",
                    "recommended_action": "create_pending_knowledge",
                    "weight_delta": -0.55,
                    "confidence": 0.9,
                    "needs_review": True,
                },
            )
            app.state.retrieval_feedback_store.record_from_feedback(
                "fb1",
                {
                    "kb_id": "kb",
                    "query": "问题",
                    "feedback": "thumbs_down",
                    "feedback_type": "bad_retrieval",
                    "citations": [{"chunk_id": "c1"}],
                },
            )
            app.state.retrieval_eval_draft_store.ensure(
                create_pending_draft(
                    kb_id="kb",
                    query="问题",
                    units=[
                        {
                            "unit_id": "r1",
                            "task_kind": "qa_requirement",
                            "label": "问题",
                            "retrieval_query": "问题",
                        }
                    ],
                    now="2026-08-05T00:00:00+00:00",
                )
            )
            research_response = await client.post(
                "/v1/research-jobs",
                json={"kb_id": "kb", "objective": "删除后不应保留的研究目标"},
            )
            assert research_response.status_code == 201
            app.state.index_jobs._store.create(
                {
                    "job_id": "old-index-job",
                    "kb_id": "kb",
                    "status": "succeeded",
                    "created_at": "2026-08-27T00:00:00+00:00",
                }
            )
            app.state.claim_verification_review_store.record_candidates(
                "default",
                [
                    {
                        "review_id": "a" * 32,
                        "kb_id": "kb",
                        "task_type": "qa",
                        "policy_id": "b" * 16,
                        "effective_mode": "shadow",
                        "decision": "would_allow",
                        "claim_id": "claim-1",
                        "claim": "删除后不应保留的声明",
                        "actual_verdict": "supported",
                        "reason": "测试",
                        "confidence": 0.9,
                        "duration_ms": 1.0,
                        "cited_chunk_ids": ["chunk-1"],
                        "supporting_chunk_ids": ["chunk-1"],
                        "evidence": [
                            {
                                "chunk_id": "chunk-1",
                                "source": "old.pdf",
                                "text": "删除后不应保留的证据",
                            }
                        ],
                        "evidence_complete": True,
                    }
                ],
            )
            app.state.index_jobs._journal.begin_upload(
                "old-journal",
                "kb",
                str(tmp_path / "old.pdf"),
                str(tmp_path / "old.backup"),
                had_old=True,
            )

            before = await client.get("/v1/review-queue", params={"kb_id": "kb"})
            deleted = await client.delete("/v1/knowledge-bases/kb")
            recreated = await client.post("/v1/knowledge-bases", json={"kb_id": "kb"})
            summary = await client.get("/v1/review-queue", params={"kb_id": "kb"})
            pending_count = await client.get(
                "/v1/knowledge/pending-count", params={"kb_id": "kb"}
            )
            eval_drafts_after = app.state.retrieval_eval_draft_store.export_records()
            research_jobs_after = app.state.research_job_store.export_records()
            index_jobs_after = app.state.index_jobs.list({"kb"})
            claim_reviews_after = app.state.claim_verification_review_store.list_page(
                "default", kb_id="kb"
            )["items"]
            journal_after = app.state.index_jobs._journal.has_entries("kb")

    assert before.json()["feedback_counts"]["total"] == 1
    assert before.json()["retrieval_feedback"]["enabled"] == 1
    assert deleted.status_code == 204
    assert recreated.status_code == 201
    body = summary.json()
    assert body["knowledge"] == {}
    assert body["feedback_counts"]["total"] == 0
    assert body["feedback_counts"]["bad_cases"] == 0
    assert body["feedback_analysis"]["needs_review"] == 0
    assert body["retrieval_feedback"]["enabled"] == 0
    assert eval_drafts_after == []
    assert research_jobs_after == []
    assert index_jobs_after == []
    assert claim_reviews_after == []
    assert pending_count.json()["total"] == 0

    assert journal_after is False


# 验证 delete kb cleanup failure keeps kb。
@pytest.mark.anyio
async def test_delete_kb_cleanup_failure_keeps_kb(tmp_path, monkeypatch):
    from cogdoc.service.ingest_service import KBCleanupError

    app, _ = _make_app(tmp_path, monkeypatch=monkeypatch)
    import cogdoc.api.routes.documents as docs_module

    # 模拟失败路径。
    def boom(kb_id):
        raise KBCleanupError("部分代清理失败")

    monkeypatch.setattr(docs_module, "delete_kb_index_transactional", boom)

    async with app.router.lifespan_context(app):
        async with await _client(app) as client:
            await client.post("/v1/knowledge-bases", json={"kb_id": "kb"})
            resp = await client.delete("/v1/knowledge-bases/kb")
            after = await client.get("/v1/knowledge-bases")
            retry = await client.delete("/v1/knowledge-bases/kb")

    # 清理失败：registry 保留作为重试入口，但 deleting KB 不再可读。
    assert resp.status_code == 500
    assert resp.json()["error_code"] == "KB_CLEANUP_FAILED"
    assert after.json() == []
    assert app.state.kb_registry.get("kb") is not None
    assert retry.status_code == 500
    assert retry.json()["error_code"] == "KB_CLEANUP_FAILED"


# 验证会话清理失败不撤销 registry，保留 DELETE 重试入口。
def test_delete_kb_session_cleanup_failure_keeps_registry(monkeypatch):
    import cogdoc.api.routes.documents as docs_module
    from cogdoc.service.ingest_service import KBCleanupError

    deleted = []
    released = []
    registry = SimpleNamespace(delete=lambda kb_id: deleted.append(kb_id))
    jobs = SimpleNamespace(release_executor=lambda kb_id: released.append(kb_id))
    sessions = SimpleNamespace(
        clear_kb=lambda kb_id: (_ for _ in ()).throw(RuntimeError("session cleanup"))
    )
    monkeypatch.setattr(docs_module, "kb_write_lock", lambda kb_id: nullcontext())
    monkeypatch.setattr(
        docs_module,
        "delete_kb_index_transactional",
        lambda kb_id: None,
    )
    monkeypatch.setattr(docs_module, "mark_kb_deleted", lambda kb_id: None)
    monkeypatch.setattr(docs_module, "delete_trace_files", lambda **kwargs: 0)

    with pytest.raises(KBCleanupError, match="会话状态"):
        docs_module._delete_kb(
            "kb",
            registry,
            jobs,
            session_store=sessions,
        )

    assert deleted == []
    assert released == ["kb"]


def test_queued_kb_delete_revalidates_before_any_destructive_cleanup(monkeypatch):
    import cogdoc.api.routes.documents as docs_module

    deleted = []
    released = []
    destructive_calls = []
    registry = SimpleNamespace(delete=lambda kb_id: deleted.append(kb_id))
    jobs = SimpleNamespace(release_executor=lambda kb_id: released.append(kb_id))

    def authorization_guard() -> None:
        raise PermissionError("membership was removed while delete was queued")

    monkeypatch.setattr(docs_module, "kb_write_lock", lambda kb_id: nullcontext())
    monkeypatch.setattr(
        docs_module,
        "delete_kb_index_transactional",
        lambda kb_id: destructive_calls.append(("index", kb_id)),
    )
    monkeypatch.setattr(
        docs_module,
        "mark_kb_deleted",
        lambda kb_id: destructive_calls.append(("tombstone", kb_id)),
    )
    monkeypatch.setattr(
        docs_module,
        "delete_trace_files",
        lambda **kwargs: destructive_calls.append(("traces", kwargs["doc_id"])),
    )

    with pytest.raises(PermissionError, match="membership was removed"):
        docs_module._delete_kb(
            "kb",
            registry,
            jobs,
            authorization_guard=authorization_guard,
        )

    assert destructive_calls == []
    assert deleted == []
    assert released == ["kb"]


@pytest.mark.anyio
async def test_kb_delete_revoked_during_connector_drain_restores_fence_and_schedule(
    monkeypatch,
):
    import cogdoc.api.routes.documents as docs_module

    lifecycle = SimpleNamespace(current="active")
    lifecycle.status = lambda _kb_id: lifecycle.current
    lifecycle.set = lambda _kb_id, status: setattr(lifecycle, "current", status)
    registry = SimpleNamespace(
        get_by_storage_id=lambda _kb_id: {
            "tenant_id": "tenant",
            "storage_id": "storage-kb",
            "kb_id": "docs",
        }
    )
    scope = SimpleNamespace(
        tenant_id="tenant",
        storage_id="storage-kb",
        external_id="docs",
    )
    guard_calls = []

    def guard(**kwargs):
        guard_calls.append(kwargs)
        if kwargs.get("require_resource_acl"):
            raise PermissionError("grant revoked while workers drained")

    restored = []
    manager = SimpleNamespace(
        prepare_knowledge_base_delete=lambda *_args: {
            "previously_enabled_connection_ids": ["conn-scheduled"]
        },
        restore_knowledge_base_delete=lambda tenant, kb, ids: restored.append(
            (tenant, kb, ids)
        ),
    )

    async def immediate(_executor, function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(docs_module, "run_sync", immediate)
    monkeypatch.setattr(docs_module, "resolve_kb_scope", lambda *_a, **_k: scope)
    monkeypatch.setattr(
        docs_module, "_live_session_authorization_guard", lambda *_a, **_k: guard
    )
    monkeypatch.setattr(docs_module, "shared_lifecycle_store", lambda: lifecycle)
    monkeypatch.setattr(docs_module, "kb_write_lock", lambda _kb_id: nullcontext())
    monkeypatch.setattr(
        docs_module,
        "_cleanup_connector_kb_state",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("irreversible cleanup must not run")
        ),
    )
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                kb_registry=registry,
                index_jobs=SimpleNamespace(),
                connector_sync_store=SimpleNamespace(
                    scope_activity=lambda *_args: {"committing": 0}
                ),
                sync_manager=manager,
                offload_executor=None,
            )
        )
    )

    response = await docs_module.delete_knowledge_base("docs", request)

    assert response.status_code == 404
    assert guard_calls == [{}, {"require_resource_acl": True}]
    assert lifecycle.current == "active"
    assert restored == [("tenant", "storage-kb", ("conn-scheduled",))]


# 验证 create kb rejects overlong id。
@pytest.mark.anyio
async def test_create_kb_rejects_overlong_id(tmp_path, monkeypatch):
    app, _ = _make_app(tmp_path, monkeypatch=monkeypatch)
    async with app.router.lifespan_context(app):
        async with await _client(app) as client:
            too_long = await client.post(
                "/v1/knowledge-bases", json={"kb_id": "x" * 57}
            )
            boundary = await client.post(
                "/v1/knowledge-bases", json={"kb_id": "y" * 56}
            )

    # 超过 56 字符会让 col-{kb_id} 截断撞库，必须在契约层挡掉。
    assert too_long.status_code == 422
    assert boundary.status_code == 201


# 验证 upload triggers job until succeeded。
@pytest.mark.anyio
async def test_upload_triggers_job_until_succeeded(tmp_path, monkeypatch):
    app, source_dir_for = _make_app(tmp_path, monkeypatch=monkeypatch)
    async with app.router.lifespan_context(app):
        async with await _client(app) as client:
            await client.post("/v1/knowledge-bases", json={"kb_id": "kb"})
            up = await client.post(
                "/v1/knowledge-bases/kb/documents",
                files={"file": ("a.pdf", b"%PDF-1.4 fake content", "application/pdf")},
            )
            job_id = up.json()["job_id"]
            done = await _wait_job(client, job_id)
            listed = await client.get("/v1/index-jobs")
            missing_kb = await client.get(
                "/v1/index-jobs", params={"kb_id": "does-not-exist"}
            )

    assert up.status_code == 202 and up.json()["job_id"]
    assert up.json()["status"] in ("pending", "running", "succeeded")
    assert done.json()["status"] == "succeeded"
    assert done.json()["document_count"] == 1 and done.json()["chunk_count"] == 3
    assert listed.status_code == 200
    assert listed.json()["jobs"] == [done.json()]
    assert missing_kb.status_code == 404
    assert missing_kb.json()["error_code"] == "KB_NOT_FOUND"
    assert done.json()["ocr_summary"] == {
        "candidate_pages": 2,
        "attempted_pages": 2,
        "succeeded_pages": 1,
        "degraded_pages": 1,
        "failed_pages": 0,
        "status_counts": {"succeeded": 1, "timeout": 1},
    }
    import os

    assert os.path.exists(os.path.join(source_dir_for("kb"), "a.pdf"))


# 验证 upload rejects bad inputs。
@pytest.mark.anyio
async def test_upload_rejects_bad_inputs(tmp_path, monkeypatch):
    app, _ = _make_app(tmp_path, monkeypatch=monkeypatch)
    async with app.router.lifespan_context(app):
        async with await _client(app) as client:
            to_missing = await client.post(
                "/v1/knowledge-bases/nope/documents",
                files={"file": ("a.pdf", b"%PDF-1.4", "application/pdf")},
            )
            await client.post("/v1/knowledge-bases", json={"kb_id": "kb"})
            not_pdf_ext = await client.post(
                "/v1/knowledge-bases/kb/documents",
                files={"file": ("a.txt", b"%PDF-1.4", "text/plain")},
            )
            bad_magic = await client.post(
                "/v1/knowledge-bases/kb/documents",
                files={"file": ("a.pdf", b"not a pdf", "application/pdf")},
            )
            reserved = await client.post(
                "/v1/knowledge-bases/kb/documents",
                files={
                    "file": (
                        ".cogdoc-connector-deadbeef.md",
                        b"must not enter connector namespace",
                        "text/markdown",
                    )
                },
            )
            reserved_delete = await client.delete(
                "/v1/knowledge-bases/kb/documents/.cogdoc-connector-deadbeef.md"
            )

    assert to_missing.status_code == 404
    assert (
        not_pdf_ext.status_code == 400
        and not_pdf_ext.json()["error_code"] == "INVALID_PDF"
    )
    assert (
        bad_magic.status_code == 400 and bad_magic.json()["error_code"] == "INVALID_PDF"
    )
    assert reserved.status_code == 400
    assert reserved_delete.status_code == 404


@pytest.mark.anyio
async def test_upload_accepts_supported_non_pdf_document(tmp_path, monkeypatch):
    app, _ = _make_app(tmp_path, monkeypatch=monkeypatch)
    async with app.router.lifespan_context(app):
        async with await _client(app) as client:
            await client.post("/v1/knowledge-bases", json={"kb_id": "kb"})
            response = await client.post(
                "/v1/knowledge-bases/kb/documents",
                files={
                    "file": (
                        "guide.md",
                        b"# Guide\n\nSupported markdown content.",
                        "text/markdown",
                    )
                },
            )
    assert response.status_code == 202


@pytest.mark.anyio
async def test_embedding_profiles_hide_credentials(tmp_path, monkeypatch):
    app, _ = _make_app(tmp_path, monkeypatch=monkeypatch)
    async with app.router.lifespan_context(app):
        async with await _client(app) as client:
            response = await client.get("/v1/embedding-profiles")

    assert response.status_code == 200
    profiles = response.json()
    assert [profile["profile_id"] for profile in profiles] == ["local", "cloud"]
    assert profiles[0]["available"] is True
    assert all("api_key" not in profile for profile in profiles)


def test_kb_embedding_metadata_cache_is_invalidated_by_state_fingerprint(monkeypatch):
    calls = []

    class FakeState:
        def __init__(self, storage_id):
            self.storage_id = storage_id

        def active(self):
            calls.append(self.storage_id)
            return {"embedding_model": "local"}

    documents_route._cached_kb_embedding_metadata.cache_clear()
    monkeypatch.setattr(documents_route, "KBState", FakeState)
    try:
        first = documents_route._cached_kb_embedding_metadata("kb", 1, 10, "")
        repeated = documents_route._cached_kb_embedding_metadata("kb", 1, 10, "")
        changed = documents_route._cached_kb_embedding_metadata("kb", 2, 10, "")
    finally:
        documents_route._cached_kb_embedding_metadata.cache_clear()

    assert first == repeated == changed == ("local", "BAAI/bge-m3")
    assert calls == ["kb", "kb"]


@pytest.mark.anyio
async def test_batch_upload_writes_all_files_and_indexes_once(tmp_path, monkeypatch):
    calls = []

    def ingest(kb_id, source_dir, *, embedding_profile_id=None):
        calls.append((kb_id, embedding_profile_id))
        return SimpleNamespace(document_count=2, chunk_count=4, ocr_summary=None)

    app, source_dir_for = _make_app(tmp_path, ingest_fn=ingest, monkeypatch=monkeypatch)
    async with app.router.lifespan_context(app):
        async with await _client(app) as client:
            await client.post("/v1/knowledge-bases", json={"kb_id": "kb"})
            response = await client.post(
                "/v1/knowledge-bases/kb/documents/batch",
                files=[
                    ("files", ("guide.md", b"# Guide", "text/markdown")),
                    ("files", ("notes.txt", b"notes", "text/plain")),
                ],
                data={"embedding_profile_id": "local"},
            )
            done = await _wait_job(client, response.json()["job_id"])

    assert response.status_code == 202
    assert done.json()["status"] == "succeeded"
    assert calls == [("kb", "local")]
    source_dir = source_dir_for("kb")
    assert (tmp_path / "kb" / "kb" / "sources" / "guide.md").exists()
    assert (tmp_path / "kb" / "kb" / "sources" / "notes.txt").exists()
    assert source_dir.endswith("/kb/kb/sources")


@pytest.mark.anyio
async def test_batch_upload_rejects_duplicate_names(tmp_path, monkeypatch):
    app, _ = _make_app(tmp_path, monkeypatch=monkeypatch)
    async with app.router.lifespan_context(app):
        async with await _client(app) as client:
            await client.post("/v1/knowledge-bases", json={"kb_id": "kb"})
            response = await client.post(
                "/v1/knowledge-bases/kb/documents/batch",
                files=[
                    ("files", ("same.txt", b"one", "text/plain")),
                    ("files", ("same.txt", b"two", "text/plain")),
                ],
                data={"embedding_profile_id": "local"},
            )

    assert response.status_code == 422
    assert response.json()["error_code"] == "BAD_REQUEST"


# 验证 upload rejects oversize。
@pytest.mark.anyio
async def test_upload_rejects_oversize(tmp_path, monkeypatch):
    app, _ = _make_app(tmp_path, monkeypatch=monkeypatch)
    import cogdoc.api.routes.documents as docs_module

    monkeypatch.setattr(
        docs_module, "get_settings", lambda: SimpleNamespace(max_upload_mb=0)
    )
    async with app.router.lifespan_context(app):
        async with await _client(app) as client:
            await client.post("/v1/knowledge-bases", json={"kb_id": "kb"})
            resp = await client.post(
                "/v1/knowledge-bases/kb/documents",
                files={"file": ("a.pdf", b"%PDF-1.4 data", "application/pdf")},
            )

    assert resp.status_code == 413 and resp.json()["error_code"] == "FILE_TOO_LARGE"


# 验证 delete document and job not found。
@pytest.mark.anyio
async def test_delete_document_and_job_not_found(tmp_path, monkeypatch):
    app, source_dir_for = _make_app(tmp_path, monkeypatch=monkeypatch)
    async with app.router.lifespan_context(app):
        async with await _client(app) as client:
            await client.post("/v1/knowledge-bases", json={"kb_id": "kb"})
            up = await client.post(
                "/v1/knowledge-bases/kb/documents",
                files={"file": ("a.pdf", b"%PDF-1.4 data", "application/pdf")},
            )
            # 文件写入在 executor 内异步完成；等 upload job 结束后文件才落盘。
            await _wait_job(client, up.json()["job_id"])
            deleted = await client.delete("/v1/knowledge-bases/kb/documents/a.pdf")
            delete_missing = await client.delete(
                "/v1/knowledge-bases/kb/documents/ghost.pdf"
            )
            job_missing = await client.get("/v1/index-jobs/does-not-exist")

    # delete_document 始终 202；文档不存在时 job 以 DOCUMENT_NOT_FOUND 状态失败。
    assert deleted.status_code == 202
    assert delete_missing.status_code == 202
    assert job_missing.status_code == 404
    assert job_missing.json()["error_code"] == "JOB_NOT_FOUND"


# 验证 delete missing document job fails with not found。
@pytest.mark.anyio
async def test_delete_missing_document_job_fails_with_not_found(tmp_path, monkeypatch):
    app, _ = _make_app(tmp_path, monkeypatch=monkeypatch)
    async with app.router.lifespan_context(app):
        async with await _client(app) as client:
            await client.post("/v1/knowledge-bases", json={"kb_id": "kb"})
            resp = await client.delete("/v1/knowledge-bases/kb/documents/ghost.pdf")
            assert resp.status_code == 202
            done = await _wait_job(client, resp.json()["job_id"])
    assert done.json()["status"] == "failed"
    assert done.json()["error_code"] == "DOCUMENT_NOT_FOUND"


@pytest.mark.anyio
async def test_successful_document_delete_clears_acl_before_job_succeeds_and_reupload(
    tmp_path, monkeypatch
):
    access_store = ResourceAccessStore(tmp_path / "resource-access.db")
    app, _ = _make_app(
        tmp_path,
        monkeypatch=monkeypatch,
        resource_access_store=access_store,
    )
    document_id = build_document_id("a.pdf")

    async with app.router.lifespan_context(app):
        async with await _client(app) as client:
            created = await client.post("/v1/knowledge-bases", json={"kb_id": "kb"})
            assert created.status_code == 201
            storage_id = app.state.kb_registry.resolve("kb", "default")["storage_id"]

            uploaded = await client.post(
                "/v1/knowledge-bases/kb/documents",
                files={"file": ("a.pdf", b"%PDF-1.4 data", "application/pdf")},
            )
            uploaded_done = await _wait_job(client, uploaded.json()["job_id"])
            assert uploaded_done.json()["status"] == "succeeded"

            access_store.set_document_policy(
                "default", storage_id, document_id, "a.pdf", policy="private"
            )
            access_store.grant_subject(
                "default", storage_id, "alice", "viewer", document_id=document_id
            )

            cleared_reviews = []
            app.state.claim_verification_review_store.clear_document = lambda *scope: (
                cleared_reviews.append(scope)
            )

            deleted = await client.delete("/v1/knowledge-bases/kb/documents/a.pdf")
            deleted_done = await _wait_job(client, deleted.json()["job_id"])
            assert deleted_done.json()["status"] == "succeeded"
            assert cleared_reviews == [("default", storage_id, "a.pdf")]
            # The terminal success is published only after both policy and
            # document-scoped grants have been removed.
            assert (
                access_store.get_document_policy("default", storage_id, document_id)
                is None
            )
            assert (
                access_store.list_grants("default", storage_id, document_id=document_id)
                == []
            )

            reuploaded = await client.post(
                "/v1/knowledge-bases/kb/documents",
                files={"file": ("a.pdf", b"%PDF-1.4 new", "application/pdf")},
            )
            reuploaded_done = await _wait_job(client, reuploaded.json()["job_id"])
            assert reuploaded_done.json()["status"] == "succeeded"

    new_policy = access_store.get_document_policy("default", storage_id, document_id)
    assert new_policy is not None
    assert new_policy["policy"] == "inherit"
    assert (
        access_store.list_grants("default", storage_id, document_id=document_id) == []
    )


@pytest.mark.anyio
async def test_document_delete_retry_finishes_acl_after_source_commit(
    tmp_path, monkeypatch
):
    access_store = ResourceAccessStore(tmp_path / "resource-access.db")
    app, _ = _make_app(
        tmp_path,
        monkeypatch=monkeypatch,
        resource_access_store=access_store,
    )
    document_id = build_document_id("a.pdf")
    original_finish = access_store.finish_document_retirement
    attempts = 0

    def fail_finish(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        raise OSError("temporary ACL failure")

    async with app.router.lifespan_context(app):
        async with await _client(app) as client:
            created = await client.post("/v1/knowledge-bases", json={"kb_id": "kb"})
            assert created.status_code == 201
            storage_id = app.state.kb_registry.resolve("kb", "default")["storage_id"]
            uploaded = await client.post(
                "/v1/knowledge-bases/kb/documents",
                files={"file": ("a.pdf", b"%PDF-1.4 data", "application/pdf")},
            )
            assert (await _wait_job(client, uploaded.json()["job_id"])).json()[
                "status"
            ] == "succeeded"
            access_store.set_document_policy(
                "default", storage_id, document_id, "a.pdf", policy="private"
            )
            access_store.grant_subject(
                "default", storage_id, "alice", "viewer", document_id=document_id
            )
            monkeypatch.setattr(
                access_store,
                "finish_document_retirement",
                fail_finish,
            )

            first = await client.delete("/v1/knowledge-bases/kb/documents/a.pdf")
            first_done = await _wait_job(client, first.json()["job_id"])
            assert first_done.json()["status"] == "failed"
            assert attempts == 4
            assert access_store.retiring_document_ids(
                "default",
                storage_id,
                f"document-delete:{document_id}",
            ) == (document_id,)

            monkeypatch.setattr(
                access_store,
                "finish_document_retirement",
                original_finish,
            )
            retried = await client.delete("/v1/knowledge-bases/kb/documents/a.pdf")
            retried_done = await _wait_job(client, retried.json()["job_id"])
            assert retried_done.json()["status"] == "succeeded"

    assert (
        access_store.retiring_document_ids(
            "default",
            storage_id,
            f"document-delete:{document_id}",
        )
        == ()
    )
    assert access_store.get_document_policy("default", storage_id, document_id) is None
    assert (
        access_store.list_grants("default", storage_id, document_id=document_id) == []
    )


@pytest.mark.anyio
async def test_failed_document_delete_preserves_policy_and_document_grants(
    tmp_path, monkeypatch
):
    access_store = ResourceAccessStore(tmp_path / "resource-access.db")
    app, _ = _make_app(
        tmp_path,
        monkeypatch=monkeypatch,
        resource_access_store=access_store,
    )
    document_id = build_document_id("ghost.pdf")

    async with app.router.lifespan_context(app):
        async with await _client(app) as client:
            created = await client.post("/v1/knowledge-bases", json={"kb_id": "kb"})
            assert created.status_code == 201
            storage_id = app.state.kb_registry.resolve("kb", "default")["storage_id"]
            access_store.set_document_policy(
                "default", storage_id, document_id, "ghost.pdf", policy="private"
            )
            access_store.grant_subject(
                "default", storage_id, "alice", "viewer", document_id=document_id
            )

            deleted = await client.delete("/v1/knowledge-bases/kb/documents/ghost.pdf")
            deleted_done = await _wait_job(client, deleted.json()["job_id"])

    assert deleted_done.json()["status"] == "failed"
    assert deleted_done.json()["error_code"] == "DOCUMENT_NOT_FOUND"
    preserved = access_store.get_document_policy("default", storage_id, document_id)
    assert preserved is not None
    assert preserved["policy"] == "private"
    assert [
        grant["subject_id"]
        for grant in access_store.list_grants(
            "default", storage_id, document_id=document_id
        )
    ] == ["alice"]


# 验证 ingest failure marks job failed。
@pytest.mark.anyio
async def test_ingest_failure_marks_job_failed(tmp_path, monkeypatch):
    # 模拟失败路径。
    def boom(kb_id, source_dir):
        raise ValueError(f"解析崩了: {kb_id} {source_dir}/private.pdf")

    app, _ = _make_app(tmp_path, ingest_fn=boom, monkeypatch=monkeypatch)
    async with app.router.lifespan_context(app):
        async with await _client(app) as client:
            await client.post("/v1/knowledge-bases", json={"kb_id": "kb"})
            up = await client.post(
                "/v1/knowledge-bases/kb/documents",
                files={"file": ("a.pdf", b"%PDF-1.4 data", "application/pdf")},
            )
            done = await _wait_job(client, up.json()["job_id"])

    assert done.json()["status"] == "failed"
    assert done.json()["error_code"] == "INGEST_FAILED"
    assert done.json()["message"] == "索引任务执行失败"
    assert "private.pdf" not in done.text
    assert str(tmp_path) not in done.text
