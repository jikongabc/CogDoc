from __future__ import annotations

import json
import os
import shutil
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from cogdoc.connectors.base import RetryableConnectorError
from cogdoc.service.external_acl import ExternalAclSnapshot, ExternalAclSynchronizer
from cogdoc.service.kb_locks import kb_write_lock
from cogdoc.service.source_catalog import SourceCatalog
from cogdoc.source_model import SourceDocument
from cogdoc.tools.chunk_identity import build_document_id
from cogdoc.tools.source_parser import SUPPORTED_EXTENSIONS


_MANIFEST = ".cogdoc-connection.json"
_SOURCE_CONTRACTS = ".cogdoc-source-contracts.json"


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
    stem = Path(display).stem[:120] or "source"
    return f"{stem}--{document.source_id.removeprefix('src-')[:12]}{suffix}"


class MaterializedSyncSink:
    """Crash-recoverable directory swap plus catalog, ACL, and index commit."""

    def __init__(
        self,
        *,
        source_dir: str,
        catalog: SourceCatalog,
        index_submitter: Callable[[str], Mapping[str, Any]],
        index_status_reader: Callable[[str], Mapping[str, Any] | None] | None = None,
        owner_id: str,
        workspace_visible: bool,
        acl_sync: ExternalAclSynchronizer | None = None,
    ) -> None:
        self.source_dir = Path(source_dir).resolve()
        self.catalog = catalog
        self.index_submitter = index_submitter
        self.index_status_reader = index_status_reader
        self.owner_id = owner_id
        self.workspace_visible = workspace_visible
        self.acl_sync = acl_sync
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

    def begin(
        self,
        *,
        job_id: str,
        tenant_id: str,
        kb_id: str,
        connection_id: str,
        connector_type: str,
        attempt: int,
    ) -> None:
        del attempt
        self.job_id = _safe_component(job_id, "job_id")
        self.tenant_id = _safe_component(tenant_id, "tenant_id")
        self.kb_id = _safe_component(kb_id, "kb_id")
        self.connection_id = _safe_component(connection_id, "connection_id")
        self.connector_type = _safe_component(connector_type, "connector_type")
        self.source_dir.mkdir(parents=True, exist_ok=True)
        connection_root = self.source_dir / ".connections"
        connection_root.mkdir(exist_ok=True)
        work_root = self.source_dir.parent / f".{self.source_dir.name}.sync-work"
        work_root.mkdir(exist_ok=True)
        self.current = connection_root / self.connection_id
        self.staging = work_root / f"{self.connection_id}-{self.job_id}.staging"
        self.backup = work_root / f"{self.connection_id}-{self.job_id}.backup"
        self.journal = work_root / f"{self.connection_id}-{self.job_id}.journal.json"
        if self.journal.exists():
            return
        if self.staging.exists():
            shutil.rmtree(self.staging)
        if self.current.exists():
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
        self._write_manifest(self.staging)

    def delete(self, external_id: str) -> None:
        if self.staging is None:
            raise RuntimeError("sync sink has not begun")
        row = self._rows.pop(external_id, None)
        if row:
            (self.staging / str(row["filename"])).unlink(missing_ok=True)
            self._write_manifest(self.staging)

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
        if snapshot:
            for external_id in tuple(self._rows):
                if external_id not in seen_external_ids:
                    self.delete(external_id)
        self._write_journal("prepared")
        try:
            heartbeat()
            with kb_write_lock(self.kb_id):
                if backup.exists():
                    shutil.rmtree(backup)
                if current.exists():
                    os.replace(current, backup)
                os.replace(staging, current)
                self._write_journal("swapped")
                self._apply_side_effects(heartbeat)
                self._write_journal("materialized")
            self._build_index(heartbeat)
            self._write_journal("indexed")
        except Exception as exc:
            raise RetryableConnectorError("materialized sync commit failed") from exc

    def recover_commit(self, *, heartbeat: Callable[[], None]) -> None:
        if self.journal is None or self.current is None or self.staging is None:
            raise RuntimeError("sync sink has not begun")
        if not self.journal.exists():
            raise RetryableConnectorError("sync commit journal is missing")
        phase = str(json.loads(self.journal.read_text(encoding="utf-8"))["phase"])
        with kb_write_lock(self.kb_id):
            if phase == "prepared" and self.staging.exists():
                if self.backup and self.backup.exists():
                    shutil.rmtree(self.backup)
                if self.current.exists() and self.backup:
                    os.replace(self.current, self.backup)
                os.replace(self.staging, self.current)
                self._write_journal("swapped")
                phase = "swapped"
            if phase in {"prepared", "swapped"}:
                self._apply_side_effects(heartbeat)
                self._write_journal("materialized")
                phase = "materialized"
        if phase == "materialized":
            self._build_index(heartbeat)
            self._write_journal("indexed")

    def finalize(self) -> None:
        for path in (self.staging, self.backup):
            if path and path.exists():
                shutil.rmtree(path)
        if self.journal:
            self.journal.unlink(missing_ok=True)

    def abort(self) -> None:
        if self.journal and self.journal.exists():
            return
        if self.staging and self.staging.exists():
            shutil.rmtree(self.staging)

    def _apply_side_effects(self, heartbeat: Callable[[], None]) -> None:
        if self.current is None:
            raise RuntimeError("sync sink has not begun")
        self._rows = self._read_manifest(self.current)
        documents: list[SourceDocument] = []
        for row in self._rows.values():
            document = SourceDocument.from_manifest_document(row["document"])
            documents.append(document)
            self.catalog.upsert(self.tenant_id, self.kb_id, document)
            if self.acl_sync is not None:
                raw_acl = row.get("acl")
                snapshot = (
                    ExternalAclSnapshot.from_mapping(raw_acl)
                    if raw_acl is not None
                    else ExternalAclSnapshot(
                        complete=True, workspace_visible=self.workspace_visible
                    )
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
        desired = {document.source_id for document in documents}
        stale = [
            row["source_id"]
            for row in self.catalog.list_sources(self.tenant_id, self.kb_id)
            if row.get("metadata", {}).get("connection_id") == self.connection_id
            and row["source_id"] not in desired
        ]
        self._publish_materialized_files()
        self.catalog.tombstone(self.tenant_id, self.kb_id, stale)

    def _build_index(self, heartbeat: Callable[[], None]) -> None:
        job = self.index_submitter(self.kb_id)
        job_id = str(job.get("job_id") or "") if isinstance(job, Mapping) else ""
        if not job_id or self.index_status_reader is None:
            return
        while True:
            heartbeat()
            current = self.index_status_reader(job_id)
            if current is None:
                raise RuntimeError("connector index job disappeared")
            status = str(current.get("status") or "")
            if status == "succeeded":
                return
            if status in {"failed", "cancelled"}:
                raise RuntimeError("connector index job failed")
            time.sleep(0.1)

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
        for name in new_names:
            source = self.current / name
            target = self.source_dir / name
            temporary = target.with_suffix(target.suffix + f".{self.job_id}.tmp")
            shutil.copy2(source, temporary)
            os.replace(temporary, target)
        for name in old_names - new_names:
            if name:
                (self.source_dir / name).unlink(missing_ok=True)

        contract_path = self.source_dir / _SOURCE_CONTRACTS
        try:
            payload = json.loads(contract_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            payload = {"schema_version": 1, "documents": {}}
        documents = payload.get("documents", {})
        if payload.get("schema_version") != 1 or not isinstance(documents, dict):
            raise ValueError("source contract sidecar is invalid")
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
        temporary = contract_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                {"schema_version": 1, "documents": documents},
                ensure_ascii=False,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        os.replace(temporary, contract_path)

    def _write_journal(self, phase: str) -> None:
        if self.journal is None:
            raise RuntimeError("sync sink has not begun")
        temporary = self.journal.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                {
                    "phase": phase,
                    "job_id": self.job_id,
                    "connection_id": self.connection_id,
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        os.replace(temporary, self.journal)

    def _write_manifest(self, directory: Path) -> None:
        temporary = directory / (_MANIFEST + ".tmp")
        temporary.write_text(
            json.dumps(
                {"schema_version": 1, "sources": self._rows},
                ensure_ascii=False,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        os.replace(temporary, directory / _MANIFEST)

    @staticmethod
    def _read_manifest(directory: Path) -> dict[str, dict[str, Any]]:
        path = directory / _MANIFEST
        if not path.exists():
            return {}
        payload = json.loads(path.read_text(encoding="utf-8"))
        rows = payload.get("sources")
        if payload.get("schema_version") != 1 or not isinstance(rows, dict):
            raise ValueError("connection materialization manifest is invalid")
        return {
            str(key): dict(value)
            for key, value in rows.items()
            if isinstance(value, dict)
        }
