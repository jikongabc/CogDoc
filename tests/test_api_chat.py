import asyncio
import threading
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.requests import Request
from cogdoc.api.app import create_app
from cogdoc.api.persistence import SqliteSessionStore
from cogdoc.api.routes.chat import _event_to_frame, chat_stream
from cogdoc.api.schemas import ChatRequest
from cogdoc.api.session_store import SessionStore
from cogdoc.memory.manager import MemoryPolicy
from cogdoc.service.chat_service import ChatEvent, ChatResult, ChatServiceError


# 声明异步测试使用的后端。
@pytest.fixture
def anyio_backend():
    return "asyncio"


# 发送chat。
async def _post_chat(app, payload: dict):
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            return await client.post("/v1/chat", json=payload)


# 构造测试结果。
def _result(answer: str, trace_id: str, messages=None) -> ChatResult:
    return ChatResult(
        answer=answer,
        task_type="qa",
        citations=[{"chunk_id": "chunk-1", "source": "a.pdf", "page": 1}],
        evidence=[
            {
                "chunk_id": "chunk-1",
                "chunk_index": 0,
                "source": "a.pdf",
                "page": 1,
                "text_preview": "证据预览",
            }
        ],
        critique="",
        is_valid=True,
        trace_id=trace_id,
        request_id=trace_id,
        steps=[],
        chat_messages=messages
        or [
            {"role": "user", "content": "问题", "timestamp": None},
            {"role": "assistant", "content": answer, "timestamp": None},
        ],
        raw_output={"answer": answer},
    )


# 证据校验进度通过统一 node SSE 帧暴露给瘦客户端。
def test_evidence_rejected_event_maps_to_node_sse_frame():
    frame = _event_to_frame(
        ChatEvent(
            "evidence_rejected",
            {"supported": False, "reason": "缺少明确事实"},
        ),
        doc_id="kb",
        session_id="s1",
    )

    assert frame is not None
    assert "event: node" in frame
    assert '"stage": "evidence_rejected"' in frame
    assert '"supported": false' in frame


# 验证自适应补检索进度不会在 SSE 转换层被静默丢弃。
def test_retrieval_retry_event_maps_to_node_sse_frame():
    frame = _event_to_frame(
        ChatEvent(
            "retrieval_retry",
            {
                "retry_count": 1,
                "reason": "missing_requirements",
                "missing_requirement_ids": ["r2"],
            },
        ),
        doc_id="kb",
        session_id="s1",
    )

    assert frame is not None
    assert "event: node" in frame
    assert '"stage": "retrieval_retry"' in frame
    assert '"retry_count": 1' in frame


# 已完成安全校验的长回答按小块交付，避免客户端只能在末尾一次性显示全文。
def test_finalized_token_is_split_into_incremental_sse_frames():
    frame = _event_to_frame(
        ChatEvent("token", {"content": "123456789012ABCDEFGHIJKL"}),
        doc_id="kb",
        session_id="s1",
    )

    assert frame is not None
    assert frame.count("event: token") == 2
    assert '"content": "123456789012"' in frame
    assert '"content": "ABCDEFGHIJKL"' in frame


def test_empty_token_does_not_emit_a_placeholder_sse_frame():
    frame = _event_to_frame(
        ChatEvent("token", {"content": ""}),
        doc_id="kb",
        session_id="s1",
    )

    assert frame is None


