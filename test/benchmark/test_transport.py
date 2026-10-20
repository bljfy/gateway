"""Real TLS authentication and business framing regressions for the baseline."""

import asyncio
import ssl

import pytest

from gateway.config import GatewayConfig
from gateway.contracts import AuthenticationError, PeerIdentity, PeerRole, ProtocolError
from test.benchmark.transport import TransportManager, certificates


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["tls", "tls-resume", "tls-pool", "tls-combined"])
@pytest.mark.parametrize(
    "fault", ["none", "missing_certificate", "wrong_certificate", "wrong_name"]
)
async def test_tls_mutual_identity_and_ready(tmp_path, fault, monkeypatch, mode):
    key_log = tmp_path / "tls-secrets.log"
    monkeypatch.setenv("SSLKEYLOGFILE", str(key_log))
    certificates(tmp_path)
    client = PeerIdentity("client", PeerRole.CLIENT)
    gateway = PeerIdentity("gateway", PeerRole.GATEWAY)
    acceptor = TransportManager(gateway, client, None, 0, tmp_path, mode)
    accepted = asyncio.Event()
    owned = set()

    async def handle(reader, writer):
        task = asyncio.current_task()
        owned.add(task)
        try:
            session = await acceptor.accept(reader, writer)
            accepted.set()
            await reader.read()
            await session.close()
        finally:
            writer.close()
            owned.discard(task)

    listener = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=acceptor.server_context)
    initiator = TransportManager(
        client, None, gateway, listener.sockets[0].getsockname()[1], tmp_path, mode
    )
    if fault == "missing_certificate":
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.minimum_version = ctx.maximum_version = ssl.TLSVersion.TLSv1_3
        ctx.load_verify_locations(tmp_path / "gateway.pem")
        initiator.client_context = ctx
    elif fault == "wrong_certificate":
        initiator.client_context.load_cert_chain(tmp_path / "adapter.pem", tmp_path / "adapter.key")
    try:
        if fault == "none":
            session = await initiator.open(gateway)
            assert accepted.is_set()
            assert initiator.negotiated == {"TLS_AES_256_GCM_SHA384"}
            await session.close()
        else:
            with pytest.raises(
                (ssl.SSLError, AuthenticationError, OSError, ProtocolError)
            ) as error:
                # The mismatched name is rejected before any business is admitted.
                await initiator.open(
                    PeerIdentity("adapter", PeerRole.ADAPTER) if fault == "wrong_name" else gateway
                )
            assert type(error.value).__name__ in {
                "SSLCertVerificationError",
                "SSLError",
                "ConnectionResetError",
                "ProtocolError",
            }
            assert not accepted.is_set()
    finally:
        listener.close()
        await listener.wait_closed()
        await initiator.close()
        await acceptor.close()
        await asyncio.gather(*tuple(owned), return_exceptions=True)
    assert not key_log.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["tls", "tls-resume", "tls-pool", "tls-combined"])
async def test_actual_resumption_expiry_and_identity(tmp_path, mode, monkeypatch):
    key_log = tmp_path / "tls-secrets.log"
    monkeypatch.setenv("SSLKEYLOGFILE", str(key_log))
    certificates(tmp_path)
    client = PeerIdentity("client", PeerRole.CLIENT)
    gateway = PeerIdentity("gateway", PeerRole.GATEWAY)
    acceptor = TransportManager(gateway, client, None, 0, tmp_path, mode)
    owned = set()
    server_resumed = []

    async def handle(reader, writer):
        task = asyncio.current_task()
        owned.add(task)
        try:
            session = await acceptor.accept(reader, writer)
            server_resumed.append(session.resumed)
            await reader.read()
            await session.close()
        finally:
            writer.close()
            owned.discard(task)

    listener = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=acceptor.server_context)
    initiator = TransportManager(
        client, None, gateway, listener.sockets[0].getsockname()[1], tmp_path, mode
    )
    try:
        sessions = []
        for _ in range(3):
            session = await initiator.open(gateway)
            sessions.append(session)
            config = GatewayConfig(PeerIdentity("adapter", PeerRole.ADAPTER))
            assert session.usable_for_pool(config, 1)
            created = session.created
            session.created -= config.session.absolute_lifetime_seconds
            assert not session.usable_for_pool(config, 1)
            session.created = created
            session.sent = config.session.max_records_per_direction
            assert not session.usable_for_pool(config, 1)
            with pytest.raises(ProtocolError, match="budget"):
                session.check_budget(1, sending=True)
            session.sent = 0
            session.received_bytes = config.limits.max_plaintext_bytes_per_direction
            assert not session.usable_for_pool(config, 1)
            session.received_bytes = 0
            await session.close()
        expected = [False, initiator.resumption_enabled, initiator.resumption_enabled]
        assert [s.resumed for s in sessions] == server_resumed == expected
        assert [row["resumed"] for row in initiator.timings] == expected
        if initiator.resumption_enabled:
            deadline = initiator.client_context.cache_deadline
            assert deadline > 0
            # A cached session must not bypass hostname validation.
            with pytest.raises((ssl.SSLError, OSError, ProtocolError)):
                await initiator.open(PeerIdentity("adapter", PeerRole.ADAPTER))
            assert initiator.client_context.cache_deadline == deadline
            initiator.client_context.cache_deadline = 0
            session = await initiator.open(gateway)
            assert not session.resumed
            await session.close()
    finally:
        listener.close()
        await listener.wait_closed()
        await initiator.close()
        await acceptor.close()
        await asyncio.gather(*tuple(owned), return_exceptions=True)
    assert not key_log.exists()
