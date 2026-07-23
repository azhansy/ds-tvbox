from __future__ import annotations

import pytest

from ds_tvbox.models import (
    FailureReason,
    PublicationStatus,
    ReleaseKind,
    RightsStatus,
    TechnicalStatus,
)
from ds_tvbox.policy import (
    evaluate_gates,
    prioritize_failure_reasons,
    publication_status_for,
    safety_retained_ids,
    technical_status_for_failure,
    warning_name,
)


def test_failure_priority_is_stable_and_deduplicated() -> None:
    primary, secondary = prioritize_failure_reasons(
        [
            FailureReason.FETCH_TIMEOUT,
            FailureReason.HTTP_404,
            FailureReason.TAKEDOWN,
            FailureReason.HTTP_404,
            None,
        ]
    )
    assert primary is FailureReason.TAKEDOWN
    assert secondary == (FailureReason.HTTP_404, FailureReason.FETCH_TIMEOUT)


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        (FailureReason.HTTP_404, TechnicalStatus.DEAD),
        (FailureReason.INVALID_JSON, TechnicalStatus.DEAD),
        (FailureReason.FETCH_TIMEOUT, TechnicalStatus.SUSPECT),
        (FailureReason.UNSUPPORTED_ENVIRONMENT, TechnicalStatus.UNSUPPORTED_ENVIRONMENT),
        (FailureReason.CLIENT_EXTENSION_UNSUPPORTED, TechnicalStatus.PARTIAL),
        (FailureReason.MEDIA_PROBE_FAILED, TechnicalStatus.PARTIAL),
        (FailureReason.MISSING_UPSTREAM, TechnicalStatus.DEAD),
    ],
)
def test_failure_to_technical_status(
    reason: FailureReason, expected: TechnicalStatus
) -> None:
    assert technical_status_for_failure(None, reason) is expected


def test_rights_failure_preserves_last_technical_fact() -> None:
    assert (
        technical_status_for_failure(
            TechnicalStatus.HEALTHY, FailureReason.RIGHTS_RESTRICTED
        )
        is TechnicalStatus.HEALTHY
    )


def test_blocked_child_follows_source_failure_class() -> None:
    assert (
        technical_status_for_failure(
            None,
            FailureReason.BLOCKED_BY_SOURCE,
            source_status=TechnicalStatus.DEAD,
        )
        is TechnicalStatus.DEAD
    )
    assert (
        technical_status_for_failure(
            None,
            FailureReason.BLOCKED_BY_SOURCE,
            source_status=TechnicalStatus.SUSPECT,
        )
        is TechnicalStatus.SUSPECT
    )


@pytest.mark.parametrize(
    ("rights", "technical", "kind", "site_type", "media", "expected"),
    [
        (
            RightsStatus.VERIFIED,
            TechnicalStatus.HEALTHY,
            "vod",
            1,
            True,
            PublicationStatus.STABLE,
        ),
        (
            RightsStatus.PUBLIC_UNVERIFIED,
            TechnicalStatus.HEALTHY,
            "vod",
            0,
            True,
            PublicationStatus.STABLE,
        ),
        (
            RightsStatus.OPEN_LICENSE,
            TechnicalStatus.PARTIAL,
            "vod",
            1,
            True,
            PublicationStatus.EXPERIMENTAL,
        ),
        (
            RightsStatus.VERIFIED,
            TechnicalStatus.PARTIAL,
            "vod",
            4,
            True,
            PublicationStatus.EXPERIMENTAL,
        ),
        (
            RightsStatus.UNKNOWN,
            TechnicalStatus.HEALTHY,
            "vod",
            1,
            True,
            PublicationStatus.WITHHELD,
        ),
        (
            RightsStatus.PUBLIC_UNVERIFIED,
            TechnicalStatus.PARTIAL,
            "live",
            None,
            True,
            PublicationStatus.WITHHELD,
        ),
        (
            RightsStatus.RESTRICTED,
            TechnicalStatus.HEALTHY,
            "vod",
            1,
            True,
            PublicationStatus.REJECTED,
        ),
        (
            RightsStatus.VERIFIED,
            TechnicalStatus.HEALTHY,
            "live",
            None,
            False,
            PublicationStatus.WITHHELD,
        ),
    ],
)
def test_publication_matrix(
    rights: RightsStatus,
    technical: TechnicalStatus,
    kind: str,
    site_type: int | None,
    media: bool,
    expected: PublicationStatus,
) -> None:
    assert (
        publication_status_for(
            rights,
            technical,
            entity_kind=kind,  # type: ignore[arg-type]
            site_type=site_type,
            media_verified=media,
        )
        is expected
    )


