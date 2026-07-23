from __future__ import annotations

import json
from pathlib import Path

import pytest

from ds_tvbox.errors import ContractError
from ds_tvbox.models import FailureReason
from ds_tvbox.parsers import (
    parse_hls,
    parse_json5_data,
    parse_json_data,
    parse_m3u,
    parse_maccms_json,
    parse_maccms_xml,
    parse_tvbox_config,
    parse_txt_live,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "vod"


def test_tvbox_json_and_json5_are_pure_and_resolve_relative_urls() -> None:
    config = parse_tvbox_config(
        """
        {
          // data only -- never eval'd
          sites: [{
            key: 'one', name: 'One', type: 1, api: './api',
            searchable: 1, quickSearch: 0, filterable: 1, changeable: 0,
            categories: ['电影'], header: {'Referer': 'https://example.com/'},
          }],
          lives: [{name: 'Live', type: 0, url: './live.m3u'}],
          urls: [{name: 'Depot', url: './config.json'}],
          storeHouse: [{sourceName: 'Store', sourceUrl: './warehouse.json'}],
          spider: 'https://example.com/a.jar',
        }
        """,
        json5_mode=True,
        base_url="https://example.com/root/config.json",
    )
    assert config.sites[0].api == "https://example.com/root/api"
    assert config.sites[0].quick_search == 0
    assert config.sites[0].declared_headers is not None
    assert config.lives[0].url == "https://example.com/root/live.m3u"
    assert config.urls[0].url.endswith("/root/config.json")
    assert config.storehouses[0].source_url.endswith("/root/warehouse.json")
    assert config.has_spider is True


def test_tvbox_duplicate_json_and_duplicate_site_key_are_rejected() -> None:
    with pytest.raises(ContractError, match="duplicate JSON key"):
        parse_json_data('{"sites": [], "sites": []}')
    with pytest.raises(ContractError, match="duplicate TVBox site key"):
        parse_tvbox_config(
            '{"sites": ['
            '{"key":"same","name":"a","type":1,"api":"https://example.com/a"},'
            '{"key":"same","name":"b","type":1,"api":"https://example.com/b"}'
            "]}"
        )


def test_tvbox_invalid_independent_header_does_not_drop_other_sites() -> None:
    config = parse_tvbox_config(
        """
        {
          "sites": [
            {"key":"bad","name":"Bad","type":1,"api":"https://example.com/a",
             "header":{"X-API-Key":"visible"}},
            {"key":"good","name":"Good","type":1,"api":"https://example.com/b"}
          ]
        }
        """
    )
    assert [site.key for site in config.sites] == ["good"]
    assert len(config.issues) == 1
    assert config.issues[0].failure_reason is FailureReason.CREDENTIAL_HEADER_REJECTED


def test_tvbox_spider_presence_accepts_structured_untrusted_value() -> None:
    config = parse_tvbox_config('{"spider":{"url":"https://example.com/a.jar"}}')
    assert config.has_spider is True


def test_m3u_parses_metadata_and_all_allowed_header_syntaxes() -> None:
    result = parse_m3u(
        """#EXTM3U
#EXTINF:-1 tvg-id="News.ID" tvg-logo="logo.png" group-title="News",News Channel
#EXTHTTP:{"Accept":"application/vnd.apple.mpegurl"}
#EXTVLCOPT:http-user-agent=TVBox
stream/index.m3u8|Referer=https%3A%2F%2Fexample.com%2F
""",
        base_url="https://example.com/list/main.m3u",
    )
    assert not result.issues
    entry = result.entries[0]
    assert entry.name == "News Channel"
    assert entry.tvg_id == "News.ID"
    assert entry.group == "News"
    assert entry.url == "https://example.com/list/stream/index.m3u8"
    assert entry.declared_headers is not None
    assert set(entry.declared_headers.values) == {"Accept", "Referer", "User-Agent"}


def test_m3u_rejects_only_entity_with_unknown_or_sensitive_header() -> None:
    result = parse_m3u(
        """#EXTM3U
#EXTINF:-1,Bad
#EXTVLCOPT:http-cookie=secret
https://example.com/bad.m3u8
#EXTINF:-1,Good
https://example.com/good.m3u8
"""
    )
    assert [item.name for item in result.entries] == ["Good"]
    assert len(result.issues) == 1
    assert result.issues[0].failure_reason is FailureReason.CREDENTIAL_HEADER_REJECTED


def test_txt_live_tracks_groups_and_parses_url_headers() -> None:
    result = parse_txt_live(
        """News,#genre#
CCTV 1,https://example.com/cctv1.m3u8
CCTV 2,stream/cctv2.m3u8|User-Agent=TVBox
broken line
""",
        base_url="https://example.com/list.txt",
    )
    assert [item.group for item in result.entries] == ["News", "News"]
    assert result.entries[1].url == "https://example.com/stream/cctv2.m3u8"
    assert len(result.issues) == 1


def test_hls_master_and_media_are_structurally_distinguished() -> None:
    master = parse_hls(
        """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-STREAM-INF:BANDWIDTH=3000000,RESOLUTION=1920x1080
high/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360
low/index.m3u8
""",
        base_url="https://media.example.com/master.m3u8",
    )
    assert master.kind == "master"
    assert master.variants[0].width == 1920
    assert master.variants[0].bandwidth == 3_000_000
    assert master.variants[0].uri == "https://media.example.com/high/index.m3u8"

    media = parse_hls(
        """#EXTM3U
#EXT-X-TARGETDURATION:10
#EXT-X-MAP:URI="init.mp4"
#EXTINF:10,
segment-1.ts
#EXTINF:10,
segment-2.ts
""",
        base_url="https://media.example.com/path/index.m3u8",
    )
    assert media.kind == "media"
    assert media.initialization_map is not None
    assert media.initialization_map.uri.endswith("/path/init.mp4")
    assert [segment.uri.rsplit("/", 1)[-1] for segment in media.segments] == [
        "segment-1.ts",
        "segment-2.ts",
    ]


def test_hls_rejects_plain_m3u_and_missing_variant_uri() -> None:
    with pytest.raises(ContractError, match="playable"):
        parse_hls("#EXTM3U\nhttps://example.com/not-hls.ts\n")
    with pytest.raises(ContractError, match="variant has no URI"):
        parse_hls("#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1000\n")


def test_maccms_json_home_and_detail_share_canonical_model() -> None:
    home = parse_maccms_json((FIXTURES / "home.json").read_bytes())
    detail = parse_maccms_json((FIXTURES / "detail.json").read_bytes())
    assert home.classes[0].type_id == "1"
    assert home.videos[0].vod_name == "测试影片"
    assert not home.videos[0].play_lines
    selected = detail.detail("101")
    assert selected.play_lines[0].name == "hls"
    assert selected.play_lines[0].episodes[0].url.endswith("index.m3u8")


def test_maccms_xml_home_and_detail_share_canonical_model() -> None:
    home = parse_maccms_xml((FIXTURES / "home.xml").read_bytes())
    detail = parse_maccms_xml((FIXTURES / "detail.xml").read_bytes())
    assert home.classes[0].type_name == "电影"
    assert detail.detail("101").play_lines[0].episodes[0].title == "正片"


def test_maccms_contract_failures_are_stable() -> None:
    with pytest.raises(ContractError, match="vod_name"):
        parse_maccms_json((FIXTURES / "missing.json").read_bytes())
    with pytest.raises(ContractError, match="line counts"):
        parse_maccms_json(
            '{"list":[{"vod_id":"1","vod_name":"x",'
            '"vod_play_from":"a$$$b","vod_play_url":"x$https://example.com/x"}]}'
        )
    with pytest.raises(ContractError, match="invalid or unsafe"):
        parse_maccms_xml(
            '<!DOCTYPE rss [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
            "<rss><list><video><id>1</id><name>&xxe;</name></video></list></rss>"
        )


def test_json_decoders_reject_non_utf8_and_invalid_json5() -> None:
    with pytest.raises(ContractError, match="UTF-8"):
        parse_json_data(b"\xff")
    with pytest.raises(ContractError, match="invalid JSON"):
        parse_json_data("{")
    with pytest.raises(ContractError, match="invalid JSON5"):
        parse_json5_data("{unterminated:")
    assert parse_json_data("\ufeff{}") == {}


def test_tvbox_root_arrays_and_independent_entries_fail_locally() -> None:
    with pytest.raises(ContractError, match="root must be an object"):
        parse_tvbox_config("[]")
    with pytest.raises(ContractError, match="sites must be an array"):
        parse_tvbox_config('{"sites":{}}')
    assert parse_tvbox_config('{"sites":null}').sites == ()

    config = parse_tvbox_config(
        """
        {
          "sites": [
            "bad",
            {"key":"", "name":"x", "type":1, "api":"https://example.com"},
            {"key":"type", "name":"x", "type":2, "api":"https://example.com"},
            {"key":"caps", "name":"x", "type":1, "api":"https://example.com",
             "searchable":true},
            {"key":"categories", "name":"x", "type":1,
             "api":"https://example.com", "categories":[""]}
          ],
          "lives": ["bad", {"name":"x", "type":true, "url":"u"}],
          "urls": ["bad", {"name":"", "url":"u"}],
          "storeHouse": ["bad", {"sourceName":"x", "sourceUrl":""}]
        }
        """
    )
    assert not config.sites and not config.lives and not config.urls and not config.storehouses
    assert len(config.issues) == 11


def test_m3u_records_structural_entity_errors_without_losing_good_entries() -> None:
    with pytest.raises(ContractError, match="start"):
        parse_m3u("#EXTINF:-1,News\nhttps://example.com/live.m3u8")

    result = parse_m3u(
        """#EXTM3U
https://example.com/orphan.m3u8
#EXTINF:-1 no-separator
https://example.com/bad-meta.m3u8
#EXTINF:-1,
https://example.com/empty-name.m3u8
#EXTINF:-1 tvg-id="a" tvg-id="b",Duplicate Attribute
https://example.com/duplicate.m3u8
#EXTINF:-1,Unknown Option
#EXTVLCOPT:unknown=value
https://example.com/unknown.m3u8
#EXTINF:-1,Malformed Option
#EXTVLCOPT:missing-equals
https://example.com/malformed.m3u8
#EXTINF:-1,Duplicate Header
#EXTHTTP:{"Accept":"one"}
#EXTHTTP:{"Accept":"two"}
https://example.com/header.m3u8
#EXTINF:-1,No URL
"""
    )
    assert result.entries == ()
    assert len(result.issues) == 8
    assert {issue.failure_reason for issue in result.issues} == {
        FailureReason.SCHEMA_INCOMPATIBLE,
        FailureReason.CREDENTIAL_HEADER_REJECTED,
        FailureReason.INVALID_HEADER_SYNTAX,
    }


def test_txt_live_empty_groups_channels_and_headers_are_independent_issues() -> None:
    result = parse_txt_live(
        """// comment
# comment
,#genre#
,https://example.com/empty-name.m3u8
Name,
Bad Header,https://example.com/x|Cookie=secret
Good,relative.m3u8
""",
        base_url="https://example.com/list.txt",
    )
    assert [entry.name for entry in result.entries] == ["Good"]
    assert result.entries[0].url == "https://example.com/relative.m3u8"
    assert len(result.issues) == 4


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("", "start"),
        ("#EXTM3U\n#EXT-X-MAP:BYTERANGE=10\nsegment.ts", "MAP has no URI"),
        (
            "#EXTM3U\n#EXT-X-STREAM-INF:RESOLUTION=bad,BANDWIDTH=100\nvideo.m3u8",
            "RESOLUTION",
        ),
        (
            "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=zero\nvideo.m3u8",
            "BANDWIDTH",
        ),
        ("#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=0\nvideo.m3u8", "BANDWIDTH"),
        (
            "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=100\nmaster.m3u8\n"
            "#EXT-X-TARGETDURATION:10\nsegment.ts",
            "cannot mix",
        ),
        (
            '#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH="unterminated\nvideo.m3u8',
            "attribute list",
        ),
    ],
)
def test_hls_rejects_malformed_structures(text: str, message: str) -> None:
    with pytest.raises(ContractError, match=message):
        parse_hls(text)


