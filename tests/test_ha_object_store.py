from __future__ import annotations

import hashlib
import io

import pytest

import cogdoc.ha.object_store as object_module
from cogdoc.ha.index_generation import IndexGenerationStore, IndexIntegrityError
from cogdoc.ha.object_store import (
    LocalObjectStore,
    ObjectConflict,
    ObjectIndexRepository,
    S3ObjectStore,
)
from cogdoc.ha.storage import SQLiteBackend


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


class ClientError(Exception):
    def __init__(self, code):
        self.response = {"Error": {"Code": code}}


class Body(io.BytesIO):
    pass


class FakeS3:
    def __init__(self):
        self.objects = {}
        self.uploads = {}
        self.aborted = []
        self.version = 0
        self.fail_part = False

    def get_bucket_versioning(self, **_kwargs):
        return {"Status": "Enabled"}

    def head_bucket(self, **_kwargs):
        return {}

    def head_object(self, *, Key, **_kwargs):
        if Key not in self.objects:
            raise ClientError("404")
        value = self.objects[Key]
        return {
            "ContentLength": len(value["body"]),
            "Metadata": value["metadata"],
            "ETag": value["etag"],
            "VersionId": value["version"],
        }

    def put_object(
        self, *, Key, Body, Metadata, ChecksumSHA256, IfNoneMatch, **_kwargs
    ):
        assert IfNoneMatch == "*"
        assert ChecksumSHA256
        if Key in self.objects:
            raise ClientError("PreconditionFailed")
        return self._store(Key, bytes(Body), Metadata)

    def create_multipart_upload(self, *, Key, Metadata, ChecksumAlgorithm, **_kwargs):
        assert ChecksumAlgorithm == "SHA256"
        upload_id = f"upload-{len(self.uploads) + 1}"
        self.uploads[upload_id] = {"key": Key, "metadata": Metadata, "parts": {}}
        return {"UploadId": upload_id}

    def upload_part(
        self,
        *,
        UploadId,
        PartNumber,
        Body,
        ContentLength,
        ChecksumSHA256,
        **_kwargs,
    ):
        if self.fail_part:
            raise ClientError("SlowDown")
        content = bytes(Body)
        assert ContentLength == len(content)
        assert ChecksumSHA256
        self.uploads[UploadId]["parts"][PartNumber] = content
        return {
            "ETag": f"part-{PartNumber}",
            "ChecksumSHA256": ChecksumSHA256,
        }

    def complete_multipart_upload(
        self, *, UploadId, MultipartUpload, IfNoneMatch, **_kwargs
    ):
        assert IfNoneMatch == "*"
        assert MultipartUpload["Parts"]
        assert all(part.get("ChecksumSHA256") for part in MultipartUpload["Parts"])
        upload = self.uploads.pop(UploadId)
        body = b"".join(upload["parts"][part] for part in sorted(upload["parts"]))
        return self._store(upload["key"], body, upload["metadata"])

    def abort_multipart_upload(self, *, UploadId, **_kwargs):
        self.aborted.append(UploadId)
        self.uploads.pop(UploadId, None)

    def _store(self, key, body, metadata):
        self.version += 1
        value = {
            "body": body,
            "metadata": metadata,
            "etag": _digest(body),
            "version": f"v{self.version}",
        }
        self.objects[key] = value
        return {"ETag": value["etag"], "VersionId": value["version"]}

    def get_object(self, *, Key, VersionId=None, **_kwargs):
        value = self.objects[Key]
        assert VersionId in {None, value["version"]}
        return {"Body": Body(value["body"])}

    def delete_object(self, *, Key, **_kwargs):
        self.objects.pop(Key, None)
        return {}

    def list_objects_v2(self, *, Prefix, **_kwargs):
        return {
            "Contents": [
                {"Key": key} for key in sorted(self.objects) if key.startswith(Prefix)
            ],
            "IsTruncated": False,
        }


def test_local_store_is_immutable_and_stream_verified(tmp_path):
    store = LocalObjectStore(tmp_path / "objects")
    content = b"immutable"
    info = store.put_bytes("tenant/object.bin", content, sha256=_digest(content))
    assert b"".join(store.iter_bytes(info.key)) == content
    assert store.put_bytes(info.key, content, sha256=_digest(content)) == info
    with pytest.raises(ObjectConflict):
        store.put_bytes(info.key, b"different", sha256=_digest(b"different"))
    assert store.check()


def test_local_store_rejects_traversal_and_symlink(tmp_path):
    store = LocalObjectStore(tmp_path / "objects")
    with pytest.raises(ValueError):
        store.put_bytes("../escape", b"x", sha256=_digest(b"x"))
    outside = tmp_path / "outside"
    outside.write_bytes(b"secret")
    path = tmp_path / "objects" / "link"
    path.symlink_to(outside)
    with pytest.raises(Exception, match="unsafe"):
        store.head("link")


