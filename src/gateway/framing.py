"""Record framing helpers shared by the client and simulator business layers.

Business messages may exceed the per-record plaintext limit and must be split
into consecutive records. These helpers own that framing so the client and
simulator share one encoding of a logical message across records; chunk order,
request identity and record type are validated on receive.
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from gateway.contracts import (
    MAX_RECORD_PLAINTEXT,
    ProtocolError,
    RecordType,
    SecureSession,
    VerifiedRecord,
)


def split_payload(payload: bytes) -> Sequence[bytes]:
    """Split a payload into record-sized chunks, always returning at least one."""
    if not payload:
        return [b""]
    return [
        payload[start : start + MAX_RECORD_PLAINTEXT]
        for start in range(0, len(payload), MAX_RECORD_PLAINTEXT)
    ]


async def send_message(
    session: SecureSession,
    record_type: RecordType,
    payload: bytes,
    *,
    request_id: UUID,
) -> None:
    """Send one logical message, splitting it across records when necessary."""
    chunks = split_payload(payload)
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
) -> bytes:
    """Assemble one logical message from consecutive records and validate framing.

    ``first`` carries an already-received leading record, which lets callers
    inspect the record type before deciding to assemble a message.
    """
    parts: list[bytes] = []
    next_index = 0
    if first is not None:
        _check(first, request_id, record_type, next_index)
        parts.append(first.plaintext)
        next_index = 1
        if first.header.end_of_message:
            return b"".join(parts)
    while True:
        record = await session.recv()
        _check(record, request_id, record_type, next_index)
        parts.append(record.plaintext)
        next_index += 1
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
