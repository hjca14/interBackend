from __future__ import annotations

from typing import Any

import pytest

from domain.push.fcm_result import FcmResult
from lambdas.push_sender import handler, idempotency
from lambdas.push_sender.firebase_auth import FirebaseCredentialError

DEVICE = "ib-" + "a" * 32
EVENT_ID = "evt-" + "b" * 32
CALL_ID = "call-" + "c" * 32
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
        values = kwargs["ExpressionAttributeValues"]
        item = self.deliveries.setdefault(key, {})

        if ":old_lease" in values:
            # Lease-steal (RESUMED).
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
        elif ":expired" in values:
            # abandon(): releases the lease immediately, status stays
            # PROCESSING.
            if current is None or current.get("attempt", {}).get("N") != values[":attempt"]["N"]:
                raise ConditionalCheckFailed
            item["lease_expires_at"] = values[":expired"]
            item["updated_at"] = values[":now"]
        else:
            # complete().
            condition = kwargs.get("ConditionExpression", "")
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
        "call_id": CALL_ID,
        "timestamp_source": "device",
        "occurred_at": "1970-01-12T13:46:39Z",
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


class PersistentFailureFcm:
    """Always returns the same (typically retryable) result -- for
    exercising send_with_retry()'s local retry limit and the "still
    unresolved after every local retry" path, where a short scripted list
    would run out and silently fall back to SUCCESS.
    """

    def __init__(self, result: FcmResult) -> None:
        self.result = result
        self.sent_messages: list[dict[str, Any]] = []

    def send(self, message: dict[str, Any]) -> FcmResult:
        self.sent_messages.append(message)
        return self.result


@pytest.fixture(autouse=True)
def environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PUSH_DELIVERIES_TABLE", DELIVERIES_TABLE)
    monkeypatch.setenv("MEMBERSHIPS_TABLE", MEMBERSHIPS_TABLE)
    monkeypatch.setenv("PUSH_INSTALLATIONS_TABLE", INSTALLATIONS_TABLE)
    monkeypatch.setenv("PUSH_INSTALLATIONS_BY_USER_INDEX", INSTALLATIONS_INDEX)


