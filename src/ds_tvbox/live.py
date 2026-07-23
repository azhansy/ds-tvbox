"""Bounded HLS probing and deterministic global channel selection."""

from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import replace
from statistics import median
from urllib.parse import urljoin

from ds_tvbox.errors import ContractError, SecurityError
from ds_tvbox.models import (
    FailureReason,
    LiveCandidate,
    LiveProbeResult,
    MediaProbeResult,
    PublicationStatus,
    SelectedChannel,
    TechnicalStatus,
)
from ds_tvbox.policy import (
    publication_status_for,
    technical_status_for_failure,
)
from ds_tvbox.security import normalize_client_url_offline
from ds_tvbox.vod import HttpClient, HttpResponse, ProbeRequestError

MAX_HLS_MANIFEST_BYTES = 2 * 1024 * 1024
MAX_MEDIA_PROBE_BYTES = 1024 * 1024
MAX_SUCCESSFUL_RESPONSE_SAMPLES = 7
_ASCII_LOWER_TRANS = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
_ATTRIBUTE_RE = re.compile(r'(?:^|,)([A-Z0-9-]+)=("[^"]*"|[^,]*)')
_MEDIA_PLAYLIST_ANCHORS = ("#EXT-X-TARGETDURATION:", "#EXT-X-MEDIA-SEQUENCE:")
_ISO_BMFF_BOX_TYPES = frozenset(
    {b"emsg", b"free", b"ftyp", b"mdat", b"moof", b"prft", b"sidx", b"skip", b"styp"}
)


def normalize_tvg_id(value: str) -> str:
    return unicodedata.normalize("NFKC", value).strip().translate(_ASCII_LOWER_TRANS)


def normalize_channel_name(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).strip()
    return " ".join(normalized.split())


def channel_identity(candidate: LiveCandidate) -> tuple[str, str, str]:
    normalized_tvg = normalize_tvg_id(candidate.tvg_id or "")
    if normalized_tvg:
        identity_basis = "tvg_id"
        normalized_identity = normalized_tvg
        digest_input = f"tvg:{normalized_tvg}"
    else:
        normalized_name = normalize_channel_name(candidate.name)
        if not normalized_name:
            raise ContractError("live candidate has no usable channel identity")
        identity_basis = "source_name"
        normalized_identity = f"{candidate.source_id}:{normalized_name}"
        digest_input = f"local:{candidate.source_id}:{normalized_name}"
    digest = hashlib.sha256(digest_input.encode("utf-8")).hexdigest()[:16]
    return f"channel:{digest}", identity_basis, normalized_identity


def live_url_id(candidate: LiveCandidate) -> str:
    digest = hashlib.sha256(candidate.normalized_url.encode("utf-8")).hexdigest()[:16]
    return f"live-url:{candidate.source_id}:{digest}"


def quality_score(media: MediaProbeResult) -> int:
    if media.width is None or media.height is None or media.bandwidth is None:
        return 1
    if media.width >= 1920 and media.height >= 1080 and media.bandwidth >= 3_000_000:
        return 4
    if media.width >= 1280 and media.height >= 720 and media.bandwidth >= 1_500_000:
        return 3
    if media.width >= 640 and media.height >= 360 and media.bandwidth >= 500_000:
        return 2
    return 0


def median_response_ms(values: Sequence[int]) -> int:
    if not values:
        raise ValueError("at least one response time is required")
    return int(median(values))


def normalized_media_final_url(media: MediaProbeResult) -> str:
    """Return the canonical, client-visible URL reached after redirects."""

    if not media.ok or not media.final_url:
        raise ContractError("healthy live media requires a final URL")
    try:
        return normalize_client_url_offline(media.final_url).value
    except SecurityError as exc:
        raise ContractError("live media final URL is not publishable") from exc


