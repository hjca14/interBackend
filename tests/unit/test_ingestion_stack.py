from __future__ import annotations

import json
from typing import Any

import aws_cdk as cdk
from aws_cdk import Stack
from aws_cdk import aws_lambda as lambda_
from aws_cdk.assertions import Template

from infrastructure.config.environment import EnvironmentConfig
from infrastructure.config.iot import iot_names
from infrastructure.stacks import DataStack, IngestionStack, ObservabilityStack


def synth() -> tuple[dict[str, Any], dict[str, Any]]:
    app = cdk.App()
    config = EnvironmentConfig()
    data = DataStack(app, "Data", config=config)
    ingestion = IngestionStack(app, "Ingestion", config=config, data_stack=data)
    observation = ObservabilityStack(app, "Observation", config=config, ingestion_stack=ingestion)
    return Template.from_stack(ingestion).to_json(), Template.from_stack(observation).to_json()


def _stand_in_push_sender(app: cdk.App) -> lambda_.IFunction:
    # A minimal, non-Docker-bundled stand-in for NotificationStack's real
    # push_sender Lambda, used only to test IngestionStack's own wiring
    # (env var + IAM grant) without paying for a real Docker bundling
    # synth in every test in this file -- see test_notification_stack.py
    # for the real NotificationStack's own assertions.
    support = Stack(app, "SupportingNotification")
    return lambda_.Function(
        support,
        "StandInPushSender",
        runtime=lambda_.Runtime.PYTHON_3_12,
        handler="index.handler",
        code=lambda_.Code.from_inline("def handler(event, context): pass"),
    )


def test_two_reserved_basic_ingest_rules_and_stable_sql() -> None:
    body, _ = synth()
    rules = [r for r in body["Resources"].values() if r["Type"] == "AWS::IoT::TopicRule"]
    assert len(rules) == 2
    names = iot_names(EnvironmentConfig())
    assert {r["Properties"]["RuleName"] for r in rules} == {
        names.ingest_rule_name,
        names.response_rule_name,
    }
    for rule in rules:
        payload = rule["Properties"]["TopicRulePayload"]
        sql = payload["Sql"]
        assert payload["AwsIotSqlVersion"] == "2016-03-23"
        assert "$aws/rules" not in sql
        assert " AS _" not in sql
        assert sql.count(" AS ibmeta_device_id") == 1
        assert sql.count(" AS ibmeta_category") == 1
        assert sql.count(" AS ibmeta_received_at") == 1
        for name in ("ibmeta_device_id", "ibmeta_category", "ibmeta_received_at"):
            assert f"isUndefined({name})" in sql


def test_topic_rules_reject_device_supplied_internal_metadata() -> None:
    body, _ = synth()
    rules = [r for r in body["Resources"].values() if r["Type"] == "AWS::IoT::TopicRule"]
    for injected_name in (
        "ibmeta_device_id",
        "ibmeta_category",
        "ibmeta_received_at",
    ):
        # WHERE runs before SELECT in AWS IoT SQL, so this predicate checks the
        # original payload and rejects a collision with each reserved field.
        for rule in rules:
            sql = rule["Properties"]["TopicRulePayload"]["Sql"]
            assert f"isUndefined({injected_name})" in sql


def test_runtime_resources_cost_controls_and_minimal_iam() -> None:
    body, _ = synth()
    types = [resource["Type"] for resource in body["Resources"].values()]
    assert len(body["Resources"]) == 12
    assert types.count("AWS::Lambda::Function") == 1
    assert types.count("AWS::SQS::Queue") == 2
    assert types.count("AWS::Logs::LogGroup") == 1
    function = next(r for r in body["Resources"].values() if r["Type"] == "AWS::Lambda::Function")
    assert "ReservedConcurrentExecutions" not in function["Properties"]
    assert function["Properties"]["Timeout"] == 15
    assert function["Properties"]["MemorySize"] == 256
    assert function["Properties"]["Environment"]["Variables"]["HISTORY_DAYS"] == "30"
    assert function["Properties"]["Environment"]["Variables"]["DETAIL_LIMIT"] == "200"
    queues = [r for r in body["Resources"].values() if r["Type"] == "AWS::SQS::Queue"]
    assert all(queue["Properties"]["MessageRetentionPeriod"] == 4 * 86400 for queue in queues)
    rendered = json.dumps(body)
    for wildcard in ('"dynamodb:*"', '"iot:*"', '"sqs:*"', '"Resource":"*"'):
        assert wildcard not in rendered
    assert "interbridge-dev-devices" not in rendered
    assert "dynamodb:TransactWriteItems" in rendered
    assert "invalid-message-quarantine" in rendered
    assert "technical-dlq" in rendered


