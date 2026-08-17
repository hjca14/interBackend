from __future__ import annotations

import importlib
import json

import pytest

from lambdas.telemetry_ingestion.adapter import TelemetryStore

DEVICE = "ib-" + "a" * 32


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.cancellation_reasons: list[dict[str, str]] | None = None

    def __getattr__(self, name: str):  # type: ignore[no-untyped-def]
        assert name != "scan"

        def call(**kwargs: object) -> dict[str, object]:
            self.calls.append((name, kwargs))
            if name == "transact_write_items" and self.cancellation_reasons is not None:
                raise FakeClientError(
                    "TransactionCanceledException", reasons=self.cancellation_reasons
                )
            return {}

        return call


class FakeClientError(Exception):
    def __init__(self, code: str, *, reasons: list[dict[str, str]] | None = None) -> None:
        self.response = {"Error": {"Code": code, "Message": "safe"}}
        if reasons is not None:
            self.response["CancellationReasons"] = reasons


def _event():  # type: ignore[no-untyped-def]
    from domain.telemetry.models import parse_envelope

    return parse_envelope(
        {
            "_ib_device_id": DEVICE,
            "_ib_category": "events",
            "_ib_received_at": 1_786_977_245_000,
            "protocol_version": 1,
            "device_id": DEVICE,
            "event_id": "evt-" + "b" * 32,
            "event": "ERROR",
        },
        max_payload_bytes=8192,
    )


def test_import_does_not_create_aws_clients(monkeypatch: pytest.MonkeyPatch) -> None:
    import lambdas.telemetry_ingestion.handler as handler

    importlib.reload(handler)
    assert callable(handler.lambda_handler)


def test_duplicate_and_limit_are_successful_and_use_only_exact_keys() -> None:
    client = FakeClient()
    store = TelemetryStore(client, "fictional-table", history_days=30, detail_limit=200)
    client.cancellation_reasons = [
        {"Code": "ConditionalCheckFailed"},
        {"Code": "None"},
    ]
    assert store.record(_event()) == "duplicate"
    client.cancellation_reasons = [
        {"Code": "None"},
        {"Code": "ConditionalCheckFailed"},
    ]
    assert store.record(_event()) == "dropped"
    assert all(name != "scan" for name, _ in client.calls)
    for name, request in client.calls:
        if name in {"get_item", "update_item"}:
            assert set(request["Key"]) == {"device_id", "record_key"}  # type: ignore[arg-type]


def test_transient_failure_propagates() -> None:
    client = FakeClient()
    store = TelemetryStore(client, "fictional-table", history_days=30, detail_limit=200)

    def fail(**kwargs: object) -> None:
        raise FakeClientError("InternalServerError")

    client.transact_write_items = fail  # type: ignore[method-assign]
    with pytest.raises(FakeClientError):
        store.record(_event())


@pytest.mark.parametrize("reason", ["TransactionConflict", "ThrottlingError", "None"])
def test_transient_transaction_cancellation_propagates(reason: str) -> None:
    client = FakeClient()
    client.cancellation_reasons = [{"Code": reason}, {"Code": "None"}]
    store = TelemetryStore(client, "fictional-table", history_days=30, detail_limit=200)
    with pytest.raises(FakeClientError):
        store.record(_event())


def test_duplicate_does_not_increment_unique_event_count() -> None:
    client = FakeClient()
    client.cancellation_reasons = [
        {"Code": "ConditionalCheckFailed"},
        {"Code": "None"},
    ]
    store = TelemetryStore(client, "fictional-table", history_days=30, detail_limit=200)
    assert store.record(_event()) == "duplicate"
    update = next(request for name, request in client.calls if name == "update_item")
    assert "duplicate_count" in str(update["UpdateExpression"])
    assert "event_count" not in str(update["UpdateExpression"])