def _valid_response_ms(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _previous_response_history(
    previous: Mapping[str, object] | None,
) -> tuple[int, ...]:
    previous = previous or {}
    raw_history = previous.get("response_ms_history")
    values: list[int] = []
    if isinstance(raw_history, Sequence) and not isinstance(
        raw_history, (str, bytes, bytearray)
    ):
        values.extend(
            value
            for raw in raw_history
            if (value := _valid_response_ms(raw)) is not None
        )
    if not values and previous.get("technical_status") == TechnicalStatus.HEALTHY.value:
        legacy_value = _valid_response_ms(previous.get("response_ms"))
        if legacy_value is not None:
            values.append(legacy_value)
    return tuple(values[-MAX_SUCCESSFUL_RESPONSE_SAMPLES:])


def _next_response_history(
    previous: Mapping[str, object] | None,
    response_ms: int | None,
    *,
    successful: bool,
) -> tuple[int, ...]:
    values = list(_previous_response_history(previous))
    current = _valid_response_ms(response_ms)
    if successful and current is not None:
        values.append(current)
    return tuple(values[-MAX_SUCCESSFUL_RESPONSE_SAMPLES:])


def _http_reason(status: int) -> FailureReason:
    if status in {401, 403}:
        return FailureReason.CREDENTIAL_REQUIRED
    if status == 404:
        return FailureReason.HTTP_404
    if status == 410:
        return FailureReason.HTTP_410
    if status == 429:
        return FailureReason.RATE_LIMITED
    if 500 <= status <= 599:
        return FailureReason.UPSTREAM_5XX
    return FailureReason.SCHEMA_INCOMPATIBLE


def _get(
    client: HttpClient,
    url: str,
    *,
    headers: Mapping[str, str] | None,
    max_bytes: int,
) -> HttpResponse:
    try:
        response = client.get(url, headers=headers, max_bytes=max_bytes)
    except ProbeRequestError:
        raise
    except TimeoutError as exc:
        raise ProbeRequestError(FailureReason.FETCH_TIMEOUT) from exc
    except OSError as exc:
        raise ProbeRequestError(FailureReason.DNS_FAILURE) from exc
    if not 200 <= response.status_code <= 299:
        raise ProbeRequestError(_http_reason(response.status_code))
    if len(response.body) > max_bytes:
        raise ProbeRequestError(FailureReason.RESPONSE_TOO_LARGE)
    if not response.body:
        raise ProbeRequestError(FailureReason.MEDIA_PROBE_FAILED)
    return response


def _manifest_text(response: HttpResponse) -> str:
    try:
        text = response.body.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ProbeRequestError(FailureReason.MEDIA_PROBE_FAILED) from exc
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines or lines[0] != "#EXTM3U":
        raise ProbeRequestError(FailureReason.MEDIA_PROBE_FAILED)
    return text


def _attributes(line: str) -> dict[str, str]:
    _, _, payload = line.partition(":")
    values: dict[str, str] = {}
    for match in _ATTRIBUTE_RE.finditer(payload):
        raw = match.group(2)
        values[match.group(1)] = raw[1:-1] if raw.startswith('"') and raw.endswith('"') else raw
    return values


def _first_master_variant(
    text: str, base_url: str
) -> tuple[str, int | None, int | None, int | None] | None:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not any(line.startswith("#EXT-X-STREAM-INF:") for line in lines):
        return None
    if any(line.startswith(("#EXTINF:", *_MEDIA_PLAYLIST_ANCHORS)) for line in lines):
        raise ProbeRequestError(FailureReason.MEDIA_PROBE_FAILED)

    selected: tuple[str, int | None, int | None, int | None] | None = None
    claimed_uris: set[int] = set()
    for index, line in enumerate(lines):
        if line.startswith("#EXT-X-STREAM-INF:"):
            attrs = _attributes(line)
            uri_index = index + 1
            uri = lines[uri_index] if uri_index < len(lines) else None
            if uri is None or uri.startswith("#"):
                raise ProbeRequestError(FailureReason.MEDIA_PROBE_FAILED)
            raw_bandwidth = attrs.get("BANDWIDTH", "")
            if re.fullmatch(r"[1-9][0-9]*", raw_bandwidth) is None:
                raise ProbeRequestError(FailureReason.MEDIA_PROBE_FAILED)
            bandwidth = int(raw_bandwidth)
            width: int | None = None
            height: int | None = None
            resolution = attrs.get("RESOLUTION")
            if resolution is not None:
                match = re.fullmatch(r"([1-9][0-9]*)[xX]([1-9][0-9]*)", resolution)
                if match is None:
                    raise ProbeRequestError(FailureReason.MEDIA_PROBE_FAILED)
                width, height = int(match.group(1)), int(match.group(2))
            claimed_uris.add(uri_index)
            if selected is None:
                selected = urljoin(base_url, uri), width, height, bandwidth
        elif not line.startswith("#") and index not in claimed_uris:
            raise ProbeRequestError(FailureReason.MEDIA_PROBE_FAILED)

    if selected is None:  # guarded by the initial scan, retained for type narrowing
        raise ProbeRequestError(FailureReason.MEDIA_PROBE_FAILED)
    return selected


def _validate_extinf(line: str) -> None:
    raw_duration, separator, _title = line.removeprefix("#EXTINF:").partition(",")
    raw_duration = raw_duration.strip()
    if not separator or re.fullmatch(r"(?:0|[1-9][0-9]*)(?:\.[0-9]+)?", raw_duration) is None:
        raise ProbeRequestError(FailureReason.MEDIA_PROBE_FAILED)
    try:
        duration = float(raw_duration)
    except ValueError as exc:
        raise ProbeRequestError(FailureReason.MEDIA_PROBE_FAILED) from exc
    if not math.isfinite(duration) or duration <= 0:
        raise ProbeRequestError(FailureReason.MEDIA_PROBE_FAILED)


def _validate_media_anchor(line: str) -> None:
    if line.startswith("#EXT-X-TARGETDURATION:"):
        raw_value = line.removeprefix("#EXT-X-TARGETDURATION:").strip()
        minimum = 1
    elif line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
        raw_value = line.removeprefix("#EXT-X-MEDIA-SEQUENCE:").strip()
        minimum = 0
    else:
        return
    if re.fullmatch(r"[0-9]+", raw_value) is None:
        raise ProbeRequestError(FailureReason.MEDIA_PROBE_FAILED)
    value = int(raw_value)
    if value < minimum:
        raise ProbeRequestError(FailureReason.MEDIA_PROBE_FAILED)


def _first_media_segment(text: str, base_url: str) -> str:
    """Return a segment only from a structurally valid HLS media playlist."""

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if any(line.startswith("#EXT-X-STREAM-INF:") for line in lines):
        raise ProbeRequestError(FailureReason.MEDIA_PROBE_FAILED)
    if not any(line.startswith(_MEDIA_PLAYLIST_ANCHORS) for line in lines):
        # A normal channel M3U also begins with #EXTM3U and uses #EXTINF, but
        # it does not carry either mandatory HLS media-timeline anchor.
        raise ProbeRequestError(FailureReason.MEDIA_PROBE_FAILED)

    first_segment: str | None = None
    pending_extinf = False
    saw_extinf = False
    for line in lines[1:]:
        _validate_media_anchor(line)
        if line.startswith("#EXTINF:"):
            if pending_extinf:
                raise ProbeRequestError(FailureReason.MEDIA_PROBE_FAILED)
            _validate_extinf(line)
            pending_extinf = True
            saw_extinf = True
            continue
        if line.startswith("#"):
            continue
        if not pending_extinf:
            raise ProbeRequestError(FailureReason.MEDIA_PROBE_FAILED)
        if first_segment is None:
            first_segment = urljoin(base_url, line)
        pending_extinf = False
    if pending_extinf or not saw_extinf or first_segment is None:
        raise ProbeRequestError(FailureReason.MEDIA_PROBE_FAILED)
    return first_segment


def _looks_like_mpeg_ts(body: bytes) -> bool:
    # Standard TS is 188 bytes.  192-byte M2TS and 204-byte FEC framing are
    # common enough in public HLS feeds to retain as compatible variants.
    for packet_size, sync_offset in ((188, 0), (192, 4), (204, 0)):
        packet_count = (len(body) - sync_offset) // packet_size
        if packet_count < 1:
            continue
        headers = [
            sync_offset + packet_size * index for index in range(min(packet_count, 3))
        ]
        if all(
            body[offset] == 0x47
            and body[offset + 1] & 0x80 == 0
            and body[offset + 3] & 0x30 != 0
            for offset in headers
        ):
            return True
    return False


def _looks_like_iso_bmff(body: bytes) -> bool:
    if len(body) < 8 or body[4:8] not in _ISO_BMFF_BOX_TYPES:
        return False
    size = int.from_bytes(body[:4], "big")
    if size == 0:
        return True
    if size == 1:
        return len(body) >= 16 and int.from_bytes(body[8:16], "big") >= 16
    return size >= 8


def _looks_like_adts(body: bytes) -> bool:
    if len(body) < 7 or body[0] != 0xFF or body[1] & 0xF6 != 0xF0:
        return False
    sample_frequency_index = (body[2] >> 2) & 0x0F
    frame_length = ((body[3] & 0x03) << 11) | (body[4] << 3) | (body[5] >> 5)
    return sample_frequency_index != 0x0F and frame_length >= 7


def _looks_like_mpeg_audio(body: bytes) -> bool:
    if len(body) < 4 or body[0] != 0xFF or body[1] & 0xE0 != 0xE0:
        return False
    layer = (body[1] >> 1) & 0x03
    bitrate_index = (body[2] >> 4) & 0x0F
    sample_rate_index = (body[2] >> 2) & 0x03
    return layer != 0 and bitrate_index not in {0, 0x0F} and sample_rate_index != 0x03


def _looks_like_webm_or_matroska(body: bytes) -> bool:
    if len(body) < 8 or not body.startswith(b"\x1a\x45\xdf\xa3"):
        return False
    header = body[: min(len(body), 4096)].lower()
    return b"webm" in header or b"matroska" in header


def _looks_like_flv(body: bytes) -> bool:
    if len(body) < 9 or body[:3] != b"FLV" or body[3] != 1:
        return False
    flags = body[4]
    data_offset = int.from_bytes(body[5:9], "big")
    return flags & ~0x05 == 0 and flags & 0x05 != 0 and data_offset >= 9


def looks_like_media_payload(body: bytes, *, allow_id3: bool = True) -> bool:
    """Recognize bounded bytes from common TVBox-playable media containers."""

    if (
        _looks_like_mpeg_ts(body)
        or _looks_like_iso_bmff(body)
        or _looks_like_adts(body)
        or _looks_like_mpeg_audio(body)
        or _looks_like_webm_or_matroska(body)
        or _looks_like_flv(body)
        or (len(body) >= 7 and body[:2] == b"\x0b\x77")
    ):
        return True
    if not allow_id3 or len(body) < 10 or body[:3] != b"ID3":
        return False
    size_bytes = body[6:10]
    if any(value & 0x80 for value in size_bytes):
        return False
    tag_size = (
        (size_bytes[0] << 21)
        | (size_bytes[1] << 14)
        | (size_bytes[2] << 7)
        | size_bytes[3]
    )
    payload_offset = 10 + tag_size
    return payload_offset < len(body) and looks_like_media_payload(
        body[payload_offset:], allow_id3=False
    )


def probe_hls(
    url: str,
    client: HttpClient,
    *,
    headers: Mapping[str, str] | None = None,
) -> MediaProbeResult:
    """Read a manifest and one bounded segment; never download a full stream."""

    elapsed = 0
    try:
        root = _get(
            client,
            url,
            headers=headers,
            max_bytes=MAX_HLS_MANIFEST_BYTES,
        )
        elapsed += root.elapsed_ms
        root_text = _manifest_text(root)
        variant = _first_master_variant(root_text, root.final_url)
        if variant is None:
            media_text = root_text
            media_url = root.final_url
            width = height = bandwidth = None
            path_score = 1
        else:
            variant_url, width, height, bandwidth = variant
            media_response = _get(
                client,
                variant_url,
                headers=headers,
                max_bytes=MAX_HLS_MANIFEST_BYTES,
            )
            elapsed += media_response.elapsed_ms
            media_text = _manifest_text(media_response)
            media_url = media_response.final_url
            path_score = 2
        segment_url = _first_media_segment(media_text, media_url)
        segment_headers = dict(headers or {})
        segment_headers["Range"] = f"bytes=0-{MAX_MEDIA_PROBE_BYTES - 1}"
        segment = _get(
            client,
            segment_url,
            headers=segment_headers,
            max_bytes=MAX_MEDIA_PROBE_BYTES,
        )
        elapsed += segment.elapsed_ms
        if not looks_like_media_payload(segment.body):
            raise ProbeRequestError(FailureReason.MEDIA_PROBE_FAILED)
        return MediaProbeResult(
            ok=True,
            final_url=root.final_url,
            response_ms=elapsed,
            media_path_score=path_score,
            width=width,
            height=height,
            bandwidth=bandwidth,
        )
    except ProbeRequestError as exc:
        return MediaProbeResult(
            ok=False,
            final_url=None,
            response_ms=elapsed or None,
            media_path_score=0,
            failure_reason=exc.reason,
        )


def _history(
    status: TechnicalStatus,
    previous: Mapping[str, object] | None,
    checked_at: str,
) -> tuple[int, int, str | None]:
    old_successes = _history_int((previous or {}).get("consecutive_successes", 0))
    old_failures = _history_int((previous or {}).get("consecutive_failures", 0))
    old_last_success = (previous or {}).get("last_success_at")
    if status is TechnicalStatus.HEALTHY:
        return old_successes + 1, 0, checked_at
    return 0, old_failures + 1, str(old_last_success) if old_last_success else None


def _history_int(value: object) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, str)):
        return int(value)
    return 0


