"""Command-line entry point for ``python -m mqtt_smoke``."""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

from mqtt_smoke.config import SmokeConfig
from mqtt_smoke.device_simulator import DeviceSimulator


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Safe InterBridge DEV MQTT/mTLS device simulator")
    parser.add_argument(
        "--endpoint",
        default=os.getenv("INTERBRIDGE_IOT_ENDPOINT"),
        required=os.getenv("INTERBRIDGE_IOT_ENDPOINT") is None,
    )
    parser.add_argument(
        "--device-id",
        default=os.getenv("INTERBRIDGE_DEVICE_ID"),
        required=os.getenv("INTERBRIDGE_DEVICE_ID") is None,
    )
    parser.add_argument(
        "--certificate",
        type=Path,
        default=os.getenv("INTERBRIDGE_CERTIFICATE_PATH"),
        required=os.getenv("INTERBRIDGE_CERTIFICATE_PATH") is None,
    )
    parser.add_argument(
        "--private-key",
        type=Path,
        default=os.getenv("INTERBRIDGE_PRIVATE_KEY_PATH"),
        required=os.getenv("INTERBRIDGE_PRIVATE_KEY_PATH") is None,
    )
    parser.add_argument(
        "--root-ca",
        type=Path,
        default=os.getenv("INTERBRIDGE_ROOT_CA_PATH"),
        required=os.getenv("INTERBRIDGE_ROOT_CA_PATH") is None,
    )
    parser.add_argument("--port", type=int, default=int(os.getenv("INTERBRIDGE_IOT_PORT", "8883")))
    return parser


def main() -> None:
    args = _parser().parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    DeviceSimulator(
        SmokeConfig(
            args.endpoint,
            args.device_id,
            args.certificate,
            args.private_key,
            args.root_ca,
            args.port,
        )
    ).run()


if __name__ == "__main__":
    main()
