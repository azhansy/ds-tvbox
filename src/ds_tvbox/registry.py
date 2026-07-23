"""Strict source-registry loading and semantic validation."""

from __future__ import annotations

import copy
import re
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote, urlsplit

import yaml
from jsonschema import Draft202012Validator, FormatChecker  # type: ignore[import-untyped]

from .errors import ContractError, SecurityError
from .models import (
    ClientSiteSpec,
    FetchMode,
    FetchSpec,
    HttpExceptionSpec,
    ParserKind,
    RightsStatus,
    SourceKind,
    SourceSpec,
    TermsWatchSpec,
)
from .security import normalize_url, validate_registry_host

_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_SCHEMA = _ROOT / "schemas" / "source-registry.schema.json"
_SHA40_RE = re.compile(r"^[0-9a-f]{40}$")
_SAFE_REF_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._/-]{0,253}[A-Za-z0-9])?$")
_SAFE_SPDX_RE = re.compile(
    r"^(?:NOASSERTION|[A-Za-z0-9][A-Za-z0-9.+-]*(?: WITH [A-Za-z0-9][A-Za-z0-9.+-]*)?)$"
)


class _UniqueKeyLoader(yaml.SafeLoader):
    """Safe YAML loader that preserves date scalars and rejects duplicate keys."""


# Dates in the registry are contract strings.  PyYAML otherwise silently turns
# them into ``datetime.date`` and makes JSON Schema validation environment-specific.
_UniqueKeyLoader.yaml_implicit_resolvers = copy.deepcopy(yaml.SafeLoader.yaml_implicit_resolvers)
for first_character, resolvers in list(_UniqueKeyLoader.yaml_implicit_resolvers.items()):
    _UniqueKeyLoader.yaml_implicit_resolvers[first_character] = [
        resolver for resolver in resolvers if resolver[0] != "tag:yaml.org,2002:timestamp"
    ]


def _construct_unique_mapping(
    loader: _UniqueKeyLoader,
    node: yaml.MappingNode,
    deep: bool = False,
) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    result: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in result
        except TypeError as exc:
            raise ContractError("YAML mapping key must be scalar") from exc
        if duplicate:
            mark = key_node.start_mark
            raise ContractError(
                f"duplicate YAML key {key!r} at line {mark.line + 1}, column {mark.column + 1}"
            )
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def load_yaml_strict(text: str) -> Any:
    """Parse untrusted YAML without object construction or duplicate-key loss."""

    try:
        return yaml.load(text, Loader=_UniqueKeyLoader)  # noqa: S506 - custom SafeLoader
    except ContractError:
        raise
    except yaml.YAMLError as exc:
        raise ContractError(f"invalid YAML: {exc}") from exc


def _load_schema(path: Path) -> Mapping[str, Any]:
    import json

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise ContractError(f"cannot read registry schema: {path}") from exc
    if not isinstance(value, Mapping):
        raise ContractError("registry schema root must be an object")
    return value


def _format_schema_error(error: Any) -> str:
    path = "$"
    for component in error.absolute_path:
        path += f"[{component}]" if isinstance(component, int) else f".{component}"
    return f"{path}: {error.message}"


def _validate_schema(value: Any, schema: Mapping[str, Any]) -> None:
    try:
        Draft202012Validator.check_schema(schema)
    except Exception as exc:  # pragma: no cover - a repository corruption guard
        raise ContractError("source registry JSON Schema is invalid") from exc
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(value), key=lambda error: tuple(error.absolute_path))
    if errors:
        raise ContractError("registry schema violation: " + _format_schema_error(errors[0]))


def _safe_relative_path(value: str, *, field: str) -> str:
    if not value or "\\" in value or "\x00" in value or value.startswith("/"):
        raise ContractError(f"{field} must be a non-empty POSIX relative path")
    raw_parts = value.split("/")
    if any(part in {"", ".", ".."} for part in raw_parts):
        raise ContractError(f"{field} contains an unsafe path component")
    return value


def _safe_glob(value: str, *, field: str) -> str:
    _safe_relative_path(value, field=field)
    if "[" in value or "]" in value or "{" in value or "}" in value:
        raise ContractError(f"{field} only supports *, ** and ? glob syntax")
    return value


def _validate_json_pointer(value: str, *, field: str) -> None:
    if not value.startswith("/") or "*" in value:
        raise ContractError(f"{field} must be an RFC 6901 array pointer without wildcards")
    index = 0
    while index < len(value):
        if value[index] == "~":
            if index + 1 >= len(value) or value[index + 1] not in {"0", "1"}:
                raise ContractError(f"{field} contains an invalid RFC 6901 escape")
            index += 2
        else:
            index += 1


