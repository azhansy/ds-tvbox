"""Independent validation used by both collector and privileged publisher."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from typing import Any
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator, FormatChecker  # type: ignore[import-untyped]

from ds_tvbox.errors import ContractError, SecurityError
from ds_tvbox.security import normalize_client_url_offline
from ds_tvbox.serialization import sha256_file

_EXECUTABLE_SUFFIX = re.compile(r"(?i)\.(?:jar|js|py|dex|so)(?:[?#]|$)")
_FORBIDDEN_KEYS = {"spider", "jar", "ext", "header", "headers", "rules"}
_SHA256_VALUE = re.compile(r"^sha256:([0-9a-f]{64})$")
_RELEASE_ID = re.compile(r"^g[0-9]{8}$")
_TRUSTED_OWNER = "azhansy"
_TRUSTED_REPOSITORY = "ds-tvbox"
_TRUSTED_GENERATED_REF = "generated"
_SOURCE_ID = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_MAX_LIVE_NAME = 512
_MAX_LIVE_TEXT = 1024
_MAX_LIVE_URL = 8192


@dataclass(frozen=True)
class HealthValidationResult:
    """Stable identities recovered from a schema-valid health graph."""

    source_ids: frozenset[str]
    vod_entity_ids: frozenset[str]
    live_entity_ids: frozenset[str]
    channel_ids: frozenset[str]
    selected_live_entity_ids: frozenset[str]
    canonical_m3u: bytes


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ContractError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text("utf-8"), object_pairs_hook=_unique_object)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ContractError(f"invalid JSON {path}: {error}") from error


def validate_schema(instance: Any, schema_path: Path) -> None:
    schema = load_json(schema_path)
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(instance), key=lambda item: list(item.absolute_path))
    if errors:
        rendered = "; ".join(
            f"{'/'.join(str(part) for part in error.absolute_path) or '<root>'}: {error.message}"
            for error in errors[:20]
        )
        raise ContractError(f"schema validation failed ({schema_path.name}): {rendered}")


def assert_public_https_url(value: str) -> None:
    try:
        normalize_client_url_offline(value)
    except SecurityError as error:
        raise SecurityError(
            "client URL is not credential-free HTTPS public URL: "
            f"{redact_url(value)} ({error})"
        ) from error


def client_vod_entity_id(source_id: str, site_type: int, api: str) -> str:
    """Recompute the stable VOD identity from client-visible normalized facts."""

    if not _SOURCE_ID.fullmatch(source_id):
        raise ContractError(f"invalid VOD source_id: {source_id!r}")
    if site_type not in {0, 1, 4} or isinstance(site_type, bool):
        raise ContractError(f"invalid VOD site type: {site_type!r}")
    try:
        normalized_api = normalize_client_url_offline(api).value
    except SecurityError as error:
        raise SecurityError("VOD site API is not a safe public HTTPS URL") from error
    fingerprint = hashlib.sha256(f"{site_type}{normalized_api}".encode()).hexdigest()[:16]
    return f"vod:{source_id}:{fingerprint}"


def redact_url(value: str) -> str:
    parts = urlsplit(value)
    if not parts.scheme or not parts.netloc:
        return "<invalid-url>"
    return f"{parts.scheme}://{parts.hostname or '<host>'}{parts.path}"


def scan_client_value(value: Any, path: tuple[str | int, ...] = ()) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            normalized = str(key).casefold()
            if normalized in _FORBIDDEN_KEYS:
                raise SecurityError(f"forbidden client field at {path + (key,)}")
            if normalized == "type" and nested == 3:
                raise SecurityError(f"type 3 client source at {path + (key,)}")
            scan_client_value(nested, path + (key,))
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            scan_client_value(nested, path + (index,))
    elif isinstance(value, str) and _EXECUTABLE_SUFFIX.search(value):
        raise SecurityError(f"executable dependency at {path}")


def validate_client_json(document: Mapping[str, Any], schema_root: Path) -> None:
    validate_schema(document, schema_root / "tvbox-config.schema.json")
    scan_client_value(document)
    for site in document["sites"]:
        assert_public_https_url(site["api"])
    for live in document["lives"]:
        assert_public_https_url(live["url"])


def validate_m3u(data: bytes) -> tuple[str, ...]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ContractError("M3U must be UTF-8") from error
    if "\r" in text or not text.startswith("#EXTM3U\n"):
        raise ContractError("M3U must use LF and start with #EXTM3U")
    forbidden = ("#EXTHTTP", "#EXTVLCOPT", "|Header=", "|header=")
    if any(marker.casefold() in text.casefold() for marker in forbidden):
        raise SecurityError("M3U contains client header instructions")
    urls: list[str] = []
    for line in text.splitlines():
        if line and not line.startswith("#"):
            assert_public_https_url(line)
            urls.append(line)
    if len(urls) != len(set(urls)):
        raise ContractError("M3U contains duplicate playback URLs")
    return tuple(urls)


def _live_url_entity_id(source_id: str, normalized_url: str) -> str:
    digest = hashlib.sha256(normalized_url.encode()).hexdigest()[:16]
    return f"live-url:{source_id}:{digest}"


def _normalized_tvg_id(value: str) -> str:
    return unicodedata.normalize("NFKC", value).strip().translate(
        str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
    )


def _normalized_channel_name(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).strip().split())


def _channel_identity(
    source_id: str, name: str, tvg_id: str | None
) -> tuple[str, str, str]:
    normalized_tvg = _normalized_tvg_id(tvg_id or "")
    if normalized_tvg:
        basis = "tvg_id"
        normalized_identity = normalized_tvg
        material = f"tvg:{normalized_tvg}"
    else:
        normalized_name = _normalized_channel_name(name)
        if not normalized_name:
            raise ContractError("health live URL has no usable channel identity")
        basis = "source_name"
        normalized_identity = f"{source_id}:{normalized_name}"
        material = f"local:{source_id}:{normalized_name}"
    digest = hashlib.sha256(material.encode()).hexdigest()[:16]
    return f"channel:{digest}", basis, normalized_identity


def _aggregate_status(values: Sequence[str], order: Sequence[str], *, empty: str) -> str:
    if not values:
        return empty
    return next(status for status in order if status in values)


def _live_quality_score(item: Mapping[str, Any]) -> int:
    width = item["width"]
    height = item["height"]
    bandwidth = item["bandwidth"]
    if width is None or height is None or bandwidth is None:
        return 1
    if width >= 1920 and height >= 1080 and bandwidth >= 3_000_000:
        return 4
    if width >= 1280 and height >= 720 and bandwidth >= 1_500_000:
        return 3
    if width >= 640 and height >= 360 and bandwidth >= 500_000:
        return 2
    return 0


def _live_rank(item: Mapping[str, Any]) -> tuple[int, int, float, int, bytes]:
    response_history = tuple(int(value) for value in item["response_ms_history"])
    if response_history:
        response_rank = float(median(response_history[-7:]))
    elif item["response_ms"] is not None:
        response_rank = float(item["response_ms"])
    else:
        response_rank = float("inf")
    return (
        -int(item["consecutive_successes"]),
        -int(item["media_path_score"]),
        response_rank,
        -_live_quality_score(item),
        str(item["normalized_url"]).encode(),
    )


def _single_line_text(
    value: str,
    *,
    label: str,
    allow_empty: bool = False,
    max_length: int = _MAX_LIVE_TEXT,
) -> str:
    stripped = value.strip()
    if len(value) > max_length or (not allow_empty and not stripped) or any(
        character in stripped for character in ("\r", "\n", "\x00")
    ):
        raise ContractError(f"{label} must be safe single-line text")
    return stripped


def _normalized_live_url(value: str, *, label: str) -> str:
    if len(value) > _MAX_LIVE_URL:
        raise ContractError(f"{label} exceeds the maximum URL length")
    return normalize_client_url_offline(value).value


def _warning_name(name: str, rights_status: str) -> str:
    clean = _single_line_text(
        name,
        label="health live name",
        max_length=_MAX_LIVE_NAME,
    )
    while clean.startswith("⚠️"):
        clean = clean.removeprefix("⚠️").lstrip()
    if not clean:
        raise ContractError("health live name cannot consist only of warning markers")
    return f"⚠️ {clean}" if rights_status == "public_unverified" else clean


def _escape_m3u_attribute(value: str) -> str:
    return value.replace("&", "&amp;").replace('"', "&quot;")


def _canonical_health_m3u(
    selected: Sequence[
        tuple[Mapping[str, Any], Mapping[str, Any], str, str, str]
    ],
) -> bytes:
    ordered = sorted(
        selected,
        key=lambda entry: (
            str(entry[0]["normalized_identity"]).encode(),
            entry[2].encode(),
            str(entry[1]["name"]).encode(),
            entry[3].encode(),
        ),
    )
    lines = ["#EXTM3U"]
    for channel, item, source_id, final_url, rights_status in ordered:
        _ = channel, source_id
        name = _warning_name(str(item["name"]), rights_status)
        attributes: list[tuple[str, str]] = []
        tvg_id = item["tvg_id"]
        if isinstance(tvg_id, str) and tvg_id.strip():
            attributes.append(
                ("tvg-id", _single_line_text(tvg_id, label="health live tvg_id"))
            )
        logo = item["logo"]
        if isinstance(logo, str):
            attributes.append(("tvg-logo", logo))
        epg = item["epg"]
        if isinstance(epg, str):
            attributes.append(("tvg-url", epg))
        group = item["group"]
        if isinstance(group, str) and group.strip():
            attributes.append(
                ("group-title", _single_line_text(group, label="health live group"))
            )
        suffix = "".join(
            f' {key}="{_escape_m3u_attribute(value)}"' for key, value in attributes
        )
        lines.extend((f"#EXTINF:-1{suffix},{name}", final_url))
    return ("\n".join(lines) + "\n").encode()


def validate_health_document(
    health: Mapping[str, Any],
    schema_root: Path,
    *,
    m3u_urls: Sequence[str] = (),
) -> HealthValidationResult:
    """Validate the health schema and its cross-entity/channel relationships."""

    validate_schema(health, schema_root / "health.schema.json")
    sources = health["sources"]
    channels = health["channels"]
    assert isinstance(sources, list) and isinstance(channels, list)

    source_ids: set[str] = set()
    all_entity_ids: set[str] = set()
    vod_entity_ids: set[str] = set()
    live_items: dict[str, Mapping[str, Any]] = {}
    live_item_sources: dict[str, str] = {}
    live_item_rights: dict[str, str] = {}
    live_channel_facts: dict[str, tuple[str, str]] = {}
    normalized_final_urls: dict[str, str] = {}
    live_ids_by_channel: dict[str, set[str]] = {}
    source_order: list[str] = []
    for source in sources:
        assert isinstance(source, Mapping)
        source_id = str(source["source_id"])
        source_entity_id = str(source["entity_id"])
        if source_entity_id != f"source:{source_id}":
            raise ContractError("health source entity_id does not match source_id")
        if source_id in source_ids or source_entity_id in all_entity_ids:
            raise ContractError(f"duplicate health entity: {source_entity_id}")
        source_ids.add(source_id)
        source_order.append(source_id)
        all_entity_ids.add(source_entity_id)
        if source["last_checked_at"] != health["generated_at"]:
            raise ContractError("health source last_checked_at differs from generated_at")
        source_rights = str(source["rights_status"])
        item_order: list[str] = []
        for item in source["items"]:
            assert isinstance(item, Mapping)
            entity_id = str(item["entity_id"])
            if entity_id in all_entity_ids:
                raise ContractError(f"duplicate health entity: {entity_id}")
            all_entity_ids.add(entity_id)
            item_order.append(entity_id)
            entity_type = item["entity_type"]
            expected_prefix = "vod" if entity_type == "vod_site" else "live-url"
            if not entity_id.startswith(f"{expected_prefix}:{source_id}:"):
                raise ContractError("health item entity_id does not match its source")
            if entity_type == "vod_site":
                vod_entity_ids.add(entity_id)
                continue
            channel_id = str(item["channel_id"])
            live_items[entity_id] = item
            live_item_sources[entity_id] = source_id
            live_item_rights[entity_id] = source_rights
            normalized_url = item["normalized_url"]
            assert isinstance(normalized_url, str)
            normalized = _normalized_live_url(
                normalized_url,
                label="health live normalized_url",
            )
            if normalized_url != normalized:
                raise ContractError("health live normalized_url is not canonical")
            if item["protocol"] != urlsplit(normalized).scheme:
                raise ContractError("health live protocol differs from normalized_url")
            if entity_id != _live_url_entity_id(source_id, normalized):
                raise ContractError("health live entity_id differs from normalized_url")
            expected_channel_id, identity_basis, normalized_identity = _channel_identity(
                source_id,
                str(item["name"]),
                str(item["tvg_id"]) if item["tvg_id"] is not None else None,
            )
            if channel_id != expected_channel_id:
                raise ContractError("health live channel_id differs from channel identity")
            channel_facts = (identity_basis, normalized_identity)
            previous_channel_facts = live_channel_facts.setdefault(
                channel_id, channel_facts
            )
            if previous_channel_facts != channel_facts:
                raise ContractError("health channel candidates disagree on identity facts")
            live_ids_by_channel.setdefault(channel_id, set()).add(entity_id)
            final_url = item["final_url"]
            if final_url is not None:
                assert isinstance(final_url, str)
                normalized_final = _normalized_live_url(
                    final_url,
                    label="health live final_url",
                )
                if final_url != normalized_final:
                    raise ContractError("health live final_url is not canonical")
                normalized_final_urls[entity_id] = normalized_final
            for optional_url in ("logo", "epg"):
                value = item[optional_url]
                if value is None:
                    continue
                assert isinstance(value, str)
                normalized_optional = _normalized_live_url(
                    value,
                    label=f"health live {optional_url}",
                )
                if value != normalized_optional:
                    raise ContractError(
                        f"health live {optional_url} is not canonical public HTTPS"
                    )
            _single_line_text(
                str(item["name"]),
                label="health live name",
                max_length=_MAX_LIVE_NAME,
            )
            for optional_text in ("tvg_id", "group"):
                value = item[optional_text]
                if isinstance(value, str) and value:
                    _single_line_text(
                        value,
                        label=f"health live {optional_text}",
                        allow_empty=True,
                    )
        if item_order != sorted(item_order):
            raise ContractError("health source items are not deterministically sorted")
        if source["failure_reason"] is None:
            item_technical = [str(item["technical_status"]) for item in source["items"]]
            expected_technical = _aggregate_status(
                item_technical,
                (
                    "healthy",
                    "partial",
                    "suspect",
                    "unknown",
                    "unsupported_environment",
                    "dead",
                ),
                empty="unknown",
            )
            if source_rights in {"restricted", "takedown"}:
                expected_publication = "rejected"
            else:
                expected_publication = _aggregate_status(
                    [str(item["publication_status"]) for item in source["items"]],
                    ("stable", "experimental", "withheld", "rejected"),
                    empty="withheld",
                )
            if (
                source["technical_status"] != expected_technical
                or source["publication_status"] != expected_publication
            ):
                raise ContractError("health source aggregate status is inconsistent")
    if source_order != sorted(source_order):
        raise ContractError("health sources are not deterministically sorted")

    # Recompute the global final-URL winner before trusting channel selections.
    publishable_rights = {"verified", "open_license", "public_unverified"}
    base_eligible: set[str] = set()
    candidates_by_final: dict[str, list[str]] = {}
    for entity_id, item in live_items.items():
        final_url = normalized_final_urls.get(entity_id)
        healthy_observation = (
            int(item["consecutive_successes"]) > 0
            and int(item["consecutive_failures"]) == 0
            and item["last_success_at"] is not None
            and item["response_ms"] is not None
            and bool(item["response_ms_history"])
            and not item["secondary_reasons"]
        )
        eligible = (
            item["technical_status"] == "healthy"
            and item["media_path_score"] in {1, 2}
            and final_url is not None
            and item["failure_reason"] is None
            and live_item_rights[entity_id] in publishable_rights
            and healthy_observation
        )
        if eligible:
            base_eligible.add(entity_id)
            assert final_url is not None
            candidates_by_final.setdefault(final_url, []).append(entity_id)
        elif item["publication_status"] == "stable":
            raise ContractError("non-playable health live URL claims stable publication")

    globally_retained: set[str] = set()
    for candidates in candidates_by_final.values():
        winner = min(
            candidates,
            key=lambda entity_id: (
                _live_rank(live_items[entity_id]),
                str(live_items[entity_id]["channel_id"]).encode(),
                live_item_sources[entity_id].encode(),
            ),
        )
        globally_retained.add(winner)
    for entity_id in base_eligible:
        expected = "stable" if entity_id in globally_retained else "withheld"
        if live_items[entity_id]["publication_status"] != expected:
            raise ContractError("health live final-URL deduplication winner is inconsistent")

    channel_ids: set[str] = set()
    selected_ids: set[str] = set()
    selected_urls: list[str] = []
    selected_m3u: list[
        tuple[Mapping[str, Any], Mapping[str, Any], str, str, str]
    ] = []
    channel_order: list[str] = []
    for channel in channels:
        assert isinstance(channel, Mapping)
        channel_id = str(channel["entity_id"])
        if channel_id in all_entity_ids or channel_id in channel_ids:
            raise ContractError(f"duplicate health entity: {channel_id}")
        channel_ids.add(channel_id)
        all_entity_ids.add(channel_id)
        channel_order.append(channel_id)
        candidate_ids = [str(item) for item in channel["candidate_url_ids"]]
        if candidate_ids != sorted(candidate_ids):
            raise ContractError("health channel candidate_url_ids are not sorted")
        expected_candidates = live_ids_by_channel.get(channel_id, set())
        if set(candidate_ids) != expected_candidates:
            raise ContractError("health channel has dangling or missing candidate URL IDs")
        facts = live_channel_facts.get(channel_id)
        if facts is None or (
            channel["identity_basis"], channel["normalized_identity"]
        ) != facts:
            raise ContractError("health channel identity facts are inconsistent")
        eligible_ids = [
            entity_id for entity_id in candidate_ids if entity_id in globally_retained
        ]
        expected_selected = (
            min(eligible_ids, key=lambda entity_id: _live_rank(live_items[entity_id]))
            if eligible_ids
            else None
        )
        selected = channel["selected_url_id"]
        if selected != expected_selected:
            raise ContractError("health selected_url_id differs from deterministic winner")
        technical_values = [str(live_items[item]["technical_status"]) for item in candidate_ids]
        expected_technical = _aggregate_status(
            technical_values,
            ("healthy", "partial", "suspect", "unknown", "unsupported_environment", "dead"),
            empty="unknown",
        )
        publication_values = [
            str(live_items[item]["publication_status"]) for item in candidate_ids
        ]
        expected_publication = (
            "stable"
            if expected_selected is not None
            else _aggregate_status(
                publication_values,
                ("stable", "experimental", "withheld", "rejected"),
                empty="withheld",
            )
        )
        candidate_rights = {live_item_rights[item] for item in candidate_ids}
        expected_rights = (
            live_item_rights[expected_selected]
            if expected_selected is not None
            else next(
                rights
                for rights in (
                    "takedown",
                    "restricted",
                    "unknown",
                    "public_unverified",
                    "open_license",
                    "verified",
                )
                if rights in candidate_rights
            )
        )
        if (
            channel["technical_status"] != expected_technical
            or channel["publication_status"] != expected_publication
            or channel["rights_status"] != expected_rights
        ):
            raise ContractError("health channel aggregate status is inconsistent")
        if expected_selected is None:
            continue
        selected_id = expected_selected
        if selected_id in selected_ids:
            raise ContractError("health selected_url_id is duplicated")
        selected_item = live_items[selected_id]
        final_url = normalized_final_urls[selected_id]
        selected_ids.add(selected_id)
        selected_urls.append(final_url)
        selected_m3u.append(
            (
                channel,
                selected_item,
                live_item_sources[selected_id],
                final_url,
                live_item_rights[selected_id],
            )
        )
    if channel_order != sorted(channel_order):
        raise ContractError("health channels are not deterministically sorted")
    if set(live_ids_by_channel).difference(channel_ids):
        raise ContractError("health live URL has a dangling channel_id")
    if len(m3u_urls) != len(selected_urls) or set(m3u_urls) != set(selected_urls):
        raise ContractError("M3U channel URLs differ from selected health URLs")

    return HealthValidationResult(
        source_ids=frozenset(source_ids),
        vod_entity_ids=frozenset(vod_entity_ids),
        live_entity_ids=frozenset(live_items),
        channel_ids=frozenset(channel_ids),
        selected_live_entity_ids=frozenset(selected_ids),
        canonical_m3u=_canonical_health_m3u(selected_m3u),
    )


def _digest_from_manifest(value: str) -> str:
    match = _SHA256_VALUE.fullmatch(value)
    if not match:
        raise ContractError(f"invalid manifest digest: {value!r}")
    return match.group(1)


def _assert_hashes(root: Path, entries: Mapping[str, str]) -> None:
    for relative, declared in sorted(entries.items()):
        candidate = root / relative
        try:
            candidate.relative_to(root)
        except ValueError as error:
            raise ContractError(f"manifest path escapes tree: {relative}") from error
        if not candidate.is_file() or candidate.is_symlink():
            raise ContractError(f"manifest artifact missing or not a regular file: {relative}")
        actual = sha256_file(candidate)
        expected = _digest_from_manifest(declared)
        if actual != expected:
            raise ContractError(f"manifest hash mismatch: {relative}")


def _iter_urls(value: Any) -> Iterable[str]:
    if isinstance(value, Mapping):
        for nested in value.values():
            yield from _iter_urls(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _iter_urls(nested)
    elif isinstance(value, str) and value.startswith(("http://", "https://")):
        yield value


def _assert_release_closure(document: Any, release_id: str) -> None:
    expected = (
        f"/{_TRUSTED_OWNER}/{_TRUSTED_REPOSITORY}/{_TRUSTED_GENERATED_REF}/"
        f"dist/releases/{release_id}/"
    )
    for url in _iter_urls(document):
        assert_public_https_url(url)
        parts = urlsplit(url)
        is_release_reference = (
            parts.hostname == "raw.githubusercontent.com" and "/dist/releases/" in parts.path
        )
        if is_release_reference and not parts.path.startswith(expected):
            raise ContractError(f"cross-generation repository URL: {redact_url(url)}")


def _assert_trusted_entry_urls(
    index: Mapping[str, Any],
    warehouse: Mapping[str, Any],
    config: Mapping[str, Any],
    *,
    release_id: str,
    has_live_channels: bool,
) -> None:
    base = (
        f"https://raw.githubusercontent.com/{_TRUSTED_OWNER}/{_TRUSTED_REPOSITORY}/"
        f"{_TRUSTED_GENERATED_REF}/dist/releases/{release_id}/"
    )
    urls = index.get("urls")
    if not isinstance(urls, list) or not urls:
        raise ContractError("index has no trusted release entries")
    for position, item in enumerate(urls):
        if not isinstance(item, Mapping):
            raise ContractError("index entry is not an object")
        expected_suffix = "configs/stable.json" if position == 0 else None
        value = item.get("url")
        if not isinstance(value, str) or not value.startswith(base + "configs/"):
            raise ContractError("index contains a foreign owner/repository/ref URL")
        if expected_suffix is not None and value != base + expected_suffix:
            raise ContractError("index stable entry is not the trusted release config")

    stores = warehouse.get("storeHouse")
    expected_stores = (
        ("DS 稳定仓", "depots/stable.json"),
        ("DS 公共实验仓", "depots/public-unverified.json"),
    )
    if not isinstance(stores, list) or len(stores) != len(expected_stores):
        raise ContractError("warehouse does not contain the trusted depot set")
    for item, (name, suffix) in zip(stores, expected_stores, strict=True):
        if not isinstance(item, Mapping) or item.get("sourceName") != name:
            raise ContractError("warehouse contains an unexpected depot")
        if item.get("sourceUrl") != base + suffix:
            raise ContractError("warehouse contains a foreign owner/repository/ref URL")

    lives = config.get("lives")
    expected_lives: list[dict[str, object]] = []
    if has_live_channels:
        expected_lives = [
            {"name": "DS 稳定直播", "type": 0, "url": base + "live/stable.m3u"}
        ]
    if lives != expected_lives:
        raise ContractError("stable config live URL is not the trusted release M3U")


def validate_release_tree(
    root: Path,
    schema_root: Path,
    *,
    owner: str,
    repository: str,
    expected_status: str | None = None,
) -> None:
    if (owner, repository) != (_TRUSTED_OWNER, _TRUSTED_REPOSITORY):
        raise SecurityError("release validation is restricted to azhansy/ds-tvbox")
    root_manifest = load_json(root / "dist/manifest.json")
    state = load_json(root / "state/release.json")
    release_id = root_manifest.get("active_release_id")
    if not isinstance(release_id, str) or not _RELEASE_ID.fullmatch(release_id):
        raise ContractError("invalid active_release_id")
    if state.get("active_release_id") != release_id:
        raise ContractError("state/root manifest release mismatch")
    if expected_status is not None and state.get("status") != expected_status:
        raise ContractError(f"state is not {expected_status}")

    release_pointer = root_manifest.get("release_manifest")
    if not isinstance(release_pointer, Mapping):
        raise ContractError("missing release manifest pointer")
    expected_release_path = f"dist/releases/{release_id}/manifest.json"
    if release_pointer.get("path") != expected_release_path:
        raise ContractError("release manifest path mismatch")
    release_manifest_path = root / expected_release_path
    if sha256_file(release_manifest_path) != _digest_from_manifest(
        str(release_pointer.get("sha256"))
    ):
        raise ContractError("release manifest pointer hash mismatch")
    release_manifest = load_json(release_manifest_path)
    if release_manifest.get("release_id") != release_id:
        raise ContractError("release manifest ID mismatch")
    if (
        release_manifest.get("content_workflow_run_id")
        != root_manifest.get("content_workflow_run_id")
        or release_manifest.get("content_workflow_run_attempt")
        != root_manifest.get("content_workflow_run_attempt")
    ):
        raise ContractError("manifest content identity mismatch")

    artifacts = release_manifest.get("artifacts")
    aliases = root_manifest.get("aliases")
    if not isinstance(artifacts, Mapping) or not isinstance(aliases, Mapping):
        raise ContractError("manifest hash maps missing")
    _assert_hashes(root, artifacts)
    _assert_hashes(root, aliases)

    alias_pairs = {
        "dist/index.json": f"dist/releases/{release_id}/index.json",
        "dist/warehouse.json": f"dist/releases/{release_id}/warehouse.json",
        "dist/configs/stable.json": f"dist/releases/{release_id}/configs/stable.json",
        "dist/live/stable.m3u": f"dist/releases/{release_id}/live/stable.m3u",
        "dist/health.json": f"dist/releases/{release_id}/health.json",
    }
    if set(aliases) != set(alias_pairs):
        raise ContractError("root alias set mismatch")
    for alias, release_path in alias_pairs.items():
        if (root / alias).read_bytes() != (root / release_path).read_bytes():
            raise ContractError(f"root alias is not byte-identical: {alias}")

    health = load_json(root / "dist/health.json")
    if not isinstance(health, Mapping):
        raise ContractError("health must be an object")
    if health.get("release_id") != release_id:
        raise ContractError("health release mismatch")

    report_path = root / "dist/reports/latest.json"
    if report_path.exists():
        report = load_json(report_path)
        if not isinstance(report, Mapping):
            raise ContractError("release report must be an object")
        validate_schema(report, schema_root / "report.schema.json")
        event_identity = (state.get("workflow_run_id"), state.get("workflow_run_attempt"))
        if event_identity != (
            report.get("workflow_run_id"),
            report.get("workflow_run_attempt"),
        ):
            raise ContractError("state/report event identity mismatch")
        content_identity = (
            release_manifest.get("content_workflow_run_id"),
            release_manifest.get("content_workflow_run_attempt"),
        )
        report_content = report.get("content_identity")
        if not isinstance(report_content, Mapping) or content_identity != (
            report_content.get("workflow_run_id"),
            report_content.get("workflow_run_attempt"),
        ):
            raise ContractError("report/manifest content identity mismatch")

    documents: dict[str, Mapping[str, Any]] = {}
    for relative in ("dist/index.json", "dist/warehouse.json", "dist/configs/stable.json"):
        document = load_json(root / relative)
        if not isinstance(document, Mapping):
            raise ContractError(f"{relative} must be an object")
        documents[relative] = document
        _assert_release_closure(document, release_id)
        scan_client_value(document)
    validate_schema(load_json(root / "dist/index.json"), schema_root / "depot.schema.json")
    validate_schema(load_json(root / "dist/warehouse.json"), schema_root / "depot.schema.json")
    stable_config = documents["dist/configs/stable.json"]
    validate_client_json(stable_config, schema_root)
    m3u_urls = validate_m3u((root / "dist/live/stable.m3u").read_bytes())
    _assert_trusted_entry_urls(
        documents["dist/index.json"],
        documents["dist/warehouse.json"],
        stable_config,
        release_id=release_id,
        has_live_channels=bool(m3u_urls),
    )
    health_result = validate_health_document(health, schema_root, m3u_urls=m3u_urls)
    if release_manifest.get("vod_site_count") != len(stable_config["sites"]):
        raise ContractError("release manifest VOD count differs from stable config")
    if release_manifest.get("live_channel_count") != len(m3u_urls):
        raise ContractError("release manifest live count differs from M3U")
    upstreams = release_manifest.get("upstreams")
    assert isinstance(upstreams, list)
    upstream_ids = {
        str(item["source_id"]) for item in upstreams if isinstance(item, Mapping)
    }
    if upstream_ids != set(health_result.source_ids):
        raise ContractError("release upstreams and health sources differ")
