"""Security primitives for untrusted source URLs and declared HTTP headers.

This module deliberately contains no "unsafe" or "test mode" switch.  Tests can
inject DNS and HTTP transports at the caller boundary, but every address returned
by a resolver is still checked by :func:`resolve_public_addresses`.
"""

from __future__ import annotations

import ipaddress
import json
import re
import socket
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, unquote_to_bytes, urlsplit, urlunsplit

from .errors import ContractError, SecurityError
from .models import DeclaredHeaders, HttpExceptionSpec

_ALLOWED_SCHEMES = frozenset({"http", "https"})
_ALLOWED_DECLARED_HEADERS = {
    "user-agent": "User-Agent",
    "referer": "Referer",
    "origin": "Origin",
    "accept": "Accept",
    "accept-language": "Accept-Language",
}
_SENSITIVE_QUERY_KEYS = frozenset(
    {
        "accesskey",
        "auth",
        "authorization",
        "accesstoken",
        "apikey",
        "cookie",
        "credential",
        "key",
        "password",
        "sig",
        "sign",
        "signature",
        "secret",
        "token",
        "expires",
        "expiry",
    }
)
_HEADER_QUERY_KEYS = frozenset({"header", "headers", "httpheader"})
_BAD_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_BAD_HEADER_CONTROL_RE = re.compile(r"[\x00\r\n]")
_BAD_PERCENT_RE = re.compile(r"%(?![0-9A-Fa-f]{2})")
_DNS_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")

Resolver = Callable[..., Sequence[tuple[int, int, int, str, tuple[Any, ...]]]]


@dataclass(frozen=True)
class NormalizedUrl:
    """A structurally validated HTTP(S) URL."""

    value: str
    scheme: str
    host: str
    port: int
    path: str
    query: str

    @property
    def request_target(self) -> str:
        target = self.path or "/"
        if self.query:
            target = f"{target}?{self.query}"
        return target


def _strict_percent_decode(value: str, *, what: str) -> str:
    if _BAD_PERCENT_RE.search(value):
        raise SecurityError(f"invalid percent encoding in {what}")
    try:
        return unquote_to_bytes(value).decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise SecurityError(f"non-UTF-8 percent encoding in {what}") from exc


def normalize_query_key(raw_key: str) -> str:
    """Apply the exact credential-key normalization from SPEC 10.2.

    ``+`` intentionally remains a plus sign.  Only one strict percent-decoding
    pass is performed.
    """

    decoded = _strict_percent_decode(raw_key, what="query key")
    normalized = unicodedata.normalize("NFKC", decoded)
    normalized = "".join(char.lower() if "A" <= char <= "Z" else char for char in normalized)
    return normalized.replace("_", "").replace("-", "")


def rejected_query_keys(raw_query: str) -> tuple[str, ...]:
    """Return normalized sensitive/header query names without exposing values."""

    rejected: list[str] = []
    if not raw_query:
        return ()
    for pair in raw_query.split("&"):
        raw_key = pair.split("=", 1)[0]
        normalized = normalize_query_key(raw_key)
        if normalized in _SENSITIVE_QUERY_KEYS or normalized in _HEADER_QUERY_KEYS:
            rejected.append(normalized)
    return tuple(sorted(set(rejected)))


def normalize_host(host: str) -> str:
    """Return a canonical DNS/IP host and reject ambiguous DNS spellings."""

    if not host or _BAD_CONTROL_RE.search(host) or "\\" in host or "%" in host:
        raise SecurityError("invalid URL host")
    host = host.rstrip(".")
    if not host:
        raise SecurityError("invalid URL host")
    try:
        return ipaddress.ip_address(host).compressed.lower()
    except ValueError:
        pass
    try:
        ascii_host = host.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise SecurityError("invalid IDNA host") from exc
    if len(ascii_host) > 253:
        raise SecurityError("DNS host is too long")
    labels = ascii_host.split(".")
    if len(labels) < 2 or any(not _DNS_LABEL_RE.fullmatch(label) for label in labels):
        raise SecurityError("host must be a fully-qualified DNS name or IP address")
    return ascii_host


def validate_registry_host(host: str) -> str:
    """Validate a registry allow-list host (exact DNS names only)."""

    normalized = normalize_host(host)
    try:
        ipaddress.ip_address(normalized)
    except ValueError:
        return normalized
    raise ContractError("registry hosts cannot be IP literals")


