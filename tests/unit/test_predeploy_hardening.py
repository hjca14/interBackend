from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def test_current_stack_dependency_api_is_used() -> None:
    app_source = (REPO_ROOT / "app.py").read_text(encoding="utf-8")
    assert ".add_stack_dependency(" in app_source
    assert ".add_dependency(" not in app_source


def test_cross_stack_references_are_explicitly_strong() -> None:
    config = json.loads((REPO_ROOT / "cdk.json").read_text(encoding="utf-8"))
    assert config["context"]["@aws-cdk/core:defaultCrossStackReferences"] == "strong"
