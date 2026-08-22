from __future__ import annotations

import math
import time
from collections.abc import Callable, Mapping
from typing import Any

from cogdoc.ha.api_state import DistributedKnowledgeBaseRegistry, StaleMutationFence
from cogdoc.ha.index_generation import IndexGenerationStore
from cogdoc.ha.outbox import OutboxStore
from cogdoc.ha.session_store import DistributedSessionStore
from cogdoc.ha.source_artifact_store import DistributedSourceArtifactStore
from cogdoc.ha.source_catalog import DistributedSourceCatalog
from cogdoc.ha.source_generation import (
    SOURCE_ACTIVE,
    SOURCE_SUPERSEDED,
    SourceGenerationStore,
)
from cogdoc.ha.storage import DatabaseBackend, DatabaseConnection
from cogdoc.api.tenancy import Permission
from cogdoc.service.kb_lifecycle import (
    LIFECYCLE_ACTIVE,
    LIFECYCLE_DELETED,
    LIFECYCLE_DELETING,
)


DELETE_FENCED = "fenced"
DELETE_CLEANED = "cleaned"
DELETE_COMPLETE = "deleted"
_DELETE_PHASES = {DELETE_FENCED, DELETE_CLEANED, DELETE_COMPLETE}


class DistributedKBDeletionError(RuntimeError):
    pass


def _row(value: Any | None) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, Mapping):
        return dict(value)
    keys = getattr(value, "keys", None)
    if callable(keys):
        return {str(key): value[key] for key in keys()}
    raise DistributedKBDeletionError("KB deletion row mapping is unavailable")


def _identity(value: object, field: str) -> str:
    text = str(value or "")
    if (
        not text
        or text != text.strip()
        or len(text.encode()) > 255
        or any(ord(char) < 32 or ord(char) == 127 for char in text)
    ):
        raise ValueError(f"{field} is invalid")
    return text


