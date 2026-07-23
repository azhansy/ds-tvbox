from __future__ import annotations

import json
import socket
from collections.abc import Callable, Mapping
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from ds_tvbox import parsers as canonical_parsers
from ds_tvbox.errors import ContractError
from ds_tvbox.models import (
    DeclaredHeaders,
    FailureReason,
    MediaProbeResult,
    PublicationStatus,
    RightsStatus,
    TechnicalStatus,
    VodSiteCandidate,
)
from ds_tvbox.vod import (
    HttpResponse,
    ProbeRequestError,
    build_maccms_url,
    extract_play_urls,
    parse_maccms_json,
    parse_maccms_xml,
    probe_type4,
    probe_vod,
)

FIXTURES = Path(__file__).parents[1] / "fixtures" / "vod"


class FakeClient:
    def __init__(self, responder: Callable[[str, Mapping[str, str] | None], HttpResponse]) -> None:
        self.responder = responder
        self.calls: list[tuple[str, Mapping[str, str] | None, int | None]] = []

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        max_bytes: int | None = None,
    ) -> HttpResponse:
        self.calls.append((url, headers, max_bytes))
        return self.responder(url, headers)


class FakeMediaProber:
    def __init__(self, result: MediaProbeResult) -> None:
        self.result = result
        self.calls: list[tuple[str, Mapping[str, str] | None]] = []

    def probe(
        self, url: str, *, headers: Mapping[str, str] | None = None
    ) -> MediaProbeResult:
        self.calls.append((url, headers))
        return self.result


def candidate(
    site_type: int = 1,
    *,
    rights: RightsStatus = RightsStatus.PUBLIC_UNVERIFIED,
    headers: DeclaredHeaders | None = None,
    raw: Mapping[str, object] | None = None,
    api: str = "https://vod.example.test/api.php/provide/vod?fixed=yes&ac=list",
) -> VodSiteCandidate:
    return VodSiteCandidate(
        source_id="source-a",
        key="a",
        name="示例",
        type=site_type,
        api=api,
        searchable=1,
        quick_search=1,
        filterable=1,
        changeable=1,
        categories=(),
        rights_status=rights,
        declared_headers=headers,
        raw=raw or {},
    )


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def healthy_media() -> MediaProbeResult:
    return MediaProbeResult(
        ok=True,
        final_url="https://media.example.test/movie/index.m3u8",
        response_ms=25,
        media_path_score=1,
    )


def json_responder(url: str, headers: Mapping[str, str] | None) -> HttpResponse:
    del headers
    query = parse_qs(urlsplit(url).query, keep_blank_values=True)
    if "ids" in query:
        body = fixture("detail.json")
    elif "wd" in query:
        body = fixture("search.json")
    else:
        body = fixture("home.json")
    return HttpResponse(200, body, url, 5)


def xml_responder(url: str, headers: Mapping[str, str] | None) -> HttpResponse:
    del headers
    query = parse_qs(urlsplit(url).query, keep_blank_values=True)
    body = fixture("detail.xml") if "ids" in query else fixture("home.xml")
    return HttpResponse(200, body, url, 5)


def test_build_maccms_urls_preserve_unrelated_fixed_query() -> None:
    api = "https://example.test/api?foo=1&ac=old&pg=99"
    category = build_maccms_url(api, 1, "category", type_id="7", page=2, filters={"b": 2})
    query = parse_qs(urlsplit(category).query)
    assert query == {"foo": ["1"], "ac": ["detail"], "t": ["7"], "pg": ["2"], "f": ['{"b":2}']}

    search = build_maccms_url(api, 1, "search", keyword="标题")
    search_query = parse_qs(urlsplit(search).query, keep_blank_values=True)
    assert search_query["ac"] == ["old"]
    assert "pg" not in search_query
    assert search_query["wd"] == ["标题"]
    assert search_query["quick"] == ["false"]
    assert search_query["extend"] == [""]

    detail = build_maccms_url(api, 0, "detail", vod_id="42")
    detail_query = parse_qs(urlsplit(detail).query)
    assert detail_query["ac"] == ["videolist"]
    assert detail_query["ids"] == ["42"]


