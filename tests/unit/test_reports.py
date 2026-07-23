from pathlib import Path

import pytest

from ds_tvbox.errors import ContractError
from ds_tvbox.models import ReleaseKind, RunContext
from ds_tvbox.reports import (
    build_candidates_report,
    build_change_summary,
    build_latest_report,
    render_latest_markdown,
)
from ds_tvbox.validation import validate_schema

SCHEMAS = Path(__file__).resolve().parents[2] / "schemas"


def _context() -> RunContext:
    return RunContext(
        owner="azhansy",
        repository="ds-tvbox",
        generated_ref="generated",
        workflow_run_id="123",
        workflow_run_attempt=2,
        generated_at="2026-07-22T00:00:00Z",
        generation=1,
        release_kind=ReleaseKind.BOOTSTRAP,
        previous_head=None,
        previous_last_success_at=None,
    )


def test_latest_report_is_sorted_and_risk_is_visible() -> None:
    report = build_latest_report(
        _context(),
        status="pending",
        started_at="2026-07-22T00:00:00Z",
        finished_at="2026-07-22T00:01:00Z",
        due=True,
        forced=True,
        recovery_due=False,
        sources=[
            {
                "source_id": "z-source",
                "technical_status": "dead",
                "publication_status": "withheld",
                "rights_status": "unknown",
                "failure_reason": "http_404",
            },
            {
                "source_id": "a-source",
                "technical_status": "healthy",
                "publication_status": "stable",
                "rights_status": "public_unverified",
                "failure_reason": None,
            },
        ],
        counts={"vod_sites": 1},
        gate={"publish": True},
        previous_release_head_sha=None,
        candidate_ref=_context().candidate_ref,
        content_identity={"workflow_run_id": "123", "workflow_run_attempt": 2},
        entity_failure_reasons=("credential_query_rejected",),
    )
    assert [item["source_id"] for item in report["sources"]] == ["a-source", "z-source"]
    assert report["failures"] == {
        "credential_query_rejected": 1,
        "http_404": 1,
    }
    validate_schema(report, SCHEMAS / "report.schema.json")
    markdown = render_latest_markdown(report).decode()
    assert "public_unverified" in markdown
    assert "123/2" in markdown
    assert "candidate/run-123-attempt-2" in markdown
    assert "发布闸门" in markdown
    assert "http_404" in markdown
    assert "`new`" in markdown


@pytest.mark.parametrize(
    ("previous", "current", "category"),
    [
        (None, {"technical_status": "healthy", "publication_status": "stable"}, "new"),
        ({"technical_status": "healthy", "publication_status": "stable"}, None, "removed"),
        (
            {"technical_status": "dead", "publication_status": "withheld"},
            {"technical_status": "healthy", "publication_status": "stable"},
            "recovered",
        ),
        (
            {"technical_status": "healthy", "publication_status": "stable"},
            {"technical_status": "partial", "publication_status": "experimental"},
            "degraded",
        ),
        (
            {"technical_status": "healthy", "publication_status": "stable"},
            {"technical_status": "healthy", "publication_status": "withheld"},
            "withheld",
        ),
        (
            {"technical_status": "healthy", "publication_status": "stable"},
            {"technical_status": "dead", "publication_status": "withheld"},
            "dead",
        ),
        (
            {"technical_status": "healthy", "publication_status": "stable"},
            {
                "technical_status": "healthy",
                "publication_status": "rejected",
                "rights_status": "takedown",
            },
            "rejected",
        ),
    ],
)
def test_change_summary_covers_stable_source_transitions(
    previous: dict[str, str] | None,
    current: dict[str, str] | None,
    category: str,
) -> None:
    assert build_change_summary(previous, current)["category"] == category


def test_report_schema_accepts_real_live_url_mandatory_identifier() -> None:
    report = build_latest_report(
        _context(),
        status="pending",
        started_at="2026-07-22T00:00:00Z",
        finished_at="2026-07-22T00:01:00Z",
        due=True,
        forced=True,
        recovery_due=False,
        sources=[],
        counts={},
        gate={
            "publish": True,
            "release_kind": "safety",
            "mandatory_removal_ids": ["live-url:iptv-org-cn-cctv:" + "a" * 16],
        },
        previous_release_head_sha="b" * 40,
    )
    report["release_kind"] = "safety"
    report["gate"]["release_kind"] = "safety"
    validate_schema(report, SCHEMAS / "report.schema.json")


def test_report_schema_rejects_missing_fixed_gate_and_count_fields() -> None:
    report = build_latest_report(
        _context(),
        status="pending",
        started_at="2026-07-22T00:00:00Z",
        finished_at="2026-07-22T00:01:00Z",
        due=True,
        forced=False,
        recovery_due=False,
        sources=[],
        counts={},
        gate={"publish": True},
        previous_release_head_sha=None,
        candidate_ref=_context().candidate_ref,
        content_identity={"workflow_run_id": "123", "workflow_run_attempt": 2},
    )
    del report["counts"]["current_vod_sites"]
    del report["gate"]["inputs"]

    with pytest.raises(ContractError, match="schema validation failed"):
        validate_schema(report, SCHEMAS / "report.schema.json")


def test_candidate_report_is_deterministic() -> None:
    data = build_candidates_report(
        _context(),
        catalogs=[{"source_id": "catalog", "resolved_revision": "a" * 40}],
        candidates=[
            {"candidate_id": "candidate:catalog:bbbb", "rights_status": "unknown"},
            {"candidate_id": "candidate:catalog:aaaa", "rights_status": "unknown"},
        ],
    )
    assert data.index(b"candidate:catalog:aaaa") < data.index(b"candidate:catalog:bbbb")
