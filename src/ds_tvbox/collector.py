"""End-to-end collection orchestration over the safe transport boundary."""

from __future__ import annotations

import hashlib
import queue
import re
import socket
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from .catalog import CatalogScanResult, scan_catalog
from .errors import ContractError, FetchError, SecurityError
from .health import aggregate_technical_status, build_health_document, vod_entity_id
from .http import HttpRequest, resolve_public_addresses_with_deadline
from .http import HttpResponse as SafeHttpResponse
from .live import (
    live_url_id,
    looks_like_media_payload,
    probe_hls,
    probe_live,
    select_channels,
)
from .models import (
    DeclaredHeaders,
    FailureReason,
    LiveCandidate,
    LiveProbeResult,
    MediaProbeResult,
    RightsStatus,
    SelectedChannel,
    SourceKind,
    SourceSpec,
    TechnicalStatus,
    VodCapabilities,
    VodProbeResult,
    VodSiteCandidate,
)
from .parsers import parse_m3u, parse_tvbox_config, parse_txt_live
from .policy import (
    prioritize_failure_reasons,
    publication_status_for,
    technical_status_for_failure,
)
from .security import normalize_url, validate_declared_headers
from .upstream import Fetcher, UpstreamFailure, resolve_upstream
from .vod import (
    HttpResponse as ProbeHttpResponse,
)
from .vod import (
    ProbeRequestError,
    parse_maccms_document,
    probe_vod,
)

_RANGE = re.compile(r"^bytes=([0-9]+)-([0-9]+)$")
_EXECUTABLE = re.compile(r"(?i)\.(?:jar|js|py|dex|so)(?:$|[?#])")
_DNS_NAME_TIMEOUT_SECONDS = 5.0
_NETWORK_OBSERVATION_WINDOW_SECONDS = 60.0


@dataclass(frozen=True)
class SourceObservation:
    source_id: str
    fetch_mode: str
    resolved_revision: str | None
    resolved_fetch_url: str | None
    content_sha256: str | None
    terms_sha256: Mapping[str, str]
    technical_status: TechnicalStatus
    failure_reason: FailureReason | None
    secondary_reasons: tuple[FailureReason, ...]
    enumerated: bool


@dataclass(frozen=True)
class DiscardedEntity:
    source_id: str
    entity_kind: str
    target_hash: str
    failure_reason: FailureReason


@dataclass(frozen=True)
class NetworkProbeObservation:
    group: str
    passed: bool
    attempts: int
    elapsed_ms: int
    detail: str


