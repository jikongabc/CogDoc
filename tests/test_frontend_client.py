import httpx
import pytest
from cogdoc.frontend.api_client import (
    CogDocAPIError,
    CogDocClient,
    format_api_error,
    iter_sse_events,
    response_payload,
)


# 验证流式事件解析片段和最终帧场景。
def test_iter_sse_events_parses_token_and_final_frames():
    lines = [
        "event: start",
        'data: {"trace_id": "t1"}',
        "",
        "event: token",
        'data: {"content": "你好"}',
        "",
        "event: final",
        'data: {"answer": "完整答案", "citations": []}',
        "",
    ]

    events = list(iter_sse_events(lines))

    assert [name for name, _ in events] == ["start", "token", "final"]
    assert events[1][1]["content"] == "你好"
    assert events[2][1]["answer"] == "完整答案"


# 验证流式事件跳过非法数据场景。
def test_iter_sse_events_skips_non_json_data():
    lines = [
        "event: token",
        "data: not-json",
        "",
        "event: token",
        'data: {"content": "ok"}',
    ]

    events = list(iter_sse_events(lines))

    assert events == [("token", {"content": "ok"})]


# 验证接口错误格式化优先使用结构化错误体场景。
def test_format_api_error_prefers_structured_error_body():
    message = format_api_error(
        {"error_code": "REQUEST_THROTTLED", "message": "请求过于频繁，请稍后重试"},
        429,
    )

    assert message == "HTTP 429: [REQUEST_THROTTLED] 请求过于频繁，请稍后重试"


# 验证知识库列表遇到结构化错误时抛出异常场景。
def test_list_knowledge_bases_raises_on_structured_error(monkeypatch):
    # 测试伪造读取。
    def fake_get(*args, **kwargs):
        return httpx.Response(
            429,
            json={
                "schema_version": "v1",
                "error_code": "REQUEST_THROTTLED",
                "message": "请求过于频繁，请稍后重试",
                "request_id": None,
                "trace_id": None,
                "details": None,
            },
        )

    monkeypatch.setattr("cogdoc.frontend.api_client.httpx.get", fake_get)

    with pytest.raises(CogDocAPIError) as excinfo:
        CogDocClient("http://api").list_knowledge_bases()

    assert excinfo.value.status_code == 429
    assert "REQUEST_THROTTLED" in str(excinfo.value)


# 验证知识库列表拒绝非列表成功载荷场景。
def test_list_knowledge_bases_rejects_non_list_success_payload(monkeypatch):
    monkeypatch.setattr(
        "cogdoc.frontend.api_client.httpx.get",
        lambda *args, **kwargs: httpx.Response(200, json={"items": []}),
    )

    with pytest.raises(CogDocAPIError, match="知识库列表响应格式不符合预期"):
        CogDocClient("http://api").list_knowledge_bases()


# 验证响应载荷在非对象响应时退回文本场景。
def test_response_payload_falls_back_to_text_for_non_json_response():
    response = httpx.Response(502, text="bad gateway body")

    assert response_payload(response) == "bad gateway body"


# 验证跟踪客户端方法调用预期端点场景。
def test_trace_client_methods_call_expected_endpoints(monkeypatch):
    calls = []

    # 测试伪造读取。
    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr("cogdoc.frontend.api_client.httpx.get", fake_get)
    client = CogDocClient("http://api", api_key="secret")

    trace_resp = client.get_trace("trace-1")
    list_resp = client.list_traces(limit=7, kb_id="kb", session_id="s1")

    assert trace_resp.json() == {"ok": True}
    assert list_resp.json() == {"ok": True}
    assert calls[0][0] == "http://api/v1/traces/trace-1"
    assert calls[0][1]["headers"] == {"Authorization": "Bearer secret"}
    assert calls[1][0] == "http://api/v1/traces"
    assert calls[1][1]["params"] == {
        "limit": 7,
        "doc_id": "kb",
        "session_id": "s1",
    }


