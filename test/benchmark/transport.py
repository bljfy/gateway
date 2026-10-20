"""TLS 1.3 and plaintext measurement adapters for the existing business stack."""

import asyncio
import math
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

from gateway.config import Limits
from gateway.contracts import (
    AuthenticationError,
    Direction,
    ProtocolError,
    RecordHeader,
    RecordType,
    SecureSession,
    SessionClosedError,
    SessionPolicy,
    SessionState,
    VerifiedRecord,
)
from gateway.protocol import HEADER, read_frame, write_frame
from gateway.server.pool import UpstreamPool


def certificates(directory: Path) -> None:
    """Fresh per-role self-signed P-256 certificates, explicitly trusted by peers."""
    now = datetime.now(UTC)
    for role in ("client", "gateway", "adapter"):
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


class ResumingContext(ssl.SSLContext):
    """Supply an explicit session through asyncio's public wrap_bio hook.

    asyncio.start_tls has no session argument. Sessions remain in memory and
    belong to this exact context/peer; no process-global SSL monkeypatch is used.
    """

    cached_session = None
    cached_hostname = None
    cache_deadline = 0.0

    def wrap_bio(self, incoming, outgoing, server_side=False, server_hostname=None, session=None):
        if (
            not server_side
            and server_hostname == self.cached_hostname
            and time.monotonic() < self.cache_deadline
        ):
            session = self.cached_session
        return super().wrap_bio(incoming, outgoing, server_side, server_hostname, session=session)


def context(directory: Path, local: str, remote: str, *, server: bool, resume=False):
    cls = ResumingContext if resume and not server else ssl.SSLContext
    ctx = cls(ssl.PROTOCOL_TLS_SERVER if server else ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ctx.maximum_version = ssl.TLSVersion.TLSv1_3
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.load_cert_chain(directory / f"{local}.pem", directory / f"{local}.key")
    ctx.load_verify_locations(directory / f"{remote}.pem")
    if not resume:
        ctx.options |= ssl.OP_NO_TICKET
    ctx.set_ecdh_curve("prime256v1")
    if server:
        ctx.num_tickets = 2 if resume else 0
    # Construct directly: SSLKEYLOGFILE must never turn on secret logging.
    return ctx


class TransportSession:
    def __init__(self, manager, reader, writer, peer, sid, initiator):
        self.manager, self.reader, self.writer = manager, reader, writer
        self.peer, self.sid, self.initiator = peer, sid, initiator
        self.state = SessionState.ACTIVE
        self.sent = self.received = 0
        self.resumed = False
        self.created = self.last_used = time.monotonic()
        self.sent_bytes = self.received_bytes = 0

    def usable_for_pool(self, config, request_records):
        now = time.monotonic()
        policy, limits = config.session, config.limits
        reserve = max(
            policy.max_records_per_direction // 2,
            math.ceil(limits.max_response_body_bytes / limits.max_record_plaintext_bytes) + 1,
        )
        return (
            not self.writer.is_closing()
            and not self.reader.at_eof()
            and now - self.created
            < policy.absolute_lifetime_seconds
            - limits.request_timeout_seconds
            - policy.rotate_before_seconds
            and now - self.last_used < policy.idle_timeout_seconds
            and self.sent + request_records <= policy.max_records_per_direction
            and self.received + reserve <= policy.max_records_per_direction
            and self.sent_bytes + limits.max_request_body_bytes
            <= limits.max_plaintext_bytes_per_direction
            and self.received_bytes + limits.max_response_body_bytes
            <= limits.max_plaintext_bytes_per_direction
        )

    def check_budget(self, size, *, sending):
        now, policy = time.monotonic(), self.manager.policy
        count = self.sent if sending else self.received
        total = self.sent_bytes if sending else self.received_bytes
        if (
            self.state is not SessionState.ACTIVE
            or now - self.created >= policy.absolute_lifetime_seconds
            or now - self.last_used >= policy.idle_timeout_seconds
            or count >= policy.max_records_per_direction
            or total + size > self.manager.limits.max_plaintext_bytes_per_direction
        ):
            raise ProtocolError("transport budget exhausted")
        self.last_used = now

    async def handshake(self):
        if self.state is not SessionState.ACTIVE:
            raise ProtocolError("closed transport")

    async def send(self, record_type, plaintext, *, request_id, chunk_index=0, end_of_message=True):
        if self.state is not SessionState.ACTIVE or len(plaintext) > 65536:
            raise ProtocolError("invalid transport send")
        self.check_budget(len(plaintext), sending=True)
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
        self.sent_bytes += len(plaintext)
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
        self.check_budget(size, sending=False)
        self.received += 1
        self.received_bytes += size
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


class TransportPool(UpstreamPool):
    """Apply the same lease lifetime and budget reservations to the TLS adapter."""

    def _usable(self, session, request_records=1):
        return super()._usable(session, request_records) and session.usable_for_pool(
            self.config, request_records
        )


class TransportManager:
    """Benchmark-only: each listener trusts exactly the intended peer certificate."""

    def __init__(self, local, incoming, outgoing, address, directory, mode):
        self.local, self.incoming, self.outgoing = local, incoming, outgoing
        self.address, self.mode = address, mode
        self.sessions = set()
        self.negotiated = set()
        self.open_errors = Counter()
        self.timings = []
        self.resumption_enabled = mode in {"tls-resume", "tls-combined"}
        self.handshakes = Counter()
        self.policy, self.limits = SessionPolicy(), Limits()
        self.server_context = (
            context(
                directory,
                local.role.value,
                incoming.role.value,
                server=True,
                resume=self.resumption_enabled,
            )
            if mode.startswith("tls") and incoming
            else None
        )
        self.client_context = (
            context(
                directory,
                local.role.value,
                outgoing.role.value,
                server=False,
                resume=self.resumption_enabled,
            )
            if mode.startswith("tls") and outgoing
            else None
        )

    def verify(self, writer, peer):
        if not self.mode.startswith("tls"):
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
            or (tls.session_reused and not self.resumption_enabled)
            or names != [peer.role.value]
            or tls.cipher()[0] != "TLS_AES_256_GCM_SHA384"
        ):
            raise AuthenticationError("TLS peer or full handshake mismatch")
        self.negotiated.add(tls.cipher()[0])

    def authenticated(self, session):
        if not self.mode.startswith("tls"):
            return
        tls = session.writer.get_extra_info("ssl_object")
        session.resumed = tls.session_reused
        self.handshakes["resumed" if session.resumed else "full"] += 1
        # Tickets arrive after the TLS handshake. READY is read before caching,
        # so OpenSSL has processed the server's NewSessionTicket messages.
        if session.initiator and self.resumption_enabled and tls.session.has_ticket:
            if not session.resumed:
                self.client_context.cache_deadline = time.monotonic() + 1800
            self.client_context.cached_session = tls.session
            self.client_context.cached_hostname = session.peer.role.value

    async def open(self, peer):
        writer = None
        started = time.perf_counter()
        connected = None
        session = None
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
                self.authenticated(session)
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
                    "resumed": session.resumed if session else False,
                }
            )

    async def accept(self, reader, writer):
        try:
            self.verify(writer, self.incoming)
            sid = os.urandom(16)
            await write_frame(writer, sid)
            session = TransportSession(self, reader, writer, self.incoming, sid, False)
            self.authenticated(session)
            self.sessions.add(session)
            return session
        except BaseException:
            writer.close()
            raise

    async def rotate(self, session: SecureSession):
        return await self.open(session.peer)

    async def close(self):
        await asyncio.gather(*(s.close() for s in tuple(self.sessions)))
