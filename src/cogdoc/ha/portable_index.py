from __future__ import annotations

import hashlib
import fcntl
import json
import math
import os
import re
import sqlite3
import struct
import threading
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import closing
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from cogdoc.graph.state import DocMeta, RetrievedDoc


PORTABLE_INDEX_FORMAT = "cogdoc-portable-index-v1"
PORTABLE_INDEX_FILENAME = "portable-index.sqlite"
_MAX_CHUNKS = 10_000_000
_MAX_DIMENSIONS = 1_000_000
_MAX_TEXT_BYTES = 16 * 1024 * 1024
_MAX_METADATA_BYTES = 4 * 1024 * 1024
_READ_BATCH = 1000
_MAX_ARTIFACT_BYTES = 64 * 1024 * 1024 * 1024
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_REQUIRED_META_TEXT = ("chunk_id", "document_id", "source", "origin")
_REQUIRED_META_INT = (
    "local_chunk_index",
    "chunk_index",
    "page",
    "page_start",
    "page_end",
)
_OPTIONAL_META_TEXT = (
    "context",
    "source_type",
    "knowledge_id",
    "parent_chunk_id",
    "section_title",
    "section_path",
    "chunk_type",
    "document_profile",
    "chunking_strategy_version",
    "sheet",
    "cell_range",
    "source_id",
    "source_version_id",
    "media_type",
    "origin_uri",
    "connector_type",
)
_OPTIONAL_META_INT = (
    "section_level",
    "child_index_in_parent",
    "parent_child_count",
    "parent_char_count",
    "chunk_char_count",
    "line_start",
    "line_end",
    "slide",
    "image",
)
_OPTIONAL_META_FLOAT = ("score", "chunk_quality_score")


class PortableIndexError(RuntimeError):
    pass


class PortableIndexIntegrityError(PortableIndexError):
    pass


@dataclass(frozen=True)
class PortableIndexMetadata:
    embedding_model: str
    dimensions: int
    chunk_version: str
    expected_count: int

    def contract(self) -> dict[str, Any]:
        return {
            "embedding_model": self.embedding_model,
            "dimensions": self.dimensions,
            "chunk_version": self.chunk_version,
            "portable_format": PORTABLE_INDEX_FORMAT,
            "expected_count": self.expected_count,
        }


def _clean_text(value: Any, field: str, *, maximum: int = 1024) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode()) > maximum
        or any(ord(character) < 32 and character not in "\t\n\r" for character in value)
    ):
        raise ValueError(f"portable index {field} is invalid")
    return value


def _canonical(value: Any, field: str, maximum: int) -> bytes:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    except (TypeError, ValueError) as exc:
        raise ValueError(f"portable index {field} must be finite JSON") from exc
    if len(encoded) > maximum:
        raise ValueError(f"portable index {field} is too large")
    return encoded


def _normalize_doc_meta(value: Any) -> DocMeta:
    if not isinstance(value, Mapping):
        raise ValueError("portable index document metadata is invalid")
    normalized = dict(value)
    for field in _REQUIRED_META_TEXT:
        normalized[field] = _clean_text(normalized.get(field), field, maximum=4096)
    source_sha256 = normalized.get("source_sha256")
    if not isinstance(source_sha256, str) or _SHA256.fullmatch(source_sha256) is None:
        raise ValueError("portable index source_sha256 is invalid")
    for field in _REQUIRED_META_INT:
        item = normalized.get(field)
        if type(item) is not int or item < 0:
            raise ValueError(f"portable index {field} is invalid")
    if not (normalized["page_start"] <= normalized["page"] <= normalized["page_end"]):
        raise ValueError("portable index page range is invalid")
    for field in _OPTIONAL_META_TEXT:
        if field in normalized:
            normalized[field] = _clean_text(
                normalized[field], field, maximum=_MAX_METADATA_BYTES
            )
    for field in _OPTIONAL_META_INT:
        if field in normalized and type(normalized[field]) is not int:
            raise ValueError(f"portable index {field} is invalid")
    for field in _OPTIONAL_META_FLOAT:
        if field not in normalized:
            continue
        item = normalized[field]
        if (
            isinstance(item, bool)
            or not isinstance(item, (int, float))
            or not math.isfinite(float(item))
        ):
            raise ValueError(f"portable index {field} is invalid")
        normalized[field] = float(item)
    if "source_location" in normalized and not isinstance(
        normalized["source_location"], Mapping
    ):
        raise ValueError("portable index source_location is invalid")
    if "source_locations" in normalized:
        locations = normalized["source_locations"]
        if not isinstance(locations, list) or not all(
            isinstance(item, Mapping) for item in locations
        ):
            raise ValueError("portable index source_locations is invalid")
    return cast(DocMeta, normalized)


