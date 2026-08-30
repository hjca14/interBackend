"""FCM push sender runtime (Fase 3B.6/3B.7).

Owns the push_sender Lambda, its execution role, and the async-invoke
failure DLQ. Depends only on ``DataStack`` (memberships, push
installations, and push deliveries tables); the Firebase service-account
credential is *referenced*, never created, in AWS Secrets Manager -- see
``docs/fcm-notification-sender.md`` for the manual provisioning procedure
this stack deliberately does not automate.

The Lambda's asset is Docker-bundled (not the plain ``lambdas/`` asset the
API Gateway Lambdas share, and not the whole-repo-no-bundling asset
IngestionStack uses): ``lambdas/push_sender/requirements.txt`` pins
``google-auth``/``requests`` (and their transitive dependencies) exactly,
installed for the Lambda's own Linux/ARM64/cp312 target regardless of the
host architecture running ``cdk synth``/``cdk deploy``, so the artifact is
reproducible and CI exercises the same bundling path deploy would. No
wheel or binary is committed to this repository.
"""

from __future__ import annotations

from typing import Any

from aws_cdk import ArnFormat, BundlingOptions, CfnOutput, Duration, Stack, Tags
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_lambda_destinations as destinations
from aws_cdk import aws_logs as logs
from aws_cdk import aws_sqs as sqs

from constructs import Construct
from infrastructure.config.environment import EnvironmentConfig
from infrastructure.config.notifications import NotificationConfig, notification_names
from infrastructure.stacks.data_stack import DataStack

_PIP_TARGET_PLATFORM_ARGS = (
    "--platform manylinux2014_aarch64 --python-version 3.12 "
    "--implementation cp --abi cp312 --only-binary=:all:"
)


class NotificationStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: EnvironmentConfig,
        data_stack: DataStack,
        settings: NotificationConfig | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        self.config = config
        self.settings = settings or NotificationConfig()
        self.names = notification_names(config)
        for key, value in {**config.standard_tags, **config.component_tag("notifications")}.items():
            Tags.of(self).add(key, value)

        self.async_failure_dlq = sqs.Queue(
            self,
            "PushSenderAsyncFailureDlq",
            queue_name=self.names.async_failure_dlq_name,
            retention_period=Duration.days(self.settings.async_dlq_retention_days),
            encryption=sqs.QueueEncryption.SQS_MANAGED,
        )

        # Referenced, never created here -- see the module docstring and
        # docs/fcm-notification-sender.md for the manual procedure. Built
        # by hand (not secretsmanager.Secret.from_secret_name_v2(), whose
        # own docstring warns its .secret_arn is a *partial* ARN -- no
        # trailing random suffix -- and "could lead to AccessDeniedException
        # when you pass the partial ARN to CLI or SDK to get the secret
        # value": https://docs.aws.amazon.com/secretsmanager/latest/userguide/troubleshoot.html#ARN_secretnamehyphen).
        # Secrets Manager always appends six random characters to a secret's
        # name at creation time, so an IAM Resource has to either know that
        # suffix or end in a wildcard to ever match the real secret -- this
        # mirrors exactly the ARN shape CDK's own ISecret.grant_read()
        # produces internally (verified against a throwaway synth), while
        # keeping the grant below scoped to only GetSecretValue rather than
        # grant_read()'s GetSecretValue+DescribeSecret.
        self.firebase_credentials_secret_arn = self.format_arn(
            service="secretsmanager",
            resource="secret",
            resource_name=f"{self.names.firebase_credentials_secret_name}-??????",
            arn_format=ArnFormat.COLON_RESOURCE_NAME,
        )

        role = iam.Role(
            self,
            "PushSenderFunctionRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
        )
        log_group = logs.LogGroup(
            self,
            "PushSenderLogGroup",
            log_group_name=f"/aws/lambda/{self.names.function_name}",
            retention=self.settings.log_retention,
        )
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["logs:CreateLogStream", "logs:PutLogEvents"],
                resources=[f"{log_group.log_group_arn}:*"],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["dynamodb:PutItem", "dynamodb:GetItem", "dynamodb:UpdateItem"],
                resources=[data_stack.push_deliveries_table.table_arn],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["dynamodb:Query"],
                resources=[data_stack.device_memberships_table.table_arn],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["dynamodb:Query"],
                resources=[
                    f"{data_stack.push_installations_table.table_arn}/index/"
                    f"{data_stack.names.push_installations_by_user_index_name}"
                ],
            )
        )
        # BatchGetItem for fan-out reads; DeleteItem for the transactional
        # invalid-token cleanup (TransactWriteItems authorizes via the
        # underlying item actions -- DynamoDB has no separate
        # "TransactWriteItems" IAM action). Matches the unconditioned
        # pattern lambdas/push_api's own IAM already uses for this table.
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["dynamodb:BatchGetItem", "dynamodb:DeleteItem"],
                resources=[data_stack.push_installations_table.table_arn],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["secretsmanager:GetSecretValue"],
                resources=[self.firebase_credentials_secret_arn],
            )
        )

        self.function = lambda_.Function(
            self,
            "PushSenderFunction",
            function_name=self.names.function_name,
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            handler="lambdas.push_sender.handler.lambda_handler",
            code=lambda_.Code.from_asset(
                ".",
                exclude=[".git", ".venv", "cdk.out", "tests", "docs"],
                bundling=BundlingOptions(
                    image=lambda_.Runtime.PYTHON_3_12.bundling_image,
                    command=[
                        "bash",
                        "-c",
                        "pip install --no-cache-dir "
                        f"{_PIP_TARGET_PLATFORM_ARGS} "
                        "-r lambdas/push_sender/requirements.txt -t /asset-output "
                        "&& rm -rf /asset-output/bin "
                        "&& cp -r domain /asset-output/domain "
                        "&& cp -r lambdas /asset-output/lambdas",
                    ],
                ),
            ),
            role=role,
            timeout=Duration.seconds(20),
            memory_size=256,
            retry_attempts=self.settings.async_retry_attempts,
            on_failure=destinations.SqsDestination(self.async_failure_dlq),
            environment={
                "MEMBERSHIPS_TABLE": data_stack.device_memberships_table.table_name,
                "PUSH_INSTALLATIONS_TABLE": data_stack.push_installations_table.table_name,
                "PUSH_INSTALLATIONS_BY_USER_INDEX": (
                    data_stack.names.push_installations_by_user_index_name
                ),
                "PUSH_DELIVERIES_TABLE": data_stack.push_deliveries_table.table_name,
                "FIREBASE_CREDENTIALS_SECRET_NAME": (self.names.firebase_credentials_secret_name),
            },
        )
        self.function.node.add_dependency(log_group)

        CfnOutput(self, "PushSenderFunctionName", value=self.function.function_name)
        CfnOutput(
            self,
            "FirebaseCredentialsSecretNameExpected",
            value=self.names.firebase_credentials_secret_name,
            description=(
                "Secrets Manager secret name this stack expects to already "
                "exist -- see docs/fcm-notification-sender.md for the "
                "manual creation procedure. This stack never creates it."
            ),
        )
