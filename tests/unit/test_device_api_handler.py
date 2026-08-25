from __future__ import annotations

import json
import os
from typing import Any

import pytest

from lambdas.device_api import handler

DEVICE = "ib-" + "a" * 32
SUB = "00000000-0000-4000-8000-000000000001"


class FakeClientError(Exception):
    def __init__(self, code: str) -> None:
        self.response: dict[str, Any] = {"Error": {"Code": code, "Message": "secret AWS text"}}


class FakeDdb:
    def __init__(
        self,
        *,
        role: str | None = "OWNER",
        membership_active: bool = True,
        device: dict[str, Any] | None = None,
        device_exists: bool = True,
        update_error: Exception | None = None,
        membership_error: Exception | None = None,
    ) -> None:
        self.role = role
        self.membership_active = membership_active
        self.device_exists = device_exists
        self.membership_error = membership_error
        self.membership: dict[str, Any] = {
            "device_id": DEVICE,
            "user_id": SUB,
            "status": "ACTIVE" if membership_active else "REMOVED",
            "role": role or "UNKNOWN",
            "created_at": 1_700_000_000,
            "updated_at": 1_700_000_000,
        }
        self.device: dict[str, Any] = device or {
            "device_id": DEVICE,
            "ownership_status": "OWNED",
            "provisioning_status": "PROVISIONED",
            "created_at": 1_700_000_000,
            "updated_at": 1_700_000_000,
        }
        self.update_error = update_error
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("get_item", kwargs))
        if kwargs["TableName"] == "memberships" and self.membership_error is not None:
            raise self.membership_error
        if kwargs["TableName"] == "devices":
            return {"Item": handler._item(self.device)} if self.device_exists else {}
        return {"Item": handler._item(self.membership)} if self.membership_active else {}

    def update_item(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("update_item", kwargs))
        if self.update_error is not None:
            raise self.update_error
        if not self.membership_active or self.role not in handler.ROLES:
            raise FakeClientError("ConditionalCheckFailedException")
        values = handler._plain(kwargs["ExpressionAttributeValues"])
        if "dn" in values:
            self.membership["display_name"] = values["dn"]
        else:
            self.membership.pop("display_name", None)
        self.membership["updated_at"] = values["now"]
        return {"Attributes": handler._item(self.membership)}


def event(
    *,
    sub: str | None = SUB,
    device: str | None = DEVICE,
    body: dict[str, Any] | None | str = "__default__",
) -> dict[str, Any]:
    claims = {"sub": sub, "token_use": "access", "client_id": "client"} if sub is not None else {}
    value: dict[str, Any] = {
        "requestContext": {"requestId": "request-1", "authorizer": {"jwt": {"claims": claims}}},
    }
    if device is not None:
        value["pathParameters"] = {"device_id": device}
    if body != "__default__":
        value["body"] = body if isinstance(body, str) or body is None else json.dumps(body)
    else:
        value["body"] = json.dumps({"display_name": "Minha casa"})
    return value


def body(response: dict[str, Any]) -> dict[str, Any]:
    return json.loads(response["body"])


@pytest.fixture(autouse=True)
def configured() -> None:
    os.environ.update(
        EXPECTED_APP_CLIENT_ID="client",
        DEVICES_TABLE="devices",
        MEMBERSHIPS_TABLE="memberships",
    )


def test_owner_can_set_display_name() -> None:
    ddb = FakeDdb()
    response = handler.update_device_name(
        event(body={"display_name": "  Minha casa  "}),
        None,
        clock=lambda: 1_800_000_000,
        ddb_provider=lambda: ddb,
    )
    assert response["statusCode"] == 200
    result = body(response)
    assert result["display_name"] == "Minha casa"
    assert result["updated_at"] == "2023-11-14T22:13:20Z"
    assert result["role"] == "OWNER"
    assert result["device_id"] == DEVICE
    assert result["ownership_status"] == "OWNED"
    assert result["provisioning_status"] == "PROVISIONED"


