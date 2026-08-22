from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from cogdoc.ha.connector_lock import DistributedConnectorReferenceLock
from cogdoc.ha.storage import SQLiteBackend


@pytest.mark.anyio
async def test_distributed_reference_lock_serializes_two_nodes(tmp_path: Path) -> None:
    path = tmp_path / "shared.db"
    first_backend = SQLiteBackend(path)
    second_backend = SQLiteBackend(path)
    with ThreadPoolExecutor(max_workers=4) as executor:
        first = DistributedConnectorReferenceLock(
            first_backend,
            owner_id="node-a",
            executor_provider=lambda: executor,
            lease_seconds=5,
            acquire_timeout_seconds=2,
        )
        second = DistributedConnectorReferenceLock(
            second_backend,
            owner_id="node-b",
            executor_provider=lambda: executor,
            lease_seconds=5,
            acquire_timeout_seconds=2,
        )
        entered: list[str] = []
        release = asyncio.Event()

        async def hold_first() -> None:
            async with first:
                entered.append("first")
                await release.wait()

        async def wait_second() -> None:
            async with second:
                entered.append("second")

        first_task = asyncio.create_task(hold_first())
        while not entered:
            await asyncio.sleep(0)
        second_task = asyncio.create_task(wait_second())
        await asyncio.sleep(0.1)
        assert entered == ["first"]
        release.set()
        await asyncio.gather(first_task, second_task)
        assert entered == ["first", "second"]
    first_backend.close()
    second_backend.close()


@pytest.mark.anyio
async def test_distributed_reference_lock_recovers_lost_executor_wakeup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = SQLiteBackend(tmp_path / "shared.db")
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "call_soon_threadsafe", loop.call_soon)
    with ThreadPoolExecutor(max_workers=1) as executor:
        lock = DistributedConnectorReferenceLock(
            backend,
            owner_id="node-a",
            executor_provider=lambda: executor,
            lease_seconds=5,
            acquire_timeout_seconds=1,
        )
        async with asyncio.timeout(1):
            async with lock:
                assert lock._token is not None
    backend.close()
