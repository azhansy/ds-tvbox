import json
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest
from test_raw import _release_tree

import ds_tvbox.validation as validation_module
from ds_tvbox.errors import ContractError, SecurityError
from ds_tvbox.manifests import prefixed_sha256
from ds_tvbox.serialization import canonical_json_bytes
from ds_tvbox.validation import (
    _digest_from_manifest,
    assert_public_https_url,
    load_json,
    redact_url,
    scan_client_value,
    validate_client_json,
    validate_health_document,
    validate_m3u,
    validate_release_tree,
    validate_schema,
)

SCHEMAS = Path(__file__).resolve().parents[2] / "schemas"
_LIVE_URL = "https://live.example.test/index.m3u8"
_LIVE_ID = f"live-url:live-source:{sha256(_LIVE_URL.encode()).hexdigest()[:16]}"
_CHANNEL_ID = f"channel:{sha256(b'tvg:live.test').hexdigest()[:16]}"


def _health_graph() -> dict[str, Any]:
    live_id = _LIVE_ID
    channel_id = _CHANNEL_ID
    return {
        "schema_version": "1.0.0",
        "generated_at": "2026-07-22T12:00:00Z",
        "generation": 1,
        "release_id": "g00000001",
        "sources": [
            {
                "entity_id": "source:live-source",
                "source_id": "live-source",
                "technical_status": "healthy",
                "publication_status": "stable",
                "rights_status": "public_unverified",
                "last_checked_at": "2026-07-22T12:00:00Z",
                "upstream_revision": None,
                "failure_reason": None,
                "items": [
                    {
                        "entity_type": "live_url",
                        "entity_id": live_id,
                        "channel_id": channel_id,
                        "technical_status": "healthy",
                        "publication_status": "stable",
                        "last_success_at": "2026-07-22T12:00:00Z",
                        "consecutive_successes": 1,
                        "consecutive_failures": 0,
                        "media_path_score": 1,
                        "response_ms": 100,
                        "response_ms_history": [100],
                        "protocol": "https",
                        "final_url": "https://live.example.test/index.m3u8",
                        "normalized_url": "https://live.example.test/index.m3u8",
                        "name": "Live",
                        "tvg_id": "live.test",
                        "group": None,
                        "logo": None,
                        "epg": None,
                        "width": None,
                        "height": None,
                        "bandwidth": None,
                        "failure_reason": None,
                        "secondary_reasons": [],
                    }
                ],
            }
        ],
        "channels": [
            {
                "entity_id": channel_id,
                "identity_basis": "tvg_id",
                "normalized_identity": "live.test",
                "technical_status": "healthy",
                "publication_status": "stable",
                "rights_status": "public_unverified",
                "selected_url_id": live_id,
                "candidate_url_ids": [live_id],
            }
        ],
    }


def test_load_json_rejects_duplicate_keys(tmp_path: Path) -> None:
    path = tmp_path / "duplicate.json"
    path.write_text('{"sites": [], "sites": []}', encoding="utf-8")
    with pytest.raises(ContractError, match="duplicate JSON key"):
        load_json(path)


def test_health_schema_and_graph_relationships_are_strict() -> None:
    document = _health_graph()
    result = validate_health_document(
        document,
        SCHEMAS,
        m3u_urls=("https://live.example.test/index.m3u8",),
    )
    assert result.source_ids == frozenset({"live-source"})
    assert result.selected_live_entity_ids == frozenset(
        {_LIVE_ID}
    )

    document = _health_graph()
    document["unexpected"] = True
    with pytest.raises(ContractError, match="health.schema.json"):
        validate_health_document(document, SCHEMAS)


