"""Bounded, whitelist-only audit queues. Use on a single event-loop thread."""

import json
import math
from collections import deque
from collections.abc import Callable
from uuid import UUID

from gateway.contracts import AuditEvent

EVENTS = frozenset({"request_started", "request_finished", "request_rejected"})
SECURITY_EVENTS = frozenset({"authentication_failed", "protocol_rejected", "session_failed"})
RESULTS = frozenset({"accepted", "ok", "failed", "timeout", "cancelled", "capacity", "invalid"})


class PrivacyAudit:
    """Writer failures latch unhealthy state and stop admission until restart."""

    def __init__(self, capacity: int = 1024, security_capacity: int = 128) -> None:
        if type(capacity) is not int or type(security_capacity) is not int:
            raise ValueError("audit capacities must be integers")
        if min(capacity, security_capacity) < 1:
            raise ValueError("audit capacities must be positive")
        self._business: deque[str] = deque()
        self._security: deque[str] = deque()
        self._capacity = capacity
        self._security_capacity = security_capacity
        self.healthy = True
        self.rejected = 0
        self.write_failures = 0

    @property
    def queued(self) -> int:
        return len(self._business) + len(self._security)

    def publish(self, event: AuditEvent) -> bool:
        if (
            type(event.event_code) is not str
            or event.event_code not in EVENTS | SECURITY_EVENTS
            or type(event.result) is not str
            or event.result not in RESULTS
            or (event.request_id is not None and type(event.request_id) is not UUID)
            or type(event.duration_ms) not in (int, float)
            or not 0 <= event.duration_ms <= 86400000
            or not math.isfinite(event.duration_ms)
            or type(event.byte_count) is not int
            or not 0 <= event.byte_count < 2**64
        ):
            self.rejected += 1
            return False
        security = event.event_code in SECURITY_EVENTS
        queue = self._security if security else self._business
        capacity = self._security_capacity if security else self._capacity
        if not self.healthy or len(queue) >= capacity:
            self.rejected += 1
            return False
        queue.append(
            json.dumps(
                {
                    "event_code": event.event_code,
                    "result": event.result,
                    "request_id": str(event.request_id) if event.request_id else None,
                    "duration_ms": event.duration_ms,
                    "byte_count": event.byte_count,
                },
                separators=(",", ":"),
                allow_nan=False,
            )
        )
        return True

    def drain(self, write: Callable[[str], object], *, limit: int = 128) -> int:
        """Bounded synchronous batch; caller schedules regularly with a nonblocking writer."""
        if type(limit) is not int or limit < 1:
            raise ValueError("drain limit must be positive")
        count = 0
        while count < limit and self.queued and self.healthy:
            queue = self._security if self._security else self._business
            try:
                write(queue[0] + "\n")
            except Exception:
                self.write_failures += 1
                self.healthy = False
                break
            queue.popleft()
            count += 1
        return count
