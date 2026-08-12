import queue
import threading

import httpx

from cogdoc.frontend import app as frontend_app


class _SessionState(dict):
    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError as exc:
            raise AttributeError(key) from exc

    def __setattr__(self, key, value):
        self[key] = value


class _StreamlitStub:
    def __init__(self, **state):
        self.session_state = _SessionState(state)
        self.query_params = {"sid": "old", "kb": "kb-old"}
        self.warnings = []
        self.errors = []
        self.successes = []
        self.rerun_count = 0

    def warning(self, message):
        self.warnings.append(message)

    def error(self, message):
        self.errors.append(message)

    def success(self, message):
        self.successes.append(message)

    def rerun(self):
        self.rerun_count += 1


def _base_state(**updates):
    state = {
        "auth_token": "old-token",
        "auth_mode": "account",
        "auth_user": {"user_id": "old-user"},
        "auth_workspace": {"workspace_id": "old-workspace", "role": "owner"},
        "auth_workspaces": [],
        "auth_permissions": ["read", "query", "manage_access"],
        "auth_me_loaded_for": "old",
        "auth_me_loaded_at": 10.0,
        "last_invite_token": "invite",
        "messages_by_context": {("kb-old", "old"): [{"content": "private"}]},
        "restored_contexts": {("kb-old", "old")},
        "pending_streams": {},
        "pending_retrieve_debugs": {},
        "api_cache": {},
        "main_views_by_context": {},
        "trace_cache": {},
        "trace_labels": {},
        "trace_options_by_id": {},
        "trace_session_items_by_context": {},
        "trace_session_loaded": set(),
        "trace_session_error": {},
        "retrieve_debug_by_context": {},
        "feedback_action_by_message": {},
        "research_summary_cache": {},
        "research_summary_pages": {},
        "research_open_job_by_kb": {},
        "known_sessions": {},
        "kb_id": "kb-old",
        "active_trace_id": "trace-old",
        "research_notice": None,
        "session_id": "old",
    }
    state.update(updates)
    return state


def test_apply_auth_session_clears_previous_identity_context(monkeypatch):
    streamlit = _StreamlitStub(**_base_state())
    monkeypatch.setattr(frontend_app, "st", streamlit)

    applied = frontend_app._apply_auth_session(
        {
            "access_token": "new-token",
            "user": {
                "user_id": "new-user",
                "email": "new@example.com",
                "display_name": "New",
            },
            "workspace": {
                "workspace_id": "new-workspace",
                "name": "New Workspace",
                "role": "owner",
            },
            "permissions": ["read", "query"],
        }
    )

    assert applied is True
    assert streamlit.session_state.auth_token == "new-token"
    assert streamlit.session_state.messages_by_context == {}
    assert streamlit.session_state.kb_id is None
    assert streamlit.session_state.session_id != "old"
    assert streamlit.session_state.last_invite_token == ""
    assert streamlit.session_state.auth_me_loaded_at == 0.0
    assert "kb" not in streamlit.query_params
    assert streamlit.query_params["sid"] == streamlit.session_state.session_id


def test_apply_auth_profile_resets_context_on_server_workspace_fallback(monkeypatch):
    streamlit = _StreamlitStub(**_base_state())
    monkeypatch.setattr(frontend_app, "st", streamlit)

    applied = frontend_app._apply_auth_profile(
        {
            "user": {
                "user_id": "old-user",
                "email": "user@example.com",
                "display_name": "User",
            },
            "workspace": {
                "workspace_id": "personal-workspace",
                "name": "Personal",
                "role": "owner",
            },
            "permissions": ["read", "query", "manage_access"],
            "workspaces": [
                {
                    "workspace_id": "personal-workspace",
                    "name": "Personal",
                    "role": "owner",
                }
            ],
        }
    )

    assert applied is True
    assert streamlit.session_state.auth_workspace["workspace_id"] == (
        "personal-workspace"
    )
    assert streamlit.session_state.messages_by_context == {}
    assert streamlit.session_state.kb_id is None
    assert streamlit.session_state.last_invite_token == ""
    assert streamlit.query_params.get("kb") is None


