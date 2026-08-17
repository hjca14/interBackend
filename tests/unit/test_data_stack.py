"""Semantic tests for DataStack (Fase 1C).

Deliberately avoids snapshotting the whole template: assertions target the
specific tables, keys, indexes and safety properties this stack is
supposed to produce, so a future accidental change (e.g. turning on
provisioned throughput, or losing deletion protection) breaks a precise
assertion instead of a brittle full-template diff.
"""

from __future__ import annotations

import json
import re
from typing import Any

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template

from infrastructure.config.data import data_names
from infrastructure.config.environment import EnvironmentConfig
from infrastructure.stacks.data_stack import DataStack


def _synth() -> tuple[DataStack, Template, dict[str, Any]]:
    app = cdk.App()
    config = EnvironmentConfig()
    stack = DataStack(app, "TestDataStack", config=config)
    template = Template.from_stack(stack)
    body = template.to_json()
    return stack, template, body


def _tables(body: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {k: v for k, v in body["Resources"].items() if v["Type"] == "AWS::DynamoDB::Table"}


def _table_by_name(body: dict[str, Any], table_name: str) -> dict[str, Any]:
    matches = [t for t in _tables(body).values() if t["Properties"].get("TableName") == table_name]
    assert len(matches) == 1, f"expected exactly one table named {table_name!r}"
    return matches[0]


# ---------------------------------------------------------------------------
# Resource inventory
# ---------------------------------------------------------------------------


def test_exactly_five_dynamodb_tables_including_one_telemetry_table() -> None:
    _, template, _ = _synth()
    template.resource_count_is("AWS::DynamoDB::Table", 5)


def test_table_names_are_deterministic_and_match_requested_pattern() -> None:
    config = EnvironmentConfig()
    names = data_names(config)

    assert names.devices_table_name == "interbridge-dev-devices"
    assert names.setup_code_lookups_table_name == "interbridge-dev-setup-code-lookups"
    assert names.device_memberships_table_name == "interbridge-dev-device-memberships"
    assert names.claim_sessions_table_name == "interbridge-dev-claim-sessions"
    assert names.telemetry_table_name == "interbridge-dev-telemetry"

    _, _, body = _synth()
    table_names = {t["Properties"]["TableName"] for t in _tables(body).values()}
    assert table_names == {
        names.devices_table_name,
        names.setup_code_lookups_table_name,
        names.device_memberships_table_name,
        names.claim_sessions_table_name,
        names.telemetry_table_name,
    }


@pytest.mark.parametrize(
    "forbidden_type",
    [
        "AWS::Lambda::Function",
        "AWS::ApiGateway::RestApi",
        "AWS::ApiGatewayV2::Api",
        "AWS::Cognito::UserPool",
        "AWS::EC2::VPC",
        "AWS::EC2::NatGateway",
        "AWS::EC2::VPCEndpoint",
        "AWS::SecretsManager::Secret",
        "AWS::KMS::Key",
        "AWS::KMS::Alias",
        "AWS::IoT::Thing",
        "AWS::IoT::Certificate",
        "AWS::ApplicationAutoScaling::ScalableTarget",
    ],
)
def test_forbidden_resource_type_absent(forbidden_type: str) -> None:
    _, template, _ = _synth()
    template.resource_count_is(forbidden_type, 0)


def test_no_iam_resources_created_in_this_phase() -> None:
    # No Lambda/API consumer exists yet -- see docs/data-model.md for the
    # documented future minimum-privilege roles; none are created here.
    _, template, _ = _synth()
    template.resource_count_is("AWS::IAM::Role", 0)
    template.resource_count_is("AWS::IAM::Policy", 0)
    template.resource_count_is("AWS::IAM::ManagedPolicy", 0)


def test_only_dynamodb_tables_in_the_whole_stack() -> None:
    _, _, body = _synth()
    resource_types = sorted(res["Type"] for res in body["Resources"].values())
    assert resource_types == ["AWS::DynamoDB::Table"] * 5


# ---------------------------------------------------------------------------
# Shared safety properties (billing, encryption, PITR, deletion protection)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "table_name",
    [
        "interbridge-dev-devices",
        "interbridge-dev-setup-code-lookups",
        "interbridge-dev-device-memberships",
        "interbridge-dev-claim-sessions",
        "interbridge-dev-telemetry",
    ],
)
def test_every_table_is_on_demand_billing(table_name: str) -> None:
    _, _, body = _synth()
    table = _table_by_name(body, table_name)
    assert table["Properties"]["BillingMode"] == "PAY_PER_REQUEST"
    assert "ProvisionedThroughput" not in table["Properties"]


