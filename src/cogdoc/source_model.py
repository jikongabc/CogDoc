from __future__ import annotations

import hashlib
import json
import mimetypes
import math
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit


SOURCE_CONTRACT_VERSION = "source-document-v1"
LEGACY_CONNECTOR_TYPE = "legacy-upload"


class SourceKind(str, Enum):
    FILE = "file"
    WEB = "web"
    RECORD = "record"
    IMAGE = "image"


def _required_text(value: object, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field_name} must be a non-empty string")
    return text


def _optional_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _json_object(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    # Round-tripping rejects unserialisable connector objects at the boundary
    # and returns a detached value that callers cannot mutate underneath us.
    encoded = json.dumps(dict(value), ensure_ascii=False, sort_keys=True)
    decoded = json.loads(encoded)
    if not isinstance(decoded, dict):  # pragma: no cover - defensive invariant
        raise ValueError("metadata must encode to a JSON object")
    return decoded


def canonical_origin_uri(value: str | None) -> str | None:
    """Return a stable URI without credentials or fragments.

    Connector secrets must never become provenance, logs, manifests, or API
    payloads. User info, query strings, and fragments are therefore removed;
    provider-specific stable IDs belong in ``external_id`` instead.
    """

    uri = _optional_text(value)
    if uri is None:
        return None
    parts = urlsplit(uri)
    if not parts.scheme:
        return uri
    host = parts.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    if parts.port is not None:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme.lower(), host, parts.path, "", ""))


def build_source_id(connector_type: str, external_id: str) -> str:
    connector = _required_text(connector_type, "connector_type").casefold()
    external = _required_text(external_id, "external_id")
    digest = hashlib.sha256(
        b"cogdoc-source-id-v1\0"
        + connector.encode("utf-8")
        + b"\0"
        + external.encode("utf-8")
    ).hexdigest()
    return f"src-{digest}"


def build_version_id(source_id: str, content_sha256: str) -> str:
    source = _required_text(source_id, "source_id")
    content_hash = _required_text(content_sha256, "content_sha256").lower()
    digest = hashlib.sha256(
        b"cogdoc-source-version-v1\0"
        + source.encode("utf-8")
        + b"\0"
        + content_hash.encode("ascii")
    ).hexdigest()
    return f"sv-{digest}"


def _legacy_content_sha256(value: object) -> str:
    """Normalize pre-contract opaque fingerprints without weakening new writes."""

    fingerprint = _required_text(value, "sha256").lower()
    if len(fingerprint) == 64 and all(c in "0123456789abcdef" for c in fingerprint):
        return fingerprint
    return hashlib.sha256(
        b"cogdoc-legacy-content-fingerprint-v1\0" + fingerprint.encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True)
