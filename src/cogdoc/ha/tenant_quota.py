from __future__ import annotations

import math
import os
import secrets
import threading
import time
from collections.abc import Mapping
from typing import Any

from cogdoc.api.tenant_quota import (
    TenantMutationInProgress,
    TenantQuotaExceeded,
    TenantQuotaManager,
    TenantQuotaPolicy,
    TenantQuotaReservationLost,
)
from cogdoc.ha.source_generation import SourceGenerationStore
from cogdoc.ha.storage import DatabaseBackend, DatabaseConnection
from cogdoc.service.kb_lifecycle import LIFECYCLE_DELETED


class DistributedTenantQuotaManager:
    """Cluster-authoritative tenant quotas with leased reservations."""

    def __init__(
        self,
        backend: DatabaseBackend,
        source_generations: SourceGenerationStore,
        policy: TenantQuotaPolicy,
        *,
        owner_id: str,
        lease_seconds: float = 300.0,
        clock: Any = time.time,
    ) -> None:
        if source_generations.backend is not backend:
            raise ValueError("quota and source generations must share a backend")
        if not owner_id or len(owner_id.encode()) > 255:
            raise ValueError("quota owner_id is invalid")
        if not math.isfinite(lease_seconds) or not 5 <= lease_seconds <= 3600:
            raise ValueError("quota lease_seconds must be between 5 and 3600")
        self.backend = backend
        self.source_generations = source_generations
        self.policy = policy
        self.owner_id = owner_id
        self.lease_seconds = float(lease_seconds)
        self._clock = clock
        self._owned_tokens: set[str] = set()
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_error: BaseException | None = None
        with backend.transaction(write=True) as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS ha_tenant_quota_locks (
                tenant_id TEXT PRIMARY KEY,revision BIGINT NOT NULL,
                updated_at DOUBLE PRECISION NOT NULL)"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS ha_tenant_quota_reservations (
                token TEXT PRIMARY KEY,tenant_id TEXT NOT NULL,kind TEXT NOT NULL,
                storage_id TEXT NOT NULL,filename TEXT NOT NULL,
                document_delta BIGINT NOT NULL,byte_delta BIGINT NOT NULL,
                lease_owner TEXT NOT NULL,lease_expires_at DOUBLE PRECISION NOT NULL,
                created_at DOUBLE PRECISION NOT NULL)"""
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_ha_tenant_quota_expiry "
                "ON ha_tenant_quota_reservations(tenant_id,lease_expires_at)"
            )

    @property
    def enabled(self) -> bool:
        return any(self.policy.public_limits().values())

    def _tenant_lock(self, connection: DatabaseConnection, tenant_id: str) -> None:
        now = float(self._clock())
        insert = self.backend.sql(
            sqlite=(
                "INSERT OR IGNORE INTO ha_tenant_quota_locks"
                "(tenant_id,revision,updated_at) VALUES(?,0,?)"
            ),
            postgres=(
                "INSERT INTO ha_tenant_quota_locks(tenant_id,revision,updated_at) "
                "VALUES(%s,0,%s) ON CONFLICT(tenant_id) DO NOTHING"
            ),
        )
        connection.execute(insert, (tenant_id, now))
        marker = self.backend.sql(sqlite="?", postgres="%s")
        lock = self.backend.sql(sqlite="", postgres=" FOR UPDATE")
        row = connection.execute(
            f"SELECT revision FROM ha_tenant_quota_locks WHERE tenant_id={marker}{lock}",
            (tenant_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError("tenant quota lock is unavailable")

    def _expire_locked(
        self, connection: DatabaseConnection, tenant_id: str, now: float
    ) -> None:
        marker = self.backend.sql(sqlite="?", postgres="%s")
        connection.execute(
            f"DELETE FROM ha_tenant_quota_reservations WHERE tenant_id={marker} "
            f"AND lease_expires_at<={marker}",
            (tenant_id, now),
        )

    def _usage_locked(
        self, connection: DatabaseConnection, tenant_id: str
    ) -> dict[str, int]:
        marker = self.backend.sql(sqlite="?", postgres="%s")
        kb_row = connection.execute(
            "SELECT COUNT(*) AS value FROM ha_api_knowledge_bases WHERE tenant_id="
            f"{marker} AND lifecycle<>{marker}",
            (tenant_id, LIFECYCLE_DELETED),
        ).fetchone()
        source_row = connection.execute(
            "SELECT COALESCE(SUM(g.document_count),0) AS documents,"
            "COALESCE(SUM(g.document_bytes),0) AS storage_bytes "
            "FROM ha_source_heads h JOIN ha_source_generations g "
            "ON g.generation_id=h.generation_id JOIN ha_api_knowledge_bases k "
            "ON k.storage_id=h.storage_id WHERE k.tenant_id="
            f"{marker} AND k.lifecycle<>{marker}",
            (tenant_id, LIFECYCLE_DELETED),
        ).fetchone()
        kb = self._mapping(kb_row)
        source = self._mapping(source_row)
        return {
            "knowledge_bases": int(kb.get("value", 0)),
            "documents": int(source.get("documents", 0)),
            "storage_bytes": int(source.get("storage_bytes", 0)),
        }

    @staticmethod
    def _mapping(row: Any | None) -> dict[str, Any]:
        if row is None:
            return {}
        if isinstance(row, Mapping):
            return dict(row)
        keys = getattr(row, "keys", None)
        if callable(keys):
            return {str(key): row[key] for key in keys()}
        raise RuntimeError("quota database row mapping is unavailable")

    def _reserved_locked(
        self, connection: DatabaseConnection, tenant_id: str
    ) -> dict[str, int]:
        marker = self.backend.sql(sqlite="?", postgres="%s")
        row = connection.execute(
            "SELECT COALESCE(SUM(CASE WHEN kind='knowledge_base' THEN 1 ELSE 0 END),0) "
            "AS knowledge_bases,COALESCE(SUM(document_delta),0) AS documents,"
            "COALESCE(SUM(byte_delta),0) AS storage_bytes "
            f"FROM ha_tenant_quota_reservations WHERE tenant_id={marker}",
            (tenant_id,),
        ).fetchone()
        value = self._mapping(row)
        return {
            key: int(value.get(key, 0))
            for key in ("knowledge_bases", "documents", "storage_bytes")
        }

    def _enforce(self, resource: str, used: int, reserved: int, requested: int) -> None:
        limit = {
            "knowledge_bases": self.policy.max_knowledge_bases,
            "documents": self.policy.max_documents,
            "storage_bytes": self.policy.max_storage_bytes,
        }[resource]
        if limit > 0 and used + reserved + requested > limit:
            raise TenantQuotaExceeded(
                resource,
                limit=limit,
                used=used + reserved,
                requested=requested,
            )

    def _insert_locked(
        self,
        connection: DatabaseConnection,
        *,
        tenant_id: str,
        kind: str,
        storage_id: str = "",
        filename: str = "",
        document_delta: int = 0,
        byte_delta: int = 0,
    ) -> str:
        token = f"quota-{secrets.token_hex(16)}"
        now = float(self._clock())
        marker = self.backend.sql(sqlite="?", postgres="%s")
        connection.execute(
            "INSERT INTO ha_tenant_quota_reservations(token,tenant_id,kind,storage_id,"
            "filename,document_delta,byte_delta,lease_owner,lease_expires_at,created_at) "
            f"VALUES({','.join([marker] * 10)})",
            (
                token,
                tenant_id,
                kind,
                storage_id,
                filename,
                document_delta,
                byte_delta,
                self.owner_id,
                now + self.lease_seconds,
                now,
            ),
        )
        return token

    def _track(self, token: str) -> str:
        with self._lock:
            self._owned_tokens.add(token)
        return token

    def reserve_knowledge_base(self, tenant_id: str) -> str:
        tenant = str(tenant_id or "").strip()
        if not tenant:
            raise ValueError("tenant_id is required")
        now = float(self._clock())
        with self.backend.transaction(write=True) as connection:
            self._tenant_lock(connection, tenant)
            self._expire_locked(connection, tenant, now)
            usage = self._usage_locked(connection, tenant)
            reserved = self._reserved_locked(connection, tenant)
            self._enforce(
                "knowledge_bases",
                usage["knowledge_bases"],
                reserved["knowledge_bases"],
                1,
            )
            token = self._insert_locked(
                connection, tenant_id=tenant, kind="knowledge_base"
            )
        return self._track(token)

    def reserve_upload(
        self,
        tenant_id: str,
        storage_id: str,
        _source_dir: str,
        filename: str,
        content_bytes: int,
    ) -> str:
        tenant = str(tenant_id or "").strip()
        storage = str(storage_id or "").strip()
        safe_name = os.path.basename(filename)
        if not tenant or not storage or not safe_name or safe_name != filename:
            raise ValueError("quota upload scope is invalid")
        if isinstance(content_bytes, bool) or int(content_bytes) < 0:
            raise ValueError("content_bytes is invalid")
        requested_bytes = int(content_bytes)
        marker = self.backend.sql(sqlite="?", postgres="%s")
        for _attempt in range(4):
            generation = self.source_generations.current(storage)
            expected_head = (
                None if generation is None else str(generation["generation_id"])
            )
            manifest = self.source_generations.current_manifest(storage)
            old_files = {
                str(item["path"]): int(item["byte_size"])
                for item in (manifest or {}).get("files", [])
                if isinstance(item, Mapping)
            }
            now = float(self._clock())
            with self.backend.transaction(write=True) as connection:
                self._tenant_lock(connection, tenant)
                live = connection.execute(
                    f"SELECT generation_id FROM ha_source_heads WHERE storage_id={marker}",
                    (storage,),
                ).fetchone()
                live_head = (
                    None if live is None else str(self._mapping(live)["generation_id"])
                )
                if live_head != expected_head:
                    continue
                kb = connection.execute(
                    "SELECT 1 AS value FROM ha_api_knowledge_bases WHERE storage_id="
                    f"{marker} AND tenant_id={marker} AND lifecycle<>{marker}",
                    (storage, tenant, LIFECYCLE_DELETED),
                ).fetchone()
                if kb is None:
                    raise ValueError("quota knowledge base scope is unavailable")
                self._expire_locked(connection, tenant, now)
                duplicate = connection.execute(
                    "SELECT 1 AS value FROM ha_tenant_quota_reservations WHERE tenant_id="
                    f"{marker} AND kind='upload' AND storage_id={marker} AND filename={marker}",
                    (tenant, storage, safe_name),
                ).fetchone()
                if duplicate is not None:
                    raise TenantMutationInProgress(
                        f"document mutation already pending: {safe_name}"
                    )
                usage = self._usage_locked(connection, tenant)
                reserved = self._reserved_locked(connection, tenant)
                existed = safe_name in old_files
                document_delta = 0 if existed else 1
                byte_delta = max(0, requested_bytes - old_files.get(safe_name, 0))
                self._enforce(
                    "documents",
                    usage["documents"],
                    reserved["documents"],
                    document_delta,
                )
                self._enforce(
                    "storage_bytes",
                    usage["storage_bytes"],
                    reserved["storage_bytes"],
                    byte_delta,
                )
                token = self._insert_locked(
                    connection,
                    tenant_id=tenant,
                    kind="upload",
                    storage_id=storage,
                    filename=safe_name,
                    document_delta=document_delta,
                    byte_delta=byte_delta,
                )
            return self._track(token)
        raise TenantMutationInProgress("source head changed during quota admission")

    def reserve_connector_snapshot(
        self,
        tenant_id: str,
        storage_id: str,
        _source_dir: str,
        baseline_dir: str,
        proposed_dir: str,
        reservation_key: str,
    ) -> str | None:
        """Reserve connector snapshot growth against the authoritative HA head."""

        if not (self.policy.max_documents or self.policy.max_storage_bytes):
            return None
        tenant = str(tenant_id or "").strip()
        storage = str(storage_id or "").strip()
        key = str(reservation_key or "").strip()
        if not tenant or not storage or not key or len(key) > 256:
            raise ValueError("connector quota scope is invalid")
        baseline = TenantQuotaManager._document_entries(baseline_dir)
        proposed = TenantQuotaManager._document_entries(proposed_dir)
        affected_names = baseline.keys() | proposed.keys()
        marker = self.backend.sql(sqlite="?", postgres="%s")

        for _attempt in range(4):
            generation = self.source_generations.current(storage)
            expected_head = (
                None if generation is None else str(generation["generation_id"])
            )
            manifest = self.source_generations.current_manifest(storage)
            published = {
                str(item["path"]): int(item["byte_size"])
                for item in (manifest or {}).get("files", [])
                if isinstance(item, Mapping)
            }
            published_affected = {
                name: published[name]
                for name in affected_names
                if name in published
            }
            document_delta = max(0, len(proposed) - len(published_affected))
            byte_delta = max(
                0, sum(proposed.values()) - sum(published_affected.values())
            )
            now = float(self._clock())
            with self.backend.transaction(write=True) as connection:
                self._tenant_lock(connection, tenant)
                live = connection.execute(
                    f"SELECT generation_id FROM ha_source_heads WHERE storage_id={marker}",
                    (storage,),
                ).fetchone()
                live_head = (
                    None if live is None else str(self._mapping(live)["generation_id"])
                )
                if live_head != expected_head:
                    continue
                kb = connection.execute(
                    "SELECT 1 AS value FROM ha_api_knowledge_bases WHERE storage_id="
                    f"{marker} AND tenant_id={marker} AND lifecycle<>{marker}",
                    (storage, tenant, LIFECYCLE_DELETED),
                ).fetchone()
                if kb is None:
                    raise ValueError("quota knowledge base scope is unavailable")
                self._expire_locked(connection, tenant, now)
                duplicate = connection.execute(
                    "SELECT 1 AS value FROM ha_tenant_quota_reservations WHERE tenant_id="
                    f"{marker} AND kind='connector' AND storage_id={marker} "
                    f"AND filename={marker}",
                    (tenant, storage, key),
                ).fetchone()
                if duplicate is not None:
                    raise TenantMutationInProgress(
                        f"connector snapshot already pending: {key}"
                    )
                usage = self._usage_locked(connection, tenant)
                reserved = self._reserved_locked(connection, tenant)
                self._enforce(
                    "documents",
                    usage["documents"],
                    reserved["documents"],
                    document_delta,
                )
                self._enforce(
                    "storage_bytes",
                    usage["storage_bytes"],
                    reserved["storage_bytes"],
                    byte_delta,
                )
                token = self._insert_locked(
                    connection,
                    tenant_id=tenant,
                    kind="connector",
                    storage_id=storage,
                    filename=key,
                    document_delta=document_delta,
                    byte_delta=byte_delta,
                )
            return self._track(token)
        raise TenantMutationInProgress("source head changed during quota admission")

    def snapshot(self, tenant_id: str) -> dict[str, Any]:
        tenant = str(tenant_id or "").strip()
        now = float(self._clock())
        with self.backend.transaction(write=True) as connection:
            self._tenant_lock(connection, tenant)
            self._expire_locked(connection, tenant, now)
            return {
                "tenant_id": tenant,
                "limits": self.policy.public_limits(),
                "usage": self._usage_locked(connection, tenant),
                "reserved": self._reserved_locked(connection, tenant),
            }

    def release(self, token: str | None) -> None:
        if not token:
            return
        marker = self.backend.sql(sqlite="?", postgres="%s")
        with self._lock:
            with self.backend.transaction(write=True) as connection:
                connection.execute(
                    f"DELETE FROM ha_tenant_quota_reservations WHERE token={marker} "
                    f"AND lease_owner={marker}",
                    (token, self.owner_id),
                )
            self._owned_tokens.discard(token)

    def assert_live(self, token: str | None) -> None:
        """Renew and validate the reservation immediately before publication."""

        if not token:
            raise TenantQuotaReservationLost("tenant quota reservation is unavailable")
        now = float(self._clock())
        marker = self.backend.sql(sqlite="?", postgres="%s")
        with self._lock:
            if token not in self._owned_tokens:
                raise TenantQuotaReservationLost(
                    "tenant quota reservation is no longer owned"
                )
            with self.backend.transaction(write=True) as connection:
                changed = connection.execute(
                    "UPDATE ha_tenant_quota_reservations SET lease_expires_at="
                    f"{marker} WHERE token={marker} AND lease_owner={marker} "
                    f"AND lease_expires_at>{marker}",
                    (now + self.lease_seconds, token, self.owner_id, now),
                )
            if changed.rowcount != 1:
                self._owned_tokens.discard(token)
                raise TenantQuotaReservationLost(
                    "tenant quota reservation is stale or expired"
                )

    def heartbeat(self) -> int:
        with self._lock:
            tokens = tuple(self._owned_tokens)
            if not tokens:
                return 0
            now = float(self._clock())
            marker = self.backend.sql(sqlite="?", postgres="%s")
            updated = 0
            lost: list[str] = []
            with self.backend.transaction(write=True) as connection:
                for token in tokens:
                    changed = connection.execute(
                        "UPDATE ha_tenant_quota_reservations SET lease_expires_at="
                        f"{marker} WHERE token={marker} AND lease_owner={marker} "
                        f"AND lease_expires_at>{marker}",
                        (now + self.lease_seconds, token, self.owner_id, now),
                    )
                    updated += changed.rowcount
                    if changed.rowcount != 1:
                        lost.append(token)
            if lost:
                self._owned_tokens.difference_update(lost)
                raise TenantQuotaReservationLost(
                    "one or more tenant quota reservations expired"
                )
            return updated

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._last_error = None
            self._thread = threading.Thread(
                target=self._run,
                name=f"cogdoc-ha-quota-{self.owner_id[:16]}",
                daemon=True,
            )
            self._thread.start()

    def _run(self) -> None:
        interval = max(1.0, self.lease_seconds / 3)
        while not self._stop.wait(interval):
            try:
                self.heartbeat()
                self._last_error = None
            except BaseException as exc:
                self._last_error = exc

    def stop(self, timeout: float = 10.0) -> bool:
        self._stop.set()
        with self._lock:
            thread = self._thread
        if thread is not None:
            thread.join(timeout)
        stopped = thread is None or not thread.is_alive()
        if stopped:
            with self._lock:
                self._thread = None
        return stopped

    def check(self) -> bool:
        with self.backend.transaction() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS value FROM ha_tenant_quota_reservations"
            ).fetchone()
        with self._lock:
            thread = self._thread
        return (
            row is not None
            and self._last_error is None
            and (thread is None or thread.is_alive() or self._stop.is_set())
        )


__all__ = ["DistributedTenantQuotaManager"]
