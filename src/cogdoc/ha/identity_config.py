from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Mapping
from threading import RLock
from typing import Any

from cogdoc.ha.storage import DatabaseBackend


def _canonical(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        dict(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


class DistributedIdentityConfigRegistry:
    """Fail startup when security-sensitive identity config differs by node."""

    def __init__(self, backend: DatabaseBackend, *, clock: Any = time.time) -> None:
        self.backend = backend
        self._clock = clock
        self._lock = RLock()
        self._expected: dict[str, tuple[int, str]] = {}
        with backend.transaction(write=True) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS ha_identity_config ("
                "config_name TEXT PRIMARY KEY,config_version BIGINT NOT NULL,"
                "config_fingerprint TEXT NOT NULL,"
                "registered_at DOUBLE PRECISION NOT NULL)"
            )

    def register(
        self, config_name: str, config_version: int, payload: Mapping[str, Any]
    ) -> str:
        name = str(config_name).strip()
        if not name or len(name) > 160:
            raise ValueError("identity config name is invalid")
        if type(config_version) is not int or not 1 <= config_version <= 1_000_000:
            raise ValueError("identity config version is invalid")
        fingerprint = hashlib.sha256(
            b"cogdoc-ha-identity-config-v1\0" + _canonical(payload)
        ).hexdigest()
        marker = self.backend.sql(sqlite="?", postgres="%s")
        with self._lock, self.backend.transaction(write=True) as connection:
            connection.execute(
                "INSERT INTO ha_identity_config(config_name,config_version,"
                f"config_fingerprint,registered_at) VALUES({marker},{marker},"
                f"{marker},{marker}) "
                "ON CONFLICT(config_name) DO NOTHING",
                (name, config_version, fingerprint, float(self._clock())),
            )
            row = connection.execute(
                "SELECT config_version,config_fingerprint FROM ha_identity_config "
                f"WHERE config_name={marker}",
                (name,),
            ).fetchone()
            if row is not None:
                values = tuple(row.values()) if isinstance(row, Mapping) else tuple(row)
                stored_version, stored_fingerprint = int(values[0]), str(values[1])
                if config_version == stored_version + 1:
                    changed = connection.execute(
                        "UPDATE ha_identity_config SET config_version="
                        f"{marker},config_fingerprint={marker},registered_at={marker} "
                        f"WHERE config_name={marker} AND config_version={marker}",
                        (
                            config_version,
                            fingerprint,
                            float(self._clock()),
                            name,
                            stored_version,
                        ),
                    ).rowcount
                    if changed != 1:
                        current = connection.execute(
                            "SELECT config_version,config_fingerprint FROM "
                            f"ha_identity_config WHERE config_name={marker}",
                            (name,),
                        ).fetchone()
                        if current is None:
                            raise RuntimeError(
                                "HA identity configuration rollout lost CAS"
                            )
                        current_values = (
                            tuple(current.values())
                            if isinstance(current, Mapping)
                            else tuple(current)
                        )
                        stored_version = int(current_values[0])
                        stored_fingerprint = str(current_values[1])
                    else:
                        stored_version, stored_fingerprint = (
                            config_version,
                            fingerprint,
                        )
        if row is None:
            raise RuntimeError("HA identity configuration was not persisted")
        if stored_version != config_version or stored_fingerprint != fingerprint:
            raise RuntimeError(
                f"HA identity configuration differs across nodes: {name}"
            )
        self._expected[name] = (config_version, fingerprint)
        return fingerprint

    def check(self) -> bool:
        marker = self.backend.sql(sqlite="?", postgres="%s")
        with self._lock, self.backend.transaction() as connection:
            for name, expected in self._expected.items():
                row = connection.execute(
                    "SELECT config_version,config_fingerprint FROM "
                    f"ha_identity_config WHERE config_name={marker}",
                    (name,),
                ).fetchone()
                if row is None:
                    raise RuntimeError("HA identity configuration is unavailable")
                values = tuple(row.values()) if isinstance(row, Mapping) else tuple(row)
                if (int(values[0]), str(values[1])) != expected:
                    raise RuntimeError("HA identity configuration became stale")
        return True


__all__ = ["DistributedIdentityConfigRegistry"]
