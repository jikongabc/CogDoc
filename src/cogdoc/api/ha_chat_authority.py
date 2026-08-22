from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Callable

from fastapi import Request

from cogdoc.api.connector_scope import (
    KBIncarnationChanged,
    assert_active_kb_incarnation,
    capture_kb_epoch,
)
from cogdoc.api.tenant_scope import KnowledgeBaseScope, request_principal
from cogdoc.api.tenancy import Permission, Principal, Role


class HAChatAuthorityChanged(PermissionError):
    pass


def capture_ha_chat_epoch(registry: Any, storage_id: str) -> int:
    """Read the shared HA epoch when available, with local-mode compatibility."""

    current = getattr(registry, "current", None)
    return (
        int(current(storage_id)) if callable(current) else capture_kb_epoch(storage_id)
    )


def ha_authority_guard(
    request: Request,
    scope: KnowledgeBaseScope,
    expected_epoch: int,
    *,
    permission: Permission,
) -> Callable[[], None]:
    """Freeze and repeatedly revalidate a shared HA resource authority."""

    principal = request_principal(request)
    auth_context = getattr(request.state, "auth_context", None)
    access_store = getattr(request.app.state, "resource_access_store", None)
    auth_store = getattr(request.app.state, "auth_store", None)
    expected_acl_epoch = (
        int(access_store.acl_epoch(scope.tenant_id, scope.storage_id))
        if access_store is not None
        else 0
    )
    session = getattr(auth_context, "session", None)
    service_account = getattr(auth_context, "service_account", None)
    service_token = getattr(auth_context, "token", None)
    evidence: dict[str, Any] = {
        "tenant_id": scope.tenant_id,
        "storage_id": scope.storage_id,
        "kb_epoch": expected_epoch,
        "acl_epoch": expected_acl_epoch,
        "acl_required": access_store is not None,
        "auth_kind": "api_principal",
        "subject_id": principal.subject_id,
        "role": principal.role.value,
        "permission": permission.value,
    }
    if isinstance(session, Mapping):
        evidence.update(
            {
                "auth_kind": "user_session",
                "session_id": str(session.get("session_id") or ""),
                "membership_id": str(
                    session.get("membership_id") or principal.membership_id or ""
                ),
            }
        )
    elif isinstance(service_account, Mapping) and isinstance(service_token, Mapping):
        evidence.update(
            {
                "auth_kind": "service_account",
                "service_account_id": str(
                    service_account.get("service_account_id") or ""
                ),
                "token_id": str(service_token.get("token_id") or ""),
            }
        )

    def guard() -> None:
        try:
            registry = request.app.state.kb_registry
            current = getattr(registry, "current", None)
            status = getattr(registry, "status", None)
            if callable(current) and callable(status):
                row = registry.get_by_storage_id(scope.storage_id)
                if (
                    row is None
                    or str(row.get("tenant_id") or "default") != scope.tenant_id
                    or str(status(scope.storage_id)) != "active"
                    or int(current(scope.storage_id)) != expected_epoch
                ):
                    raise KBIncarnationChanged("shared chat KB authority changed")
            else:
                assert_active_kb_incarnation(
                    registry,
                    scope.tenant_id,
                    scope.storage_id,
                    expected_epoch,
                )
        except KBIncarnationChanged as exc:
            raise HAChatAuthorityChanged("chat knowledge base changed") from exc
        live_principal = principal
        if isinstance(session, Mapping):
            if auth_store is None:
                raise HAChatAuthorityChanged("chat login session is unavailable")
            session_id = str(session.get("session_id") or "")
            membership_id = str(
                session.get("membership_id") or principal.membership_id or ""
            )
            if not auth_store.session_is_active(
                session_id=session_id,
                user_id=principal.subject_id,
                workspace_id=scope.tenant_id,
            ):
                raise HAChatAuthorityChanged("chat login session was revoked")
            membership = auth_store.membership(scope.tenant_id, principal.subject_id)
            if not isinstance(membership, Mapping):
                raise HAChatAuthorityChanged("chat membership was revoked")
            live_membership_id = str(
                membership.get("member_id") or membership.get("membership_id") or ""
            )
            if not membership_id or live_membership_id != membership_id:
                raise HAChatAuthorityChanged("chat membership incarnation changed")
            try:
                live_role = Role(str(membership.get("role") or ""))
            except (TypeError, ValueError) as exc:
                raise HAChatAuthorityChanged("chat membership role is invalid") from exc
            live_principal = Principal(
                tenant_id=scope.tenant_id,
                subject_id=principal.subject_id,
                role=live_role,
                key_fingerprint=principal.key_fingerprint,
                membership_id=live_membership_id,
            )
        elif isinstance(service_account, Mapping) and isinstance(
            service_token, Mapping
        ):
            active: Any = getattr(auth_store, "service_token_is_active", None)
            if (
                auth_store is None
                or not callable(active)
                or not active(
                    workspace_id=scope.tenant_id,
                    service_account_id=str(
                        service_account.get("service_account_id") or ""
                    ),
                    token_id=str(service_token.get("token_id") or ""),
                    permission=permission,
                )
            ):
                raise HAChatAuthorityChanged("chat service token was revoked")
        if access_store is not None:
            if not access_store.is_epoch_current(
                scope.tenant_id, scope.storage_id, expected_acl_epoch
            ):
                raise HAChatAuthorityChanged("chat ACL changed during execution")
            decision = access_store.allowed_sources(
                live_principal,
                scope.storage_id,
                tenant_id=scope.tenant_id,
                permission=permission,
            )
            if not decision.is_allowed:
                raise HAChatAuthorityChanged("chat access was revoked")

    guard()
    setattr(guard, "evidence", evidence)
    return guard


def ha_chat_authority_guard(
    request: Request,
    scope: KnowledgeBaseScope,
    expected_epoch: int,
) -> Callable[[], None]:
    return ha_authority_guard(
        request,
        scope,
        expected_epoch,
        permission=Permission.QUERY,
    )


__all__ = [
    "HAChatAuthorityChanged",
    "capture_ha_chat_epoch",
    "ha_authority_guard",
    "ha_chat_authority_guard",
]
