from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from ds_tvbox.artifact import validate_publish_artifact
from ds_tvbox.collector import CollectResult, NetworkProbeObservation, SourceObservation
from ds_tvbox.errors import InconclusiveError
from ds_tvbox.http import HttpRequest, HttpResponse
from ds_tvbox.live import select_channels
from ds_tvbox.models import (
    FailureReason,
    LiveCandidate,
    LiveProbeResult,
    MediaProbeResult,
    PublicationStatus,
    TechnicalStatus,
    VodCapabilities,
    VodProbeResult,
    VodSiteCandidate,
)
from ds_tvbox.pipeline import collect_publish_artifact
from ds_tvbox.publisher import Publisher

PROJECT = Path(__file__).resolve().parents[2]


def _git(path: Path, *arguments: str) -> str:
    result = subprocess.run(  # noqa: S603 - fixed executable and test-owned argv
        ["/usr/bin/git", *arguments],
        cwd=path,
        check=True,
        text=True,
        capture_output=True,
    )
    return result.stdout.strip()


def _repository(tmp_path: Path) -> tuple[Path, Path]:
    repository = tmp_path / "repository"
    remote = tmp_path / "origin.git"
    repository.mkdir()
    _git(tmp_path, "init", "--bare", str(remote))
    _git(repository, "init", "-b", "main")
    _git(repository, "config", "user.name", "test")
    _git(repository, "config", "user.email", "test@example.invalid")
    _git(repository, "remote", "add", "origin", str(remote))
    for relative in ("config", "schemas", "sources"):
        shutil.copytree(PROJECT / relative, repository / relative)
    (repository / "README.md").write_text("fixture\n", encoding="utf-8")
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "fixture")
    _git(repository, "push", "-u", "origin", "main")
    return repository, remote


class _UnusedFetcher:
    def fetch(self, request: HttpRequest) -> HttpResponse:
        del request
        raise AssertionError("pipeline should use the injected collection result")


class _LocalRaw:
    owner = "azhansy"
    repository = "ds-tvbox"

    def __init__(self, remote: Path) -> None:
        self.remote = remote

    def poll_revision(self, revision: str, **kwargs: object) -> None:
        del kwargs
        assert _git(self.remote, "cat-file", "-t", revision) == "commit"

    def poll_bare(self, *, ref: str = "generated", **kwargs: object) -> None:
        del kwargs
        assert _git(self.remote, "show-ref", "--verify", f"refs/heads/{ref}")

    def poll_absent(self, ref: str, paths: tuple[str, ...]) -> None:
        for path in paths:
            result = subprocess.run(  # noqa: S603 - fixed executable and test-owned argv
                ["/usr/bin/git", "show", f"{ref}:{path}"],
                cwd=self.remote,
                check=False,
                text=True,
                capture_output=True,
            )
            assert result.returncode != 0