def test_auth_profile_uses_short_ttl_then_reloads_live_role(monkeypatch):
    token = "old-token"
    fingerprint = frontend_app.hashlib.sha256(token.encode("utf-8")).hexdigest()
    streamlit = _StreamlitStub(
        **_base_state(
            auth_token=token,
            auth_me_loaded_for=fingerprint,
            auth_me_loaded_at=100.0,
        )
    )
    monkeypatch.setattr(frontend_app, "st", streamlit)
    clock = [100.0 + frontend_app.AUTH_PROFILE_TTL_SECONDS - 0.1]
    monkeypatch.setattr(frontend_app.time, "monotonic", lambda: clock[0])
    calls = []

    class FakeClient:
        def get_me(self):
            calls.append("get_me")
            return httpx.Response(
                200,
                json={
                    "user": {
                        "user_id": "old-user",
                        "email": "user@example.com",
                        "display_name": "User",
                    },
                    "workspace": {
                        "workspace_id": "old-workspace",
                        "name": "Team",
                        "role": "viewer",
                    },
                    "permissions": ["read", "query"],
                    "workspaces": [
                        {
                            "workspace_id": "old-workspace",
                            "name": "Team",
                            "role": "viewer",
                        }
                    ],
                },
            )

    monkeypatch.setattr(frontend_app, "_client", lambda: FakeClient())

    assert frontend_app._refresh_auth_profile() is True
    assert calls == []

    clock[0] = 100.0 + frontend_app.AUTH_PROFILE_TTL_SECONDS
    assert frontend_app._refresh_auth_profile() is True
    assert calls == ["get_me"]
    assert streamlit.session_state.auth_workspace["role"] == "viewer"
    assert streamlit.session_state.messages_by_context == {}


def test_auth_profile_recovers_from_removed_pinned_workspace(monkeypatch):
    streamlit = _StreamlitStub(
        **_base_state(
            api_url="http://api",
            auth_me_loaded_for="",
            auth_me_loaded_at=0.0,
        )
    )
    monkeypatch.setattr(frontend_app, "st", streamlit)
    selected_workspaces = []

    class FakeClient:
        def __init__(self, _api_url, api_key=None, workspace_id=None):
            assert api_key == "old-token"
            self.workspace_id = workspace_id
            selected_workspaces.append(workspace_id)

        def get_me(self):
            if self.workspace_id == "old-workspace":
                return httpx.Response(
                    404,
                    json={
                        "error_code": "WORKSPACE_NOT_FOUND",
                        "message": "工作区不存在",
                    },
                )
            return httpx.Response(
                200,
                json={
                    "user": {
                        "user_id": "old-user",
                        "email": "user@example.com",
                        "display_name": "User",
                    },
                    "workspace": {
                        "workspace_id": "personal-workspace",
                        "name": "Personal",
                        "role": "owner",
                    },
                    "permissions": ["read", "query", "manage_access"],
                    "workspaces": [
                        {
                            "workspace_id": "personal-workspace",
                            "name": "Personal",
                            "role": "owner",
                        }
                    ],
                },
            )

    monkeypatch.setattr(frontend_app, "CogDocClient", FakeClient)

    assert frontend_app._refresh_auth_profile(force=True) is True
    assert selected_workspaces == ["old-workspace", None]
    assert streamlit.session_state.auth_workspace["workspace_id"] == (
        "personal-workspace"
    )
    assert streamlit.session_state.messages_by_context == {}
    assert streamlit.session_state.kb_id is None


