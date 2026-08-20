import hashlib
import json
from pathlib import Path
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event

import pytest

from cogdoc.service.source_artifact_store import (
    ArtifactConflictError,
    ArtifactIntegrityError,
    ArtifactLimitError,
    ArtifactNotFoundError,
    SourceArtifactStore,
)
from cogdoc.source_model import build_version_id


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _version_id(content: bytes, source_id: str = "src-one") -> str:
    return build_version_id(source_id, _digest(content))


def _put(
    store: SourceArtifactStore,
    content: bytes,
    *,
    tenant_id: str = "tenant",
    kb_id: str = "kb",
    source_id: str = "src-one",
    media_type: str = "text/plain",
    display_name: str | None = None,
    created_at: float | None = None,
    reservation_token: str | None = None,
):
    return store.put(
        tenant_id,
        kb_id,
        source_id,
        _version_id(content, source_id),
        content,
        content_sha256=_digest(content),
        media_type=media_type,
        display_name=display_name
        or ("source.txt" if media_type == "text/plain" else "source.bin"),
        created_at=created_at,
        reservation_token=reservation_token,
    )


def _reservation_item(
    content: bytes,
    *,
    source_id: str = "src-one",
    media_type: str = "text/plain",
    display_name: str = "source.txt",
    created_at: float = 1.0,
):
    return {
        "source_id": source_id,
        "version_id": _version_id(content, source_id),
        "content_sha256": _digest(content),
        "byte_size": len(content),
        "media_type": media_type,
        "display_name": display_name,
        "created_at": created_at,
    }


def test_artifact_store_writes_immutable_versions_and_isolates_scopes(tmp_path):
    store = SourceArtifactStore(tmp_path / "artifacts")
    content = b"immutable content"
    version_id = _version_id(content)
    created = _put(store, content, created_at=1.0)

    assert created["content_sha256"] == _digest(content)
    assert store.read("tenant", "kb", "src-one", version_id) == content
    assert _put(store, content, created_at=2.0) == created
    replayed = _put(
        store,
        content,
        media_type="application/json",
        display_name="renamed.json",
        created_at=3.0,
    )
    assert replayed == created
    assert replayed["media_type"] == "text/plain"
    assert replayed["display_name"] == "source.txt"
    with pytest.raises(ArtifactNotFoundError):
        store.read("other-tenant", "kb", "src-one", version_id)
    with pytest.raises(ArtifactNotFoundError):
        store.read("tenant", "other-kb", "src-one", version_id)


def test_verified_download_handle_is_streamed_and_anchored_across_soft_delete(
    tmp_path,
):
    store = SourceArtifactStore(tmp_path / "artifacts")
    content = (b"verified-stream-" * 10_000) + b"end"
    metadata = _put(store, content)

    opened, handle = store.open_verified(
        "tenant", "kb", "src-one", metadata["version_id"]
    )
    try:
        assert opened["byte_size"] == len(content)
        # The response reads an already-verified inode. A concurrent API soft
        # delete only renames its directory and cannot retarget the stream.
        store.delete_version("tenant", "kb", "src-one", metadata["version_id"])
        chunks = iter(lambda: handle.read(64 * 1024), b"")
        assert b"".join(chunks) == content
    finally:
        handle.close()


def test_verified_download_hashing_does_not_hold_the_store_lock(
    tmp_path, monkeypatch
):
    store = SourceArtifactStore(tmp_path / "artifacts")
    metadata = _put(store, b"verified-content" * 10_000)
    payload_path = next(
        (tmp_path / "artifacts").glob(
            f"tenant-*/kb-*/sources/src-one/{metadata['version_id']}/payload"
        )
    )
    hashing_started = Event()
    allow_hashing = Event()
    original_open = Path.open

    class _BlockingPayload:
        def __init__(self, handle):
            self._handle = handle

        def read(self, *args, **kwargs):
            hashing_started.set()
            if not allow_hashing.wait(timeout=5):
                raise TimeoutError("test did not release artifact hashing")
            return self._handle.read(*args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._handle, name)

    def blocking_open(path, *args, **kwargs):
        handle = original_open(path, *args, **kwargs)
        return _BlockingPayload(handle) if path == payload_path else handle

    monkeypatch.setattr(Path, "open", blocking_open)
    with ThreadPoolExecutor(max_workers=2) as pool:
        opened = pool.submit(
            store.open_verified,
            "tenant",
            "kb",
            "src-one",
            metadata["version_id"],
        )
        assert hashing_started.wait(timeout=2)
        lookup = pool.submit(
            store.get_metadata,
            "tenant",
            "kb",
            "src-one",
            metadata["version_id"],
        )
        try:
            # Metadata for another request remains available while the large
            # payload is being hashed outside the store-wide lock.
            assert lookup.result(timeout=1)["version_id"] == metadata["version_id"]
        finally:
            allow_hashing.set()
        opened_metadata, handle = opened.result(timeout=2)
        try:
            assert opened_metadata["content_sha256"] == metadata["content_sha256"]
        finally:
            handle.close()