class SourceLocation:
    """Format-neutral position used by chunks and public citations.

    All numeric source coordinates are one-based. Text offsets remain separate
    chunk-local, zero-based offsets in the existing evidence ledger.
    """

    page_start: int | None = None
    page_end: int | None = None
    line_start: int | None = None
    line_end: int | None = None
    slide: int | None = None
    image: int | None = None
    sheet: str | None = None
    cell_range: str | None = None
    section_path: tuple[str, ...] = ()
    anchor: str | None = None
    bbox: tuple[float, float, float, float] | None = None

    def __post_init__(self) -> None:
        for name in (
            "page_start",
            "page_end",
            "line_start",
            "line_end",
            "slide",
            "image",
        ):
            value = getattr(self, name)
            if value is not None and (isinstance(value, bool) or value < 1):
                raise ValueError(f"{name} must be a positive integer")
        if self.page_start and self.page_end and self.page_end < self.page_start:
            raise ValueError("page_end must be greater than or equal to page_start")
        if self.line_start and self.line_end and self.line_end < self.line_start:
            raise ValueError("line_end must be greater than or equal to line_start")
        if self.cell_range and not self.sheet:
            raise ValueError("cell_range requires sheet")
        if self.bbox is not None:
            if len(self.bbox) != 4:
                raise ValueError("bbox must contain four coordinates")
            if any(not math.isfinite(float(value)) for value in self.bbox):
                raise ValueError("bbox coordinates must be finite")

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        for name in (
            "page_start",
            "page_end",
            "line_start",
            "line_end",
            "slide",
            "image",
            "sheet",
            "cell_range",
            "anchor",
        ):
            value = getattr(self, name)
            if value is not None:
                payload[name] = value
        if self.section_path:
            payload["section_path"] = list(self.section_path)
        if self.bbox is not None:
            payload["bbox"] = list(self.bbox)
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any] | None) -> SourceLocation:
        row = payload or {}
        raw_section_path = row.get("section_path") or (
            [row["section"]] if row.get("section") else []
        )
        if not isinstance(raw_section_path, (list, tuple)):
            raise ValueError("section_path must be a list")
        raw_bbox = row.get("bbox")
        if raw_bbox is not None and (
            not isinstance(raw_bbox, (list, tuple)) or len(raw_bbox) != 4
        ):
            raise ValueError("bbox must contain four coordinates")
        bbox: tuple[float, float, float, float] | None = None
        if raw_bbox is not None:
            bbox = (
                float(raw_bbox[0]),
                float(raw_bbox[1]),
                float(raw_bbox[2]),
                float(raw_bbox[3]),
            )
        return cls(
            page_start=row.get("page_start"),
            page_end=row.get("page_end"),
            line_start=row.get("line_start"),
            line_end=row.get("line_end"),
            slide=row.get("slide"),
            image=row.get("image"),
            sheet=_optional_text(row.get("sheet")),
            cell_range=_optional_text(row.get("cell_range")),
            section_path=tuple(
                str(item).strip() for item in raw_section_path if str(item).strip()
            ),
            anchor=_optional_text(row.get("anchor")),
            bbox=bbox,
        )


@dataclass(frozen=True)
class SourceVersion:
    source_id: str
    content_sha256: str
    version_id: str = ""
    byte_size: int | None = None
    etag: str | None = None
    modified_at: str | None = None
    fetched_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        source_id = _required_text(self.source_id, "source_id")
        content_hash = _required_text(self.content_sha256, "content_sha256").lower()
        if len(content_hash) != 64 or any(
            c not in "0123456789abcdef" for c in content_hash
        ):
            raise ValueError("content_sha256 must be a 64-character hex digest")
        if self.byte_size is not None and (
            isinstance(self.byte_size, bool) or self.byte_size < 0
        ):
            raise ValueError("byte_size must be non-negative")
        if not isinstance(self.fetched_at, (int, float)) or self.fetched_at < 0:
            raise ValueError("fetched_at must be non-negative")
        expected = build_version_id(source_id, content_hash)
        if self.version_id and self.version_id != expected:
            raise ValueError("version_id does not match source/content identity")
        object.__setattr__(self, "source_id", source_id)
        object.__setattr__(self, "content_sha256", content_hash)
        object.__setattr__(self, "version_id", expected)
        object.__setattr__(self, "etag", _optional_text(self.etag))
        object.__setattr__(self, "modified_at", _optional_text(self.modified_at))

    def to_dict(self) -> dict[str, Any]:
        return {
            "version_id": self.version_id,
            "content_sha256": self.content_sha256,
            "byte_size": self.byte_size,
            "etag": self.etag,
            "modified_at": self.modified_at,
            "fetched_at": float(self.fetched_at),
        }


