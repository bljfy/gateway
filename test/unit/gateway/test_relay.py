"""Contract substitutes only: these tests make no cryptographic security claim."""

import asyncio
import json
from dataclasses import replace
from uuid import UUID, uuid4

import pytest

from gateway.audit import PrivacyAudit
from gateway.config import GatewayConfig, Limits
from gateway.contracts import (
    AuditEvent,
    AuthenticationError,
    CapacityError,
    Direction,
    GatewayError,
    InferenceRequest,
    InferenceResponse,
    InferenceService,
    PeerIdentity,
    PeerRole,
    ProtocolError,
    RecordHeader,
    RecordType,
    SessionClosedError,
    SessionState,
    VerifiedRecord,
)
from gateway.server import ForwardingService, GatewayServer

UPSTREAM = PeerIdentity("configured-simulator", PeerRole.SIMULATOR)


def record(request_id, payload=b"synthetic", *, kind=RecordType.REQUEST, index=0, final=True):
    direction = (
        Direction.ACCEPTOR_TO_INITIATOR
        if kind is RecordType.RESPONSE
        else Direction.INITIATOR_TO_ACCEPTOR
    )
    return VerifiedRecord(
        RecordHeader(
            kind, b"s" * 16, direction, index, request_id, chunk_index=index, end_of_message=final
        ),
        payload,
    )


class Session:
    def __init__(self, peer, *, echo=False):
        self.peer = peer
        self.state = SessionState.ACTIVE
        self.incoming = asyncio.Queue()
        self.sent = []
        self.echo = echo
        self.closed = asyncio.Event()
        self.sent_event = asyncio.Event()
        self.send_gate = None
        self.send_started = asyncio.Event()
        self.recv_count = 0
        self.close_started = asyncio.Event()
        self.close_gate = None

    async def handshake(self):
        raise AssertionError("manager already authenticates")

    async def send(self, record_type, plaintext, *, request_id, chunk_index=0, end_of_message=True):
        if self.state is SessionState.CLOSED:
            raise ProtocolError("session closed")
        self.send_started.set()
        try:
            if self.send_gate is not None:
                await self.send_gate.wait()
        except BaseException:
            self.state = SessionState.CLOSED
            self.closed.set()
            raise
        self.sent.append((record_type, plaintext, request_id, chunk_index, end_of_message))
        self.sent_event.set()
        if self.echo and end_of_message:
            self.incoming.put_nowait(
                record(request_id, b"synthetic-output", kind=RecordType.RESPONSE)
            )

    async def recv(self):
        self.recv_count += 1
        item = await self.incoming.get()
        if isinstance(item, Exception):
            raise item
        return item

    async def close(self):
        self.close_started.set()
        if self.close_gate is not None:
            await self.close_gate.wait()
        self.state = SessionState.CLOSED
        self.closed.set()


class Writer:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class Manager:
    def __init__(self, inbound, upstream=None):
        self.inbound = inbound
        self.upstream = upstream
        self.opened = []
        self.accept_error = None
        self.accept_gate = None
        self.closed = False

    async def accept(self, reader, writer):
        if self.accept_gate is not None:
            await self.accept_gate.wait()
        if self.accept_error:
            raise self.accept_error
        return self.inbound

    async def open(self, peer):
        assert peer == UPSTREAM
        session = self.upstream or Session(UPSTREAM, echo=True)
        self.opened.append(session)
        return session

    async def rotate(self, session):
        raise AssertionError("per-request fresh upstream")

    async def close(self):
        self.closed = True


def setup(*, limits=None, audit=None, upstream=None):
    inbound = Session(PeerIdentity("authenticated-client", PeerRole.CLIENT))
    manager = Manager(inbound, upstream)
    audit = audit if audit is not None else PrivacyAudit()
    config = GatewayConfig(UPSTREAM, limits=limits or Limits())
    server = GatewayServer(manager, config, audit)
    writer = Writer()
    task = asyncio.create_task(server.handle(asyncio.StreamReader(), writer))
    return inbound, manager, audit, server, writer, task


