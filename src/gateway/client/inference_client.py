"""Client-side business layer.

The caller opens an ACTIVE session through the security-core line and hands it
to :class:`InferenceClient`. The client exposes the stable inference interface
without exposing keys, sequence numbers or record framing. It validates the
response identity and stream framing before returning business data.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from gateway.codec import decode_response, encode_request
from gateway.contracts import (
    InferenceChunk,
    InferenceRequest,
    InferenceResponse,
    ProtocolError,
    RecordType,
    SecureSession,
)
from gateway.framing import recv_message, send_message


class InferenceClient:
    """Implements the business interface over one authenticated session."""

    def __init__(self, session: SecureSession) -> None:
        self._session = session

    async def complete(self, request: InferenceRequest) -> InferenceResponse:
        """Send a request and return the assembled, verified response."""
        await send_message(
            self._session,
            RecordType.REQUEST,
            encode_request(request),
            request_id=request.request_id,
        )
        payload = await recv_message(
            self._session, request.request_id, record_type=RecordType.RESPONSE
        )
        response = decode_response(payload)
        if response.request_id != request.request_id:
            raise ProtocolError("response request id does not match the request")
        return response

    def stream(self, request: InferenceRequest) -> AsyncIterator[InferenceChunk]:
        """Request streaming output and yield authenticated chunks in order."""
        return self._stream(request)

    async def _stream(self, request: InferenceRequest) -> AsyncIterator[InferenceChunk]:
        await send_message(
            self._session,
            RecordType.REQUEST,
            encode_request(request, stream=True),
            request_id=request.request_id,
        )
        expected_index = 0
        while True:
            record = await self._session.recv()
            if record.header.request_id != request.request_id:
                raise ProtocolError("chunk carries an unexpected request id")
            if record.header.record_type != RecordType.RESPONSE:
                raise ProtocolError("unexpected record type while streaming")
            if record.header.chunk_index != expected_index:
                raise ProtocolError("stream chunk index is out of order")
            try:
                output = record.plaintext.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ProtocolError("chunk payload is not valid UTF-8") from exc
            chunk = InferenceChunk(
                request_id=request.request_id,
                index=record.header.chunk_index,
                output=output,
                is_final=record.header.end_of_message,
            )
            expected_index += 1
            yield chunk
            if chunk.is_final:
                return
