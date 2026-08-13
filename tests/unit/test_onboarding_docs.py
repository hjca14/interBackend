"""Lightweight documentation regression tests for the Fase 1B.2 BLE-first
onboarding architecture.

These are *not* full-text snapshots (the task and general project
convention explicitly avoid brittle whole-document comparisons). Each test
checks for a specific, meaningful marker -- a distinct section header, a
short defining sentence, or a file's existence -- so a docs rewrite that
preserves meaning won't break these, but accidentally deleting/renaming a
concept, or merging two distinct concepts back into one, will.
"""

from __future__ import annotations

import re
from pathlib import Path


def _normalize(text: str) -> str:
    """Collapse whitespace (including markdown line-wrapping) to single spaces.

    Multi-word phrases in the documents below are hand-wrapped at ~80
    columns, so a raw substring check would be brittle against a line break
    landing mid-phrase. Checks for multi-word phrases run against this
    normalized text instead of the raw file content.
    """
    return re.sub(r"\s+", " ", text)


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CONTEXT_MD = (REPO_ROOT / "CONTEXT.md").read_text(encoding="utf-8")
CONTEXT_MD_NORM = _normalize(CONTEXT_MD)
PHASES_MD = (REPO_ROOT / "docs" / "phases.md").read_text(encoding="utf-8")
PHASES_MD_NORM = _normalize(PHASES_MD)
ADR_PATH = REPO_ROOT / "docs" / "adr" / "0001-ble-first-onboarding.md"


def test_context_defines_setup_code_as_its_own_section() -> None:
    assert "#### `setup_code`" in CONTEXT_MD
    assert "12 dígitos numéricos aleatórios" in CONTEXT_MD_NORM


def test_context_defines_claim_session_as_its_own_section() -> None:
    assert "#### `claim_session`" in CONTEXT_MD


def test_context_defines_fleet_provisioning_temporary_claim() -> None:
    assert "Fleet Provisioning temporary claim" in CONTEXT_MD_NORM
    assert "AWS IoT Fleet Provisioning by Trusted User" in CONTEXT_MD_NORM


def test_context_terms_are_not_conflated() -> None:
    # Each term has its own header, distinct from the other two -- guards
    # against a future edit collapsing them back into a single concept
    # (the exact ambiguity this ADR was written to eliminate).
    headers = [
        "#### `setup_code`",
        "#### `claim_session`",
        "#### Fleet Provisioning temporary claim",
    ]
    for header in headers:
        assert CONTEXT_MD.count(header) == 1, f"expected exactly one {header!r} section"


def test_context_describes_ble_as_primary_flow() -> None:
    assert "PRIMÁRIO:   descoberta e contato físico por BLE" in CONTEXT_MD


def test_context_does_not_describe_qr_as_mandatory_in_new_flow() -> None:
    assert "QR **não é obrigatório**" in CONTEXT_MD


def test_context_registers_isattached_hardening() -> None:
    assert "iot:Connection.Thing.IsAttached" in CONTEXT_MD
    assert '"true"' in CONTEXT_MD


def test_context_flags_official_protocol_not_yet_updated() -> None:
    # The live interBridge protocol doc still uses claim_code/old QR --
    # CONTEXT.md must say so explicitly rather than implying the new
    # terminology is already a ratified cross-repo contract.
    assert "ainda não existe" in CONTEXT_MD or "ainda não foi atualizado" in CONTEXT_MD


def test_adr_0001_exists_and_is_accepted() -> None:
    assert ADR_PATH.is_file()
    adr_text = ADR_PATH.read_text(encoding="utf-8")
    assert "**Status:** Accepted" in adr_text
    assert "## Alternativas consideradas" in adr_text
    assert "## Decisões ainda abertas" in adr_text


def test_phases_doc_reflects_1b_split() -> None:
    assert "## Fase 1B.1" in PHASES_MD
    assert "## Fase 1B.2" in PHASES_MD
    assert "## Fase 1B.3" in PHASES_MD


def test_phases_doc_clarifies_1b2_is_architecture_not_working_ble() -> None:
    assert "não BLE funcional" in PHASES_MD_NORM
