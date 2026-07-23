from __future__ import annotations

import hashlib
import json
import multiprocessing
import socket
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]

from ds_tvbox.catalog import CatalogCandidate, CatalogScanResult
from ds_tvbox.collector import (
    CollectResult,
    NetworkProbeObservation,
    ProbeHttpAdapter,
    SafeMediaProber,
    _candidate_static_reason,
    _failed_vod,
    _previous_items,
    _previous_source_status,
    _reason_for_exception,
    collect_sources,
    run_public_network_probes,
)
from ds_tvbox.errors import ContractError, FetchError, SecurityError
from ds_tvbox.http import HttpRequest, HttpResponse
from ds_tvbox.models import (
    ClientSiteSpec,
    DeclaredHeaders,
    FailureReason,
    FetchMode,
    FetchSpec,
    ParserKind,
    PublicationStatus,
    RightsStatus,
    SourceKind,
    SourceSpec,
    TechnicalStatus,
    TermsWatchSpec,
    VodSiteCandidate,
)
from ds_tvbox.vod import HttpResponse as ProbeHttpResponse
from ds_tvbox.vod import ProbeRequestError

_TS_PACKET = b"\x47\x40\x00\x10" + b"\x00" * 184
_TS_SEGMENT = _TS_PACKET * 3
_MP4_PREFIX = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 12


