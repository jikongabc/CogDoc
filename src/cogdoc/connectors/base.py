from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol, runtime_checkable

from cogdoc.source_model import SourceDocument, SourceKind


def _required(value: object, name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{name} is required")
    return text


@dataclass(frozen=True)
class ConnectorSourceRef:
    external_id: str
    display_name: str
    media_type: str | None = None
    origin_uri: str | None = None
    etag: str | None = None
    modified_at: str | None = None
    content_sha256: str | None = None
    byte_size: int | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "external_id", _required(self.external_id, "external_id")
        )
        object.__setattr__(
            self, "display_name", _required(self.display_name, "display_name")
        )
        if self.byte_size is not None and (
            isinstance(self.byte_size, bool) or self.byte_size < 0
        ):
            raise ValueError("byte_size must be non-negative")


@dataclass(frozen=True)
class ConnectorPage:
    items: tuple[ConnectorSourceRef, ...] = ()
    deleted_external_ids: tuple[str, ...] = ()
    next_cursor: str | None = None
    complete: bool = False
    snapshot: bool = False

    def __post_init__(self) -> None:
        if not self.complete and not str(self.next_cursor or "").strip():
            raise ValueError("an incomplete connector page requires next_cursor")
        deleted = tuple(
            _required(value, "deleted_external_id")
            for value in self.deleted_external_ids
        )
        if len(set(deleted)) != len(deleted):
            raise ValueError("deleted_external_ids must be unique")
        object.__setattr__(self, "deleted_external_ids", deleted)
        if self.next_cursor is not None:
            object.__setattr__(
                self, "next_cursor", str(self.next_cursor).strip() or None
            )


@dataclass(frozen=True)
class FetchedSource:
    ref: ConnectorSourceRef
    content: bytes
    kind: SourceKind = SourceKind.FILE
    acl: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.content, bytes):
            raise TypeError("connector content must be bytes")

    def document(self, connector_type: str, *, content_sha256: str) -> SourceDocument:
        return SourceDocument.create(
            connector_type=connector_type,
            external_id=self.ref.external_id,
            display_name=self.ref.display_name,
            content_sha256=content_sha256,
            media_type=self.ref.media_type,
            kind=self.kind,
            byte_size=len(self.content),
            origin_uri=self.ref.origin_uri,
            etag=self.ref.etag,
            modified_at=self.ref.modified_at,
            metadata=dict(self.ref.metadata),
        )


@runtime_checkable
class SourceConnector(Protocol):
    connector_type: str

    def list_page(self, cursor: str | None, *, limit: int) -> ConnectorPage: ...

    def fetch(self, ref: ConnectorSourceRef) -> FetchedSource: ...


@runtime_checkable
class SyncSink(Protocol):
    """Idempotent, job-scoped staging sink.

    `commit` is the only operation allowed to make a snapshot visible. Runtime
    retries may call begin/upsert/delete repeatedly with the same job and item.
    """

    def begin(
        self,
        *,
        job_id: str,
        tenant_id: str,
        kb_id: str,
        connection_id: str,
        connector_type: str,
        attempt: int,
    ) -> None: ...

    def upsert(
        self,
        document: SourceDocument,
        content: bytes,
        *,
        acl: Mapping[str, Any] | None = None,
    ) -> None: ...

    def delete(self, external_id: str) -> None: ...

    def commit(
        self,
        *,
        snapshot: bool,
        seen_external_ids: frozenset[str],
        heartbeat: Callable[[], None],
    ) -> None: ...

    def recover_commit(self, *, heartbeat: Callable[[], None]) -> None:
        """Idempotently finish a durable sink journal after worker loss."""
        ...

    def finalize(self) -> None:
        """Discard the commit journal after the durable job terminal exists."""
        ...

    def abort(self) -> None: ...


class ConnectorError(RuntimeError):
    retryable = False


class RetryableConnectorError(ConnectorError):
    retryable = True


class SyncCancelled(ConnectorError):
    pass


class SyncBudgetExceeded(ConnectorError):
    pass


class StaleSyncLease(ConnectorError):
    pass
