from __future__ import annotations

import hashlib
import json
import math
import secrets
import threading
import time
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Callable

from cogdoc.ha.storage import DatabaseBackend, execute_script
from cogdoc.api.tenancy import Permission, ROLE_PERMISSIONS, Role
from cogdoc.memory.manager import MemoryPolicy, rank_long_term_facts, update_memory
from cogdoc.memory.retriever import EmbeddingFunction, MemoryRetriever


class SessionRecordConflict(RuntimeError):
    """The same durable chat turn identity was reused with different content."""


class SessionBusy(RuntimeError):
    """Another node owns the live execution lease for this chat session."""


class StaleSessionLease(RuntimeError):
    """A superseded chat execution tried to persist a late answer."""


def _clean(value: str, field: str, maximum: int) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or len(value) > maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError(f"{field} is invalid")
    return value


def _json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _list_json(value: object, field: str) -> list[dict[str, Any]]:
    try:
        decoded = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"stored {field} is corrupt") from exc
    if type(decoded) is not list or any(type(item) is not dict for item in decoded):
        raise RuntimeError(f"stored {field} is corrupt")
    return decoded


def _dict_json(value: object, field: str) -> dict[str, Any]:
    try:
        decoded = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"stored {field} is corrupt") from exc
    if type(decoded) is not dict:
        raise RuntimeError(f"stored {field} is corrupt")
    return decoded


def _column(row: Any, name: str, index: int) -> Any:
    return row.get(name) if isinstance(row, Mapping) else row[index]