# 验证 chat endpoint maps response and trace header 场景。
@pytest.mark.anyio
async def test_chat_endpoint_maps_response_and_trace_header(monkeypatch):
    import cogdoc.api.app as app_module

    monkeypatch.setattr(app_module, "configure_logging", lambda: None)
    calls = []

    # 构造运行器。
    def fake_runner(doc_id, query, is_local, chat_history, forced_task):
        calls.append(
            {
                "doc_id": doc_id,
                "query": query,
                "is_local": is_local,
                "chat_history": chat_history,
                "forced_task": forced_task,
            }
        )
        return _result("答案", "trace-sync")

    app = create_app(chat_runner=fake_runner, session_store=SessionStore())

    response = await _post_chat(
        app,
        {
            "query": "  问题  ",
            "doc_id": "kb",
            "session_id": "s1",
            "mode": "summary",
            "is_local": True,
        },
    )

    assert response.status_code == 200
    assert response.headers["X-Trace-Id"] == "trace-sync"
    payload = response.json()
    assert payload["schema_version"] == "v1"
    assert payload["doc_id"] == "kb"
    assert payload["session_id"] == "s1"
    assert payload["answer"] == "答案"
    assert payload["citations"][0]["source"] == "a.pdf"
    assert calls == [
        {
            "doc_id": "kb",
            "query": "问题",
            "is_local": True,
            "chat_history": [],
            "forced_task": "summary",
        }
    ]


# 验证 chat endpoint reuses session history 场景。
@pytest.mark.anyio
async def test_chat_endpoint_reuses_session_history(monkeypatch):
    import cogdoc.api.app as app_module

    monkeypatch.setattr(app_module, "configure_logging", lambda: None)
    history_lengths = []

    # 构造运行器。
    def fake_runner(doc_id, query, is_local, chat_history, forced_task):
        history_lengths.append(len(chat_history))
        return _result(f"history={len(chat_history)}", f"trace-{len(history_lengths)}")

    app = create_app(chat_runner=fake_runner, session_store=SessionStore())

    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            first = await client.post(
                "/v1/chat",
                json={"query": "第一问", "doc_id": "kb", "session_id": "s1"},
            )
            second = await client.post(
                "/v1/chat",
                json={"query": "第二问", "doc_id": "kb", "session_id": "s1"},
            )

    assert first.status_code == 200
    assert second.status_code == 200
    assert history_lengths == [0, 2]
    assert second.json()["answer"] == "history=2"


# 验证聊天入口把当前问题传给记忆召回器。
@pytest.mark.anyio
async def test_chat_endpoint_uses_query_aware_memory_retrieval(monkeypatch):
    import cogdoc.api.app as app_module

    monkeypatch.setattr(app_module, "configure_logging", lambda: None)
    captured_history = []

    # 捕获送入运行器的记忆上下文。
    def fake_runner(doc_id, query, is_local, chat_history, forced_task):
        captured_history.extend(chat_history)
        return _result("完成", "trace-memory")

    policy = MemoryPolicy(
        context_long_term_limit=1,
        memory_semantic_enabled=False,
        memory_retrieval_mid_limit=0,
    )
    store = SessionStore(memory_policy=policy)
    store.record(
        "kb", "source", [], [{"role": "user", "content": "请记住：默认使用中文"}]
    )
    store.record(
        "kb",
        "source",
        [],
        [{"role": "user", "content": "我偏好 PostgreSQL 数据库"}],
    )
    app = create_app(chat_runner=fake_runner, session_store=store)

    response = await _post_chat(
        app,
        {"query": "PostgreSQL 怎么配置", "doc_id": "kb", "session_id": "target"},
    )

    assert response.status_code == 200
    assert "PostgreSQL" in captured_history[0]["content"]
    assert "默认使用中文" not in captured_history[0]["content"]


