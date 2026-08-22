"""Cognito and the authenticated Phase 2B/2D HTTP API."""

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
from infrastructure.config.iot import commands_topic
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
                min_length=8,
                require_lowercase=True,
                require_uppercase=True,
                require_digits=True,
                require_symbols=False,
            ),
            user_verification=cognito.UserVerificationConfig(
                email_subject="InterBridge | Código de confirmação / Confirmation code",
                email_body=(
                    "Olá,\n\nSeu código de confirmação do InterBridge é {####}.\n\n"
                    "Se você não solicitou esta conta, ignore este e-mail.\n\n"
                    "---\n\nHello,\n\nYour InterBridge confirmation code is {####}.\n\n"
                    "If you did not request this account, please ignore this email.\n\n"
                    "InterBridge"
                ),
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
            refresh_token_validity=Duration.days(3650),
        )
        cursor_key = kms.Key(
            self,
            "CursorKey",
            alias=f"alias/{resource_name(config, 'api', 'cursor')}",
            enable_key_rotation=False,
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
        create_command_fn = lambda_.Function(
            self,
            "CreateCommandFunction",
            handler="command_api.handler.create_command",
            environment=env,
            log_group=logs.LogGroup(
                self,
                "CreateCommandLogs",
                retention=logs.RetentionDays.ONE_WEEK,
                removal_policy=RemovalPolicy.DESTROY,
            ),
            **common,
        )
        get_command_fn = lambda_.Function(
            self,
            "GetCommandFunction",
            handler="command_api.handler.get_command",
            environment=env,
            log_group=logs.LogGroup(
                self,
                "GetCommandLogs",
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
        create_command_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["dynamodb:GetItem"],
                resources=[
                    data_stack.device_memberships_table.table_arn,
                    data_stack.devices_table.table_arn,
                    data_stack.telemetry_table.table_arn,
                ],
            )
        )
        create_command_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["dynamodb:PutItem"],
                resources=[data_stack.telemetry_table.table_arn],
                conditions={
                    "ForAnyValue:StringEquals": {
                        "dynamodb:EnclosingOperation": ["TransactWriteItems"]
                    }
                },
            )
        )
        create_command_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["dynamodb:UpdateItem"],
                resources=[data_stack.telemetry_table.table_arn],
            )
        )
        create_command_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["iot:Publish"],
                resources=[
                    self.format_arn(
                        service="iot",
                        resource="topic",
                        resource_name=commands_topic("ib-*"),
                    )
                ],
            )
        )
        # DescribeEndpoint has no resource-level ARN in AWS IoT IAM; the
        # exact action is used once per cold execution environment and cached.
        create_command_fn.add_to_role_policy(
            iam.PolicyStatement(actions=["iot:DescribeEndpoint"], resources=["*"])
        )
        get_command_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["dynamodb:GetItem"],
                resources=[
                    data_stack.device_memberships_table.table_arn,
                    data_stack.devices_table.table_arn,
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
        create_command_routes = api.add_routes(
            path="/v1/devices/{device_id}/commands",
            methods=[apigw.HttpMethod.POST],
            authorizer=auth,
            integration=integrations.HttpLambdaIntegration(
                "CreateCommandIntegration",
                create_command_fn,
                payload_format_version=apigw.PayloadFormatVersion.VERSION_2_0,
            ),
        )
        api.add_routes(
            path="/v1/devices/{device_id}/commands/{command_id}",
            methods=[apigw.HttpMethod.GET],
            authorizer=auth,
            integration=integrations.HttpLambdaIntegration(
                "GetCommandIntegration",
                get_command_fn,
                payload_format_version=apigw.PayloadFormatVersion.VERSION_2_0,
            ),
        )
        # A deliberately small DEV route throttle complements (never replaces)
        # the atomic per-user/device cooldown in the handler.
        if api.default_stage is None:
            raise RuntimeError("HTTP API default stage was not created")
        cfn_stage = api.default_stage.node.default_child
        if not isinstance(cfn_stage, apigw.CfnStage):
            raise RuntimeError("unexpected HTTP API stage implementation")
        if len(create_command_routes) != 1:
            raise RuntimeError("unexpected create-command route count")
        cfn_create_command_route = create_command_routes[0].node.default_child
        if not isinstance(cfn_create_command_route, apigw.CfnRoute):
            raise RuntimeError("unexpected create-command route implementation")
        cfn_stage.add_dependency(cfn_create_command_route)
        cfn_stage.add_property_override(
            "RouteSettings.POST /v1/devices/{device_id}/commands",
            {"ThrottlingBurstLimit": 2, "ThrottlingRateLimit": 1},
        )
        CfnOutput(self, "ApiUrl", value=api.api_endpoint)
        CfnOutput(self, "UserPoolId", value=pool.user_pool_id)
        CfnOutput(self, "UserPoolClientId", value=client.user_pool_client_id)
        CfnOutput(self, "JwtIssuer", value=pool.user_pool_provider_url)

        # Dedicated break-glass-style DEV operation role. AccountPrincipal does
        # not grant assume access by itself: the caller also needs an explicit
        # identity policy for sts:AssumeRole, and must authenticate with MFA.
        registrar_role = iam.Role(
            self,
            "DevDeviceRegistrarRole",
            role_name="interbridge-dev-device-registrar-role",
            assumed_by=iam.PrincipalWithConditions(
                iam.AccountPrincipal(Stack.of(self).account),
                conditions={"Bool": {"aws:MultiFactorAuthPresent": "true"}},
            ),
            description="Scoped DEV role used only by tools/register_dev_device.py",
        )
        registrar_role.add_to_policy(
            iam.PolicyStatement(actions=["sts:GetCallerIdentity"], resources=["*"])
        )
        registrar_role.add_to_policy(
            iam.PolicyStatement(
                actions=["cloudformation:DescribeStacks"],
                resources=[
                    self.format_arn(
                        service="cloudformation",
                        resource="stack",
                        resource_name=f"InterBridge-Dev-{name}Stack/*",
                    )
                    for name in ("Data", "Api")
                ],
            )
        )
        registrar_role.add_to_policy(
            iam.PolicyStatement(actions=["cognito-idp:ListUsers"], resources=[pool.user_pool_arn])
        )
        registrar_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "iot:DescribeThing",
                    "iot:ListThingGroupsForThing",
                    "iot:ListThingPrincipals",
                ],
                resources=[self.format_arn(service="iot", resource="thing", resource_name="ib-*")],
            )
        )
        registrar_role.add_to_policy(
            iam.PolicyStatement(
                actions=["iot:DescribeCertificate", "iot:ListAttachedPolicies"],
                resources=[self.format_arn(service="iot", resource="cert", resource_name="*")],
            )
        )
        registrar_role.add_to_policy(
            iam.PolicyStatement(
                actions=["dynamodb:GetItem"],
                resources=[
                    data_stack.devices_table.table_arn,
                    data_stack.device_memberships_table.table_arn,
                ],
            )
        )
        registrar_role.add_to_policy(
            iam.PolicyStatement(
                actions=["dynamodb:PutItem"],
                resources=[
                    data_stack.devices_table.table_arn,
                    data_stack.device_memberships_table.table_arn,
                ],
                conditions={
                    "ForAnyValue:StringEquals": {
                        "dynamodb:EnclosingOperation": ["TransactWriteItems"]
                    }
                },
            )
        )
        CfnOutput(self, "DevDeviceRegistrarRoleArn", value=registrar_role.role_arn)
