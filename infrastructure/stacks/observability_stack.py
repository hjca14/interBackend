"""Low-cost Phase 1E operational visibility.

This stack owns exactly three standard CloudWatch metric alarms: ingestion
Lambda errors, Lambda throttles and visible quarantine messages. It creates
no dashboard, custom/device metric, detailed monitoring or global IoT logging.

This stack intentionally does **not** attempt to reimplement AWS Cost
Explorer; cost visibility is handled by the AWS Budget described in
``docs/cost-controls.md``.

It depends only on ``IngestionStack``. Nothing depends on this stack, avoiding
a cycle and allowing observability to be removed independently.
"""

from __future__ import annotations

from typing import Any

from aws_cdk import Stack, Tags
from aws_cdk import aws_cloudwatch as cloudwatch

from constructs import Construct
from infrastructure.config.environment import EnvironmentConfig
from infrastructure.config.ingestion import ingestion_names
from infrastructure.stacks.ingestion_stack import IngestionStack


class ObservabilityStack(Stack):
    """Owns dashboards and alarms (CloudWatch) for InterBridge."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: EnvironmentConfig,
        ingestion_stack: IngestionStack,
        **kwargs: Any,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        self.config = config

        for key, value in config.standard_tags.items():
            Tags.of(self).add(key, value)
        for key, value in config.component_tag("monitoring").items():
            Tags.of(self).add(key, value)

        names = ingestion_names(config)
        self.errors_alarm = cloudwatch.Alarm(
            self,
            "IngestionErrorsAlarm",
            alarm_name=names.errors_alarm_name,
            metric=ingestion_stack.function.metric_errors(),
            threshold=1,
            evaluation_periods=1,
        )
        self.throttles_alarm = cloudwatch.Alarm(
            self,
            "IngestionThrottlesAlarm",
            alarm_name=names.throttles_alarm_name,
            metric=ingestion_stack.function.metric_throttles(),
            threshold=1,
            evaluation_periods=1,
        )
        self.quarantine_alarm = cloudwatch.Alarm(
            self,
            "QuarantineVisibleAlarm",
            alarm_name=names.quarantine_alarm_name,
            metric=ingestion_stack.quarantine_queue.metric_approximate_number_of_messages_visible(),
            threshold=1,
            evaluation_periods=1,
        )
