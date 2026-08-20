from __future__ import annotations

import os
import secrets
import stat
from dataclasses import dataclass
from threading import RLock
from typing import Any

from cogdoc.tools.source_parser import SUPPORTED_EXTENSIONS


class TenantQuotaExceeded(RuntimeError):
    """Raised before a tenant mutation would exceed a configured hard limit."""

    def __init__(
        self,
        resource: str,
        *,
        limit: int,
        used: int,
        requested: int,
    ) -> None:
        self.resource = resource
        self.limit = limit
        self.used = used
        self.requested = requested
        super().__init__(
            f"tenant {resource} quota exceeded: "
            f"used={used}, requested={requested}, limit={limit}"
        )


class TenantMutationInProgress(RuntimeError):
    """Reject duplicate file mutations while their quota is still reserved."""


@dataclass(frozen=True)
class TenantQuotaPolicy:
    max_knowledge_bases: int = 0
    max_documents: int = 0
    max_storage_bytes: int = 0

    def public_limits(self) -> dict[str, int | None]:
        return {
            "knowledge_bases": self.max_knowledge_bases or None,
            "documents": self.max_documents or None,
            "storage_bytes": self.max_storage_bytes or None,
        }


@dataclass(frozen=True)
class _Reservation:
    token: str
    tenant_id: str
    kind: str
    storage_id: str = ""
    filename: str = ""
    document_delta: int = 0
    byte_delta: int = 0


