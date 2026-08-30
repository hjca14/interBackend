"""Typed environment configuration for the InterBridge backend.

This module is intentionally free of any AWS CDK imports so it can be
unit-tested in isolation and reasoned about without synthesizing a stack.

The AWS account is never hardcoded: it is read from ``CDK_DEFAULT_ACCOUNT``
(set automatically by the CDK CLI from the active credentials, or by CI) and
may be ``None`` when no credentials are available (e.g. in CI, where only
``cdk synth`` runs). The region defaults to ``sa-east-1`` when
``CDK_DEFAULT_REGION`` is not set, so synthesis never depends on AWS
credentials being present.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

PROJECT_NAME = "interbridge"
DEFAULT_ENVIRONMENT = "dev"
DEFAULT_REGION = "sa-east-1"
MANAGED_BY = "AWS-CDK"
REPOSITORY = "interBackend"

# Only "dev" is a supported deployment environment in this phase. Additional
# environments (e.g. "staging", "prod") should be added here deliberately,
# together with the review of the resources/policies that depend on them.
ALLOWED_ENVIRONMENTS = frozenset({"dev"})

# Logical components used for the "Component" resource tag. Kept here so the
# allowed values have a single source of truth.
ALLOWED_COMPONENTS = frozenset(
    {"iot", "api", "database", "monitoring", "ingestion", "notifications"}
)


@dataclass(frozen=True)
class EnvironmentConfig:
    """Centralized, typed configuration shared by every stack."""

    project: str = PROJECT_NAME
    environment: str = DEFAULT_ENVIRONMENT
    region: str = DEFAULT_REGION
    account: str | None = None
    managed_by: str = MANAGED_BY
    repository: str = REPOSITORY
    standard_tags: dict[str, str] = field(init=False)

    def __post_init__(self) -> None:
        if self.environment not in ALLOWED_ENVIRONMENTS:
            allowed = ", ".join(sorted(ALLOWED_ENVIRONMENTS))
            raise ValueError(
                f"Invalid environment {self.environment!r}. Allowed values: {allowed}."
            )
        tags = {
            "Project": "InterBridge",
            "Environment": self.environment,
            "ManagedBy": self.managed_by,
            "Repository": self.repository,
        }
        object.__setattr__(self, "standard_tags", tags)

    def component_tag(self, component: str) -> dict[str, str]:
        """Return the ``Component`` tag for a given logical component.

        Raises ``ValueError`` for components outside ``ALLOWED_COMPONENTS`` so
        typos are caught at synth time rather than producing an inconsistent
        tag in the deployed template.
        """
        if component not in ALLOWED_COMPONENTS:
            allowed = ", ".join(sorted(ALLOWED_COMPONENTS))
            raise ValueError(f"Invalid component {component!r}. Allowed values: {allowed}.")
        return {"Component": component}


def get_environment_config(environment: str | None = None) -> EnvironmentConfig:
    """Build the typed configuration for the current run.

    Precedence for ``environment``: explicit argument, then the
    ``INTERBRIDGE_ENVIRONMENT`` environment variable, then ``dev``.

    Precedence for the region: the ``CDK_DEFAULT_REGION`` environment
    variable (set by the CDK CLI from the active AWS profile), falling back
    to ``sa-east-1`` so ``cdk synth`` works without AWS credentials.

    The account is read from ``CDK_DEFAULT_ACCOUNT`` only. It is never
    hardcoded and is left as ``None`` when unset (synth-only / CI usage).
    """
    resolved_environment = environment or os.environ.get(
        "INTERBRIDGE_ENVIRONMENT", DEFAULT_ENVIRONMENT
    )
    resolved_region = os.environ.get("CDK_DEFAULT_REGION") or DEFAULT_REGION
    resolved_account = os.environ.get("CDK_DEFAULT_ACCOUNT") or None

    return EnvironmentConfig(
        environment=resolved_environment,
        region=resolved_region,
        account=resolved_account,
    )
