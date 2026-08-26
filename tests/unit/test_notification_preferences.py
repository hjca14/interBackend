from __future__ import annotations

import json
import os
from copy import deepcopy
from typing import Any

import pytest

from domain.notifications import DEFAULTS, combine
from lambdas.device_api import handler

DEVICE = "ib-" + "b" * 32
SUB = "user-1"


class Ddb:
    def __init__(self, *, status: str = "ACTIVE", user: str = SUB, preferences: Any = None) -> None:
        self.item: dict[str, Any] = {
            "device_id": DEVICE,
            "user_id": user,
            "status": status,
            "role": "MEMBER",
            "unrelated": "preserved",
        }
        if preferences is not None:
            self.item["notification_preferences"] = preferences
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("get", kwargs))
        key = handler._plain(kwargs["Key"])
        return {"Item": handler._item(self.item)} if key["user_id"] == self.item["user_id"] else {}

    def update_item(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("update", kwargs))
        values = handler._plain(kwargs["ExpressionAttributeValues"])
        assert kwargs["UpdateExpression"] == "SET #preferences = :preferences"
        assert kwargs["ExpressionAttributeNames"]["#preferences"] == "notification_preferences"
        assert self.item["status"] == values[":active"] == "ACTIVE"
        before = {
            key: value for key, value in self.item.items() if key != "notification_preferences"
        }
        self.item["notification_preferences"] = values[":preferences"]
        assert before == {
            key: value for key, value in self.item.items() if key != "notification_preferences"
        }
        return {"Attributes": handler._item(self.item)}


def event(body: dict[str, Any] | None = None, *, sub: str = SUB) -> dict[str, Any]:
    result: dict[str, Any] = {
        "pathParameters": {"device_id": DEVICE},
        "requestContext": {
            "requestId": "rid",
            "authorizer": {
                "jwt": {"claims": {"sub": sub, "token_use": "access", "client_id": "client"}}
            },
        },
    }
    if body is not None:
        result["body"] = json.dumps(body)
    return result


def response_body(response: dict[str, Any]) -> dict[str, Any]:
    return json.loads(response["body"])


@pytest.fixture(autouse=True)
def environment() -> None:
    os.environ.update(EXPECTED_APP_CLIENT_ID="client", MEMBERSHIPS_TABLE="memberships")


def test_defaults_for_legacy_membership_and_get_does_not_write() -> None:
    ddb = Ddb()
    response = handler.get_notification_preferences(event(), None, ddb_provider=lambda: ddb)
    assert response["statusCode"] == 200
    assert response_body(response) == DEFAULTS
    assert [name for name, _ in ddb.calls] == ["get"]
    assert ddb.calls[0][1]["ConsistentRead"] is True


def test_get_merges_persisted_preferences_and_sorts_days() -> None:
    ddb = Ddb(preferences={"notifications_enabled": False, "quiet_schedule": {"days": [7, 1]}})
    result = response_body(
        handler.get_notification_preferences(event(), None, ddb_provider=lambda: ddb)
    )
    assert result["notifications_enabled"] is False
    assert result["incoming_calls_enabled"] is True
    assert result["quiet_schedule"]["days"] == [1, 7]


def test_partial_patch_preserves_fields_and_only_updates_preference_map() -> None:
    existing = combine(patch={"incoming_calls_enabled": False})
    ddb = Ddb(preferences=existing)
    response = handler.update_notification_preferences(
        event({"notifications_enabled": False}),
        None,
        clock=lambda: 1_800_000_000,
        ddb_provider=lambda: ddb,
    )
    result = response_body(response)
    assert response["statusCode"] == 200
    assert result["incoming_calls_enabled"] is False
    assert result["notifications_enabled"] is False
    assert result["updated_at"] == "2027-01-15T08:00:00Z"
    assert ddb.item["role"] == "MEMBER" and ddb.item["status"] == "ACTIVE"
    assert ddb.item["unrelated"] == "preserved"


@pytest.mark.parametrize(
    "patch",
    [
        {},
        {"unknown": True},
        {"version": 1},
        {"updated_at": None},
        {"delivery_scope": "HOME"},
        {"quiet_schedule": {"behavior": "MUTE"}},
        {
            "quiet_schedule": {
                "enabled": True,
                "days": [1],
                "start_time": "22:00",
                "end_time": "06:00",
            }
        },
        {"quiet_schedule": {"timezone": "Not/A_Zone"}},
        {
            "quiet_schedule": {
                "enabled": True,
                "timezone": "UTC",
                "days": [],
                "start_time": "22:00",
                "end_time": "06:00",
            }
        },
        {"quiet_schedule": {"days": [1, 1]}},
        {"quiet_schedule": {"days": [0]}},
        {"quiet_schedule": {"start_time": "7:00"}},
        {
            "quiet_schedule": {
                "enabled": True,
                "timezone": "UTC",
                "days": [1],
                "start_time": "07:00",
                "end_time": "07:00",
            }
        },
    ],
)
def test_invalid_patches_are_sanitized(patch: dict[str, Any]) -> None:
    ddb = Ddb()
    response = handler.update_notification_preferences(event(patch), None, ddb_provider=lambda: ddb)
    assert response["statusCode"] == 400
    assert "Not/A_Zone" not in response["body"]
    assert all(name != "update" for name, _ in ddb.calls)


@pytest.mark.parametrize("start,end", [("08:30", "17:45"), ("22:00", "06:00")])
def test_valid_normal_and_overnight_intervals(start: str, end: str) -> None:
    result = combine(
        patch={
            "quiet_schedule": {
                "enabled": True,
                "timezone": "America/Sao_Paulo",
                "days": [5, 1],
                "start_time": start,
                "end_time": end,
            }
        }
    )
    assert result["quiet_schedule"]["days"] == [1, 5]


@pytest.mark.parametrize("status", ["PENDING", "REVOKED", "INACTIVE"])
def test_non_active_membership_is_hidden(status: str) -> None:
    ddb = Ddb(status=status)
    response = handler.get_notification_preferences(event(), None, ddb_provider=lambda: ddb)
    assert response["statusCode"] == 404


def test_missing_membership_and_other_user_cannot_access() -> None:
    ddb = Ddb(user="another-user")
    assert (
        handler.get_notification_preferences(event(), None, ddb_provider=lambda: ddb)["statusCode"]
        == 404
    )
    assert (
        handler.update_notification_preferences(
            event({"notifications_enabled": False}), None, ddb_provider=lambda: ddb
        )["statusCode"]
        == 404
    )
    assert all(name != "update" for name, _ in ddb.calls)


def test_defaults_are_not_mutated() -> None:
    original = deepcopy(DEFAULTS)
    combine(patch={"quiet_schedule": {"days": [3]}})
    assert original == DEFAULTS
