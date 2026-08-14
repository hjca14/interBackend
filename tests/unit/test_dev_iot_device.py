from __future__ import annotations

import logging
import stat
from pathlib import Path
from typing import Any

import pytest

from tools.dev_iot_device import (
    DevIotDeviceTool,
    SafetyError,
    validate_device_id,
    validate_output_dir,
    validate_region,
)

DEVICE = "ib-" + "a" * 32
ARN = "arn:aws:iot:sa-east-1:000000000000:cert/cert-id"


class NotFound(Exception):
    pass


class FakeSts:
    def __init__(
        self, arn: str = "arn:aws:sts::000000000000:assumed-role/operator/session"
    ) -> None:
        self.arn = arn
        self.calls = 0

    def get_caller_identity(self) -> dict[str, str]:
        self.calls += 1
        return {"Account": "000000000000", "Arn": self.arn}


class FakeIot:
    class exceptions:
        ResourceNotFoundException = NotFound

    def __init__(
        self,
        *,
        exists: bool = False,
        principals: list[str] | None = None,
        policies: list[str] | None = None,
        fail_on: str | None = None,
    ) -> None:
        self.exists = exists
        self.principals = [ARN] if principals is None else principals
        self.policies = ["interbridge-dev-device-policy"] if policies is None else policies
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail_on = fail_on

    def __getattr__(self, name: str) -> Any:
        def call(**kwargs: Any) -> dict[str, Any]:
            self.calls.append((name, kwargs))
            if name == self.fail_on:
                raise RuntimeError("attachment failed")
            if name == "describe_thing":
                if not self.exists:
                    raise NotFound
                return {"thingName": DEVICE, "thingTypeName": "interbridge-dev-device"}
            if name == "list_thing_groups_for_thing":
                return {"thingGroups": [{"groupName": "interbridge-dev-devices"}]}
            if name == "list_thing_principals":
                return {"principals": self.principals}
            if name == "describe_certificate":
                return {"certificateDescription": {"certificateArn": ARN, "status": "ACTIVE"}}
            if name == "list_attached_policies":
                return {"policies": [{"policyName": item} for item in self.policies]}
            if name == "create_keys_and_certificate":
                return {
                    "certificateArn": ARN,
                    "certificateId": "cert-id",
                    "certificatePem": "CERTIFICATE-SENSITIVE",
                    "keyPair": {"PrivateKey": "PRIVATE-KEY-SENSITIVE"},
                }
            if name == "create_thing":
                self.exists = True
                return {}
            if name == "describe_endpoint":
                return {"endpointAddress": "example-ats.iot.sa-east-1.amazonaws.com"}
            return {}

        return call


def tool(
    tmp_path: Path,
    *,
    exists: bool = False,
    principals: list[str] | None = None,
    policies: list[str] | None = None,
) -> tuple[DevIotDeviceTool, FakeSts, FakeIot]:
    sts, iot = FakeSts(), FakeIot(exists=exists, principals=principals, policies=policies)
    return DevIotDeviceTool(sts, iot, checkout=tmp_path / "checkout"), sts, iot


@pytest.mark.parametrize("value", [DEVICE, "ib-" + "0" * 32])
def test_valid_device_id(value: str) -> None:
    assert validate_device_id(value) == value


@pytest.mark.parametrize("value", ["", "ib-short", "ib-" + "A" * 32, "x-" + "a" * 32])
def test_invalid_device_id(value: str) -> None:
    with pytest.raises(SafetyError):
        validate_device_id(value)


def test_region_and_external_directory_guards(tmp_path: Path) -> None:
    assert validate_region("sa-east-1") == "sa-east-1"
    with pytest.raises(SafetyError):
        validate_region("us-east-1")
    checkout = tmp_path / "repo"
    assert validate_output_dir(tmp_path / "vault", checkout) == (tmp_path / "vault").resolve()
    with pytest.raises(SafetyError):
        validate_output_dir(checkout / "secrets", checkout)


def test_dry_run_verifies_identity_confirms_and_makes_no_iot_calls(tmp_path: Path) -> None:
    subject, sts, iot = tool(tmp_path)
    subject.provision(
        DEVICE, "sa-east-1", tmp_path / "vault", dry_run=True, confirmation=f"PROVISION {DEVICE}"
    )
    assert sts.calls == 1
    assert iot.calls == []


def test_confirmation_and_root_user_are_refused(tmp_path: Path) -> None:
    subject, _, _ = tool(tmp_path)
    with pytest.raises(SafetyError, match="confirmation"):
        subject.provision(DEVICE, "sa-east-1", tmp_path / "vault", dry_run=True, confirmation="yes")
    subject.sts = FakeSts("arn:aws:iam::000000000000:root")
    with pytest.raises(SafetyError, match="root"):
        subject.provision(
            DEVICE,
            "sa-east-1",
            tmp_path / "vault",
            dry_run=True,
            confirmation=f"PROVISION {DEVICE}",
        )


