from __future__ import annotations

from typing import Any

import pytest

from domain.push.fcm_result import FcmResult
from lambdas.push_sender import handler

DEVICE = "ib-" + "a" * 32
EVENT_ID = "evt-" + "b" * 32
DELIVERIES_TABLE = "deliveries"
MEMBERSHIPS_TABLE = "memberships"
INSTALLATIONS_TABLE = "installations"
INSTALLATIONS_INDEX = "by-user"


def av(item: dict[str, object]) -> dict[str, dict[str, object]]:
    def attr(value: object) -> dict[str, object]:
        if isinstance(value, bool):
            return {"BOOL": value}
        if isinstance(value, str):
            return {"S": value}
        if isinstance(value, int):
            return {"N": str(value)}
        if value is None:
            return {"NULL": True}
        if isinstance(value, list):
            return {"L": [attr(v) for v in value]}
        if isinstance(value, dict):
            return {"M": {k: attr(v) for k, v in value.items()}}
        raise TypeError(value)

    return {key: attr(value) for key, value in item.items()}


class ConditionalCheckFailed(Exception):
    response = {"Error": {"Code": "ConditionalCheckFailedException"}}


class FakeDdb:
    def __init__(self) -> None:
        self.deliveries: dict[tuple[str, str], dict[str, Any]] = {}
        self.memberships: list[dict[str, object]] = []
        self.installations: dict[str, dict[str, object]] = {}
        self.gsi_index: dict[str, list[str]] = {}
        self.deleted_installations: list[str] = []
        self.calls: list[tuple[str, dict[str, Any]]] = []

    # -- idempotency table --
    def put_item(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("put_item", kwargs))
        item = kwargs["Item"]
        key = (item["device_id"]["S"], item["event_id"]["S"])
        if key in self.deliveries:
            raise ConditionalCheckFailed
        self.deliveries[key] = item
        return {}

    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("get_item", kwargs))
        key_attrs = kwargs["Key"]
        if "event_id" in key_attrs:
            key = (key_attrs["device_id"]["S"], key_attrs["event_id"]["S"])
            item = self.deliveries.get(key)
            return {"Item": item} if item else {}
        return {}

    def update_item(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("update_item", kwargs))
        key_attrs = kwargs["Key"]
        key = (key_attrs["device_id"]["S"], key_attrs["event_id"]["S"])
        current = self.deliveries.get(key)
        condition = kwargs.get("ConditionExpression", "")
        values = kwargs["ExpressionAttributeValues"]
        item = self.deliveries.setdefault(key, {})

        if "lease_expires_at" in condition:
            if current is None or (
                current.get("status", {}).get("S") != values[":processing"]["S"]
                or current.get("lease_expires_at", {}).get("N") != values[":old_lease"]["N"]
                or current.get("attempt", {}).get("N") != values[":old_attempt"]["N"]
            ):
                raise ConditionalCheckFailed
            item["status"] = values[":processing"]
            item["lease_expires_at"] = values[":new_lease"]
            item["attempt"] = values[":new_attempt"]
            item["updated_at"] = values[":now"]
        else:
            if condition and (
                current is None or current.get("attempt", {}).get("N") != values[":attempt"]["N"]
            ):
                raise ConditionalCheckFailed
            item["status"] = values[":status"]
            item["updated_at"] = values[":now"]
            for name, value in values.items():
                bare = name.removeprefix(":")
                if bare not in {"status", "now", "attempt"}:
                    item[bare] = value
        return {}

    # -- memberships table --
    def query(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("query", kwargs))
        if kwargs["TableName"] == MEMBERSHIPS_TABLE:
            device_id = kwargs["ExpressionAttributeValues"][":d"]["S"]
            items = [m for m in self.memberships if m["device_id"] == device_id]
            return {"Items": [av(m) for m in items]}
        user_id = kwargs["ExpressionAttributeValues"][":u"]["S"]
        ids = self.gsi_index.get(user_id, [])
        return {"Items": [av({"user_id": user_id, "installation_id": iid}) for iid in ids]}

    def batch_get_item(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("batch_get_item", kwargs))
        keys = kwargs["RequestItems"][INSTALLATIONS_TABLE]["Keys"]
        responses = []
        for key in keys:
            iid = key["pk"]["S"].removeprefix("INSTALLATION#")
            if iid in self.installations:
                responses.append(av(self.installations[iid]))
        return {"Responses": {INSTALLATIONS_TABLE: responses}}

    def transact_write_items(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("transact_write_items", kwargs))
        actions = kwargs["TransactItems"]
        installation_id = actions[0]["Delete"]["Key"]["pk"]["S"].removeprefix("INSTALLATION#")
        self.deleted_installations.append(installation_id)
        self.installations.pop(installation_id, None)
        return {}


