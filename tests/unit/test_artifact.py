from __future__ import annotations

import json
import shutil
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest

from ds_tvbox.artifact import (
    _file_records,
    _validate_source_kinds_and_client_configs,
    build_publish_artifact,
    validate_publish_artifact,
)
from ds_tvbox.bundle import build_bundle_files
from ds_tvbox.errors import ContractError, SecurityError
from ds_tvbox.generator import build_client_artifacts
from ds_tvbox.health import build_health_document
from ds_tvbox.live import select_channels
from ds_tvbox.models import (
    LiveCandidate,
    LiveProbeResult,
    MediaProbeResult,
    PublicationStatus,
    ReleaseKind,
    RightsStatus,
    RunContext,
    TechnicalStatus,
    VodCapabilities,
    VodProbeResult,
    VodSiteCandidate,
)
from ds_tvbox.registry import load_registry
from ds_tvbox.reports import build_latest_report, render_latest_markdown
from ds_tvbox.serialization import canonical_json_bytes

SCHEMAS = Path(__file__).resolve().parents[2] / "schemas"


def _context(kind: ReleaseKind = ReleaseKind.BOOTSTRAP) -> RunContext:
    generation = 1 if kind is ReleaseKind.BOOTSTRAP else 2
    return RunContext(
        owner="azhansy",
        repository="ds-tvbox",
        generated_ref="generated",
        workflow_run_id="42",
        workflow_run_attempt=2,
        generated_at="2026-07-22T12:00:00Z",
        generation=generation,
        release_kind=kind,
        previous_head=None if kind is ReleaseKind.BOOTSTRAP else "a" * 40,
        previous_last_success_at=None,
    )


def _artifact(
    tmp_path: Path,
    kind: ReleaseKind = ReleaseKind.BOOTSTRAP,
    *,
    include_vod: bool = True,
    include_live: bool = True,
    mandatory: tuple[str, ...] | None = None,
) -> Path:
    context = _context(kind)
    registry = load_registry(
        SCHEMAS.parent / "sources/registry.yaml",
        schema_path=SCHEMAS / "source-registry.schema.json",
    )
    by_id = {source.id: source for source in registry}
    vod_source = by_id["ikun-vod"]
    live_source = by_id["iptv-org-cn-cctv"]
    sources = (vod_source, live_source)
    assert vod_source.client_site is not None
    assert vod_source.fetch.reviewed_url is not None
    client_site = vod_source.client_site
    vod_candidate = VodSiteCandidate(
        source_id="ikun-vod",
        key=client_site.key,
        name=client_site.name,
        type=1,
        api=vod_source.fetch.reviewed_url,
        searchable=client_site.searchable,
        quick_search=client_site.quick_search,
        filterable=client_site.filterable,
        changeable=client_site.changeable,
        categories=("电影",),
        rights_status=RightsStatus.PUBLIC_UNVERIFIED,
    )
    vod_results = (
        VodProbeResult(
            candidate=vod_candidate,
            technical_status=TechnicalStatus.HEALTHY,
            publication_status=PublicationStatus.STABLE,
            capabilities=VodCapabilities(True, True, True, True, True),
            failure_reason=None,
        ),
    ) if include_vod else ()
    live_candidate = LiveCandidate(
        source_id="iptv-org-cn-cctv",
        name="Live",
        original_url="https://live.example.test/index.m3u8",
        normalized_url="https://live.example.test/index.m3u8",
        rights_status=RightsStatus.PUBLIC_UNVERIFIED,
        tvg_id="live.test",
    )
    live_results = (
        LiveProbeResult(
            candidate=live_candidate,
            technical_status=TechnicalStatus.HEALTHY,
            publication_status=PublicationStatus.STABLE,
            media=MediaProbeResult(
                ok=True,
                final_url=live_candidate.normalized_url,
                response_ms=100,
                media_path_score=1,
            ),
            consecutive_successes=1,
            consecutive_failures=0,
            last_success_at=context.generated_at,
            failure_reason=None,
            response_ms_history=(100,),
        ),
    ) if include_live else ()
    selected_channels = select_channels(live_results)
    client = build_client_artifacts(
        context=context,
        sources=sources,
        vod_results=vod_results,
        channels=selected_channels,
    )
    identity = {
        "workflow_run_id": context.workflow_run_id,
        "workflow_run_attempt": context.workflow_run_attempt,
    }
    if mandatory is None:
        mandatory = ("source:removed",) if kind is ReleaseKind.SAFETY else ()
    reasons = ["mandatory_removal"] if kind is ReleaseKind.SAFETY else []
    health = build_health_document(
        generated_at=context.generated_at,
        generation=context.generation,
        release_id=context.release_id,
        sources=sources,
        vod_results=vod_results,
        live_results=live_results,
        selected_channels=selected_channels,
        upstream_revisions={
            source.id: source.fetch.reviewed_revision for source in sources
        },
    )
    report_sources = [
        {
            "source_id": source["source_id"],
            "technical_status": source["technical_status"],
            "publication_status": source["publication_status"],
            "rights_status": source["rights_status"],
            "failure_reason": source["failure_reason"],
            "secondary_reasons": [],
            "upstream_revision": source["upstream_revision"],
            "change_summary": "enumerated",
        }
        for source in health["sources"]
    ]
    current_vod = int(include_vod)
    current_live = int(include_live)
    report = build_latest_report(
        context,
        status="pending",
        started_at=context.generated_at,
        finished_at=context.generated_at,
        due=True,
        forced=True,
        recovery_due=False,
        sources=report_sources,
        counts={
            "current_vod_sites": current_vod,
            "current_live_channels": current_live,
            "current_public_unverified": 2,
        },
        gate={
            "publish": True,
            "inconclusive": False,
            "release_kind": kind.value,
            "reasons": reasons,
            "mandatory_removal_ids": list(mandatory),
            "historical_deletions": [],
            "inputs": {
                "current_publishable_vod_items": current_vod,
                "current_healthy_live_urls": current_live,
                "current_vod_sites": current_vod,
                "current_live_channels": current_live,
            },
            "thresholds": {
                "minimum_vod_sites": 1,
                "minimum_live_channels": 1,
                "minimum_previous_items": 5,
                "max_new_failure_ratio": 0.2,
                "failed_groups_to_abort": 2,
            },
            "network_probes": [
                {
                    "group": group,
                    "passed": True,
                    "attempts": 1,
                    "elapsed_ms": 10,
                    "detail": "ok",
                }
                for group in (
                    "github_raw",
                    "dns_public",
                    "cloudflare_http",
                    "google_http",
                )
            ],
        },
        previous_release_head_sha=context.previous_head,
        candidate_ref=context.candidate_ref,
        content_identity=identity,
    )
    state = {
        "schema_version": "1.0.0",
        "status": "pending",
        "release_kind": kind.value,
        "generation": context.generation,
        "active_release_id": context.release_id,
        "last_publish_at": None,
        "last_success_at": None,
        "content_commit_sha": None,
        "previous_release_head_sha": context.previous_head,
        "workflow_run_id": context.workflow_run_id,
        "workflow_run_attempt": context.workflow_run_attempt,
    }
    bundle = build_bundle_files(
        context=context,
        client_artifacts=client,
        health=health,
        upstreams=(
            {
                "source_id": source.id,
                "fetch_mode": source.fetch.mode.value,
                "reviewed_revision": source.fetch.reviewed_revision,
                "resolved_revision": source.fetch.reviewed_revision,
                "resolved_fetch_url": str(source.fetch.reviewed_url),
                "terms_sha256": {
                    str(term.url or term.path): term.reviewed_sha256
                    for term in source.terms_watch
                },
            }
            for source in sources
        ),
        source_count=len(sources),
        supplemental_files={
            "state/release.json": canonical_json_bytes(state),
            "dist/reports/latest.json": canonical_json_bytes(report),
            "dist/reports/latest.md": render_latest_markdown(report),
        },
    )
    return build_publish_artifact(
        tmp_path / "artifact",
        context=context,
        bundle_files=bundle,
        mandatory_removal_ids=mandatory,
    )


