"""Synthetic sensitive markers exercise admission, sinks and CLI configuration."""

import asyncio
import io
import logging
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from gateway.audit import PrivacyAudit
from gateway.cli import main
from gateway.config import GatewayConfig, load_config
from gateway.contracts import AuditEvent, PeerIdentity, PeerRole
from gateway.logging import (
    LEVELS,
    SENSITIVE_FIELDS,
    EventLogger,
    PrivacyFormatter,
    PrivacyStreamHandler,
    console_logging,
    set_log_level,
)


@pytest.mark.parametrize("level", LEVELS)
def test_all_levels_redact_sensitive_and_invalid_metadata(level, caplog):
    marker = "SYNTHETIC_SECRET_密钥\nforged_log"
    fields = dict.fromkeys(SENSITIVE_FIELDS, {"nested": [marker, b"KEY_MARKER"]})
    fields.update({marker: marker, "role": marker, "result": marker, "host": marker})
    fields.update(request_id=marker, audit_id=marker, bytes=True, duration_ms=float("nan"))
    with caplog.at_level(logging.DEBUG, logger="gateway"):
        EventLogger("gateway.test").log(level, "request_finished", **fields)
        EventLogger("gateway.test").log(level, marker, prompt=marker)
    assert marker not in caplog.text
    assert "KEY_MARKER" not in caplog.text
    assert "[REDACTED]" in caplog.text
    assert caplog.records[-1].getMessage() == "log_rejected"
    # Sanitization happens before handlers receive records, not just in formatting.
    assert all(not record.args and record.exc_info is None for record in caplog.records)
    assert marker not in repr([record.__dict__ for record in caplog.records])


def test_metadata_remains_useful():
    output = io.StringIO()
    request_id = uuid4()
    with console_logging(stream=output):
        EventLogger("gateway.test").info(
            "request_finished",
            request_id=request_id,
            result="ok",
            bytes=1024,
            duration_ms=1.25,
            fragments=2,
            stream=True,
            host="::1",
            port=1234,
        )
    line = output.getvalue()
    for expected in (str(request_id), "result=ok", "bytes=1024", "duration_ms=1.25", "host=::1"):
        assert expected in line


@pytest.mark.parametrize("threshold", LEVELS)
def test_level_thresholds_and_audit_independence(threshold):
    output = io.StringIO()
    with console_logging(stream=output):
        set_log_level(threshold)
        for level in LEVELS:
            EventLogger("gateway.test").log(level, "request_started")
        audit = PrivacyAudit()
        assert audit.publish(AuditEvent("request_started", "accepted"))
        lines = []
        assert audit.drain(lines.append) == 1
    lines = output.getvalue().splitlines()
    assert len(lines) == sum(number >= LEVELS[threshold] for number in LEVELS.values())


def test_quiet_and_logger_state_restored_after_failure():
    logger = logging.getLogger("gateway")
    before = (logger.level, logger.propagate, logger.handlers[:])
    output = io.StringIO()
    with pytest.raises(RuntimeError), console_logging(quiet=True, stream=output):
        set_log_level("DEBUG")
        EventLogger("gateway.test").log("WARNING", "request_rejected")
        EventLogger("gateway.test").error("client_failed")
        raise RuntimeError("SYNTHETIC_EXCEPTION")
    assert "request_rejected" not in output.getvalue()
    assert "client_failed" in output.getvalue()
    assert (logger.level, logger.propagate, logger.handlers) == before


def test_raw_logging_exception_extra_and_object_never_formatted():
    class Dangerous:
        def __str__(self):
            raise AssertionError("must not format arbitrary objects")

    output = io.StringIO()
    with console_logging(stream=output):
        logger = logging.getLogger("gateway.raw")
        logger.error(Dangerous())
        logger.error("%s", Dangerous())
        try:
            raise ValueError("SYNTHETIC_EXCEPTION")
        except ValueError:
            logger.exception("client_failed", extra={"prompt": "SYNTHETIC_PROMPT"}, stack_info=True)
        logger.error("request_finished result=SYNTHETIC_OUTPUT prompt=SYNTHETIC_PROMPT")
    assert "SYNTHETIC" not in output.getvalue()
    assert "Traceback" not in output.getvalue()
    assert "Stack" not in output.getvalue()
    assert output.getvalue().count("log_rejected") == 2
    assert "client_failed" in output.getvalue()


