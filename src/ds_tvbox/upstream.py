"""Resolve and fetch immutable upstream snapshots.

The registry contains reviewed trust anchors.  This module deliberately keeps
those anchors separate from values resolved during a run: GitHub refs are
resolved once, every repository file is read from that commit, and terms are
verified before a configuration body is returned to the collector.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import quote, urlsplit

from .errors import ContractError, FetchError, SecurityError
from .http import HttpRequest, HttpResponse, SafeHttpClient
from .models import FailureReason, FetchMode, SourceSpec

MAX_TERMS_BYTES = 1024 * 1024
MAX_CONFIG_BYTES = 5 * 1024 * 1024
MAX_LIVE_PLAYLIST_BYTES = 10 * 1024 * 1024
MAX_GITHUB_API_BYTES = 5 * 1024 * 1024
_SHA40 = re.compile(r"^[0-9a-f]{40}$")


class Fetcher(Protocol):
    """Small seam used by deterministic tests and the production safe client."""

    def fetch(self, request: HttpRequest) -> HttpResponse: ...


class UpstreamFailure(Exception):
    """An upstream could not produce a trusted snapshot this run."""

    def __init__(
        self,
        reason: FailureReason,
        *,
        secondary_reasons: Sequence[FailureReason] = (),
        resolved_revision: str | None = None,
        terms_sha256: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(reason.value)
        self.reason = reason
        self.secondary_reasons = tuple(secondary_reasons)
        self.resolved_revision = resolved_revision
        self.terms_sha256 = dict(terms_sha256 or {})


@dataclass(frozen=True)
class GitHubSnapshot:
    repository_url: str
    owner: str
    repository: str
    resolved_revision: str
    tree_sha: str


@dataclass(frozen=True)
class UpstreamSnapshot:
    source_id: str
    fetch_mode: FetchMode
    resolved_revision: str | None
    resolved_fetch_url: str | None
    content: bytes | None
    content_sha256: str | None
    terms_sha256: Mapping[str, str]
    github: GitHubSnapshot | None = None


@dataclass(frozen=True)
class GitHubTreeEntry:
    path: str
    object_type: str
    sha: str
    size: int | None


def _repository_identity(repository_url: str) -> tuple[str, str]:
    parsed = urlsplit(repository_url)
    parts = tuple(part for part in parsed.path.split("/") if part)
    if parsed.scheme != "https" or parsed.hostname != "github.com" or len(parts) != 2:
        raise ContractError("repository_url must identify one HTTPS GitHub repository")
    owner, repository = parts
    if repository.endswith(".git"):
        repository = repository[:-4]
    if not owner or not repository:
        raise ContractError("repository_url has an empty owner or repository")
    return owner, repository


def github_raw_url(repository_url: str, revision: str, path: str) -> str:
    """Build a Raw URL containing an immutable commit and a safe POSIX path."""

    owner, repository = _repository_identity(repository_url)
    if not _SHA40.fullmatch(revision):
        raise ContractError("GitHub revision must be a lowercase 40-character commit")
    parts = path.split("/")
    if not path or any(part in {"", ".", ".."} for part in parts) or "\\" in path:
        raise ContractError("GitHub path must be a safe POSIX relative path")
    encoded_path = "/".join(quote(part, safe="-._~") for part in parts)
    return (
        f"https://raw.githubusercontent.com/{quote(owner, safe='-._~')}/"
        f"{quote(repository, safe='-._~')}/{revision}/{encoded_path}"
    )


def _failure_from_exception(exc: BaseException) -> FailureReason:
    message = str(exc).casefold()
    if isinstance(exc, SecurityError):
        if "credential" in message or "query key" in message:
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


def _request(
    client: Fetcher,
    source: SourceSpec,
    url: str,
    *,
    max_bytes: int,
    allowed_hosts: frozenset[str] | None = None,
) -> HttpResponse:
    try:
        response = client.fetch(
            HttpRequest(
                url=url,
                allowed_hosts=allowed_hosts or source.allowed_hosts,
                http_exceptions=source.http_exceptions,
                max_bytes=max_bytes,
            )
        )
    except (FetchError, SecurityError, OSError, TimeoutError) as exc:
        raise UpstreamFailure(_failure_from_exception(exc)) from exc
    if response.status == 404:
        raise UpstreamFailure(FailureReason.HTTP_404)
    if response.status == 410:
        raise UpstreamFailure(FailureReason.HTTP_410)
    if response.status == 429:
        raise UpstreamFailure(FailureReason.RATE_LIMITED)
    if response.status in {401, 403}:
        raise UpstreamFailure(FailureReason.CREDENTIAL_REQUIRED)
    if response.status == 408:
        raise UpstreamFailure(FailureReason.FETCH_TIMEOUT)
    if 500 <= response.status <= 599:
        raise UpstreamFailure(FailureReason.UPSTREAM_5XX)
    if not 200 <= response.status <= 299:
        raise UpstreamFailure(FailureReason.SCHEMA_INCOMPATIBLE)
    if len(response.body) > max_bytes:
        raise UpstreamFailure(FailureReason.RESPONSE_TOO_LARGE)
    return response


def _json_object(response: HttpResponse) -> Mapping[str, object]:
    try:
        value = json.loads(response.body.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
        raise UpstreamFailure(FailureReason.INVALID_JSON) from exc
    if not isinstance(value, Mapping):
        raise UpstreamFailure(FailureReason.SCHEMA_INCOMPATIBLE)
    return value


def resolve_github_snapshot(
    source: SourceSpec,
    client: Fetcher,
) -> GitHubSnapshot:
    """Resolve ``track_ref`` through GitHub's commit API exactly once."""

    fetch = source.fetch
    if fetch.mode not in {FetchMode.GITHUB_TRACKED_FILE, FetchMode.GITHUB_REPOSITORY}:
        raise ContractError("resolve_github_snapshot requires a GitHub fetch mode")
    assert fetch.repository_url is not None
    assert fetch.track_ref is not None
    owner, repository = _repository_identity(fetch.repository_url)
    endpoint = (
        f"https://api.github.com/repos/{quote(owner, safe='-._~')}/"
        f"{quote(repository, safe='-._~')}/commits/{quote(fetch.track_ref, safe='')}"
    )
    response = _request(
        client,
        source,
        endpoint,
        max_bytes=MAX_GITHUB_API_BYTES,
        allowed_hosts=frozenset({"api.github.com"}),
    )
    value = _json_object(response)
    revision = value.get("sha")
    commit = value.get("commit")
    tree_sha: object = None
    if isinstance(commit, Mapping):
        tree = commit.get("tree")
        if isinstance(tree, Mapping):
            tree_sha = tree.get("sha")
    if not isinstance(revision, str) or not _SHA40.fullmatch(revision):
        raise UpstreamFailure(FailureReason.SCHEMA_INCOMPATIBLE)
    if not isinstance(tree_sha, str) or not _SHA40.fullmatch(tree_sha):
        raise UpstreamFailure(FailureReason.SCHEMA_INCOMPATIBLE)
    return GitHubSnapshot(
        repository_url=fetch.repository_url,
        owner=owner,
        repository=repository,
        resolved_revision=revision,
        tree_sha=tree_sha,
    )