# 验证 session history endpoint returns stored turns 场景。
@pytest.mark.anyio
async def test_session_history_endpoint_returns_stored_turns(monkeypatch):
    import cogdoc.api.app as app_module

    monkeypatch.setattr(app_module, "configure_logging", lambda: None)

    # 构造运行器。
    def fake_runner(doc_id, query, is_local, chat_history, forced_task):
        return _result("答案", "trace-h")

    store = SessionStore()
    app = create_app(chat_runner=fake_runner, session_store=store)

    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            await client.post(
                "/v1/chat", json={"query": "问题", "doc_id": "kb", "session_id": "s1"}
            )
            history = await client.get(
                "/v1/sessions/s1/history", params={"doc_id": "kb"}
            )
            empty = await client.get(
                "/v1/sessions/other/history", params={"doc_id": "kb"}
            )

    assert history.status_code == 200
    body = history.json()
    assert body["session_id"] == "s1" and body["doc_id"] == "kb"
    # 刷新后能拉回多轮（user + assistant）。
    assert [m["role"] for m in body["messages"]] == ["user", "assistant"]
    assert body["messages"][1]["trace_id"] == "trace-h"
    assert body["messages"][1]["query"] == "问题"
    assert empty.json()["messages"] == []


# 验证 list and delete sessions 场景。
@pytest.mark.anyio
async def test_list_and_delete_sessions(monkeypatch):
    import cogdoc.api.app as app_module

    monkeypatch.setattr(app_module, "configure_logging", lambda: None)

    # 构造运行器。
    def fake_runner(doc_id, query, is_local, chat_history, forced_task):
        return _result(
            "答案",
            "trace-x",
            messages=[
                {"role": "user", "content": query, "timestamp": None},
                {"role": "assistant", "content": "答案", "timestamp": None},
            ],
        )

    app = create_app(chat_runner=fake_runner, session_store=SessionStore())
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            await client.post(
                "/v1/chat", json={"query": "第一问", "doc_id": "kb", "session_id": "s1"}
            )
            await client.post(
                "/v1/chat", json={"query": "另一问", "doc_id": "kb", "session_id": "s2"}
            )
            listed = await client.get("/v1/sessions", params={"doc_id": "kb"})
            deleted = await client.delete("/v1/sessions/s1", params={"doc_id": "kb"})
            after = await client.get("/v1/sessions", params={"doc_id": "kb"})

    sessions = listed.json()["sessions"]
    assert {s["session_id"] for s in sessions} == {"s1", "s2"}
    # title 取首条用户消息。
    assert any(s["title"] == "第一问" for s in sessions)
    assert deleted.status_code == 204
    assert {s["session_id"] for s in after.json()["sessions"]} == {"s2"}


# 验证 chat endpoint offloads runner 场景。
@pytest.mark.anyio
async def test_chat_endpoint_offloads_runner(monkeypatch):
    import cogdoc.api.app as app_module

    monkeypatch.setattr(app_module, "configure_logging", lambda: None)
    caller_thread_id = threading.get_ident()
    runner_thread_ids = []

    # 构造运行器。
    def fake_runner(doc_id, query, is_local, chat_history, forced_task):
        runner_thread_ids.append(threading.get_ident())
        return _result("答案", "trace-offload")

    app = create_app(chat_runner=fake_runner, session_store=SessionStore())

    response = await _post_chat(app, {"query": "问题", "doc_id": "kb"})

    assert response.status_code == 200
    assert runner_thread_ids
    assert runner_thread_ids[0] != caller_thread_id


# 验证 chat stream emits sse frames and writes session 场景。
@pytest.mark.anyio
async def test_chat_stream_emits_sse_frames_and_writes_session(monkeypatch):
    import cogdoc.api.app as app_module

    monkeypatch.setattr(app_module, "configure_logging", lambda: None)
    result = _result("最终答案", "trace-sse")

    # 构造流式响应。
    def fake_stream(doc_id, query, is_local, chat_history, forced_task):
        yield ChatEvent("request_started", {"trace_id": "trace-sse", "doc_id": doc_id})
        yield ChatEvent("token", {"content": "最终"})
        yield ChatEvent("final", {"result": result, "output": result.raw_output})

    store = SessionStore()
    app = create_app(chat_stream_runner=fake_stream, session_store=store)

    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            resp = await client.post(
                "/v1/chat/stream",
                json={"query": "问题", "doc_id": "kb", "session_id": "s1"},
            )

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    body = resp.text
    assert "event: start" in body and "trace-sse" in body
    assert "event: token" in body
    assert "event: final" in body
    assert '"citations"' in body
    # 结构化 final，不泄漏 raw_output。
    assert "raw_output" not in body
    assert store.get_history("kb", "s1") == result.chat_messages
    display = store.get_display("kb", "s1")
    assert display[1]["trace_id"] == "trace-sse"
    assert display[1]["query"] == "问题"


