"""Collector-to-publisher data artifact with a strict, non-executable envelope."""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from ds_tvbox.bundle import BundleFiles, materialize_bundle, validate_bundle
from ds_tvbox.errors import ContractError, SecurityError
from ds_tvbox.generator import ClientSiteAssignment, assign_client_site_fields
from ds_tvbox.models import (
    FetchMode,
    ParserKind,
    ReleaseKind,
    RightsStatus,
    RunContext,
    SourceKind,
    SourceSpec,
    TechnicalStatus,
)
from ds_tvbox.policy import publication_status_for
from ds_tvbox.registry import load_registry, load_yaml_strict
from ds_tvbox.reports import build_change_summary
from ds_tvbox.security import normalize_client_url_offline, validate_registry_host
from ds_tvbox.serialization import canonical_json_bytes, sha256_file, write_bytes
from ds_tvbox.upstream import github_raw_url
from ds_tvbox.validation import client_vod_entity_id, load_json, validate_schema

_RELEASE_DIR = re.compile(r"^dist/releases/g[0-9]{8}$")
_SHA40 = re.compile(r"^[0-9a-f]{40}$")
_MANDATORY_ID = re.compile(
    r"^(?:(source):([a-z0-9]+(?:-[a-z0-9]+)*)|"
    r"(vod|live-url):([a-z0-9]+(?:-[a-z0-9]+)*):([0-9a-f]{16}))$"
)
_ALLOWED_PAYLOAD_PREFIXES = ("dist/", "state/")
_RIGHTS_COUNT_SUFFIXES = (
    "verified",
    "open_license",
    "public_unverified",
    "restricted",
    "takedown",
    "unknown",
)
_NONPUBLISHABLE_RIGHTS = frozenset(
    {RightsStatus.UNKNOWN, RightsStatus.RESTRICTED, RightsStatus.TAKEDOWN}
)
_SUPPLEMENTAL_PAYLOAD_FILES = frozenset(
    {
        "state/release.json",
        "dist/reports/latest.json",
        "dist/reports/latest.md",
    }
)


@dataclass(frozen=True)
class PublishArtifact:
    root: Path
    payload_root: Path
    expected_previous_head: str | None
    release_kind: ReleaseKind
    generation: int
    release_id: str
    workflow_run_id: str
    workflow_run_attempt: int
    deletions: tuple[str, ...]
    mandatory_removal_ids: tuple[str, ...]
    release_manifest_sha256: str
    root_manifest_sha256: str


@dataclass(frozen=True)
class _TrustedGatePolicy:
    minimum_vod_sites: int
    minimum_live_channels: int
    minimum_previous_items: int
    max_new_failure_ratio: float
    failed_groups_to_abort: int

    @property
    def report_thresholds(self) -> dict[str, int | float]:
        return {
            "minimum_vod_sites": self.minimum_vod_sites,
            "minimum_live_channels": self.minimum_live_channels,
            "minimum_previous_items": self.minimum_previous_items,
            "max_new_failure_ratio": self.max_new_failure_ratio,
            "failed_groups_to_abort": self.failed_groups_to_abort,
        }


@dataclass(frozen=True)
class _TrustedDenylist:
    source_ids: frozenset[str]
    hosts: frozenset[str]
    urls: frozenset[str]


