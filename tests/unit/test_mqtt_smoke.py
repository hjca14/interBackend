from __future__ import annotations

import json
import logging
import ssl
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from infrastructure.config.environment import EnvironmentConfig
from infrastructure.config.iot import iot_names
from mqtt_smoke.config import SmokeConfig, validate_device_id, validate_endpoint
from mqtt_smoke.device_simulator import DeviceSimulator
from mqtt_smoke.messages import (
    MAX_COMMAND_BYTES,
    Command,
    health_payload,
    parse_command,
    rejected_response,
    safe_event_payload,
)
from mqtt_smoke.topics import topics_for

DEVICE_ID = "ib-" + "a" * 32
COMMAND_ID = "b" * 32


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.on_connect: Any = None
        self.on_disconnect: Any = None
        self.on_message: Any = None
        self.on_publish: Any = None
        self.on_subscribe: Any = None

    def tls_set(self, **kwargs: Any) -> None:
        self.calls.append(("tls_set", kwargs))

    def tls_insecure_set(self, value: bool) -> None:
        self.calls.append(("tls_insecure_set", value))

    def connect(self, host: str, port: int, keepalive: int) -> None:
        self.calls.append(("connect", (host, port, keepalive)))

    def subscribe(self, topic: str, qos: int) -> None:
        self.calls.append(("subscribe", (topic, qos)))

    def publish(self, topic: str, payload: bytes, qos: int, retain: bool) -> None:
        self.calls.append(("publish", (topic, json.loads(payload), qos, retain)))

    def loop_forever(self) -> None:
        self.calls.append(("loop_forever", None))

    def disconnect(self) -> None:
        self.calls.append(("disconnect", None))


@pytest.fixture
def credential_files(tmp_path: Path) -> tuple[Path, Path, Path]:
    paths = (tmp_path / "device.crt", tmp_path / "device.key", tmp_path / "root.pem")
    for index, path in enumerate(paths):
        path.write_text(f"secret-{index}", encoding="utf-8")
    return paths


def config(credential_files: tuple[Path, Path, Path]) -> SmokeConfig:
    return SmokeConfig("endpoint.iot.sa-east-1.amazonaws.com", DEVICE_ID, *credential_files)


def simulator(credential_files: tuple[Path, Path, Path]) -> tuple[DeviceSimulator, FakeClient]:
    client = FakeClient()
    device = DeviceSimulator(config(credential_files), client_factory=lambda client_id: client)
    return device, client


def test_exact_topics_and_dev_rule_names() -> None:
    topics = topics_for(DEVICE_ID)
    assert topics.commands == f"interbridge/{DEVICE_ID}/commands"
    assert topics.events == f"$aws/rules/interbridge_dev_ingest_rule/interbridge/{DEVICE_ID}/events"
    assert topics.health == f"$aws/rules/interbridge_dev_ingest_rule/interbridge/{DEVICE_ID}/health"
    assert (
        topics.responses
        == f"$aws/rules/interbridge_dev_response_rule/interbridge/{DEVICE_ID}/responses"
    )
    names = iot_names(EnvironmentConfig())
    assert (names.ingest_rule_name, names.response_rule_name) == (
        "interbridge_dev_ingest_rule",
        "interbridge_dev_response_rule",
    )


@pytest.mark.parametrize("value", ["ib-" + "A" * 32, "ib-short", "x" + "a" * 32, ""])
def test_invalid_device_ids(value: str) -> None:
    with pytest.raises(ValueError, match="device_id"):
        validate_device_id(value)


@pytest.mark.parametrize(
    "value",
    ["https://host.example", "host.example/path", "host.example:8883", "localhost", "host example"],
)
def test_invalid_endpoints(value: str) -> None:
    with pytest.raises(ValueError, match="endpoint"):
        validate_endpoint(value)


def test_file_validation_and_distinct_paths(
    tmp_path: Path, credential_files: tuple[Path, Path, Path]
) -> None:
    with pytest.raises(ValueError, match="regular file"):
        SmokeConfig(
            "host.example",
            DEVICE_ID,
            tmp_path / "missing",
            credential_files[1],
            credential_files[2],
        )
    with pytest.raises(ValueError, match="different files"):
        SmokeConfig(
            "host.example", DEVICE_ID, credential_files[0], credential_files[0], credential_files[2]
        )


