from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import time
import filecmp
from collections.abc import Callable, Iterable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from cogdoc.connectors.base import RetryableConnectorError
from cogdoc.service.external_acl import ExternalAclSnapshot, ExternalAclSynchronizer
from cogdoc.service.kb_locks import kb_write_lock
from cogdoc.service.source_catalog import SourceCatalog
from cogdoc.source_model import SourceDocument
from cogdoc.tools.chunk_identity import build_document_id
from cogdoc.tools.source_parser import (
    CONNECTOR_MATERIALIZED_PREFIX,
    SUPPORTED_EXTENSIONS,
)


_MANIFEST = ".cogdoc-connection.json"
_SOURCE_CONTRACTS = ".cogdoc-source-contracts.json"
_PROVIDER_ACL_CONNECTORS = frozenset({"confluence", "sharepoint"})


def _safe_component(value: str, field: str) -> str:
    text = str(value or "").strip()
    if (
        not text
        or len(text) > 180
        or text in {".", ".."}
        or any(char in "/\\\x00" or ord(char) < 32 for char in text)
    ):
        raise ValueError(f"{field} is invalid")
    return text


def _materialized_name(document: SourceDocument) -> str:
    display = Path(document.display_name).name
    suffix = Path(display).suffix.casefold()
    if not display or suffix not in SUPPORTED_EXTENSIONS:
        raise ValueError("connector source has an unsupported display name")
    # Connector files live in a reserved flat namespace because the legacy
    # indexer scans only the source-directory top level.  The complete source
    # identity avoids collisions between connections, while the original name
    # remains available in the source contract and catalog metadata.
    identity = document.source_id.removeprefix("src-")
    return f"{CONNECTOR_MATERIALIZED_PREFIX}{identity}{suffix}"