class TenantQuotaManager:
    """Admission-time tenant quotas with in-flight reservation accounting.

    Actual usage is derived from the registry and committed source files.  A
    reservation closes the window between admission and an asynchronous index
    job reaching a terminal state.  Limits of zero intentionally mean
    unlimited, preserving the single-user deployment contract.
    """

    def __init__(self, registry: Any, policy: TenantQuotaPolicy) -> None:
        self._registry = registry
        self.policy = policy
        self._lock = RLock()
        self._reservations: dict[str, _Reservation] = {}

    @property
    def enabled(self) -> bool:
        return any(
            (
                self.policy.max_knowledge_bases,
                self.policy.max_documents,
                self.policy.max_storage_bytes,
            )
        )

    def _tenant_records(self, tenant_id: str) -> list[dict[str, Any]]:
        try:
            return list(self._registry.list(tenant_id=tenant_id))
        except TypeError:
            return [
                row
                for row in self._registry.list()
                if str(row.get("tenant_id") or "default") == tenant_id
            ]

    @staticmethod
    def _document_entries(source_dir: str) -> dict[str, int]:
        result: dict[str, int] = {}
        try:
            entries = os.scandir(source_dir)
        except FileNotFoundError:
            return result
        with entries:
            for entry in entries:
                if os.path.splitext(entry.name)[1].casefold() not in SUPPORTED_EXTENSIONS:
                    continue
                try:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    size = entry.stat(follow_symlinks=False).st_size
                except FileNotFoundError:
                    # A concurrent delete is covered by the serialization and
                    # will be reflected by the next admission snapshot.
                    continue
                except OSError:
                    # Quota accounting is an authorization boundary. An
                    # unreadable committed entry must reject admission rather
                    # than be treated as zero usage.
                    raise
                result[entry.name] = max(0, int(size))
        return result

    @classmethod
    def _document_usage(cls, source_dir: str) -> tuple[int, int]:
        entries = cls._document_entries(source_dir)
        return len(entries), sum(entries.values())

    def _actual_usage(self, tenant_id: str) -> dict[str, int]:
        records = self._tenant_records(tenant_id)
        documents = 0
        storage_bytes = 0
        for row in records:
            storage_id = str(row.get("storage_id") or row.get("kb_id") or "")
            if not storage_id:
                continue
            source_dir = self._registry.source_dir(storage_id)
            count, byte_count = self._document_usage(source_dir)
            documents += count
            storage_bytes += byte_count
        return {
            "knowledge_bases": len(records),
            "documents": documents,
            "storage_bytes": storage_bytes,
        }

    def snapshot(self, tenant_id: str) -> dict[str, Any]:
        with self._lock:
            usage = self._actual_usage(tenant_id)
            reserved = {
                "knowledge_bases": sum(
                    item.kind == "knowledge_base"
                    for item in self._reservations.values()
                    if item.tenant_id == tenant_id
                ),
                "documents": sum(
                    item.document_delta
                    for item in self._reservations.values()
                    if item.tenant_id == tenant_id
                ),
                "storage_bytes": sum(
                    item.byte_delta
                    for item in self._reservations.values()
                    if item.tenant_id == tenant_id
                ),
            }
            return {
                "tenant_id": tenant_id,
                "limits": self.policy.public_limits(),
                "usage": usage,
                "reserved": reserved,
            }

    @staticmethod
    def _enforce(resource: str, limit: int, used: int, requested: int) -> None:
        if limit > 0 and used + requested > limit:
            raise TenantQuotaExceeded(
                resource,
                limit=limit,
                used=used,
                requested=requested,
            )

    def reserve_knowledge_base(self, tenant_id: str) -> str:
        with self._lock:
            usage = self._actual_usage(tenant_id)
            pending = sum(
                item.kind == "knowledge_base"
                for item in self._reservations.values()
                if item.tenant_id == tenant_id
            )
            self._enforce(
                "knowledge_bases",
                self.policy.max_knowledge_bases,
                usage["knowledge_bases"] + pending,
                1,
            )
            token = secrets.token_hex(16)
            self._reservations[token] = _Reservation(
                token=token,
                tenant_id=tenant_id,
                kind="knowledge_base",
            )
            return token

    def reserve_upload(
        self,
        tenant_id: str,
        storage_id: str,
        source_dir: str,
        filename: str,
        content_bytes: int,
    ) -> str:
        safe_name = os.path.basename(filename)
        if not safe_name or safe_name != filename:
            raise ValueError("filename must be a basename")
        content_bytes = max(0, int(content_bytes))
        with self._lock:
            for item in self._reservations.values():
                if (
                    item.kind == "upload"
                    and item.storage_id == storage_id
                    and item.filename == safe_name
                ):
                    raise TenantMutationInProgress(
                        f"document mutation already pending: {safe_name}"
                    )

            usage = self._actual_usage(tenant_id)
            destination = os.path.join(source_dir, safe_name)
            try:
                destination_stat = os.stat(destination, follow_symlinks=False)
                existed = stat.S_ISREG(destination_stat.st_mode)
                old_size = destination_stat.st_size if existed else 0
            except OSError:
                # An unreadable existing destination must not be treated as an
                # overwrite that earns quota credit.
                old_size = 0
                existed = False
            document_delta = 0 if existed else 1
            # A pending replacement must never lend speculative quota credit
            # to another job: the replacement can still fail and roll back to
            # the larger original.  Smaller files reduce actual usage only
            # after they are durably committed.
            byte_delta = max(0, content_bytes - max(0, int(old_size)))
            pending_documents = sum(
                item.document_delta
                for item in self._reservations.values()
                if item.tenant_id == tenant_id
            )
            pending_bytes = sum(
                item.byte_delta
                for item in self._reservations.values()
                if item.tenant_id == tenant_id
            )
            self._enforce(
                "documents",
                self.policy.max_documents,
                usage["documents"] + pending_documents,
                document_delta,
            )
            self._enforce(
                "storage_bytes",
                self.policy.max_storage_bytes,
                usage["storage_bytes"] + pending_bytes,
                byte_delta,
            )
            token = secrets.token_hex(16)
            self._reservations[token] = _Reservation(
                token=token,
                tenant_id=tenant_id,
                kind="upload",
                storage_id=storage_id,
                filename=safe_name,
                document_delta=document_delta,
                byte_delta=byte_delta,
            )
            return token

    def reserve_connector_snapshot(
        self,
        tenant_id: str,
        storage_id: str,
        source_dir: str,
        baseline_dir: str,
        proposed_dir: str,
        reservation_key: str,
    ) -> str | None:
        """Reserve the non-negative growth of one connector snapshot.

        Connector staging is private until its directory swap.  Comparing the
        previous and proposed connection directories lets admission account
        only for this job's growth while ``_actual_usage`` continues to cover
        uploads and every other connector in the tenant.  Shrinkage is never
        lent speculatively: it becomes available after the committed source
        directory is visible and this reservation is released.
        """

        if not (
            self.policy.max_documents or self.policy.max_storage_bytes
        ):
            return None
        key = str(reservation_key or "").strip()
        if not key or len(key) > 256:
            raise ValueError("connector reservation key is invalid")
        with self._lock:
            for item in self._reservations.values():
                if (
                    item.kind == "connector"
                    and item.tenant_id == tenant_id
                    and item.storage_id == storage_id
                    and item.filename == key
                ):
                    raise TenantMutationInProgress(
                        f"connector snapshot already pending: {key}"
                    )

            usage = self._actual_usage(tenant_id)
            baseline = self._document_entries(baseline_dir)
            proposed = self._document_entries(proposed_dir)
            published = self._document_entries(source_dir)
            affected_names = baseline.keys() | proposed.keys()
            published_affected = {
                name: published[name]
                for name in affected_names
                if name in published
            }
            # During recovery the top-level source directory may contain any
            # prefix of the new snapshot. Project its final state rather than
            # blindly adding the whole proposed-minus-baseline delta, which
            # would double-charge files already published before a crash.
            document_delta = max(0, len(proposed) - len(published_affected))
            byte_delta = max(
                0, sum(proposed.values()) - sum(published_affected.values())
            )
            pending_documents = sum(
                item.document_delta
                for item in self._reservations.values()
                if item.tenant_id == tenant_id
            )
            pending_bytes = sum(
                item.byte_delta
                for item in self._reservations.values()
                if item.tenant_id == tenant_id
            )
            self._enforce(
                "documents",
                self.policy.max_documents,
                usage["documents"] + pending_documents,
                document_delta,
            )
            self._enforce(
                "storage_bytes",
                self.policy.max_storage_bytes,
                usage["storage_bytes"] + pending_bytes,
                byte_delta,
            )
            token = secrets.token_hex(16)
            self._reservations[token] = _Reservation(
                token=token,
                tenant_id=tenant_id,
                kind="connector",
                storage_id=storage_id,
                filename=key,
                document_delta=document_delta,
                byte_delta=byte_delta,
            )
            return token

    def release(self, token: str | None) -> None:
        if not token:
            return
        with self._lock:
            self._reservations.pop(token, None)


__all__ = [
    "TenantMutationInProgress",
    "TenantQuotaExceeded",
    "TenantQuotaManager",
    "TenantQuotaPolicy",
]
