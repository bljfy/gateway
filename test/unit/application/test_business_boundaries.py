"""Regressions for PR #2: bounded assembly, identity, control and stream cleanup."""

import asyncio
from dataclasses import replace
from uuid import UUID, uuid4

import pytest

from gateway.client import InferenceClient
from gateway.codec import MAX_REQUEST_BODY_BYTES, encode_request, encode_response
from gateway.config import Limits
from gateway.contracts import (
    MAX_RECORD_PLAINTEXT,
    Direction,
    InferenceRequest,
    InferenceResponse,
    PeerIdentity,
    PeerRole,
    ProtocolError,
    RecordHeader,
    RecordType,
    SessionState,
    VerifiedRecord,
)
from gateway.framing import recv_message
from gateway.simulator import InferenceSimulator
from test.fixtures.in_memory import SESSION_ID, InMemorySession, connect_pair


def record(
    rid: UUID,
    data: bytes,
    index: int = 0,
    final: bool = True,
    kind: RecordType = RecordType.RESPONSE,
) -> VerifiedRecord:
    return VerifiedRecord(
        RecordHeader(kind, SESSION_ID, Direction.ACCEPTOR_TO_INITIATOR, index, rid, index, final),
        data,
    )


@pytest.mark.asyncio
async def test_assembly_rejects_over_budget_without_waiting_for_final() -> None:
    session = InMemorySession(PeerIdentity("gateway", PeerRole.GATEWAY))
    await session.handshake()
    rid = uuid4()
    for index in range(MAX_REQUEST_BODY_BYTES // MAX_RECORD_PLAINTEXT + 1):
        session.enqueue(record(rid, b"x" * MAX_RECORD_PLAINTEXT, index, False, RecordType.REQUEST))
    with pytest.raises(ProtocolError, match="budget"):
        await asyncio.wait_for(recv_message(session, rid, record_type=RecordType.REQUEST), 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_client_consumes_heartbeat(stream: bool) -> None:
    session = InMemorySession(PeerIdentity("gateway", PeerRole.GATEWAY))
    await session.handshake()
    rid = uuid4()
    session.enqueue(record(UUID(int=0), b"", kind=RecordType.HEARTBEAT))
    session.enqueue(record(rid, b"ok" if stream else encode_response(InferenceResponse(rid, "ok"))))
    client = InferenceClient(session)
    request = InferenceRequest(rid, "model", "synthetic")
    if stream:
        assert [chunk.output async for chunk in client.stream(request)] == ["ok"]
    else:
        assert (await client.complete(request)).output == "ok"


@pytest.mark.asyncio
async def test_simulator_consumes_interleaved_heartbeat() -> None:
    session = InMemorySession(PeerIdentity("gateway", PeerRole.GATEWAY))
    await session.handshake()
    rid = uuid4()
    data = encode_request(InferenceRequest(rid, "model", "synthetic"))
    session.enqueue(record(rid, data[:20], 0, False, RecordType.REQUEST))
    session.enqueue(record(UUID(int=0), b"", kind=RecordType.HEARTBEAT))
    session.enqueue(record(rid, data[20:], 1, True, RecordType.REQUEST))
    session.enqueue(record(UUID(int=0), b"", kind=RecordType.CLOSE))
    await InferenceSimulator().serve(session)
    assert session.sent


@pytest.mark.asyncio
async def test_simulator_rejects_payload_identity_before_inference() -> None:
    session = InMemorySession(PeerIdentity("gateway", PeerRole.GATEWAY))
    await session.handshake()
    session.enqueue(
        record(
            uuid4(),
            encode_request(InferenceRequest(uuid4(), "model", "synthetic")),
            kind=RecordType.REQUEST,
        )
    )
    with pytest.raises(ProtocolError, match="identifiers"):
        await InferenceSimulator().serve(session)
    assert not session.sent


@pytest.mark.asyncio
async def test_stream_aclose_releases_session() -> None:
    client_session, server = connect_pair(
        PeerIdentity("gateway", PeerRole.GATEWAY), PeerIdentity("client", PeerRole.CLIENT)
    )
    await client_session.handshake()
    await server.handshake()
    serving = asyncio.create_task(InferenceSimulator(stream_chunk_chars=8).serve(server))
    iterator = InferenceClient(client_session).stream(
        InferenceRequest(uuid4(), "model", "long output")
    )
    try:
        assert not (await anext(iterator)).is_final
        await iterator.aclose()  # type: ignore[attr-defined]
        assert client_session.state is SessionState.CLOSED
    finally:
        serving.cancel()
        await asyncio.gather(serving, return_exceptions=True)


@pytest.mark.asyncio
async def test_stream_decodes_utf8_across_record_boundaries() -> None:
    session = InMemorySession(PeerIdentity("gateway", PeerRole.GATEWAY))
    await session.handshake()
    rid = uuid4()
    data = "你好".encode()
    session.enqueue(record(rid, data[:2], 0, False))
    session.enqueue(record(rid, data[2:], 1))
    chunks = [
        chunk async for chunk in InferenceClient(session).stream(InferenceRequest(rid, "m", "x"))
    ]
    assert "".join(chunk.output for chunk in chunks) == "你好"


@pytest.mark.asyncio
async def test_client_serializes_concurrent_calls() -> None:
    client_session, server = connect_pair(
        PeerIdentity("gateway", PeerRole.GATEWAY), PeerIdentity("client", PeerRole.CLIENT)
    )
    await client_session.handshake()
    await server.handshake()
    serving = asyncio.create_task(InferenceSimulator().serve(server))
    client = InferenceClient(client_session)
    try:
        requests = [InferenceRequest(uuid4(), "model", str(index)) for index in range(4)]
        responses = await asyncio.gather(*(client.complete(request) for request in requests))
        assert [item.request_id for item in responses] == [item.request_id for item in requests]
    finally:
        serving.cancel()
        await asyncio.gather(serving, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("consumer", ["complete", "stream", "simulator"])
async def test_receivers_reject_records_above_configured_limit(consumer: str) -> None:
    session = InMemorySession(PeerIdentity("gateway", PeerRole.GATEWAY))
    await session.handshake()
    rid = uuid4()
    limits = replace(Limits(), max_record_plaintext_bytes=8)
    kind = RecordType.REQUEST if consumer == "simulator" else RecordType.RESPONSE
    session.enqueue(record(rid, b"x" * 9, kind=kind))
    client = InferenceClient(session, limits)
    request = InferenceRequest(rid, "m", "x")
    with pytest.raises(ProtocolError, match="plaintext limit"):
        if consumer == "simulator":
            await InferenceSimulator(limits=limits).serve(session)
        elif consumer == "complete":
            await client.complete(request)
        else:
            await anext(client.stream(request))
    if consumer == "simulator":
        assert not session.sent
    else:
        assert session.state is SessionState.CLOSED
