from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from cogdoc.connectors.connection_store import ConnectionLimitError, ConnectionStore
from cogdoc.connectors.factory import build_connector


def _create_local(store, tmp_path, *, tenant_id, kb_id, name):
    root = tmp_path / f"root-{tenant_id}-{kb_id}-{name}"
    root.mkdir()
    return store.create(
        tenant_id=tenant_id,
        kb_id=kb_id,
        connector_type="local-directory",
        name=name,
        config={"root": str(root)},
        secret_env={},
        owner_id="owner",
    )


def test_connection_cardinality_limits_bound_control_plane_lists(tmp_path):
    store = ConnectionStore(
        str(tmp_path / "limits.db"),
        max_connections_global=3,
        max_connections_per_tenant=2,
        max_connections_per_kb=1,
    )
    _create_local(store, tmp_path, tenant_id="tenant-a", kb_id="kb-1", name="a")
    with pytest.raises(ConnectionLimitError, match="knowledge-base"):
        _create_local(
            store, tmp_path, tenant_id="tenant-a", kb_id="kb-1", name="overflow"
        )
    _create_local(store, tmp_path, tenant_id="tenant-a", kb_id="kb-2", name="b")
    with pytest.raises(ConnectionLimitError, match="tenant"):
        _create_local(
            store, tmp_path, tenant_id="tenant-a", kb_id="kb-3", name="overflow"
        )
    _create_local(store, tmp_path, tenant_id="tenant-b", kb_id="kb-1", name="c")
    with pytest.raises(ConnectionLimitError, match="global"):
        _create_local(
            store, tmp_path, tenant_id="tenant-c", kb_id="kb-1", name="overflow"
        )
    assert len(store.list_entries("tenant-a", "kb-1")) == 1
    store.close()


def test_connection_limit_admission_is_atomic_across_store_instances(tmp_path):
    database = str(tmp_path / "concurrent-limits.db")
    stores = [
        ConnectionStore(
            database,
            max_connections_global=2,
            max_connections_per_tenant=2,
            max_connections_per_kb=1,
        )
        for _ in range(2)
    ]
    barrier = Barrier(2)

    def create(index):
        barrier.wait()
        try:
            return _create_local(
                stores[index],
                tmp_path,
                tenant_id="tenant",
                kb_id="kb",
                name=str(index),
            )
        except ConnectionLimitError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(create, range(2)))

    assert sum(isinstance(result, dict) for result in results) == 1
    assert sum(isinstance(result, ConnectionLimitError) for result in results) == 1
    assert len(stores[0].list_entries("tenant", "kb")) == 1
    for store in stores:
        store.close()


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


@pytest.mark.parametrize(
    ("connector_type", "config"),
    [
        ("local-directory", None),
        ("git", None),
        ("url", {"urls": ["https://example.test/docs"]}),
    ],
)
def test_secretless_connectors_reject_credentials_without_resolving_them(
    tmp_path, connector_type, config
):
    root = tmp_path / "source"
    root.mkdir()
    effective_config = config or {
        "root" if connector_type == "local-directory" else "repository": str(root)
    }
    store = ConnectionStore(str(tmp_path / f"{connector_type}.db"))
    with pytest.raises(ValueError, match="does not accept credentials"):
        store.create(
            tenant_id="tenant",
            kb_id="kb",
            connector_type=connector_type,
            name="secretless",
            config=effective_config,
            secret_env={},
            credential_id="credential-one",
            credential_fields=("token",),
            owner_id="owner",
        )

    resolver_calls = []
    with pytest.raises(ValueError, match="does not accept credentials"):
        build_connector(
            {
                "tenant_id": "tenant",
                "kb_id": "kb",
                "connection_id": "connection",
                "connector_type": connector_type,
                "config": effective_config,
                "credential_id": "credential-one",
                "secret_env": {},
            },
            secret_resolver=lambda *scope: (
                resolver_calls.append(scope) or {"token": "x"}
            ),
        )
    assert resolver_calls == []
    store.close()


def test_connection_store_rejects_unused_extra_credential_fields(tmp_path):
    store = ConnectionStore(str(tmp_path / "state.db"))
    with pytest.raises(ValueError, match="unsupported fields: api_key"):
        store.create(
            tenant_id="tenant",
            kb_id="kb",
            connector_type="notion",
            name="overprivileged",
            config={},
            secret_env={},
            credential_id="credential-one",
            credential_fields=("token", "api_key"),
            owner_id="owner",
        )
    store.close()


