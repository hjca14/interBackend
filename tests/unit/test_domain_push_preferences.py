from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from domain.push.preferences import Decision, evaluate

# Mirrors lambdas/device_api/notification_preferences.py's DEFAULTS/combine()
# shape exactly -- evaluate() trusts its input to already look like this.
QUIET_OFF: dict[str, object] = {
    "enabled": False,
    "timezone": None,
    "days": [],
    "start_time": None,
    "end_time": None,
    "behavior": "NOTIFICATION_ONLY",
}


def prefs(alert_mode: str, quiet: dict[str, object] | None = None) -> dict[str, object]:
    return {
        "version": 1,
        "alert_mode": alert_mode,
        "quiet_schedule": quiet if quiet is not None else dict(QUIET_OFF),
        "updated_at": "2026-08-20T00:00:00Z",
    }


def quiet_on(
    *,
    timezone: str = "America/Sao_Paulo",
    days: list[int],
    start: str,
    end: str,
    behavior: str = "NOTIFICATION_ONLY",
) -> dict[str, object]:
    return {
        "enabled": True,
        "timezone": timezone,
        "days": days,
        "start_time": start,
        "end_time": end,
        "behavior": behavior,
    }


# A UTC instant that is always a Wednesday 12:00 in America/Sao_Paulo
# (UTC-3, no DST since 2019), independent of any real DST elsewhere.
WEDNESDAY_NOON_UTC = datetime(2026, 8, 19, 15, 0, tzinfo=UTC)


def test_unsupported_event_type_is_suppressed_without_raising() -> None:
    decision = evaluate("OFF_HOOK", prefs("RING_AND_NOTIFICATION"), now=WEDNESDAY_NOON_UTC)
    assert decision == Decision("NONE", True, False, False, "UNSUPPORTED_EVENT_TYPE")


def test_naive_now_is_rejected() -> None:
    with pytest.raises(ValueError):
        evaluate("RING_DETECTED", prefs("RING_AND_NOTIFICATION"), now=datetime(2026, 8, 19, 12, 0))


@pytest.mark.parametrize(
    "alert_mode,expected",
    [
        ("NONE", Decision("NONE", True, False, False, "ALERT_MODE_NONE")),
        ("RING_ONLY", Decision("RING_ONLY", False, False, False, None)),
        ("NOTIFICATION_ONLY", Decision("NOTIFICATION_ONLY", False, False, False, None)),
        ("RING_AND_NOTIFICATION", Decision("RING_ONLY", False, False, False, None)),
    ],
)
def test_all_alert_modes_outside_quiet_window(alert_mode: str, expected: Decision) -> None:
    assert evaluate("RING_DETECTED", prefs(alert_mode), now=WEDNESDAY_NOON_UTC) == expected


def test_quiet_disabled_is_ignored_even_with_stale_schedule_fields_preserved() -> None:
    # enabled=False but days/start/end still carry a previously-saved
    # schedule (the write-side PATCH only clears fields the client touches).
    stale = quiet_on(days=[3], start="00:00", end="23:59")
    stale["enabled"] = False
    decision = evaluate(
        "RING_DETECTED", prefs("RING_AND_NOTIFICATION", stale), now=WEDNESDAY_NOON_UTC
    )
    assert decision == Decision("RING_ONLY", False, False, False, None)


@pytest.mark.parametrize("alert_mode", ["RING_ONLY", "NOTIFICATION_ONLY", "RING_AND_NOTIFICATION"])
def test_block_all_suppresses_every_alert_mode_during_active_window(alert_mode: str) -> None:
    quiet = quiet_on(days=[3], start="10:00", end="14:00", behavior="BLOCK_ALL")
    decision = evaluate("RING_DETECTED", prefs(alert_mode, quiet), now=WEDNESDAY_NOON_UTC)
    assert decision == Decision("NONE", True, True, True, "QUIET_BLOCK_ALL")


@pytest.mark.parametrize(
    "alert_mode,expected",
    [
        ("NONE", Decision("NONE", True, False, False, "ALERT_MODE_NONE")),
        (
            "RING_ONLY",
            Decision(
                "NOTIFICATION_ONLY",
                False,
                True,
                True,
                "QUIET_NOTIFICATION_ONLY_REDUCED",
            ),
        ),
        ("NOTIFICATION_ONLY", Decision("NOTIFICATION_ONLY", False, True, False, None)),
        (
            "RING_AND_NOTIFICATION",
            Decision(
                "NOTIFICATION_ONLY",
                False,
                True,
                True,
                "QUIET_NOTIFICATION_ONLY_REDUCED",
            ),
        ),
    ],
)
def test_notification_only_schedule_reduces_allowed_calls_without_reactivating_none(
    alert_mode: str, expected: Decision
) -> None:
    quiet = quiet_on(days=[3], start="10:00", end="14:00", behavior="NOTIFICATION_ONLY")
    decision = evaluate("RING_DETECTED", prefs(alert_mode, quiet), now=WEDNESDAY_NOON_UTC)
    assert decision == expected


