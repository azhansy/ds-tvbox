from __future__ import annotations

import socket
from collections.abc import Mapping
from dataclasses import dataclass, field

import pytest

from ds_tvbox.errors import ContractError, FetchError, SecurityError
from ds_tvbox.http import (
    ByteBudget,
    HttpRequest,
    SafeHttpClient,
    TransportResponse,
)
from ds_tvbox.models import DeclaredHeaders, HttpExceptionSpec
from ds_tvbox.security import (
    merge_declared_headers,
    normalize_client_url_offline,
    normalize_host,
    normalize_query_key,
    normalize_url,
    redact_url,
    rejected_query_keys,
    resolve_public_addresses,
    split_url_headers,
    validate_declared_headers,
    validate_header_url,
    validate_peer_address,
    validate_registry_host,
)

PUBLIC_IP = "93.184.216.34"
OTHER_PUBLIC_IP = "1.1.1.1"


def _answer(ip: str):
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    sockaddr = (ip, 443, 0, 0) if family == socket.AF_INET6 else (ip, 443)
    return family, socket.SOCK_STREAM, 6, "", sockaddr


def public_resolver(host: str, port: int, *_args):
    del host, port
    return [_answer(PUBLIC_IP)]


@dataclass
class ScriptedTransport:
    responses: list[TransportResponse | BaseException]
    calls: list[tuple[str, str, Mapping[str, str]]] = field(default_factory=list)

    def request(
        self,
        *,
        target,
        connect_ip: str,
        headers: Mapping[str, str],
        connect_timeout: float,
        read_timeout: float,
        max_bytes: int,
    ) -> TransportResponse:
        del connect_timeout, read_timeout, max_bytes
        self.calls.append((target.value, connect_ip, dict(headers)))
        result = self.responses.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


def _response(
    status: int = 200,
    *,
    body: bytes = b"ok",
    headers: tuple[tuple[str, str], ...] = (),
    peer: str = PUBLIC_IP,
    raw_bytes: int | None = None,
) -> TransportResponse:
    return TransportResponse(
        status, headers, body, peer, len(body) if raw_bytes is None else raw_bytes
    )


def _request(url: str = "https://example.com/data") -> HttpRequest:
    return HttpRequest(url=url, allowed_hosts=frozenset({"example.com", "cdn.example.com"}))


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/x?auth=testpub",
        "https://example.com/x?a%75th=testpub",
        "https://example.com/x?ACCESS-TOKEN=testpub",
        "https://example.com/x?api_key=testpub",
        "https://example.com/x?header=User-Agent%3Ax",
        "https://user@example.com/x",
        "file:///etc/passwd",
    ],
)
def test_url_credentials_and_dangerous_forms_are_rejected(url: str) -> None:
    with pytest.raises(SecurityError):
        normalize_url(url, allowed_hosts={"example.com"})


def test_query_key_algorithm_decodes_once_without_plus_as_space() -> None:
    assert normalize_query_key("ACCESS-_TOKEN") == "accesstoken"
    assert normalize_query_key("ｔｏ＿ｋｅｎ") == "token"
    assert normalize_query_key("a%75th") == "auth"
    assert normalize_query_key("a+uth") == "a+uth"
    assert rejected_query_keys("safe=1&auth=x&AUTH=y") == ("auth",)
    with pytest.raises(SecurityError, match="percent encoding"):
        normalize_query_key("auth%ZZ")


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/x?to_ken=visible",
        "https://example.com/x?ｔｏ＿ｋｅｎ=visible",
        "https://example.com/x?to%5Fken=visible",
        "https://127.0.0.1/live.m3u8",
        "https://10.0.0.1/live.m3u8",
        "https://169.254.169.254/latest/meta-data",
        "https://[::1]/live.m3u8",
        "https://[::ffff:127.0.0.1]/live.m3u8",
    ],
)
def test_offline_client_url_rejects_obfuscated_credentials_and_nonpublic_literals(
    url: str,
) -> None:
    with pytest.raises(SecurityError):
        normalize_client_url_offline(url)