def membership(
    user_id: str,
    *,
    status: str = "ACTIVE",
    role: str = "OWNER",
    preferences: dict[str, object] | None = None,
) -> dict[str, object]:
    item: dict[str, object] = {
        "device_id": DEVICE,
        "user_id": user_id,
        "status": status,
        "role": role,
    }
    if preferences is not None:
        item["notification_preferences"] = preferences
    return item


def installation(installation_id: str, user_id: str, *, token: str = "tok") -> dict[str, object]:
    return {
        "pk": f"INSTALLATION#{installation_id}",
        "sk": "INSTALLATION",
        "installation_id": installation_id,
        "user_id": user_id,
        "token": token,
        "token_hash": f"hash-{installation_id}",
    }


def register(ddb: FakeDdb, installation_id: str, user_id: str, **kwargs: object) -> None:
    ddb.installations[installation_id] = installation(installation_id, user_id, **kwargs)
    ddb.gsi_index.setdefault(user_id, []).append(installation_id)


def prefs(alert_mode: str) -> dict[str, object]:
    return {
        "version": 1,
        "alert_mode": alert_mode,
        "quiet_schedule": {
            "enabled": False,
            "timezone": None,
            "days": [],
            "start_time": None,
            "end_time": None,
            "behavior": "NOTIFICATION_ONLY",
        },
        "updated_at": None,
    }


def invocation(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": 1,
        "device_id": DEVICE,
        "event_id": EVENT_ID,
        "event": "RING_DETECTED",
        "occurred_at": "2026-08-20T12:00:00Z",
    }
    payload.update(overrides)
    return payload


class ScriptedFcm:
    def __init__(self, results: list[FcmResult] | None = None) -> None:
        self.results = list(results or [])
        self.sent_messages: list[dict[str, Any]] = []

    def send(self, message: dict[str, Any]) -> FcmResult:
        self.sent_messages.append(message)
        if self.results:
            return self.results.pop(0)
        return FcmResult("SUCCESS", 200)


@pytest.fixture(autouse=True)
def environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PUSH_DELIVERIES_TABLE", DELIVERIES_TABLE)
    monkeypatch.setenv("MEMBERSHIPS_TABLE", MEMBERSHIPS_TABLE)
    monkeypatch.setenv("PUSH_INSTALLATIONS_TABLE", INSTALLATIONS_TABLE)
    monkeypatch.setenv("PUSH_INSTALLATIONS_BY_USER_INDEX", INSTALLATIONS_INDEX)


def run(
    ddb: FakeDdb,
    fcm: ScriptedFcm,
    payload: dict[str, object] | None = None,
    *,
    now: float = 1_000_000.0,
) -> dict[str, Any]:
    return handler.lambda_handler(
        payload if payload is not None else invocation(),
        None,
        ddb=ddb,
        fcm_client=fcm,
        clock=lambda: now,
    )


def test_invalid_invocation_is_rejected_without_touching_dynamodb() -> None:
    ddb, fcm = FakeDdb(), ScriptedFcm()
    result = run(ddb, fcm, invocation(event="OFF_HOOK"))
    assert result["result"] == "rejected"
    assert ddb.calls == []
    assert fcm.sent_messages == []


def test_zero_memberships_is_a_valid_no_recipients_outcome() -> None:
    ddb, fcm = FakeDdb(), ScriptedFcm()
    result = run(ddb, fcm)
    assert result["result"] == "no_recipients"
    assert fcm.sent_messages == []
    key = (DEVICE, EVENT_ID)
    assert ddb.deliveries[key]["status"]["S"] == "COMPLETED"


