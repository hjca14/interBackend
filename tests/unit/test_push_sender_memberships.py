from __future__ import annotations

from typing import Any

from lambdas.push_sender.memberships import MAX_MEMBERSHIPS_PER_DEVICE, active_memberships

DEVICE = "ib-" + "a" * 32


def av(item: dict[str, object]) -> dict[str, dict[str, object]]:
    return {
        key: ({"N": str(value)} if isinstance(value, int) else {"S": value})
        for key, value in item.items()
    }


class FakeDdb:
    def __init__(self, pages: list[list[dict[str, object]]]) -> None:
        self.pages = pages
        self.calls: list[dict[str, Any]] = []

    def query(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        page = self.pages.pop(0) if self.pages else []
        result: dict[str, Any] = {"Items": [av(item) for item in page]}
        if self.pages:
            result["LastEvaluatedKey"] = {"device_id": {"S": DEVICE}}
        return result


def test_zero_memberships_is_a_valid_empty_result() -> None:
    ddb = FakeDdb([[]])
    memberships, truncated = active_memberships(ddb, "table", DEVICE)
    assert memberships == []
    assert truncated is False


def test_only_active_memberships_with_a_valid_role_are_returned() -> None:
    ddb = FakeDdb(
        [
            [
                {"device_id": DEVICE, "user_id": "u1", "status": "ACTIVE", "role": "OWNER"},
                {"device_id": DEVICE, "user_id": "u2", "status": "REMOVED", "role": "OWNER"},
                {"device_id": DEVICE, "user_id": "u3", "status": "ACTIVE", "role": "UNKNOWN"},
                {"device_id": DEVICE, "user_id": "u4", "status": "PENDING", "role": "MEMBER"},
                {"device_id": DEVICE, "user_id": "u5", "status": "ACTIVE", "role": "MEMBER"},
            ]
        ]
    )
    memberships, truncated = active_memberships(ddb, "table", DEVICE)
    assert {m["user_id"] for m in memberships} == {"u1", "u5"}
    assert truncated is False


def test_pagination_across_multiple_pages_is_followed() -> None:
    page1 = [
        {"device_id": DEVICE, "user_id": f"u{i}", "status": "ACTIVE", "role": "MEMBER"}
        for i in range(3)
    ]
    page2 = [
        {"device_id": DEVICE, "user_id": f"u{i}", "status": "ACTIVE", "role": "MEMBER"}
        for i in range(3, 5)
    ]
    ddb = FakeDdb([page1, page2])
    memberships, truncated = active_memberships(ddb, "table", DEVICE)
    assert len(memberships) == 5
    assert truncated is False
    assert len(ddb.calls) == 2
    assert "ExclusiveStartKey" in ddb.calls[1]


def test_fan_out_is_capped_and_reports_truncation() -> None:
    huge_page = [
        {"device_id": DEVICE, "user_id": f"u{i}", "status": "ACTIVE", "role": "MEMBER"}
        for i in range(MAX_MEMBERSHIPS_PER_DEVICE + 10)
    ]
    ddb = FakeDdb([huge_page])
    memberships, truncated = active_memberships(ddb, "table", DEVICE)
    assert len(memberships) == MAX_MEMBERSHIPS_PER_DEVICE
    assert truncated is True


def test_query_uses_consistent_read_and_exact_device_id() -> None:
    ddb = FakeDdb([[]])
    active_memberships(ddb, "table", DEVICE)
    call = ddb.calls[0]
    assert call["ConsistentRead"] is True
    assert call["ExpressionAttributeValues"] == {":d": {"S": DEVICE}}
    assert call["TableName"] == "table"
