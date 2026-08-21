from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import shutil
import stat
import time
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path, PurePosixPath
from typing import Any, Final, Protocol

from cogdoc.ha.storage import DatabaseBackend, DatabaseConnection, execute_script


GEN_BUILDING: Final = "building"
GEN_PREPARED: Final = "prepared"
GEN_PUBLISHED: Final = "published"
GEN_ABORTED: Final = "aborted"
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_MAX_FILES = 100_000
_MAX_MANIFEST_BYTES = 16 * 1024 * 1024
_READ_CHUNK = 1024 * 1024


class IndexGenerationError(RuntimeError):
    pass


class StaleIndexFence(IndexGenerationError):
    pass


class IndexIntegrityError(IndexGenerationError):
    pass


class IndexConflict(IndexGenerationError):
    pass


class GenerationVerifier(Protocol):
    def __call__(self, generation: Mapping[str, Any]) -> None: ...


class PublicationHook(Protocol):
    def __call__(
        self, connection: DatabaseConnection, generation: Mapping[str, Any]
    ) -> None: ...


def _clean(value: str, field: str, maximum: int = 255) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError(f"{field} is invalid")
    return value


def _canonical(value: Any) -> bytes:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    except (TypeError, ValueError) as exc:
        raise ValueError("index manifest must be JSON serializable") from exc
    if len(encoded) > _MAX_MANIFEST_BYTES:
        raise ValueError("index manifest exceeds 16 MiB")
    return encoded


def normalize_manifest(manifest: Mapping[str, Any]) -> tuple[dict[str, Any], str]:
    if (
        not isinstance(manifest, Mapping)
        or manifest.get("schema_version") != "index-manifest-v1"
    ):
        raise ValueError("index manifest schema is invalid")
    raw_contract = manifest.get("contract")
    if not isinstance(raw_contract, Mapping):
        raise ValueError("index manifest contract is required")
    contract = {
        "chunk_version": _clean(
            str(raw_contract.get("chunk_version") or ""), "chunk_version"
        ),
        "embedding_model": _clean(
            str(raw_contract.get("embedding_model") or ""), "embedding_model", 512
        ),
        "dimensions": raw_contract.get("dimensions"),
    }
    if (
        type(contract["dimensions"]) is not int
        or not 1 <= contract["dimensions"] <= 1_000_000
    ):
        raise ValueError("index dimensions are invalid")
    raw_files = manifest.get("files")
    if not isinstance(raw_files, list) or not 0 <= len(raw_files) <= _MAX_FILES:
        raise ValueError("index manifest files are invalid")
    files: list[dict[str, Any]] = []
    paths: set[str] = set()
    total_bytes = 0
    for raw in raw_files:
        if not isinstance(raw, Mapping):
            raise ValueError("index manifest file is invalid")
        path = str(raw.get("path") or "")
        parsed = PurePosixPath(path)
        if (
            not path
            or parsed.is_absolute()
            or path != parsed.as_posix()
            or "\\" in path
            or any(part in {"", ".", ".."} for part in parsed.parts)
            or path in paths
            or len(path.encode()) > 1024
        ):
            raise ValueError("index manifest path is unsafe or duplicated")
        digest = str(raw.get("sha256") or "")
        size = raw.get("byte_size")
        if not _SHA256.fullmatch(digest) or type(size) is not int or size < 0:
            raise ValueError("index manifest file hash or size is invalid")
        total_bytes += size
        if total_bytes > 10 * 1024**4:
            raise ValueError("index generation exceeds 10 TiB")
        paths.add(path)
        files.append({"path": path, "sha256": digest, "byte_size": size})
    normalized = {
        "schema_version": "index-manifest-v1",
        "contract": contract,
        "files": sorted(files, key=lambda item: item["path"]),
        "total_bytes": total_bytes,
    }
    encoded = _canonical(normalized)
    return normalized, hashlib.sha256(encoded).hexdigest()


