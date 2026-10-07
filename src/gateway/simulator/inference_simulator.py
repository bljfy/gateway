"""Mock inference with response metadata and the simulator's inbound session loop.

The business body is deterministic; response metadata records generation time
and the request UUID. It performs no cryptography; callers must only pass
authenticated plaintext, and the inbound loop replies through an authenticated
session.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime

from gateway.codec import decode_request, encode_response
from gateway.config import Limits
from gateway.contracts import (
    MAX_RECORD_PLAINTEXT,
    InferenceChunk,
    InferenceRequest,
    InferenceResponse,
    ProtocolError,
    RecordType,
    SecureSession,
    SessionClosedError,
    VerifiedRecord,
)
from gateway.framing import recv_business_record, recv_message, send_message, split_payload

STREAM_CHUNK_CHARS = 256


class InferenceSimulator:
    """Implements mock inference with a timestamp and request identity."""

    def __init__(
        self, *, stream_chunk_chars: int = STREAM_CHUNK_CHARS, limits: Limits | None = None
    ) -> None:
        if stream_chunk_chars < 1:
            raise ValueError("stream_chunk_chars must be positive")
        if stream_chunk_chars > MAX_RECORD_PLAINTEXT // 4:
            raise ValueError("stream_chunk_chars is too large for a single record")
        self._stream_chunk_chars = stream_chunk_chars
        self._limits = limits if limits is not None else Limits()

    async def complete(self, request: InferenceRequest) -> InferenceResponse:
        """Return metadata followed by the deterministic business body."""
        return InferenceResponse(request_id=request.request_id, output=self._output(request))

    def stream(self, request: InferenceRequest) -> AsyncIterator[InferenceChunk]:
        """Yield the deterministic output as an ordered stream of chunks."""
        return self._stream(request)

    async def serve(self, session: SecureSession) -> None:
        """Monitor authenticated closure during inference; own and drain all child tasks."""
        receiving: asyncio.Task[VerifiedRecord] | None = None
        responding: asyncio.Task[None] | None = None
        try:
            while True:
                if receiving is None:
                    receiving = asyncio.create_task(recv_business_record(session))
                record = await receiving
                receiving = None
                if record.header.record_type is RecordType.CLOSE:
                    await session.close()
                    return
                if record.header.record_type is not RecordType.REQUEST:
                    raise ProtocolError("simulator received an unexpected record type")
                deadline = asyncio.get_running_loop().time() + self._limits.request_timeout_seconds
                async with asyncio.timeout_at(deadline):
                    payload = await recv_message(
                        session,
                        record.header.request_id,
                        record_type=RecordType.REQUEST,
                        first=record,
                        max_bytes=self._limits.max_request_body_bytes,
                        record_bytes=self._limits.max_record_plaintext_bytes,
                    )
                request, stream = decode_request(payload)
                if request.request_id != record.header.request_id:
                    raise ProtocolError("request payload and record identifiers differ")
                responding = asyncio.create_task(self._respond(session, request, stream, deadline))
                receiving = asyncio.create_task(recv_business_record(session))
                done, _ = await asyncio.wait(
                    {responding, receiving}, return_when=asyncio.FIRST_COMPLETED
                )
                if responding in done:
                    responding.result()
                    responding = None
                    continue
                control = receiving.result()
                receiving = None
                if (
                    control.header.record_type is RecordType.CANCEL
                    and control.header.request_id == request.request_id
                    and not control.plaintext
                    and control.header.chunk_index == 0
                    and control.header.end_of_message
                ):
                    return
                if control.header.record_type is RecordType.CLOSE:
                    return
                raise ProtocolError("unexpected record during inference")
        except SessionClosedError:
            return
        finally:
            owned = [task for task in (receiving, responding) if task is not None]
            for task in owned:
                task.cancel()
            await asyncio.gather(*owned, return_exceptions=True)

    async def _respond(
        self, session: SecureSession, request: InferenceRequest, stream: bool, deadline: float
    ) -> None:
        async with asyncio.timeout_at(deadline):
            await self._respond_message(session, request, stream)

    async def _respond_message(
        self, session: SecureSession, request: InferenceRequest, stream: bool
    ) -> None:
        if stream:
            index, size = 0, 0
            async for chunk in self.stream(request):
                encoded = chunk.output.encode("utf-8")
                size += len(encoded)
                if size > self._limits.max_response_body_bytes:
                    raise ProtocolError("response exceeds configured limit")
                parts = split_payload(encoded, self._limits.max_record_plaintext_bytes)
                for offset, part in enumerate(parts):
                    await session.send(
                        RecordType.RESPONSE,
                        part,
                        request_id=request.request_id,
                        chunk_index=index,
                        end_of_message=chunk.is_final and offset == len(parts) - 1,
                    )
                    index += 1
        else:
            response = await self.complete(request)
            payload = encode_response(response)
            if len(payload) > self._limits.max_response_body_bytes:
                raise ProtocolError("response exceeds configured limit")
            await send_message(
                session,
                RecordType.RESPONSE,
                payload,
                request_id=request.request_id,
                record_bytes=self._limits.max_record_plaintext_bytes,
            )

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
        metadata = json.dumps(
            {
                "timestamp": datetime.now(UTC).isoformat(timespec="milliseconds"),
                "request_id": str(request.request_id),
            },
            separators=(",", ":"),
        )
        return metadata + "\n" + body[: request.max_output_tokens]