def _manifest(root: Path) -> dict[str, Any]:
    value = json.loads((root / "bundle.json").read_bytes())
    assert isinstance(value, dict)
    return value


def _write_manifest(root: Path, value: object) -> None:
    (root / "bundle.json").write_bytes(canonical_json_bytes(value))


def _refresh_envelope_records(root: Path, relatives: tuple[str, ...]) -> None:
    manifest = _manifest(root)
    for relative in relatives:
        data = (root / "payload" / relative).read_bytes()
        record = next(item for item in manifest["files"] if item["path"] == relative)
        record["size"] = len(data)
        record["sha256"] = sha256(data).hexdigest()
    _write_manifest(root, manifest)


def _reseal_release_json(
    root: Path,
    relative: str,
    mutate: Any,
    *,
    alias: str | None = None,
) -> None:
    payload = root / "payload"
    document = json.loads((payload / relative).read_bytes())
    mutate(document)
    data = canonical_json_bytes(document)
    (payload / relative).write_bytes(data)
    if alias is not None:
        (payload / alias).write_bytes(data)

    root_manifest_path = payload / "dist/manifest.json"
    root_manifest = json.loads(root_manifest_path.read_bytes())
    release_manifest_relative = str(root_manifest["release_manifest"]["path"])
    release_manifest_path = payload / release_manifest_relative
    release_manifest = json.loads(release_manifest_path.read_bytes())
    digest = "sha256:" + sha256(data).hexdigest()
    release_manifest["artifacts"][relative] = digest
    if alias is not None:
        root_manifest["aliases"][alias] = digest
    release_manifest_bytes = canonical_json_bytes(release_manifest)
    release_manifest_path.write_bytes(release_manifest_bytes)
    root_manifest["release_manifest"]["sha256"] = (
        "sha256:" + sha256(release_manifest_bytes).hexdigest()
    )
    root_manifest_path.write_bytes(canonical_json_bytes(root_manifest))
    changed: tuple[str, ...] = (
        relative,
        release_manifest_relative,
        "dist/manifest.json",
    )
    if alias is not None:
        changed = (*changed, alias)
    _refresh_envelope_records(root, changed)


def _reseal_release_bytes(
    root: Path,
    relative: str,
    data: bytes,
    *,
    alias: str | None = None,
) -> None:
    payload = root / "payload"
    (payload / relative).write_bytes(data)
    if alias is not None:
        (payload / alias).write_bytes(data)
    root_manifest_path = payload / "dist/manifest.json"
    root_manifest = json.loads(root_manifest_path.read_bytes())
    release_manifest_relative = str(root_manifest["release_manifest"]["path"])
    release_manifest_path = payload / release_manifest_relative
    release_manifest = json.loads(release_manifest_path.read_bytes())
    digest = "sha256:" + sha256(data).hexdigest()
    release_manifest["artifacts"][relative] = digest
    if alias is not None:
        root_manifest["aliases"][alias] = digest
    release_manifest_bytes = canonical_json_bytes(release_manifest)
    release_manifest_path.write_bytes(release_manifest_bytes)
    root_manifest["release_manifest"]["sha256"] = (
        "sha256:" + sha256(release_manifest_bytes).hexdigest()
    )
    root_manifest_path.write_bytes(canonical_json_bytes(root_manifest))
    changed: tuple[str, ...] = (
        relative,
        release_manifest_relative,
        "dist/manifest.json",
    )
    if alias is not None:
        changed = (*changed, alias)
    _refresh_envelope_records(root, changed)


def _reseal_release_manifest(root: Path, mutate: Any) -> None:
    payload = root / "payload"
    root_manifest_path = payload / "dist/manifest.json"
    root_manifest = json.loads(root_manifest_path.read_bytes())
    relative = str(root_manifest["release_manifest"]["path"])
    release_manifest_path = payload / relative
    release_manifest = json.loads(release_manifest_path.read_bytes())
    mutate(release_manifest)
    data = canonical_json_bytes(release_manifest)
    release_manifest_path.write_bytes(data)
    root_manifest["release_manifest"]["sha256"] = "sha256:" + sha256(data).hexdigest()
    root_manifest_path.write_bytes(canonical_json_bytes(root_manifest))
    _refresh_envelope_records(root, (relative, "dist/manifest.json"))


