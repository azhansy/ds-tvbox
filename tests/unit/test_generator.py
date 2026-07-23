from __future__ import annotations

import json

from ds_tvbox.generator import (
    build_client_artifacts,
    deduplicate_vod_results,
    render_m3u,
)
from ds_tvbox.models import (
    ClientSiteSpec,
    FetchMode,
    FetchSpec,
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


def _context(*, generation: int = 2, release_kind: ReleaseKind = ReleaseKind.REGULAR) -> RunContext:
    return RunContext(
        owner="azhansy",
        repository="ds-tvbox",
        generated_ref="generated",
        workflow_run_id="123",
        workflow_run_attempt=2,
        generated_at="2026-07-22T12:00:00Z",
        generation=generation,
        release_kind=release_kind,
        previous_head="a" * 40,
        previous_last_success_at="2026-07-12T12:00:00Z",
    )


def _source(
    source_id: str,
    *,
    rights: RightsStatus,
    kind: SourceKind = SourceKind.VOD_CONFIG,
    line_name: str | None = None,
) -> SourceSpec:
    return SourceSpec(
        id=source_id,
        kind=kind,
        parser=ParserKind.TVBOX_JSON,
        enabled=True,
        fetch=FetchSpec(
            mode=FetchMode.DIRECT_URL,
            reviewed_url="https://example.test/config.json",
            repository_url=None,
            track_ref=None,
            config_path=None,
            reviewed_revision=None,
        ),
        terms_watch=(),
        rights_status=rights,
        config_license_status="unknown",
        content_rights_status="unverified",
        allowed_hosts=frozenset({"example.test"}),
        allow_discovered_media_hosts=False,
        http_exceptions=(),
        denied_categories=("Denied",),
        client_site=(
            ClientSiteSpec(
                key=f"client-{source_id}",
                name=line_name or source_id,
                searchable=1,
                quick_search=1,
                filterable=1,
                changeable=1,
            )
            if kind is SourceKind.VOD_SITE
            else None
        ),
        catalog=None,
        raw={},
    )


def _vod(
    source_id: str,
    *,
    rights: RightsStatus,
    publication: PublicationStatus = PublicationStatus.STABLE,
    technical: TechnicalStatus = TechnicalStatus.HEALTHY,
    site_type: int = 1,
    key: str = "same",
    name: str = "Shared",
    api: str | None = None,
    raw: dict[str, object] | None = None,
) -> VodProbeResult:
    return VodProbeResult(
        candidate=VodSiteCandidate(
            source_id=source_id,
            key=key,
            name=name,
            type=site_type,
            api=api or f"https://example.test/{source_id}/api",
            searchable=1,
            quick_search=1,
            filterable=1,
            changeable=1,
            categories=("Drama", "Denied", "Drama"),
            rights_status=rights,
            raw=raw or {},
        ),
        technical_status=technical,
        publication_status=publication,
        capabilities=VodCapabilities(home=True, search=True, detail=True, play=True),
        failure_reason=None,
    )


def _channel(
    *,
    channel_id: str = "channel:1",
    normalized_identity: str = "news.test",
    source_id: str = "z-public",
    entry_url: str = "https://live.example.test/news.m3u8",
    final_url: str | None = None,
    name: str = "⚠️ News",
    tvg_id: str = "news.test",
) -> SelectedChannel:
    candidate = LiveCandidate(
        source_id=source_id,
        name=name,
        original_url=entry_url,
        normalized_url=entry_url,
        rights_status=RightsStatus.PUBLIC_UNVERIFIED,
        tvg_id=tvg_id,
        group="Public",
        logo="http://invalid.example/logo.png",
        epg="https://epg.example.test/guide.xml",
    )
    probe = LiveProbeResult(
        candidate=candidate,
        technical_status=TechnicalStatus.HEALTHY,
        publication_status=PublicationStatus.STABLE,
        media=MediaProbeResult(
            ok=True,
            final_url=final_url or candidate.normalized_url,
            response_ms=100,
            media_path_score=2,
        ),
        consecutive_successes=2,
        consecutive_failures=0,
        last_success_at="2026-07-22T12:00:00Z",
        failure_reason=None,
    )
    return SelectedChannel(
        channel_id=channel_id,
        identity_basis="tvg_id",
        normalized_identity=normalized_identity,
        selected=probe,
        candidates=(probe,),
    )


def test_m3u_deduplicates_redirected_entries_by_normalized_final_url() -> None:
    first = _channel(
        channel_id="channel:a",
        normalized_identity="a.test",
        source_id="source-a",
        entry_url="https://entry-a.example.test/live.m3u8",
        final_url="HTTPS://CDN.EXAMPLE.TEST:443/shared.m3u8#fragment",
        name="First",
        tvg_id="a.test",
    )
    second = _channel(
        channel_id="channel:b",
        normalized_identity="b.test",
        source_id="source-b",
        entry_url="https://entry-b.example.test/live.m3u8",
        final_url="https://cdn.example.test/shared.m3u8",
        name="Second",
        tvg_id="b.test",
    )

    rendered, count = render_m3u((second, first))
    text = rendered.decode("utf-8")

    assert count == 1
    assert text.count("https://cdn.example.test/shared.m3u8\n") == 1
    assert "entry-a.example.test" not in text
    assert "entry-b.example.test" not in text
    assert ",⚠️ First\n" in text


def _json(data: bytes) -> dict[str, object]:
    value = json.loads(data)
    assert isinstance(value, dict)
    return value


def test_generation_is_deterministic_sorted_warned_and_single_release() -> None:
    sources = [
        _source(
            "z-public",
            rights=RightsStatus.PUBLIC_UNVERIFIED,
            kind=SourceKind.VOD_SITE,
            line_name="⚠️ Public",
        ),
        _source("c-experimental", rights=RightsStatus.VERIFIED),
        _source("b-open", rights=RightsStatus.OPEN_LICENSE),
        _source("a-verified", rights=RightsStatus.VERIFIED),
    ]
    vod = [
        _vod("z-public", rights=RightsStatus.PUBLIC_UNVERIFIED, name="⚠️ Shared"),
        _vod(
            "c-experimental",
            rights=RightsStatus.VERIFIED,
            publication=PublicationStatus.EXPERIMENTAL,
            technical=TechnicalStatus.PARTIAL,
            site_type=4,
            name="Experimental",
        ),
        _vod("b-open", rights=RightsStatus.OPEN_LICENSE),
        _vod("a-verified", rights=RightsStatus.VERIFIED),
    ]
    context = _context()

    first = build_client_artifacts(
        context=context,
        sources=sources,
        vod_results=vod,
        channels=[_channel()],
    )
    second = build_client_artifacts(
        context=context,
        sources=reversed(sources),
        vod_results=reversed(vod),
        channels=[_channel()],
    )

    assert dict(first.files) == dict(second.files)
    prefix = "dist/releases/g00000002"
    index = _json(first.release_files[f"{prefix}/index.json"])
    assert [item["name"] for item in index["urls"]] == [  # type: ignore[index]
        "DS 稳定聚合",
        "DS a-verified",
        "DS b-open",
        "⚠️ Public",
        "DS c-experimental",
    ]
    assert all(
        "/dist/releases/g00000002/" in item["url"]  # type: ignore[index]
        for item in index["urls"]  # type: ignore[index]
    )
    stable = _json(first.release_files[f"{prefix}/configs/stable.json"])
    sites = stable["sites"]  # type: ignore[assignment]
    assert [item["key"] for item in sites] == [  # type: ignore[index]
        "src_a-verified_same",
        "src_b-open_same",
        "client-z-public",
    ]
    assert [item["name"] for item in sites] == [  # type: ignore[index]
        "Shared [a-verified]",
        "Shared [b-open]",
        "⚠️ Public",
    ]
    assert all(item["categories"] == ["Drama"] for item in sites)  # type: ignore[index]
    independent_stable_sites = []
    for source_id in ("a-verified", "b-open", "z-public"):
        config = _json(first.release_files[f"{prefix}/configs/{source_id}.json"])
        independent_stable_sites.extend(config["sites"])  # type: ignore[arg-type]
    assert sites == independent_stable_sites

    stable_depot = _json(first.release_files[f"{prefix}/depots/stable.json"])
    assert len(stable_depot["urls"]) == 4  # type: ignore[arg-type]
    risk_depot = _json(first.release_files[f"{prefix}/depots/public-unverified.json"])
    assert [item["name"] for item in risk_depot["urls"]] == ["⚠️ Public"]  # type: ignore[index]
    assert first.independent_source_ids == (
        "a-verified",
        "b-open",
        "z-public",
        "c-experimental",
    )

    m3u = first.release_files[f"{prefix}/live/stable.m3u"].decode()
    assert m3u.startswith("#EXTM3U\n")
    assert "⚠️ ⚠️" not in m3u
    assert ",⚠️ News\n" in m3u
    assert "tvg-logo" not in m3u
    assert 'tvg-url="https://epg.example.test/guide.xml"' in m3u
    assert first.live_channel_count == 1


def test_safety_empty_generation_keeps_stable_entry_and_empty_m3u() -> None:
    result = build_client_artifacts(
        context=_context(generation=3, release_kind=ReleaseKind.SAFETY),
        sources=[],
        vod_results=[],
        channels=[],
    )
    prefix = "dist/releases/g00000003"
    index = _json(result.release_files[f"{prefix}/index.json"])
    stable_depot = _json(result.release_files[f"{prefix}/depots/stable.json"])
    risk_depot = _json(result.release_files[f"{prefix}/depots/public-unverified.json"])
    stable = _json(result.release_files[f"{prefix}/configs/stable.json"])

    assert len(index["urls"]) == 1  # type: ignore[arg-type]
    assert len(stable_depot["urls"]) == 1  # type: ignore[arg-type]
    assert risk_depot == {"urls": []}
    assert stable == {"lives": [], "parses": [], "sites": []}
    assert result.release_files[f"{prefix}/live/stable.m3u"] == b"#EXTM3U\n"


def test_vod_sites_are_globally_deduplicated_by_type_and_api() -> None:
    shared_api = "https://example.test/shared/api"
    sources = [
        _source("z-open", rights=RightsStatus.OPEN_LICENSE),
        _source("a-verified", rights=RightsStatus.VERIFIED),
    ]
    results = [
        _vod("z-open", rights=RightsStatus.OPEN_LICENSE, api=shared_api),
        _vod("a-verified", rights=RightsStatus.VERIFIED, api=shared_api),
    ]

    generated = build_client_artifacts(
        context=_context(),
        sources=reversed(sources),
        vod_results=reversed(results),
        channels=[],
    )

    stable = _json(
        generated.release_files["dist/releases/g00000002/configs/stable.json"]
    )
    assert [item["api"] for item in stable["sites"]] == [shared_api]  # type: ignore[attr-defined]
    assert generated.independent_source_ids == ("a-verified",)


def test_vod_deduplication_is_shared_with_health_and_gate_facts() -> None:
    shared_api = "https://example.test/shared/api"
    sources = (
        _source("z-open", rights=RightsStatus.OPEN_LICENSE),
        _source("a-verified", rights=RightsStatus.VERIFIED),
    )
    results = (
        _vod("z-open", rights=RightsStatus.OPEN_LICENSE, api=shared_api),
        _vod("a-verified", rights=RightsStatus.VERIFIED, api=shared_api),
        _vod(
            "a-verified",
            rights=RightsStatus.VERIFIED,
            api=shared_api,
            key="duplicate-alias",
        ),
    )

    deduplicated = deduplicate_vod_results(results, sources)

    assert len(deduplicated) == 2
    by_source = {item.candidate.source_id: item for item in deduplicated}
    assert by_source["a-verified"].publication_status is PublicationStatus.STABLE
    assert by_source["z-open"].publication_status is PublicationStatus.WITHHELD


def test_non_publishable_and_extension_dependent_sites_are_filtered() -> None:
    sources = [
        _source("blocked", rights=RightsStatus.UNKNOWN),
        _source("adapter", rights=RightsStatus.VERIFIED),
    ]
    vod = [
        _vod("blocked", rights=RightsStatus.UNKNOWN),
        _vod(
            "adapter",
            rights=RightsStatus.VERIFIED,
            publication=PublicationStatus.EXPERIMENTAL,
            technical=TechnicalStatus.PARTIAL,
            site_type=4,
            raw={"ext": "https://example.test/adapter.js"},
        ),
        _vod(
            "adapter",
            rights=RightsStatus.VERIFIED,
            raw={"jar": "https://example.test/adapter.jar"},
        ),
    ]
    result = build_client_artifacts(
        context=_context(), sources=sources, vod_results=vod, channels=[]
    )
    assert result.independent_source_ids == ()
    assert not any(path.endswith("blocked.json") for path in result.release_files)
    assert not any(path.endswith("adapter.json") for path in result.release_files)
