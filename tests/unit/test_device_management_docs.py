"""Lightweight documentation regression tests for device management (display_name).

Not full-document snapshots -- see `tests/unit/test_onboarding_docs.py` for the
same convention. Each test checks a specific, meaningful marker so a docs
rewrite that preserves meaning won't break these, but losing the substance
(the no-room-field product decision, the OWNER-only rule, the app-owned
fallback label) will.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text)


CONTEXT_MD = (REPO_ROOT / "CONTEXT.md").read_text(encoding="utf-8")
README_MD = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
PHASES_MD = (REPO_ROOT / "docs" / "phases.md").read_text(encoding="utf-8")
ARCHITECTURE_MD = (REPO_ROOT / "docs" / "phase-2-architecture.md").read_text(encoding="utf-8")
DATA_MODEL_MD = (REPO_ROOT / "docs" / "data-model.md").read_text(encoding="utf-8")

DOCS = {
    "CONTEXT.md": CONTEXT_MD,
    "README.md": README_MD,
    "docs/phases.md": PHASES_MD,
    "docs/phase-2-architecture.md": ARCHITECTURE_MD,
    "docs/data-model.md": DATA_MODEL_MD,
}


def test_every_doc_mentions_display_name() -> None:
    for name, text in DOCS.items():
        assert "display_name" in text, f"{name} does not mention display_name"


def test_no_room_or_location_field_was_added() -> None:
    forbidden = ("room_name", "location_name", "device_room", "device_location")
    for name, text in DOCS.items():
        lowered = text.lower()
        for term in forbidden:
            assert term not in lowered, f"{name} unexpectedly mentions {term!r}"


def test_architecture_doc_states_owner_only_for_now() -> None:
    normalized = _normalize(ARCHITECTURE_MD)
    assert "somente `OWNER` pode alterar" in normalized
    assert "403 ACCESS_DENIED" in normalized


def test_architecture_doc_explains_fallback_is_the_apps_responsibility() -> None:
    normalized = _normalize(ARCHITECTURE_MD)
    assert "InterBridge" in normalized
    assert "nunca persistido pelo backend" in normalized


def test_data_model_doc_documents_backward_compatible_legacy_items() -> None:
    normalized = _normalize(DATA_MODEL_MD)
    assert "display_name" in normalized
    assert "sem qualquer migração" in normalized or "sem migração" in normalized


def test_readme_and_context_do_not_claim_full_phase_2_complete() -> None:
    for name, text in (("README.md", README_MD), ("CONTEXT.md", CONTEXT_MD)):
        assert "não implantad" in text.lower() or "nao implantad" in text.lower(), (
            f"{name} should still say the API was not deployed"
        )
