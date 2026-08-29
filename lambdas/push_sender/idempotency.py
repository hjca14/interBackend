"""Adapter: authoritative, atomic idempotency for one (device_id, event_id)
ring delivery attempt.

Semantics (see ``docs/fcm-notification-sender.md`` for the full writeup):

- A conditional ``PutItem`` (``attribute_not_exists(device_id)``) is the
  sole authority. It is a real table, not a GSI, and every read here uses
  ``ConsistentRead=True``.
- The first caller to win the conditional write claims the record
  (``status=PROCESSING``) and proceeds to fan out.
- Any concurrent or retried caller that loses the race is told
  ``DUPLICATE_COMPLETED`` (the earlier attempt already finished -- do not
  resend, return success) or ``DUPLICATE_IN_FLIGHT`` (an earlier attempt
  claimed this event and has not finished yet -- also do not resend).
- ``complete()`` is only ever called after a fan-out attempt has fully
  run (not necessarily fully *succeeded* -- see the handler for exactly
  when it is/is not called), so a genuinely crashed mid-fan-out attempt
  is deliberately never marked ``COMPLETED``.
- Recovery from a crashed ``PROCESSING`` record relies entirely on the
  item's TTL (``RETENTION_SECONDS``), not on an active "steal a stale
  claim" code path -- this keeps the idempotency logic to a single simple
  conditional write plus one conditional update, at the cost of a
  crashed attempt's event being unrecoverable until the TTL clears it.
"""

from __future__ import annotations

from typing import Any, Literal

from .dynamo import item

STATUS_PROCESSING = "PROCESSING"
STATUS_COMPLETED = "COMPLETED"
# Long enough to absorb realistic AWS-level redelivery (Lambda async
# invoke retries happen within minutes), short enough that a crashed
# mid-fan-out attempt self-heals in a bounded, documented window rather
# than blocking that event_id forever.
RETENTION_SECONDS = 2 * 60 * 60

ClaimOutcome = Literal["CLAIMED", "DUPLICATE_COMPLETED", "DUPLICATE_IN_FLIGHT"]


def claim(ddb: Any, table_name: str, device_id: str, event_id: str, *, now: int) -> ClaimOutcome:
    try:
        ddb.put_item(
            TableName=table_name,
            Item=item(
                {
                    "device_id": device_id,
                    "event_id": event_id,
                    "status": STATUS_PROCESSING,
                    "claimed_at": now,
                    "updated_at": now,
                    "expires_at": now + RETENTION_SECONDS,
                }
            ),
            ConditionExpression="attribute_not_exists(device_id)",
        )
        return "CLAIMED"
    except Exception as error:
        if not _is_conditional_check_failure(error):
            raise
        existing = ddb.get_item(
            TableName=table_name,
            Key=item({"device_id": device_id, "event_id": event_id}),
            ConsistentRead=True,
        )
        status = existing.get("Item", {}).get("status", {}).get("S")
        return "DUPLICATE_COMPLETED" if status == STATUS_COMPLETED else "DUPLICATE_IN_FLIGHT"


def complete(
    ddb: Any,
    table_name: str,
    device_id: str,
    event_id: str,
    *,
    now: int,
    counters: dict[str, int],
) -> None:
    values: dict[str, Any] = {":status": STATUS_COMPLETED, ":now": now}
    set_parts = ["#status = :status", "updated_at = :now"]
    for name, value in counters.items():
        set_parts.append(f"{name} = :{name}")
        values[f":{name}"] = value
    ddb.update_item(
        TableName=table_name,
        Key=item({"device_id": device_id, "event_id": event_id}),
        UpdateExpression="SET " + ", ".join(set_parts),
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues=item(values),
    )


def _is_conditional_check_failure(error: Exception) -> bool:
    response = getattr(error, "response", None)
    details = response.get("Error") if isinstance(response, dict) else None
    code = details.get("Code") if isinstance(details, dict) else None
    return code == "ConditionalCheckFailedException"