def _healthy_collection(**kwargs: Any) -> CollectResult:
    sources = tuple(kwargs["sources"])
    vod_source = next(source for source in sources if source.id == "ikun-vod")
    live_source = next(source for source in sources if source.kind.value == "live_playlist")
    assert vod_source.client_site is not None
    site = vod_source.client_site
    vod = VodProbeResult(
        candidate=VodSiteCandidate(
            source_id=vod_source.id,
            key=site.key,
            name=site.name,
            type=1,
            api=vod_source.fetch.reviewed_url or "",
            searchable=site.searchable,
            quick_search=site.quick_search,
            filterable=site.filterable,
            changeable=site.changeable,
            categories=("电影",),
            rights_status=vod_source.rights_status,
        ),
        technical_status=TechnicalStatus.HEALTHY,
        publication_status=PublicationStatus.STABLE,
        capabilities=VodCapabilities(True, True, True, True, True),
        failure_reason=None,
    )
    live_candidate = LiveCandidate(
        source_id=live_source.id,
        name="CCTV-1",
        original_url="https://media.example.test/cctv1.m3u8",
        normalized_url="https://media.example.test/cctv1.m3u8",
        rights_status=live_source.rights_status,
        tvg_id="CCTV1.cn",
    )
    live = LiveProbeResult(
        candidate=live_candidate,
        technical_status=TechnicalStatus.HEALTHY,
        publication_status=PublicationStatus.STABLE,
        media=MediaProbeResult(
            ok=True,
            final_url=live_candidate.normalized_url,
            response_ms=120,
            media_path_score=2,
        ),
        consecutive_successes=1,
        consecutive_failures=0,
        last_success_at=str(kwargs["checked_at"]),
        failure_reason=None,
    )
    observations = (
        SourceObservation(
            source_id=vod_source.id,
            fetch_mode="direct_url",
            resolved_revision=None,
            resolved_fetch_url=vod_source.fetch.reviewed_url,
            content_sha256="1" * 64,
            terms_sha256={
                str(vod_source.terms_watch[0].url): vod_source.terms_watch[0].reviewed_sha256
            },
            technical_status=TechnicalStatus.HEALTHY,
            failure_reason=None,
            secondary_reasons=(),
            enumerated=True,
        ),
        SourceObservation(
            source_id=live_source.id,
            fetch_mode="github_tracked_file",
            resolved_revision=live_source.fetch.reviewed_revision,
            resolved_fetch_url=live_source.fetch.reviewed_url,
            content_sha256="2" * 64,
            terms_sha256={
                str(term.path): term.reviewed_sha256 for term in live_source.terms_watch
            },
            technical_status=TechnicalStatus.HEALTHY,
            failure_reason=None,
            secondary_reasons=(),
            enumerated=True,
        ),
    )
    probes = tuple(
        NetworkProbeObservation(group, True, 1, 10, "ok")
        for group in ("github_raw", "dns_public", "cloudflare_http", "google_http")
    )
    return CollectResult(
        checked_at=str(kwargs["checked_at"]),
        sources=sources,
        vod_results=(vod,),
        live_results=(live,),
        selected_channels=select_channels((live,)),
        source_observations=observations,
        catalog_results=(),
        discarded_entities=(),
        upstream_revisions={
            vod_source.id: None,
            live_source.id: live_source.fetch.reviewed_revision,
        },
        source_failures={},
        enumerated_source_ids=frozenset({vod_source.id, live_source.id}),
        network_probes=probes,
        failed_network_groups=0,
    )


def _healthy_collection_with_live_backup(**kwargs: Any) -> CollectResult:
    result = _healthy_collection(**kwargs)
    original = result.live_results[0]
    primary_candidate = replace(
        original.candidate,
        original_url="https://entry.example.test/primary.m3u8",
        normalized_url="https://entry.example.test/primary.m3u8",
    )
    primary = replace(
        original,
        candidate=primary_candidate,
        media=replace(
            original.media,
            final_url="https://cdn.example.test/primary.m3u8",
        ),
        consecutive_successes=5,
    )
    backup_candidate = replace(
        original.candidate,
        original_url="https://entry.example.test/backup.m3u8",
        normalized_url="https://entry.example.test/backup.m3u8",
    )
    backup = replace(
        original,
        candidate=backup_candidate,
        media=replace(
            original.media,
            final_url="https://cdn.example.test/backup.m3u8",
        ),
        consecutive_successes=2,
    )
    live_results = (primary, backup)
    return replace(
        result,
        live_results=live_results,
        selected_channels=select_channels(live_results),
    )


