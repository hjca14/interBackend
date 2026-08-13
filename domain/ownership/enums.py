"""Enumerations for ``DeviceMembership``."""

from __future__ import annotations

from enum import StrEnum


class MembershipRole(StrEnum):
    """A user's role on a specific device.

    Only ``OWNER`` is enforced/supported end-to-end in this phase (see
    ``CONTEXT.md``, "Device Registry futuro") -- ``ADMIN`` and ``MEMBER``
    are reserved for future phases but modeled now so the table schema
    does not need to change when they are implemented.
    """

    OWNER = "OWNER"
    ADMIN = "ADMIN"
    MEMBER = "MEMBER"


class MembershipStatus(StrEnum):
    """Whether a membership record is currently in effect."""

    ACTIVE = "ACTIVE"
    REMOVED = "REMOVED"