class FakeFetcher:
    def __init__(self, handler: Callable[[HttpRequest], HttpResponse]) -> None:
        self.handler = handler
        self.requests: list[HttpRequest] = []

    def fetch(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        return self.handler(request)


def response(request: HttpRequest, body: bytes, status: int = 200) -> HttpResponse:
    return HttpResponse(status, request.url, (), body, 2, 1, 0)


def direct_source(
    *,
    source_id: str,
    kind: SourceKind,
    parser: ParserKind,
    url: str,
    allowed_hosts: frozenset[str],
    terms: tuple[TermsWatchSpec, ...] = (),
    client_site: ClientSiteSpec | None = None,
    allow_discovered: bool = True,
) -> SourceSpec:
    return SourceSpec(
        id=source_id,
        kind=kind,
        parser=parser,
        enabled=True,
        fetch=FetchSpec(FetchMode.DIRECT_URL, url, None, None, None, None),
        terms_watch=terms,
        rights_status=RightsStatus.PUBLIC_UNVERIFIED,
        config_license_status="unknown",
        content_rights_status="unverified",
        allowed_hosts=allowed_hosts,
        allow_discovered_media_hosts=allow_discovered,
        http_exceptions=(),
        denied_categories=("Denied",),
        client_site=client_site,
        catalog=None,
        raw={},
    )


def network_observations(
    *, failed: frozenset[str] = frozenset()
) -> tuple[NetworkProbeObservation, ...]:
    return tuple(
        NetworkProbeObservation(group, group not in failed, 1, 1, "ok")
        for group in (
            "github_raw",
            "dns_public",
            "cloudflare_http",
            "google_http",
        )
    )


def test_probe_adapter_turns_range_into_internal_request_field() -> None:
    source = direct_source(
        source_id="live",
        kind=SourceKind.LIVE_PLAYLIST,
        parser=ParserKind.M3U,
        url="https://list.example.test/live.m3u",
        allowed_hosts=frozenset({"list.example.test"}),
    )
    client = FakeFetcher(lambda request: response(request, b"segment", 206))

    result = ProbeHttpAdapter(client, source).get(
        "https://media.example.test/segment.ts",
        headers={"Range": "bytes=0-1048575"},
        max_bytes=1024 * 1024,
    )

    assert result.status_code == 206
    assert client.requests[0].internal_range == (0, 1024 * 1024 - 1)
    assert client.requests[0].declared_headers is None
    assert client.requests[0].allow_discovered_host is True

    with pytest.raises(ProbeRequestError):
        ProbeHttpAdapter(client, source).get(
            "https://media.example.test/segment.ts",
            headers={"Range": "bytes=0-1048576"},
        )


def test_collect_direct_vod_keeps_registry_client_fields_and_full_chain_result() -> None:
    api = "https://vod.example.test/api.php/provide/vod"
    source = direct_source(
        source_id="vod",
        kind=SourceKind.VOD_SITE,
        parser=ParserKind.MACCMS_JSON,
        url=api,
        allowed_hosts=frozenset({"vod.example.test"}),
        client_site=ClientSiteSpec("vod_key", "VOD Name", 1, 1, 0, 1),
    )
    home = (
        b'{"class":[{"type_id":1,"type_name":"Allowed"},'
        b'{"type_id":2,"type_name":"Denied"}],'
        b'"list":[{"vod_id":"v1","vod_name":"Known"}]}'
    )
    search = b'{"list":[{"vod_id":"v1","vod_name":"Known"}]}'
    detail = (
        b'{"list":[{"vod_id":"v1","vod_name":"Known",'
        b'"vod_play_from":"line","vod_play_url":'
        b'"Episode$https://media.example.test/video.mp4"}]}'
    )

    def handler(request: HttpRequest) -> HttpResponse:
        parsed = urlsplit(request.url)
        if parsed.hostname == "media.example.test":
            return response(request, _MP4_PREFIX, 206)
        query = parse_qs(parsed.query, keep_blank_values=True)
        if "ids" in query:
            return response(request, detail)
        if "wd" in query:
            return response(request, search)
        return response(request, home)

    result = collect_sources(
        sources=(source,),
        http_client=FakeFetcher(handler),
        checked_at="2026-07-22T00:00:00Z",
        network_probes=network_observations(),
    )

    assert len(result.vod_results) == 1
    probe = result.vod_results[0]
    assert probe.technical_status.value == "healthy"
    assert probe.publication_status.value == "stable"
    assert probe.candidate.key == "vod_key"
    assert probe.candidate.searchable == 1
    assert probe.candidate.filterable == 0
    assert probe.candidate.categories == ("Allowed",)
    assert result.enumerated_source_ids == frozenset({"vod"})
    assert result.failed_network_groups == 0


def test_collect_live_parses_playlist_probes_hls_and_selects_channel() -> None:
    playlist_url = "https://list.example.test/live.m3u"
    source = direct_source(
        source_id="live",
        kind=SourceKind.LIVE_PLAYLIST,
        parser=ParserKind.M3U,
        url=playlist_url,
        allowed_hosts=frozenset({"list.example.test"}),
    )
    playlist = (
        b'#EXTM3U\n#EXTINF:-1 tvg-id="news" '
        b'tvg-logo="https://assets.example.test/bad.png" '
        b'tvg-url="https://assets.example.test/epg.xml",News\n'
        b"https://media.example.test/live.m3u8\n"
    )
    media = b"#EXTM3U\n#EXT-X-TARGETDURATION:10\n#EXTINF:10,\nsegment.ts\n"

    def handler(request: HttpRequest) -> HttpResponse:
        if request.url == playlist_url:
            return response(request, playlist)
        if request.url.endswith("live.m3u8"):
            return response(request, media)
        if request.url.endswith("bad.png"):
            return response(request, b"missing", 404)
        if request.url.endswith("epg.xml"):
            return response(request, b"<tv/>")
        if request.url.endswith("segment.ts"):
            assert request.internal_range == (0, 1024 * 1024 - 1)
            return response(request, _TS_SEGMENT, 206)
        raise AssertionError(request.url)

    result = collect_sources(
        sources=(source,),
        http_client=FakeFetcher(handler),
        checked_at="2026-07-22T00:00:00Z",
        network_probes=network_observations(),
    )

    assert len(result.live_results) == 1
    assert result.live_results[0].technical_status.value == "healthy"
    assert len(result.selected_channels) == 1
    assert result.selected_channels[0].selected.candidate.name == "News"
    assert result.selected_channels[0].selected.candidate.logo is None
    assert result.selected_channels[0].selected.candidate.epg == (
        "https://assets.example.test/epg.xml"
    )


def test_collect_vod_config_filters_credential_target_and_rejects_spider_per_item() -> None:
    config_url = "https://config.example.test/tvbox.json"
    source = direct_source(
        source_id="config",
        kind=SourceKind.VOD_CONFIG,
        parser=ParserKind.TVBOX_JSON,
        url=config_url,
        allowed_hosts=frozenset({"config.example.test", "api.example.test"}),
    )
    config = json.dumps(
        {
            "sites": [
                {
                    "key": "good",
                    "name": "Good",
                    "type": 1,
                    "api": "https://api.example.test/vod",
                    "searchable": 1,
                    "quickSearch": 1,
                    "filterable": 1,
                    "changeable": 1,
                },
                {
                    "key": "secret",
                    "name": "Secret",
                    "type": 1,
                    "api": "https://api.example.test/vod?token=do-not-log",
                    "searchable": 1,
                    "quickSearch": 1,
                    "filterable": 1,
                    "changeable": 1,
                },
                {
                    "key": "spider",
                    "name": "Spider",
                    "type": 3,
                    "api": "https://api.example.test/spider",
                    "searchable": 1,
                    "quickSearch": 1,
                    "filterable": 1,
                    "changeable": 1,
                    "jar": "https://api.example.test/a.jar",
                },
            ]
        }
    ).encode()
    home = b'{"list":[{"vod_id":"v1","vod_name":"Known"}]}'
    detail = (
        b'{"list":[{"vod_id":"v1","vod_name":"Known",'
        b'"vod_play_from":"line","vod_play_url":'
        b'"Episode$https://media.example.test/video.mp4"}]}'
    )

    def handler(request: HttpRequest) -> HttpResponse:
        if request.url == config_url:
            return response(request, config)
        if urlsplit(request.url).hostname == "media.example.test":
            return response(request, _MP4_PREFIX, 206)
        query = parse_qs(urlsplit(request.url).query, keep_blank_values=True)
        return response(request, detail if "ids" in query else home)

    result = collect_sources(
        sources=(source,),
        http_client=FakeFetcher(handler),
        checked_at="2026-07-22T00:00:00Z",
        network_probes=network_observations(),
    )

    assert len(result.vod_results) == 2
    assert {item.publication_status.value for item in result.vod_results} == {
        "stable",
        "rejected",
    }
    assert len(result.discarded_entities) == 1
    assert result.discarded_entities[0].failure_reason.value == "credential_query_rejected"
    assert "do-not-log" not in repr(result)


def test_terms_changed_remains_source_failure_and_does_not_fetch_direct_config() -> None:
    terms_url = "https://terms.example.test/terms"
    config_url = "https://vod.example.test/api"
    source = direct_source(
        source_id="vod",
        kind=SourceKind.VOD_SITE,
        parser=ParserKind.MACCMS_JSON,
        url=config_url,
        allowed_hosts=frozenset({"terms.example.test", "vod.example.test"}),
        terms=(TermsWatchSpec("url", terms_url, None, "0" * 64),),
        client_site=ClientSiteSpec("vod", "VOD", 1, 1, 1, 1),
    )
    client = FakeFetcher(lambda request: response(request, b"changed terms"))

    result = collect_sources(
        sources=(source,),
        http_client=client,
        checked_at="2026-07-22T00:00:00Z",
        network_probes=network_observations(),
    )

    assert result.vod_results == ()
    assert result.source_failures["vod"][1] is not None
    assert result.source_failures["vod"][1].value == "terms_changed"
    assert (
        result.source_observations[0].terms_sha256[terms_url]
        == hashlib.sha256(b"changed terms").hexdigest()
    )
    assert [request.url for request in client.requests] == [terms_url]


def test_public_network_probe_reports_one_failed_group_without_aborting_others() -> None:
    def handler(request: HttpRequest) -> HttpResponse:
        host = urlsplit(request.url).hostname
        if host == "raw.githubusercontent.com":
            return response(request, b"config docs")
        if host == "www.cloudflare.com":
            return response(request, b"fl=1\nip=203.0.113.1\n")
        if host == "connectivitycheck.gstatic.com":
            return response(request, b"failure", 500)
        raise AssertionError(request.url)

    def resolver(
        host: str,
        port: int,
        family: int,
        socktype: int,
    ) -> Sequence[tuple[int, int, int, str, tuple[str, int]]]:
        _ = host, port, family, socktype
        return ((socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),)

    client = FakeFetcher(handler)
    observations = run_public_network_probes(
        client,
        resolver=resolver,
        sleeper=lambda _: None,
    )

    assert [item.group for item in observations] == [
        "cloudflare_http",
        "dns_public",
        "github_raw",
        "google_http",
    ]
    assert sum(not item.passed for item in observations) == 1
    google = next(item for item in observations if item.group == "google_http")
    assert google.attempts == 2
    assert google.detail == "unexpected_response"


def test_collect_rejects_incomplete_network_probe_contract() -> None:
    with pytest.raises(ContractError, match="SPEC 14.1"):
        collect_sources(
            sources=(),
            http_client=FakeFetcher(lambda request: response(request, b"")),
            checked_at="2026-07-22T00:00:00Z",
            network_probes=(NetworkProbeObservation("github_raw", True, 1, 1, "ok"),),
        )


def test_candidates_report_matches_artifact_schema_and_keeps_catalog_candidates_flat() -> None:
    candidate = CatalogCandidate(
        candidate_id="candidate:catalog-source:0123456789abcdef",
        kind="vod_site",
        normalized_target_hash="a" * 64,
        technical_status=TechnicalStatus.UNKNOWN,
        rights_status=RightsStatus.UNKNOWN,
        publication_status=PublicationStatus.WITHHELD,
        evidence_locations=(f"https://github.com/example/repo@{'1' * 40}:config.json#/sites/0",),
        failure_reason=None,
    )
    catalog = CatalogScanResult(
        source_id="catalog-source",
        reviewed_revision="0" * 40,
        resolved_revision="1" * 40,
        technical_status=TechnicalStatus.HEALTHY,
        publication_status=PublicationStatus.WITHHELD,
        inconclusive=False,
        files_scanned=1,
        candidates=(candidate,),
    )
    result = CollectResult(
        checked_at="2026-07-22T00:00:00Z",
        sources=(),
        vod_results=(),
        live_results=(),
        selected_channels=(),
        source_observations=(),
        catalog_results=(catalog,),
        discarded_entities=(),
        upstream_revisions={},
        source_failures={},
        enumerated_source_ids=frozenset(),
        network_probes=network_observations(),
        failed_network_groups=0,
    )
    report = result.candidates_report(workflow_run_id="123", workflow_run_attempt=1)
    schema = json.loads(Path("schemas/candidates.schema.json").read_text(encoding="utf-8"))

    Draft202012Validator(schema).validate(report)
    assert report["schema_version"] == "1.0.0"
    catalogs = report["catalogs"]
    candidates = report["candidates"]
    assert isinstance(catalogs, list)
    assert isinstance(candidates, list)
    assert "candidates" not in catalogs[0]
    assert len(candidates) == 1


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (SecurityError("credential/header query key"), FailureReason.CREDENTIAL_QUERY_REJECTED),
        (SecurityError("header syntax control"), FailureReason.INVALID_HEADER_SYNTAX),
        (SecurityError("forbidden header"), FailureReason.CREDENTIAL_HEADER_REJECTED),
        (SecurityError("credential found"), FailureReason.CREDENTIAL_QUERY_REJECTED),
        (SecurityError("private peer"), FailureReason.PRIVATE_ADDRESS_REJECTED),
        (SecurityError("dangerous scheme"), FailureReason.DANGEROUS_SCHEME_REJECTED),
        (SecurityError("client http"), FailureReason.CLIENT_HTTP_DISALLOWED),
        (SecurityError("other"), FailureReason.PRIVATE_ADDRESS_REJECTED),
        (FetchError("TLS certificate"), FailureReason.TLS_FAILURE),
        (FetchError("DNS address"), FailureReason.DNS_FAILURE),
        (FetchError("all approved addresses failed"), FailureReason.FETCH_TIMEOUT),
        (FetchError("body limit"), FailureReason.RESPONSE_TOO_LARGE),
        (TimeoutError("timed out"), FailureReason.FETCH_TIMEOUT),
        (FetchError("reset"), FailureReason.FETCH_TIMEOUT),
    ],
)
def test_collector_exception_mapping_never_exposes_transport_details(
    error: BaseException,
    reason: FailureReason,
) -> None:
    assert _reason_for_exception(error) is reason


