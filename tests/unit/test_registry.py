from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from ds_tvbox.errors import ContractError
from ds_tvbox.models import FetchMode, ParserKind, SourceKind
from ds_tvbox.registry import load_registry, load_yaml_strict

ROOT = Path(__file__).resolve().parents[2]
REGISTRY = ROOT / "sources" / "registry.yaml"


def _registry_dict() -> dict:
    value = load_yaml_strict(REGISTRY.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _write_registry(tmp_path: Path, value: dict) -> Path:
    path = tmp_path / "registry.yaml"
    path.write_text(yaml.safe_dump(value, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


def test_real_registry_loads_into_strict_models() -> None:
    sources = load_registry(REGISTRY)
    assert [source.id for source in sources] == ["ikun-vod", "iptv-org-cn-cctv"]
    assert sources[0].kind is SourceKind.VOD_SITE
    assert sources[0].parser is ParserKind.MACCMS_JSON
    assert sources[0].fetch.mode is FetchMode.DIRECT_URL
    assert sources[0].client_site is not None
    assert sources[0].client_site.quick_search == 1
    assert sources[1].terms_watch[0].path == "LICENSE"


def test_duplicate_yaml_key_is_rejected_before_schema() -> None:
    with pytest.raises(ContractError, match="duplicate YAML key 'id'"):
        load_yaml_strict("version: 1\nsources:\n  - id: first\n    id: second\n")


def test_unknown_registry_field_is_rejected(tmp_path: Path) -> None:
    value = _registry_dict()
    value["sources"][0]["surprise"] = True
    with pytest.raises(ContractError, match=r"schema violation.*surprise"):
        load_registry(_write_registry(tmp_path, value))


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda source: source.update(parser="tvbox_json"), "illegal kind/parser"),
        (
            lambda source: source["terms_watch"].append(
                {
                    "type": "github_path",
                    "url": None,
                    "path": "LICENSE",
                    "reviewed_sha256": "a" * 64,
                }
            ),
            "github_path terms are illegal",
        ),
        (
            lambda source: source.update(catalog={}),
            "schema violation",
        ),
        (
            lambda source: source.update(client_site=None),
            "vod_site requires client_site",
        ),
    ],
)
def test_illegal_source_combinations_fail_before_network(
    tmp_path: Path,
    mutation,
    message: str,
) -> None:
    value = _registry_dict()
    mutation(value["sources"][0])
    with pytest.raises(ContractError, match=message):
        load_registry(_write_registry(tmp_path, value))


def test_github_tracking_requires_license_and_readme_terms(tmp_path: Path) -> None:
    value = _registry_dict()
    tracked = value["sources"][1]
    tracked["terms_watch"] = [copy.deepcopy(tracked["terms_watch"][0])]
    with pytest.raises(ContractError, match="README/terms"):
        load_registry(_write_registry(tmp_path, value))


def test_tracked_url_must_pin_reviewed_commit_and_path(tmp_path: Path) -> None:
    value = _registry_dict()
    value["sources"][1]["fetch"]["reviewed_url"] = (
        "https://raw.githubusercontent.com/iptv-org/iptv/master/streams/cn_cctv.m3u"
    )
    with pytest.raises(ContractError, match="reviewed commit and config_path"):
        load_registry(_write_registry(tmp_path, value))


def test_tracked_raw_url_must_belong_to_reviewed_repository(tmp_path: Path) -> None:
    value = _registry_dict()
    value["sources"][1]["fetch"]["reviewed_url"] = (
        "https://raw.githubusercontent.com/other/other/"
        "061e61103edeab1ecd1bc4d99cb38992f003653e/streams/cn_cctv.m3u"
    )
    with pytest.raises(ContractError, match="belong to fetch.repository_url"):
        load_registry(_write_registry(tmp_path, value))


def test_registry_hosts_are_exact_dns_names(tmp_path: Path) -> None:
    value = _registry_dict()
    value["sources"][0]["allowed_hosts"] = ["*.example.com"]
    with pytest.raises(ContractError):
        load_registry(_write_registry(tmp_path, value))


