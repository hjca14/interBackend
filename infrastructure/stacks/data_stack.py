"""DataStack: future persistence layer for the InterBridge backend.

Planned responsibilities (not yet implemented as real AWS resources):

- Device registry (one item per physical InterBridge device).
- User <-> device ownership / linking records.
- Current device status (mirrors the AWS IoT Device Shadow reported state).
- Command idempotency records, keyed by the protocol's ``command_id``
  (see ``interBridge/docs/communication-protocol.md``), so a retried or
  duplicated command is never applied twice.
- Any event history required by the product (retention/shape TBD).

The single-table vs. multi-table DynamoDB design, partition/sort key
strategy, and GSIs are intentionally **not** decided yet -- see
``CONTEXT.md`` ("Pendencias e decisoes abertas"). This stack exists so the
rest of the app (naming, tagging, dependency wiring) is exercised end to end
without inventing a data model prematurely. It deliberately declares no
DynamoDB tables or other resources yet: doing so before the model is closed
would risk a costly migration and would not reflect a decision that has
actually been made.
"""

from __future__ import annotations

from typing import Any

from aws_cdk import Stack, Tags

from constructs import Construct
from infrastructure.config.environment import EnvironmentConfig


class DataStack(Stack):
    """Owns the persistence layer (DynamoDB tables) for InterBridge.

    No other stack should provision its own data storage: ``IoTStack`` and
    ``ApiStack`` will depend on table references exported from here once the
    schema is defined, avoiding circular dependencies.
    """

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
        for key, value in config.component_tag("database").items():
            Tags.of(self).add(key, value)

        # Intentionally no resources yet -- see module docstring.