def _url_port(scheme: str, explicit_port: int | None) -> int:
    if explicit_port is not None:
        if not 1 <= explicit_port <= 65535:
            raise SecurityError("URL port is outside 1..65535")
        return explicit_port
    return 443 if scheme == "https" else 80


def _format_netloc(host: str, port: int, scheme: str) -> str:
    rendered_host = f"[{host}]" if ":" in host else host
    if (scheme, port) in {("https", 443), ("http", 80)}:
        return rendered_host
    return f"{rendered_host}:{port}"


def _decode_security_path(path: str) -> str:
    decoded = _strict_percent_decode(path, what="URL path")
    if "\x00" in decoded or "\\" in decoded:
        raise SecurityError("invalid URL path")
    components: list[str] = []
    for component in decoded.split("/"):
        if component in {"", "."}:
            continue
        if component == "..":
            if components:
                components.pop()
            continue
        components.append(component)
    return "/" + "/".join(components)


def _http_exception_matches(
    host: str,
    port: int,
    path: str,
    exceptions: Iterable[HttpExceptionSpec],
) -> bool:
    normalized_path = _decode_security_path(path or "/")
    for exception in exceptions:
        prefix = _decode_security_path(exception.path_prefix)
        if host == exception.host and port == exception.port and normalized_path.startswith(prefix):
            return True
    return False


def normalize_url(
    url: str,
    *,
    allowed_hosts: Iterable[str] | None = None,
    allow_discovered_host: bool = False,
    http_exceptions: Iterable[HttpExceptionSpec] = (),
    client_visible: bool = False,
) -> NormalizedUrl:
    """Validate and normalize an untrusted HTTP(S) URL.

    This function performs structural and policy checks.  DNS and the connected
    peer are checked separately by :func:`resolve_public_addresses` and
    :func:`validate_peer_address`.
    """

    if not isinstance(url, str) or not url or _BAD_CONTROL_RE.search(url):
        raise SecurityError("URL must be a non-empty control-free string")
    if "\\" in url.split("?", 1)[0]:
        raise SecurityError("backslashes are not allowed in URL authority/path")
    try:
        parts = urlsplit(url)
        explicit_port = parts.port
    except ValueError as exc:
        raise SecurityError("malformed URL") from exc
    scheme = parts.scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise SecurityError(f"dangerous or unsupported URL scheme: {scheme or '<missing>'}")
    if not parts.netloc or parts.hostname is None:
        raise SecurityError("absolute URL with host is required")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise SecurityError("URL userinfo is forbidden")
    host = normalize_host(parts.hostname)
    port = _url_port(scheme, explicit_port)

    configured_hosts = frozenset(normalize_host(item) for item in (allowed_hosts or ()))
    if not allow_discovered_host and host not in configured_hosts:
        raise SecurityError(f"host is not allowed: {host}")

    rejected = rejected_query_keys(parts.query)
    if rejected:
        # Values are deliberately never included in this exception.
        raise SecurityError(f"credential/header query key rejected: {', '.join(rejected)}")

    if scheme == "http":
        if client_visible:
            raise SecurityError("client-visible HTTP URL is forbidden")
        if not _http_exception_matches(host, port, parts.path, http_exceptions):
            raise SecurityError("HTTP URL does not match an audited exception")

    netloc = _format_netloc(host, port, scheme)
    normalized = urlunsplit((scheme, netloc, parts.path, parts.query, ""))
    return NormalizedUrl(
        value=normalized,
        scheme=scheme,
        host=host,
        port=port,
        path=parts.path,
        query=parts.query,
    )


def normalize_client_url_offline(url: str) -> NormalizedUrl:
    """Validate a client-visible URL without performing DNS resolution.

    DNS names are intentionally left for the network fetch boundary, where the
    resolver result and connected peer can both be checked.  Literal addresses
    need no resolver, so reject every non-public literal here as well.  This is
    suitable for independently revalidating a sealed publish artifact.
    """

    normalized = normalize_url(
        url,
        allow_discovered_host=True,
        client_visible=True,
    )
    reject_non_public_ip_literal(normalized.host)
    return normalized


