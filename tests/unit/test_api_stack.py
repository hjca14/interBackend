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


def test_no_write_or_iot_permissions() -> None:
    body = str(template().to_json())
    for forbidden in (
        "dynamodb:PutItem",
        "dynamodb:UpdateItem",
        "dynamodb:DeleteItem",
        "iot:Publish",
        "Resource': '*",
    ):
        assert forbidden not in body
