"""End-to-end business path over loopback sessions (no cryptography).

These tests exercise the client, framing, codec and simulator together to show
the ordinary request/response and streaming loops are correct end to end. They
use in-memory session doubles and do not constitute security verification.
"""

import asyncio
from uuid import uuid4

import pytest

from gateway.client import InferenceClient
from gateway.contracts import (
    MAX_RECORD_PLAINTEXT,
    InferenceRequest,
    PeerIdentity,
    PeerRole,
    RecordType,
)
from gateway.simulator import InferenceSimulator
from test.fixtures.in_memory import InMemorySession, connect_pair

CLIENT_PEER = PeerIdentity("client", PeerRole.CLIENT)
SIMULATOR_PEER = PeerIdentity("simulator", PeerRole.SIMULATOR)


async def _finish(client_session: InMemorySession, serve_task: asyncio.Task[None]) -> None:
    await client_session.send(RecordType.CLOSE, b"", request_id=uuid4())
    await serve_task


@pytest.mark.asyncio
async def test_complete_roundtrip_through_sessions() -> None:
    client_session, simulator_session = connect_pair(CLIENT_PEER, SIMULATOR_PEER)
    await client_session.handshake()
    await simulator_session.handshake()
    serve_task = asyncio.create_task(InferenceSimulator().serve(simulator_session))

    request = InferenceRequest(uuid4(), "mock-model", "hello")
    response = await InferenceClient(client_session).complete(request)

    assert response.request_id == request.request_id
    assert response.output == "[mock:mock-model] hello"
    await _finish(client_session, serve_task)


@pytest.mark.asyncio
async def test_large_request_roundtrip_through_sessions() -> None:
    client_session, simulator_session = connect_pair(CLIENT_PEER, SIMULATOR_PEER)
    await client_session.handshake()
    await simulator_session.handshake()
    serve_task = asyncio.create_task(InferenceSimulator().serve(simulator_session))

    request = InferenceRequest(uuid4(), "mock-model", "x" * (MAX_RECORD_PLAINTEXT + 100))
    response = await InferenceClient(client_session).complete(request)

    assert response.request_id == request.request_id
    assert response.output.startswith("[mock:mock-model] ")
    await _finish(client_session, serve_task)


@pytest.mark.asyncio
async def test_stream_roundtrip_through_sessions() -> None:
    client_session, simulator_session = connect_pair(CLIENT_PEER, SIMULATOR_PEER)
    await client_session.handshake()
    await simulator_session.handshake()
    serve_task = asyncio.create_task(InferenceSimulator().serve(simulator_session))

    request = InferenceRequest(uuid4(), "mock-model", "stream me")
    chunks = [chunk async for chunk in InferenceClient(client_session).stream(request)]

    assert chunks[-1].is_final is True
    assert all(chunk.request_id == request.request_id for chunk in chunks)
    assert "".join(chunk.output for chunk in chunks).startswith("[mock:mock-model] ")
    await _finish(client_session, serve_task)


@pytest.mark.asyncio
async def test_serve_returns_gracefully_on_close() -> None:
    client_session, simulator_session = connect_pair(CLIENT_PEER, SIMULATOR_PEER)
    await client_session.handshake()
    await simulator_session.handshake()
    serve_task = asyncio.create_task(InferenceSimulator().serve(simulator_session))

    await client_session.send(RecordType.CLOSE, b"", request_id=uuid4())
    await serve_task
