"""Supplier-neutral inference boundary and trusted adapter configuration."""

import asyncio
import ipaddress
import itertools
import os
import re
from collections.abc import AsyncGenerator, Callable, Sequence
from contextlib import aclosing
from dataclasses import dataclass
from typing import Protocol, runtime_checkable
from urllib.parse import urlsplit

from gateway.contracts import GatewayError, InferenceChunk, InferenceRequest, InferenceResponse

DEFAULT_MODEL = "deepseek-flash"
MAX_MODEL_SERVICES = 16


@dataclass(frozen=True, slots=True)
class ModelServiceConfig:
    """One trusted OpenAI-compatible model endpoint and its credential source."""

    name: str
    provider: str
    base_url: str
    api_key_env: str

    def __post_init__(self) -> None:
        try:
            if not isinstance(self.name, str) or not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", self.name
            ):
                raise ValueError
            if self.provider not in {"deepseek", "openai-compatible"} or not re.fullmatch(
                r"[A-Za-z_][A-Za-z0-9_]*", self.api_key_env
            ):
                raise ValueError
            url = urlsplit(self.base_url)
            if not url.hostname or url.username or url.password or url.query or url.fragment:
                raise ValueError
            if url.port is not None and not 1 <= url.port <= 65535:
                raise ValueError
            if url.scheme != "https":
                if url.scheme != "http" or not ipaddress.ip_address(url.hostname).is_loopback:
                    raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise GatewayError("invalid adapter configuration") from None


@runtime_checkable
class ModelProvider(Protocol):
    async def complete(self, request: InferenceRequest) -> InferenceResponse: ...
    def stream(self, request: InferenceRequest) -> AsyncGenerator[InferenceChunk, None]: ...
    async def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class AdapterConfig:
    provider: str = "deepseek"
    base_url: str = "https://api.deepseek.com"
    api_key_env: str = "DEEPSEEK_API_KEY"
    services: tuple[ModelServiceConfig, ...] = ()

    @classmethod
    def from_environment(cls) -> "AdapterConfig":
        return cls(base_url=os.environ.get("DEEPSEEK_BASE_URL", cls().base_url))

    def __post_init__(self) -> None:
        ModelServiceConfig("default", self.provider, self.base_url, self.api_key_env)
        if (
            not isinstance(self.services, tuple)
            or len(self.services) > MAX_MODEL_SERVICES
            or any(not isinstance(service, ModelServiceConfig) for service in self.services)
            or len({service.name for service in self.services}) != len(self.services)
        ):
            raise GatewayError("invalid adapter configuration") from None

    def service_configs(self) -> tuple[ModelServiceConfig, ...]:
        if self.services:
            return self.services
        return (ModelServiceConfig("default", self.provider, self.base_url, self.api_key_env),)

    def api_key(self) -> str:
        key = os.environ.get(self.api_key_env)
        if not key or not key.strip():
            raise GatewayError("model API credential missing")
        return key


class RoundRobinProvider:
    """Dispatch each inference once to the next configured service."""

    def __init__(self, factories: Sequence[Callable[[], ModelProvider]]) -> None:
        if not factories:
            raise ValueError("at least one model service is required")
        self._factories = tuple(factories)
        self._providers: list[ModelProvider | None] = [None] * len(factories)
        self._locks = tuple(asyncio.Lock() for _ in factories)
        self._indices = itertools.count()

    def _next(self) -> int:
        return next(self._indices) % len(self._factories)

    async def _provider(self, index: int) -> ModelProvider:
        provider = self._providers[index]
        if provider is not None:
            return provider
        async with self._locks[index]:
            provider = self._providers[index]
            if provider is None:
                try:
                    provider = self._factories[index]()
                except Exception:
                    raise GatewayError("model client initialization failed") from None
                self._providers[index] = provider
            return provider

    async def complete(self, request: InferenceRequest) -> InferenceResponse:
        return await (await self._provider(self._next())).complete(request)

    async def stream(self, request: InferenceRequest) -> AsyncGenerator[InferenceChunk, None]:
        provider = await self._provider(self._next())
        async with aclosing(provider.stream(request)) as chunks:
            async for chunk in chunks:
                yield chunk

    async def close(self) -> None:
        providers = [provider for provider in self._providers if provider is not None]
        results = await asyncio.gather(
            *(provider.close() for provider in providers), return_exceptions=True
        )
        if any(isinstance(result, BaseException) for result in results):
            raise GatewayError("model client shutdown failed") from None
