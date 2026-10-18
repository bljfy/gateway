"""Trusted local provisioning and three-program lifecycle; never accepts network keys."""

import asyncio
import getpass
import json
import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TextIO, cast
from uuid import uuid4

from gateway.adapter import AdapterConfig, AdapterServer, ModelProvider, create_provider
from gateway.adapter.server import AdapterMetrics
from gateway.audit import PrivacyAudit
from gateway.client import InferenceClient
from gateway.config import GatewayConfig, load_config, positive
from gateway.connections import ConnectionMonitor
from gateway.contracts import GatewayError, InferenceRequest, PeerIdentity, PeerRole, SecureSession
from gateway.crypto import GmSSLBackend
from gateway.logging import EventLogger
from gateway.metrics import Metrics, start_metrics_server
from gateway.server import GatewayServer
from gateway.session import LocalIdentity, SecuritySessionManager, TrustRecord

LOGGER = EventLogger(__name__)


def read_bounded(path: Path, limit: int) -> bytes:
    with path.open("rb") as source:
        data = source.read(limit + 1)
    if len(data) > limit:
        raise ValueError("local input exceeds limit")
    return data


def _table(value: object, keys: set[str]) -> dict[str, object]:
    if not isinstance(value, dict) or any(type(key) is not str for key in value):
        raise ValueError("invalid runtime table")
    table = cast(dict[str, object], value)
    if set(table) - keys:
        raise ValueError("unknown runtime field")
    return table


def _text(value: object) -> str:
    if type(value) is not str or not value:
        raise ValueError("runtime text required")
    return value


def load_backend(manifest: Path) -> GmSSLBackend:
    data = _table(
        json.loads(read_bounded(manifest, 16384)),
        {
            "source_url",
            "source_commit",
            "source_sha256",
            "binding",
            "native_version",
            "platform",
            "library",
            "sha256",
        },
    )
    library = Path(_text(data.get("library"))).resolve(strict=True)
    if os.name == "nt":
        os.environ["PATH"] = str(library.parent) + os.pathsep + os.environ.get("PATH", "")
    return GmSSLBackend(library, sha256=_text(data.get("sha256")))


@dataclass(frozen=True)
class Runtime:
    manager: SecuritySessionManager
    gateway: GatewayConfig
    listen: tuple[str, int]
    target: PeerIdentity | None
    audit_file: Path
    adapter_config: AdapterConfig | None = None


@dataclass(frozen=True, slots=True)
class RuntimeOverrides:
    """仅覆盖本地端点；身份、公钥和信任策略仍来自原配置。"""

    listen: tuple[str, int] | None = None
    target_id: str | None = None
    target_address: tuple[str, int] | None = None
    adapter_config: AdapterConfig | None = None


@dataclass(slots=True)
class RuntimeStatus:
    """进程内只读展示来源，不包含业务正文或密钥。"""

    metrics: Metrics = field(default_factory=Metrics)
    adapter: AdapterMetrics = field(default_factory=AdapterMetrics)
    audit: PrivacyAudit | None = None
    authenticated_sessions: int = 0
    connections: ConnectionMonitor = field(default_factory=ConnectionMonitor)

    def snapshot(self, runtime: Runtime) -> dict[str, object]:
        result: dict[str, object] = dict(runtime.manager.session_counts())
        result["authenticated_sessions_total"] = self.authenticated_sessions
        if runtime.manager.local.peer.role is PeerRole.GATEWAY:
            result.update(self.metrics.snapshot(self.audit))
        else:
            result.update(self.adapter.snapshot())
        return result


