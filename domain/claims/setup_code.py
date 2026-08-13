"""``setup_code`` normalization and its protected (HMAC-SHA256) digest.

The raw ``setup_code`` (12 random decimal digits, generated at
manufacturing time -- see ``CONTEXT.md``, "Onboarding BLE-first") is
**never** stored in DynamoDB or logged. Only its digest is stored, in the
``interbridge-dev-setup-code-lookups`` table -- see
``infrastructure/stacks/data_stack.py`` and ``docs/data-model.md``.

A plain SHA-256 of a 12-digit code is not acceptable: the input space is
only 10**12 (~40 bits), small enough that an attacker with a stolen copy
of the lookup table could brute-force every possible code by hashing them
all and comparing. Keying the hash with a secret, per-deployment "pepper"
(``HMAC-SHA256(pepper, normalized_code)``) makes that offline brute-force
attack infeasible without also stealing the pepper -- which, per this
phase's scope, is not provisioned yet (see ``compute_setup_code_digest``).
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass, fields
from hashlib import sha256

from domain.devices.identifiers import validate_device_id

SETUP_CODE_LENGTH = 12
_ASCII_DIGITS = frozenset("0123456789")
_DIGEST_HEX_CHARS = frozenset("0123456789abcdef")
_DIGEST_LENGTH = 64  # sha256().hexdigest() length


def normalize_setup_code(raw: str) -> str:
    """Validate ``raw`` as a setup_code and return it unchanged.

    Deliberately does **not** strip whitespace or separators: a code
    containing them is rejected outright rather than silently repaired,
    so a UI bug that lets a malformed code through fails loudly instead
    of quietly accepting a different value than the user typed.

    Raises ``ValueError`` unless ``raw`` is a ``str`` of exactly
    ``SETUP_CODE_LENGTH`` ASCII decimal digits (``0``-``9``). Unicode
    digit characters from other scripts (e.g. Arabic-Indic, full-width)
    are rejected even though some of them satisfy ``str.isdigit()``,
    because ``[0-9]`` only matches the literal ASCII code points.
    """
    if not isinstance(raw, str):
        raise ValueError(f"setup_code must be a str, got {type(raw).__name__}.")
    if len(raw) != SETUP_CODE_LENGTH or any(char not in _ASCII_DIGITS for char in raw):
        raise ValueError(
            f"setup_code must be exactly {SETUP_CODE_LENGTH} ASCII digits (0-9), got {raw!r}."
        )
    return raw


def compute_setup_code_digest(pepper: bytes, normalized_code: str) -> str:
    """Compute the HMAC-SHA256 digest stored in place of the raw setup_code.

    ``pepper`` is a mandatory, caller-supplied secret with **no default
    value** -- this function intentionally has no built-in pepper, no
    hardcoded fallback, and no environment-variable fallback, so it can
    never be called "successfully" without the caller deliberately
    providing a real secret. It also never logs ``pepper`` or
    ``normalized_code`` (this module does not import ``logging`` at all).

    The pepper itself is not provisioned in this phase -- see
    ``docs/data-model.md`` for why (no Secrets Manager / customer-managed
    KMS key yet, since no runtime consumer exists to use them).
    """
    normalize_setup_code(normalized_code)
    return hmac.new(pepper, normalized_code.encode("ascii"), sha256).hexdigest()


def is_valid_setup_code_digest(digest: str) -> bool:
    """Return whether ``digest`` looks like a valid HMAC-SHA256 hex digest.

    Exactly 64 lowercase hexadecimal characters -- uppercase or
    mixed-case input is rejected rather than normalized, so a
    case-mismatched digest never silently matches (or fails to match) a
    stored value depending on comparison method.
    """
    return (
        isinstance(digest, str)
        and len(digest) == _DIGEST_LENGTH
        and all(char in _DIGEST_HEX_CHARS for char in digest)
    )


@dataclass(frozen=True)
class SetupCodeLookup:
    """One item in the setup-code lookup table: digest -> device_id.

    Contains **no** raw ``setup_code`` field -- only its digest. See the
    module docstring for why a raw code must never be persisted.
    """

    setup_code_digest: str
    device_id: str
    created_at: int
    disabled_at: int | None = None
    version: int = 1

    def __post_init__(self) -> None:
        if not is_valid_setup_code_digest(self.setup_code_digest):
            raise ValueError(
                f"setup_code_digest must be {_DIGEST_LENGTH} lowercase hex characters, "
                f"got {self.setup_code_digest!r}."
            )
        validate_device_id(self.device_id)

        if self.created_at < 0:
            raise ValueError("created_at must be a non-negative Unix epoch second value.")
        if self.disabled_at is not None and self.disabled_at < self.created_at:
            raise ValueError("disabled_at must not be earlier than created_at.")

        if self.version < 1:
            raise ValueError("version must be a positive integer (optimistic concurrency).")

    def to_item(self) -> dict[str, object]:
        """Render as a plain dict suitable for a DynamoDB item.

        Never includes a raw setup_code -- there is no such field on this
        model to begin with.
        """
        item: dict[str, object] = {}
        for f in fields(self):
            value = getattr(self, f.name)
            if value is None:
                continue
            item[f.name] = value
        return item
