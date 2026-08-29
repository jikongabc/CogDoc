import json
from types import SimpleNamespace

import pytest

from cogdoc.api.session_store import SessionStore
from cogdoc.api.webhooks import WebhookDispatcher


class _Transport:
    def __init__(self):
        self.calls = []

    def request(self, method, url, *, headers, body):
        self.calls.append((method, url, headers, body))


def test_webhook_rejects_plain_http_and_embedded_credentials():
    with pytest.raises(ValueError, match="HTTPS"):
        WebhookDispatcher(url="http://hooks.example.com/events")
    with pytest.raises(ValueError, match="HTTPS"):
        WebhookDispatcher(url="https://user:pass@hooks.example.com/events")


def test_webhook_uses_bounded_transport_for_json_post():
    transport = _Transport()
    dispatcher = WebhookDispatcher(
        url="https://hooks.example.com/events",
        secret="shared-secret",
        transport=transport,
    )

    assert dispatcher.emit("knowledge.created", {"knowledge_id": "k1"}) is True
    method, url, headers, body = transport.calls[0]
    assert method == "POST"
    assert url == "https://hooks.example.com/events"
    assert headers["Content-Type"] == "application/json"
    assert headers["X-CogDoc-Webhook-Secret"] == "shared-secret"
    assert json.loads(body)["payload"] == {"knowledge_id": "k1"}


def test_webhook_transport_uses_configured_redirect_and_response_limits(monkeypatch):
    import cogdoc.api.webhooks as webhook_module

    captured = {}

    class Transport:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def request(self, method, url, *, headers, body):
            return None

    monkeypatch.setattr(webhook_module, "HttpTransport", Transport)
    monkeypatch.setattr(
        webhook_module,
        "get_settings",
        lambda: SimpleNamespace(
            cogdoc_webhook_url="https://hooks.example.com/events",
            cogdoc_webhook_secret="",
            cogdoc_webhook_timeout_seconds=3.0,
            cogdoc_webhook_allow_private_hosts=False,
            cogdoc_webhook_max_redirects=2,
            cogdoc_webhook_max_response_bytes=1024 * 1024,
        ),
    )

    WebhookDispatcher()

    assert captured["allowed_hosts"] == {"hooks.example.com"}
    assert captured["max_redirects"] == 2
    assert captured["max_response_bytes"] == 1024 * 1024
    assert captured["allow_private_hosts"] is False


def test_invalid_webhook_configuration_does_not_prevent_app_creation(monkeypatch):
    import cogdoc.api.app as app_module
    import cogdoc.api.webhooks as webhook_module

    monkeypatch.setattr(app_module, "configure_logging", lambda: None)
    monkeypatch.setattr(
        webhook_module,
        "get_settings",
        lambda: SimpleNamespace(
            cogdoc_webhook_url="http://invalid.example.com/events",
            cogdoc_webhook_secret="",
            cogdoc_webhook_timeout_seconds=3.0,
        ),
    )

    app = app_module.create_app(session_store=SessionStore())

    assert app.state.webhook_dispatcher.enabled is False