def _live_only_collection(**kwargs: Any) -> CollectResult:
    sources = tuple(kwargs["sources"])
    live_source = next(source for source in sources if source.kind.value == "live_playlist")
    candidate = LiveCandidate(
        source_id=live_source.id,
        name="CURRENT-ONLY",
        original_url="https://media.example.test/current-only.m3u8",
        normalized_url="https://media.example.test/current-only.m3u8",
        rights_status=live_source.rights_status,
        tvg_id="CCTV1.cn",
    )
    live = LiveProbeResult(
        candidate=candidate,
        technical_status=TechnicalStatus.HEALTHY,
        publication_status=PublicationStatus.STABLE,
        media=MediaProbeResult(True, candidate.normalized_url, 80, 2),
        consecutive_successes=1,
        consecutive_failures=0,
        last_success_at=str(kwargs["checked_at"]),
        failure_reason=None,
    )
    observation = SourceObservation(
        source_id=live_source.id,
        fetch_mode="github_tracked_file",
        resolved_revision=live_source.fetch.reviewed_revision,
        resolved_fetch_url=live_source.fetch.reviewed_url,
        content_sha256="2" * 64,
        terms_sha256={
            str(term.path): term.reviewed_sha256 for term in live_source.terms_watch
        },
        technical_status=TechnicalStatus.HEALTHY,
        failure_reason=None,
        secondary_reasons=(),
        enumerated=True,
    )
    probes = tuple(
        NetworkProbeObservation(group, True, 1, 10, "ok")
        for group in ("github_raw", "dns_public", "cloudflare_http", "google_http")
    )
    return CollectResult(
        checked_at=str(kwargs["checked_at"]),
        sources=sources,
        vod_results=(),
        live_results=(live,),
        selected_channels=select_channels((live,)),
        source_observations=(observation,),
        catalog_results=(),
        discarded_entities=(),
        upstream_revisions={live_source.id: live_source.fetch.reviewed_revision},
        source_failures={},
        enumerated_source_ids=frozenset({live_source.id}),
        network_probes=probes,
        failed_network_groups=0,
    )


def test_bootstrap_pipeline_builds_isolated_publish_and_candidate_artifacts(
    tmp_path: Path, monkeypatch: Any
) -> None:
    repository, _remote = _repository(tmp_path)
    monkeypatch.setattr("ds_tvbox.pipeline.collect_sources", _healthy_collection)
    output = tmp_path / "action-artifact"

    result = collect_publish_artifact(
        repository=repository,
        output=output,
        force=False,
        bootstrap=True,
        http_client=_UnusedFetcher(),
        clock=lambda: datetime(2026, 7, 22, 12, 0, tzinfo=UTC),
        environment={"GITHUB_RUN_ID": "123456", "GITHUB_RUN_ATTEMPT": "2"},
    )

    assert result == output
    artifact = validate_publish_artifact(output / "publish", repository / "schemas")
    assert artifact.release_id == "g00000001"
    stable = json.loads(
        (artifact.payload_root / "dist/configs/stable.json").read_text(encoding="utf-8")
    )
    assert len(stable["sites"]) == 1
    assert len(stable["lives"]) == 1
    candidates = json.loads(
        (output / "reports/candidates.json").read_text(encoding="utf-8")
    )
    assert candidates == {
        "schema_version": "1.0.0",
        "workflow_run_id": "123456",
        "workflow_run_attempt": 2,
        "catalogs": [],
        "candidates": [],
    }
    assert not (artifact.payload_root / "reports/candidates.json").exists()


def test_inconclusive_collection_writes_diagnostics_without_publish_payload(
    tmp_path: Path, monkeypatch: Any
) -> None:
    repository, _remote = _repository(tmp_path)

    def no_live_collection(**kwargs: Any) -> CollectResult:
        result = _healthy_collection(**kwargs)
        return replace(result, live_results=(), selected_channels=())

    monkeypatch.setattr("ds_tvbox.pipeline.collect_sources", no_live_collection)
    output = tmp_path / "inconclusive-artifact"

    with pytest.raises(InconclusiveError, match="live_zero"):
        collect_publish_artifact(
            repository=repository,
            output=output,
            force=False,
            bootstrap=True,
            http_client=_UnusedFetcher(),
            clock=lambda: datetime(2026, 7, 22, 12, 0, tzinfo=UTC),
            environment={"GITHUB_RUN_ID": "124", "GITHUB_RUN_ATTEMPT": "1"},
        )

    report = json.loads((output / "reports/latest.json").read_text(encoding="utf-8"))
    assert report["status"] == "inconclusive"
    assert report["gate"]["publish"] is False
    assert "live_zero" in report["gate"]["reasons"]
    assert (output / "reports/latest.md").is_file()
    assert (output / "reports/candidates.json").is_file()
    assert not (output / "publish").exists()


