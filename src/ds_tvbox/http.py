"""Bounded, redirect-aware HTTP GET client with DNS pinning and peer checks."""

from __future__ import annotations

import email.utils
import http.client
import multiprocessing
import socket
import ssl
import threading
import time
import zlib
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from typing import Any, Protocol, cast
from urllib.parse import urljoin

from .errors import FetchError, SecurityError
from .models import DeclaredHeaders, HttpExceptionSpec
from .security import (
    NormalizedUrl,
    Resolver,
    normalize_url,
    resolve_public_addresses,
    validate_declared_headers,
    validate_header_url,
    validate_peer_address,
)

USER_AGENT = "DS-TVBox/1.0 (+https://github.com/azhansy/ds-tvbox)"
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_DEFAULT_DNS_TIMEOUT_SECONDS = 5.0
_RESOLVER_PROCESS_LIMIT = 20
_RESOLVER_SLOTS = threading.BoundedSemaphore(_RESOLVER_PROCESS_LIMIT)
_RESOLVER_CLEANUP_RESERVE_SECONDS = 0.05
_ORPHANED_RESOLVERS: list[BaseProcess] = []
_ORPHANED_RESOLVERS_LOCK = threading.Lock()

ResolverAnswer = tuple[int, int, int, str, tuple[Any, ...]]


def _reap_orphaned_resolvers() -> None:
    """Reap previously killed workers without ever exceeding the process cap."""

    with _ORPHANED_RESOLVERS_LOCK:
        survivors: list[BaseProcess] = []
        for process in _ORPHANED_RESOLVERS:
            if process.is_alive():
                survivors.append(process)
                continue
            process.join(timeout=0)
            process.close()
            _RESOLVER_SLOTS.release()
        _ORPHANED_RESOLVERS[:] = survivors


def _resolver_process_context(resolver: Resolver) -> Any:
    methods = multiprocessing.get_all_start_methods()
    # Production uses a clean fork-server so collector worker threads never fork
    # their own lock-bearing address space.  Local injected resolver fixtures may
    # be closures and therefore need POSIX fork; a stuck child is still isolated
    # and killed at the deadline without blocking its parent thread.
    if resolver is socket.getaddrinfo and "forkserver" in methods:
        return multiprocessing.get_context("forkserver")
    return multiprocessing.get_context("fork" if "fork" in methods else "spawn")


def _resolver_process_main(connection: Connection, resolver: Resolver) -> None:
    """Serve bounded raw resolver calls inside a disposable child process."""

    try:
        while True:
            try:
                message = connection.recv()
            except EOFError:
                return
            if message is None:
                return
            request_id, host, port, family, socktype = cast(
                tuple[int, str, int, int, int], message
            )
            try:
                answers = tuple(resolver(host, port, family, socktype))
                if len(answers) > 256:
                    raise OSError("DNS returned too many answers")
                response: tuple[int, str, object] = (request_id, "ok", answers)
            except OSError as exc:
                response = (request_id, "os_error", str(exc))
            except Exception as exc:  # pragma: no cover - defensive child boundary
                response = (
                    request_id,
                    "resolver_error",
                    f"{type(exc).__name__}: {exc}",
                )
            try:
                connection.send(response)
            except (BrokenPipeError, EOFError, OSError):
                return
    finally:
        connection.close()


