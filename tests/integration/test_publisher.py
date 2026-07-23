from __future__ import annotations

import copy
import json
import shutil
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ds_tvbox.artifact import build_publish_artifact, validate_publish_artifact
from ds_tvbox.bundle import build_bundle_files
from ds_tvbox.errors import ContractError, PublishError
from ds_tvbox.generator import build_client_artifacts
from ds_tvbox.health import build_health_document, vod_entity_id
from ds_tvbox.live import live_url_id, select_channels
from ds_tvbox.manifests import prefixed_sha256
from ds_tvbox.models import (
    LiveCandidate,
    LiveProbeResult,
    MediaProbeResult,
    PublicationStatus,
    ReleaseKind,
    RunContext,
    SourceSpec,
    TechnicalStatus,
    VodCapabilities,
    VodProbeResult,
    VodSiteCandidate,
)
from ds_tvbox.publisher import Publisher
from ds_tvbox.raw import RawExpectedRelease
from ds_tvbox.registry import load_registry
from ds_tvbox.reports import build_change_summary, build_latest_report, render_latest_markdown
from ds_tvbox.serialization import canonical_json_bytes

SCHEMAS = Path(__file__).resolve().parents[2] / "schemas"
PROJECT = SCHEMAS.parent
FIXED_NOW = datetime(2026, 7, 22, 13, 0, tzinfo=UTC)


def _git(path: Path, *arguments: str, check: bool = True) -> str:
    result = subprocess.run(  # noqa: S603 - fixed argv, test-owned repository
        ["git", *arguments],  # noqa: S607 - test uses the Git executable from PATH
        cwd=path,
        check=False,
        capture_output=True,
        text=True,
    )
    if check and result.returncode != 0:
        raise AssertionError(result.stderr or result.stdout)
    return result.stdout.strip()


def _bare_git(remote: Path, *arguments: str, check: bool = True) -> str:
    result = subprocess.run(  # noqa: S603 - fixed argv, test-owned repository
        ["git", "--git-dir", str(remote), *arguments],  # noqa: S607
        check=False,
        capture_output=True,
        text=True,
    )
    if check and result.returncode != 0:
        raise AssertionError(result.stderr or result.stdout)
    return result.stdout.strip()


def _bare_bytes(remote: Path, *arguments: str) -> bytes:
    result = subprocess.run(  # noqa: S603 - fixed argv, test-owned repository
        ["git", "--git-dir", str(remote), *arguments],  # noqa: S607
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr.decode() or result.stdout.decode())
    return result.stdout


def _bare_object_exists(remote: Path, spec: str) -> bool:
    result = subprocess.run(  # noqa: S603 - fixed argv, test-owned repository
        ["git", "--git-dir", str(remote), "cat-file", "-e", spec],  # noqa: S607
        check=False,
        capture_output=True,
    )
    return result.returncode == 0


def _init_repository(tmp_path: Path) -> tuple[Path, Path]:
    remote = tmp_path / "origin.git"
    repository = tmp_path / "repository"
    remote.mkdir()
    repository.mkdir()
    _git(remote, "init", "--bare")
    _git(repository, "init", "-b", "main")
    _git(repository, "config", "user.name", "test")
    _git(repository, "config", "user.email", "test@example.invalid")
    _git(repository, "remote", "add", "origin", str(remote))
    (repository / "README.md").write_text("publisher fixture\n", encoding="utf-8")
    _git(repository, "add", "README.md")
    _git(repository, "commit", "-m", "fixture")
    _git(repository, "push", "-u", "origin", "main")
    return repository, remote


class FakeRaw:
    owner = "azhansy"
    repository = "ds-tvbox"

    def __init__(
        self,
        remote: Path,
        *,
        bare_failures: int = 0,
        absent_failures: int = 0,
        revision_hook: Callable[[str, str, int], None] | None = None,
        bare_hook: Callable[[str], None] | None = None,
    ) -> None:
        self.remote = remote
        self.bare_failures = bare_failures
        self.absent_failures = absent_failures
        self.revision_hook = revision_hook
        self.bare_hook = bare_hook
        self.calls: list[tuple[str, str, str]] = []
        self.expectations: list[RawExpectedRelease] = []

    def _assert_identity(
        self,
        revision: str,
        expected_status: str,
        expected: RawExpectedRelease,
    ) -> str:
        commit = (
            _bare_git(self.remote, "rev-parse", "refs/heads/generated")
            if revision == "generated"
            else revision
        )
        root_bytes = _bare_bytes(self.remote, "show", f"{commit}:dist/manifest.json")
        assert prefixed_sha256(root_bytes) == expected.root_manifest_sha256
        root = json.loads(root_bytes)
        assert root["active_release_id"] == expected.release_id
        assert root["aliases"] == dict(expected.aliases)
        assert (
            root["content_workflow_run_id"],
            root["content_workflow_run_attempt"],
        ) == (
            expected.content_workflow_run_id,
            expected.content_workflow_run_attempt,
        )
        pointer = root["release_manifest"]
        assert pointer["sha256"] == expected.release_manifest_sha256
        release_bytes = _bare_bytes(self.remote, "show", f"{commit}:{pointer['path']}")
        assert prefixed_sha256(release_bytes) == expected.release_manifest_sha256
        release = json.loads(release_bytes)
        assert release["generation"] == expected.release_generation
        state = _json_at(self.remote, commit, "state/release.json")
        report = _json_at(self.remote, commit, "dist/reports/latest.json")
        assert state["status"] == expected_status
        assert state["generation"] == report["generation"] == expected.event_generation
        assert (
            state["workflow_run_id"],
            state["workflow_run_attempt"],
        ) == (
            expected.workflow_run_id,
            expected.workflow_run_attempt,
        )
        assert state.get("required_absent_paths", []) == list(
            expected.required_absent_paths
        )
        self.expectations.append(expected)
        return commit

    def poll_revision(
        self,
        revision: str,
        *,
        expected_status: str = "success",
        expected: RawExpectedRelease | None = None,
    ) -> None:
        assert _bare_git(self.remote, "cat-file", "-t", revision) == "commit"
        assert expected is not None
        self._assert_identity(revision, expected_status, expected)
        self.calls.append(("revision", revision, expected_status))
        if self.revision_hook is not None:
            self.revision_hook(revision, expected_status, len(self.calls))

    def poll_bare(
        self,
        *,
        ref: str = "generated",
        expected: RawExpectedRelease | None = None,
    ) -> None:
        head = _bare_git(self.remote, "rev-parse", f"refs/heads/{ref}")
        assert expected is not None
        self._assert_identity(ref, "success", expected)
        self.calls.append(("bare", head, "success"))
        if self.bare_hook is not None:
            self.bare_hook(head)
        if self.bare_failures:
            self.bare_failures -= 1
            raise PublishError("simulated bare Raw timeout")

    def poll_absent(self, revision: str, relatives: tuple[str, ...]) -> None:
        assert revision == "generated"
        head = _bare_git(self.remote, "rev-parse", "refs/heads/generated")
        self.calls.append(("absent", head, str(len(relatives))))
        if self.absent_failures:
            self.absent_failures -= 1
            raise PublishError("simulated deleted Raw path is still visible")
        for relative in relatives:
            assert not _bare_object_exists(self.remote, f"{head}:{relative}")


