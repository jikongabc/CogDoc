from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Mapping

from fastapi import Request

from cogdoc.api.tenancy import Permission, Principal, required_permission
from cogdoc.service.kb_lifecycle import LIFECYCLE_ACTIVE, shared_lifecycle_store
from cogdoc.tools.retriever.scope import RetrievalAccessMode, RetrievalScope


class PhysicalIdentityProjectionError(RuntimeError):
    """Raised rather than exposing an unresolved internal storage identity."""


@dataclass(frozen=True, slots=True)
class KnowledgeBaseScope:
    tenant_id: str
    external_id: str
    storage_id: str
    owner_id: str
    created_at: str = ""


def request_principal(request: Request) -> Principal:
    try:
        principal = request.state.principal
    except (AttributeError, KeyError, TypeError):
        principal = None
    if not isinstance(principal, Principal):
        # Tenant projection is an authorization boundary.  Tests and internal
        # callers must install an explicit principal instead of silently
        # inheriting owner authority when middleware was skipped or failed.
        raise RuntimeError("authenticated request principal is unavailable")
    return principal


def is_user_session_principal(principal: Principal) -> bool:
    return principal.key_fingerprint.startswith("session:")


def _uses_private_session_namespace(request: Request, principal: Principal) -> bool:
    explicit: set[str] | frozenset[str] = getattr(
        request.app.state, "explicit_principal_fingerprints", frozenset()
    )
    return (
        is_user_session_principal(principal)
        or principal.key_fingerprint.startswith("service-token:")
        or principal.key_fingerprint in explicit
    )


def _user_namespace(principal: Principal) -> str:
    return hashlib.sha256(
        b"cogdoc-user-resource-v1\0"
        + principal.tenant_id.encode("utf-8")
        + b"\0"
        + principal.subject_id.encode("utf-8")
    ).hexdigest()


def session_store_doc_id(request: Request, storage_id: str) -> str:
    """Namespace chat memory per real user while preserving legacy API-key data."""

    principal = request_principal(request)
    if not _uses_private_session_namespace(request, principal):
        return storage_id
    return f"{storage_id}~u-{_user_namespace(principal)}"


def internal_session_id(request: Request, session_id: str | None) -> str | None:
    """Namespace trace/session correlation IDs so peers cannot guess each other."""

    if not session_id:
        return session_id
    principal = request_principal(request)
    if not _uses_private_session_namespace(request, principal):
        return session_id
    return f"u-{_user_namespace(principal)}:{session_id}"


def external_session_id(request: Request, session_id: str) -> str | None:
    principal = request_principal(request)
    if not _uses_private_session_namespace(request, principal):
        return session_id
    prefix = f"u-{_user_namespace(principal)}:"
    return session_id[len(prefix) :] if session_id.startswith(prefix) else None


def session_id_is_authorized(request: Request, session_id: str | None) -> bool:
    principal = request_principal(request)
    if not _uses_private_session_namespace(request, principal):
        return True
    if not session_id:
        return False
    return session_id.startswith(f"u-{_user_namespace(principal)}:")


def _scope_from_record(record: Mapping[str, Any]) -> KnowledgeBaseScope:
    external_id = str(record.get("kb_id") or "")
    return KnowledgeBaseScope(
        tenant_id=str(record.get("tenant_id") or "default"),
        external_id=external_id,
        storage_id=str(record.get("storage_id") or external_id),
        owner_id=str(record.get("owner_id") or "default"),
        created_at=str(record.get("created_at") or ""),
    )


def _lifecycle_active(storage_id: str) -> bool:
    try:
        return shared_lifecycle_store().status(storage_id) == LIFECYCLE_ACTIVE
    except Exception:
        # Lifecycle state is an authorization boundary for incarnation reuse.
        return False


def _requested_permission(request: Request) -> Permission:
    return required_permission(request.method, request.url.path)


def _access_decision(
    request: Request,
    scope: KnowledgeBaseScope,
    *,
    permission: Permission | None = None,
) -> Any | None:
    """Return a resource decision, or ``None`` in explicit legacy mode.

    Merely having no ACL store remains the backwards-compatible, single-user
    deployment contract. Once a store is configured, every exception and every
    malformed decision is a denial at the callers below.
    """

    store = getattr(request.app.state, "resource_access_store", None)
    if store is None:
        return None
    resolver = getattr(store, "allowed_sources", None)
    if not callable(resolver):
        return False
    try:
        return resolver(
            request_principal(request),
            scope.storage_id,
            tenant_id=scope.tenant_id,
            permission=permission or _requested_permission(request),
        )
    except Exception:
        return False


def _decision_is_allowed(decision: Any | None) -> bool:
    if decision is None:
        return True
    return bool(decision is not False and getattr(decision, "is_allowed", False))


