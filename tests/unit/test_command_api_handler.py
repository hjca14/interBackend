from __future__ import annotations

import json
import os
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
    def __init__(self, role: str = "OWNER") -> None:
        self.role = role
        self.items: dict[tuple[str, str], dict[str, Any]] = {}
        self.transaction_before_publish = False

    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        key = kwargs["Key"]
        if "user_id" in key:
            return {"Item": handler._item({"status": "ACTIVE", "role": self.role})}
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

    def query(self, **kwargs: Any) -> dict[str, Any]:
        return {
            "Items": [
                item
                for (device, record), item in self.items.items()
                if device == DEVICE and record.startswith("RESPONSE#")
            ]
        }


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
        clients=lambda: (ddb, publisher),
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
        event(), None, clients=lambda: (FakeDdb(role), FakePublisher())
    )
    assert response["statusCode"] == 403


def test_unknown_fields_forbidden_commands_and_large_body() -> None:
    ddb, publisher = FakeDdb(), FakePublisher()
    invalid = event()
    invalid["body"] = '{"command":"FACTORY_RESET"}'
    assert (
        handler.create_command(invalid, None, clients=lambda: (ddb, publisher))["statusCode"] == 400
    )
    invalid["body"] = '{"command":"OPEN_DOOR","command_id":"' + COMMAND + '"}'
    assert (
        handler.create_command(invalid, None, clients=lambda: (ddb, publisher))["statusCode"] == 400
    )
    invalid["body"] = " " * 4097
    assert (
        handler.create_command(invalid, None, clients=lambda: (ddb, publisher))["statusCode"] == 413
    )


def test_publish_failure_is_sanitized_503_and_intent_remains() -> None:
    ddb, publisher = FakeDdb(), FakePublisher(fails=True)
    response = handler.create_command(
        event(), None, rng=lambda _: COMMAND, clients=lambda: (ddb, publisher)
    )
    assert response["statusCode"] == 503
    assert "AWS detail" not in response["body"]
    assert (DEVICE, f"COMMAND#{COMMAND}") in ddb.items


def test_idempotent_retry_reuses_and_republishes_command_id() -> None:
    ddb, publisher = FakeDdb(), FakePublisher()
    request = event()
    request["headers"] = {"Idempotency-Key": "opaque-key"}
    first = handler.create_command(
        request, None, rng=lambda _: COMMAND, clients=lambda: (ddb, publisher)
    )
    second = handler.create_command(
        request, None, rng=lambda _: "c" * 32, clients=lambda: (ddb, publisher)
    )
    assert body(first)["command_id"] == body(second)["command_id"] == COMMAND
    assert len(publisher.calls) == 2


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
        event(command_id=COMMAND), None, clock=lambda: 120, clients=lambda: (ddb, None)
    )
    expired = handler.get_command(
        event(command_id=COMMAND), None, clock=lambda: 131, clients=lambda: (ddb, None)
    )
    assert body(pending)["state"] == "PENDING" and body(expired)["state"] == "EXPIRED"
    ddb.items[(DEVICE, f"RESPONSE#2026-01-01T00:00:00Z#{COMMAND}")] = handler._item(
        {
            "device_id": DEVICE,
            "record_key": f"RESPONSE#2026-01-01T00:00:00Z#{COMMAND}",
            "status": "COMPLETED",
            "received_at": "2026-01-01T00:00:00Z",
        }
    )
    completed = handler.get_command(event(command_id=COMMAND), None, clients=lambda: (ddb, None))
    assert body(completed)["state"] == "COMPLETED"