def test_home_url_is_byte_for_byte_original_and_invalid_args_fail() -> None:
    api = "https://example.test/api?x=%2F"
    assert build_maccms_url(api, 1, "home") == api
    with pytest.raises(ValueError):
        build_maccms_url(api, 4, "home")
    with pytest.raises(ValueError):
        build_maccms_url(api, 1, "category")
    with pytest.raises(ValueError):
        build_maccms_url(api, 1, "search")
    with pytest.raises(ValueError):
        build_maccms_url(api, 1, "detail")
    with pytest.raises(ValueError):
        build_maccms_url(api, 1, "search", keyword="x", page=0)
    with pytest.raises(ValueError):
        build_maccms_url(api, 1, "other")


def test_json_and_xml_parsers_and_play_routes() -> None:
    json_home = parse_maccms_json(fixture("home.json"))
    xml_home = parse_maccms_xml(fixture("home.xml"))
    assert json_home.categories == (("1", "电影"),)
    assert xml_home.videos[0].vod_id == "101"
    detail = parse_maccms_json(fixture("detail.json")).videos[0]
    assert extract_play_urls(detail) == (
        "https://media.example.test/movie/index.m3u8",
    )


def test_probe_vod_uses_one_canonical_parser_for_home_search_and_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = canonical_parsers.parse_maccms_json
    parsed_bodies: list[bytes | str] = []

    def tracked(data: bytes | str) -> canonical_parsers.MacCmsResponse:
        parsed_bodies.append(data)
        return original(data)

    monkeypatch.setattr(canonical_parsers, "parse_maccms_json", tracked)
    result = probe_vod(candidate(), FakeClient(json_responder), FakeMediaProber(healthy_media()))

    assert result.technical_status is TechnicalStatus.HEALTHY
    assert parsed_bodies == [
        fixture("home.json"),
        fixture("search.json"),
        fixture("detail.json"),
    ]


def test_parsers_reject_missing_fields_and_html_error() -> None:
    with pytest.raises(ProbeRequestError) as missing:
        parse_maccms_json(fixture("missing.json"))
    assert missing.value.reason is FailureReason.SCHEMA_INCOMPATIBLE
    with pytest.raises(ProbeRequestError) as html:
        parse_maccms_json(b"<html><body>captcha</body></html>")
    assert html.value.reason is FailureReason.HOME_CONTRACT_FAILED
    with pytest.raises(ProbeRequestError) as xml:
        parse_maccms_xml(b"<not-rss />")
    assert xml.value.reason is FailureReason.SCHEMA_INCOMPATIBLE
    with pytest.raises(ProbeRequestError) as malformed:
        parse_maccms_xml(b"<rss>")
    assert malformed.value.reason is FailureReason.INVALID_XML
    with pytest.raises(ProbeRequestError) as encoding:
        parse_maccms_json(b"\xff")
    assert encoding.value.reason is FailureReason.INVALID_JSON


def test_duplicate_json_rejection_is_shared_by_adapter_and_health_probe() -> None:
    duplicate = (
        b'{"list":[],"list":'
        b'[{"vod_id":"101","vod_name":"shadowed duplicate"}]}'
    )
    with pytest.raises(ContractError, match="duplicate JSON key"):
        canonical_parsers.parse_maccms_json(duplicate)
    with pytest.raises(ProbeRequestError) as adapted:
        parse_maccms_json(duplicate)
    assert adapted.value.reason is FailureReason.INVALID_JSON

    client = FakeClient(lambda url, headers: HttpResponse(200, duplicate, url))
    result = probe_vod(candidate(), client, FakeMediaProber(healthy_media()))
    assert result.failure_reason is FailureReason.INVALID_JSON
    assert client.calls and len(client.calls) == 1


def test_unsafe_xml_rejection_is_shared_by_adapter_and_health_probe() -> None:
    malicious = (
        b'<!DOCTYPE rss [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
        b"<rss><list><video><id>1</id><name>&xxe;</name></video></list></rss>"
    )
    with pytest.raises(ContractError, match="invalid or unsafe"):
        canonical_parsers.parse_maccms_xml(malicious)
    with pytest.raises(ProbeRequestError) as adapted:
        parse_maccms_xml(malicious)
    assert adapted.value.reason is FailureReason.INVALID_XML

    client = FakeClient(lambda url, headers: HttpResponse(200, malicious, url))
    result = probe_vod(candidate(0), client, FakeMediaProber(healthy_media()))
    assert result.failure_reason is FailureReason.INVALID_XML
    assert client.calls and len(client.calls) == 1