class DistributedKBDeletionCoordinator:
    """Crash-resumable deletion of one shared HA knowledge-base incarnation."""

    def __init__(
        self,
        backend: DatabaseBackend,
        registry: DistributedKnowledgeBaseRegistry,
        index_generations: IndexGenerationStore,
        source_generations: SourceGenerationStore,
        source_catalog: DistributedSourceCatalog,
        source_artifacts: DistributedSourceArtifactStore,
        outbox: OutboxStore,
        *,
        clock: Any = time.time,
    ) -> None:
        stores = (
            registry,
            index_generations,
            source_generations,
            source_catalog,
            source_artifacts,
            outbox,
        )
        if any(getattr(store, "backend", None) is not backend for store in stores):
            raise ValueError("KB deletion stores must share one database backend")
        self.backend = backend
        self.registry = registry
        self.index_generations = index_generations
        self.source_generations = source_generations
        self.source_catalog = source_catalog
        self.source_artifacts = source_artifacts
        self.chat_sessions = DistributedSessionStore(backend)
        self.outbox = outbox
        self._clock = clock
        self._control_plane_cleanups: list[Callable[[str, str], None]] = []
        with backend.transaction(write=True) as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS ha_api_kb_deletions (
                storage_id TEXT PRIMARY KEY,tenant_id TEXT NOT NULL,kb_epoch BIGINT NOT NULL,
                phase TEXT NOT NULL,index_generation_id TEXT,source_generation_id TEXT,
                artifact_versions BIGINT NOT NULL DEFAULT 0,
                catalog_documents BIGINT NOT NULL DEFAULT 0,
                started_at DOUBLE PRECISION NOT NULL,updated_at DOUBLE PRECISION NOT NULL)"""
            )

    def bind_control_plane_cleanup(
        self, cleanup: Callable[[str, str], None]
    ) -> None:
        """Bind the idempotent shared-state cleanup before HA startup.

        The deletion fence is already durable when this callback runs.  A
        callback failure therefore leaves the saga in ``fenced`` and recovery
        safely repeats it. Multiple in-process API replicas may bind their
        node-local auxiliary cleanup; every registered callback must converge.
        """

        if not callable(cleanup):
            raise TypeError("KB control-plane cleanup must be callable")
        if cleanup not in self._control_plane_cleanups:
            self._control_plane_cleanups.append(cleanup)

    def _marker(self) -> str:
        return self.backend.sql(sqlite="?", postgres="%s")

    def _get_locked(
        self, connection: DatabaseConnection, storage_id: str
    ) -> dict[str, Any] | None:
        marker = self._marker()
        lock = self.backend.sql(sqlite="", postgres=" FOR UPDATE")
        return _row(
            connection.execute(
                f"SELECT * FROM ha_api_kb_deletions WHERE storage_id={marker}{lock}",
                (storage_id,),
            ).fetchone()
        )

    def assert_index_active(
        self, connection: DatabaseConnection, tenant_id: str, storage_id: str
    ) -> None:
        tenant = _identity(tenant_id, "tenant_id")
        storage = _identity(storage_id, "storage_id")
        marker = self._marker()
        lock = self.backend.sql(sqlite="", postgres=" FOR UPDATE")
        kb = _row(
            connection.execute(
                "SELECT tenant_id,lifecycle FROM ha_api_knowledge_bases WHERE storage_id="
                f"{marker}{lock}",
                (storage,),
            ).fetchone()
        )
        if (
            kb is None
            or str(kb["tenant_id"]) != tenant
            or kb["lifecycle"] != LIFECYCLE_ACTIVE
        ):
            raise StaleMutationFence("index publication KB is not active")

    def get(self, storage_id: str) -> dict[str, Any] | None:
        storage = _identity(storage_id, "storage_id")
        marker = self._marker()
        with self.backend.transaction() as connection:
            row = _row(
                connection.execute(
                    f"SELECT * FROM ha_api_kb_deletions WHERE storage_id={marker}",
                    (storage,),
                ).fetchone()
            )
        return row

    def activate(self, tenant_id: str, storage_id: str, *, kb_epoch: int) -> None:
        tenant = _identity(tenant_id, "tenant_id")
        storage = _identity(storage_id, "storage_id")
        if type(kb_epoch) is not int or kb_epoch < 1:
            raise ValueError("KB activation epoch is invalid")
        marker = self._marker()
        lock = self.backend.sql(sqlite="", postgres=" FOR UPDATE")
        now = float(self._clock())
        with self.backend.transaction(write=True) as connection:
            kb = _row(
                connection.execute(
                    "SELECT tenant_id,lifecycle,epoch FROM ha_api_knowledge_bases "
                    f"WHERE storage_id={marker}{lock}",
                    (storage,),
                ).fetchone()
            )
            if (
                kb is None
                or str(kb["tenant_id"]) != tenant
                or kb["lifecycle"] != LIFECYCLE_ACTIVE
                or int(kb["epoch"]) != kb_epoch
            ):
                raise StaleMutationFence("KB activation incarnation changed")
            self._activate_locked(connection, tenant, storage, kb_epoch, now)

    def activate_created_kb(
        self, connection: DatabaseConnection, record: Mapping[str, Any]
    ) -> None:
        """Activate a newly created incarnation in its registry transaction."""

        tenant = _identity(record.get("tenant_id"), "tenant_id")
        storage = _identity(record.get("storage_id"), "storage_id")
        kb_epoch = record.get("epoch")
        if type(kb_epoch) is not int or kb_epoch < 1:
            raise ValueError("KB activation epoch is invalid")
        self._activate_locked(
            connection, tenant, storage, kb_epoch, float(self._clock())
        )

    def _activate_locked(
        self,
        connection: DatabaseConnection,
        tenant: str,
        storage: str,
        kb_epoch: int,
        now: float,
    ) -> None:
        marker = self._marker()
        deletion = self._get_locked(connection, storage)
        if deletion is not None:
            if (
                deletion["phase"] != DELETE_COMPLETE
                or int(deletion["kb_epoch"]) >= kb_epoch
            ):
                raise StaleMutationFence("previous KB deletion is incomplete")
            connection.execute(
                f"DELETE FROM ha_api_kb_deletions WHERE storage_id={marker} "
                f"AND phase={marker} AND kb_epoch<{marker}",
                (storage, DELETE_COMPLETE, kb_epoch),
            )
        scope_upsert = self.backend.sql(
            sqlite=(
                "INSERT INTO ha_source_artifact_scopes(tenant_id,kb_id,state,kb_epoch,updated_at) "
                "VALUES(?,?,?,?,?) ON CONFLICT(tenant_id,kb_id) DO UPDATE SET "
                "state=excluded.state,kb_epoch=excluded.kb_epoch,updated_at=excluded.updated_at "
                "WHERE ha_source_artifact_scopes.kb_epoch<excluded.kb_epoch OR "
                "(ha_source_artifact_scopes.kb_epoch=excluded.kb_epoch AND "
                "ha_source_artifact_scopes.state='active')"
            ),
            postgres=(
                "INSERT INTO ha_source_artifact_scopes(tenant_id,kb_id,state,kb_epoch,updated_at) "
                "VALUES(%s,%s,%s,%s,%s) ON CONFLICT(tenant_id,kb_id) DO UPDATE SET "
                "state=EXCLUDED.state,kb_epoch=EXCLUDED.kb_epoch,updated_at=EXCLUDED.updated_at "
                "WHERE ha_source_artifact_scopes.kb_epoch<EXCLUDED.kb_epoch OR "
                "(ha_source_artifact_scopes.kb_epoch=EXCLUDED.kb_epoch AND "
                "ha_source_artifact_scopes.state='active')"
            ),
        )
        changed = connection.execute(
            scope_upsert, (tenant, storage, "active", kb_epoch, now)
        )
        if changed.rowcount != 1:
            raise StaleMutationFence("artifact scope activation was rejected")

    def _begin(
        self,
        tenant_id: str,
        storage_id: str,
        *,
        authority: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        tenant = _identity(tenant_id, "tenant_id")
        storage = _identity(storage_id, "storage_id")
        marker = self._marker()
        lock = self.backend.sql(sqlite="", postgres=" FOR UPDATE")
        now = float(self._clock())
        if not math.isfinite(now):
            raise ValueError("KB deletion clock is invalid")
        with self.backend.transaction(write=True) as connection:
            kb = _row(
                connection.execute(
                    "SELECT * FROM ha_api_knowledge_bases WHERE storage_id="
                    f"{marker}{lock}",
                    (storage,),
                ).fetchone()
            )
            if kb is None or str(kb["tenant_id"]) != tenant:
                raise KeyError(storage)
            if authority is not None:
                self.chat_sessions.check_authority_locked(
                    connection,
                    authority,
                    required_permission=Permission.DELETE,
                    now=now,
                )
            existing = self._get_locked(connection, storage)
            if kb["lifecycle"] == LIFECYCLE_DELETED:
                if existing is not None and existing["phase"] == DELETE_COMPLETE:
                    return existing
                raise KeyError(storage)
            if kb["lifecycle"] not in {LIFECYCLE_ACTIVE, LIFECYCLE_DELETING}:
                raise DistributedKBDeletionError("knowledge-base lifecycle is invalid")
            if existing is not None:
                if (
                    str(existing["tenant_id"]) != tenant
                    or int(existing["kb_epoch"]) != int(kb["epoch"])
                    or existing["phase"] not in _DELETE_PHASES
                ):
                    raise StaleMutationFence("KB deletion incarnation changed")
                return existing
            epoch = int(kb["epoch"])
            if kb["lifecycle"] == LIFECYCLE_ACTIVE:
                epoch += 1
                changed = connection.execute(
                    "UPDATE ha_api_knowledge_bases SET lifecycle="
                    f"{marker},epoch={marker},revision=revision+1,updated_at={marker} "
                    f"WHERE storage_id={marker} AND lifecycle={marker} AND epoch={marker}",
                    (
                        LIFECYCLE_DELETING,
                        epoch,
                        now,
                        storage,
                        LIFECYCLE_ACTIVE,
                        int(kb["epoch"]),
                    ),
                )
                if changed.rowcount != 1:
                    raise StaleMutationFence("KB deletion fence lost")
            index_head = _row(
                connection.execute(
                    "SELECT current_generation_id FROM ha_index_heads WHERE tenant_id="
                    f"{marker} AND kb_id={marker}",
                    (tenant, storage),
                ).fetchone()
            )
            source_head = _row(
                connection.execute(
                    f"SELECT generation_id FROM ha_source_heads WHERE storage_id={marker}",
                    (storage,),
                ).fetchone()
            )
            connection.execute(
                "INSERT INTO ha_api_kb_deletions(storage_id,tenant_id,kb_epoch,phase,"
                "index_generation_id,source_generation_id,artifact_versions,catalog_documents,"
                f"started_at,updated_at) VALUES({','.join(marker for _ in range(10))})",
                (
                    storage,
                    tenant,
                    epoch,
                    DELETE_FENCED,
                    (index_head or {}).get("current_generation_id"),
                    (source_head or {}).get("generation_id"),
                    0,
                    0,
                    now,
                    now,
                ),
            )
            connection.execute(
                f"UPDATE ha_api_mutation_leases SET lease_expires_at=0,updated_at={marker} "
                f"WHERE storage_id={marker}",
                (now, storage),
            )
            scope_upsert = self.backend.sql(
                sqlite=(
                    "INSERT INTO ha_source_artifact_scopes(tenant_id,kb_id,state,kb_epoch,updated_at) "
                    "VALUES(?,?,?,?,?) ON CONFLICT(tenant_id,kb_id) DO UPDATE SET "
                    "state=excluded.state,kb_epoch=excluded.kb_epoch,updated_at=excluded.updated_at"
                ),
                postgres=(
                    "INSERT INTO ha_source_artifact_scopes(tenant_id,kb_id,state,kb_epoch,updated_at) "
                    "VALUES(%s,%s,%s,%s,%s) ON CONFLICT(tenant_id,kb_id) DO UPDATE SET "
                    "state=EXCLUDED.state,kb_epoch=EXCLUDED.kb_epoch,updated_at=EXCLUDED.updated_at"
                ),
            )
            connection.execute(scope_upsert, (tenant, storage, "deleting", epoch, now))
            result = self._get_locked(connection, storage)
        assert result is not None
        return result

    def _clean(self, row: Mapping[str, Any]) -> dict[str, Any]:
        if row["phase"] != DELETE_FENCED:
            return dict(row)
        tenant = str(row["tenant_id"])
        storage = str(row["storage_id"])
        epoch = int(row["kb_epoch"])
        for cleanup in tuple(self._control_plane_cleanups):
            cleanup(tenant, storage)
        artifact_result = self.source_artifacts.delete_scope(
            tenant, storage, kb_epoch=epoch
        )
        catalog_result = self.source_catalog.delete_scope(tenant, storage)
        # The lifecycle fence committed by _begin prevents a late chat writer
        # from recreating either the bare API-principal scope or a per-user one.
        self.chat_sessions.clear_kb(storage)
        marker = self._marker()
        now = float(self._clock())
        with self.backend.transaction(write=True) as connection:
            current = self._get_locked(connection, storage)
            if current is None or int(current["kb_epoch"]) != epoch:
                raise StaleMutationFence("KB deletion cleanup incarnation changed")
            connection.execute(
                "UPDATE ha_api_kb_deletions SET phase="
                f"{marker},artifact_versions=artifact_versions+{marker},"
                f"catalog_documents=catalog_documents+{marker},updated_at={marker} "
                f"WHERE storage_id={marker} AND kb_epoch={marker} AND phase={marker}",
                (
                    DELETE_CLEANED,
                    int(artifact_result["active_versions"])
                    + int(artifact_result["trash_versions"]),
                    int(catalog_result["documents"]),
                    now,
                    storage,
                    epoch,
                    DELETE_FENCED,
                ),
            )
            result = self._get_locked(connection, storage)
        assert result is not None
        return result

    def _finalize(self, row: Mapping[str, Any]) -> dict[str, Any]:
        if row["phase"] == DELETE_COMPLETE:
            return dict(row)
        if row["phase"] != DELETE_CLEANED:
            raise DistributedKBDeletionError("KB deletion cleanup is incomplete")
        tenant = str(row["tenant_id"])
        storage = str(row["storage_id"])
        epoch = int(row["kb_epoch"])
        expected_index = row.get("index_generation_id")
        expected_source = row.get("source_generation_id")
        marker = self._marker()
        lock = self.backend.sql(sqlite="", postgres=" FOR UPDATE")
        now = float(self._clock())
        with self.backend.transaction(write=True) as connection:
            current = self._get_locked(connection, storage)
            kb = _row(
                connection.execute(
                    "SELECT * FROM ha_api_knowledge_bases WHERE storage_id="
                    f"{marker}{lock}",
                    (storage,),
                ).fetchone()
            )
            if current is None or kb is None:
                raise StaleMutationFence("KB deletion authority disappeared")
            if current["phase"] == DELETE_COMPLETE:
                return current
            if (
                current["phase"] != DELETE_CLEANED
                or int(current["kb_epoch"]) != epoch
                or kb["lifecycle"] != LIFECYCLE_DELETING
                or int(kb["epoch"]) != epoch
            ):
                raise StaleMutationFence("KB deletion finalization fence changed")
            index_head = _row(
                connection.execute(
                    "SELECT current_generation_id FROM ha_index_heads WHERE tenant_id="
                    f"{marker} AND kb_id={marker}{lock}",
                    (tenant, storage),
                ).fetchone()
            )
            source_head = _row(
                connection.execute(
                    f"SELECT generation_id FROM ha_source_heads WHERE storage_id={marker}{lock}",
                    (storage,),
                ).fetchone()
            )
            if (index_head or {}).get("current_generation_id") != expected_index or (
                source_head or {}
            ).get("generation_id") != expected_source:
                raise StaleMutationFence(
                    "KB generation head changed after deletion fence"
                )
            connection.execute(
                f"DELETE FROM ha_index_heads WHERE tenant_id={marker} AND kb_id={marker}",
                (tenant, storage),
            )
            connection.execute(
                f"DELETE FROM ha_source_heads WHERE storage_id={marker}", (storage,)
            )
            if expected_source is not None:
                connection.execute(
                    f"UPDATE ha_source_generations SET status={marker} WHERE generation_id={marker} "
                    f"AND status={marker}",
                    (SOURCE_SUPERSEDED, expected_source, SOURCE_ACTIVE),
                )
            revision = int(kb["revision"]) + 1
            changed = connection.execute(
                "UPDATE ha_api_knowledge_bases SET lifecycle="
                f"{marker},revision={marker},updated_at={marker} WHERE storage_id={marker} "
                f"AND lifecycle={marker} AND epoch={marker}",
                (
                    LIFECYCLE_DELETED,
                    revision,
                    now,
                    storage,
                    LIFECYCLE_DELETING,
                    epoch,
                ),
            )
            if changed.rowcount != 1:
                raise StaleMutationFence("KB tombstone CAS failed")
            connection.execute(
                f"UPDATE ha_api_kb_deletions SET phase={marker},updated_at={marker} "
                f"WHERE storage_id={marker} AND kb_epoch={marker} AND phase={marker}",
                (DELETE_COMPLETE, now, storage, epoch, DELETE_CLEANED),
            )
            self.outbox.append(
                connection,
                tenant_id=tenant,
                topic="kb.deleted",
                aggregate_type="knowledge_base",
                aggregate_id=storage,
                aggregate_revision=revision,
                payload={"storage_id": storage, "kb_epoch": epoch},
                idempotency_key=f"kb-deleted:{storage}:{epoch}",
            )
            result = self._get_locked(connection, storage)
        assert result is not None
        self._purge_local_cache(storage)
        return result

    def delete(
        self,
        tenant_id: str,
        storage_id: str,
        *,
        authority: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        row = self._begin(tenant_id, storage_id, authority=authority)
        row = self._clean(row)
        return self._finalize(row)

    def recover(self, *, limit: int = 100) -> dict[str, int]:
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("KB deletion recovery limit is invalid")
        recovered = failed = 0
        while True:
            with self.backend.transaction() as connection:
                rows = connection.execute(
                    "SELECT tenant_id,storage_id FROM ha_api_kb_deletions "
                    f"WHERE phase<>'{DELETE_COMPLETE}' "
                    f"ORDER BY started_at,storage_id LIMIT {limit}"
                ).fetchall()
            if not rows:
                break
            for raw in rows:
                candidate = _row(raw) or {}
                try:
                    self.delete(
                        str(candidate["tenant_id"]), str(candidate["storage_id"])
                    )
                    recovered += 1
                except Exception:
                    failed += 1
            # Do not spin forever on a deterministic external/storage failure.
            # Startup fails closed and the next retry resumes the same rows.
            if failed:
                break
        return {"recovered": recovered, "failed": failed}

    def _purge_local_cache(self, storage_id: str) -> None:
        try:
            self.registry.purge_cache(storage_id)
        except OSError:
            # Cache is non-authoritative; invalidation/restart can retry it.
            pass

    def check(self) -> bool:
        try:
            with self.backend.transaction() as connection:
                connection.execute("SELECT 1 FROM ha_api_kb_deletions LIMIT 1")
            return True
        except Exception:
            return False


__all__ = [
    "DELETE_CLEANED",
    "DELETE_COMPLETE",
    "DELETE_FENCED",
    "DistributedKBDeletionCoordinator",
    "DistributedKBDeletionError",
]
