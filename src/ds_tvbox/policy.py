"""Publication, failure-priority, and availability-gate policy.

This module is deliberately pure: callers provide observed facts and receive a
deterministic decision.  Network and Git side effects belong to other trust
zones.
"""

from __future__ import annotations

from collections.abc import Iterable, Set
from typing import Literal

from ds_tvbox.models import (
    FailureReason,
    GateDecision,
    PublicationStatus,
    ReleaseKind,
    RightsStatus,
    TechnicalStatus,
)

EntityKind = Literal["vod", "live", "spider"]


_FAILURE_ORDER: tuple[FailureReason, ...] = (
    FailureReason.TAKEDOWN,
    FailureReason.RIGHTS_RESTRICTED,
    FailureReason.TERMS_CHANGED,
    FailureReason.CREDENTIAL_REQUIRED,
    FailureReason.CREDENTIAL_QUERY_REJECTED,
    FailureReason.CREDENTIAL_HEADER_REJECTED,
    FailureReason.PRIVATE_ADDRESS_REJECTED,
    FailureReason.DANGEROUS_SCHEME_REJECTED,
    FailureReason.INVALID_HEADER_SYNTAX,
    FailureReason.CLIENT_HTTP_DISALLOWED,
    FailureReason.CLIENT_HEADER_UNSUPPORTED,
    FailureReason.CLIENT_EXTENSION_UNSUPPORTED,
    FailureReason.UNSUPPORTED_SPIDER,
    FailureReason.HTTP_404,
    FailureReason.HTTP_410,
    FailureReason.DNS_FAILURE,
    FailureReason.TLS_FAILURE,
    FailureReason.RESPONSE_TOO_LARGE,
    FailureReason.INVALID_JSON,
    FailureReason.INVALID_XML,
    FailureReason.SCHEMA_INCOMPATIBLE,
    FailureReason.HOME_CONTRACT_FAILED,
    FailureReason.SEARCH_CONTRACT_FAILED,
    FailureReason.DETAIL_CONTRACT_FAILED,
    FailureReason.PLAY_CONTRACT_FAILED,
    FailureReason.MEDIA_PROBE_FAILED,
    FailureReason.RATE_LIMITED,
    FailureReason.UPSTREAM_5XX,
    FailureReason.FETCH_TIMEOUT,
    FailureReason.UNSUPPORTED_ENVIRONMENT,
    FailureReason.BLOCKED_BY_SOURCE,
    FailureReason.MISSING_UPSTREAM,
    FailureReason.CATALOG_DEPTH_EXCEEDED,
    FailureReason.CATALOG_LIMIT_EXCEEDED,
)
_FAILURE_RANK = {reason: rank for rank, reason in enumerate(_FAILURE_ORDER)}

_RETAIN_TECHNICAL_FACT = frozenset(
    {
        FailureReason.TAKEDOWN,
        FailureReason.RIGHTS_RESTRICTED,
        FailureReason.TERMS_CHANGED,
        FailureReason.CREDENTIAL_REQUIRED,
        FailureReason.CREDENTIAL_QUERY_REJECTED,
        FailureReason.CREDENTIAL_HEADER_REJECTED,
        FailureReason.PRIVATE_ADDRESS_REJECTED,
        FailureReason.DANGEROUS_SCHEME_REJECTED,
        FailureReason.INVALID_HEADER_SYNTAX,
    }
)
_CLIENT_LIMITATIONS = frozenset(
    {
        FailureReason.CLIENT_HTTP_DISALLOWED,
        FailureReason.CLIENT_HEADER_UNSUPPORTED,
        FailureReason.CLIENT_EXTENSION_UNSUPPORTED,
        FailureReason.UNSUPPORTED_SPIDER,
        FailureReason.MEDIA_PROBE_FAILED,
        FailureReason.CATALOG_DEPTH_EXCEEDED,
    }
)
_HARD_FAILURES = frozenset(
    {
        FailureReason.HTTP_404,
        FailureReason.HTTP_410,
        FailureReason.DNS_FAILURE,
        FailureReason.TLS_FAILURE,
        FailureReason.RESPONSE_TOO_LARGE,
        FailureReason.INVALID_JSON,
        FailureReason.INVALID_XML,
        FailureReason.SCHEMA_INCOMPATIBLE,
        FailureReason.HOME_CONTRACT_FAILED,
        FailureReason.SEARCH_CONTRACT_FAILED,
        FailureReason.DETAIL_CONTRACT_FAILED,
        FailureReason.PLAY_CONTRACT_FAILED,
        FailureReason.MISSING_UPSTREAM,
    }
)
_TEMPORARY_FAILURES = frozenset(
    {
        FailureReason.RATE_LIMITED,
        FailureReason.UPSTREAM_5XX,
        FailureReason.FETCH_TIMEOUT,
    }
)
_REJECT_REASONS = frozenset(
    {
        FailureReason.TAKEDOWN,
        FailureReason.RIGHTS_RESTRICTED,
        FailureReason.CREDENTIAL_REQUIRED,
        FailureReason.CREDENTIAL_QUERY_REJECTED,
        FailureReason.CREDENTIAL_HEADER_REJECTED,
        FailureReason.PRIVATE_ADDRESS_REJECTED,
        FailureReason.DANGEROUS_SCHEME_REJECTED,
        FailureReason.INVALID_HEADER_SYNTAX,
        FailureReason.CLIENT_HTTP_DISALLOWED,
        FailureReason.UNSUPPORTED_SPIDER,
    }
)


