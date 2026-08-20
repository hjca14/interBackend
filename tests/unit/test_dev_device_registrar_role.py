from __future__ import annotations

import aws_cdk as cdk
from aws_cdk.assertions import Template

from infrastructure.config.environment import EnvironmentConfig
from infrastructure.stacks import ApiStack, DataStack


def _template() -> dict[str, object]:
    app = cdk.App()
    config = EnvironmentConfig(account="111122223333", region="sa-east-1")
    data = DataStack(app, "InterBridge-Dev-DataStack", config=config)
    api = ApiStack(app, "InterBridge-Dev-ApiStack", config=config, data_stack=data)
    return Template.from_stack(api).to_json()


def test_registrar_trust_is_same_account_and_requires_mfa() -> None:
    resources = _template()["Resources"]
    roles = [value for value in resources.values() if value["Type"] == "AWS::IAM::Role"]
    registrar = next(
        role
        for role in roles
        if role["Properties"].get("RoleName") == "interbridge-dev-device-registrar-role"
    )
    trust = registrar["Properties"]["AssumeRolePolicyDocument"]["Statement"]
    assert len(trust) == 1
    assert trust[0]["Action"] == "sts:AssumeRole"
    assert trust[0]["Condition"] == {"Bool": {"aws:MultiFactorAuthPresent": "true"}}
    principal = str(trust[0]["Principal"])
    assert "AWS::AccountId" in principal and ":root" in principal
    assert "PrincipalOrgID" not in principal and "Service" not in principal


def test_registrar_policy_is_the_minimum_allowlist() -> None:
    resources = _template()["Resources"]
    policies = [value for value in resources.values() if value["Type"] == "AWS::IAM::Policy"]
    policy = next(
        value for value in policies if "DevDeviceRegistrarRole" in str(value["Properties"]["Roles"])
    )
    statements = policy["Properties"]["PolicyDocument"]["Statement"]
    actions = {
        action
        for statement in statements
        for action in (
            statement["Action"] if isinstance(statement["Action"], list) else [statement["Action"]]
        )
    }
    assert actions == {
        "sts:GetCallerIdentity",
        "cloudformation:DescribeStacks",
        "cognito-idp:ListUsers",
        "iot:DescribeThing",
        "iot:ListThingGroupsForThing",
        "iot:ListThingPrincipals",
        "iot:DescribeCertificate",
        "iot:ListAttachedPolicies",
        "dynamodb:GetItem",
        "dynamodb:PutItem",
    }
    assert not actions.intersection(
        {
            "dynamodb:TransactWriteItems",
            "dynamodb:UpdateItem",
            "dynamodb:DeleteItem",
            "dynamodb:BatchWriteItem",
            "dynamodb:Scan",
            "dynamodb:ConditionCheckItem",
        }
    )
    assert "iot:Publish" not in actions
    assert all(statement["Effect"] == "Allow" for statement in statements)

    wildcard = next(
        statement for statement in statements if statement["Action"] == "sts:GetCallerIdentity"
    )
    assert wildcard["Resource"] == "*"  # STS does not support resource-level permissions.
    rendered = str(statements)
    assert "InterBridge-Dev-DataStack/*" in rendered
    assert "InterBridge-Dev-ApiStack/*" in rendered
    dynamodb = {
        statement["Action"]: statement
        for statement in statements
        if isinstance(statement["Action"], str)
        and statement["Action"] in {"dynamodb:GetItem", "dynamodb:PutItem"}
    }
    assert set(dynamodb) == {"dynamodb:GetItem", "dynamodb:PutItem"}
    for statement in dynamodb.values():
        assert len(statement["Resource"]) == 2
        assert "DevicesTable" in str(statement["Resource"])
        assert "DeviceMembershipsTable" in str(statement["Resource"])
    assert "Condition" not in dynamodb["dynamodb:GetItem"]
    assert dynamodb["dynamodb:PutItem"]["Condition"] == {
        "ForAnyValue:StringEquals": {"dynamodb:EnclosingOperation": ["TransactWriteItems"]}
    }


def test_registrar_arn_is_a_non_sensitive_output_and_change_is_additive() -> None:
    template = _template()
    outputs = template["Outputs"]
    assert "DevDeviceRegistrarRoleArn" in outputs
    resources = template["Resources"]
    assert sum(value["Type"] == "AWS::Cognito::UserPool" for value in resources.values()) == 1
    assert not any(value["Type"] == "AWS::DynamoDB::Table" for value in resources.values())
