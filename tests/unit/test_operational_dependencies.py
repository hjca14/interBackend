"""Regression tests for the Fase 1D.1 operational-dependency fix.

The first real MQTT/mTLS smoke test failed operationally because
``tools/dev_iot_device.py`` needs the optional ``crt`` (awscrt) dependency for
newer boto3/botocore credential providers (e.g. the AWS CLI "login" provider),
and that dependency was not declared. These tests guard the fix without making
any AWS call or network request: they check the requirement file text, that
runtime/operational dependencies stay separate, and that boto3/botocore/awscrt
are importable and that importing the operational tool/simulator modules never
talks to AWS.
"""

from __future__ import annotations

import importlib.metadata
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
REQUIREMENTS_TOOLS = (REPO_ROOT / "requirements-tools.txt").read_text(encoding="utf-8")
REQUIREMENTS_PROD = (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8")
REQUIREMENTS_DEV = (REPO_ROOT / "requirements-dev.txt").read_text(encoding="utf-8")


def _requirement_lines(text: str) -> list[str]:
    return [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def test_requirements_tools_declares_the_official_boto3_crt_extra() -> None:
    lines = _requirement_lines(REQUIREMENTS_TOOLS)
    assert any(line.startswith("boto3[crt]") for line in lines)
    # Not an independently pinned awscrt: boto3[crt]/botocore[crt] already
    # select a version of awscrt compatible with the pinned boto3/botocore
    # release, and a second independent pin could drift out of sync with it
    # (the comments above may mention "awscrt" by name; only the actual
    # requirement lines matter here).
    assert not any(line.lower().startswith("awscrt") for line in lines)


def test_boto3_and_botocore_officially_recognize_the_crt_extra() -> None:
    # Guards against the "crt" extra being renamed/removed upstream, which
    # would silently turn requirements-tools.txt's pin into a no-op.
    for package in ("boto3", "botocore"):
        extras = importlib.metadata.metadata(package).get_all("Provides-Extra") or []
        assert "crt" in extras, f"{package} no longer declares a 'crt' extra"


def test_operational_dependencies_stay_out_of_production_and_dev_requirements() -> None:
    # requirements.txt is the Lambda/CDK runtime; requirements-dev.txt is
    # shared lint/test tooling. Neither should ever need boto3 at all, let
    # alone the CRT extra -- only local operational CLI tooling does.
    assert "boto3" not in REQUIREMENTS_PROD.lower()
    assert "boto3" not in REQUIREMENTS_DEV.lower()
    assert "crt" not in REQUIREMENTS_PROD.lower()
    assert "crt" not in REQUIREMENTS_DEV.lower()


def test_requirements_tools_still_layers_on_top_of_dev_requirements() -> None:
    assert "-r requirements-dev.txt" in REQUIREMENTS_TOOLS


def test_boto3_stack_is_importable_offline() -> None:
    # If the crt extra (or boto3/botocore themselves) were missing from the
    # active environment, these imports alone would raise ImportError -- no
    # AWS credentials or network access are needed to import them.
    import awscrt  # noqa: F401
    import boto3  # noqa: F401
    import botocore  # noqa: F401


def test_importing_operational_tool_and_simulator_modules_makes_no_aws_call() -> None:
    # Both modules build AWS/network clients lazily inside functions, not at
    # import time, specifically so tests (and CDK synth) stay offline.
    import mqtt_smoke.device_simulator
    import tools.dev_iot_device

    assert hasattr(tools.dev_iot_device, "build_tool")
    assert hasattr(mqtt_smoke.device_simulator, "DeviceSimulator")
