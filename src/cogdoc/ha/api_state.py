from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import threading
import time
import uuid
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from cogdoc.api.ingest import KBExistsError, KnowledgeBaseRegistry
from cogdoc.ha.storage import DatabaseBackend, DatabaseConnection
from cogdoc.service.kb_lifecycle import (
    LIFECYCLE_ACTIVE,
    LIFECYCLE_DELETED,
    LIFECYCLE_DELETING,
)


_LIFECYCLES = {LIFECYCLE_ACTIVE, LIFECYCLE_DELETING, LIFECYCLE_DELETED}
_TERMINAL_JOBS = {"succeeded", "failed"}
_MAX_JOB_RECORD_BYTES = 1024 * 1024


class DistributedStateError(RuntimeError):
    pass


class KnowledgeBaseCreateHook(Protocol):
    def __call__(
        self, connection: DatabaseConnection, record: Mapping[str, Any]
    ) -> None: ...


class MutationBusy(DistributedStateError):
    def __init__(self, retry_after: float) -> None:
        super().__init__("knowledge-base mutation is already leased")
        self.retry_after = max(0.0, retry_after)


class StaleMutationFence(DistributedStateError):
    pass


@dataclass(frozen=True, slots=True)
class MutationLease:
    storage_id: str
    owner_id: str
    lease_token: str
    fencing_token: int
    kb_epoch: int
    expires_at: float


