from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from cogdoc.api.auth_store import AuthStore
from cogdoc.ha.session_store import (
    DistributedSessionStore,
    SessionBusy,
    SessionRecordConflict,
    StaleSessionLease,
)
from cogdoc.ha.api_state import DistributedKnowledgeBaseRegistry
from cogdoc.ha.storage import SQLiteBackend
from cogdoc.api.resource_access import ResourceAccessStore
from cogdoc.api.tenant_scope import session_store_doc_id
from cogdoc.api.tenancy import Permission, Principal, Role
from cogdoc.memory.manager import MemoryPolicy


def _turn(name: str) -> tuple[list[dict], list[dict]]:
    memory = [
        {"role": "user", "content": f"问题 {name}"},
        {"role": "assistant", "content": f"回答 {name}"},
    ]
    display = [
        {"role": "user", "content": f"问题 {name}"},
        {
            "role": "assistant",
            "content": f"回答 {name}",
            "trace_id": f"trace-{name}",
        },
    ]
    return memory, display


def test_shared_chat_session_survives_another_node(tmp_path):
    path = tmp_path / "chat.db"
    first = DistributedSessionStore(SQLiteBackend(path))
    second = DistributedSessionStore(SQLiteBackend(path))
    memory, display = _turn("one")
    first.record("kb~u-user", "session", memory, display)

    assert second.get_history("kb~u-user", "session") == memory
    assert second.get_display("kb~u-user", "session") == display
    assert second.list_sessions("kb~u-user") == [
        {"session_id": "session", "title": "问题 one", "message_count": 2}
    ]


def test_concurrent_nodes_append_without_losing_a_turn(tmp_path):
    path = tmp_path / "concurrent.db"
    first = DistributedSessionStore(SQLiteBackend(path))
    second = DistributedSessionStore(SQLiteBackend(path))
    barrier = threading.Barrier(2)

    def record(store: DistributedSessionStore, name: str) -> None:
        memory, display = _turn(name)
        barrier.wait()
        store.record("kb", "session", memory, display)

    with ThreadPoolExecutor(max_workers=2) as pool:
        one = pool.submit(record, first, "one")
        two = pool.submit(record, second, "two")
        one.result()
        two.result()

    persisted = first.get_display("kb", "session")
    assert len(persisted) == 4
    assert {item["content"] for item in persisted if item["role"] == "user"} == {
        "问题 one",
        "问题 two",
    }


def test_trace_identity_makes_replay_idempotent_and_conflicting_reuse_fails(
    tmp_path,
):
    store = DistributedSessionStore(SQLiteBackend(tmp_path / "replay.db"))
    memory, display = _turn("same")
    store.record("kb", "session", memory, display)
    store.record("kb", "session", memory, display)
    assert len(store.get_display("kb", "session")) == 2

    changed = [dict(item) for item in display]
    changed[-1]["content"] = "tampered"
    with pytest.raises(SessionRecordConflict):
        store.record("kb", "session", memory, changed)
    assert store.get_display("kb", "session") == display


def test_long_term_memory_is_shared_and_bounded(tmp_path):
    policy = MemoryPolicy(
        long_term_fact_limit=2,
        context_long_term_limit=2,
        memory_semantic_enabled=False,
    )
    store = DistributedSessionStore(
        SQLiteBackend(tmp_path / "memory.db"), memory_policy=policy
    )
    for index, content in enumerate(
        ("请记住：高优先级事实", "我偏好 PostgreSQL", "我偏好 SQLite")
    ):
        store.record(
            "kb~u-user",
            "source",
            [],
            [
                {"role": "user", "content": content},
                {
                    "role": "assistant",
                    "content": "收到",
                    "trace_id": f"trace-{index}",
                },
            ],
        )
    facts = store.get_memory_snapshot("kb~u-user", "other")["long_term"]
    assert len(facts) == 2
    assert facts[0]["content"] == "高优先级事实"


def test_capacity_ttl_and_kb_incarnation_cleanup_are_bounded(tmp_path):
    now = [100.0]
    store = DistributedSessionStore(
        SQLiteBackend(tmp_path / "lifecycle.db"),
        max_sessions=1,
        ttl_seconds=10,
        clock=lambda: now[0],
    )
    for session in ("old", "new"):
        memory, display = _turn(session)
        store.record("kb~u-user", session, memory, display, storage_id="kb")
        now[0] += 1
    assert [item["session_id"] for item in store.list_sessions("kb~u-user")] == ["new"]

    memory, display = _turn("api-key")
    store.record("kb", "legacy", memory, display)
    store.clear_kb("kb")
    assert store.list_sessions("kb") == []
    assert store.list_sessions("kb~u-user") == []

    memory, display = _turn("expired")
    store.record("other", "expired", memory, display)
    now[0] += 11
    assert store.prune_expired(before=now[0] - 10, limit=100) == 1
    assert store.check() is True