def test_pipeline_uses_one_vod_deduplication_for_health_gate_and_client(
    tmp_path: Path, monkeypatch: Any
) -> None:
    repository, _remote = _repository(tmp_path)

    def duplicate_vod_collection(**kwargs: Any) -> CollectResult:
        result = _healthy_collection(**kwargs)
        duplicate = replace(
            result.vod_results[0],
            candidate=replace(
                result.vod_results[0].candidate,
                key="duplicate-alias",
                name="Duplicate Alias",
            ),
        )
        return replace(result, vod_results=(duplicate, *result.vod_results))

    monkeypatch.setattr("ds_tvbox.pipeline.collect_sources", duplicate_vod_collection)
    output = tmp_path / "deduplicated-artifact"
    collect_publish_artifact(
        repository=repository,
        output=output,
        force=False,
        bootstrap=True,
        http_client=_UnusedFetcher(),
        clock=lambda: datetime(2026, 7, 22, 12, 0, tzinfo=UTC),
        environment={"GITHUB_RUN_ID": "125", "GITHUB_RUN_ATTEMPT": "1"},
    )

    artifact = validate_publish_artifact(output / "publish", repository / "schemas")
    health = json.loads(
        (artifact.payload_root / "dist/health.json").read_text(encoding="utf-8")
    )
    vod_items = [
        item
        for source in health["sources"]
        for item in source["items"]
        if item["entity_type"] == "vod_site"
    ]
    report = json.loads(
        (artifact.payload_root / "dist/reports/latest.json").read_text(encoding="utf-8")
    )
    stable = json.loads(
        (artifact.payload_root / "dist/configs/stable.json").read_text(encoding="utf-8")
    )
    assert len(vod_items) == 1
    assert report["gate"]["inputs"]["current_publishable_vod_items"] == 1
    assert report["gate"]["inputs"]["current_vod_sites"] == 1
    assert len(stable["sites"]) == 1


