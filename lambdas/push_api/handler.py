"""Authenticated, privacy-safe lifecycle API for Android FCM installations."""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import time
import uuid
from collections.abc import Callable, Mapping
from typing import Protocol, TypedDict, TypeGuard, cast

from .models import PushInstallation, token_hash

LOG = logging.getLogger(__name__)
MAX_BODY_BYTES = 8 * 1024
MAX_SUB_LENGTH = 128
MAX_TRANSACTION_ATTEMPTS = 3

type AttributeValue = dict[str, str]
type DynamoItem = dict[str, AttributeValue]


class InstallationBody(TypedDict):
    version: int
    platform: str
    push_provider: str
    token: str
    app_id: str
    app_version: str


class DynamoDb(Protocol):
    def transact_get_items(self, **kwargs: object) -> dict[str, object]: ...
    def transact_write_items(self, **kwargs: object) -> object: ...


type DynamoDbProvider = Callable[[], DynamoDb]
type Clock = Callable[[], float]
_ddb: DynamoDb | None = None


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, request_id: str) -> None:
        self.status, self.code, self.message, self.request_id = status, code, message, request_id


class DependencyFailure(RuntimeError):
    """A DynamoDB failure whose details must not cross the API boundary."""


def _client() -> DynamoDb:
    global _ddb
    if _ddb is None:
        import boto3

        _ddb = cast(DynamoDb, boto3.client("dynamodb"))
    return _ddb


def _item(values: Mapping[str, str | int]) -> DynamoItem:
    return {
        key: ({"N": str(value)} if isinstance(value, int) else {"S": value})
        for key, value in values.items()
    }


def _plain(item: object) -> dict[str, str | int]:
    if not isinstance(item, dict):
        return {}
    result: dict[str, str | int] = {}
    for key, raw in item.items():
        if not isinstance(key, str) or not isinstance(raw, dict):
            raise DependencyFailure
        if isinstance(raw.get("S"), str):
            result[key] = raw["S"]
        elif isinstance(raw.get("N"), str):
            result[key] = int(raw["N"])
        else:
            raise DependencyFailure
    return result


def _request_id(event: Mapping[str, object]) -> str:
    context = event.get("requestContext")
    value = context.get("requestId") if isinstance(context, dict) else None
    return value if isinstance(value, str) and value else str(uuid.uuid4())


def _valid_sub(value: object) -> TypeGuard[str]:
    return isinstance(value, str) and 0 < len(value) <= MAX_SUB_LENGTH and value.isprintable()


def _identity(event: Mapping[str, object], request_id: str) -> str:
    context = event.get("requestContext")
    authorizer = context.get("authorizer") if isinstance(context, dict) else None
    jwt = authorizer.get("jwt") if isinstance(authorizer, dict) else None
    claims = jwt.get("claims") if isinstance(jwt, dict) else None
    claims = claims if isinstance(claims, dict) else {}
    sub = claims.get("sub")
    if (
        not _valid_sub(sub)
        or claims.get("token_use") != "access"
        or claims.get("client_id") != os.environ["EXPECTED_APP_CLIENT_ID"]
    ):
        raise ApiError(401, "UNAUTHENTICATED", "Authentication is required.", request_id)
    return sub


def _installation_id(event: Mapping[str, object], request_id: str) -> str:
    paths = event.get("pathParameters")
    value = paths.get("installation_id") if isinstance(paths, dict) else None
    if not isinstance(value, str):
        raise ApiError(400, "INVALID_REQUEST", "Installation identifier is invalid.", request_id)
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        raise ApiError(
            400, "INVALID_REQUEST", "Installation identifier is invalid.", request_id
        ) from None
    if str(parsed) != value.lower():
        raise ApiError(400, "INVALID_REQUEST", "Installation identifier is invalid.", request_id)
    return value


def _body(event: Mapping[str, object], request_id: str) -> InstallationBody:
    raw = event.get("body")
    if not isinstance(raw, str):
        raise ApiError(400, "INVALID_REQUEST", "A JSON object is required.", request_id)
    try:
        encoded = (
            base64.b64decode(raw, validate=True)
            if event.get("isBase64Encoded") is True
            else raw.encode("utf-8")
        )
    except (UnicodeError, binascii.Error):
        raise ApiError(
            400, "INVALID_REQUEST", "A valid JSON object is required.", request_id
        ) from None
    if len(encoded) > MAX_BODY_BYTES:
        raise ApiError(413, "INVALID_REQUEST", "Request body is too large.", request_id)
    try:
        value = json.loads(encoded)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ApiError(
            400, "INVALID_REQUEST", "A valid JSON object is required.", request_id
        ) from None
    expected = {"version", "platform", "push_provider", "token", "app_id", "app_version"}
    if (
        not isinstance(value, dict)
        or set(value) != expected
        or type(value["version"]) is not int
        or value["version"] != 1
        or any(not isinstance(value[field], str) for field in expected - {"version"})
    ):
        raise ApiError(400, "INVALID_REQUEST", "Request fields are invalid.", request_id)
    return cast(InstallationBody, value)


