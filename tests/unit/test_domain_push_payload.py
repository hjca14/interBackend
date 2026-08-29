from __future__ import annotations

import pytest

from domain.push.payload import compose_message

TOKEN = "fictional-fcm-token"
DEVICE = "ib-" + "a" * 32
EVENT_ID = "evt-" + "b" * 32


def base_kwargs(**overrides: object) -> dict[str, object]:
    kwargs: dict[str, object] = {
        "token": TOKEN,
        "device_id": DEVICE,
        "event_id": EVENT_ID,
        "event": "RING_DETECTED",
        "presentation_intent": "RING_AND_NOTIFICATION",
        "occurred_at": "2026-08-20T12:00:00Z",
    }
    kwargs.update(overrides)
    return kwargs


def test_message_shape_is_minimal_and_versioned() -> None:
    body = compose_message(**base_kwargs())
    message = body["message"]
    assert message["token"] == TOKEN
    assert message["data"] == {
        "push_contract_version": "1",
        "event_id": EVENT_ID,
        "device_id": DEVICE,
        "event": "RING_DETECTED",
        "presentation_intent": "RING_AND_NOTIFICATION",
        "occurred_at": "2026-08-20T12:00:00Z",
    }


def test_no_top_level_notification_block() -> None:
    body = compose_message(**base_kwargs())
    assert "notification" not in body["message"]


def test_android_priority_and_ttl_are_set() -> None:
    body = compose_message(**base_kwargs())
    android = body["message"]["android"]
    assert android["priority"] == "high"
    assert android["ttl"].endswith("s")


@pytest.mark.parametrize("intent", ["RING_ONLY", "NOTIFICATION_ONLY", "RING_AND_NOTIFICATION"])
def test_presentation_intent_reuses_existing_alert_mode_vocabulary(intent: str) -> None:
    body = compose_message(**base_kwargs(presentation_intent=intent))
    assert body["message"]["data"]["presentation_intent"] == intent


def test_no_membership_email_or_internal_identifiers_leak_into_the_payload() -> None:
    body = compose_message(**base_kwargs())
    serialized = str(body)
    for forbidden in ("user_id", "membership", "email", "@", "installation_id", "role"):
        assert forbidden not in serialized


def test_payload_never_contains_device_supplied_free_text() -> None:
    # The only device-derived values ever included are the already-
    # validated, enum-shaped event/device_id/event_id/occurred_at -- never
    # arbitrary strings the firmware could have supplied as a title/body.
    body = compose_message(**base_kwargs())
    data = body["message"]["data"]
    assert set(data) == {
        "push_contract_version",
        "event_id",
        "device_id",
        "event",
        "presentation_intent",
        "occurred_at",
    }
