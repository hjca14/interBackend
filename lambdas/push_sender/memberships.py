"""Adapter: active DeviceMemberships for one device_id.

Queries the base table by ``device_id`` (the exact access pattern already
documented in ``docs/data-model.md`` as "Obter membros de um dispositivo"),
strongly consistent, paginated and explicitly capped -- an absent or
exhausted result is a normal, valid outcome, not an error.
"""

from __future__ import annotations

from typing import Any

from .dynamo import plain

ROLES = frozenset({"OWNER", "ADMIN", "MEMBER"})
MAX_MEMBERSHIPS_PER_DEVICE = 50


def active_memberships(
    ddb: Any, table_name: str, device_id: str
) -> tuple[list[dict[str, Any]], bool]:
    """Return ``(memberships, truncated)``.

    ``truncated`` is ``True`` only when ``MAX_MEMBERSHIPS_PER_DEVICE`` was
    reached before the query was exhausted -- a safety cap, not an
    expected real-world limit for a residential intercom.
    """
    memberships: list[dict[str, Any]] = []
    exclusive_start_key: dict[str, Any] | None = None
    while True:
        kwargs: dict[str, Any] = {
            "TableName": table_name,
            "KeyConditionExpression": "device_id = :d",
            "ExpressionAttributeValues": {":d": {"S": device_id}},
            "ConsistentRead": True,
        }
        if exclusive_start_key is not None:
            kwargs["ExclusiveStartKey"] = exclusive_start_key
        result = ddb.query(**kwargs)
        for raw in result.get("Items", []):
            parsed = plain(raw)
            if parsed.get("status") == "ACTIVE" and parsed.get("role") in ROLES:
                memberships.append(parsed)
                if len(memberships) >= MAX_MEMBERSHIPS_PER_DEVICE:
                    return memberships, True
        exclusive_start_key = result.get("LastEvaluatedKey")
        if not exclusive_start_key:
            return memberships, False