def test_session_execution_lease_fences_concurrent_and_late_writer(tmp_path):
    now = [100.0]
    path = tmp_path / "lease.db"
    first = DistributedSessionStore(SQLiteBackend(path), clock=lambda: now[0])
    second = DistributedSessionStore(SQLiteBackend(path), clock=lambda: now[0])
    memory, display = _turn("late")

    with first.execution("kb", "session", "node-a", lease_seconds=5):
        with pytest.raises(SessionBusy):
            second.acquire_execution("kb", "session", "node-b", lease_seconds=5)
        now[0] += 6
        replacement = second.acquire_execution(
            "kb", "session", "node-b", lease_seconds=5
        )
        with pytest.raises(StaleSessionLease):
            first.record("kb", "session", memory, display)
        second.release_execution(
            "kb",
            "session",
            "node-b",
            str(replacement["lease_token"]),
        )
    assert first.get_display("kb", "session") == []


def test_turn_commit_is_atomically_fenced_by_shared_kb_and_acl_epochs(tmp_path):
    backend = SQLiteBackend(tmp_path / "authority.db")
    registry = DistributedKnowledgeBaseRegistry(backend, tmp_path / "cache")
    kb = registry.create("docs", "tenant", "owner")
    storage_id = str(kb["storage_id"])
    access = ResourceAccessStore(None, backend=backend)
    access.set_kb_policy("tenant", storage_id, "owner", "workspace")
    store = DistributedSessionStore(backend)
    memory, display = _turn("one")
    authority = {
        "tenant_id": "tenant",
        "storage_id": storage_id,
        "kb_epoch": registry.current(storage_id),
        "acl_epoch": access.acl_epoch("tenant", storage_id),
        "acl_required": True,
        "auth_kind": "api_principal",
        "subject_id": "owner",
        "role": "owner",
    }
    store.record(storage_id, "session", memory, display, authority=authority)

    access.set_kb_policy("tenant", storage_id, "owner", "private")
    memory_two, display_two = _turn("two")
    with pytest.raises(StaleSessionLease, match="authorization generation"):
        store.record(
            storage_id,
            "session",
            memory_two,
            display_two,
            authority=authority,
        )
    assert store.get_display(storage_id, "session") == display

    authority["acl_epoch"] = access.acl_epoch("tenant", storage_id)
    registry.bump(storage_id)
    with pytest.raises(StaleSessionLease, match="incarnation"):
        store.record(
            storage_id,
            "session",
            memory_two,
            display_two,
            authority=authority,
        )
    assert store.get_display(storage_id, "session") == display


def test_session_deletion_requires_and_accepts_delete_authority(tmp_path):
    backend = SQLiteBackend(tmp_path / "delete-authority.db")
    registry = DistributedKnowledgeBaseRegistry(backend, tmp_path / "cache")
    kb = registry.create("docs", "tenant", "owner")
    storage_id = str(kb["storage_id"])
    store = DistributedSessionStore(backend)
    memory, display = _turn("delete")
    store.record(storage_id, "session", memory, display)
    authority = {
        "tenant_id": "tenant",
        "storage_id": storage_id,
        "kb_epoch": registry.current(storage_id),
        "acl_epoch": 0,
        "acl_required": False,
        "auth_kind": "api_principal",
        "subject_id": "owner",
        "role": "owner",
        "permission": Permission.DELETE.value,
    }

    store.clear(storage_id, "session", authority=authority)
    store.clear_long_term(storage_id, authority=authority)

    assert store.get_display(storage_id, "session") == []
    query_authority = {**authority, "permission": Permission.QUERY.value}
    with pytest.raises(StaleSessionLease, match="permission"):
        store.clear(storage_id, "session", authority=query_authority)


