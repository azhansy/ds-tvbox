from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from ds_tvbox.collector import CollectResult, DiscardedEntity, SourceObservation
from ds_tvbox.errors import ContractError
from ds_tvbox.live import channel_identity, live_url_id
from ds_tvbox.models import (
    FailureReason,
    LiveCandidate,
    LiveProbeResult,
    MediaProbeResult,
    PublicationStatus,
    ReleaseKind,
    RightsStatus,
    RunContext,
    SelectedChannel,
    TechnicalStatus,
)
from ds_tvbox.pipeline import (
    DenylistMatchers,
    _mandatory_removals,
    _previous_baselines,
    _previous_source_specs,
    _release_source_url_matches,
    _safety_health_document,
    _source_report,
    _upstream_records,
)
from ds_tvbox.registry import load_registry

PROJECT = Path(__file__).resolve().parents[2]


def test_previous_live_baseline_excludes_healthy_deduplication_loser() -> None:
    health = {
        "channels": [
            {
                "publication_status": "stable",
                "candidate_url_ids": ["live:winner", "live:duplicate"],
            }
        ],
        "sources": [
            {
                "source_id": "live-source",
                "items": [
                    {
                        "entity_id": "live:winner",
                        "entity_type": "live_url",
                        "technical_status": "healthy",
                        "publication_status": "stable",
                    },
                    {
                        "entity_id": "live:duplicate",
                        "entity_type": "live_url",
                        "technical_status": "healthy",
                        "publication_status": "withheld",
                    },
                ],
            }
        ],
    }

    _vod, live, by_source = _previous_baselines(health)

    assert live == {"live:winner"}
    assert by_source == {"live-source": {"live:winner", "live:duplicate"}}


def _collection(
    *,
    discarded: tuple[DiscardedEntity, ...] = (),
    source_failures: dict[str, tuple[TechnicalStatus, FailureReason | None]] | None = None,
) -> CollectResult:
    sources = tuple(load_registry(PROJECT / "sources/registry.yaml"))
    return CollectResult(
        checked_at="2026-07-22T12:00:00Z",
        sources=sources,
        vod_results=(),
        live_results=(),
        selected_channels=(),
        source_observations=(),
        catalog_results=(),
        discarded_entities=discarded,
        upstream_revisions={},
        source_failures=source_failures or {},
        enumerated_source_ids=frozenset(),
        network_probes=(),
        failed_network_groups=0,
    )


def test_new_discarded_security_candidate_does_not_remove_its_whole_source() -> None:
    source_id = "iptv-org-cn-cctv"
    result = _collection(
        discarded=(
            DiscardedEntity(
                source_id=source_id,
                entity_kind="live_url",
                target_hash="new-token-url-hash",
                failure_reason=FailureReason.CREDENTIAL_QUERY_REJECTED,
            ),
        )
    )

    mandatory, sources, historical = _mandatory_removals(
        result,
        {source_id: {"live:old-good"}},
        {source_id: {"old-good-hash": "live:old-good"}},
        frozenset(),
    )

    assert mandatory == ()
    assert sources == frozenset()
    assert historical == ()


def test_discarded_security_candidate_matching_previous_target_is_entity_only() -> None:
    source_id = "iptv-org-cn-cctv"
    result = _collection(
        discarded=(
            DiscardedEntity(
                source_id=source_id,
                entity_kind="live_url",
                target_hash="old-bad-hash",
                failure_reason=FailureReason.PRIVATE_ADDRESS_REJECTED,
            ),
        )
    )

    mandatory, sources, historical = _mandatory_removals(
        result,
        {source_id: {"live:old-bad", "live:old-good"}},
        {source_id: {"old-bad-hash": "live:old-bad"}},
        frozenset(),
    )

    assert mandatory == ("live:old-bad",)
    assert sources == frozenset()
    assert historical == ("live:old-bad",)


