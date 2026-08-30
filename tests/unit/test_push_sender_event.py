from __future__ import annotations

from datetime import UTC, datetime

import pytest

from lambdas.push_sender.event import InvalidInvocation, RingEvent, parse_invocation

DEVICE = "ib-" + "a" * 32
EVENT_ID = "evt-" + "b" * 32


def valid_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": 1,
        "device_id": DEVICE,
        "event_id": EVENT_ID,
        "event": "RING_DETECTED",
        "occurred_at": "2026-08-20T12:00:00Z",
    }
    payload.update(overrides)
    return payload


def test_valid_invocation_is_parsed() -> None:
    result = parse_invocation(valid_payload())
    assert result == RingEvent(
        device_id=DEVICE,
        event_id=EVENT_ID,
        event="RING_DETECTED",
        occurred_at=datetime(2026, 8, 20, 12, 0, tzinfo=UTC),
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"schema_version": 2},
        {"schema_version": None},
        {"device_id": "not-a-device"},
        {"device_id": 123},
        {"event_id": "not-an-event-id"},
        {"event_id": None},
        {"event": "OFF_HOOK"},
        {"event": "DOOR_OPENED"},
        {"event": "UNKNOWN_EVENT"},
        {"occurred_at": "not-a-timestamp"},
        {"occurred_at": "2026-08-20T12:00:00"},  # missing Z
        {"occurred_at": "2026-08-20T12:00:00+02:00"},  # not UTC
        {"occurred_at": None},
    ],
)
def test_invalid_fields_are_rejected(overrides: dict[str, object]) -> None:
    with pytest.raises(InvalidInvocation):
        parse_invocation(valid_payload(**overrides))


def test_non_dict_payload_is_rejected() -> None:
    with pytest.raises(InvalidInvocation):
        parse_invocation("not-a-dict")
    with pytest.raises(InvalidInvocation):
        parse_invocation(None)
    with pytest.raises(InvalidInvocation):
        parse_invocation([1, 2, 3])


def test_extra_fields_are_rejected() -> None:
    with pytest.raises(InvalidInvocation):
        parse_invocation(valid_payload(injected_user_id="attacker-controlled"))


def test_device_reported_fields_cannot_smuggle_recipients_or_tokens() -> None:
    # Even if a caller tried to inject these, the parsed RingEvent has no
    # such fields to carry them -- validated structurally, not just by
    # convention.
    malicious = valid_payload()
    malicious["user_id"] = "attacker"
    malicious["token"] = "attacker-token"
    with pytest.raises(InvalidInvocation):
        parse_invocation(malicious)
    assert not hasattr(RingEvent, "user_id")
    assert not hasattr(RingEvent, "token")
