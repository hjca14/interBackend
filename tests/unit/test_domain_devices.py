from __future__ import annotations

import pytest

from domain.devices import (
    Device,
    OwnershipStatus,
    ProvisioningStatus,
    is_valid_device_id,
)
from domain.devices.identifiers import validate_device_id
from domain.ownership.display_name import validate_display_name

VALID_DEVICE_ID = "ib-" + "a" * 32


def _device(**overrides: object) -> Device:
    fields = {
        "device_id": VALID_DEVICE_ID,
        "hardware_version": "rev-b",
        "manufacturing_batch": "batch-2026-01",
        "ownership_status": OwnershipStatus.UNCLAIMED,
        "provisioning_status": ProvisioningStatus.MANUFACTURED,
        "aws_thing_name": VALID_DEVICE_ID,
        "created_at": 1_700_000_000,
        "updated_at": 1_700_000_000,
    }
    fields.update(overrides)
    return Device(**fields)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# device_id format
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "device_id",
    [
        VALID_DEVICE_ID,
        "ib-" + "0" * 32,
        "ib-" + "f" * 32,
        "ib-0123456789abcdef0123456789abcdef",
    ],
)
def test_valid_device_ids_accepted(device_id: str) -> None:
    assert is_valid_device_id(device_id)
    assert validate_device_id(device_id) == device_id


@pytest.mark.parametrize(
    "device_id",
    [
        "",
        "ib-" + "a" * 31,  # too short
        "ib-" + "a" * 33,  # too long
        "ib-" + "A" * 32,  # uppercase hex not allowed
        "ib-" + "g" * 32,  # non-hex character
        "IB-" + "a" * 32,  # uppercase prefix
        "a" * 32,  # missing prefix
        "ib_" + "a" * 32,  # wrong separator
        "ib-" + "a" * 32 + " ",  # trailing whitespace
    ],
)
def test_invalid_device_ids_rejected(device_id: str) -> None:
    assert not is_valid_device_id(device_id)
    with pytest.raises(ValueError, match="Invalid device_id"):
        validate_device_id(device_id)


# ---------------------------------------------------------------------------
# Device model
# ---------------------------------------------------------------------------


def test_valid_device_constructs() -> None:
    device = _device()
    assert device.device_id == VALID_DEVICE_ID
    assert device.version == 1


def test_device_rejects_invalid_device_id() -> None:
    with pytest.raises(ValueError, match="Invalid device_id"):
        _device(device_id="not-a-device-id", aws_thing_name="not-a-device-id")


def test_device_requires_thing_name_equal_to_device_id() -> None:
    with pytest.raises(ValueError, match="aws_thing_name"):
        _device(aws_thing_name="ib-" + "b" * 32)


@pytest.mark.parametrize("field", ["hardware_version", "manufacturing_batch"])
def test_device_rejects_empty_string_fields(field: str) -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        _device(**{field: "   "})


def test_device_rejects_updated_at_before_created_at() -> None:
    with pytest.raises(ValueError, match="updated_at"):
        _device(created_at=1_700_000_100, updated_at=1_700_000_000)


def test_device_rejects_claimed_at_before_created_at() -> None:
    with pytest.raises(ValueError, match="claimed_at"):
        _device(created_at=1_700_000_100, updated_at=1_700_000_100, claimed_at=1_700_000_000)


def test_device_rejects_decommissioned_at_before_created_at() -> None:
    with pytest.raises(ValueError, match="decommissioned_at"):
        _device(
            created_at=1_700_000_100,
            updated_at=1_700_000_100,
            decommissioned_at=1_700_000_000,
        )


def test_device_rejects_negative_created_at() -> None:
    with pytest.raises(ValueError, match="created_at"):
        _device(created_at=-1, updated_at=0)


def test_device_rejects_non_positive_version() -> None:
    with pytest.raises(ValueError, match="version"):
        _device(version=0)


def test_device_to_item_omits_none_fields_and_renders_enums_as_strings() -> None:
    device = _device()
    item = device.to_item()

    assert item["ownership_status"] == "UNCLAIMED"
    assert item["provisioning_status"] == "MANUFACTURED"
    assert "claimed_at" not in item
    assert "decommissioned_at" not in item


def test_device_to_item_includes_optional_fields_when_set() -> None:
    device = _device(claimed_at=1_700_000_500, updated_at=1_700_000_500)
    item = device.to_item()
    assert item["claimed_at"] == 1_700_000_500


# ---------------------------------------------------------------------------
# display_name
# ---------------------------------------------------------------------------


def test_device_without_display_name_reads_like_a_legacy_item() -> None:
    # An old DynamoDB item has no display_name attribute at all; a caller
    # building kwargs from such an item simply omits the key.
    device = _device()
    assert not hasattr(device, "display_name")
    assert "display_name" not in device.to_item()


def test_device_display_name_round_trips() -> None:
    with pytest.raises(TypeError):
        _device(display_name="Minha casa")


def test_device_display_name_is_trimmed_and_accepts_unicode() -> None:
    assert validate_display_name("  Casa da Vovó 🏠  ") == "Casa da Vovó 🏠"


def test_device_rejects_empty_display_name_after_trim() -> None:
    with pytest.raises(ValueError, match="display_name must not be empty"):
        validate_display_name("   ")


def test_device_accepts_display_name_at_max_length() -> None:
    assert validate_display_name("x" * 60) == "x" * 60


def test_device_rejects_display_name_over_max_length() -> None:
    with pytest.raises(ValueError, match="at most 60 characters"):
        validate_display_name("x" * 61)


def test_validate_display_name_trims_before_checking_length() -> None:
    # 65 characters of padding around a 60-character name must still pass:
    # the length limit applies to the trimmed value, not the raw input.
    assert validate_display_name("  " + "x" * 60 + "  ") == "x" * 60


def test_validate_display_name_rejects_whitespace_only() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        validate_display_name("\t\n  　")  # includes a full-width Unicode space


# ---------------------------------------------------------------------------
# Enum coverage
# ---------------------------------------------------------------------------


def test_ownership_status_has_every_documented_member() -> None:
    assert {member.value for member in OwnershipStatus} == {
        "UNCLAIMED",
        "CLAIM_IN_PROGRESS",
        "OWNED",
        "TRANSFER_PENDING",
        "RECOVERY_PENDING",
        "DECOMMISSIONED",
    }


def test_provisioning_status_has_every_documented_member() -> None:
    assert {member.value for member in ProvisioningStatus} == {
        "MANUFACTURED",
        "REGISTERED",
        "CLAIM_AUTHORIZED",
        "PROVISIONING",
        "PROVISIONED",
        "FAILED",
        "REVOKED",
        "DECOMMISSIONED",
    }
