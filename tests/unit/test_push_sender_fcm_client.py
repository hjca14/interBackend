from __future__ import annotations

from typing import Any

from domain.push.fcm_result import FcmResult
from lambdas.push_sender.fcm_client import FCM_ENDPOINT_TEMPLATE, FcmClient

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
    def __init__(self, status_code: int, body: object) -> None:
        self.status_code = status_code
        self._body = body

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
