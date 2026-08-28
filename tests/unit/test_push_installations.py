from __future__ import annotations

import hashlib
import importlib.util
import json
import logging
import sys
from pathlib import Path
from typing import Any

import pytest

from domain.push.models import (
    MAX_APP_VERSION_LENGTH,
    MAX_TOKEN_LENGTH,
    PushInstallation,
    token_hash,
)
from lambdas.push_api import handler

IID = "550e8400-e29b-41d4-a716-446655440000"
IID2 = "6ba7b810-9dad-41d1-80b4-00c04fd430c8"
TOKEN = "fictional-fcm-token"


class Conflict(Exception):
    response = {"Error": {"Code": "TransactionCanceledException"}}


class FakeDdb:
    def __init__(
        self, reads: list[tuple[dict[str, Any], dict[str, Any]]] | None = None, failures: int = 0
    ) -> None:
        self.reads = list(reads or [({}, {})])
        self.failures = failures
        self.writes: list[list[dict[str, Any]]] = []

    def transact_get_items(self, **kwargs: object) -> dict[str, object]:
        current, claim = self.reads.pop(0) if self.reads else ({}, {})
        return {
            "Responses": [{"Item": current} if current else {}, {"Item": claim} if claim else {}]
        }

    def transact_write_items(self, **kwargs: object) -> object:
        actions = kwargs["TransactItems"]
        assert isinstance(actions, list)
        self.writes.append(actions)
        if self.failures:
            self.failures -= 1
            raise Conflict
        return {}


def av(**values: str | int) -> dict[str, dict[str, str]]:
    return {
        key: ({"N": str(value)} if isinstance(value, int) else {"S": value})
        for key, value in values.items()
    }


def event(*, user: str = "user-a", iid: str = IID, body: object | None = None) -> dict[str, object]:
    payload = (
        body
        if body is not None
        else {
            "version": 1,
            "platform": "ANDROID",
            "push_provider": "FCM",
            "token": TOKEN,
            "app_id": "com.interbridge.app",
            "app_version": "1.0.0",
        }
    )
    return {
        "requestContext": {
            "requestId": "request-1",
            "authorizer": {
                "jwt": {"claims": {"sub": user, "token_use": "access", "client_id": "client"}}
            },
        },
        "pathParameters": {"installation_id": iid},
        "body": json.dumps(payload),
    }


@pytest.fixture(autouse=True)
def environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXPECTED_APP_CLIENT_ID", "client")
    monkeypatch.setenv("PUSH_INSTALLATIONS_TABLE", "push-table")


def put(
    fake: FakeDdb, value: dict[str, object] | None = None, *, now: float = 100.0
) -> dict[str, object]:
    return handler.put_installation(
        value or event(), None, clock=lambda: now, ddb_provider=lambda: fake
    )


def test_domain_validates_uuid_hash_exact_token_and_safe_repr() -> None:
    model = PushInstallation(
        "u", IID, "ANDROID", "FCM", " token ", "com.interbridge.app", "1", 1, 2
    )
    assert model.token == " token "
    assert token_hash(model.token) == hashlib.sha256(b" token ").hexdigest()
    assert " token " not in repr(model)
    with pytest.raises(ValueError, match="identifier"):
        PushInstallation("u", "invalid", "ANDROID", "FCM", TOKEN, "com.interbridge.app", "1", 1, 2)


@pytest.mark.parametrize("token", ["", 123, "x" * (MAX_TOKEN_LENGTH + 1)])
def test_domain_rejects_invalid_token_without_echo(token: object) -> None:
    with pytest.raises((ValueError, TypeError)) as caught:
        PushInstallation("u", IID, "ANDROID", "FCM", token, "com.interbridge.app", "1", 1, 2)  # type: ignore[arg-type]
    assert TOKEN not in str(caught.value)


@pytest.mark.parametrize(
    "field,value",
    [
        ("platform", "IOS"),
        ("push_provider", "APNS"),
        ("app_id", "other"),
        ("app_version", ""),
        ("app_version", "x" * (MAX_APP_VERSION_LENGTH + 1)),
    ],
)
def test_domain_rejects_unsupported_values(field: str, value: str) -> None:
    values = {
        "user_id": "u",
        "installation_id": IID,
        "platform": "ANDROID",
        "push_provider": "FCM",
        "token": TOKEN,
        "app_id": "com.interbridge.app",
        "app_version": "1",
        "created_at": 1,
        "updated_at": 2,
    }
    values[field] = value
    with pytest.raises(ValueError):
        PushInstallation(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "bad_body",
    [
        None,
        "{",
        {},
        {
            "version": True,
            "platform": "ANDROID",
            "push_provider": "FCM",
            "token": TOKEN,
            "app_id": "com.interbridge.app",
            "app_version": "1",
        },
        {
            "version": 1,
            "platform": "ANDROID",
            "push_provider": "FCM",
            "token": TOKEN,
            "app_id": "com.interbridge.app",
            "app_version": "1",
            "extra": 1,
        },
    ],
)
def test_put_rejects_missing_invalid_boolean_version_and_unknown_fields(bad_body: object) -> None:
    value = event()
    value["body"] = (
        None
        if bad_body is None
        else (bad_body if isinstance(bad_body, str) else json.dumps(bad_body))
    )
    assert put(FakeDdb(), value)["statusCode"] == 400


def test_put_rejects_oversized_body_and_bad_claims() -> None:
    value = event()
    value["body"] = "x" * (handler.MAX_BODY_BYTES + 1)
    assert put(FakeDdb(), value)["statusCode"] == 413
    value = event()
    value["requestContext"] = {}
    assert put(FakeDdb(), value)["statusCode"] == 401


