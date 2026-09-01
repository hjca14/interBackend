"""Pure temporal eligibility policy for ring lifecycle pushes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

RING_DETECTED_PUSH_MAX_AGE_SECONDS = 30
RING_ENDED_PUSH_MAX_AGE_SECONDS = 60
MAX_FUTURE_CLOCK_SKEW_SECONDS = 5


@dataclass(frozen=True)
class TemporalEligibility:
    eligible: bool
    reason: str | None = None
    age_bucket: str | None = None


def max_age_seconds(event_type: str) -> int:
    if event_type == "RING_DETECTED":
        return RING_DETECTED_PUSH_MAX_AGE_SECONDS
    if event_type == "RING_ENDED":
        return RING_ENDED_PUSH_MAX_AGE_SECONDS
    raise ValueError("unsupported event type")


def evaluate_temporal_eligibility(
    event_type: str,
    occurred_at: datetime,
    timestamp_source: str,
    *,
    now: datetime,
) -> TemporalEligibility:
    if now.utcoffset() is None or occurred_at.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    if timestamp_source != "device":
        return TemporalEligibility(False, "unknown_event_time", "unknown")
    if occurred_at > now + timedelta(seconds=MAX_FUTURE_CLOCK_SKEW_SECONDS):
        return TemporalEligibility(False, "future_event_time", "future_over_5s")
    max_age = max_age_seconds(event_type)
    if now >= occurred_at + timedelta(seconds=max_age):
        return TemporalEligibility(False, "expired", f"over_{max_age}s")
    return TemporalEligibility(True)