def _trusted_repository(tmp_path: Path) -> Path:
    trusted = tmp_path / "trusted"
    shutil.copytree(SCHEMAS, trusted / "schemas")
    shutil.copytree(SCHEMAS.parent / "config", trusted / "config")
    shutil.copytree(SCHEMAS.parent / "sources", trusted / "sources")
    return trusted


def _colliding_direct_vod_fixture(
    tmp_path: Path,
) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    """Build two trusted direct sources whose base key and name collide."""

    base = next(
        source
        for source in load_registry(
            SCHEMAS.parent / "sources/registry.yaml",
            schema_path=SCHEMAS / "source-registry.schema.json",
        )
        if source.id == "ikun-vod"
    )
    assert base.client_site is not None
    sources = []
    for source_id, host in (("a-direct", "a.example.test"), ("b-direct", "b.example.test")):
        sources.append(
            replace(
                base,
                id=source_id,
                fetch=replace(
                    base.fetch,
                    reviewed_url=f"https://{host}/api.php/provide/vod",
                ),
                allowed_hosts=frozenset({host}),
                client_site=replace(base.client_site, key="same", name="Same"),
            )
        )

    release_root = tmp_path / "payload/dist/releases/g00000001"
    configs = release_root / "configs"
    configs.mkdir(parents=True)
    health_sources: list[dict[str, Any]] = []
    stable_sites: list[dict[str, Any]] = []
    for source in sources:
        assert source.fetch.reviewed_url is not None
        site = {
            "key": f"src_{source.id}_same",
            "name": f"⚠️ Same [{source.id}]",
            "type": 1,
            "api": source.fetch.reviewed_url,
            "searchable": 1,
            "quickSearch": 1,
            "filterable": 1,
            "changeable": 1,
            "categories": ["电影"],
        }
        document = {"sites": [site], "lives": [], "parses": []}
        (configs / f"{source.id}.json").write_bytes(canonical_json_bytes(document))
        stable_sites.append(site)
        health_sources.append(
            {
                "source_id": source.id,
                "items": [
                    {
                        "entity_id": (
                            "vod:"
                            f"{source.id}:"
                            f"{sha256(f'1{source.fetch.reviewed_url}'.encode()).hexdigest()[:16]}"
                        ),
                        "entity_type": "vod_site",
                        "publication_status": "stable",
                    }
                ],
            }
        )
    stable = {"sites": stable_sites, "lives": [], "parses": []}
    (configs / "stable.json").write_bytes(canonical_json_bytes(stable))
    health = {"sources": health_sources, "channels": []}
    trusted = {source.id: source for source in sources}
    return tmp_path / "payload", health, trusted


def _rewrite_payload_json(root: Path, relative: str, mutate: Any) -> None:
    target = root / "payload" / relative
    document = json.loads(target.read_bytes())
    mutate(document)
    target.write_bytes(canonical_json_bytes(document))
    manifest = _manifest(root)
    rewritten = [(relative, target.read_bytes())]
    if relative == "dist/reports/latest.json":
        markdown_relative = "dist/reports/latest.md"
        markdown = render_latest_markdown(document)
        (root / "payload" / markdown_relative).write_bytes(markdown)
        rewritten.append((markdown_relative, markdown))
    for rewritten_relative, data in rewritten:
        record = next(
            item for item in manifest["files"] if item["path"] == rewritten_relative
        )
        record["size"] = len(data)
        record["sha256"] = sha256(data).hexdigest()
    _write_manifest(root, manifest)


def test_valid_privileged_artifact_binds_trusted_gate_counts_and_root_hash(
    tmp_path: Path,
) -> None:
    root = _artifact(tmp_path)

    artifact = validate_publish_artifact(root, SCHEMAS)

    assert artifact.release_kind is ReleaseKind.BOOTSTRAP
    assert artifact.release_id == "g00000001"
    assert artifact.release_manifest_sha256.startswith("sha256:")
    assert artifact.root_manifest_sha256.startswith("sha256:")
    assert len(artifact.root_manifest_sha256) == 71


def test_trusted_direct_vod_collisions_are_rebuilt_globally(tmp_path: Path) -> None:
    payload, health, trusted = _colliding_direct_vod_fixture(tmp_path)

    _validate_source_kinds_and_client_configs(
        payload=payload,
        release_id="g00000001",
        health=health,
        trusted_sources=trusted,
        strict_client_rebuild=True,
    )


@pytest.mark.parametrize(
    ("relative", "field", "error"),
    [
        (
            "a-direct.json",
            "key",
            "source config differs from deterministic trusted rebuild",
        ),
        (
            "stable.json",
            "name",
            "stable config differs from deterministic trusted rebuild",
        ),
    ],
)
def test_trusted_direct_vod_collision_rewrite_cannot_be_forged(
    tmp_path: Path,
    relative: str,
    field: str,
    error: str,
) -> None:
    payload, health, trusted = _colliding_direct_vod_fixture(tmp_path)
    path = payload / f"dist/releases/g00000001/configs/{relative}"
    document = json.loads(path.read_bytes())
    document["sites"][0][field] = "forged"
    path.write_bytes(canonical_json_bytes(document))

    with pytest.raises(ContractError, match=error):
        _validate_source_kinds_and_client_configs(
            payload=payload,
            release_id="g00000001",
            health=health,
            trusted_sources=trusted,
            strict_client_rebuild=True,
        )


def test_safety_keeps_retained_collision_assignment_from_previous_release(
    tmp_path: Path,
) -> None:
    payload, health, _trusted = _colliding_direct_vod_fixture(tmp_path)
    configs = payload / "dist/releases/g00000001/configs"
    (configs / "b-direct.json").unlink()
    health["sources"] = [
        source for source in health["sources"] if source["source_id"] == "a-direct"
    ]
    retained = json.loads((configs / "a-direct.json").read_bytes())["sites"][0]
    (configs / "stable.json").write_bytes(
        canonical_json_bytes({"sites": [retained], "lives": [], "parses": []})
    )

    _validate_source_kinds_and_client_configs(
        payload=payload,
        release_id="g00000001",
        health=health,
        trusted_sources={},
        strict_client_rebuild=False,
    )

    stable = json.loads((configs / "stable.json").read_bytes())
    stable["sites"][0]["name"] = "forged"
    (configs / "stable.json").write_bytes(canonical_json_bytes(stable))
    with pytest.raises(ContractError, match="deterministic trusted rebuild"):
        _validate_source_kinds_and_client_configs(
            payload=payload,
            release_id="g00000001",
            health=health,
            trusted_sources={},
            strict_client_rebuild=False,
        )


