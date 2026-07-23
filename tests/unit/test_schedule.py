from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from ds_tvbox.errors import ContractError
from ds_tvbox.models import ReleaseKind, ReleaseState
from ds_tvbox.schedule import (
    evaluate_due,
    parse_utc_timestamp,
    successful_last_success_at,
    validate_success_state,
)

NOW = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)


def _state(
    *,
    release_kind: ReleaseKind = ReleaseKind.REGULAR,
    elapsed: timedelta = timedelta(hours=239, minutes=59),
) -> ReleaseState:
    last_success = NOW - elapsed
    return ReleaseState(
        schema_version="1.0.0",
        status="success",
        release_kind=release_kind,
        generation=2,
        active_release_id="g00000002",
        last_publish_at="2026-07-22T11:00:00Z",
        last_success_at=last_success.isoformat().replace("+00:00", "Z"),
        content_commit_sha="a" * 40,
        previous_release_head_sha="b" * 40,
        workflow_run_id="100",
        workflow_run_attempt=1,
    )


def test_240_hour_boundary_uses_continuous_elapsed_time() -> None:
    before = evaluate_due(now=NOW, state=_state())
    boundary = evaluate_due(now=NOW, state=_state(elapsed=timedelta(hours=240)))

    assert not before.should_refresh
    assert not before.due
    assert before.reason == "not_due"
    assert boundary.should_refresh
    assert boundary.due
    assert boundary.reason == "elapsed_240h"


@pytest.mark.parametrize("kind", [ReleaseKind.SAFETY, ReleaseKind.ROLLBACK])
def test_safety_and_rollback_are_due_for_recovery_without_waiting(kind: ReleaseKind) -> None:
    state = _state(release_kind=kind, elapsed=timedelta(hours=1))
    if kind is ReleaseKind.ROLLBACK:
        state = replace(state, active_release_id="g00000001")

    decision = evaluate_due(now=NOW, state=state)

    assert decision.should_refresh
    assert decision.recovery_due
    assert not decision.due
    assert decision.reason == "recovery_due"


def test_force_and_bootstrap_rules_are_explicit() -> None:
    forced = evaluate_due(now=NOW, state=_state(), force=True)
    missing = evaluate_due(now=NOW, state=None, generated_ref_exists=False)
    bootstrap = evaluate_due(
        now=NOW,
        state=None,
        generated_ref_exists=False,
        bootstrap=True,
    )

    assert forced.should_refresh and forced.forced and forced.reason == "forced"
    assert missing.bootstrap_required and not missing.should_refresh
    assert bootstrap.should_refresh and bootstrap.forced and not bootstrap.bootstrap_required
    with pytest.raises(ContractError, match="bootstrap_ref_exists"):
        evaluate_due(now=NOW, state=_state(), bootstrap=True)


def test_existing_ref_with_missing_or_pending_state_fails_closed() -> None:
    with pytest.raises(ContractError, match="invalid_release_state"):
        evaluate_due(now=NOW, state=None, generated_ref_exists=True)
    with pytest.raises(ContractError, match="invalid_release_state"):
        evaluate_due(now=NOW, state=replace(_state(), status="pending"))
    mapped = {
        **_state().__dict__,
        "release_kind": "regular",
        "workflow_run_id": None,
    }
    with pytest.raises(ContractError, match="field types"):
        evaluate_due(now=NOW, state=mapped)


def test_future_success_timestamp_is_rejected_instead_of_skipping_forever() -> None:
    future = replace(_state(), last_success_at="2026-07-22T12:00:01Z")
    with pytest.raises(ContractError, match="future"):
        evaluate_due(now=NOW, state=future)


