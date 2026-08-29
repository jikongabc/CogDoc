from __future__ import annotations

import base64
import json
import sqlite3

import pytest

from cogdoc.connectors.credential_store import (
    ACTIVE_KEY_VERSION_ENV,
    MASTER_KEYS_ENV,
    CredentialExpiredError,
    CredentialIntegrityError,
    CredentialRevisionConflict,
    CredentialVault,
    delete_sqlite_connector_secret_scope,
)


KEY_V1 = b"1" * 32
KEY_V2 = b"2" * 32


def _vault(tmp_path, *, keys=None, active="v1", clock=None):
    return CredentialVault(
        str(tmp_path / "state.db"),
        master_keys=keys or {"v1": KEY_V1},
        active_key_version=active,
        clock=clock or (lambda: 1_000.0),
    )


def _create(vault, **overrides):
    values = {
        "tenant_id": "tenant-a",
        "kb_id": "kb-a",
        "connection_id": "conn-a",
        "provider": "notion",
        "credential_kind": "manual",
        "label": "Notion production",
        "secret_values": {"token": "ntn_super_secret"},
        "actor_id": "user-a",
        "subject": "workspace-a",
        "scopes": ["read:content"],
    }
    values.update(overrides)
    return vault.create(**values)


def _database_bytes(path) -> bytes:
    result = b""
    for suffix in ("", "-wal", "-shm"):
        candidate = path.parent / f"{path.name}{suffix}"
        if candidate.exists():
            result += candidate.read_bytes()
    return result


def test_manual_credential_is_envelope_encrypted_and_metadata_is_redacted(tmp_path):
    vault = _vault(tmp_path)
    metadata = _create(vault)

    assert metadata["secret_fields"] == ["token"]
    assert metadata["credential_kind"] == "manual"
    assert metadata["key_version"] == "v1"
    assert metadata["revision"] == 1
    assert "ntn_super_secret" not in json.dumps(metadata)
    assert b"ntn_super_secret" not in _database_bytes(tmp_path / "state.db")

    assert vault.get_for_use(
        metadata["credential_id"],
        tenant_id="tenant-a",
        kb_id="kb-a",
        connection_id="conn-a",
        actor_id="connector-worker",
    ) == {"token": "ntn_super_secret"}
    refreshed = vault.get_metadata(
        metadata["credential_id"], tenant_id="tenant-a", kb_id="kb-a"
    )
    assert refreshed is not None
    assert refreshed["last_used_at"] == 1_000.0
    vault.close()


def test_legacy_vault_schema_migrates_credentials_to_active_lifecycle(tmp_path):
    vault = _vault(tmp_path)
    metadata = _create(vault)
    vault.close()
    with sqlite3.connect(tmp_path / "state.db") as connection:
        connection.execute("DROP INDEX idx_connector_credentials_lifecycle")
        connection.execute("ALTER TABLE connector_credentials DROP COLUMN lifecycle")

    reopened = _vault(tmp_path)
    assert (
        reopened.get_metadata(
            metadata["credential_id"], tenant_id="tenant-a", kb_id="kb-a"
        )
        is not None
    )
    assert reopened.get_for_use(
        metadata["credential_id"],
        tenant_id="tenant-a",
        kb_id="kb-a",
        connection_id="conn-a",
        actor_id="worker",
    ) == {"token": "ntn_super_secret"}
    reopened.close()


def test_scope_is_required_for_plaintext_use_and_delete(tmp_path):
    vault = _vault(tmp_path)
    metadata = _create(vault)

    with pytest.raises(KeyError):
        vault.get_for_use(
            metadata["credential_id"],
            tenant_id="tenant-b",
            kb_id="kb-a",
            connection_id="conn-a",
            actor_id="intruder",
        )
    with pytest.raises(KeyError):
        vault.get_for_use(
            metadata["credential_id"],
            tenant_id="tenant-a",
            kb_id="kb-a",
            connection_id="conn-b",
            actor_id="intruder",
        )
    assert (
        vault.delete(
            metadata["credential_id"],
            tenant_id="tenant-b",
            kb_id="kb-a",
            connection_id="conn-a",
            actor_id="intruder",
        )
        is False
    )
    vault.close()


