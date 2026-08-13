"""IoTStack: future AWS IoT Core layer for the InterBridge backend.

Planned responsibilities (not yet implemented as real AWS resources):

- AWS IoT Core endpoint configuration for MQTT/TLS device connections.
- IoT policies scoped to the topics defined by the protocol (least
  privilege: a device may only publish/subscribe to its own
  ``interbridge/{device_id}/...`` topics).
- IoT Rules implementing AWS IoT Basic Ingest for device events, routing
  them toward Lambda/DynamoDB (owned by ``DataStack``) without a broker
  round-trip.
- Fleet Provisioning / device registration workflow.
- CloudWatch Logs / metrics for the MQTT broker (consumed by
  ``ObservabilityStack``).

This phase intentionally does **not** create:

- Any X.509 certificate, private key, or IoT "Thing" for an individual
  device. Device identity is provisioned out-of-band per
  ``interBridge/docs/communication-protocol.md`` and must never be
  generated from, or committed to, this repository.
- Any IoT policy yet, since the exact topic/action scoping depends on the
  ``ApiStack`` and ``DataStack`` contracts that are still open (see
  ``CONTEXT.md``).

Depends on (future): ``DataStack`` (for table ARNs used by Basic Ingest
rules). Should not depend on ``ApiStack`` to avoid a circular dependency
between "commands sent by the API" and "events ingested by IoT".
"""

from __future__ import annotations

from typing import Any

from aws_cdk import Stack, Tags

from constructs import Construct
from infrastructure.config.environment import EnvironmentConfig


class IoTStack(Stack):
    """Owns AWS IoT Core resources (policies, rules, Basic Ingest) for InterBridge."""

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
        for key, value in config.component_tag("iot").items():
            Tags.of(self).add(key, value)

        # Intentionally no resources yet -- see module docstring.
