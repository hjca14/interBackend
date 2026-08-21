"""Phase 2D command handlers with injectable clock, RNG, DynamoDB and publisher."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import secrets
import time
import unicodedata
import uuid
from datetime import UTC, datetime
from typing import Any, TypeGuard

LOG = logging.getLogger(__name__)
DEVICE = re.compile(r"ib-[0-9a-f]{32}\Z")
COMMAND_ID = re.compile(r"[0-9a-f]{32}\Z")
SAFE_CODE = re.compile(r"[A-Za-z0-9._-]{1,64}\Z")
ROLES = {"OWNER", "ADMIN", "MEMBER"}
REMOTE_COMMANDS = {"OPEN_DOOR", "RESTART"}
MAX_BODY_BYTES = 4 * 1024
MAX_MQTT_BYTES = 8 * 1024
COMMAND_LIFETIME_SECONDS = 30
IDEMPOTENCY_SECONDS = 24 * 60 * 60
INTENT_RETENTION_SECONDS = 30 * 24 * 60 * 60
COOLDOWN_SECONDS = 2
MAX_SUB_LENGTH = 128

_ddb: Any = None
_publisher: Any = None


class ApiError(Exception):
    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        request_id: str,
        *,
        retry_after: int | None = None,
    ) -> None:
        self.status, self.code, self.message, self.request_id = status, code, message, request_id
        self.retry_after = retry_after


class DependencyUnavailable(RuntimeError):
    """A dependency failure whose details must not cross the API boundary."""


def _clients() -> tuple[Any, Any]:
    global _ddb, _publisher
    if _ddb is None or _publisher is None:
        import boto3

        _ddb = _ddb or boto3.client("dynamodb")
        _publisher = _publisher or boto3.client("iot-data")
    return _ddb, _publisher


def _valid_sub(value: object) -> TypeGuard[str]:
    return (
        isinstance(value, str)
        and 0 < len(value) <= MAX_SUB_LENGTH
        and all(unicodedata.category(character) != "Cc" for character in value)
    )


def _request(event: dict[str, Any]) -> tuple[str, str]:
    request_id = str(event.get("requestContext", {}).get("requestId") or uuid.uuid4())
    claims = event.get("requestContext", {}).get("authorizer", {}).get("jwt", {}).get("claims", {})
    sub = claims.get("sub") if isinstance(claims, dict) else None
    if (
        not _valid_sub(sub)
        or claims.get("token_use") != "access"
        or claims.get("client_id") != os.environ["EXPECTED_APP_CLIENT_ID"]
    ):
        raise ApiError(401, "UNAUTHENTICATED", "Authentication is required.", request_id)
    return sub, request_id


def _response(status: int, body: dict[str, Any], retry_after: int | None = None) -> dict[str, Any]:
    headers = {"content-type": "application/json"}
    if retry_after is not None:
        headers["retry-after"] = str(retry_after)
    return {
        "statusCode": status,
        "headers": headers,
        "body": json.dumps(body, separators=(",", ":")),
    }


def _run(event: dict[str, Any], operation: str, callback: Any) -> dict[str, Any]:
    request_id = str(event.get("requestContext", {}).get("requestId") or uuid.uuid4())
    try:
        sub, request_id = _request(event)
        status, result = callback(sub, request_id)
        return _response(status, result)
    except ApiError as error:
        return _response(
            error.status,
            {
                "error": {
                    "code": error.code,
                    "message": error.message,
                    "request_id": error.request_id,
                }
            },
            error.retry_after,
        )
    except DependencyUnavailable:
        LOG.error(
            json.dumps(
                {
                    "event": "command_api_failure",
                    "request_id": request_id,
                    "operation": operation,
                    "error_code": "DEPENDENCY_UNAVAILABLE",
                }
            )
        )
        return _response(
            503,
            {
                "error": {
                    "code": "SERVICE_UNAVAILABLE",
                    "message": "A required service is temporarily unavailable.",
                    "request_id": request_id,
                }
            },
        )
    except Exception:
        LOG.error(
            json.dumps(
                {
                    "event": "command_api_failure",
                    "request_id": request_id,
                    "operation": operation,
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
    result: dict[str, Any] = {}
    for key, attribute in item.items():
        if "S" in attribute:
            result[key] = attribute["S"]
        elif "N" in attribute:
            result[key] = int(attribute["N"])
        elif "M" in attribute:
            result[key] = _plain(attribute["M"])
        else:
            raise ValueError("unsupported item")
    return result


def _item(values: dict[str, Any]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, value in values.items():
        if isinstance(value, str):
            output[key] = {"S": value}
        elif isinstance(value, int) and not isinstance(value, bool):
            output[key] = {"N": str(value)}
        else:
            raise TypeError("unsupported item")
    return output


def _device(event: dict[str, Any], rid: str) -> str:
    value = event.get("pathParameters", {}).get("device_id")
    if not isinstance(value, str) or DEVICE.fullmatch(value) is None:
        raise ApiError(400, "INVALID_DEVICE_ID", "Invalid device identifier.", rid)
    return value


def _membership(ddb: Any, device: str, sub: str, rid: str) -> str:
    result = ddb.get_item(
        TableName=os.environ["MEMBERSHIPS_TABLE"],
        Key=_item({"device_id": device, "user_id": sub}),
        ConsistentRead=True,
    )
    membership = _plain(result["Item"]) if result.get("Item") else {}
    if membership.get("status") != "ACTIVE" or membership.get("role") not in ROLES:
        raise ApiError(404, "RESOURCE_NOT_FOUND", "Resource not found.", rid)
    return str(membership["role"])


def _body(event: dict[str, Any], rid: str) -> tuple[dict[str, Any], bytes]:
    raw = event.get("body")
    if not isinstance(raw, str):
        raise ApiError(400, "INVALID_REQUEST", "A JSON object is required.", rid)
    try:
        encoded = (
            base64.b64decode(raw, validate=True)
            if event.get("isBase64Encoded")
            else raw.encode("utf-8")
        )
    except (UnicodeError, ValueError):
        raise ApiError(400, "INVALID_REQUEST", "A valid JSON object is required.", rid) from None
    if len(encoded) > MAX_BODY_BYTES:
        raise ApiError(413, "INVALID_REQUEST", "Request body is too large.", rid)
    try:
        value = json.loads(encoded)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ApiError(400, "INVALID_REQUEST", "A valid JSON object is required.", rid) from None
    if (
        not isinstance(value, dict)
        or not set(value) <= {"command", "parameters"}
        or "command" not in value
    ):
        raise ApiError(400, "INVALID_REQUEST", "Request fields are invalid.", rid)
    if value["command"] not in REMOTE_COMMANDS:
        raise ApiError(400, "INVALID_COMMAND", "Command is not remotely available.", rid)
    if value.get("parameters", {}) != {}:
        raise ApiError(400, "INVALID_COMMAND", "Command parameters are invalid.", rid)
    canonical = json.dumps(
        {"command": value["command"], "parameters": {}}, sort_keys=True, separators=(",", ":")
    ).encode()
    return value, canonical


def _iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _digest(*parts: str | bytes) -> str:
    digest = hashlib.sha256()
    for part in parts:
        data = part.encode() if isinstance(part, str) else part
        digest.update(len(data).to_bytes(4, "big"))
        digest.update(data)
    return digest.hexdigest()


def _transaction(
    ddb: Any, intent: dict[str, Any], sub: str, canonical: bytes, idem: str | None, now: int
) -> None:
    table = os.environ["TELEMETRY_TABLE"]
    cooldown = _digest(sub, str(intent["device_id"]))
    entries: list[dict[str, Any]] = [
        {
            "Put": {
                "TableName": table,
                "Item": _item(intent),
                "ConditionExpression": "attribute_not_exists(device_id)",
            }
        }
    ]
    if idem is not None:
        idem_digest = _digest(sub, str(intent["device_id"]), idem)
        entries.append(
            {
                "Put": {
                    "TableName": table,
                    "Item": _item(
                        {
                            "device_id": intent["device_id"],
                            "record_key": f"IDEMPOTENCY#{idem_digest}",
                            "command_id": intent["command_id"],
                            "request_digest": _digest(canonical),
                            "expires_at": now + IDEMPOTENCY_SECONDS,
                        }
                    ),
                    "ConditionExpression": "attribute_not_exists(device_id)",
                }
            }
        )
    entries.append(
        {
            "Put": {
                "TableName": table,
                "Item": _item(
                    {
                        "device_id": intent["device_id"],
                        "record_key": f"COOLDOWN#{cooldown}",
                        "available_at": now + COOLDOWN_SECONDS,
                        "expires_at": now + COOLDOWN_SECONDS,
                    }
                ),
                "ConditionExpression": "attribute_not_exists(device_id) OR available_at <= :now",
                "ExpressionAttributeValues": {":now": {"N": str(now)}},
            }
        }
    )
    ddb.transact_write_items(TransactItems=entries)


def _existing_idempotency(
    ddb: Any, device: str, sub: str, key: str, canonical: bytes, rid: str
) -> dict[str, Any] | None:
    digest = _digest(sub, device, key)
    result = ddb.get_item(
        TableName=os.environ["TELEMETRY_TABLE"],
        Key=_item({"device_id": device, "record_key": f"IDEMPOTENCY#{digest}"}),
        ConsistentRead=True,
    )
    if not result.get("Item"):
        return None
    marker = _plain(result["Item"])
    if marker.get("request_digest") != _digest(canonical):
        raise ApiError(
            409, "IDEMPOTENCY_CONFLICT", "Idempotency key was used for another request.", rid
        )
    command_id = marker.get("command_id")
    result = ddb.get_item(
        TableName=os.environ["TELEMETRY_TABLE"],
        Key=_item({"device_id": device, "record_key": f"COMMAND#{command_id}"}),
        ConsistentRead=True,
    )
    return _plain(result["Item"]) if result.get("Item") else None


def _publish(publisher: Any, intent: dict[str, Any]) -> None:
    payload = json.dumps(
        {
            "protocol_version": 1,
            "device_id": intent["device_id"],
            "command_id": intent["command_id"],
            "command": intent["command"],
            "parameters": {},
            "issued_at": intent["issued_at"],
            "expires_at": intent["command_expires_at"],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    if len(payload) > MAX_MQTT_BYTES:
        raise RuntimeError("internal MQTT payload limit exceeded")
    publisher.publish(
        topic=f"interbridge/{intent['device_id']}/commands",
        qos=1,
        retain=False,
        payload=payload,
    )


def create_command(
    event: dict[str, Any],
    context: Any,
    *,
    clock: Any = time.time,
    rng: Any = secrets.token_hex,
    clients: Any = _clients,
) -> dict[str, Any]:
    def operation(sub: str, rid: str) -> tuple[int, dict[str, Any]]:
        ddb, publisher = clients()
        device = _device(event, rid)
        role = _membership(ddb, device, sub, rid)
        if role != "OWNER":
            raise ApiError(403, "ACCESS_DENIED", "Access denied.", rid)
        value, canonical = _body(event, rid)
        headers = {str(k).lower(): v for k, v in (event.get("headers") or {}).items()}
        idem = headers.get("idempotency-key")
        if idem is not None and (
            not isinstance(idem, str)
            or not 1 <= len(idem) <= 128
            or any(unicodedata.category(c) == "Cc" for c in idem)
        ):
            raise ApiError(400, "INVALID_REQUEST", "Invalid Idempotency-Key.", rid)
        now = int(clock())
        if idem is not None:
            existing = _existing_idempotency(ddb, device, sub, idem, canonical, rid)
            if existing is not None:
                _publish(publisher, existing)
                return 202, _accepted(existing)
        command_id = rng(16)
        if not isinstance(command_id, str) or COMMAND_ID.fullmatch(command_id) is None:
            raise RuntimeError("invalid RNG output")
        intent = {
            "device_id": device,
            "record_key": f"COMMAND#{command_id}",
            "command_id": command_id,
            "command": value["command"],
            "issued_at": now,
            "command_expires_at": now + COMMAND_LIFETIME_SECONDS,
            "expires_at": now + INTENT_RETENTION_SECONDS,
        }
        try:
            _transaction(ddb, intent, sub, canonical, idem, now)
        except Exception:
            if idem is not None:
                existing = _existing_idempotency(ddb, device, sub, idem, canonical, rid)
                if existing is not None:
                    _publish(publisher, existing)
                    return 202, _accepted(existing)
            raise ApiError(
                429, "RATE_LIMITED", "Too many command requests.", rid, retry_after=COOLDOWN_SECONDS
            ) from None
        try:
            _publish(publisher, intent)
        except Exception:
            raise DependencyUnavailable from None
        return 202, _accepted(intent)

    return _run(event, "create_command", operation)


def _accepted(intent: dict[str, Any]) -> dict[str, Any]:
    return {
        "command_id": intent["command_id"],
        "state": "PENDING",
        "issued_at": _iso(intent["issued_at"]),
        "expires_at": _iso(intent["command_expires_at"]),
    }


def get_command(
    event: dict[str, Any], context: Any, *, clock: Any = time.time, clients: Any = _clients
) -> dict[str, Any]:
    def operation(sub: str, rid: str) -> tuple[int, dict[str, Any]]:
        ddb, _ = clients()
        device = _device(event, rid)
        _membership(ddb, device, sub, rid)
        command_id = event.get("pathParameters", {}).get("command_id")
        if not isinstance(command_id, str) or COMMAND_ID.fullmatch(command_id) is None:
            raise ApiError(400, "INVALID_REQUEST", "Invalid command identifier.", rid)
        result = ddb.get_item(
            TableName=os.environ["TELEMETRY_TABLE"],
            Key=_item({"device_id": device, "record_key": f"COMMAND#{command_id}"}),
            ConsistentRead=True,
        )
        if not result.get("Item"):
            raise ApiError(404, "COMMAND_NOT_FOUND", "Command not found.", rid)
        intent = _plain(result["Item"])
        if intent.get("device_id") != device or intent.get("command_id") != command_id:
            raise ApiError(404, "COMMAND_NOT_FOUND", "Command not found.", rid)
        response = _terminal_response(ddb, device, command_id)
        output = {"device_id": device, **_accepted(intent)}
        if response is None:
            output["state"] = (
                "PENDING" if int(clock()) < intent["command_expires_at"] else "EXPIRED"
            )
        elif response.get("status") == "COMPLETED":
            output["state"] = "COMPLETED"
            output["completed_at"] = response["received_at"]
        elif response.get("status") in {"FAILED", "REJECTED"}:
            output["state"] = "REJECTED"
            output["completed_at"] = response["received_at"]
            error = response.get("error")
            code = error.get("code") if isinstance(error, dict) else None
            output["rejection"] = {
                "code": code
                if isinstance(code, str) and SAFE_CODE.fullmatch(code)
                else "COMMAND_REJECTED"
            }
        return 200, output

    return _run(event, "get_command", operation)


def _terminal_response(ddb: Any, device: str, command_id: str) -> dict[str, Any] | None:
    result = ddb.query(
        TableName=os.environ["TELEMETRY_TABLE"],
        KeyConditionExpression="device_id = :device AND begins_with(record_key, :prefix)",
        ExpressionAttributeValues={":device": {"S": device}, ":prefix": {"S": "RESPONSE#"}},
        ConsistentRead=True,
        ScanIndexForward=False,
    )
    for raw in result.get("Items", []):
        response = _plain(raw)
        if response.get("record_key", "").endswith(f"#{command_id}") and response.get("status") in {
            "COMPLETED",
            "FAILED",
            "REJECTED",
        }:
            received = response.get("received_at")
            try:
                parsed = datetime.fromisoformat(str(received).replace("Z", "+00:00"))
                if parsed.tzinfo != UTC:
                    continue
            except (TypeError, ValueError):
                continue
            return response
    return None
