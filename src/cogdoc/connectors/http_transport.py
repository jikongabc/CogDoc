from __future__ import annotations

import errno
import http.client
import ipaddress
import json
import math
import socket
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol
from urllib.parse import SplitResult, urljoin, urlsplit

from cogdoc.connectors.base import ConnectorError, RetryableConnectorError


MAX_HTTP_RESPONSE_BYTES = 100 * 1024 * 1024
MAX_HTTP_REDIRECTS = 10
MAX_RESOLVED_ADDRESSES = 16
_READ_CHUNK_BYTES = 64 * 1024


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


@dataclass(frozen=True)
class _ResolvedAddress:
    family: int
    ip: str
    sockaddr: tuple[Any, ...]

    def authority(self, port: int) -> str:
        host = f"[{self.ip}]" if self.family == socket.AF_INET6 else self.ip
        return f"{host}:{port}"


class _PinnedOpener(Protocol):
    def open_pinned(
        self,
        request: urllib.request.Request,
        *,
        timeout: float,
        addresses: tuple[_ResolvedAddress, ...],
        server_hostname: str,
        port: int,
    ) -> Any: ...


def _resolved_addresses(
    host: str,
    port: int,
    resolver: Callable[..., Any] = socket.getaddrinfo,
) -> tuple[_ResolvedAddress, ...]:
    try:
        rows = resolver(
            host,
            port,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
        )
    except OSError as exc:
        raise RetryableConnectorError("provider host resolution failed") from exc

    addresses: list[_ResolvedAddress] = []
    seen: set[tuple[int, str, int]] = set()
    for row in rows or ():
        try:
            raw_family = row[0]
            raw_sockaddr = row[4]
            raw_ip = str(raw_sockaddr[0])
            parsed = ipaddress.ip_address(raw_ip)
        except (IndexError, TypeError, ValueError) as exc:
            raise ConnectorError(
                "provider host resolution returned an invalid address"
            ) from exc

        expected_family = socket.AF_INET6 if parsed.version == 6 else socket.AF_INET
        try:
            family = expected_family if raw_family is None else int(raw_family)
        except (TypeError, ValueError) as exc:
            raise ConnectorError(
                "provider host resolution returned an invalid address"
            ) from exc
        # Some test resolvers historically omitted the family. Real getaddrinfo
        # always returns it, and a conflicting family/address pair is unsafe.
        if family not in {socket.AF_INET, socket.AF_INET6}:
            raise ConnectorError("provider host resolution returned an invalid address")
        if family != expected_family:
            raise ConnectorError("provider host resolution returned an invalid address")

        ip = str(parsed)
        scope_id = 0
        if family == socket.AF_INET6:
            try:
                flowinfo = int(raw_sockaddr[2]) if len(raw_sockaddr) > 2 else 0
                scope_id = int(raw_sockaddr[3]) if len(raw_sockaddr) > 3 else 0
            except (TypeError, ValueError) as exc:
                raise ConnectorError(
                    "provider host resolution returned an invalid address"
                ) from exc
            sockaddr: tuple[Any, ...] = (ip, port, flowinfo, scope_id)
        else:
            sockaddr = (ip, port)

        key = (family, ip, scope_id)
        if key not in seen:
            seen.add(key)
            addresses.append(_ResolvedAddress(family, ip, sockaddr))
            if len(addresses) > MAX_RESOLVED_ADDRESSES:
                raise ConnectorError(
                    "provider host resolution returned too many addresses"
                )

    if not addresses:
        raise ConnectorError("provider URL resolves to a non-public address")
    return tuple(addresses)


def _public_host(host: str, resolver: Callable = socket.getaddrinfo) -> bool:
    addresses = _resolved_addresses(host, 443, resolver)
    return all(_is_public_address(address.ip) for address in addresses)


def _is_public_address(value: str) -> bool:
    address = ipaddress.ip_address(value)
    # ipaddress considers multicast addresses "global" because they are not
    # confined to a single host/network. They are not valid public unicast
    # destinations for provider HTTP traffic.
    return address.is_global and not address.is_multicast


def _remaining_seconds(
    deadline: float,
    monotonic: Callable[[], float] = time.monotonic,
) -> float:
    remaining = deadline - monotonic()
    if not math.isfinite(remaining) or remaining <= 0:
        raise TimeoutError("provider request deadline exceeded")
    return remaining


