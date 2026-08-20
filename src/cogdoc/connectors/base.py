from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol, runtime_checkable

from cogdoc.source_model import SourceDocument, SourceKind


MAX_CONNECTOR_EXTERNAL_ID_BYTES = 4 * 1024
MAX_CONNECTOR_DISPLAY_NAME_BYTES = 2 * 1024
MAX_CONNECTOR_URI_BYTES = 8 * 1024
MAX_CONNECTOR_CURSOR_BYTES = 16 * 1024
MAX_CONNECTOR_METADATA_BYTES = 64 * 1024
MAX_CONNECTOR_ACL_BYTES = 256 * 1024
MAX_CONNECTOR_ACL_GRANTS = 4_096
MAX_CONNECTOR_PAGE_ITEMS = 10_000

_MEDIA_TOKEN = re.compile(r"^[A-Za-z0-9!#$&^_.+-]+$")


def _bounded_text(
    value: object,
    name: str,
    *,
    max_bytes: int,
    required: bool,
) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    text = value.strip()
    if not text:
        if not required:
            return None
        raise ValueError(f"{name} is required")
    if len(text.encode("utf-8")) > max_bytes:
        raise ValueError(f"{name} exceeds the byte limit")
    if any(ord(character) < 32 or ord(character) == 127 for character in text):
        raise ValueError(f"{name} must not contain control characters")
    return text


def _required(value: object, name: str) -> str:
    result = _bounded_text(
        value,
        name,
        max_bytes=MAX_CONNECTOR_EXTERNAL_ID_BYTES,
        required=True,
    )
    assert result is not None
    return result


def _optional(value: object, name: str, *, max_bytes: int) -> str | None:
    return _bounded_text(value, name, max_bytes=max_bytes, required=False)


def _media_type(value: object) -> str | None:
    media_type = _optional(value, "media_type", max_bytes=255)
    if media_type is None:
        return None
    essence = media_type.split(";", 1)[0].strip()
    parts = essence.split("/")
    if len(parts) != 2 or not all(_MEDIA_TOKEN.fullmatch(part) for part in parts):
        raise ValueError("media_type is invalid")
    return media_type


def _json_mapping(value: object, name: str, *, max_bytes: int) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{name} must be a mapping")
    if any(not isinstance(key, str) for key in value):
        raise ValueError(f"{name} keys must be strings")
    try:
        encoded = json.dumps(
            dict(value),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (RecursionError, TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a finite JSON object") from exc
    if len(encoded) > max_bytes:
        raise ValueError(f"{name} exceeds the byte limit")
    decoded = json.loads(encoded)
    if not isinstance(decoded, dict):  # pragma: no cover - defensive invariant
        raise ValueError(f"{name} must encode to a JSON object")
    return decoded


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
            self,
            "display_name",
            _bounded_text(
                self.display_name,
                "display_name",
                max_bytes=MAX_CONNECTOR_DISPLAY_NAME_BYTES,
                required=True,
            ),
        )
        object.__setattr__(self, "media_type", _media_type(self.media_type))
        object.__setattr__(
            self,
            "origin_uri",
            _optional(self.origin_uri, "origin_uri", max_bytes=MAX_CONNECTOR_URI_BYTES),
        )
        object.__setattr__(
            self, "etag", _optional(self.etag, "etag", max_bytes=2 * 1024)
        )
        object.__setattr__(
            self,
            "modified_at",
            _optional(self.modified_at, "modified_at", max_bytes=256),
        )
        content_sha256 = _optional(self.content_sha256, "content_sha256", max_bytes=64)
        if content_sha256 is not None:
            content_sha256 = content_sha256.casefold()
            if len(content_sha256) != 64 or any(
                character not in "0123456789abcdef" for character in content_sha256
            ):
                raise ValueError("content_sha256 must be a 64-character hex digest")
        object.__setattr__(self, "content_sha256", content_sha256)
        if self.byte_size is not None:
            if (
                not isinstance(self.byte_size, int)
                or isinstance(self.byte_size, bool)
                or self.byte_size < 0
            ):
                raise ValueError("byte_size must be a non-negative integer")
        object.__setattr__(
            self,
            "metadata",
            _json_mapping(
                self.metadata, "metadata", max_bytes=MAX_CONNECTOR_METADATA_BYTES
            ),
        )


@dataclass(frozen=True)
class ConnectorPage:
    items: tuple[ConnectorSourceRef, ...] = ()
    deleted_external_ids: tuple[str, ...] = ()
    next_cursor: str | None = None
    complete: bool = False
    snapshot: bool = False

    def __post_init__(self) -> None:
        if type(self.complete) is not bool or type(self.snapshot) is not bool:
            raise TypeError("connector page flags must be booleans")
        items = tuple(self.items)
        if len(items) > MAX_CONNECTOR_PAGE_ITEMS:
            raise ValueError("connector page exceeds the hard item limit")
        if any(not isinstance(item, ConnectorSourceRef) for item in items):
            raise TypeError("connector page items must be source references")
        object.__setattr__(self, "items", items)
        cursor = _optional(
            self.next_cursor, "next_cursor", max_bytes=MAX_CONNECTOR_CURSOR_BYTES
        )
        if not self.complete and cursor is None:
            raise ValueError("an incomplete connector page requires next_cursor")
        deleted = tuple(
            _required(value, "deleted_external_id")
            for value in self.deleted_external_ids
        )
        if len(deleted) > MAX_CONNECTOR_PAGE_ITEMS:
            raise ValueError("connector page exceeds the hard deletion limit")
        if len(set(deleted)) != len(deleted):
            raise ValueError("deleted_external_ids must be unique")
        object.__setattr__(self, "deleted_external_ids", deleted)
        object.__setattr__(self, "next_cursor", cursor)


@dataclass(frozen=True)
class FetchedSource:
    ref: ConnectorSourceRef
    content: bytes
    kind: SourceKind = SourceKind.FILE
    acl: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.content, bytes):
            raise TypeError("connector content must be bytes")
        if not isinstance(self.ref, ConnectorSourceRef):
            raise TypeError("fetched source ref must be a connector source reference")
        if not isinstance(self.kind, SourceKind):
            raise TypeError("fetched source kind must be a SourceKind")
        if self.acl is not None:
            acl = _json_mapping(self.acl, "acl", max_bytes=MAX_CONNECTOR_ACL_BYTES)
            grants = acl.get("grants", [])
            if not isinstance(grants, list):
                raise ValueError("acl grants must be a list")
            if len(grants) > MAX_CONNECTOR_ACL_GRANTS:
                raise ValueError("acl grant count exceeds the limit")
            if any(not isinstance(grant, Mapping) for grant in grants):
                raise ValueError("acl grants must contain JSON objects")
            object.__setattr__(self, "acl", acl)

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
        recovering_commit: bool = False,
    ) -> None: ...

    def upsert(
        self,
        document: SourceDocument,
        content: bytes,
        *,
        acl: Mapping[str, Any] | None = None,
    ) -> None: ...

    def delete(self, external_id: str) -> None: ...

    def prepare_commit(
        self,
        *,
        snapshot: bool,
        seen_external_ids: frozenset[str],
    ) -> None:
        """Durably finalize staging before the job crosses into committing."""
        ...

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
