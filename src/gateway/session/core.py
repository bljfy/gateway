"""Mutual SM2 authentication, directional GCM records and bounded lifecycle."""

import asyncio
import json
import math
import struct
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from typing import Protocol
from uuid import UUID

from gateway.contracts import (
    AuthenticationError,
    CapacityError,
    CryptoBackend,
    Direction,
    GatewayError,
    PeerIdentity,
    PeerRole,
    ProtocolError,
    RecordHeader,
    RecordType,
    SecureSession,
    SessionClosedError,
    SessionExpiredError,
    SessionPolicy,
    SessionState,
    VerifiedRecord,
)
from gateway.logging import EventLogger
from gateway.protocol import (
    SUITE,
    ZERO_REQUEST,
    decode_record,
    encode_header,
    fields,
    identity,
    read_frame,
    write_frame,
)
from gateway.session.handshakes import ResumeMiss, accept, initiate
from gateway.session.store import Credential, ResumptionStore


class SecurityBackend(CryptoBackend, Protocol):
    def sm3(self, message: bytes) -> bytes: ...
    def public_key(self, private_key: bytes) -> bytes: ...


DEFAULT_POLICY = SessionPolicy()


async def _await_completion[T](future: asyncio.Future[T]) -> T:
    """Drain owned work even under repeated cancellation, then propagate cancellation."""
    # 取消协程不会停止已运行的原生线程；先收回任务，再向上层传播取消。
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(future)
            if cancelled:
                raise asyncio.CancelledError
            return result
        except asyncio.CancelledError:
            if future.done():
                if not future.cancelled():
                    future.exception()
                raise
            cancelled = True
        except Exception:
            if cancelled:
                raise asyncio.CancelledError from None
            raise


@dataclass(frozen=True, slots=True)
class LocalIdentity:
    """本地签名与解密身份；两种用途使用独立私钥，且 repr 不展示密钥。"""

    peer: PeerIdentity
    signing_private_key: bytes = field(repr=False)
    encryption_private_key: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if len(self.signing_private_key) != 32 or len(self.encryption_private_key) != 32:
            raise ValueError("invalid local keys")
        if self.signing_private_key == self.encryption_private_key:
            raise ValueError("signing and decryption keys must be distinct")


@dataclass(frozen=True, slots=True)
class TrustRecord:
    """由本地配置提供的可信身份、公钥、有效期和可选出站地址。"""

    peer: PeerIdentity
    signing_public_key: bytes = field(repr=False)
    encryption_public_key: bytes = field(repr=False)
    not_before: float = 0
    expires_at: float = 4_102_444_800
    enabled: bool = True
    address: tuple[str, int] | None = None

    def __post_init__(self) -> None:
        if any(
            len(key) != 65 or key[0] != 4
            for key in (self.signing_public_key, self.encryption_public_key)
        ):
            raise ValueError("invalid trusted public keys")
        if self.signing_public_key == self.encryption_public_key:
            raise ValueError("trusted keys must be distinct")
        if not all(math.isfinite(t) for t in (self.not_before, self.expires_at)):
            raise ValueError("non-finite trust validity")
        if self.not_before >= self.expires_at:
            raise ValueError("invalid trust validity")
        if self.address is not None and (not self.address[0] or not 1 <= self.address[1] <= 65535):
            raise ValueError("invalid trusted address")


