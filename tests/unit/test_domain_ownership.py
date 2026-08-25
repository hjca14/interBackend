from __future__ import annotations

import pytest

from domain.ownership.display_name import validate_display_name
from domain.ownership.enums import MembershipRole, MembershipStatus
from domain.ownership.models import DeviceMembership

VALID_DEVICE_ID = "ib-" + "a" * 32


def _membership(**overrides: object) -> DeviceMembership:
    fields = {
        "device_id": VALID_DEVICE_ID,
        "user_id": "user-123",
        "role": MembershipRole.OWNER,
        "status": MembershipStatus.ACTIVE,
        "created_at": 1_700_000_000,
        "updated_at": 1_700_000_000,
        "created_by": "user-123",
    }
    fields.update(overrides)
    return DeviceMembership(**fields)  # type: ignore[arg-type]


def test_valid_membership_constructs() -> None:
    membership = _membership()
    assert membership.role == MembershipRole.OWNER
    assert membership.version == 1


def test_rejects_invalid_device_id() -> None:
    with pytest.raises(ValueError, match="Invalid device_id"):
        _membership(device_id="not-a-device-id")


def test_rejects_empty_user_id() -> None:
    with pytest.raises(ValueError, match="user_id"):
        _membership(user_id="   ")


def test_rejects_empty_created_by() -> None:
    with pytest.raises(ValueError, match="created_by"):
        _membership(created_by="")


def test_rejects_updated_at_before_created_at() -> None:
    with pytest.raises(ValueError, match="updated_at"):
        _membership(created_at=1_700_000_100, updated_at=1_700_000_000)


def test_rejects_negative_created_at() -> None:
    with pytest.raises(ValueError, match="created_at"):
        _membership(created_at=-1, updated_at=0)


def test_rejects_non_positive_version() -> None:
    with pytest.raises(ValueError, match="version"):
        _membership(version=0)


def test_to_item_renders_enums_as_strings() -> None:
    item = _membership().to_item()
    assert item["role"] == "OWNER"
    assert item["status"] == "ACTIVE"
    assert "display_name" not in item


def test_membership_display_name_is_optional_trimmed_and_unicode() -> None:
    membership = _membership(display_name="  Casa da Vovó 🏠  ")
    assert membership.display_name == "Casa da Vovó 🏠"
    assert membership.to_item()["display_name"] == "Casa da Vovó 🏠"


def test_membership_rejects_empty_or_long_display_name() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        _membership(display_name="   ")
    with pytest.raises(ValueError, match="at most 60"):
        _membership(display_name="x" * 61)
    assert validate_display_name("  " + "x" * 60 + "  ") == "x" * 60


def test_membership_role_has_every_documented_member() -> None:
    assert {member.value for member in MembershipRole} == {"OWNER", "ADMIN", "MEMBER"}


def test_membership_status_has_every_documented_member() -> None:
    assert {member.value for member in MembershipStatus} == {"ACTIVE", "REMOVED"}


@pytest.mark.parametrize("role", list(MembershipRole))
def test_every_role_is_constructible(role: MembershipRole) -> None:
    membership = _membership(role=role)
    assert membership.role == role
