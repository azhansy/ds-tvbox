"""Strict domain models. They contain no network, Git, or clock access."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any


class StringEnum(StrEnum):
    pass


class SourceKind(StringEnum):
    VOD_SITE = "vod_site"
    VOD_CONFIG = "vod_config"
    LIVE_PLAYLIST = "live_playlist"
    REPOSITORY_CATALOG = "repository_catalog"


class ParserKind(StringEnum):
    MACCMS_JSON = "maccms_json"
    MACCMS_XML = "maccms_xml"
    TVBOX_JSON = "tvbox_json"
    TVBOX_JSON5 = "tvbox_json5"
    M3U = "m3u"
    TXT_LIVE = "txt_live"
    REPOSITORY_CATALOG = "repository_catalog"


class FetchMode(StringEnum):
    DIRECT_URL = "direct_url"
    GITHUB_TRACKED_FILE = "github_tracked_file"
    GITHUB_REPOSITORY = "github_repository"


class RightsStatus(StringEnum):
    VERIFIED = "verified"
    OPEN_LICENSE = "open_license"
    PUBLIC_UNVERIFIED = "public_unverified"
    RESTRICTED = "restricted"
    TAKEDOWN = "takedown"
    UNKNOWN = "unknown"


class TechnicalStatus(StringEnum):
    UNKNOWN = "unknown"
    HEALTHY = "healthy"
    PARTIAL = "partial"
    SUSPECT = "suspect"
    DEAD = "dead"
    UNSUPPORTED_ENVIRONMENT = "unsupported_environment"


class PublicationStatus(StringEnum):
    STABLE = "stable"
    EXPERIMENTAL = "experimental"
    WITHHELD = "withheld"
    REJECTED = "rejected"


class ReleaseKind(StringEnum):
    BOOTSTRAP = "bootstrap"
    REGULAR = "regular"
    SAFETY = "safety"
    ROLLBACK = "rollback"


class FailureReason(StringEnum):
    FETCH_TIMEOUT = "fetch_timeout"
    DNS_FAILURE = "dns_failure"
    TLS_FAILURE = "tls_failure"
    HTTP_404 = "http_404"
    HTTP_410 = "http_410"
    RATE_LIMITED = "rate_limited"
    UPSTREAM_5XX = "upstream_5xx"
    INVALID_JSON = "invalid_json"
    INVALID_XML = "invalid_xml"
    SCHEMA_INCOMPATIBLE = "schema_incompatible"
    HOME_CONTRACT_FAILED = "home_contract_failed"
    SEARCH_CONTRACT_FAILED = "search_contract_failed"
    DETAIL_CONTRACT_FAILED = "detail_contract_failed"
    PLAY_CONTRACT_FAILED = "play_contract_failed"
    MEDIA_PROBE_FAILED = "media_probe_failed"
    CREDENTIAL_REQUIRED = "credential_required"
    CREDENTIAL_QUERY_REJECTED = "credential_query_rejected"
    CREDENTIAL_HEADER_REJECTED = "credential_header_rejected"
    INVALID_HEADER_SYNTAX = "invalid_header_syntax"
    PRIVATE_ADDRESS_REJECTED = "private_address_rejected"
    DANGEROUS_SCHEME_REJECTED = "dangerous_scheme_rejected"
    RESPONSE_TOO_LARGE = "response_too_large"
    CLIENT_HTTP_DISALLOWED = "client_http_disallowed"
    CLIENT_HEADER_UNSUPPORTED = "client_header_unsupported"
    CLIENT_EXTENSION_UNSUPPORTED = "client_extension_unsupported"
    UNSUPPORTED_SPIDER = "unsupported_spider"
    UNSUPPORTED_ENVIRONMENT = "unsupported_environment"
    BLOCKED_BY_SOURCE = "blocked_by_source"
    MISSING_UPSTREAM = "missing_upstream"
    TERMS_CHANGED = "terms_changed"
    RIGHTS_RESTRICTED = "rights_restricted"
    TAKEDOWN = "takedown"
    CATALOG_DEPTH_EXCEEDED = "catalog_depth_exceeded"
    CATALOG_LIMIT_EXCEEDED = "catalog_limit_exceeded"


@dataclass(frozen=True)
class FetchSpec:
    mode: FetchMode
    reviewed_url: str | None
    repository_url: str | None
    track_ref: str | None
    config_path: str | None
    reviewed_revision: str | None


@dataclass(frozen=True)
class TermsWatchSpec:
    type: str
    url: str | None
    path: str | None
    reviewed_sha256: str


@dataclass(frozen=True)
class ClientSiteSpec:
    key: str
    name: str
    searchable: int
    quick_search: int
    filterable: int
    changeable: int


@dataclass(frozen=True)
class HttpExceptionSpec:
    host: str
    port: int
    path_prefix: str
    reason: str
    reviewed_at: str


@dataclass(frozen=True)
class SourceSpec:
    id: str
    kind: SourceKind
    parser: ParserKind
    enabled: bool
    fetch: FetchSpec
    terms_watch: tuple[TermsWatchSpec, ...]
    rights_status: RightsStatus
    config_license_status: str
    content_rights_status: str
    allowed_hosts: frozenset[str]
    allow_discovered_media_hosts: bool
    http_exceptions: tuple[HttpExceptionSpec, ...]
    denied_categories: tuple[str, ...]
    client_site: ClientSiteSpec | None
    catalog: Mapping[str, Any] | None
    raw: Mapping[str, Any] = field(repr=False, compare=False)


@dataclass(frozen=True)
class DeclaredHeaders:
    values: Mapping[str, str]


@dataclass(frozen=True)
class VodSiteCandidate:
    source_id: str
    key: str
    name: str
    type: int
    api: str
    searchable: int
    quick_search: int
    filterable: int
    changeable: int
    categories: tuple[str, ...]
    rights_status: RightsStatus
    declared_headers: DeclaredHeaders | None = None
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)


@dataclass(frozen=True)
class VodCapabilities:
    home: bool = False
    search: bool = False
    detail: bool = False
    play: bool = False
    media_probe: bool = False


@dataclass(frozen=True)
class MediaProbeResult:
    ok: bool
    final_url: str | None
    response_ms: int | None
    media_path_score: int
    width: int | None = None
    height: int | None = None
    bandwidth: int | None = None
    failure_reason: FailureReason | None = None


@dataclass(frozen=True)
class VodProbeResult:
    candidate: VodSiteCandidate
    technical_status: TechnicalStatus
    publication_status: PublicationStatus
    capabilities: VodCapabilities
    failure_reason: FailureReason | None
    secondary_reasons: tuple[FailureReason, ...] = ()
    sample_title: str | None = None
    sample_vod_id: str | None = None
    sample_media_url: str | None = None


@dataclass(frozen=True)
class LiveCandidate:
    source_id: str
    name: str
    original_url: str
    normalized_url: str
    rights_status: RightsStatus
    tvg_id: str | None = None
    group: str | None = None
    logo: str | None = None
    epg: str | None = None
    declared_headers: DeclaredHeaders | None = None


@dataclass(frozen=True)
class LiveProbeResult:
    candidate: LiveCandidate
    technical_status: TechnicalStatus
    publication_status: PublicationStatus
    media: MediaProbeResult
    consecutive_successes: int
    consecutive_failures: int
    last_success_at: str | None
    failure_reason: FailureReason | None
    secondary_reasons: tuple[FailureReason, ...] = ()
    response_ms_history: tuple[int, ...] = ()


@dataclass(frozen=True)
class SelectedChannel:
    channel_id: str
    identity_basis: str
    normalized_identity: str
    selected: LiveProbeResult
    candidates: tuple[LiveProbeResult, ...]


@dataclass(frozen=True)
class GateDecision:
    publish: bool
    inconclusive: bool
    release_kind: ReleaseKind
    reasons: tuple[str, ...]
    mandatory_removal_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class RunContext:
    owner: str
    repository: str
    generated_ref: str
    workflow_run_id: str
    workflow_run_attempt: int
    generated_at: str
    generation: int
    release_kind: ReleaseKind
    previous_head: str | None
    previous_last_success_at: str | None

    @property
    def release_id(self) -> str:
        return f"g{self.generation:08d}"

    @property
    def candidate_ref(self) -> str:
        return (
            f"candidate/run-{self.workflow_run_id}"
            f"-attempt-{self.workflow_run_attempt}"
        )


@dataclass(frozen=True)
class ReleaseState:
    schema_version: str
    status: str
    release_kind: ReleaseKind
    generation: int
    active_release_id: str
    last_publish_at: str | None
    last_success_at: str | None
    content_commit_sha: str | None
    previous_release_head_sha: str | None
    workflow_run_id: str
    workflow_run_attempt: int


@dataclass(frozen=True)
class BuildResult:
    root: Path
    release_id: str
    root_manifest_path: Path
    release_manifest_path: Path
    state_path: Path
    report_path: Path
