from __future__ import annotations

import pytest

from infrastructure.config.environment import (
    ALLOWED_COMPONENTS,
    DEFAULT_REGION,
    EnvironmentConfig,
    get_environment_config,
)


def test_default_region_is_sa_east_1(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CDK_DEFAULT_REGION", raising=False)
    config = get_environment_config()
    assert config.region == "sa-east-1"
    assert DEFAULT_REGION == "sa-east-1"


def test_region_env_var_overrides_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CDK_DEFAULT_REGION", "us-east-1")
    config = get_environment_config()
    assert config.region == "us-east-1"


def test_default_environment_is_dev(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("INTERBRIDGE_ENVIRONMENT", raising=False)
    config = get_environment_config()
    assert config.environment == "dev"


def test_invalid_environment_is_rejected() -> None:
    with pytest.raises(ValueError, match="Invalid environment"):
        EnvironmentConfig(environment="production")


def test_invalid_environment_from_env_var_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INTERBRIDGE_ENVIRONMENT", "staging")
    with pytest.raises(ValueError, match="Invalid environment"):
        get_environment_config()


def test_account_is_not_hardcoded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CDK_DEFAULT_ACCOUNT", raising=False)
    config = get_environment_config()
    assert config.account is None


def test_account_comes_only_from_cdk_default_account_env_var(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CDK_DEFAULT_ACCOUNT", "123456789012")
    config = get_environment_config()
    assert config.account == "123456789012"


def test_standard_tags_contain_required_keys() -> None:
    config = EnvironmentConfig()
    assert config.standard_tags == {
        "Project": "InterBridge",
        "Environment": "dev",
        "ManagedBy": "AWS-CDK",
        "Repository": "interBackend",
    }


@pytest.mark.parametrize("component", sorted(ALLOWED_COMPONENTS))
def test_component_tag_accepts_allowed_components(component: str) -> None:
    config = EnvironmentConfig()
    assert config.component_tag(component) == {"Component": component}


def test_component_tag_rejects_unknown_component() -> None:
    config = EnvironmentConfig()
    with pytest.raises(ValueError, match="Invalid component"):
        config.component_tag("billing")
