from __future__ import annotations

import hashlib

from cogdoc.ha.migrations import Migration
from cogdoc.ha.storage import DatabaseBackend


CURRENT_SCHEMA_VERSION = 1
MINIMUM_SCHEMA_VERSION = 1

_BASELINE_TABLES = (
    "ha_index_generations",
    "ha_index_heads",
    "ha_job_keys",
    "ha_jobs",
    "ha_outbox",
    "ha_schedule_fires",
    "ha_schedules",
)


def _baseline_is_present(backend: DatabaseBackend) -> bool:
    with backend.transaction() as connection:
        for table in _BASELINE_TABLES:
            try:
                connection.execute(f"SELECT 1 FROM {table} WHERE 1=0")
            except Exception:
                return False
    return True


_BASELINE_CONTRACT = "cogdoc-ha-control-plane-v1:" + ",".join(_BASELINE_TABLES)
REGISTERED_MIGRATIONS = (
    Migration(
        version=CURRENT_SCHEMA_VERSION,
        name="HA control-plane baseline",
        checksum=hashlib.sha256(_BASELINE_CONTRACT.encode()).hexdigest(),
        validate=_baseline_is_present,
    ),
)


__all__ = [
    "CURRENT_SCHEMA_VERSION",
    "MINIMUM_SCHEMA_VERSION",
    "REGISTERED_MIGRATIONS",
]
