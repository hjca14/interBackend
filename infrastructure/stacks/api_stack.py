"""Phase 2B Cognito and authenticated, read-only HTTP API."""

from __future__ import annotations

from typing import Any

from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack, Tags
from aws_cdk import aws_apigatewayv2 as apigw
from aws_cdk import aws_apigatewayv2_authorizers as authorizers
from aws_cdk import aws_apigatewayv2_integrations as integrations
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs

from constructs import Construct
from infrastructure.config.environment import EnvironmentConfig
from infrastructure.config.naming import resource_name
from infrastructure.stacks.data_stack import DataStack


class ApiStack(Stack):
    """Owns the public HTTPS API (API Gateway + Lambda) for InterBridge."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: EnvironmentConfig,
        data_stack: DataStack,
        **kwargs: Any,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        self.config = config

        for key, value in config.standard_tags.items():
            Tags.of(self).add(key, value)
        for key, value in config.component_tag("api").items():
            Tags.of(self).add(key, value)

        pool = cognito.UserPool(
            self,
            "UserPool",
            user_pool_name=resource_name(config, "api", "users"),
            self_sign_up_enabled=True,
            sign_in_aliases=cognito.SignInAliases(email=True),
            auto_verify=cognito.AutoVerifiedAttrs(email=True),
            account_recovery=cognito.AccountRecovery.EMAIL_ONLY,
            mfa=cognito.Mfa.OFF,
            password_policy=cognito.PasswordPolicy(
                min_length=10,
                require_lowercase=True,
                require_uppercase=True,
                require_digits=True,
                require_symbols=False,
            ),
            deletion_protection=True,
            removal_policy=RemovalPolicy.RETAIN,
        )
        client = pool.add_client(
            "MobileClient",
            user_pool_client_name=resource_name(config, "api", "mobile"),
            generate_secret=False,
            prevent_user_existence_errors=True,
            auth_flows=cognito.AuthFlow(user_srp=True),
            enable_token_revocation=True,
            access_token_validity=Duration.minutes(15),
            id_token_validity=Duration.minutes(15),
            refresh_token_validity=Duration.days(7),
        )
        cursor_key = kms.Key(
            self,
            "CursorKey",
            alias=f"alias/{resource_name(config, 'api', 'cursor')}",
            enable_key_rotation=True,
            removal_policy=RemovalPolicy.DESTROY,
            pending_window=Duration.days(7),
        )
        common = {
            "runtime": lambda_.Runtime.PYTHON_3_12,
            "architecture": lambda_.Architecture.ARM_64,
            "code": lambda_.Code.from_asset("lambdas"),
            "timeout": Duration.seconds(10),
            "memory_size": 256,
        }
        env = {
            "DEVICES_TABLE": data_stack.devices_table.table_name,
            "MEMBERSHIPS_TABLE": data_stack.device_memberships_table.table_name,
            "MEMBERSHIPS_INDEX": data_stack.names.memberships_by_user_index_name,
            "TELEMETRY_TABLE": data_stack.telemetry_table.table_name,
            "EXPECTED_APP_CLIENT_ID": client.user_pool_client_id,
        }
        list_fn = lambda_.Function(
            self,
            "ListDevicesFunction",
            handler="read_api.handler.list_devices",
            environment={**env, "CURSOR_KEY_ARN": cursor_key.key_arn},
            log_group=logs.LogGroup(
                self,
                "ListDevicesLogs",
                retention=logs.RetentionDays.ONE_WEEK,
                removal_policy=RemovalPolicy.DESTROY,
            ),
            **common,
        )
        detail_fn = lambda_.Function(
            self,
            "GetDeviceFunction",
            handler="read_api.handler.get_device",
            environment=env,
            log_group=logs.LogGroup(
                self,
                "GetDeviceLogs",
                retention=logs.RetentionDays.ONE_WEEK,
                removal_policy=RemovalPolicy.DESTROY,
            ),
            **common,
        )
        status_fn = lambda_.Function(
            self,
            "GetStatusFunction",
            handler="read_api.handler.get_status",
            environment=env,
            log_group=logs.LogGroup(
                self,
                "GetStatusLogs",
                retention=logs.RetentionDays.ONE_WEEK,
                removal_policy=RemovalPolicy.DESTROY,
            ),
            **common,
        )
        list_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["dynamodb:Query"],
                resources=[
                    f"{data_stack.device_memberships_table.table_arn}/index/{data_stack.names.memberships_by_user_index_name}"
                ],
            )
        )
        list_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["dynamodb:BatchGetItem"], resources=[data_stack.devices_table.table_arn]
            )
        )
        list_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["kms:Encrypt", "kms:Decrypt"], resources=[cursor_key.key_arn]
            )
        )
        detail_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["dynamodb:GetItem"],
                resources=[
                    data_stack.device_memberships_table.table_arn,
                    data_stack.devices_table.table_arn,
                ],
            )
        )
        status_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["dynamodb:GetItem"],
                resources=[
                    data_stack.device_memberships_table.table_arn,
                    data_stack.telemetry_table.table_arn,
                ],
            )
        )
        api = apigw.HttpApi(
            self,
            "HttpApi",
            api_name=resource_name(config, "api", "http"),
            create_default_stage=True,
        )
        auth = authorizers.HttpJwtAuthorizer(
            "JwtAuthorizer", pool.user_pool_provider_url, jwt_audience=[client.user_pool_client_id]
        )
        for path, fn in (
            ("/v1/devices", list_fn),
            ("/v1/devices/{device_id}", detail_fn),
            ("/v1/devices/{device_id}/status", status_fn),
        ):
            api.add_routes(
                path=path,
                methods=[apigw.HttpMethod.GET],
                authorizer=auth,
                integration=integrations.HttpLambdaIntegration(
                    path.replace("/", "-") + "Integration",
                    fn,
                    payload_format_version=apigw.PayloadFormatVersion.VERSION_2_0,
                ),
            )
        CfnOutput(self, "ApiUrl", value=api.api_endpoint)
        CfnOutput(self, "UserPoolId", value=pool.user_pool_id)
        CfnOutput(self, "UserPoolClientId", value=client.user_pool_client_id)
        CfnOutput(self, "JwtIssuer", value=pool.user_pool_provider_url)
