"""Enumerations for the ``Device`` record's two independent status axes.

Ownership and provisioning are tracked separately on purpose: a device can
be, for example, ``OWNED`` (ownership) while its provisioning history
still shows ``PROVISIONED`` or even ``FAILED`` from a prior attempt --
conflating the two into one status field would make illegal states
representable.
"""

from __future__ import annotations

from enum import StrEnum


class OwnershipStatus(StrEnum):
    """Who -- if anyone -- currently owns the device."""

    UNCLAIMED = "UNCLAIMED"
    CLAIM_IN_PROGRESS = "CLAIM_IN_PROGRESS"
    OWNED = "OWNED"
    TRANSFER_PENDING = "TRANSFER_PENDING"
    RECOVERY_PENDING = "RECOVERY_PENDING"
    DECOMMISSIONED = "DECOMMISSIONED"


class ProvisioningStatus(StrEnum):
    """Where the device is in its manufacturing/provisioning lifecycle."""

    MANUFACTURED = "MANUFACTURED"
    REGISTERED = "REGISTERED"
    CLAIM_AUTHORIZED = "CLAIM_AUTHORIZED"
    PROVISIONING = "PROVISIONING"
    PROVISIONED = "PROVISIONED"
    FAILED = "FAILED"
    REVOKED = "REVOKED"
    DECOMMISSIONED = "DECOMMISSIONED"
