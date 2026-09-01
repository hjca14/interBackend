"""Pure evaluation of ``notification_preferences`` against a device event.

No AWS, no FCM, no I/O, no clock reads -- ``now`` is always supplied by the
caller. See ``docs/fcm-notification-sender.md`` for the full behavior
matrix this module implements.

This module deliberately trusts its ``preferences`` argument to already be
the fully validated, defaulted v1 shape produced by
``lambdas.device_api.notification_preferences.combine()`` (the same
function the GET/PATCH preferences API already uses). Parsing/validation of
raw, possibly-corrupt stored data is the caller's job -- see
``lambdas/push_sender/handler.py``'s ``_decide()`` -- so a caller never
needs to guard a fan-out against one user's malformed preferences: ``combine(None)``
already defines the safe fallback (v1 defaults), and the caller is expected
to fall back to it before calling :func:`evaluate`.

Only ``RING_DETECTED`` is preference-controlled today; any other ``event_type`` is
suppressed with ``UNSUPPORTED_EVENT_TYPE`` rather than raising, so this
function stays total over well-typed input.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from datetime import time as time_
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ALERT_MODES = frozenset({"NONE", "RING_ONLY", "NOTIFICATION_ONLY", "RING_AND_NOTIFICATION"})
QUIET_BEHAVIORS = frozenset({"NOTIFICATION_ONLY", "BLOCK_ALL"})
SUPPORTED_EVENT_TYPES = frozenset({"RING_DETECTED"})

# Base (pre-quiet-schedule) permissions per alert_mode. A mode grants a
# capability if it appears in the corresponding set -- this table is the
# single source of truth the rest of this module derives from.
_BASE_RING = {"RING_ONLY", "RING_AND_NOTIFICATION"}


@dataclass(frozen=True)
class Decision:
    """The outcome of evaluating one membership's preferences for one event.

    ``delivery_mode`` is always one of the four existing ``alert_mode``
    values -- this module never invents a new enumeration. ``"NONE"`` means
    suppressed (no FCM message should be sent); the other three values are
    exactly the ``presentation_intent`` the FCM payload composer should use.
    """

    delivery_mode: str
    suppressed: bool
    quiet_active: bool
    quiet_reduced: bool
    reason: str | None


def _suppressed(
    reason: str, *, quiet_active: bool = False, quiet_reduced: bool = False
) -> Decision:
    return Decision(
        delivery_mode="NONE",
        suppressed=True,
        quiet_active=quiet_active,
        quiet_reduced=quiet_reduced,
        reason=reason,
    )


def _delivered(mode: str, *, quiet_active: bool, quiet_reduced: bool) -> Decision:
    return Decision(
        delivery_mode=mode,
        suppressed=False,
        quiet_active=quiet_active,
        quiet_reduced=quiet_reduced,
        reason="QUIET_NOTIFICATION_ONLY_REDUCED" if quiet_reduced else None,
    )


def _parse_time(value: object) -> time_:
    if not isinstance(value, str):
        raise ValueError("invalid local time")
    hour_str, _, minute_str = value.partition(":")
    if not hour_str.isdigit() or not minute_str.isdigit():
        raise ValueError("invalid local time")
    return time_(int(hour_str), int(minute_str))


def _quiet_active(quiet: dict[str, object], *, now: datetime) -> bool:
    """Whether ``now`` (UTC, aware) falls inside the configured quiet window.

    Converts to the configured IANA timezone (DST-aware via ``zoneinfo``)
    and evaluates two half-open sub-windows so an overnight window is
    correctly attributed to the day it *started*, per
    ``docs/notification-preferences.md``:

    - the evening portion, gated by today's weekday and ``local >= start``;
    - the early-morning portion (only when the window wraps past midnight),
      gated by *yesterday's* weekday and ``local < end``.

    Both start and end are local ``HH:MM`` boundaries; the window is
    half-open ``[start, end)`` -- inclusive of the exact start minute,
    exclusive of the exact end minute. This is a deliberate, documented
    choice (the contract itself does not specify boundary inclusivity).
    """
    timezone = ZoneInfo(str(quiet["timezone"]))
    local = now.astimezone(timezone)
    weekday = local.isoweekday()
    local_time = local.time()
    days = quiet["days"]
    if not isinstance(days, list) or any(type(day) is not int for day in days):
        raise ValueError("invalid days")
    start = _parse_time(quiet["start_time"])
    end = _parse_time(quiet["end_time"])
    if start == end:
        raise ValueError("schedule interval must be non-zero")

    if start < end:
        return weekday in days and start <= local_time < end

    previous_weekday = weekday - 1 or 7
    evening = weekday in days and local_time >= start
    early_morning = previous_weekday in days and local_time < end
    return evening or early_morning


def evaluate(event_type: str, preferences: dict[str, object], *, now: datetime) -> Decision:
    """Decide whether/how to present ``event_type`` for one membership.

    ``preferences`` must already be the fully validated v1 shape (see the
    module docstring). ``now`` must be timezone-aware UTC.
    """
    if event_type not in SUPPORTED_EVENT_TYPES:
        return _suppressed("UNSUPPORTED_EVENT_TYPE")
    if now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")

    alert_mode = preferences["alert_mode"]
    if alert_mode not in ALERT_MODES:
        raise ValueError("invalid alert_mode")

    allow_ring = alert_mode in _BASE_RING
    if alert_mode == "NONE":
        return _suppressed("ALERT_MODE_NONE")

    quiet = preferences["quiet_schedule"]
    if not isinstance(quiet, dict) or not isinstance(quiet.get("enabled"), bool):
        raise ValueError("invalid quiet_schedule")

    quiet_active = False
    if quiet["enabled"]:
        try:
            quiet_active = _quiet_active(quiet, now=now)
        except (ZoneInfoNotFoundError, ValueError, KeyError, TypeError):
            raise ValueError("invalid or incomplete quiet_schedule") from None

    if not quiet_active:
        mode = "RING_ONLY" if allow_ring else "NOTIFICATION_ONLY"
        return _delivered(mode, quiet_active=False, quiet_reduced=False)

    behavior = quiet.get("behavior")
    if behavior not in QUIET_BEHAVIORS:
        raise ValueError("invalid quiet_schedule.behavior")

    if behavior == "BLOCK_ALL":
        return _suppressed("QUIET_BLOCK_ALL", quiet_active=True, quiet_reduced=True)

    # behavior == "NOTIFICATION_ONLY": reduce an allowed call to the same
    # actionable event with a less interruptive presentation. This does not
    # re-enable NONE (handled above); it changes how an already-allowed ring
    # is presented while its call session remains valid.
    quiet_reduced = allow_ring
    return _delivered("NOTIFICATION_ONLY", quiet_active=True, quiet_reduced=quiet_reduced)