def test_s3_conditional_put_and_verified_read():
    client = FakeS3()
    store = S3ObjectStore("bucket", prefix="root", client=client)
    content = b"payload"
    info = store.put_bytes("a/data", content, sha256=_digest(content))
    assert info.version_id == "v1"
    assert b"".join(store.iter_bytes("a/data")) == content
    assert (
        store.put_bytes("a/data", content, sha256=_digest(content)).version_id == "v1"
    )


def test_s3_metadata_names_are_case_insensitive():
    client = FakeS3()
    store = S3ObjectStore("bucket", prefix="root", client=client)
    content = b"payload"
    store.put_bytes("a/data", content, sha256=_digest(content))
    remote = store._remote("a/data")
    metadata = client.objects[remote]["metadata"]
    client.objects[remote]["metadata"] = {"Cogdoc-Sha256": metadata["cogdoc-sha256"]}
    assert store.head("a/data").sha256 == _digest(content)


def test_pinned_botocore_model_supports_required_conditional_writes():
    botocore = pytest.importorskip("botocore.session")
    model = botocore.get_session().get_service_model("s3")
    for operation in ("PutObject", "CompleteMultipartUpload"):
        shape = model.operation_model(operation).input_shape
        assert shape is not None
        assert "IfNoneMatch" in shape.members
    upload_shape = model.operation_model("UploadPart").input_shape
    assert upload_shape is not None
    assert "ChecksumSHA256" in upload_shape.members


def test_s3_multipart_is_aborted_and_never_visible_on_failure(tmp_path, monkeypatch):
    client = FakeS3()
    client.fail_part = True
    store = S3ObjectStore("bucket", client=client)
    monkeypatch.setattr(object_module, "_MAX_IN_MEMORY", 3)
    source = tmp_path / "large"
    source.write_bytes(b"larger")
    with pytest.raises(Exception, match="multipart"):
        store.put_file("large", source, sha256=_digest(b"larger"))
    assert client.aborted
    assert store.head("large") is None


def _manifest(content):
    return {
        "schema_version": "index-manifest-v1",
        "contract": {
            "chunk_version": "v1",
            "embedding_model": "model",
            "dimensions": 3,
        },
        "files": [
            {
                "path": "vectors.bin",
                "sha256": _digest(content),
                "byte_size": len(content),
            }
        ],
    }


@pytest.mark.parametrize("kind", ["local", "s3"])
def test_object_index_commit_marker_precedes_publication(tmp_path, kind):
    backend = SQLiteBackend(tmp_path / f"{kind}.db")
    store = (
        LocalObjectStore(tmp_path / "objects")
        if kind == "local"
        else S3ObjectStore("bucket", client=FakeS3())
    )
    repository = ObjectIndexRepository(store)
    authority = IndexGenerationStore(backend)
    content = b"safe index"
    source = tmp_path / f"source-{kind}"
    source.mkdir()
    (source / "vectors.bin").write_bytes(content)
    generation = authority.begin_build("tenant", "kb", "build", "worker")
    prepared = authority.prepare(
        generation["generation_id"], generation["lease_token"], _manifest(content)
    )
    assert authority.current("tenant", "kb") is None
    repository.materialize(prepared, source)
    repository.verify(prepared)
    published = authority.publish(
        prepared["generation_id"], prepared["lease_token"], repository.verify
    )
    assert authority.resolve_current("tenant", "kb", repository.verify) == published
    backend.close()


def test_object_index_missing_or_extra_object_is_never_published(tmp_path):
    backend = SQLiteBackend(tmp_path / "ha.db")
    objects = LocalObjectStore(tmp_path / "objects")
    repository = ObjectIndexRepository(objects)
    authority = IndexGenerationStore(backend)
    content = b"safe index"
    source = tmp_path / "source"
    source.mkdir()
    (source / "vectors.bin").write_bytes(content)
    generation = authority.begin_build("tenant", "kb", "build", "worker")
    prepared = authority.prepare(
        generation["generation_id"], generation["lease_token"], _manifest(content)
    )
    repository.materialize(prepared, source)
    objects.put_bytes(
        f"{repository._base(prepared)}/files/unmanifested",
        b"bad",
        sha256=_digest(b"bad"),
    )
    with pytest.raises(IndexIntegrityError, match="unmanifested"):
        authority.publish(
            prepared["generation_id"], prepared["lease_token"], repository.verify
        )
    assert authority.current("tenant", "kb") is None
    backend.close()


