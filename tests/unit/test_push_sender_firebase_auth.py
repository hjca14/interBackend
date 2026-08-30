from __future__ import annotations

import json
from typing import Any

import pytest
import rsa

from lambdas.push_sender.firebase_auth import (
    REFRESH_SKEW_SECONDS,
    FirebaseCredentialError,
    TokenProvider,
)


def _throwaway_test_private_key_pem() -> str:
    """A fresh, throwaway RSA key generated at test time -- never a real
    credential, never written to disk or git as static key material (which
    is also why this helper exists instead of a hardcoded PEM constant:
    scripts/check_secrets.py correctly flags a literal PEM block, and a
    generated-at-runtime key is both a cleaner test fixture and avoids that
    false positive on a legitimate, non-secret value).
    """
    _, private_key = rsa.newkeys(2048, poolsize=1)
    return private_key.save_pkcs1(format="PEM").decode()


VALID_INFO = {
    "type": "service_account",
    "project_id": "interbridge-dev",
    "private_key_id": "key-id-1",
    "private_key": _throwaway_test_private_key_pem(),
    "client_email": "sender@interbridge-dev.iam.gserviceaccount.com",
    "client_id": "12345",
    "token_uri": "https://oauth2.googleapis.com/token",
}


class FakeSecretsClient:
    def __init__(self, secret_string: str | None, *, fail: bool = False) -> None:
        self.secret_string = secret_string
        self.fail = fail
        self.calls = 0

    def get_secret_value(self, **kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        if self.fail:
            raise RuntimeError("arn:aws:secretsmanager:sa-east-1:111111111111:secret:x")
        result: dict[str, Any] = {}
        if self.secret_string is not None:
            result["SecretString"] = self.secret_string
        return result


class FakeTokenResponse:
    def __init__(self, status: int, data: dict[str, Any]) -> None:
        self.status = status
        self.data = json.dumps(data).encode()


def install_fake_token_endpoint(
    monkeypatch: pytest.MonkeyPatch, responses: list[FakeTokenResponse]
) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def fake_request() -> Any:
        def call(**kwargs: Any) -> FakeTokenResponse:
            calls.append(kwargs)
            return responses.pop(0)

        return call

    monkeypatch.setattr("lambdas.push_sender.firebase_auth._bounded_timeout_request", fake_request)
    return calls


def test_missing_secret_string_raises_without_leaking_the_secret_id() -> None:
    provider = TokenProvider(FakeSecretsClient(None), "secret-id")
    with pytest.raises(FirebaseCredentialError) as excinfo:
        provider.get_access_token()
    assert "secret-id" not in str(excinfo.value)


def test_secrets_manager_failure_is_wrapped_and_never_leaks_details() -> None:
    provider = TokenProvider(FakeSecretsClient(None, fail=True), "secret-id")
    with pytest.raises(FirebaseCredentialError) as excinfo:
        provider.get_access_token()
    assert "arn:aws" not in str(excinfo.value)


def test_malformed_json_secret_raises() -> None:
    provider = TokenProvider(FakeSecretsClient("not-json"), "secret-id")
    with pytest.raises(FirebaseCredentialError):
        provider.get_access_token()


def test_malformed_credential_shape_raises() -> None:
    provider = TokenProvider(FakeSecretsClient(json.dumps({"type": "service_account"})), "sid")
    with pytest.raises(FirebaseCredentialError):
        provider.get_access_token()


def test_successful_refresh_returns_and_caches_the_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secrets_client = FakeSecretsClient(json.dumps(VALID_INFO))
    calls = install_fake_token_endpoint(
        monkeypatch, [FakeTokenResponse(200, {"access_token": "token-1", "expires_in": 3600})]
    )
    clock = iter([1000.0, 1001.0, 1002.0]).__next__
    provider = TokenProvider(secrets_client, "secret-id", clock=clock)

    token = provider.get_access_token()
    assert token == "token-1"
    assert secrets_client.calls == 1

    # Second call within the cache window must not re-fetch the secret or
    # re-hit the token endpoint.
    token_again = provider.get_access_token()
    assert token_again == "token-1"
    assert secrets_client.calls == 1
    assert len(calls) == 1


def test_token_within_skew_window_of_expiry_is_refreshed(monkeypatch: pytest.MonkeyPatch) -> None:
    # google-auth computes Credentials.expiry from real wall-clock time, so
    # this test drives the cache comparison directly (the logic under
    # test) rather than trying to fake time all the way through
    # google-auth's own internals.
    secrets_client = FakeSecretsClient(json.dumps(VALID_INFO))
    install_fake_token_endpoint(
        monkeypatch, [FakeTokenResponse(200, {"access_token": "token-2", "expires_in": 3600})]
    )
    provider = TokenProvider(secrets_client, "secret-id", clock=lambda: 2000.0)
    provider._cached_token = "token-1"
    provider._cached_expiry = 2000.0 + REFRESH_SKEW_SECONDS - 1

    token = provider.get_access_token()
    assert token == "token-2"


def test_token_outside_skew_window_is_reused_without_a_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secrets_client = FakeSecretsClient(json.dumps(VALID_INFO))
    calls = install_fake_token_endpoint(monkeypatch, [])
    provider = TokenProvider(secrets_client, "secret-id", clock=lambda: 2000.0)
    provider._cached_token = "token-1"
    provider._cached_expiry = 2000.0 + REFRESH_SKEW_SECONDS + 100

    token = provider.get_access_token()
    assert token == "token-1"
    assert calls == []
    assert secrets_client.calls == 0


def test_token_endpoint_failure_is_wrapped(monkeypatch: pytest.MonkeyPatch) -> None:
    secrets_client = FakeSecretsClient(json.dumps(VALID_INFO))

    def fake_request() -> Any:
        def call(**kwargs: Any) -> Any:
            raise TimeoutError("token endpoint unreachable")

        return call

    monkeypatch.setattr("lambdas.push_sender.firebase_auth._bounded_timeout_request", fake_request)
    provider = TokenProvider(secrets_client, "secret-id", clock=lambda: 1000.0)
    with pytest.raises(FirebaseCredentialError):
        provider.get_access_token()


def test_naive_utc_expiry_is_interpreted_as_utc_not_local_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # google-auth's Credentials.expiry is a *naive* datetime that already
    # represents UTC (see google.auth._helpers.utcnow). Regression test for
    # a real bug caught during development: calling .timestamp() on it
    # directly would silently reinterpret it in the system's local
    # timezone. This bypasses the real Credentials.refresh() entirely so
    # the assertion is independent of the machine's own timezone.
    from datetime import datetime

    class FakeCredentials:
        token: str | None = None
        expiry: datetime | None = None

        def refresh(self, request: object) -> None:
            self.token = "token-1"
            self.expiry = datetime(2026, 1, 1, 12, 0, 0)  # naive, represents UTC

    secrets_client = FakeSecretsClient(json.dumps(VALID_INFO))
    provider = TokenProvider(secrets_client, "secret-id", clock=lambda: 0.0)
    provider._credentials = FakeCredentials()

    provider.get_access_token()

    import calendar

    expected = calendar.timegm((2026, 1, 1, 12, 0, 0, 0, 0, 0))
    assert provider._cached_expiry == expected


def test_project_id_is_exposed_without_requiring_a_token_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secrets_client = FakeSecretsClient(json.dumps(VALID_INFO))
    provider = TokenProvider(secrets_client, "secret-id")
    assert provider.project_id == "interbridge-dev"
    assert secrets_client.calls == 1
