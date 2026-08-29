from __future__ import annotations

from typing import Any

from domain.push.fcm_result import FcmResult
from lambdas.push_sender.fcm_client import (
    FCM_ENDPOINT_TEMPLATE,
    MAX_RETRY_DELAY_SECONDS,
    MAX_SEND_ATTEMPTS,
    FcmClient,
    send_with_retry,
)

PROJECT_ID = "interbridge-dev"
TOKEN = "fictional-access-token"


class FakeTokenSource:
    def __init__(self, token: str = TOKEN) -> None:
        self.token = token
        self.calls = 0

    def get_access_token(self) -> str:
        self.calls += 1
        return self.token


class FakeResponse:
    def __init__(
        self, status_code: int, body: object, *, headers: dict[str, str] | None = None
    ) -> None:
        self.status_code = status_code
        self._body = body
        self.headers = headers or {}

    def json(self) -> object:
        if self._body is None:
            raise ValueError("no body")
        return self._body


class FakeSession:
    def __init__(
        self, response: FakeResponse | None = None, error: Exception | None = None
    ) -> None:
        self.response = response
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append({"url": url, **kwargs})
        if self.error is not None:
            raise self.error
        assert self.response is not None
        return self.response


def client(session: FakeSession, token_source: FakeTokenSource | None = None) -> FcmClient:
    return FcmClient(
        project_id=PROJECT_ID, token_source=token_source or FakeTokenSource(), session=session
    )


def test_success_response_is_classified() -> None:
    session = FakeSession(FakeResponse(200, {"name": "projects/x/messages/1"}))
    result = client(session).send({"message": {"token": "t"}})
    assert result == FcmResult("SUCCESS", 200)