def test_rotate_changes_revision_and_supports_master_key_rewrap(tmp_path):
    vault = _vault(tmp_path, keys={"v1": KEY_V1, "v2": KEY_V2}, active="v1")
    metadata = _create(vault)
    vault.close()

    rotating = _vault(tmp_path, keys={"v1": KEY_V1, "v2": KEY_V2}, active="v2")
    rewrapped = rotating.rotate(
        metadata["credential_id"],
        tenant_id="tenant-a",
        kb_id="kb-a",
        connection_id="conn-a",
        actor_id="security-admin",
        expected_revision=1,
    )
    assert rewrapped["key_version"] == "v2"
    assert rewrapped["revision"] == 2
    rotating.close()

    # A successful envelope-key rotation no longer needs the retired v1 key.
    current_only = _vault(tmp_path, keys={"v2": KEY_V2}, active="v2")
    assert current_only.get_for_use(
        metadata["credential_id"],
        tenant_id="tenant-a",
        kb_id="kb-a",
        connection_id="conn-a",
        actor_id="connector-worker",
    ) == {"token": "ntn_super_secret"}
    current_only.close()


def test_secret_rotation_is_optimistic_and_audited(tmp_path):
    vault = _vault(tmp_path)
    metadata = _create(vault)
    rotated = vault.rotate(
        metadata["credential_id"],
        tenant_id="tenant-a",
        kb_id="kb-a",
        connection_id="conn-a",
        actor_id="security-admin",
        secret_values={"token": "replacement-token", "refresh_token": "refresh-2"},
        expected_revision=1,
    )
    assert rotated["revision"] == 2
    assert rotated["secret_fields"] == ["refresh_token", "token"]
    with pytest.raises(CredentialRevisionConflict):
        vault.rotate(
            metadata["credential_id"],
            tenant_id="tenant-a",
            kb_id="kb-a",
            connection_id="conn-a",
            actor_id="stale-admin",
            secret_values={"token": "must-not-win"},
            expected_revision=1,
        )
    assert vault.get_for_use(
        metadata["credential_id"],
        tenant_id="tenant-a",
        kb_id="kb-a",
        connection_id="conn-a",
        actor_id="worker",
    ) == {"token": "replacement-token", "refresh_token": "refresh-2"}

    events = vault.audit_events(
        "tenant-a", "kb-a", credential_id=metadata["credential_id"]
    )
    assert [event["action"] for event in reversed(events)] == [
        "create",
        "rotate",
        "use",
    ]
    assert "replacement-token" not in json.dumps(events)
    vault.close()


def test_get_for_use_rejects_stale_expected_revision_before_decryption(tmp_path):
    vault = _vault(tmp_path)
    metadata = _create(vault)

    with pytest.raises(CredentialRevisionConflict):
        vault.get_for_use(
            metadata["credential_id"],
            tenant_id="tenant-a",
            kb_id="kb-a",
            connection_id="conn-a",
            actor_id="stale-worker",
            expected_revision=2,
        )

    assert [event["action"] for event in vault.audit_events("tenant-a", "kb-a")] == [
        "create"
    ]
    vault.close()


def test_high_volume_use_audit_has_bounded_retention_without_erasing_security_events(
    tmp_path,
):
    now = [0.0]
    vault = _vault(tmp_path, clock=lambda: now[0])
    metadata = _create(vault)
    for timestamp in (10.0, 20.0):
        now[0] = timestamp
        vault.get_for_use(
            metadata["credential_id"],
            tenant_id="tenant-a",
            kb_id="kb-a",
            connection_id="conn-a",
            actor_id="worker",
        )
    now[0] = 30.0
    vault.rotate(
        metadata["credential_id"],
        tenant_id="tenant-a",
        kb_id="kb-a",
        connection_id="conn-a",
        actor_id="admin",
        expected_revision=1,
    )
    now[0] = 100.0
    vault.get_for_use(
        metadata["credential_id"],
        tenant_id="tenant-a",
        kb_id="kb-a",
        connection_id="conn-a",
        actor_id="worker",
    )

    assert vault.prune_use_audit_events(older_than=50.0, limit=1) == 1
    assert vault.prune_use_audit_events(older_than=50.0, limit=1) == 1
    assert vault.prune_use_audit_events(older_than=50.0, limit=1) == 0
    actions = [
        event["action"] for event in reversed(vault.audit_events("tenant-a", "kb-a"))
    ]
    assert actions == ["create", "rotate", "use"]
    vault.close()