@pytest.mark.anyio
async def test_chat_stream_watchdog_recovers_lost_threadsafe_wakeup(monkeypatch):
    """A completed producer must not depend on one selector wakeup succeeding."""

    import cogdoc.api.app as app_module
    import cogdoc.api.routes.chat as chat_module

    monkeypatch.setattr(app_module, "configure_logging", lambda: None)
    result = _result("最终答案", "trace-watchdog")

    def fake_stream(doc_id, query, is_local, chat_history, forced_task):
        yield ChatEvent("final", {"result": result, "output": result.raw_output})

    app = create_app(chat_stream_runner=fake_stream, session_store=SessionStore())
    loop = asyncio.get_running_loop()
    # Append callbacks to the ready queue without writing the selector self-pipe.
    # The route's short timer must wake the loop and drain those callbacks.
    monkeypatch.setattr(loop, "call_soon_threadsafe", loop.call_soon)
    monkeypatch.setattr(chat_module, "_STREAM_QUEUE_WATCHDOG_SECONDS", 0.01)

    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            response = await asyncio.wait_for(
                client.post(
                    "/v1/chat/stream",
                    json={"query": "问题", "doc_id": "kb", "session_id": "s1"},
                ),
                timeout=2.0,
            )

    assert response.status_code == 200
    assert "event: final" in response.text
    assert "trace-watchdog" in response.text


@pytest.mark.anyio
async def test_chat_stream_workers_do_not_starve_live_authorization(monkeypatch):
    """Two long-lived producers must not occupy the control-plane pool."""

    import cogdoc.api.app as app_module
    import cogdoc.api.routes.chat as chat_module

    monkeypatch.setattr(app_module, "configure_logging", lambda: None)
    barrier = threading.Barrier(2)
    result = _result("并发答案", "trace-concurrent")

    class Coordinator:
        def stream(self, **_kwargs):
            yield ChatEvent("request_started", {"trace_id": "trace-concurrent"})
            barrier.wait(1)
            yield ChatEvent("final", {"result": result, "output": result.raw_output})

    def guard():
        return None

    monkeypatch.setattr(chat_module, "capture_ha_chat_epoch", lambda *_args: 1)
    monkeypatch.setattr(chat_module, "ha_chat_authority_guard", lambda *_a, **_k: guard)
    app = create_app(
        session_store=SessionStore(),
        offload_workers=1,
        chat_stream_workers=2,
    )
    app.state.ha_chat_coordinator = Coordinator()

    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            first, second = await asyncio.wait_for(
                asyncio.gather(
                    client.post(
                        "/v1/chat/stream",
                        json={"query": "一", "doc_id": "kb", "session_id": "s1"},
                    ),
                    client.post(
                        "/v1/chat/stream",
                        json={"query": "二", "doc_id": "kb", "session_id": "s2"},
                    ),
                ),
                timeout=3,
            )

    assert first.status_code == second.status_code == 200
    assert "event: final" in first.text
    assert "event: final" in second.text
    assert app.state.chat_stream_executor_shutdown is True


