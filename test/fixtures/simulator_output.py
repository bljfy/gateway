"""Validate simulator metadata while comparing its reproducible business body."""

import json
from datetime import UTC, datetime
from uuid import UUID


def response_body(output: str, request_id: UUID | None = None) -> str:
    header, separator, body = output.partition("\n")
    assert separator
    metadata = json.loads(header)
    assert set(metadata) == {"timestamp", "request_id"}
    timestamp = datetime.fromisoformat(metadata["timestamp"])
    assert timestamp.tzinfo == UTC
    identity = UUID(metadata["request_id"])
    if request_id is not None:
        assert identity == request_id
    return body
