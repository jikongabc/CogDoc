from __future__ import annotations

import logging
import json
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from cogdoc.api.schemas import DerivedKnowledge
from cogdoc.api.time_utils import now_iso
from cogdoc.config.settings import get_settings
from cogdoc.connectors.http_transport import HttpTransport
from cogdoc.observability.logger import log_event


def validate_webhook_url(url: str) -> str:
    """Validate one credential-free HTTPS origin and return its host."""

    parts = urlsplit(url)
    try:
        port = parts.port
    except ValueError as exc:
        raise ValueError("webhook URL is invalid") from exc
    host = str(parts.hostname or "").casefold()
    if (
        parts.scheme != "https"
        or not host
        or port not in {None, 443}
        or parts.username is not None
        or parts.password is not None
    ):
        raise ValueError("webhook URL must be a credential-free HTTPS URL")
    return host


# 发送外部回调。
class WebhookDispatcher:
    def __init__(
        self,
        *,
        url: str | None = None,
        secret: str | None = None,
        timeout_seconds: float | None = None,
        allow_private_hosts: bool | None = None,
        transport: Any | None = None,
    ):
        settings = get_settings()
        self._url = (url if url is not None else settings.cogdoc_webhook_url).strip()
        self._secret = (
            secret if secret is not None else settings.cogdoc_webhook_secret
        ).strip()
        self._timeout_seconds = (
            timeout_seconds
            if timeout_seconds is not None
            else settings.cogdoc_webhook_timeout_seconds
        )
        self._transport = transport
        if self._url:
            host = validate_webhook_url(self._url)
            if self._transport is None:
                self._transport = HttpTransport(
                    allowed_hosts={host},
                    timeout_seconds=self._timeout_seconds,
                    max_response_bytes=settings.cogdoc_webhook_max_response_bytes,
                    max_redirects=settings.cogdoc_webhook_max_redirects,
                    allow_private_hosts=(
                        settings.cogdoc_webhook_allow_private_hosts
                        if allow_private_hosts is None
                        else allow_private_hosts
                    ),
                )

    @property
    def enabled(self) -> bool:
        return bool(self._url)

    # 发送事件，失败只记日志。
    def emit(self, event: str, payload: dict[str, Any]) -> bool:
        if not self.enabled:
            return False
        body = {
            "schema_version": "v1",
            "event_id": uuid4().hex,
            "event": event,
            "occurred_at": now_iso(),
            "payload": payload,
        }
        headers = {"Content-Type": "application/json"}
        if self._secret:
            headers["X-CogDoc-Webhook-Secret"] = self._secret
        try:
            assert self._transport is not None
            self._transport.request(
                "POST",
                self._url,
                headers=headers,
                body=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            )
        except Exception as exc:
            log_event(
                "webhook",
                "webhook_emit_failed",
                {},
                level=logging.WARNING,
                event_type=event,
                error_class=type(exc).__name__,
            )
            return False
        log_event("webhook", "webhook_emit_succeeded", {}, event_type=event)
        return True


# 异步提交待审核知识通知。
def notify_pending_created(app, row: dict[str, Any], source: str) -> None:
    if row.get("status") != "pending":
        return
    dispatcher = getattr(app.state, "webhook_dispatcher", None)
    if dispatcher is None or not getattr(dispatcher, "enabled", False):
        return
    public_row = {
        key: value for key, value in row.items() if key in DerivedKnowledge.model_fields
    }
    payload = {
        "source": source,
        "knowledge": DerivedKnowledge.model_validate(public_row).model_dump(
            mode="json"
        ),
    }
    executor = getattr(app.state, "offload_executor", None)
    if executor is None:
        return
    try:
        executor.submit(dispatcher.emit, "knowledge.pending_created", payload)
    except RuntimeError as exc:
        level = logging.DEBUG if "shutdown" in str(exc).lower() else logging.WARNING
        log_event(
            "webhook",
            "webhook_submit_failed",
            {},
            level=level,
            event_type="knowledge.pending_created",
            error_class=type(exc).__name__,
        )
    except Exception as exc:
        log_event(
            "webhook",
            "webhook_submit_failed",
            {},
            level=logging.WARNING,
            event_type="knowledge.pending_created",
            error_class=type(exc).__name__,
        )