def test_duplicate_source_id_is_rejected(tmp_path: Path) -> None:
    value = _registry_dict()
    duplicate = copy.deepcopy(value["sources"][0])
    value["sources"].append(duplicate)
    with pytest.raises(ContractError, match="duplicate source id"):
        load_registry(_write_registry(tmp_path, value))


def test_yaml_and_registry_files_fail_closed_on_parse_or_read_errors(tmp_path: Path) -> None:
    with pytest.raises(ContractError, match="mapping key must be scalar"):
        load_yaml_strict("? [a, b]\n: value\n")
    with pytest.raises(ContractError, match="invalid YAML"):
        load_yaml_strict("sources: [unterminated")
    with pytest.raises(ContractError, match="cannot read source registry"):
        load_registry(tmp_path / "missing.yaml")

    malformed_schema = tmp_path / "schema.json"
    malformed_schema.write_text("not-json", encoding="utf-8")
    with pytest.raises(ContractError, match="cannot read registry schema"):
        load_registry(REGISTRY, malformed_schema)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda source: source["fetch"].update(reviewed_url=None), "direct_url requires"),
        (
            lambda source: source["fetch"].update(
                repository_url="https://github.com/example/repo"
            ),
            "direct_url requires",
        ),
        (
            lambda source: source["fetch"].update(
                reviewed_url="https://other.example/api"
            ),
            "reviewed_url violates",
        ),
        (
            lambda source: source["fetch"].update(
                reviewed_url="https://ikunzyapi.com/api?token=secret"
            ),
            "reviewed_url violates",
        ),
        (
            lambda source: source.update(license_spdx="MIT OR Apache-2.0"),
            "safe SPDX",
        ),
        (
            lambda source: source.update(
                allowed_hosts=["ikunzyapi.com", "ikunzyapi.com."]
            ),
            "normalized duplicates",
        ),
    ],
)
def test_direct_source_semantic_contracts_are_enforced(
    tmp_path: Path,
    mutation,
    message: str,
) -> None:
    value = _registry_dict()
    mutation(value["sources"][0])
    with pytest.raises(ContractError, match=message):
        load_registry(_write_registry(tmp_path, value))


@pytest.mark.parametrize("track_ref", ["a..b", "a//b", "release.lock", "a@{b", "/main"])
def test_github_track_ref_rejects_ambiguous_git_names(
    tmp_path: Path,
    track_ref: str,
) -> None:
    value = _registry_dict()
    value["sources"][1]["fetch"]["track_ref"] = track_ref
    with pytest.raises(ContractError, match="safe Git ref"):
        load_registry(_write_registry(tmp_path, value))


@pytest.mark.parametrize(
    ("path", "message"),
    [
        ("", "requires all fetch fields"),
        ("../config.m3u", "fetch.config_path"),
        ("a//b.m3u", "fetch.config_path"),
        ("a\\b.m3u", "fetch.config_path"),
        ("/a.m3u", "fetch.config_path"),
    ],
)
def test_github_config_path_must_be_a_safe_posix_relative_path(
    tmp_path: Path,
    path: str,
    message: str,
) -> None:
    value = _registry_dict()
    value["sources"][1]["fetch"]["config_path"] = path
    with pytest.raises(ContractError, match=message):
        load_registry(_write_registry(tmp_path, value))


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda source: source["fetch"].update(repository_url="https://github.com/a/b/c"),
            "owner/repository",
        ),
        (
            lambda source: source.update(allowed_hosts=["raw.githubusercontent.com"]),
            "GitHub tracking requires",
        ),
        (
            lambda source: source["fetch"].update(reviewed_url="https://example.com/config.m3u"),
            "tracked reviewed_url violates",
        ),
        (
            lambda source: source["terms_watch"].append(
                copy.deepcopy(source["terms_watch"][0])
            ),
            "duplicate terms_watch",
        ),
        (
            lambda source: source["terms_watch"][0].update(path="../LICENSE"),
            r"terms_watch\[0\].path",
        ),
    ],
)
def test_github_tracking_host_and_terms_contracts(
    tmp_path: Path,
    mutation,
    message: str,
) -> None:
    value = _registry_dict()
    mutation(value["sources"][1])
    with pytest.raises(ContractError, match=message):
        load_registry(_write_registry(tmp_path, value))


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda evidence: evidence.pop("selector"), "missing or unknown"),
        (lambda evidence: evidence.update(revision="bad"), "revision must be a commit"),
        (lambda evidence: evidence.update(config_path="../secret"), "config_path"),
        (lambda evidence: evidence.update(selector={}), "non-empty object"),
        (
            lambda evidence: evidence.update(selector={"api": ["not", "scalar"]}),
            "scalar evidence",
        ),
        (
            lambda evidence: evidence.update(repository_url="https://gitlab.com/a/b"),
            "GitHub URL",
        ),
    ],
)
def test_discovery_evidence_is_complete_and_immutable(
    tmp_path: Path,
    mutation,
    message: str,
) -> None:
    value = _registry_dict()
    evidence = value["sources"][0]["discovery_evidence"]
    assert isinstance(evidence, dict)
    mutation(evidence)
    with pytest.raises(ContractError, match=message):
        load_registry(_write_registry(tmp_path, value))