@pytest.mark.parametrize(
    "headers",
    [
        {"Range": "not-a-range"},
        {"Range": "bytes=9-2"},
        {"Range": "bytes=0-1048576"},
        {"Cookie": "secret"},
    ],
)
def test_probe_adapter_rejects_invalid_internal_or_declared_headers(
    headers: dict[str, str],
) -> None:
    source = direct_source(
        source_id="live",
        kind=SourceKind.LIVE_PLAYLIST,
        parser=ParserKind.M3U,
        url="https://list.example.test/live.m3u",
        allowed_hosts=frozenset({"list.example.test"}),
    )
    adapter = ProbeHttpAdapter(
        FakeFetcher(lambda request: response(request, b"unused")),
        source,
    )
    with pytest.raises(ProbeRequestError) as raised:
        adapter.get("https://media.example/live.ts", headers=headers)
    assert raised.value.reason in {
        FailureReason.INVALID_HEADER_SYNTAX,
        FailureReason.CREDENTIAL_HEADER_REJECTED,
    }


def test_probe_adapter_maps_fetch_errors_and_response_headers() -> None:
    source = direct_source(
        source_id="live",
        kind=SourceKind.LIVE_PLAYLIST,
        parser=ParserKind.M3U,
        url="https://list.example.test/live.m3u",
        allowed_hosts=frozenset({"list.example.test"}),
    )
    failing = ProbeHttpAdapter(
        FakeFetcher(lambda _request: (_ for _ in ()).throw(FetchError("TLS certificate"))),
        source,
    )
    with pytest.raises(ProbeRequestError) as raised:
        failing.get("https://media.example/live.ts")
    assert raised.value.reason is FailureReason.TLS_FAILURE

    client = FakeFetcher(
        lambda request: HttpResponse(
            200,
            request.url,
            (("Content-Type", "video/mp2t"),),
            b"data",
            3,
            1,
            0,
        )
    )
    result = ProbeHttpAdapter(client, source).get(
        "https://media.example/live.ts",
        headers={"User-Agent": "TVBox"},
    )
    assert result.headers == {"Content-Type": "video/mp2t"}
    assert client.requests[0].declared_headers == DeclaredHeaders({"User-Agent": "TVBox"})


