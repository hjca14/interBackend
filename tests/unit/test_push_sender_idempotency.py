from __future__ import annotations

from typing import Any

from lambdas.push_sender import idempotency

TABLE = "deliveries"
DEVICE = "ib-" + "a" * 32
EVENT_ID = "evt-" + "b" * 32


class ConditionalCheckFailed(Exception):
    response = {"Error": {"Code": "ConditionalCheckFailedException"}}


class FakeDdb:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict[str, Any]] = {}
        self.put_calls: list[dict[str, Any]] = []
        self.update_calls: list[dict[str, Any]] = []
        self.get_calls: list[dict[str, Any]] = []

    def put_item(self, **kwargs: Any) -> dict[str, Any]:
        self.put_calls.append(kwargs)
        item = kwargs["Item"]
        key = (item["device_id"]["S"], item["event_id"]["S"])
        if key in self.items:
            raise ConditionalCheckFailed
        self.items[key] = item
        return {}

    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        self.get_calls.append(kwargs)
        key_attrs = kwargs["Key"]
        key = (key_attrs["device_id"]["S"], key_attrs["event_id"]["S"])
        item = self.items.get(key)
        return {"Item": item} if item else {}

    def update_item(self, **kwargs: Any) -> dict[str, Any]:
        self.update_calls.append(kwargs)
        key_attrs = kwargs["Key"]
        key = (key_attrs["device_id"]["S"], key_attrs["event_id"]["S"])
        current = self.items.get(key)
        condition = kwargs.get("ConditionExpression", "")
        values = kwargs["ExpressionAttributeValues"]
        item = self.items.setdefault(key, {})

        if "lease_expires_at" in condition:
            # The lease-steal update: SET status/lease_expires_at/attempt/
            # updated_at, conditioned on the exact prior lease+attempt.
            if current is None or (
                current.get("status", {}).get("S") != values[":processing"]["S"]
                or current.get("lease_expires_at", {}).get("N") != values[":old_lease"]["N"]
                or current.get("attempt", {}).get("N") != values[":old_attempt"]["N"]
            ):
                raise ConditionalCheckFailed
            item["status"] = values[":processing"]
            item["lease_expires_at"] = values[":new_lease"]
            item["attempt"] = values[":new_attempt"]
            item["updated_at"] = values[":now"]
        else:
            # complete(): SET status/updated_at/<arbitrary counters>,
            # conditioned only on the attempt still matching.
            if condition and (
                current is None or current.get("attempt", {}).get("N") != values[":attempt"]["N"]
            ):
                raise ConditionalCheckFailed
            item["status"] = values[":status"]
            item["updated_at"] = values[":now"]
            for name, value in values.items():
                bare = name.removeprefix(":")
                if bare not in {"status", "now", "attempt"}:
                    item[bare] = value
        return {}


def test_first_claim_acquires_with_attempt_one() -> None:
    ddb = FakeDdb()
    outcome, attempt = idempotency.claim(ddb, TABLE, DEVICE, EVENT_ID, now=1000)
    assert outcome == "CLAIMED"
    assert attempt == 1
    assert len(ddb.put_calls) == 1
    item = ddb.put_calls[0]["Item"]
    assert item["lease_expires_at"]["N"] == str(1000 + idempotency.LEASE_SECONDS)
    assert item["expires_at"]["N"] == str(1000 + idempotency.RETENTION_SECONDS)


def test_duplicate_after_completion_is_reported_and_never_reclaimed() -> None:
    ddb = FakeDdb()
    outcome, attempt = idempotency.claim(ddb, TABLE, DEVICE, EVENT_ID, now=1000)
    idempotency.complete(
        ddb, TABLE, DEVICE, EVENT_ID, now=1005, attempt=attempt, counters={"sent_count": 2}
    )
    outcome, _ = idempotency.claim(ddb, TABLE, DEVICE, EVENT_ID, now=2000)
    assert outcome == "DUPLICATE_COMPLETED"


def test_concurrent_claim_while_lease_is_still_valid_is_reported() -> None:
    ddb = FakeDdb()
    idempotency.claim(ddb, TABLE, DEVICE, EVENT_ID, now=1000)
    outcome, attempt = idempotency.claim(ddb, TABLE, DEVICE, EVENT_ID, now=1001)
    assert outcome == "DUPLICATE_IN_FLIGHT"
    assert attempt == 1


def test_resume_after_lease_expiry_bumps_the_attempt() -> None:
    ddb = FakeDdb()
    idempotency.claim(ddb, TABLE, DEVICE, EVENT_ID, now=1000)
    past_expiry = 1000 + idempotency.LEASE_SECONDS + 1
    outcome, attempt = idempotency.claim(ddb, TABLE, DEVICE, EVENT_ID, now=past_expiry)
    assert outcome == "RESUMED"
    assert attempt == 2
    key = (DEVICE, EVENT_ID)
    assert ddb.items[key]["lease_expires_at"]["N"] == str(past_expiry + idempotency.LEASE_SECONDS)


