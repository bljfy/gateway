"""Explicitly authorized DeepSeek smoke test; never part of offline regression."""

from contextlib import aclosing
from uuid import uuid4

import pytest

from gateway.adapter import AdapterConfig, create_provider
from gateway.config import Limits
from gateway.contracts import InferenceRequest
from test.fixtures.adapter_output import response_body


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_deepseek_complete_and_stream():
    provider = create_provider(AdapterConfig(), Limits())
    try:
        request = InferenceRequest(
            uuid4(), "deepseek-flash", "请回复：连接成功", max_output_tokens=32
        )
        response = await provider.complete(request)
        assert response_body(response.output, request.request_id)
        async with aclosing(provider.stream(request)) as iterator:
            chunks = [chunk async for chunk in iterator]
        assert chunks[-1].is_final and sum(c.is_final for c in chunks) == 1
        assert response_body("".join(c.output for c in chunks), request.request_id)
    finally:
        await provider.close()
