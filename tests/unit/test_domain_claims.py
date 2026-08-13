from __future__ import annotations

import dataclasses

import pytest

from domain.claims.enums import ClaimSource, ClaimStatus
from domain.claims.models import ClaimSession

VALID_DEVICE_ID = "ib-" + "a" * 32
CREATED_AT = 1_700_000_000
EXPIRES_AT = CREATED_AT + 300


def _session(**overrides: object) -> ClaimSession:
    fields = {
        "claim_session_id": "cs-0001",
        "device_id": VALID_DEVICE_ID,
        "user_id": "user-123",
        "source": ClaimSource.BLE,
        "status": ClaimStatus.PENDING,
        "created_at": CREATED_AT,
        "expires_at": EXPIRES_AT,
        "ttl": EXPIRES_AT,
    }
    fields.update(overrides)
    return ClaimSession(**fields)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Basic field validation
# ---------------------------------------------------------------------------


def test_valid_pending_session_constructs() -> None:
    session = _session()
    assert session.status == ClaimStatus.PENDING
    assert session.version == 1


def test_rejects_empty_claim_session_id() -> None:
    with pytest.raises(ValueError, match="claim_session_id"):
        _session(claim_session_id="  ")


def test_rejects_invalid_device_id() -> None:
    with pytest.raises(ValueError, match="Invalid device_id"):
        _session(device_id="not-a-device-id")


def test_rejects_empty_user_id() -> None:
    with pytest.raises(ValueError, match="user_id"):
        _session(user_id="")


def test_rejects_non_positive_version() -> None:
    with pytest.raises(ValueError, match="version"):
        _session(version=0)


def test_rejects_negative_created_at() -> None:
    with pytest.raises(ValueError, match="created_at"):
        _session(created_at=-1, expires_at=100, ttl=100)


# ---------------------------------------------------------------------------
# Expiration / TTL
# ---------------------------------------------------------------------------


def test_expires_at_must_be_later_than_created_at() -> None:
    with pytest.raises(ValueError, match="expires_at must be later than created_at"):
        _session(expires_at=CREATED_AT, ttl=CREATED_AT)


def test_expires_at_equal_to_created_at_is_rejected() -> None:
    with pytest.raises(ValueError, match="expires_at"):
        _session(created_at=1000, expires_at=1000, ttl=1000)


def test_ttl_must_equal_expires_at() -> None:
    with pytest.raises(ValueError, match="ttl must equal expires_at"):
        _session(ttl=EXPIRES_AT + 1)


def test_used_at_before_created_at_rejected() -> None:
    with pytest.raises(ValueError, match="used_at"):
        _session(
            status=ClaimStatus.AUTHORIZED,
            used_at=CREATED_AT - 1,
        )


# ---------------------------------------------------------------------------
# Status <-> timestamp consistency
# ---------------------------------------------------------------------------


def test_pending_must_not_have_used_at() -> None:
    with pytest.raises(ValueError, match="used_at must not be set while status is PENDING"):
        _session(status=ClaimStatus.PENDING, used_at=CREATED_AT + 1)


def test_authorized_requires_used_at() -> None:
    with pytest.raises(ValueError, match="used_at is required"):
        _session(status=ClaimStatus.AUTHORIZED)


def test_authorized_with_used_at_is_valid() -> None:
    session = _session(status=ClaimStatus.AUTHORIZED, used_at=CREATED_AT + 1)
    assert session.used_at == CREATED_AT + 1


def test_provisioning_requires_used_at() -> None:
    with pytest.raises(ValueError, match="used_at is required"):
        _session(status=ClaimStatus.PROVISIONING)


def test_completed_requires_completed_at() -> None:
    with pytest.raises(ValueError, match="completed_at is required"):
        _session(status=ClaimStatus.COMPLETED, used_at=CREATED_AT + 1)


def test_completed_with_completed_at_is_valid() -> None:
    session = _session(
        status=ClaimStatus.COMPLETED, used_at=CREATED_AT + 1, completed_at=CREATED_AT + 2
    )
    assert session.completed_at == CREATED_AT + 2


def test_completed_must_not_also_have_cancelled_at() -> None:
    with pytest.raises(ValueError, match="COMPLETED sessions must not have"):
        _session(
            status=ClaimStatus.COMPLETED,
            used_at=CREATED_AT + 1,
            completed_at=CREATED_AT + 2,
            cancelled_at=CREATED_AT + 2,
        )


def test_cancelled_requires_cancelled_at() -> None:
    with pytest.raises(ValueError, match="cancelled_at is required"):
        _session(status=ClaimStatus.CANCELLED)


def test_cancelled_must_not_also_have_completed_at() -> None:
    with pytest.raises(ValueError, match="CANCELLED sessions must not have"):
        _session(
            status=ClaimStatus.CANCELLED,
            cancelled_at=CREATED_AT + 2,
            completed_at=CREATED_AT + 2,
        )


def test_failed_requires_failure_code() -> None:
    with pytest.raises(ValueError, match="failure_code is required"):
        _session(status=ClaimStatus.FAILED, used_at=CREATED_AT + 1)


def test_failed_with_failure_code_is_valid() -> None:
    session = _session(
        status=ClaimStatus.FAILED, used_at=CREATED_AT + 1, failure_code="PROVISIONING_TIMEOUT"
    )
    assert session.failure_code == "PROVISIONING_TIMEOUT"


def test_failed_must_not_also_have_completed_at() -> None:
    with pytest.raises(ValueError, match="FAILED sessions must not have"):
        _session(
            status=ClaimStatus.FAILED,
            used_at=CREATED_AT + 1,
            failure_code="X",
            completed_at=CREATED_AT + 2,
        )


def test_expired_must_not_have_completion_fields() -> None:
    with pytest.raises(ValueError, match="completed_at must not be set"):
        _session(status=ClaimStatus.EXPIRED, completed_at=CREATED_AT + 2)


def test_pending_must_not_have_failure_code() -> None:
    with pytest.raises(ValueError, match="failure_code must not be set"):
        _session(status=ClaimStatus.PENDING, failure_code="X")


def test_non_terminal_status_must_not_have_cancelled_at() -> None:
    with pytest.raises(ValueError, match="cancelled_at must not be set"):
        _session(status=ClaimStatus.AUTHORIZED, used_at=CREATED_AT + 1, cancelled_at=CREATED_AT + 2)


# ---------------------------------------------------------------------------
# Enum coverage
# ---------------------------------------------------------------------------


def test_claim_source_has_every_documented_member() -> None:
    assert {member.value for member in ClaimSource} == {"BLE", "QR", "MANUAL"}


def test_claim_status_has_every_documented_member() -> None:
    assert {member.value for member in ClaimStatus} == {
        "PENDING",
        "AUTHORIZED",
        "PROVISIONING",
        "COMPLETED",
        "EXPIRED",
        "CANCELLED",
        "FAILED",
    }


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def test_to_item_renders_enums_as_strings_and_omits_none_fields() -> None:
    session = _session()
    item = session.to_item()
    assert item["source"] == "BLE"
    assert item["status"] == "PENDING"
    assert "completed_at" not in item
    assert "cancelled_at" not in item
    assert "failure_code" not in item


def test_claim_session_has_no_raw_setup_code_field() -> None:
    field_names = {f.name for f in dataclasses.fields(ClaimSession)}
    assert "setup_code" not in field_names
