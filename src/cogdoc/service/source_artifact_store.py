from __future__ import annotations

import difflib
import hashlib
import json
import math
import os
import re
import shutil
import stat
import tempfile
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Any, BinaryIO
from uuid import uuid4

from cogdoc.source_model import build_version_id


_SAFE_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}\Z")
_IMMUTABLE_ARTIFACT_FIELDS = ("content_sha256", "byte_size")
_TEXT_MEDIA_TYPES = frozenset(
    {
        "application/csv",
        "application/json",
        "application/ld+json",
        "application/markdown",
        "application/sql",
        "application/toml",
        "application/xml",
        "application/x-ndjson",
        "application/x-yaml",
        "application/yaml",
        "image/svg+xml",
    }
)
_TEXT_SUFFIXES = frozenset(
    {
        ".csv",
        ".htm",
        ".html",
        ".json",
        ".md",
        ".rst",
        ".sql",
        ".toml",
        ".tsv",
        ".txt",
        ".xml",
        ".yaml",
        ".yml",
    }
)


class SourceArtifactError(Exception):
    """Base error for immutable source-artifact operations."""


class ArtifactNotFoundError(SourceArtifactError, FileNotFoundError):
    """The requested scoped source version or recovery token does not exist."""


class ArtifactConflictError(SourceArtifactError):
    """An immutable version already exists with different data or metadata."""


class ArtifactIntegrityError(SourceArtifactError):
    """The expected digest or persisted artifact integrity check failed."""


class ArtifactLimitError(SourceArtifactError):
    """A file, source-version, or physical-store quota would be exceeded."""


@dataclass
class _ReservedArtifact:
    metadata: dict[str, Any]
    encoded_metadata: bytes
    reserved_bytes: int
    consumed: bool = False


@dataclass(frozen=True)
class _ArtifactReservation:
    token: str
    tenant_id: str
    kb_id: str
    reservation_key: str
    fingerprint: str
    entries: dict[tuple[str, str], _ReservedArtifact]


def _required_scope(value: object, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field_name} is required")
    if len(text) > 1_024 or "\x00" in text:
        raise ValueError(f"{field_name} is invalid")
    return text


def _safe_token(value: object, field_name: str) -> str:
    text = str(value or "").strip()
    if not _SAFE_TOKEN.fullmatch(text) or text in {".", ".."}:
        raise ValueError(f"{field_name} is invalid")
    return text


def _sha256(value: object) -> str:
    digest = str(value or "").strip().casefold()
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise ValueError("content_sha256 must be a 64-character hex digest")
    return digest


def _positive_limit(value: int, field_name: str) -> int:
    if isinstance(value, bool) or int(value) < 1:
        raise ValueError(f"{field_name} must be positive")
    return int(value)