def resource_access_decision(
    request: Request,
    scope: KnowledgeBaseScope,
    *,
    permission: Permission | None = None,
) -> Any | None:
    """Expose the immutable ACL snapshot without exposing physical IDs."""

    return _access_decision(request, scope, permission=permission)


def retrieval_scope_for_request(
    request: Request, scope: KnowledgeBaseScope
) -> RetrievalScope:
    """Translate the persisted ACL snapshot into an explicit retrieval scope."""

    decision = _access_decision(request, scope, permission=Permission.QUERY)
    if decision is None:
        return RetrievalScope()
    if decision is False or not getattr(decision, "is_allowed", False):
        return RetrievalScope.deny()
    mode = str(getattr(getattr(decision, "mode", None), "value", ""))
    if mode == "all":
        return RetrievalScope(access_mode=RetrievalAccessMode.ALL)
    if mode == "subset":
        sources = tuple(str(item) for item in decision.allowed_sources if item)
        if sources:
            return RetrievalScope(
                allowed_sources=sources,
                access_mode=RetrievalAccessMode.SUBSET,
            )
    return RetrievalScope.deny()


def source_is_authorized(
    request: Request,
    scope: KnowledgeBaseScope,
    source: str,
    *,
    permission: Permission | None = None,
) -> bool:
    decision = _access_decision(request, scope, permission=permission)
    if decision is None:
        return True
    if decision is False or not getattr(decision, "is_allowed", False):
        return False
    allows_source = getattr(decision, "allows_source", None)
    return bool(callable(allows_source) and allows_source(source))


def _resource_references(value: Any) -> tuple[set[str], set[str]]:
    """Collect stable document/source references from a persisted API row."""

    document_ids: set[str] = set()
    sources: set[str] = set()

    def visit(item: Any, depth: int) -> None:
        if depth > 10:
            return
        if isinstance(item, Mapping):
            for raw_key, nested in item.items():
                key = str(raw_key)
                if key in {"document_id", "related_document_id"} and isinstance(
                    nested, str
                ):
                    if nested:
                        document_ids.add(nested)
                elif key in {"source", "related_source"} and isinstance(nested, str):
                    if nested and not nested.startswith("knowledge:"):
                        sources.add(nested)
                else:
                    visit(nested, depth + 1)
        elif isinstance(item, (list, tuple)):
            for nested in item[:1000]:
                visit(nested, depth + 1)

    visit(value, 0)
    return document_ids, sources


def row_is_authorized(
    request: Request,
    scope: KnowledgeBaseScope,
    row: Mapping[str, Any],
    *,
    permission: Permission | None = None,
) -> bool:
    """Apply document ACLs to feedback, knowledge, traces, and eval artifacts."""

    decision = _access_decision(request, scope, permission=permission)
    if decision is None:
        return True
    if decision is False or not getattr(decision, "is_allowed", False):
        return False
    mode = str(getattr(getattr(decision, "mode", None), "value", ""))
    if mode == "all":
        return True
    if mode != "subset":
        return False
    document_ids, sources = _resource_references(row)
    allows_document = getattr(decision, "allows_document_id", None)
    allows_source = getattr(decision, "allows_source", None)
    if not callable(allows_document) or not callable(allows_source):
        return False
    # Legacy ``related_document_id`` values were sometimes source filenames,
    # so require each value to match either stable identity namespace.
    if any(
        not (allows_document(document_id) or allows_source(document_id))
        for document_id in document_ids
    ):
        return False
    if any(not allows_source(source) for source in sources):
        return False
    if document_ids or sources:
        return True
    # Unbound derived records are creator-private under a subset grant.
    created_by = str(row.get("created_by") or "")
    return bool(created_by and created_by == request_principal(request).subject_id)


def resolve_kb_scope(
    request: Request,
    kb_id: str,
    *,
    allow_legacy_default: bool = False,
    allow_inactive: bool = False,
) -> KnowledgeBaseScope | None:
    """Resolve an external tenant-local slug to its internal physical ID."""

    principal = request_principal(request)
    registry = request.app.state.kb_registry
    resolver = getattr(registry, "resolve", None)
    record = None
    try:
        if callable(resolver):
            record = resolver(kb_id, principal.tenant_id)
        elif principal.tenant_id == "default":
            record = registry.get(kb_id)
    except (TypeError, ValueError):
        # User-controlled path/query IDs are an ordinary miss when they do not
        # satisfy the registry's canonical identity contract.
        record = None
    if isinstance(record, Mapping):
        scope = _scope_from_record(record)
        active = _lifecycle_active(scope.storage_id)
        decision_allowed = _decision_is_allowed(_access_decision(request, scope))
        cleanup_authorized = bool(
            allow_inactive and not active and principal.allows(Permission.DELETE)
        )
        if (
            scope.tenant_id == principal.tenant_id
            and (active or allow_inactive)
            and (decision_allowed or cleanup_authorized)
        ):
            return scope
        return None
    if (
        allow_legacy_default
        and principal.tenant_id == "default"
        and not is_user_session_principal(principal)
    ):
        # Never reinterpret a named tenant's physical identifier as a legacy
        # default-tenant slug.  The non-default namespace is reserved even
        # after a registry entry is deleted, so orphaned index data cannot be
        # reached by guessing the deterministic storage ID.
        physical_getter = getattr(registry, "get_by_storage_id", None)
        physical_record = physical_getter(kb_id) if callable(physical_getter) else None
        if isinstance(physical_record, Mapping) or kb_id.startswith("t-"):
            return None
        # Pre-registry test runners and legacy default-KB deployments used the
        # public ID directly throughout the runtime. This fallback never exists
        # for named tenants, so it cannot cross a tenant boundary.
        legacy_scope = KnowledgeBaseScope(
            tenant_id="default",
            external_id=kb_id,
            storage_id=kb_id,
            owner_id="default",
            created_at="",
        )
        return (
            legacy_scope
            if (allow_inactive or _lifecycle_active(legacy_scope.storage_id))
            and _decision_is_allowed(_access_decision(request, legacy_scope))
            else None
        )
    return None


