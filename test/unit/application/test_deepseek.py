"""Real SDK over controlled HTTP transport: mapping, errors and stream cleanup."""

import asyncio
import json
from contextlib import aclosing
from dataclasses import replace
from uuid import uuid4

import httpx2
import pytest
from openai import AsyncOpenAI

from gateway.adapter import AdapterConfig, DeepSeekProvider, ModelProvider, create_provider
from gateway.config import Limits
from gateway.contracts import GatewayError, InferenceRequest
from test.fixtures.adapter_output import response_body


def request():
    return InferenceRequest(uuid4(), "deepseek-flash", "你好🙂", ("片段A", "片段B"), 37)


def completion(content="回答🙂", finish="stop"):
    return {
        "id": "synthetic",
        "object": "chat.completion",
        "created": 1,
        "model": "deepseek-flash",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": finish,
            }
        ],
    }


def event(content=None, finish=None, choices=None):
    return (
        "data: "
        + json.dumps(
            {
                "id": "synthetic",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "deepseek-flash",
                "choices": choices
                if choices is not None
                else [
                    {
                        "index": 0,
                        "delta": {"content": content},
                        "finish_reason": finish,
                    }
                ],
            }
        )
        + "\n\n"
    )


async def controlled_provider(handler, limits=None):
    provider = DeepSeekProvider(AdapterConfig(), limits or Limits())
    await provider._client.close()
    provider._client = AsyncOpenAI(
        api_key="synthetic-api-key",
        base_url="https://api.deepseek.com",
        max_retries=0,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
    )
    return provider


@pytest.mark.asyncio
async def test_sdk_mapping_and_real_token_budget():
    seen = []

    def handler(req):
        seen.append(json.loads(req.content))
        return httpx2.Response(200, json=completion("x" * 50))

    provider = await controlled_provider(handler)
    try:
        req = request()
        response = await provider.complete(req)
        assert response_body(response.output, req.request_id) == "x" * 50
        assert seen == [
            {
                "model": "deepseek-flash",
                "messages": [
                    {"role": "user", "content": "参考资料：\n片段A"},
                    {"role": "user", "content": "参考资料：\n片段B"},
                    {"role": "user", "content": "你好🙂"},
                ],
                "max_tokens": 37,
                "stream": False,
                "thinking": {"type": "disabled"},
            }
        ]
    finally:
        await provider.close()
    assert provider._client.is_closed()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 429, 500])
async def test_api_errors_are_sanitized_and_not_retried(status):
    calls = []

    def handler(req):
        calls.append(req)
        return httpx2.Response(status, json={"error": {"message": "SYNTHETIC_SECRET"}})

    provider = await controlled_provider(handler)
    try:
        with pytest.raises(GatewayError, match="^model request failed$") as caught:
            await provider.complete(request())
        assert "SYNTHETIC_SECRET" not in str(caught.value)
        assert len(calls) == 1
    finally:
        await provider.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [completion(None), completion(finish="content_filter"), {}])
async def test_invalid_response_rejected(body):
    provider = await controlled_provider(lambda _: httpx2.Response(200, json=body))
    try:
        with pytest.raises(GatewayError):
            await provider.complete(request())
    finally:
        await provider.close()


class BytesStream(httpx2.AsyncByteStream):
    def __init__(self, parts, *, block=False):
        self.parts = parts
        self.closed = False
        self.block = block
        self.entered = asyncio.Event()

    async def __aiter__(self):
        for part in self.parts:
            yield part.encode()
        if self.block:
            self.entered.set()
            await asyncio.Event().wait()

    async def aclose(self):
        self.closed = True


