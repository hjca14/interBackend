"""Device identity: the ``device_id`` format and the ``Device`` record."""

from domain.devices.enums import OwnershipStatus, ProvisioningStatus
from domain.devices.identifiers import DEVICE_ID_PATTERN, is_valid_device_id, validate_device_id
from domain.devices.models import Device

__all__ = [
    "DEVICE_ID_PATTERN",
    "Device",
    "OwnershipStatus",
    "ProvisioningStatus",
    "is_valid_device_id",
    "validate_device_id",
]