def test_same_day_window_active_strictly_between_boundaries() -> None:
    quiet = quiet_on(days=[3], start="10:00", end="14:00", behavior="BLOCK_ALL")
    inside = WEDNESDAY_NOON_UTC  # 12:00 local
    decision = evaluate("RING_DETECTED", prefs("RING_ONLY", quiet), now=inside)
    assert decision.suppressed


def test_same_day_window_start_boundary_is_inclusive_end_boundary_is_exclusive() -> None:
    quiet = quiet_on(days=[3], start="10:00", end="14:00", behavior="BLOCK_ALL")
    # 2026-08-19 is Wednesday in America/Sao_Paulo (UTC-3).
    at_start = datetime(2026, 8, 19, 13, 0, tzinfo=UTC)  # 10:00 local exactly
    just_before_start = at_start - timedelta(minutes=1)
    at_end = datetime(2026, 8, 19, 17, 0, tzinfo=UTC)  # 14:00 local exactly
    just_before_end = at_end - timedelta(minutes=1)

    assert evaluate("RING_DETECTED", prefs("RING_ONLY", quiet), now=at_start).suppressed
    assert not evaluate(
        "RING_DETECTED", prefs("RING_ONLY", quiet), now=just_before_start
    ).suppressed
    assert not evaluate("RING_DETECTED", prefs("RING_ONLY", quiet), now=at_end).suppressed
    assert evaluate("RING_DETECTED", prefs("RING_ONLY", quiet), now=just_before_end).suppressed


def test_same_day_window_outside_configured_weekday_is_not_active() -> None:
    # Thursday (isoweekday=4), schedule only configured for Wednesday (3).
    quiet = quiet_on(days=[3], start="10:00", end="14:00", behavior="BLOCK_ALL")
    thursday_noon = WEDNESDAY_NOON_UTC + timedelta(days=1)
    decision = evaluate("RING_DETECTED", prefs("RING_ONLY", quiet), now=thursday_noon)
    assert not decision.suppressed


def test_overnight_window_evening_and_early_morning_both_active() -> None:
    # 22:00 Wed -> 06:00 Thu local, scheduled only for Wednesday (3).
    quiet = quiet_on(days=[3], start="22:00", end="06:00", behavior="BLOCK_ALL")
    # 23:00 Wednesday local (evening portion, gated by Wednesday).
    evening = datetime(2026, 8, 20, 2, 0, tzinfo=UTC)  # 2026-08-19 23:00 local
    # 05:00 Thursday local (early-morning portion, still gated by Wednesday
    # -- the day the window *started* -- per the documented contract).
    early_morning = datetime(2026, 8, 20, 8, 0, tzinfo=UTC)  # 2026-08-20 05:00 local

    assert evaluate("RING_DETECTED", prefs("RING_ONLY", quiet), now=evening).suppressed
    assert evaluate("RING_DETECTED", prefs("RING_ONLY", quiet), now=early_morning).suppressed


def test_overnight_notification_schedule_reduces_ring_on_both_sides_of_midnight() -> None:
    quiet = quiet_on(days=[3], start="22:00", end="06:00", behavior="NOTIFICATION_ONLY")
    evening = datetime(2026, 8, 20, 2, 0, tzinfo=UTC)
    early_morning = datetime(2026, 8, 20, 8, 0, tzinfo=UTC)
    expected = Decision("NOTIFICATION_ONLY", False, True, True, "QUIET_NOTIFICATION_ONLY_REDUCED")
    assert evaluate("RING_DETECTED", prefs("RING_ONLY", quiet), now=evening) == expected
    assert evaluate("RING_DETECTED", prefs("RING_ONLY", quiet), now=early_morning) == expected


def test_overnight_window_early_morning_is_not_active_if_only_next_day_is_scheduled() -> None:
    # Schedule configured for Thursday (4), not Wednesday. The early-morning
    # hours after a Wednesday midnight must NOT be attributed to Thursday.
    quiet = quiet_on(days=[4], start="22:00", end="06:00", behavior="BLOCK_ALL")
    early_morning_after_wednesday = datetime(2026, 8, 20, 8, 0, tzinfo=UTC)  # Thu 05:00 local
    decision = evaluate(
        "RING_DETECTED", prefs("RING_ONLY", quiet), now=early_morning_after_wednesday
    )
    assert not decision.suppressed


def test_overnight_window_midday_between_end_and_start_is_not_active() -> None:
    quiet = quiet_on(days=[3, 4], start="22:00", end="06:00", behavior="BLOCK_ALL")
    decision = evaluate("RING_DETECTED", prefs("RING_ONLY", quiet), now=WEDNESDAY_NOON_UTC)
    assert not decision.suppressed