def load_runtime(
    path: Path, role: PeerRole, *, overrides: RuntimeOverrides | None = None
) -> Runtime:
    """Strict, size-bounded local config. All failures omit paths and key material."""
    try:
        base = path.resolve().parent
        data = _table(
            json.loads(read_bounded(path, 65536)),
            {
                "peer_id",
                "role",
                "native_manifest",
                "signing_key",
                "encryption_key",
                "password_file",
                "trust",
                "gateway_config",
                "host",
                "port",
                "target",
                "audit_file",
                "provider",
                "base_url",
                "api_key_env",
            },
        )
        if _text(data.get("role")) != role.value:
            raise ValueError("wrong runtime role")
        adapter_config = None
        provider_fields = {"provider", "base_url", "api_key_env"}
        if role is PeerRole.ADAPTER:
            defaults = AdapterConfig()
            adapter_config = AdapterConfig(
                _text(data.get("provider", defaults.provider)),
                _text(data.get("base_url", defaults.base_url)),
                _text(data.get("api_key_env", defaults.api_key_env)),
            )
        elif provider_fields & data.keys():
            raise ValueError("provider configuration requires adapter role")
        config = load_config(base / _text(data.get("gateway_config")))
        backend = load_backend(base / _text(data.get("native_manifest")))
        password = read_bounded(base / _text(data.get("password_file")), 1024)
        local = LocalIdentity(
            PeerIdentity(_text(data.get("peer_id")), role),
            backend.import_private_key(
                read_bounded(base / _text(data.get("signing_key")), 512), password
            ),
            backend.import_private_key(
                read_bounded(base / _text(data.get("encryption_key")), 512), password
            ),
        )
        entries = data.get("trust")
        if not isinstance(entries, list) or len(entries) > 64:
            raise ValueError("bounded trust list required")
        trust: dict[str, TrustRecord] = {}
        for raw in entries:
            entry = _table(
                raw,
                {
                    "peer_id",
                    "role",
                    "signing_public_key",
                    "encryption_public_key",
                    "host",
                    "port",
                    "enabled",
                    "expires_at",
                },
            )
            peer = PeerIdentity(_text(entry.get("peer_id")), PeerRole(_text(entry.get("role"))))
            if peer.peer_id in trust:
                raise ValueError("duplicate trust identity")
            enabled = entry.get("enabled", True)
            expires = entry.get("expires_at", 4102444800)
            if type(enabled) is not bool or type(expires) is not int:
                raise ValueError("invalid trust policy")
            address = None
            if "host" in entry or "port" in entry:
                address = (_text(entry.get("host")), positive(entry.get("port"), "port"))
            trust[peer.peer_id] = TrustRecord(
                peer,
                bytes.fromhex(_text(entry.get("signing_public_key"))),
                bytes.fromhex(_text(entry.get("encryption_public_key"))),
                enabled=enabled,
                expires_at=expires,
                address=address,
            )
        target_name = data.get("target")
        target = trust[_text(target_name)].peer if target_name is not None else None
        if role is PeerRole.CLIENT and (target is None or target.role is not PeerRole.GATEWAY):
            raise ValueError("client gateway target required")
        if role is PeerRole.GATEWAY and config.upstream not in [
            item.peer for item in trust.values()
        ]:
            raise ValueError("configured upstream must be trusted")
        host = _text(data.get("host", "127.0.0.1"))
        port = positive(data.get("port", 1), "port")
        if port > 65535:
            raise ValueError("invalid listen port")
        if overrides is not None:
            if overrides.listen is not None:
                if role is PeerRole.CLIENT:
                    raise ValueError("client has no listener")
                host, port = overrides.listen
            if overrides.adapter_config is not None:
                if role is not PeerRole.ADAPTER:
                    raise ValueError("provider override requires adapter")
                adapter_config = overrides.adapter_config
            if overrides.target_id is not None or overrides.target_address is not None:
                expected_role = {
                    PeerRole.CLIENT: PeerRole.GATEWAY,
                    PeerRole.GATEWAY: PeerRole.ADAPTER,
                }.get(role)
                trusted_target = trust[_text(overrides.target_id)]
                if expected_role is None or trusted_target.peer.role is not expected_role:
                    raise ValueError("invalid target role")
                if not trusted_target.enabled:
                    raise ValueError("target disabled")
                if overrides.target_address is not None:
                    trust[trusted_target.peer.peer_id] = replace(
                        trusted_target, address=overrides.target_address
                    )
                if role is PeerRole.CLIENT:
                    target = trusted_target.peer
                else:
                    config = replace(config, upstream=trusted_target.peer)
        if type(host) is not str or not host or type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("invalid listener")
        limits = config.limits
        # A's wire frame maximum is fixed at 131072; reject unsupported overrides.
        if limits.max_wire_frame_bytes != 131072:
            raise ValueError("wire frame maximum is fixed")
        manager = SecuritySessionManager(
            backend,
            local,
            trust,
            config.session,
            max_active=limits.max_active,
            max_pending=limits.max_pending,
            max_bytes=limits.max_plaintext_bytes_per_direction,
            max_inflight=limits.max_inflight_requests_per_session,
        )
        return Runtime(
            manager,
            config,
            (host, port),
            target,
            base / _text(data.get("audit_file", "audit.jsonl")),
            adapter_config,
        )
    except (ValueError, TypeError, KeyError, OSError, GatewayError):
        raise GatewayError("invalid runtime configuration") from None


