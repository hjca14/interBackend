"""Input validation for the DEV MQTT smoke test."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

DEVICE_ID_PATTERN = re.compile(r"^ib-[0-9a-f]{32}$")


def validate_device_id(value: str) -> str:
    if DEVICE_ID_PATTERN.fullmatch(value) is None:
        raise ValueError("device_id must match ^ib-[0-9a-f]{32}$")
    return value


def validate_endpoint(value: str) -> str:
    if not value or any(character.isspace() for character in value):
        raise ValueError("endpoint must be a hostname only")
    parsed = urlsplit(f"//{value}")
    if (
        parsed.hostname != value
        or parsed.port is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("endpoint must be a hostname only, without scheme, port, or path")
    if "." not in value or value.startswith(".") or value.endswith("."):
        raise ValueError("endpoint must be a valid hostname")
    return value


def validate_regular_file(value: str | Path, label: str) -> Path:
    path = Path(value).expanduser().resolve()
    if not path.is_file():
        raise ValueError(f"{label} must exist and be a regular file")
    return path


@dataclass(frozen=True)
class SmokeConfig:
    endpoint: str
    device_id: str
    certificate_path: Path
    private_key_path: Path
    root_ca_path: Path
    port: int = 8883

    def __post_init__(self) -> None:
        object.__setattr__(self, "endpoint", validate_endpoint(self.endpoint))
        object.__setattr__(self, "device_id", validate_device_id(self.device_id))
        object.__setattr__(
            self, "certificate_path", validate_regular_file(self.certificate_path, "certificate")
        )
        object.__setattr__(
            self, "private_key_path", validate_regular_file(self.private_key_path, "private key")
        )
        object.__setattr__(
            self, "root_ca_path", validate_regular_file(self.root_ca_path, "root CA")
        )
        if len({self.certificate_path, self.private_key_path, self.root_ca_path}) != 3:
            raise ValueError("certificate, private key, and root CA must be different files")
        if not 1 <= self.port <= 65535:
            raise ValueError("port must be between 1 and 65535")

    @property
    def client_id(self) -> str:
        """The deployed policy requires ClientId == ThingName == device_id."""
        return self.device_id
