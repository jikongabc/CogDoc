import hashlib
import html
import json
import os
import queue
import threading
import time
import unicodedata
import uuid
from collections.abc import Mapping, Sequence

try:
    import streamlit as st
except ModuleNotFoundError:

    class _MissingStreamlit:
        def __getattr__(self, name):
            raise ModuleNotFoundError(
                "Streamlit is required to run the CogDoc frontend. "
                'Install it with `pip install -e ".[frontend]"`.'
            )

    st = _MissingStreamlit()

from cogdoc.frontend.api_client import (
    CogDocAPIError,
    CogDocClient,
    format_api_error,
    response_payload,
)

DEFAULT_API_URL = os.getenv("COGDOC_API_URL", "http://localhost:8000")
MAIN_VIEWS = ["对话", "研究", "来源", "派生知识", "证据审核", "调试"]
STREAM_RERUN_INTERVAL_SECONDS = 0.8
STREAM_PREVIEW_HEAD_CHARS = 1200
STREAM_PREVIEW_TAIL_CHARS = 3600
SIDEBAR_CACHE_TTL_SECONDS = 2.0
SIDEBAR_STREAM_CACHE_TTL_SECONDS = 30.0
SIDEBAR_STALE_CACHE_GRACE_SECONDS = 120.0
# Keep the displayed workspace and role close to the server's live membership
# state without turning every Streamlit rerun into an authentication round trip.
AUTH_PROFILE_TTL_SECONDS = 5.0
RESEARCH_SUMMARY_PAGE_SIZE = 20
NO_REFERENCE_ANSWER = "在所提供的参考资料中未找到与该问题相关的内容，建议查阅更多资料。"
TRACE_NODE_LABELS = {
    "runtime.setup": "运行准备",
    "intent_router": "意图路由",
    "rewrite_node": "问题改写",
    "verify_rewrite_node": "改写校验",
    "retrieve_node": "召回检索",
    "rerank_node": "重排",
    "generate_node": "答案生成",
    "citation_node": "引用校验",
    "qa_subgraph": "问答流程",
    "summary_subgraph": "摘要流程",
    "compare_subgraph": "对比流程",
}
CLAIM_VERDICT_LABELS = {
    "supported": "证据支持",
    "unsupported": "证据反驳",
    "insufficient": "证据不足",
    "not_factual": "非事实声明",
}
CONNECTOR_SECRET_FIELDS = {
    "zotero": (("api_key", "API key", True),),
    "notion": (("token", "Integration token", True),),
    "atlassian": (("token", "Access token", True),),
    "microsoft": (("token", "Access token", True),),
    "s3": (
        ("access_key", "Access key", True),
        ("secret_key", "Secret key", True),
        ("session_token", "Session token", False),
    ),
}
CONNECTOR_PROVIDER_ALIASES = {
    "zotero": "zotero",
    "notion": "notion",
    "confluence": "atlassian",
    "sharepoint": "microsoft",
    "s3": "s3",
}


def _research_contract_key(value: object) -> str:
    normalized = " ".join(str(value or "").split())
    return unicodedata.normalize("NFKC", normalized).casefold()


def _distinct_recovery_query(question: str, retrieval_query: str) -> str:
    clean_question = " ".join(question.split())
    retrieval_key = _research_contract_key(retrieval_query)
    for prefix in ("直接证据：", "同一问题的相关资料：", "替代检索："):
        candidate = f"{prefix}{clean_question}"[:1000]
        if _research_contract_key(candidate) != retrieval_key:
            return candidate
    return f"{clean_question[:994]} 证据".strip()


def _research_requirement_editor_lines(
    original_requirements: Sequence[Mapping],
) -> tuple[str, str, str]:
    questions: list[str] = []
    retrieval_queries: list[str] = []
    recovery_queries: list[str] = []
    for requirement in original_requirements:
        question = " ".join(str(requirement.get("question") or "").split())
        if not question:
            continue
        retrieval_query = " ".join(
            str(requirement.get("retrieval_query") or question).split()
        )
        recovery_query = " ".join(str(requirement.get("recovery_query") or "").split())
        if not recovery_query or _research_contract_key(
            retrieval_query
        ) == _research_contract_key(recovery_query):
            recovery_query = _distinct_recovery_query(question, retrieval_query)
        questions.append(question)
        retrieval_queries.append(retrieval_query)
        recovery_queries.append(recovery_query)
    return (
        "\n".join(questions),
        "\n".join(retrieval_queries),
        "\n".join(recovery_queries),
    )


def _build_edited_research_requirements(
    requirement_text: str,
    retrieval_query_text: str,
    recovery_query_text: str,
    original_requirements: Sequence[Mapping],
) -> list[dict[str, str]]:
    questions = [
        " ".join(line.split())
        for line in requirement_text.splitlines()
        if " ".join(line.split())
    ][:3]
    retrieval_rows = [
        " ".join(line.split()) for line in retrieval_query_text.splitlines()
    ]
    recovery_rows = [
        " ".join(line.split()) for line in recovery_query_text.splitlines()
    ]
    edited: list[dict[str, str]] = []
    for position, question in enumerate(questions):
        original = (
            original_requirements[position]
            if position < len(original_requirements)
            else {}
        )
        original_question = " ".join(str(original.get("question") or "").split())
        original_retrieval = " ".join(
            str(original.get("retrieval_query") or original_question).split()
        )
        original_recovery = " ".join(str(original.get("recovery_query") or "").split())
        if not original_recovery or _research_contract_key(
            original_recovery
        ) == _research_contract_key(original_retrieval):
            original_recovery = _distinct_recovery_query(
                original_question, original_retrieval
            )
        entered_retrieval = (
            retrieval_rows[position] if position < len(retrieval_rows) else ""
        )
        entered_recovery = (
            recovery_rows[position] if position < len(recovery_rows) else ""
        )
        question_changed = _research_contract_key(question) != _research_contract_key(
            original_question
        )
        retrieval_was_edited = bool(entered_retrieval) and (
            _research_contract_key(entered_retrieval)
            != _research_contract_key(original_retrieval)
        )
        recovery_was_edited = bool(entered_recovery) and (
            _research_contract_key(entered_recovery)
            != _research_contract_key(original_recovery)
        )
        retrieval_query = (
            entered_retrieval
            if not question_changed or retrieval_was_edited
            else question
        ) or question
        recovery_query = (
            entered_recovery
            if not question_changed or recovery_was_edited
            else _distinct_recovery_query(question, retrieval_query)
        )
        if not recovery_query or _research_contract_key(
            recovery_query
        ) == _research_contract_key(retrieval_query):
            recovery_query = _distinct_recovery_query(question, retrieval_query)
        edited.append(
            {
                "question": question,
                "retrieval_query": retrieval_query,
                "recovery_query": recovery_query,
            }
        )
    return edited


def _research_summary_cache_key(
    base_url: str,
    kb_id: str,
    cursor: str | None,
    status: str | None = None,
    auth_identity: str = "anonymous",
) -> tuple[str, str, str, str, str, str]:
    return (
        "research-summaries",
        base_url,
        auth_identity,
        kb_id,
        status or "",
        cursor or "",
    )


def _research_summary_progress_label(summary: Mapping) -> str:
    counts = summary.get("section_counts")
    if not isinstance(counts, Mapping):
        return "章节进度未知"
    total = counts.get("total")
    completed = counts.get("completed")
    running = counts.get("running")
    failed = counts.get("failed")
    if any(
        type(value) is not int or value < 0
        for value in (total, completed, running, failed)
    ):
        return "章节进度未知"
    label = f"章节 {completed}/{total}"
    if running:
        label += f" · 进行中 {running}"
    if failed:
        label += f" · 失败 {failed}"
    return label


def _research_summary_status_label(value: object) -> str:
    return {
        "planned": "待开始",
        "running": "取证中",
        "paused": "已暂停",
        "evidence_ready": "证据就绪",
        "generating": "报告生成中",
        "completed": "已完成",
        "failed": "失败",
        "cancelled": "已取消",
    }.get(str(value or ""), "状态未知")


def _research_summary_response_payload(response, cached_payload: object) -> Mapping:
    if response.status_code == 304:
        if not isinstance(cached_payload, Mapping):
            raise ValueError("摘要接口返回 304，但本地没有可复用的案卷索引")
        return cached_payload
    if response.status_code != 200:
        raise ValueError(_response_error(response, "读取研究案卷索引失败"))
    payload = response_payload(response)
    if not isinstance(payload, Mapping) or not isinstance(payload.get("jobs"), list):
        raise ValueError("研究案卷索引响应格式不符合预期")
    if type(payload.get("has_more", False)) is not bool:
        raise ValueError("研究案卷索引分页状态不符合预期")
    next_cursor = payload.get("next_cursor")
    if next_cursor is not None and type(next_cursor) is not str:
        raise ValueError("研究案卷索引游标不符合预期")
    return payload


# 创建测试客户端。
def _client() -> CogDocClient:
    return CogDocClient(
        st.session_state.api_url,
        api_key=_current_api_credential(),
        workspace_id=_current_workspace_id(),
    )


def _current_api_credential() -> str:
    """Return the active credential without ever persisting it outside session state."""

    token = str(st.session_state.get("auth_token") or "")
    return token or os.getenv("COGDOC_API_KEY", "")


def _current_workspace_id() -> str | None:
    if st.session_state.get("auth_mode") != "account":
        return None
    workspace = st.session_state.get("auth_workspace", {})
    if not isinstance(workspace, Mapping):
        return None
    workspace_id = str(workspace.get("workspace_id") or "")
    return workspace_id or None


def _active_auth_cache_identity() -> str:
    credential = _current_api_credential()
    return (
        hashlib.sha256(credential.encode("utf-8")).hexdigest()
        if credential
        else "anonymous"
    )


# 处理响应错误。
def _response_error(response, fallback: str = "请求失败") -> str:
    _observe_authenticated_response(response.status_code)
    return format_api_error(response_payload(response), response.status_code, fallback)


def _response_succeeded(response) -> bool:
    """Treat every HTTP 2xx response as success at the thin-client boundary."""

    return 200 <= int(response.status_code) < 300


def _handle_acl_grant_response(
    response, *, success_message: str, failure_message: str
) -> None:
    """Render exactly one terminal outcome for an ACL grant request."""

    if _response_succeeded(response):
        st.success(success_message)
        st.rerun()
    else:
        st.error(_response_error(response, failure_message))


# 提取响应状态与载荷。
def _response_status_payload(response) -> tuple[int, object]:
    _observe_authenticated_response(response.status_code)
    return response.status_code, response_payload(response)


# 初始化状态。
def _init_state() -> None:
    st.session_state.setdefault("api_url", DEFAULT_API_URL)
    # Account tokens live only in Streamlit's server-side browser session.  They
    # are never copied to query parameters, disk caches, or widget defaults.
    st.session_state.setdefault("auth_token", "")
    st.session_state.setdefault("auth_mode", "unknown")
    st.session_state.setdefault("auth_config_by_url", {})
    st.session_state.setdefault("auth_user", {})
    st.session_state.setdefault("auth_workspace", {})
    st.session_state.setdefault("auth_workspaces", [])
    st.session_state.setdefault("auth_permissions", [])
    st.session_state.setdefault("auth_me_loaded_for", "")
    st.session_state.setdefault("auth_me_loaded_at", 0.0)
    st.session_state.setdefault("last_invite_token", "")
    # 会话标识持久化进地址栏，刷新后复用同一会话。
    if "session_id" not in st.session_state:
        st.session_state.session_id = st.query_params.get("sid") or uuid.uuid4().hex
        st.query_params["sid"] = st.session_state.session_id
    st.session_state.setdefault("kb_id", None)
    st.session_state.setdefault("msg_seq", 0)
    st.session_state.setdefault("messages_by_context", {})
    st.session_state.setdefault("restored_contexts", set())
    st.session_state.setdefault("pending_streams", {})
    st.session_state.setdefault("pending_retrieve_debugs", {})
    st.session_state.setdefault("api_cache", {})
    st.session_state.setdefault("main_views_by_context", {})
    st.session_state.setdefault("trace_cache", {})
    st.session_state.setdefault("active_trace_id", "")
    st.session_state.setdefault("trace_labels", {})
    st.session_state.setdefault("trace_options_by_id", {})
    st.session_state.setdefault("trace_session_items_by_context", {})
    st.session_state.setdefault("trace_session_loaded", set())
    st.session_state.setdefault("trace_session_error", {})
    st.session_state.setdefault("retrieve_debug_by_context", {})
    st.session_state.setdefault("feedback_action_by_message", {})
    st.session_state.setdefault("research_notice", None)
    st.session_state.setdefault("research_summary_cache", {})
    st.session_state.setdefault("research_summary_pages", {})
    st.session_state.setdefault("research_open_job_by_kb", {})
    st.session_state.setdefault("eval_review_key", "")
    st.session_state.setdefault("eval_candidate_cache", {})
    st.session_state.setdefault("eval_export_jsonl", "")
    st.session_state.setdefault("claim_review_pages", {})
    st.session_state.setdefault("claim_review_export_jsonl", "")
    st.session_state.setdefault("claim_review_export_scope", "")
    st.session_state.setdefault("source_artifact_recovery", {})
    # 兼容旧状态：升级前只有一份全局消息，迁移到当前上下文桶里。
    if "messages" in st.session_state:
        if st.session_state.kb_id and st.session_state.messages:
            st.session_state.messages_by_context.setdefault(
                _context_key(st.session_state.kb_id, st.session_state.session_id),
                st.session_state.messages,
            )
        st.session_state.pop("messages", None)
    for legacy_key in ("restored_for", "answering", "pending_prompt", "pending_mode"):
        st.session_state.pop(legacy_key, None)
    # 本会话内已知的对话标识按知识库保存，并与后端列表合并。
    st.session_state.setdefault("known_sessions", {})


# 判断是否存在未完成流式请求。
def _has_pending_stream() -> bool:
    has_stream = any(
        not pending.get("done")
        for pending in st.session_state.pending_streams.values()
        if isinstance(pending, Mapping)
    )
    has_retrieve = any(
        not pending.get("done")
        for pending in st.session_state.pending_retrieve_debugs.values()
        if isinstance(pending, Mapping)
    )
    return has_stream or has_retrieve


# 返回侧栏缓存时长。
def _sidebar_cache_ttl() -> float:
    return (
        SIDEBAR_STREAM_CACHE_TTL_SECONDS
        if _has_pending_stream()
        else SIDEBAR_CACHE_TTL_SECONDS
    )


# 读取带时效的接口缓存。
def _cached_api_value(key: tuple, loader):
    cache = st.session_state.api_cache
    scoped_key = ("auth", _active_auth_cache_identity(), *key)
    now = time.monotonic()
    entry = cache.get(scoped_key)
    if entry and now - entry["time"] <= _sidebar_cache_ttl():
        return entry["value"]
    try:
        value = loader()
    except Exception:
        if entry and now - entry["time"] <= SIDEBAR_STALE_CACHE_GRACE_SECONDS:
            return entry["value"]
        raise
    cache[scoped_key] = {"time": now, "value": value}
    return value


# 清理接口缓存。
def _clear_api_cache(prefix: tuple | None = None) -> None:
    if prefix is None:
        st.session_state.api_cache.clear()
        return
    for key in list(st.session_state.api_cache):
        # New cache entries are identity scoped.  Accept legacy keys as well so
        # hot-upgraded Streamlit sessions can be cleared normally.
        logical_key = key[2:] if key[:1] == ("auth",) else key
        if logical_key[: len(prefix)] == prefix:
            st.session_state.api_cache.pop(key, None)


def _clear_research_summary_cache(client: CogDocClient, kb_id: str) -> None:
    cache = st.session_state.research_summary_cache
    prefix = (
        "research-summaries",
        client.base_url,
        getattr(client, "auth_cache_identity", "anonymous"),
        kb_id,
    )
    for key in list(cache):
        if isinstance(key, tuple) and key[: len(prefix)] == prefix:
            cache.pop(key, None)


def _stop_background_requests() -> None:
    for pending in st.session_state.get("pending_streams", {}).values():
        stop_event = pending.get("stop_event") if isinstance(pending, Mapping) else None
        if stop_event is not None:
            stop_event.set()
        response = pending.get("response") if isinstance(pending, Mapping) else None
        if response is not None:
            response.close()


def _reset_user_context() -> None:
    """Drop every user/workspace-scoped UI value on identity boundary changes."""

    _stop_background_requests()
    for name, empty in (
        ("messages_by_context", {}),
        ("restored_contexts", set()),
        ("pending_streams", {}),
        ("pending_retrieve_debugs", {}),
        ("api_cache", {}),
        ("main_views_by_context", {}),
        ("trace_cache", {}),
        ("trace_labels", {}),
        ("trace_options_by_id", {}),
        ("trace_session_items_by_context", {}),
        ("trace_session_loaded", set()),
        ("trace_session_error", {}),
        ("retrieve_debug_by_context", {}),
        ("feedback_action_by_message", {}),
        ("research_summary_cache", {}),
        ("research_summary_pages", {}),
        ("research_open_job_by_kb", {}),
        ("eval_candidate_cache", {}),
        ("claim_review_pages", {}),
        ("source_artifact_recovery", {}),
        ("known_sessions", {}),
    ):
        st.session_state[name] = empty
    st.session_state.kb_id = None
    st.session_state.active_trace_id = ""
    st.session_state.research_notice = None
    st.session_state.eval_review_key = ""
    st.session_state.eval_export_jsonl = ""
    st.session_state.claim_review_export_jsonl = ""
    st.session_state.claim_review_export_scope = ""
    # An invite is a one-time workspace capability. Never carry it across an
    # identity/workspace/role boundary where it could be shown under the wrong
    # tenant and delivered to the wrong recipient.
    st.session_state.last_invite_token = ""
    _invalidate_auth_profile()
    for key in list(st.session_state):
        if str(key).startswith(("member-role-", "member-save-", "member-remove-")):
            st.session_state.pop(key, None)
    st.session_state.pop("active-workspace-picker", None)
    st.session_state.session_id = uuid.uuid4().hex
    st.query_params["sid"] = st.session_state.session_id
    st.query_params.pop("kb", None)


def _clear_account_session() -> None:
    _reset_user_context()
    st.session_state.auth_token = ""
    st.session_state.auth_user = {}
    st.session_state.auth_workspace = {}
    st.session_state.auth_workspaces = []
    st.session_state.auth_permissions = []
    st.session_state.last_invite_token = ""


def _invalidate_auth_profile() -> None:
    st.session_state.auth_me_loaded_for = ""
    st.session_state.auth_me_loaded_at = 0.0


def _observe_authenticated_response(status_code: int) -> None:
    """Reconcile authentication state after a protected request is rejected."""

    if st.session_state.get("auth_mode") != "account":
        return
    if status_code == 401:
        _clear_account_session()
    elif status_code == 403:
        # A 403 can be a legitimate route-level denial, so retain the session;
        # force /auth/me on the next render to pick up a concurrent role change.
        _invalidate_auth_profile()


def _apply_auth_session(payload: object) -> bool:
    if not isinstance(payload, Mapping):
        return False
    token = payload.get("access_token")
    user = payload.get("user")
    workspace = payload.get("workspace")
    permissions = payload.get("permissions", [])
    if (
        not isinstance(token, str)
        or not token
        or not isinstance(user, Mapping)
        or not isinstance(workspace, Mapping)
        or not isinstance(permissions, list)
    ):
        return False
    previous_token = str(st.session_state.get("auth_token") or "")
    previous_workspace = st.session_state.get("auth_workspace", {})
    previous_workspace_id = (
        str(previous_workspace.get("workspace_id") or "")
        if isinstance(previous_workspace, Mapping)
        else ""
    )
    workspace_id = str(workspace.get("workspace_id") or "")
    if not workspace_id:
        return False
    if token != previous_token or workspace_id != previous_workspace_id:
        _reset_user_context()
    st.session_state.auth_token = token
    st.session_state.auth_mode = "account"
    st.session_state.auth_user = dict(user)
    st.session_state.auth_workspace = dict(workspace)
    st.session_state.auth_permissions = [str(value) for value in permissions]
    _invalidate_auth_profile()
    return True


def _apply_auth_profile(payload: object) -> bool:
    if not isinstance(payload, Mapping):
        return False
    user = payload.get("user")
    workspace = payload.get("workspace")
    workspaces = payload.get("workspaces", [])
    permissions = payload.get("permissions", [])
    if (
        not isinstance(user, Mapping)
        or not isinstance(workspace, Mapping)
        or not isinstance(workspaces, list)
        or not isinstance(permissions, list)
    ):
        return False
    previous_user = st.session_state.get("auth_user", {})
    previous_workspace = st.session_state.get("auth_workspace", {})
    previous_permissions = st.session_state.get("auth_permissions", [])
    previous_user_id = (
        str(previous_user.get("user_id") or "")
        if isinstance(previous_user, Mapping)
        else ""
    )
    previous_workspace_id = (
        str(previous_workspace.get("workspace_id") or "")
        if isinstance(previous_workspace, Mapping)
        else ""
    )
    previous_role = (
        str(previous_workspace.get("role") or "")
        if isinstance(previous_workspace, Mapping)
        else ""
    )
    user_id = str(user.get("user_id") or "")
    workspace_id = str(workspace.get("workspace_id") or "")
    role = str(workspace.get("role") or "")
    if not user_id or not workspace_id or not role:
        return False
    identity_changed = bool(previous_user_id and previous_user_id != user_id)
    workspace_changed = bool(
        previous_workspace_id and previous_workspace_id != workspace_id
    )
    authorization_changed = bool(
        previous_role
        and (
            previous_role != role
            or {str(value) for value in previous_permissions}
            != {str(value) for value in permissions}
        )
    )
    if identity_changed or workspace_changed or authorization_changed:
        # This also stops background requests carrying the former authority and
        # removes cached KB/document data before applying the server profile.
        _reset_user_context()
    st.session_state.auth_user = dict(user)
    st.session_state.auth_workspace = dict(workspace)
    st.session_state.auth_workspaces = [
        dict(value) for value in workspaces if isinstance(value, Mapping)
    ]
    st.session_state.auth_permissions = [str(value) for value in permissions]
    return True


def _auth_config() -> Mapping | None:
    base_url = str(st.session_state.api_url).rstrip("/")
    cached = st.session_state.auth_config_by_url.get(base_url)
    if isinstance(cached, Mapping):
        return cached
    try:
        response = CogDocClient(base_url, api_key="").get_auth_config()
    except Exception as exc:
        st.error(f"连接身份服务失败: {exc}")
        return None
    if response.status_code == 404:
        # Older/local backends without the capability endpoint remain usable.
        config = {
            "account_auth_enabled": False,
            "self_registration_enabled": False,
        }
    elif response.status_code == 200:
        payload = response_payload(response)
        if not isinstance(payload, Mapping):
            st.error("身份服务返回了无效配置。")
            return None
        config = dict(payload)
    else:
        st.error(_response_error(response, "读取身份配置失败"))
        return None
    st.session_state.auth_config_by_url[base_url] = config
    return config


def _refresh_auth_profile(*, force: bool = False) -> bool:
    token = str(st.session_state.auth_token or "")
    fingerprint = hashlib.sha256(token.encode("utf-8")).hexdigest()
    now = time.monotonic()
    loaded_at = float(st.session_state.get("auth_me_loaded_at") or 0.0)
    if (
        not force
        and st.session_state.auth_me_loaded_for == fingerprint
        and 0.0 <= now - loaded_at < AUTH_PROFILE_TTL_SECONDS
    ):
        return True
    try:
        response = _client().get_me()
    except Exception as exc:
        st.error(f"读取账号信息失败: {exc}")
        return False
    if response.status_code == 404 and _current_workspace_id() is not None:
        # The tab-pinned workspace may have been removed concurrently. Retry
        # exactly once without the selector so AuthStore can fall back to the
        # user's personal workspace; _apply_auth_profile then clears old-tenant
        # UI state before applying that authoritative profile.
        try:
            response = CogDocClient(
                st.session_state.api_url,
                api_key=token,
                workspace_id=None,
            ).get_me()
        except Exception as exc:
            st.error(f"读取账号信息失败: {exc}")
            return False
    if response.status_code in {401, 403}:
        _clear_account_session()
        st.warning("登录状态已失效或权限已变更，请重新登录。")
        return False
    if response.status_code != 200:
        st.error(_response_error(response, "读取账号信息失败"))
        return False
    if not _apply_auth_profile(response_payload(response)):
        st.error("身份服务返回了无效账号信息。")
        return False
    st.session_state.auth_me_loaded_for = fingerprint
    st.session_state.auth_me_loaded_at = time.monotonic()
    return True


def _submit_auth_response(response, fallback: str) -> bool:
    if response.status_code not in (200, 201):
        st.error(_response_error(response, fallback))
        return False
    if not _apply_auth_session(response_payload(response)):
        st.error("身份服务返回了无效登录结果。")
        return False
    st.rerun()
    return True


def _render_auth_screen(config: Mapping) -> None:
    st.subheader("登录 CogDoc")
    st.caption("账号会话仅保存在当前浏览器的 Streamlit 会话中。")
    login_tab, register_tab, invite_tab = st.tabs(["登录", "注册", "接受邀请"])
    public_client = CogDocClient(st.session_state.api_url, api_key="")
    with login_tab:
        with st.form("account-login", clear_on_submit=True):
            email = st.text_input("邮箱", key="login-email")
            password = st.text_input("密码", type="password", key="login-password")
            submitted = st.form_submit_button("登录", use_container_width=True)
        if submitted:
            try:
                response = public_client.login(email.strip(), password)
                _submit_auth_response(response, "登录失败")
            except Exception as exc:
                st.error(f"登录失败: {exc}")
    with register_tab:
        if not bool(config.get("self_registration_enabled", False)):
            st.info("当前部署未开放自主注册，请使用邀请链接加入工作区。")
        else:
            with st.form("account-register", clear_on_submit=True):
                display_name = st.text_input("显示名称", key="register-name")
                email = st.text_input("邮箱", key="register-email")
                workspace_name = st.text_input(
                    "个人工作区名称（可选）", key="register-workspace"
                )
                password = st.text_input(
                    "密码（至少 12 位）", type="password", key="register-password"
                )
                submitted = st.form_submit_button("创建账号", use_container_width=True)
            if submitted:
                try:
                    response = public_client.register(
                        email.strip(),
                        password,
                        display_name.strip(),
                        workspace_name.strip() or None,
                    )
                    _submit_auth_response(response, "注册失败")
                except Exception as exc:
                    st.error(f"注册失败: {exc}")
    with invite_tab:
        with st.form("anonymous-invite", clear_on_submit=True):
            token = st.text_input("邀请令牌", type="password", key="invite-token")
            email = st.text_input("受邀邮箱", key="invite-email")
            display_name = st.text_input("显示名称", key="invite-name")
            password = st.text_input("密码", type="password", key="invite-password")
            submitted = st.form_submit_button("接受邀请", use_container_width=True)
        if submitted:
            try:
                response = public_client.accept_workspace_invite(
                    token.strip(),
                    email=email.strip(),
                    password=password,
                    display_name=display_name.strip() or None,
                )
                _submit_auth_response(response, "接受邀请失败")
            except Exception as exc:
                st.error(f"接受邀请失败: {exc}")


def _auth_gate() -> bool:
    with st.sidebar:
        st.title("CogDoc")
        previous_url = str(st.session_state.api_url)
        entered_url = st.text_input("后端地址", previous_url, key="auth-api-url")
        if entered_url.rstrip("/") != previous_url.rstrip("/"):
            _clear_account_session()
            st.session_state.api_url = entered_url.rstrip("/")
            st.rerun()
    config = _auth_config()
    if config is None:
        return False
    if not bool(config.get("account_auth_enabled", False)):
        st.session_state.auth_mode = "legacy"
        return True
    if st.session_state.auth_token:
        st.session_state.auth_mode = "account"
        return _refresh_auth_profile()
    if os.getenv("COGDOC_API_KEY", ""):
        # Static API principals remain a supported automation/local deployment
        # path even when human account auth is enabled alongside them.
        st.session_state.auth_mode = "api_key"
        return True
    _render_auth_screen(config)
    return False


# 处理上下文键。
def _context_key(kb_id: str, session_id: str | None = None) -> tuple[str, str]:
    return (kb_id, session_id or st.session_state.session_id)


# 处理消息列表。
def _messages_for(kb_id: str, session_id: str | None = None) -> list[dict]:
    return st.session_state.messages_by_context.setdefault(
        _context_key(kb_id, session_id), []
    )


# 从历史构造消息。
def _message_from_history(turn: Mapping, fallback_query: str = "") -> dict:
    metadata = turn.get("metadata") if isinstance(turn.get("metadata"), Mapping) else {}
    trace_id = turn.get("trace_id") or metadata.get("trace_id")
    query = turn.get("query") or metadata.get("query") or fallback_query
    if trace_id and query:
        st.session_state.trace_labels[str(trace_id)] = str(query)
    msg = {
        "role": turn.get("role", "assistant"),
        "content": turn.get("content", ""),
        "id": _next_id(),
    }
    if trace_id:
        msg["final"] = {
            "trace_id": trace_id,
            "task_type": turn.get("task_type") or metadata.get("task_type") or "-",
            "is_valid": True,
        }
        msg["query"] = query
    return msg


# 从历史列表构造消息。
def _messages_from_history(turns: list[Mapping]) -> list[dict]:
    messages = []
    last_user_query = ""
    for turn in turns:
        if turn.get("role") == "user":
            last_user_query = str(turn.get("content") or "")
            messages.append(_message_from_history(turn))
        else:
            messages.append(_message_from_history(turn, fallback_query=last_user_query))
    return messages


# 恢复历史记录。
def _restore_history(kb_id: str) -> None:
    # 知识库或会话变化时重载历史，同一上下文内不重复拉取。
    marker = _context_key(kb_id)
    if marker in st.session_state.restored_contexts:
        return
    if st.session_state.messages_by_context.get(marker):
        st.session_state.restored_contexts.add(marker)
        return
    try:
        resp = _client().get_session_history(st.session_state.session_id, kb_id)
        turns = resp.json().get("messages", []) if resp.status_code == 200 else []
    except Exception:
        turns = []
    st.session_state.messages_by_context[marker] = _messages_from_history(turns)
    st.session_state.restored_contexts.add(marker)


# 构造标签。
def _page_label(page) -> str:
    # 页码可能为空，避免渲染成无效页码。
    return f" · P{page}" if page is not None else ""


# 切换会话。
def _switch_session(session_id: str) -> None:
    # 切换或新建对话时同步地址栏，消息按上下文分桶。
    st.session_state.session_id = session_id
    st.query_params["sid"] = session_id
    st.rerun()


