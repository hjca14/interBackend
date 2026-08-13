"""Typed, centralized configuration for environment, region, naming and tags."""

from infrastructure.config.environment import EnvironmentConfig, get_environment_config
from infrastructure.config.naming import resource_name, stack_id

__all__ = [
    "EnvironmentConfig",
    "get_environment_config",
    "resource_name",
    "stack_id",
]
