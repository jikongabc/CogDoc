from __future__ import annotations

import json
import re
import sqlite3
import time
from collections.abc import Iterable, Mapping
from threading import RLock
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from cogdoc.api.persistence import connect_sqlite
from cogdoc.ha.dbapi_compat import BackendDBAPIConnection
from cogdoc.ha.storage import DatabaseBackend


SUPPORTED_CONNECTOR_TYPES = frozenset(
    {
        "local-directory",
        "git",
        "url",
        "zotero",
        "notion",
        "confluence",
        "sharepoint",
        "s3",
    }
)
SECRETLESS_CONNECTOR_TYPES = frozenset({"local-directory", "git", "url"})
CONNECTOR_PROVIDER_ALIASES = {
    "zotero": frozenset({"zotero"}),
    "notion": frozenset({"notion"}),
    "confluence": frozenset({"confluence", "atlassian"}),
    "sharepoint": frozenset({"sharepoint", "microsoft"}),
    "s3": frozenset({"s3", "aws"}),
}
_SECRET_FIELDS = frozenset(
    {
        "token",
        "access_token",
        "refresh_token",
        "access_token_expires_at",
        "api_key",
        "access_key",
        "secret_key",
        "session_token",
        "cloud_id",
        "site_url",
    }
)
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")
_S3_BUCKET = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,62}\Z")
_S3_REGION = re.compile(r"[a-z0-9][a-z0-9-]{0,62}\Z")


class ConnectionRevisionConflict(ValueError):
    """A connection changed after an asynchronous control-plane read."""


class ConnectionLimitError(ValueError):
    """The configured connector-definition cardinality bound was reached."""


def connector_provider_matches(connector_type: str, provider: str) -> bool:
    allowed = CONNECTOR_PROVIDER_ALIASES.get(str(connector_type).casefold())
    return allowed is not None and str(provider).casefold() in allowed


def validate_connector_secret_fields(
    connector_type: str, secret_fields: Iterable[str]
) -> set[str]:
    """Validate the least-privilege secret contract for a connector."""

    kind = str(connector_type).strip().casefold()
    if kind not in SUPPORTED_CONNECTOR_TYPES:
        raise ValueError("unsupported connector_type")
    fields = {str(field).strip().casefold() for field in secret_fields}
    if not fields <= _SECRET_FIELDS:
        raise ValueError("credential contains unsupported secret fields")
    if kind in SECRETLESS_CONNECTOR_TYPES:
        if fields:
            raise ValueError(f"{kind} connector does not accept credentials")
        return fields
    allowed = {
        "zotero": {"api_key"},
        "notion": {
            "token",
            "access_token",
            "refresh_token",
            "access_token_expires_at",
        },
        "confluence": {
            "token",
            "access_token",
            "refresh_token",
            "access_token_expires_at",
            "cloud_id",
            "site_url",
        },
        "sharepoint": {
            "token",
            "access_token",
            "refresh_token",
            "access_token_expires_at",
        },
        "s3": {"access_key", "secret_key", "session_token"},
    }[kind]
    unexpected = sorted(fields - allowed)
    if unexpected:
        raise ValueError(
            "connector credentials contain unsupported fields: " + ",".join(unexpected)
        )
    available = set(fields)
    if "access_token" in available:
        # Preserve compatibility with credentials produced by early OAuth
        # control-plane previews while exposing only the connector token alias.
        available.add("token")
    required = {
        "zotero": {"api_key"},
        "notion": {"token"},
        "confluence": {"token"},
        "sharepoint": {"token"},
        "s3": {"access_key", "secret_key"},
    }[kind]
    missing = sorted(required - available)
    if missing:
        raise ValueError("connector credentials are missing: " + ",".join(missing))
    return fields


def _text(value: object, field: str, *, limit: int = 512) -> str:
    result = str(value or "").strip()
    if not result or len(result) > limit or any(ord(char) < 32 for char in result):
        raise ValueError(f"{field} is invalid")
    return result


def _non_secret_https_url(value: object, field: str) -> str:
    url = _text(value, field, limit=4096)
    try:
        parts = urlsplit(url)
        # Accessing port also rejects malformed bracketed hosts and ports.
        parts.port
    except ValueError as exc:
        raise ValueError(f"{field} is invalid") from exc
    if (
        parts.scheme.casefold() != "https"
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
    ):
        raise ValueError(f"{field} must be HTTPS without userinfo, query, or fragment")
    return url