class DistributedSessionStore:
    """PostgreSQL-backed layered chat memory with per-scope serialization.

    A scope row is locked before either a session or its long-term facts are
    changed.  This avoids lost updates when two API nodes finish turns for the
    same user/KB at the same time.  The optional trace ID in the assistant
    display message is also a durable idempotency key for HTTP/worker replay.
    """

    def __init__(
        self,
        backend: DatabaseBackend,
        *,
        max_sessions: int = 1024,
        ttl_seconds: int = 604800,
        max_display_messages: int = 2000,
        max_session_bytes: int = 4 * 1024 * 1024,
        memory_policy: MemoryPolicy | None = None,
        memory_embedding_fn: EmbeddingFunction | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if type(max_sessions) is not int or not 1 <= max_sessions <= 100_000:
            raise ValueError("max_sessions is invalid")
        if type(ttl_seconds) is not int or not 0 <= ttl_seconds <= 10 * 365 * 86400:
            raise ValueError("ttl_seconds is invalid")
        if (
            type(max_display_messages) is not int
            or not 2 <= max_display_messages <= 100_000
            or max_display_messages % 2
        ):
            raise ValueError("max_display_messages is invalid")
        if (
            type(max_session_bytes) is not int
            or not 4096 <= max_session_bytes <= 128 * 1024 * 1024
        ):
            raise ValueError("max_session_bytes is invalid")
        self.backend = backend
        self.max_sessions = max_sessions
        self.ttl_seconds = ttl_seconds
        self.max_display_messages = max_display_messages
        self.max_session_bytes = max_session_bytes
        self.memory_policy = memory_policy or MemoryPolicy()
        self.memory_retriever = MemoryRetriever(
            self.memory_policy, embedding_fn=memory_embedding_fn
        )
        self._clock = clock
        execute_script(
            backend,
            [
                """CREATE TABLE IF NOT EXISTS ha_chat_memory_scopes (
                doc_id TEXT PRIMARY KEY,storage_id TEXT NOT NULL,
                revision BIGINT NOT NULL DEFAULT 1,
                updated_at DOUBLE PRECISION NOT NULL)""",
                "CREATE INDEX IF NOT EXISTS idx_ha_chat_scope_storage ON ha_chat_memory_scopes(storage_id,doc_id)",
                """CREATE TABLE IF NOT EXISTS ha_chat_sessions (
                doc_id TEXT NOT NULL,session_id TEXT NOT NULL,memory_json TEXT NOT NULL,
                display_json TEXT NOT NULL,mid_memory_json TEXT NOT NULL,
                updated_at DOUBLE PRECISION NOT NULL,revision BIGINT NOT NULL DEFAULT 1,
                PRIMARY KEY(doc_id,session_id))""",
                "CREATE INDEX IF NOT EXISTS idx_ha_chat_sessions_expiry ON ha_chat_sessions(updated_at,doc_id,session_id)",
                "CREATE INDEX IF NOT EXISTS idx_ha_chat_sessions_list ON ha_chat_sessions(doc_id,updated_at DESC,session_id DESC)",
                """CREATE TABLE IF NOT EXISTS ha_chat_long_memories (
                doc_id TEXT NOT NULL,memory_id TEXT NOT NULL,type TEXT NOT NULL,
                content TEXT NOT NULL,importance DOUBLE PRECISION NOT NULL,
                updated_at DOUBLE PRECISION NOT NULL,
                PRIMARY KEY(doc_id,memory_id))""",
                "CREATE INDEX IF NOT EXISTS idx_ha_chat_long_order ON ha_chat_long_memories(doc_id,importance DESC,updated_at DESC,memory_id)",
                """CREATE TABLE IF NOT EXISTS ha_chat_turns (
                doc_id TEXT NOT NULL,session_id TEXT NOT NULL,turn_id TEXT NOT NULL,
                payload_sha256 TEXT NOT NULL,created_at DOUBLE PRECISION NOT NULL,
                PRIMARY KEY(doc_id,session_id,turn_id))""",
                "CREATE INDEX IF NOT EXISTS idx_ha_chat_turns_created ON ha_chat_turns(created_at,doc_id,session_id,turn_id)",
                """CREATE TABLE IF NOT EXISTS ha_chat_session_leases (
                doc_id TEXT NOT NULL,storage_id TEXT NOT NULL,session_id TEXT NOT NULL,
                lease_owner TEXT NOT NULL,
                lease_token TEXT NOT NULL,lease_expires_at DOUBLE PRECISION NOT NULL,
                updated_at DOUBLE PRECISION NOT NULL,revision BIGINT NOT NULL DEFAULT 1,
                PRIMARY KEY(doc_id,session_id))""",
                "CREATE INDEX IF NOT EXISTS idx_ha_chat_session_lease_expiry ON ha_chat_session_leases(lease_expires_at,doc_id,session_id)",
                "CREATE INDEX IF NOT EXISTS idx_ha_chat_lease_storage ON ha_chat_session_leases(storage_id,doc_id,session_id)",
            ],
        )
        self._execution_context: ContextVar[dict[tuple[str, str], tuple[str, str]]] = (
            ContextVar(f"cogdoc_ha_chat_execution_{id(self)}", default={})
        )

    @property
    def _marker(self) -> str:
        return self.backend.sql(sqlite="?", postgres="%s")

    def _ensure_scope_locked(
        self, connection: Any, doc_id: str, storage_id: str, now: float
    ) -> None:
        marker = self._marker
        connection.execute(
            self.backend.sql(
                sqlite=(
                    "INSERT OR IGNORE INTO ha_chat_memory_scopes"
                    "(doc_id,storage_id,revision,updated_at) VALUES(?,?,1,?)"
                ),
                postgres=(
                    "INSERT INTO ha_chat_memory_scopes(doc_id,storage_id,revision,updated_at) "
                    "VALUES(%s,%s,1,%s) ON CONFLICT(doc_id) DO NOTHING"
                ),
            ),
            (doc_id, storage_id, now),
        )
        suffix = " FOR UPDATE" if self.backend.kind == "postgres" else ""
        row = connection.execute(
            "SELECT revision,storage_id FROM ha_chat_memory_scopes "
            f"WHERE doc_id={marker}{suffix}",
            (doc_id,),
        ).fetchone()
        if row is None:  # pragma: no cover - insert/transaction invariant
            raise RuntimeError("chat memory scope disappeared")
        if str(_column(row, "storage_id", 1)) != storage_id:
            raise SessionRecordConflict("chat memory scope storage identity changed")

    def _session_locked(
        self, connection: Any, doc_id: str, session_id: str
    ) -> Any | None:
        suffix = " FOR UPDATE" if self.backend.kind == "postgres" else ""
        return connection.execute(
            "SELECT memory_json,display_json,mid_memory_json,updated_at,revision "
            f"FROM ha_chat_sessions WHERE doc_id={self._marker} "
            f"AND session_id={self._marker}{suffix}",
            (doc_id, session_id),
        ).fetchone()

    @staticmethod
    def _turn_id(display_messages: Sequence[Mapping[str, Any]]) -> str | None:
        candidates = [
            message.get("trace_id")
            for message in display_messages
            if message.get("role") == "assistant" and message.get("trace_id")
        ]
        if not candidates:
            return None
        value = candidates[-1]
        return _clean(str(value), "trace_id", 256)

    def _validate_payload(
        self,
        memory_messages: list[dict[str, Any]],
        display_messages: list[dict[str, Any]],
    ) -> str:
        if type(memory_messages) is not list or any(
            type(item) is not dict for item in memory_messages
        ):
            raise TypeError("memory_messages must be a strict list of objects")
        if type(display_messages) is not list or any(
            type(item) is not dict for item in display_messages
        ):
            raise TypeError("display_messages must be a strict list of objects")
        payload = _json({"memory": memory_messages, "display": display_messages})
        if len(payload.encode("utf-8")) > self.max_session_bytes:
            raise ValueError("chat turn exceeds the session byte limit")
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _check_authority_locked(
        self,
        connection: Any,
        authority: Mapping[str, Any],
        now: float,
        *,
        required_permission: Permission | None = None,
    ) -> None:
        """Fence the memory write against shared KB, ACL and login authority."""

        required_permission = required_permission or Permission(
            str(authority.get("permission") or Permission.QUERY.value)
        )

        marker = self._marker
        tenant_id = _clean(str(authority.get("tenant_id") or ""), "tenant_id", 255)
        storage_id = _clean(str(authority.get("storage_id") or ""), "storage_id", 512)
        lock = " FOR SHARE" if self.backend.kind == "postgres" else ""
        kb = connection.execute(
            "SELECT tenant_id,lifecycle,epoch FROM ha_api_knowledge_bases "
            f"WHERE storage_id={marker}{lock}",
            (storage_id,),
        ).fetchone()
        if (
            kb is None
            or str(_column(kb, "tenant_id", 0)) != tenant_id
            or str(_column(kb, "lifecycle", 1)) != "active"
            or int(_column(kb, "epoch", 2)) != int(authority.get("kb_epoch") or -1)
        ):
            raise StaleSessionLease("chat knowledge-base incarnation is stale")
        if bool(authority.get("acl_required")):
            acl = connection.execute(
                "SELECT epoch FROM resource_access_acl_epochs "
                f"WHERE tenant_id={marker} AND kb_id={marker}{lock}",
                (tenant_id, storage_id),
            ).fetchone()
            acl_epoch = 0 if acl is None else int(_column(acl, "epoch", 0))
            if acl_epoch != int(authority.get("acl_epoch") or 0):
                raise StaleSessionLease("chat authorization generation is stale")

        auth_kind = str(authority.get("auth_kind") or "")
        frozen_role = str(authority.get("role") or "")
        if str(authority.get("permission") or required_permission.value) != str(
            required_permission.value
        ):
            raise StaleSessionLease("chat authority permission is stale")
        if auth_kind == "user_session":
            subject_id = _clean(
                str(authority.get("subject_id") or ""), "subject_id", 255
            )
            session_id = _clean(
                str(authority.get("session_id") or ""), "session_id", 255
            )
            membership_id = _clean(
                str(authority.get("membership_id") or ""), "membership_id", 255
            )
            session = connection.execute(
                "SELECT active_workspace_id,created_at,last_seen_at,expires_at,"
                "revoked_at FROM auth_sessions "
                f"WHERE session_id={marker} AND user_id={marker}{lock}",
                (session_id, subject_id),
            ).fetchone()
            membership = connection.execute(
                "SELECT member_id,role FROM auth_memberships "
                f"WHERE workspace_id={marker} AND user_id={marker}{lock}",
                (tenant_id, subject_id),
            ).fetchone()
            policy = connection.execute(
                "SELECT idle_timeout_minutes,absolute_timeout_hours FROM "
                "auth_workspace_session_policies "
                f"WHERE workspace_id={marker}{lock}",
                (tenant_id,),
            ).fetchone()
            scim = connection.execute(
                "SELECT COUNT(*),COALESCE(SUM(CASE WHEN active=1 AND "
                "deleted_at IS NULL THEN 1 ELSE 0 END),0) FROM auth_scim_users "
                f"WHERE user_id={marker}",
                (subject_id,),
            ).fetchone()
            tombstone = (
                connection.execute(
                    "SELECT 1 FROM resource_access_membership_tombstones "
                    f"WHERE tenant_id={marker} AND subject_id={marker} AND "
                    f"membership_id={marker}{lock}",
                    (tenant_id, subject_id, membership_id),
                ).fetchone()
                if bool(authority.get("acl_required"))
                else None
            )
            idle_minutes = (
                None if policy is None else _column(policy, "idle_timeout_minutes", 0)
            )
            absolute_hours = (
                None if policy is None else _column(policy, "absolute_timeout_hours", 1)
            )
            policy_expired = (
                bool(
                    (
                        absolute_hours is not None
                        and float(_column(session, "created_at", 1))
                        + int(absolute_hours) * 3600
                        <= now
                    )
                    or (
                        idle_minutes is not None
                        and float(_column(session, "last_seen_at", 2))
                        + int(idle_minutes) * 60
                        <= now
                    )
                )
                if session is not None
                else True
            )
            try:
                live_role_permissions = ROLE_PERMISSIONS[
                    Role(str(_column(membership, "role", 1)))
                ]
            except (TypeError, ValueError):
                live_role_permissions = frozenset()
            if (
                session is None
                or str(_column(session, "active_workspace_id", 0) or "") != tenant_id
                or float(_column(session, "expires_at", 3)) <= now
                or _column(session, "revoked_at", 4) is not None
                or policy_expired
                or membership is None
                or str(_column(membership, "member_id", 0)) != membership_id
                or str(_column(membership, "role", 1)) != frozen_role
                or required_permission not in live_role_permissions
                or tombstone is not None
                or (
                    scim is not None
                    and int(_column(scim, "count", 0)) > 0
                    and int(_column(scim, "active", 1)) == 0
                )
            ):
                raise StaleSessionLease("chat login authority is stale")
        elif auth_kind == "service_account":
            service_account_id = _clean(
                str(authority.get("service_account_id") or ""),
                "service_account_id",
                255,
            )
            token_id = _clean(str(authority.get("token_id") or ""), "token_id", 255)
            service = connection.execute(
                "SELECT accounts.role,accounts.active,accounts.deleted_at,"
                "tokens.revoked_at,tokens.expires_at,tokens.permissions_json FROM "
                "auth_service_tokens AS tokens "
                "JOIN auth_service_accounts AS accounts ON "
                "accounts.service_account_id=tokens.service_account_id "
                f"WHERE tokens.token_id={marker} AND tokens.service_account_id={marker} "
                f"AND accounts.workspace_id={marker}{lock}",
                (token_id, service_account_id, tenant_id),
            ).fetchone()
            policy = connection.execute(
                "SELECT allowed_permissions_json FROM auth_service_account_policies "
                f"WHERE workspace_id={marker}{lock}",
                (tenant_id,),
            ).fetchone()
            try:
                role_permissions = ROLE_PERMISSIONS[
                    Role(str(_column(service, "role", 0)))
                ]
                token_raw = _column(service, "permissions_json", 5)
                token_values = (
                    [item.value for item in role_permissions]
                    if token_raw is None
                    else json.loads(str(token_raw))
                )
                policy_values = (
                    [item.value for item in ROLE_PERMISSIONS[Role.ADMIN]]
                    if policy is None
                    else json.loads(str(_column(policy, "allowed_permissions_json", 0)))
                )
                effective_permissions = (
                    role_permissions
                    & frozenset(Permission(item) for item in token_values)
                    & frozenset(Permission(item) for item in policy_values)
                )
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                effective_permissions = frozenset()
            if (
                service is None
                or str(_column(service, "role", 0)) != frozen_role
                or not bool(_column(service, "active", 1))
                or _column(service, "deleted_at", 2) is not None
                or _column(service, "revoked_at", 3) is not None
                or (
                    _column(service, "expires_at", 4) is not None
                    and float(_column(service, "expires_at", 4)) <= now
                )
                or required_permission not in effective_permissions
            ):
                raise StaleSessionLease("chat service authority is stale")
        elif auth_kind == "api_principal":
            try:
                allowed = required_permission in ROLE_PERMISSIONS[Role(frozen_role)]
            except ValueError:
                allowed = False
            if not allowed:
                raise StaleSessionLease("chat principal authority is stale")
        else:
            raise StaleSessionLease("chat authority kind is invalid")

    def check_authority_locked(
        self,
        connection: Any,
        authority: Mapping[str, Any],
        *,
        required_permission: Permission,
        now: float | None = None,
    ) -> None:
        """Validate shared authority inside a caller-owned write transaction."""

        self._check_authority_locked(
            connection,
            authority,
            self._clock() if now is None else now,
            required_permission=required_permission,
        )

    def record(
        self,
        doc_id: str,
        session_id: str | None,
        memory_messages: list[dict[str, Any]],
        display_messages: list[dict[str, Any]],
        *,
        authority: Mapping[str, Any] | None = None,
        storage_id: str | None = None,
    ) -> None:
        if not session_id or (not memory_messages and not display_messages):
            return
        doc_id = _clean(doc_id, "doc_id", 512)
        session_id = _clean(session_id, "session_id", 256)
        payload_sha256 = self._validate_payload(memory_messages, display_messages)
        turn_id = self._turn_id(display_messages)
        now = self._clock()
        marker = self._marker
        with self.backend.transaction(write=True) as connection:
            if authority is not None:
                self._check_authority_locked(connection, authority, now)
            self._ensure_scope_locked(
                connection,
                doc_id,
                str(authority.get("storage_id") or storage_id or doc_id)
                if authority is not None
                else str(storage_id or doc_id),
                now,
            )
            row = self._session_locked(connection, doc_id, session_id)
            capability = self._execution_context.get().get((doc_id, session_id))
            if capability is not None:
                owner, token = capability
                lease = connection.execute(
                    "SELECT 1 FROM ha_chat_session_leases "
                    f"WHERE doc_id={marker} AND session_id={marker} "
                    f"AND lease_owner={marker} AND lease_token={marker} "
                    f"AND lease_expires_at>{marker}",
                    (doc_id, session_id, owner, token, now),
                ).fetchone()
                if lease is None:
                    raise StaleSessionLease("chat session execution lease is stale")
            if turn_id is not None:
                existing_turn = connection.execute(
                    "SELECT payload_sha256 FROM ha_chat_turns "
                    f"WHERE doc_id={marker} AND session_id={marker} AND turn_id={marker}",
                    (doc_id, session_id, turn_id),
                ).fetchone()
                if existing_turn is not None:
                    if (
                        str(_column(existing_turn, "payload_sha256", 0))
                        != payload_sha256
                    ):
                        raise SessionRecordConflict(
                            "chat turn identity was reused with different content"
                        )
                    return
            memory = _list_json(_column(row, "memory_json", 0), "memory") if row else []
            display = (
                _list_json(_column(row, "display_json", 1), "display") if row else []
            )
            mid_memory = (
                _dict_json(_column(row, "mid_memory_json", 2), "mid memory")
                if row
                else {}
            )
            memory, mid_memory, facts = update_memory(
                memory,
                mid_memory,
                memory_messages,
                display_messages,
                self.memory_policy,
            )
            display.extend(display_messages)
            if len(display) > self.max_display_messages:
                display = display[-self.max_display_messages :]
            memory_json = _json(memory)
            display_json = _json(display)
            mid_json = _json(mid_memory)
            if (
                sum(
                    len(value.encode("utf-8"))
                    for value in (memory_json, display_json, mid_json)
                )
                > self.max_session_bytes
            ):
                raise ValueError("chat session exceeds the byte limit")
            connection.execute(
                self.backend.sql(
                    sqlite=(
                        "INSERT INTO ha_chat_sessions(doc_id,session_id,memory_json,"
                        "display_json,mid_memory_json,updated_at,revision) "
                        "VALUES(?,?,?,?,?,?,1) ON CONFLICT(doc_id,session_id) DO UPDATE SET "
                        "memory_json=excluded.memory_json,display_json=excluded.display_json,"
                        "mid_memory_json=excluded.mid_memory_json,updated_at=excluded.updated_at,"
                        "revision=ha_chat_sessions.revision+1"
                    ),
                    postgres=(
                        "INSERT INTO ha_chat_sessions(doc_id,session_id,memory_json,"
                        "display_json,mid_memory_json,updated_at,revision) "
                        "VALUES(%s,%s,%s,%s,%s,%s,1) "
                        "ON CONFLICT(doc_id,session_id) DO UPDATE SET "
                        "memory_json=EXCLUDED.memory_json,display_json=EXCLUDED.display_json,"
                        "mid_memory_json=EXCLUDED.mid_memory_json,updated_at=EXCLUDED.updated_at,"
                        "revision=ha_chat_sessions.revision+1"
                    ),
                ),
                (doc_id, session_id, memory_json, display_json, mid_json, now),
            )
            self._upsert_long_memories(connection, doc_id, facts, now)
            if turn_id is not None:
                connection.execute(
                    f"INSERT INTO ha_chat_turns(doc_id,session_id,turn_id,payload_sha256,created_at) VALUES({marker},{marker},{marker},{marker},{marker})",
                    (doc_id, session_id, turn_id, payload_sha256, now),
                )
            self._evict_overflow(connection, doc_id)
            connection.execute(
                "UPDATE ha_chat_memory_scopes SET revision=revision+1,updated_at="
                f"{marker} WHERE doc_id={marker}",
                (now, doc_id),
            )

    def _upsert_long_memories(
        self,
        connection: Any,
        doc_id: str,
        facts: list[dict[str, Any]],
        now: float,
    ) -> None:
        marker = self._marker
        for fact in facts:
            connection.execute(
                self.backend.sql(
                    sqlite=(
                        "INSERT INTO ha_chat_long_memories(doc_id,memory_id,type,content,"
                        "importance,updated_at) VALUES(?,?,?,?,?,?) "
                        "ON CONFLICT(doc_id,memory_id) DO UPDATE SET type=excluded.type,"
                        "content=excluded.content,importance=excluded.importance,"
                        "updated_at=excluded.updated_at"
                    ),
                    postgres=(
                        "INSERT INTO ha_chat_long_memories(doc_id,memory_id,type,content,"
                        "importance,updated_at) VALUES(%s,%s,%s,%s,%s,%s) "
                        "ON CONFLICT(doc_id,memory_id) DO UPDATE SET type=EXCLUDED.type,"
                        "content=EXCLUDED.content,importance=EXCLUDED.importance,"
                        "updated_at=EXCLUDED.updated_at"
                    ),
                ),
                (
                    doc_id,
                    str(fact["id"]),
                    str(fact["type"]),
                    str(fact["content"]),
                    float(fact["importance"]),
                    now,
                ),
            )
        rows = connection.execute(
            "SELECT memory_id,type,content,importance,updated_at "
            f"FROM ha_chat_long_memories WHERE doc_id={marker}",
            (doc_id,),
        ).fetchall()
        ranked = rank_long_term_facts(
            [
                {
                    "id": str(_column(row, "memory_id", 0)),
                    "type": str(_column(row, "type", 1)),
                    "content": str(_column(row, "content", 2)),
                    "importance": float(_column(row, "importance", 3)),
                    "updated_at": float(_column(row, "updated_at", 4)),
                }
                for row in rows
            ],
            self.memory_policy.long_term_fact_limit,
        )
        keep = {str(item["id"]) for item in ranked}
        for row in rows:
            memory_id = str(_column(row, "memory_id", 0))
            if memory_id not in keep:
                connection.execute(
                    "DELETE FROM ha_chat_long_memories "
                    f"WHERE doc_id={marker} AND memory_id={marker}",
                    (doc_id, memory_id),
                )

    def _read_long_memories(self, connection: Any, doc_id: str) -> list[dict[str, Any]]:
        rows = connection.execute(
            "SELECT memory_id,type,content,importance,updated_at "
            f"FROM ha_chat_long_memories WHERE doc_id={self._marker} "
            "ORDER BY importance DESC,updated_at DESC,memory_id",
            (doc_id,),
        ).fetchall()
        return [
            {
                "id": str(_column(row, "memory_id", 0)),
                "type": str(_column(row, "type", 1)),
                "content": str(_column(row, "content", 2)),
                "importance": float(_column(row, "importance", 3)),
                "updated_at": float(_column(row, "updated_at", 4)),
            }
            for row in rows
        ]

    def get_history(
        self, doc_id: str, session_id: str | None, query: str = ""
    ) -> list[dict[str, Any]]:
        if not session_id:
            return []
        doc_id = _clean(doc_id, "doc_id", 512)
        session_id = _clean(session_id, "session_id", 256)
        now = self._clock()
        with self.backend.transaction(write=True) as connection:
            self._purge_expired_scope(connection, doc_id, now)
            row = self._session_locked(connection, doc_id, session_id)
            facts = self._read_long_memories(connection, doc_id)
            if row is None:
                memory: list[dict[str, Any]] = []
                mid_memory: dict[str, Any] = {}
            else:
                memory = _list_json(_column(row, "memory_json", 0), "memory")
                mid_memory = _dict_json(
                    _column(row, "mid_memory_json", 2), "mid memory"
                )
                connection.execute(
                    "UPDATE ha_chat_sessions SET updated_at="
                    f"{self._marker},revision=revision+1 WHERE doc_id={self._marker} "
                    f"AND session_id={self._marker}",
                    (now, doc_id, session_id),
                )
        return self.memory_retriever.retrieve(query, memory, mid_memory, facts)

    def get_memory_snapshot(
        self, doc_id: str, session_id: str | None
    ) -> dict[str, Any]:
        if not session_id:
            return {"short_term": [], "mid_term": {}, "long_term": []}
        doc_id = _clean(doc_id, "doc_id", 512)
        session_id = _clean(session_id, "session_id", 256)
        with self.backend.transaction() as connection:
            row = connection.execute(
                "SELECT memory_json,mid_memory_json FROM ha_chat_sessions "
                f"WHERE doc_id={self._marker} AND session_id={self._marker}",
                (doc_id, session_id),
            ).fetchone()
            return {
                "short_term": (
                    _list_json(_column(row, "memory_json", 0), "memory") if row else []
                ),
                "mid_term": (
                    _dict_json(_column(row, "mid_memory_json", 1), "mid memory")
                    if row
                    else {}
                ),
                "long_term": self._read_long_memories(connection, doc_id),
            }

    def get_display(self, doc_id: str, session_id: str | None) -> list[dict[str, Any]]:
        if not session_id:
            return []
        doc_id = _clean(doc_id, "doc_id", 512)
        session_id = _clean(session_id, "session_id", 256)
        with self.backend.transaction() as connection:
            row = connection.execute(
                "SELECT display_json FROM ha_chat_sessions "
                f"WHERE doc_id={self._marker} AND session_id={self._marker}",
                (doc_id, session_id),
            ).fetchone()
        return _list_json(_column(row, "display_json", 0), "display") if row else []

    def list_sessions(self, doc_id: str) -> list[dict[str, Any]]:
        doc_id = _clean(doc_id, "doc_id", 512)
        now = self._clock()
        with self.backend.transaction(write=True) as connection:
            self._purge_expired_scope(connection, doc_id, now)
            rows = connection.execute(
                "SELECT session_id,display_json FROM ha_chat_sessions "
                f"WHERE doc_id={self._marker} "
                "ORDER BY updated_at DESC,session_id DESC",
                (doc_id,),
            ).fetchall()
        result = []
        for row in rows:
            display = _list_json(_column(row, "display_json", 1), "display")
            title = next(
                (
                    str(item.get("content") or "")
                    for item in display
                    if item.get("role") == "user"
                ),
                "",
            )
            result.append(
                {
                    "session_id": str(_column(row, "session_id", 0)),
                    "title": title.strip()[:40] or "新对话",
                    "message_count": len(display),
                }
            )
        return result

    def answer_count(self, doc_id: str) -> int:
        doc_id = _clean(doc_id, "doc_id", 512)
        with self.backend.transaction() as connection:
            rows = connection.execute(
                "SELECT display_json FROM ha_chat_sessions "
                f"WHERE doc_id={self._marker}",
                (doc_id,),
            ).fetchall()
        return sum(
            1
            for row in rows
            for message in _list_json(_column(row, "display_json", 0), "display")
            if message.get("role") == "assistant"
        )

    def clear(
        self,
        doc_id: str,
        session_id: str | None,
        *,
        authority: Mapping[str, Any] | None = None,
    ) -> None:
        if not session_id:
            return
        doc_id = _clean(doc_id, "doc_id", 512)
        session_id = _clean(session_id, "session_id", 256)
        now = self._clock()
        with self.backend.transaction(write=True) as connection:
            if authority is not None:
                self._check_authority_locked(
                    connection,
                    authority,
                    now,
                    required_permission=Permission.DELETE,
                )
            self._ensure_scope_locked(
                connection,
                doc_id,
                str(authority.get("storage_id") or doc_id)
                if authority is not None
                else doc_id,
                now,
            )
            connection.execute(
                "DELETE FROM ha_chat_turns "
                f"WHERE doc_id={self._marker} AND session_id={self._marker}",
                (doc_id, session_id),
            )
            connection.execute(
                "DELETE FROM ha_chat_sessions "
                f"WHERE doc_id={self._marker} AND session_id={self._marker}",
                (doc_id, session_id),
            )
            connection.execute(
                "DELETE FROM ha_chat_session_leases "
                f"WHERE doc_id={self._marker} AND session_id={self._marker}",
                (doc_id, session_id),
            )

    def clear_long_term(
        self,
        doc_id: str,
        *,
        authority: Mapping[str, Any] | None = None,
    ) -> None:
        doc_id = _clean(doc_id, "doc_id", 512)
        now = self._clock()
        with self.backend.transaction(write=True) as connection:
            if authority is not None:
                self._check_authority_locked(
                    connection,
                    authority,
                    now,
                    required_permission=Permission.DELETE,
                )
            self._ensure_scope_locked(
                connection,
                doc_id,
                str(authority.get("storage_id") or doc_id)
                if authority is not None
                else doc_id,
                now,
            )
            connection.execute(
                f"DELETE FROM ha_chat_long_memories WHERE doc_id={self._marker}",
                (doc_id,),
            )

    def clear_kb(self, doc_id: str) -> None:
        doc_id = _clean(doc_id, "doc_id", 512)
        marker = self._marker
        with self.backend.transaction(write=True) as connection:
            rows = connection.execute(
                "SELECT doc_id FROM ha_chat_memory_scopes "
                f"WHERE storage_id={marker} UNION SELECT doc_id FROM "
                f"ha_chat_session_leases WHERE storage_id={marker}",
                (doc_id, doc_id),
            ).fetchall()
            scopes = {doc_id, *(str(_column(row, "doc_id", 0)) for row in rows)}
            for table in (
                "ha_chat_session_leases",
                "ha_chat_turns",
                "ha_chat_sessions",
                "ha_chat_long_memories",
                "ha_chat_memory_scopes",
            ):
                for scope in sorted(scopes):
                    connection.execute(
                        f"DELETE FROM {table} WHERE doc_id={marker}", (scope,)
                    )

    def _purge_expired_scope(self, connection: Any, doc_id: str, now: float) -> None:
        if self.ttl_seconds <= 0:
            return
        cutoff = now - self.ttl_seconds
        rows = connection.execute(
            "SELECT session_id FROM ha_chat_sessions "
            f"WHERE doc_id={self._marker} AND updated_at<{self._marker} "
            "AND NOT EXISTS (SELECT 1 FROM ha_chat_session_leases AS leases "
            "WHERE leases.doc_id=ha_chat_sessions.doc_id AND "
            "leases.session_id=ha_chat_sessions.session_id "
            f"AND leases.lease_expires_at>{self._marker})",
            (doc_id, cutoff, now),
        ).fetchall()
        for row in rows:
            session_id = str(_column(row, "session_id", 0))
            connection.execute(
                "DELETE FROM ha_chat_turns "
                f"WHERE doc_id={self._marker} AND session_id={self._marker}",
                (doc_id, session_id),
            )
            connection.execute(
                "DELETE FROM ha_chat_sessions "
                f"WHERE doc_id={self._marker} AND session_id={self._marker}",
                (doc_id, session_id),
            )

    def _evict_overflow(self, connection: Any, doc_id: str) -> None:
        rows = connection.execute(
            "SELECT session_id FROM ha_chat_sessions "
            f"WHERE doc_id={self._marker} ORDER BY updated_at DESC,session_id DESC",
            (doc_id,),
        ).fetchall()
        overflow = len(rows) - self.max_sessions
        if overflow <= 0:
            return
        live_rows = connection.execute(
            "SELECT session_id FROM ha_chat_session_leases "
            f"WHERE doc_id={self._marker} AND lease_expires_at>{self._marker}",
            (doc_id, self._clock()),
        ).fetchall()
        live = {str(_column(row, "session_id", 0)) for row in live_rows}
        candidates = [
            row
            for row in reversed(rows)
            if str(_column(row, "session_id", 0)) not in live
        ][:overflow]
        for row in candidates:
            session_id = str(_column(row, "session_id", 0))
            connection.execute(
                "DELETE FROM ha_chat_turns "
                f"WHERE doc_id={self._marker} AND session_id={self._marker}",
                (doc_id, session_id),
            )
            connection.execute(
                "DELETE FROM ha_chat_sessions "
                f"WHERE doc_id={self._marker} AND session_id={self._marker}",
                (doc_id, session_id),
            )

    def _prune_execution_leases_locked(
        self,
        connection: Any,
        *,
        before: float,
        limit: int,
    ) -> int:
        marker = self._marker
        expired_leases = connection.execute(
            "SELECT doc_id,session_id FROM ha_chat_session_leases "
            f"WHERE lease_expires_at<={marker} "
            f"ORDER BY lease_expires_at,doc_id,session_id LIMIT {marker}",
            (before, limit),
        ).fetchall()
        removed = 0
        for lease in expired_leases:
            changed = connection.execute(
                "DELETE FROM ha_chat_session_leases "
                f"WHERE doc_id={marker} AND session_id={marker} "
                f"AND lease_expires_at<={marker}",
                (
                    str(_column(lease, "doc_id", 0)),
                    str(_column(lease, "session_id", 1)),
                    before,
                ),
            )
            removed += int(changed.rowcount)
        return removed

    def prune_execution_leases(self, *, before: float, limit: int = 1000) -> int:
        if not math.isfinite(before):
            raise ValueError("before must be finite")
        if type(limit) is not int or not 1 <= limit <= 10_000:
            raise ValueError("limit is invalid")
        with self.backend.transaction(write=True) as connection:
            return self._prune_execution_leases_locked(
                connection, before=before, limit=limit
            )

    def prune_expired(self, *, before: float, limit: int = 1000) -> int:
        if not math.isfinite(before):
            raise ValueError("before must be finite")
        if type(limit) is not int or not 1 <= limit <= 10_000:
            raise ValueError("limit is invalid")
        marker = self._marker
        with self.backend.transaction(write=True) as connection:
            # Expired execution fences have no authority and must not accumulate
            # forever or keep otherwise-expired sessions artificially live.
            self._prune_execution_leases_locked(
                connection, before=self._clock(), limit=limit
            )
            rows = connection.execute(
                "SELECT doc_id,session_id FROM ha_chat_sessions "
                f"WHERE updated_at<{marker} AND NOT EXISTS ("
                "SELECT 1 FROM ha_chat_session_leases AS leases WHERE "
                "leases.doc_id=ha_chat_sessions.doc_id AND "
                "leases.session_id=ha_chat_sessions.session_id "
                f"AND leases.lease_expires_at>{marker}) "
                f"ORDER BY updated_at,doc_id,session_id LIMIT {marker}",
                (before, self._clock(), limit),
            ).fetchall()
            for row in rows:
                scope = str(_column(row, "doc_id", 0))
                session = str(_column(row, "session_id", 1))
                connection.execute(
                    "DELETE FROM ha_chat_turns "
                    f"WHERE doc_id={marker} AND session_id={marker}",
                    (scope, session),
                )
                connection.execute(
                    "DELETE FROM ha_chat_sessions "
                    f"WHERE doc_id={marker} AND session_id={marker}",
                    (scope, session),
                )
        return len(rows)

    def acquire_execution(
        self,
        doc_id: str,
        session_id: str,
        lease_owner: str,
        *,
        lease_seconds: float = 300.0,
        authority: Mapping[str, Any] | None = None,
        storage_id: str | None = None,
    ) -> dict[str, Any]:
        doc_id = _clean(doc_id, "doc_id", 512)
        session_id = _clean(session_id, "session_id", 256)
        lease_owner = _clean(lease_owner, "lease_owner", 255)
        if not math.isfinite(lease_seconds) or not 5 <= lease_seconds <= 3600:
            raise ValueError("session lease_seconds is invalid")
        now = self._clock()
        token = secrets.token_urlsafe(32)
        marker = self._marker
        scope_storage_id = _clean(
            str(authority.get("storage_id") or storage_id or doc_id)
            if authority is not None
            else str(storage_id or doc_id),
            "storage_id",
            512,
        )
        with self.backend.transaction(write=True) as connection:
            if authority is not None:
                self._check_authority_locked(connection, authority, now)
            self._ensure_scope_locked(connection, doc_id, scope_storage_id, now)
            connection.execute(
                self.backend.sql(
                    sqlite=(
                        "INSERT OR IGNORE INTO ha_chat_session_leases"
                        "(doc_id,storage_id,session_id,lease_owner,lease_token,"
                        "lease_expires_at,updated_at,revision) VALUES(?,?,?,?,?,?,?,0)"
                    ),
                    postgres=(
                        "INSERT INTO ha_chat_session_leases"
                        "(doc_id,storage_id,session_id,lease_owner,lease_token,"
                        "lease_expires_at,updated_at,revision) "
                        "VALUES(%s,%s,%s,%s,%s,%s,%s,0) "
                        "ON CONFLICT(doc_id,session_id) DO NOTHING"
                    ),
                ),
                (
                    doc_id,
                    scope_storage_id,
                    session_id,
                    "unclaimed",
                    "unclaimed",
                    0.0,
                    now,
                ),
            )
            suffix = " FOR UPDATE" if self.backend.kind == "postgres" else ""
            row = connection.execute(
                "SELECT lease_owner,lease_token,lease_expires_at,revision,storage_id "
                f"FROM ha_chat_session_leases WHERE doc_id={marker} "
                f"AND session_id={marker}{suffix}",
                (doc_id, session_id),
            ).fetchone()
            if row is None:  # pragma: no cover - insert invariant
                raise RuntimeError("chat session lease disappeared")
            if str(_column(row, "storage_id", 4)) != scope_storage_id:
                raise SessionRecordConflict("chat lease storage identity changed")
            if float(_column(row, "lease_expires_at", 2)) > now:
                raise SessionBusy("chat session already has an active request")
            connection.execute(
                "UPDATE ha_chat_session_leases SET lease_owner="
                f"{marker},lease_token={marker},lease_expires_at={marker},"
                f"updated_at={marker},revision=revision+1 WHERE doc_id={marker} "
                f"AND session_id={marker}",
                (
                    lease_owner,
                    token,
                    now + lease_seconds,
                    now,
                    doc_id,
                    session_id,
                ),
            )
        return {
            "doc_id": doc_id,
            "session_id": session_id,
            "lease_owner": lease_owner,
            "lease_token": token,
            "lease_expires_at": now + lease_seconds,
        }

    def heartbeat_execution(
        self,
        doc_id: str,
        session_id: str,
        lease_owner: str,
        lease_token: str,
        *,
        lease_seconds: float = 300.0,
        authority: Mapping[str, Any] | None = None,
    ) -> None:
        doc_id = _clean(doc_id, "doc_id", 512)
        session_id = _clean(session_id, "session_id", 256)
        lease_owner = _clean(lease_owner, "lease_owner", 255)
        lease_token = _clean(lease_token, "lease_token", 512)
        if not math.isfinite(lease_seconds) or not 5 <= lease_seconds <= 3600:
            raise ValueError("session lease_seconds is invalid")
        now = self._clock()
        marker = self._marker
        with self.backend.transaction(write=True) as connection:
            if authority is not None:
                self._check_authority_locked(connection, authority, now)
            changed = connection.execute(
                "UPDATE ha_chat_session_leases SET lease_expires_at="
                f"{marker},updated_at={marker},revision=revision+1 "
                f"WHERE doc_id={marker} AND session_id={marker} "
                f"AND lease_owner={marker} AND lease_token={marker} "
                f"AND lease_expires_at>{marker}",
                (
                    now + lease_seconds,
                    now,
                    doc_id,
                    session_id,
                    lease_owner,
                    lease_token,
                    now,
                ),
            )
            if changed.rowcount != 1:
                raise StaleSessionLease("chat session execution lease is stale")

    def release_execution(
        self,
        doc_id: str,
        session_id: str,
        lease_owner: str,
        lease_token: str,
    ) -> bool:
        marker = self._marker
        with self.backend.transaction(write=True) as connection:
            changed = connection.execute(
                "DELETE FROM ha_chat_session_leases "
                f"WHERE doc_id={marker} AND session_id={marker} "
                f"AND lease_owner={marker} AND lease_token={marker}",
                (doc_id, session_id, lease_owner, lease_token),
            )
            if changed.rowcount == 1:
                self._evict_overflow(connection, doc_id)
            return changed.rowcount == 1

    def assert_execution(self, doc_id: str, session_id: str | None) -> None:
        """Fail closed when the current execution lost its durable lease."""

        if not session_id:
            return
        capability = self._execution_context.get().get((doc_id, session_id))
        if capability is None:
            raise StaleSessionLease("chat session execution context is unavailable")
        owner, token = capability
        now = self._clock()
        with self.backend.transaction() as connection:
            row = connection.execute(
                "SELECT 1 FROM ha_chat_session_leases "
                f"WHERE doc_id={self._marker} AND session_id={self._marker} "
                f"AND lease_owner={self._marker} AND lease_token={self._marker} "
                f"AND lease_expires_at>{self._marker}",
                (doc_id, session_id, owner, token, now),
            ).fetchone()
        if row is None:
            raise StaleSessionLease("chat session execution lease is stale")

    @contextmanager
    def execution(
        self,
        doc_id: str,
        session_id: str | None,
        lease_owner: str,
        *,
        lease_seconds: float = 300.0,
        authority: Mapping[str, Any] | None = None,
        storage_id: str | None = None,
    ):
        if not session_id:
            yield None
            return
        lease = self.acquire_execution(
            doc_id,
            session_id,
            lease_owner,
            lease_seconds=lease_seconds,
            authority=authority,
            storage_id=storage_id,
        )
        stopped = threading.Event()

        def heartbeat() -> None:
            interval = max(1.0, lease_seconds / 3)
            while not stopped.wait(interval):
                try:
                    self.heartbeat_execution(
                        doc_id,
                        session_id,
                        lease_owner,
                        str(lease["lease_token"]),
                        lease_seconds=lease_seconds,
                        authority=authority,
                    )
                except BaseException:
                    stopped.set()

        thread = threading.Thread(
            target=heartbeat,
            name="cogdoc-ha-chat-session",
            daemon=True,
        )
        thread.start()
        current = dict(self._execution_context.get())
        current[(doc_id, session_id)] = (lease_owner, str(lease["lease_token"]))
        context_token = self._execution_context.set(current)
        try:
            yield lease
        finally:
            self._execution_context.reset(context_token)
            stopped.set()
            thread.join(timeout=min(5.0, lease_seconds))
            try:
                self.release_execution(
                    doc_id,
                    session_id,
                    lease_owner,
                    str(lease["lease_token"]),
                )
            except Exception:
                pass

    def check(self) -> bool:
        try:
            with self.backend.transaction() as connection:
                for table in (
                    "ha_chat_memory_scopes",
                    "ha_chat_sessions",
                    "ha_chat_long_memories",
                    "ha_chat_turns",
                    "ha_chat_session_leases",
                ):
                    connection.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
            return True
        except Exception:
            return False


__all__ = [
    "DistributedSessionStore",
    "SessionBusy",
    "SessionRecordConflict",
    "StaleSessionLease",
]