class IndexGenerationStore:
    """Database authority for immutable index generation publication."""

    def __init__(
        self, backend: DatabaseBackend, *, clock: Callable[[], float] = time.time
    ) -> None:
        self.backend = backend
        self._clock = clock
        execute_script(
            backend,
            [
                backend.sql(
                    sqlite="""CREATE TABLE IF NOT EXISTS ha_index_heads (
                    tenant_id TEXT NOT NULL,kb_id TEXT NOT NULL,current_generation_id TEXT,
                    fencing_token INTEGER NOT NULL DEFAULT 0,revision INTEGER NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL,PRIMARY KEY(tenant_id,kb_id))""",
                    postgres="""CREATE TABLE IF NOT EXISTS ha_index_heads (
                    tenant_id TEXT NOT NULL,kb_id TEXT NOT NULL,current_generation_id TEXT,
                    fencing_token BIGINT NOT NULL DEFAULT 0,revision BIGINT NOT NULL DEFAULT 0,
                    updated_at DOUBLE PRECISION NOT NULL,PRIMARY KEY(tenant_id,kb_id))""",
                ),
                backend.sql(
                    sqlite="""CREATE TABLE IF NOT EXISTS ha_index_generations (
                    generation_id TEXT PRIMARY KEY,tenant_id TEXT NOT NULL,kb_id TEXT NOT NULL,
                    build_id TEXT NOT NULL,status TEXT NOT NULL,base_generation_id TEXT,
                    fencing_token INTEGER NOT NULL,lease_owner TEXT NOT NULL,lease_token TEXT NOT NULL,
                    lease_expires_at REAL NOT NULL,manifest_json TEXT,manifest_sha256 TEXT,
                    created_at REAL NOT NULL,prepared_at REAL,published_at REAL,aborted_at REAL,
                    UNIQUE(tenant_id,kb_id,build_id))""",
                    postgres="""CREATE TABLE IF NOT EXISTS ha_index_generations (
                    generation_id TEXT PRIMARY KEY,tenant_id TEXT NOT NULL,kb_id TEXT NOT NULL,
                    build_id TEXT NOT NULL,status TEXT NOT NULL,base_generation_id TEXT,
                    fencing_token BIGINT NOT NULL,lease_owner TEXT NOT NULL,lease_token TEXT NOT NULL,
                    lease_expires_at DOUBLE PRECISION NOT NULL,manifest_json TEXT,manifest_sha256 TEXT,
                    created_at DOUBLE PRECISION NOT NULL,prepared_at DOUBLE PRECISION,
                    published_at DOUBLE PRECISION,aborted_at DOUBLE PRECISION,
                    UNIQUE(tenant_id,kb_id,build_id))""",
                ),
                "CREATE INDEX IF NOT EXISTS idx_ha_index_generations_scope ON ha_index_generations(tenant_id,kb_id,created_at)",
                "CREATE INDEX IF NOT EXISTS idx_ha_index_generations_cleanup ON ha_index_generations(status,published_at,aborted_at)",
            ],
        )

    @staticmethod
    def _row(row: Any | None) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        raw = result.pop("manifest_json", None)
        result["manifest"] = None if raw is None else json.loads(str(raw))
        return result

    def begin_build(
        self,
        tenant_id: str,
        kb_id: str,
        build_id: str,
        worker_id: str,
        *,
        base_generation_id: str | None = None,
        lease_seconds: float = 300.0,
    ) -> dict[str, Any]:
        tenant_id = _clean(tenant_id, "tenant_id")
        kb_id = _clean(kb_id, "kb_id")
        build_id = _clean(build_id, "build_id")
        worker_id = _clean(worker_id, "worker_id")
        if base_generation_id is not None:
            base_generation_id = _clean(base_generation_id, "base_generation_id")
        if not math.isfinite(lease_seconds) or not 5 <= lease_seconds <= 3600:
            raise ValueError("index lease_seconds must be between 5 and 3600")
        now = self._clock()
        marker = self.backend.sql(sqlite="?", postgres="%s")
        insert_head = self.backend.sql(
            sqlite="INSERT OR IGNORE INTO ha_index_heads(tenant_id,kb_id,updated_at) VALUES(?,?,?)",
            postgres="INSERT INTO ha_index_heads(tenant_id,kb_id,updated_at) VALUES(%s,%s,%s) ON CONFLICT(tenant_id,kb_id) DO NOTHING",
        )
        with self.backend.transaction(write=True) as connection:
            connection.execute(insert_head, (tenant_id, kb_id, now))
            lock = self.backend.sql(sqlite="", postgres=" FOR UPDATE")
            head = connection.execute(
                f"SELECT * FROM ha_index_heads WHERE tenant_id={marker} AND kb_id={marker}{lock}",
                (tenant_id, kb_id),
            ).fetchone()
            if head is None:
                raise IndexGenerationError("index head creation failed")
            head_row = dict(head)
            current = head_row["current_generation_id"]
            if base_generation_id is not None and current != base_generation_id:
                raise IndexConflict("index base generation changed")
            existing = connection.execute(
                f"SELECT * FROM ha_index_generations WHERE tenant_id={marker} AND kb_id={marker} "
                f"AND build_id={marker}{lock}",
                (tenant_id, kb_id, build_id),
            ).fetchone()
            if existing is not None:
                result = self._row(existing)
                assert result is not None
                if result["status"] == GEN_PUBLISHED:
                    return result
                lease_is_live = float(result["lease_expires_at"]) > now
                if lease_is_live:
                    if result["lease_owner"] != worker_id:
                        raise IndexConflict(
                            "index build_id is already owned by another worker"
                        )
                    return result
                # A worker identity is commonly stable across process restarts.
                # Expiry must therefore rotate the capability even when the
                # replacement process has the same worker_id; otherwise the
                # stable build_id can never recover without a special API call.
                if not lease_is_live:
                    if int(result["fencing_token"]) != int(head_row["fencing_token"]):
                        raise StaleIndexFence("index generation was superseded")
                    rotated = secrets.token_urlsafe(32)
                    connection.execute(
                        f"UPDATE ha_index_generations SET lease_owner={marker},"
                        f"lease_token={marker},lease_expires_at={marker} "
                        f"WHERE generation_id={marker}",
                        (
                            worker_id,
                            rotated,
                            now + lease_seconds,
                            result["generation_id"],
                        ),
                    )
                    row = connection.execute(
                        f"SELECT * FROM ha_index_generations WHERE generation_id={marker}",
                        (result["generation_id"],),
                    ).fetchone()
                    return self._row(row) or {}
            fence = int(head_row["fencing_token"]) + 1
            generation_id = f"gen-{uuid.uuid4().hex}"
            lease_token = secrets.token_urlsafe(32)
            placeholders = self.backend.sql(
                sqlite="?,?,?,?,?,?,?,?,?,?,?",
                postgres="%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s",
            )
            connection.execute(
                "INSERT INTO ha_index_generations(generation_id,tenant_id,kb_id,build_id,status,"
                "base_generation_id,fencing_token,lease_owner,lease_token,lease_expires_at,created_at) "
                f"VALUES({placeholders})",
                (
                    generation_id,
                    tenant_id,
                    kb_id,
                    build_id,
                    GEN_BUILDING,
                    current,
                    fence,
                    worker_id,
                    lease_token,
                    now + lease_seconds,
                    now,
                ),
            )
            connection.execute(
                f"UPDATE ha_index_heads SET fencing_token={marker},updated_at={marker},"
                f"revision=revision+1 WHERE tenant_id={marker} AND kb_id={marker}",
                (fence, now, tenant_id, kb_id),
            )
            row = connection.execute(
                f"SELECT * FROM ha_index_generations WHERE generation_id={marker}",
                (generation_id,),
            ).fetchone()
            return self._row(row) or {}

    def resume_build(
        self,
        generation_id: str,
        worker_id: str,
        *,
        lease_seconds: float = 300.0,
    ) -> dict[str, Any]:
        generation_id = _clean(generation_id, "generation_id")
        worker_id = _clean(worker_id, "worker_id")
        if not math.isfinite(lease_seconds) or not 5 <= lease_seconds <= 3600:
            raise ValueError("index lease_seconds must be between 5 and 3600")
        now = self._clock()
        marker = self.backend.sql(sqlite="?", postgres="%s")
        with self.backend.transaction(write=True) as connection:
            lock = self.backend.sql(sqlite="", postgres=" FOR UPDATE")
            raw = connection.execute(
                f"SELECT * FROM ha_index_generations WHERE generation_id={marker}{lock}",
                (generation_id,),
            ).fetchone()
            generation = self._row(raw)
            if generation is None or generation["status"] not in {
                GEN_BUILDING,
                GEN_PREPARED,
            }:
                raise IndexConflict("index generation cannot be resumed")
            head = connection.execute(
                f"SELECT fencing_token FROM ha_index_heads WHERE tenant_id={marker} AND kb_id={marker}{lock}",
                (generation["tenant_id"], generation["kb_id"]),
            ).fetchone()
            if head is None or int(
                head[0] if not isinstance(head, Mapping) else head["fencing_token"]
            ) != int(generation["fencing_token"]):
                raise StaleIndexFence("index generation was superseded")
            if float(generation["lease_expires_at"]) > now:
                raise IndexConflict("index generation lease is still active")
            token = secrets.token_urlsafe(32)
            connection.execute(
                f"UPDATE ha_index_generations SET lease_owner={marker},lease_token={marker},"
                f"lease_expires_at={marker} WHERE generation_id={marker}",
                (worker_id, token, now + lease_seconds, generation_id),
            )
            return (
                self._row(
                    connection.execute(
                        f"SELECT * FROM ha_index_generations WHERE generation_id={marker}",
                        (generation_id,),
                    ).fetchone()
                )
                or {}
            )

    def heartbeat(
        self, generation_id: str, lease_token: str, *, lease_seconds: float = 300.0
    ) -> dict[str, Any]:
        generation_id = _clean(generation_id, "generation_id")
        lease_token = _clean(lease_token, "lease_token", 512)
        if not math.isfinite(lease_seconds) or not 5 <= lease_seconds <= 3600:
            raise ValueError("index lease_seconds must be between 5 and 3600")
        now = self._clock()
        marker = self.backend.sql(sqlite="?", postgres="%s")
        with self.backend.transaction(write=True) as connection:
            changed = connection.execute(
                f"UPDATE ha_index_generations SET lease_expires_at={marker} "
                f"WHERE generation_id={marker} AND lease_token={marker} "
                f"AND status IN ('{GEN_BUILDING}','{GEN_PREPARED}') AND lease_expires_at>{marker}",
                (now + lease_seconds, generation_id, lease_token, now),
            )
            if changed.rowcount != 1:
                raise StaleIndexFence("index build lease is stale")
        result = self.get(generation_id)
        assert result is not None
        return result

    def prepare(
        self, generation_id: str, lease_token: str, manifest: Mapping[str, Any]
    ) -> dict[str, Any]:
        generation_id = _clean(generation_id, "generation_id")
        lease_token = _clean(lease_token, "lease_token", 512)
        normalized, digest = normalize_manifest(manifest)
        encoded = _canonical(normalized).decode()
        now = self._clock()
        marker = self.backend.sql(sqlite="?", postgres="%s")
        with self.backend.transaction(write=True) as connection:
            lock = self.backend.sql(sqlite="", postgres=" FOR UPDATE")
            row = connection.execute(
                f"SELECT * FROM ha_index_generations WHERE generation_id={marker}{lock}",
                (generation_id,),
            ).fetchone()
            current = self._row(row)
            self._require_live(current, lease_token, now, {GEN_BUILDING, GEN_PREPARED})
            assert current is not None
            if current["status"] == GEN_PREPARED:
                if current["manifest_sha256"] != digest:
                    raise IndexConflict("prepared index manifest changed")
                return current
            self._require_current_fence(connection, current)
            connection.execute(
                f"UPDATE ha_index_generations SET status='{GEN_PREPARED}',manifest_json={marker},"
                f"manifest_sha256={marker},prepared_at={marker} WHERE generation_id={marker} "
                f"AND lease_token={marker} AND status='{GEN_BUILDING}'",
                (encoded, digest, now, generation_id, lease_token),
            )
            return (
                self._row(
                    connection.execute(
                        f"SELECT * FROM ha_index_generations WHERE generation_id={marker}",
                        (generation_id,),
                    ).fetchone()
                )
                or {}
            )

    def publish(
        self,
        generation_id: str,
        lease_token: str,
        verifier: GenerationVerifier,
        *,
        on_publish: PublicationHook | None = None,
    ) -> dict[str, Any]:
        generation_id = _clean(generation_id, "generation_id")
        lease_token = _clean(lease_token, "lease_token", 512)
        candidate = self.get(generation_id)
        self._require_live(candidate, lease_token, self._clock(), {GEN_PREPARED})
        assert candidate is not None
        verifier(candidate)
        now = self._clock()
        marker = self.backend.sql(sqlite="?", postgres="%s")
        with self.backend.transaction(write=True) as connection:
            lock = self.backend.sql(sqlite="", postgres=" FOR UPDATE")
            current = self._row(
                connection.execute(
                    f"SELECT * FROM ha_index_generations WHERE generation_id={marker}{lock}",
                    (generation_id,),
                ).fetchone()
            )
            self._require_live(current, lease_token, now, {GEN_PREPARED})
            assert current is not None
            self._require_current_fence(connection, current)
            # Verify again after taking the authority locks. Local verification
            # is cheap relative to an index build; remote implementations use
            # immutable object versions and can validate the stored manifest ETag.
            verifier(current)
            head = connection.execute(
                f"SELECT current_generation_id,revision FROM ha_index_heads "
                f"WHERE tenant_id={marker} AND kb_id={marker}{lock}",
                (current["tenant_id"], current["kb_id"]),
            ).fetchone()
            if head is None:
                raise StaleIndexFence("index head is unavailable")
            head_row = dict(head)
            if head_row["current_generation_id"] != current["base_generation_id"]:
                raise IndexConflict("current index generation changed")
            changed = connection.execute(
                f"UPDATE ha_index_heads SET current_generation_id={marker},updated_at={marker},"
                f"revision=revision+1 WHERE tenant_id={marker} AND kb_id={marker} "
                f"AND revision={marker} AND fencing_token={marker}",
                (
                    generation_id,
                    now,
                    current["tenant_id"],
                    current["kb_id"],
                    int(head_row["revision"]),
                    int(current["fencing_token"]),
                ),
            )
            if changed.rowcount != 1:
                raise StaleIndexFence("index publication CAS failed")
            published = connection.execute(
                f"UPDATE ha_index_generations SET status='{GEN_PUBLISHED}',published_at={marker},"
                f"lease_expires_at={marker} WHERE generation_id={marker} AND status='{GEN_PREPARED}' "
                f"AND lease_token={marker}",
                (now, now, generation_id, lease_token),
            )
            if published.rowcount != 1:
                raise StaleIndexFence("index generation publication was superseded")
            if on_publish is not None:
                on_publish(connection, current)
        result = self.get(generation_id)
        assert result is not None
        return result

    def _require_current_fence(
        self, connection: Any, generation: Mapping[str, Any]
    ) -> None:
        marker = self.backend.sql(sqlite="?", postgres="%s")
        lock = self.backend.sql(sqlite="", postgres=" FOR UPDATE")
        head = connection.execute(
            f"SELECT fencing_token FROM ha_index_heads WHERE tenant_id={marker} AND kb_id={marker}{lock}",
            (generation["tenant_id"], generation["kb_id"]),
        ).fetchone()
        value = (
            None
            if head is None
            else (head["fencing_token"] if isinstance(head, Mapping) else head[0])
        )
        if value is None or int(value) != int(generation["fencing_token"]):
            raise StaleIndexFence("index generation was superseded")

    @staticmethod
    def _require_live(
        generation: Mapping[str, Any] | None,
        lease_token: str,
        now: float,
        statuses: set[str],
    ) -> None:
        if (
            generation is None
            or generation.get("status") not in statuses
            or generation.get("lease_token") != lease_token
            or float(generation.get("lease_expires_at") or 0) <= now
        ):
            raise StaleIndexFence("index build lease is stale or expired")

    def abort(self, generation_id: str, lease_token: str) -> dict[str, Any]:
        generation_id = _clean(generation_id, "generation_id")
        lease_token = _clean(lease_token, "lease_token", 512)
        now = self._clock()
        marker = self.backend.sql(sqlite="?", postgres="%s")
        with self.backend.transaction(write=True) as connection:
            changed = connection.execute(
                f"UPDATE ha_index_generations SET status='{GEN_ABORTED}',aborted_at={marker},"
                f"lease_expires_at={marker} WHERE generation_id={marker} AND lease_token={marker} "
                f"AND status IN ('{GEN_BUILDING}','{GEN_PREPARED}') AND lease_expires_at>{marker}",
                (now, now, generation_id, lease_token, now),
            )
            if changed.rowcount != 1:
                raise StaleIndexFence("index generation cannot be aborted")
        result = self.get(generation_id)
        assert result is not None
        return result

    def get(self, generation_id: str) -> dict[str, Any] | None:
        generation_id = _clean(generation_id, "generation_id")
        marker = self.backend.sql(sqlite="?", postgres="%s")
        with self.backend.transaction() as connection:
            return self._row(
                connection.execute(
                    f"SELECT * FROM ha_index_generations WHERE generation_id={marker}",
                    (generation_id,),
                ).fetchone()
            )

    def current(self, tenant_id: str, kb_id: str) -> dict[str, Any] | None:
        tenant_id = _clean(tenant_id, "tenant_id")
        kb_id = _clean(kb_id, "kb_id")
        marker = self.backend.sql(sqlite="?", postgres="%s")
        with self.backend.transaction() as connection:
            row = connection.execute(
                f"SELECT generations.* FROM ha_index_heads AS heads JOIN ha_index_generations "
                f"AS generations ON generations.generation_id=heads.current_generation_id "
                f"WHERE heads.tenant_id={marker} AND heads.kb_id={marker} "
                f"AND generations.status='{GEN_PUBLISHED}'",
                (tenant_id, kb_id),
            ).fetchone()
            return self._row(row)

    def list_current(
        self,
        *,
        limit: int = 100,
        after: tuple[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        """Page authoritative generations in stable tenant/KB order."""

        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("current generation limit must be between 1 and 1000")
        marker = self.backend.sql(sqlite="?", postgres="%s")
        values: tuple[Any, ...] = ()
        cursor = ""
        if after is not None:
            if not isinstance(after, tuple) or len(after) != 2:
                raise ValueError("current generation cursor is invalid")
            tenant_id = _clean(after[0], "tenant_id")
            kb_id = _clean(after[1], "kb_id")
            cursor = (
                f"AND (heads.tenant_id>{marker} OR "
                f"(heads.tenant_id={marker} AND heads.kb_id>{marker})) "
            )
            values = (tenant_id, tenant_id, kb_id)
        with self.backend.transaction() as connection:
            rows = connection.execute(
                "SELECT generations.* FROM ha_index_heads AS heads "
                "JOIN ha_index_generations AS generations "
                "ON generations.generation_id=heads.current_generation_id "
                f"WHERE generations.status='{GEN_PUBLISHED}' {cursor}"
                f"ORDER BY heads.tenant_id,heads.kb_id LIMIT {limit}",
                values,
            ).fetchall()
            return [item for row in rows if (item := self._row(row)) is not None]

    def list_tenant_generations(
        self,
        tenant_id: str,
        *,
        kb_id: str | None = None,
        status: str | None = None,
        before: tuple[float, str] | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Page immutable generations inside one tenant authority boundary."""

        tenant_id = _clean(tenant_id, "tenant_id")
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("generation limit must be between 1 and 1000")
        marker = self.backend.sql(sqlite="?", postgres="%s")
        conditions = [f"tenant_id={marker}"]
        values: list[Any] = [tenant_id]
        if kb_id is not None:
            conditions.append(f"kb_id={marker}")
            values.append(_clean(kb_id, "kb_id"))
        if status is not None:
            if status not in {GEN_BUILDING, GEN_PREPARED, GEN_PUBLISHED, GEN_ABORTED}:
                raise ValueError("generation status is invalid")
            conditions.append(f"status={marker}")
            values.append(status)
        if before is not None:
            if not isinstance(before, tuple) or len(before) != 2:
                raise ValueError("generation cursor is invalid")
            created_at = float(before[0])
            if not math.isfinite(created_at) or created_at < 0:
                raise ValueError("generation cursor is invalid")
            generation_id = _clean(before[1], "before_generation_id")
            conditions.append(
                f"(created_at<{marker} OR (created_at={marker} AND generation_id<{marker}))"
            )
            values.extend((created_at, created_at, generation_id))
        with self.backend.transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM ha_index_generations WHERE "
                + " AND ".join(conditions)
                + f" ORDER BY created_at DESC,generation_id DESC LIMIT {limit}",
                tuple(values),
            ).fetchall()
            return [item for row in rows if (item := self._row(row)) is not None]

    def resolve_current(
        self, tenant_id: str, kb_id: str, verifier: GenerationVerifier
    ) -> dict[str, Any] | None:
        """Return the current generation only after verifying immutable storage.

        Readers should use this method at generation-open boundaries. Keeping
        the verification adjacent to authority lookup prevents a damaged or
        partially restored generation from being served merely because its DB
        pointer is valid.
        """

        generation = self.current(tenant_id, kb_id)
        if generation is None:
            return None
        verifier(generation)
        # A newer generation may be published while verification is running.
        # Returning the verified old snapshot would be safe but surprising, so
        # retry at the caller instead of mixing authority generations.
        latest = self.current(tenant_id, kb_id)
        if latest is None or latest["generation_id"] != generation["generation_id"]:
            raise StaleIndexFence(
                "current index generation changed during verification"
            )
        return generation

    def garbage_candidates(
        self, *, before: float, limit: int = 100
    ) -> list[dict[str, Any]]:
        if not math.isfinite(before):
            raise ValueError("garbage collection cutoff must be finite")
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("garbage collection limit must be between 1 and 1000")
        marker = self.backend.sql(sqlite="?", postgres="%s")
        with self.backend.transaction() as connection:
            rows = connection.execute(
                "SELECT generations.* FROM ha_index_generations AS generations "
                "LEFT JOIN ha_index_heads AS heads ON heads.tenant_id=generations.tenant_id "
                "AND heads.kb_id=generations.kb_id "
                "WHERE (heads.current_generation_id IS NULL OR "
                "generations.generation_id<>heads.current_generation_id) AND ("
                f"(generations.status='{GEN_ABORTED}' AND generations.aborted_at<={marker}) OR "
                f"(generations.status='{GEN_PUBLISHED}' AND generations.published_at<={marker}) OR "
                f"(generations.status IN ('{GEN_BUILDING}','{GEN_PREPARED}') "
                f"AND generations.lease_expires_at<={marker} "
                "AND generations.fencing_token<heads.fencing_token)) "
                "ORDER BY generations.created_at,generations.generation_id "
                f"LIMIT {limit}",
                (before, before, before),
            ).fetchall()
            return [item for row in rows if (item := self._row(row)) is not None]

    def forget_collectable(self, generation_id: str, *, before: float) -> bool:
        """Remove non-authoritative generation metadata after its objects are gone."""

        generation_id = _clean(generation_id, "generation_id")
        if not math.isfinite(before):
            raise ValueError("garbage collection cutoff must be finite")
        marker = self.backend.sql(sqlite="?", postgres="%s")
        with self.backend.transaction(write=True) as connection:
            changed = connection.execute(
                "DELETE FROM ha_index_generations WHERE generation_id="
                f"{marker} AND generation_id NOT IN ("
                "SELECT current_generation_id FROM ha_index_heads "
                "WHERE current_generation_id IS NOT NULL) AND ((status="
                f"'{GEN_ABORTED}' AND aborted_at<={marker}) OR (status='{GEN_PUBLISHED}' "
                f"AND published_at<={marker}) OR (status IN ('{GEN_BUILDING}','{GEN_PREPARED}') "
                f"AND lease_expires_at<={marker} AND fencing_token<(SELECT fencing_token "
                "FROM ha_index_heads WHERE tenant_id=ha_index_generations.tenant_id "
                "AND kb_id=ha_index_generations.kb_id)))",
                (generation_id, before, before, before),
            )
            return changed.rowcount == 1


class LocalIndexRepository:
    """Immutable, fsync-durable generation directories for local HA tests/deployments."""

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    @staticmethod
    def _scope(value: str) -> str:
        return hashlib.sha256(value.encode()).hexdigest()

    def _target(self, generation: Mapping[str, Any]) -> Path:
        return (
            self.root
            / self._scope(str(generation["tenant_id"]))
            / self._scope(str(generation["kb_id"]))
            / _clean(str(generation["generation_id"]), "generation_id")
        )

    def materialize(
        self, generation: Mapping[str, Any], source_directory: str | os.PathLike[str]
    ) -> Path:
        manifest = generation.get("manifest")
        normalized, digest = normalize_manifest(manifest)  # type: ignore[arg-type]
        if digest != generation.get("manifest_sha256"):
            raise IndexIntegrityError("database index manifest hash is invalid")
        source = Path(source_directory).resolve(strict=True)
        if not source.is_dir():
            raise IndexIntegrityError("index source must be a directory")
        target = self._target(generation)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if target.exists():
            self.verify(generation)
            return target
        temporary = target.parent / f".tmp-{target.name}-{uuid.uuid4().hex}"
        temporary.mkdir(mode=0o700)
        try:
            for item in normalized["files"]:
                relative = PurePosixPath(item["path"])
                source_file = source.joinpath(*relative.parts)
                self._reject_symlink_components(source, relative)
                try:
                    info = source_file.lstat()
                except OSError as exc:
                    raise IndexIntegrityError(
                        "index source file is unavailable"
                    ) from exc
                if not stat.S_ISREG(info.st_mode):
                    raise IndexIntegrityError(
                        "index source contains a non-regular file"
                    )
                destination = temporary.joinpath(*relative.parts)
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                digest_builder = hashlib.sha256()
                size = 0
                with source_file.open("rb") as reader, destination.open("xb") as writer:
                    for chunk in iter(lambda: reader.read(_READ_CHUNK), b""):
                        size += len(chunk)
                        digest_builder.update(chunk)
                        writer.write(chunk)
                    writer.flush()
                    os.fsync(writer.fileno())
                if (
                    size != item["byte_size"]
                    or digest_builder.hexdigest() != item["sha256"]
                ):
                    raise IndexIntegrityError(
                        "index source does not match its manifest"
                    )
            manifest_path = temporary / "manifest.json"
            with manifest_path.open("xb") as handle:
                handle.write(_canonical(normalized))
                handle.flush()
                os.fsync(handle.fileno())
            self._fsync_tree_directories(temporary)
            os.rename(temporary, target)
            self._fsync_directory(target.parent)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
        self.verify(generation)
        return target

    @staticmethod
    def _reject_symlink_components(source: Path, relative: PurePosixPath) -> None:
        current = source
        for part in relative.parts:
            current = current / part
            try:
                if stat.S_ISLNK(current.lstat().st_mode):
                    raise IndexIntegrityError("index source path contains a symlink")
            except FileNotFoundError as exc:
                raise IndexIntegrityError("index source file is unavailable") from exc

    def verify(self, generation: Mapping[str, Any]) -> None:
        manifest = generation.get("manifest")
        normalized, digest = normalize_manifest(manifest)  # type: ignore[arg-type]
        if digest != generation.get("manifest_sha256"):
            raise IndexIntegrityError("database index manifest hash is invalid")
        target = self._target(generation)
        manifest_path = target / "manifest.json"
        try:
            raw_manifest = manifest_path.read_bytes()
        except OSError as exc:
            raise IndexIntegrityError(
                "index generation manifest is unavailable"
            ) from exc
        if hashlib.sha256(raw_manifest).hexdigest() != digest:
            raise IndexIntegrityError("stored index manifest is corrupt")
        expected_paths = {"manifest.json"}
        for item in normalized["files"]:
            expected_paths.add(item["path"])
            path = target.joinpath(*PurePosixPath(item["path"]).parts)
            try:
                info = path.lstat()
            except OSError as exc:
                raise IndexIntegrityError(
                    "index generation file is unavailable"
                ) from exc
            if (
                not stat.S_ISREG(info.st_mode)
                or path.is_symlink()
                or info.st_size != item["byte_size"]
            ):
                raise IndexIntegrityError("index generation file metadata is invalid")
            builder = hashlib.sha256()
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(_READ_CHUNK), b""):
                    builder.update(chunk)
            if builder.hexdigest() != item["sha256"]:
                raise IndexIntegrityError("index generation file is corrupt")
        actual_paths = {
            path.relative_to(target).as_posix()
            for path in target.rglob("*")
            if path.is_file() or path.is_symlink()
        }
        if actual_paths != expected_paths:
            raise IndexIntegrityError("index generation contains unmanifested files")

    def delete_generation(self, generation: Mapping[str, Any]) -> None:
        target = self._target(generation)
        if not target.exists():
            return
        if target.is_symlink() or not target.is_dir():
            raise IndexIntegrityError("index generation path is unsafe")
        shutil.rmtree(target)
        self._fsync_directory(target.parent)

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _fsync_tree_directories(self, root: Path) -> None:
        directories = [path for path in root.rglob("*") if path.is_dir()]
        for path in sorted(
            directories, key=lambda value: len(value.parts), reverse=True
        ):
            self._fsync_directory(path)
        self._fsync_directory(root)


__all__ = [
    "GEN_ABORTED",
    "GEN_BUILDING",
    "GEN_PREPARED",
    "GEN_PUBLISHED",
    "IndexConflict",
    "IndexGenerationError",
    "IndexGenerationStore",
    "IndexIntegrityError",
    "LocalIndexRepository",
    "PublicationHook",
    "StaleIndexFence",
    "normalize_manifest",
]