def test_s3_object_body_tampering_cannot_be_hidden_by_matching_metadata(tmp_path):
    backend = SQLiteBackend(tmp_path / "authority.db")
    client = FakeS3()
    store = S3ObjectStore("bucket", client=client)
    repository = ObjectIndexRepository(store)
    authority = IndexGenerationStore(backend)
    content = b"safe index"
    source = tmp_path / "source"
    source.mkdir()
    (source / "vectors.bin").write_bytes(content)
    generation = authority.begin_build("tenant", "kb", "tamper", "worker")
    prepared = authority.prepare(
        generation["generation_id"], generation["lease_token"], _manifest(content)
    )
    repository.materialize(prepared, source)
    data_key = f"{repository._base(prepared)}/files/vectors.bin"
    remote_key = store._remote(data_key)
    # Simulate a compatible store returning intact user metadata for corrupt bytes.
    client.objects[remote_key]["body"] = b"BAD INDEX!"

    with pytest.raises(IndexIntegrityError, match="content"):
        authority.publish(
            prepared["generation_id"], prepared["lease_token"], repository.verify
        )
    assert authority.current("tenant", "kb") is None
    backend.close()


def test_repository_materializes_verified_local_cache_atomically(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "vectors.bin").write_bytes(b"safe index")
    backend = SQLiteBackend(tmp_path / "authority.db")
    authority = IndexGenerationStore(backend)
    objects = LocalObjectStore(tmp_path / "objects")
    repository = ObjectIndexRepository(objects)
    generation = authority.begin_build("tenant", "kb", "build", "worker")
    generation = authority.prepare(
        generation["generation_id"],
        generation["lease_token"],
        _manifest(b"safe index"),
    )
    repository.materialize(generation, source)

    destination = tmp_path / "cache" / generation["generation_id"]
    assert repository.materialize_local(generation, destination) == destination
    assert (destination / "vectors.bin").read_bytes() == b"safe index"
    repository.verify_local(generation, destination)
    assert repository.materialize_local(generation, destination) == destination
    backend.close()


def test_repository_download_failure_never_exposes_partial_cache(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "vectors.bin").write_bytes(b"safe index")
    backend = SQLiteBackend(tmp_path / "authority.db")
    authority = IndexGenerationStore(backend)
    objects = LocalObjectStore(tmp_path / "objects")
    repository = ObjectIndexRepository(objects)
    generation = authority.begin_build("tenant", "kb", "build", "worker")
    generation = authority.prepare(
        generation["generation_id"],
        generation["lease_token"],
        _manifest(b"safe index"),
    )
    repository.materialize(generation, source)
    remote = objects._path(f"{repository._base(generation)}/files/vectors.bin")
    remote.write_bytes(b"bad index!")
    destination = tmp_path / "cache" / generation["generation_id"]

    with pytest.raises(IndexIntegrityError):
        repository.materialize_local(generation, destination)
    assert not destination.exists()
    assert not list(destination.parent.glob(".tmp-materialize-*"))
    backend.close()


def test_repository_repairs_corrupt_local_cache_by_atomic_rematerialization(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "vectors.bin").write_bytes(b"safe index")
    backend = SQLiteBackend(tmp_path / "authority.db")
    authority = IndexGenerationStore(backend)
    repository = ObjectIndexRepository(LocalObjectStore(tmp_path / "objects"))
    generation = authority.begin_build("tenant", "kb", "build", "worker")
    generation = authority.prepare(
        generation["generation_id"],
        generation["lease_token"],
        _manifest(b"safe index"),
    )
    repository.materialize(generation, source)
    destination = tmp_path / "cache" / generation["generation_id"]
    repository.materialize_local(generation, destination)
    (destination / "vectors.bin").write_bytes(b"corrupt")

    assert repository.materialize_local(generation, destination) == destination
    assert (destination / "vectors.bin").read_bytes() == b"safe index"
    assert not list(destination.parent.glob(".corrupt-*"))
    repository.verify_local(generation, destination)
    backend.close()


def test_repository_scavenges_only_same_generation_crash_remnants(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "vectors.bin").write_bytes(b"safe index")
    backend = SQLiteBackend(tmp_path / "authority.db")
    authority = IndexGenerationStore(backend)
    repository = ObjectIndexRepository(LocalObjectStore(tmp_path / "objects"))
    generation = authority.begin_build("tenant", "kb", "build", "worker")
    generation = authority.prepare(
        generation["generation_id"],
        generation["lease_token"],
        _manifest(b"safe index"),
    )
    repository.materialize(generation, source)
    destination = tmp_path / "cache" / generation["generation_id"]
    destination.parent.mkdir(parents=True)
    namespace = object_module._materialization_namespace(destination)
    stale_temp = destination.parent / f".tmp-materialize-{namespace}-crashed"
    stale_corrupt = destination.parent / f".corrupt-{namespace}-crashed"
    unrelated = destination.parent / ".tmp-another-generation-live"
    stale_temp.mkdir()
    stale_corrupt.mkdir()
    unrelated.mkdir()

    repository.materialize_local(generation, destination)

    assert not stale_temp.exists()
    assert not stale_corrupt.exists()
    assert unrelated.is_dir()
    repository.verify_local(generation, destination)
    backend.close()
