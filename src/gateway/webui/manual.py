"""独立运行对象上的手动协议实验，故障注入不修改生产协议实现。"""

import asyncio
from collections import deque
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from uuid import UUID, uuid4

from gateway.contracts import (
    AuthenticationError,
    Direction,
    ProtocolError,
    RecordHeader,
    RecordType,
    SessionClosedError,
    SessionExpiredError,
    SessionState,
)
from gateway.crypto import GmSSLBackend
from gateway.protocol import ZERO_REQUEST, decode_record, encode_header, read_frame, write_frame
from gateway.runtime import Runtime
from gateway.session import SecuritySession
from gateway.session.core import _await_completion
from gateway.webui.settings import table

MAX_ACTIONS = 64
MAX_PACKET = 8192


def hex_bytes(value: object, size: int | None = None) -> bytes:
    if not isinstance(value, str) or len(value) > MAX_PACKET * 2:
        raise ValueError("invalid hex input")
    result = bytes.fromhex(value)
    if not result or (size is not None and len(result) != size):
        raise ValueError("invalid hex length")
    return result


def integer(value: object, maximum: int) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError("invalid integer")
    return value


@dataclass(frozen=True)
class OpenOptions:
    mode: str
    duration: int
    keys: dict[str, bytes] = field(repr=False)
    identity: str
    key_version: int
    expired_trust: bool
    fault: str

    @classmethod
    def parse(cls, data: object) -> "OpenOptions":
        values = table(
            data,
            {
                "mode",
                "duration",
                "signing_private",
                "gateway_signing_public",
                "gateway_encryption_public",
                "identity",
                "key_version",
                "expired_trust",
                "fault",
            },
        )
        mode, duration = values.get("mode", "secure"), values.get("duration", 60)
        if mode not in {"secure", "raw"} or type(duration) is not int or not 1 <= duration <= 60:
            raise ValueError("invalid connection options")
        keys = {}
        for name, size in (
            ("signing_private", 32),
            ("gateway_signing_public", 65),
            ("gateway_encryption_public", 65),
        ):
            if name in values and not isinstance(values[name], str):
                raise ValueError("invalid key input")
            if values.get(name):
                keys[name] = hex_bytes(values[name], size)
        identity = values.get("identity", "")
        if not isinstance(identity, str) or len(identity.encode()) > 64:
            raise ValueError("invalid identity")
        version = integer(values.get("key_version", 1), 2**32 - 1)
        expired = values.get("expired_trust", False)
        if version == 0 or type(expired) is not bool:
            raise ValueError("invalid trust options")
        fault = values.get("fault", "none")
        if not isinstance(fault, str) or fault not in {
            "none",
            "signing_key",
            "gateway_signing_key",
            "gateway_encryption_key",
            "hello",
            "client_key",
            "client_finished",
            "server_hello",
            "server_finished",
            "client_key_replay",
        }:
            raise ValueError("invalid handshake fault")
        return cls(mode, duration, keys, identity, version, expired, fault)


