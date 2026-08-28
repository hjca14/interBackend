"""Authenticated lifecycle API for Android FCM installations."""

from __future__ import annotations

import base64
import json
import logging
import os
import time
import uuid
from typing import Any

from .models import PushInstallation, token_hash

LOG = logging.getLogger(__name__)
MAX_BODY_BYTES = 8 * 1024
_ddb = None


def _client():
    global _ddb
    if _ddb is None:
        import boto3

        _ddb = boto3.client("dynamodb")
    return _ddb


def _av(values: dict[str, object]) -> dict[str, dict[str, str]]:
    return {k: ({"N": str(v)} if isinstance(v, int) else {"S": str(v)}) for k, v in values.items()}


def _plain(item):
    return {k: next(iter(v.values())) for k, v in item.items()}


def _rid(e):
    return str(e.get("requestContext", {}).get("requestId") or uuid.uuid4())


def _error(status, code, message, rid):
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(
            {"error": {"code": code, "message": message, "request_id": rid}}, separators=(",", ":")
        ),
    }


def _identity(e, rid):
    c = e.get("requestContext", {}).get("authorizer", {}).get("jwt", {}).get("claims", {})
    sub = c.get("sub") if isinstance(c, dict) else None
    if (
        not isinstance(sub, str)
        or not sub
        or c.get("token_use") != "access"
        or c.get("client_id") != os.environ["EXPECTED_APP_CLIENT_ID"]
    ):
        raise ValueError("auth")
    return sub


def _iid(e):
    value = (e.get("pathParameters") or {}).get("installation_id")
    parsed = uuid.UUID(value)
    assert str(parsed) == value.lower()
    return value


def _body(e):
    raw = e.get("body")
    if not isinstance(raw, str):
        raise ValueError("body")
    data = base64.b64decode(raw, validate=True) if e.get("isBase64Encoded") else raw.encode()
    if len(data) > MAX_BODY_BYTES:
        raise OverflowError
    obj = json.loads(data)
    expected = {"version", "platform", "push_provider", "token", "app_id", "app_version"}
    if not isinstance(obj, dict) or set(obj) != expected or obj["version"] != 1:
        raise ValueError("body")
    return obj


def put_installation(event: dict[str, Any], context: Any, *, clock=time.time, ddb_provider=_client):
    rid = _rid(event)
    try:
        user = _identity(event, rid)
        iid = _iid(event)
        body = _body(event)
        ddb = ddb_provider()
        now = int(clock())
        key = _av({"user_id": user, "installation_id": iid})
        old = ddb.get_item(
            TableName=os.environ["PUSH_INSTALLATIONS_TABLE"], Key=key, ConsistentRead=True
        ).get("Item")
        created = int(_plain(old)["created_at"]) if old else now
        model = PushInstallation(
            user,
            iid,
            body["platform"],
            body["push_provider"],
            body["token"],
            body["app_id"],
            body["app_version"],
            created,
            now,
        )
        digest = token_hash(model.token)
        found = ddb.query(
            TableName=os.environ["PUSH_INSTALLATIONS_TABLE"],
            IndexName=os.environ["PUSH_TOKEN_INDEX"],
            KeyConditionExpression="token_hash = :h",
            ExpressionAttributeValues={":h": {"S": digest}},
            ProjectionExpression="user_id, installation_id",
        ).get("Items", [])
        actions = []
        for candidate in found:
            prior = _plain(candidate)
            if (prior["user_id"], prior["installation_id"]) != (user, iid):
                actions.append(
                    {
                        "Delete": {
                            "TableName": os.environ["PUSH_INSTALLATIONS_TABLE"],
                            "Key": _av(prior),
                            "ConditionExpression": "token_hash = :h",
                            "ExpressionAttributeValues": {":h": {"S": digest}},
                        }
                    }
                )
        actions.append(
            {
                "Put": {
                    "TableName": os.environ["PUSH_INSTALLATIONS_TABLE"],
                    "Item": _av(model.to_item()),
                }
            }
        )
        ddb.transact_write_items(TransactItems=actions)
        return {"statusCode": 204, "body": ""}
    except OverflowError:
        return _error(413, "INVALID_REQUEST", "Request body is too large.", rid)
    except (ValueError, TypeError, AssertionError, json.JSONDecodeError, UnicodeError):
        code = "UNAUTHENTICATED" if "user" not in locals() else "INVALID_REQUEST"
        return _error(
            401 if code == "UNAUTHENTICATED" else 400,
            code,
            "Authentication is required." if code == "UNAUTHENTICATED" else "Request is invalid.",
            rid,
        )
    except Exception:
        LOG.error(
            json.dumps(
                {
                    "event": "push_installation_failure",
                    "operation": "put",
                    "request_id": rid,
                    "error_code": "DEPENDENCY_FAILURE",
                }
            )
        )
        return _error(
            503, "SERVICE_UNAVAILABLE", "A required service is temporarily unavailable.", rid
        )


def delete_installation(event: dict[str, Any], context: Any, *, ddb_provider=_client):
    rid = _rid(event)
    try:
        user = _identity(event, rid)
        iid = _iid(event)
        if event.get("body") not in (None, "") or event.get("queryStringParameters"):
            raise ValueError("body")
        ddb_provider().delete_item(
            TableName=os.environ["PUSH_INSTALLATIONS_TABLE"],
            Key=_av({"user_id": user, "installation_id": iid}),
        )
        return {"statusCode": 204, "body": ""}
    except (ValueError, TypeError, AssertionError):
        code = "UNAUTHENTICATED" if "user" not in locals() else "INVALID_REQUEST"
        return _error(
            401 if code == "UNAUTHENTICATED" else 400,
            code,
            "Authentication is required." if code == "UNAUTHENTICATED" else "Request is invalid.",
            rid,
        )
    except Exception:
        LOG.error(
            json.dumps(
                {
                    "event": "push_installation_failure",
                    "operation": "delete",
                    "request_id": rid,
                    "error_code": "DEPENDENCY_FAILURE",
                }
            )
        )
        return _error(
            503, "SERVICE_UNAVAILABLE", "A required service is temporarily unavailable.", rid
        )
