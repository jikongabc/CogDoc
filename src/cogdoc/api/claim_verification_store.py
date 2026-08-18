from __future__ import annotations

import sqlite3
import time
from collections import Counter
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from threading import RLock
from typing import Any

from cogdoc.api.persistence import connect_sqlite
from cogdoc.service.claim_verification_policy import (
    claim_verification_policy_projection,
)
from cogdoc.service.claim_verification_rollout import ROLLOUT_DECISIONS


_MODES = frozenset({"off", "shadow", "enforce"})
_TASK_TYPES = frozenset({"qa", "summary", "compare"})
_AUDIT_STATUSES = frozenset(
    {"not_run", "passed", "failed", "repaired", "rejected", "error"}
)


def _utc_iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


def _normalize_observation(
    tenant_id: str,
    task_type: str,
    rollout: Any,
    *,
    observed_at: float,
) -> dict[str, Any] | None:
    if not isinstance(rollout, Mapping):
        return None
    tenant = str(tenant_id or "").strip()
    task = str(task_type or "").strip()
    mode = str(rollout.get("mode") or "")
    decision = str(rollout.get("decision") or "")
    if (
        not tenant
        or len(tenant) > 160
        or any(ord(character) < 32 or ord(character) == 127 for character in tenant)
        or task not in _TASK_TYPES
    ):
        return None
    if mode not in _MODES or decision not in ROLLOUT_DECISIONS:
        return None
    policy = claim_verification_policy_projection(rollout, effective_mode=mode)
    if not policy["policy_id"]:
        return None
    audit_status = str(rollout.get("audit_status") or "not_run")
    if audit_status not in _AUDIT_STATUSES:
        audit_status = "error"
    return {
        "tenant_id": tenant,
        "observed_at": float(observed_at),
        "task_type": task,
        "configured_mode": policy["configured_mode"],
        "effective_mode": mode,
        "cohort_selected": bool(policy["cohort_selected"]),
        "policy_id": policy["policy_id"],
        "decision": decision,
        "audit_status": audit_status,
        "executed": bool(rollout.get("executed", False)),
        "released": bool(rollout.get("released", True)),
        "would_intervene": bool(rollout.get("would_intervene", False)),
        "would_repair": bool(rollout.get("would_repair", False)),
        "would_block": bool(rollout.get("would_block", False)),
    }


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 6) if denominator else None


def _summary(
    rows: list[Mapping[str, Any]],
    *,
    tenant_id: str,
    window_hours: int,
    now: float,
    operational_min_samples: int,
    operational_max_error_rate: float,
    effective_mode_filter: str | None,
    policy_id_filter: str | None,
) -> dict[str, Any]:
    total = len(rows)
    executed = sum(bool(row.get("executed")) for row in rows)
    errors = sum(
        bool(row.get("executed"))
        and str(row.get("audit_status")) == "error"
        for row in rows
    )
    intervention = sum(bool(row.get("would_intervene")) for row in rows)
    repairs = sum(bool(row.get("would_repair")) for row in rows)
    blocks = sum(bool(row.get("would_block")) for row in rows)
    released = sum(bool(row.get("released")) for row in rows)
    eligible = [row for row in rows if row.get("effective_mode") != "off"]
    eligible_executed = sum(bool(row.get("executed")) for row in eligible)
    eligible_errors = sum(
        bool(row.get("executed"))
        and str(row.get("audit_status")) == "error"
        for row in eligible
    )
    operational_error_rate = _rate(eligible_errors, eligible_executed)
    blockers: list[str] = []
    if eligible_executed < operational_min_samples:
        blockers.append("minimum_samples")
    if (
        operational_error_rate is None
        or operational_error_rate > operational_max_error_rate
    ):
        blockers.append("verifier_error_rate")
    return {
        "tenant_id": tenant_id,
        "window_hours": window_hours,
        "window_start": _utc_iso(now - window_hours * 3600),
        "generated_at": _utc_iso(now),
        "effective_mode_filter": effective_mode_filter,
        "policy_id_filter": policy_id_filter,
        "total_count": total,
        "counts": {
            "executed": executed,
            "errors": errors,
            "released": released,
            "would_intervene": intervention,
            "would_repair": repairs,
            "would_block": blocks,
        },
        "rates": {
            "execution_rate": _rate(executed, total),
            "error_rate": _rate(errors, executed),
            "intervention_rate": _rate(intervention, total),
            "repair_rate": _rate(repairs, total),
            "block_rate": _rate(blocks, total),
        },
        "by_configured_mode": dict(
            sorted(Counter(str(row["configured_mode"]) for row in rows).items())
        ),
        "by_effective_mode": dict(
            sorted(Counter(str(row["effective_mode"]) for row in rows).items())
        ),
        "by_decision": dict(
            sorted(Counter(str(row["decision"]) for row in rows).items())
        ),
        "by_task_type": dict(
            sorted(Counter(str(row["task_type"]) for row in rows).items())
        ),
        "operational_readiness": {
            "ready": not blockers,
            "sample_count": eligible_executed,
            "minimum_samples": operational_min_samples,
            "verifier_error_rate": operational_error_rate,
            "maximum_verifier_error_rate": operational_max_error_rate,
            "blockers": blockers,
            "semantic_release_gate_required": True,
        },
    }


