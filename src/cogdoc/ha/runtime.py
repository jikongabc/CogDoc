from __future__ import annotations

import hashlib
import importlib.metadata
import logging
import math
import os
import socket
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from cogdoc.ha.index_generation import (
    GEN_PREPARED,
    GEN_PUBLISHED,
    IndexAuthorityGuard,
    IndexGenerationStore,
)
from cogdoc.ha.index_replica import HAIndexReplica
from cogdoc.ha.invalidation import CacheInvalidationFeed, HACacheInvalidator
from cogdoc.ha.maintenance import HAMaintenance
from cogdoc.ha.migration_catalog import (
    CURRENT_SCHEMA_VERSION,
    MINIMUM_SCHEMA_VERSION,
    REGISTERED_MIGRATIONS,
    migrations_are_current,
)
from cogdoc.ha.migrations import MigrationRunner
from cogdoc.ha.object_store import (
    LocalObjectStore,
    ObjectIndexRepository,
    ObjectStore,
    S3ObjectStore,
)
from cogdoc.ha.outbox import EventHandler, OutboxDispatcher, OutboxStore
from cogdoc.ha.postgres import PostgresBackend
from cogdoc.ha.scheduler import DistributedScheduler, ScheduleStore
from cogdoc.ha.source_generation import SourceGenerationStore
from cogdoc.ha.storage import DatabaseBackend, DatabaseConnection, SQLiteBackend
from cogdoc.ha.tasks import LeaseJobStore
from cogdoc.ha.versioning import ApplicationVersionRegistry, VersionHeartbeat


