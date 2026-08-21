from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Final, Mapping


class Role(str, Enum):
    """Tenant-scoped roles understood by the HTTP authorization boundary."""

    OWNER = "owner"
    ADMIN = "admin"
    EDITOR = "editor"
    REVIEWER = "reviewer"
    VIEWER = "viewer"


class Permission(str, Enum):
    READ = "read"
    QUERY = "query"
    WRITE = "write"
    DELETE = "delete"
    REVIEW = "review"
    PUBLISH = "publish"
    MANAGE_ACCESS = "manage_access"
    MANAGE_TENANT = "manage_tenant"


_VIEWER_PERMISSIONS = frozenset({Permission.READ, Permission.QUERY})
_REVIEWER_PERMISSIONS = _VIEWER_PERMISSIONS | frozenset(
    {Permission.REVIEW, Permission.PUBLISH}
)
_EDITOR_PERMISSIONS = _VIEWER_PERMISSIONS | frozenset({Permission.WRITE})
_ADMIN_PERMISSIONS = (
    _REVIEWER_PERMISSIONS
    | _EDITOR_PERMISSIONS
    | frozenset({Permission.DELETE, Permission.MANAGE_ACCESS})
)

# Immutable and public so route-level dependencies and tests can consume the same
# matrix instead of growing a second, subtly different authorization model.
ROLE_PERMISSIONS: Final[Mapping[Role, frozenset[Permission]]] = MappingProxyType(
    {
        Role.OWNER: _ADMIN_PERMISSIONS | frozenset({Permission.MANAGE_TENANT}),
        Role.ADMIN: _ADMIN_PERMISSIONS,
        Role.EDITOR: _EDITOR_PERMISSIONS,
        Role.REVIEWER: _REVIEWER_PERMISSIONS,
        Role.VIEWER: _VIEWER_PERMISSIONS,
    }
)


def fingerprint_api_key(api_key: str) -> str:
    """Return a stable non-secret identifier suitable for logs and state."""

    if not isinstance(api_key, str) or not api_key:
        raise ValueError("api_key must be a non-empty string")
    return f"sha256:{hashlib.sha256(api_key.encode('utf-8')).hexdigest()}"


def _identity_part(value: str, *, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{name} must not be empty")
    if len(normalized) > 160:
        raise ValueError(f"{name} is too long")
    if any(ord(character) < 32 or ord(character) == 127 for character in normalized):
        raise ValueError(f"{name} must not contain control characters")
    return normalized


@dataclass(frozen=True, slots=True)
class Principal:
    """Authenticated tenant identity attached to ``request.state``.

    ``key_fingerprint`` is deliberately not the credential.  Callers creating
    explicit principals should derive it with :func:`fingerprint_api_key`.
    """

    tenant_id: str
    subject_id: str
    role: Role
    key_fingerprint: str
    membership_id: str | None = None
    permission_scope: frozenset[Permission] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "tenant_id", _identity_part(self.tenant_id, name="tenant_id")
        )
        object.__setattr__(
            self, "subject_id", _identity_part(self.subject_id, name="subject_id")
        )
        try:
            role = self.role if isinstance(self.role, Role) else Role(self.role)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"unsupported role: {self.role!r}") from exc
        object.__setattr__(self, "role", role)
        object.__setattr__(
            self,
            "key_fingerprint",
            _identity_part(self.key_fingerprint, name="key_fingerprint"),
        )
        if self.membership_id is not None:
            object.__setattr__(
                self,
                "membership_id",
                _identity_part(self.membership_id, name="membership_id"),
            )
        if self.permission_scope is not None:
            try:
                scope = frozenset(Permission(item) for item in self.permission_scope)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    "permission_scope contains an unsupported permission"
                ) from exc
            object.__setattr__(self, "permission_scope", scope)

    @property
    def permissions(self) -> frozenset[Permission]:
        role_permissions = ROLE_PERMISSIONS[self.role]
        if self.permission_scope is None:
            return role_permissions
        return role_permissions & self.permission_scope

    @property
    def rate_limit_identity(self) -> str:
        # Identity components reject control characters, so the unit separator
        # makes this tuple unambiguous without exposing the API-key fingerprint.
        return f"{self.tenant_id}\x1f{self.subject_id}"

    def allows(self, permission: Permission) -> bool:
        return permission in self.permissions

    @classmethod
    def for_api_key(
        cls,
        api_key: str,
        *,
        tenant_id: str,
        subject_id: str,
        role: Role | str,
    ) -> "Principal":
        return cls(
            tenant_id=tenant_id,
            subject_id=subject_id,
            role=Role(role),
            key_fingerprint=fingerprint_api_key(api_key),
        )

    @classmethod
    def local_owner(cls) -> "Principal":
        return cls(
            tenant_id="default",
            subject_id="local",
            role=Role.OWNER,
            key_fingerprint="auth-disabled",
        )

    @classmethod
    def for_user_session(
        cls,
        *,
        tenant_id: str,
        subject_id: str,
        role: Role | str,
        session_id: str,
        membership_id: str | None = None,
    ) -> "Principal":
        """Build a principal without embedding a bearer secret in memory or logs."""

        clean_session_id = _identity_part(session_id, name="session_id")
        return cls(
            tenant_id=tenant_id,
            subject_id=subject_id,
            role=Role(role),
            key_fingerprint=f"session:{clean_session_id}",
            membership_id=membership_id,
        )


_READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH"})
_QUERY_PATHS = frozenset(
    {
        "/v1/chat",
        "/v1/chat/stream",
        "/v1/summary",
        "/v1/compare",
        "/v1/retrieve",
    }
)
_PUBLIC_AUTH_PATHS = frozenset(
    {
        "/v1/auth/config",
        "/v1/auth/register",
        "/v1/auth/login",
        "/v1/auth/oidc/authorize",
        "/v1/auth/oidc/callback",
        "/v1/auth/oidc/exchange",
        "/v1/auth/invitations/accept",
        "/v1/auth/connector-oauth/callback/notion",
        "/v1/auth/connector-oauth/callback/atlassian",
        "/v1/auth/connector-oauth/callback/microsoft",
    }
)
_KNOWLEDGE_BATCH_REVIEW_PATHS = frozenset(
    {"/v1/knowledge/batch-approve", "/v1/knowledge/batch-reject"}
)
_KNOWLEDGE_REVIEW_SUFFIXES = frozenset({"approve", "reject", "archive"})


def _normalized_path(path: str) -> str:
    if not isinstance(path, str) or not path.startswith("/"):
        return "/"
    return path.rstrip("/") or "/"


def _is_research_review(path: str, method: str) -> Permission | None:
    parts = path.split("/")
    if len(parts) != 5 or parts[:3] != ["", "v1", "research-jobs"]:
        return None
    if method == "PUT" and parts[4] == "review":
        return Permission.REVIEW
    if method == "POST" and parts[4] == "publish":
        return Permission.PUBLISH
    return None


def _is_knowledge_review(path: str, method: str) -> bool:
    if method != "POST":
        return False
    if path in _KNOWLEDGE_BATCH_REVIEW_PATHS:
        return True
    parts = path.split("/")
    return (
        len(parts) == 5
        and parts[:3] == ["", "v1", "knowledge"]
        and bool(parts[3])
        and parts[4] in _KNOWLEDGE_REVIEW_SUFFIXES
    )


def required_permission(method: str, path: str) -> Permission:
    """Resolve one centralized, ordered method/path policy.

    Specific high-trust and compute-only operations take precedence over the
    conservative method fallback.  Unrecognized methods fail closed to the one
    permission held only by a tenant owner.
    """

    normalized_method = str(method or "").upper()
    normalized_path = _normalized_path(path)

    # These routes authenticate/authorize inside their own handler.  This
    # branch is useful to policy consumers even though the HTTP middleware
    # exempts them from principal-based RBAC.
    if normalized_path in _PUBLIC_AUTH_PATHS and normalized_method == "POST":
        return Permission.READ

    # Credential/session lifecycle is self-service. A viewer must be able to
    # log out, rotate a password, revoke their own sessions, and switch to a
    # workspace without acquiring content-mutation rights.
    if normalized_path == "/v1/auth/config" or normalized_path.startswith("/v1/auth/"):
        return Permission.READ
    if normalized_path == "/v1/workspaces" and normalized_method in {
        "GET",
        "POST",
    }:
        return Permission.READ
    workspace_parts = normalized_path.split("/")
    if (
        len(workspace_parts) == 5
        and workspace_parts[:3] == ["", "v1", "workspaces"]
        and workspace_parts[4] == "switch"
    ):
        return Permission.READ
    if normalized_path.startswith("/v1/workspaces/"):
        if (
            "/service-accounts" in normalized_path
            or "/security-sessions" in normalized_path
            or normalized_path.endswith("/service-account-policy")
            or normalized_path.endswith("/session-policy")
        ):
            return Permission.MANAGE_ACCESS
        if "/members" in normalized_path or "/invites" in normalized_path:
            return (
                Permission.READ
                if normalized_method in _READ_METHODS
                else Permission.MANAGE_ACCESS
            )
        if normalized_method in {"PATCH", "DELETE"}:
            return Permission.MANAGE_TENANT

    if normalized_path == "/v1/tenants" or normalized_path.startswith("/v1/tenants/"):
        return Permission.MANAGE_TENANT
    if normalized_path == "/v1/audit-events" or normalized_path.startswith(
        "/v1/audit-events/"
    ):
        return Permission.MANAGE_ACCESS
    if normalized_path == "/v1/principals" or normalized_path.startswith(
        "/v1/principals/"
    ):
        return Permission.MANAGE_ACCESS
    if "/access" in normalized_path:
        return Permission.MANAGE_ACCESS
    if normalized_path == "/v1/retrieval-eval-drafts" or normalized_path.startswith(
        "/v1/retrieval-eval-drafts/"
    ):
        return Permission.REVIEW
    if normalized_path == "/v1/review-queue" or normalized_path.startswith(
        "/v1/review-queue/"
    ):
        return Permission.REVIEW
    if normalized_path.startswith("/v1/claim-verification/observations"):
        return Permission.REVIEW
    if normalized_path.startswith("/v1/claim-verification/reviews"):
        return Permission.REVIEW

    research_permission = _is_research_review(normalized_path, normalized_method)
    if research_permission is not None:
        return research_permission
    if _is_knowledge_review(normalized_path, normalized_method):
        return Permission.REVIEW
    if normalized_method == "POST" and normalized_path in _QUERY_PATHS:
        return Permission.QUERY
    if normalized_method in _READ_METHODS:
        return Permission.READ
    if normalized_method in _WRITE_METHODS:
        return Permission.WRITE
    if normalized_method == "DELETE":
        return Permission.DELETE
    return Permission.MANAGE_TENANT


__all__ = [
    "Permission",
    "Principal",
    "ROLE_PERMISSIONS",
    "Role",
    "fingerprint_api_key",
    "required_permission",
]
