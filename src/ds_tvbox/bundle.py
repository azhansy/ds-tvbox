"""Bundle assembly, safe materialization, and independent output validation."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]

from ds_tvbox.errors import ContractError, SecurityError
from ds_tvbox.generator import GeneratedClientArtifacts
from ds_tvbox.manifests import (
    ContentIdentity,
    create_release_manifest,
    create_root_manifest,
    prefixed_sha256,
    verify_hash,
)
from ds_tvbox.models import ReleaseKind, RunContext
from ds_tvbox.reports import render_latest_markdown
from ds_tvbox.security import normalize_client_url_offline, rejected_query_keys
from ds_tvbox.serialization import canonical_json_bytes, ensure_relative_safe_path, write_bytes
from ds_tvbox.validation import client_vod_entity_id, validate_health_document

_RELEASE_ID = re.compile(r"^g[0-9]{8}$")
_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
_SOURCE_ID = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_SOURCE_CONFIG = re.compile(r"^configs/[a-z0-9]+(?:-[a-z0-9]+)*\.json$")
_TRUSTED_OWNER = "azhansy"
_TRUSTED_REPOSITORY = "ds-tvbox"
_TRUSTED_GENERATED_REF = "generated"
_RAW_RELEASE_URL = re.compile(
    r"^https://raw\.githubusercontent\.com/azhansy/ds-tvbox/generated/"
    r"dist/releases/(g[0-9]{8})/(.+)$"
)
_EXECUTABLE_URL = re.compile(r"(?:\.jar|\.js|\.py|\.dex|\.so)(?:$|[?#])", re.I)
_DANGEROUS_KEYS = frozenset({"spider", "jar", "ext", "header", "headers", "rules"})
_ROOT_ALIASES = (
    "dist/index.json",
    "dist/warehouse.json",
    "dist/configs/stable.json",
    "dist/live/stable.m3u",
    "dist/health.json",
)
_FIXED_RELEASE_ARTIFACTS = frozenset(
    {
        "index.json",
        "warehouse.json",
        "depots/stable.json",
        "depots/public-unverified.json",
        "configs/stable.json",
        "live/stable.m3u",
        "health.json",
    }
)


@dataclass(frozen=True)
class BundleFiles:
    """All content files for a candidate content commit."""

    files: Mapping[str, bytes]
    release_manifest_sha256: str
    root_manifest_sha256: str


@dataclass(frozen=True)
class BundleValidationResult:
    release_id: str
    generation: int
    artifact_count: int
    alias_count: int
    source_count: int
    vod_site_count: int
    live_channel_count: int
    source_ids: frozenset[str]
    vod_entity_ids: frozenset[str]
    live_entity_ids: frozenset[str]
    release_manifest_sha256: str
    root_manifest_sha256: str


def _byte_key(value: str) -> bytes:
    return value.encode("utf-8")


def _strict_object(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, value in pairs:
        if key in output:
            raise ContractError(f"duplicate JSON key: {key}")
        output[key] = value
    return output


def _load_json_bytes(data: bytes, *, label: str, canonical: bool = True) -> Any:
    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=_strict_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ContractError(f"invalid JSON in {label}") from error
    if canonical and canonical_json_bytes(value) != data:
        raise ContractError(f"non-canonical JSON in {label}")
    return value


def _read_json(path: Path, *, root: Path) -> Any:
    return _load_json_bytes(path.read_bytes(), label=path.relative_to(root).as_posix())


def _require_object(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError(f"{label} must be a JSON object")
    return value


def _require_exact_keys(
    value: Mapping[str, Any], *, required: set[str], label: str, optional: set[str] | None = None
) -> None:
    optional = optional or set()
    missing = required.difference(value)
    extra = set(value).difference(required | optional)
    if missing or extra:
        raise ContractError(
            f"{label} keys invalid; missing={sorted(missing)}, extra={sorted(extra)}"
        )


def _schemas_root(schemas_dir: Path | None) -> Path:
    return schemas_dir or Path(__file__).resolve().parents[2] / "schemas"


def _validate_schema(document: Any, schema_path: Path, *, label: str) -> None:
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ContractError(f"cannot load schema {schema_path}") from error
    errors = sorted(
        Draft202012Validator(schema).iter_errors(document),
        key=lambda item: tuple(str(part) for part in item.path),
    )
    if errors:
        first = errors[0]
        location = "/".join(str(item) for item in first.absolute_path) or "$"
        raise ContractError(f"Schema validation failed for {label} at {location}: {first.message}")


def _normalized_health_bytes(health: Mapping[str, Any] | bytes) -> bytes:
    if isinstance(health, bytes):
        _load_json_bytes(health, label="health")
        return health
    return canonical_json_bytes(health)


def build_bundle_files(
    *,
    context: RunContext,
    client_artifacts: GeneratedClientArtifacts,
    health: Mapping[str, Any] | bytes,
    upstreams: Iterable[Mapping[str, Any]] = (),
    source_count: int | None = None,
    generator_version: str | None = None,
    content_identity: ContentIdentity | None = None,
    supplemental_files: Mapping[str, bytes] | None = None,
) -> BundleFiles:
    """Assemble manifests around generator output without creating self hashes."""

    if (context.owner, context.repository, context.generated_ref) != (
        _TRUSTED_OWNER,
        _TRUSTED_REPOSITORY,
        _TRUSTED_GENERATED_REF,
    ):
        raise ContractError("bundle context is not the trusted azhansy/ds-tvbox/generated target")
    if client_artifacts.release_id != context.release_id:
        raise ContractError("client artifacts and run context use different releases")
    health_bytes = _normalized_health_bytes(health)
    release_health_path = f"dist/releases/{context.release_id}/health.json"
    release_files = dict(client_artifacts.release_files)
    release_files[release_health_path] = health_bytes
    aliases = dict(client_artifacts.alias_files)
    aliases["dist/health.json"] = health_bytes

    release_kwargs: dict[str, Any] = {
        "context": context,
        "release_files": release_files,
        "upstreams": tuple(upstreams),
        "source_count": source_count,
        "vod_site_count": client_artifacts.vod_site_count,
        "live_channel_count": client_artifacts.live_channel_count,
        "content_identity": content_identity,
    }
    if generator_version is not None:
        release_kwargs["generator_version"] = generator_version
    release_manifest = create_release_manifest(**release_kwargs)
    root_manifest = create_root_manifest(
        context=context,
        release_manifest_bytes=release_manifest.data,
        alias_files=aliases,
        content_identity=content_identity,
    )
    files = {
        **release_files,
        f"dist/releases/{context.release_id}/manifest.json": release_manifest.data,
        **aliases,
        "dist/manifest.json": root_manifest.data,
    }
    if supplemental_files:
        for path, data in supplemental_files.items():
            if path not in {
                "state/release.json",
                "dist/reports/latest.json",
                "dist/reports/latest.md",
            }:
                raise ContractError(f"unsupported supplemental bundle path: {path}")
            if not isinstance(data, bytes):
                raise ContractError(f"supplemental file {path} is not bytes")
            files[path] = data
    return BundleFiles(
        files=MappingProxyType(dict(sorted(files.items(), key=lambda item: _byte_key(item[0])))),
        release_manifest_sha256=release_manifest.sha256,
        root_manifest_sha256=root_manifest.sha256,
    )


def _safe_target(root: Path, relative: str) -> Path:
    safe = ensure_relative_safe_path(relative)
    if PurePosixPath(relative).as_posix() != relative:
        raise ContractError(f"bundle path is not canonical POSIX: {relative!r}")
    target = root / safe
    current = root
    for part in safe.parts[:-1]:
        current = current / part
        if current.is_symlink():
            raise SecurityError(f"bundle path traverses symlink: {relative}")
    if target.is_symlink():
        raise SecurityError(f"bundle target is a symlink: {relative}")
    return target


def materialize_bundle(
    root: Path,
    files: Mapping[str, bytes] | BundleFiles,
    *,
    refuse_existing_release_files: bool = True,
) -> None:
    """Write a candidate tree safely while refusing historical release mutation."""

    mapping = files.files if isinstance(files, BundleFiles) else files
    if root.is_symlink():
        raise SecurityError("bundle root must not be a symlink")
    root.mkdir(parents=True, exist_ok=True)
    prepared: list[tuple[str, Path, bytes, bool]] = []
    for relative in sorted(mapping, key=_byte_key):
        data = mapping[relative]
        if not isinstance(data, bytes):
            raise ContractError(f"bundle file {relative} is not bytes")
        target = _safe_target(root, relative)
        keep_existing = False
        if target.exists() and relative.startswith("dist/releases/"):
            if refuse_existing_release_files or target.read_bytes() != data:
                raise ContractError(f"refusing to overwrite immutable release file: {relative}")
            keep_existing = True
        prepared.append((relative, target, data, keep_existing))
    for _relative, target, data, keep_existing in prepared:
        if keep_existing:
            continue
        write_bytes(target, data)


def _validate_client_url(value: str, *, label: str) -> str:
    if not isinstance(value, str):
        raise ContractError(f"{label} must be a URL string")
    try:
        normalize_client_url_offline(value)
    except SecurityError as error:
        raise SecurityError(
            f"{label} is not a credential-free HTTPS public URL: {error}"
        ) from error
    if _EXECUTABLE_URL.search(value):
        raise SecurityError(f"{label} references an executable dependency")
    return value


def _validate_public_upstream_url(value: Any, *, label: str) -> str:
    if not isinstance(value, str):
        raise ContractError(f"{label} must be a URL string")
    parsed = urlsplit(value)
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise SecurityError(f"{label} is not a public HTTP(S) URL")
    if rejected_query_keys(parsed.query):
        raise SecurityError(f"{label} contains a credential query parameter")
    return value


def _scan_client_tree(value: Any, *, path: str = "$") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key.casefold() in _DANGEROUS_KEYS:
                raise SecurityError(f"dangerous client field at {path}/{key}")
            _scan_client_tree(child, path=f"{path}/{key}")
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            _scan_client_tree(child, path=f"{path}/{index}")
        return
    if isinstance(value, str):
        if _EXECUTABLE_URL.search(value):
            raise SecurityError(f"executable dependency at {path}")
        parsed = urlsplit(value)
        if parsed.scheme:
            _validate_client_url(value, label=path)


def _validate_unique_entries(document: Mapping[str, Any], *, label: str) -> None:
    if "urls" in document:
        names: set[str] = set()
        urls: set[str] = set()
        for item in document["urls"]:
            if item["name"] in names or item["url"] in urls:
                raise ContractError(f"duplicate line in {label}")
            names.add(item["name"])
            urls.add(item["url"])
    if "storeHouse" in document:
        names = set()
        urls = set()
        for item in document["storeHouse"]:
            if item["sourceName"] in names or item["sourceUrl"] in urls:
                raise ContractError(f"duplicate warehouse in {label}")
            names.add(item["sourceName"])
            urls.add(item["sourceUrl"])
    if "sites" in document:
        keys: set[str] = set()
        names = set()
        for item in document["sites"]:
            if item["key"] in keys or item["name"] in names:
                raise ContractError(f"duplicate site key or name in {label}")
            keys.add(item["key"])
            names.add(item["name"])


def _parse_m3u(data: bytes) -> list[str]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ContractError("M3U is not UTF-8") from error
    if "\r" in text or not text.endswith("\n"):
        raise ContractError("M3U must use LF and end with a newline")
    lines = text.splitlines()
    if not lines or lines[0] != "#EXTM3U":
        raise ContractError("M3U must start with #EXTM3U")
    if (len(lines) - 1) % 2:
        raise ContractError("M3U channel records must be EXTINF/URL pairs")
    urls: list[str] = []
    attribute = r'(?: tvg-id="[^"]*"| tvg-logo="[^"]*"| tvg-url="[^"]*"| group-title="[^"]*")*'
    pattern = re.compile(rf"^#EXTINF:-1{attribute},[^\r\n]+$")
    for index in range(1, len(lines), 2):
        info = lines[index]
        url = lines[index + 1]
        if not pattern.fullmatch(info):
            raise ContractError(f"unsupported M3U directive at line {index + 1}")
        _validate_client_url(url, label=f"M3U line {index + 2}")
        urls.append(url)
    if len(urls) != len(set(urls)):
        raise ContractError("M3U contains duplicate playback URLs")
    return urls


def _raw_prefix(index: Mapping[str, Any], release_id: str) -> str:
    urls = index.get("urls")
    if not isinstance(urls, list) or not urls or urls[0].get("name") != "DS 稳定聚合":
        raise ContractError("index first line must be DS stable aggregation")
    stable_url = urls[0].get("url")
    if not isinstance(stable_url, str):
        raise ContractError("index stable URL is missing")
    match = _RAW_RELEASE_URL.fullmatch(stable_url)
    if not match or match.group(1) != release_id or match.group(2) != "configs/stable.json":
        raise ContractError(
            "index stable URL is not locked to trusted azhansy/ds-tvbox/generated release"
        )
    return stable_url.removesuffix("configs/stable.json")


def _require_release_url(value: Any, *, prefix: str, relative: str, label: str) -> None:
    expected = f"{prefix}{relative}"
    if value != expected:
        raise ContractError(f"{label} crosses release boundary or has an unexpected path")


def _valid_release_artifact(relative: str) -> bool:
    if relative in _FIXED_RELEASE_ARTIFACTS:
        return True
    return bool(_SOURCE_CONFIG.fullmatch(relative)) and relative != "configs/stable.json"


def _validate_release_closure(
    *,
    release_id: str,
    index: Mapping[str, Any],
    warehouse: Mapping[str, Any],
    stable_depot: Mapping[str, Any],
    risk_depot: Mapping[str, Any],
    configs: Mapping[str, Mapping[str, Any]],
    m3u_urls: Sequence[str],
) -> None:
    prefix = _raw_prefix(index, release_id)
    referenced_configs: set[str] = set()
    for position, item in enumerate(index["urls"]):
        match = _RAW_RELEASE_URL.fullmatch(item["url"])
        if (
            not match
            or match.group(1) != release_id
            or not _SOURCE_CONFIG.fullmatch(match.group(2))
        ):
            raise ContractError(f"index URL {position} is outside the active release config set")
        _require_release_url(
            item["url"], prefix=prefix, relative=match.group(2), label=f"index URL {position}"
        )
        referenced_configs.add(match.group(2))

    expected_warehouse = (
        ("DS 稳定仓", "depots/stable.json"),
        ("DS 公共实验仓", "depots/public-unverified.json"),
    )
    if len(warehouse["storeHouse"]) != 2:
        raise ContractError("warehouse must contain exactly the two declared depots")
    for item, (name, relative) in zip(warehouse["storeHouse"], expected_warehouse, strict=True):
        if item["sourceName"] != name:
            raise ContractError("warehouse order or name is invalid")
        _require_release_url(item["sourceUrl"], prefix=prefix, relative=relative, label=name)

    if not stable_depot["urls"] or stable_depot["urls"][0]["name"] != "DS 稳定聚合":
        raise ContractError("stable depot must start with stable aggregation")
    for label, depot in (("stable depot", stable_depot), ("risk depot", risk_depot)):
        for position, item in enumerate(depot["urls"]):
            match = _RAW_RELEASE_URL.fullmatch(item["url"])
            if (
                not match
                or match.group(1) != release_id
                or not _SOURCE_CONFIG.fullmatch(match.group(2))
            ):
                raise ContractError(f"{label} URL {position} is outside active release configs")
            _require_release_url(
                item["url"], prefix=prefix, relative=match.group(2), label=f"{label} URL {position}"
            )
            referenced_configs.add(match.group(2))
            if label == "risk depot" and not item["name"].startswith("⚠️ "):
                raise ContractError("risk depot names must carry exactly one warning prefix")
            if item["name"].startswith("⚠️ ⚠️"):
                raise ContractError("warning prefix must not be duplicated")

    if referenced_configs != set(configs):
        missing = sorted(set(configs).difference(referenced_configs))
        extra = sorted(referenced_configs.difference(configs))
        raise ContractError(f"config closure mismatch; unreferenced={missing}, missing={extra}")
    expected_live = f"{prefix}live/stable.m3u"
    for relative, config in configs.items():
        lives = config["lives"]
        if m3u_urls:
            if lives != [{"name": "DS 稳定直播", "type": 0, "url": expected_live}]:
                raise ContractError(f"{relative} does not use the active release M3U")
        elif lives:
            raise ContractError(f"{relative} references M3U although it has no channels")


def _validate_client_vod_health_bindings(
    *,
    configs: Mapping[str, Mapping[str, Any]],
    health: Mapping[str, Any],
) -> None:
    """Bind every shipped VOD site to its source-scoped health entity."""

    health_by_source: dict[str, dict[str, Mapping[str, Any]]] = {}
    for source in health["sources"]:
        assert isinstance(source, Mapping)
        source_id = str(source["source_id"])
        health_by_source[source_id] = {
            str(item["entity_id"]): item
            for item in source["items"]
            if isinstance(item, Mapping) and item.get("entity_type") == "vod_site"
        }

    # The stable aggregate deliberately omits source_id. Recover it only from a
    # unique, independently shipped source config; never infer it from display
    # names or client-controlled keys.
    independent_by_identity: dict[tuple[int, str], tuple[str, Mapping[str, Any]]] = {}
    for relative, config in sorted(configs.items(), key=lambda item: _byte_key(item[0])):
        if relative == "configs/stable.json":
            continue
        source_id = relative.removeprefix("configs/").removesuffix(".json")
        source_health = health_by_source.get(source_id)
        if source_health is None:
            raise ContractError(f"client VOD config has no health source: {source_id}")
        for site in config["sites"]:
            site_type = site["type"]
            api = site["api"]
            normalized_api = normalize_client_url_offline(api).value
            entity_id = client_vod_entity_id(source_id, site_type, api)
            health_item = source_health.get(entity_id)
            if health_item is None:
                raise ContractError(
                    f"client VOD site has no matching health entity: {source_id}/{entity_id}"
                )
            technical = health_item["technical_status"]
            publication = health_item["publication_status"]
            capabilities = health_item["capabilities"]
            if publication == "stable":
                eligible = (
                    technical == "healthy"
                    and site_type in {0, 1}
                    and all(
                        capabilities[name] is True
                        for name in ("home", "search", "detail", "play", "media_probe")
                    )
                )
            elif publication == "experimental":
                eligible = technical == "partial"
            else:
                eligible = False
            if not eligible:
                raise ContractError(
                    f"client VOD site is not supported by publishable health: {entity_id}"
                )
            identity = (site_type, normalized_api)
            if identity in independent_by_identity:
                raise ContractError("normalized VOD site identity is duplicated across configs")
            independent_by_identity[identity] = (source_id, health_item)

    seen_stable: set[tuple[int, str]] = set()
    for site in configs["configs/stable.json"]["sites"]:
        site_type = site["type"]
        normalized_api = normalize_client_url_offline(site["api"]).value
        identity = (site_type, normalized_api)
        if identity in seen_stable:
            raise ContractError("stable config contains a duplicate normalized VOD identity")
        seen_stable.add(identity)
        binding = independent_by_identity.get(identity)
        if binding is None:
            raise ContractError("stable config contains a VOD site absent from source configs")
        source_id, health_item = binding
        expected_id = client_vod_entity_id(source_id, site_type, site["api"])
        if (
            health_item["entity_id"] != expected_id
            or health_item["technical_status"] != "healthy"
            or health_item["publication_status"] != "stable"
            or site_type not in {0, 1}
        ):
            raise ContractError("stable config VOD site is not backed by stable healthy health")


def _validate_manifest_shapes(
    root_manifest: Mapping[str, Any], release_manifest: Mapping[str, Any]
) -> None:
    _require_exact_keys(
        root_manifest,
        required={
            "schema_version",
            "generated_at",
            "active_release_id",
            "previous_commit_sha",
            "content_workflow_run_id",
            "content_workflow_run_attempt",
            "release_manifest",
            "aliases",
        },
        label="root manifest",
    )
    _require_exact_keys(
        release_manifest,
        required={
            "schema_version",
            "generated_at",
            "generation",
            "release_id",
            "generator_version",
            "source_count",
            "vod_site_count",
            "live_channel_count",
            "previous_commit_sha",
            "content_workflow_run_id",
            "content_workflow_run_attempt",
            "artifacts",
            "upstreams",
        },
        label="release manifest",
    )
    if root_manifest["schema_version"] != "1.0.0" or release_manifest["schema_version"] != "1.0.0":
        raise ContractError("unsupported manifest schema version")
    pointer = root_manifest["release_manifest"]
    if not isinstance(pointer, Mapping):
        raise ContractError("root manifest release pointer must be an object")
    _require_exact_keys(
        pointer,
        required={"path", "sha256"},
        label="root manifest release pointer",
    )
    if (
        root_manifest["generated_at"] != release_manifest["generated_at"]
        or root_manifest["previous_commit_sha"] != release_manifest["previous_commit_sha"]
    ):
        raise ContractError("root and release manifest content metadata differ")
    root_identity = (
        root_manifest["content_workflow_run_id"],
        root_manifest["content_workflow_run_attempt"],
    )
    release_identity = (
        release_manifest["content_workflow_run_id"],
        release_manifest["content_workflow_run_attempt"],
    )
    if root_identity != release_identity:
        raise ContractError("root and release manifest content identities differ")
    if (
        not isinstance(root_identity[0], str)
        or not root_identity[0]
        or not isinstance(root_identity[1], int)
        or root_identity[1] < 1
    ):
        raise ContractError("manifest content identity is invalid")
    generation = release_manifest["generation"]
    release_id = release_manifest["release_id"]
    counts = (
        release_manifest["source_count"],
        release_manifest["vod_site_count"],
        release_manifest["live_channel_count"],
    )
    if (
        not isinstance(generation, int)
        or isinstance(generation, bool)
        or generation < 1
        or release_id != f"g{generation:08d}"
    ):
        raise ContractError("release generation and release_id differ")
    if any(not isinstance(count, int) or isinstance(count, bool) or count < 0 for count in counts):
        raise ContractError("release manifest counts are invalid")
    upstreams = release_manifest["upstreams"]
    if not isinstance(upstreams, list) or release_manifest["source_count"] != len(upstreams):
        raise ContractError("release source_count and upstream records differ")
    source_ids: list[str] = []
    for upstream in upstreams:
        if not isinstance(upstream, Mapping):
            raise ContractError("release upstream entry must be an object")
        _require_exact_keys(
            upstream,
            required={
                "source_id",
                "fetch_mode",
                "reviewed_revision",
                "resolved_revision",
                "resolved_fetch_url",
                "terms_sha256",
            },
            label="release upstream",
        )
        source_id = upstream["source_id"]
        if not isinstance(source_id, str) or not _SOURCE_ID.fullmatch(source_id):
            raise ContractError("release upstream source_id is invalid")
        source_ids.append(source_id)
        _validate_public_upstream_url(upstream["resolved_fetch_url"], label=f"upstream {source_id}")
        mode = upstream["fetch_mode"]
        if mode not in {"direct_url", "github_tracked_file", "github_repository"}:
            raise ContractError(f"release upstream {source_id} fetch mode is invalid")
        revisions = (upstream["reviewed_revision"], upstream["resolved_revision"])
        if mode == "direct_url":
            if revisions != (None, None):
                raise ContractError(f"direct upstream {source_id} must not claim revisions")
        elif any(
            not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision)
            for revision in revisions
        ):
            raise ContractError(f"tracked upstream {source_id} revisions are invalid")
        terms = upstream["terms_sha256"]
        if not isinstance(terms, Mapping) or any(
            not isinstance(name, str)
            or not name
            or not isinstance(digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
            for name, digest in terms.items()
        ):
            raise ContractError(f"release upstream {source_id} terms hashes are invalid")
    if source_ids != sorted(source_ids, key=_byte_key) or len(source_ids) != len(set(source_ids)):
        raise ContractError("release upstream records are not uniquely sorted")


def _validate_state_and_report(
    root: Path,
    *,
    release_id: str,
    release_generation: int,
    content_identity: tuple[str, int],
    schemas: Path,
) -> None:
    state_path = root / "state/release.json"
    report_path = root / "dist/reports/latest.json"
    markdown_path = root / "dist/reports/latest.md"
    if not state_path.exists() and not report_path.exists() and not markdown_path.exists():
        return
    if not state_path.exists() or not report_path.exists() or not markdown_path.exists():
        raise ContractError("state and both latest reports must be present together")
    markdown_bytes = markdown_path.read_bytes()
    try:
        markdown_bytes.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ContractError("Markdown report must be UTF-8") from error
    state = _require_object(_read_json(state_path, root=root), label="state")
    report = _require_object(_read_json(report_path, root=root), label="report")
    _validate_schema(report, schemas / "report.schema.json", label="report")
    if markdown_bytes != render_latest_markdown(report):
        raise ContractError("Markdown report differs from its machine-readable JSON source")
    if state.get("schema_version") != "1.0.0":
        raise ContractError("state schema version is invalid")
    state_status = state.get("status")
    report_status = report.get("status")
    valid_status_pair = (state_status, report_status) in {
        ("pending", "pending"),
        ("success", "success"),
    } or (
        state_status == "success"
        and report_status == "safety_degraded"
        and state.get("release_kind") == ReleaseKind.SAFETY.value
        and isinstance(report.get("gate"), Mapping)
        and "safety_degraded" in report["gate"].get("reasons", [])
    )
    if not valid_status_pair:
        raise ContractError("state/report publication statuses differ or are not publishable")
    if (
        state.get("active_release_id") != release_id
        or report.get("active_release_id") != release_id
    ):
        raise ContractError("state/report active release differs from manifests")
    event_identity = (state.get("workflow_run_id"), state.get("workflow_run_attempt"))
    if event_identity != (report.get("workflow_run_id"), report.get("workflow_run_attempt")):
        raise ContractError("state/report event identities differ")
    if (
        not isinstance(event_identity[0], str)
        or not event_identity[0]
        or not isinstance(event_identity[1], int)
        or event_identity[1] < 1
    ):
        raise ContractError("state/report event identity is invalid")
    expected_candidate = f"candidate/run-{event_identity[0]}-attempt-{event_identity[1]}"
    if report.get("candidate_ref") != expected_candidate:
        raise ContractError("report candidate ref differs from event identity")
    kind = state.get("release_kind")
    if kind != report.get("release_kind"):
        raise ContractError("state/report release kinds differ")
    state_generation = state.get("generation")
    if (
        not isinstance(state_generation, int)
        or state_generation < release_generation
        or report.get("generation") != state_generation
    ):
        raise ContractError("state/report generation is invalid for active content")
    if kind == ReleaseKind.ROLLBACK.value:
        if state_generation <= release_generation:
            raise ContractError("rollback event generation must exceed target content generation")
    elif state_generation != release_generation:
        raise ContractError("new content event and release generations differ")
    report_content = report.get("content_identity")
    if (
        not isinstance(report_content, Mapping)
        or (report_content.get("workflow_run_id"), report_content.get("workflow_run_attempt"))
        != content_identity
    ):
        raise ContractError("report content identity differs from manifests")
    if kind != ReleaseKind.ROLLBACK.value and event_identity != content_identity:
        raise ContractError("new content identity must equal its publication event identity")


def validate_bundle(
    root: Path,
    *,
    schemas_dir: Path | None = None,
    expected_release_id: str | None = None,
) -> BundleValidationResult:
    """Independently re-open and validate an assembled candidate bundle."""

    schemas = _schemas_root(schemas_dir)
    root_manifest_path = root / "dist/manifest.json"
    if not root_manifest_path.is_file():
        raise ContractError("bundle is missing dist/manifest.json")
    root_manifest_bytes = root_manifest_path.read_bytes()
    root_manifest = _require_object(
        _load_json_bytes(root_manifest_bytes, label="dist/manifest.json"), label="root manifest"
    )
    release_id = root_manifest.get("active_release_id")
    if not isinstance(release_id, str) or not _RELEASE_ID.fullmatch(release_id):
        raise ContractError("root manifest active_release_id is invalid")
    if expected_release_id is not None and release_id != expected_release_id:
        raise ContractError("bundle active release does not match expected release")

    release_manifest_relative = f"dist/releases/{release_id}/manifest.json"
    pointer = root_manifest.get("release_manifest")
    if not isinstance(pointer, dict) or pointer.get("path") != release_manifest_relative:
        raise ContractError("root manifest release pointer path is invalid")
    release_manifest_path = root / release_manifest_relative
    if not release_manifest_path.is_file():
        raise ContractError("bundle is missing the active release manifest")
    release_manifest_bytes = release_manifest_path.read_bytes()
    verify_hash(pointer.get("sha256", ""), release_manifest_bytes, label=release_manifest_relative)
    release_manifest = _require_object(
        _load_json_bytes(release_manifest_bytes, label=release_manifest_relative),
        label="release manifest",
    )
    _validate_manifest_shapes(root_manifest, release_manifest)
    if release_manifest.get("release_id") != release_id:
        raise ContractError("root and release manifest IDs differ")

    artifacts = release_manifest.get("artifacts")
    aliases = root_manifest.get("aliases")
    if not isinstance(artifacts, dict) or not isinstance(aliases, dict):
        raise ContractError("manifest hashes must be objects")
    if list(artifacts) != sorted(artifacts, key=_byte_key):
        raise ContractError("release artifact keys are not deterministically sorted")
    if list(aliases) != sorted(aliases, key=_byte_key):
        raise ContractError("root alias keys are not deterministically sorted")
    if set(aliases) != set(_ROOT_ALIASES):
        raise ContractError("root manifest does not contain the exact alias set")
    if release_manifest_relative in artifacts or "dist/manifest.json" in aliases:
        raise ContractError("a manifest includes itself in its hash set")

    artifact_files: set[str] = set()
    release_prefix = f"dist/releases/{release_id}/"
    for relative, digest in artifacts.items():
        if not isinstance(relative, str) or not relative.startswith(release_prefix):
            raise ContractError("release manifest contains a cross-release path")
        if not _valid_release_artifact(relative.removeprefix(release_prefix)):
            raise ContractError(f"release manifest contains an unsupported artifact: {relative}")
        safe = ensure_relative_safe_path(relative)
        if PurePosixPath(relative).as_posix() != relative:
            raise ContractError(f"artifact path is not canonical: {relative}")
        path = root / safe
        if not path.is_file() or path.is_symlink():
            raise ContractError(f"manifest artifact is missing or unsafe: {relative}")
        verify_hash(digest, path.read_bytes(), label=relative)
        artifact_files.add(relative)

    active_dir = root / f"dist/releases/{release_id}"
    actual_release_files = {
        path.relative_to(root).as_posix() for path in active_dir.rglob("*") if path.is_file()
    }
    expected_release_files = artifact_files | {release_manifest_relative}
    if actual_release_files != expected_release_files:
        raise ContractError("active release directory has untracked or missing files")

    for relative, digest in aliases.items():
        if not isinstance(digest, str) or not _HASH.fullmatch(digest):
            raise ContractError(f"invalid alias hash for {relative}")
        alias_path = root / relative
        release_path = root / f"dist/releases/{release_id}/{relative.removeprefix('dist/')}"
        if not alias_path.is_file() or alias_path.is_symlink() or not release_path.is_file():
            raise ContractError(f"alias target is missing: {relative}")
        alias_bytes = alias_path.read_bytes()
        if alias_bytes != release_path.read_bytes():
            raise ContractError(f"alias is not byte-identical to active release: {relative}")
        verify_hash(digest, alias_bytes, label=relative)

    required_release_artifacts = {
        f"{release_prefix}index.json",
        f"{release_prefix}warehouse.json",
        f"{release_prefix}depots/stable.json",
        f"{release_prefix}depots/public-unverified.json",
        f"{release_prefix}configs/stable.json",
        f"{release_prefix}live/stable.m3u",
        f"{release_prefix}health.json",
    }
    if not required_release_artifacts.issubset(artifact_files):
        raise ContractError("release manifest is missing required client artifacts")

    index = _require_object(_read_json(root / "dist/index.json", root=root), label="index")
    warehouse = _require_object(
        _read_json(root / "dist/warehouse.json", root=root), label="warehouse"
    )
    stable_depot = _require_object(
        _read_json(active_dir / "depots/stable.json", root=root), label="stable depot"
    )
    risk_depot = _require_object(
        _read_json(active_dir / "depots/public-unverified.json", root=root), label="risk depot"
    )
    depot_schema = schemas / "depot.schema.json"
    for label, document in (
        ("index", index),
        ("warehouse", warehouse),
        ("stable depot", stable_depot),
        ("risk depot", risk_depot),
    ):
        _validate_schema(document, depot_schema, label=label)
        _validate_unique_entries(document, label=label)
        _scan_client_tree(document)

    configs: dict[str, Mapping[str, Any]] = {}
    for relative in sorted(artifact_files, key=_byte_key):
        local = relative.removeprefix(release_prefix)
        if not _SOURCE_CONFIG.fullmatch(local):
            continue
        document = _require_object(_read_json(root / relative, root=root), label=relative)
        _validate_schema(document, schemas / "tvbox-config.schema.json", label=relative)
        _validate_unique_entries(document, label=relative)
        _scan_client_tree(document)
        configs[local] = document
    if "configs/stable.json" not in configs:
        raise ContractError("stable config is absent from config closure")

    m3u_bytes = (active_dir / "live/stable.m3u").read_bytes()
    m3u_urls = _parse_m3u(m3u_bytes)
    _validate_release_closure(
        release_id=release_id,
        index=index,
        warehouse=warehouse,
        stable_depot=stable_depot,
        risk_depot=risk_depot,
        configs=configs,
        m3u_urls=m3u_urls,
    )

    health = _require_object(_read_json(root / "dist/health.json", root=root), label="health")
    if health.get("release_id") != release_id:
        raise ContractError("health release_id differs from manifests")
    generation = release_manifest.get("generation")
    if not isinstance(generation, int) or generation < 1 or health.get("generation") != generation:
        raise ContractError("release manifest and health generation differ")
    if health.get("generated_at") != release_manifest.get("generated_at"):
        raise ContractError("release manifest and health generated_at differ")
    stable_vod_count = len(configs["configs/stable.json"]["sites"])
    if release_manifest["vod_site_count"] != stable_vod_count:
        raise ContractError("release manifest VOD count differs from stable config")
    if release_manifest["live_channel_count"] != len(m3u_urls):
        raise ContractError("release manifest live count differs from M3U")
    health_result = validate_health_document(health, schemas, m3u_urls=m3u_urls)
    if m3u_bytes != health_result.canonical_m3u:
        raise ContractError("M3U bytes differ from canonical validated health rendering")
    _validate_client_vod_health_bindings(configs=configs, health=health)
    upstream_ids = frozenset(str(item["source_id"]) for item in release_manifest["upstreams"])
    if upstream_ids != health_result.source_ids:
        raise ContractError("release upstreams and health sources differ")
    source_config_ids = {
        relative.removeprefix("configs/").removesuffix(".json")
        for relative in configs
        if relative != "configs/stable.json"
    }
    if not source_config_ids.issubset(health_result.source_ids):
        raise ContractError("source config has no matching health source")
    content_identity = (
        release_manifest["content_workflow_run_id"],
        release_manifest["content_workflow_run_attempt"],
    )
    _validate_state_and_report(
        root,
        release_id=release_id,
        release_generation=generation,
        content_identity=content_identity,
        schemas=schemas,
    )
    return BundleValidationResult(
        release_id=release_id,
        generation=generation,
        artifact_count=len(artifacts),
        alias_count=len(aliases),
        source_count=int(release_manifest["source_count"]),
        vod_site_count=stable_vod_count,
        live_channel_count=len(m3u_urls),
        source_ids=health_result.source_ids,
        vod_entity_ids=health_result.vod_entity_ids,
        live_entity_ids=health_result.live_entity_ids,
        release_manifest_sha256=prefixed_sha256(release_manifest_bytes),
        root_manifest_sha256=prefixed_sha256(root_manifest_bytes),
    )