class HAConfigurationError(RuntimeError, ValueError):
    pass


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class HAConfig:
    enabled: bool
    database_url: str
    database_schema: str
    object_store: str
    object_root: str
    s3_bucket: str
    s3_prefix: str
    s3_endpoint_url: str | None
    s3_region: str | None
    s3_require_versioning: bool
    worker_id: str
    scheduler_enabled: bool
    outbox_enabled: bool
    maintenance_enabled: bool = True
    maintenance_interval_seconds: float = 30.0
    retention_seconds: float = 7 * 86_400.0
    scrub_interval_seconds: float = 3600.0
    maintenance_batch_size: int = 100
    index_worker_enabled: bool = True
    index_worker_count: int = 2
    index_worker_poll_seconds: float = 0.5
    index_worker_lease_seconds: float = 300.0
    research_worker_poll_seconds: float = 0.5
    research_worker_lease_seconds: float = 120.0
    chat_session_lease_seconds: float = 300.0
    chat_index_reader_lease_seconds: float = 600.0
    chat_max_sessions_per_scope: int = 1024
    chat_session_ttl_seconds: int = 604800
    chat_max_display_messages: int = 2000
    chat_max_session_bytes: int = 4 * 1024 * 1024
    release_id: str = "unknown"
    minimum_schema_version: int = MINIMUM_SCHEMA_VERSION
    maximum_schema_version: int = CURRENT_SCHEMA_VERSION
    version_heartbeat_interval_seconds: float = 30.0
    version_heartbeat_ttl_seconds: float = 90.0
    index_reads_enabled: bool = True
    index_replica_cache_root: str = ""
    api_multi_writer_enabled: bool = False
    mutation_lease_seconds: float = 300.0
    source_cache_root: str = ""
    source_max_files: int = 100_000
    source_max_total_bytes: int = 10 * 1024 * 1024 * 1024
    source_artifact_max_file_bytes: int = 100 * 1024 * 1024
    source_artifact_max_total_bytes: int = 10 * 1024 * 1024 * 1024
    source_artifact_max_tenant_bytes: int = 512 * 1024 * 1024
    source_artifact_max_versions: int = 10

    @classmethod
    def from_settings(cls, settings: Any) -> HAConfig:
        configured_worker = str(settings.cogdoc_ha_worker_id).strip()
        worker = configured_worker or f"{socket.gethostname()}-{os.getpid()}"
        object_root = str(settings.cogdoc_ha_object_root).strip() or str(
            Path(settings.cogdoc_data_dir) / "ha-objects"
        )
        configured_release = str(getattr(settings, "cogdoc_ha_release_id", "")).strip()
        try:
            package_release = importlib.metadata.version("cogdoc")
        except importlib.metadata.PackageNotFoundError:
            package_release = "development"
        config = cls(
            enabled=bool(settings.cogdoc_ha_enabled),
            database_url=str(settings.cogdoc_ha_database_url).strip(),
            database_schema=str(settings.cogdoc_ha_database_schema),
            object_store=str(settings.cogdoc_ha_object_store),
            object_root=object_root,
            s3_bucket=str(settings.cogdoc_ha_s3_bucket).strip(),
            s3_prefix=str(settings.cogdoc_ha_s3_prefix).strip(),
            s3_endpoint_url=str(settings.cogdoc_ha_s3_endpoint_url).strip() or None,
            s3_region=str(settings.cogdoc_ha_s3_region).strip() or None,
            s3_require_versioning=bool(settings.cogdoc_ha_s3_require_versioning),
            worker_id=worker,
            scheduler_enabled=bool(settings.cogdoc_ha_scheduler_enabled),
            outbox_enabled=bool(settings.cogdoc_ha_outbox_enabled),
            maintenance_enabled=bool(
                getattr(settings, "cogdoc_ha_maintenance_enabled", True)
            ),
            maintenance_interval_seconds=float(
                getattr(settings, "cogdoc_ha_maintenance_interval_seconds", 30.0)
            ),
            retention_seconds=float(
                getattr(settings, "cogdoc_ha_retention_seconds", 7 * 86_400.0)
            ),
            scrub_interval_seconds=float(
                getattr(settings, "cogdoc_ha_scrub_interval_seconds", 3600.0)
            ),
            maintenance_batch_size=int(
                getattr(settings, "cogdoc_ha_maintenance_batch_size", 100)
            ),
            index_worker_enabled=bool(
                getattr(settings, "cogdoc_ha_index_worker_enabled", True)
            ),
            index_worker_count=int(
                getattr(settings, "cogdoc_ha_index_worker_count", 2)
            ),
            index_worker_poll_seconds=float(
                getattr(settings, "cogdoc_ha_index_worker_poll_seconds", 0.5)
            ),
            index_worker_lease_seconds=float(
                getattr(settings, "cogdoc_ha_index_worker_lease_seconds", 300.0)
            ),
            research_worker_poll_seconds=float(
                getattr(settings, "cogdoc_ha_research_worker_poll_seconds", 0.5)
            ),
            research_worker_lease_seconds=float(
                getattr(settings, "cogdoc_ha_research_worker_lease_seconds", 120.0)
            ),
            chat_session_lease_seconds=float(
                getattr(settings, "cogdoc_ha_chat_session_lease_seconds", 300.0)
            ),
            chat_index_reader_lease_seconds=float(
                getattr(settings, "cogdoc_ha_chat_index_reader_lease_seconds", 600.0)
            ),
            chat_max_sessions_per_scope=int(
                getattr(settings, "cogdoc_ha_chat_max_sessions_per_scope", 1024)
            ),
            chat_session_ttl_seconds=int(
                getattr(settings, "cogdoc_ha_chat_session_ttl_seconds", 604800)
            ),
            chat_max_display_messages=int(
                getattr(settings, "cogdoc_ha_chat_max_display_messages", 2000)
            ),
            chat_max_session_bytes=int(
                getattr(settings, "cogdoc_ha_chat_max_session_bytes", 4 * 1024 * 1024)
            ),
            release_id=configured_release or package_release,
            minimum_schema_version=int(
                getattr(
                    settings,
                    "cogdoc_ha_minimum_schema_version",
                    MINIMUM_SCHEMA_VERSION,
                )
            ),
            maximum_schema_version=int(
                getattr(
                    settings,
                    "cogdoc_ha_maximum_schema_version",
                    CURRENT_SCHEMA_VERSION,
                )
            ),
            version_heartbeat_interval_seconds=float(
                getattr(
                    settings,
                    "cogdoc_ha_version_heartbeat_interval_seconds",
                    30.0,
                )
            ),
            version_heartbeat_ttl_seconds=float(
                getattr(
                    settings,
                    "cogdoc_ha_version_heartbeat_ttl_seconds",
                    90.0,
                )
            ),
            index_reads_enabled=bool(
                getattr(settings, "cogdoc_ha_index_reads_enabled", True)
            ),
            index_replica_cache_root=str(
                getattr(settings, "cogdoc_ha_index_replica_cache_root", "")
            ).strip()
            or str(Path(settings.cogdoc_data_dir) / "ha-index-cache"),
            api_multi_writer_enabled=bool(
                getattr(settings, "cogdoc_ha_api_multi_writer_enabled", False)
            ),
            mutation_lease_seconds=float(
                getattr(settings, "cogdoc_ha_mutation_lease_seconds", 300.0)
            ),
            source_cache_root=str(
                getattr(settings, "cogdoc_ha_source_cache_root", "")
            ).strip()
            or str(Path(settings.cogdoc_data_dir) / "ha-source-cache"),
            source_max_files=int(
                getattr(settings, "cogdoc_ha_source_max_files", 100_000)
            ),
            source_max_total_bytes=int(
                getattr(
                    settings,
                    "cogdoc_ha_source_max_total_bytes",
                    10 * 1024 * 1024 * 1024,
                )
            ),
            source_artifact_max_file_bytes=int(
                getattr(settings, "cogdoc_source_artifact_max_file_mb", 100)
            )
            * 1024
            * 1024,
            source_artifact_max_total_bytes=int(
                getattr(
                    settings,
                    "cogdoc_ha_source_artifact_max_total_bytes",
                    10 * 1024 * 1024 * 1024,
                )
            ),
            source_artifact_max_tenant_bytes=int(
                getattr(settings, "cogdoc_source_artifact_max_tenant_mb", 512)
            )
            * 1024
            * 1024,
            source_artifact_max_versions=int(
                getattr(settings, "cogdoc_source_artifact_max_versions", 10)
            ),
        )
        config.validate()
        return config

    def validate(self) -> None:
        if not self.enabled:
            return
        if not self.worker_id or len(self.worker_id) > 255:
            raise HAConfigurationError("COGDOC_HA_WORKER_ID is invalid")
        if self.database_url and not self.database_url.startswith(
            ("postgresql://", "postgres://")
        ):
            raise HAConfigurationError("COGDOC_HA_DATABASE_URL must be PostgreSQL")
        if self.object_store == "s3":
            if not self.s3_bucket:
                raise HAConfigurationError("COGDOC_HA_S3_BUCKET is required")
            if not self.s3_require_versioning:
                raise HAConfigurationError("HA S3 storage requires bucket versioning")
        elif self.object_store != "local":
            raise HAConfigurationError("COGDOC_HA_OBJECT_STORE must be local or s3")
        if (
            not math.isfinite(self.maintenance_interval_seconds)
            or not 1 <= self.maintenance_interval_seconds <= 3600
        ):
            raise HAConfigurationError("HA maintenance interval is invalid")
        if not math.isfinite(self.retention_seconds) or self.retention_seconds < 60:
            raise HAConfigurationError("HA retention is invalid")
        if (
            not math.isfinite(self.scrub_interval_seconds)
            or self.scrub_interval_seconds < self.maintenance_interval_seconds
        ):
            raise HAConfigurationError("HA scrub interval is invalid")
        if not 1 <= self.maintenance_batch_size <= 1000:
            raise HAConfigurationError("HA maintenance batch size is invalid")
        if not 1 <= self.index_worker_count <= 32:
            raise HAConfigurationError("HA index worker count is invalid")
        if (
            not math.isfinite(self.index_worker_poll_seconds)
            or not 0.05 <= self.index_worker_poll_seconds <= 60
        ):
            raise HAConfigurationError("HA index worker poll interval is invalid")
        if (
            not math.isfinite(self.index_worker_lease_seconds)
            or not 5 <= self.index_worker_lease_seconds <= 3600
        ):
            raise HAConfigurationError("HA index worker lease is invalid")
        if (
            not math.isfinite(self.research_worker_poll_seconds)
            or not 0.05 <= self.research_worker_poll_seconds <= 60
        ):
            raise HAConfigurationError("HA research worker poll interval is invalid")
        if (
            not math.isfinite(self.research_worker_lease_seconds)
            or not 5 <= self.research_worker_lease_seconds <= 3600
        ):
            raise HAConfigurationError("HA research worker lease is invalid")
        if (
            not math.isfinite(self.chat_session_lease_seconds)
            or not 5 <= self.chat_session_lease_seconds <= 3600
            or not math.isfinite(self.chat_index_reader_lease_seconds)
            or not 15 <= self.chat_index_reader_lease_seconds <= 3600
        ):
            raise HAConfigurationError("HA chat lease configuration is invalid")
        if (
            type(self.chat_max_sessions_per_scope) is not int
            or not 1 <= self.chat_max_sessions_per_scope <= 100_000
            or type(self.chat_session_ttl_seconds) is not int
            or not 0 <= self.chat_session_ttl_seconds <= 10 * 365 * 86400
            or type(self.chat_max_display_messages) is not int
            or not 2 <= self.chat_max_display_messages <= 100_000
            or self.chat_max_display_messages % 2
            or type(self.chat_max_session_bytes) is not int
            or not 4096 <= self.chat_max_session_bytes <= 128 * 1024 * 1024
        ):
            raise HAConfigurationError("HA chat storage limits are invalid")
        if (
            not self.release_id
            or self.release_id != self.release_id.strip()
            or len(self.release_id.encode()) > 255
            or any(ord(character) < 32 for character in self.release_id)
        ):
            raise HAConfigurationError("HA release id is invalid")
        if (
            type(self.minimum_schema_version) is not int
            or type(self.maximum_schema_version) is not int
            or not 1
            <= self.minimum_schema_version
            <= self.maximum_schema_version
            <= CURRENT_SCHEMA_VERSION
        ):
            raise HAConfigurationError("HA schema compatibility range is invalid")
        if (
            not math.isfinite(self.version_heartbeat_interval_seconds)
            or not math.isfinite(self.version_heartbeat_ttl_seconds)
            or not 5
            <= self.version_heartbeat_interval_seconds
            < self.version_heartbeat_ttl_seconds
            <= 3600
        ):
            raise HAConfigurationError("HA version heartbeat timing is invalid")
        if (
            not math.isfinite(self.mutation_lease_seconds)
            or not 5 <= self.mutation_lease_seconds <= 3600
        ):
            raise HAConfigurationError("HA mutation lease is invalid")
        if not 1 <= self.source_max_files <= 1_000_000:
            raise HAConfigurationError("HA source file limit is invalid")
        if self.source_max_total_bytes < 1:
            raise HAConfigurationError("HA source byte limit is invalid")
        if (
            self.source_artifact_max_file_bytes < 1
            or self.source_artifact_max_total_bytes < 1
            or self.source_artifact_max_tenant_bytes < 1
            or self.source_artifact_max_versions < 1
        ):
            raise HAConfigurationError("HA source artifact limits are invalid")
        if self.api_multi_writer_enabled and not (
            self.database_url.startswith(("postgresql://", "postgres://"))
            and self.object_store == "s3"
            and self.s3_require_versioning
        ):
            raise HAConfigurationError(
                "multi-writer API requires PostgreSQL and versioned S3"
            )

    @property
    def multi_instance_safe(self) -> bool:
        return bool(
            self.enabled
            and self.database_url.startswith(("postgresql://", "postgres://"))
            and self.object_store == "s3"
            and self.s3_require_versioning
        )

    @property
    def api_multi_writer_safe(self) -> bool:
        return self.multi_instance_safe and self.api_multi_writer_enabled