def _validate_repository_url(value: str, *, field: str) -> None:
    try:
        normalized = normalize_url(
            value,
            allowed_hosts={"github.com"},
            allow_discovered_host=False,
        )
    except SecurityError as exc:
        raise ContractError(f"{field} must be a credential-free HTTPS GitHub URL") from exc
    if normalized.scheme != "https" or normalized.host != "github.com":
        raise ContractError(f"{field} must be a GitHub HTTPS URL")
    components = [item for item in normalized.path.split("/") if item]
    if len(components) != 2:
        raise ContractError(f"{field} must identify one GitHub owner/repository")


def _validate_fetch(source: Mapping[str, Any], allowed_hosts: frozenset[str]) -> None:
    kind = source["kind"]
    parser = source["parser"]
    fetch = source["fetch"]
    mode = fetch["mode"]
    combinations = {
        "vod_site": ({"maccms_json", "maccms_xml"}, {"direct_url"}),
        "vod_config": ({"tvbox_json", "tvbox_json5"}, {"direct_url", "github_tracked_file"}),
        "live_playlist": ({"m3u", "txt_live"}, {"direct_url", "github_tracked_file"}),
        "repository_catalog": ({"repository_catalog"}, {"github_repository"}),
    }
    parsers, modes = combinations[kind]
    if parser not in parsers or mode not in modes:
        raise ContractError(f"illegal kind/parser/fetch.mode combination for source {source['id']}")

    nullable_fields = (
        "reviewed_url",
        "repository_url",
        "track_ref",
        "config_path",
        "reviewed_revision",
    )
    if mode == "direct_url":
        if not fetch["reviewed_url"] or any(
            fetch[name] is not None for name in nullable_fields[1:]
        ):
            raise ContractError("direct_url requires reviewed_url and null GitHub fields")
        try:
            normalize_url(
                fetch["reviewed_url"],
                allowed_hosts=allowed_hosts,
                http_exceptions=tuple(_http_exception(item) for item in source["http_exceptions"]),
            )
        except SecurityError as exc:
            raise ContractError("direct reviewed_url violates source network policy") from exc
    elif mode == "github_tracked_file":
        if any(not fetch[name] for name in nullable_fields):
            raise ContractError("github_tracked_file requires all fetch fields")
        revision = fetch["reviewed_revision"]
        assert isinstance(revision, str)
        if not _SHA40_RE.fullmatch(revision):
            raise ContractError("reviewed_revision must be a lowercase 40-character commit")
        _validate_repository_url(fetch["repository_url"], field="fetch.repository_url")
        if not {"api.github.com", "raw.githubusercontent.com"}.issubset(allowed_hosts):
            raise ContractError(
                "GitHub tracking requires api.github.com and raw.githubusercontent.com"
            )
        _validate_ref(fetch["track_ref"])
        config_path = _safe_relative_path(fetch["config_path"], field="fetch.config_path")
        try:
            reviewed = normalize_url(fetch["reviewed_url"], allowed_hosts=allowed_hosts)
        except SecurityError as exc:
            raise ContractError("tracked reviewed_url violates source network policy") from exc
        decoded_path = unquote(reviewed.path)
        if f"/{revision}/" not in decoded_path or not decoded_path.endswith(f"/{config_path}"):
            raise ContractError("tracked reviewed_url must contain reviewed commit and config_path")
        repository_parts = [
            part for part in urlsplit(fetch["repository_url"]).path.split("/") if part
        ]
        reviewed_parts = [part for part in decoded_path.split("/") if part]
        if reviewed_parts[:2] != repository_parts[:2]:
            raise ContractError("tracked reviewed_url must belong to fetch.repository_url")
    else:
        if fetch["reviewed_url"] is not None or fetch["config_path"] is not None:
            raise ContractError("github_repository requires null reviewed_url/config_path")
        for name in ("repository_url", "track_ref", "reviewed_revision"):
            if not fetch[name]:
                raise ContractError(f"github_repository requires fetch.{name}")
        _validate_repository_url(fetch["repository_url"], field="fetch.repository_url")
        if not {"api.github.com", "raw.githubusercontent.com"}.issubset(allowed_hosts):
            raise ContractError(
                "GitHub tracking requires api.github.com and raw.githubusercontent.com"
            )
        _validate_ref(fetch["track_ref"])
        if not _SHA40_RE.fullmatch(fetch["reviewed_revision"]):
            raise ContractError("reviewed_revision must be a lowercase 40-character commit")


