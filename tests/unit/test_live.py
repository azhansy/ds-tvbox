from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace

import pytest

from ds_tvbox.live import (
    MAX_SUCCESSFUL_RESPONSE_SAMPLES,
    channel_identity,
    deduplicate_final_urls,
    median_response_ms,
    normalize_channel_name,
    normalize_tvg_id,
    probe_hls,
    probe_live,
    quality_score,
    select_channels,
)
from ds_tvbox.models import (
    DeclaredHeaders,
    FailureReason,
    LiveCandidate,
    LiveProbeResult,
    MediaProbeResult,
    PublicationStatus,
    RightsStatus,
    TechnicalStatus,
)
from ds_tvbox.vod import HttpResponse

_TS_PACKET = b"\x47\x40\x00\x10" + b"\x00" * 184
_TS_SEGMENT = _TS_PACKET * 3


class FakeClient:
    def __init__(self, responses: Mapping[str, HttpResponse], require_header: bool = False) -> None:
        self.responses = responses
        self.require_header = require_header
        self.calls: list[tuple[str, Mapping[str, str] | None, int | None]] = []

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        max_bytes: int | None = None,
    ) -> HttpResponse:
        self.calls.append((url, headers, max_bytes))
        if self.require_header and not (headers or {}).get("Referer"):
            return HttpResponse(403, b"forbidden", url)
        return self.responses[url]


def live_candidate(
    url: str = "https://live.example.test/master.m3u8",
    *,
    source: str = "source-a",
    name: str = "央视 1",
    tvg_id: str | None = "CCTV1.CN",
    rights: RightsStatus = RightsStatus.PUBLIC_UNVERIFIED,
    headers: DeclaredHeaders | None = None,
) -> LiveCandidate:
    return LiveCandidate(
        source_id=source,
        name=name,
        original_url=url,
        normalized_url=url,
        rights_status=rights,
        tvg_id=tvg_id,
        group="新闻",
        declared_headers=headers,
    )


def hls_responses() -> dict[str, HttpResponse]:
    root = "https://live.example.test/master.m3u8"
    child = "https://live.example.test/1080/index.m3u8"
    segment = "https://live.example.test/1080/seg-1.ts"
    return {
        root: HttpResponse(
            200,
            b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=5000000,RESOLUTION=1920x1080\n1080/index.m3u8\n",
            root,
            10,
        ),
        child: HttpResponse(
            200,
            b"#EXTM3U\n#EXT-X-TARGETDURATION:10\n#EXTINF:10,\nseg-1.ts\n",
            child,
            20,
        ),
        segment: HttpResponse(206, _TS_SEGMENT, segment, 30),
    }


def test_hls_master_child_and_bounded_segment_probe() -> None:
    client = FakeClient(hls_responses())
    result = probe_hls("https://live.example.test/master.m3u8", client)
    assert result.ok
    assert result.media_path_score == 2
    assert (result.width, result.height, result.bandwidth) == (1920, 1080, 5_000_000)
    assert result.response_ms == 60
    assert client.calls[-1][1] == {"Range": "bytes=0-1048575"}
    assert client.calls[-1][2] == 1024 * 1024


def test_direct_media_playlist_has_path_score_one_and_nullable_quality() -> None:
    playlist = "https://live.example.test/index.m3u8"
    segment = "https://live.example.test/a.ts"
    client = FakeClient(
        {
            playlist: HttpResponse(
                200,
                b"#EXTM3U\n#EXT-X-TARGETDURATION:5\n#EXTINF:5,\na.ts\n",
                playlist,
                5,
            ),
            segment: HttpResponse(200, _TS_SEGMENT, segment, 7),
        }
    )
    result = probe_hls(playlist, client)
    assert result.ok and result.media_path_score == 1
    assert result.width is result.height is result.bandwidth is None
    assert quality_score(result) == 1


def test_media_sequence_playlist_and_fmp4_segment_are_supported() -> None:
    playlist = "https://live.example.test/index.m3u8"
    segment = "https://live.example.test/a.m4s"
    fmp4 = b"\x00\x00\x00\x10moof" + b"\x00" * 8
    client = FakeClient(
        {
            playlist: HttpResponse(
                200,
                b"#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:0\n#EXTINF:5.5,\na.m4s\n",
                playlist,
                5,
            ),
            segment: HttpResponse(206, fmp4, segment, 7),
        }
    )

    result = probe_hls(playlist, client)

    assert result.ok and result.media_path_score == 1


