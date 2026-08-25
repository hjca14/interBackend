"""Regression tests for the exact ``lambdas`` deployment asset boundary."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from domain.ownership.display_name import validate_display_name as validate_domain
from lambdas.device_api.display_name import MAX_DISPLAY_NAME_LENGTH
from lambdas.device_api.display_name import validate_display_name as validate_runtime

REPO_ROOT = Path(__file__).resolve().parents[2]
LAMBDAS_ASSET = REPO_ROOT / "lambdas"


@pytest.mark.parametrize(
    "value,expected",
    [
        ("  Casa da Vovó 🏠  ", "Casa da Vovó 🏠"),
        ("x" * 60, "x" * 60),
    ],
)
def test_runtime_and_domain_accept_the_same_names(value: str, expected: str) -> None:
    assert MAX_DISPLAY_NAME_LENGTH == 60
    assert validate_runtime(value) == validate_domain(value) == expected


@pytest.mark.parametrize("value", ["", "   ", "x" * 61])
def test_runtime_and_domain_reject_the_same_names(value: str) -> None:
    for validator in (validate_runtime, validate_domain):
        with pytest.raises(ValueError):
            validator(value)


def test_handler_imports_with_only_the_deployed_lambdas_asset_on_pythonpath() -> None:
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(LAMBDAS_ASSET),
        "PYTHONIOENCODING": "utf-8",
    }
    result = subprocess.run(
        [
            sys.executable,
            "-S",
            "-c",
            (
                "import pathlib; import device_api.handler as handler; "
                "root=pathlib.Path.cwd().resolve(); "
                "path=pathlib.Path(handler.__file__).resolve(); "
                "assert path.is_relative_to(root), (path, root); "
                "assert handler.validate_display_name('  Olá 🏠  ') == 'Olá 🏠'"
            ),
        ],
        cwd=LAMBDAS_ASSET,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