def test_artifact_store_rejects_hash_mismatch_and_path_traversal(tmp_path):
    store = SourceArtifactStore(tmp_path / "artifacts")
    with pytest.raises(ArtifactIntegrityError, match="hash"):
        store.put(
            "tenant",
            "kb",
            "src-one",
            build_version_id("src-one", "0" * 64),
            b"content",
            content_sha256="0" * 64,
        )
    with pytest.raises(ArtifactIntegrityError, match="content address"):
        store.put(
            "tenant",
            "kb",
            "src-one",
            "version-one",
            b"content",
            content_sha256=_digest(b"content"),
        )
    for source_id, version_id in (
        ("../escape", "version-one"),
        ("src-one", "../../escape"),
        ("src/one", "version-one"),
    ):
        with pytest.raises(ValueError, match="invalid"):
            store.put(
                "../../tenant-is-hashed",
                "../kb-is-hashed",
                source_id,
                version_id,
                b"content",
                content_sha256=_digest(b"content"),
            )
    assert list(tmp_path.parent.glob("escape")) == []


def test_artifact_store_rejects_trash_symlink_escape(tmp_path):
    root = tmp_path / "artifacts"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / ".trash").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="escapes"):
        SourceArtifactStore(root)


def test_artifact_store_startup_removes_crash_leftover_put_directory(tmp_path):
    root = tmp_path / "artifacts"
    first = SourceArtifactStore(root)
    content = b"stable"
    _put(first, content)
    version_dir = next(
        root.glob(f"tenant-*/kb-*/sources/src-one/{_version_id(content)}")
    )
    stale = version_dir.parent / ".tmp-crash-leftover"
    stale.mkdir()
    (stale / "payload").write_bytes(b"uncommitted bytes")

    restarted = SourceArtifactStore(root)

    assert not stale.exists()
    assert restarted._cached_physical_usage_bytes == restarted._physical_usage_bytes()


def test_artifact_cross_directory_moves_fsync_both_parents(tmp_path, monkeypatch):
    root = tmp_path / "artifacts"
    store = SourceArtifactStore(root)
    content = b"durable move"
    version_id = _version_id(content)
    _put(store, content)
    version_dir = next(root.glob(f"tenant-*/kb-*/sources/src-one/{version_id}"))
    calls = []
    monkeypatch.setattr(store, "_fsync_directory", calls.append)

    deleted = store.delete_version("tenant", "kb", "src-one", version_id)
    assert calls == [version_dir.parent, root / ".trash"]

    calls.clear()
    store.restore("tenant", "kb", deleted["recovery_token"])
    assert calls == [version_dir.parent, root / ".trash"]


def test_artifact_store_enforces_file_version_and_total_bounds(tmp_path):
    file_limited = SourceArtifactStore(tmp_path / "file-limited", max_file_bytes=3)
    with pytest.raises(ArtifactLimitError, match="max_file_bytes"):
        _put(file_limited, b"four")

    version_limited = SourceArtifactStore(
        tmp_path / "version-limited", max_versions_per_source=1
    )
    _put(version_limited, b"one")
    with pytest.raises(ArtifactLimitError, match="max_versions_per_source"):
        _put(version_limited, b"two")

    total_limited = SourceArtifactStore(tmp_path / "total-limited", max_total_bytes=1)
    with pytest.raises(ArtifactLimitError, match="max_total_bytes"):
        _put(total_limited, b"x")