def test_play_contract_rejects_missing_or_mismatched_routes() -> None:
    entry = parse_maccms_json(fixture("home.json")).videos[0]
    with pytest.raises(ProbeRequestError):
        extract_play_urls(entry)
    broken = parse_maccms_json(fixture("detail.json")).videos[0]
    broken = type(broken)(broken.vod_id, broken.name, "a$$$b", "name$https://x")
    with pytest.raises(ProbeRequestError):
        extract_play_urls(broken)
    with pytest.raises(ProbeRequestError) as canonical_failure:
        parse_maccms_json(
            b'{"list":[{"vod_id":"1","vod_name":"x",'
            b'"vod_play_from":"a$$$b","vod_play_url":"name$https://x"}]}'
        )
    assert canonical_failure.value.reason is FailureReason.PLAY_CONTRACT_FAILED


@pytest.mark.parametrize("site_type", [0, 1])
def test_probe_vod_completes_full_maccms_chain(site_type: int) -> None:
    client = FakeClient(xml_responder if site_type == 0 else json_responder)
    media = FakeMediaProber(healthy_media())
    result = probe_vod(candidate(site_type), client, media)
    assert result.technical_status is TechnicalStatus.HEALTHY
    assert result.publication_status is PublicationStatus.STABLE
    assert result.capabilities.media_probe
    assert result.sample_title == "测试影片"
    assert result.sample_vod_id == "101"
    assert len(client.calls) == 3
    assert media.calls[0][0].endswith("index.m3u8")


def test_empty_search_is_partial_but_detail_and_media_are_still_checked() -> None:
    def responder(url: str, headers: Mapping[str, str] | None) -> HttpResponse:
        del headers
        query = parse_qs(urlsplit(url).query)
        if "ids" in query:
            body = fixture("detail.json")
        elif "wd" in query:
            body = b'{"list": []}'
        else:
            body = fixture("home.json")
        return HttpResponse(200, body, url)

    result = probe_vod(candidate(), FakeClient(responder), FakeMediaProber(healthy_media()))
    assert result.technical_status is TechnicalStatus.PARTIAL
    assert result.publication_status is PublicationStatus.EXPERIMENTAL
    assert not result.capabilities.search
    assert result.capabilities.media_probe


def test_unrelated_search_result_cannot_be_reported_healthy() -> None:
    def responder(url: str, headers: Mapping[str, str] | None) -> HttpResponse:
        del headers
        query = parse_qs(urlsplit(url).query)
        if "ids" in query:
            body = fixture("detail.json")
        elif "wd" in query:
            body = b'{"list":[{"vod_id":"999","vod_name":"unrelated"}]}'
        else:
            body = fixture("home.json")
        return HttpResponse(200, body, url)

    result = probe_vod(candidate(), FakeClient(responder), FakeMediaProber(healthy_media()))
    assert result.technical_status is TechnicalStatus.PARTIAL
    assert result.publication_status is PublicationStatus.EXPERIMENTAL
    assert not result.capabilities.search


def test_detail_title_must_match_the_selected_search_result() -> None:
    def responder(url: str, headers: Mapping[str, str] | None) -> HttpResponse:
        del headers
        query = parse_qs(urlsplit(url).query)
        if "ids" in query:
            body = (
                b'{"list":[{"vod_id":"101","vod_name":"wrong title",'
                b'"vod_play_from":"line","vod_play_url":'
                b'"episode$https://media.example.test/movie.m3u8"}]}'
            )
        elif "wd" in query:
            body = fixture("search.json")
        else:
            body = fixture("home.json")
        return HttpResponse(200, body, url)

    result = probe_vod(candidate(), FakeClient(responder), FakeMediaProber(healthy_media()))
    assert result.failure_reason is FailureReason.DETAIL_CONTRACT_FAILED
    assert result.technical_status is TechnicalStatus.DEAD


def test_home_with_only_categories_fetches_category_before_search() -> None:
    def responder(url: str, headers: Mapping[str, str] | None) -> HttpResponse:
        del headers
        query = parse_qs(urlsplit(url).query)
        if "ids" in query:
            body = fixture("detail.json")
        elif "wd" in query:
            body = fixture("search.json")
        elif "t" in query:
            body = fixture("home.json")
        else:
            body = '{"class":[{"type_id":1,"type_name":"电影"}]}'.encode()
        return HttpResponse(200, body, url)

    client = FakeClient(responder)
    result = probe_vod(candidate(), client, FakeMediaProber(healthy_media()))
    assert result.technical_status is TechnicalStatus.HEALTHY
    assert len(client.calls) == 4


