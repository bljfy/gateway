"""Boundary and lifecycle tests for shared contracts, without a fake crypto implementation."""

from dataclasses import FrozenInstanceError
from uuid import UUID, uuid4

import pytest

from gateway.contracts import (
    MAX_RECORD_PLAINTEXT,
    Direction,
    InferenceChunk,
    InferenceRequest,
    InferenceResponse,
    PeerIdentity,
    PeerRole,
    ProtocolError,
    RecordHeader,
    RecordType,
    SecureSession,
    SessionPolicy,
    SessionState,
    VerifiedRecord,
)


def make_header(sequence: int = 0) -> RecordHeader:
    return RecordHeader(
        record_type=RecordType.RESPONSE,
        session_id=b"s" * 16,
        direction=Direction.ACCEPTOR_TO_INITIATOR,
        sequence=sequence,
        request_id=uuid4(),
    )


@pytest.mark.parametrize("sequence", [-1, 2**64])
def test_sequence_outside_wire_range_is_rejected(sequence: int) -> None:
    with pytest.raises(ValueError, match="sequence"):
        make_header(sequence)


@pytest.mark.parametrize("sequence", [0, 2**64 - 1])
def test_sequence_wire_boundaries_are_accepted(sequence: int) -> None:
    assert make_header(sequence).sequence == sequence


def test_peer_limit_is_utf8_bytes_not_characters() -> None:
    with pytest.raises(ValueError, match="UTF-8 bytes"):
        PeerIdentity("网" * 22, PeerRole.GATEWAY)
    assert PeerIdentity("a" * 64, PeerRole.GATEWAY).peer_id == "a" * 64


def test_bad_session_id_is_rejected() -> None:
    with pytest.raises(ValueError, match="session_id"):
        RecordHeader(RecordType.REQUEST, b"short", Direction.INITIATOR_TO_ACCEPTOR, 0, uuid4())


@pytest.mark.parametrize("chunk_index", [-1, 2**32])
def test_chunk_index_outside_wire_range_is_rejected(chunk_index: int) -> None:
    with pytest.raises(ValueError, match="chunk_index"):
        RecordHeader(
            RecordType.REQUEST, b"s" * 16, Direction.INITIATOR_TO_ACCEPTOR, 0, uuid4(), chunk_index
        )
    with pytest.raises(ValueError, match="index"):
        InferenceChunk(uuid4(), chunk_index, "synthetic-output")


def test_unsupported_version_is_rejected() -> None:
    with pytest.raises(ValueError, match="version"):
        RecordHeader(
            RecordType.REQUEST,
            b"s" * 16,
            Direction.INITIATOR_TO_ACCEPTOR,
            0,
            uuid4(),
            protocol_version=2,
        )


def test_plaintext_record_limit_and_private_repr() -> None:
    record = VerifiedRecord(make_header(), b"secret-marker")
    assert "secret-marker" not in repr(record)
    assert len(VerifiedRecord(make_header(), b"x" * MAX_RECORD_PLAINTEXT).plaintext) == 65_536
    with pytest.raises(ValueError, match="limit"):
        VerifiedRecord(make_header(), b"x" * (MAX_RECORD_PLAINTEXT + 1))


def test_business_payload_is_not_in_repr_and_request_is_frozen() -> None:
    request = InferenceRequest(uuid4(), "mock-model", "private-prompt", ("private-context",))
    response = InferenceResponse(request.request_id, "private-output")
    chunk = InferenceChunk(request.request_id, 0, "private-output")
    assert "private-prompt" not in repr(request)
    assert "private-context" not in repr(request)
    assert "private-output" not in repr(response)
    assert "private-output" not in repr(chunk)
    with pytest.raises(FrozenInstanceError):
        request.model = "changed"


@pytest.mark.parametrize("margin", [0, -1, 1800, 1801])
def test_rotation_margin_must_precede_hard_expiry(margin: int) -> None:
    with pytest.raises(ValueError, match="rotation margin"):
        SessionPolicy(rotate_before_seconds=margin)


def test_sequence_budget_cannot_overflow() -> None:
    with pytest.raises(ValueError, match="sequence space"):
        SessionPolicy(max_records_per_direction=2**64 + 1)


@pytest.mark.parametrize("budget", [0, -1])
def test_sequence_budget_must_be_positive(budget: int) -> None:
    with pytest.raises(ValueError, match="positive"):
        SessionPolicy(max_records_per_direction=budget)


@pytest.mark.parametrize("tokens", [0, -1])
def test_output_token_budget_must_be_positive(tokens: int) -> None:
    with pytest.raises(ValueError, match="positive"):
        InferenceRequest(uuid4(), "mock-model", "synthetic-prompt", max_output_tokens=tokens)


class TestSession:
    """Test-only transport substitute: exercises async contracts, never authenticates a peer."""

    __test__ = False

    def __init__(self) -> None:
        self._peer = PeerIdentity("simulator", PeerRole.SIMULATOR)
        self._state = SessionState.NEW
        self._record: VerifiedRecord | None = None

    @property
    def peer(self) -> PeerIdentity:
        return self._peer

    @property
    def state(self) -> SessionState:
        return self._state

    async def handshake(self) -> None:
        self._state = SessionState.ACTIVE

    async def send(
        self,
        record_type: RecordType,
        plaintext: bytes,
        *,
        request_id: UUID,
        chunk_index: int = 0,
        end_of_message: bool = True,
    ) -> None:
        if self.state is not SessionState.ACTIVE:
            raise ProtocolError("session is not active")
        self._record = VerifiedRecord(
            RecordHeader(
                record_type,
                b"s" * 16,
                Direction.INITIATOR_TO_ACCEPTOR,
                0,
                request_id,
                chunk_index,
                end_of_message,
            ),
            plaintext,
        )

    async def recv(self) -> VerifiedRecord:
        if self.state is not SessionState.ACTIVE or self._record is None:
            raise ProtocolError("no active record")
        return self._record

    async def close(self) -> None:
        self._record = None
        self._state = SessionState.CLOSED


@pytest.mark.asyncio
async def test_async_session_consumer_uses_the_contract() -> None:
    session: SecureSession = TestSession()
    assert isinstance(session, SecureSession)
    request_id = uuid4()
    with pytest.raises(ProtocolError, match="active"):
        await session.send(RecordType.REQUEST, b"synthetic", request_id=request_id)
    await session.handshake()
    await session.send(RecordType.REQUEST, b"synthetic", request_id=request_id)
    record = await session.recv()
    assert record.header.request_id == request_id
    assert record.plaintext == b"synthetic"
    await session.close()
    await session.close()
    assert session.state is SessionState.CLOSED
    with pytest.raises(ProtocolError, match="active"):
        await session.recv()
