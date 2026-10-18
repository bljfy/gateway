"""Secure model adapter and explicit provider factory."""

from gateway.adapter.deepseek import DeepSeekProvider, OpenAICompatibleProvider
from gateway.adapter.provider import (
    DEFAULT_MODEL,
    AdapterConfig,
    ModelProvider,
    ModelServiceConfig,
    RoundRobinProvider,
)
from gateway.adapter.server import AdapterServer
from gateway.config import Limits


def _build_provider(config: AdapterConfig, limits: Limits, api_key: str) -> ModelProvider:
    if config.provider == "deepseek":
        return DeepSeekProvider(config, limits, api_key=api_key)
    return OpenAICompatibleProvider(config, limits, api_key=api_key)


def create_provider(config: AdapterConfig, limits: Limits) -> ModelProvider:
    services = config.service_configs()
    # Validate every credential before opening any HTTP clients.
    validated: list[tuple[AdapterConfig, str]] = []
    for service in services:
        service_config = AdapterConfig(
            provider=service.provider, base_url=service.base_url, api_key_env=service.api_key_env
        )
        validated.append((service_config, service_config.api_key()))
    if len(validated) == 1:
        service_config, api_key = validated[0]
        return _build_provider(service_config, limits, api_key)
    factories = [
        lambda service_config=service_config, api_key=api_key: _build_provider(
            service_config, limits, api_key
        )
        for service_config, api_key in validated
    ]
    return RoundRobinProvider(factories)


__all__ = [
    "DEFAULT_MODEL",
    "AdapterConfig",
    "AdapterServer",
    "DeepSeekProvider",
    "ModelServiceConfig",
    "ModelProvider",
    "OpenAICompatibleProvider",
    "RoundRobinProvider",
    "create_provider",
]
