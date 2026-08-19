from __future__ import annotations

import base64
import json
import sys
from datetime import UTC, datetime, timedelta
from types import ModuleType
from typing import Any

import pytest

from lambdas.read_api import handler

SUB = "11111111-1111-4111-8111-111111111111"
OTHER_SUB = "22222222-2222-4222-8222-222222222222"
DEVICE = "ib-" + "a" * 32
CLIENT = "public-client"


def av(item: dict[str, object]) -> dict[str, dict[str, object]]:
    return {
        key: ({"N": str(value)} if isinstance(value, int) else {"S": value})
        for key, value in item.items()
    }


class FakeKms:
    def __init__(self) -> None:
        self.values: dict[tuple[bytes, tuple[tuple[str, str], ...]], bytes] = {}
        self.failure: Exception | None = None
        self.response: dict[str, bytes] | None = None

    def encrypt(self, **kwargs: Any) -> dict[str, bytes]:
        raw = bytes(kwargs["Plaintext"])
        context = tuple(sorted(kwargs["EncryptionContext"].items()))
        cipher = b"KMS-CIPHERTEXT-" + len(self.values).to_bytes(2, "big")
        self.values[(cipher, context)] = raw
        return {"CiphertextBlob": cipher}

    def decrypt(self, **kwargs: Any) -> dict[str, bytes]:
        if self.failure:
            raise self.failure
        if self.response is not None:
            return self.response
        context = tuple(sorted(kwargs["EncryptionContext"].items()))
        try:
            return {"Plaintext": self.values[(bytes(kwargs["CiphertextBlob"]), context)]}
        except KeyError as exc:
            raise FakeClientError("InvalidCiphertextException", "sensitive KMS detail") from exc


class FakeClientError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.response = {"Error": {"Code": code, "Message": message}}


class ReadTimeoutError(Exception):
    pass


class ConnectTimeoutError(Exception):
    pass


class EndpointConnectionError(Exception):
    pass


class FakeDdb:
    def __init__(self) -> None:
        self.membership: dict[str, object] | None = None
        self.device: dict[str, object] | None = None
        self.telemetry: dict[str, object] | None = None
        self.query_items: list[dict[str, object]] = []
        self.last_key: dict[str, Any] | None = None
        self.batch_results: list[dict[str, Any]] = []
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.error: Exception | None = None

    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("get_item", kwargs))
        if self.error:
            raise self.error
        table = kwargs["TableName"]
        item = (
            self.membership
            if table == "memberships"
            else self.device
            if table == "devices"
            else self.telemetry
        )
        return {"Item": av(item)} if item else {}

    def query(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("query", kwargs))
        result: dict[str, Any] = {"Items": [av(i) for i in self.query_items]}
        if self.last_key:
            result["LastEvaluatedKey"] = self.last_key
        return result

    def batch_get_item(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("batch_get_item", kwargs))
        if self.batch_results:
            return self.batch_results.pop(0)
        return {"Responses": {"devices": [av(self.device)] if self.device else []}}


@pytest.fixture(autouse=True)
def configured(monkeypatch: pytest.MonkeyPatch) -> tuple[FakeDdb, FakeKms]:
    botocore = ModuleType("botocore")
    exceptions = ModuleType("botocore.exceptions")
    exceptions.ClientError = FakeClientError  # type: ignore[attr-defined]
    exceptions.ConnectTimeoutError = ConnectTimeoutError  # type: ignore[attr-defined]
    exceptions.EndpointConnectionError = EndpointConnectionError  # type: ignore[attr-defined]
    exceptions.ReadTimeoutError = ReadTimeoutError  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "botocore", botocore)
    monkeypatch.setitem(sys.modules, "botocore.exceptions", exceptions)
    for key, value in {
        "DEVICES_TABLE": "devices",
        "MEMBERSHIPS_TABLE": "memberships",
        "MEMBERSHIPS_INDEX": "by-user",
        "TELEMETRY_TABLE": "telemetry",
        "CURSOR_KEY_ARN": "key",
        "EXPECTED_APP_CLIENT_ID": CLIENT,
    }.items():
        monkeypatch.setenv(key, value)
    ddb, kms = FakeDdb(), FakeKms()
    monkeypatch.setattr(handler, "_ddb", ddb)
    monkeypatch.setattr(handler, "_kms", kms)
    return ddb, kms


def event(
    *,
    sub: str | None = SUB,
    token_use: str | None = "access",
    client_id: str | None = CLIENT,
    device: str | None = None,
    query: dict[str, str] | None = None,
) -> dict[str, Any]:
    claims = {
        k: v
        for k, v in {"sub": sub, "token_use": token_use, "client_id": client_id}.items()
        if v is not None
    }
    value: dict[str, Any] = {
        "requestContext": {"requestId": "request-1", "authorizer": {"jwt": {"claims": claims}}}
    }
    if device is not None:
        value["pathParameters"] = {"device_id": device}
    if query is not None:
        value["queryStringParameters"] = query
    return value