@pytest.mark.anyio
async def test_ha_stream_rechecks_authority_at_network_release(monkeypatch):
    import cogdoc.api.app as app_module
    import cogdoc.api.routes.chat as chat_module

    monkeypatch.setattr(app_module, "configure_logging", lambda: None)
    allowed = [True]
    result = _result("不得提交", "trace-revoked")

    class Coordinator:
        def stream(self, **_kwargs):
            yield ChatEvent("request_started", {"trace_id": "trace-revoked"})
            yield ChatEvent("token", {"content": "SECRET-AFTER-REVOKE"})
            yield ChatEvent("final", {"result": result, "output": result.raw_output})

    def guard():
        if not allowed[0]:
            raise PermissionError("revoked")

    monkeypatch.setattr(chat_module, "capture_ha_chat_epoch", lambda *_args: 1)
    monkeypatch.setattr(chat_module, "ha_chat_authority_guard", lambda *_a, **_k: guard)
    app = create_app(session_store=SessionStore())
    app.state.ha_chat_coordinator = Coordinator()

    async with app.router.lifespan_context(app):
        request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/v1/chat/stream",
                "query_string": b"",
                "headers": [],
                "app": app,
                "state": {},
            }
        )
        response = await chat_stream(
            ChatRequest(query="问题", doc_id="kb", session_id="s1"), request
        )
        iterator = response.body_iterator
        first = await anext(iterator)
        assert "event: start" in first
        allowed[0] = False
        second = await anext(iterator)
        assert "event: error" in second
        assert "SECRET-AFTER-REVOKE" not in second
        await iterator.aclose()


@pytest.mark.anyio
async def test_stream_disconnect_does_not_advance_or_record_provider(monkeypatch):
    import cogdoc.api.app as app_module

    monkeypatch.setattr(app_module, "configure_logging", lambda: None)
    advanced_after_disconnect = threading.Event()
    result = _result("幽灵答案", "trace-ghost")

    def stream_runner(*_args, **_kwargs):
        yield ChatEvent("request_started", {"trace_id": "trace-ghost"})
        advanced_after_disconnect.set()
        yield ChatEvent("final", {"result": result, "output": result.raw_output})

    store = SessionStore()
    app = create_app(chat_stream_runner=stream_runner, session_store=store)
    async with app.router.lifespan_context(app):
        request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/v1/chat/stream",
                "query_string": b"",
                "headers": [],
                "app": app,
                "state": {},
            }
        )
        response = await chat_stream(
            ChatRequest(query="问题", doc_id="kb", session_id="s1"), request
        )
        iterator = response.body_iterator
        assert "event: start" in await anext(iterator)
        await iterator.aclose()
        await asyncio.sleep(0.1)

    assert not advanced_after_disconnect.is_set()
    assert store.get_display("kb", "s1") == []


@pytest.mark.anyio
async def test_chat_stream_idle_timeout_emits_error_and_ends(monkeypatch):
    import cogdoc.api.app as app_module
    import cogdoc.api.routes.chat as chat_module

    monkeypatch.setattr(app_module, "configure_logging", lambda: None)
    monkeypatch.setattr(
        chat_module,
        "get_settings",
        lambda: SimpleNamespace(cogdoc_chat_stream_idle_timeout_seconds=0.05),
    )
    result = _result("过晚答案", "trace-too-late")

    def slow_stream(doc_id, query, is_local, chat_history, forced_task):
        yield ChatEvent("request_started", {"trace_id": "slow", "doc_id": doc_id})
        threading.Event().wait(0.2)
        yield ChatEvent("final", {"result": result, "output": result.raw_output})

    app = create_app(chat_stream_runner=slow_stream, session_store=SessionStore())
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            response = await asyncio.wait_for(
                client.post(
                    "/v1/chat/stream",
                    json={"query": "问题", "doc_id": "kb", "session_id": "s1"},
                ),
                timeout=2.0,
            )

    assert response.status_code == 200
    assert "event: start" in response.text
    assert "event: error" in response.text
    assert "STREAM_INTERRUPTED" in response.text
    assert "event: final" not in response.text