def _normalize_document(value: Mapping[str, Any]) -> tuple[str, bytes, str]:
    if not isinstance(value, Mapping):
        raise TypeError("portable index document must be an object")
    text = value.get("text")
    metadata = value.get("meta")
    if not isinstance(text, str) or len(text.encode()) > _MAX_TEXT_BYTES:
        raise ValueError("portable index document text is invalid")
    normalized_metadata = _normalize_doc_meta(metadata)
    chunk_id = normalized_metadata["chunk_id"]
    metadata_bytes = _canonical(normalized_metadata, "metadata", _MAX_METADATA_BYTES)
    return text, metadata_bytes, chunk_id


def _embedding_bytes(values: Sequence[float], dimensions: int) -> bytes:
    if len(values) != dimensions:
        raise ValueError("portable index embedding dimensions do not match")
    normalized: list[float] = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("portable index embedding contains a non-number")
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("portable index embedding contains a non-finite number")
        normalized.append(number)
    return struct.pack(f"<{dimensions}f", *normalized)


def _decode_embedding(value: bytes, dimensions: int) -> list[float]:
    if not isinstance(value, bytes) or len(value) != dimensions * 4:
        raise PortableIndexIntegrityError("portable index embedding size is invalid")
    decoded = list(struct.unpack(f"<{dimensions}f", value))
    if not all(math.isfinite(number) for number in decoded):
        raise PortableIndexIntegrityError(
            "portable index embedding contains a non-finite number"
        )
    return decoded


def _row_digest(chunk_id: str, text: str, metadata: bytes, embedding: bytes) -> str:
    builder = hashlib.sha256()
    for value in (chunk_id.encode(), text.encode(), metadata, embedding):
        builder.update(len(value).to_bytes(8, "big"))
        builder.update(value)
    return builder.hexdigest()


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@contextmanager
def _installation_lock(path: Path) -> Iterator[None]:
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


