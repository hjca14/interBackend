"""Semantic tests for IoTStack (Fase 1B).

Deliberately avoids snapshotting the whole template: assertions target the
specific resources, names and IoT policy statements this stack is supposed
to produce, so a future accidental change (e.g. widening the shared device
policy) breaks a precise assertion instead of a brittle full-template diff.
"""

from __future__ import annotations

import json
import re
from typing import Any

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template

from infrastructure.config.environment import EnvironmentConfig
from infrastructure.config.iot import (
    THING_ATTACHED_CONDITION,
    THING_NAME_POLICY_VARIABLE,
    iot_names,
)
from infrastructure.stacks.iot_stack import IoTStack


def _synth() -> tuple[IoTStack, Template, dict[str, Any]]:
    app = cdk.App()
    config = EnvironmentConfig()
    stack = IoTStack(app, "TestIoTStack", config=config)
    template = Template.from_stack(stack)
    body = template.to_json()
    return stack, template, body


def _resolve(value: Any) -> str:
    """Flatten a CFN intrinsic (Fn::Join/Ref) into a literal string.

    Turns e.g. ``{"Fn::Join": ["", ["arn:", {"Ref": "AWS::Partition"}, ...]]}``
    into ``"arn:${AWS::Partition}:..."`` so tests can assert on the exact
    resource string without depending on how CDK happens to encode it.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and "Fn::Join" in value:
        sep, parts = value["Fn::Join"]
        return sep.join(_resolve(p) for p in parts)
    if isinstance(value, dict) and "Ref" in value:
        return f"${{{value['Ref']}}}"
    raise TypeError(f"Don't know how to resolve {value!r} in a test assertion")


def _policy_statements(body: dict[str, Any]) -> list[dict[str, Any]]:
    policies = {k: v for k, v in body["Resources"].items() if v["Type"] == "AWS::IoT::Policy"}
    assert len(policies) == 1, "expected exactly one AWS::IoT::Policy"
    (policy,) = policies.values()
    return policy["Properties"]["PolicyDocument"]["Statement"]


def _statement_by_sid(statements: list[dict[str, Any]], sid: str) -> dict[str, Any]:
    matches = [s for s in statements if s.get("Sid") == sid]
    assert len(matches) == 1, f"expected exactly one statement with Sid={sid!r}"
    return matches[0]


# ---------------------------------------------------------------------------
# Resource inventory
# ---------------------------------------------------------------------------


def test_exactly_one_thing_type() -> None:
    _, template, _ = _synth()
    template.resource_count_is("AWS::IoT::ThingType", 1)


def test_exactly_one_thing_group() -> None:
    _, template, _ = _synth()
    template.resource_count_is("AWS::IoT::ThingGroup", 1)


def test_exactly_one_iot_policy() -> None:
    _, template, _ = _synth()
    template.resource_count_is("AWS::IoT::Policy", 1)


@pytest.mark.parametrize(
    "forbidden_type",
    [
        "AWS::IoT::Thing",
        "AWS::IoT::Certificate",
        "AWS::IoT::PolicyPrincipalAttachment",
        "AWS::IoT::ThingPrincipalAttachment",
        "AWS::IoT::ProvisioningTemplate",
        "AWS::IoT::TopicRule",
        "AWS::Lambda::Function",
        "AWS::DynamoDB::Table",
        "AWS::ApiGateway::RestApi",
        "AWS::ApiGatewayV2::Api",
        "AWS::Cognito::UserPool",
        "AWS::EC2::VPC",
        "AWS::EC2::NatGateway",
    ],
)
def test_forbidden_resource_type_absent(forbidden_type: str) -> None:
    _, template, _ = _synth()
    template.resource_count_is(forbidden_type, 0)


def test_only_three_resources_in_the_whole_stack() -> None:
    # Guards against a future change silently adding an extra resource
    # (e.g. a Thing or a Rule) without updating the tests above.
    _, _, body = _synth()
    resource_types = sorted(res["Type"] for res in body["Resources"].values())
    assert resource_types == ["AWS::IoT::Policy", "AWS::IoT::ThingGroup", "AWS::IoT::ThingType"]


# ---------------------------------------------------------------------------
# Deterministic names / description
# ---------------------------------------------------------------------------


def test_thing_type_name_and_description() -> None:
    _, template, _ = _synth()
    template.has_resource_properties(
        "AWS::IoT::ThingType",
        {
            "ThingTypeName": "interbridge-dev-device",
            "ThingTypeProperties": {
                "ThingTypeDescription": "InterBridge device type for the dev environment"
            },
        },
    )


def test_thing_group_name() -> None:
    _, template, _ = _synth()
    template.has_resource_properties(
        "AWS::IoT::ThingGroup", {"ThingGroupName": "interbridge-dev-devices"}
    )


def test_device_policy_name() -> None:
    _, template, _ = _synth()
    template.has_resource_properties(
        "AWS::IoT::Policy", {"PolicyName": "interbridge-dev-device-policy"}
    )


def test_names_match_config_iot_names_helper() -> None:
    config = EnvironmentConfig()
    names = iot_names(config)
    _, template, _ = _synth()

    template.has_resource_properties(
        "AWS::IoT::ThingType", {"ThingTypeName": names.thing_type_name}
    )
    template.has_resource_properties(
        "AWS::IoT::ThingGroup", {"ThingGroupName": names.thing_group_name}
    )
    template.has_resource_properties("AWS::IoT::Policy", {"PolicyName": names.device_policy_name})


# ---------------------------------------------------------------------------
# Tags (applied where CloudFormation actually supports them)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "resource_type", ["AWS::IoT::ThingType", "AWS::IoT::ThingGroup", "AWS::IoT::Policy"]
)
def test_standard_and_component_tags_applied(resource_type: str) -> None:
    _, _, body = _synth()
    (resource,) = [r for r in body["Resources"].values() if r["Type"] == resource_type]
    tag_dict = {t["Key"]: t["Value"] for t in resource["Properties"]["Tags"]}

    assert tag_dict == {
        "Project": "InterBridge",
        "Environment": "dev",
        "ManagedBy": "AWS-CDK",
        "Repository": "interBackend",
        "Component": "iot",
    }


# ---------------------------------------------------------------------------
# IoT policy: exact, minimal permission set
# ---------------------------------------------------------------------------


def test_policy_has_exactly_four_statements() -> None:
    _, _, body = _synth()
    statements = _policy_statements(body)
    sids = sorted(s["Sid"] for s in statements)
    assert sids == [
        "ConnectAsOwnThing",
        "PublishOwnEventsHealthAndResponses",
        "ReceiveOwnCommands",
        "SubscribeToOwnCommands",
    ]


def test_connect_is_scoped_to_own_thing_name_via_client_resource() -> None:
    _, _, body = _synth()
    statement = _statement_by_sid(_policy_statements(body), "ConnectAsOwnThing")

    assert statement["Effect"] == "Allow"
    assert statement["Action"] == "iot:Connect"
    resource = _resolve(statement["Resource"])
    assert resource == (
        f"arn:${{AWS::Partition}}:iot:${{AWS::Region}}:${{AWS::AccountId}}"
        f":client/{THING_NAME_POLICY_VARIABLE}"
    )
    # Fase 1B.2 hardening: the connecting certificate must actually be
    # attached to the Thing it claims to be, not just share its name.
    assert statement["Condition"] == THING_ATTACHED_CONDITION


def test_connect_client_id_equals_thing_name_in_non_exclusive_model() -> None:
    # Current model: non-exclusive thing association, so AWS IoT derives
    # ${iot:Connection.Thing.ThingName} from the MQTT Client ID presented
    # at connect time -- the firmware must always use its device_id as
    # both the Thing name AND the Client ID (see docs/adr/0001 and
    # interBridge/docs/communication-protocol.md). This remains true even
    # after a future move to exclusive thing association (Fase 1D+).
    _, _, body = _synth()
    statement = _statement_by_sid(_policy_statements(body), "ConnectAsOwnThing")
    resource = _resolve(statement["Resource"])
    assert resource.endswith(f"client/{THING_NAME_POLICY_VARIABLE}")


def test_subscribe_is_scoped_to_own_commands_topicfilter() -> None:
    _, _, body = _synth()
    statement = _statement_by_sid(_policy_statements(body), "SubscribeToOwnCommands")

    assert statement["Effect"] == "Allow"
    assert statement["Action"] == "iot:Subscribe"
    resource = _resolve(statement["Resource"])
    assert resource == (
        f"arn:${{AWS::Partition}}:iot:${{AWS::Region}}:${{AWS::AccountId}}"
        f":topicfilter/interbridge/{THING_NAME_POLICY_VARIABLE}/commands"
    )
    # client/, topic/ and topicfilter/ are distinct AWS IoT ARN resource
    # types with different authorization semantics -- Subscribe must use
    # topicfilter/, never topic/ or client/.
    assert ":topicfilter/" in resource
    assert not resource.startswith(
        "arn:${AWS::Partition}:iot:${AWS::Region}:${AWS::AccountId}:topic/"
    )
    assert statement["Condition"] == THING_ATTACHED_CONDITION


def test_receive_is_scoped_to_own_commands_topic() -> None:
    _, _, body = _synth()
    statement = _statement_by_sid(_policy_statements(body), "ReceiveOwnCommands")

    assert statement["Effect"] == "Allow"
    assert statement["Action"] == "iot:Receive"
    resource = _resolve(statement["Resource"])
    assert resource == (
        f"arn:${{AWS::Partition}}:iot:${{AWS::Region}}:${{AWS::AccountId}}"
        f":topic/interbridge/{THING_NAME_POLICY_VARIABLE}/commands"
    )
    # Receive authorizes delivery on a concrete topic/, not a topicfilter/.
    assert ":topicfilter/" not in resource
    assert statement["Condition"] == THING_ATTACHED_CONDITION


def test_publish_is_scoped_to_exactly_three_basic_ingest_paths() -> None:
    _, _, body = _synth()
    statement = _statement_by_sid(_policy_statements(body), "PublishOwnEventsHealthAndResponses")

    assert statement["Effect"] == "Allow"
    assert statement["Action"] == "iot:Publish"
    resources = sorted(_resolve(r) for r in statement["Resource"])

    prefix = "arn:${AWS::Partition}:iot:${AWS::Region}:${AWS::AccountId}:topic/"
    expected = sorted(
        [
            f"{prefix}$aws/rules/interbridge_dev_ingest_rule/interbridge/"
            f"{THING_NAME_POLICY_VARIABLE}/events",
            f"{prefix}$aws/rules/interbridge_dev_ingest_rule/interbridge/"
            f"{THING_NAME_POLICY_VARIABLE}/health",
            f"{prefix}$aws/rules/interbridge_dev_response_rule/interbridge/"
            f"{THING_NAME_POLICY_VARIABLE}/responses",
        ]
    )
    assert resources == expected
    # Every publish path goes through the $aws/rules/ Basic Ingest prefix --
    # the device never publishes to the plain interbridge/... topic.
    for resource in resources:
        assert "$aws/rules/" in resource
    assert statement["Condition"] == THING_ATTACHED_CONDITION


def test_every_statement_requires_thing_attached() -> None:
    # Explicit, single-assertion guard for the Fase 1B.2 hardening: every
    # one of the four statements must carry the IsAttached condition with
    # the exact AWS-documented operator ("Bool") and value ("true") --
    # this is what stands in, at the policy-document level, for "reject a
    # connection whose certificate is not attached to the claimed Thing."
    _, _, body = _synth()
    statements = _policy_statements(body)
    assert len(statements) == 4
    for statement in statements:
        assert statement["Condition"] == {"Bool": {"iot:Connection.Thing.IsAttached": "true"}}


def test_thing_name_policy_variable_preserved_literally() -> None:
    _, _, body = _synth()
    raw = json.dumps(body)
    assert THING_NAME_POLICY_VARIABLE in raw
    assert "${iot:Connection.Thing.ThingName}" in raw


# ---------------------------------------------------------------------------
# Negative tests: the policy must never be broad
# ---------------------------------------------------------------------------


def test_policy_never_grants_iot_wildcard_action() -> None:
    _, _, body = _synth()
    for statement in _policy_statements(body):
        actions = statement["Action"]
        actions = actions if isinstance(actions, list) else [actions]
        assert "iot:*" not in actions
        assert all(not action.endswith(":*") for action in actions)


def test_policy_never_grants_wildcard_resource() -> None:
    _, _, body = _synth()
    for statement in _policy_statements(body):
        resources = statement["Resource"]
        resources = resources if isinstance(resources, list) else [resources]
        for resource in resources:
            resolved = _resolve(resource)
            assert resolved != "*"
            assert not resolved.endswith("*")


def test_policy_only_allows_expected_actions() -> None:
    _, _, body = _synth()
    all_actions: set[str] = set()
    for statement in _policy_statements(body):
        actions = statement["Action"]
        all_actions.update(actions if isinstance(actions, list) else [actions])

    assert all_actions == {"iot:Connect", "iot:Subscribe", "iot:Receive", "iot:Publish"}


def test_policy_does_not_allow_administrative_or_thing_management_actions() -> None:
    _, _, body = _synth()
    raw = json.dumps(_policy_statements(body))

    forbidden_action_substrings = [
        "iot:CreateThing",
        "iot:DeleteThing",
        "iot:UpdateThing",
        "iot:CreateCertificate",
        "iot:AttachPolicy",
        "iot:AttachThingPrincipal",
        "iot:CreatePolicy",
        "iot:DeletePolicy",
        "iot:UpdatePolicy",
    ]
    for action in forbidden_action_substrings:
        assert action not in raw


def test_policy_statements_reference_only_own_thing_no_wildcard_client_ids() -> None:
    # Every resource in every statement must be scoped through the
    # per-connection policy variable, never a "*" or a different
    # hardcoded/wildcard client id.
    _, _, body = _synth()
    for statement in _policy_statements(body):
        resources = statement["Resource"]
        resources = resources if isinstance(resources, list) else [resources]
        for resource in resources:
            resolved = _resolve(resource)
            if ":client/" in resolved or ":topic" in resolved:
                assert THING_NAME_POLICY_VARIABLE in resolved


# ---------------------------------------------------------------------------
# Outputs: safe, non-secret values only
# ---------------------------------------------------------------------------


def test_outputs_expose_only_non_secret_values() -> None:
    _, _, body = _synth()
    outputs = body.get("Outputs", {})
    assert set(outputs) == {
        "ThingTypeNameOutput",
        "ThingGroupNameOutput",
        "DevicePolicyNameOutput",
        "RegionOutput",
        "EnvironmentOutput",
    }
    assert outputs["ThingTypeNameOutput"]["Value"] == "interbridge-dev-device"
    assert outputs["ThingGroupNameOutput"]["Value"] == "interbridge-dev-devices"
    assert outputs["DevicePolicyNameOutput"]["Value"] == "interbridge-dev-device-policy"
    assert outputs["EnvironmentOutput"]["Value"] == "dev"
    # Region is a CFN pseudo-parameter reference, never a literal value.
    assert outputs["RegionOutput"]["Value"] == {"Ref": "AWS::Region"}


def test_no_account_id_or_secret_markers_in_template() -> None:
    _, _, body = _synth()
    raw = json.dumps(body)

    assert "-----BEGIN" not in raw
    assert "AKIA" not in raw
    assert "claim_code" not in raw.lower()
    assert "claim-code" not in raw.lower()
    # No literal 12-digit AWS Account ID anywhere -- only the
    # AWS::AccountId pseudo-parameter reference is allowed.
    assert not re.search(r"\b\d{12}\b", raw)
    # No real AWS IoT data-plane endpoint hostname (device connection
    # config is out of scope for this stack; nothing should reference one).
    assert ".amazonaws.com" not in raw


def test_stack_has_required_standard_tags_and_component() -> None:
    stack, template, _ = _synth()
    Template.from_stack(stack)  # ensure aspects resolved

    tag_values = stack.tags.tag_values()
    assert tag_values["Project"] == "InterBridge"
    assert tag_values["Environment"] == "dev"
    assert tag_values["ManagedBy"] == "AWS-CDK"
    assert tag_values["Repository"] == "interBackend"
    assert tag_values["Component"] == "iot"