def test_inactive_memberships_never_receive_installations_or_sends() -> None:
    ddb, fcm = FakeDdb(), ScriptedFcm()
    ddb.memberships = [
        membership("removed-user", status="REMOVED", preferences=prefs("RING_AND_NOTIFICATION")),
        membership("pending-user", status="PENDING", preferences=prefs("RING_AND_NOTIFICATION")),
    ]
    register(ddb, "iid-1", "removed-user")
    result = run(ddb, fcm)
    assert result["result"] == "no_recipients"
    assert fcm.sent_messages == []


def test_zero_installations_for_an_otherwise_deliverable_membership() -> None:
    ddb, fcm = FakeDdb(), ScriptedFcm()
    ddb.memberships = [membership("u1", preferences=prefs("RING_AND_NOTIFICATION"))]
    result = run(ddb, fcm)
    assert result["result"] == "no_installations"
    assert fcm.sent_messages == []


def test_multiple_users_and_installations_each_get_correct_decisions() -> None:
    ddb, fcm = FakeDdb(), ScriptedFcm()
    ddb.memberships = [
        membership("owner", preferences=prefs("RING_AND_NOTIFICATION")),
        membership("silent-member", preferences=prefs("NONE")),
    ]
    register(ddb, "iid-owner-phone", "owner")
    register(ddb, "iid-owner-tablet", "owner")
    register(ddb, "iid-silent-phone", "silent-member")

    result = run(ddb, fcm)

    assert result["result"] == "processed"
    assert result["sent_count"] == 2
    assert result["suppressed_count"] == 1
    sent_tokens = {message["message"]["token"] for message in fcm.sent_messages}
    assert sent_tokens == {"tok"}  # both owner installs share the fixture default token
    assert len(fcm.sent_messages) == 2


def test_success_marks_sent_and_records_counters() -> None:
    ddb, fcm = FakeDdb(), ScriptedFcm([FcmResult("SUCCESS", 200)])
    ddb.memberships = [membership("u1", preferences=prefs("RING_ONLY"))]
    register(ddb, "iid-1", "u1")
    result = run(ddb, fcm)
    assert result["sent_count"] == 1
    assert fcm.sent_messages[0]["message"]["data"]["presentation_intent"] == "RING_ONLY"


def test_unregistered_token_triggers_transactional_cleanup() -> None:
    ddb, fcm = FakeDdb(), ScriptedFcm([FcmResult("INVALID_TOKEN", 404)])
    ddb.memberships = [membership("u1", preferences=prefs("RING_AND_NOTIFICATION"))]
    register(ddb, "iid-1", "u1")
    result = run(ddb, fcm)
    assert result["invalid_token_count"] == 1
    assert ddb.deleted_installations == ["iid-1"]


def test_rate_limited_counts_as_temporary_failure_and_does_not_delete() -> None:
    ddb, fcm = FakeDdb(), ScriptedFcm([FcmResult("RATE_LIMITED", 429)])
    ddb.memberships = [membership("u1", preferences=prefs("RING_AND_NOTIFICATION"))]
    register(ddb, "iid-1", "u1")
    result = run(ddb, fcm)
    assert result["temporary_failure_count"] == 1
    assert ddb.deleted_installations == []


def test_server_error_counts_as_temporary_failure_and_does_not_delete() -> None:
    ddb, fcm = FakeDdb(), ScriptedFcm([FcmResult("TEMPORARY_ERROR", 503)])
    ddb.memberships = [membership("u1", preferences=prefs("RING_AND_NOTIFICATION"))]
    register(ddb, "iid-1", "u1")
    result = run(ddb, fcm)
    assert result["temporary_failure_count"] == 1
    assert ddb.deleted_installations == []


def test_malformed_payload_error_counts_as_permanent_failure_and_does_not_delete() -> None:
    ddb, fcm = FakeDdb(), ScriptedFcm([FcmResult("PERMANENT_PAYLOAD_ERROR", 400)])
    ddb.memberships = [membership("u1", preferences=prefs("RING_AND_NOTIFICATION"))]
    register(ddb, "iid-1", "u1")
    result = run(ddb, fcm)
    assert result["permanent_failure_count"] == 1
    assert ddb.deleted_installations == []


