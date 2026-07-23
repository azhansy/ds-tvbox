"""Stable entity identities, history inheritance, and four-layer health output."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Mapping, Sequence, Set
from typing import Any
from urllib.parse import urlsplit

from ds_tvbox.errors import ContractError
from ds_tvbox.live import (
    MAX_SUCCESSFUL_RESPONSE_SAMPLES,
    channel_identity,
    deduplicate_final_urls,
    live_url_id,
)
from ds_tvbox.models import (
    FailureReason,
    LiveProbeResult,
    PublicationStatus,
    RightsStatus,
    SelectedChannel,
    SourceSpec,
    TechnicalStatus,
    VodProbeResult,
    VodSiteCandidate,
)
from ds_tvbox.policy import publication_status_for


def vod_entity_id(candidate: VodSiteCandidate) -> str:
    fingerprint = hashlib.sha256(f"{candidate.type}{candidate.api}".encode()).hexdigest()[:16]
    return f"vod:{candidate.source_id}:{fingerprint}"


def aggregate_technical_status(statuses: Sequence[TechnicalStatus]) -> TechnicalStatus:
    """Apply the exact source/channel first-match aggregation order."""

    if not statuses:
        return TechnicalStatus.UNKNOWN
    for status in (
        TechnicalStatus.HEALTHY,
        TechnicalStatus.PARTIAL,
        TechnicalStatus.SUSPECT,
        TechnicalStatus.UNKNOWN,
        TechnicalStatus.UNSUPPORTED_ENVIRONMENT,
        TechnicalStatus.DEAD,
    ):
        if status in statuses:
            return status
    raise ContractError("unknown technical status")


def aggregate_publication_status(
    statuses: Sequence[PublicationStatus],
) -> PublicationStatus:
    if not statuses:
        return PublicationStatus.WITHHELD
    for status in (
        PublicationStatus.STABLE,
        PublicationStatus.EXPERIMENTAL,
        PublicationStatus.WITHHELD,
        PublicationStatus.REJECTED,
    ):
        if status in statuses:
            return status
    raise ContractError("unknown publication status")


def next_history(
    technical_status: TechnicalStatus,
    previous: Mapping[str, Any] | None,
    checked_at: str,
) -> tuple[int, int, str | None]:
    previous = previous or {}
    old_successes = int(previous.get("consecutive_successes", 0))
    old_failures = int(previous.get("consecutive_failures", 0))
    old_last_success = previous.get("last_success_at")
    if technical_status is TechnicalStatus.HEALTHY:
        return old_successes + 1, 0, checked_at
    return 0, old_failures + 1, str(old_last_success) if old_last_success else None


def _previous_indexes(
    previous_health: Mapping[str, Any] | None,
) -> tuple[dict[str, Mapping[str, Any]], dict[str, str], dict[str, Mapping[str, Any]]]:
    items: dict[str, Mapping[str, Any]] = {}
    item_sources: dict[str, str] = {}
    channels: dict[str, Mapping[str, Any]] = {}
    if not previous_health:
        return items, item_sources, channels
    for source in previous_health.get("sources", []):
        if not isinstance(source, Mapping):
            continue
        source_id = str(source.get("source_id", ""))
        for item in source.get("items", []):
            if isinstance(item, Mapping) and item.get("entity_id"):
                entity_id = str(item["entity_id"])
                items[entity_id] = item
                item_sources[entity_id] = source_id
    for channel in previous_health.get("channels", []):
        if isinstance(channel, Mapping) and channel.get("entity_id"):
            channels[str(channel["entity_id"])] = channel
    return items, item_sources, channels


def _reason_value(reason: FailureReason | None) -> str | None:
    return reason.value if reason is not None else None


def _vod_item(
    result: VodProbeResult,
    previous: Mapping[str, Any] | None,
    checked_at: str,
) -> dict[str, Any]:
    successes, failures, last_success = next_history(
        result.technical_status, previous, checked_at
    )
    item: dict[str, Any] = {
        "entity_type": "vod_site",
        "entity_id": vod_entity_id(result.candidate),
        "technical_status": result.technical_status.value,
        "publication_status": result.publication_status.value,
        "last_success_at": last_success,
        "consecutive_successes": successes,
        "consecutive_failures": failures,
        "capabilities": {
            "home": result.capabilities.home,
            "search": result.capabilities.search,
            "detail": result.capabilities.detail,
            "play": result.capabilities.play,
            "media_probe": result.capabilities.media_probe,
        },
        "failure_reason": _reason_value(result.failure_reason),
        "secondary_reasons": [reason.value for reason in result.secondary_reasons],
    }
    return item


def _live_item(result: LiveProbeResult, channel_id: str) -> dict[str, Any]:
    final_url = result.media.final_url
    response_history = [
        value
        for value in result.response_ms_history[-MAX_SUCCESSFUL_RESPONSE_SAMPLES:]
        if not isinstance(value, bool) and value >= 0
    ]
    if (
        not response_history
        and result.technical_status is TechnicalStatus.HEALTHY
        and result.media.ok
        and result.media.response_ms is not None
        and result.media.response_ms >= 0
    ):
        response_history.append(result.media.response_ms)
    return {
        "entity_type": "live_url",
        "entity_id": live_url_id(result.candidate),
        "channel_id": channel_id,
        "technical_status": result.technical_status.value,
        "publication_status": result.publication_status.value,
        "last_success_at": result.last_success_at,
        "consecutive_successes": result.consecutive_successes,
        "consecutive_failures": result.consecutive_failures,
        "media_path_score": result.media.media_path_score,
        "response_ms": result.media.response_ms,
        "response_ms_history": response_history,
        "protocol": urlsplit(final_url).scheme.lower() if final_url else None,
        "final_url": final_url,
        "normalized_url": result.candidate.normalized_url,
        "name": result.candidate.name,
        "tvg_id": result.candidate.tvg_id,
        "group": result.candidate.group,
        "logo": result.candidate.logo,
        "epg": result.candidate.epg,
        "width": result.media.width,
        "height": result.media.height,
        "bandwidth": result.media.bandwidth,
        "failure_reason": _reason_value(result.failure_reason),
        "secondary_reasons": [reason.value for reason in result.secondary_reasons],
    }


def _missing_item(
    previous: Mapping[str, Any],
    *,
    source_status: TechnicalStatus | None,
    source_rights: RightsStatus,
) -> dict[str, Any]:
    result = dict(previous)
    if source_status is None:
        technical = TechnicalStatus.DEAD
        reason = FailureReason.MISSING_UPSTREAM
    else:
        technical = (
            TechnicalStatus.DEAD
            if source_status is TechnicalStatus.DEAD
            else TechnicalStatus.SUSPECT
        )
        reason = FailureReason.BLOCKED_BY_SOURCE
    _, failures, last_success = next_history(technical, previous, "")
    result["technical_status"] = technical.value
    result["publication_status"] = publication_status_for(
        source_rights,
        technical,
        entity_kind="live" if result.get("entity_type") == "live_url" else "vod",
        failure_reasons=(reason,),
    ).value
    result["last_success_at"] = last_success
    result["consecutive_successes"] = 0
    result["consecutive_failures"] = failures
    result["failure_reason"] = reason.value
    result["secondary_reasons"] = []
    if result.get("entity_type") == "live_url":
        result["media_path_score"] = 0
        result["response_ms"] = None
    return result


def _rights_for_channel(results: Sequence[LiveProbeResult]) -> RightsStatus:
    order = (
        RightsStatus.TAKEDOWN,
        RightsStatus.RESTRICTED,
        RightsStatus.UNKNOWN,
        RightsStatus.PUBLIC_UNVERIFIED,
        RightsStatus.OPEN_LICENSE,
        RightsStatus.VERIFIED,
    )
    present = {result.candidate.rights_status for result in results}
    return next(status for status in order if status in present)


def _channel_object(
    channel_id: str,
    results: Sequence[LiveProbeResult],
    selected: SelectedChannel | None,
) -> dict[str, Any]:
    if not results:
        raise ContractError("channel requires candidate URLs")
    _, basis, normalized = channel_identity(results[0].candidate)
    technical = aggregate_technical_status([result.technical_status for result in results])
    publication = (
        PublicationStatus.STABLE
        if selected is not None
        else aggregate_publication_status([result.publication_status for result in results])
    )
    rights = selected.selected.candidate.rights_status if selected else _rights_for_channel(results)
    return {
        "entity_id": channel_id,
        "identity_basis": basis,
        "normalized_identity": normalized,
        "technical_status": technical.value,
        "publication_status": publication.value,
        "rights_status": rights.value,
        "selected_url_id": live_url_id(selected.selected.candidate) if selected else None,
        "candidate_url_ids": sorted(live_url_id(result.candidate) for result in results),
    }


def build_health_document(
    *,
    generated_at: str,
    generation: int,
    release_id: str,
    sources: Sequence[SourceSpec],
    vod_results: Sequence[VodProbeResult],
    live_results: Sequence[LiveProbeResult],
    selected_channels: Sequence[SelectedChannel],
    upstream_revisions: Mapping[str, str | None] | None = None,
    previous_health: Mapping[str, Any] | None = None,
    source_failures: Mapping[
        str, tuple[TechnicalStatus, FailureReason | None]
    ] | None = None,
    enumerated_source_ids: Set[str] | None = None,
    schema_version: str = "1.0.0",
) -> dict[str, Any]:
    """Create a deterministic source→item plus channel→URL health graph."""

    upstream_revisions = upstream_revisions or {}
    live_results = deduplicate_final_urls(live_results)
    source_failures = source_failures or {}
    source_by_id = {source.id: source for source in sources}
    if len(source_by_id) != len(sources):
        raise ContractError("duplicate source id")
    enumerated = set(
        source_by_id if enumerated_source_ids is None else enumerated_source_ids
    ).difference(source_failures)
    previous_items, previous_item_sources, previous_channels = _previous_indexes(previous_health)

    items_by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
    current_item_ids: set[str] = set()
    live_by_channel: dict[str, list[LiveProbeResult]] = defaultdict(list)

    for vod_result in vod_results:
        if vod_result.candidate.source_id not in source_by_id:
            raise ContractError(f"unknown VOD source: {vod_result.candidate.source_id}")
        entity_id = vod_entity_id(vod_result.candidate)
        if entity_id in current_item_ids:
            raise ContractError(f"duplicate health entity: {entity_id}")
        current_item_ids.add(entity_id)
        items_by_source[vod_result.candidate.source_id].append(
            _vod_item(vod_result, previous_items.get(entity_id), generated_at)
        )

    for live_result in live_results:
        if live_result.candidate.source_id not in source_by_id:
            raise ContractError(f"unknown live source: {live_result.candidate.source_id}")
        entity_id = live_url_id(live_result.candidate)
        if entity_id in current_item_ids:
            raise ContractError(f"duplicate health entity: {entity_id}")
        current_item_ids.add(entity_id)
        channel_id, _, _ = channel_identity(live_result.candidate)
        live_by_channel[channel_id].append(live_result)
        items_by_source[live_result.candidate.source_id].append(
            _live_item(live_result, channel_id)
        )

    for entity_id, previous in previous_items.items():
        if entity_id in current_item_ids:
            continue
        source_id = previous_item_sources[entity_id]
        source = source_by_id.get(source_id)
        if source is None:
            continue
        source_status = source_failures.get(source_id, (None, None))[0]
        if source_id in enumerated:
            source_status = None
        missing = _missing_item(
            previous,
            source_status=source_status,
            source_rights=source.rights_status,
        )
        items_by_source[source_id].append(missing)
        current_item_ids.add(entity_id)

    source_objects: list[dict[str, Any]] = []
    for source_id in sorted(source_by_id):
        source = source_by_id[source_id]
        items = sorted(items_by_source[source_id], key=lambda item: str(item["entity_id"]))
        failure = source_failures.get(source_id)
        if failure is not None:
            technical, failure_reason = failure
            publication = publication_status_for(
                source.rights_status,
                technical,
                entity_kind="live" if source.kind.value == "live_playlist" else "vod",
                failure_reasons=(failure_reason,),
            )
        else:
            technical = aggregate_technical_status(
                [TechnicalStatus(str(item["technical_status"])) for item in items]
            )
            if source.rights_status in {RightsStatus.RESTRICTED, RightsStatus.TAKEDOWN}:
                publication = PublicationStatus.REJECTED
            else:
                publication = aggregate_publication_status(
                    [PublicationStatus(str(item["publication_status"])) for item in items]
                )
            failure_reason = None
        source_objects.append(
            {
                "entity_id": f"source:{source_id}",
                "source_id": source_id,
                "technical_status": technical.value,
                "publication_status": publication.value,
                "rights_status": source.rights_status.value,
                "last_checked_at": generated_at,
                "upstream_revision": upstream_revisions.get(source_id),
                "failure_reason": _reason_value(failure_reason),
                "items": items,
            }
        )

    selected_by_id = {channel.channel_id: channel for channel in selected_channels}
    if len(selected_by_id) != len(selected_channels):
        raise ContractError("duplicate selected channel id")
    channel_objects = [
        _channel_object(channel_id, results, selected_by_id.get(channel_id))
        for channel_id, results in sorted(live_by_channel.items())
    ]

    current_channel_ids = {str(channel["entity_id"]) for channel in channel_objects}
    live_items = {
        str(item["entity_id"]): item
        for items in items_by_source.values()
        for item in items
        if item.get("entity_type") == "live_url"
    }
    for channel_id, previous in sorted(previous_channels.items()):
        if channel_id in current_channel_ids:
            continue
        candidate_ids = [
            str(value)
            for value in previous.get("candidate_url_ids", [])
            if str(value) in live_items
        ]
        if not candidate_ids:
            continue
        statuses = [
            TechnicalStatus(str(live_items[entity_id]["technical_status"]))
            for entity_id in candidate_ids
        ]
        restored = dict(previous)
        restored["technical_status"] = aggregate_technical_status(statuses).value
        restored["publication_status"] = PublicationStatus.WITHHELD.value
        restored["selected_url_id"] = None
        restored["candidate_url_ids"] = sorted(candidate_ids)
        channel_objects.append(restored)

    channel_objects.sort(key=lambda item: str(item["entity_id"]))
    all_entity_ids = [
        str(item["entity_id"])
        for source in source_objects
        for item in source["items"]
    ]
    if len(all_entity_ids) != len(set(all_entity_ids)):
        raise ContractError("health item entity IDs are not globally unique")

    return {
        "schema_version": schema_version,
        "generated_at": generated_at,
        "generation": generation,
        "release_id": release_id,
        "sources": source_objects,
        "channels": channel_objects,
    }