@dataclass(frozen=True)
class SourceDocument:
    source_id: str
    connector_type: str
    external_id: str
    display_name: str
    media_type: str
    kind: SourceKind
    version: SourceVersion
    origin_uri: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        connector = _required_text(self.connector_type, "connector_type").casefold()
        external = _required_text(self.external_id, "external_id")
        expected_source_id = build_source_id(connector, external)
        if self.source_id != expected_source_id:
            raise ValueError("source_id does not match connector/external identity")
        if self.version.source_id != self.source_id:
            raise ValueError("version belongs to another source")
        try:
            kind = SourceKind(self.kind)
        except (TypeError, ValueError) as exc:
            raise ValueError("unsupported source kind") from exc
        object.__setattr__(self, "connector_type", connector)
        object.__setattr__(self, "external_id", external)
        object.__setattr__(
            self, "display_name", _required_text(self.display_name, "display_name")
        )
        object.__setattr__(
            self, "media_type", _required_text(self.media_type, "media_type").lower()
        )
        object.__setattr__(self, "origin_uri", canonical_origin_uri(self.origin_uri))
        object.__setattr__(self, "metadata", _json_object(self.metadata))
        object.__setattr__(self, "kind", kind)

    @classmethod
    def create(
        cls,
        *,
        connector_type: str,
        external_id: str,
        display_name: str,
        content_sha256: str,
        media_type: str | None = None,
        kind: SourceKind = SourceKind.FILE,
        byte_size: int | None = None,
        origin_uri: str | None = None,
        etag: str | None = None,
        modified_at: str | None = None,
        fetched_at: float | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> SourceDocument:
        source_id = build_source_id(connector_type, external_id)
        guessed_type = (
            mimetypes.guess_type(display_name)[0] or "application/octet-stream"
        )
        version = SourceVersion(
            source_id=source_id,
            content_sha256=content_sha256,
            byte_size=byte_size,
            etag=etag,
            modified_at=modified_at,
            fetched_at=time.time() if fetched_at is None else fetched_at,
        )
        return cls(
            source_id=source_id,
            connector_type=connector_type,
            external_id=external_id,
            display_name=display_name,
            media_type=media_type or guessed_type,
            kind=kind,
            version=version,
            origin_uri=origin_uri,
            metadata=dict(metadata or {}),
        )

    @classmethod
    def from_manifest_document(cls, payload: Mapping[str, Any]) -> SourceDocument:
        name = _required_text(
            payload.get("name") or payload.get("display_name"), "name"
        )
        connector = str(payload.get("connector_type") or LEGACY_CONNECTOR_TYPE)
        external_id = str(payload.get("external_id") or name)
        content_hash = _legacy_content_sha256(
            payload.get("content_sha256") or payload.get("sha256")
        )
        return cls.create(
            connector_type=connector,
            external_id=external_id,
            display_name=name,
            content_sha256=content_hash,
            media_type=str(payload.get("media_type") or "") or None,
            kind=SourceKind(str(payload.get("kind") or SourceKind.FILE.value)),
            byte_size=payload.get("size")
            if payload.get("size") is not None
            else payload.get("byte_size"),
            origin_uri=payload.get("origin_uri"),
            etag=payload.get("etag"),
            modified_at=payload.get("modified_at"),
            fetched_at=float(payload.get("fetched_at") or 0.0),
            metadata=payload.get("metadata")
            if isinstance(payload.get("metadata"), Mapping)
            else {},
        )

    def to_manifest_document(self) -> dict[str, Any]:
        # name/size/sha256 remain the compatibility projection consumed by the
        # v7 incremental index and older deployments.
        return {
            "name": self.display_name,
            "size": self.version.byte_size,
            "sha256": self.version.content_sha256,
            "source_id": self.source_id,
            "version_id": self.version.version_id,
            "connector_type": self.connector_type,
            "external_id": self.external_id,
            "media_type": self.media_type,
            "kind": self.kind.value,
            "origin_uri": self.origin_uri,
            "etag": self.version.etag,
            "modified_at": self.version.modified_at,
            "fetched_at": self.version.fetched_at,
            "metadata": _json_object(self.metadata),
        }


def normalize_manifest_document(payload: Mapping[str, Any]) -> dict[str, Any]:
    normalized = SourceDocument.from_manifest_document(payload).to_manifest_document()
    # Keep the scanner's compatibility fingerprint byte-for-byte. The generic
    # version identity above is canonical even when a pre-contract test or old
    # deployment used an opaque fingerprint instead of a real SHA-256.
    if payload.get("sha256") is not None:
        normalized["sha256"] = str(payload["sha256"])
    return normalized


def stamp_source_contract(manifest: Mapping[str, Any]) -> dict[str, Any]:
    normalized = dict(manifest)
    documents = manifest.get("documents") or []
    if not isinstance(documents, list):
        raise ValueError("manifest documents must be a list")
    normalized["documents"] = [normalize_manifest_document(row) for row in documents]
    normalized["source_contract_version"] = SOURCE_CONTRACT_VERSION
    return normalized
