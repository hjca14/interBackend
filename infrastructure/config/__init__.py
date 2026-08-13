"""Typed, centralized configuration for environment, region, naming and tags."""

from infrastructure.config.data import DataNames, data_names
from infrastructure.config.environment import EnvironmentConfig, get_environment_config
from infrastructure.config.iot import (
    THING_ATTACHED_CONDITION,
    THING_NAME_POLICY_VARIABLE,
    IotNames,
    basic_ingest_topic,
    commands_topic,
    events_topic,
    health_topic,
    iot_names,
    responses_topic,
)
from infrastructure.config.naming import resource_name, stack_id

__all__ = [
    "THING_ATTACHED_CONDITION",
    "THING_NAME_POLICY_VARIABLE",
    "DataNames",
    "EnvironmentConfig",
    "IotNames",
    "basic_ingest_topic",
    "commands_topic",
    "data_names",
    "events_topic",
    "get_environment_config",
    "health_topic",
    "iot_names",
    "resource_name",
    "responses_topic",
    "stack_id",
]