def scope_for_storage_id(
    request: Request,
    storage_id: str,
    *,
    permission: Permission | None = None,
) -> KnowledgeBaseScope | None:
    """Authorize an opaque persisted KB ID against the request tenant."""

    principal = request_principal(request)
    registry = request.app.state.kb_registry
    getter = getattr(registry, "get_by_storage_id", None)
    try:
        record = getter(storage_id) if callable(getter) else registry.get(storage_id)
    except (TypeError, ValueError):
        return None
    if not isinstance(record, Mapping):
        return None
    scope = _scope_from_record(record)
    if scope.tenant_id != principal.tenant_id:
        return None
    if not _lifecycle_active(scope.storage_id):
        return None
    return (
        scope
        if _decision_is_allowed(_access_decision(request, scope, permission=permission))
        else None
    )


def tenant_kb_scopes(request: Request) -> list[KnowledgeBaseScope]:
    principal = request_principal(request)
    registry = request.app.state.kb_registry
    try:
        rows = registry.list(tenant_id=principal.tenant_id)
    except TypeError:
        rows = [
            row
            for row in registry.list()
            if str(row.get("tenant_id") or "default") == principal.tenant_id
        ]
    scopes = [_scope_from_record(row) for row in rows]
    return [
        scope
        for scope in scopes
        if _lifecycle_active(scope.storage_id)
        and _decision_is_allowed(_access_decision(request, scope))
    ]


def tenant_storage_ids(request: Request) -> set[str]:
    return {scope.storage_id for scope in tenant_kb_scopes(request)}


def external_kb_id(request: Request, storage_id: str) -> str | None:
    scope = scope_for_storage_id(request, storage_id)
    return scope.external_id if scope is not None else None


def externalize_kb_fields(value: Any, request: Request) -> Any:
    """Copy a public payload and replace only explicit KB identity fields.

    This is intentionally not a generic string replacement: document text,
    reports, source names, and audit prose must never be rewritten merely
    because they happen to equal an internal storage identifier.
    """

    if isinstance(value, Mapping):
        result = {}
        for key, item in value.items():
            if key in {"kb_id", "doc_id"} and isinstance(item, str):
                external_id = external_kb_id(request, item)
                if external_id is not None:
                    result[key] = external_id
                elif item.startswith("t-"):
                    raise PhysicalIdentityProjectionError(
                        "internal knowledge-base identity cannot be projected"
                    )
                else:
                    result[key] = item
            else:
                result[key] = externalize_kb_fields(item, request)
        return result
    if isinstance(value, list):
        return [externalize_kb_fields(item, request) for item in value]
    if isinstance(value, tuple):
        return tuple(externalize_kb_fields(item, request) for item in value)
    return value


def row_belongs_to_tenant(request: Request, row: Mapping[str, Any] | None) -> bool:
    if not isinstance(row, Mapping):
        return False
    storage_id = str(row.get("kb_id") or row.get("doc_id") or "")
    scope = scope_for_storage_id(request, storage_id) if storage_id else None
    return bool(scope is not None and row_is_authorized(request, scope, row))


__all__ = [
    "KnowledgeBaseScope",
    "PhysicalIdentityProjectionError",
    "external_kb_id",
    "externalize_kb_fields",
    "external_session_id",
    "internal_session_id",
    "is_user_session_principal",
    "request_principal",
    "resource_access_decision",
    "resolve_kb_scope",
    "retrieval_scope_for_request",
    "row_belongs_to_tenant",
    "row_is_authorized",
    "scope_for_storage_id",
    "session_store_doc_id",
    "session_id_is_authorized",
    "source_is_authorized",
    "tenant_kb_scopes",
    "tenant_storage_ids",
]
