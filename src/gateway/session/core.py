"""Mutual SM2 authentication, directional GCM records and bounded lifecycle."""

import asyncio
import hmac
import math
import struct
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
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
from gateway.protocol import (
    SUITE,
    ZERO_REQUEST,
    decode_record,
    encode_header,
    fields,
    identity,
    parse_identity,
    read_frame,
    unfields,
    write_frame,
)


class SecurityBackend(CryptoBackend, Protocol):
    def sm3(self, message: bytes) -> bytes: ...


DEFAULT_POLICY = SessionPolicy()


async def _await_completion[T](future: asyncio.Future[T]) -> T:
    """Drain owned work even under repeated cancellation, then propagate cancellation."""
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
        self._send_lock = asyncio.Lock()
        self._recv_lock = asyncio.Lock()
        self._handshake_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._expiry_task: asyncio.Task[None] | None = None
        self._inflight: set[UUID] = set()
        self._ack = asyncio.Event()
        self._close_sent = False

    @property
    def peer(self) -> PeerIdentity:
        if self._peer is None:
            raise AuthenticationError("peer is not authenticated")
        return self._peer

    @property
    def state(self) -> SessionState:
        return self._state

    def _abort(self) -> None:
        self._keys[:] = b"\x00" * len(self._keys)
        self._keys.clear()
        self._inflight.clear()
        self._state = SessionState.CLOSED
        self._ready_pending = False
        self.writer.close()
        self.manager._sessions.discard(self)
        self.manager._ids.discard(self.session_id)
        if self._expiry_task is not None and self._expiry_task is not asyncio.current_task():
            self._expiry_task.cancel()

    def _remaining(self) -> float:
        now = self.manager.clock()
        if not math.isfinite(now) or now < self._started or now < self._last_activity:
            self._abort()
            raise SessionExpiredError("invalid session clock")
        return min(
            self._started + self._lifetime - now,
            self._last_activity + self.manager.policy.idle_timeout_seconds - now,
        )

    def _check(self, *, ready: bool = False) -> None:
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
        return await self.manager._crypto(
            lambda: self.manager.backend.hmac_sm3(key, fields(domain, transcript))
        )

    async def handshake(self) -> None:
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
                        material, transcript = await self._initiate()
                    else:
                        material, transcript = await self._accept()
                    self._keys = bytearray(material[:40])
                    material[:] = b"\x00" * len(material)
                    self._started = self._last_activity = self.manager.clock()
                    self._ready_pending = True
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
                    self.manager._trust(self.peer, outbound=self.initiator)
                    self._ready_pending = False
                    self._state = SessionState.ACTIVE
                    self._expiry_task = asyncio.create_task(self._expire())
            except BaseException:
                material[:] = b"\x00" * len(material)
                self._state = SessionState.FAILED
                self._abort()
                raise

    async def _initiate(self) -> tuple[bytearray, bytes]:
        trust = self.manager._trust(self.peer, outbound=True)
        nc = await self.manager._crypto(lambda: self.manager.backend.random_bytes(32))
        ch = b"\x01" + fields(
            b"\x01", SUITE, identity(self.manager.local.peer), identity(self.peer), nc
        )
        await write_frame(self.writer, ch)
        sh = await read_frame(self.reader)
        if sh[:1] != b"\x02":
            raise ProtocolError("expected server hello")
        body, signature = unfields(sh[1:], 2)
        ns, sid, lifetime, timeout = unfields(body, 4)
        if len(ns) != 32 or len(sid) != 16 or len(lifetime) != 4 or len(timeout) != 4:
            raise ProtocolError("invalid server parameters")
        seconds, hs_timeout = int.from_bytes(lifetime), int.from_bytes(timeout)
        if not 1 <= seconds <= self.manager.policy.absolute_lifetime_seconds:
            raise ProtocolError("unacceptable session lifetime")
        if not 1 <= hs_timeout <= self.manager.policy.handshake_timeout_seconds:
            raise ProtocolError("unacceptable handshake timeout")
        await self._verify(trust, fields(b"server-hello", await self._hash(ch), body), signature)
        self.manager._reserve_id(sid)
        self.session_id, self._lifetime = sid, float(seconds)
        material = bytearray(
            await self.manager._crypto(lambda: self.manager.backend.random_bytes(72))
        )
        try:
            encrypted = await self.manager._crypto(
                lambda: self.manager.backend.encrypt_sm2(
                    trust.encryption_public_key, bytes(material)
                )
            )
            signed = await self._sign(
                fields(b"client-key", await self._hash(fields(ch, sh)), encrypted)
            )
            ck = b"\x03" + fields(encrypted, signed)
            await write_frame(self.writer, ck)
            transcript = await self._hash(fields(ch, sh, ck))
            finish_key = bytes(material[40:72])
            sf = await read_frame(self.reader)
            if not hmac.compare_digest(
                sf, b"\x04" + await self._mac(finish_key, b"server-finished", transcript)
            ):
                raise AuthenticationError("key confirmation failed")
            await write_frame(
                self.writer, b"\x05" + await self._mac(finish_key, b"client-finished", transcript)
            )
            return material, transcript
        except BaseException:
            material[:] = b"\x00" * len(material)
            raise

    async def _accept(self) -> tuple[bytearray, bytes]:
        ch = await read_frame(self.reader)
        if ch[:1] != b"\x01":
            raise ProtocolError("expected client hello")
        version, suite, sender, target, nc = unfields(ch[1:], 5)
        if version != b"\x01" or suite != SUITE or len(nc) != 32:
            raise ProtocolError("unsupported client hello")
        remote = parse_identity(sender)
        if parse_identity(target) != self.manager.local.peer:
            raise AuthenticationError("wrong recipient")
        trust = self.manager._trust(remote, outbound=False)
        self._peer = remote
        ns = await self.manager._crypto(lambda: self.manager.backend.random_bytes(32))
        for _ in range(8):
            sid = await self.manager._crypto(lambda: self.manager.backend.random_bytes(16))
            if sid not in self.manager._ids:
                self.manager._reserve_id(sid)
                self.session_id = sid
                break
        else:
            raise CapacityError("session identifier collisions")
        body = fields(
            ns,
            sid,
            struct.pack("!I", self.manager.policy.absolute_lifetime_seconds),
            struct.pack("!I", self.manager.policy.handshake_timeout_seconds),
        )
        sh = b"\x02" + fields(
            body, await self._sign(fields(b"server-hello", await self._hash(ch), body))
        )
        await write_frame(self.writer, sh)
        ck = await read_frame(self.reader)
        if ck[:1] != b"\x03":
            raise ProtocolError("expected client key")
        encrypted, signature = unfields(ck[1:], 2)
        await self._verify(
            trust, fields(b"client-key", await self._hash(fields(ch, sh)), encrypted), signature
        )
        material = bytearray(
            await self.manager._crypto(
                lambda: self.manager.backend.decrypt_sm2(
                    self.manager.local.encryption_private_key, encrypted
                )
            )
        )
        try:
            if len(material) != 72:
                raise AuthenticationError("invalid key material")
            transcript = await self._hash(fields(ch, sh, ck))
            finish_key = bytes(material[40:72])
            await write_frame(
                self.writer, b"\x04" + await self._mac(finish_key, b"server-finished", transcript)
            )
            cf = await read_frame(self.reader)
            if not hmac.compare_digest(
                cf, b"\x05" + await self._mac(finish_key, b"client-finished", transcript)
            ):
                raise AuthenticationError("key confirmation failed")
            return material, transcript
        except BaseException:
            material[:] = b"\x00" * len(material)
            raise

    def _direction(self, sending: bool) -> Direction:
        return (
            Direction.INITIATOR_TO_ACCEPTOR
            if self.initiator == sending
            else Direction.ACCEPTOR_TO_INITIATOR
        )

    def _parameters(self, direction: Direction, sequence: int) -> tuple[bytes, bytes]:
        index = int(direction)
        return bytes(self._keys[index * 16 : index * 16 + 16]), bytes(
            self._keys[32 + index * 4 : 36 + index * 4]
        ) + struct.pack("!Q", sequence)

    def _record_policy(self, header: RecordHeader, plaintext: bytes) -> None:
        kind, rid = header.record_type, header.request_id
        if kind in (RecordType.READY, RecordType.HEARTBEAT, RecordType.CLOSE, RecordType.CLOSE_ACK):
            if rid != ZERO_REQUEST or header.chunk_index != 0 or not header.end_of_message:
                raise ProtocolError("invalid control header")
            if kind is not RecordType.READY and plaintext:
                raise ProtocolError("invalid control payload")
        elif rid == ZERO_REQUEST:
            raise ProtocolError("missing business request identity")
        if kind is RecordType.REQUEST:
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
        try:
            if record_type in (RecordType.READY, RecordType.CLOSE, RecordType.CLOSE_ACK):
                raise ProtocolError("control record is reserved")
            await self._send(record_type, plaintext, request_id, chunk_index, end_of_message)
        except BaseException:
            self._abort()
            raise

    async def _receive(self) -> VerifiedRecord:
        self._check(ready=self._ready_pending)
        async with asyncio.timeout(self._remaining()):
            data = await read_frame(self.reader)
        header, aad, encrypted = decode_record(data)
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
        self._record_policy(header, plaintext)
        self._receive_sequence += 1
        self._receive_bytes += len(plaintext)
        self._last_activity = self.manager.clock()
        return VerifiedRecord(header, plaintext)

    async def recv(self) -> VerifiedRecord:
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
        async with self._close_lock:
            if self.state is SessionState.CLOSED:
                return
            try:
                if self.state in (SessionState.ACTIVE, SessionState.DRAINING):
                    self._state = SessionState.DRAINING
                    self._close_sent = True
                    async with asyncio.timeout(min(self.manager.close_timeout, self._remaining())):
                        await self._send(RecordType.CLOSE, b"", ZERO_REQUEST, 0, True)
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

    def _trust(self, peer: PeerIdentity, *, outbound: bool) -> TrustRecord:
        entry = self.trust.get(peer.peer_id)
        now = self.wall_clock()
        pair = (self.local.peer.role, peer.role) if outbound else (peer.role, self.local.peer.role)
        allowed = {(PeerRole.CLIENT, PeerRole.GATEWAY), (PeerRole.GATEWAY, PeerRole.SIMULATOR)}
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
        if self._closed:
            raise ProtocolError("manager closed")
        active = sum(
            session.state in (SessionState.ACTIVE, SessionState.DRAINING)
            for session in self._sessions
        )
        if self._pending >= self.max_pending or active + self._pending >= self.max_active:
            raise CapacityError("session capacity exhausted")
        self._pending += 1

    async def open(self, peer: PeerIdentity) -> SecuritySession:
        self._admit()
        return await self._finish_handshake(asyncio.create_task(self._open(peer)))

    async def _finish_handshake(
        self,
        task: asyncio.Task["SecuritySession"],
        writer: asyncio.StreamWriter | None = None,
    ) -> SecuritySession:
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

    async def _open(self, peer: PeerIdentity) -> SecuritySession:
        trust = self._trust(peer, outbound=True)
        if trust.address is None:
            raise ProtocolError("trusted endpoint has no outbound address")
        session: SecuritySession | None = None
        try:
            async with asyncio.timeout(self.policy.handshake_timeout_seconds):
                reader, writer = await asyncio.open_connection(*trust.address)
                session = SecuritySession(self, reader, writer, peer)
                self._sessions.add(session)
                await session.handshake()
            if self._closed:
                raise ProtocolError("manager closed during handshake")
            return session
        except BaseException:
            if session is not None:
                session._abort()
            raise

    async def accept(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> SecuritySession:
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
                self._crypto_stopped = True
                await _await_completion(asyncio.gather(*self._native_tasks, return_exceptions=True))
