"""IoTStack: AWS IoT Core layer for the InterBridge backend.

Fase 1B scope (this phase) -- shared infrastructure only, no individual
device identity:

- One ``AWS::IoT::ThingType`` for InterBridge devices in this environment.
- One ``AWS::IoT::ThingGroup`` to hold future dev devices (empty for now).
- One shared, least-privilege ``AWS::IoT::Policy`` that every device
  certificate will eventually attach to. It scopes every permission to the
  connecting device's own Thing name via the AWS IoT policy variable
  ``${iot:Connection.Thing.ThingName}`` (see
  ``infrastructure/config/iot.py``), so it is safe to share across every
  device without granting access to another device's topics. Fase 1B.2
  hardens this further with the ``iot:Connection.Thing.IsAttached: true``
  condition on every statement, rejecting a certificate that was never
  attached to any Thing (see ``_build_device_policy_document`` below).

Topic names and the policy's permission boundaries mirror
``interBridge/docs/communication-protocol.md`` (the authoritative protocol
spec) exactly. AWS IoT Basic Ingest rule *names* are reserved and
centralized (``infrastructure/config/iot.py``) so the policy can
pre-authorize the exact publish paths the protocol defines, but the rules
themselves (``AWS::IoT::TopicRule``) are **not** created here -- see
``docs/phases.md`` (Fase 1D).

This phase intentionally does **not** create:

- Any X.509 certificate, private key, CSR, or certificate attachment.
- Any individual ``AWS::IoT::Thing`` (no device is registered yet).
- Any Fleet Provisioning template.
- Any ``AWS::IoT::TopicRule`` (Basic Ingest rules) -- names only, reserved.

Device identity is provisioned out-of-band per the protocol spec and must
never be generated from, or committed to, this repository.

Depends on (future): ``DataStack`` (for table ARNs used once Basic Ingest
rules are created in Fase 1D). Should not depend on ``ApiStack`` to avoid a
circular dependency between "commands sent by the API" and "events
ingested by IoT".
"""

from __future__ import annotations

from typing import Any

from aws_cdk import ArnFormat, CfnOutput, Stack, Tags
from aws_cdk import aws_iam as iam
from aws_cdk import aws_iot as iot

from constructs import Construct
from infrastructure.config.environment import EnvironmentConfig
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