def test_iot_lambda_permissions_are_scoped_to_account_and_each_rule() -> None:
    body, _ = synth()
    permissions = [
        resource
        for resource in body["Resources"].values()
        if resource["Type"] == "AWS::Lambda::Permission"
    ]
    assert len(permissions) == 2
    names = iot_names(EnvironmentConfig())
    rendered = json.dumps(permissions)
    assert names.ingest_rule_name in rendered
    assert names.response_rule_name in rendered
    for permission in permissions:
        properties = permission["Properties"]
        assert properties["Principal"] == "iot.amazonaws.com"
        assert properties["SourceAccount"] == {"Ref": "AWS::AccountId"}
        assert ":rule/" in json.dumps(properties["SourceArn"])


def test_rule_error_role_trust_is_scoped_to_account_and_exact_rules() -> None:
    body, _ = synth()
    role = next(
        resource
        for logical_id, resource in body["Resources"].items()
        if logical_id.startswith("RuleErrorRole") and resource["Type"] == "AWS::IAM::Role"
    )
    statement = role["Properties"]["AssumeRolePolicyDocument"]["Statement"]
    assert len(statement) == 1
    assert statement[0]["Principal"] == {"Service": "iot.amazonaws.com"}
    assert statement[0]["Condition"]["StringEquals"]["aws:SourceAccount"] == {
        "Ref": "AWS::AccountId"
    }
    names = iot_names(EnvironmentConfig())
    expected_arns = {
        json.dumps(
            {
                "Fn::Join": [
                    "",
                    [
                        "arn:",
                        {"Ref": "AWS::Partition"},
                        ":iot:",
                        {"Ref": "AWS::Region"},
                        ":",
                        {"Ref": "AWS::AccountId"},
                        f":rule/{rule_name}",
                    ],
                ]
            },
            sort_keys=True,
        )
        for rule_name in (names.ingest_rule_name, names.response_rule_name)
    }
    actual_arns = statement[0]["Condition"]["ArnEquals"]["aws:SourceArn"]
    assert {json.dumps(arn, sort_keys=True) for arn in actual_arns} == expected_arns


def test_rule_error_role_can_only_send_to_technical_dlq() -> None:
    body, _ = synth()
    technical_queue_id = next(
        logical_id
        for logical_id, resource in body["Resources"].items()
        if resource["Type"] == "AWS::SQS::Queue"
        and "technical-dlq" in resource["Properties"]["QueueName"]
    )
    policy = next(
        resource
        for logical_id, resource in body["Resources"].items()
        if logical_id.startswith("RuleErrorRoleDefaultPolicy")
        and resource["Type"] == "AWS::IAM::Policy"
    )
    statements = policy["Properties"]["PolicyDocument"]["Statement"]
    assert statements == [
        {
            "Action": "sqs:SendMessage",
            "Effect": "Allow",
            "Resource": {"Fn::GetAtt": [technical_queue_id, "Arn"]},
        }
    ]


def test_sanitized_quarantine_is_separate_from_technical_dlq() -> None:
    body, _ = synth()
    queues = {
        logical_id: resource
        for logical_id, resource in body["Resources"].items()
        if resource["Type"] == "AWS::SQS::Queue"
    }
    invalid_id = next(
        logical_id
        for logical_id, queue in queues.items()
        if "invalid-message-quarantine" in queue["Properties"]["QueueName"]
    )
    technical_id = next(
        logical_id
        for logical_id, queue in queues.items()
        if "technical-dlq" in queue["Properties"]["QueueName"]
    )
    function = next(
        resource
        for resource in body["Resources"].values()
        if resource["Type"] == "AWS::Lambda::Function"
    )
    assert function["Properties"]["DeadLetterConfig"]["TargetArn"] == {
        "Fn::GetAtt": [technical_id, "Arn"]
    }
    queue_url = function["Properties"]["Environment"]["Variables"]["INVALID_QUARANTINE_QUEUE_URL"]
    assert queue_url == {"Ref": invalid_id}
    rules = [
        resource
        for resource in body["Resources"].values()
        if resource["Type"] == "AWS::IoT::TopicRule"
    ]
    for rule in rules:
        assert rule["Properties"]["TopicRulePayload"]["ErrorAction"]["Sqs"]["QueueUrl"] == {
            "Ref": technical_id
        }


