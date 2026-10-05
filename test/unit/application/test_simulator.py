"""Deterministic mock inference and its chunked streaming behaviour."""

from uuid import uuid4

import pytest

from gateway.contracts import MAX_RECORD_PLAINTEXT, InferenceRequest, InferenceService
from gateway.simulator import InferenceSimulator

REQUEST_ID = uuid4()


def make_request(**overrides: object) -> InferenceRequest:
    fields: dict[str, object] = {
        "request_id": REQUEST_ID,
        "model": "mock-model",
        "prompt": "hello",
        "retrieval_context": ("context-a",),
        "max_output_tokens": 512,
    }
    fields.update(overrides)
    return InferenceRequest(**fields)  # type: ignore[arg-type]


def test_simulator_conforms_to_inference_service() -> None:
    assert isinstance(InferenceSimulator(), InferenceService)


@pytest.mark.asyncio
async def test_complete_is_deterministic() -> None:
    simulator = InferenceSimulator()
    request = make_request()
    first = await simulator.complete(request)
    second = await simulator.complete(request)
    assert first == second
    assert first.request_id == request.request_id


@pytest.mark.asyncio
async def test_complete_includes_prompt_model_and_context() -> None:
    response = await InferenceSimulator().complete(make_request())
    assert response.output == "[mock:mock-model] hello (context: context-a)"


@pytest.mark.asyncio
async def test_output_is_capped_by_token_budget() -> None:
    request = make_request(prompt="x" * 1000, max_output_tokens=32)
    response = await InferenceSimulator().complete(request)
    assert len(response.output) == 32


@pytest.mark.asyncio
async def test_stream_reassembles_to_complete_output() -> None:
    simulator = InferenceSimulator()
    request = make_request(prompt="streamed prompt")
    complete = await simulator.complete(request)
    chunks = [chunk async for chunk in simulator.stream(request)]
    assert "".join(chunk.output for chunk in chunks) == complete.output
    assert [chunk.index for chunk in chunks] == list(range(len(chunks)))
    assert [chunk.is_final for chunk in chunks][-1] is True
    assert sum(1 for chunk in chunks if chunk.is_final) == 1


@pytest.mark.asyncio
async def test_stream_respects_chunk_size() -> None:
    simulator = InferenceSimulator(stream_chunk_chars=8)
    request = make_request(prompt="a" * 50, max_output_tokens=100)
    chunks = [chunk async for chunk in simulator.stream(request)]
    assert all(len(chunk.output) <= 8 for chunk in chunks)
    assert chunks[-1].is_final is True


@pytest.mark.asyncio
async def test_stream_empty_output_yields_single_final_chunk() -> None:
    simulator = InferenceSimulator()
    request = make_request(max_output_tokens=1)
    chunks = [chunk async for chunk in simulator.stream(request)]
    assert len(chunks) == 1
    assert chunks[0].is_final is True


def test_constructor_rejects_nonpositive_chunk_size() -> None:
    with pytest.raises(ValueError, match="stream_chunk_chars"):
        InferenceSimulator(stream_chunk_chars=0)


def test_constructor_rejects_chunk_size_beyond_record_limit() -> None:
    with pytest.raises(ValueError, match="stream_chunk_chars"):
        InferenceSimulator(stream_chunk_chars=MAX_RECORD_PLAINTEXT)
