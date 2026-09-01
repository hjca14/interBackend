from __future__ import annotations

from datetime import UTC, datetime

import pytest

from domain.telemetry.models import InvalidMessage, parse_envelope

DEVICE = "ib-" + "a" * 32
EVENT_ID = "evt-" + "b" * 32
COMMAND_ID = "c" * 32
RECEIVED = 1_786_977_245_000


def envelope(category: str, **values: object) -> dict[str, object]:
    return {
        "ibmeta_device_id": DEVICE,
        "ibmeta_category": category,
        "ibmeta_received_at": RECEIVED,
        "protocol_version": 1,
        "device_id": DEVICE,
        **values,
    }


def test_health_updates_state_and_hour_metric_without_history() -> None:
    message = parse_envelope(
        envelope(
            "health",
            firmware_version="1.0.0",
            intercom_state="IDLE",
            uptime_ms=10,
            wifi_rssi=-50,
            free_heap=1000,
        ),
        max_payload_bytes=8192,
    )
    assert message.detail_key is None
    assert message.metric_key == "METRIC#2026-08-17T14"
    assert message.values["RSSI"] == -50


def test_functional_event_and_response_have_ordered_keys() -> None:
    event = parse_envelope(
        envelope(
            "events",
            event_id=EVENT_ID,
            event="ERROR",
            error_code="SENSOR_ERROR",
            timestamp="2026-08-17T14:30:25Z",
        ),
        max_payload_bytes=8192,
    )
    response = parse_envelope(
        envelope(
            "responses",
            command_id=COMMAND_ID,
            command="OPEN_DOOR",
            status="REJECTED",
            error={"code": "COMMAND_NOT_ALLOWED", "message": "Command rejected"},
        ),
        max_payload_bytes=8192,
    )
    assert event.detail_key == f"EVENT#2026-08-17T14:30:25Z#{EVENT_ID}"
    assert response.detail_key == f"RESPONSE#2026-08-17T14:34:05Z#{COMMAND_ID}"


def test_all_official_firmware_events_are_accepted() -> None:
    from domain.telemetry.models import EVENTS

    assert len(EVENTS) == 16
    for event_name in EVENTS:
        extra = {"call_id": "call-" + "f" * 32} if event_name == "RING_ENDED" else {}
        message = parse_envelope(
            envelope("events", event_id=EVENT_ID, event=event_name, **extra),
            max_payload_bytes=8192,
        )
        assert message.detail_key is not None


def test_invented_connectivity_event_is_rejected() -> None:
    with pytest.raises(InvalidMessage, match="invalid_event"):
        parse_envelope(
            envelope("events", event_id=EVENT_ID, event="CONNECTED"), max_payload_bytes=8192
        )


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"protocol_version": 2}, "unsupported_protocol_version"),
        ({"device_id": "ib-" + "d" * 32}, "device_id_mismatch"),
        ({"ibmeta_category": "commands"}, "unexpected_category"),
        ({"event": "UNKNOWN"}, "invalid_event"),
        ({"event_id": "bad"}, "invalid_event_id"),
        ({"timestamp": "not-a-time"}, "invalid_timestamp"),
    ],
)
def test_events_fail_closed(change: dict[str, object], reason: str) -> None:
    payload = envelope("events", event_id=EVENT_ID, event="ERROR")
    payload.update(change)
    with pytest.raises(InvalidMessage, match=reason):
        parse_envelope(payload, max_payload_bytes=8192)


def test_oversized_payload_is_rejected() -> None:
    with pytest.raises(InvalidMessage, match="payload_too_large"):
        parse_envelope(envelope("events", event_id=EVENT_ID, event="ERROR"), max_payload_bytes=10)


def test_ttl_reference_time_is_exact_utc() -> None:
    message = parse_envelope(
        envelope("events", event_id=EVENT_ID, event="ERROR"), max_payload_bytes=8192
    )
    assert message.received_at == datetime(2026, 8, 17, 14, 34, 5, tzinfo=UTC)


def test_pure_domain_has_no_aws_dependency() -> None:
    import domain.telemetry.models as module

    names = set(module.__dict__)
    assert "boto3" not in names
    assert "aws_cdk" not in names


@pytest.mark.parametrize("status", ["ACCEPTED", "COMPLETED"])
def test_real_response_without_timestamp_uses_received_time(status: str) -> None:
    message = parse_envelope(
        envelope(
            "responses",
            command_id=COMMAND_ID,
            command="OPEN_DOOR",
            status=status,
            future_protocol_field={"ignored": True},
        ),
        max_payload_bytes=8192,
    )
    assert message.values == {"command": "OPEN_DOOR", "status": status}
    assert message.occurred_at == message.received_at


@pytest.mark.parametrize("status", ["FAILED", "REJECTED"])
def test_error_response_preserves_sanitized_error_object(status: str) -> None:
    message = parse_envelope(
        envelope(
            "responses",
            command_id=COMMAND_ID,
            command="RESTART",
            status=status,
            error={"code": "COMMAND_FAILED", "message": "Safe canonical description"},
        ),
        max_payload_bytes=8192,
    )
    assert message.values["error"] == {
        "code": "COMMAND_FAILED",
        "message": "Safe canonical description",
    }


def test_health_tolerates_additional_protocol_fields() -> None:
    message = parse_envelope(
        envelope(
            "health",
            firmware_version="1.0.0",
            intercom_state="IDLE",
            uptime_ms=10,
            wifi_rssi=-50,
            free_heap=1000,
            future_field="ignored",
        ),
        max_payload_bytes=8192,
    )
    assert "future_field" not in message.values


