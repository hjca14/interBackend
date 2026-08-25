"""The ``DeviceMembership`` record: one user's role on one device.

Mirrors the ``interbridge-dev-device-memberships`` DynamoDB table (partition
key ``device_id``, sort key ``user_id``, plus a
``<...>-by-user-index`` GSI keyed by ``user_id``/``device_id`` -- see
``infrastructure/stacks/data_stack.py`` and ``docs/data-model.md``).

**Single-OWNER invariant is not enforced here.** This phase only supports
one active ``OWNER`` membership per device, but DynamoDB item-level
validation (this dataclass) cannot see other items in the table -- it has
no way to know whether another ``OWNER`` record already exists for the
same ``device_id``. Enforcing "at most one active OWNER" requires a
transactional write (e.g. a conditional check against a denormalized
marker on the ``Device`` item) implemented by a future runtime consumer,
not by this model.
"""

from __future__ import annotations

from dataclasses import dataclass, fields

from domain.devices.identifiers import validate_device_id
from domain.ownership.display_name import validate_display_name
from domain.ownership.enums import MembershipRole, MembershipStatus


@dataclass(frozen=True)
class DeviceMembership:
    """One user's role on one device."""

    device_id: str
    user_id: str
    role: MembershipRole
    status: MembershipStatus
    created_at: int
    updated_at: int
    created_by: str
    version: int = 1
    display_name: str | None = None

    def __post_init__(self) -> None:
        validate_device_id(self.device_id)

        if not self.user_id.strip():
            raise ValueError("user_id must not be empty.")
        if not self.created_by.strip():
            raise ValueError("created_by must not be empty.")

        if self.created_at < 0:
            raise ValueError("created_at must be a non-negative Unix epoch second value.")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must not be earlier than created_at.")

        if self.version < 1:
            raise ValueError("version must be a positive integer (optimistic concurrency).")

        if self.display_name is not None:
            object.__setattr__(self, "display_name", validate_display_name(self.display_name))

    def to_item(self) -> dict[str, object]:
        """Render as a plain dict suitable for a DynamoDB item."""
        item: dict[str, object] = {}
        for f in fields(self):
            value = getattr(self, f.name)
            if isinstance(value, (MembershipRole, MembershipStatus)):
                value = value.value
            if value is not None:
                item[f.name] = value
        return item