@pytest.mark.parametrize(
    ("mutate", "error"),
    [
        (lambda report: report["gate"].update(publish=False), "explicitly publishable"),
        (lambda report: report["gate"].update(inconclusive=True), "explicitly publishable"),
        (
            lambda report: report["gate"]["thresholds"].update(minimum_vod_sites=2),
            "trusted policy",
        ),
        (lambda report: report["counts"].update(current_vod_sites=0), "current_vod_sites"),
        (
            lambda report: report["gate"]["inputs"].update(current_live_channels=0),
            "current_live_channels",
        ),
        (
            lambda report: report["gate"]["inputs"].update(
                current_publishable_vod_items=0
            ),
            "publishable VOD input",
        ),
        (
            lambda report: report["gate"]["inputs"].update(previous_vod_items=10),
            "VOD batch failure gate",
        ),
    ],
)
def test_privileged_artifact_rejects_tampered_gate_and_counts(
    tmp_path: Path,
    mutate: Any,
    error: str,
) -> None:
    root = _artifact(tmp_path)
    _rewrite_payload_json(root, "dist/reports/latest.json", mutate)

    with pytest.raises(ContractError, match=error):
        validate_publish_artifact(root, SCHEMAS)


def test_privileged_artifact_recomputes_health_rights_and_network_outage_gate(
    tmp_path: Path,
) -> None:
    root = _artifact(tmp_path / "rights")

    def change_rights(report: dict[str, Any]) -> None:
        source = next(item for item in report["sources"] if item["source_id"] == "ikun-vod")
        source["rights_status"] = "verified"
        source["change_summary"]["current"]["rights_status"] = "verified"
        report["counts"]["current_public_unverified"] = 1
        report["counts"]["current_verified"] = 1

    _rewrite_payload_json(root, "dist/reports/latest.json", change_rights)
    with pytest.raises(ContractError, match="differs from validated health"):
        validate_publish_artifact(root, SCHEMAS)

    root = _artifact(tmp_path / "network")

    def bypass_network(report: dict[str, Any]) -> None:
        report["gate"]["network_probes"][0]["passed"] = False
        report["gate"]["network_probes"][1]["passed"] = False
        report["gate"]["inputs"]["failed_network_groups"] = 2

    _rewrite_payload_json(root, "dist/reports/latest.json", bypass_network)
    with pytest.raises(ContractError, match="network outage gate"):
        validate_publish_artifact(root, SCHEMAS)


def test_report_may_audit_only_nonpublishable_sources_absent_from_health(
    tmp_path: Path,
) -> None:
    def add_report_only_source(report: dict[str, Any], publication: str) -> None:
        report["sources"].append(
            {
                "source_id": "zzz-report-only",
                "technical_status": "unknown",
                "publication_status": publication,
                "rights_status": "unknown",
                "failure_reason": None,
                "secondary_reasons": [],
                "upstream_revision": None,
                "change_summary": {
                    "category": "new",
                    "previous": None,
                    "current": {
                        "technical_status": "unknown",
                        "publication_status": publication,
                        "rights_status": "unknown",
                    },
                },
            }
        )

    root = _artifact(tmp_path / "withheld")
    _rewrite_payload_json(
        root,
        "dist/reports/latest.json",
        lambda report: add_report_only_source(report, "withheld"),
    )
    validate_publish_artifact(root, SCHEMAS)

    root = _artifact(tmp_path / "stable")
    _rewrite_payload_json(
        root,
        "dist/reports/latest.json",
        lambda report: add_report_only_source(report, "stable"),
    )
    with pytest.raises(ContractError, match="report-only current source"):
        validate_publish_artifact(root, SCHEMAS)

@pytest.mark.parametrize("missing", ["vod", "live"])
def test_bootstrap_artifact_must_meet_trusted_minimums(
    tmp_path: Path,
    missing: str,
) -> None:
    root = _artifact(
        tmp_path,
        include_vod=missing != "vod",
        include_live=missing != "live",
    )

    with pytest.raises(ContractError, match="trusted publication minimums"):
        validate_publish_artifact(root, SCHEMAS)


def test_safety_artifact_rejects_a_mandatory_source_retained_in_payload(
    tmp_path: Path,
) -> None:
    root = _artifact(
        tmp_path,
        ReleaseKind.SAFETY,
        mandatory=("source:ikun-vod",),
    )

    with pytest.raises(ContractError, match="mandatory source remains"):
        validate_publish_artifact(root, SCHEMAS)


@pytest.mark.parametrize(
    "mandatory_id",
    [
        "vod:ikun-vod:"
        + sha256(b"1https://ikunzyapi.com/api.php/provide/vod").hexdigest()[:16],
        "live-url:iptv-org-cn-cctv:"
        + sha256(b"https://live.example.test/index.m3u8").hexdigest()[:16],
    ],
)
def test_safety_artifact_rejects_retained_mandatory_vod_and_live_entities(
    tmp_path: Path,
    mandatory_id: str,
) -> None:
    root = _artifact(
        tmp_path,
        ReleaseKind.SAFETY,
        mandatory=(mandatory_id,),
    )

    with pytest.raises(ContractError, match="mandatory (?:VOD|live URL).* remains"):
        validate_publish_artifact(root, SCHEMAS)


def test_rollback_cannot_be_uploaded_as_a_publish_artifact(tmp_path: Path) -> None:
    with pytest.raises(ContractError, match="rollback cannot enter"):
        _artifact(tmp_path, ReleaseKind.ROLLBACK)


