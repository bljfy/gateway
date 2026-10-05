"""Versioned boundaries between security, gateway and application workstreams.

These protocols describe implementations; they do not authenticate, encrypt or
run a server. Test substitutes must remain inside the test package.
"""

from asyncio import StreamReader, StreamWriter
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import IntEnum, StrEnum
from typing import Protocol, runtime_checkable
from uuid import UUID

CONTRACT_VERSION = "1.0"
MAX_RECORD_PLAINTEXT = 65_536


class GatewayError(Exception):
    """Base error. Messages must not contain keys or business payloads."""


class AuthenticationError(GatewayError):
    """The peer identity or record authentication could not be verified."""


class SessionExpiredError(GatewayError):
    """The session can no longer protect business records."""


class ProtocolError(GatewayError):
    """The peer record violates the agreed protocol."""


class SessionClosedError(ProtocolError):
    """Authenticated CLOSE/CLOSE_ACK completed; not a protocol rejection."""


class CapacityError(GatewayError):
    """A bounded resource cannot accept more work."""


class SessionState(StrEnum):
    NEW = "new"
    HANDSHAKING = "handshaking"
    ACTIVE = "active"
    DRAINING = "draining"
    CLOSED = "closed"
    FAILED = "failed"


class PeerRole(StrEnum):
    CLIENT = "client"
    GATEWAY = "gateway"
    SIMULATOR = "simulator"


class Direction(IntEnum):
    INITIATOR_TO_ACCEPTOR = 0
    ACCEPTOR_TO_INITIATOR = 1


class RecordType(IntEnum):
    READY = 0
    REQUEST = 1
    RESPONSE = 2
    HEARTBEAT = 3
    CANCEL = 4
    CLOSE = 5
    CLOSE_ACK = 6
    ERROR = 7


@dataclass(frozen=True, slots=True)
class PeerIdentity:
    """Trusted peer reference; keys are resolved from local trust configuration."""

    peer_id: str
    role: PeerRole
    key_version: int = 1

    def __post_init__(self) -> None:
        if not self.peer_id or len(self.peer_id.encode("utf-8")) > 64:
            raise ValueError("peer_id must contain 1 to 64 UTF-8 bytes")
        if self.key_version < 1:
            raise ValueError("key_version must be positive")


@dataclass(frozen=True, slots=True)
class RecordHeader:
    """Semantic header. A owns canonical wire encoding in protocol/."""

    record_type: RecordType
    session_id: bytes
    direction: Direction
    sequence: int
    request_id: UUID
    chunk_index: int = 0
    end_of_message: bool = True
    protocol_version: int = 1

    def __post_init__(self) -> None:
        if len(self.session_id) != 16:
            raise ValueError("session_id must contain 16 bytes")
        if not 0 <= self.sequence < 2**64:
            raise ValueError("sequence must fit an unsigned 64-bit integer")
        if not 0 <= self.chunk_index < 2**32:
            raise ValueError("chunk_index must fit an unsigned 32-bit integer")
        if self.protocol_version != 1:
            raise ValueError("unsupported protocol version")


@dataclass(frozen=True, slots=True)
class VerifiedRecord:
    """Only recv() may deliver plaintext after authentication succeeds."""

    header: RecordHeader
    plaintext: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if len(self.plaintext) > MAX_RECORD_PLAINTEXT:
            raise ValueError("record plaintext exceeds the per-record limit")


@dataclass(frozen=True, slots=True)
class InferenceRequest:
    request_id: UUID
    model: str
    prompt: str = field(repr=False)
    retrieval_context: tuple[str, ...] = field(default=(), repr=False)
    max_output_tokens: int = 512

    def __post_init__(self) -> None:
        if not self.model:
            raise ValueError("model must not be empty")
        if self.max_output_tokens < 1:
            raise ValueError("max_output_tokens must be positive")


@dataclass(frozen=True, slots=True)
class InferenceResponse:
    request_id: UUID
    output: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class InferenceChunk:
    request_id: UUID
    index: int
    output: str = field(repr=False)
    is_final: bool = False

    def __post_init__(self) -> None:
        if not 0 <= self.index < 2**32:
            raise ValueError("index must fit an unsigned 32-bit integer")