def test_safety_artifact_is_pure_subtraction_of_previous_release(
    tmp_path: Path, monkeypatch: Any
) -> None:
    repository, remote = _repository(tmp_path)
    monkeypatch.setattr("ds_tvbox.pipeline.collect_sources", _healthy_collection)
    first_output = tmp_path / "first-artifact"
    collect_publish_artifact(
        repository=repository,
        output=first_output,
        force=False,
        bootstrap=True,
        http_client=_UnusedFetcher(),
        clock=lambda: datetime(2026, 7, 22, 12, 0, tzinfo=UTC),
        environment={"GITHUB_RUN_ID": "100", "GITHUB_RUN_ATTEMPT": "1"},
    )
    Publisher(
        repository=repository,
        schemas_dir=repository / "schemas",
        raw_verifier=_LocalRaw(remote),  # type: ignore[arg-type]
        now=lambda: datetime(2026, 7, 22, 12, 5, tzinfo=UTC),
        environment={"GITHUB_RUN_ID": "100", "GITHUB_RUN_ATTEMPT": "1"},
    ).publish(first_output / "publish")

    def changed_collection(**kwargs: Any) -> CollectResult:
        current = _healthy_collection(**kwargs)
        old_live = current.live_results[0]
        new_candidate = replace(
            old_live.candidate,
            name="NEW-CURRENT-ONLY",
            original_url="https://media.example.test/new-current-only.m3u8",
            normalized_url="https://media.example.test/new-current-only.m3u8",
        )
        new_live = replace(
            old_live,
            candidate=new_candidate,
            media=replace(old_live.media, final_url=new_candidate.normalized_url),
        )
        return replace(
            current,
            live_results=(old_live, new_live),
            selected_channels=select_channels((old_live, new_live)),
        )

    (repository / "sources/denylist.yaml").write_text(
        """version: 1
entries:
  - id: remove-ikun
    source_ids: [ikun-vod]
    hosts: []
    urls: []
    reason: takedown
    requested_at: 2026-07-22
    evidence_urls: []
""",
        encoding="utf-8",
    )
    monkeypatch.setattr("ds_tvbox.pipeline.collect_sources", changed_collection)
    second_output = tmp_path / "second-artifact"

    collect_publish_artifact(
        repository=repository,
        output=second_output,
        force=True,
        bootstrap=False,
        http_client=_UnusedFetcher(),
        clock=lambda: datetime(2026, 7, 22, 13, 0, tzinfo=UTC),
        environment={"GITHUB_RUN_ID": "101", "GITHUB_RUN_ATTEMPT": "1"},
    )

    artifact = validate_publish_artifact(second_output / "publish", repository / "schemas")
    assert artifact.release_kind.value == "safety"
    stable = json.loads(
        (artifact.payload_root / "dist/configs/stable.json").read_text(encoding="utf-8")
    )
    playlist = (artifact.payload_root / "dist/live/stable.m3u").read_text(
        encoding="utf-8"
    )
    health = json.loads(
        (artifact.payload_root / "dist/health.json").read_text(encoding="utf-8")
    )
    assert stable["sites"] == []
    assert "https://media.example.test/cctv1.m3u8" in playlist
    assert "new-current-only" not in playlist
    assert {source["source_id"] for source in health["sources"]} == {
        "iptv-org-cn-cctv"
    }
    assert artifact.mandatory_removal_ids
    report = json.loads(
        (artifact.payload_root / "dist/reports/latest.json").read_text(encoding="utf-8")
    )
    denied = next(
        source for source in report["sources"] if source["source_id"] == "ikun-vod"
    )
    assert report["gate"]["inputs"]["current_healthy_live_urls"] == 1
    assert denied["publication_status"] == "rejected"
    assert denied["change_summary"]["category"] == "removed"
    assert denied["change_summary"]["current"] is None
    Publisher(
        repository=repository,
        schemas_dir=repository / "schemas",
        raw_verifier=_LocalRaw(remote),  # type: ignore[arg-type]
        now=lambda: datetime(2026, 7, 22, 13, 5, tzinfo=UTC),
        environment={"GITHUB_RUN_ID": "101", "GITHUB_RUN_ATTEMPT": "1"},
    ).publish(second_output / "publish")