class PortableIndexStore:
    """Strict, non-pickle serialization for cross-node retrieval generations."""

    def write(
        self,
        path: str | os.PathLike[str],
        documents: Sequence[Mapping[str, Any]],
        embeddings: Sequence[Sequence[float]],
        *,
        embedding_model: str,
        dimensions: int,
        chunk_version: str,
    ) -> PortableIndexMetadata:
        target = Path(path)
        if target.is_symlink():
            raise ValueError("portable index target cannot be a symlink")
        if target.exists():
            raise FileExistsError(target)
        if len(documents) != len(embeddings) or len(documents) > _MAX_CHUNKS:
            raise ValueError("portable index document/embedding count is invalid")
        if type(dimensions) is not int or not 1 <= dimensions <= _MAX_DIMENSIONS:
            raise ValueError("portable index dimensions are invalid")
        embedding_model = _clean_text(embedding_model, "embedding_model")
        chunk_version = _clean_text(chunk_version, "chunk_version")
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = target.parent / f".tmp-{target.name}-{uuid.uuid4().hex}"
        metadata = PortableIndexMetadata(
            embedding_model,
            dimensions,
            chunk_version,
            len(documents),
        )
        seen: set[str] = set()
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(temporary)
            connection.execute("PRAGMA journal_mode=DELETE")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA trusted_schema=OFF")
            connection.execute(
                "CREATE TABLE metadata(format TEXT NOT NULL,embedding_model TEXT NOT NULL,"
                "dimensions INTEGER NOT NULL,chunk_version TEXT NOT NULL,"
                "expected_count INTEGER NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE chunks(ordinal INTEGER PRIMARY KEY,chunk_id TEXT NOT NULL UNIQUE,"
                "text TEXT NOT NULL,metadata_json BLOB NOT NULL,embedding BLOB NOT NULL,"
                "row_sha256 TEXT NOT NULL)"
            )
            connection.execute(
                "INSERT INTO metadata VALUES(?,?,?,?,?)",
                (
                    PORTABLE_INDEX_FORMAT,
                    embedding_model,
                    dimensions,
                    chunk_version,
                    len(documents),
                ),
            )
            for ordinal, (document, embedding) in enumerate(
                zip(documents, embeddings, strict=True)
            ):
                text, metadata_bytes, chunk_id = _normalize_document(document)
                if chunk_id in seen:
                    raise ValueError("portable index chunk_id is duplicated")
                seen.add(chunk_id)
                vector = _embedding_bytes(embedding, dimensions)
                connection.execute(
                    "INSERT INTO chunks VALUES(?,?,?,?,?,?)",
                    (
                        ordinal,
                        chunk_id,
                        text,
                        metadata_bytes,
                        vector,
                        _row_digest(chunk_id, text, metadata_bytes, vector),
                    ),
                )
            connection.commit()
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            connection.close()
            connection = None
            descriptor = os.open(temporary, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.link(temporary, target)
            _fsync_directory(target.parent)
            return metadata
        except sqlite3.Error as exc:
            raise PortableIndexError("portable index write failed") from exc
        finally:
            if connection is not None:
                connection.close()
            temporary.unlink(missing_ok=True)

    def metadata(self, path: str | os.PathLike[str]) -> PortableIndexMetadata:
        with closing(self._open(path)) as connection:
            return self._read_metadata(connection)

    def verify(self, path: str | os.PathLike[str]) -> PortableIndexMetadata:
        with closing(self._open(path)) as connection:
            metadata = self._read_metadata(connection)
            count = 0
            cursor = connection.execute(
                "SELECT ordinal,chunk_id,text,metadata_json,embedding,row_sha256 "
                "FROM chunks ORDER BY ordinal"
            )
            while rows := cursor.fetchmany(_READ_BATCH):
                for row in rows:
                    self._verify_row(row, count, metadata.dimensions)
                    count += 1
            if count != metadata.expected_count:
                raise PortableIndexIntegrityError(
                    "portable index row count does not match metadata"
                )
            return metadata

    def load(
        self, path: str | os.PathLike[str]
    ) -> tuple[PortableIndexMetadata, list[RetrievedDoc], list[list[float]]]:
        documents: list[RetrievedDoc] = []
        embeddings: list[list[float]] = []
        with closing(self._open(path)) as connection:
            metadata = self._read_metadata(connection)
            cursor = connection.execute(
                "SELECT ordinal,chunk_id,text,metadata_json,embedding,row_sha256 "
                "FROM chunks ORDER BY ordinal"
            )
            expected_ordinal = 0
            while rows := cursor.fetchmany(_READ_BATCH):
                for row in rows:
                    metadata_value, vector = self._verify_row(
                        row, expected_ordinal, metadata.dimensions
                    )
                    documents.append({"text": str(row["text"]), "meta": metadata_value})
                    embeddings.append(vector)
                    expected_ordinal += 1
            if expected_ordinal != metadata.expected_count:
                raise PortableIndexIntegrityError(
                    "portable index row count does not match metadata"
                )
        return metadata, documents, embeddings

    @staticmethod
    def _open(path: str | os.PathLike[str]) -> sqlite3.Connection:
        supplied = Path(path)
        if supplied.is_symlink():
            raise PortableIndexIntegrityError("portable index path is unsafe")
        try:
            resolved = supplied.resolve(strict=True)
        except FileNotFoundError as exc:
            raise PortableIndexIntegrityError("portable index is unavailable") from exc
        if not resolved.is_file():
            raise PortableIndexIntegrityError("portable index is not a regular file")
        if resolved.stat().st_size > _MAX_ARTIFACT_BYTES:
            raise PortableIndexIntegrityError("portable index file is too large")
        try:
            connection = sqlite3.connect(
                f"file:{resolved.as_posix()}?mode=ro&immutable=1",
                uri=True,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            connection.execute("PRAGMA trusted_schema=OFF")
            objects = connection.execute(
                "SELECT name,type FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' "
                "ORDER BY name,type"
            ).fetchall()
            if [(row["name"], row["type"]) for row in objects] != [
                ("chunks", "table"),
                ("metadata", "table"),
            ]:
                raise PortableIndexIntegrityError("portable index schema is invalid")
            expected_columns = {
                "chunks": [
                    "ordinal",
                    "chunk_id",
                    "text",
                    "metadata_json",
                    "embedding",
                    "row_sha256",
                ],
                "metadata": [
                    "format",
                    "embedding_model",
                    "dimensions",
                    "chunk_version",
                    "expected_count",
                ],
            }
            for table, expected in expected_columns.items():
                columns = [
                    str(row["name"])
                    for row in connection.execute(f"PRAGMA table_info({table})")
                ]
                if columns != expected:
                    raise PortableIndexIntegrityError(
                        "portable index schema is invalid"
                    )
            return connection
        except sqlite3.Error as exc:
            raise PortableIndexIntegrityError(
                "portable index cannot be opened"
            ) from exc

    @staticmethod
    def _read_metadata(connection: sqlite3.Connection) -> PortableIndexMetadata:
        try:
            rows = connection.execute("SELECT * FROM metadata").fetchall()
        except sqlite3.Error as exc:
            raise PortableIndexIntegrityError(
                "portable index metadata is invalid"
            ) from exc
        if len(rows) != 1:
            raise PortableIndexIntegrityError("portable index metadata row is invalid")
        row = rows[0]
        if row["format"] != PORTABLE_INDEX_FORMAT:
            raise PortableIndexIntegrityError("portable index format is unsupported")
        try:
            embedding_model = _clean_text(row["embedding_model"], "embedding_model")
            chunk_version = _clean_text(row["chunk_version"], "chunk_version")
        except ValueError as exc:
            raise PortableIndexIntegrityError(str(exc)) from exc
        dimensions = row["dimensions"]
        expected_count = row["expected_count"]
        if (
            type(dimensions) is not int
            or not 1 <= dimensions <= _MAX_DIMENSIONS
            or type(expected_count) is not int
            or not 0 <= expected_count <= _MAX_CHUNKS
        ):
            raise PortableIndexIntegrityError(
                "portable index metadata bounds are invalid"
            )
        return PortableIndexMetadata(
            embedding_model, dimensions, chunk_version, expected_count
        )

    @staticmethod
    def _verify_row(
        row: sqlite3.Row, expected_ordinal: int, dimensions: int
    ) -> tuple[DocMeta, list[float]]:
        if row["ordinal"] != expected_ordinal:
            raise PortableIndexIntegrityError(
                "portable index ordinals are not contiguous"
            )
        chunk_id = row["chunk_id"]
        text = row["text"]
        raw_metadata = row["metadata_json"]
        raw_embedding = row["embedding"]
        digest = row["row_sha256"]
        if (
            not isinstance(chunk_id, str)
            or not isinstance(text, str)
            or not isinstance(raw_metadata, bytes)
            or not isinstance(raw_embedding, bytes)
            or not isinstance(digest, str)
            or len(text.encode()) > _MAX_TEXT_BYTES
            or _row_digest(chunk_id, text, raw_metadata, raw_embedding) != digest
        ):
            raise PortableIndexIntegrityError("portable index row checksum is invalid")
        try:
            decoded = json.loads(raw_metadata)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PortableIndexIntegrityError(
                "portable index document metadata is invalid"
            ) from exc
        if not isinstance(decoded, dict) or decoded.get("chunk_id") != chunk_id:
            raise PortableIndexIntegrityError(
                "portable index chunk identity is invalid"
            )
        try:
            normalized = _normalize_doc_meta(decoded)
            canonical = _canonical(normalized, "metadata", _MAX_METADATA_BYTES)
        except ValueError as exc:
            raise PortableIndexIntegrityError(str(exc)) from exc
        if canonical != raw_metadata:
            raise PortableIndexIntegrityError(
                "portable index metadata is not canonical"
            )
        return normalized, _decode_embedding(raw_embedding, dimensions)


class PortableIndexInstaller:
    """Idempotently materialize a verified portable generation into local engines."""

    _locks: dict[str, threading.Lock] = {}
    _locks_guard = threading.Lock()

    @classmethod
    def _lock_for(cls, collection_id: str) -> threading.Lock:
        with cls._locks_guard:
            return cls._locks.setdefault(collection_id, threading.Lock())

    def install(
        self,
        kb_id: str,
        generation_id: str,
        portable_path: str | os.PathLike[str],
    ) -> Any:
        from cogdoc.config.settings import get_settings
        from cogdoc.tools.embedder import (
            embedding_contract,
            resolve_embedder,
        )
        from cogdoc.tools.retriever.base_retriever import NullRetriever
        from cogdoc.tools.retriever.bm25_retriever import BM25Retriever
        from cogdoc.tools.retriever.hybrid import HybridRetriever
        from cogdoc.tools.retriever.vector_retriever import VectorRetriever

        kb_id = _clean_text(kb_id, "kb_id", maximum=255)
        generation_id = _clean_text(generation_id, "generation_id", maximum=255)
        portable_path = Path(portable_path)
        metadata, documents, embeddings = PortableIndexStore().load(portable_path)
        try:
            embedder = resolve_embedder(metadata.embedding_model)
            expected_contract = embedding_contract(embedder)
            expected_dimensions = int(
                getattr(embedder, "EMBEDDING_DIM", 0)
                or embedder.embedding_dim()
            )
        except (RuntimeError, ValueError) as exc:
            raise PortableIndexIntegrityError(
                "portable index embedding contract is incompatible or unavailable"
            ) from exc
        if (
            metadata.embedding_model != expected_contract
            or metadata.dimensions != expected_dimensions
        ):
            raise PortableIndexIntegrityError(
                "portable index embedding contract is incompatible"
            )
        collection_id = get_settings().kb_collection_id(kb_id, generation_id)
        marker = (
            portable_path.parent.parent / f".installed-{portable_path.parent.name}.json"
        )
        lock_path = (
            portable_path.parent.parent / f".install-{portable_path.parent.name}.lock"
        )
        expected_marker = {
            "format": PORTABLE_INDEX_FORMAT,
            "generation_id": generation_id,
            "collection_id": collection_id,
            "expected_count": metadata.expected_count,
            "portable_sha256": _file_sha256(portable_path),
        }
        with self._lock_for(collection_id), _installation_lock(lock_path):
            vector = VectorRetriever(collection_id=collection_id, embedder=embedder)
            bm25 = BM25Retriever(collection_id=collection_id)
            if self._marker_matches(marker, expected_marker):
                engine = HybridRetriever(vector, bm25)
                if self._consistent(engine, metadata.expected_count):
                    return engine
            try:
                vector.clear()
                bm25.clear()
                if documents:
                    vector.add_with_embeddings(documents, embeddings)
                    bm25.index(documents)
                engine = HybridRetriever(vector, bm25)
                if not self._consistent(engine, metadata.expected_count):
                    raise PortableIndexIntegrityError(
                        "installed retrieval engines are inconsistent"
                    )
                self._write_marker(marker, expected_marker)
                if metadata.expected_count == 0:
                    return HybridRetriever(NullRetriever(), NullRetriever())
                return engine
            except Exception:
                try:
                    vector.clear()
                except Exception:
                    pass
                try:
                    bm25.clear()
                except Exception:
                    pass
                marker.unlink(missing_ok=True)
                raise

    @staticmethod
    def _consistent(engine: Any, expected_count: int) -> bool:
        if engine.count() != expected_count:
            return False
        if expected_count == 0:
            return engine.vector_retriever.count() == 0
        return engine.is_consistent()

    @staticmethod
    def _marker_matches(path: Path, expected: Mapping[str, Any]) -> bool:
        try:
            with path.open(encoding="utf-8") as handle:
                value = json.load(handle)
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return False
        return value == expected

    @staticmethod
    def _write_marker(path: Path, value: Mapping[str, Any]) -> None:
        encoded = _canonical(dict(value), "installation marker", 4096)
        temporary = path.parent / f".tmp-installed-{uuid.uuid4().hex}"
        try:
            with temporary.open("xb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            _fsync_directory(path.parent)
        finally:
            temporary.unlink(missing_ok=True)


def _file_sha256(path: Path) -> str:
    builder = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            builder.update(chunk)
    return builder.hexdigest()


def export_retrieval_generation(
    kb_id: str,
    generation_id: str,
    destination: str | os.PathLike[str],
    *,
    embedding_model: str,
    dimensions: int,
    chunk_version: str,
) -> PortableIndexMetadata:
    """Export one already-built local generation without activating another one."""

    from cogdoc.config.settings import get_settings
    from cogdoc.tools.embedder import resolve_embedder
    from cogdoc.tools.retriever.bm25_retriever import BM25Retriever
    from cogdoc.tools.retriever.vector_retriever import VectorRetriever

    collection_id = get_settings().kb_collection_id(kb_id, generation_id)
    registry = BM25Retriever(collection_id=collection_id).export_registry()
    embedder = resolve_embedder(embedding_model)
    embeddings_by_id = VectorRetriever(
        collection_id=collection_id, embedder=embedder
    ).embeddings_by_chunk_id()
    chunk_ids = [
        str(document.get("meta", {}).get("chunk_id", "")) for document in registry
    ]
    if len(set(chunk_ids)) != len(chunk_ids) or set(chunk_ids) != set(embeddings_by_id):
        raise PortableIndexIntegrityError(
            "local vector and BM25 generations are inconsistent"
        )
    embeddings = [embeddings_by_id[chunk_id] for chunk_id in chunk_ids]
    return PortableIndexStore().write(
        Path(destination) / PORTABLE_INDEX_FILENAME,
        registry,
        embeddings,
        embedding_model=embedding_model,
        dimensions=dimensions,
        chunk_version=chunk_version,
    )


__all__ = [
    "PORTABLE_INDEX_FILENAME",
    "PORTABLE_INDEX_FORMAT",
    "PortableIndexError",
    "PortableIndexIntegrityError",
    "PortableIndexInstaller",
    "PortableIndexMetadata",
    "PortableIndexStore",
    "export_retrieval_generation",
]
