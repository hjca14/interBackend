"""Device identity: the ``device_id`` format and the ``Device`` record."""

from domain.devices.display_name import MAX_DISPLAY_NAME_LENGTH, validate_display_name
from domain.devices.enums import OwnershipStatus, ProvisioningStatus
from domain.devices.identifiers import DEVICE_ID_PATTERN, is_valid_device_id, validate_device_id
from domain.devices.models import Device

__all__ = [
    "DEVICE_ID_PATTERN",
    "MAX_DISPLAY_NAME_LENGTH",
    "Device",
    "OwnershipStatus",
    "ProvisioningStatus",
    "is_valid_device_id",
    "validate_device_id",
    "validate_display_name",
]