# 处理对话列表。
def _conversations(client: CogDocClient, kb_id: str) -> None:
    # 多对话列表：前端已知会话 ∪ 后端已存会话，新建/切换/删除，全部可点。
    st.subheader("对话")
    current = st.session_state.session_id
    known = st.session_state.known_sessions.setdefault(kb_id, [])
    if current not in known:
        known.insert(0, current)

    if st.button("➕ 新对话", use_container_width=True):
        new_id = uuid.uuid4().hex
        st.session_state.known_sessions.setdefault(kb_id, []).insert(0, new_id)
        _switch_session(new_id)

    try:
        status_code, payload = _cached_api_value(
            ("sessions", client.base_url, kb_id),
            lambda: _response_status_payload(client.list_sessions(kb_id)),
        )
        if status_code == 200:
            sessions = (
                payload.get("sessions", []) if isinstance(payload, Mapping) else []
            )
            backend = {
                s["session_id"]: s
                for s in sessions
                if isinstance(s, Mapping) and isinstance(s.get("session_id"), str)
            }
        else:
            st.warning(
                "读取会话列表失败: "
                f"{format_api_error(payload, status_code, '读取会话列表失败')}"
            )
            backend = {}
    except Exception as exc:
        st.warning(f"读取会话列表失败: {exc}")
        backend = {}

    # 已知列表打底（含空对话），再补上后端有、前端没记的（如刷新后从别处恢复的）。
    ordered = list(known)
    for sid in backend:
        if sid not in ordered:
            ordered.append(sid)

    for sid in ordered:
        title = backend.get(sid, {}).get("title") or "新对话"
        mark = "🟢 " if sid == current else ""
        row = st.columns([5, 1])
        if row[0].button(f"{mark}{title}", key=f"sess-{sid}", use_container_width=True):
            _switch_session(sid)
        if row[1].button("🗑", key=f"sessdel-{sid}"):
            client.delete_session(sid, kb_id)
            _clear_api_cache(("sessions", client.base_url, kb_id))
            if sid in known:
                known.remove(sid)
            if sid == current:
                _switch_session(uuid.uuid4().hex)
            st.rerun()


# 发送反馈。
def _send_feedback(final: dict, query: str, feedback: str) -> str:
    # 凭该回答的跟踪标识提交赞踩，并关联问题和答案。
    return _submit_feedback(final, query, feedback)


# 提交反馈。
def _submit_feedback(
    final: dict,
    query: str,
    feedback: str,
    comment: str | None = None,
    correction: str | None = None,
    save_as_knowledge: bool = False,
    skip_retrieval_feedback: bool = False,
    certainty: str | None = None,
    feedback_type: str | None = None,
) -> str:
    trace_id = final.get("trace_id")
    if not trace_id:
        st.toast("缺少 trace_id，无法提交反馈")
        return "error"
    client = _client()
    resp = client.submit_feedback(
        trace_id=trace_id,
        feedback=feedback,
        kb_id=st.session_state.kb_id,
        query=query,
        answer=final.get("answer", ""),
        citations=final.get("citations") or [],
        evidence=final.get("evidence") or [],
        comment=comment,
        correction=correction,
        feedback_type=(
            feedback_type or ("correction" if feedback == "correction" else None)
        ),
        feedback_text=comment,
        correction_text=correction,
        save_as_knowledge=save_as_knowledge,
        skip_retrieval_feedback=skip_retrieval_feedback,
        related_source=_first_citation_source(final),
        related_source_sha256=_first_evidence_source_sha(final),
        related_chunk_ids=_citation_chunk_ids(final),
        related_page_start=_first_citation_page_start(final),
        related_page_end=_first_citation_page_end(final),
        related_chunk_text_hash=_first_evidence_text_hash(final),
        related_anchor_text=_first_evidence_anchor_text(final),
        certainty=certainty,
    )
    if resp.status_code == 201 and st.session_state.kb_id:
        _clear_api_cache(("feedback", client.base_url, st.session_state.kb_id))
        _clear_api_cache(("feedback-analysis", client.base_url, st.session_state.kb_id))
        _clear_api_cache(
            ("retrieval-feedback", client.base_url, st.session_state.kb_id)
        )
        _clear_api_cache(("review-queue", client.base_url, st.session_state.kb_id))
        _clear_api_cache(
            ("review-queue-export", client.base_url, st.session_state.kb_id)
        )
        _clear_api_cache(("pending-count", client.base_url, st.session_state.kb_id))
        _clear_api_cache(
            ("feedback-loop-metrics", client.base_url, st.session_state.kb_id)
        )
    if resp.status_code == 201:
        payload = response_payload(resp)
        status = payload.get("status") if isinstance(payload, Mapping) else ""
        st.toast("这条回答已有反馈" if status == "duplicate_ignored" else "反馈已记录")
        return str(status or "recorded")
    else:
        st.toast(f"反馈失败: {resp.status_code}")
        return "error"


# 保存回答为派生知识。
def _save_answer_as_knowledge(
    final: Mapping,
    query: str,
    *,
    source_note: str | None = None,
    certainty: str = "medium",
) -> None:
    kb_id = st.session_state.kb_id
    answer = str(final.get("answer") or "").strip()
    if not kb_id:
        st.toast("先选择知识库")
        return
    if not answer:
        st.toast("当前回答为空，无法保存")
        return
    note = source_note or (f"保存自问答：{query}" if query else "保存自问答")
    client = _client()
    resp = client.create_knowledge(
        kb_id=kb_id,
        text=answer,
        related_source=_first_citation_source(final),
        related_source_sha256=_first_evidence_source_sha(final),
        related_chunk_ids=_citation_chunk_ids(final),
        related_page_start=_first_citation_page_start(final),
        related_page_end=_first_citation_page_end(final),
        related_chunk_text_hash=_first_evidence_text_hash(final),
        related_anchor_text=_first_evidence_anchor_text(final),
        source_note=note,
        certainty=certainty,
        origin="saved_answer",
        created_from_trace_id=str(final.get("trace_id") or "") or None,
        created_by="frontend",
    )
    if resp.status_code in (200, 201):
        _clear_knowledge_cache(client, kb_id)
        st.toast("答案已保存到补充知识库")
    else:
        st.toast(f"保存失败: {resp.status_code}")


# 读取首个引用来源。
def _first_citation_source(final: Mapping) -> str | None:
    for item in final.get("citations") or []:
        if isinstance(item, Mapping) and item.get("source"):
            return str(item["source"])
    return None


# 读取首个证据来源哈希。
def _first_evidence_source_sha(final: Mapping) -> str | None:
    for item in final.get("evidence") or []:
        if isinstance(item, Mapping) and item.get("source_sha256"):
            return str(item["source_sha256"])
    return None


# 读取引用分块标识列表。
def _citation_chunk_ids(final: Mapping) -> list[str]:
    chunk_ids = []
    for item in final.get("citations") or []:
        if isinstance(item, Mapping) and item.get("chunk_id"):
            chunk_ids.append(str(item["chunk_id"]))
    return chunk_ids


# 读取首个引用页码起点。
def _first_citation_page_start(final: Mapping) -> int | None:
    for item in final.get("citations") or []:
        if not isinstance(item, Mapping):
            continue
        value = item.get("page_start", item.get("page"))
        parsed = _parse_optional_int(value)
        if parsed is not None:
            return parsed
    return None


# 读取首个引用页码终点。
def _first_citation_page_end(final: Mapping) -> int | None:
    for item in final.get("citations") or []:
        if not isinstance(item, Mapping):
            continue
        value = item.get("page_end", item.get("page"))
        parsed = _parse_optional_int(value)
        if parsed is not None:
            return parsed
    return None


# 读取首个证据文本锚点。
def _first_evidence_anchor_text(final: Mapping) -> str | None:
    for item in final.get("evidence") or []:
        if isinstance(item, Mapping) and item.get("text_preview"):
            return str(item["text_preview"]).strip()[:240]
    return None


# 读取首个证据文本哈希。
def _first_evidence_text_hash(final: Mapping) -> str | None:
    anchor = _first_evidence_anchor_text(final)
    if not anchor:
        return None
    return hashlib.sha256(anchor.encode("utf-8")).hexdigest()


# 解析可空页码。
def _parse_optional_int(value) -> int | None:
    if value in (None, ""):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


# 加载跟踪。
def _load_trace(trace_id: str, force: bool = False) -> None:
    trace_id = trace_id.strip()
    if not trace_id:
        return
    if force:
        st.session_state.trace_cache.pop(trace_id, None)
    elif trace_id in st.session_state.trace_cache:
        return
    try:
        resp = _client().get_trace(trace_id)
        if resp.status_code == 200:
            payload = response_payload(resp)
            st.session_state.trace_cache[trace_id] = payload
            if isinstance(payload, Mapping):
                query = (payload.get("config") or {}).get("query_preview")
                if query:
                    st.session_state.trace_labels[trace_id] = str(query)
        else:
            st.session_state.trace_cache[trace_id] = {
                "error": _response_error(resp, "读取 trace 失败")
            }
    except Exception as exc:
        st.session_state.trace_cache[trace_id] = {"error": str(exc)}


# 加载会话跟踪。
def _load_session_traces(kb_id: str | None, force: bool = False) -> None:
    if not kb_id:
        return
    marker = _context_key(kb_id)
    if not force and marker in st.session_state.trace_session_loaded:
        return
    try:
        resp = _client().list_traces(
            limit=30,
            kb_id=kb_id,
            session_id=st.session_state.session_id,
        )
        if resp.status_code != 200:
            st.session_state.trace_session_error[marker] = _response_error(
                resp, "读取当前会话 trace 失败"
            )
            st.session_state.trace_session_items_by_context[marker] = []
            st.session_state.trace_session_loaded.add(marker)
            return
        payload = response_payload(resp)
        traces = payload.get("traces", []) if isinstance(payload, Mapping) else []
        items = [
            trace
            for trace in traces
            if isinstance(trace, Mapping) and isinstance(trace.get("trace_id"), str)
        ]
        st.session_state.trace_session_items_by_context[marker] = items
        st.session_state.trace_session_error.pop(marker, None)
        st.session_state.trace_session_loaded.add(marker)
        for trace in items:
            query = str(trace.get("query_preview") or "").strip()
            if query:
                st.session_state.trace_labels[trace["trace_id"]] = query
    except Exception as exc:
        st.session_state.trace_session_error[marker] = str(exc)
        st.session_state.trace_session_items_by_context[marker] = []
        st.session_state.trace_session_loaded.add(marker)


# 处理跟踪选项标签。
def _trace_option_label(trace_id: str) -> str:
    if not trace_id:
        return "选择最近 trace"
    option_trace = st.session_state.trace_options_by_id.get(trace_id)
    if option_trace:
        status = option_trace.get("status", "-")
        task = option_trace.get("task_type", "-")
        title = _trace_query_title(trace_id, option_trace)
        suffix = _format_duration(option_trace.get("duration_ms"))
        duration = f" · {suffix}" if suffix else ""
        return f"{title} · {task} · {status}{duration}"
    return _trace_query_title(trace_id, {})


# 处理跟踪查询标题。
def _trace_query_title(trace_id: str, trace: Mapping) -> str:
    query = str(trace.get("query_preview") or "").strip()
    if not query:
        query = str(st.session_state.trace_labels.get(trace_id, "")).strip()
    return query or "未记录问题"


# 格式化耗时。
def _format_duration(duration_ms) -> str:
    if duration_ms is None:
        return ""
    try:
        value = float(duration_ms)
    except (TypeError, ValueError):
        return str(duration_ms)
    if value >= 1000:
        return f"{value / 1000:.1f}s"
    return f"{value:.0f} ms"


# 格式化页码范围。
def _page_range_label(page_start, page_end=None) -> str:
    if page_start is None and page_end is None:
        return ""
    if page_end is None or page_start == page_end:
        return f"P{page_start}"
    if page_start is None:
        return f"P{page_end}"
    return f"P{page_start}-{page_end}"


# 判断回答是否缺少可用证据。
def _is_no_evidence_final(final: Mapping) -> bool:
    answer = str(final.get("answer") or "")
    if NO_REFERENCE_ANSWER in answer:
        return True
    if not final.get("is_valid"):
        return True
    if not final.get("citations") and not final.get("evidence"):
        return True
    return "未明确" in answer or "无答案" in answer


def _render_no_evidence_knowledge_form(final: dict, key: str, query: str) -> None:
    with st.expander("补充知识", expanded=True):
        with st.form(f"no-evidence-knowledge-{key}", clear_on_submit=True):
            supplement = st.text_area(
                "正确说法",
                key=f"no-evidence-correction-{key}",
                height=120,
            )
            note = st.text_area(
                "来源说明",
                value=f"补充自无答案问题：{query}" if query else "补充自无答案问题",
                key=f"no-evidence-note-{key}",
                height=68,
            )
            certainty = st.selectbox(
                "可信度",
                ["medium", "high", "low"],
                format_func=lambda value: {
                    "high": "高",
                    "medium": "中",
                    "low": "低",
                }[value],
                key=f"no-evidence-certainty-{key}",
            )
            submitted = st.form_submit_button("保存为待审核派生知识")
        if submitted:
            if supplement.strip():
                _submit_feedback(
                    final,
                    query,
                    "correction",
                    comment=note.strip() or None,
                    correction=supplement.strip(),
                    save_as_knowledge=True,
                    skip_retrieval_feedback=True,
                    certainty=certainty,
                    feedback_type="no_evidence",
                )
            else:
                st.warning("请输入正确说法。")


def _render_save_answer_form(final: dict, key: str, query: str) -> None:
    with st.expander("保存到补充知识库", expanded=True):
        with st.form(f"save-answer-{key}", clear_on_submit=True):
            save_note = st.text_area("来源说明", key=f"save-note-{key}", height=80)
            save_certainty = st.selectbox(
                "可信度",
                ["medium", "high", "low"],
                format_func=lambda value: {
                    "high": "高",
                    "medium": "中",
                    "low": "低",
                }[value],
                key=f"save-answer-certainty-{key}",
            )
            save_submitted = st.form_submit_button("保存到补充知识库")
        if save_submitted:
            _save_answer_as_knowledge(
                final,
                query,
                source_note=save_note.strip() or None,
                certainty=save_certainty,
            )
            st.session_state.feedback_action_by_message.pop(
                _feedback_action_key(final, key), None
            )
            st.rerun()


def _render_correction_form(final: dict, key: str, query: str) -> None:
    with st.expander("纠错", expanded=True):
        with st.form(f"correction-{key}", clear_on_submit=True):
            comment = st.text_area("备注", key=f"comment-{key}", height=80)
            correction = st.text_area(
                "纠正答案", key=f"correction-text-{key}", height=120
            )
            certainty = st.selectbox(
                "可信度",
                ["medium", "high", "low"],
                format_func=lambda value: {
                    "high": "高",
                    "medium": "中",
                    "low": "低",
                }[value],
                key=f"correction-certainty-{key}",
            )
            submitted = st.form_submit_button("提交纠错")
        if submitted:
            if not correction.strip():
                st.warning("请输入纠正答案。")
                return
            _submit_feedback(
                final,
                query,
                "correction",
                comment=comment.strip() or None,
                correction=correction.strip(),
                save_as_knowledge=True,
                certainty=certainty,
            )
            st.session_state.feedback_action_by_message.pop(
                _feedback_action_key(final, key), None
            )
            st.rerun()


# 构造反馈动作状态键。
def _feedback_action_key(final: Mapping, key: str) -> str:
    return str(final.get("trace_id") or key)


# 处理赞踩点击，先锁住同一条回答的两个按钮，再提交反馈。
def _handle_quick_feedback_click(
    final: dict, key: str, query: str, feedback: str, recorded_action: str
) -> None:
    action_key = _feedback_action_key(final, key)
    if st.session_state.feedback_action_by_message.get(action_key):
        return
    st.session_state.feedback_action_by_message[action_key] = "locked"
    result = _send_feedback(final, query, feedback)
    if result == "recorded":
        st.session_state.feedback_action_by_message[action_key] = recorded_action
    elif result == "duplicate_ignored":
        st.session_state.feedback_action_by_message[action_key] = "locked"
    else:
        st.session_state.feedback_action_by_message.pop(action_key, None)


# 格式化引用来源标签。
def _citation_label(item: Mapping) -> str:
    if item.get("source_type") == "derived_knowledge":
        knowledge_id = item.get("knowledge_id") or str(
            item.get("chunk_id", "")
        ).replace("knowledge:", "")
        return f"补充知识 · `{knowledge_id}`"
    return (
        f"**{item.get('source', '')}**"
        f"{_page_label(item.get('page'))} · `{item.get('chunk_id', '')}`"
    )


# 构建流式预览文本。
def _stream_preview(answer: str | None) -> str:
    answer = str(answer or "")
    if len(answer) <= STREAM_PREVIEW_HEAD_CHARS + STREAM_PREVIEW_TAIL_CHARS:
        return answer + "▌" if answer else "正在思考…"
    omitted = len(answer) - STREAM_PREVIEW_HEAD_CHARS - STREAM_PREVIEW_TAIL_CHARS
    return (
        answer[:STREAM_PREVIEW_HEAD_CHARS]
        + f"\n\n... 已生成 {len(answer)} 字，暂折叠中间 {omitted} 字，完成后显示全文 ...\n\n"
        + answer[-STREAM_PREVIEW_TAIL_CHARS:]
        + "▌"
    )


# 处理当前跟踪项。
def _current_trace_items(kb_id: str | None) -> list[dict]:
    items = []
    seen = set()
    for msg in reversed(_messages_for(kb_id) if kb_id else []):
        final = msg.get("final") or {}
        trace_id = final.get("trace_id")
        if not trace_id or trace_id in seen:
            continue
        seen.add(trace_id)
        cached = st.session_state.trace_cache.get(str(trace_id))
        item = {"trace_id": str(trace_id)}
        if isinstance(cached, Mapping) and not isinstance(cached.get("error"), str):
            item.update(
                {
                    "query_preview": (cached.get("config") or {}).get("query_preview"),
                    "task_type": cached.get("task_type"),
                    "status": cached.get("status"),
                    "duration_ms": cached.get("duration_ms"),
                }
            )
        query = msg.get("query") or st.session_state.trace_labels.get(trace_id, "")
        if query and not item.get("query_preview"):
            item["query_preview"] = str(query)
        if final.get("task_type") and not item.get("task_type"):
            item["task_type"] = final.get("task_type")
        items.append(item)
    if kb_id:
        marker = _context_key(kb_id)
        for trace in st.session_state.trace_session_items_by_context.get(marker, []):
            trace_id = trace.get("trace_id")
            if not trace_id or trace_id in seen:
                continue
            seen.add(trace_id)
            items.append(dict(trace))
    return items


# 处理跟踪节点键。
def _trace_node_key(node_name: str) -> str:
    tail = (node_name or "").rsplit(".", 1)[-1]
    if ":" in tail:
        return tail.split(":", 1)[0]
    return tail


# 处理跟踪步骤标签。
def _trace_step_label(step: Mapping, idx: int) -> str:
    node_name = str(step.get("node_name") or f"step-{idx + 1}")
    node_key = _trace_node_key(node_name)
    title = TRACE_NODE_LABELS.get(node_key, node_key)
    duration = step.get("duration_ms")
    label = f"{idx + 1}. {title}"
    if node_key != title:
        label += f" · {node_key}"
    formatted = _format_duration(duration)
    if formatted:
        label += f" · {formatted}"
    return label


# 渲染跟踪步骤。
def _render_trace_step(step: Mapping, idx: int) -> None:
    with st.expander(_trace_step_label(step, idx)):
        if step.get("node_name"):
            st.caption(f"原始节点: {step.get('node_name')}")
        details = []
        if step.get("task_type"):
            details.append(f"任务: {step.get('task_type')}")
        if step.get("model"):
            details.append(f"模型: {step.get('model')}")
        if step.get("retrieval_top_k") is not None:
            details.append(f"top_k: {step.get('retrieval_top_k')}")
        if step.get("error_class"):
            details.append(f"错误: {step.get('error_class')}")
        if details:
            cols = st.columns(len(details))
            for col, text in zip(cols, details):
                col.caption(text)

        if step.get("router_reason"):
            st.markdown("**路由理由**")
            st.write(step["router_reason"])
        rewritten = step.get("rewritten_queries") or []
        if rewritten:
            st.markdown("**改写查询**")
            for query in rewritten:
                st.write(f"- {query}")
        elif (step.get("counts") or {}).get("rewritten_query_count"):
            st.caption("此旧 trace 只记录了改写数量，未保存具体改写查询。")
        if step.get("critique"):
            st.markdown("**校验反馈**")
            st.write(step["critique"])
        counts = step.get("counts") or {}
        if counts:
            st.markdown("**计数**")
            st.json(counts)
        evidence = step.get("evidence") or []
        if evidence:
            st.markdown("**证据预览**")
            for item in evidence:
                source = item.get("source", "")
                chunk_id = item.get("chunk_id", "")
                st.caption(f"{source}{_page_label(item.get('page'))} · `{chunk_id}`")
                st.write(item.get("text_preview", ""))


# 渲染跟踪调试。
def _render_trace_debug(trace: dict) -> None:
    trace_error = trace.get("error")
    if isinstance(trace_error, str):
        st.error(trace["error"])
        return
    summary = trace.get("summary") or {}
    meta = st.columns(4)
    meta[0].caption(f"状态: {trace.get('status') or '-'}")
    meta[1].caption(f"任务: {trace.get('task_type') or '-'}")
    meta[2].caption(f"耗时: {_format_duration(trace.get('duration_ms')) or '-'}")
    meta[3].caption(f"步骤: {summary.get('step_count', 0)}")
    if trace.get("config"):
        with st.expander("请求配置"):
            st.json(trace["config"])
    if trace_error:
        with st.expander("运行错误", expanded=True):
            st.json(trace_error)
    for idx, step in enumerate(trace.get("steps") or []):
        if isinstance(step, Mapping):
            _render_trace_step(step, idx)


# 渲染跟踪查询。
def _render_trace_lookup(kb_id: str | None) -> None:
    st.subheader("Trace 调试")
    _load_session_traces(kb_id)
    left, right = st.columns([1, 2])
    with left:
        traces = _current_trace_items(kb_id)
        trace_ids = {trace["trace_id"] for trace in traces}
        if st.session_state.active_trace_id not in trace_ids:
            st.session_state.active_trace_id = ""
        marker = _context_key(kb_id) if kb_id else None
        if marker and st.session_state.trace_session_error.get(marker):
            st.warning(st.session_state.trace_session_error[marker])
        if not traces:
            st.info("当前对话还没有 trace。发送问题后这里会显示。")
        if traces:
            st.session_state.trace_options_by_id = {
                trace["trace_id"]: trace for trace in traces
            }
            status_options = ["全部"] + sorted(
                {str(trace.get("status") or "-") for trace in traces}
            )
            task_options = ["全部"] + sorted(
                {str(trace.get("task_type") or "-") for trace in traces}
            )
            status_filter = st.selectbox(
                "状态", status_options, key="trace-status-filter"
            )
            task_filter = st.selectbox("任务", task_options, key="trace-task-filter")
            filtered = [
                trace
                for trace in traces
                if (status_filter == "全部" or trace.get("status") == status_filter)
                and (task_filter == "全部" or trace.get("task_type") == task_filter)
            ]
            options = [""] + [trace["trace_id"] for trace in filtered]
            selected = st.selectbox(
                "当前对话请求",
                options,
                format_func=_trace_option_label,
                key="trace_recent_select",
            )
            if selected and st.button(
                "打开选中 trace", key="trace-open-selected", use_container_width=True
            ):
                st.session_state.active_trace_id = selected
                _load_trace(selected)

    with right:
        active = st.session_state.active_trace_id
        if not active:
            st.info("选择当前对话中的请求查看 trace。")
            return
        top = st.columns([3, 1])
        top[0].markdown(f"**{_trace_query_title(active, {})}**")
        top[0].caption(f"trace_id: {active}")
        if top[1].button("刷新 trace", key="trace-refresh", use_container_width=True):
            _load_session_traces(kb_id, force=True)
            _load_trace(active, force=True)
        if active in st.session_state.trace_cache:
            _render_trace_debug(st.session_state.trace_cache[active])


# 渲染证据。
def _render_evidence(final: dict, key: str, query: str = "") -> None:
    # 渲染一条回答的元信息 + 引用/证据面板 + 赞踩按钮（消费结构化字段）。
    meta = st.columns(3)
    meta[0].caption(f"任务: {final.get('task_type', '-')}")
    meta[1].caption(f"引用校验: {'通过' if final.get('is_valid') else '未通过'}")
    meta[2].caption(f"trace: {(final.get('trace_id') or '')[:8]}")

    citations = final.get("citations") or []
    evidence = final.get("evidence") or []
    if citations:
        with st.expander(f"📌 引用来源 ({len(citations)})"):
            for c in citations:
                st.write(f"- {_citation_label(c)}")
    if evidence:
        with st.expander(f"🧩 证据片段 ({len(evidence)})"):
            for e in evidence:
                if e.get("source_type") == "derived_knowledge":
                    knowledge_id = e.get("knowledge_id") or str(
                        e.get("chunk_id", "")
                    ).replace("knowledge:", "")
                    st.markdown(f"**补充知识** `{knowledge_id}`")
                else:
                    st.markdown(
                        f"**{e.get('source', '')}**{_page_label(e.get('page'))}"
                    )
                st.caption(e.get("text_preview", ""))
                retrieval = e.get("retrieval") if isinstance(e, Mapping) else {}
                if isinstance(retrieval, Mapping) and retrieval.get("search_channel"):
                    terms = ", ".join(retrieval.get("matched_terms") or [])
                    coverage = retrieval.get("match_coverage")
                    density = retrieval.get("match_density")
                    details = [f"通道: {retrieval.get('search_channel')}"]
                    if terms:
                        details.append(f"匹配词: {terms}")
                    if isinstance(coverage, (int, float)):
                        details.append(f"覆盖率: {coverage:.2f}")
                    if isinstance(density, (int, float)):
                        details.append(f"密度: {density:.2f}")
                    st.caption(" · ".join(details))

    if _is_no_evidence_final(final):
        _render_no_evidence_knowledge_form(final, key, query)
    else:
        fb = st.columns([1, 1, 6])
        action_key = _feedback_action_key(final, key)
        action = st.session_state.feedback_action_by_message.get(action_key)
        feedback_locked = bool(action)
        fb[0].button(
            "👍",
            key=f"up-{key}",
            disabled=feedback_locked,
            on_click=_handle_quick_feedback_click,
            args=(final, key, query, "thumbs_up", "save"),
        )
        fb[1].button(
            "👎",
            key=f"down-{key}",
            disabled=feedback_locked,
            on_click=_handle_quick_feedback_click,
            args=(final, key, query, "thumbs_down", "correct"),
        )

        if action == "save":
            _render_save_answer_form(final, key, query)
        elif action == "correct":
            _render_correction_form(final, key, query)

    trace_id = final.get("trace_id")
    if trace_id:
        if query:
            st.session_state.trace_labels[trace_id] = query


# 渲染来源分块浏览。
def _render_source_browser(client: CogDocClient, kb_id: str) -> None:
    with st.expander("索引内容"):
        try:
            status_code, payload = _cached_api_value(
                ("sources", client.base_url, kb_id),
                lambda: _response_status_payload(client.list_sources(kb_id)),
            )
        except Exception as exc:
            st.warning(f"读取来源文件失败: {exc}")
            return
        if status_code != 200:
            st.warning(
                "读取来源文件失败: "
                f"{format_api_error(payload, status_code, '读取来源文件失败')}"
            )
            return
        sources = payload.get("sources", []) if isinstance(payload, Mapping) else []
        sources = [str(source) for source in sources if source]
        if not sources:
            st.caption("暂无已索引 source")
            return
        selected = st.selectbox("source", sources, key=f"source-browser-{kb_id}")
        if not st.checkbox("加载 chunk 预览", key=f"source-browser-load-{kb_id}"):
            return
        chunk_limit = st.selectbox(
            "显示数量", [10, 20, 50], index=1, key=f"source-browser-limit-{kb_id}"
        )
        try:
            status_code, payload = _cached_api_value(
                ("chunks", client.base_url, kb_id, selected, 0, chunk_limit),
                lambda: _response_status_payload(
                    client.list_source_chunks(kb_id, selected, limit=chunk_limit)
                ),
            )
        except Exception as exc:
            st.warning(f"读取 chunks 失败: {exc}")
            return
        if status_code != 200:
            st.warning(
                "读取 chunks 失败: "
                f"{format_api_error(payload, status_code, '读取 chunks 失败')}"
            )
            return
        chunks = payload.get("chunks", []) if isinstance(payload, Mapping) else []
        total = (
            payload.get("total_count", len(chunks))
            if isinstance(payload, Mapping)
            else len(chunks)
        )
        st.caption(f"{selected} · {total} chunks")
        for chunk in chunks:
            if not isinstance(chunk, Mapping):
                continue
            chunk_id = str(chunk.get("chunk_id") or "")
            page_start = chunk.get("page_start", chunk.get("page"))
            page_end = chunk.get("page_end", page_start)
            page_label = _page_range_label(page_start, page_end)
            prefix = f"{page_label} · " if page_label else ""
            st.caption(f"{prefix}`{chunk_id}`")
            if chunk.get("context_preview"):
                st.caption(str(chunk.get("context_preview")))
            st.write(str(chunk.get("text_preview") or ""))


# 格式化检索分数。
def _score_label(value) -> str:
    if value is None:
        return "-"
    try:
        return f"{float(value):.4f}"
    except (TypeError, ValueError):
        return str(value)


# 渲染检索命中。
def _render_retrieve_hit(hit: Mapping) -> None:
    rank = hit.get("rank", "-")
    source = hit.get("source") or "-"
    page = _page_range_label(
        hit.get("page_start", hit.get("page")), hit.get("page_end")
    )
    chunk_id = hit.get("chunk_id") or "-"
    score = _score_label(hit.get("rerank_score"))
    title = f"#{rank} · {source}"
    if page:
        title += f" · {page}"
    with st.expander(title):
        cols = st.columns(4)
        cols[0].caption(f"chunk: {chunk_id}")
        cols[1].caption(f"chunk_index: {hit.get('chunk_index')}")
        cols[2].caption(f"rerank_score: {score}")
        cols[3].caption(f"rewrite: {hit.get('rewrite_query') or '-'}")
        st.write(hit.get("text_preview") or "")
        retrieval = hit.get("retrieval") or {}
        if retrieval:
            st.markdown("**retrieval metadata**")
            st.json(retrieval)


# 读取知识库文档列表。
def _document_rows(client: CogDocClient, kb_id: str) -> list[Mapping]:
    try:
        status_code, docs = _cached_api_value(
            ("documents", client.base_url, kb_id),
            lambda: _response_status_payload(client.list_documents(kb_id)),
        )
    except Exception:
        return []
    if status_code != 200 or not isinstance(docs, list):
        return []
    return [doc for doc in docs if isinstance(doc, Mapping) and doc.get("name")]


# 读取来源分块列表。
def _source_chunk_rows(
    client: CogDocClient,
    kb_id: str,
    source: str,
    limit: int = 100,
    anchor_text: str | None = None,
) -> list[Mapping]:
    try:
        status_code, payload = _cached_api_value(
            ("chunks", client.base_url, kb_id, source, 0, limit, anchor_text),
            lambda: _response_status_payload(
                client.list_source_chunks(
                    kb_id, source, limit=limit, anchor_text=anchor_text
                )
            ),
        )
    except Exception:
        return []
    if status_code != 200 or not isinstance(payload, Mapping):
        return []
    chunks = payload.get("chunks", [])
    return [chunk for chunk in chunks if isinstance(chunk, Mapping)]


