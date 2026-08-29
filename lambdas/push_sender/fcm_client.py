"""FCM HTTP v1 client.

Real network I/O only -- message composition lives in
``domain/push/payload.py`` and response classification in
``domain/push/fcm_result.py``. Never logs the token, the access token, or
the raw response body; callers only ever see an :class:`FcmResult`.
"""

from __future__ import annotations

import random
import time
from typing import Any, Protocol

from domain.push.fcm_result import FcmResult, classify

FCM_ENDPOINT_TEMPLATE = "https://fcm.googleapis.com/v1/projects/{project_id}/messages:send"
FCM_REQUEST_TIMEOUT_SECONDS = 8

# Local, in-invocation retry policy for RATE_LIMITED/TEMPORARY_ERROR only --
# never for INVALID_TOKEN, AUTH_OR_CONFIG_ERROR or PERMANENT_PAYLOAD_ERROR,
# which are never worth retrying immediately. Deliberately small: this runs
# inside push_sender's own 20-second Lambda timeout, once per installation,
# so it must never be able to turn a large fan-out into a request storm or
# eat the whole time budget on its own.
MAX_SEND_ATTEMPTS = 3
RETRY_BACKOFF_BASE_SECONDS = 0.2
MAX_RETRY_DELAY_SECONDS = 2.0
RETRYABLE_OUTCOMES = frozenset({"RATE_LIMITED", "TEMPORARY_ERROR"})


class TokenSource(Protocol):
    def get_access_token(self) -> str: ...


class HttpSession(Protocol):
    def post(self, url: str, **kwargs: Any) -> Any: ...


class FcmClient:
    def __init__(self, *, project_id: str, token_source: TokenSource, session: HttpSession) -> None:
        self._project_id = project_id
        self._token_source = token_source
        self._session = session

    def send(self, message_body: dict[str, Any]) -> FcmResult:
        access_token = self._token_source.get_access_token()
        url = FCM_ENDPOINT_TEMPLATE.format(project_id=self._project_id)
        try:
            response = self._session.post(
                url,
                json=message_body,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json; charset=UTF-8",
                },
                timeout=FCM_REQUEST_TIMEOUT_SECONDS,
            )
        except Exception:
            # No HTTP status was ever received (timeout, connection error,
            # DNS, ...): always temporary/retryable, never a reason to
            # delete a token.
            return FcmResult("TEMPORARY_ERROR", 0)
        try:
            body = response.json()
        except ValueError:
            body = None
        result = classify(response.status_code, body)
        retry_after = _parse_retry_after(getattr(response, "headers", None))
        if retry_after is not None:
            result = FcmResult(result.outcome, result.http_status, retry_after)
        return result


def send_with_retry(
    client: FcmClient, message_body: dict[str, Any], *, sleeper: Any = time.sleep
) -> FcmResult:
    """Sends once, then retries locally -- only for RATE_LIMITED/
    TEMPORARY_ERROR, only up to ``MAX_SEND_ATTEMPTS`` total, honoring a
    numeric-seconds ``Retry-After`` when FCM sent one (capped at
    ``MAX_RETRY_DELAY_SECONDS`` either way so one aggressive Retry-After
    can't consume the whole Lambda timeout by itself). Never retries
    INVALID_TOKEN/AUTH_OR_CONFIG_ERROR/PERMANENT_PAYLOAD_ERROR -- those are
    never resolved by trying again immediately.

    If every attempt is exhausted and the outcome is still retryable, that
    is returned as-is; the caller (``lambdas/push_sender/handler.py``) is
    responsible for deciding what an unresolved temporary failure means
    for the delivery as a whole -- this function only ever concerns itself
    with one message to one installation.
    """
    result = client.send(message_body)
    attempt = 1
    while result.outcome in RETRYABLE_OUTCOMES and attempt < MAX_SEND_ATTEMPTS:
        if result.retry_after_seconds is not None:
            delay = min(result.retry_after_seconds, MAX_RETRY_DELAY_SECONDS)
        else:
            delay = min(RETRY_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)), MAX_RETRY_DELAY_SECONDS)
        sleeper(delay + random.uniform(0, 0.05))
        result = client.send(message_body)
        attempt += 1
    return result


def _parse_retry_after(headers: Any) -> float | None:
    if headers is None or not hasattr(headers, "get"):
        return None
    value = headers.get("Retry-After")
    if value is None:
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        # The HTTP-date form of Retry-After is not supported -- falls back
        # to the caller's own default backoff instead of guessing.
        return None
    return max(0.0, seconds)