def test_expiry_is_fail_closed(tmp_path):
    now = [1_000.0]
    vault = _vault(tmp_path, clock=lambda: now[0])
    metadata = _create(vault, expires_at=1_100.0)
    now[0] = 1_100.0
    with pytest.raises(CredentialExpiredError, match="expired"):
        vault.get_for_use(
            metadata["credential_id"],
            tenant_id="tenant-a",
            kb_id="kb-a",
            connection_id="conn-a",
            actor_id="worker",
        )
    vault.close()


def test_pending_oauth_credential_is_hidden_until_activation_and_pruned_when_quarantined(
    tmp_path,
):
    now = [1_000.0]
    vault = _vault(tmp_path, clock=lambda: now[0])
    pending = _create(
        vault,
        credential_kind="oauth",
        pending_activation=True,
    )
    credential_id = str(pending["credential_id"])

    assert vault.get_metadata(credential_id, tenant_id="tenant-a", kb_id="kb-a") is None
    assert credential_id not in {
        row["credential_id"] for row in vault.list_metadata("tenant-a", "kb-a")
    }
    with pytest.raises(CredentialExpiredError, match="not active"):
        vault.get_for_use(
            credential_id,
            tenant_id="tenant-a",
            kb_id="kb-a",
            connection_id="conn-a",
            actor_id="worker",
        )

    active = vault.activate(
        credential_id,
        tenant_id="tenant-a",
        kb_id="kb-a",
        connection_id="conn-a",
        actor_id="user-a",
        expected_revision=1,
    )
    assert active["revision"] == 1
    assert vault.get_for_use(
        credential_id,
        tenant_id="tenant-a",
        kb_id="kb-a",
        connection_id="conn-a",
        actor_id="worker",
    ) == {"token": "ntn_super_secret"}

    abandoned = _create(
        vault,
        credential_kind="oauth",
        pending_activation=True,
        label="abandoned callback",
    )
    assert vault.quarantine(
        abandoned["credential_id"],
        tenant_id="tenant-a",
        kb_id="kb-a",
        connection_id="conn-a",
        actor_id="oauth-rollback",
        expected_revision=1,
    )
    now[0] = 2_000.0
    assert vault.prune_inactive_credentials(older_than=1_500.0) == 1
    assert (
        vault.get_metadata(
            abandoned["credential_id"],
            tenant_id="tenant-a",
            kb_id="kb-a",
            include_inactive=True,
        )
        is None
    )
    vault.close()


def test_non_finite_expiry_is_rejected(tmp_path):
    vault = _vault(tmp_path)
    with pytest.raises(ValueError, match="future"):
        _create(vault, expires_at=float("nan"))
    with pytest.raises(ValueError, match="future"):
        _create(vault, expires_at=float("inf"))
    vault.close()


def test_authenticated_envelope_detects_ciphertext_and_scope_tampering(tmp_path):
    vault = _vault(tmp_path)
    metadata = _create(vault)
    connection = sqlite3.connect(tmp_path / "state.db")
    ciphertext = connection.execute(
        "SELECT payload_ciphertext FROM connector_credentials WHERE credential_id=?",
        (metadata["credential_id"],),
    ).fetchone()[0]
    modified = bytearray(ciphertext)
    modified[-1] ^= 1
    connection.execute(
        "UPDATE connector_credentials SET payload_ciphertext=? WHERE credential_id=?",
        (bytes(modified), metadata["credential_id"]),
    )
    connection.commit()
    connection.close()

    with pytest.raises(CredentialIntegrityError, match="authentication"):
        vault.get_for_use(
            metadata["credential_id"],
            tenant_id="tenant-a",
            kb_id="kb-a",
            connection_id="conn-a",
            actor_id="worker",
        )
    vault.close()


def test_tampered_secret_field_metadata_fails_as_integrity_error(tmp_path):
    vault = _vault(tmp_path)
    metadata = _create(vault)
    connection = sqlite3.connect(tmp_path / "state.db")
    connection.execute(
        "UPDATE connector_credentials SET secret_fields_json=? WHERE credential_id=?",
        ("not-json", metadata["credential_id"]),
    )
    connection.commit()
    connection.close()
    with pytest.raises(CredentialIntegrityError, match="authentication"):
        vault.get_for_use(
            metadata["credential_id"],
            tenant_id="tenant-a",
            kb_id="kb-a",
            connection_id="conn-a",
            actor_id="worker",
        )
    vault.close()