def test_owner_can_clear_display_name() -> None:
    ddb = FakeDdb(
        device={
            "device_id": DEVICE,
            "ownership_status": "OWNED",
            "provisioning_status": "PROVISIONED",
            "created_at": 1_700_000_000,
            "updated_at": 1_700_000_000,
        }
    )
    ddb.membership["display_name"] = "Old name"
    response = handler.update_device_name(
        event(body={"display_name": None}), None, ddb_provider=lambda: ddb
    )
    assert response["statusCode"] == 200
    assert "display_name" not in body(response)
    update_kwargs = next(kwargs for name, kwargs in ddb.calls if name == "update_item")
    assert update_kwargs["UpdateExpression"] == "SET updated_at = :now REMOVE display_name"


def test_update_preserves_all_other_attributes() -> None:
    ddb = FakeDdb()
    response = handler.update_device_name(
        event(body={"display_name": "Apartamento"}), None, ddb_provider=lambda: ddb
    )
    result = body(response)
    assert result["ownership_status"] == "OWNED"
    assert result["provisioning_status"] == "PROVISIONED"
    assert result["created_at"] == "2023-11-14T22:13:20Z"
    update_kwargs = next(kwargs for name, kwargs in ddb.calls if name == "update_item")
    assert update_kwargs["TableName"] == "memberships"
    assert set(update_kwargs["Key"]) == {"device_id", "user_id"}
    assert handler._plain(update_kwargs["Key"])["user_id"] == SUB
    assert "attribute_exists(device_id)" in update_kwargs["ConditionExpression"]
    assert "attribute_exists(user_id)" in update_kwargs["ConditionExpression"]
    assert "#status = :active" in update_kwargs["ConditionExpression"]
    assert "hardware_version" not in update_kwargs["UpdateExpression"]
    assert ddb.device["updated_at"] == 1_700_000_000
    assert not any(
        name == "update_item" and kwargs["TableName"] == "devices" for name, kwargs in ddb.calls
    )


def test_request_cannot_select_another_users_membership() -> None:
    ddb = FakeDdb()
    response = handler.update_device_name(
        event(body={"display_name": "Minha casa", "user_id": "other-user"}),
        None,
        ddb_provider=lambda: ddb,
    )
    assert response["statusCode"] == 400
    assert all(name != "update_item" for name, _ in ddb.calls)


@pytest.mark.parametrize("role", ["OWNER", "ADMIN", "MEMBER"])
def test_every_active_role_can_update_own_name(role: str) -> None:
    ddb = FakeDdb(role=role)
    response = handler.update_device_name(event(), None, ddb_provider=lambda: ddb)
    assert response["statusCode"] == 200
    assert body(response)["role"] == role


@pytest.mark.parametrize(
    "membership_active,role",
    [(False, "OWNER"), (True, None), (True, "UNKNOWN")],
)
def test_no_membership_is_indistinguishable_404(membership_active: bool, role: str | None) -> None:
    ddb = FakeDdb(role=role, membership_active=membership_active)
    response = handler.update_device_name(event(), None, ddb_provider=lambda: ddb)
    assert response["statusCode"] == 404
    assert body(response)["error"]["code"] == "RESOURCE_NOT_FOUND"
    assert any(name == "update_item" for name, _ in ddb.calls)


@pytest.mark.parametrize(
    "raw_body",
    [
        None,
        "not json",
        json.dumps({"display_name": "x", "extra": 1}),
        json.dumps({}),
        json.dumps({"other": "field"}),
        json.dumps({"display_name": 5}),
        json.dumps({"display_name": "   "}),
        json.dumps({"display_name": "x" * 61}),
        json.dumps("just a string"),
    ],
)
def test_invalid_bodies_are_rejected(raw_body: str | None) -> None:
    ddb = FakeDdb()
    response = handler.update_device_name(event(body=raw_body), None, ddb_provider=lambda: ddb)
    assert response["statusCode"] == 400
    assert body(response)["error"]["code"] == "INVALID_REQUEST"
    assert all(name != "update_item" for name, _ in ddb.calls)


