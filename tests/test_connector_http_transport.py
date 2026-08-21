from __future__ import annotations

import http.client
import socket
from dataclasses import dataclass, field

import pytest

from cogdoc.connectors import http_transport as http_module
from cogdoc.connectors.base import ConnectorError, RetryableConnectorError
from cogdoc.connectors.http_transport import HttpTransport


def _address(ip: str, port: int = 443):
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    sockaddr = (ip, port, 0, 0) if family == socket.AF_INET6 else (ip, port)
    return (family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sockaddr)


class _Response:
    def __init__(
        self,
        url: str,
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
        body: bytes = b"ok",
    ) -> None:
        self.status = status
        self.headers = headers or {}
        self._body = body
        self._offset = 0
        self._url = url

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self, limit: int) -> bytes:
        chunk = self._body[self._offset : self._offset + limit]
        self._offset += len(chunk)
        return chunk

    def geturl(self) -> str:
        return self._url


@dataclass
class _RecordingOpener:
    responses: list[tuple[int, dict[str, str], bytes]] = field(default_factory=list)
    calls: list[dict] = field(default_factory=list)

    def open_pinned(
        self,
        request,
        *,
        timeout,
        addresses,
        server_hostname,
        port,
    ):
        self.calls.append(
            {
                "url": request.full_url,
                "method": request.get_method(),
                "headers": dict(request.header_items()),
                "host": request.get_header("Host"),
                "timeout": timeout,
                "addresses": addresses,
                "server_hostname": server_hostname,
                "port": port,
            }
        )
        status, headers, body = (
            self.responses.pop(0) if self.responses else (200, {}, b"ok")
        )
        return _Response(request.full_url, status=status, headers=headers, body=body)


def test_dns_snapshot_is_resolved_once_and_pinned_before_open():
    resolver_calls: list[str] = []

    def changing_resolver(host, _port, **_kwargs):
        resolver_calls.append(host)
        if len(resolver_calls) == 1:
            return [_address("93.184.216.34")]
        return [_address("127.0.0.1")]

    opener = _RecordingOpener()
    transport = HttpTransport(
        allowed_hosts={"provider.example"},
        opener=opener,
        resolver=changing_resolver,
    )

    response = transport.request("GET", "https://provider.example/data")

    assert response.body == b"ok"
    assert resolver_calls == ["provider.example"]
    assert [address.ip for address in opener.calls[0]["addresses"]] == ["93.184.216.34"]
    assert opener.calls[0]["server_hostname"] == "provider.example"
    assert opener.calls[0]["host"] == "provider.example"


def test_non_default_port_is_pinned_and_preserved_in_host_header():
    resolver_ports: list[int] = []

    def resolver(_host, port, **_kwargs):
        resolver_ports.append(port)
        return [_address("93.184.216.34", port)]

    opener = _RecordingOpener()
    transport = HttpTransport(
        allowed_hosts={"provider.example"},
        opener=opener,
        resolver=resolver,
    )

    transport.request("GET", "https://provider.example:8443/data")

    assert resolver_ports == [8443]
    assert opener.calls[0]["port"] == 8443
    assert opener.calls[0]["addresses"][0].sockaddr == ("93.184.216.34", 8443)
    assert opener.calls[0]["host"] == "provider.example:8443"


def test_every_dns_candidate_must_be_public_before_open():
    opener = _RecordingOpener()
    transport = HttpTransport(
        allowed_hosts={"provider.example"},
        opener=opener,
        resolver=lambda *_args, **_kwargs: [
            _address("93.184.216.34"),
            _address("127.0.0.1"),
        ],
    )

    with pytest.raises(ConnectorError, match="non-public"):
        transport.request("GET", "https://provider.example/data")

    assert opener.calls == []


