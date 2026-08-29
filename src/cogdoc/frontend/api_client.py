import hashlib
import json
import mimetypes
import os
from collections.abc import Mapping
from typing import Any, Callable, Iterable, Iterator
from urllib.parse import quote, unquote

import httpx

DEFAULT_TIMEOUT = 180.0
WORKSPACE_HEADER = "X-CogDoc-Workspace"


def _path_segment(value: Any) -> str:
    """Encode one opaque identifier for use as exactly one URL path segment."""

    return quote(str(value), safe="")


def _canonical_workspace_id(value: str | None) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 160
        or any(ord(character) < 33 or ord(character) == 127 for character in value)
    ):
        raise ValueError("workspace_id must be an exact canonical identifier")
    return value


# 定义接口错误。
class CogDocAPIError(RuntimeError):
    # 后端返回结构化错误体或非预期响应时抛出，供界面直接展示。
    def __init__(
        self, message: str, status_code: int | None = None, payload: Any = None
    ):
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload


# 格式化接口错误。
def format_api_error(
    payload: Any, status_code: int | None = None, fallback: str = "请求失败"
) -> str:
    status = f"HTTP {status_code}: " if status_code is not None else ""
    if isinstance(payload, Mapping):
        code = payload.get("error_code")
        message = payload.get("message")
        if code and message:
            return f"{status}[{code}] {message}"
        if message:
            return f"{status}{message}"
    if payload not in (None, ""):
        return f"{status}{fallback}: {payload}"
    return f"{status}{fallback}"


# 处理响应载荷。
def response_payload(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return response.text[:200]


# 处理响应对象。
def _response_json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError as exc:
        raise CogDocAPIError(
            f"HTTP {response.status_code}: 后端返回非 JSON 响应: {response.text[:200]}",
            status_code=response.status_code,
        ) from exc


# 处理已校验响应对象。
def _checked_json(response: httpx.Response) -> Any:
    payload = _response_json(response)
    if response.status_code >= 400:
        raise CogDocAPIError(
            format_api_error(payload, response.status_code),
            status_code=response.status_code,
            payload=payload,
        )
    return payload


# 处理预期列表。
def _expect_list(payload: Any, label: str) -> list[dict]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, Mapping) and payload.get("error_code"):
        raise CogDocAPIError(format_api_error(payload), payload=payload)
    raise CogDocAPIError(f"{label}响应格式不符合预期: {payload}")


# 解析流式事件列表。
def iter_sse_events(lines: Iterable[str]) -> Iterator[tuple[str, dict]]:
    # 把行流解析成事件名和数据；空行结束一帧，非法数据跳过。
    event_name = "message"
    for line in lines:
        if not line:
            event_name = "message"
            continue
        if line.startswith("event:"):
            event_name = line[len("event:") :].strip()
        elif line.startswith("data:"):
            raw = line[len("data:") :].strip()
            try:
                yield event_name, json.loads(raw)
            except json.JSONDecodeError:
                continue