def validate_header_url(value: str, *, origin_only: bool = False) -> NormalizedUrl:
    """Validate a Referer/Origin value without granting an HTTP fetch exception."""

    if not isinstance(value, str) or not value or _BAD_CONTROL_RE.search(value):
        raise SecurityError("invalid Header URL")
    try:
        parts = urlsplit(value)
        explicit_port = parts.port
    except ValueError as exc:
        raise SecurityError("malformed Header URL") from exc
    scheme = parts.scheme.lower()
    if scheme not in _ALLOWED_SCHEMES or not parts.netloc or parts.hostname is None:
        raise SecurityError("Header URL must be absolute HTTP(S)")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise SecurityError("Header URL userinfo is forbidden")
    host = normalize_host(parts.hostname)
    rejected = rejected_query_keys(parts.query)
    if rejected:
        raise SecurityError(f"credential/header query key rejected: {', '.join(rejected)}")
    if origin_only and (parts.path not in {"", "/"} or parts.query or parts.fragment):
        raise SecurityError("Origin must contain only scheme and authority")
    port = _url_port(scheme, explicit_port)
    path = "" if origin_only else parts.path
    value_out = urlunsplit((scheme, _format_netloc(host, port, scheme), path, parts.query, ""))
    return NormalizedUrl(value_out, scheme, host, port, path, parts.query)


def _load_header_json(value: str) -> Mapping[str, Any]:
    def no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise SecurityError(f"duplicate declared Header: {key}")
            result[key] = item
        return result

    try:
        loaded = json.loads(value, object_pairs_hook=no_duplicates)
    except (ValueError, TypeError) as exc:
        raise SecurityError("declared Header must be valid JSON object") from exc
    if not isinstance(loaded, Mapping):
        raise SecurityError("declared Header must be a JSON object")
    return loaded


def validate_declared_headers(value: Mapping[str, Any] | str) -> DeclaredHeaders:
    """Normalize and validate a source-declared Header mapping."""

    mapping = _load_header_json(value) if isinstance(value, str) else value
    if not isinstance(mapping, Mapping):
        raise SecurityError("declared Header must be an object")
    normalized: dict[str, str] = {}
    total_size = 0
    for raw_name, raw_value in mapping.items():
        if not isinstance(raw_name, str) or not isinstance(raw_value, str):
            raise SecurityError("Header names and values must be strings")
        if _BAD_HEADER_CONTROL_RE.search(raw_name) or _BAD_HEADER_CONTROL_RE.search(raw_value):
            raise SecurityError("Header names and values cannot contain CR/LF/NUL")
        canonical = _ALLOWED_DECLARED_HEADERS.get(raw_name.lower())
        if canonical is None:
            raise SecurityError(f"declared Header is not allowed: {raw_name}")
        if canonical in normalized:
            raise SecurityError(f"duplicate declared Header: {canonical}")
        encoded_value = raw_value.encode("utf-8")
        if len(encoded_value) > 1024:
            raise SecurityError(f"declared Header value is too large: {canonical}")
        total_size += len(canonical.encode("ascii")) + len(encoded_value)
        if total_size > 4096:
            raise SecurityError("declared Header block is too large")
        if canonical == "Referer":
            raw_value = validate_header_url(raw_value).value
        elif canonical == "Origin":
            raw_value = validate_header_url(raw_value, origin_only=True).value
        normalized[canonical] = raw_value
    return DeclaredHeaders(values=dict(sorted(normalized.items())))


def split_url_headers(value: str) -> tuple[str, DeclaredHeaders | None]:
    """Split common M3U ``URL|Header=Value&...`` syntax safely."""

    if "|" not in value:
        return value.strip(), None
    if value.count("|") != 1:
        raise SecurityError("malformed URL-line Header syntax")
    url, raw_headers = value.split("|", 1)
    if not url.strip() or not raw_headers:
        raise SecurityError("malformed URL-line Header syntax")
    pairs: dict[str, str] = {}
    for pair in raw_headers.split("&"):
        if not pair or "=" not in pair:
            raise SecurityError("malformed URL-line Header syntax")
        raw_name, raw_value = pair.split("=", 1)
        name = _strict_percent_decode(raw_name, what="Header name")
        header_value = _strict_percent_decode(raw_value, what="Header value")
        if name in pairs:
            raise SecurityError(f"duplicate declared Header: {name}")
        pairs[name] = header_value
    return url.strip(), validate_declared_headers(pairs)