def test_safety_reconstructs_redirect_identity_and_reselects_previous_backup(
    tmp_path: Path, monkeypatch: Any
) -> None:
    repository, remote = _repository(tmp_path)
    monkeypatch.setattr(
        "ds_tvbox.pipeline.collect_sources", _healthy_collection_with_live_backup
    )
    first_output = tmp_path / "backup-first"
    collect_publish_artifact(
        repository=repository,
        output=first_output,
        force=False,
        bootstrap=True,
        http_client=_UnusedFetcher(),
        clock=lambda: datetime(2026, 7, 22, 12, 0, tzinfo=UTC),
        environment={"GITHUB_RUN_ID": "200", "GITHUB_RUN_ATTEMPT": "1"},
    )
    first_artifact = validate_publish_artifact(
        first_output / "publish", repository / "schemas"
    )
    first_health = json.loads(
        (first_artifact.payload_root / "dist/health.json").read_text(encoding="utf-8")
    )
    first_vod_sites = json.loads(
        (
            first_artifact.payload_root
            / f"dist/releases/{first_artifact.release_id}/configs/ikun-vod.json"
        ).read_text(encoding="utf-8")
    )["sites"]
    removed_id = first_health["channels"][0]["selected_url_id"]
    Publisher(
        repository=repository,
        schemas_dir=repository / "schemas",
        raw_verifier=_LocalRaw(remote),  # type: ignore[arg-type]
        now=lambda: datetime(2026, 7, 22, 12, 5, tzinfo=UTC),
        environment={"GITHUB_RUN_ID": "200", "GITHUB_RUN_ATTEMPT": "1"},
    ).publish(first_output / "publish")

    def security_violation_collection(**kwargs: Any) -> CollectResult:
        current = _healthy_collection_with_live_backup(**kwargs)
        primary = current.live_results[0]
        rejected_primary = replace(
            primary,
            technical_status=TechnicalStatus.PARTIAL,
            publication_status=PublicationStatus.REJECTED,
            media=MediaProbeResult(
                ok=False,
                final_url=None,
                response_ms=10,
                media_path_score=0,
                failure_reason=FailureReason.CLIENT_HTTP_DISALLOWED,
            ),
            failure_reason=FailureReason.CLIENT_HTTP_DISALLOWED,
        )
        return replace(
            current,
            live_results=(rejected_primary,),
            selected_channels=(),
        )

    monkeypatch.setattr(
        "ds_tvbox.pipeline.collect_sources", security_violation_collection
    )
    second_output = tmp_path / "backup-safety"
    collect_publish_artifact(
        repository=repository,
        output=second_output,
        force=True,
        bootstrap=False,
        http_client=_UnusedFetcher(),
        clock=lambda: datetime(2026, 7, 22, 13, 0, tzinfo=UTC),
        environment={"GITHUB_RUN_ID": "201", "GITHUB_RUN_ATTEMPT": "1"},
    )

    artifact = validate_publish_artifact(second_output / "publish", repository / "schemas")
    playlist = (artifact.payload_root / "dist/live/stable.m3u").read_text(
        encoding="utf-8"
    )
    health = json.loads(
        (artifact.payload_root / "dist/health.json").read_text(encoding="utf-8")
    )
    safety_vod_sites = json.loads(
        (
            artifact.payload_root
            / f"dist/releases/{artifact.release_id}/configs/ikun-vod.json"
        ).read_text(encoding="utf-8")
    )["sites"]
    live_source = next(
        source for source in health["sources"] if source["source_id"] == "iptv-org-cn-cctv"
    )
    channel = health["channels"][0]

    assert "https://cdn.example.test/backup.m3u8" in playlist
    assert "primary.m3u8" not in playlist
    assert safety_vod_sites == first_vod_sites
    assert safety_vod_sites[0]["key"] == "ikun_vod"
    assert safety_vod_sites[0]["name"] == "⚠️ iKun 资源"
    assert len(live_source["items"]) == 1
    assert channel["selected_url_id"] == live_source["items"][0]["entity_id"]
    assert artifact.mandatory_removal_ids == (removed_id,)
    report = json.loads(
        (artifact.payload_root / "dist/reports/latest.json").read_text(encoding="utf-8")
    )
    assert "safety_degraded" not in report["gate"]["reasons"]


