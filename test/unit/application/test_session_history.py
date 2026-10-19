"""Volatile conversation retention, split envelopes, Unicode and isolation."""

import json
from uuid import uuid4

import pytest

from gateway.codec import encode_request, encode_response
from gateway.contracts import InferenceRequest, InferenceResponse
from gateway.server.observation import RelayEvent
from gateway.webui.sessions import HISTORY_CAPACITY, TEXT_CAPACITY, SessionHistory


def start(history, connection="connection", request_id=None, stream=False, prompt="你好"):
    request_id = request_id or uuid4()
    history.observe(RelayEvent("connected", connection, address="127.0.0.1:1234"))
    history.observe(RelayEvent("authenticated", connection, peer="client"))
    history.observe(
        RelayEvent(
            "request",
            connection,
            request_id=request_id,
            payload=encode_request(InferenceRequest(request_id, "model", prompt), stream=stream),
        )
    )
    return request_id, f"{connection}:{request_id}"


@pytest.mark.parametrize("stream", [False, True])
def test_split_unicode_live_history_and_summary_privacy(stream):
    history = SessionHistory()
    request_id, identity = start(history, stream=stream)
    output = "中文🙂\n# 回答"
    payload = output.encode() if stream else encode_response(InferenceResponse(request_id, output))
    for index, byte in enumerate(payload):
        history.observe(
            RelayEvent(
                "response",
                "connection",
                request_id=request_id,
                payload=bytes([byte]),
                final=index == len(payload) - 1,
            )
        )
    chat = history.conversations[identity]
    assert chat.output == output and chat.phase == "running"
    assert "你好" not in json.dumps(history.summary(), ensure_ascii=False)
    assert "回答" not in json.dumps(history.summary(), ensure_ascii=False)
    history.observe(RelayEvent("finished", "connection", request_id=request_id, result="ok"))
    history.observe(RelayEvent("closed", "connection"))
    assert chat.phase == "ok" and chat.finished_at
    assert history.connections["connection"].phase == "closed"
    assert chat.public(detail=True)["prompt"] == "你好"


def test_same_request_uuid_on_two_connections_is_isolated():
    history = SessionHistory()
    request_id, first = start(history, "first", stream=True)
    _, second = start(history, "second", request_id=request_id, stream=True, prompt="另一个问题")
    history.observe(RelayEvent("response", "first", request_id=request_id, payload=b"first"))
    assert history.conversations[first].output == "first"
    assert history.conversations[second].output == ""


def test_text_limit_failed_partial_output_and_bounded_history():
    history = SessionHistory()
    request_id, identity = start(history, stream=True)
    history.observe(
        RelayEvent(
            "response", "connection", request_id=request_id, payload=b"x" * (TEXT_CAPACITY + 100)
        )
    )
    history.observe(RelayEvent("finished", "connection", request_id=request_id, result="cancelled"))
    chat = history.conversations[identity]
    assert len(chat.prompt) + len(chat.output) == TEXT_CAPACITY and chat.truncated
    assert chat.phase == "cancelled"
    history.observe(RelayEvent("closed", "connection"))
    for index in range(HISTORY_CAPACITY + 5):
        connection = str(index)
        request_id, _ = start(history, connection)
        history.observe(RelayEvent("finished", connection, request_id=request_id, result="failed"))
        history.observe(RelayEvent("closed", connection))
    assert len(history.connections) == len(history.conversations) == HISTORY_CAPACITY
    assert "connection" not in history.connections and identity not in history.conversations
    assert not SessionHistory().connections


def test_capacity_never_evicts_live_entries():
    history = SessionHistory()
    for index in range(HISTORY_CAPACITY + 2):
        start(history, str(index))
    assert len(history.connections) == len(history.conversations) == HISTORY_CAPACITY
    assert "0" in history.connections and str(HISTORY_CAPACITY) not in history.connections