class SourceArtifactStore:
    """Tenant-scoped immutable raw source-version storage.

    The root is deliberately independent from connector materialization and
    index-generation directories. Deleting or pruning here therefore cannot
    change the currently served knowledge-base materialization. Deletes move a
    complete artifact directory into the store-local trash and are recoverable
    until an explicit :meth:`purge_trash` call.
    """

    def __init__(
        self,
        root: str | Path,
        *,
        max_file_bytes: int = 64 * 1024 * 1024,
        max_total_bytes: int = 2 * 1024 * 1024 * 1024,
        max_bytes_per_tenant: int | None = None,
        max_versions_per_source: int = 50,
        user_max_versions_per_source: int | None = None,
        max_diff_bytes: int = 256 * 1024,
        max_diff_lines: int = 5_000,
    ) -> None:
        self.root = Path(root).resolve()
        self.max_file_bytes = _positive_limit(max_file_bytes, "max_file_bytes")
        self.max_total_bytes = _positive_limit(max_total_bytes, "max_total_bytes")
        self.max_bytes_per_tenant = _positive_limit(
            (
                self.max_total_bytes
                if max_bytes_per_tenant is None
                else max_bytes_per_tenant
            ),
            "max_bytes_per_tenant",
        )
        self.max_versions_per_source = _positive_limit(
            max_versions_per_source, "max_versions_per_source"
        )
        self.user_max_versions_per_source = _positive_limit(
            (
                self.max_versions_per_source
                if user_max_versions_per_source is None
                else user_max_versions_per_source
            ),
            "user_max_versions_per_source",
        )
        if self.user_max_versions_per_source > self.max_versions_per_source:
            raise ValueError(
                "user_max_versions_per_source cannot exceed max_versions_per_source"
            )
        self.max_diff_bytes = _positive_limit(max_diff_bytes, "max_diff_bytes")
        self.max_diff_lines = _positive_limit(max_diff_lines, "max_diff_lines")
        self._lock = RLock()
        self._reservations: dict[str, _ArtifactReservation] = {}
        self._reservation_tokens_by_key: dict[tuple[str, str, str], str] = {}
        self._reserved_artifact_owners: dict[tuple[str, str, str, str], str] = {}
        self._reserved_physical_usage_bytes = 0
        self._reserved_physical_usage_bytes_by_tenant: dict[str, int] = {}
        self.root.mkdir(parents=True, exist_ok=True)
        if not self.root.is_dir():
            raise ValueError("artifact root must be a directory")
        self._assert_inside_root(self._trash_root)
        self._trash_root.mkdir(exist_ok=True)
        self._cleanup_stale_temporary_directories()
        # One startup reconciliation makes subsequent puts O(1) with respect
        # to the number of already stored documents. Every mutating method
        # updates this counter while holding ``_lock``. Full-tree reconciliation
        # is reserved for startup and exceptional mutation races; readiness is
        # intentionally O(1) in artifact count.
        self._cached_physical_usage_bytes = 0
        self._cached_physical_usage_bytes_by_tenant: dict[str, int] = {}
        self._reconcile_physical_usage_locked()

    @property
    def _trash_root(self) -> Path:
        return self.root / ".trash"

    def check(self) -> bool:
        """Verify active/trash roots are directories that accept real writes."""

        with self._lock:
            for directory in (self.root, self._trash_root):
                if (
                    not directory.is_dir()
                    or directory.is_symlink()
                    or not directory.exists()
                ):
                    raise OSError("artifact store directory is unavailable")
                self._assert_inside_root(directory)
                # Opening the directory proves search/read access. A real
                # zero-byte create+fsync+unlink catches read-only mounts where
                # os.access() can produce misleading results for privileged
                # processes.
                with os.scandir(directory):
                    pass
                descriptor: int | None = None
                probe_path: str | None = None
                try:
                    descriptor, probe_path = tempfile.mkstemp(
                        prefix=".readiness-", dir=directory
                    )
                    os.fsync(descriptor)
                finally:
                    if descriptor is not None:
                        os.close(descriptor)
                    if probe_path is not None:
                        os.unlink(probe_path)
            if (
                self._cached_physical_usage_bytes + self._reserved_physical_usage_bytes
                > self.max_total_bytes
            ):
                raise ArtifactLimitError("artifact store exceeds max_total_bytes")
            for tenant in (
                self._cached_physical_usage_bytes_by_tenant.keys()
                | self._reserved_physical_usage_bytes_by_tenant.keys()
            ):
                self._assert_capacity_locked(tenant)
        return True

    def reserve_batch(
        self,
        tenant_id: str,
        kb_id: str,
        artifacts: Iterable[Mapping[str, Any]],
        *,
        reservation_key: str,
    ) -> str:
        """Reserve one sync batch before its durable authority transition.

        Each artifact mapping uses the same metadata fields as :meth:`put`, with
        an explicit ``byte_size`` and ``created_at``. Repeating the same scoped
        key and canonical batch returns the live token without reserving twice.
        A later ``put(..., reservation_token=token)`` atomically converts the
        matching reservation into physical usage; release is idempotent.
        """

        tenant = _required_scope(tenant_id, "tenant_id")
        knowledge_base = _required_scope(kb_id, "kb_id")
        key = _safe_token(reservation_key, "reservation_key")
        normalized: dict[tuple[str, str], _ReservedArtifact] = {}
        for raw in artifacts:
            if not isinstance(raw, Mapping):
                raise TypeError("reserved artifact must be a mapping")
            if "created_at" not in raw:
                raise ValueError("reserved artifact created_at is required")
            source = _safe_token(raw.get("source_id"), "source_id")
            version = _safe_token(raw.get("version_id"), "version_id")
            metadata = self._artifact_metadata(
                tenant,
                knowledge_base,
                source,
                version,
                content_sha256=raw.get("content_sha256"),
                byte_size=raw.get("byte_size"),
                media_type=raw.get("media_type", "application/octet-stream"),
                display_name=raw.get("display_name"),
                created_at=raw.get("created_at"),
            )
            encoded_metadata = self._encode_metadata(metadata)
            identity = (source, version)
            candidate = _ReservedArtifact(
                metadata=metadata,
                encoded_metadata=encoded_metadata,
                reserved_bytes=0,
            )
            previous = normalized.get(identity)
            if previous is not None:
                if previous.encoded_metadata != encoded_metadata:
                    raise ArtifactConflictError(
                        "reservation contains conflicting source-version metadata"
                    )
                continue
            normalized[identity] = candidate

        fingerprint_payload = b"\0".join(
            normalized[identity].encoded_metadata for identity in sorted(normalized)
        )
        fingerprint = hashlib.sha256(
            b"cogdoc-artifact-reservation-v1\0" + fingerprint_payload
        ).hexdigest()
        reservation_identity = (tenant, knowledge_base, key)
        with self._lock:
            existing_token = self._reservation_tokens_by_key.get(reservation_identity)
            if existing_token is not None:
                existing_reservation = self._reservations[existing_token]
                if existing_reservation.fingerprint != fingerprint:
                    raise ArtifactConflictError(
                        "artifact reservation key already identifies another batch"
                    )
                return existing_reservation.token

            requested_bytes = 0
            requested_versions: dict[str, int] = {}
            entries: dict[tuple[str, str], _ReservedArtifact] = {}
            for identity, candidate in normalized.items():
                source, version = identity
                owner_identity = (tenant, knowledge_base, source, version)
                if owner_identity in self._reserved_artifact_owners:
                    raise ArtifactConflictError(
                        "source version is already reserved by another batch"
                    )
                final = self._version_dir(tenant, knowledge_base, source, version)
                if final.exists():
                    existing_metadata = self._load_metadata(
                        final,
                        tenant,
                        knowledge_base,
                        source,
                        version,
                        verify_content=False,
                    )
                    if any(
                        existing_metadata.get(field) != candidate.metadata.get(field)
                        for field in _IMMUTABLE_ARTIFACT_FIELDS
                    ):
                        raise ArtifactConflictError(
                            "existing source version conflicts with reserved artifact"
                        )
                    entries[identity] = candidate
                    continue

                reserved_bytes = int(candidate.metadata["byte_size"]) + len(
                    candidate.encoded_metadata
                )
                candidate.reserved_bytes = reserved_bytes
                requested_bytes += reserved_bytes
                requested_versions[source] = requested_versions.get(source, 0) + 1
                entries[identity] = candidate

            for source, requested in requested_versions.items():
                active = self._active_version_count_locked(
                    tenant, knowledge_base, source
                )
                pending = self._reserved_version_count_locked(
                    tenant, knowledge_base, source
                )
                if active + pending + requested > self.max_versions_per_source:
                    raise ArtifactLimitError("source exceeds max_versions_per_source")
            self._assert_capacity_locked(tenant, requested_bytes)

            token = f"res-{uuid4().hex}"
            reservation = _ArtifactReservation(
                token=token,
                tenant_id=tenant,
                kb_id=knowledge_base,
                reservation_key=key,
                fingerprint=fingerprint,
                entries=entries,
            )
            self._reservations[token] = reservation
            self._reservation_tokens_by_key[reservation_identity] = token
            for source, version in entries:
                self._reserved_artifact_owners[
                    (tenant, knowledge_base, source, version)
                ] = token
            self._reserved_physical_usage_bytes += requested_bytes
            self._reserved_physical_usage_bytes_by_tenant[tenant] = (
                self._reserved_physical_usage_bytes_by_tenant.get(tenant, 0)
                + requested_bytes
            )
            return token

    def release_reservation(self, reservation_token: str | None) -> None:
        """Idempotently release the unconsumed capacity of one batch."""

        if reservation_token is None:
            return
        token = _safe_token(reservation_token, "reservation_token")
        with self._lock:
            self._release_reservation_locked(token)

    def reservation_usage(self, tenant_id: str, kb_id: str) -> dict[str, int]:
        """Return in-flight artifact capacity for one tenant/KB scope."""

        tenant = _required_scope(tenant_id, "tenant_id")
        knowledge_base = _required_scope(kb_id, "kb_id")
        with self._lock:
            scoped = [
                reservation
                for reservation in self._reservations.values()
                if reservation.tenant_id == tenant
                and reservation.kb_id == knowledge_base
            ]
            remaining = [
                entry
                for reservation in scoped
                for entry in reservation.entries.values()
                if not entry.consumed and entry.reserved_bytes
            ]
            return {
                "reservations": len(scoped),
                "reserved_versions": len(remaining),
                "reserved_bytes": sum(entry.reserved_bytes for entry in remaining),
            }

    def put(
        self,
        tenant_id: str,
        kb_id: str,
        source_id: str,
        version_id: str,
        content: bytes,
        *,
        content_sha256: str,
        media_type: str = "application/octet-stream",
        display_name: str | None = None,
        created_at: float | None = None,
        reservation_token: str | None = None,
    ) -> dict[str, Any]:
        """Atomically create a version, consuming reserved capacity if supplied."""

        tenant, knowledge_base, source, version = self._identity(
            tenant_id, kb_id, source_id, version_id
        )
        if not isinstance(content, bytes):
            raise TypeError("artifact content must be bytes")
        if len(content) > self.max_file_bytes:
            raise ArtifactLimitError("artifact exceeds max_file_bytes")
        expected_hash = _sha256(content_sha256)
        actual_hash = hashlib.sha256(content).hexdigest()
        if actual_hash != expected_hash:
            raise ArtifactIntegrityError("artifact content hash does not match")
        timestamp = time.time() if created_at is None else float(created_at)
        metadata = self._artifact_metadata(
            tenant,
            knowledge_base,
            source,
            version,
            content_sha256=actual_hash,
            byte_size=len(content),
            media_type=media_type,
            display_name=display_name,
            created_at=timestamp,
        )
        encoded_metadata = self._encode_metadata(metadata)
        final = self._version_dir(tenant, knowledge_base, source, version)
        with self._lock:
            reservation_entry: _ReservedArtifact | None = None
            normalized_reservation_token: str | None = None
            if reservation_token is not None:
                normalized_reservation_token = _safe_token(
                    reservation_token, "reservation_token"
                )
                reservation_entry = self._reservation_entry_locked(
                    normalized_reservation_token,
                    tenant,
                    knowledge_base,
                    source,
                    version,
                    encoded_metadata,
                )
            if final.exists():
                existing = self._load_metadata(
                    final,
                    tenant,
                    knowledge_base,
                    source,
                    version,
                    verify_content=True,
                )
                if any(
                    existing.get(key) != metadata.get(key)
                    for key in _IMMUTABLE_ARTIFACT_FIELDS
                ):
                    raise ArtifactConflictError(
                        "source version already exists with different immutable metadata"
                    )
                if (
                    reservation_entry is not None
                    and not reservation_entry.consumed
                    and reservation_entry.reserved_bytes
                ):
                    # A pending reservation normally implies an absent final.
                    # Reconcile the exceptional cross-process winner before
                    # converting the reservation so physical bytes remain exact.
                    self._reconcile_physical_usage_locked()
                if normalized_reservation_token is not None:
                    self._consume_reservation_entry_locked(
                        normalized_reservation_token, source, version
                    )
                    self._assert_capacity_locked(tenant)
                return existing
            source_dir = final.parent
            source_dir.mkdir(parents=True, exist_ok=True)
            self._assert_inside_root(source_dir)
            active_versions = self._active_version_count_locked(
                tenant, knowledge_base, source
            )
            pending_versions = self._reserved_version_count_locked(
                tenant, knowledge_base, source
            )
            owner_identity = (tenant, knowledge_base, source, version)
            owner_token = self._reserved_artifact_owners.get(owner_identity)
            additional_bytes = len(content) + len(encoded_metadata)
            if normalized_reservation_token is not None:
                if (
                    reservation_entry is None
                    or reservation_entry.consumed
                    or owner_token != normalized_reservation_token
                    or reservation_entry.reserved_bytes != additional_bytes
                ):
                    raise ArtifactConflictError(
                        "artifact reservation no longer covers this source version"
                    )
                additional_versions = 0
                capacity_growth = 0
            else:
                if owner_token is not None:
                    raise ArtifactConflictError(
                        "source version is reserved by another batch"
                    )
                additional_versions = 1
                capacity_growth = additional_bytes
            if (
                active_versions + pending_versions + additional_versions
                > self.max_versions_per_source
            ):
                raise ArtifactLimitError("source exceeds max_versions_per_source")
            self._assert_capacity_locked(tenant, capacity_growth)
            temporary = Path(tempfile.mkdtemp(prefix=".tmp-", dir=source_dir))
            try:
                self._write_file(temporary / "payload", content)
                self._write_file(temporary / "metadata.json", encoded_metadata)
                self._fsync_directory(temporary)
                try:
                    os.rename(temporary, final)
                except OSError:
                    if not final.exists():
                        raise
                    existing = self._load_metadata(
                        final,
                        tenant,
                        knowledge_base,
                        source,
                        version,
                        verify_content=True,
                    )
                    if existing.get("content_sha256") != actual_hash:
                        raise ArtifactConflictError(
                            "concurrent immutable version contains different content"
                        )
                    if any(
                        existing.get(key) != metadata.get(key)
                        for key in _IMMUTABLE_ARTIFACT_FIELDS
                    ):
                        raise ArtifactConflictError(
                            "concurrent immutable version contains different metadata"
                        )
                    # Another process won the directory race. Remove this
                    # process's temporary copy before reconciling so it is not
                    # double-counted, then re-establish the global hard bound.
                    shutil.rmtree(temporary)
                    self._reconcile_physical_usage_locked()
                    if normalized_reservation_token is not None:
                        self._consume_reservation_entry_locked(
                            normalized_reservation_token, source, version
                        )
                    self._assert_capacity_locked(tenant)
                    return existing
                self._adjust_cached_physical_usage_locked(tenant, additional_bytes)
                if normalized_reservation_token is not None:
                    self._consume_reservation_entry_locked(
                        normalized_reservation_token, source, version
                    )
                self._fsync_directory(source_dir)
            finally:
                if temporary.exists():
                    shutil.rmtree(temporary)
        return dict(metadata)

    def get_metadata(
        self,
        tenant_id: str,
        kb_id: str,
        source_id: str,
        version_id: str,
        *,
        verify_content: bool = False,
    ) -> dict[str, Any]:
        tenant, knowledge_base, source, version = self._identity(
            tenant_id, kb_id, source_id, version_id
        )
        with self._lock:
            return self._load_metadata(
                self._version_dir(tenant, knowledge_base, source, version),
                tenant,
                knowledge_base,
                source,
                version,
                verify_content=verify_content,
            )

    def read(
        self,
        tenant_id: str,
        kb_id: str,
        source_id: str,
        version_id: str,
    ) -> bytes:
        tenant, knowledge_base, source, version = self._identity(
            tenant_id, kb_id, source_id, version_id
        )
        with self._lock:
            directory = self._version_dir(tenant, knowledge_base, source, version)
            metadata = self._load_metadata(
                directory,
                tenant,
                knowledge_base,
                source,
                version,
                verify_content=False,
            )
            return self._read_verified_payload(directory, metadata)

    def open_verified(
        self,
        tenant_id: str,
        kb_id: str,
        source_id: str,
        version_id: str,
    ) -> tuple[dict[str, Any], BinaryIO]:
        """Return a verified, rewound payload handle without buffering it all.

        The descriptor is opened before the store lock is released, so an API
        soft-delete/rename cannot redirect the subsequent response to another
        path.  Callers own the returned handle and must close it.
        """

        tenant, knowledge_base, source, version = self._identity(
            tenant_id, kb_id, source_id, version_id
        )
        with self._lock:
            directory = self._version_dir(tenant, knowledge_base, source, version)
            metadata = self._load_metadata(
                directory,
                tenant,
                knowledge_base,
                source,
                version,
                verify_content=False,
            )
            payload_path = directory / "payload"
            self._assert_inside_root(payload_path)
            if payload_path.is_symlink() or not payload_path.is_file():
                raise ArtifactIntegrityError("source artifact payload is missing")
            handle = payload_path.open("rb")
            try:
                file_stat = os.fstat(handle.fileno())
                expected_size = int(metadata["byte_size"])
                if (
                    not stat.S_ISREG(file_stat.st_mode)
                    or file_stat.st_size != expected_size
                    or file_stat.st_size > self.max_file_bytes
                ):
                    raise ArtifactIntegrityError(
                        "source artifact payload size does not match"
                    )
            except BaseException:
                handle.close()
                raise

        # Hash the already-open inode outside the store-wide metadata lock.
        # Soft-delete/restore only rename directories, and immutable artifact
        # writes never mutate this descriptor, so releasing the lock cannot
        # retarget the verified stream.  It does prevent one large download
        # from serializing unrelated tenants' artifact operations.
        try:
            digest = hashlib.sha256()
            total_bytes = 0
            while chunk := handle.read(64 * 1024):
                total_bytes += len(chunk)
                digest.update(chunk)
            if total_bytes != expected_size:
                raise ArtifactIntegrityError(
                    "source artifact payload size does not match"
                )
            if digest.hexdigest() != metadata.get("content_sha256"):
                raise ArtifactIntegrityError(
                    "source artifact payload hash does not match"
                )
            handle.seek(0)
        except BaseException:
            handle.close()
            raise
        return metadata, handle

    def list_versions(
        self,
        tenant_id: str,
        kb_id: str,
        source_id: str,
    ) -> list[dict[str, Any]]:
        tenant = _required_scope(tenant_id, "tenant_id")
        knowledge_base = _required_scope(kb_id, "kb_id")
        source = _safe_token(source_id, "source_id")
        source_dir = self._source_dir(tenant, knowledge_base, source)
        if not source_dir.exists():
            return []
        with self._lock:
            rows: list[dict[str, Any]] = []
            for child in source_dir.iterdir():
                if (
                    child.name.startswith(".")
                    or child.is_symlink()
                    or not child.is_dir()
                    or not _SAFE_TOKEN.fullmatch(child.name)
                ):
                    continue
                rows.append(
                    self._load_metadata(
                        child,
                        tenant,
                        knowledge_base,
                        source,
                        child.name,
                        verify_content=False,
                    )
                )
        return sorted(
            rows,
            key=lambda row: (float(row["created_at"]), str(row["version_id"])),
            reverse=True,
        )

    def diff(
        self,
        tenant_id: str,
        kb_id: str,
        source_id: str,
        from_version_id: str,
        to_version_id: str,
    ) -> dict[str, Any]:
        tenant = _required_scope(tenant_id, "tenant_id")
        knowledge_base = _required_scope(kb_id, "kb_id")
        source = _safe_token(source_id, "source_id")
        from_version = _safe_token(from_version_id, "from_version_id")
        to_version = _safe_token(to_version_id, "to_version_id")
        with self._lock:
            from_directory = self._version_dir(
                tenant,
                knowledge_base,
                source,
                from_version,
            )
            to_directory = self._version_dir(
                tenant,
                knowledge_base,
                source,
                to_version,
            )
            from_metadata = self._load_metadata(
                from_directory,
                tenant,
                knowledge_base,
                source,
                from_version,
                verify_content=False,
            )
            to_metadata = self._load_metadata(
                to_directory,
                tenant,
                knowledge_base,
                source,
                to_version,
                verify_content=False,
            )
            result: dict[str, Any] = {
                "kind": "binary",
                "from_version_id": from_version,
                "to_version_id": to_version,
                "diff": None,
                "truncated": False,
                "from": from_metadata,
                "to": to_metadata,
            }
            is_text = self._is_text(from_metadata) and self._is_text(to_metadata)
            prefix_bytes = self.max_diff_bytes if is_text else 0
            before, before_truncated = self._read_verified_payload_prefix(
                from_directory,
                from_metadata,
                prefix_bytes=prefix_bytes,
            )
            if to_directory == from_directory:
                after, after_truncated = before, before_truncated
            else:
                after, after_truncated = self._read_verified_payload_prefix(
                    to_directory,
                    to_metadata,
                    prefix_bytes=prefix_bytes,
                )
            if not is_text:
                return result
            before_lines, before_lines_truncated = self._bounded_text_lines(before)
            after_lines, after_lines_truncated = self._bounded_text_lines(after)
            generated = difflib.unified_diff(
                before_lines,
                after_lines,
                fromfile=from_version,
                tofile=to_version,
                lineterm="",
            )
            rendered, output_truncated = self._bounded_diff(generated)
            result.update(
                {
                    "kind": "text",
                    "diff": rendered,
                    "truncated": before_truncated
                    or after_truncated
                    or before_lines_truncated
                    or after_lines_truncated
                    or output_truncated,
                }
            )
        return result

    def delete_version(
        self,
        tenant_id: str,
        kb_id: str,
        source_id: str,
        version_id: str,
    ) -> dict[str, Any]:
        """Soft-delete an artifact and return the token required to restore it."""

        tenant, knowledge_base, source, version = self._identity(
            tenant_id, kb_id, source_id, version_id
        )
        with self._lock:
            current = self._version_dir(tenant, knowledge_base, source, version)
            metadata = self._load_metadata(
                current,
                tenant,
                knowledge_base,
                source,
                version,
                verify_content=False,
            )
            if (
                tenant,
                knowledge_base,
                source,
                version,
            ) in self._reserved_artifact_owners:
                raise ArtifactConflictError(
                    "source version is reserved by an in-flight batch"
                )
            recovery_token = f"del-{time.time_ns()}-{uuid4().hex}"
            trashed = self._trash_root / recovery_token
            self._assert_inside_root(trashed)
            os.rename(current, trashed)
            self._fsync_directory(current.parent)
            self._fsync_directory(self._trash_root)
        return {
            "deleted": True,
            "recovery_token": recovery_token,
            "metadata": metadata,
        }

    def restore(
        self,
        tenant_id: str,
        kb_id: str,
        recovery_token: str,
    ) -> dict[str, Any]:
        tenant = _required_scope(tenant_id, "tenant_id")
        knowledge_base = _required_scope(kb_id, "kb_id")
        token = _safe_token(recovery_token, "recovery_token")
        trashed = self._trash_root / token
        self._assert_inside_root(trashed)
        with self._lock:
            metadata = self._load_unscoped_metadata(trashed)
            if (
                metadata.get("tenant_id") != tenant
                or metadata.get("kb_id") != knowledge_base
            ):
                # Do not reveal whether another tenant owns this token.
                raise ArtifactNotFoundError("recovery token was not found")
            # Recovery is a trust boundary: do not reactivate a payload whose
            # bytes changed while it was quarantined in the trash area.
            self._verify_payload(trashed, metadata)
            source = _safe_token(metadata.get("source_id"), "source_id")
            version = _safe_token(metadata.get("version_id"), "version_id")
            target = self._version_dir(tenant, knowledge_base, source, version)
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                active = self._load_metadata(
                    target,
                    tenant,
                    knowledge_base,
                    source,
                    version,
                    verify_content=True,
                )
                if any(
                    active.get(key) != metadata.get(key)
                    for key in _IMMUTABLE_ARTIFACT_FIELDS
                ):
                    raise ArtifactConflictError(
                        "active version conflicts with recoverable artifact"
                    )
                removed_bytes = self._tree_usage_bytes(trashed)
                shutil.rmtree(trashed)
                self._adjust_cached_physical_usage_locked(tenant, -removed_bytes)
                self._fsync_directory(self._trash_root)
                return active
            active_versions = self._active_version_count_locked(
                tenant, knowledge_base, source
            )
            pending_other_versions = self._reserved_version_count_locked(
                tenant,
                knowledge_base,
                source,
                exclude_version_id=version,
            )
            if (
                active_versions + pending_other_versions
                >= self.user_max_versions_per_source
            ):
                raise ArtifactLimitError("source exceeds user_max_versions_per_source")
            os.rename(trashed, target)
            self._fsync_directory(target.parent)
            self._fsync_directory(self._trash_root)
            return metadata

    def prune_versions(
        self,
        tenant_id: str,
        kb_id: str,
        source_id: str,
        *,
        keep_latest: int,
        protect_version_ids: tuple[str, ...] = (),
    ) -> list[dict[str, Any]]:
        """Soft-delete old versions, never deleting caller-protected versions."""

        keep = _positive_limit(keep_latest, "keep_latest")
        protected = {
            _safe_token(version, "protect_version_id")
            for version in protect_version_ids
        }
        with self._lock:
            rows = self.list_versions(tenant_id, kb_id, source_id)
            # Protected versions count toward the caller's retention limit.
            # In particular, journal replay can protect a current version whose
            # fetched_at is older than the other rows; a simple ``top N | protected``
            # union would then leak a permanent N+1 artifact.
            existing_versions = {str(row["version_id"]) for row in rows}
            retained = protected & existing_versions
            if len(retained) > keep:
                raise ArtifactLimitError(
                    "protected versions exceed the keep_latest limit"
                )
            for row in rows:
                if len(retained) >= keep:
                    break
                retained.add(str(row["version_id"]))
            candidates = [
                str(row["version_id"])
                for row in rows
                if str(row["version_id"]) not in retained
            ]
            tenant = _required_scope(tenant_id, "tenant_id")
            knowledge_base = _required_scope(kb_id, "kb_id")
            source = _safe_token(source_id, "source_id")
            if any(
                (tenant, knowledge_base, source, version)
                in self._reserved_artifact_owners
                for version in candidates
            ):
                raise ArtifactConflictError(
                    "prune would move a version reserved by an in-flight batch"
                )
            deleted: list[dict[str, Any]] = []
            for version in candidates:
                deleted.append(
                    self.delete_version(tenant_id, kb_id, source_id, version)
                )
        return deleted

    def purge_trash(
        self,
        tenant_id: str,
        kb_id: str,
        *,
        older_than: float,
        limit: int = 100,
    ) -> int:
        """Permanently remove scoped trash older than a caller-chosen boundary."""

        tenant = _required_scope(tenant_id, "tenant_id")
        knowledge_base = _required_scope(kb_id, "kb_id")
        boundary = float(older_than)
        if not math.isfinite(boundary) or boundary < 0:
            raise ValueError("older_than must be a finite non-negative timestamp")
        maximum = _positive_limit(limit, "limit")
        purged = 0
        with self._lock:
            try:
                for child in sorted(
                    self._trash_root.iterdir(), key=lambda item: item.name
                ):
                    if purged >= maximum or child.is_symlink() or not child.is_dir():
                        continue
                    deleted_at = self._deleted_at_from_token(child.name)
                    if deleted_at is None or deleted_at >= boundary:
                        continue
                    try:
                        metadata = self._load_unscoped_metadata(child)
                    except (
                        ArtifactNotFoundError,
                        ArtifactIntegrityError,
                        ValueError,
                    ):
                        continue
                    if (
                        metadata.get("tenant_id") == tenant
                        and metadata.get("kb_id") == knowledge_base
                    ):
                        removed_bytes = self._tree_usage_bytes(child)
                        shutil.rmtree(child)
                        self._adjust_cached_physical_usage_locked(
                            tenant, -removed_bytes
                        )
                        purged += 1
                if purged:
                    self._fsync_directory(self._trash_root)
            except Exception:
                self._reconcile_physical_usage_locked()
                raise
        return purged

    def delete_scope(self, tenant_id: str, kb_id: str) -> dict[str, int]:
        """Idempotently erase active and recoverable artifacts for one KB."""

        tenant = _required_scope(tenant_id, "tenant_id")
        knowledge_base = _required_scope(kb_id, "kb_id")
        scope_dir = self._scope_dir(tenant, knowledge_base)
        active_versions = 0
        trash_versions = 0
        active_bytes = 0
        trash_bytes = 0
        with self._lock:
            try:
                scoped_trash: list[Path] = []
                # Trash is intentionally flat, so its authenticated metadata is
                # the only safe way to decide which scope owns an entry. Verify
                # every entry before deleting anything: silently skipping an
                # unreadable record could report successful KB erasure while a
                # sensitive payload remains orphaned on disk.
                for child in tuple(self._trash_root.iterdir()):
                    if child.is_symlink() or not child.is_dir():
                        raise ArtifactIntegrityError(
                            "artifact trash contains an unverifiable entry"
                        )
                    try:
                        metadata = self._load_unscoped_metadata(child)
                        self._verify_payload(child, metadata)
                    except (
                        ArtifactNotFoundError,
                        ArtifactIntegrityError,
                        OSError,
                        ValueError,
                    ) as exc:
                        raise ArtifactIntegrityError(
                            "artifact trash contains an unverifiable entry"
                        ) from exc
                    if (
                        metadata.get("tenant_id") == tenant
                        and metadata.get("kb_id") == knowledge_base
                    ):
                        scoped_trash.append(child)

                if scope_dir.exists():
                    if scope_dir.is_symlink() or not scope_dir.is_dir():
                        raise ArtifactIntegrityError(
                            "artifact scope directory is invalid"
                        )
                    active_directories = [
                        path
                        for path in scope_dir.glob("sources/*/*")
                        if path.is_dir()
                        and not path.is_symlink()
                        and not path.name.startswith(".")
                    ]
                    active_versions = len(active_directories)
                    active_bytes = sum(
                        self._tree_usage_bytes(path) for path in active_directories
                    )
                    removed_physical_bytes = self._tree_usage_bytes(scope_dir)
                    parent = scope_dir.parent
                    shutil.rmtree(scope_dir)
                    self._adjust_cached_physical_usage_locked(
                        tenant,
                        -active_bytes,
                        global_delta=-removed_physical_bytes,
                    )
                    self._fsync_directory(parent)

                trash_changed = False
                for child in scoped_trash:
                    removed_bytes = self._tree_usage_bytes(child)
                    shutil.rmtree(child)
                    trash_versions += 1
                    trash_bytes += removed_bytes
                    self._adjust_cached_physical_usage_locked(tenant, -removed_bytes)
                    trash_changed = True
                if trash_changed:
                    self._fsync_directory(self._trash_root)
                self._release_scope_reservations_locked(tenant, knowledge_base)
            except Exception:
                # A recursive delete can fail after removing only part of a
                # tree. Reconcile before propagating so future quota decisions
                # still use the bytes that actually remain on disk.
                self._reconcile_physical_usage_locked()
                raise
        return {
            "active_versions": active_versions,
            "trash_versions": trash_versions,
            "freed_bytes": active_bytes + trash_bytes,
        }

    def usage(self, tenant_id: str, kb_id: str) -> dict[str, int]:
        tenant = _required_scope(tenant_id, "tenant_id")
        knowledge_base = _required_scope(kb_id, "kb_id")
        with self._lock:
            scope_dir = self._scope_dir(tenant, knowledge_base)
            active_bytes = self._tree_usage_bytes(scope_dir)
            active_versions = sum(
                1
                for path in scope_dir.glob("sources/*/*/metadata.json")
                if not path.is_symlink()
            )
            trash_bytes = 0
            trash_versions = 0
            for child in self._trash_root.iterdir():
                if child.is_symlink() or not child.is_dir():
                    continue
                try:
                    metadata = self._load_unscoped_metadata(child)
                except (ArtifactNotFoundError, ArtifactIntegrityError, ValueError):
                    continue
                if (
                    metadata.get("tenant_id") == tenant
                    and metadata.get("kb_id") == knowledge_base
                ):
                    trash_bytes += self._tree_usage_bytes(child)
                    trash_versions += 1
        return {
            "active_bytes": active_bytes,
            "active_versions": active_versions,
            "trash_bytes": trash_bytes,
            "trash_versions": trash_versions,
        }

    def _artifact_metadata(
        self,
        tenant_id: str,
        kb_id: str,
        source_id: str,
        version_id: str,
        *,
        content_sha256: object,
        byte_size: object,
        media_type: object,
        display_name: object,
        created_at: object,
    ) -> dict[str, Any]:
        digest = _sha256(content_sha256)
        if version_id != build_version_id(source_id, digest):
            raise ArtifactIntegrityError(
                "artifact version_id does not match its content address"
            )
        if (
            isinstance(byte_size, bool)
            or not isinstance(byte_size, int)
            or byte_size < 0
        ):
            raise ValueError("byte_size must be a non-negative integer")
        if byte_size > self.max_file_bytes:
            raise ArtifactLimitError("artifact exceeds max_file_bytes")
        normalized_media_type = (
            str(media_type or "").split(";", 1)[0].strip().casefold()
        )
        if not normalized_media_type or len(normalized_media_type) > 255:
            raise ValueError("media_type is invalid")
        normalized_name = None
        if display_name is not None:
            normalized_name = str(display_name).strip()
            if (
                not normalized_name
                or len(normalized_name) > 1_024
                or "\x00" in normalized_name
            ):
                raise ValueError("display_name is invalid")
        if isinstance(created_at, bool):
            raise ValueError("created_at must be a finite non-negative timestamp")
        try:
            timestamp = float(str(created_at))
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "created_at must be a finite non-negative timestamp"
            ) from exc
        if not math.isfinite(timestamp) or timestamp < 0:
            raise ValueError("created_at must be a finite non-negative timestamp")
        return {
            "tenant_id": tenant_id,
            "kb_id": kb_id,
            "source_id": source_id,
            "version_id": version_id,
            "content_sha256": digest,
            "byte_size": byte_size,
            "media_type": normalized_media_type,
            "display_name": normalized_name,
            "created_at": timestamp,
        }

    def _active_version_count_locked(
        self, tenant_id: str, kb_id: str, source_id: str
    ) -> int:
        source_dir = self._source_dir(tenant_id, kb_id, source_id)
        if not source_dir.exists():
            return 0
        return sum(
            1
            for child in source_dir.iterdir()
            if child.is_dir()
            and not child.is_symlink()
            and not child.name.startswith(".")
        )

    def _reserved_version_count_locked(
        self,
        tenant_id: str,
        kb_id: str,
        source_id: str,
        *,
        exclude_version_id: str | None = None,
    ) -> int:
        count = 0
        for (
            tenant,
            knowledge_base,
            source,
            version,
        ), token in self._reserved_artifact_owners.items():
            if (
                tenant != tenant_id
                or knowledge_base != kb_id
                or source != source_id
                or version == exclude_version_id
            ):
                continue
            reservation = self._reservations[token]
            entry = reservation.entries[(source, version)]
            if entry.reserved_bytes:
                count += 1
        return count

    def _adjust_cached_physical_usage_locked(
        self,
        tenant_id: str,
        tenant_delta: int,
        *,
        global_delta: int | None = None,
    ) -> None:
        physical_delta = tenant_delta if global_delta is None else global_delta
        self._cached_physical_usage_bytes = max(
            0, self._cached_physical_usage_bytes + physical_delta
        )
        tenant_usage = max(
            0,
            self._cached_physical_usage_bytes_by_tenant.get(tenant_id, 0)
            + tenant_delta,
        )
        if tenant_usage:
            self._cached_physical_usage_bytes_by_tenant[tenant_id] = tenant_usage
        else:
            self._cached_physical_usage_bytes_by_tenant.pop(tenant_id, None)

    def _assert_capacity_locked(
        self, tenant_id: str, additional_bytes: int = 0
    ) -> None:
        if (
            self._cached_physical_usage_bytes
            + self._reserved_physical_usage_bytes
            + additional_bytes
            > self.max_total_bytes
        ):
            raise ArtifactLimitError("artifact store exceeds max_total_bytes")
        if (
            self._cached_physical_usage_bytes_by_tenant.get(tenant_id, 0)
            + self._reserved_physical_usage_bytes_by_tenant.get(tenant_id, 0)
            + additional_bytes
            > self.max_bytes_per_tenant
        ):
            raise ArtifactLimitError(
                "tenant artifact storage exceeds max_bytes_per_tenant"
            )

    def _reservation_entry_locked(
        self,
        reservation_token: str,
        tenant_id: str,
        kb_id: str,
        source_id: str,
        version_id: str,
        encoded_metadata: bytes,
    ) -> _ReservedArtifact:
        reservation = self._reservations.get(reservation_token)
        if (
            reservation is None
            or reservation.tenant_id != tenant_id
            or reservation.kb_id != kb_id
        ):
            raise ArtifactConflictError("artifact reservation is unavailable")
        entry = reservation.entries.get((source_id, version_id))
        if entry is None or entry.encoded_metadata != encoded_metadata:
            raise ArtifactConflictError(
                "artifact write does not match its batch reservation"
            )
        return entry

    def _consume_reservation_entry_locked(
        self, reservation_token: str, source_id: str, version_id: str
    ) -> None:
        reservation = self._reservations.get(reservation_token)
        if reservation is None:
            raise ArtifactConflictError("artifact reservation is unavailable")
        entry = reservation.entries.get((source_id, version_id))
        if entry is None:
            raise ArtifactConflictError(
                "artifact write does not match its batch reservation"
            )
        if entry.consumed:
            return
        owner_identity = (
            reservation.tenant_id,
            reservation.kb_id,
            source_id,
            version_id,
        )
        if self._reserved_artifact_owners.get(owner_identity) != reservation_token:
            raise ArtifactConflictError(
                "artifact reservation ownership is inconsistent"
            )
        self._reserved_artifact_owners.pop(owner_identity)
        if entry.reserved_bytes:
            self._reserved_physical_usage_bytes -= entry.reserved_bytes
            tenant = reservation.tenant_id
            remaining = (
                self._reserved_physical_usage_bytes_by_tenant.get(tenant, 0)
                - entry.reserved_bytes
            )
            if remaining:
                self._reserved_physical_usage_bytes_by_tenant[tenant] = remaining
            else:
                self._reserved_physical_usage_bytes_by_tenant.pop(tenant, None)
        entry.consumed = True

    def _release_reservation_locked(self, reservation_token: str) -> None:
        reservation = self._reservations.pop(reservation_token, None)
        if reservation is None:
            return
        self._reservation_tokens_by_key.pop(
            (
                reservation.tenant_id,
                reservation.kb_id,
                reservation.reservation_key,
            ),
            None,
        )
        released_bytes = 0
        for (source, version), entry in reservation.entries.items():
            if entry.consumed:
                continue
            self._reserved_artifact_owners.pop(
                (
                    reservation.tenant_id,
                    reservation.kb_id,
                    source,
                    version,
                ),
                None,
            )
            released_bytes += entry.reserved_bytes
        self._reserved_physical_usage_bytes -= released_bytes
        tenant = reservation.tenant_id
        remaining = (
            self._reserved_physical_usage_bytes_by_tenant.get(tenant, 0)
            - released_bytes
        )
        if remaining:
            self._reserved_physical_usage_bytes_by_tenant[tenant] = remaining
        else:
            self._reserved_physical_usage_bytes_by_tenant.pop(tenant, None)

    def _release_scope_reservations_locked(self, tenant_id: str, kb_id: str) -> None:
        tokens = [
            token
            for token, reservation in self._reservations.items()
            if reservation.tenant_id == tenant_id and reservation.kb_id == kb_id
        ]
        for token in tokens:
            self._release_reservation_locked(token)

    @staticmethod
    def _identity(
        tenant_id: object,
        kb_id: object,
        source_id: object,
        version_id: object,
    ) -> tuple[str, str, str, str]:
        return (
            _required_scope(tenant_id, "tenant_id"),
            _required_scope(kb_id, "kb_id"),
            _safe_token(source_id, "source_id"),
            _safe_token(version_id, "version_id"),
        )

    @staticmethod
    def _scope_key(prefix: str, value: str) -> str:
        digest = hashlib.sha256(
            b"cogdoc-artifact-scope-v1\0"
            + prefix.encode("ascii")
            + b"\0"
            + value.encode("utf-8")
        ).hexdigest()
        return f"{prefix}-{digest}"

    def _scope_dir(self, tenant_id: str, kb_id: str) -> Path:
        path = (
            self.root
            / self._scope_key("tenant", tenant_id)
            / self._scope_key("kb", kb_id)
        )
        self._assert_inside_root(path)
        return path

    def _source_dir(self, tenant_id: str, kb_id: str, source_id: str) -> Path:
        path = self._scope_dir(tenant_id, kb_id) / "sources" / source_id
        self._assert_inside_root(path)
        return path

    def _version_dir(
        self, tenant_id: str, kb_id: str, source_id: str, version_id: str
    ) -> Path:
        path = self._source_dir(tenant_id, kb_id, source_id) / version_id
        self._assert_inside_root(path)
        return path

    def _assert_inside_root(self, path: Path) -> None:
        try:
            path.resolve().relative_to(self.root)
        except ValueError as exc:
            raise ValueError("artifact path escapes its root") from exc

    @staticmethod
    def _encode_metadata(metadata: dict[str, Any]) -> bytes:
        return json.dumps(
            metadata,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    @staticmethod
    def _write_file(path: Path, content: bytes) -> None:
        with path.open("xb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _load_unscoped_metadata(self, directory: Path) -> dict[str, Any]:
        self._assert_inside_root(directory)
        if directory.is_symlink() or not directory.is_dir():
            raise ArtifactNotFoundError("source artifact was not found")
        metadata_path = directory / "metadata.json"
        if metadata_path.is_symlink() or not metadata_path.is_file():
            raise ArtifactNotFoundError("source artifact metadata was not found")
        try:
            raw = metadata_path.read_bytes()
            if len(raw) > 64 * 1024:
                raise ArtifactIntegrityError("artifact metadata exceeds its bound")
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ArtifactIntegrityError("source artifact metadata is invalid") from exc
        if not isinstance(payload, dict):
            raise ArtifactIntegrityError("source artifact metadata must be an object")
        required = {
            "tenant_id",
            "kb_id",
            "source_id",
            "version_id",
            "content_sha256",
            "byte_size",
            "media_type",
            "created_at",
        }
        if not required.issubset(payload):
            raise ArtifactIntegrityError("source artifact metadata is incomplete")
        try:
            _required_scope(payload.get("tenant_id"), "tenant_id")
            _required_scope(payload.get("kb_id"), "kb_id")
            source_id = _safe_token(payload.get("source_id"), "source_id")
            version_id = _safe_token(payload.get("version_id"), "version_id")
            content_sha256 = _sha256(payload.get("content_sha256"))
            byte_size = payload.get("byte_size")
            if (
                isinstance(byte_size, bool)
                or not isinstance(byte_size, int)
                or byte_size < 0
            ):
                raise ValueError("byte_size is invalid")
            created_at = payload.get("created_at")
            if isinstance(created_at, bool) or not isinstance(created_at, (int, float)):
                raise ValueError("created_at is invalid")
            timestamp = float(created_at)
            if not math.isfinite(timestamp) or timestamp < 0:
                raise ValueError("created_at is invalid")
            media_type = payload.get("media_type")
            if not isinstance(media_type, str) or not media_type:
                raise ValueError("media_type is invalid")
        except (TypeError, ValueError) as exc:
            raise ArtifactIntegrityError("source artifact metadata is invalid") from exc
        if version_id != build_version_id(source_id, content_sha256):
            raise ArtifactIntegrityError(
                "artifact version_id does not match its content address"
            )
        return payload

    def _load_metadata(
        self,
        directory: Path,
        tenant_id: str,
        kb_id: str,
        source_id: str,
        version_id: str,
        *,
        verify_content: bool,
    ) -> dict[str, Any]:
        metadata = self._load_unscoped_metadata(directory)
        expected = {
            "tenant_id": tenant_id,
            "kb_id": kb_id,
            "source_id": source_id,
            "version_id": version_id,
        }
        if any(metadata.get(key) != value for key, value in expected.items()):
            raise ArtifactIntegrityError("artifact metadata does not match its scope")
        if verify_content:
            self._verify_payload(directory, metadata)
        return metadata

    def _verify_payload(self, directory: Path, metadata: dict[str, Any]) -> None:
        self._read_verified_payload_prefix(directory, metadata, prefix_bytes=0)

    def _read_verified_payload(
        self, directory: Path, metadata: dict[str, Any]
    ) -> bytes:
        content, _ = self._read_verified_payload_prefix(
            directory,
            metadata,
            prefix_bytes=int(metadata["byte_size"]),
        )
        return content

    def _read_verified_payload_prefix(
        self,
        directory: Path,
        metadata: dict[str, Any],
        *,
        prefix_bytes: int,
    ) -> tuple[bytes, bool]:
        if prefix_bytes < 0:
            raise ValueError("prefix_bytes must be non-negative")
        payload_path = directory / "payload"
        self._assert_inside_root(payload_path)
        if payload_path.is_symlink() or not payload_path.is_file():
            raise ArtifactIntegrityError("source artifact payload is missing")
        expected_size = int(metadata["byte_size"])
        digest = hashlib.sha256()
        prefix = bytearray()
        total_bytes = 0
        with payload_path.open("rb") as handle:
            file_stat = os.fstat(handle.fileno())
            if not stat.S_ISREG(file_stat.st_mode):
                raise ArtifactIntegrityError(
                    "source artifact payload is not a regular file"
                )
            if (
                file_stat.st_size != expected_size
                or file_stat.st_size > self.max_file_bytes
            ):
                raise ArtifactIntegrityError(
                    "source artifact payload size does not match"
                )
            while chunk := handle.read(64 * 1024):
                total_bytes += len(chunk)
                digest.update(chunk)
                remaining = prefix_bytes - len(prefix)
                if remaining > 0:
                    prefix.extend(chunk[:remaining])
        if total_bytes != expected_size:
            raise ArtifactIntegrityError("source artifact payload size does not match")
        if digest.hexdigest() != metadata.get("content_sha256"):
            raise ArtifactIntegrityError("source artifact payload hash does not match")
        return bytes(prefix), total_bytes > prefix_bytes

    def _cleanup_stale_temporary_directories(self) -> None:
        """Remove private put remnants before establishing startup usage."""

        for path in tuple(self.root.glob("tenant-*/kb-*/sources/*/.tmp-*")):
            self._assert_inside_root(path)
            if path.is_symlink() or not path.is_dir():
                raise ArtifactIntegrityError(
                    "artifact temporary path is not a private directory"
                )
            parent = path.parent
            shutil.rmtree(path)
            self._fsync_directory(parent)

    def _physical_usage_bytes(self) -> int:
        return self._tree_usage_bytes(self.root)

    def _physical_usage_bytes_by_tenant(self) -> dict[str, int]:
        usage: dict[str, int] = {}
        active_metadata_paths = self.root.glob(
            "tenant-*/kb-*/sources/*/*/metadata.json"
        )
        for metadata_path in active_metadata_paths:
            directory = metadata_path.parent
            try:
                metadata = self._load_unscoped_metadata(directory)
                tenant = _required_scope(metadata.get("tenant_id"), "tenant_id")
                knowledge_base = _required_scope(metadata.get("kb_id"), "kb_id")
                source = _safe_token(metadata.get("source_id"), "source_id")
                version = _safe_token(metadata.get("version_id"), "version_id")
                if directory != self._version_dir(
                    tenant, knowledge_base, source, version
                ):
                    raise ArtifactIntegrityError(
                        "artifact metadata does not match its physical scope"
                    )
            except (
                ArtifactNotFoundError,
                ArtifactIntegrityError,
                OSError,
                ValueError,
            ):
                # Unattributable bytes remain covered by the global emergency
                # cap. Scope operations still fail closed on corrupt records.
                continue
            usage[tenant] = usage.get(tenant, 0) + self._tree_usage_bytes(directory)

        for directory in self._trash_root.iterdir():
            if directory.is_symlink() or not directory.is_dir():
                continue
            try:
                metadata = self._load_unscoped_metadata(directory)
                tenant = _required_scope(metadata.get("tenant_id"), "tenant_id")
            except (
                ArtifactNotFoundError,
                ArtifactIntegrityError,
                OSError,
                ValueError,
            ):
                continue
            usage[tenant] = usage.get(tenant, 0) + self._tree_usage_bytes(directory)
        return usage

    def _reconcile_physical_usage_locked(self) -> int:
        self._cached_physical_usage_bytes = self._physical_usage_bytes()
        self._cached_physical_usage_bytes_by_tenant = (
            self._physical_usage_bytes_by_tenant()
        )
        return self._cached_physical_usage_bytes

    @staticmethod
    def _tree_usage_bytes(root: Path) -> int:
        if not root.exists():
            return 0
        total = 0
        for directory, directory_names, file_names in os.walk(
            root, topdown=True, followlinks=False
        ):
            base = Path(directory)
            directory_names[:] = [
                name for name in directory_names if not (base / name).is_symlink()
            ]
            for name in file_names:
                path = base / name
                try:
                    info = path.lstat()
                except FileNotFoundError:
                    continue
                if stat.S_ISREG(info.st_mode):
                    total += info.st_size
        return total

    @staticmethod
    def _is_text(metadata: dict[str, Any]) -> bool:
        media_type = str(metadata.get("media_type") or "").casefold()
        if media_type.startswith("text/") or media_type in _TEXT_MEDIA_TYPES:
            return True
        display_name = str(metadata.get("display_name") or "")
        return Path(display_name).suffix.casefold() in _TEXT_SUFFIXES

    def _bounded_text_lines(self, content: bytes) -> tuple[list[str], bool]:
        truncated = len(content) > self.max_diff_bytes
        bounded = content[: self.max_diff_bytes]
        text = bounded.decode("utf-8", errors="replace")
        lines = text.splitlines()
        if len(lines) > self.max_diff_lines:
            lines = lines[: self.max_diff_lines]
            truncated = True
        return lines, truncated

    def _bounded_diff(self, lines: Iterable[str]) -> tuple[str, bool]:
        output: list[str] = []
        byte_count = 0
        truncated = False
        for line in lines:
            rendered = str(line) + "\n"
            encoded = rendered.encode("utf-8")
            if len(output) >= self.max_diff_lines:
                truncated = True
                break
            remaining = self.max_diff_bytes - byte_count
            if remaining <= 0:
                truncated = True
                break
            if len(encoded) > remaining:
                output.append(encoded[:remaining].decode("utf-8", errors="ignore"))
                byte_count = self.max_diff_bytes
                truncated = True
                break
            output.append(rendered)
            byte_count += len(encoded)
        return "".join(output), truncated

    @staticmethod
    def _deleted_at_from_token(token: str) -> float | None:
        parts = token.split("-", 2)
        if len(parts) != 3 or parts[0] != "del":
            return None
        try:
            return int(parts[1]) / 1_000_000_000
        except ValueError:
            return None