def run(
    ddb: FakeDdb,
    fcm: Any,
    payload: dict[str, object] | None = None,
    *,
    now: float = 1_000_000.0,
    sleeper: Any = lambda _: None,
) -> dict[str, Any]:
    return handler.lambda_handler(
        payload if payload is not None else invocation(),
        None,
        ddb=ddb,
        fcm_client=fcm,
        clock=lambda: now,
        sleeper=sleeper,
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


def test_rate_limited_persisting_through_local_retries_does_not_become_silent_success() -> None:
    # 429 that never resolves (even after fcm_client.send_with_retry()'s
    # own bounded local retries) must not be swallowed into a completed,
    # successful-looking delivery -- see handler.UnresolvedTemporaryFailure.
    ddb = FakeDdb()
    fcm = PersistentFailureFcm(FcmResult("RATE_LIMITED", 429))
    ddb.memberships = [membership("u1", preferences=prefs("RING_AND_NOTIFICATION"))]
    register(ddb, "iid-1", "u1")

    with pytest.raises(handler.UnresolvedTemporaryFailure):
        run(ddb, fcm)

    key = (DEVICE, EVENT_ID)
    assert ddb.deliveries[key]["status"]["S"] == "PROCESSING"
    assert ddb.deleted_installations == []
    # fcm_client.send_with_retry()'s own MAX_SEND_ATTEMPTS bounds how many
    # times this one installation is actually called.
    from lambdas.push_sender.fcm_client import MAX_SEND_ATTEMPTS

    assert len(fcm.sent_messages) == MAX_SEND_ATTEMPTS


def test_server_error_persisting_through_local_retries_does_not_become_silent_success() -> None:
    ddb = FakeDdb()
    fcm = PersistentFailureFcm(FcmResult("TEMPORARY_ERROR", 503))
    ddb.memberships = [membership("u1", preferences=prefs("RING_AND_NOTIFICATION"))]
    register(ddb, "iid-1", "u1")

    with pytest.raises(handler.UnresolvedTemporaryFailure):
        run(ddb, fcm)

    key = (DEVICE, EVENT_ID)
    assert ddb.deliveries[key]["status"]["S"] == "PROCESSING"
    assert ddb.deleted_installations == []


def test_temporary_failure_that_resolves_within_local_retries_still_completes_normally() -> None:
    # The common case: send_with_retry() itself resolves a transient
    # hiccup, so the delivery completes without ever bothering the
    # idempotency/abandon machinery.
    ddb = FakeDdb()
    fcm = ScriptedFcm([FcmResult("TEMPORARY_ERROR", 503), FcmResult("SUCCESS", 200)])
    ddb.memberships = [membership("u1", preferences=prefs("RING_AND_NOTIFICATION"))]
    register(ddb, "iid-1", "u1")

    result = run(ddb, fcm)
    assert result["result"] == "processed"
    assert result["sent_count"] == 1
    assert result["temporary_failure_count"] == 0
    key = (DEVICE, EVENT_ID)
    assert ddb.deliveries[key]["status"]["S"] == "COMPLETED"


def test_temporary_failure_does_not_short_circuit_other_installations() -> None:
    # Unlike a systemic auth failure, one installation's temporary error
    # says nothing about the others -- they must still be attempted.
    ddb = FakeDdb()
    fcm = ScriptedFcm(
        [
            FcmResult("TEMPORARY_ERROR", 503),
            FcmResult("TEMPORARY_ERROR", 503),
            FcmResult("TEMPORARY_ERROR", 503),
            FcmResult("SUCCESS", 200),
        ]
    )
    ddb.memberships = [membership("u1", preferences=prefs("RING_AND_NOTIFICATION"))]
    register(ddb, "iid-1", "u1")
    register(ddb, "iid-2", "u1")

    with pytest.raises(handler.UnresolvedTemporaryFailure):
        run(ddb, fcm)

    # iid-1 exhausted its retries (3 attempts) and iid-2 still got a real
    # attempt (and succeeded) afterwards.
    assert len(fcm.sent_messages) == 4


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


def test_legacy_membership_without_preferences_defaults_to_ring_only() -> None:
    ddb, fcm = FakeDdb(), ScriptedFcm()
    ddb.memberships = [membership("legacy-user")]  # no notification_preferences at all
    register(ddb, "iid-1", "legacy-user")
    result = run(ddb, fcm)
    assert result["sent_count"] == 1
    assert fcm.sent_messages[0]["message"]["data"]["presentation_intent"] == "RING_ONLY"


def test_ring_ended_is_silent_and_sent_only_to_current_authorized_installations() -> None:
    ddb, fcm = FakeDdb(), ScriptedFcm()
    ddb.memberships = [
        membership("owner", preferences=prefs("NONE")),
        membership("removed", status="REMOVED", preferences=prefs("RING_ONLY")),
    ]
    register(ddb, "iid-owner", "owner")
    register(ddb, "iid-removed", "removed")
    result = run(
        ddb,
        fcm,
        invocation(event="RING_ENDED", event_id="evt-" + "d" * 32),
    )
    assert result["sent_count"] == 1
    message = fcm.sent_messages[0]["message"]
    assert message["token"] == "tok"
    assert "notification" not in message
    assert "presentation_intent" not in message["data"]
    assert message["data"]["call_id"] == CALL_ID


def test_late_ring_end_never_becomes_a_generic_cancellation() -> None:
    ddb, fcm = FakeDdb(), ScriptedFcm()
    ddb.memberships = [membership("owner", preferences=prefs("RING_ONLY"))]
    register(ddb, "iid-owner", "owner")
    run(ddb, fcm, invocation(event="RING_ENDED", call_id="call-" + "d" * 32))
    data = fcm.sent_messages[0]["message"]["data"]
    assert data["event"] == "RING_ENDED"
    assert data["call_id"] == "call-" + "d" * 32
    assert "cancel_all" not in data


@pytest.mark.parametrize("mode", ["RING_ONLY", "NOTIFICATION_ONLY", "RING_AND_NOTIFICATION"])
def test_expired_ring_is_terminal_before_installations_or_fcm(mode: str) -> None:
    ddb, fcm = FakeDdb(), ScriptedFcm()
    ddb.memberships = [membership("u1", preferences=prefs(mode))]
    register(ddb, "iid-1", "u1")
    result = run(
        ddb,
        fcm,
        invocation(occurred_at="1970-01-12T13:46:10Z"),
        now=1_000_000,
    )
    assert result["result"] == "suppressed_expired"
    assert result["sent_count"] == 0
    assert result["expired_suppressed_count"] == 1
    assert fcm.sent_messages == []
    assert not any(
        name == "query" and call["TableName"] == INSTALLATIONS_TABLE for name, call in ddb.calls
    )
    stored = ddb.deliveries[(DEVICE, EVENT_ID)]
    assert stored["status"]["S"] == "COMPLETED"
    assert stored["outcome"]["S"] == "SUPPRESSED_EXPIRED"

    retry = run(ddb, fcm, invocation(occurred_at="1970-01-12T13:46:10Z"), now=1_000_001)
    assert retry["result"] == "duplicate"
    assert fcm.sent_messages == []


def test_none_remains_preference_suppression_even_when_old() -> None:
    ddb, fcm = FakeDdb(), ScriptedFcm()
    ddb.memberships = [membership("u1", preferences=prefs("NONE"))]
    result = run(
        ddb,
        fcm,
        invocation(occurred_at="1970-01-01T00:00:00Z"),
        now=1_000_000,
    )
    assert result["result"] == "all_suppressed"
    assert result["expired_suppressed_count"] == 0


@pytest.mark.parametrize(
    "event_type,occurred_at,expected",
    [
        ("RING_DETECTED", "1970-01-12T13:46:11Z", "processed"),
        ("RING_DETECTED", "1970-01-12T13:46:10Z", "suppressed_expired"),
        ("RING_ENDED", "1970-01-12T13:45:41Z", "processed"),
        ("RING_ENDED", "1970-01-12T13:45:40Z", "suppressed_expired"),
    ],
)
def test_handler_uses_distinct_exact_age_boundaries(
    event_type: str, occurred_at: str, expected: str
) -> None:
    ddb, fcm = FakeDdb(), ScriptedFcm()
    ddb.memberships = [membership("u1", preferences=prefs("RING_ONLY"))]
    register(ddb, "iid-1", "u1")
    result = run(ddb, fcm, invocation(event=event_type, occurred_at=occurred_at), now=1_000_000)
    assert result["result"] == expected


@pytest.mark.parametrize(
    "source,occurred_at,expected,metric_counter",
    [
        (
            "unknown",
            "1970-01-12T13:46:39Z",
            "suppressed_unknown_event_time",
            "unknown_event_time_suppressed_count",
        ),
        (
            "device",
            "1970-01-12T13:46:46Z",
            "suppressed_future_event_time",
            "future_event_time_suppressed_count",
        ),
    ],
)
def test_unknown_and_excessively_future_times_are_terminal_without_fcm(
    source: str, occurred_at: str, expected: str, metric_counter: str
) -> None:
    ddb, fcm = FakeDdb(), ScriptedFcm()
    ddb.memberships = [membership("u1", preferences=prefs("RING_ONLY"))]
    register(ddb, "iid-1", "u1")
    result = run(
        ddb,
        fcm,
        invocation(timestamp_source=source, occurred_at=occurred_at),
        now=1_000_000,
    )
    assert result["result"] == expected
    assert result[metric_counter] == 1
    assert fcm.sent_messages == []
    assert ddb.deleted_installations == []


def test_expired_path_never_composes_message_or_initializes_firebase(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    ddb = FakeDdb()
    ddb.memberships = [membership("u1", preferences=prefs("RING_ONLY"))]
    register(ddb, "iid-1", "u1")

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("expired path touched FCM composition or credentials")

    monkeypatch.setattr(handler, "compose_message", forbidden)
    monkeypatch.setattr(handler, "_default_fcm_client", forbidden)
    with caplog.at_level("INFO"):
        result = handler.lambda_handler(
            invocation(occurred_at="1970-01-12T13:46:10Z"),
            None,
            ddb=ddb,
            clock=lambda: 1_000_000,
        )
    assert result["result"] == "suppressed_expired"
    assert "push_sender_suppressed" in caplog.text
    for sensitive in (DEVICE, EVENT_ID, CALL_ID, "tok"):
        assert sensitive not in caplog.text


@pytest.mark.parametrize("event_type", ["RING_DETECTED", "RING_ENDED"])
@pytest.mark.parametrize(
    "reason,source,occurred_at,temporal_metric,counter_name",
    [
        ("expired", "device", None, "PushSuppressedExpired", "expired_suppressed_count"),
        (
            "unknown_event_time",
            "unknown",
            "1970-01-12T13:46:39Z",
            "PushSuppressedUnknownEventTime",
            "unknown_event_time_suppressed_count",
        ),
        (
            "future_event_time",
            "device",
            "1970-01-12T13:46:46Z",
            "PushSuppressedFutureEventTime",
            "future_event_time_suppressed_count",
        ),
    ],
)
def test_temporal_suppression_emits_one_complete_metric_payload(
    monkeypatch: pytest.MonkeyPatch,
    event_type: str,
    reason: str,
    source: str,
    occurred_at: str | None,
    temporal_metric: str,
    counter_name: str,
) -> None:
    emitted: list[tuple[dict[str, int], dict[str, Any]]] = []
    monkeypatch.setattr(
        handler.metrics,
        "emit",
        lambda payload, **fields: emitted.append((payload, fields)),
    )
    ddb, fcm = FakeDdb(), ScriptedFcm()
    ddb.memberships = [
        membership("owner", preferences=prefs("RING_ONLY")),
        membership("member", preferences=prefs("NOTIFICATION_ONLY")),
    ]
    register(ddb, "iid-owner", "owner")
    effective_time = occurred_at
    if effective_time is None:
        effective_time = (
            "1970-01-12T13:46:10Z" if event_type == "RING_DETECTED" else "1970-01-12T13:45:40Z"
        )
    payload = invocation(
        event=event_type,
        timestamp_source=source,
        occurred_at=effective_time,
    )

    result = run(ddb, fcm, payload, now=1_000_000)

    completion = [metrics for metrics, _ in emitted if "EventsProcessed" in metrics]
    assert completion == [
        {
            "EventsProcessed": 1,
            ("RingEndedAccepted" if event_type == "RING_ENDED" else "RingDetectedAccepted"): 1,
            "MembershipsFound": 2,
            "InstallationsFound": 0,
            "Sent": 0,
            "Suppressed": 0,
            "InvalidTokens": 0,
            "TemporaryFailures": 0,
            "PermanentFailures": 0,
            "AuthConfigFailures": 0,
            temporal_metric: 1,
        }
    ]
    assert sum(metrics.get(temporal_metric, 0) for metrics, _ in emitted) == 1
    assert result[counter_name] == 1
    assert fcm.sent_messages == []

    duplicate = run(ddb, fcm, payload, now=1_000_001)
    assert duplicate["result"] == "duplicate"
    assert sum("EventsProcessed" in metrics for metrics, _ in emitted) == 1
    assert sum(metrics.get(temporal_metric, 0) for metrics, _ in emitted) == 1


def test_none_completion_metrics_remain_preference_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    emitted: list[dict[str, int]] = []
    monkeypatch.setattr(handler.metrics, "emit", lambda payload, **fields: emitted.append(payload))
    ddb, fcm = FakeDdb(), ScriptedFcm()
    ddb.memberships = [membership("u1", preferences=prefs("NONE"))]
    result = run(
        ddb,
        fcm,
        invocation(occurred_at="1970-01-01T00:00:00Z"),
        now=1_000_000,
    )
    completion = [metrics for metrics in emitted if "EventsProcessed" in metrics]
    assert result["result"] == "all_suppressed"
    assert completion[0]["Suppressed"] == 1
    assert completion[0]["Sent"] == 0
    assert not any(name.startswith("PushSuppressed") for name in completion[0])


def test_typed_auth_error_result_propagates_as_a_recoverable_failure_when_nothing_sent() -> None:
    # AUTH_OR_CONFIG_ERROR can arrive as an ordinary FcmResult (bad
    # per-message auth state reported by FCM itself), distinct from a
    # raised FirebaseCredentialError (handler.py's own pre-flight check,
    # covered separately below) -- both are the same systemic failure
    # class and must both propagate instead of completing.

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

    class RaisingFcm:
        def send(self, message: dict[str, Any]) -> FcmResult:
            raise FirebaseCredentialError("token refresh failed")

    ddb = FakeDdb()
    ddb.memberships = [membership("u1", preferences=prefs("RING_AND_NOTIFICATION"))]
    register(ddb, "iid-1", "u1")

    with pytest.raises(FirebaseCredentialError):
        run(ddb, RaisingFcm())


def test_auth_failure_short_circuits_all_remaining_installations_not_just_the_next_one() -> None:

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


def test_auth_failure_after_a_partial_success_does_not_complete_and_allows_retry() -> None:
    # Corrected semantics: a systemic auth failure -- even after some
    # installations already succeeded -- must NOT mark the event
    # COMPLETED, because that would silently and permanently drop every
    # installation that never got a real attempt. It is acceptable for the
    # already-reached installation to receive a rare duplicate on retry;
    # it is not acceptable to abandon the rest. See
    # docs/fcm-notification-sender.md, section 3/6.
    ddb = FakeDdb()
    fcm = ScriptedFcm([FcmResult("SUCCESS", 200), FcmResult("AUTH_OR_CONFIG_ERROR", 401)])
    ddb.memberships = [membership("u1", preferences=prefs("RING_AND_NOTIFICATION"))]
    register(ddb, "iid-1", "u1")
    register(ddb, "iid-2", "u1")
    register(ddb, "iid-3", "u1")

    with pytest.raises(FirebaseCredentialError):
        run(ddb, fcm)

    key = (DEVICE, EVENT_ID)
    assert ddb.deliveries[key]["status"]["S"] == "PROCESSING"
    # iid-1 succeeded, iid-2 hit the auth error, iid-3 was never even
    # attempted (short-circuited) -- none of that is silently discarded.
    assert len(fcm.sent_messages) == 2


def test_a_known_recoverable_failure_abandons_its_own_attempt() -> None:
    # handler.py must call idempotency.abandon() (not just raise) for a
    # *recognized* failure, so the lease is released immediately instead
    # of sitting there for up to LEASE_SECONDS.
    ddb = FakeDdb()
    ddb.memberships = [membership("u1", preferences=prefs("RING_AND_NOTIFICATION"))]
    register(ddb, "iid-1", "u1")

    with pytest.raises(FirebaseCredentialError):
        run(ddb, ScriptedFcm([FcmResult("AUTH_OR_CONFIG_ERROR", 401)]), now=1_000_000.0)

    key = (DEVICE, EVENT_ID)
    assert ddb.deliveries[key]["status"]["S"] == "PROCESSING"
    # abandon() sets lease_expires_at to the abandon-time "now" -- proving
    # it actually ran, rather than the lease still reflecting the original
    # claim()'s now + LEASE_SECONDS.
    assert ddb.deliveries[key]["lease_expires_at"]["N"] == str(int(1_000_000.0))


def test_immediate_retry_after_abandon_resumes_without_waiting_for_the_lease() -> None:
    # This is the fix for the real gap: a retry landing well *inside* the
    # old 90s/30s lease window must still be able to proceed immediately
    # when the previous attempt explicitly abandoned, because nothing
    # about a real AWS async-invoke retry's arrival time is guaranteed to
    # land after the lease would have expired on its own.
    ddb = FakeDdb()
    ddb.memberships = [membership("u1", preferences=prefs("RING_AND_NOTIFICATION"))]
    register(ddb, "iid-1", "u1")

    with pytest.raises(FirebaseCredentialError):
        run(ddb, ScriptedFcm([FcmResult("AUTH_OR_CONFIG_ERROR", 401)]), now=1_000_000.0)

    # A retry arriving one second later -- nowhere near LEASE_SECONDS --
    # succeeds immediately because the failed attempt abandoned its lease.
    assert idempotency.LEASE_SECONDS > 1  # the point being made requires this
    recovered = run(ddb, ScriptedFcm([FcmResult("SUCCESS", 200)]), now=1_000_000.0 + 1)
    assert recovered["result"] == "processed"
    assert recovered["sent_count"] == 1
    key = (DEVICE, EVENT_ID)
    assert ddb.deliveries[key]["status"]["S"] == "COMPLETED"


def test_real_async_retry_sequence_initial_failure_then_immediate_resume() -> None:
    # Models the specific sequence CI review asked to see modeled
    # end-to-end, rather than only advancing the clock past the lease:
    #   1. initial attempt fails
    #   2. handler abandons and raises
    #   3. a fresh invocation ("the async retry") arrives
    #   4. claim() resumes immediately (not "duplicate, try later")
    #   5. processing completes
    ddb = FakeDdb()
    ddb.memberships = [membership("u1", preferences=prefs("RING_AND_NOTIFICATION"))]
    register(ddb, "iid-1", "u1")

    # 1 & 2: the initial invocation.
    with pytest.raises(FirebaseCredentialError):
        handler.lambda_handler(
            invocation(),
            None,
            ddb=ddb,
            fcm_client=ScriptedFcm([FcmResult("AUTH_OR_CONFIG_ERROR", 401)]),
            clock=lambda: 1_000_000.0,
            sleeper=lambda _: None,
        )

    # 3 & 4 & 5: a separate, later Lambda invocation -- a fresh call into
    # lambda_handler, exactly as AWS's own async retry would make, no
    # shared in-memory state relied upon -- picks the claim back up right
    # away and finishes it.
    result = handler.lambda_handler(
        invocation(),
        None,
        ddb=ddb,
        fcm_client=ScriptedFcm([FcmResult("SUCCESS", 200)]),
        clock=lambda: 1_000_000.0 + 2,
        sleeper=lambda _: None,
    )
    assert result["result"] == "processed"
    assert result["sent_count"] == 1
    key = (DEVICE, EVENT_ID)
    assert ddb.deliveries[key]["status"]["S"] == "COMPLETED"
    assert ddb.deliveries[key]["attempt"]["N"] == "2"


def test_abandon_by_a_superseded_attempt_never_touches_a_newer_resumed_one() -> None:
    ddb = FakeDdb()
    ddb.memberships = [membership("u1", preferences=prefs("RING_AND_NOTIFICATION"))]
    register(ddb, "iid-1", "u1")

    claim_outcome, first_attempt = idempotency.claim(
        ddb, DELIVERIES_TABLE, DEVICE, EVENT_ID, now=1_000_000
    )
    assert claim_outcome == "CLAIMED"

    # The lease naturally expires and a second, independent invocation
    # resumes it before the first ever gets a chance to abandon.
    second_result = handler.lambda_handler(
        invocation(),
        None,
        ddb=ddb,
        fcm_client=ScriptedFcm([FcmResult("SUCCESS", 200)]),
        clock=lambda: 1_000_000.0 + idempotency.LEASE_SECONDS + 1,
        sleeper=lambda _: None,
    )
    assert second_result["result"] == "suppressed_expired"
    key = (DEVICE, EVENT_ID)
    assert ddb.deliveries[key]["status"]["S"] == "COMPLETED"

    # The original, now-superseded attempt finally (and pointlessly) tries
    # to abandon with its own stale attempt number -- must not disturb the
    # newer attempt's COMPLETED state.
    idempotency.abandon(
        ddb, DELIVERIES_TABLE, DEVICE, EVENT_ID, now=1_000_000 + 5, attempt=first_attempt
    )
    assert ddb.deliveries[key]["status"]["S"] == "COMPLETED"


def test_crash_without_abandon_still_recovers_via_lease_expiry() -> None:
    # An *unrecognized* crash (an exception this code never anticipated,
    # so abandon() never runs) must still fall back to the lease-timeout
    # recovery path -- the safety net abandon() is not a replacement for.
    ddb = FakeDdb()
    ddb.memberships = [membership("u1", preferences=prefs("RING_AND_NOTIFICATION"))]
    register(ddb, "iid-1", "u1")

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("unexpected bug, not a recognized recoverable failure")

    import lambdas.push_sender.handler as handler_module

    original = handler_module.memberships.active_memberships
    handler_module.memberships.active_memberships = boom  # type: ignore[method-assign]
    try:
        with pytest.raises(RuntimeError):
            run(ddb, ScriptedFcm(), now=1_000_000.0)
    finally:
        handler_module.memberships.active_memberships = original  # type: ignore[method-assign]

    key = (DEVICE, EVENT_ID)
    stored = ddb.deliveries[key]
    assert stored["status"]["S"] == "PROCESSING"
    # Crucially, abandon() never ran: the lease still reflects the
    # original claim (now + LEASE_SECONDS), not an immediately-expired one.
    assert stored["lease_expires_at"]["N"] == str(int(1_000_000.0) + idempotency.LEASE_SECONDS)

    # An immediate retry is still told to wait -- exactly the pre-existing,
    # correct behavior for a lease that has not actually expired yet.
    immediate_retry = run(ddb, ScriptedFcm(), now=1_000_000.0 + 1)
    assert immediate_retry["result"] == "duplicate"
    assert immediate_retry["claim_outcome"] == "DUPLICATE_IN_FLIGHT"

    # Only once the lease has actually expired does recovery kick in.
    recovered = run(
        ddb,
        ScriptedFcm([FcmResult("SUCCESS", 200)]),
        now=1_000_000.0 + idempotency.LEASE_SECONDS + 1,
    )
    assert recovered["result"] == "suppressed_expired"
    assert recovered["sent_count"] == 0


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
