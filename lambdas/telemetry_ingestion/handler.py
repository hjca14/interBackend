"""AWS Lambda entry point. Invalid input is quarantined; infrastructure errors propagate."""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from domain.telemetry.models import DEVICE_ID, InvalidMessage, Message, parse_envelope
from lambdas.telemetry_ingestion.adapter import TelemetryStore, epoch_ms

LOGGER = logging.getLogger(__name__)
LOGGER.setLevel(logging.INFO)

# Events that trigger a best-effort, fire-and-forget invoke of the push
# sender. These are the only event types that do today; any
# future addition here still requires the push sender itself to keep
# rejecting/ignoring anything it does not implement -- see
# lambdas/push_sender/event.py.
PUSH_TRIGGER_EVENTS = frozenset({"RING_DETECTED", "RING_ENDED"})


def _clients() -> tuple[Any, Any]:
    import boto3

    return boto3.client("dynamodb"), boto3.client("sqs")


def _default_push_invoker(message: Message) -> None:
    """Fire-and-forget async invoke of the push sender Lambda.

    Reuses this exact, already-validated ingestion invocation as the
    dispatch point (see docs/fcm-notification-sender.md) instead of adding
    a competing transport: AWS IoT Basic Ingest only invokes the single
    rule-specific Lambda a device's publish topic names, so a second,
    independent IoT Topic Rule cannot observe the same message without a
    firmware change. The push sender owns its own authoritative
    idempotency (device_id + event_id), so this never needs to be
    exactly-once -- Lambda's own asynchronous-invoke retry (this call uses
    InvocationType="Event") is an acceptable, expected source of the "AWS
    delivery may happen more than once" the push sender is built for.

    ``PUSH_SENDER_FUNCTION_NAME`` is deliberately optional: telemetry
    ingestion must keep working (and must never be retried/failed) even
    when the notification stack is not deployed yet, or is deployed
    without this Lambda's environment variable wired up.
    """
    function_name = os.environ.get("PUSH_SENDER_FUNCTION_NAME")
    if not function_name or message.identifier is None:
        return
    import boto3

    client = boto3.client("lambda")
    payload = {
        "schema_version": 1,
        "device_id": message.device_id,
        "event_id": message.identifier,
        "event": message.values.get("event"),
        "call_id": message.values.get("call_id"),
        "occurred_at": message.occurred_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    client.invoke(
        FunctionName=function_name,
        InvocationType="Event",
        Payload=json.dumps(payload, separators=(",", ":")).encode(),
    )


def lambda_handler(
    event: object,
    context: object,
    *,
    clients: tuple[Any, Any] | None = None,
    push_invoker: Callable[[Message], None] = _default_push_invoker,
) -> dict[str, str]:
    dynamodb, sqs = clients or _clients()
    store = TelemetryStore(
        dynamodb,
        os.environ["TELEMETRY_TABLE_NAME"],
        history_days=int(os.environ["HISTORY_DAYS"]),
        detail_limit=int(os.environ["DETAIL_LIMIT"]),
    )
    try:
        message = parse_envelope(event, max_payload_bytes=int(os.environ["MAX_PAYLOAD_BYTES"]))
    except InvalidMessage as error:
        envelope = event if isinstance(event, dict) else {}
        topic_device = envelope.get("ibmeta_device_id")
        category = envelope.get("ibmeta_category")
        received_ms = envelope.get("ibmeta_received_at", epoch_ms())
        received = datetime.now(UTC)
        if (
            isinstance(received_ms, int)
            and not isinstance(received_ms, bool)
            and 946_684_800_000 <= received_ms <= 4_102_444_800_000
        ):
            received = datetime.fromtimestamp(received_ms / 1000, UTC)
        safe_device = (
            topic_device
            if isinstance(topic_device, str) and DEVICE_ID.fullmatch(topic_device)
            else None
        )
        if safe_device is not None:
            store.invalid(safe_device, received)
        quarantine = {
            "reason_code": str(error),
            "category": category if category in {"events", "health", "responses"} else "unknown",
            "device_id": safe_device,
            "received_at": received.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        sqs.send_message(
            QueueUrl=os.environ["INVALID_QUARANTINE_QUEUE_URL"],
            MessageBody=json.dumps(quarantine, separators=(",", ":")),
        )
        LOGGER.warning(
            "telemetry message quarantined reason=%s category=%s", error, quarantine["category"]
        )
        return {"result": "quarantined"}
    result = store.record(message)
    LOGGER.info("telemetry processed category=%s result=%s", message.category, result)
    if message.category == "events" and message.values.get("event") in PUSH_TRIGGER_EVENTS:
        try:
            push_invoker(message)
        except Exception:
            # Best-effort: telemetry persistence already succeeded above and
            # must not be undone or retried just because the notification
            # dispatch failed. The push sender's own async-invoke retry/DLQ
            # (see infrastructure/stacks/notification_stack.py) is the
            # recovery path for a failed *push_sender* execution; a failure
            # to even start that invocation here is logged and swallowed.
            LOGGER.warning(
                json.dumps(
                    {
                        "event": "push_trigger_failure",
                        "category": message.category,
                    }
                )
            )
    return {"result": result}