def _config(
    connector_type: str,
    raw_config: Mapping[str, Any],
    raw_secret_env: Mapping[str, str],
    credential_fields: Iterable[str] = (),
) -> tuple[dict[str, Any], dict[str, str]]:
    if not isinstance(raw_config, Mapping) or not isinstance(raw_secret_env, Mapping):
        raise TypeError("connector config and secret_env must be mappings")
    if any(str(key).casefold() in _SECRET_FIELDS for key in raw_config):
        raise ValueError("secret values must be referenced through secret_env")
    try:
        json.dumps(raw_config, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("connector config must be JSON-safe") from exc
    encoded = json.dumps(raw_config, sort_keys=True, separators=(",", ":"))
    if len(encoded) > 32_768:
        raise ValueError("connector config is too large")
    config = json.loads(encoded)
    allowed_fields = {
        "local-directory": {"root", "follow_symlinks", "schedule_seconds"},
        "git": {"repository", "ref", "subpath", "schedule_seconds"},
        "url": {"urls", "schedule_seconds"},
        "zotero": {"library_type", "library_id", "schedule_seconds"},
        "notion": {"schedule_seconds"},
        "confluence": {"base_url", "include_acl", "schedule_seconds"},
        "sharepoint": {"site_id", "drive_id", "include_acl", "schedule_seconds"},
        "s3": {"bucket", "region", "prefix", "endpoint", "schedule_seconds"},
    }[connector_type]
    unexpected = sorted(set(config) - allowed_fields)
    if unexpected:
        raise ValueError(
            "connector config contains unsupported fields: " + ",".join(unexpected)
        )
    schedule = config.get("schedule_seconds")
    if schedule is not None and (
        type(schedule) is not int or not 60 <= schedule <= 31_536_000
    ):
        raise ValueError("schedule_seconds must be between 60 and 31536000")
    secret_env: dict[str, str] = {}
    for field, env_name in raw_secret_env.items():
        key = str(field).strip().casefold()
        if key not in _SECRET_FIELDS or not _ENV_NAME.fullmatch(str(env_name)):
            raise ValueError("secret_env contains an unsupported reference")
        secret_env[key] = str(env_name)
    vault_fields = {str(field).strip().casefold() for field in credential_fields}
    if vault_fields and secret_env:
        raise ValueError("credential_id and secret_env cannot be combined")

    required: dict[str, tuple[str, ...]] = {
        "local-directory": ("root",),
        "git": ("repository",),
        "url": ("urls",),
        "zotero": ("library_type", "library_id"),
        "notion": (),
        "confluence": ("base_url",),
        "sharepoint": ("site_id", "drive_id"),
        "s3": ("bucket", "region"),
    }
    for field in required[connector_type]:
        if config.get(field) in (None, "", []):
            raise ValueError(f"connector config requires {field}")
    if connector_type == "confluence":
        config["base_url"] = _non_secret_https_url(config["base_url"], "base_url")
    if connector_type == "s3" and config.get("endpoint") not in (None, ""):
        config["endpoint"] = _non_secret_https_url(config["endpoint"], "endpoint")
    if connector_type == "s3":
        if not _S3_BUCKET.fullmatch(str(config["bucket"])):
            raise ValueError("bucket is invalid")
        if not _S3_REGION.fullmatch(str(config["region"])):
            raise ValueError("region is invalid")
    available_secrets = set(secret_env) | vault_fields
    validate_connector_secret_fields(connector_type, available_secrets)
    if connector_type == "url":
        urls = config.get("urls")
        if not isinstance(urls, list) or not urls or len(urls) > 1000:
            raise ValueError("url connector requires a bounded urls list")
        config["urls"] = [
            _non_secret_https_url(url, f"urls[{index}]")
            for index, url in enumerate(urls)
        ]
    return config, secret_env


class ConnectionStore:
    """Durable connector definitions that never persist credential values."""

    def __init__(
        self,
        db_path: str | None = None,
        *,
        backend: DatabaseBackend | None = None,
        max_connections_global: int = 10_000,
        max_connections_per_tenant: int = 1_000,
        max_connections_per_kb: int = 100,
    ):
        for name, value in (
            ("max_connections_global", max_connections_global),
            ("max_connections_per_tenant", max_connections_per_tenant),
            ("max_connections_per_kb", max_connections_per_kb),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if max_connections_per_kb > max_connections_per_tenant:
            raise ValueError(
                "max_connections_per_kb cannot exceed max_connections_per_tenant"
            )
        if max_connections_per_tenant > max_connections_global:
            raise ValueError(
                "max_connections_per_tenant cannot exceed max_connections_global"
            )
        self.max_connections_global = max_connections_global
        self.max_connections_per_tenant = max_connections_per_tenant
        self.max_connections_per_kb = max_connections_per_kb
        if (db_path is None) == (backend is None):
            raise ValueError("exactly one of db_path or backend is required")
        self._lock = RLock()
        self._distributed = backend is not None
        self._conn: Any = (
            BackendDBAPIConnection(backend)
            if backend is not None
            else connect_sqlite(str(db_path))
        )
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS connector_connections ("
            "connection_id TEXT PRIMARY KEY,tenant_id TEXT NOT NULL,kb_id TEXT NOT NULL,"
            "connector_type TEXT NOT NULL,name TEXT NOT NULL,config_json TEXT NOT NULL,"
            "secret_env_json TEXT NOT NULL,owner_id TEXT NOT NULL,workspace_visible INTEGER NOT NULL,"
            "enabled INTEGER NOT NULL,created_at REAL NOT NULL,updated_at REAL NOT NULL,revision INTEGER NOT NULL,"
            "credential_id TEXT,credential_fields_json TEXT NOT NULL DEFAULT '[]',"
            "deleting INTEGER NOT NULL DEFAULT 0,delete_index_job_id TEXT)"
        )
        if not self._distributed:
            self._ensure_column("credential_id", "TEXT")
            self._ensure_column("credential_fields_json", "TEXT NOT NULL DEFAULT '[]'")
            self._ensure_column("deleting", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column("delete_index_job_id", "TEXT")
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_connector_connections_scope ON "
            "connector_connections(tenant_id,kb_id,created_at,connection_id)"
        )

    def _ensure_column(self, name: str, definition: str) -> None:
        with self._lock:
            columns = {
                str(row[1])
                for row in self._conn.execute(
                    "PRAGMA table_info(connector_connections)"
                ).fetchall()
            }
            if name in columns:
                return
            try:
                self._conn.execute(
                    f"ALTER TABLE connector_connections ADD COLUMN {name} {definition}"
                )
            except sqlite3.OperationalError:
                # A concurrently starting process may have completed the same
                # additive migration after this connection's schema read.
                refreshed = {
                    str(row[1])
                    for row in self._conn.execute(
                        "PRAGMA table_info(connector_connections)"
                    ).fetchall()
                }
                if name not in refreshed:
                    raise

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def check(self) -> bool:
        """Reference the required schema with a bounded readiness query."""

        with self._lock:
            self._conn.execute("SELECT 1 FROM connector_connections LIMIT 1").fetchone()
        return True

    def create(
        self,
        *,
        tenant_id: str,
        kb_id: str,
        connector_type: str,
        name: str,
        config: Mapping[str, Any],
        secret_env: Mapping[str, str],
        credential_id: str | None = None,
        credential_fields: Iterable[str] = (),
        owner_id: str,
        workspace_visible: bool = False,
    ) -> dict[str, Any]:
        connector_type = _text(connector_type, "connector_type").casefold()
        if connector_type not in SUPPORTED_CONNECTOR_TYPES:
            raise ValueError("unsupported connector_type")
        clean_credential_id = (
            _text(credential_id, "credential_id", limit=160)
            if credential_id is not None
            else None
        )
        clean_credential_fields = sorted(
            {str(field).strip().casefold() for field in credential_fields}
        )
        if clean_credential_fields and clean_credential_id is None:
            raise ValueError("credential_fields require credential_id")
        if (
            connector_type in SECRETLESS_CONNECTOR_TYPES
            and clean_credential_id is not None
        ):
            raise ValueError(f"{connector_type} connector does not accept credentials")
        clean_config, clean_secrets = _config(
            connector_type,
            config,
            secret_env,
            clean_credential_fields,
        )
        values = (
            _text(tenant_id, "tenant_id", limit=160),
            _text(kb_id, "kb_id", limit=160),
            connector_type,
            _text(name, "name", limit=160),
            _text(owner_id, "owner_id", limit=160),
        )
        if type(workspace_visible) is not bool:
            raise TypeError("workspace_visible must be a boolean")
        now = time.time()
        connection_id = f"conn-{uuid4().hex}"
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                global_count = int(
                    self._conn.execute(
                        "SELECT COUNT(*) FROM connector_connections"
                    ).fetchone()[0]
                )
                tenant_count = int(
                    self._conn.execute(
                        "SELECT COUNT(*) FROM connector_connections WHERE tenant_id=?",
                        (values[0],),
                    ).fetchone()[0]
                )
                kb_count = int(
                    self._conn.execute(
                        "SELECT COUNT(*) FROM connector_connections "
                        "WHERE tenant_id=? AND kb_id=?",
                        (values[0], values[1]),
                    ).fetchone()[0]
                )
                if global_count >= self.max_connections_global:
                    raise ConnectionLimitError(
                        "global connector connection limit reached"
                    )
                if tenant_count >= self.max_connections_per_tenant:
                    raise ConnectionLimitError(
                        "tenant connector connection limit reached"
                    )
                if kb_count >= self.max_connections_per_kb:
                    raise ConnectionLimitError(
                        "knowledge-base connector connection limit reached"
                    )
                self._conn.execute(
                    "INSERT INTO connector_connections "
                    "(connection_id,tenant_id,kb_id,connector_type,name,config_json,secret_env_json,"
                    "owner_id,workspace_visible,enabled,created_at,updated_at,revision,"
                    "credential_id,credential_fields_json) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        connection_id,
                        *values[:4],
                        json.dumps(clean_config, sort_keys=True),
                        json.dumps(clean_secrets, sort_keys=True),
                        values[4],
                        int(workspace_visible),
                        1,
                        now,
                        now,
                        1,
                        clean_credential_id,
                        json.dumps(clean_credential_fields),
                    ),
                )
                self._conn.execute("COMMIT")
            except Exception:
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")
                raise
        return self.get(connection_id) or {}

    def get(
        self, connection_id: str, *, include_secret_refs: bool = False
    ) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT connection_id,tenant_id,kb_id,connector_type,name,config_json,secret_env_json,"
                "owner_id,workspace_visible,enabled,created_at,updated_at,revision "
                ",credential_id,credential_fields_json,deleting,delete_index_job_id "
                "FROM connector_connections WHERE connection_id=?",
                (connection_id,),
            ).fetchone()
        return self._row(row, include_secret_refs=include_secret_refs) if row else None

    def list_entries(self, tenant_id: str, kb_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT connection_id,tenant_id,kb_id,connector_type,name,config_json,secret_env_json,"
                "owner_id,workspace_visible,enabled,created_at,updated_at,revision "
                ",credential_id,credential_fields_json,deleting,delete_index_job_id "
                "FROM connector_connections WHERE tenant_id=? AND kb_id=? "
                "ORDER BY created_at,connection_id",
                (tenant_id, kb_id),
            ).fetchall()
        return [self._row(row, include_secret_refs=False) for row in rows]

    def enabled(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT connection_id,tenant_id,kb_id,connector_type,name,config_json,secret_env_json,"
                "owner_id,workspace_visible,enabled,created_at,updated_at,revision "
                ",credential_id,credential_fields_json,deleting,delete_index_job_id "
                "FROM connector_connections WHERE enabled=1 ORDER BY created_at,connection_id"
            ).fetchall()
        return [self._row(row, include_secret_refs=False) for row in rows]

    def disable_scope(self, tenant_id: str, kb_id: str) -> int:
        """Revoke every connection in a KB incarnation before its teardown."""

        tenant = _text(tenant_id, "tenant_id", limit=160)
        knowledge_base = _text(kb_id, "kb_id", limit=160)
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE connector_connections SET enabled=0,updated_at=?,revision=revision+1 "
                "WHERE tenant_id=? AND kb_id=? AND enabled=1",
                (time.time(), tenant, knowledge_base),
            )
        return int(cursor.rowcount)

    def delete_scope(self, tenant_id: str, kb_id: str) -> int:
        """Idempotently remove definitions after the scope has quiesced."""

        tenant = _text(tenant_id, "tenant_id", limit=160)
        knowledge_base = _text(kb_id, "kb_id", limit=160)
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM connector_connections WHERE tenant_id=? AND kb_id=?",
                (tenant, knowledge_base),
            )
        return int(cursor.rowcount)

    def set_enabled(self, connection_id: str, enabled: bool) -> dict[str, Any]:
        if type(enabled) is not bool:
            raise TypeError("enabled must be a boolean")
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT deleting FROM connector_connections WHERE connection_id=?",
                    (connection_id,),
                ).fetchone()
                if row is None:
                    raise KeyError(connection_id)
                if bool(row[0]):
                    raise ValueError("connection deletion is in progress")
                updated = self._conn.execute(
                    "UPDATE connector_connections SET enabled=?,updated_at=?,revision=revision+1 "
                    "WHERE connection_id=?",
                    (int(enabled), time.time(), connection_id),
                ).rowcount
                self._conn.execute("COMMIT")
            except Exception:
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")
                raise
        if updated != 1:
            raise KeyError(connection_id)
        return self.get(connection_id) or {}

    def fence_delete(
        self, tenant_id: str, kb_id: str, connection_id: str
    ) -> dict[str, Any]:
        """Durably reject reactivation and credential mutation during teardown."""

        tenant = _text(tenant_id, "tenant_id", limit=160)
        knowledge_base = _text(kb_id, "kb_id", limit=160)
        connection = _text(connection_id, "connection_id", limit=160)
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT tenant_id,kb_id,enabled,deleting,delete_index_job_id "
                    "FROM connector_connections WHERE connection_id=?",
                    (connection,),
                ).fetchone()
                if row is None:
                    raise KeyError(connection)
                if str(row[0]) != tenant or str(row[1]) != knowledge_base:
                    raise ValueError("connection does not belong to knowledge base")
                if bool(row[2]) or not bool(row[3]):
                    self._conn.execute(
                        "UPDATE connector_connections SET enabled=0,deleting=1,"
                        "delete_index_job_id=?,updated_at=?,revision=revision+1 "
                        "WHERE connection_id=?",
                        (
                            None if not bool(row[3]) else row[4],
                            time.time(),
                            connection,
                        ),
                    )
                self._conn.execute("COMMIT")
            except Exception:
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")
                raise
        return self.get(connection, include_secret_refs=True) or {}

    def record_delete_index_job(
        self, connection_id: str, job_id: str
    ) -> dict[str, Any]:
        """Persist the rebuild handle used by retryable connection teardown."""

        connection = _text(connection_id, "connection_id", limit=160)
        job = _text(job_id, "job_id", limit=180)
        with self._lock:
            updated = self._conn.execute(
                "UPDATE connector_connections SET delete_index_job_id=?,updated_at=? "
                "WHERE connection_id=? AND deleting=1 AND enabled=0",
                (job, time.time(), connection),
            ).rowcount
        if updated != 1:
            row = self.get(connection)
            if row is None:
                raise KeyError(connection)
            raise ValueError("connection deletion fence is not active")
        return self.get(connection, include_secret_refs=True) or {}

    def delete(self, connection_id: str) -> bool:
        with self._lock:
            return (
                self._conn.execute(
                    "DELETE FROM connector_connections WHERE connection_id=?",
                    (connection_id,),
                ).rowcount
                == 1
            )

    def set_credential(
        self,
        connection_id: str,
        credential_id: str,
        credential_fields: Iterable[str],
        *,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        clean_id = _text(credential_id, "credential_id", limit=160)
        clean_fields = sorted(
            {str(field).strip().casefold() for field in credential_fields}
        )
        if not clean_fields or not set(clean_fields) <= _SECRET_FIELDS:
            raise ValueError("credential fields are invalid")
        if expected_revision is not None and (
            isinstance(expected_revision, bool) or expected_revision < 1
        ):
            raise ValueError("expected_revision must be a positive integer")
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT connector_type,config_json,revision,deleting "
                    "FROM connector_connections WHERE connection_id=?",
                    (connection_id,),
                ).fetchone()
                if row is None:
                    raise KeyError(connection_id)
                if bool(row[3]):
                    raise ConnectionRevisionConflict(
                        "connection deletion is in progress"
                    )
                if expected_revision is not None and int(row[2]) != expected_revision:
                    raise ConnectionRevisionConflict("connection revision has changed")
                _config(str(row[0]), json.loads(row[1]), {}, clean_fields)
                updated = self._conn.execute(
                    "UPDATE connector_connections SET credential_id=?,credential_fields_json=?,"
                    "secret_env_json='{}',updated_at=?,revision=revision+1 WHERE connection_id=? "
                    "AND revision=?",
                    (
                        clean_id,
                        json.dumps(clean_fields),
                        time.time(),
                        connection_id,
                        int(row[2]),
                    ),
                ).rowcount
                self._conn.execute("COMMIT")
            except Exception:
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")
                raise
        if updated != 1:  # pragma: no cover - guarded inside the same lock
            raise ConnectionRevisionConflict("connection revision has changed")
        return self.get(connection_id) or {}

    def credential_references(
        self, tenant_id: str, kb_id: str, credential_id: str
    ) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT connection_id FROM connector_connections WHERE tenant_id=? "
                "AND kb_id=? AND credential_id=? ORDER BY connection_id",
                (tenant_id, kb_id, credential_id),
            ).fetchall()
        return [str(row[0]) for row in rows]

    def restore_secret_reference(
        self,
        connection_id: str,
        *,
        credential_id: str | None,
        credential_fields: Iterable[str],
        secret_env: Mapping[str, str],
        expected_revision: int,
    ) -> dict[str, Any]:
        """CAS-restore the reference captured before an OAuth binding."""

        clean_connection_id = _text(connection_id, "connection_id", limit=160)
        clean_credential_id = (
            _text(credential_id, "credential_id", limit=160)
            if credential_id is not None
            else None
        )
        clean_fields = sorted(
            {str(field).strip().casefold() for field in credential_fields}
        )
        if clean_credential_id is None and clean_fields:
            raise ValueError("credential_fields require credential_id")
        if clean_credential_id is not None and not clean_fields:
            raise ValueError("credential fields are required")
        if isinstance(expected_revision, bool) or expected_revision < 1:
            raise ValueError("expected_revision must be a positive integer")
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT connector_type,config_json,revision,deleting "
                    "FROM connector_connections WHERE connection_id=?",
                    (clean_connection_id,),
                ).fetchone()
                if row is None:
                    raise KeyError(clean_connection_id)
                if bool(row[3]):
                    raise ConnectionRevisionConflict(
                        "connection deletion is in progress"
                    )
                if int(row[2]) != expected_revision:
                    raise ConnectionRevisionConflict("connection revision has changed")
                _config(
                    str(row[0]),
                    json.loads(row[1]),
                    secret_env,
                    clean_fields,
                )
                clean_secret_env = {
                    str(field).strip().casefold(): str(env_name)
                    for field, env_name in secret_env.items()
                }
                updated = self._conn.execute(
                    "UPDATE connector_connections SET credential_id=?,"
                    "credential_fields_json=?,secret_env_json=?,updated_at=?,"
                    "revision=revision+1 WHERE connection_id=? AND revision=?",
                    (
                        clean_credential_id,
                        json.dumps(clean_fields),
                        json.dumps(clean_secret_env, sort_keys=True),
                        time.time(),
                        clean_connection_id,
                        expected_revision,
                    ),
                ).rowcount
                self._conn.execute("COMMIT")
            except Exception:
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")
                raise
        if updated != 1:  # pragma: no cover - guarded by the same store lock
            raise ConnectionRevisionConflict("connection revision has changed")
        return self.get(clean_connection_id, include_secret_refs=True) or {}

    @staticmethod
    def _row(row, *, include_secret_refs: bool) -> dict[str, Any]:
        secret_env = json.loads(row[6])
        credential_id = str(row[13] or "") or None
        credential_fields = json.loads(row[14] or "[]")
        secret_fields = sorted(set(secret_env) | set(credential_fields))
        result = {
            "connection_id": row[0],
            "tenant_id": row[1],
            "kb_id": row[2],
            "connector_type": row[3],
            "name": row[4],
            "config": json.loads(row[5]),
            "secret_fields": secret_fields,
            "credential_id": credential_id,
            "credential_source": (
                "vault" if credential_id else "environment" if secret_env else "none"
            ),
            "owner_id": row[7],
            "workspace_visible": bool(row[8]),
            "enabled": bool(row[9]),
            "created_at": row[10],
            "updated_at": row[11],
            "revision": row[12],
            "deleting": bool(row[15]),
            "delete_index_job_id": str(row[16]) if row[16] else None,
        }
        if include_secret_refs:
            result["secret_env"] = secret_env
        return result
