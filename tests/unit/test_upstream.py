from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import replace

import pytest

from ds_tvbox.errors import ContractError, FetchError, SecurityError
from ds_tvbox.http import HttpRequest, HttpResponse
from ds_tvbox.models import (
    FetchMode,
    FetchSpec,
    ParserKind,
    RightsStatus,
    SourceKind,
    SourceSpec,
    TermsWatchSpec,
)
from ds_tvbox.upstream import (
    GitHubSnapshot,
    UpstreamFailure,
    _failure_from_exception,
    fetch_github_file,
    fetch_github_tree,
    github_raw_url,
    resolve_github_snapshot,
    resolve_upstream,
)

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


def source(
    *,
    mode: FetchMode,
    terms: tuple[TermsWatchSpec, ...],
) -> SourceSpec:
    tracked = mode is FetchMode.GITHUB_TRACKED_FILE
    return SourceSpec(
        id="example-source",
        kind=SourceKind.VOD_CONFIG,
        parser=ParserKind.TVBOX_JSON,
        enabled=True,
        fetch=FetchSpec(
            mode=mode,
            reviewed_url=(
                f"https://raw.githubusercontent.com/example/repo/{REVISION}/config.json"
                if tracked
                else "https://config.example.test/config.json"
            ),
            repository_url="https://github.com/example/repo" if tracked else None,
            track_ref="main" if tracked else None,
            config_path="config.json" if tracked else None,
            reviewed_revision=REVISION if tracked else None,
        ),
        terms_watch=terms,
        rights_status=RightsStatus.PUBLIC_UNVERIFIED,
        config_license_status="unknown",
        content_rights_status="unverified",
        allowed_hosts=frozenset(
            {"api.github.com", "raw.githubusercontent.com"}
            if tracked
            else {"config.example.test", "terms.example.test"}
        ),
        allow_discovered_media_hosts=False,
        http_exceptions=(),
        denied_categories=(),
        client_site=None,
        catalog=None,
        raw={},
    )


def test_tracked_source_resolves_once_and_fetches_terms_and_config_from_same_commit() -> None:
    license_body = b"license\n"
    readme_body = b"readme\n"
    terms = (
        TermsWatchSpec("github_path", None, "LICENSE", hashlib.sha256(license_body).hexdigest()),
        TermsWatchSpec("github_path", None, "README.md", hashlib.sha256(readme_body).hexdigest()),
    )
    spec = source(mode=FetchMode.GITHUB_TRACKED_FILE, terms=terms)
    commit_url = "https://api.github.com/repos/example/repo/commits/main"
    expected_config = github_raw_url("https://github.com/example/repo", REVISION, "config.json")
    bodies: Mapping[str, bytes] = {
        commit_url: json.dumps({"sha": REVISION, "commit": {"tree": {"sha": TREE}}}).encode(),
        github_raw_url("https://github.com/example/repo", REVISION, "LICENSE"): license_body,
        github_raw_url("https://github.com/example/repo", REVISION, "README.md"): readme_body,
        expected_config: b'{"sites":[]}',
    }
    client = FakeFetcher(lambda request: response(request.url, bodies[request.url]))

    snapshot = resolve_upstream(spec, client)

    assert snapshot.resolved_revision == REVISION
    assert snapshot.resolved_fetch_url == expected_config
    assert snapshot.content == b'{"sites":[]}'
    assert [request.url for request in client.requests] == [
        commit_url,
        github_raw_url("https://github.com/example/repo", REVISION, "LICENSE"),
        github_raw_url("https://github.com/example/repo", REVISION, "README.md"),
        expected_config,
    ]
    assert all(REVISION in request.url for request in client.requests[1:])


def test_terms_change_is_source_failure_and_config_is_never_fetched() -> None:
    terms = (
        TermsWatchSpec("github_path", None, "LICENSE", "0" * 64),
        TermsWatchSpec("github_path", None, "README.md", "0" * 64),
    )
    spec = source(mode=FetchMode.GITHUB_TRACKED_FILE, terms=terms)

    def handler(request: HttpRequest) -> HttpResponse:
        if request.url.startswith("https://api.github.com/"):
            body = json.dumps({"sha": REVISION, "commit": {"tree": {"sha": TREE}}}).encode()
            return response(request.url, body)
        return response(request.url, b"changed")

    client = FakeFetcher(handler)
    with pytest.raises(UpstreamFailure) as raised:
        resolve_upstream(spec, client)

    assert raised.value.reason.value == "terms_changed"
    assert raised.value.resolved_revision == REVISION
    assert len(raised.value.terms_sha256) == 2
    assert not any(request.url.endswith("/config.json") for request in client.requests)


