from __future__ import annotations

from pathlib import Path
import threading
from types import MethodType

import pytest

from cogdoc.api.auth_store import (
    AuthAuthenticationError,
    AuthConflictError,
    AuthLockedError,
    AuthStore,
)
from cogdoc.api.oidc import OIDCConfigurationError, OIDCFlowError, OIDCFlowStore
from cogdoc.api.resource_access import AccessMode, ResourceAccessStore
from cogdoc.api.resource_access import ResourceAccessConflictError
from cogdoc.api.tenancy import Permission, Principal, Role
from cogdoc.ha.storage import SQLiteBackend
from cogdoc.ha.identity_config import DistributedIdentityConfigRegistry
from cogdoc.service.external_acl import ExternalAclSyncStore


PASSWORD = "correct horse battery staple"


def test_shared_auth_sessions_and_service_tokens_are_cross_node(tmp_path: Path) -> None:
    first_backend = SQLiteBackend(tmp_path / "shared.db")
    second_backend = SQLiteBackend(tmp_path / "shared.db")
    first = AuthStore(None, backend=first_backend, scrypt_n=1 << 10)
    second = AuthStore(None, backend=second_backend, scrypt_n=1 << 10)

    registration = first.register("alice@example.com", PASSWORD, "Alice")
    token = str(registration["access_token"])
    user_id = str(registration["user"]["user_id"])
    workspace_id = str(registration["workspace"]["workspace_id"])
    context = second.authenticate_session(token)
    assert context.principal.subject_id == user_id

    account = first.create_service_account(
        workspace_id=workspace_id,
        name="Indexer",
        actor_user_id=user_id,
    )
    issued = first.create_service_token(
        workspace_id=workspace_id,
        service_account_id=str(account["service_account_id"]),
        label="worker",
        ttl_seconds=3600,
        actor_user_id=user_id,
    )
    service = second.authenticate_service_token(str(issued["token"]), workspace_id)
    assert service.principal.subject_id == (
        f"service-account:{account['service_account_id']}"
    )

    assert second.revoke_session(user_id, str(registration["session"]["session_id"]))
    with pytest.raises(AuthAuthenticationError):
        first.authenticate_session(token)
    first.close()
    second.close()
    first_backend.close()
    second_backend.close()


def test_shared_acl_epoch_and_revocation_are_immediately_visible(tmp_path: Path) -> None:
    first_backend = SQLiteBackend(tmp_path / "shared.db")
    second_backend = SQLiteBackend(tmp_path / "shared.db")
    first = ResourceAccessStore(None, backend=first_backend)
    second = ResourceAccessStore(None, backend=second_backend)
    principal = Principal("tenant", "alice", Role.VIEWER, "fingerprint", "mem-one")

    first.set_kb_policy(
        "tenant",
        "docs",
        "owner",
        "private",
        owner_membership_id="mem-owner",
    )
    first.grant_subject(
        "tenant", "docs", "alice", Role.VIEWER, membership_id="mem-one"
    )
    decision = second.allowed_sources(
        principal, "docs", tenant_id="tenant", permission=Permission.READ
    )
    assert decision.mode is AccessMode.ALL
    epoch = second.acl_epoch("tenant", "docs")

    second.revoke_all_subject_grants(
        "tenant", "alice", membership_id="mem-one"
    )
    assert first.acl_epoch("tenant", "docs") > epoch
    assert (
        first.allowed_sources(
            principal, "docs", tenant_id="tenant", permission=Permission.READ
        ).mode
        is AccessMode.DENY
    )
    first.close()
    second.close()
    first_backend.close()
    second_backend.close()


def test_shared_oidc_state_is_one_shot_and_key_drift_fails_closed(
    tmp_path: Path,
) -> None:
    first_backend = SQLiteBackend(tmp_path / "shared.db")
    second_backend = SQLiteBackend(tmp_path / "shared.db")
    first = OIDCFlowStore(None, b"a" * 32, backend=first_backend)
    second = OIDCFlowStore(None, b"a" * 32, backend=second_backend)
    flow = first.create(intent="login", return_url="/login")
    assert second.consume_state(flow.state).flow_id == flow.flow_id
    with pytest.raises(OIDCFlowError):
        first.consume_state(flow.state)
    with pytest.raises(OIDCConfigurationError):
        OIDCFlowStore(None, b"b" * 32, backend=second_backend)
    first.close()
    second.close()
    first_backend.close()
    second_backend.close()


def test_external_acl_checkpoint_is_shared(tmp_path: Path) -> None:
    first_backend = SQLiteBackend(tmp_path / "shared.db")
    second_backend = SQLiteBackend(tmp_path / "shared.db")
    first = ExternalAclSyncStore(None, backend=first_backend)
    second = ExternalAclSyncStore(None, backend=second_backend)
    from cogdoc.service.external_acl import ExternalAclSnapshot

    snapshot = ExternalAclSnapshot(complete=False, workspace_visible=False)
    first.record(
        tenant_id="tenant",
        kb_id="docs",
        document_id="doc-one",
        managed_by="connector:one",
        status="quarantined",
        snapshot=snapshot,
        resolved_count=0,
        unresolved_count=1,
    )
    assert second.get("tenant", "docs", "doc-one", "connector:one")["status"] == (
        "quarantined"
    )
    first.close()
    second.close()
    first_backend.close()
    second_backend.close()


