from __future__ import annotations

import gzip
import multiprocessing
import socket
import ssl
import subprocess
import sys
import threading
import time
import zlib
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from ds_tvbox.errors import FetchError, SecurityError
from ds_tvbox.http import (
    ByteBudget,
    ConcurrencyLimits,
    HttpRequest,
    HttpResponse,
    PinnedHttpTransport,
    SafeHttpClient,
    TransportResponse,
    _decode_response_stream,
    _parse_retry_after,
    _resolver_process_context,
)
from ds_tvbox.models import DeclaredHeaders

PUBLIC_IP = "93.184.216.34"
SECOND_PUBLIC_IP = "1.1.1.1"


def _answer(ip: str) -> tuple[int, int, int, str, tuple[Any, ...]]:
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    sockaddr: tuple[Any, ...] = (ip, 443, 0, 0) if family == socket.AF_INET6 else (ip, 443)
    return family, socket.SOCK_STREAM, 6, "", sockaddr


def public_resolver(host: str, port: int, *_args: object):
    del host, port
    return [_answer(PUBLIC_IP)]


def picklable_blocking_resolver(*_args: object):
    time.sleep(60)
    return [_answer(PUBLIC_IP)]


class FakeStream:
    def __init__(
        self,
        chunks: list[bytes],
        *,
        headers: Mapping[str, str] | None = None,
        status: int = 200,
    ) -> None:
        self._chunks = list(chunks)
        self._headers = dict(headers or {})
        self.status = status

    def getheader(self, name: str) -> str | None:
        return self._headers.get(name)

    def read(self, amount: int) -> bytes:
        del amount
        return self._chunks.pop(0) if self._chunks else b""

    def getheaders(self) -> list[tuple[str, str]]:
        return list(self._headers.items())


def transport_response(
    status: int = 200,
    *,
    body: bytes = b"ok",
    headers: tuple[tuple[str, str], ...] = (),
    peer_ip: str = PUBLIC_IP,
    raw_bytes_read: int | None = None,
) -> TransportResponse:
    return TransportResponse(
        status=status,
        headers=headers,
        body=body,
        peer_ip=peer_ip,
        raw_bytes_read=len(body) if raw_bytes_read is None else raw_bytes_read,
    )


