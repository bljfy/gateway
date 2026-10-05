"""Deterministic mock inference and the simulator's inbound session loop.

The simulator runs the same computation for the same request so tests and
benchmarks are reproducible. It performs no cryptography; callers must only pass
authenticated plaintext, and the inbound loop replies through an authenticated
session.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from gateway.codec import decode_request, encode_response
from gateway.contracts import (
    MAX_RECORD_PLAINTEXT,
    InferenceChunk,
    InferenceRequest,
    InferenceResponse,
    ProtocolError,
    RecordType,
    SecureSession,
    SessionClosedError,
)
from gateway.framing import recv_message, send_message

STREAM_CHUNK_CHARS = 256


class InferenceSimulator:
    """Implements the deterministic inference business interface."""

    def __init__(self, *, stream_chunk_chars: int = STREAM_CHUNK_CHARS) -> None:
        if stream_chunk_chars < 1:
            raise ValueError("stream_chunk_chars must be positive")
        if stream_chunk_chars > MAX_RECORD_PLAINTEXT // 4:
            raise ValueError("stream_chunk_chars is too large for a single record")
        self._stream_chunk_chars = stream_chunk_chars

    async def complete(self, request: InferenceRequest) -> InferenceResponse:
        """Return a deterministic response for the request."""
        return InferenceResponse(request_id=request.request_id, output=self._output(request))

    def stream(self, request: InferenceRequest) -> AsyncIterator[InferenceChunk]:
        """Yield the deterministic output as an ordered stream of chunks."""
        return self._stream(request)

    async def serve(self, session: SecureSession) -> None:
        """Run the inbound business loop over one ACTIVE session until closure.

        Heartbeat records are skipped; an authenticated close (``SessionClosedError``)
        or a CLOSE record ends the loop. Cancellation is reconciled once the
        security-core line exposes stream cancellation to the business layer.
        """
        while True:
            try:
                record = await session.recv()
                if record.header.record_type is RecordType.CLOSE:
                    await session.close()
                    return
                if record.header.record_type is RecordType.HEARTBEAT:
                    continue
                if record.header.record_type is not RecordType.REQUEST:
                    raise ProtocolError("simulator received an unexpected record type")
                payload = await recv_message(
                    session,
                    record.header.request_id,
                    record_type=RecordType.REQUEST,
                    first=record,
                )
                request, stream = decode_request(payload)
                if stream:
                    async for chunk in self.stream(request):
                        await session.send(
                            RecordType.RESPONSE,
                            chunk.output.encode("utf-8"),
                            request_id=request.request_id,
                            chunk_index=chunk.index,
                            end_of_message=chunk.is_final,
                        )
                else:
                    response = await self.complete(request)
                    await send_message(
                        session,
                        RecordType.RESPONSE,
                        encode_response(response),
                        request_id=request.request_id,
                    )
            except SessionClosedError:
                return

    async def _stream(self, request: InferenceRequest) -> AsyncIterator[InferenceChunk]:
        output = self._output(request)
        if not output:
            yield InferenceChunk(request.request_id, 0, "", is_final=True)
            return
        chunks = [
            output[start : start + self._stream_chunk_chars]
            for start in range(0, len(output), self._stream_chunk_chars)
        ]
        last_index = len(chunks) - 1
        for index, part in enumerate(chunks):
            yield InferenceChunk(request.request_id, index, part, is_final=index == last_index)

    @staticmethod
    def _output(request: InferenceRequest) -> str:
        context = " | ".join(request.retrieval_context)
        body = f"[mock:{request.model}] {request.prompt}"
        if context:
            body = f"{body} (context: {context})"
        return body[: request.max_output_tokens]