def body(response: dict[str, Any]) -> dict[str, Any]:
    return json.loads(response["body"])


@pytest.mark.parametrize(
    "bad_event",
    [
        {},
        {"requestContext": {}},
        event(sub=None),
        event(sub="bad"),
        event(token_use="id"),
        event(token_use=None),
        event(client_id="wrong"),
        event(client_id=None),
    ],
)
def test_rejects_missing_or_non_access_jwt(bad_event: dict[str, Any]) -> None:
    response = handler.list_devices(bad_event, None)
    assert response["statusCode"] == 401 and body(response)["error"]["code"] == "UNAUTHENTICATED"


def test_valid_access_token_and_empty_list(configured: tuple[FakeDdb, FakeKms]) -> None:
    response = handler.list_devices(event(), None)
    assert response["statusCode"] == 200 and body(response) == {"items": []}


@pytest.mark.parametrize(
    "membership",
    [
        None,
        {"status": "REMOVED", "role": "OWNER"},
        {"status": "UNKNOWN", "role": "OWNER"},
        {"status": "ACTIVE", "role": "UNKNOWN"},
    ],
)
def test_membership_failures_are_indistinguishable_404(
    configured: tuple[FakeDdb, FakeKms], membership: dict[str, object] | None
) -> None:
    ddb, _ = configured
    ddb.membership = membership
    response = handler.get_device(event(device=DEVICE), None)
    assert response["statusCode"] == 404 and body(response)["error"]["code"] == "RESOURCE_NOT_FOUND"
    assert all(call[1]["TableName"] != "devices" for call in ddb.calls)


@pytest.mark.parametrize("role", ["OWNER", "ADMIN", "MEMBER"])
def test_all_documented_roles_can_read_detail(
    configured: tuple[FakeDdb, FakeKms], role: str
) -> None:
    ddb, _ = configured
    ddb.membership = {"device_id": DEVICE, "user_id": SUB, "status": "ACTIVE", "role": role}
    ddb.device = {
        "device_id": DEVICE,
        "ownership_status": "OWNED",
        "provisioning_status": "PROVISIONED",
        "hardware_version": "1",
        "owner_user_id": SUB,
        "aws_thing_name": DEVICE,
    }
    result = body(handler.get_device(event(device=DEVICE), None))
    assert result == {
        "device_id": DEVICE,
        "ownership_status": "OWNED",
        "provisioning_status": "PROVISIONED",
        "hardware_version": "1",
        "role": role,
    }
    assert "owner_user_id" not in result and "aws_thing_name" not in result


def test_invalid_device_and_authorized_missing_device(configured: tuple[FakeDdb, FakeKms]) -> None:
    assert handler.get_device(event(device="bad"), None)["statusCode"] == 400
    ddb, _ = configured
    ddb.membership = {"status": "ACTIVE", "role": "OWNER"}
    response = handler.get_device(event(device=DEVICE), None)
    assert (
        response["statusCode"] == 500
        and body(response)["error"]["message"] == "An internal error occurred."
    )


def test_list_order_partial_batch_and_pagination_cursor_is_confidential(
    configured: tuple[FakeDdb, FakeKms],
) -> None:
    ddb, kms = configured
    second = "ib-" + "b" * 32
    ddb.query_items = [
        {"device_id": DEVICE, "user_id": SUB, "status": "ACTIVE", "role": "OWNER"},
        {"device_id": second, "user_id": SUB, "status": "ACTIVE", "role": "MEMBER"},
    ]
    ddb.last_key = {"user_id": {"S": SUB}, "device_id": {"S": second}}
    ddb.batch_results = [
        {"Responses": {"devices": [av({"device_id": second})]}, "UnprocessedKeys": {}}
    ]
    result = body(handler.list_devices(event(query={"limit": "2"}), None, sleeper=lambda _: None))
    assert [item["device_id"] for item in result["items"]] == [second]
    decoded = base64.urlsafe_b64decode(result["next_cursor"] + "==")
    assert (
        SUB.encode() not in decoded and second.encode() not in decoded and b"user_id" not in decoded
    )
    followup = handler.list_devices(
        event(query={"limit": "2", "cursor": result["next_cursor"]}), None
    )
    assert followup["statusCode"] == 200
    assert any("ExclusiveStartKey" in kwargs for name, kwargs in ddb.calls if name == "query")
    assert kms.values


def test_cursor_tampering_user_and_limit_are_rejected(configured: tuple[FakeDdb, FakeKms]) -> None:
    _, kms = configured
    token = handler._cursor_encode(
        kms, SUB, 25, {"device_id": {"S": DEVICE}, "user_id": {"S": SUB}}
    )
    for changed in (("A" if token[0] != "A" else "B") + token[1:],):
        assert handler.list_devices(event(query={"cursor": changed}), None)["statusCode"] == 400
    assert (
        handler.list_devices(event(sub=OTHER_SUB, query={"cursor": token}), None)["statusCode"]
        == 400
    )
    assert (
        handler.list_devices(event(query={"limit": "10", "cursor": token}), None)["statusCode"]
        == 400
    )