async def stop(inbound, task):
    inbound.incoming.put_nowait(record(UUID(int=0), b"", kind=RecordType.CLOSE))
    await asyncio.wait_for(task, 2)


@pytest.mark.asyncio
async def test_verified_request_forwarded_on_distinct_fixed_upstream_and_private_audit(caplog):
    inbound, manager, audit, server, writer, task = setup()
    request_id = uuid4()
    inbound.incoming.put_nowait(record(request_id, b"sensitive-prompt-and-retrieval"))
    await asyncio.wait_for(inbound.sent_event.wait(), 2)
    await stop(inbound, task)
    assert manager.opened[0] is not inbound
    assert manager.opened[0].sent[0][1] == b"sensitive-prompt-and-retrieval"
    assert inbound.sent[0] == (RecordType.RESPONSE, b"synthetic-output", request_id, 0, True)
    assert manager.opened[0].closed.is_set() and inbound.closed.is_set() and writer.closed
    assert server.metrics.active_sessions == 0
    lines = []
    audit.drain(lines.append)
    assert "sensitive-prompt" not in "".join(lines) + caplog.text
    assert str(request_id) not in "".join(lines)
    assert any(json.loads(line)["result"] == "ok" for line in lines)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["handshake", "record", "inactive", "role"])
async def test_authentication_failure_never_opens_upstream(failure):
    inbound, manager, audit, server, writer, task = setup()
    if failure == "handshake":
        manager.accept_error = AuthenticationError("secret-key")
    elif failure == "record":
        inbound.incoming.put_nowait(AuthenticationError("secret-prompt"))
    elif failure == "inactive":
        inbound.state = SessionState.HANDSHAKING
    else:
        inbound.peer = UPSTREAM
    await asyncio.wait_for(task, 2)
    assert not manager.opened and not inbound.sent and writer.closed
    assert server.metrics.pending_sessions == 0
    lines = []
    audit.drain(lines.append)
    assert "secret" not in "".join(lines)


@pytest.mark.asyncio
async def test_fragmented_request_waits_for_end_before_forwarding():
    inbound, manager, _, _, _, task = setup()
    request_id = uuid4()
    inbound.incoming.put_nowait(record(request_id, b"first", final=False))
    for _ in range(8):
        await asyncio.sleep(0)
    assert not manager.opened
    inbound.incoming.put_nowait(record(request_id, b"last", index=1))
    await asyncio.wait_for(inbound.sent_event.wait(), 2)
    await stop(inbound, task)
    assert [item[1] for item in manager.opened[0].sent] == [b"first", b"last"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["sequence", "empty", "direction", "type", "size"])
async def test_invalid_request_does_not_reach_upstream(kind):
    limits = replace(Limits(), max_record_plaintext_bytes=8)
    inbound, manager, _, _, _, task = setup(limits=limits)
    item = record(uuid4(), b"ok")
    if kind == "sequence":
        item = replace(item, header=replace(item.header, chunk_index=1))
    elif kind == "empty":
        item = record(uuid4(), b"", final=False)
    elif kind == "direction":
        item = replace(item, header=replace(item.header, direction=Direction.ACCEPTOR_TO_INITIATOR))
    elif kind == "type":
        item = record(uuid4(), b"ok", kind=RecordType.RESPONSE)
    else:
        item = record(uuid4(), b"0123456789")
    inbound.incoming.put_nowait(item)
    await asyncio.wait_for(task, 2)
    assert not manager.opened


@pytest.mark.asyncio
async def test_full_audit_rejects_new_business():
    audit = PrivacyAudit(1)
    audit.publish(AuditEvent("request_started", "accepted"))
    inbound, manager, _, server, _, task = setup(audit=audit)
    inbound.incoming.put_nowait(record(uuid4()))
    await asyncio.wait_for(task, 2)
    assert not manager.opened
    assert b"gateway_capacity_rejected_total 1" in server.metrics.render(audit)


