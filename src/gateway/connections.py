"""进程内连接观察；只保存连接元数据，不接触密钥和业务正文。"""

from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from time import monotonic
from uuid import uuid4


@dataclass(slots=True)
class Connection:
    source: str
    id: str = field(default_factory=lambda: str(uuid4()))
    started_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    started: float = field(default_factory=monotonic)
    ended: float | None = None
    peer: str | None = None
    phase: str = "handshaking"
    result: str | None = None
    inflight: int = 0
    received_bytes: int = 0
    sent_bytes: int = 0

    def snapshot(self) -> dict[str, object]:
        return {
            "id": self.id,
            "source": self.source,
            "peer": self.peer,
            "phase": self.phase,
            "result": self.result,
            "started_at": self.started_at,
            "duration_seconds": round((self.ended or monotonic()) - self.started, 1),
            "inflight": self.inflight,
            "received_bytes": self.received_bytes,
            "sent_bytes": self.sent_bytes,
        }


class ConnectionMonitor:
    def __init__(self) -> None:
        self.active: dict[str, Connection] = {}
        self.recent: deque[Connection] = deque(maxlen=100)

    def begin(self, address: object) -> Connection:
        source = "unknown"
        if isinstance(address, tuple) and len(address) >= 2:
            source = f"[{address[0]}]:{address[1]}"
        item = Connection(source)
        self.active[item.id] = item
        return item

    def finish(self, item: Connection, result: str) -> None:
        item.ended, item.phase, item.result = monotonic(), "closed", result
        item.inflight = 0
        self.active.pop(item.id, None)
        self.recent.appendleft(item)

    def snapshot(self) -> dict[str, object]:
        return {
            "active": [item.snapshot() for item in list(self.active.values())[:200]],
            "recent": [item.snapshot() for item in self.recent],
            "total_active": len(self.active),
            "truncated": len(self.active) > 200,
        }