# 构建绑定摘要。
def _binding_summary(item: Mapping, prefix: str = "绑定") -> str:
    page = _page_range_label(
        item.get("related_page_start"), item.get("related_page_end")
    )
    chunks = ", ".join(str(x) for x in item.get("related_chunk_ids") or []) or "-"
    parts = [
        f"{prefix}: {item.get('related_source') or '-'}",
        f"sha {item.get('related_source_sha256') or '-'}",
        f"chunk {chunks}",
    ]
    if page:
        parts.append(page)
    if item.get("related_chunk_text_hash"):
        parts.append(f"hash {str(item.get('related_chunk_text_hash'))[:12]}")
    if item.get("related_anchor_text"):
        parts.append(f"锚点 {str(item.get('related_anchor_text'))[:40]}")
    return " · ".join(parts)


# 查找过期知识候选分块。
def _stale_rebind_candidates(item: Mapping, chunks: list[Mapping]) -> list[Mapping]:
    related_page = _parse_optional_int(item.get("related_page_start"))
    scored = []
    for chunk in chunks:
        page = _parse_optional_int(chunk.get("page_start", chunk.get("page")))
        anchor_hit = bool(chunk.get("anchor_hit"))
        page_hit = related_page is not None and page == related_page
        if not anchor_hit and not page_hit:
            continue
        priority = 0 if anchor_hit else 1
        scored.append((priority, chunk))
    return [chunk for _, chunk in sorted(scored, key=lambda item: item[0])[:3]]


# 清理知识缓存。
def _clear_knowledge_cache(client: CogDocClient, kb_id: str) -> None:
    _clear_api_cache(("knowledge", client.base_url, kb_id))
    _clear_api_cache(("knowledge-index-status", client.base_url, kb_id))
    _clear_api_cache(("review-queue", client.base_url, kb_id))
    _clear_api_cache(("review-queue-export", client.base_url, kb_id))
    _clear_api_cache(("pending-count", client.base_url, kb_id))
    _clear_api_cache(("feedback-loop-metrics", client.base_url, kb_id))


# 读取派生知识列表。
def _knowledge_rows(
    client: CogDocClient,
    kb_id: str,
    status: str | None,
    document_id: str | None = None,
    origin: str | None = None,
    created_by: str | None = None,
    has_conflict: bool | None = None,
    created_after: str | None = None,
    created_before: str | None = None,
) -> list[Mapping]:
    status_code, payload = _cached_api_value(
        (
            "knowledge",
            client.base_url,
            kb_id,
            status,
            document_id,
            origin,
            created_by,
            has_conflict,
            created_after,
            created_before,
        ),
        lambda: _response_status_payload(
            client.list_knowledge(
                kb_id,
                status=status,
                document_id=document_id,
                origin=origin,
                created_by=created_by,
                has_conflict=has_conflict,
                created_after=created_after,
                created_before=created_before,
            )
        ),
    )
    if status_code != 200:
        raise CogDocAPIError(format_api_error(payload, status_code, "读取派生知识失败"))
    rows = payload.get("knowledge", []) if isinstance(payload, Mapping) else []
    return [row for row in rows if isinstance(row, Mapping)]


# 读取审核队列摘要。
def _review_queue_summary(
    client: CogDocClient,
    kb_id: str,
    filters: Mapping | None = None,
) -> Mapping:
    filters = filters or {}
    status_code, payload = _cached_api_value(
        (
            "review-queue",
            client.base_url,
            kb_id,
            filters.get("document_id"),
            filters.get("origin"),
            filters.get("created_by"),
            filters.get("has_conflict"),
            filters.get("created_after"),
            filters.get("created_before"),
        ),
        lambda: _response_status_payload(
            client.review_queue_summary(
                kb_id,
                document_id=filters.get("document_id"),
                origin=filters.get("origin"),
                created_by=filters.get("created_by"),
                has_conflict=True if filters.get("has_conflict") else None,
                created_after=filters.get("created_after"),
                created_before=filters.get("created_before"),
            )
        ),
    )
    if status_code != 200 or not isinstance(payload, Mapping):
        return {}
    return payload


# 读取待处理审核数量。
def _pending_review_count(client: CogDocClient, kb_id: str | None) -> int:
    if not kb_id:
        return 0
    try:
        status_code, payload = _cached_api_value(
            ("pending-count", client.base_url, kb_id),
            lambda: _response_status_payload(client.pending_knowledge_count(kb_id)),
        )
    except Exception:
        return 0
    if status_code != 200 or not isinstance(payload, Mapping):
        return 0
    return int(payload.get("total") or 0)


# 读取审核队列导出载荷。
def _review_queue_export_payload(
    client: CogDocClient,
    kb_id: str,
    filters: Mapping | None = None,
) -> Mapping:
    filters = filters or {}
    status_code, payload = _cached_api_value(
        (
            "review-queue-export",
            client.base_url,
            kb_id,
            filters.get("document_id"),
            filters.get("origin"),
            filters.get("created_by"),
            filters.get("has_conflict"),
            filters.get("created_after"),
            filters.get("created_before"),
        ),
        lambda: _response_status_payload(
            client.review_queue_export(
                kb_id,
                limit=500,
                knowledge_document_id=filters.get("document_id"),
                knowledge_origin=filters.get("origin"),
                knowledge_created_by=filters.get("created_by"),
                knowledge_has_conflict=True if filters.get("has_conflict") else None,
                knowledge_created_after=filters.get("created_after"),
                knowledge_created_before=filters.get("created_before"),
            )
        ),
    )
    if status_code != 200 or not isinstance(payload, Mapping):
        return {}
    return payload


# 读取计数。
def _summary_count(summary: Mapping, group: str, key: str) -> int:
    values = summary.get(group)
    if not isinstance(values, Mapping):
        return 0
    value = values.get(key)
    return int(value) if isinstance(value, int) else 0


# 格式化比率。
def _rate_label(value) -> str:
    if isinstance(value, (int, float)):
        return f"{value * 100:.0f}%"
    return "-"


# 统计当前会话回答数。
def _answer_count_for(kb_id: str) -> int:
    return sum(
        1
        for message in _messages_for(kb_id)
        if message.get("role") == "assistant" and message.get("final")
    )


# 读取反馈闭环指标。
def _feedback_loop_metrics(
    client: CogDocClient, kb_id: str, answer_count: int
) -> Mapping:
    status_code, payload = _cached_api_value(
        ("feedback-loop-metrics", client.base_url, kb_id, answer_count),
        lambda: _response_status_payload(
            client.feedback_loop_metrics(kb_id, answer_count=answer_count)
        ),
    )
    if status_code != 200 or not isinstance(payload, Mapping):
        return {}
    return payload


# 读取派生知识索引状态。
def _knowledge_index_status(client: CogDocClient, kb_id: str) -> Mapping:
    status_code, payload = _cached_api_value(
        ("knowledge-index-status", client.base_url, kb_id),
        lambda: _response_status_payload(client.knowledge_index_status(kb_id)),
    )
    if status_code != 200 or not isinstance(payload, Mapping):
        return {}
    return payload


def _index_state_label(value) -> str:
    labels = {
        "fresh": "已同步",
        "stale": "待刷新",
        "missing": "未建立",
        "error": "异常",
    }
    return labels.get(str(value or ""), str(value or "-"))


# 格式化页签计数。
def _tab_label(label: str, count: int) -> str:
    return f"{label} ({count})" if count else label


KNOWLEDGE_ORIGIN_LABELS = {
    "": "全部来源",
    "manual_entry": "手工新增",
    "correction": "纠错",
    "no_evidence": "无答案补充",
    "saved_answer": "保存答案",
    "agent_suggested": "分析建议",
}


# 构建审核文档过滤选项。
def _review_document_options(docs: list[Mapping]) -> dict[str, str]:
    options = {"": "全部文档"}
    for doc in docs:
        name = str(doc.get("name") or "")
        if name:
            options.setdefault(name, name)
        document_id = str(doc.get("document_id") or doc.get("id") or "")
        if document_id:
            label = f"{name} · {document_id}" if name else document_id
            options.setdefault(document_id, label)
    return options


# 清空审核范围控件。
def _reset_review_filters(kb_id: str) -> None:
    st.session_state[f"review-filter-document-{kb_id}"] = ""
    st.session_state[f"review-filter-origin-{kb_id}"] = ""
    st.session_state[f"review-filter-created-by-{kb_id}"] = ""
    st.session_state[f"review-filter-created-after-{kb_id}"] = ""
    st.session_state[f"review-filter-created-before-{kb_id}"] = ""
    st.session_state[f"review-filter-conflict-{kb_id}"] = False


# 渲染审核范围过滤。
def _render_review_filters(client: CogDocClient, kb_id: str) -> dict[str, str | bool]:
    docs = _document_rows(client, kb_id)
    doc_options = _review_document_options(docs)
    with st.expander("审核范围", expanded=True):
        first = st.columns([1, 1, 1])
        document_id = first[0].selectbox(
            "文档",
            list(doc_options),
            format_func=lambda value: doc_options[value],
            key=f"review-filter-document-{kb_id}",
        )
        origin = first[1].selectbox(
            "来源",
            list(KNOWLEDGE_ORIGIN_LABELS),
            format_func=lambda value: KNOWLEDGE_ORIGIN_LABELS[value],
            key=f"review-filter-origin-{kb_id}",
        )
        created_by = first[2].text_input(
            "创建者",
            key=f"review-filter-created-by-{kb_id}",
            placeholder="全部",
        )
        second = st.columns([1, 1, 1])
        created_after = second[0].text_input(
            "起始时间",
            key=f"review-filter-created-after-{kb_id}",
            placeholder="YYYY-MM-DD",
        )
        created_before = second[1].text_input(
            "结束时间",
            key=f"review-filter-created-before-{kb_id}",
            placeholder="YYYY-MM-DD",
        )
        conflict_only = second[2].checkbox(
            "只看冲突",
            key=f"review-filter-conflict-{kb_id}",
        )
        filter_actions = st.columns([1, 1, 4])
        if filter_actions[0].button(
            "刷新范围",
            key=f"review-filter-refresh-{kb_id}",
            use_container_width=True,
        ):
            _clear_knowledge_cache(client, kb_id)
            st.rerun()
        filter_actions[1].button(
            "清空范围",
            key=f"review-filter-reset-{kb_id}",
            on_click=_reset_review_filters,
            args=(kb_id,),
            use_container_width=True,
        )
    return {
        "document_id": document_id or "",
        "origin": origin or "",
        "created_by": created_by.strip(),
        "created_after": created_after.strip(),
        "created_before": created_before.strip(),
        "has_conflict": conflict_only,
    }


# 清理检索调权反馈缓存。
def _clear_retrieval_feedback_cache(client: CogDocClient, kb_id: str) -> None:
    _clear_api_cache(("retrieval-feedback", client.base_url, kb_id))
    _clear_api_cache(("review-queue", client.base_url, kb_id))
    _clear_api_cache(("review-queue-export", client.base_url, kb_id))
    _clear_api_cache(("feedback-loop-metrics", client.base_url, kb_id))


# 读取检索调权反馈列表。
def _retrieval_feedback_rows(
    client: CogDocClient,
    kb_id: str,
    enabled: bool | None,
) -> list[Mapping]:
    status_code, payload = _cached_api_value(
        ("retrieval-feedback", client.base_url, kb_id, enabled),
        lambda: _response_status_payload(
            client.list_retrieval_feedback(kb_id, enabled=enabled, limit=200)
        ),
    )
    if status_code != 200:
        raise CogDocAPIError(format_api_error(payload, status_code, "读取检索调权失败"))
    rows = payload.get("retrieval_feedback", []) if isinstance(payload, Mapping) else []
    return [row for row in rows if isinstance(row, Mapping)]


# 读取反馈记录列表。
def _feedback_rows(
    client: CogDocClient,
    kb_id: str,
    feedback: str | None,
    is_bad_case: bool | None,
) -> list[Mapping]:
    status_code, payload = _cached_api_value(
        ("feedback", client.base_url, kb_id, feedback, is_bad_case),
        lambda: _response_status_payload(
            client.list_feedback(
                kb_id, feedback=feedback, is_bad_case=is_bad_case, limit=200
            )
        ),
    )
    if status_code != 200:
        raise CogDocAPIError(format_api_error(payload, status_code, "读取反馈失败"))
    rows = payload.get("feedback", []) if isinstance(payload, Mapping) else []
    return [row for row in rows if isinstance(row, Mapping)]


# 处理审核响应。
def _handle_knowledge_response(resp, client: CogDocClient, kb_id: str) -> None:
    if resp.status_code >= 400:
        st.error(_response_error(resp, "派生知识操作失败"))
        return
    _clear_knowledge_cache(client, kb_id)
    st.rerun()


# 渲染主动新增派生知识。
def _render_create_knowledge(client: CogDocClient, kb_id: str) -> None:
    notice_key = f"create-knowledge-notice-{kb_id}"
    notice = st.session_state.pop(notice_key, None)
    if isinstance(notice, Mapping):
        st.success(str(notice.get("message") or "派生知识已保存。"))
        if notice.get("warning"):
            st.warning(str(notice["warning"]))
        for item in notice.get("conflicts") or []:
            if isinstance(item, Mapping):
                st.caption(
                    f"{item.get('knowledge_id')} · "
                    f"{item.get('status')} · {item.get('text')}"
                )
    docs = _document_rows(client, kb_id)
    doc_options = [""] + [str(doc["name"]) for doc in docs]
    doc_by_name = {str(doc["name"]): doc for doc in docs}
    with st.form(f"create-knowledge-{kb_id}", clear_on_submit=True):
        text = st.text_area("内容", height=140)
        selected_source = st.selectbox("关联文档", doc_options)
        source_note = st.text_area("来源备注", height=80)
        certainty = st.selectbox(
            "可信度",
            ["medium", "high", "low"],
            format_func=lambda value: {"high": "高", "medium": "中", "low": "低"}[
                value
            ],
        )
        submitted = st.form_submit_button("新增派生知识", use_container_width=True)
    if not submitted:
        return
    if not text.strip():
        st.warning("请输入派生知识内容。")
        return
    doc = doc_by_name.get(selected_source) if selected_source else None
    resp = client.create_knowledge(
        kb_id=kb_id,
        text=text.strip(),
        related_source=selected_source or None,
        related_source_sha256=str(doc.get("sha256")) if doc else None,
        related_chunk_ids=[],
        related_page_start=None,
        related_page_end=None,
        related_chunk_text_hash=None,
        related_anchor_text=None,
        source_note=source_note.strip() or None,
        certainty=certainty,
    )
    if resp.status_code == 201:
        payload = response_payload(resp)
        _clear_knowledge_cache(client, kb_id)
        conflicts = payload.get("conflicts", []) if isinstance(payload, Mapping) else []
        st.success("派生知识已保存。")
        if conflicts:
            warning = "发现相似知识，已转为待审核并加入冲突组。"
            st.session_state[notice_key] = {
                "message": "派生知识已保存。",
                "warning": warning,
                "conflicts": conflicts[:3],
            }
            st.rerun()
        st.rerun()
    else:
        st.error(_response_error(resp, "新增派生知识失败"))


# 渲染知识修订表单。
def _render_knowledge_revision_form(
    client: CogDocClient, kb_id: str, item: Mapping, suffix: str
) -> None:
    knowledge_id = str(item.get("knowledge_id") or "")
    if not knowledge_id or item.get("status") not in {"approved", "stale"}:
        return
    active_key = f"revise-active-{suffix}-{knowledge_id}"
    if not st.session_state.get(active_key):
        if st.button("修订版本", key=f"show-revise-{suffix}-{knowledge_id}"):
            st.session_state[active_key] = True
            st.rerun()
        return
    if st.button("收起修订", key=f"hide-revise-{suffix}-{knowledge_id}"):
        st.session_state[active_key] = False
        st.rerun()
    with st.form(f"revise-knowledge-{suffix}-{knowledge_id}"):
        text = st.text_area(
            "修订内容",
            value=str(item.get("text") or ""),
            height=120,
            key=f"revise-text-{suffix}-{knowledge_id}",
        )
        fields = st.columns([1, 1, 1])
        related_document_id = fields[0].text_input(
            "文档标识",
            value=str(item.get("related_document_id") or ""),
            key=f"revise-doc-{suffix}-{knowledge_id}",
        )
        related_source = fields[1].text_input(
            "关联文档",
            value=str(item.get("related_source") or ""),
            key=f"revise-source-{suffix}-{knowledge_id}",
        )
        related_source_sha256 = fields[2].text_input(
            "文档哈希",
            value=str(item.get("related_source_sha256") or ""),
            key=f"revise-sha-{suffix}-{knowledge_id}",
        )
        chunk_ids_text = st.text_input(
            "关联分块",
            value=", ".join(str(x) for x in item.get("related_chunk_ids") or []),
            key=f"revise-chunks-{suffix}-{knowledge_id}",
        )
        page_fields = st.columns([1, 1, 2])
        related_page_start_text = page_fields[0].text_input(
            "起始页",
            value=str(item.get("related_page_start") or ""),
            key=f"revise-page-start-{suffix}-{knowledge_id}",
        )
        related_page_end_text = page_fields[1].text_input(
            "结束页",
            value=str(item.get("related_page_end") or ""),
            key=f"revise-page-end-{suffix}-{knowledge_id}",
        )
        related_chunk_text_hash = page_fields[2].text_input(
            "分块文本哈希",
            value=str(item.get("related_chunk_text_hash") or ""),
            key=f"revise-chunk-hash-{suffix}-{knowledge_id}",
        )
        related_anchor_text = st.text_input(
            "锚点文本",
            value=str(item.get("related_anchor_text") or ""),
            key=f"revise-anchor-{suffix}-{knowledge_id}",
        )
        source_note = st.text_area(
            "修订说明",
            value=str(item.get("source_note") or ""),
            height=68,
            key=f"revise-note-{suffix}-{knowledge_id}",
        )
        certainty_options = ["medium", "high", "low"]
        certainty_value = str(item.get("certainty") or "medium")
        if certainty_value not in certainty_options:
            certainty_value = "medium"
        certainty = st.selectbox(
            "可信度",
            certainty_options,
            index=certainty_options.index(certainty_value),
            format_func=lambda value: {"high": "高", "medium": "中", "low": "低"}[
                value
            ],
            key=f"revise-certainty-{suffix}-{knowledge_id}",
        )
        submitted = st.form_submit_button("创建修订版本", use_container_width=True)
    if not submitted:
        return
    if not text.strip():
        st.warning("请输入修订内容。")
        return
    chunk_ids = [part.strip() for part in chunk_ids_text.split(",") if part.strip()]
    related_page_start = _parse_optional_int(related_page_start_text.strip())
    related_page_end = _parse_optional_int(related_page_end_text.strip())
    _handle_knowledge_response(
        client.revise_knowledge(
            knowledge_id,
            text=text.strip(),
            related_document_id=related_document_id.strip() or None,
            related_source=related_source.strip() or None,
            related_source_sha256=related_source_sha256.strip() or None,
            related_chunk_ids=chunk_ids,
            related_page_start=related_page_start,
            related_page_end=related_page_end,
            related_chunk_text_hash=related_chunk_text_hash.strip() or None,
            related_anchor_text=related_anchor_text.strip() or None,
            source_note=source_note.strip() or None,
            certainty=certainty,
            created_by="frontend",
        ),
        client,
        kb_id,
    )


# 渲染过期知识候选重绑。
def _render_stale_rebind_candidates(
    client: CogDocClient, kb_id: str, item: Mapping
) -> None:
    knowledge_id = str(item.get("knowledge_id") or "")
    source = str(item.get("related_source") or "")
    if not knowledge_id or not source:
        return
    anchor = str(item.get("related_anchor_text") or "").strip() or None
    chunks = _source_chunk_rows(client, kb_id, source, anchor_text=anchor)
    candidates = _stale_rebind_candidates(item, chunks)
    if not candidates:
        st.caption("未找到可直接确认的新版分块候选。")
        return
    st.caption("新版分块候选")
    for idx, chunk in enumerate(candidates, start=1):
        chunk_id = str(chunk.get("chunk_id") or "")
        page_start = chunk.get("page_start", chunk.get("page"))
        page_end = chunk.get("page_end", page_start)
        page = _page_range_label(page_start, page_end)
        title = f"候选 {idx}"
        if page:
            title += f" · {page}"
        if chunk_id:
            title += f" · `{chunk_id}`"
        with st.expander(title):
            st.write(str(chunk.get("text_preview") or ""))
            if st.button(
                "采用候选并通过",
                key=f"stale-candidate-approve-{knowledge_id}-{chunk_id or idx}",
                use_container_width=True,
            ):
                _handle_knowledge_response(
                    client.review_knowledge(
                        knowledge_id,
                        "approve",
                        actor="frontend",
                        note="采用候选分块复核通过",
                        related_source=source,
                        related_source_sha256=str(chunk.get("source_sha256") or "")
                        or None,
                        related_chunk_ids=[chunk_id] if chunk_id else [],
                        related_page_start=_parse_optional_int(page_start),
                        related_page_end=_parse_optional_int(page_end),
                        related_chunk_text_hash=str(chunk.get("text_hash") or "")
                        or None,
                        related_anchor_text=str(item.get("related_anchor_text") or "")
                        or None,
                    ),
                    client,
                    kb_id,
                )


# 渲染单条派生知识。
def _render_knowledge_item(
    client: CogDocClient, kb_id: str, item: Mapping, suffix: str
) -> None:
    knowledge_id = str(item.get("knowledge_id") or "")
    title = f"{knowledge_id} · {item.get('status') or '-'}"
    source = item.get("related_source")
    conflict_group = item.get("conflict_group_id")
    if source:
        title += f" · {source}"
    if conflict_group:
        title += f" · 冲突 {conflict_group}"
    with st.expander(title):
        st.write(item.get("text") or "")
        meta = st.columns(4)
        meta[0].caption(f"来源: {item.get('origin') or '-'}")
        meta[1].caption(f"可信度: {item.get('certainty') or '-'}")
        meta[2].caption(f"创建者: {item.get('created_by') or '-'}")
        meta[3].caption(f"创建时间: {item.get('created_at') or '-'}")
        if conflict_group:
            st.caption(f"冲突组: {conflict_group}")
        st.caption(_binding_summary(item))
        if item.get("reviewed_by") or item.get("review_note"):
            st.caption(
                "审核: "
                f"{item.get('reviewed_by') or '-'} · "
                f"{item.get('reviewed_at') or '-'} · "
                f"{item.get('review_note') or '-'}"
            )
        if item.get("source_note"):
            st.caption(str(item["source_note"]))
        _render_knowledge_revision_form(client, kb_id, item, suffix)
        if item.get("status") == "stale":
            _render_stale_rebind_candidates(client, kb_id, item)
            with st.form(f"stale-rebind-{knowledge_id}"):
                stale_cols = st.columns([1, 1, 1, 1])
                related_document_id = stale_cols[0].text_input(
                    "文档标识",
                    value=str(item.get("related_document_id") or ""),
                    key=f"stale-document-{knowledge_id}",
                )
                related_source = stale_cols[1].text_input(
                    "新版文档",
                    value=str(item.get("related_source") or ""),
                    key=f"stale-source-{knowledge_id}",
                )
                source_sha = stale_cols[2].text_input(
                    "新版哈希",
                    value=str(item.get("related_source_sha256") or ""),
                    key=f"stale-sha-{knowledge_id}",
                )
                chunk_ids_text = stale_cols[3].text_input(
                    "新版分块",
                    value=", ".join(
                        str(x) for x in item.get("related_chunk_ids") or []
                    ),
                    key=f"stale-chunks-{knowledge_id}",
                )
                page_cols = st.columns([1, 1, 1, 1])
                related_page_start_text = page_cols[0].text_input(
                    "新版起始页",
                    value=str(item.get("related_page_start") or ""),
                    key=f"stale-page-start-{knowledge_id}",
                )
                related_page_end_text = page_cols[1].text_input(
                    "新版结束页",
                    value=str(item.get("related_page_end") or ""),
                    key=f"stale-page-end-{knowledge_id}",
                )
                chunk_text_hash = page_cols[2].text_input(
                    "新版文本哈希",
                    value=str(item.get("related_chunk_text_hash") or ""),
                    key=f"stale-chunk-hash-{knowledge_id}",
                )
                anchor_text = page_cols[3].text_input(
                    "新版锚点",
                    value=str(item.get("related_anchor_text") or ""),
                    key=f"stale-anchor-{knowledge_id}",
                )
                note = st.text_input("复核说明", key=f"stale-note-{knowledge_id}")
                if st.form_submit_button("确认仍有效并通过"):
                    chunk_ids = [
                        part.strip()
                        for part in chunk_ids_text.split(",")
                        if part.strip()
                    ]
                    related_page_start = _parse_optional_int(
                        related_page_start_text.strip()
                    )
                    related_page_end = _parse_optional_int(
                        related_page_end_text.strip()
                    )
                    _handle_knowledge_response(
                        client.review_knowledge(
                            knowledge_id,
                            "approve",
                            actor="frontend",
                            note=note.strip() or None,
                            related_document_id=related_document_id.strip() or None,
                            related_source=related_source.strip() or None,
                            related_source_sha256=source_sha.strip() or None,
                            related_chunk_ids=chunk_ids,
                            related_page_start=related_page_start,
                            related_page_end=related_page_end,
                            related_chunk_text_hash=chunk_text_hash.strip() or None,
                            related_anchor_text=anchor_text.strip() or None,
                        ),
                        client,
                        kb_id,
                    )
        status = str(item.get("status") or "")
        if status == "pending":
            actions = st.columns([1, 1, 4])
            if actions[0].button("通过", key=f"approve-{suffix}-{knowledge_id}"):
                _handle_knowledge_response(
                    client.review_knowledge(knowledge_id, "approve"),
                    client,
                    kb_id,
                )
            if actions[1].button("驳回", key=f"reject-{suffix}-{knowledge_id}"):
                _handle_knowledge_response(
                    client.review_knowledge(knowledge_id, "reject"),
                    client,
                    kb_id,
                )
        elif status == "stale":
            actions = st.columns([1, 5])
            if actions[0].button("驳回", key=f"reject-{suffix}-{knowledge_id}"):
                _handle_knowledge_response(
                    client.review_knowledge(knowledge_id, "reject"),
                    client,
                    kb_id,
                )
        elif status in {"approved", "rejected", "archived"}:
            actions = st.columns([1, 1, 4])
            if status == "approved" and actions[0].button(
                "归档", key=f"archive-{suffix}-{knowledge_id}"
            ):
                _handle_knowledge_response(
                    client.review_knowledge(knowledge_id, "archive"),
                    client,
                    kb_id,
                )
            delete_key = f"delete-active-{suffix}-{knowledge_id}"
            if actions[1].button("删除", key=f"delete-{suffix}-{knowledge_id}"):
                st.session_state[delete_key] = True
                st.rerun()
            if st.session_state.get(delete_key):
                st.warning("确认删除这条派生知识？删除后不可恢复。")
                confirm_cols = st.columns([1, 1, 4])
                if confirm_cols[0].button(
                    "确认删除此派生知识",
                    key=f"confirm-delete-{suffix}-{knowledge_id}",
                ):
                    st.session_state.pop(delete_key, None)
                    _handle_knowledge_response(
                        client.delete_knowledge(knowledge_id),
                        client,
                        kb_id,
                    )
                if confirm_cols[1].button(
                    "取消",
                    key=f"cancel-delete-{suffix}-{knowledge_id}",
                ):
                    st.session_state.pop(delete_key, None)
                    st.rerun()


# 渲染知识审核列表。
def _render_knowledge_review_list(
    client: CogDocClient,
    kb_id: str,
    status: str,
    label: str,
    filters: Mapping | None = None,
) -> None:
    filters = filters or {}
    try:
        rows = _knowledge_rows(
            client,
            kb_id,
            status,
            document_id=filters.get("document_id") or None,
            origin=filters.get("origin") or None,
            created_by=filters.get("created_by") or None,
            has_conflict=True if filters.get("has_conflict") else None,
            created_after=filters.get("created_after") or None,
            created_before=filters.get("created_before") or None,
        )
    except Exception as exc:
        st.warning(str(exc))
        return
    if not rows:
        st.info(f"暂无{label}派生知识。")
        return
    if status == "pending":
        options = [
            str(row.get("knowledge_id")) for row in rows if row.get("knowledge_id")
        ]
        selected = st.multiselect(
            f"批量选择{label}派生知识", options, key=f"batch-{status}"
        )
        batch = st.columns([1, 1, 3])
        if batch[0].button(
            "批量通过", key=f"batch-approve-{status}", disabled=not selected
        ):
            _handle_knowledge_response(
                client.batch_review_knowledge(selected, "batch-approve"),
                client,
                kb_id,
            )
        if batch[1].button(
            "批量驳回", key=f"batch-reject-{status}", disabled=not selected
        ):
            _handle_knowledge_response(
                client.batch_review_knowledge(selected, "batch-reject"),
                client,
                kb_id,
            )
    for item in rows:
        _render_knowledge_item(client, kb_id, item, status)


# 渲染派生知识列表。
def _render_knowledge_catalog(
    client: CogDocClient, kb_id: str, filters: Mapping | None = None
) -> None:
    filters = filters or {}
    status_options = {
        "全部": None,
        "待审核": "pending",
        "已通过": "approved",
        "过期": "stale",
        "已驳回": "rejected",
        "已归档": "archived",
    }
    selected = st.radio(
        "状态",
        list(status_options),
        horizontal=True,
        key=f"knowledge-catalog-status-{kb_id}",
    )
    try:
        rows = _knowledge_rows(
            client,
            kb_id,
            status_options[selected],
            document_id=filters.get("document_id") or None,
            origin=filters.get("origin") or None,
            created_by=filters.get("created_by") or None,
            has_conflict=True if filters.get("has_conflict") else None,
            created_after=filters.get("created_after") or None,
            created_before=filters.get("created_before") or None,
        )
    except Exception as exc:
        st.warning(str(exc))
        return
    if not rows:
        st.info("暂无派生知识。")
        return
    st.caption(f"共 {len(rows)} 条")
    for item in rows:
        _render_knowledge_item(client, kb_id, item, f"catalog-{selected}")


# 处理检索调权反馈响应。
def _handle_retrieval_feedback_response(resp, client: CogDocClient, kb_id: str) -> None:
    if resp.status_code >= 400:
        st.error(_response_error(resp, "检索调权操作失败"))
        return
    _clear_retrieval_feedback_cache(client, kb_id)
    st.rerun()


