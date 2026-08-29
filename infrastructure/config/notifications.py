"""Validated DEV settings and deterministic names for the FCM push sender
(Fase 3B.6/3B.7).
"""

from __future__ import annotations

from dataclasses import dataclass

from aws_cdk.aws_logs import RetentionDays

from infrastructure.config.environment import EnvironmentConfig
from infrastructure.config.naming import resource_name


@dataclass(frozen=True)
class NotificationConfig:
    # How long a claimed idempotency record stays authoritative before its
    # TTL clears it -- see docs/fcm-notification-sender.md for the chosen
    # semantics (recovery from a crashed mid-fan-out attempt relies on this
    # TTL, not an active reclaim).
    delivery_retention_hours: int = 2
    async_retry_attempts: int = 2
    async_dlq_retention_days: int = 4
    log_retention: RetentionDays = RetentionDays.ONE_WEEK

    def __post_init__(self) -> None:
        if self.delivery_retention_hours <= 0:
            raise ValueError("delivery_retention_hours must be positive")
        if not 0 <= self.async_retry_attempts <= 2:
            raise ValueError("async_retry_attempts must be between 0 and 2")
        if self.async_dlq_retention_days <= 0:
            raise ValueError("async_dlq_retention_days must be positive")


@dataclass(frozen=True)
class NotificationNames:
    function_name: str
    async_failure_dlq_name: str
    firebase_credentials_secret_name: str
    errors_alarm_name: str
    throttles_alarm_name: str
    async_failure_alarm_name: str


def notification_names(config: EnvironmentConfig) -> NotificationNames:
    return NotificationNames(
        function_name=resource_name(config, "notifications", "push-sender"),
        async_failure_dlq_name=resource_name(config, "notifications", "push-sender-dlq"),
        # Referenced, never created by this project -- see the manual
        # procedure in docs/fcm-notification-sender.md. Matches this
        # project's usual naming convention so the operator knows exactly
        # what to create.
        firebase_credentials_secret_name=resource_name(
            config, "notifications", "firebase-credentials"
        ),
        errors_alarm_name=resource_name(config, "monitoring", "push-sender-errors"),
        throttles_alarm_name=resource_name(config, "monitoring", "push-sender-throttles"),
        async_failure_alarm_name=resource_name(config, "monitoring", "push-sender-async-failures"),
    )
