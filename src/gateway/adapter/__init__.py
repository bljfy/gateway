"""Secure model adapter and explicit provider factory."""

from gateway.adapter.deepseek import DeepSeekProvider
from gateway.adapter.provider import DEFAULT_MODEL, AdapterConfig, ModelProvider
from gateway.adapter.server import AdapterServer
from gateway.config import Limits
from gateway.contracts import GatewayError


def create_provider(config: AdapterConfig, limits: Limits) -> ModelProvider:
    if config.provider == "deepseek":
        return DeepSeekProvider(config, limits)
    raise GatewayError("unsupported model provider")


__all__ = [
    "DEFAULT_MODEL",
    "AdapterConfig",
    "AdapterServer",
    "DeepSeekProvider",
    "ModelProvider",
    "create_provider",
]