# 渲染单条检索调权反馈。
def _render_retrieval_feedback_item(
    client: CogDocClient, kb_id: str, item: Mapping
) -> None:
    feedback_id = str(item.get("retrieval_feedback_id") or "")
    target_chunks = item.get("target_chunks")
    if not isinstance(target_chunks, list):
        target_chunks = []
    chunk_ids = [
        str(target.get("chunk_id"))
        for target in target_chunks
        if isinstance(target, Mapping) and target.get("chunk_id")
    ]
    chunk_count = int(item.get("chunk_count") or len(chunk_ids) or 1)
    status = "启用" if item.get("enabled") is True else "禁用"
    delta = item.get("weight_delta")
    title = f"{chunk_count} 个分块 · {status} · {delta}"
    with st.expander(title):
        st.write(item.get("query_text") or "")
        meta = st.columns(4)
        meta[0].caption(f"来源: {item.get('source_type') or '-'}")
        meta[1].caption(f"置信度: {item.get('confidence') or '-'}")
        meta[2].caption(f"反馈: {item.get('feedback_id') or '-'}")
        meta[3].caption(f"创建时间: {item.get('created_at') or '-'}")
        if chunk_ids:
            st.caption(f"分块: {', '.join(chunk_ids)}")
        elif item.get("chunk_id"):
            st.caption(f"分块: {item.get('chunk_id')}")
        if item.get("trace_id"):
            st.caption(f"trace: {item.get('trace_id')}")
        if item.get("disable_reason"):
            st.caption(f"停用原因: {item.get('disable_reason')}")
        action_cols = st.columns([1, 5])
        if item.get("enabled") is True:
            if action_cols[0].button("禁用", key=f"disable-rf-{feedback_id}"):
                _handle_retrieval_feedback_response(
                    client.set_retrieval_feedback_enabled(
                        feedback_id,
                        False,
                        actor="frontend",
                    ),
                    client,
                    kb_id,
                )
        elif action_cols[0].button("启用", key=f"enable-rf-{feedback_id}"):
            _handle_retrieval_feedback_response(
                client.set_retrieval_feedback_enabled(feedback_id, True),
                client,
                kb_id,
            )


# 渲染检索调权反馈列表。
def _render_retrieval_feedback_area(client: CogDocClient, kb_id: str) -> None:
    status_options = {
        "启用": True,
        "禁用": False,
        "全部": None,
    }
    selected = st.radio(
        "状态",
        list(status_options),
        horizontal=True,
        key=f"retrieval-feedback-status-{kb_id}",
    )
    try:
        rows = _retrieval_feedback_rows(client, kb_id, status_options[selected])
    except Exception as exc:
        st.warning(str(exc))
        return
    if not rows:
        st.info("暂无检索调权反馈。")
        return
    for item in rows:
        _render_retrieval_feedback_item(client, kb_id, item)


# 渲染单条反馈记录。
def _render_feedback_item(item: Mapping) -> None:
    feedback_id = str(item.get("feedback_id") or "")
    feedback = str(item.get("feedback") or "-")
    issue_type = str(item.get("feedback_type") or "-")
    trace_id = str(item.get("trace_id") or "-")
    title = f"{feedback} · {issue_type} · {trace_id}"
    with st.expander(title):
        if item.get("query"):
            st.caption(f"query: {item.get('query')}")
        if item.get("answer"):
            st.write(item.get("answer"))
        if item.get("correction"):
            st.success(str(item.get("correction")))
        if item.get("comment"):
            st.caption(str(item.get("comment")))
        meta = st.columns(4)
        meta[0].caption(f"反馈: {feedback_id or '-'}")
        meta[1].caption(f"会话: {item.get('session_id') or '-'}")
        meta[2].caption(f"评分: {item.get('rating') or '-'}")
        meta[3].caption(f"创建时间: {item.get('created_at') or '-'}")
        citations = (
            item.get("citations") if isinstance(item.get("citations"), list) else []
        )
        evidence = (
            item.get("evidence") if isinstance(item.get("evidence"), list) else []
        )
        if citations:
            st.caption(
                "引用: "
                + ", ".join(
                    str(ref.get("chunk_id") or ref.get("source") or "-")
                    for ref in citations
                    if isinstance(ref, Mapping)
                )
            )
        if evidence:
            previews = [
                str(ref.get("text_preview") or "")
                for ref in evidence
                if isinstance(ref, Mapping) and ref.get("text_preview")
            ]
            if previews:
                st.caption(f"证据: {previews[0]}")


# 渲染反馈记录列表。
def _render_feedback_area(client: CogDocClient, kb_id: str) -> None:
    feedback_options = {
        "全部": None,
        "点赞": "thumbs_up",
        "点踩": "thumbs_down",
        "纠错": "correction",
    }
    selected_feedback = st.radio(
        "反馈",
        list(feedback_options),
        horizontal=True,
        key=f"feedback-kind-{kb_id}",
    )
    try:
        rows = _feedback_rows(
            client,
            kb_id,
            feedback_options[selected_feedback],
            None,
        )
    except Exception as exc:
        st.warning(str(exc))
        return
    if not rows:
        st.info("暂无反馈记录。")
        return
    for item in rows:
        _render_feedback_item(item)


# 渲染派生知识页。
def _knowledge_area(kb_id: str | None) -> None:
    st.subheader("派生知识")
    if not kb_id:
        st.info("先选择知识库。")
        return
    client = _client()
    notice_key = f"knowledge-area-notice-{kb_id}"
    notice = st.session_state.pop(notice_key, None)
    if isinstance(notice, Mapping):
        kind = str(notice.get("kind") or "info")
        message = str(notice.get("message") or "")
        if kind == "success":
            st.success(message)
        elif kind == "warning":
            st.warning(message)
        else:
            st.info(message)
    review_filters = _render_review_filters(client, kb_id)
    api_filters = {key: value or None for key, value in review_filters.items()}
    try:
        summary = _review_queue_summary(client, kb_id, api_filters)
    except Exception:
        summary = {}
    answer_count = _answer_count_for(kb_id)
    try:
        loop_metrics = _feedback_loop_metrics(client, kb_id, answer_count)
    except Exception:
        loop_metrics = {}
    try:
        index_status = _knowledge_index_status(client, kb_id)
    except Exception:
        index_status = {}
    pending_count = _summary_count(summary, "knowledge", "pending")
    stale_count = _summary_count(summary, "knowledge", "stale")
    auto_rebound_count = _summary_count(
        summary, "knowledge_auto_review", "auto_rebound"
    )
    conflict_count = _summary_count(summary, "knowledge_conflicts", "total")
    conflict_group_count = _summary_count(summary, "knowledge_conflicts", "groups")
    feedback_count = _summary_count(summary, "feedback_counts", "total")
    retrieval_count = _summary_count(summary, "retrieval_feedback", "enabled")
    retrieval_disabled_count = _summary_count(summary, "retrieval_feedback", "disabled")
    metrics = st.columns(4)
    metrics[0].metric("待审核派生知识", pending_count)
    metrics[0].caption(f"冲突 {conflict_count} · 组 {conflict_group_count}")
    metrics[1].metric("过期派生知识", stale_count)
    metrics[1].caption(f"自动重绑 {auto_rebound_count}")
    metrics[2].metric("反馈", feedback_count)
    metrics[3].metric("检索调权", retrieval_count + retrieval_disabled_count)
    metrics[3].caption(f"启用 {retrieval_count} · 禁用 {retrieval_disabled_count}")
    if isinstance(index_status, Mapping) and index_status:
        state_label = _index_state_label(index_status.get("state"))
        approved_index_count = index_status.get("approved_count", 0)
        indexed_count = index_status.get("indexed_count", 0)
        auto_refresh = "开" if index_status.get("auto_refresh_enabled") else "关"
        st.caption(
            f"派生知识索引 {state_label} · 已审 {approved_index_count} · "
            f"已索引 {indexed_count} · 后台刷新 {auto_refresh}"
        )
        if index_status.get("last_error") or index_status.get("collection_error"):
            st.caption(
                "索引异常: "
                f"{index_status.get('last_error') or index_status.get('collection_error')}"
            )
    rates = loop_metrics.get("rates") if isinstance(loop_metrics, Mapping) else {}
    if isinstance(rates, Mapping):
        st.caption(
            "审核通过率 "
            f"{_rate_label(rates.get('pending_approval_rate'))} · "
            "审核驳回率 "
            f"{_rate_label(rates.get('pending_rejection_rate'))} · "
            "无答案反馈 "
            f"{_rate_label(rates.get('no_evidence_rate'))} · "
            "反馈转派生知识 "
            f"{_rate_label(rates.get('feedback_to_pending_rate'))} · "
            "调权回滚 "
            f"{_rate_label(rates.get('retrieval_feedback_rollback_rate'))}"
        )
    try:
        export_payload = _review_queue_export_payload(client, kb_id, api_filters)
    except Exception:
        export_payload = {}
    export_cols = st.columns([1, 1, 1, 3])
    export_cols[0].download_button(
        "导出审核队列",
        data=json.dumps(export_payload, ensure_ascii=False, indent=2),
        file_name=f"cogdoc-review-queue-{kb_id}.json",
        mime="application/json",
        disabled=not bool(export_payload),
        use_container_width=True,
    )
    if export_cols[1].button("刷新队列", use_container_width=True):
        _clear_knowledge_cache(client, kb_id)
        _clear_api_cache(("feedback", client.base_url, kb_id))
        _clear_api_cache(("feedback-analysis", client.base_url, kb_id))
        _clear_api_cache(("retrieval-feedback", client.base_url, kb_id))
        st.rerun()
    if export_cols[2].button("检查过期派生知识", use_container_width=True):
        resp = client.scan_stale_knowledge(kb_id)
        if resp.status_code >= 400:
            st.error(_response_error(resp, "过期派生知识扫描失败"))
        else:
            payload = response_payload(resp)
            marked = (
                int(payload.get("stale_marked") or 0)
                if isinstance(payload, Mapping)
                else 0
            )
            st.session_state[notice_key] = {
                "kind": "success" if marked else "info",
                "message": f"过期派生知识扫描完成，新增标记 {marked} 条。",
            }
            _clear_knowledge_cache(client, kb_id)
            st.rerun()
    (
        create_tab,
        catalog_tab,
        pending_tab,
        stale_tab,
        feedback_tab,
        retrieval_tab,
    ) = st.tabs(
        [
            "新增",
            "派生知识列表",
            _tab_label("待审核", pending_count),
            _tab_label("过期", stale_count),
            _tab_label("反馈", feedback_count),
            _tab_label("调权", retrieval_count),
        ]
    )
    with create_tab:
        _render_create_knowledge(client, kb_id)
    with catalog_tab:
        _render_knowledge_catalog(client, kb_id, review_filters)
    with pending_tab:
        _render_knowledge_review_list(
            client, kb_id, "pending", "待审核", review_filters
        )
    with stale_tab:
        _render_knowledge_review_list(client, kb_id, "stale", "过期", review_filters)
    with feedback_tab:
        _render_feedback_area(client, kb_id)
    with retrieval_tab:
        _render_retrieval_feedback_area(client, kb_id)


# 检索调试后台线程。
def _retrieve_debug_worker(
    *,
    api_url: str,
    auth_token: str,
    workspace_id: str | None,
    kb_id: str,
    query: str,
    top_k: int,
    rerank: bool,
    rerank_top_n: int | None,
    outbox: queue.Queue,
) -> None:
    try:
        client = CogDocClient(
            api_url,
            api_key=auth_token,
            workspace_id=workspace_id,
        )
        resp = client.retrieve(
            kb_id,
            query,
            top_k=top_k,
            rerank=rerank,
            rerank_top_n=rerank_top_n,
        )
        outbox.put(
            (
                "result",
                {"status_code": resp.status_code, "payload": response_payload(resp)},
            )
        )
    except Exception as exc:
        outbox.put(("result", {"status_code": None, "payload": {"message": str(exc)}}))
    finally:
        outbox.put(("done", {}))


# 启动检索调试。
def _start_retrieve_debug(
    kb_id: str,
    query: str,
    top_k: int,
    rerank: bool,
    rerank_top_n: int | None,
) -> None:
    marker = _context_key(kb_id)
    pending = st.session_state.pending_retrieve_debugs.get(marker)
    if pending and not pending.get("done"):
        return
    outbox: queue.Queue = queue.Queue()
    pending = {
        "query": query,
        "top_k": top_k,
        "rerank": rerank,
        "rerank_top_n": rerank_top_n,
        "started_at": time.monotonic(),
        "queue": outbox,
        "done": False,
    }
    worker = threading.Thread(
        target=_retrieve_debug_worker,
        kwargs={
            "api_url": st.session_state.api_url,
            "auth_token": _current_api_credential(),
            "workspace_id": _current_workspace_id(),
            "kb_id": kb_id,
            "query": query,
            "top_k": top_k,
            "rerank": rerank,
            "rerank_top_n": rerank_top_n,
            "outbox": outbox,
        },
        daemon=True,
    )
    pending["thread"] = worker
    st.session_state.pending_retrieve_debugs[marker] = pending
    worker.start()


# 消费检索调试事件。
def _drain_retrieve_debug_events() -> None:
    for marker, pending in list(st.session_state.pending_retrieve_debugs.items()):
        outbox = pending["queue"]
        while True:
            try:
                event, data = outbox.get_nowait()
            except queue.Empty:
                break
            if event == "result":
                st.session_state.retrieve_debug_by_context[marker] = data
            elif event == "done":
                pending["done"] = True
        if pending.get("done"):
            st.session_state.pending_retrieve_debugs.pop(marker, None)


# 渲染检索调试。
def _render_retrieve_debug(client: CogDocClient, kb_id: str | None) -> None:
    st.subheader("检索调试")
    if not kb_id:
        st.info("先选择知识库。")
        return
    marker = _context_key(kb_id)
    pending = st.session_state.pending_retrieve_debugs.get(marker)
    with st.form(f"retrieve-debug-{kb_id}-{st.session_state.session_id}"):
        query = st.text_area("检索问题", height=90, placeholder="输入要召回的查询…")
        controls = st.columns([1, 1, 1])
        top_k = controls[0].slider("top_k", min_value=1, max_value=50, value=8)
        rerank = controls[1].checkbox("重排", value=True)
        rerank_top_n = controls[2].number_input(
            "rerank_top_n",
            min_value=1,
            max_value=50,
            value=min(8, top_k),
            disabled=not rerank,
        )
        if rerank:
            st.caption(
                "重排会加载 bge-reranker-v2-m3；无可用 GPU 时后端默认跳过 CPU 重排，"
                "如强制开启可能明显卡顿。"
            )
        submitted = st.form_submit_button(
            "运行检索", use_container_width=True, disabled=bool(pending)
        )
    if submitted:
        if not query.strip():
            st.warning("请输入检索问题。")
        else:
            _start_retrieve_debug(
                kb_id,
                query.strip(),
                top_k,
                rerank,
                int(rerank_top_n) if rerank else None,
            )
            st.rerun()
    pending = st.session_state.pending_retrieve_debugs.get(marker)
    if pending:
        elapsed = time.monotonic() - pending.get("started_at", time.monotonic())
        st.info(
            f"正在检索：{pending.get('query')} · "
            f"top_k={pending.get('top_k')} · "
            f"rerank={pending.get('rerank')} · {elapsed:.1f}s"
        )
        time.sleep(STREAM_RERUN_INTERVAL_SECONDS)
        st.rerun()
    result = st.session_state.retrieve_debug_by_context.get(marker)
    if not result:
        st.info("运行一次检索后，这里会显示命中 chunk、分数和 retrieval 元数据。")
        return
    status_code = result.get("status_code")
    payload = result.get("payload")
    if status_code != 200:
        st.error(format_api_error(payload, status_code, "检索失败"))
        return
    if not isinstance(payload, Mapping):
        st.error(f"检索响应格式不符合预期: {payload}")
        return
    hits = payload.get("hits", [])
    header = st.columns(4)
    header[0].caption(f"query: {payload.get('query') or '-'}")
    header[1].caption(f"top_k: {payload.get('top_k')}")
    header[2].caption(f"rerank: {payload.get('rerank')}")
    header[3].caption(f"hits: {len(hits) if isinstance(hits, list) else 0}")
    if not hits:
        st.warning("没有召回到内容。")
        return
    if any(
        isinstance(hit, Mapping)
        and (hit.get("retrieval") or {}).get("rerank_skipped_reason")
        for hit in hits
    ):
        st.warning("后端检测到 reranker 会走 CPU，已跳过重排以避免卡死。")
    for hit in hits:
        if isinstance(hit, Mapping):
            _render_retrieve_hit(hit)


# 渲染调试区。
def _debug_area(kb_id: str | None) -> None:
    trace_tab, retrieve_tab = st.tabs(["Trace 调试", "检索调试"])
    with trace_tab:
        _render_trace_lookup(kb_id)
    with retrieve_tab:
        _render_retrieve_debug(_client(), kb_id)


def _render_workspace_members(client: CogDocClient, workspace_id: str) -> None:
    try:
        member_status, member_payload = _cached_api_value(
            ("workspace-members", client.base_url, workspace_id),
            lambda: _response_status_payload(
                client.list_workspace_members(workspace_id)
            ),
        )
    except Exception as exc:
        st.error(f"读取成员失败: {exc}")
        return
    if member_status != 200 or not isinstance(member_payload, Mapping):
        st.error(format_api_error(member_payload, member_status, "读取成员失败"))
        return
    members = member_payload.get("members", [])
    if not isinstance(members, list):
        st.error("成员列表响应格式不符合预期。")
        return
    for member in members:
        if not isinstance(member, Mapping):
            continue
        member_id = str(member.get("member_id") or member.get("user_id") or "")
        role = str(member.get("role") or "viewer")
        label = str(member.get("display_name") or member.get("email") or member_id)
        st.caption(f"{label} · {member.get('email') or '-'} · {role}")
        if not member_id or role == "owner":
            continue
        controls = st.columns([3, 1, 1])
        roles = ["viewer", "reviewer", "editor", "admin"]
        selected_role = controls[0].selectbox(
            "成员角色",
            roles,
            index=roles.index(role) if role in roles else 0,
            key=f"member-role-{workspace_id}-{member_id}",
            label_visibility="collapsed",
        )
        if controls[1].button("保存", key=f"member-save-{workspace_id}-{member_id}"):
            response = client.update_workspace_member(
                workspace_id, member_id, selected_role
            )
            if response.status_code == 200:
                _invalidate_auth_profile()
                _clear_api_cache(("workspace-members", client.base_url, workspace_id))
                st.rerun()
            else:
                st.error(_response_error(response, "更新成员失败"))
        if controls[2].button("移除", key=f"member-remove-{workspace_id}-{member_id}"):
            response = client.remove_workspace_member(workspace_id, member_id)
            if response.status_code == 204:
                _invalidate_auth_profile()
                _clear_api_cache(("workspace-members", client.base_url, workspace_id))
                st.rerun()
            else:
                st.error(_response_error(response, "移除成员失败"))


def _render_workspace_invites(client: CogDocClient, workspace_id: str) -> None:
    with st.form(f"workspace-invite-{workspace_id}", clear_on_submit=True):
        email = st.text_input("邀请邮箱")
        role = st.selectbox("邀请角色", ["viewer", "reviewer", "editor", "admin"])
        submitted = st.form_submit_button("创建邀请", use_container_width=True)
    if submitted:
        response = client.create_workspace_invite(workspace_id, email.strip(), role)
        if response.status_code == 201:
            payload = response_payload(response)
            invite_token = (
                str(payload.get("invite_token") or "")
                if isinstance(payload, Mapping)
                else ""
            )
            st.session_state.last_invite_token = invite_token
            _clear_api_cache(("workspace-invites", client.base_url, workspace_id))
            st.success("邀请已创建。请通过可信渠道把一次性令牌交给受邀人。")
        else:
            st.error(_response_error(response, "创建邀请失败"))
    if st.session_state.last_invite_token:
        st.code(st.session_state.last_invite_token, language=None)
        if st.button("隐藏邀请令牌", key=f"hide-invite-token-{workspace_id}"):
            st.session_state.last_invite_token = ""
            st.rerun()
    try:
        invite_status, invite_payload = _cached_api_value(
            ("workspace-invites", client.base_url, workspace_id),
            lambda: _response_status_payload(
                client.list_workspace_invites(workspace_id)
            ),
        )
    except Exception as exc:
        st.error(f"读取邀请失败: {exc}")
        return
    if invite_status != 200 or not isinstance(invite_payload, Mapping):
        st.error(format_api_error(invite_payload, invite_status, "读取邀请失败"))
        return
    invites = invite_payload.get("invites", [])
    if not isinstance(invites, list):
        return
    for invite in invites:
        if not isinstance(invite, Mapping) or invite.get("status") != "pending":
            continue
        invite_id = str(invite.get("invite_id") or "")
        row = st.columns([5, 1])
        row[0].caption(
            f"{invite.get('email') or '-'} · {invite.get('role') or '-'} · 待接受"
        )
        if invite_id and row[1].button(
            "撤销", key=f"invite-revoke-{workspace_id}-{invite_id}"
        ):
            response = client.revoke_workspace_invite(workspace_id, invite_id)
            if response.status_code == 204:
                _clear_api_cache(("workspace-invites", client.base_url, workspace_id))
                st.rerun()
            else:
                st.error(_response_error(response, "撤销邀请失败"))


def _render_account_sidebar(client: CogDocClient) -> None:
    mode = st.session_state.auth_mode
    if mode == "api_key":
        st.caption("🔐 API Key 身份")
        return
    if mode != "account":
        st.caption("本地兼容模式")
        return
    user = st.session_state.auth_user
    workspace = st.session_state.auth_workspace
    display_name = str(user.get("display_name") or user.get("email") or "账号")
    st.subheader(display_name)
    st.caption(str(user.get("email") or ""))

    workspaces = [
        item
        for item in st.session_state.auth_workspaces
        if isinstance(item, Mapping) and item.get("workspace_id")
    ]
    current_id = str(workspace.get("workspace_id") or "")
    if current_id and all(
        item.get("workspace_id") != current_id for item in workspaces
    ):
        workspaces.append(workspace)
    workspace_ids = [str(item["workspace_id"]) for item in workspaces]
    names = {
        str(item["workspace_id"]): str(item.get("name") or item["workspace_id"])
        for item in workspaces
    }
    if workspace_ids:
        selected = st.selectbox(
            "当前工作区",
            workspace_ids,
            index=workspace_ids.index(current_id) if current_id in workspace_ids else 0,
            format_func=lambda value: names.get(value, value),
            key="active-workspace-picker",
        )
        if selected != current_id:
            response = client.switch_workspace(selected)
            if response.status_code == 200:
                if _apply_auth_session(response_payload(response)):
                    st.rerun()
                else:
                    st.error("身份服务返回了无效工作区会话。")
            else:
                st.error(_response_error(response, "切换工作区失败"))
    st.caption(f"角色：{workspace.get('role') or '-'}")

    with st.expander("新建工作区"):
        with st.form("create-workspace", clear_on_submit=True):
            name = st.text_input("工作区名称")
            submitted = st.form_submit_button("创建", use_container_width=True)
        if submitted:
            response = client.create_workspace(name.strip())
            payload = response_payload(response)
            created = payload.get("workspace") if isinstance(payload, Mapping) else None
            created_id = (
                str(created.get("workspace_id") or "")
                if isinstance(created, Mapping)
                else ""
            )
            if response.status_code == 201 and created_id:
                switched = client.switch_workspace(created_id)
                if switched.status_code == 200 and _apply_auth_session(
                    response_payload(switched)
                ):
                    st.rerun()
                st.error(_response_error(switched, "切换到新工作区失败"))
            else:
                st.error(_response_error(response, "创建工作区失败"))

    role = str(workspace.get("role") or "")
    if current_id and role in {"owner", "admin"}:
        with st.expander("成员与邀请"):
            st.markdown("**成员**")
            _render_workspace_members(client, current_id)
            st.markdown("**邀请**")
            _render_workspace_invites(client, current_id)

    with st.expander("接受工作区邀请"):
        with st.form("authenticated-invite", clear_on_submit=True):
            token = st.text_input("邀请令牌", type="password")
            submitted = st.form_submit_button("接受并切换", use_container_width=True)
        if submitted:
            response = client.accept_workspace_invite(token.strip())
            if response.status_code == 200 and _apply_auth_session(
                response_payload(response)
            ):
                st.rerun()
            st.error(_response_error(response, "接受邀请失败"))

    if st.button("退出登录", use_container_width=True):
        try:
            response = client.logout()
            if response.status_code not in (204, 401):
                st.warning(_response_error(response, "服务端退出失败"))
        except Exception:
            pass
        _clear_account_session()
        st.rerun()


def _render_resource_access_controls(
    client: CogDocClient, kb_id: str, documents: list[Mapping]
) -> None:
    """Render owner/admin controls for KB, document, and subject ACLs."""

    if st.session_state.auth_mode != "account" or "manage_access" not in set(
        st.session_state.auth_permissions
    ):
        return
    with st.expander("访问权限"):
        try:
            policy_response = client.get_kb_access_policy(kb_id)
            policy_payload = response_payload(policy_response)
        except Exception as exc:
            st.error(f"读取权限失败: {exc}")
            return
        if policy_response.status_code != 200 or not isinstance(
            policy_payload, Mapping
        ):
            st.error(_response_error(policy_response, "读取权限失败"))
            return

        current_policy = str(policy_payload.get("policy") or "workspace")
        selected_policy = st.selectbox(
            "知识库可见性",
            ["workspace", "private"],
            index=1 if current_policy == "private" else 0,
            format_func=lambda value: (
                "仅授权成员" if value == "private" else "工作区成员"
            ),
            key=f"kb-policy-{kb_id}",
        )
        if st.button("保存知识库权限", key=f"save-kb-policy-{kb_id}"):
            response = client.update_kb_access_policy(kb_id, selected_policy)
            if response.status_code == 200:
                _clear_api_cache()
                st.success("知识库权限已更新。")
                st.rerun()
            st.error(_response_error(response, "更新知识库权限失败"))

        workspace = st.session_state.auth_workspace
        workspace_id = str(workspace.get("workspace_id") or "")
        try:
            member_response = client.list_workspace_members(workspace_id)
            member_payload = response_payload(member_response)
        except Exception as exc:
            st.error(f"读取可授权成员失败: {exc}")
            return
        members = (
            member_payload.get("members", [])
            if member_response.status_code == 200
            and isinstance(member_payload, Mapping)
            else []
        )
        member_rows = [item for item in members if isinstance(item, Mapping)]
        subject_ids = [str(item.get("user_id") or "") for item in member_rows]
        subject_ids = [item for item in subject_ids if item]
        member_labels = {
            str(item.get("user_id") or ""): str(
                item.get("display_name") or item.get("email") or item.get("user_id")
            )
            for item in member_rows
        }

        if subject_ids:
            st.markdown("**知识库单独授权**")
            grant_columns = st.columns([3, 2, 1])
            subject_id = grant_columns[0].selectbox(
                "成员",
                subject_ids,
                format_func=lambda value: member_labels.get(value, value),
                key=f"kb-grant-subject-{kb_id}",
            )
            grant_role = grant_columns[1].selectbox(
                "权限",
                ["viewer", "reviewer", "editor"],
                key=f"kb-grant-role-{kb_id}",
            )
            if grant_columns[2].button("授权", key=f"kb-grant-save-{kb_id}"):
                response = client.grant_kb_access(kb_id, subject_id, grant_role)
                _handle_acl_grant_response(
                    response,
                    success_message="知识库授权已更新。",
                    failure_message="知识库授权失败",
                )
        grant_response = client.list_kb_grants(kb_id)
        grant_payload = response_payload(grant_response)
        grants = (
            grant_payload.get("grants", [])
            if grant_response.status_code == 200 and isinstance(grant_payload, Mapping)
            else []
        )
        for grant in grants:
            if not isinstance(grant, Mapping) or not grant.get("subject_id"):
                continue
            granted_subject = str(grant["subject_id"])
            row = st.columns([5, 1])
            row[0].caption(
                f"{member_labels.get(granted_subject, granted_subject)} · {grant.get('role') or '-'}"
            )
            if row[1].button("撤销", key=f"kb-grant-revoke-{kb_id}-{granted_subject}"):
                response = client.revoke_kb_access(kb_id, granted_subject)
                if response.status_code == 204:
                    st.rerun()
                st.error(_response_error(response, "撤销知识库授权失败"))

        document_options = {
            str(item.get("document_id") or ""): str(item.get("name") or "")
            for item in documents
            if item.get("document_id") and item.get("name")
        }
        if not document_options:
            return
        st.markdown("**文档级权限**")
        document_id = st.selectbox(
            "文档",
            list(document_options),
            format_func=lambda value: document_options.get(value, value),
            key=f"document-policy-target-{kb_id}",
        )
        document_response = client.get_document_access_policy(kb_id, document_id)
        document_payload = response_payload(document_response)
        document_configured = document_response.status_code == 200 and isinstance(
            document_payload, Mapping
        )
        if document_response.status_code not in {200, 404}:
            st.error(_response_error(document_response, "读取文档权限失败"))
            return
        if not document_configured:
            st.caption("该文档尚未建立 ACL，保存后将以稳定 document_id 初始化。")
            document_payload = {}
        document_policy = str(document_payload.get("policy") or "inherit")
        selected_document_policy = st.selectbox(
            "文档可见性",
            ["inherit", "workspace", "private"],
            index=["inherit", "workspace", "private"].index(document_policy),
            format_func=lambda value: {
                "inherit": "继承知识库",
                "workspace": "工作区成员",
                "private": "仅授权成员",
            }[value],
            key=f"document-policy-{kb_id}-{document_id}",
        )
        if st.button("保存文档权限", key=f"save-document-policy-{kb_id}"):
            response = client.update_document_access_policy(
                kb_id,
                document_id,
                selected_document_policy,
                source=None if document_configured else document_options[document_id],
            )
            if response.status_code == 200:
                _clear_api_cache()
                st.success("文档权限已更新。")
                st.rerun()
            st.error(_response_error(response, "更新文档权限失败"))
        if subject_ids and document_configured:
            doc_columns = st.columns([3, 2, 1])
            doc_subject = doc_columns[0].selectbox(
                "文档授权成员",
                subject_ids,
                format_func=lambda value: member_labels.get(value, value),
                key=f"document-grant-subject-{kb_id}-{document_id}",
            )
            doc_role = doc_columns[1].selectbox(
                "文档权限",
                ["viewer", "reviewer", "editor"],
                key=f"document-grant-role-{kb_id}-{document_id}",
            )
            if doc_columns[2].button(
                "授权", key=f"document-grant-save-{kb_id}-{document_id}"
            ):
                response = client.grant_document_access(
                    kb_id, document_id, doc_subject, doc_role
                )
                _handle_acl_grant_response(
                    response,
                    success_message="文档授权已更新。",
                    failure_message="文档授权失败",
                )
        if document_configured:
            document_grant_response = client.list_document_grants(kb_id, document_id)
            document_grant_payload = response_payload(document_grant_response)
            document_grants = (
                document_grant_payload.get("grants", [])
                if document_grant_response.status_code == 200
                and isinstance(document_grant_payload, Mapping)
                else []
            )
            for grant in document_grants:
                if not isinstance(grant, Mapping) or not grant.get("subject_id"):
                    continue
                granted_subject = str(grant["subject_id"])
                row = st.columns([5, 1])
                row[0].caption(
                    f"{member_labels.get(granted_subject, granted_subject)} · {grant.get('role') or '-'}"
                )
                if row[1].button(
                    "撤销",
                    key=(
                        f"document-grant-revoke-{kb_id}-{document_id}-{granted_subject}"
                    ),
                ):
                    response = client.revoke_document_access(
                        kb_id, document_id, granted_subject
                    )
                    if response.status_code == 204:
                        st.rerun()
                    st.error(_response_error(response, "撤销文档授权失败"))


