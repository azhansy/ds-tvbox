from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace

import pytest

from ds_tvbox.errors import ContractError
from ds_tvbox.health import (
    aggregate_technical_status,
    build_health_document,
    next_history,
    vod_entity_id,
)
from ds_tvbox.live import select_channels
from ds_tvbox.models import (
    FailureReason,
    FetchMode,
    FetchSpec,
    LiveCandidate,
    LiveProbeResult,
    MediaProbeResult,
    ParserKind,
    PublicationStatus,
    RightsStatus,
    SourceKind,
    SourceSpec,
    TechnicalStatus,
    VodCapabilities,
    VodProbeResult,
    VodSiteCandidate,
)

NOW = "2026-07-22T12:00:00Z"


def source(
    source_id: str,
    kind: SourceKind,
    rights: RightsStatus = RightsStatus.PUBLIC_UNVERIFIED,
) -> SourceSpec:
    parser = ParserKind.MACCMS_JSON if kind is SourceKind.VOD_SITE else ParserKind.M3U
    return SourceSpec(
        id=source_id,
        kind=kind,
        parser=parser,
        enabled=True,
        fetch=FetchSpec(FetchMode.DIRECT_URL, "https://example.test", None, None, None, None),
        terms_watch=(),
        rights_status=rights,
        config_license_status="unknown",
        content_rights_status="unverified",
        allowed_hosts=frozenset({"example.test"}),
        allow_discovered_media_hosts=True,
        http_exceptions=(),
        denied_categories=(),
        client_site=None,
        catalog=None,
        raw={},
    )


def vod_candidate(
    name: str = "影片", api: str = "https://vod.example.test/api"
) -> VodSiteCandidate:
    return VodSiteCandidate(
        source_id="vod-source",
        key="key",
        name=name,
        type=1,
        api=api,
        searchable=1,
        quick_search=1,
        filterable=1,
        changeable=1,
        categories=(),
        rights_status=RightsStatus.PUBLIC_UNVERIFIED,
    )


def vod_result(
    *,
    status: TechnicalStatus = TechnicalStatus.HEALTHY,
    publication: PublicationStatus = PublicationStatus.STABLE,
    name: str = "影片",
    api: str = "https://vod.example.test/api",
) -> VodProbeResult:
    return VodProbeResult(
        candidate=vod_candidate(name, api),
        technical_status=status,
        publication_status=publication,
        capabilities=VodCapabilities(True, True, True, True, status is TechnicalStatus.HEALTHY),
        failure_reason=None if status is TechnicalStatus.HEALTHY else FailureReason.FETCH_TIMEOUT,
    )


def live_result(
    *,
    status: TechnicalStatus = TechnicalStatus.HEALTHY,
    publication: PublicationStatus = PublicationStatus.STABLE,
    response_history: tuple[int, ...] = (),
) -> LiveProbeResult:
    candidate = LiveCandidate(
        source_id="live-source",
        name="央视",
        original_url="https://live.example.test/index.m3u8",
        normalized_url="https://live.example.test/index.m3u8",
        rights_status=RightsStatus.PUBLIC_UNVERIFIED,
        tvg_id="cctv.cn",
    )
    return LiveProbeResult(
        candidate=candidate,
        technical_status=status,
        publication_status=publication,
        media=MediaProbeResult(
            ok=status is TechnicalStatus.HEALTHY,
            final_url=candidate.original_url if status is TechnicalStatus.HEALTHY else None,
            response_ms=100,
            media_path_score=1 if status is TechnicalStatus.HEALTHY else 0,
            failure_reason=(
                None if status is TechnicalStatus.HEALTHY else FailureReason.FETCH_TIMEOUT
            ),
        ),
        consecutive_successes=2 if status is TechnicalStatus.HEALTHY else 0,
        consecutive_failures=0 if status is TechnicalStatus.HEALTHY else 1,
        last_success_at=NOW if status is TechnicalStatus.HEALTHY else None,
        failure_reason=None if status is TechnicalStatus.HEALTHY else FailureReason.FETCH_TIMEOUT,
        response_ms_history=response_history,
    )


def base_sources() -> tuple[SourceSpec, SourceSpec]:
    return (
        source("vod-source", SourceKind.VOD_SITE),
        source("live-source", SourceKind.LIVE_PLAYLIST),
    )


def build(
    vod: tuple[VodProbeResult, ...] = (),
    live: tuple[LiveProbeResult, ...] = (),
    *,
    previous: Mapping[str, object] | None = None,
    source_failures: Mapping[str, tuple[TechnicalStatus, FailureReason | None]] | None = None,
    enumerated: frozenset[str] | None = None,
) -> dict[str, object]:
    return build_health_document(
        generated_at=NOW,
        generation=1,
        release_id="g00000001",
        sources=base_sources(),
        vod_results=vod,
        live_results=live,
        selected_channels=select_channels(live),
        upstream_revisions={"vod-source": "sha256:abc", "live-source": "deadbeef"},
        previous_health=previous,
        source_failures=source_failures,
        enumerated_source_ids=enumerated,
    )


def test_vod_identity_ignores_display_name_and_key() -> None:
    first = vod_candidate("旧名称")
    second = VodSiteCandidate(
        **{
            **first.__dict__,
            "key": "new-key",
            "name": "新名称",
        }
    )
    assert vod_entity_id(first) == vod_entity_id(second)
    assert vod_entity_id(first) != vod_entity_id(vod_candidate(api="https://vod.example.test/v2"))


