from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest
import yaml
from jsonschema.exceptions import ValidationError
from openapi_schema_validator import OAS30Validator
from openapi_spec_validator import validate_spec

OPENAPI_PATH = Path(__file__).resolve().parents[2] / "docs" / "openapi-v1.yaml"


@pytest.fixture(scope="module")
def specification() -> dict[str, object]:
    document = yaml.safe_load(OPENAPI_PATH.read_text(encoding="utf-8"))
    validate_spec(document)
    return document


def test_complete_response_and_partial_patch_are_structurally_distinct(
    specification: dict[str, object],
) -> None:
    schemas = specification["components"]["schemas"]  # type: ignore[index]
    response = schemas["NotificationPreferences"]["example"]
    response_schema = deepcopy(schemas["NotificationPreferences"])
    response_schema["properties"]["quiet_schedule"] = schemas["QuietSchedule"]
    OAS30Validator(response_schema).validate(response)
    OAS30Validator(schemas["QuietSchedulePatch"]).validate({"behavior": "BLOCK_ALL"})
    OAS30Validator(schemas["NotificationPreferencesPatch"]).validate(
        {"notifications_enabled": False}
    )
    for forbidden in ({"version": 1}, {"updated_at": None}):
        with pytest.raises(ValidationError):
            OAS30Validator(schemas["NotificationPreferencesPatch"]).validate(forbidden)

    examples = specification["paths"][  # type: ignore[index]
        "/v1/devices/{device_id}/notification-preferences"
    ]["patch"]["requestBody"]["content"]["application/json"]["examples"]
    for example in examples.values():
        OAS30Validator(schemas["QuietSchedulePatch"]).validate(example["value"]["quiet_schedule"])


def test_complete_schedules_require_every_field(specification: dict[str, object]) -> None:
    schemas = specification["components"]["schemas"]  # type: ignore[index]
    assert set(schemas["QuietSchedule"]["required"]) == {
        "enabled",
        "timezone",
        "days",
        "start_time",
        "end_time",
        "behavior",
    }
    assert "required" not in schemas["QuietSchedulePatch"]
    assert schemas["NotificationPreferences"]["additionalProperties"] is False
    assert schemas["NotificationPreferencesPatch"]["additionalProperties"] is False


def test_only_new_behavior_enum_is_published(specification: dict[str, object]) -> None:
    schemas = specification["components"]["schemas"]  # type: ignore[index]
    expected = ["NOTIFICATION_ONLY", "BLOCK_ALL"]
    assert schemas["QuietSchedule"]["properties"]["behavior"]["enum"] == expected
    assert schemas["QuietSchedulePatch"]["properties"]["behavior"]["enum"] == expected
    published_contract = OPENAPI_PATH.read_text(encoding="utf-8")
    assert "SILENT" not in published_contract
    assert "enum: [SILENT, BLOCK]" not in published_contract


def test_patch_documents_conflict_and_payload_limit(specification: dict[str, object]) -> None:
    operation = specification["paths"][  # type: ignore[index]
        "/v1/devices/{device_id}/notification-preferences"
    ]["patch"]
    assert {"409", "413"} <= set(operation["responses"])