def test_expired_execution_leases_are_boundedly_pruned_without_session_ttl(tmp_path):
    now = [100.0]
    store = DistributedSessionStore(
        SQLiteBackend(tmp_path / "lease-prune.db"),
        ttl_seconds=0,
        clock=lambda: now[0],
    )
    store.acquire_execution("kb", "one", "node-a", lease_seconds=5)
    store.acquire_execution("kb", "two", "node-a", lease_seconds=5)
    now[0] += 6
    assert store.prune_execution_leases(before=now[0], limit=1) == 1
    assert store.prune_execution_leases(before=now[0], limit=1) == 1
    assert store.prune_execution_leases(before=now[0], limit=1) == 0


def test_kb_cleanup_removes_lease_only_user_scopes(tmp_path):
    store = DistributedSessionStore(SQLiteBackend(tmp_path / "lease-only.db"))
    store.acquire_execution(
        "kb~u-userhash",
        "session",
        "node-a",
        lease_seconds=30,
        storage_id="kb",
    )

    store.clear_kb("kb")

    replacement = store.acquire_execution(
        "kb~u-userhash",
        "session",
        "node-b",
        lease_seconds=30,
        storage_id="kb",
    )
    assert replacement["lease_owner"] == "node-b"


def test_kb_cleanup_uses_exact_storage_identity_not_a_string_prefix(tmp_path):
    store = DistributedSessionStore(SQLiteBackend(tmp_path / "scope-identity.db"))
    suffix = "a" * 64
    colliding_kb = f"victim~u-{suffix}"
    memory, display = _turn("collision")
    store.record(
        f"victim~u-{'b' * 64}",
        "victim-chat",
        memory,
        display,
        storage_id="victim",
    )
    store.record(
        colliding_kb,
        "other-chat",
        memory,
        display,
        storage_id=colliding_kb,
    )

    store.clear_kb("victim")

    assert store.list_sessions(f"victim~u-{'b' * 64}") == []
    assert store.get_display(colliding_kb, "other-chat") == display


def test_service_and_explicit_principals_receive_private_session_namespaces():
    service = Principal(
        tenant_id="tenant",
        subject_id="service-account:one",
        role=Role.VIEWER,
        key_fingerprint="service-token:token-one",
    )
    explicit = Principal(
        tenant_id="tenant",
        subject_id="automation-two",
        role=Role.VIEWER,
        key_fingerprint="sha256:explicit",
    )

    def request(principal, explicit_fingerprints=frozenset()):
        return SimpleNamespace(
            state=SimpleNamespace(principal=principal),
            app=SimpleNamespace(
                state=SimpleNamespace(
                    explicit_principal_fingerprints=explicit_fingerprints
                )
            ),
        )

    service_scope = session_store_doc_id(request(service), "kb")
    explicit_scope = session_store_doc_id(
        request(explicit, {explicit.key_fingerprint}), "kb"
    )
    assert service_scope.startswith("kb~u-") and service_scope != "kb"
    assert explicit_scope.startswith("kb~u-") and explicit_scope != service_scope


def test_turn_commit_revalidates_live_login_session_in_same_database(tmp_path):
    backend = SQLiteBackend(tmp_path / "login-authority.db")
    registry = DistributedKnowledgeBaseRegistry(backend, tmp_path / "cache")
    auth = AuthStore(None, backend=backend, scrypt_n=1 << 10)
    registration = auth.register(
        "owner@example.com", "correct horse battery staple", "Owner"
    )
    tenant_id = str(registration["workspace"]["workspace_id"])
    user_id = str(registration["user"]["user_id"])
    membership = auth.membership(tenant_id, user_id)
    assert membership is not None
    membership_id = str(membership["member_id"])
    session_id = str(registration["session"]["session_id"])
    kb = registry.create("docs", tenant_id, user_id)
    storage_id = str(kb["storage_id"])
    store = DistributedSessionStore(backend)
    authority = {
        "tenant_id": tenant_id,
        "storage_id": storage_id,
        "kb_epoch": registry.current(storage_id),
        "acl_epoch": 0,
        "acl_required": False,
        "auth_kind": "user_session",
        "subject_id": user_id,
        "role": "owner",
        "session_id": session_id,
        "membership_id": membership_id,
    }
    memory, display = _turn("authorized")
    store.record(storage_id, "chat", memory, display, authority=authority)
    assert auth.revoke_session(user_id, session_id)
    later_memory, later_display = _turn("late")
    with pytest.raises(StaleSessionLease, match="login authority"):
        store.record(
            storage_id,
            "chat",
            later_memory,
            later_display,
            authority=authority,
        )
    assert store.get_display(storage_id, "chat") == display


