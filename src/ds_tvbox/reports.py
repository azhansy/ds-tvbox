"""Machine-readable and human-readable run reports with stable redaction."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from typing import Any

from ds_tvbox.models import RightsStatus, RunContext
from ds_tvbox.serialization import canonical_json_bytes

_COUNT_KEYS = (
    "previous_vod_sites",
    "current_vod_sites",
    "previous_live_channels",
    "current_live_channels",
    *(f"previous_{status.value}" for status in RightsStatus),
    *(f"current_{status.value}" for status in RightsStatus),
)
_GATE_INPUT_KEYS = (
    "previous_vod_items",
    "current_publishable_vod_items",
    "previous_live_urls",
    "current_healthy_live_urls",
    "current_vod_sites",
    "current_live_channels",
    "failed_network_groups",
)
_GATE_THRESHOLD_KEYS = (
    "minimum_vod_sites",
    "minimum_live_channels",
    "minimum_previous_items",
    "max_new_failure_ratio",
    "failed_groups_to_abort",
)

_CHANGE_CATEGORIES = frozenset(
    {
        "new",
        "recovered",
        "degraded",
        "withheld",
        "dead",
        "rejected",
        "removed",
        "unchanged",
    }
)
_TECHNICAL_RANK = {
    "dead": 0,
    "unsupported_environment": 1,
    "unknown": 2,
    "suspect": 3,
    "partial": 4,
    "healthy": 5,
}
_PUBLICATION_RANK = {"rejected": 0, "withheld": 1, "experimental": 2, "stable": 3}
_RIGHTS_RANK = {
    "takedown": 0,
    "restricted": 1,
    "unknown": 2,
    "public_unverified": 3,
    "open_license": 4,
    "verified": 5,
}


def _status_snapshot(source: Mapping[str, Any]) -> dict[str, str]:
    return {
        "technical_status": str(source.get("technical_status", "unknown")),
        "publication_status": str(source.get("publication_status", "withheld")),
        "rights_status": str(source.get("rights_status", "unknown")),
    }


def build_change_summary(
    previous: Mapping[str, Any] | None,
    current: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Build one deterministic before/after source transition summary.

    The JSON report is the machine-readable source of truth for the Markdown
    report, so disappearing sources are represented explicitly instead of being
    silently omitted from the current collection.
    """

    before = _status_snapshot(previous) if previous is not None else None
    after = _status_snapshot(current) if current is not None else None
    if before is None:
        category = "new"
    elif after is None:
        category = "removed"
    elif (
        after["publication_status"] == "rejected"
        or after["rights_status"] in {"restricted", "takedown"}
    ):
        category = "rejected"
    elif after["technical_status"] == "dead":
        category = "dead"
    elif after["publication_status"] == "withheld":
        category = "withheld"
    elif (
        _TECHNICAL_RANK[after["technical_status"]]
        > _TECHNICAL_RANK[before["technical_status"]]
        or _PUBLICATION_RANK[after["publication_status"]]
        > _PUBLICATION_RANK[before["publication_status"]]
    ):
        category = "recovered"
    elif (
        _TECHNICAL_RANK[after["technical_status"]]
        < _TECHNICAL_RANK[before["technical_status"]]
        or _PUBLICATION_RANK[after["publication_status"]]
        < _PUBLICATION_RANK[before["publication_status"]]
        or _RIGHTS_RANK[after["rights_status"]] < _RIGHTS_RANK[before["rights_status"]]
    ):
        category = "degraded"
    else:
        category = "unchanged"
    return {"category": category, "previous": before, "current": after}


