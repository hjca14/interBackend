"""MQTT 3.1.1 simulator that acknowledges commands but never executes them."""

from __future__ import annotations

import logging
import ssl
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Protocol

from mqtt_smoke.config import SmokeConfig
from mqtt_smoke.messages import (
    health_payload,
    parse_command,
    rejected_response,
    safe_event_payload,
)
from mqtt_smoke.topics import SmokeTopics, topics_for

LOGGER = logging.getLogger("mqtt_smoke")


class MqttClient(Protocol):
    on_connect: Any
    on_disconnect: Any
    on_message: Any
    on_publish: Any
    on_subscribe: Any

    def tls_set(self, **kwargs: Any) -> None: ...
    def tls_insecure_set(self, value: bool) -> None: ...
    def connect(self, host: str, port: int, keepalive: int) -> Any: ...
    def subscribe(self, topic: str, qos: int) -> Any: ...
    def publish(self, topic: str, payload: bytes, qos: int, retain: bool) -> Any: ...
    def loop_forever(self) -> Any: ...
    def disconnect(self) -> Any: ...


def _paho_client(client_id: str) -> MqttClient:
    import paho.mqtt.client as mqtt

    return mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=client_id,
        protocol=mqtt.MQTTv311,
    )


class DeviceSimulator:
    def __init__(
        self,
        config: SmokeConfig,
        *,
        client_factory: Callable[[str], MqttClient] = _paho_client,
    ) -> None:
        self.config = config
        self.topics: SmokeTopics = topics_for(config.device_id)
        self.client = client_factory(config.client_id)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_subscribe = self._on_subscribe
        self.client.on_publish = self._on_publish
        self.client.on_message = self._on_message
        self.client.tls_set(
            ca_certs=str(config.root_ca_path),
            certfile=str(config.certificate_path),
            keyfile=str(config.private_key_path),
            cert_reqs=ssl.CERT_REQUIRED,
            tls_version=ssl.PROTOCOL_TLS_CLIENT,
        )
        self.client.tls_insecure_set(False)

    def run(self) -> None:
        LOGGER.info(
            "Connecting endpoint=%s port=%d client_id=%s",
            self.config.endpoint,
            self.config.port,
            self.config.client_id,
        )
        self.client.connect(self.config.endpoint, self.config.port, keepalive=60)
        self.client.loop_forever()

    def _on_connect(
        self,
        client: MqttClient,
        _userdata: object,
        _flags: object,
        reason_code: object,
        _properties: object,
    ) -> None:
        LOGGER.info("MQTT connection acknowledged reason_code=%s", reason_code)
        client.subscribe(self.topics.commands, qos=1)
        health = health_payload(
            self.config.device_id,
            firmware_version="mqtt-smoke-1.0",
            intercom_state="IDLE",
            uptime_ms=0,
            wifi_rssi=0,
            free_heap=0,
        )
        client.publish(self.topics.health, health, qos=0, retain=False)
        client.publish(
            self.topics.events,
            safe_event_payload(self.config.device_id, timestamp=datetime.now(UTC)),
            qos=1,
            retain=False,
        )

    def _on_subscribe(
        self,
        _client: MqttClient,
        _userdata: object,
        mid: int,
        reason_codes: object,
        _properties: object,
    ) -> None:
        LOGGER.info("MQTT subscription acknowledged mid=%d qos=%s", mid, reason_codes)

    def _on_publish(
        self,
        _client: MqttClient,
        _userdata: object,
        mid: int,
        reason_code: object,
        _properties: object,
    ) -> None:
        LOGGER.info("MQTT publish acknowledged mid=%d reason_code=%s", mid, reason_code)

    def _on_disconnect(
        self,
        _client: MqttClient,
        _userdata: object,
        _flags: object,
        reason_code: object,
        _properties: object,
    ) -> None:
        LOGGER.info("MQTT disconnected reason_code=%s", reason_code)

    def _on_message(self, client: MqttClient, _userdata: object, message: Any) -> None:
        try:
            command = parse_command(bytes(message.payload))
        except ValueError as error:
            LOGGER.warning(
                "Rejected unsafe command summary=%s bytes=%d", str(error), len(message.payload)
            )
            return
        LOGGER.info(
            "Received valid command command_id=%s command=%s; execution disabled",
            command.command_id,
            command.command,
        )
        client.publish(
            self.topics.responses,
            rejected_response(command, self.config.device_id),
            qos=1,
            retain=False,
        )