class MaterializedSyncSink:
    """Crash-recoverable directory swap plus catalog, ACL, and index commit."""

    def __init__(
        self,
        *,
        source_dir: str,
        catalog: SourceCatalog,
        index_submitter: Callable[[str], Mapping[str, Any]],
        index_status_reader: Callable[[str], Mapping[str, Any] | None] | None = None,
        keyed_index_submitter: Callable[[str, str], Mapping[str, Any]] | None = None,
        owner_id: str,
        workspace_visible: bool,
        acl_sync: ExternalAclSynchronizer | None = None,
        artifact_store: Any | None = None,
        artifact_versions_to_keep: int = 10,
        quota_reserver: (
            Callable[[str, str, str, str, str, str], str | None] | None
        ) = None,
        quota_releaser: Callable[[str | None], None] | None = None,
        index_timeout_seconds: float = 30.0,
        commit_store: Any | None = None,
    ) -> None:
        if (
            isinstance(index_timeout_seconds, bool)
            or not isinstance(index_timeout_seconds, (int, float))
            or not math.isfinite(float(index_timeout_seconds))
            or index_timeout_seconds <= 0
        ):
            raise ValueError("connector index timeout must be positive")
        self.source_dir = Path(source_dir).resolve()
        self.catalog = catalog
        self.index_submitter = index_submitter
        self.index_status_reader = index_status_reader
        self.keyed_index_submitter = keyed_index_submitter
        self.owner_id = owner_id
        self.workspace_visible = workspace_visible
        self.acl_sync = acl_sync
        self.artifact_store = artifact_store
        self.artifact_versions_to_keep = artifact_versions_to_keep
        self.quota_reserver = quota_reserver
        self.quota_releaser = quota_releaser
        self.index_timeout_seconds = float(index_timeout_seconds)
        self.commit_store = commit_store
        self.job_id = ""
        self.tenant_id = ""
        self.kb_id = ""
        self.connection_id = ""
        self.connector_type = ""
        self.staging: Path | None = None
        self.current: Path | None = None
        self.backup: Path | None = None
        self.journal: Path | None = None
        self._rows: dict[str, dict[str, Any]] = {}
        self._quota_token: str | None = None
        self._staging_prepared = False
        self._artifact_reservation_token: str | None = None
        self._index_job_id: str | None = None
        self._authority_committed = False

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
    ) -> None:
        del attempt
        self.job_id = _safe_component(job_id, "job_id")
        self.tenant_id = _safe_component(tenant_id, "tenant_id")
        self.kb_id = _safe_component(kb_id, "kb_id")
        self.connection_id = _safe_component(connection_id, "connection_id")
        self.connector_type = _safe_component(connector_type, "connector_type")
        self._staging_prepared = False
        self._index_job_id = None
        self._authority_committed = recovering_commit
        self.source_dir.mkdir(parents=True, exist_ok=True)
        connection_root = self.source_dir / ".connections"
        connection_root.mkdir(exist_ok=True)
        work_root = self.source_dir.parent / f".{self.source_dir.name}.sync-work"
        work_root.mkdir(exist_ok=True)
        self.current = connection_root / self.connection_id
        self.staging = work_root / f"{self.connection_id}-{self.job_id}.staging"
        self.backup = work_root / f"{self.connection_id}-{self.job_id}.backup"
        self.journal = work_root / f"{self.connection_id}-{self.job_id}.journal.json"
        if recovering_commit and self.commit_store is not None:
            restored = self.commit_store.restore(
                job_id=self.job_id,
                tenant_id=self.tenant_id,
                kb_id=self.kb_id,
                connection_id=self.connection_id,
                connector_type=self.connector_type,
                staging=self.staging,
            )
            raw_index_job_id = restored.get("index_job_id")
            self._index_job_id = (
                _safe_component(raw_index_job_id, "index_job_id")
                if isinstance(raw_index_job_id, str)
                else None
            )
            self._write_json_atomic(
                self.journal,
                {
                    "phase": "prepared",
                    "job_id": self.job_id,
                    "connection_id": self.connection_id,
                    "index_job_id": self._index_job_id,
                },
            )
        if self.journal.exists() or self._staging_prepared:
            return
        if recovering_commit:
            if not self.staging.exists():
                raise RetryableConnectorError(
                    "sync commit staging directory is missing"
                )
            self._rows = self._read_manifest(self.staging)
            return
        if self.staging.exists():
            shutil.rmtree(self.staging)
        if self.current.exists():
            try:
                # Unchanged materializations are immutable. Hard links make a
                # delta sync O(changed bytes) while every mutation below uses
                # replace/unlink and therefore cannot alter ``current``.
                shutil.copytree(self.current, self.staging, copy_function=os.link)
            except OSError:
                if self.staging.exists():
                    shutil.rmtree(self.staging)
                shutil.copytree(self.current, self.staging)
        else:
            self.staging.mkdir()
        self._rows = self._read_manifest(self.staging)

    def upsert(
        self,
        document: SourceDocument,
        content: bytes,
        *,
        acl: Mapping[str, Any] | None = None,
    ) -> None:
        if self.staging is None or self.journal is None:
            raise RuntimeError("sync sink has not begun")
        scoped = SourceDocument.create(
            connector_type=document.connector_type,
            external_id=f"{self.connection_id}:{document.external_id}",
            display_name=document.display_name,
            content_sha256=document.version.content_sha256,
            media_type=document.media_type,
            kind=document.kind,
            byte_size=document.version.byte_size,
            origin_uri=document.origin_uri,
            etag=document.version.etag,
            modified_at=document.version.modified_at,
            fetched_at=document.version.fetched_at,
            metadata={
                **document.metadata,
                "connection_id": self.connection_id,
                "provider_external_id": document.external_id,
            },
        )
        name = _materialized_name(scoped)
        scoped = SourceDocument.create(
            connector_type=scoped.connector_type,
            external_id=scoped.external_id,
            display_name=name,
            content_sha256=scoped.version.content_sha256,
            media_type=scoped.media_type,
            kind=scoped.kind,
            byte_size=scoped.version.byte_size,
            origin_uri=scoped.origin_uri,
            etag=scoped.version.etag,
            modified_at=scoped.version.modified_at,
            fetched_at=scoped.version.fetched_at,
            metadata={
                **scoped.metadata,
                "original_display_name": document.display_name,
                "materialized_name": name,
            },
        )
        target = self.staging / name
        temporary = target.with_suffix(target.suffix + ".tmp")
        with temporary.open("wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        previous = self._rows.get(document.external_id)
        if previous and previous.get("filename") != name:
            (self.staging / str(previous["filename"])).unlink(missing_ok=True)
        self._rows[document.external_id] = {
            "external_id": document.external_id,
            "filename": name,
            "document": scoped.to_manifest_document(),
            "acl": dict(acl) if isinstance(acl, Mapping) else None,
        }

    def delete(self, external_id: str) -> None:
        if self.staging is None:
            raise RuntimeError("sync sink has not begun")
        row = self._rows.pop(external_id, None)
        if row:
            (self.staging / str(row["filename"])).unlink(missing_ok=True)

    def prepare_commit(
        self,
        *,
        snapshot: bool,
        seen_external_ids: frozenset[str],
    ) -> None:
        """Finalize the private staging tree before the authority boundary."""

        if self.staging is None or self.journal is None:
            raise RuntimeError("sync sink has not begun")
        if self.journal.exists():
            return
        if snapshot:
            for external_id in tuple(self._rows):
                if external_id not in seen_external_ids:
                    row = self._rows.pop(external_id)
                    (self.staging / str(row["filename"])).unlink(missing_ok=True)
        # This is the single durable staging handoff. The runtime moves its
        # SQLite job to ``committing`` only after this manifest is fsynced, so
        # a crash in between can recover without replaying provider pages.
        self._write_manifest(self.staging)
        # Persist the staging directory entry itself, not only its manifest.
        # Otherwise a power loss after the SQLite authority transition can
        # retain the committing row while losing the whole staging tree.
        self._fsync_directory(self.staging.parent)
        with kb_write_lock(self.kb_id):
            self._validate_publish_ownership()
        self._reserve_quota(self.current, self.staging)
        self._reserve_artifacts(self.staging)
        if self.commit_store is not None:
            self.commit_store.prepare(
                job_id=self.job_id,
                tenant_id=self.tenant_id,
                kb_id=self.kb_id,
                connection_id=self.connection_id,
                connector_type=self.connector_type,
                staging=self.staging,
            )
        self._staging_prepared = True

    def mark_committing(self) -> None:
        """Record that the canonical sync ledger crossed its commit boundary."""

        if not self._staging_prepared:
            raise RuntimeError("sync staging is not prepared")
        self._authority_committed = True

    def commit(
        self,
        *,
        snapshot: bool,
        seen_external_ids: frozenset[str],
        heartbeat: Callable[[], None],
    ) -> None:
        staging = self.staging
        current = self.current
        backup = self.backup
        journal = self.journal
        if staging is None or current is None or backup is None or journal is None:
            raise RuntimeError("sync sink has not begun")
        self.prepare_commit(
            snapshot=snapshot,
            seen_external_ids=seen_external_ids,
        )
        self._write_journal("prepared")
        try:
            heartbeat()
            with kb_write_lock(self.kb_id):
                if backup.exists():
                    shutil.rmtree(backup)
                if current.exists():
                    os.replace(current, backup)
                os.replace(staging, current)
                self._fsync_directory(current.parent)
                self._fsync_directory(staging.parent)
                self._write_journal("swapped")
                self._apply_side_effects(heartbeat)
                self._write_journal("materialized")
            self._build_index(heartbeat)
            self._finish_stale_acl_retirements()
            self._write_journal("indexed")
        except Exception as exc:
            raise RetryableConnectorError("materialized sync commit failed") from exc

    def recover_commit(self, *, heartbeat: Callable[[], None]) -> None:
        if self.journal is None or self.current is None or self.staging is None:
            raise RuntimeError("sync sink has not begun")
        if not self.journal.exists():
            if not self.staging.exists():
                raise RetryableConnectorError(
                    "sync commit journal and staging directory are missing"
                )
            self._reserve_quota(self._recovery_baseline(), self.staging)
            self._reserve_artifacts(self.staging)
            # The ledger enters ``committing`` only after prepare_commit has
            # finalized this private staging tree. Recreate the tiny journal
            # after a process loss in the DB-to-filesystem handoff window.
            self._write_journal("prepared")
        phase = self._read_journal()
        if phase == "prepared":
            self._reserve_quota(self._recovery_baseline(), self.staging)
            self._reserve_artifacts(self.staging)
        elif phase == "swapped":
            self._reserve_quota(self.backup, self.current)
            self._reserve_artifacts(self.current)
        else:
            # Side effects have already published the proposed files into the
            # source root, so actual tenant usage includes them. Reserve zero
            # growth while index completion is retried.
            self._reserve_quota(self.source_dir, self.source_dir)
            self._reserve_artifacts(self.current)
        with kb_write_lock(self.kb_id):
            if phase == "prepared" and self.staging.exists():
                if self.current.exists() and self.backup:
                    if self.backup.exists():
                        shutil.rmtree(self.backup)
                    os.replace(self.current, self.backup)
                os.replace(self.staging, self.current)
                self._fsync_directory(self.current.parent)
                self._fsync_directory(self.staging.parent)
                self._write_journal("swapped")
                phase = "swapped"
            if phase in {"prepared", "swapped"}:
                self._apply_side_effects(heartbeat)
                self._write_journal("materialized")
                phase = "materialized"
        if phase == "materialized":
            self._build_index(heartbeat)
            self._finish_stale_acl_retirements()
            self._write_journal("indexed")

    def finalize(self) -> None:
        try:
            if self.commit_store is not None:
                self.commit_store.finalize(self.job_id)
            for path in (self.staging, self.backup):
                if path and path.exists():
                    shutil.rmtree(path)
            if self.journal:
                self.journal.unlink(missing_ok=True)
        finally:
            self._release_quota()
            self._release_artifact_reservation()

    @staticmethod
    def cleanup_work(
        *, source_dir: str | Path, connection_id: str, job_id: str
    ) -> None:
        """Idempotently remove one terminal job's private work artifacts."""

        source = Path(source_dir)
        connection = _safe_component(connection_id, "connection_id")
        job = _safe_component(job_id, "job_id")
        work_root = source.parent / f".{source.name}.sync-work"
        for suffix in ("staging", "backup"):
            path = work_root / f"{connection}-{job}.{suffix}"
            if path.exists():
                shutil.rmtree(path)
        (work_root / f"{connection}-{job}.journal.json").unlink(missing_ok=True)

    @classmethod
    def cleanup_connection(
        cls,
        *,
        source_dir: str | Path,
        tenant_id: str,
        kb_id: str,
        connection_id: str,
        catalog: SourceCatalog,
        index_submitter: Callable[[str], Mapping[str, Any]],
        index_status_reader: Callable[[str], Mapping[str, Any] | None],
        resource_access_store: Any | None = None,
        acl_document_ids: Iterable[str] = (),
        work_job_ids: Iterable[str] = (),
        acl_state_cleaner: Callable[[str, str, str, tuple[str, ...]], Any]
        | None = None,
        authority_guard: Callable[[], None] | None = None,
        index_timeout_seconds: float = 30.0,
        cleanup_index_job_id: str | None = None,
        index_job_recorder: Callable[[str], Any] | None = None,
    ) -> dict[str, int]:
        """Remove one fenced connection's visible projection and rebuild search.

        The connection definition deliberately remains outside this method.  Its
        caller deletes that durable retry handle only after this routine returns.
        Every ownership fact needed after a crash is retained in at least one of
        the source contract, current manifest, or tombstoned catalog rows.
        """

        tenant = _safe_component(tenant_id, "tenant_id")
        knowledge_base = _safe_component(kb_id, "kb_id")
        connection = _safe_component(connection_id, "connection_id")
        source = Path(source_dir).resolve()
        connection_root = source / ".connections"
        current = connection_root / connection
        retired_root = source.parent / f".{source.name}.connection-delete"
        retired_current = retired_root / hashlib.sha256(connection.encode()).hexdigest()
        work_root = source.parent / f".{source.name}.sync-work"
        managed_by = f"connector:{connection}"

        managed_document_ids = tuple(
            dict.fromkeys(
                _safe_component(document_id, "document_id")
                for document_id in acl_document_ids
            )
        )
        owned_job_ids = tuple(
            dict.fromkeys(_safe_component(job_id, "job_id") for job_id in work_job_ids)
        )
        begin_retirement = None
        finish_retirement = None
        if resource_access_store is not None:
            begin_retirement = getattr(
                resource_access_store, "begin_document_retirement", None
            )
            finish_retirement = getattr(
                resource_access_store, "finish_document_retirement", None
            )
            if not callable(begin_retirement) or not callable(finish_retirement):
                raise RuntimeError(
                    "resource access store does not support fenced cleanup"
                )

        with kb_write_lock(knowledge_base):
            if authority_guard is not None:
                authority_guard()
            if connection_root.is_symlink():
                raise ValueError(
                    "connection materialization root must not be a symlink"
                )
            if retired_root.is_symlink():
                raise ValueError("connection deletion root must not be a symlink")
            if retired_root.exists() and not retired_root.is_dir():
                raise ValueError("connection deletion root is not a directory")
            if current.is_symlink() or (current.exists() and not current.is_dir()):
                raise ValueError("connection materialization is not a directory")
            if retired_current.is_symlink() or (
                retired_current.exists() and not retired_current.is_dir()
            ):
                raise ValueError(
                    "retired connection materialization is not a directory"
                )
            if current.exists() and retired_current.exists():
                raise ValueError("connection materialization cleanup state conflicts")
            catalog_rows = catalog.list_sources(
                tenant,
                knowledge_base,
                include_deleted=True,
                connection_id=connection,
            )
            source_ids = tuple(str(row["source_id"]) for row in catalog_rows)
            catalog_claims: dict[str, list[Mapping[str, Any]]] = {}
            for row in catalog_rows:
                name = cls._catalog_materialized_name(row)
                if name is not None:
                    catalog_claims.setdefault(name, []).append(row)
            materialized_names = set(catalog_claims)

            manifest = (
                cls._read_manifest(current, required=True) if current.exists() else {}
            )
            manifest_claims: dict[str, list[Mapping[str, Any]]] = {}
            for row in manifest.values():
                name = cls._owned_manifest_name(row, connection)
                manifest_claims.setdefault(name, []).append(row)
                materialized_names.add(name)

            contract_path = source / _SOURCE_CONTRACTS
            documents = cls._read_source_contracts(contract_path)
            owned_contract_names = {
                cls._owned_contract_name(name, document, connection)
                for name, document in documents.items()
                if cls._contract_connection_id(document) == connection
            }
            materialized_names.update(owned_contract_names)
            for name in materialized_names:
                contract = documents.get(name)
                if (
                    contract is not None
                    and cls._contract_connection_id(contract) != connection
                ):
                    raise ValueError(
                        "connection materialization ownership ledgers conflict"
                    )
                if not name.startswith(CONNECTOR_MATERIALIZED_PREFIX):
                    cls._validate_legacy_ownership(
                        source_dir=source,
                        current=current,
                        name=name,
                        connection_id=connection,
                        catalog_rows=catalog_claims.get(name, ()),
                        manifest_rows=manifest_claims.get(name, ()),
                        contract_document=documents.get(name),
                    )
            for row in catalog.list_sources(
                tenant,
                knowledge_base,
                include_deleted=True,
            ):
                row_connection = str(row.get("connection_id") or "")
                if not row_connection or row_connection == connection:
                    continue
                if cls._catalog_materialized_name(row) in materialized_names:
                    raise ValueError(
                        "connection materialization ownership ledgers conflict"
                    )
            owned_names = tuple(sorted(str(name) for name in materialized_names))
            document_ids = tuple(
                dict.fromkeys(
                    (
                        *(build_document_id(name) for name in owned_names),
                        *managed_document_ids,
                    )
                )
            )
            quarantined_policies = (
                int(
                    begin_retirement(
                        tenant,
                        knowledge_base,
                        managed_by,
                        document_ids,
                    )
                )
                if begin_retirement is not None
                else 0
            )

            # Files first, then the ownership ledgers.  A process loss can
            # therefore never leave an unowned top-level file that a retry is
            # unable to identify safely.
            removed_files = 0
            for name in owned_names:
                target = source / name
                if target.is_symlink():
                    raise ValueError(
                        "connector materialization target must not be a symlink"
                    )
                if target.is_file():
                    target.unlink()
                    removed_files += 1
                elif target.exists():
                    raise ValueError(
                        "connector materialization target is not a regular file"
                    )
            if source.exists():
                cls._fsync_directory(source)

            if owned_contract_names:
                remaining = {
                    name: document
                    for name, document in documents.items()
                    if name not in owned_contract_names
                }
                cls._write_json_atomic(
                    contract_path,
                    {"schema_version": 1, "documents": remaining},
                )

            if current.exists():
                retired_root.mkdir(exist_ok=True)
                os.replace(current, retired_current)
                cls._fsync_directory(current.parent)
                cls._fsync_directory(retired_root)
            if retired_current.exists():
                shutil.rmtree(retired_current)
                cls._fsync_directory(retired_current.parent)
            removed_work = cls._cleanup_connection_work(
                work_root,
                connection,
                owned_job_ids,
            )

            tombstoned = catalog.tombstone(tenant, knowledge_base, source_ids)

        # The indexer's generation switch takes this same KB lock. Waiting
        # while holding it would deadlock production even though lightweight
        # test indexers often complete without taking the lock.
        cls._rebuild_index(
            knowledge_base,
            index_submitter=index_submitter,
            index_status_reader=index_status_reader,
            timeout_seconds=index_timeout_seconds,
            existing_job_id=cleanup_index_job_id,
            job_recorder=index_job_recorder,
        )

        with kb_write_lock(knowledge_base):
            if authority_guard is not None:
                authority_guard()
            # The active retrieval generation no longer references these
            # sources, so removing their quarantine policies cannot fall back
            # to a broader KB policy and resurrect stale indexed content.
            removed_policies = 0
            if finish_retirement is not None:
                removed_policies = int(
                    finish_retirement(
                        tenant,
                        knowledge_base,
                        managed_by,
                        document_ids,
                    )
                )
            if acl_state_cleaner is not None:
                acl_state_cleaner(
                    tenant,
                    knowledge_base,
                    managed_by,
                    document_ids,
                )

        return {
            "files": removed_files,
            "work_paths": removed_work,
            "sources_tombstoned": int(tombstoned),
            "document_policies_quarantined": quarantined_policies,
            "document_policies": removed_policies,
        }

    @staticmethod
    def _catalog_materialized_name(row: Mapping[str, Any]) -> str | None:
        metadata = row.get("metadata")
        raw_name = (
            metadata.get("materialized_name") if isinstance(metadata, Mapping) else None
        ) or row.get("display_name")
        if raw_name is None:
            return None
        return MaterializedSyncSink._validate_owned_basename(str(raw_name))

    @staticmethod
    def _owned_manifest_name(row: Mapping[str, Any], connection_id: str) -> str:
        document = row.get("document")
        if not isinstance(document, Mapping):
            raise ValueError("connection materialization manifest is invalid")
        metadata = document.get("metadata")
        if (
            not isinstance(metadata, Mapping)
            or str(metadata.get("connection_id") or "") != connection_id
        ):
            raise ValueError("connection materialization manifest ownership is invalid")
        filename = str(row.get("filename") or "")
        document_name = str(document.get("name") or document.get("display_name") or "")
        if filename != document_name:
            raise ValueError("connection materialization manifest filename is invalid")
        return MaterializedSyncSink._validate_owned_basename(filename)

    @staticmethod
    def _contract_connection_id(document: object) -> str:
        metadata = document.get("metadata") if isinstance(document, Mapping) else None
        return (
            str(metadata.get("connection_id") or "")
            if isinstance(metadata, Mapping)
            else ""
        )

    @classmethod
    def _owned_contract_name(
        cls, name: object, document: object, connection_id: str
    ) -> str:
        if cls._contract_connection_id(document) != connection_id:
            raise ValueError("source contract ownership is invalid")
        validated = cls._validate_owned_basename(str(name))
        if (
            not isinstance(document, Mapping)
            or str(document.get("name") or document.get("display_name") or "")
            != validated
        ):
            raise ValueError("source contract filename is invalid")
        return validated

    @staticmethod
    def _validate_owned_name(name: str) -> str:
        value = MaterializedSyncSink._validate_owned_basename(name)
        if not value.startswith(CONNECTOR_MATERIALIZED_PREFIX):
            raise ValueError("connector materialized filename is invalid")
        return value

    @staticmethod
    def _validate_owned_basename(name: str) -> str:
        value = _safe_component(name, "materialized_name")
        if (
            Path(value).name != value
            or Path(value).suffix.casefold() not in SUPPORTED_EXTENSIONS
        ):
            raise ValueError("connector materialized filename is invalid")
        return value

    @classmethod
    def _validate_legacy_ownership(
        cls,
        *,
        source_dir: Path,
        current: Path,
        name: str,
        connection_id: str,
        catalog_rows: Iterable[Mapping[str, Any]],
        manifest_rows: Iterable[Mapping[str, Any]],
        contract_document: object,
    ) -> None:
        """Allow old names only when every durable ownership ledger agrees."""

        target = source_dir / name
        if target.is_symlink():
            raise ValueError("legacy connector materialization must not be a symlink")
        if not target.exists():
            # Cleanup deletes the file before its ownership ledgers.  A retry
            # may therefore have only the durable catalog tombstone left and
            # no longer performs a risky top-level deletion.
            return
        if not target.is_file():
            raise ValueError("legacy connector materialization is not a regular file")
        catalog_values = tuple(catalog_rows)
        manifest_values = tuple(manifest_rows)
        if (
            len(catalog_values) != 1
            or len(manifest_values) != 1
            or not isinstance(contract_document, Mapping)
        ):
            raise ValueError("legacy connector ownership is not fully corroborated")
        catalog_row = catalog_values[0]
        manifest_row = manifest_values[0]
        manifest_document = manifest_row.get("document")
        if not isinstance(manifest_document, Mapping) or dict(
            manifest_document
        ) != dict(contract_document):
            raise ValueError("legacy connector ownership ledgers conflict")
        metadata = manifest_document.get("metadata")
        if not isinstance(metadata, Mapping):
            raise ValueError("legacy connector metadata is invalid")
        provider_external_id = str(metadata.get("provider_external_id") or "")
        original_display_name = str(metadata.get("original_display_name") or "")
        if (
            str(metadata.get("connection_id") or "") != connection_id
            or not provider_external_id
            or not original_display_name
            or str(manifest_row.get("external_id") or "") != provider_external_id
        ):
            raise ValueError("legacy connector identity is invalid")
        normalized = SourceDocument.from_manifest_document(manifest_document)
        expected_external_id = f"{connection_id}:{provider_external_id}"
        display = Path(original_display_name).name
        suffix = Path(display).suffix.casefold()
        stem = Path(display).stem[:120] or "source"
        expected_name = (
            f"{stem}--{normalized.source_id.removeprefix('src-')[:12]}{suffix}"
        )
        if (
            name != expected_name
            or normalized.external_id != expected_external_id
            or str(manifest_document.get("source_id") or "") != normalized.source_id
            or str(manifest_document.get("version_id") or "")
            != normalized.version.version_id
            or str(catalog_row.get("connection_id") or "") != connection_id
            or str(catalog_row.get("source_id") or "") != normalized.source_id
            or str(catalog_row.get("connector_type") or "") != normalized.connector_type
            or str(catalog_row.get("external_id") or "") != expected_external_id
            or str(catalog_row.get("display_name") or "") != name
            or catalog_row.get("metadata") != normalized.metadata
            or str(catalog_row.get("version_id") or "") != normalized.version.version_id
            or str(catalog_row.get("content_sha256") or "")
            != normalized.version.content_sha256
            or catalog_row.get("byte_size") != normalized.version.byte_size
        ):
            raise ValueError("legacy connector ownership ledgers conflict")
        private_copy = current / name
        for path in (target, private_copy):
            if path.is_symlink() or not path.is_file():
                raise ValueError(
                    "legacy connector materialization is not a regular file"
                )
            digest = hashlib.sha256()
            size = 0
            with path.open("rb") as handle:
                while chunk := handle.read(64 * 1024):
                    digest.update(chunk)
                    size += len(chunk)
            if (
                digest.hexdigest() != normalized.version.content_sha256
                or size != normalized.version.byte_size
            ):
                raise ValueError("legacy connector materialization content conflicts")

    @staticmethod
    def _read_source_contracts(path: Path) -> dict[str, Any]:
        if path.is_symlink():
            raise ValueError("source contract sidecar must not be a symlink")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        if not path.is_file():
            raise ValueError("source contract sidecar is not a regular file")
        documents = payload.get("documents")
        if payload.get("schema_version") != 1 or not isinstance(documents, dict):
            raise ValueError("source contract sidecar is invalid")
        return dict(documents)

    @classmethod
    def _cleanup_connection_work(
        cls,
        work_root: Path,
        connection_id: str,
        job_ids: Iterable[str],
    ) -> int:
        if work_root.is_symlink():
            raise ValueError("connector work root must not be a symlink")
        if not work_root.exists():
            return 0
        removed = 0
        for job_id in job_ids:
            for suffix in (".staging", ".backup", ".journal.json"):
                path = work_root / f"{connection_id}-{job_id}{suffix}"
                if path.is_symlink():
                    raise ValueError("connector work path must not be a symlink")
                if suffix == ".journal.json":
                    if path.is_file():
                        path.unlink()
                        removed += 1
                    elif path.exists():
                        raise ValueError("connector work journal is not a regular file")
                elif path.is_dir():
                    shutil.rmtree(path)
                    removed += 1
                elif path.exists():
                    raise ValueError("connector work tree is not a directory")
        if removed:
            cls._fsync_directory(work_root)
        return removed

    @staticmethod
    def _rebuild_index(
        kb_id: str,
        *,
        index_submitter: Callable[[str], Mapping[str, Any]],
        index_status_reader: Callable[[str], Mapping[str, Any] | None],
        timeout_seconds: float = 30.0,
        existing_job_id: str | None = None,
        job_recorder: Callable[[str], Any] | None = None,
    ) -> None:
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("connector cleanup index timeout must be positive")
        job_id = (
            _safe_component(existing_job_id, "cleanup_index_job_id")
            if existing_job_id is not None
            else ""
        )
        deadline = time.monotonic() + timeout_seconds
        while True:
            current = index_status_reader(job_id) if job_id else None
            status = str(current.get("status") or "") if current is not None else ""
            if status == "succeeded":
                return
            if not job_id or current is None or status in {"failed", "cancelled"}:
                job = index_submitter(kb_id)
                job_id = (
                    str(job.get("job_id") or "") if isinstance(job, Mapping) else ""
                )
                if not job_id:
                    raise RuntimeError("connector cleanup index job was not accepted")
                _safe_component(job_id, "cleanup_index_job_id")
                if job_recorder is not None:
                    job_recorder(job_id)
                current = index_status_reader(job_id)
                if current is None:
                    raise RuntimeError("connector cleanup index job disappeared")
                status = str(current.get("status") or "")
                if status == "succeeded":
                    return
                if status in {"failed", "cancelled"}:
                    raise RuntimeError("connector cleanup index job failed")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("connector cleanup index job timed out")
            time.sleep(min(0.1, remaining))

    def abort(self) -> None:
        try:
            if self._authority_committed or (self.journal and self.journal.exists()):
                return
            if self.commit_store is not None:
                self.commit_store.finalize(self.job_id)
            if self.staging and self.staging.exists():
                shutil.rmtree(self.staging)
        finally:
            self._release_quota()
            if self.journal is None or not self.journal.exists():
                self._release_artifact_reservation()

    def _reserve_quota(self, baseline: Path | None, proposed: Path | None) -> None:
        if self._quota_token is not None or self.quota_reserver is None:
            return
        if proposed is None:
            raise RuntimeError("sync quota proposed directory is unavailable")
        self._quota_token = self.quota_reserver(
            self.tenant_id,
            self.kb_id,
            str(self.source_dir),
            str(baseline) if baseline is not None else "",
            str(proposed),
            self.job_id,
        )

    def _recovery_baseline(self) -> Path | None:
        if self.current is not None and self.current.exists():
            return self.current
        if self.backup is not None and self.backup.exists():
            return self.backup
        return self.current

    def _release_quota(self) -> None:
        token = self._quota_token
        self._quota_token = None
        if token is not None and self.quota_releaser is not None:
            self.quota_releaser(token)

    def _reserve_artifacts(self, proposed: Path | None) -> None:
        if self._artifact_reservation_token is not None or self.artifact_store is None:
            return
        reserve = getattr(self.artifact_store, "reserve_batch", None)
        if not callable(reserve):
            return
        if proposed is None:
            raise RuntimeError("artifact reservation directory is unavailable")
        rows = self._read_manifest(proposed)
        artifacts = []
        for row in rows.values():
            document = SourceDocument.from_manifest_document(row["document"])
            artifacts.append(
                {
                    "source_id": document.source_id,
                    "version_id": document.version.version_id,
                    "content_sha256": document.version.content_sha256,
                    "byte_size": document.version.byte_size,
                    "media_type": document.media_type,
                    "display_name": str(
                        document.metadata.get("original_display_name")
                        or document.display_name
                    ),
                    "created_at": document.version.fetched_at,
                }
            )
        self._artifact_reservation_token = reserve(
            self.tenant_id,
            self.kb_id,
            artifacts,
            reservation_key=self.job_id,
        )

    def _release_artifact_reservation(self) -> None:
        token = self._artifact_reservation_token
        self._artifact_reservation_token = None
        release = (
            getattr(self.artifact_store, "release_reservation", None)
            if self.artifact_store is not None
            else None
        )
        if token is not None and callable(release):
            release(token)

    def _apply_side_effects(self, heartbeat: Callable[[], None]) -> None:
        if self.current is None:
            raise RuntimeError("sync sink has not begun")
        self._rows = self._read_manifest(self.current)
        materializations = [
            (row, SourceDocument.from_manifest_document(row["document"]))
            for row in self._rows.values()
        ]
        documents = [document for _row, document in materializations]
        desired = {document.source_id for document in documents}
        stale_rows = [
            row
            for row in self.catalog.list_sources(self.tenant_id, self.kb_id)
            if row.get("metadata", {}).get("connection_id") == self.connection_id
            and row["source_id"] not in desired
        ]
        stale_source_ids = tuple(str(row["source_id"]) for row in stale_rows)
        stale_document_ids = tuple(
            build_document_id(name)
            for row in stale_rows
            if (name := self._catalog_materialized_name(row)) is not None
        )
        self._begin_stale_acl_retirements(stale_document_ids)
        for row, document in materializations:
            if self.artifact_store is not None:
                content = (self.current / str(row["filename"])).read_bytes()
                put_arguments = {
                    "content_sha256": document.version.content_sha256,
                    "media_type": document.media_type,
                    "display_name": str(
                        document.metadata.get("original_display_name")
                        or document.display_name
                    ),
                    "created_at": document.version.fetched_at,
                }
                if self._artifact_reservation_token is not None:
                    put_arguments["reservation_token"] = (
                        self._artifact_reservation_token
                    )
                self.artifact_store.put(
                    self.tenant_id,
                    self.kb_id,
                    document.source_id,
                    document.version.version_id,
                    content,
                    **put_arguments,
                )
            self.catalog.upsert(
                self.tenant_id,
                self.kb_id,
                document,
                connection_id=self.connection_id,
            )
            if self.artifact_store is not None:
                # Prune only after the catalog atomically points at the new
                # version. A crash before this line leaves one recoverable
                # overflow slot; journal replay idempotently completes pruning.
                self.artifact_store.prune_versions(
                    self.tenant_id,
                    self.kb_id,
                    document.source_id,
                    keep_latest=self.artifact_versions_to_keep,
                    protect_version_ids=(document.version.version_id,),
                )
            if self.acl_sync is not None:
                raw_acl = row.get("acl")
                try:
                    provider_snapshot = (
                        ExternalAclSnapshot.from_mapping(raw_acl)
                        if raw_acl is not None
                        else ExternalAclSnapshot(
                            complete=self.connector_type
                            not in _PROVIDER_ACL_CONNECTORS,
                            workspace_visible=self.connector_type
                            not in _PROVIDER_ACL_CONNECTORS,
                        )
                    )
                except Exception:
                    # Provider parsers are expected to emit validated ACLs,
                    # but a malformed persisted mapping must still retire the
                    # previous managed grants without blocking content commit.
                    provider_snapshot = ExternalAclSnapshot(complete=False)
                # A provider-wide grant describes the upstream audience; it
                # never overrides the connection administrator's explicit
                # workspace-sharing choice. Incomplete ACLs stay private even
                # if a malformed provider payload also claimed broad access.
                snapshot = replace(
                    provider_snapshot,
                    workspace_visible=(
                        provider_snapshot.complete
                        and provider_snapshot.workspace_visible
                        and self.workspace_visible
                    ),
                )
                self.acl_sync.apply(
                    tenant_id=self.tenant_id,
                    kb_id=self.kb_id,
                    document_id=build_document_id(str(row["filename"])),
                    source=str(row["filename"]),
                    owner_id=self.owner_id,
                    managed_by=f"connector:{self.connection_id}",
                    snapshot=snapshot,
                )
            heartbeat()
        self._publish_materialized_files()
        self.catalog.tombstone(self.tenant_id, self.kb_id, stale_source_ids)

    def _begin_stale_acl_retirements(self, document_ids: tuple[str, ...]) -> None:
        if not document_ids or not isinstance(self.acl_sync, ExternalAclSynchronizer):
            return
        self.acl_sync.access_store.begin_document_retirement(
            self.tenant_id,
            self.kb_id,
            f"connector:{self.connection_id}",
            document_ids,
        )

    def _finish_stale_acl_retirements(self) -> None:
        if not isinstance(self.acl_sync, ExternalAclSynchronizer):
            return
        managed_by = f"connector:{self.connection_id}"
        with kb_write_lock(self.kb_id):
            document_ids = self.acl_sync.access_store.retiring_document_ids(
                self.tenant_id,
                self.kb_id,
                managed_by,
            )
            if not document_ids:
                return
            # Checkpoint deletion precedes policy/fence deletion. A crash between
            # the two leaves the durable retirement fence available for an
            # idempotent recovery; the reverse order could orphan a checkpoint.
            self.acl_sync.state_store.delete_managed(
                self.tenant_id,
                self.kb_id,
                managed_by,
                document_ids,
            )
            self.acl_sync.access_store.finish_document_retirement(
                self.tenant_id,
                self.kb_id,
                managed_by,
                document_ids,
            )

    def _build_index(self, heartbeat: Callable[[], None]) -> None:
        deadline = time.monotonic() + self.index_timeout_seconds
        job_id = self._index_job_id
        submitted = False
        if job_id is None:
            job_id = self._submit_index_job()
            submitted = True
            if job_id is None:
                return
        status_reader = self.index_status_reader
        if status_reader is None:
            raise RuntimeError("connector index status reader is unavailable")
        while True:
            heartbeat()
            current = status_reader(job_id)
            if current is None:
                raise RuntimeError("connector index job disappeared")
            status = str(current.get("status") or "")
            if status == "succeeded":
                return
            if status in {"failed", "cancelled"}:
                if submitted:
                    raise RuntimeError("connector index job failed")
                job_id = self._submit_index_job()
                submitted = True
                if job_id is None:
                    return
                continue
            if status not in {"pending", "running"}:
                raise RuntimeError("connector index job status is invalid")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("connector index job timed out")
            time.sleep(min(0.1, remaining))

    def _submit_index_job(self) -> str | None:
        previous_job_id = self._index_job_id
        idempotency_key = (
            f"connector-sync:{self.job_id}:initial"
            if previous_job_id is None
            else f"connector-sync:{self.job_id}:after:{previous_job_id}"
        )
        job = (
            self.keyed_index_submitter(self.kb_id, idempotency_key)
            if self.keyed_index_submitter is not None
            else self.index_submitter(self.kb_id)
        )
        raw_job_id = job.get("job_id") if isinstance(job, Mapping) else None
        if not raw_job_id:
            if self.index_status_reader is not None:
                raise RuntimeError("connector index job was not accepted")
            return None
        if not isinstance(raw_job_id, str):
            raise RuntimeError("connector index job id is invalid")
        job_id = _safe_component(raw_job_id, "index_job_id")
        self._index_job_id = job_id
        # Persist acceptance before the first status read. Recovery reuses this
        # exact job rather than queueing duplicate index generations.
        self._write_journal("materialized")
        return job_id

    def _publish_materialized_files(self) -> None:
        if self.current is None:
            raise RuntimeError("sync sink has not begun")
        previous = (
            self._read_manifest(self.backup)
            if self.backup and self.backup.exists()
            else {}
        )
        old_names = {str(row.get("filename") or "") for row in previous.values()}
        new_names = {str(row.get("filename") or "") for row in self._rows.values()}
        documents = self._validate_publish_ownership(
            previous=previous,
            new_names=new_names,
        )
        for name in new_names:
            source = self.current / name
            target = self.source_dir / name
            temporary = target.with_suffix(target.suffix + f".{self.job_id}.tmp")
            with source.open("rb") as source_handle, temporary.open("wb") as handle:
                shutil.copyfileobj(source_handle, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        for name in old_names - new_names:
            if name:
                (self.source_dir / name).unlink(missing_ok=True)
        self._fsync_directory(self.source_dir)

        contract_path = self.source_dir / _SOURCE_CONTRACTS
        documents = {
            str(name): row
            for name, row in documents.items()
            if not (
                isinstance(row, Mapping)
                and isinstance(row.get("metadata"), Mapping)
                and row["metadata"].get("connection_id") == self.connection_id
            )
        }
        for row in self._rows.values():
            documents[str(row["filename"])] = row["document"]
        self._write_json_atomic(
            contract_path,
            {"schema_version": 1, "documents": documents},
        )

    def _validate_publish_ownership(
        self,
        *,
        previous: Mapping[str, Mapping[str, Any]] | None = None,
        new_names: set[str] | None = None,
    ) -> dict[str, Any]:
        """Reject replacing or deleting a top-level file we do not own."""

        contract_path = self.source_dir / _SOURCE_CONTRACTS
        try:
            payload = json.loads(contract_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            payload = {"schema_version": 1, "documents": {}}
        documents = payload.get("documents")
        if payload.get("schema_version") != 1 or not isinstance(documents, dict):
            raise ValueError("source contract sidecar is invalid")
        if previous is None:
            previous = (
                self._read_manifest(self.current)
                if self.current is not None and self.current.exists()
                else {}
            )
        if new_names is None:
            new_names = {str(row.get("filename") or "") for row in self._rows.values()}
        old_names = {str(row.get("filename") or "") for row in previous.values()}
        for name in new_names | old_names:
            if not name:
                continue
            contract = documents.get(name)
            metadata = (
                contract.get("metadata") if isinstance(contract, Mapping) else None
            )
            owner = (
                str(metadata.get("connection_id") or "")
                if isinstance(metadata, Mapping)
                else ""
            )
            target_exists = (self.source_dir / name).exists()
            if name in new_names:
                replayed_copy = False
                if (
                    target_exists
                    and contract is None
                    and owner == ""
                    and self.current is not None
                    and (self.current / name).is_file()
                    and self.journal is not None
                    and self.journal.exists()
                ):
                    try:
                        phase = str(
                            json.loads(self.journal.read_text(encoding="utf-8"))[
                                "phase"
                            ]
                        )
                        replayed_copy = phase == "swapped" and filecmp.cmp(
                            self.source_dir / name,
                            self.current / name,
                            shallow=False,
                        )
                    except (OSError, ValueError, KeyError, json.JSONDecodeError):
                        replayed_copy = False
                if (
                    (target_exists or contract is not None)
                    and owner != self.connection_id
                    and not replayed_copy
                ):
                    raise ValueError(
                        "connector materialization conflicts with an existing source"
                    )
            elif target_exists and owner != self.connection_id:
                raise ValueError(
                    "connector cannot delete a source owned by another producer"
                )
        return dict(documents)

    def _write_journal(self, phase: str) -> None:
        if self.journal is None:
            raise RuntimeError("sync sink has not begun")
        if phase not in {"prepared", "swapped", "materialized", "indexed"}:
            raise ValueError("sync commit journal phase is invalid")
        self._write_json_atomic(
            self.journal,
            {
                "phase": phase,
                "job_id": self.job_id,
                "connection_id": self.connection_id,
                "index_job_id": self._index_job_id,
            },
        )
        if self.commit_store is not None:
            self.commit_store.set_phase(self.job_id, phase, self._index_job_id)

    def _read_journal(self) -> str:
        if self.journal is None:
            raise RuntimeError("sync sink has not begun")
        payload = json.loads(self.journal.read_text(encoding="utf-8"))
        if not isinstance(payload, Mapping):
            raise ValueError("sync commit journal is invalid")
        phase = payload.get("phase")
        if phase not in {"prepared", "swapped", "materialized", "indexed"}:
            raise ValueError("sync commit journal phase is invalid")
        if payload.get("job_id") != self.job_id:
            raise ValueError("sync commit journal job does not match")
        if payload.get("connection_id") != self.connection_id:
            raise ValueError("sync commit journal connection does not match")
        raw_index_job_id = payload.get("index_job_id")
        if raw_index_job_id is None:
            self._index_job_id = None
        elif isinstance(raw_index_job_id, str):
            self._index_job_id = _safe_component(raw_index_job_id, "index_job_id")
        else:
            raise ValueError("sync commit journal index job is invalid")
        return str(phase)

    def _write_manifest(self, directory: Path) -> None:
        self._write_json_atomic(
            directory / _MANIFEST,
            {"schema_version": 1, "sources": self._rows},
        )

    @staticmethod
    def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
        temporary = path.with_name(path.name + ".tmp")
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
        with temporary.open("wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        # Persist the rename that forms the filesystem side of the durable
        # handoff. Some platforms/filesystems do not support directory fsync;
        # the file itself remains fsynced there.
        MaterializedSyncSink._fsync_directory(path.parent)

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        try:
            directory_fd = os.open(directory, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(directory_fd)
        except OSError:
            pass
        finally:
            os.close(directory_fd)

    @staticmethod
    def _read_manifest(
        directory: Path, *, required: bool = False
    ) -> dict[str, dict[str, Any]]:
        path = directory / _MANIFEST
        if path.is_symlink():
            raise ValueError(
                "connection materialization manifest must not be a symlink"
            )
        if not path.exists():
            if required:
                raise ValueError("connection materialization manifest is missing")
            return {}
        if not path.is_file():
            raise ValueError(
                "connection materialization manifest is not a regular file"
            )
        payload = json.loads(path.read_text(encoding="utf-8"))
        rows = payload.get("sources")
        if payload.get("schema_version") != 1 or not isinstance(rows, dict):
            raise ValueError("connection materialization manifest is invalid")
        if any(not isinstance(value, dict) for value in rows.values()):
            raise ValueError("connection materialization manifest is invalid")
        return {str(key): dict(value) for key, value in rows.items()}
