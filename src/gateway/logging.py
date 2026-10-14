"""Structured operational logs; sanitize before creating a standard LogRecord."""

import ipaddress
import logging
import math
import sys
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import TextIO, cast
from uuid import UUID

from gateway.audit import EVENTS, RESULTS, SECURITY_EVENTS

LEVELS = {
    name: getattr(logging, name) for name in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
}
LOG_EVENTS = (
    EVENTS
    | SECURITY_EVENTS
    | frozenset(
        {
            "server_started",
            "server_failed",
            "server_stopping",
            "server_stopped",
            "client_connecting",
            "client_authenticated",
            "client_completed",
            "client_failed",
            "audit_rejected",
            "connection_rejected",
            "client_connection_received",
            "client_connection_closed",
            "upstream_connecting",
            "upstream_authenticated",
            "request_forwarded",
            "response_started",
            "log_rejected",
        }
    )
)
SENSITIVE_FIELDS = frozenset(
    {
        "key",
        "keys",
        "session_key",
        "session_keys",
        "private_key",
        "signing_private_key",
        "encryption_private_key",
        "password",
        "token",
        "authorization",
        "prompt",
        "retrieval_context",
        "context",
        "output",
        "model_output",
        "plaintext",
        "payload",
        "request",
        "response",
        "exception",
        "exc_info",
        "stack_info",
    }
)


def validate_level(level: object) -> str:
    """Reject invalid levels without echoing user-supplied configuration."""
    if type(level) is not str or level not in LEVELS:
        raise ValueError("invalid log level")
    return level


def _safe_value(name: str, value: object) -> str | int | float | bool | None:
    if name in {"request_id", "audit_id"} and (value is None or type(value) is UUID):
        return str(value) if value is not None else None
    if name in {"bytes", "fragments", "port"} and type(value) is int:
        if 0 <= value < 2**64 and (name != "port" or value <= 65535):
            return value
    if name == "duration_ms" and type(value) in (int, float):
        number = cast(int | float, value)
        if 0 <= number <= 86400000 and math.isfinite(number):
            return number
    if name == "stream" and type(value) is bool:
        return value
    if name == "role" and type(value) is str and value in {"client", "gateway", "simulator"}:
        return value
    if name == "result" and type(value) is str and value in RESULTS:
        return value
    if name == "host" and type(value) is str and "%" not in value:
        try:
            return str(ipaddress.ip_address(value))
        except ValueError:
            pass
    return "[REDACTED]"


def _message(event: object, fields: Mapping[str, object]) -> str:
    if type(event) is not str or event not in LOG_EVENTS:
        return "log_rejected"
    parts = [event]
    for name, value in fields.items():
        # Unknown field names can themselves contain secrets. Never emit them.
        if name in SENSITIVE_FIELDS:
            parts.append(f"{name}=[REDACTED]")
        elif name in {
            "request_id",
            "audit_id",
            "bytes",
            "fragments",
            "port",
            "duration_ms",
            "stream",
            "role",
            "result",
            "host",
        }:
            parts.append(f"{name}={_safe_value(name, value)}")
    return " ".join(parts)


class EventLogger:
    """Only fixed event names and validated scalar metadata enter logging handlers."""

    def __init__(self, name: str) -> None:
        if name != "gateway" and not name.startswith("gateway."):
            raise ValueError("logger must belong to gateway")
        self._logger = logging.getLogger(name)

    def log(self, level: str, event: str, **fields: object) -> None:
        number = LEVELS[validate_level(level)]
        if self._logger.isEnabledFor(number):
            self._logger.log(number, _message(event, fields))

    def info(self, event: str, **fields: object) -> None:
        self.log("INFO", event, **fields)

    def debug(self, event: str, **fields: object) -> None:
        self.log("DEBUG", event, **fields)

    def warning(self, event: str, **fields: object) -> None:
        self.log("WARNING", event, **fields)

    def error(self, event: str, **fields: object) -> None:
        self.log("ERROR", event, **fields)

    def critical(self, event: str, **fields: object) -> None:
        self.log("CRITICAL", event, **fields)


class PrivacyFormatter(logging.Formatter):
    """Fail closed for raw logging calls, without formatting arbitrary objects."""

    @staticmethod
    def converter(timestamp: float | None = None) -> time.struct_time:
        return time.gmtime(timestamp)

    def format(self, record: logging.LogRecord) -> str:
        # Re-validate even standard logging messages; no exception/stack/extra data.
        message = "log_rejected"
        if type(record.msg) is str and not record.args:
            parts = record.msg.split(" ")
            if parts[0] in LOG_EVENTS:
                metadata: dict[str, object] = {}
                valid = True
                for part in parts[1:]:
                    name, separator, value = part.partition("=")
                    if not separator:
                        valid = False
                        break
                    if value == "[REDACTED]":
                        metadata[name] = None
                    elif name in {"request_id", "audit_id"}:
                        try:
                            metadata[name] = UUID(value) if value != "None" else None
                        except ValueError:
                            valid = False
                    elif name in {"bytes", "fragments", "port"}:
                        try:
                            metadata[name] = int(value)
                        except ValueError:
                            valid = False
                    elif name == "duration_ms":
                        try:
                            metadata[name] = float(value)
                        except ValueError:
                            valid = False
                    elif name == "stream":
                        metadata[name] = value == "True" if value in {"True", "False"} else None
                    else:
                        metadata[name] = value
                if valid:
                    message = _message(parts[0], metadata)
        safe = logging.LogRecord("gateway", record.levelno, "", 0, message, (), None)
        safe.created = record.created
        safe.msecs = record.msecs
        return super().format(safe)


class PrivacyStreamHandler(logging.StreamHandler[TextIO]):
    """Never let logging's sink-error diagnostic dump the original record."""

    def __init__(self, stream: TextIO | None = None) -> None:
        super().__init__(stream)
        self.setFormatter(PrivacyFormatter("%(levelname)s %(name)s %(message)s"))

    def handleError(self, record: logging.LogRecord) -> None:
        try:
            sys.stderr.write("log output failed\n")
        except Exception:
            pass


def set_log_level(level: str) -> None:
    """Apply startup configuration to the gateway namespace."""
    logging.getLogger("gateway").setLevel(LEVELS[validate_level(level)])


@contextmanager
def console_logging(*, quiet: bool = False, stream: TextIO | None = None) -> Iterator[None]:
    """Install a private console sink and restore caller state on every exit path."""
    logger = logging.getLogger("gateway")
    previous_level, previous_propagate = logger.level, logger.propagate
    previous_handlers = logger.handlers[:]
    handler = PrivacyStreamHandler(stream if stream is not None else sys.stderr)
    handler.setLevel(logging.ERROR if quiet else logging.DEBUG)
    handler.setFormatter(
        PrivacyFormatter(
            "%(asctime)s %(levelname)s %(name)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%SZ"
        )
    )
    logger.handlers = [handler]
    logger.propagate = False
    set_log_level("INFO")
    try:
        yield
    finally:
        logger.handlers = previous_handlers
        logger.setLevel(previous_level)
        logger.propagate = previous_propagate
        handler.close()