def test_artifact_batch_reservation_is_idempotent_and_put_consumes_once(tmp_path):
    store = SourceArtifactStore(tmp_path / "artifacts", max_total_bytes=100_000)
    content = b"reserved immutable content"
    item = _reservation_item(content)
    initial_physical = store._cached_physical_usage_bytes

    token = store.reserve_batch("tenant", "kb", [item], reservation_key="sync-job-one")
    reserved = store.reservation_usage("tenant", "kb")
    assert reserved["reservations"] == 1
    assert reserved["reserved_versions"] == 1
    assert reserved["reserved_bytes"] > len(content)
    assert (
        store.reserve_batch(
            "tenant", "kb", [dict(item)], reservation_key="sync-job-one"
        )
        == token
    )
    assert store.reservation_usage("tenant", "kb") == reserved

    created = _put(store, content, created_at=1.0, reservation_token=token)
    assert created["version_id"] == item["version_id"]
    assert store._cached_physical_usage_bytes == (
        initial_physical + reserved["reserved_bytes"]
    )
    assert store.reservation_usage("tenant", "kb") == {
        "reservations": 1,
        "reserved_versions": 0,
        "reserved_bytes": 0,
    }

    # Journal replay sees the existing immutable artifact and the consumed
    # entry; neither physical nor reserved usage is charged again.
    assert (
        store.reserve_batch(
            "tenant", "kb", [dict(item)], reservation_key="sync-job-one"
        )
        == token
    )
    assert _put(store, content, created_at=1.0, reservation_token=token) == created
    assert store._cached_physical_usage_bytes == (
        initial_physical + reserved["reserved_bytes"]
    )
    store.release_reservation(token)
    store.release_reservation(token)
    assert store.reservation_usage("tenant", "kb") == {
        "reservations": 0,
        "reserved_versions": 0,
        "reserved_bytes": 0,
    }


def test_artifact_batch_reservation_rejects_key_reuse_with_another_batch(tmp_path):
    store = SourceArtifactStore(tmp_path / "artifacts")
    store.reserve_batch(
        "tenant",
        "kb",
        [_reservation_item(b"one")],
        reservation_key="sync-job-one",
    )

    with pytest.raises(ArtifactConflictError, match="another batch"):
        store.reserve_batch(
            "tenant",
            "kb",
            [_reservation_item(b"two")],
            reservation_key="sync-job-one",
        )


def test_existing_version_reservation_blocks_delete_and_prune_until_replay(
    tmp_path,
):
    store = SourceArtifactStore(tmp_path / "artifacts")
    contents = (b"oldest", b"middle", b"newest")
    for created_at, content in enumerate(contents, start=1):
        _put(store, content, created_at=float(created_at))
    oldest_version = _version_id(contents[0])
    token = store.reserve_batch(
        "tenant",
        "kb",
        [_reservation_item(contents[0], created_at=1.0)],
        reservation_key="replay-existing",
    )

    assert store.reservation_usage("tenant", "kb") == {
        "reservations": 1,
        "reserved_versions": 0,
        "reserved_bytes": 0,
    }
    with pytest.raises(ArtifactConflictError, match="reserved"):
        store.delete_version("tenant", "kb", "src-one", oldest_version)
    with pytest.raises(ArtifactConflictError, match="reserved"):
        store.prune_versions("tenant", "kb", "src-one", keep_latest=1)
    assert len(store.list_versions("tenant", "kb", "src-one")) == 3
    assert store.usage("tenant", "kb")["trash_versions"] == 0

    replayed = _put(
        store,
        contents[0],
        created_at=1.0,
        reservation_token=token,
    )
    assert replayed["version_id"] == oldest_version
    assert (
        "tenant",
        "kb",
        "src-one",
        oldest_version,
    ) not in store._reserved_artifact_owners
    assert store.delete_version("tenant", "kb", "src-one", oldest_version)["deleted"]


def test_delete_scope_releases_partially_consumed_batch_reservation(tmp_path):
    store = SourceArtifactStore(tmp_path / "artifacts", max_total_bytes=100_000)
    first_content = b"already materialized"
    second_content = b"still pending"
    first_item = _reservation_item(first_content, source_id="src-first")
    second_item = _reservation_item(second_content, source_id="src-second")
    token = store.reserve_batch(
        "tenant",
        "kb",
        [first_item, second_item],
        reservation_key="partially-consumed",
    )
    _put(
        store,
        first_content,
        source_id="src-first",
        created_at=1.0,
        reservation_token=token,
    )
    assert store.reservation_usage("tenant", "kb")["reserved_versions"] == 1

    deleted = store.delete_scope("tenant", "kb")

    assert deleted["active_versions"] == 1
    assert store.reservation_usage("tenant", "kb") == {
        "reservations": 0,
        "reserved_versions": 0,
        "reserved_bytes": 0,
    }
    assert store._reserved_physical_usage_bytes == 0
    assert "tenant" not in store._reserved_physical_usage_bytes_by_tenant
    assert not any(
        owner[:2] == ("tenant", "kb") for owner in store._reserved_artifact_owners
    )
    store.release_reservation(token)
    with pytest.raises(ArtifactConflictError, match="unavailable"):
        _put(
            store,
            second_content,
            source_id="src-second",
            created_at=1.0,
            reservation_token=token,
        )