@pytest.mark.asyncio
async def test_cancel_stops_upstream_and_allows_next_request():
    upstream = Session(UPSTREAM)
    inbound, manager, _, _, _, task = setup(upstream=upstream)
    request_id = uuid4()
    inbound.incoming.put_nowait(record(request_id))
    await asyncio.wait_for(upstream.sent_event.wait(), 2)
    inbound.incoming.put_nowait(record(request_id, b"", kind=RecordType.CANCEL))
    await asyncio.wait_for(upstream.closed.wait(), 2)
    manager.upstream = None
    inbound.incoming.put_nowait(record(uuid4()))
    await asyncio.wait_for(inbound.sent_event.wait(), 2)
    await stop(inbound, task)
    assert len(manager.opened) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("upstream_heartbeat", [False, True])
async def test_heartbeat_preserves_request_and_response(upstream_heartbeat):
    upstream = Session(UPSTREAM)
    inbound, _, audit, _, _, task = setup(upstream=upstream)
    identifier = uuid4()
    heartbeat = record(UUID(int=0), b"", kind=RecordType.HEARTBEAT)
    if not upstream_heartbeat:
        inbound.incoming.put_nowait(heartbeat)
    inbound.incoming.put_nowait(record(identifier))
    await asyncio.wait_for(upstream.sent_event.wait(), 2)
    if upstream_heartbeat:
        upstream.incoming.put_nowait(
            replace(
                heartbeat,
                header=replace(heartbeat.header, direction=Direction.ACCEPTOR_TO_INITIATOR),
            )
        )
    upstream.incoming.put_nowait(record(identifier, b"ok", kind=RecordType.RESPONSE))
    await asyncio.wait_for(inbound.sent_event.wait(), 2)
    await stop(inbound, task)
    assert inbound.sent[0][1] == b"ok"
    lines = []
    audit.drain(lines.append)
    assert not any("protocol_rejected" in line for line in lines)


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["id", "payload", "index", "final"])
async def test_invalid_heartbeat_is_still_rejected(invalid):
    inbound, _, audit, _, _, task = setup()
    heartbeat = record(UUID(int=0), b"", kind=RecordType.HEARTBEAT)
    if invalid == "id":
        heartbeat = replace(heartbeat, header=replace(heartbeat.header, request_id=uuid4()))
    elif invalid == "payload":
        heartbeat = replace(heartbeat, plaintext=b"invalid")
    elif invalid == "index":
        heartbeat = replace(heartbeat, header=replace(heartbeat.header, chunk_index=1))
    else:
        heartbeat = replace(heartbeat, header=replace(heartbeat.header, end_of_message=False))
    inbound.incoming.put_nowait(heartbeat)
    await asyncio.wait_for(task, 2)
    lines = []
    audit.drain(lines.append)
    assert any("protocol_rejected" in line for line in lines)


@pytest.mark.asyncio
async def test_heartbeats_do_not_extend_request_deadline():
    inbound, _, _, _, _, task = setup(limits=replace(Limits(), request_timeout_seconds=1))
    inbound.incoming.put_nowait(record(uuid4(), b"first", final=False))
    async with asyncio.timeout(2):
        while not task.done():
            inbound.incoming.put_nowait(record(UUID(int=0), b"", kind=RecordType.HEARTBEAT))
            await asyncio.sleep(0.02)
    await task
    assert inbound.closed.is_set()


@pytest.mark.asyncio
async def test_authenticated_close_does_not_consume_security_capacity():
    audit = PrivacyAudit(security_capacity=1)
    inbound, manager, _, server, _, task = setup(audit=audit)
    for _ in range(3):
        inbound.incoming.put_nowait(SessionClosedError("peer closed session"))
        await asyncio.wait_for(task, 2)
        assert audit.queued == 0 and not server._audit_failed
        inbound = Session(PeerIdentity("authenticated-client", PeerRole.CLIENT))
        manager.inbound = inbound
        task = asyncio.create_task(server.handle(asyncio.StreamReader(), Writer()))
    inbound.incoming.put_nowait(record(uuid4()))
    await asyncio.wait_for(inbound.sent_event.wait(), 2)
    await stop(inbound, task)
    assert len(manager.opened) == 1


