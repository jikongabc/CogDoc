import base64
import hashlib
import json
from urllib.parse import parse_qs, urlsplit

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
import pytest

from cogdoc.api.oidc import (
    OIDCClient,
    OIDCConfigurationError,
    OIDCFlowError,
    OIDCFlowStore,
    OIDCProtocolError,
    OIDCProviderConfig,
)


class Clock:
    def __init__(self, value=1_900_000_000.0):
        self.value = value

    def __call__(self):
        return self.value


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _integer(value: int) -> str:
    return _b64(value.to_bytes((value.bit_length() + 7) // 8, "big"))


class FakeTransport:
    def __init__(self, issuer, private_key, clock):
        numbers = private_key.public_key().public_numbers()
        self.discovery = {
            "issuer": issuer,
            "authorization_endpoint": f"{issuer}/authorize",
            "token_endpoint": f"{issuer}/token",
            "jwks_uri": f"{issuer}/jwks",
            "id_token_signing_alg_values_supported": ["RS256"],
        }
        self.jwks = {
            "keys": [
                {
                    "kid": "key-1",
                    "kty": "RSA",
                    "use": "sig",
                    "alg": "RS256",
                    "n": _integer(numbers.n),
                    "e": _integer(numbers.e),
                }
            ]
        }
        self.private_key = private_key
        self.clock = clock
        self.nonce = ""
        self.posts = []
        self.extra_claims = {}

    def get_json(self, url):
        if url.endswith("openid-configuration"):
            return self.discovery
        if url.endswith("/jwks"):
            return self.jwks
        raise AssertionError(url)

    def _token(self):
        header = _b64(json.dumps({"alg": "RS256", "kid": "key-1"}).encode())
        claims_payload = {
            "iss": self.discovery["issuer"],
            "sub": "subject-1",
            "aud": "client-1",
            "exp": self.clock() + 300,
            "iat": self.clock(),
            "nonce": self.nonce,
            "email": "alice@example.com",
            "email_verified": True,
            "name": "Alice",
            "groups": ["CogDoc Editors", "CogDoc Admins"],
            **self.extra_claims,
        }
        claims = _b64(json.dumps(claims_payload).encode())
        signing_input = f"{header}.{claims}".encode()
        signature = self.private_key.sign(
            signing_input, padding.PKCS1v15(), hashes.SHA256()
        )
        return f"{header}.{claims}.{_b64(signature)}"

    def post_form(self, url, data):
        self.posts.append((url, dict(data)))
        return {"id_token": self._token()}


def _config():
    return OIDCProviderConfig(
        issuer="https://id.example.com",
        client_id="client-1",
        client_secret="secret",
        redirect_uri="https://api.example.com/v1/auth/oidc/callback",
        allowed_endpoint_hosts=("id.example.com",),
        allowed_return_urls=("https://app.example.com/login",),
    )


def test_oidc_config_rejects_http_and_unlisted_return_url():
    with pytest.raises(OIDCConfigurationError):
        OIDCProviderConfig(
            issuer="http://id.example.com",
            client_id="client",
            redirect_uri="https://api.example.com/callback",
            allowed_return_urls=("https://app.example.com/login",),
        ).validated()

    config = _config().validated()
    with pytest.raises(OIDCConfigurationError, match="allowlisted"):
        config.validate_return_url("https://evil.example/login")

    with pytest.raises(OIDCConfigurationError, match="default HTTPS port"):
        OIDCProviderConfig(
            issuer="https://id.example.com:8443",
            client_id="client",
            redirect_uri="https://api.example.com/callback",
            allowed_return_urls=("https://app.example.com/login",),
        ).validated()


def test_flow_store_state_and_handoff_are_durable_one_shot(tmp_path):
    clock = Clock()
    key = bytes(range(32))
    path = str(tmp_path / "state.db")
    store = OIDCFlowStore(path, key, clock=clock)
    flow = store.create(
        intent="login",
        return_url="https://app.example.com/login",
        workspace_id="wsp_1",
    )
    assert flow.code_verifier not in "\n".join(store._conn.iterdump())
    store.close()

    reopened = OIDCFlowStore(path, key, clock=clock)
    consumed = reopened.consume_state(flow.state)
    assert consumed.code_verifier == flow.code_verifier
    with pytest.raises(OIDCFlowError, match="already used"):
        reopened.consume_state(flow.state)

    code = reopened.store_result(
        flow.flow_id, {"kind": "login", "session": {"access_token": "secret"}}
    )
    assert code not in "\n".join(reopened._conn.iterdump())
    assert reopened.exchange_result(code)["session"]["access_token"] == "secret"
    with pytest.raises(OIDCFlowError, match="already used"):
        reopened.exchange_result(code)
    reopened.close()


def test_flow_expiry_is_fail_closed_and_purge_is_bounded(tmp_path):
    clock = Clock()
    store = OIDCFlowStore(
        str(tmp_path / "state.db"),
        bytes(range(32)),
        clock=clock,
        flow_ttl_seconds=30,
    )
    flow = store.create(intent="login", return_url="https://app.example.com/login")
    clock.value += 31
    with pytest.raises(OIDCFlowError, match="expired"):
        store.consume_state(flow.state)
    assert store.purge_expired(limit=1) == 1
    store.close()


def test_oidc_client_uses_pkce_and_verifies_rs256_claims(tmp_path):
    clock = Clock()
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    transport = FakeTransport("https://id.example.com", private_key, clock)
    client = OIDCClient(_config(), transport=transport, clock=clock)
    store = OIDCFlowStore(str(tmp_path / "state.db"), bytes(range(32)), clock=clock)
    flow = store.create(intent="login", return_url="https://app.example.com/login")
    transport.nonce = flow.nonce

    authorization_url = client.authorization_url(flow)
    query = parse_qs(urlsplit(authorization_url).query)
    assert query["state"] == [flow.state]
    assert query["nonce"] == [flow.nonce]
    assert query["code_challenge_method"] == ["S256"]
    assert query["code_challenge"] == [
        _b64(hashlib.sha256(flow.code_verifier.encode()).digest())
    ]

    claims = client.exchange_code(
        "provider-code", code_verifier=flow.code_verifier, nonce=flow.nonce
    )
    assert claims.subject == "subject-1"
    assert claims.email == "alice@example.com"
    assert claims.string_list_claims["groups"] == (
        "CogDoc Editors",
        "CogDoc Admins",
    )
    assert transport.posts[0][1]["client_secret"] == "secret"


def test_oidc_client_rejects_unbounded_string_list_claim(tmp_path):
    clock = Clock()
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    transport = FakeTransport("https://id.example.com", private_key, clock)
    transport.extra_claims = {"groups": [f"group-{index}" for index in range(201)]}
    client = OIDCClient(_config(), transport=transport, clock=clock)
    store = OIDCFlowStore(str(tmp_path / "state.db"), bytes(range(32)), clock=clock)
    flow = store.create(intent="login", return_url="https://app.example.com/login")
    transport.nonce = flow.nonce

    with pytest.raises(OIDCProtocolError, match="exceeds its limit"):
        client.exchange_code(
            "provider-code", code_verifier=flow.code_verifier, nonce=flow.nonce
        )


def test_oidc_client_refetches_jwks_when_key_material_rotates_under_same_kid(
    tmp_path,
):
    clock = Clock()
    first_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    transport = FakeTransport("https://id.example.com", first_key, clock)
    client = OIDCClient(_config(), transport=transport, clock=clock)
    store = OIDCFlowStore(str(tmp_path / "state.db"), bytes(range(32)), clock=clock)
    flow = store.create(intent="login", return_url="https://app.example.com/login")
    transport.nonce = flow.nonce
    client.exchange_code(
        "first-code", code_verifier=flow.code_verifier, nonce=flow.nonce
    )

    second_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    numbers = second_key.public_key().public_numbers()
    transport.private_key = second_key
    transport.jwks["keys"][0].update(
        {"n": _integer(numbers.n), "e": _integer(numbers.e)}
    )

    claims = client.exchange_code(
        "second-code", code_verifier=flow.code_verifier, nonce=flow.nonce
    )
    assert claims.subject == "subject-1"


def test_oidc_client_rejects_nonce_and_discovery_issuer_mismatch(tmp_path):
    clock = Clock()
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    transport = FakeTransport("https://id.example.com", private_key, clock)
    client = OIDCClient(_config(), transport=transport, clock=clock)
    store = OIDCFlowStore(str(tmp_path / "state.db"), bytes(range(32)), clock=clock)
    flow = store.create(intent="login", return_url="https://app.example.com/login")
    transport.nonce = "wrong"
    with pytest.raises(OIDCProtocolError, match="nonce"):
        client.exchange_code(
            "provider-code", code_verifier=flow.code_verifier, nonce=flow.nonce
        )

    transport.discovery["issuer"] = "https://other.example.com"
    other = OIDCClient(_config(), transport=transport, clock=clock)
    with pytest.raises(OIDCProtocolError, match="issuer"):
        other.discovery()
