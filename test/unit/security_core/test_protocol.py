import asyncio
from uuid import UUID

import pytest

from gateway.contracts import (
    Direction,
    PeerIdentity,
    PeerRole,
    ProtocolError,
    RecordHeader,
    RecordType,
)
from gateway.protocol import (
    HEADER,
    MAX_FRAME,
    decode_record,
    encode_header,
    fields,
    identity,
    parse_identity,
    read_frame,
    unfields,
)


def test_canonical_header() -> None:
    header = RecordHeader(
        RecordType.REQUEST,
        b"s" * 16,
        Direction.INITIATOR_TO_ACCEPTOR,
        2**64 - 1,
        UUID(int=1),
        2**32 - 1,
    )
    wire = encode_header(header, 16)
    assert HEADER.size == 52
    assert wire.hex() == (
        "0101" + "73" * 16 + "00ffffffffffffffff" + "00" * 15 + "01ffffffff0100000010"
    )
    assert decode_record(wire + b"t" * 16) == (header, wire, b"t" * 16)


@pytest.mark.parametrize("data", [b"", b"\x00", b"\x00\x03a", b"\x00\x01ab"])
def test_malformed_fields(data: bytes) -> None:
    with pytest.raises(ProtocolError):
        unfields(data, 1)


def test_identity_roundtrip_and_invalid_utf8() -> None:
    peer = PeerIdentity("测试", PeerRole.GATEWAY, 9)
    assert parse_identity(identity(peer)) == peer
    with pytest.raises(ProtocolError):
        parse_identity(fields(b"\xff", b"gateway", b"\x00\x00\x00\x01"))


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [0, MAX_FRAME + 1])
async def test_frame_bound_before_payload(size: int) -> None:
    reader = asyncio.StreamReader()
    reader.feed_data(size.to_bytes(4))
    with pytest.raises(ProtocolError):
        await read_frame(reader)


@pytest.mark.asyncio
async def test_truncated_frame() -> None:
    reader = asyncio.StreamReader()
    reader.feed_data(b"\x00\x00\x00\x04xx")
    reader.feed_eof()
    with pytest.raises(ProtocolError):
        await read_frame(reader)
