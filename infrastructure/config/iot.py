"""Centralized AWS IoT Core naming, topics and reserved rule names.

This is the single source of truth for every IoT resource name and topic
string used by ``IoTStack`` (``infrastructure/stacks/iot_stack.py``), so
none of it is duplicated or hand-typed at call sites.

Topic strings mirror ``interBridge/docs/communication-protocol.md`` (the
authoritative protocol spec) exactly -- do not invent or rename topics
here. As of protocol v1:

- Devices subscribe to receive commands on ``interbridge/{device_id}/commands``.
- Devices publish events, health telemetry and command responses via AWS
  IoT Basic Ingest, i.e. directly to
  ``$aws/rules/{rule_name}/interbridge/{device_id}/{events,health,responses}``.
  Publishing to that special prefix invokes the rule directly; the message
  never lands on the plain ``interbridge/{device_id}/...`` topic.
- The protocol requires the AWS IoT Thing name *and* the MQTT Client ID to
  both equal the device's stable ``device_id``. This module never
  hardcodes a specific device: every topic/ARN is built against the AWS
  IoT policy variable ``${iot:Connection.Thing.ThingName}``, which AWS IoT
  resolves per-connection from the Thing attached to the client
  certificate, so a single shared policy safely scopes every device to its
  own topics.

The Basic Ingest rules themselves (``AWS::IoT::TopicRule``) are **not**
created in this phase (Fase 1B) -- only their names are reserved here so
the IoT Policy can pre-authorize the exact publish paths the protocol
defines. Creating the rules is Fase 1E work (see ``docs/phases.md``).
"""

from __future__ import annotations

from dataclasses import dataclass

from infrastructure.config.environment import EnvironmentConfig

# AWS IoT policy variable resolved server-side, per-connection, from the
# Thing attached to the certificate in use. Using it (instead of a
# hardcoded device id) is what lets one policy be shared safely by every
# device -- see the module docstring.
THING_NAME_POLICY_VARIABLE = "${iot:Connection.Thing.ThingName}"

# Fase 1B.2 hardening: requires that the certificate actually be attached
# to an AWS IoT Thing (via AttachThingPrincipal) before ${iot:Connection.
# Thing.ThingName} is trusted for authorization. Without this condition, a
# certificate that has never been attached to any Thing would still make
# ${iot:Connection.Thing.ThingName} resolve to an empty string, which (in
# a misconfigured/edge-case policy) could otherwise be exploitable. This is
# the exact condition AWS's own documentation uses for every "registered
# device" policy example -- see
# https://docs.aws.amazon.com/iot/latest/developerguide/pub-sub-policy.html
# and https://docs.aws.amazon.com/iot/latest/developerguide/thing-policy-variables.html
# (operator "Bool", key "iot:Connection.Thing.IsAttached", value "true").
THING_ATTACHED_CONDITION: dict[str, dict[str, str]] = {
    "Bool": {"iot:Connection.Thing.IsAttached": "true"}
}

# Every custom application topic is namespaced under this literal prefix
# per the protocol spec.
TOPIC_NAMESPACE = "interbridge"


def commands_topic(thing_name: str = THING_NAME_POLICY_VARIABLE) -> str:
    """Topic a device subscribes to / receives commands on."""
    return f"{TOPIC_NAMESPACE}/{thing_name}/commands"


def events_topic(thing_name: str = THING_NAME_POLICY_VARIABLE) -> str:
    """Underlying (pre-Basic-Ingest-prefix) topic for device events."""
    return f"{TOPIC_NAMESPACE}/{thing_name}/events"


def health_topic(thing_name: str = THING_NAME_POLICY_VARIABLE) -> str:
    """Underlying (pre-Basic-Ingest-prefix) topic for device health telemetry."""
    return f"{TOPIC_NAMESPACE}/{thing_name}/health"


def responses_topic(thing_name: str = THING_NAME_POLICY_VARIABLE) -> str:
    """Underlying (pre-Basic-Ingest-prefix) topic for command responses."""
    return f"{TOPIC_NAMESPACE}/{thing_name}/responses"


def basic_ingest_topic(rule_name: str, topic: str) -> str:
    """Build the literal AWS IoT Basic Ingest publish topic for a rule.

    A device publishes directly to this ``$aws/rules/...`` topic (never to
    the plain ``topic``) so AWS IoT Core invokes the rule without a
    standard broker round-trip.
    """
    return f"$aws/rules/{rule_name}/{topic}"


@dataclass(frozen=True)
class IotNames:
    """Deterministic names for every IoT resource this stack owns or reserves."""

    thing_type_name: str
    thing_type_description: str
    thing_group_name: str
    device_policy_name: str
    # Reserved for Fase 1E: AWS::IoT::TopicRule names may only contain
    # [a-zA-Z0-9_] (no hyphens), unlike the other IoT resource names above,
    # so these deliberately use underscores instead of the project's usual
    # hyphenated convention.
    ingest_rule_name: str
    response_rule_name: str


def iot_names(config: EnvironmentConfig) -> IotNames:
    """Build every IoT resource/rule name from the shared environment config."""
    return IotNames(
        thing_type_name=f"{config.project}-{config.environment}-device",
        thing_type_description=(
            f"InterBridge device type for the {config.environment} environment"
        ),
        thing_group_name=f"{config.project}-{config.environment}-devices",
        device_policy_name=f"{config.project}-{config.environment}-device-policy",
        ingest_rule_name=f"{config.project}_{config.environment}_ingest_rule",
        response_rule_name=f"{config.project}_{config.environment}_response_rule",
    )