def test_auth_rejections_clear_or_revalidate_frontend_state(monkeypatch):
    forbidden = _StreamlitStub(**_base_state())
    monkeypatch.setattr(frontend_app, "st", forbidden)

    frontend_app._observe_authenticated_response(403)

    assert forbidden.session_state.auth_token == "old-token"
    assert forbidden.session_state.auth_me_loaded_for == ""
    assert forbidden.session_state.auth_me_loaded_at == 0.0

    unauthorized = _StreamlitStub(**_base_state())
    monkeypatch.setattr(frontend_app, "st", unauthorized)

    frontend_app._observe_authenticated_response(401)

    assert unauthorized.session_state.auth_token == ""
    assert unauthorized.session_state.messages_by_context == {}
    assert unauthorized.session_state.last_invite_token == ""


def test_frontend_success_contract_accepts_every_2xx_response():
    assert frontend_app._response_succeeded(httpx.Response(200)) is True
    assert frontend_app._response_succeeded(httpx.Response(201)) is True
    assert frontend_app._response_succeeded(httpx.Response(204)) is True
    assert frontend_app._response_succeeded(httpx.Response(199)) is False
    assert frontend_app._response_succeeded(httpx.Response(300)) is False


def test_acl_grant_success_never_falls_through_to_error(monkeypatch):
    streamlit = _StreamlitStub(**_base_state())
    monkeypatch.setattr(frontend_app, "st", streamlit)

    frontend_app._handle_acl_grant_response(
        httpx.Response(200, json={"grant": {"role": "viewer"}}),
        success_message="updated",
        failure_message="failed",
    )

    assert streamlit.successes == ["updated"]
    assert streamlit.rerun_count == 1
    assert streamlit.errors == []


def test_frontend_api_cache_is_partitioned_by_authentication_identity(monkeypatch):
    streamlit = _StreamlitStub(
        **_base_state(
            auth_token="first-token",
            pending_streams={},
            pending_retrieve_debugs={},
            api_cache={},
        )
    )
    monkeypatch.setattr(frontend_app, "st", streamlit)
    loads = []

    first = frontend_app._cached_api_value(
        ("kbs", "http://api"), lambda: loads.append("first") or ["first"]
    )
    streamlit.session_state.auth_token = "second-token"
    second = frontend_app._cached_api_value(
        ("kbs", "http://api"), lambda: loads.append("second") or ["second"]
    )

    assert first == ["first"]
    assert second == ["second"]
    assert loads == ["first", "second"]
    assert all("token" not in str(key) for key in streamlit.session_state.api_cache)


def test_background_workers_receive_explicit_current_bearer(monkeypatch):
    credentials = []

    class FakeClient:
        def __init__(self, api_url, api_key=None, workspace_id=None):
            credentials.append((api_url, api_key, workspace_id))

        def retrieve(self, *_args, **_kwargs):
            return httpx.Response(200, json={"hits": []})

        def stream_chat(self, *_args, **_kwargs):
            yield "final", {"answer": "ok"}

    monkeypatch.setattr(frontend_app, "CogDocClient", FakeClient)
    retrieve_outbox = queue.Queue()
    frontend_app._retrieve_debug_worker(
        api_url="http://api",
        auth_token="session-token",
        workspace_id="workspace-a",
        kb_id="kb",
        query="q",
        top_k=3,
        rerank=False,
        rerank_top_n=None,
        outbox=retrieve_outbox,
    )
    stream_outbox = queue.Queue()
    frontend_app._stream_chat_worker(
        api_url="http://api",
        auth_token="session-token",
        workspace_id="workspace-a",
        kb_id="kb",
        session_id="session",
        prompt="q",
        mode="qa",
        is_local=False,
        stop_event=threading.Event(),
        outbox=stream_outbox,
    )

    assert credentials == [
        ("http://api", "session-token", "workspace-a"),
        ("http://api", "session-token", "workspace-a"),
    ]
    assert retrieve_outbox.get_nowait()[0] == "result"
    assert stream_outbox.get_nowait() == ("final", {"answer": "ok"})