@dataclass(frozen=True, slots=True)
class SessionPolicy:
    handshake_timeout_seconds: int = 5
    absolute_lifetime_seconds: int = 1800
    idle_timeout_seconds: int = 300
    rotate_before_seconds: int = 60
    max_records_per_direction: int = 1_048_576

    def __post_init__(self) -> None:
        if (
            min(
                self.handshake_timeout_seconds,
                self.absolute_lifetime_seconds,
                self.idle_timeout_seconds,
                self.max_records_per_direction,
            )
            < 1
        ):
            raise ValueError("timeouts and record budget must be positive")
        if not 0 < self.rotate_before_seconds < self.absolute_lifetime_seconds:
            raise ValueError("rotation margin must be positive and shorter than session lifetime")
        if self.max_records_per_direction > 2**64:
            raise ValueError("record budget exceeds the sequence space")


@dataclass(frozen=True, slots=True)
class AuditEvent:
    """Whitelist-only event; callers must not place payloads in event_code/result."""

    event_code: str
    result: str
    request_id: UUID | None = None
    duration_ms: float = 0.0
    byte_count: int = 0


@runtime_checkable
class CryptoBackend(Protocol):
    """Synchronous operations. A validates encodings, lengths and signer IDs."""

    def random_bytes(self, length: int) -> bytes: ...
    def sign_sm2(self, private_key: bytes, message: bytes, *, signer_id: bytes) -> bytes: ...
    def verify_sm2(
        self, public_key: bytes, message: bytes, signature: bytes, *, signer_id: bytes
    ) -> bool: ...
    def encrypt_sm2(self, public_key: bytes, plaintext: bytes) -> bytes: ...
    def decrypt_sm2(self, private_key: bytes, ciphertext: bytes) -> bytes: ...
    def seal_sm4_gcm(self, key: bytes, nonce: bytes, plaintext: bytes, aad: bytes) -> bytes:
        """Return ciphertext followed by a 16-byte tag."""
        ...

    def open_sm4_gcm(
        self, key: bytes, nonce: bytes, ciphertext_and_tag: bytes, aad: bytes
    ) -> bytes:
        """Verify before returning plaintext; raise AuthenticationError on failure."""
        ...

    def hmac_sm3(self, key: bytes, message: bytes) -> bytes: ...


@runtime_checkable
class SecureSession(Protocol):
    @property
    def peer(self) -> PeerIdentity: ...
    @property
    def state(self) -> SessionState: ...
    async def handshake(self) -> None:
        """Activate after mutual authentication and key confirmation; failure closes resources."""
        ...

    async def send(
        self,
        record_type: RecordType,
        plaintext: bytes,
        *,
        request_id: UUID,
        chunk_index: int = 0,
        end_of_message: bool = True,
    ) -> None:
        """Allocate sequence/nonce internally. Caller cannot select keys or sequence numbers."""
        ...

    async def recv(self) -> VerifiedRecord:
        """Single reader; authenticated closure raises SessionClosedError after cleanup."""
        ...

    async def close(self) -> None:
        """Idempotent cleanup. Cancellation paths must release owned resources."""
        ...


@runtime_checkable
class SessionManager(Protocol):
    async def open(self, peer: PeerIdentity) -> SecureSession:
        """Return an ACTIVE session under the configured trust, expiry and capacity policy."""
        ...

    async def accept(self, reader: StreamReader, writer: StreamWriter) -> SecureSession:
        """Authenticate an inbound transport and return ACTIVE; close transport on failure."""
        ...

    async def rotate(self, session: SecureSession) -> SecureSession:
        """Create fresh keys and session ID; old session drains only until its hard expiry."""
        ...

    async def close(self) -> None:
        """Stop accepting sessions and clean up all owned sessions."""
        ...


@runtime_checkable
class InferenceService(Protocol):
    """Implemented by C's client/simulator and B's forwarding service."""

    async def complete(self, request: InferenceRequest) -> InferenceResponse: ...
    def stream(self, request: InferenceRequest) -> AsyncIterator[InferenceChunk]:
        """Optional extension: consecutive chunks and exactly one final chunk; no false success."""
        ...


@runtime_checkable
class AuditSink(Protocol):
    def publish(self, event: AuditEvent) -> bool:
        """Non-blocking bounded enqueue. False means caller must refuse new business."""
        ...