def _validate_ref(value: str) -> None:
    if (
        not _SAFE_REF_RE.fullmatch(value)
        or ".." in value
        or "//" in value
        or value.endswith(".lock")
        or "@{" in value
    ):
        raise ContractError("track_ref is not a safe Git ref name")


def _validate_terms(source: Mapping[str, Any], allowed_hosts: frozenset[str]) -> None:
    mode = source["fetch"]["mode"]
    terms = source["terms_watch"]
    github_paths: list[str] = []
    identities: set[tuple[Any, Any, Any]] = set()
    for index, term in enumerate(terms):
        identity = (term["type"], term["url"], term["path"])
        if identity in identities:
            raise ContractError("duplicate terms_watch target")
        identities.add(identity)
        if term["type"] == "github_path":
            if mode not in {"github_tracked_file", "github_repository"}:
                raise ContractError("github_path terms are illegal for direct_url")
            if term["url"] is not None or not term["path"]:
                raise ContractError("github_path term requires path and null url")
            github_paths.append(
                _safe_relative_path(term["path"], field=f"terms_watch[{index}].path")
            )
        else:
            if term["path"] is not None or not term["url"]:
                raise ContractError("url term requires url and null path")
            try:
                normalized = normalize_url(term["url"], allowed_hosts=allowed_hosts)
            except SecurityError as exc:
                raise ContractError(
                    "term URL must be fixed credential-free HTTPS on an allowed host"
                ) from exc
            if normalized.scheme != "https":
                raise ContractError("term URL must use HTTPS")
    if mode in {"github_tracked_file", "github_repository"}:
        if not github_paths:
            raise ContractError("GitHub tracking requires github_path terms")
        lower_names = {PurePosixPath(path).name.lower() for path in github_paths}
        if not any(
            name.startswith("license") or name.startswith("copying") for name in lower_names
        ):
            raise ContractError("GitHub tracking must monitor LICENSE/COPYING")
        if not any("readme" in name or "term" in name for name in lower_names):
            raise ContractError("GitHub tracking must monitor README/terms")


def _validate_catalog(source: Mapping[str, Any]) -> None:
    catalog = source["catalog"]
    if source["kind"] == "repository_catalog":
        if not isinstance(catalog, Mapping):
            raise ContractError("repository_catalog requires catalog")
    elif catalog is not None:
        raise ContractError("catalog must be null for non-catalog sources")
    if catalog is None:
        return
    globs = tuple(_safe_glob(item, field="catalog.path_globs") for item in catalog["path_globs"])
    parser_globs = tuple(
        _safe_glob(item["glob"], field="catalog.parsers_by_glob.glob")
        for item in catalog["parsers_by_glob"]
    )
    if len(set(globs)) != len(globs) or len(set(parser_globs)) != len(parser_globs):
        raise ContractError("catalog glob entries must be unique")
    if set(globs) != set(parser_globs):
        raise ContractError("each path_glob must have exactly one parser mapping")
    for array_name, pointers in catalog["selectors"].items():
        for pointer in pointers:
            _validate_json_pointer(pointer, field=f"catalog.selectors.{array_name}")
    for host in catalog["allowed_downstream_hosts"]:
        validate_registry_host(host)


def _validate_discovery_evidence(source: Mapping[str, Any]) -> None:
    evidence = source["discovery_evidence"]
    if evidence is None:
        return
    if not isinstance(evidence, Mapping):
        raise ContractError("discovery_evidence must be an object or null")
    required = {"repository_url", "revision", "config_path", "selector"}
    if set(evidence) != required:
        raise ContractError("discovery_evidence has missing or unknown fields")
    _validate_repository_url(evidence["repository_url"], field="discovery_evidence.repository_url")
    if not isinstance(evidence["revision"], str) or not _SHA40_RE.fullmatch(evidence["revision"]):
        raise ContractError("discovery_evidence.revision must be a commit SHA")
    _safe_relative_path(evidence["config_path"], field="discovery_evidence.config_path")
    selector = evidence["selector"]
    if not isinstance(selector, Mapping) or not selector:
        raise ContractError("discovery_evidence.selector must be a non-empty object")
    if any(
        not isinstance(key, str) or not isinstance(value, (str, int, bool))
        for key, value in selector.items()
    ):
        raise ContractError("discovery_evidence.selector only accepts scalar evidence")