@pytest.mark.parametrize(
    "payload",
    [
        b"\xff\xf1\x50\x80\x01\x3f\xfc\x00\x00",  # ADTS AAC
        b"ID3\x04\x00\x00\x00\x00\x00\x00"
        b"\xff\xf1\x50\x80\x01\x3f\xfc\x00\x00",  # ID3-prefixed AAC
        b"\xff\xfb\x90\x64" + b"\x00" * 16,  # MPEG audio
        b"\x0b\x77" + b"\x00" * 8,  # AC-3/E-AC-3
    ],
)
def test_common_hls_packed_audio_segments_are_supported(payload: bytes) -> None:
    playlist = "https://live.example.test/audio.m3u8"
    segment = "https://live.example.test/audio.aac"
    client = FakeClient(
        {
            playlist: HttpResponse(
                200,
                b"#EXTM3U\n#EXT-X-TARGETDURATION:5\n#EXTINF:5,\naudio.aac\n",
                playlist,
                5,
            ),
            segment: HttpResponse(206, payload, segment, 7),
        }
    )

    assert probe_hls(playlist, client).ok


@pytest.mark.parametrize(
    "body",
    [
        b"#EXTM3U\n#EXTINF:-1,News\nhttps://live.example.test/channel.m3u8\n",
        b"#EXTM3U\n#EXTINF:5,\na.ts\n",
        b"#EXTM3U\n#EXT-X-TARGETDURATION:5\na.ts\n",
        b"#EXTM3U\n#EXT-X-TARGETDURATION:5\n#EXTINF:0,\na.ts\n",
        b"#EXTM3U\n#EXT-X-TARGETDURATION:nope\n#EXTINF:5,\na.ts\n",
        b"#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:-1\n#EXTINF:5,\na.ts\n",
        b"#EXTM3U\n#EXT-X-TARGETDURATION:5\n#EXTINF:5,\n#EXTINF:5,\na.ts\n",
        b"#EXTM3U\n#EXT-X-TARGETDURATION:5\n#EXTINF:5,\n",
        b"#EXTM3U\n#EXT-X-STREAM-INF:RESOLUTION=1920x1080\nchild.m3u8\n",
        b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=0\nchild.m3u8\n",
        b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1,RESOLUTION=bad\nchild.m3u8\n",
        b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nchild.m3u8\nstray.m3u8\n",
        b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nchild.m3u8\n"
        b"#EXT-X-TARGETDURATION:5\n",
        b"<html><body>#EXTM3U</body></html>",
    ],
)
def test_non_hls_or_malformed_playlists_are_rejected(body: bytes) -> None:
    url = "https://live.example.test/index.m3u8"

    result = probe_hls(url, FakeClient({url: HttpResponse(200, body, url)}))

    assert not result.ok
    assert result.failure_reason is FailureReason.MEDIA_PROBE_FAILED


@pytest.mark.parametrize("payload", [b"segment", b"<html>blocked</html>", b"not media"])
def test_arbitrary_nonempty_segment_payload_is_rejected(payload: bytes) -> None:
    playlist = "https://live.example.test/index.m3u8"
    segment = "https://live.example.test/a.ts"
    client = FakeClient(
        {
            playlist: HttpResponse(
                200,
                b"#EXTM3U\n#EXT-X-TARGETDURATION:5\n#EXTINF:5,\na.ts\n",
                playlist,
                5,
            ),
            segment: HttpResponse(206, payload, segment, 7),
        }
    )

    result = probe_hls(playlist, client)

    assert not result.ok
    assert result.failure_reason is FailureReason.MEDIA_PROBE_FAILED


@pytest.mark.parametrize(
    ("status", "body", "reason"),
    [
        (401, b"authorization required", FailureReason.CREDENTIAL_REQUIRED),
        (403, b"forbidden", FailureReason.CREDENTIAL_REQUIRED),
        (404, b"missing", FailureReason.HTTP_404),
        (200, b"not hls", FailureReason.MEDIA_PROBE_FAILED),
        (429, b"slow", FailureReason.RATE_LIMITED),
    ],
)
def test_hls_failures_are_stable(status: int, body: bytes, reason: FailureReason) -> None:
    url = "https://live.example.test/master.m3u8"
    result = probe_hls(url, FakeClient({url: HttpResponse(status, body, url)}))
    assert not result.ok
    assert result.failure_reason is reason