def prioritize_failure_reasons(
    reasons: Iterable[FailureReason | None],
) -> tuple[FailureReason | None, tuple[FailureReason, ...]]:
    """Return the canonical primary reason and stable, de-duplicated remainder."""

    unique = {reason for reason in reasons if reason is not None}
    if not unique:
        return None, ()
    ordered = sorted(unique, key=lambda reason: (_FAILURE_RANK.get(reason, 10_000), reason.value))
    return ordered[0], tuple(ordered[1:])


def technical_status_for_failure(
    previous_status: TechnicalStatus | None,
    reason: FailureReason,
    *,
    source_status: TechnicalStatus | None = None,
) -> TechnicalStatus:
    """Map one canonical failure to its technical state without conflating rights."""

    if reason in _RETAIN_TECHNICAL_FACT:
        return previous_status or TechnicalStatus.UNKNOWN
    if reason in _CLIENT_LIMITATIONS:
        return TechnicalStatus.PARTIAL
    if reason in _HARD_FAILURES:
        return TechnicalStatus.DEAD
    if reason in _TEMPORARY_FAILURES:
        return TechnicalStatus.SUSPECT
    if reason is FailureReason.UNSUPPORTED_ENVIRONMENT:
        return TechnicalStatus.UNSUPPORTED_ENVIRONMENT
    if reason is FailureReason.BLOCKED_BY_SOURCE:
        if source_status is TechnicalStatus.DEAD:
            return TechnicalStatus.DEAD
        return TechnicalStatus.SUSPECT
    if reason is FailureReason.CATALOG_LIMIT_EXCEEDED:
        return previous_status or TechnicalStatus.UNKNOWN
    return previous_status or TechnicalStatus.UNKNOWN


def publication_status_for(
    rights_status: RightsStatus,
    technical_status: TechnicalStatus,
    *,
    entity_kind: EntityKind,
    site_type: int | None = None,
    media_verified: bool = True,
    failure_reasons: Iterable[FailureReason | None] = (),
) -> PublicationStatus:
    """Apply the single rights/technical publication matrix from SPEC 9.2."""

    primary, _ = prioritize_failure_reasons(failure_reasons)
    if rights_status in {RightsStatus.RESTRICTED, RightsStatus.TAKEDOWN}:
        return PublicationStatus.REJECTED
    if entity_kind == "spider" or site_type == 3:
        return PublicationStatus.REJECTED
    if primary in _REJECT_REASONS:
        return PublicationStatus.REJECTED
    if primary in {
        FailureReason.TERMS_CHANGED,
        FailureReason.CLIENT_HEADER_UNSUPPORTED,
        FailureReason.CLIENT_EXTENSION_UNSUPPORTED,
    }:
        return PublicationStatus.WITHHELD
    if rights_status is RightsStatus.UNKNOWN:
        return PublicationStatus.WITHHELD
    if technical_status is TechnicalStatus.HEALTHY:
        if entity_kind == "live" and not media_verified:
            return PublicationStatus.WITHHELD
        if entity_kind == "vod" and site_type == 4:
            return PublicationStatus.EXPERIMENTAL
        return PublicationStatus.STABLE
    if technical_status is TechnicalStatus.PARTIAL:
        if entity_kind == "live":
            return PublicationStatus.WITHHELD
        if primary in {
            FailureReason.CLIENT_HEADER_UNSUPPORTED,
            FailureReason.CLIENT_EXTENSION_UNSUPPORTED,
        }:
            return PublicationStatus.WITHHELD
        return PublicationStatus.EXPERIMENTAL
    return PublicationStatus.WITHHELD


