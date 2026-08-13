"""Enumerations for ``ClaimSession``."""

from __future__ import annotations

from enum import StrEnum


class ClaimSource(StrEnum):
    """Which onboarding channel started the claim session.

    Mirrors the BLE-first flow and its two fallbacks -- see
    ``CONTEXT.md``, "Onboarding BLE-first": BLE is primary, QR and MANUAL
    (typed) both carry the same ``setup_code`` and are not separate
    security paths.
    """

    BLE = "BLE"
    QR = "QR"
    MANUAL = "MANUAL"


class ClaimStatus(StrEnum):
    """Lifecycle state of a claim session.

    ``COMPLETED``, ``EXPIRED``, ``CANCELLED`` and ``FAILED`` are terminal:
    once in one of those states, a session can never transition again --
    see ``ClaimSession.__post_init__`` for the timestamp/status
    consistency this implies, and ``CONTEXT.md`` for the invariants.
    """

    PENDING = "PENDING"
    AUTHORIZED = "AUTHORIZED"
    PROVISIONING = "PROVISIONING"
    COMPLETED = "COMPLETED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


TERMINAL_CLAIM_STATUSES = frozenset(
    {ClaimStatus.COMPLETED, ClaimStatus.EXPIRED, ClaimStatus.CANCELLED, ClaimStatus.FAILED}
)