def test_security_and_header_failures_override_publication_matrix() -> None:
    assert (
        publication_status_for(
            RightsStatus.VERIFIED,
            TechnicalStatus.HEALTHY,
            entity_kind="vod",
            site_type=1,
            failure_reasons=(FailureReason.CREDENTIAL_QUERY_REJECTED,),
        )
        is PublicationStatus.REJECTED
    )
    assert (
        publication_status_for(
            RightsStatus.VERIFIED,
            TechnicalStatus.PARTIAL,
            entity_kind="vod",
            site_type=1,
            failure_reasons=(FailureReason.CLIENT_HEADER_UNSUPPORTED,),
        )
        is PublicationStatus.WITHHELD
    )


def test_warning_prefix_is_added_exactly_once() -> None:
    assert warning_name("新闻", RightsStatus.PUBLIC_UNVERIFIED) == "⚠️ 新闻"
    assert warning_name("⚠️ 新闻", RightsStatus.PUBLIC_UNVERIFIED) == "⚠️ 新闻"
    assert warning_name("新闻", RightsStatus.VERIFIED) == "新闻"


def test_gate_ratio_is_strictly_greater_than_twenty_percent() -> None:
    common = {
        "release_kind": ReleaseKind.REGULAR,
        "previous_vod_ids": frozenset({"1", "2", "3", "4", "5"}),
        "previous_live_url_ids": frozenset(),
        "current_healthy_live_url_ids": frozenset(),
        "current_vod_sites": 4,
        "current_live_channels": 1,
    }
    at_boundary = evaluate_gates(
        **common,
        current_publishable_vod_ids=frozenset({"1", "2", "3", "4"}),
    )
    assert at_boundary.publish

    over_boundary = evaluate_gates(
        **common,
        current_publishable_vod_ids=frozenset({"1", "2", "3"}),
    )
    assert over_boundary.inconclusive
    assert "vod_failure_ratio" in over_boundary.reasons


def test_gate_rejects_zero_network_outage_and_missing_state() -> None:
    decision = evaluate_gates(
        release_kind=ReleaseKind.REGULAR,
        previous_vod_ids=frozenset(),
        current_publishable_vod_ids=frozenset(),
        previous_live_url_ids=frozenset(),
        current_healthy_live_url_ids=frozenset(),
        current_vod_sites=0,
        current_live_channels=0,
        failed_network_groups=2,
        state_available=False,
        previous_release_known=False,
    )
    assert decision.inconclusive
    assert set(decision.reasons) >= {
        "state_unavailable",
        "previous_release_unknown",
        "vod_zero",
        "live_zero",
        "network_outage",
    }


def test_bootstrap_skips_only_previous_state_requirements() -> None:
    decision = evaluate_gates(
        release_kind=ReleaseKind.BOOTSTRAP,
        previous_vod_ids=frozenset(),
        current_publishable_vod_ids=frozenset({"v"}),
        previous_live_url_ids=frozenset(),
        current_healthy_live_url_ids=frozenset({"l"}),
        current_vod_sites=1,
        current_live_channels=1,
        state_available=False,
        previous_release_known=False,
    )
    assert decision.publish


def test_mandatory_removal_bypasses_only_availability_gates() -> None:
    decision = evaluate_gates(
        release_kind=ReleaseKind.REGULAR,
        previous_vod_ids=frozenset({"v1", "v2", "v3", "v4", "v5"}),
        current_publishable_vod_ids=frozenset(),
        previous_live_url_ids=frozenset({"l1", "l2", "l3", "l4", "l5"}),
        current_healthy_live_url_ids=frozenset(),
        current_vod_sites=0,
        current_live_channels=0,
        failed_network_groups=4,
        mandatory_removal_ids=("source:z", "source:a", "source:a"),
    )
    assert decision.publish and not decision.inconclusive
    assert decision.release_kind is ReleaseKind.SAFETY
    assert decision.mandatory_removal_ids == ("source:a", "source:z")
    assert "safety_degraded" in decision.reasons


def test_safety_requires_a_readable_previous_release() -> None:
    decision = evaluate_gates(
        release_kind=ReleaseKind.REGULAR,
        previous_vod_ids=frozenset(),
        current_publishable_vod_ids=frozenset(),
        previous_live_url_ids=frozenset(),
        current_healthy_live_url_ids=frozenset(),
        current_vod_sites=0,
        current_live_channels=0,
        state_available=False,
        previous_release_known=False,
        mandatory_removal_ids=("source:bad",),
    )
    assert not decision.publish and decision.inconclusive
    assert set(decision.reasons) >= {
        "mandatory_removal",
        "safety_state_unavailable",
        "safety_baseline_unavailable",
    }


def test_safety_derivation_can_only_remove_previous_entities() -> None:
    assert safety_retained_ids(
        ["a", "b", "c"], ["b", "new-not-in-baseline"], dependent_ids=["c"]
    ) == ("a",)
