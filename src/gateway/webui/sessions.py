"""Bounded, volatile gateway connection and conversation views; never written to logs."""

import codecs
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import UTC, datetime

from gateway.codec import decode_request
from gateway.server.observation import RelayEvent

HISTORY_CAPACITY = 128
TEXT_CAPACITY = 65536


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass
class Connection:
    id: str
    address: str
    peer: str = ""
    phase: str = "pending"
    connected_at: str = field(default_factory=now)
    disconnected_at: str = ""

    def public(self) -> dict[str, object]:
        return vars(self).copy()


@dataclass
class Conversation:
    id: str
    request_id: str
    connection_id: str
    peer: str
    model: str
    prompt: str
    stream: bool
    phase: str = "running"
    output: str = ""
    started_at: str = field(default_factory=now)
    finished_at: str = ""
    truncated: bool = False
    decoder: codecs.IncrementalDecoder = field(
        default_factory=lambda: codecs.getincrementaldecoder("utf-8")("replace")
    )
    prefix: bytes = b""
    header_done: bool = False

    def public(self, *, detail: bool = False) -> dict[str, object]:
        result: dict[str, object] = {
            key: getattr(self, key)
            for key in (
                "id",
                "request_id",
                "connection_id",
                "peer",
                "model",
                "stream",
                "phase",
                "started_at",
                "finished_at",
                "truncated",
            )
        }
        if detail:
            result.update(prompt=self.prompt, output=self.output)
        return result


class SessionHistory:
    def __init__(self) -> None:
        self.connections: OrderedDict[str, Connection] = OrderedDict()
        self.conversations: OrderedDict[str, Conversation] = OrderedDict()

    @staticmethod
    def reserve(items: OrderedDict[str, Connection] | OrderedDict[str, Conversation]) -> bool:
        if len(items) < HISTORY_CAPACITY:
            return True
        for key, item in items.items():
            if item.phase in {"closed", "ok", "failed", "cancelled", "timeout"}:
                del items[key]
                return True
        return False

    def observe(self, event: RelayEvent) -> None:
        if event.kind == "connected":
            if self.reserve(self.connections):
                self.connections[event.connection_id] = Connection(
                    event.connection_id, event.address
                )
            return
        connection = self.connections.get(event.connection_id)
        if connection is None:
            return
        if event.kind == "authenticated":
            connection.peer, connection.phase = event.peer, "active"
        elif event.kind == "closed":
            connection.phase, connection.disconnected_at = "closed", now()
        elif event.kind == "request" and event.request_id is not None:
            request, stream = decode_request(event.payload)
            if request.request_id != event.request_id or not self.reserve(self.conversations):
                return
            identity = f"{event.connection_id}:{event.request_id}"
            self.conversations[identity] = Conversation(
                identity,
                str(event.request_id),
                event.connection_id,
                connection.peer,
                request.model,
                request.prompt[:TEXT_CAPACITY],
                stream,
                truncated=len(request.prompt) > TEXT_CAPACITY,
            )
        elif event.request_id is not None:
            chat = self.conversations.get(f"{event.connection_id}:{event.request_id}")
            if chat is None or chat.connection_id != event.connection_id:
                return
            if event.kind == "response":
                payload = event.payload
                if not chat.stream and not chat.header_done:
                    # Ordinary responses have a fixed 22-byte codec envelope; it can
                    # span records. Keep only the envelope, never a full response copy.
                    needed = 22 - len(chat.prefix)
                    chat.prefix += payload[:needed]
                    payload = payload[needed:]
                    if len(chat.prefix) < 22:
                        return
                    chat.header_done = True
                    if (
                        chat.prefix[:2] != b"\x01\x03"
                        or chat.prefix[2:18] != event.request_id.bytes
                    ):
                        return
                text = chat.decoder.decode(payload, final=event.final)
                remaining = TEXT_CAPACITY - len(chat.prompt) - len(chat.output)
                chat.output += text[:remaining]
                chat.truncated |= len(text) > remaining
            elif event.kind == "finished":
                chat.phase, chat.finished_at = event.result, now()

    def summary(self) -> dict[str, object]:
        return {
            "connections": [item.public() for item in reversed(self.connections.values())],
            "conversations": [item.public() for item in reversed(self.conversations.values())],
            "capacity": HISTORY_CAPACITY,
        }
