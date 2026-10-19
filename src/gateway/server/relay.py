"""Bounded ordinary request relay; business payload encoding belongs to C."""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from time import monotonic
from uuid import UUID, uuid4

from gateway.config import GatewayConfig
from gateway.connections import Connection, ConnectionMonitor
from gateway.contracts import (
    AuditEvent,
    AuditSink,
    AuthenticationError,
    CapacityError,
    Direction,
    PeerRole,
    ProtocolError,
    RecordType,
    SecureSession,
    SessionClosedError,
    SessionManager,
    SessionState,
    VerifiedRecord,
)
from gateway.logging import EventLogger
from gateway.metrics import Metrics
from gateway.server.pool import UpstreamPool
from gateway.server.observation import RelayEvent

LOGGER = EventLogger(__name__)


@dataclass(slots=True)
class _Request:
    deadline: float
    audit_id: UUID = field(default_factory=uuid4)
    records: list[VerifiedRecord] = field(default_factory=list)
    size: int = 0
    payload_size: int = 0
    task: asyncio.Task[None] | None = None
    cancelled: bool = False


class GatewayServer:
    """Each request gets its own upstream session; no ambiguous retry or shared reader.

    A owns cryptographic verification, sequence numbers, wire limits and hard expiry.
    The application must configure the injected manager with the same limits/policy.
    This object is owned by one asyncio event loop.
    """

    def __init__(
        self,
        manager: SessionManager,
        config: GatewayConfig,
        audit: AuditSink,
        metrics: Metrics | None = None,
        connections: ConnectionMonitor | None = None,
        observer: Callable[[RelayEvent], None] | None = None,
    ) -> None:
        self.manager = manager
        self.config = config
        self.audit = audit
        self.metrics = metrics if metrics is not None else Metrics()
        self.connections = connections if connections is not None else ConnectionMonitor()
        self._connections: set[asyncio.Task[None]] = set()
        self._pending = 0
        self._active = 0
        self._outbound = 0
        self._opening = 0
        self._closing = False
        self._audit_failed = False
        self.pool = UpstreamPool(manager, config, self.metrics) if config.pool_enabled else None
        self.observer = observer

    def _observe(self, event: RelayEvent) -> None:
        if self.observer is not None:
            try:
                self.observer(event)
            except Exception:
                # A display failure must never interrupt forwarding or expose data in logs.
                pass

    def _publish(self, event: AuditEvent, *, terminal: bool = False) -> bool:
        try:
            accepted = self.audit.publish(event)
        except Exception:
            accepted = False
        if not accepted:
            LOGGER.error("audit_rejected")
            self.metrics.increment("audit_failure")
            if terminal:
                self._audit_failed = True
        else:
            LOGGER.info(
                event.event_code,
                result=event.result,
                audit_id=event.request_id,
                duration_ms=event.duration_ms,
                bytes=event.byte_count,
            )
        return accepted

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Pass to asyncio.start_server; raw bytes never reach the relay directly."""
        connection = self.connections.begin(writer.get_extra_info("peername"))
        connection_id = connection.id
        self._observe(
            RelayEvent(
                "connected", connection_id, address=str(writer.get_extra_info("peername") or "")
            )
        )
        if (
            self._closing
            or self._audit_failed
            or self._pending >= self.config.limits.max_pending
            or self._active + self._pending >= self.config.limits.max_active
        ):
            LOGGER.info("connection_rejected", result="capacity")
            self.metrics.increment("capacity_rejected")
            self.connections.finish(connection, "capacity")
            self._observe(RelayEvent("closed", connection_id))
            writer.close()
            return
        current = asyncio.current_task()
        assert current is not None
        self._connections.add(current)
        self._pending += 1
        self.metrics.pending_sessions += 1
        inbound: SecureSession | None = None
        activated = False
        result = "closed"
        LOGGER.info("client_connection_received")
        try:
            try:
                async with asyncio.timeout(self.config.session.handshake_timeout_seconds):
                    inbound = await self.manager.accept(reader, writer)
                if (
                    inbound.state is not SessionState.ACTIVE
                    or inbound.peer.role is not PeerRole.CLIENT
                ):
                    raise AuthenticationError("inbound session is not authorized")
            except Exception:
                self.metrics.increment("handshake_failure")
                raise
            finally:
                self._pending -= 1
                self.metrics.pending_sessions -= 1
            self.metrics.increment("handshake_success")
            self._active += 1
            self.metrics.active_sessions += 1
            activated = True
            connection.peer, connection.phase = inbound.peer.peer_id, "active"
            LOGGER.info("client_authenticated")
            self._observe(RelayEvent("authenticated", connection_id, peer=inbound.peer.peer_id))
            await self._serve(inbound, connection, connection_id)
        except asyncio.CancelledError:
            result = "stopped"
            raise
        except AuthenticationError:
            result = "authentication_failed"
            self.metrics.increment("integrity_failure")
            self._publish(AuditEvent("authentication_failed", "failed"), terminal=True)
        except CapacityError:
            result = "capacity"
            self.metrics.increment("capacity_rejected")
            self._publish(AuditEvent("request_rejected", "capacity"))
        except ProtocolError:
            result = "protocol_rejected"
            self._publish(AuditEvent("protocol_rejected", "invalid"), terminal=True)
        except TimeoutError:
            result = "timeout"
            self._publish(AuditEvent("session_failed", "timeout"), terminal=True)
        except Exception:
            result = "failed"
            # Exception text can contain business data; never pass it to logs or peers.
            self._publish(AuditEvent("session_failed", "failed"), terminal=True)
        finally:
            try:
                if inbound is not None:
                    await self._close_session(inbound)
            finally:
                if activated:
                    self._active -= 1
                    self.metrics.active_sessions -= 1
                writer.close()
                self._connections.discard(current)
                self.connections.finish(connection, result)
                LOGGER.info("client_connection_closed")
                self._observe(RelayEvent("closed", connection_id))

    async def _close_session(self, session: SecureSession) -> None:
        try:
            async with asyncio.timeout(self.config.session.handshake_timeout_seconds):
                await session.close()
        except Exception:
            self._publish(AuditEvent("session_failed", "failed"), terminal=True)

    async def _serve(
        self,
        inbound: SecureSession,
        connection: Connection | None = None,
        connection_id: str = "",
    ) -> None:
        requests: dict[UUID, _Request] = {}
        sending: set[asyncio.Task[None]] = set()
        send_lock = asyncio.Lock()
        receiving: asyncio.Task[VerifiedRecord] | None = None
        queued = 0
        try:
            while not self._closing:
                if receiving is None:
                    receiving = asyncio.create_task(inbound.recv())
                workers = {item.task for item in requests.values() if item.task is not None}
                deadlines = [item.deadline for item in requests.values()]
                if deadlines and min(deadlines) <= monotonic():
                    raise TimeoutError("request deadline exceeded")
                wait_seconds: float = self.config.session.idle_timeout_seconds
                if deadlines:
                    wait_seconds = min(wait_seconds, max(0, min(deadlines) - monotonic()))
                done, _ = await asyncio.wait(
                    {receiving, *workers},
                    timeout=wait_seconds,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if not done:
                    raise TimeoutError("request or idle deadline exceeded")
                for request_id, finished in list(requests.items()):
                    if finished.task in done:
                        assert finished.task is not None
                        if not (finished.cancelled and finished.task.cancelled()):
                            finished.task.result()
                        queued -= finished.size
                        del requests[request_id]
                        if connection:
                            connection.inflight = len(requests)
                if receiving not in done:
                    continue
                try:
                    record = receiving.result()
                except SessionClosedError:
                    return
                receiving = None
                header = record.header
                if connection:
                    connection.received_bytes += len(record.plaintext)
                if header.direction is not Direction.INITIATOR_TO_ACCEPTOR:
                    raise ProtocolError("invalid inbound direction")
                if len(record.plaintext) > self.config.limits.max_record_plaintext_bytes:
                    raise CapacityError("record limit exceeded")
                if header.record_type is RecordType.CLOSE:
                    return
                if header.record_type is RecordType.HEARTBEAT:
                    self._check_heartbeat(record)
                    continue
                if header.record_type is RecordType.CANCEL:
                    if record.plaintext or header.chunk_index or not header.end_of_message:
                        raise ProtocolError("invalid cancellation")
                    cancelled = requests.get(header.request_id)
                    if cancelled is not None:
                        if cancelled.task is not None:
                            if not cancelled.cancelled:
                                cancelled.cancelled = True
                                cancelled.task.cancel()
                        else:
                            self._publish(
                                AuditEvent("request_finished", "cancelled", cancelled.audit_id),
                                terminal=True,
                            )
                            queued -= cancelled.size
                            del requests[header.request_id]
                            if connection:
                                connection.inflight = len(requests)
                    continue
                if header.record_type is not RecordType.REQUEST or header.request_id.int == 0:
                    raise ProtocolError("expected request")
                item = requests.get(header.request_id)
                if item is None:
                    if inbound.state is not SessionState.ACTIVE:
                        raise ProtocolError("session does not admit new requests")
                    if self._audit_failed or len(requests) >= (
                        self.config.limits.max_inflight_requests_per_session
                    ):
                        raise CapacityError("request capacity exhausted")
                    item = _Request(monotonic() + self.config.limits.request_timeout_seconds)
                    if not self._publish(AuditEvent("request_started", "accepted", item.audit_id)):
                        raise CapacityError("audit capacity exhausted")
                    requests[header.request_id] = item
                    if connection:
                        connection.inflight = len(requests)
                if item.task is not None or header.chunk_index != len(item.records):
                    raise ProtocolError("invalid request fragment sequence")
                if not record.plaintext and not header.end_of_message:
                    raise ProtocolError("empty intermediate fragment")
                # Bound both retained payload and record-object overhead.
                cost = len(record.plaintext) + 128
                if item.payload_size + len(record.plaintext) > (
                    self.config.limits.max_request_body_bytes
                ) or (queued + cost > self.config.limits.max_queued_bytes_per_session):
                    raise CapacityError("request buffer exhausted")
                item.records.append(record)
                item.size += cost
                item.payload_size += len(record.plaintext)
                queued += cost
                if header.end_of_message:
                    item.task = asyncio.create_task(
                        self._forward(inbound, item, send_lock, sending, connection, connection_id)
                    )
        finally:
            tasks: list[asyncio.Task[object]] = []
            # Whole-connection teardown no longer needs to preserve shared sends.
            for send in sending:
                send.cancel()
                tasks.append(send)
            if receiving is not None:
                receiving.cancel()
                tasks.append(receiving)
            for item in requests.values():
                if item.task is not None:
                    item.task.cancel()
                    tasks.append(item.task)
                else:
                    self._publish(
                        AuditEvent("request_finished", "failed", item.audit_id), terminal=True
                    )
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

    @staticmethod
    def _check_heartbeat(record: VerifiedRecord) -> None:
        header = record.header
        if (
            record.plaintext
            or header.request_id.int != 0
            or header.chunk_index != 0
            or not header.end_of_message
        ):
            raise ProtocolError("invalid heartbeat")

    async def _send_response(
        self,
        inbound: SecureSession,
        response: VerifiedRecord,
        deadline: float,
        owned: set[asyncio.Task[None]],
    ) -> None:
        # A cancels the whole session if send is interrupted. Drain this one frame
        # before propagating request cancellation, retaining the shared send lock.
        async def send() -> None:
            async with asyncio.timeout_at(deadline):
                await inbound.send(
                    RecordType.RESPONSE,
                    response.plaintext,
                    request_id=response.header.request_id,
                    chunk_index=response.header.chunk_index,
                    end_of_message=response.header.end_of_message,
                )

        sending = asyncio.create_task(send())
        owned.add(sending)
        cancelled = False
        try:
            while True:
                try:
                    await asyncio.shield(sending)
                    break
                except asyncio.CancelledError:
                    cancelled = True
                    if sending.done():
                        break
                except Exception:
                    if cancelled:
                        raise asyncio.CancelledError from None
                    raise
            if cancelled:
                if not sending.cancelled():
                    sending.exception()
                raise asyncio.CancelledError
        finally:
            owned.discard(sending)

    async def _forward(
        self,
        inbound: SecureSession,
        item: _Request,
        lock: asyncio.Lock,
        sending: set[asyncio.Task[None]],
        connection: Connection | None = None,
        connection_id: str = "",
    ) -> None:
        upstream: SecureSession | None = None
        reserved = False
        started = monotonic()
        result = "failed"
        response_size = 0
        request_id = item.records[0].header.request_id
        if self.observer is not None:
            self._observe(
                RelayEvent(
                    "request",
                    connection_id,
                    request_id=request_id,
                    payload=b"".join(record.plaintext for record in item.records),
                )
            )
        try:
            if (
                self._outbound >= self.config.limits.max_active
                or self._opening >= self.config.limits.max_pending
            ):
                raise CapacityError("upstream capacity exhausted")
            self._outbound += 1
            reserved = True
            async with asyncio.timeout_at(item.deadline):
                self._opening += 1
                try:
                    LOGGER.info("upstream_connecting", audit_id=item.audit_id)
                    if self.pool is not None:
                        upstream = await self.pool.acquire(
                            item.deadline, request_records=len(item.records)
                        )
                    else:
                        async with asyncio.timeout(self.config.session.handshake_timeout_seconds):
                            upstream = await self.manager.open(self.config.upstream)
                finally:
                    self._opening -= 1
                if upstream is inbound:
                    raise AuthenticationError("upstream session must be independent")
                if (
                    upstream.state is not SessionState.ACTIVE
                    or upstream.peer != self.config.upstream
                ):
                    raise AuthenticationError("upstream session is not authorized")
                LOGGER.info("upstream_authenticated", audit_id=item.audit_id)
                for record in item.records:
                    await upstream.send(
                        RecordType.REQUEST,
                        record.plaintext,
                        request_id=request_id,
                        chunk_index=record.header.chunk_index,
                        end_of_message=record.header.end_of_message,
                    )
                LOGGER.info(
                    "request_forwarded",
                    audit_id=item.audit_id,
                    fragments=len(item.records),
                    bytes=item.payload_size,
                )
                index = 0
                while True:
                    response = await upstream.recv()
                    header = response.header
                    if header.direction is not Direction.ACCEPTOR_TO_INITIATOR:
                        raise ProtocolError("invalid upstream direction")
                    if header.record_type is RecordType.HEARTBEAT:
                        self._check_heartbeat(response)
                        continue
                    if (
                        header.record_type is not RecordType.RESPONSE
                        or header.direction is not Direction.ACCEPTOR_TO_INITIATOR
                        or header.request_id != request_id
                        or header.chunk_index != index
                    ):
                        raise ProtocolError("invalid upstream response")
                    if len(response.plaintext) > self.config.limits.max_record_plaintext_bytes:
                        raise CapacityError("response record limit exceeded")
                    if not response.plaintext and not header.end_of_message:
                        raise ProtocolError("empty intermediate response")
                    if index == 0:
                        LOGGER.info("response_started", audit_id=item.audit_id)
                    response_size += len(response.plaintext)
                    if response_size > self.config.limits.max_response_body_bytes:
                        raise CapacityError("response body limit exceeded")
                    async with lock:
                        await self._send_response(inbound, response, item.deadline, sending)
                        if connection:
                            connection.sent_bytes += len(response.plaintext)
                    self._observe(
                        RelayEvent(
                            "response",
                            connection_id,
                            request_id=request_id,
                            payload=response.plaintext,
                            final=header.end_of_message,
                        )
                    )
                    if header.end_of_message:
                        result = "ok"
                        self.metrics.increment("requests_completed")
                        return
                    index += 1
        except asyncio.CancelledError:
            result = "cancelled"
            raise
        except TimeoutError:
            result = "timeout"
            raise
        finally:
            self._observe(
                RelayEvent("finished", connection_id, request_id=request_id, result=result)
            )
            if result != "ok":
                self.metrics.increment("requests_failed")
            audited = self._publish(
                AuditEvent(
                    "request_finished",
                    result,
                    item.audit_id,
                    duration_ms=(monotonic() - started) * 1000,
                    byte_count=response_size,
                ),
                terminal=True,
            )
            try:
                if upstream is not None and upstream is not inbound:
                    if self.pool is not None:
                        await self.pool.release(
                            upstream, reusable=result == "ok" and audited and not self._audit_failed
                        )
                    else:
                        await self._close_session(upstream)
            finally:
                if reserved:
                    self._outbound -= 1

    async def close(self) -> None:
        """Stop admission, cancel owned connections and close the dedicated manager."""
        self._closing = True
        tasks = list(self._connections)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self.pool is not None:
            await self.pool.close()
        await self.manager.close()
