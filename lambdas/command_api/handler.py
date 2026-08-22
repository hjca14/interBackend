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
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Protocol, TypeGuard

LOG = logging.getLogger(__name__)
DEVICE = re.compile(r"ib-[0-9a-f]{32}\Z")
COMMAND_ID = re.compile(r"[0-9a-f]{32}\Z")
SAFE_CODE = re.compile(r"[A-Za-z0-9._-]{1,64}\Z")
ROLES = {"OWNER", "ADMIN", "MEMBER"}
REMOTE_COMMANDS = {"OPEN_DOOR"}
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

    def __init__(self, step: str) -> None:
        super().__init__(step)
        self.step = step


class DependencyFailure(RuntimeError):
    """A non-transient dependency failure with a safe internal diagnostic step."""

    def __init__(self, step: str) -> None:
        super().__init__(step)
        self.step = step


class Publisher(Protocol):
    def publish(self, **kwargs: Any) -> Any: ...


DynamoDbProvider = Callable[[], Any]
PublisherProvider = Callable[[], Publisher]


def _dynamodb_client() -> Any:
    global _ddb
    if _ddb is None:
        import boto3

        try:
            _ddb = boto3.client("dynamodb")
        except Exception as error:
            exception = (
                DependencyUnavailable if _temporary_dependency_error(error) else DependencyFailure
            )
            raise exception("DDB_CLIENT_INIT") from None
    return _ddb


def _iot_publisher() -> Publisher:
    global _publisher
    if _publisher is None:
        import boto3

        try:
            endpoint = (
                boto3.client("iot")
                .describe_endpoint(endpointType="iot:Data-ATS")
                .get("endpointAddress")
            )
        except Exception as error:
            if _temporary_dependency_error(error):
                raise DependencyUnavailable("IOT_ENDPOINT_DISCOVERY") from None
            raise DependencyFailure("IOT_ENDPOINT_DISCOVERY") from None
        if not isinstance(endpoint, str) or not endpoint.endswith(".amazonaws.com"):
            raise DependencyUnavailable("IOT_ENDPOINT_VALIDATION")
        try:
            _publisher = boto3.client("iot-data", endpoint_url=f"https://{endpoint}")
        except Exception as error:
            exception = (
                DependencyUnavailable if _temporary_dependency_error(error) else DependencyFailure
            )
            raise exception("IOT_DATA_CLIENT_INIT") from None
    return _publisher


def _valid_sub(value: object) -> TypeGuard[str]:
    return (
        isinstance(value, str)
        and 0 < len(value) <= MAX_SUB_LENGTH
        and all(unicodedata.category(character) != "Cc" for character in value)
    )


