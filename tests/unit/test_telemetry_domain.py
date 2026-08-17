from __future__ import annotations

from datetime import UTC, datetime

import pytest

from domain.telemetry.models import InvalidMessage, parse_envelope

DEVICE = "ib-" + "a" * 32
EVENT_ID = "b" * 32
COMMAND_ID = "c" * 32
RECEIVED = 1_786_977_245_000


def envelope(category: str, **values: object) -> dict[str, object]:
    return {
        "_ib_device_id": DEVICE,
        "_ib_category": category,
        "_ib_received_at": RECEIVED,
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
            error_code="COMMAND_NOT_ALLOWED",
            timestamp="2026-08-17T14:30:26Z",
        ),
        max_payload_bytes=8192,
    )
    assert event.detail_key == f"EVENT#2026-08-17T14:30:25Z#{EVENT_ID}"
    assert response.detail_key == f"RESPONSE#2026-08-17T14:30:26Z#{COMMAND_ID}"


def test_connectivity_event_is_aggregated_only() -> None:
    message = parse_envelope(
        envelope("events", event_id=EVENT_ID, event="CONNECTED"), max_payload_bytes=8192
    )
    assert message.detail_key is None


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"protocol_version": 2}, "unsupported_protocol_version"),
        ({"device_id": "ib-" + "d" * 32}, "device_id_mismatch"),
        ({"_ib_category": "commands"}, "unexpected_category"),
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