class HARuntime:
    """Owned lifecycle for the distributed scheduler, queue and publication plane."""

    def __init__(
        self,
        config: HAConfig,
        *,
        backend: DatabaseBackend | None = None,
        object_store: ObjectStore | None = None,
        outbox_handler: EventHandler | None = None,
        s3_client: Any | None = None,
        index_builder: IndexBuilder | None = None,
    ) -> None:
        config.validate()
        if not config.enabled:
            raise HAConfigurationError("HA runtime is disabled")
        self.config = config
        self._owns_backend = backend is None
        selected_backend: DatabaseBackend
        if backend is None:
            selected_backend = cast(
                DatabaseBackend,
                PostgresBackend(config.database_url, schema=config.database_schema)
                if config.database_url
                else SQLiteBackend(Path(config.object_root).parent / "ha-control.db"),
            )
        else:
            selected_backend = backend
        self.backend = selected_backend
        if object_store is None:
            object_store = (
                S3ObjectStore(
                    config.s3_bucket,
                    prefix=config.s3_prefix,
                    client=s3_client,
                    endpoint_url=config.s3_endpoint_url,
                    region_name=config.s3_region,
                    require_versioning=config.s3_require_versioning,
                )
                if config.object_store == "s3"
                else LocalObjectStore(config.object_root)
            )
        self.object_store = object_store
        self.index_repository = ObjectIndexRepository(object_store)
        self.jobs = LeaseJobStore(selected_backend)
        self.schedules = ScheduleStore(selected_backend)
        self.scheduler = DistributedScheduler(self.schedules, self.jobs)
        self.index_generations = IndexGenerationStore(selected_backend)
        replica_root = config.index_replica_cache_root or str(
            Path(config.object_root).parent / "ha-index-cache"
        )
        self.index_replica = (
            HAIndexReplica(
                self.index_generations,
                self.index_repository,
                replica_root,
            )
            if config.index_reads_enabled
            else None
        )
        self.outbox = OutboxStore(selected_backend)
        self.source_generations = SourceGenerationStore(
            selected_backend,
            object_store,
            outbox=self.outbox,
            max_files=config.source_max_files,
            max_total_bytes=config.source_max_total_bytes,
        )
        self.connector_commits: Any | None = None
        self.connector_reference_lock_factory: Any | None = None
        from cogdoc.ha.source_catalog import DistributedSourceCatalog
        from cogdoc.ha.source_artifact_store import DistributedSourceArtifactStore

        self.source_catalog = DistributedSourceCatalog(selected_backend)
        self.source_artifact_store = DistributedSourceArtifactStore(
            selected_backend,
            object_store,
            owner_id=config.worker_id,
            max_file_bytes=config.source_artifact_max_file_bytes,
            max_total_bytes=config.source_artifact_max_total_bytes,
            max_bytes_per_tenant=config.source_artifact_max_tenant_bytes,
            max_versions_per_source=config.source_artifact_max_versions + 1,
            user_max_versions_per_source=config.source_artifact_max_versions,
            reservation_lease_seconds=max(60.0, config.mutation_lease_seconds),
        )
        from cogdoc.ha.recovery import HARecoveryManifest

        self.recovery = HARecoveryManifest(
            selected_backend, object_store, self.source_generations
        )
        # Bound by the production API only when every mutation authority is
        # shared. Worker-only HA deployments leave these unset.
        self.api_registry: Any | None = None
        self.api_mutation_coordinator: Any | None = None
        self.api_kb_deletion: Any | None = None
        self.tenant_quota_manager: Any | None = None
        self.cache_invalidation_feed: CacheInvalidationFeed | None = None
        self.versions = ApplicationVersionRegistry(selected_backend)
        self.version_heartbeat = VersionHeartbeat(
            self.versions,
            instance_id=config.worker_id,
            release_id=config.release_id,
            minimum_schema_version=config.minimum_schema_version,
            maximum_schema_version=config.maximum_schema_version,
            interval_seconds=config.version_heartbeat_interval_seconds,
            ttl_seconds=config.version_heartbeat_ttl_seconds,
        )
        self.maintenance = HAMaintenance(
            self.jobs,
            self.schedules,
            self.outbox,
            self.index_generations,
            self.index_repository,
            source_generations=self.source_generations,
            source_artifacts=self.source_artifact_store,
            interval_seconds=config.maintenance_interval_seconds,
            retention_seconds=config.retention_seconds,
            scrub_interval_seconds=config.scrub_interval_seconds,
            batch_size=config.maintenance_batch_size,
        )
        self.outbox_dispatcher = (
            OutboxDispatcher(
                self.outbox,
                outbox_handler,
                worker_id=f"{config.worker_id}-outbox",
            )
            if outbox_handler is not None and config.outbox_enabled
            else None
        )
        self.index_worker = (
            DistributedIndexWorker(
                self,
                index_builder or _prepared_generation_only,
                lease_seconds=config.index_worker_lease_seconds,
                worker_count=config.index_worker_count,
                idle_seconds=config.index_worker_poll_seconds,
            )
            if config.index_worker_enabled
            else None
        )
        self._started = False
        self._lock = threading.RLock()
        # Local/CI mode is a single-process authority and can safely bootstrap
        # through the same checksummed migration runner. Production PostgreSQL
        # remains operator-controlled and readiness fails until `cogdoc-ha
        # migrate` has completed under its advisory lock.
        if selected_backend.kind == "sqlite":
            MigrationRunner(
                selected_backend,
                REGISTERED_MIGRATIONS,
                owner_id=f"{config.worker_id}-bootstrap",
            ).run()

    @property
    def multi_instance_safe(self) -> bool:
        return self.config.multi_instance_safe and self.backend.kind == "postgres"

    @property
    def api_multi_writer_safe(self) -> bool:
        return self.config.api_multi_writer_safe and self.backend.kind == "postgres"

    def bind_api_cache_invalidation(self, registry: Any) -> None:
        with self._lock:
            if self._started:
                raise RuntimeError("cannot bind cache invalidation after HA startup")
            if self.cache_invalidation_feed is not None:
                raise RuntimeError("cache invalidation is already bound")
            self.api_registry = registry
            self.cache_invalidation_feed = CacheInvalidationFeed(
                self.backend,
                HACacheInvalidator(self.index_replica, registry),
                consumer_id=self.config.worker_id,
                interval_seconds=self.config.index_worker_poll_seconds,
            )

    def bind_api_tenant_quota(self, policy: Any) -> Any:
        with self._lock:
            if self._started:
                raise RuntimeError("cannot bind tenant quota after HA startup")
            if self.tenant_quota_manager is not None:
                raise RuntimeError("tenant quota is already bound")
            from cogdoc.ha.tenant_quota import DistributedTenantQuotaManager

            self.tenant_quota_manager = DistributedTenantQuotaManager(
                self.backend,
                self.source_generations,
                policy,
                owner_id=self.config.worker_id,
                lease_seconds=self.config.mutation_lease_seconds,
            )
            return self.tenant_quota_manager

    def bind_api_kb_deletion(self, registry: Any) -> Any:
        with self._lock:
            if self._started:
                raise RuntimeError("cannot bind KB deletion after HA startup")
            if self.api_kb_deletion is not None:
                raise RuntimeError("KB deletion is already bound")
            if self.api_registry is not None and self.api_registry is not registry:
                raise ValueError("HA API registry identity does not match")
            from cogdoc.ha.kb_deletion import DistributedKBDeletionCoordinator

            self.api_registry = registry
            coordinator = DistributedKBDeletionCoordinator(
                self.backend,
                registry,
                self.index_generations,
                self.source_generations,
                self.source_catalog,
                self.source_artifact_store,
                self.outbox,
            )
            self.api_kb_deletion = coordinator
            bind_create_hook = getattr(registry, "bind_create_hook", None)
            if not callable(bind_create_hook):
                raise ValueError(
                    "HA API registry does not support atomic KB activation"
                )
            bind_create_hook(coordinator.activate_created_kb)
            self.index_generations.bind_authority_guard(
                cast(IndexAuthorityGuard, coordinator.assert_index_active)
            )
            if self.api_mutation_coordinator is not None:
                from cogdoc.ha.connector_commit import (
                    DistributedConnectorCommitStore,
                )

                self.connector_commits = DistributedConnectorCommitStore(
                    self.backend,
                    self.object_store,
                    self.api_mutation_coordinator,
                    max_files=self.config.source_max_files,
                    max_total_bytes=self.config.source_max_total_bytes,
                )
            return coordinator

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self.backend.reopen()
            try:
                if not self.check():
                    raise RuntimeError("HA runtime dependencies are not ready")
                if self.api_kb_deletion is not None:
                    recovered = self.api_kb_deletion.recover(limit=100)
                    if recovered["failed"]:
                        raise RuntimeError("HA KB deletion recovery is incomplete")
                self.version_heartbeat.start()
                if self.tenant_quota_manager is not None:
                    self.tenant_quota_manager.start()
                self.source_artifact_store.start()
                if self.cache_invalidation_feed is not None:
                    self.cache_invalidation_feed.start()
                if self.config.scheduler_enabled:
                    self.scheduler.start()
                if self.outbox_dispatcher is not None:
                    self.outbox_dispatcher.start()
                if self.config.maintenance_enabled:
                    self.maintenance.start()
                if self.index_worker is not None:
                    self.index_worker.start()
            except BaseException as exc:
                cleanup_errors = self._stop_background()
                if self._owns_backend and not cleanup_errors:
                    self.backend.close()
                if cleanup_errors:
                    raise RuntimeError(
                        "HA runtime startup failed and background rollback did not stop"
                    ) from exc
                raise
            self._started = True

    def shutdown(self) -> None:
        with self._lock:
            cleanup_errors = self._stop_background()
            if cleanup_errors:
                raise RuntimeError(
                    "HA runtime background workers did not stop"
                ) from cleanup_errors[0]
            self._started = False
            if self._owns_backend:
                self.backend.close()

    def _stop_background(self) -> list[BaseException]:
        errors: list[BaseException] = []
        try:
            if not self.source_artifact_store.stop():
                errors.append(
                    TimeoutError("artifact reservation heartbeat did not stop")
                )
        except BaseException as exc:
            errors.append(exc)
        if self.tenant_quota_manager is not None:
            try:
                if not self.tenant_quota_manager.stop():
                    errors.append(TimeoutError("tenant quota heartbeat did not stop"))
            except BaseException as exc:
                errors.append(exc)
        if self.cache_invalidation_feed is not None:
            try:
                if not self.cache_invalidation_feed.stop():
                    errors.append(TimeoutError("cache invalidation feed did not stop"))
            except BaseException as exc:
                errors.append(exc)
        if self.index_worker is not None:
            try:
                if not self.index_worker.stop():
                    errors.append(TimeoutError("HA index worker did not stop"))
            except BaseException as exc:
                errors.append(exc)
        if self.config.maintenance_enabled:
            try:
                if not self.maintenance.stop():
                    errors.append(TimeoutError("HA maintenance did not stop"))
            except BaseException as exc:
                errors.append(exc)
        if self.outbox_dispatcher is not None:
            try:
                self.outbox_dispatcher.stop()
            except BaseException as exc:
                errors.append(exc)
        if self.config.scheduler_enabled:
            try:
                if not self.scheduler.stop():
                    errors.append(TimeoutError("HA scheduler did not stop"))
            except BaseException as exc:
                errors.append(exc)
        try:
            if not self.version_heartbeat.stop():
                errors.append(TimeoutError("HA version heartbeat did not stop"))
        except BaseException as exc:
            errors.append(exc)
        return errors

    def check(self) -> bool:
        try:
            dependencies = (
                self.backend.check() is True
                and migrations_are_current(self.backend)
                and self.object_store.check() is True
                and self.source_catalog.check() is True
                and self.source_artifact_store.check() is True
                and (
                    self.connector_commits is None
                    or self.connector_commits.check() is True
                )
            )
            maintenance_ready = (
                not self._started
                or not self.config.maintenance_enabled
                or self.maintenance.check()
            )
            worker_ready = (
                not self._started
                or self.index_worker is None
                or self.index_worker.check()
            )
            version_ready = not self._started or self.version_heartbeat.check()
            scheduler_ready = (
                not self._started
                or not self.config.scheduler_enabled
                or self.scheduler.check()
            )
            outbox_ready = (
                not self._started
                or self.outbox_dispatcher is None
                or self.outbox_dispatcher.check()
            )
            invalidation_ready = (
                not self._started
                or self.cache_invalidation_feed is None
                or self.cache_invalidation_feed.check()
            )
            quota_ready = (
                not self._started
                or self.tenant_quota_manager is None
                or self.tenant_quota_manager.check()
            )
            deletion_ready = (
                self.api_kb_deletion is None or self.api_kb_deletion.check()
            )
            return (
                dependencies
                and maintenance_ready
                and worker_ready
                and version_ready
                and scheduler_ready
                and outbox_ready
                and invalidation_ready
                and quota_ready
                and deletion_ready
            )
        except Exception:
            return False

    def publish_generation(
        self,
        generation: Mapping[str, Any],
        *,
        publication_hook: Callable[[DatabaseConnection, Mapping[str, Any]], Any]
        | None = None,
    ) -> dict[str, Any]:
        tenant_id = str(generation["tenant_id"])
        kb_id = str(generation["kb_id"])

        def append_event(
            connection: DatabaseConnection, generation: Mapping[str, Any]
        ) -> None:
            if publication_hook is not None:
                publication_hook(connection, generation)
            self.outbox.append(
                connection,
                tenant_id=tenant_id,
                topic="index.published",
                aggregate_type="knowledge_base",
                aggregate_id=kb_id,
                aggregate_revision=int(generation["fencing_token"]),
                payload={
                    "kb_id": kb_id,
                    "generation_id": generation["generation_id"],
                    "manifest_sha256": generation["manifest_sha256"],
                },
                idempotency_key=f"index:{generation['generation_id']}",
            )

        return self.index_generations.publish(
            str(generation["generation_id"]),
            str(generation["lease_token"]),
            self.index_repository.verify,
            on_publish=append_event,
        )