class SecuritySession:
    """维护一段 TCP 连接的认证状态、双向记录序号和密钥使用预算。"""

    def __init__(
        self,
        manager: "SecuritySessionManager",
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        expected_peer: PeerIdentity | None,
    ) -> None:
        self.manager = manager
        self.reader = reader
        self.writer = writer
        self._peer = expected_peer
        self.initiator = expected_peer is not None
        self._state = SessionState.NEW
        self._ready_pending = False
        self.session_id = b""
        self._keys = bytearray()
        self._send_sequence = 0
        self._receive_sequence = 0
        self._send_bytes = 0
        self._receive_bytes = 0
        self._started = 0.0
        self._last_activity = 0.0
        self._lifetime = float(manager.policy.absolute_lifetime_seconds)
        # 同方向读写各自串行：发送不能复用序号，接收不能由多个读者争抢帧。
        self._send_lock = asyncio.Lock()
        self._recv_lock = asyncio.Lock()
        self._handshake_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._expiry_task: asyncio.Task[None] | None = None
        self._inflight: set[UUID] = set()
        self._ack = asyncio.Event()
        self._close_sent = False
        self.resumed = False
        self.attempted_resume = False
        self.force_full = False
        self.binding = b""
        self.pending_credential: Credential | None = None
        self.credential_expiry: int | None = None
        self.credential_epoch = manager._credential_epoch

    @property
    def peer(self) -> PeerIdentity:
        if self._peer is None:
            raise AuthenticationError("peer is not authenticated")
        return self._peer

    @property
    def state(self) -> SessionState:
        return self._state

    def _abort(self) -> None:
        """立即撤销会话并释放登记；擦除本会话持有的可变密钥缓冲区。"""
        self._keys[:] = b"\x00" * len(self._keys)
        self._keys.clear()
        self.pending_credential = None
        self._inflight.clear()
        self._state = SessionState.CLOSED
        self._ready_pending = False
        self.writer.close()
        self.manager._sessions.discard(self)
        self.manager._ids.discard(self.session_id)
        if self._expiry_task is not None and self._expiry_task is not asyncio.current_task():
            self._expiry_task.cancel()

    def _remaining(self) -> float:
        # 会话计时使用单调时钟，取绝对寿命和空闲寿命中更早的截止时间。
        now = self.manager.clock()
        if not math.isfinite(now) or now < self._started or now < self._last_activity:
            self._abort()
            raise SessionExpiredError("invalid session clock")
        remaining = min(
            self._started + self._lifetime - now,
            self._last_activity + self.manager.policy.idle_timeout_seconds - now,
        )
        if self.credential_expiry is not None:
            wall_now = self.manager.wall_clock()
            if not math.isfinite(wall_now):
                self._abort()
                raise SessionExpiredError("invalid session clock")
            remaining = min(remaining, self.credential_expiry - wall_now)
        return remaining

    def _check(self, *, ready: bool = False) -> None:
        # READY 是进入 ACTIVE 前的加密确认，只有握手内部可走此例外路径。
        if self.state not in (SessionState.ACTIVE, SessionState.DRAINING) and not (
            ready and self.state is SessionState.HANDSHAKING and self._ready_pending
        ):
            raise ProtocolError("session is not active")
        if self._remaining() <= 0:
            self._abort()
            raise SessionExpiredError("session expired")
        if not ready and self.manager.clock() >= (
            self._started + self._lifetime - self.manager.policy.rotate_before_seconds
        ):
            self._state = SessionState.DRAINING

    async def _expire(self) -> None:
        """即使没有业务读写，也按时进入 DRAINING 或关闭过期会话。"""
        try:
            while self.state in (SessionState.ACTIVE, SessionState.DRAINING):
                remaining = self._remaining()
                if remaining <= 0:
                    self._abort()
                    return
                rotation_in = (
                    self._started
                    + self._lifetime
                    - self.manager.policy.rotate_before_seconds
                    - self.manager.clock()
                )
                if rotation_in <= 0:
                    self._state = SessionState.DRAINING
                await asyncio.sleep(min(remaining, rotation_in) if rotation_in > 0 else remaining)
        except asyncio.CancelledError:
            pass

    async def _hash(self, message: bytes) -> bytes:
        return await self.manager._crypto(lambda: self.manager.backend.sm3(message))

    async def _sign(self, message: bytes) -> bytes:
        local = self.manager.local
        return await self.manager._crypto(
            lambda: self.manager.backend.sign_sm2(
                local.signing_private_key, message, signer_id=local.peer.peer_id.encode()
            )
        )

    async def _verify(self, trust: TrustRecord, message: bytes, signature: bytes) -> None:
        valid = await self.manager._crypto(
            lambda: self.manager.backend.verify_sm2(
                trust.signing_public_key, message, signature, signer_id=trust.peer.peer_id.encode()
            )
        )
        if not valid:
            raise AuthenticationError("peer authentication failed")

    async def _mac(self, key: bytes, domain: bytes, transcript: bytes) -> bytes:
        # 双方 Finished 使用不同域标签，避免将一个方向的确认反射到另一方向。
        return await self.manager._crypto(
            lambda: self.manager.backend.hmac_sm3(key, fields(domain, transcript))
        )

    async def handshake(self) -> None:
        """完成身份认证、密钥确认和 READY 记录交换后才允许业务读写。"""
        async with self._handshake_lock:
            if self.state is SessionState.ACTIVE:
                return
            if self.state is not SessionState.NEW:
                raise ProtocolError("handshake cannot be restarted")
            self._state = SessionState.HANDSHAKING
            material = bytearray()
            try:
                async with asyncio.timeout(self.manager.policy.handshake_timeout_seconds):
                    if self.initiator:
                        material, transcript = await initiate(self)
                    else:
                        material, transcript = await accept(self)
                    # 完整握手 104 字节、恢复握手 72 字节；会话只保留双向
                    # SM4 密钥与 nonce 前缀共 40 字节，恢复秘密独立加密保存。
                    self._keys = bytearray(material[:40])
                    material[:] = b"\x00" * len(material)
                    self._started = self._last_activity = self.manager.clock()
                    self._ready_pending = True
                    self.manager._trust(self.peer, outbound=self.initiator)
                    if self.credential_epoch != self.manager._credential_epoch:
                        raise AuthenticationError("credentials revoked during handshake")
                    if not self.initiator and self.pending_credential is not None:
                        assert self.manager.store is not None
                        self.manager.store.put(self.pending_credential)
                    if self.initiator:
                        ready = await self._receive()
                        if (
                            ready.header.record_type is not RecordType.READY
                            or ready.plaintext != transcript
                        ):
                            raise AuthenticationError("invalid ready confirmation")
                    else:
                        await self._send(RecordType.READY, transcript, ZERO_REQUEST, 0, True)
                    if self.manager._closed or self.writer.is_closing():
                        raise ProtocolError("session closed during handshake")
                    # 握手期间信任可能过期，在公开 ACTIVE 会话前再次检查。
                    self.manager._trust(self.peer, outbound=self.initiator)
                    if self.credential_epoch != self.manager._credential_epoch:
                        raise AuthenticationError("credentials revoked during handshake")
                    if self.initiator and self.pending_credential is not None:
                        assert self.manager.store is not None
                        self.manager.store.put(self.pending_credential)
                    self.pending_credential = None
                    self.manager.handshakes["resumed" if self.resumed else "full"] += 1
                    EventLogger(__name__).info(
                        "resumed_handshake_completed"
                        if self.resumed
                        else "full_handshake_completed"
                    )
                    self._ready_pending = False
                    self._state = SessionState.ACTIVE
                    self._expiry_task = asyncio.create_task(self._expire())
            except BaseException:
                if self.attempted_resume:
                    self.manager.handshakes["resume_rejected"] += 1
                    EventLogger(__name__).warning("resumption_rejected")
                material[:] = b"\x00" * len(material)
                self._state = SessionState.FAILED
                self._abort()
                raise

    def _direction(self, sending: bool) -> Direction:
        # 方向以握手发起方为基准，而不是固定按 client/gateway/adapter 角色划分。
        return (
            Direction.INITIATOR_TO_ACCEPTOR
            if self.initiator == sending
            else Direction.ACCEPTOR_TO_INITIATOR
        )

    def _parameters(self, direction: Direction, sequence: int) -> tuple[bytes, bytes]:
        """选择方向密钥，并以 4 字节方向前缀加 8 字节序号组成 GCM nonce。"""
        index = int(direction)
        return bytes(self._keys[index * 16 : index * 16 + 16]), bytes(
            self._keys[32 + index * 4 : 36 + index * 4]
        ) + struct.pack("!Q", sequence)

    def _record_policy(self, header: RecordHeader, plaintext: bytes) -> None:
        # 控制记录使用零 UUID；业务记录按 UUID 跟踪在途请求和容量。
        kind, rid = header.record_type, header.request_id
        if kind in (RecordType.READY, RecordType.HEARTBEAT, RecordType.CLOSE, RecordType.CLOSE_ACK):
            if rid != ZERO_REQUEST or header.chunk_index != 0 or not header.end_of_message:
                raise ProtocolError("invalid control header")
            if kind is not RecordType.READY and plaintext:
                raise ProtocolError("invalid control payload")
        elif rid == ZERO_REQUEST:
            raise ProtocolError("missing business request identity")
        if kind is RecordType.REQUEST:
            # DRAINING 允许已有请求的后续分片，但拒绝开始新的请求。
            if self.state is SessionState.DRAINING and rid not in self._inflight:
                raise SessionExpiredError("session is draining")
            if rid not in self._inflight and len(self._inflight) >= self.manager.max_inflight:
                raise CapacityError("inflight request limit")
            self._inflight.add(rid)
        if (
            kind in (RecordType.RESPONSE, RecordType.ERROR, RecordType.CANCEL)
            and header.end_of_message
        ):
            self._inflight.discard(rid)

    async def _send(
        self, kind: RecordType, plaintext: bytes, rid: UUID, chunk: int, end: bool
    ) -> None:
        """串行分配序号、认证头部并发送记录；失败由外层关闭会话。"""
        async with self._send_lock:
            self._check(ready=kind is RecordType.READY)
            if (
                self._send_sequence >= self.manager.policy.max_records_per_direction
                or self._send_bytes + len(plaintext) > self.manager.max_bytes
            ):
                raise SessionExpiredError("key usage budget exhausted")
            header = RecordHeader(
                kind, self.session_id, self._direction(True), self._send_sequence, rid, chunk, end
            )
            aad = encode_header(header, len(plaintext) + 16)
            self._record_policy(header, plaintext)
            key, nonce = self._parameters(header.direction, header.sequence)
            # 在密码运算前消耗序号，失败时不回退，防止同密钥 nonce 被再次使用。
            self._send_sequence += 1
            encrypted = await self.manager._crypto(
                lambda: self.manager.backend.seal_sm4_gcm(key, nonce, plaintext, aad)
            )
            self._check(ready=kind is RecordType.READY)
            async with asyncio.timeout(self._remaining()):
                await write_frame(self.writer, aad + encrypted)
            self._send_bytes += len(plaintext)
            self._last_activity = self.manager.clock()

    async def send(
        self,
        record_type: RecordType,
        plaintext: bytes,
        *,
        request_id: UUID,
        chunk_index: int = 0,
        end_of_message: bool = True,
    ) -> None:
        """发送业务或心跳记录；内部握手及关闭控制记录不能由业务层伪造。"""
        try:
            if record_type in (RecordType.READY, RecordType.CLOSE, RecordType.CLOSE_ACK):
                raise ProtocolError("control record is reserved")
            await self._send(record_type, plaintext, request_id, chunk_index, end_of_message)
        except BaseException:
            self._abort()
            raise

    async def _receive(self) -> VerifiedRecord:
        """严格校验会话、方向、序号及 GCM 标签，成功后才推进接收状态。"""
        self._check(ready=self._ready_pending)
        async with asyncio.timeout(self._remaining()):
            data = await read_frame(self.reader)
        header, aad, encrypted = decode_record(data)
        # 必须是本会话下一条预期记录，拒绝跨会话替换、反射、重放和乱序。
        if (
            header.session_id != self.session_id
            or header.direction != self._direction(False)
            or header.sequence != self._receive_sequence
        ):
            raise AuthenticationError("record context mismatch")
        if (
            self._receive_sequence >= self.manager.policy.max_records_per_direction
            or self._receive_bytes + len(encrypted) - 16 > self.manager.max_bytes
        ):
            raise SessionExpiredError("key usage budget exhausted")
        key, nonce = self._parameters(header.direction, header.sequence)
        plaintext = await self.manager._crypto(
            lambda: self.manager.backend.open_sm4_gcm(key, nonce, encrypted, aad)
        )
        self._check(ready=self._ready_pending)
        # 明文已通过 GCM 认证，才更新业务状态、序号与字节预算。
        self._record_policy(header, plaintext)
        self._receive_sequence += 1
        self._receive_bytes += len(plaintext)
        self._last_activity = self.manager.clock()
        return VerifiedRecord(header, plaintext)

    async def recv(self) -> VerifiedRecord:
        """提供唯一接收入口；处理关闭控制记录，返回已认证的业务或心跳记录。"""
        self._check()
        if self._recv_lock.locked():
            raise ProtocolError("only one receiver is permitted")
        try:
            async with self._recv_lock:
                record = await self._receive()
                kind = record.header.record_type
                if kind is RecordType.READY:
                    raise ProtocolError("unexpected ready record")
                if kind is RecordType.CLOSE:
                    await self._send(RecordType.CLOSE_ACK, b"", ZERO_REQUEST, 0, True)
                    self._abort()
                    self._ack.set()
                    raise SessionClosedError("peer closed session")
                if kind is RecordType.CLOSE_ACK:
                    if not self._close_sent:
                        raise ProtocolError("unexpected close acknowledgement")
                    self._ack.set()
                    self._abort()
                    raise SessionClosedError("session closed")
                return record
        except TimeoutError:
            self._abort()
            raise SessionExpiredError("session expired") from None
        except BaseException:
            self._abort()
            raise

    async def close(self) -> None:
        """在有限时间内尝试关闭确认，最终无论结果如何都回收会话。"""
        async with self._close_lock:
            if self.state is SessionState.CLOSED:
                return
            try:
                if self.state in (SessionState.ACTIVE, SessionState.DRAINING):
                    self._state = SessionState.DRAINING
                    self._close_sent = True
                    async with asyncio.timeout(min(self.manager.close_timeout, self._remaining())):
                        await self._send(RecordType.CLOSE, b"", ZERO_REQUEST, 0, True)
                        # 已有 recv 正在读时，由它处理 ACK，close 不再争抢同一流。
                        if self._recv_lock.locked():
                            await self._ack.wait()
                        else:
                            async with self._recv_lock:
                                response = await self._receive()
                                if response.header.record_type is RecordType.CLOSE:
                                    await self._send(
                                        RecordType.CLOSE_ACK, b"", ZERO_REQUEST, 0, True
                                    )
                                elif response.header.record_type is not RecordType.CLOSE_ACK:
                                    raise ProtocolError("expected close acknowledgement")
            except (GatewayError, OSError, TimeoutError):
                pass
            finally:
                self._abort()
                try:
                    await asyncio.wait_for(self.writer.wait_closed(), self.manager.close_timeout)
                except (OSError, TimeoutError):
                    pass