def probe_live(
    candidate: LiveCandidate,
    client: HttpClient,
    *,
    checked_at: str,
    previous: Mapping[str, object] | None = None,
) -> LiveProbeResult:
    """Probe Header-free first; source Headers are diagnostic-only."""

    first = probe_hls(candidate.original_url, client)
    first_reason = first.failure_reason or (
        None if first.ok else FailureReason.MEDIA_PROBE_FAILED
    )
    if first.ok:
        technical = TechnicalStatus.HEALTHY
        publication = publication_status_for(
            candidate.rights_status,
            technical,
            entity_kind="live",
            media_verified=True,
        )
        successes, failures, last_success = _history(technical, previous, checked_at)
        return LiveProbeResult(
            candidate=candidate,
            technical_status=technical,
            publication_status=publication,
            media=first,
            consecutive_successes=successes,
            consecutive_failures=failures,
            last_success_at=last_success,
            failure_reason=None,
            response_ms_history=_next_response_history(
                previous, first.response_ms, successful=True
            ),
        )

    declared = candidate.declared_headers.values if candidate.declared_headers else None
    if declared:
        diagnostic = probe_hls(candidate.original_url, client, headers=declared)
        if diagnostic.ok:
            technical = TechnicalStatus.PARTIAL
            successes, failures, last_success = _history(technical, previous, checked_at)
            secondary = (first_reason,) if first_reason is not None else ()
            return LiveProbeResult(
                candidate=candidate,
                technical_status=technical,
                publication_status=PublicationStatus.WITHHELD,
                media=diagnostic,
                consecutive_successes=successes,
                consecutive_failures=failures,
                last_success_at=last_success,
                failure_reason=FailureReason.CLIENT_HEADER_UNSUPPORTED,
                secondary_reasons=secondary,
                response_ms_history=_next_response_history(
                    previous, diagnostic.response_ms, successful=False
                ),
            )

    reason = first_reason or FailureReason.MEDIA_PROBE_FAILED
    technical = technical_status_for_failure(
        TechnicalStatus(str(previous["technical_status"]))
        if previous and previous.get("technical_status")
        else None,
        reason,
    )
    publication = publication_status_for(
        candidate.rights_status,
        technical,
        entity_kind="live",
        media_verified=False,
        failure_reasons=(reason,),
    )
    successes, failures, last_success = _history(technical, previous, checked_at)
    return LiveProbeResult(
        candidate=candidate,
        technical_status=technical,
        publication_status=publication,
        media=first,
        consecutive_successes=successes,
        consecutive_failures=failures,
        last_success_at=last_success,
        failure_reason=reason,
        response_ms_history=_next_response_history(
            previous, first.response_ms, successful=False
        ),
    )


