from __future__ import annotations

import base64
import json
import math
import os
import re
import secrets
import sqlite3
import time
from collections.abc import Callable, Mapping
from threading import RLock
from typing import Any
from uuid import uuid4

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from cogdoc.api.persistence import connect_sqlite


MASTER_KEYS_ENV = "COGDOC_CREDENTIAL_MASTER_KEYS"
ACTIVE_KEY_VERSION_ENV = "COGDOC_CREDENTIAL_ACTIVE_KEY_VERSION"
_KEY_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_SECRET_FIELD = re.compile(r"[a-z][a-z0-9_]{0,63}")
_PROVIDER = re.compile(r"[a-z][a-z0-9_-]{0,63}")
_MAX_SECRET_BYTES = 256 * 1024
_MAX_SECRET_FIELDS = 32
_UNSET = object()
_ACTIVE = "active"
_PENDING = "pending"
_QUARANTINED = "quarantined"


class CredentialVaultError(RuntimeError):
    """Base class for fail-closed credential-vault errors."""


class CredentialIntegrityError(CredentialVaultError):
    """The encrypted envelope could not be authenticated."""


class CredentialExpiredError(CredentialVaultError):
    """A credential is no longer valid for use."""


class CredentialRevisionConflict(CredentialVaultError):
    """Optimistic credential rotation detected a concurrent update."""


def _required(value: object, field: str, *, limit: int = 256) -> str:
    text = str(value or "").strip()
    if not text or len(text) > limit or any(ord(char) < 32 for char in text):
        raise ValueError(f"{field} is invalid")
    return text


def _optional(value: object | None, field: str, *, limit: int = 512) -> str | None:
    if value is None:
        return None
    return _required(value, field, limit=limit)


def _scope_values(
    tenant_id: object, kb_id: object, connection_id: object | None
) -> tuple[str, str, str | None]:
    return (
        _required(tenant_id, "tenant_id", limit=160),
        _required(kb_id, "kb_id", limit=160),
        _optional(connection_id, "connection_id", limit=160),
    )


def _decode_master_key(value: str, version: str) -> bytes:
    try:
        encoded = value.strip().encode("ascii")
        encoded += b"=" * (-len(encoded) % 4)
        key = base64.b64decode(encoded, altchars=b"-_", validate=True)
    except (UnicodeEncodeError, ValueError) as exc:
        raise ValueError(f"master key {version!r} is not valid base64url") from exc
    if len(key) != 32:
        raise ValueError(f"master key {version!r} must decode to 32 bytes")
    return key


def _environment_keys(env: Mapping[str, str]) -> tuple[dict[str, bytes], str]:
    raw = str(env.get(MASTER_KEYS_ENV, "")).strip()
    active = str(env.get(ACTIVE_KEY_VERSION_ENV, "")).strip()
    if not raw or not active:
        raise ValueError(f"{MASTER_KEYS_ENV} and {ACTIVE_KEY_VERSION_ENV} are required")
    try:
        values = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{MASTER_KEYS_ENV} must be a JSON object") from exc
    if not isinstance(values, dict) or not values:
        raise ValueError(f"{MASTER_KEYS_ENV} must be a non-empty JSON object")
    keys: dict[str, bytes] = {}
    for raw_version, raw_key in values.items():
        version = str(raw_version)
        if not _KEY_VERSION.fullmatch(version) or not isinstance(raw_key, str):
            raise ValueError(f"{MASTER_KEYS_ENV} contains an invalid key version")
        keys[version] = _decode_master_key(raw_key, version)
    return keys, active


def _validated_keys(
    master_keys: Mapping[str, bytes], active_key_version: str
) -> tuple[dict[str, bytes], str]:
    keys: dict[str, bytes] = {}
    for raw_version, raw_key in master_keys.items():
        version = str(raw_version)
        if not _KEY_VERSION.fullmatch(version):
            raise ValueError("master key version is invalid")
        if not isinstance(raw_key, bytes) or len(raw_key) != 32:
            raise ValueError("AES-256-GCM master keys must contain exactly 32 bytes")
        keys[version] = raw_key
    version = str(active_key_version or "").strip()
    if version not in keys:
        raise ValueError("active credential key version is unavailable")
    return keys, version


