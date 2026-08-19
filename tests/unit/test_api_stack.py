from __future__ import annotations

import aws_cdk as cdk
from aws_cdk.assertions import Match, Template

from infrastructure.config.environment import EnvironmentConfig
from infrastructure.stacks import ApiStack, DataStack


def template() -> Template:
    app = cdk.App()
    config = EnvironmentConfig()
    data = DataStack(app, "Data", config=config)
    return Template.from_stack(ApiStack(app, "Api", config=config, data_stack=data))


def test_cognito_security_and_no_identity_pool() -> None:
    result = template()
    result.resource_count_is("AWS::Cognito::UserPool", 1)
    result.resource_count_is("AWS::Cognito::UserPoolClient", 1)
    result.resource_count_is("AWS::Cognito::IdentityPool", 0)
    result.has_resource_properties(
        "AWS::Cognito::UserPool",
        {
            "MfaConfiguration": "OFF",
            "UsernameAttributes": ["email"],
            "DeletionProtection": "ACTIVE",
        },
    )
    result.has_resource_properties(
        "AWS::Cognito::UserPoolClient",
        {
            "GenerateSecret": False,
            "PreventUserExistenceErrors": "ENABLED",
            "EnableTokenRevocation": True,
        },
    )


def test_exactly_three_protected_get_routes() -> None:
    result = template()
    result.resource_count_is("AWS::ApiGatewayV2::Route", 3)
    for route in (
        "GET /v1/devices",
        "GET /v1/devices/{device_id}",
        "GET /v1/devices/{device_id}/status",
    ):
        result.has_resource_properties(
            "AWS::ApiGatewayV2::Route",
            {"RouteKey": route, "AuthorizationType": "JWT", "AuthorizerId": Match.any_value()},
        )
    result.resource_count_is("AWS::Lambda::Function", 3)


def test_public_lambda_iam_is_structurally_minimal() -> None:
    resources = template().to_json()["Resources"]
    policies = [value for value in resources.values() if value["Type"] == "AWS::IAM::Policy"]
    statements = [
        statement
        for policy in policies
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]
    ]
    actions = {
        action
        for statement in statements
        for action in (
            [statement["Action"]] if isinstance(statement["Action"], str) else statement["Action"]
        )
    }
    assert not actions.intersection(
        {
            "dynamodb:PutItem",
            "dynamodb:UpdateItem",
            "dynamodb:DeleteItem",
            "dynamodb:TransactWriteItems",
            "iot:Publish",
        }
    )
    assert all(statement["Resource"] != "*" for statement in statements)
    by_action = {
        statement["Action"]: statement["Resource"]
        for statement in statements
        if isinstance(statement["Action"], str)
    }
    assert "/index/interbridge-dev-device-memberships-by-user-index" in str(
        by_action["dynamodb:Query"]
    )
    assert "DevicesTable" in str(by_action["dynamodb:BatchGetItem"])
    get_resources = [
        statement["Resource"]
        for statement in statements
        if statement["Action"] == "dynamodb:GetItem"
    ]
    joined = str(get_resources)
    assert len(get_resources) == 2
    assert all(
        name in joined for name in ("DeviceMembershipsTable", "DevicesTable", "TelemetryTable")
    )
    assert {action for action in actions if action.startswith("kms:")} == {
        "kms:Encrypt",
        "kms:Decrypt",
    }


def test_client_id_and_cursor_key_are_delivered_only_where_required() -> None:
    functions = [
        value["Properties"]
        for value in template().to_json()["Resources"].values()
        if value["Type"] == "AWS::Lambda::Function"
    ]
    assert len(functions) == 3
    for function in functions:
        client = function["Environment"]["Variables"]["EXPECTED_APP_CLIENT_ID"]
        assert set(client) == {"Ref"} and "UserPoolMobileClient" in client["Ref"]
    list_function = next(
        function for function in functions if function["Handler"].endswith("list_devices")
    )
    assert "CURSOR_KEY_ARN" in list_function["Environment"]["Variables"]
    assert all(
        "CURSOR_KEY_ARN" not in function["Environment"]["Variables"]
        for function in functions
        if function is not list_function
    )