def test_failed_live_observation_has_no_final_protocol() -> None:
    document = _health_graph()
    source = document["sources"][0]
    item = source["items"][0]
    channel = document["channels"][0]

    source["technical_status"] = "dead"
    source["publication_status"] = "withheld"
    item.update(
        {
            "technical_status": "dead",
            "publication_status": "withheld",
            "last_success_at": None,
            "consecutive_successes": 0,
            "consecutive_failures": 1,
            "media_path_score": 0,
            "response_ms_history": [],
            "protocol": None,
            "final_url": None,
            "failure_reason": "response_too_large",
        }
    )
    channel.update(
        {
            "technical_status": "dead",
            "publication_status": "withheld",
            "selected_url_id": None,
        }
    )

    result = validate_health_document(document, SCHEMAS, m3u_urls=())
    assert result.selected_live_entity_ids == frozenset()

    item["protocol"] = "https"
    with pytest.raises(ContractError, match="protocol must be null"):
        validate_health_document(document, SCHEMAS, m3u_urls=())


def test_health_graph_rejects_duplicate_and_dangling_entities() -> None:
    duplicate = _health_graph()
    duplicate["sources"].append(dict(duplicate["sources"][0]))
    with pytest.raises(ContractError, match="duplicate health entity"):
        validate_health_document(
            duplicate,
            SCHEMAS,
            m3u_urls=("https://live.example.test/index.m3u8",),
        )

    dangling_selected = _health_graph()
    dangling_selected["channels"][0]["selected_url_id"] = (
        "live-url:live-source:8899aabbccddeeff"
    )
    with pytest.raises(ContractError, match="selected_url_id"):
        validate_health_document(
            dangling_selected,
            SCHEMAS,
            m3u_urls=("https://live.example.test/index.m3u8",),
        )

    dangling_channel = _health_graph()
    dangling_channel["sources"][0]["items"][0]["channel_id"] = (
        "channel:0011223344556677"
    )
    with pytest.raises(
        ContractError,
        match="dangling or missing candidate|channel_id differs",
    ):
        validate_health_document(
            dangling_channel,
            SCHEMAS,
            m3u_urls=("https://live.example.test/index.m3u8",),
        )


def test_health_selected_urls_must_equal_published_m3u_urls() -> None:
    with pytest.raises(ContractError, match="M3U channel URLs differ"):
        validate_health_document(
            _health_graph(),
            SCHEMAS,
            m3u_urls=("https://other.example.test/index.m3u8",),
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("technical_status", "dead"),
        ("publication_status", "withheld"),
        ("media_path_score", 0),
    ],
)
def test_health_selected_url_must_be_healthy_stable_playable_media(
    field: str, value: object
) -> None:
    document = _health_graph()
    document["sources"][0]["items"][0][field] = value

    with pytest.raises(
        ContractError,
        match="aggregate status|deduplication winner|non-playable",
    ):
        validate_health_document(
            document,
            SCHEMAS,
            m3u_urls=("https://live.example.test/index.m3u8",),
        )


def test_stable_health_channel_requires_one_valid_selection() -> None:
    document = _health_graph()
    document["channels"][0]["selected_url_id"] = None

    with pytest.raises(ContractError, match="selected_url_id"):
        validate_health_document(document, SCHEMAS, m3u_urls=())


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("name", "n" * 513),
        ("tvg_id", "t" * 1025),
        ("group", "g" * 1025),
        ("normalized_url", "https://example.test/" + "n" * 8192),
        ("final_url", "https://example.test/" + "f" * 8192),
        ("logo", "https://example.test/" + "l" * 8192),
        ("epg", "https://example.test/" + "e" * 8192),
    ],
)
def test_health_live_client_fields_have_bounded_lengths(
    field: str, value: str
) -> None:
    document = _health_graph()
    document["sources"][0]["items"][0][field] = value

    with pytest.raises(ContractError, match="health.schema.json"):
        validate_health_document(document, SCHEMAS)


