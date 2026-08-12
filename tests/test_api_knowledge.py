from contextlib import asynccontextmanager
from threading import Event

import pytest
from httpx import ASGITransport, AsyncClient

from cogdoc.api.app import create_app
from cogdoc.api.derived_knowledge_store import (
    AUTO_REBIND_REVIEW_NOTE,
    DerivedKnowledgeStore,
)
from cogdoc.api.feedback_analysis_store import FeedbackAnalysisStore
from cogdoc.api.feedback_store import FeedbackStore
from cogdoc.api.ingest import KnowledgeBaseRegistry
from cogdoc.api.retrieval_feedback_store import RetrievalFeedbackStore
from cogdoc.api.retrieval_eval_draft_store import RetrievalEvalDraftStore


# 声明异步测试使用的后端。
@pytest.fixture
def anyio_backend():
    return "asyncio"


# 构造应用。
def _make_app(tmp_path, monkeypatch):
    import cogdoc.api.app as app_module

    monkeypatch.setattr(app_module, "configure_logging", lambda: None)
    store = DerivedKnowledgeStore(path=str(tmp_path / "knowledge.jsonl"))
    feedback_analysis_store = FeedbackAnalysisStore(
        path=str(tmp_path / "feedback_analysis.jsonl")
    )
    feedback_store = FeedbackStore(
        feedback_path=str(tmp_path / "feedback.jsonl"),
        bad_cases_path=str(tmp_path / "bad_cases.jsonl"),
    )
    retrieval_feedback_store = RetrievalFeedbackStore(
        path=str(tmp_path / "retrieval_feedback.jsonl")
    )
    registry = KnowledgeBaseRegistry(
        registry_path=str(tmp_path / "registry.json"),
        source_dir_for=lambda kb_id: str(tmp_path / "sources" / kb_id),
    )
    return create_app(
        kb_registry=registry,
        knowledge_store=store,
        feedback_store=feedback_store,
        feedback_analysis_store=feedback_analysis_store,
        retrieval_feedback_store=retrieval_feedback_store,
        retrieval_eval_draft_store=RetrievalEvalDraftStore(
            path=str(tmp_path / "retrieval_eval_drafts.jsonl")
        ),
    )


# 构造测试客户端。
@asynccontextmanager
async def _client(app):
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            yield client


# 验证手工知识生命周期场景。
@pytest.mark.anyio
async def test_manual_knowledge_lifecycle(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)

    async with _client(app) as client:
        create = await client.post(
            "/v1/knowledge",
            json={
                "kb_id": "kb",
                "text": "入职审批需要直属经理确认。",
                "related_source": "hr.pdf",
                "related_source_sha256": "sha-old",
                "related_chunk_ids": ["c1"],
                "related_page_start": 2,
                "related_page_end": 3,
                "related_chunk_text_hash": "hash-old",
                "related_anchor_text": "审批规则",
                "source_note": "HR 手工确认",
                "certainty": "high",
                "created_by": "reviewer",
            },
        )
        assert create.status_code == 201
        row = create.json()["knowledge"]
        assert row["status"] == "pending"
        assert row["origin"] == "manual_entry"
        assert row["related_page_start"] == 2
        assert row["related_page_end"] == 3
        assert row["related_chunk_text_hash"] == "hash-old"
        assert row["related_anchor_text"] == "审批规则"
        # 客户端自报 created_by 不可信；本地无鉴权模式固定归属 local。
        assert row["created_by"] == "local"
        knowledge_id = row["knowledge_id"]

        pending = await client.get(
            "/v1/knowledge", params={"kb_id": "kb", "status": "pending"}
        )
        assert pending.status_code == 200
        assert [item["knowledge_id"] for item in pending.json()["knowledge"]] == [
            knowledge_id
        ]

        approved = await client.post(
            f"/v1/knowledge/{knowledge_id}/approve",
            json={"actor": "admin", "note": "确认有效"},
        )
        assert approved.status_code == 200
        assert approved.json()["status"] == "approved"
        assert approved.json()["reviewed_by"] == "local"

        archived = await client.post(
            f"/v1/knowledge/{knowledge_id}/archive", json={"actor": "admin"}
        )
        assert archived.status_code == 200
        assert archived.json()["status"] == "archived"
        assert archived.json()["archived_at"]