def test_concurrent_artifact_reservations_cannot_overbook_global_cap(tmp_path):
    store = SourceArtifactStore(tmp_path / "artifacts", max_total_bytes=100_000)
    content_a = b"tenant-a-payload"
    content_b = b"tenant-b-payload"
    item_a = _reservation_item(content_a, source_id="src-a")
    item_b = _reservation_item(content_b, source_id="src-b")

    probe = store.reserve_batch("tenant-a", "kb", [item_a], reservation_key="probe-job")
    one_batch_bytes = store.reservation_usage("tenant-a", "kb")["reserved_bytes"]
    store.release_reservation(probe)
    store.max_total_bytes = store._cached_physical_usage_bytes + one_batch_bytes
    barrier = Barrier(2)

    def reserve(tenant_id, item, reservation_key):
        barrier.wait()
        try:
            return store.reserve_batch(
                tenant_id,
                "kb",
                [item],
                reservation_key=reservation_key,
            )
        except ArtifactLimitError:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = (
            executor.submit(reserve, "tenant-a", item_a, "sync-job-a"),
            executor.submit(reserve, "tenant-b", item_b, "sync-job-b"),
        )
        tokens = [future.result() for future in futures]

    assert sum(token is not None for token in tokens) == 1
    assert (
        sum(
            store.reservation_usage(tenant, "kb")["reserved_versions"]
            for tenant in ("tenant-a", "tenant-b")
        )
        == 1
    )
    winner = next(token for token in tokens if token is not None)
    store.release_reservation(winner)
    assert store.reserve_batch(
        "tenant-b", "kb", [item_b], reservation_key="retry-job-b"
    )


def test_artifact_reservations_hold_version_slots_until_release(tmp_path):
    store = SourceArtifactStore(tmp_path / "artifacts", max_versions_per_source=1)
    first = store.reserve_batch(
        "tenant",
        "kb",
        [_reservation_item(b"one")],
        reservation_key="sync-job-one",
    )

    with pytest.raises(ArtifactLimitError, match="max_versions_per_source"):
        store.reserve_batch(
            "tenant",
            "kb",
            [_reservation_item(b"two")],
            reservation_key="sync-job-two",
        )
    store.release_reservation(first)
    assert store.reserve_batch(
        "tenant",
        "kb",
        [_reservation_item(b"two")],
        reservation_key="sync-job-two",
    )


def test_reserved_capacity_blocks_unreserved_put_without_double_counting(tmp_path):
    store = SourceArtifactStore(tmp_path / "artifacts", max_total_bytes=100_000)
    reserved_content = b"reserved payload"
    item = _reservation_item(reserved_content, source_id="src-reserved")
    token = store.reserve_batch("tenant-a", "kb", [item], reservation_key="sync-job-a")
    reserved_bytes = store.reservation_usage("tenant-a", "kb")["reserved_bytes"]
    store.max_total_bytes = store._cached_physical_usage_bytes + reserved_bytes

    with pytest.raises(ArtifactLimitError, match="max_total_bytes"):
        _put(
            store,
            b"other payload",
            tenant_id="tenant-b",
            source_id="src-other",
        )

    _put(
        store,
        reserved_content,
        tenant_id="tenant-a",
        source_id="src-reserved",
        created_at=1.0,
        reservation_token=token,
    )
    assert store._cached_physical_usage_bytes == store.max_total_bytes
    assert store.reservation_usage("tenant-a", "kb")["reserved_bytes"] == 0