def test_terms_change_removes_current_source_without_auto_deleting_history() -> None:
    source_id = "ikun-vod"
    result = _collection(
        source_failures={
            source_id: (TechnicalStatus.PARTIAL, FailureReason.TERMS_CHANGED)
        }
    )

    mandatory, sources, historical = _mandatory_removals(
        result,
        {source_id: {"vod:ikun-vod:old"}},
        {},
        frozenset(),
    )

    assert mandatory == (f"source:{source_id}", "vod:ikun-vod:old")
    assert sources == frozenset({source_id})
    assert historical == ()


def test_client_http_violation_is_mandatory_only_while_source_is_active() -> None:
    source_id = "ikun-vod"
    result = _collection(
        source_failures={
            source_id: (TechnicalStatus.PARTIAL, FailureReason.CLIENT_HTTP_DISALLOWED)
        }
    )

    mandatory, sources, historical = _mandatory_removals(
        result,
        {source_id: {"vod:ikun-vod:old"}},
        {},
        frozenset(),
    )
    assert mandatory == (f"source:{source_id}", "vod:ikun-vod:old")
    assert sources == frozenset({source_id})
    assert historical == (f"source:{source_id}",)

    recovered, recovered_sources, recovered_history = _mandatory_removals(
        result,
        {},
        {},
        frozenset({source_id}),
    )
    assert recovered == ()
    assert recovered_sources == frozenset()
    assert recovered_history == ()


def test_safety_source_report_keeps_removed_observation_out_of_current_snapshot() -> None:
    original = _collection()
    denied = replace(original.sources[0], rights_status=RightsStatus.TAKEDOWN)
    new_source = original.sources[1]
    result = replace(
        original,
        sources=(denied, new_source),
        source_observations=(
            SourceObservation(
                source_id=denied.id,
                fetch_mode=denied.fetch.mode.value,
                resolved_revision=None,
                resolved_fetch_url=None,
                content_sha256=None,
                terms_sha256={},
                technical_status=TechnicalStatus.HEALTHY,
                failure_reason=FailureReason.TAKEDOWN,
                secondary_reasons=(),
                enumerated=False,
            ),
        ),
        source_failures={
            denied.id: (TechnicalStatus.HEALTHY, FailureReason.TAKEDOWN)
        },
    )
    previous_health = {
        "sources": [
            {
                "source_id": denied.id,
                "technical_status": "healthy",
                "publication_status": "stable",
                "rights_status": "public_unverified",
            }
        ]
    }

    reports = {
        item["source_id"]: item
        for item in _source_report(result, {"sources": []}, previous_health)
    }

    assert reports[denied.id]["publication_status"] == "rejected"
    assert reports[denied.id]["rights_status"] == "takedown"
    assert reports[denied.id]["change_summary"]["category"] == "removed"
    assert reports[denied.id]["change_summary"]["current"] is None
    assert reports[new_source.id]["publication_status"] == "withheld"
    assert reports[new_source.id]["change_summary"]["category"] == "new"
    assert reports[new_source.id]["change_summary"]["current"] is None