def _canonical(value: Mapping[str, Any]) -> str:
    try:
        return json.dumps(
            dict(value),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("distributed record must be finite JSON") from exc


def _row_mapping(row: Any | None) -> dict[str, Any] | None:
    if row is None:
        return None
    if isinstance(row, Mapping):
        return dict(row)
    keys = getattr(row, "keys", None)
    if callable(keys):
        return {str(key): row[key] for key in keys()}
    raise DistributedStateError("database row mapping is unavailable")


class DistributedKnowledgeBaseRegistry:
    """PostgreSQL/SQLite authority for KB identity, lifecycle and incarnation.

    Local directories are disposable node caches.  A deleted row remains as a
    tombstone so recreating the same tenant/slug always advances ``epoch``.
    """

    def __init__(self, backend: DatabaseBackend, source_root: str | os.PathLike[str]):
        self.backend = backend
        self.source_root = Path(source_root)
        self._create_hook: KnowledgeBaseCreateHook | None = None
        self.source_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._initialize()

    def bind_create_hook(self, hook: KnowledgeBaseCreateHook) -> None:
        """Bind the authority transition that must commit with KB creation."""

        if not callable(hook):
            raise TypeError("knowledge-base create hook must be callable")
        if self._create_hook is not None and self._create_hook is not hook:
            raise ValueError("knowledge-base create hook is already bound")
        self._create_hook = hook

    def _initialize(self) -> None:
        with self.backend.transaction(write=True) as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS ha_api_knowledge_bases (
                storage_id TEXT PRIMARY KEY,tenant_id TEXT NOT NULL,kb_id TEXT NOT NULL,
                owner_id TEXT NOT NULL,created_at TEXT NOT NULL,lifecycle TEXT NOT NULL,
                epoch BIGINT NOT NULL,revision BIGINT NOT NULL,updated_at DOUBLE PRECISION NOT NULL,
                UNIQUE(tenant_id,kb_id))"""
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_ha_api_kbs_tenant "
                "ON ha_api_knowledge_bases(tenant_id,kb_id)"
            )

    @classmethod
    def storage_id_for(cls, kb_id: str, tenant_id: str = "default") -> str:
        return KnowledgeBaseRegistry.storage_id_for(kb_id, tenant_id)

    def _source_dir_for(self, storage_id: str) -> str:
        digest = hashlib.sha256(storage_id.encode()).hexdigest()
        return str(self.source_root / digest / "source")

    def source_dir(self, kb_id: str, tenant_id: str | None = None) -> str:
        record = (
            self.get_by_storage_id(kb_id)
            if tenant_id is None
            else self.resolve(kb_id, tenant_id)
        )
        storage_id = (
            str(record["storage_id"])
            if record is not None
            else self.storage_id_for(kb_id, tenant_id or "default")
        )
        return self._source_dir_for(storage_id)

    def purge_cache(self, storage_id: str) -> None:
        if not isinstance(storage_id, str) or not storage_id:
            raise ValueError("storage_id is invalid")
        try:
            shutil.rmtree(Path(self._source_dir_for(storage_id)).parent)
        except FileNotFoundError:
            pass

    @staticmethod
    def _public(row: Mapping[str, Any] | None) -> dict[str, Any] | None:
        if row is None or row.get("lifecycle") == LIFECYCLE_DELETED:
            return None
        return {
            "kb_id": str(row["kb_id"]),
            "tenant_id": str(row["tenant_id"]),
            "owner_id": str(row["owner_id"]),
            "storage_id": str(row["storage_id"]),
            "created_at": str(row["created_at"]),
            "epoch": int(row["epoch"]),
            "revision": int(row["revision"]),
        }

    def resolve(self, kb_id: str, tenant_id: str = "default") -> dict[str, Any] | None:
        kb_id, tenant_id, _owner = KnowledgeBaseRegistry._validated_identity(
            kb_id, tenant_id
        )
        marker = self.backend.sql(sqlite="?", postgres="%s")
        with self.backend.transaction() as connection:
            row = _row_mapping(
                connection.execute(
                    f"SELECT * FROM ha_api_knowledge_bases WHERE tenant_id={marker} "
                    f"AND kb_id={marker}",
                    (tenant_id, kb_id),
                ).fetchone()
            )
        return self._public(row)

    def get_by_storage_id(self, storage_id: str) -> dict[str, Any] | None:
        if not isinstance(storage_id, str) or not storage_id:
            return None
        marker = self.backend.sql(sqlite="?", postgres="%s")
        with self.backend.transaction() as connection:
            row = _row_mapping(
                connection.execute(
                    f"SELECT * FROM ha_api_knowledge_bases WHERE storage_id={marker}",
                    (storage_id,),
                ).fetchone()
            )
        return self._public(row)

    def get(self, kb_id: str, tenant_id: str | None = None) -> dict[str, Any] | None:
        return (
            self.get_by_storage_id(kb_id)
            if tenant_id is None
            else self.resolve(kb_id, tenant_id)
        )

    def exists(self, kb_id: str, tenant_id: str | None = None) -> bool:
        return self.get(kb_id, tenant_id) is not None

    def list(self, tenant_id: str | None = None) -> list[dict[str, Any]]:
        marker = self.backend.sql(sqlite="?", postgres="%s")
        query = (
            "SELECT * FROM ha_api_knowledge_bases WHERE lifecycle<>"
            f"'{LIFECYCLE_DELETED}'"
        )
        params: tuple[Any, ...] = ()
        if tenant_id is not None:
            tenant_id = KnowledgeBaseRegistry._normalize_identity_id(
                tenant_id, field="tenant_id"
            )
            query += f" AND tenant_id={marker}"
            params = (tenant_id,)
        query += " ORDER BY tenant_id,kb_id"
        with self.backend.transaction() as connection:
            rows = connection.execute(query, params).fetchall()
        return [
            value
            for row in rows
            if (value := self._public(_row_mapping(row))) is not None
        ]

    def create(
        self,
        kb_id: str,
        tenant_id: str = "default",
        owner_id: str = "default",
    ) -> dict[str, Any]:
        kb_id, tenant_id, owner = KnowledgeBaseRegistry._validated_identity(
            kb_id, tenant_id, owner_id
        )
        assert owner is not None
        storage_id = self.storage_id_for(kb_id, tenant_id)
        source_dir = Path(self._source_dir_for(storage_id))
        source_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        now = time.time()
        created_at = datetime.now(timezone.utc).isoformat()
        marker = self.backend.sql(sqlite="?", postgres="%s")
        lock = self.backend.sql(sqlite="", postgres=" FOR UPDATE")
        with self.backend.transaction(write=True) as connection:
            existing = _row_mapping(
                connection.execute(
                    f"SELECT * FROM ha_api_knowledge_bases WHERE storage_id={marker}{lock}",
                    (storage_id,),
                ).fetchone()
            )
            if existing is not None and existing["lifecycle"] != LIFECYCLE_DELETED:
                raise KBExistsError(kb_id)
            if existing is None:
                insert = self.backend.sql(
                    sqlite="INSERT OR IGNORE",
                    postgres="INSERT",
                )
                conflict = self.backend.sql(
                    sqlite="",
                    postgres=" ON CONFLICT(storage_id) DO NOTHING",
                )
                changed = connection.execute(
                    f"{insert} INTO ha_api_knowledge_bases(storage_id,tenant_id,kb_id,owner_id,"
                    "created_at,lifecycle,epoch,revision,updated_at) VALUES("
                    f"{marker},{marker},{marker},{marker},{marker},{marker},1,1,{marker}){conflict}",
                    (
                        storage_id,
                        tenant_id,
                        kb_id,
                        owner,
                        created_at,
                        LIFECYCLE_ACTIVE,
                        now,
                    ),
                )
                if changed.rowcount != 1:
                    raise KBExistsError(kb_id)
            else:
                changed = connection.execute(
                    f"UPDATE ha_api_knowledge_bases SET owner_id={marker},created_at={marker},"
                    f"lifecycle={marker},epoch=epoch+1,revision=revision+1,updated_at={marker} "
                    f"WHERE storage_id={marker} AND lifecycle={marker}",
                    (
                        owner,
                        created_at,
                        LIFECYCLE_ACTIVE,
                        now,
                        storage_id,
                        LIFECYCLE_DELETED,
                    ),
                )
                if changed.rowcount != 1:
                    raise KBExistsError(kb_id)
            row = _row_mapping(
                connection.execute(
                    f"SELECT * FROM ha_api_knowledge_bases WHERE storage_id={marker}",
                    (storage_id,),
                ).fetchone()
            )
            if row is None:
                raise DistributedStateError("created knowledge-base row is unavailable")
            if self._create_hook is not None:
                self._create_hook(connection, row)
        result = self._public(row)
        assert result is not None
        return result

    def import_legacy(
        self,
        records: Iterable[Mapping[str, Any]],
        *,
        lifecycles: Mapping[str, str] | None = None,
        epochs: Mapping[str, int] | None = None,
    ) -> dict[str, int]:
        """Idempotently import a stopped single-writer registry.

        The caller must quiesce the legacy API first. Existing distributed rows
        are never overwritten; identity mismatch fails instead of guessing.
        """

        lifecycle_map = dict(lifecycles or {})
        epoch_map = dict(epochs or {})
        imported = skipped = 0
        marker = self.backend.sql(sqlite="?", postgres="%s")
        lock = self.backend.sql(sqlite="", postgres=" FOR UPDATE")
        now = time.time()
        with self.backend.transaction(write=True) as connection:
            for raw in records:
                kb_id, tenant_id, owner_id = KnowledgeBaseRegistry._validated_identity(
                    str(raw.get("kb_id") or ""),
                    str(raw.get("tenant_id") or "default"),
                    str(raw.get("owner_id") or "default"),
                )
                assert owner_id is not None
                storage_id = self.storage_id_for(kb_id, tenant_id)
                supplied_storage = str(raw.get("storage_id") or storage_id)
                if supplied_storage != storage_id:
                    raise ValueError("legacy registry storage identity does not match")
                lifecycle = lifecycle_map.get(storage_id, LIFECYCLE_ACTIVE)
                epoch = epoch_map.get(storage_id, 1)
                if lifecycle not in _LIFECYCLES:
                    raise ValueError("legacy lifecycle is invalid")
                if type(epoch) is not int or epoch < 1:
                    raise ValueError("legacy epoch is invalid")
                existing = _row_mapping(
                    connection.execute(
                        f"SELECT tenant_id,kb_id FROM ha_api_knowledge_bases "
                        f"WHERE storage_id={marker}{lock}",
                        (storage_id,),
                    ).fetchone()
                )
                if existing is not None:
                    if existing["tenant_id"] != tenant_id or existing["kb_id"] != kb_id:
                        raise DistributedStateError(
                            "distributed registry identity conflicts with legacy data"
                        )
                    skipped += 1
                    continue
                connection.execute(
                    "INSERT INTO ha_api_knowledge_bases(storage_id,tenant_id,kb_id,owner_id,"
                    "created_at,lifecycle,epoch,revision,updated_at) VALUES("
                    f"{marker},{marker},{marker},{marker},{marker},{marker},{marker},1,{marker})",
                    (
                        storage_id,
                        tenant_id,
                        kb_id,
                        owner_id,
                        str(
                            raw.get("created_at")
                            or datetime.now(timezone.utc).isoformat()
                        ),
                        lifecycle,
                        epoch,
                        now,
                    ),
                )
                imported += 1
        return {"imported": imported, "skipped": skipped}

    def delete(self, kb_id: str, tenant_id: str | None = None) -> bool:
        record = self.get(kb_id, tenant_id)
        if record is None:
            return False
        storage_id = str(record["storage_id"])
        marker = self.backend.sql(sqlite="?", postgres="%s")
        now = time.time()
        with self.backend.transaction(write=True) as connection:
            changed = connection.execute(
                f"UPDATE ha_api_knowledge_bases SET lifecycle={marker},epoch=epoch+1,"
                f"revision=revision+1,updated_at={marker} WHERE storage_id={marker} "
                f"AND lifecycle<>{marker}",
                (LIFECYCLE_DELETED, now, storage_id, LIFECYCLE_DELETED),
            )
        try:
            shutil.rmtree(Path(self._source_dir_for(storage_id)).parent)
        except FileNotFoundError:
            pass
        except OSError:
            # Cache cleanup is not the authority transition and is retried by
            # bounded cache maintenance.  Never resurrect a committed tombstone.
            pass
        return changed.rowcount == 1

    def status(self, storage_id: str) -> str:
        marker = self.backend.sql(sqlite="?", postgres="%s")
        with self.backend.transaction() as connection:
            row = connection.execute(
                f"SELECT lifecycle FROM ha_api_knowledge_bases WHERE storage_id={marker}",
                (storage_id,),
            ).fetchone()
        if row is None:
            return LIFECYCLE_DELETED
        value = row["lifecycle"] if isinstance(row, Mapping) else row[0]
        return str(value) if value in _LIFECYCLES else LIFECYCLE_DELETING

    def set(self, storage_id: str, status: str) -> None:
        if status not in _LIFECYCLES:
            raise ValueError("invalid lifecycle status")
        marker = self.backend.sql(sqlite="?", postgres="%s")
        with self.backend.transaction(write=True) as connection:
            changed = connection.execute(
                f"UPDATE ha_api_knowledge_bases SET lifecycle={marker},revision=revision+1,"
                f"updated_at={marker} WHERE storage_id={marker}",
                (status, time.time(), storage_id),
            )
        if changed.rowcount != 1:
            raise KeyError(storage_id)

    def current(self, storage_id: str) -> int:
        marker = self.backend.sql(sqlite="?", postgres="%s")
        with self.backend.transaction() as connection:
            row = connection.execute(
                f"SELECT epoch FROM ha_api_knowledge_bases WHERE storage_id={marker}",
                (storage_id,),
            ).fetchone()
        if row is None:
            return 0
        value = row["epoch"] if isinstance(row, Mapping) else row[0]
        return int(value)

    def bump(self, storage_id: str) -> int:
        marker = self.backend.sql(sqlite="?", postgres="%s")
        with self.backend.transaction(write=True) as connection:
            changed = connection.execute(
                f"UPDATE ha_api_knowledge_bases SET epoch=epoch+1,revision=revision+1,"
                f"updated_at={marker} WHERE storage_id={marker}",
                (time.time(), storage_id),
            )
            if changed.rowcount != 1:
                raise KeyError(storage_id)
            row = connection.execute(
                f"SELECT epoch FROM ha_api_knowledge_bases WHERE storage_id={marker}",
                (storage_id,),
            ).fetchone()
        if row is None:
            raise DistributedStateError("knowledge-base epoch row is unavailable")
        value = row["epoch"] if isinstance(row, Mapping) else row[0]
        return int(value)

    def check(self) -> bool:
        try:
            with self.backend.transaction() as connection:
                return (
                    connection.execute(
                        "SELECT COUNT(*) AS row_count FROM ha_api_knowledge_bases"
                    ).fetchone()
                    is not None
                )
        except Exception:
            return False


class DistributedMutationCoordinator:
    """Lease-fenced cross-process serialization for one KB mutation stream."""

    def __init__(
        self,
        backend: DatabaseBackend,
        registry: DistributedKnowledgeBaseRegistry,
        *,
        owner_id: str,
        lease_seconds: float = 300.0,
        clock: Any = time.time,
    ) -> None:
        if not owner_id or len(owner_id.encode()) > 255:
            raise ValueError("mutation owner_id is invalid")
        if not math.isfinite(lease_seconds) or not 5 <= lease_seconds <= 3600:
            raise ValueError("mutation lease_seconds must be between 5 and 3600")
        if registry.backend is not backend:
            raise ValueError("mutation coordinator and registry must share a backend")
        self.backend = backend
        self.registry = registry
        self.owner_id = owner_id
        self.lease_seconds = float(lease_seconds)
        self._clock = clock
        self._local = threading.local()
        with backend.transaction(write=True) as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS ha_api_mutation_leases (
                storage_id TEXT PRIMARY KEY,lease_owner TEXT NOT NULL,lease_token TEXT NOT NULL,
                lease_expires_at DOUBLE PRECISION NOT NULL,fencing_token BIGINT NOT NULL,
                kb_epoch BIGINT NOT NULL,updated_at DOUBLE PRECISION NOT NULL)"""
            )

    def acquire(self, storage_id: str) -> MutationLease:
        if not isinstance(storage_id, str) or not storage_id:
            raise ValueError("storage_id is invalid")
        now = float(self._clock())
        marker = self.backend.sql(sqlite="?", postgres="%s")
        lock = self.backend.sql(sqlite="", postgres=" FOR UPDATE")
        token = f"mut-{uuid.uuid4().hex}"
        with self.backend.transaction(write=True) as connection:
            kb = _row_mapping(
                connection.execute(
                    f"SELECT lifecycle,epoch FROM ha_api_knowledge_bases "
                    f"WHERE storage_id={marker}{lock}",
                    (storage_id,),
                ).fetchone()
            )
            if kb is None or kb["lifecycle"] != LIFECYCLE_ACTIVE:
                raise StaleMutationFence("knowledge base is not active")
            current = _row_mapping(
                connection.execute(
                    f"SELECT * FROM ha_api_mutation_leases WHERE storage_id={marker}{lock}",
                    (storage_id,),
                ).fetchone()
            )
            if current is not None and float(current["lease_expires_at"]) > now:
                raise MutationBusy(float(current["lease_expires_at"]) - now)
            fencing = 1 if current is None else int(current["fencing_token"]) + 1
            expires = now + self.lease_seconds
            if current is None:
                connection.execute(
                    "INSERT INTO ha_api_mutation_leases(storage_id,lease_owner,lease_token,"
                    "lease_expires_at,fencing_token,kb_epoch,updated_at) VALUES("
                    f"{marker},{marker},{marker},{marker},{marker},{marker},{marker})",
                    (
                        storage_id,
                        self.owner_id,
                        token,
                        expires,
                        fencing,
                        int(kb["epoch"]),
                        now,
                    ),
                )
            else:
                connection.execute(
                    f"UPDATE ha_api_mutation_leases SET lease_owner={marker},"
                    f"lease_token={marker},lease_expires_at={marker},fencing_token={marker},"
                    f"kb_epoch={marker},updated_at={marker} WHERE storage_id={marker}",
                    (
                        self.owner_id,
                        token,
                        expires,
                        fencing,
                        int(kb["epoch"]),
                        now,
                        storage_id,
                    ),
                )
        return MutationLease(
            storage_id,
            self.owner_id,
            token,
            fencing,
            int(kb["epoch"]),
            expires,
        )

    def heartbeat(self, lease: MutationLease) -> MutationLease:
        now = float(self._clock())
        expires = now + self.lease_seconds
        marker = self.backend.sql(sqlite="?", postgres="%s")
        with self.backend.transaction(write=True) as connection:
            changed = connection.execute(
                f"UPDATE ha_api_mutation_leases SET lease_expires_at={marker},updated_at={marker} "
                f"WHERE storage_id={marker} AND lease_token={marker} AND fencing_token={marker} "
                f"AND kb_epoch={marker} AND lease_expires_at>{marker}",
                (
                    expires,
                    now,
                    lease.storage_id,
                    lease.lease_token,
                    lease.fencing_token,
                    lease.kb_epoch,
                    now,
                ),
            )
        if changed.rowcount != 1:
            raise StaleMutationFence("mutation lease is stale")
        return MutationLease(
            lease.storage_id,
            lease.owner_id,
            lease.lease_token,
            lease.fencing_token,
            lease.kb_epoch,
            expires,
        )

    def assert_live(self, lease: MutationLease | None = None) -> None:
        active = lease or getattr(self._local, "lease", None)
        if not isinstance(active, MutationLease):
            raise StaleMutationFence("mutation lease is unavailable")
        now = float(self._clock())
        marker = self.backend.sql(sqlite="?", postgres="%s")
        with self.backend.transaction() as connection:
            row = _row_mapping(
                connection.execute(
                    "SELECT leases.lease_token,leases.fencing_token,leases.kb_epoch,"
                    "leases.lease_expires_at,kbs.lifecycle,kbs.epoch FROM "
                    "ha_api_mutation_leases AS leases JOIN ha_api_knowledge_bases AS kbs "
                    "ON kbs.storage_id=leases.storage_id WHERE leases.storage_id="
                    f"{marker}",
                    (active.storage_id,),
                ).fetchone()
            )
        if (
            row is None
            or row["lease_token"] != active.lease_token
            or int(row["fencing_token"]) != active.fencing_token
            or int(row["kb_epoch"]) != active.kb_epoch
            or int(row["epoch"]) != active.kb_epoch
            or row["lifecycle"] != LIFECYCLE_ACTIVE
            or float(row["lease_expires_at"]) <= now
        ):
            raise StaleMutationFence("mutation authority changed")

    def release(self, lease: MutationLease) -> None:
        marker = self.backend.sql(sqlite="?", postgres="%s")
        with self.backend.transaction(write=True) as connection:
            connection.execute(
                f"UPDATE ha_api_mutation_leases SET lease_expires_at=0,updated_at={marker} "
                f"WHERE storage_id={marker} AND lease_token={marker}",
                (float(self._clock()), lease.storage_id, lease.lease_token),
            )

    @contextmanager
    def lease(self, storage_id: str) -> Iterator[MutationLease]:
        lease = self.acquire(storage_id)
        stopped = threading.Event()
        lost: list[BaseException] = []
        latest = [lease]

        def keep_alive() -> None:
            interval = max(1.0, self.lease_seconds / 3)
            while not stopped.wait(interval):
                try:
                    latest[0] = self.heartbeat(latest[0])
                except Exception as exc:
                    lost.append(exc)
                    return

        thread = threading.Thread(
            target=keep_alive,
            name=f"cogdoc-kb-lease-{storage_id[:12]}",
            daemon=True,
        )
        previous = getattr(self._local, "lease", None)
        self._local.lease = lease
        thread.start()
        try:
            yield lease
            if lost:
                raise StaleMutationFence("mutation heartbeat was lost") from lost[0]
            self.assert_live(lease)
        finally:
            stopped.set()
            thread.join(min(10.0, self.lease_seconds))
            self._local.lease = previous
            self.release(lease)

    def current_lease(self) -> MutationLease | None:
        value = getattr(self._local, "lease", None)
        return value if isinstance(value, MutationLease) else None

    @contextmanager
    def bind_lease(self, lease: MutationLease) -> Iterator[MutationLease]:
        """Delegate a live lease to another thread without reacquiring it."""

        if not isinstance(lease, MutationLease):
            raise TypeError("delegated mutation lease is invalid")
        self.assert_live(lease)
        previous = getattr(self._local, "lease", None)
        self._local.lease = lease
        try:
            yield lease
            self.assert_live(lease)
        finally:
            self._local.lease = previous