class ClaimVerificationObservationStore:
    """Bounded process-local store used by isolated app factories and tests."""

    def __init__(
        self,
        *,
        retention_days: int = 30,
        max_per_tenant: int = 100_000,
        clock: Callable[[], float] = time.time,
    ):
        self.retention_seconds = max(1, int(retention_days)) * 86400
        self.max_per_tenant = max(1, int(max_per_tenant))
        self._clock = clock
        self._lock = RLock()
        self._rows: list[dict[str, Any]] = []

    def record(self, tenant_id: str, task_type: str, rollout: Any) -> bool:
        now = self._clock()
        row = _normalize_observation(
            tenant_id, task_type, rollout, observed_at=now
        )
        if row is None:
            return False
        cutoff = now - self.retention_seconds
        with self._lock:
            self._rows = [
                item
                for item in self._rows
                if float(item["observed_at"]) >= cutoff
            ]
            self._rows.append(row)
            tenant_rows = [
                item for item in self._rows if item["tenant_id"] == row["tenant_id"]
            ]
            if len(tenant_rows) > self.max_per_tenant:
                remove_count = len(tenant_rows) - self.max_per_tenant
                remove_ids = {id(item) for item in tenant_rows[:remove_count]}
                self._rows = [
                    item for item in self._rows if id(item) not in remove_ids
                ]
        return True

    def summary(
        self,
        tenant_id: str,
        *,
        window_hours: int,
        effective_mode: str | None = None,
        policy_id: str | None = None,
        operational_min_samples: int = 200,
        operational_max_error_rate: float = 0.02,
    ) -> dict[str, Any]:
        now = self._clock()
        cutoff = now - int(window_hours) * 3600
        with self._lock:
            rows = [
                dict(row)
                for row in self._rows
                if row["tenant_id"] == tenant_id
                and float(row["observed_at"]) >= cutoff
                and (effective_mode is None or row["effective_mode"] == effective_mode)
                and (policy_id is None or row["policy_id"] == policy_id)
            ]
        return _summary(
            rows,
            tenant_id=tenant_id,
            window_hours=window_hours,
            now=now,
            operational_min_samples=operational_min_samples,
            operational_max_error_rate=operational_max_error_rate,
            effective_mode_filter=effective_mode,
            policy_id_filter=policy_id,
        )