class SecuritySessionManager:
    """按本地信任配置管理会话准入、原生运算并发和所有子任务的生命周期。"""

    def __init__(
        self,
        backend: SecurityBackend,
        local: LocalIdentity,
        trust: Mapping[str, TrustRecord],
        policy: SessionPolicy = DEFAULT_POLICY,
        *,
        max_active: int = 1000,
        max_pending: int = 100,
        max_bytes: int = 1_073_741_824,
        max_inflight: int = 16,
        crypto_workers: int = 4,
        close_timeout: float = 1,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        store: ResumptionStore | None = None,
        resume_lifetime: int = 1800,
        request_timeout: int = 120,
        security_context: bytes = b"",
    ) -> None:
        if min(max_active, max_pending, max_bytes, max_inflight, crypto_workers) < 1:
            raise ValueError("resource limits must be positive")
        if not math.isfinite(close_timeout) or close_timeout <= 0:
            raise ValueError("invalid close timeout")
        if max(policy.absolute_lifetime_seconds, policy.handshake_timeout_seconds) >= 2**32:
            raise ValueError("policy exceeds wire range")
        if any(name != entry.peer.peer_id for name, entry in trust.items()):
            raise ValueError("trust index mismatch")
        self.backend, self.local, self.policy = backend, local, policy
        self._public_source = local
        self._local_public = (
            backend.public_key(local.signing_private_key),
            backend.public_key(local.encryption_private_key),
        )
        self.store = store
        self.resume_lifetime, self.request_timeout = resume_lifetime, request_timeout
        self.security_context = security_context
        self.handshakes = {"full": 0, "resumed": 0, "resume_rejected": 0}
        self._credential_epoch = 0
        self.trust = dict(trust)
        self.max_active, self.max_pending, self.max_bytes = max_active, max_pending, max_bytes
        self.max_inflight, self.close_timeout = max_inflight, close_timeout
        self.clock, self.wall_clock = clock, wall_clock
        self._workers = asyncio.Semaphore(crypto_workers)
        self._sessions: set[SecuritySession] = set()
        self._ids: set[bytes] = set()
        self._pending = 0
        self._closed = False
        self._crypto_stopped = False
        self._handshakes: set[asyncio.Task[object]] = set()
        self._native_tasks: set[asyncio.Task[object]] = set()
        self._close_lock = asyncio.Lock()

    async def _crypto[T](self, operation: Callable[[], T]) -> T:
        # 信号量限制原生运算并发；线程完成前不能释放它占用的工作名额。
        async with self._workers:
            if self._crypto_stopped:
                raise ProtocolError("manager crypto stopped")
            task = asyncio.create_task(asyncio.to_thread(operation))
            # The erased task type is only used for lifecycle tracking, never for its result.
            self._native_tasks.add(task)
            try:
                return await _await_completion(task)
            finally:
                self._native_tasks.discard(task)

    def session_counts(self) -> dict[str, int]:
        """只暴露会话数量，不暴露会话标识、密钥或记录内容。"""
        return {
            "active_sessions": sum(s.state is SessionState.ACTIVE for s in self._sessions),
            "pending_sessions": self._pending,
            "full_handshakes_total": self.handshakes["full"],
            "resumed_handshakes_total": self.handshakes["resumed"],
            "resume_rejected_total": self.handshakes["resume_rejected"],
            "credential_store_healthy": int(self.store is None or self.store.healthy),
        }

    def binding(self, peer: PeerIdentity, *, initiator: bool) -> bytes:
        """Bind credentials to public identities and both local security policies."""
        trust = self._trust(peer, outbound=initiator)
        if self.local != self._public_source:
            self._local_public = (
                self.backend.public_key(self.local.signing_private_key),
                self.backend.public_key(self.local.encryption_private_key),
            )
            self._public_source = self.local
        local = fields(
            identity(self.local.peer),
            *self._local_public,
        )
        remote = fields(identity(peer), trust.signing_public_key, trust.encryption_public_key)
        policy = json.dumps(
            dict(
                policy=asdict(self.policy),
                max_bytes=self.max_bytes,
                max_inflight=self.max_inflight,
                request_timeout=self.request_timeout,
                resume_lifetime=self.resume_lifetime,
                context=self.security_context.hex(),
            ),
            sort_keys=True,
        ).encode()
        return self.backend.sm3(
            fields(SUITE, local if initiator else remote, remote if initiator else local, policy)
        )

    async def revoke(self, peer_id: str | None = None) -> None:
        """Revoke local credentials and immediately terminate affected connections."""
        if self.store is None:
            raise GatewayError("credential storage disabled")
        self.store.revoke(peer_id)
        self._credential_epoch += 1
        for session in tuple(self._sessions):
            if peer_id is None or session._peer is None or session.peer.peer_id == peer_id:
                session._abort()

    def _trust(self, peer: PeerIdentity, *, outbound: bool) -> TrustRecord:
        # 信任有效期使用墙上时钟；角色只允许 client→gateway→adapter 两段链路。
        entry = self.trust.get(peer.peer_id)
        now = self.wall_clock()
        pair = (self.local.peer.role, peer.role) if outbound else (peer.role, self.local.peer.role)
        allowed = {(PeerRole.CLIENT, PeerRole.GATEWAY), (PeerRole.GATEWAY, PeerRole.ADAPTER)}
        if (
            entry is None
            or entry.peer != peer
            or not entry.enabled
            or not math.isfinite(now)
            or not entry.not_before <= now < entry.expires_at
            or pair not in allowed
        ):
            raise AuthenticationError("peer is not trusted")
        return entry

    def _reserve_id(self, sid: bytes) -> None:
        if len(sid) != 16 or sid in self._ids:
            raise AuthenticationError("invalid or duplicate session identifier")
        self._ids.add(sid)

    def _admit(self) -> None:
        # 未完成握手也占用总容量，避免大量半开连接绕过活动会话上限。
        if self._closed:
            raise ProtocolError("manager closed")
        if self.store is not None and not self.store.healthy:
            raise GatewayError("credential storage unhealthy")
        active = sum(
            session.state in (SessionState.ACTIVE, SessionState.DRAINING)
            for session in self._sessions
        )
        if self._pending >= self.max_pending or active + self._pending >= self.max_active:
            raise CapacityError("session capacity exhausted")
        self._pending += 1

    async def open(self, peer: PeerIdentity) -> SecuritySession:
        """连接本地信任表中的固定地址，返回已完成认证的出站会话。"""
        self._admit()
        return await self._finish_handshake(asyncio.create_task(self._open(peer)))

    async def _finish_handshake(
        self,
        task: asyncio.Task["SecuritySession"],
        writer: asyncio.StreamWriter | None = None,
    ) -> SecuritySession:
        # 完成回调统一归还握手名额，覆盖成功、异常和取消三种退出路径。
        self._handshakes.add(task)

        def finished(completed: asyncio.Task[SecuritySession]) -> None:
            self._pending -= 1
            self._handshakes.discard(completed)
            if writer is not None and (completed.cancelled() or completed.exception() is not None):
                writer.close()

        task.add_done_callback(finished)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            task.cancel()
            try:
                await _await_completion(task)
            except (asyncio.CancelledError, Exception):
                pass
            finally:
                # Cancellation can race with a successful operation whose result has
                # not reached its caller. Reclaim that session instead of orphaning it.
                if task.done() and not task.cancelled() and task.exception() is None:
                    task.result()._abort()
            raise

    async def _open(self, peer: PeerIdentity, *, force_full: bool = False) -> SecuritySession:
        trust = self._trust(peer, outbound=True)
        if trust.address is None:
            raise ProtocolError("trusted endpoint has no outbound address")
        session: SecuritySession | None = None
        try:
            async with asyncio.timeout(self.policy.handshake_timeout_seconds):
                reader, writer = await asyncio.open_connection(*trust.address)
                session = SecuritySession(self, reader, writer, peer)
                session.force_full = force_full
                self._sessions.add(session)
                await session.handshake()
            if self._closed:
                raise ProtocolError("manager closed during handshake")
            return session
        except ResumeMiss:
            if session is not None:
                session._abort()
            if force_full:
                raise
            return await self._open(peer, force_full=True)
        except BaseException:
            if session is not None:
                session._abort()
            raise

    async def accept(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> SecuritySession:
        """接收已有 TCP 连接；认证与准入失败时负责关闭该连接。"""
        try:
            self._admit()
        except BaseException:
            writer.close()
            raise
        return await self._finish_handshake(
            asyncio.create_task(self._accept(reader, writer)), writer
        )

    async def _accept(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> SecuritySession:
        session: SecuritySession | None = None
        try:
            session = SecuritySession(self, reader, writer, None)
            self._sessions.add(session)
            await session.handshake()
            if self._closed:
                raise ProtocolError("manager closed during handshake")
            return session
        except BaseException:
            if session is not None:
                session._abort()
            else:
                writer.close()
            raise

    async def rotate(self, session: SecureSession) -> SecuritySession:
        """重新握手得到独立新会话，旧会话进入 DRAINING 供已有请求收尾。"""
        if (
            not isinstance(session, SecuritySession)
            or session not in self._sessions
            or not session.initiator
        ):
            raise ProtocolError("rotation requires an owned outbound session")
        session._check()
        fresh = await self.open(session.peer)
        if session.state in (SessionState.ACTIVE, SessionState.DRAINING):
            session._state = SessionState.DRAINING
        return fresh

    async def close(self) -> None:
        """停止准入，收回握手和会话，最后等所有原生线程任务结束。"""
        async with self._close_lock:
            self._closed = True
            handshakes = tuple(
                task for task in self._handshakes if task is not asyncio.current_task()
            )
            for task in handshakes:
                task.cancel()
            try:
                await _await_completion(asyncio.gather(*handshakes, return_exceptions=True))
                await _await_completion(
                    asyncio.gather(
                        *(session.close() for session in tuple(self._sessions)),
                        return_exceptions=True,
                    )
                )
            finally:
                for session in tuple(self._sessions):
                    session._abort()
                # 关闭确认仍需要密码运算，因此在会话清理完成后才停止接收原生工作。
                self._crypto_stopped = True
                await _await_completion(asyncio.gather(*self._native_tasks, return_exceptions=True))
                if self.store is not None:
                    self.store.close()