def test_client_cache_identity_is_stable_but_never_contains_api_key():
    first = CogDocClient("http://api", api_key="secret-a")
    same = CogDocClient("http://api", api_key="secret-a")
    other = CogDocClient("http://api", api_key="secret-b")
    anonymous = CogDocClient("http://api", api_key="")

    assert first.auth_cache_identity == same.auth_cache_identity
    assert first.auth_cache_identity != other.auth_cache_identity
    assert "secret" not in first.auth_cache_identity
    assert anonymous.auth_cache_identity == "anonymous"


def test_research_client_methods_call_expected_endpoints(monkeypatch):
    calls = []

    def fake_post(url, **kwargs):
        calls.append(("post", url, kwargs))
        return httpx.Response(201, json={"job": {"job_id": "rj_1"}})

    def fake_get(url, **kwargs):
        calls.append(("get", url, kwargs))
        return httpx.Response(200, json={"jobs": []})

    def fake_put(url, **kwargs):
        calls.append(("put", url, kwargs))
        return httpx.Response(200, json={"job": {"job_id": "rj_1"}})

    monkeypatch.setattr("cogdoc.frontend.api_client.httpx.post", fake_post)
    monkeypatch.setattr("cogdoc.frontend.api_client.httpx.get", fake_get)
    monkeypatch.setattr("cogdoc.frontend.api_client.httpx.put", fake_put)
    client = CogDocClient("http://api", api_key="secret")

    client.create_research_job(
        "kb",
        "研究目标",
        title="标题",
        section_titles=["证据"],
        is_local=True,
    )
    client.list_research_jobs("kb", status="planned", limit=12)
    client.get_research_job("rj_1")
    client.update_research_plan(
        "rj_1",
        expected_revision=2,
        sections=[{"title": "证据", "research_question": "有哪些证据？"}],
    )
    client.generate_research_plan("rj_1", expected_revision=3, is_local=True)
    client.research_action("rj_1", "pause")
    client.research_action("rj_1", "generate")
    client.research_action("rj_1", "refresh")
    client.get_research_provenance("rj_1")
    client.get_research_report("rj_1")
    client.review_research_report(
        "rj_1",
        expected_revision=7,
        decisions=[{"section_id": "s1", "decision": "approved", "note": "已核对"}],
    )
    client.publish_research_report("rj_1", expected_revision=8)
    client.get_published_research_report("rj_1")
    client.get_published_research_bundle("rj_1")

    assert calls[0][0:2] == ("post", "http://api/v1/research-jobs")
    assert calls[0][2]["json"]["section_titles"] == ["证据"]
    assert calls[0][2]["json"]["is_local"] is True
    assert calls[1][0:2] == ("get", "http://api/v1/research-jobs")
    assert calls[1][2]["params"] == {
        "kb_id": "kb",
        "limit": 12,
        "status": "planned",
    }
    assert calls[2][0:2] == ("get", "http://api/v1/research-jobs/rj_1")
    assert calls[3][0:2] == ("put", "http://api/v1/research-jobs/rj_1/plan")
    assert calls[3][2]["json"]["expected_revision"] == 2
    assert calls[3][2]["headers"] == {"Authorization": "Bearer secret"}
    assert calls[4][0:2] == (
        "post",
        "http://api/v1/research-jobs/rj_1/plan/auto",
    )
    assert calls[4][2]["json"] == {"expected_revision": 3, "is_local": True}
    assert calls[5][0:2] == (
        "post",
        "http://api/v1/research-jobs/rj_1/pause",
    )
    assert calls[6][0:2] == (
        "post",
        "http://api/v1/research-jobs/rj_1/generate",
    )
    assert calls[7][0:2] == (
        "post",
        "http://api/v1/research-jobs/rj_1/refresh",
    )
    assert calls[8][0:2] == (
        "get",
        "http://api/v1/research-jobs/rj_1/provenance",
    )
    assert calls[9][0:2] == ("get", "http://api/v1/research-jobs/rj_1/report")
    assert calls[10][0:2] == ("put", "http://api/v1/research-jobs/rj_1/review")
    assert calls[10][2]["json"]["expected_revision"] == 7
    assert calls[11][0:2] == ("post", "http://api/v1/research-jobs/rj_1/publish")
    assert calls[11][2]["json"] == {"expected_revision": 8}
    assert calls[12][0:2] == (
        "get",
        "http://api/v1/research-jobs/rj_1/published-report",
    )
    assert calls[13][0:2] == (
        "get",
        "http://api/v1/research-jobs/rj_1/published-bundle",
    )
    with pytest.raises(ValueError, match="unsupported research action"):
        client.research_action("rj_1", "delete")