class SqliteClaimVerificationObservationStore:
    """Durable, tenant-scoped rollout observations without answer or query text."""

    def __init__(
        self,
        db_path: str,
        *,
        retention_days: int = 30,
        max_per_tenant: int = 100_000,
        clock: Callable[[], float] = time.time,
    ):
        self.retention_seconds = max(1, int(retention_days)) * 86400
        self.max_per_tenant = max(1, int(max_per_tenant))
        self._clock = clock
        self._lock = RLock()
        self._conn = connect_sqlite(db_path, busy_timeout_ms=250)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS claim_verification_observations ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL, "
            "observed_at REAL NOT NULL, task_type TEXT NOT NULL, "
            "configured_mode TEXT NOT NULL, effective_mode TEXT NOT NULL, "
            "cohort_selected INTEGER NOT NULL, policy_id TEXT NOT NULL, "
            "decision TEXT NOT NULL, audit_status TEXT NOT NULL, "
            "executed INTEGER NOT NULL, released INTEGER NOT NULL, "
            "would_intervene INTEGER NOT NULL, would_repair INTEGER NOT NULL, "
            "would_block INTEGER NOT NULL)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_claim_verification_tenant_time "
            "ON claim_verification_observations(tenant_id, observed_at DESC, id DESC)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_claim_verification_observed_at "
            "ON claim_verification_observations(observed_at)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_claim_verification_tenant_policy "
            "ON claim_verification_observations(tenant_id, policy_id, observed_at DESC)"
        )

    def record(self, tenant_id: str, task_type: str, rollout: Any) -> bool:
        now = self._clock()
        row = _normalize_observation(
            tenant_id, task_type, rollout, observed_at=now
        )
        if row is None:
            return False
        values = (
            row["tenant_id"],
            row["observed_at"],
            row["task_type"],
            row["configured_mode"],
            row["effective_mode"],
            int(row["cohort_selected"]),
            row["policy_id"],
            row["decision"],
            row["audit_status"],
            int(row["executed"]),
            int(row["released"]),
            int(row["would_intervene"]),
            int(row["would_repair"]),
            int(row["would_block"]),
        )
        with self._lock:
            self._conn.execute(
                "INSERT INTO claim_verification_observations(tenant_id,observed_at,"
                "task_type,configured_mode,effective_mode,cohort_selected,policy_id,"
                "decision,audit_status,executed,released,would_intervene,would_repair,"
                "would_block) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                values,
            )
            self._conn.execute(
                "DELETE FROM claim_verification_observations "
                "WHERE observed_at<?",
                (now - self.retention_seconds,),
            )
            self._conn.execute(
                "DELETE FROM claim_verification_observations WHERE tenant_id=? AND id "
                "NOT IN (SELECT id FROM claim_verification_observations "
                "WHERE tenant_id=? ORDER BY observed_at DESC,id DESC LIMIT ?)",
                (row["tenant_id"], row["tenant_id"], self.max_per_tenant),
            )
        return True

    def summary(
        self,
        tenant_id: str,
        *,
        window_hours: int,
        effective_mode: str | None = None,
        policy_id: str | None = None,
        operational_min_samples: int = 200,
        operational_max_error_rate: float = 0.02,
    ) -> dict[str, Any]:
        now = self._clock()
        clauses = ["tenant_id=?", "observed_at>=?"]
        params: list[Any] = [tenant_id, now - int(window_hours) * 3600]
        if effective_mode is not None:
            clauses.append("effective_mode=?")
            params.append(effective_mode)
        if policy_id is not None:
            clauses.append("policy_id=?")
            params.append(policy_id)
        with self._lock:
            cursor = self._conn.execute(
                "SELECT tenant_id,observed_at,task_type,configured_mode,effective_mode,"
                "cohort_selected,policy_id,decision,audit_status,executed,released,"
                "would_intervene,would_repair,would_block "
                "FROM claim_verification_observations WHERE " + " AND ".join(clauses),
                params,
            )
            names = [str(item[0]) for item in cursor.description]
            rows = [dict(zip(names, values, strict=True)) for values in cursor.fetchall()]
        return _summary(
            rows,
            tenant_id=tenant_id,
            window_hours=window_hours,
            now=now,
            operational_min_samples=operational_min_samples,
            operational_max_error_rate=operational_max_error_rate,
            effective_mode_filter=effective_mode,
            policy_id_filter=policy_id,
        )

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass
