"""Append-only, tamper-evident audit storage.

The store deliberately accepts only audit metadata.  Request bodies and raw
credentials are not part of its API and sensitive metadata keys are rejected.
Each tenant has an independent sequence and SHA-256 hash chain even though all
events share one JSONL file.
"""

from __future__ import annotations

import builtins
import hashlib
import hmac
import json
import math
import os
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any, TypeGuard


AUDIT_SCHEMA_VERSION = "v1"
GENESIS_HASH = "0" * 64
_MAX_PAGE_SIZE = 1000
_HASH_HEX_LENGTH = 64
_EVENT_FIELDS = frozenset(
    {
        "schema_version",
        "tenant",
        "sequence",
        "timestamp",
        "principal",
        "action",
        "method",
        "path",
        "status",
        "resource",
        "result",
        "request_id",
        "prev_hash",
        "event_hash",
    }
)
_SENSITIVE_METADATA_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "authorization",
        "body",
        "cookie",
        "credential",
        "credentials",
        "password",
        "payload",
        "raw_body",
        "request_body",
        "secret",
        "set_cookie",
        "token",
    }
)


class AuditStoreError(RuntimeError):
    """Base class for durable audit-store failures."""


class AuditIntegrityError(AuditStoreError):
    """Raised when persisted audit data cannot be trusted."""


class AuditCorruptionError(AuditIntegrityError):
    """Raised for malformed, truncated, or cryptographically invalid data."""


class AuditWriteError(AuditStoreError):
    """Raised when an event could not be durably appended."""


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("audit metadata must be finite JSON data") from exc


def _event_hash(event_without_hash: Mapping[str, Any]) -> str:
    payload = _canonical_json(event_without_hash).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _normalize_key(key: str) -> str:
    return key.strip().casefold().replace("-", "_").replace(" ", "_")


def _copy_safe_metadata(value: object, *, field: str) -> object:
    """Validate and detach JSON metadata while rejecting credential/body keys."""

    def validate(item: object, location: str) -> None:
        if item is None or isinstance(item, (str, bool, int)):
            return
        if isinstance(item, float):
            if not math.isfinite(item):
                raise ValueError(f"{location} must not contain non-finite numbers")
            return
        if isinstance(item, Mapping):
            for key, child in item.items():
                if not isinstance(key, str):
                    raise ValueError(f"{location} keys must be strings")
                if _normalize_key(key) in _SENSITIVE_METADATA_KEYS:
                    raise ValueError(
                        f"{location} must not contain request bodies or credentials"
                    )
                validate(child, f"{location}.{key}")
            return
        if isinstance(item, Sequence) and not isinstance(item, (str, bytes, bytearray)):
            for index, child in enumerate(item):
                validate(child, f"{location}[{index}]")
            return
        raise ValueError(f"{location} must contain only JSON values")

    validate(value, field)
    return json.loads(_canonical_json(value))


def _nonempty_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _optional_text(value: object, field: str) -> str | None:
    if value is None:
        return None
    return _nonempty_text(value, field)


