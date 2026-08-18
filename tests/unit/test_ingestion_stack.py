from __future__ import annotations

import json
from typing import Any

import aws_cdk as cdk
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
        assert payload["AwsIotSqlVersion"] == "2016-03-23"
        assert "$aws/rules" not in payload["Sql"]
        assert "topic(2) AS _ib_device_id" in payload["Sql"]
        assert "isUndefined(_ib_device_id)" in payload["Sql"]


def test_runtime_resources_retention_concurrency_and_minimal_iam() -> None:
    body, _ = synth()
    types = [resource["Type"] for resource in body["Resources"].values()]
    assert types.count("AWS::Lambda::Function") == 1
    assert types.count("AWS::SQS::Queue") == 2
    assert types.count("AWS::Logs::LogGroup") == 1
    function = next(r for r in body["Resources"].values() if r["Type"] == "AWS::Lambda::Function")
    assert function["Properties"]["ReservedConcurrentExecutions"] == 2
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