def test_direct_terms_are_verified_before_the_fixed_config_url() -> None:
    terms_body = b"fixed terms"
    spec = source(
        mode=FetchMode.DIRECT_URL,
        terms=(
            TermsWatchSpec(
                "url",
                "https://terms.example.test/terms",
                None,
                hashlib.sha256(terms_body).hexdigest(),
            ),
        ),
    )
    bodies = {
        "https://terms.example.test/terms": terms_body,
        "https://config.example.test/config.json": b'{"sites":[]}',
    }
    client = FakeFetcher(lambda request: response(request.url, bodies[request.url]))

    snapshot = resolve_upstream(spec, client)

    assert snapshot.resolved_revision is None
    assert [request.url for request in client.requests] == list(bodies)


def test_raw_url_quotes_path_segments_without_allowing_traversal() -> None:
    assert github_raw_url(
        "https://github.com/example/repo", REVISION, "configs/中文 file.json"
    ).endswith(f"/{REVISION}/configs/%E4%B8%AD%E6%96%87%20file.json")
    with pytest.raises(ContractError):
        github_raw_url("https://github.com/example/repo", REVISION, "../secret")


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (404, "http_404"),
        (410, "http_410"),
        (429, "rate_limited"),
        (401, "credential_required"),
        (403, "credential_required"),
        (408, "fetch_timeout"),
        (500, "upstream_5xx"),
        (599, "upstream_5xx"),
        (300, "schema_incompatible"),
    ],
)
def test_direct_upstream_maps_http_statuses_to_stable_failure_reasons(
    status: int,
    reason: str,
) -> None:
    spec = source(mode=FetchMode.DIRECT_URL, terms=())
    client = FakeFetcher(lambda request: response(request.url, b"failure", status))

    with pytest.raises(UpstreamFailure) as raised:
        resolve_upstream(spec, client)

    assert raised.value.reason.value == reason


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (SecurityError("credential query key rejected"), "credential_query_rejected"),
        (SecurityError("private address"), "private_address_rejected"),
        (SecurityError("dangerous scheme"), "dangerous_scheme_rejected"),
        (SecurityError("client-visible http"), "client_http_disallowed"),
        (SecurityError("unknown policy"), "private_address_rejected"),
        (FetchError("TLS certificate failed"), "tls_failure"),
        (FetchError("DNS address failed"), "dns_failure"),
        (FetchError("all approved addresses failed"), "fetch_timeout"),
        (FetchError("response limit"), "response_too_large"),
        (TimeoutError("timed out"), "fetch_timeout"),
        (FetchError("connection reset"), "fetch_timeout"),
    ],
)
def test_fetch_exception_classification_is_stable(error: BaseException, reason: str) -> None:
    assert _failure_from_exception(error).value == reason


def test_direct_upstream_maps_transport_failure_and_rejects_empty_or_oversized_body() -> None:
    spec = source(mode=FetchMode.DIRECT_URL, terms=())

    with pytest.raises(UpstreamFailure) as raised:
        resolve_upstream(spec, FakeFetcher(lambda _request: (_ for _ in ()).throw(OSError("DNS"))))
    assert raised.value.reason.value == "dns_failure"

    with pytest.raises(UpstreamFailure) as raised:
        resolve_upstream(spec, FakeFetcher(lambda request: response(request.url, b"")))
    assert raised.value.reason.value == "schema_incompatible"

    with pytest.raises(UpstreamFailure) as raised:
        resolve_upstream(
            spec,
            FakeFetcher(lambda request: response(request.url, b"x" * (5 * 1024 * 1024 + 1))),
        )
    assert raised.value.reason.value == "response_too_large"


def test_disabled_and_malformed_terms_sources_fail_before_untrusted_content() -> None:
    disabled = replace(source(mode=FetchMode.DIRECT_URL, terms=()), enabled=False)
    with pytest.raises(ContractError, match="disabled"):
        resolve_upstream(disabled, FakeFetcher(lambda request: response(request.url, b"unused")))

    malformed_url_term = replace(
        source(mode=FetchMode.DIRECT_URL, terms=()),
        terms_watch=(TermsWatchSpec("url", None, None, "0" * 64),),
    )
    with pytest.raises(ContractError, match="fixed URL"):
        resolve_upstream(
            malformed_url_term,
            FakeFetcher(lambda request: response(request.url, b"unused")),
        )

    unsupported = replace(
        source(mode=FetchMode.DIRECT_URL, terms=()),
        terms_watch=(TermsWatchSpec("other", None, None, "0" * 64),),
    )
    with pytest.raises(ContractError, match="unsupported"):
        resolve_upstream(unsupported, FakeFetcher(lambda request: response(request.url, b"unused")))


def test_repository_identity_and_revision_contracts_are_strict() -> None:
    assert github_raw_url("https://github.com/example/repo.git", REVISION, "a.txt").startswith(
        "https://raw.githubusercontent.com/example/repo/"
    )
    for repository in (
        "http://github.com/example/repo",
        "https://gitlab.com/example/repo",
        "https://github.com/example",
        "https://github.com/example/.git",
    ):
        with pytest.raises(ContractError, match="repository"):
            github_raw_url(repository, REVISION, "a.txt")
    with pytest.raises(ContractError, match="revision"):
        github_raw_url("https://github.com/example/repo", "MAIN", "a.txt")
    for path in ("", "/absolute", "a//b", "a/./b", "a\\b"):
        with pytest.raises(ContractError, match="path"):
            github_raw_url("https://github.com/example/repo", REVISION, path)