def test_race_between_cleanup_and_concurrent_token_update_is_a_benign_skip() -> None:
    ddb, fcm = FakeDdb(), ScriptedFcm([FcmResult("INVALID_TOKEN", 404)])
    ddb.memberships = [membership("u1", preferences=prefs("RING_AND_NOTIFICATION"))]
    register(ddb, "iid-1", "u1")

    def racing_transact_write_items(**kwargs: Any) -> Any:
        raise ConditionalCheckFailed

    ddb.transact_write_items = racing_transact_write_items  # type: ignore[method-assign]
    result = run(ddb, fcm)
    # The send's own result is still reported correctly even though cleanup
    # lost the race.
    assert result["invalid_token_count"] == 1
    assert result["result"] == "processed"


def test_sequential_duplicate_does_not_resend() -> None:
    ddb, fcm = FakeDdb(), ScriptedFcm()
    ddb.memberships = [membership("u1", preferences=prefs("RING_AND_NOTIFICATION"))]
    register(ddb, "iid-1", "u1")
    first = run(ddb, fcm)
    assert first["result"] == "processed"
    assert len(fcm.sent_messages) == 1

    second = run(ddb, fcm, now=1_000_010.0)
    assert second["result"] == "duplicate"
    assert second["claim_outcome"] == "DUPLICATE_COMPLETED"
    assert len(fcm.sent_messages) == 1  # not resent


def test_concurrent_duplicate_while_in_flight_does_not_resend() -> None:
    ddb, fcm = FakeDdb(), ScriptedFcm()
    ddb.memberships = [membership("u1", preferences=prefs("RING_AND_NOTIFICATION"))]
    register(ddb, "iid-1", "u1")
    # Simulate a concurrent invocation that already claimed the record.
    from lambdas.push_sender import idempotency

    idempotency.claim(ddb, DELIVERIES_TABLE, DEVICE, EVENT_ID, now=999_999)
    result = run(ddb, fcm)
    assert result["result"] == "duplicate"
    assert result["claim_outcome"] == "DUPLICATE_IN_FLIGHT"
    assert fcm.sent_messages == []


def test_user_with_malicious_extra_fields_cannot_control_recipients() -> None:
    ddb, fcm = FakeDdb(), ScriptedFcm()
    ddb.memberships = [membership("u1", preferences=prefs("RING_AND_NOTIFICATION"))]
    register(ddb, "iid-1", "u1")
    malicious = invocation()
    malicious["user_id"] = "attacker"
    malicious["token"] = "attacker-controlled-token"
    result = run(ddb, fcm, malicious)
    assert result["result"] == "rejected"
    assert fcm.sent_messages == []


def test_logs_never_contain_the_push_token(caplog: pytest.LogCaptureFixture) -> None:
    ddb, fcm = FakeDdb(), ScriptedFcm()
    ddb.memberships = [membership("u1", preferences=prefs("RING_AND_NOTIFICATION"))]
    register(ddb, "iid-1", "u1", token="super-secret-token-value")
    with caplog.at_level("INFO"):
        run(ddb, fcm)
    assert "super-secret-token-value" not in caplog.text


def test_all_recipients_suppressed_still_completes_without_sending() -> None:
    ddb, fcm = FakeDdb(), ScriptedFcm()
    ddb.memberships = [membership("u1", preferences=prefs("NONE"))]
    register(ddb, "iid-1", "u1")
    result = run(ddb, fcm)
    assert result["result"] == "all_suppressed"
    assert fcm.sent_messages == []


def test_legacy_membership_without_preferences_defaults_to_ring_and_notification() -> None:
    ddb, fcm = FakeDdb(), ScriptedFcm()
    ddb.memberships = [membership("legacy-user")]  # no notification_preferences at all
    register(ddb, "iid-1", "legacy-user")
    result = run(ddb, fcm)
    assert result["sent_count"] == 1
    assert fcm.sent_messages[0]["message"]["data"]["presentation_intent"] == "RING_AND_NOTIFICATION"


