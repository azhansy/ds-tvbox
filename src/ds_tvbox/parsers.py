"""Pure, non-executing parsers for the data formats admitted by SPEC 1.0.3."""

from __future__ import annotations

import json
import re
import shlex
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlsplit

import json5
from defusedxml import ElementTree as DefusedElementTree  # type: ignore[import-untyped]

from .errors import ContractError, SecurityError
from .models import DeclaredHeaders, FailureReason
from .security import (
    merge_declared_headers,
    split_url_headers,
    validate_declared_headers,
)


@dataclass(frozen=True)
class ParseIssue:
    index: int | None
    failure_reason: FailureReason
    message: str


@dataclass(frozen=True)
class ParsedTvboxSite:
    key: str
    name: str
    type: int
    api: str
    searchable: int | None
    quick_search: int | None
    filterable: int | None
    changeable: int | None
    categories: tuple[str, ...]
    declared_headers: DeclaredHeaders | None
    raw: Mapping[str, Any] = field(repr=False, compare=False)


@dataclass(frozen=True)
class ParsedTvboxLive:
    name: str
    type: int
    url: str
    raw: Mapping[str, Any] = field(repr=False, compare=False)


@dataclass(frozen=True)
class ParsedDepotEntry:
    name: str
    url: str


@dataclass(frozen=True)
class ParsedStorehouseEntry:
    source_name: str
    source_url: str


@dataclass(frozen=True)
class TvboxConfig:
    sites: tuple[ParsedTvboxSite, ...]
    lives: tuple[ParsedTvboxLive, ...]
    urls: tuple[ParsedDepotEntry, ...]
    storehouses: tuple[ParsedStorehouseEntry, ...]
    issues: tuple[ParseIssue, ...]
    has_spider: bool
    raw: Mapping[str, Any] = field(repr=False, compare=False)


@dataclass(frozen=True)
class ParsedLiveEntry:
    name: str
    url: str
    tvg_id: str | None = None
    group: str | None = None
    logo: str | None = None
    epg: str | None = None
    declared_headers: DeclaredHeaders | None = None


@dataclass(frozen=True)
class LivePlaylist:
    entries: tuple[ParsedLiveEntry, ...]
    issues: tuple[ParseIssue, ...]


@dataclass(frozen=True)
class HlsVariant:
    uri: str
    width: int | None
    height: int | None
    bandwidth: int | None
    declared_headers: DeclaredHeaders | None = None


@dataclass(frozen=True)
class HlsSegment:
    uri: str
    declared_headers: DeclaredHeaders | None = None


@dataclass(frozen=True)
class HlsPlaylist:
    kind: str
    variants: tuple[HlsVariant, ...]
    segments: tuple[HlsSegment, ...]
    initialization_map: HlsSegment | None = None


@dataclass(frozen=True)
class MacCmsClass:
    type_id: str
    type_name: str


@dataclass(frozen=True)
class MacCmsEpisode:
    title: str
    url: str


@dataclass(frozen=True)
class MacCmsPlayLine:
    name: str
    episodes: tuple[MacCmsEpisode, ...]


@dataclass(frozen=True)
class MacCmsVideo:
    vod_id: str
    vod_name: str
    play_lines: tuple[MacCmsPlayLine, ...] = ()


@dataclass(frozen=True)
class MacCmsResponse:
    classes: tuple[MacCmsClass, ...]
    videos: tuple[MacCmsVideo, ...]

    def detail(self, requested_id: str) -> MacCmsVideo:
        matches = [video for video in self.videos if video.vod_id == str(requested_id)]
        if len(matches) != 1:
            raise ContractError("detail response must locate exactly one requested vod_id")
        detail = matches[0]
        if not detail.play_lines:
            raise ContractError("detail response has no valid playback lines")
        return detail


def _decode_utf8(data: bytes | str, *, format_name: str) -> str:
    if isinstance(data, str):
        return data.lstrip("\ufeff")
    try:
        return data.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError as exc:
        raise ContractError(f"{format_name} must be UTF-8") from exc


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ContractError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def parse_json_data(data: bytes | str) -> Any:
    text = _decode_utf8(data, format_name="JSON")
    try:
        return json.loads(text, object_pairs_hook=_unique_object)
    except ContractError:
        raise
    except (ValueError, TypeError) as exc:
        raise ContractError("invalid JSON") from exc


