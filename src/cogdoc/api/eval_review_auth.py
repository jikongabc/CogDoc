from __future__ import annotations

import hashlib
import hmac

from fastapi import HTTPException, Request

from cogdoc.api.tenancy import Permission, Principal


def _request_api_key(request: Request) -> str:
    authorization = request.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return request.headers.get("x-api-key", "").strip()


async def require_eval_reviewer(request: Request) -> str:
    """Authorize high-trust review actions and return a non-secret actor identity.

    Keep this lightweight dependency event-loop native.  Declaring it as a
    synchronous FastAPI dependency sends every review request through AnyIO's
    worker pool, which can leave an ASGI request waiting forever when a worker
    completion notification is missed.
    """

    # Explicit tenant API principals and authenticated account sessions are the
    # collaboration authority. The middleware has already validated the live
    # session and resolved the endpoint-specific REVIEW/PUBLISH permission.
    # Requiring a membership incarnation keeps fabricated/local principals and
    # legacy shared admin keys on the independent reviewer-key path below.
    principal = getattr(request.state, "principal", None)
    explicit_fingerprints = set(
        getattr(request.app.state, "explicit_principal_fingerprints", set())
    )
    is_account_session = (
        isinstance(principal, Principal)
        and principal.membership_id is not None
        and principal.key_fingerprint.startswith("session:")
    )
    if (
        isinstance(principal, Principal)
        and (
            principal.key_fingerprint in explicit_fingerprints
            or is_account_session
        )
        and (
            principal.allows(Permission.REVIEW)
            or principal.allows(Permission.PUBLISH)
        )
    ):
        return principal.subject_id

    # Legacy deployments retain their independent reviewer-key gate exactly as
    # before.  Merely being a shared default/admin API key is not sufficient.
    configured = set(getattr(request.app.state, "eval_review_api_keys", set()))
    if not configured:
        raise HTTPException(status_code=403, detail="独立审核接口未启用")
    supplied = _request_api_key(request)
    if not supplied or not any(
        hmac.compare_digest(supplied, expected) for expected in configured
    ):
        raise HTTPException(status_code=403, detail="需要独立审核权限")
    fingerprint = hashlib.sha256(supplied.encode("utf-8")).hexdigest()[:16]
    return f"eval-review:{fingerprint}"
