"""Small payload-v2 handlers; API Gateway performs JWT verification."""

from __future__ import annotations

import base64
import json
import logging
import os
import random
import re
import time
import uuid
from datetime import UTC, datetime
from typing import Any

LOG = logging.getLogger(__name__)
DEVICE = re.compile(r"ib-[0-9a-f]{32}\Z")
SUB = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z", re.I)
ROLES = {"OWNER", "ADMIN", "MEMBER"}
FRESH_SECONDS = 120
_ddb: Any = None
_kms: Any = None


def _clients() -> tuple[Any, Any]:
    global _ddb, _kms
    if _ddb is None or _kms is None:
        import boto3

        _ddb = _ddb or boto3.client("dynamodb")
        _kms = _kms or boto3.client("kms")
    return _ddb, _kms


def _request(event: dict[str, Any]) -> tuple[str, str]:
    request_id = str(event.get("requestContext", {}).get("requestId") or uuid.uuid4())
    claims = event.get("requestContext", {}).get("authorizer", {}).get("jwt", {}).get("claims", {})
    sub = claims.get("sub") if isinstance(claims, dict) else None
    expected_client = os.environ["EXPECTED_APP_CLIENT_ID"]
    if (
        not isinstance(sub, str)
        or not SUB.fullmatch(sub)
        or claims.get("token_use") != "access"
        or claims.get("client_id") != expected_client
    ):
        raise ApiError(401, "UNAUTHENTICATED", "Authentication is required.", request_id)
    return sub, request_id


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, request_id: str) -> None:
        self.status, self.code, self.message, self.request_id = status, code, message, request_id


def _response(status: int, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(body, separators=(",", ":")),
    }


def _run(event: dict[str, Any], operation_name: str, operation: Any) -> dict[str, Any]:
    request_id = str(event.get("requestContext", {}).get("requestId") or uuid.uuid4())
    try:
        sub, request_id = _request(event)
        return _response(200, operation(sub, request_id))
    except ApiError as exc:
        return _response(
            exc.status,
            {"error": {"code": exc.code, "message": exc.message, "request_id": exc.request_id}},
        )
    except Exception:
        LOG.error(
            json.dumps(
                {
                    "event": "read_api_failure",
                    "request_id": request_id,
                    "operation": operation_name,
                    "error_code": "DEPENDENCY_FAILURE",
                }
            )
        )
        return _response(
            500,
            {
                "error": {
                    "code": "INTERNAL_ERROR",
                    "message": "An internal error occurred.",
                    "request_id": request_id,
                }
            },
        )


def _plain(item: dict[str, Any]) -> dict[str, Any]:
    def value(attribute: dict[str, Any]) -> Any:
        if "S" in attribute:
            return attribute["S"]
        if "N" in attribute:
            return int(attribute["N"])
        if "BOOL" in attribute:
            return attribute["BOOL"]
        raise ValueError("unsupported DynamoDB attribute")

    return {key: value(attribute) for key, attribute in item.items()}


def _membership(ddb: Any, device_id: str, sub: str, request_id: str) -> dict[str, Any]:
    result = ddb.get_item(
        TableName=os.environ["MEMBERSHIPS_TABLE"],
        Key={"device_id": {"S": device_id}, "user_id": {"S": sub}},
        ConsistentRead=True,
    )
    item = _plain(result["Item"]) if result.get("Item") else {}
    if item.get("status") != "ACTIVE" or item.get("role") not in ROLES:
        raise ApiError(404, "RESOURCE_NOT_FOUND", "Resource not found.", request_id)
    return item


def _device_id(event: dict[str, Any], request_id: str) -> str:
    value = event.get("pathParameters", {}).get("device_id")
    if not isinstance(value, str) or not DEVICE.fullmatch(value):
        raise ApiError(400, "INVALID_DEVICE_ID", "Invalid device identifier.", request_id)
    return value


def _cursor_context(sub: str, limit: int) -> dict[str, str]:
    return {"purpose": "device-list-v1", "user": sub, "limit": str(limit)}


def _cursor_encode(kms: Any, sub: str, limit: int, key: dict[str, Any]) -> str:
    raw = json.dumps(key, sort_keys=True, separators=(",", ":")).encode()
    encrypted = kms.encrypt(
        KeyId=os.environ["CURSOR_KEY_ARN"],
        Plaintext=raw,
        EncryptionContext=_cursor_context(sub, limit),
    )["CiphertextBlob"]
    return base64.urlsafe_b64encode(encrypted).decode().rstrip("=")


def _cursor_decode(kms: Any, token: str, sub: str, limit: int, request_id: str) -> dict[str, Any]:
    try:
        ciphertext = base64.b64decode(
            token + "=" * (-len(token) % 4), altchars=b"-_", validate=True
        )
        raw = kms.decrypt(
            KeyId=os.environ["CURSOR_KEY_ARN"],
            CiphertextBlob=ciphertext,
            EncryptionContext=_cursor_context(sub, limit),
        )["Plaintext"]
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError
        return data
    except Exception:
        raise ApiError(400, "INVALID_REQUEST", "Invalid pagination cursor.", request_id) from None


