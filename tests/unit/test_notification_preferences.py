from __future__ import annotations

import json
import os
from copy import deepcopy
from typing import Any

import pytest

from lambdas.device_api import handler
from lambdas.device_api.notification_preferences import DEFAULTS, combine

DEVICE = "ib-" + "b" * 32
SUB = "user-1"


class Ddb:
    def __init__(
        self,
        *,
        status: str = "ACTIVE",
        user: str = SUB,
        preferences: Any = None,
        update_mutations: list[Any] | None = None,
        always_conflict: bool = False,
        update_error: Exception | None = None,
    ) -> None:
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
        self.present = True
        self.update_mutations = list(update_mutations or [])
        self.always_conflict = always_conflict
        self.update_error = update_error

    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("get", kwargs))
        key = handler._plain(kwargs["Key"])
        return (
            {"Item": handler._item(self.item)}
            if self.present and key["user_id"] == self.item["user_id"]
            else {}
        )

    def update_item(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("update", kwargs))
        values = handler._plain(kwargs["ExpressionAttributeValues"])
        assert kwargs["UpdateExpression"] == "SET #preferences = :preferences"
        assert kwargs["ExpressionAttributeNames"]["#preferences"] == "notification_preferences"
        if self.update_error is not None:
            raise self.update_error
        if self.update_mutations:
            self.update_mutations.pop(0)(self)
        condition = kwargs["ConditionExpression"]
        matches = (
            self.present
            and self.item["status"] == values[":active"] == "ACTIVE"
            and self.item["role"] in {values[":owner"], values[":admin"], values[":member"]}
        )
        if "attribute_not_exists(#preferences)" in condition:
            matches = matches and "notification_preferences" not in self.item
        if "#preferences = :expected_preferences" in condition:
            matches = matches and self.item.get("notification_preferences") == values.get(
                ":expected_preferences"
            )
        if self.always_conflict or not matches:
            raise FakeClientError("ConditionalCheckFailedException")
        before = {
            key: value for key, value in self.item.items() if key != "notification_preferences"
        }
        self.item["notification_preferences"] = values[":preferences"]
        assert before == {
            key: value for key, value in self.item.items() if key != "notification_preferences"
        }
        return {"Attributes": handler._item(self.item)}


class FakeClientError(Exception):
    def __init__(self, code: str) -> None:
        self.response = {"Error": {"Code": code, "Message": "sensitive dependency details"}}


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
    assert response_body(response)["alert_mode"] == "RING_AND_NOTIFICATION"
    assert [name for name, _ in ddb.calls] == ["get"]
    assert ddb.calls[0][1]["ConsistentRead"] is True


def test_get_merges_persisted_preferences_and_sorts_days() -> None:
    ddb = Ddb(preferences={"alert_mode": "NOTIFICATION_ONLY", "quiet_schedule": {"days": [7, 1]}})
    result = response_body(
        handler.get_notification_preferences(event(), None, ddb_provider=lambda: ddb)
    )
    assert result["alert_mode"] == "NOTIFICATION_ONLY"
    assert result["quiet_schedule"]["days"] == [1, 7]


def test_partial_patch_preserves_fields_and_only_updates_preference_map() -> None:
    existing = combine(patch={"quiet_schedule": {"behavior": "BLOCK_ALL"}})
    ddb = Ddb(preferences=existing)
    response = handler.update_notification_preferences(
        event({"alert_mode": "NOTIFICATION_ONLY"}),
        None,
        clock=lambda: 1_800_000_000,
        ddb_provider=lambda: ddb,
    )
    result = response_body(response)
    assert response["statusCode"] == 200
    assert result["alert_mode"] == "NOTIFICATION_ONLY"
    assert result["quiet_schedule"]["behavior"] == "BLOCK_ALL"
    assert result["updated_at"] == "2027-01-15T08:00:00Z"
    assert ddb.item["role"] == "MEMBER" and ddb.item["status"] == "ACTIVE"
    assert ddb.item["unrelated"] == "preserved"


def test_nested_quiet_schedule_patch_preserves_omitted_fields() -> None:
    existing = combine(
        patch={
            "quiet_schedule": {
                "timezone": "America/Sao_Paulo",
                "days": [2, 4],
                "start_time": "22:00",
                "end_time": "06:00",
                "behavior": "BLOCK_ALL",
            }
        }
    )
    ddb = Ddb(preferences=existing)
    result = response_body(
        handler.update_notification_preferences(
            event({"quiet_schedule": {"enabled": True}}), None, ddb_provider=lambda: ddb
        )
    )
    assert result["quiet_schedule"] == {
        "enabled": True,
        "timezone": "America/Sao_Paulo",
        "days": [2, 4],
        "start_time": "22:00",
        "end_time": "06:00",
        "behavior": "BLOCK_ALL",
    }