def test_put_creates_authoritative_installation_and_claim_without_query() -> None:
    fake = FakeDdb()
    response = put(fake)
    assert response == {"statusCode": 204, "body": ""}
    assert len(fake.writes[0]) == 2
    items = [action["Put"]["Item"] for action in fake.writes[0]]
    assert {item["sk"]["S"] for item in items} == {"INSTALLATION", "CLAIM"}
    claim = next(item for item in items if item["sk"]["S"] == "CLAIM")
    assert "user_id" not in claim and "installation_id" not in claim
    assert claim["claimed_installation_id"]["S"] == IID
    assert all("Query" not in str(action) for action in fake.writes[0])
    assert TOKEN not in json.dumps(response)


def test_put_preserves_created_at_updates_version_and_rotates_claim() -> None:
    old_hash = token_hash("old-token")
    current = av(
        pk=f"INSTALLATION#{IID}",
        sk="INSTALLATION",
        user_id="user-a",
        installation_id=IID,
        token_hash=old_hash,
        created_at=10,
    )
    fake = FakeDdb([(current, {})])
    value = event(
        body={
            "version": 1,
            "platform": "ANDROID",
            "push_provider": "FCM",
            "token": TOKEN,
            "app_id": "com.interbridge.app",
            "app_version": "2.0",
        }
    )
    assert put(fake, value, now=20)["statusCode"] == 204
    assert fake.writes[0][0]["Delete"]["Key"]["pk"]["S"] == f"TOKEN#{old_hash}"
    installed = fake.writes[0][1]["Put"]["Item"]
    assert installed["created_at"]["N"] == "10" and installed["updated_at"]["N"] == "20"
    assert installed["app_version"]["S"] == "2.0"


def test_put_transfers_token_and_invalidates_previous_installation_atomically() -> None:
    digest = token_hash(TOKEN)
    claim = av(
        pk=f"TOKEN#{digest}", sk="CLAIM", claimed_installation_id=IID2, claimed_user_id="old-user"
    )
    fake = FakeDdb([({}, claim)])
    assert put(fake)["statusCode"] == 204
    assert len(fake.writes[0]) == 3
    assert fake.writes[0][0]["Delete"]["Key"]["pk"]["S"] == f"INSTALLATION#{IID2}"


def test_put_transfers_installation_between_users_with_conditions() -> None:
    digest = token_hash(TOKEN)
    current = av(
        pk=f"INSTALLATION#{IID}",
        sk="INSTALLATION",
        user_id="old-user",
        installation_id=IID,
        token_hash=digest,
        created_at=1,
    )
    claim = av(
        pk=f"TOKEN#{digest}", sk="CLAIM", claimed_installation_id=IID, claimed_user_id="old-user"
    )
    fake = FakeDdb([(current, claim)])
    assert put(fake)["statusCode"] == 204
    assert "#user_id = :user_id" in fake.writes[0][0]["Put"]["ConditionExpression"]


def test_put_retries_expected_conflict_but_is_bounded_and_sanitized() -> None:
    fake = FakeDdb([({}, {}), ({}, {})], failures=1)
    assert put(fake)["statusCode"] == 204 and len(fake.writes) == 2
    failing = FakeDdb([({}, {})] * handler.MAX_TRANSACTION_ATTEMPTS, failures=99)
    response = put(failing)
    assert response["statusCode"] == 409 and len(failing.writes) == handler.MAX_TRANSACTION_ATTEMPTS
    assert TOKEN not in json.dumps(response)


def test_dependency_failure_is_sanitized_and_never_logs_token(
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake = FakeDdb()
    fake.transact_get_items = lambda **kwargs: (_ for _ in ()).throw(RuntimeError(TOKEN))  # type: ignore[method-assign]
    with caplog.at_level(logging.ERROR):
        response = put(fake)
    assert response["statusCode"] == 503
    assert TOKEN not in json.dumps(response) and TOKEN not in caplog.text


def test_delete_is_idempotent_and_other_user_cannot_delete() -> None:
    fake = FakeDdb([({}, {})])
    value = event()
    value.pop("body")
    assert handler.delete_installation(value, None, ddb_provider=lambda: fake)["statusCode"] == 204
    assert not fake.writes
    digest = token_hash(TOKEN)
    other = av(user_id="another-user", token_hash=digest)
    fake = FakeDdb([(other, {})])
    assert handler.delete_installation(value, None, ddb_provider=lambda: fake)["statusCode"] == 204
    assert not fake.writes


def test_delete_removes_only_matching_installation_and_claim() -> None:
    digest = token_hash(TOKEN)
    current = av(user_id="user-a", token_hash=digest)
    fake = FakeDdb([(current, {})])
    value = event()
    value.pop("body")
    assert handler.delete_installation(value, None, ddb_provider=lambda: fake)["statusCode"] == 204
    assert len(fake.writes[0]) == 2
    assert all("ConditionExpression" in action["Delete"] for action in fake.writes[0])


def test_packaged_lambda_imports_without_repository_root(tmp_path: Path) -> None:
    source = Path("lambdas/push_api")
    package = tmp_path / "push_api"
    package.mkdir()
    for file in source.glob("*.py"):
        (package / file.name).write_bytes(file.read_bytes())
    previous = list(sys.path)
    sys.path[:] = [str(tmp_path)]
    try:
        spec = importlib.util.spec_from_file_location("push_api.handler", package / "handler.py")
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules["push_api.handler"] = module
        spec.loader.exec_module(module)
        assert callable(module.put_installation) and callable(module.delete_installation)
    finally:
        sys.path[:] = previous


def test_domain_and_packaged_models_remain_identical() -> None:
    assert (
        Path("domain/push/models.py").read_text() == Path("lambdas/push_api/models.py").read_text()
    )