def list_devices(
    event: dict[str, Any], context: Any, *, sleeper: Any = time.sleep
) -> dict[str, Any]:
    def op(sub: str, rid: str) -> dict[str, Any]:
        ddb, kms = _clients()
        query = event.get("queryStringParameters") or {}
        try:
            limit = int(query.get("limit", "25"))
        except (TypeError, ValueError):
            limit = 0
        if not 1 <= limit <= 100:
            raise ApiError(400, "INVALID_REQUEST", "limit must be between 1 and 100.", rid)
        kwargs: dict[str, Any] = {
            "TableName": os.environ["MEMBERSHIPS_TABLE"],
            "IndexName": os.environ["MEMBERSHIPS_INDEX"],
            "KeyConditionExpression": "user_id = :u",
            "ExpressionAttributeValues": {":u": {"S": sub}},
            "Limit": limit,
        }
        if query.get("cursor"):
            kwargs["ExclusiveStartKey"] = _cursor_decode(kms, query["cursor"], sub, limit, rid)
        result = ddb.query(**kwargs)
        memberships = [_plain(i) for i in result.get("Items", [])]
        memberships = [
            m for m in memberships if m.get("status") == "ACTIVE" and m.get("role") in ROLES
        ]
        keys = [{"device_id": {"S": m["device_id"]}} for m in memberships]
        devices: dict[str, dict[str, Any]] = {}
        pending = keys
        for attempt in range(3):
            if not pending:
                break
            if attempt:
                sleeper(0.05 * (2 ** (attempt - 1)) + random.uniform(0, 0.025))
            batch = ddb.batch_get_item(
                RequestItems={os.environ["DEVICES_TABLE"]: {"Keys": pending}}
            )
            for item in batch.get("Responses", {}).get(os.environ["DEVICES_TABLE"], []):
                plain = _plain(item)
                devices[plain["device_id"]] = plain
            pending = (
                batch.get("UnprocessedKeys", {})
                .get(os.environ["DEVICES_TABLE"], {})
                .get("Keys", [])
            )
        if pending:
            raise RuntimeError("batch retry exhausted")
        items = [
            {
                "device_id": m["device_id"],
                **(
                    {"display_name": devices[m["device_id"]]["display_name"]}
                    if devices.get(m["device_id"], {}).get("display_name") is not None
                    else {}
                ),
                "role": m["role"],
                "status": "ACTIVE",
            }
            for m in memberships
            if m["device_id"] in devices
        ]
        body: dict[str, Any] = {"items": items}
        if result.get("LastEvaluatedKey"):
            body["next_cursor"] = _cursor_encode(kms, sub, limit, result["LastEvaluatedKey"])
        return body

    return _run(event, "list_devices", op)


def get_device(event: dict[str, Any], context: Any) -> dict[str, Any]:
    def op(sub: str, rid: str) -> dict[str, Any]:
        ddb, _ = _clients()
        did = _device_id(event, rid)
        membership = _membership(ddb, did, sub, rid)
        result = ddb.get_item(TableName=os.environ["DEVICES_TABLE"], Key={"device_id": {"S": did}})
        if not result.get("Item"):
            raise RuntimeError("authorized membership references missing device")
        item = _plain(result["Item"])
        out = {k: item[k] for k in ("device_id", "ownership_status", "provisioning_status")}
        out["role"] = membership["role"]
        for key in ("display_name", "hardware_version"):
            if key in item:
                out[key] = item[key]
        return out

    return _run(event, "get_device", op)


def get_status(event: dict[str, Any], context: Any) -> dict[str, Any]:
    def op(sub: str, rid: str) -> dict[str, Any]:
        ddb, _ = _clients()
        did = _device_id(event, rid)
        _membership(ddb, did, sub, rid)
        result = ddb.get_item(
            TableName=os.environ["TELEMETRY_TABLE"],
            Key={"device_id": {"S": did}, "record_key": {"S": "STATE#CURRENT"}},
        )
        if not result.get("Item"):
            return {
                "device_id": did,
                "connectivity": "UNKNOWN",
                "freshness": "UNKNOWN",
                "health": None,
            }
        item = _plain(result["Item"])
        try:
            seen = datetime.fromisoformat(str(item["last_seen_at"]).replace("Z", "+00:00"))
            age = (datetime.now(UTC) - seen).total_seconds()
            if seen.tzinfo is None or age < 0:
                raise ValueError
            freshness, connectivity = (
                ("FRESH", "RECENTLY_SEEN") if age <= FRESH_SECONDS else ("STALE", "STALE")
            )
            state, firmware = item["last_state"], item["firmware_version"]
            if state not in {"IDLE", "RINGING", "OFF_HOOK", "IN_CALL", "ERROR"} or not isinstance(
                firmware, str
            ):
                raise ValueError
        except (KeyError, TypeError, ValueError):
            return {
                "device_id": did,
                "connectivity": "UNKNOWN",
                "freshness": "UNKNOWN",
                "health": None,
            }
        return {
            "device_id": did,
            "connectivity": connectivity,
            "freshness": freshness,
            "health": {
                "intercom_state": state,
                "firmware_version": firmware,
                "last_seen_at": item["last_seen_at"],
            },
        }

    return _run(event, "get_status", op)