@pytest.mark.parametrize(
    "entry",
    [
        "source_ids: [ikun-vod]\n    hosts: []\n    urls: []",
        "source_ids: []\n    hosts: [ikunzyapi.com]\n    urls: []",
        "source_ids: []\n    hosts: []\n    urls: [https://ikunzyapi.com/api.php/provide/vod]",
    ],
)
def test_privileged_artifact_scans_trusted_denylist_by_source_host_and_url(
    tmp_path: Path,
    entry: str,
) -> None:
    root = _artifact(tmp_path / "candidate")
    trusted = _trusted_repository(tmp_path)
    (trusted / "sources/denylist.yaml").write_text(
        "version: 1\n"
        "entries:\n"
        "  - id: blocked-test\n"
        f"    {entry}\n"
        "    reason: security\n"
        "    requested_at: 2026-07-22\n"
        "    evidence_urls: []\n",
        encoding="utf-8",
    )

    with pytest.raises(ContractError, match="denylisted"):
        validate_publish_artifact(root, trusted / "schemas")


@pytest.mark.parametrize(
    ("old", "new", "error"),
    [
        ("  - id: ikun-vod", "  - id: foreign-vod", "active trusted registry"),
        ("    enabled: true", "    enabled: false", "active trusted registry"),
        (
            "    rights_status: public_unverified",
            "    rights_status: verified",
            "rights differ from trusted registry",
        ),
    ],
)
def test_privileged_artifact_binds_health_to_active_trusted_registry(
    tmp_path: Path, old: str, new: str, error: str
) -> None:
    root = _artifact(tmp_path / "candidate")
    trusted = _trusted_repository(tmp_path)
    registry_path = trusted / "sources/registry.yaml"
    registry = registry_path.read_text(encoding="utf-8")
    assert old in registry
    registry_path.write_text(registry.replace(old, new, 1), encoding="utf-8")

    with pytest.raises(ContractError, match=error):
        validate_publish_artifact(root, trusted / "schemas")


@pytest.mark.parametrize("rights", ["unknown", "restricted", "takedown"])
def test_privileged_artifact_rejects_stable_claim_under_nonpublishable_registry_rights(
    tmp_path: Path, rights: str
) -> None:
    root = _artifact(tmp_path / "candidate")
    trusted = _trusted_repository(tmp_path)
    registry_path = trusted / "sources/registry.yaml"
    registry = registry_path.read_text(encoding="utf-8")
    registry_path.write_text(
        registry.replace(
            "    rights_status: public_unverified",
            f"    rights_status: {rights}",
            1,
        ),
        encoding="utf-8",
    )

    def claim_nonpublishable_rights(health: dict[str, Any]) -> None:
        source = next(item for item in health["sources"] if item["source_id"] == "ikun-vod")
        source["rights_status"] = rights

    _reseal_release_json(
        root,
        "dist/releases/g00000001/health.json",
        claim_nonpublishable_rights,
        alias="dist/health.json",
    )

    with pytest.raises(
        ContractError,
        match="nonpublishable registry rights|aggregate status",
    ):
        validate_publish_artifact(root, trusted / "schemas")


def test_privileged_artifact_binds_selected_channel_rights_to_its_trusted_source(
    tmp_path: Path,
) -> None:
    root = _artifact(tmp_path)
    _reseal_release_json(
        root,
        "dist/releases/g00000001/health.json",
        lambda health: health["channels"][0].update(rights_status="verified"),
        alias="dist/health.json",
    )

    with pytest.raises(ContractError, match="channel (?:rights differ|aggregate status)"):
        validate_publish_artifact(root, SCHEMAS)


@pytest.mark.parametrize(
    "relative",
    [
        "dist/releases/g00000000/manifest.json",
        "dist/releases/g00000002/health.json",
        "dist/reports/debug.json",
    ],
)
def test_privileged_artifact_rejects_inactive_release_and_extra_payload_files(
    tmp_path: Path, relative: str
) -> None:
    root = _artifact(tmp_path)
    target = root / "payload" / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"{}\n")
    manifest = _manifest(root)
    manifest["files"].append(
        {
            "path": relative,
            "size": target.stat().st_size,
            "sha256": sha256(target.read_bytes()).hexdigest(),
        }
    )
    _write_manifest(root, manifest)

    with pytest.raises(ContractError, match="active manifest closure"):
        validate_publish_artifact(root, SCHEMAS)


@pytest.mark.parametrize(
    ("mutate", "error"),
    [
        (
            lambda item: item.update(technical_status="dead"),
            "not supported by publishable health|aggregate status",
        ),
        (
            lambda item: item["capabilities"].update(media_probe=False),
            "not supported by publishable health",
        ),
        (
            lambda item: item.update(
                technical_status="partial", publication_status="experimental"
            ),
            "stable config VOD site is not backed by stable healthy health|aggregate status",
        ),
    ],
)
def test_privileged_artifact_rejects_resealed_vod_health_status_attacks(
    tmp_path: Path, mutate: Any, error: str
) -> None:
    root = _artifact(tmp_path)

    def mutate_vod(health: dict[str, Any]) -> None:
        source = next(item for item in health["sources"] if item["source_id"] == "ikun-vod")
        item = next(item for item in source["items"] if item["entity_type"] == "vod_site")
        mutate(item)

    _reseal_release_json(
        root,
        "dist/releases/g00000001/health.json",
        mutate_vod,
        alias="dist/health.json",
    )

    with pytest.raises(ContractError, match=error):
        validate_publish_artifact(root, SCHEMAS)


def test_privileged_artifact_requires_canonical_reviewed_api_after_identity_binding(
    tmp_path: Path,
) -> None:
    root = _artifact(tmp_path)
    alternate = "https://IKUNZYAPI.COM:443/api.php/provide/vod#client-fragment"
    _reseal_release_json(
        root,
        "dist/releases/g00000001/configs/ikun-vod.json",
        lambda config: config["sites"][0].update(api=alternate),
    )
    _reseal_release_json(
        root,
        "dist/releases/g00000001/configs/stable.json",
        lambda config: config["sites"][0].update(api=alternate),
        alias="dist/configs/stable.json",
    )

    with pytest.raises(ContractError, match="client VOD API is not canonical"):
        validate_publish_artifact(root, SCHEMAS)


