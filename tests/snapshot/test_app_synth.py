"""End-to-end synth of the real app.py entry point.

These tests run ``python app.py`` in a subprocess with a clean environment
(no AWS credentials, no CDK_DEFAULT_ACCOUNT/REGION) to prove that synthesis
never depends on AWS access -- the same guarantee CI relies on.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

EXPECTED_STACKS = {
    "InterBridge-Dev-DataStack": "database",
    "InterBridge-Dev-IoTStack": "iot",
    "InterBridge-Dev-ApiStack": "api",
    "InterBridge-Dev-ObservabilityStack": "monitoring",
}

FORBIDDEN_RESOURCE_TYPES = (
    "AWS::EC2::VPC",
    "AWS::EC2::NatGateway",
    "AWS::IoT::Certificate",
    "AWS::IoT::Thing",
    "AWS::RDS::DBInstance",
)

ACCOUNT_ID_PATTERN = re.compile(r"\b\d{12}\b")


def _run_app_synth(tmp_path: Path) -> Path:
    outdir = tmp_path / "cdk.out"
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("AWS_") and key not in {"CDK_DEFAULT_ACCOUNT", "CDK_DEFAULT_REGION"}
    }
    env["CDK_OUTDIR"] = str(outdir)

    result = subprocess.run(
        [sys.executable, "app.py"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, (
        f"app.py failed without AWS credentials.\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    return outdir


def test_app_synthesizes_all_stacks_without_aws_credentials(tmp_path: Path) -> None:
    outdir = _run_app_synth(tmp_path)
    manifest = json.loads((outdir / "manifest.json").read_text(encoding="utf-8"))

    for stack_name, component in EXPECTED_STACKS.items():
        artifact = manifest["artifacts"][stack_name]
        assert artifact["environment"] == "aws://unknown-account/sa-east-1"
        assert artifact["properties"]["tags"]["Component"] == component
        assert artifact["properties"]["tags"]["Project"] == "InterBridge"
        assert artifact["properties"]["tags"]["Environment"] == "dev"

        template_path = outdir / f"{stack_name}.template.json"
        assert template_path.exists()


def test_synthesized_templates_have_no_forbidden_resources(tmp_path: Path) -> None:
    outdir = _run_app_synth(tmp_path)

    for stack_name in EXPECTED_STACKS:
        body = json.loads((outdir / f"{stack_name}.template.json").read_text(encoding="utf-8"))
        resources = body.get("Resources", {})
        resource_types = {res["Type"] for res in resources.values()}
        for forbidden in FORBIDDEN_RESOURCE_TYPES:
            assert forbidden not in resource_types


def test_synthesized_templates_contain_no_secrets_or_hardcoded_account(tmp_path: Path) -> None:
    outdir = _run_app_synth(tmp_path)

    for stack_name in EXPECTED_STACKS:
        raw = (outdir / f"{stack_name}.template.json").read_text(encoding="utf-8")
        assert "-----BEGIN" not in raw
        assert "AKIA" not in raw
        assert "claim_code" not in raw.lower()
        assert "claim-code" not in raw.lower()
        # The only 12-digit-number-shaped values allowed in a template are
        # CloudFormation pseudo-parameter references (e.g. "${AWS::AccountId}"),
        # never a literal account id.
        assert not ACCOUNT_ID_PATTERN.search(raw)