# 完成 侧边栏 处理。
def _connection_workbench(client: CogDocClient, kb_id: str) -> None:
    """Keep the sidebar as a radar; detailed operations live in the main view."""

    if st.session_state.main_views_by_context.get(_context_key(kb_id)) == "来源":
        st.caption("来源航海台已在主区打开")
        return
    try:
        connection_response = client.list_connections(kb_id)
        health_response = client.list_connection_health(kb_id)
    except Exception as exc:
        st.caption(f"来源雷达暂不可用：{exc}")
        return
    connections, connection_error = _source_console_rows(
        connection_response, "connections", "读取来源连接失败"
    )
    health_rows, health_error = _source_console_rows(
        health_response, "connections", "读取连接健康失败"
    )
    if connection_error:
        st.caption(connection_error)
        return
    health_by_id = {str(row.get("connection_id") or ""): row for row in health_rows}
    active_count = sum(bool(row.get("enabled")) for row in connections)
    alert_count = sum(
        str(row.get("health_status") or "unknown") in {"failed", "dead_letter"}
        for row in health_rows
    )
    with st.expander(f"来源雷达 · {active_count} 条航线", expanded=False):
        if health_error:
            st.caption(health_error)
        if not connections:
            st.caption("尚未接入来源。打开航海台建立第一条航线。")
        for connection in connections[:4]:
            connection_id = str(connection.get("connection_id") or "")
            health = health_by_id.get(connection_id, {})
            status = str(health.get("health_status") or "unknown")
            marker = "●" if status == "healthy" else "◆" if status == "syncing" else "○"
            st.caption(
                f"{marker} {connection.get('name') or connection_id} · "
                f"{_source_status_label(status)}"
            )
        if len(connections) > 4:
            st.caption(f"另有 {len(connections) - 4} 条航线")
        if alert_count:
            st.warning(f"{alert_count} 条航线需要处理")
        if st.button(
            "打开来源航海台 →",
            key=f"open-source-console-{kb_id}",
            use_container_width=True,
        ):
            st.session_state.main_views_by_context[_context_key(kb_id)] = "来源"
            st.session_state[f"main-view-{kb_id}-{st.session_state.session_id}"] = (
                "来源"
            )
            st.rerun()


def _source_console_rows(
    response, key: str, fallback: str
) -> tuple[list[Mapping], str | None]:
    status, payload = _response_status_payload(response)
    if status != 200:
        return [], format_api_error(payload, status, fallback)
    if not isinstance(payload, Mapping) or not isinstance(payload.get(key), list):
        return [], f"{fallback}：响应格式不符合预期"
    return [row for row in payload[key] if isinstance(row, Mapping)], None


def _source_console_mapping(response, fallback: str) -> tuple[Mapping, str | None]:
    status, payload = _response_status_payload(response)
    if status != 200:
        return {}, format_api_error(payload, status, fallback)
    if not isinstance(payload, Mapping):
        return {}, f"{fallback}：响应格式不符合预期"
    return payload, None


def _source_status_label(value: object) -> str:
    return {
        "unknown": "等待首航",
        "queued": "已排队",
        "syncing": "同步中",
        "retrying": "等待重试",
        "healthy": "健康",
        "degraded": "降级",
        "stale": "已过期",
        "error": "来源错误",
        "failed": "同步失败",
        "dead_letter": "死信",
        "cancelled": "已取消",
        "pending": "已排队",
        "running": "同步中",
        "committing": "提交中",
        "retry_wait": "等待重试",
        "succeeded": "已完成",
    }.get(str(value or "unknown"), str(value or "未知"))


def _source_status_class(value: object) -> str:
    status = str(value or "unknown")
    if status in {"healthy", "succeeded"}:
        return "is-healthy"
    if status in {"syncing", "running", "committing", "queued", "pending"}:
        return "is-moving"
    if status in {"failed", "dead_letter", "error"}:
        return "is-fault"
    if status in {"retrying", "retry_wait", "degraded", "stale"}:
        return "is-warning"
    return "is-muted"


def _source_format_bytes(value: object) -> str:
    if not isinstance(value, (str, int, float)):
        return "—"
    try:
        size = max(0.0, float(value or 0))
    except (TypeError, ValueError):
        return "—"
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return "—"


def _source_format_time(value: object) -> str:
    if value is None:
        return "—"
    if not isinstance(value, (str, int, float)):
        return "—"
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(value)))
    except (TypeError, ValueError, OSError, OverflowError):
        return "—"


def _source_navigation_styles() -> None:
    st.markdown(
        """
        <style>
        :root {
          --nav-abyss:#071b2d; --nav-sounding:#0d3554; --nav-signal:#28c5d9;
          --nav-foam:#eaf6f8; --nav-amber:#d99a2b; --nav-fault:#d85959;
        }
        .source-hero {background:linear-gradient(112deg,var(--nav-abyss),#0a2942 72%,#0d4057);
          color:white;padding:1.2rem 1.35rem 1rem;border-top:5px solid var(--nav-signal);
          margin:.15rem 0 1rem;box-shadow:0 12px 30px rgba(7,27,45,.13)}
        .source-eyebrow,.source-ledger,.source-route,.source-pulse,.source-meta {
          font-family:"IBM Plex Mono","SFMono-Regular",Consolas,monospace}
        .source-eyebrow {font-size:.68rem;letter-spacing:.12em;text-transform:uppercase;
          color:#8de5ef;font-weight:760}
        .source-hero h2 {font-family:"Noto Sans SC","PingFang SC",sans-serif;
          font-size:1.85rem;letter-spacing:-.045em;margin:.12rem 0 .18rem}
        .source-hero p {color:#c9e1e7;margin:0;max-width:55rem;line-height:1.65}
        .source-pulse {display:grid;grid-template-columns:repeat(4,1fr);margin-top:1rem;
          border-top:1px solid #35647b;position:relative}
        .source-pulse-stage {font-size:.68rem;color:#9bbac7;padding:.72rem .35rem .1rem 1rem;
          position:relative;letter-spacing:.035em}
        .source-pulse-stage::before {content:"";position:absolute;left:0;top:-.32rem;width:.58rem;
          height:.58rem;border-radius:50%;background:#55778a;border:2px solid var(--nav-abyss)}
        .source-pulse-stage.is-healthy::before {background:var(--nav-signal)}
        .source-pulse-stage.is-moving::before {background:#fff;box-shadow:0 0 0 0 rgba(40,197,217,.6);
          animation:source-ping 1.6s ease-out infinite}
        .source-pulse-stage.is-warning::before {background:var(--nav-amber)}
        .source-pulse-stage.is-fault::before {background:var(--nav-fault)}
        @keyframes source-ping {60%{box-shadow:0 0 0 .55rem rgba(40,197,217,0)}100%{box-shadow:none}}
        .source-ledger {border-top:1px solid #b9ced7;border-bottom:1px solid #b9ced7;
          padding:.48rem 0;color:#244559;font-size:.7rem;letter-spacing:.035em;margin:.2rem 0 .7rem}
        .source-route {display:grid;grid-template-columns:minmax(0,1.5fr) auto;gap:.7rem;
          align-items:start;padding:.72rem .1rem;border-bottom:1px solid #c7d8df}
        .source-route-name {font-family:"Noto Sans SC","PingFang SC",sans-serif;
          color:#0b2639;font-weight:720;overflow-wrap:anywhere}
        .source-meta {font-size:.66rem;color:#587180;line-height:1.65;margin-top:.12rem}
        .source-state {font-size:.66rem;border:1px solid #87a9b8;padding:.18rem .4rem;
          color:#31566a;white-space:nowrap}
        .source-state.is-healthy {border-color:#258a95;color:#116874;background:#e7f7f8}
        .source-state.is-moving {border-color:#238da8;color:#0d667d;background:#e5f5f8}
        .source-state.is-warning {border-color:#b57c18;color:#7e560f;background:#fff6e2}
        .source-state.is-fault {border-color:#bd4b4b;color:#9b3030;background:#fff0f0}
        .source-trace {background:var(--nav-foam);border-left:4px solid var(--nav-signal);
          padding:.66rem .8rem;margin:.5rem 0 .9rem;color:#173b50;font-size:.7rem;
          font-family:"IBM Plex Mono","SFMono-Regular",Consolas,monospace;overflow-wrap:anywhere}
        .source-trace b {color:#08768a}.source-diff {border-left:4px solid var(--nav-sounding)}
        div[data-testid="stAppViewContainer"] button:focus-visible,
        div[data-testid="stAppViewContainer"] a:focus-visible {outline:3px solid var(--nav-signal)!important;
          outline-offset:2px}
        @media (prefers-reduced-motion:reduce){.source-pulse-stage.is-moving::before{animation:none}}
        @media (max-width:720px){.source-hero{padding:1rem}.source-hero h2{font-size:1.5rem}
          .source-pulse{grid-template-columns:1fr 1fr}.source-route{grid-template-columns:1fr}
          .source-state{justify-self:start}}
        </style>
        """,
        unsafe_allow_html=True,
    )


def _source_navigation_hero(
    connections: Sequence[Mapping],
    health_rows: Sequence[Mapping],
    jobs: Sequence[Mapping],
    usage: Mapping,
) -> None:
    health_values = [str(row.get("health_status") or "unknown") for row in health_rows]
    latest_status = str(jobs[0].get("status") or "unknown") if jobs else "unknown"
    connection_status = (
        "failed"
        if any(value in {"failed", "dead_letter"} for value in health_values)
        else "syncing"
        if any(value in {"syncing", "queued", "retrying"} for value in health_values)
        else "healthy"
        if health_values and all(value == "healthy" for value in health_values)
        else "unknown"
    )
    active_versions = int(usage.get("active_versions") or 0)
    stages = (
        ("01 连接健康", connection_status),
        ("02 同步任务", latest_status),
        ("03 来源版本", "healthy" if active_versions else "unknown"),
        ("04 ACL / 引用", "healthy" if active_versions else "unknown"),
    )
    stage_markup = "".join(
        f'<div class="source-pulse-stage {_source_status_class(status)}" role="listitem">'
        f"{html.escape(label)}</div>"
        for label, status in stages
    )
    st.markdown(
        f"""
        <section class="source-hero">
          <div class="source-eyebrow">Source operations / {len(connections):02d} routes</div>
          <h2>来源航海台</h2>
          <p>沿一条可审计的同步脉冲，从连接健康追到原始版本、文档权限与引用身份。</p>
          <div class="source-pulse" role="list" aria-label="来源同步链路">{stage_markup}</div>
        </section>
        """,
        unsafe_allow_html=True,
    )


def _source_render_connection_routes(
    client: CogDocClient,
    kb_id: str,
    connections: Sequence[Mapping],
    health_rows: Sequence[Mapping],
    jobs: Sequence[Mapping],
) -> None:
    health_by_id = {str(row.get("connection_id") or ""): row for row in health_rows}
    latest_by_connection: dict[str, Mapping] = {}
    for job in jobs:
        connection_id = str(job.get("connection_id") or "")
        latest_by_connection.setdefault(connection_id, job)
    st.markdown("### 航线与同步")
    st.markdown(
        f"<div class='source-ledger'>ROUTES {len(connections):03d}　"
        f"BACKLOG {sum(int(row.get('backlog') or 0) for row in health_rows):03d}</div>",
        unsafe_allow_html=True,
    )
    if not connections:
        st.info("还没有来源连接。展开“接入新航线”建立第一条可同步航线。")
    for connection in connections:
        connection_id = str(connection.get("connection_id") or "")
        health = health_by_id.get(connection_id, {})
        latest = latest_by_connection.get(connection_id, {})
        status = str(health.get("health_status") or "unknown")
        name = html.escape(str(connection.get("name") or connection_id))
        connector_type = html.escape(str(connection.get("connector_type") or "—"))
        credential_source = html.escape(
            str(connection.get("credential_source") or "none")
        )
        st.markdown(
            f"""
            <article class="source-route">
              <div><div class="source-route-name">{name}</div>
              <div class="source-meta">{connector_type} · AUTH {credential_source} ·
              LAST {_source_format_time(health.get("last_success_at"))} ·
              BACKLOG {int(health.get("backlog") or 0)}</div></div>
              <span class="source-state {_source_status_class(status)}">
              {html.escape(_source_status_label(status))}</span>
            </article>
            """,
            unsafe_allow_html=True,
        )
        actions = st.columns([1, 1, 1])
        if actions[0].button(
            "立即同步",
            key=f"source-sync-{kb_id}-{connection_id}",
            use_container_width=True,
        ):
            response = client.start_connection_sync(kb_id, connection_id)
            if response.status_code == 202:
                st.success("同步任务已进入队列。")
                st.rerun()
            else:
                st.error(_response_error(response, "启动同步失败"))
        enabled = bool(connection.get("enabled"))
        if actions[1].button(
            "暂停" if enabled else "启用",
            key=f"source-toggle-{kb_id}-{connection_id}",
            use_container_width=True,
        ):
            response = client.set_connection_enabled(kb_id, connection_id, not enabled)
            if response.status_code == 200:
                st.success("连接已暂停。" if enabled else "连接已启用。")
                st.rerun()
            else:
                st.error(_response_error(response, "更新连接失败"))
        replayable = str(latest.get("status") or "") == "dead_letter"
        if actions[2].button(
            "重放死信",
            key=f"source-replay-{kb_id}-{connection_id}",
            disabled=not replayable or not enabled,
            help="只会创建新任务，原死信记录保持不变。",
            use_container_width=True,
        ):
            response = client.replay_sync_job(kb_id, str(latest.get("job_id") or ""))
            if response.status_code == 202:
                st.success("已从死信创建新的同步任务。")
                st.rerun()
            else:
                st.error(_response_error(response, "重放同步任务失败"))
        if latest.get("error_code"):
            st.caption(
                f"最近错误：{latest.get('error_code')} · "
                f"{str(latest.get('error_message') or '')[:180]}"
            )


def _source_provider_connections(
    connections: Sequence[Mapping], provider: str
) -> list[Mapping]:
    return [
        row
        for row in connections
        if CONNECTOR_PROVIDER_ALIASES.get(str(row.get("connector_type") or ""))
        == provider
        and not row.get("credential_id")
        and str(row.get("credential_source") or "none") == "none"
    ]


def _source_render_credentials(
    client: CogDocClient, kb_id: str, connections: Sequence[Mapping]
) -> list[Mapping]:
    try:
        credential_response = client.list_connector_credentials(kb_id)
        credentials, credential_error = _source_console_rows(
            credential_response, "credentials", "读取连接凭据失败"
        )
    except Exception as exc:
        credentials, credential_error = [], f"读取连接凭据失败：{exc}"
    with st.expander(f"加密凭据 · {len(credentials)}", expanded=False):
        if credential_error:
            st.warning(credential_error)
            st.caption("凭据库未配置时，既有环境变量引用仍可继续使用。")
            return credentials
        credential_by_id = {
            str(row.get("credential_id") or ""): row for row in credentials
        }
        if credential_by_id:
            selected_id = st.selectbox(
                "凭据",
                list(credential_by_id),
                format_func=lambda value: (
                    f"{credential_by_id[value].get('label') or value} · "
                    f"{credential_by_id[value].get('provider') or 'unknown'}"
                ),
                key=f"source-credential-selected-{kb_id}",
            )
            selected = credential_by_id[selected_id]
            referenced_by = [
                str(connection.get("name") or connection.get("connection_id") or "")
                for connection in connections
                if str(connection.get("credential_id") or "") == selected_id
            ]
            is_bound = bool(selected.get("connection_id") or referenced_by)
            st.caption(
                f"{selected.get('credential_kind') or 'static'} · "
                f"字段 {', '.join(str(item) for item in selected.get('secret_fields') or [])} · "
                f"revision {selected.get('revision') or 1} · "
                f"最近使用 {_source_format_time(selected.get('last_used_at'))}"
            )
            if referenced_by:
                st.caption("使用连接：" + "、".join(referenced_by))
            if str(selected.get("credential_kind") or "") == "oauth":
                if st.button(
                    "刷新 OAuth 令牌",
                    key=f"source-credential-refresh-{kb_id}-{selected_id}",
                    use_container_width=True,
                ):
                    response = client.refresh_connector_credential(
                        kb_id,
                        selected_id,
                        expected_revision=int(selected.get("revision") or 1),
                    )
                    if response.status_code == 200:
                        st.success("OAuth 令牌已刷新。")
                        st.rerun()
                    else:
                        st.error(_response_error(response, "刷新 OAuth 令牌失败"))
            elif selected.get("secret_fields"):
                with st.form(
                    f"source-credential-rotate-{kb_id}-{selected_id}",
                    clear_on_submit=True,
                ):
                    rotated_values = {
                        str(field): st.text_input(
                            f"新的 {field}",
                            type="password",
                            autocomplete="off",
                        )
                        for field in selected.get("secret_fields") or []
                    }
                    rotate = st.form_submit_button("轮换密钥", use_container_width=True)
                if rotate:
                    if any(not str(value) for value in rotated_values.values()):
                        st.warning("轮换时请填写全部现有密钥字段。")
                    else:
                        response = client.rotate_connector_credential(
                            kb_id,
                            selected_id,
                            secret_values=rotated_values,
                            expected_revision=int(selected.get("revision") or 1),
                        )
                        if response.status_code == 200:
                            st.success("凭据已原子轮换；明文未写入页面状态。")
                            st.rerun()
                        else:
                            st.error(_response_error(response, "轮换凭据失败"))
            delete_columns = st.columns([2, 1])
            delete_confirmed = delete_columns[0].checkbox(
                "确认删除未绑定凭据",
                key=f"source-credential-delete-confirm-{kb_id}-{selected_id}",
                disabled=is_bound,
            )
            if delete_columns[1].button(
                "删除",
                key=f"source-credential-delete-{kb_id}-{selected_id}",
                disabled=not delete_confirmed or is_bound,
                use_container_width=True,
            ):
                response = client.delete_connector_credential(
                    kb_id,
                    selected_id,
                    expected_revision=int(selected.get("revision") or 1),
                )
                if response.status_code == 204:
                    st.success("凭据已删除。")
                    st.rerun()
                else:
                    st.error(_response_error(response, "删除凭据失败"))
        else:
            st.caption("凭据库可用，但这个知识库还没有保存凭据。")

        static_tab, oauth_tab, audit_tab = st.tabs(
            ["新增静态凭据", "OAuth 接入", "审计"]
        )
        with static_tab:
            provider = st.selectbox(
                "提供方",
                list(CONNECTOR_SECRET_FIELDS),
                key=f"source-static-provider-{kb_id}",
            )
            targets = _source_provider_connections(connections, provider)
            target_ids = [""] + [str(row.get("connection_id") or "") for row in targets]
            target_labels = {
                str(row.get("connection_id") or ""): str(row.get("name") or "")
                for row in targets
            }
            with st.form(f"source-credential-create-{kb_id}", clear_on_submit=True):
                label = st.text_input("凭据名称", placeholder="例如：产品空间只读令牌")
                target_id = st.selectbox(
                    "绑定连接（可稍后绑定）",
                    target_ids,
                    format_func=lambda value: target_labels.get(value, "暂不绑定"),
                )
                secret_values = {
                    field: st.text_input(
                        label_text + ("" if required else "（可选）"),
                        type="password",
                        autocomplete="off",
                    )
                    for field, label_text, required in CONNECTOR_SECRET_FIELDS[provider]
                }
                create_credential = st.form_submit_button(
                    "加密保存", use_container_width=True
                )
            if create_credential:
                required_fields = {
                    field
                    for field, _, required in CONNECTOR_SECRET_FIELDS[provider]
                    if required
                }
                clean_values = {
                    field: str(value)
                    for field, value in secret_values.items()
                    if str(value)
                }
                if not label.strip():
                    st.warning("请填写凭据名称。")
                elif not required_fields <= clean_values.keys():
                    st.warning("请填写全部必需密钥字段。")
                else:
                    payload = {
                        "provider": provider,
                        "credential_kind": "static",
                        "label": label.strip(),
                        "secret_values": clean_values,
                    }
                    if target_id:
                        payload["connection_id"] = target_id
                    response = client.create_connector_credential(kb_id, payload)
                    if response.status_code == 201:
                        st.success("凭据已加密保存；接口只返回字段名。")
                        st.rerun()
                    else:
                        st.error(_response_error(response, "保存凭据失败"))
        with oauth_tab:
            oauth_provider = st.selectbox(
                "OAuth 提供方",
                ["notion", "atlassian", "microsoft"],
                key=f"source-oauth-provider-{kb_id}",
            )
            oauth_targets = _source_provider_connections(connections, oauth_provider)
            oauth_ids = [""] + [
                str(row.get("connection_id") or "") for row in oauth_targets
            ]
            oauth_labels = {
                str(row.get("connection_id") or ""): str(row.get("name") or "")
                for row in oauth_targets
            }
            oauth_target = st.selectbox(
                "授权后绑定（可稍后绑定）",
                oauth_ids,
                format_func=lambda value: oauth_labels.get(value, "暂不绑定"),
                key=f"source-oauth-target-{kb_id}-{oauth_provider}",
            )
            if st.button(
                "生成一次性授权链接",
                key=f"source-oauth-start-{kb_id}-{oauth_provider}",
                use_container_width=True,
            ):
                response = client.authorize_connector_oauth(
                    kb_id,
                    oauth_provider,
                    connection_id=oauth_target or None,
                )
                payload = response_payload(response)
                if response.status_code in {200, 201} and isinstance(payload, Mapping):
                    authorization_url = str(payload.get("authorization_url") or "")
                    if authorization_url.startswith("https://"):
                        st.success("一次性授权会话已建立，请在有效期内完成授权。")
                        st.link_button(
                            f"前往 {oauth_provider} 授权 →",
                            authorization_url,
                            type="primary",
                            use_container_width=True,
                        )
                        if st.button(
                            "我已完成授权，刷新凭据列表",
                            key=f"source-oauth-finished-{kb_id}-{oauth_provider}",
                            use_container_width=True,
                        ):
                            st.rerun()
                    else:
                        st.error("OAuth 服务返回了无效授权地址。")
                else:
                    st.error(_response_error(response, "建立 OAuth 授权会话失败"))
        with audit_tab:
            load_audit = st.toggle(
                "加载最近 50 条凭据操作",
                value=False,
                key=f"source-credential-audit-load-{kb_id}",
            )
            if load_audit:
                event_response = client.list_connector_credential_events(
                    kb_id, limit=50
                )
                events, event_error = _source_console_rows(
                    event_response, "events", "读取凭据审计失败"
                )
                if event_error:
                    st.error(event_error)
                elif not events:
                    st.caption("还没有凭据操作记录。")
                else:
                    for event in events[:20]:
                        st.caption(
                            f"{_source_format_time(event.get('occurred_at'))} · "
                            f"{event.get('action') or 'unknown'} · "
                            f"{str(event.get('credential_id') or '')[:14]} · "
                            f"actor {str(event.get('actor_id') or '')[:14]}"
                        )
            else:
                st.caption("审计按需加载，不会在每次页面刷新时重复查询。")
    return credentials


def _source_render_connection_creator(
    client: CogDocClient,
    kb_id: str,
    credentials: Sequence[Mapping],
) -> None:
    examples = {
        "local-directory": '{"root":"/data/handbook","schedule_seconds":300}',
        "git": '{"repository":"/repos/docs","ref":"main","subpath":"docs"}',
        "url": '{"urls":["https://docs.example.com/guide"]}',
        "zotero": '{"library_type":"users","library_id":"123"}',
        "notion": '{"schedule_seconds":300}',
        "confluence": '{"base_url":"https://team.atlassian.net"}',
        "sharepoint": '{"site_id":"...","drive_id":"..."}',
        "s3": '{"bucket":"docs","region":"us-east-1","prefix":"manuals/"}',
    }
    secret_examples = {
        "zotero": '{"api_key":"COGDOC_ZOTERO_API_KEY"}',
        "notion": '{"token":"COGDOC_NOTION_TOKEN"}',
        "confluence": '{"token":"COGDOC_CONFLUENCE_TOKEN"}',
        "sharepoint": '{"token":"COGDOC_SHAREPOINT_TOKEN"}',
        "s3": '{"access_key":"AWS_ACCESS_KEY_ID","secret_key":"AWS_SECRET_ACCESS_KEY"}',
    }
    with st.expander("接入新航线", expanded=False):
        connector_type = st.selectbox(
            "来源类型",
            list(examples),
            key=f"source-new-connector-type-{kb_id}",
        )
        provider = CONNECTOR_PROVIDER_ALIASES.get(connector_type)
        matching_credentials = [
            row
            for row in credentials
            if str(row.get("provider") or "") == provider
            and not row.get("connection_id")
        ]
        auth_options = ["无需凭据"]
        if provider is not None:
            auth_options = ["加密凭据", "环境变量"]
        auth_mode = st.radio(
            "认证方式",
            auth_options,
            horizontal=True,
            key=f"source-new-auth-mode-{kb_id}-{connector_type}",
        )
        credential_by_id = {
            str(row.get("credential_id") or ""): row for row in matching_credentials
        }
        selected_credential = None
        if auth_mode == "加密凭据":
            if credential_by_id:
                selected_credential = st.selectbox(
                    "未绑定凭据",
                    list(credential_by_id),
                    format_func=lambda value: str(
                        credential_by_id[value].get("label") or value
                    ),
                    key=f"source-new-credential-{kb_id}-{connector_type}",
                )
            else:
                st.caption("没有匹配的未绑定凭据；请先在“加密凭据”中创建或完成 OAuth。")
        with st.form(
            f"source-create-connection-{kb_id}-{connector_type}-{auth_mode}",
            clear_on_submit=True,
        ):
            name = st.text_input("连接名称", placeholder="例如：产品手册")
            config_text = st.text_area(
                "位置与周期（JSON）", value=examples[connector_type], height=100
            )
            secret_text = "{}"
            if auth_mode == "环境变量":
                secret_text = st.text_area(
                    "密钥环境变量（JSON，只填变量名）",
                    value=secret_examples.get(connector_type, "{}"),
                    height=80,
                )
            workspace_visible = st.checkbox("同步后对工作区成员可见", value=False)
            create = st.form_submit_button("保存航线", use_container_width=True)
        if create:
            try:
                config = json.loads(config_text)
                secret_env = json.loads(secret_text)
                if not isinstance(config, dict) or not isinstance(secret_env, dict):
                    raise ValueError("JSON 顶层必须是对象")
                if not name.strip():
                    raise ValueError("连接名称不能为空")
                if auth_mode == "加密凭据" and not selected_credential:
                    raise ValueError("请先选择匹配的加密凭据")
                payload = {
                    "connector_type": connector_type,
                    "name": name.strip(),
                    "config": config,
                    "secret_env": secret_env if auth_mode == "环境变量" else {},
                    "workspace_visible": workspace_visible,
                }
                if selected_credential:
                    payload["credential_id"] = selected_credential
                response = client.create_connection(kb_id, payload)
                if response.status_code == 201:
                    st.success("航线已保存。")
                    st.rerun()
                else:
                    st.error(_response_error(response, "保存连接失败"))
            except (json.JSONDecodeError, ValueError) as exc:
                st.error(f"连接配置无效：{exc}")


def _source_render_job_ledger(
    client: CogDocClient, kb_id: str, jobs: Sequence[Mapping]
) -> None:
    with st.expander(f"同步任务账本 · {len(jobs)}", expanded=False):
        if not jobs:
            st.caption("还没有同步任务。")
            return
        job_by_id = {str(row.get("job_id") or ""): row for row in jobs}
        selected_id = st.selectbox(
            "任务",
            list(job_by_id),
            format_func=lambda value: (
                f"{_source_status_label(job_by_id[value].get('status'))} · "
                f"{value[:12]} · {job_by_id[value].get('connector_type') or 'unknown'}"
            ),
            key=f"source-job-ledger-{kb_id}",
        )
        job = job_by_id[selected_id]
        st.code(
            json.dumps(
                {
                    key: job.get(key)
                    for key in (
                        "job_id",
                        "connection_id",
                        "status",
                        "attempt",
                        "pages_processed",
                        "documents_seen",
                        "documents_fetched",
                        "deleted_seen",
                        "bytes_fetched",
                        "error_code",
                        "error_message",
                        "retry_at",
                        "replay_of",
                    )
                },
                ensure_ascii=False,
                indent=2,
            ),
            language="json",
        )
        if str(job.get("status") or "") == "dead_letter" and st.button(
            "从这条死信创建新任务",
            key=f"source-ledger-replay-{kb_id}-{selected_id}",
            use_container_width=True,
        ):
            response = client.replay_sync_job(kb_id, selected_id)
            if response.status_code == 202:
                st.success("新任务已创建，原任务保留用于审计。")
                st.rerun()
            else:
                st.error(_response_error(response, "重放同步任务失败"))


def _source_label(row: Mapping) -> str:
    name = str(row.get("display_name") or row.get("external_id") or "未命名来源")
    if len(name) > 54:
        name = name[:53] + "…"
    return f"{_source_status_label(row.get('health_status'))} · {name}"


def _source_render_acl(client: CogDocClient, kb_id: str, source: Mapping) -> None:
    document_id = str(source.get("document_id") or "")
    if not document_id:
        st.caption("该来源尚未物化为可引用文档，暂时没有文档 ACL 身份。")
        return
    configured = bool(source.get("access_configured"))
    current_policy = str(source.get("access_policy") or "inherit")
    policy_options: tuple[str, ...] = ("inherit", "workspace", "private")
    policy_labels = {
        "inherit": "继承知识库",
        "workspace": "工作区成员",
        "private": "仅授权成员",
    }
    policy = st.selectbox(
        "文档可见性",
        policy_options,
        index=policy_options.index(current_policy)
        if current_policy in policy_options
        else 0,
        format_func=lambda value: policy_labels.get(value, value),
        key=f"source-acl-{kb_id}-{document_id}",
    )
    if st.button(
        "保存来源权限",
        key=f"source-acl-save-{kb_id}-{document_id}",
        use_container_width=True,
    ):
        metadata = source.get("metadata")
        materialized_name = (
            str(metadata.get("materialized_name") or "")
            if isinstance(metadata, Mapping)
            else ""
        )
        response = client.update_document_access_policy(
            kb_id,
            document_id,
            str(policy or current_policy),
            source=None
            if configured
            else materialized_name or str(source.get("display_name") or ""),
        )
        if response.status_code == 200:
            st.success("来源权限已更新；后续查询按新的 ACL 判定。")
            st.rerun()
        else:
            st.error(_response_error(response, "更新来源权限失败"))