def _normalized_change_summary(item: Mapping[str, Any]) -> dict[str, Any]:
    raw = item.get("change_summary")
    if not isinstance(raw, Mapping):
        return build_change_summary(None, item)
    category = str(raw.get("category", ""))
    previous = raw.get("previous")
    current = raw.get("current")
    if category not in _CHANGE_CATEGORIES:
        raise ValueError(f"invalid source change category: {category}")
    if previous is not None and not isinstance(previous, Mapping):
        raise ValueError("source change previous snapshot must be an object or null")
    if current is not None and not isinstance(current, Mapping):
        raise ValueError("source change current snapshot must be an object or null")
    return {
        "category": category,
        "previous": _status_snapshot(previous) if isinstance(previous, Mapping) else None,
        "current": _status_snapshot(current) if isinstance(current, Mapping) else None,
    }


def _sorted_sources(sources: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "source_id": str(item["source_id"]),
            "technical_status": str(item.get("technical_status", "unknown")),
            "publication_status": str(item.get("publication_status", "withheld")),
            "rights_status": str(item.get("rights_status", "unknown")),
            "failure_reason": item.get("failure_reason"),
            "secondary_reasons": list(item.get("secondary_reasons", [])),
            "upstream_revision": item.get("upstream_revision"),
            "change_summary": _normalized_change_summary(item),
        }
        for item in sorted(sources, key=lambda item: str(item["source_id"]))
    ]


def _normalized_counts(counts: Mapping[str, int]) -> dict[str, int]:
    return {key: int(counts.get(key, 0)) for key in _COUNT_KEYS}


def _normalized_gate(context: RunContext, gate: Mapping[str, Any]) -> dict[str, Any]:
    raw_inputs = gate.get("inputs")
    inputs: Mapping[str, Any] = raw_inputs if isinstance(raw_inputs, Mapping) else {}
    raw_thresholds = gate.get("thresholds")
    thresholds: Mapping[str, Any] = (
        raw_thresholds if isinstance(raw_thresholds, Mapping) else {}
    )
    probes = gate.get("network_probes")
    return {
        "publish": bool(gate.get("publish", False)),
        "inconclusive": bool(gate.get("inconclusive", False)),
        "release_kind": str(gate.get("release_kind", context.release_kind.value)),
        "reasons": [str(item) for item in gate.get("reasons", [])],
        "mandatory_removal_ids": [
            str(item) for item in gate.get("mandatory_removal_ids", [])
        ],
        "historical_deletions": [
            str(item) for item in gate.get("historical_deletions", [])
        ],
        "inputs": {key: int(inputs.get(key, 0)) for key in _GATE_INPUT_KEYS},
        "thresholds": {
            key: float(thresholds.get(key, 0.0))
            if key == "max_new_failure_ratio"
            else int(thresholds.get(key, 0))
            for key in _GATE_THRESHOLD_KEYS
        },
        "network_probes": [dict(item) for item in probes]
        if isinstance(probes, list)
        else [],
    }


def build_latest_report(
    context: RunContext,
    *,
    status: str,
    started_at: str,
    finished_at: str,
    due: bool,
    forced: bool,
    recovery_due: bool,
    sources: Iterable[Mapping[str, Any]],
    counts: Mapping[str, int],
    gate: Mapping[str, Any],
    previous_release_head_sha: str | None,
    content_commit_sha: str | None = None,
    candidate_ref: str | None = None,
    content_identity: Mapping[str, Any] | None = None,
    entity_failure_reasons: Iterable[str] = (),
) -> dict[str, Any]:
    source_list = _sorted_sources(sources)
    failures = Counter(
        str(item["failure_reason"])
        for item in source_list
        if item.get("failure_reason") is not None
    )
    failures.update(str(reason) for reason in entity_failure_reasons)
    return {
        "schema_version": "1.0.0",
        "status": status,
        "release_kind": context.release_kind.value,
        "generation": context.generation,
        "active_release_id": context.release_id,
        "workflow_run_id": context.workflow_run_id,
        "workflow_run_attempt": context.workflow_run_attempt,
        "started_at": started_at,
        "finished_at": finished_at,
        "due": due,
        "forced": forced,
        "recovery_due": recovery_due,
        "previous_release_head_sha": previous_release_head_sha,
        "content_commit_sha": content_commit_sha,
        "candidate_ref": candidate_ref,
        "content_identity": dict(content_identity) if content_identity is not None else None,
        "counts": _normalized_counts(counts),
        "gate": _normalized_gate(context, gate),
        "failures": dict(sorted(failures.items())),
        "sources": source_list,
    }