def test_per_tenant_cap_isolated_from_global_cap_and_survives_restart(tmp_path):
    root = tmp_path / "artifacts"
    store = SourceArtifactStore(
        root,
        max_total_bytes=100_000,
        max_bytes_per_tenant=100_000,
    )
    first = b"tenant payload one"
    second = b"tenant payload two"
    probe = store.reserve_batch(
        "tenant-a",
        "kb",
        [_reservation_item(first)],
        reservation_key="size-probe",
    )
    one_artifact_bytes = store.reservation_usage("tenant-a", "kb")["reserved_bytes"]
    store.release_reservation(probe)
    store.max_bytes_per_tenant = one_artifact_bytes

    token_a = store.reserve_batch(
        "tenant-a",
        "kb",
        [_reservation_item(first)],
        reservation_key="tenant-a-first",
    )
    _put(store, first, tenant_id="tenant-a", created_at=1.0, reservation_token=token_a)
    assert store._cached_physical_usage_bytes_by_tenant["tenant-a"] == (
        one_artifact_bytes
    )
    with pytest.raises(ArtifactLimitError, match="max_bytes_per_tenant"):
        _put(store, second, tenant_id="tenant-a", created_at=1.0)
    with pytest.raises(ArtifactLimitError, match="max_bytes_per_tenant"):
        store.reserve_batch(
            "tenant-a",
            "kb",
            [_reservation_item(second)],
            reservation_key="tenant-a-second",
        )

    token_b = store.reserve_batch(
        "tenant-b",
        "kb",
        [_reservation_item(first)],
        reservation_key="tenant-b-first",
    )
    _put(store, first, tenant_id="tenant-b", created_at=1.0, reservation_token=token_b)
    assert store._cached_physical_usage_bytes_by_tenant == {
        "tenant-a": one_artifact_bytes,
        "tenant-b": one_artifact_bytes,
    }

    restarted = SourceArtifactStore(
        root,
        max_total_bytes=100_000,
        max_bytes_per_tenant=one_artifact_bytes,
    )
    assert restarted._cached_physical_usage_bytes_by_tenant == {
        "tenant-a": one_artifact_bytes,
        "tenant-b": one_artifact_bytes,
    }
    restarted.max_total_bytes = restarted._cached_physical_usage_bytes
    with pytest.raises(ArtifactLimitError, match="max_total_bytes"):
        restarted.reserve_batch(
            "tenant-c",
            "kb",
            [_reservation_item(first)],
            reservation_key="global-cap",
        )

    restarted.delete_scope("tenant-a", "kb")
    assert "tenant-a" not in restarted._cached_physical_usage_bytes_by_tenant
    assert restarted._cached_physical_usage_bytes_by_tenant["tenant-b"] == (
        one_artifact_bytes
    )
    assert restarted.reserve_batch(
        "tenant-a",
        "kb",
        [_reservation_item(second)],
        reservation_key="tenant-a-after-delete",
    )


def test_artifact_store_detects_persisted_content_tampering(tmp_path):
    root = tmp_path / "artifacts"
    store = SourceArtifactStore(root)
    content = b"original"
    version_id = _version_id(content)
    _put(store, content)
    payload = next(root.glob(f"tenant-*/kb-*/sources/src-one/{version_id}/payload"))
    payload.write_bytes(b"tampered")

    with pytest.raises(ArtifactIntegrityError):
        store.read("tenant", "kb", "src-one", version_id)
    with pytest.raises(ArtifactIntegrityError):
        store.get_metadata("tenant", "kb", "src-one", version_id, verify_content=True)


def test_artifact_store_rejects_joint_payload_and_metadata_tampering(tmp_path):
    root = tmp_path / "artifacts"
    store = SourceArtifactStore(root)
    original = b"original"
    version_id = _version_id(original)
    _put(store, original)
    version_dir = next(root.glob(f"tenant-*/kb-*/sources/src-one/{version_id}"))
    tampered = b"coordinated tamper"
    (version_dir / "payload").write_bytes(tampered)
    metadata_path = version_dir / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["content_sha256"] = _digest(tampered)
    metadata["byte_size"] = len(tampered)
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    with pytest.raises(ArtifactIntegrityError, match="content address"):
        store.read("tenant", "kb", "src-one", version_id)


