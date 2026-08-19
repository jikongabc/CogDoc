from __future__ import annotations

import ipaddress
import json
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable, Mapping
from urllib.parse import urlsplit

from cogdoc.connectors.base import ConnectorError, RetryableConnectorError


MAX_HTTP_RESPONSE_BYTES = 100 * 1024 * 1024


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes
    url: str

    def json(self) -> dict:
        try:
            value = json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ConnectorError("provider returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise ConnectorError("provider JSON response must be an object")
        return value


def _public_host(host: str, resolver: Callable = socket.getaddrinfo) -> bool:
    try:
        addresses = resolver(host, None, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise RetryableConnectorError("provider host resolution failed") from exc
    if not addresses:
        return False
    for row in addresses:
        address = ipaddress.ip_address(row[4][0])
        if not address.is_global:
            return False
    return True


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class HttpTransport:
    """Bounded HTTP transport with explicit host and redirect policy."""

    def __init__(
        self,
        *,
        allowed_hosts: set[str],
        timeout_seconds: float = 30.0,
        max_response_bytes: int = MAX_HTTP_RESPONSE_BYTES,
        allow_private_hosts: bool = False,
        opener=None,
        resolver: Callable = socket.getaddrinfo,
    ) -> None:
        hosts = {
            str(host).strip().casefold() for host in allowed_hosts if str(host).strip()
        }
        if not hosts:
            raise ValueError("allowed_hosts must not be empty")
        if timeout_seconds <= 0 or max_response_bytes <= 0:
            raise ValueError("HTTP bounds must be positive")
        self.allowed_hosts = hosts
        self.timeout_seconds = timeout_seconds
        self.max_response_bytes = max_response_bytes
        self.allow_private_hosts = allow_private_hosts
        self._resolver = resolver
        self._opener = opener or urllib.request.build_opener(_NoRedirect())

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: bytes | None = None,
    ) -> HttpResponse:
        parts = urlsplit(url)
        host = str(parts.hostname or "").casefold()
        if (
            parts.scheme != "https"
            or host not in self.allowed_hosts
            or parts.username
            or parts.password
        ):
            raise ConnectorError("provider URL is outside the configured HTTPS origin")
        if not self.allow_private_hosts and not _public_host(host, self._resolver):
            raise ConnectorError("provider URL resolves to a non-public address")
        request = urllib.request.Request(
            url,
            data=body,
            headers={str(key): str(value) for key, value in (headers or {}).items()},
            method=method.upper(),
        )
        try:
            response = self._opener.open(request, timeout=self.timeout_seconds)
            with response:
                declared_length = response.headers.get("Content-Length")
                if declared_length is not None:
                    try:
                        if int(declared_length) > self.max_response_bytes:
                            raise ConnectorError(
                                "provider response exceeds the byte limit"
                            )
                    except ValueError as exc:
                        raise ConnectorError(
                            "provider content length is invalid"
                        ) from exc
                raw = response.read(self.max_response_bytes + 1)
                status = int(getattr(response, "status", 200))
                response_headers = {
                    str(k).casefold(): str(v) for k, v in response.headers.items()
                }
                final_url = str(response.geturl())
        except urllib.error.HTTPError as exc:
            if exc.code in {408, 425, 429, 500, 502, 503, 504}:
                raise RetryableConnectorError(f"provider HTTP {exc.code}") from exc
            raise ConnectorError(f"provider HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise RetryableConnectorError("provider request failed") from exc
        final = urlsplit(final_url)
        if (
            final.scheme != "https"
            or str(final.hostname or "").casefold() not in self.allowed_hosts
            or final.username
            or final.password
        ):
            raise ConnectorError("provider redirected outside the configured origin")
        if len(raw) > self.max_response_bytes:
            raise ConnectorError("provider response exceeds the byte limit")
        if status >= 400:
            raise ConnectorError(f"provider HTTP {status}")
        return HttpResponse(status, response_headers, raw, final_url)