class _ResolverProcessSession:
    """One lazily started, restartable resolver process for a single fetch.

    A request ID binds every answer to exactly one call.  A timed-out child is
    terminated before another request can start, so delayed answers cannot be
    consumed by a later host.  The global semaphore keeps process use bounded
    even when many collector threads enter DNS at once.
    """

    def __init__(self, resolver: Resolver, timeout: float) -> None:
        self._resolver = resolver
        self._timeout = timeout
        self._process: BaseProcess | None = None
        self._connection: Connection | None = None
        self._slot_held = False
        self._next_request_id = 1

    def __enter__(self) -> _ResolverProcessSession:
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: object,
    ) -> None:
        self.close()

    def _start(self, deadline: float) -> None:
        if self._process is not None:
            return
        _reap_orphaned_resolvers()
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not _RESOLVER_SLOTS.acquire(timeout=remaining):
            raise TimeoutError("DNS resolver capacity deadline exceeded")
        self._slot_held = True
        parent_connection: Connection | None = None
        child_connection: Connection | None = None
        try:
            context = _resolver_process_context(self._resolver)
            parent_connection, child_connection = context.Pipe(duplex=True)
            process: BaseProcess = context.Process(
                target=_resolver_process_main,
                args=(child_connection, self._resolver),
                name="ds-tvbox-resolver",
                daemon=True,
            )
            process.start()
        except BaseException:
            if parent_connection is not None:
                parent_connection.close()
            if child_connection is not None:
                child_connection.close()
            self._release_slot()
            raise
        assert parent_connection is not None and child_connection is not None
        child_connection.close()
        self._process = process
        self._connection = parent_connection
        if time.monotonic() >= deadline:
            self._stop(force=True, deadline=deadline)
            raise TimeoutError("DNS resolver startup deadline exceeded")

    def _release_slot(self) -> None:
        if self._slot_held:
            self._slot_held = False
            _RESOLVER_SLOTS.release()

    def _stop(self, *, force: bool, deadline: float | None = None) -> None:
        process = self._process
        connection = self._connection
        self._process = None
        self._connection = None
        slot_transferred = False
        try:
            if connection is not None and not force and process is not None and process.is_alive():
                with suppress(BrokenPipeError, EOFError, OSError):
                    connection.send(None)
            if connection is not None:
                connection.close()
            if process is not None:
                if force and process.is_alive():
                    process.terminate()
                wait = (
                    max(0.0, deadline - time.monotonic())
                    if deadline is not None
                    else 0.2
                )
                process.join(timeout=wait)
                if process.is_alive():
                    process.kill()
                    process.join(timeout=_RESOLVER_CLEANUP_RESERVE_SECONDS)
                if process.is_alive():
                    with _ORPHANED_RESOLVERS_LOCK:
                        _ORPHANED_RESOLVERS.append(process)
                    self._slot_held = False
                    slot_transferred = True
                else:
                    process.close()
        finally:
            if not slot_transferred:
                self._release_slot()

    def close(self) -> None:
        self._stop(force=False)

    def __call__(
        self,
        host: str,
        port: int,
        family: int = socket.AF_UNSPEC,
        socktype: int = socket.SOCK_STREAM,
    ) -> Sequence[ResolverAnswer]:
        started = time.monotonic()
        deadline = started + self._timeout
        self._start(deadline)
        connection = self._connection
        if connection is None:  # pragma: no cover - guarded by _start
            raise OSError("DNS resolver worker did not start")
        request_id = self._next_request_id
        self._next_request_id += 1
        try:
            connection.send((request_id, host, port, family, socktype))
        except (BrokenPipeError, EOFError, OSError) as exc:
            self._stop(force=True, deadline=deadline)
            raise OSError("DNS resolver worker communication failed") from exc

        remaining = deadline - time.monotonic()
        cleanup_reserve = min(_RESOLVER_CLEANUP_RESERVE_SECONDS, self._timeout / 2)
        if remaining <= 0 or not connection.poll(max(0.0, remaining - cleanup_reserve)):
            self._stop(force=True, deadline=deadline)
            raise TimeoutError(f"DNS resolution timed out for {host}")
        try:
            response = connection.recv()
        except (EOFError, OSError) as exc:
            self._stop(force=True, deadline=deadline)
            raise OSError("DNS resolver worker exited without a result") from exc
        if not isinstance(response, tuple) or len(response) != 3:
            self._stop(force=True, deadline=deadline)
            raise OSError("DNS resolver worker returned an invalid result")
        response_id, status, payload = response
        if response_id != request_id:
            self._stop(force=True, deadline=deadline)
            raise OSError("DNS resolver response identity mismatch")
        if status == "os_error":
            raise OSError(str(payload))
        if status != "ok":
            raise OSError(f"DNS resolver failed: {payload}")
        if not isinstance(payload, tuple):
            raise OSError("DNS resolver worker returned invalid answers")
        return cast(tuple[ResolverAnswer, ...], payload)


