"""Safe, computer-side MQTT/mTLS smoke-test device."""

from mqtt_smoke.config import SmokeConfig
from mqtt_smoke.device_simulator import DeviceSimulator

__all__ = ["DeviceSimulator", "SmokeConfig"]
