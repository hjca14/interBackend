"""Small boto3 adapter; every DynamoDB request uses exact PK/SK keys."""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from domain.telemetry.models import Message


class TelemetryStore:
    def __init__(
        self, dynamodb: Any, table_name: str, *, history_days: int, detail_limit: int
    ) -> None:
        self.client = dynamodb
        self.table_name = table_name
        self.history_seconds = history_days * 86400
        self.detail_limit = detail_limit

    def _item(self, value: dict[str, Any]) -> dict[str, Any]:
        serialized: dict[str, Any] = {}
        for key, item in value.items():
            if isinstance(item, str):
                serialized[key] = {"S": item}
            elif isinstance(item, int) and not isinstance(item, bool):
                serialized[key] = {"N": str(item)}
            else:
                raise TypeError("unsupported DynamoDB value type")
        return serialized

    def record(self, message: Message) -> str:
        now = message.received_at.strftime("%Y-%m-%dT%H:%M:%SZ")
        expires = int(message.received_at.timestamp()) + self.history_seconds
        if message.category == "health":
            state = {
                "device_id": message.device_id,
                "record_key": "STATE#CURRENT",
                "last_seen_at": now,
                "last_message_type": "health",
                "updated_at": now,
                "schema_version": 1,
                **message.values,
            }
            self.client.put_item(TableName=self.table_name, Item=self._item(state))
            self._increment(message, "health_count", expires)
            return "state"

        counter = "event_count" if message.category == "events" else "response_count"
        if message.detail_key is None:
            self._increment(message, counter, expires)
            return "aggregated"

        detail = {
            "device_id": message.device_id,
            "record_key": message.detail_key,
            "occurred_at": message.occurred_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "received_at": now,
            "schema_version": 1,
            "expires_at": expires,
            **message.values,
        }
        try:
            self.client.transact_write_items(
                TransactItems=[
                    {
                        "Put": {
                            "TableName": self.table_name,
                            "Item": self._item(detail),
                            "ConditionExpression": "attribute_not_exists(device_id)",
                        }
                    },
                    self._metric_transaction(message, counter, expires),
                ]
            )
            return "detailed"
        except Exception as error:
            response = getattr(error, "response", {})
            if response.get("Error", {}).get("Code") != "TransactionCanceledException":
                raise
        existing = self.client.get_item(
            TableName=self.table_name,
            Key=self._item({"device_id": message.device_id, "record_key": message.detail_key}),
            ConsistentRead=True,
            ProjectionExpression="device_id",
        )
        if "Item" in existing:
            self._increment(message, counter, expires, extra="duplicate_count")
            return "duplicate"
        self._increment(message, counter, expires, extra="detailed_dropped_count")
        return "dropped"

    def invalid(self, device_id: str, received_at: datetime) -> None:
        message = Message(device_id, "invalid", received_at, received_at, {})
        self._increment(
            message,
            "invalid_count",
            int(received_at.timestamp()) + self.history_seconds,
        )

    def _metric_transaction(self, message: Message, counter: str, expires: int) -> dict[str, Any]:
        values = self._metric_values(message, expires)
        values.update({":one": {"N": "1"}, ":limit": {"N": str(self.detail_limit)}})
        return {
            "Update": {
                "TableName": self.table_name,
                "Key": self._item(
                    {"device_id": message.device_id, "record_key": message.metric_key}
                ),
                "UpdateExpression": (
                    f"SET first_received_at = if_not_exists(first_received_at, :now), "
                    "last_received_at = :now, expires_at = :ttl "
                    f"ADD {counter} :one, detailed_count :one"
                ),
                "ConditionExpression": (
                    "attribute_not_exists(detailed_count) OR detailed_count < :limit"
                ),
                "ExpressionAttributeValues": values,
            }
        }

    def _metric_values(self, message: Message, expires: int) -> dict[str, Any]:
        return {
            ":now": {"S": message.received_at.strftime("%Y-%m-%dT%H:%M:%SZ")},
            ":ttl": {"N": str(expires)},
        }

    def _increment(
        self, message: Message, counter: str, expires: int, extra: str | None = None
    ) -> None:
        counters = counter if extra is None else f"{counter} :one, {extra}"
        self.client.update_item(
            TableName=self.table_name,
            Key=self._item({"device_id": message.device_id, "record_key": message.metric_key}),
            UpdateExpression=(
                "SET first_received_at = if_not_exists(first_received_at, :now), "
                "last_received_at = :now, expires_at = :ttl "
                f"ADD {counters} :one"
            ),
            ExpressionAttributeValues={**self._metric_values(message, expires), ":one": {"N": "1"}},
        )


def epoch_ms() -> int:
    return time.time_ns() // 1_000_000
