"""Strict, immutable B-line configuration; no secret material is loaded here."""

import ipaddress
import tomllib
from dataclasses import dataclass, fields
from pathlib import Path

from gateway.contracts import MAX_RECORD_PLAINTEXT, PeerIdentity, PeerRole, SessionPolicy


def positive(value: object, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


@dataclass(frozen=True, slots=True)
class Limits:
    max_record_plaintext_bytes: int = MAX_RECORD_PLAINTEXT
    max_wire_frame_bytes: int = 131072
    max_request_body_bytes: int = 1048576
    max_response_body_bytes: int = 16777216
    max_inflight_requests_per_session: int = 16
    max_queued_bytes_per_session: int = 1048576
    request_timeout_seconds: int = 120
    max_active: int = 1000
    max_pending: int = 100
    max_plaintext_bytes_per_direction: int = 1073741824

    def __post_init__(self) -> None:
        for item in fields(self):
            positive(getattr(self, item.name), item.name)
        if self.max_record_plaintext_bytes > MAX_RECORD_PLAINTEXT:
            raise ValueError("record limit exceeds contract")
        if self.max_wire_frame_bytes < self.max_record_plaintext_bytes + 128:
            raise ValueError("wire frame budget must include authenticated framing overhead")
        if self.max_request_body_bytes > self.max_queued_bytes_per_session:
            raise ValueError("queue must accommodate one complete request")
        if min(self.max_request_body_bytes, self.max_response_body_bytes) < (
            self.max_record_plaintext_bytes
        ):
            raise ValueError("body limits must accommodate one record")


@dataclass(frozen=True, slots=True)
class GatewayConfig:
    upstream: PeerIdentity
    limits: Limits = Limits()
    session: SessionPolicy = SessionPolicy()
    suite: str = "SM2-SM4-GCM-SM3-v1"
    tag_bytes: int = 16
    audit_capacity: int = 1024
    security_audit_capacity: int = 128
    metrics_host: str = "127.0.0.1"
    metrics_port: int = 9100

    def __post_init__(self) -> None:
        if self.upstream.role is not PeerRole.SIMULATOR:
            raise ValueError("upstream must have simulator role")
        if self.suite != "SM2-SM4-GCM-SM3-v1" or type(self.tag_bytes) is not int:
            raise ValueError("unsupported cipher suite or tag")
        if self.tag_bytes != 16:
            raise ValueError("GCM tag must contain 16 bytes")
        for item in fields(self.session):
            positive(getattr(self.session, item.name), item.name)
        if self.session.idle_timeout_seconds > self.session.absolute_lifetime_seconds:
            raise ValueError("idle timeout exceeds absolute lifetime")
        if self.limits.request_timeout_seconds > (
            self.session.absolute_lifetime_seconds - self.session.rotate_before_seconds
        ):
            raise ValueError("request timeout exceeds usable session lifetime")
        positive(self.audit_capacity, "audit_capacity")
        positive(self.security_audit_capacity, "security_audit_capacity")
        if not ipaddress.ip_address(self.metrics_host).is_loopback:
            raise ValueError("metrics must bind a loopback IP literal")
        if positive(self.metrics_port, "metrics_port") > 65535:
            raise ValueError("invalid metrics port")


def _table(value: object, allowed: set[str]) -> dict[str, object]:
    if not isinstance(value, dict) or any(type(key) is not str for key in value):
        raise ValueError("expected configuration table")
    if set(value) - allowed:
        raise ValueError("unknown configuration field")
    return dict(value)


def load_config(path: Path) -> GatewayConfig:
    """Load once at startup. Errors deliberately omit supplied values and TOML text."""
    try:
        with path.open("rb") as source:
            data = _table(tomllib.load(source), {"upstream", "limits", "session", "gateway"})
        peer = _table(data.get("upstream"), {"peer_id", "key_version"})
        peer_id = peer.get("peer_id")
        if type(peer_id) is not str:
            raise ValueError("upstream identity required")
        version = positive(peer.get("key_version", 1), "key_version")
        limits = _table(data.get("limits", {}), {f.name for f in fields(Limits)})
        session = _table(data.get("session", {}), {f.name for f in fields(SessionPolicy)})
        options = _table(
            data.get("gateway", {}),
            {f.name for f in fields(GatewayConfig)} - {"upstream", "limits", "session"},
        )
        # Validate each primitive before constructing dataclasses from external input.
        ints = {key: positive(value, key) for key, value in limits.items()}
        times = {key: positive(value, key) for key, value in session.items()}
        defaults = GatewayConfig(PeerIdentity(peer_id, PeerRole.SIMULATOR, version))
        for key, value in options.items():
            if type(value) is not type(getattr(defaults, key)):
                raise ValueError("invalid gateway option type")
        return GatewayConfig(
            upstream=defaults.upstream,
            limits=Limits(**ints),
            session=SessionPolicy(**times),
            suite=str(options.get("suite", defaults.suite)),
            tag_bytes=positive(options.get("tag_bytes", defaults.tag_bytes), "tag_bytes"),
            audit_capacity=positive(
                options.get("audit_capacity", defaults.audit_capacity), "audit_capacity"
            ),
            security_audit_capacity=positive(
                options.get("security_audit_capacity", defaults.security_audit_capacity),
                "security_audit_capacity",
            ),
            metrics_host=str(options.get("metrics_host", defaults.metrics_host)),
            metrics_port=positive(
                options.get("metrics_port", defaults.metrics_port), "metrics_port"
            ),
        )
    except (ValueError, TypeError, OSError):
        raise ValueError("invalid gateway configuration") from None
