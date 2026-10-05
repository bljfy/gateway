"""In-memory :class:`SecureSession` double for exercising the business layers.

Test doubles live only under ``test/`` and never authenticate a peer; they exist
to verify the client/simulator/framing contracts while the security-core line is
developed in parallel.
"""

from __future__ import annotations

import asyncio
from uuid import UUID

from gateway.contracts import (
    Direction,
    PeerIdentity,
    ProtocolError,
    RecordHeader,
    RecordType,
    SessionClosedError,
    SessionState,
    VerifiedRecord,
)

SESSION_ID = b"s" * 16


class InMemorySession:
    """Records sent to ``remote`` are forwarded to the peer's receive queue."""

    def __init__(self, peer: PeerIdentity, *, remote: InMemorySession | None = None) -> None:
        self._peer = peer
        self._state = SessionState.NEW
        self._inbox: asyncio.Queue[VerifiedRecord] = asyncio.Queue()
        self._remote = remote
        self._sequence = 0
        self.sent: list[VerifiedRecord] = []

    @property
    def peer(self) -> PeerIdentity:
        return self._peer

    @property
    def state(self) -> SessionState:
        return self._state

    def attach(self, remote: InMemorySession) -> None:
        self._remote = remote

    def enqueue(self, record: VerifiedRecord) -> None:
        self._inbox.put_nowait(record)

    async def handshake(self) -> None:
        self._state = SessionState.ACTIVE

    async def send(
        self,
        record_type: RecordType,
        plaintext: bytes,
        *,
        request_id: UUID,
        chunk_index: int = 0,
        end_of_message: bool = True,
    ) -> None:
        if self._state is not SessionState.ACTIVE:
            raise ProtocolError("session is not active")
        header = RecordHeader(
            record_type,
            SESSION_ID,
            Direction.INITIATOR_TO_ACCEPTOR,
            self._sequence,
            request_id,
            chunk_index,
            end_of_message,
        )
        self._sequence += 1
        record = VerifiedRecord(header, plaintext)
        self.sent.append(record)
        if self._remote is not None:
            self._remote.enqueue(record)

    async def recv(self) -> VerifiedRecord:
        if self._state is not SessionState.ACTIVE:
            raise ProtocolError("session is not active")
        record = await self._inbox.get()
        if record.header.record_type in (RecordType.CLOSE, RecordType.CLOSE_ACK):
            self._state = SessionState.CLOSED
            raise SessionClosedError("peer closed session")
        return record

    async def close(self) -> None:
        self._state = SessionState.CLOSED


def connect_pair(
    client_peer: PeerIdentity, server_peer: PeerIdentity
) -> tuple[InMemorySession, InMemorySession]:
    """Return two sessions whose sends are forwarded to each other."""
    client = InMemorySession(client_peer)
    server = InMemorySession(server_peer)
    client.attach(server)
    server.attach(client)
    return client, server
