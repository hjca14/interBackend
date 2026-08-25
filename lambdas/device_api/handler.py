"""Device resource mutation handler: currently only ``display_name``.

Self-contained like ``lambdas.read_api.handler`` and ``lambdas.command_api.handler`` --
no import of ``domain`` beyond the single shared validator below, no shared runtime
state with those modules, and API Gateway performs JWT verification before this code
ever runs.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import time
import unicodedata
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Protocol, TypeGuard

from domain.devices.display_name import validate_display_name

LOG = logging.getLogger(__name__)
DEVICE = re.compile(r"ib-[0-9a-f]{32}\Z")
ROLES = {"OWNER", "ADMIN", "MEMBER"}
MAX_BODY_BYTES = 2 * 1024
MAX_SUB_LENGTH = 128


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, request_id: str) -> None:
        self.status, self.code, self.message, self.request_id = status, code, message, request_id


class DependencyUnavailable(RuntimeError):
    """A dependency failure whose details must not cross the API boundary."""


class DynamoDb(Protocol):
    def get_item(self, **kwargs: Any) -> dict[str, Any]: ...

    def update_item(self, **kwargs: Any) -> Any: ...


DynamoDbProvider = Callable[[], DynamoDb]
_ddb: DynamoDb | None = None


def _dynamodb_client() -> DynamoDb:
    global _ddb
    if _ddb is None:
        import boto3

        _ddb = boto3.client("dynamodb")
    return _ddb


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


def _response(status: int, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json"},
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
        )
    except DependencyUnavailable:
        LOG.error(
            json.dumps(
                {
                    "event": "device_api_failure",
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
                    "event": "device_api_failure",
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
    def value(attribute: dict[str, Any]) -> Any:
        if "S" in attribute:
            return attribute["S"]
        if "N" in attribute:
            return int(attribute["N"])
        if "BOOL" in attribute:
            return attribute["BOOL"]
        raise ValueError("unsupported DynamoDB attribute")

    return {key: value(attribute) for key, attribute in item.items()}


def _item(values: dict[str, Any]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, val in values.items():
        if isinstance(val, str):
            output[key] = {"S": val}
        elif isinstance(val, int) and not isinstance(val, bool):
            output[key] = {"N": str(val)}
        else:
            raise TypeError("unsupported item")
    return output


def _iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _error_code(error: Exception) -> str | None:
    response = getattr(error, "response", {})
    details = response.get("Error", {}) if isinstance(response, dict) else {}
    code = details.get("Code") if isinstance(details, dict) else None
    return code if isinstance(code, str) else None


def _temporary_dependency_error(error: Exception) -> bool:
    return _error_code(error) in {
        "InternalFailureException",
        "InternalServerError",
        "ProvisionedThroughputExceededException",
        "RequestLimitExceeded",
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
        if _temporary_dependency_error(error):
            raise DependencyUnavailable from None
        raise
    if not isinstance(result, dict):
        raise RuntimeError("invalid dependency response")
    return result


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


def _new_display_name(event: dict[str, Any], rid: str) -> str | None:
    """Return the trimmed new name, or ``None`` if the request clears it.

    Requires ``display_name`` to be present (possibly ``null``) and no other
    field, so intent is always explicit -- there is no "no-op" shape.
    """
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
    if not isinstance(value, dict) or set(value) != {"display_name"}:
        raise ApiError(400, "INVALID_REQUEST", "Request fields are invalid.", rid)
    raw_name = value["display_name"]
    if raw_name is None:
        return None
    if not isinstance(raw_name, str):
        raise ApiError(400, "INVALID_REQUEST", "display_name must be a string or null.", rid)
    try:
        return validate_display_name(raw_name)
    except ValueError:
        raise ApiError(400, "INVALID_REQUEST", "display_name is invalid.", rid) from None


def _device_detail(item: dict[str, Any], role: str) -> dict[str, Any]:
    out = {k: item[k] for k in ("device_id", "ownership_status", "provisioning_status")}
    out["role"] = role
    for key in ("display_name", "hardware_version"):
        if key in item:
            out[key] = item[key]
    for key in ("created_at", "updated_at"):
        if isinstance(item.get(key), int):
            out[key] = _iso(item[key])
    return out


def update_device_name(
    event: dict[str, Any],
    context: Any,
    *,
    clock: Any = time.time,
    ddb_provider: DynamoDbProvider = _dynamodb_client,
) -> dict[str, Any]:
    def operation(sub: str, rid: str) -> tuple[int, dict[str, Any]]:
        ddb = ddb_provider()
        device = _device(event, rid)
        role = _membership(ddb, device, sub, rid)
        if role != "OWNER":
            raise ApiError(403, "ACCESS_DENIED", "Access denied.", rid)
        new_name = _new_display_name(event, rid)
        now = int(clock())
        update_expression = "SET updated_at = :now" + (
            ", display_name = :dn" if new_name is not None else " REMOVE display_name"
        )
        values: dict[str, Any] = {"now": now}
        if new_name is not None:
            values["dn"] = new_name
        try:
            result = ddb.update_item(
                TableName=os.environ["DEVICES_TABLE"],
                Key=_item({"device_id": device}),
                UpdateExpression=update_expression,
                ConditionExpression="attribute_exists(device_id)",
                ExpressionAttributeValues=_item(values),
                ReturnValues="ALL_NEW",
            )
        except Exception as error:
            if _error_code(error) == "ConditionalCheckFailedException":
                # An ACTIVE membership was just confirmed above; a Device
                # item missing right after that is an internal invariant
                # violation, not a normal 404 -- mirrors read_api's
                # "authorized membership references missing device" case.
                raise RuntimeError("authorized membership references missing device") from None
            if _temporary_dependency_error(error):
                raise DependencyUnavailable from None
            raise
        updated = _plain(result["Attributes"])
        return 200, _device_detail(updated, role)

    return _run(event, "update_device_name", operation)