def _verify_terms(
    source: SourceSpec,
    client: Fetcher,
    github: GitHubSnapshot | None,
) -> Mapping[str, str]:
    actual: dict[str, str] = {}
    changed = False
    for term in source.terms_watch:
        if term.type == "github_path":
            if github is None or term.path is None:
                raise ContractError("github_path terms require a resolved GitHub snapshot")
            url = github_raw_url(
                github.repository_url,
                github.resolved_revision,
                term.path,
            )
            identity = term.path
        elif term.type == "url":
            if term.url is None:
                raise ContractError("url terms require a fixed URL")
            url = term.url
            identity = term.url
        else:  # registry validation should make this unreachable
            raise ContractError(f"unsupported terms_watch type: {term.type}")
        response = _request(client, source, url, max_bytes=MAX_TERMS_BYTES)
        digest = hashlib.sha256(response.body).hexdigest()
        actual[identity] = digest
        changed = changed or digest != term.reviewed_sha256
    if changed:
        raise UpstreamFailure(
            FailureReason.TERMS_CHANGED,
            resolved_revision=github.resolved_revision if github is not None else None,
            terms_sha256=actual,
        )
    return dict(sorted(actual.items()))


def resolve_upstream(source: SourceSpec, client: Fetcher) -> UpstreamSnapshot:
    """Return one terms-verified direct or immutable GitHub snapshot."""

    if not source.enabled:
        raise ContractError("disabled sources must not be fetched")
    github: GitHubSnapshot | None = None
    if source.fetch.mode in {
        FetchMode.GITHUB_TRACKED_FILE,
        FetchMode.GITHUB_REPOSITORY,
    }:
        github = resolve_github_snapshot(source, client)
    terms = _verify_terms(source, client, github)

    resolved_url: str | None
    content: bytes | None
    if source.fetch.mode is FetchMode.DIRECT_URL:
        assert source.fetch.reviewed_url is not None
        resolved_url = source.fetch.reviewed_url
    elif source.fetch.mode is FetchMode.GITHUB_TRACKED_FILE:
        assert github is not None
        assert source.fetch.config_path is not None
        resolved_url = github_raw_url(
            github.repository_url,
            github.resolved_revision,
            source.fetch.config_path,
        )
    else:
        resolved_url = None

    if resolved_url is None:
        content = None
    else:
        max_bytes = (
            MAX_LIVE_PLAYLIST_BYTES if source.kind.value == "live_playlist" else MAX_CONFIG_BYTES
        )
        response = _request(client, source, resolved_url, max_bytes=max_bytes)
        content = response.body
        if not content:
            raise UpstreamFailure(FailureReason.SCHEMA_INCOMPATIBLE)

    return UpstreamSnapshot(
        source_id=source.id,
        fetch_mode=source.fetch.mode,
        resolved_revision=github.resolved_revision if github is not None else None,
        resolved_fetch_url=resolved_url,
        content=content,
        content_sha256=hashlib.sha256(content).hexdigest() if content is not None else None,
        terms_sha256=terms,
        github=github,
    )