@pytest.mark.parametrize(
    ("connector_type", "config"),
    [
        (
            "url",
            {"urls": ["https://example.test/doc?access_token=plaintext-marker"]},
        ),
        (
            "url",
            {"urls": ["https://user:password@example.test/doc"]},
        ),
        (
            "confluence",
            {"base_url": "https://example.test/wiki#plaintext-marker"},
        ),
        (
            "s3",
            {
                "bucket": "docs",
                "region": "us-east-1",
                "endpoint": "https://example.test?signature=plaintext-marker",
            },
        ),
    ],
)
def test_connection_store_rejects_secret_bearing_external_urls(
    tmp_path, connector_type, config
):
    store = ConnectionStore(str(tmp_path / f"{connector_type}.db"))
    secret_env = {
        "confluence": {"token": "CONFLUENCE_TOKEN"},
        "s3": {
            "access_key": "AWS_ACCESS_KEY_ID",
            "secret_key": "AWS_SECRET_ACCESS_KEY",
        },
    }.get(connector_type, {})
    with pytest.raises(ValueError, match="without userinfo, query, or fragment"):
        store.create(
            tenant_id="tenant",
            kb_id="kb",
            connector_type=connector_type,
            name="unsafe-url",
            config=config,
            secret_env=secret_env,
            owner_id="owner",
        )
    assert b"plaintext-marker" not in (tmp_path / f"{connector_type}.db").read_bytes()
    store.close()


@pytest.mark.parametrize(
    ("connector_type", "config", "credential"),
    [
        (
            "confluence",
            {"base_url": "https://attacker.example"},
            {"token": "cross-tenant-marker"},
        ),
        (
            "s3",
            {
                "bucket": "docs",
                "region": "us-east-1",
                "endpoint": "https://attacker.example",
            },
            {"access_key": "marker", "secret_key": "cross-tenant-marker"},
        ),
    ],
)
def test_credential_endpoint_policy_rejects_before_secret_resolution(
    connector_type, config, credential
):
    resolver_calls = []
    with pytest.raises(ValueError, match="endpoint host is not allowed"):
        build_connector(
            {
                "tenant_id": "tenant",
                "kb_id": "kb",
                "connection_id": "connection",
                "connector_type": connector_type,
                "config": config,
                "credential_id": "credential-one",
                "secret_env": {},
            },
            secret_resolver=lambda *scope: resolver_calls.append(scope) or credential,
        )
    assert resolver_calls == []


def test_authenticated_connector_build_disables_process_environment_secrets(
    monkeypatch,
):
    monkeypatch.setenv("COGDOC_REVIEW_SECRET", "cross-tenant-marker")
    with pytest.raises(ValueError, match="secret_env connections are disabled"):
        build_connector(
            {
                "connector_type": "notion",
                "config": {},
                "credential_id": None,
                "secret_env": {"token": "COGDOC_REVIEW_SECRET"},
            },
            allow_environment_secrets=False,
        )


@pytest.mark.parametrize(
    ("connector_type", "field"),
    [("local-directory", "root"), ("git", "repository")],
)
def test_authenticated_local_connectors_are_confined_to_server_roots(
    tmp_path, connector_type, field
):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "other-tenant"
    outside.mkdir()
    connection = {
        "connector_type": connector_type,
        "config": {field: str(outside)},
        "credential_id": None,
        "secret_env": {},
    }
    with pytest.raises(ValueError, match="outside the server-owned allowlist"):
        build_connector(
            connection,
            enforce_local_access_policy=True,
            local_allowed_roots=(str(allowed),),
            git_allowed_roots=(str(allowed),),
        )

    connection["config"][field] = str(allowed)
    built = build_connector(
        connection,
        enforce_local_access_policy=True,
        local_allowed_roots=(str(allowed),),
        git_allowed_roots=(str(allowed),),
    )
    assert built is not None


def test_authenticated_local_connector_cannot_follow_symlinks(tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    with pytest.raises(ValueError, match="follow_symlinks is disabled"):
        build_connector(
            {
                "connector_type": "local-directory",
                "config": {"root": str(allowed), "follow_symlinks": True},
                "credential_id": None,
                "secret_env": {},
            },
            enforce_local_access_policy=True,
            local_allowed_roots=(str(allowed),),
        )