@pytest.mark.asyncio
async def test_cancel_during_send_preserves_concurrent_and_subsequent_requests():
    inbound, manager, _, _, _, task = setup()
    inbound.send_gate = asyncio.Event()
    first, second, third = uuid4(), uuid4(), uuid4()
    inbound.incoming.put_nowait(record(first))
    await asyncio.wait_for(inbound.send_started.wait(), 2)
    inbound.incoming.put_nowait(record(second))
    inbound.incoming.put_nowait(record(first, b"", kind=RecordType.CANCEL))
    for _ in range(20):
        await asyncio.sleep(0)
    assert inbound.state is SessionState.ACTIVE
    inbound.send_gate.set()
    async with asyncio.timeout(2):
        while not any(sent[2] == second for sent in inbound.sent):
            await asyncio.sleep(0)
    inbound.incoming.put_nowait(record(third))
    async with asyncio.timeout(2):
        while not any(sent[2] == third for sent in inbound.sent):
            await asyncio.sleep(0)
    assert inbound.state is SessionState.ACTIVE
    await stop(inbound, task)
    assert len(manager.opened) == 3 and all(session.closed.is_set() for session in manager.opened)


@pytest.mark.asyncio
async def test_repeated_cancel_drains_one_send_without_orphaning_it():
    inbound = Session(PeerIdentity("authenticated-client", PeerRole.CLIENT))
    inbound.send_gate = asyncio.Event()
    server = GatewayServer(Manager(inbound), GatewayConfig(UPSTREAM), PrivacyAudit())
    sending = asyncio.create_task(
        server._send_response(
            inbound,
            record(uuid4(), kind=RecordType.RESPONSE),
            asyncio.get_running_loop().time() + 2,
            set(),
        )
    )
    await asyncio.wait_for(inbound.send_started.wait(), 1)
    sending.cancel()
    await asyncio.sleep(0)
    sending.cancel()
    await asyncio.sleep(0)
    assert not sending.done() and inbound.state is SessionState.ACTIVE
    inbound.send_gate.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(sending, 1)
    assert len(inbound.sent) == 1 and inbound.state is SessionState.ACTIVE


@pytest.mark.asyncio
async def test_cancelled_send_still_has_a_hard_deadline():
    inbound = Session(PeerIdentity("authenticated-client", PeerRole.CLIENT))
    inbound.send_gate = asyncio.Event()
    server = GatewayServer(Manager(inbound), GatewayConfig(UPSTREAM), PrivacyAudit())
    sending = asyncio.create_task(
        server._send_response(
            inbound,
            record(uuid4(), kind=RecordType.RESPONSE),
            asyncio.get_running_loop().time() + 0.1,
            set(),
        )
    )
    await asyncio.wait_for(inbound.send_started.wait(), 1)
    sending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(sending, 1)
    assert inbound.closed.is_set() and not inbound.sent


@pytest.mark.asyncio
async def test_cancel_drain_does_not_delay_an_earlier_incomplete_request_deadline():
    inbound, _, _, _, _, task = setup(limits=replace(Limits(), request_timeout_seconds=1))
    inbound.send_gate = asyncio.Event()
    inbound.incoming.put_nowait(record(uuid4(), b"first", final=False))
    await asyncio.sleep(0.6)
    identifier = uuid4()
    inbound.incoming.put_nowait(record(identifier))
    await asyncio.wait_for(inbound.send_started.wait(), 0.2)
    inbound.incoming.put_nowait(record(identifier, b"", kind=RecordType.CANCEL))
    await asyncio.wait_for(task, 0.7)
    assert inbound.closed.is_set()


@pytest.mark.asyncio
async def test_shutdown_interrupts_a_cancelled_blocked_response_send():
    inbound, _, _, server, writer, task = setup()
    inbound.send_gate = asyncio.Event()
    identifier = uuid4()
    inbound.incoming.put_nowait(record(identifier))
    await asyncio.wait_for(inbound.send_started.wait(), 1)
    inbound.incoming.put_nowait(record(identifier, b"", kind=RecordType.CANCEL))
    for _ in range(20):
        await asyncio.sleep(0)
    await asyncio.wait_for(server.close(), 1)
    assert task.cancelled() and writer.closed and inbound.closed.is_set()
    assert server._outbound == server._active == 0 and not server._connections