@pytest.mark.parametrize(
    "patch",
    [
        {},
        {"unknown": True},
        {"version": 1},
        {"updated_at": None},
        {"alert_mode": "UNKNOWN"},
        {"incoming_calls_enabled": True},
        {"notifications_enabled": True},
        {"delivery_scope": "ANYWHERE"},
        {"delivery_scope": "LOCAL_ONLY"},
        {"delivery_scope": "AWAY_ONLY"},
        {"local_network_alert_mode": "NONE"},
        {"remote_network_alert_mode": "NONE"},
        {"quiet_schedule": {"behavior": "SILENT"}},
        {"quiet_schedule": {"behavior": "BLOCK"}},
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


@pytest.mark.parametrize("behavior", ["NOTIFICATION_ONLY", "BLOCK_ALL"])
def test_new_schedule_behaviors_are_accepted(behavior: str) -> None:
    assert (
        combine(patch={"quiet_schedule": {"behavior": behavior}})["quiet_schedule"]["behavior"]
        == behavior
    )


@pytest.mark.parametrize(
    "mode", ["NONE", "RING_ONLY", "NOTIFICATION_ONLY", "RING_AND_NOTIFICATION"]
)
def test_all_alert_modes_are_accepted(mode: str) -> None:
    assert combine(patch={"alert_mode": mode})["alert_mode"] == mode


def test_alert_mode_and_partial_schedule_can_be_patched_together() -> None:
    desired = combine(patch={"alert_mode": "NONE", "quiet_schedule": {"behavior": "BLOCK_ALL"}})
    assert desired["alert_mode"] == "NONE"
    assert desired["quiet_schedule"]["behavior"] == "BLOCK_ALL"
    assert desired["quiet_schedule"]["enabled"] is False


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
            event({"alert_mode": "NOTIFICATION_ONLY"}), None, ddb_provider=lambda: ddb
        )["statusCode"]
        == 404
    )
    assert all(name != "update" for name, _ in ddb.calls)


def test_defaults_are_not_mutated() -> None:
    original = deepcopy(DEFAULTS)
    combine(patch={"quiet_schedule": {"days": [3]}})
    assert original == DEFAULTS


def test_concurrent_creation_retries_and_preserves_both_changes() -> None:
    concurrent = combine(patch={"quiet_schedule": {"behavior": "BLOCK_ALL"}})
    ddb = Ddb(
        update_mutations=[
            lambda client: client.item.__setitem__("notification_preferences", concurrent)
        ]
    )
    response = handler.update_notification_preferences(
        event({"alert_mode": "NOTIFICATION_ONLY"}), None, ddb_provider=lambda: ddb
    )
    result = response_body(response)
    assert response["statusCode"] == 200
    assert result["alert_mode"] == "NOTIFICATION_ONLY"
    assert result["quiet_schedule"]["behavior"] == "BLOCK_ALL"
    assert [name for name, _ in ddb.calls] == ["get", "update", "get", "update"]
    first_update = ddb.calls[1][1]
    assert "attribute_not_exists(#preferences)" in first_update["ConditionExpression"]


def test_concurrent_map_change_retries_against_exact_value() -> None:
    original = combine()
    concurrent = combine(original, {"quiet_schedule": {"behavior": "BLOCK_ALL"}})
    ddb = Ddb(
        preferences=original,
        update_mutations=[
            lambda client: client.item.__setitem__("notification_preferences", concurrent)
        ],
    )
    result = response_body(
        handler.update_notification_preferences(
            event({"alert_mode": "NOTIFICATION_ONLY"}), None, ddb_provider=lambda: ddb
        )
    )
    assert result["alert_mode"] == "NOTIFICATION_ONLY"
    assert result["quiet_schedule"]["behavior"] == "BLOCK_ALL"
    assert all(
        "#preferences = :expected_preferences" in call["ConditionExpression"]
        for name, call in ddb.calls
        if name == "update"
    )


def test_persistent_conflict_has_strict_retry_limit_and_sanitized_409() -> None:
    ddb = Ddb(always_conflict=True)
    response = handler.update_notification_preferences(
        event({"alert_mode": "NOTIFICATION_ONLY"}), None, ddb_provider=lambda: ddb
    )
    assert response["statusCode"] == 409
    assert response_body(response)["error"] == {
        "code": "CONFLICT",
        "message": "The resource changed while it was being updated.",
        "request_id": "rid",
    }
    assert sum(name == "update" for name, _ in ddb.calls) == handler.MAX_PREFERENCE_UPDATE_ATTEMPTS
    assert sum(name == "get" for name, _ in ddb.calls) == handler.MAX_PREFERENCE_UPDATE_ATTEMPTS + 1
    assert "sensitive" not in response["body"]


@pytest.mark.parametrize("removed", [False, True])
def test_membership_becoming_inactive_or_removed_during_retry_is_404(removed: bool) -> None:
    def revoke(client: Ddb) -> None:
        if removed:
            client.present = False
        else:
            client.item["status"] = "REVOKED"

    ddb = Ddb(update_mutations=[revoke])
    response = handler.update_notification_preferences(
        event({"alert_mode": "NOTIFICATION_ONLY"}), None, ddb_provider=lambda: ddb
    )
    assert response["statusCode"] == 404
    assert response_body(response)["error"]["code"] == "RESOURCE_NOT_FOUND"
    assert sum(name == "update" for name, _ in ddb.calls) == 1


def test_temporary_update_failure_is_service_unavailable_without_retry() -> None:
    ddb = Ddb(update_error=FakeClientError("ProvisionedThroughputExceededException"))
    response = handler.update_notification_preferences(
        event({"alert_mode": "NOTIFICATION_ONLY"}), None, ddb_provider=lambda: ddb
    )
    assert response["statusCode"] == 503
    assert response_body(response)["error"]["code"] == "SERVICE_UNAVAILABLE"
    assert sum(name == "update" for name, _ in ddb.calls) == 1


def test_invalid_persisted_map_is_sanitized() -> None:
    ddb = Ddb(preferences={"alert_mode": "INVALID"})
    response = handler.get_notification_preferences(event(), None, ddb_provider=lambda: ddb)
    assert response["statusCode"] == 500
    assert response_body(response)["error"]["code"] == "INTERNAL_ERROR"
    assert "INVALID" not in response["body"]
