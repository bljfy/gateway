"""Client-side business layer.

The caller opens an ACTIVE session through the security-core line and hands it
to :class:`InferenceClient`. The client exposes the stable inference interface
without exposing keys, sequence numbers or record framing. It validates the
response identity and stream framing before returning business data.
"""

from __future__ import annotations

import asyncio
import codecs
from collections.abc import AsyncIterator

from gateway.codec import MAX_RESPONSE_BODY_BYTES, decode_response, encode_request
from gateway.config import Limits
from gateway.contracts import (
    InferenceChunk,
    InferenceRequest,
    InferenceResponse,
    ProtocolError,
    RecordType,
    SecureSession,
)
from gateway.framing import recv_business_record, recv_message, send_message


class InferenceClient:
    """Implements the business interface over one authenticated session."""

    def __init__(self, session: SecureSession, limits: Limits | None = None) -> None:
        self._session = session
        self._limits = limits if limits is not None else Limits()
        self._call_lock = asyncio.Lock()

    async def complete(self, request: InferenceRequest) -> InferenceResponse:
        """Send a request and return the assembled, verified response."""
        async with self._call_lock:
            try:
                async with asyncio.timeout(self._limits.request_timeout_seconds):
                    return await self._complete(request)
            except BaseException:
                await self._session.close()
                raise

    async def _complete(self, request: InferenceRequest) -> InferenceResponse:
        payload = encode_request(request)
        if len(payload) > self._limits.max_request_body_bytes:
            raise ProtocolError("request exceeds configured limit")
        await send_message(
            self._session,
            RecordType.REQUEST,
            payload,
            request_id=request.request_id,
            record_bytes=self._limits.max_record_plaintext_bytes,
        )
        payload = await recv_message(
            self._session,
            request.request_id,
            record_type=RecordType.RESPONSE,
            max_bytes=self._limits.max_response_body_bytes,
            record_bytes=self._limits.max_record_plaintext_bytes,
        )
        response = decode_response(payload)
        if response.request_id != request.request_id:
            raise ProtocolError("response request id does not match the request")
        return response

    def stream(self, request: InferenceRequest) -> AsyncIterator[InferenceChunk]:
        """Request streaming output and yield authenticated chunks in order."""
        return self._stream(request)

    async def _stream(self, request: InferenceRequest) -> AsyncIterator[InferenceChunk]:
        async with self._call_lock:
            completed = False
            deadline = asyncio.get_running_loop().time() + self._limits.request_timeout_seconds
            try:
                payload = encode_request(request, stream=True)
                if len(payload) > self._limits.max_request_body_bytes:
                    raise ProtocolError("request exceeds configured limit")
                async with asyncio.timeout_at(deadline):
                    await send_message(
                        self._session,
                        RecordType.REQUEST,
                        payload,
                        request_id=request.request_id,
                        record_bytes=self._limits.max_record_plaintext_bytes,
                    )
                expected_index = 0
                size = 0
                decoder = codecs.getincrementaldecoder("utf-8")()
                while True:
                    async with asyncio.timeout_at(deadline):
                        record = await recv_business_record(self._session)
                    if record.header.request_id != request.request_id:
                        raise ProtocolError("chunk carries an unexpected request id")
                    if record.header.record_type is not RecordType.RESPONSE:
                        raise ProtocolError("unexpected record type while streaming")
                    if record.header.chunk_index != expected_index:
                        raise ProtocolError("stream chunk index is out of order")
                    if len(record.plaintext) > self._limits.max_record_plaintext_bytes:
                        raise ProtocolError("record exceeds configured plaintext limit")
                    if not record.plaintext and not record.header.end_of_message:
                        raise ProtocolError("empty intermediate stream fragment")
                    size += len(record.plaintext)
                    if size > min(MAX_RESPONSE_BODY_BYTES, self._limits.max_response_body_bytes):
                        raise ProtocolError("stream exceeds response budget")
                    try:
                        output = decoder.decode(
                            record.plaintext, final=record.header.end_of_message
                        )
                    except UnicodeDecodeError:
                        raise ProtocolError("chunk payload is not valid UTF-8") from None
                    completed = record.header.end_of_message
                    yield InferenceChunk(request.request_id, expected_index, output, completed)
                    expected_index += 1
                    if completed:
                        return
            finally:
                if not completed:
                    # Closing also cancels gateway work; no stale responses are reused.
                    await self._session.close()