def _fixture_results(
    generated_at: str,
    *,
    live_url: str = "https://media.example.test/cctv1.m3u8",
) -> tuple[
    tuple[SourceSpec, ...],
    VodProbeResult,
    LiveProbeResult,
]:
    sources = load_registry(PROJECT / "sources/registry.yaml")
    vod_source = next(source for source in sources if source.id == "ikun-vod")
    live_source = next(source for source in sources if source.id == "iptv-org-cn-cctv")
    assert vod_source.client_site is not None
    assert vod_source.fetch.reviewed_url is not None
    site = vod_source.client_site
    vod = VodProbeResult(
        candidate=VodSiteCandidate(
            source_id=vod_source.id,
            key=site.key,
            name=site.name,
            type=1,
            api=vod_source.fetch.reviewed_url,
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
        original_url=live_url,
        normalized_url=live_url,
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
        last_success_at=generated_at,
        failure_reason=None,
    )
    return sources, vod, live


def _fixture_upstreams(sources: tuple[SourceSpec, ...]) -> tuple[dict[str, object], ...]:
    records: list[dict[str, object]] = []
    for source in sources:
        resolved_url = source.fetch.reviewed_url
        assert isinstance(resolved_url, str)
        records.append(
            {
                "source_id": source.id,
                "fetch_mode": source.fetch.mode.value,
                "reviewed_revision": source.fetch.reviewed_revision,
                "resolved_revision": source.fetch.reviewed_revision,
                "resolved_fetch_url": resolved_url,
                "terms_sha256": {
                    str(term.url or term.path): term.reviewed_sha256 for term in source.terms_watch
                },
            }
        )
    return tuple(records)


def _artifact(
    tmp_path: Path,
    *,
    kind: ReleaseKind,
    generation: int,
    previous_head: str | None,
    run_id: str,
    attempt: int,
    previous_last_success_at: str | None,
    mandatory_removal_ids: tuple[str, ...] = (),
    deletions: tuple[str, ...] = (),
    live_url: str = "https://media.example.test/cctv1.m3u8",
    excluded_source_ids: tuple[str, ...] = (),
    previous_source_ids: frozenset[str] | None = None,
    previous_vod_sites: int | None = None,
    previous_live_channels: int | None = None,
    previous_public_unverified: int | None = None,
) -> Path:
    if kind is ReleaseKind.SAFETY and not mandatory_removal_ids:
        mandatory_removal_ids = ("source:ikun-vod",)
    generated_at = f"2026-07-{20 + generation:02d}T12:00:00Z"
    context = RunContext(
        owner="azhansy",
        repository="ds-tvbox",
        generated_ref="generated",
        workflow_run_id=run_id,
        workflow_run_attempt=attempt,
        generated_at=generated_at,
        generation=generation,
        release_kind=kind,
        previous_head=previous_head,
        previous_last_success_at=previous_last_success_at,
    )
    all_sources_raw, vod, live = _fixture_results(generated_at, live_url=live_url)
    expanded_mandatory = set(mandatory_removal_ids)
    if "source:ikun-vod" in expanded_mandatory:
        expanded_mandatory.add(vod_entity_id(vod.candidate))
    if "source:iptv-org-cn-cctv" in expanded_mandatory:
        expanded_mandatory.add(live_url_id(live.candidate))
    mandatory_removal_ids = tuple(sorted(expanded_mandatory))
    blocked_sources = {
        identifier.removeprefix("source:")
        for identifier in mandatory_removal_ids
        if identifier.startswith("source:")
    }
    payload_blocked_sources = blocked_sources | set(excluded_source_ids)
    removed_vod = vod_entity_id(vod.candidate) in mandatory_removal_ids
    sources = tuple(
        source for source in all_sources_raw if source.id not in payload_blocked_sources
    )
    vod_results = (
        () if vod.candidate.source_id in payload_blocked_sources or removed_vod else (vod,)
    )
    live_results = () if live.candidate.source_id in payload_blocked_sources else (live,)
    channels = select_channels(live_results)
    client = build_client_artifacts(
        context=context,
        sources=sources,
        vod_results=vod_results,
        channels=channels,
    )
    upstream_revisions = {source.id: source.fetch.reviewed_revision for source in sources}
    health = build_health_document(
        generated_at=generated_at,
        generation=generation,
        release_id=context.release_id,
        sources=sources,
        vod_results=vod_results,
        live_results=live_results,
        selected_channels=channels,
        upstream_revisions=upstream_revisions,
        enumerated_source_ids=frozenset(upstream_revisions),
    )
    if kind is ReleaseKind.SAFETY:
        previous_generated_at = f"2026-07-{19 + generation:02d}T12:00:00Z"
        previous_sources_raw, previous_vod, previous_live = _fixture_results(
            previous_generated_at,
            live_url=live_url,
        )
        previous_channels = select_channels((previous_live,))
        previous_revisions = {
            source.id: source.fetch.reviewed_revision for source in previous_sources_raw
        }
        previous_health = build_health_document(
            generated_at=previous_generated_at,
            generation=generation - 1,
            release_id=f"g{generation - 1:08d}",
            sources=previous_sources_raw,
            vod_results=(previous_vod,),
            live_results=(previous_live,),
            selected_channels=previous_channels,
            upstream_revisions=previous_revisions,
            enumerated_source_ids=frozenset(previous_revisions),
        )
        retained_live_ids: set[str] = set()
        retained_sources: list[dict[str, object]] = []
        for previous_source in previous_health["sources"]:
            source_id = str(previous_source["source_id"])
            if source_id in payload_blocked_sources:
                continue
            retained = copy.deepcopy(previous_source)
            retained["last_checked_at"] = generated_at
            retained["items"] = [
                item
                for item in retained["items"]
                if item["entity_id"] not in mandatory_removal_ids
            ]
            if len(retained["items"]) != len(previous_source["items"]):
                retained["technical_status"] = next(
                    (
                        status
                        for status in (
                            "healthy",
                            "partial",
                            "suspect",
                            "unknown",
                            "unsupported_environment",
                            "dead",
                        )
                        if any(
                            item["technical_status"] == status
                            for item in retained["items"]
                        )
                    ),
                    "unknown",
                )
                retained["publication_status"] = next(
                    (
                        status
                        for status in ("stable", "experimental", "withheld", "rejected")
                        if any(
                            item["publication_status"] == status
                            for item in retained["items"]
                        )
                    ),
                    "withheld",
                )
            retained_live_ids.update(
                str(item["entity_id"])
                for item in retained["items"]
                if item["entity_type"] == "live_url"
            )
            retained_sources.append(retained)
        retained_channels: list[dict[str, object]] = []
        for previous_channel in previous_health["channels"]:
            retained_candidates = sorted(
                item
                for item in previous_channel["candidate_url_ids"]
                if item in retained_live_ids
            )
            if not retained_candidates:
                continue
            retained = copy.deepcopy(previous_channel)
            retained["candidate_url_ids"] = retained_candidates
            if retained.get("selected_url_id") not in retained_candidates:
                retained["selected_url_id"] = None
                retained["publication_status"] = "withheld"
            retained_channels.append(retained)
        health = {
            "schema_version": previous_health["schema_version"],
            "generated_at": generated_at,
            "generation": generation,
            "release_id": context.release_id,
            "sources": retained_sources,
            "channels": retained_channels,
        }
    content_identity = {
        "workflow_run_id": run_id,
        "workflow_run_attempt": attempt,
    }
    health_sources = {str(source["source_id"]): source for source in health["sources"]}
    source_rows = [
        {
            "source_id": source.id,
            "technical_status": health_sources[source.id]["technical_status"],
            "publication_status": health_sources[source.id]["publication_status"],
            "rights_status": health_sources[source.id]["rights_status"],
            "failure_reason": health_sources[source.id]["failure_reason"],
            "secondary_reasons": [],
            "upstream_revision": source.fetch.reviewed_revision,
        }
        for source in sources
    ]
    source_specs = {source.id: source for source in all_sources_raw}
    source_rows.extend(
        {
            "source_id": source_id,
            "technical_status": "unknown",
            "publication_status": "withheld",
            "rights_status": (
                source_specs[source_id].rights_status.value
                if source_id in source_specs
                else "unknown"
            ),
            "failure_reason": "terms_changed",
            "secondary_reasons": [],
            "upstream_revision": (
                source_specs[source_id].fetch.reviewed_revision
                if source_id in source_specs
                else None
            ),
        }
        for source_id in sorted(blocked_sources)
    )
    audited_previous_source_ids = (
        frozenset(source_specs)
        if generation > 1 and previous_source_ids is None
        else previous_source_ids or frozenset()
    )
    if generation > 1:
        for row in source_rows:
            source_id = str(row["source_id"])
            previous_snapshot = (
                {
                    "technical_status": "healthy",
                    "publication_status": "stable",
                    "rights_status": source_specs[source_id].rights_status.value,
                }
                if source_id in audited_previous_source_ids
                else None
            )
            current_snapshot = health_sources.get(source_id)
            row["change_summary"] = build_change_summary(
                previous_snapshot, current_snapshot
            )
    previous_vod_count = (
        (0 if generation == 1 else 1)
        if previous_vod_sites is None
        else previous_vod_sites
    )
    previous_live_count = (
        (0 if generation == 1 else 1)
        if previous_live_channels is None
        else previous_live_channels
    )
    report = build_latest_report(
        context,
        status="pending",
        started_at=generated_at,
        finished_at=generated_at,
        due=True,
        forced=True,
        recovery_due=kind is ReleaseKind.SAFETY,
        sources=source_rows,
        counts={
            "previous_vod_sites": previous_vod_count,
            "current_vod_sites": client.vod_site_count,
            "previous_live_channels": previous_live_count,
            "current_live_channels": client.live_channel_count,
            "previous_public_unverified": (
                len(audited_previous_source_ids)
                if previous_public_unverified is None
                else previous_public_unverified
            ),
            "current_public_unverified": len(sources),
        },
        gate={
            "publish": True,
            "inconclusive": False,
            "release_kind": kind.value,
            "reasons": (
                ["mandatory_removal", "safety_degraded"] if kind is ReleaseKind.SAFETY else []
            ),
            "mandatory_removal_ids": list(mandatory_removal_ids),
            "historical_deletions": list(deletions),
            "inputs": {
                "previous_vod_items": previous_vod_count,
                "current_publishable_vod_items": len(vod_results),
                "previous_live_urls": previous_live_count,
                "current_healthy_live_urls": len(live_results),
                "current_vod_sites": client.vod_site_count,
                "current_live_channels": client.live_channel_count,
                "failed_network_groups": 0,
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
        previous_release_head_sha=previous_head,
        candidate_ref=context.candidate_ref,
        content_identity=content_identity,
        entity_failure_reasons=(("credential_required",) if removed_vod else ()),
    )
    state = {
        "schema_version": "1.0.0",
        "status": "pending",
        "release_kind": kind.value,
        "generation": generation,
        "active_release_id": context.release_id,
        "last_publish_at": None,
        "last_success_at": previous_last_success_at,
        "content_commit_sha": None,
        "previous_release_head_sha": previous_head,
        "workflow_run_id": run_id,
        "workflow_run_attempt": attempt,
    }
    bundle = build_bundle_files(
        context=context,
        client_artifacts=client,
        health=health,
        upstreams=_fixture_upstreams(sources),
        source_count=len(sources),
        supplemental_files={
            "state/release.json": canonical_json_bytes(state),
            "dist/reports/latest.json": canonical_json_bytes(report),
            "dist/reports/latest.md": render_latest_markdown(report),
        },
    )
    return build_publish_artifact(
        tmp_path / f"artifact-{run_id}-{attempt}",
        context=context,
        bundle_files=bundle,
        deletions=deletions,
        mandatory_removal_ids=mandatory_removal_ids,
    )


def _publisher(
    repository: Path,
    raw: FakeRaw,
    *,
    run_id: str = "unused",
    attempt: int = 1,
    schemas_dir: Path = SCHEMAS,
    safety_fact_verifier: Callable[[SourceSpec, str], bool] | None = None,
) -> Publisher:
    verifier = safety_fact_verifier or (lambda _source, reason: reason == "terms_changed")
    return Publisher(
        repository=repository,
        schemas_dir=schemas_dir,
        raw_verifier=raw,  # type: ignore[arg-type]
        now=lambda: FIXED_NOW,
        environment={
            "GITHUB_RUN_ID": run_id,
            "GITHUB_RUN_ATTEMPT": str(attempt),
        },
        safety_fact_verifier=verifier,
    )


def _trusted_schemas(
    tmp_path: Path,
    name: str,
    *,
    source_ids: tuple[str, ...] = (),
    hosts: tuple[str, ...] = (),
    urls: tuple[str, ...] = (),
) -> Path:
    trusted = tmp_path / name
    shutil.copytree(SCHEMAS, trusted / "schemas")
    shutil.copytree(PROJECT / "config", trusted / "config")
    (trusted / "sources").mkdir()
    shutil.copy2(PROJECT / "sources/registry.yaml", trusted / "sources/registry.yaml")
    if source_ids or hosts or urls:
        denylist = (
            "version: 1\n"
            "entries:\n"
            "  - id: publisher-test-removal\n"
            f"    source_ids: {json.dumps(list(source_ids))}\n"
            f"    hosts: {json.dumps(list(hosts))}\n"
            f"    urls: {json.dumps(list(urls))}\n"
            "    reason: security\n"
            "    requested_at: 2026-07-22\n"
            "    evidence_urls: []\n"
        )
    else:
        denylist = "version: 1\nentries: []\n"
    (trusted / "sources/denylist.yaml").write_text(denylist, encoding="utf-8")
    return trusted / "schemas"


def _bootstrap(tmp_path: Path, repository: Path, remote: Path, run_id: str = "100") -> str:
    raw = FakeRaw(remote)
    artifact = _artifact(
        tmp_path,
        kind=ReleaseKind.BOOTSTRAP,
        generation=1,
        previous_head=None,
        run_id=run_id,
        attempt=1,
        previous_last_success_at=None,
    )
    return _publisher(repository, raw, run_id=run_id).publish(artifact)


def _json_at(remote: Path, revision: str, relative: str) -> dict[str, object]:
    value = json.loads(_bare_git(remote, "show", f"{revision}:{relative}"))
    assert isinstance(value, dict)
    return value


def test_bootstrap_is_orphan_two_commits_and_attempt_refs_are_isolated(tmp_path: Path) -> None:
    repository, remote = _init_repository(tmp_path)
    main_sha = _git(repository, "rev-parse", "HEAD")
    _git(
        repository,
        "push",
        "origin",
        f"{main_sha}:refs/heads/candidate/run-200-attempt-1",
    )
    raw = FakeRaw(remote)
    artifact = _artifact(
        tmp_path,
        kind=ReleaseKind.BOOTSTRAP,
        generation=1,
        previous_head=None,
        run_id="200",
        attempt=2,
        previous_last_success_at=None,
    )

    final_sha = _publisher(repository, raw, run_id="200", attempt=2).publish(artifact)

    assert _bare_git(remote, "rev-parse", "refs/heads/generated") == final_sha
    assert _bare_git(remote, "show-ref", "--verify", "refs/heads/candidate/run-200-attempt-1")
    assert not _bare_git(
        remote,
        "show-ref",
        "--verify",
        "refs/heads/candidate/run-200-attempt-2",
        check=False,
    )
    final_parents = _bare_git(remote, "rev-list", "--parents", "-n", "1", final_sha).split()
    assert len(final_parents) == 2
    content_sha = final_parents[1]
    assert _bare_git(remote, "rev-list", "--parents", "-n", "1", content_sha).split() == [
        content_sha
    ]
    assert set(_bare_git(remote, "diff", "--name-only", content_sha, final_sha).splitlines()) == {
        "state/release.json",
        "dist/reports/latest.json",
        "dist/reports/latest.md",
    }
    assert raw.calls[0] == ("revision", content_sha, "pending")
    assert raw.calls[-1][0] == "bare"
    state = _json_at(remote, final_sha, "state/release.json")
    assert state["status"] == "success"
    assert state["content_commit_sha"] == content_sha
    assert state["workflow_run_attempt"] == 2
    sealed = validate_publish_artifact(artifact, SCHEMAS)
    assert raw.expectations
    assert all(
        expectation.root_manifest_sha256 == sealed.root_manifest_sha256
        and expectation.release_manifest_sha256 == sealed.release_manifest_sha256
        and expectation.release_id == sealed.release_id
        for expectation in raw.expectations
    )


def test_regular_release_is_fast_forward_and_success_cleans_candidate(tmp_path: Path) -> None:
    repository, remote = _init_repository(tmp_path)
    previous = _bootstrap(tmp_path, repository, remote)
    raw = FakeRaw(remote)
    artifact = _artifact(
        tmp_path,
        kind=ReleaseKind.REGULAR,
        generation=2,
        previous_head=previous,
        run_id="300",
        attempt=1,
        previous_last_success_at="2026-07-22T13:00:00Z",
    )

    final_sha = _publisher(repository, raw, run_id="300").publish(artifact)

    assert _bare_git(remote, "merge-base", "--is-ancestor", previous, final_sha) == ""
    assert _bare_git(remote, "rev-parse", "refs/heads/generated") == final_sha
    assert not _bare_git(
        remote,
        "show-ref",
        "--verify",
        "refs/heads/candidate/run-300-attempt-1",
        check=False,
    )
    assert _bare_git(remote, "cat-file", "-t", f"{final_sha}:dist/releases/g00000001") == "tree"
    assert _bare_git(remote, "cat-file", "-t", f"{final_sha}:dist/releases/g00000002") == "tree"


def test_expected_head_cas_rejects_race_without_overwriting_generated(tmp_path: Path) -> None:
    repository, remote = _init_repository(tmp_path)
    previous = _bootstrap(tmp_path, repository, remote)
    _git(repository, "checkout", "-B", "race", previous)
    (repository / "RACE.txt").write_text("concurrent update\n", encoding="utf-8")
    _git(repository, "add", "RACE.txt")
    _git(repository, "commit", "-m", "race")
    race_sha = _git(repository, "rev-parse", "HEAD")
    _git(repository, "checkout", "main")

    def race_after_confirmation(_revision: str, status: str, call_count: int) -> None:
        if status == "success" and call_count == 2:
            _git(repository, "push", "origin", f"{race_sha}:refs/heads/generated")

    raw = FakeRaw(remote, revision_hook=race_after_confirmation)
    artifact = _artifact(
        tmp_path,
        kind=ReleaseKind.REGULAR,
        generation=2,
        previous_head=previous,
        run_id="400",
        attempt=1,
        previous_last_success_at="2026-07-22T13:00:00Z",
    )

    with pytest.raises(PublishError, match="remote generated changed"):
        _publisher(repository, raw, run_id="400").publish(artifact)

    assert _bare_git(remote, "rev-parse", "refs/heads/generated") == race_sha
    assert _bare_git(remote, "show-ref", "--verify", "refs/heads/candidate/run-400-attempt-1")


def test_bootstrap_bare_timeout_uses_precise_lease_and_removes_new_ref(tmp_path: Path) -> None:
    repository, remote = _init_repository(tmp_path)
    raw = FakeRaw(remote, bare_failures=1)
    artifact = _artifact(
        tmp_path,
        kind=ReleaseKind.BOOTSTRAP,
        generation=1,
        previous_head=None,
        run_id="500",
        attempt=1,
        previous_last_success_at=None,
    )

    with pytest.raises(PublishError, match="simulated bare Raw timeout"):
        _publisher(repository, raw, run_id="500").publish(artifact)

    assert not _bare_git(remote, "show-ref", "--verify", "refs/heads/generated", check=False)
    assert _bare_git(remote, "show-ref", "--verify", "refs/heads/candidate/run-500-attempt-1")


def test_bootstrap_lease_never_deletes_a_concurrently_changed_ref(tmp_path: Path) -> None:
    repository, remote = _init_repository(tmp_path)
    main_sha = _git(repository, "rev-parse", "HEAD")

    def replace_generated(_published: str) -> None:
        _git(repository, "push", "--force", "origin", f"{main_sha}:refs/heads/generated")

    raw = FakeRaw(remote, bare_failures=1, bare_hook=replace_generated)
    artifact = _artifact(
        tmp_path,
        kind=ReleaseKind.BOOTSTRAP,
        generation=1,
        previous_head=None,
        run_id="501",
        attempt=1,
        previous_last_success_at=None,
    )

    with pytest.raises(PublishError, match="could not be precisely removed"):
        _publisher(repository, raw, run_id="501").publish(artifact)

    assert _bare_git(remote, "rev-parse", "refs/heads/generated") == main_sha


def test_regular_bare_timeout_creates_non_force_compensating_rollback(tmp_path: Path) -> None:
    repository, remote = _init_repository(tmp_path)
    target_head = _bootstrap(tmp_path, repository, remote)
    target_state = _json_at(remote, target_head, "state/release.json")
    target_report = _json_at(remote, target_head, "dist/reports/latest.json")
    target_manifest = _bare_git(remote, "show", f"{target_head}:dist/manifest.json")
    target_health = _bare_git(remote, "show", f"{target_head}:dist/health.json")
    raw = FakeRaw(remote, bare_failures=1)
    artifact = _artifact(
        tmp_path,
        kind=ReleaseKind.REGULAR,
        generation=2,
        previous_head=target_head,
        run_id="600",
        attempt=1,
        previous_last_success_at=str(target_state["last_success_at"]),
    )

    with pytest.raises(PublishError, match="simulated bare Raw timeout"):
        _publisher(repository, raw, run_id="600").publish(artifact)

    rollback_head = _bare_git(remote, "rev-parse", "refs/heads/generated")
    rollback_state = _json_at(remote, rollback_head, "state/release.json")
    rollback_report = _json_at(remote, rollback_head, "dist/reports/latest.json")
    bad_head = str(rollback_state["previous_release_head_sha"])
    bad_report = _json_at(remote, bad_head, "dist/reports/latest.json")
    assert _bare_git(remote, "rev-parse", f"{rollback_head}^") == bad_head
    assert rollback_state["release_kind"] == "rollback"
    assert rollback_state["status"] == "success"
    assert rollback_state["generation"] == 3
    assert rollback_state["active_release_id"] == "g00000001"
    assert rollback_state["content_commit_sha"] == target_state["content_commit_sha"]
    assert _bare_git(remote, "show", f"{rollback_head}:dist/manifest.json") == target_manifest
    assert _bare_git(remote, "show", f"{rollback_head}:dist/health.json") == target_health
    rollback_counts = rollback_report["counts"]
    bad_counts = bad_report["counts"]
    target_counts = target_report["counts"]
    assert isinstance(rollback_counts, dict)
    assert isinstance(bad_counts, dict)
    assert isinstance(target_counts, dict)
    assert rollback_counts["previous_vod_sites"] == bad_counts["current_vod_sites"]
    assert rollback_counts["previous_live_channels"] == bad_counts["current_live_channels"]
    assert rollback_counts["previous_public_unverified"] == bad_counts["current_public_unverified"]
    assert rollback_counts["current_vod_sites"] == target_counts["current_vod_sites"]
    assert rollback_counts["current_live_channels"] == target_counts["current_live_channels"]
    assert all(
        source["change_summary"]["category"] == "unchanged"
        and source["change_summary"]["previous"] is not None
        and source["change_summary"]["current"] is not None
        for source in rollback_report["sources"]
    )
    assert any(source["change_summary"]["category"] == "new" for source in target_report["sources"])
    assert rollback_report["failures"] == target_report["failures"]
    assert rollback_report["active_release_id"] == rollback_state["active_release_id"]
    assert rollback_report["generation"] == rollback_state["generation"]
    assert rollback_report["workflow_run_id"] == rollback_state["workflow_run_id"]
    assert rollback_report["workflow_run_attempt"] == rollback_state["workflow_run_attempt"]
    rollback_gate = rollback_report["gate"]
    bad_gate = bad_report["gate"]
    target_gate = target_report["gate"]
    assert isinstance(rollback_gate, dict)
    assert isinstance(bad_gate, dict)
    assert isinstance(target_gate, dict)
    assert rollback_gate["publish"] is True
    assert rollback_gate["inconclusive"] is False
    assert rollback_gate["release_kind"] == "rollback"
    assert rollback_gate["reasons"] == []
    assert rollback_gate["mandatory_removal_ids"] == []
    assert rollback_gate["historical_deletions"] == []
    assert rollback_gate["network_probes"] == bad_gate["network_probes"]
    assert rollback_gate["thresholds"] == target_gate["thresholds"]
    assert rollback_gate["inputs"] == {
        "previous_vod_items": bad_gate["inputs"]["current_publishable_vod_items"],
        "current_publishable_vod_items": target_gate["inputs"]["current_publishable_vod_items"],
        "previous_live_urls": bad_gate["inputs"]["current_healthy_live_urls"],
        "current_healthy_live_urls": target_gate["inputs"]["current_healthy_live_urls"],
        "current_vod_sites": target_gate["inputs"]["current_vod_sites"],
        "current_live_channels": target_gate["inputs"]["current_live_channels"],
        "failed_network_groups": bad_gate["inputs"]["failed_network_groups"],
    }
    assert _bare_git(remote, "cat-file", "-t", f"{rollback_head}:dist/releases/g00000002") == "tree"
    assert ("revision", rollback_head, "success") in raw.calls
    rollback_revision_index = raw.calls.index(("revision", rollback_head, "success"))
    rollback_bare_index = next(
        index
        for index, call in enumerate(raw.calls)
        if call[0] == "bare" and call[1] == rollback_head
    )
    assert rollback_revision_index < rollback_bare_index


def test_safety_bare_timeout_keeps_verified_safe_sha_without_rollback(tmp_path: Path) -> None:
    repository, remote = _init_repository(tmp_path)
    previous = _bootstrap(tmp_path, repository, remote)
    previous_state = _json_at(remote, previous, "state/release.json")
    raw = FakeRaw(remote, bare_failures=1)
    artifact = _artifact(
        tmp_path,
        kind=ReleaseKind.SAFETY,
        generation=2,
        previous_head=previous,
        run_id="700",
        attempt=1,
        previous_last_success_at=str(previous_state["last_success_at"]),
    )

    with pytest.raises(PublishError, match="delivery_unverified"):
        _publisher(repository, raw, run_id="700").publish(artifact)

    safe_head = _bare_git(remote, "rev-parse", "refs/heads/generated")
    state = _json_at(remote, safe_head, "state/release.json")
    report = _json_at(remote, safe_head, "dist/reports/latest.json")
    assert state["release_kind"] == "safety"
    assert state["status"] == "success"
    assert report["status"] == "safety_degraded"
    assert state["active_release_id"] == "g00000002"
    assert state["last_success_at"] == previous_state["last_success_at"]
    assert _bare_git(remote, "rev-parse", f"{safe_head}^^") == previous
    assert _bare_git(remote, "show-ref", "--verify", "refs/heads/candidate/run-700-attempt-1")


def test_release_removal_helper_rejects_broad_paths_and_removes_exact_release(
    tmp_path: Path,
) -> None:
    worktree = tmp_path / "worktree"
    release = worktree / "dist/releases/g00000001"
    release.mkdir(parents=True)
    (release / "index.json").write_text("{}", encoding="utf-8")
    with pytest.raises(PublishError, match="unsafe release deletion target"):
        Publisher._remove_exact_release(worktree, "dist/releases")
    Publisher._remove_exact_release(worktree, "dist/releases/g00000001")
    assert not release.exists()
    Publisher._remove_exact_release(worktree, "dist/releases/g00000002")


def test_confirm_rejects_nonobject_and_nonpending_state_report(tmp_path: Path) -> None:
    repository, remote = _init_repository(tmp_path)
    artifact_path = _artifact(
        tmp_path,
        kind=ReleaseKind.BOOTSTRAP,
        generation=1,
        previous_head=None,
        run_id="800",
        attempt=1,
        previous_last_success_at=None,
    )
    artifact = validate_publish_artifact(artifact_path, SCHEMAS)
    publisher = _publisher(repository, FakeRaw(remote))

    state_path = artifact.payload_root / "state/release.json"
    original_state = state_path.read_bytes()
    state_path.write_text("[]\n", encoding="utf-8")
    with pytest.raises(ContractError, match="must be objects"):
        publisher._confirm(artifact.payload_root, artifact, "a" * 40)

    state_path.write_bytes(original_state)
    report_path = artifact.payload_root / "dist/reports/latest.json"
    report = json.loads(report_path.read_bytes())
    report["status"] = "success"
    report_path.write_bytes(canonical_json_bytes(report))
    with pytest.raises(ContractError, match="expected pending"):
        publisher._confirm(artifact.payload_root, artifact, "a" * 40)


def test_confirmation_diff_allows_only_state_and_latest_reports(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, remote = _init_repository(tmp_path)
    publisher = _publisher(repository, FakeRaw(remote))
    monkeypatch.setattr(
        publisher.git,
        "changed_paths",
        lambda *_args, **_kwargs: ["dist/index.json"],
    )
    with pytest.raises(PublishError, match="changed forbidden paths"):
        publisher._verify_confirmation_diff("a" * 40, "b" * 40, repository)


def test_mandatory_removal_scan_checks_root_and_release_files(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    release = tree / "dist/releases/g00000001"
    release.mkdir(parents=True)
    (tree / "dist/index.json").write_text("safe", encoding="utf-8")
    (release / "config.json").write_text("source:blocked", encoding="utf-8")
    repository, remote = _init_repository(tmp_path)
    publisher = _publisher(repository, FakeRaw(remote))
    assert not publisher._target_contains_mandatory_removal(tree, "g00000001", ())
    assert publisher._target_contains_mandatory_removal(tree, "g00000001", ("source:blocked",))
    assert not publisher._target_contains_mandatory_removal(tree, "g00000001", ("source:absent",))


def test_bootstrap_deletion_identity_must_match_exact_event(tmp_path: Path) -> None:
    artifact_path = _artifact(
        tmp_path,
        kind=ReleaseKind.BOOTSTRAP,
        generation=1,
        previous_head=None,
        run_id="900",
        attempt=2,
        previous_last_success_at=None,
    )
    artifact = validate_publish_artifact(artifact_path, SCHEMAS)
    valid = {
        "status": "success",
        "release_kind": "bootstrap",
        "generation": 1,
        "active_release_id": "g00000001",
        "workflow_run_id": "900",
        "workflow_run_attempt": 2,
    }
    Publisher._assert_bootstrap_deletion_identity(artifact, valid)
    invalid = dict(valid, workflow_run_attempt=3)
    with pytest.raises(PublishError, match="identity check failed"):
        Publisher._assert_bootstrap_deletion_identity(artifact, invalid)


def test_publish_rejects_existing_generated_wrong_head_and_existing_candidate(
    tmp_path: Path,
) -> None:
    repository, remote = _init_repository(tmp_path)
    previous = _bootstrap(tmp_path, repository, remote, run_id="1000")

    second_bootstrap = _artifact(
        tmp_path,
        kind=ReleaseKind.BOOTSTRAP,
        generation=1,
        previous_head=None,
        run_id="1001",
        attempt=1,
        previous_last_success_at=None,
    )
    with pytest.raises(PublishError, match="expected generated to be absent"):
        _publisher(repository, FakeRaw(remote), run_id="1001").publish(second_bootstrap)

    wrong_head = _artifact(
        tmp_path,
        kind=ReleaseKind.REGULAR,
        generation=2,
        previous_head="a" * 40,
        run_id="1002",
        attempt=1,
        previous_last_success_at="2026-07-22T13:00:00Z",
    )
    with pytest.raises(PublishError, match="does not equal artifact expected"):
        _publisher(repository, FakeRaw(remote), run_id="1002").publish(wrong_head)

    candidate_ref = "candidate/run-1003-attempt-1"
    main_sha = _git(repository, "rev-parse", "main")
    _git(repository, "push", "origin", f"{main_sha}:refs/heads/{candidate_ref}")
    duplicate_candidate = _artifact(
        tmp_path,
        kind=ReleaseKind.REGULAR,
        generation=2,
        previous_head=previous,
        run_id="1003",
        attempt=1,
        previous_last_success_at="2026-07-22T13:00:00Z",
    )
    with pytest.raises(PublishError, match="candidate ref already exists"):
        _publisher(repository, FakeRaw(remote), run_id="1003").publish(duplicate_candidate)


def test_safety_publication_deletes_exact_denylisted_historical_release(
    tmp_path: Path,
) -> None:
    repository, remote = _init_repository(tmp_path)
    previous = _bootstrap(tmp_path, repository, remote, run_id="1100")
    previous_state = _json_at(remote, previous, "state/release.json")
    deleted_files = _bare_git(
        remote,
        "ls-tree",
        "-r",
        "--name-only",
        previous,
        "dist/releases/g00000001",
    ).splitlines()
    _sources, removed_vod, _live = _fixture_results("2026-07-22T12:00:00Z")
    trusted_schemas = _trusted_schemas(
        tmp_path,
        "trusted-safety-deletion",
        source_ids=("ikun-vod",),
    )
    raw = FakeRaw(remote)
    artifact = _artifact(
        tmp_path,
        kind=ReleaseKind.SAFETY,
        generation=2,
        previous_head=previous,
        run_id="1101",
        attempt=1,
        previous_last_success_at=str(previous_state["last_success_at"]),
        mandatory_removal_ids=(
            "source:ikun-vod",
            vod_entity_id(removed_vod.candidate),
        ),
        deletions=("dist/releases/g00000001",),
    )

    final_sha = _publisher(
        repository,
        raw,
        run_id="1101",
        schemas_dir=trusted_schemas,
    ).publish(artifact)

    assert not _bare_git(remote, "ls-tree", final_sha, "dist/releases/g00000001")
    final_state = _json_at(remote, final_sha, "state/release.json")
    assert final_state["required_absent_paths"] == deleted_files
    assert _bare_git(remote, "cat-file", "-t", f"{final_sha}:dist/releases/g00000002") == "tree"
    absent_call = next(call for call in raw.calls if call[0] == "absent")
    bare_index = next(index for index, call in enumerate(raw.calls) if call[0] == "bare")
    assert int(absent_call[2]) == len(deleted_files)
    assert raw.calls.index(absent_call) < bare_index


def test_regular_publication_can_remove_denylisted_history_only_release(
    tmp_path: Path,
) -> None:
    repository, remote = _init_repository(tmp_path)
    old_url = "https://old.example.test/stream.m3u8"
    new_url = "https://media.example.test/cctv1.m3u8"
    bootstrap = _artifact(
        tmp_path,
        kind=ReleaseKind.BOOTSTRAP,
        generation=1,
        previous_head=None,
        run_id="1110",
        attempt=1,
        previous_last_success_at=None,
        live_url=old_url,
    )
    first = _publisher(repository, FakeRaw(remote), run_id="1110").publish(bootstrap)
    first_state = _json_at(remote, first, "state/release.json")
    regular = _artifact(
        tmp_path,
        kind=ReleaseKind.REGULAR,
        generation=2,
        previous_head=first,
        run_id="1111",
        attempt=1,
        previous_last_success_at=str(first_state["last_success_at"]),
        live_url=new_url,
    )
    second = _publisher(repository, FakeRaw(remote), run_id="1111").publish(regular)

    trusted = tmp_path / "trusted"
    shutil.copytree(SCHEMAS, trusted / "schemas")
    shutil.copytree(PROJECT / "config", trusted / "config")
    (trusted / "sources").mkdir()
    shutil.copy2(PROJECT / "sources/registry.yaml", trusted / "sources/registry.yaml")
    (trusted / "sources/denylist.yaml").write_text(
        "version: 1\n"
        "entries:\n"
        "  - id: old-live-url\n"
        "    source_ids: []\n"
        "    hosts: []\n"
        f"    urls: [{old_url}]\n"
        "    reason: security\n"
        "    requested_at: 2026-07-22\n"
        "    evidence_urls: []\n",
        encoding="utf-8",
    )
    second_state = _json_at(remote, second, "state/release.json")
    cleanup = _artifact(
        tmp_path,
        kind=ReleaseKind.REGULAR,
        generation=3,
        previous_head=second,
        run_id="1112",
        attempt=1,
        previous_last_success_at=str(second_state["last_success_at"]),
        deletions=("dist/releases/g00000001",),
        live_url=new_url,
    )
    raw = FakeRaw(remote)
    final_sha = _publisher(
        repository,
        raw,
        run_id="1112",
        schemas_dir=trusted / "schemas",
    ).publish(cleanup)

    assert not _bare_git(remote, "ls-tree", final_sha, "dist/releases/g00000001")
    assert _bare_git(remote, "cat-file", "-t", f"{final_sha}:dist/releases/g00000002") == "tree"
    assert _bare_git(remote, "cat-file", "-t", f"{final_sha}:dist/releases/g00000003") == "tree"
    assert any(call[0] == "absent" for call in raw.calls)


def test_deletion_receipt_blocks_recovery_until_bare_paths_are_404(
    tmp_path: Path,
) -> None:
    repository, remote = _init_repository(tmp_path)
    previous = _bootstrap(tmp_path, repository, remote, run_id="1120")
    previous_state = _json_at(remote, previous, "state/release.json")
    deleted_files = _bare_git(
        remote,
        "ls-tree",
        "-r",
        "--name-only",
        previous,
        "dist/releases/g00000001",
    ).splitlines()
    _sources, removed_vod, _live = _fixture_results("2026-07-22T12:00:00Z")
    trusted_schemas = _trusted_schemas(
        tmp_path,
        "trusted-receipt-recovery",
        source_ids=("ikun-vod",),
    )
    safety = _artifact(
        tmp_path,
        kind=ReleaseKind.SAFETY,
        generation=2,
        previous_head=previous,
        run_id="1121",
        attempt=1,
        previous_last_success_at=str(previous_state["last_success_at"]),
        mandatory_removal_ids=(
            "source:ikun-vod",
            vod_entity_id(removed_vod.candidate),
        ),
        deletions=("dist/releases/g00000001",),
    )
    with pytest.raises(PublishError, match="delivery_unverified"):
        _publisher(
            repository,
            FakeRaw(remote, absent_failures=1),
            run_id="1121",
            schemas_dir=trusted_schemas,
        ).publish(safety)

    safe_head = _bare_git(remote, "rev-parse", "refs/heads/generated")
    safe_state = _json_at(remote, safe_head, "state/release.json")
    assert safe_state["required_absent_paths"] == deleted_files
    assert safe_state["last_success_at"] == previous_state["last_success_at"]

    recovery = _artifact(
        tmp_path,
        kind=ReleaseKind.REGULAR,
        generation=3,
        previous_head=safe_head,
        run_id="1122",
        attempt=1,
        previous_last_success_at=str(safe_state["last_success_at"]),
        previous_source_ids=frozenset({"iptv-org-cn-cctv"}),
        previous_vod_sites=0,
        previous_live_channels=1,
    )
    stale_raw = FakeRaw(remote, absent_failures=1)
    with pytest.raises(PublishError, match="still visible"):
        _publisher(repository, stale_raw, run_id="1122").publish(recovery)
    assert _bare_git(remote, "rev-parse", "refs/heads/generated") == safe_head
    assert _json_at(remote, safe_head, "state/release.json")[
        "required_absent_paths"
    ] == deleted_files

    recovered_head = _publisher(
        repository,
        FakeRaw(remote),
        run_id="1122",
    ).publish(recovery)
    recovered_state = _json_at(remote, recovered_head, "state/release.json")
    assert "required_absent_paths" not in recovered_state
    assert recovered_state["last_success_at"] == "2026-07-22T13:00:00Z"


def test_compensating_rollback_preserves_and_rechecks_deletion_receipt(
    tmp_path: Path,
) -> None:
    repository, remote = _init_repository(tmp_path)
    old_url = "https://rollback-history.example.test/stream.m3u8"
    new_url = "https://media.example.test/cctv1.m3u8"
    first_artifact = _artifact(
        tmp_path,
        kind=ReleaseKind.BOOTSTRAP,
        generation=1,
        previous_head=None,
        run_id="1130",
        attempt=1,
        previous_last_success_at=None,
        live_url=old_url,
    )
    first = _publisher(repository, FakeRaw(remote), run_id="1130").publish(
        first_artifact
    )
    deleted_files = _bare_git(
        remote,
        "ls-tree",
        "-r",
        "--name-only",
        first,
        "dist/releases/g00000001",
    ).splitlines()
    first_state = _json_at(remote, first, "state/release.json")
    second_artifact = _artifact(
        tmp_path,
        kind=ReleaseKind.REGULAR,
        generation=2,
        previous_head=first,
        run_id="1131",
        attempt=1,
        previous_last_success_at=str(first_state["last_success_at"]),
        live_url=new_url,
    )
    second = _publisher(repository, FakeRaw(remote), run_id="1131").publish(
        second_artifact
    )
    second_state = _json_at(remote, second, "state/release.json")
    trusted_schemas = _trusted_schemas(
        tmp_path,
        "trusted-rollback-receipt",
        urls=(old_url,),
    )
    cleanup = _artifact(
        tmp_path,
        kind=ReleaseKind.REGULAR,
        generation=3,
        previous_head=second,
        run_id="1132",
        attempt=1,
        previous_last_success_at=str(second_state["last_success_at"]),
        deletions=("dist/releases/g00000001",),
        live_url=new_url,
    )
    raw = FakeRaw(remote, bare_failures=1)
    with pytest.raises(PublishError, match="simulated bare Raw timeout"):
        _publisher(
            repository,
            raw,
            run_id="1132",
            schemas_dir=trusted_schemas,
        ).publish(cleanup)

    rollback_head = _bare_git(remote, "rev-parse", "refs/heads/generated")
    rollback_state = _json_at(remote, rollback_head, "state/release.json")
    assert rollback_state["release_kind"] == "rollback"
    assert rollback_state["required_absent_paths"] == deleted_files
    assert not _bare_git(remote, "ls-tree", rollback_head, "dist/releases/g00000001")
    assert len([call for call in raw.calls if call[0] == "absent"]) == 2


def test_publisher_requires_exact_github_event_identity(tmp_path: Path) -> None:
    repository, remote = _init_repository(tmp_path)
    artifact = _artifact(
        tmp_path,
        kind=ReleaseKind.BOOTSTRAP,
        generation=1,
        previous_head=None,
        run_id="1200",
        attempt=2,
        previous_last_success_at=None,
    )

    for environment, error in (
        ({}, "identity is missing"),
        (
            {"GITHUB_RUN_ID": "other", "GITHUB_RUN_ATTEMPT": "2"},
            "differs from the publisher event",
        ),
        (
            {"GITHUB_RUN_ID": "1200", "GITHUB_RUN_ATTEMPT": "zero"},
            "attempt is invalid",
        ),
    ):
        publisher = Publisher(
            repository=repository,
            schemas_dir=SCHEMAS,
            raw_verifier=FakeRaw(remote),  # type: ignore[arg-type]
            now=lambda: FIXED_NOW,
            environment=environment,
        )
        with pytest.raises(PublishError, match=error):
            publisher.publish(artifact)
    assert not _bare_git(remote, "show-ref", "--verify", "refs/heads/generated", check=False)


def test_publisher_recomputes_generation_from_previous_success_state(tmp_path: Path) -> None:
    repository, remote = _init_repository(tmp_path)
    previous = _bootstrap(tmp_path, repository, remote, run_id="1210")
    previous_state = _json_at(remote, previous, "state/release.json")
    skipped_generation = _artifact(
        tmp_path,
        kind=ReleaseKind.REGULAR,
        generation=3,
        previous_head=previous,
        run_id="1211",
        attempt=1,
        previous_last_success_at=str(previous_state["last_success_at"]),
    )

    with pytest.raises(PublishError, match="previous generation plus one"):
        _publisher(repository, FakeRaw(remote), run_id="1211").publish(skipped_generation)
    assert _bare_git(remote, "rev-parse", "refs/heads/generated") == previous


def test_publisher_recomputes_batch_failure_gate_from_previous_health(
    tmp_path: Path,
) -> None:
    repository, remote = _init_repository(tmp_path)
    regular_path = _artifact(
        tmp_path,
        kind=ReleaseKind.REGULAR,
        generation=2,
        previous_head="a" * 40,
        run_id="1220",
        attempt=1,
        previous_last_success_at="2026-07-22T13:00:00Z",
    )
    artifact = validate_publish_artifact(regular_path, SCHEMAS)
    baseline_path = _artifact(
        tmp_path,
        kind=ReleaseKind.BOOTSTRAP,
        generation=1,
        previous_head=None,
        run_id="1221",
        attempt=1,
        previous_last_success_at=None,
    )
    health = json.loads((baseline_path / "payload/dist/health.json").read_bytes())
    vod_source = next(source for source in health["sources"] if source["source_id"] == "ikun-vod")
    template = next(item for item in vod_source["items"] if item["entity_type"] == "vod_site")
    vod_source["items"] = [
        {**template, "entity_id": f"vod:ikun-vod:{index:016x}"} for index in range(5)
    ]
    worktree = tmp_path / "previous-tree"
    (worktree / "dist").mkdir(parents=True)
    (worktree / "dist/health.json").write_bytes(canonical_json_bytes(health))

    report_path = artifact.payload_root / "dist/reports/latest.json"
    report = json.loads(report_path.read_bytes())
    report["counts"]["previous_vod_sites"] = 5
    report["gate"]["inputs"]["previous_vod_items"] = 5
    report_path.write_bytes(canonical_json_bytes(report))
    publisher = _publisher(repository, FakeRaw(remote), run_id="1220")

    with pytest.raises(PublishError, match="conclusion differs"):
        publisher._validate_privileged_gate(worktree, artifact, "a" * 40)


def test_regular_deletion_cannot_remove_the_previously_active_release(tmp_path: Path) -> None:
    repository, remote = _init_repository(tmp_path)
    previous = _bootstrap(tmp_path, repository, remote, run_id="1230")
    previous_state = _json_at(remote, previous, "state/release.json")
    artifact = _artifact(
        tmp_path,
        kind=ReleaseKind.REGULAR,
        generation=2,
        previous_head=previous,
        run_id="1231",
        attempt=1,
        previous_last_success_at=str(previous_state["last_success_at"]),
        deletions=("dist/releases/g00000001",),
    )

    with pytest.raises(PublishError, match="previously active release"):
        _publisher(repository, FakeRaw(remote), run_id="1231").publish(artifact)
    assert _bare_git(remote, "rev-parse", "refs/heads/generated") == previous


def test_regular_detects_generated_race_after_bare_raw_verification(tmp_path: Path) -> None:
    repository, remote = _init_repository(tmp_path)
    previous = _bootstrap(tmp_path, repository, remote, run_id="1240")
    previous_state = _json_at(remote, previous, "state/release.json")
    raced: list[str] = []

    def advance_after_bare(published: str) -> None:
        if raced:
            return
        _git(repository, "checkout", "-B", "post-bare-race", published)
        (repository / "POST_BARE_RACE.txt").write_text("race\n", encoding="utf-8")
        _git(repository, "add", "POST_BARE_RACE.txt")
        _git(repository, "commit", "-m", "post bare race")
        race_sha = _git(repository, "rev-parse", "HEAD")
        _git(repository, "push", "origin", f"{race_sha}:refs/heads/generated")
        raced.append(race_sha)

    raw = FakeRaw(remote, bare_hook=advance_after_bare)
    artifact = _artifact(
        tmp_path,
        kind=ReleaseKind.REGULAR,
        generation=2,
        previous_head=previous,
        run_id="1241",
        attempt=1,
        previous_last_success_at=str(previous_state["last_success_at"]),
    )

    with pytest.raises(PublishError, match="changed before compensating rollback"):
        _publisher(repository, raw, run_id="1241").publish(artifact)
    assert raced
    assert _bare_git(remote, "rev-parse", "refs/heads/generated") == raced[0]


def test_safety_rejects_ghost_and_unbound_entity_mandatory_ids(
    tmp_path: Path,
) -> None:
    repository, remote = _init_repository(tmp_path)
    previous = _bootstrap(tmp_path, repository, remote, run_id="1300")
    previous_state = _json_at(remote, previous, "state/release.json")
    _sources, vod, _live = _fixture_results("2026-07-22T12:00:00Z")

    ghost = _artifact(
        tmp_path,
        kind=ReleaseKind.SAFETY,
        generation=2,
        previous_head=previous,
        run_id="1301",
        attempt=1,
        previous_last_success_at=str(previous_state["last_success_at"]),
        mandatory_removal_ids=("source:ghost-source",),
    )
    with pytest.raises(PublishError, match="mandatory source lacks trusted evidence"):
        _publisher(repository, FakeRaw(remote), run_id="1301").publish(ghost)

    unbound_entity = _artifact(
        tmp_path,
        kind=ReleaseKind.SAFETY,
        generation=2,
        previous_head=previous,
        run_id="1302",
        attempt=1,
        previous_last_success_at=str(previous_state["last_success_at"]),
        mandatory_removal_ids=(vod_entity_id(vod.candidate),),
    )
    with pytest.raises(PublishError, match="differ from trusted facts"):
        _publisher(repository, FakeRaw(remote), run_id="1302").publish(unbound_entity)
    assert _bare_git(remote, "rev-parse", "refs/heads/generated") == previous


def test_terms_changed_requires_independent_trusted_source_verification(
    tmp_path: Path,
) -> None:
    repository, remote = _init_repository(tmp_path)
    previous = _bootstrap(tmp_path, repository, remote, run_id="1310")
    previous_state = _json_at(remote, previous, "state/release.json")
    artifact = _artifact(
        tmp_path,
        kind=ReleaseKind.SAFETY,
        generation=2,
        previous_head=previous,
        run_id="1311",
        attempt=1,
        previous_last_success_at=str(previous_state["last_success_at"]),
    )
    checked: list[tuple[str, str]] = []

    def reject_unverified(source: SourceSpec, reason: str) -> bool:
        checked.append((source.id, reason))
        return False

    with pytest.raises(PublishError, match="mandatory source lacks trusted evidence"):
        _publisher(
            repository,
            FakeRaw(remote),
            run_id="1311",
            safety_fact_verifier=reject_unverified,
        ).publish(artifact)
    assert checked == [("ikun-vod", "terms_changed")]
    assert _bare_git(remote, "rev-parse", "refs/heads/generated") == previous


def test_safety_rejects_fully_sealed_live_injection_against_exact_parent(
    tmp_path: Path,
) -> None:
    repository, remote = _init_repository(tmp_path)
    previous = _bootstrap(tmp_path, repository, remote, run_id="1312")
    previous_state = _json_at(remote, previous, "state/release.json")
    injected = _artifact(
        tmp_path,
        kind=ReleaseKind.SAFETY,
        generation=2,
        previous_head=previous,
        run_id="1313",
        attempt=1,
        previous_last_success_at=str(previous_state["last_success_at"]),
        live_url="https://injected.example.test/new-stream.m3u8",
    )

    with pytest.raises(PublishError, match="mutated retained source facts"):
        _publisher(repository, FakeRaw(remote), run_id="1313").publish(injected)
    assert _bare_git(remote, "rev-parse", "refs/heads/generated") == previous


def test_safety_uses_exact_parent_when_retained_registry_endpoint_changes(
    tmp_path: Path,
) -> None:
    repository, remote = _init_repository(tmp_path)
    previous = _bootstrap(tmp_path, repository, remote, run_id="1316")
    previous_state = _json_at(remote, previous, "state/release.json")
    _sources, _vod, removed_live = _fixture_results("2026-07-22T12:00:00Z")
    trusted_schemas = _trusted_schemas(tmp_path, "changed-retained-registry")
    registry_path = trusted_schemas.parent / "sources/registry.yaml"
    registry = registry_path.read_text(encoding="utf-8")
    old_api = "https://ikunzyapi.com/api.php/provide/vod"
    new_api = "https://changed.example.test/api.php/provide/vod"
    assert registry.count(old_api) == 2
    registry = registry.replace(old_api, new_api).replace(
        "allowed_hosts: [ikunzyapi.com, www.ikunzy.com]",
        "allowed_hosts: [changed.example.test, www.ikunzy.com]",
    )
    registry = registry.replace("key: ikun_vod", "key: changed_ikun_vod").replace(
        "name: iKun 资源", "name: Changed iKun"
    )
    registry_path.write_text(registry, encoding="utf-8")
    safety = _artifact(
        tmp_path,
        kind=ReleaseKind.SAFETY,
        generation=2,
        previous_head=previous,
        run_id="1317",
        attempt=1,
        previous_last_success_at=str(previous_state["last_success_at"]),
        mandatory_removal_ids=(
            "source:iptv-org-cn-cctv",
            live_url_id(removed_live.candidate),
        ),
    )

    final_sha = _publisher(
        repository,
        FakeRaw(remote),
        run_id="1317",
        schemas_dir=trusted_schemas,
    ).publish(safety)
    config = _json_at(
        remote,
        final_sha,
        "dist/releases/g00000002/configs/stable.json",
    )
    assert config["sites"][0]["api"] == old_api
    assert config["sites"][0]["key"] == "ikun_vod"


def test_publisher_rejects_fully_sealed_forged_previous_source_audit(
    tmp_path: Path,
) -> None:
    repository, remote = _init_repository(tmp_path)
    previous = _bootstrap(tmp_path, repository, remote, run_id="1314")
    previous_state = _json_at(remote, previous, "state/release.json")
    forged = _artifact(
        tmp_path,
        kind=ReleaseKind.REGULAR,
        generation=2,
        previous_head=previous,
        run_id="1315",
        attempt=1,
        previous_last_success_at=str(previous_state["last_success_at"]),
        previous_source_ids=frozenset(),
        previous_public_unverified=2,
    )

    with pytest.raises(PublishError, match="change history differs from exact parent"):
        _publisher(repository, FakeRaw(remote), run_id="1315").publish(forged)
    assert _bare_git(remote, "rev-parse", "refs/heads/generated") == previous


def test_exact_history_scan_rejects_partial_set_then_removes_every_receipt(
    tmp_path: Path,
) -> None:
    repository, remote = _init_repository(tmp_path)
    old_url = "https://old-history.example.test/stream.m3u8"
    new_url = "https://media.example.test/cctv1.m3u8"
    first_artifact = _artifact(
        tmp_path,
        kind=ReleaseKind.BOOTSTRAP,
        generation=1,
        previous_head=None,
        run_id="1320",
        attempt=1,
        previous_last_success_at=None,
        live_url=old_url,
    )
    first = _publisher(repository, FakeRaw(remote), run_id="1320").publish(first_artifact)
    first_state = _json_at(remote, first, "state/release.json")
    second_artifact = _artifact(
        tmp_path,
        kind=ReleaseKind.REGULAR,
        generation=2,
        previous_head=first,
        run_id="1321",
        attempt=1,
        previous_last_success_at=str(first_state["last_success_at"]),
        live_url=old_url,
    )
    second = _publisher(repository, FakeRaw(remote), run_id="1321").publish(second_artifact)
    second_state = _json_at(remote, second, "state/release.json")
    third_artifact = _artifact(
        tmp_path,
        kind=ReleaseKind.REGULAR,
        generation=3,
        previous_head=second,
        run_id="1322",
        attempt=1,
        previous_last_success_at=str(second_state["last_success_at"]),
        live_url=new_url,
    )
    third = _publisher(repository, FakeRaw(remote), run_id="1322").publish(third_artifact)
    third_state = _json_at(remote, third, "state/release.json")
    trusted_schemas = _trusted_schemas(
        tmp_path,
        "trusted-multi-history",
        urls=(old_url,),
    )

    partial = _artifact(
        tmp_path,
        kind=ReleaseKind.REGULAR,
        generation=4,
        previous_head=third,
        run_id="1323",
        attempt=1,
        previous_last_success_at=str(third_state["last_success_at"]),
        deletions=("dist/releases/g00000001",),
        live_url=new_url,
    )
    with pytest.raises(PublishError, match="exact trusted scan"):
        _publisher(
            repository,
            FakeRaw(remote),
            run_id="1323",
            schemas_dir=trusted_schemas,
        ).publish(partial)

    deleted_files = [
        relative
        for release_id in ("g00000001", "g00000002")
        for relative in _bare_git(
            remote,
            "ls-tree",
            "-r",
            "--name-only",
            third,
            f"dist/releases/{release_id}",
        ).splitlines()
    ]
    complete = _artifact(
        tmp_path,
        kind=ReleaseKind.REGULAR,
        generation=4,
        previous_head=third,
        run_id="1324",
        attempt=1,
        previous_last_success_at=str(third_state["last_success_at"]),
        deletions=(
            "dist/releases/g00000001",
            "dist/releases/g00000002",
        ),
        live_url=new_url,
    )
    raw = FakeRaw(remote)
    final_sha = _publisher(
        repository,
        raw,
        run_id="1324",
        schemas_dir=trusted_schemas,
    ).publish(complete)
    absent = next(call for call in raw.calls if call[0] == "absent")
    assert int(absent[2]) == len(deleted_files)
    assert not _bare_git(remote, "ls-tree", final_sha, "dist/releases/g00000001")
    assert not _bare_git(remote, "ls-tree", final_sha, "dist/releases/g00000002")
    assert _bare_git(remote, "cat-file", "-t", f"{final_sha}:dist/releases/g00000003") == "tree"


def test_rollback_reloads_current_denylist_even_without_mandatory_ids(
    tmp_path: Path,
) -> None:
    repository, remote = _init_repository(tmp_path)
    old_url = "https://rollback-blocked.example.test/stream.m3u8"
    new_url = "https://media.example.test/cctv1.m3u8"
    first_artifact = _artifact(
        tmp_path,
        kind=ReleaseKind.BOOTSTRAP,
        generation=1,
        previous_head=None,
        run_id="1330",
        attempt=1,
        previous_last_success_at=None,
        live_url=old_url,
    )
    first = _publisher(repository, FakeRaw(remote), run_id="1330").publish(first_artifact)
    first_state = _json_at(remote, first, "state/release.json")
    trusted_schemas = _trusted_schemas(tmp_path, "trusted-rollback")
    denylist_path = trusted_schemas.parent / "sources/denylist.yaml"

    def block_target_after_promotion(_published: str) -> None:
        denylist_path.write_text(
            "version: 1\n"
            "entries:\n"
            "  - id: rollback-target\n"
            "    source_ids: []\n"
            "    hosts: []\n"
            f"    urls: [{old_url}]\n"
            "    reason: security\n"
            "    requested_at: 2026-07-22\n"
            "    evidence_urls: []\n",
            encoding="utf-8",
        )

    raw = FakeRaw(
        remote,
        bare_failures=1,
        bare_hook=block_target_after_promotion,
    )
    artifact = _artifact(
        tmp_path,
        kind=ReleaseKind.REGULAR,
        generation=2,
        previous_head=first,
        run_id="1331",
        attempt=1,
        previous_last_success_at=str(first_state["last_success_at"]),
        live_url=new_url,
    )
    with pytest.raises(PublishError, match="trusted removal trigger"):
        _publisher(
            repository,
            raw,
            run_id="1331",
            schemas_dir=trusted_schemas,
        ).publish(artifact)
    retained = _bare_git(remote, "rev-parse", "refs/heads/generated")
    state = _json_at(remote, retained, "state/release.json")
    assert state["release_kind"] == "regular"
    assert state["active_release_id"] == "g00000002"


def test_live_gate_baseline_ignores_healthy_withheld_duplicate_in_both_generations(
    tmp_path: Path,
) -> None:
    repository, remote = _init_repository(tmp_path)
    regular_path = _artifact(
        tmp_path,
        kind=ReleaseKind.REGULAR,
        generation=2,
        previous_head="a" * 40,
        run_id="1340",
        attempt=1,
        previous_last_success_at="2026-07-22T13:00:00Z",
    )
    artifact = validate_publish_artifact(regular_path, SCHEMAS)
    bootstrap_path = _artifact(
        tmp_path,
        kind=ReleaseKind.BOOTSTRAP,
        generation=1,
        previous_head=None,
        run_id="1341",
        attempt=1,
        previous_last_success_at=None,
    )

    def add_withheld_duplicate(health: dict[str, object]) -> None:
        sources = health["sources"]
        channels = health["channels"]
        assert isinstance(sources, list) and isinstance(channels, list)
        live_source = next(
            source for source in sources if source["source_id"] == "iptv-org-cn-cctv"
        )
        original = live_source["items"][0]
        duplicate = copy.deepcopy(original)
        duplicate["entity_id"] = "live-url:backup-live:0000000000000001"
        duplicate["publication_status"] = "withheld"
        backup_source = copy.deepcopy(live_source)
        backup_source["entity_id"] = "source:backup-live"
        backup_source["source_id"] = "backup-live"
        backup_source["publication_status"] = "withheld"
        backup_source["items"] = [duplicate]
        sources.append(backup_source)
        sources.sort(key=lambda item: item["source_id"])
        channels[0]["candidate_url_ids"].append(duplicate["entity_id"])
        channels[0]["candidate_url_ids"].sort()

    previous_health = json.loads((bootstrap_path / "payload/dist/health.json").read_bytes())
    current_health = json.loads((regular_path / "payload/dist/health.json").read_bytes())
    add_withheld_duplicate(previous_health)
    add_withheld_duplicate(current_health)
    worktree = tmp_path / "live-baseline-tree"
    (worktree / "dist").mkdir(parents=True)
    (worktree / "dist/health.json").write_bytes(canonical_json_bytes(previous_health))
    (artifact.payload_root / "dist/health.json").write_bytes(canonical_json_bytes(current_health))
    report_path = artifact.payload_root / "dist/reports/latest.json"
    report = json.loads(report_path.read_bytes())
    report["counts"]["previous_public_unverified"] = 3
    report["counts"]["current_public_unverified"] = 3
    backup = next(
        source for source in current_health["sources"] if source["source_id"] == "backup-live"
    )
    backup_snapshot = {
        "technical_status": backup["technical_status"],
        "publication_status": backup["publication_status"],
        "rights_status": backup["rights_status"],
    }
    report["sources"].append(
        {
            "source_id": "backup-live",
            **backup_snapshot,
            "failure_reason": backup["failure_reason"],
            "secondary_reasons": [],
            "upstream_revision": backup["upstream_revision"],
            "change_summary": build_change_summary(backup_snapshot, backup_snapshot),
        }
    )
    report["sources"].sort(key=lambda source: source["source_id"])
    report_path.write_bytes(canonical_json_bytes(report))

    previous_facts = Publisher._health_gate_facts(previous_health)
    current_facts = Publisher._health_gate_facts(current_health)
    assert len(previous_facts[1]) == len(current_facts[1]) == 1
    assert previous_facts[3] == current_facts[3] == 1
    _publisher(repository, FakeRaw(remote), run_id="1340")._validate_privileged_gate(
        worktree,
        artifact,
        "a" * 40,
    )
