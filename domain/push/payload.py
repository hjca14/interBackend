"""Pure composition of the FCM HTTP v1 message body for a RING_DETECTED
delivery decision. No AWS, no network -- see
``docs/fcm-notification-sender.md`` for the full payload contract this
implements.
"""

from __future__ import annotations

from typing import Any

PUSH_CONTRACT_VERSION = 1
# A ring is only meaningful for a short window; FCM drops the message
# instead of holding and redelivering it once it has gone stale.
MESSAGE_TTL_SECONDS = 30


def compose_message(
    *,
    token: str,
    device_id: str,
    event_id: str,
    event: str,
    presentation_intent: str,
    occurred_at: str,
) -> dict[str, Any]:
    """Build one FCM HTTP v1 request body (``{"message": {...}}``).

    Deliberately data-only (no top-level ``notification`` block): the
    visual/sound presentation for a ring is Fase 3B.9's job, not this
    delivery's, and the app-side handling for a generic "someone rang"
    notification has not been designed either -- see "Limitações
    conhecidas" in ``docs/fcm-notification-sender.md``. This composer only
    guarantees the *intent* is delivered, versioned and documented.

    Only these fields are ever included: no push token appears outside
    ``message.token`` (which FCM itself requires to address the device),
    no membership/email/internal identifier, and nothing sourced from the
    device's own MQTT payload beyond the already-validated ``event``/
    ``event_id``/``device_id``/``occurred_at`` values the caller passes in.
    """
    return {
        "message": {
            "token": token,
            "data": {
                "push_contract_version": str(PUSH_CONTRACT_VERSION),
                "event_id": event_id,
                "device_id": device_id,
                "event": event,
                "presentation_intent": presentation_intent,
                "occurred_at": occurred_at,
            },
            "android": {
                "priority": "high",
                "ttl": f"{MESSAGE_TTL_SECONDS}s",
            },
        }
    }