def _installation_key(installation_id: str) -> DynamoItem:
    return _item({"pk": f"INSTALLATION#{installation_id}", "sk": "INSTALLATION"})


def _claim_key(digest: str) -> DynamoItem:
    return _item({"pk": f"TOKEN#{digest}", "sk": "CLAIM"})


def _get_pair(
    ddb: DynamoDb, installation_id: str, digest: str
) -> tuple[dict[str, str | int], dict[str, str | int]]:
    response = ddb.transact_get_items(
        TransactItems=[
            {
                "Get": {
                    "TableName": os.environ["PUSH_INSTALLATIONS_TABLE"],
                    "Key": _installation_key(installation_id),
                }
            },
            {
                "Get": {
                    "TableName": os.environ["PUSH_INSTALLATIONS_TABLE"],
                    "Key": _claim_key(digest),
                }
            },
        ]
    )
    responses = response.get("Responses")
    if not isinstance(responses, list) or len(responses) != 2:
        raise DependencyFailure
    return _plain(responses[0].get("Item") if isinstance(responses[0], dict) else None), _plain(
        responses[1].get("Item") if isinstance(responses[1], dict) else None
    )


def _condition(
    observed: dict[str, str | int], fields: tuple[str, ...]
) -> tuple[str, dict[str, AttributeValue]]:
    if not observed:
        return "attribute_not_exists(pk)", {}
    expression = " AND ".join(f"#{field} = :{field}" for field in fields)
    values = _item({f":{field}": observed[field] for field in fields})
    return expression, values


def _conflict(error: Exception) -> bool:
    response = getattr(error, "response", None)
    details = response.get("Error") if isinstance(response, dict) else None
    code = details.get("Code") if isinstance(details, dict) else None
    return code in {
        "TransactionCanceledException",
        "TransactionConflictException",
        "ConditionalCheckFailedException",
    }


def _response(status: int, body: dict[str, object] | None = None) -> dict[str, object]:
    result: dict[str, object] = {"statusCode": status, "body": ""}
    if body is not None:
        result["headers"] = {"content-type": "application/json"}
        result["body"] = json.dumps(body, separators=(",", ":"))
    return result


def _error(error: ApiError) -> dict[str, object]:
    return _response(
        error.status,
        {"error": {"code": error.code, "message": error.message, "request_id": error.request_id}},
    )


def put_installation(
    event: dict[str, object],
    context: object,
    *,
    clock: Clock = time.time,
    ddb_provider: DynamoDbProvider = _client,
) -> dict[str, object]:
    del context
    request_id = _request_id(event)
    try:
        user_id = _identity(event, request_id)
        installation_id = _installation_id(event, request_id)
        body = _body(event, request_id)
        now = int(clock())
        # Validation messages never interpolate the sensitive token.
        requested = PushInstallation(
            user_id=user_id,
            installation_id=installation_id,
            platform=body["platform"],
            push_provider=body["push_provider"],
            token=body["token"],
            app_id=body["app_id"],
            app_version=body["app_version"],
            created_at=now,
            updated_at=now,
        )
        digest = token_hash(requested.token)
        ddb = ddb_provider()
        for attempt in range(MAX_TRANSACTION_ATTEMPTS):
            current, claim = _get_pair(ddb, installation_id, digest)
            created_at = current.get("created_at", now)
            if not isinstance(created_at, int):
                raise DependencyFailure
            model = PushInstallation(
                user_id,
                installation_id,
                requested.platform,
                requested.push_provider,
                requested.token,
                requested.app_id,
                requested.app_version,
                created_at,
                now,
            )
            install_condition, install_values = _condition(current, ("user_id", "token_hash"))
            claim_condition, claim_values = _condition(
                claim, ("claimed_installation_id", "claimed_user_id")
            )
            actions: list[dict[str, object]] = []
            old_hash = current.get("token_hash")
            if isinstance(old_hash, str) and old_hash != digest:
                actions.append(
                    {
                        "Delete": {
                            "TableName": os.environ["PUSH_INSTALLATIONS_TABLE"],
                            "Key": _claim_key(old_hash),
                            "ConditionExpression": (
                                "claimed_installation_id = :iid AND claimed_user_id = :uid"
                            ),
                            "ExpressionAttributeValues": _item(
                                {":iid": installation_id, ":uid": current["user_id"]}
                            ),
                        }
                    }
                )
            actions.extend(
                [
                    {
                        "Put": {
                            "TableName": os.environ["PUSH_INSTALLATIONS_TABLE"],
                            "Item": _item(
                                {
                                    "pk": f"INSTALLATION#{installation_id}",
                                    "sk": "INSTALLATION",
                                    **model.to_item(),
                                }
                            ),
                            "ConditionExpression": install_condition,
                            **(
                                {
                                    "ExpressionAttributeNames": {
                                        f"#{name}": name for name in ("user_id", "token_hash")
                                    },
                                    "ExpressionAttributeValues": install_values,
                                }
                                if install_values
                                else {}
                            ),
                        }
                    },
                    {
                        "Put": {
                            "TableName": os.environ["PUSH_INSTALLATIONS_TABLE"],
                            "Item": _item(
                                {
                                    "pk": f"TOKEN#{digest}",
                                    "sk": "CLAIM",
                                    "token_hash": digest,
                                    "claimed_installation_id": installation_id,
                                    "claimed_user_id": user_id,
                                    "updated_at": now,
                                }
                            ),
                            "ConditionExpression": claim_condition,
                            **(
                                {
                                    "ExpressionAttributeNames": {
                                        f"#{name}": name
                                        for name in ("claimed_installation_id", "claimed_user_id")
                                    },
                                    "ExpressionAttributeValues": claim_values,
                                }
                                if claim_values
                                else {}
                            ),
                        }
                    },
                ]
            )
            # If this token belonged elsewhere, invalidate that installation in the same commit.
            prior_id = claim.get("claimed_installation_id")
            if isinstance(prior_id, str) and prior_id != installation_id:
                actions.insert(
                    0,
                    {
                        "Delete": {
                            "TableName": os.environ["PUSH_INSTALLATIONS_TABLE"],
                            "Key": _installation_key(prior_id),
                            "ConditionExpression": "token_hash = :hash AND user_id = :uid",
                            "ExpressionAttributeValues": _item(
                                {":hash": digest, ":uid": claim["claimed_user_id"]}
                            ),
                        }
                    },
                )
            try:
                ddb.transact_write_items(TransactItems=actions)
                return _response(204)
            except Exception as error:
                if not _conflict(error) or attempt + 1 == MAX_TRANSACTION_ATTEMPTS:
                    if _conflict(error):
                        raise ApiError(
                            409, "CONFLICT", "The installation changed concurrently.", request_id
                        ) from None
                    raise DependencyFailure from None
        raise DependencyFailure
    except ApiError as error:
        return _error(error)
    except (ValueError, TypeError):
        return _error(ApiError(400, "INVALID_REQUEST", "Request fields are invalid.", request_id))
    except Exception:
        LOG.error(
            json.dumps(
                {
                    "event": "push_installation_failure",
                    "operation": "put",
                    "request_id": request_id,
                    "error_code": "DEPENDENCY_FAILURE",
                }
            )
        )
        return _error(
            ApiError(
                503,
                "SERVICE_UNAVAILABLE",
                "A required service is temporarily unavailable.",
                request_id,
            )
        )