def test_health_candidates_for_one_channel_must_agree_on_identity_facts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = _health_graph()
    first = document["sources"][0]["items"][0]
    second = json.loads(json.dumps(first))
    second_url = "https://live.example.test/second.m3u8"
    second["entity_id"] = (
        "live-url:live-source:" + sha256(second_url.encode()).hexdigest()[:16]
    )
    second["normalized_url"] = second_url
    second["final_url"] = second_url
    second["name"] = "Second"
    document["sources"][0]["items"].append(second)
    document["sources"][0]["items"].sort(key=lambda item: item["entity_id"])

    monkeypatch.setattr(
        validation_module,
        "_channel_identity",
        lambda _source, name, _tvg: (_CHANNEL_ID, "tvg_id", name.casefold()),
    )

    with pytest.raises(ContractError, match="disagree on identity facts"):
        validate_health_document(document, SCHEMAS)


def test_empty_source_cannot_retain_healthy_stable_aggregate() -> None:
    document = _health_graph()
    document["sources"][0]["items"] = []
    document["channels"] = []

    with pytest.raises(ContractError, match="source aggregate status"):
        validate_health_document(document, SCHEMAS, m3u_urls=())


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("consecutive_successes", 0),
        ("consecutive_failures", 1),
        ("last_success_at", None),
        ("response_ms", None),
        ("response_ms_history", []),
        ("secondary_reasons", ["fetch_timeout"]),
    ],
)
def test_selected_live_requires_coherent_success_facts(
    field: str, value: object
) -> None:
    document = _health_graph()
    document["sources"][0]["items"][0][field] = value

    with pytest.raises(ContractError, match="non-playable|selected_url_id"):
        validate_health_document(
            document,
            SCHEMAS,
            m3u_urls=("https://live.example.test/index.m3u8",),
        )


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/live.m3u8",
        "https://user@example.com/live.m3u8",
        "https://example.com/live.m3u8?auth=testpub",
        "https://example.com/live.m3u8?api-key=visible",
        "https://example.com/live.m3u8?to_ken=visible",
        "https://example.com/live.m3u8?ｔｏ＿ｋｅｎ=visible",
        "https://127.0.0.1/live.m3u8",
        "https://[::1]/live.m3u8",
    ],
)
def test_client_url_must_be_credential_free_https(url: str) -> None:
    with pytest.raises(SecurityError):
        assert_public_https_url(url)


def test_recursive_scanner_rejects_nested_executable_and_header() -> None:
    with pytest.raises(SecurityError, match="forbidden client field"):
        scan_client_value({"sites": [{"nested": {"header": {"Referer": "x"}}}]})
    with pytest.raises(SecurityError, match="executable dependency"):
        scan_client_value({"sites": [{"nested": ["https://example.com/a.js"]}]})


def test_output_m3u_has_no_header_directives() -> None:
    good = b"#EXTM3U\n#EXTINF:-1,News\nhttps://example.com/live.m3u8\n"
    validate_m3u(good)
    with pytest.raises(SecurityError, match="header instructions"):
        validate_m3u(b"#EXTM3U\n#EXTVLCOPT:http-referrer=x\nhttps://example.com/x.m3u8\n")


def _document(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_bytes())
    assert isinstance(value, dict)
    return value


def _write(path: Path, value: object) -> bytes:
    data = canonical_json_bytes(value)
    path.write_bytes(data)
    return data


def _rehash_release_manifest(root: Path, relative: str) -> None:
    root_path = root / "dist/manifest.json"
    root_manifest = _document(root_path)
    release_path = root / str(root_manifest["release_manifest"]["path"])
    release = _document(release_path)
    release["artifacts"][relative] = prefixed_sha256((root / relative).read_bytes())
    release_bytes = _write(release_path, release)
    root_manifest["release_manifest"]["sha256"] = prefixed_sha256(release_bytes)
    _write(root_path, root_manifest)