def test_only_one_concurrent_resumer_wins_the_stolen_lease() -> None:
    # Two readers race after the lease expires and both observe the exact
    # same stale (lease_expires_at, attempt) snapshot -- proves the
    # DynamoDB-level conditional update, not just claim()'s sequential
    # black-box behavior, is what prevents a double win.
    ddb = FakeDdb()
    idempotency.claim(ddb, TABLE, DEVICE, EVENT_ID, now=1000)
    past_expiry = 1000 + idempotency.LEASE_SECONDS + 1

    first_outcome, first_attempt = idempotency.claim(ddb, TABLE, DEVICE, EVENT_ID, now=past_expiry)
    assert first_outcome == "RESUMED"
    assert first_attempt == 2

    # Replay the exact steal request the first (winning) reader issued --
    # this is what a second reader who observed the identical stale
    # snapshot before the first writer landed would also issue.
    stale_steal_kwargs = ddb.update_calls[0]
    try:
        ddb.update_item(**stale_steal_kwargs)
        raise AssertionError("expected the second, stale steal attempt to be rejected")
    except ConditionalCheckFailed:
        pass

    # The record reflects only the winner's attempt, never a second bump.
    key = (DEVICE, EVENT_ID)
    assert ddb.items[key]["attempt"]["N"] == "2"


def test_crash_before_first_send_leaves_a_processing_record_recoverable_after_lease() -> None:
    ddb = FakeDdb()
    outcome, attempt = idempotency.claim(ddb, TABLE, DEVICE, EVENT_ID, now=1000)
    assert outcome == "CLAIMED"
    # Simulated crash: complete() is never called.
    key = (DEVICE, EVENT_ID)
    assert ddb.items[key]["status"]["S"] == idempotency.STATUS_PROCESSING

    # Immediately retried (e.g. a fast Lambda async retry): still in-flight.
    retry_outcome, _ = idempotency.claim(ddb, TABLE, DEVICE, EVENT_ID, now=1005)
    assert retry_outcome == "DUPLICATE_IN_FLIGHT"

    # Retried again after the lease has expired: recoverable.
    late_outcome, late_attempt = idempotency.claim(
        ddb, TABLE, DEVICE, EVENT_ID, now=1000 + idempotency.LEASE_SECONDS + 1
    )
    assert late_outcome == "RESUMED"
    assert late_attempt == 2


def test_crash_after_partial_send_still_recovers_and_a_stale_complete_is_a_no_op() -> None:
    ddb = FakeDdb()
    outcome, attempt = idempotency.claim(ddb, TABLE, DEVICE, EVENT_ID, now=1000)
    # Simulated partial progress: some installations were sent before the
    # crash, but complete() for THIS attempt never runs.

    # A resumed attempt takes over after the lease expires.
    resumed_outcome, resumed_attempt = idempotency.claim(
        ddb, TABLE, DEVICE, EVENT_ID, now=1000 + idempotency.LEASE_SECONDS + 1
    )
    assert resumed_outcome == "RESUMED"
    assert resumed_attempt == attempt + 1

    # The resumed attempt finishes and completes first.
    idempotency.complete(
        ddb,
        TABLE,
        DEVICE,
        EVENT_ID,
        now=2000,
        attempt=resumed_attempt,
        counters={"sent_count": 3},
    )
    key = (DEVICE, EVENT_ID)
    assert ddb.items[key]["status"]["S"] == idempotency.STATUS_COMPLETED

    # The original, now-stale attempt finally wakes up (e.g. a very slow
    # crashed process) and tries to complete with its own old attempt
    # number -- must not clobber the resumed attempt's COMPLETED state.
    idempotency.complete(
        ddb, TABLE, DEVICE, EVENT_ID, now=2100, attempt=attempt, counters={"sent_count": 1}
    )
    assert ddb.items[key]["status"]["S"] == idempotency.STATUS_COMPLETED
    assert ddb.items[key]["sent_count"]["N"] == "3"  # unchanged by the stale completion


def test_retry_after_authentication_failure_recovers_via_the_same_lease_mechanism() -> None:
    # No AWS-specific behavior here (that's handler.py's job) -- this just
    # confirms the idempotency primitive itself treats "the previous
    # attempt raised before calling complete()" uniformly, regardless of
    # *why* it never completed.
    ddb = FakeDdb()
    outcome, attempt = idempotency.claim(ddb, TABLE, DEVICE, EVENT_ID, now=1000)
    assert outcome == "CLAIMED"
    # The handler would raise FirebaseCredentialError here and never call
    # complete() -- simulated by simply not calling it.
    retry_outcome, retry_attempt = idempotency.claim(
        ddb, TABLE, DEVICE, EVENT_ID, now=1000 + idempotency.LEASE_SECONDS + 1
    )
    assert retry_outcome == "RESUMED"
    assert retry_attempt == 2


