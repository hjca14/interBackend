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


def test_runtime_resources_retention_concurrency_and_minimal_iam() -> None:
    body, _ = synth()
    types = [resource["Type"] for resource in body["Resources"].values()]
    assert types.count("AWS::Lambda::Function") == 1
    assert types.count("AWS::SQS::Queue") == 1
    assert types.count("AWS::Logs::LogGroup") == 1
    function = next(r for r in body["Resources"].values() if r["Type"] == "AWS::Lambda::Function")
    assert function["Properties"]["ReservedConcurrentExecutions"] == 2
    queue = next(r for r in body["Resources"].values() if r["Type"] == "AWS::SQS::Queue")
    assert queue["Properties"]["MessageRetentionPeriod"] == 4 * 86400
    rendered = json.dumps(body)
    for wildcard in ('"dynamodb:*"', '"iot:*"', '"sqs:*"', '"Resource":"*"'):
        assert wildcard not in rendered
    assert "interbridge-dev-devices" not in rendered


def test_three_low_cost_alarms_without_dashboard() -> None:
    _, body = synth()
    types = [resource["Type"] for resource in body["Resources"].values()]
    assert types.count("AWS::CloudWatch::Alarm") == 3
    assert "AWS::CloudWatch::Dashboard" not in types