class IoTStack(Stack):
    """Owns AWS IoT Core resources (Thing Type, Thing Group, device policy) for InterBridge."""

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
        self.names: IotNames = iot_names(config)

        for key, value in config.standard_tags.items():
            Tags.of(self).add(key, value)
        for key, value in config.component_tag("iot").items():
            Tags.of(self).add(key, value)

        # AWS::IoT::ThingType, AWS::IoT::ThingGroup and AWS::IoT::Policy all
        # support the CloudFormation `Tags` property (verified against the
        # CloudFormation resource reference), so the Tags aspect above
        # applies to all three like any other taggable resource -- see the
        # test asserting this so a future CFN change that silently
        # drops/ignores the property doesn't go unnoticed.
        self.thing_type = iot.CfnThingType(
            self,
            "DeviceThingType",
            thing_type_name=self.names.thing_type_name,
            thing_type_properties=iot.CfnThingType.ThingTypePropertiesProperty(
                thing_type_description=self.names.thing_type_description,
            ),
        )

        self.thing_group = iot.CfnThingGroup(
            self,
            "DeviceThingGroup",
            thing_group_name=self.names.thing_group_name,
        )

        self.device_policy = iot.CfnPolicy(
            self,
            "DevicePolicy",
            policy_name=self.names.device_policy_name,
            policy_document=self._build_device_policy_document().to_json(),
        )

        CfnOutput(
            self,
            "ThingTypeNameOutput",
            value=self.names.thing_type_name,
            description="Name of the shared AWS IoT Thing Type for InterBridge devices.",
        )
        CfnOutput(
            self,
            "ThingGroupNameOutput",
            value=self.names.thing_group_name,
            description="Name of the AWS IoT Thing Group for dev devices.",
        )
        CfnOutput(
            self,
            "DevicePolicyNameOutput",
            value=self.names.device_policy_name,
            description="Name of the shared, least-privilege AWS IoT device policy.",
        )
        CfnOutput(
            self,
            "RegionOutput",
            value=self.region,
            description="AWS region this stack is synthesized for.",
        )
        CfnOutput(
            self,
            "EnvironmentOutput",
            value=self.config.environment,
            description="Deployment environment (e.g. dev).",
        )

    def _iot_arn(self, resource: str, resource_name: str) -> str:
        """Build an AWS IoT ARN using CloudFormation pseudo-parameters.

        Never uses a real Account ID: ``self.format_arn`` falls back to the
        ``AWS::AccountId``/``AWS::Region`` pseudo-parameters whenever the
        stack's account/region are tokens (as they are here -- see
        ``app.py``, which never hardcodes the account).
        """
        return self.format_arn(
            service="iot",
            resource=resource,
            resource_name=resource_name,
            arn_format=ArnFormat.SLASH_RESOURCE_NAME,
        )

    def _build_device_policy_document(self) -> iam.PolicyDocument:
        """Least-privilege IoT policy shared by every device certificate.

        Every resource ARN is scoped through the AWS IoT policy variable
        ``${iot:Connection.Thing.ThingName}`` (never a literal device id),
        which AWS IoT resolves per-connection from the Thing attached to
        the certificate in use -- see ``infrastructure/config/iot.py``.
        This is a plain Python f-string substitution, not a CloudFormation
        ``Fn::Sub``, so the literal ``${iot:Connection.Thing.ThingName}``
        string reaches the template unresolved and unmangled, exactly as
        AWS IoT expects it.

        Fase 1B.2 hardening: every statement also carries the
        ``iot:Connection.Thing.IsAttached: true`` condition (see
        ``THING_ATTACHED_CONDITION`` in ``infrastructure/config/iot.py``).
        AWS's own documentation only ever shows this condition on the
        ``iot:Connect`` statement (the connection's Thing association is
        fixed for the life of that connection, so it cannot change between
        ``Connect`` and a later ``Publish``/``Subscribe``/``Receive`` on the
        same connection). It is repeated here on the other three statements
        anyway, as defense-in-depth: ``iot:Connection.Thing.IsAttached`` is
        a ``Connection.*`` policy variable exactly like
        ``iot:Connection.Thing.ThingName`` (already used in every one of
        these statements' resources), and AWS IoT Core documents no
        restriction against using it outside ``Connect``.
        """
        names = self.names

        connect_resource = self._iot_arn("client", THING_NAME_POLICY_VARIABLE)
        commands_topicfilter_resource = self._iot_arn("topicfilter", commands_topic())
        commands_topic_resource = self._iot_arn("topic", commands_topic())

        publish_resources = [
            self._iot_arn("topic", basic_ingest_topic(names.ingest_rule_name, events_topic())),
            self._iot_arn("topic", basic_ingest_topic(names.ingest_rule_name, health_topic())),
            self._iot_arn("topic", basic_ingest_topic(names.response_rule_name, responses_topic())),
        ]

        return iam.PolicyDocument(
            statements=[
                # 1. Connect only as the device's own Thing name, and only
                # if that Thing is actually attached to the certificate
                # (AWS-documented ConnectAsOwnThing + IsAttached pattern).
                # Scoping `iot:Connect` to
                # `client/${iot:Connection.Thing.ThingName}` forces the
                # MQTT Client ID to equal the Thing attached to the
                # certificate; IsAttached additionally rejects a
                # certificate that was never attached to any Thing at all
                # (where the variable would otherwise resolve empty).
                iam.PolicyStatement(
                    sid="ConnectAsOwnThing",
                    effect=iam.Effect.ALLOW,
                    actions=["iot:Connect"],
                    resources=[connect_resource],
                    conditions=THING_ATTACHED_CONDITION,
                ),
                # 2. Subscribe only to the device's own commands topic filter.
                iam.PolicyStatement(
                    sid="SubscribeToOwnCommands",
                    effect=iam.Effect.ALLOW,
                    actions=["iot:Subscribe"],
                    resources=[commands_topicfilter_resource],
                    conditions=THING_ATTACHED_CONDITION,
                ),
                # 3. Receive messages only on the device's own commands topic.
                iam.PolicyStatement(
                    sid="ReceiveOwnCommands",
                    effect=iam.Effect.ALLOW,
                    actions=["iot:Receive"],
                    resources=[commands_topic_resource],
                    conditions=THING_ATTACHED_CONDITION,
                ),
                # 4. Publish only to the device's own events/health/response
                # Basic Ingest paths (protocol v1). The underlying
                # AWS::IoT::TopicRule resources are not created until Fase
                # 1D; granting the permission now does not require the
                # rule to exist.
                iam.PolicyStatement(
                    sid="PublishOwnEventsHealthAndResponses",
                    effect=iam.Effect.ALLOW,
                    actions=["iot:Publish"],
                    resources=publish_resources,
                    conditions=THING_ATTACHED_CONDITION,
                ),
            ]
        )