@pytest.mark.asyncio
async def test_incremental_stream_unicode_empty_events_and_final():
    body = BytesStream(
        [event(), event("你"), event(choices=[]), event("好🙂", "stop"), "data: [DONE]\n\n"]
    )
    provider = await controlled_provider(
        lambda _: httpx2.Response(200, headers={"Content-Type": "text/event-stream"}, stream=body)
    )
    try:
        req = request()
        chunks = [chunk async for chunk in provider.stream(req)]
        assert response_body("".join(c.output for c in chunks), req.request_id) == "你好🙂"
        assert [c.index for c in chunks] == list(range(len(chunks)))
        assert sum(c.is_final for c in chunks) == 1 and chunks[-1].is_final
        assert all(c.output or c.is_final for c in chunks)
        assert body.closed
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_truncated_stream_has_no_success_final():
    body = BytesStream([event("partial")])
    provider = await controlled_provider(lambda _: httpx2.Response(200, stream=body))
    chunks = []
    try:
        with pytest.raises(GatewayError):
            async for chunk in provider.stream(request()):
                chunks.append(chunk)
        assert chunks and not any(c.is_final for c in chunks)
        assert body.closed
    finally:
        await provider.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_response_budget_includes_metadata(stream):
    limits = replace(Limits(), max_record_plaintext_bytes=32, max_response_body_bytes=32)
    body = BytesStream([event("answer", "stop"), "data: [DONE]\n\n"])
    provider = await controlled_provider(
        lambda _: (
            httpx2.Response(200, stream=body) if stream else httpx2.Response(200, json=completion())
        ),
        limits,
    )
    try:
        with pytest.raises(GatewayError):
            if stream:
                async with aclosing(provider.stream(request())) as iterator:
                    await anext(iterator)
            else:
                await provider.complete(request())
        if stream:
            assert body.closed
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_early_stream_close_releases_http_response():
    body = BytesStream([event("partial")], block=True)
    provider = await controlled_provider(lambda _: httpx2.Response(200, stream=body))
    try:
        iterator = provider.stream(request())
        assert not (await anext(iterator)).is_final
        await iterator.aclose()
        assert body.closed
    finally:
        await provider.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_timeout_or_cancel_releases_stream(cancel):
    body = BytesStream([], block=True)
    provider = await controlled_provider(
        lambda _: httpx2.Response(200, stream=body), replace(Limits(), request_timeout_seconds=1)
    )
    try:

        async def consume():
            async with aclosing(provider.stream(request())) as iterator:
                return [chunk async for chunk in iterator]

        task = asyncio.create_task(consume())
        await asyncio.wait_for(body.entered.wait(), 2)
        if cancel:
            task.cancel()
        with pytest.raises(asyncio.CancelledError if cancel else TimeoutError):
            await task
        assert body.closed
    finally:
        await provider.close()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"provider": "unknown"},
        {"base_url": "http://remote.example"},
        {"base_url": "https://key:secret@api.deepseek.com"},
        {"api_key_env": "bad name"},
    ],
)
def test_invalid_provider_config(kwargs):
    with pytest.raises(GatewayError, match="^invalid adapter configuration$"):
        AdapterConfig(**kwargs)


def test_credentials_not_needed_until_provider_creation(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY")
    config = AdapterConfig()
    with pytest.raises(GatewayError, match="credential missing"):
        create_provider(config, Limits())


@pytest.mark.asyncio
async def test_factory_provider_contract():
    provider = create_provider(AdapterConfig(), Limits())
    assert isinstance(provider, ModelProvider)
    await provider.close()


@pytest.mark.asyncio
async def test_stream_deadline_does_not_cancel_consumer_between_reads():
    body = BytesStream([event("answer", "stop"), "data: [DONE]\n\n"])
    provider = await controlled_provider(
        lambda _: httpx2.Response(200, stream=body),
        replace(Limits(), request_timeout_seconds=1),
    )
    try:
        async with aclosing(provider.stream(request())) as iterator:
            await anext(iterator)
            await asyncio.sleep(1.1)
            with pytest.raises(TimeoutError):
                await anext(iterator)
        assert body.closed
    finally:
        await provider.close()
