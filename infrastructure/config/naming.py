"""Deterministic naming helpers so every resource/stack name is derived from
the same typed configuration instead of being hand-typed at each call site.
"""

from __future__ import annotations

from infrastructure.config.environment import EnvironmentConfig


def resource_name(config: EnvironmentConfig, component: str, resource: str) -> str:
    """Build a deterministic, lower-case, hyphenated resource name.

    Example: ``resource_name(config, "iot", "commands-rule")`` with the
    default config returns ``"interbridge-dev-iot-commands-rule"``.
    """
    parts = [config.project, config.environment, component, resource]
    return "-".join(part.strip().lower() for part in parts if part)


def stack_id(config: EnvironmentConfig, stack_name: str) -> str:
    """Build a deterministic CDK stack id/construct id.

    Example: ``stack_id(config, "Data")`` with the default config returns
    ``"InterBridge-Dev-DataStack"``.
    """
    return f"InterBridge-{config.environment.capitalize()}-{stack_name}Stack"
