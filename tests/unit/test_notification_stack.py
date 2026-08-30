"""Semantic tests for NotificationStack (Fase 3B.6/3B.7)."""

from __future__ import annotations

import json
from typing import Any

import aws_cdk as cdk
from aws_cdk.assertions import Template

from infrastructure.config.data import data_names
from infrastructure.config.environment import EnvironmentConfig
from infrastructure.config.notifications import notification_names
from infrastructure.stacks.data_stack import DataStack
from infrastructure.stacks.notification_stack import NotificationStack


def _synth() -> tuple[NotificationStack, dict[str, Any]]:
    app = cdk.App()
    config = EnvironmentConfig()
    data = DataStack(app, "SupportingData", config=config)
    stack = NotificationStack(app, "TestNotificationStack", config=config, data_stack=data)
    return stack, Template.from_stack(stack).to_json()


def test_no_dynamodb_table_or_secret_is_created_by_this_stack() -> None:
    _, body = _synth()
    types = {res["Type"] for res in body["Resources"].values()}
    assert "AWS::DynamoDB::Table" not in types
    assert "AWS::SecretsManager::Secret" not in types


def test_exactly_one_lambda_function_with_expected_runtime_and_handler() -> None:
    _, body = _synth()
    functions = [r for r in body["Resources"].values() if r["Type"] == "AWS::Lambda::Function"]
    assert len(functions) == 1
    function = functions[0]
    assert function["Properties"]["Runtime"] == "python3.12"
    assert function["Properties"]["Architectures"] == ["arm64"]
    assert function["Properties"]["Handler"] == "lambdas.push_sender.handler.lambda_handler"


def test_function_environment_references_the_expected_tables_and_index() -> None:
    _, body = _synth()
    config = EnvironmentConfig()
    function = next(r for r in body["Resources"].values() if r["Type"] == "AWS::Lambda::Function")
    env = function["Properties"]["Environment"]["Variables"]
    # DataStack is a separate stack, so its table *names* (real resource
    # attributes) are genuine cross-stack references (CloudFormation
    # Fn::ImportValue) -- matching the same pattern ApiStack already uses
    # for these same tables (see tests/unit/test_api_stack.py). The GSI
    # *index name* is a deterministic constant from DataNames, not a CDK
    # token, so it renders as a literal string even across stacks.
    for key in ("MEMBERSHIPS_TABLE", "PUSH_INSTALLATIONS_TABLE", "PUSH_DELIVERIES_TABLE"):
        assert key in env
        assert "Fn::ImportValue" in json.dumps(env[key])
    push_names = notification_names(config)
    assert (
        env["PUSH_INSTALLATIONS_BY_USER_INDEX"]
        == data_names(config).push_installations_by_user_index_name
    )
    assert env["FIREBASE_CREDENTIALS_SECRET_NAME"] == push_names.firebase_credentials_secret_name


def test_async_invoke_config_has_bounded_retries_and_a_failure_destination() -> None:
    _, body = _synth()
    function = next(r for r in body["Resources"].values() if r["Type"] == "AWS::Lambda::Function")
    assert function["Properties"]["Timeout"] == 20
    event_invoke_configs = [
        r for r in body["Resources"].values() if r["Type"] == "AWS::Lambda::EventInvokeConfig"
    ]
    assert len(event_invoke_configs) == 1
    config_props = event_invoke_configs[0]["Properties"]
    assert config_props["MaximumRetryAttempts"] == 2
    assert "OnFailure" in config_props["DestinationConfig"]

    queues = [r for r in body["Resources"].values() if r["Type"] == "AWS::SQS::Queue"]
    assert len(queues) == 1
    assert queues[0]["Properties"]["MessageRetentionPeriod"] == 4 * 86400


def test_iam_is_least_privilege_no_wildcards_and_scoped_secret_read() -> None:
    _, body = _synth()
    rendered = json.dumps(body)
    for wildcard in ('"dynamodb:*"', '"secretsmanager:*"', '"lambda:*"', '"Resource":"*"'):
        assert wildcard not in rendered

    policies = [r for r in body["Resources"].values() if r["Type"] == "AWS::IAM::Policy"]
    (policy,) = policies
    statements = policy["Properties"]["PolicyDocument"]["Statement"]

    secret_statements = [
        statement
        for statement in statements
        if statement["Action"] == "secretsmanager:GetSecretValue"
        or statement["Action"] == ["secretsmanager:GetSecretValue"]
    ]
    assert len(secret_statements) == 1
    resource = secret_statements[0]["Resource"]
    # Resolves to a single ARN (a Fn::Join token), not a list/wildcard --
    # scoped to exactly the one expected secret name.
    assert not isinstance(resource, list)
    config = EnvironmentConfig()
    push_names = notification_names(config)
    assert push_names.firebase_credentials_secret_name in json.dumps(resource)


def test_iam_grants_only_the_dynamodb_actions_actually_used() -> None:
    _, body = _synth()
    policies = [r for r in body["Resources"].values() if r["Type"] == "AWS::IAM::Policy"]
    (policy,) = policies
    statements = policy["Properties"]["PolicyDocument"]["Statement"]
    dynamodb_actions: set[str] = set()
    for statement in statements:
        actions = statement["Action"]
        actions = [actions] if isinstance(actions, str) else actions
        dynamodb_actions.update(a for a in actions if a.startswith("dynamodb:"))
    assert dynamodb_actions == {
        "dynamodb:PutItem",
        "dynamodb:GetItem",
        "dynamodb:UpdateItem",
        "dynamodb:Query",
        "dynamodb:BatchGetItem",
        "dynamodb:DeleteItem",
    }


def test_no_high_confidence_secrets_or_hardcoded_account_in_template() -> None:
    _, body = _synth()
    raw = json.dumps(body)
    assert "-----BEGIN" not in raw
    assert "AKIA" not in raw


def test_idempotency_lease_stays_correctly_calibrated_against_the_real_function_timeout() -> None:
    # Drift guard: lambdas/push_sender/idempotency.LEASE_SECONDS documents
    # its relationship to this stack's actual Lambda Timeout in prose --
    # this test ties the two together against the real synthesized value,
    # so a future change to one without the other fails loudly instead of
    # silently reintroducing the exact gap this was fixed for (a lease
    # that could be mistaken as still legitimately held by a function that
    # has already been killed by its own timeout).
    from lambdas.push_sender import idempotency

    _, body = _synth()
    function = next(r for r in body["Resources"].values() if r["Type"] == "AWS::Lambda::Function")
    real_timeout_seconds = function["Properties"]["Timeout"]

    assert real_timeout_seconds < idempotency.LEASE_SECONDS
    assert idempotency.LEASE_SECONDS - real_timeout_seconds >= 5


def test_stack_has_notifications_component_tag() -> None:
    stack, _ = _synth()
    tag_values = stack.tags.tag_values()
    assert tag_values["Component"] == "notifications"
    assert tag_values["Project"] == "InterBridge"