def test_hls_resources_preserve_url_line_headers() -> None:
    result = parse_hls(
        """#EXTM3U
#EXT-X-TARGETDURATION:10
#EXT-X-MAP:URI="init.mp4|User-Agent=TVBox"
#EXTINF:10,
segment.ts|Referer=https%3A%2F%2Fexample.com%2F
""",
        base_url="https://example.com/path/master.m3u8",
    )
    assert result.initialization_map is not None
    assert result.initialization_map.declared_headers is not None
    assert result.segments[0].declared_headers is not None


def test_maccms_detail_and_container_contracts() -> None:
    response = parse_maccms_json(
        '{"list":[{"vod_id":"1","vod_name":"One"},'
        '{"vod_id":"1","vod_name":"Duplicate"}]}'
    )
    with pytest.raises(ContractError, match="exactly one"):
        response.detail("1")
    with pytest.raises(ContractError, match="exactly one"):
        response.detail("missing")
    no_play = parse_maccms_json('{"list":[{"vod_id":"1","vod_name":"One"}]}')
    with pytest.raises(ContractError, match="no valid playback"):
        no_play.detail("1")

    for payload, message in (
        ("[]", "root"),
        ('{"class":{},"list":[]}', "must be arrays"),
        ('{"class":[1]}', "class entry"),
        ('{"class":[{"type_id":true,"type_name":"x"}]}', "identifier"),
        ('{"list":[1]}', "video entry"),
        ('{"list":[{"vod_id":" ","vod_name":"x"}]}', "non-empty"),
    ):
        with pytest.raises(ContractError, match=message):
            parse_maccms_json(payload)