def _catalog_registry() -> dict:
    value = _registry_dict()
    source = value["sources"][1]
    source.update(
        id="catalog-source",
        kind="repository_catalog",
        parser="repository_catalog",
        client_site=None,
        catalog={
            "max_depth": 3,
            "path_globs": ["configs/*.json"],
            "parsers_by_glob": [{"glob": "configs/*.json", "parser": "tvbox_json"}],
            "selectors": {
                "sites_arrays": ["/sites"],
                "depot_arrays": ["/urls"],
                "storehouse_arrays": ["/storeHouse"],
                "live_arrays": ["/lives"],
            },
            "max_files": 10,
            "max_candidates": 20,
            "max_live_urls": 30,
            "allowed_downstream_hosts": ["example.com"],
            "auto_onboard_public_unverified": False,
        },
    )
    source["fetch"].update(reviewed_url=None, config_path=None, mode="github_repository")
    return value


def test_valid_repository_catalog_loads_as_catalog_source(tmp_path: Path) -> None:
    sources = load_registry(_write_registry(tmp_path, _catalog_registry()))
    assert sources[1].kind is SourceKind.REPOSITORY_CATALOG
    assert sources[1].catalog is not None
    assert sources[1].fetch.mode is FetchMode.GITHUB_REPOSITORY


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda catalog: catalog.update(path_globs=["configs/[ab].json"]), "glob syntax"),
        (
            lambda catalog: catalog.update(
                path_globs=["configs/*.json", "configs/*.json"]
            ),
            "glob entries must be unique",
        ),
        (
            lambda catalog: catalog["parsers_by_glob"][0].update(glob="other/*.json"),
            "exactly one parser mapping",
        ),
        (
            lambda catalog: catalog["selectors"].update(sites_arrays=["sites"]),
            "schema violation",
        ),
        (
            lambda catalog: catalog["selectors"].update(sites_arrays=["/sites/*"]),
            "without wildcards",
        ),
        (
            lambda catalog: catalog["selectors"].update(sites_arrays=["/sites/~2bad"]),
            "invalid RFC 6901 escape",
        ),
        (
            lambda catalog: catalog.update(allowed_downstream_hosts=["127.0.0.1"]),
            "IP literals",
        ),
    ],
)
def test_catalog_globs_selectors_and_hosts_are_strict(
    tmp_path: Path,
    mutation,
    message: str,
) -> None:
    value = _catalog_registry()
    catalog = value["sources"][1]["catalog"]
    assert isinstance(catalog, dict)
    mutation(catalog)
    with pytest.raises(ContractError, match=message):
        load_registry(_write_registry(tmp_path, value))


def test_http_exception_must_be_bound_to_allowed_host(tmp_path: Path) -> None:
    value = _registry_dict()
    value["sources"][0]["http_exceptions"] = [
        {
            "host": "legacy.example.com",
            "port": 80,
            "path_prefix": "/public",
            "reason": "legacy",
            "reviewed_at": "2026-07-22",
        }
    ]
    with pytest.raises(ContractError, match="also be in allowed_hosts"):
        load_registry(_write_registry(tmp_path, value))
