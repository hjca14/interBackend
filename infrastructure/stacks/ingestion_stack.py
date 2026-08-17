"""Basic Ingest runtime pipeline with sanitized quarantine and technical DLQ."""

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

        self.invalid_message_quarantine = sqs.Queue(
            self,
            "InvalidMessageQuarantine",
            queue_name=self.names.invalid_quarantine_queue_name,
            retention_period=Duration.days(self.settings.quarantine_days),
            encryption=sqs.QueueEncryption.SQS_MANAGED,
        )
        self.technical_dlq = sqs.Queue(
            self,
            "IngestionTechnicalDlq",
            queue_name=self.names.technical_dlq_name,
            retention_period=Duration.days(self.settings.technical_dlq_days),
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
                    "dynamodb:PutItem",
                    "dynamodb:UpdateItem",
                    "dynamodb:TransactWriteItems",
                ],
                resources=[data_stack.telemetry_table.table_arn],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["sqs:SendMessage"],
                resources=[self.invalid_message_quarantine.queue_arn],
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
            dead_letter_queue=self.technical_dlq,
            environment={
                "TELEMETRY_TABLE_NAME": data_stack.telemetry_table.table_name,
                "INVALID_QUARANTINE_QUEUE_URL": self.invalid_message_quarantine.queue_url,
                "HISTORY_DAYS": str(self.settings.history_days),
                "DETAIL_LIMIT": str(self.settings.detailed_limit_per_hour),
                "MAX_PAYLOAD_BYTES": str(self.settings.max_payload_bytes),
            },
        )
        self.function.node.add_dependency(self.log_group)
        rule_error_role = iam.Role(
            self, "RuleErrorRole", assumed_by=iam.ServicePrincipal("iot.amazonaws.com")
        )
        rule_error_role.add_to_policy(
            iam.PolicyStatement(
                actions=["sqs:SendMessage"], resources=[self.technical_dlq.queue_arn]
            )
        )
        error_action = iot.CfnTopicRule.ActionProperty(
            sqs=iot.CfnTopicRule.SqsActionProperty(
                queue_url=self.technical_dlq.queue_url,
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
                "WHERE isUndefined(_ib_device_id) AND isUndefined(_ib_category) "
                "AND isUndefined(_ib_received_at) "
                "AND (topic(3) = 'events' OR topic(3) = 'health')"
            ),
            lambda_action,
            error_action,
        )
        self.response_rule = self._rule(
            "ResponseRule",
            rules.response_rule_name,
            (
                "SELECT *, topic(2) AS _ib_device_id, topic(3) AS _ib_category, "
                "timestamp() AS _ib_received_at FROM 'interbridge/+/responses' "
                "WHERE isUndefined(_ib_device_id) AND isUndefined(_ib_category) "
                "AND isUndefined(_ib_received_at)"
            ),
            lambda_action,
            error_action,
        )
        for construct_id, rule_name in (
            ("AllowIngestRuleInvoke", rules.ingest_rule_name),
            ("AllowResponseRuleInvoke", rules.response_rule_name),
        ):
            self.function.add_permission(
                construct_id,
                principal=iam.ServicePrincipal("iot.amazonaws.com"),
                source_account=self.account,
                source_arn=self.format_arn(service="iot", resource="rule", resource_name=rule_name),
            )
        CfnOutput(self, "IngestionFunctionName", value=self.function.function_name)
        CfnOutput(
            self,
            "InvalidMessageQuarantineName",
            value=self.invalid_message_quarantine.queue_name,
        )
        CfnOutput(self, "IngestionTechnicalDlqName", value=self.technical_dlq.queue_name)

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