def resolve_public_addresses_with_deadline(
    host: str,
    port: int,
    *,
    resolver: Resolver = socket.getaddrinfo,
    timeout: float = _DEFAULT_DNS_TIMEOUT_SECONDS,
) -> tuple[str, ...]:
    """Resolve one public host through the same killable DNS boundary as HTTP."""

    if timeout <= 0:
        raise ValueError("DNS timeout must be positive")
    with _ResolverProcessSession(resolver, timeout) as bounded_resolver:
        return resolve_public_addresses(host, port, resolver=bounded_resolver)


class _PermanentFetchError(FetchError):
    """A response contract/size failure that must never be retried."""


@dataclass(frozen=True)
class HttpRequest:
    url: str
    allowed_hosts: frozenset[str]
    allow_discovered_host: bool = False
    http_exceptions: tuple[HttpExceptionSpec, ...] = ()
    client_visible: bool = False
    declared_headers: DeclaredHeaders | None = None
    max_bytes: int = 5 * 1024 * 1024
    connect_timeout: float = 5.0
    read_timeout: float = 15.0
    max_redirects: int = 5
    max_attempts: int = 3
    internal_range: tuple[int, int] | None = None

    def __post_init__(self) -> None:
        if self.max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        if self.connect_timeout <= 0 or self.read_timeout <= 0:
            raise ValueError("timeouts must be positive")
        if not 0 <= self.max_redirects <= 5:
            raise ValueError("max_redirects must be within 0..5")
        if not 1 <= self.max_attempts <= 3:
            raise ValueError("max_attempts must be within 1..3")
        if self.internal_range is not None:
            start, end = self.internal_range
            if start < 0 or end < start or end - start + 1 > 1024 * 1024:
                raise ValueError("internal Range must be a positive span no larger than 1 MiB")


@dataclass(frozen=True)
class TransportResponse:
    status: int
    headers: tuple[tuple[str, str], ...]
    body: bytes
    peer_ip: str
    raw_bytes_read: int

    def header(self, name: str) -> str | None:
        lowered = name.lower()
        values = [value for key, value in self.headers if key.lower() == lowered]
        if not values:
            return None
        if len(values) != 1:
            raise FetchError(f"multiple {name} response headers are not supported")
        return values[0]


@dataclass(frozen=True)
class HttpResponse:
    status: int
    final_url: str
    headers: tuple[tuple[str, str], ...]
    body: bytes
    elapsed_ms: int
    attempts: int
    redirects: int

    def header(self, name: str) -> str | None:
        lowered = name.lower()
        values = [value for key, value in self.headers if key.lower() == lowered]
        if not values:
            return None
        return values[-1]


class HttpTransport(Protocol):
    """A connect-to-IP transport.  Implementations must not resolve ``host``."""

    def request(
        self,
        *,
        target: NormalizedUrl,
        connect_ip: str,
        headers: Mapping[str, str],
        connect_timeout: float,
        read_timeout: float,
        max_bytes: int,
    ) -> TransportResponse: ...


class ByteBudget:
    """Thread-safe aggregate network byte budget."""

    def __init__(self, limit: int = 200 * 1024 * 1024) -> None:
        if limit <= 0:
            raise ValueError("byte budget must be positive")
        self.limit = limit
        self._used = 0
        self._lock = threading.Lock()

    @property
    def used(self) -> int:
        with self._lock:
            return self._used

    def consume(self, amount: int) -> None:
        if amount < 0:
            raise ValueError("byte count cannot be negative")
        with self._lock:
            if self._used + amount > self.limit:
                raise _PermanentFetchError("global external-data byte budget exceeded")
            self._used += amount