def prepare_demo(
    directory: Path,
    manifest: Path,
    *,
    gateway_port: int = 18443,
    adapter_port: int = 19443,
    metrics_port: int = 19100,
) -> None:
    """Create fresh encrypted identities for an isolated local demonstration."""
    ports = (gateway_port, adapter_port, metrics_port)
    if len(set(ports)) != 3 or any(
        type(port) is not int or not 1 <= port <= 65535 for port in ports
    ):
        raise ValueError("demo ports must be distinct and valid")
    adapter_config = AdapterConfig.from_environment()
    backend = load_backend(manifest)
    directory.mkdir(mode=0o700)
    if os.name == "nt":
        subprocess.run(
            [
                "icacls",
                str(directory),
                "/inheritance:r",
                "/grant:r",
                f"{getpass.getuser()}:(OI)(CI)F",
            ],
            check=True,
            capture_output=True,
        )
    public: dict[str, tuple[bytes, bytes]] = {}
    for role in PeerRole:
        name = role.value
        password = backend.random_bytes(32).hex().encode("ascii")
        signing, signing_public = backend.generate_keypair()
        encryption, encryption_public = backend.generate_keypair()
        public[name] = signing_public, encryption_public
        for suffix, content in (
            ("password", password),
            ("sign.der", backend.export_private_key(signing, password)),
            ("encrypt.der", backend.export_private_key(encryption, password)),
        ):
            target = directory / f"{name}.{suffix}"
            target.write_bytes(content)
            target.chmod(0o600)
    (directory / "gateway.toml").write_text(
        f'[upstream]\npeer_id="adapter"\n[gateway]\nmetrics_port={metrics_port}\n'
        'log_level="INFO"\n'
        "[limits]\nmax_queued_bytes_per_session=2097152\n",
        encoding="utf-8",
    )
    for role in PeerRole:
        peers = {
            PeerRole.CLIENT: [PeerRole.GATEWAY],
            PeerRole.GATEWAY: [PeerRole.CLIENT, PeerRole.ADAPTER],
            PeerRole.ADAPTER: [PeerRole.GATEWAY],
        }[role]
        trust = []
        for peer in peers:
            item: dict[str, object] = {
                "peer_id": peer.value,
                "role": peer.value,
                "signing_public_key": public[peer.value][0].hex(),
                "encryption_public_key": public[peer.value][1].hex(),
            }
            if peer is not PeerRole.CLIENT:
                item.update(
                    host="127.0.0.1",
                    port=gateway_port if peer is PeerRole.GATEWAY else adapter_port,
                )
            trust.append(item)
        data: dict[str, object] = {
            "peer_id": role.value,
            "role": role.value,
            "native_manifest": str(manifest.resolve()),
            "signing_key": f"{role.value}.sign.der",
            "encryption_key": f"{role.value}.encrypt.der",
            "password_file": f"{role.value}.password",
            "trust": trust,
            "gateway_config": "gateway.toml",
            "host": "127.0.0.1",
            "port": adapter_port if role is PeerRole.ADAPTER else gateway_port,
            "audit_file": "audit.jsonl",
        }
        if role is PeerRole.CLIENT:
            data["target"] = "gateway"
        if role is PeerRole.ADAPTER:
            data.update(
                provider=adapter_config.provider,
                base_url=adapter_config.base_url,
                api_key_env=adapter_config.api_key_env,
            )
        (directory / f"{role.value}.json").write_text(
            json.dumps(data, indent=2) + "\n", encoding="utf-8"
        )


