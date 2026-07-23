"""Pure 240-hour due-check and recovery scheduling rules."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from ds_tvbox.errors import ContractError
from ds_tvbox.models import ReleaseKind, ReleaseState

DUE_INTERVAL = timedelta(hours=240)
RECOVERY_KINDS = frozenset({ReleaseKind.SAFETY, ReleaseKind.ROLLBACK})


@dataclass(frozen=True)
class DueDecision:
    """A complete, serializable result for the Action due-check job."""

    should_refresh: bool
    due: bool
    forced: bool
    recovery_due: bool
    bootstrap_required: bool
    reason: str
    last_success_at: datetime | None
    next_due_at: datetime | None


def _utc_datetime(value: datetime, *, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ContractError(f"{label} must be timezone-aware")
    return value.astimezone(UTC)


def parse_utc_timestamp(value: str, *, label: str = "timestamp") -> datetime:
    """Parse an RFC 3339 timestamp and normalize it to UTC."""

    if not isinstance(value, str) or not value:
        raise ContractError(f"{label} must be a non-empty timestamp")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise ContractError(f"invalid {label}: {value!r}") from error
    return _utc_datetime(parsed, label=label)


def _state_from_mapping(value: Mapping[str, Any]) -> ReleaseState:
    required = {
        "schema_version",
        "status",
        "release_kind",
        "generation",
        "active_release_id",
        "last_publish_at",
        "last_success_at",
        "content_commit_sha",
        "previous_release_head_sha",
        "workflow_run_id",
        "workflow_run_attempt",
    }
    missing = required.difference(value)
    if missing:
        raise ContractError(f"release state is missing fields: {sorted(missing)}")
    try:
        release_kind = ReleaseKind(value["release_kind"])
        generation = value["generation"]
        workflow_run_attempt = value["workflow_run_attempt"]
        required_strings = (
            "schema_version",
            "status",
            "active_release_id",
            "workflow_run_id",
        )
        nullable_strings = (
            "last_publish_at",
            "last_success_at",
            "content_commit_sha",
            "previous_release_head_sha",
        )
        if any(not isinstance(value[field], str) for field in required_strings):
            raise ValueError("string field")
        if any(
            value[field] is not None and not isinstance(value[field], str)
            for field in nullable_strings
        ):
            raise ValueError("nullable string field")
        if not isinstance(generation, int) or isinstance(generation, bool):
            raise ValueError("generation")
        if not isinstance(workflow_run_attempt, int) or isinstance(workflow_run_attempt, bool):
            raise ValueError("workflow_run_attempt")
        return ReleaseState(
            schema_version=value["schema_version"],
            status=value["status"],
            release_kind=release_kind,
            generation=generation,
            active_release_id=value["active_release_id"],
            last_publish_at=value["last_publish_at"],
            last_success_at=value["last_success_at"],
            content_commit_sha=value["content_commit_sha"],
            previous_release_head_sha=value["previous_release_head_sha"],
            workflow_run_id=value["workflow_run_id"],
            workflow_run_attempt=workflow_run_attempt,
        )
    except (TypeError, ValueError) as error:
        raise ContractError("release state has invalid field types") from error


def validate_success_state(state: ReleaseState | Mapping[str, Any]) -> ReleaseState:
    """Validate the minimum stable-branch state required by due-check."""

    parsed = _state_from_mapping(state) if isinstance(state, Mapping) else state
    if parsed.schema_version != "1.0.0" or parsed.status != "success":
        raise ContractError(
            "invalid_release_state: generated state is not a supported success state"
        )
    if parsed.generation < 1:
        raise ContractError("invalid_release_state: generation must be positive")
    expected_release_id = f"g{parsed.generation:08d}"
    if parsed.release_kind is not ReleaseKind.ROLLBACK:
        if parsed.active_release_id != expected_release_id:
            raise ContractError("invalid_release_state: active release does not match generation")
    elif not (
        parsed.active_release_id.startswith("g")
        and len(parsed.active_release_id) == 9
        and parsed.active_release_id[1:].isdigit()
    ):
        raise ContractError("invalid_release_state: rollback active release id is malformed")
    if not parsed.workflow_run_id or parsed.workflow_run_attempt < 1:
        raise ContractError("invalid_release_state: invalid workflow identity")
    if parsed.last_publish_at is None:
        raise ContractError("invalid_release_state: missing last_publish_at")
    parse_utc_timestamp(parsed.last_publish_at, label="last_publish_at")
    if not parsed.content_commit_sha:
        raise ContractError("invalid_release_state: missing content_commit_sha")
    if parsed.last_success_at is None and parsed.release_kind not in RECOVERY_KINDS:
        raise ContractError("invalid_release_state: missing last_success_at")
    if parsed.last_success_at is not None:
        parse_utc_timestamp(parsed.last_success_at, label="last_success_at")
    return parsed


def evaluate_due(
    *,
    now: datetime,
    state: ReleaseState | Mapping[str, Any] | None,
    generated_ref_exists: bool | None = None,
    force: bool = False,
    bootstrap: bool = False,
) -> DueDecision:
    """Evaluate the daily Action wake-up without reading a floating Raw URL.

    ``generated_ref_exists`` should come from a Git ref lookup. It is optional for
    local callers and then inferred from whether ``state`` was supplied.
    """

    current = _utc_datetime(now, label="now")
    ref_exists = state is not None if generated_ref_exists is None else generated_ref_exists

    if bootstrap:
        if ref_exists or state is not None:
            raise ContractError("bootstrap_ref_exists: bootstrap cannot overwrite generated")
        return DueDecision(
            should_refresh=True,
            due=False,
            forced=True,
            recovery_due=False,
            bootstrap_required=False,
            reason="bootstrap",
            last_success_at=None,
            next_due_at=None,
        )
    if not ref_exists:
        if state is not None:
            raise ContractError("invalid_release_state: state exists without generated ref")
        return DueDecision(
            should_refresh=False,
            due=False,
            forced=force,
            recovery_due=False,
            bootstrap_required=True,
            reason="bootstrap_required",
            last_success_at=None,
            next_due_at=None,
        )
    if state is None:
        raise ContractError("invalid_release_state: generated ref has no release state")

    parsed = validate_success_state(state)
    recovery_due = parsed.release_kind in RECOVERY_KINDS
    last_success = (
        parse_utc_timestamp(parsed.last_success_at, label="last_success_at")
        if parsed.last_success_at is not None
        else None
    )
    if last_success is not None and last_success > current:
        raise ContractError("invalid_release_state: last_success_at is in the future")
    last_publish = parse_utc_timestamp(parsed.last_publish_at or "", label="last_publish_at")
    if last_publish > current:
        raise ContractError("invalid_release_state: last_publish_at is in the future")
    if last_success is not None and last_success > last_publish:
        raise ContractError("invalid_release_state: success time is after publish time")
    next_due = last_success + DUE_INTERVAL if last_success is not None else None
    elapsed_due = next_due is not None and current >= next_due

    if force:
        reason = "forced"
    elif recovery_due:
        reason = "recovery_due"
    elif elapsed_due:
        reason = "elapsed_240h"
    else:
        reason = "not_due"
    return DueDecision(
        should_refresh=force or recovery_due or elapsed_due,
        due=elapsed_due,
        forced=force,
        recovery_due=recovery_due,
        bootstrap_required=False,
        reason=reason,
        last_success_at=last_success,
        next_due_at=next_due,
    )


def successful_last_success_at(
    *,
    release_kind: ReleaseKind,
    published_at: str,
    previous_last_success_at: str | None,
) -> str | None:
    """Apply the success-time rule without giving failed/safety runs a new baseline."""

    parse_utc_timestamp(published_at, label="published_at")
    if previous_last_success_at is not None:
        parse_utc_timestamp(previous_last_success_at, label="previous_last_success_at")
    if release_kind in (ReleaseKind.BOOTSTRAP, ReleaseKind.REGULAR):
        return published_at
    return previous_last_success_at