def _validate_source(source: Mapping[str, Any]) -> None:
    allowed_hosts = frozenset(validate_registry_host(item) for item in source["allowed_hosts"])
    if len(allowed_hosts) != len(source["allowed_hosts"]):
        raise ContractError("allowed_hosts contains normalized duplicates")
    for item in source["http_exceptions"]:
        host = validate_registry_host(item["host"])
        if host not in allowed_hosts:
            raise ContractError("HTTP exception host must also be in allowed_hosts")
        # Reuse URL normalization to reject traversal/ambiguous percent encoding.
        try:
            normalize_url(
                f"http://{host}:{item['port']}{item['path_prefix']}",
                allowed_hosts=allowed_hosts,
                http_exceptions=(_http_exception(item),),
            )
        except SecurityError as exc:
            raise ContractError("invalid HTTP exception path") from exc
    if not _SAFE_SPDX_RE.fullmatch(source["license_spdx"]):
        raise ContractError("license_spdx is not a safe SPDX identifier")
    if source["kind"] == "vod_site":
        if not isinstance(source["client_site"], Mapping):
            raise ContractError("vod_site requires client_site")
    elif source["client_site"] is not None:
        raise ContractError("client_site must be null for non-vod_site sources")
    _validate_fetch(source, allowed_hosts)
    _validate_terms(source, allowed_hosts)
    _validate_catalog(source)
    _validate_discovery_evidence(source)


def _fetch_spec(value: Mapping[str, Any]) -> FetchSpec:
    return FetchSpec(
        mode=FetchMode(value["mode"]),
        reviewed_url=value["reviewed_url"],
        repository_url=value["repository_url"],
        track_ref=value["track_ref"],
        config_path=value["config_path"],
        reviewed_revision=value["reviewed_revision"],
    )


def _http_exception(value: Mapping[str, Any]) -> HttpExceptionSpec:
    return HttpExceptionSpec(
        host=value["host"],
        port=value["port"],
        path_prefix=value["path_prefix"],
        reason=value["reason"],
        reviewed_at=value["reviewed_at"],
    )


def _to_source(value: Mapping[str, Any]) -> SourceSpec:
    client_raw = value["client_site"]
    client_site = None
    if client_raw is not None:
        client_site = ClientSiteSpec(
            key=client_raw["key"],
            name=client_raw["name"],
            searchable=client_raw["searchable"],
            quick_search=client_raw["quickSearch"],
            filterable=client_raw["filterable"],
            changeable=client_raw["changeable"],
        )
    return SourceSpec(
        id=value["id"],
        kind=SourceKind(value["kind"]),
        parser=ParserKind(value["parser"]),
        enabled=value["enabled"],
        fetch=_fetch_spec(value["fetch"]),
        terms_watch=tuple(
            TermsWatchSpec(
                type=item["type"],
                url=item["url"],
                path=item["path"],
                reviewed_sha256=item["reviewed_sha256"],
            )
            for item in value["terms_watch"]
        ),
        rights_status=RightsStatus(value["rights_status"]),
        config_license_status=value["config_license_status"],
        content_rights_status=value["content_rights_status"],
        allowed_hosts=frozenset(value["allowed_hosts"]),
        allow_discovered_media_hosts=value["allow_discovered_media_hosts"],
        http_exceptions=tuple(_http_exception(item) for item in value["http_exceptions"]),
        denied_categories=tuple(value["category_policy"]["deny_names"]),
        client_site=client_site,
        catalog=copy.deepcopy(value["catalog"]),
        raw=copy.deepcopy(value),
    )


def load_registry(
    path: str | Path,
    schema_path: str | Path | None = None,
) -> tuple[SourceSpec, ...]:
    """Load, schema-check, semantically validate, and freeze a source registry."""

    registry_path = Path(path)
    try:
        text = registry_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ContractError(f"cannot read source registry: {registry_path}") from exc
    value = load_yaml_strict(text)
    schema = _load_schema(Path(schema_path) if schema_path is not None else _DEFAULT_SCHEMA)
    _validate_schema(value, schema)
    assert isinstance(value, Mapping)
    sources = value["sources"]
    ids: set[str] = set()
    result: list[SourceSpec] = []
    for source in sources:
        assert isinstance(source, Mapping)
        if source["id"] in ids:
            raise ContractError(f"duplicate source id: {source['id']}")
        ids.add(source["id"])
        _validate_source(source)
        result.append(_to_source(source))
    return tuple(result)
