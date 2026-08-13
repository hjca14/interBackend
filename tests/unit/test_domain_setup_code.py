from __future__ import annotations

import dataclasses
import hashlib
import inspect

import pytest

from domain.claims.setup_code import (
    SETUP_CODE_LENGTH,
    SetupCodeLookup,
    compute_setup_code_digest,
    is_valid_setup_code_digest,
    normalize_setup_code,
)

VALID_CODE = "012345678901"
VALID_DEVICE_ID = "ib-" + "a" * 32
PEPPER_A = b"pepper-a-not-a-real-secret"
PEPPER_B = b"pepper-b-not-a-real-secret"


# ---------------------------------------------------------------------------
# normalize_setup_code
# ---------------------------------------------------------------------------


def test_normalize_accepts_exactly_twelve_ascii_digits() -> None:
    assert normalize_setup_code(VALID_CODE) == VALID_CODE
    assert len(VALID_CODE) == SETUP_CODE_LENGTH


def test_normalize_accepts_leading_zeros() -> None:
    code = "000000000001"
    assert normalize_setup_code(code) == code


@pytest.mark.parametrize(
    "raw",
    [
        "1234567890",  # 10 digits -- too short
        "1234567890123",  # 13 digits -- too long
        "",
    ],
)
def test_normalize_rejects_wrong_length(raw: str) -> None:
    with pytest.raises(ValueError, match="12 ASCII digits"):
        normalize_setup_code(raw)


@pytest.mark.parametrize(
    "raw",
    [
        "1234 5678901",  # space
        "1234-5678901",  # hyphen
        "1234_5678901",  # underscore
        "1234.5678901",  # period
        "123456789012 ",  # trailing space
    ],
)
def test_normalize_rejects_spaces_and_separators(raw: str) -> None:
    with pytest.raises(ValueError, match="12 ASCII digits"):
        normalize_setup_code(raw)


@pytest.mark.parametrize(
    "raw",
    [
        "０１２３４５６７８９０１",  # full-width digits
        "٠١٢٣٤٥٦٧٨٩٠١",  # Arabic-Indic digits
        "੦੧੨੩੪੫੬੭੮੯੦੧",  # Gurmukhi digits
    ],
)
def test_normalize_rejects_unicode_digit_variants(raw: str) -> None:
    # These satisfy str.isdigit() for some scripts, but must still be
    # rejected -- only literal ASCII '0'-'9' are valid.
    with pytest.raises(ValueError, match="12 ASCII digits"):
        normalize_setup_code(raw)


def test_normalize_rejects_non_string_input() -> None:
    with pytest.raises(ValueError, match="must be a str"):
        normalize_setup_code(123456789012)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# compute_setup_code_digest
# ---------------------------------------------------------------------------


def test_pepper_has_no_default_value() -> None:
    # The helper must not be callable without an explicit pepper -- no
    # default, no environment fallback.
    sig = inspect.signature(compute_setup_code_digest)
    assert sig.parameters["pepper"].default is inspect.Parameter.empty


def test_digest_is_deterministic_for_same_code_and_pepper() -> None:
    first = compute_setup_code_digest(PEPPER_A, VALID_CODE)
    second = compute_setup_code_digest(PEPPER_A, VALID_CODE)
    assert first == second


def test_digest_differs_for_different_peppers() -> None:
    assert compute_setup_code_digest(PEPPER_A, VALID_CODE) != compute_setup_code_digest(
        PEPPER_B, VALID_CODE
    )


def test_digest_differs_for_different_codes() -> None:
    other_code = "999999999999"
    assert compute_setup_code_digest(PEPPER_A, VALID_CODE) != compute_setup_code_digest(
        PEPPER_A, other_code
    )


def test_digest_is_64_lowercase_hex_characters() -> None:
    digest = compute_setup_code_digest(PEPPER_A, VALID_CODE)
    assert is_valid_setup_code_digest(digest)
    assert len(digest) == 64
    assert digest == digest.lower()


def test_digest_rejects_invalid_normalized_code() -> None:
    with pytest.raises(ValueError, match="12 ASCII digits"):
        compute_setup_code_digest(PEPPER_A, "not-a-code")


def test_digest_is_not_a_plain_sha256_of_the_code() -> None:
    # A plain (unkeyed) SHA-256 would be brute-forceable offline given the
    # tiny (10**12) input space -- the digest must depend on the pepper.
    plain_sha256 = hashlib.sha256(VALID_CODE.encode("ascii")).hexdigest()
    assert compute_setup_code_digest(PEPPER_A, VALID_CODE) != plain_sha256


# ---------------------------------------------------------------------------
# is_valid_setup_code_digest
# ---------------------------------------------------------------------------


def test_is_valid_setup_code_digest_rejects_uppercase() -> None:
    digest = compute_setup_code_digest(PEPPER_A, VALID_CODE)
    assert not is_valid_setup_code_digest(digest.upper())


def test_is_valid_setup_code_digest_rejects_wrong_length() -> None:
    assert not is_valid_setup_code_digest("abc123")


# ---------------------------------------------------------------------------
# SetupCodeLookup
# ---------------------------------------------------------------------------


def _lookup(**overrides: object) -> SetupCodeLookup:
    fields = {
        "setup_code_digest": compute_setup_code_digest(PEPPER_A, VALID_CODE),
        "device_id": VALID_DEVICE_ID,
        "created_at": 1_700_000_000,
    }
    fields.update(overrides)
    return SetupCodeLookup(**fields)  # type: ignore[arg-type]


def test_valid_setup_code_lookup_constructs() -> None:
    lookup = _lookup()
    assert lookup.device_id == VALID_DEVICE_ID


def test_setup_code_lookup_rejects_invalid_digest() -> None:
    with pytest.raises(ValueError, match="setup_code_digest"):
        _lookup(setup_code_digest="too-short")


def test_setup_code_lookup_rejects_invalid_device_id() -> None:
    with pytest.raises(ValueError, match="Invalid device_id"):
        _lookup(device_id="not-a-device-id")


def test_setup_code_lookup_rejects_negative_created_at() -> None:
    with pytest.raises(ValueError, match="created_at"):
        _lookup(created_at=-1)


def test_setup_code_lookup_rejects_disabled_at_before_created_at() -> None:
    with pytest.raises(ValueError, match="disabled_at"):
        _lookup(created_at=1_700_000_100, disabled_at=1_700_000_000)


def test_setup_code_lookup_rejects_non_positive_version() -> None:
    with pytest.raises(ValueError, match="version"):
        _lookup(version=0)


def test_setup_code_lookup_never_has_a_raw_setup_code_field() -> None:
    field_names = {f.name for f in dataclasses.fields(SetupCodeLookup)}
    assert "setup_code" not in field_names
    assert "setup_code_digest" in field_names


def test_setup_code_lookup_to_item_never_contains_raw_code() -> None:
    lookup = _lookup()
    item = lookup.to_item()
    assert "setup_code" not in item
    assert item["setup_code_digest"] == lookup.setup_code_digest
    assert VALID_CODE not in str(item)
