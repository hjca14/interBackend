"""Lightweight documentation regression tests for the Fase 1D.1 real smoke test.

Not full-document snapshots -- each test checks a specific, meaningful
marker (a section header, a required/forbidden fragment, a real-identifier
pattern) so future rewording doesn't break these, but losing the fix's
substance would. See ``tests/unit/test_onboarding_docs.py`` for the same
convention applied to the Fase 1B.2 docs.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
MQTT_SMOKE_MD = (REPO_ROOT / "docs" / "mqtt-smoke-test.md").read_text(encoding="utf-8")
PHASE_1D_MD = (REPO_ROOT / "docs" / "phase-1d-dev-device.md").read_text(encoding="utf-8")
CONTEXT_MD = (REPO_ROOT / "CONTEXT.md").read_text(encoding="utf-8")
README_MD = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
PHASES_MD = (REPO_ROOT / "docs" / "phases.md").read_text(encoding="utf-8")

DOCS_WITH_FASE_1D_STATE = {
    "docs/mqtt-smoke-test.md": MQTT_SMOKE_MD,
    "docs/phase-1d-dev-device.md": PHASE_1D_MD,
    "CONTEXT.md": CONTEXT_MD,
    "README.md": README_MD,
    "docs/phases.md": PHASES_MD,
}

# Real-identifier patterns that must never appear in documentation: a
# 12-digit AWS account ID, an ARN, a concrete (non-placeholder) DEV
# device_id, and a PEM block header.
REAL_IDENTIFIER_PATTERNS = [
    re.compile(r"\b\d{12}\b"),
    re.compile(r"\barn:aws:"),
    re.compile(r"\bib-[0-9a-f]{32}\b"),
    re.compile(r"-----BEGIN "),
]


def _powershell_publish_block(text: str) -> str:
    match = re.search(r"```powershell\n(.*?)\n```", text, re.S)
    assert match, "expected a ```powershell code block in docs/mqtt-smoke-test.md"
    return match.group(1)


def _publish_invocation(block: str) -> str:
    # Isolates the actual `aws iot-data publish ...` command from the block's
    # explanatory comment lines (which legitimately name --cli-binary-format
    # and "retained" while explaining what *not* to do).
    match = re.search(r"^aws iot-data publish.*?--payload \$payloadBase64", block, re.S | re.M)
    assert match, "expected an `aws iot-data publish` invocation in the PowerShell block"
    return match.group(0)


def test_no_real_identifiers_in_fase_1d_documentation() -> None:
    for name, text in DOCS_WITH_FASE_1D_STATE.items():
        for pattern in REAL_IDENTIFIER_PATTERNS:
            assert not pattern.search(text), f"{name} contains a real-identifier-shaped value"


def test_powershell_section_exists_and_explains_the_quoting_failure() -> None:
    assert "## Send one safe command from Windows PowerShell (AWS CLI)" in MQTT_SMOKE_MD
    assert "quotes stripped or altered by PowerShell" in MQTT_SMOKE_MD
    assert "rejected" in MQTT_SMOKE_MD.lower()


def test_powershell_example_generates_command_id_with_csprng_and_epoch_times() -> None:
    block = _powershell_publish_block(MQTT_SMOKE_MD)
    assert "secrets.token_hex(16)" in block
    assert "ToUnixTimeSeconds()" in block
    assert "$expiresAt" in block and "$issuedAt" in block


def test_powershell_example_uses_base64_not_raw_binary_format_flag() -> None:
    block = _powershell_publish_block(MQTT_SMOKE_MD)
    assert "ConvertTo-Json -Compress" in block
    assert "[Convert]::ToBase64String" in block
    invocation = _publish_invocation(block)
    assert "--payload $payloadBase64" in invocation
    # The whole point of the fix: the actual publish invocation must not use
    # --cli-binary-format raw-in-base64-out (that flag re-encodes the blob a
    # second time, which is wrong for an already-Base64 payload). Comments
    # in the surrounding block may still name that flag while explaining why
    # not to use it -- only the invocation itself must be clean.
    assert "--cli-binary-format" not in invocation
    assert "raw-in-base64-out" not in invocation


def test_powershell_example_uses_correct_topic_qos_and_not_retained() -> None:
    block = _powershell_publish_block(MQTT_SMOKE_MD)
    invocation = _publish_invocation(block)
    assert '--topic "interbridge/$deviceId/commands"' in invocation
    assert "--qos 1" in invocation
    assert "--retain" not in invocation


def test_documentation_distinguishes_simulator_validated_from_esp32_pending() -> None:
    # docs/mqtt-smoke-test.md and docs/phase-1d-dev-device.md are in English
    # ("simulator"); the others are in Portuguese ("simulado"/"simulador").
    for name, text in DOCS_WITH_FASE_1D_STATE.items():
        assert "ESP32" in text, f"{name} must say the ESP32-C3 firmware is still untested"
        lowered = text.lower()
        assert "simulad" in lowered or "simulator" in lowered, (
            f"{name} must credit the computer simulator, not real hardware"
        )


def test_fase_1d_is_not_marked_fully_complete_anywhere() -> None:
    # The task's explicit constraint: never mark the whole Fase 1D as
    # "concluída" while the ESP32-C3 firmware itself remains untested. Checked
    # sentence-by-sentence (not with an unbounded ".*") so an unrelated
    # "concluída" elsewhere in the same document (e.g. Fase 1C) can't produce
    # a false positive by matching across sentence boundaries.
    for name, text in DOCS_WITH_FASE_1D_STATE.items():
        # Split on markdown headers first: a heading has no trailing period,
        # so without this a preceding section's "...concluídas)" could merge
        # with the next "### Fase 1D..." heading into one false "sentence".
        sections = re.split(r"\n(?=#)", text)
        sentences = [
            sentence
            for section in sections
            for sentence in re.split(r"(?<=[.!?])\s+", re.sub(r"\s+", " ", section))
        ]
        for sentence in sentences:
            if "Fase 1D" not in sentence:
                continue
            if not re.search(r"conclu[ií]da|concluir", sentence, re.IGNORECASE):
                continue
            lowered = sentence.lower()
            negated = any(
                marker in lowered
                for marker in ("não", "not ", "ainda", "pendente", "aberta", "sem ")
            )
            assert negated, (
                f"{name}: sentence marks Fase 1D complete without negation: {sentence!r}"
            )