def test_provision_order_files_permissions_and_no_secret_logs(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    subject, _, iot = tool(tmp_path)
    output = tmp_path / "vault"
    with caplog.at_level(logging.DEBUG):
        subject.provision(
            DEVICE, "sa-east-1", output, dry_run=False, confirmation=f"PROVISION {DEVICE}"
        )
    assert [name for name, _ in iot.calls] == [
        "describe_thing",
        "create_thing",
        "add_thing_to_thing_group",
        "create_keys_and_certificate",
        "attach_thing_principal",
        "attach_policy",
        "describe_thing",
        "list_thing_groups_for_thing",
        "list_thing_principals",
        "describe_certificate",
        "list_attached_policies",
        "describe_endpoint",
    ]
    assert {p.name for p in output.iterdir()} == {
        "device-certificate.pem.crt",
        "private.pem.key",
        "endpoint.txt",
        "device-metadata.json",
    }
    assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in output.iterdir())
    assert "PRIVATE-KEY-SENSITIVE" not in caplog.text
    assert "CERTIFICATE-SENSITIVE" not in caplog.text
    assert all("dynamodb" not in name for name, _ in iot.calls)


def test_existing_thing_or_nonempty_output_is_never_reused(tmp_path: Path) -> None:
    subject, _, _ = tool(tmp_path, exists=True)
    with pytest.raises(SafetyError, match="Thing already"):
        subject.provision(
            DEVICE,
            "sa-east-1",
            tmp_path / "vault",
            dry_run=False,
            confirmation=f"PROVISION {DEVICE}",
        )
    subject, _, _ = tool(tmp_path)
    output = tmp_path / "other"
    output.mkdir()
    (output / "private.pem.key").write_text("old")
    with pytest.raises(SafetyError, match="empty"):
        subject.provision(
            DEVICE, "sa-east-1", output, dry_run=False, confirmation=f"PROVISION {DEVICE}"
        )


def test_verify_is_repeatable_and_uses_data_ats_endpoint(tmp_path: Path) -> None:
    subject, _, iot = tool(tmp_path, exists=True)
    subject.verify(DEVICE, "sa-east-1")
    subject.verify(DEVICE, "sa-east-1")
    endpoint_calls = [kwargs for name, kwargs in iot.calls if name == "describe_endpoint"]
    assert endpoint_calls == [{"endpointType": "iot:Data-ATS"}] * 2


def test_cleanup_exact_safe_order(tmp_path: Path) -> None:
    subject, _, iot = tool(tmp_path, exists=True)
    subject.cleanup(DEVICE, "sa-east-1", dry_run=False, confirmation=f"CLEANUP {DEVICE}")
    mutations = [
        name
        for name, _ in iot.calls
        if name
        in {
            "detach_policy",
            "detach_thing_principal",
            "update_certificate",
            "delete_certificate",
            "remove_thing_from_thing_group",
            "delete_thing",
        }
    ]
    assert mutations == [
        "detach_policy",
        "detach_thing_principal",
        "update_certificate",
        "delete_certificate",
        "remove_thing_from_thing_group",
        "delete_thing",
    ]


@pytest.mark.parametrize(
    ("principals", "policies"), [([ARN, ARN + "2"], None), (None, ["unexpected-policy"])]
)
def test_cleanup_stops_on_unexpected_bindings(
    tmp_path: Path, principals: list[str] | None, policies: list[str] | None
) -> None:
    subject, _, iot = tool(tmp_path, exists=True, principals=principals, policies=policies)
    with pytest.raises(SafetyError, match="unexpected|exactly one"):
        subject.cleanup(DEVICE, "sa-east-1", dry_run=False, confirmation=f"CLEANUP {DEVICE}")
    assert not any(
        name.startswith(("detach", "delete", "update", "remove")) for name, _ in iot.calls
    )


def test_partial_failure_does_not_hide_or_log_secret(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    sts, iot = FakeSts(), FakeIot(fail_on="attach_policy")
    subject = DevIotDeviceTool(sts, iot, checkout=tmp_path / "checkout")
    with pytest.raises(RuntimeError, match="attachment failed"), caplog.at_level(logging.DEBUG):
        subject.provision(
            DEVICE,
            "sa-east-1",
            tmp_path / "vault",
            dry_run=False,
            confirmation=f"PROVISION {DEVICE}",
        )
    assert "PRIVATE-KEY-SENSITIVE" not in caplog.text