def test_typed_auth_error_result_propagates_as_a_recoverable_failure_when_nothing_sent() -> None:
    # AUTH_OR_CONFIG_ERROR can arrive as an ordinary FcmResult (bad
    # per-message auth state reported by FCM itself), distinct from a
    # raised FirebaseCredentialError (handler.py's own pre-flight check,
    # covered separately below) -- both are the same systemic failure
    # class and must both propagate instead of completing.
    from lambdas.push_sender.firebase_auth import FirebaseCredentialError

    ddb = FakeDdb()
    fcm = ScriptedFcm([FcmResult("AUTH_OR_CONFIG_ERROR", 401)])
    ddb.memberships = [membership("u1", preferences=prefs("RING_AND_NOTIFICATION"))]
    register(ddb, "iid-1", "u1")

    with pytest.raises(FirebaseCredentialError):
        run(ddb, fcm)

    key = (DEVICE, EVENT_ID)
    assert ddb.deliveries[key]["status"]["S"] == "PROCESSING"
    assert ddb.deleted_installations == []


def test_raised_credential_error_propagates_as_a_recoverable_failure_when_nothing_sent() -> None:
    from lambdas.push_sender.firebase_auth import FirebaseCredentialError

    class RaisingFcm:
        def send(self, message: dict[str, Any]) -> FcmResult:
            raise FirebaseCredentialError("token refresh failed")

    ddb = FakeDdb()
    ddb.memberships = [membership("u1", preferences=prefs("RING_AND_NOTIFICATION"))]
    register(ddb, "iid-1", "u1")

    with pytest.raises(FirebaseCredentialError):
        run(ddb, RaisingFcm())


def test_auth_failure_short_circuits_all_remaining_installations_not_just_the_next_one() -> None:
    from lambdas.push_sender.firebase_auth import FirebaseCredentialError

    ddb = FakeDdb()
    fcm = ScriptedFcm(
        [
            FcmResult("AUTH_OR_CONFIG_ERROR", 401),
            FcmResult("SUCCESS", 200),
            FcmResult("SUCCESS", 200),
        ]
    )
    ddb.memberships = [membership("u1", preferences=prefs("RING_AND_NOTIFICATION"))]
    register(ddb, "iid-1", "u1")
    register(ddb, "iid-2", "u1")
    register(ddb, "iid-3", "u1")

    with pytest.raises(FirebaseCredentialError):
        run(ddb, fcm)

    # Only the first installation was actually sent to; the remaining two
    # were counted as auth-config failures without ever calling fcm.send.
    assert len(fcm.sent_messages) == 1

    key = (DEVICE, EVENT_ID)
    assert ddb.deliveries[key]["status"]["S"] == "PROCESSING"
    assert ddb.deleted_installations == []


def test_auth_failure_after_a_partial_success_still_completes_without_resending() -> None:
    # Only a *total* failure (nothing sent at all) is treated as
    # recoverable/systemic. A failure partway through -- some installations
    # already reached -- still completes, so a retry never re-notifies the
    # ones that already succeeded.
    ddb = FakeDdb()
    fcm = ScriptedFcm([FcmResult("SUCCESS", 200), FcmResult("AUTH_OR_CONFIG_ERROR", 401)])
    ddb.memberships = [membership("u1", preferences=prefs("RING_AND_NOTIFICATION"))]
    register(ddb, "iid-1", "u1")
    register(ddb, "iid-2", "u1")

    result = run(ddb, fcm)
    assert result["result"] == "processed"
    assert result["sent_count"] == 1
    assert result["auth_config_failure_count"] == 1
    key = (DEVICE, EVENT_ID)
    assert ddb.deliveries[key]["status"]["S"] == "COMPLETED"