def test_artifact_store_returns_bounded_text_diff_and_binary_metadata(tmp_path):
    store = SourceArtifactStore(
        tmp_path / "artifacts", max_diff_bytes=512, max_diff_lines=8
    )
    before = b"heading\nold line\ntail\n"
    after = b"heading\nnew line\ntail\n"
    version_one = _version_id(before)
    version_two = _version_id(after)
    _put(store, before, created_at=1)
    _put(store, after, created_at=2)

    result = store.diff("tenant", "kb", "src-one", version_one, version_two)
    assert result["kind"] == "text"
    assert "-old line" in result["diff"]
    assert "+new line" in result["diff"]
    assert len(result["diff"].encode("utf-8")) <= 512
    assert len(result["diff"].splitlines()) <= 8
    assert result["from"]["version_id"] == version_one
    assert result["to"]["version_id"] == version_two

    binary_before = b"\x00\x01"
    binary_after = b"\x00\x02"
    binary_one = _version_id(binary_before, "src-binary")
    binary_two = _version_id(binary_after, "src-binary")
    _put(
        store,
        binary_before,
        source_id="src-binary",
        media_type="application/octet-stream",
    )
    _put(
        store,
        binary_after,
        source_id="src-binary",
        media_type="application/octet-stream",
    )
    binary = store.diff("tenant", "kb", "src-binary", binary_one, binary_two)
    assert binary["kind"] == "binary"
    assert binary["diff"] is None
    assert binary["from"]["byte_size"] == 2
    assert binary["to"]["content_sha256"] == _digest(b"\x00\x02")


def test_artifact_diff_marks_large_text_as_truncated(tmp_path):
    store = SourceArtifactStore(
        tmp_path / "artifacts", max_diff_bytes=128, max_diff_lines=5
    )
    before = ("old\n" * 100).encode()
    after = ("new\n" * 100).encode()
    version_one = _version_id(before)
    version_two = _version_id(after)
    _put(store, before)
    _put(store, after)

    result = store.diff("tenant", "kb", "src-one", version_one, version_two)
    assert result["kind"] == "text"
    assert result["truncated"] is True
    assert len(result["diff"].encode("utf-8")) <= 128
    assert len(result["diff"].splitlines()) <= 5


def test_artifact_diff_streams_full_verification_with_bounded_prefixes(
    tmp_path, monkeypatch
):
    store = SourceArtifactStore(
        tmp_path / "artifacts",
        max_file_bytes=2 * 1024 * 1024,
        max_diff_bytes=96,
        max_diff_lines=6,
    )
    before = b"same prefix\n" + (b"old payload line\n" * 32_000)
    after = b"same prefix\n" + (b"new payload line\n" * 32_000)
    before_version = _version_id(before)
    after_version = _version_id(after)
    _put(store, before, created_at=1.0)
    _put(store, after, created_at=2.0)
    original_prefix_reader = store._read_verified_payload_prefix
    retained: list[tuple[int, int]] = []

    def tracked_prefix_reader(directory, metadata, *, prefix_bytes):
        content, truncated = original_prefix_reader(
            directory,
            metadata,
            prefix_bytes=prefix_bytes,
        )
        retained.append((prefix_bytes, len(content)))
        return content, truncated

    def unexpected_full_read(*args, **kwargs):
        raise AssertionError("diff must not call the full-payload read API")

    monkeypatch.setattr(store, "_read_verified_payload_prefix", tracked_prefix_reader)
    monkeypatch.setattr(store, "read", unexpected_full_read)
    monkeypatch.setattr(store, "_read_verified_payload", unexpected_full_read)

    result = store.diff("tenant", "kb", "src-one", before_version, after_version)

    assert result["kind"] == "text"
    assert result["truncated"] is True
    assert retained == [(96, 96), (96, 96)]

    after_payload = next(
        (tmp_path / "artifacts").glob(
            f"tenant-*/kb-*/sources/src-one/{after_version}/payload"
        )
    )
    tampered = bytearray(after_payload.read_bytes())
    tampered[-1] ^= 1
    after_payload.write_bytes(tampered)
    with pytest.raises(ArtifactIntegrityError, match="hash"):
        store.diff("tenant", "kb", "src-one", before_version, after_version)