def _connect_verified(
    addresses: tuple[_ResolvedAddress, ...],
    timeout: Any,
    source_address: tuple[str, int] | None,
    *,
    deadline: float | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> socket.socket:
    """Connect without asking the resolver to interpret the target again."""

    last_error: OSError | None = None
    for address in addresses:
        sock: socket.socket | None = None
        try:
            candidate_timeout = timeout
            if deadline is not None:
                remaining = _remaining_seconds(deadline, monotonic)
                candidate_timeout = (
                    remaining
                    if timeout is None
                    else (
                        min(float(timeout), remaining)
                        if isinstance(timeout, (int, float))
                        else remaining
                    )
                )
            sock = socket.socket(address.family, socket.SOCK_STREAM)
            if candidate_timeout is None or isinstance(candidate_timeout, (int, float)):
                sock.settimeout(candidate_timeout)
            if source_address:
                sock.bind(source_address)
            sock.connect(address.sockaddr)
            return sock
        except OSError as exc:
            last_error = exc
            if sock is not None:
                sock.close()
    if last_error is not None:
        raise last_error
    raise OSError("provider host resolution returned no connectable addresses")


def _set_tcp_nodelay(sock: socket.socket) -> None:
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except OSError as exc:
        if exc.errno != errno.ENOPROTOOPT:
            raise


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    _context: Any
    _tunnel_headers: dict[str, str]
    _tunnel_host: str | None
    source_address: tuple[str, int] | None

    def __init__(
        self,
        host: str,
        *,
        addresses: tuple[_ResolvedAddress, ...],
        server_hostname: str,
        target_port: int,
        deadline: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        **kwargs,
    ) -> None:
        super().__init__(host, **kwargs)
        self._verified_addresses = addresses
        self._server_hostname = server_hostname
        self._target_port = target_port
        self._deadline = deadline
        self._monotonic = monotonic

    def connect(self) -> None:
        if self._deadline is not None:
            self.timeout = _remaining_seconds(self._deadline, self._monotonic)
        if self._tunnel_host:
            # urllib has selected a proxy. Connect to that proxy as usual, but
            # make the CONNECT authority the verified numeric target so the
            # proxy cannot perform a second, attacker-controlled DNS lookup.
            pinned = self._verified_addresses[0]
            tunnel_headers = dict(self._tunnel_headers)
            tunnel_headers["Host"] = pinned.authority(self._target_port)
            self.set_tunnel(
                pinned.ip,
                self._target_port,
                headers=tunnel_headers,
            )
            http.client.HTTPConnection.connect(self)
        else:
            sys.audit("http.client.connect", self, self.host, self.port)
            self.sock = _connect_verified(
                self._verified_addresses,
                self.timeout,
                self.source_address,
                deadline=self._deadline,
                monotonic=self._monotonic,
            )
            _set_tcp_nodelay(self.sock)

        try:
            if self._deadline is not None:
                set_timeout = getattr(self.sock, "settimeout", None)
                if callable(set_timeout):
                    set_timeout(_remaining_seconds(self._deadline, self._monotonic))
            self.sock = self._context.wrap_socket(
                self.sock,
                server_hostname=self._server_hostname,
            )
        except BaseException:
            if self.sock is not None:
                self.sock.close()
            raise


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(
        self,
        *,
        addresses: tuple[_ResolvedAddress, ...],
        server_hostname: str,
        target_port: int,
        deadline: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__()
        self._addresses = addresses
        self._server_hostname = server_hostname
        self._target_port = target_port
        self._deadline = deadline
        self._monotonic = monotonic

    def _connection(self, host: str, **kwargs) -> _PinnedHTTPSConnection:
        return _PinnedHTTPSConnection(
            host,
            addresses=self._addresses,
            server_hostname=self._server_hostname,
            target_port=self._target_port,
            deadline=self._deadline,
            monotonic=self._monotonic,
            **kwargs,
        )

    def https_open(self, request):
        return self.do_open(
            self._connection,
            request,
            context=self._context,
        )


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass(frozen=True)
class _Redirect:
    status: int
    location: str


class HttpTransport:
    """Bounded HTTP transport with DNS-pinned HTTPS and redirect policy."""

    def __init__(
        self,
        *,
        allowed_hosts: set[str],
        timeout_seconds: float = 30.0,
        max_response_bytes: int = MAX_HTTP_RESPONSE_BYTES,
        max_redirects: int = MAX_HTTP_REDIRECTS,
        allow_private_hosts: bool = False,
        opener=None,
        resolver: Callable = socket.getaddrinfo,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        hosts = {
            str(host).strip().casefold() for host in allowed_hosts if str(host).strip()
        }
        if not hosts:
            raise ValueError("allowed_hosts must not be empty")
        if (
            not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
            or max_response_bytes <= 0
            or type(max_redirects) is not int
            or not 0 <= max_redirects <= MAX_HTTP_REDIRECTS
        ):
            raise ValueError("invalid HTTP bounds")
        self.allowed_hosts = hosts
        self.timeout_seconds = timeout_seconds
        self.max_response_bytes = max_response_bytes
        self.max_redirects = max_redirects
        self.allow_private_hosts = allow_private_hosts
        self._resolver = resolver
        self._monotonic = monotonic
        self._custom_opener: _PinnedOpener | None = opener
        # Match urllib's normal environment-proxy behavior, while allowing a
        # fresh, request-local HTTPS handler for the immutable DNS snapshot.
        self._proxies = urllib.request.getproxies()

    def _validated_url(
        self, url: str
    ) -> tuple[SplitResult, str, int, tuple[_ResolvedAddress, ...]]:
        parts = urlsplit(url)
        host = str(parts.hostname or "").casefold()
        try:
            explicit_port = parts.port
            port = 443 if explicit_port is None else explicit_port
        except ValueError as exc:
            raise ConnectorError(
                "provider URL is outside the configured HTTPS origin"
            ) from exc
        if (
            parts.scheme != "https"
            or host not in self.allowed_hosts
            or parts.username is not None
            or parts.password is not None
        ):
            raise ConnectorError("provider URL is outside the configured HTTPS origin")

        addresses = _resolved_addresses(host, port, self._resolver)
        if not self.allow_private_hosts and any(
            not _is_public_address(address.ip) for address in addresses
        ):
            raise ConnectorError("provider URL resolves to a non-public address")
        return parts, host, port, addresses

    @staticmethod
    def _host_header(parts: SplitResult, host: str, port: int) -> str:
        encoded_host = host.encode("idna").decode("ascii")
        authority = f"[{encoded_host}]" if ":" in encoded_host else encoded_host
        if parts.port is not None:
            authority = f"{authority}:{port}"
        return authority

    def _open(
        self,
        request: urllib.request.Request,
        *,
        addresses: tuple[_ResolvedAddress, ...],
        server_hostname: str,
        port: int,
        deadline: float,
    ):
        timeout = _remaining_seconds(deadline, self._monotonic)
        if self._custom_opener is not None:
            open_pinned = getattr(self._custom_opener, "open_pinned", None)
            if not callable(open_pinned):
                raise ConnectorError(
                    "custom HTTP opener does not support DNS-pinned connections"
                )
            return open_pinned(
                request,
                timeout=timeout,
                addresses=addresses,
                server_hostname=server_hostname,
                port=port,
            )

        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler(self._proxies),
            _NoRedirect(),
            _PinnedHTTPSHandler(
                addresses=addresses,
                server_hostname=server_hostname,
                target_port=port,
                deadline=deadline,
                monotonic=self._monotonic,
            ),
        )
        return opener.open(request, timeout=timeout)

    def _perform(
        self,
        request: urllib.request.Request,
        *,
        addresses: tuple[_ResolvedAddress, ...],
        server_hostname: str,
        port: int,
        deadline: float,
    ) -> HttpResponse | _Redirect:
        try:
            response = self._open(
                request,
                addresses=addresses,
                server_hostname=server_hostname,
                port=port,
                deadline=deadline,
            )
        except urllib.error.HTTPError as exc:
            if exc.code in {301, 302, 303, 307, 308}:
                location = str(exc.headers.get("Location") or "")
                exc.close()
                if not location:
                    raise ConnectorError(f"provider HTTP {exc.code}") from exc
                return _Redirect(exc.code, location)
            if exc.code in {408, 425, 429, 500, 502, 503, 504}:
                exc.close()
                raise RetryableConnectorError(f"provider HTTP {exc.code}") from exc
            exc.close()
            raise ConnectorError(f"provider HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise RetryableConnectorError("provider request failed") from exc

        try:
            with response:
                status = int(getattr(response, "status", 200))
                if status in {301, 302, 303, 307, 308}:
                    location = str(response.headers.get("Location") or "")
                    if not location:
                        raise ConnectorError(f"provider HTTP {status}")
                    return _Redirect(status, location)

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
                chunks: list[bytes] = []
                total_bytes = 0
                while total_bytes <= self.max_response_bytes:
                    remaining = _remaining_seconds(deadline, self._monotonic)
                    response_socket = getattr(
                        getattr(getattr(response, "fp", None), "raw", None),
                        "_sock",
                        None,
                    )
                    set_timeout = getattr(response_socket, "settimeout", None)
                    if callable(set_timeout):
                        set_timeout(remaining)
                    chunk = response.read(
                        min(
                            _READ_CHUNK_BYTES,
                            self.max_response_bytes + 1 - total_bytes,
                        )
                    )
                    if not chunk:
                        break
                    chunks.append(chunk)
                    total_bytes += len(chunk)
                raw = b"".join(chunks)
                response_headers = {
                    str(k).casefold(): str(v) for k, v in response.headers.items()
                }
                final_url = str(response.geturl())
        except (TimeoutError, OSError) as exc:
            raise RetryableConnectorError("provider request failed") from exc

        # A custom opener must not silently follow redirects, because doing so
        # would skip the destination's allowlist and DNS-snapshot checks.
        if final_url != request.full_url:
            raise ConnectorError("provider performed an unvalidated redirect")
        if len(raw) > self.max_response_bytes:
            raise ConnectorError("provider response exceeds the byte limit")
        if status >= 400:
            raise ConnectorError(f"provider HTTP {status}")
        return HttpResponse(status, response_headers, raw, final_url)

    @staticmethod
    def _redirect_request(
        method: str,
        body: bytes | None,
        headers: dict[str, str],
        status: int,
    ) -> tuple[str, bytes | None, dict[str, str]]:
        filtered = {
            key: value
            for key, value in headers.items()
            if key.casefold() not in {"content-length", "content-type"}
        }
        if status in {307, 308}:
            return method, body, dict(headers)
        if method in {"GET", "HEAD"}:
            return method, None, filtered
        if method == "POST" and status in {301, 302, 303}:
            return "GET", None, filtered
        raise ConnectorError(f"provider HTTP {status}")

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: bytes | None = None,
    ) -> HttpResponse:
        current_method = method.upper()
        current_url = url
        current_headers = {
            str(key): str(value) for key, value in (headers or {}).items()
        }
        current_body = body
        deadline = self._monotonic() + self.timeout_seconds

        for redirect_count in range(self.max_redirects + 1):
            try:
                _remaining_seconds(deadline, self._monotonic)
            except TimeoutError as exc:
                raise RetryableConnectorError(
                    "provider request deadline exceeded"
                ) from exc
            parts, host, port, addresses = self._validated_url(current_url)
            try:
                _remaining_seconds(deadline, self._monotonic)
            except TimeoutError as exc:
                raise RetryableConnectorError(
                    "provider request deadline exceeded"
                ) from exc
            hop_headers = {
                key: value
                for key, value in current_headers.items()
                if key.casefold() != "host"
            }
            hop_headers["Host"] = self._host_header(parts, host, port)
            request = urllib.request.Request(
                current_url,
                data=current_body,
                headers=hop_headers,
                method=current_method,
            )
            result = self._perform(
                request,
                addresses=addresses,
                server_hostname=host,
                port=port,
                deadline=deadline,
            )
            if isinstance(result, HttpResponse):
                return result
            if redirect_count == self.max_redirects:
                raise ConnectorError("provider exceeded the redirect limit")

            current_method, current_body, current_headers = self._redirect_request(
                current_method,
                current_body,
                current_headers,
                result.status,
            )
            redirected_url = urljoin(current_url, result.location).replace(" ", "%20")
            previous_origin = (
                parts.scheme.casefold(),
                host,
                port,
            )
            redirected_parts = urlsplit(redirected_url)
            try:
                redirected_port = redirected_parts.port or 443
            except ValueError as exc:
                raise ConnectorError(
                    "provider URL is outside the configured HTTPS origin"
                ) from exc
            redirected_origin = (
                redirected_parts.scheme.casefold(),
                str(redirected_parts.hostname or "").casefold(),
                redirected_port,
            )
            if redirected_origin != previous_origin:
                current_headers = {
                    key: value
                    for key, value in current_headers.items()
                    if key.casefold()
                    not in {
                        "authorization",
                        "cookie",
                        "proxy-authorization",
                    }
                }
            current_url = redirected_url

        raise ConnectorError("provider exceeded the redirect limit")
