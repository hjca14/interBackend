"""Validated, privacy-safe push installation values."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field

MAX_TOKEN_LENGTH = 4096
MAX_APP_VERSION_LENGTH = 64


def token_hash(token: str) -> str:
    """Hash the exact UTF-8 token; whitespace is deliberately significant."""
    if not isinstance(token, str) or not token or len(token) > MAX_TOKEN_LENGTH:
        raise ValueError("invalid push token")
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass(frozen=True)
class PushInstallation:
    user_id: str
    installation_id: str
    platform: str
    push_provider: str
    token: str = field(repr=False)
    app_id: str
    app_version: str
    created_at: int
    updated_at: int

    def __post_init__(self) -> None:
        try:
            parsed = uuid.UUID(self.installation_id)
        except (ValueError, TypeError, AttributeError):
            raise ValueError("invalid installation identifier") from None
        if str(parsed) != self.installation_id.lower():
            raise ValueError("invalid installation identifier")
        token_hash(self.token)
        if self.platform != "ANDROID" or self.push_provider != "FCM":
            raise ValueError("unsupported push configuration")
        if self.app_id != "com.interbridge.app":
            raise ValueError("unsupported app identifier")
        if (
            not isinstance(self.app_version, str)
            or not self.app_version
            or len(self.app_version) > MAX_APP_VERSION_LENGTH
        ):
            raise ValueError("invalid app version")
        if not isinstance(self.created_at, int) or not isinstance(self.updated_at, int):
            raise ValueError("invalid timestamp")

    def to_item(self) -> dict[str, str | int]:
        return {
            "user_id": self.user_id,
            "installation_id": self.installation_id,
            "platform": self.platform,
            "push_provider": self.push_provider,
            "token": self.token,
            "token_hash": token_hash(self.token),
            "app_id": self.app_id,
            "app_version": self.app_version,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
