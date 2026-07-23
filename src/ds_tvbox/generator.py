"""Deterministic TVBox client artifact generation.

This module is deliberately pure: it does not read the network, the clock, or Git.
It consumes already-normalized probe results and emits canonical UTF-8 bytes whose
repository paths are locked to exactly one immutable release.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from types import MappingProxyType
from urllib.parse import parse_qsl, urlsplit

from ds_tvbox.errors import ContractError
from ds_tvbox.live import normalized_media_final_url
from ds_tvbox.models import (
    PublicationStatus,
    RightsStatus,
    RunContext,
    SelectedChannel,
    SourceKind,
    SourceSpec,
    TechnicalStatus,
    VodProbeResult,
)
from ds_tvbox.serialization import canonical_json_bytes

_PUBLISHABLE_RIGHTS = frozenset(
    {RightsStatus.VERIFIED, RightsStatus.OPEN_LICENSE, RightsStatus.PUBLIC_UNVERIFIED}
)
_PUBLICATION_RANK = {
    PublicationStatus.STABLE: 0,
    PublicationStatus.EXPERIMENTAL: 1,
}
_RIGHTS_RANK = {
    RightsStatus.VERIFIED: 0,
    RightsStatus.OPEN_LICENSE: 1,
    RightsStatus.PUBLIC_UNVERIFIED: 2,
}
_SOURCE_ID = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_EXECUTABLE_SUFFIX = re.compile(r"(?:\.jar|\.js|\.py|\.dex|\.so)(?:$|[?#])", re.I)
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
class GeneratedClientArtifacts:
    """Canonical client files before health and manifests are attached.

    Keys in both mappings are repository-relative POSIX paths. ``release_files``
    contains immutable release-local files; ``alias_files`` contains byte-identical
    root conveniences. The mappings are read-only to prevent a caller from changing
    bytes after hashes have been calculated.
    """

    release_id: str
    release_files: Mapping[str, bytes]
    alias_files: Mapping[str, bytes]
    independent_source_ids: tuple[str, ...]
    vod_site_count: int
    live_channel_count: int

    @property
    def files(self) -> Mapping[str, bytes]:
        """Return all generated client files as one read-only mapping."""

        return MappingProxyType({**self.release_files, **self.alias_files})


@dataclass(frozen=True)
class _SiteEntry:
    source_id: str
    result: VodProbeResult
    key: str
    name: str


@dataclass(frozen=True)
class ClientSiteAssignment:
    """Minimal client identity facts consumed by deterministic key/name assignment."""

    source_id: str
    key: str
    name: str
    site_type: int
    api: str


@dataclass(frozen=True)
class _SourceLine:
    source_id: str
    publication_rank: int
    rights: RightsStatus
    name: str
    config_path: str
    has_stable: bool


def _byte_key(value: str) -> bytes:
    return value.encode("utf-8")


def _require_text(value: str, label: str) -> str:
    stripped = value.strip()
    if not stripped or any(character in stripped for character in ("\r", "\n", "\x00")):
        raise ContractError(f"{label} must be non-empty single-line text")
    return stripped


def _warning_name(value: str, *, warned: bool) -> str:
    """Apply the public-unverified warning exactly once."""

    name = _require_text(value, "display name")
    while name.startswith("⚠️"):
        name = name.removeprefix("⚠️").lstrip()
    if not name:
        raise ContractError("display name cannot consist only of warning markers")
    return f"⚠️ {name}" if warned else name


def _require_https(value: str, label: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ContractError(f"{label} must be an absolute credential-free HTTPS URL")
    if _EXECUTABLE_SUFFIX.search(value):
        raise ContractError(f"{label} contains an executable dependency URL")
    query_keys = {key.casefold() for key, _ in parse_qsl(parsed.query, keep_blank_values=True)}
    if query_keys.intersection(_CREDENTIAL_QUERY_KEYS):
        raise ContractError(f"{label} contains a credential query parameter")
    if "\r" in value or "\n" in value or "\x00" in value:
        raise ContractError(f"{label} contains control characters")
    return value


def _contains_executable_dependency(value: object) -> bool:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if str(key).casefold() in {"jar", "spider"}:
                return True
            if _contains_executable_dependency(child):
                return True
        return False
    if isinstance(value, (list, tuple)):
        return any(_contains_executable_dependency(item) for item in value)
    return isinstance(value, str) and bool(_EXECUTABLE_SUFFIX.search(value))


def raw_release_base(context: RunContext) -> str:
    """Return the immutable Raw base URL for ``context.release_id``."""

    for value, label in (
        (context.owner, "owner"),
        (context.repository, "repository"),
        (context.generated_ref, "generated ref"),
    ):
        if not value or any(character in value for character in "/?#\\\r\n"):
            raise ContractError(f"invalid GitHub {label}: {value!r}")
    return (
        f"https://raw.githubusercontent.com/{context.owner}/{context.repository}/"
        f"{context.generated_ref}/dist/releases/{context.release_id}"
    )


def _publishable_vod(result: VodProbeResult) -> bool:
    candidate = result.candidate
    if candidate.rights_status not in _PUBLISHABLE_RIGHTS:
        return False
    if result.publication_status not in _PUBLICATION_RANK:
        return False
    if candidate.type == 3 or candidate.type not in (0, 1, 4):
        return False
    if _contains_executable_dependency(candidate.raw):
        return False
    if result.publication_status is PublicationStatus.STABLE:
        return result.technical_status is TechnicalStatus.HEALTHY and candidate.type in (0, 1)
    if result.technical_status not in (TechnicalStatus.HEALTHY, TechnicalStatus.PARTIAL):
        return False
    # Type 4 is only client-safe when it needs no ext or other executable adapter.
    return not (candidate.type == 4 and candidate.raw.get("ext") not in (None, "", [], {}))


def _assignment_identity(entry: ClientSiteAssignment) -> str:
    material = f"{entry.source_id}\0{entry.site_type}\0{entry.api}".encode()
    return hashlib.sha256(material).hexdigest()[:8]


def _rewritten_assignment_key(entry: ClientSiteAssignment) -> str:
    original = entry.key.strip()
    safe_original = re.sub(r"[^A-Za-z0-9_.-]+", "_", original).strip("_.-")
    if not safe_original:
        safe_original = _assignment_identity(entry)
    return f"src_{entry.source_id}_{safe_original}"


def assign_client_site_fields(
    entries: Sequence[ClientSiteAssignment],
) -> tuple[ClientSiteAssignment, ...]:
    """Assign globally unique client keys/names from recoverable base facts."""

    scoped_identities = [
        (entry.source_id, entry.site_type, entry.api) for entry in entries
    ]
    if len(scoped_identities) != len(set(scoped_identities)):
        raise ContractError("duplicate source-scoped VOD assignment identity")
    key_counts: dict[str, int] = {}
    for entry in entries:
        original = _require_text(entry.key, "site key")
        key_counts[original] = key_counts.get(original, 0) + 1

    keyed: list[ClientSiteAssignment] = []
    used: set[str] = set()
    for entry in sorted(
        entries,
        key=lambda item: (
            _byte_key(item.source_id),
            _byte_key(item.key),
            _byte_key(item.api),
        ),
    ):
        original = entry.key.strip()
        rewritten = _rewritten_assignment_key(entry)
        desired = original if key_counts[original] == 1 else rewritten
        if desired in used:
            desired = f"{rewritten}_{_assignment_identity(entry)}"
        suffix = 2
        base = desired
        while desired in used:
            desired = f"{base}_{suffix}"
            suffix += 1
        used.add(desired)
        keyed.append(
            ClientSiteAssignment(
                source_id=entry.source_id,
                key=desired,
                name=entry.name,
                site_type=entry.site_type,
                api=entry.api,
            )
        )

    counts: dict[str, int] = {}
    for entry in keyed:
        counts[entry.name] = counts.get(entry.name, 0) + 1

    used_names: set[str] = set()
    output: list[ClientSiteAssignment] = []
    for entry in keyed:
        desired = entry.name
        if counts[desired] > 1:
            desired = f"{desired} [{entry.source_id}]"
        if desired in used_names:
            desired = f"{desired} [{entry.key}]"
        counter = 2
        base = desired
        while desired in used_names:
            desired = f"{base} #{counter}"
            counter += 1
        used_names.add(desired)
        output.append(
            ClientSiteAssignment(
                source_id=entry.source_id,
                key=entry.key,
                name=desired,
                site_type=entry.site_type,
                api=entry.api,
            )
        )
    return tuple(output)


def _assign_site_entries(entries: Sequence[_SiteEntry]) -> list[_SiteEntry]:
    by_identity = {
        (entry.source_id, entry.result.candidate.type, entry.result.candidate.api): entry
        for entry in entries
    }
    assigned = assign_client_site_fields(
        tuple(
            ClientSiteAssignment(
                source_id=entry.source_id,
                key=entry.key,
                name=entry.name,
                site_type=entry.result.candidate.type,
                api=entry.result.candidate.api,
            )
            for entry in entries
        )
    )
    return [
        _SiteEntry(
            source_id=item.source_id,
            result=by_identity[(item.source_id, item.site_type, item.api)].result,
            key=item.key,
            name=item.name,
        )
        for item in assigned
    ]


def _prepare_sites(
    results: Sequence[VodProbeResult], sources: Mapping[str, SourceSpec]
) -> list[_SiteEntry]:
    entries: list[_SiteEntry] = []
    identities: set[tuple[int, str]] = set()
    eligible = [result for result in results if _publishable_vod(result)]
    eligible.sort(
        key=lambda result: (
            _PUBLICATION_RANK[result.publication_status],
            _RIGHTS_RANK[result.candidate.rights_status],
            _byte_key(result.candidate.source_id),
            result.candidate.type,
            _byte_key(result.candidate.api),
        )
    )
    for result in eligible:
        candidate = result.candidate
        source = sources.get(candidate.source_id)
        if source is None or not source.enabled:
            continue
        if source.rights_status is not candidate.rights_status:
            raise ContractError(f"rights mismatch for source {candidate.source_id}")
        if source.kind is SourceKind.VOD_SITE:
            if source.client_site is None or source.fetch.reviewed_url is None:
                raise ContractError(f"vod_site {source.id} is missing reviewed client facts")
            client = source.client_site
            candidate = replace(
                candidate,
                key=client.key,
                name=client.name,
                searchable=client.searchable,
                quick_search=client.quick_search,
                filterable=client.filterable,
                changeable=client.changeable,
            )
            result = replace(result, candidate=candidate)
        identity = (candidate.type, candidate.api)
        if identity in identities:
            continue
        identities.add(identity)
        _require_https(candidate.api, f"site API for {candidate.source_id}")
        name = _warning_name(
            candidate.name,
            warned=candidate.rights_status is RightsStatus.PUBLIC_UNVERIFIED,
        )
        entries.append(
            _SiteEntry(
                source_id=candidate.source_id,
                result=result,
                key=candidate.key.strip(),
                name=name,
            )
        )
    return entries


def deduplicate_vod_results(
    results: Iterable[VodProbeResult], sources: Iterable[SourceSpec]
) -> tuple[VodProbeResult, ...]:
    """Apply the client-visible ``(type, api)`` winner to all downstream facts.

    A source can repeat the same stable entity under multiple display keys, so
    those aliases are first collapsed to one source-local result.  Publishable
    results are then ranked by the same deterministic order used by the client
    generator; cross-source losers remain auditable in health as ``withheld``.
    """

    source_map: dict[str, SourceSpec] = {}
    for source in sources:
        if not _SOURCE_ID.fullmatch(source.id) or source.id in source_map:
            raise ContractError(f"invalid or duplicate source id: {source.id!r}")
        source_map[source.id] = source

    materialized = tuple(results)
    publication_rank = {
        PublicationStatus.STABLE: 0,
        PublicationStatus.EXPERIMENTAL: 1,
        PublicationStatus.WITHHELD: 2,
        PublicationStatus.REJECTED: 3,
    }
    technical_rank = {
        TechnicalStatus.HEALTHY: 0,
        TechnicalStatus.PARTIAL: 1,
        TechnicalStatus.SUSPECT: 2,
        TechnicalStatus.UNKNOWN: 3,
        TechnicalStatus.UNSUPPORTED_ENVIRONMENT: 4,
        TechnicalStatus.DEAD: 5,
    }

    def eligible(index: int) -> bool:
        result = materialized[index]
        source = source_map.get(result.candidate.source_id)
        if source is None or not source.enabled or not _publishable_vod(result):
            return False
        if source.rights_status is not result.candidate.rights_status:
            raise ContractError(f"rights mismatch for source {result.candidate.source_id}")
        return True

    def rank(index: int) -> tuple[int, int, int, int, bytes, int, bytes, bytes, bytes, int]:
        result = materialized[index]
        candidate = result.candidate
        is_eligible = eligible(index)
        return (
            0 if is_eligible else 1,
            publication_rank[result.publication_status],
            technical_rank[result.technical_status],
            _RIGHTS_RANK.get(candidate.rights_status, len(_RIGHTS_RANK)),
            _byte_key(candidate.source_id),
            candidate.type,
            _byte_key(candidate.api),
            _byte_key(candidate.key),
            _byte_key(candidate.name),
            index,
        )

    # One health entity is defined by source + type + normalized API.  Pick its
    # best deterministic observation before applying the global publication key.
    representative_by_entity: dict[tuple[str, int, str], int] = {}
    for index in sorted(range(len(materialized)), key=rank):
        candidate = materialized[index].candidate
        representative_by_entity.setdefault(
            (candidate.source_id, candidate.type, candidate.api), index
        )
    representatives = set(representative_by_entity.values())

    losers: set[int] = set()
    identities: set[tuple[int, str]] = set()
    for index in sorted((item for item in representatives if eligible(item)), key=rank):
        candidate = materialized[index].candidate
        identity = (candidate.type, candidate.api)
        if identity in identities:
            losers.add(index)
        else:
            identities.add(identity)

    output: list[VodProbeResult] = []
    for index, result in enumerate(materialized):
        if index not in representatives:
            continue
        if index in losers:
            output.append(replace(result, publication_status=PublicationStatus.WITHHELD))
        else:
            output.append(result)
    return tuple(output)


def _site_document(entry: _SiteEntry, source: SourceSpec) -> dict[str, object]:
    candidate = entry.result.candidate
    document: dict[str, object] = {
        "key": entry.key,
        "name": entry.name,
        "type": candidate.type,
        "api": candidate.api,
        "searchable": candidate.searchable,
        "quickSearch": candidate.quick_search,
        "filterable": candidate.filterable,
        "changeable": candidate.changeable,
    }
    denied = {item.strip() for item in source.denied_categories}
    categories = sorted(
        {
            _require_text(category, "category")
            for category in candidate.categories
            if category.strip() not in denied
        },
        key=_byte_key,
    )
    if categories:
        document["categories"] = categories
    return document


def _line_base_name(source: SourceSpec) -> str:
    if source.kind is SourceKind.VOD_SITE:
        if source.client_site is None:
            raise ContractError(f"vod_site {source.id} is missing client_site")
        return source.client_site.name
    if source.kind in (SourceKind.VOD_CONFIG, SourceKind.REPOSITORY_CATALOG):
        return f"DS {source.id}"
    raise ContractError(f"source {source.id} cannot produce a VOD config")


def _assign_unique_line_names(lines: Sequence[_SourceLine]) -> list[_SourceLine]:
    counts: dict[str, int] = {}
    for line in lines:
        counts[line.name] = counts.get(line.name, 0) + 1
    used = {"DS 稳定聚合"}
    output: list[_SourceLine] = []
    for line in lines:
        desired = line.name
        if counts[desired] > 1 or desired in used:
            desired = f"{desired} [{line.source_id}]"
        counter = 2
        base = desired
        while desired in used:
            desired = f"{base} #{counter}"
            counter += 1
        used.add(desired)
        output.append(
            _SourceLine(
                source_id=line.source_id,
                publication_rank=line.publication_rank,
                rights=line.rights,
                name=desired,
                config_path=line.config_path,
                has_stable=line.has_stable,
            )
        )
    return output


def _escape_m3u_attribute(value: str) -> str:
    return _require_text(value, "M3U attribute").replace("&", "&amp;").replace('"', "&quot;")


def render_m3u(channels: Iterable[SelectedChannel]) -> tuple[bytes, int]:
    """Render the selected, publishable channels as a deterministic M3U file."""

    eligible: list[tuple[SelectedChannel, str]] = []
    seen_channel_ids: set[str] = set()
    for channel in channels:
        result = channel.selected
        candidate = result.candidate
        if not channel.channel_id or channel.channel_id in seen_channel_ids:
            raise ContractError(f"invalid or duplicate channel id: {channel.channel_id!r}")
        seen_channel_ids.add(channel.channel_id)
        if result not in channel.candidates:
            raise ContractError(
                f"selected URL is absent from channel candidates: {channel.channel_id}"
            )
        if (
            result.technical_status is not TechnicalStatus.HEALTHY
            or result.publication_status is not PublicationStatus.STABLE
            or not result.media.ok
            or result.media.media_path_score not in (1, 2)
            or candidate.rights_status not in _PUBLISHABLE_RIGHTS
        ):
            continue
        _require_text(candidate.name, "channel name")
        _require_https(candidate.normalized_url, "channel source URL")
        final_url = _require_https(
            normalized_media_final_url(result.media), "channel final URL"
        )
        eligible.append((channel, final_url))

    eligible.sort(
        key=lambda item: (
            _byte_key(item[0].normalized_identity),
            _byte_key(item[0].selected.candidate.source_id),
            _byte_key(item[0].selected.candidate.name),
            _byte_key(item[1]),
        )
    )
    lines = ["#EXTM3U"]
    seen_urls: set[str] = set()
    published = 0
    for channel, final_url in eligible:
        candidate = channel.selected.candidate
        if final_url in seen_urls:
            continue
        seen_urls.add(final_url)
        name = _warning_name(
            candidate.name,
            warned=candidate.rights_status is RightsStatus.PUBLIC_UNVERIFIED,
        )
        attributes: list[tuple[str, str]] = []
        if candidate.tvg_id and candidate.tvg_id.strip():
            attributes.append(("tvg-id", candidate.tvg_id.strip()))
        if candidate.logo:
            try:
                logo = _require_https(candidate.logo, "channel logo")
            except ContractError:
                logo = ""
            if logo:
                attributes.append(("tvg-logo", logo))
        if candidate.epg:
            try:
                epg = _require_https(candidate.epg, "channel EPG")
            except ContractError:
                epg = ""
            if epg:
                attributes.append(("tvg-url", epg))
        if candidate.group and candidate.group.strip():
            attributes.append(("group-title", candidate.group.strip()))
        suffix = "".join(f' {key}="{_escape_m3u_attribute(value)}"' for key, value in attributes)
        lines.append(f"#EXTINF:-1{suffix},{name}")
        lines.append(final_url)
        published += 1
    return ("\n".join(lines) + "\n").encode("utf-8"), published


def build_client_artifacts(
    *,
    context: RunContext,
    sources: Iterable[SourceSpec],
    vod_results: Iterable[VodProbeResult],
    channels: Iterable[SelectedChannel],
) -> GeneratedClientArtifacts:
    """Build all index/depot/config/M3U files for one immutable release."""

    source_map: dict[str, SourceSpec] = {}
    for source in sources:
        if not _SOURCE_ID.fullmatch(source.id) or source.id in source_map:
            raise ContractError(f"invalid or duplicate source id: {source.id!r}")
        source_map[source.id] = source

    raw_base = raw_release_base(context)
    entries = _prepare_sites(tuple(vod_results), source_map)
    # Assign client-visible identity fields once across the full release.  The
    # stable aggregate is then an exact subset of the independently shipped
    # source configs, which lets the privileged publisher reconstruct and
    # compare it without trusting collector-controlled display metadata.
    entries = _assign_site_entries(entries)
    entries_by_source: dict[str, list[_SiteEntry]] = {}
    for entry in entries:
        entries_by_source.setdefault(entry.source_id, []).append(entry)

    m3u_bytes, live_count = render_m3u(channels)
    live_document: list[dict[str, object]] = []
    if live_count:
        live_document = [{"name": "DS 稳定直播", "type": 0, "url": f"{raw_base}/live/stable.m3u"}]

    release_prefix = f"dist/releases/{context.release_id}"
    release_files: dict[str, bytes] = {
        f"{release_prefix}/live/stable.m3u": m3u_bytes,
    }

    lines: list[_SourceLine] = []
    independent_documents: dict[str, dict[str, object]] = {}
    for source_id in sorted(entries_by_source, key=_byte_key):
        source = source_map[source_id]
        source_entries = list(entries_by_source[source_id])
        source_entries.sort(key=lambda item: _byte_key(item.key))
        independent_documents[source_id] = {
            "sites": [_site_document(entry, source) for entry in source_entries],
            "lives": live_document,
            "parses": [],
        }
        publication_rank = min(
            _PUBLICATION_RANK[entry.result.publication_status] for entry in source_entries
        )
        rights = source.rights_status
        lines.append(
            _SourceLine(
                source_id=source_id,
                publication_rank=publication_rank,
                rights=rights,
                name=_warning_name(
                    _line_base_name(source),
                    warned=rights is RightsStatus.PUBLIC_UNVERIFIED,
                ),
                config_path=f"configs/{source_id}.json",
                has_stable=any(
                    entry.result.publication_status is PublicationStatus.STABLE
                    for entry in source_entries
                ),
            )
        )
    lines = _assign_unique_line_names(lines)

    for source_id, document in independent_documents.items():
        release_files[f"{release_prefix}/configs/{source_id}.json"] = canonical_json_bytes(document)

    stable_entries = [
        entry for entry in entries if entry.result.publication_status is PublicationStatus.STABLE
    ]
    stable_entries.sort(key=lambda item: (_byte_key(item.source_id), _byte_key(item.key)))
    stable_document = {
        "sites": [_site_document(entry, source_map[entry.source_id]) for entry in stable_entries],
        "lives": live_document,
        "parses": [],
    }
    stable_bytes = canonical_json_bytes(stable_document)
    release_files[f"{release_prefix}/configs/stable.json"] = stable_bytes

    index_lines = sorted(
        lines,
        key=lambda line: (
            line.publication_rank,
            _RIGHTS_RANK[line.rights],
            _byte_key(line.source_id),
        ),
    )
    index = {
        "urls": [
            {"name": "DS 稳定聚合", "url": f"{raw_base}/configs/stable.json"},
            *[{"name": line.name, "url": f"{raw_base}/{line.config_path}"} for line in index_lines],
        ]
    }
    stable_depot_lines = sorted(
        (line for line in lines if line.has_stable),
        key=lambda line: (_RIGHTS_RANK[line.rights], _byte_key(line.source_id)),
    )
    stable_depot = {
        "urls": [
            {"name": "DS 稳定聚合", "url": f"{raw_base}/configs/stable.json"},
            *[
                {"name": line.name, "url": f"{raw_base}/{line.config_path}"}
                for line in stable_depot_lines
            ],
        ]
    }
    risk_lines = sorted(
        (line for line in lines if line.rights is RightsStatus.PUBLIC_UNVERIFIED),
        key=lambda line: (line.publication_rank, _byte_key(line.source_id)),
    )
    risk_depot = {
        "urls": [
            {"name": line.name, "url": f"{raw_base}/{line.config_path}"} for line in risk_lines
        ]
    }
    warehouse = {
        "storeHouse": [
            {"sourceName": "DS 稳定仓", "sourceUrl": f"{raw_base}/depots/stable.json"},
            {
                "sourceName": "DS 公共实验仓",
                "sourceUrl": f"{raw_base}/depots/public-unverified.json",
            },
        ]
    }

    index_bytes = canonical_json_bytes(index)
    warehouse_bytes = canonical_json_bytes(warehouse)
    release_files.update(
        {
            f"{release_prefix}/index.json": index_bytes,
            f"{release_prefix}/warehouse.json": warehouse_bytes,
            f"{release_prefix}/depots/stable.json": canonical_json_bytes(stable_depot),
            f"{release_prefix}/depots/public-unverified.json": canonical_json_bytes(risk_depot),
        }
    )
    alias_files = {
        "dist/index.json": index_bytes,
        "dist/warehouse.json": warehouse_bytes,
        "dist/configs/stable.json": stable_bytes,
        "dist/live/stable.m3u": m3u_bytes,
    }
    return GeneratedClientArtifacts(
        release_id=context.release_id,
        release_files=MappingProxyType(
            dict(sorted(release_files.items(), key=lambda item: _byte_key(item[0])))
        ),
        alias_files=MappingProxyType(
            dict(sorted(alias_files.items(), key=lambda item: _byte_key(item[0])))
        ),
        independent_source_ids=tuple(line.source_id for line in index_lines),
        vod_site_count=len(stable_entries),
        live_channel_count=live_count,
    )


# A descriptive alias for callers that use "generate" terminology.
generate_client_artifacts = build_client_artifacts
