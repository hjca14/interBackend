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
            "Policies": {
                "PasswordPolicy": {
                    "MinimumLength": 8,
                    "RequireLowercase": True,
                    "RequireUppercase": True,
                    "RequireNumbers": True,
                    "RequireSymbols": False,
                }
            },
        },
    )
    result.has_resource_properties("AWS::KMS::Key", {"EnableKeyRotation": False})
    result.has_resource_properties(
        "AWS::Cognito::UserPoolClient",
        {
            "GenerateSecret": False,
            "PreventUserExistenceErrors": "ENABLED",
            "EnableTokenRevocation": True,
        },
    )


def test_user_pool_policy_and_email_are_in_place_updates() -> None:
    """Keep the existing logical resource; these mutable properties must not create a new pool."""
    resources = template().to_json()["Resources"]
    pools = {
        logical_id: resource
        for logical_id, resource in resources.items()
        if resource["Type"] == "AWS::Cognito::UserPool"
    }
    assert set(pools) == {"UserPool6BA7E5F2"}
    pool = pools["UserPool6BA7E5F2"]
    assert pool["UpdateReplacePolicy"] == "Retain"
    assert pool["DeletionPolicy"] == "Retain"
    verification = pool["Properties"]["VerificationMessageTemplate"]
    assert verification["DefaultEmailOption"] == "CONFIRM_WITH_CODE"
    assert verification["EmailSubject"] == (
        "InterBridge | Código de confirmação / Confirmation code"
    )
    message = verification["EmailMessage"]
    assert message.count("{####}") == 2
    assert all(text in message for text in ("InterBridge", "não solicitou", "did not request"))


def test_exactly_five_protected_routes() -> None:
    result = template()
    result.resource_count_is("AWS::ApiGatewayV2::Route", 5)
    for route in (
        "GET /v1/devices",
        "GET /v1/devices/{device_id}",
        "GET /v1/devices/{device_id}/status",
        "POST /v1/devices/{device_id}/commands",
        "GET /v1/devices/{device_id}/commands/{command_id}",
    ):
        result.has_resource_properties(
            "AWS::ApiGatewayV2::Route",
            {"RouteKey": route, "AuthorizationType": "JWT", "AuthorizerId": Match.any_value()},
        )
    result.resource_count_is("AWS::Lambda::Function", 5)


def test_public_lambda_iam_is_structurally_minimal() -> None:
    resources = template().to_json()["Resources"]
    policies = [
        value
        for value in resources.values()
        if value["Type"] == "AWS::IAM::Policy"
        and "DevDeviceRegistrarRole" not in value["Properties"]["PolicyName"]
    ]
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
    assert not actions.intersection({"dynamodb:DeleteItem", "dynamodb:TransactWriteItems"})
    wildcard = [statement for statement in statements if statement["Resource"] == "*"]
    assert len(wildcard) == 1 and wildcard[0]["Action"] == "iot:DescribeEndpoint"
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
    assert len(get_resources) == 4
    assert all(
        name in joined for name in ("DeviceMembershipsTable", "DevicesTable", "TelemetryTable")
    )
    assert {action for action in actions if action.startswith("kms:")} == {
        "kms:Encrypt",
        "kms:Decrypt",
    }


def test_only_creator_can_publish_to_exact_command_topic_shape() -> None:
    resources = template().to_json()["Resources"]
    publish = []
    for logical_id, policy in resources.items():
        if policy["Type"] != "AWS::IAM::Policy":
            continue
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
            actions = (
                statement["Action"]
                if isinstance(statement["Action"], list)
                else [statement["Action"]]
            )
            if "iot:Publish" in actions:
                publish.append((logical_id, statement))
    assert len(publish) == 1
    logical_id, statement = publish[0]
    assert "CreateCommand" in logical_id
    assert "topic/interbridge/ib-*/commands" in str(statement["Resource"])
    assert "GetCommand" not in logical_id


def test_client_id_and_cursor_key_are_delivered_only_where_required() -> None:
    functions = [
        value["Properties"]
        for value in template().to_json()["Resources"].values()
        if value["Type"] == "AWS::Lambda::Function"
    ]
    assert len(functions) == 5
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