def _source_render_versions(client: CogDocClient, kb_id: str, source: Mapping) -> None:
    source_id = str(source.get("source_id") or "")
    version_response = client.list_source_versions(kb_id, source_id)
    versions, version_error = _source_console_rows(
        version_response, "versions", "读取来源版本失败"
    )
    if version_error:
        st.error(version_error)
        return
    if not versions:
        st.info("这个来源还没有可审计版本。")
        return
    version_by_id = {str(row.get("version_id") or ""): row for row in versions}
    version_ids = list(version_by_id)
    current_id = next(
        (value for value in version_ids if version_by_id[value].get("is_current")),
        version_ids[0],
    )
    st.markdown(
        f"<div class='source-ledger'>VERSIONS {len(versions):03d}　"
        f"CURRENT {html.escape(current_id[:16])}　"
        f"BYTES {_source_format_bytes(source.get('byte_size'))}</div>",
        unsafe_allow_html=True,
    )
    selected_id = st.selectbox(
        "版本内容",
        version_ids,
        format_func=lambda value: (
            ("当前 · " if version_by_id[value].get("is_current") else "历史 · ")
            + _source_format_time(version_by_id[value].get("fetched_at"))
            + (
                " · 原件可用"
                if version_by_id[value].get("artifact_available")
                else " · 仅元数据"
            )
        ),
        key=f"source-version-selected-{kb_id}-{source_id}",
    )
    selected = version_by_id[selected_id]
    content_columns = st.columns([1, 1])
    if content_columns[0].button(
        "准备下载原件",
        key=f"source-version-download-prepare-{kb_id}-{source_id}-{selected_id}",
        disabled=not bool(selected.get("artifact_available")),
        use_container_width=True,
    ):
        response = client.download_source_version(kb_id, source_id, selected_id)
        if response.status_code == 200:
            content_columns[1].download_button(
                "下载这个版本",
                data=response.content,
                file_name=str(
                    source.get("display_name") or f"{source_id}-{selected_id}"
                ),
                mime=response.headers.get("content-type", "application/octet-stream"),
                key=f"source-version-download-{kb_id}-{source_id}-{selected_id}",
                use_container_width=True,
            )
        else:
            st.error(_response_error(response, "读取版本原件失败"))

    if len(version_ids) >= 2:
        diff_columns = st.columns(2)
        default_from = next(
            (value for value in reversed(version_ids) if value != current_id),
            version_ids[-1],
        )
        from_id = diff_columns[0].selectbox(
            "对比基线",
            version_ids,
            index=version_ids.index(default_from),
            key=f"source-version-from-{kb_id}-{source_id}",
        )
        to_id = diff_columns[1].selectbox(
            "目标版本",
            version_ids,
            index=version_ids.index(current_id),
            key=f"source-version-to-{kb_id}-{source_id}",
        )
        if st.button(
            "生成有界差异",
            key=f"source-version-diff-{kb_id}-{source_id}",
            disabled=from_id == to_id,
            use_container_width=True,
        ):
            response = client.diff_source_versions(kb_id, source_id, from_id, to_id)
            payload = response_payload(response)
            if response.status_code != 200 or not isinstance(payload, Mapping):
                st.error(_response_error(response, "生成版本差异失败"))
            else:
                st.markdown(
                    f"<div class='source-trace source-diff'>DIFF / "
                    f"+{int(payload.get('added_lines') or 0)}　"
                    f"-{int(payload.get('removed_lines') or 0)}　"
                    f"{html.escape(str(payload.get('kind') or 'unknown'))}"
                    f"{'　TRUNCATED' if payload.get('truncated') else ''}</div>",
                    unsafe_allow_html=True,
                )
                if payload.get("diff"):
                    st.code(str(payload["diff"]), language="diff")
                else:
                    st.info("这是二进制版本；已核对摘要，但没有可展示的逐行差异。")

    historical = [
        value
        for value in version_ids
        if not version_by_id[value].get("is_current")
        and version_by_id[value].get("artifact_available")
    ]
    recovery_key = f"{kb_id}:{source_id}"
    recovery = st.session_state.source_artifact_recovery.get(recovery_key)
    with st.expander("原件保留与恢复", expanded=False):
        if historical:
            delete_id = st.selectbox(
                "可删除历史原件",
                historical,
                key=f"source-artifact-delete-version-{kb_id}-{source_id}",
            )
            confirm = st.checkbox(
                "我确认只删除历史原件；版本元数据与当前在线版本不受影响",
                key=f"source-artifact-delete-confirm-{kb_id}-{source_id}-{delete_id}",
            )
            if st.button(
                "移入可恢复区",
                key=f"source-artifact-delete-{kb_id}-{source_id}-{delete_id}",
                disabled=not confirm,
                use_container_width=True,
            ):
                response = client.delete_source_artifact(kb_id, source_id, delete_id)
                payload = response_payload(response)
                if response.status_code == 200 and isinstance(payload, Mapping):
                    st.session_state.source_artifact_recovery[recovery_key] = {
                        "token": str(payload.get("recovery_token") or ""),
                        "version_id": delete_id,
                    }
                    st.success("历史原件已移入可恢复区。")
                    st.rerun()
                else:
                    st.error(_response_error(response, "删除历史原件失败"))
        else:
            st.caption("没有可删除的历史原件；当前在线版本始终受保护。")
        if isinstance(recovery, Mapping) and recovery.get("token"):
            st.caption(f"待恢复版本：{recovery.get('version_id')}")
            if st.button(
                "恢复最近移除的原件",
                key=f"source-artifact-restore-{kb_id}-{source_id}",
                use_container_width=True,
            ):
                response = client.restore_source_artifact(
                    kb_id, str(recovery.get("token") or "")
                )
                if response.status_code == 200:
                    st.session_state.source_artifact_recovery.pop(recovery_key, None)
                    st.success("原件已恢复到活动版本库。")
                    st.rerun()
                else:
                    st.error(_response_error(response, "恢复来源原件失败"))


