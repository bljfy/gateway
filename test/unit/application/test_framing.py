"""Record framing: payload splitting, message assembly and validation."""

from uuid import UUID, uuid4

import pytest

from gateway.contracts import (
    MAX_RECORD_PLAINTEXT,
    Direction,
    PeerIdentity,
    PeerRole,
    ProtocolError,
    RecordHeader,
    RecordType,
    VerifiedRecord,
)
from gateway.framing import recv_message, send_message, split_payload
from test.fixtures.in_memory import SESSION_ID, InMemorySession

REQUEST_ID = uuid4()


def make_record(
    plaintext: bytes,
    *,
    request_id: UUID = REQUEST_ID,
    record_type: RecordType = RecordType.REQUEST,
    chunk_index: int = 0,
    end_of_message: bool = True,
) -> VerifiedRecord:
    return VerifiedRecord(
        RecordHeader(
            record_type,
            SESSION_ID,
            Direction.INITIATOR_TO_ACCEPTOR,
            chunk_index,
            request_id,
            chunk_index,
            end_of_message,
        ),
        plaintext,
    )


def make_session() -> InMemorySession:
    return InMemorySession(PeerIdentity("client", PeerRole.CLIENT))


def test_split_payload_returns_single_empty_chunk() -> None:
    assert split_payload(b"") == [b""]


def test_split_payload_keeps_small_payload_intact() -> None:
    assert split_payload(b"abc") == [b"abc"]


def test_split_payload_respects_record_limit() -> None:
    payload = b"x" * (MAX_RECORD_PLAINTEXT * 2 + 7)
    chunks = list(split_payload(payload))
    assert all(len(chunk) <= MAX_RECORD_PLAINTEXT for chunk in chunks)
    assert b"".join(chunks) == payload
    assert len(chunks) == 3


@pytest.mark.asyncio
async def test_send_message_single_record() -> None:
    session = make_session()
    await session.handshake()
    await send_message(session, RecordType.REQUEST, b"payload", request_id=REQUEST_ID)
    assert len(session.sent) == 1
    record = session.sent[0]
    assert record.header.chunk_index == 0
    assert record.header.end_of_message is True
    assert record.header.request_id == REQUEST_ID
    assert record.plaintext == b"payload"


@pytest.mark.asyncio
async def test_send_message_splits_large_payload() -> None:
    session = make_session()
    await session.handshake()
    payload = b"y" * (MAX_RECORD_PLAINTEXT + 3)
    await send_message(session, RecordType.REQUEST, payload, request_id=REQUEST_ID)
    assert len(session.sent) == 2
    assert [r.header.chunk_index for r in session.sent] == [0, 1]
    assert [r.header.end_of_message for r in session.sent] == [False, True]
    assert b"".join(r.plaintext for r in session.sent) == payload


@pytest.mark.asyncio
async def test_recv_message_assembles_fragments() -> None:
    session = make_session()
    await session.handshake()
    session.enqueue(make_record(b"ab", chunk_index=0, end_of_message=False))
    session.enqueue(make_record(b"cd", chunk_index=1, end_of_message=True))
    assert await recv_message(session, REQUEST_ID, record_type=RecordType.REQUEST) == b"abcd"


@pytest.mark.asyncio
async def test_recv_message_accepts_already_received_first_record() -> None:
    session = make_session()
    await session.handshake()
    first = make_record(b"ab", chunk_index=0, end_of_message=False)
    session.enqueue(make_record(b"cd", chunk_index=1, end_of_message=True))
    assert (
        await recv_message(session, REQUEST_ID, record_type=RecordType.REQUEST, first=first)
        == b"abcd"
    )


@pytest.mark.asyncio
async def test_recv_message_rejects_mismatched_request_id() -> None:
    session = make_session()
    await session.handshake()
    session.enqueue(make_record(b"data", request_id=uuid4()))
    with pytest.raises(ProtocolError, match="request id"):
        await recv_message(session, REQUEST_ID, record_type=RecordType.REQUEST)


@pytest.mark.asyncio
async def test_recv_message_rejects_out_of_order_chunk() -> None:
    session = make_session()
    await session.handshake()
    session.enqueue(make_record(b"a", chunk_index=1))
    with pytest.raises(ProtocolError, match="chunk index"):
        await recv_message(session, REQUEST_ID, record_type=RecordType.REQUEST)


@pytest.mark.asyncio
async def test_recv_message_rejects_wrong_record_type() -> None:
    session = make_session()
    await session.handshake()
    session.enqueue(make_record(b"a", record_type=RecordType.RESPONSE))
    with pytest.raises(ProtocolError, match="record type"):
        await recv_message(session, REQUEST_ID, record_type=RecordType.REQUEST)