class ScriptedProbeAdapter:
    def __init__(self, results: list[ProbeHttpResponse | ProbeRequestError]) -> None:
        self.results = list(results)

    def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        max_bytes: int | None = None,
    ) -> ProbeHttpResponse:
        del url, headers, max_bytes
        result = self.results.pop(0)
        if isinstance(result, ProbeRequestError):
            raise result
        return result


def test_safe_media_prober_handles_transport_status_and_hls_detection() -> None:
    failed = SafeMediaProber(  # type: ignore[arg-type]
        ScriptedProbeAdapter([ProbeRequestError(FailureReason.FETCH_TIMEOUT)])
    ).probe("https://media.example/video.mp4")
    assert failed.failure_reason is FailureReason.FETCH_TIMEOUT

    empty = SafeMediaProber(  # type: ignore[arg-type]
        ScriptedProbeAdapter([ProbeHttpResponse(404, b"", "https://media.example/final", 7)])
    ).probe("https://media.example/video.mp4")
    assert empty.failure_reason is FailureReason.HTTP_404
    assert empty.final_url == "https://media.example/final"

    media = b"#EXTM3U\n#EXT-X-TARGETDURATION:10\n#EXTINF:10,\nsegment.ts\n"
    hls = SafeMediaProber(  # type: ignore[arg-type]
        ScriptedProbeAdapter(
            [
                ProbeHttpResponse(200, media, "https://media.example/live", 2),
                ProbeHttpResponse(200, media, "https://media.example/live", 2),
                ProbeHttpResponse(206, _TS_SEGMENT, "https://media.example/segment.ts", 1),
            ]
        )
    ).probe("https://media.example/live")
    assert hls.ok is True
    assert hls.media_path_score >= 1


