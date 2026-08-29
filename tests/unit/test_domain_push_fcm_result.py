from __future__ import annotations

import pytest

from domain.push.fcm_result import FcmResult, classify


def fcm_error(code: int, error_code: str | None = None, status: str = "ERROR") -> dict[str, object]:
    error: dict[str, object] = {"code": code, "message": "opaque", "status": status}
    if error_code is not None:
        error["details"] = [
            {
                "@type": "type.googleapis.com/google.firebase.fcm.v1.FcmError",
                "errorCode": error_code,
            }
        ]
    return {"error": error}


@pytest.mark.parametrize("status", [200, 201])
def test_success(status: int) -> None:
    assert classify(status, {"name": "projects/x/messages/1"}) == FcmResult("SUCCESS", status)


def test_unregistered_error_code_is_invalid_token() -> None:
    result = classify(400, fcm_error(400, "UNREGISTERED", "NOT_FOUND"))
    assert result == FcmResult("INVALID_TOKEN", 400)


def test_404_status_is_invalid_token_even_without_error_code() -> None:
    result = classify(404, {"error": {"code": 404, "message": "opaque"}})
    assert result == FcmResult("INVALID_TOKEN", 404)


def test_sender_id_mismatch_is_not_treated_as_invalid_token() -> None:
    # Deliberately conservative: only UNREGISTERED deletes a token.
    result = classify(403, fcm_error(403, "SENDER_ID_MISMATCH"))
    assert result.outcome != "INVALID_TOKEN"


@pytest.mark.parametrize("status", [401, 403])
def test_auth_and_config_errors(status: int) -> None:
    result = classify(status, fcm_error(status))
    assert result == FcmResult("AUTH_OR_CONFIG_ERROR", status)


def test_rate_limited_by_status() -> None:
    assert classify(429, fcm_error(429)) == FcmResult("RATE_LIMITED", 429)


def test_rate_limited_by_error_code() -> None:
    result = classify(429, fcm_error(429, "QUOTA_EXCEEDED"))
    assert result == FcmResult("RATE_LIMITED", 429)


@pytest.mark.parametrize("status", [500, 502, 503])
def test_server_errors_are_temporary(status: int) -> None:
    result = classify(status, fcm_error(status))
    assert result == FcmResult("TEMPORARY_ERROR", status)


def test_unavailable_error_code_is_temporary() -> None:
    result = classify(503, fcm_error(503, "UNAVAILABLE"))
    assert result == FcmResult("TEMPORARY_ERROR", 503)


def test_malformed_payload_error_is_permanent() -> None:
    result = classify(400, fcm_error(400, "INVALID_ARGUMENT"))
    assert result == FcmResult("PERMANENT_PAYLOAD_ERROR", 400)


def test_unrecognized_status_defaults_to_temporary_never_invalid_token() -> None:
    result = classify(418, {"unexpected": "shape"})
    assert result.outcome == "TEMPORARY_ERROR"


def test_non_dict_body_does_not_crash_classification() -> None:
    for body in (None, "plain text", [1, 2, 3], 42):
        result = classify(500, body)
        assert result.outcome == "TEMPORARY_ERROR"
