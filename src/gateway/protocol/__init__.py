"""Canonical bounded wire codec for security contract 1.0."""

import asyncio
import struct
from uuid import UUID

from gateway.contracts import (
    MAX_RECORD_PLAINTEXT,
    Direction,
    PeerIdentity,
    PeerRole,
    ProtocolError,
    RecordHeader,
    RecordType,
)

SUITE = b"SM2-SM4-GCM-SM3-v1"
MAX_FRAME = 131_072
HEADER = struct.Struct("!BB16sBQ16sIBI")
ZERO_REQUEST = UUID(int=0)


def fields(*values: bytes) -> bytes:
    if any(len(value) > 65535 for value in values):
        raise ProtocolError("field too large")
    return b"".join(struct.pack("!H", len(value)) + value for value in values)


def unfields(data: bytes, count: int) -> tuple[bytes, ...]:
    result = []
    for _ in range(count):
        if len(data) < 2:
            raise ProtocolError("truncated field")
        size = int.from_bytes(data[:2], "big")
        if len(data) < size + 2:
            raise ProtocolError("truncated field")
        result.append(data[2 : size + 2])
        data = data[size + 2 :]
    if data:
        raise ProtocolError("trailing fields")
    return tuple(result)


def identity(peer: PeerIdentity) -> bytes:
    if not 1 <= peer.key_version < 2**32:
        raise ProtocolError("key version outside wire range")
    return fields(
        peer.peer_id.encode(), peer.role.value.encode(), struct.pack("!I", peer.key_version)
    )


def parse_identity(data: bytes) -> PeerIdentity:
    name, role, version = unfields(data, 3)
    try:
        if len(version) != 4:
            raise ValueError
        return PeerIdentity(
            name.decode("utf-8"), PeerRole(role.decode("ascii")), int.from_bytes(version)
        )
    except (ValueError, UnicodeError):
        raise ProtocolError("invalid peer identity") from None


def encode_header(header: RecordHeader, ciphertext_length: int) -> bytes:
    if not 16 <= ciphertext_length <= MAX_RECORD_PLAINTEXT + 16:
        raise ProtocolError("invalid ciphertext length")
    if type(header.end_of_message) is not bool:
        raise ProtocolError("invalid end flag")
    return HEADER.pack(
        header.protocol_version,
        header.record_type,
        header.session_id,
        header.direction,
        header.sequence,
        header.request_id.bytes,
        header.chunk_index,
        header.end_of_message,
        ciphertext_length,
    )


def decode_record(data: bytes) -> tuple[RecordHeader, bytes, bytes]:
    if len(data) < HEADER.size + 16:
        raise ProtocolError("truncated record")
    version, kind, sid, direction, seq, rid, chunk, end, size = HEADER.unpack(data[: HEADER.size])
    try:
        if end not in (0, 1) or not 16 <= size <= MAX_RECORD_PLAINTEXT + 16:
            raise ValueError
        if len(data) != HEADER.size + size:
            raise ValueError
        header = RecordHeader(
            RecordType(kind),
            sid,
            Direction(direction),
            seq,
            UUID(bytes=rid),
            chunk,
            bool(end),
            version,
        )
    except ValueError:
        raise ProtocolError("invalid record header") from None
    return header, data[: HEADER.size], data[HEADER.size :]


async def read_frame(reader: asyncio.StreamReader) -> bytes:
    try:
        size = int.from_bytes(await reader.readexactly(4), "big")
        if not 1 <= size <= MAX_FRAME:
            raise ProtocolError("invalid frame length")
        return await reader.readexactly(size)
    except asyncio.IncompleteReadError:
        raise ProtocolError("transport truncated") from None


async def write_frame(writer: asyncio.StreamWriter, data: bytes) -> None:
    if not 1 <= len(data) <= MAX_FRAME:
        raise ProtocolError("invalid frame length")
    writer.write(struct.pack("!I", len(data)) + data)
    await writer.drain()