def test_delete_removes_secret_but_keeps_non_secret_audit_record(tmp_path):
    vault = _vault(tmp_path)
    metadata = _create(vault)
    assert vault.delete(
        metadata["credential_id"],
        tenant_id="tenant-a",
        kb_id="kb-a",
        connection_id="conn-a",
        actor_id="security-admin",
        expected_revision=1,
    )
    assert (
        vault.get_metadata(
            metadata["credential_id"], tenant_id="tenant-a", kb_id="kb-a"
        )
        is None
    )
    events = vault.audit_events("tenant-a", "kb-a")
    assert [event["action"] for event in reversed(events)] == ["create", "delete"]
    vault.close()


def test_internal_audit_purge_is_tenant_and_kb_scoped(tmp_path):
    vault = _vault(tmp_path)
    internal = _create(
        vault,
        connection_id=None,
        credential_kind="oauth-session",
        secret_values={"code_verifier": "v" * 64},
    )
    other = _create(
        vault,
        tenant_id="tenant-b",
        kb_id="kb-b",
        connection_id=None,
    )
    assert (
        vault.purge_internal_audit_events(
            internal["credential_id"],
            tenant_id="tenant-b",
            kb_id="kb-b",
        )
        == 0
    )
    with sqlite3.connect(tmp_path / "state.db") as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM connector_credential_events WHERE credential_id=?",
                (internal["credential_id"],),
            ).fetchone()[0]
            == 1
        )
    assert (
        vault.purge_internal_audit_events(
            internal["credential_id"],
            tenant_id="tenant-a",
            kb_id="kb-a",
        )
        == 1
    )
    assert vault.audit_events("tenant-a", "kb-a") == []
    with sqlite3.connect(tmp_path / "state.db") as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM connector_credential_events WHERE credential_id=?",
                (internal["credential_id"],),
            ).fetchone()[0]
            == 0
        )
    assert {
        event["credential_id"] for event in vault.audit_events("tenant-b", "kb-b")
    } == {other["credential_id"]}
    vault.close()


def test_master_keys_can_be_loaded_from_environment_mapping(tmp_path):
    encoded = base64.urlsafe_b64encode(KEY_V1).rstrip(b"=").decode("ascii")
    vault = CredentialVault(
        str(tmp_path / "state.db"),
        env={
            MASTER_KEYS_ENV: json.dumps({"primary-2026": encoded}),
            ACTIVE_KEY_VERSION_ENV: "primary-2026",
        },
        clock=lambda: 1_000.0,
    )
    metadata = _create(vault)
    assert metadata["key_version"] == "primary-2026"
    vault.close()


@pytest.mark.parametrize(
    "values",
    [
        {},
        {"Bad Field": "value"},
        {"token": ""},
        {"token": 123},
        {"token": "x" * (256 * 1024 + 1)},
    ],
)
def test_invalid_secret_payloads_are_rejected_without_persistence(tmp_path, values):
    vault = _vault(tmp_path)
    with pytest.raises(ValueError):
        _create(vault, secret_values=values)
    assert vault.list_metadata("tenant-a", "kb-a") == []
    vault.close()


def test_scope_can_be_erased_without_vault_keys(tmp_path):
    database = tmp_path / "state.db"
    vault = _vault(tmp_path)
    _create(vault)
    _create(
        vault,
        kb_id="kb-b",
        connection_id="conn-b",
        secret_values={"token": "keep"},
    )
    vault.close()

    connection = sqlite3.connect(database)
    connection.execute(
        "CREATE TABLE connector_oauth_sessions "
        "(session_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, kb_id TEXT NOT NULL)"
    )
    connection.executemany(
        "INSERT INTO connector_oauth_sessions(session_id,tenant_id,kb_id) "
        "VALUES(?,?,?)",
        (
            ("session-a", "tenant-a", "kb-a"),
            ("session-b", "tenant-a", "kb-b"),
        ),
    )
    connection.commit()
    connection.close()

    removed = delete_sqlite_connector_secret_scope(
        str(database),
        "tenant-a",
        "kb-a",
    )

    assert removed["connector_credentials"] == 1
    connection = sqlite3.connect(database)
    assert connection.execute(
        "SELECT kb_id FROM connector_credentials ORDER BY kb_id"
    ).fetchall() == [("kb-b",)]
    assert connection.execute(
        "SELECT kb_id FROM connector_oauth_sessions ORDER BY kb_id"
    ).fetchall() == [("kb-b",)]
    connection.close()
