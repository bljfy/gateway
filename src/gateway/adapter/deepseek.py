"""DeepSeek Chat Completions provider with bounded, cancellable output."""

import asyncio
import json
from collections.abc import AsyncGenerator
from datetime import UTC, datetime

from openai import AsyncOpenAI
from openai.types.chat import ChatCompletionMessageParam

from gateway.adapter.provider import AdapterConfig
from gateway.codec import encode_response
from gateway.config import Limits
from gateway.contracts import GatewayError, InferenceChunk, InferenceRequest, InferenceResponse


def response_metadata(request: InferenceRequest) -> str:
    return (
        json.dumps(
            {
                "timestamp": datetime.now(UTC).isoformat(timespec="milliseconds"),
                "request_id": str(request.request_id),
            },
            separators=(",", ":"),
        )
        + "\n"
    )


class DeepSeekProvider:
    def __init__(self, config: AdapterConfig, limits: Limits) -> None:
        self._limits = limits
        key = config.api_key()
        try:
            self._client = AsyncOpenAI(
                api_key=key,
                base_url=config.base_url,
                timeout=float(limits.request_timeout_seconds),
                max_retries=0,
            )
        except Exception:
            raise GatewayError("model client initialization failed") from None

    @staticmethod
    def _messages(request: InferenceRequest) -> list[ChatCompletionMessageParam]:
        messages: list[ChatCompletionMessageParam] = [
            {"role": "user", "content": "参考资料：\n" + context}
            for context in request.retrieval_context
        ]
        messages.append({"role": "user", "content": request.prompt})
        return messages

    async def complete(self, request: InferenceRequest) -> InferenceResponse:
        try:
            async with asyncio.timeout(self._limits.request_timeout_seconds):
                result = await self._client.chat.completions.create(
                    model=request.model,
                    messages=self._messages(request),
                    max_tokens=request.max_output_tokens,
                    stream=False,
                    extra_body={"thinking": {"type": "disabled"}},
                )
                if len(result.choices) != 1:
                    raise GatewayError("invalid model response")
                choice = result.choices[0]
                if choice.finish_reason not in {"stop", "length"}:
                    raise GatewayError("model response incomplete")
                if not isinstance(choice.message.content, str):
                    raise GatewayError("invalid model response")
                response = InferenceResponse(
                    request.request_id, response_metadata(request) + choice.message.content
                )
                if len(encode_response(response)) > self._limits.max_response_body_bytes:
                    raise GatewayError("model response exceeds limit")
                return response
        except TimeoutError:
            raise TimeoutError("model request timed out") from None
        except Exception:
            raise GatewayError("model request failed") from None

    async def stream(self, request: InferenceRequest) -> AsyncGenerator[InferenceChunk, None]:
        deadline = asyncio.get_running_loop().time() + self._limits.request_timeout_seconds
        try:
            async with asyncio.timeout_at(deadline):
                stream = await self._client.chat.completions.create(
                    model=request.model,
                    messages=self._messages(request),
                    max_tokens=request.max_output_tokens,
                    stream=True,
                    extra_body={"thinking": {"type": "disabled"}},
                )
            async with stream:
                index = 0
                metadata = response_metadata(request)
                size = len(metadata.encode("utf-8"))
                if size > self._limits.max_response_body_bytes:
                    raise GatewayError("model response exceeds limit")
                yield InferenceChunk(request.request_id, index, metadata)
                index += 1
                finished = False
                iterator = stream.__aiter__()
                while True:
                    if asyncio.get_running_loop().time() >= deadline:
                        raise TimeoutError("model request timed out")
                    try:
                        async with asyncio.timeout_at(deadline):
                            event = await anext(iterator)
                    except StopAsyncIteration:
                        break
                    if not event.choices:
                        continue
                    if len(event.choices) != 1 or finished:
                        raise GatewayError("invalid model stream")
                    choice = event.choices[0]
                    content = choice.delta.content
                    if content:
                        size += len(content.encode("utf-8"))
                        if size > self._limits.max_response_body_bytes:
                            raise GatewayError("model response exceeds limit")
                        yield InferenceChunk(request.request_id, index, content)
                        index += 1
                    if choice.finish_reason is not None:
                        if choice.finish_reason not in {"stop", "length"}:
                            raise GatewayError("model response incomplete")
                        finished = True
                if not finished:
                    raise GatewayError("model stream interrupted")
                yield InferenceChunk(request.request_id, index, "", is_final=True)
        except TimeoutError:
            raise TimeoutError("model request timed out") from None
        except Exception:
            raise GatewayError("model request failed") from None

    async def close(self) -> None:
        await self._client.close()