class ConcurrencyLimits:
    """Shared global/per-host connection limits for one collector run."""

    def __init__(self, global_limit: int = 20, per_host_limit: int = 4) -> None:
        if global_limit <= 0 or per_host_limit <= 0 or per_host_limit > global_limit:
            raise ValueError("invalid network concurrency limits")
        self._global = threading.BoundedSemaphore(global_limit)
        self._per_host_limit = per_host_limit
        self._hosts: dict[str, threading.BoundedSemaphore] = {}
        self._host_lock = threading.Lock()

    @contextmanager
    def slot(self, host: str) -> Iterator[None]:
        with self._host_lock:
            host_limit = self._hosts.setdefault(
                host, threading.BoundedSemaphore(self._per_host_limit)
            )
        host_limit.acquire()
        try:
            self._global.acquire()
            try:
                yield
            finally:
                self._global.release()
        finally:
            host_limit.release()


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, hostname: str, connect_ip: str, port: int, timeout: float) -> None:
        self._connect_ip = connect_ip
        super().__init__(hostname, port=port, timeout=timeout)

    def connect(self) -> None:
        self.sock = socket.create_connection((self._connect_ip, self.port), self.timeout)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(
        self,
        hostname: str,
        connect_ip: str,
        port: int,
        timeout: float,
        context: ssl.SSLContext,
    ) -> None:
        self._connect_ip = connect_ip
        self._verified_context = context
        super().__init__(hostname, port=port, timeout=timeout, context=context)

    def connect(self) -> None:
        raw_socket = socket.create_connection((self._connect_ip, self.port), self.timeout)
        try:
            self.sock = self._verified_context.wrap_socket(raw_socket, server_hostname=self.host)
        except BaseException:
            raw_socket.close()
            raise


def _decode_response_stream(
    response: http.client.HTTPResponse,
    *,
    max_bytes: int,
) -> tuple[bytes, int]:
    content_length = response.getheader("Content-Length")
    if content_length is not None:
        try:
            length = int(content_length)
        except ValueError as exc:
            raise _PermanentFetchError("invalid Content-Length") from exc
        if length < 0 or length > max_bytes:
            raise _PermanentFetchError("response body exceeds configured limit")

    encoding = (response.getheader("Content-Encoding") or "identity").strip().lower()
    if encoding in {"", "identity"}:
        decoder: Any | None = None
    elif encoding == "gzip":
        decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
    elif encoding == "deflate":
        decoder = zlib.decompressobj()
    else:
        raise _PermanentFetchError(f"unsupported Content-Encoding: {encoding}")

    raw_count = 0
    decoded = bytearray()
    try:
        while True:
            chunk = response.read(min(64 * 1024, max_bytes + 1))
            if not chunk:
                break
            raw_count += len(chunk)
            if raw_count > max_bytes:
                raise _PermanentFetchError("response transfer exceeds configured limit")
            if decoder is None:
                decoded.extend(chunk)
            else:
                remaining = max_bytes - len(decoded) + 1
                decoded.extend(decoder.decompress(chunk, max(remaining, 1)))
                if decoder.unconsumed_tail:
                    raise _PermanentFetchError("decoded response body exceeds configured limit")
            if len(decoded) > max_bytes:
                raise _PermanentFetchError("decoded response body exceeds configured limit")
        if decoder is not None:
            remaining = max_bytes - len(decoded) + 1
            decoded.extend(decoder.flush(max(remaining, 1)))
            if not decoder.eof:
                raise _PermanentFetchError("truncated compressed response body")
    except zlib.error as exc:
        raise _PermanentFetchError("invalid compressed response body") from exc
    if len(decoded) > max_bytes:
        raise _PermanentFetchError("decoded response body exceeds configured limit")
    return bytes(decoded), raw_count