def test_github_snapshot_rejects_wrong_mode_invalid_json_and_commit_shapes() -> None:
    direct = source(mode=FetchMode.DIRECT_URL, terms=())
    with pytest.raises(ContractError, match="GitHub fetch mode"):
        resolve_github_snapshot(direct, FakeFetcher(lambda request: response(request.url, b"{}")))

    tracked = source(mode=FetchMode.GITHUB_TRACKED_FILE, terms=())
    for body, reason in (
        (b"not-json", "invalid_json"),
        (b"[]", "schema_incompatible"),
        (
            json.dumps({"sha": "bad", "commit": {"tree": {"sha": TREE}}}).encode(),
            "schema_incompatible",
        ),
        (json.dumps({"sha": REVISION, "commit": {}}).encode(), "schema_incompatible"),
        (
            json.dumps({"sha": REVISION, "commit": {"tree": {"sha": "BAD"}}}).encode(),
            "schema_incompatible",
        ),
    ):
        with pytest.raises(UpstreamFailure) as raised:
            resolve_github_snapshot(
                tracked,
                FakeFetcher(lambda request, body=body: response(request.url, body)),
            )
        assert raised.value.reason.value == reason


def _repository_source() -> SourceSpec:
    base = source(mode=FetchMode.GITHUB_TRACKED_FILE, terms=())
    return replace(
        base,
        kind=SourceKind.REPOSITORY_CATALOG,
        parser=ParserKind.REPOSITORY_CATALOG,
        fetch=FetchSpec(
            FetchMode.GITHUB_REPOSITORY,
            None,
            "https://github.com/example/repo",
            "main",
            None,
            REVISION,
        ),
    )


def test_repository_snapshot_has_no_config_body() -> None:
    spec = _repository_source()
    body = json.dumps({"sha": REVISION, "commit": {"tree": {"sha": TREE}}}).encode()
    snapshot = resolve_upstream(
        spec,
        FakeFetcher(lambda request: response(request.url, body)),
    )
    assert snapshot.content is None
    assert snapshot.content_sha256 is None
    assert snapshot.resolved_fetch_url is None
    assert snapshot.github is not None


def _snapshot() -> GitHubSnapshot:
    return GitHubSnapshot(
        repository_url="https://github.com/example/repo",
        owner="example",
        repository="repo",
        resolved_revision=REVISION,
        tree_sha=TREE,
    )


def test_github_tree_is_sorted_and_preserves_optional_blob_size() -> None:
    payload = {
        "tree": [
            {"path": "z.txt", "type": "blob", "sha": "3" * 40},
            {"path": "a/config.json", "type": "blob", "sha": "4" * 40, "size": 12},
        ],
        "truncated": False,
    }
    entries = fetch_github_tree(
        _repository_source(),
        _snapshot(),
        FakeFetcher(lambda request: response(request.url, json.dumps(payload).encode())),
    )
    assert [entry.path for entry in entries] == ["a/config.json", "z.txt"]
    assert entries[0].size == 12
    assert entries[1].size is None


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        ({"truncated": True, "tree": []}, "catalog_limit_exceeded"),
        ({"truncated": False, "tree": {}}, "schema_incompatible"),
        ({"tree": ["not-an-object"]}, "schema_incompatible"),
        ({"tree": [{"path": "../x", "type": "blob", "sha": "3" * 40}]}, "schema_incompatible"),
        ({"tree": [{"path": "x", "type": 1, "sha": "3" * 40}]}, "schema_incompatible"),
        ({"tree": [{"path": "x", "type": "blob", "sha": "bad"}]}, "schema_incompatible"),
        (
            {"tree": [{"path": "x", "type": "blob", "sha": "3" * 40, "size": True}]},
            "schema_incompatible",
        ),
        (
            {"tree": [{"path": "x", "type": "blob", "sha": "3" * 40, "size": -1}]},
            "schema_incompatible",
        ),
    ],
)
def test_github_tree_rejects_inconclusive_or_malformed_responses(
    payload: object,
    reason: str,
) -> None:
    with pytest.raises(UpstreamFailure) as raised:
        fetch_github_tree(
            _repository_source(),
            _snapshot(),
            FakeFetcher(lambda request: response(request.url, json.dumps(payload).encode())),
        )
    assert raised.value.reason.value == reason


def test_github_file_is_commit_pinned_and_nonempty() -> None:
    client = FakeFetcher(lambda request: response(request.url, b"file"))
    url, body = fetch_github_file(_repository_source(), _snapshot(), "config/a.json", client)
    assert f"/{REVISION}/config/a.json" in url
    assert body == b"file"

    with pytest.raises(UpstreamFailure) as raised:
        fetch_github_file(
            _repository_source(),
            _snapshot(),
            "empty.json",
            FakeFetcher(lambda request: response(request.url, b"")),
        )
    assert raised.value.reason.value == "schema_incompatible"
