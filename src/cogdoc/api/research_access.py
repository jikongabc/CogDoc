from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from cogdoc.api.tenancy import Principal
from cogdoc.tools.retriever.scope import RetrievalAccessMode, RetrievalScope


def build_research_authorization(
    principal: Principal,
    decision: Any | None,
) -> dict[str, Any]:
    """Freeze the creator and exact document boundary for durable work."""

    if decision is None:
        return {
            "version": "research-auth-v1",
            "tenant_id": principal.tenant_id,
            "created_by": principal.subject_id,
            "mode": "all",
            "acl_epoch": 0,
            "allowed_sources": [],
        }
    mode = str(getattr(getattr(decision, "mode", None), "value", ""))
    sources = (
        sorted({str(item) for item in decision.allowed_sources if str(item)})
        if mode == "subset"
        else []
    )
    if mode not in {"all", "subset"} or (mode == "subset" and not sources):
        mode = "deny"
        sources = []
    return {
        "version": "research-auth-v1",
        "tenant_id": principal.tenant_id,
        "created_by": principal.subject_id,
        "creator_role": principal.role.value,
        "auth_kind": (
            "user_session"
            if principal.key_fingerprint.startswith("session:")
            else "service"
        ),
        "mode": mode,
        "acl_epoch": max(0, int(getattr(decision, "acl_epoch", 0))),
        "allowed_sources": sources,
    }


def research_authorization(job: Mapping[str, Any]) -> Mapping[str, Any] | None:
    value = job.get("authorization")
    if not isinstance(value, Mapping):
        return None
    if value.get("version") != "research-auth-v1":
        return None
    return value


def research_retrieval_scope(job: Mapping[str, Any]) -> RetrievalScope | None:
    """Return ``None`` only for an explicitly legacy, pre-auth research job."""

    authorization = research_authorization(job)
    if authorization is None:
        return None
    mode = str(authorization.get("mode") or "")
    if mode == "all":
        return RetrievalScope(access_mode=RetrievalAccessMode.ALL)
    if mode == "subset":
        sources = tuple(
            str(item)
            for item in authorization.get("allowed_sources") or ()
            if str(item)
        )
        if sources:
            return RetrievalScope(
                allowed_sources=sources,
                access_mode=RetrievalAccessMode.SUBSET,
            )
    return RetrievalScope.deny()


__all__ = [
    "build_research_authorization",
    "research_authorization",
    "research_retrieval_scope",
]