def test_ttl_retention_is_independent_of_and_much_longer_than_the_lease() -> None:
    ddb = FakeDdb()
    idempotency.claim(ddb, TABLE, DEVICE, EVENT_ID, now=1000)
    key = (DEVICE, EVENT_ID)
    lease_expires_at = int(ddb.items[key]["lease_expires_at"]["N"])
    ttl_expires_at = int(ddb.items[key]["expires_at"]["N"])
    assert lease_expires_at == 1000 + idempotency.LEASE_SECONDS
    assert ttl_expires_at == 1000 + idempotency.RETENTION_SECONDS
    assert idempotency.RETENTION_SECONDS > idempotency.LEASE_SECONDS * 10


def test_complete_writes_status_and_arbitrary_counters() -> None:
    ddb = FakeDdb()
    _, attempt = idempotency.claim(ddb, TABLE, DEVICE, EVENT_ID, now=1000)
    idempotency.complete(
        ddb,
        TABLE,
        DEVICE,
        EVENT_ID,
        now=1050,
        attempt=attempt,
        counters={"sent_count": 3, "suppressed_count": 1, "invalid_token_count": 0},
    )
    key = (DEVICE, EVENT_ID)
    stored = ddb.items[key]
    assert stored["status"]["S"] == "COMPLETED"
    assert stored["sent_count"]["N"] == "3"
    assert stored["suppressed_count"]["N"] == "1"


def test_item_vanishing_between_the_failed_put_and_the_consistent_read_is_treated_as_fresh() -> (
    None
):
    # Exceedingly rare race: attribute_not_exists(device_id) fails (someone
    # else's item existed a moment ago) but the item is already gone by
    # the time claim() does its consistent read (e.g. an in-flight TTL
    # deletion). claim() must recover by treating this exactly like a
    # brand-new claim, not raise or silently do nothing.
    ddb = FakeDdb()
    real_put_item = ddb.put_item
    calls = {"count": 0}

    def flaky_put_item(**kwargs: Any) -> dict[str, Any]:
        calls["count"] += 1
        if calls["count"] == 1:
            raise ConditionalCheckFailed
        return real_put_item(**kwargs)

    ddb.put_item = flaky_put_item  # type: ignore[method-assign]
    outcome, attempt = idempotency.claim(ddb, TABLE, DEVICE, EVENT_ID, now=1000)
    assert outcome == "CLAIMED"
    assert attempt == 1
    assert calls["count"] == 2


def test_losing_a_concurrent_steal_inside_claim_is_reported_as_in_flight() -> None:
    # Distinct from test_only_one_concurrent_resumer_wins_the_stolen_lease
    # (which replays the raw DynamoDB call): this exercises claim()'s own
    # exception handling around a losing steal attempt.
    ddb = FakeDdb()
    idempotency.claim(ddb, TABLE, DEVICE, EVENT_ID, now=1000)
    past_expiry = 1000 + idempotency.LEASE_SECONDS + 1

    real_update_item = ddb.update_item

    def racing_update_item(**kwargs: Any) -> dict[str, Any]:
        raise ConditionalCheckFailed

    ddb.update_item = racing_update_item  # type: ignore[method-assign]
    outcome, attempt = idempotency.claim(ddb, TABLE, DEVICE, EVENT_ID, now=past_expiry)
    assert outcome == "DUPLICATE_IN_FLIGHT"
    assert attempt == 1

    ddb.update_item = real_update_item  # type: ignore[method-assign]


def test_infrastructure_failure_before_claim_propagates() -> None:
    class BrokenDdb(FakeDdb):
        def put_item(self, **kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("dependency unavailable")

    ddb = BrokenDdb()
    try:
        idempotency.claim(ddb, TABLE, DEVICE, EVENT_ID, now=1000)
        raise AssertionError("expected RuntimeError")
    except RuntimeError:
        pass


def test_infrastructure_failure_on_complete_propagates_and_leaves_record_processing() -> None:
    class BrokenCompleteDdb(FakeDdb):
        def update_item(self, **kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("dependency unavailable")

    ddb = BrokenCompleteDdb()
    _, attempt = idempotency.claim(ddb, TABLE, DEVICE, EVENT_ID, now=1000)
    try:
        idempotency.complete(
            ddb, TABLE, DEVICE, EVENT_ID, now=1050, attempt=attempt, counters={"sent_count": 1}
        )
        raise AssertionError("expected RuntimeError")
    except RuntimeError:
        pass
    key = (DEVICE, EVENT_ID)
    assert ddb.items[key]["status"]["S"] == idempotency.STATUS_PROCESSING