IndexBuilder = Callable[[Mapping[str, Any]], tuple[Mapping[str, Any], Path]]


def _prepared_generation_only(
    _job: Mapping[str, Any],
) -> tuple[Mapping[str, Any], Path]:
    raise RuntimeError(
        "index job has no prepared immutable generation; upload and prepare it first"
    )


class DistributedIndexWorker:
    """Lease-fenced worker that publishes only verified immutable generations."""

    QUEUE = "index-build"

    def __init__(
        self,
        runtime: HARuntime,
        builder: IndexBuilder,
        *,
        lease_seconds: float = 300,
        worker_count: int = 1,
        idle_seconds: float = 0.5,
    ) -> None:
        self.runtime = runtime
        self.builder = builder
        if not math.isfinite(lease_seconds) or not 5 <= lease_seconds <= 3600:
            raise ValueError("index worker lease_seconds must be between 5 and 3600")
        self.lease_seconds = lease_seconds
        if type(worker_count) is not int or not 1 <= worker_count <= 32:
            raise ValueError("index worker_count must be between 1 and 32")
        if not math.isfinite(idle_seconds) or not 0.05 <= idle_seconds <= 60:
            raise ValueError("index worker idle_seconds must be between 0.05 and 60")
        self.worker_count = worker_count
        self.idle_seconds = float(idle_seconds)
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._threads: list[threading.Thread] = []
        self._lifecycle_lock = threading.Lock()

    def enqueue(
        self,
        tenant_id: str,
        kb_id: str,
        build_id: str,
        payload: Mapping[str, Any] | None = None,
        *,
        generation_id: str | None = None,
        generation_lease_token: str | None = None,
    ) -> dict[str, Any]:
        if (generation_id is None) != (generation_lease_token is None):
            raise ValueError(
                "prepared generation id and lease token must be provided together"
            )
        body: dict[str, Any] = {
            "tenant_id": tenant_id,
            "kb_id": kb_id,
            "build_id": build_id,
            "input": dict(payload or {}),
        }
        if generation_id is not None:
            assert generation_lease_token is not None
            body["generation_id"] = generation_id
            body["generation_lease_token"] = generation_lease_token
        job = self.runtime.jobs.enqueue(
            self.QUEUE,
            tenant_id,
            body,
            idempotency_key=f"{kb_id}:{build_id}",
            max_attempts=10,
        )
        self._wake.set()
        return job

    def start(self) -> None:
        with self._lifecycle_lock:
            if any(thread.is_alive() for thread in self._threads):
                return
            self._stop.clear()
            self._threads = [
                threading.Thread(
                    target=self._run,
                    name=f"cogdoc-ha-index-{index + 1}",
                    daemon=True,
                )
                for index in range(self.worker_count)
            ]
            for thread in self._threads:
                thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                worked = self.run_once()
            except Exception:
                LOGGER.exception("HA index worker iteration failed")
                worked = False
            if not worked:
                self._wake.wait(self.idle_seconds)
                self._wake.clear()

    def stop(self, *, timeout_seconds: float = 10.0) -> bool:
        self._stop.set()
        self._wake.set()
        deadline = time.monotonic() + timeout_seconds
        for thread in self._threads:
            thread.join(max(0.0, deadline - time.monotonic()))
        stopped = all(not thread.is_alive() for thread in self._threads)
        if stopped:
            self._threads = []
        return stopped

    def check(self) -> bool:
        return len(self._threads) == self.worker_count and all(
            thread.is_alive() for thread in self._threads
        )

    def run_once(self) -> bool:
        job = self.runtime.jobs.claim(
            self.QUEUE,
            self.runtime.config.worker_id,
            lease_seconds=self.lease_seconds,
        )
        if job is None:
            return False
        token = str(job["lease_token"])
        finished = threading.Event()
        lease_lost = threading.Event()
        generation_lease: dict[str, str] = {}

        def keep_leases() -> None:
            interval = max(1.0, self.lease_seconds / 3)
            while not finished.wait(interval):
                try:
                    self.runtime.jobs.heartbeat(
                        str(job["job_id"]), token, lease_seconds=self.lease_seconds
                    )
                    if generation_lease:
                        self.runtime.index_generations.heartbeat(
                            generation_lease["generation_id"],
                            generation_lease["lease_token"],
                            lease_seconds=self.lease_seconds,
                        )
                except Exception:
                    lease_lost.set()
                    return

        keeper = threading.Thread(
            target=keep_leases,
            name=f"index-heartbeat-{job['job_id']}",
            daemon=True,
        )
        keeper.start()
        try:
            result = self._execute(job, generation_lease, lease_lost)
        except Exception as exc:
            finished.set()
            keeper.join()
            if not lease_lost.is_set():
                try:
                    self.runtime.jobs.fail(
                        str(job["job_id"]),
                        token,
                        type(exc).__name__.upper(),
                        retryable=True,
                        retry_delay_seconds=min(
                            300.0, 2 ** min(int(job["attempt"]), 8)
                        ),
                    )
                except Exception:
                    pass
        else:
            finished.set()
            keeper.join()
            if not lease_lost.is_set():
                self.runtime.jobs.complete(str(job["job_id"]), token, result)
        finally:
            finished.set()
        return True

    def _execute(
        self,
        job: Mapping[str, Any],
        generation_lease: dict[str, str],
        lease_lost: threading.Event,
    ) -> dict[str, Any]:
        payload = job["payload"]
        tenant_id = str(payload["tenant_id"])
        kb_id = str(payload["kb_id"])
        build_id = str(payload["build_id"])
        supplied_generation_id = payload.get("generation_id")
        supplied_lease_token = payload.get("generation_lease_token")
        if supplied_generation_id is not None and supplied_lease_token is not None:
            generation = self.runtime.index_generations.get(str(supplied_generation_id))
            if (
                generation is None
                or generation["tenant_id"] != tenant_id
                or generation["kb_id"] != kb_id
                or generation["build_id"] != build_id
                or generation["lease_token"] != str(supplied_lease_token)
            ):
                raise RuntimeError("prepared index generation handoff is invalid")
            if generation["status"] != GEN_PUBLISHED:
                try:
                    generation = self.runtime.index_generations.heartbeat(
                        str(generation["generation_id"]),
                        str(supplied_lease_token),
                        lease_seconds=self.lease_seconds,
                    )
                except Exception:
                    # Queue delivery may legitimately happen after the producer's
                    # generation lease expires.  resume_build rotates the lease
                    # capability only if this generation still owns the current
                    # fencing token; a concurrently adopted/superseded generation
                    # therefore fails closed.
                    generation = self.runtime.index_generations.resume_build(
                        str(generation["generation_id"]),
                        self.runtime.config.worker_id,
                        lease_seconds=self.lease_seconds,
                    )
        else:
            generation = self.runtime.index_generations.begin_build(
                tenant_id,
                kb_id,
                build_id,
                self.runtime.config.worker_id,
                lease_seconds=self.lease_seconds,
            )
        if generation["status"] == GEN_PUBLISHED:
            return {"generation_id": generation["generation_id"], "replayed": True}
        generation_lease.update(
            generation_id=str(generation["generation_id"]),
            lease_token=str(generation["lease_token"]),
        )
        if generation["status"] != GEN_PREPARED:
            manifest, source = self.builder(job)
            if lease_lost.is_set():
                raise RuntimeError("index worker lease was lost during build")
            generation = self.runtime.index_generations.prepare(
                str(generation["generation_id"]),
                str(generation["lease_token"]),
                manifest,
            )
            self.runtime.index_repository.materialize(generation, source)
        else:
            self.runtime.index_repository.verify(generation)
        if lease_lost.is_set():
            raise RuntimeError("index worker lease was lost before publication")

        published = self.runtime.publish_generation(generation)
        return {
            "generation_id": published["generation_id"],
            "manifest_sha256": published["manifest_sha256"],
        }


def manifest_for_directory(
    directory: str | os.PathLike[str], *, contract: Mapping[str, Any]
) -> dict[str, Any]:
    root = Path(directory).resolve(strict=True)
    files: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError("index directory contains a symlink")
        if not path.is_file():
            continue
        builder = hashlib.sha256()
        size = 0
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                builder.update(chunk)
                size += len(chunk)
        files.append(
            {
                "path": path.relative_to(root).as_posix(),
                "sha256": builder.hexdigest(),
                "byte_size": size,
            }
        )
    return {
        "schema_version": "index-manifest-v1",
        "contract": dict(contract),
        "files": files,
    }


__all__ = [
    "DistributedIndexWorker",
    "HAConfig",
    "HAConfigurationError",
    "HARuntime",
    "IndexBuilder",
    "manifest_for_directory",
]
