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
            elif isinstance(item, dict):
                serialized[key] = {"M": self._item(item)}
            else:
                raise TypeError("unsupported DynamoDB value type")
        return serialized

    def record(self, message: Message) -> str:
        now = message.received_at.strftime("%Y-%m-%dT%H:%M:%SZ")
        expires = int(message.received_at.timestamp()) + self.history_seconds
        if message.category == "health":
            values = {
                ":seen": {"S": now},
                ":kind": {"S": "health"},
                ":schema": {"N": "1"},
                ":firmware": self._item({"v": message.values["firmware_version"]})["v"],
                ":state": self._item({"v": message.values["last_state"]})["v"],
                ":rssi": self._item({"v": message.values["RSSI"]})["v"],
                ":heap": self._item({"v": message.values["free_heap"]})["v"],
            }
            try:
                self.client.update_item(
                    TableName=self.table_name,
                    Key=self._item({"device_id": message.device_id, "record_key": "STATE#CURRENT"}),
                    UpdateExpression=(
                        "SET last_seen_at = :seen, last_message_type = :kind, "
                        "firmware_version = :firmware, last_state = :state, RSSI = :rssi, "
                        "free_heap = :heap, updated_at = :seen, schema_version = :schema"
                    ),
                    ConditionExpression=(
                        "attribute_not_exists(last_seen_at) OR last_seen_at <= :seen"
                    ),
                    ExpressionAttributeValues=values,
                )
            except Exception as error:
                if self._error_code(error) != "ConditionalCheckFailedException":
                    raise
                # A trusted newer health already won. Old delivery/retry is benign.
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
            "updated_at": now,
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
            self._project_response(message, expires)
            return "detailed"
        except Exception as error:
            if self._error_code(error) != "TransactionCanceledException":
                raise
            reasons = getattr(error, "response", {}).get("CancellationReasons", [])
            if len(reasons) < 2:
                raise
            first_code = reasons[0].get("Code")
            second_code = reasons[1].get("Code")
            if first_code == "ConditionalCheckFailed":
                self._increment_only(message, "duplicate_count", expires)
                self._project_response(message, expires)
                return "duplicate"
            if second_code != "ConditionalCheckFailed":
                # TransactionConflict/ThrottlingError/InternalServerError and
                # unknown cancellations are infrastructure failures: retry.
                raise
        self._increment(message, counter, expires, extra="detailed_dropped_count")
        self._project_response(message, expires)
        return "dropped"

    def _project_response(self, message: Message, expires: int) -> None:
        if message.category != "responses" or message.identifier is None:
            return
        now = message.received_at.strftime("%Y-%m-%dT%H:%M:%SZ")
        status = message.values["status"]
        projection = {
            "device_id": message.device_id,
            "record_key": f"COMMAND_RESULT#{message.identifier}",
            "command_id": message.identifier,
            "received_at": now,
            "updated_at": now,
            "schema_version": 1,
            "expires_at": expires,
            **message.values,
        }
        try:
            self.client.put_item(
                TableName=self.table_name,
                Item=self._item(projection),
                ConditionExpression=(
                    "attribute_not_exists(received_at) OR "
                    "(#status = :accepted AND received_at <= :received) OR "
                    "(#status <> :accepted AND :incoming <> :accepted AND received_at < :received)"
                ),
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":accepted": {"S": "ACCEPTED"},
                    ":incoming": {"S": str(status)},
                    ":received": {"S": now},
                },
            )
        except Exception as error:
            if self._error_code(error) != "ConditionalCheckFailedException":
                raise

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

    def _increment_only(self, message: Message, counter: str, expires: int) -> None:
        self.client.update_item(
            TableName=self.table_name,
            Key=self._item({"device_id": message.device_id, "record_key": message.metric_key}),
            UpdateExpression=(
                "SET first_received_at = if_not_exists(first_received_at, :now), "
                "last_received_at = :now, expires_at = :ttl "
                f"ADD {counter} :one"
            ),
            ExpressionAttributeValues={**self._metric_values(message, expires), ":one": {"N": "1"}},
        )

    @staticmethod
    def _error_code(error: Exception) -> str | None:
        response = getattr(error, "response", {})
        code = response.get("Error", {}).get("Code")
        return code if isinstance(code, str) else None


def epoch_ms() -> int:
    return time.time_ns() // 1_000_000