def fetch_github_tree(
    source: SourceSpec,
    snapshot: GitHubSnapshot,
    client: Fetcher,
) -> tuple[GitHubTreeEntry, ...]:
    """List every blob in the resolved commit; truncated trees are inconclusive."""

    endpoint = (
        f"https://api.github.com/repos/{quote(snapshot.owner, safe='-._~')}/"
        f"{quote(snapshot.repository, safe='-._~')}/git/trees/{snapshot.tree_sha}?recursive=1"
    )
    response = _request(
        client,
        source,
        endpoint,
        max_bytes=MAX_GITHUB_API_BYTES,
        allowed_hosts=frozenset({"api.github.com"}),
    )
    value = _json_object(response)
    if value.get("truncated") is True:
        raise UpstreamFailure(FailureReason.CATALOG_LIMIT_EXCEEDED)
    raw_tree = value.get("tree")
    if not isinstance(raw_tree, list):
        raise UpstreamFailure(FailureReason.SCHEMA_INCOMPATIBLE)
    entries: list[GitHubTreeEntry] = []
    for raw in raw_tree:
        if not isinstance(raw, Mapping):
            raise UpstreamFailure(FailureReason.SCHEMA_INCOMPATIBLE)
        path = raw.get("path")
        object_type = raw.get("type")
        sha = raw.get("sha")
        size = raw.get("size")
        if (
            not isinstance(path, str)
            or not path
            or path.startswith("/")
            or "\\" in path
            or any(part in {"", ".", ".."} for part in path.split("/"))
            or not isinstance(object_type, str)
            or not isinstance(sha, str)
            or not _SHA40.fullmatch(sha)
            or (size is not None and (type(size) is not int or size < 0))
        ):
            raise UpstreamFailure(FailureReason.SCHEMA_INCOMPATIBLE)
        entries.append(GitHubTreeEntry(path, object_type, sha, size))
    entries.sort(key=lambda entry: entry.path.encode("utf-8"))
    return tuple(entries)


def fetch_github_file(
    source: SourceSpec,
    snapshot: GitHubSnapshot,
    path: str,
    client: Fetcher,
    *,
    max_bytes: int = MAX_CONFIG_BYTES,
) -> tuple[str, bytes]:
    """Read one file from the exact commit represented by ``snapshot``."""

    url = github_raw_url(snapshot.repository_url, snapshot.resolved_revision, path)
    response = _request(client, source, url, max_bytes=max_bytes)
    if not response.body:
        raise UpstreamFailure(FailureReason.SCHEMA_INCOMPATIBLE)
    return url, response.body


def production_fetcher() -> SafeHttpClient:
    """Construct the production implementation without creating it at import time."""

    return SafeHttpClient()
