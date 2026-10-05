"""DTO forwarding boundary with an injected application codec/session adapter."""

import asyncio
from collections.abc import AsyncIterator, Callable
from time import monotonic
from uuid import uuid4

from gateway.config import GatewayConfig
from gateway.contracts import (
    AuditEvent,
    AuditSink,
    AuthenticationError,
    CapacityError,
    GatewayError,
    InferenceChunk,
    InferenceRequest,
    InferenceResponse,
    InferenceService,
    ProtocolError,
    SecureSession,
    SessionManager,
    SessionState,
)


class ForwardingService:
    """B's InferenceService; C's adapter owns DTO serialization on a real session.

    The factory must be trusted application code, not supplied by a client. Call
    complete only after inbound authentication, or use GatewayServer for records.
    This service does not own manager shutdown and is confined to one event loop.
    """

    def __init__(
        self,
        manager: SessionManager,
        config: GatewayConfig,
        audit: AuditSink,
        service_factory: Callable[[SecureSession], InferenceService],
    ) -> None:
        self._manager = manager
        self._config = config
        self._audit = audit
        self._factory = service_factory
        self._active = 0
        self._pending = 0
        self.healthy = True

    def _publish(self, event: AuditEvent, *, terminal: bool = False) -> bool:
        try:
            accepted = self._audit.publish(event)
        except Exception:
            accepted = False
        if terminal and not accepted:
            self.healthy = False
        return accepted

    async def complete(self, request: InferenceRequest) -> InferenceResponse:
        """Forward once to the configured identity, preserving cancellation semantics."""
        limits = self._config.limits
        if (
            not self.healthy
            or self._active >= limits.max_active
            or self._pending >= limits.max_pending
        ):
            raise CapacityError("forwarding capacity exhausted")
        try:
            request_size = sum(
                len(text.encode("utf-8"))
                for text in (request.model, request.prompt, *request.retrieval_context)
            )
        except (UnicodeError, AttributeError):
            raise ProtocolError("invalid request text") from None
        if request_size > limits.max_request_body_bytes:
            raise CapacityError("request body limit exceeded")
        audit_id = uuid4()
        if not self._publish(AuditEvent("request_started", "accepted", audit_id)):
            raise CapacityError("audit capacity exhausted")
        self._active += 1
        session: SecureSession | None = None
        started = monotonic()
        result = "failed"
        size = 0
        try:
            async with asyncio.timeout(limits.request_timeout_seconds):
                self._pending += 1
                try:
                    async with asyncio.timeout(self._config.session.handshake_timeout_seconds):
                        session = await self._manager.open(self._config.upstream)
                finally:
                    self._pending -= 1
                if (
                    session.state is not SessionState.ACTIVE
                    or session.peer != self._config.upstream
                ):
                    raise AuthenticationError("upstream session is not authorized")
                response = await self._factory(session).complete(request)
                if response.request_id != request.request_id:
                    raise ProtocolError("upstream response identifier mismatch")
                size = len(response.output.encode("utf-8"))
                if size > limits.max_response_body_bytes:
                    raise CapacityError("response body limit exceeded")
                result = "ok"
                return response
        except asyncio.CancelledError:
            result = "cancelled"
            raise
        except TimeoutError:
            result = "timeout"
            raise TimeoutError("upstream request timed out") from None
        except Exception:
            raise GatewayError("upstream request failed") from None
        finally:
            self._publish(
                AuditEvent(
                    "request_finished",
                    result,
                    audit_id,
                    duration_ms=(monotonic() - started) * 1000,
                    byte_count=size,
                ),
                terminal=True,
            )
            try:
                if session is not None:
                    try:
                        async with asyncio.timeout(self._config.session.handshake_timeout_seconds):
                            await session.close()
                    except Exception:
                        self.healthy = False
            finally:
                self._active -= 1

    def stream(self, request: InferenceRequest) -> AsyncIterator[InferenceChunk]:
        """Token streaming is outside this ordinary-response delivery."""
        raise NotImplementedError("streaming is not implemented")