@pytest.mark.parametrize(
    "payload",
    [
        b"\x00\x00\x00\x18ftypisom" + b"\x00" * 12,
        b"\x00\x00\x00\x10moof" + b"\x00" * 8,
        _TS_SEGMENT,
        b"\xff\xf1\x50\x80\x01\x3f\xfc\x00\x00",
        b"ID3\x04\x00\x00\x00\x00\x00\x00"
        b"\xff\xf1\x50\x80\x01\x3f\xfc\x00\x00",
        b"\xff\xfb\x90\x64" + b"\x00" * 16,
        b"\x0b\x77" + b"\x00" * 8,
        b"\x1a\x45\xdf\xa3\x84webm",
        b"\x1a\x45\xdf\xa3\x88matroska",
        b"FLV\x01\x05\x00\x00\x00\x09",
    ],
)
def test_safe_media_prober_accepts_common_direct_media_signatures(payload: bytes) -> None:
    result = SafeMediaProber(  # type: ignore[arg-type]
        ScriptedProbeAdapter(
            [ProbeHttpResponse(206, payload, "https://media.example/video", 7)]
        )
    ).probe("https://media.example/video")

    assert result.ok
    assert result.final_url == "https://media.example/video"


@pytest.mark.parametrize(
    "payload",
    [b"<html>blocked</html>", b'{"url":"not-media"}', b"random bytes"],
)
def test_safe_media_prober_rejects_non_media_direct_bodies(payload: bytes) -> None:
    result = SafeMediaProber(  # type: ignore[arg-type]
        ScriptedProbeAdapter(
            [ProbeHttpResponse(200, payload, "https://media.example/video", 7)]
        )
    ).probe("https://media.example/video")

    assert not result.ok
    assert result.failure_reason is FailureReason.MEDIA_PROBE_FAILED


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (401, FailureReason.CREDENTIAL_REQUIRED),
        (403, FailureReason.CREDENTIAL_REQUIRED),
        (404, FailureReason.HTTP_404),
        (410, FailureReason.HTTP_410),
        (429, FailureReason.RATE_LIMITED),
        (503, FailureReason.UPSTREAM_5XX),
    ],
)
def test_safe_media_prober_preserves_http_failure_reason(
    status: int,
    reason: FailureReason,
) -> None:
    result = SafeMediaProber(  # type: ignore[arg-type]
        ScriptedProbeAdapter(
            [ProbeHttpResponse(status, b"failure", "https://media.example/video", 7)]
        )
    ).probe("https://media.example/video")

    assert not result.ok
    assert result.failure_reason is reason