def test_retry_after_total_auth_failure_resumes_once_the_lease_expires() -> None:
    from lambdas.push_sender.firebase_auth import FirebaseCredentialError

    ddb = FakeDdb()
    ddb.memberships = [membership("u1", preferences=prefs("RING_AND_NOTIFICATION"))]
    register(ddb, "iid-1", "u1")

    with pytest.raises(FirebaseCredentialError):
        run(ddb, ScriptedFcm([FcmResult("AUTH_OR_CONFIG_ERROR", 401)]), now=1_000_000.0)

    # An immediate retry (lease still valid) must not resend -- it's
    # indistinguishable from a genuinely still-running attempt.
    still_in_flight = run(ddb, ScriptedFcm([FcmResult("SUCCESS", 200)]), now=1_000_000.0 + 1)
    assert still_in_flight["result"] == "duplicate"
    assert still_in_flight["claim_outcome"] == "DUPLICATE_IN_FLIGHT"

    # Once the credential is fixed and the lease has expired, the retry
    # (Lambda's own async-invoke retry in production) succeeds.
    from lambdas.push_sender import idempotency

    recovered = run(
        ddb,
        ScriptedFcm([FcmResult("SUCCESS", 200)]),
        now=1_000_000.0 + idempotency.LEASE_SECONDS + 1,
    )
    assert recovered["result"] == "processed"
    assert recovered["sent_count"] == 1
    key = (DEVICE, EVENT_ID)
    assert ddb.deliveries[key]["status"]["S"] == "COMPLETED"


def test_injected_ddb_and_fcm_client_never_trigger_default_client_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Regression test for the bug that made CI reach out to real AWS
    # (botocore.exceptions.NoRegionError): _default_clients()/
    # _default_fcm_client() must be completely unreachable whenever both
    # ddb and fcm_client are injected, regardless of how the fan-out plays
    # out (success, suppression, no recipients, ...).
    def boom_clients() -> tuple[Any, Any]:
        raise AssertionError("_default_clients() must not be called when ddb/fcm are injected")

    def boom_fcm(secrets_client: Any) -> Any:
        raise AssertionError("_default_fcm_client() must not be called when fcm_client is injected")

    monkeypatch.setattr(handler, "_default_clients", boom_clients)
    monkeypatch.setattr(handler, "_default_fcm_client", boom_fcm)

    ddb, fcm = FakeDdb(), ScriptedFcm()
    ddb.memberships = [
        membership("owner", preferences=prefs("RING_AND_NOTIFICATION")),
        membership("silent-member", preferences=prefs("NONE")),
    ]
    register(ddb, "iid-1", "owner")

    result = run(ddb, fcm)
    assert result["result"] == "processed"
    assert result["sent_count"] == 1


def test_evaluate_failure_after_a_valid_combine_still_falls_back_to_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Defense in depth: even if combine() succeeds but evaluate() somehow
    # still raises on that exact normalized shape (should not happen in
    # practice -- evaluate() trusts combine()'s invariants), one member's
    # bad luck must not abort their own delivery, let alone anyone else's.
    ddb, fcm = FakeDdb(), ScriptedFcm()
    ddb.memberships = [membership("u1", preferences=prefs("RING_ONLY"))]
    register(ddb, "iid-1", "u1")

    real_evaluate = handler.evaluate
    calls = {"count": 0}

    def flaky_evaluate(event: str, preferences: dict[str, object], *, now: object) -> object:
        calls["count"] += 1
        if calls["count"] == 1:
            raise ValueError("simulated inconsistency")
        return real_evaluate(event, preferences, now=now)  # type: ignore[arg-type]

    monkeypatch.setattr(handler, "evaluate", flaky_evaluate)
    result = run(ddb, fcm)
    assert result["result"] == "processed"
    assert result["sent_count"] == 1
    assert calls["count"] == 2


def test_malformed_stored_preferences_fall_back_to_defaults_without_blocking_others() -> None:
    ddb, fcm = FakeDdb(), ScriptedFcm()
    ddb.memberships = [
        membership("broken-user", preferences={"version": 1, "alert_mode": "GARBAGE"}),
        membership("normal-user", preferences=prefs("RING_ONLY")),
    ]
    register(ddb, "iid-broken", "broken-user")
    register(ddb, "iid-normal", "normal-user")
    result = run(ddb, fcm)
    assert result["result"] == "processed"
    assert result["sent_count"] == 2  # broken-user falls back to defaults (RING_AND_NOTIFICATION)
