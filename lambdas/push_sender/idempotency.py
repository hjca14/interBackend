"""Adapter: authoritative idempotency for one (device_id, event_id) ring
delivery attempt, with a recoverable lease and an explicit abandon path.

Semantics (see ``docs/fcm-notification-sender.md`` for the full writeup):

- A conditional ``PutItem`` (``attribute_not_exists(device_id)``) is the
  sole authority for a brand-new event. It is a real table, not a GSI, and
  every read here uses ``ConsistentRead=True``.
- The winner claims the record (``status=PROCESSING``, ``attempt=1``,
  ``lease_expires_at``) and proceeds to fan out.
- A concurrent or retried caller that loses the initial race reads the
  existing record:
  - ``status=COMPLETED`` -> ``DUPLICATE_COMPLETED``: an earlier attempt
    finished. Do not resend, return success. Terminal, never retomada.
  - ``status=PROCESSING`` and the lease has **not** expired ->
    ``DUPLICATE_IN_FLIGHT``: another attempt is genuinely still running
    (or was deliberately abandoned -- see below -- and nothing has
    retried yet). Do not resend, return success.
  - ``status=PROCESSING`` and the lease **has** expired -> the caller
    attempts to atomically steal the lease (a conditional ``UpdateItem``
    requiring the exact ``lease_expires_at``/``attempt`` this reader just
    saw). Exactly one concurrent stealer can win that condition; the
    winner gets ``RESUMED`` and proceeds to fan out again, the loser(s)
    get ``DUPLICATE_IN_FLIGHT``.
- ``complete()`` only marks the record terminal if the caller still holds
  the lease it was given (its ``attempt`` still matches) -- if a lease
  expired and someone else already resumed by the time this attempt
  finishes late, ``complete()`` is a safe no-op instead of clobbering the
  newer attempt's state.
- ``abandon()`` is how a caller that recognizes its OWN failure as
  recoverable (a total auth/config failure, or a temporary failure that
  never resolved after local retries -- see ``handler.py``) gets out of
  the way *immediately*, instead of leaving a live-looking lease sitting
  around for up to ``LEASE_SECONDS`` while it raises. It expires the
  lease right now (same condition discipline as ``complete()``: only the
  current attempt's own abandon call can do this), so the very next
  ``claim()`` call -- however soon it arrives -- can resume without
  waiting for the lease to time out on its own. This is the fast path.
  The lease timeout itself remains the fallback for an *abrupt* crash
  (the process dies before it can call anything, including ``abandon()``)
  -- see the timing analysis below for why that fallback is still safe.
- The item's TTL (``RETENTION_SECONDS``) is unrelated to either recovery
  mechanism -- it only exists to eventually garbage collect old,
  finished-or-abandoned records, and is intentionally much longer than
  the lease.

### Lease duration vs. Lambda's real timing (not just "about a minute")

``LEASE_SECONDS`` is calibrated against two concrete numbers, not a vague
estimate:

1. push_sender's own Lambda **timeout is 20 seconds**
   (``infrastructure/stacks/notification_stack.py``). AWS enforces this as
   a hard ceiling -- no single real execution attempt can hold the lease
   "legitimately" for longer than that, regardless of cold start or slow
   downstream calls.
2. AWS Lambda's documented default behavior for a failed **asynchronous**
   invocation (exactly how ``telemetry_ingestion`` invokes
   ``push_sender``) is to retry automatically, with roughly a one-minute
   wait before the first retry and a further roughly two-minute wait
   before the second (AWS does not contractually guarantee the exact
   delay, only that it increases between attempts).

``LEASE_SECONDS = 30`` sits deliberately between those two numbers: it is
10 seconds (50%) longer than the absolute maximum a legitimate execution
can run, so a still-running attempt is never mistaken for a dead one, and
it is well under half of the ~60 second delay before AWS's *first*
automatic retry -- so even that first retry, not only the second, already
finds an abandoned/crashed lease expired and can resume immediately. The
explicit ``abandon()`` path above exists precisely so that a *recognized*
failure does not have to wait for any of this timing at all; the lease
math here only has to hold for an *abrupt*, unrecognized crash.

Explicit, documented tradeoff: exactly-once delivery to FCM cannot be
guaranteed atomically together with a DynamoDB write -- there is always a
window between "FCM accepted the message" and "this record is durably
marked complete" where a crash (or a deliberate abandon after a partial
send) forces a resumed attempt to re-run the whole fan-out, which can
re-notify an installation that was already reached. This module
deliberately chooses at-least-once-with-deduplication over any scheme
that could silently drop a ring: a rare duplicate notification is an
acceptable cost, a lost one is not.
"""

from __future__ import annotations

from typing import Any, Literal

from .dynamo import item

STATUS_PROCESSING = "PROCESSING"
STATUS_COMPLETED = "COMPLETED"

# See the module docstring's "Lease duration vs. Lambda's real timing"
# section for exactly why 30: > the function's own 20s timeout (with a
# 10s margin), and comfortably < AWS's ~60s first async-retry delay.
LEASE_SECONDS = 30
# Purely a garbage-collection horizon now -- must stay well above
# LEASE_SECONDS, but no longer needs to be short for recovery to work.
RETENTION_SECONDS = 2 * 60 * 60

ClaimOutcome = Literal["CLAIMED", "RESUMED", "DUPLICATE_COMPLETED", "DUPLICATE_IN_FLIGHT"]


