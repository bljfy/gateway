import asyncio
import json
from dataclasses import replace
from uuid import uuid4

import pytest

from gateway.audit import PrivacyAudit
from gateway.config import GatewayConfig, Limits, load_config
from gateway.contracts import AuditEvent, PeerIdentity, PeerRole
from gateway.metrics import Metrics, start_metrics_server


def test_config_defaults_and_custom_limits(tmp_path):
    path = tmp_path / "gateway.toml"
    path.write_text('[upstream]\npeer_id="simulator"\n[limits]\nmax_pending=7', encoding="utf-8")
    config = load_config(path)
    assert config.upstream == PeerIdentity("simulator", PeerRole.SIMULATOR)
    assert config.limits.max_pending == 7
    assert config.tag_bytes == 16


@pytest.mark.parametrize(
    "text",
    [
        "",
        "[upstream]\npeer_id=23",
        '[upstream]\npeer_id="s"\nkey_version=true',
        '[upstream]\npeer_id="s"\nurl="http://untrusted"',
        '[upstream]\npeer_id="s"\n[limits]\nmax_pending=-1',
        '[upstream]\npeer_id="s"\n[limits]\nmax_pending=true',
        '[upstream]\npeer_id="s"\n[limits]\nmax_record_plaintext_bytes=65537',
        '[upstream]\npeer_id="s"\n[session]\nrotate_before_seconds=1800',
        '[upstream]\npeer_id="s"\n[session]\nidle_timeout_seconds=1801',
        '[upstream]\npeer_id="s"\n[gateway]\ntag_bytes=12',
        '[upstream]\npeer_id="s"\n[gateway]\nsuite="OTHER"',
        '[upstream]\npeer_id="s"\n[gateway]\nmetrics_host="0.0.0.0"',
        '[upstream]\npeer_id="s"\n[gateway]\nmetrics_port=65536',
        '[upstream]\npeer_id="s"\n[gateway]\naudit_capacity=0',
        '[upstream]\npeer_id="s"\n[unknown]\nx=1',
        'secret_marker = "unterminated',
    ],
)
def test_config_rejects_invalid_without_echoing_input(tmp_path, text):
    path = tmp_path / "bad.toml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match="^invalid gateway configuration$"):
        load_config(path)


@pytest.mark.parametrize(
    "changes",
    [
        {"max_wire_frame_bytes": 10},
        {"max_queued_bytes_per_session": 10},
        {"max_response_body_bytes": 10},
        {"request_timeout_seconds": False},
    ],
)
def test_inconsistent_limits(changes):
    with pytest.raises(ValueError):
        replace(Limits(), **changes)


def test_upstream_role_cannot_be_client():
    with pytest.raises(ValueError):
        GatewayConfig(PeerIdentity("client", PeerRole.CLIENT))


def test_audit_reserves_security_queue_and_emits_only_schema():
    audit = PrivacyAudit(1, 1)
    event = AuditEvent("request_started", "accepted", uuid4(), 1.5, 42)
    assert audit.publish(event)
    assert not audit.publish(event)
    assert audit.publish(AuditEvent("authentication_failed", "failed"))
    assert not audit.publish(AuditEvent("authentication_failed", "failed"))
    lines = []
    assert audit.drain(lines.append) == 2
    assert json.loads(lines[0])["event_code"] == "authentication_failed"
    assert set(json.loads(lines[1])) == {
        "event_code",
        "result",
        "request_id",
        "duration_ms",
        "byte_count",
    }
    assert audit.publish(event)


@pytest.mark.parametrize(
    "event",
    [
        AuditEvent("sensitive_prompt", "ok"),
        AuditEvent("request_started", "secret-key"),
        AuditEvent("request_started", "ok", "sensitive_prompt"),
        AuditEvent("request_started", "ok", duration_ms=float("nan")),
        AuditEvent("request_started", "ok", duration_ms=float("inf")),
        AuditEvent("request_started", "ok", byte_count=-1),
        AuditEvent("request_started", "ok", byte_count=True),
    ],
)
def test_audit_invalid_fields_never_written(event):
    audit = PrivacyAudit()
    assert not audit.publish(event)
    assert audit.queued == 0


def test_audit_writer_failure_latches_and_metrics_expose_failure():
    audit = PrivacyAudit()
    assert audit.publish(AuditEvent("request_started", "accepted"))

    def fail(_line):
        raise OSError("sensitive_prompt")

    assert audit.drain(fail) == 0
    assert not audit.healthy
    assert not audit.publish(AuditEvent("request_started", "accepted"))
    metrics = Metrics().render(audit)
    assert b"gateway_audit_healthy 0" in metrics
    assert b"gateway_audit_write_failures_total 1" in metrics
    assert b"sensitive_prompt" not in metrics


@pytest.mark.asyncio
async def test_metrics_loopback_http_and_unknown_path():
    audit = PrivacyAudit()
    metrics = Metrics()
    metrics.increment("handshake_success")
    server = await start_metrics_server(metrics, audit, port=0)
    try:
        for path, status in [("/metrics", b"200 OK"), ("/secret", b"404 Not Found")]:
            reader, writer = await asyncio.open_connection(
                "127.0.0.1", server.sockets[0].getsockname()[1]
            )
            writer.write(f"GET {path} HTTP/1.1\r\nHost: localhost\r\n\r\n".encode())
            await writer.drain()
            response = await asyncio.wait_for(reader.read(), 3)
            assert status in response
            if path == "/metrics":
                assert b"gateway_handshake_success_total 1" in response
            writer.close()
            await writer.wait_closed()
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.1", "localhost"])
async def test_metrics_refuses_non_loopback_literal(host):
    with pytest.raises(ValueError):
        await start_metrics_server(Metrics(), PrivacyAudit(), host=host, port=0)