# 验证派生知识可以被硬删除。
@pytest.mark.anyio
async def test_delete_knowledge_removes_history(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)

    async with _client(app) as client:
        created = await client.post(
            "/v1/knowledge",
            json={"kb_id": "kb", "text": "需要删除的知识。"},
        )
        knowledge_id = created.json()["knowledge"]["knowledge_id"]
        deleted = await client.delete(f"/v1/knowledge/{knowledge_id}")
        listed = await client.get("/v1/knowledge", params={"kb_id": "kb"})
        missing = await client.delete(f"/v1/knowledge/{knowledge_id}")

    assert deleted.status_code == 204
    assert listed.json()["knowledge"] == []
    assert missing.status_code == 404


# 验证手动过期扫描会标记旧文档绑定。
@pytest.mark.anyio
async def test_stale_scan_marks_changed_document_bindings(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    app.state.kb_registry.create("kb")
    monkeypatch.setattr(
        "cogdoc.api.routes.knowledge._current_documents",
        lambda kb_id: [{"name": "policy.pdf", "sha256": "sha-new"}],
    )

    async with _client(app) as client:
        created = await client.post(
            "/v1/knowledge",
            json={
                "kb_id": "kb",
                "text": "旧文档绑定的知识。",
                "related_source": "policy.pdf",
                "related_source_sha256": "sha-old",
            },
        )
        knowledge_id = created.json()["knowledge"]["knowledge_id"]
        await client.post(f"/v1/knowledge/{knowledge_id}/approve", json={})
        scanned = await client.post("/v1/knowledge/stale-scan", params={"kb_id": "kb"})
        stale = await client.get(
            "/v1/knowledge", params={"kb_id": "kb", "status": "stale"}
        )

    assert scanned.status_code == 200
    assert scanned.json()["stale_marked"] == 1
    assert stale.json()["knowledge"][0]["knowledge_id"] == knowledge_id
    assert (
        stale.json()["knowledge"][0]["review_note"]
        == "手动扫描发现绑定文档已变化或缺失"
    )


# 验证精确重复返回现有记录场景。
@pytest.mark.anyio
async def test_exact_duplicate_returns_existing_knowledge(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)

    async with _client(app) as client:
        first = await client.post(
            "/v1/knowledge", json={"kb_id": "kb", "text": "A   B"}
        )
        second = await client.post("/v1/knowledge", json={"kb_id": "kb", "text": "A B"})

        assert first.status_code == 201
        assert second.status_code == 201
        assert second.json()["deduplicated"] is True
        assert (
            first.json()["knowledge"]["knowledge_id"]
            == second.json()["knowledge"]["knowledge_id"]
        )


# 验证相似知识会进入冲突组等待审核。
@pytest.mark.anyio
async def test_similar_knowledge_creates_conflict_group(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)

    async with _client(app) as client:
        first = await client.post(
            "/v1/knowledge",
            json={
                "kb_id": "kb",
                "text": "差旅报销需要七天内提交。",
            },
        )
        second = await client.post(
            "/v1/knowledge",
            json={
                "kb_id": "kb",
                "text": "差旅报销需要7天内提交。",
            },
        )
        listed = await client.get("/v1/knowledge", params={"kb_id": "kb"})
        conflict_only = await client.get(
            "/v1/knowledge", params={"kb_id": "kb", "has_conflict": True}
        )
        summary = await client.get("/v1/review-queue", params={"kb_id": "kb"})

    assert first.status_code == 201
    assert second.status_code == 201
    body = second.json()
    assert body["knowledge"]["status"] == "pending"
    assert body["knowledge"]["conflict_group_id"]
    assert body["requires_review"] is True
    assert (
        body["conflicts"][0]["knowledge_id"]
        == first.json()["knowledge"]["knowledge_id"]
    )
    rows = listed.json()["knowledge"]
    groups = {row["knowledge_id"]: row["conflict_group_id"] for row in rows}
    assert len(set(groups.values())) == 1
    conflict_group_id = body["knowledge"]["conflict_group_id"]
    conflict_rows = conflict_only.json()["knowledge"]
    assert {row["knowledge_id"] for row in conflict_rows} == set(groups)

    async with _client(app) as client:
        grouped = await client.get(
            "/v1/knowledge",
            params={"kb_id": "kb", "conflict_group_id": conflict_group_id},
        )

    assert grouped.status_code == 200
    assert {row["knowledge_id"] for row in grouped.json()["knowledge"]} == set(groups)
    assert summary.status_code == 200
    assert summary.json()["knowledge_conflicts"] == {
        "total": 2,
        "groups": 1,
        "pending": 2,
        "stale": 0,
    }


# 验证保存回答来源可以创建待审核知识场景。
@pytest.mark.anyio
async def test_saved_answer_origin_creates_pending_knowledge(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)

    async with _client(app) as client:
        created = await client.post(
            "/v1/knowledge",
            json={
                "kb_id": "kb",
                "text": "系统回答中的高价值结论。",
                "origin": "saved_answer",
                "created_from_trace_id": "trace-1",
                "related_chunk_ids": ["c1", "c2"],
                "source_note": "保存自问答",
            },
        )

        assert created.status_code == 201
        row = created.json()["knowledge"]
        assert row["origin"] == "saved_answer"
        assert row["status"] == "pending"
        assert row["created_from_trace_id"] == "trace-1"
        assert row["related_chunk_ids"] == ["c1", "c2"]


# 验证待审核知识创建会发送回调场景。
@pytest.mark.anyio
async def test_pending_knowledge_creation_emits_webhook(
    tmp_path, monkeypatch, webhook_dispatcher
):
    import cogdoc.api.app as app_module

    monkeypatch.setattr(app_module, "configure_logging", lambda: None)
    app = create_app(
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
        webhook_dispatcher=webhook_dispatcher,
    )

    async with _client(app) as client:
        pending = await client.post(
            "/v1/knowledge", json={"kb_id": "kb", "text": "待审核知识。"}
        )

    assert pending.status_code == 201
    assert [event for event, _ in webhook_dispatcher.events] == [
        "knowledge.pending_created"
    ]
    payload = webhook_dispatcher.events[0][1]
    assert payload["source"] == "knowledge_create"
    assert payload["knowledge"]["status"] == "pending"
    assert payload["knowledge"]["text"] == "待审核知识。"


# 验证过期知识复核通过时刷新绑定场景。
@pytest.mark.anyio
async def test_stale_knowledge_approve_refreshes_binding(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)

    async with _client(app) as client:
        created = await client.post(
            "/v1/knowledge",
            json={
                "kb_id": "kb",
                "text": "旧文档中的规则。",
                "related_document_id": "doc-old",
                "related_source": "policy.pdf",
                "related_source_sha256": "sha-old",
                "related_chunk_ids": ["old-c1"],
                "related_page_start": 1,
                "related_page_end": 1,
                "related_chunk_text_hash": "hash-old",
                "related_anchor_text": "旧锚点",
            },
        )
        knowledge_id = created.json()["knowledge"]["knowledge_id"]
        await client.post(f"/v1/knowledge/{knowledge_id}/approve", json={})
        app.state.knowledge_store.set_status(knowledge_id, "stale")

        approved = await client.post(
            f"/v1/knowledge/{knowledge_id}/approve",
            json={
                "actor": "admin",
                "note": "新版文档确认仍有效",
                "related_document_id": "doc-new",
                "related_source": "policy.pdf",
                "related_source_sha256": "sha-new",
                "related_chunk_ids": ["new-c1", "new-c2"],
                "related_page_start": 4,
                "related_page_end": 5,
                "related_chunk_text_hash": "hash-new",
                "related_anchor_text": "新版锚点",
            },
        )

    assert approved.status_code == 200
    row = approved.json()
    assert row["status"] == "approved"
    assert row["related_document_id"] == "doc-new"
    assert row["related_source_sha256"] == "sha-new"
    assert row["related_chunk_ids"] == ["new-c1", "new-c2"]
    assert row["related_page_start"] == 4
    assert row["related_page_end"] == 5
    assert row["related_chunk_text_hash"] == "hash-new"
    assert row["related_anchor_text"] == "新版锚点"
    assert row["reviewed_by"] == "local"
    assert row["review_note"] == "新版文档确认仍有效"


# 验证绑定更新不会覆盖非绑定字段。
def test_knowledge_binding_updates_are_allowlisted(tmp_path):
    store = DerivedKnowledgeStore(path=str(tmp_path / "knowledge.jsonl"))
    row, _ = store.create(
        {
            "kb_id": "kb",
            "text": "知识",
            "status": "approved",
            "related_source_sha256": "sha-old",
        }
    )

    updated = store.set_status(
        row["knowledge_id"],
        "approved",
        binding_updates={
            "status": "rejected",
            "related_source_sha256": "sha-new",
            "related_page_start": 9,
            "related_chunk_text_hash": "hash-new",
        },
    )

    assert updated["status"] == "approved"
    assert updated["related_source_sha256"] == "sha-new"
    assert updated["related_page_start"] == 9
    assert updated["related_chunk_text_hash"] == "hash-new"


# 验证按日期过滤包含结束日期当天。
def test_knowledge_created_before_includes_whole_day(tmp_path):
    store = DerivedKnowledgeStore(path=str(tmp_path / "knowledge.jsonl"))
    row, _ = store.create({"kb_id": "kb", "text": "知识", "status": "approved"})
    store.set_status(row["knowledge_id"], "approved")

    filtered = store.list(kb_id="kb", created_before=row["created_at"][:10])

    assert [item["knowledge_id"] for item in filtered] == [row["knowledge_id"]]


# 验证知识修订创建新版本且通过后归档旧版本。
@pytest.mark.anyio
async def test_knowledge_revision_supersedes_previous_version(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)

    async with _client(app) as client:
        created = await client.post(
            "/v1/knowledge",
            json={
                "kb_id": "kb",
                "text": "旧规则。",
                "related_source": "policy.pdf",
                "related_page_start": 1,
                "related_page_end": 2,
                "related_anchor_text": "旧规则锚点",
            },
        )
        previous = created.json()["knowledge"]
        await client.post(f"/v1/knowledge/{previous['knowledge_id']}/approve", json={})
        revised = await client.post(
            f"/v1/knowledge/{previous['knowledge_id']}/revise",
            json={
                "text": "新规则。",
                "related_source": "policy-v2.pdf",
                "related_chunk_ids": ["c2"],
                "related_page_start": 5,
                "related_page_end": 6,
                "related_chunk_text_hash": "hash-v2",
                "related_anchor_text": "新规则锚点",
                "source_note": "人工修订",
                "created_by": "admin",
            },
        )
        revision = revised.json()["knowledge"]
        approved = await client.post(
            f"/v1/knowledge/{revision['knowledge_id']}/approve",
            json={"actor": "admin", "note": "新版确认"},
        )
        archived = await client.get(
            "/v1/knowledge", params={"kb_id": "kb", "status": "archived"}
        )

    assert created.status_code == 201
    assert revised.status_code == 201
    assert revision["version"] == 2
    assert revision["previous_version_id"] == previous["knowledge_id"]
    assert revision["status"] == "pending"
    assert revision["related_source"] == "policy-v2.pdf"
    assert revision["related_chunk_ids"] == ["c2"]
    assert revision["related_page_start"] == 5
    assert revision["related_page_end"] == 6
    assert revision["related_chunk_text_hash"] == "hash-v2"
    assert revision["related_anchor_text"] == "新规则锚点"
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"
    archived_rows = archived.json()["knowledge"]
    assert [row["knowledge_id"] for row in archived_rows] == [previous["knowledge_id"]]
    assert archived_rows[0]["review_note"].startswith("由新版本 ")


# 验证审核通过修订版本会归档旧版本。
@pytest.mark.anyio
async def test_knowledge_revision_approval_archives_previous(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)

    async with _client(app) as client:
        created = await client.post(
            "/v1/knowledge",
            json={"kb_id": "kb", "text": "旧规则。"},
        )
        previous = created.json()["knowledge"]
        await client.post(f"/v1/knowledge/{previous['knowledge_id']}/approve", json={})
        revised = await client.post(
            f"/v1/knowledge/{previous['knowledge_id']}/revise",
            json={
                "text": "新规则。",
                "created_by": "admin",
            },
        )
        revision_id = revised.json()["knowledge"]["knowledge_id"]
        approved = await client.post(f"/v1/knowledge/{revision_id}/approve", json={})
        archived = await client.get(
            "/v1/knowledge", params={"kb_id": "kb", "status": "archived"}
        )

    assert revised.status_code == 201
    assert revised.json()["knowledge"]["status"] == "pending"
    assert approved.json()["status"] == "approved"
    archived_rows = archived.json()["knowledge"]
    assert [row["knowledge_id"] for row in archived_rows] == [previous["knowledge_id"]]


# 验证待审核和驳回知识不能修订。
@pytest.mark.anyio
async def test_knowledge_revision_rejects_non_reviewed_statuses(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)

    async with _client(app) as client:
        pending = await client.post(
            "/v1/knowledge", json={"kb_id": "kb", "text": "待审核知识。"}
        )
        pending_id = pending.json()["knowledge"]["knowledge_id"]
        pending_revision = await client.post(
            f"/v1/knowledge/{pending_id}/revise", json={"text": "新知识。"}
        )
        await client.post(f"/v1/knowledge/{pending_id}/reject", json={})
        rejected_revision = await client.post(
            f"/v1/knowledge/{pending_id}/revise", json={"text": "新知识。"}
        )

    assert pending_revision.status_code == 400
    assert rejected_revision.status_code == 400


# 验证知识修订拒绝活跃重复文本。
@pytest.mark.anyio
async def test_knowledge_revision_rejects_duplicate_active_text(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)

    async with _client(app) as client:
        first = await client.post(
            "/v1/knowledge",
            json={"kb_id": "kb", "text": "第一条。"},
        )
        second = await client.post(
            "/v1/knowledge",
            json={"kb_id": "kb", "text": "第二条。"},
        )
        knowledge_id = first.json()["knowledge"]["knowledge_id"]
        await client.post(f"/v1/knowledge/{knowledge_id}/approve", json={})
        await client.post(
            f"/v1/knowledge/{second.json()['knowledge']['knowledge_id']}/approve",
            json={},
        )
        duplicate = await client.post(
            f"/v1/knowledge/{knowledge_id}/revise", json={"text": "第二条。"}
        )

    assert duplicate.status_code == 400


# 验证审核队列摘要聚合多类待处理事项场景。
@pytest.mark.anyio
async def test_review_queue_summary_counts_pending_work(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)

    app.state.feedback_analysis_store.record(
        "fb1",
        {"kb_id": "kb", "trace_id": "t1", "query": "问题"},
        {
            "feedback_type": "correction",
            "sentiment": "negative",
            "target": {"chunk_ids": [], "sources": [], "source_type": "none"},
            "extracted_claim": "正确说法",
            "recommended_action": "create_pending_knowledge",
            "weight_delta": -0.55,
            "confidence": 0.72,
            "needs_review": True,
        },
    )
    app.state.feedback_store.record(
        {
            "kb_id": "kb",
            "trace_id": "t0",
            "query": "问题",
            "feedback": "thumbs_down",
        }
    )
    app.state.retrieval_feedback_store.record_from_feedback(
        "fb2",
        {
            "kb_id": "kb",
            "query": "问题",
            "feedback": "thumbs_down",
            "feedback_type": "bad_retrieval",
            "citations": [{"chunk_id": "c1", "source": "a.pdf"}],
        },
    )

    async with _client(app) as client:
        await client.post(
            "/v1/knowledge",
            json={"kb_id": "kb", "text": "待审核知识。"},
        )
        saved = await client.post(
            "/v1/knowledge",
            json={
                "kb_id": "kb",
                "text": "保存答案知识。",
                "origin": "saved_answer",
            },
        )
        await client.post(
            f"/v1/knowledge/{saved.json()['knowledge']['knowledge_id']}/approve",
            json={},
        )
        auto = await client.post(
            "/v1/knowledge",
            json={
                "kb_id": "kb",
                "text": "自动重绑知识。",
            },
        )
        app.state.knowledge_store.set_status(
            auto.json()["knowledge"]["knowledge_id"],
            "approved",
            actor="system",
            note=AUTO_REBIND_REVIEW_NOTE,
        )
        summary = await client.get("/v1/review-queue", params={"kb_id": "kb"})
        export = await client.get(
            "/v1/review-queue/export",
            params={
                "kb_id": "kb",
                "knowledge_origin": "manual_entry",
                "limit": 50,
            },
        )

    assert summary.status_code == 200
    body = summary.json()
    assert body["knowledge"]["pending"] == 1
    assert body["knowledge"]["approved"] == 2
    assert body["knowledge_origin"]["saved_answer"] == 1
    assert body["knowledge_auto_review"]["auto_rebound"] == 1
    assert body["knowledge_auto_review"]["stale_pending"] == 0
    assert body["feedback_counts"]["total"] == 1
    assert body["feedback_counts"]["bad_cases"] == 1
    assert body["feedback_analysis"]["create_pending_knowledge"] == 1
    assert body["feedback_analysis"]["needs_review"] == 1
    assert body["retrieval_feedback"]["enabled"] == 1
    assert export.status_code == 200
    exported = export.json()
    assert exported["kb_id"] == "kb"
    assert exported["summary"]["feedback_counts"]["bad_cases"] == 1
    assert exported["summary"]["knowledge_origin"]["manual_entry"] == 2
    assert exported["summary"]["knowledge_auto_review"]["auto_rebound"] == 1
    assert len(exported["pending_knowledge"]) == 1
    assert exported["pending_knowledge"][0]["text"] == "待审核知识。"
    assert exported["stale_knowledge"] == []
    assert len(exported["auto_review_events"]) == 1
    assert exported["auto_review_events"][0]["review_note"] == AUTO_REBIND_REVIEW_NOTE
    assert len(exported["feedback_analysis_needs_review"]) == 1
    assert len(exported["retrieval_feedback_enabled"]) == 1
    assert len(exported["feedback_bad_cases"]) == 1


# 验证待审核计数和反馈闭环指标场景。
@pytest.mark.anyio
async def test_review_metrics_endpoints_report_feedback_loop(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)

    app.state.feedback_store.record(
        {
            "kb_id": "kb",
            "trace_id": "t1",
            "feedback": "thumbs_down",
            "feedback_type": "no_evidence",
        }
    )
    app.state.feedback_store.record(
        {"kb_id": "kb", "trace_id": "t2", "feedback": "correction"}
    )
    app.state.feedback_analysis_store.record(
        "fb1",
        {"kb_id": "kb", "trace_id": "t2", "query": "问题"},
        {
            "feedback_type": "correction",
            "sentiment": "negative",
            "target": {"chunk_ids": [], "sources": [], "source_type": "none"},
            "extracted_claim": "正确说法",
            "recommended_action": "create_pending_knowledge",
            "weight_delta": -0.55,
            "confidence": 0.9,
            "needs_review": True,
        },
    )
    records = app.state.retrieval_feedback_store.record_from_feedback(
        "fb2",
        {
            "kb_id": "kb",
            "query": "问题",
            "feedback": "thumbs_down",
            "feedback_type": "bad_retrieval",
            "citations": [{"chunk_id": "c1", "source": "a.pdf"}],
        },
    )
    app.state.retrieval_feedback_store.set_enabled(
        records[0]["retrieval_feedback_id"], False
    )

    async with _client(app) as client:
        approved = await client.post(
            "/v1/knowledge",
            json={"kb_id": "kb", "text": "已通过知识。"},
        )
        await client.post(
            f"/v1/knowledge/{approved.json()['knowledge']['knowledge_id']}/approve",
            json={},
        )
        rejected = await client.post(
            "/v1/knowledge", json={"kb_id": "kb", "text": "驳回知识。"}
        )
        await client.post(
            f"/v1/knowledge/{rejected.json()['knowledge']['knowledge_id']}/reject",
            json={},
        )
        stale_id = approved.json()["knowledge"]["knowledge_id"]
        app.state.knowledge_store.set_status(stale_id, "stale")
        pending_count = await client.get(
            "/v1/knowledge/pending-count", params={"kb_id": "kb"}
        )
        metrics = await client.get(
            "/v1/feedback-loop-metrics",
            params={"kb_id": "kb", "answer_count": 4},
        )
        app.state.knowledge_store.set_status(stale_id, "approved")
        reviewed_metrics = await client.get(
            "/v1/feedback-loop-metrics", params={"kb_id": "kb"}
        )

    assert pending_count.status_code == 200
    assert pending_count.json()["stale"] == 1
    assert pending_count.json()["feedback_analysis_needs_review"] == 1
    assert pending_count.json()["total"] == 1
    assert metrics.status_code == 200
    body = metrics.json()
    assert body["counts"]["feedback_total"] == 2
    assert body["counts"]["negative_feedback_total"] == 2
    assert body["counts"]["no_evidence_feedback_total"] == 1
    assert body["rates"]["feedback_rate"] == 0.5
    assert body["rates"]["negative_feedback_rate"] == 0.5
    assert body["rates"]["no_evidence_rate"] == 0.25
    assert body["rates"]["pending_rejection_rate"] == 0.5
    assert body["rates"]["feedback_to_pending_rate"] == 1.0
    assert body["rates"]["retrieval_feedback_rollback_rate"] == 1.0
    assert body["rates"]["stale_review_completion_rate"] == 0.0
    assert reviewed_metrics.json()["rates"]["stale_review_completion_rate"] == 1.0


# 验证删除待审核派生知识后徽标计数下降且不受反馈分析影响。
@pytest.mark.anyio
async def test_pending_count_decrements_after_knowledge_delete(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    app.state.feedback_analysis_store.record(
        "fb1",
        {"kb_id": "kb", "trace_id": "t1", "query": "问题"},
        {
            "feedback_type": "correction",
            "sentiment": "negative",
            "target": {"chunk_ids": [], "sources": [], "source_type": "none"},
            "extracted_claim": "正确说法",
            "recommended_action": "create_pending_knowledge",
            "weight_delta": 0,
            "confidence": 0.9,
            "needs_review": True,
        },
    )

    async with _client(app) as client:
        created = await client.post(
            "/v1/knowledge", json={"kb_id": "kb", "text": "待删除派生知识。"}
        )
        knowledge_id = created.json()["knowledge"]["knowledge_id"]
        before = await client.get("/v1/knowledge/pending-count", params={"kb_id": "kb"})
        deleted = await client.delete(f"/v1/knowledge/{knowledge_id}")
        after = await client.get("/v1/knowledge/pending-count", params={"kb_id": "kb"})

    assert before.json()["pending"] == 1
    assert before.json()["feedback_analysis_needs_review"] == 1
    assert before.json()["total"] == 1
    assert deleted.status_code == 204
    assert after.json()["pending"] == 0
    assert after.json()["feedback_analysis_needs_review"] == 1
    assert after.json()["total"] == 0


# 验证反馈闭环指标使用服务端回答数兜底场景。
@pytest.mark.anyio
async def test_feedback_loop_metrics_uses_session_answer_count(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    app.state.session_store.record(
        "kb",
        "s1",
        [],
        [
            {"role": "user", "content": "问题一"},
            {"role": "assistant", "content": "回答一"},
            {"role": "user", "content": "问题二"},
            {"role": "assistant", "content": "回答二"},
        ],
    )
    app.state.feedback_store.record(
        {"kb_id": "kb", "trace_id": "t1", "feedback": "thumbs_down"}
    )

    async with _client(app) as client:
        metrics = await client.get("/v1/feedback-loop-metrics", params={"kb_id": "kb"})

    assert metrics.status_code == 200
    body = metrics.json()
    assert body["counts"]["answer_total"] == 2
    assert body["rates"]["feedback_rate"] == 0.5
    assert body["rates"]["negative_feedback_rate"] == 0.5


# 验证审核通过后投递派生知识索引刷新场景。
@pytest.mark.anyio
async def test_knowledge_review_queues_index_refresh(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)
    refreshed = []
    done = Event()

    def fake_refresher(kb_id, store):
        refreshed.append((kb_id, store))
        done.set()

    app.state.derived_knowledge_index_auto_refresh = True
    app.state.derived_knowledge_index_refresher = fake_refresher

    async with _client(app) as client:
        created = await client.post(
            "/v1/knowledge",
            json={"kb_id": "kb", "text": "需要审核的知识。"},
        )
        knowledge_id = created.json()["knowledge"]["knowledge_id"]
        approved = await client.post(f"/v1/knowledge/{knowledge_id}/approve", json={})

        assert approved.status_code == 200
        assert done.wait(1.0)

    assert refreshed == [("kb", app.state.knowledge_store)]


# 验证派生知识索引状态接口场景。
@pytest.mark.anyio
async def test_knowledge_index_status_endpoint(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)

    def fake_statuser(kb_id, store):
        return {
            "kb_id": kb_id,
            "state": "fresh",
            "approved_count": 2,
            "indexed_count": 2,
            "collection_name": "dk-test",
        }

    app.state.derived_knowledge_index_auto_refresh = True
    app.state.derived_knowledge_index_statuser = fake_statuser

    async with _client(app) as client:
        status = await client.get("/v1/knowledge/index-status", params={"kb_id": "kb"})

    assert status.status_code == 200
    body = status.json()
    assert body["kb_id"] == "kb"
    assert body["state"] == "fresh"
    assert body["approved_count"] == 2
    assert body["indexed_count"] == 2
    assert body["auto_refresh_enabled"] is True


# 验证批量审核报告缺失标识场景。
@pytest.mark.anyio
async def test_batch_review_reports_missing_ids(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)

    async with _client(app) as client:
        created = await client.post(
            "/v1/knowledge", json={"kb_id": "kb", "text": "知识 A"}
        )
        knowledge_id = created.json()["knowledge"]["knowledge_id"]

        batch = await client.post(
            "/v1/knowledge/batch-approve",
            json={"knowledge_ids": [knowledge_id, "missing"], "actor": "admin"},
        )

        assert batch.status_code == 200
        body = batch.json()
        assert [item["knowledge_id"] for item in body["updated"]] == [knowledge_id]
        assert body["missing_ids"] == ["missing"]


# 验证批量审核拒绝绑定字段场景。
@pytest.mark.anyio
async def test_batch_review_rejects_binding_fields(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch)

    async with _client(app) as client:
        created = await client.post(
            "/v1/knowledge", json={"kb_id": "kb", "text": "知识 A"}
        )
        knowledge_id = created.json()["knowledge"]["knowledge_id"]

        batch = await client.post(
            "/v1/knowledge/batch-approve",
            json={
                "knowledge_ids": [knowledge_id],
                "actor": "admin",
                "related_source_sha256": "sha-new",
            },
        )

    assert batch.status_code == 422
