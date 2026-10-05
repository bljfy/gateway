"""Client business layer: request/reply and streaming over a session double."""

from uuid import UUID, uuid4

import pytest

from gateway.client import InferenceClient
from gateway.codec import decode_request, encode_response
from gateway.contracts import (
    Direction,
    InferenceRequest,
    InferenceResponse,
    InferenceService,
    PeerIdentity,
    PeerRole,
    ProtocolError,
    RecordHeader,
    RecordType,
    VerifiedRecord,
)
from test.fixtures.in_memory import SESSION_ID, InMemorySession

REQUEST_ID = uuid4()


def make_request(**overrides: object) -> InferenceRequest:
    fields: dict[str, object] = {
        "request_id": REQUEST_ID,
        "model": "mock-model",
        "prompt": "hello",
    }
    fields.update(overrides)
    return InferenceRequest(**fields)  # type: ignore[arg-type]


def make_record(
    plaintext: bytes,
    *,
    request_id: UUID = REQUEST_ID,
    record_type: RecordType = RecordType.RESPONSE,
    chunk_index: int = 0,
    end_of_message: bool = True,
) -> VerifiedRecord:
    return VerifiedRecord(
        RecordHeader(
            record_type,
            SESSION_ID,
            Direction.ACCEPTOR_TO_INITIATOR,
            chunk_index,
            request_id,
            chunk_index,
            end_of_message,
        ),
        plaintext,
    )


def make_session() -> InMemorySession:
    return InMemorySession(PeerIdentity("client", PeerRole.CLIENT))


def test_client_conforms_to_inference_service() -> None:
    assert isinstance(InferenceClient(make_session()), InferenceService)


@pytest.mark.asyncio
async def test_complete_roundtrip() -> None:
    request = make_request()
    response = InferenceResponse(request_id=REQUEST_ID, output="mock output")
    session = make_session()
    await session.handshake()
    session.enqueue(make_record(encode_response(response)))

    result = await InferenceClient(session).complete(request)
    assert result == response

    sent, _ = decode_request(session.sent[0].plaintext)
    assert sent == request


@pytest.mark.asyncio
async def test_complete_rejects_response_with_wrong_request_id() -> None:
    request = make_request()
    session = make_session()
    await session.handshake()
    response = InferenceResponse(request_id=uuid4(), output="mock output")
    session.enqueue(make_record(encode_response(response)))

    with pytest.raises(ProtocolError, match="request id"):
        await InferenceClient(session).complete(request)


@pytest.mark.asyncio
async def test_complete_assembles_fragmented_response() -> None:
    request = make_request()
    response = InferenceResponse(request_id=REQUEST_ID, output="a" * 100)
    encoded = encode_response(response)
    session = make_session()
    await session.handshake()
    session.enqueue(make_record(encoded[:40], chunk_index=0, end_of_message=False))
    session.enqueue(make_record(encoded[40:], chunk_index=1, end_of_message=True))

    assert await InferenceClient(session).complete(request) == response


@pytest.mark.asyncio
async def test_stream_yields_ordered_chunks_with_single_final() -> None:
    request = make_request()
    session = make_session()
    await session.handshake()
    session.enqueue(make_record(b"hello", chunk_index=0, end_of_message=False))
    session.enqueue(make_record(b"world", chunk_index=1, end_of_message=True))

    chunks = [chunk async for chunk in InferenceClient(session).stream(request)]
    assert [chunk.output for chunk in chunks] == ["hello", "world"]
    assert [chunk.index for chunk in chunks] == [0, 1]
    assert [chunk.is_final for chunk in chunks] == [False, True]


@pytest.mark.asyncio
async def test_stream_sends_stream_flag() -> None:
    request = make_request()
    session = make_session()
    await session.handshake()
    session.enqueue(make_record(b"done", chunk_index=0, end_of_message=True))

    async for _ in InferenceClient(session).stream(request):
        pass

    _, is_stream = decode_request(session.sent[0].plaintext)
    assert is_stream is True


@pytest.mark.asyncio
async def test_stream_rejects_out_of_order_chunk() -> None:
    request = make_request()
    session = make_session()
    await session.handshake()
    session.enqueue(make_record(b"late", chunk_index=1, end_of_message=True))

    with pytest.raises(ProtocolError, match="chunk index"):
        async for _ in InferenceClient(session).stream(request):
            pass


@pytest.mark.asyncio
async def test_stream_rejects_non_utf8_chunk() -> None:
    request = make_request()
    session = make_session()
    await session.handshake()
    session.enqueue(make_record(b"\xff", chunk_index=0, end_of_message=True))

    with pytest.raises(ProtocolError, match="UTF-8"):
        async for _ in InferenceClient(session).stream(request):
            pass


@pytest.mark.asyncio
async def test_stream_rejects_mismatched_request_id() -> None:
    request = make_request()
    session = make_session()
    await session.handshake()
    session.enqueue(make_record(b"data", request_id=uuid4()))

    with pytest.raises(ProtocolError, match="request id"):
        async for _ in InferenceClient(session).stream(request):
            pass


@pytest.mark.asyncio
async def test_stream_propagates_error_when_truncated() -> None:
    request = make_request()
    session = make_session()
    await session.handshake()
    session.enqueue(make_record(b"partial", chunk_index=0, end_of_message=False))

    iterator = InferenceClient(session).stream(request)
    first = await anext(iterator)
    assert first.output == "partial"
    assert first.is_final is False

    await session.close()
    with pytest.raises(ProtocolError):
        await anext(iterator)