@pytest.mark.parametrize(
    "table_name",
    [
        "interbridge-dev-devices",
        "interbridge-dev-setup-code-lookups",
        "interbridge-dev-device-memberships",
        "interbridge-dev-claim-sessions",
        "interbridge-dev-telemetry",
    ],
)
def test_every_table_uses_aws_owned_encryption_key(table_name: str) -> None:
    _, _, body = _synth()
    table = _table_by_name(body, table_name)
    # TableEncryption.DEFAULT renders as SSESpecification.SSEEnabled=false:
    # DynamoDB's classic "AWS owned key" behavior, distinct from the
    # opt-in SSE feature that would let a KMSMasterKeyId (AWS-managed or
    # customer-managed) be specified. No KMSMasterKeyId is present.
    sse = table["Properties"].get("SSESpecification", {})
    assert sse.get("SSEEnabled") is False
    assert "KMSMasterKeyId" not in sse
    assert "SSESpecificationOverride" not in table["Properties"]


@pytest.mark.parametrize(
    "table_name",
    [
        "interbridge-dev-devices",
        "interbridge-dev-setup-code-lookups",
        "interbridge-dev-device-memberships",
        "interbridge-dev-claim-sessions",
        "interbridge-dev-telemetry",
    ],
)
def test_every_table_has_point_in_time_recovery_disabled(table_name: str) -> None:
    _, _, body = _synth()
    table = _table_by_name(body, table_name)
    pitr = table["Properties"]["PointInTimeRecoverySpecification"]
    assert pitr["PointInTimeRecoveryEnabled"] is False


@pytest.mark.parametrize(
    "table_name",
    [
        "interbridge-dev-devices",
        "interbridge-dev-setup-code-lookups",
        "interbridge-dev-device-memberships",
        "interbridge-dev-claim-sessions",
        "interbridge-dev-telemetry",
    ],
)
def test_every_table_has_deletion_protection_and_retain_policy(table_name: str) -> None:
    _, _, body = _synth()
    table = _table_by_name(body, table_name)
    assert table["Properties"]["DeletionProtectionEnabled"] is True
    assert table["DeletionPolicy"] == "Retain"
    assert table["UpdateReplacePolicy"] == "Retain"


@pytest.mark.parametrize(
    "table_name",
    [
        "interbridge-dev-devices",
        "interbridge-dev-setup-code-lookups",
        "interbridge-dev-device-memberships",
        "interbridge-dev-claim-sessions",
        "interbridge-dev-telemetry",
    ],
)
def test_no_table_has_a_stream(table_name: str) -> None:
    _, _, body = _synth()
    table = _table_by_name(body, table_name)
    assert "StreamSpecification" not in table["Properties"]


@pytest.mark.parametrize(
    "table_name",
    [
        "interbridge-dev-devices",
        "interbridge-dev-setup-code-lookups",
        "interbridge-dev-device-memberships",
        "interbridge-dev-claim-sessions",
        "interbridge-dev-telemetry",
    ],
)
def test_no_table_is_a_global_table_replica(table_name: str) -> None:
    _, _, body = _synth()
    table = _table_by_name(body, table_name)
    assert "Replicas" not in table["Properties"]


@pytest.mark.parametrize(
    "table_name",
    [
        "interbridge-dev-devices",
        "interbridge-dev-setup-code-lookups",
        "interbridge-dev-device-memberships",
        "interbridge-dev-claim-sessions",
        "interbridge-dev-telemetry",
    ],
)
def test_every_table_has_standard_and_component_tags(table_name: str) -> None:
    _, _, body = _synth()
    table = _table_by_name(body, table_name)
    tag_dict = {t["Key"]: t["Value"] for t in table["Properties"]["Tags"]}
    assert tag_dict == {
        "Project": "InterBridge",
        "Environment": "dev",
        "ManagedBy": "AWS-CDK",
        "Repository": "interBackend",
        "Component": "database",
    }


# ---------------------------------------------------------------------------
# Keys and indexes, per table
# ---------------------------------------------------------------------------


def test_devices_table_partition_key_is_device_id() -> None:
    _, _, body = _synth()
    table = _table_by_name(body, "interbridge-dev-devices")
    assert table["Properties"]["KeySchema"] == [{"AttributeName": "device_id", "KeyType": "HASH"}]
    assert "GlobalSecondaryIndexes" not in table["Properties"]
    assert "TimeToLiveSpecification" not in table["Properties"]


