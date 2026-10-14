"""Authenticated adapter session loop, framing and cancellation."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import aclosing

from gateway.adapter.provider import ModelProvider
from gateway.codec import decode_request, encode_response
from gateway.config import Limits
from gateway.contracts import (
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


class AdapterServer:
    """Serve authenticated requests using an injected model provider."""

    def __init__(self, provider: ModelProvider, *, limits: Limits | None = None) -> None:
        self._provider = provider
        self._limits = limits if limits is not None else Limits()

    async def complete(self, request: InferenceRequest) -> InferenceResponse:
        return await self._provider.complete(request)

    def stream(self, request: InferenceRequest) -> AsyncIterator[InferenceChunk]:
        return self._provider.stream(request)

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
                    raise ProtocolError("adapter received an unexpected record type")
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
            expected = 0
            finished = False
            async with aclosing(self._provider.stream(request)) as iterator:
                async for chunk in iterator:
                    if chunk.request_id != request.request_id or chunk.index != expected:
                        raise ProtocolError("invalid provider chunk identity or sequence")
                    if not chunk.output and not chunk.is_final:
                        raise ProtocolError("empty intermediate provider chunk")
                    expected += 1
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
                    if chunk.is_final:
                        finished = True
                        break
            if not finished:
                raise ProtocolError("provider stream interrupted")
        else:
            response = await self.complete(request)
            if response.request_id != request.request_id:
                raise ProtocolError("provider response identity mismatch")
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