def test_master_variant_uri_must_immediately_follow_stream_info() -> None:
    url = "https://live.example.test/master.m3u8"
    body = b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\n#comment\nchild.m3u8\n"
    result = probe_hls(url, FakeClient({url: HttpResponse(200, body, url)}))
    assert result.failure_reason is FailureReason.MEDIA_PROBE_FAILED


def test_live_probe_inherits_history_and_publishes_healthy_url() -> None:
    result = probe_live(
        live_candidate(),
        FakeClient(hls_responses()),
        checked_at="2026-07-22T12:00:00Z",
        previous={
            "technical_status": "healthy",
            "consecutive_successes": 3,
            "consecutive_failures": 0,
            "last_success_at": "2026-07-12T12:00:00Z",
            "response_ms_history": list(range(10, 18)),
        },
    )
    assert result.technical_status is TechnicalStatus.HEALTHY
    assert result.publication_status is PublicationStatus.STABLE
    assert result.consecutive_successes == 4
    assert result.consecutive_failures == 0
    assert result.last_success_at == "2026-07-22T12:00:00Z"
    assert result.response_ms_history == (12, 13, 14, 15, 16, 17, 60)
    assert len(result.response_ms_history) == MAX_SUCCESSFUL_RESPONSE_SAMPLES


def test_live_header_is_used_only_for_diagnosis() -> None:
    candidate = live_candidate(
        headers=DeclaredHeaders({"Referer": "https://origin.example.test/"})
    )
    result = probe_live(
        candidate,
        FakeClient(hls_responses(), require_header=True),
        checked_at="2026-07-22T12:00:00Z",
    )
    assert result.technical_status is TechnicalStatus.PARTIAL
    assert result.publication_status is PublicationStatus.WITHHELD
    assert result.failure_reason is FailureReason.CLIENT_HEADER_UNSUPPORTED
    assert result.secondary_reasons == (FailureReason.CREDENTIAL_REQUIRED,)
    assert result.media.ok


def test_live_failure_resets_success_streak_and_preserves_last_success() -> None:
    url = "https://live.example.test/master.m3u8"
    result = probe_live(
        live_candidate(),
        FakeClient({url: HttpResponse(404, b"missing", url)}),
        checked_at="2026-07-22T12:00:00Z",
        previous={
            "technical_status": "healthy",
            "consecutive_successes": 3,
            "consecutive_failures": 1,
            "last_success_at": "2026-07-12T12:00:00Z",
        },
    )
    assert result.technical_status is TechnicalStatus.DEAD
    assert result.publication_status is PublicationStatus.WITHHELD
    assert result.consecutive_successes == 0
    assert result.consecutive_failures == 2
    assert result.last_success_at == "2026-07-12T12:00:00Z"


def test_channel_identity_normalization_and_source_isolation() -> None:
    first = live_candidate(tvg_id="  ＣＣＴＶA.CN ")
    second = live_candidate(tvg_id="ｃｃｔｖa.cn", source="source-b")
    assert normalize_tvg_id(first.tvg_id or "") == "cctva.cn"
    assert channel_identity(first)[0] == channel_identity(second)[0]

    non_ascii_upper = live_candidate(tvg_id="Ä.example")
    non_ascii_lower = live_candidate(tvg_id="ä.example", source="source-b")
    assert channel_identity(non_ascii_upper)[0] != channel_identity(non_ascii_lower)[0]

    ascii_first = live_candidate(tvg_id=" CCTV-A.CN ")
    ascii_second = live_candidate(tvg_id="cctv-a.cn", source="source-b")
    assert channel_identity(ascii_first)[0] == channel_identity(ascii_second)[0]

    local_a = live_candidate(tvg_id=None, source="source-a", name=" 央视　 1 ")
    local_b = live_candidate(tvg_id=None, source="source-b", name="央视 1")
    assert normalize_channel_name(local_a.name) == "央视 1"
    assert channel_identity(local_a)[0] != channel_identity(local_b)[0]