def _file_records(root: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise SecurityError(f"artifact contains symlink: {path.relative_to(root)}")
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if not relative.startswith(_ALLOWED_PAYLOAD_PREFIXES):
            raise ContractError(f"artifact payload path is not allowed: {relative}")
        records.append(
            {
                "path": relative,
                "size": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )
    return records


def _positive_int(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ContractError(f"trusted policy {label} must be a positive integer")
    return value


def _ratio(value: object, label: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ContractError(f"trusted policy {label} must be numeric")
    result = float(value)
    if not 0 <= result <= 1:
        raise ContractError(f"trusted policy {label} must be within 0..1")
    return result


def _load_trusted_gate_policy(schemas_dir: Path) -> _TrustedGatePolicy:
    policy_path = schemas_dir.resolve().parent / "config/policy.yaml"
    try:
        value = load_yaml_strict(policy_path.read_text(encoding="utf-8"))
    except OSError as error:
        raise ContractError(f"cannot read trusted policy {policy_path}") from error
    if not isinstance(value, Mapping) or value.get("version") != 1:
        raise ContractError("trusted policy version is invalid")
    minimums = value.get("minimums")
    failure_gate = value.get("failure_gate")
    outage_gate = value.get("network_outage_gate")
    if not all(isinstance(item, Mapping) for item in (minimums, failure_gate, outage_gate)):
        raise ContractError("trusted publication gate policy sections are missing")
    assert isinstance(minimums, Mapping)
    assert isinstance(failure_gate, Mapping)
    assert isinstance(outage_gate, Mapping)
    return _TrustedGatePolicy(
        minimum_vod_sites=_positive_int(minimums.get("vod_sites"), "minimums.vod_sites"),
        minimum_live_channels=_positive_int(
            minimums.get("live_channels"), "minimums.live_channels"
        ),
        minimum_previous_items=_positive_int(
            failure_gate.get("minimum_previous_items"),
            "failure_gate.minimum_previous_items",
        ),
        max_new_failure_ratio=_ratio(
            failure_gate.get("max_new_failure_ratio"),
            "failure_gate.max_new_failure_ratio",
        ),
        failed_groups_to_abort=_positive_int(
            outage_gate.get("failed_groups_to_abort"),
            "network_outage_gate.failed_groups_to_abort",
        ),
    )


def _load_trusted_denylist(schemas_dir: Path) -> _TrustedDenylist:
    repository = schemas_dir.resolve().parent
    denylist_path = repository / "sources/denylist.yaml"
    try:
        value = load_yaml_strict(denylist_path.read_text(encoding="utf-8"))
    except OSError as error:
        raise ContractError(f"cannot read trusted denylist {denylist_path}") from error
    validate_schema(value, schemas_dir / "denylist.schema.json")
    assert isinstance(value, Mapping)
    entries = value["entries"]
    assert isinstance(entries, list)
    source_ids: set[str] = set()
    hosts: set[str] = set()
    urls: set[str] = set()
    for entry in entries:
        assert isinstance(entry, Mapping)
        source_ids.update(str(item) for item in entry["source_ids"])
        for raw_host in entry["hosts"]:
            host = validate_registry_host(str(raw_host))
            if host != raw_host:
                raise ContractError("trusted denylist host is not canonical")
            hosts.add(host)
        for raw_url in entry["urls"]:
            try:
                normalized = normalize_client_url_offline(str(raw_url))
            except SecurityError as error:
                raise ContractError(
                    "trusted denylist URL is not a safe public HTTPS URL"
                ) from error
            if normalized.value != raw_url:
                raise ContractError("trusted denylist URL is not canonical")
            urls.add(normalized.value)
    return _TrustedDenylist(
        source_ids=frozenset(source_ids),
        hosts=frozenset(hosts),
        urls=frozenset(urls),
    )


def _load_trusted_registry(schemas_dir: Path) -> dict[str, SourceSpec]:
    repository = schemas_dir.resolve().parent
    sources = load_registry(
        repository / "sources/registry.yaml",
        schema_path=schemas_dir / "source-registry.schema.json",
    )
    return {source.id: source for source in sources}


def _registry_url_is_denied(value: str | None, denylist: _TrustedDenylist) -> bool:
    if value is None:
        return False
    parsed = urlsplit(value)
    host = parsed.hostname
    if host in denylist.hosts:
        return True
    try:
        normalized = normalize_client_url_offline(value).value
    except SecurityError:
        # Registry loading already established whether a reviewed URL is a safe
        # source URL.  Some explicitly reviewed fetch URLs may use an HTTP
        # exception and therefore are not client URLs; exact denylist matching
        # still applies to those values.
        normalized = value
    return normalized in denylist.urls


def _active_trusted_sources(
    trusted_sources: Mapping[str, SourceSpec], denylist: _TrustedDenylist
) -> dict[str, SourceSpec]:
    """Return the exact enabled registry set after trusted denylist subtraction."""

    active: dict[str, SourceSpec] = {}
    for source_id, source in trusted_sources.items():
        registry_urls = (
            source.fetch.reviewed_url,
            source.fetch.repository_url,
            *(term.url for term in source.terms_watch),
        )
        denied = (
            not source.enabled
            or source_id in denylist.source_ids
            or bool(source.allowed_hosts.intersection(denylist.hosts))
            or any(_registry_url_is_denied(value, denylist) for value in registry_urls)
        )
        if not denied:
            active[source_id] = source
    return active


def _trusted_upstream_url(source: SourceSpec, resolved_revision: str | None) -> str:
    fetch = source.fetch
    if fetch.mode is FetchMode.DIRECT_URL:
        assert fetch.reviewed_url is not None
        return fetch.reviewed_url
    if not isinstance(resolved_revision, str) or not _SHA40.fullmatch(resolved_revision):
        raise ContractError(f"tracked upstream {source.id} resolved revision is invalid")
    assert fetch.repository_url is not None
    if fetch.mode is FetchMode.GITHUB_TRACKED_FILE:
        assert fetch.config_path is not None
        return github_raw_url(fetch.repository_url, resolved_revision, fetch.config_path)
    return f"{fetch.repository_url}/tree/{quote(resolved_revision, safe='')}"


def _validate_trusted_upstreams(
    *,
    release_manifest: Mapping[str, Any],
    health: Mapping[str, Any],
    trusted_sources: Mapping[str, SourceSpec],
    strict_reviewed_facts: bool,
) -> None:
    """Bind sealed upstream claims to reviewed fetch and terms declarations."""

    health_by_id = {
        str(source["source_id"]): source
        for source in health["sources"]
        if isinstance(source, Mapping)
    }
    upstreams = release_manifest.get("upstreams")
    if not isinstance(upstreams, list):
        raise ContractError("release manifest upstreams are invalid")
    upstream_by_id = {
        str(upstream["source_id"]): upstream
        for upstream in upstreams
        if isinstance(upstream, Mapping)
    }
    if set(upstream_by_id) != set(health_by_id):
        raise ContractError("release upstreams and health sources differ")
    if strict_reviewed_facts and set(upstream_by_id) != set(trusted_sources):
        raise ContractError("release sources differ from active trusted registry")

    for source_id, upstream in upstream_by_id.items():
        resolved_revision = upstream.get("resolved_revision")
        if health_by_id[source_id].get("upstream_revision") != resolved_revision:
            raise ContractError(
                f"health upstream_revision differs from release upstream: {source_id}"
            )
        trusted = trusted_sources.get(source_id)
        if trusted is None:
            # A safety release can retain a source removed from the current
            # registry.  Only the Publisher, with previous HEAD, can prove that
            # it is an old fact rather than an injected source.
            if strict_reviewed_facts:
                raise ContractError(
                    f"release source is not active in trusted registry: {source_id}"
                )
            continue
        fetch = trusted.fetch
        if upstream.get("fetch_mode") != fetch.mode.value:
            raise ContractError(f"upstream fetch_mode differs from registry: {source_id}")
        if fetch.mode is FetchMode.DIRECT_URL:
            if (
                upstream.get("reviewed_revision") is not None
                or resolved_revision is not None
            ):
                raise ContractError(f"direct upstream claims a revision: {source_id}")
        elif not isinstance(resolved_revision, str) or not _SHA40.fullmatch(
            resolved_revision
        ):
            raise ContractError(f"tracked upstream revision is invalid: {source_id}")
        expected_url = _trusted_upstream_url(trusted, resolved_revision)
        if upstream.get("resolved_fetch_url") != expected_url:
            raise ContractError(
                f"upstream resolved_fetch_url differs from trusted fetch: {source_id}"
            )
        if not strict_reviewed_facts:
            continue
        if upstream.get("reviewed_revision") != fetch.reviewed_revision:
            raise ContractError(
                f"upstream reviewed_revision differs from registry: {source_id}"
            )
        expected_terms = {
            str(term.url or term.path): term.reviewed_sha256
            for term in trusted.terms_watch
        }
        if upstream.get("terms_sha256") != expected_terms:
            raise ContractError(
                f"upstream terms hashes differ from reviewed registry: {source_id}"
            )


def _validate_safety_registry_rights_floor(
    health: Mapping[str, Any], trusted_sources: Mapping[str, SourceSpec]
) -> None:
    """Current security rights may veto retention without rewriting old facts."""

    for source in health["sources"]:
        assert isinstance(source, Mapping)
        source_id = str(source["source_id"])
        trusted = trusted_sources.get(source_id)
        if trusted is not None and trusted.rights_status in {
            RightsStatus.RESTRICTED,
            RightsStatus.TAKEDOWN,
        }:
            raise ContractError(
                f"safety retains a source now blocked by registry rights: {source_id}"
            )


def _validate_trusted_registry_health(
    health: Mapping[str, Any], trusted_sources: Mapping[str, SourceSpec]
) -> None:
    """Bind every currently identifiable health rights claim to its registry source."""

    rights_by_live_id: dict[str, RightsStatus] = {}
    for source in health["sources"]:
        assert isinstance(source, Mapping)
        source_id = str(source["source_id"])
        trusted = trusted_sources.get(source_id)
        claimed_rights = RightsStatus(str(source["rights_status"]))
        if trusted is not None and claimed_rights is not trusted.rights_status:
            raise ContractError(f"health rights differ from trusted registry: {source_id}")
        statuses = [str(source["publication_status"])]
        for item in source["items"]:
            assert isinstance(item, Mapping)
            statuses.append(str(item["publication_status"]))
            if item["entity_type"] == "live_url":
                rights_by_live_id[str(item["entity_id"])] = claimed_rights
        if claimed_rights in _NONPUBLISHABLE_RIGHTS and any(
            status in {"stable", "experimental"} for status in statuses
        ):
            raise ContractError(
                f"nonpublishable registry rights claim publishable health: {source_id}"
            )

    rights_order = (
        RightsStatus.TAKEDOWN,
        RightsStatus.RESTRICTED,
        RightsStatus.UNKNOWN,
        RightsStatus.PUBLIC_UNVERIFIED,
        RightsStatus.OPEN_LICENSE,
        RightsStatus.VERIFIED,
    )
    for channel in health["channels"]:
        assert isinstance(channel, Mapping)
        selected = channel["selected_url_id"]
        candidate_rights = {
            rights_by_live_id[str(entity_id)] for entity_id in channel["candidate_url_ids"]
        }
        expected_rights = (
            rights_by_live_id[str(selected)]
            if selected is not None
            else next(right for right in rights_order if right in candidate_rights)
        )
        claimed_rights = RightsStatus(str(channel["rights_status"]))
        if claimed_rights is not expected_rights:
            raise ContractError("health channel rights differ from trusted source rights")
        if (
            claimed_rights in _NONPUBLISHABLE_RIGHTS
            and channel["publication_status"] in {"stable", "experimental"}
        ):
            raise ContractError("nonpublishable channel rights claim publishable health")


def _warned_name(value: str, rights: RightsStatus) -> str:
    name = value.strip()
    while name.startswith("⚠️"):
        name = name.removeprefix("⚠️").lstrip()
    return f"⚠️ {name}" if rights is RightsStatus.PUBLIC_UNVERIFIED else name


def _validate_site_categories(site: Mapping[str, Any], source: SourceSpec) -> None:
    raw = site.get("categories", [])
    if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
        raise ContractError(f"client categories are invalid: {source.id}")
    categories = [str(item) for item in raw]
    if categories != sorted(set(categories), key=lambda item: item.encode("utf-8")):
        raise ContractError(f"client categories are not canonical: {source.id}")
    denied = {item.strip() for item in source.denied_categories}
    if any(category.strip() in denied for category in categories):
        raise ContractError(f"denied VOD category remains in client config: {source.id}")


def _validate_dynamic_vod_site(site: Mapping[str, Any], source: SourceSpec) -> None:
    normalized = normalize_client_url_offline(str(site["api"]))
    host = urlsplit(normalized.value).hostname
    if not source.allow_discovered_media_hosts and host not in source.allowed_hosts:
        raise ContractError(f"dynamic VOD API host is outside registry policy: {source.id}")
    _validate_site_categories(site, source)


def _trusted_direct_vod_base(
    site: Mapping[str, Any], source: SourceSpec
) -> dict[str, Any]:
    if source.client_site is None or source.fetch.reviewed_url is None:
        raise ContractError(f"trusted vod_site declaration is incomplete: {source.id}")
    expected_type = {
        ParserKind.MACCMS_JSON: 1,
        ParserKind.MACCMS_XML: 0,
    }.get(source.parser)
    if expected_type is None:
        raise ContractError(f"trusted vod_site parser is unsupported: {source.id}")
    client = source.client_site
    expected: dict[str, Any] = {
        "key": client.key,
        "name": _warned_name(client.name, source.rights_status),
        "type": expected_type,
        "api": source.fetch.reviewed_url,
        "searchable": client.searchable,
        "quickSearch": client.quick_search,
        "filterable": client.filterable,
        "changeable": client.changeable,
    }
    if any(
        site.get(key) != value
        for key, value in expected.items()
        if key not in {"key", "name"}
    ):
        raise ContractError(f"direct VOD client fields differ from registry: {source.id}")
    _validate_site_categories(site, source)
    categories = site.get("categories", [])
    if categories:
        expected["categories"] = list(categories)
    return expected


def _recover_dynamic_assignment_base(
    site: Mapping[str, Any], source_id: str
) -> dict[str, Any]:
    """Recover the base key/name encoded by deterministic assignment output."""

    base = dict(site)
    key = str(site["key"])
    prefix = f"src_{source_id}_"
    if key.startswith(prefix):
        recovered = key.removeprefix(prefix)
        identity = ClientSiteAssignment(
            source_id=source_id,
            key=recovered,
            name=str(site["name"]),
            site_type=int(site["type"]),
            api=str(site["api"]),
        )
        identity_suffix = "_" + hashlib.sha256(
            f"{source_id}\0{identity.site_type}\0{identity.api}".encode()
        ).hexdigest()[:8]
        if recovered.endswith(identity_suffix):
            recovered = recovered.removesuffix(identity_suffix)
        base["key"] = recovered

    name = str(site["name"])
    numbered = re.fullmatch(r"(.+) #[2-9][0-9]*", name)
    if numbered is not None:
        name = numbered.group(1)
    key_suffix = f" [{site['key']}]"
    if name.endswith(key_suffix):
        name = name.removesuffix(key_suffix)
    source_suffix = f" [{source_id}]"
    if name.endswith(source_suffix):
        name = name.removesuffix(source_suffix)
    base["name"] = name
    return base


def _validate_source_kinds_and_client_configs(
    *,
    payload: Path,
    release_id: str,
    health: Mapping[str, Any],
    trusted_sources: Mapping[str, SourceSpec],
    strict_client_rebuild: bool,
) -> None:
    """Bind source kinds to health item types and independently shipped configs."""

    release_root = payload / f"dist/releases/{release_id}"
    stable = load_json(release_root / "configs/stable.json")
    if not isinstance(stable, Mapping) or not isinstance(stable.get("sites"), list):
        raise ContractError("stable client config is invalid")
    source_configs: dict[str, Mapping[str, Any]] = {}
    for path in sorted((release_root / "configs").glob("*.json")):
        if path.name == "stable.json":
            continue
        value = load_json(path)
        if not isinstance(value, Mapping) or not isinstance(value.get("sites"), list):
            raise ContractError(f"source client config is invalid: {path.name}")
        source_configs[path.stem] = value

    health_sources = {
        str(source["source_id"]): source
        for source in health["sources"]
        if isinstance(source, Mapping)
    }
    base_sites: dict[tuple[str, int, str], dict[str, Any]] = {}
    stable_scopes: set[tuple[str, int, str]] = set()
    global_identities: set[tuple[int, str]] = set()
    expected_config_ids: set[str] = set()

    for source_id, health_source in health_sources.items():
        trusted = trusted_sources.get(source_id)
        items = [item for item in health_source["items"] if isinstance(item, Mapping)]
        item_types = {str(item["entity_type"]) for item in items}
        config = source_configs.get(source_id)
        config_sites = list(config["sites"]) if config is not None else []
        if config_sites != sorted(
            config_sites,
            key=lambda site: str(site.get("key", "")).encode("utf-8")
            if isinstance(site, Mapping)
            else b"",
        ):
            raise ContractError(
                f"source client sites are not deterministically sorted: {source_id}"
            )

        if trusted is not None:
            if trusted.kind in {SourceKind.VOD_SITE, SourceKind.VOD_CONFIG}:
                if item_types.difference({"vod_site"}):
                    raise ContractError(f"VOD source emits a live health item: {source_id}")
            elif trusted.kind is SourceKind.LIVE_PLAYLIST:
                if item_types.difference({"live_url"}) or config is not None:
                    raise ContractError(f"live source emits VOD client content: {source_id}")
            elif items or config is not None:
                raise ContractError(f"repository catalog emits V1 client content: {source_id}")
        elif len(item_types) > 1:
            raise ContractError(f"legacy safety source mixes VOD and live items: {source_id}")
        elif item_types == {"live_url"} and config is not None:
            raise ContractError(f"legacy live source emits VOD client content: {source_id}")

        publishable_vod = {
            str(item["entity_id"]): item
            for item in items
            if item["entity_type"] == "vod_site"
            and item["publication_status"] in {"stable", "experimental"}
        }
        if publishable_vod:
            expected_config_ids.add(source_id)
        config_entity_ids: set[str] = set()
        for raw_site in config_sites:
            if not isinstance(raw_site, Mapping):
                raise ContractError(f"client VOD site is invalid: {source_id}")
            site_type = int(raw_site["type"])
            normalized_api = normalize_client_url_offline(str(raw_site["api"])).value
            if raw_site["api"] != normalized_api:
                raise ContractError(f"client VOD API is not canonical: {source_id}")
            entity_id = client_vod_entity_id(source_id, site_type, normalized_api)
            if entity_id in config_entity_ids:
                raise ContractError(f"duplicate VOD entity in source config: {source_id}")
            config_entity_ids.add(entity_id)
            identity = (site_type, normalized_api)
            if identity in global_identities:
                raise ContractError("normalized VOD site identity is duplicated across configs")
            global_identities.add(identity)
            scope = (source_id, site_type, normalized_api)
            item = publishable_vod.get(entity_id)
            if item is not None and item["publication_status"] == "stable":
                stable_scopes.add(scope)
            if not strict_client_rebuild:
                categories = raw_site.get("categories", [])
                if not isinstance(categories, list) or categories != sorted(
                    set(categories), key=lambda value: str(value).encode("utf-8")
                ):
                    raise ContractError(
                        f"safety VOD categories are not canonical: {source_id}"
                    )
                base = dict(raw_site)
            elif trusted is not None and trusted.kind is SourceKind.VOD_SITE:
                base = _trusted_direct_vod_base(raw_site, trusted)
            elif trusted is not None and trusted.kind is SourceKind.VOD_CONFIG:
                _validate_dynamic_vod_site(raw_site, trusted)
                base = _recover_dynamic_assignment_base(raw_site, source_id)
            elif trusted is not None:
                raise ContractError(f"non-VOD source has a client site: {source_id}")
            else:
                raise ContractError(
                    f"regular client source is absent from trusted registry: {source_id}"
                )
            base_sites[scope] = base
        if config_entity_ids != set(publishable_vod):
            raise ContractError(
                f"publishable VOD health and source config differ: {source_id}"
            )

    if set(source_configs) != expected_config_ids:
        raise ContractError("source config set differs from publishable VOD health")

    if strict_client_rebuild:
        assigned = assign_client_site_fields(
            tuple(
                ClientSiteAssignment(
                    source_id=source_id,
                    key=str(site["key"]),
                    name=str(site["name"]),
                    site_type=site_type,
                    api=api,
                )
                for (source_id, site_type, api), site in base_sites.items()
            )
        )
        assigned_by_scope = {
            (item.source_id, item.site_type, item.api): item for item in assigned
        }
        final_sites: dict[tuple[str, int, str], dict[str, Any]] = {}
        for scope, base in base_sites.items():
            assignment = assigned_by_scope[scope]
            final = dict(base)
            final["key"] = assignment.key
            final["name"] = assignment.name
            final_sites[scope] = final
    else:
        # A safety artifact is an exact previous-release subtraction. Re-running
        # global collision assignment after one source is removed would rename
        # retained sites and violate that contract. The privileged Publisher
        # proves these objects came unchanged from the exact previous HEAD.
        final_sites = {scope: dict(site) for scope, site in base_sites.items()}

    has_live = any(
        isinstance(channel, Mapping) and channel.get("selected_url_id") is not None
        for channel in health["channels"]
    )
    expected_lives = (
        [
            {
                "name": "DS 稳定直播",
                "type": 0,
                "url": (
                    "https://raw.githubusercontent.com/azhansy/ds-tvbox/generated/"
                    f"dist/releases/{release_id}/live/stable.m3u"
                ),
            }
        ]
        if has_live
        else []
    )
    for source_id, config in source_configs.items():
        expected_sites = sorted(
            (
                site
                for (candidate_source, _site_type, _api), site in final_sites.items()
                if candidate_source == source_id
            ),
            key=lambda site: str(site["key"]).encode("utf-8"),
        )
        expected_config = {
            "sites": expected_sites,
            "lives": expected_lives,
            "parses": [],
        }
        if dict(config) != expected_config:
            raise ContractError(
                f"source config differs from deterministic trusted rebuild: {source_id}"
            )

    expected_stable = [
        final_sites[scope]
        for scope in sorted(
            stable_scopes,
            key=lambda item: (
                item[0].encode("utf-8"),
                str(final_sites[item]["key"]).encode("utf-8"),
            ),
        )
    ]
    expected_stable_config = {
        "sites": expected_stable,
        "lives": expected_lives,
        "parses": [],
    }
    if dict(stable) != expected_stable_config:
        raise ContractError("stable config differs from deterministic trusted rebuild")


def _validate_failed_source_publication(
    health: Mapping[str, Any], trusted_sources: Mapping[str, SourceSpec]
) -> None:
    for source in health["sources"]:
        assert isinstance(source, Mapping)
        failure = source["failure_reason"]
        if failure is None:
            continue
        source_id = str(source["source_id"])
        trusted = trusted_sources.get(source_id)
        if trusted is None:
            continue
        expected = publication_status_for(
            RightsStatus(str(source["rights_status"])),
            TechnicalStatus(str(source["technical_status"])),
            entity_kind="live" if trusted.kind is SourceKind.LIVE_PLAYLIST else "vod",
            failure_reasons=(failure,),
        )
        if source["publication_status"] != expected.value:
            raise ContractError(f"failed source publication status is inconsistent: {source_id}")


def _validate_payload_manifest_closure(
    *,
    payload: Path,
    actual_paths: set[str],
    release_id: str,
    root_manifest: Mapping[str, Any],
) -> None:
    """Reject collector-carried history and every file outside active manifests."""

    release_manifest_relative = f"dist/releases/{release_id}/manifest.json"
    release_manifest = load_json(payload / release_manifest_relative)
    aliases = root_manifest.get("aliases")
    artifacts = release_manifest.get("artifacts") if isinstance(release_manifest, Mapping) else None
    if not isinstance(aliases, Mapping) or not isinstance(artifacts, Mapping):
        raise ContractError("publish payload manifests do not define a file closure")
    expected_paths = (
        {str(path) for path in artifacts}
        | {str(path) for path in aliases}
        | {
            release_manifest_relative,
            "dist/manifest.json",
        }
        | set(_SUPPLEMENTAL_PAYLOAD_FILES)
    )
    if actual_paths != expected_paths:
        extra = sorted(actual_paths.difference(expected_paths))
        missing = sorted(expected_paths.difference(actual_paths))
        raise ContractError(
            "publish artifact payload differs from active manifest closure; "
            f"extra={extra}, missing={missing}"
        )


def _denylist_matches_url(value: object, denylist: _TrustedDenylist) -> bool:
    if not isinstance(value, str):
        return False
    try:
        normalized = normalize_client_url_offline(value)
    except SecurityError as error:
        raise SecurityError("safety payload contains an invalid client URL") from error
    host = urlsplit(normalized.value).hostname
    return normalized.value in denylist.urls or host in denylist.hosts


def _validate_trusted_denylist(
    *,
    payload: Path,
    release_id: str,
    source_ids: frozenset[str],
    denylist: _TrustedDenylist,
) -> None:
    release_root = payload / f"dist/releases/{release_id}"
    blocked_sources = source_ids.intersection(denylist.source_ids)
    if blocked_sources:
        raise ContractError(
            "denylisted source remains in publish payload: " + ", ".join(sorted(blocked_sources))
        )
    for source_id in denylist.source_ids:
        if (release_root / f"configs/{source_id}.json").exists():
            raise ContractError(f"denylisted source config remains in payload: {source_id}")

    values: list[tuple[str, object]] = []
    for config_path in sorted((release_root / "configs").glob("*.json")):
        config = load_json(config_path)
        if not isinstance(config, Mapping) or not isinstance(config.get("sites"), list):
            raise ContractError("publish payload source config is invalid")
        values.extend(
            (f"{config_path.name} site API", site.get("api"))
            for site in config["sites"]
            if isinstance(site, Mapping)
        )

    m3u_path = release_root / "live/stable.m3u"
    for line in m3u_path.read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#"):
            values.append(("M3U playback URL", line))

    health = load_json(release_root / "health.json")
    if not isinstance(health, Mapping) or not isinstance(health.get("sources"), list):
        raise ContractError("publish payload health is invalid")
    for source in health["sources"]:
        if not isinstance(source, Mapping) or not isinstance(source.get("items"), list):
            raise ContractError("publish payload health source is invalid")
        for item in source["items"]:
            if not isinstance(item, Mapping) or item.get("entity_type") != "live_url":
                continue
            for key in ("normalized_url", "final_url", "logo", "epg"):
                candidate = item.get(key)
                if isinstance(candidate, str) and candidate.startswith("https://"):
                    values.append((f"health live {key}", candidate))

    release_manifest = load_json(release_root / "manifest.json")
    if not isinstance(release_manifest, Mapping) or not isinstance(
        release_manifest.get("upstreams"), list
    ):
        raise ContractError("publish payload release upstreams are invalid")
    values.extend(
        ("resolved upstream URL", item.get("resolved_fetch_url"))
        for item in release_manifest["upstreams"]
        if isinstance(item, Mapping)
    )
    for label, value in values:
        if _denylist_matches_url(value, denylist):
            raise ContractError(f"denylisted URL or host remains in {label}")


def _validate_transition(
    *,
    kind: ReleaseKind,
    generation: object,
    release_id: object,
    expected_previous_head: object,
) -> None:
    if kind is ReleaseKind.ROLLBACK:
        raise ContractError("rollback cannot enter through a publish artifact")
    if not isinstance(generation, int) or isinstance(generation, bool) or generation < 1:
        raise ContractError("publish artifact generation is invalid")
    if release_id != f"g{generation:08d}":
        raise ContractError("publish artifact release_id differs from generation")
    if kind is ReleaseKind.BOOTSTRAP:
        if generation != 1 or expected_previous_head is not None:
            raise ContractError("bootstrap artifact must start at generation 1 without a head")
        return
    if generation < 2 or not isinstance(expected_previous_head, str):
        raise ContractError("non-bootstrap artifact requires a previous generation and head")
    if not _SHA40.fullmatch(expected_previous_head):
        raise ContractError("publish artifact previous head is not a Git SHA")


def _validate_report_counts(
    *,
    report: Mapping[str, Any],
    gate: Mapping[str, Any],
    source_count: int,
    vod_site_count: int,
    live_channel_count: int,
    source_ids: frozenset[str],
    health_sources: Mapping[str, Mapping[str, Any]],
    health_channels: Sequence[Mapping[str, Any]],
    kind: ReleaseKind,
    trusted_policy: _TrustedGatePolicy,
) -> None:
    counts = report.get("counts")
    inputs = gate.get("inputs")
    sources = report.get("sources")
    if not isinstance(counts, Mapping) or not isinstance(inputs, Mapping):
        raise ContractError("publish artifact report count inputs are invalid")
    expected_counts = {
        "current_vod_sites": vod_site_count,
        "current_live_channels": live_channel_count,
    }
    for key, expected in expected_counts.items():
        if counts.get(key) != expected or inputs.get(key) != expected:
            raise ContractError(f"publish artifact {key} differs from validated client output")
    probes = gate.get("network_probes")
    if not isinstance(probes, list):
        raise ContractError("publish artifact network probes are invalid")
    expected_groups = {"github_raw", "dns_public", "cloudflare_http", "google_http"}
    probe_groups = [str(item.get("group")) for item in probes if isinstance(item, Mapping)]
    if len(probe_groups) != 4 or set(probe_groups) != expected_groups:
        raise ContractError("publish artifact network probes do not contain the trusted groups")
    failed_groups = sum(item.get("passed") is not True for item in probes)
    if inputs.get("failed_network_groups") != failed_groups:
        raise ContractError("publish artifact failed network group count is inconsistent")
    if not isinstance(sources, list):
        raise ContractError("publish artifact report sources are invalid")
    report_source_ids = [str(item.get("source_id")) for item in sources]
    if (
        len(report_source_ids) != len(set(report_source_ids))
        or report_source_ids != sorted(report_source_ids)
    ):
        raise ContractError("publish artifact report source count is invalid")
    current_source_ids: set[str] = set()
    current_rights = dict.fromkeys(_RIGHTS_COUNT_SUFFIXES, 0)
    primary_failures: Counter[str] = Counter()
    for source in sources:
        summary = source.get("change_summary")
        assert isinstance(summary, Mapping)
        current = summary.get("current")
        category = summary.get("category")
        previous = summary.get("previous")
        expected_summary = build_change_summary(
            previous if isinstance(previous, Mapping) else None,
            current if isinstance(current, Mapping) else None,
        )
        if dict(summary) != expected_summary:
            raise ContractError("report source change_summary category is inconsistent")
        if kind is ReleaseKind.BOOTSTRAP and previous is not None:
            raise ContractError("bootstrap report source claims a previous snapshot")
        if current is None:
            removed = category == "removed" and isinstance(previous, Mapping)
            audit_only_new = (
                category == "new"
                and previous is None
                and source.get("publication_status") in {"withheld", "rejected"}
            )
            if not (removed or audit_only_new):
                raise ContractError("report source without current state is not removed")
            continue
        if not isinstance(current, Mapping) or category == "removed":
            raise ContractError("report current source change summary is invalid")
        snapshot = {
            "technical_status": source.get("technical_status"),
            "publication_status": source.get("publication_status"),
            "rights_status": source.get("rights_status"),
        }
        if dict(current) != snapshot:
            raise ContractError("report current source snapshot differs from source status")
        source_id = str(source["source_id"])
        health_source = health_sources.get(source_id)
        if health_source is None:
            if source.get("publication_status") not in {"withheld", "rejected"} or category not in {
                "new",
                "withheld",
                "rejected",
                "dead",
                "degraded",
            }:
                raise ContractError("report-only current source is incorrectly publishable")
        else:
            health_snapshot = {
                "technical_status": health_source.get("technical_status"),
                "publication_status": health_source.get("publication_status"),
                "rights_status": health_source.get("rights_status"),
            }
            if snapshot != health_snapshot:
                raise ContractError("report current source status differs from validated health")
            if source.get("failure_reason") != health_source.get("failure_reason"):
                raise ContractError("report source failure differs from validated health")
            if source.get("upstream_revision") != health_source.get("upstream_revision"):
                raise ContractError("report source revision differs from validated health")
        failure = source.get("failure_reason")
        if isinstance(failure, str):
            primary_failures[failure] += 1
        current_source_ids.add(source_id)
    for source in health_sources.values():
        current_rights[str(source["rights_status"])] += 1
        if source.get("failure_reason") is None:
            for item in source["items"]:
                if isinstance(item, Mapping) and isinstance(
                    item.get("failure_reason"), str
                ):
                    primary_failures[str(item["failure_reason"])] += 1
    if not set(source_ids).issubset(current_source_ids) or len(source_ids) != source_count:
        raise ContractError("report omits a current source from validated health")
    failures = report.get("failures")
    if not isinstance(failures, Mapping) or any(
        not isinstance(failures.get(reason), int)
        or int(failures[reason]) < minimum
        for reason, minimum in primary_failures.items()
    ):
        raise ContractError("report failure counts omit current source failures")
    if any(
        counts[f"current_{suffix}"] != current_rights[suffix]
        for suffix in _RIGHTS_COUNT_SUFFIXES
    ):
        raise ContractError("publish artifact current rights counts differ from health")
    rights_count = sum(int(counts[f"current_{suffix}"]) for suffix in _RIGHTS_COUNT_SUFFIXES)
    if rights_count != source_count:
        raise ContractError("publish artifact current rights counts differ from source count")

    reasons = gate.get("reasons")
    assert isinstance(reasons, list)
    if kind in {ReleaseKind.BOOTSTRAP, ReleaseKind.REGULAR}:
        publishable_vod_ids = {
            str(item["entity_id"])
            for source in health_sources.values()
            for item in source["items"]
            if item["entity_type"] == "vod_site"
            and item["publication_status"] in {"stable", "experimental"}
        }
        live_items = {
            str(item["entity_id"]): item
            for source in health_sources.values()
            for item in source["items"]
            if item["entity_type"] == "live_url"
        }
        healthy_live_ids = {
            str(entity_id)
            for channel in health_channels
            if channel["publication_status"] == "stable"
            for entity_id in channel["candidate_url_ids"]
            if str(entity_id) in live_items
            and live_items[str(entity_id)]["technical_status"] == "healthy"
            and live_items[str(entity_id)]["publication_status"] == "stable"
        }
        if inputs.get("current_publishable_vod_items") != len(publishable_vod_ids):
            raise ContractError("publish artifact current publishable VOD input is inconsistent")
        if inputs.get("current_healthy_live_urls") != len(healthy_live_ids):
            raise ContractError("publish artifact current healthy live input is inconsistent")
        previous_vod = inputs.get("previous_vod_items")
        current_vod = inputs.get("current_publishable_vod_items")
        previous_live = inputs.get("previous_live_urls")
        current_live = inputs.get("current_healthy_live_urls")
        numeric = (previous_vod, current_vod, previous_live, current_live)
        if not all(isinstance(value, int) and not isinstance(value, bool) for value in numeric):
            raise ContractError("publish artifact batch gate inputs are invalid")
        assert isinstance(previous_vod, int) and isinstance(current_vod, int)
        assert isinstance(previous_live, int) and isinstance(current_live, int)
        for previous, current, label in (
            (previous_vod, current_vod, "VOD"),
            (previous_live, current_live, "live"),
        ):
            if previous < trusted_policy.minimum_previous_items:
                continue
            minimum_retained = previous - int(previous * trusted_policy.max_new_failure_ratio)
            if current < minimum_retained:
                raise ContractError(f"publish artifact {label} batch failure gate is bypassed")
        if failed_groups >= trusted_policy.failed_groups_to_abort:
            raise ContractError("publish artifact network outage gate is bypassed")
        if reasons:
            raise ContractError("regular publish artifact has non-publishable gate reasons")


def _validate_mandatory_removals(
    *,
    payload: Path,
    release_id: str,
    mandatory_ids: tuple[str, ...],
    source_ids: frozenset[str],
    vod_entity_ids: frozenset[str],
    live_entity_ids: frozenset[str],
) -> None:
    config_root = payload / f"dist/releases/{release_id}/configs"
    for identifier in mandatory_ids:
        match = _MANDATORY_ID.fullmatch(identifier)
        if match is None:
            raise ContractError(f"mandatory removal ID is invalid: {identifier}")
        source_kind, source_only, entity_kind, entity_source, _digest = match.groups()
        if source_kind is not None:
            assert source_only is not None
            if source_only in source_ids or (config_root / f"{source_only}.json").exists():
                raise ContractError(f"mandatory source remains in safety payload: {identifier}")
            continue
        assert entity_kind is not None and entity_source is not None
        if entity_kind == "live-url":
            if identifier in live_entity_ids:
                raise ContractError(f"mandatory live URL remains in safety payload: {identifier}")
            continue
        if identifier in vod_entity_ids:
            raise ContractError(f"mandatory VOD entity remains in safety payload: {identifier}")
        source_config = config_root / f"{entity_source}.json"
        if not source_config.exists():
            continue
        document = load_json(source_config)
        if not isinstance(document, Mapping) or not isinstance(document.get("sites"), list):
            raise ContractError("safety source config is invalid")
        if any(
            isinstance(site, Mapping)
            and client_vod_entity_id(
                entity_source, int(site.get("type", -1)), str(site.get("api", ""))
            )
            == identifier
            for site in document["sites"]
        ):
            raise ContractError(f"mandatory VOD entity remains in safety config: {identifier}")


def build_publish_artifact(
    root: Path,
    *,
    context: RunContext,
    bundle_files: BundleFiles,
    deletions: Iterable[str] = (),
    mandatory_removal_ids: Iterable[str] = (),
) -> Path:
    if root.exists() and any(root.iterdir()):
        raise ContractError("publish artifact directory must be empty")
    _validate_transition(
        kind=context.release_kind,
        generation=context.generation,
        release_id=context.release_id,
        expected_previous_head=context.previous_head,
    )
    payload = root / "payload"
    materialize_bundle(payload, bundle_files)
    deletion_values = tuple(sorted(set(deletions)))
    mandatory_values = tuple(sorted(set(mandatory_removal_ids)))
    if context.release_kind not in {ReleaseKind.REGULAR, ReleaseKind.SAFETY} and deletion_values:
        raise ContractError("bootstrap artifacts may not request historical deletion")
    if any(not _RELEASE_DIR.fullmatch(item) for item in deletion_values):
        raise ContractError("artifact deletion must target an exact release directory")
    if f"dist/releases/{context.release_id}" in deletion_values:
        raise ContractError("artifact cannot delete its current release directory")
    if any(int(item.rsplit("g", 1)[1]) >= context.generation for item in deletion_values):
        raise ContractError("artifact deletion is not a historical release")
    if context.release_kind is ReleaseKind.SAFETY and not mandatory_values:
        raise ContractError("safety artifact requires a mandatory removal set")
    if context.release_kind is not ReleaseKind.SAFETY and mandatory_values:
        raise ContractError("mandatory removals require a safety artifact")
    if any(_MANDATORY_ID.fullmatch(item) is None for item in mandatory_values):
        raise ContractError("mandatory removal ID is invalid")
    manifest = {
        "schema_version": "1.0.0",
        "expected_previous_head": context.previous_head,
        "release_kind": context.release_kind.value,
        "generation": context.generation,
        "release_id": context.release_id,
        "workflow_run_id": context.workflow_run_id,
        "workflow_run_attempt": context.workflow_run_attempt,
        "files": _file_records(payload),
        "deletions": list(deletion_values),
        "mandatory_removal_ids": list(mandatory_values),
    }
    write_bytes(root / "bundle.json", canonical_json_bytes(manifest))
    return root


def _validate_manifest_shape(value: Mapping[str, Any]) -> None:
    expected = {
        "schema_version",
        "expected_previous_head",
        "release_kind",
        "generation",
        "release_id",
        "workflow_run_id",
        "workflow_run_attempt",
        "files",
        "deletions",
        "mandatory_removal_ids",
    }
    if set(value) != expected or value.get("schema_version") != "1.0.0":
        raise ContractError("publish artifact manifest keys/version are invalid")


def validate_publish_artifact(
    root: Path,
    schemas_dir: Path,
    *,
    max_total_bytes: int = 220 * 1024 * 1024,
) -> PublishArtifact:
    root = root.resolve()
    manifest_path = root / "bundle.json"
    payload = root / "payload"
    if not manifest_path.is_file() or manifest_path.is_symlink() or not payload.is_dir():
        raise ContractError("publish artifact envelope is incomplete")
    manifest_raw = load_json(manifest_path)
    if not isinstance(manifest_raw, dict):
        raise ContractError("publish artifact manifest must be an object")
    _validate_manifest_shape(manifest_raw)
    try:
        kind = ReleaseKind(str(manifest_raw["release_kind"]))
    except ValueError as error:
        raise ContractError("publish artifact release kind is invalid") from error
    _validate_transition(
        kind=kind,
        generation=manifest_raw["generation"],
        release_id=manifest_raw["release_id"],
        expected_previous_head=manifest_raw["expected_previous_head"],
    )
    records = manifest_raw.get("files")
    if not isinstance(records, list):
        raise ContractError("publish artifact files must be an array")
    declared_paths: set[str] = set()
    total = 0
    for record in records:
        if not isinstance(record, dict) or set(record) != {"path", "size", "sha256"}:
            raise ContractError("publish artifact file record is invalid")
        relative = record.get("path")
        if not isinstance(relative, str) or relative in declared_paths:
            raise ContractError("publish artifact path is missing or duplicated")
        if not relative.startswith(_ALLOWED_PAYLOAD_PREFIXES) or ".." in Path(relative).parts:
            raise SecurityError(f"publish artifact path is unsafe: {relative!r}")
        candidate = payload / relative
        if not candidate.is_file() or candidate.is_symlink():
            raise SecurityError(f"publish artifact file is missing or unsafe: {relative}")
        size = record.get("size")
        digest = record.get("sha256")
        if not isinstance(size, int) or size < 0 or size != candidate.stat().st_size:
            raise ContractError(f"publish artifact size mismatch: {relative}")
        if not isinstance(digest, str) or digest != sha256_file(candidate):
            raise ContractError(f"publish artifact hash mismatch: {relative}")
        declared_paths.add(relative)
        total += size
    payload_entries = list(payload.rglob("*"))
    symlinks = [path for path in payload_entries if path.is_symlink()]
    if symlinks:
        raise SecurityError(
            f"publish artifact contains symlink: {symlinks[0].relative_to(payload)}"
        )
    actual_paths = {
        path.relative_to(payload).as_posix() for path in payload_entries if path.is_file()
    }
    if declared_paths != actual_paths or total > max_total_bytes:
        raise ContractError("publish artifact file closure or total size is invalid")

    validated = validate_bundle(
        payload,
        schemas_dir=schemas_dir,
        expected_release_id=str(manifest_raw["release_id"]),
    )
    state = load_json(payload / "state/release.json")
    report = load_json(payload / "dist/reports/latest.json")
    root_manifest = load_json(payload / "dist/manifest.json")
    health = load_json(payload / "dist/health.json")
    release_manifest = load_json(
        payload / f"dist/releases/{validated.release_id}/manifest.json"
    )
    if (
        not isinstance(state, dict)
        or not isinstance(report, dict)
        or not isinstance(root_manifest, dict)
        or not isinstance(health, dict)
        or not isinstance(release_manifest, dict)
    ):
        raise ContractError("pending state/report/health/manifests must be objects")
    _validate_payload_manifest_closure(
        payload=payload,
        actual_paths=actual_paths,
        release_id=validated.release_id,
        root_manifest=root_manifest,
    )
    validate_schema(report, schemas_dir / "report.schema.json")
    event = (manifest_raw["workflow_run_id"], manifest_raw["workflow_run_attempt"])
    if (
        state.get("status") != "pending"
        or report.get("status") != "pending"
        or (state.get("workflow_run_id"), state.get("workflow_run_attempt")) != event
        or (report.get("workflow_run_id"), report.get("workflow_run_attempt")) != event
    ):
        raise ContractError("pending state/report event identity is invalid")
    if validated.generation != manifest_raw["generation"]:
        raise ContractError("publish artifact generation mismatch")

    deletions = manifest_raw.get("deletions")
    mandatory = manifest_raw.get("mandatory_removal_ids")
    if not isinstance(deletions, list) or not all(isinstance(item, str) for item in deletions):
        raise ContractError("publish artifact deletions are invalid")
    if not isinstance(mandatory, list) or not all(isinstance(item, str) for item in mandatory):
        raise ContractError("mandatory removal IDs are invalid")
    if deletions != sorted(set(deletions)):
        raise ContractError("publish artifact deletions must be uniquely sorted")
    if mandatory != sorted(set(mandatory)):
        raise ContractError("mandatory removal IDs must be uniquely sorted")
    if kind not in {ReleaseKind.REGULAR, ReleaseKind.SAFETY} and deletions:
        raise ContractError("bootstrap artifact requests deletion")
    if any(not _RELEASE_DIR.fullmatch(item) for item in deletions):
        raise ContractError("artifact deletion is outside an exact release directory")
    if f"dist/releases/{validated.release_id}" in deletions or any(
        int(item.rsplit("g", 1)[1]) >= validated.generation for item in deletions
    ):
        raise ContractError("artifact deletion is not an older historical release")
    expected_head = manifest_raw.get("expected_previous_head")
    gate = report.get("gate")
    if not isinstance(gate, Mapping):
        raise ContractError("publish artifact report gate is invalid")
    if (
        state.get("release_kind") != kind.value
        or report.get("release_kind") != kind.value
        or gate.get("release_kind") != kind.value
    ):
        raise ContractError("publish artifact release kind is not bound to its payload")
    if (
        state.get("generation") != manifest_raw["generation"]
        or report.get("generation") != manifest_raw["generation"]
        or state.get("active_release_id") != manifest_raw["release_id"]
        or report.get("active_release_id") != manifest_raw["release_id"]
    ):
        raise ContractError("publish artifact release identity is not bound to its payload")
    if (
        state.get("previous_release_head_sha") != expected_head
        or report.get("previous_release_head_sha") != expected_head
        or root_manifest.get("previous_commit_sha") != expected_head
    ):
        raise ContractError("publish artifact expected head is not bound to its payload")
    if gate.get("mandatory_removal_ids") != mandatory:
        raise ContractError("publish artifact mandatory removals differ from its report")
    if gate.get("historical_deletions", []) != deletions:
        raise ContractError("publish artifact deletions differ from its report")
    trusted_denylist = _load_trusted_denylist(schemas_dir)
    registry_sources = _load_trusted_registry(schemas_dir)
    active_sources = _active_trusted_sources(registry_sources, trusted_denylist)
    _validate_trusted_denylist(
        payload=payload,
        release_id=validated.release_id,
        source_ids=validated.source_ids,
        denylist=trusted_denylist,
    )
    source_trust = active_sources if kind in {
        ReleaseKind.BOOTSTRAP,
        ReleaseKind.REGULAR,
    } else {}
    if kind is ReleaseKind.SAFETY:
        _validate_safety_registry_rights_floor(health, registry_sources)
    _validate_trusted_upstreams(
        release_manifest=release_manifest,
        health=health,
        trusted_sources=source_trust,
        strict_reviewed_facts=kind in {ReleaseKind.BOOTSTRAP, ReleaseKind.REGULAR},
    )
    _validate_trusted_registry_health(health, source_trust)
    _validate_source_kinds_and_client_configs(
        payload=payload,
        release_id=validated.release_id,
        health=health,
        trusted_sources=source_trust,
        strict_client_rebuild=kind in {
            ReleaseKind.BOOTSTRAP,
            ReleaseKind.REGULAR,
        },
    )
    _validate_failed_source_publication(health, source_trust)
    trusted_policy = _load_trusted_gate_policy(schemas_dir)
    if gate.get("thresholds") != trusted_policy.report_thresholds:
        raise ContractError("publish artifact gate thresholds differ from trusted policy")
    if gate.get("publish") is not True or gate.get("inconclusive") is not False:
        raise ContractError("publish artifact gate is not explicitly publishable and conclusive")
    health_sources = {
        str(item["source_id"]): item
        for item in health["sources"]
        if isinstance(item, Mapping)
    }
    health_channels = [
        item for item in health["channels"] if isinstance(item, Mapping)
    ]
    _validate_report_counts(
        report=report,
        gate=gate,
        source_count=validated.source_count,
        vod_site_count=validated.vod_site_count,
        live_channel_count=validated.live_channel_count,
        source_ids=validated.source_ids,
        health_sources=health_sources,
        health_channels=health_channels,
        kind=kind,
        trusted_policy=trusted_policy,
    )
    reasons = gate.get("reasons")
    if not isinstance(reasons, list):
        raise ContractError("publish artifact gate reasons are invalid")
    if kind in {ReleaseKind.BOOTSTRAP, ReleaseKind.REGULAR}:
        if mandatory:
            raise ContractError("mandatory removals require a safety artifact")
        if (
            validated.vod_site_count < trusted_policy.minimum_vod_sites
            or validated.live_channel_count < trusted_policy.minimum_live_channels
        ):
            raise ContractError("publish artifact is below trusted publication minimums")
    else:
        if not mandatory or "mandatory_removal" not in reasons:
            raise ContractError("safety artifact requires an explicit mandatory removal reason")
        _validate_mandatory_removals(
            payload=payload,
            release_id=validated.release_id,
            mandatory_ids=tuple(mandatory),
            source_ids=validated.source_ids,
            vod_entity_ids=validated.vod_entity_ids,
            live_entity_ids=validated.live_entity_ids,
        )
        below_minimum = (
            validated.vod_site_count < trusted_policy.minimum_vod_sites
            or validated.live_channel_count < trusted_policy.minimum_live_channels
        )
        if below_minimum and "safety_degraded" not in reasons:
            raise ContractError("below-minimum safety artifact is not marked safety_degraded")
    return PublishArtifact(
        root=root,
        payload_root=payload,
        expected_previous_head=expected_head,
        release_kind=kind,
        generation=int(manifest_raw["generation"]),
        release_id=str(manifest_raw["release_id"]),
        workflow_run_id=str(manifest_raw["workflow_run_id"]),
        workflow_run_attempt=int(manifest_raw["workflow_run_attempt"]),
        deletions=tuple(deletions),
        mandatory_removal_ids=tuple(mandatory),
        release_manifest_sha256=validated.release_manifest_sha256,
        root_manifest_sha256=validated.root_manifest_sha256,
    )