def test_previous_health_extractors_ignore_malformed_entries() -> None:
    previous = {
        "sources": [
            "bad",
            {
                "source_id": "vod",
                "technical_status": "not-a-status",
                "items": [
                    "bad",
                    {"entity_id": 1},
                    {"entity_id": "vod:one", "technical_status": "healthy"},
                ],
            },
        ]
    }
    assert _previous_items(previous) == {
        "vod:one": {"entity_id": "vod:one", "technical_status": "healthy"}
    }
    assert _previous_source_status(previous, "vod") is None
    assert _previous_source_status(previous, "missing") is None


def _vod_candidate(*, site_type: int = 1, raw: dict[str, object] | None = None):
    return VodSiteCandidate(
        source_id="source",
        key="key",
        name="name",
        type=site_type,
        api="https://api.example/vod",
        searchable=1,
        quick_search=1,
        filterable=1,
        changeable=1,
        categories=(),
        rights_status=RightsStatus.PUBLIC_UNVERIFIED,
        raw=raw or {},
    )


@pytest.mark.parametrize(
    ("candidate", "reason"),
    [
        (_vod_candidate(raw={"jar": "plugin.jar"}), FailureReason.UNSUPPORTED_SPIDER),
        (_vod_candidate(raw={"misc": "plugin.js"}), FailureReason.UNSUPPORTED_SPIDER),
        (
            _vod_candidate(raw={"ext": {"dependency": "x"}}),
            FailureReason.CLIENT_EXTENSION_UNSUPPORTED,
        ),
        (_vod_candidate(site_type=4, raw={"ext": "allowed"}), None),
    ],
)
def test_static_vod_rejections_are_deterministic(
    candidate: VodSiteCandidate,
    reason: FailureReason | None,
) -> None:
    assert _candidate_static_reason(candidate) is reason


