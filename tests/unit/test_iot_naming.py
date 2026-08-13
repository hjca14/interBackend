from __future__ import annotations

from infrastructure.config.environment import EnvironmentConfig
from infrastructure.config.iot import (
    THING_NAME_POLICY_VARIABLE,
    basic_ingest_topic,
    commands_topic,
    events_topic,
    health_topic,
    iot_names,
    responses_topic,
)


def test_iot_resource_names_are_deterministic_and_match_requested_pattern() -> None:
    config = EnvironmentConfig()
    names = iot_names(config)

    assert names.thing_type_name == "interbridge-dev-device"
    assert names.thing_group_name == "interbridge-dev-devices"
    assert names.device_policy_name == "interbridge-dev-device-policy"
    assert names.thing_type_description == "InterBridge device type for the dev environment"


def test_reserved_rule_names_use_underscores_not_hyphens() -> None:
    # AWS::IoT::TopicRule rule names may only contain [a-zA-Z0-9_] -- no
    # hyphens -- unlike every other IoT resource name in this project.
    config = EnvironmentConfig()
    names = iot_names(config)

    assert names.ingest_rule_name == "interbridge_dev_ingest_rule"
    assert names.response_rule_name == "interbridge_dev_response_rule"
    assert "-" not in names.ingest_rule_name
    assert "-" not in names.response_rule_name


def test_topics_use_policy_variable_by_default() -> None:
    assert commands_topic() == f"interbridge/{THING_NAME_POLICY_VARIABLE}/commands"
    assert events_topic() == f"interbridge/{THING_NAME_POLICY_VARIABLE}/events"
    assert health_topic() == f"interbridge/{THING_NAME_POLICY_VARIABLE}/health"
    assert responses_topic() == f"interbridge/{THING_NAME_POLICY_VARIABLE}/responses"


def test_topics_accept_an_explicit_thing_name() -> None:
    # Protocol: ThingName == device_id == MQTT ClientId.
    assert commands_topic("ib-" + "a" * 32) == f"interbridge/ib-{'a' * 32}/commands"


def test_basic_ingest_topic_uses_dollar_aws_rules_prefix() -> None:
    topic = basic_ingest_topic("interbridge_dev_ingest_rule", events_topic())
    assert topic == (
        f"$aws/rules/interbridge_dev_ingest_rule/interbridge/{THING_NAME_POLICY_VARIABLE}/events"
    )
    assert topic.startswith("$aws/rules/")
