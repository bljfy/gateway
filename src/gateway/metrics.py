"""Low-cardinality metrics exposed only on a loopback HTTP listener."""

import asyncio
import ipaddress
from collections import Counter

from gateway.audit import PrivacyAudit

COUNTERS = frozenset(
    {
        "handshake_success",
        "handshake_failure",
        "requests_completed",
        "requests_failed",
        "capacity_rejected",
        "integrity_failure",
        "replay_rejected",
        "rotation_failure",
        "audit_failure",
    }
)


class Metrics:
    def __init__(self) -> None:
        self._counts: Counter[str] = Counter()
        self.active_sessions = 0
        self.pending_sessions = 0

    def increment(self, name: str) -> None:
        if name not in COUNTERS:
            raise ValueError("unknown metric")
        self._counts[name] += 1

    def render(self, audit: PrivacyAudit) -> bytes:
        values = {f"{key}_total": self._counts[key] for key in sorted(COUNTERS)}
        values.update(
            active_sessions=self.active_sessions,
            pending_sessions=self.pending_sessions,
            audit_queued=audit.queued,
            audit_healthy=int(audit.healthy),
            audit_rejected_total=audit.rejected,
            audit_write_failures_total=audit.write_failures,
        )
        return "".join(f"gateway_{key} {value}\n" for key, value in values.items()).encode()


async def start_metrics_server(
    metrics: Metrics, audit: PrivacyAudit, *, host: str = "127.0.0.1", port: int = 9100
) -> asyncio.Server:
    """Loopback is the access boundary; deploy only on a trusted local host."""
    if not ipaddress.ip_address(host).is_loopback:
        raise ValueError("metrics requires a loopback IP literal")
    active = 0

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nonlocal active
        if active >= 8:
            writer.close()
            return
        active += 1
        try:
            async with asyncio.timeout(2):
                request = await reader.readuntil(b"\r\n\r\n")
                first = request.split(b"\r\n", 1)[0]
                if first == b"GET /metrics HTTP/1.1":
                    status, body = b"200 OK", metrics.render(audit)
                else:
                    status, body = b"404 Not Found", b""
                writer.write(
                    b"HTTP/1.1 " + status + b"\r\nContent-Type: text/plain; version=0.0.4\r\n"
                    b"Connection: close\r\nContent-Length: "
                    + str(len(body)).encode()
                    + b"\r\n\r\n"
                    + body
                )
                await writer.drain()
        except (
            TimeoutError,
            OSError,
            ValueError,
            asyncio.IncompleteReadError,
            asyncio.LimitOverrunError,
        ):
            pass
        finally:
            active -= 1
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass

    return await asyncio.start_server(handle, host, port, limit=4096)
