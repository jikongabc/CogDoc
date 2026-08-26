from types import SimpleNamespace

import pytest

from cogdoc.tools.embedder import (
    CloudEmbedder,
    embedding_contract,
    public_embedding_model_name,
    public_embedding_profiles,
    resolve_embedder,
)


def _settings(**overrides):
    values = {
        "cloud_embedding_api_key": "server-secret",
        "cloud_embedding_base_url": "https://embedding.example/v1",
        "cloud_embedding_model": "enterprise-embed",
        "cloud_embedding_dimensions": 3,
        "cloud_embedding_batch_size": 2,
        "cloud_embedding_timeout_seconds": 5.0,
        "cloud_embedding_max_retries": 0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_cloud_embedder_batches_orders_and_normalizes(monkeypatch):
    monkeypatch.setattr(CloudEmbedder, "_settings", classmethod(lambda cls: _settings()))
    requests = []

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "data": [
                    {"index": 1, "embedding": [0.0, 3.0, 4.0]},
                    {"index": 0, "embedding": [2.0, 0.0, 0.0]},
                ]
            }

    class Client:
        def __init__(self, **kwargs):
            requests.append(("client", kwargs))

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def post(self, url, *, json, headers):
            requests.append((url, json, headers))
            return Response()

    monkeypatch.setattr("cogdoc.tools.embedder.httpx.Client", Client)
    vectors = CloudEmbedder.embed_documents(["first", "second"])

    assert vectors == [[1.0, 0.0, 0.0], [0.0, 0.6, 0.8]]
    assert requests[1][1]["dimensions"] == 3
    assert requests[1][2]["Authorization"] == "Bearer server-secret"
    assert resolve_embedder("cloud") is CloudEmbedder
    assert "enterprise-embed" in embedding_contract(CloudEmbedder)


def test_cloud_embedder_is_unavailable_without_server_key(monkeypatch):
    monkeypatch.setattr(
        CloudEmbedder,
        "_settings",
        classmethod(lambda cls: _settings(cloud_embedding_api_key="")),
    )

    assert CloudEmbedder.is_configured() is False


def test_unconfigured_cloud_profile_has_no_empty_model_label(monkeypatch):
    monkeypatch.setattr(
        CloudEmbedder,
        "_settings",
        classmethod(
            lambda cls: _settings(
                cloud_embedding_api_key="",
                cloud_embedding_model="",
            )
        ),
    )

    cloud = public_embedding_profiles()[1]

    assert cloud["model"] is None
    assert cloud["label"] == "云端 Embedding"
    assert cloud["available"] is False


def test_persisted_cloud_contract_keeps_public_model_after_config_removal(monkeypatch):
    monkeypatch.setattr(
        CloudEmbedder,
        "_settings",
        classmethod(
            lambda cls: _settings(
                cloud_embedding_api_key="",
                cloud_embedding_model="",
            )
        ),
    )

    assert (
        public_embedding_model_name(
            "openai-compatible:archived-embed@0123456789abcdef|dim=3|norm=True"
        )
        == "archived-embed"
    )
    with pytest.raises(RuntimeError, match="尚未在服务端配置"):
        resolve_embedder(
            "openai-compatible:archived-embed@0123456789abcdef|dim=3|norm=True"
        )