def delete_installation(
    event: dict[str, object], context: object, *, ddb_provider: DynamoDbProvider = _client
) -> dict[str, object]:
    del context
    request_id = _request_id(event)
    try:
        user_id = _identity(event, request_id)
        installation_id = _installation_id(event, request_id)
        if event.get("body") not in (None, "") or event.get("queryStringParameters"):
            raise ApiError(
                400,
                "INVALID_REQUEST",
                "DELETE does not accept body or query parameters.",
                request_id,
            )
        ddb = ddb_provider()
        for attempt in range(MAX_TRANSACTION_ATTEMPTS):
            current, _ = _get_pair(ddb, installation_id, "missing")
            if not current or current.get("user_id") != user_id:
                return _response(204)
            digest = current.get("token_hash")
            if not isinstance(digest, str):
                raise DependencyFailure
            try:
                ddb.transact_write_items(
                    TransactItems=[
                        {
                            "Delete": {
                                "TableName": os.environ["PUSH_INSTALLATIONS_TABLE"],
                                "Key": _installation_key(installation_id),
                                "ConditionExpression": "user_id = :uid AND token_hash = :hash",
                                "ExpressionAttributeValues": _item(
                                    {":uid": user_id, ":hash": digest}
                                ),
                            }
                        },
                        {
                            "Delete": {
                                "TableName": os.environ["PUSH_INSTALLATIONS_TABLE"],
                                "Key": _claim_key(digest),
                                "ConditionExpression": (
                                    "claimed_installation_id = :iid AND claimed_user_id = :uid"
                                ),
                                "ExpressionAttributeValues": _item(
                                    {":iid": installation_id, ":uid": user_id}
                                ),
                            }
                        },
                    ]
                )
                return _response(204)
            except Exception as error:
                if not _conflict(error) or attempt + 1 == MAX_TRANSACTION_ATTEMPTS:
                    if _conflict(error):
                        raise ApiError(
                            409, "CONFLICT", "The installation changed concurrently.", request_id
                        ) from None
                    raise DependencyFailure from None
        raise DependencyFailure
    except ApiError as error:
        return _error(error)
    except Exception:
        LOG.error(
            json.dumps(
                {
                    "event": "push_installation_failure",
                    "operation": "delete",
                    "request_id": request_id,
                    "error_code": "DEPENDENCY_FAILURE",
                }
            )
        )
        return _error(
            ApiError(
                503,
                "SERVICE_UNAVAILABLE",
                "A required service is temporarily unavailable.",
                request_id,
            )
        )