def test_technical_aggregation_uses_spec_order() -> None:
    assert aggregate_technical_status([]) is TechnicalStatus.UNKNOWN
    assert (
        aggregate_technical_status([TechnicalStatus.DEAD, TechnicalStatus.SUSPECT])
        is TechnicalStatus.SUSPECT
    )
    assert (
        aggregate_technical_status([TechnicalStatus.PARTIAL, TechnicalStatus.HEALTHY])
        is TechnicalStatus.HEALTHY
    )


def test_history_success_and_failure_transitions() -> None:
    previous = {
        "consecutive_successes": 3,
        "consecutive_failures": 2,
        "last_success_at": "old",
    }
    assert next_history(TechnicalStatus.HEALTHY, previous, NOW) == (4, 0, NOW)
    assert next_history(TechnicalStatus.SUSPECT, previous, NOW) == (0, 3, "old")


def test_health_document_contains_four_layer_graph_and_sorted_entities() -> None:
    vod = vod_result()
    live = live_result()
    document = build((vod,), (live,))
    assert document["schema_version"] == "1.0.0"
    assert document["release_id"] == "g00000001"
    sources = document["sources"]
    assert isinstance(sources, list)
    assert [item["source_id"] for item in sources] == ["live-source", "vod-source"]
    assert sources[0]["items"][0]["entity_type"] == "live_url"
    assert sources[0]["items"][0]["response_ms"] == 100
    assert sources[0]["items"][0]["response_ms_history"] == [100]
    assert sources[1]["items"][0]["entity_type"] == "vod_site"
    assert sources[1]["items"][0]["capabilities"]["search"] is True
    channels = document["channels"]
    assert isinstance(channels, list) and len(channels) == 1
    assert channels[0]["selected_url_id"] == sources[0]["items"][0]["entity_id"]
    assert channels[0]["candidate_url_ids"] == [channels[0]["selected_url_id"]]


def test_health_marks_duplicate_final_url_channel_withheld_before_counting() -> None:
    winner = live_result()
    duplicate_candidate = replace(
        winner.candidate,
        name="重复频道",
        original_url="https://live.example.test/duplicate.m3u8",
        normalized_url="https://live.example.test/duplicate.m3u8",
        tvg_id="duplicate.cn",
    )
    duplicate = replace(
        winner,
        candidate=duplicate_candidate,
        media=replace(winner.media, final_url=winner.media.final_url),
        consecutive_successes=1,
    )

    document = build((), (winner, duplicate))
    source_items = document["sources"][0]["items"]
    channels = document["channels"]

    assert sorted(item["publication_status"] for item in source_items) == [
        "stable",
        "withheld",
    ]
    assert sorted(channel["publication_status"] for channel in channels) == [
        "stable",
        "withheld",
    ]
    assert sum(channel["selected_url_id"] is not None for channel in channels) == 1


def test_failed_live_latency_is_not_recorded_as_a_success_sample() -> None:
    document = build((), (live_result(status=TechnicalStatus.SUSPECT),))
    item = document["sources"][0]["items"][0]

    assert item["response_ms"] == 100
    assert item["response_ms_history"] == []


def test_vod_history_is_inherited_only_for_same_stable_id() -> None:
    first = build((vod_result(),), ())
    second = build((vod_result(name="改名"),), (), previous=first)
    second_item = second["sources"][1]["items"][0]
    assert second_item["consecutive_successes"] == 2

    changed = build(
        (vod_result(api="https://vod.example.test/new"),),
        (),
        previous=first,
    )
    current = [
        item
        for item in changed["sources"][1]["items"]
        if item["failure_reason"] is None
    ][0]
    old = [
        item
        for item in changed["sources"][1]["items"]
        if item["failure_reason"] == "missing_upstream"
    ][0]
    assert current["consecutive_successes"] == 1
    assert old["technical_status"] == "dead"


def test_missing_child_uses_blocked_by_source_for_source_failure() -> None:
    previous = build((vod_result(),), ())
    current = build(
        (),
        (),
        previous=previous,
        source_failures={
            "vod-source": (TechnicalStatus.SUSPECT, FailureReason.FETCH_TIMEOUT)
        },
        enumerated=frozenset({"live-source"}),
    )
    vod_source = current["sources"][1]
    item = vod_source["items"][0]
    assert vod_source["technical_status"] == "suspect"
    assert item["technical_status"] == "suspect"
    assert item["failure_reason"] == "blocked_by_source"


def test_missing_live_url_retains_global_channel_as_withheld() -> None:
    previous = build((), (live_result(),))
    current = build((), (), previous=previous)
    live_source = current["sources"][0]
    assert live_source["items"][0]["failure_reason"] == "missing_upstream"
    assert current["channels"][0]["publication_status"] == "withheld"
    assert current["channels"][0]["selected_url_id"] is None


def test_duplicate_entity_id_is_rejected() -> None:
    duplicate = vod_result()
    with pytest.raises(ContractError, match="duplicate health entity"):
        build((duplicate, duplicate), ())


def test_unknown_source_result_is_rejected() -> None:
    unknown = vod_result()
    unknown_candidate = VodSiteCandidate(**{**unknown.candidate.__dict__, "source_id": "missing"})
    unknown_result = VodProbeResult(**{**unknown.__dict__, "candidate": unknown_candidate})
    with pytest.raises(ContractError, match="unknown VOD source"):
        build((unknown_result,), ())


def test_source_rights_restriction_overrides_child_publication() -> None:
    document = build_health_document(
        generated_at=NOW,
        generation=1,
        release_id="g00000001",
        sources=(source("vod-source", SourceKind.VOD_SITE, RightsStatus.RESTRICTED),),
        vod_results=(vod_result(),),
        live_results=(),
        selected_channels=(),
    )
    assert document["sources"][0]["publication_status"] == "rejected"