def test_artifact_delete_restore_prune_and_explicit_purge_are_scoped(tmp_path):
    store = SourceArtifactStore(tmp_path / "artifacts")
    version_one = _version_id(b"one")
    version_two = _version_id(b"two")
    version_three = _version_id(b"three")
    _put(store, b"one", created_at=1)
    _put(store, b"two", created_at=2)
    _put(store, b"three", created_at=3)

    deleted = store.delete_version("tenant", "kb", "src-one", version_one)
    assert store.list_versions("tenant", "kb", "src-one") == [
        store.get_metadata("tenant", "kb", "src-one", version_three),
        store.get_metadata("tenant", "kb", "src-one", version_two),
    ]
    with pytest.raises(ArtifactNotFoundError):
        store.restore("other-tenant", "kb", deleted["recovery_token"])
    restored = store.restore("tenant", "kb", deleted["recovery_token"])
    assert restored["version_id"] == version_one

    pruned = store.prune_versions(
        "tenant",
        "kb",
        "src-one",
        keep_latest=1,
        protect_version_ids=(version_one,),
    )
    assert [row["metadata"]["version_id"] for row in pruned] == [
        version_three,
        version_two,
    ]
    usage = store.usage("tenant", "kb")
    assert usage["active_versions"] == 1
    assert usage["trash_versions"] == 2
    assert store.purge_trash("other-tenant", "kb", older_than=time.time() + 1) == 0
    assert store.purge_trash("tenant", "kb", older_than=time.time() + 1) == 2
    assert store.usage("tenant", "kb")["trash_versions"] == 0
    assert store._cached_physical_usage_bytes_by_tenant == (
        store._physical_usage_bytes_by_tenant()
    )


def test_artifact_restore_respects_active_version_limit(tmp_path):
    store = SourceArtifactStore(
        tmp_path / "artifacts",
        max_versions_per_source=2,
        user_max_versions_per_source=1,
    )
    version_one = _version_id(b"one")
    _put(store, b"one")
    deleted = store.delete_version("tenant", "kb", "src-one", version_one)
    _put(store, b"two")

    with pytest.raises(ArtifactLimitError, match="user_max_versions_per_source"):
        store.restore("tenant", "kb", deleted["recovery_token"])
    assert store.usage("tenant", "kb")["trash_versions"] == 1


def test_artifact_many_document_puts_use_cached_global_usage(tmp_path, monkeypatch):
    store = SourceArtifactStore(tmp_path / "artifacts", max_total_bytes=1_000_000)
    physical_usage_calls = 0
    tenant_usage_calls = 0
    original_physical_usage = store._physical_usage_bytes
    original_tenant_usage = store._physical_usage_bytes_by_tenant

    def counted_physical_usage():
        nonlocal physical_usage_calls
        physical_usage_calls += 1
        return original_physical_usage()

    def counted_tenant_usage():
        nonlocal tenant_usage_calls
        tenant_usage_calls += 1
        return original_tenant_usage()

    monkeypatch.setattr(store, "_physical_usage_bytes", counted_physical_usage)
    monkeypatch.setattr(store, "_physical_usage_bytes_by_tenant", counted_tenant_usage)
    for index in range(50):
        _put(
            store,
            f"document-{index}".encode(),
            source_id=f"src-{index}",
            created_at=float(index),
        )

    assert physical_usage_calls == 0
    assert tenant_usage_calls == 0
    assert store.check() is True
    assert physical_usage_calls == 0
    assert tenant_usage_calls == 0

    store.max_total_bytes = store._cached_physical_usage_bytes
    with pytest.raises(ArtifactLimitError, match="max_total_bytes"):
        _put(store, b"over quota", source_id="src-over-quota")
    assert physical_usage_calls == 0
    assert tenant_usage_calls == 0


