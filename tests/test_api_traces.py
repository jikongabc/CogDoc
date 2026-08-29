import json
import pytest
from httpx import ASGITransport, AsyncClient
from cogdoc.api.app import create_app
from cogdoc.observability.trace import build_trace_payload, build_trace_step


# 声明异步测试使用的后端。
@pytest.fixture
def anyio_backend():
    return "asyncio"


# 创建测试客户端。
async def _get_trace(app, trace_id: str):
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            return await client.get(f"/v1/traces/{trace_id}")


# 验证接口返回已导出的跟踪文件。
@pytest.mark.anyio
async def test_trace_endpoint_returns_exported_trace(tmp_path, monkeypatch):
    import cogdoc.api.app as app_module
    import cogdoc.api.routes.traces as traces_module

    monkeypatch.setattr(app_module, "configure_logging", lambda: None)
    monkeypatch.setattr(
        traces_module, "trace_path", lambda trace_id: tmp_path / f"{trace_id}.json"
    )
    step = build_trace_step("intent_router", {"task_type": "qa"}, 1.0)
    payload = build_trace_payload(
        "trace-1",
        "req-1",
        "qa",
        [step],
        status="ok",
        duration_ms=2.0,
        config={"doc_id": "kb"},
    )
    (tmp_path / "trace-1.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    app = create_app()

    response = await _get_trace(app, "trace-1")

    body = response.json()
    assert response.status_code == 200
    assert body["schema_version"] == "v1"
    assert body["trace_id"] == "trace-1"
    assert body["status"] == "ok"
    assert body["config"]["doc_id"] == "kb"
    assert body["summary"]["step_count"] == 1
    assert body["steps"][0]["node_name"] == "intent_router"


@pytest.mark.anyio
async def test_trace_endpoint_hides_previous_kb_incarnation(tmp_path, monkeypatch):
    import cogdoc.api.app as app_module
    import cogdoc.api.routes.traces as traces_module

    monkeypatch.setattr(app_module, "configure_logging", lambda: None)
    monkeypatch.setattr(
        traces_module, "trace_path", lambda trace_id: tmp_path / f"{trace_id}.json"
    )
    monkeypatch.setattr(
        traces_module,
        "shared_epoch_store",
        lambda: type("Epoch", (), {"current": lambda self, _storage_id: 2})(),
    )
    payload = build_trace_payload(
        "trace-old-incarnation",
        "req-old-incarnation",
        "qa",
        [],
        config={"doc_id": "kb", "kb_epoch": 1},
    )
    (tmp_path / "trace-old-incarnation.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    app = create_app()

    response = await _get_trace(app, "trace-old-incarnation")

    assert response.status_code == 404
    assert response.json()["error_code"] == "TRACE_NOT_FOUND"


# 验证接口返回最近跟踪文件列表。
@pytest.mark.anyio
async def test_trace_endpoint_lists_recent_traces(tmp_path, monkeypatch):
    import os
    import cogdoc.api.app as app_module
    import cogdoc.api.routes.traces as traces_module

    monkeypatch.setattr(app_module, "configure_logging", lambda: None)
    monkeypatch.setattr(
        traces_module, "trace_path", lambda trace_id: tmp_path / f"{trace_id}.json"
    )
    monkeypatch.setattr(traces_module, "trace_dir", lambda: tmp_path)
    first = build_trace_payload(
        "trace-old",
        "req-old",
        "qa",
        [build_trace_step("intent_router", {"task_type": "qa"}, 1.0)],
        status="ok",
        config={"query_preview": "旧问题"},
    )
    second = build_trace_payload(
        "trace-new",
        "req-new",
        "summary",
        [build_trace_step("summary", {"task_type": "summary"}, 2.0)],
        status="degraded",
        duration_ms=3.0,
        config={"query_preview": "最新问题"},
    )
    (tmp_path / "trace-old.json").write_text(
        json.dumps(first, ensure_ascii=False), encoding="utf-8"
    )
    (tmp_path / "trace-new.json").write_text(
        json.dumps(second, ensure_ascii=False), encoding="utf-8"
    )
    (tmp_path / "broken.json").write_text("{", encoding="utf-8")
    os.utime(tmp_path / "trace-old.json", (1000, 1000))
    os.utime(tmp_path / "trace-new.json", (2000, 2000))
    app = create_app()

    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            response = await client.get("/v1/traces?limit=10")

    body = response.json()
    assert response.status_code == 200
    assert [trace["trace_id"] for trace in body["traces"]] == [
        "trace-new",
        "trace-old",
    ]
    assert body["traces"][0]["status"] == "degraded"
    assert body["traces"][0]["query_preview"] == "最新问题"
    assert body["traces"][0]["summary"]["step_count"] == 1


# 验证 trace 列表可按当前知识库和会话过滤。
@pytest.mark.anyio
async def test_trace_endpoint_filters_by_doc_and_session(tmp_path, monkeypatch):
    import cogdoc.api.app as app_module
    import cogdoc.api.routes.traces as traces_module

    monkeypatch.setattr(app_module, "configure_logging", lambda: None)
    monkeypatch.setattr(traces_module, "trace_dir", lambda: tmp_path)
    first = build_trace_payload(
        "trace-current",
        "req-current",
        "qa",
        [build_trace_step("intent_router", {"task_type": "qa"}, 1.0)],
        status="ok",
        config={"doc_id": "kb", "session_id": "s1", "query_preview": "当前问题"},
    )
    other = build_trace_payload(
        "trace-other",
        "req-other",
        "qa",
        [build_trace_step("intent_router", {"task_type": "qa"}, 1.0)],
        status="ok",
        config={"doc_id": "kb", "session_id": "s2", "query_preview": "其他问题"},
    )
    (tmp_path / "trace-current.json").write_text(
        json.dumps(first, ensure_ascii=False), encoding="utf-8"
    )
    (tmp_path / "trace-other.json").write_text(
        json.dumps(other, ensure_ascii=False), encoding="utf-8"
    )
    app = create_app()

    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            response = await client.get("/v1/traces?limit=10&doc_id=kb&session_id=s1")

    body = response.json()
    assert response.status_code == 200
    assert [trace["trace_id"] for trace in body["traces"]] == ["trace-current"]
    assert body["traces"][0]["query_preview"] == "当前问题"


# 验证缺失跟踪文件返回稳定错误。
@pytest.mark.anyio
async def test_trace_endpoint_returns_not_found_for_missing_trace(
    tmp_path, monkeypatch
):
    import cogdoc.api.app as app_module
    import cogdoc.api.routes.traces as traces_module

    monkeypatch.setattr(app_module, "configure_logging", lambda: None)
    monkeypatch.setattr(
        traces_module, "trace_path", lambda trace_id: tmp_path / f"{trace_id}.json"
    )
    app = create_app()

    response = await _get_trace(app, "missing")

    body = response.json()
    assert response.status_code == 404
    assert body["error_code"] == "TRACE_NOT_FOUND"


# 验证接口兼容旧版跟踪文件。
@pytest.mark.anyio
async def test_trace_endpoint_normalizes_legacy_trace(tmp_path, monkeypatch):
    import cogdoc.api.app as app_module
    import cogdoc.api.routes.traces as traces_module

    monkeypatch.setattr(app_module, "configure_logging", lambda: None)
    monkeypatch.setattr(
        traces_module, "trace_path", lambda trace_id: tmp_path / f"{trace_id}.json"
    )
    (tmp_path / "legacy.json").write_text(
        json.dumps(
            {
                "trace_id": "legacy",
                "request_id": "req-legacy",
                "task_type": "qa",
                "steps": [{"node_name": "intent_router"}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    app = create_app()

    response = await _get_trace(app, "legacy")

    body = response.json()
    assert response.status_code == 200
    assert body["schema_version"] == "v1"
    assert body["status"] == "ok"
    assert body["summary"]["step_count"] == 1
    assert body["summary"]["claim_audit"] is None


# 验证非法标识不会落到文件系统路径。
@pytest.mark.anyio
async def test_trace_endpoint_rejects_unsafe_trace_id(tmp_path, monkeypatch):
    import cogdoc.api.app as app_module
    import cogdoc.api.routes.traces as traces_module

    monkeypatch.setattr(app_module, "configure_logging", lambda: None)
    calls = []
    monkeypatch.setattr(
        traces_module,
        "trace_path",
        lambda trace_id: calls.append(trace_id) or tmp_path / f"{trace_id}.json",
    )
    app = create_app()

    response = await _get_trace(app, "bad.trace")

    body = response.json()
    assert response.status_code == 404
    assert body["error_code"] == "TRACE_NOT_FOUND"
    assert calls == []


# 验证损坏跟踪文件返回稳定错误。
@pytest.mark.anyio
async def test_trace_endpoint_handles_corrupt_trace_file(tmp_path, monkeypatch):
    import cogdoc.api.app as app_module
    import cogdoc.api.routes.traces as traces_module

    monkeypatch.setattr(app_module, "configure_logging", lambda: None)
    monkeypatch.setattr(
        traces_module, "trace_path", lambda trace_id: tmp_path / f"{trace_id}.json"
    )
    (tmp_path / "bad.json").write_text("{", encoding="utf-8")
    app = create_app()

    response = await _get_trace(app, "bad")

    body = response.json()
    assert response.status_code == 500
    assert body["error_code"] == "INTERNAL_ERROR"