def test_cursor_kms_missing_or_malformed_plaintext_is_rejected(
    configured: tuple[FakeDdb, FakeKms], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, kms = configured
    token = handler._cursor_encode(
        kms, SUB, 25, {"device_id": {"S": DEVICE}, "user_id": {"S": SUB}}
    )
    monkeypatch.setattr(kms, "decrypt", lambda **kwargs: {})
    assert handler.list_devices(event(query={"cursor": token}), None)["statusCode"] == 500
    monkeypatch.setattr(kms, "decrypt", lambda **kwargs: {"Plaintext": b"not-json"})
    assert handler.list_devices(event(query={"cursor": token}), None)["statusCode"] == 400


@pytest.mark.parametrize(
    "failure,status,code",
    [
        (FakeClientError("InvalidCiphertextException", "tampered"), 400, "INVALID_REQUEST"),
        (FakeClientError("AccessDeniedException", "arn:aws secret"), 500, "INTERNAL_ERROR"),
        (
            FakeClientError("DependencyTimeoutException", "timeout secret"),
            503,
            "SERVICE_UNAVAILABLE",
        ),
        (ReadTimeoutError("network token secret"), 503, "SERVICE_UNAVAILABLE"),
    ],
)
def test_kms_failures_are_classified_without_blame_or_leakage(
    configured: tuple[FakeDdb, FakeKms],
    caplog: pytest.LogCaptureFixture,
    failure: Exception,
    status: int,
    code: str,
) -> None:
    _, kms = configured
    token = handler._cursor_encode(
        kms, SUB, 25, {"device_id": {"S": DEVICE}, "user_id": {"S": SUB}}
    )
    kms.failure = failure
    with caplog.at_level("ERROR"):
        response = handler.list_devices(event(query={"cursor": token}), None)
    assert response["statusCode"] == status and body(response)["error"]["code"] == code
    assert "arn:aws" not in caplog.text + response["body"]
    assert "timeout secret" not in caplog.text + response["body"]
    assert "network token secret" not in caplog.text + response["body"]


def test_unprocessed_keys_recover_then_exhaust(configured: tuple[FakeDdb, FakeKms]) -> None:
    ddb, _ = configured
    ddb.query_items = [{"device_id": DEVICE, "status": "ACTIVE", "role": "OWNER"}]
    pending = {
        "UnprocessedKeys": {"devices": {"Keys": [{"device_id": {"S": DEVICE}}]}},
        "Responses": {"devices": []},
    }
    ddb.batch_results = [
        pending,
        {"UnprocessedKeys": {}, "Responses": {"devices": [av({"device_id": DEVICE})]}},
    ]
    sleeps: list[float] = []
    assert handler.list_devices(event(), None, sleeper=sleeps.append)["statusCode"] == 200
    assert len(sleeps) == 1
    ddb.batch_results = [pending, pending, pending]
    assert handler.list_devices(event(), None, sleeper=lambda _: None)["statusCode"] == 500
    assert len([c for c in ddb.calls if c[0] == "batch_get_item"]) == 5


def active(ddb: FakeDdb) -> None:
    ddb.membership = {"status": "ACTIVE", "role": "OWNER"}


def test_status_missing_fresh_stale_and_malformed(configured: tuple[FakeDdb, FakeKms]) -> None:
    ddb, _ = configured
    active(ddb)
    assert body(handler.get_status(event(device=DEVICE), None))["health"] is None
    for seconds, expected in ((10, "FRESH"), (121, "STALE")):
        seen = (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")
        ddb.telemetry = {"last_seen_at": seen, "last_state": "IDLE", "firmware_version": "1.0"}
        result = body(handler.get_status(event(device=DEVICE), None))
        assert result["freshness"] == expected
        assert set(result["health"]) == {"intercom_state", "firmware_version", "last_seen_at"}
    for invalid in (
        "invalid",
        datetime.now().isoformat(),
        (datetime.now(UTC) + timedelta(seconds=1)).isoformat(),
    ):
        ddb.telemetry = {"last_seen_at": invalid, "last_state": "IDLE", "firmware_version": "1.0"}
        assert body(handler.get_status(event(device=DEVICE), None))["freshness"] == "UNKNOWN"


def test_dependency_errors_and_logs_are_sanitized(
    configured: tuple[FakeDdb, FakeKms], caplog: pytest.LogCaptureFixture
) -> None:
    ddb, _ = configured
    ddb.error = RuntimeError("arn:aws secret@example.test Authorization bearer-token table-name")
    with caplog.at_level("ERROR"):
        response = handler.get_device(event(device=DEVICE), None)
    assert response["statusCode"] == 500
    output = caplog.text + response["body"]
    for sensitive in ("arn:aws", "secret@example.test", "bearer-token", "table-name"):
        assert sensitive not in output
    assert "DEPENDENCY_FAILURE" in output and "request-1" in output
