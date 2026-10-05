"""Local TCP tests with real SM2/SM4, fault injection and resource assertions."""

import asyncio
import threading
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import replace
from uuid import uuid4

import pytest

import gateway.session.core as core
from gateway.audit import PrivacyAudit
from gateway.config import GatewayConfig
from gateway.contracts import (
    AuthenticationError,
    CapacityError,
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
    SessionManager,
    SessionPolicy,
    SessionState,
)
from gateway.crypto import GmSSLBackend
from gateway.protocol import HEADER, ZERO_REQUEST, encode_header, write_frame
from gateway.server import GatewayServer
from gateway.session import LocalIdentity, SecuritySession, SecuritySessionManager, TrustRecord


class Link:
    def __init__(self, backend: GmSSLBackend, policy: SessionPolicy = core.DEFAULT_POLICY) -> None:
        client = PeerIdentity("client", PeerRole.CLIENT)
        gateway = PeerIdentity("gateway", PeerRole.GATEWAY)
        cs, csp = backend.generate_keypair()
        ce, cep = backend.generate_keypair()
        gs, gsp = backend.generate_keypair()
        ge, gep = backend.generate_keypair()
        self.client = SecuritySessionManager(
            backend,
            LocalIdentity(client, cs, ce),
            {"gateway": TrustRecord(gateway, gsp, gep)},
            policy,
            close_timeout=0.1,
        )
        self.server = SecuritySessionManager(
            backend,
            LocalIdentity(gateway, gs, ge),
            {"client": TrustRecord(client, csp, cep)},
            policy,
            close_timeout=0.1,
        )
        self.inbound: asyncio.Queue[SecuritySession | BaseException] = asyncio.Queue()
        self.tasks: set[asyncio.Task[None]] = set()

    async def accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        self.tasks.add(task)
        try:
            await self.inbound.put(await self.server.accept(reader, writer))
        except BaseException as exc:
            await self.inbound.put(exc)
        finally:
            self.tasks.discard(task)

    async def connect(self) -> tuple[SecuritySession, SecuritySession]:
        outbound = await self.client.open(self.server.local.peer)
        incoming = await asyncio.wait_for(self.inbound.get(), 3)
        if isinstance(incoming, BaseException):
            raise incoming
        return outbound, incoming