def render_latest_markdown(report: Mapping[str, Any]) -> bytes:
    counts = report.get("counts", {})
    failures = report.get("failures", {})
    sources = report.get("sources", [])
    content_identity = report.get("content_identity")
    if isinstance(content_identity, Mapping):
        content_event = (
            f"{content_identity.get('workflow_run_id', '')}/"
            f"{content_identity.get('workflow_run_attempt', '')}"
        )
    else:
        content_event = ""
    gate = report.get("gate", {})
    lines = [
        "# DS TVBox 刷新报告",
        "",
        f"- 状态：`{report['status']}`",
        f"- 发布类型：`{report['release_kind']}`",
        f"- generation：`{report['generation']}`",
        f"- active release：`{report['active_release_id']}`",
        (
            "- Workflow："
            f"`{report['workflow_run_id']}/{report['workflow_run_attempt']}`"
        ),
        f"- 候选 ref：`{report.get('candidate_ref') or ''}`",
        f"- 内容来源事件：`{content_event}`",
        f"- 上一 generated HEAD：`{report.get('previous_release_head_sha') or ''}`",
        f"- 内容提交：`{report.get('content_commit_sha') or ''}`",
        (
            f"- due / force / recovery：`{report['due']}` / `{report['forced']}` / "
            f"`{report['recovery_due']}`"
        ),
        f"- 开始：`{report['started_at']}`",
        f"- 结束：`{report['finished_at']}`",
        "",
        "## 数量",
        "",
    ]
    if isinstance(counts, Mapping):
        lines.extend(f"- {key}: `{value}`" for key, value in sorted(counts.items()))
    lines.extend(["", "## 失败原因", ""])
    if isinstance(failures, Mapping) and failures:
        lines.extend(f"- `{key}`: {value}" for key, value in sorted(failures.items()))
    else:
        lines.append("- 无")
    lines.extend(["", "## 发布闸门", ""])
    if isinstance(gate, Mapping):
        lines.extend(
            [
                f"- publish：`{gate.get('publish')}`",
                f"- inconclusive：`{gate.get('inconclusive')}`",
                f"- reasons：`{', '.join(str(item) for item in gate.get('reasons', []))}`",
            ]
        )
    lines.extend(
        [
            "",
            "## 来源",
            "",
            "| 来源 | 变化 | 技术状态 | 发布状态 | 权利状态 | 失败原因 |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
    )
    if isinstance(sources, list):
        for source in sources:
            if not isinstance(source, Mapping):
                continue
            change = source.get("change_summary")
            category = str(change.get("category", "")) if isinstance(change, Mapping) else ""
            lines.append(
                f"| `{source.get('source_id', '')}` | `{category}` "
                f"| `{source.get('technical_status', '')}` "
                f"| `{source.get('publication_status', '')}` | "
                f"`{source.get('rights_status', '')}` | "
                f"`{source.get('failure_reason') or ''}` |"
            )
    lines.extend(
        [
            "",
            "> `public_unverified` 仅表示公开可访问且通过技术验效，不代表内容已获授权。",
            "",
        ]
    )
    return "\n".join(lines).encode("utf-8")


def build_candidates_report(
    context: RunContext,
    catalogs: Iterable[Mapping[str, Any]],
    candidates: Iterable[Mapping[str, Any]],
) -> bytes:
    payload = {
        "schema_version": "1.0.0",
        "workflow_run_id": context.workflow_run_id,
        "workflow_run_attempt": context.workflow_run_attempt,
        "catalogs": sorted(
            (dict(item) for item in catalogs), key=lambda item: str(item["source_id"])
        ),
        "candidates": sorted(
            (dict(item) for item in candidates), key=lambda item: str(item["candidate_id"])
        ),
    }
    return canonical_json_bytes(payload)
