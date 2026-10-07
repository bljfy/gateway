"""Real two-link topology: fixed synthetic business input, native cryptography."""

import asyncio
import hashlib
import json
import os
import socket
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio

import gateway.session.core as core
from gateway.client import InferenceClient
from gateway.contracts import (
    GatewayError,
    InferenceRequest,
    InferenceResponse,
    PeerRole,
    SessionState,
)
from gateway.crypto import GmSSLBackend
from gateway.runtime import Runtime, load_runtime, prepare_demo, run_server
from gateway.session import SecuritySession
from gateway.simulator import InferenceSimulator
from test.fixtures.simulator_output import response_body


def ports() -> tuple[int, int, int]:
    sockets = [socket.socket() for _ in range(3)]
    try:
        for sock in sockets:
            sock.bind(("127.0.0.1", 0))
        return tuple(sock.getsockname()[1] for sock in sockets)  # type: ignore[return-value]
    finally:
        for sock in sockets:
            sock.close()


@dataclass
class Topology:
    client: Runtime
    gateway: Runtime
    simulator: Runtime
    directory: Path
    outbound: list[SecuritySession]
    key_fingerprints: list[tuple[bytes, str]]

    async def connect(self) -> SecuritySession:
        assert self.client.target is not None
        return await self.client.manager.open(self.client.target)