# 交付层瘦客户端：只打版本接口，不碰后端智能逻辑。
class CogDocClient:
    # 交付层瘦客户端：只打版本接口，不碰后端智能逻辑。
    def __init__(
        self,
        base_url: str,
        timeout: float = DEFAULT_TIMEOUT,
        api_key: str | None = None,
        workspace_id: str | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        # 后端开启鉴权时带上密钥；缺省读环境变量，未配置则不带头。
        key = api_key if api_key is not None else os.getenv("COGDOC_API_KEY", "")
        self._headers = {"Authorization": f"Bearer {key}"} if key else {}
        self.workspace_id: str | None = None
        self.set_workspace(workspace_id)
        # Session caches must not cross authentication identities, while the
        # credential itself must never become part of a Streamlit cache key.
        self.auth_cache_identity = (
            hashlib.sha256(key.encode("utf-8")).hexdigest() if key else "anonymous"
        )

    # 拼接结果。
    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def set_workspace(self, workspace_id: str | None) -> None:
        """Pin subsequent protected requests to one explicit workspace."""

        self.workspace_id = _canonical_workspace_id(workspace_id)
        # Replace rather than mutate the mapping: an in-flight httpx call or
        # observer must keep the exact tab selector it started with.
        headers = {
            key: value
            for key, value in self._headers.items()
            if key != WORKSPACE_HEADER
        }
        if self.workspace_id is not None:
            headers[WORKSPACE_HEADER] = self.workspace_id
        self._headers = headers

    def _headers_for_workspace(self, workspace_id: str) -> dict[str, str]:
        headers = dict(self._headers)
        headers[WORKSPACE_HEADER] = _canonical_workspace_id(workspace_id) or ""
        return headers

    def _remember_response_workspace(self, response: httpx.Response) -> httpx.Response:
        if not 200 <= response.status_code < 300:
            return response
        payload = response_payload(response)
        if not isinstance(payload, Mapping):
            return response
        workspace = payload.get("workspace")
        if not isinstance(workspace, Mapping):
            return response
        workspace_id = workspace.get("workspace_id")
        if isinstance(workspace_id, str):
            self.set_workspace(workspace_id)
        return response

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Any = None,
    ) -> httpx.Response:
        """Call a versioned endpoint for CLI/admin workflows not yet typed here."""

        normalized_method = method.strip().upper()
        if normalized_method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
            raise ValueError("unsupported HTTP method")
        normalized_path = path if path.startswith("/") else f"/{path}"
        decoded_path = normalized_path
        for _ in range(8):
            expanded = unquote(decoded_path)
            if expanded == decoded_path:
                break
            decoded_path = expanded
        else:
            raise ValueError("path encoding is too deeply nested")
        if (
            not decoded_path.startswith("/v1/")
            or any(segment in {".", ".."} for segment in decoded_path.split("/"))
            or "\\" in decoded_path
            or "?" in decoded_path
            or "#" in decoded_path
            or any(ord(character) < 32 or ord(character) == 127 for character in decoded_path)
        ):
            raise ValueError("path must start with /v1/")
        return httpx.request(
            normalized_method,
            self._url(normalized_path),
            params=dict(params or {}),
            json=json_body,
            timeout=self.timeout,
            headers=self._headers,
        )

    # 读取部署的账号认证能力；该端点始终公开且不会携带凭据也能调用。
    def get_auth_config(self) -> httpx.Response:
        return httpx.get(
            self._url("/v1/auth/config"),
            timeout=self.timeout,
            headers=self._headers,
        )

    def auth_config(self) -> httpx.Response:
        return self.get_auth_config()

    # 注册账号并创建个人工作区。
    def register(
        self,
        email: str,
        password: str,
        display_name: str,
        workspace_name: str | None = None,
    ) -> httpx.Response:
        payload = {
            "email": email,
            "password": password,
            "display_name": display_name,
            "workspace_name": workspace_name,
        }
        response = httpx.post(
            self._url("/v1/auth/register"),
            json={key: value for key, value in payload.items() if value is not None},
            timeout=self.timeout,
            headers=self._headers,
        )
        return self._remember_response_workspace(response)

    # 使用邮箱密码创建不落盘的 Bearer 会话。
    def login(
        self,
        email: str,
        password: str,
        workspace_id: str | None = None,
    ) -> httpx.Response:
        payload = {
            "email": email,
            "password": password,
            "workspace_id": workspace_id,
        }
        response = httpx.post(
            self._url("/v1/auth/login"),
            json={key: value for key, value in payload.items() if value is not None},
            timeout=self.timeout,
            headers=self._headers,
        )
        return self._remember_response_workspace(response)

    def begin_oidc_login(
        self, return_url: str, workspace_id: str | None = None
    ) -> httpx.Response:
        payload = {"return_url": return_url, "workspace_id": workspace_id}
        return httpx.post(
            self._url("/v1/auth/oidc/authorize"),
            json={key: value for key, value in payload.items() if value is not None},
            timeout=self.timeout,
            headers=self._headers,
        )

    def exchange_oidc_handoff(self, code: str) -> httpx.Response:
        response = httpx.post(
            self._url("/v1/auth/oidc/exchange"),
            json={"code": code},
            timeout=self.timeout,
            headers=self._headers,
        )
        if not 200 <= response.status_code < 300:
            return response
        payload = response_payload(response)
        if isinstance(payload, Mapping) and isinstance(payload.get("session"), Mapping):
            workspace = payload["session"].get("workspace")
            if isinstance(workspace, Mapping) and isinstance(
                workspace.get("workspace_id"), str
            ):
                self.set_workspace(str(workspace["workspace_id"]))
        return response

    def begin_oidc_link(self, return_url: str) -> httpx.Response:
        return httpx.post(
            self._url("/v1/auth/oidc/link/authorize"),
            json={"return_url": return_url},
            timeout=self.timeout,
            headers=self._headers,
        )

    def list_oidc_identities(self) -> httpx.Response:
        return httpx.get(
            self._url("/v1/auth/oidc/identities"),
            timeout=self.timeout,
            headers=self._headers,
        )

    def unlink_oidc_identity(self, identity_id: str) -> httpx.Response:
        return httpx.delete(
            self._url(f"/v1/auth/oidc/identities/{quote(identity_id, safe='')}"),
            timeout=self.timeout,
            headers=self._headers,
        )

    def get_workspace_oidc_policy(self, workspace_id: str) -> httpx.Response:
        return httpx.get(
            self._url(f"/v1/workspaces/{quote(workspace_id, safe='')}/oidc-policy"),
            timeout=self.timeout,
            headers=self._headers_for_workspace(workspace_id),
        )

    def update_workspace_oidc_policy(
        self,
        workspace_id: str,
        *,
        allowed_domains: list[str],
        default_role: str,
        enabled: bool,
        group_claim: str = "groups",
        group_role_map: dict[str, str] | None = None,
        require_mapped_group: bool = False,
        expected_revision: int | None = None,
    ) -> httpx.Response:
        payload = {
            "allowed_domains": allowed_domains,
            "default_role": default_role,
            "enabled": enabled,
            "group_claim": group_claim,
            "group_role_map": group_role_map or {},
            "require_mapped_group": require_mapped_group,
            "expected_revision": expected_revision,
        }
        return httpx.put(
            self._url(f"/v1/workspaces/{quote(workspace_id, safe='')}/oidc-policy"),
            json={key: value for key, value in payload.items() if value is not None},
            timeout=self.timeout,
            headers=self._headers_for_workspace(workspace_id),
        )

    def get_workspace_scim_status(self, workspace_id: str) -> httpx.Response:
        return httpx.get(
            self._url(f"/v1/workspaces/{quote(workspace_id, safe='')}/scim-status"),
            timeout=self.timeout,
            headers=self._headers_for_workspace(workspace_id),
        )

    def list_audit_exports(self, limit: int = 100) -> httpx.Response:
        return httpx.get(
            self._url("/v1/audit-events/exports"),
            params={"limit": limit},
            timeout=self.timeout,
            headers=self._headers,
        )

    def create_audit_export(
        self,
        *,
        from_sequence: int | None = None,
        to_sequence: int | None = None,
        actions: list[str] | None = None,
        statuses: list[int] | None = None,
        retention_seconds: int = 86_400,
    ) -> httpx.Response:
        return httpx.post(
            self._url("/v1/audit-events/exports"),
            json={
                "from_sequence": from_sequence,
                "to_sequence": to_sequence,
                "actions": actions or [],
                "statuses": statuses or [],
                "retention_seconds": retention_seconds,
            },
            timeout=self.timeout,
            headers=self._headers,
        )

    def get_audit_export(self, job_id: str) -> httpx.Response:
        return httpx.get(
            self._url(f"/v1/audit-events/exports/{quote(job_id, safe='')}"),
            timeout=self.timeout,
            headers=self._headers,
        )

    def download_audit_export(self, job_id: str) -> httpx.Response:
        return httpx.get(
            self._url(f"/v1/audit-events/exports/{quote(job_id, safe='')}/content"),
            timeout=self.timeout,
            headers=self._headers,
        )

    def delete_audit_export(
        self, job_id: str, expected_revision: int
    ) -> httpx.Response:
        return httpx.delete(
            self._url(f"/v1/audit-events/exports/{quote(job_id, safe='')}"),
            params={"expected_revision": expected_revision},
            timeout=self.timeout,
            headers=self._headers,
        )

    def list_service_accounts(self, workspace_id: str) -> httpx.Response:
        return httpx.get(
            self._url(
                f"/v1/workspaces/{quote(workspace_id, safe='')}/service-accounts"
            ),
            timeout=self.timeout,
            headers=self._headers_for_workspace(workspace_id),
        )

    def get_service_account_policy(self, workspace_id: str) -> httpx.Response:
        return httpx.get(
            self._url(
                f"/v1/workspaces/{quote(workspace_id, safe='')}/service-account-policy"
            ),
            timeout=self.timeout,
            headers=self._headers_for_workspace(workspace_id),
        )

    def update_service_account_policy(
        self,
        workspace_id: str,
        *,
        max_accounts: int,
        max_tokens_per_account: int,
        max_token_ttl_days: int,
        allow_non_expiring: bool,
        allowed_permissions: list[str],
        expected_revision: int,
    ) -> httpx.Response:
        return httpx.put(
            self._url(
                f"/v1/workspaces/{quote(workspace_id, safe='')}/service-account-policy"
            ),
            json={
                "max_accounts": max_accounts,
                "max_tokens_per_account": max_tokens_per_account,
                "max_token_ttl_days": max_token_ttl_days,
                "allow_non_expiring": allow_non_expiring,
                "allowed_permissions": allowed_permissions,
                "expected_revision": expected_revision,
            },
            timeout=self.timeout,
            headers=self._headers_for_workspace(workspace_id),
        )

    def create_service_account(
        self, workspace_id: str, *, name: str, description: str, role: str
    ) -> httpx.Response:
        return httpx.post(
            self._url(
                f"/v1/workspaces/{quote(workspace_id, safe='')}/service-accounts"
            ),
            json={"name": name, "description": description, "role": role},
            timeout=self.timeout,
            headers=self._headers_for_workspace(workspace_id),
        )

    def update_service_account(
        self,
        workspace_id: str,
        service_account_id: str,
        *,
        name: str,
        description: str,
        role: str,
        active: bool,
        expected_revision: int,
    ) -> httpx.Response:
        return httpx.patch(
            self._url(
                f"/v1/workspaces/{quote(workspace_id, safe='')}/service-accounts/"
                f"{quote(service_account_id, safe='')}"
            ),
            json={
                "name": name,
                "description": description,
                "role": role,
                "active": active,
                "expected_revision": expected_revision,
            },
            timeout=self.timeout,
            headers=self._headers_for_workspace(workspace_id),
        )

    def delete_service_account(
        self, workspace_id: str, service_account_id: str, expected_revision: int
    ) -> httpx.Response:
        return httpx.delete(
            self._url(
                f"/v1/workspaces/{quote(workspace_id, safe='')}/service-accounts/"
                f"{quote(service_account_id, safe='')}"
            ),
            params={"expected_revision": expected_revision},
            timeout=self.timeout,
            headers=self._headers_for_workspace(workspace_id),
        )

    def list_service_tokens(
        self, workspace_id: str, service_account_id: str
    ) -> httpx.Response:
        return httpx.get(
            self._url(
                f"/v1/workspaces/{quote(workspace_id, safe='')}/service-accounts/"
                f"{quote(service_account_id, safe='')}/tokens"
            ),
            timeout=self.timeout,
            headers=self._headers_for_workspace(workspace_id),
        )

    def create_service_token(
        self,
        workspace_id: str,
        service_account_id: str,
        *,
        label: str,
        expires_in_days: int | None = 90,
        permissions: list[str] | None = None,
    ) -> httpx.Response:
        return httpx.post(
            self._url(
                f"/v1/workspaces/{quote(workspace_id, safe='')}/service-accounts/"
                f"{quote(service_account_id, safe='')}/tokens"
            ),
            json={
                "label": label,
                "expires_in_days": expires_in_days,
                "permissions": permissions,
            },
            timeout=self.timeout,
            headers=self._headers_for_workspace(workspace_id),
        )

    def revoke_service_token(
        self,
        workspace_id: str,
        service_account_id: str,
        token_id: str,
        expected_revision: int,
    ) -> httpx.Response:
        return httpx.delete(
            self._url(
                f"/v1/workspaces/{quote(workspace_id, safe='')}/service-accounts/"
                f"{quote(service_account_id, safe='')}/tokens/"
                f"{quote(token_id, safe='')}"
            ),
            params={"expected_revision": expected_revision},
            timeout=self.timeout,
            headers=self._headers_for_workspace(workspace_id),
        )

    def logout(self) -> httpx.Response:
        return httpx.post(
            self._url("/v1/auth/logout"),
            timeout=self.timeout,
            headers=self._headers,
        )

    def logout_all(self) -> httpx.Response:
        return httpx.post(
            self._url("/v1/auth/logout-all"),
            timeout=self.timeout,
            headers=self._headers,
        )

    def change_password(
        self, current_password: str, new_password: str
    ) -> httpx.Response:
        return httpx.post(
            self._url("/v1/auth/change-password"),
            json={
                "current_password": current_password,
                "new_password": new_password,
            },
            timeout=self.timeout,
            headers=self._headers,
        )

    def get_me(self) -> httpx.Response:
        response = httpx.get(
            self._url("/v1/auth/me"),
            timeout=self.timeout,
            headers=self._headers,
        )
        return self._remember_response_workspace(response)

    def me(self) -> httpx.Response:
        return self.get_me()

    def list_auth_sessions(self) -> httpx.Response:
        return httpx.get(
            self._url("/v1/auth/sessions"),
            timeout=self.timeout,
            headers=self._headers,
        )

    def get_workspace_session_policy(self, workspace_id: str) -> httpx.Response:
        return httpx.get(
            self._url(f"/v1/workspaces/{quote(workspace_id, safe='')}/session-policy"),
            timeout=self.timeout,
            headers=self._headers_for_workspace(workspace_id),
        )

    def update_workspace_session_policy(
        self,
        workspace_id: str,
        *,
        idle_timeout_minutes: int | None,
        absolute_timeout_hours: int | None,
        max_active_sessions: int | None,
        expected_revision: int,
    ) -> httpx.Response:
        return httpx.put(
            self._url(f"/v1/workspaces/{quote(workspace_id, safe='')}/session-policy"),
            json={
                "idle_timeout_minutes": idle_timeout_minutes,
                "absolute_timeout_hours": absolute_timeout_hours,
                "max_active_sessions": max_active_sessions,
                "expected_revision": expected_revision,
            },
            timeout=self.timeout,
            headers=self._headers_for_workspace(workspace_id),
        )

    def list_workspace_security_sessions(
        self,
        workspace_id: str,
        *,
        limit: int = 50,
        before_session_id: str | None = None,
        include_inactive: bool = False,
    ) -> httpx.Response:
        params = {"limit": limit, "include_inactive": str(include_inactive).lower()}
        if before_session_id is not None:
            params["before_session_id"] = before_session_id
        return httpx.get(
            self._url(
                f"/v1/workspaces/{quote(workspace_id, safe='')}/security-sessions"
            ),
            params=params,
            timeout=self.timeout,
            headers=self._headers_for_workspace(workspace_id),
        )

    def revoke_workspace_security_session(
        self, workspace_id: str, session_id: str
    ) -> httpx.Response:
        return httpx.delete(
            self._url(
                f"/v1/workspaces/{quote(workspace_id, safe='')}/security-sessions/"
                f"{quote(session_id, safe='')}"
            ),
            timeout=self.timeout,
            headers=self._headers_for_workspace(workspace_id),
        )

    def delete_auth_session(self, session_id: str) -> httpx.Response:
        return httpx.delete(
            self._url(f"/v1/auth/sessions/{_path_segment(session_id)}"),
            timeout=self.timeout,
            headers=self._headers,
        )

    # 工作区和成员管理。
    def list_workspaces(self) -> httpx.Response:
        return httpx.get(
            self._url("/v1/workspaces"),
            timeout=self.timeout,
            headers=self._headers,
        )

    def get_workspace(self, workspace_id: str) -> httpx.Response:
        return httpx.get(
            self._url(f"/v1/workspaces/{_path_segment(workspace_id)}"),
            timeout=self.timeout,
            headers=self._headers_for_workspace(workspace_id),
        )

    def create_workspace(self, name: str) -> httpx.Response:
        return httpx.post(
            self._url("/v1/workspaces"),
            json={"name": name},
            timeout=self.timeout,
            headers=self._headers,
        )

    def update_workspace(
        self,
        workspace_id: str,
        name: str,
        expected_revision: int | None = None,
    ) -> httpx.Response:
        payload = {"name": name, "expected_revision": expected_revision}
        return httpx.patch(
            self._url(f"/v1/workspaces/{_path_segment(workspace_id)}"),
            json={key: value for key, value in payload.items() if value is not None},
            timeout=self.timeout,
            headers=self._headers_for_workspace(workspace_id),
        )

    def delete_workspace(self, workspace_id: str) -> httpx.Response:
        return httpx.delete(
            self._url(f"/v1/workspaces/{_path_segment(workspace_id)}"),
            timeout=self.timeout,
            headers=self._headers_for_workspace(workspace_id),
        )

    def switch_workspace(self, workspace_id: str) -> httpx.Response:
        response = httpx.post(
            self._url(f"/v1/workspaces/{_path_segment(workspace_id)}/switch"),
            timeout=self.timeout,
            headers=self._headers_for_workspace(workspace_id),
        )
        return self._remember_response_workspace(response)

    def list_workspace_members(self, workspace_id: str) -> httpx.Response:
        return httpx.get(
            self._url(f"/v1/workspaces/{_path_segment(workspace_id)}/members"),
            timeout=self.timeout,
            headers=self._headers_for_workspace(workspace_id),
        )

    def list_workspace_roles(self, workspace_id: str) -> httpx.Response:
        return httpx.get(
            self._url(f"/v1/workspaces/{_path_segment(workspace_id)}/roles"),
            timeout=self.timeout,
            headers=self._headers_for_workspace(workspace_id),
        )

    def create_workspace_role(
        self,
        workspace_id: str,
        name: str,
        base_role: str,
        description: str = "",
    ) -> httpx.Response:
        return httpx.post(
            self._url(f"/v1/workspaces/{_path_segment(workspace_id)}/roles"),
            json={
                "name": name,
                "base_role": base_role,
                "description": description,
            },
            timeout=self.timeout,
            headers=self._headers_for_workspace(workspace_id),
        )

    def delete_workspace_role(self, workspace_id: str, role_id: str) -> httpx.Response:
        return httpx.delete(
            self._url(
                f"/v1/workspaces/{_path_segment(workspace_id)}/roles/"
                f"{_path_segment(role_id)}"
            ),
            timeout=self.timeout,
            headers=self._headers_for_workspace(workspace_id),
        )

    def assign_workspace_member_role(
        self,
        workspace_id: str,
        member_id: str,
        role_id: str,
        expected_revision: int | None = None,
    ) -> httpx.Response:
        payload = {"role_id": role_id, "expected_revision": expected_revision}
        return httpx.patch(
            self._url(
                f"/v1/workspaces/{_path_segment(workspace_id)}/members/"
                f"{_path_segment(member_id)}"
            ),
            json={key: value for key, value in payload.items() if value is not None},
            timeout=self.timeout,
            headers=self._headers_for_workspace(workspace_id),
        )

    def update_workspace_member(
        self,
        workspace_id: str,
        member_id: str,
        role: str,
        expected_revision: int | None = None,
    ) -> httpx.Response:
        payload = {"role": role, "expected_revision": expected_revision}
        return httpx.patch(
            self._url(
                f"/v1/workspaces/{_path_segment(workspace_id)}/members/"
                f"{_path_segment(member_id)}"
            ),
            json={key: value for key, value in payload.items() if value is not None},
            timeout=self.timeout,
            headers=self._headers_for_workspace(workspace_id),
        )

    def remove_workspace_member(
        self, workspace_id: str, member_id: str
    ) -> httpx.Response:
        return httpx.delete(
            self._url(
                f"/v1/workspaces/{_path_segment(workspace_id)}/members/"
                f"{_path_segment(member_id)}"
            ),
            timeout=self.timeout,
            headers=self._headers_for_workspace(workspace_id),
        )

    def create_workspace_invite(
        self, workspace_id: str, email: str, role: str
    ) -> httpx.Response:
        return httpx.post(
            self._url(f"/v1/workspaces/{_path_segment(workspace_id)}/invites"),
            json={"email": email, "role": role},
            timeout=self.timeout,
            headers=self._headers_for_workspace(workspace_id),
        )

    def list_workspace_invites(self, workspace_id: str) -> httpx.Response:
        return httpx.get(
            self._url(f"/v1/workspaces/{_path_segment(workspace_id)}/invites"),
            timeout=self.timeout,
            headers=self._headers_for_workspace(workspace_id),
        )

    def revoke_workspace_invite(
        self, workspace_id: str, invite_id: str
    ) -> httpx.Response:
        return httpx.delete(
            self._url(
                f"/v1/workspaces/{_path_segment(workspace_id)}/invites/"
                f"{_path_segment(invite_id)}"
            ),
            timeout=self.timeout,
            headers=self._headers_for_workspace(workspace_id),
        )

    def accept_workspace_invite(
        self,
        token: str,
        *,
        email: str | None = None,
        password: str | None = None,
        display_name: str | None = None,
    ) -> httpx.Response:
        payload = {
            "token": token,
            "email": email,
            "password": password,
            "display_name": display_name,
        }
        response = httpx.post(
            self._url("/v1/auth/invitations/accept"),
            json={key: value for key, value in payload.items() if value is not None},
            timeout=self.timeout,
            headers=self._headers,
        )
        return self._remember_response_workspace(response)

    def accept_invite(
        self,
        token: str,
        *,
        email: str | None = None,
        password: str | None = None,
        display_name: str | None = None,
    ) -> httpx.Response:
        return self.accept_workspace_invite(
            token,
            email=email,
            password=password,
            display_name=display_name,
        )

    # 知识库和文档的可见性策略与按主体授权。
    def get_kb_access_policy(self, kb_id: str) -> httpx.Response:
        return httpx.get(
            self._url(f"/v1/knowledge-bases/{_path_segment(kb_id)}/access"),
            timeout=self.timeout,
            headers=self._headers,
        )

    def update_kb_access_policy(
        self, kb_id: str, policy: str, role_ids: list[str] | None = None
    ) -> httpx.Response:
        payload: dict[str, Any] = {"schema_version": "v1", "policy": policy}
        if role_ids is not None:
            payload["role_ids"] = role_ids
        return httpx.patch(
            self._url(f"/v1/knowledge-bases/{_path_segment(kb_id)}/access"),
            json=payload,
            timeout=self.timeout,
            headers=self._headers,
        )

    def get_document_access_policy(
        self, kb_id: str, document_id: str
    ) -> httpx.Response:
        return httpx.get(
            self._url(
                f"/v1/knowledge-bases/{_path_segment(kb_id)}/documents/"
                f"{_path_segment(document_id)}/access"
            ),
            timeout=self.timeout,
            headers=self._headers,
        )

    def update_document_access_policy(
        self,
        kb_id: str,
        document_id: str,
        policy: str,
        *,
        source: str | None = None,
        role_ids: list[str] | None = None,
    ) -> httpx.Response:
        payload = {"schema_version": "v1", "policy": policy, "source": source}
        if role_ids is not None:
            payload["role_ids"] = role_ids
        return httpx.patch(
            self._url(
                f"/v1/knowledge-bases/{_path_segment(kb_id)}/documents/"
                f"{_path_segment(document_id)}/access"
            ),
            json={key: value for key, value in payload.items() if value is not None},
            timeout=self.timeout,
            headers=self._headers,
        )

    def list_kb_grants(self, kb_id: str) -> httpx.Response:
        return httpx.get(
            self._url(f"/v1/knowledge-bases/{_path_segment(kb_id)}/access/grants"),
            timeout=self.timeout,
            headers=self._headers,
        )

    def grant_kb_access(self, kb_id: str, subject_id: str, role: str) -> httpx.Response:
        return httpx.post(
            self._url(f"/v1/knowledge-bases/{_path_segment(kb_id)}/access/grants"),
            json={
                "schema_version": "v1",
                "subject_id": subject_id,
                "role": role,
            },
            timeout=self.timeout,
            headers=self._headers,
        )

    def revoke_kb_access(self, kb_id: str, subject_id: str) -> httpx.Response:
        return httpx.delete(
            self._url(
                f"/v1/knowledge-bases/{_path_segment(kb_id)}/access/grants/"
                f"{_path_segment(subject_id)}"
            ),
            timeout=self.timeout,
            headers=self._headers,
        )

    def list_document_grants(self, kb_id: str, document_id: str) -> httpx.Response:
        return httpx.get(
            self._url(
                f"/v1/knowledge-bases/{_path_segment(kb_id)}/documents/"
                f"{_path_segment(document_id)}/access/grants"
            ),
            timeout=self.timeout,
            headers=self._headers,
        )

    def grant_document_access(
        self,
        kb_id: str,
        document_id: str,
        subject_id: str,
        role: str,
    ) -> httpx.Response:
        return httpx.post(
            self._url(
                f"/v1/knowledge-bases/{_path_segment(kb_id)}/documents/"
                f"{_path_segment(document_id)}/access/grants"
            ),
            json={
                "schema_version": "v1",
                "subject_id": subject_id,
                "role": role,
            },
            timeout=self.timeout,
            headers=self._headers,
        )

    def revoke_document_access(
        self, kb_id: str, document_id: str, subject_id: str
    ) -> httpx.Response:
        return httpx.delete(
            self._url(
                f"/v1/knowledge-bases/{_path_segment(kb_id)}/documents/"
                f"{_path_segment(document_id)}/access/grants/"
                f"{_path_segment(subject_id)}"
            ),
            timeout=self.timeout,
            headers=self._headers,
        )

    # 列出知识库。
    def list_knowledge_bases(self) -> list[dict]:
        response = httpx.get(
            self._url("/v1/knowledge-bases"),
            timeout=self.timeout,
            headers=self._headers,
        )
        return _expect_list(_checked_json(response), "知识库列表")

    def list_embedding_profiles(self) -> list[dict]:
        """Return the same embedding choices exposed by the Web upload flow."""

        response = httpx.get(
            self._url("/v1/embedding-profiles"),
            timeout=self.timeout,
            headers=self._headers,
        )
        return _expect_list(_checked_json(response), "Embedding 配置列表")

    # 创建知识库。
    def create_knowledge_base(
        self,
        kb_id: str,
        *,
        access_policy: str = "workspace",
        role_ids: list[str] | None = None,
    ) -> httpx.Response:
        payload: dict[str, Any] = {
            "kb_id": kb_id,
            "access_policy": access_policy,
        }
        if role_ids is not None:
            payload["role_ids"] = role_ids
        return httpx.post(
            self._url("/v1/knowledge-bases"),
            json=payload,
            timeout=self.timeout,
            headers=self._headers,
        )

    # 删除知识库。
    def delete_knowledge_base(self, kb_id: str) -> httpx.Response:
        return httpx.delete(
            self._url(f"/v1/knowledge-bases/{_path_segment(kb_id)}"),
            timeout=self.timeout,
            headers=self._headers,
        )

    # 列出文档。
    def list_documents(self, kb_id: str) -> httpx.Response:
        return httpx.get(
            self._url(f"/v1/knowledge-bases/{_path_segment(kb_id)}/documents"),
            timeout=self.timeout,
            headers=self._headers,
        )

    # 列出知识库来源文件。
    def list_sources(self, kb_id: str) -> httpx.Response:
        return httpx.get(
            self._url(f"/v1/knowledge-bases/{_path_segment(kb_id)}/sources"),
            timeout=self.timeout,
            headers=self._headers,
        )

    # 列出来源文件分块。
    def list_source_chunks(
        self,
        kb_id: str,
        source: str,
        offset: int = 0,
        limit: int = 50,
        anchor_text: str | None = None,
    ) -> httpx.Response:
        params = {"offset": offset, "limit": limit, "anchor_text": anchor_text}
        return httpx.get(
            self._url(
                f"/v1/knowledge-bases/{_path_segment(kb_id)}/sources/"
                f"{_path_segment(source)}/chunks"
            ),
            params={k: v for k, v in params.items() if v is not None},
            timeout=self.timeout,
            headers=self._headers,
        )

    # 上传文档。
    def upload_document(
        self,
        kb_id: str,
        filename: str,
        content: bytes,
        *,
        allowed_role_ids: list[str] | None = None,
    ) -> httpx.Response:
        files = {
            "file": (
                filename,
                content,
                mimetypes.guess_type(filename)[0] or "application/octet-stream",
            )
        }
        return httpx.post(
            self._url(f"/v1/knowledge-bases/{_path_segment(kb_id)}/documents"),
            files=files,
            data=(
                [("allowed_role_ids", role_id) for role_id in allowed_role_ids]
                if allowed_role_ids is not None
                else None
            ),
            timeout=self.timeout,
            headers=self._headers,
        )

    def upload_documents(
        self,
        kb_id: str,
        documents: Iterable[tuple[str, bytes]],
        *,
        allowed_role_ids: list[str] | None = None,
        embedding_profile_id: str | None = None,
    ) -> httpx.Response:
        """Submit one batch upload job, matching the current Web client."""

        files = [
            (
                "files",
                (
                    filename,
                    content,
                    mimetypes.guess_type(filename)[0] or "application/octet-stream",
                ),
            )
            for filename, content in documents
        ]
        data: list[tuple[str, str]] = []
        data.extend(
            ("allowed_role_ids", role_id) for role_id in (allowed_role_ids or [])
        )
        if embedding_profile_id is not None:
            data.append(("embedding_profile_id", embedding_profile_id))
        return httpx.post(
            self._url(f"/v1/knowledge-bases/{quote(kb_id, safe='')}/documents/batch"),
            files=files,
            data=data,
            timeout=self.timeout,
            headers=self._headers,
        )

    def list_index_jobs(
        self, kb_id: str | None = None, *, limit: int = 200
    ) -> httpx.Response:
        params: dict[str, Any] = {"limit": limit}
        if kb_id is not None:
            params["kb_id"] = kb_id
        return httpx.get(
            self._url("/v1/index-jobs"),
            params=params,
            timeout=self.timeout,
            headers=self._headers,
        )

    def list_connections(self, kb_id: str) -> httpx.Response:
        return httpx.get(
            self._url(f"/v1/knowledge-bases/{_path_segment(kb_id)}/connections"),
            timeout=self.timeout,
            headers=self._headers,
        )

    def create_connection(
        self, kb_id: str, payload: Mapping[str, Any]
    ) -> httpx.Response:
        return httpx.post(
            self._url(f"/v1/knowledge-bases/{_path_segment(kb_id)}/connections"),
            json=dict(payload),
            timeout=self.timeout,
            headers=self._headers,
        )

    def set_connection_enabled(
        self, kb_id: str, connection_id: str, enabled: bool
    ) -> httpx.Response:
        return httpx.patch(
            self._url(
                f"/v1/knowledge-bases/{_path_segment(kb_id)}/connections/"
                f"{_path_segment(connection_id)}"
            ),
            json={"enabled": enabled},
            timeout=self.timeout,
            headers=self._headers,
        )

    def update_connection(
        self, kb_id: str, connection_id: str, payload: Mapping[str, Any]
    ) -> httpx.Response:
        return httpx.patch(
            self._url(
                f"/v1/knowledge-bases/{quote(kb_id, safe='')}/connections/"
                f"{quote(connection_id, safe='')}"
            ),
            json=dict(payload),
            timeout=self.timeout,
            headers=self._headers,
        )

    def delete_connection(self, kb_id: str, connection_id: str) -> httpx.Response:
        return httpx.delete(
            self._url(
                f"/v1/knowledge-bases/{quote(kb_id, safe='')}/connections/"
                f"{quote(connection_id, safe='')}"
            ),
            timeout=self.timeout,
            headers=self._headers,
        )

    def start_connection_sync(self, kb_id: str, connection_id: str) -> httpx.Response:
        return httpx.post(
            self._url(
                f"/v1/knowledge-bases/{_path_segment(kb_id)}/connections/"
                f"{_path_segment(connection_id)}/sync"
            ),
            timeout=self.timeout,
            headers=self._headers,
        )

    def list_sync_jobs(self, kb_id: str) -> httpx.Response:
        return httpx.get(
            self._url(f"/v1/knowledge-bases/{_path_segment(kb_id)}/sync-jobs"),
            timeout=self.timeout,
            headers=self._headers,
        )

    def list_workspace_sync_jobs(self, *, limit: int = 200) -> httpx.Response:
        return httpx.get(
            self._url("/v1/sync-jobs"),
            params={"limit": limit},
            timeout=self.timeout,
            headers=self._headers,
        )

    def list_ha_jobs(self, *, limit: int = 200) -> httpx.Response:
        return httpx.get(
            self._url("/v1/ha/jobs"),
            params={"limit": limit},
            timeout=self.timeout,
            headers=self._headers,
        )

    def cancel_ha_job(self, job_id: str) -> httpx.Response:
        return httpx.post(
            self._url(f"/v1/ha/jobs/{quote(job_id, safe='')}/cancel"),
            timeout=self.timeout,
            headers=self._headers,
        )

    def replay_ha_job(self, job_id: str, replay_key: str) -> httpx.Response:
        return httpx.post(
            self._url(f"/v1/ha/jobs/{quote(job_id, safe='')}/replay"),
            json={"replay_key": replay_key},
            timeout=self.timeout,
            headers=self._headers,
        )

    def get_sync_job(self, kb_id: str, job_id: str) -> httpx.Response:
        return httpx.get(
            self._url(
                f"/v1/knowledge-bases/{_path_segment(kb_id)}/sync-jobs/"
                f"{_path_segment(job_id)}"
            ),
            timeout=self.timeout,
            headers=self._headers,
        )

    def replay_sync_job(self, kb_id: str, job_id: str) -> httpx.Response:
        return httpx.post(
            self._url(
                f"/v1/knowledge-bases/{quote(kb_id, safe='')}/sync-jobs/"
                f"{quote(job_id, safe='')}/replay"
            ),
            timeout=self.timeout,
            headers=self._headers,
        )

    def list_connection_health(self, kb_id: str) -> httpx.Response:
        return httpx.get(
            self._url(f"/v1/knowledge-bases/{quote(kb_id, safe='')}/connection-health"),
            timeout=self.timeout,
            headers=self._headers,
        )

    def get_connection_health(self, kb_id: str, connection_id: str) -> httpx.Response:
        return httpx.get(
            self._url(
                f"/v1/knowledge-bases/{quote(kb_id, safe='')}/connections/"
                f"{quote(connection_id, safe='')}/health"
            ),
            timeout=self.timeout,
            headers=self._headers,
        )

    def list_connector_credentials(self, kb_id: str) -> httpx.Response:
        return httpx.get(
            self._url(
                f"/v1/knowledge-bases/{quote(kb_id, safe='')}/connector-credentials"
            ),
            timeout=self.timeout,
            headers=self._headers,
        )

    def create_connector_credential(
        self, kb_id: str, payload: Mapping[str, Any]
    ) -> httpx.Response:
        return httpx.post(
            self._url(
                f"/v1/knowledge-bases/{quote(kb_id, safe='')}/connector-credentials"
            ),
            json=dict(payload),
            timeout=self.timeout,
            headers=self._headers,
        )

    def rotate_connector_credential(
        self,
        kb_id: str,
        credential_id: str,
        *,
        secret_values: Mapping[str, str] | None = None,
        expires_at: float | None = None,
        expected_revision: int | None = None,
        update_expiry: bool = False,
    ) -> httpx.Response:
        payload: dict[str, Any] = {}
        if secret_values is not None:
            payload["secret_values"] = dict(secret_values)
        if update_expiry:
            payload["expires_at"] = expires_at
        if expected_revision is not None:
            payload["expected_revision"] = expected_revision
        return httpx.patch(
            self._url(
                f"/v1/knowledge-bases/{quote(kb_id, safe='')}/"
                f"connector-credentials/{quote(credential_id, safe='')}"
            ),
            json=payload,
            timeout=self.timeout,
            headers=self._headers,
        )

    def refresh_connector_credential(
        self,
        kb_id: str,
        credential_id: str,
        *,
        expected_revision: int | None = None,
    ) -> httpx.Response:
        params = (
            {"expected_revision": expected_revision}
            if expected_revision is not None
            else None
        )
        return httpx.post(
            self._url(
                f"/v1/knowledge-bases/{quote(kb_id, safe='')}/"
                f"connector-credentials/{quote(credential_id, safe='')}/refresh"
            ),
            params=params,
            timeout=self.timeout,
            headers=self._headers,
        )

    def delete_connector_credential(
        self,
        kb_id: str,
        credential_id: str,
        *,
        expected_revision: int | None = None,
    ) -> httpx.Response:
        params = (
            {"expected_revision": expected_revision}
            if expected_revision is not None
            else None
        )
        return httpx.delete(
            self._url(
                f"/v1/knowledge-bases/{quote(kb_id, safe='')}/"
                f"connector-credentials/{quote(credential_id, safe='')}"
            ),
            params=params,
            timeout=self.timeout,
            headers=self._headers,
        )

    def list_connector_credential_events(
        self,
        kb_id: str,
        *,
        credential_id: str | None = None,
        limit: int = 200,
    ) -> httpx.Response:
        params: dict[str, Any] = {"limit": limit}
        if credential_id is not None:
            params["credential_id"] = credential_id
        return httpx.get(
            self._url(
                f"/v1/knowledge-bases/{quote(kb_id, safe='')}/"
                "connector-credentials/audit/events"
            ),
            params=params,
            timeout=self.timeout,
            headers=self._headers,
        )

    def authorize_connector_oauth(
        self,
        kb_id: str,
        provider: str,
        *,
        connection_id: str | None = None,
    ) -> httpx.Response:
        payload = {"provider": provider}
        if connection_id is not None:
            payload["connection_id"] = connection_id
        return httpx.post(
            self._url(
                f"/v1/knowledge-bases/{quote(kb_id, safe='')}/connector-oauth/authorize"
            ),
            json=payload,
            timeout=self.timeout,
            headers=self._headers,
        )

    def list_source_catalog(
        self,
        kb_id: str,
        *,
        connection_id: str | None = None,
        health_status: str | None = None,
        include_deleted: bool = False,
    ) -> httpx.Response:
        params: dict[str, Any] = {"include_deleted": include_deleted}
        if connection_id is not None:
            params["connection_id"] = connection_id
        if health_status is not None:
            params["health_status"] = health_status
        return httpx.get(
            self._url(f"/v1/knowledge-bases/{quote(kb_id, safe='')}/source-catalog"),
            params=params,
            timeout=self.timeout,
            headers=self._headers,
        )

    def get_source_catalog_entry(self, kb_id: str, source_id: str) -> httpx.Response:
        return httpx.get(
            self._url(
                f"/v1/knowledge-bases/{quote(kb_id, safe='')}/source-catalog/"
                f"{quote(source_id, safe='')}"
            ),
            timeout=self.timeout,
            headers=self._headers,
        )

    def list_source_versions(self, kb_id: str, source_id: str) -> httpx.Response:
        return httpx.get(
            self._url(
                f"/v1/knowledge-bases/{quote(kb_id, safe='')}/source-catalog/"
                f"{quote(source_id, safe='')}/versions"
            ),
            timeout=self.timeout,
            headers=self._headers,
        )

    def diff_source_versions(
        self,
        kb_id: str,
        source_id: str,
        from_version_id: str,
        to_version_id: str,
    ) -> httpx.Response:
        return httpx.get(
            self._url(
                f"/v1/knowledge-bases/{quote(kb_id, safe='')}/source-catalog/"
                f"{quote(source_id, safe='')}/diff"
            ),
            params={
                "from_version_id": from_version_id,
                "to_version_id": to_version_id,
            },
            timeout=self.timeout,
            headers=self._headers,
        )

    def download_source_version(
        self, kb_id: str, source_id: str, version_id: str
    ) -> httpx.Response:
        return httpx.get(
            self._url(
                f"/v1/knowledge-bases/{quote(kb_id, safe='')}/source-catalog/"
                f"{quote(source_id, safe='')}/versions/"
                f"{quote(version_id, safe='')}/content"
            ),
            timeout=self.timeout,
            headers=self._headers,
        )

    def delete_source_artifact(
        self, kb_id: str, source_id: str, version_id: str
    ) -> httpx.Response:
        return httpx.delete(
            self._url(
                f"/v1/knowledge-bases/{quote(kb_id, safe='')}/source-catalog/"
                f"{quote(source_id, safe='')}/versions/"
                f"{quote(version_id, safe='')}/artifact"
            ),
            timeout=self.timeout,
            headers=self._headers,
        )

    def restore_source_artifact(
        self, kb_id: str, recovery_token: str
    ) -> httpx.Response:
        return httpx.post(
            self._url(
                f"/v1/knowledge-bases/{quote(kb_id, safe='')}/source-artifacts/"
                f"{quote(recovery_token, safe='')}/restore"
            ),
            timeout=self.timeout,
            headers=self._headers,
        )

    def get_source_artifact_usage(self, kb_id: str) -> httpx.Response:
        return httpx.get(
            self._url(
                f"/v1/knowledge-bases/{quote(kb_id, safe='')}/source-artifacts/usage"
            ),
            timeout=self.timeout,
            headers=self._headers,
        )

    # 删除文档。
    def delete_document(self, kb_id: str, name: str) -> httpx.Response:
        return httpx.delete(
            self._url(
                f"/v1/knowledge-bases/{_path_segment(kb_id)}/documents/"
                f"{_path_segment(name)}"
            ),
            timeout=self.timeout,
            headers=self._headers,
        )

    # 获取任务。
    def get_job(self, job_id: str) -> httpx.Response:
        return httpx.get(
            self._url(f"/v1/index-jobs/{_path_segment(job_id)}"),
            timeout=self.timeout,
            headers=self._headers,
        )

    # 获取跟踪。
    def get_trace(self, trace_id: str) -> httpx.Response:
        return httpx.get(
            self._url(f"/v1/traces/{_path_segment(trace_id)}"),
            timeout=self.timeout,
            headers=self._headers,
        )

    # 列出跟踪。
    def list_traces(
        self, limit: int = 20, kb_id: str = "", session_id: str = ""
    ) -> httpx.Response:
        return httpx.get(
            self._url("/v1/traces"),
            params={"limit": limit, "doc_id": kb_id, "session_id": session_id},
            timeout=self.timeout,
            headers=self._headers,
        )

    # 获取会话历史。
    def get_session_history(self, session_id: str, kb_id: str) -> httpx.Response:
        return httpx.get(
            self._url(f"/v1/sessions/{_path_segment(session_id)}/history"),
            params={"doc_id": kb_id},
            timeout=self.timeout,
            headers=self._headers,
        )

    # 列出会话。
    def list_sessions(self, kb_id: str) -> httpx.Response:
        return httpx.get(
            self._url("/v1/sessions"),
            params={"doc_id": kb_id},
            timeout=self.timeout,
            headers=self._headers,
        )

    # 删除会话。
    def delete_session(self, session_id: str, kb_id: str) -> httpx.Response:
        return httpx.delete(
            self._url(f"/v1/sessions/{_path_segment(session_id)}"),
            params={"doc_id": kb_id},
            timeout=self.timeout,
            headers=self._headers,
        )

    # 提交反馈。
    def submit_feedback(
        self,
        trace_id: str,
        feedback: str,
        kb_id: str | None = None,
        query: str | None = None,
        answer: str | None = None,
        citations: list[dict] | None = None,
        evidence: list[dict] | None = None,
        comment: str | None = None,
        correction: str | None = None,
        feedback_type: str | None = None,
        feedback_text: str | None = None,
        correction_text: str | None = None,
        save_as_knowledge: bool = False,
        skip_retrieval_feedback: bool = False,
        related_source: str | None = None,
        related_source_sha256: str | None = None,
        related_chunk_ids: list[str] | None = None,
        related_page_start: int | None = None,
        related_page_end: int | None = None,
        related_chunk_text_hash: str | None = None,
        related_anchor_text: str | None = None,
        certainty: str | None = None,
        created_by: str | None = None,
    ) -> httpx.Response:
        payload = {
            "trace_id": trace_id,
            "feedback": feedback,
            "kb_id": kb_id,
            "query": query,
            "answer": answer,
            "citations": citations or [],
            "evidence": evidence or [],
            "comment": comment,
            "correction": correction,
            "feedback_type": feedback_type,
            "feedback_text": feedback_text,
            "correction_text": correction_text,
            "save_as_knowledge": save_as_knowledge,
            "skip_retrieval_feedback": skip_retrieval_feedback,
            "related_source": related_source,
            "related_source_sha256": related_source_sha256,
            "related_chunk_ids": related_chunk_ids or [],
            "related_page_start": related_page_start,
            "related_page_end": related_page_end,
            "related_chunk_text_hash": related_chunk_text_hash,
            "related_anchor_text": related_anchor_text,
            "certainty": certainty,
            "created_by": created_by,
        }
        return httpx.post(
            self._url("/v1/feedback"),
            json={k: v for k, v in payload.items() if v is not None},
            timeout=self.timeout,
            headers=self._headers,
        )

    # 查询反馈记录。
    def list_feedback(
        self,
        kb_id: str,
        trace_id: str | None = None,
        session_id: str | None = None,
        feedback: str | None = None,
        feedback_type: str | None = None,
        is_bad_case: bool | None = None,
        limit: int = 100,
    ) -> httpx.Response:
        params = {
            "kb_id": kb_id,
            "trace_id": trace_id,
            "session_id": session_id,
            "feedback": feedback,
            "feedback_type": feedback_type,
            "is_bad_case": is_bad_case,
            "limit": limit,
        }
        return httpx.get(
            self._url("/v1/feedback"),
            params={k: v for k, v in params.items() if v is not None},
            timeout=self.timeout,
            headers=self._headers,
        )

    # 查询反馈理解结果。
    def list_feedback_analysis(
        self,
        kb_id: str,
        recommended_action: str | None = None,
        needs_review: bool | None = None,
        limit: int = 100,
    ) -> httpx.Response:
        params = {
            "kb_id": kb_id,
            "recommended_action": recommended_action,
            "needs_review": needs_review,
            "limit": limit,
        }
        return httpx.get(
            self._url("/v1/feedback-analysis"),
            params={k: v for k, v in params.items() if v is not None},
            timeout=self.timeout,
            headers=self._headers,
        )

    # 新增派生知识。
    def create_knowledge(
        self,
        *,
        kb_id: str,
        text: str,
        related_document_id: str | None = None,
        related_source: str | None = None,
        related_source_sha256: str | None = None,
        related_chunk_ids: list[str] | None = None,
        related_page_start: int | None = None,
        related_page_end: int | None = None,
        related_chunk_text_hash: str | None = None,
        related_anchor_text: str | None = None,
        source_note: str | None = None,
        certainty: str = "medium",
        origin: str = "manual_entry",
        created_from_trace_id: str | None = None,
        created_by: str | None = None,
    ) -> httpx.Response:
        payload = {
            "kb_id": kb_id,
            "text": text,
            "related_document_id": related_document_id,
            "related_source": related_source,
            "related_source_sha256": related_source_sha256,
            "related_chunk_ids": related_chunk_ids or [],
            "related_page_start": related_page_start,
            "related_page_end": related_page_end,
            "related_chunk_text_hash": related_chunk_text_hash,
            "related_anchor_text": related_anchor_text,
            "source_note": source_note,
            "certainty": certainty,
            "origin": origin,
            "created_from_trace_id": created_from_trace_id,
            "created_by": created_by,
        }
        return httpx.post(
            self._url("/v1/knowledge"),
            json={k: v for k, v in payload.items() if v is not None},
            timeout=self.timeout,
            headers=self._headers,
        )

    # 查询派生知识。
    def list_knowledge(
        self,
        kb_id: str,
        status: str | None = None,
        document_id: str | None = None,
        origin: str | None = None,
        created_by: str | None = None,
        conflict_group_id: str | None = None,
        has_conflict: bool | None = None,
        created_after: str | None = None,
        created_before: str | None = None,
    ) -> httpx.Response:
        params = {
            "kb_id": kb_id,
            "status": status,
            "document_id": document_id,
            "origin": origin,
            "created_by": created_by,
            "conflict_group_id": conflict_group_id,
            "has_conflict": has_conflict,
            "created_after": created_after,
            "created_before": created_before,
        }
        return httpx.get(
            self._url("/v1/knowledge"),
            params={k: v for k, v in params.items() if v is not None},
            timeout=self.timeout,
            headers=self._headers,
        )

    # 查询审核队列摘要。
    def review_queue_summary(
        self,
        kb_id: str,
        document_id: str | None = None,
        origin: str | None = None,
        created_by: str | None = None,
        has_conflict: bool | None = None,
        created_after: str | None = None,
        created_before: str | None = None,
    ) -> httpx.Response:
        params = {
            "kb_id": kb_id,
            "document_id": document_id,
            "origin": origin,
            "created_by": created_by,
            "has_conflict": has_conflict,
            "created_after": created_after,
            "created_before": created_before,
        }
        return httpx.get(
            self._url("/v1/review-queue"),
            params={k: v for k, v in params.items() if v is not None},
            timeout=self.timeout,
            headers=self._headers,
        )

    # 导出审核队列。
    def review_queue_export(
        self,
        kb_id: str,
        limit: int = 200,
        knowledge_document_id: str | None = None,
        knowledge_origin: str | None = None,
        knowledge_created_by: str | None = None,
        knowledge_has_conflict: bool | None = None,
        knowledge_created_after: str | None = None,
        knowledge_created_before: str | None = None,
    ) -> httpx.Response:
        params = {
            "kb_id": kb_id,
            "limit": limit,
            "knowledge_document_id": knowledge_document_id,
            "knowledge_origin": knowledge_origin,
            "knowledge_created_by": knowledge_created_by,
            "knowledge_has_conflict": knowledge_has_conflict,
            "knowledge_created_after": knowledge_created_after,
            "knowledge_created_before": knowledge_created_before,
        }
        return httpx.get(
            self._url("/v1/review-queue/export"),
            params={k: v for k, v in params.items() if v is not None},
            timeout=self.timeout,
            headers=self._headers,
        )

    # 查询待审核计数。
    def pending_knowledge_count(self, kb_id: str) -> httpx.Response:
        return httpx.get(
            self._url("/v1/knowledge/pending-count"),
            params={"kb_id": kb_id},
            timeout=self.timeout,
            headers=self._headers,
        )

    # 查询派生知识索引状态。
    def knowledge_index_status(self, kb_id: str) -> httpx.Response:
        return httpx.get(
            self._url("/v1/knowledge/index-status"),
            params={"kb_id": kb_id},
            timeout=self.timeout,
            headers=self._headers,
        )

    # 扫描过期派生知识。
    def scan_stale_knowledge(self, kb_id: str) -> httpx.Response:
        return httpx.post(
            self._url("/v1/knowledge/stale-scan"),
            params={"kb_id": kb_id},
            timeout=self.timeout,
            headers=self._headers,
        )

    # 查询反馈闭环指标。
    def feedback_loop_metrics(
        self,
        kb_id: str,
        answer_count: int | None = None,
    ) -> httpx.Response:
        params = {"kb_id": kb_id, "answer_count": answer_count}
        return httpx.get(
            self._url("/v1/feedback-loop-metrics"),
            params={k: v for k, v in params.items() if v is not None},
            timeout=self.timeout,
            headers=self._headers,
        )

    # 审核派生知识。
    def review_knowledge(
        self,
        knowledge_id: str,
        action: str,
        actor: str | None = None,
        note: str | None = None,
        related_document_id: str | None = None,
        related_source: str | None = None,
        related_source_sha256: str | None = None,
        related_chunk_ids: list[str] | None = None,
        related_page_start: int | None = None,
        related_page_end: int | None = None,
        related_chunk_text_hash: str | None = None,
        related_anchor_text: str | None = None,
    ) -> httpx.Response:
        payload = {
            "actor": actor,
            "note": note,
            "related_document_id": related_document_id,
            "related_source": related_source,
            "related_source_sha256": related_source_sha256,
            "related_chunk_ids": related_chunk_ids,
            "related_page_start": related_page_start,
            "related_page_end": related_page_end,
            "related_chunk_text_hash": related_chunk_text_hash,
            "related_anchor_text": related_anchor_text,
        }
        return httpx.post(
            self._url(
                f"/v1/knowledge/{_path_segment(knowledge_id)}/"
                f"{_path_segment(action)}"
            ),
            json={k: v for k, v in payload.items() if v is not None},
            timeout=self.timeout,
            headers=self._headers,
        )

    # 创建知识修订版本。
    def revise_knowledge(
        self,
        knowledge_id: str,
        *,
        text: str,
        related_document_id: str | None = None,
        related_source: str | None = None,
        related_source_sha256: str | None = None,
        related_chunk_ids: list[str] | None = None,
        related_page_start: int | None = None,
        related_page_end: int | None = None,
        related_chunk_text_hash: str | None = None,
        related_anchor_text: str | None = None,
        source_note: str | None = None,
        certainty: str = "medium",
        created_from_trace_id: str | None = None,
        created_by: str | None = None,
    ) -> httpx.Response:
        payload = {
            "text": text,
            "related_document_id": related_document_id,
            "related_source": related_source,
            "related_source_sha256": related_source_sha256,
            "related_chunk_ids": related_chunk_ids,
            "related_page_start": related_page_start,
            "related_page_end": related_page_end,
            "related_chunk_text_hash": related_chunk_text_hash,
            "related_anchor_text": related_anchor_text,
            "source_note": source_note,
            "certainty": certainty,
            "created_from_trace_id": created_from_trace_id,
            "created_by": created_by,
        }
        return httpx.post(
            self._url(f"/v1/knowledge/{_path_segment(knowledge_id)}/revise"),
            json={k: v for k, v in payload.items() if v is not None},
            timeout=self.timeout,
            headers=self._headers,
        )

    # 批量审核派生知识。
    def batch_review_knowledge(
        self,
        knowledge_ids: list[str],
        action: str,
        actor: str | None = None,
        note: str | None = None,
    ) -> httpx.Response:
        payload = {"knowledge_ids": knowledge_ids, "actor": actor, "note": note}
        return httpx.post(
            self._url(f"/v1/knowledge/{_path_segment(action)}"),
            json={k: v for k, v in payload.items() if v is not None},
            timeout=self.timeout,
            headers=self._headers,
        )

    # 删除派生知识。
    def delete_knowledge(self, knowledge_id: str) -> httpx.Response:
        return httpx.delete(
            self._url(f"/v1/knowledge/{_path_segment(knowledge_id)}"),
            timeout=self.timeout,
            headers=self._headers,
        )

    # 查询检索调权反馈。
    def list_retrieval_feedback(
        self,
        kb_id: str,
        enabled: bool | None = None,
        limit: int = 100,
    ) -> httpx.Response:
        params = {"kb_id": kb_id, "enabled": enabled, "limit": limit}
        return httpx.get(
            self._url("/v1/retrieval-feedback"),
            params={k: v for k, v in params.items() if v is not None},
            timeout=self.timeout,
            headers=self._headers,
        )

    # 设置检索调权反馈状态。
    def set_retrieval_feedback_enabled(
        self,
        feedback_id: str,
        enabled: bool,
        actor: str | None = None,
        reason: str | None = None,
    ) -> httpx.Response:
        action = "enable" if enabled else "disable"
        payload = {"actor": actor, "reason": reason}
        kwargs = {"timeout": self.timeout, "headers": self._headers}
        if not enabled:
            kwargs["json"] = {k: v for k, v in payload.items() if v is not None}
        return httpx.post(
            self._url(
                f"/v1/retrieval-feedback/{_path_segment(feedback_id)}/"
                f"{_path_segment(action)}"
            ),
            **kwargs,
        )

    def list_retrieval_eval_drafts(
        self,
        *,
        kb_id: str | None = None,
        status: str | None = None,
        dataset_partition: str | None = None,
        task_kind: str | None = None,
        is_stale: bool | None = None,
        limit: int = 100,
    ) -> httpx.Response:
        params = {
            "kb_id": kb_id,
            "status": status,
            "dataset_partition": dataset_partition,
            "task_kind": task_kind,
            "is_stale": is_stale,
            "limit": limit,
        }
        return httpx.get(
            self._url("/v1/retrieval-eval-drafts"),
            params={key: value for key, value in params.items() if value is not None},
            timeout=self.timeout,
            headers=self._headers,
        )

    def get_retrieval_eval_draft(self, draft_id: str) -> httpx.Response:
        return httpx.get(
            self._url(f"/v1/retrieval-eval-drafts/{_path_segment(draft_id)}"),
            timeout=self.timeout,
            headers=self._headers,
        )

    def diagnose_retrieval(
        self,
        kb_id: str,
        query: str,
        *,
        top_k: int = 12,
        rerank: bool = True,
        route_weights: Mapping[str, float] | None = None,
        requirements: list[dict[str, Any]] | None = None,
    ) -> httpx.Response:
        return httpx.post(
            self._url("/v1/retrieval-diagnostics"),
            json={
                "doc_id": kb_id,
                "query": query,
                "top_k": top_k,
                "rerank": rerank,
                "route_weights": dict(route_weights) if route_weights else None,
                "requirements": requirements or [],
            },
            timeout=self.timeout,
            headers=self._headers,
        )

    def scan_index_migrations(self) -> httpx.Response:
        return httpx.get(
            self._url("/v1/index-migrations/scan"),
            timeout=self.timeout,
            headers=self._headers,
        )

    def start_index_migration(self, kb_ids: list[str]) -> httpx.Response:
        return httpx.post(
            self._url("/v1/index-migrations"),
            json={"kb_ids": kb_ids},
            timeout=self.timeout,
            headers=self._headers,
        )

    def get_index_migration(self, run_id: str) -> httpx.Response:
        return httpx.get(
            self._url(f"/v1/index-migrations/{_path_segment(run_id)}"),
            timeout=self.timeout,
            headers=self._headers,
        )

    def rollback_index_migration(self, run_id: str) -> httpx.Response:
        return httpx.post(
            self._url(f"/v1/index-migrations/{_path_segment(run_id)}/rollback"),
            json={"kb_ids": []},
            timeout=self.timeout,
            headers=self._headers,
        )

    def finalize_index_migration(self, run_id: str) -> httpx.Response:
        return httpx.post(
            self._url(f"/v1/index-migrations/{_path_segment(run_id)}/finalize"),
            timeout=self.timeout,
            headers=self._headers,
        )

    def save_retrieval_diagnostic_label(
        self,
        kb_id: str,
        query: str,
        *,
        no_answer: bool,
        acceptable_evidence: list[dict[str, Any]],
        hard_negative_evidence: list[dict[str, Any]],
        requirement_label: str = "",
    ) -> httpx.Response:
        return httpx.post(
            self._url("/v1/retrieval-diagnostics/labels"),
            json={
                "doc_id": kb_id,
                "query": query,
                "no_answer": no_answer,
                "acceptable_evidence": acceptable_evidence,
                "hard_negative_evidence": hard_negative_evidence,
                "requirement_label": requirement_label,
            },
            timeout=self.timeout,
            headers=self._headers,
        )

    def get_retrieval_eval_candidates(
        self, draft_id: str, *, top_k: int = 12
    ) -> httpx.Response:
        return httpx.get(
            self._url(
                f"/v1/retrieval-eval-drafts/{_path_segment(draft_id)}/candidates"
            ),
            params={"top_k": top_k},
            timeout=self.timeout,
            headers=self._headers,
        )

    def review_retrieval_eval_draft(
        self,
        draft_id: str,
        *,
        decision: str,
        expected_revision: int,
        annotations: Mapping[str, Any] | None = None,
        reason: str = "",
    ) -> httpx.Response:
        payload: dict[str, Any] = {
            "decision": decision,
            "expected_revision": expected_revision,
            "reason": reason,
        }
        if annotations is not None:
            payload["annotations"] = dict(annotations)
        return httpx.post(
            self._url(f"/v1/retrieval-eval-drafts/{_path_segment(draft_id)}/review"),
            json=payload,
            timeout=self.timeout,
            headers=self._headers,
        )

    def export_retrieval_eval_drafts(
        self,
        *,
        dataset_partition: str = "release_gate",
        export_format: str = "retrieval_eval_v1",
    ) -> httpx.Response:
        return httpx.get(
            self._url("/v1/retrieval-eval-drafts/export"),
            params={
                "dataset_partition": dataset_partition,
                "format": export_format,
            },
            timeout=self.timeout,
            headers=self._headers,
        )

    def claim_verification_review_summary(
        self, kb_id: str | None = None
    ) -> httpx.Response:
        request_options: dict[str, Any] = {
            "timeout": self.timeout,
            "headers": self._headers,
        }
        if kb_id is not None:
            request_options["params"] = {"kb_id": kb_id}
        return httpx.get(
            self._url("/v1/claim-verification/reviews/summary"),
            **request_options,
        )

    def list_claim_verification_reviews(
        self,
        *,
        status: str | None = None,
        limit: int = 25,
        cursor: str | None = None,
        kb_id: str | None = None,
    ) -> httpx.Response:
        params = {
            "status": status,
            "limit": limit,
            "cursor": cursor,
            "kb_id": kb_id,
        }
        return httpx.get(
            self._url("/v1/claim-verification/reviews"),
            params={key: value for key, value in params.items() if value is not None},
            timeout=self.timeout,
            headers=self._headers,
        )

    def get_claim_verification_review(self, review_id: str) -> httpx.Response:
        return httpx.get(
            self._url(f"/v1/claim-verification/reviews/{_path_segment(review_id)}"),
            timeout=self.timeout,
            headers=self._headers,
        )

    def label_claim_verification_review(
        self,
        review_id: str,
        *,
        expected_verdict: str,
        expected_revision: int,
        review_note: str = "",
    ) -> httpx.Response:
        return httpx.post(
            self._url(
                f"/v1/claim-verification/reviews/{_path_segment(review_id)}/label"
            ),
            json={
                "expected_verdict": expected_verdict,
                "expected_revision": expected_revision,
                "review_note": review_note,
            },
            timeout=self.timeout,
            headers=self._headers,
        )

    def export_claim_verification_reviews(
        self,
        *,
        limit: int = 1000,
        cursor: str | None = None,
    ) -> httpx.Response:
        params = {"limit": limit, "cursor": cursor}
        return httpx.get(
            self._url("/v1/claim-verification/reviews/export"),
            params={key: value for key, value in params.items() if value is not None},
            timeout=self.timeout,
            headers=self._headers,
        )

    def export_all_claim_verification_reviews(
        self,
        *,
        page_size: int = 1000,
        max_items: int = 10_000,
    ) -> list[dict[str, Any]]:
        bounded_page_size = max(1, min(1000, int(page_size)))
        bounded_max_items = max(1, min(10_000, int(max_items)))
        items: list[dict[str, Any]] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        while True:
            response = self.export_claim_verification_reviews(
                limit=bounded_page_size,
                cursor=cursor,
            )
            payload = _checked_json(response)
            if not isinstance(payload, Mapping):
                raise CogDocAPIError("声明核验导出响应格式不符合预期")
            page_items = payload.get("items")
            if not isinstance(page_items, list) or not all(
                isinstance(item, Mapping) for item in page_items
            ):
                raise CogDocAPIError("声明核验导出 items 格式不符合预期")
            if len(items) + len(page_items) > bounded_max_items:
                raise CogDocAPIError(
                    f"声明核验导出超过客户端上限 {bounded_max_items} 条"
                )
            items.extend(dict(item) for item in page_items)
            next_cursor = payload.get("next_cursor")
            if next_cursor is None:
                return items
            if not isinstance(next_cursor, str) or not next_cursor:
                raise CogDocAPIError("声明核验导出 next_cursor 格式不符合预期")
            if next_cursor in seen_cursors:
                raise CogDocAPIError("声明核验导出分页游标发生循环")
            seen_cursors.add(next_cursor)
            cursor = next_cursor

    def create_research_job(
        self,
        kb_id: str,
        objective: str,
        *,
        title: str = "",
        section_titles: list[str] | None = None,
        is_local: bool = False,
    ) -> httpx.Response:
        payload = {
            "kb_id": kb_id,
            "objective": objective,
            "title": title,
            "section_titles": section_titles or [],
            "is_local": is_local,
        }
        return httpx.post(
            self._url("/v1/research-jobs"),
            json=payload,
            timeout=self.timeout,
            headers=self._headers,
        )

    def list_research_jobs(
        self, kb_id: str, *, status: str | None = None, limit: int = 100
    ) -> httpx.Response:
        params = {"kb_id": kb_id, "limit": limit}
        if status:
            params["status"] = status
        return httpx.get(
            self._url("/v1/research-jobs"),
            params=params,
            timeout=self.timeout,
            headers=self._headers,
        )

    def list_research_job_summaries(
        self,
        kb_id: str,
        *,
        status: str | None = None,
        limit: int = 20,
        cursor: str | None = None,
        if_none_match: str | None = None,
    ) -> httpx.Response:
        params: dict[str, str | int] = {"kb_id": kb_id, "limit": limit}
        if status:
            params["status"] = status
        if cursor:
            params["cursor"] = cursor
        headers = dict(self._headers)
        if if_none_match:
            headers["If-None-Match"] = if_none_match
        return httpx.get(
            self._url("/v1/research-jobs/summaries"),
            params=params,
            timeout=self.timeout,
            headers=headers,
        )

    def get_research_job(self, job_id: str) -> httpx.Response:
        return httpx.get(
            self._url(f"/v1/research-jobs/{_path_segment(job_id)}"),
            timeout=self.timeout,
            headers=self._headers,
        )

    def update_research_plan(
        self,
        job_id: str,
        *,
        expected_revision: int,
        sections: list[dict[str, str]],
    ) -> httpx.Response:
        return httpx.put(
            self._url(f"/v1/research-jobs/{_path_segment(job_id)}/plan"),
            json={
                "expected_revision": expected_revision,
                "sections": sections,
            },
            timeout=self.timeout,
            headers=self._headers,
        )

    def generate_research_plan(
        self,
        job_id: str,
        *,
        expected_revision: int,
        is_local: bool | None = None,
    ) -> httpx.Response:
        return httpx.post(
            self._url(f"/v1/research-jobs/{_path_segment(job_id)}/plan/auto"),
            json={
                "expected_revision": expected_revision,
                "is_local": is_local,
            },
            timeout=self.timeout,
            headers=self._headers,
        )

    def research_action(self, job_id: str, action: str) -> httpx.Response:
        if action not in {
            "start",
            "pause",
            "resume",
            "cancel",
            "generate",
            "refresh",
        }:
            raise ValueError(f"unsupported research action: {action}")
        return httpx.post(
            self._url(
                f"/v1/research-jobs/{_path_segment(job_id)}/{_path_segment(action)}"
            ),
            timeout=self.timeout,
            headers=self._headers,
        )

    def get_research_provenance(self, job_id: str) -> httpx.Response:
        return httpx.get(
            self._url(f"/v1/research-jobs/{_path_segment(job_id)}/provenance"),
            timeout=self.timeout,
            headers=self._headers,
        )

    def get_research_report(self, job_id: str) -> httpx.Response:
        return httpx.get(
            self._url(f"/v1/research-jobs/{_path_segment(job_id)}/report"),
            timeout=self.timeout,
            headers=self._headers,
        )

    def review_research_report(
        self,
        job_id: str,
        *,
        expected_revision: int,
        decisions: list[dict[str, str]],
    ) -> httpx.Response:
        return httpx.put(
            self._url(f"/v1/research-jobs/{_path_segment(job_id)}/review"),
            json={
                "expected_revision": expected_revision,
                "decisions": decisions,
            },
            timeout=self.timeout,
            headers=self._headers,
        )

    def publish_research_report(
        self, job_id: str, *, expected_revision: int
    ) -> httpx.Response:
        return httpx.post(
            self._url(f"/v1/research-jobs/{_path_segment(job_id)}/publish"),
            json={"expected_revision": expected_revision},
            timeout=self.timeout,
            headers=self._headers,
        )

    def get_published_research_report(self, job_id: str) -> httpx.Response:
        return httpx.get(
            self._url(f"/v1/research-jobs/{_path_segment(job_id)}/published-report"),
            timeout=self.timeout,
            headers=self._headers,
        )

    def get_published_research_bundle(self, job_id: str) -> httpx.Response:
        return httpx.get(
            self._url(f"/v1/research-jobs/{_path_segment(job_id)}/published-bundle"),
            timeout=self.timeout,
            headers=self._headers,
        )

    # 构造对话请求体。
    def _chat_payload(
        self, kb_id: str, query: str, mode: str, session_id: str | None, is_local: bool
    ) -> dict:
        payload = {"query": query, "doc_id": kb_id, "mode": mode, "is_local": is_local}
        if session_id:
            payload["session_id"] = session_id
        return payload

    # 调用独立摘要接口。
    def summary(
        self,
        kb_id: str,
        query: str,
        session_id: str | None = None,
        is_local: bool = False,
    ) -> httpx.Response:
        payload = {
            "query": query,
            "doc_id": kb_id,
            "session_id": session_id,
            "is_local": is_local,
        }
        return httpx.post(
            self._url("/v1/summary"),
            json={k: v for k, v in payload.items() if v is not None},
            timeout=self.timeout,
            headers=self._headers,
        )

    # 调用独立对比接口。
    def compare(
        self,
        kb_id: str,
        query: str,
        session_id: str | None = None,
        is_local: bool = False,
    ) -> httpx.Response:
        payload = {
            "query": query,
            "doc_id": kb_id,
            "session_id": session_id,
            "is_local": is_local,
        }
        return httpx.post(
            self._url("/v1/compare"),
            json={k: v for k, v in payload.items() if v is not None},
            timeout=self.timeout,
            headers=self._headers,
        )

    # 调用独立检索接口。
    def retrieve(
        self,
        kb_id: str,
        query: str,
        top_k: int = 8,
        rerank: bool = False,
        rerank_top_n: int | None = None,
    ) -> httpx.Response:
        payload = {
            "query": query,
            "doc_id": kb_id,
            "top_k": top_k,
            "rerank": rerank,
            "rerank_top_n": rerank_top_n,
        }
        return httpx.post(
            self._url("/v1/retrieve"),
            json={k: v for k, v in payload.items() if v is not None},
            timeout=self.timeout,
            headers=self._headers,
        )

    # 流式返回对话。
    def stream_chat(
        self,
        kb_id: str,
        query: str,
        mode: str = "auto",
        session_id: str | None = None,
        is_local: bool = False,
        on_response: Callable[[httpx.Response], None] | None = None,
    ) -> Iterator[tuple[str, dict]]:
        payload = self._chat_payload(kb_id, query, mode, session_id, is_local)
        with httpx.stream(
            "POST",
            self._url("/v1/chat/stream"),
            json=payload,
            timeout=self.timeout,
            headers=self._headers,
        ) as response:
            if on_response is not None:
                on_response(response)
            if response.status_code != 200:
                # 流式响应失败不会抛异常，需读出正文转成错误事件，避免静默成空答案。
                response.read()
                try:
                    yield "error", response.json()
                except ValueError:
                    yield "error", {"message": response.text}
                return
            yield from iter_sse_events(response.iter_lines())
