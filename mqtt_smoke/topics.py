"""Exact DEV protocol topics, composed from infrastructure's authority."""

from __future__ import annotations

from dataclasses import dataclass

from infrastructure.config.environment import EnvironmentConfig
from infrastructure.config.iot import (
    basic_ingest_topic,
    commands_topic,
    events_topic,
    health_topic,
    iot_names,
    responses_topic,
)


@dataclass(frozen=True)
class SmokeTopics:
    commands: str
    events: str
    health: str
    responses: str


def topics_for(device_id: str) -> SmokeTopics:
    names = iot_names(EnvironmentConfig(environment="dev", region="sa-east-1"))
    return SmokeTopics(
        commands=commands_topic(device_id),
        events=basic_ingest_topic(names.ingest_rule_name, events_topic(device_id)),
        health=basic_ingest_topic(names.ingest_rule_name, health_topic(device_id)),
        responses=basic_ingest_topic(names.response_rule_name, responses_topic(device_id)),
    )