def test_denylist_url_matches_removed_registry_vod_and_deletes_history(
    tmp_path: Path, monkeypatch: Any
) -> None:
    repository, remote = _repository(tmp_path)
    monkeypatch.setattr("ds_tvbox.pipeline.collect_sources", _healthy_collection)
    first_output = tmp_path / "removed-vod-first"
    collect_publish_artifact(
        repository=repository,
        output=first_output,
        force=False,
        bootstrap=True,
        http_client=_UnusedFetcher(),
        clock=lambda: datetime(2026, 7, 22, 12, 0, tzinfo=UTC),
        environment={"GITHUB_RUN_ID": "300", "GITHUB_RUN_ATTEMPT": "1"},
    )
    Publisher(
        repository=repository,
        schemas_dir=repository / "schemas",
        raw_verifier=_LocalRaw(remote),  # type: ignore[arg-type]
        now=lambda: datetime(2026, 7, 22, 12, 5, tzinfo=UTC),
        environment={"GITHUB_RUN_ID": "300", "GITHUB_RUN_ATTEMPT": "1"},
    ).publish(first_output / "publish")

    registry_path = repository / "sources/registry.yaml"
    registry_text = registry_path.read_text(encoding="utf-8")
    first_source = registry_text.index("  - id: ikun-vod")
    second_source = registry_text.index("  - id: iptv-org-cn-cctv")
    registry_path.write_text(
        registry_text[:first_source] + registry_text[second_source:], encoding="utf-8"
    )
    (repository / "sources/denylist.yaml").write_text(
        """version: 1
entries:
  - id: remove-old-vod-url
    source_ids: []
    hosts: []
    urls: [https://ikunzyapi.com/api.php/provide/vod]
    reason: takedown
    requested_at: 2026-07-22
    evidence_urls: []
""",
        encoding="utf-8",
    )
    monkeypatch.setattr("ds_tvbox.pipeline.collect_sources", _live_only_collection)
    second_output = tmp_path / "removed-vod-safety"

    collect_publish_artifact(
        repository=repository,
        output=second_output,
        force=True,
        bootstrap=False,
        http_client=_UnusedFetcher(),
        clock=lambda: datetime(2026, 7, 22, 13, 0, tzinfo=UTC),
        environment={"GITHUB_RUN_ID": "301", "GITHUB_RUN_ATTEMPT": "1"},
    )

    artifact = validate_publish_artifact(second_output / "publish", repository / "schemas")
    stable = json.loads(
        (artifact.payload_root / "dist/configs/stable.json").read_text(encoding="utf-8")
    )
    playlist = (artifact.payload_root / "dist/live/stable.m3u").read_text(
        encoding="utf-8"
    )
    report = json.loads(
        (artifact.payload_root / "dist/reports/latest.json").read_text(encoding="utf-8")
    )
    removed = next(source for source in report["sources"] if source["source_id"] == "ikun-vod")

    assert stable["sites"] == []
    assert "https://media.example.test/cctv1.m3u8" in playlist
    assert "current-only.m3u8" not in playlist
    assert "source:ikun-vod" in artifact.mandatory_removal_ids
    assert artifact.deletions == ("dist/releases/g00000001",)
    assert removed["change_summary"]["category"] == "removed"
    assert removed["change_summary"]["current"] is None