def test_overnight_window_exact_boundaries() -> None:
    quiet = quiet_on(days=[3], start="22:00", end="06:00", behavior="BLOCK_ALL")
    at_start = datetime(2026, 8, 20, 1, 0, tzinfo=UTC)  # Wed 22:00 local exactly
    just_before_start = at_start - timedelta(minutes=1)
    at_end = datetime(2026, 8, 20, 9, 0, tzinfo=UTC)  # Thu 06:00 local exactly
    just_before_end = at_end - timedelta(minutes=1)

    assert evaluate("RING_DETECTED", prefs("RING_ONLY", quiet), now=at_start).suppressed
    assert not evaluate(
        "RING_DETECTED", prefs("RING_ONLY", quiet), now=just_before_start
    ).suppressed
    assert not evaluate("RING_DETECTED", prefs("RING_ONLY", quiet), now=at_end).suppressed
    assert evaluate("RING_DETECTED", prefs("RING_ONLY", quiet), now=just_before_end).suppressed


@pytest.mark.parametrize("day", range(1, 8))
def test_every_iso_weekday_is_accepted(day: int) -> None:
    quiet = quiet_on(days=[day], start="10:00", end="14:00", behavior="BLOCK_ALL")
    # Pick the UTC instant landing on ISO weekday `day` at local noon.
    now = WEDNESDAY_NOON_UTC + timedelta(days=(day - 3) % 7)
    decision = evaluate("RING_DETECTED", prefs("RING_ONLY", quiet), now=now)
    assert decision.suppressed


def test_timezone_with_daylight_saving_time_shifts_the_window() -> None:
    # America/New_York observes DST; a schedule of 10:00-14:00 local must
    # track the local clock, not a fixed UTC offset, across the transition.
    # 2026-03-08 02:00 America/New_York is the (post-2007 US rule) spring
    # DST transition -- clocks jump from 02:00 to 03:00 EST->EDT.
    quiet = quiet_on(
        timezone="America/New_York",
        days=list(range(1, 8)),
        start="10:00",
        end="14:00",
        behavior="BLOCK_ALL",
    )
    before_dst = datetime(2026, 3, 1, 15, 0, tzinfo=UTC)  # EST (UTC-5) -> 10:00 local
    after_dst = datetime(2026, 3, 15, 14, 0, tzinfo=UTC)  # EDT (UTC-4) -> 10:00 local

    assert evaluate("RING_DETECTED", prefs("RING_ONLY", quiet), now=before_dst).suppressed
    assert evaluate("RING_DETECTED", prefs("RING_ONLY", quiet), now=after_dst).suppressed
    # The same fixed UTC instant that was 10:00 EST is 11:00 EDT, i.e. still
    # inside the window only because of the local-time (not UTC-offset)
    # evaluation; shifting one more hour should now fall outside for EDT.
    outside_after_dst = after_dst + timedelta(hours=4, minutes=1)
    assert not evaluate(
        "RING_DETECTED", prefs("RING_ONLY", quiet), now=outside_after_dst
    ).suppressed


def test_invalid_timezone_raises_for_the_caller_to_fall_back_on() -> None:
    quiet = quiet_on(timezone="Not/AZone", days=[3], start="10:00", end="14:00")
    with pytest.raises(ValueError):
        evaluate("RING_DETECTED", prefs("RING_ONLY", quiet), now=WEDNESDAY_NOON_UTC)


@pytest.mark.parametrize(
    "broken_quiet",
    [
        {**quiet_on(days=[3], start="10:00", end="14:00"), "days": "not-a-list"},
        {**quiet_on(days=[3], start="10:00", end="14:00"), "start_time": None},
        {
            **quiet_on(days=[3], start="10:00", end="14:00"),
            "start_time": "10:00",
            "end_time": "10:00",
        },
        {**quiet_on(days=[3], start="10:00", end="14:00"), "behavior": "GARBAGE"},
    ],
)
def test_incomplete_or_malformed_active_schedule_raises_for_the_caller_to_fall_back_on(
    broken_quiet: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        evaluate("RING_DETECTED", prefs("RING_ONLY", broken_quiet), now=WEDNESDAY_NOON_UTC)


def test_invalid_alert_mode_raises() -> None:
    with pytest.raises(ValueError):
        evaluate("RING_DETECTED", prefs("GARBAGE"), now=WEDNESDAY_NOON_UTC)


def test_two_memberships_on_the_same_device_can_reach_different_decisions() -> None:
    owner = evaluate("RING_DETECTED", prefs("RING_AND_NOTIFICATION"), now=WEDNESDAY_NOON_UTC)
    member = evaluate("RING_DETECTED", prefs("NONE"), now=WEDNESDAY_NOON_UTC)
    assert not owner.suppressed
    assert member.suppressed
