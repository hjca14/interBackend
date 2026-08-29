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

    def put_item(self, **kwargs: Any) -> dict[str, Any]:
        self.put_calls.append(kwargs)
        item = kwargs["Item"]
        key = (item["device_id"]["S"], item["event_id"]["S"])
        if key in self.items:
            raise ConditionalCheckFailed
        self.items[key] = item
        return {}

    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        key_attrs = kwargs["Key"]
        key = (key_attrs["device_id"]["S"], key_attrs["event_id"]["S"])
        item = self.items.get(key)
        return {"Item": item} if item else {}

    def update_item(self, **kwargs: Any) -> dict[str, Any]:
        self.update_calls.append(kwargs)
        key_attrs = kwargs["Key"]
        key = (key_attrs["device_id"]["S"], key_attrs["event_id"]["S"])
        values = kwargs["ExpressionAttributeValues"]
        item = self.items.setdefault(key, {})
        item["status"] = values[":status"]
        item["updated_at"] = values[":now"]
        for name, value in values.items():
            if name not in (":status", ":now"):
                item[name.removeprefix(":")] = value
        return {}


def test_first_processing_claims() -> None:
    ddb = FakeDdb()
    outcome = idempotency.claim(ddb, TABLE, DEVICE, EVENT_ID, now=1000)
    assert outcome == "CLAIMED"
    assert len(ddb.put_calls) == 1


def test_sequential_duplicate_after_completion_is_reported_and_not_reclaimed() -> None:
    ddb = FakeDdb()
    assert idempotency.claim(ddb, TABLE, DEVICE, EVENT_ID, now=1000) == "CLAIMED"
    idempotency.complete(ddb, TABLE, DEVICE, EVENT_ID, now=1005, counters={"sent_count": 2})
    outcome = idempotency.claim(ddb, TABLE, DEVICE, EVENT_ID, now=2000)
    assert outcome == "DUPLICATE_COMPLETED"


def test_concurrent_duplicate_while_still_processing_is_reported() -> None:
    ddb = FakeDdb()
    assert idempotency.claim(ddb, TABLE, DEVICE, EVENT_ID, now=1000) == "CLAIMED"
    outcome = idempotency.claim(ddb, TABLE, DEVICE, EVENT_ID, now=1001)
    assert outcome == "DUPLICATE_IN_FLIGHT"


def test_ttl_is_set_on_claim() -> None:
    ddb = FakeDdb()
    idempotency.claim(ddb, TABLE, DEVICE, EVENT_ID, now=1000)
    item = ddb.put_calls[0]["Item"]
    assert int(item["expires_at"]["N"]) == 1000 + idempotency.RETENTION_SECONDS


def test_complete_writes_status_and_arbitrary_counters() -> None:
    ddb = FakeDdb()
    idempotency.claim(ddb, TABLE, DEVICE, EVENT_ID, now=1000)
    idempotency.complete(
        ddb,
        TABLE,
        DEVICE,
        EVENT_ID,
        now=1050,
        counters={"sent_count": 3, "suppressed_count": 1, "invalid_token_count": 0},
    )
    key = (DEVICE, EVENT_ID)
    stored = ddb.items[key]
    assert stored["status"]["S"] == "COMPLETED"
    assert stored["sent_count"]["N"] == "3"
    assert stored["suppressed_count"]["N"] == "1"


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
    idempotency.claim(ddb, TABLE, DEVICE, EVENT_ID, now=1000)
    try:
        idempotency.complete(ddb, TABLE, DEVICE, EVENT_ID, now=1050, counters={"sent_count": 1})
        raise AssertionError("expected RuntimeError")
    except RuntimeError:
        pass
    key = (DEVICE, EVENT_ID)
    assert ddb.items[key]["status"]["S"] == idempotency.STATUS_PROCESSING
