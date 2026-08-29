"""FCM HTTP v1 client.

Real network I/O only -- message composition lives in
``domain/push/payload.py`` and response classification in
``domain/push/fcm_result.py``. Never logs the token, the access token, or
the raw response body; callers only ever see an :class:`FcmResult`.
"""

from __future__ import annotations

from typing import Any, Protocol

from domain.push.fcm_result import FcmResult, classify

FCM_ENDPOINT_TEMPLATE = "https://fcm.googleapis.com/v1/projects/{project_id}/messages:send"
FCM_REQUEST_TIMEOUT_SECONDS = 8


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
        return classify(response.status_code, body)