@pytest.mark.parametrize(
    ("width", "height", "bandwidth", "score"),
    [
        (1920, 1080, 3_000_000, 4),
        (1280, 720, 1_500_000, 3),
        (640, 360, 500_000, 2),
        (320, 180, 100_000, 0),
        (None, 1080, 5_000_000, 1),
    ],
)
def test_quality_thresholds(
    width: int | None, height: int | None, bandwidth: int | None, score: int
) -> None:
    media = MediaProbeResult(True, "https://x", 1, 1, width, height, bandwidth)
    assert quality_score(media) == score


def probe_result(
    url: str,
    *,
    source: str = "source-a",
    successes: int = 1,
    path_score: int = 1,
    response_ms: int = 100,
    width: int | None = None,
    height: int | None = None,
    bandwidth: int | None = None,
    tvg_id: str | None = "same",
    publication: PublicationStatus = PublicationStatus.STABLE,
    response_history: tuple[int, ...] = (),
) -> LiveProbeResult:
    candidate = live_candidate(url, source=source, tvg_id=tvg_id)
    media = MediaProbeResult(
        True,
        url,
        response_ms,
        path_score,
        width,
        height,
        bandwidth,
    )
    return LiveProbeResult(
        candidate=candidate,
        technical_status=TechnicalStatus.HEALTHY,
        publication_status=publication,
        media=media,
        consecutive_successes=successes,
        consecutive_failures=0,
        last_success_at="2026-07-22T12:00:00Z",
        failure_reason=None,
        response_ms_history=response_history,
    )


def test_channel_selection_uses_all_five_deterministic_keys() -> None:
    results = [
        probe_result("https://x/5.m3u8", successes=1, path_score=2, response_ms=10),
        probe_result("https://x/4.m3u8", successes=2, path_score=1, response_ms=10),
        probe_result("https://x/3.m3u8", successes=2, path_score=2, response_ms=30),
        probe_result(
            "https://x/2.m3u8",
            successes=2,
            path_score=2,
            response_ms=20,
            width=1280,
            height=720,
            bandwidth=1_500_000,
        ),
        probe_result(
            "https://x/1.m3u8",
            successes=2,
            path_score=2,
            response_ms=20,
            width=1280,
            height=720,
            bandwidth=1_500_000,
        ),
    ]
    selected = select_channels(tuple(reversed(results)))
    assert len(selected) == 1
    assert selected[0].selected.candidate.normalized_url == "https://x/1.m3u8"
    assert select_channels(results)[0].selected == selected[0].selected


def test_no_tvg_id_does_not_merge_sources_and_withheld_is_not_selected() -> None:
    first = probe_result("https://x/a.m3u8", source="a", tvg_id=None)
    second = probe_result("https://x/b.m3u8", source="b", tvg_id=None)
    withheld = probe_result(
        "https://x/c.m3u8",
        source="c",
        tvg_id="withheld",
        publication=PublicationStatus.WITHHELD,
    )
    channels = select_channels([first, second, withheld])
    assert len(channels) == 2


def test_channel_selection_uses_rolling_response_median_not_current_jitter() -> None:
    fast_spike = probe_result(
        "https://x/a.m3u8",
        response_ms=1,
        response_history=(100, 100, 100, 1),
    )
    slow_spike = probe_result(
        "https://x/b.m3u8",
        response_ms=200,
        response_history=(50, 50, 50, 200),
    )

    selected = select_channels((fast_spike, slow_spike))

    assert selected[0].selected.candidate.normalized_url == "https://x/b.m3u8"


def test_redirect_final_url_is_deduplicated_before_channel_selection() -> None:
    first = probe_result(
        "https://entry-a.example.test/live.m3u8",
        source="source-a",
        tvg_id="channel-a",
        successes=3,
    )
    second = probe_result(
        "https://entry-b.example.test/live.m3u8",
        source="source-b",
        tvg_id="channel-b",
        successes=1,
    )
    shared = "https://cdn.example.test/shared.m3u8"
    results = (
        replace(first, media=replace(first.media, final_url=shared)),
        replace(second, media=replace(second.media, final_url=shared)),
    )

    deduplicated = deduplicate_final_urls(results)
    channels = select_channels(results)

    assert [item.publication_status for item in deduplicated] == [
        PublicationStatus.STABLE,
        PublicationStatus.WITHHELD,
    ]
    assert len(channels) == 1
    assert channels[0].selected.candidate.source_id == "source-a"


def test_median_response_time_requires_values() -> None:
    assert median_response_ms([30, 10, 20]) == 20
    with pytest.raises(ValueError):
        median_response_ms([])
