from __future__ import annotations

import hashlib
import os
import threading
import uuid

import pytest

from cogdoc.ha.index_generation import IndexGenerationStore, IndexIntegrityError
from cogdoc.ha.object_store import ObjectConflict, ObjectIndexRepository, S3ObjectStore
from cogdoc.ha.runtime import manifest_for_directory
from cogdoc.ha.storage import SQLiteBackend


pytestmark = pytest.mark.skipif(
    not os.environ.get("COGDOC_TEST_S3_ENDPOINT_URL"),
    reason="COGDOC_TEST_S3_ENDPOINT_URL is not configured",
)


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


@pytest.fixture
def versioned_bucket():
    import boto3
    from botocore.config import Config

    endpoint = os.environ["COGDOC_TEST_S3_ENDPOINT_URL"]
    region = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        region_name=region,
        config=Config(s3={"addressing_style": "path"}),
    )
    bucket = f"cogdoc-ha-test-{uuid.uuid4().hex}"
    client.create_bucket(Bucket=bucket)
    client.put_bucket_versioning(
        Bucket=bucket, VersioningConfiguration={"Status": "Enabled"}
    )
    try:
        yield client, bucket
    finally:
        uploads = client.list_multipart_uploads(Bucket=bucket).get("Uploads") or ()
        for upload in uploads:
            client.abort_multipart_upload(
                Bucket=bucket,
                Key=upload["Key"],
                UploadId=upload["UploadId"],
            )
        while True:
            listed = client.list_object_versions(Bucket=bucket)
            objects = [
                {"Key": item["Key"], "VersionId": item["VersionId"]}
                for key in ("Versions", "DeleteMarkers")
                for item in listed.get(key) or ()
            ]
            if objects:
                client.delete_objects(
                    Bucket=bucket,
                    Delete={"Objects": objects, "Quiet": True},
                )
            if not listed.get("IsTruncated"):
                break
        client.delete_bucket(Bucket=bucket)


def test_real_s3_versioning_conditional_writes_and_multipart(
    tmp_path, versioned_bucket
):
    client, bucket = versioned_bucket
    store = S3ObjectStore(
        bucket,
        prefix="integration",
        client=client,
        require_versioning=True,
    )
    assert store.check() is True

    small = b"immutable-control-object"
    first = store.put_bytes("small.bin", small, sha256=_digest(small))
    assert first.version_id
    assert b"".join(store.iter_bytes("small.bin")) == small
    assert store.put_bytes("small.bin", small, sha256=_digest(small)) == first

    large_path = tmp_path / "large.bin"
    large = (b"0123456789abcdef" * (1024 * 1024 + 1))[: 17 * 1024 * 1024]
    large_path.write_bytes(large)
    uploaded = store.put_file("large.bin", large_path, sha256=_digest(large))
    assert uploaded.version_id
    assert uploaded.byte_size == len(large)
    assert b"".join(store.iter_bytes("large.bin")) == large

    barrier = threading.Barrier(2)
    outcomes: list[tuple[str, bytes]] = []

    def race(content: bytes) -> None:
        barrier.wait()
        try:
            store.put_bytes("race.bin", content, sha256=_digest(content))
        except ObjectConflict:
            outcomes.append(("conflict", content))
        else:
            outcomes.append(("stored", content))

    contenders = [b"first-writer", b"second-writer"]
    threads = [threading.Thread(target=race, args=(content,)) for content in contenders]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert all(not thread.is_alive() for thread in threads)
    assert sorted(result for result, _content in outcomes) == ["conflict", "stored"]
    winner = next(content for result, content in outcomes if result == "stored")
    assert b"".join(store.iter_bytes("race.bin")) == winner

    backend = SQLiteBackend(tmp_path / "authority.db")
    authority = IndexGenerationStore(backend)
    repository = ObjectIndexRepository(store)
    generation_dir = tmp_path / "generation"
    generation_dir.mkdir()
    (generation_dir / "portable.sqlite3").write_bytes(b"verified-index-generation")
    generation = authority.begin_build("tenant", "kb", "build", "worker")
    generation = authority.prepare(
        generation["generation_id"],
        generation["lease_token"],
        manifest_for_directory(
            generation_dir,
            contract={
                "chunk_version": "chunks-v1",
                "embedding_model": "model-v1",
                "dimensions": 3,
            },
        ),
    )
    repository.materialize(generation, generation_dir)
    published = authority.publish(
        generation["generation_id"], generation["lease_token"], repository.verify
    )
    assert authority.resolve_current("tenant", "kb", repository.verify) == published

    # Versioning preserves recovery material, but a delete marker still makes
    # the authoritative current generation unavailable. Readers must fail
    # closed instead of selecting an older local or object version.
    store.delete(f"{repository._base(published)}/files/portable.sqlite3")
    with pytest.raises(IndexIntegrityError):
        authority.resolve_current("tenant", "kb", repository.verify)
    assert (
        authority.current("tenant", "kb")["generation_id"] == published["generation_id"]
    )
    backend.close()