@pytest.mark.asyncio
async def test_timeout_closes_both_links_without_retry():
    upstream = Session(UPSTREAM)
    inbound, manager, _, _, writer, task = setup(
        limits=replace(Limits(), request_timeout_seconds=1), upstream=upstream
    )
    inbound.incoming.put_nowait(record(uuid4()))
    await asyncio.wait_for(task, 3)
    assert len(manager.opened) == 1 and not inbound.sent
    assert upstream.closed.is_set() and inbound.closed.is_set() and writer.closed


@pytest.mark.asyncio
async def test_slow_client_backpressures_response_reader():
    upstream = Session(UPSTREAM)
    inbound, _, _, _, _, task = setup(upstream=upstream)
    inbound.send_gate = asyncio.Event()
    request_id = uuid4()
    inbound.incoming.put_nowait(record(request_id))
    upstream.incoming.put_nowait(
        record(request_id, b"first", kind=RecordType.RESPONSE, final=False)
    )
    upstream.incoming.put_nowait(record(request_id, b"last", kind=RecordType.RESPONSE, index=1))
    await asyncio.wait_for(upstream.sent_event.wait(), 2)
    for _ in range(8):
        await asyncio.sleep(0)
    assert upstream.recv_count == 1 and not inbound.sent
    inbound.send_gate.set()
    await asyncio.wait_for(inbound.sent_event.wait(), 2)
    await stop(inbound, task)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["wrong_id", "wrong_index", "authentication", "truncated"])
async def test_bad_upstream_never_sends_success(failure):
    upstream = Session(UPSTREAM)
    inbound, _, _, _, _, task = setup(upstream=upstream)
    request_id = uuid4()
    inbound.incoming.put_nowait(record(request_id))
    if failure == "wrong_id":
        upstream.incoming.put_nowait(record(uuid4(), kind=RecordType.RESPONSE))
    elif failure == "wrong_index":
        upstream.incoming.put_nowait(record(request_id, kind=RecordType.RESPONSE, index=1))
    elif failure == "authentication":
        upstream.incoming.put_nowait(AuthenticationError("synthetic-secret"))
    else:
        upstream.incoming.put_nowait(record(request_id, kind=RecordType.RESPONSE, final=False))
        upstream.incoming.put_nowait(EOFError("synthetic-secret"))
    await asyncio.wait_for(task, 2)
    assert all(not sent[4] for sent in inbound.sent)
    assert upstream.closed.is_set()


@pytest.mark.asyncio
async def test_shutdown_cancels_pending_handshake_and_closes_manager():
    inbound, manager, _, server, writer, task = setup()
    manager.accept_gate = asyncio.Event()
    await asyncio.sleep(0)
    await server.close()
    assert task.cancelled() and manager.closed and writer.closed
    assert server.metrics.pending_sessions == 0


@pytest.mark.asyncio
async def test_pending_capacity_rejects_before_accept():
    _, manager, _, server, _, task = setup(limits=replace(Limits(), max_pending=1))
    manager.accept_gate = asyncio.Event()
    await asyncio.sleep(0)
    rejected = Writer()
    await server.handle(asyncio.StreamReader(), rejected)
    assert rejected.closed
    await server.close()
    assert task.cancelled()


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["queue", "body", "inflight"])
async def test_request_resource_limits_release_all_buffers(boundary):
    limits = replace(
        Limits(),
        max_record_plaintext_bytes=8,
        max_request_body_bytes=16,
        max_queued_bytes_per_session=300,
        max_inflight_requests_per_session=1,
    )
    inbound, manager, _, _, _, task = setup(limits=limits)
    request_id = uuid4()
    inbound.incoming.put_nowait(record(request_id, b"12345678", final=False))
    if boundary == "inflight":
        inbound.incoming.put_nowait(record(uuid4(), b"1"))
    else:
        inbound.incoming.put_nowait(record(request_id, b"12345678", index=1, final=False))
        inbound.incoming.put_nowait(
            record(request_id, b"1" if boundary == "body" else b"", index=2)
        )
    await asyncio.wait_for(task, 2)
    assert not manager.opened and inbound.closed.is_set()


