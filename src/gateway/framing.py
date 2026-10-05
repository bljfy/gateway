"""Record framing helpers shared by the client and simulator business layers.

Business messages may exceed the per-record plaintext limit and must be split
into consecutive records. These helpers own that framing so the client and
simulator share one encoding of a logical message across records; chunk order,
request identity and record type are validated on receive.
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from gateway.codec import MAX_REQUEST_BODY_BYTES, MAX_RESPONSE_BODY_BYTES
from gateway.contracts import (
    MAX_RECORD_PLAINTEXT,
    ProtocolError,
    RecordType,
    SecureSession,
    VerifiedRecord,
)


async def recv_business_record(session: SecureSession) -> VerifiedRecord:
    """Consume authenticated heartbeats without advancing business framing."""
    while True:
        record = await session.recv()
        if record.header.record_type is not RecordType.HEARTBEAT:
            return record
        if (
            record.plaintext
            or record.header.request_id.int
            or record.header.chunk_index
            or not record.header.end_of_message
        ):
            raise ProtocolError("invalid heartbeat")


def split_payload(payload: bytes, record_bytes: int = MAX_RECORD_PLAINTEXT) -> Sequence[bytes]:
    """Split a payload into record-sized chunks, always returning at least one."""
    if type(record_bytes) is not int or not 1 <= record_bytes <= MAX_RECORD_PLAINTEXT:
        raise ValueError("invalid record size")
    if not payload:
        return [b""]
    return [payload[start : start + record_bytes] for start in range(0, len(payload), record_bytes)]


async def send_message(
    session: SecureSession,
    record_type: RecordType,
    payload: bytes,
    *,
    request_id: UUID,
    record_bytes: int = MAX_RECORD_PLAINTEXT,
) -> None:
    """Send one logical message, splitting it across records when necessary."""
    chunks = split_payload(payload, record_bytes)
    last_index = len(chunks) - 1
    for index, chunk in enumerate(chunks):
        await session.send(
            record_type,
            chunk,
            request_id=request_id,
            chunk_index=index,
            end_of_message=index == last_index,
        )


async def recv_message(
    session: SecureSession,
    request_id: UUID,
    *,
    record_type: RecordType,
    first: VerifiedRecord | None = None,
    max_bytes: int | None = None,
    record_bytes: int = MAX_RECORD_PLAINTEXT,
) -> bytes:
    """Assemble one logical message from consecutive records and validate framing.

    ``first`` carries an already-received leading record, which lets callers
    inspect the record type before deciding to assemble a message.
    """
    limit = (
        max_bytes
        if max_bytes is not None
        else (
            MAX_REQUEST_BODY_BYTES if record_type is RecordType.REQUEST else MAX_RESPONSE_BODY_BYTES
        )
    )
    if type(limit) is not int or limit < 1:
        raise ValueError("message limit must be positive")
    if type(record_bytes) is not int or not 1 <= record_bytes <= MAX_RECORD_PLAINTEXT:
        raise ValueError("invalid record size")
    parts: list[bytes] = []
    size = 0
    cost = 0
    while True:
        record = first if first is not None else await recv_business_record(session)
        first = None
        _check(record, request_id, record_type, len(parts))
        if len(record.plaintext) > record_bytes:
            raise ProtocolError("record exceeds configured plaintext limit")
        size += len(record.plaintext)
        cost += len(record.plaintext) + 128
        if size > limit or cost > limit * 2:
            raise ProtocolError("business message exceeds receive budget")
        if not record.plaintext and not record.header.end_of_message:
            raise ProtocolError("empty intermediate business fragment")
        parts.append(record.plaintext)
        if record.header.end_of_message:
            return b"".join(parts)


def _check(
    record: VerifiedRecord,
    request_id: UUID,
    record_type: RecordType,
    expected_index: int,
) -> None:
    if record.header.request_id != request_id:
        raise ProtocolError("record carries an unexpected request id")
    if record.header.record_type != record_type:
        raise ProtocolError("unexpected record type while receiving a business message")
    if record.header.chunk_index != expected_index:
        raise ProtocolError("record chunk index is out of order")