@pytest_asyncio.fixture
async def topology(
    tmp_path: Path,
    backend: GmSSLBackend,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> AsyncIterator[Topology]:
    manifest = tmp_path / "native.json"
    manifest.write_text(
        json.dumps({"library": os.environ["GMSSL_LIBRARY"], "sha256": os.environ["GMSSL_SHA256"]}),
        encoding="utf-8",
    )
    directory = tmp_path / "demo"
    gp, sp, mp = ports()
    prepare_demo(directory, manifest, gateway_port=gp, simulator_port=sp, metrics_port=mp)
    if getattr(request, "param", None) == "small_records":
        with (directory / "gateway.toml").open("a", encoding="utf-8") as handle:
            handle.write("max_record_plaintext_bytes=8\n")
    client = load_runtime(directory / "client.json", PeerRole.CLIENT)
    gateway = load_runtime(directory / "gateway.json", PeerRole.GATEWAY)
    simulator = load_runtime(directory / "simulator.json", PeerRole.SIMULATOR)
    outbound: list[SecuritySession] = []
    fingerprints: list[tuple[bytes, str]] = []
    original_open = gateway.manager.open

    async def opened(peer: core.PeerIdentity) -> SecuritySession:
        session = await original_open(peer)
        outbound.append(session)
        fingerprints.append((session.session_id, hashlib.sha256(session._keys).hexdigest()))
        return session

    monkeypatch.setattr(gateway.manager, "open", opened)
    stops, ready = [asyncio.Event(), asyncio.Event()], [asyncio.Event(), asyncio.Event()]
    servers = [
        asyncio.create_task(run_server(runtime, stop=stop, ready=signal.set))
        for runtime, stop, signal in zip((simulator, gateway), stops, ready, strict=True)
    ]
    try:
        await asyncio.wait_for(asyncio.gather(*(signal.wait() for signal in ready)), 3)
        yield Topology(client, gateway, simulator, directory, outbound, fingerprints)
    finally:
        await client.manager.close()
        for stop in stops:
            stop.set()
        await asyncio.wait_for(asyncio.gather(*servers), 5)
        assert (
            not client.manager._sessions
            and not gateway.manager._sessions
            and not simulator.manager._sessions
        )
        assert not gateway.manager._native_tasks and not simulator.manager._native_tasks


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [1024, 16384, 70000])
@pytest.mark.parametrize("stream", [False, True])
async def test_real_roundtrip_and_privacy(
    topology: Topology, size: int, stream: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    packets: list[bytes] = []
    original = core.write_frame

    async def capture(writer: asyncio.StreamWriter, data: bytes) -> None:
        packets.append(data)
        await original(writer, data)

    monkeypatch.setattr(core, "write_frame", capture)
    session = await topology.connect()
    client = InferenceClient(session)
    marker = "synthetic-private-marker"
    request = InferenceRequest(
        uuid4(),
        "model",
        marker + "x" * (size - len(marker)),
        ("synthetic-retrieval-marker",),
        max_output_tokens=size + 128,
    )
    expected = (await InferenceSimulator().complete(request)).output
    if stream:
        chunks = [chunk async for chunk in client.stream(request)]
        assert chunks[-1].is_final and sum(item.is_final for item in chunks) == 1
        assert response_body("".join(item.output for item in chunks), request.request_id) == (
            response_body(expected, request.request_id)
        )
    else:
        assert response_body((await client.complete(request)).output, request.request_id) == (
            response_body(expected, request.request_id)
        )
    assert topology.key_fingerprints[0][0] != session.session_id
    assert topology.key_fingerprints[0][1] != hashlib.sha256(session._keys).hexdigest()
    assert all(marker.encode() not in packet for packet in packets)
    assert all(b"synthetic-retrieval-marker" not in packet for packet in packets)
    await session.close()
    await asyncio.sleep(0.15)
    audit = (topology.directory / "audit.jsonl").read_text(encoding="utf-8")
    assert marker not in audit and request.prompt not in audit and expected not in audit
    assert topology.gateway.manager.local.signing_private_key.hex() not in audit
    assert any(json.loads(line)["result"] == "ok" for line in audit.splitlines())


@pytest.mark.asyncio
@pytest.mark.parametrize("hop", ["client", "upstream"])
async def test_ciphertext_tampering_never_calls_inference(
    topology: Topology, hop: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = await topology.connect()
    calls = 0
    original_output = InferenceSimulator._output

    def counted(request: InferenceRequest) -> str:
        nonlocal calls
        calls += 1
        return original_output(request)

    monkeypatch.setattr(InferenceSimulator, "_output", staticmethod(counted))
    original = core.write_frame
    mutated = False

    async def corrupt(writer: asyncio.StreamWriter, data: bytes) -> None:
        nonlocal mutated
        selected = (
            writer is session.writer
            if hop == "client"
            else any(writer is item.writer for item in topology.outbound)
        )
        if not mutated and selected and data[:2] == b"\x01\x01":
            data = data[:-1] + bytes([data[-1] ^ 1])
            mutated = True
        await original(writer, data)

    monkeypatch.setattr(core, "write_frame", corrupt)
    with pytest.raises((GatewayError, OSError)):
        await asyncio.wait_for(
            InferenceClient(session).complete(InferenceRequest(uuid4(), "model", "synthetic")), 3
        )
    assert mutated and calls == 0 and session.state is SessionState.CLOSED


@pytest.mark.asyncio
async def test_replay_never_executes_twice(
    topology: Topology, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = await topology.connect()
    calls = 0
    original_output = InferenceSimulator._output

    def counted(request: InferenceRequest) -> str:
        nonlocal calls
        calls += 1
        return original_output(request)

    monkeypatch.setattr(InferenceSimulator, "_output", staticmethod(counted))
    original = core.write_frame
    repeated = False

    async def replay(writer: asyncio.StreamWriter, data: bytes) -> None:
        nonlocal repeated
        await original(writer, data)
        if not repeated and writer is session.writer and data[:2] == b"\x01\x01":
            repeated = True
            await original(writer, data)

    monkeypatch.setattr(core, "write_frame", replay)
    try:
        await asyncio.wait_for(
            InferenceClient(session).complete(InferenceRequest(uuid4(), "m", "synthetic")), 3
        )
    except (GatewayError, OSError):
        pass
    await asyncio.sleep(0.1)
    assert repeated and calls <= 1
    with pytest.raises((GatewayError, OSError)):
        await InferenceClient(session).complete(InferenceRequest(uuid4(), "m", "next"))


@pytest.mark.asyncio
@pytest.mark.parametrize("hop", ["client", "upstream"])
@pytest.mark.parametrize("failure", ["wrong_key", "expired"])
async def test_trust_failure_blocks_business(topology: Topology, hop: str, failure: str) -> None:
    manager = topology.client.manager if hop == "client" else topology.gateway.manager
    target = "gateway" if hop == "client" else "simulator"
    trusted = manager.trust[target]
    if failure == "wrong_key":
        _, wrong = manager.backend.generate_keypair()
        manager.trust[target] = replace(trusted, signing_public_key=wrong)
    else:
        manager.trust[target] = replace(trusted, expires_at=1)
    with pytest.raises((GatewayError, OSError)):
        session = await topology.connect()
        await InferenceClient(session).complete(InferenceRequest(uuid4(), "m", "synthetic"))
    assert not topology.outbound


@pytest.mark.asyncio
async def test_expired_client_session_never_forwards(topology: Topology) -> None:
    session = await topology.connect()
    topology.client.manager.clock = lambda: session._started + 1801
    with pytest.raises(GatewayError):
        await InferenceClient(session).complete(InferenceRequest(uuid4(), "m", "synthetic"))
    assert session.state is SessionState.CLOSED and not topology.outbound and not session._keys


@pytest.mark.asyncio
async def test_rotation_then_business_uses_new_session(topology: Topology) -> None:
    old = await topology.connect()
    fresh = await topology.client.manager.rotate(old)
    assert fresh.session_id != old.session_id and fresh._keys != old._keys
    assert (
        await InferenceClient(fresh).complete(InferenceRequest(uuid4(), "m", "synthetic"))
    ).output
    await asyncio.gather(old.close(), fresh.close())
    assert not old._keys and not fresh._keys


@pytest.mark.asyncio
async def test_early_stream_close_cleans_links_and_reconnects(topology: Topology) -> None:
    session = await topology.connect()
    iterator = InferenceClient(session).stream(
        InferenceRequest(uuid4(), "m", "x" * 70000, max_output_tokens=70064)
    )
    assert not (await anext(iterator)).is_final
    await iterator.aclose()  # type: ignore[attr-defined]
    assert session.state is SessionState.CLOSED
    fresh = await topology.connect()
    assert (await InferenceClient(fresh).complete(InferenceRequest(uuid4(), "m", "next"))).output
    await fresh.close()


@pytest.mark.asyncio
async def test_concurrent_client_calls_use_one_reader(topology: Topology) -> None:
    session = await topology.connect()
    client = InferenceClient(session)
    requests = [InferenceRequest(uuid4(), "m", str(index)) for index in range(4)]
    responses = await asyncio.gather(*(client.complete(request) for request in requests))
    assert [item.request_id for item in responses] == [item.request_id for item in requests]
    await session.close()


@pytest.mark.asyncio
async def test_request_timeout_releases_simulator_work(
    topology: Topology, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Runtime gateway config remains immutable; client request deadline covers the full link.
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def slow(self: InferenceSimulator, request: InferenceRequest) -> InferenceResponse:
        entered.set()
        try:
            await asyncio.sleep(5)
            return InferenceResponse(request.request_id, "late")
        except asyncio.CancelledError:
            cancelled.set()
            raise

    monkeypatch.setattr(InferenceSimulator, "complete", slow)
    session = await topology.connect()
    client = InferenceClient(
        session, replace(topology.gateway.gateway.limits, request_timeout_seconds=1)
    )
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(client.complete(InferenceRequest(uuid4(), "m", "x")), 3)
    assert entered.is_set()
    await asyncio.wait_for(cancelled.wait(), 2)
    assert session.state is SessionState.CLOSED


@pytest.mark.asyncio
@pytest.mark.parametrize("topology", ["small_records"], indirect=True)
@pytest.mark.parametrize("stream", [False, True])
async def test_configured_small_records_preserve_unicode(topology: Topology, stream: bool) -> None:
    session = await topology.connect()
    client = InferenceClient(session, topology.gateway.gateway.limits)
    request = InferenceRequest(uuid4(), "m", "中文🙂" * 20, max_output_tokens=200)
    expected = (await InferenceSimulator().complete(request)).output
    if stream:
        assert response_body(
            "".join([chunk.output async for chunk in client.stream(request)]), request.request_id
        ) == response_body(expected, request.request_id)
    else:
        assert response_body((await client.complete(request)).output, request.request_id) == (
            response_body(expected, request.request_id)
        )
    await session.close()