def test_only_bootstrap_and_regular_advance_success_baseline() -> None:
    previous = "2026-07-12T12:00:00Z"
    published = "2026-07-22T12:00:00Z"
    assert (
        successful_last_success_at(
            release_kind=ReleaseKind.REGULAR,
            published_at=published,
            previous_last_success_at=previous,
        )
        == published
    )
    assert (
        successful_last_success_at(
            release_kind=ReleaseKind.SAFETY,
            published_at=published,
            previous_last_success_at=previous,
        )
        == previous
    )
    assert (
        successful_last_success_at(
            release_kind=ReleaseKind.ROLLBACK,
            published_at=published,
            previous_last_success_at=previous,
        )
        == previous
    )


@pytest.mark.parametrize("value", ["", "not-a-time", "2026-07-22T12:00:00"])
def test_timestamp_parser_rejects_empty_invalid_and_naive_values(value: str) -> None:
    with pytest.raises(ContractError):
        parse_utc_timestamp(value, label="fixture")


def test_mapping_state_requires_all_fields_and_exact_types() -> None:
    valid = dict(_state().__dict__)
    valid["release_kind"] = "regular"
    missing = dict(valid)
    missing.pop("generation")
    with pytest.raises(ContractError, match="missing fields"):
        validate_success_state(missing)

    for field, invalid in (
        ("schema_version", 1),
        ("last_publish_at", 1),
        ("generation", True),
        ("workflow_run_attempt", False),
        ("release_kind", "unknown"),
    ):
        changed = dict(valid)
        changed[field] = invalid
        with pytest.raises(ContractError, match="invalid field types"):
            validate_success_state(changed)


@pytest.mark.parametrize(
    ("state", "error"),
    [
        (replace(_state(), schema_version="2.0.0"), "supported success state"),
        (replace(_state(), status="pending"), "supported success state"),
        (replace(_state(), generation=0), "generation must be positive"),
        (replace(_state(), active_release_id="g00000001"), "does not match generation"),
        (
            replace(
                _state(release_kind=ReleaseKind.ROLLBACK),
                active_release_id="release-1",
            ),
            "malformed",
        ),
        (replace(_state(), workflow_run_attempt=0), "invalid workflow identity"),
        (replace(_state(), last_publish_at=None), "missing last_publish_at"),
        (replace(_state(), content_commit_sha=None), "missing content_commit_sha"),
        (replace(_state(), last_success_at=None), "missing last_success_at"),
    ],
)
def test_success_state_rejects_incomplete_or_inconsistent_state(
    state: ReleaseState,
    error: str,
) -> None:
    with pytest.raises(ContractError, match=error):
        validate_success_state(state)


def test_recovery_state_may_have_no_success_baseline() -> None:
    state = replace(
        _state(release_kind=ReleaseKind.SAFETY),
        last_success_at=None,
    )
    assert validate_success_state(state) == state
    decision = evaluate_due(now=NOW, state=state)
    assert decision.should_refresh
    assert decision.recovery_due
    assert decision.next_due_at is None


def test_ref_state_and_publish_time_consistency_fail_closed() -> None:
    with pytest.raises(ContractError, match="state exists without generated ref"):
        evaluate_due(now=NOW, state=_state(), generated_ref_exists=False)
    with pytest.raises(ContractError, match="last_publish_at is in the future"):
        evaluate_due(
            now=NOW,
            state=replace(_state(), last_publish_at="2026-07-22T12:00:01Z"),
        )
    with pytest.raises(ContractError, match="success time is after publish time"):
        evaluate_due(
            now=NOW,
            state=replace(
                _state(),
                last_publish_at="2026-07-01T00:00:00Z",
                last_success_at="2026-07-02T00:00:00Z",
            ),
        )


def test_success_timestamp_helper_validates_both_inputs() -> None:
    with pytest.raises(ContractError, match="invalid published_at"):
        successful_last_success_at(
            release_kind=ReleaseKind.REGULAR,
            published_at="bad",
            previous_last_success_at=None,
        )
    with pytest.raises(ContractError, match="invalid previous_last_success_at"):
        successful_last_success_at(
            release_kind=ReleaseKind.SAFETY,
            published_at="2026-07-22T12:00:00Z",
            previous_last_success_at="bad",
        )
