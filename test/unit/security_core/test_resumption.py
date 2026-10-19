"""Real backend recovery, persistence failures and transcript fault injection."""

import asyncio
import hmac
import os
import sqlite3
import time
from dataclasses import replace
from uuid import uuid4

import pytest

import gateway.session.handshakes as handshake
from gateway.config import GatewayConfig
from gateway.contracts import GatewayError, PeerIdentity, PeerRole, RecordType
from gateway.crypto.kdf import hkdf
from gateway.metrics import Metrics
from gateway.protocol import write_frame
from gateway.server.pool import UpstreamPool
from gateway.session.protection import dpapi, restricted
from gateway.session.store import Credential, ResumptionStore, StoreError
from test.unit.security_core.test_session import Link


@pytest.mark.parametrize(
    "fault", ["wrong_master", "corrupt_master", "write_failure", "clock_rollback"]
)
def test_store_faults_fail_closed(tmp_path, backend, monkeypatch, fault):
    directory, master = provision(tmp_path, backend, "fault")
    secret = backend.random_bytes(32)
    entry = Credential(bytes(32), secret, bytes(32), "gateway", int(time.time()) + 1800)
    with ResumptionStore(directory, master, backend) as store:
        store.put(entry)
        if fault == "write_failure":
            blocker = sqlite3.connect(directory / "credentials.sqlite3")
            try:
                blocker.execute("BEGIN EXCLUSIVE")
                with pytest.raises(StoreError):
                    store.put(entry)
                assert not store.healthy
            finally:
                blocker.close()
        elif fault == "clock_rollback":
            earlier = time.time() - 20
            monkeypatch.setattr(time, "time", lambda: earlier)
            assert store.get(entry.binding) is None
            assert store.healthy
    if fault in {"wrong_master", "corrupt_master"}:
        key = backend.random_bytes(32)
        master.write_bytes(
            dpapi(key, protect=True) if os.name == "nt" and fault == "wrong_master" else key
        )
        with pytest.raises(StoreError):
            ResumptionStore(directory, master, backend)


@pytest.mark.asyncio
async def test_revoke_during_handshake_cannot_reinsert_credential(tmp_path, backend, monkeypatch):
    pair = Link(backend)
    paths = [provision(tmp_path, backend, name) for name in ("client", "server")]
    pair.client.store = ResumptionStore(*paths[0], backend)
    pair.server.store = ResumptionStore(*paths[1], backend)
    listener = await asyncio.start_server(pair.accept, "127.0.0.1", 0)
    pair.client.trust["gateway"] = replace(
        pair.client.trust["gateway"], address=("127.0.0.1", listener.sockets[0].getsockname()[1])
    )
    paused, release = asyncio.Event(), asyncio.Event()
    original = handshake._finish

    async def pause(session, material, transcript):
        await original(session, material, transcript)
        if session.initiator:
            paused.set()
            await release.wait()

    monkeypatch.setattr(handshake, "_finish", pause)
    opening = asyncio.create_task(pair.client.open(pair.server.local.peer))
    try:
        await asyncio.wait_for(paused.wait(), 3)
        await pair.client.revoke("gateway")
        release.set()
        with pytest.raises(GatewayError):
            await opening
        assert pair.client.store.status() == []
        assert not pair.client._sessions
    finally:
        release.set()
        listener.close()
        await asyncio.gather(pair.client.close(), pair.server.close())
        await listener.wait_closed()


def provision(tmp_path, backend, name):
    key_dir = tmp_path / (name + "-keys")
    restricted(key_dir, create=True, directory=True)
    directory, master = tmp_path / name, key_dir / "master.key"
    ResumptionStore.initialize(directory, master, backend)
    return directory, master


