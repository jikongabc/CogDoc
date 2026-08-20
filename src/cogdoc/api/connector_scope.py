from __future__ import annotations

from collections.abc import Callable
from typing import Any

from cogdoc.service.kb_epoch import shared_epoch_store
from cogdoc.service.kb_lifecycle import LIFECYCLE_ACTIVE, shared_lifecycle_store
from cogdoc.service.kb_locks import kb_write_lock


class KBIncarnationChanged(RuntimeError):
    """A control-plane mutation outlived the KB incarnation that authorized it."""


def capture_kb_epoch(storage_id: str) -> int:
    return shared_epoch_store().current(storage_id)


def assert_active_kb_incarnation(
    registry: Any,
    tenant_id: str,
    storage_id: str,
    expected_epoch: int,
) -> None:
    record = registry.get_by_storage_id(storage_id)
    if (
        record is None
        or str(record.get("tenant_id") or "default") != tenant_id
        or shared_lifecycle_store().status(storage_id) != LIFECYCLE_ACTIVE
        or shared_epoch_store().current(storage_id) != expected_epoch
    ):
        raise KBIncarnationChanged(
            "knowledge base changed while the connector mutation was pending"
        )


def guarded_kb_mutation(
    registry: Any,
    guard_tenant_id: str,
    guard_storage_id: str,
    guard_expected_epoch: int,
    operation: Callable[..., Any],
    *args: Any,
    **kwargs: Any,
) -> Any:
    """Revalidate and mutate under the same per-KB authority lock."""

    with kb_write_lock(guard_storage_id):
        assert_active_kb_incarnation(
            registry,
            guard_tenant_id,
            guard_storage_id,
            guard_expected_epoch,
        )
        return operation(*args, **kwargs)
