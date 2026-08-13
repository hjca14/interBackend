from __future__ import annotations

import json

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template

from infrastructure.config.environment import EnvironmentConfig
from infrastructure.config.naming import stack_id
from infrastructure.stacks import ApiStack, DataStack, IoTStack, ObservabilityStack

# Resource types that must never appear in this phase: they either cost
# money by default (VPC/NAT Gateway) or represent per-device secrets that
# must not be generated from stack code (IoT certificates/Things).
FORBIDDEN_RESOURCE_TYPES = (
    "AWS::EC2::VPC",
    "AWS::EC2::NatGateway",
    "AWS::IoT::Certificate",
    "AWS::IoT::Thing",
    "AWS::RDS::DBInstance",
    "AWS::OpenSearchService::Domain",
    "AWS::EKS::Cluster",
    "AWS::ECS::Cluster",
)

STACK_CASES = [
    (DataStack, "Data", "database"),
    (IoTStack, "IoT", "iot"),
    (ApiStack, "Api", "api"),
    (ObservabilityStack, "Observability", "monitoring"),
]


@pytest.mark.parametrize("stack_cls, name, component", STACK_CASES)
def test_stack_synthesizes(stack_cls: type, name: str, component: str) -> None:
    app = cdk.App()
    config = EnvironmentConfig()
    stack = stack_cls(app, stack_id(config, name), config=config)

    template = Template.from_stack(stack)
    body = template.to_json()

    # In this phase every stack is intentionally empty (see each stack's
    # module docstring): synthesis must succeed and produce no resources.
    assert body.get("Resources", {}) == {}


@pytest.mark.parametrize("stack_cls, name, component", STACK_CASES)
def test_stack_has_required_standard_tags(stack_cls: type, name: str, component: str) -> None:
    app = cdk.App()
    config = EnvironmentConfig()
    stack = stack_cls(app, stack_id(config, name), config=config)

    Template.from_stack(stack)  # forces synth so tag Aspects are resolved

    tag_values = stack.tags.tag_values()
    assert tag_values["Project"] == "InterBridge"
    assert tag_values["Environment"] == "dev"
    assert tag_values["ManagedBy"] == "AWS-CDK"
    assert tag_values["Repository"] == "interBackend"
    assert tag_values["Component"] == component


@pytest.mark.parametrize("stack_cls, name, component", STACK_CASES)
def test_stack_contains_no_forbidden_resource_types(
    stack_cls: type, name: str, component: str
) -> None:
    app = cdk.App()
    config = EnvironmentConfig()
    stack = stack_cls(app, stack_id(config, name), config=config)

    template = Template.from_stack(stack)
    resources = template.to_json().get("Resources", {})
    resource_types = {res["Type"] for res in resources.values()}

    for forbidden in FORBIDDEN_RESOURCE_TYPES:
        assert forbidden not in resource_types


@pytest.mark.parametrize("stack_cls, name, component", STACK_CASES)
def test_stack_template_has_no_high_confidence_secrets(
    stack_cls: type, name: str, component: str
) -> None:
    app = cdk.App()
    config = EnvironmentConfig()
    stack = stack_cls(app, stack_id(config, name), config=config)

    template = Template.from_stack(stack)
    rendered = json.dumps(template.to_json())

    assert "-----BEGIN" not in rendered
    assert "AKIA" not in rendered
    assert "claim_code" not in rendered.lower()
    assert "claim-code" not in rendered.lower()