# 验证反馈客户端发送证据载荷场景。
def test_feedback_client_sends_citation_and_evidence_payload(monkeypatch):
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return httpx.Response(201, json={"feedback_id": "f1", "is_bad_case": True})

    monkeypatch.setattr("cogdoc.frontend.api_client.httpx.post", fake_post)

    response = CogDocClient("http://api", api_key="secret").submit_feedback(
        trace_id="t1",
        feedback="thumbs_down",
        kb_id="kb",
        query="问题",
        answer="答案",
        citations=[{"chunk_id": "c1", "source": "a.pdf", "page": 1}],
        evidence=[{"chunk_id": "c1", "source": "a.pdf", "text_preview": "证据"}],
    )

    assert response.status_code == 201
    assert calls[0][0] == "http://api/v1/feedback"
    assert calls[0][1]["headers"] == {"Authorization": "Bearer secret"}
    assert calls[0][1]["json"]["citations"][0]["source"] == "a.pdf"
    assert calls[0][1]["json"]["evidence"][0]["text_preview"] == "证据"


# 验证反馈客户端发送保存知识字段场景。
def test_feedback_client_sends_save_as_knowledge_payload(monkeypatch):
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return httpx.Response(
            201,
            json={
                "feedback_id": "f1",
                "is_bad_case": True,
                "knowledge_id": "K1",
                "knowledge_status": "pending",
            },
        )

    monkeypatch.setattr("cogdoc.frontend.api_client.httpx.post", fake_post)

    response = CogDocClient("http://api").submit_feedback(
        trace_id="t1",
        feedback="correction",
        kb_id="kb",
        feedback_type="no_evidence",
        correction_text="正确说法",
        related_source="a.pdf",
        related_source_sha256="sha",
        related_chunk_ids=["c1"],
        related_page_start=1,
        related_page_end=2,
        related_chunk_text_hash="hash",
        related_anchor_text="证据锚点",
        certainty="high",
        skip_retrieval_feedback=True,
    )

    assert response.status_code == 201
    payload = calls[0][1]["json"]
    assert payload["feedback_type"] == "no_evidence"
    assert payload["correction_text"] == "正确说法"
    assert payload["related_source"] == "a.pdf"
    assert payload["related_chunk_ids"] == ["c1"]
    assert payload["related_page_start"] == 1
    assert payload["related_page_end"] == 2
    assert payload["related_chunk_text_hash"] == "hash"
    assert payload["related_anchor_text"] == "证据锚点"
    assert payload["certainty"] == "high"
    assert payload["skip_retrieval_feedback"] is True


# 验证反馈查询客户端方法调用稳定端点场景。
def test_feedback_client_list_method_calls_expected_endpoint(monkeypatch):
    calls = []

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return httpx.Response(200, json={"feedback": []})

    monkeypatch.setattr("cogdoc.frontend.api_client.httpx.get", fake_get)

    response = CogDocClient("http://api", api_key="secret").list_feedback(
        "kb",
        trace_id="t1",
        session_id="s1",
        feedback="thumbs_down",
        feedback_type="bad_retrieval",
        is_bad_case=False,
        limit=25,
    )

    assert response.status_code == 200
    assert calls[0][0] == "http://api/v1/feedback"
    assert calls[0][1]["headers"] == {"Authorization": "Bearer secret"}
    assert calls[0][1]["params"] == {
        "kb_id": "kb",
        "trace_id": "t1",
        "session_id": "s1",
        "feedback": "thumbs_down",
        "feedback_type": "bad_retrieval",
        "is_bad_case": False,
        "limit": 25,
    }


