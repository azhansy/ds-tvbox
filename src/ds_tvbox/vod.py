"""MacCMS request contracts and deterministic VOD health orchestration."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ds_tvbox import parsers as canonical_parsers
from ds_tvbox.errors import ContractError, SecurityError
from ds_tvbox.models import (
    DeclaredHeaders,
    FailureReason,
    MediaProbeResult,
    PublicationStatus,
    TechnicalStatus,
    VodCapabilities,
    VodProbeResult,
    VodSiteCandidate,
)
from ds_tvbox.policy import (
    prioritize_failure_reasons,
    publication_status_for,
    technical_status_for_failure,
)
from ds_tvbox.security import normalize_client_url_offline

MAX_CONFIG_BYTES = 5 * 1024 * 1024
_HTML_ERROR_MARKERS = (
    "captcha",
    "验证码",
    "verify you are human",
    "access denied",
    "login required",
    "请登录",
    "403 forbidden",
    "404 not found",
    "502 bad gateway",
    "503 service unavailable",
)
_CREDENTIAL_QUERY_KEYS = frozenset(
    {
        "auth",
        "authorization",
        "token",
        "accesstoken",
        "apikey",
        "key",
        "sign",
        "signature",
        "secret",
        "expires",
        "expiry",
        "header",
        "headers",
        "httpheader",
    }
)
_EXECUTABLE_SUFFIX_RE = re.compile(r"(?i)\.(?:jar|js|py|dex|so)(?:$|[?#])")
_STATIC_PLAY_URL_FAILURES = frozenset(
    {
        FailureReason.CREDENTIAL_QUERY_REJECTED,
        FailureReason.PRIVATE_ADDRESS_REJECTED,
        FailureReason.DANGEROUS_SCHEME_REJECTED,
        FailureReason.CLIENT_HTTP_DISALLOWED,
    }
)


@dataclass(frozen=True)
class HttpResponse:
    """Bounded response returned by the injected safe HTTP client."""

    status_code: int
    body: bytes
    final_url: str
    elapsed_ms: int = 0
    headers: Mapping[str, str] = field(default_factory=dict)


class HttpClient(Protocol):
    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        max_bytes: int | None = None,
    ) -> HttpResponse: ...


class MediaProber(Protocol):
    def probe(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> MediaProbeResult: ...


class ProbeRequestError(Exception):
    """A safe transport can raise this to preserve a stable failure reason."""

    def __init__(self, reason: FailureReason) -> None:
        super().__init__(reason.value)
        self.reason = reason


@dataclass(frozen=True)
class VodEntry:
    vod_id: str
    name: str
    play_from: str | None = None
    play_url: str | None = None


@dataclass(frozen=True)
class MaccmsDocument:
    categories: tuple[tuple[str, str], ...]
    videos: tuple[VodEntry, ...]


def build_maccms_url(
    api: str,
    site_type: int,
    operation: str,
    *,
    type_id: str | None = None,
    page: int = 1,
    keyword: str | None = None,
    vod_id: str | None = None,
    filters: Mapping[str, Any] | None = None,
) -> str:
    """Build one MacCMS request while retaining unrelated fixed query values."""

    if site_type not in {0, 1}:
        raise ValueError("MacCMS site_type must be 0 or 1")
    if operation == "home":
        return api
    if page < 1:
        raise ValueError("page must be positive")

    params: list[tuple[str, str]]
    controlled: frozenset[str]
    if operation == "category":
        if not type_id:
            raise ValueError("type_id is required for category")
        params = [
            ("ac", "videolist" if site_type == 0 else "detail"),
            ("t", type_id),
            ("pg", str(page)),
        ]
        controlled = frozenset({"ac", "t", "pg", "f"})
        if site_type == 1 and filters is not None:
            params.append(
                (
                    "f",
                    json.dumps(filters, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                )
            )
    elif operation == "search":
        if keyword is None:
            raise ValueError("keyword is required for search")
        params = [("wd", keyword), ("quick", "false"), ("extend", "")]
        controlled = frozenset({"wd", "quick", "extend", "pg"})
        if page >= 2:
            params.append(("pg", str(page)))
    elif operation == "detail":
        if not vod_id:
            raise ValueError("vod_id is required for detail")
        params = [
            ("ac", "videolist" if site_type == 0 else "detail"),
            ("ids", vod_id),
        ]
        controlled = frozenset({"ac", "ids"})
    else:
        raise ValueError(f"unsupported operation: {operation}")

    split = urlsplit(api)
    original = parse_qsl(split.query, keep_blank_values=True)
    kept = [(key, value) for key, value in original if key not in controlled]
    return urlunsplit(
        (split.scheme, split.netloc, split.path, urlencode(kept + params), split.fragment)
    )


def _text(body: bytes, invalid_reason: FailureReason) -> str:
    try:
        return body.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ProbeRequestError(invalid_reason) from exc


def _looks_like_html_error(text: str) -> bool:
    folded = text[:16_384].casefold()
    is_html = "<html" in folded or "<!doctype html" in folded
    return is_html and any(marker in folded for marker in _HTML_ERROR_MARKERS)


def _adapter_failure(exc: ContractError, invalid_format: FailureReason) -> ProbeRequestError:
    message = str(exc)
    if message == "playback URL has an invalid scheme":
        return ProbeRequestError(FailureReason.DANGEROUS_SCHEME_REJECTED)
    if message.startswith("playback "):
        return ProbeRequestError(FailureReason.PLAY_CONTRACT_FAILED)
    if invalid_format is FailureReason.INVALID_JSON and (
        message == "JSON must be UTF-8"
        or message == "invalid JSON"
        or message.startswith("duplicate JSON key:")
    ):
        return ProbeRequestError(FailureReason.INVALID_JSON)
    if invalid_format is FailureReason.INVALID_XML and message == "invalid or unsafe MacCMS XML":
        return ProbeRequestError(FailureReason.INVALID_XML)
    return ProbeRequestError(FailureReason.SCHEMA_INCOMPATIBLE)


def _adapt_maccms(response: canonical_parsers.MacCmsResponse) -> MaccmsDocument:
    categories = tuple((item.type_id, item.type_name) for item in response.classes)
    videos: list[VodEntry] = []
    for item in response.videos:
        play_from = None
        play_url = None
        if item.play_lines:
            play_from = "$$$".join(line.name for line in item.play_lines)
            play_url = "$$$".join(
                "#".join(f"{episode.title}${episode.url}" for episode in line.episodes)
                for line in item.play_lines
            )
        videos.append(
            VodEntry(
                vod_id=item.vod_id,
                name=item.vod_name,
                play_from=play_from,
                play_url=play_url,
            )
        )
    return MaccmsDocument(categories, tuple(videos))


def parse_maccms_json(body: bytes) -> MaccmsDocument:
    text = _text(body, FailureReason.INVALID_JSON)
    if _looks_like_html_error(text):
        raise ProbeRequestError(FailureReason.HOME_CONTRACT_FAILED)
    try:
        return _adapt_maccms(canonical_parsers.parse_maccms_json(body))
    except ContractError as exc:
        raise _adapter_failure(exc, FailureReason.INVALID_JSON) from exc


def parse_maccms_xml(body: bytes) -> MaccmsDocument:
    text = _text(body, FailureReason.INVALID_XML)
    if _looks_like_html_error(text):
        raise ProbeRequestError(FailureReason.HOME_CONTRACT_FAILED)
    try:
        return _adapt_maccms(canonical_parsers.parse_maccms_xml(body))
    except ContractError as exc:
        raise _adapter_failure(exc, FailureReason.INVALID_XML) from exc


def parse_maccms_document(site_type: int, body: bytes) -> MaccmsDocument:
    if site_type == 0:
        return parse_maccms_xml(body)
    if site_type == 1:
        return parse_maccms_json(body)
    raise ValueError("MacCMS site_type must be 0 or 1")


def extract_play_urls(entry: VodEntry) -> tuple[str, ...]:
    if not entry.play_from or not entry.play_url:
        raise ProbeRequestError(FailureReason.PLAY_CONTRACT_FAILED)
    providers = entry.play_from.split("$$$")
    routes = entry.play_url.split("$$$")
    if len(providers) != len(routes) or not providers:
        raise ProbeRequestError(FailureReason.PLAY_CONTRACT_FAILED)
    urls: list[str] = []
    for provider, route in zip(providers, routes, strict=True):
        if not provider.strip() or not route.strip():
            raise ProbeRequestError(FailureReason.PLAY_CONTRACT_FAILED)
        for episode in route.split("#"):
            if "$" not in episode:
                raise ProbeRequestError(FailureReason.PLAY_CONTRACT_FAILED)
            title, url = episode.split("$", 1)
            url = url.strip()
            if not title.strip() or not url:
                raise ProbeRequestError(FailureReason.PLAY_CONTRACT_FAILED)
            try:
                urls.append(normalize_client_url_offline(url).value)
            except SecurityError as exc:
                raise ProbeRequestError(_play_url_security_reason(exc)) from exc
    if not urls:
        raise ProbeRequestError(FailureReason.PLAY_CONTRACT_FAILED)
    return tuple(urls)


def _play_url_security_reason(exc: SecurityError) -> FailureReason:
    message = str(exc).casefold()
    if "credential/header query" in message or "query key" in message:
        return FailureReason.CREDENTIAL_QUERY_REJECTED
    if "userinfo" in message:
        # Userinfo is a static URL credential violation, not an observation that
        # could become valid during the declared-Header diagnostic pass.
        return FailureReason.CREDENTIAL_QUERY_REJECTED
    if "client-visible http" in message:
        return FailureReason.CLIENT_HTTP_DISALLOWED
    if "private" in message or "special-purpose" in message:
        return FailureReason.PRIVATE_ADDRESS_REJECTED
    if "scheme" in message:
        return FailureReason.DANGEROUS_SCHEME_REJECTED
    return FailureReason.PLAY_CONTRACT_FAILED


def _http_reason(status: int) -> FailureReason:
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
    return FailureReason.SCHEMA_INCOMPATIBLE


def _get(
    client: HttpClient,
    url: str,
    headers: Mapping[str, str] | None,
    *,
    max_bytes: int = MAX_CONFIG_BYTES,
) -> HttpResponse:
    try:
        response = client.get(url, headers=headers, max_bytes=max_bytes)
    except ProbeRequestError:
        raise
    except TimeoutError as exc:
        raise ProbeRequestError(FailureReason.FETCH_TIMEOUT) from exc
    except OSError as exc:
        raise ProbeRequestError(FailureReason.DNS_FAILURE) from exc
    if not 200 <= response.status_code <= 299:
        raise ProbeRequestError(_http_reason(response.status_code))
    if len(response.body) > max_bytes:
        raise ProbeRequestError(FailureReason.RESPONSE_TOO_LARGE)
    if not response.body:
        raise ProbeRequestError(FailureReason.SCHEMA_INCOMPATIBLE)
    return response


def _failed_result(
    candidate: VodSiteCandidate,
    capabilities: VodCapabilities,
    reasons: Sequence[FailureReason | None],
    *,
    previous_status: TechnicalStatus | None = None,
) -> VodProbeResult:
    primary, secondary = prioritize_failure_reasons(reasons)
    assert primary is not None
    technical = technical_status_for_failure(previous_status, primary)
    publication = publication_status_for(
        candidate.rights_status,
        technical,
        entity_kind="spider" if candidate.type == 3 else "vod",
        site_type=candidate.type,
        failure_reasons=(primary, *secondary),
    )
    return VodProbeResult(
        candidate=candidate,
        technical_status=technical,
        publication_status=publication,
        capabilities=capabilities,
        failure_reason=primary,
        secondary_reasons=secondary,
    )


def _probe_type4_once(
    candidate: VodSiteCandidate,
    client: HttpClient,
    headers: Mapping[str, str] | None,
) -> VodProbeResult:
    capabilities = VodCapabilities()
    split = urlsplit(candidate.api)
    if split.scheme.lower() != "https" or not split.netloc:
        return _failed_result(
            candidate, capabilities, (FailureReason.CLIENT_HTTP_DISALLOWED,)
        )
    for key, _ in parse_qsl(split.query, keep_blank_values=True):
        normalized = re.sub(r"[_-]", "", key).casefold()
        if normalized in _CREDENTIAL_QUERY_KEYS:
            return _failed_result(
                candidate, capabilities, (FailureReason.CREDENTIAL_QUERY_REJECTED,)
            )
    forbidden_keys = {"ext", "jar", "parser", "spider"}.intersection(candidate.raw)
    if any(candidate.raw.get(key) not in (None, "", [], {}) for key in forbidden_keys):
        return _failed_result(
            candidate, capabilities, (FailureReason.CLIENT_EXTENSION_UNSUPPORTED,)
        )
    if any(
        _EXECUTABLE_SUFFIX_RE.search(str(value))
        for value in _walk_scalars(candidate.raw)
    ):
        return _failed_result(candidate, capabilities, (FailureReason.UNSUPPORTED_SPIDER,))
    try:
        response = _get(client, candidate.api, headers)
    except ProbeRequestError as exc:
        return _failed_result(candidate, capabilities, (exc.reason,))
    try:
        text = _text(response.body, FailureReason.SCHEMA_INCOMPATIBLE)
    except ProbeRequestError as exc:
        return _failed_result(candidate, capabilities, (exc.reason,))
    if _looks_like_html_error(text):
        return _failed_result(candidate, capabilities, (FailureReason.HOME_CONTRACT_FAILED,))
    technical = TechnicalStatus.PARTIAL
    return VodProbeResult(
        candidate=candidate,
        technical_status=technical,
        publication_status=publication_status_for(
            candidate.rights_status,
            technical,
            entity_kind="vod",
            site_type=4,
        ),
        capabilities=capabilities,
        failure_reason=None,
    )


def _walk_scalars(value: Any) -> tuple[Any, ...]:
    if isinstance(value, Mapping):
        return tuple(item for nested in value.values() for item in _walk_scalars(nested))
    if isinstance(value, (list, tuple)):
        return tuple(item for nested in value for item in _walk_scalars(nested))
    return (value,)


def _probe_maccms_once(
    candidate: VodSiteCandidate,
    client: HttpClient,
    media_prober: MediaProber,
    headers: Mapping[str, str] | None,
) -> VodProbeResult:
    capabilities = VodCapabilities()
    try:
        home = parse_maccms_document(
            candidate.type,
            _get(client, build_maccms_url(candidate.api, candidate.type, "home"), headers).body,
        )
        if not home.categories and not home.videos:
            raise ProbeRequestError(FailureReason.HOME_CONTRACT_FAILED)
        capabilities = VodCapabilities(home=True)

        sample = home.videos[0] if home.videos else None
        if sample is None:
            category = parse_maccms_document(
                candidate.type,
                _get(
                    client,
                    build_maccms_url(
                        candidate.api,
                        candidate.type,
                        "category",
                        type_id=home.categories[0][0],
                    ),
                    headers,
                ).body,
            )
            if not category.videos:
                raise ProbeRequestError(FailureReason.HOME_CONTRACT_FAILED)
            sample = category.videos[0]

        search = parse_maccms_document(
            candidate.type,
            _get(
                client,
                build_maccms_url(
                    candidate.api,
                    candidate.type,
                    "search",
                    keyword=sample.name,
                ),
                headers,
            ).body,
        )
        search_match = next(
            (item for item in search.videos if item.name == sample.name),
            None,
        )
        capabilities = VodCapabilities(home=True, search=search_match is not None)
        detail_target = search_match or sample

        detail = parse_maccms_document(
            candidate.type,
            _get(
                client,
                build_maccms_url(
                    candidate.api,
                    candidate.type,
                    "detail",
                    vod_id=detail_target.vod_id,
                ),
                headers,
            ).body,
        )
        exact = [item for item in detail.videos if item.vod_id == detail_target.vod_id]
        if len(exact) != 1:
            raise ProbeRequestError(FailureReason.DETAIL_CONTRACT_FAILED)
        detail_entry = exact[0]
        if detail_entry.name != detail_target.name:
            raise ProbeRequestError(FailureReason.DETAIL_CONTRACT_FAILED)
        capabilities = VodCapabilities(
            home=True,
            search=search_match is not None,
            detail=True,
        )
        play_urls = extract_play_urls(detail_entry)
        capabilities = VodCapabilities(
            home=True,
            search=search_match is not None,
            detail=True,
            play=True,
        )
        media = media_prober.probe(play_urls[0], headers=headers)
        if not media.ok:
            reason = media.failure_reason or FailureReason.MEDIA_PROBE_FAILED
            result = _failed_result(candidate, capabilities, (reason,))
            return VodProbeResult(
                candidate=result.candidate,
                technical_status=result.technical_status,
                publication_status=result.publication_status,
                capabilities=result.capabilities,
                failure_reason=reason,
                secondary_reasons=result.secondary_reasons,
                sample_title=sample.name,
                sample_vod_id=detail_target.vod_id,
                sample_media_url=play_urls[0],
            )
        capabilities = VodCapabilities(
            home=True,
            search=search_match is not None,
            detail=True,
            play=True,
            media_probe=True,
        )
        technical = (
            TechnicalStatus.HEALTHY if search_match is not None else TechnicalStatus.PARTIAL
        )
        return VodProbeResult(
            candidate=candidate,
            technical_status=technical,
            publication_status=publication_status_for(
                candidate.rights_status,
                technical,
                entity_kind="vod",
                site_type=candidate.type,
            ),
            capabilities=capabilities,
            failure_reason=None,
            sample_title=sample.name,
            sample_vod_id=detail_target.vod_id,
            sample_media_url=play_urls[0],
        )
    except ProbeRequestError as exc:
        return _failed_result(candidate, capabilities, (exc.reason,))


def _headers(candidate: VodSiteCandidate) -> Mapping[str, str] | None:
    declared: DeclaredHeaders | None = candidate.declared_headers
    return None if declared is None else declared.values


def _header_diagnostic_result(
    candidate: VodSiteCandidate,
    first: VodProbeResult,
    second: VodProbeResult,
) -> VodProbeResult:
    if second.failure_reason is None and second.technical_status in {
        TechnicalStatus.HEALTHY,
        TechnicalStatus.PARTIAL,
    }:
        secondary = tuple(
            dict.fromkeys(
                reason
                for reason in (first.failure_reason, *first.secondary_reasons)
                if reason is not None
            )
        )
        return VodProbeResult(
            candidate=candidate,
            technical_status=TechnicalStatus.PARTIAL,
            publication_status=PublicationStatus.WITHHELD,
            capabilities=second.capabilities,
            failure_reason=FailureReason.CLIENT_HEADER_UNSUPPORTED,
            secondary_reasons=secondary,
            sample_title=second.sample_title,
            sample_vod_id=second.sample_vod_id,
            sample_media_url=second.sample_media_url,
        )
    primary, secondary = prioritize_failure_reasons(
        (
            first.failure_reason,
            *first.secondary_reasons,
            second.failure_reason,
            *second.secondary_reasons,
        )
    )
    return VodProbeResult(
        candidate=candidate,
        technical_status=first.technical_status,
        publication_status=first.publication_status,
        capabilities=first.capabilities,
        failure_reason=primary,
        secondary_reasons=secondary,
        sample_title=first.sample_title,
        sample_vod_id=first.sample_vod_id,
        sample_media_url=first.sample_media_url,
    )


def probe_type4(candidate: VodSiteCandidate, client: HttpClient) -> VodProbeResult:
    if candidate.type != 4:
        raise ValueError("probe_type4 requires candidate.type == 4")
    first = _probe_type4_once(candidate, client, None)
    declared = _headers(candidate)
    if declared is None or first.technical_status is TechnicalStatus.PARTIAL:
        return first
    second = _probe_type4_once(candidate, client, declared)
    return _header_diagnostic_result(candidate, first, second)


def probe_vod(
    candidate: VodSiteCandidate,
    client: HttpClient,
    media_prober: MediaProber,
) -> VodProbeResult:
    """Run the no-Header path first and use declared Headers for diagnosis only."""

    if candidate.type == 4:
        return probe_type4(candidate, client)
    if candidate.type == 3:
        return _failed_result(
            candidate, VodCapabilities(), (FailureReason.UNSUPPORTED_SPIDER,)
        )
    if candidate.type not in {0, 1}:
        return _failed_result(
            candidate, VodCapabilities(), (FailureReason.SCHEMA_INCOMPATIBLE,)
        )
    first = _probe_maccms_once(candidate, client, media_prober, None)
    declared = _headers(candidate)
    if (
        declared is None
        or first.technical_status is TechnicalStatus.HEALTHY
        or first.failure_reason in _STATIC_PLAY_URL_FAILURES
    ):
        return first
    second = _probe_maccms_once(candidate, client, media_prober, declared)
    return _header_diagnostic_result(candidate, first, second)
