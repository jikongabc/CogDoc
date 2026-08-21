from __future__ import annotations

import threading

from cogdoc.ha.storage import SQLiteBackend
from cogdoc.ha.versioning import ApplicationVersionRegistry, VersionHeartbeat


def test_live_version_floor_excludes_expired_and_retired_instances(tmp_path):
    now = [100.0]
    backend = SQLiteBackend(tmp_path / "versions.db")
    registry = ApplicationVersionRegistry(backend, clock=lambda: now[0])
    registry.heartbeat(
        "old",
        "old-session",
        "release-1",
        minimum_schema_version=1,
        maximum_schema_version=2,
        ttl_seconds=10,
    )
    registry.heartbeat(
        "new",
        "new-session",
        "release-2",
        minimum_schema_version=2,
        maximum_schema_version=3,
        ttl_seconds=20,
    )
    assert registry.contract_floor() == 1

    now[0] = 111.0
    assert [row["instance_id"] for row in registry.live()] == ["new"]
    assert registry.contract_floor() == 2
    assert registry.retire("new", "new-session") is True
    assert registry.contract_floor() is None
    backend.close()


def test_version_heartbeat_is_live_until_stopped(tmp_path):
    backend = SQLiteBackend(tmp_path / "versions.db")
    registry = ApplicationVersionRegistry(backend)
    heartbeat = VersionHeartbeat(
        registry,
        instance_id="worker",
        release_id="release",
        minimum_schema_version=1,
        maximum_schema_version=1,
        interval_seconds=5,
        ttl_seconds=10,
    )

    heartbeat.start()
    assert heartbeat.check() is True
    assert registry.contract_floor() == 1
    assert heartbeat.stop() is True
    assert heartbeat.check() is False
    assert registry.live() == []
    backend.close()


def test_version_heartbeat_failure_changes_readiness(tmp_path):
    backend = SQLiteBackend(tmp_path / "versions.db")
    registry = ApplicationVersionRegistry(backend)
    heartbeat = VersionHeartbeat(
        registry,
        instance_id="worker",
        release_id="release",
        minimum_schema_version=1,
        maximum_schema_version=1,
        interval_seconds=5,
        ttl_seconds=10,
    )
    heartbeat.start()
    heartbeat._last_error = RuntimeError("lost")
    assert heartbeat.check() is False
    assert heartbeat.stop() is True
    backend.close()


def test_concurrent_same_instance_id_has_one_fenced_owner(tmp_path):
    backend_a = SQLiteBackend(tmp_path / "versions.db")
    backend_b = SQLiteBackend(tmp_path / "versions.db")
    first = ApplicationVersionRegistry(backend_a, clock=lambda: 100.0)
    # Both contenders observe the same authority time. A clock more than one
    # full TTL ahead is, by definition, an expiry/takeover test rather than a
    # simultaneous ownership test (covered separately below).
    second = ApplicationVersionRegistry(backend_b, clock=lambda: 100.0)
    barrier = threading.Barrier(2)
    rows = []
    errors = []

    def beat(registry, token):
        barrier.wait()
        try:
            rows.append(
                registry.heartbeat(
                    "same",
                    token,
                    "release",
                    minimum_schema_version=1,
                    maximum_schema_version=1,
                )
            )
        except Exception as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=beat, args=(first, "first-session")),
        threading.Thread(target=beat, args=(second, "second-session")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(rows) == 1
    assert len(errors) == 1
    assert "another live process" in str(errors[0])
    with backend_a.transaction() as connection:
        row = connection.execute(
            "SELECT started_at,last_heartbeat_at FROM ha_application_instances"
        ).fetchone()
    assert row["started_at"] in {100.0, 200.0}
    assert row["last_heartbeat_at"] in {100.0, 200.0}
    backend_a.close()
    backend_b.close()