@pytest.mark.parametrize("ip", ["224.0.0.1", "ff02::1"])
def test_multicast_dns_candidate_is_not_treated_as_public(ip):
    opener = _RecordingOpener()
    transport = HttpTransport(
        allowed_hosts={"provider.example"},
        opener=opener,
        resolver=lambda *_args, **_kwargs: [_address(ip)],
    )

    with pytest.raises(ConnectorError, match="non-public"):
        transport.request("GET", "https://provider.example/data")

    assert opener.calls == []


def test_verified_socket_connector_never_resolves_numeric_target(monkeypatch):
    calls: list[tuple] = []

    class FakeSocket:
        def settimeout(self, timeout) -> None:
            calls.append(("timeout", timeout))

        def bind(self, address) -> None:
            calls.append(("bind", address))

        def connect(self, address) -> None:
            calls.append(("connect", address))

        def close(self) -> None:
            calls.append(("close",))

    monkeypatch.setattr(
        http_module.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: pytest.fail("must not resolve while connecting"),
    )
    monkeypatch.setattr(
        http_module.socket,
        "socket",
        lambda family, kind: calls.append(("socket", family, kind)) or FakeSocket(),
    )
    address = http_module._ResolvedAddress(
        socket.AF_INET,
        "93.184.216.34",
        ("93.184.216.34", 8443),
    )

    sock = http_module._connect_verified((address,), 7.5, None)

    assert isinstance(sock, FakeSocket)
    assert ("connect", ("93.184.216.34", 8443)) in calls


def test_direct_connection_uses_original_hostname_for_tls(monkeypatch):
    class FakeSocket:
        def setsockopt(self, *_args) -> None:
            return None

        def close(self) -> None:
            return None

    class FakeContext:
        def __init__(self) -> None:
            self.server_hostname = None

        def wrap_socket(self, sock, *, server_hostname):
            self.server_hostname = server_hostname
            return sock

    raw_socket = FakeSocket()
    context = FakeContext()
    address = http_module._ResolvedAddress(
        socket.AF_INET,
        "93.184.216.34",
        ("93.184.216.34", 443),
    )
    connected: list[tuple] = []
    monkeypatch.setattr(
        http_module,
        "_connect_verified",
        lambda addresses, timeout, source, **_kwargs: (
            connected.append((addresses, timeout, source)) or raw_socket
        ),
    )
    connection = http_module._PinnedHTTPSConnection(
        "provider.example:443",
        timeout=4.0,
        context=context,
        addresses=(address,),
        server_hostname="provider.example",
        target_port=443,
    )

    connection.connect()

    assert connected[0][0] == (address,)
    assert context.server_hostname == "provider.example"


def test_proxy_connect_targets_pinned_ip_but_tls_uses_hostname(monkeypatch):
    class FakeSocket:
        def close(self) -> None:
            return None

    class FakeContext:
        def __init__(self) -> None:
            self.server_hostname = None

        def wrap_socket(self, sock, *, server_hostname):
            self.server_hostname = server_hostname
            return sock

    context = FakeContext()
    observed: dict = {}

    def fake_proxy_connect(connection):
        observed["host"] = connection._tunnel_host
        observed["port"] = connection._tunnel_port
        observed["headers"] = dict(connection._tunnel_headers)
        connection.sock = FakeSocket()

    monkeypatch.setattr(http.client.HTTPConnection, "connect", fake_proxy_connect)
    address = http_module._ResolvedAddress(
        socket.AF_INET6,
        "2606:2800:220:1:248:1893:25c8:1946",
        ("2606:2800:220:1:248:1893:25c8:1946", 443, 0, 0),
    )
    connection = http_module._PinnedHTTPSConnection(
        "proxy.example:8080",
        timeout=4.0,
        context=context,
        addresses=(address,),
        server_hostname="provider.example",
        target_port=443,
    )
    connection.set_tunnel(
        "provider.example",
        443,
        headers={"Proxy-Authorization": "Basic token"},
    )

    connection.connect()

    assert observed["host"] == address.ip
    assert observed["port"] == 443
    assert observed["headers"]["Host"] == f"[{address.ip}]:443"
    assert observed["headers"]["Proxy-Authorization"] == "Basic token"
    assert context.server_hostname == "provider.example"


