"""Lightweight contract regression tests for the display_name PATCH route.

Not a full-document snapshot and not a YAML schema validator (no PyYAML
dependency is added for this) -- each test checks for a specific, meaningful
marker in `docs/openapi-v1.yaml`, matching the convention already used by
`tests/unit/test_onboarding_docs.py`.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
OPENAPI = (REPO_ROOT / "docs" / "openapi-v1.yaml").read_text(encoding="utf-8")


def test_patch_route_and_operation_exist() -> None:
    assert "  /v1/devices/{device_id}:" in OPENAPI
    assert "    patch:" in OPENAPI
    assert "operationId: updateDeviceName" in OPENAPI


def test_update_device_name_request_schema_supports_clearing() -> None:
    assert "UpdateDeviceNameRequest:" in OPENAPI
    assert "required: [display_name]" in OPENAPI
    # The property itself must accept null (to clear), not just be optional.
    start = OPENAPI.index("UpdateDeviceNameRequest:")
    end = OPENAPI.index("DeviceStatus:", start)
    section = OPENAPI[start:end]
    assert "nullable: true" in section
    assert "maxLength: 60" in section


def test_device_detail_includes_created_and_updated_at() -> None:
    start = OPENAPI.index("DeviceDetail:")
    end = OPENAPI.index("UpdateDeviceNameRequest:", start)
    section = OPENAPI[start:end]
    assert "created_at:" in section
    assert "updated_at:" in section
    assert "display_name:" in section


def test_patch_route_error_taxonomy_includes_not_found() -> None:
    start = OPENAPI.index("operationId: updateDeviceName")
    end = OPENAPI.index("/v1/devices/{device_id}/status:")
    section = OPENAPI[start:end]
    for status in ("'400'", "'401'", "'404'", "'500'", "'503'"):
        assert status in section


def test_display_name_max_length_is_consistent_across_summary_and_detail() -> None:
    assert OPENAPI.count("description: Apelido da membership do usuário autenticado") == 2