def test_privileged_artifact_rejects_client_vod_without_matching_health_entity(
    tmp_path: Path,
) -> None:
    root = _artifact(tmp_path)
    replacement = "https://other.example.test/api"
    _reseal_release_json(
        root,
        "dist/releases/g00000001/configs/ikun-vod.json",
        lambda config: config["sites"][0].update(api=replacement),
    )
    _reseal_release_json(
        root,
        "dist/releases/g00000001/configs/stable.json",
        lambda config: config["sites"][0].update(api=replacement),
        alias="dist/configs/stable.json",
    )

    with pytest.raises(ContractError, match="no matching health entity"):
        validate_publish_artifact(root, SCHEMAS)


def test_build_rejects_nonempty_target_and_unauthorized_deletions(tmp_path: Path) -> None:
    context = _context()
    client = build_client_artifacts(
        context=context,
        sources=[],
        vod_results=[],
        channels=[],
    )
    bundle = build_bundle_files(
        context=context,
        client_artifacts=client,
        health={
            "schema_version": "1.0.0",
            "generated_at": context.generated_at,
            "generation": 1,
            "release_id": context.release_id,
            "sources": [],
            "channels": [],
        },
        source_count=0,
    )
    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "keep").write_text("x", encoding="utf-8")
    with pytest.raises(ContractError, match="must be empty"):
        build_publish_artifact(occupied, context=context, bundle_files=bundle)
    with pytest.raises(ContractError, match="bootstrap artifacts"):
        build_publish_artifact(
            tmp_path / "regular-delete",
            context=context,
            bundle_files=bundle,
            deletions=("dist/releases/g00000001",),
        )

    safety = _context(ReleaseKind.SAFETY)
    with pytest.raises(ContractError, match="exact release directory"):
        build_publish_artifact(
            tmp_path / "unsafe-delete",
            context=safety,
            bundle_files=bundle,
            deletions=("dist/releases",),
            mandatory_removal_ids=("source:removed",),
        )


def test_file_inventory_rejects_symlinks_and_non_payload_paths(tmp_path: Path) -> None:
    root = tmp_path / "payload"
    root.mkdir()
    (root / "outside.txt").write_text("x", encoding="utf-8")
    with pytest.raises(ContractError, match="path is not allowed"):
        _file_records(root)

    (root / "outside.txt").unlink()
    (root / "dist").mkdir()
    target = root / "target"
    target.write_text("x", encoding="utf-8")
    (root / "dist/link.json").symlink_to(target)
    with pytest.raises(SecurityError, match="contains symlink"):
        _file_records(root)


@pytest.mark.parametrize(
    ("mutate", "error"),
    [
        (lambda value: value.update(schema_version="2.0.0"), "keys/version"),
        (lambda value: value.update(files={}), "files must be an array"),
        (lambda value: value["files"].append("bad"), "file record is invalid"),
        (
            lambda value: value["files"].append(dict(value["files"][0])),
            "missing or duplicated",
        ),
        (lambda value: value["files"][0].update(path="../escape"), "path is unsafe"),
        (lambda value: value["files"][0].update(size=-1), "size mismatch"),
        (lambda value: value["files"][0].update(sha256="0" * 64), "hash mismatch"),
        (lambda value: value.update(deletions="bad"), "deletions are invalid"),
        (lambda value: value.update(mandatory_removal_ids=[1]), "removal IDs are invalid"),
        (
            lambda value: value.update(deletions=["dist/releases/g00000001"]),
            "bootstrap artifact requests deletion",
        ),
    ],
)
def test_manifest_envelope_tampering_is_rejected(
    tmp_path: Path,
    mutate: Any,
    error: str,
) -> None:
    root = _artifact(tmp_path)
    manifest = _manifest(root)
    mutate(manifest)
    _write_manifest(root, manifest)

    with pytest.raises((ContractError, SecurityError), match=error):
        validate_publish_artifact(root, SCHEMAS)


def test_manifest_must_be_object_and_envelope_complete(tmp_path: Path) -> None:
    root = _artifact(tmp_path)
    _write_manifest(root, [])
    with pytest.raises(ContractError, match="manifest must be an object"):
        validate_publish_artifact(root, SCHEMAS)

    root = _artifact(tmp_path / "second")
    (root / "bundle.json").unlink()
    with pytest.raises(ContractError, match="envelope is incomplete"):
        validate_publish_artifact(root, SCHEMAS)


def test_payload_file_missing_symlink_extra_and_total_limit_are_rejected(tmp_path: Path) -> None:
    root = _artifact(tmp_path / "missing")
    manifest = _manifest(root)
    missing = root / "payload" / manifest["files"][0]["path"]
    missing.unlink()
    with pytest.raises(SecurityError, match="file is missing or unsafe"):
        validate_publish_artifact(root, SCHEMAS)

    root = _artifact(tmp_path / "symlink")
    manifest = _manifest(root)
    linked = root / "payload" / manifest["files"][0]["path"]
    linked.unlink()
    linked.symlink_to(root / "bundle.json")
    with pytest.raises(SecurityError, match="file is missing or unsafe"):
        validate_publish_artifact(root, SCHEMAS)

    root = _artifact(tmp_path / "extra")
    extra = root / "payload/dist/undeclared.txt"
    extra.write_text("not declared", encoding="utf-8")
    with pytest.raises(ContractError, match="file closure"):
        validate_publish_artifact(root, SCHEMAS)

    root = _artifact(tmp_path / "limit")
    with pytest.raises(ContractError, match="total size"):
        validate_publish_artifact(root, SCHEMAS, max_total_bytes=1)


@pytest.mark.parametrize(
    ("relative", "mutate", "error"),
    [
        (
            "state/release.json",
            lambda value: value.update(status="success"),
            "publication statuses differ",
        ),
        (
            "state/release.json",
            lambda value: value.update(workflow_run_id="different"),
            "event identities differ",
        ),
        (
            "dist/reports/latest.json",
            lambda value: value.update(workflow_run_attempt=99),
            "event identities differ",
        ),
    ],
)
def test_pending_state_and_report_identity_is_revalidated_after_hash_update(
    tmp_path: Path,
    relative: str,
    mutate: Any,
    error: str,
) -> None:
    root = _artifact(tmp_path)
    _rewrite_payload_json(root, relative, mutate)

    with pytest.raises(ContractError, match=error):
        validate_publish_artifact(root, SCHEMAS)