@pytest.mark.parametrize("received", [None, "bad", True, -1, 10**100])
def test_invalid_internal_received_time_is_canonical_validation_error(received: object) -> None:
    payload = envelope("events", event_id=EVENT_ID, event="ERROR")
    payload["ibmeta_received_at"] = received
    with pytest.raises(InvalidMessage, match="invalid_received_at"):
        parse_envelope(payload, max_payload_bytes=8192)


@pytest.mark.parametrize(
    "missing,reason",
    [
        ("ibmeta_device_id", "invalid_topic_device"),
        ("ibmeta_category", "unexpected_category"),
        ("ibmeta_received_at", "invalid_received_at"),
    ],
)
def test_internal_metadata_is_required(missing: str, reason: str) -> None:
    payload = envelope("events", event_id=EVENT_ID, event="ERROR")
    del payload[missing]
    with pytest.raises(InvalidMessage, match=reason):
        parse_envelope(payload, max_payload_bytes=8192)


def test_legacy_internal_metadata_names_are_not_accepted() -> None:
    payload = envelope("events", event_id=EVENT_ID, event="ERROR")
    for current in ("ibmeta_device_id", "ibmeta_category", "ibmeta_received_at"):
        value = payload.pop(current)
        payload["_" + current.replace("ibmeta", "ib", 1)] = value
    with pytest.raises(InvalidMessage, match="invalid_topic_device"):
        parse_envelope(payload, max_payload_bytes=8192)


def test_event_id_requires_evt_prefix_but_command_id_forbids_it() -> None:
    valid_event = parse_envelope(
        envelope("events", event_id="evt-" + "d" * 32, event="RING_DETECTED"),
        max_payload_bytes=8192,
    )
    assert valid_event.identifier == "evt-" + "d" * 32

    with pytest.raises(InvalidMessage, match="invalid_event_id"):
        parse_envelope(
            envelope("events", event_id="d" * 32, event="RING_DETECTED"),
            max_payload_bytes=8192,
        )


def test_ring_detected_call_id_and_legacy_derivation() -> None:
    explicit = parse_envelope(
        envelope(
            "events",
            event_id="evt-" + "d" * 32,
            event="RING_DETECTED",
            call_id="call-" + "e" * 32,
        ),
        max_payload_bytes=8192,
    )
    assert explicit.values["call_id"] == "call-" + "e" * 32
    assert explicit.identifier == "evt-" + "d" * 32
    assert explicit.values["timestamp_source"] == "unknown"

    legacy = parse_envelope(
        envelope("events", event_id="evt-" + "d" * 32, event="RING_DETECTED"),
        max_payload_bytes=8192,
    )
    assert legacy.values["call_id"] == "call-" + "d" * 32
    assert legacy.values["legacy_call_id"] == 1

    timestamped = parse_envelope(
        envelope(
            "events",
            event_id="evt-" + "f" * 32,
            event="RING_DETECTED",
            timestamp="2026-08-17T14:34:05Z",
        ),
        max_payload_bytes=8192,
    )
    assert timestamped.values["timestamp_source"] == "device"

    unknown = parse_envelope(
        envelope(
            "events",
            event_id="evt-" + "a" * 32,
            event="RING_DETECTED",
            timestamp=None,
        ),
        max_payload_bytes=8192,
    )
    assert unknown.values["timestamp_source"] == "unknown"
    assert unknown.occurred_at == unknown.received_at


def test_ring_ended_requires_valid_call_id() -> None:
    with pytest.raises(InvalidMessage, match="missing_call_id"):
        parse_envelope(
            envelope("events", event_id="evt-" + "d" * 32, event="RING_ENDED"),
            max_payload_bytes=8192,
        )
    ended = parse_envelope(
        envelope(
            "events",
            event_id="evt-" + "e" * 32,
            event="RING_ENDED",
            call_id="call-" + "d" * 32,
        ),
        max_payload_bytes=8192,
    )
    assert ended.identifier == "evt-" + "e" * 32
    assert ended.values["call_id"] == "call-" + "d" * 32
    with pytest.raises(InvalidMessage, match="invalid_command_id"):
        parse_envelope(
            envelope(
                "responses",
                command_id="evt-" + "d" * 32,
                command="OPEN_DOOR",
                status="REJECTED",
            ),
            max_payload_bytes=8192,
        )


@pytest.mark.parametrize(
    "command",
    ["ENTER_PROVISIONING", "FACTORY_RESET", "UNKNOWN_COMMAND"],
)
def test_rejected_response_accepts_safe_echoed_command(command: str) -> None:
    message = parse_envelope(
        envelope(
            "responses",
            command_id=COMMAND_ID,
            command=command,
            status="REJECTED",
        ),
        max_payload_bytes=8192,
    )
    assert message.values["command"] == command


@pytest.mark.parametrize("command", ["", "   ", "A" * 65, "OPEN\nDOOR", "BAD\x7fCOMMAND"])
def test_response_rejects_empty_oversized_or_control_command(command: str) -> None:
    with pytest.raises(InvalidMessage, match="invalid_response_command"):
        parse_envelope(
            envelope(
                "responses",
                command_id=COMMAND_ID,
                command=command,
                status="REJECTED",
            ),
            max_payload_bytes=8192,
        )