@pytest.mark.asyncio
async def test_response_total_limit_no_final_success():
    limits = replace(Limits(), max_record_plaintext_bytes=8, max_response_body_bytes=8)
    upstream = Session(UPSTREAM)
    inbound, _, _, _, _, task = setup(limits=limits, upstream=upstream)
    request_id = uuid4()
    inbound.incoming.put_nowait(record(request_id, b"r"))
    upstream.incoming.put_nowait(
        record(request_id, b"12345678", kind=RecordType.RESPONSE, final=False)
    )
    upstream.incoming.put_nowait(record(request_id, b"9", kind=RecordType.RESPONSE, index=1))
    await asyncio.wait_for(task, 2)
    assert len(inbound.sent) == 1 and not inbound.sent[0][4]
    assert upstream.closed.is_set()


@pytest.mark.asyncio
async def test_failed_terminal_audit_blocks_further_admission():
    inbound, manager, audit, server, _, task = setup(audit=PrivacyAudit(1))
    inbound.incoming.put_nowait(record(uuid4()))
    await asyncio.wait_for(inbound.sent_event.wait(), 2)
    for _ in range(8):
        await asyncio.sleep(0)
    audit.drain(lambda _line: None)
    inbound.incoming.put_nowait(record(uuid4()))
    await asyncio.wait_for(task, 2)
    assert len(manager.opened) == 1
    assert b"gateway_audit_failure_total 1" in server.metrics.render(audit)


@pytest.mark.asyncio
async def test_wrong_upstream_identity_is_closed_without_sending():
    upstream = Session(PeerIdentity("unconfigured", PeerRole.SIMULATOR))
    inbound, _, _, _, _, task = setup(upstream=upstream)
    inbound.incoming.put_nowait(record(uuid4()))
    await asyncio.wait_for(task, 2)
    assert not upstream.sent and upstream.closed.is_set() and not inbound.sent


@pytest.mark.asyncio
async def test_concurrent_requests_have_distinct_sessions_and_response_ids():
    inbound, manager, _, _, _, task = setup()
    identifiers = {uuid4(), uuid4()}
    for identifier in identifiers:
        inbound.incoming.put_nowait(record(identifier))
    async with asyncio.timeout(2):
        while len(inbound.sent) < 2:
            await asyncio.sleep(0)
    await stop(inbound, task)
    assert len(manager.opened) == 2 and manager.opened[0] is not manager.opened[1]
    assert {sent[2] for sent in inbound.sent} == identifiers


@pytest.mark.asyncio
async def test_incomplete_request_deadline_expires_without_forwarding():
    inbound, manager, _, _, _, task = setup(limits=replace(Limits(), request_timeout_seconds=1))
    inbound.incoming.put_nowait(record(uuid4(), b"first", final=False))
    await asyncio.wait_for(task, 3)
    assert not manager.opened and inbound.closed.is_set()


@pytest.mark.asyncio
async def test_slow_inbound_close_keeps_capacity_and_second_cancel_cleans_writer():
    inbound, _, _, server, writer, task = setup(limits=replace(Limits(), max_active=1))
    inbound.close_gate = asyncio.Event()
    inbound.incoming.put_nowait(record(UUID(int=0), b"", kind=RecordType.CLOSE))
    await asyncio.wait_for(inbound.close_started.wait(), 2)
    assert server.metrics.active_sessions == 1
    rejected = Writer()
    await server.handle(asyncio.StreamReader(), rejected)
    assert rejected.closed
    await server.close()
    assert task.cancelled() and writer.closed and not server._connections
    assert server.metrics.active_sessions == 0


@pytest.mark.asyncio
async def test_slow_upstream_close_retains_capacity_until_cleanup():
    upstream = Session(UPSTREAM, echo=True)
    upstream.close_gate = asyncio.Event()
    inbound, manager, _, _, _, task = setup(
        limits=replace(Limits(), max_active=1), upstream=upstream
    )
    inbound.incoming.put_nowait(record(uuid4()))
    await asyncio.wait_for(upstream.close_started.wait(), 2)
    inbound.incoming.put_nowait(record(uuid4()))
    await asyncio.wait_for(task, 2)
    assert len(manager.opened) == 1


