from types import SimpleNamespace

import pytest

from cogdoc.api.routes import tasks as tasks_route


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_workspace_task_list_ignores_stale_unknown_kb(monkeypatch):
    async def immediate(_executor, function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(tasks_route, "run_sync", immediate)
    monkeypatch.setattr(
        tasks_route,
        "tenant_kb_scopes",
        lambda _request: [
            SimpleNamespace(storage_id="known-storage", external_id="known")
        ],
    )
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                offload_executor=None,
                connector_sync_store=SimpleNamespace(
                    list_workspace_jobs=lambda *_args, **_kwargs: [
                        {"kb_id": "stale-storage"}
                    ]
                ),
            )
        )
    )

    response = await tasks_route.list_workspace_sync_jobs(request)

    assert response.jobs == []
