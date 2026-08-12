from __future__ import annotations

from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient
import pytest

from cogdoc.api.access_control import AccessControlMiddleware, TokenBucketRateLimiter
from cogdoc.api.eval_review_auth import require_eval_reviewer
from cogdoc.api.tenancy import Principal


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _app(*, api_key: str, role: str, explicit: bool, legacy_reviewer: bool = False):
    app = FastAPI()
    principal = Principal.for_api_key(
        api_key,
        tenant_id="team-a",
        subject_id="alice",
        role=role,
    )
    app.state.eval_review_api_keys = {api_key} if legacy_reviewer else set()
    app.state.explicit_principal_fingerprints = (
        {principal.key_fingerprint} if explicit else set()
    )

    @app.post("/v1/research-jobs/job-1/publish")
    async def publish(actor: str = Depends(require_eval_reviewer)):
        return {"actor": actor}

    app.add_middleware(
        AccessControlMiddleware,
        api_keys=set() if explicit else {api_key},
        principals={api_key: principal} if explicit else None,
        rate_limiter=TokenBucketRateLimiter(0, 0),
    )
    return app


@pytest.mark.anyio
async def test_explicit_reviewer_principal_is_the_audit_actor():
    app = _app(api_key="reviewer-key", role="reviewer", explicit=True)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/v1/research-jobs/job-1/publish",
            headers={"X-API-Key": "reviewer-key"},
        )

    assert response.status_code == 200
    assert response.json() == {"actor": "alice"}


@pytest.mark.anyio
async def test_legacy_admin_still_requires_independent_reviewer_key():
    app = _app(api_key="legacy-key", role="admin", explicit=False)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/v1/research-jobs/job-1/publish",
            headers={"X-API-Key": "legacy-key"},
        )

    assert response.status_code == 403


@pytest.mark.anyio
async def test_legacy_independent_reviewer_key_remains_compatible():
    app = _app(
        api_key="legacy-reviewer",
        role="admin",
        explicit=False,
        legacy_reviewer=True,
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/v1/research-jobs/job-1/publish",
            headers={"X-API-Key": "legacy-reviewer"},
        )

    assert response.status_code == 200
    assert response.json()["actor"].startswith("eval-review:")