def test_health_payload_matches_protocol_fields() -> None:
    payload = json.loads(
        health_payload(
            DEVICE_ID,
            firmware_version="1.2.3",
            intercom_state="IDLE",
            uptime_ms=10,
            wifi_rssi=-55,
            free_heap=999,
        )
    )
    assert payload == {
        "protocol_version": 1,
        "device_id": DEVICE_ID,
        "firmware_version": "1.2.3",
        "intercom_state": "IDLE",
        "uptime_ms": 10,
        "wifi_rssi": -55,
        "free_heap": 999,
    }


def test_safe_event_has_canonical_id_and_utc_timestamp() -> None:
    payload = json.loads(safe_event_payload(DEVICE_ID, timestamp=datetime(2026, 8, 14, tzinfo=UTC)))
    assert payload["event"] == "ERROR"
    assert len(payload["event_id"]) == 36
    assert payload["event_id"].startswith("evt-")
    int(payload["event_id"][4:], 16)
    assert payload["timestamp"] == "2026-08-14T00:00:00Z"
    with pytest.raises(ValueError, match="UTC"):
        safe_event_payload(DEVICE_ID, timestamp=datetime(2026, 8, 14))


def command_bytes(**changes: object) -> bytes:
    body = {"protocol_version": 1, "command_id": COMMAND_ID, "command": "OPEN_DOOR"}
    body.update(changes)
    return json.dumps(body).encode()


def test_command_parsing_preserves_identifiers() -> None:
    assert parse_command(command_bytes()) == Command(COMMAND_ID, "OPEN_DOOR")


def test_command_limit_and_malformed_commands() -> None:
    with pytest.raises(ValueError, match="8 KiB"):
        parse_command(b"{" + b" " * MAX_COMMAND_BYTES)
    for payload in (
        b"not-json",
        b"[]",
        command_bytes(protocol_version=2),
        command_bytes(command_id="bad"),
        command_bytes(command="FACTORY_RESET"),
    ):
        with pytest.raises(ValueError):
            parse_command(payload)


def test_safe_rejection_response() -> None:
    payload = json.loads(rejected_response(Command(COMMAND_ID, "RESTART"), DEVICE_ID))
    assert payload == {
        "protocol_version": 1,
        "device_id": DEVICE_ID,
        "command_id": COMMAND_ID,
        "command": "RESTART",
        "status": "REJECTED",
        "error_code": "COMMAND_NOT_ALLOWED",
    }
    assert "COMPLETED" not in payload.values()


def test_tls_client_id_qos_and_no_retained_messages(
    credential_files: tuple[Path, Path, Path],
) -> None:
    captured: list[str] = []
    client = FakeClient()
    device = DeviceSimulator(
        config(credential_files),
        client_factory=lambda client_id: captured.append(client_id) or client,
    )
    assert captured == [DEVICE_ID]
    tls = next(value for name, value in client.calls if name == "tls_set")
    assert tls["cert_reqs"] == ssl.CERT_REQUIRED
    assert ("tls_insecure_set", False) in client.calls
    device._on_connect(client, None, None, 0, None)
    assert ("subscribe", (device.topics.commands, 1)) in client.calls
    publishes = [value for name, value in client.calls if name == "publish"]
    assert [(item[0], item[2]) for item in publishes] == [
        (device.topics.health, 0),
        (device.topics.events, 1),
    ]
    assert all(item[3] is False for item in publishes)


def test_valid_command_is_never_executed_and_only_rejected(
    credential_files: tuple[Path, Path, Path],
) -> None:
    device, client = simulator(credential_files)
    device._on_message(client, None, SimpleNamespace(payload=command_bytes()))
    publishes = [value for name, value in client.calls if name == "publish"]
    assert len(publishes) == 1
    topic, payload, qos, retain = publishes[0]
    assert (topic, payload["status"], payload["error_code"], qos, retain) == (
        device.topics.responses,
        "REJECTED",
        "COMMAND_NOT_ALLOWED",
        1,
        False,
    )
    assert not hasattr(device, "open_door")
    assert not hasattr(device, "restart")


def test_logs_never_contain_payload_or_credentials(
    credential_files: tuple[Path, Path, Path], caplog: pytest.LogCaptureFixture
) -> None:
    device, client = simulator(credential_files)
    secret = "unknown-secret-payload"
    with caplog.at_level(logging.INFO):
        device._on_message(
            client, None, SimpleNamespace(payload=json.dumps({"unknown": secret}).encode())
        )
        device.run()
    assert secret not in caplog.text
    for path in credential_files:
        assert path.read_text(encoding="utf-8") not in caplog.text