class DistributedIndexJobStore:
    """Shared index-job projection with lease-fenced worker updates."""

    distributed = True

    def __init__(
        self,
        backend: DatabaseBackend,
        *,
        owner_id: str,
        lease_seconds: float = 300.0,
        clock: Any = time.time,
    ) -> None:
        if not owner_id or len(owner_id.encode()) > 255:
            raise ValueError("index job owner_id is invalid")
        if not math.isfinite(lease_seconds) or not 5 <= lease_seconds <= 3600:
            raise ValueError("index job lease_seconds must be between 5 and 3600")
        self.backend = backend
        self.owner_id = owner_id
        self.lease_seconds = lease_seconds
        self._clock = clock
        self._local = threading.local()
        with backend.transaction(write=True) as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS ha_api_index_jobs (
                job_id TEXT PRIMARY KEY,kb_id TEXT NOT NULL,status TEXT NOT NULL,
                record_json TEXT NOT NULL,lease_owner TEXT,lease_token TEXT,
                lease_expires_at DOUBLE PRECISION,created_at DOUBLE PRECISION NOT NULL,
                updated_at DOUBLE PRECISION NOT NULL)"""
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_ha_api_index_jobs_scope "
                "ON ha_api_index_jobs(kb_id,created_at)"
            )

    def create(self, record: dict[str, Any]) -> None:
        if not isinstance(record.get("job_id"), str) or not record["job_id"]:
            raise ValueError("index job_id is invalid")
        if not isinstance(record.get("kb_id"), str) or not record["kb_id"]:
            raise ValueError("index job kb_id is invalid")
        encoded = _canonical(record)
        if len(encoded.encode()) > _MAX_JOB_RECORD_BYTES:
            raise ValueError("index job record exceeds 1 MiB")
        now = float(self._clock())
        marker = self.backend.sql(sqlite="?", postgres="%s")
        with self.backend.transaction(write=True) as connection:
            connection.execute(
                "INSERT INTO ha_api_index_jobs(job_id,kb_id,status,record_json,created_at,"
                f"updated_at) VALUES({marker},{marker},{marker},{marker},{marker},{marker})",
                (
                    record["job_id"],
                    record["kb_id"],
                    record["status"],
                    encoded,
                    now,
                    now,
                ),
            )

    def claim(self, job_id: str) -> str | None:
        now = float(self._clock())
        token = f"ij-{uuid.uuid4().hex}"
        marker = self.backend.sql(sqlite="?", postgres="%s")
        with self.backend.transaction(write=True) as connection:
            changed = connection.execute(
                f"UPDATE ha_api_index_jobs SET lease_owner={marker},lease_token={marker},"
                f"lease_expires_at={marker},updated_at={marker} WHERE job_id={marker} "
                f"AND status='pending' AND (lease_expires_at IS NULL OR lease_expires_at<={marker})",
                (
                    self.owner_id,
                    token,
                    now + self.lease_seconds,
                    now,
                    job_id,
                    now,
                ),
            )
        if changed.rowcount == 1:
            return token
        return None

    def heartbeat(self, job_id: str, lease_token: str) -> None:
        now = float(self._clock())
        marker = self.backend.sql(sqlite="?", postgres="%s")
        with self.backend.transaction(write=True) as connection:
            changed = connection.execute(
                f"UPDATE ha_api_index_jobs SET lease_expires_at={marker},updated_at={marker} "
                f"WHERE job_id={marker} AND lease_token={marker} "
                "AND status IN ('pending','running') "
                f"AND lease_expires_at>{marker}",
                (now + self.lease_seconds, now, job_id, lease_token, now),
            )
        if changed.rowcount != 1:
            raise StaleMutationFence("index job lease is stale")

    @contextmanager
    def bind_claim(self, job_id: str, lease_token: str) -> Iterator[None]:
        previous = getattr(self._local, "job_token", None)
        self._local.job_token = (job_id, lease_token)
        try:
            yield
        finally:
            self._local.job_token = previous

    def update(self, job_id: str, **fields: Any) -> None:
        marker = self.backend.sql(sqlite="?", postgres="%s")
        now = float(self._clock())
        active = getattr(self._local, "job_token", None)
        with self.backend.transaction(write=True) as connection:
            lock = self.backend.sql(sqlite="", postgres=" FOR UPDATE")
            row = _row_mapping(
                connection.execute(
                    f"SELECT * FROM ha_api_index_jobs WHERE job_id={marker}{lock}",
                    (job_id,),
                ).fetchone()
            )
            if row is None:
                return
            if (
                not isinstance(active, tuple)
                or active[0] != job_id
                or row["lease_token"] != active[1]
                or float(row["lease_expires_at"] or 0) <= now
            ):
                raise StaleMutationFence("index job update requires the live claim")
            record = json.loads(str(row["record_json"]))
            record.update(fields)
            encoded = _canonical(record)
            if len(encoded.encode()) > _MAX_JOB_RECORD_BYTES:
                raise ValueError("index job record exceeds 1 MiB")
            status = str(record.get("status") or row["status"])
            if status not in {"pending", "running", *_TERMINAL_JOBS}:
                raise ValueError("index job status is invalid")
            terminal = status in _TERMINAL_JOBS
            connection.execute(
                f"UPDATE ha_api_index_jobs SET status={marker},record_json={marker},"
                f"updated_at={marker},lease_expires_at={marker} WHERE job_id={marker}",
                (
                    status,
                    encoded,
                    now,
                    now if terminal else row["lease_expires_at"],
                    job_id,
                ),
            )

    def get(self, job_id: str) -> dict[str, Any] | None:
        marker = self.backend.sql(sqlite="?", postgres="%s")
        with self.backend.transaction() as connection:
            row = connection.execute(
                f"SELECT record_json FROM ha_api_index_jobs WHERE job_id={marker}",
                (job_id,),
            ).fetchone()
        if row is None:
            return None
        encoded = row["record_json"] if isinstance(row, Mapping) else row[0]
        value = json.loads(str(encoded))
        return value if isinstance(value, dict) else None

    def list(self, kb_ids: set[str], *, limit: int = 200) -> list[dict[str, Any]]:
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        if not kb_ids:
            return []
        marker = self.backend.sql(sqlite="?", postgres="%s")
        ordered_ids = sorted(kb_ids)
        placeholders = ",".join(marker for _ in ordered_ids)
        with self.backend.transaction() as connection:
            rows = connection.execute(
                "SELECT record_json FROM ha_api_index_jobs "
                f"WHERE kb_id IN ({placeholders}) "
                "ORDER BY created_at DESC,job_id DESC "
                f"LIMIT {marker}",
                (*ordered_ids, limit),
            ).fetchall()
        jobs = []
        for row in rows:
            encoded = row["record_json"] if isinstance(row, Mapping) else row[0]
            value = json.loads(str(encoded))
            if isinstance(value, dict):
                jobs.append(value)
        return jobs

    def reconcile_orphans(self) -> int:
        now = float(self._clock())
        marker = self.backend.sql(sqlite="?", postgres="%s")
        lock = self.backend.sql(sqlite="", postgres=" FOR UPDATE SKIP LOCKED")
        reconciled = 0
        with self.backend.transaction(write=True) as connection:
            rows = connection.execute(
                "SELECT job_id,record_json,lease_token FROM ha_api_index_jobs "
                f"WHERE status='running' AND lease_expires_at<={marker}{lock}",
                (now,),
            ).fetchall()
            for row in rows:
                value = _row_mapping(row)
                assert value is not None
                record = json.loads(str(value["record_json"]))
                record.update(
                    status="failed",
                    error_code="INGEST_FAILED",
                    message="服务重启或 worker 租约过期，任务中断",
                    finished_at=datetime.now(timezone.utc).isoformat(),
                )
                changed = connection.execute(
                    f"UPDATE ha_api_index_jobs SET status='failed',record_json={marker},"
                    f"updated_at={marker},lease_expires_at={marker} WHERE job_id={marker} "
                    f"AND status='running' AND lease_token={marker} "
                    f"AND lease_expires_at<={marker}",
                    (
                        _canonical(record),
                        now,
                        now,
                        value["job_id"],
                        value["lease_token"],
                        now,
                    ),
                )
                reconciled += changed.rowcount
        return reconciled


__all__ = [
    "DistributedIndexJobStore",
    "DistributedKnowledgeBaseRegistry",
    "DistributedMutationCoordinator",
    "DistributedStateError",
    "MutationBusy",
    "MutationLease",
    "StaleMutationFence",
]
