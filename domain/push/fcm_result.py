"""Pure classification of an FCM HTTP v1 response into a typed outcome.

No AWS, no network -- the actual HTTP call lives in
``lambdas/push_sender/fcm_client.py``, which calls :func:`classify` with
whatever it received. Classification never depends on token content or
message content, only on the HTTP status and FCM's own structured error
body, so it never needs to see (or risk logging) the token or payload.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Outcome = Literal[
    "SUCCESS",
    "INVALID_TOKEN",
    "AUTH_OR_CONFIG_ERROR",
    "RATE_LIMITED",
    "TEMPORARY_ERROR",
    "PERMANENT_PAYLOAD_ERROR",
]

# The one FCM error code that Firebase documents as a definitive,
# permanent signal the token is gone (app uninstalled, token rotated,
# etc.) -- see https://firebase.google.com/docs/cloud-messaging/manage-tokens.
# This is deliberately the ONLY code this module treats as
# INVALID_TOKEN; anything else (including SENDER_ID_MISMATCH, which could
# just as easily indicate a misconfiguration on our side) is classified
# more conservatively so a fixable/temporary problem never causes a
# permanent, unrecoverable delete of someone's installation.
_UNREGISTERED = "UNREGISTERED"
_RATE_LIMIT_CODES = frozenset({"QUOTA_EXCEEDED"})
_TEMPORARY_CODES = frozenset({"UNAVAILABLE", "INTERNAL", "UNSPECIFIED_ERROR"})


@dataclass(frozen=True)
class FcmResult:
    outcome: Outcome
    http_status: int
    # Parsed from a numeric-seconds `Retry-After` response header when FCM
    # sends one (only fcm_client.py ever sets this -- classify() only ever
    # sees status/body, never headers). None means "use the caller's own
    # default backoff".
    retry_after_seconds: float | None = None


def classify(http_status: int, body: object) -> FcmResult:
    if 200 <= http_status < 300:
        return FcmResult("SUCCESS", http_status)

    error_code = _fcm_error_code(body)
    if http_status == 404 or error_code == _UNREGISTERED:
        return FcmResult("INVALID_TOKEN", http_status)
    if http_status in (401, 403):
        return FcmResult("AUTH_OR_CONFIG_ERROR", http_status)
    if http_status == 429 or error_code in _RATE_LIMIT_CODES:
        return FcmResult("RATE_LIMITED", http_status)
    if http_status >= 500 or error_code in _TEMPORARY_CODES:
        return FcmResult("TEMPORARY_ERROR", http_status)
    if http_status == 400:
        return FcmResult("PERMANENT_PAYLOAD_ERROR", http_status)
    # An HTTP status this module does not recognize is treated as
    # temporary/retryable -- the safe default, since it never causes a
    # token to be deleted.
    return FcmResult("TEMPORARY_ERROR", http_status)


def _fcm_error_code(body: object) -> str | None:
    if not isinstance(body, dict):
        return None
    error = body.get("error")
    if not isinstance(error, dict):
        return None
    details = error.get("details")
    if not isinstance(details, list):
        return None
    for detail in details:
        if isinstance(detail, dict) and isinstance(detail.get("errorCode"), str):
            return detail["errorCode"]
    return None