@asynccontextmanager
async def link(
    backend: GmSSLBackend, policy: SessionPolicy = core.DEFAULT_POLICY
) -> AsyncIterator[Link]:
    pair = Link(backend, policy)
    listener = await asyncio.start_server(pair.accept, "127.0.0.1", 0)
    port = listener.sockets[0].getsockname()[1]
    pair.client.trust["gateway"] = replace(
        pair.client.trust["gateway"], address=("127.0.0.1", port)
    )
    try:
        yield pair
    finally:
        listener.close()
        await asyncio.gather(pair.client.close(), pair.server.close())
        for task in tuple(pair.tasks):
            task.cancel()
        await asyncio.gather(*pair.tasks, return_exceptions=True)
        await asyncio.wait_for(listener.wait_closed(), 3)


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [1024, 16384, 65536])
async def test_mutual_handshake_roundtrip_and_close(backend: GmSSLBackend, size: int) -> None:
    async with link(backend) as pair:
        client, server = await pair.connect()
        assert isinstance(client, SecureSession) and isinstance(pair.client, SessionManager)
        assert client.state is server.state is SessionState.ACTIVE
        assert client._receive_sequence == 1 and server._send_sequence == 1
        rid = uuid4()
        payload = b"synthetic-prompt" * (size // 16) + b"x" * (size % 16)
        payload = payload.ljust(size, b"x")[:size]
        await client.send(RecordType.REQUEST, payload, request_id=rid)
        request = await server.recv()
        assert request.plaintext == payload and request.header.sequence == 0
        await server.send(RecordType.RESPONSE, payload, request_id=rid)
        response = await client.recv()
        assert response.plaintext == payload and response.header.sequence == 1
        assert not client._inflight and not server._inflight
        await asyncio.gather(client.close(), server.close())
        await client.close()
        assert client.state is server.state is SessionState.CLOSED
        assert not client._keys and not server._keys
        assert not pair.client._sessions and not pair.server._ids


def capture_records(
    monkeypatch: pytest.MonkeyPatch,
    client: SecuritySession,
    mutate: Callable[[bytes], bytes] = lambda value: value,
) -> list[bytes]:
    captured: list[bytes] = []

    async def write(writer: asyncio.StreamWriter, data: bytes) -> None:
        if writer is client.writer:
            captured.append(data)
            data = mutate(data)
        await write_frame(writer, data)

    monkeypatch.setattr(core, "write_frame", write)
    return captured


@pytest.mark.asyncio
@pytest.mark.parametrize("offset", [2, 18, 19, 28, 44, 48, HEADER.size, -1])
async def test_tampering_never_delivers_plaintext(
    backend: GmSSLBackend,
    monkeypatch: pytest.MonkeyPatch,
    offset: int,
) -> None:
    async with link(backend) as pair:
        client, server = await pair.connect()

        def mutate(data: bytes) -> bytes:
            damaged = bytearray(data)
            damaged[offset] ^= 1
            return bytes(damaged)

        capture_records(monkeypatch, client, mutate)
        await client.send(RecordType.REQUEST, b"private-marker", request_id=uuid4())
        with pytest.raises((AuthenticationError, ProtocolError)):
            await server.recv()
        assert server.state is SessionState.CLOSED and server._receive_sequence == 0
        assert not server._keys and not server._inflight


@pytest.mark.asyncio
async def test_replay_and_wire_privacy(
    backend: GmSSLBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with link(backend) as pair:
        client, server = await pair.connect()
        captured = capture_records(monkeypatch, client)
        await client.send(RecordType.REQUEST, b"private-marker", request_id=uuid4())
        assert (await server.recv()).plaintext == b"private-marker"
        assert b"private-marker" not in captured[0]
        await write_frame(client.writer, captured[0])
        with pytest.raises(AuthenticationError):
            await server.recv()
        assert server.state is SessionState.CLOSED and server._receive_sequence == 1


@pytest.mark.asyncio
async def test_cross_session_and_reflection(
    backend: GmSSLBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with link(backend) as pair:
        client, server = await pair.connect()
        captured = capture_records(monkeypatch, client)
        await client.send(RecordType.REQUEST, b"synthetic", request_id=uuid4())
        await server.recv()
        fresh, incoming = await pair.connect()
        await write_frame(fresh.writer, captured[0])
        with pytest.raises(AuthenticationError):
            await asyncio.wait_for(incoming.recv(), 2)
        await write_frame(server.writer, captured[0])
        with pytest.raises(AuthenticationError):
            await asyncio.wait_for(client.recv(), 2)


@pytest.mark.asyncio
async def test_concurrent_send_sequence_order(backend: GmSSLBackend) -> None:
    async with link(backend) as pair:
        client, server = await pair.connect()
        rid = uuid4()
        await asyncio.gather(
            *(
                client.send(
                    RecordType.REQUEST,
                    str(n).encode(),
                    request_id=rid,
                    chunk_index=n,
                    end_of_message=n == 9,
                )
                for n in range(10)
            )
        )
        records = [await server.recv() for _ in range(10)]
        assert [record.header.sequence for record in records] == list(range(10))
        assert [record.plaintext for record in records] == [str(n).encode() for n in range(10)]


@pytest.mark.asyncio
@pytest.mark.parametrize("expiry", ["idle", "absolute"])
async def test_expiry_releases_keys(backend: GmSSLBackend, expiry: str) -> None:
    async with link(backend) as pair:
        client, _ = await pair.connect()
        advance = 301 if expiry == "idle" else 1801
        pair.client.clock = lambda: client._started + advance
        with pytest.raises(SessionExpiredError):
            await client.send(RecordType.REQUEST, b"x", request_id=uuid4())
        assert client.state is SessionState.CLOSED and not client._keys


@pytest.mark.asyncio
async def test_idle_timer_closes_without_api_call(backend: GmSSLBackend) -> None:
    policy = SessionPolicy(idle_timeout_seconds=1)
    async with link(backend, policy) as pair:
        client, server = await pair.connect()
        await asyncio.sleep(1.05)
        assert client.state is server.state is SessionState.CLOSED
        assert not client._keys and not server._keys


@pytest.mark.asyncio
async def test_rotation_fresh_keys_and_existing_request_drains(backend: GmSSLBackend) -> None:
    async with link(backend) as pair:
        client, server = await pair.connect()
        rid = uuid4()
        await client.send(RecordType.REQUEST, b"x", request_id=rid)
        await server.recv()
        fresh = await pair.client.rotate(client)
        incoming = await pair.inbound.get()
        assert isinstance(incoming, SecuritySession)
        assert fresh.session_id != client.session_id and fresh._keys != client._keys
        assert client.state is SessionState.DRAINING
        await server.send(RecordType.RESPONSE, b"done", request_id=rid)
        assert (await client.recv()).plaintext == b"done"
        with pytest.raises(SessionExpiredError):
            await client.send(RecordType.REQUEST, b"new", request_id=uuid4())
        assert client.state is SessionState.CLOSED


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["disabled", "expired", "version", "role", "key"])
async def test_trust_failures(backend: GmSSLBackend, change: str) -> None:
    async with link(backend) as pair:
        trust = pair.client.trust["gateway"]
        if change == "disabled":
            trust = replace(trust, enabled=False)
        elif change == "expired":
            trust = replace(trust, expires_at=1)
        elif change == "version":
            trust = replace(trust, peer=replace(trust.peer, key_version=2))
        elif change == "role":
            trust = replace(trust, peer=replace(trust.peer, role=PeerRole.SIMULATOR))
        else:
            trust = replace(trust, signing_public_key=backend.generate_keypair()[1])
        pair.client.trust["gateway"] = trust
        with pytest.raises(AuthenticationError):
            await pair.client.open(pair.server.local.peer)
        assert not pair.client._sessions and not pair.client._ids and pair.client._pending == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("message_type", [2, 3, 4, 5])
async def test_handshake_modification_cleans_pending(
    backend: GmSSLBackend,
    monkeypatch: pytest.MonkeyPatch,
    message_type: int,
) -> None:
    async with link(backend) as pair:

        async def write(writer: asyncio.StreamWriter, data: bytes) -> None:
            if data[0] == message_type:
                data = data[:-1] + bytes([data[-1] ^ 1])
            await write_frame(writer, data)

        monkeypatch.setattr(core, "write_frame", write)
        with pytest.raises((GatewayError, OSError)):
            await pair.connect()
        await asyncio.sleep(0.02)
        assert not pair.client._sessions and not pair.server._sessions
        assert not pair.client._ids and not pair.server._ids
        assert pair.client._pending == pair.server._pending == 0


@pytest.mark.asyncio
async def test_cancel_receiver_cleans_session(backend: GmSSLBackend) -> None:
    async with link(backend) as pair:
        client, _ = await pair.connect()
        task = asyncio.create_task(client.recv())
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert client.state is SessionState.CLOSED and not client._keys


@pytest.mark.asyncio
async def test_record_budget_and_capacity(backend: GmSSLBackend) -> None:
    policy = SessionPolicy(max_records_per_direction=1)
    async with link(backend, policy) as pair:
        client, _ = await pair.connect()
        pair.client.max_active = 1
        with pytest.raises(CapacityError):
            await pair.client.open(pair.server.local.peer)
        assert pair.client._pending == 0
        await client.send(RecordType.HEARTBEAT, b"", request_id=ZERO_REQUEST)
        with pytest.raises(SessionExpiredError):
            await client.send(RecordType.HEARTBEAT, b"", request_id=ZERO_REQUEST)
        assert client.state is SessionState.CLOSED


@pytest.mark.asyncio
async def test_pending_handshake_cancellation(backend: GmSSLBackend) -> None:
    async with link(backend) as pair:
        address = pair.client.trust["gateway"].address
        assert address is not None
        _, writer = await asyncio.open_connection(*address)
        await asyncio.sleep(0.01)
        assert pair.server._pending == 1
        for task in tuple(pair.tasks):
            task.cancel()
        await asyncio.gather(*pair.tasks)
        assert not pair.server._sessions and pair.server._pending == 0
        writer.close()
        await writer.wait_closed()


@pytest.mark.asyncio
async def test_valid_tag_with_bad_control_payload_rejected(backend: GmSSLBackend) -> None:
    async with link(backend) as pair:
        client, server = await pair.connect()
        header = RecordHeader(
            RecordType.CLOSE, client.session_id, Direction.INITIATOR_TO_ACCEPTOR, 0, ZERO_REQUEST
        )
        aad = encode_header(header, 17)
        key, nonce = client._parameters(header.direction, 0)
        await write_frame(client.writer, aad + backend.seal_sm4_gcm(key, nonce, b"x", aad))
        with pytest.raises(ProtocolError):
            await server.recv()
        assert server.state is SessionState.CLOSED


@pytest.mark.asyncio
async def test_write_failure_cannot_reuse_sequence(
    backend: GmSSLBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with link(backend) as pair:
        client, _ = await pair.connect()

        async def fail(*_: object) -> None:
            raise OSError("synthetic transport failure")

        monkeypatch.setattr(core, "write_frame", fail)
        with pytest.raises(OSError):
            await client.send(RecordType.REQUEST, b"x", request_id=uuid4())
        assert client._send_sequence == 1 and not client._keys
        with pytest.raises(ProtocolError):
            await client.send(RecordType.REQUEST, b"x", request_id=uuid4())


@pytest.mark.asyncio
async def test_byte_budget_includes_ready(backend: GmSSLBackend) -> None:
    async with link(backend) as pair:
        client, server = await pair.connect()
        pair.server.max_bytes = 32
        assert server._send_bytes == 32
        with pytest.raises(SessionExpiredError):
            await server.send(RecordType.RESPONSE, b"x", request_id=uuid4())
        assert not server._keys
        await client.close()


@pytest.mark.asyncio
async def test_rotation_failure_keeps_original_deadline(backend: GmSSLBackend) -> None:
    async with link(backend) as pair:
        client, _ = await pair.connect()
        started = client._started
        pair.client.trust["gateway"] = replace(pair.client.trust["gateway"], enabled=False)
        with pytest.raises(AuthenticationError):
            await pair.client.rotate(client)
        assert client.state is SessionState.ACTIVE and client._started == started
        pair.client.clock = lambda: started + 1801
        with pytest.raises(SessionExpiredError):
            await client.recv()


@pytest.mark.asyncio
async def test_rotation_margin_and_hard_expiry(backend: GmSSLBackend) -> None:
    async with link(backend) as pair:
        client, server = await pair.connect()
        rid = uuid4()
        await client.send(RecordType.REQUEST, b"x", request_id=rid)
        await server.recv()
        client._last_activity = client._started + 1740
        pair.client.clock = lambda: client._started + 1741
        client._check()
        assert client.state is SessionState.DRAINING
        await server.send(RecordType.RESPONSE, b"done", request_id=rid)
        assert (await client.recv()).plaintext == b"done"
        pair.client.clock = lambda: client._started + 1801
        with pytest.raises(SessionExpiredError):
            await client.recv()


@pytest.mark.asyncio
async def test_handshake_timeout_and_pending_limit(backend: GmSSLBackend) -> None:
    policy = SessionPolicy(handshake_timeout_seconds=1)
    async with link(backend, policy) as pair:
        pair.server.max_pending = 1
        address = pair.client.trust["gateway"].address
        assert address is not None
        _, first_writer = await asyncio.open_connection(*address)
        await asyncio.sleep(0.01)
        second_reader, second_writer = await asyncio.open_connection(*address)
        assert await asyncio.wait_for(second_reader.read(), 1) == b""
        rejection = await asyncio.wait_for(pair.inbound.get(), 1)
        assert isinstance(rejection, CapacityError)
        timeout = await asyncio.wait_for(pair.inbound.get(), 2)
        assert isinstance(timeout, TimeoutError)
        assert pair.server._pending == 0 and not pair.server._sessions
        first_writer.close()
        second_writer.close()
        await asyncio.gather(first_writer.wait_closed(), second_writer.wait_closed())


@pytest.mark.asyncio
async def test_client_key_replay_is_not_accepted(
    backend: GmSSLBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded: list[bytes] = []
    async with link(backend) as pair:

        async def replay(writer: asyncio.StreamWriter, data: bytes) -> None:
            if data[0] == 3:
                if recorded:
                    data = recorded[0]
                else:
                    recorded.append(data)
            await write_frame(writer, data)

        monkeypatch.setattr(core, "write_frame", replay)
        client, server = await pair.connect()
        with pytest.raises(GatewayError):
            await pair.connect()
        await asyncio.sleep(0.01)
        assert client.state is server.state is SessionState.ACTIVE
        assert len(pair.client._sessions) == len(pair.server._sessions) == 1


@pytest.mark.asyncio
async def test_active_receiver_acknowledges_close(backend: GmSSLBackend) -> None:
    async with link(backend) as pair:
        client, server = await pair.connect()
        receive = asyncio.create_task(server.recv())
        await asyncio.sleep(0)
        await client.close()
        with pytest.raises(SessionClosedError, match="peer closed"):
            await receive
        assert client.state is server.state is SessionState.CLOSED


@pytest.mark.asyncio
async def test_relay_consumes_real_heartbeat_and_authenticated_close(backend: GmSSLBackend) -> None:
    async with link(backend) as pair:
        client, server = await pair.connect()
        audit = PrivacyAudit(security_capacity=1)
        relay = GatewayServer(
            pair.server, GatewayConfig(PeerIdentity("simulator", PeerRole.SIMULATOR)), audit
        )
        serving = asyncio.create_task(relay._serve(server))
        await client.send(RecordType.HEARTBEAT, b"", request_id=ZERO_REQUEST)
        await client.close()
        await asyncio.wait_for(serving, 1)
        assert client.state is server.state is SessionState.CLOSED
        assert audit.queued == 0 and not relay._audit_failed


@pytest.mark.asyncio
async def test_relay_cancellation_preserves_real_secure_send(
    backend: GmSSLBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with link(backend) as pair:
        client, server = await pair.connect()
        relay = GatewayServer(
            pair.server,
            GatewayConfig(PeerIdentity("simulator", PeerRole.SIMULATOR)),
            PrivacyAudit(),
        )
        identifier = uuid4()
        await client.send(RecordType.REQUEST, b"synthetic", request_id=identifier)
        request = await server.recv()
        response = replace(
            request,
            header=replace(
                request.header,
                record_type=RecordType.RESPONSE,
                direction=Direction.ACCEPTOR_TO_INITIATOR,
            ),
        )
        reached, release = asyncio.Event(), asyncio.Event()
        original = core.write_frame

        async def pause_send(writer: asyncio.StreamWriter, data: bytes) -> None:
            if writer is server.writer and not release.is_set():
                reached.set()
                await release.wait()
            await original(writer, data)

        monkeypatch.setattr(core, "write_frame", pause_send)
        sending = asyncio.create_task(
            relay._send_response(server, response, asyncio.get_running_loop().time() + 2, set())
        )
        try:
            await asyncio.wait_for(reached.wait(), 1)
            sending.cancel()
            await asyncio.sleep(0)
            sending.cancel()
            await asyncio.sleep(0)
            assert server.state is SessionState.ACTIVE and not sending.done()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(sending, 1)
        assert (await client.recv()).plaintext == b"synthetic"
        assert client.state is server.state is SessionState.ACTIVE
        await client.send(RecordType.REQUEST, b"next", request_id=uuid4())
        assert (await server.recv()).plaintext == b"next"


@pytest.mark.asyncio
async def test_pending_counts_against_outbound_capacity(backend: GmSSLBackend) -> None:
    async with link(backend) as pair:
        pair.client.max_active = 1
        first = asyncio.create_task(pair.client.open(pair.server.local.peer))
        await asyncio.sleep(0)
        with pytest.raises(CapacityError):
            await pair.client.open(pair.server.local.peer)
        assert (await first).state is SessionState.ACTIVE


@pytest.mark.asyncio
async def test_duplicate_session_identifier_keeps_live_session(
    backend: GmSSLBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with link(backend) as pair:
        client, server = await pair.connect()
        original = backend.random_bytes
        monkeypatch.setattr(
            backend,
            "random_bytes",
            lambda size: client.session_id if size == 16 else original(size),
        )
        with pytest.raises(GatewayError):
            await pair.connect()
        await asyncio.sleep(0.01)
        assert server.session_id in pair.server._ids
        assert client.state is server.state is SessionState.ACTIVE


@pytest.mark.asyncio
async def test_ready_must_complete_before_active(
    backend: GmSSLBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with link(backend) as pair:
        reached = asyncio.Event()
        release = asyncio.Event()
        original = core.read_frame

        async def pause_ready(reader: asyncio.StreamReader) -> bytes:
            data = await original(reader)
            if len(data) == HEADER.size + 48 and data[:2] == b"\x01\x00":
                reached.set()
                await release.wait()
            return data

        monkeypatch.setattr(core, "read_frame", pause_ready)
        opening = asyncio.create_task(pair.client.open(pair.server.local.peer))
        await asyncio.wait_for(reached.wait(), 2)
        pending = next(iter(pair.client._sessions))
        assert pending.state is SessionState.HANDSHAKING
        release.set()
        assert (await opening).state is SessionState.ACTIVE


@pytest.mark.asyncio
async def test_repeated_cancel_preserves_crypto_worker_limit(backend: GmSSLBackend) -> None:
    async with link(backend) as pair:
        pair.client._workers = asyncio.Semaphore(1)
        entered, release, second_entered = threading.Event(), threading.Event(), threading.Event()

        def blocking() -> bytes:
            entered.set()
            assert release.wait(3)
            return b"first"

        first = asyncio.create_task(pair.client._crypto(blocking))
        second: asyncio.Task[bytes] | None = None
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            first.cancel()
            await asyncio.sleep(0)
            first.cancel()
            await asyncio.sleep(0)

            def next_operation() -> bytes:
                second_entered.set()
                return b"second"

            second = asyncio.create_task(pair.client._crypto(next_operation))
            await asyncio.sleep(0.02)
            assert not second_entered.is_set() and not first.done()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert second is not None and await second == b"second"
        assert not pair.client._native_tasks


@pytest.mark.asyncio
async def test_close_cancels_pending_tcp_connection(
    backend: GmSSLBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with link(backend) as pair:
        entered = asyncio.Event()

        async def blocked(*_: object) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
            entered.set()
            await asyncio.Event().wait()
            raise AssertionError("connection must be cancelled")

        monkeypatch.setattr(asyncio, "open_connection", blocked)
        opening = asyncio.create_task(pair.client.open(pair.server.local.peer))
        await asyncio.wait_for(entered.wait(), 1)
        await pair.client.close()
        with pytest.raises(asyncio.CancelledError):
            await opening
        assert pair.client._pending == 0
        assert not pair.client._handshakes and not pair.client._sessions


@pytest.mark.asyncio
async def test_close_waits_for_native_handshake_operation(
    backend: GmSSLBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with link(backend) as pair:
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        original = backend.verify_sm2

        def blocked_verify(*args: object, **kwargs: object) -> bool:
            entered.set()
            assert release.wait(3)
            finished.set()
            return original(*args, **kwargs)

        monkeypatch.setattr(backend, "verify_sm2", blocked_verify)
        opening = asyncio.create_task(pair.client.open(pair.server.local.peer))
        closing: asyncio.Task[None] | None = None
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            closing = asyncio.create_task(pair.client.close())
            await asyncio.sleep(0.02)
            assert not closing.done() and pair.client._pending == 1
        finally:
            release.set()
        assert closing is not None
        await closing
        with pytest.raises(asyncio.CancelledError):
            await opening
        assert finished.is_set() and pair.client._pending == 0
        assert not pair.client._handshakes and not pair.client._native_tasks


@pytest.mark.asyncio
async def test_trust_expiry_before_ready_prevents_activation(
    backend: GmSSLBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with link(backend) as pair:
        pair.client.trust["gateway"] = replace(pair.client.trust["gateway"], expires_at=2)
        pair.client.wall_clock = lambda: 1
        original = core.read_frame

        async def expire_on_ready(reader: asyncio.StreamReader) -> bytes:
            data = await original(reader)
            if len(data) == HEADER.size + 48 and data[:2] == b"\x01\x00":
                pair.client.wall_clock = lambda: 3
            return data

        monkeypatch.setattr(core, "read_frame", expire_on_ready)
        with pytest.raises(AuthenticationError):
            await pair.connect()
        assert not pair.client._sessions and not pair.client._ids


@pytest.mark.asyncio
async def test_caller_finally_close_does_not_deadlock_shutdown(
    backend: GmSSLBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with link(backend) as pair:
        entered = asyncio.Event()

        async def blocked(*_: object) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
            entered.set()
            await asyncio.Event().wait()
            raise AssertionError("connection must be cancelled")

        monkeypatch.setattr(asyncio, "open_connection", blocked)

        async def service() -> None:
            try:
                await pair.client.open(pair.server.local.peer)
            finally:
                await pair.client.close()

        caller = asyncio.create_task(service())
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.wait_for(pair.client.close(), 1)
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(caller, 1)
        assert pair.client._pending == 0 and not pair.client._handshakes


@pytest.mark.asyncio
async def test_shutdown_before_owned_handshake_task_starts(backend: GmSSLBackend) -> None:
    async with link(backend) as pair:
        opening = asyncio.create_task(pair.client.open(pair.server.local.peer))
        closing = asyncio.create_task(pair.client.close())
        await asyncio.wait_for(closing, 1)
        with pytest.raises(asyncio.CancelledError):
            await opening
        assert pair.client._pending == 0 and not pair.client._handshakes


@pytest.mark.asyncio
async def test_cancel_after_operation_success_reclaims_session(
    backend: GmSSLBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with link(backend) as pair:
        original = pair.client._open
        caller: asyncio.Task[SecuritySession]

        async def cancel_at_completion(peer: PeerIdentity) -> SecuritySession:
            operation = asyncio.current_task()
            assert operation is not None
            operation.add_done_callback(lambda _: caller.cancel())
            return await original(peer)

        monkeypatch.setattr(pair.client, "_open", cancel_at_completion)
        caller = asyncio.create_task(pair.client.open(pair.server.local.peer))
        with pytest.raises(asyncio.CancelledError):
            await caller
        assert pair.client._pending == 0
        assert not pair.client._sessions and not pair.client._ids