def test_generation_and_exact_safety_deletion_are_checked(tmp_path: Path) -> None:
    root = _artifact(tmp_path / "generation")
    manifest = _manifest(root)
    manifest["generation"] = 2
    _write_manifest(root, manifest)
    with pytest.raises(ContractError, match="generation"):
        validate_publish_artifact(root, SCHEMAS)

    root = _artifact(tmp_path / "deletion", ReleaseKind.SAFETY)
    manifest = _manifest(root)
    manifest["deletions"] = ["dist/releases/not-a-release"]
    _write_manifest(root, manifest)
    with pytest.raises(ContractError, match="outside an exact release directory"):
        validate_publish_artifact(root, SCHEMAS)


def test_outer_release_kind_and_expected_head_are_bound_to_payload(tmp_path: Path) -> None:
    root = _artifact(tmp_path / "kind")
    manifest = _manifest(root)
    manifest["release_kind"] = "safety"
    _write_manifest(root, manifest)
    with pytest.raises(ContractError, match="non-bootstrap artifact"):
        validate_publish_artifact(root, SCHEMAS)

    root = _artifact(tmp_path / "head")
    manifest = _manifest(root)
    manifest["expected_previous_head"] = "a" * 40
    _write_manifest(root, manifest)
    with pytest.raises(ContractError, match="bootstrap artifact"):
        validate_publish_artifact(root, SCHEMAS)


def test_outer_deletions_and_mandatory_removals_are_bound_to_gate(tmp_path: Path) -> None:
    root = _artifact(tmp_path / "deletions", ReleaseKind.SAFETY)
    manifest = _manifest(root)
    manifest["deletions"] = ["dist/releases/g00000001"]
    _write_manifest(root, manifest)
    with pytest.raises(ContractError, match="deletions differ"):
        validate_publish_artifact(root, SCHEMAS)

    root = _artifact(tmp_path / "mandatory")
    _rewrite_payload_json(
        root,
        "dist/reports/latest.json",
        lambda report: report["gate"].update(mandatory_removal_ids=["source:blocked"]),
    )
    manifest = _manifest(root)
    manifest["mandatory_removal_ids"] = ["source:blocked"]
    _write_manifest(root, manifest)
    with pytest.raises(ContractError, match="require a safety artifact"):
        validate_publish_artifact(root, SCHEMAS)


def test_regular_artifact_may_request_trusted_historical_cleanup(tmp_path: Path) -> None:
    root = _artifact(tmp_path, ReleaseKind.REGULAR)
    deletion = "dist/releases/g00000001"
    _rewrite_payload_json(
        root,
        "dist/reports/latest.json",
        lambda report: report["gate"].update(historical_deletions=[deletion]),
    )
    manifest = _manifest(root)
    manifest["deletions"] = [deletion]
    _write_manifest(root, manifest)

    artifact = validate_publish_artifact(root, SCHEMAS)

    assert artifact.release_kind is ReleaseKind.REGULAR
    assert artifact.deletions == (deletion,)


def _seal_mutated_client_config(root: Path, api: str) -> None:
    payload = root / "payload"
    release_relative = "dist/releases/g00000001/configs/stable.json"
    alias_relative = "dist/configs/stable.json"
    config = json.loads((payload / release_relative).read_bytes())
    config["sites"] = [
        {
            "key": "mutated",
            "name": "Mutated",
            "type": 1,
            "api": api,
            "searchable": 1,
            "quickSearch": 1,
            "filterable": 0,
            "changeable": 0,
        }
    ]
    config_bytes = canonical_json_bytes(config)
    (payload / release_relative).write_bytes(config_bytes)
    (payload / alias_relative).write_bytes(config_bytes)

    root_manifest_path = payload / "dist/manifest.json"
    root_manifest = json.loads(root_manifest_path.read_bytes())
    release_manifest_relative = root_manifest["release_manifest"]["path"]
    release_manifest_path = payload / release_manifest_relative
    release_manifest = json.loads(release_manifest_path.read_bytes())
    config_digest = "sha256:" + sha256(config_bytes).hexdigest()
    release_manifest["artifacts"][release_relative] = config_digest
    root_manifest["aliases"][alias_relative] = config_digest
    release_manifest_bytes = canonical_json_bytes(release_manifest)
    release_manifest_path.write_bytes(release_manifest_bytes)
    root_manifest["release_manifest"]["sha256"] = (
        "sha256:" + sha256(release_manifest_bytes).hexdigest()
    )
    root_manifest_bytes = canonical_json_bytes(root_manifest)
    root_manifest_path.write_bytes(root_manifest_bytes)

    envelope = _manifest(root)
    for relative in (
        release_relative,
        alias_relative,
        release_manifest_relative,
        "dist/manifest.json",
    ):
        data = (payload / relative).read_bytes()
        record = next(item for item in envelope["files"] if item["path"] == relative)
        record["size"] = len(data)
        record["sha256"] = sha256(data).hexdigest()
    _write_manifest(root, envelope)


@pytest.mark.parametrize(
    "api",
    [
        "https://example.test/api?to_ken=visible",
        "https://example.test/api?ｔｏ＿ｋｅｎ=visible",
        "https://127.0.0.1/api",
        "https://[::1]/api",
    ],
)
def test_privileged_artifact_validation_rejects_fully_resealed_unsafe_client_url(
    tmp_path: Path,
    api: str,
) -> None:
    root = _artifact(tmp_path)
    _seal_mutated_client_config(root, api)

    with pytest.raises(SecurityError, match="credential-free HTTPS"):
        validate_publish_artifact(root, SCHEMAS)