def test_failed_vod_uses_valid_previous_status_but_ignores_invalid_value() -> None:
    candidate = _vod_candidate(site_type=3)
    valid = _failed_vod(
        candidate,
        FailureReason.UNSUPPORTED_SPIDER,
        {"technical_status": "healthy"},
    )
    invalid = _failed_vod(
        candidate,
        FailureReason.UNSUPPORTED_SPIDER,
        {"technical_status": "invalid"},
    )
    assert valid.failure_reason is FailureReason.UNSUPPORTED_SPIDER
    assert invalid.failure_reason is FailureReason.UNSUPPORTED_SPIDER


@pytest.mark.parametrize("rights", [RightsStatus.RESTRICTED, RightsStatus.TAKEDOWN])
def test_collect_never_fetches_restricted_or_takedown_sources(rights: RightsStatus) -> None:
    source = replace(
        direct_source(
            source_id="blocked",
            kind=SourceKind.VOD_SITE,
            parser=ParserKind.MACCMS_JSON,
            url="https://vod.example/api",
            allowed_hosts=frozenset({"vod.example"}),
            client_site=ClientSiteSpec("vod", "VOD", 1, 1, 1, 1),
        ),
        rights_status=rights,
    )
    client = FakeFetcher(lambda _request: (_ for _ in ()).throw(AssertionError("must not fetch")))
    result = collect_sources(
        sources=(source,),
        http_client=client,
        checked_at="2026-07-22T00:00:00Z",
        network_probes=network_observations(),
    )
    assert not client.requests
    assert result.source_failures["blocked"][1] in {
        FailureReason.RIGHTS_RESTRICTED,
        FailureReason.TAKEDOWN,
    }


def test_collect_vod_config_discards_parse_issues_and_missing_capabilities() -> None:
    source = direct_source(
        source_id="config",
        kind=SourceKind.VOD_CONFIG,
        parser=ParserKind.TVBOX_JSON,
        url="https://config.example/tvbox.json",
        allowed_hosts=frozenset({"config.example", "api.example"}),
    )
    body = json.dumps(
        {
            "sites": [
                "not-an-object",
                {
                    "key": "missing-capabilities",
                    "name": "Missing",
                    "type": 1,
                    "api": "https://api.example/vod",
                },
            ]
        }
    ).encode()
    result = collect_sources(
        sources=(source,),
        http_client=FakeFetcher(lambda request: response(request, body)),
        checked_at="2026-07-22T00:00:00Z",
        network_probes=network_observations(),
    )
    assert result.vod_results == ()
    assert len(result.discarded_entities) == 2
    assert {item.failure_reason for item in result.discarded_entities} == {
        FailureReason.SCHEMA_INCOMPATIBLE
    }


def test_collect_live_discards_playlist_and_credential_url_independently() -> None:
    source = direct_source(
        source_id="live",
        kind=SourceKind.LIVE_PLAYLIST,
        parser=ParserKind.M3U,
        url="https://list.example/live.m3u",
        allowed_hosts=frozenset({"list.example"}),
    )
    playlist = (
        b"#EXTM3U\ninvalid-orphan\n#EXTINF:-1,Secret\n"
        b"https://media.example/live.m3u8?token=secret\n"
    )
    result = collect_sources(
        sources=(source,),
        http_client=FakeFetcher(lambda request: response(request, playlist)),
        checked_at="2026-07-22T00:00:00Z",
        network_probes=network_observations(),
    )
    assert result.live_results == ()
    assert len(result.discarded_entities) == 2
    assert {item.failure_reason for item in result.discarded_entities} == {
        FailureReason.SCHEMA_INCOMPATIBLE,
        FailureReason.CREDENTIAL_QUERY_REJECTED,
    }


