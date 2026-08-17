"""Fail-closed parsing and normalization of the IoT Rule envelope.

This module deliberately imports neither boto3 nor CDK.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

DEVICE_ID = re.compile(r"^ib-[0-9a-f]{32}$")
HEX_ID = re.compile(r"^[0-9a-f]{32}$")
VERSION = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
STATES = frozenset({"IDLE", "RINGING", "OFF_HOOK", "IN_CALL", "ERROR"})
EVENTS = frozenset({"BUTTON_PRESSED", "STATE_CHANGED", "ERROR", "CONNECTED", "DISCONNECTED"})
TECHNICAL_EVENTS = frozenset({"CONNECTED", "DISCONNECTED"})
RESPONSE_STATUSES = frozenset({"SUCCESS", "REJECTED", "FAILED"})
COMMANDS = frozenset({"OPEN_DOOR", "RESTART"})
MAX_CLOCK_SKEW = timedelta(hours=24)


class InvalidMessage(ValueError):
    """A canonical, safe validation failure (never contains payload data)."""


@dataclass(frozen=True)
class Message:
    device_id: str
    category: str
    received_at: datetime
    occurred_at: datetime
    values: dict[str, str | int]
    identifier: str | None = None
    detailed: bool = False

    @property
    def hour(self) -> str:
        return self.received_at.strftime("%Y-%m-%dT%H")

    @property
    def metric_key(self) -> str:
        return f"METRIC#{self.hour}"

    @property
    def detail_key(self) -> str | None:
        if not self.detailed or self.identifier is None:
            return None
        prefix = "EVENT" if self.category == "events" else "RESPONSE"
        stamp = self.occurred_at.strftime("%Y-%m-%dT%H:%M:%SZ")
        return f"{prefix}#{stamp}#{self.identifier}"


def _integer(
    value: Any, name: str, *, minimum: int | None = None, maximum: int | None = None
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidMessage(f"invalid_{name}")
    if minimum is not None and value < minimum or maximum is not None and value > maximum:
        raise InvalidMessage(f"invalid_{name}")
    return value


def _timestamp(value: Any, received: datetime, *, required: bool) -> datetime:
    if value is None and not required:
        return received
    if not isinstance(value, str) or not value.endswith("Z"):
        raise InvalidMessage("invalid_timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise InvalidMessage("invalid_timestamp") from error
    if parsed.tzinfo != UTC or parsed.microsecond or abs(parsed - received) > MAX_CLOCK_SKEW:
        raise InvalidMessage("invalid_timestamp")
    return parsed


def parse_envelope(envelope: object, *, max_payload_bytes: int) -> Message:
    if not isinstance(envelope, dict):
        raise InvalidMessage("invalid_envelope")
    try:
        encoded = json.dumps(envelope, separators=(",", ":"), ensure_ascii=True).encode()
    except (TypeError, ValueError) as error:
        raise InvalidMessage("malformed_json") from error
    if len(encoded) > max_payload_bytes:
        raise InvalidMessage("payload_too_large")
    topic_device = envelope.get("_ib_device_id")
    category = envelope.get("_ib_category")
    received_ms = envelope.get("_ib_received_at")
    if not isinstance(topic_device, str) or DEVICE_ID.fullmatch(topic_device) is None:
        raise InvalidMessage("invalid_topic_device")
    if category not in {"events", "health", "responses"}:
        raise InvalidMessage("unexpected_category")
    received = datetime.fromtimestamp(_integer(received_ms, "received_at", minimum=0) / 1000, UTC)
    if envelope.get("protocol_version") != 1:
        raise InvalidMessage("unsupported_protocol_version")
    if envelope.get("device_id") != topic_device:
        raise InvalidMessage("device_id_mismatch")

    common = {"protocol_version", "device_id", "_ib_device_id", "_ib_category", "_ib_received_at"}
    if category == "health":
        allowed = common | {
            "firmware_version",
            "intercom_state",
            "uptime_ms",
            "wifi_rssi",
            "free_heap",
        }
        _exact_fields(envelope, allowed, allowed)
        state = envelope["intercom_state"]
        version = envelope["firmware_version"]
        if (
            state not in STATES
            or not isinstance(version, str)
            or VERSION.fullmatch(version) is None
        ):
            raise InvalidMessage("invalid_health_enum")
        values: dict[str, str | int] = {
            "firmware_version": version,
            "last_state": state,
            "RSSI": _integer(envelope["wifi_rssi"], "wifi_rssi", minimum=-127, maximum=0),
            "free_heap": _integer(envelope["free_heap"], "free_heap", minimum=0),
        }
        _integer(envelope["uptime_ms"], "uptime_ms", minimum=0)
        return Message(topic_device, category, received, received, values)

    if category == "events":
        allowed = common | {"event_id", "event", "timestamp", "state", "error_code"}
        _exact_fields(envelope, allowed, common | {"event_id", "event"})
        event_id = envelope["event_id"]
        event = envelope["event"]
        if not isinstance(event_id, str) or HEX_ID.fullmatch(event_id) is None:
            raise InvalidMessage("invalid_event_id")
        if event not in EVENTS:
            raise InvalidMessage("invalid_event")
        occurred = _timestamp(envelope.get("timestamp"), received, required=False)
        values = {"event": event}
        if "state" in envelope:
            if envelope["state"] not in STATES:
                raise InvalidMessage("invalid_state")
            values["state"] = envelope["state"]
        if "error_code" in envelope:
            code = envelope["error_code"]
            if not isinstance(code, str) or VERSION.fullmatch(code) is None:
                raise InvalidMessage("invalid_error_code")
            values["error_code"] = code
        return Message(
            topic_device,
            category,
            received,
            occurred,
            values,
            event_id,
            event not in TECHNICAL_EVENTS,
        )

    allowed = common | {"command_id", "command", "status", "error_code", "timestamp"}
    _exact_fields(envelope, allowed, common | {"command_id", "command", "status"})
    command_id = envelope["command_id"]
    if not isinstance(command_id, str) or HEX_ID.fullmatch(command_id) is None:
        raise InvalidMessage("invalid_command_id")
    if envelope["command"] not in COMMANDS or envelope["status"] not in RESPONSE_STATUSES:
        raise InvalidMessage("invalid_response_enum")
    values = {"command": envelope["command"], "status": envelope["status"]}
    if "error_code" in envelope:
        code = envelope["error_code"]
        if not isinstance(code, str) or VERSION.fullmatch(code) is None:
            raise InvalidMessage("invalid_error_code")
        values["error_code"] = code
    return Message(
        topic_device,
        category,
        received,
        _timestamp(envelope.get("timestamp"), received, required=False),
        values,
        command_id,
        True,
    )


def _exact_fields(payload: dict[str, Any], allowed: set[str], required: set[str]) -> None:
    if not required <= payload.keys():
        raise InvalidMessage("missing_required_field")
    if payload.keys() - allowed:
        raise InvalidMessage("unknown_field")