def test_turn_commit_enforces_session_policy_and_membership_tombstone(tmp_path):
    backend = SQLiteBackend(tmp_path / "login-policy.db")
    registry = DistributedKnowledgeBaseRegistry(backend, tmp_path / "cache")
    auth = AuthStore(None, backend=backend, scrypt_n=1 << 10)
    registration = auth.register(
        "owner@example.com", "correct horse battery staple", "Owner"
    )
    tenant_id = str(registration["workspace"]["workspace_id"])
    user_id = str(registration["user"]["user_id"])
    membership = auth.membership(tenant_id, user_id)
    assert membership is not None
    membership_id = str(membership["member_id"])
    session_id = str(registration["session"]["session_id"])
    storage_id = str(registry.create("docs", tenant_id, user_id)["storage_id"])
    access = ResourceAccessStore(None, backend=backend)
    authority = {
        "tenant_id": tenant_id,
        "storage_id": storage_id,
        "kb_epoch": registry.current(storage_id),
        "acl_epoch": access.acl_epoch(tenant_id, storage_id),
        "acl_required": True,
        "auth_kind": "user_session",
        "subject_id": user_id,
        "role": "owner",
        "session_id": session_id,
        "membership_id": membership_id,
    }
    store = DistributedSessionStore(backend)
    memory, display = _turn("policy")

    with backend.transaction(write=True) as connection:
        connection.execute(
            "INSERT INTO auth_workspace_session_policies("
            "workspace_id,idle_timeout_minutes,absolute_timeout_hours,"
            "max_active_sessions,revision,created_at,updated_at) "
            "VALUES(?,5,NULL,NULL,1,0,0)",
            (tenant_id,),
        )
        connection.execute(
            "UPDATE auth_sessions SET last_seen_at=0 WHERE session_id=?",
            (session_id,),
        )
    with pytest.raises(StaleSessionLease, match="login authority"):
        store.record(storage_id, "chat", memory, display, authority=authority)

    with backend.transaction(write=True) as connection:
        connection.execute(
            "DELETE FROM auth_workspace_session_policies WHERE workspace_id=?",
            (tenant_id,),
        )
        connection.execute(
            "INSERT INTO resource_access_membership_tombstones("
            "tenant_id,subject_id,membership_id,revoked_at) VALUES(?,?,?,?)",
            (tenant_id, user_id, membership_id, "now"),
        )
    with pytest.raises(StaleSessionLease, match="login authority"):
        store.record(storage_id, "chat", memory, display, authority=authority)
    assert store.get_display(storage_id, "chat") == []


def test_turn_commit_enforces_live_service_token_permission_policy(tmp_path):
    backend = SQLiteBackend(tmp_path / "service-policy.db")
    registry = DistributedKnowledgeBaseRegistry(backend, tmp_path / "cache")
    auth = AuthStore(None, backend=backend, scrypt_n=1 << 10)
    registration = auth.register(
        "owner@example.com", "correct horse battery staple", "Owner"
    )
    tenant_id = str(registration["workspace"]["workspace_id"])
    owner_id = str(registration["user"]["user_id"])
    account = auth.create_service_account(
        workspace_id=tenant_id,
        name="Chat reader",
        role="viewer",
        actor_user_id=owner_id,
    )
    token = auth.create_service_token(
        workspace_id=tenant_id,
        service_account_id=str(account["service_account_id"]),
        label="chat",
        ttl_seconds=3600,
        actor_user_id=owner_id,
    )
    storage_id = str(registry.create("docs", tenant_id, owner_id)["storage_id"])
    authority = {
        "tenant_id": tenant_id,
        "storage_id": storage_id,
        "kb_epoch": registry.current(storage_id),
        "acl_epoch": 0,
        "acl_required": False,
        "auth_kind": "service_account",
        "subject_id": f"service-account:{account['service_account_id']}",
        "role": "viewer",
        "service_account_id": str(account["service_account_id"]),
        "token_id": str(token["token_id"]),
    }
    auth.set_service_account_policy(
        workspace_id=tenant_id,
        max_accounts=100,
        max_tokens_per_account=10,
        max_token_ttl_days=365,
        allow_non_expiring=True,
        allowed_permissions=["read"],
        expected_revision=0,
        actor_user_id=owner_id,
    )
    store = DistributedSessionStore(backend)
    memory, display = _turn("service")
    with pytest.raises(StaleSessionLease, match="service authority"):
        store.record(storage_id, "chat", memory, display, authority=authority)
    assert store.get_display(storage_id, "chat") == []