def test_collect_converts_invalid_config_contract_to_source_failure() -> None:
    source = direct_source(
        source_id="config",
        kind=SourceKind.VOD_CONFIG,
        parser=ParserKind.TVBOX_JSON,
        url="https://config.example/tvbox.json",
        allowed_hosts=frozenset({"config.example"}),
    )
    result = collect_sources(
        sources=(source,),
        http_client=FakeFetcher(lambda request: response(request, b"not-json")),
        checked_at="2026-07-22T00:00:00Z",
        network_probes=network_observations(),
    )
    assert result.source_failures["config"][1] is FailureReason.INVALID_JSON
    assert result.source_observations[0].enumerated is False


def test_network_observation_window_marks_all_groups_inconclusive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ds_tvbox.collector as collector

    blocked = threading.Event()
    release = threading.Event()

    def http_probe(
        *_args: object, group: str, **_kwargs: object
    ) -> NetworkProbeObservation:
        if group == "github_raw":
            blocked.set()
            release.wait()
        return NetworkProbeObservation(group, True, 1, 0, "ok")

    monkeypatch.setattr(
        collector,
        "_network_http_probe",
        http_probe,
    )
    monkeypatch.setattr(
        collector,
        "_network_dns_probe",
        lambda *_args, **_kwargs: NetworkProbeObservation("dns_public", True, 2, 0, "ok"),
    )
    monkeypatch.setattr(collector, "_NETWORK_OBSERVATION_WINDOW_SECONDS", 0.05)
    started = time.monotonic()
    try:
        observations = run_public_network_probes(
            FakeFetcher(lambda request: response(request, b"unused")),
        )
    finally:
        release.set()
    assert blocked.is_set()
    assert time.monotonic() - started < 0.5
    assert all(not item.passed for item in observations)
    assert {item.detail for item in observations} == {"observation_window_exceeded"}
    assert all(
        worker.daemon
        for worker in threading.enumerate()
        if worker.name.startswith("network-probe-")
    )


def test_blocking_dns_resolver_is_bounded_per_name_without_residual_workers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ds_tvbox.collector as collector

    calls = multiprocessing.get_context("fork").Value("i", 0)

    def resolver(
        host: str,
        port: int,
        family: int,
        socktype: int,
    ) -> Sequence[tuple[int, int, int, str, tuple[str, int]]]:
        _ = host, port, family, socktype
        with calls.get_lock():
            calls.value += 1
        time.sleep(60)
        return ((socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),)

    def handler(request: HttpRequest) -> HttpResponse:
        host = urlsplit(request.url).hostname
        if host == "raw.githubusercontent.com":
            return response(request, b"config docs")
        if host == "www.cloudflare.com":
            return response(request, b"fl=1\nip=203.0.113.1\n")
        if host == "connectivitycheck.gstatic.com":
            return response(request, b"", 204)
        raise AssertionError(request.url)

    monkeypatch.setattr(collector, "_DNS_NAME_TIMEOUT_SECONDS", 0.05)
    started = time.monotonic()
    observations = run_public_network_probes(
        FakeFetcher(handler),
        resolver=resolver,
        sleeper=lambda _: None,
    )
    elapsed = time.monotonic() - started
    dns = next(item for item in observations if item.group == "dns_public")
    workers = [
        worker
        for worker in threading.enumerate()
        if worker.name.startswith("network-probe-dns-name-")
    ]
    assert calls.value == 2
    assert elapsed < 0.5
    assert not dns.passed
    assert dns.detail == "dns_failure"
    assert workers == []
    assert not [
        child
        for child in multiprocessing.active_children()
        if child.name == "ds-tvbox-resolver"
    ]


def test_collect_can_run_fixed_network_probes_when_not_precomputed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ds_tvbox.collector as collector

    monkeypatch.setattr(
        collector,
        "run_public_network_probes",
        lambda *_args, **_kwargs: network_observations(),
    )
    result = collect_sources(
        sources=(),
        http_client=FakeFetcher(lambda request: response(request, b"unused")),
        checked_at="2026-07-22T00:00:00Z",
    )
    assert result.failed_network_groups == 0
    health = result.build_health(generation=1, release_id="release-1")
    assert health["generation"] == 1