def merge_declared_headers(*headers: DeclaredHeaders | None) -> DeclaredHeaders | None:
    """Merge independent M3U Header declarations, rejecting collisions."""

    merged: dict[str, str] = {}
    for declaration in headers:
        if declaration is None:
            continue
        for key, value in declaration.values.items():
            if key in merged:
                raise SecurityError(f"duplicate declared Header: {key}")
            merged[key] = value
    if not merged:
        return None
    return validate_declared_headers(merged)


def _is_public_ip(value: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(value, ipaddress.IPv6Address) and value.ipv4_mapped is not None:
        value = value.ipv4_mapped
    return bool(
        value.is_global
        and not value.is_private
        and not value.is_loopback
        and not value.is_link_local
        and not value.is_multicast
        and not value.is_reserved
        and not value.is_unspecified
    )


def reject_non_public_ip_literal(host: str) -> None:
    """Reject a private/special IP literal while leaving DNS names unresolved."""

    normalized_host = normalize_host(host)
    try:
        literal = ipaddress.ip_address(normalized_host)
    except ValueError:
        return
    if not _is_public_ip(literal):
        raise SecurityError("private or special-purpose address rejected")


def resolve_public_addresses(
    host: str,
    port: int,
    *,
    resolver: Resolver = socket.getaddrinfo,
) -> tuple[str, ...]:
    """Resolve once, reject mixed public/private answers, and return pinned IPs."""

    normalized_host = normalize_host(host)
    try:
        literal = ipaddress.ip_address(normalized_host)
    except ValueError:
        literal = None
    if literal is not None:
        if not _is_public_ip(literal):
            raise SecurityError("private or special-purpose address rejected")
        return (literal.compressed.lower(),)

    try:
        answers = resolver(normalized_host, port, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except OSError:
        raise
    addresses: set[str] = set()
    for family, _socktype, _proto, _canonname, sockaddr in answers:
        if family not in {socket.AF_INET, socket.AF_INET6} or not sockaddr:
            continue
        try:
            address = ipaddress.ip_address(str(sockaddr[0]))
        except ValueError as exc:
            raise SecurityError("resolver returned an invalid IP address") from exc
        if not _is_public_ip(address):
            raise SecurityError("private or special-purpose DNS answer rejected")
        addresses.add(address.compressed.lower())
    if not addresses:
        raise OSError(f"DNS returned no usable address for {normalized_host}")
    return tuple(sorted(addresses, key=lambda item: (ipaddress.ip_address(item).version, item)))


def validate_peer_address(peer_ip: str, approved_addresses: Iterable[str]) -> str:
    """Verify the connected socket peer belongs to the exact approved DNS snapshot."""

    try:
        peer = ipaddress.ip_address(peer_ip)
    except ValueError as exc:
        raise SecurityError("transport returned an invalid peer address") from exc
    if isinstance(peer, ipaddress.IPv6Address) and peer.ipv4_mapped is not None:
        peer = peer.ipv4_mapped
    approved = {ipaddress.ip_address(item) for item in approved_addresses}
    if peer not in approved or not _is_public_ip(peer):
        raise SecurityError("connected peer does not match approved public DNS answers")
    return peer.compressed.lower()


def redact_url(url: str) -> str:
    """Return a safe diagnostic URL with every query value redacted."""

    try:
        parts = urlsplit(url)
    except ValueError:
        return "<invalid-url>"
    try:
        host = parts.hostname
        port = parts.port
    except ValueError:
        return "<invalid-url>"
    if host is None:
        safe_netloc = "<invalid-host>"
    else:
        safe_host = f"[{host}]" if ":" in host else host
        safe_netloc = f"{safe_host}:{port}" if port is not None else safe_host
        if parts.username is not None or parts.password is not None:
            safe_netloc = f"<userinfo-redacted>@{safe_netloc}"
    pairs: list[str] = []
    for pair in parts.query.split("&") if parts.query else ():
        key = pair.split("=", 1)[0]
        pairs.append(f"{quote(key, safe='%')}=<redacted>")
    return urlunsplit((parts.scheme, safe_netloc, parts.path, "&".join(pairs), ""))