def _object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _decode_line(raw_line: bytes, line_number: int) -> dict[str, Any]:
    if not raw_line.endswith(b"\n"):
        raise AuditCorruptionError(f"audit record on line {line_number} is truncated")
    encoded = raw_line[:-1]
    if not encoded:
        raise AuditCorruptionError(f"audit record on line {line_number} is empty")
    try:
        text = encoded.decode("utf-8")
        value = json.loads(text, object_pairs_hook=_object_without_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise AuditCorruptionError(
            f"audit record on line {line_number} is malformed"
        ) from exc
    if not isinstance(value, dict):
        raise AuditCorruptionError(
            f"audit record on line {line_number} must be a JSON object"
        )
    return value


class AuditStore:
    """One-file JSONL audit store with independent per-tenant hash chains.

    ``list`` returns newest events first.  ``before_sequence`` is an exclusive
    tenant-local cursor, so it can be set to the last sequence from one page to
    fetch the next page.
    """

    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)
        self._lock = RLock()
        self._events: builtins.list[dict[str, Any]] = []
        self._tenant_heads: dict[str, tuple[int, str]] = {}
        self._file_signature: tuple[int, int, int, int, int] | None = None
        self._poisoned: AuditStoreError | None = None

        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            events, heads = self._read_verified_locked()
            self._events = events
            self._tenant_heads = heads
            self._file_signature = self._stat_signature()

    def record(
        self,
        tenant: str,
        principal: str,
        action: str,
        method: str,
        path: str,
        status: int,
        resource: object,
        result: object,
        request_id: str | None,
    ) -> dict[str, Any]:
        """Durably append one metadata-only audit event."""

        clean_tenant = _nonempty_text(tenant, "tenant")
        clean_principal = _nonempty_text(principal, "principal")
        clean_action = _nonempty_text(action, "action")
        clean_method = _nonempty_text(method, "method").upper()
        clean_path = _nonempty_text(path, "path")
        clean_request_id = _optional_text(request_id, "request_id")
        if "?" in clean_path or "#" in clean_path:
            raise ValueError("path must exclude query strings and fragments")
        if clean_principal.casefold().startswith("bearer "):
            raise ValueError("principal must be an identifier, not a credential")
        if type(status) is not int or not 100 <= status <= 599:
            raise ValueError("status must be an HTTP status integer")
        clean_resource = _copy_safe_metadata(resource, field="resource")
        clean_result = _copy_safe_metadata(result, field="result")

        with self._lock:
            self._raise_if_poisoned()
            self._refresh_if_changed_locked()
            previous_sequence, previous_hash = self._tenant_heads.get(
                clean_tenant, (0, GENESIS_HASH)
            )
            unsigned: dict[str, Any] = {
                "schema_version": AUDIT_SCHEMA_VERSION,
                "tenant": clean_tenant,
                "sequence": previous_sequence + 1,
                "timestamp": datetime.now(timezone.utc)
                .isoformat(timespec="microseconds")
                .replace("+00:00", "Z"),
                "principal": clean_principal,
                "action": clean_action,
                "method": clean_method,
                "path": clean_path,
                "status": status,
                "resource": clean_resource,
                "result": clean_result,
                "request_id": clean_request_id,
                "prev_hash": previous_hash,
            }
            event = {**unsigned, "event_hash": _event_hash(unsigned)}
            encoded = (_canonical_json(event) + "\n").encode("utf-8")
            existed = self.path.exists()
            try:
                self._durable_append_locked(encoded)
                if not existed:
                    self._fsync_parent_directory()
            except Exception as exc:
                error = AuditWriteError("audit event could not be durably appended")
                self._poisoned = error
                raise error from exc

            self._events.append(event)
            self._tenant_heads[clean_tenant] = (
                event["sequence"],
                event["event_hash"],
            )
            self._file_signature = self._stat_signature()
            return _copy_event(event)

    def list(
        self,
        tenant: str,
        *,
        limit: int = 100,
        before_sequence: int | None = None,
    ) -> builtins.list[dict[str, Any]]:
        """Return one tenant's events in descending sequence order."""

        clean_tenant = _nonempty_text(tenant, "tenant")
        if type(limit) is not int or not 1 <= limit <= _MAX_PAGE_SIZE:
            raise ValueError(f"limit must be between 1 and {_MAX_PAGE_SIZE}")
        if before_sequence is not None and (
            type(before_sequence) is not int or before_sequence < 1
        ):
            raise ValueError("before_sequence must be a positive integer")
        with self._lock:
            self._raise_if_poisoned()
            self._refresh_if_changed_locked()
            selected = [
                event
                for event in reversed(self._events)
                if event["tenant"] == clean_tenant
                and (before_sequence is None or event["sequence"] < before_sequence)
            ][:limit]
            return [_copy_event(event) for event in selected]

    def snapshot(
        self,
        tenant: str,
        *,
        from_sequence: int | None = None,
        to_sequence: int | None = None,
        actions: Sequence[str] = (),
        statuses: Sequence[int] = (),
    ) -> builtins.list[dict[str, Any]]:
        """Return a verified, ascending, point-in-time tenant snapshot.

        This is the export seam: callers never read the JSONL file directly,
        so an export has exactly the same integrity and tenant-isolation
        guarantees as the online audit API.
        """

        clean_tenant = _nonempty_text(tenant, "tenant")
        if from_sequence is not None and (
            type(from_sequence) is not int or from_sequence < 1
        ):
            raise ValueError("from_sequence must be a positive integer")
        if to_sequence is not None and (
            type(to_sequence) is not int or to_sequence < 1
        ):
            raise ValueError("to_sequence must be a positive integer")
        if (
            from_sequence is not None
            and to_sequence is not None
            and from_sequence > to_sequence
        ):
            raise ValueError("from_sequence must not exceed to_sequence")
        clean_actions = frozenset(_nonempty_text(item, "action") for item in actions)
        clean_statuses = frozenset(statuses)
        if any(type(item) is not int or not 100 <= item <= 599 for item in statuses):
            raise ValueError("statuses must contain HTTP status integers")
        with self._lock:
            self._raise_if_poisoned()
            self._refresh_if_changed_locked()
            selected = [
                event
                for event in self._events
                if event["tenant"] == clean_tenant
                and (from_sequence is None or event["sequence"] >= from_sequence)
                and (to_sequence is None or event["sequence"] <= to_sequence)
                and (not clean_actions or event["action"] in clean_actions)
                and (not clean_statuses or event["status"] in clean_statuses)
            ]
            return [_copy_event(event) for event in selected]

    def verify(self) -> bool:
        """Re-read and cryptographically verify the complete JSONL file."""

        with self._lock:
            self._raise_if_poisoned()
            events, heads = self._read_verified_locked()
            self._assert_append_only_prefix(events)
            self._events = events
            self._tenant_heads = heads
            self._file_signature = self._stat_signature()
            return True

    def check(self) -> bool:
        """Cheap request-boundary integrity check, re-reading only on change."""

        with self._lock:
            self._raise_if_poisoned()
            self._refresh_if_changed_locked()
            return True

    def _raise_if_poisoned(self) -> None:
        if self._poisoned is not None:
            raise AuditStoreError(
                "audit store is fail-closed after an earlier write failure"
            ) from self._poisoned

    def _refresh_if_changed_locked(self) -> None:
        signature = self._stat_signature()
        if signature == self._file_signature:
            return
        events, heads = self._read_verified_locked()
        self._assert_append_only_prefix(events)
        self._events = events
        self._tenant_heads = heads
        self._file_signature = self._stat_signature()

    def _assert_append_only_prefix(self, events: builtins.list[dict[str, Any]]) -> None:
        # A validly re-hashed rewrite is still not an append.  While this store
        # is alive, retain its last verified prefix as an in-process anchor so
        # deletion, truncation, or history replacement fails closed too.
        known_count = len(self._events)
        if len(events) < known_count or events[:known_count] != self._events:
            raise AuditCorruptionError(
                "audit log was truncated or its verified history was replaced"
            )

    def _read_verified_locked(
        self,
    ) -> tuple[builtins.list[dict[str, Any]], dict[str, tuple[int, str]]]:
        if not self.path.exists():
            return [], {}
        events: builtins.list[dict[str, Any]] = []
        heads: dict[str, tuple[int, str]] = {}
        try:
            with self.path.open("rb") as handle:
                for line_number, raw_line in enumerate(handle, start=1):
                    event = _decode_line(raw_line, line_number)
                    self._validate_event(event, heads, line_number)
                    events.append(event)
                    heads[event["tenant"]] = (
                        event["sequence"],
                        event["event_hash"],
                    )
        except AuditIntegrityError:
            raise
        except OSError as exc:
            raise AuditIntegrityError("audit log could not be read") from exc
        return events, heads

    @staticmethod
    def _validate_event(
        event: dict[str, Any],
        heads: dict[str, tuple[int, str]],
        line_number: int,
    ) -> None:
        if frozenset(event) != _EVENT_FIELDS:
            raise AuditCorruptionError(
                f"audit record on line {line_number} has an invalid schema"
            )
        if event.get("schema_version") != AUDIT_SCHEMA_VERSION:
            raise AuditCorruptionError(
                f"audit record on line {line_number} has an unsupported schema"
            )
        try:
            tenant = _nonempty_text(event.get("tenant"), "tenant")
            _nonempty_text(event.get("timestamp"), "timestamp")
            _nonempty_text(event.get("principal"), "principal")
            _nonempty_text(event.get("action"), "action")
            _nonempty_text(event.get("method"), "method")
            persisted_path = _nonempty_text(event.get("path"), "path")
            _optional_text(event.get("request_id"), "request_id")
            _copy_safe_metadata(event.get("resource"), field="resource")
            _copy_safe_metadata(event.get("result"), field="result")
        except ValueError as exc:
            raise AuditCorruptionError(
                f"audit record on line {line_number} has invalid field data"
            ) from exc
        if "?" in persisted_path or "#" in persisted_path:
            raise AuditCorruptionError(
                f"audit record on line {line_number} contains a query or fragment"
            )
        sequence = event.get("sequence")
        status = event.get("status")
        if type(sequence) is not int or sequence < 1:
            raise AuditCorruptionError(
                f"audit record on line {line_number} has an invalid sequence"
            )
        if type(status) is not int or not 100 <= status <= 599:
            raise AuditCorruptionError(
                f"audit record on line {line_number} has an invalid status"
            )
        previous_sequence, previous_hash = heads.get(tenant, (0, GENESIS_HASH))
        if sequence != previous_sequence + 1:
            raise AuditCorruptionError(
                f"audit sequence for tenant {tenant!r} is not contiguous on line "
                f"{line_number}"
            )
        persisted_prev_hash = event.get("prev_hash")
        persisted_event_hash = event.get("event_hash")
        if not _is_hash(persisted_prev_hash) or not _is_hash(persisted_event_hash):
            raise AuditCorruptionError(
                f"audit record on line {line_number} has an invalid hash"
            )
        if not hmac.compare_digest(persisted_prev_hash, previous_hash):
            raise AuditCorruptionError(
                f"audit hash chain is broken on line {line_number}"
            )
        unsigned = {key: value for key, value in event.items() if key != "event_hash"}
        expected_hash = _event_hash(unsigned)
        if not hmac.compare_digest(persisted_event_hash, expected_hash):
            raise AuditCorruptionError(
                f"audit event hash does not match on line {line_number}"
            )

    def _durable_append_locked(self, encoded: bytes) -> None:
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        descriptor = os.open(self.path, flags, 0o600)
        try:
            with os.fdopen(descriptor, "ab", buffering=0) as handle:
                descriptor = -1
                written = handle.write(encoded)
                if written != len(encoded):
                    raise OSError("short audit-log append")
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def _fsync_parent_directory(self) -> None:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        descriptor = os.open(self.path.parent, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _stat_signature(self) -> tuple[int, int, int, int, int] | None:
        try:
            stat = self.path.stat()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise AuditIntegrityError("audit log metadata could not be read") from exc
        # ctime cannot be restored by an ordinary file writer.  Including it
        # closes the same-inode/same-size rewrite bypass where an attacker
        # restores mtime after modifying bytes.  Mutations additionally call
        # ``verify`` at the HTTP boundary, so this signature is only the fast
        # path for read-only requests.
        return (
            stat.st_dev,
            stat.st_ino,
            stat.st_size,
            stat.st_mtime_ns,
            stat.st_ctime_ns,
        )


def _is_hash(value: object) -> TypeGuard[str]:
    if not isinstance(value, str) or len(value) != _HASH_HEX_LENGTH:
        return False
    return all(character in "0123456789abcdef" for character in value)


def _copy_event(event: Mapping[str, Any]) -> dict[str, Any]:
    return json.loads(_canonical_json(event))


__all__ = [
    "AUDIT_SCHEMA_VERSION",
    "GENESIS_HASH",
    "AuditCorruptionError",
    "AuditIntegrityError",
    "AuditStore",
    "AuditStoreError",
    "AuditWriteError",
]
