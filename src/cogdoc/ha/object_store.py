from __future__ import annotations

import base64
import contextlib
import fcntl
import hashlib
import json
import os
import re
import shutil
import tempfile
import uuid
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from cogdoc.ha.index_generation import IndexIntegrityError, normalize_manifest


_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_READ_CHUNK = 1024 * 1024
_MULTIPART_CHUNK = 16 * 1024 * 1024
_MAX_IN_MEMORY = 16 * 1024 * 1024


class ObjectStoreError(RuntimeError):
    pass


class ObjectNotFound(ObjectStoreError):
    pass


class ObjectConflict(ObjectStoreError):
    pass


class ObjectIntegrityError(ObjectStoreError):
    pass


@dataclass(frozen=True)
class ObjectInfo:
    key: str
    byte_size: int
    sha256: str
    etag: str | None = None
    version_id: str | None = None


class ObjectStore(Protocol):
    def put_file(self, key: str, source: Path, *, sha256: str) -> ObjectInfo: ...

    def put_bytes(self, key: str, content: bytes, *, sha256: str) -> ObjectInfo: ...

    def head(self, key: str) -> ObjectInfo | None: ...

    def iter_bytes(self, key: str) -> Iterator[bytes]: ...

    def delete(self, key: str) -> None: ...

    def list_prefix(self, prefix: str) -> Iterable[ObjectInfo]: ...

    def check(self) -> bool: ...


def _key(value: str, *, allow_empty: bool = False) -> str:
    if (
        not isinstance(value, str)
        or (not value and not allow_empty)
        or len(value.encode()) > 2048
    ):
        raise ValueError("object key is invalid")
    if not value and allow_empty:
        return ""
    if allow_empty and value.endswith("/"):
        base = value[:-1]
        if not base or base.endswith("/"):
            raise ValueError("object key prefix is unsafe")
        _key(base)
        return value
    parsed = PurePosixPath(value)
    if (
        parsed.is_absolute()
        or value != parsed.as_posix()
        or "\\" in value
        or any(part in {"", ".", ".."} for part in parsed.parts)
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("object key is unsafe")
    return value


def _digest(value: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError("object sha256 is invalid")
    return value


def _hash_file(path: Path) -> tuple[str, int]:
    builder = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_READ_CHUNK), b""):
            builder.update(chunk)
            size += len(chunk)
    return builder.hexdigest(), size


@contextlib.contextmanager
def _materialization_lock(parent: Path, target: Path) -> Iterator[None]:
    lock_name = _materialization_namespace(target)
    lock_path = parent / f".materialize-{lock_name}.lock"
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _materialization_namespace(target: Path) -> str:
    return hashlib.sha256(str(target.absolute()).encode()).hexdigest()


def _remove_materialization_path(path: Path) -> None:
    """Remove only a cache entry already isolated under an internal name."""

    if path.is_symlink() or not path.is_dir():
        path.unlink(missing_ok=True)
    else:
        shutil.rmtree(path)


def _scavenge_materialization_remnants(parent: Path, target: Path) -> None:
    """Clean crash leftovers while the target-specific process lock is held."""

    namespace = _materialization_namespace(target)
    prefixes = (f".tmp-materialize-{namespace}-", f".corrupt-{namespace}-")
    changed = False
    for candidate in parent.iterdir():
        if not candidate.name.startswith(prefixes):
            continue
        _remove_materialization_path(candidate)
        changed = True
    if changed:
        LocalObjectStore._fsync_directory(parent)


