"""The ``Device`` record: manufacturing identity and status.

Mirrors the ``interbridge-dev-devices`` DynamoDB table (see
``infrastructure/stacks/data_stack.py`` and ``docs/data-model.md``) but has
no dependency on ``aws_cdk`` or ``boto3`` -- it is a plain, independently
testable representation of one item in that table.
"""

from __future__ import annotations

from dataclasses import dataclass, fields

from domain.devices.enums import OwnershipStatus, ProvisioningStatus
from domain.devices.identifiers import validate_device_id


@dataclass(frozen=True)
class Device:
    """One physical InterBridge device, identified by ``device_id``.

    Ownership (who has claimed it) and membership (which users have
    access) are deliberately **not** part of this record -- see
    ``domain.ownership.DeviceMembership``. A device's physical identity
    must never depend on who currently owns it.
    """

    device_id: str
    hardware_version: str
    manufacturing_batch: str
    ownership_status: OwnershipStatus
    provisioning_status: ProvisioningStatus
    aws_thing_name: str
    created_at: int
    updated_at: int
    claimed_at: int | None = None
    decommissioned_at: int | None = None
    version: int = 1

    def __post_init__(self) -> None:
        validate_device_id(self.device_id)

        # The AWS IoT Thing name is always the device_id (see
        # infrastructure/config/iot.py and CONTEXT.md) -- never a
        # separately assignable value.
        if self.aws_thing_name != self.device_id:
            raise ValueError(
                f"aws_thing_name {self.aws_thing_name!r} must equal device_id {self.device_id!r}."
            )

        if not self.hardware_version.strip():
            raise ValueError("hardware_version must not be empty.")
        if not self.manufacturing_batch.strip():
            raise ValueError("manufacturing_batch must not be empty.")

        if self.created_at < 0:
            raise ValueError("created_at must be a non-negative Unix epoch second value.")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must not be earlier than created_at.")
        if self.claimed_at is not None and self.claimed_at < self.created_at:
            raise ValueError("claimed_at must not be earlier than created_at.")
        if self.decommissioned_at is not None and self.decommissioned_at < self.created_at:
            raise ValueError("decommissioned_at must not be earlier than created_at.")

        if self.version < 1:
            raise ValueError("version must be a positive integer (optimistic concurrency).")

    def to_item(self) -> dict[str, object]:
        """Render as a plain dict suitable for a DynamoDB item.

        Enum fields are rendered as their string ``.value``; unset
        (``None``) optional fields are omitted rather than stored as an
        explicit null.
        """
        item: dict[str, object] = {}
        for f in fields(self):
            value = getattr(self, f.name)
            if value is None:
                continue
            item[f.name] = (
                value.value if isinstance(value, (OwnershipStatus, ProvisioningStatus)) else value
            )
        return item
