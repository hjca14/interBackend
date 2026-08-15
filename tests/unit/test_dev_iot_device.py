from __future__ import annotations

import logging
import stat
from pathlib import Path
from typing import Any

import pytest

from tools.dev_iot_device import (
    DevIotDeviceTool,
    MissingCrtDependencyError,
    SafetyError,
    build_tool,
    discover_checkout_root,
    main,
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


def fake_checkout(tmp_path: Path) -> tuple[Path, Path]:
    checkout = tmp_path / "interBackend"
    module = checkout / "tools" / "dev_iot_device.py"
    module.parent.mkdir(parents=True)
    module.write_text("# test module\n", encoding="utf-8")
    (checkout / ".git").mkdir()
    for marker in ("CONTEXT.md", "README.md", "pyproject.toml"):
        (checkout / marker).write_text("test\n", encoding="utf-8")
    return checkout, module


@pytest.mark.parametrize("relative_cwd", [Path("."), Path("tools")])
def test_checkout_discovery_is_independent_of_current_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, relative_cwd: Path
) -> None:
    checkout, module = fake_checkout(tmp_path)
    monkeypatch.chdir(checkout / relative_cwd)
    assert discover_checkout_root(module) == checkout.resolve()


def test_discovered_root_rejects_another_checkout_subdirectory(tmp_path: Path) -> None:
    checkout, module = fake_checkout(tmp_path)
    checkout_root = discover_checkout_root(module)
    with pytest.raises(SafetyError, match="outside the Git checkout"):
        validate_output_dir(checkout / "certificates", checkout_root)
    assert (
        validate_output_dir(tmp_path / "external-vault", checkout_root)
        == (tmp_path / "external-vault").resolve()
    )


def test_checkout_discovery_without_git_root_refuses_safely(tmp_path: Path) -> None:
    checkout, module = fake_checkout(tmp_path)
    (checkout / ".git").rmdir()
    with pytest.raises(SafetyError, match="could not safely determine"):
        discover_checkout_root(module)


def test_checkout_discovery_with_missing_module_refuses_safely(tmp_path: Path) -> None:
    with pytest.raises(SafetyError, match="could not safely determine"):
        discover_checkout_root(tmp_path / "missing" / "dev_iot_device.py")


def test_symlink_cannot_bypass_checkout_guard(tmp_path: Path) -> None:
    checkout, _ = fake_checkout(tmp_path)
    credentials = checkout / "certificates"
    credentials.mkdir()
    symlink = tmp_path / "apparently-external"
    try:
        symlink.symlink_to(credentials, target_is_directory=True)
    except (NotImplementedError, OSError):
        pytest.skip("directory symlinks are not supported by this operating system")
    with pytest.raises(SafetyError, match="outside the Git checkout"):
        validate_output_dir(symlink, checkout)


def test_invalid_output_directory_makes_no_aws_calls(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    sts, iot = FakeSts(), FakeIot()
    subject = DevIotDeviceTool(sts, iot, checkout=checkout)
    with pytest.raises(SafetyError, match="outside the Git checkout"):
        subject.provision(
            DEVICE,
            "sa-east-1",
            checkout / "certificates",
            dry_run=True,
            confirmation=f"PROVISION {DEVICE}",
        )
    assert sts.calls == 0
    assert iot.calls == []


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


def test_build_tool_wraps_missing_crt_dependency_with_actionable_message(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A missing CRT extra must fail with one short, actionable line -- not a raw traceback.

    Regression test for the real Fase 1D.1 operational failure: the AWS CLI "login" credential
    provider needs ``botocore[crt]``, which is only installed when ``requirements-tools.txt`` is
    installed with its ``crt`` extra. This never uninstalls a real package; it mocks
    ``boto3.Session`` to raise the same ``MissingDependencyException`` boto3 itself raises.
    """
    from botocore.exceptions import MissingDependencyException

    class RaisingSession:
        def __init__(self, region_name: str) -> None:
            raise MissingDependencyException(msg="botocore[crt] is required")

    monkeypatch.setattr("boto3.Session", RaisingSession)
    with pytest.raises(MissingCrtDependencyError, match="requirements-tools.txt") as excinfo:
        build_tool("sa-east-1", tmp_path)
    assert "Missing Dependency" not in str(excinfo.value)


def test_build_tool_does_not_hide_other_real_aws_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class RaisingSession:
        def __init__(self, region_name: str) -> None:
            raise RuntimeError("some other real AWS/network failure")

    monkeypatch.setattr("boto3.Session", RaisingSession)
    with pytest.raises(RuntimeError, match="some other real AWS/network failure"):
        build_tool("sa-east-1", tmp_path)


def test_main_reports_missing_crt_dependency_without_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fake_build_tool(region: str, checkout: Path) -> DevIotDeviceTool:
        raise MissingCrtDependencyError(
            "Missing optional dependency for the AWS credential provider in use "
            "(e.g. the AWS CLI 'login' provider needs botocore[crt]). "
            "Run: python -m pip install -r requirements-tools.txt"
        )

    monkeypatch.setattr("tools.dev_iot_device.build_tool", fake_build_tool)
    exit_code = main(["verify", "--device-id", DEVICE, "--region", "sa-east-1"])
    captured = capsys.readouterr()
    assert exit_code == 3
    assert "requirements-tools.txt" in captured.err
    assert "Traceback" not in captured.err
    assert "Traceback" not in captured.out