def test_identity_security_configuration_drift_is_rejected(tmp_path: Path) -> None:
    first_backend = SQLiteBackend(tmp_path / "shared.db")
    second_backend = SQLiteBackend(tmp_path / "shared.db")
    first = DistributedIdentityConfigRegistry(first_backend)
    second = DistributedIdentityConfigRegistry(second_backend)
    first.register("identity-plane-v1", 1, {"issuer": "https://id.example"})
    assert second.register(
        "identity-plane-v1", 1, {"issuer": "https://id.example"}
    )
    with pytest.raises(RuntimeError, match="differs across nodes"):
        second.register(
            "identity-plane-v1", 1, {"issuer": "https://other.example"}
        )
    assert second.register(
        "identity-plane-v1", 2, {"issuer": "https://other.example"}
    )
    with pytest.raises(RuntimeError, match="became stale"):
        first.check()
    with pytest.raises(RuntimeError, match="differs across nodes"):
        first.register("identity-plane-v1", 1, {"issuer": "https://id.example"})
    first_backend.close()
    second_backend.close()


def test_shared_registration_is_atomic_across_database_connections(
    tmp_path: Path,
) -> None:
    first_backend = SQLiteBackend(tmp_path / "shared.db")
    second_backend = SQLiteBackend(tmp_path / "shared.db")
    stores = (
        AuthStore(None, backend=first_backend, scrypt_n=1 << 10),
        AuthStore(None, backend=second_backend, scrypt_n=1 << 10),
    )
    barrier = threading.Barrier(2)
    results: list[str] = []

    def register(store: AuthStore) -> None:
        barrier.wait()
        try:
            store.register("same@example.com", PASSWORD, "Same")
        except AuthConflictError:
            results.append("conflict")
        else:
            results.append("created")

    threads = [threading.Thread(target=register, args=(store,)) for store in stores]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()
    assert sorted(results) == ["conflict", "created"]
    assert stores[0]._conn.execute("SELECT COUNT(*) FROM auth_users").fetchone()[0] == 1
    for store in stores:
        store.close()
    first_backend.close()
    second_backend.close()


def test_shared_failed_login_counter_cannot_lose_concurrent_attempts(
    tmp_path: Path,
) -> None:
    first_backend = SQLiteBackend(tmp_path / "shared.db")
    second_backend = SQLiteBackend(tmp_path / "shared.db")
    stores = (
        AuthStore(
            None, backend=first_backend, scrypt_n=1 << 10, max_failed_logins=2
        ),
        AuthStore(
            None, backend=second_backend, scrypt_n=1 << 10, max_failed_logins=2
        ),
    )
    stores[0].register("alice@example.com", PASSWORD, "Alice")
    barrier = threading.Barrier(2)
    failures: list[type[Exception]] = []

    def fail_login(store: AuthStore) -> None:
        barrier.wait()
        try:
            store.login("alice@example.com", "definitely-not-the-password")
        except (AuthAuthenticationError, AuthLockedError) as exc:
            failures.append(type(exc))

    threads = [threading.Thread(target=fail_login, args=(store,)) for store in stores]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()
    assert sorted(item.__name__ for item in failures) == [
        "AuthAuthenticationError",
        "AuthLockedError",
    ]
    row = stores[0]._conn.execute(
        "SELECT failed_login_count,locked_until FROM auth_users WHERE email=?",
        ("alice@example.com",),
    ).fetchone()
    assert row is not None and int(row[0]) == 2 and row[1] is not None
    for store in stores:
        store.close()
    first_backend.close()
    second_backend.close()


def test_membership_tombstone_serializes_with_delayed_old_grant(
    tmp_path: Path,
) -> None:
    first_backend = SQLiteBackend(tmp_path / "shared.db")
    second_backend = SQLiteBackend(tmp_path / "shared.db")
    first = ResourceAccessStore(None, backend=first_backend)
    second = ResourceAccessStore(None, backend=second_backend)
    first.set_kb_policy("tenant", "docs", "owner", "private")
    grant_checked = threading.Event()
    release_grant = threading.Event()
    revoke_finished = threading.Event()
    original = first._lock_subjects_locked

    def delayed_lock(self, tenant_id, subject_ids):
        original(tenant_id, subject_ids)
        grant_checked.set()
        assert release_grant.wait(5)

    first._lock_subjects_locked = MethodType(delayed_lock, first)

    def grant() -> None:
        first.grant_subject(
            "tenant", "docs", "alice", Role.VIEWER, membership_id="old-member"
        )

    def revoke() -> None:
        second.revoke_all_subject_grants(
            "tenant", "alice", membership_id="old-member"
        )
        revoke_finished.set()

    grant_thread = threading.Thread(target=grant)
    grant_thread.start()
    assert grant_checked.wait(5)
    revoke_thread = threading.Thread(target=revoke)
    revoke_thread.start()
    assert not revoke_finished.wait(0.1)
    release_grant.set()
    grant_thread.join(timeout=5)
    revoke_thread.join(timeout=5)
    assert not grant_thread.is_alive() and not revoke_thread.is_alive()
    assert second.list_grants("tenant", "docs") == []
    with pytest.raises(ResourceAccessConflictError):
        first.grant_subject(
            "tenant", "docs", "alice", Role.VIEWER, membership_id="old-member"
        )
    first.close()
    second.close()
    first_backend.close()
    second_backend.close()