@pytest.mark.parametrize(
    ("reason", "technical", "publication"),
    [
        (FailureReason.HTTP_404, TechnicalStatus.DEAD, PublicationStatus.WITHHELD),
        (FailureReason.HTTP_410, TechnicalStatus.DEAD, PublicationStatus.WITHHELD),
        (FailureReason.DNS_FAILURE, TechnicalStatus.DEAD, PublicationStatus.WITHHELD),
        (FailureReason.TLS_FAILURE, TechnicalStatus.DEAD, PublicationStatus.WITHHELD),
        (FailureReason.RATE_LIMITED, TechnicalStatus.SUSPECT, PublicationStatus.WITHHELD),
        (FailureReason.UPSTREAM_5XX, TechnicalStatus.SUSPECT, PublicationStatus.WITHHELD),
        (FailureReason.FETCH_TIMEOUT, TechnicalStatus.SUSPECT, PublicationStatus.WITHHELD),
        (
            FailureReason.MEDIA_PROBE_FAILED,
            TechnicalStatus.PARTIAL,
            PublicationStatus.EXPERIMENTAL,
        ),
    ],
)
def test_media_probe_failure_preserves_failure_classification(
    reason: FailureReason,
    technical: TechnicalStatus,
    publication: PublicationStatus,
) -> None:
    failed_media = MediaProbeResult(
        ok=False,
        final_url=None,
        response_ms=100,
        media_path_score=0,
        failure_reason=reason,
    )
    result = probe_vod(candidate(), FakeClient(json_responder), FakeMediaProber(failed_media))
    assert result.technical_status is technical
    assert result.publication_status is publication
    assert result.failure_reason is reason
    assert not result.capabilities.media_probe