class ScriptedTransport:
    def __init__(self, results: list[TransportResponse | BaseException]) -> None:
        self.results = list(results)
        self.calls: list[tuple[str, str, Mapping[str, str]]] = []

    def request(
        self,
        *,
        target: Any,
        connect_ip: str,
        headers: Mapping[str, str],
        connect_timeout: float,
        read_timeout: float,
        max_bytes: int,
    ) -> TransportResponse:
        del connect_timeout, read_timeout, max_bytes
        self.calls.append((target.value, connect_ip, dict(headers)))
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_bytes": 0},
        {"connect_timeout": 0},
        {"read_timeout": -1},
        {"max_redirects": -1},
        {"max_redirects": 6},
        {"max_attempts": 0},
        {"max_attempts": 4},
        {"internal_range": (-1, 0)},
        {"internal_range": (2, 1)},
        {"internal_range": (0, 1024 * 1024)},
    ],
)
def test_http_request_rejects_unbounded_parameters(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        HttpRequest("https://example.com", frozenset({"example.com"}), **kwargs)  # type: ignore[arg-type]


def test_response_header_contracts_are_case_insensitive() -> None:
    transport = transport_response(headers=(("X-Test", "one"),))
    assert transport.header("x-test") == "one"
    assert transport.header("missing") is None
    with pytest.raises(FetchError, match="multiple Location"):
        transport_response(headers=(("Location", "/a"), ("location", "/b"))).header(
            "Location"
        )

    response = HttpResponse(200, "https://example.com", (("X", "one"), ("x", "two")), b"", 0, 1, 0)
    assert response.header("X") == "two"
    assert response.header("missing") is None


def test_budget_and_concurrency_limits_reject_invalid_values_and_release_slots() -> None:
    with pytest.raises(ValueError):
        ByteBudget(0)
    budget = ByteBudget(3)
    with pytest.raises(ValueError):
        budget.consume(-1)
    budget.consume(3)
    assert budget.used == 3
    with pytest.raises(FetchError, match="budget"):
        budget.consume(1)

    for args in ((0, 1), (1, 0), (1, 2)):
        with pytest.raises(ValueError):
            ConcurrencyLimits(*args)
    limits = ConcurrencyLimits(1, 1)
    with limits.slot("example.com"):
        assert "example.com" in limits._hosts
    # A released bounded semaphore can be acquired again without blocking.
    entered: list[bool] = []
    with limits.slot("example.com"):
        entered.append(True)
    assert entered == [True]


@pytest.mark.parametrize(
    ("encoding", "encoder"),
    [
        ("identity", lambda value: value),
        ("", lambda value: value),
        ("gzip", gzip.compress),
        ("deflate", zlib.compress),
    ],
)
def test_decode_response_stream_supports_bounded_encodings(
    encoding: str,
    encoder: Any,
) -> None:
    plain = b"payload" * 20
    encoded = encoder(plain)
    stream = FakeStream(
        [encoded[: len(encoded) // 2], encoded[len(encoded) // 2 :]],
        headers={"Content-Encoding": encoding, "Content-Length": str(len(encoded))},
    )
    decoded, raw_count = _decode_response_stream(stream, max_bytes=1024)  # type: ignore[arg-type]
    assert decoded == plain
    assert raw_count == len(encoded)


@pytest.mark.parametrize(
    ("stream", "max_bytes", "message"),
    [
        (FakeStream([], headers={"Content-Length": "invalid"}), 5, "invalid Content-Length"),
        (FakeStream([], headers={"Content-Length": "-1"}), 5, "exceeds configured limit"),
        (FakeStream([], headers={"Content-Length": "11"}), 5, "exceeds configured limit"),
        (FakeStream([], headers={"Content-Encoding": "br"}), 5, "unsupported Content-Encoding"),
        (FakeStream([b"123456"]), 5, "transfer exceeds configured limit"),
        (
            FakeStream([gzip.compress(b"x" * 100)], headers={"Content-Encoding": "gzip"}),
            30,
            "decoded response body exceeds configured limit",
        ),
        (
            FakeStream(
                [b"not-deflate"],
                headers={"Content-Encoding": "deflate"},
            ),
            20,
            "invalid compressed",
        ),
        (
            FakeStream([gzip.compress(b"payload")[:-2]], headers={"Content-Encoding": "gzip"}),
            50,
            "truncated compressed",
        ),
    ],
)
def test_decode_response_stream_fails_closed(
    stream: FakeStream,
    max_bytes: int,
    message: str,
) -> None:
    with pytest.raises(FetchError, match=message):
        _decode_response_stream(stream, max_bytes=max_bytes)  # type: ignore[arg-type]


class FakeSocket:
    def __init__(self) -> None:
        self.timeout: float | None = None
        self.closed = False

    def settimeout(self, value: float) -> None:
        self.timeout = value

    def getpeername(self) -> tuple[str, int]:
        return PUBLIC_IP, 443

    def close(self) -> None:
        self.closed = True


class FakeConnection:
    instances: list[FakeConnection] = []

    def __init__(self, *_args: object) -> None:
        self.sock = FakeSocket()
        self.request_args: tuple[str, str, dict[str, str]] | None = None
        self.closed = False
        self.__class__.instances.append(self)

    def request(self, method: str, target: str, headers: dict[str, str]) -> None:
        self.request_args = method, target, headers

    def getresponse(self) -> FakeStream:
        return FakeStream([b"body"], headers={"X-Test": "value"}, status=206)

    def close(self) -> None:
        self.closed = True


@pytest.mark.parametrize("url", ["https://example.com/a?q=1", "http://example.com/a?q=1"])
def test_pinned_transport_uses_selected_connection_and_always_closes(
    monkeypatch: pytest.MonkeyPatch,
    url: str,
) -> None:
    import ds_tvbox.http as http_module
    from ds_tvbox.models import HttpExceptionSpec
    from ds_tvbox.security import normalize_url

    FakeConnection.instances.clear()
    monkeypatch.setattr(http_module, "_PinnedHTTPSConnection", FakeConnection)
    monkeypatch.setattr(http_module, "_PinnedHTTPConnection", FakeConnection)
    exception = HttpExceptionSpec("example.com", 80, "/", "test", "2026-07-22")
    target = normalize_url(url, allowed_hosts={"example.com"}, http_exceptions=(exception,))

    result = PinnedHttpTransport().request(
        target=target,
        connect_ip=PUBLIC_IP,
        headers={"Accept": "*/*"},
        connect_timeout=1,
        read_timeout=2,
        max_bytes=16,
    )

    connection = FakeConnection.instances[-1]
    assert result.status == 206
    assert result.body == b"body"
    assert result.peer_ip == PUBLIC_IP
    assert connection.request_args == ("GET", "/a?q=1", {"Accept": "*/*"})
    assert connection.sock.timeout == 2
    assert connection.closed is True


def test_pinned_connections_connect_to_selected_ip_and_close_failed_tls_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ds_tvbox.http as http_module

    sockets: list[FakeSocket] = []

    def create_connection(address: tuple[str, int], timeout: float) -> FakeSocket:
        assert address[0] == PUBLIC_IP
        assert timeout == 1
        sock = FakeSocket()
        sockets.append(sock)
        return sock

    monkeypatch.setattr(http_module.socket, "create_connection", create_connection)
    plain = http_module._PinnedHTTPConnection("example.com", PUBLIC_IP, 80, 1)
    plain.connect()
    assert plain.sock is sockets[0]

    context = ssl.create_default_context()

    def fail_wrap_socket(
        self: ssl.SSLContext,
        sock: FakeSocket,
        *,
        server_hostname: str,
        **_kwargs: object,
    ) -> None:
        assert self is context
        assert server_hostname == "example.com"
        assert sock is sockets[-1]
        raise ssl.SSLError("handshake")

    monkeypatch.setattr(ssl.SSLContext, "wrap_socket", fail_wrap_socket)

    secure = http_module._PinnedHTTPSConnection(
        "example.com",
        PUBLIC_IP,
        443,
        1,
        context,
    )
    with pytest.raises(ssl.SSLError):
        secure.connect()
    assert sockets[-1].closed is True


def test_retry_after_accepts_seconds_and_dates_and_rejects_invalid_values() -> None:
    now = datetime(2026, 7, 22, tzinfo=UTC)
    assert _parse_retry_after(None, lambda: now) is None
    assert _parse_retry_after(" 12 ", lambda: now) == 12
    assert _parse_retry_after("invalid", lambda: now) is None
    assert _parse_retry_after("Wed, 22 Jul 2026 00:00:03 GMT", lambda: now) == 3
    assert _parse_retry_after("Wed, 22 Jul 2026 00:00:00", lambda: now) == 0


def test_safe_http_fails_over_addresses_and_preserves_internal_headers() -> None:
    def resolver(host: str, port: int, *_args: object):
        del host, port
        return [_answer(PUBLIC_IP), _answer(SECOND_PUBLIC_IP)]

    transport = ScriptedTransport([TimeoutError("first IP"), transport_response(peer_ip=PUBLIC_IP)])
    client = SafeHttpClient(transport=transport, resolver=resolver)
    result = client.fetch(
        HttpRequest(
            "https://example.com/data",
            frozenset({"example.com"}),
            declared_headers=DeclaredHeaders({"Accept-Language": "zh-CN"}),
            internal_range=(5, 9),
        )
    )

    assert result.status == 200
    assert len(transport.calls) == 2
    assert {item[1] for item in transport.calls} == {PUBLIC_IP, SECOND_PUBLIC_IP}
    assert transport.calls[-1][2]["Range"] == "bytes=5-9"
    assert transport.calls[-1][2]["Accept-Language"] == "zh-CN"


def test_safe_http_validates_header_destinations_before_request() -> None:
    def resolver(host: str, port: int, *_args: object):
        del host, port
        return [_answer(PUBLIC_IP)]

    transport = ScriptedTransport([transport_response()])
    request = HttpRequest(
        "https://example.com/data",
        frozenset({"example.com"}),
        declared_headers=DeclaredHeaders(
            {"Referer": "https://referer.example/path", "Origin": "https://origin.example"}
        ),
    )
    SafeHttpClient(transport=transport, resolver=resolver).fetch(request)
    assert len(transport.calls) == 1

    def private_header_resolver(host: str, port: int, *_args: object):
        del port
        return [_answer("10.0.0.1" if host == "referer.example" else PUBLIC_IP)]

    with pytest.raises(SecurityError, match="private"):
        SafeHttpClient(
            transport=ScriptedTransport([transport_response()]),
            resolver=private_header_resolver,
        ).fetch(request)


@pytest.mark.parametrize("boundary", ["referer", "origin", "initial", "redirect"])
def test_safe_http_enforces_dns_deadline_at_every_resolution_boundary(
    boundary: str,
) -> None:
    context = multiprocessing.get_context("fork")
    release = context.Event()
    blocked_host = {
        "referer": "referer.example",
        "origin": "origin.example",
        "initial": "example.com",
        "redirect": "cdn.example.com",
    }[boundary]

    def blocking_resolver(host: str, port: int, *_args: object):
        del port
        if host == blocked_host:
            release.wait()
        return [_answer(PUBLIC_IP)]

    declared = None
    if boundary == "referer":
        declared = DeclaredHeaders({"Referer": "https://referer.example/path"})
    elif boundary == "origin":
        declared = DeclaredHeaders({"Origin": "https://origin.example"})
    if boundary == "redirect":
        transport = ScriptedTransport(
            [transport_response(302, headers=(("Location", "https://cdn.example.com/end"),))]
        )
    else:
        transport = ScriptedTransport([] if boundary != "referer" and boundary != "origin" else [])
    request = HttpRequest(
        "https://example.com/start",
        frozenset({"example.com", "cdn.example.com"}),
        declared_headers=declared,
        max_attempts=1,
    )

    started = time.monotonic()
    with pytest.raises(FetchError, match="DNS resolution"):
        SafeHttpClient(
            transport=transport,
            resolver=blocking_resolver,
            dns_timeout=0.05,
        ).fetch(request)
    assert time.monotonic() - started < 0.5
    assert not [
        child
        for child in multiprocessing.active_children()
        if child.name == "ds-tvbox-resolver"
    ]
    assert len(transport.calls) == (1 if boundary == "redirect" else 0)


def test_repeated_dns_timeouts_keep_process_and_thread_resources_constant() -> None:
    context = multiprocessing.get_context("fork")
    release = context.Event()

    def blocking_resolver(*_args: object):
        release.wait()
        return [_answer(PUBLIC_IP)]

    baseline_threads = {thread.ident for thread in threading.enumerate()}
    started = time.monotonic()
    for _ in range(75):
        with pytest.raises(FetchError, match="DNS resolution"):
            SafeHttpClient(
                transport=ScriptedTransport([]),
                resolver=blocking_resolver,
                dns_timeout=0.01,
            ).fetch(
                HttpRequest(
                    "https://example.com/start",
                    frozenset({"example.com"}),
                    max_attempts=1,
                )
            )
        assert not [
            child
            for child in multiprocessing.active_children()
            if child.name == "ds-tvbox-resolver"
        ]
    assert time.monotonic() - started < 5
    assert {
        thread.ident for thread in threading.enumerate() if thread.ident not in baseline_threads
    } == set()


def test_default_resolver_isolation_is_fork_safe_under_concurrent_fetches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if "forkserver" not in multiprocessing.get_all_start_methods():
        pytest.skip("forkserver is unavailable on this platform")
    monkeypatch.setattr(socket, "getaddrinfo", picklable_blocking_resolver)
    resolver = socket.getaddrinfo
    assert _resolver_process_context(resolver).get_start_method() == "forkserver"
    client = SafeHttpClient(
        transport=ScriptedTransport([]),
        resolver=resolver,
        dns_timeout=0.1,
    )
    request = HttpRequest(
        "https://example.com/start",
        frozenset({"example.com"}),
        max_attempts=1,
    )

    def fetch_once(_index: int) -> str:
        with pytest.raises(FetchError, match="DNS resolution"):
            client.fetch(request)
        return "timed-out"

    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = tuple(executor.map(fetch_once, range(8)))

    assert results == ("timed-out",) * 8
    assert time.monotonic() - started < 2
    assert not [
        child
        for child in multiprocessing.active_children()
        if child.name == "ds-tvbox-resolver"
    ]


def test_timed_out_dns_result_cannot_be_consumed_by_the_next_fetch() -> None:
    context = multiprocessing.get_context("fork")
    call_count = context.Value("i", 0)

    def resolver(host: str, port: int, *_args: object):
        del host, port
        with call_count.get_lock():
            call_count.value += 1
            current = call_count.value
        if current == 1:
            time.sleep(0.2)
            return [_answer(PUBLIC_IP)]
        return [_answer(SECOND_PUBLIC_IP)]

    client = SafeHttpClient(
        transport=ScriptedTransport([]),
        resolver=resolver,
        dns_timeout=0.03,
    )
    request = HttpRequest(
        "https://example.com/start",
        frozenset({"example.com"}),
        max_attempts=1,
    )
    with pytest.raises(FetchError, match="DNS resolution"):
        client.fetch(request)

    transport = ScriptedTransport(
        [transport_response(peer_ip=SECOND_PUBLIC_IP)]
    )
    client = SafeHttpClient(
        transport=transport,
        resolver=resolver,
        dns_timeout=0.2,
    )
    response = client.fetch(request)

    assert response.status == 200
    assert transport.calls[0][1] == SECOND_PUBLIC_IP
    assert call_count.value == 2


def test_dns_timeout_does_not_delay_interpreter_exit() -> None:
    code = """
import time
from ds_tvbox.http import resolve_public_addresses_with_deadline

def resolver(*_args):
    time.sleep(60)
    return []

try:
    resolve_public_addresses_with_deadline(
        "example.com", 443, resolver=resolver, timeout=0.05
    )
except TimeoutError:
    print("timed-out")
"""
    started = time.monotonic()
    result = subprocess.run(  # noqa: S603 - fixed interpreter and test-owned source
        [sys.executable, "-c", code],
        check=False,
        capture_output=True,
        text=True,
        timeout=2,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "timed-out"
    assert time.monotonic() - started < 2


def test_safe_http_maps_dns_failure_and_does_not_retry_tls_verification() -> None:
    def failing_resolver(*_args: object):
        raise OSError("resolver unavailable")

    sleeps: list[float] = []
    with pytest.raises(FetchError, match="DNS resolution"):
        SafeHttpClient(
            transport=ScriptedTransport([]),
            resolver=failing_resolver,
            sleeper=sleeps.append,
        ).fetch(
            HttpRequest("https://example.com", frozenset({"example.com"}), max_attempts=1)
        )
    assert sleeps == []

    transport = ScriptedTransport([ssl.SSLCertVerificationError("bad certificate")])
    with pytest.raises(FetchError, match="certificate"):
        SafeHttpClient(
            transport=transport,
            resolver=public_resolver,
            sleeper=sleeps.append,
        ).fetch(HttpRequest("https://example.com", frozenset({"example.com"})))
    assert len(transport.calls) == 1


def test_safe_http_redirect_contracts_and_retry_accounting() -> None:
    transport = ScriptedTransport(
        [
            transport_response(302, headers=(("Location", "/next"),)),
            transport_response(503),
            transport_response(301, headers=(("Location", "https://cdn.example.com/end"),)),
            transport_response(200, body=b"done"),
        ]
    )
    sleeps: list[float] = []
    response = SafeHttpClient(
        transport=transport,
        resolver=public_resolver,
        sleeper=sleeps.append,
    ).fetch(
        HttpRequest(
            "https://example.com/start",
            frozenset({"example.com", "cdn.example.com"}),
            max_attempts=2,
        )
    )
    assert response.final_url == "https://cdn.example.com/end"
    assert response.attempts == 2
    assert response.redirects == 2
    assert sleeps == [1]

    for first, message in (
        (transport_response(302), "no unique Location"),
        (
            transport_response(
                302,
                headers=(("Location", "/same"), ("location", "/other")),
            ),
            "multiple Location",
        ),
    ):
        with pytest.raises(FetchError, match=message):
            SafeHttpClient(
                transport=ScriptedTransport([first]),
                resolver=public_resolver,
            ).fetch(
                HttpRequest(
                    "https://example.com/start",
                    frozenset({"example.com"}),
                    max_attempts=1,
                )
            )


def test_safe_http_detects_redirect_loop_and_maximum() -> None:
    loop = ScriptedTransport(
        [transport_response(302, headers=(("Location", "/start"),))]
    )
    with pytest.raises(FetchError, match="loop"):
        SafeHttpClient(transport=loop, resolver=public_resolver).fetch(
            HttpRequest(
                "https://example.com/start",
                frozenset({"example.com"}),
                max_attempts=1,
            )
        )

    maximum = ScriptedTransport(
        [transport_response(302, headers=(("Location", "/next"),))]
    )
    with pytest.raises(FetchError, match="maximum redirects"):
        SafeHttpClient(transport=maximum, resolver=public_resolver).fetch(
            HttpRequest(
                "https://example.com/start",
                frozenset({"example.com"}),
                max_redirects=0,
                max_attempts=1,
            )
        )


def test_safe_http_retry_after_bounds_and_terminal_transient_response() -> None:
    now = datetime(2026, 7, 22, tzinfo=UTC)
    sleeps: list[float] = []
    short = ScriptedTransport(
        [
            transport_response(429, headers=(("Retry-After", "2"),)),
            transport_response(408, headers=(("Retry-After", "invalid"),)),
            transport_response(599),
        ]
    )
    result = SafeHttpClient(
        transport=short,
        resolver=public_resolver,
        sleeper=sleeps.append,
        now=lambda: now,
    ).fetch(HttpRequest("https://example.com", frozenset({"example.com"})))
    assert result.status == 599
    assert result.attempts == 3
    assert sleeps == [2, 2]

    long_date = (now + timedelta(seconds=61)).strftime("%a, %d %b %Y %H:%M:%S GMT")
    long = ScriptedTransport(
        [transport_response(503, headers=(("Retry-After", long_date),))]
    )
    result = SafeHttpClient(
        transport=long,
        resolver=public_resolver,
        sleeper=sleeps.append,
        now=lambda: now,
    ).fetch(HttpRequest("https://example.com", frozenset({"example.com"})))
    assert result.status == 503
    assert result.attempts == 1
    assert len(long.calls) == 1


def test_safe_http_retries_fetch_errors_then_surfaces_last_failure() -> None:
    transport = ScriptedTransport(
        [FetchError("one"), FetchError("two"), FetchError("three")]
    )
    sleeps: list[float] = []
    with pytest.raises(FetchError, match="all approved addresses failed"):
        SafeHttpClient(
            transport=transport,
            resolver=public_resolver,
            sleeper=sleeps.append,
        ).fetch(HttpRequest("https://example.com", frozenset({"example.com"})))
    assert sleeps == [1, 2]
    assert len(transport.calls) == 3
