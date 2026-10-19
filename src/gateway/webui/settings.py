"""本地 WebUI 非敏感设置；不改写身份或信任文件。"""

import ipaddress
import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import cast

from gateway.adapter import AdapterConfig, ModelServiceConfig
from gateway.contracts import GatewayError, PeerRole
from gateway.runtime import Runtime, RuntimeOverrides, load_runtime, read_bounded


def table(value: object, allowed: set[str]) -> dict[str, object]:
    if not isinstance(value, dict) or any(type(key) is not str for key in value):
        raise ValueError("invalid object")
    result = cast(dict[str, object], value)
    if set(result) - allowed:
        raise ValueError("unknown field")
    return result


def text(value: object, *, maximum: int = 1024) -> str:
    if type(value) is not str or not value or len(value) > maximum:
        raise ValueError("invalid text")
    if any(ord(char) < 32 for char in value):
        raise ValueError("invalid text")
    return value


def port(value: object) -> int:
    if type(value) is not int or not 1 <= value <= 65535:
        raise ValueError("invalid port")
    return value


def host(value: object) -> str:
    result = text(value, maximum=253)
    # 实验端点使用 IP 字面量，防止模糊 URL、凭据和路径混入连接配置。
    ipaddress.ip_address(result)
    return result


@dataclass(frozen=True, slots=True)
class Settings:
    config_path: str
    listen_host: str = "127.0.0.1"
    listen_port: int = 18443
    target_id: str = ""
    target_host: str = "127.0.0.1"
    target_port: int = 18443
    base_url: str = "https://api.deepseek.com"
    api_key_env: str = "DEEPSEEK_API_KEY"
    services_json: str = "[]"

    @property
    def sidecar(self) -> Path:
        path = Path(self.config_path)
        return path.with_name(path.stem + ".webui.json")

    def public(self, role: PeerRole) -> dict[str, object]:
        fields = asdict(self)
        if role is PeerRole.CLIENT:
            fields.pop("listen_host")
            fields.pop("listen_port")
        if role is PeerRole.ADAPTER:
            for name in ("target_id", "target_host", "target_port"):
                fields.pop(name)
        else:
            fields.pop("base_url")
            fields.pop("api_key_env")
            fields.pop("services_json")
        return fields

    def runtime(self, role: PeerRole, *, disable_resumption: bool = False) -> Runtime:
        return load_runtime(
            Path(self.config_path),
            role,
            overrides=RuntimeOverrides(
                disable_resumption=disable_resumption,
                listen=(self.listen_host, self.listen_port)
                if role is not PeerRole.CLIENT
                else None,
                target_id=self.target_id if role is not PeerRole.ADAPTER else None,
                target_address=(self.target_host, self.target_port)
                if role is not PeerRole.ADAPTER
                else None,
                adapter_config=self.adapter_config() if role is PeerRole.ADAPTER else None,
            ),
        )

    def adapter_config(self) -> AdapterConfig:
        try:
            services = json.loads(self.services_json)
            if not isinstance(services, list):
                raise ValueError
            configured = tuple(
                ModelServiceConfig(
                    text(item["name"]),
                    text(item["provider"]),
                    text(item["base_url"]),
                    text(item["api_key_env"]),
                )
                for item in services
                if isinstance(item, dict)
                and set(item) == {"name", "provider", "base_url", "api_key_env"}
            )
            if len(configured) != len(services):
                raise ValueError
            return (
                AdapterConfig(services=configured)
                if configured
                else AdapterConfig(base_url=self.base_url, api_key_env=self.api_key_env)
            )
        except (ValueError, TypeError, KeyError):
            raise ValueError("invalid adapter configuration") from None

    def save(self, role: PeerRole) -> None:
        # 原子替换旁文件；异常时原设置保持完整。
        descriptor, temporary = tempfile.mkstemp(prefix=".webui-", dir=self.sidecar.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                json.dump(self.public(role), output, ensure_ascii=False, indent=2)
                output.write("\n")
            os.replace(temporary, self.sidecar)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    @classmethod
    def parse(cls, value: object, role: PeerRole) -> "Settings":
        allowed = set(cls("").public(role))
        if role is not PeerRole.ADAPTER:
            # 多模型提交曾将默认空数组写入 client/gateway 旁文件。
            allowed.add("services_json")
        data = table(value, allowed)
        required = allowed - {"services_json"}
        if not required <= set(data):
            raise ValueError("missing setting")
        if role is not PeerRole.ADAPTER and data.get("services_json", "[]") != "[]":
            raise ValueError("invalid adapter configuration")
        kwargs: dict[str, object] = {
            "config_path": str(Path(text(data["config_path"])).resolve()),
        }
        for name in ("listen_host", "target_host"):
            if name in data:
                kwargs[name] = host(data[name])
        for name in ("listen_port", "target_port"):
            if name in data:
                kwargs[name] = port(data[name])
        if role is PeerRole.ADAPTER:
            adapter = AdapterConfig(
                base_url=text(data["base_url"]), api_key_env=text(data["api_key_env"])
            )
            raw_services_json = data.get("services_json", "[]")
            if type(raw_services_json) is not str or len(raw_services_json) > 8192:
                raise ValueError("invalid adapter configuration")
            services_json = raw_services_json or "[]"
            kwargs.update(
                base_url=adapter.base_url,
                api_key_env=adapter.api_key_env,
                services_json=services_json,
            )
        else:
            kwargs["target_id"] = text(data["target_id"], maximum=64)
        # 参数在上方逐字段校验，使用显式构造保持严格类型检查。
        return cls(
            config_path=cast(str, kwargs["config_path"]),
            listen_host=cast(str, kwargs.get("listen_host", "127.0.0.1")),
            listen_port=cast(int, kwargs.get("listen_port", 18443)),
            target_id=cast(str, kwargs.get("target_id", "")),
            target_host=cast(str, kwargs.get("target_host", "127.0.0.1")),
            target_port=cast(int, kwargs.get("target_port", 18443)),
            base_url=cast(str, kwargs.get("base_url", "https://api.deepseek.com")),
            api_key_env=cast(str, kwargs.get("api_key_env", "DEEPSEEK_API_KEY")),
            services_json=cast(str, kwargs.get("services_json", "[]")),
        )


async def inspect_settings(settings: Settings, role: PeerRole) -> list[dict[str, object]]:
    runtime = settings.runtime(role)
    try:
        expected = PeerRole.GATEWAY if role is PeerRole.CLIENT else PeerRole.ADAPTER
        return (
            [
                {
                    "peer_id": entry.peer.peer_id,
                    "enabled": entry.enabled,
                    "host": entry.address[0] if entry.address else "",
                    "port": entry.address[1] if entry.address else 0,
                }
                for entry in runtime.manager.trust.values()
                if entry.peer.role is expected
            ]
            if role is not PeerRole.ADAPTER
            else []
        )
    finally:
        await runtime.manager.close()


async def load_settings(path: Path, role: PeerRole) -> tuple[Settings, list[dict[str, object]]]:
    runtime = load_runtime(path, role)
    try:
        target = runtime.target if role is PeerRole.CLIENT else runtime.gateway.upstream
        entry = runtime.manager.trust.get(target.peer_id) if target else None
        address = entry.address if entry and entry.address else ("127.0.0.1", 18443)
        adapter = runtime.adapter_config or AdapterConfig()
        settings = Settings(
            str(path.resolve()),
            *runtime.listen,
            target.peer_id if role is not PeerRole.ADAPTER and target else "",
            *address,
            adapter.base_url,
            adapter.api_key_env,
            json.dumps(
                [
                    {
                        "name": item.name,
                        "provider": item.provider,
                        "base_url": item.base_url,
                        "api_key_env": item.api_key_env,
                    }
                    for item in adapter.services
                ],
                ensure_ascii=False,
            ),
        )
    finally:
        await runtime.manager.close()
    settings = Settings.parse(settings.public(role), role)
    if settings.sidecar.exists():
        settings = Settings.parse(json.loads(read_bounded(settings.sidecar, 16384)), role)
        # 旁文件不能重定向到另一份身份；选取身份只能通过页面加载动作。
        if Path(settings.config_path) != path.resolve():
            raise GatewayError("invalid WebUI settings")
    return settings, await inspect_settings(settings, role)