def test_request_targets_the_correct_project_and_uses_bearer_auth() -> None:
    session = FakeSession(FakeResponse(200, {}))
    client(session).send({"message": {"token": "t"}})
    call = session.calls[0]
    assert call["url"] == FCM_ENDPOINT_TEMPLATE.format(project_id=PROJECT_ID)
    assert call["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert "timeout" in call


def test_network_level_failure_is_temporary_error_without_a_status() -> None:
    session = FakeSession(error=TimeoutError("connection timed out"))
    result = client(session).send({"message": {"token": "t"}})
    assert result.outcome == "TEMPORARY_ERROR"
    assert result.http_status == 0


def test_invalid_token_response_is_classified_and_deletion_is_the_callers_job() -> None:
    body = {
        "error": {
            "code": 404,
            "message": "opaque",
            "status": "NOT_FOUND",
            "details": [{"errorCode": "UNREGISTERED"}],
        }
    }
    session = FakeSession(FakeResponse(404, body))
    result = client(session).send({"message": {"token": "t"}})
    assert result.outcome == "INVALID_TOKEN"


def test_rate_limited_response() -> None:
    session = FakeSession(FakeResponse(429, {"error": {"code": 429}}))
    result = client(session).send({"message": {"token": "t"}})
    assert result.outcome == "RATE_LIMITED"


def test_server_error_response() -> None:
    session = FakeSession(FakeResponse(503, {"error": {"code": 503}}))
    result = client(session).send({"message": {"token": "t"}})
    assert result.outcome == "TEMPORARY_ERROR"


def test_auth_error_response() -> None:
    session = FakeSession(FakeResponse(401, {"error": {"code": 401}}))
    result = client(session).send({"message": {"token": "t"}})
    assert result.outcome == "AUTH_OR_CONFIG_ERROR"


def test_malformed_json_response_body_does_not_crash_classification() -> None:
    session = FakeSession(FakeResponse(500, None))
    result = client(session).send({"message": {"token": "t"}})
    assert result.outcome == "TEMPORARY_ERROR"


def test_token_source_is_consulted_for_every_send() -> None:
    session = FakeSession(FakeResponse(200, {}))
    token_source = FakeTokenSource()
    fcm = client(session, token_source)
    fcm.send({"message": {"token": "t"}})
    fcm.send({"message": {"token": "t"}})
    assert token_source.calls == 2


def test_numeric_retry_after_header_is_parsed_onto_the_result() -> None:
    session = FakeSession(FakeResponse(429, {"error": {"code": 429}}, headers={"Retry-After": "3"}))
    result = client(session).send({"message": {"token": "t"}})
    assert result.outcome == "RATE_LIMITED"
    assert result.retry_after_seconds == 3.0


def test_http_date_retry_after_header_is_ignored_not_crashed_on() -> None:
    session = FakeSession(
        FakeResponse(
            429,
            {"error": {"code": 429}},
            headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"},
        )
    )
    result = client(session).send({"message": {"token": "t"}})
    assert result.outcome == "RATE_LIMITED"
    assert result.retry_after_seconds is None


def test_missing_retry_after_header_leaves_it_none() -> None:
    session = FakeSession(FakeResponse(200, {}))
    result = client(session).send({"message": {"token": "t"}})
    assert result.retry_after_seconds is None


class ScriptedSendClient:
    def __init__(self, results: list[FcmResult]) -> None:
        self.results = list(results)
        self.calls = 0

    def send(self, message_body: dict[str, Any]) -> FcmResult:
        self.calls += 1
        return self.results.pop(0)


def test_send_with_retry_returns_success_without_retrying() -> None:
    fcm = ScriptedSendClient([FcmResult("SUCCESS", 200)])
    sleeps: list[float] = []
    result = send_with_retry(fcm, {"message": {}}, sleeper=sleeps.append)
    assert result.outcome == "SUCCESS"
    assert fcm.calls == 1
    assert sleeps == []


def test_send_with_retry_retries_temporary_errors_and_eventually_succeeds() -> None:
    fcm = ScriptedSendClient([FcmResult("TEMPORARY_ERROR", 503), FcmResult("SUCCESS", 200)])
    sleeps: list[float] = []
    result = send_with_retry(fcm, {"message": {}}, sleeper=sleeps.append)
    assert result.outcome == "SUCCESS"
    assert fcm.calls == 2
    assert len(sleeps) == 1


def test_send_with_retry_never_retries_invalid_token_or_permanent_errors() -> None:
    for outcome in ("INVALID_TOKEN", "AUTH_OR_CONFIG_ERROR", "PERMANENT_PAYLOAD_ERROR"):
        fcm = ScriptedSendClient([FcmResult(outcome, 400), FcmResult("SUCCESS", 200)])  # type: ignore[list-item]
        result = send_with_retry(fcm, {"message": {}}, sleeper=lambda _: None)
        assert result.outcome == outcome
        assert fcm.calls == 1  # never even looked at the second scripted result


def test_send_with_retry_gives_up_after_max_attempts_and_reports_still_retryable() -> None:
    fcm = ScriptedSendClient([FcmResult("TEMPORARY_ERROR", 503)] * 10)
    sleeps: list[float] = []
    result = send_with_retry(fcm, {"message": {}}, sleeper=sleeps.append)
    assert result.outcome == "TEMPORARY_ERROR"
    assert fcm.calls == MAX_SEND_ATTEMPTS
    assert len(sleeps) == MAX_SEND_ATTEMPTS - 1


def test_send_with_retry_honors_retry_after_capped_at_the_max_delay() -> None:
    fcm = ScriptedSendClient(
        [
            FcmResult("RATE_LIMITED", 429, retry_after_seconds=999.0),
            FcmResult("SUCCESS", 200),
        ]
    )
    sleeps: list[float] = []
    send_with_retry(fcm, {"message": {}}, sleeper=sleeps.append)
    assert len(sleeps) == 1
    assert sleeps[0] <= MAX_RETRY_DELAY_SECONDS + 0.1  # + a little slack for jitter


def test_send_with_retry_backoff_is_bounded_across_all_attempts() -> None:
    # Guards against a request storm / runaway cost: even in the worst
    # case (every attempt retryable, no Retry-After), total local sleep
    # time this function can ever introduce for one installation is
    # small and bounded.
    fcm = ScriptedSendClient([FcmResult("TEMPORARY_ERROR", 503)] * MAX_SEND_ATTEMPTS)
    sleeps: list[float] = []
    send_with_retry(fcm, {"message": {}}, sleeper=sleeps.append)
    assert sum(sleeps) < MAX_RETRY_DELAY_SECONDS * (MAX_SEND_ATTEMPTS - 1) + 1.0
