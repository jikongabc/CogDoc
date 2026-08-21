import json

import pytest

from cogdoc.api.scim import (
    SCIMConfigurationError,
    SCIMProtocolError,
    parse_filter,
    parse_if_match,
    parse_scim_access_registry,
)
from cogdoc.api.tenancy import fingerprint_api_key


def test_scim_access_registry_hashes_tokens_and_rejects_owner_mapping():
    token = "t" * 32
    registry = parse_scim_access_registry(
        json.dumps(
            [{"token": token, "workspace_id": "wsp_enterprise", "label": "Entra"}]
        ),
        issuer="https://id.example.com",
        default_role="viewer",
        group_role_map='{"CogDoc Admins":"admin"}',
    )
    access = registry[fingerprint_api_key(token)]
    assert access.workspace_id == "wsp_enterprise"
    assert access.mapped_role("cogdoc admins") == "admin"
    assert token not in repr(registry)

    with pytest.raises(SCIMConfigurationError, match="owner"):
        parse_scim_access_registry(
            json.dumps([{"token": token, "workspace_id": "wsp_enterprise"}]),
            issuer="https://id.example.com",
            group_role_map='{"Owners":"owner"}',
        )


def test_scim_filter_and_etag_parsing_are_bounded():
    assert parse_filter('userName eq "Alice@example.com"', resource="User") == (
        "userName",
        "Alice@example.com",
    )
    assert parse_if_match('W/"12"') == 12
    assert parse_if_match("*") is None
    with pytest.raises(SCIMProtocolError) as unsupported:
        parse_filter('name co "alice"', resource="User")
    assert unsupported.value.scim_type == "invalidFilter"
    with pytest.raises(SCIMProtocolError) as stale:
        parse_if_match('"12"')
    assert stale.value.status == 412
