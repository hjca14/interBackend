from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import Any

import pytest

from tools import register_dev_device
from tools.register_dev_device import Registrar, RegistrationError

SUB = "11111111-1111-7111-6111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"
DEVICE = "ib-" + "a" * 32


class NotFound(Exception):
    pass


class Cancelled(Exception):
    pass


class Sts:
    arn = "arn:aws:sts::000000000000:assumed-role/dev-operator/session-sensitive"

    def get_caller_identity(self) -> dict[str, str]:
        return {"Account": "000000000000", "Arn": self.arn}


class Cfn:
    bad = False

    def describe_stacks(self, *, StackName: str) -> dict[str, Any]:
        values = (
            {
                "DevicesTableName": "wrong",
                "DeviceMembershipsTableName": "interbridge-dev-device-memberships",
            }
            if self.bad
            else {
                "DevicesTableName": "interbridge-dev-devices",
                "DeviceMembershipsTableName": "interbridge-dev-device-memberships",
            }
        )
        if StackName.endswith("ApiStack"):
            values = {"UserPoolId": "sa-east-1_pool"}
        return {
            "Stacks": [
                {
                    "StackStatus": "UPDATE_COMPLETE",
                    "Outputs": [{"OutputKey": k, "OutputValue": v} for k, v in values.items()],
                }
            ]
        }


class Cognito:
    users: list[dict[str, Any]] = [{"Attributes": [{"Name": "sub", "Value": SUB}]}]

    def list_users(self, **kwargs: Any) -> dict[str, Any]:
        self.request = kwargs
        return {"Users": self.users}


class Iot:
    exceptions = SimpleNamespace(ResourceNotFoundException=NotFound)
    missing = False
    thing_name = DEVICE
    thing_type = "interbridge-dev-device"
    groups = [{"groupName": "interbridge-dev-devices"}]
    principals = ["arn:aws:iot:sa-east-1:000000000000:cert/certificate"]
    certificate = {"certificateArn": principals[0], "status": "ACTIVE"}
    policies = [{"policyName": "interbridge-dev-device-policy"}]

    def describe_thing(self, **kwargs: Any) -> dict[str, Any]:
        if self.missing:
            raise NotFound
        return {"thingName": self.thing_name, "thingTypeName": self.thing_type}

    def list_thing_groups_for_thing(self, **kwargs: Any) -> dict[str, Any]:
        return {"thingGroups": self.groups}

    def list_thing_principals(self, **kwargs: Any) -> dict[str, Any]:
        return {"principals": self.principals}

    def describe_certificate(self, **kwargs: Any) -> dict[str, Any]:
        return {"certificateDescription": self.certificate}

    def list_attached_policies(self, **kwargs: Any) -> dict[str, Any]:
        return {"policies": self.policies}


class Ddb:
    exceptions = SimpleNamespace(TransactionCanceledException=Cancelled)

    def __init__(self) -> None:
        self.cancel = False
        self.writes: list[dict[str, Any]] = []
        self.existing: dict[str, dict[str, Any]] = {}

    def transact_write_items(self, **kwargs: Any) -> None:
        if self.cancel:
            raise Cancelled
        self.writes.append(kwargs)

    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        return (
            {"Item": self.existing.get(kwargs["TableName"])}
            if kwargs["TableName"] in self.existing
            else {}
        )


@pytest.fixture
def subject() -> tuple[Registrar, Sts, Cfn, Cognito, Iot, Ddb]:
    values = Sts(), Cfn(), Cognito(), Iot(), Ddb()
    return Registrar(*values), *values


def invoke(tool: Registrar, **overrides: Any) -> str:
    args = {
        "environment": "dev",
        "region": "sa-east-1",
        "sub": SUB,
        "device_id": DEVICE,
        "hardware_version": "1.0",
        "manufacturing_batch": "batch",
        "temporary_credentials": True,
        "dry_run": False,
        "confirmation": f"REGISTER DEV {DEVICE} OWNER {SUB}",
        "now": 100,
    }
    args.update(overrides)
    return tool.register(**args)


