from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal, Mapping, Protocol


SyncEventKind = Literal[
    "started",
    "progress",
    "retry",
    "succeeded",
    "failed",
    "dead_letter",
    "cancelled",
]
SYNC_EVENT_RANK: dict[SyncEventKind, int] = {
    "started": 10,
    "progress": 11,
    "retry": 20,
    "cancelled": 30,
    "failed": 31,
    "dead_letter": 32,
    "succeeded": 33,
}


@dataclass(frozen=True)
class SyncObservation:
    """A secret-free, point-in-time synchronization observation."""

    kind: SyncEventKind
    job_id: str
    tenant_id: str
    kb_id: str
    connection_id: str
    connector_type: str
    job_sequence: int
    attempt: int
    duration_seconds: float
    backlog: int
    counters: Mapping[str, int] = field(default_factory=dict)
    error_code: str | None = None
    retry_at: float | None = None

    def __post_init__(self) -> None:
        if self.duration_seconds < 0 or self.backlog < 0:
            raise ValueError("observation duration and backlog must be non-negative")
        if type(self.job_sequence) is not int or self.job_sequence <= 0:
            raise ValueError("observation job_sequence must be a positive integer")
        object.__setattr__(self, "counters", MappingProxyType(dict(self.counters)))

    @property
    def event_rank(self) -> int:
        return SYNC_EVENT_RANK[self.kind]

    @property
    def event_sequence(self) -> int:
        return self.attempt * 100 + self.event_rank


class SyncObserver(Protocol):
    """Receives lifecycle observations without participating in job authority."""

    def started(self, observation: SyncObservation) -> None: ...

    def progress(self, observation: SyncObservation) -> None: ...

    def retry(self, observation: SyncObservation) -> None: ...

    def succeeded(self, observation: SyncObservation) -> None: ...

    def failed(self, observation: SyncObservation) -> None: ...

    def dead_letter(self, observation: SyncObservation) -> None: ...

    def cancelled(self, observation: SyncObservation) -> None: ...

    def reconcile(self, observation: SyncObservation) -> None: ...


class NoOpSyncObserver:
    """Default observer. Deliberately has no persistence or I/O side effects."""

    def started(self, observation: SyncObservation) -> None:
        del observation

    def progress(self, observation: SyncObservation) -> None:
        del observation

    def retry(self, observation: SyncObservation) -> None:
        del observation

    def succeeded(self, observation: SyncObservation) -> None:
        del observation

    def failed(self, observation: SyncObservation) -> None:
        del observation

    def dead_letter(self, observation: SyncObservation) -> None:
        del observation

    def cancelled(self, observation: SyncObservation) -> None:
        del observation

    def reconcile(self, observation: SyncObservation) -> None:
        del observation


NO_OP_SYNC_OBSERVER = NoOpSyncObserver()
