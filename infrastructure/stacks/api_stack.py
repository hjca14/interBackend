"""ApiStack: future public HTTPS API for the InterBridge backend.

Planned responsibilities (not yet implemented as real AWS resources):

- API Gateway HTTP API, the single entry point used by ``interapp``.
- Lambda functions implementing each endpoint.
- Authentication/authorization for app users (mechanism not yet decided --
  see ``CONTEXT.md``, Fase 2).
- Endpoints to list/claim devices, read device status, and send commands.
- Secure command dispatch into AWS IoT Core (publishing to
  ``interbridge/{device_id}/commands`` per the protocol), with the backend
  as the source of truth for ``issued_at``/``expires_at``.

This phase intentionally implements **no** endpoints: an endpoint that
returns a canned/fake "success" response without a real backing Lambda or
data model would be misleading to any client integrating against it, which
the task explicitly avoids.

Depends on (future): ``DataStack`` (device/ownership records) and
``IoTStack`` (to publish commands). ``IoTStack`` must not depend back on
``ApiStack`` to avoid a circular dependency.
"""

from __future__ import annotations

from typing import Any

from aws_cdk import Stack, Tags

from constructs import Construct
from infrastructure.config.environment import EnvironmentConfig


class ApiStack(Stack):
    """Owns the public HTTPS API (API Gateway + Lambda) for InterBridge."""

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
        for key, value in config.component_tag("api").items():
            Tags.of(self).add(key, value)

        # Intentionally no resources yet -- see module docstring.
