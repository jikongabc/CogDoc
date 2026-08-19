from __future__ import annotations

import json
import re
import time
from collections.abc import Mapping
from threading import RLock
from typing import Any
from uuid import uuid4

from cogdoc.api.persistence import connect_sqlite


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
_SECRET_FIELDS = frozenset(
    {"token", "api_key", "access_key", "secret_key", "session_token"}
)
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")


def _text(value: object, field: str, *, limit: int = 512) -> str:
    result = str(value or "").strip()
    if not result or len(result) > limit or any(ord(char) < 32 for char in result):
        raise ValueError(f"{field} is invalid")
    return result


def _config(
    connector_type: str,
    raw_config: Mapping[str, Any],
    raw_secret_env: Mapping[str, str],
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
    required_secrets = {
        "zotero": {"api_key"},
        "notion": {"token"},
        "confluence": {"token"},
        "sharepoint": {"token"},
        "s3": {"access_key", "secret_key"},
    }.get(connector_type, set())
    if not required_secrets <= secret_env.keys():
        missing = sorted(required_secrets - secret_env.keys())
        raise ValueError("connector secret_env is missing: " + ",".join(missing))
    if connector_type == "url":
        urls = config.get("urls")
        if not isinstance(urls, list) or not urls or len(urls) > 1000:
            raise ValueError("url connector requires a bounded urls list")
    return config, secret_env


class ConnectionStore:
    """Durable connector definitions that never persist credential values."""

    def __init__(self, db_path: str):
        self._lock = RLock()
        self._conn = connect_sqlite(db_path)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS connector_connections ("
            "connection_id TEXT PRIMARY KEY,tenant_id TEXT NOT NULL,kb_id TEXT NOT NULL,"
            "connector_type TEXT NOT NULL,name TEXT NOT NULL,config_json TEXT NOT NULL,"
            "secret_env_json TEXT NOT NULL,owner_id TEXT NOT NULL,workspace_visible INTEGER NOT NULL,"
            "enabled INTEGER NOT NULL,created_at REAL NOT NULL,updated_at REAL NOT NULL,revision INTEGER NOT NULL)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_connector_connections_scope ON "
            "connector_connections(tenant_id,kb_id,created_at,connection_id)"
        )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def create(
        self,
        *,
        tenant_id: str,
        kb_id: str,
        connector_type: str,
        name: str,
        config: Mapping[str, Any],
        secret_env: Mapping[str, str],
        owner_id: str,
        workspace_visible: bool = False,
    ) -> dict[str, Any]:
        connector_type = _text(connector_type, "connector_type").casefold()
        if connector_type not in SUPPORTED_CONNECTOR_TYPES:
            raise ValueError("unsupported connector_type")
        clean_config, clean_secrets = _config(connector_type, config, secret_env)
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
            self._conn.execute(
                "INSERT INTO connector_connections "
                "(connection_id,tenant_id,kb_id,connector_type,name,config_json,secret_env_json,"
                "owner_id,workspace_visible,enabled,created_at,updated_at,revision) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
                ),
            )
        return self.get(connection_id) or {}

    def get(
        self, connection_id: str, *, include_secret_refs: bool = False
    ) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT connection_id,tenant_id,kb_id,connector_type,name,config_json,secret_env_json,"
                "owner_id,workspace_visible,enabled,created_at,updated_at,revision "
                "FROM connector_connections WHERE connection_id=?",
                (connection_id,),
            ).fetchone()
        return self._row(row, include_secret_refs=include_secret_refs) if row else None

    def list_entries(self, tenant_id: str, kb_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT connection_id,tenant_id,kb_id,connector_type,name,config_json,secret_env_json,"
                "owner_id,workspace_visible,enabled,created_at,updated_at,revision "
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
                "FROM connector_connections WHERE enabled=1 ORDER BY created_at,connection_id"
            ).fetchall()
        return [self._row(row, include_secret_refs=False) for row in rows]

    def set_enabled(self, connection_id: str, enabled: bool) -> dict[str, Any]:
        if type(enabled) is not bool:
            raise TypeError("enabled must be a boolean")
        with self._lock:
            updated = self._conn.execute(
                "UPDATE connector_connections SET enabled=?,updated_at=?,revision=revision+1 "
                "WHERE connection_id=?",
                (int(enabled), time.time(), connection_id),
            ).rowcount
        if updated != 1:
            raise KeyError(connection_id)
        return self.get(connection_id) or {}

    def delete(self, connection_id: str) -> bool:
        with self._lock:
            return (
                self._conn.execute(
                    "DELETE FROM connector_connections WHERE connection_id=?",
                    (connection_id,),
                ).rowcount
                == 1
            )

    @staticmethod
    def _row(row, *, include_secret_refs: bool) -> dict[str, Any]:
        secret_env = json.loads(row[6])
        result = {
            "connection_id": row[0],
            "tenant_id": row[1],
            "kb_id": row[2],
            "connector_type": row[3],
            "name": row[4],
            "config": json.loads(row[5]),
            "secret_fields": sorted(secret_env),
            "owner_id": row[7],
            "workspace_visible": bool(row[8]),
            "enabled": bool(row[9]),
            "created_at": row[10],
            "updated_at": row[11],
            "revision": row[12],
        }
        if include_secret_refs:
            result["secret_env"] = secret_env
        return result