def test_artifact_delete_scope_removes_active_and_trash_but_preserves_peers(
    tmp_path,
):
    store = SourceArtifactStore(tmp_path / "artifacts")
    target_active = b"target active"
    target_deleted = b"target deleted"
    _put(store, target_active, source_id="src-target-active")
    _put(store, target_deleted, source_id="src-target-deleted")
    target_recovery = store.delete_version(
        "tenant",
        "kb",
        "src-target-deleted",
        _version_id(target_deleted, "src-target-deleted"),
    )["recovery_token"]

    peer_active = b"peer active"
    peer_deleted = b"peer deleted"
    _put(
        store,
        peer_active,
        tenant_id="other-tenant",
        source_id="src-peer-active",
    )
    _put(
        store,
        peer_deleted,
        tenant_id="other-tenant",
        source_id="src-peer-deleted",
    )
    peer_recovery = store.delete_version(
        "other-tenant",
        "kb",
        "src-peer-deleted",
        _version_id(peer_deleted, "src-peer-deleted"),
    )["recovery_token"]
    same_tenant_other_kb = b"other kb"
    _put(
        store,
        same_tenant_other_kb,
        kb_id="other-kb",
        source_id="src-other-kb",
    )

    deleted = store.delete_scope("tenant", "kb")

    assert deleted["active_versions"] == 1
    assert deleted["trash_versions"] == 1
    assert deleted["freed_bytes"] > 0
    assert store.usage("tenant", "kb") == {
        "active_bytes": 0,
        "active_versions": 0,
        "trash_bytes": 0,
        "trash_versions": 0,
    }
    with pytest.raises(ArtifactNotFoundError):
        store.read(
            "tenant",
            "kb",
            "src-target-active",
            _version_id(target_active, "src-target-active"),
        )
    with pytest.raises(ArtifactNotFoundError):
        store.restore("tenant", "kb", target_recovery)
    assert (
        store.read(
            "other-tenant",
            "kb",
            "src-peer-active",
            _version_id(peer_active, "src-peer-active"),
        )
        == peer_active
    )
    assert store.restore("other-tenant", "kb", peer_recovery)[
        "content_sha256"
    ] == _digest(peer_deleted)
    assert (
        store.read(
            "tenant",
            "other-kb",
            "src-other-kb",
            _version_id(same_tenant_other_kb, "src-other-kb"),
        )
        == same_tenant_other_kb
    )
    assert store.delete_scope("tenant", "kb") == {
        "active_versions": 0,
        "trash_versions": 0,
        "freed_bytes": 0,
    }
    assert store._cached_physical_usage_bytes == store._physical_usage_bytes()
    assert store._cached_physical_usage_bytes_by_tenant == (
        store._physical_usage_bytes_by_tenant()
    )


def test_artifact_delete_scope_fails_closed_on_unverifiable_trash(tmp_path):
    root = tmp_path / "artifacts"
    store = SourceArtifactStore(root)
    active_content = b"must remain until deletion can be proven complete"
    deleted_content = b"orphan candidate"
    _put(store, active_content, source_id="src-active")
    _put(store, deleted_content, source_id="src-deleted")
    recovery_token = store.delete_version(
        "tenant",
        "kb",
        "src-deleted",
        _version_id(deleted_content, "src-deleted"),
    )["recovery_token"]
    trash_dir = root / ".trash" / recovery_token
    (trash_dir / "metadata.json").unlink()

    with pytest.raises(ArtifactIntegrityError, match="unverifiable"):
        store.delete_scope("tenant", "kb")

    assert trash_dir.is_dir()
    assert (
        store.read(
            "tenant",
            "kb",
            "src-active",
            _version_id(active_content, "src-active"),
        )
        == active_content
    )
    assert store._cached_physical_usage_bytes == store._physical_usage_bytes()


def test_artifact_restore_rejects_tampered_trash_payload(tmp_path):
    root = tmp_path / "artifacts"
    store = SourceArtifactStore(root)
    version_id = _version_id(b"original")
    _put(store, b"original")
    deleted = store.delete_version("tenant", "kb", "src-one", version_id)
    payload = root / ".trash" / deleted["recovery_token"] / "payload"
    payload.write_bytes(b"tampered")

    with pytest.raises(ArtifactIntegrityError, match="hash"):
        store.restore("tenant", "kb", deleted["recovery_token"])
    assert store.list_versions("tenant", "kb", "src-one") == []
    assert store.usage("tenant", "kb")["trash_versions"] == 1


def test_artifact_restore_rejects_joint_payload_and_metadata_tampering(tmp_path):
    root = tmp_path / "artifacts"
    store = SourceArtifactStore(root)
    original = b"original"
    version_id = _version_id(original)
    _put(store, original)
    deleted = store.delete_version("tenant", "kb", "src-one", version_id)
    trash_dir = root / ".trash" / deleted["recovery_token"]
    tampered = b"coordinated tamper"
    (trash_dir / "payload").write_bytes(tampered)
    metadata_path = trash_dir / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["content_sha256"] = _digest(tampered)
    metadata["byte_size"] = len(tampered)
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    with pytest.raises(ArtifactIntegrityError, match="content address"):
        store.restore("tenant", "kb", deleted["recovery_token"])
    assert store.list_versions("tenant", "kb", "src-one") == []
    assert trash_dir.is_dir()
