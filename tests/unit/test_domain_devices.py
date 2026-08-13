from __future__ import annotations

import pytest

from domain.devices import Device, OwnershipStatus, ProvisioningStatus, is_valid_device_id
from domain.devices.identifiers import validate_device_id

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
