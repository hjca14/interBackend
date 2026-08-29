"""Firebase OAuth2 access token provider for the FCM HTTP v1 API.

Fetches the Google service-account credential JSON from AWS Secrets
Manager (never created or rotated by this code -- see
``docs/fcm-notification-sender.md`` for the manual setup procedure) and
exchanges it for an FCM-scoped OAuth2 access token via ``google-auth``,
the same library Google publishes and maintains for exactly this flow.
This module never signs a JWT itself; ``google.oauth2.service_account``
does that internally.

Both the parsed credential and the current access token are cached only
in this instance's memory (i.e. for the lifetime of one Lambda execution
environment) and are never logged.
"""

from __future__ import annotations

import json
import time
from datetime import UTC
from typing import Any, Protocol

FCM_SCOPES = ("https://www.googleapis.com/auth/firebase.messaging",)
# Refresh this many seconds before actual expiry so a cached token is never
# handed out right at the edge of expiring mid-request.
REFRESH_SKEW_SECONDS = 300
# Explicit, short timeout for the token endpoint call -- google-auth's own
# default is 120s, far too long for a Lambda on the request's critical
# path. See _BoundedTimeoutRequest below for how this is enforced.
TOKEN_REQUEST_TIMEOUT_SECONDS = 5
FALLBACK_TOKEN_LIFETIME_SECONDS = 3600


class SecretsManagerClient(Protocol):
    def get_secret_value(self, **kwargs: Any) -> dict[str, Any]: ...


class FirebaseCredentialError(RuntimeError):
    """A Secrets Manager or token-exchange failure. Never carries secret
    content -- only a fixed, safe message.
    """


def _bounded_timeout_request() -> Any:
    """A google-auth HTTP transport whose calls default to our own short,
    explicit timeout instead of the library's 120s default.

    ``google.oauth2._client.jwt_grant`` (invoked by
    ``Credentials.refresh()``) never passes ``timeout`` itself, so
    overriding the callable's default via ``setdefault`` reliably applies
    here without depending on undocumented internals beyond the public
    ``Request.__call__`` signature.
    """
    import google.auth.transport.requests

    class _BoundedTimeoutRequest(google.auth.transport.requests.Request):
        def __call__(self, *args: Any, **kwargs: Any) -> Any:
            kwargs.setdefault("timeout", TOKEN_REQUEST_TIMEOUT_SECONDS)
            return super().__call__(*args, **kwargs)

    return _BoundedTimeoutRequest()


class TokenProvider:
    def __init__(
        self,
        secrets_client: SecretsManagerClient,
        secret_id: str,
        *,
        clock: Any = time.time,
    ) -> None:
        self._secrets_client = secrets_client
        self._secret_id = secret_id
        self._clock = clock
        self._credentials: Any = None
        self._cached_token: str | None = None
        self._cached_expiry: float = 0.0

    def _load_credentials(self) -> Any:
        if self._credentials is not None:
            return self._credentials
        try:
            response = self._secrets_client.get_secret_value(SecretId=self._secret_id)
        except Exception as error:
            raise FirebaseCredentialError("unable to load Firebase credential") from error
        raw = response.get("SecretString") if isinstance(response, dict) else None
        if not isinstance(raw, str):
            raise FirebaseCredentialError("Firebase credential secret has no string value")
        try:
            info = json.loads(raw)
        except json.JSONDecodeError as error:
            raise FirebaseCredentialError("Firebase credential secret is not valid JSON") from error
        try:
            from google.oauth2 import service_account

            self._credentials = service_account.Credentials.from_service_account_info(
                info, scopes=FCM_SCOPES
            )
        except (ValueError, KeyError) as error:
            raise FirebaseCredentialError("Firebase credential secret is malformed") from error
        return self._credentials

    @property
    def project_id(self) -> str:
        credentials = self._load_credentials()
        project_id = getattr(credentials, "project_id", None)
        if not isinstance(project_id, str) or not project_id:
            raise FirebaseCredentialError("Firebase credential secret is missing project_id")
        return project_id

    def get_access_token(self) -> str:
        """Return a valid access token, refreshing (and re-caching) if the
        cached one is missing or within ``REFRESH_SKEW_SECONDS`` of expiry.
        """
        now = self._clock()
        if self._cached_token is not None and now < self._cached_expiry - REFRESH_SKEW_SECONDS:
            return self._cached_token
        credentials = self._load_credentials()
        try:
            credentials.refresh(_bounded_timeout_request())
        except Exception as error:
            raise FirebaseCredentialError("unable to refresh Firebase access token") from error
        if not credentials.token:
            raise FirebaseCredentialError("Firebase credential refresh did not yield a token")
        self._cached_token = credentials.token
        self._cached_expiry = (
            # google-auth's Credentials.expiry is a *naive* datetime that
            # represents UTC (see google.auth._helpers.utcnow) -- calling
            # .timestamp() on it directly would misinterpret it in the
            # system's local timezone, so tzinfo is attached explicitly
            # first.
            credentials.expiry.replace(tzinfo=UTC).timestamp()
            if credentials.expiry is not None
            else now + FALLBACK_TOKEN_LIFETIME_SECONDS
        )
        return self._cached_token