def test_dry_run_validates_but_never_writes(
    subject: tuple[Registrar, Sts, Cfn, Cognito, Iot, Ddb],
) -> None:
    tool, _, _, cognito, _, ddb = subject
    assert invoke(tool, dry_run=True).startswith("DRY RUN") and not ddb.writes
    assert cognito.request == {
        "UserPoolId": "sa-east-1_pool",
        "Filter": f'sub = "{SUB}"',
        "Limit": 2,
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"environment": "prod"},
        {"region": "us-east-1"},
        {"temporary_credentials": False},
        {"sub": ""},
        {"sub": "a" * 129},
        {"sub": "safe\nunsafe"},
        {"device_id": "bad"},
        {"confirmation": "wrong"},
    ],
)
def test_safety_inputs_are_rejected(
    subject: tuple[Registrar, Sts, Cfn, Cognito, Iot, Ddb], changes: dict[str, Any]
) -> None:
    with pytest.raises(RegistrationError):
        invoke(subject[0], **changes)
    assert not subject[-1].writes


@pytest.mark.parametrize(
    "arn", ["arn:aws:iam::000000000000:root", "arn:aws:iam::000000000000:user/permanent"]
)
def test_root_and_non_assumed_identity_rejected(
    subject: tuple[Registrar, Sts, Cfn, Cognito, Iot, Ddb], arn: str
) -> None:
    subject[1].arn = arn
    with pytest.raises(RegistrationError):
        invoke(subject[0])


def test_stack_target_mismatch_rejected(
    subject: tuple[Registrar, Sts, Cfn, Cognito, Iot, Ddb],
) -> None:
    subject[2].bad = True
    with pytest.raises(RegistrationError, match="outputs"):
        invoke(subject[0])


@pytest.mark.parametrize(
    "users",
    [
        [],
        [{"Attributes": [{"Name": "sub", "Value": OTHER}]}],
        [
            {"Attributes": [{"Name": "sub", "Value": SUB}]},
            {"Attributes": [{"Name": "sub", "Value": SUB}]},
        ],
    ],
)
def test_user_missing_divergent_or_ambiguous(
    subject: tuple[Registrar, Sts, Cfn, Cognito, Iot, Ddb], users: list[dict[str, Any]]
) -> None:
    subject[3].users = users
    with pytest.raises(RegistrationError):
        invoke(subject[0])


def test_observed_cognito_shape_is_preserved_exactly(
    subject: tuple[Registrar, Sts, Cfn, Cognito, Iot, Ddb],
) -> None:
    tool, _, _, cognito, _, ddb = subject
    assert invoke(tool).endswith("atomically")
    assert cognito.request["Filter"] == f'sub = "{SUB}"'
    assert ddb.writes[0]["TransactItems"][0]["Put"]["Item"]["owner_user_id"] == {"S": SUB}


@pytest.mark.parametrize(
    "field,value",
    [
        ("missing", True),
        ("thing_name", "different"),
        ("thing_type", "wrong"),
        ("groups", []),
        ("principals", []),
        ("certificate", {"certificateArn": "wrong", "status": "ACTIVE"}),
        ("policies", []),
    ],
)
def test_invalid_iot_bindings_rejected(
    subject: tuple[Registrar, Sts, Cfn, Cognito, Iot, Ddb], field: str, value: Any
) -> None:
    setattr(subject[4], field, value)
    with pytest.raises(RegistrationError):
        invoke(subject[0])


def test_atomic_transaction_uses_exact_dev_targets(
    subject: tuple[Registrar, Sts, Cfn, Cognito, Iot, Ddb],
) -> None:
    assert invoke(subject[0]).endswith("atomically")
    transaction = subject[-1].writes[0]["TransactItems"]
    assert [entry["Put"]["TableName"] for entry in transaction] == [
        "interbridge-dev-devices",
        "interbridge-dev-device-memberships",
    ]
    assert all(
        "attribute_not_exists" in entry["Put"]["ConditionExpression"] for entry in transaction
    )
    assert transaction[0]["Put"]["Item"]["owner_user_id"] == {"S": SUB}


def test_later_retry_is_idempotent_and_preserves_original_times(
    subject: tuple[Registrar, Sts, Cfn, Cognito, Iot, Ddb],
) -> None:
    tool, *_, ddb = subject
    invoke(tool, now=100)
    transaction = ddb.writes[0]["TransactItems"]
    ddb.existing = {entry["Put"]["TableName"]: entry["Put"]["Item"] for entry in transaction}
    ddb.cancel = True
    assert "identical semantic data" in invoke(tool, now=999)
    assert ddb.existing["interbridge-dev-devices"]["created_at"] == {"N": "100"}


