import pytest

from cogdoc.connectors.connection_store import ConnectionStore
from cogdoc.connectors.factory import build_connector


def test_connection_store_never_returns_or_persists_secret_values(
    tmp_path, monkeypatch
):
    db = tmp_path / "state.db"
    store = ConnectionStore(str(db))
    monkeypatch.setenv("COGDOC_TEST_NOTION_TOKEN", "super-secret-token")
    row = store.create(
        tenant_id="tenant",
        kb_id="kb",
        connector_type="notion",
        name="Product notes",
        config={"schedule_seconds": 300},
        secret_env={"token": "COGDOC_TEST_NOTION_TOKEN"},
        owner_id="owner",
    )

    assert row["secret_fields"] == ["token"]
    assert "secret_env" not in row
    assert b"super-secret-token" not in db.read_bytes()
    private = store.get(row["connection_id"], include_secret_refs=True)
    assert private["secret_env"] == {"token": "COGDOC_TEST_NOTION_TOKEN"}
    connector = build_connector(private)
    assert connector.headers["Authorization"] == "Bearer super-secret-token"
    store.close()


def test_connection_store_rejects_plaintext_secret_and_missing_env_reference(tmp_path):
    store = ConnectionStore(str(tmp_path / "state.db"))
    with pytest.raises(ValueError, match="secret values"):
        store.create(
            tenant_id="tenant",
            kb_id="kb",
            connector_type="notion",
            name="unsafe",
            config={"token": "plaintext"},
            secret_env={},
            owner_id="owner",
        )
    with pytest.raises(ValueError, match="missing"):
        store.create(
            tenant_id="tenant",
            kb_id="kb",
            connector_type="s3",
            name="bucket",
            config={"bucket": "docs", "region": "us-east-1"},
            secret_env={"access_key": "AWS_ACCESS_KEY_ID"},
            owner_id="owner",
        )
    store.close()


def test_missing_secret_environment_fails_without_leaking_reference(
    tmp_path, monkeypatch
):
    store = ConnectionStore(str(tmp_path / "state.db"))
    row = store.create(
        tenant_id="tenant",
        kb_id="kb",
        connector_type="notion",
        name="notes",
        config={},
        secret_env={"token": "MISSING_NOTION_TOKEN"},
        owner_id="owner",
    )
    monkeypatch.delenv("MISSING_NOTION_TOKEN", raising=False)
    with pytest.raises(ValueError, match="token"):
        build_connector(store.get(row["connection_id"], include_secret_refs=True))
    assert "MISSING_NOTION_TOKEN" not in str(store.get(row["connection_id"]))
    store.close()
