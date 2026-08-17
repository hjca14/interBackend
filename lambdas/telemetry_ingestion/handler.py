"""AWS Lambda entry point. Invalid input is quarantined; infrastructure errors propagate."""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from typing import Any

from domain.telemetry.models import DEVICE_ID, InvalidMessage, parse_envelope
from lambdas.telemetry_ingestion.adapter import TelemetryStore, epoch_ms

LOGGER = logging.getLogger(__name__)
LOGGER.setLevel(logging.INFO)


def _clients() -> tuple[Any, Any]:
    import boto3

    return boto3.client("dynamodb"), boto3.client("sqs")


def lambda_handler(
    event: object, context: object, *, clients: tuple[Any, Any] | None = None
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
        topic_device = envelope.get("_ib_device_id")
        category = envelope.get("_ib_category")
        received_ms = envelope.get("_ib_received_at", epoch_ms())
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
    return {"result": result}