def test_four_low_cost_alarms_without_dashboard() -> None:
    _, body = synth()
    types = [resource["Type"] for resource in body["Resources"].values()]
    assert types.count("AWS::CloudWatch::Alarm") == 4
    assert "AWS::CloudWatch::Dashboard" not in types


def test_push_sender_function_is_optional_and_absent_by_default() -> None:
    body, _ = synth()
    rendered = json.dumps(body)
    assert "PUSH_SENDER_FUNCTION_NAME" not in rendered
    # AWS::Lambda::Permission entries (IoT invoking the ingestion function
    # itself) legitimately mention this action string as a resource-based
    # grant; only an IAM *policy statement* on the role would mean this
    # function can call something else, which must not exist by default.
    policies = [r for r in body["Resources"].values() if r["Type"] == "AWS::IAM::Policy"]
    invoke_statements = [
        statement
        for policy in policies
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]
        if statement["Action"] == "lambda:InvokeFunction"
    ]
    assert invoke_statements == []


def test_push_sender_function_when_provided_gets_env_var_and_scoped_invoke_permission() -> None:
    app = cdk.App()
    config = EnvironmentConfig()
    data = DataStack(app, "DataWithPush", config=config)
    push_sender = _stand_in_push_sender(app)
    ingestion = IngestionStack(
        app,
        "IngestionWithPush",
        config=config,
        data_stack=data,
        push_sender_function=push_sender,
    )
    body = Template.from_stack(ingestion).to_json()
    function = next(r for r in body["Resources"].values() if r["Type"] == "AWS::Lambda::Function")
    assert "PUSH_SENDER_FUNCTION_NAME" in function["Properties"]["Environment"]["Variables"]

    policies = [r for r in body["Resources"].values() if r["Type"] == "AWS::IAM::Policy"]
    invoke_statements = [
        statement
        for policy in policies
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]
        if statement["Action"] == "lambda:InvokeFunction"
    ]
    assert len(invoke_statements) == 1
    resource = invoke_statements[0]["Resource"]
    assert not isinstance(resource, list)  # scoped to exactly one function ARN
    rendered = json.dumps(body)
    assert '"lambda:*"' not in rendered


class _StandInNotificationStack:
    """Duck-types just enough of NotificationStack (``.function``,
    ``.async_failure_dlq``) for ObservabilityStack's optional alarms,
    without paying for a real Docker-bundled synth in this file -- see
    test_notification_stack.py for NotificationStack's own assertions.
    """

    def __init__(self, app: cdk.App) -> None:
        stack = Stack(app, "SupportingNotificationForAlarms")
        self.function = lambda_.Function(
            stack,
            "StandInPushSenderForAlarms",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="index.handler",
            code=lambda_.Code.from_inline("def handler(event, context): pass"),
        )
        import aws_cdk.aws_sqs as sqs

        self.async_failure_dlq = sqs.Queue(stack, "StandInDlq")


def test_notification_alarms_are_added_when_notification_stack_is_provided() -> None:
    app = cdk.App()
    config = EnvironmentConfig()
    data = DataStack(app, "DataForAlarms", config=config)
    ingestion = IngestionStack(app, "IngestionForAlarms", config=config, data_stack=data)
    notification = _StandInNotificationStack(app)
    observability = ObservabilityStack(
        app,
        "ObservabilityWithNotification",
        config=config,
        ingestion_stack=ingestion,
        notification_stack=notification,  # type: ignore[arg-type]
    )
    body = Template.from_stack(observability).to_json()
    types = [resource["Type"] for resource in body["Resources"].values()]
    assert types.count("AWS::CloudWatch::Alarm") == 7
    from infrastructure.config.notifications import notification_names

    push_names = notification_names(config)
    rendered = json.dumps(body)
    assert push_names.errors_alarm_name in rendered
    assert push_names.throttles_alarm_name in rendered
    assert push_names.async_failure_alarm_name in rendered