def _source_render_browser(
    client: CogDocClient,
    kb_id: str,
    connections: Sequence[Mapping],
    health_rows: Sequence[Mapping],
) -> None:
    st.markdown("### 来源、版本与权限")
    connection_labels = {
        str(row.get("connection_id") or ""): str(row.get("name") or "")
        for row in connections
    }
    filters = st.columns([2, 2, 1])
    connection_filter = filters[0].selectbox(
        "航线筛选",
        [""] + list(connection_labels),
        format_func=lambda value: connection_labels.get(value, "全部航线"),
        key=f"source-catalog-connection-{kb_id}",
    )
    health_filter = filters[1].selectbox(
        "来源状态",
        ["", "healthy", "syncing", "degraded", "stale", "error", "unknown"],
        format_func=lambda value: _source_status_label(value) if value else "全部状态",
        key=f"source-catalog-health-{kb_id}",
    )
    include_deleted = filters[2].toggle(
        "含已删除", value=False, key=f"source-catalog-deleted-{kb_id}"
    )
    try:
        response = client.list_source_catalog(
            kb_id,
            connection_id=connection_filter or None,
            health_status=health_filter or None,
            include_deleted=include_deleted,
        )
        sources, source_error = _source_console_rows(
            response, "sources", "读取来源目录失败"
        )
    except Exception as exc:
        sources, source_error = [], f"读取来源目录失败：{exc}"
    if source_error:
        st.error(source_error)
        st.caption("来源目录需要知识库管理权限；普通读者仍可从“文档”列表访问获准内容。")
        return
    st.markdown(
        f"<div class='source-ledger'>VISIBLE {len(sources):03d}　"
        f"HEALTHY {sum(str(row.get('health_status')) == 'healthy' for row in sources):03d}　"
        f"STALE {sum(str(row.get('health_status')) == 'stale' for row in sources):03d}</div>",
        unsafe_allow_html=True,
    )
    if not sources:
        st.info("当前筛选下没有来源。先同步一条连接，或调整航线与健康状态筛选。")
        return
    source_by_id = {str(row.get("source_id") or ""): row for row in sources}
    selected_id = st.selectbox(
        "来源目录",
        list(source_by_id),
        format_func=lambda value: _source_label(source_by_id[value]),
        key=f"source-catalog-selected-{kb_id}",
    )
    detail_response = client.get_source_catalog_entry(kb_id, selected_id)
    source, detail_error = _source_console_mapping(detail_response, "读取来源详情失败")
    if detail_error:
        st.error(detail_error)
        return
    connection_id = str(source.get("connection_id") or "")
    health_by_id = {str(row.get("connection_id") or ""): row for row in health_rows}
    connection_health = str(
        health_by_id.get(connection_id, {}).get("health_status") or "unknown"
    )
    acl_label = (
        str(source.get("access_policy") or "inherit")
        if source.get("access_configured")
        else "未配置"
    )
    st.markdown(
        f"""
        <div class="source-trace"><b>CONNECTION</b>
        {html.escape(connection_labels.get(connection_id, connection_id or "manual"))}
        [{html.escape(_source_status_label(connection_health))}]　→　
        <b>VERSION</b> {html.escape(str(source.get("version_id") or "")[:18])}　→　
        <b>ACL</b> {html.escape(acl_label)}　→　
        <b>CITATION</b> {html.escape(str(source.get("document_id") or "待物化"))}</div>
        """,
        unsafe_allow_html=True,
    )
    heading = html.escape(str(source.get("display_name") or selected_id))
    st.markdown(f"#### {heading}")
    st.caption(
        f"{source.get('connector_type') or 'unknown'} · {source.get('media_type') or 'unknown'} · "
        f"来源同步 {_source_format_time(source.get('last_sync_at'))} · "
        f"原件 {_source_format_bytes(source.get('byte_size'))}"
    )
    if source.get("last_sync_error"):
        st.error(f"来源错误：{source.get('last_sync_error')}")
    details, versions = st.tabs(["身份与权限", "版本航迹"])
    with details:
        detail_columns = st.columns([3, 2])
        with detail_columns[0]:
            origin_uri = str(source.get("origin_uri") or "")
            if origin_uri:
                st.caption(f"原始位置：{origin_uri}")
            st.code(
                json.dumps(
                    {
                        "source_id": source.get("source_id"),
                        "external_id": source.get("external_id"),
                        "content_sha256": source.get("content_sha256"),
                        "etag": source.get("etag"),
                        "modified_at": source.get("modified_at"),
                        "document_id": source.get("document_id"),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                language="json",
            )
            with st.expander("提供方元数据", expanded=False):
                st.json(source.get("metadata") or {})
        with detail_columns[1]:
            _source_render_acl(client, kb_id, source)
    with versions:
        _source_render_versions(client, kb_id, source)


def _source_navigation_console(client: CogDocClient, kb_id: str | None) -> None:
    _source_navigation_styles()
    if not kb_id:
        st.info("先选择一个知识库，再进入来源航海台。")
        return
    if st.session_state.get(
        "auth_mode"
    ) == "account" and "manage_access" not in st.session_state.get(
        "auth_permissions", []
    ):
        st.warning("来源航海台只向知识库管理员开放；当前账号可继续使用已授权文档。")
        return
    try:
        connection_response = client.list_connections(kb_id)
        health_response = client.list_connection_health(kb_id)
        job_response = client.list_sync_jobs(kb_id)
        usage_response = client.get_source_artifact_usage(kb_id)
    except Exception as exc:
        st.error(f"来源航海台无法连接后端：{exc}")
        return
    connections, connection_error = _source_console_rows(
        connection_response, "connections", "读取来源连接失败"
    )
    health_rows, health_error = _source_console_rows(
        health_response, "connections", "读取连接健康失败"
    )
    jobs, job_error = _source_console_rows(job_response, "jobs", "读取同步任务失败")
    usage, usage_error = _source_console_mapping(usage_response, "读取来源原件用量失败")
    fatal_errors = [
        error for error in (connection_error, health_error, job_error) if error
    ]
    if fatal_errors:
        for error in fatal_errors:
            st.error(error)
        return
    if usage_error:
        st.caption(usage_error)
    _source_navigation_hero(connections, health_rows, jobs, usage)
    summary = st.columns(4)
    summary[0].metric("活动航线", sum(bool(row.get("enabled")) for row in connections))
    summary[1].metric(
        "同步积压", sum(int(row.get("backlog") or 0) for row in health_rows)
    )
    summary[2].metric("保留版本", int(usage.get("active_versions") or 0))
    summary[3].metric("原件占用", _source_format_bytes(usage.get("active_bytes")))
    left, right = st.columns([0.38, 0.62], gap="large")
    with left:
        _source_render_connection_routes(client, kb_id, connections, health_rows, jobs)
        credentials = _source_render_credentials(client, kb_id, connections)
        _source_render_connection_creator(client, kb_id, credentials)
        _source_render_job_ledger(client, kb_id, jobs)
    with right:
        _source_render_browser(client, kb_id, connections, health_rows)


def _sidebar() -> None:
    # 侧栏：后端地址、模式开关、知识库选择/新建/上传入库/文档列表。
    with st.sidebar:
        client = _client()
        _render_account_sidebar(client)
        st.session_state.is_local = st.toggle("本地 Ollama 模式", value=False)

        st.divider()
        st.subheader("知识库")
        try:
            kbs = _cached_api_value(
                ("kbs", client.base_url), client.list_knowledge_bases
            )
        except CogDocAPIError as exc:
            if exc.status_code is not None:
                _observe_authenticated_response(exc.status_code)
            st.error(f"读取知识库失败: {exc}")
            return
        except Exception as exc:
            st.error(f"连不上后端: {exc}")
            return

        if not isinstance(kbs, list):
            st.error(f"意外的响应格式: {kbs}")
            return
        if not all(
            isinstance(kb, Mapping) and isinstance(kb.get("kb_id"), str) for kb in kbs
        ):
            st.error(f"知识库列表响应缺少 kb_id: {kbs}")
            return
        kb_ids = [kb["kb_id"] for kb in kbs]
        if kb_ids:
            # 知识库选择也持久化进地址栏，刷新后定位回原库。
            url_kb = st.query_params.get("kb")
            default_idx = kb_ids.index(url_kb) if url_kb in kb_ids else 0
            st.session_state.kb_id = st.selectbox(
                "选择知识库", kb_ids, index=default_idx
            )
            st.query_params["kb"] = st.session_state.kb_id
        else:
            st.session_state.kb_id = None
            st.info("还没有知识库，先在下面新建一个。")

        with st.form("create_kb", clear_on_submit=True):
            new_kb = st.text_input("新建知识库 ID")
            new_kb_policy = st.selectbox(
                "初始权限",
                ["workspace", "private"],
                format_func=lambda value: (
                    "仅自己/授权成员" if value == "private" else "工作区成员"
                ),
            )
            if st.form_submit_button("创建") and new_kb:
                resp = client.create_knowledge_base(new_kb, access_policy=new_kb_policy)
                if resp.status_code == 201:
                    _clear_api_cache(("kbs", client.base_url))
                    st.success(f"已创建 {new_kb}")
                    st.rerun()
                else:
                    st.error(resp.json().get("message", resp.text))

        if not st.session_state.kb_id:
            return

        kb_id = st.session_state.kb_id
        pending_total = _pending_review_count(client, kb_id)
        if pending_total:
            st.caption(f"待处理审核 {pending_total}")

        st.divider()
        _conversations(client, kb_id)

        st.divider()
        st.subheader("文档")
        _connection_workbench(client, kb_id)
        uploaded = st.file_uploader(
            "上传文件",
            type=[
                "pdf",
                "md",
                "markdown",
                "txt",
                "html",
                "htm",
                "docx",
                "pptx",
                "xlsx",
                "png",
                "jpg",
                "jpeg",
                "tif",
                "tiff",
                "bmp",
                "webp",
            ],
            help="支持文档、演示文稿、表格、网页和图片；图片会在 OCR 可用时提取文字。",
        )
        if st.button("上传并入库", disabled=uploaded is None):
            if uploaded is None:  # disabled controls are not a static type guard
                st.error("请先选择要上传的文件")
                return
            resp = client.upload_document(kb_id, uploaded.name, uploaded.getvalue())
            if resp.status_code != 202:
                st.error(resp.json().get("message", resp.text))
            else:
                _poll_job(client, resp.json()["job_id"])
                _clear_api_cache(("documents", client.base_url, kb_id))
                _clear_api_cache(("sources", client.base_url, kb_id))
                _clear_api_cache(("chunks", client.base_url, kb_id))
                _clear_api_cache(("kbs", client.base_url))
                st.rerun()

        try:
            status_code, docs = _cached_api_value(
                ("documents", client.base_url, kb_id),
                lambda: _response_status_payload(client.list_documents(kb_id)),
            )
            if status_code != 200:
                st.error(
                    "读取文档列表失败: "
                    f"{format_api_error(docs, status_code, '读取文档列表失败')}"
                )
                docs = []
        except Exception as exc:
            st.error(f"读取文档列表失败: {exc}")
            docs = []
        if docs and not isinstance(docs, list):
            st.error(f"文档列表响应格式不符合预期: {docs}")
            docs = []
        if isinstance(docs, list) and docs:
            st.caption(f"文档 ({len(docs)})")
            for doc in docs:
                if not isinstance(doc, Mapping) or not doc.get("name"):
                    st.error(f"文档列表项格式不符合预期: {doc}")
                    continue
                row = st.columns([5, 1])
                row[0].write(doc["name"])
                if row[1].button("🗑", key=f"del-{doc['name']}"):
                    client.delete_document(kb_id, doc["name"])
                    _clear_api_cache(("documents", client.base_url, kb_id))
                    _clear_api_cache(("sources", client.base_url, kb_id))
                    _clear_api_cache(("chunks", client.base_url, kb_id))
                    _clear_api_cache(("kbs", client.base_url))
                    st.rerun()

        _render_resource_access_controls(
            client,
            kb_id,
            [item for item in docs if isinstance(item, Mapping)]
            if isinstance(docs, list)
            else [],
        )

        _render_source_browser(client, kb_id)

        with st.expander("⚠️ 删除知识库"):
            st.caption("会删除该库全部文档与索引，不可恢复。")
            if st.button("确认删除此知识库", key="del_kb"):
                resp = client.delete_knowledge_base(kb_id)
                if resp.status_code == 204:
                    _clear_api_cache()
                    st.session_state.kb_id = None
                    st.query_params.pop("kb", None)
                    st.success("已删除")
                    st.rerun()
                else:
                    st.error(resp.json().get("message", resp.text))


# 轮询任务。
def _poll_job(client: CogDocClient, job_id: str) -> None:
    # 轮询入库任务直到终态，期间实时显示进度。
    with st.status("后台入库中…", expanded=True) as status:
        job = {}
        for _ in range(300):
            resp = client.get_job(job_id)
            if resp.status_code != 200:
                # 任务端点出错时响应没有状态字段，直接报错退出。
                status.update(
                    label=f"查询入库任务失败：{resp.text[:200]}", state="error"
                )
                return
            job = resp.json()
            if job.get("status") in ("succeeded", "failed"):
                break
            time.sleep(0.2)
        if job.get("status") == "succeeded":
            status.update(
                label=f"入库完成：{job.get('document_count')} 篇 / {job.get('chunk_count')} chunks",
                state="complete",
            )
        else:
            status.update(
                label=f"入库失败：{job.get('message', '') or '超时未完成'}",
                state="error",
            )


# 流式处理对话后台线程。
def _stream_chat_worker(
    *,
    api_url: str,
    auth_token: str,
    workspace_id: str | None,
    kb_id: str,
    session_id: str,
    prompt: str,
    mode: str,
    is_local: bool,
    stop_event: threading.Event,
    outbox: queue.Queue,
) -> None:
    # 后台线程只碰队列和停止事件，不直接写界面状态。
    try:
        client = CogDocClient(
            api_url,
            api_key=auth_token,
            workspace_id=workspace_id,
        )
        for event, data in client.stream_chat(
            kb_id,
            prompt,
            mode=mode,
            session_id=session_id,
            is_local=is_local,
            on_response=lambda response: outbox.put(
                ("response", {"response": response})
            ),
        ):
            if stop_event.is_set():
                break
            outbox.put((event, data))
    except Exception as exc:
        if not stop_event.is_set():
            outbox.put(("error", {"message": str(exc)}))
    finally:
        outbox.put(("done", {"cancelled": stop_event.is_set()}))


# 处理开始流式请求。
def _start_stream(kb_id: str, prompt: str, mode: str) -> None:
    key = _context_key(kb_id)
    pending = st.session_state.pending_streams.get(key)
    if pending and not pending.get("done"):
        return

    user_msg_id = _next_id()
    _messages_for(kb_id).append({"role": "user", "content": prompt, "id": user_msg_id})

    outbox: queue.Queue = queue.Queue()
    stop_event = threading.Event()
    pending = {
        "kb_id": kb_id,
        "session_id": st.session_state.session_id,
        "prompt": prompt,
        "mode": mode,
        "is_local": st.session_state.is_local,
        "user_msg_id": user_msg_id,
        "answer": "",
        "final": None,
        "error": None,
        "stage": "",
        "done": False,
        "cancelled": False,
        "queue": outbox,
        "stop_event": stop_event,
    }
    worker = threading.Thread(
        target=_stream_chat_worker,
        kwargs={
            "api_url": st.session_state.api_url,
            "auth_token": _current_api_credential(),
            "workspace_id": _current_workspace_id(),
            "kb_id": kb_id,
            "session_id": st.session_state.session_id,
            "prompt": prompt,
            "mode": mode,
            "is_local": st.session_state.is_local,
            "stop_event": stop_event,
            "outbox": outbox,
        },
        daemon=True,
    )
    pending["thread"] = worker
    st.session_state.pending_streams[key] = pending
    worker.start()


# 移除消息。
def _remove_message(kb_id: str, session_id: str, msg_id: int) -> None:
    messages = _messages_for(kb_id, session_id)
    st.session_state.messages_by_context[_context_key(kb_id, session_id)] = [
        msg for msg in messages if msg.get("id") != msg_id
    ]


# 处理取消流式请求。
def _cancel_stream(key: tuple[str, str]) -> None:
    pending = st.session_state.pending_streams.get(key)
    if not pending:
        return
    pending["cancelled"] = True
    pending["done"] = True
    pending["stop_event"].set()
    response = pending.get("response")
    if response is not None:
        response.close()
    _remove_message(
        pending["kb_id"],
        pending["session_id"],
        pending["user_msg_id"],
    )


# 处理完成流式请求。
def _finish_stream(key: tuple[str, str], pending: dict) -> None:
    if pending.get("cancelled"):
        _remove_message(
            pending["kb_id"],
            pending["session_id"],
            pending["user_msg_id"],
        )
        st.session_state.pending_streams.pop(key, None)
        return

    error = pending.get("error")
    if error:
        _messages_for(pending["kb_id"], pending["session_id"]).append(
            {
                "role": "assistant",
                "content": f"[{error.get('error_code', 'ERROR')}] {error.get('message', '')}",
                "id": _next_id(),
            }
        )
        st.session_state.pending_streams.pop(key, None)
        return

    final = pending.get("final")
    answer = (final or {}).get("answer") or pending.get("answer", "")
    trace_id = (final or {}).get("trace_id")
    if trace_id and pending.get("prompt"):
        st.session_state.trace_labels[trace_id] = pending["prompt"]
    _messages_for(pending["kb_id"], pending["session_id"]).append(
        {
            "role": "assistant",
            "content": answer or "（无答案）",
            "final": final,
            "query": pending["prompt"],
            "id": _next_id(),
        }
    )
    st.session_state.pending_streams.pop(key, None)


# 处理流式事件消费。
def _drain_stream_events() -> None:
    for key, pending in list(st.session_state.pending_streams.items()):
        outbox = pending["queue"]
        while True:
            try:
                event, data = outbox.get_nowait()
            except queue.Empty:
                break
            if pending.get("cancelled"):
                continue
            if event == "token":
                pending["answer"] += data.get("content", "")
            elif event == "start":
                pending["stage"] = "正在启动请求…"
            elif event == "node":
                stage = data.get("stage", "")
                pending["stage"] = f"正在处理：{stage}" if stage else ""
            elif event == "final":
                pending["final"] = data
            elif event == "error":
                pending["error"] = data
            elif event == "response":
                response = data.get("response")
                pending["response"] = response
                if response is not None:
                    _observe_authenticated_response(response.status_code)
            elif event == "done":
                pending["cancelled"] = bool(data.get("cancelled"))
                pending["done"] = True
        if pending.get("done"):
            _finish_stream(key, pending)


def _research_area(kb_id: str | None) -> None:
    st.subheader("研究计划")
    if not kb_id:
        st.info("请先选择知识库。")
        return

    client = _client()
    notice = st.session_state.pop("research_notice", None)
    if isinstance(notice, Mapping):
        if notice.get("kind") == "success":
            st.success(str(notice.get("message") or "操作成功"))
        else:
            st.error(str(notice.get("message") or "操作失败"))

    st.caption(
        "先确定研究目标和可验证问题，再检索候选证据、执行闭集校验并生成带引用报告。"
    )
    if st.button("刷新研究进度", key=f"research-refresh-{kb_id}"):
        _clear_research_summary_cache(client, kb_id)
        st.rerun()
    with st.form(f"research-create-{kb_id}", clear_on_submit=True):
        title = st.text_input("任务标题（可选）", max_chars=160)
        objective = st.text_area(
            "研究目标",
            height=110,
            max_chars=4000,
            placeholder="例如：比较三份赛事规程，形成带证据的参赛选择建议",
        )
        raw_titles = st.text_area(
            "自定义章节（可选，每行一个）",
            height=90,
            placeholder="参赛门槛\n时间成本\n评分规则\n结论与建议",
        )
        create_submitted = st.form_submit_button(
            "创建研究计划", type="primary", use_container_width=True
        )
    if create_submitted:
        if not objective.strip():
            st.warning("请输入研究目标。")
        else:
            section_titles = [
                line.strip() for line in raw_titles.splitlines() if line.strip()
            ]
            response = client.create_research_job(
                kb_id,
                objective.strip(),
                title=title.strip(),
                section_titles=section_titles,
                is_local=bool(st.session_state.get("is_local", False)),
            )
            if response.status_code == 201:
                created_payload = response_payload(response)
                created_job = (
                    created_payload.get("job")
                    if isinstance(created_payload, Mapping)
                    else None
                )
                if isinstance(created_job, Mapping) and created_job.get("job_id"):
                    st.session_state.research_open_job_by_kb[kb_id] = str(
                        created_job["job_id"]
                    )
                _clear_research_summary_cache(client, kb_id)
                st.session_state.research_notice = {
                    "kind": "success",
                    "message": "研究计划已创建。",
                }
                st.rerun()
            else:
                st.error(_response_error(response, "创建研究计划失败"))

    auth_identity = getattr(client, "auth_cache_identity", "anonymous")
    page_state_key = (auth_identity, kb_id)
    page_state = st.session_state.research_summary_pages.setdefault(
        page_state_key, {"cursor": None, "history": []}
    )
    if not isinstance(page_state, dict):
        page_state = {"cursor": None, "history": []}
        st.session_state.research_summary_pages[page_state_key] = page_state
    cursor = page_state.get("cursor")
    if cursor is not None and type(cursor) is not str:
        cursor = None
        page_state["cursor"] = None
    history = page_state.get("history")
    if not isinstance(history, list):
        history = []
        page_state["history"] = history
    cache_key = _research_summary_cache_key(
        client.base_url,
        kb_id,
        cursor,
        auth_identity=auth_identity,
    )
    cache_entry = st.session_state.research_summary_cache.get(cache_key)
    cached_payload = (
        cache_entry.get("payload") if isinstance(cache_entry, Mapping) else None
    )
    cached_etag = cache_entry.get("etag") if isinstance(cache_entry, Mapping) else None
    try:
        response = client.list_research_job_summaries(
            kb_id,
            limit=RESEARCH_SUMMARY_PAGE_SIZE,
            cursor=cursor,
            if_none_match=(cached_etag if type(cached_etag) is str else None),
        )
        payload = _research_summary_response_payload(response, cached_payload)
    except Exception as exc:
        st.error(f"读取研究案卷索引失败：{exc}")
        return
    if response.status_code == 200:
        st.session_state.research_summary_cache[cache_key] = {
            "etag": str(response.headers.get("ETag") or ""),
            "payload": dict(payload),
        }
    summaries = [item for item in payload.get("jobs", []) if isinstance(item, Mapping)]
    selected_job_id = str(st.session_state.research_open_job_by_kb.get(kb_id) or "")

    st.markdown("#### 案卷索引")
    st.caption("索引只显示进度与审阅信号；打开一份案卷后才读取计划、证据和报告。")
    if not summaries:
        st.info("当前页没有研究案卷。" if cursor else "暂无研究计划。")
    for summary in summaries:
        summary_job_id = str(summary.get("job_id") or "")
        title_label = str(
            summary.get("title") or summary.get("objective_preview") or summary_job_id
        )
        summary_status = str(summary.get("status") or "")
        revision = int(summary.get("revision") or 1)
        index_columns = st.columns([5.2, 2.2, 1.4], vertical_alignment="center")
        index_columns[0].markdown(f"**{title_label}**")
        index_columns[0].caption(
            str(summary.get("objective_preview") or "（未提供研究目标摘要）")
        )
        status_bits = [
            _research_summary_status_label(summary_status),
            _research_summary_progress_label(summary),
            f"r{revision}",
        ]
        if summary.get("provenance_status") == "stale":
            status_bits.append("证据已过期")
        elif summary.get("review_status") == "published":
            status_bits.append("已发布")
        index_columns[1].caption(" · ".join(status_bits))
        is_open = selected_job_id == summary_job_id
        if index_columns[2].button(
            "当前工作区" if is_open else "打开工作区",
            key=f"research-open-{kb_id}-{summary_job_id}-{revision}",
            type="primary" if is_open else "secondary",
            disabled=is_open,
            use_container_width=True,
        ):
            st.session_state.research_open_job_by_kb[kb_id] = summary_job_id
            st.rerun()

    previous_column, page_label_column, next_column = st.columns([1, 3, 1])
    if previous_column.button(
        "上一页",
        key=f"research-summary-previous-{kb_id}-{cursor or 'first'}",
        disabled=not history,
        use_container_width=True,
    ):
        page_state["cursor"] = history.pop() if history else None
        st.rerun()
    page_label_column.caption(
        f"第 {len(history) + 1} 页 · 本页 {len(summaries)} 份案卷"
    )
    next_cursor = payload.get("next_cursor")
    has_more = bool(payload.get("has_more")) and type(next_cursor) is str
    if next_column.button(
        "下一页",
        key=f"research-summary-next-{kb_id}-{cursor or 'first'}",
        disabled=not has_more,
        use_container_width=True,
    ):
        history.append(cursor)
        page_state["cursor"] = next_cursor
        st.rerun()

    selected_job_id = str(st.session_state.research_open_job_by_kb.get(kb_id) or "")
    if not selected_job_id:
        return
    st.divider()
    workspace_header, workspace_close = st.columns([5, 1], vertical_alignment="center")
    workspace_header.markdown("#### 打开的研究工作区")
    if workspace_close.button(
        "收起",
        key=f"research-workspace-close-{kb_id}-{selected_job_id}",
        use_container_width=True,
    ):
        st.session_state.research_open_job_by_kb.pop(kb_id, None)
        st.rerun()
    try:
        detail_response = client.get_research_job(selected_job_id)
    except Exception as exc:
        st.error(f"读取研究工作区失败：{exc}")
        return
    if detail_response.status_code != 200:
        st.error(_response_error(detail_response, "读取研究工作区失败"))
        return
    detail_payload = response_payload(detail_response)
    detail_job = (
        detail_payload.get("job") if isinstance(detail_payload, Mapping) else None
    )
    if not isinstance(detail_job, Mapping):
        st.error("研究工作区响应格式不符合预期")
        return
    jobs = [detail_job]

    for job in jobs:
        if not isinstance(job, Mapping):
            continue
        job_id = str(job.get("job_id") or "")
        title_label = str(job.get("title") or job.get("objective") or job_id)
        status = str(job.get("status") or "planned")
        report_status = str(job.get("report_status") or "not_started")
        provenance_status = str(job.get("provenance_status") or "untracked")
        provenance_stale = provenance_status == "stale"
        revision = int(job.get("revision") or 1)
        with st.expander(f"{title_label} · {status} · r{revision}", expanded=True):
            st.write(str(job.get("objective") or ""))
            st.caption(
                f"任务 ID：`{job_id}` · 最后更新：{job.get('updated_at') or '-'}"
            )
            st.caption(
                "执行后端：" + ("本地 Ollama" if job.get("is_local") else "云端模型")
            )
            if provenance_stale:
                reasons = [
                    str(reason)
                    for reason in job.get("provenance_stale_reasons") or []
                    if str(reason)
                ]
                st.warning(
                    "知识库索引、来源文件或审核知识已变化；旧证据不能继续生成、"
                    "审阅或发布。"
                    + (f"\n\n变更：{'；'.join(reasons)}" if reasons else "")
                )
            elif provenance_status == "current":
                snapshot = job.get("evidence_provenance") or {}
                st.caption(
                    "证据版本已锁定"
                    f" · 索引代：{snapshot.get('index_generation') or '-'}"
                )
            action_columns = st.columns(3)
            requested_action = None
            if provenance_stale and str(job.get("review_status") or "") != "published":
                if action_columns[0].button(
                    "按当前索引重新取证",
                    key=f"research-refresh-evidence-{job_id}-{revision}",
                ):
                    requested_action = "refresh"
            elif status == "evidence_ready" and action_columns[0].button(
                "校验并生成报告", key=f"research-generate-{job_id}-{revision}"
            ):
                requested_action = "generate"
            elif (
                status == "completed"
                and str(job.get("review_status") or "") == "changes_requested"
                and action_columns[0].button(
                    "仅重新校验生成退回章节",
                    key=f"research-review-regenerate-{job_id}-{revision}",
                )
            ):
                requested_action = "generate"
            elif (
                status == "failed"
                and report_status == "failed"
                and action_columns[0].button(
                    "重试报告生成", key=f"research-regenerate-{job_id}-{revision}"
                )
            ):
                requested_action = "generate"
            elif status in {"planned", "failed"} and action_columns[0].button(
                "开始证据检索", key=f"research-start-{job_id}-{revision}"
            ):
                requested_action = "start"
            elif status == "paused" and action_columns[0].button(
                "恢复检索", key=f"research-resume-{job_id}-{revision}"
            ):
                requested_action = "resume"
            elif status == "running" and action_columns[0].button(
                "暂停", key=f"research-pause-{job_id}-{revision}"
            ):
                requested_action = "pause"
            elif status == "generating":
                action_columns[0].info("正在校验并生成报告…")
            if status in {"planned", "running", "paused", "failed"}:
                if action_columns[1].button(
                    "取消任务", key=f"research-cancel-{job_id}-{revision}"
                ):
                    requested_action = "cancel"
            plan_locked = (
                status in {"running", "generating"}
                or str(job.get("review_status") or "") == "published"
            )
            if not plan_locked and action_columns[2].button(
                "AI 规划大纲",
                key=f"research-auto-plan-{job_id}-{revision}",
                help="根据研究目标与当前知识库来源生成可编辑的原子取证计划。",
            ):
                plan_response = client.generate_research_plan(
                    job_id,
                    expected_revision=revision,
                    is_local=bool(job.get("is_local", False)),
                )
                if plan_response.status_code == 200:
                    st.session_state.research_notice = {
                        "kind": "success",
                        "message": "智能大纲已生成，请审阅后再开始检索。",
                    }
                    st.rerun()
                st.error(_response_error(plan_response, "生成智能大纲失败"))
            if requested_action:
                action_response = client.research_action(job_id, requested_action)
                if action_response.status_code in {200, 202}:
                    st.session_state.research_notice = {
                        "kind": "success",
                        "message": "研究任务状态已更新。",
                    }
                    st.rerun()
                st.error(_response_error(action_response, "更新研究任务状态失败"))
            sections = [
                section
                for section in (job.get("sections") or [])
                if isinstance(section, Mapping)
            ]
            with st.form(f"research-plan-{job_id}-r{revision}"):
                edited_sections = []
                for position, section in enumerate(sections, start=1):
                    st.markdown(f"**章节 {position}**")
                    edited_title = st.text_input(
                        "标题",
                        value=str(section.get("title") or ""),
                        key=f"research-title-{job_id}-{revision}-{position}",
                        disabled=plan_locked,
                    )
                    edited_question = st.text_area(
                        "可验证研究问题",
                        value=str(section.get("research_question") or ""),
                        height=80,
                        key=f"research-question-{job_id}-{revision}-{position}",
                        disabled=plan_locked,
                    )
                    original_requirements = [
                        requirement
                        for requirement in section.get("evidence_requirements") or []
                        if isinstance(requirement, Mapping)
                    ]
                    (
                        requirement_editor_value,
                        retrieval_editor_value,
                        recovery_editor_value,
                    ) = _research_requirement_editor_lines(original_requirements)
                    edited_requirement_text = st.text_area(
                        "原子证据需求（每行一个，最多 3 项）",
                        value=requirement_editor_value,
                        height=90,
                        key=f"research-requirements-{job_id}-{revision}-{position}",
                        disabled=plan_locked,
                    )
                    edited_retrieval_text = st.text_area(
                        "主检索表达（与需求逐行对应）",
                        value=retrieval_editor_value,
                        height=90,
                        key=(
                            f"research-retrieval-queries-{job_id}-{revision}-{position}"
                        ),
                        disabled=plan_locked,
                    )
                    edited_recovery_text = st.text_area(
                        "恢复检索表达（语义一致，但措辞必须不同）",
                        value=recovery_editor_value,
                        height=90,
                        key=(
                            f"research-recovery-queries-{job_id}-{revision}-{position}"
                        ),
                        disabled=plan_locked,
                        help="当需求改变时，未手动改写的两条查询会自动重建；恢复查询不会与主查询相同。",
                    )
                    edited_requirements = _build_edited_research_requirements(
                        edited_requirement_text,
                        edited_retrieval_text,
                        edited_recovery_text,
                        original_requirements,
                    )
                    edited_success_criteria = st.text_area(
                        "完成标准",
                        value=str(section.get("success_criteria") or ""),
                        height=70,
                        key=f"research-success-{job_id}-{revision}-{position}",
                        disabled=plan_locked,
                    )
                    edited_sections.append(
                        {
                            "title": edited_title.strip(),
                            "research_question": edited_question.strip(),
                            "evidence_requirements": edited_requirements,
                            "success_criteria": edited_success_criteria.strip(),
                        }
                    )
                    st.caption(
                        f"证据状态：{section.get('evidence_status') or 'unsearched'}"
                    )
                    if section.get("verification_status"):
                        st.caption(
                            "闭集校验："
                            f"{section.get('verification_status')}"
                            f" · 生成：{section.get('generation_status') or '-'}"
                        )
                    claim_audit = section.get("claim_audit")
                    if isinstance(claim_audit, Mapping) and claim_audit.get("status"):
                        repair = claim_audit.get("repair") or {}
                        st.caption(
                            "声明审计："
                            f"{claim_audit.get('status')}"
                            + (
                                " · 已执行一次有界修复"
                                if isinstance(repair, Mapping)
                                and repair.get("attempted")
                                else ""
                            )
                        )
                    coverage_audit = section.get("coverage_audit")
                    if isinstance(coverage_audit, Mapping) and coverage_audit.get(
                        "status"
                    ):
                        missing_ids = coverage_audit.get("missing_requirement_ids")
                        missing_label = (
                            " · 缺失：" + ", ".join(str(item) for item in missing_ids)
                            if isinstance(missing_ids, list) and missing_ids
                            else ""
                        )
                        st.caption(
                            "原子需求覆盖："
                            f"{coverage_audit.get('status')}"
                            f" · {coverage_audit.get('covered_count', 0)}/"
                            f"{coverage_audit.get('requirement_count', 0)}"
                            f"{missing_label}"
                        )
                    if section.get("content"):
                        st.markdown(str(section.get("content") or ""))
                    for evidence in section.get("evidence") or []:
                        if not isinstance(evidence, Mapping):
                            continue
                        page = _page_range_label(
                            evidence.get("page_start", evidence.get("page")),
                            evidence.get("page_end"),
                        )
                        location = " · ".join(
                            part
                            for part in [str(evidence.get("source") or ""), page]
                            if part
                        )
                        st.caption(
                            f"证据候选 · {location or evidence.get('chunk_id') or '-'}"
                        )
                        st.write(str(evidence.get("text_preview") or ""))
                update_submitted = st.form_submit_button(
                    "保存计划修订",
                    use_container_width=True,
                    disabled=plan_locked,
                )
            if update_submitted:
                if any(
                    not item["title"] or not item["research_question"]
                    for item in edited_sections
                ):
                    st.warning("章节标题和研究问题都不能为空。")
                    continue
                if any(not item["evidence_requirements"] for item in edited_sections):
                    st.warning("每个章节至少需要一条原子证据需求。")
                    continue
                update_response = client.update_research_plan(
                    job_id,
                    expected_revision=revision,
                    sections=edited_sections,
                )
                if update_response.status_code == 200:
                    st.session_state.research_notice = {
                        "kind": "success",
                        "message": "研究计划已更新。",
                    }
                    st.rerun()
                st.error(_response_error(update_response, "更新研究计划失败"))

            report = job.get("report")
            if isinstance(report, Mapping) and report.get("content"):
                st.divider()
                report_version = int(job.get("report_version") or 1)
                review_status = str(job.get("review_status") or "pending")
                st.markdown(f"### 研究报告 · v{report_version}")
                st.caption(
                    f"审阅状态：{review_status} · "
                    f"历史版本：{len(job.get('report_history') or [])}"
                )
                last_regenerated = [
                    str(section_id)
                    for section_id in job.get("last_regenerated_section_ids") or []
                    if str(section_id)
                ]
                if last_regenerated:
                    st.caption("本版本仅重生成章节：" + "、".join(last_regenerated))
                if report_status == "ready_with_gaps":
                    st.warning("部分章节因证据不足、冲突或生成错误被明确留空。")
                report_content = str(report.get("content") or "")
                st.download_button(
                    "下载 Markdown 报告",
                    data=report_content,
                    file_name=f"{job_id}.md",
                    mime="text/markdown",
                    key=f"research-download-{job_id}-{revision}",
                    use_container_width=True,
                )
                st.markdown(report_content)

                if (
                    review_status not in {"published", "not_started"}
                    and not provenance_stale
                ):
                    with st.form(f"research-review-{job_id}-r{revision}"):
                        st.markdown("#### 逐章审阅")
                        review_decisions = []
                        for section in sections:
                            section_id = str(section.get("section_id") or "")
                            section_title = str(section.get("title") or section_id)
                            generated = section.get("generation_status") == "generated"
                            options = (
                                ["pending", "approved", "changes_requested"]
                                if generated
                                else ["pending", "accepted_gap", "changes_requested"]
                            )
                            current_review = str(
                                section.get("review_status") or "pending"
                            )
                            default_index = (
                                options.index(current_review)
                                if current_review in options
                                else 0
                            )
                            decision = st.selectbox(
                                f"{section_title} · 审阅决定",
                                options,
                                index=default_index,
                                format_func=lambda value: {
                                    "pending": "暂不处理",
                                    "approved": "批准正文",
                                    "accepted_gap": "接受证据缺口",
                                    "changes_requested": "退回修订",
                                }[value],
                                key=(
                                    f"research-review-decision-{job_id}-"
                                    f"{revision}-{section_id}"
                                ),
                            )
                            note = st.text_area(
                                f"{section_title} · 审阅意见",
                                value=str(section.get("review_note") or ""),
                                height=70,
                                max_chars=2000,
                                key=(
                                    f"research-review-note-{job_id}-"
                                    f"{revision}-{section_id}"
                                ),
                                help=(
                                    "退回修订或接受证据缺口时必填；退回意见会进入"
                                    "下一轮检索和生成。"
                                ),
                            )
                            if decision != "pending":
                                review_decisions.append(
                                    {
                                        "section_id": section_id,
                                        "decision": decision,
                                        "note": note.strip(),
                                    }
                                )
                        review_submitted = st.form_submit_button(
                            "保存审阅决定",
                            use_container_width=True,
                        )
                    if review_submitted:
                        if not review_decisions:
                            st.warning("请至少选择一个审阅决定。")
                        elif any(
                            item["decision"] in {"changes_requested", "accepted_gap"}
                            and not item["note"]
                            for item in review_decisions
                        ):
                            st.warning("退回修订或接受证据缺口必须填写审阅意见。")
                        else:
                            review_response = client.review_research_report(
                                job_id,
                                expected_revision=revision,
                                decisions=review_decisions,
                            )
                            if review_response.status_code == 200:
                                st.session_state.research_notice = {
                                    "kind": "success",
                                    "message": "审阅决定已保存。",
                                }
                                st.rerun()
                            st.error(
                                _response_error(review_response, "保存审阅决定失败")
                            )

                if review_status == "approved" and not provenance_stale:
                    if st.button(
                        "发布已审阅报告",
                        type="primary",
                        key=f"research-publish-{job_id}-{revision}",
                        use_container_width=True,
                    ):
                        publish_response = client.publish_research_report(
                            job_id,
                            expected_revision=revision,
                        )
                        if publish_response.status_code == 200:
                            st.session_state.research_notice = {
                                "kind": "success",
                                "message": "研究报告已发布并冻结。",
                            }
                            st.rerun()
                        st.error(_response_error(publish_response, "发布报告失败"))
                elif review_status == "published":
                    st.success("该版本已完成审阅并发布。")
                    published_response = None
                    if st.button(
                        "准备已发布 Markdown",
                        key=f"research-published-report-{job_id}-{revision}",
                        use_container_width=True,
                        help=(
                            "始终从服务端完整性校验端点读取；旧版报告会明确"
                            "标记为未验证。"
                        ),
                    ):
                        published_response = client.get_published_research_report(
                            job_id
                        )
                    if published_response is not None:
                        if published_response.status_code == 200:
                            integrity = published_response.headers.get(
                                "X-CogDoc-Integrity", "legacy-unverified"
                            )
                            if integrity == "legacy-unverified":
                                st.warning(
                                    "这是升级前的旧版 Markdown，可下载查阅，但没有"
                                    "完整审计承诺，不能作为可验证交付包。"
                                )
                            else:
                                st.caption("完整性状态：verified")
                            st.download_button(
                                "下载已发布报告",
                                data=published_response.content,
                                file_name=f"{job_id}-published.md",
                                mime="text/markdown",
                                key=(
                                    f"research-published-download-{job_id}-{revision}"
                                ),
                                use_container_width=True,
                            )
                        else:
                            st.error(
                                _response_error(
                                    published_response, "读取已发布报告失败"
                                )
                            )
                    bundle_response = None
                    if st.button(
                        "准备可验证交付包",
                        key=f"research-published-bundle-{job_id}-{revision}",
                        use_container_width=True,
                        help=(
                            "生成包含 Markdown、引用账本、证据版本快照、"
                            "审计承诺和完整性清单的 ZIP。"
                        ),
                    ):
                        bundle_response = client.get_published_research_bundle(job_id)
                    if bundle_response is not None:
                        if bundle_response.status_code == 200:
                            st.download_button(
                                "下载 ZIP 交付包",
                                data=bundle_response.content,
                                file_name=f"{job_id}-published-bundle.zip",
                                mime="application/zip",
                                key=(
                                    f"research-published-bundle-download-"
                                    f"{job_id}-{revision}"
                                ),
                                use_container_width=True,
                            )
                        else:
                            st.error(
                                _response_error(
                                    bundle_response,
                                    "生成可验证交付包失败",
                                )
                            )


def _eval_review_client() -> CogDocClient:
    review_key = str(st.session_state.get("eval_review_key") or "").strip()
    return CogDocClient(
        str(st.session_state.api_url),
        api_key=review_key or _current_api_credential(),
        workspace_id=_current_workspace_id(),
    )


def _eval_response_detail(response, fallback: str) -> str:
    payload = response_payload(response)
    if isinstance(payload, Mapping):
        detail = payload.get("detail")
        if isinstance(detail, Mapping):
            message = str(detail.get("message") or fallback)
            reasons = detail.get("reasons")
            if isinstance(reasons, list) and reasons:
                return f"{message}：{'、'.join(str(item) for item in reasons)}"
            return message
        if detail:
            return str(detail)
    return _response_error(response, fallback)


def _eval_candidate_identity(candidate: Mapping, *, include_span: bool) -> dict:
    identity = {
        "chunk_id": str(candidate.get("chunk_id") or ""),
        "source": str(candidate.get("source") or ""),
        "source_sha256": str(candidate.get("source_sha256") or ""),
    }
    parent_chunk_id = str(candidate.get("parent_chunk_id") or "")
    if parent_chunk_id:
        identity["parent_chunk_id"] = parent_chunk_id
    if include_span:
        identity["start"] = int(candidate["_selected_start"])
        identity["end"] = int(candidate["_selected_end"])
    return identity


def _eval_paper(candidate: Mapping) -> None:
    source = html.escape(str(candidate.get("source") or "未知来源"))
    section = html.escape(str(candidate.get("section_title") or ""))
    page = _page_range_label(
        candidate.get("page_start", candidate.get("page")),
        candidate.get("page_end"),
    )
    text = html.escape(str(candidate.get("text") or "（空文本）"))
    chunk_id = html.escape(str(candidate.get("chunk_id") or "缺少 chunk_id"))
    meta = " · ".join(item for item in (source, page, section) if item)
    st.markdown(
        f"""
        <article class="evidence-paper">
          <div class="evidence-rail">#{int(candidate.get("rank") or 0):02d}</div>
          <div class="evidence-sheet">
            <div class="evidence-meta">{meta}</div>
            <div class="evidence-text">{text}</div>
            <div class="evidence-id">{chunk_id}</div>
          </div>
        </article>
        """,
        unsafe_allow_html=True,
    )


def _diagnostic_identity(hit: Mapping) -> dict:
    return {
        key: str(hit.get(key) or "")
        for key in ("chunk_id", "source", "source_sha256", "parent_chunk_id")
    }


def _render_retrieval_diagnostic_console(
    client: CogDocClient, kb_id: str | None
) -> None:
    with st.expander("打开检索路径诊断", expanded=False):
        if not kb_id:
            st.info("先选择一个知识库，再运行路径诊断。")
            return
        st.markdown(
            "<div class='signal-path'><b>QUERY</b><span>→</span>"
            "<i>VECTOR</i><i>BM25</i><i>KNOWLEDGE·V</i><i>KNOWLEDGE·L</i>"
            "<span>→</span><b>RRF</b><span>→</span><b>RERANK</b>"
            "<span>→</span><b>GATE</b></div>",
            unsafe_allow_html=True,
        )
        with st.form(f"retrieval-diagnostic-run-{kb_id}"):
            query = st.text_area(
                "要检查的问题",
                placeholder="例如：文档如何定义长期记忆，它与短期记忆有什么区别？",
                height=86,
            )
            controls = st.columns([1, 1, 3])
            top_k = controls[0].number_input("每路候选", 1, 50, 12)
            rerank = controls[1].toggle("运行重排", value=True)
            requirement_label = controls[2].text_input(
                "原子需求（可选）", placeholder="留空时使用完整问题"
            )
            with st.expander("临时路权重"):
                weight_columns = st.columns(4)
                weights = {
                    "rag_vector": weight_columns[0].number_input(
                        "原文向量", 0.0, 5.0, 1.0, 0.1
                    ),
                    "rag_bm25": weight_columns[1].number_input(
                        "原文词法", 0.0, 5.0, 1.0, 0.1
                    ),
                    "derived_knowledge_vector": weight_columns[2].number_input(
                        "派生向量", 0.0, 5.0, 0.9, 0.1
                    ),
                    "derived_knowledge_lexical": weight_columns[3].number_input(
                        "派生词法", 0.0, 5.0, 0.8, 0.1
                    ),
                }
            run = st.form_submit_button(
                "追踪这次检索", type="primary", use_container_width=True
            )
        state_key = f"retrieval-diagnostic-result-{kb_id}"
        if run:
            if not query.strip():
                st.warning("请输入要检查的问题。")
            else:
                requirements = (
                    [
                        {
                            "requirement_id": "r1",
                            "question": requirement_label,
                            "retrieval_query": query,
                            "recovery_query": query,
                        }
                    ]
                    if requirement_label.strip()
                    else []
                )
                with st.spinner("正在追踪四路召回和融合贡献…"):
                    response = client.diagnose_retrieval(
                        kb_id,
                        query,
                        top_k=int(top_k),
                        rerank=rerank,
                        route_weights=weights,
                        requirements=requirements,
                    )
                if response.status_code != 200:
                    st.error(_eval_response_detail(response, "检索诊断失败"))
                else:
                    st.session_state[state_key] = response_payload(response)

        result = st.session_state.get(state_key)
        if not isinstance(result, Mapping):
            st.caption("运行后会保留本次路径快照；切换页面前可将人工判断存入评测草稿。")
            return
        decision = result.get("decision") or {}
        latency = result.get("latency_ms") or {}
        channel_counts = result.get("channel_counts") or {}
        summary = st.columns(4)
        summary[0].metric(
            "证据门",
            "通过" if decision.get("supported") else "拒答",
            str(decision.get("reason") or "-"),
        )
        summary[1].metric("融合候选", len(result.get("fused") or []))
        summary[2].metric("最终证据", len(result.get("final") or []))
        summary[3].metric("总耗时", f"{float(latency.get('total') or 0):.1f} ms")
        st.caption(
            " · ".join(f"{name} {count}" for name, count in channel_counts.items())
        )
        missing = decision.get("missing_requirement_ids") or []
        if missing:
            st.warning(f"未覆盖需求：{'、'.join(str(item) for item in missing)}")

        routes = result.get("routes") or []
        route_names = []
        grouped = {}
        for route in routes:
            name = str(route.get("channel") or "unknown")
            if name not in grouped:
                route_names.append(name)
                grouped[name] = []
            grouped[name].extend(route.get("hits") or [])
        if route_names:
            route_tabs = st.tabs(route_names)
            for tab, name in zip(route_tabs, route_names, strict=True):
                with tab:
                    for hit in grouped[name][: int(top_k)]:
                        st.markdown(
                            f"**#{int(hit.get('rank') or 0):02d} · "
                            f"{hit.get('source') or '未知来源'}**  "
                            f"`{hit.get('chunk_id') or '-'}`"
                        )
                        st.caption(str(hit.get("text_preview") or ""))

        st.markdown("#### 融合与重排位移")
        final_hits = result.get("final") or []
        for hit in final_hits:
            retrieval = hit.get("retrieval") or {}
            contributions = retrieval.get("channel_contributions") or {}
            movement = int(hit.get("rank_delta") or 0)
            movement_label = (
                f"↑{movement}"
                if movement > 0
                else f"↓{-movement}"
                if movement < 0
                else "—"
            )
            with st.expander(
                f"#{int(hit.get('rank') or 0):02d} {movement_label} · "
                f"{hit.get('source') or '未知来源'}",
                expanded=int(hit.get("rank") or 0) <= 3,
            ):
                st.caption(str(hit.get("text_preview") or ""))
                st.code(
                    json.dumps(contributions, ensure_ascii=False, indent=2),
                    language="json",
                )
                st.selectbox(
                    "人工判断",
                    ["skip", "gold", "negative"],
                    format_func={
                        "skip": "暂不标注",
                        "gold": "正确证据",
                        "negative": "误导项",
                    }.get,
                    key=(
                        f"diagnostic-label-{kb_id}-"
                        f"{hit.get('chunk_id') or hit.get('rank')}"
                    ),
                )

        no_answer = st.toggle(
            "这个问题在当前知识库中应当拒答",
            value=False,
            key=f"diagnostic-no-answer-{kb_id}",
        )
        if st.button(
            "保存为待审核评测题",
            key=f"diagnostic-save-{kb_id}",
            use_container_width=True,
        ):
            acceptable = []
            negatives = []
            for hit in final_hits:
                choice = st.session_state.get(
                    f"diagnostic-label-{kb_id}-"
                    f"{hit.get('chunk_id') or hit.get('rank')}",
                    "skip",
                )
                if choice == "gold":
                    acceptable.append(_diagnostic_identity(hit))
                elif choice == "negative":
                    negatives.append(_diagnostic_identity(hit))
            if no_answer:
                acceptable = []
            response = client.save_retrieval_diagnostic_label(
                kb_id,
                str(result.get("query") or ""),
                no_answer=no_answer,
                acceptable_evidence=acceptable,
                hard_negative_evidence=negatives,
                requirement_label=requirement_label,
            )
            if response.status_code == 200:
                st.success("已进入待审核评测集；通过后才会进入正式数据集。")
            else:
                st.error(_eval_response_detail(response, "保存评测题失败"))


def _render_index_migration_console(client: CogDocClient, kb_id: str | None) -> None:
    with st.expander("索引代际控制", expanded=False):
        st.caption("先检测版本，再迁移；新代验收前会保留旧代，支持原子回切。")
        controls = st.columns(3)
        if controls[0].button(
            "检测当前知识库",
            disabled=not kb_id,
            key=f"migration-scan-{kb_id}",
            use_container_width=True,
        ):
            response = client.scan_index_migrations()
            if response.status_code == 200:
                payload = response_payload(response)
                items = payload.get("items", []) if isinstance(payload, Mapping) else []
                st.session_state[f"migration-scan-result-{kb_id}"] = next(
                    (
                        item
                        for item in items
                        if isinstance(item, Mapping) and item.get("kb_id") == kb_id
                    ),
                    None,
                )
            else:
                st.error(_eval_response_detail(response, "检测索引版本失败"))
        if controls[1].button(
            "开始迁移",
            disabled=not kb_id,
            type="primary",
            key=f"migration-start-{kb_id}",
            use_container_width=True,
        ):
            response = client.start_index_migration([str(kb_id)])
            if response.status_code == 202:
                payload = response_payload(response)
                st.session_state[f"migration-run-id-{kb_id}"] = str(
                    payload.get("run_id") or ""
                )
                st.success("迁移已进入后台队列。")
            else:
                st.error(_eval_response_detail(response, "启动索引迁移失败"))
        run_id = str(st.session_state.get(f"migration-run-id-{kb_id}") or "")
        if controls[2].button(
            "刷新进度",
            disabled=not run_id,
            key=f"migration-refresh-{kb_id}",
            use_container_width=True,
        ):
            response = client.get_index_migration(run_id)
            if response.status_code == 200:
                st.session_state[f"migration-run-result-{kb_id}"] = response_payload(
                    response
                )
            else:
                st.error(_eval_response_detail(response, "读取迁移进度失败"))

        scan = st.session_state.get(f"migration-scan-result-{kb_id}")
        if isinstance(scan, Mapping):
            state = "需要迁移" if scan.get("needs_migration") else "已是当前版本"
            st.markdown(
                f"<div class='generation-track'><b>{html.escape(str(kb_id))}</b>"
                f"<span>{html.escape(str(scan.get('active_generation_id') or '无活跃代'))}</span>"
                f"<span>→ {html.escape(str(scan.get('target_chunk_identity_version') or '-'))}</span>"
                f"<strong>{state}</strong></div>",
                unsafe_allow_html=True,
            )
            reasons = scan.get("reasons") or []
            if reasons:
                st.caption("原因：" + "、".join(str(reason) for reason in reasons))

        result = st.session_state.get(f"migration-run-result-{kb_id}")
        if isinstance(result, Mapping):
            status = str(result.get("status") or "unknown")
            summary = result.get("summary") or {}
            st.code(
                f"RUN {str(result.get('run_id') or '')[:12]}  STATUS {status}\n"
                + json.dumps(summary, ensure_ascii=False),
                language=None,
            )
            action_columns = st.columns(2)
            if action_columns[0].button(
                "回滚到旧代",
                disabled=status not in {"completed", "completed_with_failures"},
                key=f"migration-rollback-{kb_id}",
                use_container_width=True,
            ):
                response = client.rollback_index_migration(str(result["run_id"]))
                if response.status_code == 200:
                    st.success("已回切到迁移前索引代。")
                else:
                    st.error(_eval_response_detail(response, "索引回滚失败"))
            if action_columns[1].button(
                "验收并清理旧代",
                disabled=status != "completed",
                key=f"migration-finalize-{kb_id}",
                use_container_width=True,
            ):
                response = client.finalize_index_migration(str(result["run_id"]))
                if response.status_code == 200:
                    st.success("旧代已清理，本次迁移完成验收。")
                else:
                    st.error(_eval_response_detail(response, "清理旧代失败"))


def _render_eval_unit(
    draft: Mapping, unit: Mapping, candidates: Sequence[Mapping]
) -> dict:
    draft_id = str(draft.get("draft_id") or "")
    revision = int(draft.get("revision") or 1)
    unit_id = str(unit.get("unit_id") or "unit")
    widget_prefix = f"eval-{draft_id}-{revision}-{unit_id}"
    st.markdown(
        f"<div class='requirement-kicker'>判题单 · {html.escape(unit_id)}</div>",
        unsafe_allow_html=True,
    )
    st.markdown(f"#### {str(unit.get('label') or '未命名需求')}")
    st.caption("需求原意不可在审核中改写；若题目本身错误，请驳回并重新生成草稿。")
    expected_status = st.radio(
        "证据预期",
        ["supported", "no_evidence"],
        index=(
            1
            if str(unit.get("expected_status") or "") == "no_evidence"
            or (unit.get("expected_status") is None and draft.get("no_answer"))
            else 0
        ),
        horizontal=True,
        format_func=lambda value: "应有证据" if value == "supported" else "应无证据",
        key=f"{widget_prefix}-status",
    )
    query_columns = st.columns(2)
    retrieval_query = query_columns[0].text_input(
        "首轮检索词",
        value=str(unit.get("retrieval_query") or ""),
        key=f"{widget_prefix}-retrieval",
    )
    recovery_query = query_columns[1].text_input(
        "补救检索词",
        value=str(unit.get("recovery_query") or ""),
        key=f"{widget_prefix}-recovery",
    )

    acceptable: list[dict] = []
    negatives: list[dict] = []
    unit_errors: list[str] = []
    selected_gold_count = 0
    existing_acceptable = {
        str(item.get("chunk_id") or ""): item
        for item in unit.get("acceptable_evidence") or []
        if isinstance(item, Mapping)
    }
    existing_negatives = {
        str(item.get("chunk_id") or "")
        for item in unit.get("hard_negative_chunks") or []
        if isinstance(item, Mapping)
    }
    if expected_status == "no_evidence":
        st.info("这道题标为“应无证据”；通过时不应选择正确证据。")
    for candidate in candidates:
        rank = int(candidate.get("rank") or 0)
        source = str(candidate.get("source") or "未知来源")
        page = _page_range_label(
            candidate.get("page_start", candidate.get("page")),
            candidate.get("page_end"),
        )
        matched = candidate.get("matched_requirement_ids") or []
        relevant = not matched or unit_id in matched
        chunk_id = str(candidate.get("chunk_id") or "")
        existing_target = existing_acceptable.get(chunk_id)
        default_choice = (
            "gold"
            if existing_target is not None
            else "negative"
            if chunk_id in existing_negatives
            else "skip"
        )
        with st.expander(
            f"#{rank:02d} · {source} · {page}{'' if relevant else ' · 其他需求召回'}",
            expanded=rank <= 2 and relevant,
        ):
            _eval_paper(candidate)
            choice = st.radio(
                "这段原文对当前需求是什么？",
                ["skip", "gold", "negative"],
                index=["skip", "gold", "negative"].index(default_choice),
                horizontal=True,
                format_func={
                    "skip": "不标注",
                    "gold": "正确证据",
                    "negative": "误导项",
                }.get,
                key=f"{widget_prefix}-candidate-{rank}",
            )
            if choice == "gold":
                selected_gold_count += 1
                if expected_status == "no_evidence":
                    st.warning("“应无证据”不能同时选择正确证据。")
                quote = st.text_input(
                    "关键句（可选，复制上方连续原文后会自动定位字符区间）",
                    value=(
                        str(candidate.get("text") or "")[
                            int(existing_target["start"]) : int(existing_target["end"])
                        ]
                        if isinstance(existing_target, Mapping)
                        and isinstance(existing_target.get("start"), int)
                        and isinstance(existing_target.get("end"), int)
                        else ""
                    ),
                    key=f"{widget_prefix}-quote-{rank}",
                )
                selected = dict(candidate)
                if quote:
                    start = str(candidate.get("text") or "").find(quote)
                    if start < 0:
                        st.error("关键句不在这段原文中；请保持原文字符完全一致。")
                        unit_errors.append(f"{unit_id}：关键句不在候选原文中")
                    else:
                        selected["_selected_start"] = start
                        selected["_selected_end"] = start + len(quote)
                        st.caption(f"已定位字符区间 [{start}, {start + len(quote)})")
                        acceptable.append(
                            _eval_candidate_identity(selected, include_span=True)
                        )
                else:
                    acceptable.append(
                        _eval_candidate_identity(selected, include_span=False)
                    )
                missing_identity = [
                    field
                    for field in ("chunk_id", "source", "source_sha256")
                    if not candidate.get(field)
                ]
                if missing_identity:
                    st.error("候选证据身份不完整，不能作为正式证据。")
                    unit_errors.append(
                        f"{unit_id}：正确证据缺少 {', '.join(missing_identity)}"
                    )
            elif choice == "negative":
                negatives.append(
                    _eval_candidate_identity(candidate, include_span=False)
                )
                missing_identity = [
                    field
                    for field in ("chunk_id", "source", "source_sha256")
                    if not candidate.get(field)
                ]
                if missing_identity:
                    st.error("误导项身份不完整，不能提交。")
                    unit_errors.append(
                        f"{unit_id}：误导项缺少 {', '.join(missing_identity)}"
                    )
    if not retrieval_query.strip():
        unit_errors.append(f"{unit_id}：首轮检索词不能为空")
    if not recovery_query.strip():
        unit_errors.append(f"{unit_id}：补救检索词不能为空")
    if expected_status == "supported" and not acceptable:
        unit_errors.append(f"{unit_id}：应有证据，但尚未选择有效的正确证据")
    if expected_status == "no_evidence" and selected_gold_count:
        unit_errors.append(f"{unit_id}：应无证据，不能同时选择正确证据")
    return {
        "unit_id": unit_id,
        "retrieval_query": retrieval_query,
        "recovery_query": recovery_query,
        "expected_status": expected_status,
        "acceptable_evidence": acceptable if expected_status == "supported" else [],
        "hard_negative_chunks": negatives,
        "_ui_errors": unit_errors,
    }


def _review_desk_header() -> None:
    st.markdown(
        """
        <style>
        .review-hero {border-top:4px solid #1b7268;padding:.8rem 0 .35rem 0;margin-bottom:.5rem}
        .review-eyebrow,.requirement-kicker,.evidence-meta,.evidence-id {
          font-family:"IBM Plex Mono","SFMono-Regular",Consolas,monospace;
          letter-spacing:.06em;text-transform:uppercase
        }
        .review-eyebrow {color:#1b7268;font-size:.72rem;font-weight:700}
        .review-hero h2 {font-family:"Noto Sans SC","PingFang SC",sans-serif;
          font-weight:760;letter-spacing:-.035em;margin:.12rem 0}
        .review-hero p {color:#52606d;max-width:58rem;margin:.15rem 0}
        .queue-ledger {font-family:"IBM Plex Mono","SFMono-Regular",monospace;
          color:#263442;border-bottom:1px solid #aebbc4;padding:.45rem 0;margin-bottom:.65rem}
        .requirement-kicker {font-size:.68rem;color:#1b7268;margin-top:.5rem}
        .evidence-paper {display:grid;grid-template-columns:3.3rem 1fr;background:#f3f7f6;
          border:1px solid #b8c9c6;border-left:4px solid #1b7268;margin:.25rem 0 .75rem 0}
        .evidence-rail {font-family:"IBM Plex Mono","SFMono-Regular",monospace;
          color:#1b7268;padding:.8rem .55rem;border-right:1px solid #b8c9c6;font-weight:700}
        .evidence-sheet {padding:.75rem .9rem}
        .evidence-meta {font-size:.68rem;color:#526d69;margin-bottom:.55rem}
        .evidence-text {font-family:"Noto Sans SC","PingFang SC",sans-serif;color:#182528;
          line-height:1.75;white-space:pre-wrap;overflow-wrap:anywhere}
        .evidence-id {font-size:.62rem;color:#71807e;margin-top:.65rem}
        .signal-path {display:flex;align-items:center;gap:.5rem;flex-wrap:wrap;
          padding:.7rem .8rem;margin:.15rem 0 .8rem;background:#e8f1ef;border-left:4px solid #1b7268;
          font-family:"IBM Plex Mono","SFMono-Regular",monospace;font-size:.72rem}
        .signal-path i {font-style:normal;border:1px solid #8eaaa5;padding:.2rem .38rem;background:#fff}
        .generation-track {display:grid;grid-template-columns:1.2fr 1fr 1fr auto;gap:.6rem;
          align-items:center;border-left:4px solid #1b7268;background:#edf4f2;padding:.65rem .8rem;
          font-family:"IBM Plex Mono","SFMono-Regular",monospace;font-size:.72rem}
        .generation-track strong {color:#1b7268}
        .claim-sheet {border:1px solid #b8c9c6;border-left:5px solid #1b7268;
          background:#f7faf9;padding:1rem 1.1rem;margin:.45rem 0 .8rem}
        .claim-sheet h3 {font-family:"Noto Sans SC","PingFang SC",sans-serif;
          line-height:1.55;margin:.25rem 0 .55rem;color:#172624}
        .claim-meta {font-family:"IBM Plex Mono","SFMono-Regular",monospace;
          color:#526d69;font-size:.7rem;letter-spacing:.035em}
        .verdict-chip {display:inline-block;padding:.2rem .45rem;margin-right:.35rem;
          border:1px solid #8eaaa5;background:#edf4f2;font-size:.75rem}
        .verdict-stamp {display:inline-block;padding:.22rem .5rem;margin:.1rem .35rem .1rem 0;
          border:2px solid currentColor;font-family:"IBM Plex Mono","SFMono-Regular",monospace;
          font-size:.7rem;font-weight:760;letter-spacing:.055em;text-transform:uppercase}
        .verdict-supported {color:#176d63}.verdict-unsupported {color:#9b3a32}
        .verdict-insufficient {color:#8a641b}.verdict-not-factual {color:#59646f}
        @media (prefers-reduced-motion:reduce){*{scroll-behavior:auto!important}}
        @media (max-width:640px){
          .evidence-paper{grid-template-columns:1fr}
          .evidence-rail{border-right:0;border-bottom:1px solid #b8c9c6;padding:.35rem .6rem}
          .claim-sheet{padding:.8rem}.claim-sheet h3{font-size:1.05rem}
        }
        </style>
        <section class="review-hero">
          <div class="review-eyebrow">Evidence &amp; claim review desk</div>
          <h2>证据与声明判卷台</h2>
          <p>把生产声明抽检与检索证据评测放进同一条人工审核闭环；所有内容仍受当前工作区和来源权限约束。</p>
        </section>
        """,
        unsafe_allow_html=True,
    )

    st.text_input(
        "评测审核密钥",
        type="password",
        key="eval_review_key",
        help="使用有审核权限的账号时可留空；旧版部署请填写独立审核密钥。",
    )


def _claim_review_queue_label(row: Mapping) -> str:
    status = "待审" if row.get("status") == "pending" else "已审"
    verdict = CLAIM_VERDICT_LABELS.get(str(row.get("actual_verdict") or ""), "未知判定")
    claim = " ".join(str(row.get("claim") or "").split())
    if len(claim) > 54:
        claim = claim[:53] + "…"
    return f"{status} · {verdict} · {claim or '空声明'}"


def _claim_review_export_jsonl(items: Sequence[Mapping]) -> str:
    lines = [json.dumps(dict(item), ensure_ascii=False) for item in items]
    return "\n".join(lines) + ("\n" if lines else "")


def _claim_review_evidence_paper(evidence: Mapping, rank: int) -> None:
    source = html.escape(str(evidence.get("source") or "未知来源"))
    page_start = evidence.get("page_start")
    if page_start is None:
        page_start = evidence.get("page")
    page = _page_range_label(
        page_start,
        evidence.get("page_end"),
    )
    text = html.escape(str(evidence.get("text") or "（空证据）"))
    chunk_id = html.escape(str(evidence.get("chunk_id") or "缺少 chunk_id"))
    truncated = " · 已截断" if evidence.get("text_truncated") else ""
    st.markdown(
        f"""
        <article class="evidence-paper">
          <div class="evidence-rail">#{rank:02d}</div>
          <div class="evidence-sheet">
            <div class="evidence-meta">{source} · {html.escape(page)}{truncated}</div>
            <div class="evidence-text">{text}</div>
            <div class="evidence-id">{chunk_id}</div>
          </div>
        </article>
        """,
        unsafe_allow_html=True,
    )


def _claim_verification_review_desk(client: CogDocClient) -> None:
    try:
        summary_response = client.claim_verification_review_summary()
    except Exception as exc:
        st.error(f"连接声明核验审核接口失败：{exc}")
        return
    if summary_response.status_code != 200:
        st.error(_eval_response_detail(summary_response, "读取声明核验审核汇总失败"))
        st.caption("账号需具备审核权限；旧版部署请填写独立审核密钥。")
        return
    summary = response_payload(summary_response)
    if not isinstance(summary, Mapping):
        st.error("声明核验审核汇总响应格式不符合预期。")
        return

    metrics = st.columns(4)
    metrics[0].metric("待审核", int(summary.get("pending_count") or 0))
    metrics[1].metric("已审核", int(summary.get("reviewed_count") or 0))
    agreement_rate = summary.get("agreement_rate")
    metrics[2].metric(
        "人机一致率",
        f"{float(agreement_rate):.1%}" if agreement_rate is not None else "尚无标注",
    )
    metrics[3].metric("证据不完整", int(summary.get("evidence_incomplete_count") or 0))
    oldest_pending = str(summary.get("oldest_pending_at") or "")
    st.caption(
        f"SHADOW {int(summary.get('shadow_count') or 0):04d} · "
        f"ENFORCE {int(summary.get('enforce_count') or 0):04d} · "
        f"最早待审 {oldest_pending or '—'}"
    )

    filters = st.columns([2, 2, 4])
    status_options: tuple[str, ...] = ("pending", "reviewed", "all")
    status_filter = filters[0].selectbox(
        "审核状态",
        status_options,
        format_func=lambda value: {
            "pending": "待审核",
            "reviewed": "已审核",
            "all": "全部",
        }.get(str(value), str(value)),
        key="claim-review-status",
    )
    page_size = int(
        filters[1].selectbox(
            "每页",
            [10, 25, 50, 100],
            index=1,
            key="claim-review-page-size",
        )
    )
    filters[2].caption(
        "列表不下发证据正文；只有选中一条任务后才按当前 KB/source ACL 读取详情。"
    )

    scope_key = (
        f"{client.base_url}:{client.auth_cache_identity}:"
        f"{client.workspace_id or '-'}:{status_filter}:{page_size}"
    )
    page_state = st.session_state.claim_review_pages.setdefault(
        scope_key, {"index": 0, "cursors": [None]}
    )
    cursors = page_state.get("cursors")
    if not isinstance(cursors, list) or not cursors:
        cursors = [None]
        page_state["cursors"] = cursors
    page_index = max(0, min(int(page_state.get("index") or 0), len(cursors) - 1))
    page_state["index"] = page_index
    cursor = cursors[page_index]
    try:
        list_response = client.list_claim_verification_reviews(
            status=None if status_filter == "all" else status_filter,
            limit=page_size,
            cursor=str(cursor) if cursor else None,
        )
    except Exception as exc:
        st.error(f"读取声明核验审核队列失败：{exc}")
        return
    if list_response.status_code != 200:
        st.error(_eval_response_detail(list_response, "读取声明核验审核队列失败"))
        return
    payload = response_payload(list_response)
    if not isinstance(payload, Mapping) or not isinstance(payload.get("items"), list):
        st.error("声明核验审核队列响应格式不符合预期。")
        return
    rows = [row for row in payload["items"] if isinstance(row, Mapping)]
    next_cursor = payload.get("next_cursor")

    navigation = st.columns([1, 2, 1])
    if navigation[0].button(
        "← 上一页",
        disabled=page_index == 0,
        key=f"claim-review-prev-{scope_key}",
        use_container_width=True,
    ):
        page_state["index"] = page_index - 1
        st.rerun()
    navigation[1].markdown(
        f"<div class='queue-ledger'>PAGE {page_index + 1:03d}　"
        f"VISIBLE {len(rows):03d}　TOTAL {int(summary.get('total_count') or 0):04d}</div>",
        unsafe_allow_html=True,
    )
    if navigation[2].button(
        "下一页 →",
        disabled=not next_cursor,
        key=f"claim-review-next-{scope_key}",
        use_container_width=True,
    ):
        following_index = page_index + 1
        if following_index < len(cursors):
            cursors[following_index] = str(next_cursor)
            del cursors[following_index + 1 :]
        else:
            cursors.append(str(next_cursor))
        page_state["index"] = following_index
        st.rerun()

    if not rows:
        st.info(
            "当前筛选下没有可见任务。可切换审核状态；若队列始终为空，请确认已启用声明抽样且账号拥有对应来源权限。"
        )
    else:
        row_by_id = {str(row.get("review_id") or ""): row for row in rows}
        selected_id = st.selectbox(
            "声明审核队列",
            list(row_by_id),
            format_func=lambda value: _claim_review_queue_label(row_by_id[value]),
            key=f"claim-review-selected-{scope_key}-{page_index}",
        )
        try:
            detail_response = client.get_claim_verification_review(selected_id)
        except Exception as exc:
            st.error(f"读取声明与证据详情失败：{exc}")
            return
        if detail_response.status_code != 200:
            st.error(_eval_response_detail(detail_response, "读取声明与证据详情失败"))
            return
        detail = response_payload(detail_response)
        if not isinstance(detail, Mapping):
            st.error("声明核验详情响应格式不符合预期。")
            return

        claim = html.escape(str(detail.get("claim") or "（空声明）"))
        review_id = html.escape(str(detail.get("review_id") or ""))
        task_type = html.escape(str(detail.get("task_type") or "unknown"))
        policy_id = html.escape(str(detail.get("policy_id") or ""))
        actual_verdict = str(detail.get("actual_verdict") or "")
        verdict_class = {
            "supported": "verdict-supported",
            "unsupported": "verdict-unsupported",
            "insufficient": "verdict-insufficient",
            "not_factual": "verdict-not-factual",
        }.get(actual_verdict, "verdict-not-factual")
        st.markdown(
            f"""
            <section class="claim-sheet">
              <div class="claim-meta">{task_type} · {policy_id} · {review_id}</div>
              <h3>{claim}</h3>
              <span class="verdict-stamp {verdict_class}">MODEL / {html.escape(CLAIM_VERDICT_LABELS.get(actual_verdict, "未知"))}</span>
              <span class="verdict-chip">模式：{html.escape(str(detail.get("effective_mode") or ""))}</span>
              <span class="verdict-chip">决策：{html.escape(str(detail.get("decision") or ""))}</span>
            </section>
            """,
            unsafe_allow_html=True,
        )
        confidence = detail.get("confidence")
        duration = detail.get("duration_ms")
        st.caption(
            f"模型理由：{str(detail.get('reason') or '—')} · "
            f"置信度 {float(confidence):.2f} · "
            f"耗时 {float(duration):.1f} ms"
            if confidence is not None and duration is not None
            else f"模型理由：{str(detail.get('reason') or '—')}"
        )
        if not detail.get("evidence_complete"):
            st.warning("引用证据快照不完整；标注时应把缺失证据纳入判断。")
        evidence = [
            item for item in detail.get("evidence") or [] if isinstance(item, Mapping)
        ]
        st.markdown(f"#### 精确引用证据 · {len(evidence)} 段")
        if evidence:
            for rank, item in enumerate(evidence, start=1):
                _claim_review_evidence_paper(item, rank)
        else:
            st.warning("这条声明没有可展示的引用证据快照。")

        current_expected = str(detail.get("expected_verdict") or "")
        options = list(CLAIM_VERDICT_LABELS)
        with st.form(f"claim-review-label-{selected_id}-{detail.get('revision')}"):
            expected = st.radio(
                "人工结论",
                options,
                index=(
                    options.index(current_expected)
                    if current_expected in options
                    else None
                ),
                horizontal=True,
                format_func=CLAIM_VERDICT_LABELS.get,
            )
            if expected is None:
                st.caption("请先独立判断，再选择人工结论；系统不会默认沿用模型判定。")
            note = st.text_area(
                "审核备注",
                value=str(detail.get("review_note") or ""),
                placeholder="说明支持、反驳或证据不足的关键依据（可选）",
            )
            submitted = st.form_submit_button(
                "保存人工结论",
                type="primary",
                use_container_width=True,
                disabled=expected is None,
            )
        if submitted and expected is not None:
            try:
                label_response = client.label_claim_verification_review(
                    selected_id,
                    expected_verdict=expected,
                    expected_revision=int(detail.get("revision") or 1),
                    review_note=note.strip(),
                )
            except Exception as exc:
                st.error(f"保存人工结论失败：{exc}")
                return
            if label_response.status_code == 200:
                st.success("人工结论已保存；汇总指标和发布门禁数据已更新。")
                st.rerun()
            elif label_response.status_code == 409:
                st.warning(
                    "这条任务已被其他审核者更新，请刷新后基于最新 revision 重审。"
                )
            else:
                st.error(_eval_response_detail(label_response, "保存人工结论失败"))

    st.divider()
    export_scope = (
        f"{client.base_url}:{client.auth_cache_identity}:{client.workspace_id or '-'}"
    )
    if st.session_state.get("claim_review_export_scope") != export_scope:
        st.session_state.claim_review_export_jsonl = ""
        st.session_state.claim_review_export_scope = export_scope
    export_columns = st.columns([2, 3])
    if export_columns[0].button(
        "准备声明核验门禁集",
        key="claim-review-export",
        use_container_width=True,
    ):
        try:
            with st.spinner("正在遍历已审核分页…"):
                export_items = client.export_all_claim_verification_reviews()
        except Exception as exc:
            st.error(f"导出声明核验门禁集失败：{exc}")
        else:
            st.session_state.claim_review_export_jsonl = _claim_review_export_jsonl(
                export_items
            )
            st.session_state.claim_review_export_scope = export_scope
            st.success(f"已准备 {len(export_items)} 条人工判卷样本。")
    if st.session_state.claim_review_export_jsonl:
        export_columns[1].download_button(
            "下载 claim_verification_eval.jsonl",
            data=st.session_state.claim_review_export_jsonl,
            file_name="claim_verification_eval.jsonl",
            mime="application/x-ndjson",
            use_container_width=True,
        )


def _evidence_review_area(kb_id: str | None) -> None:
    _review_desk_header()
    client = _eval_review_client()
    desk = st.radio(
        "审核工作台",
        ["声明核验", "检索证据"],
        horizontal=True,
        label_visibility="collapsed",
        key="evidence-review-desk",
    )
    if desk == "声明核验":
        _claim_verification_review_desk(client)
    else:
        _retrieval_eval_review_desk(kb_id, client)


def _retrieval_eval_review_desk(kb_id: str | None, client: CogDocClient) -> None:
    _render_index_migration_console(client, kb_id)
    _render_retrieval_diagnostic_console(client, kb_id)
    controls = st.columns([2, 2, 3])
    status_filter = controls[0].selectbox(
        "状态",
        ["pending", "approved", "rejected", "all"],
        format_func={
            "pending": "待审核",
            "approved": "已通过",
            "rejected": "已驳回",
            "all": "全部",
        }.get,
    )
    partition_filter = controls[1].selectbox(
        "数据分区",
        ["all", "release_gate", "training"],
        format_func={
            "all": "全部",
            "release_gate": "发布门禁",
            "training": "训练集",
        }.get,
    )
    current_kb_only = controls[2].toggle(
        "只看当前知识库", value=bool(kb_id), disabled=not kb_id
    )
    try:
        response = client.list_retrieval_eval_drafts(
            kb_id=kb_id if current_kb_only else None,
            status=None if status_filter == "all" else status_filter,
            dataset_partition=None if partition_filter == "all" else partition_filter,
            limit=500,
        )
    except Exception as exc:
        st.error(f"连接审核接口失败：{exc}")
        return
    if response.status_code != 200:
        st.error(_eval_response_detail(response, "读取证据草稿失败"))
        st.caption("账号需具备审核权限；旧版部署需填写独立审核密钥。")
        return
    payload = response_payload(response)
    drafts = payload.get("drafts", []) if isinstance(payload, Mapping) else []
    counts = {
        status: sum(1 for row in drafts if row.get("status") == status)
        for status in ("pending", "approved", "rejected")
    }
    stale_count = sum(1 for row in drafts if row.get("is_stale"))
    st.markdown(
        "<div class='queue-ledger'>"
        f"QUEUE {len(drafts):03d}　PENDING {counts['pending']:03d}　"
        f"APPROVED {counts['approved']:03d}　REJECTED {counts['rejected']:03d}　"
        f"STALE {stale_count:03d}</div>",
        unsafe_allow_html=True,
    )
    if not drafts:
        st.info(
            "当前筛选条件下没有草稿。对一次错误回答点“检索问题”反馈后，这里会出现待审任务。"
        )
        return

    def draft_label(row: Mapping) -> str:
        units = row.get("units") or []
        first_label = str(units[0].get("label") or "未命名需求") if units else "无需求"
        status = {"pending": "待审", "approved": "通过", "rejected": "驳回"}.get(
            str(row.get("status")), "未知"
        )
        stale = " · 已过期" if row.get("is_stale") else ""
        return (
            f"{status}{stale} · {first_label} · {str(row.get('draft_id') or '')[:10]}"
        )

    draft_by_id = {str(row.get("draft_id")): row for row in drafts}
    selected_id = st.selectbox(
        "审核队列",
        list(draft_by_id),
        format_func=lambda value: draft_label(draft_by_id[value]),
    )
    draft = draft_by_id[selected_id]
    units = [unit for unit in draft.get("units") or [] if isinstance(unit, Mapping)]
    if not units:
        st.error("这份草稿没有可审核的原子需求，不能继续处理。")
        return
    if draft.get("is_stale"):
        st.error("这份草稿对应的索引已变化，不能继续标注。请由新反馈生成新草稿。")
        st.code("\n".join(str(item) for item in draft.get("stale_reasons") or []))
        return

    cache_key = (
        f"{client.base_url}:{client.auth_cache_identity}:"
        f"{client.workspace_id or '-'}:{selected_id}:{draft.get('revision')}"
    )
    candidate_payload = st.session_state.eval_candidate_cache.get(cache_key)
    if candidate_payload is None:
        try:
            with st.spinner("正在调取候选原文…"):
                candidate_response = client.get_retrieval_eval_candidates(selected_id)
        except Exception as exc:
            st.error(f"调取候选原文失败：{exc}")
            return
        if candidate_response.status_code != 200:
            st.error(_eval_response_detail(candidate_response, "读取候选原文失败"))
            return
        candidate_payload = response_payload(candidate_response)
        st.session_state.eval_candidate_cache[cache_key] = candidate_payload
    candidates = (
        candidate_payload.get("candidates", [])
        if isinstance(candidate_payload, Mapping)
        else []
    )
    st.caption(
        f"草稿 {selected_id} · revision {draft.get('revision')} · "
        f"{len(units)} 道需求 · {len(candidates)} 段候选原文"
    )
    if not candidates:
        st.warning(
            "本轮没有召回候选原文。若确认知识库确实无证据，可将需求标为“应无证据”。"
        )

    tabs = st.tabs(
        [
            f"{index + 1}. {str(unit.get('label') or unit.get('unit_id'))[:24]}"
            for index, unit in enumerate(units)
        ]
    )
    annotations = []
    for tab, unit in zip(tabs, units):
        with tab:
            annotations.append(_render_eval_unit(draft, unit, candidates))

    if str(draft.get("status") or "") != "pending":
        st.info(f"这份草稿已经{draft_label(draft).split(' · ')[0]}，仅供查阅。")
    else:
        st.divider()
        reason = st.text_area(
            "驳回原因",
            placeholder="驳回时必填：说明题目、查询词或证据哪里不对。",
            key=f"eval-reject-reason-{selected_id}-{draft.get('revision')}",
        )
        action_columns = st.columns([2, 2, 3])
        approve = action_columns[0].button(
            "确认并通过", type="primary", use_container_width=True
        )
        reject = action_columns[1].button("驳回草稿", use_container_width=True)
        if approve or reject:
            annotation_errors = [
                str(error)
                for annotation in annotations
                for error in annotation.get("_ui_errors", [])
            ]
            if approve and annotation_errors:
                st.error("还不能通过：\n\n- " + "\n- ".join(annotation_errors))
            elif reject and not reason.strip():
                st.error("驳回原因不能为空。")
            else:
                clean_annotations = [
                    {
                        key: value
                        for key, value in annotation.items()
                        if key != "_ui_errors"
                    }
                    for annotation in annotations
                ]
                try:
                    review_response = client.review_retrieval_eval_draft(
                        selected_id,
                        decision="approved" if approve else "rejected",
                        expected_revision=int(draft.get("revision") or 1),
                        annotations={"units": clean_annotations} if approve else None,
                        reason=reason.strip(),
                    )
                except Exception as exc:
                    st.error(f"保存审核结果失败：{exc}")
                    return
                if review_response.status_code == 200:
                    st.session_state.eval_candidate_cache.pop(cache_key, None)
                    st.success(
                        "已通过并写入正式评测集。" if approve else "已驳回草稿。"
                    )
                    st.rerun()
                else:
                    st.error(_eval_response_detail(review_response, "保存审核结果失败"))

    st.divider()
    export_columns = st.columns([2, 3])
    if export_columns[0].button("准备发布门禁集", use_container_width=True):
        try:
            export_response = client.export_retrieval_eval_drafts()
        except Exception as exc:
            st.error(f"导出评测集失败：{exc}")
            return
        if export_response.status_code != 200:
            st.error(_eval_response_detail(export_response, "导出评测集失败"))
        else:
            export_payload = response_payload(export_response)
            items = (
                export_payload.get("items", [])
                if isinstance(export_payload, Mapping)
                else []
            )
            st.session_state.eval_export_jsonl = "\n".join(
                json.dumps(item, ensure_ascii=False) for item in items
            ) + ("\n" if items else "")
            st.success(f"已准备 {len(items)} 条发布门禁样本。")
    if st.session_state.eval_export_jsonl:
        export_columns[1].download_button(
            "下载 retrieval_eval.jsonl",
            data=st.session_state.eval_export_jsonl,
            file_name="retrieval_eval.jsonl",
            mime="application/x-ndjson",
            use_container_width=True,
        )


# 处理对话区。
def _chat_area() -> None:
    # 主对话区按上下文还原历史并渲染气泡。
    _drain_stream_events()
    _drain_retrieve_debug_events()
    kb_id = st.session_state.kb_id
    if kb_id:
        _restore_history(kb_id)
    current_key = _context_key(kb_id) if kb_id else None
    current_pending = (
        st.session_state.pending_streams.get(current_key) if current_key else None
    )
    answering = bool(current_pending)
    current_view = st.session_state.main_views_by_context.get(current_key, "对话")
    if current_view == "知识":
        current_view = "派生知识"
        st.session_state.main_views_by_context[current_key] = current_view
    if current_view not in MAIN_VIEWS:
        current_view = "对话"
        st.session_state.main_views_by_context[current_key] = current_view
    pending_total = _pending_review_count(_client(), kb_id)
    view_key = (
        f"main-view-{kb_id}-{st.session_state.session_id}"
        if current_key
        else "main-view-empty"
    )
    view = st.radio(
        "主视图",
        MAIN_VIEWS,
        index=MAIN_VIEWS.index(current_view),
        horizontal=True,
        key=view_key,
        label_visibility="collapsed",
        format_func=lambda value: (
            _tab_label(value, pending_total) if value == "派生知识" else value
        ),
    )
    if current_key:
        st.session_state.main_views_by_context[current_key] = view

    if view == "对话":
        st.subheader(f"对话 · {kb_id or '未选择知识库'}")
        mode = st.radio(
            "模式",
            ["auto", "qa", "summary", "compare"],
            horizontal=True,
            key="chat_mode",
            disabled=answering,
        )

        messages = _messages_for(kb_id) if kb_id else []
        for msg in messages:
            with st.chat_message(msg["role"]):
                st.markdown(msg["content"] or "（无答案）")
                if msg.get("final") and not answering:
                    _render_evidence(
                        msg["final"], key=msg["id"], query=msg.get("query", "")
                    )

        if current_pending:
            with st.chat_message("assistant"):
                st.markdown(_stream_preview(current_pending.get("answer", "")))
                if current_pending.get("stage"):
                    st.caption(current_pending["stage"])
            if st.button("■ 终止问题", type="primary", use_container_width=True):
                _cancel_stream(current_key)
                st.rerun()
            time.sleep(STREAM_RERUN_INTERVAL_SECONDS)
            st.rerun()

        prompt = st.chat_input("问点什么…", disabled=not kb_id)
        if prompt:
            _start_stream(kb_id, prompt, mode)
            st.rerun()
    elif view == "研究":
        _research_area(kb_id)
    elif view == "来源":
        _source_navigation_console(_client(), kb_id)
    elif view == "派生知识":
        _knowledge_area(kb_id)
    elif view == "证据审核":
        _evidence_review_area(kb_id)
    else:
        _debug_area(kb_id)


# 生成下一个标识。
def _next_id() -> int:
    st.session_state.msg_seq += 1
    return st.session_state.msg_seq


# 隐藏默认界面。
def _hide_default_chrome() -> None:
    # 隐藏右上角默认工具条与页脚，只留自家品牌。
    st.markdown(
        """
        <style>
        [data-testid="stToolbar"] {display: none;}
        [data-testid="stDecoration"] {display: none;}
        body #MainMenu {visibility: hidden;}
        footer {visibility: hidden;}
        </style>
        """,
        unsafe_allow_html=True,
    )


# 完成 品牌头部页头 处理。
def _brand_header() -> None:
    # 主区顶部品牌标题，替代被隐藏的默认头。
    st.markdown(
        "<h2 style='margin:0 0 0.1rem 0;'>🧠 CogDoc</h2>"
        "<p style='color:#888;margin:0 0 0.6rem 0;'>面向个人 / 企业的本地 RAG 知识库控制台</p>",
        unsafe_allow_html=True,
    )


# 启动入口。
def main() -> None:
    st.set_page_config(page_title="CogDoc", layout="wide")
    _init_state()
    _hide_default_chrome()
    _brand_header()
    if not _auth_gate():
        return
    _sidebar()
    _chat_area()


if __name__ == "__main__":
    main()
