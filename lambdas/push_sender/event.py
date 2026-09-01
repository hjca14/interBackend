"""Validates the internal invocation payload telemetry_ingestion sends to
this Lambda.

This is deliberately NOT a new device/firmware protocol: it is a small,
versioned Lambda-to-Lambda envelope built by
``lambdas/telemetry_ingestion/handler.py`` *from* an already-validated
``domain.telemetry.models.Message`` (parsed with the one canonical
firmware event parser). Re-validating it here, using the exact same
``device_id``/``event_id`` patterns as that canonical parser, is defense
in depth against a misconfigured or buggy caller -- it is not a second
place where the device event contract is defined.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from domain.telemetry.models import CALL_ID, DEVICE_ID, EVENT_ID

SUPPORTED_SCHEMA_VERSION = 1
SUPPORTED_EVENTS = frozenset({"RING_DETECTED", "RING_ENDED"})
MAX_PAYLOAD_KEYS = 6


class InvalidInvocation(ValueError):
    """A structurally invalid invocation payload. Never includes the raw
    payload in its message -- callers must not log ``args`` from this.
    """


@dataclass(frozen=True)
class RingEvent:
    device_id: str
    event_id: str
    event: str
    call_id: str
    occurred_at: datetime


def parse_invocation(payload: object) -> RingEvent:
    if not isinstance(payload, dict) or len(payload) > MAX_PAYLOAD_KEYS:
        raise InvalidInvocation("invalid_payload")
    if payload.get("schema_version") != SUPPORTED_SCHEMA_VERSION:
        raise InvalidInvocation("unsupported_schema_version")
    device_id = payload.get("device_id")
    event_id = payload.get("event_id")
    event = payload.get("event")
    call_id = payload.get("call_id")
    occurred_at_raw = payload.get("occurred_at")
    if not isinstance(device_id, str) or DEVICE_ID.fullmatch(device_id) is None:
        raise InvalidInvocation("invalid_device_id")
    if not isinstance(event_id, str) or EVENT_ID.fullmatch(event_id) is None:
        raise InvalidInvocation("invalid_event_id")
    if event not in SUPPORTED_EVENTS:
        raise InvalidInvocation("unsupported_event")
    if call_id is None and event == "RING_DETECTED":
        call_id = f"call-{event_id.removeprefix('evt-')}"
    if not isinstance(call_id, str) or CALL_ID.fullmatch(call_id) is None:
        raise InvalidInvocation("invalid_call_id")
    if not isinstance(occurred_at_raw, str) or not occurred_at_raw.endswith("Z"):
        raise InvalidInvocation("invalid_occurred_at")
    try:
        occurred_at = datetime.fromisoformat(occurred_at_raw.replace("Z", "+00:00"))
    except ValueError:
        raise InvalidInvocation("invalid_occurred_at") from None
    if occurred_at.tzinfo != UTC:
        raise InvalidInvocation("invalid_occurred_at")
    return RingEvent(
        device_id=device_id,
        event_id=event_id,
        event=event,
        call_id=call_id,
        occurred_at=occurred_at,
    )
