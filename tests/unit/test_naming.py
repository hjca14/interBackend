from __future__ import annotations

from infrastructure.config.environment import EnvironmentConfig
from infrastructure.config.naming import resource_name, stack_id


def test_resource_name_is_deterministic_and_lowercase() -> None:
    config = EnvironmentConfig()
    name = resource_name(config, "iot", "commands-rule")
    assert name == "interbridge-dev-iot-commands-rule"


def test_resource_name_is_stable_across_calls() -> None:
    config = EnvironmentConfig()
    first = resource_name(config, "api", "devices-endpoint")
    second = resource_name(config, "api", "devices-endpoint")
    assert first == second


def test_stack_id_uses_capitalized_environment() -> None:
    config = EnvironmentConfig()
    assert stack_id(config, "Data") == "InterBridge-Dev-DataStack"
    assert stack_id(config, "IoT") == "InterBridge-Dev-IoTStack"
    assert stack_id(config, "Api") == "InterBridge-Dev-ApiStack"
    assert stack_id(config, "Observability") == "InterBridge-Dev-ObservabilityStack"


def test_ingestion_stack_name() -> None:
    assert stack_id(EnvironmentConfig(), "Ingestion") == "InterBridge-Dev-IngestionStack"