@pytest.mark.parametrize("value", [float("inf"), float("nan"), -1, True, "SYNTHETIC"])
def test_invalid_duration_is_redacted(value):
    output = io.StringIO()
    with console_logging(stream=output):
        EventLogger("gateway.test").info("request_finished", duration_ms=value)
    assert "duration_ms=[REDACTED]" in output.getvalue()


def test_formatter_does_not_mutate_shared_record():
    record = logging.LogRecord("gateway", logging.ERROR, "", 0, "SYNTHETIC", (), None)
    before = record.__dict__.copy()
    assert PrivacyFormatter("%(message)s").format(record) == "log_rejected"
    assert record.__dict__ == before


def test_sink_failure_does_not_dump_original_message_or_exception(capsys):
    class FailingStream(io.StringIO):
        def write(self, _text):
            raise OSError("SYNTHETIC_WRITER_EXCEPTION")

    with console_logging(stream=FailingStream()):
        logging.getLogger("gateway.raw").error("SYNTHETIC_SECRET")
    assert capsys.readouterr().err == "log output failed\n"


def test_standalone_handler_defaults_to_privacy_formatter():
    output = io.StringIO()
    handler = PrivacyStreamHandler(output)
    try:
        handler.handle(logging.LogRecord("gateway", logging.ERROR, "", 0, "SYNTHETIC", (), None))
        assert output.getvalue() == "ERROR gateway log_rejected\n"
    finally:
        handler.close()


def test_ipv6_scope_and_hostname_are_not_logged():
    output = io.StringIO()
    with console_logging(stream=output):
        for host in ("::1%SYNTHETIC_SECRET", "SYNTHETIC_SECRET.example"):
            EventLogger("gateway.test").info("server_started", host=host)
    assert "SYNTHETIC_SECRET" not in output.getvalue()
    assert output.getvalue().count("host=[REDACTED]") == 2


@pytest.mark.parametrize("level", LEVELS)
def test_log_level_configuration(tmp_path, level):
    path = tmp_path / "gateway.toml"
    path.write_text(f'[upstream]\npeer_id="adapter"\n[gateway]\nlog_level="{level}"')
    assert load_config(path).log_level == level


@pytest.mark.parametrize("value", ['"debug"', '"SYNTHETIC_SECRET"', "true", "10", "[]"])
def test_invalid_log_level_configuration_does_not_echo(tmp_path, value):
    path = tmp_path / "gateway.toml"
    path.write_text(f'[upstream]\npeer_id="adapter"\n[gateway]\nlog_level={value}')
    with pytest.raises(ValueError, match="^invalid gateway configuration$"):
        load_config(path)


def test_default_and_direct_config_validation():
    config = GatewayConfig(PeerIdentity("adapter", PeerRole.ADAPTER))
    assert config.log_level == "INFO"
    with pytest.raises(ValueError, match="^invalid log level$"):
        replace(config, log_level="SYNTHETIC_SECRET")


@pytest.mark.parametrize("command", ["gateway", "adapter", "client", "demo"])
@pytest.mark.parametrize(
    "level,quiet,expected",
    [("DEBUG", False, True), ("ERROR", False, False), ("DEBUG", True, False)],
)
def test_cli_applies_loaded_level(command, level, quiet, expected, monkeypatch, tmp_path, capsys):
    config = GatewayConfig(PeerIdentity("adapter", PeerRole.ADAPTER), log_level=level)

    async def close():
        pass

    runtime = SimpleNamespace(gateway=config, manager=SimpleNamespace(close=close))
    monkeypatch.setattr("gateway.cli.load_runtime", lambda *_: runtime)

    async def server(_runtime, *, ready, stop=None):
        EventLogger("gateway.test").log("DEBUG", "server_started")
        ready()
        if stop is None:
            await asyncio.Event().wait()

    async def client(*_args, **_kwargs):
        EventLogger("gateway.test").log("DEBUG", "client_connecting")

    monkeypatch.setattr("gateway.cli.run_server", server)
    monkeypatch.setattr("gateway.cli.run_client", client)
    args = (["--quiet"] if quiet else []) + [command]
    if command in {"client", "demo"}:
        args += ["--prompt", "SYNTHETIC_PROMPT"]
    if command == "demo":
        args += ["--directory", str(tmp_path)]
    assert main(args) == 0
    captured = capsys.readouterr()
    assert (" DEBUG " in captured.err) == expected
    assert "SYNTHETIC_PROMPT" not in captured.err
