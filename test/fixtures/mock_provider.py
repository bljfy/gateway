"""Mock inference with response metadata and the adapter's inbound session loop.

The business body is deterministic; response metadata records generation time
and the request UUID. It performs no cryptography; callers must only pass
authenticated plaintext, and the inbound loop replies through an authenticated
session.
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from datetime import UTC, datetime

from gateway.adapter import AdapterServer
from gateway.config import Limits
from gateway.contracts import (
    MAX_RECORD_PLAINTEXT,
    InferenceChunk,
    InferenceRequest,
    InferenceResponse,
)

STREAM_CHUNK_CHARS = 256


class MockProvider(AdapterServer):
    """Implements mock inference with a timestamp and request identity."""

    def __init__(
        self, *, stream_chunk_chars: int = STREAM_CHUNK_CHARS, limits: Limits | None = None
    ) -> None:
        if stream_chunk_chars < 1:
            raise ValueError("stream_chunk_chars must be positive")
        if stream_chunk_chars > MAX_RECORD_PLAINTEXT // 4:
            raise ValueError("stream_chunk_chars is too large for a single record")
        self._stream_chunk_chars = stream_chunk_chars
        super().__init__(self, limits=limits)

    async def complete(self, request: InferenceRequest) -> InferenceResponse:
        """Return metadata followed by the deterministic business body."""
        return InferenceResponse(request_id=request.request_id, output=self._output(request))

    def stream(self, request: InferenceRequest) -> AsyncGenerator[InferenceChunk, None]:
        """Yield the deterministic output as an ordered stream of chunks."""
        return self._stream(request)

    async def close(self) -> None:
        pass

    async def _stream(self, request: InferenceRequest) -> AsyncGenerator[InferenceChunk, None]:
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
