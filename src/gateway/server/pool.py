"""Bounded upstream leases: a connection has exactly one business owner."""

import asyncio
import math
from time import monotonic

from gateway.config import GatewayConfig
from gateway.contracts import CapacityError, SecureSession, SessionManager, SessionState
from gateway.metrics import Metrics
from gateway.session import SecuritySession
from gateway.session.core import _await_completion


class UpstreamPool:
    def __init__(self, manager: SessionManager, config: GatewayConfig, metrics: Metrics) -> None:
        self.manager, self.config, self.metrics = manager, config, metrics
        self._idle: list[tuple[SecureSession, float]] = []
        self._all: set[SecureSession] = set()
        self._retiring: set[SecureSession] = set()
        self._opening = 0
        self._waiters = 0
        self._changed = asyncio.Event()
        self._closed = False
        self._reaper: asyncio.Task[None] | None = None

    def _usable(self, session: SecureSession, request_records: int = 1) -> bool:
        if session.state is not SessionState.ACTIVE:
            return False
        if isinstance(session, SecuritySession):
            m = session.manager
            try:
                m._trust(session.peer, outbound=True)
                if m.binding(session.peer, initiator=True) != session.binding:
                    return False
                # Responses may stream in small chunks. Retire used connections
                # before half the record budget is gone, and reserve at least
                # enough records for a maximum-size conventionally split reply.
                response_reserve = max(
                    m.policy.max_records_per_direction // 2,
                    math.ceil(
                        self.config.limits.max_response_body_bytes
                        / self.config.limits.max_record_plaintext_bytes
                    )
                    + 1,
                )
                return (
                    not session.writer.is_closing()
                    and not session.reader.at_eof()
                    and not session._inflight
                    and session._remaining()
                    > self.config.limits.request_timeout_seconds + m.policy.rotate_before_seconds
                    and session._send_sequence + request_records
                    <= m.policy.max_records_per_direction
                    and session._receive_sequence + response_reserve
                    <= m.policy.max_records_per_direction
                    and session._send_bytes + self.config.limits.max_request_body_bytes
                    <= m.max_bytes
                    and session._receive_bytes + self.config.limits.max_response_body_bytes
                    <= m.max_bytes
                )
            except Exception:
                return False
        return True

    async def acquire(self, deadline: float, *, request_records: int = 1) -> SecureSession:
        if self._reaper is None:
            self._reaper = asyncio.create_task(self._reap())
        async with asyncio.timeout_at(deadline):
            while True:
                stale: list[SecureSession] = []
                candidate: SecureSession | None = None
                create = False
                if self._closed:
                    raise CapacityError("upstream pool closed")
                now = monotonic()
                while self._idle:
                    session, returned = self._idle.pop()
                    if now - returned < self.config.pool_idle_seconds and self._usable(
                        session, request_records
                    ):
                        candidate = session
                        break
                    self._all.discard(session)
                    self._retiring.add(session)
                    stale.append(session)
                    self.metrics.increment("pool_evicted")
                if candidate is None:
                    limit = min(self.config.pool_max_connections, self.config.limits.max_active)
                    if (
                        len(self._all) + len(self._retiring) + self._opening < limit
                        and self._opening < self.config.limits.max_pending
                    ):
                        self._opening += 1
                        create = True
                    elif not stale:
                        if self._waiters >= min(
                            self.config.pool_max_waiters, self.config.limits.max_pending
                        ):
                            raise CapacityError("upstream pool queue full")
                        self._waiters += 1
                        self.metrics.increment("pool_wait")
                        try:
                            self._changed.clear()
                            await self._changed.wait()
                        finally:
                            self._waiters -= 1
                        continue
                try:
                    if stale:
                        await self._retire(stale)
                    if candidate is not None:
                        self.metrics.increment("pool_hit")
                        return candidate
                    if create:
                        async with asyncio.timeout(self.config.session.handshake_timeout_seconds):
                            candidate = await self.manager.open(self.config.upstream)
                        if self._closed:
                            await candidate.close()
                            raise CapacityError("upstream pool closed")
                        self._all.add(candidate)
                        return candidate
                except BaseException:
                    if candidate is not None:
                        self._all.discard(candidate)
                        if isinstance(candidate, SecuritySession):
                            candidate._abort()
                        else:
                            await candidate.close()
                    raise
                finally:
                    if create:
                        self._opening -= 1
                        self._changed.set()

    async def release(self, session: SecureSession, *, reusable: bool) -> None:
        keep = not self._closed and reusable and self._usable(session)
        if keep:
            self._idle.append((session, monotonic()))
        else:
            self._all.discard(session)
            self._retiring.add(session)
            self.metrics.increment("pool_evicted")
        self._changed.set()
        if not keep:
            await self._retire([session])

    async def _retire(self, sessions: list[SecureSession]) -> None:
        """Start every close before yielding, then drain even on repeated cancellation."""
        self._retiring.update(sessions)
        try:
            await _await_completion(
                asyncio.gather(*(s.close() for s in sessions), return_exceptions=True)
            )
        finally:
            self._retiring.difference_update(sessions)
            self._changed.set()

    async def _reap(self) -> None:
        try:
            while True:
                await asyncio.sleep(min(1, self.config.pool_idle_seconds))
                stale = [
                    s
                    for s, t in self._idle
                    if monotonic() - t >= self.config.pool_idle_seconds or not self._usable(s)
                ]
                self._idle = [(s, t) for s, t in self._idle if s not in stale]
                for session in stale:
                    self._all.discard(session)
                    self._retiring.add(session)
                self._changed.set()
                for _ in stale:
                    self.metrics.increment("pool_evicted")
                if stale:
                    await self._retire(stale)
        except asyncio.CancelledError:
            pass

    async def close(self) -> None:
        self._closed = True
        self._changed.set()
        if self._reaper:
            self._reaper.cancel()
            await asyncio.gather(self._reaper, return_exceptions=True)
        await self._retire(list(self._all | self._retiring))
        self._all.clear()
        self._idle.clear()