def test_pinned_https_handler_passes_context_supported_by_runtime(monkeypatch):
    address = http_module._ResolvedAddress(
        socket.AF_INET,
        "93.184.216.34",
        ("93.184.216.34", 443),
    )
    handler = http_module._PinnedHTTPSHandler(
        addresses=(address,),
        server_hostname="provider.example",
        target_port=443,
    )
    request = object()
    observed: dict = {}

    def fake_do_open(connection_factory, actual_request, **kwargs):
        observed["factory"] = connection_factory
        observed["request"] = actual_request
        observed["kwargs"] = kwargs
        return "opened"

    monkeypatch.setattr(handler, "do_open", fake_do_open)

    assert handler.https_open(request) == "opened"
    assert observed["request"] is request
    assert observed["kwargs"] == {"context": handler._context}


def test_redirect_revalidates_allowlist_dns_and_host_header_each_hop():
    resolver_calls: list[str] = []

    def resolver(host, _port, **_kwargs):
        resolver_calls.append(host)
        return [_address("93.184.216.34" if host == "one.example" else "8.8.8.8")]

    opener = _RecordingOpener(
        responses=[
            (302, {"Location": "https://two.example/final"}, b""),
            (200, {}, b"done"),
        ]
    )
    transport = HttpTransport(
        allowed_hosts={"one.example", "two.example"},
        opener=opener,
        resolver=resolver,
    )

    response = transport.request(
        "GET",
        "https://one.example/start",
        headers={"Host": "attacker.example"},
    )

    assert response.url == "https://two.example/final"
    assert response.body == b"done"
    assert resolver_calls == ["one.example", "two.example"]
    assert [call["host"] for call in opener.calls] == [
        "one.example",
        "two.example",
    ]


def test_zero_redirect_budget_rejects_provider_redirect_before_following():
    opener = _RecordingOpener(
        responses=[(302, {"Location": "https://provider.example/final"}, b"")]
    )
    transport = HttpTransport(
        allowed_hosts={"provider.example"},
        max_redirects=0,
        opener=opener,
        resolver=lambda *_args, **_kwargs: [_address("93.184.216.34")],
    )

    with pytest.raises(ConnectorError, match="redirect"):
        transport.request("POST", "https://provider.example/token", body=b"code=x")

    assert len(opener.calls) == 1


def test_redirect_drops_body_content_headers_like_urllib():
    opener = _RecordingOpener(
        responses=[
            (302, {"Location": "/final"}, b""),
            (200, {}, b"done"),
        ]
    )
    transport = HttpTransport(
        allowed_hosts={"provider.example"},
        opener=opener,
        resolver=lambda *_args, **_kwargs: [_address("93.184.216.34")],
    )

    transport.request(
        "POST",
        "https://provider.example/start",
        headers={"Content-Type": "application/json", "Content-Length": "2"},
        body=b"{}",
    )

    assert [call["method"] for call in opener.calls] == ["POST", "GET"]
    redirected_headers = {
        key.casefold(): value for key, value in opener.calls[1]["headers"].items()
    }
    assert "content-type" not in redirected_headers
    assert "content-length" not in redirected_headers


def test_temporary_redirect_preserves_method_and_body_on_same_origin():
    opener = _RecordingOpener(
        responses=[
            (307, {"Location": "/final"}, b""),
            (200, {}, b"done"),
        ]
    )
    transport = HttpTransport(
        allowed_hosts={"provider.example"},
        opener=opener,
        resolver=lambda *_args, **_kwargs: [_address("93.184.216.34")],
    )

    transport.request(
        "POST",
        "https://provider.example/start",
        headers={"Content-Type": "application/json"},
        body=b"{}",
    )

    assert [call["method"] for call in opener.calls] == ["POST", "POST"]