# 验证注入流式 runner 的畸形 audit 不会在 final 帧前被指标/摘要转换打断。
@pytest.mark.anyio
async def test_malformed_claim_audit_does_not_break_stream_final(monkeypatch):
    import cogdoc.api.app as app_module

    monkeypatch.setattr(app_module, "configure_logging", lambda: None)
    result = _result("最终答案", "trace-malformed-sse")
    result.raw_output["claim_audit"] = {
        "status": "failed",
        "counts": {"claim_count": "bad", "supported": float("inf")},
        "metrics": {"claim_support_rate": float("nan")},
        "repair": {"attempt_count": "bad"},
        "verifier": {"duration_ms": "bad"},
    }

    def fake_stream(doc_id, query, is_local, chat_history, forced_task):
        yield ChatEvent(
            "request_started", {"trace_id": result.trace_id, "doc_id": doc_id}
        )
        yield ChatEvent("final", {"result": result, "output": result.raw_output})

    store = SessionStore()
    app = create_app(chat_stream_runner=fake_stream, session_store=store)
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            response = await client.post(
                "/v1/chat/stream",
                json={"query": "问题", "doc_id": "kb", "session_id": "s1"},
            )

    assert response.status_code == 200
    assert "event: final" in response.text
    assert '"claim_count": 0' in response.text
    assert '"duration_ms": null' in response.text
    assert store.get_history("kb", "s1") == result.chat_messages


# 验证 chat stream maps error event and skips session 场景。
@pytest.mark.anyio
async def test_chat_stream_maps_error_event_and_skips_session(monkeypatch):
    import cogdoc.api.app as app_module

    monkeypatch.setattr(app_module, "configure_logging", lambda: None)

    # 构造流式响应。
    def fake_stream(doc_id, query, is_local, chat_history, forced_task):
        yield ChatEvent("request_started", {"trace_id": "trace-err", "doc_id": doc_id})
        yield ChatEvent(
            "error",
            {
                "error_class": "TimeoutError",
                "message": "流中断",
                "stage": "stream",
                "trace_id": "trace-err",
            },
        )

    store = SessionStore()
    app = create_app(chat_stream_runner=fake_stream, session_store=store)

    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            resp = await client.post(
                "/v1/chat/stream",
                json={"query": "问题", "doc_id": "kb", "session_id": "s1"},
            )

    body = resp.text
    assert "event: error" in body
    assert "STREAM_INTERRUPTED" in body
    assert store.get_history("kb", "s1") == []


# 验证 session store uses doc id in key and evicts oldest 场景。
def test_session_store_uses_doc_id_in_key_and_evicts_oldest():
    store = SessionStore(max_sessions=1, ttl_seconds=3600)
    store.record("kb-a", "s1", [{"role": "user", "content": "a"}], [])
    store.record("kb-b", "s1", [{"role": "user", "content": "b"}], [])

    assert store.get_history("kb-a", "s1") == []
    assert store.get_history("kb-b", "s1") == [{"role": "user", "content": "b"}]


# 验证会话存储统计助手回答数场景。
def test_session_store_counts_assistant_answers():
    store = SessionStore(max_sessions=10, ttl_seconds=3600)
    store.record(
        "kb",
        "s1",
        [],
        [
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "b"},
        ],
    )
    store.record(
        "kb",
        "s2",
        [],
        [{"role": "assistant", "content": "c"}],
    )
    store.record("other", "s1", [], [{"role": "assistant", "content": "d"}])

    assert store.answer_count("kb") == 2


# 验证记忆快照接口与长期记忆清理接口。
@pytest.mark.anyio
async def test_memory_snapshot_and_long_term_delete_endpoints(monkeypatch, tmp_path):
    import cogdoc.api.app as app_module

    monkeypatch.setattr(app_module, "configure_logging", lambda: None)
    store = SqliteSessionStore(str(tmp_path / "state.db"))
    store.record(
        "kb",
        "s1",
        [],
        [{"role": "user", "content": "请记住：默认使用中文"}],
    )
    app = create_app(session_store=store)

    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            snapshot = await client.get(
                "/v1/sessions/s1/memory", params={"doc_id": "kb"}
            )
            deleted = await client.delete(
                "/v1/memory/long-term", params={"doc_id": "kb"}
            )

    assert snapshot.status_code == 200
    assert snapshot.json()["long_term"][0]["content"] == "默认使用中文"
    assert deleted.status_code == 204
    assert store.get_memory_snapshot("kb", "s1")["long_term"] == []


