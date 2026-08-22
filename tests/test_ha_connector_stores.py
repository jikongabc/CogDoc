from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from cogdoc.connectors.connection_store import ConnectionLimitError, ConnectionStore
from cogdoc.connectors.credential_store import (
    CredentialVaultError,
    CredentialRevisionConflict,
    CredentialVault,
)
from cogdoc.connectors.oauth import OAuthReplayError, OAuthSessionStore
from cogdoc.connectors.sync_store import ConnectorSyncStore
from cogdoc.ha.storage import SQLiteBackend


def _connection(store: ConnectionStore, name: str = "web"):
    return store.create(
        tenant_id="tenant",
        kb_id="kb",
        connector_type="url",
        name=name,
        config={"urls": ["https://example.com/docs"]},
        secret_env={},
        owner_id="owner",
    )


def test_distributed_connection_store_is_visible_and_cas_safe_across_nodes(
    tmp_path: Path,
) -> None:
    path = tmp_path / "control.db"
    first_backend = SQLiteBackend(path)
    second_backend = SQLiteBackend(path)
    first = ConnectionStore(backend=first_backend)
    second = ConnectionStore(backend=second_backend)

    created = _connection(first)
    observed = second.get(created["connection_id"], include_secret_refs=True)
    assert observed is not None and observed["revision"] == 1
    updated = second.set_enabled(created["connection_id"], False)
    assert updated["revision"] == 2 and updated["enabled"] is False
    fenced = first.fence_delete("tenant", "kb", created["connection_id"])
    assert fenced["revision"] == 3 and fenced["deleting"] is True
    with pytest.raises(ValueError, match="deletion"):
        second.set_enabled(created["connection_id"], True)

    first.close()
    second.close()
    assert first_backend.check() and second_backend.check()
    first_backend.close()
    second_backend.close()


def test_distributed_connection_limits_are_atomic_across_nodes(tmp_path: Path) -> None:
    path = tmp_path / "control.db"
    backends = (SQLiteBackend(path), SQLiteBackend(path))
    stores = tuple(
        ConnectionStore(
            backend=backend,
            max_connections_global=2,
            max_connections_per_tenant=2,
            max_connections_per_kb=1,
        )
        for backend in backends
    )

    def create(index: int) -> str:
        try:
            return str(_connection(stores[index], f"web-{index}")["connection_id"])
        except ConnectionLimitError:
            return "limited"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(create, (0, 1)))
    assert sum(result == "limited" for result in results) == 1
    assert len(stores[0].list_entries("tenant", "kb")) == 1
    for backend in backends:
        backend.close()


def test_distributed_vault_rejects_same_version_key_drift(tmp_path: Path) -> None:
    backend = SQLiteBackend(tmp_path / "control.db")
    first = CredentialVault(
        backend=backend,
        master_keys={"v1": b"a" * 32},
        active_key_version="v1",
    )
    with pytest.raises(CredentialVaultError, match="differs across nodes"):
        CredentialVault(
            backend=backend,
            master_keys={"v1": b"b" * 32},
            active_key_version="v1",
        )
    first.close()
    backend.close()


def test_distributed_sync_store_reuses_one_active_job_and_recovers_on_other_node(
    tmp_path: Path,
) -> None:
    path = tmp_path / "control.db"
    first_backend = SQLiteBackend(path)
    second_backend = SQLiteBackend(path)
    first = ConnectorSyncStore(backend=first_backend)
    second = ConnectorSyncStore(backend=second_backend)
    arguments = {
        "tenant_id": "tenant",
        "kb_id": "kb",
        "connection_id": "conn-one",
        "connector_type": "url",
        "connection_revision": 1,
    }

    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = list(
            pool.map(lambda store: store.create_if_idle(**arguments), (first, second))
        )
    assert jobs[0]["job_id"] == jobs[1]["job_id"]
    acquired, token = second.acquire(jobs[0]["job_id"], lease_seconds=30)
    assert acquired["attempt"] == 1
    counters = {
        "pages_processed": 1,
        "documents_seen": 1,
        "documents_fetched": 1,
        "deleted_seen": 0,
        "bytes_fetched": 5,
    }
    second.checkpoint(
        acquired["job_id"], token, cursor="next", counters=counters, lease_seconds=30
    )
    first.prepare_commit(acquired["job_id"], token)
    completed = second.complete(
        acquired["job_id"], token, cursor="next", counters=counters
    )
    assert completed["status"] == "succeeded"
    assert first.checkpoint_for("tenant", "kb", "conn-one")["cursor"] == "next"
    assert (
        second.health_snapshot("tenant", "kb", "conn-one")["health_status"] == "healthy"
    )

    first.close()
    second.close()
    assert first_backend.check() and second_backend.check()
    first_backend.close()
    second_backend.close()


