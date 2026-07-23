"""Two-phase Git publication, delivery verification, and compensating rollback."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

from ds_tvbox.artifact import PublishArtifact, validate_publish_artifact
from ds_tvbox.bundle import materialize_bundle, validate_bundle
from ds_tvbox.errors import ContractError, PublishError
from ds_tvbox.gitops import Git, validate_sha
from ds_tvbox.http import ByteBudget, ConcurrencyLimits, SafeHttpClient
from ds_tvbox.manifests import prefixed_sha256
from ds_tvbox.models import FailureReason, ReleaseKind, RightsStatus, RunContext, SourceSpec
from ds_tvbox.policy import evaluate_gates
from ds_tvbox.raw import RawExpectedRelease, RawVerifier
from ds_tvbox.registry import load_registry, load_yaml_strict
from ds_tvbox.reports import build_change_summary, build_latest_report, render_latest_markdown
from ds_tvbox.schedule import validate_success_state
from ds_tvbox.serialization import write_bytes, write_json
from ds_tvbox.upstream import UpstreamFailure, resolve_upstream
from ds_tvbox.validation import load_json, validate_release_tree, validate_schema

_CONFIRMATION_PATHS = {
    "state/release.json",
    "dist/reports/latest.json",
    "dist/reports/latest.md",
}
_ROLLBACK_ROOT_PATHS = (
    "dist/index.json",
    "dist/warehouse.json",
    "dist/configs/stable.json",
    "dist/live/stable.m3u",
    "dist/manifest.json",
    "dist/health.json",
)
_RELEASE_DIRECTORY = re.compile(r"^dist/releases/g[0-9]{8}$")
_RELEASE_FILE = re.compile(r"^dist/releases/g[0-9]{8}/.+$")
_URL_TOKEN = re.compile(r"https?://[^\s\"'<>]+")
_NETWORK_GROUPS = frozenset({"github_raw", "dns_public", "cloudflare_http", "google_http"})
_MANDATORY_SECURITY_REASONS = frozenset(
    {
        FailureReason.CREDENTIAL_REQUIRED.value,
        FailureReason.CREDENTIAL_QUERY_REJECTED.value,
        FailureReason.CREDENTIAL_HEADER_REJECTED.value,
        FailureReason.INVALID_HEADER_SYNTAX.value,
        FailureReason.PRIVATE_ADDRESS_REJECTED.value,
        FailureReason.DANGEROUS_SCHEME_REJECTED.value,
        FailureReason.CLIENT_HTTP_DISALLOWED.value,
    }
)
_RIGHTS_VALUES = tuple(status.value for status in RightsStatus)
_TECHNICAL_AGGREGATION = (
    "healthy",
    "partial",
    "suspect",
    "unknown",
    "unsupported_environment",
    "dead",
)
_PUBLICATION_AGGREGATION = ("stable", "experimental", "withheld", "rejected")
_CHANNEL_RIGHTS_AGGREGATION = (
    "takedown",
    "restricted",
    "unknown",
    "public_unverified",
    "open_license",
    "verified",
)


@dataclass(frozen=True)
class _TrustedRemovalEvidence:
    mandatory_ids: tuple[str, ...]
    historical_ids: tuple[str, ...]


class Publisher:
    def __init__(
        self,
        *,
        repository: Path,
        schemas_dir: Path,
        raw_verifier: RawVerifier,
        now: Callable[[], datetime] | None = None,
        environment: Mapping[str, str] | None = None,
        safety_fact_verifier: Callable[[SourceSpec, str], bool] | None = None,
    ) -> None:
        self.repository = repository.resolve()
        self.schemas_dir = schemas_dir.resolve()
        self.git = Git(self.repository)
        self.raw = raw_verifier
        self.now = now or (lambda: datetime.now(UTC))
        self.environment = dict(os.environ if environment is None else environment)
        self.safety_fact_verifier = safety_fact_verifier or self._verify_source_safety_fact

    @staticmethod
    def _iso(value: datetime) -> str:
        return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _payload_files(artifact: PublishArtifact) -> dict[str, bytes]:
        return {
            path.relative_to(artifact.payload_root).as_posix(): path.read_bytes()
            for path in artifact.payload_root.rglob("*")
            if path.is_file() and not path.is_symlink()
        }

    @staticmethod
    def _remove_exact_release(worktree: Path, relative: str) -> None:
        unresolved = worktree / relative
        if unresolved.is_symlink():
            raise PublishError(f"unsafe release deletion target: {relative}")
        target = unresolved.resolve()
        release_root = (worktree / "dist/releases").resolve()
        if target.parent != release_root or not _RELEASE_DIRECTORY.fullmatch(relative):
            raise PublishError(f"unsafe release deletion target: {relative}")
        if target.exists():
            shutil.rmtree(target)

    def _assert_event_environment(self, artifact: PublishArtifact) -> None:
        run_id = self.environment.get("GITHUB_RUN_ID")
        raw_attempt = self.environment.get("GITHUB_RUN_ATTEMPT")
        if not run_id or not raw_attempt:
            raise PublishError("publisher GitHub workflow identity is missing")
        try:
            attempt = int(raw_attempt)
        except ValueError as error:
            raise PublishError("publisher GitHub workflow attempt is invalid") from error
        if attempt < 1:
            raise PublishError("publisher GitHub workflow attempt is invalid")
        if run_id != artifact.workflow_run_id or attempt != artifact.workflow_run_attempt:
            raise PublishError("artifact workflow identity differs from the publisher event")

    @staticmethod
    def _validate_transition(
        worktree: Path,
        artifact: PublishArtifact,
        current_head: str | None,
    ) -> None:
        if current_head is None:
            if artifact.release_kind is not ReleaseKind.BOOTSTRAP or artifact.generation != 1:
                raise PublishError("bootstrap transition must create generation 1")
            return
        previous = load_json(worktree / "state/release.json")
        if not isinstance(previous, Mapping):
            raise PublishError("previous generated state is invalid")
        try:
            stable = validate_success_state(previous)
        except ContractError as error:
            raise PublishError("previous generated state is not a successful baseline") from error
        if artifact.release_kind not in {ReleaseKind.REGULAR, ReleaseKind.SAFETY}:
            raise PublishError("existing generated ref requires a regular or safety transition")
        if artifact.generation != stable.generation + 1:
            raise PublishError("artifact generation is not previous generation plus one")

    def _trusted_policy(self) -> dict[str, int | float]:
        policy_path = self.schemas_dir.parent / "config/policy.yaml"
        try:
            policy = load_yaml_strict(policy_path.read_text(encoding="utf-8"))
        except OSError as error:
            raise PublishError("publisher cannot read its trusted policy") from error
        if not isinstance(policy, Mapping) or policy.get("version") != 1:
            raise PublishError("publisher trusted policy is invalid")
        minimums = policy.get("minimums")
        failures = policy.get("failure_gate")
        outage = policy.get("network_outage_gate")
        if not all(isinstance(value, Mapping) for value in (minimums, failures, outage)):
            raise PublishError("publisher trusted policy sections are invalid")
        assert isinstance(minimums, Mapping)
        assert isinstance(failures, Mapping)
        assert isinstance(outage, Mapping)
        values: dict[str, int | float] = {
            "minimum_vod_sites": minimums.get("vod_sites", 0),
            "minimum_live_channels": minimums.get("live_channels", 0),
            "minimum_previous_items": failures.get("minimum_previous_items", 0),
            "max_new_failure_ratio": failures.get("max_new_failure_ratio", -1),
            "failed_groups_to_abort": outage.get("failed_groups_to_abort", 0),
        }
        integer_keys = (
            "minimum_vod_sites",
            "minimum_live_channels",
            "minimum_previous_items",
            "failed_groups_to_abort",
        )
        if any(
            not isinstance(values[key], int)
            or isinstance(values[key], bool)
            or int(values[key]) < 1
            for key in integer_keys
        ):
            raise PublishError("publisher trusted policy integer is invalid")
        ratio = values["max_new_failure_ratio"]
        if (
            not isinstance(ratio, (int, float))
            or isinstance(ratio, bool)
            or not 0 <= float(ratio) <= 1
        ):
            raise PublishError("publisher trusted policy ratio is invalid")
        return values

    @staticmethod
    def _health_gate_facts(
        health: Mapping[str, Any],
    ) -> tuple[set[str], set[str], int, int]:
        channels = health.get("channels")
        sources = health.get("sources")
        if not isinstance(channels, list) or not isinstance(sources, list):
            raise PublishError("publisher health graph is invalid")
        published_live_ids: set[str] = set()
        live_channel_count = 0
        for channel in channels:
            if not isinstance(channel, Mapping):
                raise PublishError("publisher health channel is invalid")
            candidate_ids = channel.get("candidate_url_ids")
            if not isinstance(candidate_ids, list) or not all(
                isinstance(item, str) for item in candidate_ids
            ):
                raise PublishError("publisher health channel candidates are invalid")
            if channel.get("publication_status") == "stable":
                published_live_ids.update(candidate_ids)
                if isinstance(channel.get("selected_url_id"), str):
                    live_channel_count += 1

        vod_ids: set[str] = set()
        stable_vod_count = 0
        healthy_live_ids: set[str] = set()
        for source in sources:
            if not isinstance(source, Mapping) or not isinstance(source.get("items"), list):
                raise PublishError("publisher health source is invalid")
            for item in source["items"]:
                if not isinstance(item, Mapping) or not isinstance(item.get("entity_id"), str):
                    raise PublishError("publisher health item is invalid")
                entity_id = str(item["entity_id"])
                if item.get("entity_type") == "vod_site" and item.get("publication_status") in {
                    "stable",
                    "experimental",
                }:
                    vod_ids.add(entity_id)
                    if item.get("publication_status") == "stable":
                        stable_vod_count += 1
                if (
                    item.get("entity_type") == "live_url"
                    and item.get("technical_status") == "healthy"
                    and item.get("publication_status") == "stable"
                    and entity_id in published_live_ids
                ):
                    healthy_live_ids.add(entity_id)
        return vod_ids, healthy_live_ids, stable_vod_count, live_channel_count

    def _trusted_registry(self) -> tuple[SourceSpec, ...]:
        path = self.schemas_dir.parent / "sources/registry.yaml"
        try:
            return tuple(load_registry(path))
        except (OSError, ContractError) as error:
            raise PublishError("publisher cannot read its trusted registry") from error

    @staticmethod
    def _verify_source_safety_fact(source: SourceSpec, reason: str) -> bool:
        """Re-run a trusted source fetch before granting a destructive safety bypass."""

        if reason not in _MANDATORY_SECURITY_REASONS | {FailureReason.TERMS_CHANGED.value}:
            return False
        client = SafeHttpClient(
            budget=ByteBudget(20 * 1024 * 1024),
            concurrency=ConcurrencyLimits(2, 1),
        )
        try:
            resolve_upstream(source, client)
        except UpstreamFailure as error:
            return error.reason.value == reason
        except (ContractError, OSError, TimeoutError):
            return False
        return False

    @staticmethod
    def _health_entity_index(
        health: Mapping[str, Any],
    ) -> tuple[dict[str, set[str]], dict[str, str]]:
        by_source: dict[str, set[str]] = {}
        entity_sources: dict[str, str] = {}
        sources = health.get("sources")
        if not isinstance(sources, list):
            raise PublishError("publisher health sources are invalid")
        for source in sources:
            if (
                not isinstance(source, Mapping)
                or not isinstance(source.get("source_id"), str)
                or not isinstance(source.get("items"), list)
            ):
                raise PublishError("publisher health source is invalid")
            source_id = str(source["source_id"])
            if source_id in by_source:
                raise PublishError("publisher health source IDs are duplicated")
            entities: set[str] = set()
            for item in source["items"]:
                if not isinstance(item, Mapping) or not isinstance(item.get("entity_id"), str):
                    raise PublishError("publisher health item is invalid")
                entity_id = str(item["entity_id"])
                if entity_id in entity_sources:
                    raise PublishError("publisher health entity IDs are duplicated")
                if not entity_id.startswith((f"vod:{source_id}:", f"live-url:{source_id}:")):
                    raise PublishError("publisher health entity/source identity differs")
                entities.add(entity_id)
                entity_sources[entity_id] = source_id
            by_source[source_id] = entities
        return by_source, entity_sources

    @staticmethod
    def _report_source_index(report: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
        raw_sources = report.get("sources")
        if not isinstance(raw_sources, list):
            raise PublishError("publisher report sources are invalid")
        sources: dict[str, Mapping[str, Any]] = {}
        for source in raw_sources:
            if not isinstance(source, Mapping) or not isinstance(source.get("source_id"), str):
                raise PublishError("publisher report source is invalid")
            source_id = str(source["source_id"])
            if source_id in sources:
                raise PublishError("publisher report source IDs are duplicated")
            sources[source_id] = source
        return sources

    @staticmethod
    def _health_source_documents(
        health: Mapping[str, Any],
    ) -> dict[str, Mapping[str, Any]]:
        raw_sources = health.get("sources")
        if not isinstance(raw_sources, list):
            raise PublishError("publisher health sources are invalid")
        sources: dict[str, Mapping[str, Any]] = {}
        for source in raw_sources:
            if not isinstance(source, Mapping) or not isinstance(source.get("source_id"), str):
                raise PublishError("publisher health source is invalid")
            source_id = str(source["source_id"])
            if source_id in sources:
                raise PublishError("publisher health source IDs are duplicated")
            sources[source_id] = source
        return sources

    @staticmethod
    def _source_status_snapshot(source: Mapping[str, Any]) -> dict[str, str]:
        return {
            "technical_status": str(source.get("technical_status", "unknown")),
            "publication_status": str(source.get("publication_status", "withheld")),
            "rights_status": str(source.get("rights_status", "unknown")),
        }

    def _validate_report_audit(
        self,
        *,
        previous_health: Mapping[str, Any] | None,
        current_health: Mapping[str, Any],
        report: Mapping[str, Any],
        previous_vod_count: int,
        previous_live_count: int,
        current_vod_count: int,
        current_live_count: int,
    ) -> None:
        """Rebuild every historical report fact from the exact generated parent."""

        counts = report.get("counts")
        if not isinstance(counts, Mapping):
            raise PublishError("publisher report counts are invalid")
        previous = previous_health or {"sources": []}
        expected_counts = {
            "previous_vod_sites": previous_vod_count,
            "current_vod_sites": current_vod_count,
            "previous_live_channels": previous_live_count,
            "current_live_channels": current_live_count,
            **self._health_rights_counts(previous, "previous"),
            **self._health_rights_counts(current_health, "current"),
        }
        if dict(counts) != expected_counts:
            raise PublishError("publisher report counts differ from exact health history")

        previous_sources = self._health_source_documents(previous)
        current_sources = self._health_source_documents(current_health)
        report_sources = self._report_source_index(report)
        missing_rows = (set(previous_sources) | set(current_sources)).difference(report_sources)
        if missing_rows:
            raise PublishError(
                "publisher report omits audited sources: " + ", ".join(sorted(missing_rows))
            )
        for source_id, row in report_sources.items():
            previous_source = previous_sources.get(source_id)
            current_source = current_sources.get(source_id)
            if current_source is None:
                expected_summary = build_change_summary(previous_source, None)
            else:
                expected_current = self._source_status_snapshot(current_source)
                if self._source_status_snapshot(row) != expected_current:
                    raise PublishError(
                        f"publisher report current source differs from health: {source_id}"
                    )
                expected_summary = build_change_summary(previous_source, current_source)
            if row.get("change_summary") != expected_summary:
                raise PublishError(
                    f"publisher report change history differs from exact parent: {source_id}"
                )

    @staticmethod
    def _vod_config_entity_id(source_id: str, site: Mapping[str, Any]) -> str:
        site_type = site.get("type")
        api = site.get("api")
        if (
            not isinstance(site_type, int)
            or isinstance(site_type, bool)
            or not isinstance(api, str)
        ):
            raise PublishError("publisher safety config site identity is invalid")
        digest = hashlib.sha256(f"{site_type}{api}".encode()).hexdigest()[:16]
        return f"vod:{source_id}:{digest}"

    @staticmethod
    def _load_release_manifest(root: Path, release_id: str) -> Mapping[str, Any]:
        value = load_json(root / f"dist/releases/{release_id}/manifest.json")
        if not isinstance(value, Mapping):
            raise PublishError("publisher release manifest is invalid")
        return value

    @staticmethod
    def _release_config_paths(manifest: Mapping[str, Any], release_id: str) -> set[str]:
        artifacts = manifest.get("artifacts")
        if not isinstance(artifacts, Mapping):
            raise PublishError("publisher release artifacts are invalid")
        prefix = f"dist/releases/{release_id}/configs/"
        return {
            str(path).removeprefix(prefix)
            for path in artifacts
            if isinstance(path, str) and path.startswith(prefix) and path.endswith(".json")
        }

    @staticmethod
    def _m3u_urls(data: bytes) -> tuple[str, ...]:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as error:
            raise PublishError("publisher safety M3U is not UTF-8") from error
        urls = tuple(
            line.strip()
            for line in text.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )
        if len(urls) != len(set(urls)):
            raise PublishError("publisher safety M3U contains duplicate URLs")
        return urls

    @staticmethod
    def _rewrite_release_url(value: object, old_release_id: str, new_release_id: str) -> object:
        if not isinstance(value, str):
            return value
        old_token = f"/dist/releases/{old_release_id}/"
        if old_token not in value:
            return value
        return value.replace(old_token, f"/dist/releases/{new_release_id}/", 1)

    def _validate_safety_subtraction(
        self,
        *,
        worktree: Path,
        artifact: PublishArtifact,
        previous_health: Mapping[str, Any],
        current_health: Mapping[str, Any],
        evidence: _TrustedRemovalEvidence,
    ) -> None:
        """Prove a safety payload is only a trusted subtraction of its exact parent."""

        if artifact.release_kind is not ReleaseKind.SAFETY:
            return
        previous_release_id = previous_health.get("release_id")
        if not isinstance(previous_release_id, str):
            raise PublishError("publisher safety parent release is invalid")
        mandatory_sources = {
            identifier.removeprefix("source:")
            for identifier in evidence.mandatory_ids
            if identifier.startswith("source:")
        }
        mandatory_entities = {
            identifier
            for identifier in evidence.mandatory_ids
            if not identifier.startswith("source:")
        }
        previous_sources = self._health_source_documents(previous_health)
        current_sources = self._health_source_documents(current_health)
        expected_source_ids = set(previous_sources).difference(mandatory_sources)
        if set(current_sources) != expected_source_ids:
            raise PublishError("publisher safety health is not a pure source subtraction")

        retained_live: dict[str, Mapping[str, Any]] = {}
        retained_live_rights: dict[str, str] = {}
        for source_id in sorted(expected_source_ids):
            previous_source = previous_sources[source_id]
            raw_items = previous_source.get("items")
            if not isinstance(raw_items, list):
                raise PublishError("publisher safety parent source items are invalid")
            if any(
                not isinstance(item, Mapping) or not isinstance(item.get("entity_id"), str)
                for item in raw_items
            ):
                raise PublishError("publisher safety parent health item is invalid")
            retained_items = [
                dict(item)
                for item in raw_items
                if isinstance(item, Mapping) and item["entity_id"] not in mandatory_entities
            ]
            expected_source = dict(previous_source)
            # The health contract ties this envelope timestamp to generated_at.
            # It is not a probe observation; every nested retained fact remains exact.
            expected_source["last_checked_at"] = current_health.get("generated_at")
            expected_source["items"] = retained_items
            if (
                len(retained_items) != len(raw_items)
                and previous_source.get("failure_reason") is None
            ):
                expected_source["technical_status"] = next(
                    (
                        status
                        for status in _TECHNICAL_AGGREGATION
                        if any(item.get("technical_status") == status for item in retained_items)
                    ),
                    "unknown",
                )
                aggregate_publication = next(
                    (
                        status
                        for status in _PUBLICATION_AGGREGATION
                        if any(
                            item.get("publication_status") == status
                            for item in retained_items
                        )
                    ),
                    "withheld",
                )
                expected_source["publication_status"] = (
                    "rejected"
                    if previous_source.get("rights_status") in {"restricted", "takedown"}
                    else aggregate_publication
                )
            if dict(current_sources[source_id]) != expected_source:
                raise PublishError(
                    f"publisher safety mutated retained source facts: {source_id}"
                )
            for item in retained_items:
                if item.get("entity_type") == "live_url":
                    retained_live[str(item["entity_id"])] = item
                    retained_live_rights[str(item["entity_id"])] = str(
                        previous_source.get("rights_status")
                    )

        previous_channels_raw = previous_health.get("channels")
        current_channels_raw = current_health.get("channels")
        if not isinstance(previous_channels_raw, list) or not isinstance(
            current_channels_raw, list
        ):
            raise PublishError("publisher safety channel health is invalid")
        expected_channels: dict[str, tuple[Mapping[str, Any], list[str]]] = {}
        for channel in previous_channels_raw:
            if not isinstance(channel, Mapping) or not isinstance(channel.get("entity_id"), str):
                raise PublishError("publisher safety parent channel is invalid")
            candidates = channel.get("candidate_url_ids")
            if not isinstance(candidates, list) or not all(
                isinstance(item, str) for item in candidates
            ):
                raise PublishError("publisher safety parent channel candidates are invalid")
            retained = sorted(item for item in candidates if item in retained_live)
            if retained:
                expected_channels[str(channel["entity_id"])] = (channel, retained)
        current_channels: dict[str, Mapping[str, Any]] = {}
        for channel in current_channels_raw:
            if not isinstance(channel, Mapping) or not isinstance(channel.get("entity_id"), str):
                raise PublishError("publisher safety current channel is invalid")
            current_channels[str(channel["entity_id"])] = channel
        if len(current_channels) != len(current_channels_raw):
            raise PublishError("publisher safety current channel IDs are duplicated")
        if set(current_channels) != set(expected_channels):
            raise PublishError("publisher safety channels are not a pure URL subtraction")
        mutable_channel_fields = {
            "candidate_url_ids",
            "selected_url_id",
            "technical_status",
            "publication_status",
            "rights_status",
        }
        for channel_id, (previous_channel, candidates) in expected_channels.items():
            current_channel = current_channels[channel_id]
            if current_channel.get("candidate_url_ids") != candidates:
                raise PublishError("publisher safety channel candidates differ from subtraction")
            if current_channel.get("selected_url_id") not in {*candidates, None}:
                raise PublishError("publisher safety selected an injected live URL")
            selected_url_id = current_channel.get("selected_url_id")
            expected_technical = next(
                (
                    status
                    for status in _TECHNICAL_AGGREGATION
                    if any(
                        retained_live[item].get("technical_status") == status
                        for item in candidates
                    )
                ),
                "unknown",
            )
            expected_publication = (
                "stable"
                if selected_url_id is not None
                else next(
                    (
                        status
                        for status in _PUBLICATION_AGGREGATION
                        if any(
                            retained_live[item].get("publication_status") == status
                            for item in candidates
                        )
                    ),
                    "withheld",
                )
            )
            if isinstance(selected_url_id, str):
                expected_rights = retained_live_rights[selected_url_id]
            else:
                expected_rights = next(
                    status
                    for status in _CHANNEL_RIGHTS_AGGREGATION
                    if status in {retained_live_rights[item] for item in candidates}
                )
            if (
                current_channel.get("technical_status") != expected_technical
                or current_channel.get("publication_status") != expected_publication
                or current_channel.get("rights_status") != expected_rights
            ):
                raise PublishError("publisher safety channel aggregates are not derived")
            previous_fixed = {
                key: value
                for key, value in previous_channel.items()
                if key not in mutable_channel_fields
            }
            current_fixed = {
                key: value
                for key, value in current_channel.items()
                if key not in mutable_channel_fields
            }
            if current_fixed != previous_fixed:
                raise PublishError("publisher safety mutated retained channel facts")

        previous_manifest = self._load_release_manifest(worktree, previous_release_id)
        current_manifest = self._load_release_manifest(
            artifact.payload_root, artifact.release_id
        )
        previous_upstreams = previous_manifest.get("upstreams")
        current_upstreams = current_manifest.get("upstreams")
        if not isinstance(previous_upstreams, list) or not isinstance(current_upstreams, list):
            raise PublishError("publisher safety upstream records are invalid")
        expected_upstreams = [
            dict(item)
            for item in previous_upstreams
            if isinstance(item, Mapping) and item.get("source_id") in expected_source_ids
        ]
        if current_upstreams != expected_upstreams:
            raise PublishError("publisher safety upstreams are not a pure subtraction")

        previous_config_paths = self._release_config_paths(
            previous_manifest, previous_release_id
        )
        current_config_paths = self._release_config_paths(current_manifest, artifact.release_id)
        current_m3u = self._m3u_urls(
            (
                artifact.payload_root
                / f"dist/releases/{artifact.release_id}/live/stable.m3u"
            ).read_bytes()
        )
        current_lives: list[dict[str, object]] = []
        if current_m3u:
            current_lives = [
                {
                    "name": "DS 稳定直播",
                    "type": 0,
                    "url": (
                        f"https://raw.githubusercontent.com/{self.raw.owner}/"
                        f"{self.raw.repository}/generated/dist/releases/"
                        f"{artifact.release_id}/live/stable.m3u"
                    ),
                }
            ]
        previous_configs: dict[str, Mapping[str, Any]] = {}
        source_site_identities: dict[tuple[int, str], tuple[str, str]] = {}
        for name in sorted(previous_config_paths):
            document = load_json(
                worktree / f"dist/releases/{previous_release_id}/configs/{name}"
            )
            if not isinstance(document, Mapping):
                raise PublishError("publisher safety parent config is invalid")
            previous_configs[name] = document
            if name == "stable.json":
                continue
            source_id = name.removesuffix(".json")
            sites = document.get("sites")
            if not isinstance(sites, list):
                raise PublishError("publisher safety parent config sites are invalid")
            for site in sites:
                if not isinstance(site, Mapping):
                    raise PublishError("publisher safety parent config site is invalid")
                entity_id = self._vod_config_entity_id(source_id, site)
                source_site_identities[(int(site["type"]), str(site["api"]))] = (
                    source_id,
                    entity_id,
                )

        expected_configs: dict[str, dict[str, Any]] = {}
        for name, document in previous_configs.items():
            if name != "stable.json" and name.removesuffix(".json") not in expected_source_ids:
                continue
            sites = document.get("sites")
            lives = document.get("lives")
            if not isinstance(sites, list) or not isinstance(lives, list):
                raise PublishError("publisher safety parent config shape is invalid")
            retained_sites: list[object] = []
            for site in sites:
                if not isinstance(site, Mapping):
                    raise PublishError("publisher safety parent config site is invalid")
                if name == "stable.json":
                    identity = source_site_identities.get((int(site["type"]), str(site["api"])))
                    if identity is None:
                        raise PublishError("publisher safety stable site has no source identity")
                    _source_id, entity_id = identity
                else:
                    entity_id = self._vod_config_entity_id(name.removesuffix(".json"), site)
                if entity_id not in mandatory_entities:
                    retained_sites.append(dict(site))
            if name != "stable.json" and not retained_sites:
                continue
            expected_document = dict(document)
            expected_document["sites"] = retained_sites
            expected_document["lives"] = current_lives
            expected_configs[name] = expected_document
        if set(expected_configs) != current_config_paths:
            raise PublishError("publisher safety config set is not a pure subtraction")
        for name, expected_document in expected_configs.items():
            current = load_json(
                artifact.payload_root / f"dist/releases/{artifact.release_id}/configs/{name}"
            )
            if current != expected_document:
                raise PublishError(f"publisher safety mutated retained config facts: {name}")

        allowed_live_urls = {
            str(item["final_url"])
            for item in retained_live.values()
            if isinstance(item.get("final_url"), str)
        }
        if not set(current_m3u).issubset(allowed_live_urls):
            raise PublishError("publisher safety M3U introduced a new live URL")

    def _denylisted_registry_sources(
        self,
        blocked_sources: frozenset[str],
        blocked_hosts: frozenset[str],
        blocked_urls: frozenset[str],
    ) -> tuple[set[str], set[str]]:
        registry_sources: set[str] = set()
        restricted_sources: set[str] = set()
        for source in self._trusted_registry():
            if source.rights_status in {RightsStatus.RESTRICTED, RightsStatus.TAKEDOWN}:
                restricted_sources.add(source.id)
            registered_urls = (
                source.fetch.reviewed_url,
                source.fetch.repository_url,
                *(term.url for term in source.terms_watch),
            )
            if (
                source.id in blocked_sources
                or source.allowed_hosts.intersection(blocked_hosts)
                or any(
                    isinstance(value, str)
                    and self._matches_blocked_url(value, blocked_hosts, blocked_urls)
                    for value in registered_urls
                )
            ):
                registry_sources.add(source.id)
        return registry_sources, restricted_sources

    def _denylisted_active_sources(
        self,
        worktree: Path,
        release_id: str,
        health: Mapping[str, Any],
        blocked_sources: frozenset[str],
        blocked_hosts: frozenset[str],
        blocked_urls: frozenset[str],
    ) -> set[str]:
        by_source, _entities = self._health_entity_index(health)
        matched = set(blocked_sources).intersection(by_source)
        release_root = worktree / f"dist/releases/{release_id}"

        for source in health["sources"]:
            assert isinstance(source, Mapping)
            source_id = str(source["source_id"])
            for item in source["items"]:
                assert isinstance(item, Mapping)
                if any(
                    self._matches_blocked_url(str(item[key]), blocked_hosts, blocked_urls)
                    for key in ("normalized_url", "final_url", "logo", "epg")
                    if isinstance(item.get(key), str)
                ):
                    matched.add(source_id)
                    break

        manifest = load_json(release_root / "manifest.json")
        if not isinstance(manifest, Mapping) or not isinstance(manifest.get("upstreams"), list):
            raise PublishError("publisher active release upstreams are invalid")
        for upstream in manifest["upstreams"]:
            if not isinstance(upstream, Mapping) or not isinstance(upstream.get("source_id"), str):
                raise PublishError("publisher active release upstream is invalid")
            value = upstream.get("resolved_fetch_url")
            if isinstance(value, str) and self._matches_blocked_url(
                value, blocked_hosts, blocked_urls
            ):
                matched.add(str(upstream["source_id"]))

        for source_id in by_source:
            path = release_root / f"configs/{source_id}.json"
            if not path.is_file():
                continue
            document = load_json(path)
            if not isinstance(document, Mapping) or not isinstance(document.get("sites"), list):
                raise PublishError("publisher active release config is invalid")
            if any(
                isinstance(site, Mapping)
                and isinstance(site.get("api"), str)
                and self._matches_blocked_url(str(site["api"]), blocked_hosts, blocked_urls)
                for site in document["sites"]
            ):
                matched.add(source_id)
        return matched

    def _trusted_removal_evidence(
        self,
        worktree: Path,
        artifact: PublishArtifact,
        previous_health: Mapping[str, Any] | None,
        current_health: Mapping[str, Any],
        report: Mapping[str, Any],
    ) -> _TrustedRemovalEvidence:
        blocked_sources, blocked_hosts, blocked_urls = self._trusted_denylist()
        denylisted_registry, restricted_registry = self._denylisted_registry_sources(
            blocked_sources,
            blocked_hosts,
            blocked_urls,
        )
        historical_sources = set(blocked_sources) | denylisted_registry | restricted_registry
        if previous_health is None:
            if artifact.mandatory_removal_ids:
                raise PublishError("bootstrap artifact claims mandatory removals")
            return _TrustedRemovalEvidence(
                mandatory_ids=(),
                historical_ids=tuple(
                    sorted(f"source:{source_id}" for source_id in historical_sources)
                ),
            )

        previous_by_source, previous_entity_sources = self._health_entity_index(previous_health)
        denied_active: set[str] = set()
        if blocked_sources or blocked_hosts or blocked_urls:
            release_id = previous_health.get("release_id")
            if not isinstance(release_id, str):
                raise PublishError("publisher previous active release is invalid")
            denied_active = self._denylisted_active_sources(
                worktree,
                release_id,
                previous_health,
                blocked_sources,
                blocked_hosts,
                blocked_urls,
            )
        source_reasons: dict[str, str] = {}
        for source_id in denied_active:
            source_reasons[source_id] = "trusted_denylist"
        for source_id in denylisted_registry.intersection(previous_by_source):
            source_reasons[source_id] = "trusted_denylist"
        for source_id in restricted_registry.intersection(previous_by_source):
            source_reasons[source_id] = "trusted_registry_rights"

        observed_reasons: dict[str, str] = {}
        report_sources = self._report_source_index(report)
        for source_id, source in report_sources.items():
            reason = source.get("failure_reason")
            if source_id in previous_by_source and reason in _MANDATORY_SECURITY_REASONS | {
                FailureReason.TERMS_CHANGED.value
            }:
                observed_reasons[source_id] = str(reason)

        current_sources = current_health.get("sources")
        assert isinstance(current_sources, list)
        for source in current_sources:
            assert isinstance(source, Mapping)
            source_id = str(source["source_id"])
            reason = source.get("failure_reason")
            if source_id in previous_by_source and reason in _MANDATORY_SECURITY_REASONS | {
                FailureReason.TERMS_CHANGED.value
            }:
                observed_reasons[source_id] = str(reason)

        registry_by_id = {source.id: source for source in self._trusted_registry()}
        for source_id, reason in observed_reasons.items():
            trusted_source = registry_by_id.get(source_id)
            if trusted_source is not None and self.safety_fact_verifier(trusted_source, reason):
                source_reasons[source_id] = reason

        required_ids: set[str] = set()
        for source_id in source_reasons:
            required_ids.add(f"source:{source_id}")
            required_ids.update(previous_by_source[source_id])

        claimed = set(artifact.mandatory_removal_ids)
        claimed_sources = {
            identifier.removeprefix("source:")
            for identifier in claimed
            if identifier.startswith("source:")
        }
        unsupported_sources = claimed_sources.difference(source_reasons)
        if unsupported_sources:
            raise PublishError(
                "artifact mandatory source lacks trusted evidence: "
                + ", ".join(sorted(unsupported_sources))
            )

        claimed_entities = {
            identifier for identifier in claimed if not identifier.startswith("source:")
        }
        ghost_entities = claimed_entities.difference(previous_entity_sources)
        if ghost_entities:
            raise PublishError(
                "artifact mandatory entity is absent from the previous active release: "
                + ", ".join(sorted(ghost_entities))
            )
        if claimed != required_ids:
            missing = sorted(required_ids.difference(claimed))
            extra = sorted(claimed.difference(required_ids))
            raise PublishError(
                "artifact mandatory removals differ from trusted facts; "
                f"missing={missing}, extra={extra}"
            )

        historical_sources.update(denied_active)
        historical_sources.update(restricted_registry)
        historical_sources.update(
            source_id
            for source_id, reason in source_reasons.items()
            if reason != FailureReason.TERMS_CHANGED.value
        )
        historical_ids = {f"source:{source_id}" for source_id in historical_sources}
        return _TrustedRemovalEvidence(
            mandatory_ids=tuple(sorted(required_ids)),
            historical_ids=tuple(sorted(historical_ids)),
        )

    def _validate_privileged_gate(
        self,
        worktree: Path,
        artifact: PublishArtifact,
        current_head: str | None,
    ) -> _TrustedRemovalEvidence:
        current_health = load_json(artifact.payload_root / "dist/health.json")
        report = load_json(artifact.payload_root / "dist/reports/latest.json")
        if not isinstance(current_health, Mapping) or not isinstance(report, Mapping):
            raise PublishError("publisher artifact health/report is invalid")
        previous_health: Mapping[str, Any] = {}
        if current_head is not None:
            loaded_previous = load_json(worktree / "dist/health.json")
            if not isinstance(loaded_previous, Mapping):
                raise PublishError("publisher previous health is invalid")
            previous_health = loaded_previous
        previous_vod, previous_live, old_vod_count, old_live_count = (
            self._health_gate_facts(previous_health)
            if current_head is not None
            else (set(), set(), 0, 0)
        )
        current_vod, current_live, current_vod_count, current_live_count = self._health_gate_facts(
            current_health
        )
        removal_evidence = self._trusted_removal_evidence(
            worktree,
            artifact,
            previous_health if current_head is not None else None,
            current_health,
            report,
        )
        self._validate_report_audit(
            previous_health=previous_health if current_head is not None else None,
            current_health=current_health,
            report=report,
            previous_vod_count=old_vod_count,
            previous_live_count=old_live_count,
            current_vod_count=current_vod_count,
            current_live_count=current_live_count,
        )
        if current_head is not None:
            self._validate_safety_subtraction(
                worktree=worktree,
                artifact=artifact,
                previous_health=previous_health,
                current_health=current_health,
                evidence=removal_evidence,
            )
        gate = report.get("gate")
        if not isinstance(gate, Mapping) or not isinstance(gate.get("inputs"), Mapping):
            raise PublishError("publisher report gate is invalid")
        inputs = gate["inputs"]
        assert isinstance(inputs, Mapping)
        probes = gate.get("network_probes")
        if not isinstance(probes, list):
            raise PublishError("publisher network probes are invalid")
        groups: set[str] = set()
        failed_network_groups = 0
        for probe in probes:
            if (
                not isinstance(probe, Mapping)
                or not isinstance(probe.get("group"), str)
                or not isinstance(probe.get("passed"), bool)
            ):
                raise PublishError("publisher network probe entry is invalid")
            group = str(probe["group"])
            if group in groups:
                raise PublishError("publisher network probe groups are duplicated")
            groups.add(group)
            failed_network_groups += probe["passed"] is False
        if groups != set(_NETWORK_GROUPS):
            raise PublishError("publisher network probe set is incomplete")
        expected_inputs = {
            "previous_vod_items": len(previous_vod),
            "current_publishable_vod_items": len(current_vod),
            "previous_live_urls": len(previous_live),
            "current_healthy_live_urls": len(current_live),
            "current_vod_sites": current_vod_count,
            "current_live_channels": current_live_count,
            "failed_network_groups": failed_network_groups,
        }
        if dict(inputs) != expected_inputs:
            raise PublishError("publisher gate inputs differ from trusted health facts")
        policy = self._trusted_policy()
        decision = evaluate_gates(
            release_kind=artifact.release_kind,
            previous_vod_ids=previous_vod,
            current_publishable_vod_ids=current_vod,
            previous_live_url_ids=previous_live,
            current_healthy_live_url_ids=current_live,
            current_vod_sites=current_vod_count,
            current_live_channels=current_live_count,
            minimum_vod_sites=int(policy["minimum_vod_sites"]),
            minimum_live_channels=int(policy["minimum_live_channels"]),
            minimum_previous_items=int(policy["minimum_previous_items"]),
            max_new_failure_ratio=float(policy["max_new_failure_ratio"]),
            failed_network_groups=failed_network_groups,
            failed_groups_to_abort=int(policy["failed_groups_to_abort"]),
            state_available=True,
            previous_release_known=True,
            mandatory_removal_ids=removal_evidence.mandatory_ids,
        )
        expected_gate = {
            "publish": decision.publish,
            "inconclusive": decision.inconclusive,
            "release_kind": decision.release_kind.value,
            "reasons": list(decision.reasons),
            "mandatory_removal_ids": list(decision.mandatory_removal_ids),
        }
        if any(gate.get(key) != value for key, value in expected_gate.items()):
            raise PublishError("publisher gate conclusion differs from trusted recomputation")
        if not decision.publish:
            raise PublishError("publisher independently rejected the artifact gate")
        return removal_evidence

    def _release_matches_removal(
        self,
        worktree: Path,
        relative: str,
        identifiers: tuple[str, ...],
    ) -> bool:
        target = worktree / relative
        if not target.is_dir() or target.is_symlink():
            raise PublishError(f"historical release directory is unsafe: {relative}")
        blocked_sources, blocked_hosts, blocked_urls = self._trusted_denylist()
        source_identifiers = tuple(
            sorted({*identifiers, *(f"source:{item}" for item in blocked_sources)})
        )
        for path in sorted(target.rglob("*")):
            if path.is_symlink():
                raise PublishError(f"historical release contains a symlink: {relative}")
            if not path.is_file():
                continue
            path_name = path.relative_to(worktree).as_posix()
            data = path.read_bytes()
            if self._file_matches_mandatory(path_name, data, source_identifiers):
                return True
            if any(
                self._matches_blocked_url(value, blocked_hosts, blocked_urls)
                for value in self._text_values(data)
            ):
                return True
        return False

    def _expected_historical_deletions(
        self,
        worktree: Path,
        identifiers: tuple[str, ...],
    ) -> tuple[str, ...]:
        releases_root = worktree / "dist/releases"
        if not releases_root.exists():
            return ()
        expected: list[str] = []
        for target in sorted(releases_root.glob("g*")):
            relative = target.relative_to(worktree).as_posix()
            if not _RELEASE_DIRECTORY.fullmatch(relative):
                raise PublishError(f"historical release path is invalid: {relative}")
            if self._release_matches_removal(worktree, relative, identifiers):
                expected.append(relative)
        return tuple(expected)

    def _validate_deletion_scope(
        self,
        worktree: Path,
        artifact: PublishArtifact,
        evidence: _TrustedRemovalEvidence,
    ) -> None:
        if artifact.release_kind is ReleaseKind.REGULAR and artifact.deletions:
            previous = load_json(worktree / "state/release.json")
            if not isinstance(previous, Mapping) or not isinstance(
                previous.get("active_release_id"), str
            ):
                raise PublishError("publisher cannot prove regular deletion is historical-only")
            active_release = f"dist/releases/{previous['active_release_id']}"
            if active_release in artifact.deletions:
                raise PublishError("regular deletion cannot remove the previously active release")
        expected = self._expected_historical_deletions(worktree, evidence.historical_ids)
        if artifact.deletions != expected:
            raise PublishError(
                "artifact historical deletions differ from the exact trusted scan; "
                f"expected={list(expected)}, actual={list(artifact.deletions)}"
            )

    @staticmethod
    def _raw_expectation(
        worktree: Path,
        *,
        event_generation: int,
        workflow_run_id: str,
        workflow_run_attempt: int,
        sealed_root_manifest_sha256: str | None = None,
        sealed_release_manifest_sha256: str | None = None,
    ) -> RawExpectedRelease:
        root_path = worktree / "dist/manifest.json"
        root_bytes = root_path.read_bytes()
        root_sha = prefixed_sha256(root_bytes)
        if sealed_root_manifest_sha256 is not None and root_sha != sealed_root_manifest_sha256:
            raise PublishError("materialized root manifest differs from the sealed artifact")
        root = load_json(root_path)
        if not isinstance(root, Mapping):
            raise PublishError("root manifest is invalid while sealing Raw expectations")
        pointer = root.get("release_manifest")
        aliases = root.get("aliases")
        if not isinstance(pointer, Mapping) or not isinstance(aliases, Mapping):
            raise PublishError("root manifest pointers are invalid while sealing Raw expectations")
        release_path = pointer.get("path")
        release_sha = pointer.get("sha256")
        if not isinstance(release_path, str) or not isinstance(release_sha, str):
            raise PublishError("release manifest pointer is invalid while sealing Raw expectations")
        if (
            sealed_release_manifest_sha256 is not None
            and release_sha != sealed_release_manifest_sha256
        ):
            raise PublishError("materialized release manifest differs from the sealed artifact")
        release = load_json(worktree / release_path)
        if not isinstance(release, Mapping):
            raise PublishError("release manifest is invalid while sealing Raw expectations")
        state = load_json(worktree / "state/release.json")
        if not isinstance(state, Mapping):
            raise PublishError("release state is invalid while sealing Raw expectations")
        return RawExpectedRelease(
            release_id=str(root.get("active_release_id")),
            release_generation=int(release.get("generation", 0)),
            event_generation=event_generation,
            workflow_run_id=workflow_run_id,
            workflow_run_attempt=workflow_run_attempt,
            content_workflow_run_id=str(root.get("content_workflow_run_id", "")),
            content_workflow_run_attempt=int(root.get("content_workflow_run_attempt", 0)),
            root_manifest_sha256=root_sha,
            release_manifest_sha256=release_sha,
            aliases={str(key): str(value) for key, value in aliases.items()},
            required_absent_paths=Publisher._required_absent_paths(state),
        )

    def _trusted_denylist(self) -> tuple[frozenset[str], frozenset[str], frozenset[str]]:
        path = self.schemas_dir.parent / "sources/denylist.yaml"
        try:
            value = load_yaml_strict(path.read_text(encoding="utf-8"))
        except OSError as error:
            raise PublishError("publisher cannot read its trusted denylist") from error
        if not isinstance(value, Mapping):
            raise PublishError("publisher trusted denylist is invalid")
        validate_schema(value, self.schemas_dir / "denylist.schema.json")
        source_ids: set[str] = set()
        hosts: set[str] = set()
        urls: set[str] = set()
        entries = value.get("entries")
        assert isinstance(entries, list)
        for entry in entries:
            assert isinstance(entry, Mapping)
            source_ids.update(str(item) for item in entry["source_ids"])
            hosts.update(str(item).lower() for item in entry["hosts"])
            urls.update(str(item) for item in entry["urls"])
        return frozenset(source_ids), frozenset(hosts), frozenset(urls)

    @staticmethod
    def _text_values(data: bytes) -> tuple[str, ...]:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return ()
        values: list[str] = list(_URL_TOKEN.findall(text))
        try:
            document = json.loads(text)
        except json.JSONDecodeError:
            return tuple(values)
        stack: list[object] = [document]
        while stack:
            value = stack.pop()
            if isinstance(value, Mapping):
                stack.extend(value.values())
            elif isinstance(value, list):
                stack.extend(value)
            elif isinstance(value, str):
                values.append(value)
        return tuple(values)

    @staticmethod
    def _matches_blocked_url(
        value: str,
        blocked_hosts: frozenset[str],
        blocked_urls: frozenset[str],
    ) -> bool:
        if value in blocked_urls:
            return True
        try:
            host = urlsplit(value).hostname
        except ValueError:
            return False
        return host is not None and host.lower() in blocked_hosts

    def _deletion_receipt(
        self,
        worktree: Path,
        relative: str,
    ) -> tuple[str, ...]:
        if not _RELEASE_DIRECTORY.fullmatch(relative):
            raise PublishError(f"unsafe release deletion target: {relative}")
        target = worktree / relative
        if not target.is_dir() or target.is_symlink():
            raise PublishError(f"historical release deletion target is missing: {relative}")
        entries = tuple(sorted(target.rglob("*")))
        if any(path.is_symlink() for path in entries):
            raise PublishError(f"historical release deletion target is unsafe: {relative}")
        paths = tuple(path.relative_to(worktree).as_posix() for path in entries if path.is_file())
        if not paths:
            raise PublishError("historical release deletion has an empty file receipt")
        return paths

    @staticmethod
    def _required_absent_paths(state: Mapping[str, Any]) -> tuple[str, ...]:
        raw = state.get("required_absent_paths", [])
        if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
            raise PublishError("release state deletion receipt is invalid")
        receipt = tuple(raw)
        if len(receipt) > 10_000 or receipt != tuple(sorted(set(receipt))):
            raise PublishError("release state deletion receipt is invalid")
        for relative in receipt:
            path = PurePosixPath(relative)
            if (
                path.as_posix() != relative
                or not _RELEASE_FILE.fullmatch(relative)
                or "." in path.parts
                or ".." in path.parts
            ):
                raise PublishError("release state deletion receipt path is unsafe")
        return receipt

    @classmethod
    def _write_required_absent_paths(
        cls,
        worktree: Path,
        relatives: tuple[str, ...],
    ) -> None:
        state_path = worktree / "state/release.json"
        state = load_json(state_path)
        if not isinstance(state, dict):
            raise PublishError("materialized release state is invalid")
        candidate = dict(state)
        if relatives:
            candidate["required_absent_paths"] = list(relatives)
        else:
            candidate.pop("required_absent_paths", None)
        cls._required_absent_paths(candidate)
        write_json(state_path, candidate)

    @staticmethod
    def _file_matches_mandatory(
        relative: str,
        data: bytes,
        mandatory_ids: tuple[str, ...],
    ) -> bool:
        for identifier in mandatory_ids:
            if identifier.encode() in data:
                return True
            if identifier.startswith("source:"):
                source_id = identifier.removeprefix("source:")
                if relative.endswith(f"/configs/{source_id}.json"):
                    return True
                try:
                    document = json.loads(data)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                stack: list[object] = [document]
                while stack:
                    value = stack.pop()
                    if isinstance(value, Mapping):
                        if value.get("source_id") == source_id:
                            return True
                        stack.extend(value.values())
                    elif isinstance(value, list):
                        stack.extend(value)
        return False

    def _confirm(self, worktree: Path, artifact: PublishArtifact, content_sha: str) -> None:
        state_path = worktree / "state/release.json"
        report_path = worktree / "dist/reports/latest.json"
        state = load_json(state_path)
        report = load_json(report_path)
        if not isinstance(state, dict) or not isinstance(report, dict):
            raise ContractError("pending state/report must be objects")
        if state.get("status") != "pending" or report.get("status") != "pending":
            raise ContractError("publisher expected pending state/report")
        timestamp = self._iso(self.now())
        state["status"] = "success"
        state["content_commit_sha"] = validate_sha(content_sha)
        state["last_publish_at"] = timestamp
        if artifact.release_kind in {ReleaseKind.BOOTSTRAP, ReleaseKind.REGULAR}:
            state["last_success_at"] = timestamp
        gate = report.get("gate")
        safety_degraded = (
            artifact.release_kind is ReleaseKind.SAFETY
            and isinstance(gate, dict)
            and "safety_degraded" in gate.get("reasons", [])
        )
        report["status"] = "safety_degraded" if safety_degraded else "success"
        report["content_commit_sha"] = content_sha
        report["finished_at"] = timestamp
        report["candidate_ref"] = (
            f"candidate/run-{artifact.workflow_run_id}-attempt-{artifact.workflow_run_attempt}"
        )
        validate_schema(report, self.schemas_dir / "report.schema.json")
        write_json(state_path, state)
        write_json(report_path, report)
        write_bytes(worktree / "dist/reports/latest.md", render_latest_markdown(report))

    def _verify_confirmation_diff(self, content_sha: str, final_sha: str, worktree: Path) -> None:
        changed = set(self.git.changed_paths(content_sha, final_sha, cwd=worktree))
        if changed != _CONFIRMATION_PATHS:
            raise PublishError(f"confirmation commit changed forbidden paths: {sorted(changed)}")

    def _target_contains_mandatory_removal(
        self,
        target_tree: Path,
        release_id: str,
        mandatory_ids: tuple[str, ...],
    ) -> bool:
        blocked_sources, blocked_hosts, blocked_urls = self._trusted_denylist()
        denylisted_registry, restricted_registry = self._denylisted_registry_sources(
            blocked_sources,
            blocked_hosts,
            blocked_urls,
        )
        trusted_ids = tuple(
            sorted(
                {
                    *mandatory_ids,
                    *(
                        f"source:{source_id}"
                        for source_id in (
                            set(blocked_sources) | denylisted_registry | restricted_registry
                        )
                    ),
                }
            )
        )
        if self._release_matches_removal(
            target_tree,
            f"dist/releases/{release_id}",
            trusted_ids,
        ):
            return True
        roots = [target_tree / relative for relative in _ROLLBACK_ROOT_PATHS]
        for path in roots:
            if path.is_file() and not path.is_symlink():
                relative = path.relative_to(target_tree).as_posix()
                data = path.read_bytes()
                if self._file_matches_mandatory(
                    relative,
                    data,
                    trusted_ids,
                ) or any(
                    self._matches_blocked_url(value, blocked_hosts, blocked_urls)
                    for value in self._text_values(data)
                ):
                    return True
        return False

    def _validate_rollback_target(
        self,
        target_head: str,
        mandatory_ids: tuple[str, ...],
    ) -> tuple[
        dict[str, Any],
        str,
        dict[str, Any],
        dict[str, Any],
        RawExpectedRelease,
    ]:
        with self.git.worktree(target_head) as target_tree:
            validated = validate_bundle(target_tree, schemas_dir=self.schemas_dir)
            validate_release_tree(
                target_tree,
                self.schemas_dir,
                owner=self.raw.owner,
                repository=self.raw.repository,
                expected_status="success",
            )
            state = load_json(target_tree / "state/release.json")
            if not isinstance(state, dict) or state.get("status") != "success":
                raise PublishError("rollback target state is not successful")
            if state.get("active_release_id") != validated.release_id:
                raise PublishError("rollback target state and release differ")
            content_sha = state.get("content_commit_sha")
            if not isinstance(content_sha, str):
                raise PublishError("rollback target has no content commit SHA")
            validate_sha(content_sha)
            if self._target_contains_mandatory_removal(
                target_tree,
                validated.release_id,
                mandatory_ids,
            ):
                raise PublishError("rollback target still contains a trusted removal trigger")
            report = load_json(target_tree / "dist/reports/latest.json")
            health = load_json(target_tree / "dist/health.json")
            if not isinstance(report, dict) or not isinstance(health, dict):
                raise PublishError("rollback target report/health is invalid")
            expectation = self._raw_expectation(
                target_tree,
                event_generation=int(state["generation"]),
                workflow_run_id=str(state["workflow_run_id"]),
                workflow_run_attempt=int(state["workflow_run_attempt"]),
            )
            self.raw.poll_revision(target_head, expected=expectation)
            return state, validated.release_id, report, health, expectation

    @staticmethod
    def _health_rights_counts(health: Mapping[str, Any], prefix: str) -> dict[str, int]:
        counts = {f"{prefix}_{value}": 0 for value in _RIGHTS_VALUES}
        sources = health.get("sources")
        if not isinstance(sources, list):
            raise PublishError("rollback health sources are invalid")
        for source in sources:
            if not isinstance(source, Mapping):
                raise PublishError("rollback health source is invalid")
            key = f"{prefix}_{source.get('rights_status')}"
            if key not in counts:
                raise PublishError("rollback health rights status is invalid")
            counts[key] += 1
        return counts

    @staticmethod
    def _rollback_source_rows(
        bad_health: Mapping[str, Any],
        bad_report: Mapping[str, Any],
        target_health: Mapping[str, Any],
        target_report: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        def health_index(value: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
            sources = value.get("sources")
            if not isinstance(sources, list):
                raise PublishError("rollback health sources are invalid")
            return {
                str(item["source_id"]): item
                for item in sources
                if isinstance(item, Mapping) and isinstance(item.get("source_id"), str)
            }

        def report_index(value: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
            raw_sources = value.get("sources")
            if not isinstance(raw_sources, list):
                raise PublishError("rollback report sources are invalid")
            return {
                str(item["source_id"]): item
                for item in raw_sources
                if isinstance(item, Mapping) and isinstance(item.get("source_id"), str)
            }

        def report_current(item: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
            if item is None:
                return None
            summary = item.get("change_summary")
            if not isinstance(summary, Mapping):
                raise PublishError("rollback report source change is invalid")
            current = summary.get("current")
            if current is None:
                return None
            if not isinstance(current, Mapping):
                raise PublishError("rollback report current source is invalid")
            return current

        bad_health_sources = health_index(bad_health)
        target_health_sources = health_index(target_health)
        bad_report_sources = report_index(bad_report)
        target_report_sources = report_index(target_report)
        source_ids = (
            set(bad_health_sources)
            | set(target_health_sources)
            | set(bad_report_sources)
            | set(target_report_sources)
        )
        rows: list[dict[str, Any]] = []
        for source_id in sorted(source_ids):
            previous = bad_health_sources.get(source_id) or report_current(
                bad_report_sources.get(source_id)
            )
            current = target_health_sources.get(source_id) or report_current(
                target_report_sources.get(source_id)
            )
            display = current or previous
            if display is None:
                continue
            metadata = target_report_sources.get(source_id, {})
            rows.append(
                {
                    "source_id": source_id,
                    "technical_status": str(display.get("technical_status", "unknown")),
                    "publication_status": str(display.get("publication_status", "withheld")),
                    "rights_status": str(display.get("rights_status", "unknown")),
                    "failure_reason": (
                        current.get("failure_reason")
                        if current is not None
                        else metadata.get("failure_reason")
                    ),
                    "secondary_reasons": list(metadata.get("secondary_reasons", [])),
                    "upstream_revision": (
                        current.get("upstream_revision")
                        if current is not None
                        else metadata.get("upstream_revision")
                    ),
                    "change_summary": build_change_summary(previous, current),
                }
            )
        return rows

    def _build_rollback_report(
        self,
        *,
        artifact: PublishArtifact,
        bad_head: str,
        bad_state: Mapping[str, Any],
        bad_health: Mapping[str, Any],
        bad_report: Mapping[str, Any],
        target_state: Mapping[str, Any],
        target_release_id: str,
        target_health: Mapping[str, Any],
        target_report: Mapping[str, Any],
        root_manifest: Mapping[str, Any],
        timestamp: str,
    ) -> dict[str, Any]:
        previous_vod, previous_live, previous_vod_count, previous_live_count = (
            self._health_gate_facts(bad_health)
        )
        current_vod, current_live, current_vod_count, current_live_count = self._health_gate_facts(
            target_health
        )
        bad_gate = bad_report.get("gate")
        if not isinstance(bad_gate, Mapping) or not isinstance(
            bad_gate.get("network_probes"), list
        ):
            raise PublishError("bad release report gate is invalid during rollback")
        probes = [dict(item) for item in bad_gate["network_probes"]]
        failed_network_groups = sum(item.get("passed") is not True for item in probes)
        policy = self._trusted_policy()
        generation = int(bad_state["generation"]) + 1
        context = RunContext(
            owner=self.raw.owner,
            repository=self.raw.repository,
            generated_ref="generated",
            workflow_run_id=artifact.workflow_run_id,
            workflow_run_attempt=artifact.workflow_run_attempt,
            generated_at=timestamp,
            generation=generation,
            release_kind=ReleaseKind.ROLLBACK,
            previous_head=bad_head,
            previous_last_success_at=(
                str(target_state["last_success_at"])
                if target_state.get("last_success_at") is not None
                else None
            ),
        )
        counts = {
            "previous_vod_sites": previous_vod_count,
            "current_vod_sites": current_vod_count,
            "previous_live_channels": previous_live_count,
            "current_live_channels": current_live_count,
            **self._health_rights_counts(bad_health, "previous"),
            **self._health_rights_counts(target_health, "current"),
        }
        entity_failures = [
            str(item["failure_reason"])
            for source in target_health["sources"]
            if isinstance(source, Mapping)
            for item in source["items"]
            if isinstance(item, Mapping) and item.get("failure_reason") is not None
        ]
        report = build_latest_report(
            context,
            status="success",
            started_at=timestamp,
            finished_at=timestamp,
            due=False,
            forced=True,
            recovery_due=True,
            sources=self._rollback_source_rows(
                bad_health, bad_report, target_health, target_report
            ),
            counts=counts,
            gate={
                "publish": True,
                "inconclusive": False,
                "release_kind": ReleaseKind.ROLLBACK.value,
                "reasons": [],
                "mandatory_removal_ids": [],
                "historical_deletions": [],
                "inputs": {
                    "previous_vod_items": len(previous_vod),
                    "current_publishable_vod_items": len(current_vod),
                    "previous_live_urls": len(previous_live),
                    "current_healthy_live_urls": len(current_live),
                    "current_vod_sites": current_vod_count,
                    "current_live_channels": current_live_count,
                    "failed_network_groups": failed_network_groups,
                },
                "thresholds": {
                    "minimum_vod_sites": int(policy["minimum_vod_sites"]),
                    "minimum_live_channels": int(policy["minimum_live_channels"]),
                    "minimum_previous_items": int(policy["minimum_previous_items"]),
                    "max_new_failure_ratio": float(policy["max_new_failure_ratio"]),
                    "failed_groups_to_abort": int(policy["failed_groups_to_abort"]),
                },
                "network_probes": probes,
            },
            previous_release_head_sha=bad_head,
            content_commit_sha=(
                str(target_state["content_commit_sha"])
                if target_state.get("content_commit_sha") is not None
                else None
            ),
            candidate_ref=context.candidate_ref,
            content_identity={
                "workflow_run_id": root_manifest["content_workflow_run_id"],
                "workflow_run_attempt": root_manifest["content_workflow_run_attempt"],
            },
            entity_failure_reasons=entity_failures,
        )
        report["active_release_id"] = target_release_id
        return report

    def _compensating_rollback(
        self,
        worktree: Path,
        artifact: PublishArtifact,
        bad_head: str,
    ) -> str:
        bad_state = load_json(worktree / "state/release.json")
        bad_health = load_json(worktree / "dist/health.json")
        bad_report = load_json(worktree / "dist/reports/latest.json")
        if (
            not isinstance(bad_state, dict)
            or not isinstance(bad_health, dict)
            or not isinstance(bad_report, dict)
        ):
            raise PublishError("bad release state/report/health is invalid")
        target_head_raw = bad_state.get("previous_release_head_sha")
        if not isinstance(target_head_raw, str):
            raise PublishError("regular release has no rollback target")
        target_head = validate_sha(target_head_raw)
        self.git.fetch_sha(target_head)
        (
            target_state,
            target_release_id,
            target_report,
            target_health,
            _target_expectation,
        ) = self._validate_rollback_target(
            target_head,
            artifact.mandatory_removal_ids,
        )

        self.git.run(
            "checkout",
            target_head,
            "--",
            *_ROLLBACK_ROOT_PATHS,
            f"dist/releases/{target_release_id}",
            cwd=worktree,
        )
        root_manifest = load_json(worktree / "dist/manifest.json")
        if not isinstance(root_manifest, dict):
            raise PublishError("rollback target root manifest is invalid")
        timestamp = self._iso(self.now())
        rollback_state = {
            "schema_version": "1.0.0",
            "status": "success",
            "release_kind": "rollback",
            "generation": int(bad_state["generation"]) + 1,
            "active_release_id": target_state["active_release_id"],
            "last_publish_at": timestamp,
            "last_success_at": target_state.get("last_success_at"),
            "content_commit_sha": target_state.get("content_commit_sha"),
            "previous_release_head_sha": bad_head,
            "workflow_run_id": artifact.workflow_run_id,
            "workflow_run_attempt": artifact.workflow_run_attempt,
        }
        bad_receipt = self._required_absent_paths(bad_state)
        if bad_receipt:
            rollback_state["required_absent_paths"] = list(bad_receipt)
        report = self._build_rollback_report(
            artifact=artifact,
            bad_head=bad_head,
            bad_state=bad_state,
            bad_health=bad_health,
            bad_report=bad_report,
            target_state=target_state,
            target_release_id=target_release_id,
            target_health=target_health,
            target_report=target_report,
            root_manifest=root_manifest,
            timestamp=timestamp,
        )
        write_json(worktree / "state/release.json", rollback_state)
        write_json(worktree / "dist/reports/latest.json", report)
        write_bytes(worktree / "dist/reports/latest.md", render_latest_markdown(report))
        validate_schema(report, self.schemas_dir / "report.schema.json")
        validate_bundle(
            worktree,
            schemas_dir=self.schemas_dir,
            expected_release_id=str(rollback_state["active_release_id"]),
        )
        validate_release_tree(
            worktree,
            self.schemas_dir,
            owner=self.raw.owner,
            repository=self.raw.repository,
            expected_status="success",
        )
        rollback_sha = self.git.commit(
            worktree,
            (
                "revert: 回滚 TVBox 自动发布"
                f"（{artifact.workflow_run_id}/{artifact.workflow_run_attempt}）"
            ),
            ["dist", "state"],
        )
        candidate_ref = (
            f"candidate/run-{artifact.workflow_run_id}-attempt-{artifact.workflow_run_attempt}"
        )
        self.git.push_if_remote_equals(rollback_sha, candidate_ref, bad_head)
        rollback_expectation = self._raw_expectation(
            worktree,
            event_generation=int(rollback_state["generation"]),
            workflow_run_id=artifact.workflow_run_id,
            workflow_run_attempt=artifact.workflow_run_attempt,
        )
        self.raw.poll_revision(rollback_sha, expected=rollback_expectation)
        if self.git.remote_head() != bad_head:
            raise PublishError("generated changed before compensating rollback")
        self.git.push_if_remote_equals(rollback_sha, "generated", bad_head)
        self.raw.poll_revision(rollback_sha, expected=rollback_expectation)
        if bad_receipt:
            self.raw.poll_absent("generated", bad_receipt)
        self.raw.poll_bare(expected=rollback_expectation)
        if self.git.remote_head() != rollback_sha:
            raise PublishError("generated changed after compensating rollback verification")
        return rollback_sha

    @staticmethod
    def _assert_bootstrap_deletion_identity(
        artifact: PublishArtifact,
        state: dict[str, Any],
    ) -> None:
        if (
            artifact.expected_previous_head is not None
            or artifact.release_kind is not ReleaseKind.BOOTSTRAP
            or artifact.generation != 1
            or state.get("status") != "success"
            or state.get("release_kind") != ReleaseKind.BOOTSTRAP.value
            or state.get("generation") != 1
            or state.get("active_release_id") != "g00000001"
            or state.get("workflow_run_id") != artifact.workflow_run_id
            or state.get("workflow_run_attempt") != artifact.workflow_run_attempt
        ):
            raise PublishError("bootstrap ref deletion identity check failed")

    def publish(self, artifact_root: Path) -> str:
        artifact = validate_publish_artifact(artifact_root, self.schemas_dir)
        self._assert_event_environment(artifact)
        current_head = self.git.remote_head()
        if artifact.expected_previous_head is None:
            if artifact.release_kind is not ReleaseKind.BOOTSTRAP or current_head is not None:
                raise PublishError("bootstrap expected generated to be absent")
        elif current_head != validate_sha(artifact.expected_previous_head):
            raise PublishError("generated does not equal artifact expected previous head")
        candidate_ref = (
            f"candidate/run-{artifact.workflow_run_id}-attempt-{artifact.workflow_run_attempt}"
        )
        if self.git.remote_head(candidate_ref) is not None:
            raise PublishError("candidate ref already exists for this run attempt")

        promoted = False
        final_sha: str | None = None
        with self.git.worktree(current_head, orphan=current_head is None) as worktree:
            self._validate_transition(worktree, artifact, current_head)
            if current_head is not None:
                validate_release_tree(
                    worktree,
                    self.schemas_dir,
                    owner=self.raw.owner,
                    repository=self.raw.repository,
                    expected_status="success",
                )
                previous_state = load_json(worktree / "state/release.json")
                if not isinstance(previous_state, Mapping):
                    raise PublishError("publisher previous state is invalid")
                previous_receipt = self._required_absent_paths(previous_state)
                if previous_receipt:
                    # A delivery-unverified state is never allowed to advance or
                    # clear its receipt until the bare generated view proves 404.
                    self.raw.poll_absent("generated", previous_receipt)
            removal_evidence = self._validate_privileged_gate(worktree, artifact, current_head)
            self._validate_deletion_scope(worktree, artifact, removal_evidence)
            deleted_paths: list[str] = []
            for deletion in artifact.deletions:
                deleted_paths.extend(
                    self._deletion_receipt(
                        worktree,
                        deletion,
                    )
                )
            deletion_receipt = tuple(sorted(set(deleted_paths)))
            if len(deletion_receipt) > 10_000:
                raise PublishError("publication deletion receipt exceeds 10000 files")
            # Validate the complete receipt before mutating even the temporary tree.
            self._required_absent_paths(
                {"required_absent_paths": list(deletion_receipt)}
            )
            for deletion in artifact.deletions:
                self._remove_exact_release(worktree, deletion)
            materialize_bundle(worktree, self._payload_files(artifact))
            self._write_required_absent_paths(worktree, deletion_receipt)
            validate_bundle(
                worktree,
                schemas_dir=self.schemas_dir,
                expected_release_id=artifact.release_id,
            )
            raw_expectation = self._raw_expectation(
                worktree,
                event_generation=artifact.generation,
                workflow_run_id=artifact.workflow_run_id,
                workflow_run_attempt=artifact.workflow_run_attempt,
                sealed_root_manifest_sha256=artifact.root_manifest_sha256,
                sealed_release_manifest_sha256=artifact.release_manifest_sha256,
            )
            content_sha = self.git.commit(
                worktree,
                f"chore: 自动更新 TVBox 资源（{artifact.release_id}）",
                ["dist", "state"],
            )
            self.git.push_if_remote_equals(content_sha, candidate_ref, None)
            self.raw.poll_revision(
                content_sha,
                expected_status="pending",
                expected=raw_expectation,
            )

            self._confirm(worktree, artifact, content_sha)
            final_sha = self.git.commit(
                worktree,
                f"chore: 确认 TVBox 发布（{artifact.release_id}）",
                ["state/release.json", "dist/reports/latest.json", "dist/reports/latest.md"],
            )
            self._verify_confirmation_diff(content_sha, final_sha, worktree)
            self.git.push_if_remote_equals(final_sha, candidate_ref, content_sha)
            self.raw.poll_revision(final_sha, expected=raw_expectation)
            self.git.push_if_remote_equals(final_sha, "generated", current_head)
            promoted = True
            try:
                if self.git.remote_head() != final_sha:
                    raise PublishError("generated changed immediately after promotion")
                self.raw.poll_revision(final_sha, expected=raw_expectation)
                if deletion_receipt:
                    self.raw.poll_absent("generated", deletion_receipt)
                self.raw.poll_bare(expected=raw_expectation)
                if self.git.remote_head() != final_sha:
                    raise PublishError("generated changed after bare Raw verification")
            except PublishError as delivery_error:
                if artifact.release_kind is ReleaseKind.BOOTSTRAP:
                    state = load_json(worktree / "state/release.json")
                    if not isinstance(state, dict):
                        raise PublishError(
                            "bootstrap state is invalid during ref deletion"
                        ) from delivery_error
                    self._assert_bootstrap_deletion_identity(artifact, state)
                    if self.git.remote_head() == final_sha:
                        self.git.delete_bootstrap_with_lease(final_sha)
                    if self.git.remote_head() is not None:
                        raise PublishError(
                            "failed bootstrap ref could not be precisely removed"
                        ) from delivery_error
                    raise
                if artifact.release_kind is ReleaseKind.SAFETY:
                    raise PublishError(
                        "delivery_unverified: safety SHA remains promoted; rollback is forbidden"
                    ) from delivery_error
                if artifact.release_kind is ReleaseKind.REGULAR:
                    self._compensating_rollback(worktree, artifact, final_sha)
                    raise
                raise
        if final_sha is None or not promoted:
            raise PublishError("publisher ended without promotion")
        self.git.delete_candidate(candidate_ref, final_sha)
        return final_sha
