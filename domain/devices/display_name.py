"""The optional, user-facing ``display_name`` on a ``Device``.

A single friendly name per device (e.g. "Minha casa", "Interfone") -- never a
room/location field; the product treats one InterBridge per residence, so a
per-room name is deliberately out of scope (see ``CONTEXT.md``). This module
is the single place that validates/normalizes the value so
``domain.devices.models.Device`` and any Lambda handler that accepts a new
name from a request body apply exactly the same rule.

``display_name`` is cosmetic only: it is never used for authorization, as a
key, as an MQTT topic segment, or as an identity. A device without one is not
"unnamed" from the app's perspective -- the app is expected to show a
fallback label (e.g. "InterBridge") when this is ``None``; that fallback is
never persisted here.
"""

from __future__ import annotations

MAX_DISPLAY_NAME_LENGTH = 60


def validate_display_name(value: str) -> str:
    """Trim ``value`` and return it if it is a valid display name, else raise ``ValueError``.

    Unicode is accepted as-is (no normalization beyond ``str.strip()``, which
    already strips Unicode whitespace, not just ASCII spaces).
    """
    trimmed = value.strip()
    if not trimmed:
        raise ValueError("display_name must not be empty after trimming.")
    if len(trimmed) > MAX_DISPLAY_NAME_LENGTH:
        raise ValueError(f"display_name must be at most {MAX_DISPLAY_NAME_LENGTH} characters.")
    return trimmed