@pytest.mark.parametrize(
    ("source_id", "field", "value", "error"),
    [
        (
            "ikun-vod",
            "resolved_fetch_url",
            "https://example.com/unreviewed",
            "resolved_fetch_url differs",
        ),
        (
            "iptv-org-cn-cctv",
            "reviewed_revision",
            "b" * 40,
            "reviewed_revision differs",
        ),
        (
            "iptv-org-cn-cctv",
            "resolved_fetch_url",
            "https://raw.githubusercontent.com/iptv-org/iptv/"
            + "b" * 40
            + "/streams/cn_cctv.m3u",
            "resolved_fetch_url differs",
        ),
    ],
)
def test_privileged_artifact_rejects_fully_resealed_upstream_claims(
    tmp_path: Path,
    source_id: str,
    field: str,
    value: str,
    error: str,
) -> None:
    root = _artifact(tmp_path)

    def mutate(manifest: dict[str, Any]) -> None:
        upstream = next(
            item for item in manifest["upstreams"] if item["source_id"] == source_id
        )
        upstream[field] = value

    _reseal_release_manifest(root, mutate)

    with pytest.raises(ContractError, match=error):
        validate_publish_artifact(root, SCHEMAS)


def test_privileged_artifact_rejects_fully_resealed_terms_digest(
    tmp_path: Path,
) -> None:
    root = _artifact(tmp_path)

    def mutate(manifest: dict[str, Any]) -> None:
        upstream = next(
            item for item in manifest["upstreams"] if item["source_id"] == "ikun-vod"
        )
        identity = next(iter(upstream["terms_sha256"]))
        upstream["terms_sha256"][identity] = "0" * 64

    _reseal_release_manifest(root, mutate)

    with pytest.raises(ContractError, match="terms hashes differ"):
        validate_publish_artifact(root, SCHEMAS)


def test_safety_artifact_defers_previous_fetch_binding_to_publisher(tmp_path: Path) -> None:
    root = _artifact(tmp_path, ReleaseKind.SAFETY)

    def mutate(manifest: dict[str, Any]) -> None:
        upstream = next(
            item for item in manifest["upstreams"] if item["source_id"] == "ikun-vod"
        )
        upstream["resolved_fetch_url"] = "https://example.com/unreviewed"

    _reseal_release_manifest(root, mutate)

    # The unprivileged artifact verifier cannot know the exact generated parent.
    # Safety retains previous facts even when main's registry changed; the
    # Publisher must prove this record is byte-for-byte inherited from that
    # parent while Artifact still enforces URL/credential/closure safety.
    validate_publish_artifact(root, SCHEMAS)


def test_privileged_artifact_rebuilds_m3u_metadata_byte_for_byte(tmp_path: Path) -> None:
    root = _artifact(tmp_path)
    relative = "dist/releases/g00000001/live/stable.m3u"
    original = (root / "payload" / relative).read_text(encoding="utf-8")
    lines = original.splitlines()
    extinf = next(index for index, line in enumerate(lines) if line.startswith("#EXTINF:"))
    lines[extinf] = lines[extinf].replace(
        ",",
        ' group-title="Collector forged",',
        1,
    )
    forged = ("\n".join(lines) + "\n").encode()
    _reseal_release_bytes(
        root,
        relative,
        forged,
        alias="dist/live/stable.m3u",
    )

    with pytest.raises(ContractError, match="canonical validated health"):
        validate_publish_artifact(root, SCHEMAS)


def test_privileged_artifact_rejects_resealed_live_identity_and_private_logo(
    tmp_path: Path,
) -> None:
    root = _artifact(tmp_path / "identity")

    def forge_identity(health: dict[str, Any]) -> None:
        source = next(
            item for item in health["sources"] if item["source_id"] == "iptv-org-cn-cctv"
        )
        item = next(item for item in source["items"] if item["entity_type"] == "live_url")
        old = item["entity_id"]
        item["entity_id"] = "live-url:iptv-org-cn-cctv:" + "0" * 16
        channel = health["channels"][0]
        channel["candidate_url_ids"] = [item["entity_id"]]
        if channel["selected_url_id"] == old:
            channel["selected_url_id"] = item["entity_id"]

    _reseal_release_json(
        root,
        "dist/releases/g00000001/health.json",
        forge_identity,
        alias="dist/health.json",
    )
    with pytest.raises(ContractError, match="entity_id differs from normalized_url"):
        validate_publish_artifact(root, SCHEMAS)

    root = _artifact(tmp_path / "logo")

    def forge_logo(health: dict[str, Any]) -> None:
        source = next(
            item for item in health["sources"] if item["source_id"] == "iptv-org-cn-cctv"
        )
        item = next(item for item in source["items"] if item["entity_type"] == "live_url")
        item["logo"] = "https://127.0.0.1/logo.png"

    _reseal_release_json(
        root,
        "dist/releases/g00000001/health.json",
        forge_logo,
        alias="dist/health.json",
    )
    with pytest.raises(SecurityError):
        validate_publish_artifact(root, SCHEMAS)


@pytest.mark.parametrize(
    "field",
    ["searchable", "quickSearch", "filterable", "changeable"],
)
def test_stable_and_independent_vod_metadata_are_registry_bound(
    tmp_path: Path,
    field: str,
) -> None:
    root = _artifact(tmp_path)
    for relative, alias in (
        ("dist/releases/g00000001/configs/ikun-vod.json", None),
        ("dist/releases/g00000001/configs/stable.json", "dist/configs/stable.json"),
    ):
        _reseal_release_json(
            root,
            relative,
            lambda config, field=field: config["sites"][0].update(
                {field: 1 - int(config["sites"][0][field])}
            ),
            alias=alias,
        )

    with pytest.raises(ContractError, match="client fields differ from registry"):
        validate_publish_artifact(root, SCHEMAS)


def test_stable_config_is_exact_deterministic_subset_of_source_configs(
    tmp_path: Path,
) -> None:
    root = _artifact(tmp_path)
    _reseal_release_json(
        root,
        "dist/releases/g00000001/configs/stable.json",
        lambda config: config["sites"][0].update(name="⚠️ Forged aggregate name"),
        alias="dist/configs/stable.json",
    )

    with pytest.raises(ContractError, match="deterministic trusted rebuild"):
        validate_publish_artifact(root, SCHEMAS)


def test_report_change_category_is_recomputed_from_claimed_snapshots(
    tmp_path: Path,
) -> None:
    root = _artifact(tmp_path)
    _rewrite_payload_json(
        root,
        "dist/reports/latest.json",
        lambda report: report["sources"][0]["change_summary"].update(
            category="unchanged"
        ),
    )

    with pytest.raises(ContractError, match="change_summary category"):
        validate_publish_artifact(root, SCHEMAS)
