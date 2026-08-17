"""Protocol-v1 message construction and deliberately narrow command parsing."""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

MAX_COMMAND_BYTES = 8 * 1024
HEX_ID = re.compile(r"^[0-9a-f]{32}$")
ALLOWED_COMMANDS = frozenset({"OPEN_DOOR", "RESTART"})


@dataclass(frozen=True)
class Command:
    command_id: str
    command: str


def _encoded(payload: dict[str, object]) -> bytes:
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()


def health_payload(
    device_id: str,
    *,
    firmware_version: str,
    intercom_state: str,
    uptime_ms: int,
    wifi_rssi: int,
    free_heap: int,
) -> bytes:
    return _encoded(
        {
            "protocol_version": 1,
            "device_id": device_id,
            "firmware_version": firmware_version,
            "intercom_state": intercom_state,
            "uptime_ms": uptime_ms,
            "wifi_rssi": wifi_rssi,
            "free_heap": free_heap,
        }
    )


def safe_event_payload(device_id: str, *, timestamp: datetime | None = None) -> bytes:
    payload: dict[str, object] = {
        "protocol_version": 1,
        "device_id": device_id,
        "event_id": f"evt-{uuid.uuid4().hex}",
        "event": "ERROR",
        "error_code": "SMOKE_TEST",
    }
    if timestamp is not None:
        if timestamp.tzinfo is None or timestamp.utcoffset() != UTC.utcoffset(timestamp):
            raise ValueError("event timestamp must be trustworthy UTC")
        payload["timestamp"] = timestamp.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return _encoded(payload)


def parse_command(payload: bytes) -> Command:
    if len(payload) > MAX_COMMAND_BYTES:
        raise ValueError("command payload exceeds 8 KiB")
    try:
        decoded: Any = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ValueError("malformed command JSON") from error
    if not isinstance(decoded, dict):
        raise ValueError("command must be a JSON object")
    if decoded.get("protocol_version") != 1:
        raise ValueError("unsupported protocol_version")
    command_id = decoded.get("command_id")
    command = decoded.get("command")
    if not isinstance(command_id, str) or HEX_ID.fullmatch(command_id) is None:
        raise ValueError("invalid command_id")
    if not isinstance(command, str) or command not in ALLOWED_COMMANDS:
        raise ValueError("invalid command")
    return Command(command_id=command_id, command=command)


def rejected_response(command: Command, device_id: str) -> bytes:
    return _encoded(
        {
            "protocol_version": 1,
            "device_id": device_id,
            "command_id": command.command_id,
            "command": command.command,
            "status": "REJECTED",
            "error_code": "COMMAND_NOT_ALLOWED",
        }
    )