# 验证来源分块客户端携带锚点参数。
def test_source_chunks_client_sends_anchor_text(monkeypatch):
    calls = []

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return httpx.Response(200, json={"chunks": []})

    monkeypatch.setattr("cogdoc.frontend.api_client.httpx.get", fake_get)

    response = CogDocClient("http://api", api_key="secret").list_source_chunks(
        "kb", "a.pdf", limit=20, anchor_text="锚点"
    )

    assert response.status_code == 200
    assert calls[0][0] == "http://api/v1/knowledge-bases/kb/sources/a.pdf/chunks"
    assert calls[0][1]["headers"] == {"Authorization": "Bearer secret"}
    assert calls[0][1]["params"] == {
        "offset": 0,
        "limit": 20,
        "anchor_text": "锚点",
    }


def test_account_client_methods_keep_bearer_on_authenticated_calls(monkeypatch):
    calls = []

    def response(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(
        "cogdoc.frontend.api_client.httpx.get",
        lambda url, **kwargs: response("GET", url, **kwargs),
    )
    monkeypatch.setattr(
        "cogdoc.frontend.api_client.httpx.post",
        lambda url, **kwargs: response("POST", url, **kwargs),
    )
    monkeypatch.setattr(
        "cogdoc.frontend.api_client.httpx.patch",
        lambda url, **kwargs: response("PATCH", url, **kwargs),
    )
    monkeypatch.setattr(
        "cogdoc.frontend.api_client.httpx.delete",
        lambda url, **kwargs: response("DELETE", url, **kwargs),
    )
    client = CogDocClient("http://api/", api_key="session-secret")

    client.get_auth_config()
    client.register("new@example.com", "password-long", "New User", "Personal")
    client.login("new@example.com", "password-long", "ws-1")
    client.get_me()
    client.list_workspaces()
    client.create_workspace("Team")
    client.switch_workspace("ws-2")
    client.list_workspace_members("ws-2")
    client.update_workspace_member("ws-2", "user-2", "editor")
    client.remove_workspace_member("ws-2", "user-2")
    client.create_workspace_invite("ws-2", "invitee@example.com", "viewer")
    client.list_workspace_invites("ws-2")
    client.revoke_workspace_invite("ws-2", "invite-1")
    client.accept_workspace_invite("opaque-invite-token")
    client.logout()

    assert calls[0][0:2] == ("GET", "http://api/v1/auth/config")
    assert calls[1][1] == "http://api/v1/auth/register"
    assert calls[1][2]["json"] == {
        "email": "new@example.com",
        "password": "password-long",
        "display_name": "New User",
        "workspace_name": "Personal",
    }
    assert calls[2][2]["json"]["workspace_id"] == "ws-1"
    assert calls[6][1] == "http://api/v1/workspaces/ws-2/switch"
    assert calls[8][2]["json"] == {"role": "editor"}
    assert calls[13][1] == "http://api/v1/auth/invitations/accept"
    assert calls[13][2]["json"] == {"token": "opaque-invite-token"}
    assert all(
        call[2]["headers"]["Authorization"] == "Bearer session-secret" for call in calls
    )
    assert all(
        calls[index][2]["headers"]["X-CogDoc-Workspace"] == "ws-2"
        for index in range(6, 13)
    )
    assert all(
        "X-CogDoc-Workspace" not in calls[index][2]["headers"]
        for index in (0, 1, 2, 3, 4, 5, 13, 14)
    )


def test_resource_access_client_methods_use_stable_acl_endpoints(monkeypatch):
    calls = []

    def response(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(
        "cogdoc.frontend.api_client.httpx.get",
        lambda url, **kwargs: response("GET", url, **kwargs),
    )
    monkeypatch.setattr(
        "cogdoc.frontend.api_client.httpx.post",
        lambda url, **kwargs: response("POST", url, **kwargs),
    )
    monkeypatch.setattr(
        "cogdoc.frontend.api_client.httpx.patch",
        lambda url, **kwargs: response("PATCH", url, **kwargs),
    )
    monkeypatch.setattr(
        "cogdoc.frontend.api_client.httpx.delete",
        lambda url, **kwargs: response("DELETE", url, **kwargs),
    )
    client = CogDocClient("http://api", api_key="secret")

    client.get_kb_access_policy("kb")
    client.update_kb_access_policy("kb", "private")
    client.get_document_access_policy("kb", "doc-1")
    client.update_document_access_policy("kb", "doc-1", "private", source="policy.pdf")
    client.list_kb_grants("kb")
    client.grant_kb_access("kb", "user-2", "viewer")
    client.revoke_kb_access("kb", "user-2")
    client.list_document_grants("kb", "doc-1")
    client.grant_document_access("kb", "doc-1", "user-2", "editor")
    client.revoke_document_access("kb", "doc-1", "user-2")

    assert calls[0][1] == "http://api/v1/knowledge-bases/kb/access"
    assert calls[1][0] == "PATCH"
    assert calls[1][2]["json"] == {
        "schema_version": "v1",
        "policy": "private",
    }
    assert calls[3][2]["json"] == {
        "schema_version": "v1",
        "policy": "private",
        "source": "policy.pdf",
    }
    assert calls[5][2]["json"]["subject_id"] == "user-2"
    assert calls[-1][1].endswith("/documents/doc-1/access/grants/user-2")


def test_client_pins_workspace_header_and_updates_it_from_session_response(
    monkeypatch,
):
    calls = []

    def fake_get(url, **kwargs):
        calls.append(("GET", url, kwargs))
        return httpx.Response(200, json=[])

    def fake_post(url, **kwargs):
        calls.append(("POST", url, kwargs))
        return httpx.Response(
            200,
            json={
                "workspace": {
                    "workspace_id": "workspace-b",
                    "name": "B",
                    "role": "owner",
                }
            },
        )

    monkeypatch.setattr("cogdoc.frontend.api_client.httpx.get", fake_get)
    monkeypatch.setattr("cogdoc.frontend.api_client.httpx.post", fake_post)
    client = CogDocClient(
        "http://api",
        api_key="same-session",
        workspace_id="workspace-a",
    )

    client.list_knowledge_bases()
    client.switch_workspace("workspace-b")
    client.list_knowledge_bases()

    assert calls[0][2]["headers"] == {
        "Authorization": "Bearer same-session",
        "X-CogDoc-Workspace": "workspace-a",
    }
    assert calls[1][2]["headers"] == {
        "Authorization": "Bearer same-session",
        "X-CogDoc-Workspace": "workspace-b",
    }
    assert calls[2][2]["headers"] == {
        "Authorization": "Bearer same-session",
        "X-CogDoc-Workspace": "workspace-b",
    }


@pytest.mark.parametrize(
    "workspace_id",
    ["", " workspace", "workspace ", "work\nspace", "x" * 161],
)
def test_client_rejects_noncanonical_workspace_header(workspace_id):
    with pytest.raises(ValueError, match="workspace_id"):
        CogDocClient("http://api", api_key="session", workspace_id=workspace_id)


def test_create_knowledge_base_sends_initial_access_policy(monkeypatch):
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return httpx.Response(201, json={"kb_id": "private-kb"})

    monkeypatch.setattr("cogdoc.frontend.api_client.httpx.post", fake_post)
    response = CogDocClient("http://api", api_key="session").create_knowledge_base(
        "private-kb", access_policy="private"
    )

    assert response.status_code == 201
    assert calls == [
        (
            "http://api/v1/knowledge-bases",
            {
                "json": {"kb_id": "private-kb", "access_policy": "private"},
                "timeout": 180.0,
                "headers": {"Authorization": "Bearer session"},
            },
        )
    ]


# 验证派生知识客户端方法调用稳定端点场景。
def test_knowledge_client_methods_call_expected_endpoints(monkeypatch):
    calls = []

    def fake_post(url, **kwargs):
        calls.append(("POST", url, kwargs))
        return httpx.Response(200, json={"ok": True})

    def fake_get(url, **kwargs):
        calls.append(("GET", url, kwargs))
        return httpx.Response(200, json={"knowledge": []})

    def fake_delete(url, **kwargs):
        calls.append(("DELETE", url, kwargs))
        return httpx.Response(204)

    monkeypatch.setattr("cogdoc.frontend.api_client.httpx.post", fake_post)
    monkeypatch.setattr("cogdoc.frontend.api_client.httpx.get", fake_get)
    monkeypatch.setattr("cogdoc.frontend.api_client.httpx.delete", fake_delete)

    client = CogDocClient("http://api", api_key="secret")
    client.create_knowledge(
        kb_id="kb",
        text="知识",
        related_source="a.pdf",
        related_source_sha256="sha",
        related_chunk_ids=["c1"],
        related_page_start=3,
        related_page_end=4,
        related_chunk_text_hash="hash-old",
        related_anchor_text="旧锚点",
        source_note="人工确认",
        certainty="high",
        origin="saved_answer",
        created_from_trace_id="trace-1",
    )
    client.list_knowledge(
        "kb",
        status="pending",
        document_id="policy.pdf",
        origin="manual_entry",
        conflict_group_id="C1",
        has_conflict=True,
        created_after="2026-01-01",
        created_before="2026-12-31",
    )
    client.review_knowledge(
        "K1",
        "approve",
        actor="admin",
        related_document_id="doc-new",
        related_source="policy.pdf",
        related_source_sha256="sha-new",
        related_chunk_ids=["c2"],
        related_page_start=5,
        related_page_end=6,
        related_chunk_text_hash="hash-new",
        related_anchor_text="新锚点",
    )
    client.revise_knowledge(
        "K1",
        text="新知识",
        related_source="policy-v2.pdf",
        related_chunk_ids=["c3"],
        related_page_start=7,
        related_page_end=8,
        related_chunk_text_hash="hash-revised",
        related_anchor_text="修订锚点",
        source_note="修订",
        created_by="admin",
    )
    client.batch_review_knowledge(["K1", "K2"], "batch-reject", note="重复")
    client.scan_stale_knowledge("kb")
    client.delete_knowledge("K1")

    assert calls[0][0:2] == ("POST", "http://api/v1/knowledge")
    assert calls[0][2]["headers"] == {"Authorization": "Bearer secret"}
    assert calls[0][2]["json"]["related_chunk_ids"] == ["c1"]
    assert calls[0][2]["json"]["related_page_start"] == 3
    assert calls[0][2]["json"]["related_page_end"] == 4
    assert calls[0][2]["json"]["related_chunk_text_hash"] == "hash-old"
    assert calls[0][2]["json"]["related_anchor_text"] == "旧锚点"
    assert calls[0][2]["json"]["origin"] == "saved_answer"
    assert calls[0][2]["json"]["created_from_trace_id"] == "trace-1"
    assert calls[1][0:2] == ("GET", "http://api/v1/knowledge")
    assert calls[1][2]["params"] == {
        "kb_id": "kb",
        "status": "pending",
        "document_id": "policy.pdf",
        "origin": "manual_entry",
        "conflict_group_id": "C1",
        "has_conflict": True,
        "created_after": "2026-01-01",
        "created_before": "2026-12-31",
    }
    assert calls[2][0:2] == ("POST", "http://api/v1/knowledge/K1/approve")
    assert calls[2][2]["json"] == {
        "actor": "admin",
        "related_document_id": "doc-new",
        "related_source": "policy.pdf",
        "related_source_sha256": "sha-new",
        "related_chunk_ids": ["c2"],
        "related_page_start": 5,
        "related_page_end": 6,
        "related_chunk_text_hash": "hash-new",
        "related_anchor_text": "新锚点",
    }
    assert calls[3][0:2] == ("POST", "http://api/v1/knowledge/K1/revise")
    assert calls[3][2]["json"] == {
        "text": "新知识",
        "related_source": "policy-v2.pdf",
        "related_chunk_ids": ["c3"],
        "related_page_start": 7,
        "related_page_end": 8,
        "related_chunk_text_hash": "hash-revised",
        "related_anchor_text": "修订锚点",
        "source_note": "修订",
        "certainty": "medium",
        "created_by": "admin",
    }
    assert calls[4][0:2] == ("POST", "http://api/v1/knowledge/batch-reject")
    assert calls[4][2]["json"] == {"knowledge_ids": ["K1", "K2"], "note": "重复"}
    assert calls[5][0:2] == ("POST", "http://api/v1/knowledge/stale-scan")
    assert calls[5][2]["params"] == {"kb_id": "kb"}
    assert calls[6][0:2] == ("DELETE", "http://api/v1/knowledge/K1")
    assert calls[6][2]["headers"] == {"Authorization": "Bearer secret"}


# 验证检索调权客户端方法调用稳定端点场景。
def test_retrieval_feedback_client_methods_call_expected_endpoints(monkeypatch):
    calls = []

    def fake_post(url, **kwargs):
        calls.append(("POST", url, kwargs))
        return httpx.Response(200, json={"ok": True})

    def fake_get(url, **kwargs):
        calls.append(("GET", url, kwargs))
        return httpx.Response(200, json={"retrieval_feedback": []})

    monkeypatch.setattr("cogdoc.frontend.api_client.httpx.post", fake_post)
    monkeypatch.setattr("cogdoc.frontend.api_client.httpx.get", fake_get)

    client = CogDocClient("http://api", api_key="secret")
    client.list_retrieval_feedback("kb", enabled=False, limit=50)
    client.set_retrieval_feedback_enabled("rf1", False, actor="admin", reason="误点")
    client.set_retrieval_feedback_enabled("rf1", True)

    assert calls[0][0:2] == ("GET", "http://api/v1/retrieval-feedback")
    assert calls[0][2]["headers"] == {"Authorization": "Bearer secret"}
    assert calls[0][2]["params"] == {
        "kb_id": "kb",
        "enabled": False,
        "limit": 50,
    }
    assert calls[1][0:2] == (
        "POST",
        "http://api/v1/retrieval-feedback/rf1/disable",
    )
    assert calls[1][2]["json"] == {"actor": "admin", "reason": "误点"}
    assert calls[2][0:2] == (
        "POST",
        "http://api/v1/retrieval-feedback/rf1/enable",
    )
    assert "json" not in calls[2][2]


def test_retrieval_eval_review_client_methods_call_expected_endpoints(monkeypatch):
    calls = []

    def fake_get(url, **kwargs):
        calls.append(("GET", url, kwargs))
        return httpx.Response(200, json={"ok": True})

    def fake_post(url, **kwargs):
        calls.append(("POST", url, kwargs))
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr("cogdoc.frontend.api_client.httpx.get", fake_get)
    monkeypatch.setattr("cogdoc.frontend.api_client.httpx.post", fake_post)

    client = CogDocClient("http://api", api_key="review-key")
    client.list_retrieval_eval_drafts(
        kb_id="kb", status="pending", is_stale=False, limit=50
    )
    client.get_retrieval_eval_candidates("d1", top_k=9)
    client.review_retrieval_eval_draft(
        "d1",
        decision="approved",
        expected_revision=3,
        annotations={"units": [{"unit_id": "r1"}]},
    )
    client.export_retrieval_eval_drafts()

    headers = {"Authorization": "Bearer review-key"}
    assert calls[0] == (
        "GET",
        "http://api/v1/retrieval-eval-drafts",
        {
            "params": {
                "kb_id": "kb",
                "status": "pending",
                "is_stale": False,
                "limit": 50,
            },
            "timeout": client.timeout,
            "headers": headers,
        },
    )
    assert calls[1][0:2] == (
        "GET",
        "http://api/v1/retrieval-eval-drafts/d1/candidates",
    )
    assert calls[1][2]["params"] == {"top_k": 9}
    assert calls[2][0:2] == (
        "POST",
        "http://api/v1/retrieval-eval-drafts/d1/review",
    )
    assert calls[2][2]["json"] == {
        "decision": "approved",
        "expected_revision": 3,
        "reason": "",
        "annotations": {"units": [{"unit_id": "r1"}]},
    }
    assert calls[3][0:2] == (
        "GET",
        "http://api/v1/retrieval-eval-drafts/export",
    )
    assert calls[3][2]["params"] == {
        "dataset_partition": "release_gate",
        "format": "retrieval_eval_v1",
    }


# 验证反馈分析客户端方法调用稳定端点场景。
def test_feedback_analysis_client_method_calls_expected_endpoint(monkeypatch):
    calls = []

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return httpx.Response(200, json={"feedback_analysis": []})

    monkeypatch.setattr("cogdoc.frontend.api_client.httpx.get", fake_get)

    response = CogDocClient("http://api", api_key="secret").list_feedback_analysis(
        "kb",
        recommended_action="create_pending_knowledge",
        needs_review=False,
        limit=25,
    )

    assert response.status_code == 200
    assert calls[0][0] == "http://api/v1/feedback-analysis"
    assert calls[0][1]["headers"] == {"Authorization": "Bearer secret"}
    assert calls[0][1]["params"] == {
        "kb_id": "kb",
        "recommended_action": "create_pending_knowledge",
        "needs_review": False,
        "limit": 25,
    }


# 验证审核队列摘要客户端方法调用稳定端点场景。
def test_review_queue_summary_client_method_calls_expected_endpoint(monkeypatch):
    calls = []

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return httpx.Response(200, json={"knowledge": {"pending": 1}})

    monkeypatch.setattr("cogdoc.frontend.api_client.httpx.get", fake_get)

    response = CogDocClient("http://api", api_key="secret").review_queue_summary(
        "kb",
        document_id="policy.pdf",
        origin="saved_answer",
        created_by="frontend",
        has_conflict=True,
        created_after="2026-01-01",
        created_before="2026-12-31",
    )
    export = CogDocClient("http://api", api_key="secret").review_queue_export(
        "kb",
        limit=50,
        knowledge_document_id="policy.pdf",
        knowledge_origin="saved_answer",
        knowledge_created_by="frontend",
        knowledge_has_conflict=True,
        knowledge_created_after="2026-01-01",
        knowledge_created_before="2026-12-31",
    )

    assert response.status_code == 200
    assert calls[0][0] == "http://api/v1/review-queue"
    assert calls[0][1]["headers"] == {"Authorization": "Bearer secret"}
    assert calls[0][1]["params"] == {
        "kb_id": "kb",
        "document_id": "policy.pdf",
        "origin": "saved_answer",
        "created_by": "frontend",
        "has_conflict": True,
        "created_after": "2026-01-01",
        "created_before": "2026-12-31",
    }
    assert export.status_code == 200
    assert calls[1][0] == "http://api/v1/review-queue/export"
    assert calls[1][1]["headers"] == {"Authorization": "Bearer secret"}
    assert calls[1][1]["params"] == {
        "kb_id": "kb",
        "limit": 50,
        "knowledge_document_id": "policy.pdf",
        "knowledge_origin": "saved_answer",
        "knowledge_created_by": "frontend",
        "knowledge_has_conflict": True,
        "knowledge_created_after": "2026-01-01",
        "knowledge_created_before": "2026-12-31",
    }


# 验证知识审核指标客户端方法调用稳定端点场景。
def test_knowledge_metrics_client_methods_call_expected_endpoints(monkeypatch):
    calls = []

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr("cogdoc.frontend.api_client.httpx.get", fake_get)

    client = CogDocClient("http://api", api_key="secret")
    client.pending_knowledge_count("kb")
    client.knowledge_index_status("kb")
    client.feedback_loop_metrics("kb", answer_count=20)

    assert calls[0][0] == "http://api/v1/knowledge/pending-count"
    assert calls[0][1]["headers"] == {"Authorization": "Bearer secret"}
    assert calls[0][1]["params"] == {"kb_id": "kb"}
    assert calls[1][0] == "http://api/v1/knowledge/index-status"
    assert calls[1][1]["headers"] == {"Authorization": "Bearer secret"}
    assert calls[1][1]["params"] == {"kb_id": "kb"}
    assert calls[2][0] == "http://api/v1/feedback-loop-metrics"
    assert calls[2][1]["headers"] == {"Authorization": "Bearer secret"}
    assert calls[2][1]["params"] == {"kb_id": "kb", "answer_count": 20}