def test_active_release_url_match_finds_source_removed_from_registry(tmp_path: Path) -> None:
    release = tmp_path / "dist/releases/g00000001"
    (release / "configs").mkdir(parents=True)
    (release / "manifest.json").write_text(
        json.dumps(
            {
                "upstreams": [
                    {
                        "source_id": "removed-vod",
                        "resolved_fetch_url": "https://vod.example.test/config.json",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (release / "configs/removed-vod.json").write_text(
        json.dumps({"sites": [{"api": "https://vod.example.test/api"}]}),
        encoding="utf-8",
    )
    previous_health = {
        "sources": [
            {
                "source_id": "removed-vod",
                "items": [
                    {
                        "entity_id": "vod:removed-vod:0123456789abcdef",
                        "entity_type": "vod_site",
                    }
                ],
            }
        ]
    }

    matched = _release_source_url_matches(
        tmp_path,
        "g00000001",
        previous_health,
        DenylistMatchers(
            source_ids=frozenset(),
            hosts=frozenset({"vod.example.test"}),
            urls=frozenset(),
        ),
    )

    assert matched == frozenset({"removed-vod"})


def test_safety_health_is_previous_health_minus_mandatory_without_history_changes() -> None:
    previous = {
        "schema_version": "1.0.0",
        "generated_at": "2026-07-12T12:00:00Z",
        "generation": 1,
        "release_id": "g00000001",
        "sources": [
            {
                "entity_id": "source:live",
                "source_id": "live",
                "technical_status": "healthy",
                "publication_status": "stable",
                "rights_status": "open_license",
                "last_checked_at": "2026-07-12T12:00:00Z",
                "upstream_revision": "a" * 40,
                "failure_reason": None,
                "items": [
                    {
                        "entity_type": "live_url",
                        "entity_id": "live:good",
                        "channel_id": "channel:one",
                        "technical_status": "healthy",
                        "publication_status": "stable",
                        "last_success_at": "2026-07-12T12:00:00Z",
                        "consecutive_successes": 7,
                        "consecutive_failures": 0,
                    },
                    {
                        "entity_type": "live_url",
                        "entity_id": "live:remove",
                        "channel_id": "channel:one",
                        "technical_status": "healthy",
                        "publication_status": "stable",
                        "last_success_at": "2026-07-12T12:00:00Z",
                        "consecutive_successes": 3,
                        "consecutive_failures": 0,
                    },
                ],
            }
        ],
        "channels": [
            {
                "entity_id": "channel:one",
                "technical_status": "healthy",
                "publication_status": "stable",
                "rights_status": "open_license",
                "selected_url_id": "live:good",
                "candidate_url_ids": ["live:good", "live:remove"],
            }
        ],
    }
    context = RunContext(
        owner="azhansy",
        repository="ds-tvbox",
        generated_ref="generated",
        workflow_run_id="2",
        workflow_run_attempt=1,
        generated_at="2026-07-22T12:00:00Z",
        generation=2,
        release_kind=ReleaseKind.SAFETY,
        previous_head="b" * 40,
        previous_last_success_at="2026-07-12T12:00:00Z",
    )

    derived = _safety_health_document(
        previous_health=previous,
        context=context,
        mandatory_ids=frozenset({"live:remove"}),
        mandatory_sources=frozenset(),
    )

    item = derived["sources"][0]["items"][0]
    assert item["entity_id"] == "live:good"
    assert item["consecutive_successes"] == 7
    assert item["last_success_at"] == "2026-07-12T12:00:00Z"
    assert derived["sources"][0]["last_checked_at"] == "2026-07-22T12:00:00Z"
    assert derived["channels"][0]["candidate_url_ids"] == ["live:good"]
    assert derived["generation"] == 2
    assert derived["release_id"] == "g00000002"
    assert previous["sources"][0]["last_checked_at"] == "2026-07-12T12:00:00Z"


def test_safety_health_recomputes_clean_sources_and_preserves_failed_aggregate() -> None:
    previous = {
        "schema_version": "1.0.0",
        "generated_at": "2026-07-12T12:00:00Z",
        "generation": 1,
        "release_id": "g00000001",
        "sources": [
            {
                "entity_id": "source:clean",
                "source_id": "clean",
                "technical_status": "healthy",
                "publication_status": "stable",
                "rights_status": "open_license",
                "last_checked_at": "2026-07-12T12:00:00Z",
                "upstream_revision": "a" * 40,
                "failure_reason": None,
                "items": [
                    {
                        "entity_type": "vod_site",
                        "entity_id": "vod:clean:one",
                        "technical_status": "healthy",
                        "publication_status": "stable",
                        "capabilities": {"home": True},
                    }
                ],
            },
            {
                "entity_id": "source:failed",
                "source_id": "failed",
                "technical_status": "suspect",
                "publication_status": "experimental",
                "rights_status": "public_unverified",
                "last_checked_at": "2026-07-12T12:00:00Z",
                "upstream_revision": None,
                "failure_reason": "fetch_timeout",
                "items": [
                    {
                        "entity_type": "vod_site",
                        "entity_id": "vod:failed:one",
                        "technical_status": "dead",
                        "publication_status": "withheld",
                    }
                ],
            },
        ],
        "channels": [],
    }
    context = RunContext(
        owner="azhansy",
        repository="ds-tvbox",
        generated_ref="generated",
        workflow_run_id="2",
        workflow_run_attempt=1,
        generated_at="2026-07-22T12:00:00Z",
        generation=2,
        release_kind=ReleaseKind.SAFETY,
        previous_head="b" * 40,
        previous_last_success_at="2026-07-12T12:00:00Z",
    )

    derived = _safety_health_document(
        previous_health=previous,
        context=context,
        mandatory_ids=frozenset({"vod:clean:one", "vod:failed:one"}),
        mandatory_sources=frozenset(),
    )

    clean, failed = derived["sources"]
    assert clean["items"] == []
    assert clean["technical_status"] == "unknown"
    assert clean["publication_status"] == "withheld"
    assert clean["rights_status"] == "open_license"
    assert clean["upstream_revision"] == "a" * 40
    assert clean["failure_reason"] is None
    assert failed["items"] == []
    assert failed["technical_status"] == "suspect"
    assert failed["publication_status"] == "experimental"
    assert failed["rights_status"] == "public_unverified"
    assert failed["upstream_revision"] is None
    assert failed["failure_reason"] == "fetch_timeout"
    assert {item["last_checked_at"] for item in derived["sources"]} == {
        context.generated_at
    }


def _selected_live(
    *,
    source_id: str,
    url: str,
    rights: RightsStatus,
) -> SelectedChannel:
    candidate = LiveCandidate(
        source_id=source_id,
        name="CCTV-1",
        original_url=url,
        normalized_url=url,
        rights_status=rights,
        tvg_id="cctv1",
    )
    result = LiveProbeResult(
        candidate=candidate,
        technical_status=TechnicalStatus.HEALTHY,
        publication_status=PublicationStatus.STABLE,
        media=MediaProbeResult(
            ok=True,
            final_url=url,
            response_ms=100,
            media_path_score=2,
        ),
        consecutive_successes=3,
        consecutive_failures=0,
        last_success_at="2026-07-12T12:00:00Z",
        failure_reason=None,
        response_ms_history=(100,),
    )
    channel_id, basis, normalized = channel_identity(candidate)
    return SelectedChannel(
        channel_id=channel_id,
        identity_basis=basis,
        normalized_identity=normalized,
        selected=result,
        candidates=(result,),
    )


def test_safety_health_recomputes_channel_from_retained_selected_candidate() -> None:
    retained = _selected_live(
        source_id="retained",
        url="https://live.example.test/retained.m3u8",
        rights=RightsStatus.PUBLIC_UNVERIFIED,
    )
    removed = _selected_live(
        source_id="removed",
        url="https://live.example.test/removed.m3u8",
        rights=RightsStatus.VERIFIED,
    )
    channel_id = retained.channel_id
    retained_id = live_url_id(retained.selected.candidate)
    removed_id = live_url_id(removed.selected.candidate)
    previous = {
        "schema_version": "1.0.0",
        "generated_at": "2026-07-12T12:00:00Z",
        "generation": 1,
        "release_id": "g00000001",
        "sources": [
            {
                "entity_id": "source:removed",
                "source_id": "removed",
                "technical_status": "healthy",
                "publication_status": "stable",
                "rights_status": "verified",
                "last_checked_at": "2026-07-12T12:00:00Z",
                "upstream_revision": "a" * 40,
                "failure_reason": None,
                "items": [
                    {
                        "entity_type": "live_url",
                        "entity_id": removed_id,
                        "channel_id": channel_id,
                        "technical_status": "healthy",
                        "publication_status": "stable",
                        "probe_fact": "must-not-leak",
                    }
                ],
            },
            {
                "entity_id": "source:retained",
                "source_id": "retained",
                "technical_status": "healthy",
                "publication_status": "stable",
                "rights_status": "public_unverified",
                "last_checked_at": "2026-07-12T12:00:00Z",
                "upstream_revision": "b" * 40,
                "failure_reason": None,
                "items": [
                    {
                        "entity_type": "live_url",
                        "entity_id": retained_id,
                        "channel_id": channel_id,
                        "technical_status": "healthy",
                        "publication_status": "stable",
                        "probe_fact": {"response_ms": 100},
                    }
                ],
            },
        ],
        "channels": [
            {
                "entity_id": channel_id,
                "identity_basis": retained.identity_basis,
                "normalized_identity": retained.normalized_identity,
                "technical_status": "healthy",
                "publication_status": "stable",
                "rights_status": "verified",
                "selected_url_id": removed_id,
                "candidate_url_ids": [removed_id, retained_id],
            }
        ],
    }
    context = RunContext(
        owner="azhansy",
        repository="ds-tvbox",
        generated_ref="generated",
        workflow_run_id="2",
        workflow_run_attempt=1,
        generated_at="2026-07-22T12:00:00Z",
        generation=2,
        release_kind=ReleaseKind.SAFETY,
        previous_head="b" * 40,
        previous_last_success_at="2026-07-12T12:00:00Z",
    )

    derived = _safety_health_document(
        previous_health=previous,
        context=context,
        mandatory_ids=frozenset(),
        mandatory_sources=frozenset({"removed"}),
        selected_channels=(retained,),
    )

    assert [source["source_id"] for source in derived["sources"]] == ["retained"]
    assert derived["sources"][0]["items"] == previous["sources"][1]["items"]
    assert derived["channels"] == [
        {
            "entity_id": channel_id,
            "identity_basis": retained.identity_basis,
            "normalized_identity": retained.normalized_identity,
            "technical_status": "healthy",
            "publication_status": "stable",
            "rights_status": "public_unverified",
            "selected_url_id": retained_id,
            "candidate_url_ids": [retained_id],
        }
    ]


def test_safety_health_rejects_selected_live_url_outside_retained_baseline() -> None:
    selected = _selected_live(
        source_id="live",
        url="https://live.example.test/injected.m3u8",
        rights=RightsStatus.OPEN_LICENSE,
    )
    channel_id = selected.channel_id
    previous = {
        "sources": [
            {
                "entity_id": "source:live",
                "source_id": "live",
                "technical_status": "healthy",
                "publication_status": "stable",
                "rights_status": "open_license",
                "last_checked_at": "2026-07-12T12:00:00Z",
                "upstream_revision": "a" * 40,
                "failure_reason": None,
                "items": [
                    {
                        "entity_type": "live_url",
                        "entity_id": "live-url:live:retained",
                        "channel_id": channel_id,
                        "technical_status": "healthy",
                        "publication_status": "stable",
                    }
                ],
            }
        ],
        "channels": [
            {
                "entity_id": channel_id,
                "candidate_url_ids": ["live-url:live:retained"],
            }
        ],
    }
    context = RunContext(
        owner="azhansy",
        repository="ds-tvbox",
        generated_ref="generated",
        workflow_run_id="2",
        workflow_run_attempt=1,
        generated_at="2026-07-22T12:00:00Z",
        generation=2,
        release_kind=ReleaseKind.SAFETY,
        previous_head="b" * 40,
        previous_last_success_at="2026-07-12T12:00:00Z",
    )

    with pytest.raises(ContractError, match="outside the retained baseline"):
        _safety_health_document(
            previous_health=previous,
            context=context,
            mandatory_ids=frozenset(),
            mandatory_sources=frozenset(),
            selected_channels=(selected,),
        )


def test_previous_source_specs_restore_exact_direct_vod_client_facts(
    tmp_path: Path,
) -> None:
    release = tmp_path / "dist/releases/g00000001"
    (release / "configs").mkdir(parents=True)
    (release / "index.json").write_text(
        json.dumps(
            {
                "urls": [
                    {
                        "name": "⚠️ iKun 资源",
                        "url": (
                            "https://raw.githubusercontent.com/azhansy/ds-tvbox/"
                            "generated/dist/releases/g00000001/configs/ikun-vod.json"
                        ),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (release / "configs/ikun-vod.json").write_text(
        json.dumps(
            {
                "sites": [
                    {
                        "key": "ikun_vod",
                        "name": "⚠️ iKun 资源",
                        "type": 1,
                        "api": "https://ikunzyapi.com/api.php/provide/vod",
                        "searchable": 1,
                        "quickSearch": 0,
                        "filterable": 1,
                        "changeable": 0,
                        "categories": ["电影"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    health = {
        "sources": [
            {
                "source_id": "ikun-vod",
                "rights_status": "public_unverified",
            }
        ]
    }
    manifest = {
        "upstreams": [
            {
                "source_id": "ikun-vod",
                "resolved_fetch_url": "https://ikunzyapi.com/api.php/provide/vod",
            }
        ]
    }

    source = _previous_source_specs(
        tmp_path,
        "g00000001",
        health,
        manifest,
    )[0]

    assert source.client_site is not None
    assert source.client_site.key == "ikun_vod"
    assert source.client_site.name == "iKun 资源"
    assert source.client_site.searchable == 1
    assert source.client_site.quick_search == 0
    assert source.client_site.filterable == 1
    assert source.client_site.changeable == 0


def test_previous_source_specs_fail_closed_for_ambiguous_direct_vod_config(
    tmp_path: Path,
) -> None:
    release = tmp_path / "dist/releases/g00000001"
    (release / "configs").mkdir(parents=True)
    line_url = (
        "https://raw.githubusercontent.com/azhansy/ds-tvbox/"
        "generated/dist/releases/g00000001/configs/vod.json"
    )
    (release / "index.json").write_text(
        json.dumps({"urls": [{"name": "VOD", "url": line_url}]}),
        encoding="utf-8",
    )
    site = {
        "key": "vod",
        "name": "VOD",
        "type": 1,
        "api": "https://vod.example.test/api",
        "searchable": 1,
        "quickSearch": 1,
        "filterable": 1,
        "changeable": 1,
    }
    (release / "configs/vod.json").write_text(
        json.dumps({"sites": [site, {**site, "key": "vod-2"}]}),
        encoding="utf-8",
    )
    health = {"sources": [{"source_id": "vod", "rights_status": "open_license"}]}
    manifest = {
        "upstreams": [
            {
                "source_id": "vod",
                "resolved_fetch_url": "https://vod.example.test/api",
            }
        ]
    }

    with pytest.raises(ContractError, match="client facts are ambiguous"):
        _previous_source_specs(tmp_path, "g00000001", health, manifest)


def test_upstream_manifest_keeps_every_enabled_source_with_reviewed_fallbacks() -> None:
    base = _collection()
    observations = tuple(
        SourceObservation(
            source_id=source.id,
            fetch_mode=source.fetch.mode.value,
            resolved_revision=None,
            resolved_fetch_url=None,
            content_sha256=None,
            terms_sha256={},
            technical_status=TechnicalStatus.SUSPECT,
            failure_reason=FailureReason.FETCH_TIMEOUT,
            secondary_reasons=(),
            enumerated=False,
        )
        for source in base.sources
    )

    records = _upstream_records(replace(base, source_observations=observations))

    assert [item["source_id"] for item in records] == sorted(
        source.id for source in base.sources
    )
    tracked = next(item for item in records if item["source_id"] == "iptv-org-cn-cctv")
    assert tracked["resolved_revision"] == tracked["reviewed_revision"]
    assert str(tracked["resolved_fetch_url"]).startswith(
        "https://raw.githubusercontent.com/iptv-org/iptv/"
    )

    with pytest.raises(ContractError, match="missing observations"):
        _upstream_records(replace(base, source_observations=observations[:-1]))
