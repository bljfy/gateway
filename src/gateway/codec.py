"""Deterministic, versioned, length-bounded encoding for business payloads.

Business payloads are the plaintext carried inside authenticated records. The
client and simulator business layers use this module to convert the shared DTOs
into bytes and back. The encoding is the single source of truth for the business
wire format: fixed field order, explicit lengths and strict rejection of trailing
bytes, so a malformed record cannot smuggle additional data past the decoder.

The security header (:class:`~gateway.contracts.RecordHeader`) is owned by the
protocol line; this module only ever sees already-authenticated plaintext.
"""

from __future__ import annotations

import struct
from enum import IntEnum
from uuid import UUID

from gateway.contracts import (
    InferenceRequest,
    InferenceResponse,
    ProtocolError,
)

CODEC_VERSION = 1
MAX_MODEL_BYTES = 256
MAX_REQUEST_BODY_BYTES = 1_048_576
MAX_RESPONSE_BODY_BYTES = 16_777_216
MAX_CONTEXT_ITEMS = 64

_HEADER = struct.Struct(">BB")
_U16 = struct.Struct(">H")
_U32 = struct.Struct(">I")


class MessageType(IntEnum):
    """Business message kinds carried inside authenticated records."""

    REQUEST = 1
    STREAM_REQUEST = 2
    RESPONSE = 3


def encode_request(request: InferenceRequest, *, stream: bool = False) -> bytes:
    """Encode an inference request, optionally requesting streaming output."""
    model = request.model.encode("utf-8")
    if not 0 < len(model) <= MAX_MODEL_BYTES:
        raise ProtocolError("model byte length is out of bounds")
    if len(request.retrieval_context) > MAX_CONTEXT_ITEMS:
        raise ProtocolError("too many retrieval context items")
    if (
        len(request.prompt) > MAX_REQUEST_BODY_BYTES
        or sum(len(item) for item in request.retrieval_context) > MAX_REQUEST_BODY_BYTES
    ):
        raise ProtocolError("request body exceeds the configured limit")
    prompt = request.prompt.encode("utf-8")
    context = [item.encode("utf-8") for item in request.retrieval_context]
    if not 0 < request.max_output_tokens <= 0xFFFFFFFF:
        raise ProtocolError("max_output_tokens is out of bounds")

    body = bytearray()
    body += request.request_id.bytes
    body += _U16.pack(len(model))
    body += model
    body += _U32.pack(len(prompt))
    body += prompt
    body += _U16.pack(len(context))
    for item in context:
        body += _U32.pack(len(item))
        body += item
    body += _U32.pack(request.max_output_tokens)
    if len(body) + _HEADER.size > MAX_REQUEST_BODY_BYTES:
        raise ProtocolError("request body exceeds the configured limit")

    message_type = MessageType.STREAM_REQUEST if stream else MessageType.REQUEST
    return _HEADER.pack(CODEC_VERSION, int(message_type)) + bytes(body)


def decode_request(data: bytes) -> tuple[InferenceRequest, bool]:
    """Decode a request, returning the DTO and whether streaming was requested."""
    message_type = _read_header(data, MessageType.REQUEST, MessageType.STREAM_REQUEST)
    if len(data) > MAX_REQUEST_BODY_BYTES:
        raise ProtocolError("request body exceeds the configured limit")
    offset = _HEADER.size

    request_id = UUID(bytes=_read_bytes(data, offset, 16, "request id"))
    offset += 16
    model, offset = _read_text(data, offset, _U16, "model", MAX_MODEL_BYTES)
    if not model:
        raise ProtocolError("model must not be empty")
    prompt, offset = _read_text(data, offset, _U32, "prompt", MAX_REQUEST_BODY_BYTES)
    count, offset = _read_uint(data, offset, _U16, "context count")
    if count > MAX_CONTEXT_ITEMS:
        raise ProtocolError("too many retrieval context items")
    context: list[str] = []
    for _ in range(count):
        item, offset = _read_text(data, offset, _U32, "context item", MAX_REQUEST_BODY_BYTES)
        context.append(item)
    max_output_tokens, offset = _read_uint(data, offset, _U32, "max_output_tokens")
    if max_output_tokens < 1:
        raise ProtocolError("max_output_tokens must be positive")
    _reject_trailing(data, offset)

    request = InferenceRequest(
        request_id=request_id,
        model=model,
        prompt=prompt,
        retrieval_context=tuple(context),
        max_output_tokens=max_output_tokens,
    )
    return request, message_type is MessageType.STREAM_REQUEST


def encode_response(response: InferenceResponse) -> bytes:
    """Encode a complete inference response."""
    if len(response.output) > MAX_RESPONSE_BODY_BYTES - 22:
        raise ProtocolError("response output exceeds the configured limit")
    output = response.output.encode("utf-8")
    if len(output) + 22 > MAX_RESPONSE_BODY_BYTES:
        raise ProtocolError("response output exceeds the configured limit")

    body = bytearray()
    body += response.request_id.bytes
    body += _U32.pack(len(output))
    body += output
    return _HEADER.pack(CODEC_VERSION, int(MessageType.RESPONSE)) + bytes(body)


def decode_response(data: bytes) -> InferenceResponse:
    """Decode a complete inference response."""
    if len(data) > MAX_RESPONSE_BODY_BYTES:
        raise ProtocolError("response body exceeds the configured limit")
    _read_header(data, MessageType.RESPONSE)
    offset = _HEADER.size

    request_id = UUID(bytes=_read_bytes(data, offset, 16, "request id"))
    offset += 16
    output, offset = _read_text(data, offset, _U32, "response output", MAX_RESPONSE_BODY_BYTES)
    _reject_trailing(data, offset)
    return InferenceResponse(request_id=request_id, output=output)


def _read_header(data: bytes, *allowed: MessageType) -> MessageType:
    if len(data) < _HEADER.size:
        raise ProtocolError("truncated business payload header")
    version, raw_type = _HEADER.unpack_from(data)
    if version != CODEC_VERSION:
        raise ProtocolError("unsupported business payload version")
    try:
        message_type = MessageType(raw_type)
    except ValueError as exc:
        raise ProtocolError("unknown business message type") from exc
    if message_type not in allowed:
        raise ProtocolError("unexpected business message type")
    return message_type


def _read_bytes(data: bytes, offset: int, length: int, field: str) -> bytes:
    end = offset + length
    if end > len(data):
        raise ProtocolError(f"truncated {field}")
    return data[offset:end]


def _read_uint(data: bytes, offset: int, fmt: struct.Struct, field: str) -> tuple[int, int]:
    end = offset + fmt.size
    if end > len(data):
        raise ProtocolError(f"truncated {field}")
    (value,) = fmt.unpack_from(data, offset)
    return value, end


def _read_text(
    data: bytes, offset: int, length_fmt: struct.Struct, field: str, max_bytes: int
) -> tuple[str, int]:
    length, offset = _read_uint(data, offset, length_fmt, f"{field} length")
    if length > max_bytes:
        raise ProtocolError(f"{field} exceeds the allowed byte length")
    raw = _read_bytes(data, offset, length, field)
    offset += length
    try:
        return raw.decode("utf-8"), offset
    except UnicodeDecodeError as exc:
        raise ProtocolError(f"{field} is not valid UTF-8") from exc


def _reject_trailing(data: bytes, offset: int) -> None:
    if offset != len(data):
        raise ProtocolError("trailing bytes after business payload")
