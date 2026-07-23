"""Unprivileged collection-to-artifact orchestration.

The collector writes a data-only envelope.  The later privileged job re-opens and
validates that envelope without importing this module or any source adapter.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from ds_tvbox.artifact import build_publish_artifact, validate_publish_artifact
from ds_tvbox.bundle import build_bundle_files, validate_bundle
from ds_tvbox.collector import CollectResult, collect_sources
from ds_tvbox.errors import ContractError, InconclusiveError, SecurityError
from ds_tvbox.generator import (
    GeneratedClientArtifacts,
    build_client_artifacts,
    deduplicate_vod_results,
)
from ds_tvbox.gitops import Git
from ds_tvbox.health import (
    aggregate_publication_status,
    aggregate_technical_status,
    vod_entity_id,
)
from ds_tvbox.http import ByteBudget, ConcurrencyLimits, SafeHttpClient
from ds_tvbox.live import channel_identity, live_url_id, select_channels
from ds_tvbox.models import (
    ClientSiteSpec,
    FailureReason,
    FetchMode,
    FetchSpec,
    GateDecision,
    LiveCandidate,
    LiveProbeResult,
    MediaProbeResult,
    ParserKind,
    PublicationStatus,
    ReleaseKind,
    RightsStatus,
    RunContext,
    SelectedChannel,
    SourceKind,
    SourceSpec,
    TechnicalStatus,
    VodCapabilities,
    VodProbeResult,
    VodSiteCandidate,
)
from ds_tvbox.parsers import ParsedLiveEntry, parse_m3u
from ds_tvbox.policy import evaluate_gates
from ds_tvbox.registry import load_registry, load_yaml_strict
from ds_tvbox.reports import build_change_summary, build_latest_report, render_latest_markdown
from ds_tvbox.schedule import evaluate_due
from ds_tvbox.security import normalize_client_url_offline, validate_registry_host
from ds_tvbox.serialization import canonical_json_bytes, write_bytes
from ds_tvbox.upstream import Fetcher, github_raw_url
from ds_tvbox.validation import load_json, validate_release_tree, validate_schema

_MANDATORY_SECURITY_REASONS = frozenset(
    {
        FailureReason.CREDENTIAL_REQUIRED,
        FailureReason.CREDENTIAL_QUERY_REJECTED,
        FailureReason.CREDENTIAL_HEADER_REJECTED,
        FailureReason.INVALID_HEADER_SYNTAX,
        FailureReason.PRIVATE_ADDRESS_REJECTED,
        FailureReason.DANGEROUS_SCHEME_REJECTED,
        FailureReason.CLIENT_HTTP_DISALLOWED,
    }
)


@dataclass(frozen=True)
class DenylistMatchers:
    """Canonical, value-free matchers shared by active and historical scans."""

    source_ids: frozenset[str]
    hosts: frozenset[str]
    urls: frozenset[str]


def _iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ContractError("pipeline clock must return a timezone-aware datetime")
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ContractError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _decode_object(data: str, label: str) -> Mapping[str, Any]:
    try:
        value = json.loads(data, object_pairs_hook=_strict_object)
    except json.JSONDecodeError as error:
        raise ContractError(f"invalid JSON in {label}") from error
    if not isinstance(value, dict):
        raise ContractError(f"{label} must contain an object")
    return value


def _git_object(git: Git, head: str, relative: str) -> Mapping[str, Any]:
    result = git.run("show", f"{head}:{relative}", check=False)
    if result.returncode != 0:
        raise ContractError(f"generated commit is missing {relative}")
    return _decode_object(result.stdout, relative)


def _yaml_mapping(path: Path) -> Mapping[str, Any]:
    try:
        value = load_yaml_strict(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise ContractError(f"cannot read {path}") from error
    if not isinstance(value, Mapping):
        raise ContractError(f"{path} must contain a mapping")
    return value


def _positive_int(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ContractError(f"{label} must be a positive integer")
    return value


def _number(value: object, label: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ContractError(f"{label} must be numeric")
    return float(value)


def _policy_values(repository: Path) -> dict[str, int | float]:
    policy = _yaml_mapping(repository / "config/policy.yaml")
    minimums = policy.get("minimums")
    failure_gate = policy.get("failure_gate")
    outage = policy.get("network_outage_gate")
    network = policy.get("network")
    if not all(isinstance(item, Mapping) for item in (minimums, failure_gate, outage, network)):
        raise ContractError("policy sections are missing")
    assert isinstance(minimums, Mapping)
    assert isinstance(failure_gate, Mapping)
    assert isinstance(outage, Mapping)
    assert isinstance(network, Mapping)
    values: dict[str, int | float] = {
        "minimum_vod_sites": _positive_int(minimums.get("vod_sites"), "minimums.vod_sites"),
        "minimum_live_channels": _positive_int(
            minimums.get("live_channels"), "minimums.live_channels"
        ),
        "minimum_previous_items": _positive_int(
            failure_gate.get("minimum_previous_items"),
            "failure_gate.minimum_previous_items",
        ),
        "max_new_failure_ratio": _number(
            failure_gate.get("max_new_failure_ratio"),
            "failure_gate.max_new_failure_ratio",
        ),
        "failed_groups_to_abort": _positive_int(
            outage.get("failed_groups_to_abort"),
            "network_outage_gate.failed_groups_to_abort",
        ),
        "total_download_max_bytes": _positive_int(
            network.get("total_download_max_bytes"),
            "network.total_download_max_bytes",
        ),
        "global_concurrency": _positive_int(
            network.get("global_concurrency"), "network.global_concurrency"
        ),
        "per_host_concurrency": _positive_int(
            network.get("per_host_concurrency"), "network.per_host_concurrency"
        ),
    }
    ratio = values["max_new_failure_ratio"]
    if not isinstance(ratio, float) or not 0 <= ratio <= 1:
        raise ContractError("failure ratio must be within 0..1")
    return values


def _compatibility(repository: Path) -> tuple[str, str, str]:
    value = _yaml_mapping(repository / "config/compatibility.yaml")
    owner, name, generated_ref = (
        value.get("owner"),
        value.get("repository"),
        value.get("generated_ref"),
    )
    if not all(isinstance(item, str) and item for item in (owner, name, generated_ref)):
        raise ContractError("compatibility owner/repository/ref is invalid")
    assert isinstance(owner, str) and isinstance(name, str) and isinstance(generated_ref, str)
    if "/" in owner or "/" in name or generated_ref != "generated":
        raise ContractError("compatibility owner/repository/ref is unsupported")
    return owner, name, generated_ref


def _denylist_matchers(repository: Path) -> DenylistMatchers:
    value = _yaml_mapping(repository / "sources/denylist.yaml")
    validate_schema(value, repository / "schemas/denylist.schema.json")
    entries = value.get("entries")
    assert isinstance(entries, list)
    source_ids: set[str] = set()
    blocked_hosts: set[str] = set()
    blocked_urls: set[str] = set()
    for entry in entries:
        assert isinstance(entry, Mapping)
        source_ids.update(str(item) for item in entry["source_ids"])
        for raw_host in entry["hosts"]:
            host = validate_registry_host(str(raw_host))
            if host != raw_host:
                raise ContractError("denylist host must use its canonical DNS form")
            blocked_hosts.add(host)
        for raw_url in entry["urls"]:
            try:
                url = normalize_client_url_offline(str(raw_url)).value
            except (ContractError, SecurityError, ValueError) as error:
                raise ContractError(
                    "denylist URL must be canonical credential-free HTTPS"
                ) from error
            if url != raw_url:
                raise ContractError("denylist URL must use its canonical HTTPS form")
            blocked_urls.add(url)
    return DenylistMatchers(
        source_ids=frozenset(source_ids),
        hosts=frozenset(blocked_hosts),
        urls=frozenset(blocked_urls),
    )


def _matches_denylist_url(value: object, matchers: DenylistMatchers) -> bool:
    if not isinstance(value, str):
        return False
    try:
        host = urlsplit(value).hostname
    except ValueError:
        return False
    return value in matchers.urls or (host is not None and host.lower() in matchers.hosts)


def _release_source_url_matches(
    tree: Path,
    release_id: str,
    previous_health: Mapping[str, Any],
    matchers: DenylistMatchers,
) -> frozenset[str]:
    """Map active-release config, health, and upstream URLs back to source IDs."""

    matched = set(matchers.source_ids)
    source_ids: set[str] = set()
    raw_sources = previous_health.get("sources")
    if not isinstance(raw_sources, list):
        raise ContractError("previous health sources must be an array")
    for source in raw_sources:
        if not isinstance(source, Mapping) or not isinstance(source.get("source_id"), str):
            raise ContractError("previous health source is invalid")
        source_id = str(source["source_id"])
        source_ids.add(source_id)
        items = source.get("items")
        if not isinstance(items, list):
            raise ContractError("previous health source items must be an array")
        if any(
            isinstance(item, Mapping)
            and any(
                _matches_denylist_url(item.get(key), matchers)
                for key in ("normalized_url", "final_url", "logo", "epg")
            )
            for item in items
        ):
            matched.add(source_id)

    release_root = tree / f"dist/releases/{release_id}"
    manifest = load_json(release_root / "manifest.json")
    if not isinstance(manifest, Mapping) or not isinstance(manifest.get("upstreams"), list):
        raise ContractError("previous release upstreams are invalid")
    for upstream in manifest["upstreams"]:
        if not isinstance(upstream, Mapping) or not isinstance(upstream.get("source_id"), str):
            raise ContractError("previous release upstream is invalid")
        if _matches_denylist_url(upstream.get("resolved_fetch_url"), matchers):
            matched.add(str(upstream["source_id"]))

    for source_id in source_ids:
        config_path = release_root / f"configs/{source_id}.json"
        if not config_path.is_file():
            continue
        config = load_json(config_path)
        if not isinstance(config, Mapping) or not isinstance(config.get("sites"), list):
            raise ContractError(f"previous independent config is invalid: {source_id}")
        if any(
            isinstance(site, Mapping)
            and _matches_denylist_url(site.get("api"), matchers)
            for site in config["sites"]
        ):
            matched.add(source_id)
    return frozenset(matched)


def _denylisted_source_ids(
    sources: tuple[SourceSpec, ...],
    matchers: DenylistMatchers,
    *,
    previous_tree: Path | None,
    previous_release_id: str | None,
    previous_health: Mapping[str, Any] | None,
) -> frozenset[str]:
    source_ids = set(matchers.source_ids)
    for source in sources:
        registered_urls = (
            source.fetch.reviewed_url,
            source.fetch.repository_url,
            *(term.url for term in source.terms_watch),
        )
        if source.allowed_hosts.intersection(matchers.hosts) or any(
            url in matchers.urls for url in registered_urls if url is not None
        ):
            source_ids.add(source.id)
    if previous_tree is not None:
        if previous_release_id is None or previous_health is None:
            raise ContractError("previous release scan requires state and health")
        source_ids.update(
            _release_source_url_matches(
                previous_tree,
                previous_release_id,
                previous_health,
                matchers,
            )
        )
    return frozenset(source_ids)


def _apply_denylist(
    sources: tuple[SourceSpec, ...], denied: frozenset[str]
) -> tuple[SourceSpec, ...]:
    return tuple(
        replace(source, rights_status=RightsStatus.TAKEDOWN) if source.id in denied else source
        for source in sources
    )


def _without_sources(
    result: CollectResult,
    excluded_source_ids: frozenset[str],
) -> CollectResult:
    """Remove denylisted sources from publishable facts while retaining raw diagnostics."""

    if not excluded_source_ids:
        return result
    retained_live = tuple(
        item
        for item in result.live_results
        if item.candidate.source_id not in excluded_source_ids
    )
    return replace(
        result,
        sources=tuple(
            source for source in result.sources if source.id not in excluded_source_ids
        ),
        vod_results=tuple(
            item
            for item in result.vod_results
            if item.candidate.source_id not in excluded_source_ids
        ),
        live_results=retained_live,
        selected_channels=select_channels(retained_live),
        source_observations=tuple(
            item
            for item in result.source_observations
            if item.source_id not in excluded_source_ids
        ),
        catalog_results=tuple(
            item
            for item in result.catalog_results
            if item.source_id not in excluded_source_ids
        ),
        discarded_entities=tuple(
            item
            for item in result.discarded_entities
            if item.source_id not in excluded_source_ids
        ),
        upstream_revisions={
            source_id: revision
            for source_id, revision in result.upstream_revisions.items()
            if source_id not in excluded_source_ids
        },
        source_failures={
            source_id: failure
            for source_id, failure in result.source_failures.items()
            if source_id not in excluded_source_ids
        },
        enumerated_source_ids=result.enumerated_source_ids.difference(
            excluded_source_ids
        ),
    )


def _previous_baselines(
    health: Mapping[str, Any] | None,
) -> tuple[set[str], set[str], dict[str, set[str]]]:
    vod_ids: set[str] = set()
    live_ids: set[str] = set()
    items_by_source: dict[str, set[str]] = {}
    if health is None:
        return vod_ids, live_ids, items_by_source
    published_live_ids: set[str] = set()
    channels = health.get("channels", [])
    if isinstance(channels, list):
        for channel in channels:
            if not isinstance(channel, Mapping) or channel.get("publication_status") != "stable":
                continue
            candidate_ids = channel.get("candidate_url_ids", [])
            if isinstance(candidate_ids, list):
                published_live_ids.update(str(item) for item in candidate_ids)
    sources = health.get("sources", [])
    if not isinstance(sources, list):
        raise ContractError("previous health sources must be an array")
    for source in sources:
        if not isinstance(source, Mapping) or not isinstance(source.get("source_id"), str):
            raise ContractError("previous health source is invalid")
        source_id = str(source["source_id"])
        source_items = items_by_source.setdefault(source_id, set())
        items = source.get("items", [])
        if not isinstance(items, list):
            raise ContractError("previous health source items must be an array")
        for item in items:
            if not isinstance(item, Mapping) or not isinstance(item.get("entity_id"), str):
                raise ContractError("previous health item is invalid")
            entity_id = str(item["entity_id"])
            source_items.add(entity_id)
            if item.get("entity_type") == "vod_site" and item.get(
                "publication_status"
            ) in {"stable", "experimental"}:
                vod_ids.add(entity_id)
            if (
                item.get("entity_type") == "live_url"
                and item.get("technical_status") == "healthy"
                and item.get("publication_status") == "stable"
                and entity_id in published_live_ids
            ):
                live_ids.add(entity_id)
    return vod_ids, live_ids, items_by_source


def _previous_target_hashes(
    git: Git,
    previous_head: str | None,
    previous_state: Mapping[str, Any] | None,
    previous_health: Mapping[str, Any] | None,
) -> dict[str, dict[str, str]]:
    """Map redacted collector hashes back to exact previously published entities."""

    targets: dict[str, dict[str, str]] = {}
    if previous_head is None or previous_state is None or previous_health is None:
        return targets
    release_id = previous_state.get("active_release_id")
    if not isinstance(release_id, str):
        raise ContractError("previous state has no active release")
    item_ids: dict[str, set[str]] = {}
    for source in previous_health.get("sources", []):
        if not isinstance(source, Mapping) or not isinstance(source.get("source_id"), str):
            raise ContractError("previous health source is invalid")
        source_id = str(source["source_id"])
        source_items = item_ids.setdefault(source_id, set())
        for item in source.get("items", []):
            if not isinstance(item, Mapping) or not isinstance(item.get("entity_id"), str):
                raise ContractError("previous health item is invalid")
            entity_id = str(item["entity_id"])
            source_items.add(entity_id)
            final_url = item.get("final_url")
            if item.get("entity_type") == "live_url" and isinstance(final_url, str):
                targets.setdefault(source_id, {})[
                    hashlib.sha256(final_url.encode("utf-8")).hexdigest()
                ] = entity_id
    for source_id, known_ids in item_ids.items():
        result = git.run(
            "show",
            f"{previous_head}:dist/releases/{release_id}/configs/{source_id}.json",
            check=False,
        )
        if result.returncode != 0:
            continue
        config = _decode_object(result.stdout, f"previous config {source_id}")
        sites = config.get("sites")
        if not isinstance(sites, list):
            raise ContractError(f"previous config sites are invalid: {source_id}")
        for site in sites:
            if (
                not isinstance(site, Mapping)
                or not isinstance(site.get("type"), int)
                or not isinstance(site.get("api"), str)
            ):
                raise ContractError(f"previous config site is invalid: {source_id}")
            api = str(site["api"])
            fingerprint = hashlib.sha256(f"{site['type']}{api}".encode()).hexdigest()[:16]
            entity_id = f"vod:{source_id}:{fingerprint}"
            if entity_id in known_ids:
                targets.setdefault(source_id, {})[
                    hashlib.sha256(api.encode("utf-8")).hexdigest()
                ] = entity_id
    return targets


def _current_sets(result: CollectResult) -> tuple[set[str], set[str], int, int]:
    vod_ids = {
        vod_entity_id(item.candidate)
        for item in result.vod_results
        if item.publication_status in {PublicationStatus.STABLE, PublicationStatus.EXPERIMENTAL}
    }
    live_ids = {
        live_url_id(item.candidate)
        for channel in result.selected_channels
        for item in channel.candidates
        if item.technical_status is TechnicalStatus.HEALTHY
        and item.publication_status is PublicationStatus.STABLE
    }
    stable_vod = sum(
        item.publication_status is PublicationStatus.STABLE for item in result.vod_results
    )
    return vod_ids, live_ids, stable_vod, len(result.selected_channels)


def _mandatory_removals(
    result: CollectResult,
    previous_by_source: Mapping[str, set[str]],
    previous_targets: Mapping[str, Mapping[str, str]],
    denied_sources: frozenset[str],
) -> tuple[tuple[str, ...], frozenset[str], tuple[str, ...]]:
    # A matcher is mandatory only while it is present in the active baseline.
    # This lets the run after a successful safety publication recover to a
    # regular release even when the denylist entry intentionally remains.
    rights_sources = set(denied_sources).intersection(previous_by_source)
    rights_sources.update(
        source.id
        for source in result.sources
        if source.rights_status in {RightsStatus.RESTRICTED, RightsStatus.TAKEDOWN}
        and source.id in previous_by_source
    )
    terms_sources = {
        source_id
        for source_id, (_technical, reason) in result.source_failures.items()
        if reason is FailureReason.TERMS_CHANGED and source_id in previous_by_source
    }
    security_sources = {
        source_id
        for source_id, (_technical, reason) in result.source_failures.items()
        if reason in _MANDATORY_SECURITY_REASONS and source_id in previous_by_source
    }
    mandatory_sources = rights_sources | terms_sources | security_sources
    identifiers = {f"source:{source_id}" for source_id in mandatory_sources}
    for source_id in mandatory_sources:
        identifiers.update(previous_by_source.get(source_id, set()))
    historical_identifiers = {
        f"source:{source_id}" for source_id in rights_sources | security_sources
    }
    for discarded in result.discarded_entities:
        if discarded.failure_reason not in _MANDATORY_SECURITY_REASONS:
            continue
        entity_id = previous_targets.get(discarded.source_id, {}).get(
            discarded.target_hash
        )
        if entity_id is not None:
            identifiers.add(entity_id)
            historical_identifiers.add(entity_id)
    for vod_result in result.vod_results:
        entity_id = vod_entity_id(vod_result.candidate)
        if (
            vod_result.failure_reason in _MANDATORY_SECURITY_REASONS
            and entity_id
            in previous_by_source.get(vod_result.candidate.source_id, set())
        ):
            identifiers.add(entity_id)
            historical_identifiers.add(entity_id)
    for live_result in result.live_results:
        entity_id = live_url_id(live_result.candidate)
        if (
            live_result.failure_reason in _MANDATORY_SECURITY_REASONS
            and entity_id
            in previous_by_source.get(live_result.candidate.source_id, set())
        ):
            identifiers.add(entity_id)
            historical_identifiers.add(entity_id)
    return (
        tuple(sorted(identifiers)),
        frozenset(mandatory_sources),
        tuple(sorted(historical_identifiers)),
    )


def _previous_live_items(
    previous_health: Mapping[str, Any],
) -> dict[str, tuple[str, Mapping[str, Any]]]:
    output: dict[str, tuple[str, Mapping[str, Any]]] = {}
    for source in previous_health.get("sources", []):
        if not isinstance(source, Mapping) or not isinstance(source.get("source_id"), str):
            continue
        source_id = str(source["source_id"])
        for item in source.get("items", []):
            if (
                isinstance(item, Mapping)
                and item.get("entity_type") == "live_url"
                and isinstance(item.get("entity_id"), str)
            ):
                output[str(item["entity_id"])] = (source_id, item)
    return output


def _previous_live_metadata_text(
    item: Mapping[str, Any],
    playlist_entry: ParsedLiveEntry | None,
    field: str,
) -> str | None:
    value = item.get(field)
    if isinstance(value, str):
        return value
    fallback = getattr(playlist_entry, field, None) if playlist_entry is not None else None
    return fallback if isinstance(fallback, str) else None


def _unwarned_name(value: str) -> str:
    name = value.strip()
    while name.startswith("⚠️"):
        name = name.removeprefix("⚠️").lstrip()
    if not name:
        raise ContractError("previous source line has no usable name")
    return name


def _previous_source_specs(
    tree: Path,
    old_release_id: str,
    previous_health: Mapping[str, Any],
    release_manifest: Mapping[str, Any],
) -> tuple[SourceSpec, ...]:
    """Reconstruct generator-only source facts from the immutable old release."""

    release_root = tree / f"dist/releases/{old_release_id}"
    index = load_json(release_root / "index.json")
    if not isinstance(index, Mapping) or not isinstance(index.get("urls"), list):
        raise ContractError("previous release index is invalid")
    line_names: dict[str, str] = {}
    for line in index["urls"]:
        if not isinstance(line, Mapping) or not isinstance(line.get("url"), str):
            raise ContractError("previous release index line is invalid")
        name = line.get("name")
        if not isinstance(name, str):
            raise ContractError("previous release index line name is invalid")
        path = urlsplit(str(line["url"])).path
        for config_path in (release_root / "configs").glob("*.json"):
            source_id = config_path.stem
            if source_id != "stable" and path.endswith(f"/configs/{source_id}.json"):
                line_names[source_id] = name

    upstream_items = release_manifest.get("upstreams")
    if not isinstance(upstream_items, list):
        raise ContractError("previous release upstreams are invalid")
    upstream_by_source: dict[str, Mapping[str, Any]] = {}
    for item in upstream_items:
        if not isinstance(item, Mapping) or not isinstance(item.get("source_id"), str):
            raise ContractError("previous release upstream is invalid")
        upstream_by_source[str(item["source_id"])] = item

    raw_sources = previous_health.get("sources")
    if not isinstance(raw_sources, list):
        raise ContractError("previous health sources must be an array")
    sources: list[SourceSpec] = []
    for health_source in raw_sources:
        if not isinstance(health_source, Mapping) or not isinstance(
            health_source.get("source_id"), str
        ):
            raise ContractError("previous health source is invalid")
        source_id = str(health_source["source_id"])
        try:
            rights = RightsStatus(str(health_source["rights_status"]))
        except (KeyError, ValueError) as error:
            raise ContractError(f"previous source rights are invalid: {source_id}") from error
        upstream = upstream_by_source.get(source_id)
        if upstream is None or not isinstance(upstream.get("resolved_fetch_url"), str):
            raise ContractError(f"previous source upstream is missing: {source_id}")
        resolved_url = str(upstream["resolved_fetch_url"])
        host = urlsplit(resolved_url).hostname
        if host is None:
            raise ContractError(f"previous source upstream URL is invalid: {source_id}")
        config_path = release_root / f"configs/{source_id}.json"
        has_vod_config = config_path.is_file()
        line_name = line_names.get(source_id)
        if has_vod_config and line_name is None:
            raise ContractError(f"previous source index line is missing: {source_id}")
        client_site: ClientSiteSpec | None = None
        if has_vod_config:
            independent = load_json(config_path)
            if not isinstance(independent, Mapping) or not isinstance(
                independent.get("sites"), list
            ):
                raise ContractError(
                    f"previous independent config is invalid: {source_id}"
                )
            sites = independent["sites"]
            if len(sites) != 1 or not isinstance(sites[0], Mapping):
                raise ContractError(
                    f"previous direct VOD client facts are ambiguous: {source_id}"
                )
            site = sites[0]
            if not isinstance(site.get("key"), str) or not isinstance(
                site.get("name"), str
            ):
                raise ContractError(
                    f"previous direct VOD client identity is invalid: {source_id}"
                )
            flags: dict[str, int] = {}
            for field in ("searchable", "quickSearch", "filterable", "changeable"):
                value = site.get(field)
                if (
                    not isinstance(value, int)
                    or isinstance(value, bool)
                    or value not in {0, 1}
                ):
                    raise ContractError(
                        f"previous direct VOD client flag is invalid: {source_id}.{field}"
                    )
                flags[field] = value
            client_site = ClientSiteSpec(
                key=str(site["key"]),
                name=_unwarned_name(str(site["name"])),
                searchable=flags["searchable"],
                quick_search=flags["quickSearch"],
                filterable=flags["filterable"],
                changeable=flags["changeable"],
            )
        sources.append(
            SourceSpec(
                id=source_id,
                kind=SourceKind.VOD_SITE if has_vod_config else SourceKind.LIVE_PLAYLIST,
                parser=ParserKind.MACCMS_JSON if has_vod_config else ParserKind.M3U,
                enabled=True,
                fetch=FetchSpec(
                    mode=FetchMode.DIRECT_URL,
                    reviewed_url=resolved_url,
                    repository_url=None,
                    track_ref=None,
                    config_path=None,
                    reviewed_revision=None,
                ),
                terms_watch=(),
                rights_status=rights,
                config_license_status="previous_release",
                content_rights_status="previous_release",
                allowed_hosts=frozenset({host.lower()}),
                allow_discovered_media_hosts=True,
                http_exceptions=(),
                denied_categories=(),
                client_site=client_site,
                catalog=None,
                raw={},
            )
        )
    ordered = tuple(sorted(sources, key=lambda source: source.id.encode("utf-8")))
    client_sites = [source.client_site for source in ordered if source.client_site is not None]
    for field in ("key", "name"):
        values = [getattr(site, field) for site in client_sites]
        if len(values) != len(set(values)):
            raise ContractError(
                f"previous direct VOD {field} collision cannot be reconstructed"
            )
    return ordered


def _safety_vod_results(
    tree: Path,
    old_release_id: str,
    sources: tuple[SourceSpec, ...],
    mandatory_ids: frozenset[str],
) -> tuple[VodProbeResult, ...]:
    stable = load_json(tree / f"dist/releases/{old_release_id}/configs/stable.json")
    if not isinstance(stable, Mapping) or not isinstance(stable.get("sites"), list):
        raise ContractError("previous stable VOD config is invalid")
    stable_identities = {
        (site.get("type"), site.get("api"))
        for site in stable["sites"]
        if isinstance(site, Mapping)
    }
    results: list[VodProbeResult] = []
    for source in sources:
        config_path = tree / f"dist/releases/{old_release_id}/configs/{source.id}.json"
        if not config_path.is_file():
            continue
        config = load_json(config_path)
        if not isinstance(config, Mapping) or not isinstance(config.get("sites"), list):
            raise ContractError(f"previous independent config is invalid: {source.id}")
        for site in config["sites"]:
            if not isinstance(site, Mapping):
                raise ContractError(f"previous site is invalid: {source.id}")
            required = (
                "key",
                "name",
                "type",
                "api",
                "searchable",
                "quickSearch",
                "filterable",
                "changeable",
            )
            if any(key not in site for key in required):
                raise ContractError(f"previous site is incomplete: {source.id}")
            categories = site.get("categories", [])
            if not isinstance(categories, list) or not all(
                isinstance(item, str) for item in categories
            ):
                raise ContractError(f"previous site categories are invalid: {source.id}")
            candidate = VodSiteCandidate(
                source_id=source.id,
                key=str(site["key"]),
                name=str(site["name"]),
                type=int(site["type"]),
                api=str(site["api"]),
                searchable=int(site["searchable"]),
                quick_search=int(site["quickSearch"]),
                filterable=int(site["filterable"]),
                changeable=int(site["changeable"]),
                categories=tuple(categories),
                rights_status=source.rights_status,
            )
            if vod_entity_id(candidate) in mandatory_ids:
                continue
            stable_item = (candidate.type, candidate.api) in stable_identities
            results.append(
                VodProbeResult(
                    candidate=candidate,
                    technical_status=(
                        TechnicalStatus.HEALTHY if stable_item else TechnicalStatus.PARTIAL
                    ),
                    publication_status=(
                        PublicationStatus.STABLE
                        if stable_item
                        else PublicationStatus.EXPERIMENTAL
                    ),
                    capabilities=VodCapabilities(
                        home=True,
                        search=bool(candidate.searchable),
                        detail=True,
                        play=True,
                        media_probe=stable_item,
                    ),
                    failure_reason=None,
                )
            )
    return tuple(results)


def _safety_live_results(
    tree: Path,
    old_release_id: str,
    sources: tuple[SourceSpec, ...],
    previous_health: Mapping[str, Any],
    mandatory_ids: frozenset[str],
) -> tuple[LiveProbeResult, ...]:
    playlist = parse_m3u(
        (tree / f"dist/releases/{old_release_id}/live/stable.m3u").read_bytes()
    )
    source_map = {source.id: source for source in sources}
    playlist_by_url = {entry.url: entry for entry in playlist.entries}
    channels = previous_health.get("channels")
    if not isinstance(channels, list):
        raise ContractError("previous health channels must be an array")
    channel_by_id: dict[str, Mapping[str, Any]] = {}
    for channel in channels:
        if not isinstance(channel, Mapping) or not isinstance(channel.get("entity_id"), str):
            raise ContractError("previous health channel is invalid")
        channel_by_id[str(channel["entity_id"])] = channel

    results: list[LiveProbeResult] = []
    for entity_id, (source_id, previous_item) in sorted(
        _previous_live_items(previous_health).items()
    ):
        if entity_id in mandatory_ids or source_id not in source_map:
            continue
        source = source_map[source_id]
        final_url = previous_item.get("final_url")
        normalized_url = previous_item.get("normalized_url")
        if not isinstance(normalized_url, str):
            # Legacy health can only be reconstructed if the client-visible URL
            # is also the exact original identity. Redirected hashes are not
            # reversible, so guessing would violate safety subtraction.
            if isinstance(final_url, str):
                fallback = LiveCandidate(
                    source_id=source_id,
                    name="legacy",
                    original_url=final_url,
                    normalized_url=final_url,
                    rights_status=source.rights_status,
                )
                if live_url_id(fallback) == entity_id:
                    normalized_url = final_url
            if not isinstance(normalized_url, str):
                raise ContractError(f"previous live URL identity is unavailable: {entity_id}")

        channel_id = previous_item.get("channel_id")
        if not isinstance(channel_id, str) or channel_id not in channel_by_id:
            raise ContractError(f"previous live channel is unavailable: {entity_id}")
        channel = channel_by_id[channel_id]
        playlist_entry = playlist_by_url.get(str(final_url)) if isinstance(final_url, str) else None

        name = previous_item.get("name")
        if not isinstance(name, str) or not name.strip():
            if playlist_entry is not None:
                name = _unwarned_name(playlist_entry.name)
            elif channel.get("identity_basis") == "source_name":
                normalized_identity = str(channel.get("normalized_identity", ""))
                prefix = f"{source_id}:"
                if normalized_identity.startswith(prefix):
                    name = normalized_identity.removeprefix(prefix)
            if not isinstance(name, str) or not name.strip():
                raise ContractError(f"previous live channel metadata is unavailable: {entity_id}")

        tvg_id = _previous_live_metadata_text(previous_item, playlist_entry, "tvg_id")
        if tvg_id is None and channel.get("identity_basis") == "tvg_id":
            raw_identity = channel.get("normalized_identity")
            if isinstance(raw_identity, str) and raw_identity:
                tvg_id = raw_identity
        candidate = LiveCandidate(
            source_id=source_id,
            name=name,
            original_url=normalized_url,
            normalized_url=normalized_url,
            rights_status=source.rights_status,
            tvg_id=tvg_id,
            group=_previous_live_metadata_text(previous_item, playlist_entry, "group"),
            logo=_previous_live_metadata_text(previous_item, playlist_entry, "logo"),
            epg=_previous_live_metadata_text(previous_item, playlist_entry, "epg"),
        )
        if live_url_id(candidate) != entity_id:
            raise ContractError(f"previous live URL identity mismatch: {entity_id}")
        if channel_identity(candidate)[0] != channel_id:
            raise ContractError(f"previous live channel identity mismatch: {entity_id}")

        try:
            technical = TechnicalStatus(str(previous_item["technical_status"]))
            publication = PublicationStatus(str(previous_item["publication_status"]))
        except (KeyError, ValueError) as error:
            raise ContractError(f"previous live status is invalid: {entity_id}") from error
        raw_reason = previous_item.get("failure_reason")
        try:
            failure_reason = FailureReason(str(raw_reason)) if raw_reason is not None else None
            secondary_reasons = tuple(
                FailureReason(str(item)) for item in previous_item.get("secondary_reasons", [])
            )
        except ValueError as error:
            raise ContractError(f"previous live failure reason is invalid: {entity_id}") from error
        media_path_score = int(previous_item.get("media_path_score", 0))
        media_ok = isinstance(final_url, str) and media_path_score in {1, 2}
        history = previous_item.get("response_ms_history", [])
        if not isinstance(history, list) or not all(
            isinstance(item, int) and not isinstance(item, bool) and item >= 0 for item in history
        ):
            raise ContractError(f"previous live response history is invalid: {entity_id}")
        results.append(
            LiveProbeResult(
                candidate=candidate,
                technical_status=technical,
                publication_status=publication,
                media=MediaProbeResult(
                    ok=media_ok,
                    final_url=str(final_url) if isinstance(final_url, str) else None,
                    response_ms=(
                        int(previous_item["response_ms"])
                        if isinstance(previous_item.get("response_ms"), int)
                        and not isinstance(previous_item.get("response_ms"), bool)
                        else None
                    ),
                    media_path_score=media_path_score,
                    width=(
                        int(previous_item["width"])
                        if isinstance(previous_item.get("width"), int)
                        and not isinstance(previous_item.get("width"), bool)
                        else None
                    ),
                    height=(
                        int(previous_item["height"])
                        if isinstance(previous_item.get("height"), int)
                        and not isinstance(previous_item.get("height"), bool)
                        else None
                    ),
                    bandwidth=(
                        int(previous_item["bandwidth"])
                        if isinstance(previous_item.get("bandwidth"), int)
                        and not isinstance(previous_item.get("bandwidth"), bool)
                        else None
                    ),
                    failure_reason=failure_reason,
                ),
                consecutive_successes=int(previous_item.get("consecutive_successes", 0)),
                consecutive_failures=int(previous_item.get("consecutive_failures", 0)),
                last_success_at=(
                    str(previous_item["last_success_at"])
                    if previous_item.get("last_success_at") is not None
                    else None
                ),
                failure_reason=failure_reason,
                secondary_reasons=secondary_reasons,
                response_ms_history=tuple(history),
            )
        )
    return tuple(results)


def _safety_health_document(
    *,
    previous_health: Mapping[str, Any],
    context: RunContext,
    mandatory_ids: frozenset[str],
    mandatory_sources: frozenset[str],
    selected_channels: tuple[SelectedChannel, ...] = (),
) -> dict[str, Any]:
    """Derive health as a strict previous-health subtraction without fake probes."""

    retained_live_items: dict[str, Mapping[str, Any]] = {}
    retained_live_rights: dict[str, RightsStatus] = {}
    sources: list[dict[str, Any]] = []
    previous_entity_ids: set[str] = set()
    retained_entity_ids: set[str] = set()
    previous_source_ids: set[str] = set()
    previous_sources = previous_health.get("sources")
    if not isinstance(previous_sources, list):
        raise ContractError("previous health sources must be an array")
    for previous_source in previous_sources:
        if not isinstance(previous_source, Mapping) or not isinstance(
            previous_source.get("source_id"), str
        ):
            raise ContractError("previous health source is invalid")
        source_id = str(previous_source["source_id"])
        if source_id in previous_source_ids:
            raise ContractError(f"previous health source is duplicated: {source_id}")
        previous_source_ids.add(source_id)
        try:
            source_rights = RightsStatus(str(previous_source["rights_status"]))
        except (KeyError, ValueError) as error:
            raise ContractError(
                f"previous health source rights are invalid: {source_id}"
            ) from error
        if "failure_reason" not in previous_source:
            raise ContractError(f"previous health source failure reason is missing: {source_id}")
        previous_items = previous_source.get("items")
        if not isinstance(previous_items, list):
            raise ContractError("previous health source items must be an array")
        retained_items: list[dict[str, Any]] = []
        for previous_item in previous_items:
            if not isinstance(previous_item, Mapping) or not isinstance(
                previous_item.get("entity_id"), str
            ):
                raise ContractError("previous health item is invalid")
            entity_id = str(previous_item["entity_id"])
            if entity_id in previous_entity_ids:
                raise ContractError(f"previous health item is duplicated: {entity_id}")
            previous_entity_ids.add(entity_id)
            if source_id in mandatory_sources or entity_id in mandatory_ids:
                continue
            retained_entity_ids.add(entity_id)
            retained_item = dict(previous_item)
            if retained_item.get("entity_type") == "live_url":
                if not isinstance(retained_item.get("channel_id"), str):
                    raise ContractError(f"previous live item channel is invalid: {entity_id}")
                retained_live_items[entity_id] = retained_item
                retained_live_rights[entity_id] = source_rights
            retained_items.append(retained_item)
        if source_id in mandatory_sources:
            continue
        retained_source = dict(previous_source)
        retained_source["last_checked_at"] = context.generated_at
        retained_source["items"] = retained_items
        if previous_source["failure_reason"] is None:
            try:
                technical = aggregate_technical_status(
                    [
                        TechnicalStatus(str(item["technical_status"]))
                        for item in retained_items
                    ]
                )
                publication = aggregate_publication_status(
                    [
                        PublicationStatus(str(item["publication_status"]))
                        for item in retained_items
                    ]
                )
            except (KeyError, ValueError) as error:
                raise ContractError(
                    f"previous health item status is invalid: {source_id}"
                ) from error
            retained_source["technical_status"] = technical.value
            retained_source["publication_status"] = (
                PublicationStatus.REJECTED.value
                if source_rights in {RightsStatus.RESTRICTED, RightsStatus.TAKEDOWN}
                else publication.value
            )
        sources.append(retained_source)

    channels: list[dict[str, Any]] = []
    previous_channels = previous_health.get("channels")
    if not isinstance(previous_channels, list):
        raise ContractError("previous health channels must be an array")
    selected_by_channel: dict[str, SelectedChannel] = {}
    for selected_channel in selected_channels:
        if selected_channel.channel_id in selected_by_channel:
            raise ContractError(
                f"safety selected channel is duplicated: {selected_channel.channel_id}"
            )
        selected_by_channel[selected_channel.channel_id] = selected_channel
    previous_channel_ids: set[str] = set()
    retained_channel_candidates: set[str] = set()
    retained_channel_ids: set[str] = set()
    for previous_channel in previous_channels:
        if not isinstance(previous_channel, Mapping) or not isinstance(
            previous_channel.get("entity_id"), str
        ):
            raise ContractError("previous health channel is invalid")
        channel_id = str(previous_channel["entity_id"])
        if channel_id in previous_channel_ids:
            raise ContractError(f"previous health channel is duplicated: {channel_id}")
        previous_channel_ids.add(channel_id)
        candidate_ids = previous_channel.get("candidate_url_ids")
        if not isinstance(candidate_ids, list) or not all(
            isinstance(item, str) for item in candidate_ids
        ):
            raise ContractError("previous health channel candidates are invalid")
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ContractError(f"previous health channel candidates are duplicated: {channel_id}")
        retained_candidates = sorted(
            item for item in candidate_ids if item in retained_live_items
        )
        if not retained_candidates:
            continue
        for entity_id in retained_candidates:
            item_channel_id = retained_live_items[entity_id].get("channel_id")
            if item_channel_id != channel_id:
                raise ContractError(
                    f"previous live item is assigned to a different channel: {entity_id}"
                )
            if entity_id in retained_channel_candidates:
                raise ContractError(
                    f"previous live item belongs to multiple channels: {entity_id}"
                )
            retained_channel_candidates.add(entity_id)
        channel = dict(previous_channel)
        channel["candidate_url_ids"] = retained_candidates
        selected = selected_by_channel.get(channel_id)
        selected_url_id: str | None = None
        if selected is not None:
            selected_url_id = live_url_id(selected.selected.candidate)
            if selected_url_id not in retained_candidates:
                raise ContractError("safety selected a live URL outside the retained baseline")
        try:
            technical = aggregate_technical_status(
                [
                    TechnicalStatus(str(retained_live_items[item]["technical_status"]))
                    for item in retained_candidates
                ]
            )
            publication = (
                PublicationStatus.STABLE
                if selected_url_id is not None
                else aggregate_publication_status(
                    [
                        PublicationStatus(
                            str(retained_live_items[item]["publication_status"])
                        )
                        for item in retained_candidates
                    ]
                )
            )
        except (KeyError, ValueError) as error:
            raise ContractError(
                f"previous live item status is invalid for channel: {channel_id}"
            ) from error
        if selected_url_id is not None:
            rights = retained_live_rights[selected_url_id]
        else:
            present_rights = {
                retained_live_rights[item] for item in retained_candidates
            }
            rights = next(
                status
                for status in (
                    RightsStatus.TAKEDOWN,
                    RightsStatus.RESTRICTED,
                    RightsStatus.UNKNOWN,
                    RightsStatus.PUBLIC_UNVERIFIED,
                    RightsStatus.OPEN_LICENSE,
                    RightsStatus.VERIFIED,
                )
                if status in present_rights
            )
        channel["technical_status"] = technical.value
        channel["publication_status"] = publication.value
        channel["rights_status"] = rights.value
        channel["selected_url_id"] = selected_url_id
        channels.append(channel)
        retained_channel_ids.add(channel_id)

    if not retained_entity_ids.issubset(previous_entity_ids):
        raise ContractError("safety health introduced a new entity")
    if retained_channel_candidates != set(retained_live_items):
        raise ContractError("previous health has a retained live item without its channel")
    unknown_selected = set(selected_by_channel).difference(retained_channel_ids)
    if unknown_selected:
        raise ContractError(
            f"safety selected a channel outside the retained baseline: {sorted(unknown_selected)}"
        )
    document = dict(previous_health)
    document["generated_at"] = context.generated_at
    document["generation"] = context.generation
    document["release_id"] = context.release_id
    document["sources"] = sorted(sources, key=lambda item: str(item["source_id"]))
    document["channels"] = sorted(channels, key=lambda item: str(item["entity_id"]))
    return document


def _derive_safety_artifacts(
    *,
    git: Git,
    previous_head: str,
    previous_health: Mapping[str, Any],
    previous_state: Mapping[str, Any],
    context: RunContext,
    mandatory_ids: tuple[str, ...],
    mandatory_sources: frozenset[str],
    schemas: Path,
) -> tuple[
    GeneratedClientArtifacts,
    dict[str, Any],
    tuple[SelectedChannel, ...],
    tuple[dict[str, Any], ...],
]:
    old_release_id = previous_state.get("active_release_id")
    if not isinstance(old_release_id, str):
        raise ContractError("previous state has no active release")
    mandatory = frozenset(mandatory_ids)
    with git.worktree(previous_head) as tree:
        validate_bundle(tree, schemas_dir=schemas, expected_release_id=old_release_id)
        validate_release_tree(
            tree,
            schemas,
            owner=context.owner,
            repository=context.repository,
            expected_status="success",
        )
        release_manifest = load_json(
            tree / f"dist/releases/{old_release_id}/manifest.json"
        )
        if not isinstance(release_manifest, Mapping) or not isinstance(
            release_manifest.get("upstreams"), list
        ):
            raise ContractError("previous release upstreams are invalid")
        previous_sources = _previous_source_specs(
            tree,
            old_release_id,
            previous_health,
            release_manifest,
        )
        retained_sources = tuple(
            source for source in previous_sources if source.id not in mandatory_sources
        )
        vod_results = _safety_vod_results(
            tree, old_release_id, retained_sources, mandatory
        )
        live_results = _safety_live_results(
            tree,
            old_release_id,
            retained_sources,
            previous_health,
            mandatory,
        )
        retained_source_ids = {source.id for source in retained_sources}
        upstreams = tuple(
            dict(item)
            for item in release_manifest["upstreams"]
            if isinstance(item, Mapping) and item.get("source_id") in retained_source_ids
        )
    selected = select_channels(live_results)
    client = build_client_artifacts(
        context=context,
        sources=retained_sources,
        vod_results=vod_results,
        channels=selected,
    )
    health = _safety_health_document(
        previous_health=previous_health,
        context=context,
        mandatory_ids=mandatory,
        mandatory_sources=mandatory_sources,
        selected_channels=selected,
    )
    return client, health, selected, upstreams


def _rights_counts(health: Mapping[str, Any] | None, prefix: str) -> dict[str, int]:
    counts = {f"{prefix}_{status.value}": 0 for status in RightsStatus}
    for source in (health or {}).get("sources", []):
        if not isinstance(source, Mapping):
            continue
        key = f"{prefix}_{source.get('rights_status')}"
        if key in counts:
            counts[key] += 1
    return counts


def _counts(
    previous_health: Mapping[str, Any] | None,
    current_health: Mapping[str, Any],
    *,
    previous_vod_sites: int,
    previous_live_channels: int,
    current_vod_sites: int,
    current_live_channels: int,
) -> dict[str, int]:
    return {
        "previous_vod_sites": previous_vod_sites,
        "current_vod_sites": current_vod_sites,
        "previous_live_channels": previous_live_channels,
        "current_live_channels": current_live_channels,
        **_rights_counts(previous_health, "previous"),
        **_rights_counts(current_health, "current"),
    }


def _source_report(
    result: CollectResult,
    health: Mapping[str, Any],
    previous_health: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    health_sources: dict[str, Mapping[str, Any]] = {
        str(item["source_id"]): item
        for item in health.get("sources", [])
        if isinstance(item, Mapping) and isinstance(item.get("source_id"), str)
    }
    previous_sources: dict[str, Mapping[str, Any]] = {
        str(item["source_id"]): item
        for item in (previous_health or {}).get("sources", [])
        if isinstance(item, Mapping) and isinstance(item.get("source_id"), str)
    }
    observations = {item.source_id: item for item in result.source_observations}
    source_specs = {item.id: item for item in result.sources}
    reports: list[dict[str, Any]] = []
    source_ids = set(previous_sources) | set(health_sources) | set(observations) | set(source_specs)
    for source_id in sorted(source_ids, key=lambda item: item.encode("utf-8")):
        observation = observations.get(source_id)
        source = source_specs.get(source_id)
        current_health = health_sources.get(source_id)
        previous = previous_sources.get(source_id)
        source_failure = result.source_failures.get(source_id)
        display_current = current_health
        if display_current is None and (observation is not None or source is not None):
            technical = (
                observation.technical_status
                if observation is not None
                else source_failure[0]
                if source_failure is not None
                else TechnicalStatus.UNKNOWN
            )
            rights = source.rights_status if source is not None else RightsStatus.UNKNOWN
            display_current = {
                "technical_status": technical.value,
                "publication_status": (
                    PublicationStatus.REJECTED.value
                    if rights in {RightsStatus.RESTRICTED, RightsStatus.TAKEDOWN}
                    else PublicationStatus.WITHHELD.value
                ),
                "rights_status": rights.value,
            }
        display = display_current or previous or {}
        failure_reason = (
            observation.failure_reason.value
            if observation is not None and observation.failure_reason is not None
            else source_failure[1].value
            if source_failure is not None and source_failure[1] is not None
            else display.get("failure_reason")
        )
        reports.append(
            {
                "source_id": source_id,
                "technical_status": str(
                    display.get(
                        "technical_status",
                        observation.technical_status.value
                        if observation is not None
                        else TechnicalStatus.UNKNOWN.value,
                    )
                ),
                "publication_status": str(display.get("publication_status", "withheld")),
                "rights_status": str(
                    display.get(
                        "rights_status",
                        source.rights_status.value if source is not None else "unknown",
                    )
                ),
                "failure_reason": failure_reason,
                "secondary_reasons": (
                    [item.value for item in observation.secondary_reasons]
                    if observation is not None
                    else []
                ),
                "upstream_revision": (
                    observation.resolved_revision
                    if observation is not None
                    else display.get("upstream_revision")
                ),
                # Only sealed health is a current release snapshot. Collection-only
                # observations remain useful top-level audit facts, but cannot make
                # a mandatory subtraction look like the source survived.
                "change_summary": build_change_summary(previous, current_health),
            }
        )
    return reports


def _entity_failure_reasons(result: CollectResult) -> tuple[str, ...]:
    return (
        *(item.failure_reason.value for item in result.discarded_entities),
        *(
            item.failure_reason.value
            for item in result.vod_results
            if item.failure_reason is not None
        ),
        *(
            item.failure_reason.value
            for item in result.live_results
            if item.failure_reason is not None
        ),
    )


def _gate_report_payload(
    gate: GateDecision,
    *,
    reasons: list[str],
    deletions: tuple[str, ...],
    previous_vod_items: int,
    current_vod_items: int,
    previous_live_urls: int,
    current_live_urls: int,
    current_vod_sites: int,
    current_live_channels: int,
    result: CollectResult,
    policy: Mapping[str, int | float],
) -> dict[str, Any]:
    return {
        "publish": gate.publish,
        "inconclusive": gate.inconclusive,
        "release_kind": gate.release_kind.value,
        "reasons": reasons,
        "mandatory_removal_ids": list(gate.mandatory_removal_ids),
        "historical_deletions": list(deletions),
        "inputs": {
            "previous_vod_items": previous_vod_items,
            "current_publishable_vod_items": current_vod_items,
            "previous_live_urls": previous_live_urls,
            "current_healthy_live_urls": current_live_urls,
            "current_vod_sites": current_vod_sites,
            "current_live_channels": current_live_channels,
            "failed_network_groups": result.failed_network_groups,
        },
        "thresholds": {
            "minimum_vod_sites": int(policy["minimum_vod_sites"]),
            "minimum_live_channels": int(policy["minimum_live_channels"]),
            "minimum_previous_items": int(policy["minimum_previous_items"]),
            "max_new_failure_ratio": float(policy["max_new_failure_ratio"]),
            "failed_groups_to_abort": int(policy["failed_groups_to_abort"]),
        },
        "network_probes": [
            {
                "group": item.group,
                "passed": item.passed,
                "attempts": item.attempts,
                "elapsed_ms": item.elapsed_ms,
                "detail": item.detail,
            }
            for item in result.network_probes
        ],
    }


def _upstream_records(result: CollectResult) -> list[dict[str, Any]]:
    source_map = {source.id: source for source in result.sources if source.enabled}
    records: list[dict[str, Any]] = []
    for observation in result.source_observations:
        if observation.source_id not in source_map:
            continue
        source = source_map[observation.source_id]
        resolved_url = observation.resolved_fetch_url
        resolved_revision = observation.resolved_revision
        if resolved_url is None and source.fetch.mode.value == "direct_url":
            resolved_url = source.fetch.reviewed_url
        if (
            resolved_url is None
            and source.fetch.repository_url is not None
        ):
            resolved_revision = resolved_revision or source.fetch.reviewed_revision
            if resolved_revision is None:
                raise ContractError(f"enabled source has no auditable revision: {source.id}")
            if source.fetch.config_path is not None:
                resolved_url = github_raw_url(
                    source.fetch.repository_url,
                    resolved_revision,
                    source.fetch.config_path,
                )
            else:
                resolved_url = (
                    f"{source.fetch.repository_url}/tree/"
                    f"{quote(resolved_revision, safe='')}"
                )
        if resolved_url is None:
            raise ContractError(f"enabled source has no auditable fetch URL: {source.id}")
        records.append(
            {
                "source_id": source.id,
                "fetch_mode": source.fetch.mode.value,
                "reviewed_revision": source.fetch.reviewed_revision,
                "resolved_revision": resolved_revision,
                "resolved_fetch_url": resolved_url,
                "terms_sha256": dict(observation.terms_sha256),
            }
        )
    missing = set(source_map).difference(item["source_id"] for item in records)
    if missing:
        raise ContractError(f"enabled sources are missing observations: {sorted(missing)}")
    records.sort(key=lambda item: str(item["source_id"]).encode("utf-8"))
    return records


def _historical_deletions(
    git: Git,
    previous_head: str | None,
    historical_identifiers: tuple[str, ...],
    denylist_matchers: DenylistMatchers,
) -> tuple[str, ...]:
    if previous_head is None or (
        not historical_identifiers
        and not denylist_matchers.source_ids
        and not denylist_matchers.hosts
        and not denylist_matchers.urls
    ):
        return ()
    identifier_set = set(historical_identifiers)
    source_ids = {
        identifier.removeprefix("source:")
        for identifier in identifier_set
        if identifier.startswith("source:")
    }
    releases: set[str] = set()
    with git.worktree(previous_head) as tree:
        for health_path in sorted((tree / "dist/releases").glob("g*/health.json")):
            release_root = health_path.parent
            relative_release = release_root.relative_to(tree).as_posix()
            health = load_json(health_path)
            if not isinstance(health, Mapping):
                raise ContractError(f"historical health is invalid: {relative_release}")
            matched = False
            for source in health.get("sources", []):
                if not isinstance(source, Mapping) or not isinstance(
                    source.get("source_id"), str
                ):
                    raise ContractError(
                        f"historical health source is invalid: {relative_release}"
                    )
                source_id = str(source["source_id"])
                if source_id in source_ids or source_id in denylist_matchers.source_ids:
                    matched = True
                    break
                items = source.get("items")
                if not isinstance(items, list):
                    raise ContractError(
                        f"historical health source items are invalid: {relative_release}"
                    )
                for item in items:
                    if not isinstance(item, Mapping):
                        raise ContractError(
                            f"historical health item is invalid: {relative_release}"
                        )
                    if (
                        isinstance(item.get("entity_id"), str)
                        and str(item["entity_id"]) in identifier_set
                    ) or any(
                        _matches_denylist_url(item.get(key), denylist_matchers)
                        for key in ("normalized_url", "final_url", "logo", "epg")
                    ):
                        matched = True
                        break
                if matched:
                    break

            manifest_path = release_root / "manifest.json"
            manifest = load_json(manifest_path)
            if not isinstance(manifest, Mapping) or not isinstance(
                manifest.get("upstreams"), list
            ):
                raise ContractError(f"historical manifest is invalid: {relative_release}")
            if not matched:
                matched = any(
                    isinstance(item, Mapping)
                    and (
                        item.get("source_id") in denylist_matchers.source_ids
                        or _matches_denylist_url(
                            item.get("resolved_fetch_url"), denylist_matchers
                        )
                    )
                    for item in manifest["upstreams"]
                )

            if not matched:
                for config_path in sorted((release_root / "configs").glob("*.json")):
                    config = load_json(config_path)
                    if not isinstance(config, Mapping) or not isinstance(
                        config.get("sites"), list
                    ):
                        raise ContractError(
                            f"historical config is invalid: {config_path.relative_to(tree)}"
                        )
                    if (
                        config_path.stem in denylist_matchers.source_ids
                        or any(
                            isinstance(site, Mapping)
                            and _matches_denylist_url(site.get("api"), denylist_matchers)
                            for site in config["sites"]
                        )
                    ):
                        matched = True
                        break

            if not matched:
                playlist = parse_m3u((release_root / "live/stable.m3u").read_bytes())
                matched = any(
                    _matches_denylist_url(entry.url, denylist_matchers)
                    for entry in playlist.entries
                )
            if matched:
                releases.add(relative_release)
    return tuple(sorted(releases))


def _run_identity(environment: Mapping[str, str]) -> tuple[str, int]:
    run_id = environment.get("GITHUB_RUN_ID")
    raw_attempt = environment.get("GITHUB_RUN_ATTEMPT")
    if not run_id or not raw_attempt:
        raise ContractError("GITHUB_RUN_ID and GITHUB_RUN_ATTEMPT are required")
    try:
        attempt = int(raw_attempt)
    except ValueError as error:
        raise ContractError("GITHUB_RUN_ATTEMPT must be an integer") from error
    if attempt < 1:
        raise ContractError("GITHUB_RUN_ATTEMPT must be positive")
    return run_id, attempt


def collect_publish_artifact(
    *,
    repository: Path,
    output: Path,
    force: bool,
    bootstrap: bool,
    http_client: Fetcher | None = None,
    clock: Callable[[], datetime] | None = None,
    environment: Mapping[str, str] | None = None,
) -> Path:
    """Collect enabled sources and build one independently publishable artifact."""

    root = repository.resolve()
    destination = output.resolve()
    if destination == root or root in destination.parents and destination.name in {"src", "tests"}:
        raise ContractError("artifact output overlaps a protected source directory")
    if destination.exists() and any(destination.iterdir()):
        raise ContractError("action artifact output must be empty")
    destination.mkdir(parents=True, exist_ok=True)

    now = clock or (lambda: datetime.now(UTC))
    started_at = _iso(now())
    run_id, attempt = _run_identity(environment or os.environ)
    owner, repository_name, generated_ref = _compatibility(root)
    policy = _policy_values(root)
    git = Git(root)
    previous_head = git.remote_head(generated_ref)
    previous_state: Mapping[str, Any] | None = None
    previous_health: Mapping[str, Any] | None = None
    if previous_head is not None:
        git.fetch_sha(previous_head)
        previous_state = _git_object(git, previous_head, "state/release.json")
        previous_health = _git_object(git, previous_head, "dist/health.json")

    due = evaluate_due(
        now=datetime.fromisoformat(started_at.replace("Z", "+00:00")),
        state=previous_state,
        generated_ref_exists=previous_head is not None,
        force=force,
        bootstrap=bootstrap,
    )
    if not due.should_refresh:
        raise ContractError(f"refresh is not due: {due.reason}")
    generation = 1 if previous_state is None else int(previous_state["generation"]) + 1
    initial_kind = ReleaseKind.BOOTSTRAP if bootstrap else ReleaseKind.REGULAR

    registry = tuple(load_registry(root / "sources/registry.yaml"))
    denylist_matchers = _denylist_matchers(root)
    previous_release_id = (
        str(previous_state["active_release_id"])
        if previous_state is not None
        and isinstance(previous_state.get("active_release_id"), str)
        else None
    )
    if previous_head is not None:
        with git.worktree(previous_head) as previous_tree:
            denied_sources = _denylisted_source_ids(
                registry,
                denylist_matchers,
                previous_tree=previous_tree,
                previous_release_id=previous_release_id,
                previous_health=previous_health,
            )
    else:
        denied_sources = _denylisted_source_ids(
            registry,
            denylist_matchers,
            previous_tree=None,
            previous_release_id=None,
            previous_health=None,
        )
    sources = _apply_denylist(registry, denied_sources)
    if http_client is None:
        http_client = SafeHttpClient(
            budget=ByteBudget(int(policy["total_download_max_bytes"])),
            concurrency=ConcurrencyLimits(
                int(policy["global_concurrency"]), int(policy["per_host_concurrency"])
            ),
        )
    collected = collect_sources(
        sources=sources,
        http_client=http_client,
        checked_at=started_at,
        previous_health=previous_health,
    )
    publishable_collected = _without_sources(collected, denied_sources)
    publishable_collected = replace(
        publishable_collected,
        vod_results=deduplicate_vod_results(
            publishable_collected.vod_results,
            publishable_collected.sources,
        ),
    )
    release_id = f"g{generation:08d}"
    current_health = publishable_collected.build_health(
        generation=generation,
        release_id=release_id,
        previous_health=previous_health,
    )
    previous_vod, previous_live, previous_by_source = _previous_baselines(previous_health)
    current_vod, current_live, stable_vod_count, live_channel_count = _current_sets(
        publishable_collected
    )
    previous_targets = _previous_target_hashes(
        git, previous_head, previous_state, previous_health
    )
    mandatory_ids, mandatory_sources, historical_identifiers = _mandatory_removals(
        collected, previous_by_source, previous_targets, denied_sources
    )
    deletions = _historical_deletions(
        git,
        previous_head,
        historical_identifiers,
        denylist_matchers,
    )
    gate = evaluate_gates(
        release_kind=initial_kind,
        previous_vod_ids=previous_vod,
        current_publishable_vod_ids=current_vod,
        previous_live_url_ids=previous_live,
        current_healthy_live_url_ids=current_live,
        current_vod_sites=stable_vod_count,
        current_live_channels=live_channel_count,
        minimum_vod_sites=int(policy["minimum_vod_sites"]),
        minimum_live_channels=int(policy["minimum_live_channels"]),
        minimum_previous_items=int(policy["minimum_previous_items"]),
        max_new_failure_ratio=float(policy["max_new_failure_ratio"]),
        failed_network_groups=collected.failed_network_groups,
        failed_groups_to_abort=int(policy["failed_groups_to_abort"]),
        state_available=bootstrap or previous_state is not None,
        previous_release_known=bootstrap or previous_health is not None,
        mandatory_removal_ids=mandatory_ids,
    )
    context = RunContext(
        owner=owner,
        repository=repository_name,
        generated_ref=generated_ref,
        workflow_run_id=run_id,
        workflow_run_attempt=attempt,
        generated_at=started_at,
        generation=generation,
        release_kind=gate.release_kind,
        previous_head=previous_head,
        previous_last_success_at=(
            str(previous_state["last_success_at"])
            if previous_state is not None and previous_state.get("last_success_at") is not None
            else None
        ),
    )
    candidates = collected.candidates_report(
        workflow_run_id=run_id,
        workflow_run_attempt=attempt,
    )
    validate_schema(candidates, root / "schemas/candidates.schema.json")
    write_bytes(destination / "reports/candidates.json", canonical_json_bytes(candidates))

    previous_channels = sum(
        isinstance(item, Mapping) and item.get("publication_status") == "stable"
        for item in (previous_health or {}).get("channels", [])
    )
    if not gate.publish:
        gate_report = _gate_report_payload(
            gate,
            reasons=list(gate.reasons),
            deletions=deletions,
            previous_vod_items=len(previous_vod),
            current_vod_items=len(current_vod),
            previous_live_urls=len(previous_live),
            current_live_urls=len(current_live),
            current_vod_sites=stable_vod_count,
            current_live_channels=live_channel_count,
            result=collected,
            policy=policy,
        )
        report = build_latest_report(
            context,
            status="inconclusive",
            started_at=started_at,
            finished_at=_iso(now()),
            due=due.due,
            forced=due.forced,
            recovery_due=due.recovery_due,
            sources=_source_report(collected, current_health, previous_health),
            counts=_counts(
                previous_health,
                current_health,
                previous_vod_sites=len(previous_vod),
                previous_live_channels=previous_channels,
                current_vod_sites=stable_vod_count,
                current_live_channels=live_channel_count,
            ),
            gate=gate_report,
            previous_release_head_sha=previous_head,
            candidate_ref=None,
            content_identity=None,
            entity_failure_reasons=_entity_failure_reasons(collected),
        )
        validate_schema(report, root / "schemas/report.schema.json")
        write_bytes(destination / "reports/latest.json", canonical_json_bytes(report))
        write_bytes(destination / "reports/latest.md", render_latest_markdown(report))
        raise InconclusiveError("refresh blocked by gates: " + ",".join(gate.reasons))

    if gate.release_kind is ReleaseKind.SAFETY:
        if previous_head is None or previous_state is None or previous_health is None:
            raise ContractError("safety release requires a validated previous release")
        (
            client_artifacts,
            current_health,
            _selected_channels,
            upstream_records,
        ) = _derive_safety_artifacts(
            git=git,
            previous_head=previous_head,
            previous_health=previous_health,
            previous_state=previous_state,
            context=context,
            mandatory_ids=mandatory_ids,
            mandatory_sources=mandatory_sources,
            schemas=root / "schemas",
        )
    else:
        client_artifacts = build_client_artifacts(
            context=context,
            sources=publishable_collected.sources,
            vod_results=publishable_collected.vod_results,
            channels=publishable_collected.selected_channels,
        )
        upstream_records = tuple(_upstream_records(publishable_collected))

    reported_current_vod = current_vod
    reported_current_live = current_live
    if gate.release_kind is ReleaseKind.SAFETY:
        reported_current_vod, reported_current_live, _items = _previous_baselines(
            current_health
        )

    report_counts = _counts(
        previous_health,
        current_health,
        previous_vod_sites=len(previous_vod),
        previous_live_channels=previous_channels,
        current_vod_sites=client_artifacts.vod_site_count,
        current_live_channels=client_artifacts.live_channel_count,
    )
    gate_reasons = [reason for reason in gate.reasons if reason != "safety_degraded"]
    if (
        gate.release_kind is ReleaseKind.SAFETY
        and (
            client_artifacts.vod_site_count < int(policy["minimum_vod_sites"])
            or client_artifacts.live_channel_count < int(policy["minimum_live_channels"])
        )
        and "safety_degraded" not in gate_reasons
    ):
        gate_reasons.append("safety_degraded")
    gate_report = _gate_report_payload(
        gate,
        reasons=gate_reasons,
        deletions=deletions,
        previous_vod_items=len(previous_vod),
        current_vod_items=len(reported_current_vod),
        previous_live_urls=len(previous_live),
        current_live_urls=len(reported_current_live),
        current_vod_sites=client_artifacts.vod_site_count,
        current_live_channels=client_artifacts.live_channel_count,
        result=collected,
        policy=policy,
    )
    finished_at = _iso(now())
    report = build_latest_report(
        context,
        status="pending",
        started_at=started_at,
        finished_at=finished_at,
        due=due.due,
        forced=due.forced,
        recovery_due=due.recovery_due,
        sources=_source_report(collected, current_health, previous_health),
        counts=report_counts,
        gate=gate_report,
        previous_release_head_sha=previous_head,
        candidate_ref=context.candidate_ref,
        content_identity={
            "workflow_run_id": run_id,
            "workflow_run_attempt": attempt,
        },
        entity_failure_reasons=_entity_failure_reasons(collected),
    )
    state = {
        "schema_version": "1.0.0",
        "status": "pending",
        "release_kind": gate.release_kind.value,
        "generation": generation,
        "active_release_id": context.release_id,
        "last_publish_at": previous_state.get("last_publish_at") if previous_state else None,
        "last_success_at": previous_state.get("last_success_at") if previous_state else None,
        "content_commit_sha": None,
        "previous_release_head_sha": previous_head,
        "workflow_run_id": run_id,
        "workflow_run_attempt": attempt,
    }
    bundle = build_bundle_files(
        context=context,
        client_artifacts=client_artifacts,
        health=current_health,
        upstreams=upstream_records,
        source_count=len(upstream_records),
        supplemental_files={
            "state/release.json": canonical_json_bytes(state),
            "dist/reports/latest.json": canonical_json_bytes(report),
            "dist/reports/latest.md": render_latest_markdown(report),
        },
    )
    publish_root = build_publish_artifact(
        destination / "publish",
        context=context,
        bundle_files=bundle,
        deletions=deletions,
        mandatory_removal_ids=mandatory_ids,
    )
    validate_publish_artifact(publish_root, root / "schemas")

    return destination