def test_display_name_is_trimmed_and_accepts_unicode() -> None:
    ddb = FakeDdb()
    response = handler.update_device_name(
        event(body={"display_name": "  Casa da Vovó 🏠  "}), None, ddb_provider=lambda: ddb
    )
    assert body(response)["display_name"] == "Casa da Vovó 🏠"


def test_invalid_device_id_is_rejected_before_any_dynamodb_call() -> None:
    ddb = FakeDdb()
    response = handler.update_device_name(
        event(device="not-a-device"), None, ddb_provider=lambda: ddb
    )
    assert response["statusCode"] == 400
    assert body(response)["error"]["code"] == "INVALID_DEVICE_ID"
    assert ddb.calls == []


def test_missing_or_invalid_auth_is_rejected() -> None:
    ddb = FakeDdb()
    for bad in (event(sub=None), event(sub="")):
        response = handler.update_device_name(bad, None, ddb_provider=lambda: ddb)
        assert response["statusCode"] == 401
        assert body(response)["error"]["code"] == "UNAUTHENTICATED"
    assert ddb.calls == []


def test_missing_device_despite_active_membership_is_internal_error() -> None:
    ddb = FakeDdb(device_exists=False)
    response = handler.update_device_name(event(), None, ddb_provider=lambda: ddb)
    assert response["statusCode"] == 500
    assert body(response)["error"]["code"] == "INTERNAL_ERROR"


def test_transient_dependency_errors_map_to_503_without_leaking_details() -> None:
    ddb = FakeDdb(update_error=FakeClientError("ThrottlingException"))
    response = handler.update_device_name(event(), None, ddb_provider=lambda: ddb)
    assert response["statusCode"] == 503
    assert body(response)["error"]["code"] == "SERVICE_UNAVAILABLE"
    assert "secret AWS text" not in response["body"]


def test_transient_membership_lookup_error_maps_to_503() -> None:
    ddb = FakeDdb(update_error=FakeClientError("ServiceUnavailableException"))
    response = handler.update_device_name(event(), None, ddb_provider=lambda: ddb)
    assert response["statusCode"] == 503
    assert body(response)["error"]["code"] == "SERVICE_UNAVAILABLE"
    assert any(name == "update_item" for name, _ in ddb.calls)


def test_unexpected_update_error_is_internal_error_not_hidden_as_conflict() -> None:
    ddb = FakeDdb(update_error=RuntimeError("truly unexpected"))
    response = handler.update_device_name(event(), None, ddb_provider=lambda: ddb)
    assert response["statusCode"] == 500
    assert body(response)["error"]["code"] == "INTERNAL_ERROR"


def test_oversized_body_is_rejected() -> None:
    ddb = FakeDdb()
    huge = json.dumps({"display_name": "x" * 4000})
    response = handler.update_device_name(event(body=huge), None, ddb_provider=lambda: ddb)
    assert response["statusCode"] == 413
    assert body(response)["error"]["code"] == "INVALID_REQUEST"
    assert all(name != "update_item" for name, _ in ddb.calls)


def test_invalid_base64_body_is_rejected() -> None:
    ddb = FakeDdb()
    bad_event = event(body="not-valid-base64!!!")
    bad_event["isBase64Encoded"] = True
    response = handler.update_device_name(bad_event, None, ddb_provider=lambda: ddb)
    assert response["statusCode"] == 400
    assert body(response)["error"]["code"] == "INVALID_REQUEST"


def test_no_setup_code_or_secrets_in_response() -> None:
    ddb = FakeDdb(
        device={
            "device_id": DEVICE,
            "ownership_status": "OWNED",
            "provisioning_status": "PROVISIONED",
            "created_at": 1_700_000_000,
            "updated_at": 1_700_000_000,
            "aws_thing_name": DEVICE,
        }
    )
    response = handler.update_device_name(event(), None, ddb_provider=lambda: ddb)
    result = body(response)
    for forbidden in ("setup_code", "aws_thing_name", "certificate", "secret", "pepper"):
        assert forbidden not in result