class PinnedHttpTransport:
    """Production HTTP/1.1 transport that connects only to an approved IP."""

    def __init__(self, ssl_context: ssl.SSLContext | None = None) -> None:
        self._ssl_context = ssl_context or ssl.create_default_context()

    def request(
        self,
        *,
        target: NormalizedUrl,
        connect_ip: str,
        headers: Mapping[str, str],
        connect_timeout: float,
        read_timeout: float,
        max_bytes: int,
    ) -> TransportResponse:
        connection: http.client.HTTPConnection
        if target.scheme == "https":
            connection = _PinnedHTTPSConnection(
                target.host,
                connect_ip,
                target.port,
                connect_timeout,
                self._ssl_context,
            )
        else:
            connection = _PinnedHTTPConnection(
                target.host,
                connect_ip,
                target.port,
                connect_timeout,
            )
        try:
            connection.request("GET", target.request_target, headers=dict(headers))
            if connection.sock is None:  # pragma: no cover - defensive stdlib guard
                raise FetchError("transport did not expose a connected socket")
            connection.sock.settimeout(read_timeout)
            peer_ip = str(connection.sock.getpeername()[0])
            response = connection.getresponse()
            body, raw_count = _decode_response_stream(response, max_bytes=max_bytes)
            response_headers = tuple((key, value) for key, value in response.getheaders())
            return TransportResponse(
                status=response.status,
                headers=response_headers,
                body=body,
                peer_ip=peer_ip,
                raw_bytes_read=raw_count,
            )
        finally:
            connection.close()


def _parse_retry_after(value: str | None, now: Callable[[], datetime]) -> float | None:
    if value is None:
        return None
    stripped = value.strip()
    if stripped.isdigit():
        return float(int(stripped))
    try:
        parsed = email.utils.parsedate_to_datetime(stripped)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return max(0.0, (parsed - now()).total_seconds())