class ManualLab:
    def __init__(
        self,
        options: OpenOptions,
        changed: Callable[[], None],
        previous: bytes = b"",
        previous_key_frame: bytes = b"",
    ):
        self.options, self.changed = options, changed
        self.phase = "connecting"
        self.result: str | None = None
        self.source = ""
        self.session: SecuritySession | None = None
        self.writer: asyncio.StreamWriter | None = None
        self.runtime: Runtime | None = None
        self.captured = b""
        self.received = b""
        self.key_frame = b""
        self.previous_key_frame = previous_key_frame
        self.previous = previous
        self.key_override = bytearray()
        self.steps: deque[dict[str, object]] = deque(maxlen=MAX_ACTIONS)
        self.lock = asyncio.Lock()
        self.actions = 0
        self.proxy: asyncio.Server | None = None
        self.proxy_tasks: set[asyncio.Task[None]] = set()

    async def start_proxy(self, address: tuple[str, int]) -> tuple[str, int]:
        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            task = asyncio.current_task()
            assert task is not None
            self.proxy_tasks.add(task)
            task.add_done_callback(self.proxy_tasks.discard)
            upstream: asyncio.StreamWriter | None = None
            try:
                peer, upstream = await asyncio.open_connection(*address)
                source = upstream.get_extra_info("sockname")
                self.source = f"[{source[0]}]:{source[1]}"

                async def pump(
                    source: asyncio.StreamReader, target: asyncio.StreamWriter, mutate: bool
                ) -> None:
                    while True:
                        packet = await read_frame(source)
                        if mutate and packet[0] == 3 and self.phase == "connecting":
                            self.key_frame = packet
                            if self.options.fault == "client_key_replay":
                                packet = self.previous_key_frame
                                self.note("handshake_replayed")
                        kind = {"hello": 1, "client_key": 3, "client_finished": 5}.get(
                            self.options.fault
                        )
                        server_kind = {"server_hello": 2, "server_finished": 4}.get(
                            self.options.fault
                        )
                        if (mutate and kind is not None and packet[0] == kind) or (
                            not mutate and server_kind is not None and packet[0] == server_kind
                        ):
                            packet = packet[:-1] + bytes([packet[-1] ^ 1])
                            self.note("handshake_mutated")
                        if not mutate:
                            try:
                                decode_record(packet)
                            except ProtocolError:
                                pass
                            else:
                                self.received = packet
                        await write_frame(target, packet)

                async with asyncio.TaskGroup() as group:
                    group.create_task(pump(reader, upstream, True))
                    group.create_task(pump(peer, writer, False))
            except (Exception, asyncio.CancelledError):
                pass
            finally:
                for transport in (writer, upstream):
                    if transport:
                        transport.close()
                for transport in (writer, upstream):
                    if transport:
                        with suppress(OSError, TimeoutError):
                            await asyncio.wait_for(transport.wait_closed(), 1)
                self.proxy_tasks.discard(task)

        self.proxy = await asyncio.start_server(handle, "127.0.0.1", 0)
        return "127.0.0.1", self.proxy.sockets[0].getsockname()[1]

    def note(self, action: str, byte_count: int = 0) -> None:
        self.steps.append(
            {
                "time": datetime.now(UTC).isoformat(timespec="seconds"),
                "action": action,
                "bytes": byte_count,
            }
        )
        self.changed()

    def snapshot(self) -> dict[str, object]:
        return {
            "phase": self.phase,
            "result": self.result,
            "source": self.source,
            "mode": self.options.mode,
            "key_modified": bool(self.key_override),
            "next_sequence": str(self.session._send_sequence) if self.session else None,
            "captured_bytes": len(self.captured),
            "previous_bytes": len(self.previous),
            "received_bytes": len(self.received),
            "previous_key_frame_bytes": len(self.previous_key_frame),
            "steps": list(self.steps),
            "actions_remaining": MAX_ACTIONS - self.actions,
        }

    async def run(self, runtime: Runtime) -> None:
        self.runtime = runtime
        try:
            assert runtime.target is not None
            manager = runtime.manager
            async with asyncio.timeout(self.options.duration):
                if self.options.mode == "secure":
                    keys = self.options.keys
                    fault = self.options.fault
                    if fault in {"signing_key", "gateway_signing_key", "gateway_encryption_key"}:
                        backend = manager.backend
                        assert isinstance(backend, GmSSLBackend)
                        private, public = await manager._crypto(backend.generate_keypair)
                        name = {
                            "signing_key": "signing_private",
                            "gateway_signing_key": "gateway_signing_public",
                            "gateway_encryption_key": "gateway_encryption_public",
                        }[fault]
                        keys[name] = private if fault == "signing_key" else public
                    manager.local = replace(
                        manager.local,
                        peer=replace(
                            manager.local.peer,
                            peer_id=self.options.identity or manager.local.peer.peer_id,
                            key_version=self.options.key_version,
                        ),
                        signing_private_key=keys.get(
                            "signing_private", manager.local.signing_private_key
                        ),
                    )
                    trust = manager.trust[runtime.target.peer_id]
                    address = trust.address
                    assert address is not None
                    address = await self.start_proxy(address)
                    manager.trust[runtime.target.peer_id] = replace(
                        trust,
                        address=address,
                        signing_public_key=keys.get(
                            "gateway_signing_public", trust.signing_public_key
                        ),
                        encryption_public_key=keys.get(
                            "gateway_encryption_public", trust.encryption_public_key
                        ),
                        expires_at=1 if self.options.expired_trust else trust.expires_at,
                    )
                    self.session = await manager.open(runtime.target)
                    self.writer = self.session.writer
                    self.phase = "active"
                else:
                    address = manager.trust[runtime.target.peer_id].address
                    assert address is not None
                    async with asyncio.timeout(runtime.gateway.session.handshake_timeout_seconds):
                        reader, self.writer = await asyncio.open_connection(*address)
                    self.phase = "raw_connected"
                if not self.proxy:
                    source = self.writer.get_extra_info("sockname")
                    self.source = f"[{source[0]}]:{source[1]}"
                self.note("connected")
                if self.session:
                    while True:
                        await self.session.recv()
                        self.note("record_received")
                else:
                    while await reader.read(4096):
                        self.note("raw_data_received")
                    self.result = "peer_closed"
        except asyncio.CancelledError:
            self.result = "cancelled"
            raise
        except SessionClosedError:
            self.result = "peer_closed"
        except AuthenticationError:
            self.result = "authentication_failed"
        except (TimeoutError, SessionExpiredError):
            self.result = "timeout"
        except Exception:
            self.result = "connection_failed"
        finally:
            self.phase = "closing"
            # 自然失败后的页面断开可能再次取消 run；清理任务不继承该取消。
            await _await_completion(asyncio.create_task(self.cleanup(runtime)))

    async def cleanup(self, runtime: Runtime) -> None:
        async with self.lock:
            try:
                await runtime.manager.close()
            finally:
                try:
                    if self.writer:
                        self.writer.close()
                    if self.proxy:
                        self.proxy.close()
                        await self.proxy.wait_closed()
                    for task in tuple(self.proxy_tasks):
                        task.cancel()
                    await asyncio.gather(*tuple(self.proxy_tasks), return_exceptions=True)
                    self.proxy_tasks.clear()
                    if self.writer:
                        with suppress(OSError, TimeoutError):
                            await asyncio.wait_for(self.writer.wait_closed(), 1)
                finally:
                    self.options.keys.clear()
                    self.key_override[:] = b"\x00" * len(self.key_override)
                    self.key_override.clear()
                    self.phase = "closed"
                    self.note("finished")

    async def act(self, data: object) -> None:
        async with self.lock:
            if self.phase not in {"active", "raw_connected"} or not self.writer:
                raise ProtocolError("manual connection unavailable")
            if self.actions >= MAX_ACTIONS:
                raise ProtocolError("manual action budget exhausted")
            values = table(
                data,
                {
                    "action",
                    "key",
                    "sequence",
                    "session_id",
                    "direction",
                    "record_type",
                    "request_id",
                    "chunk_index",
                    "payload",
                    "nonce",
                    "offset",
                    "xor",
                    "raw",
                },
            )
            action = values.get("action")
            if action == "key":
                if self.session is None:
                    raise ValueError("authenticated session required")
                new_key = hex_bytes(values["key"], 16)
                self.key_override[:] = new_key
                self.actions += 1
                self.note("key_modified")
                return
            if action == "restore_key":
                self.key_override[:] = b"\x00" * len(self.key_override)
                self.key_override.clear()
                self.actions += 1
                self.note("key_restored")
                return
            if action == "abort":
                self.writer.transport.abort()
                self.actions += 1
                self.note("transport_aborted")
                return
            if action in {"replay", "cross_session"}:
                packet = self.captured if action == "replay" else self.previous
                if not packet:
                    raise ValueError("no captured record")
            elif action == "raw":
                packet = hex_bytes(values.get("raw"))
            elif action == "reflect":
                if not self.received:
                    raise ValueError("no received record")
                packet = self.received
            elif action in {"send", "tamper"}:
                if action == "tamper":
                    payload = values.get("payload", "")
                    if not isinstance(payload, str):
                        raise ValueError("invalid payload")
                    packet_length = 52 + len(payload.encode()) + 16
                    offset = integer(values.get("offset", packet_length - 1), packet_length - 1)
                    mask = integer(values.get("xor", 1), 255)
                    if mask == 0:
                        raise ValueError("empty mutation")
                packet = await self.record(values)
                if action == "tamper":
                    modified = bytearray(packet)
                    modified[offset] ^= mask
                    packet = bytes(modified)
            else:
                raise ValueError("invalid manual action")
            self.actions += 1
            async with asyncio.timeout(3):
                await write_frame(self.writer, packet)
            if action in {"send", "tamper"}:
                self.captured = packet
            self.note(str(action) + "_sent", len(packet))

    async def record(self, values: dict[str, object]) -> bytes:
        session, runtime = self.session, self.runtime
        if session is None or runtime is None or session.state is not SessionState.ACTIVE:
            raise ProtocolError("authenticated session required")
        kind = RecordType(integer(values.get("record_type", 3), 7))
        payload = values.get("payload", "")
        if not isinstance(payload, str) or len(payload.encode()) > 4096:
            raise ValueError("invalid lab payload")
        sequence = values.get("sequence", "")
        if (
            not isinstance(sequence, str)
            or len(sequence) > 20
            or (sequence and (not sequence.isascii() or not sequence.isdecimal()))
        ):
            raise ValueError("invalid sequence")
        seq = int(sequence) if sequence else session._send_sequence
        if not 0 <= seq < 2**64:
            raise ValueError("sequence out of range")
        sid = (
            hex_bytes(values["session_id"], 16) if values.get("session_id") else session.session_id
        )
        direction = Direction(integer(values.get("direction", 0), 1))
        rid = (
            UUID(str(values["request_id"]))
            if values.get("request_id")
            else (uuid4() if kind is RecordType.REQUEST else ZERO_REQUEST)
        )
        chunk = integer(values.get("chunk_index", 0), 2**32 - 1)
        # REQUEST 永远是未结束分片：可以测试请求超时／分片乱序，不触发模型调用。
        header = RecordHeader(kind, sid, direction, seq, rid, chunk, kind is not RecordType.REQUEST)
        aad = encode_header(header, len(payload.encode()) + 16)
        key, nonce = session._parameters(direction, seq)
        if self.key_override:
            key = bytes(self.key_override)
        if values.get("nonce"):
            nonce = hex_bytes(values["nonce"], 12)
        # nonce 序号在加密前消耗。自定义旧序号是本实验显式允许的攻击输入。
        session._send_sequence = max(session._send_sequence, seq + 1)
        encrypted = await runtime.manager._crypto(
            lambda: runtime.manager.backend.seal_sm4_gcm(key, nonce, payload.encode(), aad)
        )
        return aad + encrypted
