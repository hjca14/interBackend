"""Versioned, side-effect-free notification preference contract."""

from __future__ import annotations

import re
from copy import deepcopy
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DELIVERY_SCOPES = {"ANYWHERE", "LOCAL_ONLY", "AWAY_ONLY"}
BEHAVIORS = {"NOTIFICATION_ONLY", "BLOCK_ALL"}
TOP_LEVEL_FIELDS = {
    "incoming_calls_enabled",
    "notifications_enabled",
    "delivery_scope",
    "quiet_schedule",
}
QUIET_FIELDS = {"enabled", "timezone", "days", "start_time", "end_time", "behavior"}
TIME = re.compile(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]\Z")

DEFAULTS: dict[str, Any] = {
    "version": 1,
    "incoming_calls_enabled": True,
    "notifications_enabled": True,
    "delivery_scope": "ANYWHERE",
    "quiet_schedule": {
        "enabled": False,
        "timezone": None,
        "days": [],
        "start_time": None,
        "end_time": None,
        "behavior": "NOTIFICATION_ONLY",
    },
    "updated_at": None,
}


def combine(stored: object = None, patch: object = None) -> dict[str, Any]:
    """Merge stored values and a client patch over v1 defaults, then validate."""
    result = deepcopy(DEFAULTS)
    if stored is not None:
        if not isinstance(stored, dict):
            raise ValueError("stored preferences must be an object")
        _merge(result, stored, client=False)
    if patch is not None:
        if not isinstance(patch, dict) or not patch:
            raise ValueError("patch must be a non-empty object")
        _merge(result, patch, client=True)
    _validate(result)
    result["quiet_schedule"]["days"] = sorted(result["quiet_schedule"]["days"])
    return result


def _merge(result: dict[str, Any], values: dict[str, Any], *, client: bool) -> None:
    allowed = TOP_LEVEL_FIELDS if client else TOP_LEVEL_FIELDS | {"version", "updated_at"}
    if set(values) - allowed:
        raise ValueError("unknown or read-only field")
    for key, value in values.items():
        if key == "quiet_schedule":
            if not isinstance(value, dict) or set(value) - QUIET_FIELDS:
                raise ValueError("invalid quiet_schedule")
            result[key].update(value)
        else:
            result[key] = value


def _validate(value: dict[str, Any]) -> None:
    if value["version"] != 1:
        raise ValueError("unsupported version")
    if not isinstance(value["incoming_calls_enabled"], bool) or not isinstance(
        value["notifications_enabled"], bool
    ):
        raise ValueError("enabled fields must be boolean")
    if value["delivery_scope"] not in DELIVERY_SCOPES:
        raise ValueError("invalid delivery scope")
    quiet = value["quiet_schedule"]
    if not isinstance(quiet["enabled"], bool) or quiet["behavior"] not in BEHAVIORS:
        raise ValueError("invalid quiet schedule")
    days = quiet["days"]
    if not isinstance(days, list) or any(
        type(day) is not int or day not in range(1, 8) for day in days
    ):
        raise ValueError("invalid days")
    if len(days) != len(set(days)):
        raise ValueError("duplicate days")
    timezone = quiet["timezone"]
    if timezone is not None:
        if not isinstance(timezone, str):
            raise ValueError("invalid timezone")
        try:
            ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("invalid timezone") from None
    for field in ("start_time", "end_time"):
        time_value = quiet[field]
        if time_value is not None and (
            not isinstance(time_value, str) or TIME.fullmatch(time_value) is None
        ):
            raise ValueError("invalid local time")
    if quiet["enabled"]:
        if timezone is None or not days or quiet["start_time"] is None or quiet["end_time"] is None:
            raise ValueError("active schedule is incomplete")
        if quiet["start_time"] == quiet["end_time"]:
            raise ValueError("schedule interval must be non-zero")
    updated_at = value["updated_at"]
    if updated_at is not None:
        if not isinstance(updated_at, str):
            raise ValueError("invalid updated_at")
        datetime.strptime(updated_at, "%Y-%m-%dT%H:%M:%SZ")