def test_distributed_credential_vault_keeps_ciphertext_and_revision_cas_shared(
    tmp_path: Path,
) -> None:
    path = tmp_path / "control.db"
    first_backend = SQLiteBackend(path)
    second_backend = SQLiteBackend(path)
    key = b"k" * 32
    first = CredentialVault(
        backend=first_backend,
        master_keys={"v1": key},
        active_key_version="v1",
        clock=lambda: 1_000.0,
    )
    second = CredentialVault(
        backend=second_backend,
        master_keys={"v1": key},
        active_key_version="v1",
        clock=lambda: 1_001.0,
    )
    created = first.create(
        tenant_id="tenant",
        kb_id="kb",
        connection_id="conn",
        provider="notion",
        credential_kind="manual",
        label="shared",
        secret_values={"token": "cluster-secret-marker"},
        actor_id="owner",
    )
    assert second.get_for_use(
        created["credential_id"],
        tenant_id="tenant",
        kb_id="kb",
        connection_id="conn",
        actor_id="worker-b",
    ) == {"token": "cluster-secret-marker"}
    rotated = second.rotate(
        created["credential_id"],
        tenant_id="tenant",
        kb_id="kb",
        connection_id="conn",
        actor_id="owner",
        secret_values={"token": "new-secret"},
        expected_revision=1,
    )
    assert rotated["revision"] == 2
    with pytest.raises(CredentialRevisionConflict):
        first.rotate(
            created["credential_id"],
            tenant_id="tenant",
            kb_id="kb",
            connection_id="conn",
            actor_id="owner",
            secret_values={"token": "stale"},
            expected_revision=1,
        )
    assert b"cluster-secret-marker" not in path.read_bytes()
    assert [
        event["action"] for event in reversed(first.audit_events("tenant", "kb"))
    ] == [
        "create",
        "use",
        "rotate",
    ]
    first.close()
    second.close()
    first_backend.close()
    second_backend.close()


def test_distributed_oauth_state_is_one_shot_across_nodes(tmp_path: Path) -> None:
    path = tmp_path / "control.db"
    first_backend = SQLiteBackend(path)
    second_backend = SQLiteBackend(path)
    key = b"o" * 32
    first_vault = CredentialVault(
        backend=first_backend,
        master_keys={"v1": key},
        active_key_version="v1",
        clock=lambda: 1_000.0,
    )
    second_vault = CredentialVault(
        backend=second_backend,
        master_keys={"v1": key},
        active_key_version="v1",
        clock=lambda: 1_000.0,
    )
    first = OAuthSessionStore(
        None, first_vault, backend=first_backend, clock=lambda: 1_000.0
    )
    second = OAuthSessionStore(
        None, second_vault, backend=second_backend, clock=lambda: 1_000.0
    )
    session = first.create(
        provider="microsoft",
        tenant_id="tenant",
        kb_id="kb",
        connection_id="conn",
        user_id="owner",
        redirect_uri="https://cogdoc.example/oauth/callback",
    )

    consumed = second.consume_callback(session.state, provider="microsoft")
    assert consumed.session_id == session.session_id
    assert consumed.connection_id == "conn"
    with pytest.raises(OAuthReplayError):
        first.consume_callback(session.state, provider="microsoft")

    first.close()
    second.close()
    first_vault.close()
    second_vault.close()
    first_backend.close()
    second_backend.close()