def test_load_json_schema_and_digest_fail_closed(tmp_path: Path) -> None:
    missing = tmp_path / "missing.json"
    with pytest.raises(ContractError, match="invalid JSON"):
        load_json(missing)
    malformed = tmp_path / "malformed.json"
    malformed.write_text("{", encoding="utf-8")
    with pytest.raises(ContractError, match="invalid JSON"):
        load_json(malformed)
    with pytest.raises(ContractError, match="schema validation failed"):
        validate_schema({}, SCHEMAS / "tvbox-config.schema.json")
    with pytest.raises(ContractError, match="invalid manifest digest"):
        _digest_from_manifest("0" * 64)


def test_url_redaction_and_client_scanner_cover_type3_and_lists() -> None:
    assert redact_url("not-a-url") == "<invalid-url>"
    assert redact_url("https://user:secret@example.test/a?q=secret") == (
        "https://example.test/a"
    )
    with pytest.raises(SecurityError, match="type 3"):
        scan_client_value({"sites": [{"type": 3}]})
    with pytest.raises(SecurityError, match="executable dependency"):
        scan_client_value([{"safe": "https://example.test/plugin.jar"}])


def test_client_and_m3u_validators_reject_insecure_playback(tmp_path: Path) -> None:
    config = {
        "sites": [
            {
                "key": "source",
                "name": "source",
                "type": 1,
                "api": "https://user@example.test/api",
                "searchable": 1,
                "quickSearch": 1,
                "filterable": 0,
                "changeable": 0,
            }
        ],
        "lives": [],
        "parses": [],
    }
    with pytest.raises(SecurityError, match="credential-free HTTPS"):
        validate_client_json(config, SCHEMAS)
    with pytest.raises(ContractError, match="UTF-8"):
        validate_m3u(b"#EXTM3U\n\xff")
    with pytest.raises(ContractError, match="use LF"):
        validate_m3u(b"#EXTM3U\r\n")
    with pytest.raises(SecurityError, match="credential-free HTTPS"):
        validate_m3u(b"#EXTM3U\n#EXTINF:-1,News\nhttp://example.test/live.m3u8\n")


def test_release_tree_checks_state_pointer_and_expected_status(tmp_path: Path) -> None:
    _release_tree(tmp_path)
    state_path = tmp_path / "state/release.json"
    state = _document(state_path)
    state["active_release_id"] = "g00000002"
    _write(state_path, state)
    with pytest.raises(ContractError, match="state/root manifest release mismatch"):
        validate_release_tree(
            tmp_path, SCHEMAS, owner="azhansy", repository="ds-tvbox"
        )

    _release_tree(tmp_path := tmp_path / "status")
    with pytest.raises(ContractError, match="state is not pending"):
        validate_release_tree(
            tmp_path,
            SCHEMAS,
            owner="azhansy",
            repository="ds-tvbox",
            expected_status="pending",
        )

    root_path = tmp_path / "dist/manifest.json"
    root = _document(root_path)
    root["release_manifest"] = []
    _write(root_path, root)
    with pytest.raises(ContractError, match="missing release manifest pointer"):
        validate_release_tree(
            tmp_path, SCHEMAS, owner="azhansy", repository="ds-tvbox"
        )


def test_release_tree_rejects_pointer_hash_id_and_content_identity(tmp_path: Path) -> None:
    _release_tree(tmp_path)
    root_path = tmp_path / "dist/manifest.json"
    root = _document(root_path)
    root["release_manifest"]["sha256"] = "sha256:" + "0" * 64
    _write(root_path, root)
    with pytest.raises(ContractError, match="pointer hash mismatch"):
        validate_release_tree(
            tmp_path, SCHEMAS, owner="azhansy", repository="ds-tvbox"
        )

    _release_tree(tmp_path := tmp_path / "identity")
    root_path = tmp_path / "dist/manifest.json"
    root = _document(root_path)
    release_path = tmp_path / str(root["release_manifest"]["path"])
    release = _document(release_path)
    release["release_id"] = "g00000002"
    release_bytes = _write(release_path, release)
    root["release_manifest"]["sha256"] = prefixed_sha256(release_bytes)
    _write(root_path, root)
    with pytest.raises(ContractError, match="manifest ID mismatch"):
        validate_release_tree(
            tmp_path, SCHEMAS, owner="azhansy", repository="ds-tvbox"
        )

    release["release_id"] = "g00000001"
    release["content_workflow_run_id"] = "different"
    release_bytes = _write(release_path, release)
    root["release_manifest"]["sha256"] = prefixed_sha256(release_bytes)
    _write(root_path, root)
    with pytest.raises(ContractError, match="content identity mismatch"):
        validate_release_tree(
            tmp_path, SCHEMAS, owner="azhansy", repository="ds-tvbox"
        )


