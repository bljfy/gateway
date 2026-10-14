"""Supplier-neutral adapter validates provider output and closes streams."""

from collections.abc import AsyncGenerator
from uuid import uuid4

import pytest

from gateway.adapter import AdapterServer
from gateway.cli import build_parser, main
from gateway.contracts import (
    InferenceChunk,
    InferenceRequest,
    InferenceResponse,
    PeerIdentity,
    PeerRole,
    ProtocolError,
)
from test.fixtures.in_memory import InMemorySession


class Provider:
    def __init__(self, chunks=(), *, wrong_response=False):
        self.chunks = chunks
        self.wrong_response = wrong_response
        self.stream_closed = False

    async def complete(self, request):
        return InferenceResponse(uuid4() if self.wrong_response else request.request_id, "answer")

    async def stream(self, request) -> AsyncGenerator[InferenceChunk, None]:
        try:
            for chunk in self.chunks:
                yield chunk
        finally:
            self.stream_closed = True

    async def close(self):
        pass


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["identity", "sequence", "empty", "truncated"])
async def test_invalid_provider_stream_rejected_and_closed(failure):
    req = InferenceRequest(uuid4(), "model", "synthetic")
    chunk = {
        "identity": InferenceChunk(uuid4(), 0, "answer"),
        "sequence": InferenceChunk(req.request_id, 1, "answer"),
        "empty": InferenceChunk(req.request_id, 0, ""),
        "truncated": InferenceChunk(req.request_id, 0, "answer"),
    }[failure]
    provider = Provider([chunk])
    session = InMemorySession(PeerIdentity("gateway", PeerRole.GATEWAY))
    await session.handshake()
    with pytest.raises(ProtocolError):
        await AdapterServer(provider)._respond_message(session, req, True)
    assert provider.stream_closed
    assert not any(record.header.end_of_message for record in session.sent)


@pytest.mark.asyncio
async def test_wrong_provider_response_uuid_rejected():
    session = InMemorySession(PeerIdentity("gateway", PeerRole.GATEWAY))
    await session.handshake()
    with pytest.raises(ProtocolError, match="identity mismatch"):
        await AdapterServer(Provider(wrong_response=True))._respond_message(
            session, InferenceRequest(uuid4(), "model", "synthetic"), False
        )
    assert not session.sent


@pytest.mark.parametrize("command", ["simulator", "simulate"])
def test_removed_commands_are_rejected(command):
    with pytest.raises(SystemExit):
        build_parser().parse_args([command])


def test_infer_missing_credential_has_fixed_cli_error(monkeypatch, capsys):
    monkeypatch.delenv("DEEPSEEK_API_KEY")
    assert main(["infer", "--prompt", "SYNTHETIC_SECRET"]) == 1
    result = capsys.readouterr()
    assert not result.out
    assert result.err == "command failed\n"
