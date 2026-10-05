"""TLS 1.3 and plaintext measurement adapters for the existing business stack."""

import asyncio
import os
import ssl
import time
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from gateway.contracts import (
    AuthenticationError,
    Direction,
    ProtocolError,
    RecordHeader,
    RecordType,
    SecureSession,
    SessionClosedError,
    SessionState,
    VerifiedRecord,
)
from gateway.protocol import HEADER, read_frame, write_frame


def certificates(directory: Path) -> None:
    """Fresh per-role self-signed P-256 certificates, explicitly trusted by peers."""
    now = datetime.now(UTC)
    for role in ("client", "gateway", "simulator"):
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, role)])
        cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1))
            .not_valid_after(now + timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(role)]), critical=False)
            .add_extension(
                x509.ExtendedKeyUsage(
                    [ExtendedKeyUsageOID.CLIENT_AUTH, ExtendedKeyUsageOID.SERVER_AUTH]
                ),
                critical=False,
            )
            .sign(key, hashes.SHA256())
        )
        (directory / f"{role}.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        path = directory / f"{role}.key"
        with path.open("xb") as handle:
            os.chmod(path, 0o600)
            handle.write(
                key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                )
            )


def context(directory: Path, local: str, remote: str, *, server: bool) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER if server else ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ctx.maximum_version = ssl.TLSVersion.TLSv1_3
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.load_cert_chain(directory / f"{local}.pem", directory / f"{local}.key")
    ctx.load_verify_locations(directory / f"{remote}.pem")
    ctx.options |= ssl.OP_NO_TICKET
    ctx.set_ecdh_curve("prime256v1")
    if server:
        ctx.num_tickets = 0
    # Construct directly: SSLKEYLOGFILE must never turn on secret logging.
    return ctx


class TransportSession:
    def __init__(self, manager, reader, writer, peer, sid, initiator):
        self.manager, self.reader, self.writer = manager, reader, writer
        self.peer, self.sid, self.initiator = peer, sid, initiator
        self.state = SessionState.ACTIVE
        self.sent = self.received = 0

    async def handshake(self):
        if self.state is not SessionState.ACTIVE:
            raise ProtocolError("closed transport")

    async def send(self, record_type, plaintext, *, request_id, chunk_index=0, end_of_message=True):
        if self.state is not SessionState.ACTIVE or len(plaintext) > 65536:
            raise ProtocolError("invalid transport send")
        header = HEADER.pack(
            1,
            record_type,
            self.sid,
            0 if self.initiator else 1,
            self.sent,
            request_id.bytes,
            chunk_index,
            end_of_message,
            len(plaintext),
        )
        self.sent += 1
        await write_frame(self.writer, header + plaintext)

    async def recv(self):
        data = await read_frame(self.reader)
        if len(data) < HEADER.size:
            raise ProtocolError("short transport frame")
        version, kind, sid, direction, sequence, rid, chunk, end, size = HEADER.unpack(
            data[: HEADER.size]
        )
        if (
            version != 1
            or sid != self.sid
            or sequence != self.received
            or direction != (1 if self.initiator else 0)
            or end not in (0, 1)
            or size != len(data) - HEADER.size
            or size > 65536
        ):
            raise ProtocolError("invalid transport frame")
        self.received += 1
        header = RecordHeader(
            RecordType(kind), sid, Direction(direction), sequence, UUID(bytes=rid), chunk, bool(end)
        )
        if header.record_type is RecordType.CLOSE:
            await self.close()
            raise SessionClosedError("transport closed")
        return VerifiedRecord(header, data[HEADER.size :])

    async def close(self):
        if self.state is SessionState.CLOSED:
            return
        self.state = SessionState.CLOSED
        self.manager.sessions.discard(self)
        self.writer.close()
        try:
            await self.writer.wait_closed()
        except (OSError, ssl.SSLError):
            pass


class TransportManager:
    """Benchmark-only: each listener trusts exactly the intended peer certificate."""

    def __init__(self, local, incoming, outgoing, address, directory, mode):
        self.local, self.incoming, self.outgoing = local, incoming, outgoing
        self.address, self.mode = address, mode
        self.sessions = set()
        self.negotiated = set()
        self.open_errors = Counter()
        self.timings = []
        self.server_context = (
            context(directory, local.role.value, incoming.role.value, server=True)
            if mode == "tls" and incoming
            else None
        )
        self.client_context = (
            context(directory, local.role.value, outgoing.role.value, server=False)
            if mode == "tls" and outgoing
            else None
        )

    def verify(self, writer, peer):
        if self.mode != "tls":
            return
        tls = writer.get_extra_info("ssl_object")
        names = [
            value
            for group in tls.getpeercert()["subject"]
            for key, value in group
            if key == "commonName"
        ]
        if (
            tls.version() != "TLSv1.3"
            or tls.session_reused
            or names != [peer.role.value]
            or tls.cipher()[0] != "TLS_AES_256_GCM_SHA384"
        ):
            raise AuthenticationError("TLS peer or full handshake mismatch")
        self.negotiated.add(tls.cipher()[0])

    async def open(self, peer):
        writer = None
        started = time.perf_counter()
        connected = None
        try:
            async with asyncio.timeout(5):
                reader, writer = await asyncio.open_connection(
                    "127.0.0.1",
                    self.address,
                )
                connected = time.perf_counter()
                if self.client_context:
                    await writer.start_tls(
                        self.client_context,
                        server_hostname=peer.role.value,
                        ssl_handshake_timeout=5,
                    )
                self.verify(writer, peer)
                sid = await read_frame(reader)
                if len(sid) != 16:
                    raise ProtocolError("invalid ready barrier")
                session = TransportSession(self, reader, writer, peer, sid, True)
                self.sessions.add(session)
                return session
        except BaseException as exc:
            self.open_errors[
                (type(exc).__name__, getattr(exc, "winerror", None), getattr(exc, "errno", None))
            ] += 1
            if writer:
                writer.close()
            raise
        finally:
            finished = time.perf_counter()
            self.timings.append(
                {
                    "tcp_ms": (connected - started) * 1000 if connected else None,
                    "security_ready_ms": (finished - connected) * 1000 if connected else None,
                    "secure_open_ms": (finished - started) * 1000,
                }
            )

    async def accept(self, reader, writer):
        try:
            self.verify(writer, self.incoming)
            sid = os.urandom(16)
            await write_frame(writer, sid)
            session = TransportSession(self, reader, writer, self.incoming, sid, False)
            self.sessions.add(session)
            return session
        except BaseException:
            writer.close()
            raise

    async def rotate(self, session: SecureSession):
        return await self.open(session.peer)

    async def close(self):
        await asyncio.gather(*(s.close() for s in tuple(self.sessions)))
