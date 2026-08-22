from __future__ import annotations

import json
import os
import sys
import types
from typing import Any

import pytest

from lambdas.command_api import handler

DEVICE = "ib-" + "a" * 32
COMMAND = "b" * 32
SUB = "00000000-0000-4000-8000-000000000001"


class FakePublisher:
    def __init__(self, *, fails: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fails = fails

    def publish(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)
        if self.fails:
            raise RuntimeError("AWS detail must stay private")


class FakeDdb:
    def __init__(
        self,
        role: str = "OWNER",
        *,
        device: dict[str, Any] | None = None,
        device_exists: bool = True,
        membership_active: bool = True,
    ) -> None:
        self.role = role
        self.device_exists = device_exists
        self.membership_active = membership_active
        self.device = device or {
            "device_id": DEVICE,
            "ownership_status": "OWNED",
            "provisioning_status": "PROVISIONED",
        }
        self.items: dict[tuple[str, str], dict[str, Any]] = {}
        self.transaction_before_publish = False

    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        key = kwargs["Key"]
        if "user_id" in key:
            if not self.membership_active:
                return {}
            return {"Item": handler._item({"status": "ACTIVE", "role": self.role})}
        if "record_key" not in key:
            return {"Item": handler._item(self.device)} if self.device_exists else {}
        record = key["record_key"]["S"]
        item = self.items.get((key["device_id"]["S"], record))
        return {"Item": item} if item else {}

    def transact_write_items(self, **kwargs: Any) -> None:
        for entry in kwargs["TransactItems"]:
            item = entry["Put"]["Item"]
            key = (item["device_id"]["S"], item["record_key"]["S"])
            if key in self.items:
                raise RuntimeError("conditional")
        for entry in kwargs["TransactItems"]:
            item = entry["Put"]["Item"]
            self.items[(item["device_id"]["S"], item["record_key"]["S"])] = item
        self.transaction_before_publish = True

    def update_item(self, **kwargs: Any) -> None:
        key = (kwargs["Key"]["device_id"]["S"], kwargs["Key"]["record_key"]["S"])
        self.items[key]["publish_state"] = {"S": "PUBLISHED"}


class FakeClientError(Exception):
    def __init__(self, code: str, reasons: list[dict[str, str]] | None = None) -> None:
        self.response: dict[str, Any] = {"Error": {"Code": code, "Message": "secret AWS text"}}
        if reasons is not None:
            self.response["CancellationReasons"] = reasons


def event(*, role_claims: bool = True, command_id: str | None = None) -> dict[str, Any]:
    claims = {"sub": SUB, "token_use": "access", "client_id": "client"} if role_claims else {}
    paths = {"device_id": DEVICE}
    if command_id is not None:
        paths["command_id"] = command_id
    return {
        "requestContext": {
            "requestId": "request-1",
            "authorizer": {"jwt": {"claims": claims}},
        },
        "pathParameters": paths,
        "headers": {},
        "body": json.dumps({"command": "OPEN_DOOR", "parameters": {}}),
    }


def body(response: dict[str, Any]) -> dict[str, Any]:
    return json.loads(response["body"])


@pytest.fixture(autouse=True)
def configured() -> None:
    os.environ.update(
        EXPECTED_APP_CLIENT_ID="client",
        DEVICES_TABLE="devices",
        MEMBERSHIPS_TABLE="memberships",
        TELEMETRY_TABLE="telemetry",
    )


def test_create_persists_before_exact_qos1_nonretained_publish() -> None:
    ddb, publisher = FakeDdb(), FakePublisher()
    response = handler.create_command(
        event(),
        None,
        clock=lambda: 1_800_000_000,
        rng=lambda _: COMMAND,
        ddb_provider=lambda: ddb,
        publisher_provider=lambda: publisher,
    )
    assert response["statusCode"] == 202
    assert ddb.transaction_before_publish
    assert publisher.calls[0]["topic"] == f"interbridge/{DEVICE}/commands"
    assert publisher.calls[0]["qos"] == 1 and publisher.calls[0]["retain"] is False
    mqtt = json.loads(publisher.calls[0]["payload"])
    assert mqtt["issued_at"] == 1_800_000_000 and mqtt["expires_at"] == 1_800_000_030


@pytest.mark.parametrize("role", ["ADMIN", "MEMBER"])
def test_create_is_owner_only(role: str) -> None:
    response = handler.create_command(
        event(),
        None,
        ddb_provider=lambda: FakeDdb(role),
        publisher_provider=lambda: FakePublisher(),
    )
    assert response["statusCode"] == 403


def test_unknown_fields_forbidden_commands_and_large_body() -> None:
    ddb, publisher = FakeDdb(), FakePublisher()
    invalid = event()
    invalid["body"] = '{"command":"FACTORY_RESET"}'
    assert (
        handler.create_command(
            invalid, None, ddb_provider=lambda: ddb, publisher_provider=lambda: publisher
        )["statusCode"]
        == 400
    )
    invalid["body"] = '{"command":"OPEN_DOOR","command_id":"' + COMMAND + '"}'
    assert (
        handler.create_command(
            invalid, None, ddb_provider=lambda: ddb, publisher_provider=lambda: publisher
        )["statusCode"]
        == 400
    )
    invalid["body"] = " " * 4097
    assert (
        handler.create_command(
            invalid, None, ddb_provider=lambda: ddb, publisher_provider=lambda: publisher
        )["statusCode"]
        == 413
    )


def test_publish_failure_is_sanitized_503_and_intent_remains() -> None:
    ddb, publisher = FakeDdb(), FakePublisher(fails=True)
    response = handler.create_command(
        event(),
        None,
        rng=lambda _: COMMAND,
        ddb_provider=lambda: ddb,
        publisher_provider=lambda: publisher,
    )
    assert response["statusCode"] == 503
    assert "AWS detail" not in response["body"]
    assert (DEVICE, f"COMMAND#{COMMAND}") in ddb.items


def test_idempotent_retry_reuses_published_command_without_republish() -> None:
    ddb, publisher = FakeDdb(), FakePublisher()
    request = event()
    request["headers"] = {"Idempotency-Key": "opaque-key"}
    first = handler.create_command(
        request,
        None,
        rng=lambda _: COMMAND,
        ddb_provider=lambda: ddb,
        publisher_provider=lambda: publisher,
    )
    second = handler.create_command(
        request,
        None,
        rng=lambda _: "c" * 32,
        ddb_provider=lambda: ddb,
        publisher_provider=lambda: publisher,
    )
    assert body(first)["command_id"] == body(second)["command_id"] == COMMAND
    assert len(publisher.calls) == 1


def test_get_maps_pending_expired_completed_and_rejected() -> None:
    ddb = FakeDdb()
    ddb.items[(DEVICE, f"COMMAND#{COMMAND}")] = handler._item(
        {
            "device_id": DEVICE,
            "record_key": f"COMMAND#{COMMAND}",
            "command_id": COMMAND,
            "command": "OPEN_DOOR",
            "issued_at": 100,
            "command_expires_at": 130,
            "expires_at": 999,
        }
    )
    pending = handler.get_command(
        event(command_id=COMMAND), None, clock=lambda: 120, ddb_provider=lambda: ddb
    )
    expired = handler.get_command(
        event(command_id=COMMAND), None, clock=lambda: 131, ddb_provider=lambda: ddb
    )
    assert body(pending)["state"] == "PENDING" and body(expired)["state"] == "EXPIRED"
    ddb.items[(DEVICE, f"COMMAND_RESULT#{COMMAND}")] = handler._item(
        {
            "device_id": DEVICE,
            "record_key": f"COMMAND_RESULT#{COMMAND}",
            "status": "COMPLETED",
            "received_at": "2026-01-01T00:00:00Z",
        }
    )
    completed = handler.get_command(event(command_id=COMMAND), None, ddb_provider=lambda: ddb)
    assert body(completed)["state"] == "COMPLETED"


@pytest.mark.parametrize(
    "claims",
    [None, [], {}, {"sub": SUB}, {"sub": SUB, "token_use": "id", "client_id": "client"}],
)
def test_missing_or_malformed_claims_are_401(claims: object) -> None:
    request = event()
    request["requestContext"]["authorizer"]["jwt"]["claims"] = claims
    response = handler.create_command(
        request, None, ddb_provider=lambda: FakeDdb(), publisher_provider=lambda: FakePublisher()
    )
    assert response["statusCode"] == 401


@pytest.mark.parametrize("paths", [None, {}, [], {"device_id": "invalid"}])
def test_missing_or_malformed_path_is_invalid_device(paths: object) -> None:
    request = event()
    request["pathParameters"] = paths
    response = handler.create_command(
        request, None, ddb_provider=lambda: FakeDdb(), publisher_provider=lambda: FakePublisher()
    )
    assert response["statusCode"] == 400


@pytest.mark.parametrize(
    "device",
    [
        {
            "device_id": "ib-" + "f" * 32,
            "ownership_status": "OWNED",
            "provisioning_status": "PROVISIONED",
        },
        {
            "device_id": DEVICE,
            "ownership_status": "DECOMMISSIONED",
            "provisioning_status": "PROVISIONED",
        },
        {"device_id": DEVICE, "ownership_status": "OWNED", "provisioning_status": "REVOKED"},
    ],
)
def test_missing_or_incompatible_device_is_safe_404(device: dict[str, Any]) -> None:
    response = handler.create_command(
        event(),
        None,
        ddb_provider=lambda: FakeDdb(device=device),
        publisher_provider=lambda: FakePublisher(),
    )
    assert response["statusCode"] == 404 and body(response)["error"]["code"] == "RESOURCE_NOT_FOUND"


@pytest.mark.parametrize(
    ("raw", "encoded", "expected"),
    [
        (None, False, 400),
        ("%%%", True, 400),
        ("/w==", True, 400),
        ("{", False, 400),
        ('{"command":"RESTART"}', False, 400),
        ('{"command":"OPEN_DOOR","parameters":{"gpio":1}}', False, 400),
    ],
)
def test_body_rejects_malformed_and_physical_details(
    raw: object, encoded: bool, expected: int
) -> None:
    request = event()
    request["body"], request["isBase64Encoded"] = raw, encoded
    response = handler.create_command(
        request, None, ddb_provider=lambda: FakeDdb(), publisher_provider=lambda: FakePublisher()
    )
    assert response["statusCode"] == expected


@pytest.mark.parametrize("key", ["", "x" * 129, "line\nbreak"])
def test_invalid_idempotency_key(key: str) -> None:
    request = event()
    request["headers"] = {"Idempotency-Key": key}
    assert (
        handler.create_command(
            request,
            None,
            ddb_provider=lambda: FakeDdb(),
            publisher_provider=lambda: FakePublisher(),
        )["statusCode"]
        == 400
    )


@pytest.mark.parametrize(
    ("reasons", "expected"),
    [
        ([{"Code": "None"}, {"Code": "ConditionalCheckFailed"}], 429),
        ([{"Code": "TransactionConflict"}, {"Code": "None"}], 503),
        ([], 503),
    ],
)
def test_transaction_cancellation_classification(
    reasons: list[dict[str, str]], expected: int
) -> None:
    class FailingDdb(FakeDdb):
        def transact_write_items(self, **kwargs: Any) -> None:
            raise FakeClientError("TransactionCanceledException", reasons)

    response = handler.create_command(
        event(), None, ddb_provider=lambda: FailingDdb(), publisher_provider=lambda: FakePublisher()
    )
    assert response["statusCode"] == expected and "secret AWS text" not in response["body"]


@pytest.mark.parametrize(
    ("code", "expected"),
    [("ThrottlingException", 503), ("AccessDeniedException", 500)],
)
def test_nontransaction_aws_classification(code: str, expected: int) -> None:
    class FailingDdb(FakeDdb):
        def transact_write_items(self, **kwargs: Any) -> None:
            raise FakeClientError(code)

    response = handler.create_command(
        event(), None, ddb_provider=lambda: FailingDdb(), publisher_provider=lambda: FakePublisher()
    )
    assert response["statusCode"] == expected and "secret AWS text" not in response["body"]


def test_pending_retry_republishes_but_expired_pending_does_not() -> None:
    ddb, publisher = FakeDdb(), FakePublisher()
    request = event()
    request["headers"] = {"Idempotency-Key": "key"}
    first = handler.create_command(
        request,
        None,
        clock=lambda: 100,
        rng=lambda _: COMMAND,
        ddb_provider=lambda: ddb,
        publisher_provider=lambda: publisher,
    )
    assert first["statusCode"] == 202
    ddb.items[(DEVICE, f"COMMAND#{COMMAND}")]["publish_state"] = {"S": "PUBLISH_PENDING"}
    assert (
        handler.create_command(
            request,
            None,
            clock=lambda: 110,
            ddb_provider=lambda: ddb,
            publisher_provider=lambda: publisher,
        )["statusCode"]
        == 202
    )
    assert len(publisher.calls) == 2
    ddb.items[(DEVICE, f"COMMAND#{COMMAND}")]["publish_state"] = {"S": "PUBLISH_PENDING"}
    assert (
        handler.create_command(
            request,
            None,
            clock=lambda: 131,
            ddb_provider=lambda: ddb,
            publisher_provider=lambda: publisher,
        )["statusCode"]
        == 202
    )
    assert len(publisher.calls) == 2


def test_rejection_is_sanitized_and_accepted_remains_pending() -> None:
    ddb = FakeDdb()
    ddb.items[(DEVICE, f"COMMAND#{COMMAND}")] = handler._item(
        {
            "device_id": DEVICE,
            "record_key": f"COMMAND#{COMMAND}",
            "command_id": COMMAND,
            "command": "OPEN_DOOR",
            "issued_at": 100,
            "command_expires_at": 130,
            "expires_at": 999,
            "publish_state": "PUBLISHED",
        }
    )
    key = (DEVICE, f"COMMAND_RESULT#{COMMAND}")
    ddb.items[key] = handler._item({"status": "ACCEPTED", "received_at": "2026-01-01T00:00:00Z"})
    assert (
        body(
            handler.get_command(
                event(command_id=COMMAND), None, clock=lambda: 110, ddb_provider=lambda: ddb
            )
        )["state"]
        == "PENDING"
    )
    for status, code in (("FAILED", "NOT_CONFIGURED"), ("REJECTED", "CAPABILITY_DISABLED")):
        ddb.items[key] = handler._item(
            {"status": status, "received_at": "2026-01-01T00:00:01Z", "error": {"code": code}}
        )
        result = body(
            handler.get_command(event(command_id=COMMAND), None, ddb_provider=lambda: ddb)
        )
        assert result["state"] == "REJECTED" and result["rejection"] == {"code": code}


def test_iot_data_client_uses_cached_data_ats_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    class Control:
        def describe_endpoint(self, **kwargs: Any) -> dict[str, str]:
            calls.append(("describe", kwargs))
            return {"endpointAddress": "example-ats.iot.sa-east-1.amazonaws.com"}

    ddb, publisher = object(), object()

    def client(name: str, **kwargs: Any) -> object:
        calls.append((name, kwargs))
        return {"dynamodb": ddb, "iot": Control(), "iot-data": publisher}[name]

    monkeypatch.setitem(sys.modules, "boto3", types.SimpleNamespace(client=client))
    monkeypatch.setattr(handler, "_ddb", None)
    monkeypatch.setattr(handler, "_publisher", None)
    assert handler._dynamodb_client() is ddb
    assert handler._iot_publisher() is publisher
    assert handler._dynamodb_client() is ddb
    assert handler._iot_publisher() is publisher
    assert calls.count(("describe", {"endpointType": "iot:Data-ATS"})) == 1
    assert (
        "iot-data",
        {"endpoint_url": "https://example-ats.iot.sa-east-1.amazonaws.com"},
    ) in calls


def test_get_initializes_only_cached_dynamodb_and_never_iot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ddb = FakeDdb()
    ddb.items[(DEVICE, f"COMMAND#{COMMAND}")] = handler._item(
        {
            "device_id": DEVICE,
            "command_id": COMMAND,
            "issued_at": 100,
            "command_expires_at": 130,
        }
    )
    calls: list[str] = []

    def client(name: str, **kwargs: Any) -> object:
        calls.append(name)
        if name != "dynamodb":
            raise AssertionError("GET attempted to initialize IoT")
        return ddb

    monkeypatch.setitem(sys.modules, "boto3", types.SimpleNamespace(client=client))
    monkeypatch.setattr(handler, "_ddb", None)
    monkeypatch.setattr(handler, "_publisher", None)

    response = handler.get_command(event(command_id=COMMAND), None, clock=lambda: 110)

    assert response["statusCode"] == 200
    assert calls == ["dynamodb"]
    assert handler._publisher is None


@pytest.mark.parametrize(
    "error",
    [
        FakeClientError("ThrottlingException"),
        FakeClientError("ServiceUnavailableException"),
        type("ConnectTimeoutError", (Exception,), {})("private endpoint"),
    ],
)
def test_temporary_endpoint_resolution_failure_is_sanitized_503(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    class Control:
        def describe_endpoint(self, **kwargs: Any) -> dict[str, str]:
            raise error

    def client(name: str, **kwargs: Any) -> object:
        return {"dynamodb": object(), "iot": Control()}[name]

    monkeypatch.setitem(sys.modules, "boto3", types.SimpleNamespace(client=client))
    monkeypatch.setattr(handler, "_ddb", None)
    monkeypatch.setattr(handler, "_publisher", None)
    response = handler.create_command(event(), None)
    assert response["statusCode"] == 503
    assert "private" not in response["body"] and "amazonaws" not in response["body"]


def test_endpoint_access_denied_is_sanitized_500(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    class Control:
        def describe_endpoint(self, **kwargs: Any) -> dict[str, str]:
            raise FakeClientError("AccessDeniedException")

    def client(name: str, **kwargs: Any) -> object:
        return {"dynamodb": object(), "iot": Control()}[name]

    monkeypatch.setitem(sys.modules, "boto3", types.SimpleNamespace(client=client))
    monkeypatch.setattr(handler, "_ddb", None)
    monkeypatch.setattr(handler, "_publisher", None)
    response = handler.create_command(event(), None)
    assert response["statusCode"] == 500 and "secret AWS text" not in response["body"]
    assert "IOT_ENDPOINT_DISCOVERY" in caplog.text
    assert "secret AWS text" not in caplog.text
    assert "amazonaws" not in caplog.text


@pytest.mark.parametrize(
    "ddb",
    [
        FakeDdb(device_exists=False),
        FakeDdb(membership_active=False),
        FakeDdb(
            device={
                "device_id": DEVICE,
                "ownership_status": "DECOMMISSIONED",
                "provisioning_status": "PROVISIONED",
            }
        ),
        FakeDdb(
            device={
                "device_id": DEVICE,
                "ownership_status": "OWNED",
                "provisioning_status": "REVOKED",
            }
        ),
    ],
    ids=["device-absent", "orphan-membership", "ownership", "provisioning"],
)
def test_get_command_requires_active_membership_and_ready_device(ddb: FakeDdb) -> None:
    response = handler.get_command(event(command_id=COMMAND), None, ddb_provider=lambda: ddb)
    assert response["statusCode"] == 404
    assert body(response)["error"]["code"] == "RESOURCE_NOT_FOUND"


def test_get_command_with_valid_device_reaches_command_lookup() -> None:
    ddb = FakeDdb()
    response = handler.get_command(event(command_id=COMMAND), None, ddb_provider=lambda: ddb)
    assert response["statusCode"] == 404
    assert body(response)["error"]["code"] == "COMMAND_NOT_FOUND"