def claim(
    ddb: Any, table_name: str, device_id: str, event_id: str, *, now: int
) -> tuple[ClaimOutcome, int]:
    """Returns ``(outcome, attempt)``. ``CLAIMED``/``RESUMED`` both mean
    "proceed with fan-out, then call :func:`complete` (or :func:`abandon`)
    with this exact ``attempt``"; the other two outcomes mean "do not fan
    out again".
    """
    try:
        ddb.put_item(
            TableName=table_name,
            Item=item(
                {
                    "device_id": device_id,
                    "event_id": event_id,
                    "status": STATUS_PROCESSING,
                    "attempt": 1,
                    "claimed_at": now,
                    "lease_expires_at": now + LEASE_SECONDS,
                    "updated_at": now,
                    "expires_at": now + RETENTION_SECONDS,
                }
            ),
            ConditionExpression="attribute_not_exists(device_id)",
        )
        return "CLAIMED", 1
    except Exception as error:
        if not _is_conditional_check_failure(error):
            raise

    existing = ddb.get_item(
        TableName=table_name,
        Key=item({"device_id": device_id, "event_id": event_id}),
        ConsistentRead=True,
    )
    record = existing.get("Item")
    if not record:
        # Exceedingly rare race: the item existed a moment ago (our PutItem
        # lost the condition) but is already gone by the time we read it
        # consistently -- e.g. TTL deletion landed in between. Safe to
        # treat exactly like the very first claim.
        return claim(ddb, table_name, device_id, event_id, now=now)

    status = record.get("status", {}).get("S")
    attempt = int(record.get("attempt", {}).get("N", "1"))
    if status == STATUS_COMPLETED:
        return "DUPLICATE_COMPLETED", attempt

    lease_expires_at = int(record.get("lease_expires_at", {}).get("N", "0"))
    if now < lease_expires_at:
        return "DUPLICATE_IN_FLIGHT", attempt

    # The lease has expired (naturally, or because the previous attempt
    # called abandon()): attempt to atomically steal it. The condition
    # pins both the lease timestamp and the attempt number this reader
    # just observed, so at most one concurrent stealer can win.
    new_attempt = attempt + 1
    try:
        ddb.update_item(
            TableName=table_name,
            Key=item({"device_id": device_id, "event_id": event_id}),
            UpdateExpression=(
                "SET #status = :processing, lease_expires_at = :new_lease, "
                "attempt = :new_attempt, updated_at = :now"
            ),
            ConditionExpression=(
                "#status = :processing AND lease_expires_at = :old_lease AND attempt = :old_attempt"
            ),
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues=item(
                {
                    ":processing": STATUS_PROCESSING,
                    ":new_lease": now + LEASE_SECONDS,
                    ":new_attempt": new_attempt,
                    ":now": now,
                    ":old_lease": lease_expires_at,
                    ":old_attempt": attempt,
                }
            ),
        )
        return "RESUMED", new_attempt
    except Exception as error:
        if not _is_conditional_check_failure(error):
            raise
        # Someone else won the steal (or completed it) between our read
        # and our attempt to steal it.
        return "DUPLICATE_IN_FLIGHT", attempt


def complete(
    ddb: Any,
    table_name: str,
    device_id: str,
    event_id: str,
    *,
    now: int,
    attempt: int,
    counters: dict[str, int],
    outcome: str | None = None,
) -> None:
    """Marks the record terminal, including an optional low-cardinality
    operational outcome, but only if ``attempt`` is still the
    current lease holder. If another caller already stole an expired lease
    and moved ``attempt`` forward, this is a safe no-op: two owners must
    never be able to both believe they completed the same delivery, and
    the newer attempt's own ``complete()`` call is the authoritative one.
    """
    values: dict[str, Any] = {":status": STATUS_COMPLETED, ":now": now, ":attempt": attempt}
    set_parts = ["#status = :status", "updated_at = :now"]
    if outcome is not None:
        set_parts.append("outcome = :outcome")
        values[":outcome"] = outcome
    for name, value in counters.items():
        set_parts.append(f"{name} = :{name}")
        values[f":{name}"] = value
    try:
        ddb.update_item(
            TableName=table_name,
            Key=item({"device_id": device_id, "event_id": event_id}),
            UpdateExpression="SET " + ", ".join(set_parts),
            ConditionExpression="attempt = :attempt",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues=item(values),
        )
    except Exception as error:
        if not _is_conditional_check_failure(error):
            raise


def abandon(
    ddb: Any, table_name: str, device_id: str, event_id: str, *, now: int, attempt: int
) -> None:
    """Releases the current attempt's lease immediately, without marking
    the record ``COMPLETED``, so the very next ``claim()`` -- even one
    arriving right away -- can resume rather than waiting up to
    ``LEASE_SECONDS`` for a natural expiry.

    Only takes effect if ``attempt`` is still the current lease holder
    (same ``ConditionExpression`` discipline as :func:`complete`): a call
    that arrives after this attempt has already been superseded by a
    newer ``RESUMED`` one is a safe no-op, never clobbering that newer
    attempt's in-progress or already-completed state. ``status`` stays
    ``PROCESSING`` -- this deliberately reuses the exact same "expired
    lease" recovery path :func:`claim` already implements, rather than
    introducing a third status value.
    """
    try:
        ddb.update_item(
            TableName=table_name,
            Key=item({"device_id": device_id, "event_id": event_id}),
            UpdateExpression="SET lease_expires_at = :expired, updated_at = :now",
            ConditionExpression="attempt = :attempt",
            ExpressionAttributeValues=item({":expired": now, ":now": now, ":attempt": attempt}),
        )
    except Exception as error:
        if not _is_conditional_check_failure(error):
            raise


def _is_conditional_check_failure(error: Exception) -> bool:
    response = getattr(error, "response", None)
    details = response.get("Error") if isinstance(response, dict) else None
    code = details.get("Code") if isinstance(details, dict) else None
    return code == "ConditionalCheckFailedException"