def parse_json5_data(data: bytes | str) -> Any:
    text = _decode_utf8(data, format_name="JSON5")
    try:
        return json5.loads(text, allow_duplicate_keys=False)
    except (ValueError, TypeError) as exc:
        raise ContractError("invalid JSON5") from exc


def _absolute(value: str, base_url: str | None) -> str:
    return urljoin(base_url, value) if base_url is not None else value


def _required_string(value: Mapping[str, Any], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item.strip():
        raise ContractError(f"{key} must be a non-empty string")
    return item.strip()


def _optional_binary(value: Mapping[str, Any], key: str) -> int | None:
    item = value.get(key)
    if item is None:
        return None
    if type(item) is not int or item not in {0, 1}:
        raise ContractError(f"{key} must be 0 or 1")
    return item


def _array(root: Mapping[str, Any], key: str) -> Sequence[Any]:
    value = root.get(key, [])
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ContractError(f"TVBox {key} must be an array")
    return value


def _header_failure_reason(exc: SecurityError) -> FailureReason:
    lowered = str(exc).lower()
    if any(word in lowered for word in ("malformed", "syntax", "control", "json", "duplicate")):
        return FailureReason.INVALID_HEADER_SYNTAX
    return FailureReason.CREDENTIAL_HEADER_REJECTED


def parse_tvbox_config(
    data: bytes | str,
    *,
    json5_mode: bool = False,
    base_url: str | None = None,
) -> TvboxConfig:
    """Parse a TVBox config without executing extensions or fetching dependencies.

    Invalid independent site/live entries are retained as issues while other
    entries remain available.  Duplicate site keys invalidate the whole config,
    as required by SPEC 10.3.
    """

    value = parse_json5_data(data) if json5_mode else parse_json_data(data)
    if not isinstance(value, Mapping):
        raise ContractError("TVBox config root must be an object")
    sites: list[ParsedTvboxSite] = []
    lives: list[ParsedTvboxLive] = []
    urls: list[ParsedDepotEntry] = []
    stores: list[ParsedStorehouseEntry] = []
    issues: list[ParseIssue] = []
    seen_keys: set[str] = set()

    for index, item in enumerate(_array(value, "sites")):
        if not isinstance(item, Mapping):
            issues.append(
                ParseIssue(index, FailureReason.SCHEMA_INCOMPATIBLE, "site is not an object")
            )
            continue
        raw_key = item.get("key")
        if isinstance(raw_key, str) and raw_key in seen_keys:
            raise ContractError(f"duplicate TVBox site key: {raw_key}")
        if isinstance(raw_key, str):
            seen_keys.add(raw_key)
        try:
            key = _required_string(item, "key")
            name = _required_string(item, "name")
            site_type = item.get("type")
            if type(site_type) is not int or site_type not in {0, 1, 3, 4}:
                raise ContractError("site type must be one of 0, 1, 3, 4")
            api = _absolute(_required_string(item, "api"), base_url)
            categories_raw = item.get("categories", [])
            if not isinstance(categories_raw, list) or any(
                not isinstance(category, str) or not category.strip() for category in categories_raw
            ):
                raise ContractError("categories must be an array of non-empty strings")
            declared = None
            if item.get("header") is not None:
                declared = validate_declared_headers(item["header"])
            sites.append(
                ParsedTvboxSite(
                    key=key,
                    name=name,
                    type=site_type,
                    api=api,
                    searchable=_optional_binary(item, "searchable"),
                    quick_search=_optional_binary(item, "quickSearch"),
                    filterable=_optional_binary(item, "filterable"),
                    changeable=_optional_binary(item, "changeable"),
                    categories=tuple(category.strip() for category in categories_raw),
                    declared_headers=declared,
                    raw=dict(item),
                )
            )
        except SecurityError as exc:
            issues.append(ParseIssue(index, _header_failure_reason(exc), str(exc)))
        except ContractError as exc:
            issues.append(ParseIssue(index, FailureReason.SCHEMA_INCOMPATIBLE, str(exc)))

    for index, item in enumerate(_array(value, "lives")):
        if not isinstance(item, Mapping):
            issues.append(
                ParseIssue(index, FailureReason.SCHEMA_INCOMPATIBLE, "live is not an object")
            )
            continue
        try:
            live_type = item.get("type")
            if type(live_type) is not int:
                raise ContractError("live type must be an integer")
            lives.append(
                ParsedTvboxLive(
                    name=_required_string(item, "name"),
                    type=live_type,
                    url=_absolute(_required_string(item, "url"), base_url),
                    raw=dict(item),
                )
            )
        except ContractError as exc:
            issues.append(ParseIssue(index, FailureReason.SCHEMA_INCOMPATIBLE, str(exc)))

    for index, item in enumerate(_array(value, "urls")):
        if not isinstance(item, Mapping):
            issues.append(
                ParseIssue(index, FailureReason.SCHEMA_INCOMPATIBLE, "depot entry is not an object")
            )
            continue
        try:
            urls.append(
                ParsedDepotEntry(
                    name=_required_string(item, "name"),
                    url=_absolute(_required_string(item, "url"), base_url),
                )
            )
        except ContractError as exc:
            issues.append(ParseIssue(index, FailureReason.SCHEMA_INCOMPATIBLE, str(exc)))

    for index, item in enumerate(_array(value, "storeHouse")):
        if not isinstance(item, Mapping):
            issues.append(
                ParseIssue(
                    index, FailureReason.SCHEMA_INCOMPATIBLE, "storeHouse entry is not an object"
                )
            )
            continue
        try:
            stores.append(
                ParsedStorehouseEntry(
                    source_name=_required_string(item, "sourceName"),
                    source_url=_absolute(_required_string(item, "sourceUrl"), base_url),
                )
            )
        except ContractError as exc:
            issues.append(ParseIssue(index, FailureReason.SCHEMA_INCOMPATIBLE, str(exc)))

    return TvboxConfig(
        sites=tuple(sites),
        lives=tuple(lives),
        urls=tuple(urls),
        storehouses=tuple(stores),
        issues=tuple(issues),
        has_spider="spider" in value
        and value.get("spider") is not None
        and value.get("spider") != "",
        raw=dict(value),
    )


def _split_unquoted(value: str, delimiter: str) -> tuple[str, str]:
    quoted = False
    escaped = False
    for index, char in enumerate(value):
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == '"':
            quoted = not quoted
        elif char == delimiter and not quoted:
            return value[:index], value[index + 1 :]
    raise ContractError(f"missing unquoted {delimiter!r} separator")


def _attribute_tokens(value: str, *, comma_delimited: bool = False) -> dict[str, str]:
    lexer = shlex.shlex(value, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    if comma_delimited:
        lexer.whitespace = ","
    tokens = list(lexer)
    result: dict[str, str] = {}
    for token in tokens:
        if "=" not in token:
            continue
        key, item = token.split("=", 1)
        key = key.strip().lower()
        if not key or key in result:
            raise ContractError("duplicate or empty playlist attribute")
        result[key] = item.strip()
    return result


def _merge_header_or_error(
    current: DeclaredHeaders | None,
    addition: DeclaredHeaders | None,
) -> DeclaredHeaders | None:
    return merge_declared_headers(current, addition)


def parse_m3u(data: bytes | str, *, base_url: str | None = None) -> LivePlaylist:
    """Parse a channel M3U, including all Header declaration syntaxes."""

    text = _decode_utf8(data, format_name="M3U")
    lines = [line.strip() for line in text.splitlines()]
    first = next((line for line in lines if line), "")
    if first != "#EXTM3U":
        raise ContractError("M3U must start with #EXTM3U")
    entries: list[ParsedLiveEntry] = []
    issues: list[ParseIssue] = []
    metadata: tuple[str, dict[str, str]] | None = None
    declared: DeclaredHeaders | None = None
    current_error: SecurityError | ContractError | None = None
    item_index = -1
    for line in lines[1:]:
        if not line:
            continue
        if line.startswith("#EXTINF:"):
            item_index += 1
            declared = None
            current_error = None
            try:
                prefix, name = _split_unquoted(line[len("#EXTINF:") :], ",")
                parts = prefix.split(maxsplit=1)
                attributes = _attribute_tokens(parts[1]) if len(parts) == 2 else {}
                if not name.strip():
                    raise ContractError("channel name is empty")
                metadata = (name.strip(), attributes)
            except ContractError as exc:
                metadata = None
                current_error = exc
            continue
        if line.startswith("#EXTHTTP:"):
            try:
                addition = validate_declared_headers(line[len("#EXTHTTP:") :].strip())
                declared = _merge_header_or_error(declared, addition)
            except SecurityError as exc:
                current_error = exc
            continue
        if line.startswith("#EXTVLCOPT:"):
            option = line[len("#EXTVLCOPT:") :]
            try:
                if "=" not in option:
                    raise SecurityError("malformed #EXTVLCOPT")
                key, option_value = option.split("=", 1)
                header_name = {
                    "http-referrer": "Referer",
                    "http-user-agent": "User-Agent",
                }.get(key.strip().lower())
                if header_name is None:
                    raise SecurityError("unknown #EXTVLCOPT is not allowed")
                addition = validate_declared_headers({header_name: option_value})
                declared = _merge_header_or_error(declared, addition)
            except SecurityError as exc:
                current_error = exc
            continue
        if line.startswith("#"):
            continue
        if metadata is None:
            issues.append(
                ParseIssue(
                    item_index if item_index >= 0 else None,
                    FailureReason.SCHEMA_INCOMPATIBLE,
                    "URL has no valid EXTINF",
                )
            )
            continue
        try:
            clean_url, line_headers = split_url_headers(line)
            all_headers = _merge_header_or_error(declared, line_headers)
            if current_error is not None:
                raise current_error
            name, attributes = metadata
            entries.append(
                ParsedLiveEntry(
                    name=name,
                    url=_absolute(clean_url, base_url),
                    tvg_id=attributes.get("tvg-id") or None,
                    group=attributes.get("group-title") or None,
                    logo=attributes.get("tvg-logo") or None,
                    epg=attributes.get("tvg-url") or attributes.get("url-tvg") or None,
                    declared_headers=all_headers,
                )
            )
        except (SecurityError, ContractError) as exc:
            reason = (
                _header_failure_reason(exc)
                if isinstance(exc, SecurityError)
                else FailureReason.SCHEMA_INCOMPATIBLE
            )
            issues.append(ParseIssue(item_index, reason, str(exc)))
        finally:
            metadata = None
            declared = None
            current_error = None
    if metadata is not None:
        issues.append(
            ParseIssue(item_index, FailureReason.SCHEMA_INCOMPATIBLE, "EXTINF has no URL")
        )
    return LivePlaylist(tuple(entries), tuple(issues))


def parse_txt_live(data: bytes | str, *, base_url: str | None = None) -> LivePlaylist:
    """Parse common ``name,url`` / ``group,#genre#`` live text lists."""

    text = _decode_utf8(data, format_name="TXT live playlist")
    entries: list[ParsedLiveEntry] = []
    issues: list[ParseIssue] = []
    group: str | None = None
    item_index = 0
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("//") or line.startswith("#"):
            continue
        if "," not in line:
            issues.append(
                ParseIssue(item_index, FailureReason.SCHEMA_INCOMPATIBLE, "TXT entry has no comma")
            )
            item_index += 1
            continue
        name, raw_url = (part.strip() for part in line.split(",", 1))
        if raw_url.lower() == "#genre#":
            if not name:
                issues.append(
                    ParseIssue(item_index, FailureReason.SCHEMA_INCOMPATIBLE, "group name is empty")
                )
            else:
                group = name
            item_index += 1
            continue
        try:
            if not name or not raw_url:
                raise ContractError("TXT channel name and URL must be non-empty")
            clean_url, headers = split_url_headers(raw_url)
            entries.append(
                ParsedLiveEntry(
                    name=name,
                    url=_absolute(clean_url, base_url),
                    group=group,
                    declared_headers=headers,
                )
            )
        except (ContractError, SecurityError) as exc:
            reason = (
                _header_failure_reason(exc)
                if isinstance(exc, SecurityError)
                else FailureReason.SCHEMA_INCOMPATIBLE
            )
            issues.append(ParseIssue(item_index, reason, str(exc)))
        item_index += 1
    return LivePlaylist(tuple(entries), tuple(issues))


def _hls_attributes(value: str) -> dict[str, str]:
    try:
        return _attribute_tokens(value, comma_delimited=True)
    except ValueError as exc:
        raise ContractError("invalid HLS attribute list") from exc


def _positive_int(value: str | None, *, field_name: str) -> int | None:
    if value is None:
        return None
    try:
        result = int(value)
    except ValueError as exc:
        raise ContractError(f"invalid HLS {field_name}") from exc
    if result <= 0:
        raise ContractError(f"invalid HLS {field_name}")
    return result


def _hls_resource(raw_uri: str, base_url: str | None) -> HlsSegment:
    uri, headers = split_url_headers(raw_uri.strip())
    if not uri:
        raise ContractError("empty HLS URI")
    return HlsSegment(_absolute(uri, base_url), headers)


def parse_hls(data: bytes | str, *, base_url: str | None = None) -> HlsPlaylist:
    """Parse a structurally valid HLS master or media playlist."""

    text = _decode_utf8(data, format_name="HLS")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines or lines[0] != "#EXTM3U":
        raise ContractError("HLS must start with #EXTM3U")
    variants: list[HlsVariant] = []
    segments: list[HlsSegment] = []
    initialization_map: HlsSegment | None = None
    pending_variant: dict[str, str] | None = None
    saw_hls_tag = False
    for line in lines[1:]:
        if line.startswith("#EXT-X-"):
            saw_hls_tag = True
        if line.startswith("#EXT-X-STREAM-INF:"):
            if pending_variant is not None:
                raise ContractError("HLS variant has no URI")
            pending_variant = _hls_attributes(line.split(":", 1)[1])
            continue
        if line.startswith("#EXTINF:"):
            continue
        if line.startswith("#EXT-X-MAP:"):
            attributes = _hls_attributes(line.split(":", 1)[1])
            uri = attributes.get("uri")
            if not uri:
                raise ContractError("HLS EXT-X-MAP has no URI")
            initialization_map = _hls_resource(uri, base_url)
            continue
        if line.startswith("#"):
            continue
        resource = _hls_resource(line, base_url)
        if pending_variant is not None:
            resolution = pending_variant.get("resolution")
            width: int | None = None
            height: int | None = None
            if resolution is not None:
                match = re.fullmatch(r"([1-9][0-9]*)[xX]([1-9][0-9]*)", resolution)
                if match is None:
                    raise ContractError("invalid HLS RESOLUTION")
                width, height = int(match.group(1)), int(match.group(2))
            variants.append(
                HlsVariant(
                    uri=resource.uri,
                    width=width,
                    height=height,
                    bandwidth=_positive_int(
                        pending_variant.get("bandwidth"), field_name="BANDWIDTH"
                    ),
                    declared_headers=resource.declared_headers,
                )
            )
            pending_variant = None
        else:
            # Some valid media playlists omit EXTINF for initialization/parts;
            # a non-comment URI is still a media resource.
            segments.append(resource)
    if pending_variant is not None:
        raise ContractError("HLS variant has no URI")
    if not saw_hls_tag or (not variants and not segments):
        raise ContractError("HLS has no playable master/media structure")
    if variants and segments:
        raise ContractError("HLS cannot mix master variants and media segments")
    kind = "master" if variants else "media"
    return HlsPlaylist(kind, tuple(variants), tuple(segments), initialization_map)


def _string_id(value: Any, *, field_name: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ContractError(f"{field_name} must be a string/integer identifier")
    result = str(value).strip()
    if not result:
        raise ContractError(f"{field_name} must be non-empty")
    return result


def _play_lines(play_from: Any, play_url: Any) -> tuple[MacCmsPlayLine, ...]:
    if (play_from is None or play_from == "") and (play_url is None or play_url == ""):
        return ()
    if not isinstance(play_from, str) or not isinstance(play_url, str):
        raise ContractError("playback source and URL fields must both be strings")
    names = play_from.split("$$$")
    lines = play_url.split("$$$")
    if len(names) != len(lines) or not names:
        raise ContractError("playback source/URL line counts do not match")
    result: list[MacCmsPlayLine] = []
    for name, line in zip(names, lines, strict=True):
        if not name.strip() or not line.strip():
            raise ContractError("playback line is empty")
        raw_episodes = line.split("#")
        while raw_episodes and not raw_episodes[-1].strip():
            raw_episodes.pop()
        if not raw_episodes:
            raise ContractError("playback line is empty")
        episodes: list[MacCmsEpisode] = []
        for raw_episode in raw_episodes:
            if "$" not in raw_episode:
                raise ContractError("playback episode has no title/URL separator")
            title, url = raw_episode.split("$", 1)
            if not url.strip():
                raise ContractError("playback episode URL is empty")
            if urlsplit(url.strip()).scheme.lower() not in {"http", "https"}:
                raise ContractError("playback URL has an invalid scheme")
            episodes.append(MacCmsEpisode(title.strip(), url.strip()))
        result.append(MacCmsPlayLine(name.strip(), tuple(episodes)))
    return tuple(result)


def parse_maccms_json(data: bytes | str) -> MacCmsResponse:
    """Parse MacCMS JSON home/search/detail data into one canonical model."""

    value = parse_json_data(data)
    if not isinstance(value, Mapping):
        raise ContractError("MacCMS JSON root must be an object")
    raw_classes = value.get("class", [])
    raw_videos = value.get("list", [])
    if not isinstance(raw_classes, list) or not isinstance(raw_videos, list):
        raise ContractError("MacCMS class/list must be arrays")
    classes: list[MacCmsClass] = []
    for item in raw_classes:
        if not isinstance(item, Mapping):
            raise ContractError("MacCMS class entry must be an object")
        classes.append(
            MacCmsClass(
                _string_id(item.get("type_id"), field_name="type_id"),
                _required_string(item, "type_name"),
            )
        )
    videos: list[MacCmsVideo] = []
    for item in raw_videos:
        if not isinstance(item, Mapping):
            raise ContractError("MacCMS video entry must be an object")
        videos.append(
            MacCmsVideo(
                vod_id=_string_id(item.get("vod_id"), field_name="vod_id"),
                vod_name=_required_string(item, "vod_name"),
                play_lines=_play_lines(item.get("vod_play_from"), item.get("vod_play_url")),
            )
        )
    return MacCmsResponse(tuple(classes), tuple(videos))


def _child_text(element: Any, name: str) -> str | None:
    for child in list(element):
        if str(child.tag).split("}")[-1] == name:
            text = child.text
            return text if isinstance(text, str) else None
    return None


def _children(element: Any, name: str) -> list[Any]:
    return [child for child in list(element) if str(child.tag).split("}")[-1] == name]


def parse_maccms_xml(data: bytes | str) -> MacCmsResponse:
    """Parse MacCMS XML with external entities and dangerous XML features disabled."""

    raw = data.encode("utf-8") if isinstance(data, str) else data
    try:
        root = DefusedElementTree.fromstring(raw)
    except Exception as exc:
        raise ContractError("invalid or unsafe MacCMS XML") from exc
    if str(root.tag).split("}")[-1] != "rss":
        raise ContractError("MacCMS XML root must be rss")
    classes: list[MacCmsClass] = []
    videos: list[MacCmsVideo] = []
    for class_container in _children(root, "class"):
        for item in _children(class_container, "ty"):
            classes.append(
                MacCmsClass(
                    _string_id(item.attrib.get("id"), field_name="class ty id"),
                    (item.text or "").strip(),
                )
            )
            if not classes[-1].type_name:
                raise ContractError("MacCMS XML class name is empty")
    for list_container in _children(root, "list"):
        for item in _children(list_container, "video"):
            vod_id = _string_id(_child_text(item, "id"), field_name="video id")
            vod_name = (_child_text(item, "name") or "").strip()
            if not vod_name:
                raise ContractError("MacCMS XML video name is empty")
            names: list[str] = []
            urls: list[str] = []
            for dl in _children(item, "dl"):
                for dd in _children(dl, "dd"):
                    names.append((dd.attrib.get("flag") or "").strip())
                    urls.append((dd.text or "").strip())
            play_lines = _play_lines("$$$".join(names), "$$$".join(urls)) if names else ()
            videos.append(MacCmsVideo(vod_id, vod_name, play_lines))
    return MacCmsResponse(tuple(classes), tuple(videos))