class SafeHttpClient:
    """Synchronous bounded GET client suitable for execution in worker threads."""

    def __init__(
        self,
        *,
        transport: HttpTransport | None = None,
        resolver: Resolver = socket.getaddrinfo,
        dns_timeout: float = _DEFAULT_DNS_TIMEOUT_SECONDS,
        sleeper: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] | None = None,
        budget: ByteBudget | None = None,
        concurrency: ConcurrencyLimits | None = None,
    ) -> None:
        if dns_timeout <= 0:
            raise ValueError("DNS timeout must be positive")
        self._transport = transport or PinnedHttpTransport()
        self._resolver = resolver
        self._dns_timeout = dns_timeout
        self._sleeper = sleeper
        self._now = now or (lambda: datetime.now(UTC))
        self._budget = budget or ByteBudget()
        self._concurrency = concurrency or ConcurrencyLimits()

    def _headers(self, request: HttpRequest) -> dict[str, str]:
        values = {
            "User-Agent": USER_AGENT,
            "Accept": "*/*",
            "Accept-Encoding": "gzip, deflate",
            "Connection": "close",
        }
        if request.declared_headers is not None:
            values.update(validate_declared_headers(request.declared_headers.values).values)
        if request.internal_range is not None:
            start, end = request.internal_range
            values["Range"] = f"bytes={start}-{end}"
        return values

    @staticmethod
    def _resolve_public(
        host: str,
        port: int,
        resolver: Resolver,
    ) -> tuple[str, ...]:
        try:
            return resolve_public_addresses(host, port, resolver=resolver)
        except SecurityError:
            raise
        except OSError as exc:
            raise FetchError(f"DNS resolution failed for {host}") from exc

    def _validate_header_destinations(
        self,
        request: HttpRequest,
        resolver: Resolver,
    ) -> None:
        if request.declared_headers is None:
            return
        for name in ("Referer", "Origin"):
            value = request.declared_headers.values.get(name)
            if value is None:
                continue
            target = validate_header_url(value, origin_only=name == "Origin")
            self._resolve_public(target.host, target.port, resolver)

    def _request_one_hop(
        self,
        request: HttpRequest,
        target: NormalizedUrl,
        resolver: Resolver,
    ) -> TransportResponse:
        approved = self._resolve_public(target.host, target.port, resolver)
        last_error: BaseException | None = None
        for connect_ip in approved:
            try:
                with self._concurrency.slot(target.host):
                    result = self._transport.request(
                        target=target,
                        connect_ip=connect_ip,
                        headers=self._headers(request),
                        connect_timeout=request.connect_timeout,
                        read_timeout=request.read_timeout,
                        max_bytes=request.max_bytes,
                    )
                validate_peer_address(result.peer_ip, approved)
                if (
                    len(result.body) > request.max_bytes
                    or result.raw_bytes_read > request.max_bytes
                ):
                    raise _PermanentFetchError("transport returned an over-limit response")
                self._budget.consume(result.raw_bytes_read)
                return result
            except SecurityError:
                raise
            except _PermanentFetchError:
                raise
            except ssl.SSLCertVerificationError as exc:
                raise _PermanentFetchError("TLS certificate verification failed") from exc
            except (
                FetchError,
                OSError,
                TimeoutError,
                ssl.SSLError,
                http.client.HTTPException,
            ) as exc:
                last_error = exc
        raise FetchError(f"all approved addresses failed for {target.host}") from last_error

    def _follow_redirects(
        self,
        request: HttpRequest,
        resolver: Resolver,
    ) -> tuple[TransportResponse, NormalizedUrl, int]:
        current_url = request.url
        seen: set[str] = set()
        for redirects in range(request.max_redirects + 1):
            target = normalize_url(
                current_url,
                allowed_hosts=request.allowed_hosts,
                allow_discovered_host=request.allow_discovered_host,
                http_exceptions=request.http_exceptions,
                client_visible=request.client_visible,
            )
            if target.value in seen:
                raise FetchError("redirect loop detected")
            seen.add(target.value)
            response = self._request_one_hop(request, target, resolver)
            if response.status not in _REDIRECT_STATUSES:
                return response, target, redirects
            location = response.header("Location")
            if not location:
                raise FetchError("redirect response has no unique Location")
            if redirects >= request.max_redirects:
                raise FetchError("maximum redirects exceeded")
            current_url = urljoin(target.value, location)
        raise FetchError("maximum redirects exceeded")  # pragma: no cover

    def fetch(self, request: HttpRequest) -> HttpResponse:
        """Perform a safe GET with at most three bounded transient retries."""

        if request.declared_headers is not None:
            validate_declared_headers(request.declared_headers.values)
        with _ResolverProcessSession(self._resolver, self._dns_timeout) as resolver:
            self._validate_header_destinations(request, resolver)
            started = time.monotonic()
            last_error: FetchError | None = None
            total_redirects = 0
            for attempt in range(1, request.max_attempts + 1):
                try:
                    response, target, redirects = self._follow_redirects(request, resolver)
                    total_redirects += redirects
                except SecurityError:
                    raise
                except _PermanentFetchError:
                    raise
                except FetchError as exc:
                    last_error = exc
                    if attempt == request.max_attempts:
                        raise
                    self._sleeper(float(2 ** (attempt - 1)))
                    continue
                is_transient = response.status in {408, 429} or 500 <= response.status <= 599
                if not is_transient or attempt == request.max_attempts:
                    return HttpResponse(
                        status=response.status,
                        final_url=target.value,
                        headers=response.headers,
                        body=response.body,
                        elapsed_ms=max(0, round((time.monotonic() - started) * 1000)),
                        attempts=attempt,
                        redirects=total_redirects,
                    )
                retry_after = _parse_retry_after(response.header("Retry-After"), self._now)
                delay = retry_after if retry_after is not None else float(2 ** (attempt - 1))
                # Do not violate a very long Retry-After by retrying early.  Returning
                # the transient response lets the state machine classify it as suspect.
                if delay > 60:
                    return HttpResponse(
                        status=response.status,
                        final_url=target.value,
                        headers=response.headers,
                        body=response.body,
                        elapsed_ms=max(0, round((time.monotonic() - started) * 1000)),
                        attempts=attempt,
                        redirects=total_redirects,
                    )
                self._sleeper(delay)
            assert last_error is not None  # pragma: no cover
            raise last_error