def test_offline_client_url_accepts_dns_and_public_literals_without_resolving(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_dns(*_args: object) -> object:
        raise AssertionError("offline validation must not resolve DNS")

    monkeypatch.setattr(socket, "getaddrinfo", unexpected_dns)
    assert normalize_client_url_offline("https://example.com/media").host == "example.com"
    assert normalize_client_url_offline("https://1.1.1.1/media").host == "1.1.1.1"


def test_normalize_url_preserves_query_order_and_removes_fragment_default_port() -> None:
    result = normalize_url(
        "HTTPS://Example.COM:443/a?b=2&a=1#ignored",
        allowed_hosts={"example.com"},
    )
    assert result.value == "https://example.com/a?b=2&a=1"
    assert result.request_target == "/a?b=2&a=1"


def test_http_requires_exact_audited_exception_and_never_for_client() -> None:
    exception = HttpExceptionSpec("example.com", 80, "/public", "legacy index", "2026-07-22")
    assert (
        normalize_url(
            "http://example.com/public/list.txt",
            allowed_hosts={"example.com"},
            http_exceptions=(exception,),
        ).scheme
        == "http"
    )
    with pytest.raises(SecurityError):
        normalize_url(
            "http://example.com/private/list.txt",
            allowed_hosts={"example.com"},
            http_exceptions=(exception,),
        )
    with pytest.raises(SecurityError, match="client-visible"):
        normalize_url(
            "http://example.com/public/list.txt",
            allowed_hosts={"example.com"},
            http_exceptions=(exception,),
            client_visible=True,
        )


def test_declared_header_allowlist_sizes_and_url_shapes() -> None:
    headers = validate_declared_headers(
        {
            "user-agent": "TVBox",
            "Referer": "https://example.com/path?safe=value",
            "Origin": "https://example.com",
        }
    )
    assert headers.values["User-Agent"] == "TVBox"
    assert headers.values["Origin"] == "https://example.com"
    with pytest.raises(SecurityError):
        validate_declared_headers({"X-API-Key": "visible"})
    with pytest.raises(SecurityError):
        validate_declared_headers({"Range": "bytes=0-10"})
    with pytest.raises(SecurityError):
        validate_declared_headers({"Referer": "https://example.com/?token=visible"})
    with pytest.raises(SecurityError):
        validate_declared_headers({"Origin": "https://example.com/path"})
    with pytest.raises(SecurityError):
        validate_declared_headers({"Accept": "x\r\nInjected: true"})
    with pytest.raises(SecurityError):
        validate_declared_headers({"Accept": "x" * 1025})


def test_url_line_headers_are_parsed_once_and_unknown_headers_rejected() -> None:
    url, headers = split_url_headers(
        "https://example.com/live.m3u8|User-Agent=TVBox&Referer=https%3A%2F%2Fexample.com%2F"
    )
    assert url == "https://example.com/live.m3u8"
    assert headers is not None
    assert headers.values["Referer"] == "https://example.com/"
    with pytest.raises(SecurityError):
        split_url_headers("https://example.com/x|Cookie=session")


def test_dns_rejects_private_and_mixed_answers_and_pins_public_snapshot() -> None:
    assert resolve_public_addresses("example.com", 443, resolver=public_resolver) == (PUBLIC_IP,)

    def private_resolver(*_args):
        return [_answer("127.0.0.1")]

    def mixed_resolver(*_args):
        return [_answer(PUBLIC_IP), _answer("169.254.169.254")]

    with pytest.raises(SecurityError):
        resolve_public_addresses("example.com", 443, resolver=private_resolver)
    with pytest.raises(SecurityError):
        resolve_public_addresses("example.com", 443, resolver=mixed_resolver)
    assert validate_peer_address(PUBLIC_IP, [PUBLIC_IP]) == PUBLIC_IP
    with pytest.raises(SecurityError):
        validate_peer_address(OTHER_PUBLIC_IP, [PUBLIC_IP])


def test_redaction_never_contains_query_values() -> None:
    redacted = redact_url("https://user:password@example.com/x?token=topsecret&safe=also-secret")
    assert "password" not in redacted
    assert "topsecret" not in redacted
    assert "also-secret" not in redacted
    assert redacted.endswith("token=<redacted>&safe=<redacted>")


def test_safe_http_connects_to_resolved_ip_and_checks_peer() -> None:
    transport = ScriptedTransport([_response()])
    client = SafeHttpClient(transport=transport, resolver=public_resolver)
    response = client.fetch(_request())
    assert response.body == b"ok"
    assert transport.calls[0][1] == PUBLIC_IP
    assert transport.calls[0][2]["User-Agent"].startswith("DS-TVBox/")

    wrong_peer = ScriptedTransport([_response(peer=OTHER_PUBLIC_IP)])
    with pytest.raises(SecurityError, match="peer"):
        SafeHttpClient(transport=wrong_peer, resolver=public_resolver).fetch(_request())


def test_safe_http_revalidates_redirect_and_blocks_private_target() -> None:
    transport = ScriptedTransport(
        [_response(302, headers=(("Location", "https://internal.example/private"),))]
    )

    def resolver(host: str, port: int, *_args):
        del port
        return [_answer("10.0.0.1" if host == "internal.example" else PUBLIC_IP)]

    request = HttpRequest(
        "https://example.com/start",
        allowed_hosts=frozenset({"example.com"}),
        allow_discovered_host=True,
    )
    with pytest.raises(SecurityError, match="private"):
        SafeHttpClient(transport=transport, resolver=resolver).fetch(request)
    assert len(transport.calls) == 1


def test_safe_http_retries_transient_status_and_respects_budget() -> None:
    sleeps: list[float] = []
    transport = ScriptedTransport([_response(503), _response(200, body=b"done")])
    budget = ByteBudget(10)
    response = SafeHttpClient(
        transport=transport,
        resolver=public_resolver,
        sleeper=sleeps.append,
        budget=budget,
    ).fetch(_request())
    assert response.status == 200
    assert response.attempts == 2
    assert sleeps == [1.0]
    assert budget.used == 6

    too_large = ScriptedTransport([_response(body=b"12345", raw_bytes=5)])
    with pytest.raises(FetchError, match="over-limit"):
        SafeHttpClient(transport=too_large, resolver=public_resolver).fetch(
            HttpRequest("https://example.com/x", frozenset({"example.com"}), max_bytes=4)
        )


def test_safe_http_retries_every_5xx_status() -> None:
    sleeps: list[float] = []
    transport = ScriptedTransport([_response(501), _response(200)])
    response = SafeHttpClient(
        transport=transport,
        resolver=public_resolver,
        sleeper=sleeps.append,
    ).fetch(_request())
    assert response.status == 200
    assert response.attempts == 2
    assert sleeps == [1.0]


def test_safe_http_revalidates_declared_headers_even_if_model_was_bypassed() -> None:
    transport = ScriptedTransport([_response()])
    request = HttpRequest(
        "https://example.com/x",
        frozenset({"example.com"}),
        declared_headers=DeclaredHeaders({"Authorization": "visible"}),
    )
    with pytest.raises(SecurityError):
        SafeHttpClient(transport=transport, resolver=public_resolver).fetch(request)
    assert not transport.calls


def test_host_normalization_supports_idna_ipv6_and_rejects_ambiguous_names() -> None:
    assert normalize_host("Example.COM.") == "example.com"
    assert normalize_host("bücher.example") == "xn--bcher-kva.example"
    assert normalize_host("2001:4860:4860::8888") == "2001:4860:4860::8888"
    assert validate_registry_host("Example.COM") == "example.com"
    with pytest.raises(ContractError, match="IP literals"):
        validate_registry_host("8.8.8.8")

    for value in (
        "",
        ".",
        "bad\\host.example",
        "bad%20host.example",
        "singlelabel",
        "-bad.example",
        f"{'a' * 250}.example",
        "\ud800.example",
    ):
        with pytest.raises(SecurityError):
            normalize_host(value)


@pytest.mark.parametrize(
    "url",
    [
        "",
        "https://example.com/line\nbreak",
        "https://example.com\\path",
        "https://example.com:99999/x",
        "https:///missing-host",
        "https://other.example/x",
    ],
)
def test_url_structure_and_port_are_strict(url: str) -> None:
    with pytest.raises(SecurityError):
        normalize_url(url, allowed_hosts={"example.com"})

    custom = normalize_url("https://example.com:8443/x", allowed_hosts={"example.com"})
    assert custom.value == "https://example.com:8443/x"
    ipv6 = normalize_url(
        "https://[2001:4860:4860::8888]/x",
        allow_discovered_host=True,
    )
    assert ipv6.value.startswith("https://[")


@pytest.mark.parametrize(
    "value",
    [
        "",
        "https://example.com/x\n",
        "https://example.com:invalid",
        "relative/path",
        "https://user@example.com",
        "https://example.com?token=secret",
    ],
)
def test_header_url_rejects_malformed_or_sensitive_destinations(value: str) -> None:
    with pytest.raises(SecurityError):
        validate_header_url(value)

    assert validate_header_url("http://example.com:8080/path").value == (
        "http://example.com:8080/path"
    )


def test_declared_header_json_and_merge_contracts() -> None:
    assert validate_declared_headers('{"Accept":"application/json"}').values == {
        "Accept": "application/json"
    }
    for raw in (
        '{"Accept":"one","Accept":"two"}',
        "[]",
        "not-json",
    ):
        with pytest.raises(SecurityError):
            validate_declared_headers(raw)

    with pytest.raises(SecurityError, match="object"):
        validate_declared_headers(1)  # type: ignore[arg-type]
    with pytest.raises(SecurityError, match="strings"):
        validate_declared_headers({"Accept": 1})
    with pytest.raises(SecurityError, match="duplicate"):
        validate_declared_headers({"accept": "one", "Accept": "two"})
    with pytest.raises(SecurityError, match="block is too large"):
        validate_declared_headers(
            {
                "User-Agent": "a" * 1024,
                "Referer": "https://example.com/" + "a" * 1000,
                "Origin": "https://example.com",
                "Accept": "a" * 1024,
                "Accept-Language": "a" * 1024,
            }
        )

    first = DeclaredHeaders({"Accept": "application/json"})
    second = DeclaredHeaders({"User-Agent": "TVBox"})
    assert merge_declared_headers(None) is None
    assert merge_declared_headers(first, None, second).values == {
        "Accept": "application/json",
        "User-Agent": "TVBox",
    }
    with pytest.raises(SecurityError, match="duplicate"):
        merge_declared_headers(first, first)


@pytest.mark.parametrize(
    "value",
    [
        "https://example.com/x|Accept=a|User-Agent=b",
        "|Accept=a",
        "https://example.com/x|",
        "https://example.com/x|Accept",
        "https://example.com/x|Accept=a&",
        "https://example.com/x|Accept=a&Accept=b",
        "https://example.com/x|Accept=%ZZ",
    ],
)
def test_url_line_header_syntax_fails_closed(value: str) -> None:
    with pytest.raises(SecurityError):
        split_url_headers(value)

    assert split_url_headers("  https://example.com/x  ") == (
        "https://example.com/x",
        None,
    )


def test_dns_literal_filtering_invalid_answers_and_deterministic_order() -> None:
    assert resolve_public_addresses("8.8.8.8", 443) == ("8.8.8.8",)
    with pytest.raises(SecurityError, match="special-purpose"):
        resolve_public_addresses("127.0.0.1", 443)

    def invalid_ip_resolver(*_args: object):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("not-an-ip", 443))]

    with pytest.raises(SecurityError, match="invalid IP"):
        resolve_public_addresses("example.com", 443, resolver=invalid_ip_resolver)

    def unusable_resolver(*_args: object):
        return [
            (socket.AF_UNIX, socket.SOCK_STREAM, 0, "", ("ignored",)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ()),
        ]

    with pytest.raises(OSError, match="no usable address"):
        resolve_public_addresses("example.com", 443, resolver=unusable_resolver)

    def mixed_version_resolver(*_args: object):
        return [
            _answer("2001:4860:4860::8888"),
            _answer("8.8.8.8"),
            _answer("8.8.8.8"),
        ]

    assert resolve_public_addresses(
        "example.com",
        443,
        resolver=mixed_version_resolver,
    ) == ("8.8.8.8", "2001:4860:4860::8888")


def test_peer_validation_handles_invalid_and_ipv4_mapped_addresses() -> None:
    with pytest.raises(SecurityError, match="invalid peer"):
        validate_peer_address("not-an-ip", [PUBLIC_IP])
    assert validate_peer_address("::ffff:8.8.8.8", ["8.8.8.8"]) == "8.8.8.8"


def test_redaction_handles_invalid_authority_missing_host_and_ipv6() -> None:
    assert redact_url("https://example.com:invalid/x") == "<invalid-url>"
    assert "<invalid-host>" in redact_url("file:///tmp/x?secret=value")
    assert redact_url("https://[2001:4860:4860::8888]:8443/x?a=1").startswith(
        "https://[2001:4860:4860::8888]:8443/"
    )
