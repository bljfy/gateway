"""Supplier-neutral inference boundary and trusted adapter configuration."""

import ipaddress
import os
import re
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Protocol, runtime_checkable
from urllib.parse import urlsplit

from gateway.contracts import GatewayError, InferenceChunk, InferenceRequest, InferenceResponse

DEFAULT_MODEL = "deepseek-flash"


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

    @classmethod
    def from_environment(cls) -> "AdapterConfig":
        return cls(base_url=os.environ.get("DEEPSEEK_BASE_URL", cls().base_url))

    def __post_init__(self) -> None:
        try:
            if self.provider != "deepseek" or not re.fullmatch(
                r"[A-Za-z_][A-Za-z0-9_]*", self.api_key_env
            ):
                raise ValueError
            url = urlsplit(self.base_url)
            if not url.hostname or url.username or url.password or url.query or url.fragment:
                raise ValueError
            if url.port is not None and not 1 <= url.port <= 65535:
                raise ValueError
            if url.scheme != "https":
                # HTTP is limited to loopback API fixtures, never remote suppliers.
                if url.scheme != "http" or not ipaddress.ip_address(url.hostname).is_loopback:
                    raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise GatewayError("invalid adapter configuration") from None

    def api_key(self) -> str:
        key = os.environ.get(self.api_key_env)
        if not key or not key.strip():
            raise GatewayError("model API credential missing")
        return key
