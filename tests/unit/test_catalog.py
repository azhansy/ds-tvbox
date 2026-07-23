from __future__ import annotations

import json
from collections.abc import Callable

from ds_tvbox.catalog import scan_catalog
from ds_tvbox.http import HttpRequest, HttpResponse
from ds_tvbox.models import (
    FetchMode,
    FetchSpec,
    ParserKind,
    RightsStatus,
    SourceKind,
    SourceSpec,
)
from ds_tvbox.upstream import GitHubSnapshot, UpstreamSnapshot, github_raw_url

REVISION = "1" * 40
TREE = "2" * 40


class FakeFetcher:
    def __init__(self, handler: Callable[[HttpRequest], HttpResponse]) -> None:
        self.handler = handler
        self.requests: list[HttpRequest] = []

    def fetch(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        return self.handler(request)


def response(url: str, body: bytes, status: int = 200) -> HttpResponse:
    return HttpResponse(status, url, (), body, 1, 1, 0)


def catalog_source(*, max_candidates: int = 20, max_files: int = 20) -> SourceSpec:
    catalog = {
        "max_depth": 2,
        "path_globs": ["configs/**/*.json", "playlists/**/*.m3u"],
        "parsers_by_glob": [
            {"glob": "configs/**/*.json", "parser": "tvbox_json"},
            {"glob": "playlists/**/*.m3u", "parser": "m3u"},
        ],
        "selectors": {
            "sites_arrays": ["/sites"],
            "depot_arrays": ["/urls"],
            "storehouse_arrays": ["/storeHouse"],
            "live_arrays": ["/lives"],
        },
        "max_files": max_files,
        "max_candidates": max_candidates,
        "max_live_urls": 10,
        "allowed_downstream_hosts": [
            "api.example.test",
            "config.example.test",
            "media.example.test",
        ],
        "auto_onboard_public_unverified": True,
    }
    return SourceSpec(
        id="catalog-source",
        kind=SourceKind.REPOSITORY_CATALOG,
        parser=ParserKind.REPOSITORY_CATALOG,
        enabled=True,
        fetch=FetchSpec(
            FetchMode.GITHUB_REPOSITORY,
            None,
            "https://github.com/example/repo",
            "main",
            None,
            "0" * 40,
        ),
        terms_watch=(),
        rights_status=RightsStatus.OPEN_LICENSE,
        config_license_status="verified",
        content_rights_status="unverified",
        allowed_hosts=frozenset({"api.github.com", "raw.githubusercontent.com"}),
        allow_discovered_media_hosts=False,
        http_exceptions=(),
        denied_categories=(),
        client_site=None,
        catalog=catalog,
        raw={},
    )


def snapshot() -> UpstreamSnapshot:
    github = GitHubSnapshot(
        "https://github.com/example/repo",
        "example",
        "repo",
        REVISION,
        TREE,
    )
    return UpstreamSnapshot(
        source_id="catalog-source",
        fetch_mode=FetchMode.GITHUB_REPOSITORY,
        resolved_revision=REVISION,
        resolved_fetch_url=None,
        content=None,
        content_sha256=None,
        terms_sha256={},
        github=github,
    )


def fake_catalog(
    tree_paths: list[str],
    files: dict[str, bytes],
) -> FakeFetcher:
    tree_url = f"https://api.github.com/repos/example/repo/git/trees/{TREE}?recursive=1"
    tree_body = json.dumps(
        {
            "truncated": False,
            "tree": [
                {"path": path, "type": "blob", "sha": str(index + 3) * 40, "size": 10}
                for index, path in enumerate(tree_paths)
            ],
        }
    ).encode()

    def handler(request: HttpRequest) -> HttpResponse:
        if request.url == tree_url:
            return response(request.url, tree_body)
        for path, body in files.items():
            if request.url == github_raw_url("https://github.com/example/repo", REVISION, path):
                return response(request.url, body)
        raise AssertionError(f"unexpected URL: {request.url}")

    return FakeFetcher(handler)


def test_catalog_candidates_are_deterministic_unknown_and_report_only() -> None:
    root = json.dumps(
        {
            "sites": [
                {
                    "key": "vod",
                    "name": "VOD",
                    "type": 1,
                    "api": "https://api.example.test/vod",
                }
            ],
            "urls": [{"name": "child", "url": "https://config.example.test/child.json"}],
            "storeHouse": [],
            "lives": [],
        }
    ).encode()
    playlist = b"""#EXTM3U
#EXTINF:-1 tvg-id="news",News
https://media.example.test/live.m3u8
#EXTINF:-1 tvg-id="news-dup",News duplicate
https://media.example.test/live.m3u8
#EXTINF:-1,Credentialed
https://media.example.test/private.m3u8?token=secret
"""
    client = fake_catalog(
        ["configs/root.json", "playlists/main.m3u"],
        {"configs/root.json": root, "playlists/main.m3u": playlist},
    )

    result = scan_catalog(catalog_source(), snapshot(), client)
    rerun = scan_catalog(catalog_source(), snapshot(), client)

    assert result.inconclusive is False
    assert result.files_scanned == 2
    assert [item.candidate_id for item in result.candidates] == [
        item.candidate_id for item in rerun.candidates
    ]
    assert all(item.rights_status is RightsStatus.UNKNOWN for item in result.candidates)
    assert all(item.publication_status.value == "withheld" for item in result.candidates)
    assert {item.kind for item in result.candidates} == {
        "vod_site",
        "nested_config",
        "live_url",
    }
    safe_live = next(
        item
        for item in result.candidates
        if item.kind == "live_url" and item.public_url is not None
    )
    assert len(safe_live.evidence_locations) == 2
    rejected = next(
        item
        for item in result.candidates
        if item.failure_reason is not None
        and item.failure_reason.value == "credential_query_rejected"
    )
    assert rejected.public_url is None
    assert "secret" not in json.dumps(result.as_report())


def test_catalog_cycle_is_broken_by_repository_revision_path_visit_key() -> None:
    self_url = github_raw_url("https://github.com/example/repo", REVISION, "configs/root.json")
    root = json.dumps(
        {
            "sites": [],
            "urls": [{"name": "self", "url": self_url}],
            "storeHouse": [],
            "lives": [],
        }
    ).encode()
    client = fake_catalog(["configs/root.json"], {"configs/root.json": root})

    result = scan_catalog(catalog_source(), snapshot(), client)

    assert result.files_scanned == 1
    assert len(result.candidates) == 1
    assert result.candidates[0].kind == "nested_config"


def test_catalog_limit_is_inconclusive_instead_of_truncating() -> None:
    root = json.dumps(
        {
            "sites": [
                {"type": 1, "api": "https://api.example.test/one"},
                {"type": 1, "api": "https://api.example.test/two"},
            ],
            "urls": [],
            "storeHouse": [],
            "lives": [],
        }
    ).encode()
    client = fake_catalog(["configs/root.json"], {"configs/root.json": root})

    result = scan_catalog(catalog_source(max_candidates=1), snapshot(), client)

    assert result.inconclusive is True
    assert result.failure_reason is not None
    assert result.failure_reason.value == "catalog_limit_exceeded"
    assert len(result.candidates) == 1


def test_catalog_file_limit_is_checked_before_fetching_any_file() -> None:
    client = fake_catalog(
        ["configs/a.json", "configs/b.json"],
        {},
    )

    result = scan_catalog(catalog_source(max_files=1), snapshot(), client)

    assert result.inconclusive is True
    assert result.files_scanned == 0
    assert len(client.requests) == 1