def test_invalid_handler_quarantines_only_sanitized_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEMETRY_TABLE_NAME", "fictional-table")
    monkeypatch.setenv("HISTORY_DAYS", "30")
    monkeypatch.setenv("DETAIL_LIMIT", "200")
    monkeypatch.setenv("MAX_PAYLOAD_BYTES", "8192")
    monkeypatch.setenv("INVALID_QUARANTINE_QUEUE_URL", "https://example.invalid/queue")
    dynamodb, sqs = FakeClient(), FakeClient()
    from lambdas.telemetry_ingestion.handler import lambda_handler

    payload = {
        "_ib_device_id": DEVICE,
        "_ib_category": "events",
        "_ib_received_at": 1_786_977_245_000,
        "protocol_version": 2,
        "device_id": DEVICE,
        "secret": "must-not-survive",
    }
    assert lambda_handler(payload, None, clients=(dynamodb, sqs)) == {"result": "quarantined"}
    body = next(kwargs["MessageBody"] for name, kwargs in sqs.calls if name == "send_message")
    decoded = json.loads(body)  # type: ignore[arg-type]
    assert set(decoded) == {"reason_code", "category", "device_id", "received_at"}
    assert "must-not-survive" not in body


class StatefulHealthClient(FakeClient):
    def __init__(self) -> None:
        super().__init__()
        self.last_seen: str | None = None
        self.last_state: str | None = None

    def update_item(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(("update_item", kwargs))
        key = kwargs["Key"]
        if key["record_key"] == {"S": "STATE#CURRENT"}:  # type: ignore[index]
            values = kwargs["ExpressionAttributeValues"]  # type: ignore[assignment]
            seen = values[":seen"]["S"]  # type: ignore[index]
            if self.last_seen is not None and seen < self.last_seen:
                raise FakeClientError("ConditionalCheckFailedException")
            self.last_seen = seen
            self.last_state = values[":state"]["S"]  # type: ignore[index]
        return {}


def _health(received_ms: int, state: str):  # type: ignore[no-untyped-def]
    from domain.telemetry.models import parse_envelope

    return parse_envelope(
        {
            "_ib_device_id": DEVICE,
            "_ib_category": "health",
            "_ib_received_at": received_ms,
            "protocol_version": 1,
            "device_id": DEVICE,
            "firmware_version": "1.0.0",
            "intercom_state": state,
            "uptime_ms": 10,
            "wifi_rssi": -50,
            "free_heap": 1000,
        },
        max_payload_bytes=8192,
    )


def test_health_newer_wins_and_older_or_retry_never_regresses_state() -> None:
    client = StatefulHealthClient()
    store = TelemetryStore(client, "fictional-table", history_days=30, detail_limit=200)
    newer = _health(1_786_977_245_000, "IN_CALL")
    older = _health(1_786_973_645_000, "IDLE")
    assert store.record(newer) == "state"
    assert store.record(older) == "state"
    assert client.last_state == "IN_CALL"
    assert store.record(newer) == "state"
    assert client.last_state == "IN_CALL"


class AtomicLimitClient(FakeClient):
    def __init__(self, limit: int) -> None:
        super().__init__()
        self.limit = limit
        self.details: set[str] = set()
        self.detailed_count = 0

    def transact_write_items(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(("transact_write_items", kwargs))
        items = kwargs["TransactItems"]  # type: ignore[assignment]
        detail_key = items[0]["Put"]["Item"]["record_key"]["S"]  # type: ignore[index]
        if detail_key in self.details:
            raise FakeClientError(
                "TransactionCanceledException",
                reasons=[{"Code": "ConditionalCheckFailed"}, {"Code": "None"}],
            )
        if self.detailed_count >= self.limit:
            raise FakeClientError(
                "TransactionCanceledException",
                reasons=[{"Code": "None"}, {"Code": "ConditionalCheckFailed"}],
            )
        self.details.add(detail_key)
        self.detailed_count += 1
        return {}


def test_atomic_limit_allows_200_and_counts_item_201_without_detail() -> None:
    from domain.telemetry.models import parse_envelope

    client = AtomicLimitClient(200)
    store = TelemetryStore(client, "fictional-table", history_days=30, detail_limit=200)
    results = []
    for number in range(201):
        message = parse_envelope(
            {
                "_ib_device_id": DEVICE,
                "_ib_category": "events",
                "_ib_received_at": 1_786_977_245_000,
                "protocol_version": 1,
                "device_id": DEVICE,
                "event_id": f"evt-{number:032x}",
                "event": "RING_DETECTED",
            },
            max_payload_bytes=8192,
        )
        results.append(store.record(message))
    assert results.count("detailed") == 200
    assert results[-1] == "dropped"
    assert client.detailed_count == 200
    last_update = next(request for name, request in reversed(client.calls) if name == "update_item")
    expression = str(last_update["UpdateExpression"])
    assert "event_count" in expression
    assert "detailed_dropped_count" in expression
