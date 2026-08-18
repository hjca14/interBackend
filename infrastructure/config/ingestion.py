"""Validated DEV settings and deterministic names for telemetry ingestion."""

from __future__ import annotations

from dataclasses import dataclass

from aws_cdk import Duration
from aws_cdk.aws_logs import RetentionDays

from infrastructure.config.environment import EnvironmentConfig
from infrastructure.config.naming import resource_name


@dataclass(frozen=True)
class IngestionConfig:
    history_days: int = 30
    quarantine_days: int = 4
    technical_dlq_days: int = 4
    detailed_limit_per_hour: int = 200
    max_payload_bytes: int = 8 * 1024
    log_retention: RetentionDays = RetentionDays.ONE_WEEK

    def __post_init__(self) -> None:
        for value in (
            self.history_days,
            self.quarantine_days,
            self.technical_dlq_days,
            self.detailed_limit_per_hour,
            self.max_payload_bytes,
        ):
            if value <= 0:
                raise ValueError("ingestion limits must be positive")

    @property
    def history_ttl(self) -> Duration:
        return Duration.days(self.history_days)


@dataclass(frozen=True)
class IngestionNames:
    function_name: str
    invalid_quarantine_queue_name: str
    technical_dlq_name: str
    errors_alarm_name: str
    throttles_alarm_name: str
    quarantine_alarm_name: str
    technical_dlq_alarm_name: str


def ingestion_names(config: EnvironmentConfig) -> IngestionNames:
    return IngestionNames(
        function_name=resource_name(config, "ingestion", "telemetry-handler"),
        invalid_quarantine_queue_name=resource_name(
            config, "ingestion", "invalid-message-quarantine"
        ),
        technical_dlq_name=resource_name(config, "ingestion", "technical-dlq"),
        errors_alarm_name=resource_name(config, "monitoring", "ingestion-errors"),
        throttles_alarm_name=resource_name(config, "monitoring", "ingestion-throttles"),
        quarantine_alarm_name=resource_name(config, "monitoring", "quarantine-visible"),
        technical_dlq_alarm_name=resource_name(config, "monitoring", "technical-dlq-visible"),
    )