class LocalObjectStore:
    """Filesystem implementation with the same immutable-key contract as S3."""

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _path(self, key: str) -> Path:
        return self.root.joinpath(*PurePosixPath(_key(key)).parts)

    def put_file(self, key: str, source: Path, *, sha256: str) -> ObjectInfo:
        key = _key(key)
        sha256 = _digest(sha256)
        supplied_source = Path(source)
        if supplied_source.is_symlink():
            raise ObjectIntegrityError("object source is not a regular file")
        source = supplied_source.resolve(strict=True)
        if not source.is_file():
            raise ObjectIntegrityError("object source is not a regular file")
        actual, size = _hash_file(source)
        if actual != sha256:
            raise ObjectIntegrityError("object source hash does not match")
        target = self._path(key)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        existing = self.head(key)
        if existing is not None:
            self._match(existing, size, sha256)
            return existing
        temporary = target.parent / f".tmp-{target.name}-{uuid.uuid4().hex}"
        try:
            with source.open("rb") as reader, temporary.open("xb") as writer:
                shutil.copyfileobj(reader, writer, _READ_CHUNK)
                writer.flush()
                os.fsync(writer.fileno())
            try:
                os.link(temporary, target)
            except FileExistsError:
                existing = self.head(key)
                if existing is None:
                    raise ObjectConflict("immutable object raced with deletion")
                self._match(existing, size, sha256)
                return existing
            self._fsync_directory(target.parent)
        finally:
            temporary.unlink(missing_ok=True)
        return ObjectInfo(key, size, sha256, etag=sha256)

    def put_bytes(self, key: str, content: bytes, *, sha256: str) -> ObjectInfo:
        if not isinstance(content, bytes):
            raise TypeError("object content must be bytes")
        if hashlib.sha256(content).hexdigest() != _digest(sha256):
            raise ObjectIntegrityError("object content hash does not match")
        descriptor, temporary_name = tempfile.mkstemp(prefix="cogdoc-object-")
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            return self.put_file(key, temporary, sha256=sha256)
        finally:
            temporary.unlink(missing_ok=True)

    def head(self, key: str) -> ObjectInfo | None:
        key = _key(key)
        path = self._path(key)
        try:
            info = path.lstat()
        except FileNotFoundError:
            return None
        if path.is_symlink() or not path.is_file():
            raise ObjectIntegrityError("object path is unsafe")
        digest, size = _hash_file(path)
        if size != info.st_size:
            raise ObjectIntegrityError("object changed during verification")
        return ObjectInfo(key, size, digest, etag=digest)

    def iter_bytes(self, key: str) -> Iterator[bytes]:
        expected = self.head(key)
        if expected is None:
            raise ObjectNotFound("object is unavailable")
        path = self._path(key)
        builder = hashlib.sha256()
        size = 0
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(_READ_CHUNK), b""):
                builder.update(chunk)
                size += len(chunk)
                yield chunk
        if size != expected.byte_size or builder.hexdigest() != expected.sha256:
            raise ObjectIntegrityError("object changed while reading")

    def delete(self, key: str) -> None:
        path = self._path(_key(key))
        path.unlink(missing_ok=True)
        if path.parent.exists():
            self._fsync_directory(path.parent)

    def list_prefix(self, prefix: str) -> Iterable[ObjectInfo]:
        prefix = _key(prefix, allow_empty=True)
        for path in sorted(self.root.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            key = path.relative_to(self.root).as_posix()
            if key.startswith(prefix):
                result = self.head(key)
                if result is not None:
                    yield result

    def check(self) -> bool:
        probe = f".health/{uuid.uuid4().hex}"
        content = secrets_token = os.urandom(32)
        digest = hashlib.sha256(content).hexdigest()
        try:
            self.put_bytes(probe, secrets_token, sha256=digest)
            return b"".join(self.iter_bytes(probe)) == content
        finally:
            self.delete(probe)

    @staticmethod
    def _match(existing: ObjectInfo, size: int, digest: str) -> None:
        if existing.byte_size != size or existing.sha256 != digest:
            raise ObjectConflict("immutable object key already has different content")

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


class S3ObjectStore:
    """S3-compatible immutable objects with bounded, abortable multipart upload."""

    def __init__(
        self,
        bucket: str,
        *,
        prefix: str = "cogdoc",
        client: Any | None = None,
        endpoint_url: str | None = None,
        region_name: str | None = None,
        require_versioning: bool = True,
    ) -> None:
        if not bucket or len(bucket) > 255:
            raise ValueError("S3 bucket is invalid")
        self.bucket = bucket
        self.prefix = _key(prefix).rstrip("/")
        self._client = client or self._make_client(endpoint_url, region_name)
        self.require_versioning = require_versioning
        if require_versioning:
            status = self._client.get_bucket_versioning(Bucket=bucket).get("Status")
            if status != "Enabled":
                raise ObjectStoreError("S3 bucket versioning must be enabled")

    @staticmethod
    def _make_client(endpoint_url: str | None, region_name: str | None) -> Any:
        try:
            import boto3  # type: ignore[import-not-found]
            from botocore.config import Config  # type: ignore[import-not-found]
        except ImportError as exc:
            raise ObjectStoreError(
                "install cogdoc[ha] to use S3 object storage"
            ) from exc
        return boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            region_name=region_name,
            config=Config(
                retries={"max_attempts": 5, "mode": "adaptive"},
                connect_timeout=10,
                read_timeout=60,
                max_pool_connections=32,
                s3={"addressing_style": "path" if endpoint_url else "auto"},
            ),
        )

    def _remote(self, key: str) -> str:
        return f"{self.prefix}/{_key(key)}"

    @staticmethod
    def _code(exc: BaseException) -> str:
        response = getattr(exc, "response", {})
        if isinstance(response, Mapping):
            error = response.get("Error", {})
            if isinstance(error, Mapping):
                return str(error.get("Code", ""))
        return ""

    def head(self, key: str) -> ObjectInfo | None:
        key = _key(key)
        try:
            value = self._client.head_object(Bucket=self.bucket, Key=self._remote(key))
        except Exception as exc:
            if self._code(exc) in {"404", "NoSuchKey", "NotFound"}:
                return None
            raise ObjectStoreError("S3 object metadata request failed") from exc
        raw_metadata = value.get("Metadata") or {}
        if not isinstance(raw_metadata, Mapping):
            raise ObjectIntegrityError("S3 object metadata is malformed")
        # AWS commonly lowercases user metadata keys, while compatible stores
        # such as MinIO may preserve/title-case them. HTTP metadata names are
        # case-insensitive, so normalize keys without relaxing the digest.
        metadata = {str(name).lower(): item for name, item in raw_metadata.items()}
        digest = str(metadata.get("cogdoc-sha256") or "")
        if _SHA256.fullmatch(digest) is None:
            raise ObjectIntegrityError("S3 object is missing its content hash")
        return ObjectInfo(
            key,
            int(value["ContentLength"]),
            digest,
            etag=str(value.get("ETag") or "").strip('"') or None,
            version_id=str(value.get("VersionId") or "") or None,
        )

    def put_bytes(self, key: str, content: bytes, *, sha256: str) -> ObjectInfo:
        if not isinstance(content, bytes):
            raise TypeError("object content must be bytes")
        sha256 = _digest(sha256)
        if len(content) > _MAX_IN_MEMORY:
            raise ValueError("put_bytes is limited to 16 MiB; use put_file")
        if hashlib.sha256(content).hexdigest() != sha256:
            raise ObjectIntegrityError("object content hash does not match")
        existing = self.head(key)
        if existing is not None:
            return self._match(existing, len(content), sha256)
        try:
            value = self._client.put_object(
                Bucket=self.bucket,
                Key=self._remote(key),
                Body=content,
                ContentLength=len(content),
                Metadata={"cogdoc-sha256": sha256},
                ChecksumSHA256=base64.b64encode(bytes.fromhex(sha256)).decode(),
                IfNoneMatch="*",
            )
        except Exception as exc:
            if self._code(exc) in {
                "PreconditionFailed",
                "412",
                "ConditionalRequestConflict",
            }:
                existing = self.head(key)
                if existing is not None:
                    return self._match(existing, len(content), sha256)
            raise ObjectStoreError("S3 immutable object upload failed") from exc
        return self._verify_uploaded(key, len(content), sha256, value)

    def put_file(self, key: str, source: Path, *, sha256: str) -> ObjectInfo:
        key = _key(key)
        sha256 = _digest(sha256)
        supplied_source = Path(source)
        if supplied_source.is_symlink():
            raise ObjectIntegrityError("object source is not a regular file")
        source = supplied_source.resolve(strict=True)
        if not source.is_file():
            raise ObjectIntegrityError("object source is not a regular file")
        actual, size = _hash_file(source)
        if actual != sha256:
            raise ObjectIntegrityError("object source hash does not match")
        if size <= _MAX_IN_MEMORY:
            return self.put_bytes(key, source.read_bytes(), sha256=sha256)
        existing = self.head(key)
        if existing is not None:
            return self._match(existing, size, sha256)
        upload_id: str | None = None
        try:
            created = self._client.create_multipart_upload(
                Bucket=self.bucket,
                Key=self._remote(key),
                Metadata={"cogdoc-sha256": sha256},
                ChecksumAlgorithm="SHA256",
            )
            upload_id = str(created["UploadId"])
            parts: list[dict[str, Any]] = []
            with source.open("rb") as handle:
                number = 1
                while chunk := handle.read(_MULTIPART_CHUNK):
                    part_digest = base64.b64encode(
                        hashlib.sha256(chunk).digest()
                    ).decode()
                    value = self._client.upload_part(
                        Bucket=self.bucket,
                        Key=self._remote(key),
                        UploadId=upload_id,
                        PartNumber=number,
                        Body=chunk,
                        ContentLength=len(chunk),
                        ChecksumSHA256=part_digest,
                    )
                    part = {"ETag": value["ETag"], "PartNumber": number}
                    if value.get("ChecksumSHA256"):
                        part["ChecksumSHA256"] = value["ChecksumSHA256"]
                    parts.append(part)
                    number += 1
            try:
                completed = self._client.complete_multipart_upload(
                    Bucket=self.bucket,
                    Key=self._remote(key),
                    UploadId=upload_id,
                    MultipartUpload={"Parts": parts},
                    IfNoneMatch="*",
                )
            except Exception as exc:
                if self._code(exc) in {
                    "PreconditionFailed",
                    "412",
                    "ConditionalRequestConflict",
                }:
                    existing = self.head(key)
                    if existing is not None:
                        self._match(existing, size, sha256)
                        self._client.abort_multipart_upload(
                            Bucket=self.bucket,
                            Key=self._remote(key),
                            UploadId=upload_id,
                        )
                        upload_id = None
                        return existing
                raise
            upload_id = None
            return self._verify_uploaded(key, size, sha256, completed)
        except Exception as exc:
            if upload_id is not None:
                try:
                    self._client.abort_multipart_upload(
                        Bucket=self.bucket,
                        Key=self._remote(key),
                        UploadId=upload_id,
                    )
                except Exception:
                    pass
            if isinstance(exc, ObjectStoreError):
                raise
            raise ObjectStoreError("S3 multipart upload failed") from exc

    def _verify_uploaded(
        self, key: str, size: int, sha256: str, response: Mapping[str, Any]
    ) -> ObjectInfo:
        uploaded = self.head(key)
        if uploaded is None:
            raise ObjectIntegrityError("S3 upload completed without a visible object")
        self._match(uploaded, size, sha256)
        response_version = str(response.get("VersionId") or "") or None
        if self.require_versioning and not (uploaded.version_id or response_version):
            raise ObjectIntegrityError(
                "S3 versioned upload did not return a version id"
            )
        return uploaded

    @staticmethod
    def _match(existing: ObjectInfo, size: int, digest: str) -> ObjectInfo:
        if existing.byte_size != size or existing.sha256 != digest:
            raise ObjectConflict("immutable S3 key already has different content")
        return existing

    def iter_bytes(self, key: str) -> Iterator[bytes]:
        expected = self.head(key)
        if expected is None:
            raise ObjectNotFound("S3 object is unavailable")
        kwargs: dict[str, Any] = {"Bucket": self.bucket, "Key": self._remote(key)}
        if expected.version_id:
            kwargs["VersionId"] = expected.version_id
        try:
            response = self._client.get_object(**kwargs)
            body = response["Body"]
            builder = hashlib.sha256()
            size = 0
            while chunk := body.read(_READ_CHUNK):
                builder.update(chunk)
                size += len(chunk)
                yield chunk
        except ObjectStoreError:
            raise
        except Exception as exc:
            raise ObjectStoreError("S3 object read failed") from exc
        finally:
            close = getattr(locals().get("body"), "close", None)
            if callable(close):
                close()
        if size != expected.byte_size or builder.hexdigest() != expected.sha256:
            raise ObjectIntegrityError("S3 object content is corrupt")

    def delete(self, key: str) -> None:
        key = _key(key)
        try:
            self._client.delete_object(Bucket=self.bucket, Key=self._remote(key))
        except Exception as exc:
            raise ObjectStoreError("S3 object deletion failed") from exc

    def list_prefix(self, prefix: str) -> Iterable[ObjectInfo]:
        prefix = _key(prefix, allow_empty=True)
        continuation: str | None = None
        while True:
            kwargs: dict[str, Any] = {
                "Bucket": self.bucket,
                "Prefix": f"{self.prefix}/{prefix}" if prefix else f"{self.prefix}/",
                "MaxKeys": 1000,
            }
            if continuation:
                kwargs["ContinuationToken"] = continuation
            try:
                response = self._client.list_objects_v2(**kwargs)
            except Exception as exc:
                raise ObjectStoreError("S3 object listing failed") from exc
            for item in response.get("Contents") or ():
                remote = str(item["Key"])
                key = remote[len(self.prefix) + 1 :]
                info = self.head(key)
                if info is not None:
                    yield info
            if not response.get("IsTruncated"):
                return
            continuation = str(response.get("NextContinuationToken") or "")
            if not continuation:
                raise ObjectStoreError("S3 listing omitted continuation token")

    def check(self) -> bool:
        try:
            self._client.head_bucket(Bucket=self.bucket)
            if self.require_versioning:
                return (
                    self._client.get_bucket_versioning(Bucket=self.bucket).get("Status")
                    == "Enabled"
                )
            return True
        except Exception:
            return False


