"""Construction and verification helpers for the two non-self-referential manifests."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from ds_tvbox import __version__
from ds_tvbox.errors import ContractError
from ds_tvbox.models import RunContext
from ds_tvbox.serialization import canonical_json_bytes, sha256_bytes

SCHEMA_VERSION = "1.0.0"
_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
_SOURCE_ID = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_FETCH_MODES = frozenset({"direct_url", "github_tracked_file", "github_repository"})
_CREDENTIAL_QUERY_KEYS = frozenset(
    {
        "access_key",
        "apikey",
        "api_key",
        "auth",
        "authorization",
        "cookie",
        "credential",
        "key",
        "password",
        "secret",
        "sig",
        "signature",
        "token",
    }
)


@dataclass(frozen=True)
class ContentIdentity:
    """The workflow event that first created immutable content."""

    workflow_run_id: str
    workflow_run_attempt: int

    def __post_init__(self) -> None:
        if not self.workflow_run_id or self.workflow_run_attempt < 1:
            raise ContractError("invalid content workflow identity")

    @classmethod
    def from_context(cls, context: RunContext) -> ContentIdentity:
        return cls(context.workflow_run_id, context.workflow_run_attempt)


@dataclass(frozen=True)
class ManifestBytes:
    """Canonical bytes and their externally recordable digest."""

    document: Mapping[str, Any]
    data: bytes
    sha256: str


def prefixed_sha256(data: bytes) -> str:
    return f"sha256:{sha256_bytes(data)}"


def _byte_key(value: str) -> bytes:
    return value.encode("utf-8")


def _validate_repo_path(value: str) -> PurePosixPath:
    if not value or "\\" in value:
        raise ContractError(f"invalid repository path: {value!r}")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or str(path) != value:
        raise ContractError(f"invalid repository path: {value!r}")
    return path


def _release_artifact_kind(path: str, release_id: str) -> str | None:
    prefix = f"dist/releases/{release_id}/"
    if not path.startswith(prefix):
        return None
    relative = path.removeprefix(prefix)
    fixed = {
        "index.json",
        "warehouse.json",
        "depots/stable.json",
        "depots/public-unverified.json",
        "configs/stable.json",
        "live/stable.m3u",
        "health.json",
    }
    if relative in fixed:
        return relative
    if relative.startswith("configs/") and relative.endswith(".json"):
        source_id = relative.removeprefix("configs/").removesuffix(".json")
        if _SOURCE_ID.fullmatch(source_id) and source_id != "stable":
            return "independent_config"
    return None


def release_artifact_hashes(release_id: str, files: Mapping[str, bytes]) -> Mapping[str, str]:
    """Hash exactly the allowed active-release artifacts, excluding its manifest."""

    if not re.fullmatch(r"g[0-9]{8}", release_id):
        raise ContractError(f"invalid release id: {release_id!r}")
    required = {
        f"dist/releases/{release_id}/index.json",
        f"dist/releases/{release_id}/warehouse.json",
        f"dist/releases/{release_id}/depots/stable.json",
        f"dist/releases/{release_id}/depots/public-unverified.json",
        f"dist/releases/{release_id}/configs/stable.json",
        f"dist/releases/{release_id}/live/stable.m3u",
        f"dist/releases/{release_id}/health.json",
    }
    missing = required.difference(files)
    if missing:
        raise ContractError(f"release artifacts are missing: {sorted(missing)}")

    output: dict[str, str] = {}
    for path in sorted(files, key=_byte_key):
        _validate_repo_path(path)
        if path.endswith("/manifest.json"):
            raise ContractError("release manifest must exclude itself from artifacts")
        if _release_artifact_kind(path, release_id) is None:
            raise ContractError(f"path is outside the release artifact contract: {path}")
        data = files[path]
        if not isinstance(data, bytes):
            raise ContractError(f"artifact {path} is not bytes")
        output[path] = prefixed_sha256(data)
    return MappingProxyType(output)


def alias_hashes(files: Mapping[str, bytes]) -> Mapping[str, str]:
    """Hash the exact five floating aliases, excluding the root manifest itself."""

    expected = {
        "dist/index.json",
        "dist/warehouse.json",
        "dist/configs/stable.json",
        "dist/live/stable.m3u",
        "dist/health.json",
    }
    if set(files) != expected:
        missing = sorted(expected.difference(files))
        extra = sorted(set(files).difference(expected))
        raise ContractError(f"invalid alias set; missing={missing}, extra={extra}")
    output: dict[str, str] = {}
    for path in sorted(files, key=_byte_key):
        _validate_repo_path(path)
        if path == "dist/manifest.json":
            raise ContractError("root manifest must exclude itself from aliases")
        data = files[path]
        if not isinstance(data, bytes):
            raise ContractError(f"alias {path} is not bytes")
        output[path] = prefixed_sha256(data)
    return MappingProxyType(output)


def _normalize_upstreams(upstreams: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    required = {
        "source_id",
        "fetch_mode",
        "reviewed_revision",
        "resolved_revision",
        "resolved_fetch_url",
        "terms_sha256",
    }
    for upstream in upstreams:
        missing = required.difference(upstream)
        if missing:
            raise ContractError(f"upstream record is missing: {sorted(missing)}")
        source_id = upstream["source_id"]
        if not isinstance(source_id, str) or not _SOURCE_ID.fullmatch(source_id):
            raise ContractError(f"invalid upstream source_id: {source_id!r}")
        if source_id in seen:
            raise ContractError(f"duplicate upstream source_id: {source_id}")
        seen.add(source_id)
        fetch_mode = str(upstream["fetch_mode"])
        if fetch_mode not in _FETCH_MODES:
            raise ContractError(f"invalid upstream fetch_mode for {source_id}")
        reviewed_revision = upstream["reviewed_revision"]
        resolved_revision = upstream["resolved_revision"]
        if fetch_mode == "direct_url":
            if (reviewed_revision, resolved_revision) != (None, None):
                raise ContractError(f"direct upstream {source_id} must not claim revisions")
        elif any(
            not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision)
            for revision in (reviewed_revision, resolved_revision)
        ):
            raise ContractError(f"tracked upstream {source_id} revisions are invalid")
        resolved_fetch_url = upstream["resolved_fetch_url"]
        if not isinstance(resolved_fetch_url, str):
            raise ContractError(f"upstream {source_id} resolved_fetch_url is invalid")
        parsed_url = urlsplit(resolved_fetch_url)
        if (
            parsed_url.scheme.lower() not in {"http", "https"}
            or not parsed_url.hostname
            or parsed_url.username is not None
            or parsed_url.password is not None
        ):
            raise ContractError(f"upstream {source_id} resolved_fetch_url is not public HTTP(S)")
        query_keys = {
            key.casefold() for key, _ in parse_qsl(parsed_url.query, keep_blank_values=True)
        }
        if query_keys.intersection(_CREDENTIAL_QUERY_KEYS):
            raise ContractError(f"upstream {source_id} resolved_fetch_url contains credentials")
        terms = upstream["terms_sha256"]
        if not isinstance(terms, Mapping):
            raise ContractError(f"upstream {source_id} terms_sha256 must be an object")
        normalized_terms: dict[str, str] = {}
        for name in sorted(terms, key=lambda item: _byte_key(str(item))):
            digest = terms[name]
            if not isinstance(name, str) or not name or not isinstance(digest, str):
                raise ContractError(f"invalid terms hash for upstream {source_id}")
            raw_digest = digest.removeprefix("sha256:")
            if not re.fullmatch(r"[0-9a-f]{64}", raw_digest):
                raise ContractError(f"invalid terms hash for upstream {source_id}")
            normalized_terms[name] = raw_digest
        normalized.append(
            {
                "source_id": source_id,
                "fetch_mode": fetch_mode,
                "reviewed_revision": reviewed_revision,
                "resolved_revision": resolved_revision,
                "resolved_fetch_url": resolved_fetch_url,
                "terms_sha256": normalized_terms,
            }
        )
    normalized.sort(key=lambda item: _byte_key(item["source_id"]))
    return normalized


def build_release_manifest(
    *,
    context: RunContext,
    release_files: Mapping[str, bytes],
    upstreams: Iterable[Mapping[str, Any]] = (),
    source_count: int | None = None,
    vod_site_count: int = 0,
    live_channel_count: int = 0,
    generator_version: str = __version__,
    content_identity: ContentIdentity | None = None,
) -> dict[str, Any]:
    """Build the immutable release-local manifest document."""

    if min(vod_site_count, live_channel_count) < 0:
        raise ContractError("manifest counts cannot be negative")
    identity = content_identity or ContentIdentity.from_context(context)
    normalized_upstreams = _normalize_upstreams(upstreams)
    actual_source_count = len(normalized_upstreams) if source_count is None else source_count
    if actual_source_count < 0:
        raise ContractError("source_count cannot be negative")
    if actual_source_count != len(normalized_upstreams):
        raise ContractError("source_count must equal the number of upstream records")
    artifacts = release_artifact_hashes(context.release_id, release_files)
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": context.generated_at,
        "generation": context.generation,
        "release_id": context.release_id,
        "generator_version": generator_version,
        "source_count": actual_source_count,
        "vod_site_count": vod_site_count,
        "live_channel_count": live_channel_count,
        "previous_commit_sha": context.previous_head,
        "content_workflow_run_id": identity.workflow_run_id,
        "content_workflow_run_attempt": identity.workflow_run_attempt,
        "artifacts": dict(artifacts),
        "upstreams": normalized_upstreams,
    }


def render_manifest(document: Mapping[str, Any]) -> ManifestBytes:
    data = canonical_json_bytes(document)
    return ManifestBytes(
        document=MappingProxyType(dict(document)),
        data=data,
        sha256=prefixed_sha256(data),
    )


def create_release_manifest(**kwargs: Any) -> ManifestBytes:
    """Build and canonically serialize a release manifest."""

    return render_manifest(build_release_manifest(**kwargs))


def build_root_manifest(
    *,
    context: RunContext,
    release_manifest_bytes: bytes,
    alias_files: Mapping[str, bytes],
    content_identity: ContentIdentity | None = None,
) -> dict[str, Any]:
    """Build the root pointer manifest after the release manifest is immutable."""

    if not isinstance(release_manifest_bytes, bytes):
        raise ContractError("release_manifest_bytes must be exact serialized bytes")
    identity = content_identity or ContentIdentity.from_context(context)
    aliases = alias_hashes(alias_files)
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": context.generated_at,
        "active_release_id": context.release_id,
        "previous_commit_sha": context.previous_head,
        "content_workflow_run_id": identity.workflow_run_id,
        "content_workflow_run_attempt": identity.workflow_run_attempt,
        "release_manifest": {
            "path": f"dist/releases/{context.release_id}/manifest.json",
            "sha256": prefixed_sha256(release_manifest_bytes),
        },
        "aliases": dict(aliases),
    }


def create_root_manifest(**kwargs: Any) -> ManifestBytes:
    """Build and canonically serialize the root pointer manifest."""

    return render_manifest(build_root_manifest(**kwargs))


def verify_hash(value: str, data: bytes, *, label: str) -> None:
    """Verify a manifest hash with a stable contract error."""

    if not _HASH.fullmatch(value) or value != prefixed_sha256(data):
        raise ContractError(f"hash mismatch for {label}")