class ApplicationAdapter:
    def __init__(self, session, *, gate=None, bad_id=False, error=False):
        self.session = session
        self.gate = gate
        self.bad_id = bad_id
        self.error = error
        self.called = asyncio.Event()

    async def complete(self, request):
        assert self.session.state is SessionState.ACTIVE
        self.called.set()
        if self.gate is not None:
            await self.gate.wait()
        if self.error:
            raise RuntimeError("sensitive-prompt")
        return InferenceResponse(uuid4() if self.bad_id else request.request_id, "output")

    def stream(self, request):
        raise NotImplementedError


@pytest.mark.asyncio
async def test_dto_forwarding_implements_contract_and_preserves_id():
    upstream = Session(UPSTREAM)
    manager = Manager(None, upstream)
    audit = PrivacyAudit()
    service = ForwardingService(manager, GatewayConfig(UPSTREAM), audit, ApplicationAdapter)
    assert isinstance(service, InferenceService)
    request = InferenceRequest(uuid4(), "model", "synthetic-prompt")
    response = await service.complete(request)
    assert response == InferenceResponse(request.request_id, "output")
    assert upstream.closed.is_set() and len(manager.opened) == 1
    with pytest.raises(NotImplementedError):
        service.stream(request)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["bad_id", "error", "identity", "audit", "response_size"])
async def test_dto_forwarding_rejects_failures_and_closes(mode):
    upstream = Session(UPSTREAM)
    if mode == "identity":
        upstream.peer = PeerIdentity("wrong", PeerRole.SIMULATOR)
    manager = Manager(None, upstream)
    audit = PrivacyAudit(1)
    if mode == "audit":
        audit.publish(AuditEvent("request_started", "accepted"))
    limits = (
        replace(Limits(), max_record_plaintext_bytes=1, max_response_body_bytes=1)
        if mode == "response_size"
        else Limits()
    )
    service = ForwardingService(
        manager,
        GatewayConfig(UPSTREAM, limits=limits),
        audit,
        lambda session: ApplicationAdapter(session, bad_id=mode == "bad_id", error=mode == "error"),
    )
    with pytest.raises(GatewayError) as error:
        await service.complete(InferenceRequest(uuid4(), "model", "synthetic"))
    assert "sensitive-prompt" not in str(error.value)
    assert not manager.opened if mode == "audit" else upstream.closed.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [True, False])
async def test_dto_cancellation_and_timeout_release_session(cancel):
    upstream = Session(UPSTREAM)
    adapter = ApplicationAdapter(upstream, gate=asyncio.Event())
    service = ForwardingService(
        Manager(None, upstream),
        GatewayConfig(UPSTREAM, limits=replace(Limits(), request_timeout_seconds=1)),
        PrivacyAudit(),
        lambda _session: adapter,
    )
    task = asyncio.create_task(service.complete(InferenceRequest(uuid4(), "m", "synthetic")))
    await asyncio.wait_for(adapter.called.wait(), 2)
    if cancel:
        task.cancel()
    with pytest.raises(asyncio.CancelledError if cancel else TimeoutError):
        await asyncio.wait_for(task, 2)
    assert upstream.closed.is_set() and service._active == 0


@pytest.mark.asyncio
async def test_dto_capacity_includes_slow_close():
    upstream = Session(UPSTREAM)
    upstream.close_gate = asyncio.Event()
    service = ForwardingService(
        Manager(None, upstream),
        GatewayConfig(UPSTREAM, limits=replace(Limits(), max_active=1)),
        PrivacyAudit(),
        ApplicationAdapter,
    )
    request = InferenceRequest(uuid4(), "model", "synthetic")
    task = asyncio.create_task(service.complete(request))
    await asyncio.wait_for(upstream.close_started.wait(), 2)
    with pytest.raises(CapacityError):
        await service.complete(request)
    upstream.close_gate.set()
    await task