def test_release_tree_rejects_hash_maps_alias_drift_and_health_drift(tmp_path: Path) -> None:
    _release_tree(tmp_path)
    root_path = tmp_path / "dist/manifest.json"
    root = _document(root_path)
    root["aliases"] = []
    _write(root_path, root)
    with pytest.raises(ContractError, match="manifest hash maps missing"):
        validate_release_tree(
            tmp_path, SCHEMAS, owner="azhansy", repository="ds-tvbox"
        )

    _release_tree(tmp_path := tmp_path / "alias")
    alias_path = tmp_path / "dist/index.json"
    alias = _document(alias_path)
    alias["urls"][0]["name"] = "Changed"
    alias_bytes = _write(alias_path, alias)
    root_path = tmp_path / "dist/manifest.json"
    root = _document(root_path)
    root["aliases"]["dist/index.json"] = prefixed_sha256(alias_bytes)
    _write(root_path, root)
    with pytest.raises(ContractError, match="not byte-identical"):
        validate_release_tree(
            tmp_path, SCHEMAS, owner="azhansy", repository="ds-tvbox"
        )

    _release_tree(tmp_path := tmp_path / "health")
    release_health = "dist/releases/g00000001/health.json"
    health = _document(tmp_path / release_health)
    health["release_id"] = "g00000002"
    health_bytes = _write(tmp_path / release_health, health)
    _write(tmp_path / "dist/health.json", health)
    _rehash_release_manifest(tmp_path, release_health)
    root = _document(tmp_path / "dist/manifest.json")
    root["aliases"]["dist/health.json"] = prefixed_sha256(health_bytes)
    _write(tmp_path / "dist/manifest.json", root)
    with pytest.raises(ContractError, match="health release mismatch"):
        validate_release_tree(
            tmp_path, SCHEMAS, owner="azhansy", repository="ds-tvbox"
        )


def test_release_tree_rejects_report_identity_and_cross_generation_url(tmp_path: Path) -> None:
    _release_tree(tmp_path)
    report_path = tmp_path / "dist/reports/latest.json"
    report = _document(report_path)
    report["workflow_run_id"] = "different"
    _write(report_path, report)
    with pytest.raises(ContractError, match="event identity mismatch"):
        validate_release_tree(
            tmp_path, SCHEMAS, owner="azhansy", repository="ds-tvbox"
        )

    _release_tree(tmp_path := tmp_path / "cross")
    relative = "dist/releases/g00000001/index.json"
    index = _document(tmp_path / relative)
    index["urls"][0]["url"] = index["urls"][0]["url"].replace(
        "g00000001", "g00000002"
    )
    index_bytes = _write(tmp_path / relative, index)
    _write(tmp_path / "dist/index.json", index)
    _rehash_release_manifest(tmp_path, relative)
    root = _document(tmp_path / "dist/manifest.json")
    root["aliases"]["dist/index.json"] = prefixed_sha256(index_bytes)
    _write(tmp_path / "dist/manifest.json", root)
    with pytest.raises(ContractError, match="cross-generation repository URL"):
        validate_release_tree(
            tmp_path, SCHEMAS, owner="azhansy", repository="ds-tvbox"
        )