def test_history_only_denylist_match_is_regular_then_disappears_after_deletion(
    tmp_path: Path, monkeypatch: Any
) -> None:
    repository, remote = _repository(tmp_path)
    monkeypatch.setattr("ds_tvbox.pipeline.collect_sources", _healthy_collection)
    first_output = tmp_path / "history-first"
    collect_publish_artifact(
        repository=repository,
        output=first_output,
        force=False,
        bootstrap=True,
        http_client=_UnusedFetcher(),
        clock=lambda: datetime(2026, 7, 22, 12, 0, tzinfo=UTC),
        environment={"GITHUB_RUN_ID": "400", "GITHUB_RUN_ATTEMPT": "1"},
    )
    Publisher(
        repository=repository,
        schemas_dir=repository / "schemas",
        raw_verifier=_LocalRaw(remote),  # type: ignore[arg-type]
        now=lambda: datetime(2026, 7, 22, 12, 5, tzinfo=UTC),
        environment={"GITHUB_RUN_ID": "400", "GITHUB_RUN_ATTEMPT": "1"},
    ).publish(first_output / "publish")

    old_url = "https://media.example.test/cctv1.m3u8"
    clean_url = "https://media.example.test/clean.m3u8"
    registry_path = repository / "sources/registry.yaml"
    registry_path.write_text(
        registry_path.read_text(encoding="utf-8").replace(
            "iptv-org-cn-cctv", "clean-live"
        ),
        encoding="utf-8",
    )

    def clean_collection(**kwargs: Any) -> CollectResult:
        current = _healthy_collection(**kwargs)
        old_live = current.live_results[0]
        clean_candidate = replace(
            old_live.candidate,
            original_url=clean_url,
            normalized_url=clean_url,
        )
        clean_live = replace(
            old_live,
            candidate=clean_candidate,
            media=replace(old_live.media, final_url=clean_url),
        )
        return replace(
            current,
            live_results=(clean_live,),
            selected_channels=select_channels((clean_live,)),
        )

    monkeypatch.setattr("ds_tvbox.pipeline.collect_sources", clean_collection)
    second_output = tmp_path / "history-clean"
    collect_publish_artifact(
        repository=repository,
        output=second_output,
        force=True,
        bootstrap=False,
        http_client=_UnusedFetcher(),
        clock=lambda: datetime(2026, 7, 22, 13, 0, tzinfo=UTC),
        environment={"GITHUB_RUN_ID": "401", "GITHUB_RUN_ATTEMPT": "1"},
    )
    Publisher(
        repository=repository,
        schemas_dir=repository / "schemas",
        raw_verifier=_LocalRaw(remote),  # type: ignore[arg-type]
        now=lambda: datetime(2026, 7, 22, 13, 5, tzinfo=UTC),
        environment={"GITHUB_RUN_ID": "401", "GITHUB_RUN_ATTEMPT": "1"},
    ).publish(second_output / "publish")

    (repository / "sources/denylist.yaml").write_text(
        f"""version: 1
entries:
  - id: purge-old-live-url
    source_ids: []
    hosts: []
    urls: [{old_url}]
    reason: takedown
    requested_at: 2026-07-22
    evidence_urls: []
""",
        encoding="utf-8",
    )
    third_output = tmp_path / "history-purge"
    collect_publish_artifact(
        repository=repository,
        output=third_output,
        force=True,
        bootstrap=False,
        http_client=_UnusedFetcher(),
        clock=lambda: datetime(2026, 7, 22, 14, 0, tzinfo=UTC),
        environment={"GITHUB_RUN_ID": "402", "GITHUB_RUN_ATTEMPT": "1"},
    )
    third = validate_publish_artifact(third_output / "publish", repository / "schemas")

    assert third.release_kind.value == "regular"
    assert third.mandatory_removal_ids == ()
    assert third.deletions == ("dist/releases/g00000001",)
    playlist = (third.payload_root / "dist/live/stable.m3u").read_text(encoding="utf-8")
    assert clean_url in playlist
    assert old_url not in playlist

    Publisher(
        repository=repository,
        schemas_dir=repository / "schemas",
        raw_verifier=_LocalRaw(remote),  # type: ignore[arg-type]
        now=lambda: datetime(2026, 7, 22, 14, 5, tzinfo=UTC),
        environment={"GITHUB_RUN_ID": "402", "GITHUB_RUN_ATTEMPT": "1"},
    ).publish(third_output / "publish")

    fourth_output = tmp_path / "history-after-purge"
    collect_publish_artifact(
        repository=repository,
        output=fourth_output,
        force=True,
        bootstrap=False,
        http_client=_UnusedFetcher(),
        clock=lambda: datetime(2026, 7, 22, 15, 0, tzinfo=UTC),
        environment={"GITHUB_RUN_ID": "403", "GITHUB_RUN_ATTEMPT": "1"},
    )
    fourth = validate_publish_artifact(
        fourth_output / "publish", repository / "schemas"
    )
    assert fourth.release_kind.value == "regular"
    assert fourth.deletions == ()