def _secret_payload(values: Mapping[str, str]) -> tuple[bytes, list[str]]:
    if not isinstance(values, Mapping) or not 1 <= len(values) <= _MAX_SECRET_FIELDS:
        raise ValueError("secret_values must contain between 1 and 32 fields")
    clean: dict[str, str] = {}
    for raw_field, raw_value in values.items():
        field = str(raw_field).strip().casefold()
        if not _SECRET_FIELD.fullmatch(field) or field in clean:
            raise ValueError("secret_values contains an invalid field")
        if not isinstance(raw_value, str) or not raw_value:
            raise ValueError(f"secret field {field!r} must be a non-empty string")
        clean[field] = raw_value
    payload = json.dumps(
        clean, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    if len(payload) > _MAX_SECRET_BYTES:
        raise ValueError("secret_values exceeds the encrypted payload limit")
    return payload, sorted(clean)


def _scopes(values: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    result: list[str] = []
    for value in values:
        scope = _required(value, "scope", limit=256)
        if scope not in result:
            result.append(scope)
    if len(result) > 128:
        raise ValueError("too many credential scopes")
    return tuple(result)


def _aad(
    credential_id: str,
    tenant_id: str,
    kb_id: str,
    connection_id: str | None,
    provider: str,
    credential_kind: str,
) -> bytes:
    # Bind ciphertext to the immutable authorization boundary. Copying an
    # envelope into a different tenant/connection row must fail authentication.
    return json.dumps(
        [
            "cogdoc-credential-envelope-v1",
            credential_id,
            tenant_id,
            kb_id,
            connection_id,
            provider,
            credential_kind,
        ],
        separators=(",", ":"),
    ).encode("utf-8")


class CredentialVault:
    """SQLite metadata plus AES-256-GCM envelope-encrypted secret payloads.

    Public metadata methods never decrypt. ``get_for_use`` is deliberately
    scope-bound and is the only plaintext-returning operation. A random data
    encryption key (DEK) protects every revision and is itself wrapped by the
    active versioned master key; neither key is stored in SQLite as plaintext.
    """

    def __init__(
        self,
        db_path: str,
        *,
        master_keys: Mapping[str, bytes] | None = None,
        active_key_version: str | None = None,
        env: Mapping[str, str] = os.environ,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if master_keys is None:
            if active_key_version is not None:
                raise ValueError("active_key_version requires injected master_keys")
            loaded_keys, loaded_active = _environment_keys(env)
        else:
            loaded_keys = dict(master_keys)
            loaded_active = str(active_key_version or "")
        self._master_keys, self.active_key_version = _validated_keys(
            loaded_keys, loaded_active
        )
        self._clock = clock
        self._lock = RLock()
        self._conn = connect_sqlite(db_path)
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS connector_credentials (
                credential_id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                kb_id TEXT NOT NULL,
                connection_id TEXT,
                provider TEXT NOT NULL,
                credential_kind TEXT NOT NULL,
                label TEXT NOT NULL,
                subject TEXT,
                scopes_json TEXT NOT NULL,
                secret_fields_json TEXT NOT NULL,
                wrapped_key_nonce BLOB NOT NULL,
                wrapped_key_ciphertext BLOB NOT NULL,
                payload_nonce BLOB NOT NULL,
                payload_ciphertext BLOB NOT NULL,
                key_version TEXT NOT NULL,
                expires_at REAL,
                last_used_at REAL,
                created_by TEXT NOT NULL,
                updated_by TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                revision INTEGER NOT NULL,
                lifecycle TEXT NOT NULL DEFAULT 'active'
            );
            CREATE INDEX IF NOT EXISTS idx_connector_credentials_scope
                ON connector_credentials(tenant_id,kb_id,connection_id,created_at,credential_id);
            CREATE TABLE IF NOT EXISTS connector_credential_events (
                event_id TEXT PRIMARY KEY,
                credential_id TEXT NOT NULL,
                tenant_id TEXT NOT NULL,
                kb_id TEXT NOT NULL,
                connection_id TEXT,
                action TEXT NOT NULL,
                actor_id TEXT NOT NULL,
                revision INTEGER NOT NULL,
                key_version TEXT NOT NULL,
                occurred_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_connector_credential_events_scope
                ON connector_credential_events(tenant_id,kb_id,occurred_at,event_id);
            CREATE INDEX IF NOT EXISTS idx_connector_credential_use_retention
                ON connector_credential_events(occurred_at,event_id)
                WHERE action='use';
            CREATE TABLE IF NOT EXISTS connector_credential_pending_bindings (
                credential_id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                kb_id TEXT NOT NULL,
                connection_id TEXT NOT NULL,
                credential_revision INTEGER NOT NULL,
                expected_connection_revision INTEGER NOT NULL,
                bound_connection_revision INTEGER NOT NULL,
                previous_credential_id TEXT,
                previous_credential_fields_json TEXT NOT NULL,
                previous_secret_env_json TEXT NOT NULL,
                created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_connector_pending_bindings_scope
                ON connector_credential_pending_bindings(tenant_id,kb_id,created_at,credential_id);
            """
        )
        columns = {
            str(row[1])
            for row in self._conn.execute(
                "PRAGMA table_info(connector_credentials)"
            ).fetchall()
        }
        if "lifecycle" not in columns:
            try:
                self._conn.execute(
                    "ALTER TABLE connector_credentials ADD COLUMN "
                    "lifecycle TEXT NOT NULL DEFAULT 'active'"
                )
            except sqlite3.OperationalError as exc:
                # Multiple app processes can race while opening the same
                # legacy database.  Accept only the exact migration race and
                # verify the resulting schema before continuing.
                if "duplicate column name" not in str(exc).casefold():
                    raise
            columns = {
                str(row[1])
                for row in self._conn.execute(
                    "PRAGMA table_info(connector_credentials)"
                ).fetchall()
            }
            if "lifecycle" not in columns:
                raise CredentialVaultError(
                    "credential lifecycle migration did not complete"
                )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_connector_credentials_lifecycle "
            "ON connector_credentials(lifecycle,updated_at,credential_id)"
        )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def check(self) -> bool:
        """Fail readiness when encrypted metadata or its audit schema is unavailable."""

        with self._lock:
            for table in (
                "connector_credentials",
                "connector_credential_events",
                "connector_credential_pending_bindings",
            ):
                self._conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
            invalid = self._conn.execute(
                "SELECT 1 FROM connector_credentials "
                "WHERE lifecycle NOT IN ('active','pending','quarantined') LIMIT 1"
            ).fetchone()
            if invalid is not None:
                raise CredentialIntegrityError("credential lifecycle is invalid")
        return True

    def create(
        self,
        *,
        tenant_id: str,
        kb_id: str,
        connection_id: str | None,
        provider: str,
        credential_kind: str,
        label: str,
        secret_values: Mapping[str, str],
        actor_id: str,
        subject: str | None = None,
        scopes: tuple[str, ...] | list[str] = (),
        expires_at: float | None = None,
        pending_activation: bool = False,
    ) -> dict[str, Any]:
        tenant, kb, connection = _scope_values(tenant_id, kb_id, connection_id)
        clean_provider = _required(provider, "provider", limit=64).casefold()
        clean_kind = _required(credential_kind, "credential_kind", limit=64).casefold()
        if not _PROVIDER.fullmatch(clean_provider) or not _PROVIDER.fullmatch(
            clean_kind
        ):
            raise ValueError("provider or credential_kind is invalid")
        if type(pending_activation) is not bool:
            raise TypeError("pending_activation must be a boolean")
        if pending_activation and clean_kind != "oauth":
            raise ValueError("only OAuth credentials support pending activation")
        clean_label = _required(label, "label", limit=160)
        clean_subject = _optional(subject, "subject", limit=512)
        clean_actor = _required(actor_id, "actor_id", limit=160)
        clean_scopes = _scopes(scopes)
        expiry = self._expiry(expires_at)
        payload, secret_fields = _secret_payload(secret_values)
        credential_id = f"cred-{uuid4().hex}"
        key_version = self.active_key_version
        envelope = self._encrypt(
            payload,
            credential_id=credential_id,
            tenant_id=tenant,
            kb_id=kb,
            connection_id=connection,
            provider=clean_provider,
            credential_kind=clean_kind,
            key_version=key_version,
        )
        now = self._clock()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    "INSERT INTO connector_credentials "
                    "(credential_id,tenant_id,kb_id,connection_id,provider,credential_kind,label,subject,"
                    "scopes_json,secret_fields_json,wrapped_key_nonce,wrapped_key_ciphertext,payload_nonce,"
                    "payload_ciphertext,key_version,expires_at,last_used_at,created_by,updated_by,created_at,"
                    "updated_at,revision,lifecycle) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        credential_id,
                        tenant,
                        kb,
                        connection,
                        clean_provider,
                        clean_kind,
                        clean_label,
                        clean_subject,
                        json.dumps(clean_scopes),
                        json.dumps(secret_fields),
                        *envelope,
                        key_version,
                        expiry,
                        None,
                        clean_actor,
                        clean_actor,
                        now,
                        now,
                        1,
                        _PENDING if pending_activation else _ACTIVE,
                    ),
                )
                self._event(
                    credential_id=credential_id,
                    tenant_id=tenant,
                    kb_id=kb,
                    connection_id=connection,
                    action="create",
                    actor_id=clean_actor,
                    revision=1,
                    key_version=key_version,
                    occurred_at=now,
                )
                self._conn.execute("COMMIT")
            except Exception:
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")
                raise
        metadata = self.get_metadata(
            credential_id,
            tenant_id=tenant,
            kb_id=kb,
            include_inactive=pending_activation,
        )
        if metadata is None:  # pragma: no cover - insertion and lookup are atomic
            raise CredentialVaultError("credential metadata was not persisted")
        return metadata

    def get_metadata(
        self,
        credential_id: str,
        *,
        tenant_id: str,
        kb_id: str,
        include_inactive: bool = False,
    ) -> dict[str, Any] | None:
        tenant, kb, _ = _scope_values(tenant_id, kb_id, None)
        if type(include_inactive) is not bool:
            raise TypeError("include_inactive must be a boolean")
        lifecycle_clause = "" if include_inactive else " AND lifecycle='active'"
        with self._lock:
            row = self._conn.execute(
                self._metadata_select()
                + " WHERE credential_id=? AND tenant_id=? AND kb_id=?"
                + lifecycle_clause,
                (credential_id, tenant, kb),
            ).fetchone()
        return self._metadata(row) if row else None

    def list_metadata(
        self,
        tenant_id: str,
        kb_id: str,
        *,
        connection_id: str | None = None,
    ) -> list[dict[str, Any]]:
        tenant, kb, connection = _scope_values(tenant_id, kb_id, connection_id)
        clause = " AND connection_id=?" if connection is not None else ""
        params: tuple[object, ...] = (
            (tenant, kb, connection) if connection is not None else (tenant, kb)
        )
        with self._lock:
            rows = self._conn.execute(
                self._metadata_select()
                + " WHERE tenant_id=? AND kb_id=?"
                + clause
                + " AND lifecycle='active'"
                + " ORDER BY created_at,credential_id",
                params,
            ).fetchall()
        return [self._metadata(row) for row in rows]

    def get_for_use(
        self,
        credential_id: str,
        *,
        tenant_id: str,
        kb_id: str,
        connection_id: str | None,
        actor_id: str,
        expected_revision: int | None = None,
    ) -> dict[str, str]:
        tenant, kb, connection = _scope_values(tenant_id, kb_id, connection_id)
        actor = _required(actor_id, "actor_id", limit=160)
        if expected_revision is not None and (
            type(expected_revision) is not int or expected_revision < 1
        ):
            raise ValueError("expected_revision must be a positive integer")
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._secret_row(credential_id, tenant, kb, connection)
                if row is None:
                    raise KeyError(credential_id)
                if expected_revision is not None and int(row[18]) != expected_revision:
                    raise CredentialRevisionConflict("credential revision has changed")
                if str(row[19]) != _ACTIVE:
                    raise CredentialExpiredError("credential is not active")
                if row[15] is not None and float(row[15]) <= self._clock():
                    raise CredentialExpiredError("credential has expired")
                values = self._decrypt_row(row)
                now = self._clock()
                self._conn.execute(
                    "UPDATE connector_credentials SET last_used_at=? WHERE credential_id=?",
                    (now, credential_id),
                )
                self._event(
                    credential_id=credential_id,
                    tenant_id=tenant,
                    kb_id=kb,
                    connection_id=connection,
                    action="use",
                    actor_id=actor,
                    revision=int(row[18]),
                    key_version=str(row[14]),
                    occurred_at=now,
                )
                self._conn.execute("COMMIT")
                return values
            except Exception:
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")
                raise

    def rotate(
        self,
        credential_id: str,
        *,
        tenant_id: str,
        kb_id: str,
        connection_id: str | None,
        actor_id: str,
        secret_values: Mapping[str, str] | None = None,
        expected_revision: int | None = None,
        expires_at: float | None | object = _UNSET,
    ) -> dict[str, Any]:
        """Rotate payload values and/or rewrap them under the active master key."""

        tenant, kb, connection = _scope_values(tenant_id, kb_id, connection_id)
        actor = _required(actor_id, "actor_id", limit=160)
        if expected_revision is not None and (
            isinstance(expected_revision, bool) or expected_revision < 1
        ):
            raise ValueError("expected_revision must be a positive integer")
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._secret_row(credential_id, tenant, kb, connection)
                if row is None:
                    raise KeyError(credential_id)
                revision = int(row[18])
                if expected_revision is not None and revision != expected_revision:
                    raise CredentialRevisionConflict("credential revision has changed")
                if str(row[19]) != _ACTIVE:
                    raise CredentialExpiredError("credential is not active")
                if secret_values is None:
                    current = self._decrypt_row(row)
                    payload, secret_fields = _secret_payload(current)
                else:
                    payload, secret_fields = _secret_payload(secret_values)
                next_expiry = (
                    row[15] if expires_at is _UNSET else self._expiry(expires_at)
                )
                key_version = self.active_key_version
                envelope = self._encrypt(
                    payload,
                    credential_id=credential_id,
                    tenant_id=tenant,
                    kb_id=kb,
                    connection_id=connection,
                    provider=str(row[4]),
                    credential_kind=str(row[5]),
                    key_version=key_version,
                )
                now = self._clock()
                updated = self._conn.execute(
                    "UPDATE connector_credentials SET secret_fields_json=?,wrapped_key_nonce=?,"
                    "wrapped_key_ciphertext=?,payload_nonce=?,payload_ciphertext=?,key_version=?,expires_at=?,"
                    "updated_by=?,updated_at=?,revision=revision+1 WHERE credential_id=? AND revision=?",
                    (
                        json.dumps(secret_fields),
                        *envelope,
                        key_version,
                        next_expiry,
                        actor,
                        now,
                        credential_id,
                        revision,
                    ),
                ).rowcount
                if updated != 1:
                    raise CredentialRevisionConflict("credential revision has changed")
                self._event(
                    credential_id=credential_id,
                    tenant_id=tenant,
                    kb_id=kb,
                    connection_id=connection,
                    action="rotate",
                    actor_id=actor,
                    revision=revision + 1,
                    key_version=key_version,
                    occurred_at=now,
                )
                self._conn.execute("COMMIT")
            except Exception:
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")
                raise
        metadata = self.get_metadata(credential_id, tenant_id=tenant, kb_id=kb)
        if metadata is None:  # pragma: no cover - protected by the transaction
            raise CredentialVaultError("credential metadata disappeared after rotation")
        return metadata

    def activate(
        self,
        credential_id: str,
        *,
        tenant_id: str,
        kb_id: str,
        connection_id: str | None,
        actor_id: str,
        expected_revision: int,
    ) -> dict[str, Any]:
        """Publish a pending OAuth credential after every live guard passes."""

        tenant, kb, connection = _scope_values(tenant_id, kb_id, connection_id)
        actor = _required(actor_id, "actor_id", limit=160)
        if isinstance(expected_revision, bool) or expected_revision < 1:
            raise ValueError("expected_revision must be a positive integer")
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT revision,key_version,lifecycle,credential_kind "
                    "FROM connector_credentials WHERE credential_id=? AND tenant_id=? "
                    "AND kb_id=? AND connection_id IS ?",
                    (credential_id, tenant, kb, connection),
                ).fetchone()
                if row is None:
                    raise KeyError(credential_id)
                revision = int(row[0])
                if revision != expected_revision:
                    raise CredentialRevisionConflict("credential revision has changed")
                if str(row[2]) != _PENDING or str(row[3]) != "oauth":
                    raise CredentialVaultError("credential is not pending activation")
                now = self._clock()
                updated = self._conn.execute(
                    "UPDATE connector_credentials SET lifecycle='active',updated_by=?,"
                    "updated_at=? WHERE credential_id=? AND revision=? "
                    "AND lifecycle='pending'",
                    (actor, now, credential_id, revision),
                ).rowcount
                if updated != 1:
                    raise CredentialRevisionConflict("credential revision has changed")
                self._event(
                    credential_id=credential_id,
                    tenant_id=tenant,
                    kb_id=kb,
                    connection_id=connection,
                    action="activate",
                    actor_id=actor,
                    revision=revision,
                    key_version=str(row[1]),
                    occurred_at=now,
                )
                self._conn.execute(
                    "DELETE FROM connector_credential_pending_bindings "
                    "WHERE credential_id=? AND tenant_id=? AND kb_id=?",
                    (credential_id, tenant, kb),
                )
                self._conn.execute("COMMIT")
            except Exception:
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")
                raise
        metadata = self.get_metadata(credential_id, tenant_id=tenant, kb_id=kb)
        if metadata is None:  # pragma: no cover - protected by the transaction
            raise CredentialVaultError("credential activation was not persisted")
        return metadata

    def prepare_binding(
        self,
        credential_id: str,
        *,
        tenant_id: str,
        kb_id: str,
        connection_id: str,
        expected_credential_revision: int,
        expected_connection_revision: int,
        previous_credential_id: str | None,
        previous_credential_fields: list[str] | tuple[str, ...],
        previous_secret_env: Mapping[str, str],
    ) -> dict[str, Any]:
        """Durably record how to undo a pending cross-store binding."""

        tenant, kb, connection = _scope_values(tenant_id, kb_id, connection_id)
        if connection is None:  # pragma: no cover - required by _scope_values call
            raise ValueError("connection_id is required")
        if (
            isinstance(expected_credential_revision, bool)
            or expected_credential_revision < 1
            or isinstance(expected_connection_revision, bool)
            or expected_connection_revision < 1
        ):
            raise ValueError("binding revisions must be positive integers")
        previous_id = _optional(
            previous_credential_id, "previous_credential_id", limit=160
        )
        fields = sorted(
            {
                _required(field, "previous_credential_field", limit=64).casefold()
                for field in previous_credential_fields
            }
        )
        if any(not _SECRET_FIELD.fullmatch(field) for field in fields):
            raise ValueError("previous credential fields are invalid")
        if not isinstance(previous_secret_env, Mapping):
            raise TypeError("previous_secret_env must be a mapping")
        secret_env = {
            _required(field, "previous secret field", limit=64).casefold(): _required(
                env_name, "previous secret environment name", limit=256
            )
            for field, env_name in previous_secret_env.items()
        }
        if any(not _SECRET_FIELD.fullmatch(field) for field in secret_env):
            raise ValueError("previous secret environment fields are invalid")
        if previous_id is not None and secret_env:
            raise ValueError("previous credential and environment sources conflict")
        if previous_id is None and fields:
            raise ValueError("previous credential fields require a credential id")
        if previous_id is not None and not fields:
            raise ValueError("previous credential fields are required")
        binding = {
            "credential_id": _required(credential_id, "credential_id", limit=160),
            "tenant_id": tenant,
            "kb_id": kb,
            "connection_id": connection,
            "credential_revision": expected_credential_revision,
            "expected_connection_revision": expected_connection_revision,
            "bound_connection_revision": expected_connection_revision + 1,
            "previous_credential_id": previous_id,
            "previous_credential_fields": fields,
            "previous_secret_env": secret_env,
        }
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT revision,lifecycle FROM connector_credentials "
                    "WHERE credential_id=? AND tenant_id=? AND kb_id=? AND connection_id=?",
                    (credential_id, tenant, kb, connection),
                ).fetchone()
                if row is None:
                    raise KeyError(credential_id)
                if int(row[0]) != expected_credential_revision:
                    raise CredentialRevisionConflict("credential revision has changed")
                if str(row[1]) != _PENDING:
                    raise CredentialVaultError("credential is not pending activation")
                existing = self._conn.execute(
                    "SELECT tenant_id,kb_id,connection_id,credential_revision,"
                    "expected_connection_revision,bound_connection_revision,"
                    "previous_credential_id,previous_credential_fields_json,"
                    "previous_secret_env_json FROM connector_credential_pending_bindings "
                    "WHERE credential_id=?",
                    (credential_id,),
                ).fetchone()
                if existing is None:
                    self._conn.execute(
                        "INSERT INTO connector_credential_pending_bindings "
                        "(credential_id,tenant_id,kb_id,connection_id,credential_revision,"
                        "expected_connection_revision,bound_connection_revision,"
                        "previous_credential_id,previous_credential_fields_json,"
                        "previous_secret_env_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            credential_id,
                            tenant,
                            kb,
                            connection,
                            expected_credential_revision,
                            expected_connection_revision,
                            expected_connection_revision + 1,
                            previous_id,
                            json.dumps(fields),
                            json.dumps(secret_env, sort_keys=True),
                            self._clock(),
                        ),
                    )
                else:
                    actual = (
                        str(existing[0]),
                        str(existing[1]),
                        str(existing[2]),
                        int(existing[3]),
                        int(existing[4]),
                        int(existing[5]),
                        existing[6],
                        json.loads(existing[7]),
                        json.loads(existing[8]),
                    )
                    expected = (
                        tenant,
                        kb,
                        connection,
                        expected_credential_revision,
                        expected_connection_revision,
                        expected_connection_revision + 1,
                        previous_id,
                        fields,
                        secret_env,
                    )
                    if actual != expected:
                        raise CredentialRevisionConflict(
                            "credential binding journal has changed"
                        )
                self._conn.execute("COMMIT")
            except Exception:
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")
                raise
        return binding

    def pending_bindings(self, *, limit: int = 1_000) -> list[dict[str, Any]]:
        """Return a bounded recovery batch without exposing secret values."""

        if isinstance(limit, bool) or not 1 <= limit <= 10_000:
            raise ValueError("limit must be between 1 and 10000")
        with self._lock:
            rows = self._conn.execute(
                "SELECT b.credential_id,b.tenant_id,b.kb_id,b.connection_id,"
                "b.credential_revision,b.expected_connection_revision,"
                "b.bound_connection_revision,b.previous_credential_id,"
                "b.previous_credential_fields_json,b.previous_secret_env_json,"
                "c.lifecycle FROM connector_credential_pending_bindings b "
                "LEFT JOIN connector_credentials c ON c.credential_id=b.credential_id "
                "ORDER BY b.created_at,b.credential_id LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {
                "credential_id": str(row[0]),
                "tenant_id": str(row[1]),
                "kb_id": str(row[2]),
                "connection_id": str(row[3]),
                "credential_revision": int(row[4]),
                "expected_connection_revision": int(row[5]),
                "bound_connection_revision": int(row[6]),
                "previous_credential_id": row[7],
                "previous_credential_fields": json.loads(row[8]),
                "previous_secret_env": json.loads(row[9]),
                "lifecycle": row[10],
            }
            for row in rows
        ]

    def clear_pending_binding(
        self,
        credential_id: str,
        *,
        tenant_id: str,
        kb_id: str,
    ) -> bool:
        tenant, kb, _ = _scope_values(tenant_id, kb_id, None)
        clean_id = _required(credential_id, "credential_id", limit=160)
        with self._lock:
            return (
                self._conn.execute(
                    "DELETE FROM connector_credential_pending_bindings "
                    "WHERE credential_id=? AND tenant_id=? AND kb_id=?",
                    (clean_id, tenant, kb),
                ).rowcount
                == 1
            )

    def quarantine(
        self,
        credential_id: str,
        *,
        tenant_id: str,
        kb_id: str,
        connection_id: str | None,
        actor_id: str,
        expected_revision: int,
    ) -> bool:
        """Make one owned revision permanently unusable pending deletion."""

        tenant, kb, connection = _scope_values(tenant_id, kb_id, connection_id)
        actor = _required(actor_id, "actor_id", limit=160)
        if isinstance(expected_revision, bool) or expected_revision < 1:
            raise ValueError("expected_revision must be a positive integer")
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT revision,key_version,lifecycle FROM connector_credentials "
                    "WHERE credential_id=? AND tenant_id=? AND kb_id=? AND connection_id IS ?",
                    (credential_id, tenant, kb, connection),
                ).fetchone()
                if row is None:
                    self._conn.execute("COMMIT")
                    return False
                revision = int(row[0])
                if revision != expected_revision:
                    raise CredentialRevisionConflict("credential revision has changed")
                if str(row[2]) == _QUARANTINED:
                    self._conn.execute("COMMIT")
                    return True
                now = self._clock()
                updated = self._conn.execute(
                    "UPDATE connector_credentials SET lifecycle='quarantined',updated_by=?,"
                    "updated_at=?,revision=revision+1 WHERE credential_id=? AND revision=?",
                    (actor, now, credential_id, revision),
                ).rowcount
                if updated != 1:
                    raise CredentialRevisionConflict("credential revision has changed")
                self._event(
                    credential_id=credential_id,
                    tenant_id=tenant,
                    kb_id=kb,
                    connection_id=connection,
                    action="quarantine",
                    actor_id=actor,
                    revision=revision + 1,
                    key_version=str(row[1]),
                    occurred_at=now,
                )
                self._conn.execute("COMMIT")
                return True
            except Exception:
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")
                raise

    def delete(
        self,
        credential_id: str,
        *,
        tenant_id: str,
        kb_id: str,
        connection_id: str | None,
        actor_id: str,
        expected_revision: int | None = None,
    ) -> bool:
        tenant, kb, connection = _scope_values(tenant_id, kb_id, connection_id)
        actor = _required(actor_id, "actor_id", limit=160)
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT revision,key_version,credential_kind FROM connector_credentials "
                    "WHERE credential_id=? AND tenant_id=? AND kb_id=? AND connection_id IS ?",
                    (credential_id, tenant, kb, connection),
                ).fetchone()
                if row is None:
                    self._conn.execute("COMMIT")
                    return False
                binding = self._conn.execute(
                    "SELECT 1 FROM connector_credential_pending_bindings "
                    "WHERE credential_id=?",
                    (credential_id,),
                ).fetchone()
                if binding is not None:
                    raise CredentialVaultError(
                        "pending credential binding must be reconciled before deletion"
                    )
                revision = int(row[0])
                if expected_revision is not None and revision != expected_revision:
                    raise CredentialRevisionConflict("credential revision has changed")
                now = self._clock()
                self._event(
                    credential_id=credential_id,
                    tenant_id=tenant,
                    kb_id=kb,
                    connection_id=connection,
                    action="delete",
                    actor_id=actor,
                    revision=revision,
                    key_version=str(row[1]),
                    occurred_at=now,
                )
                self._conn.execute(
                    "DELETE FROM connector_credentials WHERE credential_id=?",
                    (credential_id,),
                )
                if str(row[2]) == "oauth-session":
                    # Verifier credentials and their audit records disappear
                    # in one vault transaction. Public audit readers can
                    # therefore never observe the delete-to-purge window.
                    self._conn.execute(
                        "DELETE FROM connector_credential_events WHERE credential_id=? "
                        "AND tenant_id=? AND kb_id=?",
                        (credential_id, tenant, kb),
                    )
                self._conn.execute("COMMIT")
                return True
            except Exception:
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")
                raise

    def audit_events(
        self,
        tenant_id: str,
        kb_id: str,
        *,
        credential_id: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        tenant, kb, _ = _scope_values(tenant_id, kb_id, None)
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        clause = " AND e.credential_id=?" if credential_id is not None else ""
        params: tuple[object, ...] = (
            (tenant, kb, credential_id, limit)
            if credential_id is not None
            else (tenant, kb, limit)
        )
        with self._lock:
            rows = self._conn.execute(
                "SELECT e.event_id,e.credential_id,e.tenant_id,e.kb_id,e.connection_id,e.action,"
                "e.actor_id,e.revision,e.key_version,e.occurred_at "
                "FROM connector_credential_events e WHERE e.tenant_id=? AND e.kb_id=? "
                "AND NOT EXISTS (SELECT 1 FROM connector_credentials c "
                "WHERE c.credential_id=e.credential_id AND "
                "(c.credential_kind='oauth-session' OR c.lifecycle!='active'))"
                + clause
                + " ORDER BY e.occurred_at DESC,e.rowid DESC LIMIT ?",
                params,
            ).fetchall()
        return [
            {
                "event_id": row[0],
                "credential_id": row[1],
                "tenant_id": row[2],
                "kb_id": row[3],
                "connection_id": row[4],
                "action": row[5],
                "actor_id": row[6],
                "revision": row[7],
                "key_version": row[8],
                "occurred_at": row[9],
            }
            for row in rows
        ]

    def prune_use_audit_events(self, *, older_than: float, limit: int = 1_000) -> int:
        """Delete a bounded batch of old high-volume ``use`` events.

        Create, rotate, and delete records are deliberately retained as the
        long-lived security audit trail. Periodic connector polling can emit a
        use event every minute, so those operational reads have an independent
        finite retention window.
        """

        boundary = float(older_than)
        if not math.isfinite(boundary) or boundary < 0:
            raise ValueError("older_than must be a finite non-negative timestamp")
        if isinstance(limit, bool) or not 1 <= limit <= 10_000:
            raise ValueError("limit must be between 1 and 10000")
        with self._lock:
            return int(
                self._conn.execute(
                    "DELETE FROM connector_credential_events WHERE event_id IN ("
                    "SELECT event_id FROM connector_credential_events "
                    "WHERE action='use' AND occurred_at<? "
                    "ORDER BY occurred_at,event_id LIMIT ?)",
                    (boundary, limit),
                ).rowcount
            )

    def prune_inactive_credentials(
        self, *, older_than: float, limit: int = 1_000
    ) -> int:
        """Delete a bounded batch of abandoned pending/quarantined envelopes."""

        boundary = float(older_than)
        if not math.isfinite(boundary) or boundary < 0:
            raise ValueError("older_than must be a finite non-negative timestamp")
        if isinstance(limit, bool) or not 1 <= limit <= 10_000:
            raise ValueError("limit must be between 1 and 10000")
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                rows = self._conn.execute(
                    "SELECT credential_id FROM connector_credentials "
                    "WHERE lifecycle IN ('pending','quarantined') AND updated_at<? "
                    "AND NOT EXISTS (SELECT 1 FROM connector_credential_pending_bindings b "
                    "WHERE b.credential_id=connector_credentials.credential_id) "
                    "ORDER BY updated_at,credential_id LIMIT ?",
                    (boundary, limit),
                ).fetchall()
                credential_ids = [str(row[0]) for row in rows]
                if credential_ids:
                    self._conn.executemany(
                        "DELETE FROM connector_credential_events WHERE credential_id=?",
                        ((credential_id,) for credential_id in credential_ids),
                    )
                    self._conn.executemany(
                        "DELETE FROM connector_credentials WHERE credential_id=? "
                        "AND lifecycle IN ('pending','quarantined')",
                        ((credential_id,) for credential_id in credential_ids),
                    )
                self._conn.execute("COMMIT")
                return len(credential_ids)
            except Exception:
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")
                raise

    def delete_scope(self, tenant_id: str, kb_id: str) -> dict[str, int]:
        """Erase every encrypted capability and audit row for a deleted KB."""

        tenant, kb, _ = _scope_values(tenant_id, kb_id, None)
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    "DELETE FROM connector_credential_pending_bindings "
                    "WHERE tenant_id=? AND kb_id=?",
                    (tenant, kb),
                )
                events = self._conn.execute(
                    "DELETE FROM connector_credential_events WHERE tenant_id=? AND kb_id=?",
                    (tenant, kb),
                ).rowcount
                credentials = self._conn.execute(
                    "DELETE FROM connector_credentials WHERE tenant_id=? AND kb_id=?",
                    (tenant, kb),
                ).rowcount
                self._conn.execute("COMMIT")
            except Exception:
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")
                raise
        return {"credentials": int(credentials), "events": int(events)}

    def expired_internal_credentials(
        self, *, expires_at: float, limit: int = 100
    ) -> list[dict[str, Any]]:
        """Return a bounded batch of expired PKCE verifier metadata.

        This internal reconciliation seam lets ``OAuthSessionStore`` remove a
        vault row left by a process loss between encrypted verifier creation
        and session-row insertion, including when the two stores use separate
        SQLite databases. No secret values are exposed.
        """

        cutoff = float(expires_at)
        if not math.isfinite(cutoff) or cutoff < 0:
            raise ValueError("expires_at must be a finite non-negative timestamp")
        if isinstance(limit, bool) or not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        with self._lock:
            rows = self._conn.execute(
                self._metadata_select()
                + " WHERE credential_kind='oauth-session' AND expires_at IS NOT NULL "
                "AND expires_at<=? ORDER BY expires_at,credential_id LIMIT ?",
                (cutoff, limit),
            ).fetchall()
        return [self._metadata(row) for row in rows]

    def purge_internal_audit_events(
        self,
        credential_id: str,
        *,
        tenant_id: str,
        kb_id: str,
    ) -> int:
        """Delete audit records for an ephemeral, non-public credential.

        OAuth PKCE verifiers intentionally leave no public credential audit
        trail. Keep this operation scope-bound so a caller cannot use a leaked
        credential identifier to erase another tenant's records.
        """

        tenant, kb, _ = _scope_values(tenant_id, kb_id, None)
        clean_credential_id = _required(credential_id, "credential_id", limit=160)
        with self._lock:
            return self._conn.execute(
                "DELETE FROM connector_credential_events WHERE credential_id=? "
                "AND tenant_id=? AND kb_id=?",
                (clean_credential_id, tenant, kb),
            ).rowcount

    def _encrypt(
        self,
        payload: bytes,
        *,
        credential_id: str,
        tenant_id: str,
        kb_id: str,
        connection_id: str | None,
        provider: str,
        credential_kind: str,
        key_version: str,
    ) -> tuple[bytes, bytes, bytes, bytes]:
        aad = _aad(
            credential_id,
            tenant_id,
            kb_id,
            connection_id,
            provider,
            credential_kind,
        )
        data_key = AESGCM.generate_key(256)
        payload_nonce = secrets.token_bytes(12)
        payload_ciphertext = AESGCM(data_key).encrypt(payload_nonce, payload, aad)
        wrapped_key_nonce = secrets.token_bytes(12)
        wrapped_key_ciphertext = AESGCM(self._master_keys[key_version]).encrypt(
            wrapped_key_nonce,
            data_key,
            aad + b"\0" + key_version.encode("ascii"),
        )
        return (
            wrapped_key_nonce,
            wrapped_key_ciphertext,
            payload_nonce,
            payload_ciphertext,
        )

    def _decrypt_row(self, row: tuple[Any, ...]) -> dict[str, str]:
        credential_id = str(row[0])
        key_version = str(row[14])
        master_key = self._master_keys.get(key_version)
        if master_key is None:
            raise CredentialVaultError("credential master key version is unavailable")
        aad = _aad(
            credential_id,
            str(row[1]),
            str(row[2]),
            row[3],
            str(row[4]),
            str(row[5]),
        )
        try:
            data_key = AESGCM(master_key).decrypt(
                bytes(row[10]),
                bytes(row[11]),
                aad + b"\0" + key_version.encode("ascii"),
            )
            payload = AESGCM(data_key).decrypt(bytes(row[12]), bytes(row[13]), aad)
            values = json.loads(payload.decode("utf-8"))
            expected_fields = json.loads(row[9])
        except (
            InvalidTag,
            UnicodeDecodeError,
            json.JSONDecodeError,
            ValueError,
        ) as exc:
            raise CredentialIntegrityError(
                "credential envelope authentication failed"
            ) from exc
        if not isinstance(values, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in values.items()
        ):
            raise CredentialIntegrityError("credential payload is invalid")
        if not isinstance(expected_fields, list) or sorted(values) != expected_fields:
            raise CredentialIntegrityError("credential field metadata is inconsistent")
        return values

    def _secret_row(
        self,
        credential_id: str,
        tenant_id: str,
        kb_id: str,
        connection_id: str | None,
    ) -> tuple[Any, ...] | None:
        return self._conn.execute(
            "SELECT credential_id,tenant_id,kb_id,connection_id,provider,credential_kind,label,subject,"
            "scopes_json,secret_fields_json,wrapped_key_nonce,wrapped_key_ciphertext,payload_nonce,"
            "payload_ciphertext,key_version,expires_at,created_at,updated_at,revision,lifecycle "
            "FROM connector_credentials WHERE credential_id=? AND tenant_id=? AND kb_id=? "
            "AND connection_id IS ?",
            (credential_id, tenant_id, kb_id, connection_id),
        ).fetchone()

    @staticmethod
    def _metadata_select() -> str:
        return (
            "SELECT credential_id,tenant_id,kb_id,connection_id,provider,credential_kind,label,subject,"
            "scopes_json,secret_fields_json,key_version,expires_at,last_used_at,created_by,updated_by,"
            "created_at,updated_at,revision FROM connector_credentials"
        )

    @staticmethod
    def _metadata(row: tuple[Any, ...]) -> dict[str, Any]:
        return {
            "credential_id": row[0],
            "tenant_id": row[1],
            "kb_id": row[2],
            "connection_id": row[3],
            "provider": row[4],
            "credential_kind": row[5],
            "label": row[6],
            "subject": row[7],
            "scopes": json.loads(row[8]),
            "secret_fields": json.loads(row[9]),
            "key_version": row[10],
            "expires_at": row[11],
            "last_used_at": row[12],
            "created_by": row[13],
            "updated_by": row[14],
            "created_at": row[15],
            "updated_at": row[16],
            "revision": row[17],
        }

    def _expiry(self, value: object | None) -> float | None:
        if value is None:
            return None
        if isinstance(value, bool):
            raise ValueError("expires_at must be a timestamp")
        if not isinstance(value, (str, int, float)):
            raise ValueError("expires_at must be a timestamp")
        try:
            expiry = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("expires_at must be a timestamp") from exc
        if not math.isfinite(expiry) or expiry <= self._clock():
            raise ValueError("expires_at must be in the future")
        return expiry

    def _event(
        self,
        *,
        credential_id: str,
        tenant_id: str,
        kb_id: str,
        connection_id: str | None,
        action: str,
        actor_id: str,
        revision: int,
        key_version: str,
        occurred_at: float,
    ) -> None:
        self._conn.execute(
            "INSERT INTO connector_credential_events "
            "(event_id,credential_id,tenant_id,kb_id,connection_id,action,actor_id,revision,key_version,"
            "occurred_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                f"cevt-{uuid4().hex}",
                credential_id,
                tenant_id,
                kb_id,
                connection_id,
                action,
                actor_id,
                revision,
                key_version,
                occurred_at,
            ),
        )