@pytest.mark.parametrize(
    ("play_from", "play_url", "message"),
    [
        (1, "x$https://example.com", "both be strings"),
        ("line", "", "line is empty"),
        ("", "x$https://example.com", "line is empty"),
        ("line", "episode-without-separator", "no title/URL separator"),
        ("line", "Episode$", "URL is empty"),
        ("line", "Episode$ftp://example.com/x", "invalid scheme"),
    ],
)
def test_maccms_playback_field_failures(
    play_from: object,
    play_url: object,
    message: str,
) -> None:
    payload = {
        "list": [
            {
                "vod_id": "1",
                "vod_name": "One",
                "vod_play_from": play_from,
                "vod_play_url": play_url,
            }
        ]
    }
    with pytest.raises(ContractError, match=message):
        parse_maccms_json(json.dumps(payload))


@pytest.mark.parametrize(
    ("xml", "message"),
    [
        ("<root/>", "root must be rss"),
        ('<rss><class><ty id="1"></ty></class></rss>', "class name is empty"),
        ("<rss><list><video><name>One</name></video></list></rss>", "video id"),
        ("<rss><list><video><id>1</id></video></list></rss>", "video name is empty"),
    ],
)
def test_maccms_xml_requires_canonical_root_and_entities(xml: str, message: str) -> None:
    with pytest.raises(ContractError, match=message):
        parse_maccms_xml(xml)
