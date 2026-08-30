"""API-backed CogDoc command line interface.

The Web application and this CLI deliberately share the same HTTP boundary.
The established direct-storage console remains available through
``cogdoc --local-storage`` for offline recovery and existing local workflows.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import shlex
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

from cogdoc.config.llm_config import apply_llm_config
from cogdoc.config.settings import get_settings
from cogdoc.frontend.api_client import (
    CogDocAPIError,
    CogDocClient,
    format_api_error,
    response_payload,
)
from cogdoc.tools.source_parser import SUPPORTED_EXTENSIONS


DEFAULT_API_URL = "http://localhost:8000"
CONFIG_ENV = "COGDOC_CLI_CONFIG"
BUILT_IN_ROLE_IDS = ("owner", "admin", "editor", "reviewer", "viewer")

# Keep the established CogDoc console identity while the command implementation
# uses the same authenticated API as the Web workspace.
BANNER = r"""
 ██████╗ ██████╗  ██████╗ ██████╗  ██████╗  ██████╗
██╔════╝██╔═══██╗██╔════╝ ██╔══██╗██╔═══██╗██╔════╝
██║     ██║   ██║██║  ███╗██║  ██║██║   ██║██║
██║     ██║   ██║██║   ██║██║  ██║██║   ██║██║
╚██████╗╚██████╔╝╚██████╔╝██████╔╝╚██████╔╝╚██████╗
 ╚═════╝ ╚═════╝  ╚═════╝ ╚═════╝  ╚═════╝  ╚═════╝
"""

INTERACTIVE_HELP = """\
可用命令（可省略开头的 /）：
  账号与工作区
    /status                         查看账号、Workspace 与服务状态
    /workspace list|use|create      管理 Workspace
    /workspace members|roles        查看成员与角色
  知识库
    /kb                              列出可访问的知识库
    /kb new <名称>                   创建并选中知识库
    /kb use <名称>                   切换当前知识库
    /kb rm <名称>                    删除知识库
    /kb access [名称]                查看或设置可访问角色
  文档与任务
    /docs 或 /ls                     列出当前库文档
    /inbox                          列出本机文档收件箱
    /add <文件...>                   批量上传并建立索引
    /rm <文件名>                    删除文档
    /jobs                            查看解析、索引、外部同步与系统任务
  对话
    <问题>                            向当前知识库发起流式问答
    /qa <问题>                        强制问答模式
    /summary <文件名>              总结指定文档
    /compare <A> <B> ...              对比多篇文档
    /chats                           列出会话历史
    /new                             开启新会话
  知识与质量
    /knowledge ...                   派生知识与审核
    /dk approve|reject|archive <ID>  审核派生知识
    /tuning list|enable|disable      管理检索调权
    /review summary|metrics|export  审核队列与闭环指标
    /feedback ...                    反馈、分析与检索调权
    /research ...                    Research 工作流
    /trace ...                       Trace 调试
    /diagnose <问题>                 运行检索诊断
    /evaluation ...                  RAG 评测与声明核验
  企业能力
    /integration ...                 连接器与外部同步
    /acl ...                         知识库与文档授权
    /audit ...                       审计导出
    /security ...                    安全策略与会话
    /service-account ...             服务账号
    /migration ...                   索引代际迁移
  模型与运行
    /local  /cloud                   切换本地 / 云端对话模式
    /config                          配置本机服务的云模型
  /help                             显示本帮助
  /exit                             退出
