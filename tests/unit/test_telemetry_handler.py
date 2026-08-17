from __future__ import annotations

import importlib
import json

import pytest

from lambdas.telemetry_ingestion.adapter import TelemetryStore

DEVICE = "ib-" + "a" * 32


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.item_exists = False
        self.cancel_transaction = False

    def __getattr__(self, name: str):  # type: ignore[no-untyped-def]
        assert name != "scan"

        def call(**kwargs: object) -> dict[str, object]:
            self.calls.append((name, kwargs))
            if name == "transact_write_items" and self.cancel_transaction:
                raise FakeClientError("TransactionCanceledException")
            if name == "get_item" and self.item_exists:
                return {"Item": {"device_id": {"S": DEVICE}}}
            return {}

        return call


class FakeClientError(Exception):
    def __init__(self, code: str) -> None:
        self.response = {"Error": {"Code": code, "Message": "safe"}}


def _event():  # type: ignore[no-untyped-def]
    from domain.telemetry.models import parse_envelope

    return parse_envelope(
        {
            "_ib_device_id": DEVICE,
            "_ib_category": "events",
            "_ib_received_at": 1_786_977_245_000,
            "protocol_version": 1,
            "device_id": DEVICE,
            "event_id": "b" * 32,
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
    client.cancel_transaction = True
    client.item_exists = True
    assert store.record(_event()) == "duplicate"
    client.item_exists = False
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


def test_invalid_handler_quarantines_only_sanitized_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEMETRY_TABLE_NAME", "fictional-table")
    monkeypatch.setenv("HISTORY_DAYS", "30")
    monkeypatch.setenv("DETAIL_LIMIT", "200")
    monkeypatch.setenv("MAX_PAYLOAD_BYTES", "8192")
    monkeypatch.setenv("QUARANTINE_QUEUE_URL", "https://example.invalid/queue")
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
    assert set(decoded) == {"reason", "category", "device_id", "received_at"}
    assert "must-not-survive" not in body