# 验证 session store purges expired history 场景。
def test_session_store_purges_expired_history():
    store = SessionStore(max_sessions=10, ttl_seconds=1)
    store.record("kb", "s1", [{"role": "user", "content": "a"}], [])
    store._entries[("kb", "s1")].updated_at -= 2

    assert store.get_history("kb", "s1") == []


# 验证 chat endpoint maps runtime error to stable error code 场景。
@pytest.mark.anyio
async def test_chat_endpoint_maps_runtime_error_to_stable_error_code(monkeypatch):
    import cogdoc.api.app as app_module

    monkeypatch.setattr(app_module, "configure_logging", lambda: None)

    # 构造或驱动 失败路径运行器 测试场景。
    def failing_runner(doc_id, query, is_local, chat_history, forced_task):
        raise ChatServiceError(
            stage="runtime",
            error_class="ValueError",
            message="模型不可用",
            trace_id="trace-fail",
        )

    store = SessionStore()
    app = create_app(chat_runner=failing_runner, session_store=store)

    response = await _post_chat(
        app, {"query": "问题", "doc_id": "kb", "session_id": "s1"}
    )

    assert response.status_code == 503
    assert response.headers["X-Trace-Id"] == "trace-fail"
    payload = response.json()
    assert payload["error_code"] == "MODEL_UNAVAILABLE"
    assert payload["trace_id"] == "trace-fail"
    assert payload["details"]["error_class"] == "ValueError"
    # 失败不写会话，不漏栈。
    assert store.get_history("kb", "s1") == []
    assert "Traceback" not in payload["message"]


# 验证 chat endpoint maps stream stage to interrupted 场景。
@pytest.mark.anyio
async def test_chat_endpoint_maps_stream_stage_to_interrupted(monkeypatch):
    import cogdoc.api.app as app_module

    monkeypatch.setattr(app_module, "configure_logging", lambda: None)

    # 构造或驱动 失败路径运行器 测试场景。
    def failing_runner(doc_id, query, is_local, chat_history, forced_task):
        raise ChatServiceError(
            stage="stream",
            error_class="TimeoutError",
            message="流中断",
            trace_id="trace-stream",
        )

    app = create_app(chat_runner=failing_runner, session_store=SessionStore())

    response = await _post_chat(app, {"query": "问题", "doc_id": "kb"})

    assert response.status_code == 502
    assert response.json()["error_code"] == "STREAM_INTERRUPTED"


# 验证 run chat sync raises typed error when no final 场景。
def test_run_chat_sync_raises_typed_error_when_no_final(monkeypatch):
    from cogdoc.service import chat_service

    # 定义 CrashingApp 数据结构。
    class CrashingApp:
        # 流式返回结果。
        def stream(self, *args, **kwargs):
            raise RuntimeError("graph 调度崩溃")

    monkeypatch.setattr(chat_service, "app", CrashingApp())
    monkeypatch.setattr(chat_service, "configure_logging", lambda: None)
    monkeypatch.setattr(chat_service, "log_event", lambda *a, **k: None)
    monkeypatch.setattr(chat_service, "export_trace", lambda **k: None)

    with pytest.raises(chat_service.ChatServiceError) as excinfo:
        chat_service.run_chat_sync("kb", "问题", is_local=False)

    assert excinfo.value.stage == "runtime"
    assert excinfo.value.error_class == "RuntimeError"
    assert excinfo.value.message == "graph 调度崩溃"