def _response_rank_ms(result: LiveProbeResult) -> float:
    history = tuple(
        value
        for raw in result.response_ms_history[-MAX_SUCCESSFUL_RESPONSE_SAMPLES:]
        if (value := _valid_response_ms(raw)) is not None
    )
    if not history:
        current = _valid_response_ms(result.media.response_ms)
        if current is None:
            return float("inf")
        history = (current,)
    return float(median(history))


def _rank_key(result: LiveProbeResult) -> tuple[int, int, float, int, bytes]:
    return (
        -result.consecutive_successes,
        -result.media.media_path_score,
        _response_rank_ms(result),
        -quality_score(result.media),
        result.candidate.normalized_url.encode("utf-8"),
    )


def deduplicate_final_urls(
    results: Sequence[LiveProbeResult],
) -> tuple[LiveProbeResult, ...]:
    """Withhold duplicate publishable candidates that resolve to one media URL.

    Redirect targets are the client-visible identity, so this happens before
    channel selection, health generation, and availability-gate counting.  A
    deterministic winner remains publishable; every other observation remains
    in health as an explicit ``withheld`` technical fact.
    """

    by_final_url: dict[str, list[LiveProbeResult]] = defaultdict(list)
    for result in results:
        if (
            result.technical_status is TechnicalStatus.HEALTHY
            and result.publication_status is PublicationStatus.STABLE
            and result.media.ok
            and result.media.media_path_score in {1, 2}
        ):
            try:
                final_url = normalized_media_final_url(result.media)
            except ContractError:
                # Selection does not grant publication. The generator and sealed
                # bundle still reject this URL; retaining a raw key here keeps
                # health construction diagnostic for malformed injected facts.
                final_url = result.media.final_url or ""
            by_final_url[final_url].append(result)

    retained_ids: set[str] = set()
    for duplicates in by_final_url.values():
        winner = min(
            duplicates,
            key=lambda item: (
                _rank_key(item),
                channel_identity(item.candidate)[0].encode("utf-8"),
                item.candidate.source_id.encode("utf-8"),
            ),
        )
        retained_ids.add(live_url_id(winner.candidate))

    output: list[LiveProbeResult] = []
    for result in results:
        is_publishable = (
            result.technical_status is TechnicalStatus.HEALTHY
            and result.publication_status is PublicationStatus.STABLE
            and result.media.ok
            and result.media.media_path_score in {1, 2}
        )
        if is_publishable and live_url_id(result.candidate) not in retained_ids:
            output.append(replace(result, publication_status=PublicationStatus.WITHHELD))
        else:
            output.append(result)
    return tuple(output)


def select_channels(results: Sequence[LiveProbeResult]) -> tuple[SelectedChannel, ...]:
    """Merge by global identity and select one stable, media-verified URL."""

    grouped: dict[str, list[LiveProbeResult]] = defaultdict(list)
    identities: dict[str, tuple[str, str]] = {}
    for result in deduplicate_final_urls(results):
        channel_id, basis, normalized = channel_identity(result.candidate)
        grouped[channel_id].append(result)
        identities[channel_id] = (basis, normalized)

    selected: list[SelectedChannel] = []
    for channel_id in sorted(grouped):
        candidates = tuple(
            sorted(grouped[channel_id], key=lambda item: live_url_id(item.candidate))
        )
        eligible = [
            item
            for item in candidates
            if item.technical_status is TechnicalStatus.HEALTHY
            and item.publication_status is PublicationStatus.STABLE
            and item.media.ok
            and item.media.media_path_score in {1, 2}
        ]
        if not eligible:
            continue
        winner = min(eligible, key=_rank_key)
        basis, normalized = identities[channel_id]
        selected.append(
            SelectedChannel(
                channel_id=channel_id,
                identity_basis=basis,
                normalized_identity=normalized,
                selected=winner,
                candidates=candidates,
            )
        )
    return tuple(selected)