def test_cross_origin_redirect_strips_credentials_even_when_host_is_allowed():
    opener = _RecordingOpener(
        responses=[
            (302, {"Location": "https://two.example/final"}, b""),
            (200, {}, b"done"),
        ]
    )
    transport = HttpTransport(
        allowed_hosts={"one.example", "two.example"},
        opener=opener,
        resolver=lambda *_args, **_kwargs: [_address("93.184.216.34")],
    )

    transport.request(
        "GET",
        "https://one.example/start",
        headers={
            "Authorization": "Bearer secret",
            "Cookie": "session=secret",
            "X-Safe": "kept",
        },
    )

    redirected = {
        key.casefold(): value for key, value in opener.calls[1]["headers"].items()
    }
    assert "authorization" not in redirected
    assert "cookie" not in redirected
    assert redirected["x-safe"] == "kept"


def test_redirect_to_private_resolution_is_blocked_before_second_open():
    opener = _RecordingOpener(
        responses=[
            (302, {"Location": "https://two.example/final"}, b""),
        ]
    )

    def resolver(host, _port, **_kwargs):
        return [_address("93.184.216.34" if host == "one.example" else "127.0.0.1")]

    transport = HttpTransport(
        allowed_hosts={"one.example", "two.example"},
        opener=opener,
        resolver=resolver,
    )

    with pytest.raises(ConnectorError, match="non-public"):
        transport.request("GET", "https://one.example/start")

    assert len(opener.calls) == 1


def test_dns_address_cardinality_is_bounded_before_open():
    opener = _RecordingOpener()
    addresses = [
        _address(f"8.8.8.{index}")
        for index in range(1, http_module.MAX_RESOLVED_ADDRESSES + 2)
    ]
    transport = HttpTransport(
        allowed_hosts={"provider.example"},
        opener=opener,
        resolver=lambda *_args, **_kwargs: addresses,
    )

    with pytest.raises(ConnectorError, match="too many addresses"):
        transport.request("GET", "https://provider.example/data")

    assert opener.calls == []


def test_request_deadline_is_shared_across_redirect_hops():
    clock = [0.0]

    class SlowOpener(_RecordingOpener):
        def open_pinned(self, *args, **kwargs):
            response = super().open_pinned(*args, **kwargs)
            clock[0] += 0.6
            return response

    opener = SlowOpener(
        responses=[
            (302, {"Location": "/second"}, b""),
            (200, {}, b"done"),
        ]
    )
    transport = HttpTransport(
        allowed_hosts={"provider.example"},
        timeout_seconds=1.0,
        opener=opener,
        resolver=lambda *_args, **_kwargs: [_address("93.184.216.34")],
        monotonic=lambda: clock[0],
    )

    with pytest.raises(RetryableConnectorError, match="provider request"):
        transport.request("GET", "https://provider.example/first")

    assert len(opener.calls) == 2
    assert opener.calls[0]["timeout"] == pytest.approx(1.0)
    assert opener.calls[1]["timeout"] == pytest.approx(0.4)


def test_pinned_address_fallback_consumes_one_shared_deadline(monkeypatch):
    clock = [0.0]
    observed_timeouts: list[float] = []

    class BlackholeSocket:
        def settimeout(self, timeout) -> None:
            observed_timeouts.append(float(timeout))

        def connect(self, _address) -> None:
            clock[0] += 0.4
            raise OSError("black hole")

        def close(self) -> None:
            return None

    monkeypatch.setattr(
        http_module.socket,
        "socket",
        lambda *_args, **_kwargs: BlackholeSocket(),
    )
    addresses = tuple(
        http_module._ResolvedAddress(
            socket.AF_INET,
            f"8.8.8.{index}",
            (f"8.8.8.{index}", 443),
        )
        for index in range(1, 5)
    )

    with pytest.raises(TimeoutError, match="deadline"):
        http_module._connect_verified(
            addresses,
            10.0,
            None,
            deadline=1.0,
            monotonic=lambda: clock[0],
        )

    assert observed_timeouts == pytest.approx([1.0, 0.6, 0.2])