"""


def default_config_path() -> Path:
    configured = os.getenv(CONFIG_ENV)
    if configured and configured.strip():
        return Path(configured).expanduser()
    configured_root = os.getenv("XDG_CONFIG_HOME")
    root = (
        Path(configured_root).expanduser()
        if configured_root and configured_root.strip()
        else Path.home() / ".config"
    )
    return root / "cogdoc" / "cli.json"


class CLIState:
    """Small permission-restricted session file; passwords are never persisted."""

    def __init__(self, path: Path):
        self.path = path
        self.values: dict[str, Any] = {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self.values = raw
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            # A broken local preference file must not alter server data.  Commands
            # can still recover by logging in and replacing it.
            self.values = {}

    def get(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)

    def update(self, **values: Any) -> None:
        self.values.update(
            {key: value for key, value in values.items() if value is not None}
        )
        self.save()

    def replace_session(self, **values: Any) -> None:
        """Persist a new identity without leaking the previous KB selection."""

        for key in (
            "access_token",
            "expires_at",
            "email",
            "workspace_id",
            "kb_id",
            "session_id",
        ):
            self.values.pop(key, None)
        self.update(**values)

    def clear_session(self) -> None:
        for key in (
            "access_token",
            "expires_at",
            "email",
            "workspace_id",
            "kb_id",
            "session_id",
        ):
            self.values.pop(key, None)
        self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.path.with_name(f".{self.path.name}.tmp-{os.getpid()}")
        data = json.dumps(self.values, ensure_ascii=False, indent=2).encode("utf-8")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            os.chmod(self.path, 0o600)
            directory = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def checked(response: httpx.Response) -> Any:
    payload = response_payload(response)
    if response.status_code >= 400:
        raise CogDocAPIError(
            format_api_error(payload, response.status_code),
            status_code=response.status_code,
            payload=payload,
        )
    return payload


def csv_values(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def json_object(value: str, label: str = "JSON") -> dict[str, Any]:
    payload = json.loads(value)
    if not isinstance(payload, dict):
        raise ValueError(f"{label} 必须是对象")
    return payload


def json_list(value: str, label: str = "JSON") -> list[Any]:
    payload = json.loads(value)
    if not isinstance(payload, list):
        raise ValueError(f"{label} 必须是数组")
    return payload


def required_value(payload: Mapping[str, Any], key: str) -> Any:
    if key not in payload:
        raise ValueError(f"JSON 缺少必填字段: {key}")
    return payload[key]


def boolean_value(
    payload: Mapping[str, Any], key: str, *, default: bool | None = None
) -> bool:
    value = payload.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"JSON 字段 {key} 必须是布尔值")
    return value


def print_payload(payload: Any, *, compact: bool = False) -> None:
    if payload in (None, ""):
        return
    if isinstance(payload, str):
        print(payload)
        return
    print(
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=None if compact else 2,
            separators=(",", ":") if compact else None,
            default=str,
        )
    )


def _password(confirm: bool = False) -> str:
    value = getpass.getpass("密码: ")
    if confirm and value != getpass.getpass("再次输入密码: "):
        raise ValueError("两次输入的密码不一致")
    return value


def _add_kb_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--kb", help="知识库 ID；省略时使用 `kb use` 选中的知识库")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cogdoc",
        description="CogDoc API CLI（与 Web 使用相同账号、Workspace、ACL 和任务状态）",
    )
    parser.add_argument("--api-url", help="CogDoc API 地址")
    parser.add_argument(
        "--token", help="Bearer/服务账号令牌；优先使用环境变量 COGDOC_API_KEY"
    )
    parser.add_argument("--workspace", help="为本次命令指定 Workspace ID")
    parser.add_argument("--config-path", type=Path, help="CLI 登录态文件")
    parser.add_argument("--compact", action="store_true", help="单行 JSON 输出")
    sub = parser.add_subparsers(dest="command")

    login = sub.add_parser("login", help="登录账号")
    login.add_argument("email")
    login.add_argument("--workspace-id")

    register = sub.add_parser("register", help="注册账号")
    register.add_argument("email")
    register.add_argument("display_name")
    register.add_argument("--workspace-name")

    sub.add_parser("logout", help="注销并清除本地登录态")
    sub.add_parser("status", help="显示账号、Workspace 和服务状态")

    inbox = sub.add_parser("inbox", help="列出本机文档收件箱")
    inbox.add_argument("--directory", type=Path)

    config = sub.add_parser("config", help="配置本机 CogDoc 服务的云模型")
    config.add_argument("--base-url")
    config.add_argument("--model")

    auth = sub.add_parser("auth", help="账号、会话与 OIDC")
    auth_sub = auth.add_subparsers(dest="auth_action", required=True)
    auth_sub.add_parser("change-password")
    auth_sub.add_parser("logout-all")
    auth_sub.add_parser("sessions")
    revoke_session = auth_sub.add_parser("revoke-session")
    revoke_session.add_argument("session_id")
    oidc_begin = auth_sub.add_parser("oidc-login")
    oidc_begin.add_argument("return_url")
    oidc_begin.add_argument("--workspace-id")
    oidc_exchange = auth_sub.add_parser("oidc-exchange")
    oidc_exchange.add_argument("code")
    oidc_link = auth_sub.add_parser("oidc-link")
    oidc_link.add_argument("return_url")
    auth_sub.add_parser("oidc-identities")
    oidc_unlink = auth_sub.add_parser("oidc-unlink")
    oidc_unlink.add_argument("identity_id")
    accept_invite = auth_sub.add_parser("accept-invite")
    accept_invite.add_argument("token")
    accept_invite.add_argument("--email")
    accept_invite.add_argument("--display-name")

    workspace = sub.add_parser("workspace", aliases=["ws"], help="Workspace 管理")
    ws_sub = workspace.add_subparsers(dest="workspace_action", required=True)
    ws_sub.add_parser("list")
    ws_use = ws_sub.add_parser("use")
    ws_use.add_argument("workspace_id")
    ws_create = ws_sub.add_parser("create")
    ws_create.add_argument("name")
    ws_get = ws_sub.add_parser("get")
    ws_get.add_argument("workspace_id", nargs="?")
    ws_update = ws_sub.add_parser("update")
    ws_update.add_argument("name")
    ws_update.add_argument("--workspace-id")
    ws_update.add_argument("--revision", type=int)
    ws_delete = ws_sub.add_parser("delete")
    ws_delete.add_argument("workspace_id", nargs="?")
    ws_delete.add_argument("--yes", action="store_true")
    for action in ("members", "roles"):
        item = ws_sub.add_parser(action)
        item.add_argument("workspace_id", nargs="?")
    role_create = ws_sub.add_parser("create-role")
    role_create.add_argument("name")
    role_create.add_argument(
        "--base-role",
        choices=["admin", "editor", "reviewer", "viewer"],
        default="viewer",
    )
    role_create.add_argument("--description", default="")
    role_create.add_argument("--workspace-id")
    invite = ws_sub.add_parser("invite")
    invite.add_argument("email")
    invite.add_argument(
        "--role", choices=["admin", "editor", "reviewer", "viewer"], default="viewer"
    )
    invite.add_argument("--workspace-id")
    invites = ws_sub.add_parser("invites")
    invites.add_argument("workspace_id", nargs="?")
    revoke_invite = ws_sub.add_parser("revoke-invite")
    revoke_invite.add_argument("invite_id")
    revoke_invite.add_argument("--workspace-id")
    assign_role = ws_sub.add_parser("assign-role")
    assign_role.add_argument("member_id")
    assign_role.add_argument("role_id")
    assign_role.add_argument("--workspace-id")
    assign_role.add_argument("--revision", type=int)
    remove_member = ws_sub.add_parser("remove-member")
    remove_member.add_argument("member_id")
    remove_member.add_argument("--workspace-id")
    remove_member.add_argument("--yes", action="store_true")
    delete_role = ws_sub.add_parser("delete-role")
    delete_role.add_argument("role_id")
    delete_role.add_argument("--workspace-id")
    delete_role.add_argument("--yes", action="store_true")

    acl = sub.add_parser("acl", help="知识库和文档主体授权")
    acl_sub = acl.add_subparsers(dest="acl_action", required=True)
    for action in ("kb-grants", "kb-grant", "kb-revoke"):
        item = acl_sub.add_parser(action)
        _add_kb_argument(item)
        if action != "kb-grants":
            item.add_argument("subject_id")
        if action == "kb-grant":
            item.add_argument("role")
    for action in ("document-grants", "document-grant", "document-revoke"):
        item = acl_sub.add_parser(action)
        item.add_argument("document_id")
        _add_kb_argument(item)
        if action != "document-grants":
            item.add_argument("subject_id")
        if action == "document-grant":
            item.add_argument("role")

    kb = sub.add_parser("kb", help="知识库")
    kb_sub = kb.add_subparsers(dest="kb_action", required=True)
    kb_sub.add_parser("list")
    kb_use = kb_sub.add_parser("use")
    kb_use.add_argument("kb_id")
    kb_create = kb_sub.add_parser("create")
    kb_create.add_argument("kb_id")
    kb_create.add_argument(
        "--access", choices=["workspace", "private"], default="workspace"
    )
    kb_create.add_argument("--roles", help="允许访问的 role_id，逗号分隔")
    kb_delete = kb_sub.add_parser("delete")
    kb_delete.add_argument("kb_id")
    kb_delete.add_argument("--yes", action="store_true")
    kb_access = kb_sub.add_parser("access")
    kb_access.add_argument("kb_id", nargs="?")
    kb_access.add_argument("--set", choices=["workspace", "private"])
    kb_access.add_argument("--roles", help="允许访问的 role_id，逗号分隔")
    kb_sub.add_parser("embedding-profiles")

    document = sub.add_parser("document", aliases=["doc"], help="文档")
    doc_sub = document.add_subparsers(dest="document_action", required=True)
    doc_list = doc_sub.add_parser("list")
    _add_kb_argument(doc_list)
    upload = doc_sub.add_parser("upload")
    upload.add_argument("files", nargs="+")
    _add_kb_argument(upload)
    upload.add_argument("--roles", help="允许访问的 role_id，逗号分隔")
    upload.add_argument("--embedding", choices=["local", "cloud"], default="local")
    doc_delete = doc_sub.add_parser("delete")
    doc_delete.add_argument("name")
    _add_kb_argument(doc_delete)
    doc_delete.add_argument("--yes", action="store_true")
    doc_access = doc_sub.add_parser("access")
    doc_access.add_argument("document_id")
    _add_kb_argument(doc_access)
    doc_access.add_argument("--source")
    doc_access.add_argument("--set", choices=["workspace", "private"])
    doc_access.add_argument("--roles", help="允许访问的 role_id，逗号分隔")

    jobs = sub.add_parser("jobs", help="索引、外部同步和系统任务")
    jobs.add_argument(
        "--kind", choices=["all", "index", "sync", "system"], default="all"
    )
    _add_kb_argument(jobs)
    jobs.add_argument("--limit", type=int, default=200)
    jobs.add_argument("--cancel", metavar="JOB_ID")
    jobs.add_argument("--replay", metavar="JOB_ID")
    jobs.add_argument("--replay-key")

    chat = sub.add_parser("chat", help="流式问答")
    chat.add_argument("query", nargs="+")
    _add_kb_argument(chat)
    chat.add_argument(
        "--mode", choices=["auto", "qa", "summary", "compare"], default="auto"
    )
    chat.add_argument("--session")
    chat.add_argument("--local", action="store_true")

    sessions = sub.add_parser("sessions", help="会话历史")
    sessions.add_argument(
        "action", choices=["list", "history", "delete"], default="list", nargs="?"
    )
    sessions.add_argument("session_id", nargs="?")
    _add_kb_argument(sessions)
    sessions.add_argument("--yes", action="store_true")

    knowledge = sub.add_parser("knowledge", aliases=["dk"], help="派生知识")
    know_sub = knowledge.add_subparsers(dest="knowledge_action", required=True)
    know_list = know_sub.add_parser("list")
    _add_kb_argument(know_list)
    know_list.add_argument("--status")
    know_show = know_sub.add_parser("show")
    know_show.add_argument("knowledge_id")
    _add_kb_argument(know_show)
    know_summary = know_sub.add_parser("summary")
    _add_kb_argument(know_summary)
    know_create = know_sub.add_parser("create")
    know_create.add_argument("text", nargs="+")
    _add_kb_argument(know_create)
    know_create.add_argument("--origin", default="manual_entry")
    know_create.add_argument("--source")
    know_create.add_argument("--document-id")
    know_create.add_argument("--source-sha256")
    know_create.add_argument("--chunks", help="关联 chunk_id，逗号分隔")
    know_create.add_argument("--trace-id")
    know_create.add_argument(
        "--certainty", choices=["low", "medium", "high"], default="medium"
    )
    know_revise = know_sub.add_parser("revise")
    know_revise.add_argument("knowledge_id")
    know_revise.add_argument("text", nargs="+")
    know_revise.add_argument("--source")
    know_revise.add_argument("--document-id")
    know_revise.add_argument("--source-sha256")
    know_revise.add_argument("--chunks", help="关联 chunk_id，逗号分隔")
    know_revise.add_argument("--trace-id")
    know_revise.add_argument(
        "--certainty", choices=["low", "medium", "high"], default="medium"
    )
    know_review = know_sub.add_parser("review")
    know_review.add_argument("action", choices=["approve", "reject", "archive"])
    know_review.add_argument("knowledge_id")
    know_review.add_argument("--note")
    know_batch = know_sub.add_parser("batch")
    know_batch.add_argument("action", choices=["batch-approve", "batch-reject"])
    know_batch.add_argument("knowledge_ids", nargs="+")
    know_batch.add_argument("--note")
    know_delete = know_sub.add_parser("delete")
    know_delete.add_argument("knowledge_id")
    know_delete.add_argument("--yes", action="store_true")
    for action in ("scan", "status"):
        item = know_sub.add_parser(action)
        _add_kb_argument(item)

    feedback = sub.add_parser("feedback", help="反馈、分析与检索调权")
    feedback.add_argument(
        "action",
        choices=[
            "submit",
            "list",
            "analysis",
            "tuning",
            "enable",
            "disable",
            "metrics",
            "review-export",
        ],
    )
    feedback.add_argument("item_id", nargs="?")
    _add_kb_argument(feedback)
    feedback.add_argument("--trace-id")
    feedback.add_argument("--value", choices=["thumbs_up", "thumbs_down", "correction"])
    feedback.add_argument(
        "--issue",
        choices=[
            "no_evidence",
            "wrong_answer",
            "bad_retrieval",
            "correction",
            "other",
        ],
    )
    feedback.add_argument("--query")
    feedback.add_argument("--answer")
    feedback.add_argument("--text")
    feedback.add_argument("--correction")
    feedback.add_argument("--source")
    feedback.add_argument("--source-sha256")
    feedback.add_argument("--chunks", help="关联 chunk_id，逗号分隔")

    research = sub.add_parser("research", help="Research 工作流")
    research_sub = research.add_subparsers(dest="research_action", required=True)
    research_list = research_sub.add_parser("list")
    _add_kb_argument(research_list)
    research_create = research_sub.add_parser("create")
    research_create.add_argument("goal", nargs="+")
    _add_kb_argument(research_create)
    research_create.add_argument("--title")
    research_create.add_argument("--local", action="store_true")
    research_run = research_sub.add_parser("action")
    research_run.add_argument(
        "action", choices=["start", "resume", "pause", "cancel", "generate", "refresh"]
    )
    research_run.add_argument("job_id")
    research_report = research_sub.add_parser("report")
    research_report.add_argument("job_id")
    research_show = research_sub.add_parser("show")
    research_show.add_argument("job_id")
    research_plan = research_sub.add_parser("plan")
    research_plan.add_argument("job_id")
    research_plan.add_argument("sections", help="章节数组 JSON")
    research_plan.add_argument("--revision", type=int, required=True)
    research_auto = research_sub.add_parser("auto-plan")
    research_auto.add_argument("job_id")
    research_auto.add_argument("--revision", type=int, required=True)
    research_auto.add_argument("--local", action="store_true")
    research_provenance = research_sub.add_parser("provenance")
    research_provenance.add_argument("job_id")
    research_review = research_sub.add_parser("review")
    research_review.add_argument("job_id")
    research_review.add_argument("decisions", help="审核决定数组 JSON")
    research_review.add_argument("--revision", type=int, required=True)
    research_publish = research_sub.add_parser("publish")
    research_publish.add_argument("job_id")
    research_publish.add_argument("--revision", type=int, required=True)
    for action in ("published-report", "published-bundle"):
        item = research_sub.add_parser(action)
        item.add_argument("job_id")

    integration = sub.add_parser(
        "integration", aliases=["connector"], help="连接器与外部同步"
    )
    int_sub = integration.add_subparsers(dest="integration_action", required=True)
    int_list = int_sub.add_parser("list")
    _add_kb_argument(int_list)
    int_create = int_sub.add_parser("create")
    _add_kb_argument(int_create)
    int_create.add_argument("body", help="连接器 JSON")
    int_update = int_sub.add_parser("update")
    int_update.add_argument("connection_id")
    int_update.add_argument("body", help="连接器更新 JSON")
    _add_kb_argument(int_update)
    int_delete = int_sub.add_parser("delete")
    int_delete.add_argument("connection_id")
    int_delete.add_argument("--yes", action="store_true")
    _add_kb_argument(int_delete)
    int_sync = int_sub.add_parser("sync")
    int_sync.add_argument("connection_id")
    _add_kb_argument(int_sync)
    int_jobs = int_sub.add_parser("jobs")
    _add_kb_argument(int_jobs)
    int_enabled = int_sub.add_parser("enabled")
    int_enabled.add_argument("connection_id")
    int_enabled.add_argument("value", choices=["true", "false"])
    _add_kb_argument(int_enabled)
    int_job = int_sub.add_parser("job")
    int_job.add_argument("job_id")
    _add_kb_argument(int_job)
    int_replay = int_sub.add_parser("replay")
    int_replay.add_argument("job_id")
    _add_kb_argument(int_replay)
    int_health = int_sub.add_parser("health")
    int_health.add_argument("connection_id", nargs="?")
    _add_kb_argument(int_health)
    int_sources = int_sub.add_parser("sources")
    int_sources.add_argument("--connection-id")
    int_sources.add_argument("--include-deleted", action="store_true")
    _add_kb_argument(int_sources)
    int_source = int_sub.add_parser("source")
    int_source.add_argument("source_id")
    _add_kb_argument(int_source)
    int_versions = int_sub.add_parser("versions")
    int_versions.add_argument("source_id")
    _add_kb_argument(int_versions)
    int_diff = int_sub.add_parser("diff")
    int_diff.add_argument("source_id")
    int_diff.add_argument("from_version_id")
    int_diff.add_argument("to_version_id")
    _add_kb_argument(int_diff)
    int_usage = int_sub.add_parser("artifact-usage")
    _add_kb_argument(int_usage)
    int_restore = int_sub.add_parser("artifact-restore")
    int_restore.add_argument("recovery_token")
    _add_kb_argument(int_restore)
    int_oauth = int_sub.add_parser("oauth")
    int_oauth.add_argument("provider")
    int_oauth.add_argument("--connection-id")
    _add_kb_argument(int_oauth)
    int_credentials = int_sub.add_parser("credentials")
    _add_kb_argument(int_credentials)
    int_credential_create = int_sub.add_parser("credential-create")
    int_credential_create.add_argument("body", help="凭据 JSON")
    _add_kb_argument(int_credential_create)
    int_credential_rotate = int_sub.add_parser("credential-rotate")
    int_credential_rotate.add_argument("credential_id")
    int_credential_rotate.add_argument("body", help="新的 secret_values JSON")
    int_credential_rotate.add_argument("--revision", type=int)
    _add_kb_argument(int_credential_rotate)
    int_credential_refresh = int_sub.add_parser("credential-refresh")
    int_credential_refresh.add_argument("credential_id")
    int_credential_refresh.add_argument("--revision", type=int)
    _add_kb_argument(int_credential_refresh)
    int_credential_delete = int_sub.add_parser("credential-delete")
    int_credential_delete.add_argument("credential_id")
    int_credential_delete.add_argument("--revision", type=int)
    int_credential_delete.add_argument("--yes", action="store_true")
    _add_kb_argument(int_credential_delete)
    int_credential_events = int_sub.add_parser("credential-events")
    int_credential_events.add_argument("--credential-id")
    _add_kb_argument(int_credential_events)
    int_download = int_sub.add_parser("version-download")
    int_download.add_argument("source_id")
    int_download.add_argument("version_id")
    int_download.add_argument("--output", type=Path, required=True)
    _add_kb_argument(int_download)
    int_artifact_delete = int_sub.add_parser("artifact-delete")
    int_artifact_delete.add_argument("source_id")
    int_artifact_delete.add_argument("version_id")
    int_artifact_delete.add_argument("--yes", action="store_true")
    _add_kb_argument(int_artifact_delete)

    traces = sub.add_parser("trace", help="Trace 调试")
    trace_sub = traces.add_subparsers(dest="trace_action", required=True)
    trace_list = trace_sub.add_parser("list")
    _add_kb_argument(trace_list)
    trace_list.add_argument("--limit", type=int, default=20)
    trace_show = trace_sub.add_parser("show")
    trace_show.add_argument("trace_id")

    diagnose = sub.add_parser("diagnose", help="运行检索诊断")
    diagnose.add_argument("query", nargs="+")
    _add_kb_argument(diagnose)
    diagnose.add_argument("--top-k", type=int, default=8)

    evaluation = sub.add_parser(
        "evaluation", aliases=["eval"], help="RAG 评测与声明核验"
    )
    eval_sub = evaluation.add_subparsers(dest="evaluation_action", required=True)
    eval_retrieval = eval_sub.add_parser("retrieval")
    _add_kb_argument(eval_retrieval)
    eval_retrieval.add_argument("--status")
    eval_claims = eval_sub.add_parser("claims")
    _add_kb_argument(eval_claims)
    eval_claims.add_argument("--status")
    eval_summary = eval_sub.add_parser("claim-summary")
    _add_kb_argument(eval_summary)
    eval_retrieval_show = eval_sub.add_parser("retrieval-show")
    eval_retrieval_show.add_argument("draft_id")
    eval_candidates = eval_sub.add_parser("retrieval-candidates")
    eval_candidates.add_argument("draft_id")
    eval_candidates.add_argument("--top-k", type=int, default=12)
    eval_review = eval_sub.add_parser("retrieval-review")
    eval_review.add_argument("draft_id")
    eval_review.add_argument("decision")
    eval_review.add_argument("--revision", type=int, required=True)
    eval_review.add_argument("--annotations", default="{}")
    eval_review.add_argument("--reason", default="")
    eval_export = eval_sub.add_parser("retrieval-export")
    eval_export.add_argument("--partition", default="release_gate")
    eval_claim_show = eval_sub.add_parser("claim-show")
    eval_claim_show.add_argument("review_id")
    eval_claim_label = eval_sub.add_parser("claim-label")
    eval_claim_label.add_argument("review_id")
    eval_claim_label.add_argument("verdict")
    eval_claim_label.add_argument("--revision", type=int, required=True)
    eval_claim_label.add_argument("--note", default="")
    eval_sub.add_parser("claim-export")

    audit = sub.add_parser("audit", help="审计导出")
    audit_sub = audit.add_subparsers(dest="audit_action", required=True)
    audit_list = audit_sub.add_parser("list")
    audit_list.add_argument("--limit", type=int, default=100)
    audit_create = audit_sub.add_parser("create")
    audit_create.add_argument("--from-sequence", type=int)
    audit_create.add_argument("--to-sequence", type=int)
    audit_create.add_argument("--actions")
    audit_create.add_argument("--statuses")
    audit_create.add_argument("--retention-seconds", type=int, default=86400)
    for action in ("show", "download", "delete"):
        item = audit_sub.add_parser(action)
        item.add_argument("job_id")
        if action == "download":
            item.add_argument("--output", type=Path, required=True)
        if action == "delete":
            item.add_argument("--revision", type=int, required=True)
            item.add_argument("--yes", action="store_true")

    security = sub.add_parser("security", help="Workspace 安全策略与会话")
    security_sub = security.add_subparsers(dest="security_action", required=True)
    for action in ("sessions", "session-policy", "oidc-policy", "scim"):
        item = security_sub.add_parser(action)
        item.add_argument("--workspace-id")
        if action == "sessions":
            item.add_argument("--include-inactive", action="store_true")
    security_revoke = security_sub.add_parser("revoke-session")
    security_revoke.add_argument("session_id")
    security_revoke.add_argument("--workspace-id")
    for action in ("session-policy-set", "oidc-policy-set"):
        item = security_sub.add_parser(action)
        item.add_argument("body", help="完整策略 JSON")
        item.add_argument("--workspace-id")

    service = sub.add_parser("service-account", aliases=["sa"], help="服务账号")
    service_sub = service.add_subparsers(dest="service_action", required=True)
    for action in ("list", "policy"):
        item = service_sub.add_parser(action)
        item.add_argument("--workspace-id")
    service_create = service_sub.add_parser("create")
    service_create.add_argument("name")
    service_create.add_argument("--description", default="")
    service_create.add_argument("--role", default="viewer")
    service_create.add_argument("--workspace-id")
    service_update = service_sub.add_parser("update")
    service_update.add_argument("account_id")
    service_update.add_argument("body", help="完整更新 JSON")
    service_update.add_argument("--workspace-id")
    service_delete = service_sub.add_parser("delete")
    service_delete.add_argument("account_id")
    service_delete.add_argument("--revision", type=int, required=True)
    service_delete.add_argument("--workspace-id")
    service_delete.add_argument("--yes", action="store_true")
    service_tokens = service_sub.add_parser("tokens")
    service_tokens.add_argument("account_id")
    service_tokens.add_argument("--workspace-id")
    service_token_create = service_sub.add_parser("create-token")
    service_token_create.add_argument("account_id")
    service_token_create.add_argument("label")
    service_token_create.add_argument("--days", type=int, default=90)
    service_token_create.add_argument("--permissions")
    service_token_create.add_argument("--workspace-id")
    service_token_revoke = service_sub.add_parser("revoke-token")
    service_token_revoke.add_argument("account_id")
    service_token_revoke.add_argument("token_id")
    service_token_revoke.add_argument("--revision", type=int, required=True)
    service_token_revoke.add_argument("--workspace-id")
    service_token_revoke.add_argument("--yes", action="store_true")
    service_policy_set = service_sub.add_parser("policy-set")
    service_policy_set.add_argument("body", help="完整策略 JSON")
    service_policy_set.add_argument("--workspace-id")

    migration = sub.add_parser("migration", help="索引代际迁移")
    migration.add_argument(
        "action", choices=["scan", "start", "status", "rollback", "finalize"]
    )
    migration.add_argument("values", nargs="*")

    raw = sub.add_parser("api", help="调用尚未封装的 /v1 API（企业管理/自动化）")
    raw.add_argument("method", choices=["GET", "POST", "PUT", "PATCH", "DELETE"])
    raw.add_argument("path")
    raw.add_argument("--body", help="JSON 请求体")
    raw.add_argument(
        "--param", action="append", default=[], help="查询参数 key=value，可重复"
    )
    return parser


class APICommandRunner:
    def __init__(self, args: argparse.Namespace):
        path = args.config_path or default_config_path()
        self.state = CLIState(path)
        self.api_url = (
            args.api_url
            or os.getenv("COGDOC_API_URL")
            or self.state.get("api_url")
            or DEFAULT_API_URL
        ).rstrip("/")
        self.token = (
            args.token
            or os.getenv("COGDOC_API_KEY")
            or self.state.get("access_token")
            or ""
        )
        self.workspace_id = args.workspace or self.state.get("workspace_id")
        self.compact = bool(args.compact)

    @property
    def client(self) -> CogDocClient:
        return CogDocClient(
            self.api_url,
            api_key=self.token,
            workspace_id=self.workspace_id,
        )

    def require_workspace(self, explicit: str | None = None) -> str:
        value = explicit or self.workspace_id
        if not value:
            raise ValueError("请先登录或执行 `cogdoc workspace use <ID>`")
        return str(value)

    def require_kb(self, explicit: str | None = None) -> str:
        value = explicit or self.state.get("kb_id")
        if not value:
            raise ValueError("请通过 --kb 指定知识库，或先执行 `cogdoc kb use <ID>`")
        return str(value)

    def upload_role_ids(self, explicit: str | None) -> list[str]:
        """Mirror Web defaults: all visible roles, never an accidental empty ACL."""

        if explicit is not None:
            selected = csv_values(explicit)
        elif not self.workspace_id:
            config = checked(self.client.auth_config())
            if not isinstance(config, Mapping) or config.get("account_auth_enabled") is not False:
                raise ValueError("请先登录或执行 `cogdoc workspace use <ID>`")
            selected = list(BUILT_IN_ROLE_IDS)
        else:
            payload = checked(self.client.list_workspace_roles(self.workspace_id))
            rows = payload.get("roles", []) if isinstance(payload, Mapping) else []
            selected = [
                str(row.get("role_id"))
                for row in rows
                if isinstance(row, Mapping) and row.get("role_id")
            ]
        if not selected:
            raise ValueError("请至少选择一个可访问角色")
        return selected

    @staticmethod
    def confirm(enabled: bool, message: str) -> None:
        if enabled:
            return
        if (
            not sys.stdin.isatty()
            or input(f"{message}，输入 yes 确认: ").strip() != "yes"
        ):
            raise ValueError("操作已取消；自动化调用请显式传 --yes")

    def emit(self, payload: Any) -> None:
        print_payload(payload, compact=self.compact)

    @staticmethod
    def inbox_files(directory: Path | None = None) -> list[Path]:
        root = (directory or Path(get_settings().cogdoc_doc_dir)).expanduser()
        if not root.exists():
            return []
        if not root.is_dir():
            raise ValueError(f"文档收件箱不是目录: {root}")
        return sorted(
            path
            for path in root.iterdir()
            if path.is_file() and path.suffix.casefold() in SUPPORTED_EXTENSIONS
        )

    def _configure_model(self, args: argparse.Namespace) -> int:
        settings = get_settings()
        base_url = args.base_url
        model = args.model
        if base_url is None:
            current = str(settings.llm_base_url or "")
            base_url = input(f"Base URL [{current}]: ").strip() or current
        if model is None:
            current = str(settings.llm_model_name or "")
            model = input(f"模型 [{current}]: ").strip() or current
        entered = getpass.getpass("API Key（留空保留当前值）: ").strip()
        api_key = entered or None
        apply_llm_config(api_key=api_key, base_url=base_url, model=model)
        self.emit(
            {
                "updated": True,
                "base_url": base_url,
                "model": model,
                "api_key_updated": api_key is not None,
                "restart_required": True,
            }
        )
        return 0

    def run(self, args: argparse.Namespace) -> int:
        command = args.command
        if command == "login":
            payload = checked(
                self.client.login(args.email, _password(), args.workspace_id)
            )
            if not isinstance(payload, Mapping):
                raise CogDocAPIError("登录响应格式不符合预期")
            workspace = (
                payload.get("workspace")
                if isinstance(payload.get("workspace"), Mapping)
                else {}
            )
            user = (
                payload.get("user") if isinstance(payload.get("user"), Mapping) else {}
            )
            self.token = str(payload.get("access_token") or "")
            self.workspace_id = str(workspace.get("workspace_id") or "") or None
            self.state.replace_session(
                api_url=self.api_url,
                access_token=self.token,
                expires_at=payload.get("expires_at"),
                workspace_id=self.workspace_id,
                email=user.get("email", args.email),
            )
            self.emit(
                {
                    "user": user,
                    "workspace": workspace,
                    "expires_at": payload.get("expires_at"),
                }
            )
            return 0
        if command == "register":
            payload = checked(
                self.client.register(
                    args.email,
                    _password(confirm=True),
                    args.display_name,
                    args.workspace_name,
                )
            )
            if not isinstance(payload, Mapping):
                raise CogDocAPIError("注册响应格式不符合预期")
            workspace = (
                payload.get("workspace")
                if isinstance(payload.get("workspace"), Mapping)
                else {}
            )
            self.token = str(payload.get("access_token") or "")
            self.workspace_id = str(workspace.get("workspace_id") or "") or None
            self.state.replace_session(
                api_url=self.api_url,
                access_token=self.token,
                expires_at=payload.get("expires_at"),
                workspace_id=self.workspace_id,
                email=args.email,
            )
            self.emit({"user": payload.get("user"), "workspace": workspace})
            return 0
        if command == "logout":
            if self.token:
                try:
                    checked(self.client.logout())
                except CogDocAPIError as exc:
                    if exc.status_code != 401:
                        raise
            self.state.clear_session()
            print("已注销，CLI 登录态已清除。")
            return 0
        if command == "status":
            public_client = CogDocClient(self.api_url, timeout=10.0, api_key="")
            config = checked(public_client.get_auth_config())
            result: dict[str, Any] = {"api_url": self.api_url, "auth": config}
            if self.token:
                status_client = CogDocClient(
                    self.api_url,
                    timeout=10.0,
                    api_key=self.token,
                    workspace_id=self.workspace_id,
                )
                result["session"] = checked(status_client.get_me())
            else:
                result["session"] = None
            self.emit(result)
            return 0
        if command == "inbox":
            paths = self.inbox_files(args.directory)
            self.emit(
                {
                    "directory": str(
                        (args.directory or Path(get_settings().cogdoc_doc_dir))
                        .expanduser()
                        .resolve()
                    ),
                    "files": [path.name for path in paths],
                }
            )
            return 0
        if command == "config":
            return self._configure_model(args)
        if command == "auth":
            return self._auth(args)
        if command in {"workspace", "ws"}:
            return self._workspace(args)
        if command == "acl":
            return self._acl(args)
        if command == "kb":
            return self._kb(args)
        if command in {"document", "doc"}:
            return self._document(args)
        if command == "jobs":
            if args.cancel:
                self.emit(checked(self.client.cancel_ha_job(args.cancel)))
                return 0
            if args.replay:
                if not args.replay_key:
                    raise ValueError("重放系统任务必须提供 --replay-key")
                self.emit(
                    checked(self.client.replay_ha_job(args.replay, args.replay_key))
                )
                return 0
            kb_id = args.kb or None
            result: dict[str, Any] = {}
            unavailable: dict[str, dict[str, Any]] = {}

            def load(name: str, operation: Callable[[], httpx.Response]) -> None:
                try:
                    result[name] = checked(operation())
                except (CogDocAPIError, httpx.HTTPError) as exc:
                    # Match the Web task center: preserve every available task
                    # source when an optional control plane is unavailable.
                    unavailable[name] = {
                        "status": getattr(exc, "status_code", None),
                        "error": str(exc),
                    }

            if args.kind in {"all", "index"}:
                load(
                    "index_jobs",
                    lambda: self.client.list_index_jobs(kb_id, limit=args.limit),
                )
            if args.kind in {"all", "sync"}:
                load(
                    "sync_jobs",
                    lambda: self.client.list_workspace_sync_jobs(limit=args.limit),
                )
            if args.kind in {"all", "system"}:
                load("system_jobs", lambda: self.client.list_ha_jobs(limit=args.limit))
            if unavailable:
                result["unavailable"] = unavailable
            self.emit(result)
            return 0 if result.keys() - {"unavailable"} else 1
        if command == "chat":
            return self._chat(args)
        if command == "sessions":
            return self._sessions(args)
        if command in {"knowledge", "dk"}:
            return self._knowledge(args)
        if command == "feedback":
            return self._feedback(args)
        if command == "research":
            return self._research(args)
        if command in {"integration", "connector"}:
            return self._integration(args)
        if command == "trace":
            response = (
                self.client.get_trace(args.trace_id)
                if args.trace_action == "show"
                else self.client.list_traces(limit=args.limit, kb_id=args.kb)
            )
            self.emit(checked(response))
            return 0
        if command == "diagnose":
            kb_id = self.require_kb(args.kb)
            self.emit(
                checked(
                    self.client.diagnose_retrieval(
                        kb_id, " ".join(args.query), top_k=args.top_k
                    )
                )
            )
            return 0
        if command in {"evaluation", "eval"}:
            action = args.evaluation_action
            if action == "retrieval":
                payload = checked(
                    self.client.list_retrieval_eval_drafts(
                        kb_id=self.require_kb(args.kb), status=args.status
                    )
                )
            elif action == "claims":
                payload = checked(
                    self.client.list_claim_verification_reviews(
                        status=args.status, kb_id=self.require_kb(args.kb)
                    )
                )
            elif action == "claim-summary":
                payload = checked(
                    self.client.claim_verification_review_summary(
                        self.require_kb(args.kb)
                    )
                )
            elif action == "retrieval-show":
                payload = checked(self.client.get_retrieval_eval_draft(args.draft_id))
            elif action == "retrieval-candidates":
                payload = checked(
                    self.client.get_retrieval_eval_candidates(
                        args.draft_id, top_k=args.top_k
                    )
                )
            elif action == "retrieval-review":
                payload = checked(
                    self.client.review_retrieval_eval_draft(
                        args.draft_id,
                        decision=args.decision,
                        expected_revision=args.revision,
                        annotations=json_object(args.annotations, "annotations"),
                        reason=args.reason,
                    )
                )
            elif action == "retrieval-export":
                payload = checked(
                    self.client.export_retrieval_eval_drafts(
                        dataset_partition=args.partition
                    )
                )
            elif action == "claim-show":
                payload = checked(
                    self.client.get_claim_verification_review(args.review_id)
                )
            elif action == "claim-label":
                payload = checked(
                    self.client.label_claim_verification_review(
                        args.review_id,
                        expected_verdict=args.verdict,
                        expected_revision=args.revision,
                        review_note=args.note,
                    )
                )
            else:
                payload = checked(self.client.export_claim_verification_reviews())
            self.emit(payload)
            return 0
        if command == "audit":
            return self._audit(args)
        if command == "security":
            return self._security(args)
        if command in {"service-account", "sa"}:
            return self._service_account(args)
        if command == "migration":
            action = args.action
            if action == "scan":
                payload = checked(self.client.scan_index_migrations())
            elif action == "start":
                if not args.values:
                    raise ValueError("start 需要至少一个知识库 ID")
                payload = checked(self.client.start_index_migration(args.values))
            else:
                if len(args.values) != 1:
                    raise ValueError(f"{action} 需要一个迁移 run_id")
                run_id = args.values[0]
                if action == "status":
                    payload = checked(self.client.get_index_migration(run_id))
                elif action == "rollback":
                    payload = checked(self.client.rollback_index_migration(run_id))
                else:
                    payload = checked(self.client.finalize_index_migration(run_id))
            self.emit(payload)
            return 0
        if command == "api":
            params = {}
            for raw in args.param:
                if "=" not in raw:
                    raise ValueError("--param 必须是 key=value")
                key, value = raw.split("=", 1)
                params[key] = value
            body = json.loads(args.body) if args.body else None
            self.emit(
                checked(
                    self.client.request(
                        args.method, args.path, params=params, json_body=body
                    )
                )
            )
            return 0
        raise ValueError("缺少命令")

    def _auth(self, args: argparse.Namespace) -> int:
        action = args.auth_action
        if action == "change-password":
            current = getpass.getpass("当前密码: ")
            new_password = getpass.getpass("新密码: ")
            if new_password != getpass.getpass("再次输入新密码: "):
                raise ValueError("两次输入的新密码不一致")
            payload = checked(self.client.change_password(current, new_password))
        elif action == "logout-all":
            payload = checked(self.client.logout_all())
            self.state.clear_session()
        elif action == "sessions":
            payload = checked(self.client.list_auth_sessions())
        elif action == "revoke-session":
            payload = checked(self.client.delete_auth_session(args.session_id))
        elif action == "oidc-login":
            payload = checked(
                self.client.begin_oidc_login(args.return_url, args.workspace_id)
            )
        elif action == "oidc-exchange":
            payload = checked(self.client.exchange_oidc_handoff(args.code))
            if isinstance(payload, Mapping) and isinstance(
                payload.get("session"), Mapping
            ):
                session = payload["session"]
                workspace = (
                    session.get("workspace")
                    if isinstance(session.get("workspace"), Mapping)
                    else {}
                )
                self.state.replace_session(
                    api_url=self.api_url,
                    access_token=session.get("access_token"),
                    expires_at=session.get("expires_at"),
                    workspace_id=workspace.get("workspace_id"),
                )
        elif action == "oidc-link":
            payload = checked(self.client.begin_oidc_link(args.return_url))
        elif action == "oidc-identities":
            payload = checked(self.client.list_oidc_identities())
        elif action == "oidc-unlink":
            payload = checked(self.client.unlink_oidc_identity(args.identity_id))
        elif action == "accept-invite":
            payload = checked(
                self.client.accept_workspace_invite(
                    args.token,
                    email=args.email,
                    password=_password(confirm=True) if args.email else None,
                    display_name=args.display_name,
                )
            )
            if isinstance(payload, Mapping):
                workspace = (
                    payload.get("workspace")
                    if isinstance(payload.get("workspace"), Mapping)
                    else {}
                )
                if payload.get("access_token"):
                    self.state.replace_session(
                        api_url=self.api_url,
                        access_token=payload.get("access_token"),
                        expires_at=payload.get("expires_at"),
                        workspace_id=workspace.get("workspace_id"),
                        email=args.email,
                    )
        else:
            raise ValueError("不支持的账号操作")
        self.emit(payload)
        return 0

    def _acl(self, args: argparse.Namespace) -> int:
        action = args.acl_action
        kb_id = self.require_kb(args.kb)
        if action == "kb-grants":
            payload = checked(self.client.list_kb_grants(kb_id))
        elif action == "kb-grant":
            payload = checked(
                self.client.grant_kb_access(kb_id, args.subject_id, args.role)
            )
        elif action == "kb-revoke":
            payload = checked(self.client.revoke_kb_access(kb_id, args.subject_id))
        elif action == "document-grants":
            payload = checked(self.client.list_document_grants(kb_id, args.document_id))
        elif action == "document-grant":
            payload = checked(
                self.client.grant_document_access(
                    kb_id, args.document_id, args.subject_id, args.role
                )
            )
        elif action == "document-revoke":
            payload = checked(
                self.client.revoke_document_access(
                    kb_id, args.document_id, args.subject_id
                )
            )
        else:
            raise ValueError("不支持的 ACL 操作")
        self.emit(payload)
        return 0

    def _workspace(self, args: argparse.Namespace) -> int:
        action = args.workspace_action
        if action == "list":
            payload = checked(self.client.list_workspaces())
        elif action == "use":
            payload = checked(self.client.switch_workspace(args.workspace_id))
            if not isinstance(payload, Mapping):
                raise CogDocAPIError("Workspace 切换响应格式不符合预期")
            workspace = (
                payload.get("workspace")
                if isinstance(payload.get("workspace"), Mapping)
                else {}
            )
            self.workspace_id = str(workspace.get("workspace_id") or args.workspace_id)
            new_token = str(payload.get("access_token") or self.token)
            self.token = new_token
            self.state.values.pop("kb_id", None)
            self.state.values.pop("session_id", None)
            self.state.update(
                workspace_id=self.workspace_id,
                access_token=new_token,
                expires_at=payload.get("expires_at"),
            )
        elif action == "create":
            payload = checked(self.client.create_workspace(args.name))
        elif action == "get":
            payload = checked(
                self.client.get_workspace(self.require_workspace(args.workspace_id))
            )
        elif action == "update":
            workspace_id = self.require_workspace(args.workspace_id)
            payload = checked(
                self.client.update_workspace(
                    workspace_id, args.name, expected_revision=args.revision
                )
            )
        elif action == "delete":
            workspace_id = self.require_workspace(args.workspace_id)
            self.confirm(args.yes, f"确定删除 Workspace {workspace_id} 吗")
            payload = checked(self.client.delete_workspace(workspace_id))
            if workspace_id == self.workspace_id:
                self.state.clear_session()
        else:
            workspace_id = self.require_workspace(getattr(args, "workspace_id", None))
            if action == "members":
                payload = checked(self.client.list_workspace_members(workspace_id))
            elif action == "roles":
                payload = checked(self.client.list_workspace_roles(workspace_id))
            elif action == "create-role":
                payload = checked(
                    self.client.create_workspace_role(
                        workspace_id,
                        name=args.name,
                        description=args.description,
                        base_role=args.base_role,
                    )
                )
            elif action == "invite":
                payload = checked(
                    self.client.create_workspace_invite(
                        workspace_id, args.email, args.role
                    )
                )
            elif action == "invites":
                payload = checked(self.client.list_workspace_invites(workspace_id))
            elif action == "revoke-invite":
                payload = checked(
                    self.client.revoke_workspace_invite(workspace_id, args.invite_id)
                )
            elif action == "assign-role":
                payload = checked(
                    self.client.assign_workspace_member_role(
                        workspace_id,
                        args.member_id,
                        args.role_id,
                        expected_revision=args.revision,
                    )
                )
            elif action == "remove-member":
                self.confirm(args.yes, f"确定移除成员 {args.member_id} 吗")
                payload = checked(
                    self.client.remove_workspace_member(workspace_id, args.member_id)
                )
            elif action == "delete-role":
                self.confirm(args.yes, f"确定删除自定义角色 {args.role_id} 吗")
                payload = checked(
                    self.client.delete_workspace_role(workspace_id, args.role_id)
                )
            else:
                raise ValueError("不支持的 Workspace 操作")
        self.emit(payload)
        return 0

    def _kb(self, args: argparse.Namespace) -> int:
        action = args.kb_action
        if action == "list":
            payload = self.client.list_knowledge_bases()
        elif action == "use":
            ids = {str(row.get("kb_id")) for row in self.client.list_knowledge_bases()}
            if args.kb_id not in ids:
                raise ValueError(f"当前 Workspace 不存在或无权访问知识库: {args.kb_id}")
            self.state.values.pop("session_id", None)
            self.state.update(kb_id=args.kb_id)
            payload = {"kb_id": args.kb_id, "selected": True}
        elif action == "create":
            payload = checked(
                self.client.create_knowledge_base(
                    args.kb_id,
                    access_policy=args.access,
                    role_ids=self.upload_role_ids(args.roles),
                )
            )
            self.state.values.pop("session_id", None)
            self.state.update(kb_id=args.kb_id)
        elif action == "delete":
            self.confirm(args.yes, f"确定永久删除知识库 {args.kb_id} 及关联数据吗")
            payload = checked(self.client.delete_knowledge_base(args.kb_id))
            if self.state.get("kb_id") == args.kb_id:
                self.state.values.pop("kb_id", None)
                self.state.values.pop("session_id", None)
                self.state.save()
        elif action == "embedding-profiles":
            payload = self.client.list_embedding_profiles()
        elif action == "access":
            kb_id = self.require_kb(args.kb_id)
            payload = (
                checked(
                    self.client.update_kb_access_policy(
                        kb_id,
                        args.set,
                        csv_values(args.roles) if args.roles is not None else None,
                    )
                )
                if args.set
                else checked(self.client.get_kb_access_policy(kb_id))
            )
        else:
            raise ValueError("不支持的知识库操作")
        self.emit(payload)
        return 0

    def _document(self, args: argparse.Namespace) -> int:
        kb_id = self.require_kb(args.kb)
        if args.document_action == "list":
            payload = checked(self.client.list_documents(kb_id))
        elif args.document_action == "upload":
            paths = [Path(value).expanduser() for value in args.files]
            missing = [str(path) for path in paths if not path.is_file()]
            if missing:
                raise ValueError(f"文件不存在: {', '.join(missing)}")
            documents = [(path.name, path.read_bytes()) for path in paths]
            payload = checked(
                self.client.upload_documents(
                    kb_id,
                    documents,
                    allowed_role_ids=self.upload_role_ids(args.roles),
                    embedding_profile_id=args.embedding,
                )
            )
        elif args.document_action == "delete":
            self.confirm(args.yes, f"确定删除文档 {args.name} 吗")
            payload = checked(self.client.delete_document(kb_id, args.name))
        elif args.document_action == "access":
            payload = (
                checked(
                    self.client.update_document_access_policy(
                        kb_id,
                        args.document_id,
                        args.set,
                        source=args.source,
                        role_ids=(
                            csv_values(args.roles) if args.roles is not None else None
                        ),
                    )
                )
                if args.set
                else checked(
                    self.client.get_document_access_policy(kb_id, args.document_id)
                )
            )
        else:
            raise ValueError("不支持的文档操作")
        self.emit(payload)
        return 0

    def _chat(self, args: argparse.Namespace) -> int:
        kb_id = self.require_kb(args.kb)
        token_seen = False
        final: Mapping[str, Any] | None = None
        for event, data in self.client.stream_chat(
            kb_id,
            " ".join(args.query),
            mode=args.mode,
            session_id=args.session,
            is_local=args.local,
        ):
            if event == "token":
                content = str(data.get("content") or "")
                if content:
                    print(content, end="", flush=True)
                    token_seen = True
            elif event == "error":
                if token_seen:
                    print()
                raise CogDocAPIError(format_api_error(data))
            elif event == "final":
                final = data
        if token_seen:
            print()
        if final is None:
            raise CogDocAPIError("流式回答在完成事件前中断")
        if final is not None:
            if not token_seen:
                print(str(final.get("answer") or ""))
            citations = final.get("citations")
            if citations:
                print_payload(
                    {
                        "citations": citations,
                        "trace_id": final.get("trace_id"),
                        "session_id": final.get("session_id"),
                    },
                    compact=self.compact,
                )
        return 0

    def _sessions(self, args: argparse.Namespace) -> int:
        kb_id = self.require_kb(args.kb)
        if args.action == "list":
            payload = checked(self.client.list_sessions(kb_id))
        elif args.action == "history":
            if not args.session_id:
                raise ValueError("history 需要 session_id")
            payload = checked(self.client.get_session_history(args.session_id, kb_id))
        else:
            if not args.session_id:
                raise ValueError("delete 需要 session_id")
            self.confirm(args.yes, f"确定删除会话 {args.session_id} 吗")
            payload = checked(self.client.delete_session(args.session_id, kb_id))
        self.emit(payload)
        return 0

    def _knowledge(self, args: argparse.Namespace) -> int:
        action = args.knowledge_action
        if action == "list":
            payload = checked(
                self.client.list_knowledge(self.require_kb(args.kb), status=args.status)
            )
        elif action == "show":
            rows = checked(self.client.list_knowledge(self.require_kb(args.kb)))
            values = rows if isinstance(rows, list) else rows.get("knowledge", [])
            payload = next(
                (
                    row
                    for row in values
                    if isinstance(row, Mapping)
                    and str(row.get("knowledge_id") or row.get("id") or "")
                    == args.knowledge_id
                ),
                None,
            )
            if payload is None:
                raise ValueError(f"找不到派生知识: {args.knowledge_id}")
        elif action == "summary":
            payload = checked(
                self.client.review_queue_summary(self.require_kb(args.kb))
            )
        elif action == "create":
            payload = checked(
                self.client.create_knowledge(
                    kb_id=self.require_kb(args.kb),
                    text=" ".join(args.text),
                    related_document_id=args.document_id,
                    related_source=args.source,
                    related_source_sha256=args.source_sha256,
                    related_chunk_ids=csv_values(args.chunks),
                    certainty=args.certainty,
                    origin=args.origin,
                    created_from_trace_id=args.trace_id,
                )
            )
        elif action == "revise":
            payload = checked(
                self.client.revise_knowledge(
                    args.knowledge_id,
                    text=" ".join(args.text),
                    related_document_id=args.document_id,
                    related_source=args.source,
                    related_source_sha256=args.source_sha256,
                    related_chunk_ids=csv_values(args.chunks),
                    certainty=args.certainty,
                    created_from_trace_id=args.trace_id,
                )
            )
        elif action == "review":
            payload = checked(
                self.client.review_knowledge(
                    args.knowledge_id, args.action, note=args.note
                )
            )
        elif action == "batch":
            payload = checked(
                self.client.batch_review_knowledge(
                    args.knowledge_ids, args.action, note=args.note
                )
            )
        elif action == "delete":
            self.confirm(args.yes, f"确定删除派生知识 {args.knowledge_id} 吗")
            payload = checked(self.client.delete_knowledge(args.knowledge_id))
        elif action == "scan":
            payload = checked(
                self.client.scan_stale_knowledge(self.require_kb(args.kb))
            )
        elif action == "status":
            payload = checked(
                self.client.knowledge_index_status(self.require_kb(args.kb))
            )
        else:
            raise ValueError("不支持的派生知识操作")
        self.emit(payload)
        return 0

    def _feedback(self, args: argparse.Namespace) -> int:
        kb_id = self.require_kb(args.kb)
        action = args.action
        if action == "submit":
            if not args.trace_id or not args.value:
                raise ValueError("submit 需要 --trace-id 和 --value")
            if args.value == "correction" and not args.correction:
                raise ValueError("correction 反馈需要 --correction")
            payload = checked(
                self.client.submit_feedback(
                    args.trace_id,
                    args.value,
                    kb_id=kb_id,
                    query=args.query,
                    answer=args.answer,
                    feedback_type=args.issue,
                    feedback_text=args.text,
                    correction_text=args.correction,
                    related_source=args.source,
                    related_source_sha256=args.source_sha256,
                    related_chunk_ids=csv_values(args.chunks),
                )
            )
        elif action == "list":
            payload = checked(self.client.list_feedback(kb_id))
        elif action == "analysis":
            payload = checked(self.client.list_feedback_analysis(kb_id))
        elif action == "tuning":
            payload = checked(self.client.list_retrieval_feedback(kb_id))
        elif action in {"enable", "disable"}:
            if not args.item_id:
                raise ValueError(f"{action} 需要检索反馈 ID")
            payload = checked(
                self.client.set_retrieval_feedback_enabled(
                    args.item_id,
                    action == "enable",
                    reason="CLI 人工调整检索反馈状态",
                )
            )
        elif action == "metrics":
            payload = checked(self.client.feedback_loop_metrics(kb_id))
        else:
            payload = checked(self.client.review_queue_export(kb_id, limit=500))
        self.emit(payload)
        return 0

    def _research(self, args: argparse.Namespace) -> int:
        action = args.research_action
        if action == "list":
            payload = checked(self.client.list_research_jobs(self.require_kb(args.kb)))
        elif action == "create":
            payload = checked(
                self.client.create_research_job(
                    self.require_kb(args.kb),
                    " ".join(args.goal),
                    title=args.title,
                    is_local=args.local,
                )
            )
        elif action == "action":
            payload = checked(self.client.research_action(args.job_id, args.action))
        elif action == "report":
            payload = checked(self.client.get_research_report(args.job_id))
        elif action == "show":
            payload = checked(self.client.get_research_job(args.job_id))
        elif action == "plan":
            sections = json_list(args.sections, "章节")
            if not all(isinstance(item, dict) for item in sections):
                raise ValueError("章节数组中的每一项都必须是对象")
            payload = checked(
                self.client.update_research_plan(
                    args.job_id,
                    expected_revision=args.revision,
                    sections=sections,
                )
            )
        elif action == "auto-plan":
            payload = checked(
                self.client.generate_research_plan(
                    args.job_id,
                    expected_revision=args.revision,
                    is_local=True if args.local else None,
                )
            )
        elif action == "provenance":
            payload = checked(self.client.get_research_provenance(args.job_id))
        elif action == "review":
            decisions = json_list(args.decisions, "审核决定")
            if not all(isinstance(item, dict) for item in decisions):
                raise ValueError("审核决定数组中的每一项都必须是对象")
            payload = checked(
                self.client.review_research_report(
                    args.job_id,
                    expected_revision=args.revision,
                    decisions=decisions,
                )
            )
        elif action == "publish":
            payload = checked(
                self.client.publish_research_report(
                    args.job_id, expected_revision=args.revision
                )
            )
        elif action == "published-report":
            payload = checked(self.client.get_published_research_report(args.job_id))
        elif action == "published-bundle":
            payload = checked(self.client.get_published_research_bundle(args.job_id))
        else:
            raise ValueError("不支持的 Research 操作")
        self.emit(payload)
        return 0

    def _integration(self, args: argparse.Namespace) -> int:
        action = args.integration_action
        kb_id = self.require_kb(args.kb)
        if action == "list":
            payload = checked(self.client.list_connections(kb_id))
        elif action == "create":
            body = json_object(args.body, "连接器 JSON")
            payload = checked(self.client.create_connection(kb_id, body))
        elif action == "update":
            payload = checked(
                self.client.update_connection(
                    kb_id,
                    args.connection_id,
                    json_object(args.body, "连接器更新 JSON"),
                )
            )
        elif action == "delete":
            self.confirm(args.yes, f"确定删除连接器 {args.connection_id} 吗")
            payload = checked(self.client.delete_connection(kb_id, args.connection_id))
        elif action == "sync":
            payload = checked(
                self.client.start_connection_sync(kb_id, args.connection_id)
            )
        elif action == "jobs":
            payload = checked(self.client.list_sync_jobs(kb_id))
        elif action == "enabled":
            payload = checked(
                self.client.set_connection_enabled(
                    kb_id, args.connection_id, args.value == "true"
                )
            )
        elif action == "job":
            payload = checked(self.client.get_sync_job(kb_id, args.job_id))
        elif action == "replay":
            payload = checked(self.client.replay_sync_job(kb_id, args.job_id))
        elif action == "health":
            payload = checked(
                self.client.get_connection_health(kb_id, args.connection_id)
                if args.connection_id
                else self.client.list_connection_health(kb_id)
            )
        elif action == "sources":
            payload = checked(
                self.client.list_source_catalog(
                    kb_id,
                    connection_id=args.connection_id,
                    include_deleted=args.include_deleted,
                )
            )
        elif action == "source":
            payload = checked(
                self.client.get_source_catalog_entry(kb_id, args.source_id)
            )
        elif action == "versions":
            payload = checked(self.client.list_source_versions(kb_id, args.source_id))
        elif action == "diff":
            payload = checked(
                self.client.diff_source_versions(
                    kb_id,
                    args.source_id,
                    args.from_version_id,
                    args.to_version_id,
                )
            )
        elif action == "artifact-usage":
            payload = checked(self.client.get_source_artifact_usage(kb_id))
        elif action == "artifact-restore":
            payload = checked(
                self.client.restore_source_artifact(kb_id, args.recovery_token)
            )
        elif action == "oauth":
            payload = checked(
                self.client.authorize_connector_oauth(
                    kb_id, args.provider, connection_id=args.connection_id
                )
            )
        elif action == "credentials":
            payload = checked(self.client.list_connector_credentials(kb_id))
        elif action == "credential-create":
            payload = checked(
                self.client.create_connector_credential(
                    kb_id, json_object(args.body, "凭据 JSON")
                )
            )
        elif action == "credential-rotate":
            secrets = json_object(args.body, "secret_values JSON")
            if not all(isinstance(value, str) for value in secrets.values()):
                raise ValueError("secret_values 的值必须都是字符串")
            payload = checked(
                self.client.rotate_connector_credential(
                    kb_id,
                    args.credential_id,
                    secret_values=secrets,
                    expected_revision=args.revision,
                )
            )
        elif action == "credential-refresh":
            payload = checked(
                self.client.refresh_connector_credential(
                    kb_id,
                    args.credential_id,
                    expected_revision=args.revision,
                )
            )
        elif action == "credential-delete":
            self.confirm(args.yes, f"确定删除凭据 {args.credential_id} 吗")
            payload = checked(
                self.client.delete_connector_credential(
                    kb_id,
                    args.credential_id,
                    expected_revision=args.revision,
                )
            )
        elif action == "credential-events":
            payload = checked(
                self.client.list_connector_credential_events(
                    kb_id, credential_id=args.credential_id
                )
            )
        elif action == "version-download":
            response = self.client.download_source_version(
                kb_id, args.source_id, args.version_id
            )
            if response.status_code >= 400:
                checked(response)
            output = args.output.expanduser().resolve()
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(response.content)
            payload = {"output": str(output), "bytes": len(response.content)}
        elif action == "artifact-delete":
            self.confirm(args.yes, f"确定删除来源版本 {args.version_id} 的原始文件吗")
            payload = checked(
                self.client.delete_source_artifact(
                    kb_id, args.source_id, args.version_id
                )
            )
        else:
            raise ValueError("不支持的连接器操作")
        self.emit(payload)
        return 0

    def _audit(self, args: argparse.Namespace) -> int:
        action = args.audit_action
        if action == "list":
            payload = checked(self.client.list_audit_exports(limit=args.limit))
        elif action == "create":
            statuses = [int(value) for value in csv_values(args.statuses)]
            payload = checked(
                self.client.create_audit_export(
                    from_sequence=args.from_sequence,
                    to_sequence=args.to_sequence,
                    actions=csv_values(args.actions),
                    statuses=statuses,
                    retention_seconds=args.retention_seconds,
                )
            )
        elif action == "show":
            payload = checked(self.client.get_audit_export(args.job_id))
        elif action == "download":
            response = self.client.download_audit_export(args.job_id)
            if response.status_code >= 400:
                checked(response)
            output = args.output.expanduser().resolve()
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(response.content)
            payload = {"output": str(output), "bytes": len(response.content)}
        elif action == "delete":
            self.confirm(args.yes, f"确定删除审计导出 {args.job_id} 吗")
            payload = checked(
                self.client.delete_audit_export(args.job_id, args.revision)
            )
        else:
            raise ValueError("不支持的审计操作")
        self.emit(payload)
        return 0

    def _security(self, args: argparse.Namespace) -> int:
        action = args.security_action
        workspace_id = self.require_workspace(args.workspace_id)
        if action == "sessions":
            payload = checked(
                self.client.list_workspace_security_sessions(
                    workspace_id, include_inactive=args.include_inactive
                )
            )
        elif action == "revoke-session":
            payload = checked(
                self.client.revoke_workspace_security_session(
                    workspace_id, args.session_id
                )
            )
        elif action == "session-policy":
            payload = checked(self.client.get_workspace_session_policy(workspace_id))
        elif action == "session-policy-set":
            body = json_object(args.body, "会话策略 JSON")
            payload = checked(
                self.client.update_workspace_session_policy(
                    workspace_id,
                    idle_timeout_minutes=body.get("idle_timeout_minutes"),
                    absolute_timeout_hours=body.get("absolute_timeout_hours"),
                    max_active_sessions=body.get("max_active_sessions"),
                    expected_revision=int(required_value(body, "expected_revision")),
                )
            )
        elif action == "oidc-policy":
            payload = checked(self.client.get_workspace_oidc_policy(workspace_id))
        elif action == "oidc-policy-set":
            body = json_object(args.body, "OIDC 策略 JSON")
            payload = checked(
                self.client.update_workspace_oidc_policy(
                    workspace_id,
                    allowed_domains=[
                        str(value) for value in body.get("allowed_domains", [])
                    ],
                    default_role=str(body.get("default_role") or "viewer"),
                    enabled=boolean_value(body, "enabled"),
                    group_claim=str(body.get("group_claim") or "groups"),
                    group_role_map={
                        str(key): str(value)
                        for key, value in dict(body.get("group_role_map") or {}).items()
                    },
                    require_mapped_group=boolean_value(
                        body, "require_mapped_group", default=False
                    ),
                    expected_revision=(
                        int(body["expected_revision"])
                        if body.get("expected_revision") is not None
                        else None
                    ),
                )
            )
        elif action == "scim":
            payload = checked(self.client.get_workspace_scim_status(workspace_id))
        else:
            raise ValueError("不支持的安全操作")
        self.emit(payload)
        return 0

    def _service_account(self, args: argparse.Namespace) -> int:
        action = args.service_action
        workspace_id = self.require_workspace(args.workspace_id)
        if action == "list":
            payload = checked(self.client.list_service_accounts(workspace_id))
        elif action == "create":
            payload = checked(
                self.client.create_service_account(
                    workspace_id,
                    name=args.name,
                    description=args.description,
                    role=args.role,
                )
            )
        elif action == "update":
            body = json_object(args.body, "服务账号更新 JSON")
            payload = checked(
                self.client.update_service_account(
                    workspace_id,
                    args.account_id,
                    name=str(required_value(body, "name")),
                    description=str(body.get("description") or ""),
                    role=str(required_value(body, "role")),
                    active=boolean_value(body, "active", default=True),
                    expected_revision=int(required_value(body, "expected_revision")),
                )
            )
        elif action == "delete":
            self.confirm(args.yes, f"确定删除服务账号 {args.account_id} 吗")
            payload = checked(
                self.client.delete_service_account(
                    workspace_id, args.account_id, args.revision
                )
            )
        elif action == "tokens":
            payload = checked(
                self.client.list_service_tokens(workspace_id, args.account_id)
            )
        elif action == "create-token":
            payload = checked(
                self.client.create_service_token(
                    workspace_id,
                    args.account_id,
                    label=args.label,
                    expires_in_days=args.days,
                    permissions=csv_values(args.permissions),
                )
            )
        elif action == "revoke-token":
            self.confirm(args.yes, f"确定撤销服务令牌 {args.token_id} 吗")
            payload = checked(
                self.client.revoke_service_token(
                    workspace_id,
                    args.account_id,
                    args.token_id,
                    args.revision,
                )
            )
        elif action == "policy":
            payload = checked(self.client.get_service_account_policy(workspace_id))
        elif action == "policy-set":
            body = json_object(args.body, "服务账号策略 JSON")
            payload = checked(
                self.client.update_service_account_policy(
                    workspace_id,
                    max_accounts=int(required_value(body, "max_accounts")),
                    max_tokens_per_account=int(
                        required_value(body, "max_tokens_per_account")
                    ),
                    max_token_ttl_days=int(required_value(body, "max_token_ttl_days")),
                    allow_non_expiring=boolean_value(
                        body, "allow_non_expiring", default=False
                    ),
                    allowed_permissions=[
                        str(value) for value in body.get("allowed_permissions", [])
                    ],
                    expected_revision=int(required_value(body, "expected_revision")),
                )
            )
        else:
            raise ValueError("不支持的服务账号操作")
        self.emit(payload)
        return 0


def _interactive(base_args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    print(BANNER)
    print("=" * 60)
    print("🚀 CogDoc 控制台 | Workspace + 多知识库 + 多对话")
    print("输入 /help 查看命令，直接输入文本向当前知识库提问。")
    print("=" * 60)
    while True:
        state = CLIState(base_args.config_path or default_config_path())
        context = (
            "/".join(filter(None, [state.get("workspace_id"), state.get("kb_id")]))
            or "未登录"
        )
        try:
            line = input(f"cogdoc[{context}]> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not line:
            continue
        if line in {"exit", "quit", "/exit", "/quit"}:
            return 0
        if line in {"help", "/help"}:
            print(INTERACTIVE_HELP)
            continue
        try:
            values = shlex.split(line)
            if values and values[0].startswith("/"):
                values[0] = values[0][1:]
            if values == ["new"]:
                session_id = uuid4().hex
                state.update(session_id=session_id)
                print(f"🆕 已开启新对话（{session_id[:8]}）。")
                continue
            if values in (["local"], ["cloud"]):
                is_local = values[0] == "local"
                state.update(is_local=is_local)
                print("已切换到本地模型。" if is_local else "已切换到云端模型。")
                continue
            was_inbox_add = values[:1] == ["add"]
            values = _normalize_interactive_values(values)
            if was_inbox_add:
                runner = APICommandRunner(base_args)
                files = runner.inbox_files()
                if not files:
                    print("文档收件箱里没有受支持的文件。")
                    continue
                if len(values) == 2:
                    values.extend(str(path) for path in files)
                else:
                    by_name = {path.name: path for path in files}
                    values = [
                        *values[:2],
                        *[
                            str(
                                path
                                if path.is_file()
                                else by_name.get(path.name, path)
                            )
                            for path in (Path(value).expanduser() for value in values[2:])
                        ],
                    ]
            known_commands = {
                "login",
                "register",
                "logout",
                "status",
                "inbox",
                "config",
                "auth",
                "workspace",
                "ws",
                "acl",
                "kb",
                "document",
                "doc",
                "jobs",
                "chat",
                "sessions",
                "knowledge",
                "dk",
                "feedback",
                "research",
                "integration",
                "connector",
                "trace",
                "diagnose",
                "evaluation",
                "eval",
                "audit",
                "security",
                "service-account",
                "sa",
                "migration",
                "api",
            }
            if values and values[0] not in known_commands:
                values = ["chat", *values]
            if values[:1] == ["chat"]:
                if "--session" not in values:
                    session_id = str(state.get("session_id") or uuid4().hex)
                    state.update(session_id=session_id)
                    values.extend(["--session", session_id])
                if state.get("is_local") is True and "--local" not in values:
                    values.append("--local")
            inherited: list[str] = []
            if base_args.api_url:
                inherited.extend(["--api-url", base_args.api_url])
            if base_args.token:
                inherited.extend(["--token", base_args.token])
            if base_args.workspace:
                inherited.extend(["--workspace", base_args.workspace])
            if base_args.config_path:
                inherited.extend(["--config-path", str(base_args.config_path)])
            if base_args.compact:
                inherited.append("--compact")
            parsed = parser.parse_args([*inherited, *values])
            APICommandRunner(parsed).run(parsed)
        except KeyboardInterrupt:
            print()
            return 0
        except SystemExit:
            continue
        except httpx.HTTPError as exc:
            print(f"错误: 无法连接 CogDoc API：{exc}", file=sys.stderr)
        except (
            CogDocAPIError,
            OSError,
            ValueError,
            TypeError,
            KeyError,
            json.JSONDecodeError,
        ) as exc:
            print(f"错误: {exc}", file=sys.stderr)


def _normalize_interactive_values(values: list[str]) -> list[str]:
    """Preserve the original slash-console command muscle memory."""

    if not values:
        return values
    if values == ["kb"]:
        return ["kb", "list"]
    if values[:2] == ["kb", "new"]:
        return ["kb", "create", *values[2:]]
    if values[:2] == ["kb", "rm"]:
        return ["kb", "delete", *values[2:]]
    if values == ["dk"] or values == ["knowledge"]:
        return ["knowledge", "list"]
    if values[:2] in (["dk", "add"], ["knowledge", "add"]):
        return ["knowledge", "create", *values[2:]]
    if len(values) >= 2 and values[0] in {"dk", "knowledge"}:
        action = values[1]
        if action in {"approve", "reject", "archive"}:
            return ["knowledge", "review", action, *values[2:]]
        if action in {"batch-approve", "batch-reject"}:
            return ["knowledge", "batch", action, *values[2:]]
        if action == "stale-scan":
            return ["knowledge", "scan", *values[2:]]
    if values[:1] == ["tuning"]:
        action = values[1] if len(values) > 1 else "list"
        mapped = "tuning" if action == "list" else action
        return ["feedback", mapped, *values[2:]]
    if values[:2] == ["review", "summary"]:
        return ["knowledge", "summary", *values[2:]]
    if values[:2] == ["review", "metrics"]:
        return ["feedback", "metrics", *values[2:]]
    if values[:2] == ["review", "export"]:
        return ["feedback", "review-export", *values[2:]]
    aliases = {
        "docs": ["document", "list"],
        "ls": ["document", "list"],
        "add": ["document", "upload"],
        "rm": ["document", "delete"],
        "chats": ["sessions", "list"],
        "open": ["sessions", "history"],
        "rmchat": ["sessions", "delete"],
    }
    if values[0] in aliases:
        return [*aliases[values[0]], *values[1:]]
    if values[0] == "qa":
        return ["chat", *values[1:], "--mode", "qa"]
    if values[0] == "summary":
        return ["chat", "总结文档", *values[1:], "--mode", "summary"]
    if values[0] == "compare":
        return ["chat", "对比文档", *values[1:], "--mode", "compare"]
    return values


def main(argv: Sequence[str] | None = None) -> int:
    values = list(sys.argv[1:] if argv is None else argv)
    if values[:1] in (["--local-storage"], ["local-storage"]):
        from cogdoc.cli import local_main

        return int(local_main() or 0)
    parser = build_parser()
    args = parser.parse_args(values)
    if args.command is None:
        return _interactive(args, parser)
    try:
        return APICommandRunner(args).run(args)
    except httpx.HTTPError as exc:
        print(f"错误: 无法连接 CogDoc API：{exc}", file=sys.stderr)
        return 1
    except (
        CogDocAPIError,
        OSError,
        ValueError,
        TypeError,
        KeyError,
        json.JSONDecodeError,
    ) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