def test_setup_code_lookups_table_partition_key_is_digest() -> None:
    _, _, body = _synth()
    table = _table_by_name(body, "interbridge-dev-setup-code-lookups")
    assert table["Properties"]["KeySchema"] == [
        {"AttributeName": "setup_code_digest", "KeyType": "HASH"}
    ]
    assert "GlobalSecondaryIndexes" not in table["Properties"]
    assert "TimeToLiveSpecification" not in table["Properties"]


def test_device_memberships_table_keys_and_gsi() -> None:
    config = EnvironmentConfig()
    names = data_names(config)
    _, _, body = _synth()
    table = _table_by_name(body, "interbridge-dev-device-memberships")

    assert table["Properties"]["KeySchema"] == [
        {"AttributeName": "device_id", "KeyType": "HASH"},
        {"AttributeName": "user_id", "KeyType": "RANGE"},
    ]

    (gsi,) = table["Properties"]["GlobalSecondaryIndexes"]
    assert gsi["IndexName"] == names.memberships_by_user_index_name
    assert gsi["KeySchema"] == [
        {"AttributeName": "user_id", "KeyType": "HASH"},
        {"AttributeName": "device_id", "KeyType": "RANGE"},
    ]
    assert gsi["Projection"]["ProjectionType"] == "ALL"
    assert "TimeToLiveSpecification" not in table["Properties"]


def test_claim_sessions_table_keys_gsi_and_ttl() -> None:
    config = EnvironmentConfig()
    names = data_names(config)
    _, _, body = _synth()
    table = _table_by_name(body, "interbridge-dev-claim-sessions")

    assert table["Properties"]["KeySchema"] == [
        {"AttributeName": "claim_session_id", "KeyType": "HASH"}
    ]

    (gsi,) = table["Properties"]["GlobalSecondaryIndexes"]
    assert gsi["IndexName"] == names.claim_sessions_by_device_index_name
    assert gsi["KeySchema"] == [
        {"AttributeName": "device_id", "KeyType": "HASH"},
        {"AttributeName": "created_at", "KeyType": "RANGE"},
    ]
    assert gsi["Projection"]["ProjectionType"] == "ALL"

    # created_at must be typed as a Number so the GSI sorts numerically,
    # not lexicographically.
    attr_types = {
        a["AttributeName"]: a["AttributeType"] for a in table["Properties"]["AttributeDefinitions"]
    }
    assert attr_types["created_at"] == "N"

    # TTL is enabled only on this table, on the `ttl` attribute.
    ttl = table["Properties"]["TimeToLiveSpecification"]
    assert ttl == {"AttributeName": "ttl", "Enabled": True}


def test_telemetry_table_keys_ttl_and_no_indexes() -> None:
    _, _, body = _synth()
    table = _table_by_name(body, "interbridge-dev-telemetry")
    assert table["Properties"]["KeySchema"] == [
        {"AttributeName": "device_id", "KeyType": "HASH"},
        {"AttributeName": "record_key", "KeyType": "RANGE"},
    ]
    assert table["Properties"]["TimeToLiveSpecification"] == {
        "AttributeName": "expires_at",
        "Enabled": True,
    }
    assert "GlobalSecondaryIndexes" not in table["Properties"]
    assert "StreamSpecification" not in table["Properties"]


def test_ttl_is_not_enabled_on_any_other_table() -> None:
    _, _, body = _synth()
    for name in (
        "interbridge-dev-devices",
        "interbridge-dev-setup-code-lookups",
        "interbridge-dev-device-memberships",
    ):
        table = _table_by_name(body, name)
        assert "TimeToLiveSpecification" not in table["Properties"]


def test_stack_has_required_standard_tags_and_component() -> None:
    stack, template, _ = _synth()
    Template.from_stack(stack)  # ensure aspects resolved

    tag_values = stack.tags.tag_values()
    assert tag_values["Project"] == "InterBridge"
    assert tag_values["Environment"] == "dev"
    assert tag_values["ManagedBy"] == "AWS-CDK"
    assert tag_values["Repository"] == "interBackend"
    assert tag_values["Component"] == "database"


def test_no_account_id_or_secret_markers_in_template() -> None:
    _, _, body = _synth()
    raw = json.dumps(body)

    assert "-----BEGIN" not in raw
    assert "AKIA" not in raw
    assert "claim_code" not in raw.lower()
    assert "claim-code" not in raw.lower()
    assert not re.search(r"\b\d{12}\b", raw)
