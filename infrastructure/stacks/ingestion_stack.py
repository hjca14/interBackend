"""Basic Ingest runtime pipeline: IoT rules, Lambda, logs and quarantine queue."""

from __future__ import annotations

from typing import Any

from aws_cdk import CfnOutput, Duration, Stack, Tags
from aws_cdk import aws_iam as iam
from aws_cdk import aws_iot as iot
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_sqs as sqs

from constructs import Construct
from infrastructure.config.environment import EnvironmentConfig
from infrastructure.config.ingestion import IngestionConfig, ingestion_names
from infrastructure.config.iot import iot_names
from infrastructure.stacks.data_stack import DataStack


class IngestionStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: EnvironmentConfig,
        data_stack: DataStack,
        settings: IngestionConfig | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        self.config = config
        self.settings = settings or IngestionConfig()
        self.names = ingestion_names(config)
        rules = iot_names(config)
        for key, value in {**config.standard_tags, **config.component_tag("ingestion")}.items():
            Tags.of(self).add(key, value)

        self.quarantine_queue = sqs.Queue(
            self,
            "QuarantineQueue",
            queue_name=self.names.quarantine_queue_name,
            retention_period=Duration.days(self.settings.quarantine_days),
            encryption=sqs.QueueEncryption.SQS_MANAGED,
        )
        role = iam.Role(
            self,
            "IngestionFunctionRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
        )
        log_group_name = f"/aws/lambda/{self.names.function_name}"
        self.log_group = logs.LogGroup(
            self,
            "IngestionLogGroup",
            log_group_name=log_group_name,
            retention=self.settings.log_retention,
        )
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["logs:CreateLogStream", "logs:PutLogEvents"],
                resources=[f"{self.log_group.log_group_arn}:*"],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "dynamodb:GetItem",
                    "dynamodb:PutItem",
                    "dynamodb:UpdateItem",
                ],
                resources=[data_stack.telemetry_table.table_arn],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["sqs:SendMessage"], resources=[self.quarantine_queue.queue_arn]
            )
        )
        self.function = lambda_.Function(
            self,
            "IngestionFunction",
            function_name=self.names.function_name,
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            handler="lambdas.telemetry_ingestion.handler.lambda_handler",
            code=lambda_.Code.from_asset(
                ".", exclude=[".git", ".venv", "cdk.out", "tests", "docs"]
            ),
            role=role,
            timeout=Duration.seconds(15),
            memory_size=256,
            reserved_concurrent_executions=self.settings.reserved_concurrency,
            dead_letter_queue=self.quarantine_queue,
            environment={
                "TELEMETRY_TABLE_NAME": data_stack.telemetry_table.table_name,
                "QUARANTINE_QUEUE_URL": self.quarantine_queue.queue_url,
                "HISTORY_DAYS": str(self.settings.history_days),
                "DETAIL_LIMIT": str(self.settings.detailed_limit_per_hour),
                "MAX_PAYLOAD_BYTES": str(self.settings.max_payload_bytes),
            },
        )
        self.function.node.add_dependency(self.log_group)
        self.function.add_permission(
            "AllowIotInvoke", principal=iam.ServicePrincipal("iot.amazonaws.com")
        )

        rule_error_role = iam.Role(
            self, "RuleErrorRole", assumed_by=iam.ServicePrincipal("iot.amazonaws.com")
        )
        rule_error_role.add_to_policy(
            iam.PolicyStatement(
                actions=["sqs:SendMessage"], resources=[self.quarantine_queue.queue_arn]
            )
        )
        error_action = iot.CfnTopicRule.ActionProperty(
            sqs=iot.CfnTopicRule.SqsActionProperty(
                queue_url=self.quarantine_queue.queue_url,
                role_arn=rule_error_role.role_arn,
                use_base64=False,
            )
        )
        lambda_action = iot.CfnTopicRule.ActionProperty(
            lambda_=iot.CfnTopicRule.LambdaActionProperty(function_arn=self.function.function_arn)
        )
        self.ingest_rule = self._rule(
            "IngestRule",
            rules.ingest_rule_name,
            (
                "SELECT *, topic(2) AS _ib_device_id, topic(3) AS _ib_category, "
                "timestamp() AS _ib_received_at FROM 'interbridge/+/+' "
                "WHERE topic(3) = 'events' OR topic(3) = 'health'"
            ),
            lambda_action,
            error_action,
        )
        self.response_rule = self._rule(
            "ResponseRule",
            rules.response_rule_name,
            (
                "SELECT *, topic(2) AS _ib_device_id, topic(3) AS _ib_category, "
                "timestamp() AS _ib_received_at FROM 'interbridge/+/responses'"
            ),
            lambda_action,
            error_action,
        )
        CfnOutput(self, "IngestionFunctionName", value=self.function.function_name)
        CfnOutput(self, "QuarantineQueueName", value=self.quarantine_queue.queue_name)

    def _rule(
        self,
        construct_id: str,
        name: str,
        sql: str,
        action: iot.CfnTopicRule.ActionProperty,
        error_action: iot.CfnTopicRule.ActionProperty,
    ) -> iot.CfnTopicRule:
        return iot.CfnTopicRule(
            self,
            construct_id,
            rule_name=name,
            topic_rule_payload=iot.CfnTopicRule.TopicRulePayloadProperty(
                actions=[action],
                error_action=error_action,
                aws_iot_sql_version="2016-03-23",
                rule_disabled=False,
                sql=sql,
            ),
        )