@pytest.mark.parametrize(
    ("unsafe_url", "reason"),
    [
        (
            "https://user:password@media.example.test/movie.m3u8",
            FailureReason.CREDENTIAL_QUERY_REJECTED,
        ),
        (
            "https://media.example.test/movie.m3u8?token=visible",
            FailureReason.CREDENTIAL_QUERY_REJECTED,
        ),
        ("http://media.example.test/movie.m3u8", FailureReason.CLIENT_HTTP_DISALLOWED),
        ("https://127.0.0.1/movie.m3u8", FailureReason.PRIVATE_ADDRESS_REJECTED),
        ("file:///tmp/movie.mp4", FailureReason.DANGEROUS_SCHEME_REJECTED),
    ],
)
def test_every_detail_play_url_is_offline_security_validated_before_sampling(
    unsafe_url: str,
    reason: FailureReason,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_dns(*_args: object) -> object:
        raise AssertionError("offline play URL validation must not resolve DNS")

    monkeypatch.setattr(socket, "getaddrinfo", unexpected_dns)
    detail = json.dumps(
        {
            "list": [
                {
                    "vod_id": 101,
                    "vod_name": "测试影片",
                    "vod_play_from": "hls",
                    "vod_play_url": (
                        "正片$https://media.example.test/movie/index.m3u8"
                        f"#恶意备用线路${unsafe_url}"
                    ),
                }
            ]
        },
        ensure_ascii=False,
    ).encode()

    def responder(url: str, headers: Mapping[str, str] | None) -> HttpResponse:
        del headers
        query = parse_qs(urlsplit(url).query, keep_blank_values=True)
        if "ids" in query:
            body = detail
        elif "wd" in query:
            body = fixture("search.json")
        else:
            body = fixture("home.json")
        return HttpResponse(200, body, url, 5)

    media = FakeMediaProber(healthy_media())
    client = FakeClient(responder)
    result = probe_vod(
        candidate(headers=DeclaredHeaders({"Referer": "https://origin.example.test/"})),
        client,
        media,
    )

    assert result.failure_reason is reason
    assert result.publication_status is PublicationStatus.REJECTED
    assert media.calls == []
    assert all(headers is None for _url, headers, _max_bytes in client.calls)


def test_http_404_is_dead_and_withheld() -> None:
    client = FakeClient(lambda url, headers: HttpResponse(404, b"missing", url))
    result = probe_vod(candidate(), client, FakeMediaProber(healthy_media()))
    assert result.technical_status is TechnicalStatus.DEAD
    assert result.publication_status is PublicationStatus.WITHHELD
    assert result.failure_reason is FailureReason.HTTP_404


@pytest.mark.parametrize("status", [401, 403])
def test_http_auth_status_requires_credentials(status: int) -> None:
    client = FakeClient(lambda url, headers: HttpResponse(status, b"forbidden", url))
    result = probe_vod(candidate(), client, FakeMediaProber(healthy_media()))

    assert result.failure_reason is FailureReason.CREDENTIAL_REQUIRED
    assert result.publication_status is PublicationStatus.REJECTED


def test_media_auth_status_requires_credentials() -> None:
    auth_media = MediaProbeResult(
        ok=False,
        final_url=None,
        response_ms=10,
        media_path_score=0,
        failure_reason=FailureReason.CREDENTIAL_REQUIRED,
    )
    result = probe_vod(candidate(), FakeClient(json_responder), FakeMediaProber(auth_media))

    assert result.failure_reason is FailureReason.CREDENTIAL_REQUIRED
    assert result.publication_status is PublicationStatus.REJECTED


def test_declared_header_is_diagnostic_only() -> None:
    def responder(url: str, headers: Mapping[str, str] | None) -> HttpResponse:
        if not headers:
            return HttpResponse(403, b"forbidden", url)
        return json_responder(url, headers)

    site = candidate(headers=DeclaredHeaders({"Referer": "https://origin.example.test/"}))
    result = probe_vod(site, FakeClient(responder), FakeMediaProber(healthy_media()))
    assert result.technical_status is TechnicalStatus.PARTIAL
    assert result.publication_status is PublicationStatus.WITHHELD
    assert result.failure_reason is FailureReason.CLIENT_HEADER_UNSUPPORTED
    assert FailureReason.CREDENTIAL_REQUIRED in result.secondary_reasons


def test_type4_static_contract_success_and_failures() -> None:
    ok_client = FakeClient(lambda url, headers: HttpResponse(200, b'{"ok":true}', url))
    result = probe_type4(candidate(4, api="https://ext.example.test/api"), ok_client)
    assert result.technical_status is TechnicalStatus.PARTIAL
    assert result.publication_status is PublicationStatus.EXPERIMENTAL

    http = probe_type4(candidate(4, api="http://ext.example.test/api"), ok_client)
    assert http.failure_reason is FailureReason.CLIENT_HTTP_DISALLOWED
    assert http.publication_status is PublicationStatus.REJECTED

    credential = probe_type4(
        candidate(4, api="https://ext.example.test/api?auth=testpub"), ok_client
    )
    assert credential.failure_reason is FailureReason.CREDENTIAL_QUERY_REJECTED
    assert credential.publication_status is PublicationStatus.REJECTED

    extension = probe_type4(
        candidate(4, api="https://ext.example.test/api", raw={"ext": "remote"}), ok_client
    )
    assert extension.failure_reason is FailureReason.CLIENT_EXTENSION_UNSUPPORTED
    assert extension.publication_status is PublicationStatus.WITHHELD

    executable = probe_type4(
        candidate(4, api="https://ext.example.test/api", raw={"nested": ["x.JS"]}),
        ok_client,
    )
    assert executable.failure_reason is FailureReason.UNSUPPORTED_SPIDER

    invalid_utf8 = probe_type4(
        candidate(4, api="https://ext.example.test/api"),
        FakeClient(lambda url, headers: HttpResponse(200, b"\xff", url)),
    )
    assert invalid_utf8.failure_reason is FailureReason.SCHEMA_INCOMPATIBLE


def test_type4_header_only_success_is_withheld() -> None:
    def responder(url: str, headers: Mapping[str, str] | None) -> HttpResponse:
        return HttpResponse(200 if headers else 403, b"ok", url)

    site = candidate(
        4,
        api="https://ext.example.test/api",
        headers=DeclaredHeaders({"Referer": "https://origin.example.test/"}),
    )
    result = probe_type4(site, FakeClient(responder))
    assert result.technical_status is TechnicalStatus.PARTIAL
    assert result.publication_status is PublicationStatus.WITHHELD
    assert result.failure_reason is FailureReason.CLIENT_HEADER_UNSUPPORTED
    assert FailureReason.CREDENTIAL_REQUIRED in result.secondary_reasons


def test_type3_is_never_published_or_executed() -> None:
    client = FakeClient(lambda url, headers: HttpResponse(200, b"unexpected", url))
    result = probe_vod(candidate(3), client, FakeMediaProber(healthy_media()))
    assert result.technical_status is TechnicalStatus.PARTIAL
    assert result.publication_status is PublicationStatus.REJECTED
    assert result.failure_reason is FailureReason.UNSUPPORTED_SPIDER
    assert client.calls == []