class ObjectIndexRepository:
    """Immutable index generations stored below a generation-unique prefix."""

    def __init__(self, store: ObjectStore, *, prefix: str = "indexes") -> None:
        self.store = store
        self.prefix = _key(prefix).rstrip("/")

    @staticmethod
    def _scope(value: Any) -> str:
        return hashlib.sha256(str(value).encode()).hexdigest()

    def _base(self, generation: Mapping[str, Any]) -> str:
        generation_id = _key(str(generation["generation_id"]))
        return (
            f"{self.prefix}/{self._scope(generation['tenant_id'])}/"
            f"{self._scope(generation['kb_id'])}/{generation_id}"
        )

    def materialize(
        self, generation: Mapping[str, Any], source_directory: str | os.PathLike[str]
    ) -> None:
        manifest, digest = normalize_manifest(generation.get("manifest"))  # type: ignore[arg-type]
        if digest != generation.get("manifest_sha256"):
            raise IndexIntegrityError("database index manifest hash is invalid")
        source = Path(source_directory).resolve(strict=True)
        base = self._base(generation)
        for item in manifest["files"]:
            relative = PurePosixPath(item["path"])
            path = source.joinpath(*relative.parts)
            resolved = path.resolve(strict=True)
            try:
                resolved.relative_to(source)
            except ValueError as exc:
                raise IndexIntegrityError("index source escapes its root") from exc
            if resolved != path or not path.is_file():
                raise IndexIntegrityError("index source contains a symlink")
            self.store.put_file(
                f"{base}/files/{item['path']}", path, sha256=item["sha256"]
            )
        encoded = json.dumps(
            manifest,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
        self.store.put_bytes(f"{base}/manifest.json", encoded, sha256=digest)
        self.verify(generation)

    def verify(self, generation: Mapping[str, Any]) -> None:
        manifest, digest = normalize_manifest(generation.get("manifest"))  # type: ignore[arg-type]
        if digest != generation.get("manifest_sha256"):
            raise IndexIntegrityError("database index manifest hash is invalid")
        base = self._base(generation)
        marker_key = f"{base}/manifest.json"
        marker = self.store.head(marker_key)
        if marker is None or marker.sha256 != digest:
            raise IndexIntegrityError(
                "object index commit marker is missing or corrupt"
            )
        canonical_manifest = json.dumps(
            manifest,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
        try:
            stored_manifest = b"".join(self.store.iter_bytes(marker_key))
        except Exception as exc:
            raise IndexIntegrityError(
                "object index commit marker content is corrupt"
            ) from exc
        if stored_manifest != canonical_manifest:
            raise IndexIntegrityError(
                "object index commit marker content does not match the manifest"
            )
        expected = {marker_key}
        for item in manifest["files"]:
            key = f"{base}/files/{item['path']}"
            expected.add(key)
            info = self.store.head(key)
            if (
                info is None
                or info.byte_size != item["byte_size"]
                or info.sha256 != item["sha256"]
            ):
                raise IndexIntegrityError("object index file is missing or corrupt")
            try:
                for _chunk in self.store.iter_bytes(key):
                    pass
            except Exception as exc:
                raise IndexIntegrityError(
                    "object index file content is corrupt"
                ) from exc
        actual = {item.key for item in self.store.list_prefix(f"{base}/")}
        if actual != expected:
            raise IndexIntegrityError("object index contains unmanifested objects")

    def materialize_local(
        self,
        generation: Mapping[str, Any],
        destination: str | os.PathLike[str],
    ) -> Path:
        """Download a verified generation through an atomic directory switch."""

        manifest, digest = normalize_manifest(generation.get("manifest"))  # type: ignore[arg-type]
        if digest != generation.get("manifest_sha256"):
            raise IndexIntegrityError("database index manifest hash is invalid")
        self.verify(generation)
        target = Path(destination)
        if target.is_symlink():
            raise IndexIntegrityError("local index cache target is unsafe")
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with _materialization_lock(target.parent, target):
            if target.is_symlink():
                raise IndexIntegrityError("local index cache target is unsafe")
            _scavenge_materialization_remnants(target.parent, target)
            if target.exists():
                try:
                    self.verify_local(generation, target)
                    return target
                except IndexIntegrityError:
                    # A local cache is never authoritative. Isolate it with an
                    # atomic rename before downloading a replacement, so readers
                    # can observe either a fully verified generation or no cache,
                    # but never a partially repaired directory.
                    corrupt = (
                        target.parent
                        / f".corrupt-{_materialization_namespace(target)}-{uuid.uuid4().hex}"
                    )
                    os.rename(target, corrupt)
                    LocalObjectStore._fsync_directory(target.parent)
                    _remove_materialization_path(corrupt)
                    LocalObjectStore._fsync_directory(target.parent)
            temporary = (
                target.parent
                / f".tmp-materialize-{_materialization_namespace(target)}-{uuid.uuid4().hex}"
            )
            temporary.mkdir(mode=0o700)
            base = self._base(generation)
            try:
                directories = {temporary}
                for item in manifest["files"]:
                    relative = PurePosixPath(item["path"])
                    path = temporary.joinpath(*relative.parts)
                    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    directories.update(path.parents)
                    builder = hashlib.sha256()
                    size = 0
                    try:
                        with path.open("xb") as handle:
                            for chunk in self.store.iter_bytes(
                                f"{base}/files/{item['path']}"
                            ):
                                handle.write(chunk)
                                builder.update(chunk)
                                size += len(chunk)
                            handle.flush()
                            os.fsync(handle.fileno())
                    except Exception as exc:
                        raise IndexIntegrityError(
                            "object index download failed integrity verification"
                        ) from exc
                    if (
                        size != item["byte_size"]
                        or builder.hexdigest() != item["sha256"]
                    ):
                        raise IndexIntegrityError(
                            "object index download does not match its manifest"
                        )
                for directory in sorted(
                    (path for path in directories if path.is_relative_to(temporary)),
                    key=lambda path: len(path.parts),
                    reverse=True,
                ):
                    LocalObjectStore._fsync_directory(directory)
                os.rename(temporary, target)
                LocalObjectStore._fsync_directory(target.parent)
                self.verify_local(generation, target)
                return target
            finally:
                shutil.rmtree(temporary, ignore_errors=True)

    @staticmethod
    def verify_local(
        generation: Mapping[str, Any], directory: str | os.PathLike[str]
    ) -> None:
        manifest, digest = normalize_manifest(generation.get("manifest"))  # type: ignore[arg-type]
        if digest != generation.get("manifest_sha256"):
            raise IndexIntegrityError("database index manifest hash is invalid")
        supplied = Path(directory)
        if supplied.is_symlink():
            raise IndexIntegrityError("local index cache is unsafe")
        try:
            root = supplied.resolve(strict=True)
        except FileNotFoundError as exc:
            raise IndexIntegrityError("local index cache is unavailable") from exc
        if not root.is_dir():
            raise IndexIntegrityError("local index cache is not a directory")
        expected = {item["path"]: item for item in manifest["files"]}
        actual: set[str] = set()
        for path in root.rglob("*"):
            if path.is_symlink():
                raise IndexIntegrityError("local index cache contains a symlink")
            if not path.is_file():
                continue
            relative = path.relative_to(root).as_posix()
            actual.add(relative)
            item = expected.get(relative)
            if item is None:
                raise IndexIntegrityError("local index cache contains an extra file")
            digest_value, size = _hash_file(path)
            if digest_value != item["sha256"] or size != item["byte_size"]:
                raise IndexIntegrityError("local index cache file is corrupt")
        if actual != set(expected):
            raise IndexIntegrityError("local index cache is incomplete")

    def delete_generation(self, generation: Mapping[str, Any]) -> None:
        base = self._base(generation)
        keys = [item.key for item in self.store.list_prefix(f"{base}/")]
        # Marker first makes an interrupted GC immediately invisible to verifiers.
        marker = f"{base}/manifest.json"
        if marker in keys:
            self.store.delete(marker)
        for key in keys:
            if key != marker:
                self.store.delete(key)


__all__ = [
    "LocalObjectStore",
    "ObjectConflict",
    "ObjectIndexRepository",
    "ObjectInfo",
    "ObjectIntegrityError",
    "ObjectNotFound",
    "ObjectStore",
    "ObjectStoreError",
    "S3ObjectStore",
]