@dataclass(frozen=True)
class CollectResult:
    checked_at: str
    sources: tuple[SourceSpec, ...]
    vod_results: tuple[VodProbeResult, ...]
    live_results: tuple[LiveProbeResult, ...]
    selected_channels: tuple[SelectedChannel, ...]
    source_observations: tuple[SourceObservation, ...]
    catalog_results: tuple[CatalogScanResult, ...]
    discarded_entities: tuple[DiscardedEntity, ...]
    upstream_revisions: Mapping[str, str | None]
    source_failures: Mapping[str, tuple[TechnicalStatus, FailureReason | None]]
    enumerated_source_ids: frozenset[str]
    network_probes: tuple[NetworkProbeObservation, ...]
    failed_network_groups: int

    def build_health(
        self,
        *,
        generation: int,
        release_id: str,
        previous_health: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Build the four-layer health graph from this immutable observation."""

        return build_health_document(
            generated_at=self.checked_at,
            generation=generation,
            release_id=release_id,
            sources=self.sources,
            vod_results=self.vod_results,
            live_results=self.live_results,
            selected_channels=self.selected_channels,
            upstream_revisions=self.upstream_revisions,
            previous_health=previous_health,
            source_failures=self.source_failures,
            enumerated_source_ids=self.enumerated_source_ids,
        )

    def candidates_report(
        self,
        *,
        workflow_run_id: str,
        workflow_run_attempt: int,
    ) -> dict[str, object]:
        """Return the artifact-only candidate report; it is never a bundle input."""

        candidates = [
            candidate.as_report()
            for result in self.catalog_results
            for candidate in result.candidates
        ]
        candidates.sort(key=lambda item: str(item["candidate_id"]))
        catalogs: list[dict[str, object]] = []
        for result in sorted(self.catalog_results, key=lambda item: item.source_id):
            parent = result.as_report()
            parent.pop("candidates")
            catalogs.append(parent)
        return {
            "schema_version": "1.0.0",
            "workflow_run_id": workflow_run_id,
            "workflow_run_attempt": workflow_run_attempt,
            "catalogs": catalogs,
            "candidates": candidates,
        }


def _reason_for_exception(exc: BaseException) -> FailureReason:
    message = str(exc).casefold()
    if isinstance(exc, SecurityError):
        if "credential/header query" in message or "query key" in message:
            return FailureReason.CREDENTIAL_QUERY_REJECTED
        if "header" in message and ("syntax" in message or "control" in message):
            return FailureReason.INVALID_HEADER_SYNTAX
        if "header" in message:
            return FailureReason.CREDENTIAL_HEADER_REJECTED
        if "credential" in message:
            return FailureReason.CREDENTIAL_QUERY_REJECTED
        if "private" in message or "special-purpose" in message or "peer" in message:
            return FailureReason.PRIVATE_ADDRESS_REJECTED
        if "scheme" in message:
            return FailureReason.DANGEROUS_SCHEME_REJECTED
        if "http" in message:
            return FailureReason.CLIENT_HTTP_DISALLOWED
        return FailureReason.PRIVATE_ADDRESS_REJECTED
    if "tls" in message or "certificate" in message or "ssl" in message:
        return FailureReason.TLS_FAILURE
    if any(
        marker in message
        for marker in (
            "dns",
            "getaddrinfo",
            "name or service not known",
            "nodename nor servname",
            "temporary failure in name resolution",
        )
    ):
        return FailureReason.DNS_FAILURE
    if "large" in message or "limit" in message or "budget" in message:
        return FailureReason.RESPONSE_TOO_LARGE
    if "timeout" in message or "timed out" in message:
        return FailureReason.FETCH_TIMEOUT
    return FailureReason.FETCH_TIMEOUT


class ProbeHttpAdapter:
    """Adapt ``SafeHttpClient`` to VOD/live protocols without exposing Range.

    ``live.probe_hls`` expresses its bounded segment read through the narrow
    protocol's Header mapping.  This adapter consumes that exact internal Range
    and sets ``HttpRequest.internal_range``; it is never placed into the
    source-declared Header set.
    """

    def __init__(self, client: Fetcher, source: SourceSpec) -> None:
        self._client = client
        self._source = source

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        max_bytes: int | None = None,
    ) -> ProbeHttpResponse:
        declared: dict[str, str] = {}
        internal_range: tuple[int, int] | None = None
        seen_names: set[str] = set()
        for name, value in (headers or {}).items():
            lowered = name.casefold()
            if lowered in seen_names:
                raise ProbeRequestError(FailureReason.INVALID_HEADER_SYNTAX)
            seen_names.add(lowered)
            if lowered == "range":
                match = _RANGE.fullmatch(value)
                if match is None:
                    raise ProbeRequestError(FailureReason.INVALID_HEADER_SYNTAX)
                start, end = int(match.group(1)), int(match.group(2))
                if start < 0 or end < start or end - start + 1 > 1024 * 1024:
                    raise ProbeRequestError(FailureReason.INVALID_HEADER_SYNTAX)
                internal_range = (start, end)
            else:
                declared[name] = value
        try:
            declared_headers: DeclaredHeaders | None = (
                validate_declared_headers(declared) if declared else None
            )
            response = self._client.fetch(
                HttpRequest(
                    url=url,
                    allowed_hosts=self._source.allowed_hosts,
                    allow_discovered_host=self._source.allow_discovered_media_hosts,
                    http_exceptions=self._source.http_exceptions,
                    client_visible=True,
                    declared_headers=declared_headers,
                    max_bytes=max_bytes or 5 * 1024 * 1024,
                    internal_range=internal_range,
                )
            )
        except ProbeRequestError:
            raise
        except (FetchError, SecurityError, OSError, TimeoutError) as exc:
            raise ProbeRequestError(_reason_for_exception(exc)) from exc
        response_headers = dict(response.headers)
        return ProbeHttpResponse(
            status_code=response.status,
            body=response.body,
            final_url=response.final_url,
            elapsed_ms=response.elapsed_ms,
            headers=response_headers,
        )


class SafeMediaProber:
    def __init__(self, adapter: ProbeHttpAdapter) -> None:
        self._adapter = adapter

    def probe(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> MediaProbeResult:
        split = urlsplit(url)
        if split.path.casefold().endswith((".m3u8", ".m3u")):
            return probe_hls(url, self._adapter, headers=headers)
        range_headers = dict(headers or {})
        range_headers["Range"] = "bytes=0-1048575"
        try:
            response = self._adapter.get(
                url,
                headers=range_headers,
                max_bytes=1024 * 1024,
            )
        except ProbeRequestError as exc:
            return MediaProbeResult(
                ok=False,
                final_url=None,
                response_ms=None,
                media_path_score=0,
                failure_reason=exc.reason,
            )
        if not 200 <= response.status_code <= 299:
            status_reason = _media_http_reason(response.status_code)
            return MediaProbeResult(
                ok=False,
                final_url=response.final_url,
                response_ms=response.elapsed_ms,
                media_path_score=0,
                failure_reason=status_reason,
            )
        if not response.body:
            return MediaProbeResult(
                ok=False,
                final_url=response.final_url,
                response_ms=response.elapsed_ms,
                media_path_score=0,
                failure_reason=FailureReason.MEDIA_PROBE_FAILED,
            )
        if response.body.lstrip().startswith(b"#EXTM3U"):
            return probe_hls(url, self._adapter, headers=headers)
        if not looks_like_media_payload(response.body):
            return MediaProbeResult(
                ok=False,
                final_url=response.final_url,
                response_ms=response.elapsed_ms,
                media_path_score=0,
                failure_reason=FailureReason.MEDIA_PROBE_FAILED,
            )
        return MediaProbeResult(
            ok=True,
            final_url=response.final_url,
            response_ms=response.elapsed_ms,
            media_path_score=1,
        )


def _media_http_reason(status: int) -> FailureReason:
    if status in {401, 403}:
        return FailureReason.CREDENTIAL_REQUIRED
    if status == 404:
        return FailureReason.HTTP_404
    if status == 410:
        return FailureReason.HTTP_410
    if status == 429:
        return FailureReason.RATE_LIMITED
    if 500 <= status <= 599:
        return FailureReason.UPSTREAM_5XX
    return FailureReason.MEDIA_PROBE_FAILED


def _network_http_probe(
    client: Fetcher,
    *,
    group: str,
    url: str,
    allowed_host: str,
    expected: Callable[[SafeHttpResponse], bool],
    max_bytes: int,
    sleeper: Callable[[float], None],
) -> NetworkProbeObservation:
    started = time.monotonic()
    detail = "unexpected_response"
    for attempt in range(1, 3):
        try:
            response = client.fetch(
                HttpRequest(
                    url=url,
                    allowed_hosts=frozenset({allowed_host}),
                    max_bytes=max_bytes,
                    connect_timeout=3.0,
                    read_timeout=2.0,
                    max_redirects=2,
                    max_attempts=1,
                )
            )
            if expected(response):
                return NetworkProbeObservation(
                    group=group,
                    passed=True,
                    attempts=attempt,
                    elapsed_ms=max(0, round((time.monotonic() - started) * 1000)),
                    detail="ok",
                )
        except (FetchError, SecurityError, OSError, TimeoutError) as exc:
            detail = _reason_for_exception(exc).value
        if attempt == 1:
            sleeper(2.0)
    return NetworkProbeObservation(
        group=group,
        passed=False,
        attempts=2,
        elapsed_ms=max(0, round((time.monotonic() - started) * 1000)),
        detail=detail,
    )


def _network_dns_probe(
    resolver: Callable[..., Sequence[tuple[int, int, int, str, tuple[Any, ...]]]],
) -> NetworkProbeObservation:
    """Resolve both fixed names concurrently without trusting resolver timeouts.

    ``socket.getaddrinfo`` has no portable timeout argument and may remain blocked
    below Python.  Each call therefore uses the same killable resolver-process
    boundary as ``SafeHttpClient``.  The two lightweight coordinator threads are
    both observed against one hard five-second deadline and cannot outlive a
    blocked native resolver indefinitely.
    """

    started = time.monotonic()
    results: queue.Queue[bool | Exception] = queue.Queue()

    def resolve_name(name: str) -> None:
        try:
            results.put(
                bool(
                    resolve_public_addresses_with_deadline(
                        name,
                        443,
                        resolver=resolver,
                        timeout=_DNS_NAME_TIMEOUT_SECONDS,
                    )
                )
            )
        except (OSError, SecurityError):
            results.put(False)
        except Exception as exc:  # pragma: no cover - defensive propagation
            results.put(exc)

    for index, name in enumerate(("one.one.one.one", "dns.google"), start=1):
        threading.Thread(
            target=resolve_name,
            args=(name,),
            name=f"network-probe-dns-name-{index}",
            daemon=True,
        ).start()

    deadline = started + _DNS_NAME_TIMEOUT_SECONDS
    successes = 0
    completed = 0
    while completed < 2:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            outcome = results.get(timeout=remaining)
        except queue.Empty:
            break
        completed += 1
        if isinstance(outcome, Exception):
            raise outcome
        successes += int(outcome)
    return NetworkProbeObservation(
        group="dns_public",
        passed=successes >= 1,
        attempts=2,
        elapsed_ms=max(0, round((time.monotonic() - started) * 1000)),
        detail="ok" if successes else "dns_failure",
    )


def run_public_network_probes(
    client: Fetcher,
    *,
    resolver: Callable[
        ..., Sequence[tuple[int, int, int, str, tuple[Any, ...]]]
    ] = socket.getaddrinfo,
    sleeper: Callable[[float], None] = time.sleep,
) -> tuple[NetworkProbeObservation, ...]:
    """Run the four fixed SPEC 14.1 groups in one concurrent observation window."""

    jobs: tuple[tuple[str, Callable[[], NetworkProbeObservation]], ...] = (
        (
            "github_raw",
            lambda: _network_http_probe(
                client,
                group="github_raw",
                url=(
                    "https://raw.githubusercontent.com/FongMi/TV/"
                    "5fdff00a602dc56e8ba756174daef20edab024f2/docs/CONFIG.md"
                ),
                allowed_host="raw.githubusercontent.com",
                expected=lambda response: (
                    response.status == 200 and 1 <= len(response.body) <= 1024 * 1024
                ),
                max_bytes=1024 * 1024,
                sleeper=sleeper,
            ),
        ),
        ("dns_public", lambda: _network_dns_probe(resolver)),
        (
            "cloudflare_http",
            lambda: _network_http_probe(
                client,
                group="cloudflare_http",
                url="https://www.cloudflare.com/cdn-cgi/trace",
                allowed_host="www.cloudflare.com",
                expected=lambda response: (
                    response.status == 200
                    and len(response.body) <= 64 * 1024
                    and re.search(rb"(?m)^ip=.+$", response.body) is not None
                ),
                max_bytes=64 * 1024,
                sleeper=sleeper,
            ),
        ),
        (
            "google_http",
            lambda: _network_http_probe(
                client,
                group="google_http",
                url="https://connectivitycheck.gstatic.com/generate_204",
                allowed_host="connectivitycheck.gstatic.com",
                expected=lambda response: response.status == 204 and response.body == b"",
                max_bytes=1,
                sleeper=sleeper,
            ),
        ),
    )
    started = time.monotonic()
    deadline = started + _NETWORK_OBSERVATION_WINDOW_SECONDS
    results: queue.Queue[
        tuple[str, NetworkProbeObservation | BaseException, float]
    ] = queue.Queue()

    def run_job(group: str, job: Callable[[], NetworkProbeObservation]) -> None:
        try:
            outcome: NetworkProbeObservation | BaseException = job()
        except BaseException as exc:  # propagate without making the worker non-daemon
            outcome = exc
        results.put((group, outcome, time.monotonic()))

    for group, job in jobs:
        threading.Thread(
            target=run_job,
            args=(group, job),
            name=f"network-probe-group-{group}",
            daemon=True,
        ).start()

    observed: dict[str, NetworkProbeObservation] = {}
    late_completion = False
    while len(observed) < len(jobs):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            group, outcome, completed_at = results.get(timeout=remaining)
        except queue.Empty:
            break
        if isinstance(outcome, BaseException):
            raise outcome
        observed[group] = outcome
        late_completion = late_completion or completed_at > deadline

    if len(observed) != len(jobs) or late_completion:
        elapsed_ms = max(
            0,
            round(_NETWORK_OBSERVATION_WINDOW_SECONDS * 1000),
        )
        return tuple(
            NetworkProbeObservation(
                group=group,
                passed=False,
                attempts=observed[group].attempts if group in observed else 0,
                elapsed_ms=(
                    observed[group].elapsed_ms if group in observed else elapsed_ms
                ),
                detail="observation_window_exceeded",
            )
            for group, _job in sorted(jobs)
        )
    return tuple(sorted(observed.values(), key=lambda item: item.group))


def _previous_items(previous_health: Mapping[str, Any] | None) -> dict[str, Mapping[str, Any]]:
    result: dict[str, Mapping[str, Any]] = {}
    for source in (previous_health or {}).get("sources", []):
        if not isinstance(source, Mapping):
            continue
        for item in source.get("items", []):
            if isinstance(item, Mapping) and isinstance(item.get("entity_id"), str):
                result[str(item["entity_id"])] = item
    return result


def _previous_source_status(
    previous_health: Mapping[str, Any] | None,
    source_id: str,
) -> TechnicalStatus | None:
    for source in (previous_health or {}).get("sources", []):
        if isinstance(source, Mapping) and source.get("source_id") == source_id:
            try:
                return TechnicalStatus(str(source.get("technical_status")))
            except ValueError:
                return None
    return None


def _failed_vod(
    candidate: VodSiteCandidate,
    reason: FailureReason,
    previous: Mapping[str, Any] | None,
) -> VodProbeResult:
    previous_status: TechnicalStatus | None = None
    if previous is not None:
        try:
            previous_status = TechnicalStatus(str(previous.get("technical_status")))
        except ValueError:
            previous_status = None
    technical = technical_status_for_failure(previous_status, reason)
    return VodProbeResult(
        candidate=candidate,
        technical_status=technical,
        publication_status=publication_status_for(
            candidate.rights_status,
            technical,
            entity_kind="spider" if candidate.type == 3 else "vod",
            site_type=candidate.type,
            failure_reasons=(reason,),
        ),
        capabilities=VodCapabilities(),
        failure_reason=reason,
    )


def _candidate_static_reason(candidate: VodSiteCandidate) -> FailureReason | None:
    raw = candidate.raw
    for key in ("jar", "spider"):
        value = raw.get(key)
        if value not in (None, "", [], {}):
            return FailureReason.UNSUPPORTED_SPIDER
    if any(_EXECUTABLE.search(str(value)) for value in raw.values()):
        return FailureReason.UNSUPPORTED_SPIDER
    if candidate.type in {0, 1} and raw.get("ext") not in (None, "", [], {}):
        return FailureReason.CLIENT_EXTENSION_UNSUPPORTED
    return None


def _normalize_client_url(source: SourceSpec, value: str) -> str:
    return normalize_url(
        value,
        allowed_hosts=source.allowed_hosts,
        client_visible=True,
    ).value


def _collect_vod_site(
    source: SourceSpec,
    snapshot_content: bytes,
    resolved_url: str,
    adapter: ProbeHttpAdapter,
    previous: Mapping[str, Mapping[str, Any]],
) -> tuple[VodProbeResult, ...]:
    if source.client_site is None:
        raise ContractError("vod_site source is missing client_site")
    site_type = 1 if source.parser.value == "maccms_json" else 0
    categories: tuple[str, ...] = ()
    try:
        home = parse_maccms_document(site_type, snapshot_content)
        categories = tuple(
            name for _, name in home.categories if name not in source.denied_categories
        )
    except ProbeRequestError:
        pass
    client = source.client_site
    try:
        api = _normalize_client_url(source, resolved_url)
    except SecurityError as exc:
        candidate = VodSiteCandidate(
            source_id=source.id,
            key=client.key,
            name=client.name,
            type=site_type,
            api=resolved_url,
            searchable=client.searchable,
            quick_search=client.quick_search,
            filterable=client.filterable,
            changeable=client.changeable,
            categories=categories,
            rights_status=source.rights_status,
        )
        return (_failed_vod(candidate, _reason_for_exception(exc), None),)
    candidate = VodSiteCandidate(
        source_id=source.id,
        key=client.key,
        name=client.name,
        type=site_type,
        api=api,
        searchable=client.searchable,
        quick_search=client.quick_search,
        filterable=client.filterable,
        changeable=client.changeable,
        categories=categories,
        rights_status=source.rights_status,
    )
    result = probe_vod(candidate, adapter, SafeMediaProber(adapter))
    return (result,)


def _collect_vod_config(
    source: SourceSpec,
    snapshot_content: bytes,
    resolved_url: str,
    adapter: ProbeHttpAdapter,
    previous: Mapping[str, Mapping[str, Any]],
    discarded: list[DiscardedEntity],
) -> tuple[VodProbeResult, ...]:
    config = parse_tvbox_config(
        snapshot_content,
        json5_mode=source.parser.value == "tvbox_json5",
        base_url=resolved_url,
    )
    for issue in config.issues:
        discarded.append(
            DiscardedEntity(
                source_id=source.id,
                entity_kind="vod_site",
                target_hash=hashlib.sha256(
                    (
                        f"{source.id}\0config-issue\0{issue.index}\0{issue.failure_reason.value}"
                    ).encode()
                ).hexdigest(),
                failure_reason=issue.failure_reason,
            )
        )
    candidates: dict[tuple[int, str], VodSiteCandidate] = {}
    for parsed in config.sites:
        try:
            api = _normalize_client_url(source, parsed.api)
        except SecurityError as exc:
            discarded.append(
                DiscardedEntity(
                    source_id=source.id,
                    entity_kind="vod_site",
                    target_hash=hashlib.sha256(parsed.api.encode("utf-8")).hexdigest(),
                    failure_reason=_reason_for_exception(exc),
                )
            )
            continue
        capabilities = (
            parsed.searchable,
            parsed.quick_search,
            parsed.filterable,
            parsed.changeable,
        )
        if any(value is None for value in capabilities):
            discarded.append(
                DiscardedEntity(
                    source_id=source.id,
                    entity_kind="vod_site",
                    target_hash=hashlib.sha256(f"{parsed.type}\0{api}".encode()).hexdigest(),
                    failure_reason=FailureReason.SCHEMA_INCOMPATIBLE,
                )
            )
            continue
        candidate = VodSiteCandidate(
            source_id=source.id,
            key=parsed.key,
            name=parsed.name,
            type=parsed.type,
            api=api,
            searchable=parsed.searchable if parsed.searchable is not None else 0,
            quick_search=parsed.quick_search if parsed.quick_search is not None else 0,
            filterable=parsed.filterable if parsed.filterable is not None else 0,
            changeable=parsed.changeable if parsed.changeable is not None else 0,
            categories=parsed.categories,
            rights_status=source.rights_status,
            declared_headers=parsed.declared_headers,
            raw=parsed.raw,
        )
        candidates.setdefault((candidate.type, candidate.api), candidate)

    results: list[VodProbeResult] = []
    media = SafeMediaProber(adapter)
    for identity, candidate in sorted(
        candidates.items(), key=lambda item: (item[0][0], item[0][1].encode("utf-8"))
    ):
        _ = identity
        previous_item = previous.get(vod_entity_id(candidate))
        static_reason = _candidate_static_reason(candidate)
        results.append(
            _failed_vod(candidate, static_reason, previous_item)
            if static_reason is not None
            else probe_vod(candidate, adapter, media)
        )
    return tuple(results)


def _safe_optional_url(source: SourceSpec, value: str | None) -> str | None:
    if value is None:
        return None
    try:
        return normalize_url(
            value,
            allowed_hosts=source.allowed_hosts,
            allow_discovered_host=source.allow_discovered_media_hosts,
            client_visible=True,
        ).value
    except SecurityError:
        return None


def _probe_optional_url(
    adapter: ProbeHttpAdapter,
    value: str | None,
) -> str | None:
    if value is None:
        return None
    try:
        response = adapter.get(value, max_bytes=256 * 1024)
    except ProbeRequestError:
        return None
    return value if 200 <= response.status_code <= 299 and bool(response.body) else None


def _collect_live(
    source: SourceSpec,
    snapshot_content: bytes,
    resolved_url: str,
    adapter: ProbeHttpAdapter,
    checked_at: str,
    previous: Mapping[str, Mapping[str, Any]],
    discarded: list[DiscardedEntity],
) -> tuple[LiveProbeResult, ...]:
    playlist = (
        parse_m3u(snapshot_content, base_url=resolved_url)
        if source.parser.value == "m3u"
        else parse_txt_live(snapshot_content, base_url=resolved_url)
    )
    for issue in playlist.issues:
        discarded.append(
            DiscardedEntity(
                source_id=source.id,
                entity_kind="live_url",
                target_hash=hashlib.sha256(
                    (
                        f"{source.id}\0playlist-issue\0{issue.index}\0{issue.failure_reason.value}"
                    ).encode()
                ).hexdigest(),
                failure_reason=issue.failure_reason,
            )
        )
    candidates: dict[str, LiveCandidate] = {}
    for entry in playlist.entries:
        try:
            normalized = normalize_url(
                entry.url,
                allowed_hosts=source.allowed_hosts,
                allow_discovered_host=source.allow_discovered_media_hosts,
                client_visible=True,
            ).value
        except SecurityError as exc:
            discarded.append(
                DiscardedEntity(
                    source_id=source.id,
                    entity_kind="live_url",
                    target_hash=hashlib.sha256(entry.url.encode("utf-8")).hexdigest(),
                    failure_reason=_reason_for_exception(exc),
                )
            )
            continue
        candidates.setdefault(
            normalized,
            LiveCandidate(
                source_id=source.id,
                name=entry.name,
                original_url=normalized,
                normalized_url=normalized,
                rights_status=source.rights_status,
                tvg_id=entry.tvg_id,
                group=entry.group,
                logo=_probe_optional_url(adapter, _safe_optional_url(source, entry.logo)),
                epg=_probe_optional_url(adapter, _safe_optional_url(source, entry.epg)),
                declared_headers=entry.declared_headers,
            ),
        )
    results: list[LiveProbeResult] = []
    for _, candidate in sorted(candidates.items(), key=lambda item: item[0].encode("utf-8")):
        results.append(
            probe_live(
                candidate,
                adapter,
                checked_at=checked_at,
                previous=previous.get(live_url_id(candidate)),
            )
        )
    return tuple(results)


def _source_failure_status(
    source: SourceSpec,
    reason: FailureReason,
    previous_health: Mapping[str, Any] | None,
) -> TechnicalStatus:
    return technical_status_for_failure(
        _previous_source_status(previous_health, source.id),
        reason,
    )


def collect_sources(
    *,
    sources: Iterable[SourceSpec],
    http_client: Fetcher,
    checked_at: str,
    previous_health: Mapping[str, Any] | None = None,
    network_probes: Sequence[NetworkProbeObservation] | None = None,
    network_resolver: Callable[
        ..., Sequence[tuple[int, int, int, str, tuple[Any, ...]]]
    ] = socket.getaddrinfo,
    network_sleeper: Callable[[float], None] = time.sleep,
) -> CollectResult:
    """Collect every enabled source and return generator-ready probe results."""

    active = tuple(sorted((source for source in sources if source.enabled), key=lambda s: s.id))
    previous = _previous_items(previous_health)
    vod_results: list[VodProbeResult] = []
    live_results: list[LiveProbeResult] = []
    observations: list[SourceObservation] = []
    catalog_results: list[CatalogScanResult] = []
    discarded: list[DiscardedEntity] = []
    revisions: dict[str, str | None] = {}
    failures: dict[str, tuple[TechnicalStatus, FailureReason | None]] = {}
    enumerated: set[str] = set()

    for source in active:
        if source.rights_status in {RightsStatus.RESTRICTED, RightsStatus.TAKEDOWN}:
            reason = (
                FailureReason.TAKEDOWN
                if source.rights_status is RightsStatus.TAKEDOWN
                else FailureReason.RIGHTS_RESTRICTED
            )
            technical = _source_failure_status(source, reason, previous_health)
            failures[source.id] = (technical, reason)
            observations.append(
                SourceObservation(
                    source.id,
                    source.fetch.mode.value,
                    None,
                    None,
                    None,
                    {},
                    technical,
                    reason,
                    (),
                    False,
                )
            )
            continue
        try:
            snapshot = resolve_upstream(source, http_client)
        except UpstreamFailure as exc:
            technical = _source_failure_status(source, exc.reason, previous_health)
            failures[source.id] = (technical, exc.reason)
            observations.append(
                SourceObservation(
                    source.id,
                    source.fetch.mode.value,
                    exc.resolved_revision,
                    None,
                    None,
                    exc.terms_sha256,
                    technical,
                    exc.reason,
                    exc.secondary_reasons,
                    False,
                )
            )
            continue

        revisions[source.id] = snapshot.resolved_revision
        adapter = ProbeHttpAdapter(http_client, source)
        try:
            if source.kind is SourceKind.VOD_SITE:
                assert snapshot.content is not None and snapshot.resolved_fetch_url is not None
                source_results = _collect_vod_site(
                    source,
                    snapshot.content,
                    snapshot.resolved_fetch_url,
                    adapter,
                    previous,
                )
                vod_results.extend(source_results)
                enumerated.add(source.id)
                technical = aggregate_technical_status(
                    [result.technical_status for result in source_results]
                )
                failure_reason, _ = prioritize_failure_reasons(
                    result.failure_reason for result in source_results
                )
            elif source.kind is SourceKind.VOD_CONFIG:
                assert snapshot.content is not None and snapshot.resolved_fetch_url is not None
                source_results = _collect_vod_config(
                    source,
                    snapshot.content,
                    snapshot.resolved_fetch_url,
                    adapter,
                    previous,
                    discarded,
                )
                vod_results.extend(source_results)
                enumerated.add(source.id)
                technical = aggregate_technical_status(
                    [result.technical_status for result in source_results]
                )
                failure_reason, _ = prioritize_failure_reasons(
                    result.failure_reason for result in source_results
                )
            elif source.kind is SourceKind.LIVE_PLAYLIST:
                assert snapshot.content is not None and snapshot.resolved_fetch_url is not None
                source_live_results = _collect_live(
                    source,
                    snapshot.content,
                    snapshot.resolved_fetch_url,
                    adapter,
                    checked_at,
                    previous,
                    discarded,
                )
                live_results.extend(source_live_results)
                enumerated.add(source.id)
                technical = aggregate_technical_status(
                    [result.technical_status for result in source_live_results]
                )
                failure_reason, _ = prioritize_failure_reasons(
                    result.failure_reason for result in source_live_results
                )
            else:
                catalog = scan_catalog(source, snapshot, http_client)
                catalog_results.append(catalog)
                technical = catalog.technical_status
                failure_reason = catalog.failure_reason
                if catalog.inconclusive or failure_reason not in {
                    None,
                    FailureReason.CATALOG_DEPTH_EXCEEDED,
                }:
                    failures[source.id] = (technical, failure_reason)
                else:
                    enumerated.add(source.id)
        except (ContractError, ProbeRequestError) as exc:
            if isinstance(exc, ProbeRequestError):
                reason = exc.reason
            else:
                message = str(exc).casefold()
                reason = (
                    FailureReason.INVALID_JSON
                    if "json" in message
                    else FailureReason.SCHEMA_INCOMPATIBLE
                )
            technical = _source_failure_status(source, reason, previous_health)
            failures[source.id] = (technical, reason)
            failure_reason = reason

        observations.append(
            SourceObservation(
                source_id=source.id,
                fetch_mode=source.fetch.mode.value,
                resolved_revision=snapshot.resolved_revision,
                resolved_fetch_url=snapshot.resolved_fetch_url,
                content_sha256=snapshot.content_sha256,
                terms_sha256=snapshot.terms_sha256,
                technical_status=technical,
                failure_reason=failure_reason,
                secondary_reasons=(),
                enumerated=source.id in enumerated,
            )
        )

    probe_observations = (
        tuple(network_probes)
        if network_probes is not None
        else (
            run_public_network_probes(
                http_client,
                resolver=network_resolver,
                sleeper=network_sleeper,
            )
        )
    )
    probe_observations = tuple(sorted(probe_observations, key=lambda item: item.group))
    expected_probe_groups = {
        "github_raw",
        "dns_public",
        "cloudflare_http",
        "google_http",
    }
    if {item.group for item in probe_observations} != expected_probe_groups or len(
        probe_observations
    ) != 4:
        raise ContractError("network observations must contain each SPEC 14.1 group once")
    failed_groups = sum(not item.passed for item in probe_observations)
    selected = select_channels(live_results)
    return CollectResult(
        checked_at=checked_at,
        sources=active,
        vod_results=tuple(vod_results),
        live_results=tuple(live_results),
        selected_channels=selected,
        source_observations=tuple(observations),
        catalog_results=tuple(catalog_results),
        discarded_entities=tuple(
            sorted(
                discarded,
                key=lambda item: (item.source_id, item.entity_kind, item.target_hash),
            )
        ),
        upstream_revisions=dict(sorted(revisions.items())),
        source_failures=dict(sorted(failures.items())),
        enumerated_source_ids=frozenset(enumerated),
        network_probes=probe_observations,
        failed_network_groups=failed_groups,
    )