def _request(event: dict[str, Any]) -> tuple[str, str]:
    request_id = str(event.get("requestContext", {}).get("requestId") or uuid.uuid4())
    raw_claims = (
        event.get("requestContext", {}).get("authorizer", {}).get("jwt", {}).get("claims", {})
    )
    claims = raw_claims if isinstance(raw_claims, dict) else {}
    sub = claims.get("sub")
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
    except DependencyUnavailable as error:
        LOG.error(
            json.dumps(
                {
                    "event": "command_api_failure",
                    "request_id": request_id,
                    "operation": operation,
                    "error_code": "DEPENDENCY_UNAVAILABLE",
                    "dependency_step": error.step,
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
    except DependencyFailure as error:
        LOG.error(
            json.dumps(
                {
                    "event": "command_api_failure",
                    "request_id": request_id,
                    "operation": operation,
                    "error_code": "DEPENDENCY_FAILURE",
                    "dependency_step": error.step,
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
    except Exception:
        LOG.error(
            json.dumps(
                {
                    "event": "command_api_failure",
                    "request_id": request_id,
                    "operation": operation,
                    "error_code": "INTERNAL_FAILURE",
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
        elif isinstance(value, dict):
            output[key] = {"M": _item(value)}
        else:
            raise TypeError("unsupported item")
    return output


def _device(event: dict[str, Any], rid: str) -> str:
    raw_paths = event.get("pathParameters")
    paths = raw_paths if isinstance(raw_paths, dict) else {}
    value = paths.get("device_id")
    if not isinstance(value, str) or DEVICE.fullmatch(value) is None:
        raise ApiError(400, "INVALID_DEVICE_ID", "Invalid device identifier.", rid)
    return value


def _membership(ddb: Any, device: str, sub: str, rid: str) -> str:
    result = _get_item(
        ddb,
        TableName=os.environ["MEMBERSHIPS_TABLE"],
        Key=_item({"device_id": device, "user_id": sub}),
        ConsistentRead=True,
    )
    membership = _plain(result["Item"]) if result.get("Item") else {}
    if membership.get("status") != "ACTIVE" or membership.get("role") not in ROLES:
        raise ApiError(404, "RESOURCE_NOT_FOUND", "Resource not found.", rid)
    return str(membership["role"])


def _device_ready(ddb: Any, device: str, rid: str) -> None:
    result = _get_item(
        ddb,
        TableName=os.environ["DEVICES_TABLE"],
        Key=_item({"device_id": device}),
        ConsistentRead=True,
    )
    item = _plain(result["Item"]) if result.get("Item") else {}
    if (
        item.get("device_id") != device
        or item.get("ownership_status") != "OWNED"
        or item.get("provisioning_status") != "PROVISIONED"
    ):
        raise ApiError(404, "RESOURCE_NOT_FOUND", "Resource not found.", rid)


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
                    "ConditionExpression": "attribute_not_exists(device_id) OR expires_at <= :now",
                    "ExpressionAttributeValues": {":now": {"N": str(now)}},
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
    ddb: Any, device: str, sub: str, key: str, canonical: bytes, rid: str, now: int
) -> dict[str, Any] | None:
    digest = _digest(sub, device, key)
    result = _get_item(
        ddb,
        TableName=os.environ["TELEMETRY_TABLE"],
        Key=_item({"device_id": device, "record_key": f"IDEMPOTENCY#{digest}"}),
        ConsistentRead=True,
    )
    if not result.get("Item"):
        return None
    marker = _plain(result["Item"])
    if not isinstance(marker.get("expires_at"), int) or marker["expires_at"] <= now:
        return None
    if marker.get("request_digest") != _digest(canonical):
        raise ApiError(
            409, "IDEMPOTENCY_CONFLICT", "Idempotency key was used for another request.", rid
        )
    command_id = marker.get("command_id")
    result = _get_item(
        ddb,
        TableName=os.environ["TELEMETRY_TABLE"],
        Key=_item({"device_id": device, "record_key": f"COMMAND#{command_id}"}),
        ConsistentRead=True,
    )
    return _plain(result["Item"]) if result.get("Item") else None


def _error_code(error: Exception) -> str | None:
    response = getattr(error, "response", {})
    details = response.get("Error", {}) if isinstance(response, dict) else {}
    code = details.get("Code") if isinstance(details, dict) else None
    return code if isinstance(code, str) else None


def _temporary_dependency_error(error: Exception) -> bool:
    return _error_code(error) in {
        "InternalFailureException",
        "InternalServerError",
        "RequestTimeout",
        "ServiceUnavailableException",
        "ThrottlingException",
    } or type(error).__name__ in {
        "ConnectTimeoutError",
        "EndpointConnectionError",
        "ReadTimeoutError",
    }


def _get_item(ddb: Any, **kwargs: Any) -> dict[str, Any]:
    try:
        result = ddb.get_item(**kwargs)
    except Exception as error:
        if _error_code(error) in {
            "InternalServerError",
            "ProvisionedThroughputExceededException",
            "RequestLimitExceeded",
            "ThrottlingException",
        }:
            raise DependencyUnavailable("DDB_GET_ITEM") from None
        raise
    if not isinstance(result, dict):
        raise RuntimeError("invalid dependency response")
    return result


def _cancellation_reasons(error: Exception) -> list[dict[str, Any]]:
    response = getattr(error, "response", {})
    reasons = response.get("CancellationReasons", []) if isinstance(response, dict) else []
    return (
        reasons if isinstance(reasons, list) and all(isinstance(v, dict) for v in reasons) else []
    )


def _transaction_failure(
    error: Exception,
    *,
    ddb: Any,
    device: str,
    sub: str,
    idem: str | None,
    canonical: bytes,
    rid: str,
    now: int,
) -> dict[str, Any]:
    code = _error_code(error)
    if code != "TransactionCanceledException":
        if code in {
            "InternalServerError",
            "ProvisionedThroughputExceededException",
            "RequestLimitExceeded",
            "ThrottlingException",
            "TransactionConflictException",
        }:
            raise DependencyUnavailable("DDB_TRANSACTION") from None
        raise error
    reasons = _cancellation_reasons(error)
    if not reasons:
        raise DependencyUnavailable("DDB_TRANSACTION_RESPONSE") from None
    marker_index = 1 if idem is not None else None
    cooldown_index = 2 if idem is not None else 1
    if (
        marker_index is not None
        and len(reasons) > marker_index
        and reasons[marker_index].get("Code") == "ConditionalCheckFailed"
    ):
        assert idem is not None
        existing = _existing_idempotency(ddb, device, sub, idem, canonical, rid, now)
        if existing is not None:
            return existing
    if (
        len(reasons) > cooldown_index
        and reasons[cooldown_index].get("Code") == "ConditionalCheckFailed"
    ):
        raise ApiError(
            429, "RATE_LIMITED", "Too many command requests.", rid, retry_after=COOLDOWN_SECONDS
        ) from None
    if any(
        reason.get("Code") in {"TransactionConflict", "ThrottlingError", "InternalServerError"}
        for reason in reasons
    ):
        raise DependencyUnavailable("DDB_TRANSACTION") from None
    raise error


def _mark_published(ddb: Any, intent: dict[str, Any]) -> None:
    ddb.update_item(
        TableName=os.environ["TELEMETRY_TABLE"],
        Key=_item(
            {"device_id": intent["device_id"], "record_key": f"COMMAND#{intent['command_id']}"}
        ),
        UpdateExpression="SET publish_state = :published",
        ConditionExpression="publish_state = :pending",
        ExpressionAttributeValues={
            ":pending": {"S": "PUBLISH_PENDING"},
            ":published": {"S": "PUBLISHED"},
        },
    )


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


def _publish_pending(ddb: Any, publisher: Any, intent: dict[str, Any], now: int) -> None:
    if intent.get("publish_state") == "PUBLISHED":
        return
    if intent.get("publish_state") != "PUBLISH_PENDING":
        raise RuntimeError("invalid publish state")
    if not isinstance(intent.get("command_expires_at"), int) or intent["command_expires_at"] <= now:
        return
    try:
        _publish(publisher, intent)
    except Exception:
        raise DependencyUnavailable("IOT_PUBLISH") from None
    try:
        _mark_published(ddb, intent)
    except Exception as error:
        if _error_code(error) in {
            "InternalServerError",
            "ProvisionedThroughputExceededException",
            "RequestLimitExceeded",
            "ThrottlingException",
        }:
            raise DependencyUnavailable("DDB_MARK_PUBLISHED") from None
        raise
    intent["publish_state"] = "PUBLISHED"


def create_command(
    event: dict[str, Any],
    context: Any,
    *,
    clock: Any = time.time,
    rng: Any = secrets.token_hex,
    ddb_provider: DynamoDbProvider = _dynamodb_client,
    publisher_provider: PublisherProvider = _iot_publisher,
) -> dict[str, Any]:
    def operation(sub: str, rid: str) -> tuple[int, dict[str, Any]]:
        ddb = ddb_provider()
        publisher = publisher_provider()
        device = _device(event, rid)
        role = _membership(ddb, device, sub, rid)
        if role != "OWNER":
            raise ApiError(403, "ACCESS_DENIED", "Access denied.", rid)
        _device_ready(ddb, device, rid)
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
            existing = _existing_idempotency(ddb, device, sub, idem, canonical, rid, now)
            if existing is not None:
                _publish_pending(ddb, publisher, existing, now)
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
            "publish_state": "PUBLISH_PENDING",
        }
        try:
            _transaction(ddb, intent, sub, canonical, idem, now)
        except Exception as error:
            existing = _transaction_failure(
                error,
                ddb=ddb,
                device=device,
                sub=sub,
                idem=idem,
                canonical=canonical,
                rid=rid,
                now=now,
            )
            _publish_pending(ddb, publisher, existing, now)
            return 202, _accepted(existing)
        _publish_pending(ddb, publisher, intent, now)
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
    event: dict[str, Any],
    context: Any,
    *,
    clock: Any = time.time,
    ddb_provider: DynamoDbProvider = _dynamodb_client,
) -> dict[str, Any]:
    def operation(sub: str, rid: str) -> tuple[int, dict[str, Any]]:
        ddb = ddb_provider()
        device = _device(event, rid)
        _membership(ddb, device, sub, rid)
        _device_ready(ddb, device, rid)
        raw_paths = event.get("pathParameters")
        paths = raw_paths if isinstance(raw_paths, dict) else {}
        command_id = paths.get("command_id")
        if not isinstance(command_id, str) or COMMAND_ID.fullmatch(command_id) is None:
            raise ApiError(400, "INVALID_REQUEST", "Invalid command identifier.", rid)
        result = _get_item(
            ddb,
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
    result = _get_item(
        ddb,
        TableName=os.environ["TELEMETRY_TABLE"],
        Key=_item({"device_id": device, "record_key": f"COMMAND_RESULT#{command_id}"}),
        ConsistentRead=True,
    )
    if not result.get("Item"):
        return None
    response = _plain(result["Item"])
    received = response.get("received_at")
    try:
        parsed = datetime.fromisoformat(str(received).replace("Z", "+00:00"))
        if parsed.tzinfo != UTC:
            return None
    except (TypeError, ValueError):
        return None
    return response
