from __future__ import annotations

import math
import secrets
import time
import uuid
from collections.abc import Mapping
from typing import Any, Callable, Final

from cogdoc.ha.storage import DatabaseBackend, execute_script


DISPATCH_QUEUED: Final = "queued"
DISPATCH_RUNNING: Final = "running"
DISPATCH_SUCCEEDED: Final = "succeeded"
DISPATCH_CANCELLED: Final = "cancelled"
DISPATCH_TERMINAL = frozenset({DISPATCH_SUCCEEDED, DISPATCH_CANCELLED})
_PHASES = frozenset({"evidence", "report"})


class StaleResearchDispatch(RuntimeError):
    """A worker tried to use an expired or superseded dispatch lease."""


def _clean(value: str, field: str, maximum: int = 255) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or len(value) > maximum
        or any(ord(character) < 33 or ord(character) == 127 for character in value)
    ):
        raise ValueError(f"{field} is invalid")
    return value


def _column(row: Any, name: str, index: int) -> Any:
    return row.get(name) if isinstance(row, Mapping) else row[index]


class ResearchDispatchStore:
    """Cluster queue for executing an already-durable research attempt.

    The research record remains the result authority. This table only elects
    one node at a time to run that attempt; takeover rotates the lease in the
    research record before any provider or retrieval work begins.
    """

    def __init__(
        self,
        backend: DatabaseBackend,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.backend = backend
        self._clock = clock
        execute_script(
            backend,
            [
                """CREATE TABLE IF NOT EXISTS ha_research_dispatches (
                dispatch_id TEXT PRIMARY KEY,research_job_id TEXT NOT NULL,
                phase TEXT NOT NULL,attempt_id TEXT NOT NULL,status TEXT NOT NULL,
                lease_owner TEXT,lease_token TEXT,lease_expires_at REAL,
                created_at REAL NOT NULL,updated_at REAL NOT NULL,finished_at REAL,
                revision BIGINT NOT NULL DEFAULT 1,
                UNIQUE(research_job_id,phase,attempt_id))""",
                "CREATE INDEX IF NOT EXISTS idx_ha_research_dispatch_claim ON ha_research_dispatches(status,lease_expires_at,created_at,dispatch_id)",
                "CREATE INDEX IF NOT EXISTS idx_ha_research_dispatch_job ON ha_research_dispatches(research_job_id,phase,created_at DESC)",
            ],
        )

    @staticmethod
    def _row(row: Any | None) -> dict[str, Any] | None:
        return None if row is None else dict(row)

    @staticmethod
    def _phase(value: str) -> str:
        if value not in _PHASES:
            raise ValueError("research dispatch phase is invalid")
        return value

    def enqueue(
        self,
        research_job_id: str,
        phase: str,
        attempt_id: str,
    ) -> dict[str, Any]:
        research_job_id = _clean(research_job_id, "research_job_id")
        attempt_id = _clean(attempt_id, "attempt_id")
        phase = self._phase(phase)
        now = self._clock()
        dispatch_id = f"hrd-{uuid.uuid4().hex}"
        marker = self.backend.sql(sqlite="?", postgres="%s")
        placeholders = self.backend.sql(
            sqlite="?,?,?,?,?,?,?", postgres="%s,%s,%s,%s,%s,%s,%s"
        )
        insert = self.backend.sql(
            sqlite="INSERT OR IGNORE",
            postgres="INSERT",
        )
        suffix = self.backend.sql(
            sqlite="",
            postgres=(
                " ON CONFLICT(research_job_id,phase,attempt_id) DO NOTHING"
            ),
        )
        with self.backend.transaction(write=True) as connection:
            connection.execute(
                f"{insert} INTO ha_research_dispatches("
                "dispatch_id,research_job_id,phase,attempt_id,status,created_at,updated_at) "
                f"VALUES({placeholders}){suffix}",
                (
                    dispatch_id,
                    research_job_id,
                    phase,
                    attempt_id,
                    DISPATCH_QUEUED,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM ha_research_dispatches "
                f"WHERE research_job_id={marker} AND phase={marker} "
                f"AND attempt_id={marker}",
                (research_job_id, phase, attempt_id),
            ).fetchone()
            if row is not None and str(_column(row, "status", 4)) in DISPATCH_TERMINAL:
                connection.execute(
                    "UPDATE ha_research_dispatches SET status='queued',"
                    "lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,"
                    "finished_at=NULL,updated_at="
                    f"{marker},revision=revision+1 WHERE dispatch_id={marker}",
                    (now, str(_column(row, "dispatch_id", 0))),
                )
                row = connection.execute(
                    f"SELECT * FROM ha_research_dispatches WHERE dispatch_id={marker}",
                    (str(_column(row, "dispatch_id", 0)),),
                ).fetchone()
        result = self._row(row)
        if result is None:  # pragma: no cover - transaction invariant
            raise RuntimeError("research dispatch disappeared after enqueue")
        return result

    def claim(
        self,
        worker_id: str,
        *,
        lease_seconds: float,
    ) -> dict[str, Any] | None:
        worker_id = _clean(worker_id, "worker_id")
        if not math.isfinite(lease_seconds) or not 5 <= lease_seconds <= 3600:
            raise ValueError("research dispatch lease_seconds is invalid")
        now = self._clock()
        token = secrets.token_urlsafe(32)
        marker = self.backend.sql(sqlite="?", postgres="%s")
        with self.backend.transaction(write=True) as connection:
            if self.backend.kind == "postgres":
                row = connection.execute(
                    "WITH candidate AS (SELECT dispatch_id FROM ha_research_dispatches "
                    "WHERE status='queued' OR (status='running' AND lease_expires_at<=%s) "
                    "ORDER BY created_at,dispatch_id FOR UPDATE SKIP LOCKED LIMIT 1) "
                    "UPDATE ha_research_dispatches AS dispatches SET status='running',"
                    "lease_owner=%s,lease_token=%s,lease_expires_at=%s,updated_at=%s,"
                    "revision=revision+1 FROM candidate "
                    "WHERE dispatches.dispatch_id=candidate.dispatch_id "
                    "RETURNING dispatches.*",
                    (now, worker_id, token, now + lease_seconds, now),
                ).fetchone()
            else:
                candidate = connection.execute(
                    "SELECT dispatch_id FROM ha_research_dispatches "
                    "WHERE status='queued' OR (status='running' AND lease_expires_at<=?) "
                    "ORDER BY created_at,dispatch_id LIMIT 1",
                    (now,),
                ).fetchone()
                if candidate is None:
                    return None
                dispatch_id = str(_column(candidate, "dispatch_id", 0))
                changed = connection.execute(
                    "UPDATE ha_research_dispatches SET status='running',lease_owner=?,"
                    "lease_token=?,lease_expires_at=?,updated_at=?,revision=revision+1 "
                    "WHERE dispatch_id=? AND "
                    "(status='queued' OR (status='running' AND lease_expires_at<=?))",
                    (
                        worker_id,
                        token,
                        now + lease_seconds,
                        now,
                        dispatch_id,
                        now,
                    ),
                )
                if changed.rowcount != 1:
                    return None
                row = connection.execute(
                    f"SELECT * FROM ha_research_dispatches WHERE dispatch_id={marker}",
                    (dispatch_id,),
                ).fetchone()
        return self._row(row)

    def heartbeat(
        self,
        dispatch_id: str,
        worker_id: str,
        lease_token: str,
        *,
        lease_seconds: float,
    ) -> None:
        dispatch_id = _clean(dispatch_id, "dispatch_id")
        worker_id = _clean(worker_id, "worker_id")
        lease_token = _clean(lease_token, "lease_token", 512)
        if not math.isfinite(lease_seconds) or not 5 <= lease_seconds <= 3600:
            raise ValueError("research dispatch lease_seconds is invalid")
        now = self._clock()
        marker = self.backend.sql(sqlite="?", postgres="%s")
        with self.backend.transaction(write=True) as connection:
            changed = connection.execute(
                "UPDATE ha_research_dispatches SET lease_expires_at="
                f"{marker},updated_at={marker},revision=revision+1 "
                f"WHERE dispatch_id={marker} AND status='running' "
                f"AND lease_owner={marker} AND lease_token={marker} "
                f"AND lease_expires_at>{marker}",
                (
                    now + lease_seconds,
                    now,
                    dispatch_id,
                    worker_id,
                    lease_token,
                    now,
                ),
            )
        if changed.rowcount != 1:
            raise StaleResearchDispatch("research dispatch lease is stale")

    def finish(
        self,
        dispatch_id: str,
        worker_id: str,
        lease_token: str,
        *,
        status: str,
    ) -> None:
        dispatch_id = _clean(dispatch_id, "dispatch_id")
        worker_id = _clean(worker_id, "worker_id")
        lease_token = _clean(lease_token, "lease_token", 512)
        if status not in DISPATCH_TERMINAL:
            raise ValueError("research dispatch terminal status is invalid")
        now = self._clock()
        marker = self.backend.sql(sqlite="?", postgres="%s")
        with self.backend.transaction(write=True) as connection:
            changed = connection.execute(
                "UPDATE ha_research_dispatches SET status="
                f"{marker},lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,"
                f"finished_at={marker},updated_at={marker},revision=revision+1 "
                f"WHERE dispatch_id={marker} AND status='running' "
                f"AND lease_owner={marker} AND lease_token={marker}",
                (status, now, now, dispatch_id, worker_id, lease_token),
            )
        if changed.rowcount != 1:
            raise StaleResearchDispatch("research dispatch lease is stale")

    def requeue(
        self,
        dispatch_id: str,
        worker_id: str,
        lease_token: str,
    ) -> None:
        dispatch_id = _clean(dispatch_id, "dispatch_id")
        worker_id = _clean(worker_id, "worker_id")
        lease_token = _clean(lease_token, "lease_token", 512)
        now = self._clock()
        marker = self.backend.sql(sqlite="?", postgres="%s")
        with self.backend.transaction(write=True) as connection:
            changed = connection.execute(
                "UPDATE ha_research_dispatches SET status='queued',"
                "lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,"
                f"updated_at={marker},revision=revision+1 "
                f"WHERE dispatch_id={marker} AND status='running' "
                f"AND lease_owner={marker} AND lease_token={marker}",
                (now, dispatch_id, worker_id, lease_token),
            )
        if changed.rowcount != 1:
            raise StaleResearchDispatch("research dispatch lease is stale")

    def cancel_job(self, research_job_id: str) -> int:
        research_job_id = _clean(research_job_id, "research_job_id")
        now = self._clock()
        marker = self.backend.sql(sqlite="?", postgres="%s")
        with self.backend.transaction(write=True) as connection:
            changed = connection.execute(
                "UPDATE ha_research_dispatches SET status='cancelled',"
                "lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,"
                f"finished_at={marker},updated_at={marker},revision=revision+1 "
                f"WHERE research_job_id={marker} AND status IN ('queued','running')",
                (now, now, research_job_id),
            )
        return max(0, int(changed.rowcount))

    def release_owned(self, worker_id: str) -> int:
        """Make this node's unfinished work immediately claimable on shutdown."""

        worker_id = _clean(worker_id, "worker_id")
        now = self._clock()
        marker = self.backend.sql(sqlite="?", postgres="%s")
        with self.backend.transaction(write=True) as connection:
            changed = connection.execute(
                "UPDATE ha_research_dispatches SET status='queued',lease_owner=NULL,"
                "lease_token=NULL,lease_expires_at=NULL,updated_at="
                f"{marker},revision=revision+1 WHERE status='running' "
                f"AND lease_owner={marker}",
                (now, worker_id),
            )
        return max(0, int(changed.rowcount))

    def get(self, dispatch_id: str) -> dict[str, Any] | None:
        dispatch_id = _clean(dispatch_id, "dispatch_id")
        marker = self.backend.sql(sqlite="?", postgres="%s")
        with self.backend.transaction() as connection:
            row = connection.execute(
                f"SELECT * FROM ha_research_dispatches WHERE dispatch_id={marker}",
                (dispatch_id,),
            ).fetchone()
        return self._row(row)

    def prune_terminal(self, *, before: float, limit: int = 100) -> int:
        """Delete a bounded oldest-first page of terminal queue envelopes."""

        if not math.isfinite(before) or before < 0:
            raise ValueError("research dispatch prune boundary is invalid")
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("research dispatch prune limit is invalid")
        marker = self.backend.sql(sqlite="?", postgres="%s")
        with self.backend.transaction(write=True) as connection:
            changed = connection.execute(
                "DELETE FROM ha_research_dispatches WHERE dispatch_id IN ("
                "SELECT dispatch_id FROM ha_research_dispatches "
                "WHERE status IN ('succeeded','cancelled') AND finished_at<="
                f"{marker} ORDER BY finished_at,dispatch_id LIMIT {marker})",
                (before, limit),
            )
        return max(0, int(changed.rowcount))

    def check(self) -> bool:
        with self.backend.transaction() as connection:
            connection.execute(
                "SELECT dispatch_id FROM ha_research_dispatches WHERE 1=0"
            )
        return True


__all__ = [
    "DISPATCH_CANCELLED",
    "DISPATCH_QUEUED",
    "DISPATCH_RUNNING",
    "DISPATCH_SUCCEEDED",
    "ResearchDispatchStore",
    "StaleResearchDispatch",
]
