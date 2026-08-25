"""Validation for a user's optional device-membership alias."""

MAX_DISPLAY_NAME_LENGTH = 60


def validate_display_name(value: str) -> str:
    """Trim and validate a user-defined device alias."""
    normalized = value.strip()
    if not normalized:
        raise ValueError("display_name must not be empty after trimming.")
    if len(normalized) > MAX_DISPLAY_NAME_LENGTH:
        raise ValueError(f"display_name must be at most {MAX_DISPLAY_NAME_LENGTH} characters.")
    return normalized