def warning_name(name: str, rights_status: RightsStatus) -> str:
    """Add exactly one public-unverified warning prefix."""

    clean = name.strip()
    if rights_status is not RightsStatus.PUBLIC_UNVERIFIED:
        return clean
    without_prefix = clean.removeprefix("⚠️ ").removeprefix("⚠️").lstrip()
    return f"⚠️ {without_prefix}"


def safety_retained_ids(
    previous_entity_ids: Iterable[str],
    mandatory_removal_ids: Iterable[str],
    *,
    dependent_ids: Iterable[str] = (),
) -> tuple[str, ...]:
    """Derive a safety release only by deleting forbidden previous entities."""

    previous = set(previous_entity_ids)
    removed = set(mandatory_removal_ids).union(dependent_ids)
    return tuple(sorted(previous.difference(removed)))


def _new_failures(previous: Set[str], current_ok: Set[str]) -> int:
    return sum(item not in current_ok for item in previous)


def evaluate_gates(
    *,
    release_kind: ReleaseKind,
    previous_vod_ids: Set[str],
    current_publishable_vod_ids: Set[str],
    previous_live_url_ids: Set[str],
    current_healthy_live_url_ids: Set[str],
    current_vod_sites: int,
    current_live_channels: int,
    minimum_vod_sites: int = 1,
    minimum_live_channels: int = 1,
    minimum_previous_items: int = 5,
    max_new_failure_ratio: float = 0.20,
    failed_network_groups: int = 0,
    failed_groups_to_abort: int = 2,
    state_available: bool = True,
    previous_release_known: bool = True,
    mandatory_removal_ids: Iterable[str] = (),
) -> GateDecision:
    """Evaluate availability gates, with mandatory safety removal taking precedence."""

    mandatory = tuple(sorted(set(mandatory_removal_ids)))
    if mandatory:
        safety_reasons = ["mandatory_removal"]
        if not state_available:
            safety_reasons.append("safety_state_unavailable")
        if not previous_release_known:
            safety_reasons.append("safety_baseline_unavailable")
        if len(safety_reasons) > 1:
            return GateDecision(
                publish=False,
                inconclusive=True,
                release_kind=ReleaseKind.SAFETY,
                reasons=tuple(safety_reasons),
                mandatory_removal_ids=mandatory,
            )
        if current_vod_sites < minimum_vod_sites or current_live_channels < minimum_live_channels:
            safety_reasons.append("safety_degraded")
        return GateDecision(
            publish=True,
            inconclusive=False,
            release_kind=ReleaseKind.SAFETY,
            reasons=tuple(safety_reasons),
            mandatory_removal_ids=mandatory,
        )

    reasons: list[str] = []
    if release_kind is not ReleaseKind.BOOTSTRAP:
        if not state_available:
            reasons.append("state_unavailable")
        if not previous_release_known:
            reasons.append("previous_release_unknown")

    if len(previous_vod_ids) >= minimum_previous_items:
        ratio = _new_failures(previous_vod_ids, current_publishable_vod_ids) / len(
            previous_vod_ids
        )
        if ratio > max_new_failure_ratio:
            reasons.append("vod_failure_ratio")
    if len(previous_live_url_ids) >= minimum_previous_items:
        ratio = _new_failures(previous_live_url_ids, current_healthy_live_url_ids) / len(
            previous_live_url_ids
        )
        if ratio > max_new_failure_ratio:
            reasons.append("live_failure_ratio")

    if current_vod_sites == 0:
        reasons.append("vod_zero")
    if current_live_channels == 0:
        reasons.append("live_zero")
    if current_vod_sites < minimum_vod_sites:
        reasons.append("minimum_vod_sites")
    if current_live_channels < minimum_live_channels:
        reasons.append("minimum_live_channels")
    if failed_network_groups >= failed_groups_to_abort:
        reasons.append("network_outage")

    stable_reasons = tuple(dict.fromkeys(reasons))
    return GateDecision(
        publish=not stable_reasons,
        inconclusive=bool(stable_reasons),
        release_kind=release_kind,
        reasons=stable_reasons,
        mandatory_removal_ids=(),
    )