@pytest.mark.parametrize(
    "table,field,value",
    [
        ("interbridge-dev-devices", "owner_user_id", OTHER),
        ("interbridge-dev-devices", "hardware_version", "2.0"),
        ("interbridge-dev-device-memberships", "status", "REMOVED"),
    ],
)
def test_transaction_conflicts_never_succeed(
    subject: tuple[Registrar, Sts, Cfn, Cognito, Iot, Ddb], table: str, field: str, value: str
) -> None:
    tool, *_, ddb = subject
    invoke(tool)
    transaction = ddb.writes[0]["TransactItems"]
    ddb.existing = {entry["Put"]["TableName"]: entry["Put"]["Item"] for entry in transaction}
    ddb.existing[table][field] = {"S": value}
    ddb.cancel = True
    with pytest.raises(RegistrationError, match="conflict"):
        invoke(tool, now=200)


def test_output_is_sanitized(
    subject: tuple[Registrar, Sts, Cfn, Cognito, Iot, Ddb], capsys: pytest.CaptureFixture[str]
) -> None:
    invoke(subject[0], dry_run=True)
    output = capsys.readouterr().out
    assert "dev-operator" in output
    for secret in (SUB, DEVICE, "000000000000", "session-sensitive"):
        assert secret not in output


def test_cli_builds_read_clients_and_handles_success(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class Session:
        def __init__(self, *, region_name: str) -> None:
            assert region_name == "sa-east-1"

        def get_credentials(self) -> Any:
            return SimpleNamespace(token="temporary")

        def client(self, name: str) -> str:
            return name

    monkeypatch.setitem(sys.modules, "boto3", SimpleNamespace(Session=Session))
    monkeypatch.setattr(Registrar, "register", lambda self, **kwargs: "ok")
    args = [
        "--environment",
        "dev",
        "--region",
        "sa-east-1",
        "--sub",
        SUB,
        "--device-id",
        DEVICE,
        "--hardware-version",
        "1",
        "--manufacturing-batch",
        "batch",
        "--dry-run",
    ]
    assert register_dev_device.main(args) == 0
    assert capsys.readouterr().out.strip() == "ok"


def test_cli_sanitizes_safety_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class Session:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def get_credentials(self) -> Any:
            return SimpleNamespace(token=None)

        def client(self, name: str) -> str:
            return name

    monkeypatch.setitem(sys.modules, "boto3", SimpleNamespace(Session=Session))
    args = [
        "--environment",
        "dev",
        "--region",
        "sa-east-1",
        "--sub",
        SUB,
        "--device-id",
        DEVICE,
        "--hardware-version",
        "1",
        "--manufacturing-batch",
        "batch",
        "--dry-run",
    ]
    assert register_dev_device.main(args) == 2
    assert "Refused safely" in capsys.readouterr().err


def test_cli_sanitizes_unexpected_aws_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sensitive = (
        "arn:aws:iam::123456789012:role/operator user@example.test bearer-token "
        "interbridge-dev-devices"
    )

    class Session:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def get_credentials(self) -> Any:
            return SimpleNamespace(token="temporary")

        def client(self, name: str) -> str:
            return name

    monkeypatch.setitem(sys.modules, "boto3", SimpleNamespace(Session=Session))
    monkeypatch.setattr(
        Registrar, "register", lambda self, **kwargs: (_ for _ in ()).throw(RuntimeError(sensitive))
    )
    args = [
        "--environment",
        "dev",
        "--region",
        "sa-east-1",
        "--sub",
        SUB,
        "--device-id",
        DEVICE,
        "--hardware-version",
        "1",
        "--manufacturing-batch",
        "batch",
        "--dry-run",
    ]
    assert register_dev_device.main(args) == 3
    output = capsys.readouterr()
    assert output.out == ""
    assert "Operational failure" in output.err
    for value in (
        "arn:aws",
        "123456789012",
        "user@example.test",
        "bearer-token",
        "interbridge-dev-devices",
    ):
        assert value not in output.err