@pytest.mark.parametrize("size", [1, 32, 72, 104])
def test_hkdf_matches_independent_hmac(backend, size):
    secret, salt, info = b"synthetic-secret", b"salt", b"gateway-v2-test"
    prk = hmac.digest(salt, secret, "sm3")
    previous, expected = b"", b""
    for index in range(1, (size + 31) // 32 + 1):
        previous = hmac.digest(prk, previous + info + bytes([index]), "sm3")
        expected += previous
    assert hkdf(backend, secret, salt, info, size) == expected[:size]
    assert hkdf(backend, secret, salt, b"other-domain", size) != expected[:size]


def test_encrypted_restart_lock_tamper_and_revocation(tmp_path, backend):
    directory, master = provision(tmp_path, backend, "client")
    entry = Credential(
        backend.random_bytes(32),
        backend.random_bytes(32),
        backend.random_bytes(32),
        "gateway",
        int(time.time()) + 1800,
    )
    with ResumptionStore(directory, master, backend) as store:
        store.put(entry)
        assert store.get(entry.binding) == entry
        with pytest.raises(StoreError):
            ResumptionStore(directory, master, backend)
        assert entry.secret not in (directory / "credentials.sqlite3").read_bytes()
        assert entry.secret.hex().encode() not in (directory / "credentials.sqlite3").read_bytes()
    with ResumptionStore(directory, master, backend) as store:
        assert store.get(entry.binding) == entry
        assert store.status() == [dict(peer_id="gateway", expires_at=entry.expires_at)]
        store.revoke("gateway")
        assert store.get(entry.binding) is None
        store.put(entry)
    with sqlite3.connect(directory / "credentials.sqlite3") as db:
        db.execute("UPDATE credentials SET envelope=?", (bytes(100),))
    with pytest.raises(StoreError):
        ResumptionStore(directory, master, backend)
    with pytest.raises(StoreError):
        ResumptionStore.initialize(directory, master, backend)


def test_repeated_lookups_batch_metadata_without_losing_secret(tmp_path, backend, monkeypatch):
    directory, master = provision(tmp_path, backend, "batch")
    now = time.time()
    monkeypatch.setattr(time, "time", lambda: now)
    entry = Credential(
        backend.random_bytes(32),
        backend.random_bytes(32),
        backend.random_bytes(32),
        "gateway",
        int(now) + 1800,
    )
    with ResumptionStore(directory, master, backend) as store:
        store.put(entry)
        statements = []
        store._db.set_trace_callback(statements.append)
        for _ in range(20):
            assert store.get(entry.binding) == entry
        assert not any(s.startswith(("INSERT", "UPDATE", "DELETE")) for s in statements)
    with ResumptionStore(directory, master, backend) as store:
        assert store.get(entry.binding) == entry


@pytest.mark.asyncio
async def test_restart_resumes_with_independent_keys(tmp_path, backend):
    pair = Link(backend)
    paths = [provision(tmp_path, backend, name) for name in ("client", "server")]
    pair.client.store = ResumptionStore(*paths[0], backend)
    pair.server.store = ResumptionStore(*paths[1], backend)
    listener = await asyncio.start_server(pair.accept, "127.0.0.1", 0)
    pair.client.trust["gateway"] = replace(
        pair.client.trust["gateway"], address=("127.0.0.1", listener.sockets[0].getsockname()[1])
    )
    try:
        first, inbound = await pair.connect()
        assert not first.resumed
        original = bytes(first._keys)
        pool = UpstreamPool(
            pair.client, GatewayConfig(PeerIdentity("adapter", PeerRole.ADAPTER)), Metrics()
        )
        assert pool._usable(first)
        first._send_sequence = pair.client.policy.max_records_per_direction - 1
        assert not pool._usable(first, request_records=2)
        first._send_sequence = 0
        first._receive_sequence = pair.client.policy.max_records_per_direction - 1
        assert not pool._usable(first)
        first._receive_sequence = 1
        await asyncio.gather(first.close(), inbound.close())
        # Recreate both storage objects to force loading encrypted credentials from disk.
        for manager, path in zip((pair.client, pair.server), paths, strict=True):
            manager.store.close()
            manager.store = ResumptionStore(*path, backend)
        results = []
        for _ in range(3):
            client, server = await pair.connect()
            assert client.resumed and server.resumed
            assert bytes(client._keys) == bytes(server._keys) != original
            results.append((client.session_id, bytes(client._keys)))
            rid = uuid4()
            await client.send(RecordType.REQUEST, b"synthetic", request_id=rid)
            assert (await server.recv()).plaintext == b"synthetic"
            await asyncio.gather(client.close(), server.close())
        assert len(set(results)) == 3
        assert pair.client.handshakes == {"full": 1, "resumed": 3, "resume_rejected": 0}
        assert (
            pair.client.store.status()[0]["expires_at"]
            == pair.server.store.status()[0]["expires_at"]
        )
    finally:
        listener.close()
        await asyncio.gather(pair.client.close(), pair.server.close())
        await listener.wait_closed()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault", ["miss", "binder", "server", "client", "trust", "policy", "replay", "expired"]
)
async def test_recovery_failures_never_deliver_business(tmp_path, backend, monkeypatch, fault):
    pair = Link(backend)
    paths = [provision(tmp_path, backend, name) for name in ("client", "server")]
    pair.client.store = ResumptionStore(*paths[0], backend)
    pair.server.store = ResumptionStore(*paths[1], backend)
    listener = await asyncio.start_server(pair.accept, "127.0.0.1", 0)
    pair.client.trust["gateway"] = replace(
        pair.client.trust["gateway"], address=("127.0.0.1", listener.sockets[0].getsockname()[1])
    )
    try:
        client, server = await pair.connect()
        await asyncio.gather(client.close(), server.close())
        if fault == "miss":
            pair.server.store.revoke()
        elif fault == "expired":
            entry = pair.client.store.get(
                pair.client.binding(pair.server.local.peer, initiator=True)
            )
            pair.client.store.put(replace(entry, expires_at=int(time.time()) - 1))
        elif fault == "trust":
            pair.client.trust["gateway"] = replace(pair.client.trust["gateway"], enabled=False)
        elif fault == "policy":
            pair.server.max_bytes -= 1
        elif fault == "replay":
            captured = []

            async def capture(writer, data):
                if data[0] == 6:
                    if captured:
                        data = captured[0]
                    else:
                        captured.append(data)
                await write_frame(writer, data)

            monkeypatch.setattr(handshake, "write_frame", capture)
            client, server = await pair.connect()
            await asyncio.gather(client.close(), server.close())
        else:
            kind = {"binder": 6, "server": 7, "client": 8}[fault]

            async def mutate(writer, data):
                if data[0] == kind:
                    data = data[:-1] + bytes([data[-1] ^ 1])
                await write_frame(writer, data)

            monkeypatch.setattr(handshake, "write_frame", mutate)
        if fault in {"miss", "expired"}:
            client = await pair.client.open(pair.server.local.peer)
            assert not client.resumed
            assert pair.client.handshakes["resume_rejected"] == int(fault == "miss")
        else:
            with pytest.raises(GatewayError):
                await pair.client.open(pair.server.local.peer)
            assert pair.client.handshakes["full"] == 1
    finally:
        listener.close()
        await asyncio.gather(pair.client.close(), pair.server.close())
        await listener.wait_closed()


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission boundary")
def test_linux_permissions_and_wrong_master(tmp_path, backend):
    directory, master = provision(tmp_path, backend, "linux")
    master.chmod(0o644)
    with pytest.raises(StoreError):
        ResumptionStore(directory, master, backend)
    master.chmod(0o600)
    with ResumptionStore(directory, master, backend) as store:
        store.put(
            Credential(
                bytes(32), backend.random_bytes(32), bytes(32), "gateway", int(time.time()) + 100
            )
        )
    master.write_bytes(backend.random_bytes(32))
    with pytest.raises(StoreError):
        ResumptionStore(directory, master, backend)
