from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from domain.push.temporal_eligibility import (
    TemporalEligibility,
    evaluate_temporal_eligibility,
)

NOW = datetime(2026, 9, 1, 15, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    "event_type,age,expected",
    [
        ("RING_DETECTED", 0, TemporalEligibility(True)),
        ("RING_DETECTED", 29, TemporalEligibility(True)),
        ("RING_DETECTED", 30, TemporalEligibility(False, "expired", "over_30s")),
        ("RING_DETECTED", 31, TemporalEligibility(False, "expired", "over_30s")),
        ("RING_ENDED", 59, TemporalEligibility(True)),
        ("RING_ENDED", 60, TemporalEligibility(False, "expired", "over_60s")),
        ("RING_ENDED", 300, TemporalEligibility(False, "expired", "over_60s")),
    ],
)
def test_age_boundaries(event_type: str, age: int, expected: TemporalEligibility) -> None:
    assert (
        evaluate_temporal_eligibility(event_type, NOW - timedelta(seconds=age), "device", now=NOW)
        == expected
    )


def test_small_future_skew_is_allowed_but_larger_skew_is_suppressed() -> None:
    assert evaluate_temporal_eligibility(
        "RING_DETECTED", NOW + timedelta(seconds=5), "device", now=NOW
    ).eligible
    assert evaluate_temporal_eligibility(
        "RING_DETECTED", NOW + timedelta(seconds=6), "device", now=NOW
    ) == TemporalEligibility(False, "future_event_time", "future_over_5s")


def test_unknown_original_time_fails_closed() -> None:
    assert evaluate_temporal_eligibility(
        "RING_DETECTED", NOW, "unknown", now=NOW
    ) == TemporalEligibility(False, "unknown_event_time", "unknown")
