"""Optional process-local relay observation, separate from logs and audit."""

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True, slots=True)
class RelayEvent:
    kind: str
    connection_id: str
    peer: str = ""
    address: str = ""
    request_id: UUID | None = None
    payload: bytes = b""
    final: bool = False
    result: str = ""
