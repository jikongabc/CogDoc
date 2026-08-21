"""High-availability control-plane primitives.

The package is intentionally independent from CogDoc's local index formats.
SQLite remains the zero-configuration backend; PostgreSQL and object storage
can be enabled for horizontally scaled deployments.
"""

from cogdoc.ha.postgres import PostgresBackend
from cogdoc.ha.object_store import (
    LocalObjectStore,
    ObjectIndexRepository,
    S3ObjectStore,
)
from cogdoc.ha.outbox import OutboxDispatcher, OutboxStore, WebhookOutboxHandler
from cogdoc.ha.migrations import Migration, MigrationRunner
from cogdoc.ha.runtime import DistributedIndexWorker, HAConfig, HARuntime
from cogdoc.ha.maintenance import HAMaintenance, MaintenanceSnapshot
from cogdoc.ha.storage import DatabaseBackend, SQLiteBackend, StorageError
from cogdoc.ha.tasks import LeaseJobStore, StaleJobLease

__all__ = [
    "DatabaseBackend",
    "DistributedIndexWorker",
    "HAMaintenance",
    "HAConfig",
    "HARuntime",
    "MaintenanceSnapshot",
    "LeaseJobStore",
    "LocalObjectStore",
    "Migration",
    "MigrationRunner",
    "ObjectIndexRepository",
    "OutboxDispatcher",
    "OutboxStore",
    "WebhookOutboxHandler",
    "PostgresBackend",
    "SQLiteBackend",
    "S3ObjectStore",
    "StaleJobLease",
    "StorageError",
]
