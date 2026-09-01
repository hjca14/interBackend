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
            "ibmeta_device_id": DEVICE,
            "ibmeta_category": "events",
            "ibmeta_received_at": 1_786_977_245_000,
            "protocol_version": 1,
            "device_id": DEVICE,
            "event_id": "evt-" + "b" * 32,
            "event": "ERROR",
        },
        max_payload_bytes=8192,
    )


def _response(status: str, received_ms: int = 1_786_977_245_000):  # type: ignore[no-untyped-def]
    from domain.telemetry.models import parse_envelope

    payload: dict[str, object] = {
        "ibmeta_device_id": DEVICE,
        "ibmeta_category": "responses",
        "ibmeta_received_at": received_ms,
        "protocol_version": 1,
        "device_id": DEVICE,
        "command_id": "b" * 32,
        "command": "OPEN_DOOR",
        "status": status,
    }
    if status in {"FAILED", "REJECTED"}:
        payload["error"] = {"code": "NOT_CONFIGURED", "message": "private firmware text"}
    return parse_envelope(payload, max_payload_bytes=8192)


class ProjectionClient(FakeClient):
    def __init__(self) -> None:
        super().__init__()
        self.projection: dict[str, object] | None = None

    def put_item(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(("put_item", kwargs))
        incoming = kwargs["Item"]  # type: ignore[index]
        if self.projection is not None:
            old_status = self.projection["status"]["S"]  # type: ignore[index]
            new_status = incoming["status"]["S"]  # type: ignore[index]
            old_time = self.projection["received_at"]["S"]  # type: ignore[index]
            new_time = incoming["received_at"]["S"]  # type: ignore[index]
            allowed = (old_status == "ACCEPTED" and old_time <= new_time) or (
                old_status != "ACCEPTED" and new_status != "ACCEPTED" and old_time < new_time
            )
            if not allowed:
                raise FakeClientError("ConditionalCheckFailedException")
        self.projection = incoming  # type: ignore[assignment]
        return {}


def test_response_projection_is_direct_and_preserves_history() -> None:
    client = ProjectionClient()
    store = TelemetryStore(client, "fictional-table", history_days=30, detail_limit=200)
    assert store.record(_response("ACCEPTED")) == "detailed"
    assert store.record(_response("COMPLETED", 1_786_977_246_000)) == "detailed"
    assert client.projection is not None
    assert client.projection["record_key"]["S"] == "COMMAND_RESULT#" + "b" * 32  # type: ignore[index]
    assert client.projection["status"]["S"] == "COMPLETED"  # type: ignore[index]
    history_puts = [
        entry["Put"]["Item"]
        for name, request in client.calls
        if name == "transact_write_items"
        for entry in request["TransactItems"]  # type: ignore[index]
        if "Put" in entry
    ]
    assert len(history_puts) == 2


def test_response_projection_rejects_duplicate_and_out_of_order_without_failing_history() -> None:
    client = ProjectionClient()
    store = TelemetryStore(client, "fictional-table", history_days=30, detail_limit=200)
    assert store.record(_response("REJECTED", 1_786_977_246_000)) == "detailed"
    assert store.record(_response("ACCEPTED", 1_786_977_247_000)) == "detailed"
    assert store.record(_response("FAILED", 1_786_977_245_000)) == "detailed"
    assert client.projection is not None
    assert client.projection["status"]["S"] == "REJECTED"  # type: ignore[index]


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
        "ibmeta_device_id": DEVICE,
        "ibmeta_category": "events",
        "ibmeta_received_at": 1_786_977_245_000,
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
            "ibmeta_device_id": DEVICE,
            "ibmeta_category": "health",
            "ibmeta_received_at": received_ms,
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
                "ibmeta_device_id": DEVICE,
                "ibmeta_category": "events",
                "ibmeta_received_at": 1_786_977_245_000,
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


def _configure_ingestion_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEMETRY_TABLE_NAME", "fictional-table")
    monkeypatch.setenv("HISTORY_DAYS", "30")
    monkeypatch.setenv("DETAIL_LIMIT", "200")
    monkeypatch.setenv("MAX_PAYLOAD_BYTES", "8192")
    monkeypatch.setenv("INVALID_QUARANTINE_QUEUE_URL", "https://example.invalid/queue")


def _ring_payload(event_id: str = "evt-" + "c" * 32) -> dict[str, object]:
    return {
        "ibmeta_device_id": DEVICE,
        "ibmeta_category": "events",
        "ibmeta_received_at": 1_786_977_245_000,
        "protocol_version": 1,
        "device_id": DEVICE,
        "event_id": event_id,
        "event": "RING_DETECTED",
        "call_id": "call-" + "c" * 32,
    }


def test_ring_detected_triggers_push_invoker_with_minimal_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_ingestion_env(monkeypatch)
    from lambdas.telemetry_ingestion.handler import lambda_handler

    dynamodb, sqs = FakeClient(), FakeClient()
    calls = []
    result = lambda_handler(
        _ring_payload(),
        None,
        clients=(dynamodb, sqs),
        push_invoker=calls.append,
    )
    assert result == {"result": "detailed"}
    assert len(calls) == 1
    message = calls[0]
    assert message.device_id == DEVICE
    assert message.identifier == "evt-" + "c" * 32
    assert message.values["event"] == "RING_DETECTED"
    assert message.values["call_id"] == "call-" + "c" * 32


@pytest.mark.parametrize("event_type", ["OFF_HOOK", "DOOR_OPENED", "ERROR"])
def test_non_ring_events_do_not_trigger_push_invoker(
    monkeypatch: pytest.MonkeyPatch, event_type: str
) -> None:
    _configure_ingestion_env(monkeypatch)
    from lambdas.telemetry_ingestion.handler import lambda_handler

    dynamodb, sqs = FakeClient(), FakeClient()
    payload = _ring_payload()
    payload["event"] = event_type
    calls = []
    lambda_handler(payload, None, clients=(dynamodb, sqs), push_invoker=calls.append)
    assert calls == []


def test_health_messages_do_not_trigger_push_invoker(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_ingestion_env(monkeypatch)
    from lambdas.telemetry_ingestion.handler import lambda_handler

    dynamodb, sqs = StatefulHealthClient(), FakeClient()
    calls = []
    lambda_handler(
        {
            "ibmeta_device_id": DEVICE,
            "ibmeta_category": "health",
            "ibmeta_received_at": 1_786_977_245_000,
            "protocol_version": 1,
            "device_id": DEVICE,
            "firmware_version": "1.0.0",
            "intercom_state": "IDLE",
            "uptime_ms": 10,
            "wifi_rssi": -50,
            "free_heap": 1000,
        },
        None,
        clients=(dynamodb, sqs),
        push_invoker=calls.append,
    )
    assert calls == []


def test_duplicate_ring_event_still_triggers_push_invoker(monkeypatch: pytest.MonkeyPatch) -> None:
    # The push sender owns its own authoritative idempotency; telemetry's
    # own "duplicate"/"dropped" ingestion result is not treated as the gate
    # here, so a retried invocation always attempts to notify -- see
    # docs/fcm-notification-sender.md for why.
    _configure_ingestion_env(monkeypatch)
    from lambdas.telemetry_ingestion.handler import lambda_handler

    dynamodb = FakeClient()
    dynamodb.cancellation_reasons = [{"Code": "ConditionalCheckFailed"}, {"Code": "None"}]
    sqs = FakeClient()
    calls = []
    result = lambda_handler(
        _ring_payload(), None, clients=(dynamodb, sqs), push_invoker=calls.append
    )
    assert result == {"result": "duplicate"}
    assert len(calls) == 1


def test_push_invoker_failure_is_swallowed_and_does_not_change_the_result(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _configure_ingestion_env(monkeypatch)
    from lambdas.telemetry_ingestion.handler import lambda_handler

    dynamodb, sqs = FakeClient(), FakeClient()

    def failing_invoker(message: object) -> None:
        raise RuntimeError("push sender unreachable, arn:aws secret detail")

    with caplog.at_level("WARNING"):
        result = lambda_handler(
            _ring_payload(), None, clients=(dynamodb, sqs), push_invoker=failing_invoker
        )
    assert result == {"result": "detailed"}
    assert "arn:aws" not in caplog.text
    assert "push_trigger_failure" in caplog.text


def test_default_push_invoker_is_a_noop_without_the_function_name_env_var(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from domain.telemetry.models import parse_envelope
    from lambdas.telemetry_ingestion.handler import _default_push_invoker

    monkeypatch.delenv("PUSH_SENDER_FUNCTION_NAME", raising=False)
    message = parse_envelope(_ring_payload(), max_payload_bytes=8192)
    _default_push_invoker(message)  # must not raise, must not need boto3/AWS


def test_default_push_invoker_invokes_the_configured_function_asynchronously(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys
    from types import ModuleType

    from domain.telemetry.models import parse_envelope
    from lambdas.telemetry_ingestion.handler import _default_push_invoker

    monkeypatch.setenv("PUSH_SENDER_FUNCTION_NAME", "interbridge-dev-push-sender")
    invocations: list[dict[str, object]] = []

    class FakeLambdaClient:
        def invoke(self, **kwargs: object) -> dict[str, object]:
            invocations.append(kwargs)
            return {}

    fake_boto3 = ModuleType("boto3")
    fake_boto3.client = lambda name: FakeLambdaClient()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "boto3", fake_boto3)

    message = parse_envelope(_ring_payload(), max_payload_bytes=8192)
    _default_push_invoker(message)

    assert len(invocations) == 1
    call = invocations[0]
    assert call["FunctionName"] == "interbridge-dev-push-sender"
    assert call["InvocationType"] == "Event"
    body = json.loads(call["Payload"])  # type: ignore[arg-type]
    assert body == {
        "schema_version": 1,
        "device_id": DEVICE,
        "event_id": "evt-" + "c" * 32,
        "event": "RING_DETECTED",
        "call_id": "call-" + "c" * 32,
        "timestamp_source": "unknown",
        "occurred_at": "2026-08-17T14:34:05Z",
    }


def test_ring_ended_triggers_same_push_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_ingestion_env(monkeypatch)
    from lambdas.telemetry_ingestion.handler import lambda_handler

    payload = _ring_payload("evt-" + "d" * 32)
    payload["event"] = "RING_ENDED"
    payload["call_id"] = "call-" + "c" * 32
    calls = []
    result = lambda_handler(
        payload, None, clients=(FakeClient(), FakeClient()), push_invoker=calls.append
    )
    assert result == {"result": "detailed"}
    assert calls[0].values == {
        "event": "RING_ENDED",
        "call_id": "call-" + "c" * 32,
        "timestamp_source": "unknown",
    }
