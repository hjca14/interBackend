"""The ``device_id`` format shared by every domain model that references a device.

Per ``interBridge/docs/communication-protocol.md`` (the authoritative
protocol spec) and ``CONTEXT.md``: ``device_id`` is ``ib-`` followed by
exactly 32 lowercase hexadecimal characters. The AWS IoT Thing name and
the MQTT Client ID are always equal to this same value (see
``infrastructure/config/iot.py``) -- this module is the single place that
recognizes the format, so every domain package validates it identically.

Receiving a syntactically valid ``device_id`` in a request body is never,
by itself, proof of that device's AWS IoT identity -- it is only a
well-formed identifier. Cloud-side verification (Thing/certificate
attachment, policy, connection state) is what actually establishes
identity -- see ``CONTEXT.md``, "Verificação de conclusão".
"""

from __future__ import annotations

import re

DEVICE_ID_PATTERN = re.compile(r"^ib-[0-9a-f]{32}$")


def is_valid_device_id(device_id: str) -> bool:
    """Return whether ``device_id`` matches the ``ib-<32 lowercase hex>`` format."""
    return isinstance(device_id, str) and DEVICE_ID_PATTERN.fullmatch(device_id) is not None


def validate_device_id(device_id: str) -> str:
    """Return ``device_id`` unchanged if valid, else raise ``ValueError``.

    Never treats a received ``device_id`` as proof of AWS IoT identity --
    see the module docstring.
    """
    if not is_valid_device_id(device_id):
        raise ValueError(
            f"Invalid device_id {device_id!r}: expected 'ib-' followed by exactly "
            "32 lowercase hexadecimal characters."
        )
    return device_id
