"""Business payload codec: round trips and malformed input rejection."""

import struct
from uuid import UUID, uuid4

import pytest

from gateway.codec import (
    CODEC_VERSION,
    MAX_CONTEXT_ITEMS,
    MAX_MODEL_BYTES,
    MAX_REQUEST_BODY_BYTES,
    decode_request,
    decode_response,
    encode_request,
    encode_response,
)
from gateway.contracts import InferenceRequest, InferenceResponse, ProtocolError

REQUEST_ID = UUID(int=1)


def make_request(**overrides: object) -> InferenceRequest:
    fields: dict[str, object] = {
        "request_id": REQUEST_ID,
        "model": "mock-model",
        "prompt": "hello",
        "retrieval_context": ("context-a", "context-b"),
        "max_output_tokens": 64,
    }
    fields.update(overrides)
    return InferenceRequest(**fields)  # type: ignore[arg-type]


def make_response() -> InferenceResponse:
    return InferenceResponse(request_id=REQUEST_ID, output="mock output")


@pytest.mark.parametrize("stream", [False, True])
def test_request_roundtrip_preserves_fields_and_stream_flag(stream: bool) -> None:
    request = make_request()
    decoded, is_stream = decode_request(encode_request(request, stream=stream))
    assert is_stream is stream
    assert decoded == request
    assert decoded.request_id == request.request_id
    assert decoded.model == request.model
    assert decoded.prompt == request.prompt
    assert decoded.retrieval_context == request.retrieval_context
    assert decoded.max_output_tokens == request.max_output_tokens


def test_request_roundtrip_unicode() -> None:
    request = make_request(prompt="国密安全网关", retrieval_context=("检索片段", "网"))
    decoded, is_stream = decode_request(encode_request(request))
    assert is_stream is False
    assert decoded == request


def test_response_roundtrip() -> None:
    response = make_response()
    assert decode_response(encode_response(response)) == response


def test_response_roundtrip_unicode() -> None:
    response = InferenceResponse(request_id=REQUEST_ID, output="模拟输出")
    assert decode_response(encode_response(response)) == response


def test_decode_rejects_truncated_payload() -> None:
    encoded = encode_request(make_request())
    for length in range(len(encoded)):
        with pytest.raises(ProtocolError):
            decode_request(encoded[:length])


def test_decode_rejects_trailing_bytes() -> None:
    encoded = encode_request(make_request())
    with pytest.raises(ProtocolError, match="trailing"):
        decode_request(encoded + b"\x00")


def test_decode_rejects_unknown_version() -> None:
    with pytest.raises(ProtocolError, match="version"):
        decode_request(struct.pack(">BB", CODEC_VERSION + 1, 1) + b"\x00" * 16)


def test_decode_rejects_unknown_message_type() -> None:
    with pytest.raises(ProtocolError, match="message type"):
        decode_request(struct.pack(">BB", CODEC_VERSION, 99))


def test_decode_response_rejects_request_type() -> None:
    with pytest.raises(ProtocolError, match="message type"):
        decode_response(encode_request(make_request()))


def test_decode_rejects_empty_model() -> None:
    frame = struct.pack(">BB", CODEC_VERSION, 1) + REQUEST_ID.bytes + struct.pack(">H", 0)
    with pytest.raises(ProtocolError, match="model"):
        decode_request(frame)


def test_decode_rejects_invalid_utf8_model() -> None:
    frame = struct.pack(">BB", CODEC_VERSION, 1) + REQUEST_ID.bytes + struct.pack(">H", 1) + b"\xff"
    with pytest.raises(ProtocolError, match="UTF-8"):
        decode_request(frame)


def test_encode_rejects_oversized_model() -> None:
    request = make_request(model="x" * (MAX_MODEL_BYTES + 1))
    with pytest.raises(ProtocolError, match="model"):
        encode_request(request)


def test_encode_rejects_too_many_context_items() -> None:
    request = make_request(retrieval_context=tuple(f"c{i}" for i in range(MAX_CONTEXT_ITEMS + 1)))
    with pytest.raises(ProtocolError, match="context"):
        encode_request(request)


def test_encode_rejects_out_of_range_token_budget() -> None:
    request = make_request(max_output_tokens=2**32)
    with pytest.raises(ProtocolError, match="max_output_tokens"):
        encode_request(request)


def test_decode_does_not_leak_payload_in_errors() -> None:
    encoded = encode_request(make_request(prompt="secret-marker"))
    with pytest.raises(ProtocolError) as exc_info:
        decode_request(encoded[:5])
    assert "secret-marker" not in str(exc_info.value)


def test_request_id_is_a_real_uuid() -> None:
    request = make_request(request_id=uuid4())
    decoded, _ = decode_request(encode_request(request))
    assert decoded.request_id == request.request_id
    assert isinstance(decoded.request_id, UUID)


def test_decode_rejects_total_body_over_budget() -> None:
    prompt = b"x" * MAX_REQUEST_BODY_BYTES
    body = (
        REQUEST_ID.bytes
        + struct.pack(">H", 5)
        + b"model"
        + struct.pack(">I", len(prompt))
        + prompt
        + struct.pack(">H", 1)
        + struct.pack(">I", 3)
        + b"ctx"
        + struct.pack(">I", 64)
    )
    frame = struct.pack(">BB", CODEC_VERSION, 1) + body
    with pytest.raises(ProtocolError, match="request body"):
        decode_request(frame)


def test_decode_response_rejects_truncated() -> None:
    encoded = encode_response(make_response())
    for length in range(len(encoded)):
        with pytest.raises(ProtocolError):
            decode_response(encoded[:length])


def test_decode_response_rejects_trailing_bytes() -> None:
    with pytest.raises(ProtocolError, match="trailing"):
        decode_response(encode_response(make_response()) + b"\x00")
