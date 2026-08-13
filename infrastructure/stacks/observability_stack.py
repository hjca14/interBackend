"""ObservabilityStack: future operational visibility for the InterBridge backend.

Planned responsibilities (not yet implemented as real AWS resources):

- A small CloudWatch Dashboard covering IoT Core, Lambda, API Gateway and
  DynamoDB metrics once those resources exist in the other stacks.
- Alarms for error rates, throttling, and command delivery failures.
- Log retention policies for Lambda and IoT Core logging.

This stack is intentionally empty in this phase: a CloudWatch Dashboard
with no metrics to display (because ``DataStack``, ``IoTStack`` and
``ApiStack`` do not yet provision real resources) would be an artificial
resource created only to avoid an empty stack, which the task explicitly
avoids. Dashboards and detailed-monitoring alarms have a small but real
cost -- see ``docs/cost-controls.md`` -- so they should be added
deliberately, alongside the resources they observe, not ahead of them.

This stack intentionally does **not** attempt to reimplement AWS Cost
Explorer; cost visibility is handled by the AWS Budget described in
``docs/cost-controls.md``.

Depends on (future): ``DataStack``, ``IoTStack`` and ``ApiStack`` (to read
their metrics). Nothing should depend on ``ObservabilityStack``, so it
never becomes a source of circular dependencies.
"""

from __future__ import annotations

from typing import Any

from aws_cdk import Stack, Tags

from constructs import Construct
from infrastructure.config.environment import EnvironmentConfig


class ObservabilityStack(Stack):
    """Owns dashboards and alarms (CloudWatch) for InterBridge."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: EnvironmentConfig,
        **kwargs: Any,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        self.config = config

        for key, value in config.standard_tags.items():
            Tags.of(self).add(key, value)
        for key, value in config.component_tag("monitoring").items():
            Tags.of(self).add(key, value)

        # Intentionally no resources yet -- see module docstring.