async def run_server(
    runtime: Runtime,
    *,
    stop: asyncio.Event | None = None,
    ready: Callable[[], None] | None = None,
    provider: ModelProvider | None = None,
    status: RuntimeStatus | None = None,
) -> None:
    manager = runtime.manager
    role = manager.local.peer.role.value
    handlers: set[asyncio.Task[None]] = set()
    metrics_server: asyncio.Server | None = None
    listener: asyncio.Server | None = None
    drain_task: asyncio.Task[None] | None = None
    audit_output: TextIO | None = None
    audit = PrivacyAudit(runtime.gateway.audit_capacity, runtime.gateway.security_audit_capacity)
    status = status if status is not None else RuntimeStatus()
    status.audit = audit
    relay = GatewayServer(manager, runtime.gateway, audit, status.metrics, status.connections)
    adapter: AdapterServer | None = None

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        current = asyncio.current_task()
        assert current is not None
        handlers.add(current)
        session: SecureSession | None = None
        try:
            if manager.local.peer.role is PeerRole.GATEWAY:
                await relay.handle(reader, writer)
            else:
                assert adapter is not None
                session = await manager.accept(reader, writer)
                status.authenticated_sessions += 1
                LOGGER.info("adapter_authenticated", role=role)
                await adapter.serve(session)
        except (GatewayError, OSError, TimeoutError):
            pass
        finally:
            if session is not None:
                await session.close()
            writer.close()
            handlers.discard(current)

    async def drain() -> None:
        assert audit_output is not None
        try:
            while True:
                audit.drain(audit_output.write)
                await asyncio.sleep(0.05)
        finally:
            audit.drain(
                audit_output.write,
                limit=runtime.gateway.audit_capacity + runtime.gateway.security_audit_capacity,
            )

    try:
        if manager.local.peer.role is PeerRole.ADAPTER:
            if provider is None:
                if runtime.adapter_config is None:
                    raise GatewayError("adapter configuration missing")
                provider = create_provider(runtime.adapter_config, runtime.gateway.limits)
            adapter = AdapterServer(provider, limits=runtime.gateway.limits, metrics=status.adapter)
        elif manager.local.peer.role is not PeerRole.GATEWAY:
            raise GatewayError("invalid server role")
        if manager.local.peer.role is PeerRole.GATEWAY:
            # Open the output before announcing readiness; failures cannot leave a live server.
            audit_output = runtime.audit_file.open("a", encoding="utf-8", buffering=1)
            drain_task = asyncio.create_task(drain())
            metrics_server = await start_metrics_server(
                relay.metrics,
                audit,
                host=runtime.gateway.metrics_host,
                port=runtime.gateway.metrics_port,
            )
        listener = await asyncio.start_server(handle, *runtime.listen)
        LOGGER.info("server_started", role=role, host=runtime.listen[0], port=runtime.listen[1])
        if ready:
            ready()
        await (stop.wait() if stop is not None else listener.serve_forever())
    except Exception:
        LOGGER.error("server_failed", role=role)
        raise
    finally:
        LOGGER.info("server_stopping", role=role)
        if listener:
            listener.close()
        if metrics_server:
            metrics_server.close()
        if manager.local.peer.role is PeerRole.GATEWAY:
            await relay.close()
        for task in tuple(handlers):
            task.cancel()
        await asyncio.gather(*handlers, return_exceptions=True)
        await manager.close()
        if drain_task:
            drain_task.cancel()
            await asyncio.gather(drain_task, return_exceptions=True)
        if audit_output:
            audit_output.close()
        if listener:
            await listener.wait_closed()
        if metrics_server:
            await metrics_server.wait_closed()
        if provider is not None:
            await provider.close()
        LOGGER.info("server_stopped", role=role)


async def run_client(
    runtime: Runtime, request: InferenceRequest, *, stream: bool, write: Callable[[str], object]
) -> None:
    session: SecureSession | None = None
    try:
        if runtime.target is None:
            raise GatewayError("client target missing")
        LOGGER.info("client_connecting", request_id=request.request_id, stream=stream)
        async with asyncio.timeout(runtime.gateway.limits.request_timeout_seconds):
            session = await runtime.manager.open(runtime.target)
            LOGGER.info("client_authenticated", request_id=request.request_id)
            client = InferenceClient(session, runtime.gateway.limits)
            if stream:
                async for chunk in client.stream(request):
                    write(chunk.output)
                write("\n")
            else:
                write((await client.complete(request)).output + "\n")
            LOGGER.info("client_completed", request_id=request.request_id)
    except Exception:
        LOGGER.error("client_failed", request_id=request.request_id)
        raise
    finally:
        if session:
            await session.close()
        await runtime.manager.close()


def request_from_file(path: Path, model: str, max_output_tokens: int) -> InferenceRequest:
    return InferenceRequest(
        uuid4(),
        model,
        read_bounded(path, 1048576).decode("utf-8"),
        max_output_tokens=max_output_tokens,
    )
